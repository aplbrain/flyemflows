"""Defines the a workflow within the context of DVIDSparkServices.

This module only contains the Workflow class and a special
exception type for workflow errors.

"""
import sys
import os
import time
import requests
import subprocess
from jsonschema import ValidationError
import json
import uuid
import socket

from quilted.filelock import FileLock
from DVIDSparkServices.util import mkdir_p, unicode_to_str
from DVIDSparkServices.json_util import validate_and_inject_defaults
from DVIDSparkServices.workflow.logger import WorkflowLogger

import logging
from logcollector.client_utils import HTTPHandlerWithExtraData, make_log_collecting_decorator, noop_decorator

logger = logging.getLogger(__name__)

    

try:
    #driver_ip_addr = '127.0.0.1'
    driver_ip_addr = socket.gethostbyname(socket.gethostname())
except socket.gaierror:
    # For some reason, that line above fails sometimes
    # (depending on which network you're on)
    # The method below is a little hacky because it requires
    # making a connection to some arbitrary external site,
    # but it seems to be more reliable. 
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("google.com",80))
    driver_ip_addr = s.getsockname()[0]
    s.close()

#
#  workflow exception
class WorkflowError(Exception):
    pass


# defines workflows that work over DVID
class Workflow(object):
    """Base class for all DVIDSparkServices workflow.

    The class handles general workflow functionality such
    as handling the json schema interface required by
    all workflows and starting a spark context.

    """

    OptionsSchema = \
    {
        "type": "object",
        "default": {},
        "additionalProperties": True,

        "properties": {
            ## RESOURCE SERVER
            "resource-server": {
                "description": "If provided, workflows MAY use this resource server to coordinate competing requests from worker nodes. "
                               "Set to the IP address of the (already-running) resource server, or use the special word 'driver' "
                               "to automatically start a new resource server on the driver node.",
                "type": "string",
                "default": ""
            },
            "resource-port": {
                "description": "Which port the resource server is running on.  (See description above.)",
                "type": "integer",
                "default": 0
            },

            ## LOG SERVER
            "log-collector-port": {
                "description": "If provided, a server process will be launched on the driver node to collect certain log messages from worker nodes.",
                "type": "integer",
                "default": 0
            },
            "log-collector-directory": {
                "description": "",
                "type": "string",
                "default": "" # If not provided, a temp directory will be overwritten here.
            },

            "corespertask": {
                "type": "integer",
                "default": 1
            },

            "debug": {
                "description": "Enable certain debugging functionality.  Mandatory for integration tests.",
                "type": "boolean",
                "default": False
            }
        }
    }
    
    def __init__(self, jsonfile, schema, appname):
        """Initialization of workflow object.

        Args:
            jsonfile (dict): json config data for workflow
            schema (dict): json schema for workflow
            appname (str): name of the spark application

        """

        self.config_data = None
        schema_data = json.loads(schema)

        if jsonfile.startswith('http'):
            try:
                self.config_data = requests.get(jsonfile).json()
            except Exception, e:
                raise WorkflowError("Could not load file: ", str(e))
        else:
            try:
                self.config_data = json.load(open(jsonfile))
            except Exception, e:
                raise WorkflowError("Could not load file: ", str(e))

        # validate JSON
        try:
            validate_and_inject_defaults(self.config_data, schema_data)
        except ValidationError, e:
            raise WorkflowError("Validation error: ", str(e))

        # Convert unicode values to str (easier to pass to C++ code)
        self.config_data = unicode_to_str(self.config_data)

        self.workflow_entry_exit_printer = WorkflowLogger(appname)

        # create spark context
        self.sc = self._init_spark(appname)
        
        self._init_logcollector_config(jsonfile)

        self._execution_uuid = str(uuid.uuid1())
        self._worker_task_id = 0

    def _init_spark(self, appname):
        """Internal function to setup spark context
        
        Note: only include spark modules here so that
        the interface can be queried outside of pyspark.

        """
        from pyspark import SparkContext, SparkConf

        # set spark config
        sconfig = SparkConf()
        sconfig.setAppName(appname)

        # check config file for generic corespertask option
        corespertask = 1
        if "corespertask" in self.config_data["options"]:
            corespertask = self.config_data["options"]["corespertask"]

        # always store job info for later retrieval on master
        # set 1 cpu per task for now but potentially allow
        # each workflow to overwrite this for certain high
        # memory situations.  Maxfailures could probably be 1 if rollback
        # mechanisms exist
        sconfig.setAll([("spark.task.cpus", str(corespertask)),
                        ("spark.task.maxFailures", "2")
                       ]
                      )
        #("spark.eventLog.enabled", "true"),
        #("spark.eventLog.dir", "/tmp"), # is this a good idea -- really is temp

        # currently using LZ4 compression: should not degrade runtime much
        # but will help with some operations like shuffling, especially when
        # dealing with things object like highly compressible label volumes
        # NOTE: objects > INT_MAX will cause problems for LZ4
        worker_env = {}
        if "DVIDSPARK_WORKFLOW_TMPDIR" in os.environ and os.environ["DVIDSPARK_WORKFLOW_TMPDIR"]:
            worker_env["DVIDSPARK_WORKFLOW_TMPDIR"] = os.environ["DVIDSPARK_WORKFLOW_TMPDIR"]
        
        # Auto-batching heuristic doesn't work well with our auto-compressed numpy array pickling scheme.
        # Therefore, disable batching with batchSize=1
        return SparkContext(conf=sconfig, batchSize=1, environment=worker_env)


    def _init_logcollector_config(self, config_path):
        """
        If necessary, provide default values for the logcollector settings.
        Also, convert log-collector-directory to an abspath.
        """
        # Not all workflow schemas have been ported to inherit Workflow.OptionsSchema,
        # so we have to manually provide default values
        if "log-collector-directory" not in self.config_data["options"]:
            self.config_data["options"]["log-collector-directory"] = ""
        if "log-collector-port" not in self.config_data["options"]:
            self.config_data["options"]["log-collector-port"] = 0

        # Init logcollector directory
        log_dir = self.config_data["options"]["log-collector-directory"]
        if not log_dir:
            log_dir = '/tmp/' + str(uuid.uuid1())

        # Convert logcollector directory to absolute path,
        # assuming it was relative to config file.
        if not log_dir.startswith('/'):
            assert not config_path.startswith("http"), \
                "Can't use relative path for log collector directory if config is from http."
            config_dir = os.path.dirname( os.path.normpath(config_path) )
            log_dir = os.path.normpath( os.path.join(config_dir, log_dir) )

        self.config_data["options"]["log-collector-directory"] = log_dir

        if self.config_data["options"]["log-collector-port"]:
            mkdir_p(log_dir)


    def collect_log(self, task_key_factory=lambda *args, **kwargs: args[0]):
        """
        Use this as a decorator for functions that are executed in spark workers.
        
        task_key_factory:
            A little function that converts the arguments to your function into a key that identifies
            the log file this function should be directed to.
        
        For example, if you want to group your log messages into files according subvolumes:
        
        class MyWorkflow(Workflow):
            def execute():
                dist_subvolumes = self.sparkdvid_context.parallelize_roi(...)
                
                @self.collect_log(lambda sv: sv.box)
                def process_subvolume(subvolume):
                    logger = logging.getLogger(__name__)
                    logger.info("Processing subvolume: {}".format(subvolume.box))

                    ...
                    
                    return result
                
                dist_subvolumes.mapValues(process_subvolume)
        
        """
        port = self.config_data["options"]["log-collector-port"]
        if port == 0:
            return noop_decorator
        else:
            return make_log_collecting_decorator(driver_ip_addr, port)(task_key_factory)


    def _start_logserver(self):
        """
        If the user's config specifies a non-zero logserver port to use,
        start the logserver as a separate process and return the subprocess.Popen object.
        
        If the user's config doesn't specify a logserver port, return None.
        """
        log_port = self.config_data["options"]["log-collector-port"]
        self.log_dir = self.config_data["options"]["log-collector-directory"]
        
        if log_port == 0:
            return None

        # Start the log server in a separate process
        logserver = subprocess.Popen([sys.executable, '-m', 'logcollector.logserver',
                                      '--log-dir={}'.format(self.log_dir),
                                      '--port={}'.format(log_port)],
                                      #'--debug=True', # See note below about terminate() in debug mode...
                                      stderr=subprocess.STDOUT)
        
        # Wait for the server to actually start up before proceeding...
        try:
            time.sleep(2.0)
            r = requests.get('http://0.0.0.0:{}'.format(log_port), timeout=60.0 )
        except:
            # Retry once if necessary.
            time.sleep(5.0)
            r = requests.get('http://0.0.0.0:{}'.format(log_port), timeout=60.0 )

        r.raise_for_status()

        # Send all driver log messages to the server, too.
        driver_logname = '@_DRIVER_@' # <-- Funky name so it shows up at the top of the list.
        formatter = logging.Formatter('%(levelname)s [%(asctime)s] %(module)s %(message)s')
        handler = HTTPHandlerWithExtraData( { 'task_key': driver_logname },
                                            "0.0.0.0:{}".format(log_port),
                                            '/logsink', 'POST' )
        handler.setFormatter(formatter)
        logging.getLogger().addHandler(handler)
        
        return logserver


    def _start_resource_server(self):
        """
        Initialize the resource server config members and, if necessary,
        start the resource server process on the driver node.
        
        If the resource server is started locally, the "resource-server"
        setting is OVERWRITTEN in the config data with the driver IP.
        
        Returns:
            The resource server Popen object (if started), or None
        """
        # Not all workflow schemas have been ported to inherit Workflow.OptionsSchema,
        # so we have to manually provide default values
        if "resource-server" not in self.config_data["options"]:
            self.config_data["options"]["resource-server"] = ""
        if "resource-port" not in self.config_data["options"]:
            self.config_data["options"]["resource-port"] = 0

        self.resource_server = self.config_data["options"]["resource-server"]
        self.resource_port = self.config_data["options"]["resource-port"]

        if self.resource_server == "":
            return None
        
        if self.resource_port == 0:
            raise RuntimeError("You specified a resource server ({}), but no port"
                               .format(self.resource_server))
        
        if self.resource_server != "driver":
            return None

        # Overwrite config data so workers see our IP address.
        self.config_data["options"]["resource-server"] = driver_ip_addr
        self.resource_server = driver_ip_addr

        logger.info("Starting resource manager on the driver ({})".format(driver_ip_addr))
        resource_server_script = sys.prefix + '/bin/resource_manager.py'
        resource_server_process = subprocess.Popen([sys.executable, resource_server_script, str(self.resource_port)],
                                                   stderr=subprocess.STDOUT)

        return resource_server_process


    def run(self):
        """
        Run the workflow by calling the subclass's execute() function
        (with some startup/shutdown steps before/after).
        """
        log_server_proc = self._start_logserver()
        resource_server_proc = self._start_resource_server()
        
        try:
            self.execute()
        finally:
            sys.stderr.flush()
            
            if resource_server_proc:
                logger.info("Terminating resource manager (PID {})".format(resource_server_proc.pid))
                resource_server_proc.terminate()

            if log_server_proc:
                # NOTE: Apparently the flask server doesn't respond
                #       to SIGTERM if the server is used in debug mode.
                #       If you're using the logserver in debug mode,
                #       you may need to kill it yourself.
                #       See https://github.com/pallets/werkzeug/issues/58
                logger.info("Terminating logserver (PID {})".format(log_server_proc.pid))
                log_server_proc.terminate()

    def run_on_each_worker(self, func):
        """
        Run the given function once per worker node.
        """
        status_filepath = '/tmp/' + self._execution_uuid + str(self._worker_task_id)
        
        @self.collect_log(lambda i: socket.gethostname() + '[' + func.__name__ + ']')
        def task_f(i):
            with FileLock(status_filepath):
                if os.path.exists(status_filepath):
                    return None
                
                # create empty file to indicate the task was executed
                open(status_filepath, 'w')

            func()
            return socket.gethostname()

        num_nodes = self.sc._jsc.sc().getExecutorMemoryStatus().size()
        num_workers = max(1, num_nodes - 1) # Don't count the driver, unless it's the only thing
        
        # It would be nice if we only had to schedule N tasks for N workers,
        # but we couldn't ensure that tasks are hashed 1-to-1 onto workers.
        # Instead, we'll schedule lots of extra tasks, but the logic in
        # task_f() will skip the unnecessary work.
        num_tasks = num_workers * 16

        # Execute the tasks!
        node_names = self.sc.parallelize(list(range(num_tasks)), num_tasks).map(task_f).collect()
        node_names = filter(None, node_names) # Drop Nones

        assert len(set(node_names)) == num_workers, \
            "Tasks were not onto all workers! Nodes processed: \n{}".format(node_names)
        logger.info("Ran {} on {} nodes: {}".format(func.__name__, len(node_names), node_names))
        return node_names

    # make this an explicit abstract method ??
    def execute(self):
        """Children must provide their own execution code"""
        
        raise WorkflowError("No execution function provided")


    # make this an explicit abstract method ??
    @staticmethod
    def dumpschema():
        """Children must provide their own json specification"""

        raise WorkflowError("Derived class must provide a schema")
