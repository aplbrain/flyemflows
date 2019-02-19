from .. import ( Hdf5ServiceSchema, ZarrServiceSchema,
                 DvidGrayscaleServiceSchema, SliceFilesServiceSchema, N5ServiceSchema,
                 DvidSegmentationServiceSchema, BrainMapsServiceSchema,
                 NewAxisOrderSchema, RescaleLevelSchema, LabelMapSchema )

from .geometry import GeometrySchema

# TERMINOLOGY:
#
# - Service: Arbitrary source (or sink) of voxels, with no defined bounding box or access pattern
# - Geometry: Bounding box, access pattern, scale
# - Volume: Combines both Service with a Geometry (and possibly other properties, such as a labelmap)


#
# Generic grayscale volume (one service + geometry)
#
GrayscaleVolumeSchema = \
{
    "description": "Describes a grayscale volume (service and geometry).\n"
                   "Only one of these service definitions (e.g. 'hdf5') should be\n"
                   "listed in the config, and unused ones should be omitted.\n"
                   "Regardless of service type, the 'geometry' section MUST be listed\n"
                   "explicitly, even if it is empty.\n",
    "type": "object",
    "default": {},
    "oneOf": [
        { "properties": { "brainmaps": BrainMapsServiceSchema } },
        { "properties": { "hdf5": Hdf5ServiceSchema } },
        { "properties": { "dvid": DvidGrayscaleServiceSchema } },
        { "properties": { "slice-files": SliceFilesServiceSchema } },
        { "properties": { "n5": N5ServiceSchema } },
        { "properties": { "zarr": ZarrServiceSchema } }
    ],
    "properties": {
        "geometry": GeometrySchema,
        "transpose-axes": NewAxisOrderSchema,
        "rescale-level": RescaleLevelSchema
    }
}


#
# Generic segmentation volume (one service + geometry)
#
SegmentationVolumeSchema = \
{
    "description": "Describes a segmentation volume source (or destination), \n"
                   "extents, and preferred access pattern",
    "type": "object",
    "required": ["geometry"],
    "default": {},
    "oneOf": [
        { "properties": { "zarr": ZarrServiceSchema }, "required": ["zarr"] },
        { "properties": { "hdf5": Hdf5ServiceSchema }, "required": ["hdf5"] },
        { "properties": { "dvid": DvidSegmentationServiceSchema }, "required": ["dvid"] },
        { "properties": { "brainmaps": BrainMapsServiceSchema }, "required": ["brainmaps"] }
    ],
    "properties": {
        "geometry": GeometrySchema,
        "transpose-axes": NewAxisOrderSchema,
        "rescale-level": RescaleLevelSchema,
        "apply-labelmap": LabelMapSchema
    }
}


#
# List of segmentation volumes
#
SegmentationVolumeListSchema = \
{
    "description": "A list of segmentation volume sources (or destinations).",
    "type": "array",
    "items": SegmentationVolumeSchema,
    "minItems": 1,
    "default": [{}] # One item by default (will be filled in during yaml dump)
}
