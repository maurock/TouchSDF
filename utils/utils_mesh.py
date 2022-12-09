from glob import glob
import numpy as np
import trimesh
import os
from copy import deepcopy
import pybullet as pb
import data.objects as objects
from data_making import extract_urdf
import open3d as o3d

def urdf_to_mesh(filepath):
    """
    Receives path to object index containing the .URDF and verts and faces (both np.array).
    Directory tree:
    - obj_idx  <-- filepath
    |   - textured_objs
    |   |   - ...obj
    |- ...
    """
    total_objs = glob(os.path.join(filepath, 'textured_objs/*.obj'))
    verts = np.array([]).reshape((0,3))
    faces = np.array([]).reshape((0,3))

    mesh_list = []
    for obj_file in total_objs:
        mesh = _as_mesh(trimesh.load(obj_file))
        mesh_list.append(mesh)           
                
    verts_list = [mesh.vertices for mesh in mesh_list]
    faces_list = [mesh.faces for mesh in mesh_list]
    faces_offset = np.cumsum([v.shape[0] for v in verts_list], dtype=np.float32)   # num of faces per mesh
    faces_offset = np.insert(faces_offset, 0, 0)[:-1]            # compute offset for faces, otherwise they all start from 0
    verts = np.vstack(verts_list).astype(np.float32)
    faces = np.vstack([face + offset for face, offset in zip(faces_list, faces_offset)]).astype(np.float32)

    mesh = trimesh.Trimesh(verts, faces)
    return mesh


def _as_mesh(scene_or_mesh):
    # Utils function to get a mesh from a trimesh.Trimesh() or trimesh.scene.Scene()
    if isinstance(scene_or_mesh, trimesh.Scene):
        mesh = trimesh.util.concatenate([
            trimesh.Trimesh(vertices=m.vertices, faces=m.faces)
            for m in scene_or_mesh.geometry.values()])
    else:
        mesh = scene_or_mesh
    return mesh


def mesh_to_pointcloud(mesh, n_samples):
    """
    This method samples n points on a mesh. The number of samples for each face is weighted by its size. 

    Params:
        mesh = trimesh.Trimesh()
        n_samples: number of total samples
    
    Returns:
        pointcloud
    """
    pointcloud, _ = trimesh.sample.sample_surface(mesh, n_samples)
    pointcloud = pointcloud.astype(np.float32)
    return pointcloud


def rotate_vertices(vertices, rot=[np.pi / 2, 0, 0]):
    """Rotate vertices by 90 deg around the x-axis. """
    new_verts = deepcopy(vertices)
    # Rotate object
    rot_Q_obj = pb.getQuaternionFromEuler(rot)
    rot_M_obj = np.array(pb.getMatrixFromQuaternion(rot_Q_obj)).reshape(3, 3)
    new_verts = np.einsum('ij,kj->ik', rot_M_obj, new_verts).transpose(1, 0)
    return new_verts


def save_touch_charts(mesh_list, tactile_imgs, pointcloud_list, rot_M_wrld_list, pos_wrld_list, pos_wrk_list, initial_pos, path):
    """
    Receive list containing open3D.TriangleMesh of the local touch charts (25 vertices) and tactile images related to those meshes. It saves a dictionary containing vertices and faces as np array, and normalised tactile images. 

    Parameters:
        - mesh_list = list containing open3d.geometry.TriangleMesh (25 vertices and faces of the local geometry at touch site).
                        len (num touches)
        - tactile_imgs = np.array of tactile images, shape (num_touches, 256, 256)
        - pointcloud_list = np.array of pointclouds, containing 2000 randomly sampled points that represent the ground truth to compute the chamfer distance, shape (num_touches, 2000, 3)
        - rot_M_wrld_list: np.array of rotation matrices to convert from workframe to worldframe. shape (num_touches, 3, 3)
        - pos_wrld_list: np.array of positions of the TCP in worldframe. shape(num_touches, 3)
        - pos_wrk_list: np.array of positions of the TCP in workframe. shape(n, 3)
        - initial_pos: list of initial obj pos, len (3)
    Returns:
        - touch_charts_data, dictionary with keys: 'verts', 'faces', 'tactile_imgs', 'pointclouds', 'rot_M_wrld;, 'pos_wrld', 'pos_wrk'
            - 'verts': shape (n_samples, 75), ground truth vertices for various samples
            - 'faces': shape (n_faces, 3), concatenated triangles. The number of faces per sample varies, so it is not possible to store faces per sample.
            - 'tactile_imgs': shape (n_samples, 1, 256, 256)
            - 'pointclouds': shape (n_samples, 2000, 3), points randomly samples on the touch charts mesh surface.
            - 'rot_M_wrld': 3x3 rotation matrix collected from PyBullet.
            - 'pos_wrld': position of the sensor in world coordinates at touch, collected from PyBullet (robots.coords_at_touch)
            - 'pos_wrk': position of the sensor in world frame collected from PyBullet.
    """
    verts = np.array([], dtype=np.float32).reshape(0, 75)
    faces = np.array([], dtype=np.float32).reshape(0, 3)
    touch_charts_data = dict()

    for mesh in mesh_list:
        vert = np.asarray(mesh.vertices, dtype=np.float32).ravel()
        verts = np.vstack((verts, vert))
        faces = np.vstack((faces, np.asarray(mesh.triangles, dtype=np.float32)))   # (n, 3) not possible (b, n, 3) because n is not constant

    touch_charts_data['verts'] = verts
    touch_charts_data['faces'] = faces

    # Conv2D requires [batch, channels, size1, size2] as input. tactile_imgs is currently [num_samples, size1, size2]. I need to add a second dimension.
    tactile_imgs = np.expand_dims(tactile_imgs, 1) / 255     # normalize tactile images
    touch_charts_data['tactile_imgs'] = tactile_imgs
    touch_charts_data['pointclouds'] = pointcloud_list

    # Store data for rotation and translation
    touch_charts_data['rot_M_wrld'] = rot_M_wrld_list
    touch_charts_data['pos_wrld'] = pos_wrld_list
    touch_charts_data['pos_wrk'] = pos_wrk_list
    touch_charts_data['initial_pos'] = initial_pos

    np.save(path, touch_charts_data)


def get_mesh_z(obj_index, scale):
    """
    Compute the mesh geometry and return the initial z-axis. This is to avoid that the object
    goes partially throught the ground.
    """
    filepath_obj = os.path.join(os.path.dirname(objects.__file__), obj_index)
    mesh = urdf_to_mesh(filepath_obj)
    verts = mesh.vertices
    pointcloud_s = scale_pointcloud(np.array(verts), scale)
    pointcloud_s_r = rotate_pointcloud(pointcloud_s)
    z_values = pointcloud_s_r[:, 2]
    height = (np.amax(z_values) - np.amin(z_values))
    return height/2


def scale_pointcloud(pointcloud, scale=0.1):
    obj = deepcopy(pointcloud)
    obj = obj * scale
    return obj


def rotate_pointcloud(pointcloud, rot=[np.pi / 2, 0, 0]):
    """
    The default rotation reflects the rotation used for the object during data collection
    """
    obj = deepcopy(pointcloud)
    # Rotate object
    rot_Q_obj = pb.getQuaternionFromEuler(rot)
    rot_M_obj = np.array(pb.getMatrixFromQuaternion(rot_Q_obj)).reshape(3, 3)
    obj = np.einsum('ij,kj->ik', rot_M_obj, obj).transpose(1, 0)
    return obj


def get_ratio_urdf_deepsdf(mesh_urdf):
    """Get the ratio between the mesh in the URDF file and the processed DeepSDF mesh."""
    vertices = mesh_urdf.vertices - mesh_urdf.bounding_box.centroid
    distances = np.linalg.norm(vertices, axis=1)
    max_distances = np.max(distances)  # this is the ratio as well

    return max_distances


def preprocess_urdf():
    """The URDF mesh is processed by the loadURDF method in pybullet. It is scaled and rotated.
    This function achieves the same purpose: given a scale and a rotation matrix or quaternion, 
    it returns the vertices of the rotated and scaled mesh."""
    pass


def debug_draw_vertices_on_pb(vertices_wrld, color=[235, 52, 52]):
    color = np.array(color)/255
    color_From_array = np.full(shape=vertices_wrld.shape, fill_value=color)
    pb.addUserDebugPoints(
        pointPositions=vertices_wrld,
        pointColorsRGB=color_From_array,
        pointSize=1
    )


def translate_rotate_mesh(pos_wrld_list, rot_M_wrld_list, pointclouds_list, obj_initial_pos):
    """
    Given a pointcloud (workframe), the position of the TCP (worldframe), the rotation matrix (worldframe),
    it returns the pointcloud in worldframe. It assumes a default position of the object.

    Params:
        pos_wrld_list: (m, 3)
        rot_M_wrld_list: (m, 3, 3)
        pointclouds_list: pointcloud in workframe (m, number_points, 3)

    Returns:
    """
    a = rot_M_wrld_list @ pointclouds_list.transpose(0,2,1)
    b = a.transpose(0,2,1)
    c = pos_wrld_list[:, np.newaxis, :] + b
    pointcloud_wrld = c - obj_initial_pos
    return pointcloud_wrld


def load_save_objects(obj_dir):
    """
    Extract objects (verts and faces) from the URDF files in the PartNet-Mobility dataset.
    Store objects in dictionaries, where key=obj_idx and value=np.array[verts, faces]

    Args:
        obj_dir: directory containing the object folders
    Returns:
        dictionary of dictionaries, the first key is the object indexes, the second
        key are 'verts' and 'faces', both stores as np.array
    """
    # List all the objects in data/objects/
    list_objects = [filepath.split('/')[-1] for filepath in glob(os.path.join(obj_dir, '*'))]
    list_objects.remove('__init__.py')

    if '__pycache__' in list_objects:
        list_objects.remove('__pycache__')
    objs_dict = dict()
    
    for obj_index in list_objects:
        objs_dict[obj_index] = dict()
        filepath_obj = os.path.join(obj_dir, obj_index)
        mesh = urdf_to_mesh(filepath_obj)
        verts, faces = np.array(mesh.vertices), np.array(mesh.faces)
        verts_norm = extract_urdf.normalise_obj(verts)
        new_verts = rotate_vertices(verts_norm)
        objs_dict[obj_index]['verts'] = new_verts
        objs_dict[obj_index]['faces'] = faces
    return objs_dict  


def pointcloud_to_mesh(point_cloud):
    """
    Method to transform point cloud into mesh using the Open3D ball pivoting technique. 
    As seen here: https://stackoverflow.com/questions/56965268/how-do-i-convert-a-3d-point-cloud-ply-into-a-mesh-with-faces-and-vertices

    Parameters:
        point_cloud: np.array of 25 coordinates, obtained in pointcloud_to_vertices. They represent the vertices of the mesh.
    Return:
        mesh: open3d.geometry.TriangleMesh
    """
    pcd = o3d.geometry.PointCloud()
    pcd.points = o3d.utility.Vector3dVector(point_cloud)
    pcd.estimate_normals()

    # estimate radius for rolling ball
    distances = pcd.compute_nearest_neighbor_distance()
    avg_dist = np.mean(distances)
    radius = 1 * avg_dist   

    mesh = o3d.geometry.TriangleMesh.create_from_point_cloud_ball_pivoting(
            pcd,
            o3d.utility.DoubleVector([radius, radius * 2]))
    # to access vertices, np.array(mesh.vertices). Normals are computed by o3d, so they might be wrong.

    mesh = trimesh.Trimesh(np.asarray(mesh.vertices), np.asarray(mesh.triangles))
    return mesh