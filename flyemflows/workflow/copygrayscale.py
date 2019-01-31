import os
import copy
import logging
from functools import partial

import numpy as np
from skimage.util import view_as_blocks

from dvidutils import destripe #@UnresolvedImport 
from dvid_resource_manager.client import ResourceManagerClient

from neuclease.util import Grid, slabs_from_box, Timer, box_to_slicing
from neuclease.dvid import update_extents, reload_metadata, extend_list_value, create_voxel_instance, fetch_repo_instances
from neuclease.focused.hotknife import HEMIBRAIN_TAB_BOUNDARIES

from ..util import replace_default_entries
from ..brick import Brick, BrickWall
from ..volumes import VolumeService, VolumeServiceWriter, GrayscaleVolumeSchema

from . import Workflow

from flyemflows.util.auto_retry import auto_retry
from flyemflows.volumes.dvid_volume_service import DvidVolumeService

logger = logging.getLogger(__name__)

class CopyGrayscale(Workflow):
    ##
    ## TODO: Optionally persist the previous slab so it can be used when
    ##       downsampling the next slab, instead of forcing a re-read of
    ##       the uploaded data.  For thin slabs, the RAM usage won't be
    ##       high, and I/O will be minimized.
    ##
    CopyGrayscaleSchema = \
    {
        "type": "object",
        "description": "Settings specific to the CopyGrayscale workflow",
        "default": {},
        "additionalProperties": False,
        "properties": {
            "min-pyramid-scale": {
                "description": "The first scale to copy from input to output.",
                "type": "integer",
                "minimum": 0,
                "maximum": 10,
                "default": 0
            },
    
            "max-pyramid-scale": {
                "description": "The maximum scale to copy (or compute) from input to output.",
                "type": "integer",
                "minimum": 0,
                "maximum": 10,
                ##
                ## NO DEFAULT: Must choose!
                #"default": -1
            },
            
            "pyramid-source": {
                "description": "How to create the downsampled pyramid volumes, either copied \n"
                               "from the input source (if available) or computed from scale 0.\n",
                "type": "string",
                "enum": ["copy", "compute", "compute-as-labels"], # compute-as-labels is for debug and testing.
                "default": "compute"
            },
            
            "slab-depth": {
                "description": "The volume is processed iteratively, in 'slabs'.\n"
                               "This setting determines the thickness of each slab.\n"
                               "Must be a multiple of the output brick width, in whichever dimension \n"
                               "is specified by the 'slab-axis' setting, below (by default, the Z-axis).\n",
                "type": "integer",
                "minimum": 1
                ##
                ## NO DEFAULT: Must choose!
                #"default": -1
            },
            
            "slab-axis": {
                "description": "The axis across which the volume will be cut to create \n"
                               "'slabs' to be processed one at a time.  See 'slab-depth'.",
                "type": "string",
                "enum": ["x", "y", "z"],
                "default": "z"
            },
            
            "starting-slice": {
                "description": "In case of a failed job, you may want to restart at a particular slice.",
                "type": "integer",
                "default": 0
            },
            
            "contrast-adjustment": {
                "description": "How to adjust the contrast before uploading.",
                "type": "string",
                "enum": ["none", "clahe", "hotknife-destripe"],
                "default": "none"
            },
            
            "hotknife-seams": {
                "description": "Used by the hotknife-destripe contrast adjustment method. \n",
                               "See dvidutils.destripe() for details."
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 2,
                "default": [-1] + HEMIBRAIN_TAB_BOUNDARIES[1:].tolist() # Must begin with -1, not 0
            }
        }
    }

    Schema = copy.deepcopy(Workflow.schema())
    Schema["properties"].update({
        "input": GrayscaleVolumeSchema,
        "output": GrayscaleVolumeSchema,
        "copygrayscale": CopyGrayscaleSchema
    })

    @classmethod
    def schema(cls):
        return CopyGrayscale.Schema


    def _init_services(self):
        """
        Initialize the input and output services,
        and fill in 'auto' config values as needed.
        """
        input_config = self.config["input"]
        output_config = self.config["output"]
        mgr_options = self.config["resource-manager"]

        self.mgr_client = ResourceManagerClient( mgr_options["server"], mgr_options["port"] )
        self.input_service = VolumeService.create_from_config( input_config, os.getcwd(), self.mgr_client )

        replace_default_entries(output_config["geometry"]["bounding-box"], self.input_service.bounding_box_zyx[:, ::-1])
        self.output_service = VolumeService.create_from_config( output_config, os.getcwd(), self.mgr_client )
        assert isinstance( self.output_service, VolumeServiceWriter ), \
            "The output format you are attempting to use does not support writing"

        logger.info(f"Output bounding box: {self.output_service.bounding_box_zyx[:,::-1].tolist()}")


    def _validate_config(self):
        """
        Validate config values.
        """
        options = self.config["copygrayscale"]

        axis_name = options["slab-axis"]
        axis = 'zyx'.index(axis_name)
        brick_width = self.output_service.preferred_message_shape[axis]
        
        assert options["slab-depth"] % brick_width == 0, \
            f'slab-depth ({options["slab-depth"]}) is not a multiple of the output brick width ({brick_width}) along the slab-axis ("{axis_name}")'

        assert (options["starting-slice"] % options["slab-depth"]) == 0, \
            f'starting-slice must be a multiple of the slab depth'

        # Output bounding-box must match exactly (or left as auto)
        input_bb_zyx = self.input_service.bounding_box_zyx
        output_bb_zyx = self.output_service.bounding_box_zyx
        assert ((output_bb_zyx == input_bb_zyx) | (output_bb_zyx == -1)).all(), \
            "Output bounding box must match the input bounding box exactly. (No translation permitted)."

        if options["pyramid-source"] == "copy":
            assert not (set(range(options["max-pyramid-scale"])) - set(self.output_service.available_scales)), \
                ("Can't use 'copy' for pyramid-source.  Not all scales are available in the input.\n"
                f"Available scales are: {self.output_service.available_scales}")

        

    def _prepare_output(self):
        """
        Create DVID data instances (including multi-scale) and update metadata.
        (Only DVID output special handling.  Other formats do not.)
        """
        output_config = self.config["output"]
        max_scale = self.config["copygrayscale"]["max-pyramid-scale"]

        if "dvid" not in output_config:
            return

        server = output_config["dvid"]["server"]
        uuid = output_config["dvid"]["uuid"]
        instance_name = output_config["dvid"]["grayscale-name"]
        block_width = output_config["geometry"]["block-width"]

        compression = output_config["dvid"]["compression"]
        if compression == "raw":
            compression = None

        full_output_box_zyx = np.array(output_config["geometry"]["bounding-box"])[:, ::-1]

        for scale in range(max_scale+1):
            scaled_output_box_zyx = full_output_box_zyx // 2**scale # round down
    
            if scale == 0:
                scaled_instance_name = instance_name
            else:
                scaled_instance_name = f"{instance_name}_{scale}"
    
            if scaled_instance_name in fetch_repo_instances(server, uuid):
                logger.info(f"'{scaled_instance_name}' already exists, skipping creation")
            else:
                create_voxel_instance( server,
                                       uuid,
                                       scaled_instance_name,
                                       typename='uint8blk',
                                       compression=compression,
                                       block_size=block_width )
    
            update_extents( server, uuid, scaled_instance_name, scaled_output_box_zyx )

            # Higher-levels of the pyramid should not appear in the DVID-lite console.
            extend_list_value(server, uuid, '.meta', 'restrictions', [scaled_instance_name])

        # Bottom level of pyramid is listed as neuroglancer-compatible
        extend_list_value(server, uuid, '.meta', 'neuroglancer', [instance_name])

        # We use node-local dvid servers when uploading to a gbucket backend,
        # and the gbucket backend needs to be explicitly reloaded
        # (TODO: Is this still true, or has it been fixed by now?)
        if server.startswith("http://127.0.0.1"):
            @auto_retry(3, 5.0, __name__)
            def reload_meta():
                reload_metadata(server)
            self.run_on_each_worker( reload_meta, once_per_machine=True )


    def execute(self):
        self._init_services()
        self._validate_config()
        self._prepare_output()
        
        options = self.config["copygrayscale"]
        input_bb_zyx = self.input_service.bounding_box_zyx

        min_scale = options["min-pyramid-scale"]
        max_scale = options["max-pyramid-scale"]
        
        starting_slice = options["starting-slice"]
        
        axis_name = options["slab-axis"]
        axis = 'zyx'.index(axis_name)
        slab_boxes = list(slabs_from_box(input_bb_zyx, options["slab-depth"], 0, 'round-down', axis))
        logger.info(f"Processing volume in {len(slab_boxes)} slabs")

        for slab_index, slab_fullres_box_zyx in enumerate(slab_boxes):
            if slab_fullres_box_zyx[0, axis] < starting_slice:
                logger.info(f"Slab {slab_index}: SKIPPING. {slab_fullres_box_zyx[:,::-1].tolist()}")
                continue

            with Timer() as slab_timer:
                logger.info(f"Slab {slab_index}: STARTING. {slab_fullres_box_zyx[:,::-1].tolist()}")
                slab_wall = None
                for scale in range(0, max_scale+1):
                    with Timer() as scale_timer:
                        slab_wall = self._process_slab(scale, slab_fullres_box_zyx, slab_index, len(slab_boxes), slab_wall, min_scale)
                    logger.info(f"Slab {slab_index}: Scale {scale} took {scale_timer.timedelta}")

            logger.info(f"Slab {slab_index}: DONE. ({slab_timer.timedelta})", extra={'status': f"DONE with slab {slab_index}"})

        logger.info(f"DONE exporting {len(slab_boxes)} slabs")


    def _process_slab(self, scale, slab_fullres_box_zyx, slab_index, num_slabs, upscale_slab_wall, min_scale):
        options = self.config["copygrayscale"]
        pyramid_source = options["pyramid-source"]
        output_service = self.output_service

        if scale < min_scale and pyramid_source == "copy":
            logger.info(f"Slab {slab_index}: Skipping scale {scale}")
            return
        
        slab_voxels = np.prod(slab_fullres_box_zyx[1] - slab_fullres_box_zyx[0]) // (2**scale)**3
        voxels_per_thread = slab_voxels // self.total_cores()
        partition_voxels = voxels_per_thread // 2
        logging.info(f"Slab {slab_index}: Aiming for partitions of {partition_voxels} voxels")
        
        if pyramid_source == "copy" or scale == 0:
            # Copy from input source
            bricked_slab_wall = BrickWall.from_volume_service(self.input_service, scale, slab_fullres_box_zyx, self.client, partition_voxels)
            bricked_slab_wall.persist_and_execute(f"Slab {slab_index}: Downloading scale {scale}", logger)
        else:
            # Downsample from previous scale
            if pyramid_source == "compute-as-labels":
                method = "label"
            else:
                method = "grayscale"
            bricked_slab_wall = upscale_slab_wall.downsample( (2,2,2), method )
            bricked_slab_wall.persist_and_execute(f"Slab {slab_index}: Downsampling to scale {scale}", logger)
            del upscale_slab_wall

        if scale == 0:
            bricked_slab_wall = self.adjust_contrast(bricked_slab_wall, slab_index)
        
        # Remap to output bricks
        output_grid = Grid(output_service.preferred_message_shape)
        output_slab_wall = bricked_slab_wall.realign_to_new_grid( output_grid )
        
        # Pad from previously-existing pyramid data until
        # we have full storage blocks, e.g. (64,64,64),
        # but not necessarily full bricks, e.g. (64,64,6400)
        output_accessor_func = partial(output_service.get_subvolume, scale=scale)

        # But don't bother fetching real data for scale 0
        # the input slabs are already block-aligned, and the edges of each slice will be zeros anyway.
        if scale == 0:
            output_accessor_func = lambda _box: 0

        if isinstance( output_service.base_service, DvidVolumeService):
            # For DVID, we use minimum padding (just pad up to the
            # nearest block boundary, not the whole brick boundary).
            padding_grid = Grid( 3*(output_service.block_width,), output_grid.offset )
        else:
            padding_grid = output_slab_wall.grid
        
        padded_slab_wall = output_slab_wall.fill_missing(output_accessor_func, padding_grid)
        padded_slab_wall.persist_and_execute(f"Slab {slab_index}: Assembling scale {scale} bricks", logger)

        # Discard original bricks
        del bricked_slab_wall

        if scale < min_scale:
            logger.info(f"Slab {slab_index}: Not writing scale {scale}")
            return padded_slab_wall

        logger.info(f"Slab {slab_index}: Writing scale {scale}", extra={"status": f"Writing {slab_index}/{num_slabs}"})
        def _write(brick):
            write_brick(output_service, scale, brick)
        padded_slab_wall.bricks.map(_write).compute()

        return padded_slab_wall


    def adjust_contrast(self, bricked_slab_wall, slab_index):
        options = self.config["copygrayscale"]
        contrast_adjustment = options["contrast-adjustment"]

        if contrast_adjustment == "none":
            return bricked_slab_wall
        
        if contrast_adjustment == "hotknife-destripe":
            return self._hotknife_destripe(bricked_slab_wall, slab_index)
        
        if contrast_adjustment == "clahe":
            return self._clahe_adjust(bricked_slab_wall, slab_index)


    def _hotknife_destripe(self, bricked_slab_wall, slab_index):
        options = self.config["copygrayscale"]
        assert options["slab-axis"] == 'z', \
            "To use hotknife-destripe, processing slabs must be cut across the Z axis"

        wall_shape = self.output_service.bounding_box_zyx[1] - self.output_service.bounding_box_zyx[0]
        z_slice_shape = (1,) + (*wall_shape[1:],)
        z_slice_grid = Grid( z_slice_shape )
        
        z_slice_slab = bricked_slab_wall.realign_to_new_grid( z_slice_grid )
        z_slice_slab.persist_and_execute(f"Slab {slab_index}: Constructing slices of shape {z_slice_shape}", logger)

        # This assertion could be lifted if we adjust seams as needed before calling destripe(),
        # but for now I have no use-case for volumes that don't start at (0,0)
        assert (bricked_slab_wall.bounding_box[0, 1:] == (0,0)).all(), \
            "Input bounding box must start at YX == (0,0)"
        
        seams = options["hotknife-seams"]
        def destripe_brick(brick):
            assert brick.volume.shape[0] == 1
            adjusted_slice = destripe(brick.volume[0], seams)
            return Brick(brick.logical_box, brick.physical_box, adjusted_slice[None], location_id=brick.location_id)
        
        adjusted_bricks = z_slice_slab.bricks.map(destripe_brick)
        adjusted_wall = BrickWall( bricked_slab_wall.bounding_box,
                                   bricked_slab_wall.grid,
                                   adjusted_bricks )
        
        adjusted_wall.persist_and_execute(f"Slab {slab_index}: Destriping slices", logger)
        return adjusted_wall


    def _clahe_adjust(self, bricked_slab_wall, slab_index):
        raise NotImplementedError


def write_brick(output_service, scale, brick):
    # For most outputs, we just write the whole brick.
    if not isinstance( output_service.base_service, DvidVolumeService):
        output_service.write_subvolume(brick.volume, brick.physical_box[0], scale)

    # For dvid outputs, we add some special checks/optimizations
    else:
        shape = np.array(brick.volume.shape)
        assert (shape[0:2] == output_service.block_width).all()
        assert shape[2] % output_service.block_width == 0
        
        # Omit leading/trailing empty blocks
        block_width = output_service.block_width
        assert (np.array(brick.volume.shape) % block_width).all() == 0
        blockwise_view = view_as_blocks( brick.volume, brick.volume.shape[0:2] + (block_width,) )
        
        # blockwise view has shape (1,1,X/bx, bz, by, bx)
        assert blockwise_view.shape[0:2] == (1,1)
        blockwise_view = blockwise_view[0,0] # drop singleton axes
        
        block_maxes = blockwise_view.max( axis=(1,2,3) )
        assert block_maxes.ndim == 1
        
        nonzero_block_indexes = np.nonzero(block_maxes)[0]
        if len(nonzero_block_indexes) == 0:
            return # brick is completely empty
        
        first_nonzero_block = nonzero_block_indexes[0]
        last_nonzero_block = nonzero_block_indexes[-1]
        
        nonzero_start = (0, 0, block_width*first_nonzero_block)
        nonzero_stop = ( brick.volume.shape[0:2] + (block_width*(last_nonzero_block+1),) )
        nonzero_subvol = brick.volume[box_to_slicing(nonzero_start, nonzero_stop)]
        nonzero_subvol = np.asarray(nonzero_subvol, order='C')
    
        output_service.write_subvolume(nonzero_subvol, brick.physical_box[0] + nonzero_start, scale)

