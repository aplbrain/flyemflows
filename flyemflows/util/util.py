import os
import re
import socket
import datetime

import psutil
import scipy.ndimage
import numpy as np

from dvidutils import downsample_labels
from neuclease.util import parse_timestamp

def replace_default_entries(array, default_array, marker=-1):
    """
    Overwrite all entries in array that match the given
    marker with the corresponding entry in default_array.
    """
    new_array = np.array(array)
    default_array = np.asarray(default_array)
    assert new_array.shape == default_array.shape
    new_array[:] = np.where(new_array == marker, default_array, new_array)
    
    if isinstance(array, np.ndarray):
        array[:] = new_array
    elif isinstance(array, list):
        # Slicewise assignment is broken for Ruamel sequences,
        # which are often passed to this function.
        # array[:] = new_array.list() # <-- broken
        # https://bitbucket.org/ruamel/yaml/issues/176/commentedseq-does-not-support-slice
        #
        # Use one-by-one item assignment instead:
        for i,val in enumerate(new_array.tolist()):
            array[i] = val
    else:
        raise RuntimeError("This function supports arrays and lists, nothing else.")

DOWNSAMPLE_METHODS = ('subsample', 'zoom', 'grayscale', 'mode', 'labels', 'label')
def downsample(volume, factor, method):
    assert method in DOWNSAMPLE_METHODS
    assert (np.array(volume.shape) % factor == 0).all(), \
        "Volume dimensions must be a multiple of the downsample factor."
    
    if method == 'subsample':
        sl = slice(None, None, factor)
        return volume[(sl,)*volume.ndim].copy('C')
    if method in ('zoom', 'grayscale'): # synonyms
        return scipy.ndimage.zoom(volume, 1/factor)
    if method == 'mode':
        return downsample_labels(volume, factor, False)
    if method in ('labels', 'label'): # synonyms
        return downsample_labels(volume, factor, True)

    raise AssertionError("Shouldn't get here.")

def get_localhost_ip_address():
    """
    Return this machine's own IP address, as seen from the network
    (e.g. 192.168.1.152, not 127.0.0.1)
    """
    try:
        # Determine our own machine's IP address
        # This method is a little hacky because it requires
        # making a connection to some arbitrary external site,
        # but it seems to be more reliable than the method below. 
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("google.com",80))
        ip_addr = s.getsockname()[0]
        s.close()
        
    except socket.gaierror:
        # Warning: This method is simpler, but unreliable on some networks.
        #          For example, on a home Verizon FiOS network it will error out in the best case,
        #          or return the wrong IP in the worst case (if you haven't disabled their DNS
        #          hijacking on your router)
        ip_addr = socket.gethostbyname(socket.gethostname())
    
    return ip_addr
    

def is_port_open(port):
    """
    Return True if the given port is already open on the local machine.
    
    https://stackoverflow.com/questions/19196105/python-how-to-check-if-a-network-port-is-open-on-linux
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.connect_ex(('127.0.0.1',port))
    except ConnectionRefusedError:
        return False
    else:
        return True
    finally:
        sock.close()


def extract_ip_from_link(link):
    """
    Given a link with an IP address instead of a hostname,
    returns the IP (as a string).
    If the link does not contain an IP, returns None.

    Example inputs:
    
        http://127.0.0.1/foo
        http://10.36.111.11:38817/status
        tcp://10.36.111.11:38003
        
    """
    m = re.match(r'.*://(\d+\.\d+\.\d+.\d+).*', link)
    if m:
        return m.groups()[0]
    else:
        return None


def find_processes(search_string):
    """
    Search all processes on the machine for one whose command
    line invocation contains the given search string.
    
    Returns a list of psutil.Process objects of matching processes.
    The list will be empty of no matches were found.

    Note: Certain root-level processes cannot be scanned by this function.
    """
    procs = []
    for p in psutil.process_iter():
        try:
            if search_string in ' '.join(p.cmdline()):
                procs.append(procs)
        except psutil.AccessDenied:
            pass
    return procs


JANELIA_GANGLIA = "cganglia.int.janelia.org/ganglia"
def construct_ganglia_link(hosts, from_timestamp, to_timestamp=None, ganglia_server=JANELIA_GANGLIA):
    if isinstance(hosts, str):
        hosts = [hosts]
    
    if isinstance(from_timestamp, str):
        from_timestamp = parse_timestamp(from_timestamp)

    if isinstance(to_timestamp, str):
        to_timestamp = parse_timestamp(to_timestamp)

    def format_ts(ts):
        if ts is None:
            return ''
        assert isinstance(ts, datetime.datetime)
        year, month, day, hour, minute, *_ = ts.timetuple()
        return f'{month:02}/{day:02}/{year}+{hour:02}:{minute:02}'

    cs = format_ts(from_timestamp)
    ce = format_ts(to_timestamp)
    host_str = '|'.join(hosts)

    url = f'http://{ganglia_server}/?r=custom&cs={cs}&ce={ce}&m=load_one&tab=ch&vn=&hide-hf=false&hreg[]={host_str}'
    return url
