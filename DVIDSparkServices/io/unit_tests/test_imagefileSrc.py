import unittest
import numpy as np
import tempfile
import os
from PIL import Image

from DVIDSparkServices.io.partitionSchema import volumePartition, VolumeOffset, VolumeSize, PartitionDims, partitionSchema

from DVIDSparkServices.io.imagefileSrc import imagefileSrc
    
def writeImages(array, startindex=0):
    """Writes 3D numpy array as PNG images (can write up to index 99999)
    """
    
    # write temporary files
    postfix = "img%05d.png" # pad to preserve lexicographical order
    z,y,x = array.shape

    holderfile = tempfile.NamedTemporaryFile()
    dirname, prefix = os.path.split(holderfile.name)
    fnames = []
    prefix = dirname + '/' + prefix
    for zslice in range(0, z):
        imgdata = Image.fromarray(array[zslice,:,:])
        fname = prefix + postfix
        fname = fname % (zslice + startindex)
        imgdata.save(fname)
        fnames.append(fname)

    return prefix + '*.png', prefix + postfix, fnames, 

class TestimagefileSrc(unittest.TestCase):
    """Tests reading files into partitions.

    Note: gbucket not tested.
    """
   
    def test_retrieve_wholevolume(self):
        """Converts 3d numpy array to 2D slices and imports these slices as a 3D volume.

        Note: 
            Also checks that volume offset works properly.
        """
        zplanes = 5 
        arr = np.random.randint(255, size=(zplanes,25,13)).astype(np.uint8)
        filterstr, formatstr, fnames = writeImages(arr, 10)

        schema = partitionSchema(PartitionDims(0,0,0), offset=VolumeOffset(1,0,0))
        imgreader = imagefileSrc(schema, filterstr) 
        partitions = imgreader.extract_volume() 

        for fname in fnames:
            os.remove(fname)

        self.assertEqual(len(partitions), 1)
        finalvol = partitions[0][1]
        match = np.array_equal(arr, finalvol)
        self.assertEqual(match, True)

    def test_retrieve_shiftedpaddedimages(self):
        """Converts 3d numpy array and imports into shifted global space with padding.

        Note:
            Tests min/max plane format string as well.
        """
        zplanes = 32
        arr = np.random.randint(255, size=(zplanes,25,13)).astype(np.uint8)
        filterstr, formatstr, fnames = writeImages(arr, 5)

        schema = partitionSchema(PartitionDims(32,0,0), VolumeOffset(1,3,2), padding=8)
        imgreader = imagefileSrc(schema, formatstr, minmaxplane=(5,5+zplanes)) 
        partitions = imgreader.extract_volume() 

        for fname in fnames:
            os.remove(fname)

        self.assertEqual(len(partitions), 2)
        
        origvol = np.zeros((40, 32, 16), dtype=np.uint8)
        origvol[1:33, 3:28, 2:15] = arr
       
        zoff = partitions[0][0].get_offset().z
        if zoff == 0:
            finalvol = partitions[0][1]
            match = np.array_equal(origvol[0:32,0:32,0:16], finalvol)
            self.assertEqual(match, True)

            finalvol = partitions[1][1]
            match = np.array_equal(origvol[32:40,0:32,0:16], finalvol)
            self.assertEqual(match, True)
        else:
            finalvol = partitions[1][1]
            match = np.array_equal(origvol[0:32,0:32,0:16], finalvol)
            self.assertEqual(match, True)

            finalvol = partitions[0][1]
            match = np.array_equal(origvol[32:40,0:32,0:16], finalvol)
            self.assertEqual(match, True)


    def test_extractpartitionaligned(self):
        """Imports images shifted into partitioned space that is padded entire Z size.
        """
        zplanes = 33
        arr = np.random.randint(255, size=(zplanes,25,13)).astype(np.uint8)
        filterstr, formatstr, fnames = writeImages(arr, 5)

        schema = partitionSchema(PartitionDims(32,0,0), VolumeOffset(1,3,2), padding=32)
        imgreader = imagefileSrc(schema, formatstr, minmaxplane=(5,5+zplanes)) 
        partitions = imgreader.extract_volume() 

        for fname in fnames:
            os.remove(fname)

        self.assertEqual(len(partitions), 2)
        
        origvol = np.zeros((64, 32, 32), dtype=np.uint8)
        origvol[1:34, 3:28, 2:15] = arr
        
        zoff = partitions[0][0].get_offset().z
        if zoff == 0:
            finalvol = partitions[0][1]
            match = np.array_equal(origvol[0:32, :, :], finalvol)
            self.assertEqual(match, True)

            finalvol = partitions[1][1]
            match = np.array_equal(origvol[32:64, :, :], finalvol)
            self.assertEqual(match, True)
        else:
            finalvol = partitions[1][1]
            match = np.array_equal(origvol[0:32, :, :], finalvol)
            self.assertEqual(match, True)

            finalvol = partitions[0][1]
            match = np.array_equal(origvol[32:64, :, :], finalvol)
            self.assertEqual(match, True)


    def test_iteration(self):
        """Reads 32 images at a time and checks the final result is equal to original.
        """
        zplanes = 503
        arr = np.random.randint(255, size=(zplanes,233,112)).astype(np.uint8)
        filterstr, formatstr, fnames = writeImages(arr)

        schema = partitionSchema(PartitionDims(32,64,64), VolumeOffset(35,21,55), padding=32)
        imgreader = imagefileSrc(schema, formatstr, minmaxplane=(0,zplanes)) 
       
        # use the iterator, the iteration size is determiend by the Z partition size
        partitions = []
        for partitions_iter in imgreader:
            partitions.extend(partitions_iter)
        

        for fname in fnames:
            os.remove(fname)

        self.assertEqual(len(partitions), 192)
 
        schema = partitionSchema(PartitionDims(0,0,0))
        res = schema.partition_data(partitions)
        self.assertEqual(len(res), 1)
    
        # data that comes back will be padded by 32
        origvol = np.zeros((512, 256, 160), dtype=np.uint8)
        origvol[3:506, 21:254, 23:135] = arr
            
        finalvol = res[0][1]
        match = np.array_equal(origvol, finalvol)
        self.assertEqual(match, True)

    def test_largeimportandreverse(self):
        """Imports a large volume shifted, partitions, then unpartitions.
        """
        zplanes = 503
        arr = np.random.randint(255, size=(zplanes,233,112)).astype(np.uint8)
        filterstr, formatstr, fnames = writeImages(arr)

        schema = partitionSchema(PartitionDims(32,64,64), VolumeOffset(35,21,55), padding=32)
        imgreader = imagefileSrc(schema, formatstr, minmaxplane=(0,zplanes)) 
        partitions = imgreader.extract_volume() 

        for fname in fnames:
            os.remove(fname)

        self.assertEqual(len(partitions), 192)
 
 
        schema = partitionSchema(PartitionDims(0,0,0))
        res = schema.partition_data(partitions)
        self.assertEqual(len(res), 1)
    
        # data that comes back will be padded by 32
        origvol = np.zeros((512, 256, 160), dtype=np.uint8)
        origvol[3:506, 21:254, 23:135] = arr
            
        finalvol = res[0][1]
        match = np.array_equal(origvol, finalvol)
        self.assertEqual(match, True)


if __name__ == "main":
    unittest.main()
