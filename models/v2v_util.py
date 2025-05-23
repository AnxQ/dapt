import sys
import numpy as np


def discretize(coord, cropped_size):
    '''[-1, 1] -> [0, cropped_size]'''
    min_normalized = -1
    max_normalized = 1
    scale = (max_normalized - min_normalized) / cropped_size
    return (coord - min_normalized) / scale 


def warp2continuous(coord, refpoint, cubic_size, cropped_size):
    '''
    Map coordinates in set [0, 1, .., cropped_size-1] to original range [-cubic_size/2+refpoint, cubic_size/2 + refpoint]
    '''
    min_normalized = -1
    max_normalized = 1

    scale = (max_normalized - min_normalized) / cropped_size
    coord = coord * scale + min_normalized  # -> [-1, 1]

    coord = coord * cubic_size / 2 + refpoint

    return coord


def scattering(coord, cropped_size, feature=None, aggregation='mean'):
    # coord: [0, cropped_size]
    # Assign range[0, 1) -> 0, [1, 2) -> 1, .. [cropped_size-1, cropped_size) -> cropped_size-1
    # That is, around center 0.5 -> 0, around center 1.5 -> 1 .. around center cropped_size-0.5 -> cropped_size-1
    coord = coord.astype(np.int32)

    # mask = (coord[:, 0] >= 0) & (coord[:, 0] < cropped_size) & \
    #        (coord[:, 1] >= 0) & (coord[:, 1] < cropped_size) & \
    #        (coord[:, 2] >= 0) & (coord[:, 2] < cropped_size)
    
    mask = np.all(coord >= 0, axis=-1) & np.all(coord < cropped_size, axis=-1)

    coord = coord[mask, :]

    cubic = np.zeros(cropped_size)

    # Note, directly map point coordinate (x, y, z) to index (i, j, k), instead of (k, j, i)
    # Need to be consistent with heatmap generating and coordinates extration from heatmap 
    cubic[coord[:, 0], coord[:, 1], coord[:, 2]] = 1

    return cubic


def extract_coord_from_output(output, center=True):
    '''
    output: shape (batch, jointNum, volumeSize, volumeSize, volumeSize)
    center: if True, add 0.5, default is true
    return: shape (batch, jointNum, 3)
    '''
    assert(len(output.shape) >= 3)
    vsize = output.shape[-3:]

    output_rs = output.reshape(-1, np.prod(vsize))
    max_index = np.unravel_index(np.argmax(output_rs, axis=1), vsize)
    max_index = np.array(max_index).T
    
    xyz_output = max_index.reshape([*output.shape[:-3], 3])

    # Note discrete coord can represents real range [coord, coord+1), see function scattering() 
    # So, move coord to range center for better fittness
    if center: xyz_output = xyz_output + 0.5

    return xyz_output


def generate_coord(points, refpoint, new_size, angle, trans, sizes):
    cubic_size, cropped_size, original_size = sizes

    # points shape: (n, 3)
    coord = points

    # note, will consider points within range [refpoint-cubic_size/2, refpoint+cubic_size/2] as candidates

    # normalize
    coord = (coord - refpoint) / (cubic_size/2)  # -> [-1, 1]

    # discretize
    coord = discretize(coord, cropped_size)  # -> [0, cropped_size]
    coord += (original_size / 2 - cropped_size / 2)  # move center to original volume 

    # resize around original volume center
    resize_scale = new_size / 100
    if new_size < 100:
        coord = coord * resize_scale + original_size/2 * (1 - resize_scale)
    elif new_size > 100:
        coord = coord * resize_scale - original_size/2 * (resize_scale - 1)
    else:
        # new_size = 100 if it is in test mode
        pass

    # rotation
    if angle != 0:
        rot_cos = np.cos(angle)
        rot_sin = np.sin(angle)
        rot_t = np.array([[rot_cos, -rot_sin, 0], [rot_sin, rot_cos, 0], [0, 0, 1]])
        original_coord = coord.copy()
        original_coord -= original_size / 2

        coord = np.dot(original_coord, np.transpose(rot_t)) + original_size / 2

        # original_coord[:,0] -= original_size / 2
        # original_coord[:,1] -= original_size / 2
        # coord[:,0] = original_coord[:,0]*np.cos(angle) - original_coord[:,1]*np.sin(angle)
        # coord[:,1] = original_coord[:,0]*np.sin(angle) + original_coord[:,1]*np.cos(angle)
        # coord[:,0] += original_size / 2
        # coord[:,1] += original_size / 2

    # translation
    # Note, if trans = (original_size/2 - cropped_size/2), the following translation will
    # cancel the above translation(after discretion). It will be set it when in test mode. 
    coord -= trans

    return coord


def generate_cubic_input(points, refpoint, new_size, angle, trans, sizes):
    _, cropped_size, _ = sizes
    coord = generate_coord(points, refpoint, new_size, angle, trans, sizes)

    # scattering
    cubic = scattering(coord, cropped_size)

    return cubic


def generate_heatmap_gt(keypoints, refpoint, new_size, angle, trans, sizes, d3outputs, pool_factor, std):
    _, cropped_size, _ = sizes
    d3output_x, d3output_y, d3output_z = d3outputs

    coord = generate_coord(keypoints, refpoint, new_size, angle, trans, sizes)  # [0, cropped_size]
    coord /= pool_factor  # [0, cropped_size/pool_factor]

    # heatmap generation
    output_size = (cropped_size / pool_factor).astype(np.int32)
    heatmap = np.zeros((keypoints.shape[0], *output_size))

    # use center of cell
    center_offset = 0.5

    for i in range(coord.shape[0]):
        xi, yi, zi= coord[i]
        heatmap[i] = np.exp(-(np.power((d3output_x+center_offset-xi)/std, 2)/2 + \
            np.power((d3output_y+center_offset-yi)/std, 2)/2 + \
            np.power((d3output_z+center_offset-zi)/std, 2)/2))

    return heatmap


class V2VVoxelization(object):
    def __init__(self, cubic_size, grid_size, crop_factor=1.0, augmentation=True):
        self.cubic_size = np.array(cubic_size)
        self.cropped_size, self.original_size = (np.array(grid_size) * crop_factor).astype(np.int32), np.array(grid_size)
        self.sizes = (self.cubic_size, self.cropped_size, self.original_size)
        self.pool_factor = 2
        self.std = 1.7
        self.augmentation = augmentation

        output_size = (self.cropped_size / self.pool_factor).astype(np.int32)
        # Note, range(size) and indexing = 'ij'
        X, Y, Z = output_size
        self.d3outputs = np.meshgrid(np.arange(X), np.arange(Y), np.arange(Z), indexing='ij')

    def __call__(self, sample):
        points, keypoints, refpoint = sample['coord'], sample['keypoints_3d'][:, :3], np.zeros(3)

        ## Augmentations
        # Resize
        new_size = np.random.rand() * 40 + 80

        # Rotation
        angle = np.random.rand() * 80/180*np.pi - 40/180*np.pi

        # Translation
        trans = np.random.rand(3) * (self.original_size-self.cropped_size)
        
        if not self.augmentation:
            new_size = 100
            angle = 0
            trans = self.original_size/2 - self.cropped_size/2

        input = generate_cubic_input(points, refpoint, new_size, angle, trans, self.sizes)
        heatmap = generate_heatmap_gt(keypoints, refpoint, new_size, angle, trans, self.sizes, self.d3outputs, self.pool_factor, self.std)

        return dict(
            voxel=input.reshape((1, *input.shape)),
            heatmap=heatmap,
            keypoints_3d=sample['keypoints_3d'][:, :3],
            keypoints_3d_vis=sample['keypoints_3d'][:, 3]
        )

    def voxelize(self, points, refpoint):
        new_size, angle, trans = 100, 0, self.original_size/2 - self.cropped_size/2
        input = generate_cubic_input(points, refpoint, new_size, angle, trans, self.sizes)
        return input.reshape((1, *input.shape))

    def generate_heatmap(self, keypoints, refpoint):
        new_size, angle, trans = 100, 0, self.original_size/2 - self.cropped_size/2
        heatmap = generate_heatmap_gt(keypoints, refpoint, new_size, angle, trans, self.sizes, self.d3outputs, self.pool_factor, self.std)
        return heatmap

    def evaluate(self, heatmaps, refpoints):
        coords = extract_coord_from_output(heatmaps)
        coords *= self.pool_factor
        keypoints = warp2continuous(coords, refpoints, self.cubic_size, self.cropped_size)
        return keypoints
