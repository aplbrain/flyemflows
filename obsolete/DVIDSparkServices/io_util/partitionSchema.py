"""Implements functionality to partition large datasets.

This file contains a class to distinguish partitions
in data volumes and a schema to create new partitions.
"""
from __future__ import division

import numpy as np
from collections import namedtuple, defaultdict
from itertools import chain

# x,y,z offset of data (z,y,x order)
VolumeOffset = namedtuple('VolumeOffset', 'z y x')

# x,y,z size of data (z,y,x order)
VolumeSize = namedtuple('VolumeSize', 'z y x')

# the size of the partition dimensions (z,y,x  order)
PartitionDims = namedtuple('PartitionDims', 'zsize ysize xsize')

class volumePartition(object):
    """Defines a volume partition and index used to group partitions.

    Note: the provided index should be unique.
    """

    def __init__(self, index, offset, reloffset=VolumeOffset(0,0,0), volsize=VolumeSize(0,0,0), mask=None):
        """Init.

        TODO: support a compressed structure for data mask

        Args:
            index (hashable type): Defines hashable unique index (often just the offset)
            offset (VolumeOffset): z,y,x offset for partition in global space
            reloffset (VolumeOffset): the relative offset where data exists
            volsize (VolumeSize): size of the partition
            mask (numpy): defines which bits of data have been written (0 means unwritten)
        """
        if isinstance(index, np.ndarray):
            # Ensure hashable
            assert index.ndim == 1
            index = tuple(index)
        self.index = index 
        self.offset = VolumeOffset(*offset)
        self.reloffset = VolumeOffset(*reloffset)
        self.volsize = VolumeSize(*volsize)
        self.mask = mask

    def __repr__(self):
        return "volumePartition({}, {}, {}, {}, {})".format( self.index, self.offset, self.reloffset, self.volsize, self.mask )

    def __str__(self):
        return self.__repr__()

    def __eq__(self, other):
        """Equality only done over index.
        """
        return self.index == other.index

    def __ne__(self, other):
        """Equality only done over index.
        """
        return not self.__eq__(other)
    
    def __hash__(self):
        """Hash only considers index.
        """
        return hash(self.index)
    
    def get_offset(self):
        return self.offset
    
    def get_volsize(self):
        return self.volsize
    
    def get_reloffset(self):
        return self.reloffset

    def bounding_box(self):
        bb = np.zeros((2,3), dtype=int)
        bb[0] = self.offset
        bb[0] += self.reloffset
        bb[1] = bb[0] + self.volsize
        return bb
    

class partitionSchema(object):
    """Defines a 3D volume and its partitioning.

    The partition size defines a tiling of the 3D volume.
    The class provides a container to hold partition information
    and the ability to take an RDD or numpy volumes and
    partition it according to the defined schema.

    TOOD:
        Support partition offset and volume size and the ability
        to create partitions of None based on schema.
    """

    def __init__(self, partdims=PartitionDims(0,0,0),
            volume_size=None, blank_delimiter=0, padding=0, enablemask=False):
        """Init

        Args:
            partdims (PartitionDims): Dimension for each partition (0 means unbounded)
            volume_size ((VolumeSize)): size of the volume
            blank_delimiter (blank_delimeter): value of empty data
            padding (int): block aligned padding around actual data (if not block aligned)
            enablemask (boolean): generates mask for in each partition where data exists 
       
        Note:
            Each specified partition dimension should be a multiple of padding (if specified).
        """

        assert (padding == 0) or (np.array(partdims) % padding == 0).all(), \
            f"PartitionDims ({partdims}) must be a multiple of the padding ({padding})"

        self.partdims = PartitionDims(*partdims)
        self.volume_size = volume_size
        if volume_size:
            self.volume_size = VolumeSize(*volume_size)
        self.blank_delimiter = blank_delimiter
        self.enablemask = enablemask
        self.padding = padding

    def get_partdims(self):
        return self.partdims

    def partition_data(self, data): 
        """Repartition the input data to match the schema
        
        Args:
            data ([partition, numpy array]) | (RDD): data to be repartitioned 
        """
        usespark = True
        if type(data) == list:
            usespark = False

        # divide into subpartitions and assign new partition addresses
        dataflat = self._assignPartitions(data, usespark) 

        # group subpartitions
        datagroup = self._groupPartitions(dataflat, usespark) 

        # merge subpartitions in partition and pad
        # is is possible for data to not fill up the entire partition
        return self._padAndSplice(datagroup, usespark)


    def _assignPartitions(self, data, usespark):
        """Splits partitions and reassigns to new partition addresses.
        
        Args:
            data ([volumenumpy], RDD): data to be repartitioned
            usespark: uses Apache spark to process data
        """

        partdims = self.partdims
        def assignPartitions(partvolume):
            """Splits an input numpy array and assigns to different partitions for grouping.
                
            The volumePartition will define the global offset of this volume and
            whether it has a data border.

            Notes:
                This closure requires the schema partition dimensions.  If a given
                dimension size is 0, the partition is assumed to be infinite in that direction.
                The partition dimensions are meant to tile the 3D coordinate system
                starting at the origin.

                The partition offsets must be >= 0.
                
            TODO:
                Allow a non-0 border to be specified in the schema and applied for non-infinite dimensions.

                Allow negative partition offsets.

            Args:
                partvolume ((volumePartition, 2D/3D numpy array)): input volume
            Returns:
                [(volumePartition, (VolumeOffset, 3D numpy array))]
            """
            
            part, volume = partvolume
            
            # no-op blank volumes
            if volume is None:
                return []

            # extract volume size
            if len(volume.shape) == 2:
                volume = np.expand_dims(volume, axis=0)
            zsize, ysize, xsize = volume.shape

            # determine new partition address
            xaddr = yaddr = zaddr = 0
            xaddr2 = yaddr2 = zaddr2 = 1
            offset = part.get_offset()
            reloffset = part.get_reloffset()

            # shift offset if the data is locally shifted
            offset = VolumeOffset(offset.z+reloffset.z, offset.y+reloffset.y, offset.x + reloffset.x)

            if partdims.zsize > 0:
                zaddr = offset.z // partdims.zsize
                zaddr2 = (offset.z + zsize-1) // partdims.zsize + 1
            if partdims.ysize > 0:
                yaddr = offset.y // partdims.ysize
                yaddr2 = (offset.y + ysize-1) // partdims.ysize + 1
            if partdims.xsize > 0:
                xaddr = offset.x // partdims.xsize
                xaddr2 = (offset.x + xsize-1) // partdims.xsize + 1

            # partition volume based on schema 
            partitions = []
            for z in range(zaddr, zaddr2):
                for y in range(yaddr, yaddr2):
                    for x in range(xaddr, xaddr2):
                        # offset relative to volume start
                        z1local = max(0, z*partdims.zsize - offset.z)
                        y1local = max(0, y*partdims.ysize - offset.y)
                        x1local = max(0, x*partdims.xsize - offset.x)

                        # find size of this subpartition
                        z2local = zsize
                        if partdims.zsize > 0:
                            z2local = min(zsize, (z+1)*partdims.zsize - offset.z)
                        y2local = ysize
                        if partdims.ysize > 0:
                            y2local = min(ysize, (y+1)*partdims.ysize - offset.y)
                        x2local = xsize
                        if partdims.xsize > 0:
                            x2local = min(xsize, (x+1)*partdims.xsize - offset.x)

                        # where is subpartition compared to new partition size (when specified)
                        relpartz = relparty = relpartx = 0
                        if z == zaddr:
                            if partdims.zsize != 0:
                                relpartz = offset.z % partdims.zsize
                            else:
                                relpartz = offset.z
                        if y == yaddr:
                            if partdims.ysize != 0:
                                relparty = offset.y % partdims.ysize
                            else:
                                relparty = offset.y
                        if x == xaddr:
                            if partdims.xsize != 0:
                                relpartx = offset.x % partdims.xsize
                            else:
                                relpartx = offset.x

                        # map all partitions as an array
                        subvol = volume[z1local:z2local, y1local:y2local, x1local:x2local]
                        subvol_partition = volumePartition((z,y,x), VolumeOffset(z*partdims.zsize, y*partdims.ysize, x*partdims.xsize))
                        subvol_offset = VolumeOffset(relpartz, relparty, relpartx)
                        partitions.append( (subvol_partition, (subvol_offset, subvol)) )

            return partitions 

        if not usespark:
            # remap each partition in list
            return chain(*map(assignPartitions, data))
        else:
            # RDD -> RDD (will likely involve data shuffling)
            dataflat = data.flatMap(assignPartitions)
            return dataflat

    def _groupPartitions(self, dataflat, usespark):
        """Groups subpartitions into same partition.
        """
        if usespark:
            return dataflat.groupByKey()

        partitions = defaultdict(lambda: [])
        for k,v in dataflat:
            partitions[k].append(v)
        return partitions.items()

    def _padAndSplice(self, datagroup, usespark):
        delimiter = self.blank_delimiter
        padding = self.padding # typically a multiple of blocksize
        enablemask = self.enablemask

        def padAndSplice(subpartitions):
            """Pads a partition with a specified delimiter.
            
            Arg:
                subpartitions (partition, list): list of subpartitions to combine 
            """

            import sys      
            part, partitions = subpartitions
            glbx = glby = glbz = sys.maxsize
            glbx2 = glby2 = glbz2 = 0
        
            # extract bbox
            for (subpart, volume) in partitions:
                # volume always 3D now
                zsize, ysize, xsize = volume.shape
                dtype = volume.dtype
   
                # find bounds
                if subpart.x < glbx:
                    glbx = subpart.x
                if subpart.y < glby:
                    glby = subpart.y
                if subpart.z < glbz:
                    glbz = subpart.z

                if (subpart.x+xsize) > glbx2:
                    glbx2 = subpart.x + xsize
                if (subpart.y+ysize) > glby2:
                    glby2 = subpart.y + ysize
                if (subpart.z+zsize) > glbz2:
                    glbz2 = subpart.z + zsize
      
            # find bbox padding
            if padding > 0:
                glbx -= (glbx % padding)
                glby -= (glby % padding)
                glbz -= (glbz % padding)
                
                if (glbx2 % padding) != 0:
                    glbx2 += (padding - (glbx2 % padding))
                if (glby2 % padding) != 0:
                    glby2 += (padding - (glby2 % padding))
                if (glbz2 % padding) != 0:
                    glbz2 += (padding - (glbz2 % padding))

            # create buffer
            newvol = np.zeros(((glbz2-glbz), (glby2-glby), (glbx2-glbx)), dtype=dtype)
            mask = np.zeros(((glbz2-glbz), (glby2-glby), (glbx2-glbx)), dtype=np.uint8)
            
            newvol[:,:,:] = delimiter

            # set new partition index, offset
            # !! setting the shift of data is not necessary -- any patching of
            # data should seek to overwrite delimited data either near boundaries
            # or for anything in the volume

            # partition index could be shifted 
            offset = part.get_offset()

            # copy subparts to correct partition
            for (subpart, volume) in partitions:
                zstart = subpart.z-glbz
                ystart = subpart.y-glby
                xstart = subpart.x-glbx

                zs, ys, xs = volume.shape
                newvol[zstart:zstart+zs,ystart:ystart+ys,xstart:xstart+xs] = volume
                mask[zstart:zstart+zs,ystart:ystart+ys,xstart:xstart+xs] = 1
          
            # if no masking, do not mask
            if 0 not in mask or not enablemask:
                mask = None

            newpartition = volumePartition((offset.z, offset.y, offset.x), part.get_offset(), reloffset=VolumeOffset(glbz,glby,glbx), mask=mask) 

            return (newpartition, newvol)

        if not usespark:
            return list(map(padAndSplice, list(datagroup.items())))
        else:
            return datagroup.map(padAndSplice)



