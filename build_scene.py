import os
import re
import sys
import glob
import math
import hashlib
import logging
import fnmatch
import tempfile
import subprocess
import contextlib
import collections

try:
    import simplejson as json
except ImportError:
    import json

import hou

logging.basicConfig(format='%(asctime)s %(message)s',
                    datefmt='%H:%M:%S',
                    level=logging.INFO)

#class Element(object):
#    name : str
#    instancedPrimitiveJsonFiles : dict of PrimitiveJsonFiles
#    geomObjFile : str : path to obj
#    transformMatrix : list (Matrix4)
#    matFile : str (path to json)
#    instancedCopies : dict of InstancedCopys
#    animated
#    variants
#       named list of overrides of instancedPrimitiveJsonFiles

#class InstancedCopy
#   name : str
#   transformMatrix : list (Matrix4)
#   geomObjFile: if None use Element
#   instancedPrimitiveJsonFiles : dict of InstancedPrims or None

#class InstancedPrims(object):
#   type : str
#   jsonFile : str

#class ArchiveJson(InstancedPrims)
#   archives : list

#class CurveJson(InstancedPrims)
#   widthTip : float
#   jsonFile: str
#   widthRoot: float
#   degrees: int
#   faceCamera: bool
#       Note Uniform b-splines, duplicate first/last control points

#class ElementJson(InstancedPrims)
#   archives: list
#   element: str (name of Element)
#   variants: list (names of variants in Element)

# Unfortuntely gwavefront doesn't accept obj data as stdin
# so we'll have to save it to a file then read it back in.
# Lame.

class ObjReader(object):
    """Class for parsing through an obj file "cleaning" it up as we go."""
    pat = re.compile('\s+(\d+)')

    def __init__(self, obj_file):
        if not os.path.exists(obj_file):
            raise OSError('Can not access %s' % obj_file)
        self.obj = obj_file
        self.groups = collections.defaultdict(set)
        self.prim_names = [] #collections.deque()
        self.f = None
        self._current_group = 'default'

        # This is needed to detect duplication
        self.current_hash = None
        self.vtx_hashes = set()
        self.vtx_sections = 0
        self.dupe_vtx_sections = 0

    def _reset(self):
        self.groups.clear()
        self.vtx_hashes.clear()
        self.current_hash = None
        self.vtx_sections = 0
        self.dupe_vtx_sections = 0
        self._current_group = 'default'

    def __enter__(self):
        self.f = open(self.obj, 'r')
        self._reset()
        return self

    def __exit__(self, *args):
        self.f.close()
        self.f = None
        self._reset()
        return self

    def __iter__(self):
        return self

    def __next__(self):
        # keep iterating until we can output
        # a line
        line = None
        while line is None:
            line = self.f.next()

            # This logic is for tracking whether there is "double" geometry
            # within the obj file which is the case for at least v1.1 of the
            # data set
            if line.startswith('v '):
                if self.current_hash is None:
                    self.current_hash = hashlib.md5()
                    self.vtx_sections += 1
                self.current_hash.update(line)
            else:
                if self.current_hash is not None:
                    hash_val = self.current_hash.hexdigest()
                    if hash_val in self.vtx_hashes:
                        self.dupe_vtx_sections += 1
                    self.vtx_hashes.add(hash_val)
                    self.current_hash = None

            if line.startswith('f '):
                indices = set([int(x) for x in self.pat.findall(line)])
                self.groups[self.current_group].update(indices)
                self.prim_names.append(self.current_group)
            elif line.startswith('g '):
                tokens = line.strip().split()
                # if there are more than 1 groups treat them as groups
                if len(tokens) != 2:
                    return line
                self.current_group = tokens[1]
                # Set the line to None, to fetch another
                line = None
        return line

    # This explicitly checks to see if there are double the number
    # of dupe vtx sections vs vtx sections. If there are its a safe
    # assumption this is a case of doubled up geo. If there are dupes
    # but not exactly half, then it is a cause for further investigation
    def doubled_geo(self):
        if not self.vtx_sections:
            return False
        if self.vtx_sections == self.dupe_vtx_sections * 2:
            return True
        return False

    def next(self):
        return self.__next__()

def build_bgeo(obj, bgeo, element_name):
    """Convert a obj file to a bgeo

    obj (str): File path to obj (input)
    bgeo (str): File path to bgeo (output)
    element_name (str): Name of the element (for material assignments)
    """

    logging.info('Converting %s to %s', obj, bgeo)
    bgeo_dir = os.path.dirname(bgeo)
    if not os.path.isdir(bgeo_dir):
        os.makedirs(bgeo_dir)

    hier = os.path.splitext(obj)[0] + '.hier'
    with open(hier, 'rb') as hier_h:
        hier_data = json.load(hier_h)

    geo = hou.Geometry()
    prim_counter = collections.Counter()

    with make_tempfile() as tmp_obj_h:
        with ObjReader(obj) as f:
            for line in f:
                tmp_obj_h.write(line)
            tmp_obj_h.close()

            geo.loadFromFile(tmp_obj_h.name)

            # If duplication is detected (and verified that there
            # is an even number of them) delete the back half.
            num_prims = len(geo.prims())
            if f.doubled_geo() and num_prims%2==0:
                logging.warning('Duplicate geo detected, cleaning!')
                duped_prims = geo.prims()[num_prims/2:]
                geo.deletePrims(duped_prims)

            # Optional, remove N (normals) as we'll be subdividing
            #N_atr = geo.findPointAttrib('N')
            #if N_atr:
            #    N_atr.destroy()

            name_atr = geo.addAttrib(hou.attribType.Prim, 'name', '',
                                     create_local_variable=False)
            #hier_atr = geo.addAttrib(hou.attribType.Prim, 'hier', '',
            #                         create_local_variable=False)
            facenum_atr = geo.addAttrib(hou.attribType.Prim, 'facenum', 0,
                                        create_local_variable=False)
            for prim in geo.prims():
                prim_name = f.prim_names[prim.number()]
                prim_hier = hier_data.get(prim_name, '')
                prim.setAttribValue(name_atr, prim_name)
                material = prim.attribValue('shop_materialpath')
                material_name = os.path.basename(material)
                element_material = '/mat/%s.%s' % (element_name, material_name)
                prim.setAttribValue('shop_materialpath', element_material)
                #prim.setAttribValue(hier_atr, prim_hier)
                prim.setAttribValue(facenum_atr, prim_counter[prim_name])
                prim_counter[prim_name] += 1
            logging.info('Saving out %s', bgeo)
            geo.saveToFile(bgeo)
        geo.clear()
    return prim_counter

def pack_geo(geo, name=None, xform=None):
    pack_verb = hou.sopNodeTypeCategory().nodeVerb('pack')
    pack_verb.setParms({'createpath': 0,
                        'pivot': 0,
                        'viewportlod': 'box'})

    packed_geo = hou.Geometry()
    pack_verb.execute(packed_geo, [geo])
    name_atr = packed_geo.addAttrib(hou.attribType.Prim, 'name', '',
                                    create_local_variable=False)
    for prim in packed_geo.prims():
        if name is not None:
            prim.setAttribValue(name_atr, name)
        if xform is not None:
            prim.setTransform(xform)
    return packed_geo

class InstancedPrims(object):
    def __init__(self, element, name, json_data):
        logging.info('Building %s for %s', self.__class__.__name__, name)
        self.json_data = json_data
        self.element = element
        self.name = name
        self.type = json_data['type']
        self.json_file = json_data['jsonFile']

    def load_json_file(self):
        logging.info('Loading Prim jsonFile')
        with open(self.json_file, 'r') as f:
            return json.load(f)

    def build_geo(self):
        return hou.Geometry()

class ArchivePrims(InstancedPrims):

    copy_packed_prims = False

    @property
    def archives(self):
        return self.json_data['archives']

    def build_geo(self):
        # Pre build bgeos
        for archive in self.archives:
            bgeo = self.element.obj2bgeo(archive)

        archive_data = self.load_json_file()
        all_geo = hou.Geometry()
        name_atr = all_geo.findPrimAttrib('name')
        if not name_atr:
            name_atr = all_geo.addAttrib(hou.attribType.Prim, 'name', '',
                                         create_local_variable=False)
        for obj in archive_data:
            logging.info('Creating %i Prims for %s', len(archive_data[obj]), obj)
            # Load a packed prim
            bgeo = self.element.obj2bgeo(obj)

            # Copy Packed Prims
            if self.copy_packed_prims:
                bgeo_gdp = hou.Geometry()
                diskprim = bgeo_gdp.createPacked('PackedDisk')
                diskprim.setIntrinsicValue('unexpandedfilename', bgeo)
                # Now we are going to pack it so we can copy it
                packed_geo = pack_geo(bgeo_gdp)
                for name,xform in archive_data[obj].iteritems():
                    logging.debug('built %s', name)
                    for prim in packed_geo.prims():
                        prim.setAttribValue('name', name)
                        prim.setTransform(hou.Matrix4(xform))
                    all_geo.merge(packed_geo)
                packed_geo.clear()
                # We can't clear this as its still being referenced
                # by the all_geo
                # bgeo_gdp.clear()
            else:
                for name,xform in archive_data[obj].iteritems():
                    logging.debug('built %s', name)
                    diskprim = all_geo.createPacked('PackedDisk')
                    diskprim.setIntrinsicValue('unexpandedfilename', bgeo)
                    diskprim.setAttribValue(name_atr, name)
                    diskprim.setTransform(hou.Matrix4(xform))
        return all_geo

class CurvePrims(InstancedPrims):
    @property
    def width_tip(self):
        return self.json_data['widthTip']
    @property
    def width_root(self):
        return self.json_data['widthRoot']
    @property
    def face_camera(self):
        return self.json_data['faceCamera']
    @property
    def order(self):
        return self.json_data['degrees']+1

    def build_geo(self):
        curves_data = self.load_json_file()
        all_geo = hou.Geometry()
        width_atr = all_geo.addAttrib(hou.attribType.Vertex, 'width', 0.0,
                                      create_local_variable=False)
        #face_cam_atr = all_geo.addAttrib(hou.attribType.Prim, 'faceCamera', 0,
        #                                 create_local_variable=False)
        shop_atr = all_geo.addAttrib(hou.attribType.Prim, 'shop_materialpath', '',
                                     create_local_variable=False)
        for curve_pts in curves_data:
            num_pts = len(curve_pts)
            curve = all_geo.createNURBSCurve(num_pts, order=self.order)
            for vtx,pts in zip(curve.vertices(), curve_pts):
                vtx.point().setPosition(pts)
                width = hou.hmath.fit(vtx.number(),
                                      0,
                                      num_pts-1,
                                      self.width_root,
                                      self.width_tip)
                vtx.setAttribValue(width_atr, width)
            #curve.setAttribValue(face_cam_atr, self.face_camera)
            try:
                material = self.element.get_material_assignment(self.name)
                curve.setAttribValue(shop_atr, '/mat/%s.%s' % (self.element.name,
                                                               material))
            except ValueError:
                logging.warning('Missing assignment for %s:%s', self.element.name, self.name)
        packed_geo = pack_geo(all_geo, name=self.name)
        return packed_geo

class ElementPrims(InstancedPrims):

    def build_geo(self):
        element_json = './json/{0}/{0}.json'.format(self.json_data['element'])
        element = Element(element_json)
        element_data = self.load_json_file()
        all_geo = hou.Geometry()
        for variant, elements in element_data.iteritems():
            variant_geo = element.build_element_geo(variant)
            for variant_name, xform in elements.iteritems():
                logging.debug('Copied element %s', variant)
                for prim in variant_geo.prims():
                    prim.setAttribValue('name', variant_name)
                    prim.setTransform(hou.Matrix4(xform))
                all_geo.merge(variant_geo)
            variant_geo.clear()
        return all_geo

class Element(object):
    def __init__(self, json_file):
        self.obj_path = '/obj'
        self.mat_path = '/mat'
        self.overwrite_bgeo = False
        self.overwrite_element = False
        self._created_objs = set()
        self._assignment_cache = {}
        self.obj_file_stats = {}
        self.instance_stats = {}

        with open(json_file, 'r') as f:
            logging.info('Loading %s', json_file)
            self.json_data = json.load(f)

        with open(self.json_data['matFile'], 'r') as f:
            logging.info('Loading %s', self.json_data['matFile'])
            self.material_data = json.load(f)

    @property
    def element_bgeo(self):
        return os.path.join('bgeo', '%s.bgeo.sc' % self.name)

    @property
    def name(self):
        return self.json_data['name']

    def obj2bgeo(self, obj_path):
        path = obj_path.split('/',1)[1]
        path = os.path.splitext(path)[0]
        bgeo = 'bgeo/%s.bgeo.sc' % path
        if obj_path in self._created_objs:
            return bgeo
        if not os.path.exists(bgeo) or self.overwrite_bgeo:
            prim_counter = build_bgeo(obj_path, bgeo, self.name)
            self.obj_file_stats[obj_path] = prim_counter
            self._created_objs.add(obj_path)
        return bgeo

    def get_xform(self, element_dict):
        xform = element_dict.get('transformMatrix')
        if not xform:
            return hou.hmath.identityTransform()
        return hou.Matrix4(xform)

    def get_material_assignment(self, path):
        # First check the cache
        material = self._assignment_cache.get(path)
        if material:
            return material
        # Go digging
        for material in self.material_data:
            assignments = self.material_data[material].get('assignment',[])
            for assignment in assignments:
                if fnmatch.fnmatchcase(path, assignment):
                    logging.info('Matched %s with %s for %s', path, assignment, material)
                    self._assignment_cache[path] = material
                    return material
        raise ValueError('No material assignments match for %s' % path)

    def build_materials(self):
        logging.info('Building Material Networks')
        mat_node = hou.node(self.mat_path)
        for mat_name in self.material_data:
            mat_node_name = '%s.%s' % (self.name, mat_name)
            if mat_node.node(mat_node_name):
                mat_node.node(mat_node_name).destroy()
            logging.info('Creating %s material', mat_node_name)
            disney_mat = mat_node.createNode('disney_material', node_name=mat_node_name)
            for parm,vals in self.material_data[mat_name].iteritems():
                if parm in ('assignment',):
                    continue
                # This is needed because sometimes baseColor is size 3 and sometimes size 4
                if isinstance(vals, list):
                    vals = vals[:disney_mat.parmTuple(parm).parmTemplate().numComponents()]
                else:
                    vals = [vals,]
                logging.info('Setting %s parm to %s', parm, vals)
                disney_mat.parmTuple(parm).set(vals)

    def build_obj(self):
        obj_node = hou.node(self.obj_path)
        if obj_node.node(self.name):
            obj_node.node(self.name).destroy()
        elm_node = obj_node.createNode('geo', node_name=self.name)
        elm_node.parm('vm_rendersubd').set(True)
        file_node = elm_node.createNode('file', node_name='read_element_bgeo')
        file_node.parm('file').set(self.element_bgeo)
        file_node.parm('loadtype').set('delayed')

    def save_geo(self):
        if os.path.exists(self.element_bgeo) and not self.overwrite_element:
            logging.warning('Skipping existing Element: %s', self.name)
            return
        geo = self.build_element_geo()
        logging.info('Saving Element to %s', self.element_bgeo)
        geo.saveToFile(self.element_bgeo)
        geo.clear()

    def build_instanceprims(self, instance_dict):
        logging.info('Handling instancedPrimitiveJsonFiles')
        json_geo = hou.Geometry()
        for name, instance_prim in instance_dict.iteritems():
            logging.info('instancedPrimitiveJsonFiles: %s', name)
            if instance_prim['type'] == 'archive':
                json_prim = ArchivePrims(self, name, instance_prim)
            elif instance_prim['type'] == 'curve':
                json_prim = CurvePrims(self, name, instance_prim)
            elif instance_prim['type'] == 'element':
                json_prim = ElementPrims(self, name, instance_prim)
            else:
                logging.info('Unknown instance_prim type, %s, skipping',
                             instance_prim['type'])
                continue
            prim_geo = json_prim.build_geo()
            json_geo.merge(prim_geo)
            prim_geo.clear()
        return json_geo

    def build_geo(self, name, element_dict):

        logging.info('Building geo for %s', name)
        geom_file = element_dict['geomObjFile']
        bgeo_file = self.obj2bgeo(geom_file)

        base = hou.Geometry()
        name_atr = base.addAttrib(hou.attribType.Prim, 'name', '',
                                  create_local_variable=False)
        diskprim = base.createPacked('PackedDisk')
        diskprim.setIntrinsicValue('unexpandedfilename', bgeo_file)
        diskprim.setAttribValue(name_atr, name)

        # This can be None or {}
        instance_prim_data = element_dict.get('instancedPrimitiveJsonFiles')
        if instance_prim_data:
            json_geo = self.build_instanceprims(instance_prim_data)
            base.merge(json_geo)
            json_geo.clear()

        packed_geo = pack_geo(base, name, self.get_xform(element_dict))

        # We don't clear base since it was used to create geo
        return packed_geo

    def build_element_geo(self, variant=None):

        logging.info('Building element geo for %s', self.name)

        name = self.name
        element_dict = self.json_data
        if variant:
            name = variant
            logging.info('Constructing variant %s', name)
            if variant != 'base':
                element_dict = self.json_data['variants'][variant]
            else:
                # We don't need the transform matrix if we are a variant
                del element_dict['transformMatrix']
        geo = self.build_geo(name, element_dict)

        # If this was a variant exit out as we are done,
        # other wise keep on building any potential instance copies
        if variant:
            return geo

        out_geo = hou.Geometry()
        # We merge in the base geo
        out_geo.merge(geo)

        # Build instancedCopies
        for name, instance_dict in self.json_data.get('instancedCopies', {}).iteritems():
            logging.info('Creating instance copy for %s', name)
            if ( not instance_dict.get('instancedPrimitiveJsonFiles') and
                 not instance_dict.get('geomObjFile') ):
                # No overrides of instance prims or geom so we can just copy the pack
                for prim in geo.prims():
                    prim.setAttribValue('name', name)
                    prim.setTransform(self.get_xform(instance_dict))
                    out_geo.merge(geo)
            else:
                instance_geo = self.build_geo(name, instance_dict)
                out_geo.merge(instance_geo)
                instance_geo.clear()

        geo.clear()
        return out_geo

def find_elements():
    element_paths = glob.glob('./json/*')
    for element_path in element_paths:
        element_name = os.path.basename(element_path)
        if element_name in ('lights','cameras'):
            continue
        element_json = '%s/%s.json' % (element_path, element_name)
        if not os.path.exists(element_json):
            continue
        yield element_json

def build_json_cameras():
    logging.info('Building Json Cameras')
    cams_paths = glob.glob('./json/cameras/*.json')
    for path in cams_paths:
        build_camera(path)

def build_camera(json_file):

    with open(json_file, 'r') as f:
        json_data = json.load(f)
    obj_node = hou.node('/obj')
    cam_node = obj_node.createNode('cam', node_name=json_data['name'])

    logging.info('Building %s camera', json_data['name'])

    eye = hou.Vector3(json_data['eye'])
    up = hou.Vector3(json_data['up'])
    look = hou.Vector3(json_data['look'])

    cam_node.parmTuple('t').set(json_data['eye'])

    z = look - eye
    x = up.cross(z)
    y = z.cross(x)
    # x axis is flipped from houdini
    x = -x.normalized()
    y = y.normalized()
    z = z.normalized()
    mat3 = hou.Matrix3((x,y,z))
    cam_node.parmTuple('r').set(mat3.extractRotates())
    cam_node.parmTuple('res').set((2048, int(2048*1.0/json_data['ratio'])))
    focal = json_data['focalLength']
    cam_node.parm('focal').set(focal)
    fov_rad = math.radians(json_data['fov'])
    fov_ratio = (1.0/math.tan(fov_rad*0.5)) * 0.5
    cam_node.parm('aperture').set(focal/fov_ratio)
    cam_node.parm('focus').set(json_data['centerOfInterest'])
    cam_node.parm('fstop').set( focal / (json_data['lensRadius']*2.0) )
    cam_node.parm('far').set(1.0e6)

    create_rop(cam_node)

def build_json_lights():
    logging.info('Building Json Lights')
    light_paths = glob.glob('./json/lights/*.json')
    for path in light_paths:
        build_light(path)

def convert_tex(tex_path):
    # For whatever reason the paths in the json file are ./island/textures
    # instead of just ./textures. (Inconsistent with everything else)
    # We'll strip off the leading island
    tex_path_tokens = tex_path.split('/')
    if tex_path_tokens[0] == 'island':
        tex_path_tokens.pop(0)
    clean_tex_path = os.path.join(*tex_path_tokens)
    if tex_path_tokens[0] == 'textures':
        tex_path_tokens[0] = 'rats'
    tex_path_tokens[-1] = os.path.splitext(tex_path_tokens[-1])[0] + '.rat'
    rat_path = os.path.join(*tex_path_tokens)
    if os.path.exists(rat_path):
        return rat_path
    if not os.path.exists(os.path.dirname(rat_path)):
        os.makedirs(os.path.dirname(rat_path))
    logging.info('Converting %s to a rat texture', clean_tex_path)
    iconvert_path = os.path.expandvars('$HFS/bin/iconvert')
    if not os.path.exists(iconvert_path):
        raise OSError('Could find $HFS/bin/iconvert')
    proc = subprocess.Popen([iconvert_path, clean_tex_path, rat_path])
    stdout,stderr = proc.communicate()
    if proc.returncode != 0:
        logging.error(stderr)
        raise OSError('Could not create %s' % rat_path)
    return rat_path

def build_light(json_file):
    with open(json_file, 'r') as f:
        json_data = json.load(f)
    obj_node = hou.node('/obj')
    for light,light_data in json_data.iteritems():
        logging.info('Building %s light: %s', light_data['type'], light)
        linear_clr = [ pow(x, 2.2) for x in light_data['color'][0:3] ]
        if light_data['type'] == 'quad':
            light_node = obj_node.createNode('hlight', node_name=light)
            # Lights values are in a "monitor based color space"
            light_node.parmTuple('light_color').set(linear_clr)
            light_node.parm('light_exposure').set(light_data['exposure'])
            light_node.parm('light_type').set('grid')
            light_node.parmTuple('areasize').set((light_data['width'],
                                                  light_data['height']))
            light_node.parmTuple('t').set(light_data['location'])
            light_node.parmTuple('r').set(light_data['rotation'])
            light_node.parm('singlesided').set(True)
            light_node.parm('normalizearea').set(False)
        elif light_data['type'] == 'dome':
            light_node = obj_node.createNode('envlight', node_name=light)
            light_node.parmTuple('light_color').set(linear_clr)
            light_node.parm('light_exposure').set(light_data['exposure'])
            light_node.parmTuple('t').set(light_data['location'])
            # Houdini env lights are +180 degrees
            rot = light_data['rotation'][:]
            rot[1] = rot[1] + 180.0
            light_node.parmTuple('r').set(rot)
            rat = convert_tex(light_data['map'])
            light_node.parm('env_map').set(rat)
            # NOTE: We ignore envmapCamera, this can be emulated by mapping the image
            # to a dome or a second render, we'll just render the env map for now
            light_node.parm('light_contribprimary').set(True)

def post_scene_prep():
    # this is pretty high detail so lets just render as polys
    hou.node('/obj/osOcean').parm('vm_rendersubd').set(False)

def create_rop(camera):
    rop = hou.node('/out').createNode('ifd', node_name=camera.name())
    rop.parm('camera').set(camera.path())
    rop.parm('vm_readcheckpoint').set(True)
    rop.parm('vm_inlinestorage').set(True)
    rop.parm('vm_renderengine').set('pbrraytrace')
    rop.parmTuple('vm_samples').set([4,4])
    rop.parm('vm_maxraysamples').set(12)
    rop.parm('vm_minraysamples').set(2)
    rop.parm('vm_diffuselimit').set(2)
    rop.parm('vm_constrainmaxrough').set(True)
    rop.parm('soho_spoolrenderoutput').set(0)
    rop.parm('vm_verbose').set(2)
    rop.parm('vm_alfprogress').set(True)
    rop.parm('declare_all_shops').set('on')

@contextlib.contextmanager
def make_tempfile():
    tmpf = tempfile.NamedTemporaryFile(suffix='.obj', delete=False)
    try:
        yield tmpf
    except:
        raise
    finally:
        os.remove(tmpf.name)

def main():
    logging.info('Loading moana.hda')
    hou.hda.installFile('./otls/disney_material')
    for element_json in find_elements():
        logging.info('Loading Element Json: %s', element_json)
        element = Element(element_json)
        element.save_geo()
        element.build_materials()
        element.build_obj()
    build_json_cameras()
    build_json_lights()
    post_scene_prep()
    hou.hipFile.save('./island.hip', save_to_recent_files=False)

if __name__ == '__main__':
    main()

