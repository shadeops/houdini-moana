# ⚠️ This project is Archived.

This was meant to be brute force way of converting to Disney Moana dataset into Houdini bgeo format for investigations. However this approach is no longer needed and you should instead use the USD version provided as this imports much more efficiently into Houdini's Solaris.

# houdini-moana
Python script for converting the [Moana Data-set](https://www.disneyanimation.com/data-sets/?drawer=/resources/moana-island-scene/) to Houdini

The script will convert all the data-set into a Houdini Mantra renderable scene. The output is a bunch of bgeo files representing cleaned up versions of the objs as well as a hip file containing the geometry, lights, cameras, and materials. None of the data-set is modify, only new files/directories are created.

## Usage & Installation
This repo contains a build_scene.py and a otls folder. These two items are expected to live inside the data-set island directory. (ie the same folder as the json, obj, textures, etc directories.)

At this time only the base data-set is required. The pbrt data as well as the animated ocean are not required.

Once these placed there, the script can be run with 
`hython build_scene.py`

This will create a bgeos and rats directory in addition to a island.hip.

Rerunning the build_scene.py script will recreate any missing data, but it will not overwrite existing data. The total conversion process requires about 36GB of memory to process. (This is mainly due to the very large json files.)

To render the full data-set at once with Mantra will take approximately 130GB of memory.

## Regarding Geometry
Within each object each element the objs are converted to bgeos. Within each bgeo, duplicated geometry is packed to preserve instancing. Materials are bound within the packed geometry using the prim shop_materialpath prim attribute.

## Data-set changes
* Some of the obj files have duplicated faces sitting on top of each. The script will detect these and remove them if they exist.
* The osOcean surface is rendered as polygons and not subds. (This of course can be changed within the hip file.)
* The Houdini principled shader isn't a direct match to the Disney BSDF so further adjustments and tweaks are most likely needed.
* The linear sky exr is used to render *and* displayed, where as in the described scene a different sky exr is used to display vs illuminate.

## Other Notes
* This conversion tool has only been tested with the Moana Data-set v1.1 and Houdini 17.x

