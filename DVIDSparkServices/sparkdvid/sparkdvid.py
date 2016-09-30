"""Contains core functionality for interfacing with DVID using Spark.

This module defines a sparkdvid type that allows for basic
reading and writing operations on DVID using RDDs.  The fundamental
unit of many of the functions is the Subvolume.

Helper functions for different workflow algorithms that work
with sparkdvid can be found in DVIDSparkServices.reconutil

Note: the RDDs for many of these transformations use the
unique subvolume key.  The mapping operations map only the values
and perserve the partitioner to make future joins faster.

Note: Access to DVID is done through the python bindings to libdvid-cpp.
For now, all volume GET/POST acceses are throttled (only one at a time)
because DVID is only being run on one server.  This will obviously
greatly reduce scalability but will be changed as soon as DVID
is backed by a clustered DB.

"""

from DVIDSparkServices.sparkdvid.Subvolume import Subvolume

import logging
logger = logging.getLogger(__name__)

from DVIDSparkServices.auto_retry import auto_retry

# masks data to 0 if outside of ROI stored in subvolume
def mask_roi(data, subvolume, border=-1):
    import numpy
    mask = numpy.zeros(data.shape)
    if border == -1:
        border = subvolume.border

    for blk in subvolume.intersecting_blocks:
        # grab range of block
        x1,y1,z1 = blk
        x1 *= subvolume.roi_blocksize
        y1 *= subvolume.roi_blocksize
        z1 *= subvolume.roi_blocksize

        # adjust global location
        x1 -= (subvolume.roi.x1-border)
        if x1 < 0:
            x1 = 0
        y1 -= (subvolume.roi.y1-border)
        if y1 < 0:
            y1 = 0
        z1 -= (subvolume.roi.z1-border)
        if z1 < 0:
            z1 = 0
        
        x2 = x1+subvolume.roi_blocksize
        if x2 > (subvolume.roi.x2 + border):
            x2 = subvolume.roi.x2 + border
        
        y2 = y1+subvolume.roi_blocksize
        if y2 > (subvolume.roi.y2 + border):
            y2 = subvolume.roi.y2 + border
        
        z2 = z1+subvolume.roi_blocksize
        if z2 > (subvolume.roi.z2 + border):
            z2 = subvolume.roi.z2 + border

        mask[z1:z2,y1:y2,x1:x2] = 1
    data[mask==0] = 0



def retrieve_node_service(server, uuid, appname="sparkservices"):
    """Create a DVID node service object"""

    server = str(server)  
   
    # refresh dvid server meta if localhost (since it is exclusive or points to global db)
    """
    if server.startswith("http://127.0.0.1") or  \
            server.startswith("http://localhost") or  \
            server.startswith("127.0.0.1") or server.startswith("localhost"):
        
        import os
        if not os.path.exists("/tmp/reloaded.hack"):
            import requests
            addr = server + "/api/server/reload-metadata"
            if not server.startswith("http://"):
                addr = "http://" + addr

            requests.post(addr)
            open("/tmp/reloaded.hack", 'w').close()
    """

    from libdvid import DVIDNodeService
    import os
    username = os.environ["USER"]
    node_service = DVIDNodeService(server, str(uuid), username, appname)

    return node_service

class sparkdvid(object):
    """Creates a spark dvid context that holds the spark context.

    Note: only the server name, context, and uuid are stored in the
    object to help reduce costs of serializing/deserializing the object.

    """
    
    BLK_SIZE = 32
    
    def __init__(self, context, dvid_server, dvid_uuid):
        """Initialize object

        Args:
            context: spark context
            dvid_server (str): location of dvid server (e.g. emdata2:8000)
            dvid_uuid (str): DVID dataset unique version identifier
       
        """

        self.sc = context
        self.dvid_server = dvid_server
        self.uuid = dvid_uuid

    # Produce RDDs for each subvolume partition (this will replace default implementation)
    # Treats subvolum index as the RDD key and maximizes partition count for now
    # Assumes disjoint subsvolumes in ROI
    def parallelize_roi(self, roi, chunk_size, border=0, find_neighbors=False):
        """Creates an RDD from subvolumes found in an ROI.

        This is analogous to the Spark parallelize function.
        It currently defines the number of partitions as the 
        number of subvolumes.

        TODO: implement general partitioner given other
        input such as bounding box coordinates.
        
        Args:
            roi (str): name of DVID ROI at current server and uuid
            chunk_size (int): the desired dimension of the subvolume
            border (int): size of the border surrounding the subvolume
            find_neighbors (bool): whether to identify neighbors

        Returns:
            RDD as [(subvolume id, subvolume)] and # of subvolumes

        """

        # (map blocks to y,z lines, iterate everything including border and add relevant xy lines) 

        # function will export and should include dependencies
        subvolumes = [] # x,y,z,x2,y2,z2

        # extract roi for a given chunk size
        node_service = retrieve_node_service(self.dvid_server, self.uuid)
        substacks, packing_factor = node_service.get_roi_partition(str(roi), chunk_size / self.BLK_SIZE)
        
        # libdvid returns substack namedtuples as (size, z, y, x), but we want (x,y,z)
        substacks = map(lambda s: (s.x, s.y, s.z), substacks)
      
        # grab roi blocks (should use libdvid but there are problems handling 206 status)
        import requests
        addr = self.dvid_server + "/api/node/" + str(self.uuid) + "/" + str(roi) + "/roi"
        if not self.dvid_server.startswith("http://"):
            addr = "http://" + addr
        data = requests.get(addr)
        roi_blocks = data.json()

        # create roi array giving unique substack ids
        for substack_id, substack in enumerate(substacks):
            # use substack id as key
            subvolumes.append((substack_id, Subvolume(substack_id, substack, chunk_size, border))) 
  
        for (sid, subvol) in subvolumes:
            # find interesecting ROI lines for substack+border and store for substack
            subvol.add_intersecting_blocks(roi_blocks)



        # grab all neighbors for each substack
        if find_neighbors:
            # inefficient search for all boundaries
            for i in range(0, len(subvolumes)-1):
                for j in range(i+1, len(subvolumes)):
                    subvolumes[i][1].recordborder(subvolumes[j][1])

        # Potential TODO: custom partitioner for grouping close regions
        return self.sc.parallelize(subvolumes, len(subvolumes))

    def checkpointRDD(self, rdd, checkpoint_loc, enable_rollback):
        """Defines functionality for checkpointing an RDD.

        Future implementation should be a member function of RDD.

        """
        import os
        from pyspark import StorageLevel
        from pyspark.storagelevel import StorageLevel

        if not enable_rollback or not os.path.exists(checkpoint_loc): 
            if os.path.exists(checkpoint_loc):
                import shutil
                shutil.rmtree(checkpoint_loc)
            rdd.persist(StorageLevel.MEMORY_AND_DISK_SER)
            rdd.saveAsPickleFile(checkpoint_loc)
            return rdd
        else:
            newrdd = self.sc.pickleFile(checkpoint_loc)
            return newrdd


    def map_grayscale8(self, distsubvolumes, gray_name):
        """Creates RDD of grayscale data from subvolumes.

        Note: Since EM grayscale is not highly compressible
        lz4 is not called.

        Args:
            distsubvolumes (RDD): (subvolume id, subvolume)
            gray_name (str): name of grayscale instance

        Returns:
            RDD of grayscale data (partitioner perserved)
    
        """
        
        # copy local context to minimize sent data
        server = self.dvid_server
        uuid = self.uuid

        # only grab value
        def mapper(subvolume):
            # extract grayscale x
            # get sizes of subvolume
            size1 = subvolume.roi.x2+2*subvolume.border-subvolume.roi.x1
            size2 = subvolume.roi.y2+2*subvolume.border-subvolume.roi.y1
            size3 = subvolume.roi.z2+2*subvolume.border-subvolume.roi.z1

            #logger = logging.getLogger(__name__)
            #logger.warn("FIXME: As a temporary hack, this introduces a pause before accessing grayscale, to offset accesses to dvid")
            #import time
            #import random
            #time.sleep( random.randint(0,512) )

            # retrieve data from roi start position considering border
            @auto_retry(3, pause_between_tries=60.0, logging_name=__name__)
            def get_gray():
                node_service = retrieve_node_service(server, uuid)
                
                # Note: libdvid uses zyx order for python functions
                return node_service.get_gray3D( str(gray_name),
                                                (size3,size2,size1),
                                                (subvolume.roi.z1-subvolume.border, subvolume.roi.y1-subvolume.border, subvolume.roi.x1-subvolume.border) )
            gray_volume = get_gray()

            return (subvolume, gray_volume)

        return distsubvolumes.mapValues(mapper)

    def map_labels64(self, distrois, label_name, border, roiname=""):
        """Creates RDD of labelblk data from subvolumes.

        Note: Numpy arrays are compressed which leads to some savings.
        
        Args:
            distrois (RDD): (subvolume id, subvolume)
            label_name (str): name of labelblk instance
            border (int): size of substack border
            roiname (str): name of the roi (to restrict fetch precisely)
            compress (bool): true return compressed numpy

        Returns:
            RDD of compressed lableblk data (partitioner perserved)
            (subvolume, label_comp)
    
        """

        # copy local context to minimize sent data
        server = self.dvid_server
        uuid = self.uuid

        def mapper(subvolume):
            # get sizes of roi
            size1 = subvolume.roi[3]+2*border-subvolume.roi[0]
            size2 = subvolume.roi[4]+2*border-subvolume.roi[1]
            size3 = subvolume.roi[5]+2*border-subvolume.roi[2]

            @auto_retry(3, pause_between_tries=60.0, logging_name=__name__)
            def get_labels():
                # extract labels 64
                node_service = retrieve_node_service(server, uuid)
                # retrieve data from roi start position considering border
                # Note: libdvid uses zyx order for python functions
                data = node_service.get_labels3D( str(label_name),
                                                  (size3,size2,size1),
                                                  (subvolume.roi[2]-border, subvolume.roi[1]-border, subvolume.roi[0]-border),
                                                  compress=True )
                
                # mask ROI
                if roiname != "":
                    mask_roi(data, subvolume, border=border)        

                return data


        return distrois.mapValues(mapper)

    
    def map_labels64_pair(self, distrois, label_name, dvidserver2, uuid2, label_name2, roiname=""):
        """Creates RDD of two subvolumes (same ROI but different datasets)

        This functionality is used to compare two subvolumes.

        Note: Numpy arrays are compressed which leads to some savings.
        
        Args:
            distrois (RDD): (subvolume id, subvolume)
            label_name (str): name of labelblk instance
            dvidserver2 (str): name of dvid server for label_name2
            uuid2 (str): dataset uuid version for label_name2
            label_name2 (str): name of labelblk instance
            roiname (str): name of the roi (to restrict fetch precisely)

        Returns:
            RDD of compressed lableblk, labelblk data (partitioner perserved).
            (subvolume, label1_comp, label2_comp)

        """

        # copy local context to minimize sent data
        server = self.dvid_server
        server2 = dvidserver2
        uuid = self.uuid

        def mapper(subvolume):
            # get sizes of roi
            size1 = subvolume.roi[3]-subvolume.roi[0]
            size2 = subvolume.roi[4]-subvolume.roi[1]
            size3 = subvolume.roi[5]-subvolume.roi[2]

            @auto_retry(3, pause_between_tries=60.0, logging_name=__name__)
            def get_labels():
                # extract labels 64
                node_service = retrieve_node_service(server, uuid)
                # retrieve data from roi start position
                # Note: libdvid uses zyx order for python functions
                data = node_service.get_labels3D( str(label_name),
                                                  (size3,size2,size1),
                                                  (subvolume.roi[2], subvolume.roi[1], subvolume.roi[0]))
                
                # mask ROI
                if roiname != "":
                    mask_roi(data, subvolume)        

                return data
            label_volume = get_labels()

            @auto_retry(3, pause_between_tries=60.0, logging_name=__name__)
            def get_labels2():
                # fetch second label volume
                node_service2 = retrieve_node_service(server2, uuid2)
                # retrieve data from roi start position
                # Note: libdvid uses zyx order for python functions
                return node_service2.get_labels3D( str(label_name2),
                                                   (size3,size2,size1),
                                                   (subvolume.roi[2], subvolume.roi[1], subvolume.roi[0]))
            label_volume2 = get_labels2()

            # zero out label_volume2 where GT is 0'd out !!
            label_volume2[label_volume==0] = 0

            return (subvolume, label_volume, label_volume2)

        return distrois.mapValues(mapper)


    # foreach will write graph elements to DVID storage
    def foreachPartition_graph_elements(self, elements, graph_name):
        """Write graph nodes or edges to DVID labelgraph.

        Write nodes and edges of the specified size and weight
        to DVID.

        This operation works over a partition which could
        have many Sparks tasks.  The reason for this is to
        consolidate the number of small requests made to DVID.

        Note: edges and vertices are both encoded in the same
        datastructure (node1, node2).  node2=-1 for vertices.

        Args:
            elements (RDD): graph elements ((node1, node2), size)
            graph_name (str): name of DVID labelgraph (already created)

        """
        
        # copy local context to minimize sent data
        server = self.dvid_server
        uuid = self.uuid
        
        def writer(element_pairs):
            from libdvid import Vertex, Edge
            
            # write graph information
            node_service = retrieve_node_service(server, uuid)
       
            if element_pairs is None:
                return

            vertices = []
            edges = []
            for element_pair in element_pairs:
                edge, weight = element_pair
                v1, v2 = edge

                if v2 == -1:
                    vertices.append(Vertex(v1, weight))
                else:
                    edges.append(Edge(v1, v2, weight))
    
            if len(vertices) > 0:
                node_service.update_vertices(str(graph_name), vertices) 
            
            if len(edges) > 0:
                node_service.update_edges(str(graph_name), edges) 
            
            return []

        elements.foreachPartition(writer)

    # (key, (ROI, segmentation compressed+border))
    # => segmentation output in DVID
    def foreach_write_labels3d(self, label_name, seg_chunks, roi_name=None, mutateseg="auto"):
        """Writes RDD of label volumes to DVID.

        For each subvolume ID, this function writes the subvolume
        ROI not including the border.  The data is actually sent
        compressed to minimize network latency.

        Args:
            label_name (str): name of already created DVID labelblk
            seg_chunks (RDD): (key, (subvolume, label volume)
            roi_name (str): restrict write to within this ROI
            mutateseg (str): overwrite previous seg ("auto", "yes", "no"
            "auto" will check existence of labels beforehand)

        """

        # copy local context to minimize sent data
        server = self.dvid_server
        uuid = self.uuid

        # create labels type
        node_service = retrieve_node_service(server, uuid)
        success = node_service.create_labelblk(str(label_name))

        # check whether seg should be mutated
        mutate=False
        if (not success and mutateseg == "auto") or mutateseg == "yes":
            mutate=True

        def writer(subvolume_seg):
            import numpy
            # write segmentation
            
            (key, (subvolume, seg)) = subvolume_seg
            # get sizes of subvolume 
            size1 = subvolume.roi.x2-subvolume.roi.x1
            size2 = subvolume.roi.y2-subvolume.roi.y1
            size3 = subvolume.roi.z2-subvolume.roi.z1

            border = subvolume.border

            # extract seg ignoring borders (z,y,x)
            seg = seg[border:size3+border, border:size2+border, border:size1+border]

            # copy the slice to make contiguous before sending 
            seg = numpy.copy(seg, order='C')

            @auto_retry(3, pause_between_tries=600.0, logging_name= __name__)
            def put_labels():
                node_service = retrieve_node_service(server, uuid)
                # send data from roi start position
                # Note: libdvid uses zyx order for python functions
                if roi_name is None:
                    node_service.put_labels3D( str(label_name),
                                               seg,
                                               (subvolume.roi.z1, subvolume.roi.y1, subvolume.roi.x1),
                                               compress=True,
                                               mutate=mutate )
                else: 
                    node_service.put_labels3D( str(label_name),
                                               seg,
                                               (subvolume.roi.z1, subvolume.roi.y1, subvolume.roi.x1),
                                               compress=True,
                                               roi=str(roi_name),
                                               mutate=mutate )
            put_labels()

        return seg_chunks.foreach(writer)

