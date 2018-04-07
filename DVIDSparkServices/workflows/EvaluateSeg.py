"""Defines workflow for extracting stats to compare two segmentations."""

from __future__ import print_function, absolute_import
from DVIDSparkServices.workflow.dvidworkflow import DVIDWorkflow
from DVIDSparkServices.sparkdvid.sparkdvid import retrieve_node_service 
from DVIDSparkServices.util import NumpyConvertingEncoder
from libdvid import ConnectionMethod
import numpy

class EvaluateSeg(DVIDWorkflow):
    # schema for evaluating segmentation
    Schema = \
{ "$schema": "http://json-schema.org/schema#",
  "title": "Tool to Create DVID blocks from image slices",
  "type": "object",
  "properties": {
    "dvid-info": {
      "description": "Contains DVID information for ground truth volume",
      "type": "object",
      "properties": {
        "dvid-server": { 
          "description": "location of DVID server",
          "type": "string", 
          "minLength": 1,
          "property": "dvid-server"
        },
        "uuid": {
          "description": "version node to store segmentation",
          "type": "string",
          "minLength": 1
        },
        "label-name": { 
          "description": "DVID data instance pointing to label blocks",
          "type": "string" 
        },
        "roi": { 
          "description": "name of DVID ROI for given label-name",
          "type": "string" 
        },
        "point-lists": {
          "description": "List of keyvalue DVID locations (or location of annotation datatype) that contains seletive ponts (e.g., annotations/synapses)",
          "type": "array",
          "items": { "type": "string", "minLength": 2 },
          "minItems": 0,
          "uniqueItems": True
        },
        "stats-location": {
          "description": "Location of final results (JSON file) stored on DVID.  If there are already results present at that name, a unique number will be appended to the file name",
          "type": "string"
        }
      },
      "required" : ["dvid-server", "uuid", "label-name", "roi", "point-lists", "stats-location"]
    },
    "dvid-info-comp": {
      "description": "Contains DVID information for comparison/test volume",
      "type": "object",
      "properties": {
        "dvid-server": { 
          "description": "location of DVID server",
          "type": "string", 
          "minLength": 1,
          "property": "dvid-server"
        },
        "uuid": {
              "description": "version node to store segmentation",
              "type": "string",
              "minLength": 1
        },
        "label-name": { 
          "description": "DVID data instance pointing to label blocks",
          "type": "string" 
        }
      },
      "required" : ["dvid-server", "uuid", "label-name"]
    },
    "options": {
      "type": "object",
      "properties": {
        "plugins": {
          "description": "Custom configuration for each metric plugin.",
          "type": "array",
          "items": {
            "type": "object",
            "properties": {
              "name": {
                "description": "metric plugin name",
                "type": "string",
                "default": ""
              },
              "parameters": {
                "description": "custom parameters for metric.",
                "type" : "object",
                "default" : {},
                "additionalProperties": True
              }
            }
          },
          "minItems": 0,
          "uniqueItems": True,
          "default": [{"name": "rand"}, {"name": "vi"}, {"name": "count"}, {"name": "connectivity"}, {"name": "edit"}]
        },
        "body-threshold": {
          "description": "Filter GT bodies below this threshold for aggregate stats",
          "type": "integer",
          "default": 1000
        },
        "point-threshold": {
          "description": "Filter GT bodies below this point threshold for aggregate stats",
          "type": "integer",
          "default": 10
        },
        "num-displaybodies": {
          "description": "Maximum bodies to report for metric body stats",
          "type": "integer",
          "default": 100
        },
        "subvolume-threshold": {
          "description": "Filter to decide which percent of subvolume must have GT data (if sparse mode, indicates minimum number of bodies for a subvolume)",
          "type": "integer",
          "default": 0
        },
        "chunk-size": {
          "description": "size of subvolumes to be processed",
          "type": "integer",
          "default": 256
        },
        "boundary-size": {
          "description": "radial width of boundary for GT to mask out",
          "type": "integer",
          "default": 2
        },
        "important-bodies": {
          "description": "filter metrics based on this list of GT bodies",
          "type": "array",
          "items": {"type": "number"},
          "minItems": 0,
          "uniqueItems": True,
          "default": []
        },
        "no-gt": {
          "description": "Set flag if neither volume is ground truth.",
          "type": "boolean",
          "default": False 
        },
        "enable-sparse": {
          "description": "Set flag ground truth is restricted to the set of specified bodies.",
          "type": "boolean",
          "default": False 
        },
        "run-cc": {
          "description": "Run connected components on segmentations (assume neurons will be connected component in volume).",
          "type": "boolean",
          "default": True 
        },
        "disable-subvolumes": {
          "description": "disables subvolume stats.  This could be useful if working with smaller volumes where such information is unnecessary.  It could also save memory / computation.",
          "type": "boolean",
          "default": False 
        },
        "user-name": {
          "description": "Name of person submitting the job",
          "type": "string"
        }
      },
      "required" : ["body-threshold", "point-threshold", "chunk-size", "boundary-size", "user-name"]
    }
  }
}


    @classmethod
    def schema(cls):
        return EvaluateSeg.Schema

    # TODO:!! GT cut-off/list should probably be added
    # directly at the GT itself to indicate completeness.  Could also
    # examine a body list of complete neurons

    # TODO: !! Take body list to better pre-filter synapses for summary
    # view -- but summary numbers can mostly be computed from scratch

    chunksize = 256
    
    # 'seg-metrics' at the specified UUID will contain the evaluation results
    writelocation = "seg-metrics"

    def __init__(self, config_filename):
        super(EvaluateSeg, self).__init__(config_filename, self.schema(), "Evaluate Segmentation")
   
    def execute(self):
        # imports here so that schema can be retrieved without installation
        from DVIDSparkServices.reconutils.metrics import Evaluate
        from pyspark import SparkContext
        from pyspark import StorageLevel
        import time
        import datetime
        import json

        node_service = retrieve_node_service(self.config_data["dvid-info"]["dvid-server"],
                self.config_data["dvid-info"]["uuid"], self.resource_server, self.resource_port)

        if "chunk-size" in self.config_data["options"]:
            self.chunksize = self.config_data["options"]["chunk-size"]

        #  grab ROI (no overlap and no neighbor checking)
        distrois = self.sparkdvid_context.parallelize_roi(self.config_data["dvid-info"]["roi"],
                self.chunksize, border=1)
        def setBorderHack(subvolume):
            subvolume.border = 0
            return subvolume
        distrois = distrois.mapValues(setBorderHack)
        
        # check for self mode
        selfcompare = False
        dvidserver2 = ""
        dviduuid2 = ""
        dvidlname2 = ""
        if "dvid-info-comp" in self.config_data:
            dvidserver2 = self.config_data["dvid-info-comp"]["dvid-server"]
            dviduuid2 = self.config_data["dvid-info-comp"]["uuid"]
            dvidlname2 = self.config_data["dvid-info-comp"]["label-name"]

        # map ROI to two label volumes (0 overlap)
        # this will be used for all volume and point overlaps
        # (preserves partitioner)
        # (key, (subvolume, seggt, seg2)
        
        # creates a dummy volume if no second server is available
        lpairs = self.sparkdvid_context.map_labels64_pair(
                distrois, self.config_data["dvid-info"]["label-name"],
                dvidserver2, dviduuid2, dvidlname2,
                self.config_data["dvid-info"]["roi"])


        # TODO add downsmample
        # ?! add downsample mode (only if labelarray or map)
        # ?! modify substacks info to be donwsampled
        # ?! pass lower rez fetch to seg fetch
        # ?! modify substack back to original

        # filter bodies if there is a body list from GT
        important_bodies = self.config_data["options"]["important-bodies"]

        if self.config_data["options"]["enable-sparse"]:
            # if sparse mode is enable there should be a body list
            assert (len(important_bodies) > 0)
        else:
            # should only filter bodies for non-sparse mode
            # if the bodies densely cover the volume
            def filter_bodies(label_pairs):
                from DVIDSparkServices.sparkdvid.CompressedNumpyArray import CompressedNumpyArray
                import numpy

                subvolume, labelgtc, label2c = label_pairs

                # extract numpy arrays
                labelgt = labelgtc.deserialize()
                
                # filter bodies from gt
                bodylist = numpy.unique(labelgt)
                intersecting_bodies = set(bodylist).intersection(set(important_bodies))
                mask = numpy.zeros(labelgt.shape)
                for body in intersecting_bodies:
                    mask[labelgt==body] = 1
                labelgt[mask==0] = 0

                # compress results
                return (subvolume, CompressedNumpyArray(labelgt), label2c)
           
            if len(important_bodies) > 0:
                lpairs = lpairs.mapValues(filter_bodies)

        def _split_disjoint_labels(label_pairs):
            """Helper function: map subvolumes so disconnected bodies are different labels.

            Function preserves partitioner.

            Args:
                label_pairs (rdd): RDD is of (subvolume id, data)
       
            Returns:
                Original RDD including mappings for gt and the test seg.
        
            """
            from DVIDSparkServices.reconutils.morpho import split_disconnected_bodies
            
            subvolume, labelgt, label2 = label_pairs

            # split bodies up
            labelgt_split, labelgt_map = split_disconnected_bodies(labelgt)
            label2_split, label2_map = split_disconnected_bodies(label2)
            
            # compress results
            return (subvolume, labelgt_map, label2_map, labelgt_split, label2_split)

        
        # split bodies that are merged outside of the subvolume
        # (preserves partitioner)
        # => (key, (subvolume, seggt-split, seg2-split, seggt-map, seg2-map))
        lpairs_split = lpairs.mapValues(_split_disjoint_labels)

        if self.config_data["options"]["run-cc"]: 
            # save current segmentation state
            lpairs_split.persist()

            # apply connected components
            def _extractfaces(label_pairs):
                """Extracts 6 sides from each cube.
                """
                
                key, (subvolume, gtmap, segmap, gtvol, segvol) = label_pairs

                # extract unique bodies not remapped
                allgt = set(numpy.unique(gtvol))
                allseg = set(numpy.unique(segvol))

                gtmapbodies = set()
                for key2, body in gtmap.items():
                    gtmapbodies.add(key2)
                segmapbodies = set()
                for key2, body in segmap.items():
                    segmapbodies.add(key2)
                allgt = allgt.difference(gtmapbodies)
                if 0 in allgt:
                    allgt.remove(0)
                allseg = allseg.difference(segmapbodies)
                if 0 in allseg:
                    allseg.remove(0)

                zmax,ymax,xmax = gtvol.shape
                start = (subvolume.box.z1, subvolume.box.y1, subvolume.box.x1)

                mappedfaces = []

                # grab 6 faces for gt
                slicex0 = gtvol[:,:,0]
                slicexmax = gtvol[:,:,xmax-1]

                slicey0 = gtvol[:,0,:]
                sliceymax = gtvol[:,ymax-1,:]
                
                slicez0 = gtvol[0,:,:]
                slicezmax = gtvol[zmax-1,:,:]

                mappedfaces.append(( (start, (start[0]+zmax, start[1]+ymax, start[2]+1), True), 
                                     [(slicex0, gtmap, key, True, allgt)] ))
                mappedfaces.append(( ((start[0], start[1], start[2]+xmax),
                                      (start[0]+zmax, start[1]+ymax, start[2]+xmax+1), True), 
                                     [(slicexmax, gtmap, key, False, set())] ))
                
                mappedfaces.append(( (start, (start[0]+zmax, start[1]+1, start[2]+xmax), True), 
                                     [(slicey0, gtmap, key, False, set())] ))
                mappedfaces.append(( ((start[0], start[1]+ymax, start[2]),
                                      (start[0]+zmax, start[1]+ymax+1, start[2]+xmax), True), 
                                     [(sliceymax, gtmap, key, False, set())] ))
                
                mappedfaces.append(( (start, (start[0]+1, start[1]+ymax, start[2]+xmax), True), 
                                     [(slicez0, gtmap, key, False, set())] ))
                mappedfaces.append(( ((start[0]+zmax, start[1], start[2]),
                                      (start[0]+zmax+1, start[1]+ymax, start[2]+xmax), True), 
                                     [(slicezmax, gtmap, key, False, set())] ))

                # grab 6 faces for seg
                segslicex0 = segvol[:,:,0]
                segslicexmax = segvol[:,:,xmax-1]

                segslicey0 = segvol[:,0,:]
                segsliceymax = segvol[:,ymax-1,:]
                
                segslicez0 = segvol[0,:,:]
                segslicezmax = segvol[zmax-1,:,:]

                mappedfaces.append(( (start, (start[0]+zmax, start[1]+ymax, start[2]+1), False), 
                                     [(segslicex0, segmap, key, True, allseg)] ))
                mappedfaces.append(( ((start[0], start[1], start[2]+xmax),
                                      (start[0]+zmax, start[1]+ymax, start[2]+xmax+1), False), 
                                     [(segslicexmax, segmap, key, False, set())] ))
                
                mappedfaces.append(( (start, (start[0]+zmax, start[1]+1, start[2]+xmax), False), 
                                     [(segslicey0, segmap, key, False, set())] ))
                mappedfaces.append(( ((start[0], start[1]+ymax, start[2]),
                                      (start[0]+zmax, start[1]+ymax+1, start[2]+xmax), False), 
                                     [(segsliceymax, segmap, key, False, set())] ))
                
                mappedfaces.append(( (start, (start[0]+1, start[1]+ymax, start[2]+xmax), False), 
                                     [(segslicez0, segmap, key, False, set())] ))
                mappedfaces.append(( ((start[0]+zmax, start[1], start[2]),
                                      (start[0]+zmax+1, start[1]+ymax, start[2]+xmax), False), 
                                     [(segslicezmax, segmap, key, False, set())] ))
        
                return mappedfaces

            # assume there could be only one possible match
            def _reducematches(faces1, faces2):
                faces1.extend(faces2)
                return faces1

            def _extractmatches(keyfaces):
                """Finds matching segments that have the same body id.
                """
                key, faces = keyfaces
            
                # no match found
                if len(faces) == 1:
                    start, end, isgt = key
                    seg1, segmap, sid, hack1, segbodies = faces[0]
                    bodymatches = []
                    if hack1:
                        for label, body in segmap.items():
                            bodymatches.append(((body, isgt), [(label, sid, True)]))
                        for body in segbodies:
                            bodymatches.append(((body, isgt), [(body, sid, True)]))

                    return bodymatches
                assert(len(faces) == 2)

                start, end, isgt = key
                seg1, segmap, sid, hack1, segbodies = faces[0]
                seg2, segmap2, sid2, hack2, segbodies2 = faces[1]

                seg1 = seg1.flatten()
                seg2 = seg2.flatten()
               
                seg1seg2 = numpy.column_stack((seg1, seg2))
                unique_pairs = numpy.unique(seg1seg2, axis=0)

                bodymatches = []

                for val in unique_pairs:
                    if val[0] == 0 or val[1] == 0:
                        continue
                    
                    mapped1 = val[0]
                    if mapped1 in segmap:
                        mapped1 = segmap[mapped1]
                    mapped2 = val[1]
                    if mapped2 in segmap2:
                        mapped2 = segmap2[mapped2]

                    if mapped1 == mapped2:
                        bodymatches.append(((mapped1, isgt), [((val[0], sid), (val[1], sid2))]))


                # hack: send all bodies that have new labels
                # assume 1) disjoint bodies will always include implicit identity mapping
                # and 2) each subvolume will be represented at least 6 times
                if hack1:
                    for label, body in segmap.items():
                        bodymatches.append(((body, isgt), [(label, sid, True)]))
                    for body in segbodies:
                        bodymatches.append(((body, isgt), [(body, sid, True)]))
                    
                if hack2:
                    for label, body in segmap2.items():
                        bodymatches.append(((body, isgt), [(label, sid2, True)]))
                    for body in segbodies2:
                        bodymatches.append(((body, isgt), [(body, sid2, True)]))

                return bodymatches

            def _reduce_bodies(bodies1, bodies2):
                """Group all bodies maps together.
                """
                bodies1.extend(bodies2)
                return bodies1

            flatmatches = lpairs_split.flatMap(_extractfaces).reduceByKey(_reducematches).flatMap(_extractmatches)
            matches = flatmatches.reduceByKey(_reduce_bodies)
            
            
            # should be small enough that the list can be global
            def _find_disjoint_bodies(matches):
                """Extract bodies that should be split into more than one piece.
                """
                (bodyid, isgt), matchlist = matches
                
                merges = {} 
                mergeset = {} 

                for match in matchlist:
                    # handle original mapping disjoint ids
                    if len(match) == 3:
                        val = (match[0], match[1])
                        if val not in merges:
                           merges[val] = val
                           mergeset[val] = set([val])
                        continue

                    val, val2 = match
                    if val2 < val:
                        val, val2 = val2, val
                    
                    mappedval = val
                    if mappedval in merges:
                        mappedval = merges[mappedval]
                    else:
                        merges[val] = val 
                    
                    if mappedval not in mergeset:
                        mergeset[mappedval] = set([val])
                    else:
                        mergeset[mappedval].add(val)

                    mappedval2 = val2
                    if mappedval2 in merges:
                        mappedval2 = merges[mappedval2]
                    
                    if mappedval2 not in mergeset:
                        mergeset[mappedval2] = set([val2])
                    else:
                        mergeset[mappedval2].add(val2)

                    # if the mapped value is equal, no need for further processing
                    if mappedval2 == mappedval:
                        continue

                    merges[mappedval2] = mappedval

                    for iterval in mergeset[mappedval2]:
                        merges[iterval] = mappedval

                    mergeset[mappedval] = mergeset[mappedval].union(mergeset[mappedval2])
                    del mergeset[mappedval2]

                if len(mergeset) == 1:
                    return []

                bodygroups = []
                for (dummy, group) in mergeset.items():
                    bodygroups.append(((bodyid, isgt), group))
                return bodygroups

            # grab all remappings (should be too many disjont components presumably)
            bodies_remap = matches.flatMap(_find_disjoint_bodies).collect()

            # choose very large arbitary index for simplicity (but below js 2^53 limit)
            ccbodyindex = 2**51

            # global map of cc bodies to original body (unique across GT and seg)
            cc2body = {}
        
            ccsub2body_gt = {}
            ccsub2body_seg = {}

            # map from substack to temporary body id
            for ((body, isgt), bodyset) in bodies_remap:
                cc2body[ccbodyindex] = body

                for (subval, sid) in bodyset:
                    if isgt:
                        ccsub2body_gt[(subval, sid)] = ccbodyindex
                    else:
                        ccsub2body_seg[(subval, sid)] = ccbodyindex
                ccbodyindex += 1


            # give new ids for subvolumes
            def _insertccmappings(label_pairs):
                (subvolume, labelgt_map, label2_map, labelgt_split, label2_split) = label_pairs

                for ((subval, sid), newbodyid) in ccsub2body_gt.items():
                    if sid == subvolume.sv_index:
                        labelgt_map[subval] = newbodyid
                
                for ((subval, sid), newbodyid) in ccsub2body_seg.items():
                    if sid == subvolume.sv_index:
                        label2_map[subval] = newbodyid
                return (subvolume, labelgt_map, label2_map, labelgt_split, label2_split)

            lpairs_split = lpairs_split.mapValues(_insertccmappings)
            




        # evaluation tool (support RAND, VI, per body, graph, and
        # histogram stats over different sets of points)
        evaluator = Evaluate.Evaluate(self.config_data)

        ### VOLUMETRIC ANALYSIS ###

        # TODO: !! Grab number of intersecting disjoint faces
        # (might need +1 border) for split edit distance
        
        # grab volumetric body overlap ignoring boundaries as specified
        # and generate overlap stats for substack (compute local)
        # => (key, (subvolume, stats, seggt-split, seg2-split, seggt-map, seg2-map))
        # (preserve partitioner)
        lpairs_proc = evaluator.calcoverlap(lpairs_split, self.config_data["options"]["boundary-size"])
       
        point_data = {}
        ### POINT ANALYSIS ###
        for point_list_name in self.config_data["dvid-info"]["point-lists"]:
            # grab point list from DVID
            keyvalue = point_list_name.split('/')
            pointname = ""

            if len(keyvalue) == 2:
                # is this too large to broadcast?? -- default lz4 should help quite a bit
                # TODO: send only necessary data to each job through join might help
                point_data[keyvalue[1]] = node_service.get_json(str(keyvalue[0]),
                        str(keyvalue[1]))
                pointname = keyvalue[1]
            elif len(keyvalue) == 1:    
                # assume dvid annotation datatype and always treat as a synapse type
                # TODO: split this up into many small calls so that it scales
                syndata = node_service.custom_request(str(keyvalue[0]) + "/roi/" + str(self.config_data["dvid-info"]["roi"]), "".encode(), ConnectionMethod.GET) 
                synjson = json.loads(syndata)
                synindex = {}
                synspot = 0
                # grab index positions
                for synapse in synjson:
                    synindex[tuple(synapse["Pos"])] = synspot
                    synspot += 1
               
                # load point data
                pointlist = [] 
                for synapse in synjson:
                    pointrel = synapse["Pos"]
                    if synapse["Rels"] is not None:
                        for rel in synapse["Rels"]:
                            if rel["Rel"] == "PreSynTo":
                                # only add relations within ROI
                                if tuple(rel["To"]) in synindex:
                                    index = synindex[tuple(rel["To"])]
                                    pointrel.append(index)
                    pointlist.append(pointrel)
                pointinfo = {"type": "synapse", "sparse": False, "point-list": pointlist}
                point_data[keyvalue[0]] = pointinfo
                pointname = keyvalue[0]
            else:
               raise Exception(str(point_list_name) + "point list key value not properly specified")

            # Generate per substack and global stats for given points.
            # Querying will just be done on the local labels stored.
            # (preserve partitioner)
            lpairs_proc = evaluator.calcoverlap_pts(lpairs_proc, pointname, point_data[pointname])

        # Extract stats by retrieving substacks and stats info and
        # loading into data structures on the driver.
        stats = evaluator.calculate_stats(lpairs_proc)
        
        """
        # map temporary CC body index to original body index for body stats
        # for convenience (not very necessary since
        # CC mappings are also provided)
        for bodystat in stats["bodystats"]:
            delkeys = []
            newbodies = {}

            # rename bodyid -> bodyid-<num> for CC bodies
            for (tbody, val) in bodystat["bodies"].items():
                if tbody in cc2body:
                    delkeys.append(tbody)
                    iter1 = 0
                    while (str(tbody) + "-" + str(iter1)) in newbodies:
                        iter1 += 1
                    newbodies[str(tbody) + "-" + str(iter1)] = val
            for key in delkeys:
                del bodystat["bodies"][key]
            for (body, val) in newbodies.items():
                bodystat["bodies"][body] = val
        """

        # dump CC mappings for use in debugging
        if self.config_data["options"]["run-cc"]: 
            stats["connected-components"] = cc2body

        # none or false
        debug = False
        if "debug" in self.config_data:
            debug = self.config_data["debug"]

        if debug:
            print("DEBUG:", json.dumps(stats, cls=NumpyConvertingEncoder))

        # TODO: !! maybe generate a summary view from stats, write that back
        # with simplify output, dump the more complicated file to keyvalue as well

        # write stats and config back to DVID with time stamp
        # (@ name + user name + time stamp)
        # client should use '--' delimeter to parse name
        stats["time-analyzed"] = \
            datetime.datetime.fromtimestamp(time.time()).strftime('%Y-%m-%d %H:%M:%S')
        stats["config-file"] = self.config_data
        current_time = int(time.time())

        username = str(self.config_data["options"]["user-name"])
        username = "__".join(username.split('.'))
        
        location = str(self.config_data["dvid-info"]["stats-location"])
        location = "__".join(location.split('.'))
    
        fileloc = str(location + "--" + username + "--" + str(current_time))

        node_service.create_keyvalue(self.writelocation)
        node_service.put(self.writelocation, fileloc, json.dumps(stats, cls=NumpyConvertingEncoder).encode('utf-8'))

