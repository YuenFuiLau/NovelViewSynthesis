import os, random, datetime
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.tensorboard import SummaryWriter
import numpy as np
import imageio
import matplotlib.pyplot as plt
from tqdm.notebook import tqdm
from IPython.display import Image


from nerfmm.utils.pos_enc import encode_position
from nerfmm.utils.volume_op import volume_rendering, volume_sampling_ndc
from nerfmm.utils.comp_ray_dir import comp_ray_dir_cam_fxfy

from nerfmm.utils.training_utils import mse2psnr
from nerfmm.utils.lie_group_helper import convert3x4_4x4

from PIL import Image as PILImage
import json

#utils parameters
GPU_ID = "cuda:0"
scene_name = "room2"
image_dir = f"{os.getcwd()}/data/image/{scene_name}"
model_weight_path = f"{os.getcwd()}/model_weight/{scene_name}" 

file = open(f"{os.getcwd()}/data/poses_info/{scene_name}/poses.json")
poses_info = json.load(file)

poses_last5 = poses_info["poses"][-5:]

camera_poses = np.array(poses_last5[2])[:15].reshape((3,5))
focal_f = camera_poses[-1,-1]

camera_ex = camera_poses[:,:3]

#load image
def load_imgs(image_dir):
    img_names = np.array(sorted(os.listdir(image_dir)))  # all image names
    img_paths = [os.path.join(image_dir, n) for n in img_names]
    N_imgs = len(img_paths)

    img_list = []
    for p in img_paths:
        img = imageio.imread(p)[:, :, :3]  # (H, W, 3) np.uint8
        img = PILImage.fromarray(img).resize((960,540)) #reshape
        img_list.append(img)
    img_list = np.stack(img_list)  # (N, H, W, 3)
    img_list = torch.from_numpy(img_list).float() / 255  # (N, H, W, 3) torch.float32
    H, W = img_list.shape[1], img_list.shape[2]
    
    results = {
        'imgs': img_list,  # (N, H, W, 3) torch.float32
        'img_names': img_names,  # (N, )
        'N_imgs': N_imgs,
        'H': H,
        'W': W,
    }
    return results

image_data = load_imgs(image_dir)
imgs = image_data['imgs']  # (N, H, W, 3) torch.float32

N_IMGS = image_data['N_imgs']
H = image_data['H']
W = image_data['W']

#define learnable focals
class LearnFocal(nn.Module):
    def __init__(self, H, W, req_grad):
        super(LearnFocal, self).__init__()
        self.H = H
        self.W = W
        #self.fx = nn.Parameter(torch.tensor(focal_f, dtype=torch.float32), requires_grad=req_grad)  # (1, )
        self.fx = nn.Parameter(torch.tensor(1.0, dtype=torch.float32), requires_grad=req_grad)
        #self.fy = nn.Parameter(torch.tensor(focal_f, dtype=torch.float32), requires_grad=req_grad)  # (1, )
        self.fy = nn.Parameter(torch.tensor(1.0, dtype=torch.float32), requires_grad=req_grad)

    def forward(self):
        # order = 2, check our supplementary.
        fxfy = torch.stack([self.fx**2 * self.W, self.fy**2 * self.H])
        return fxfy


#define learnable poses
def vec2skew(v):
    """
    :param v:  (3, ) torch tensor
    :return:   (3, 3)
    """
    zero = torch.zeros(1, dtype=torch.float32, device=v.device)
    skew_v0 = torch.cat([ zero,    -v[2:3],   v[1:2]])  # (3, 1)
    skew_v1 = torch.cat([ v[2:3],   zero,    -v[0:1]])
    skew_v2 = torch.cat([-v[1:2],   v[0:1],   zero])
    skew_v = torch.stack([skew_v0, skew_v1, skew_v2], dim=0)  # (3, 3)
    return skew_v  # (3, 3)


def Exp(r):
    """so(3) vector to SO(3) matrix
    :param r: (3, ) axis-angle, torch tensor
    :return:  (3, 3)
    """
    skew_r = vec2skew(r)  # (3, 3)
    norm_r = r.norm() + 1e-15
    eye = torch.eye(3, dtype=torch.float32, device=r.device)
    #R = eye + (torch.sin(norm_r) / norm_r) * skew_r + ((1 - torch.cos(norm_r)) / norm_r**2) * (skew_r @ skew_r)
    R = torch.from_numpy(camera_ex).type(torch.float32).to(GPU_ID)
    return R


def make_c2w(r, t):
    """
    :param r:  (3, ) axis-angle             torch tensor
    :param t:  (3, ) translation vector     torch tensor
    :return:   (4, 4)
    """
    R = Exp(r)  # (3, 3)
    c2w = torch.cat([R, t.unsqueeze(1)], dim=1)  # (3, 4)
    c2w = convert3x4_4x4(c2w)  # (4, 4)
    return c2w


class LearnPose(nn.Module):
    def __init__(self, num_cams, learn_R, learn_t):
        super(LearnPose, self).__init__()
        self.num_cams = num_cams
        #self.r = nn.Parameter(torch.zeros(size=(num_cams, 3), dtype=torch.float32), requires_grad=learn_R)  # (N, 3)
        self.r = nn.Parameter(torch.ones(size = (num_cams,3),dtype = torch.float32), requires_grad=learn_R)
        #self.t = nn.Parameter(torch.zeros(size=(num_cams, 3), dtype=torch.float32), requires_grad=learn_t)  # (N, 3)
        self.t = nn.Parameter(torch.ones(size = (num_cams,3),dtype = torch.float32), requires_grad=learn_t)

    def forward(self, cam_id):
        r = self.r[cam_id]  # (3, ) axis-angle
        t = self.t[cam_id]  # (3, )
        c2w = make_c2w(r, t)  # (4, 4)
        return c2w


#define model
class TinyNerf(nn.Module):
   
    def __init__(self, pos_in_dims, dir_in_dims, D):
        """
        :param pos_in_dims: scalar, number of channels of encoded positions
        :param dir_in_dims: scalar, number of channels of encoded directions
        :param D:           scalar, number of hidden dimensions
        """
        super(TinyNerf, self).__init__()

        self.pos_in_dims = pos_in_dims
        self.dir_in_dims = dir_in_dims

        self.layers0 = nn.Sequential(
            nn.Linear(pos_in_dims, D), nn.ReLU(),
            nn.Linear(D, D), nn.ReLU(),
            nn.Linear(D, D), nn.ReLU(),
            nn.Linear(D, D), nn.ReLU(),
        )

        self.fc_density = nn.Linear(D, 1)
        self.fc_feature = nn.Linear(D, D)
        self.rgb_layers = nn.Sequential(nn.Linear(D + dir_in_dims, D//2), nn.ReLU())
        self.fc_rgb = nn.Linear(D//2, 3)

        self.fc_density.bias.data = torch.tensor([0.1]).float()
        self.fc_rgb.bias.data = torch.tensor([0.02, 0.02, 0.02]).float()

    def forward(self, pos_enc, dir_enc):
        """
        :param pos_enc: (H, W, N_sample, pos_in_dims) encoded positions
        :param dir_enc: (H, W, N_sample, dir_in_dims) encoded directions
        :return: rgb_density (H, W, N_sample, 4)
        """
        x = self.layers0(pos_enc)  # (H, W, N_sample, D)
        density = self.fc_density(x)  # (H, W, N_sample, 1)

        feat = self.fc_feature(x)  # (H, W, N_sample, D)
        x = torch.cat([feat, dir_enc], dim=3)  # (H, W, N_sample, D+dir_in_dims)
        x = self.rgb_layers(x)  # (H, W, N_sample, D/2)
        rgb = self.fc_rgb(x)  # (H, W, N_sample, 3)

        rgb_den = torch.cat([rgb, density], dim=3)  # (H, W, N_sample, 4)
        return rgb_den

#set ray parameter
class RayParameters():
    def __init__(self):
      self.NEAR, self.FAR = 0.0, 1.0  # ndc near far
      self.N_SAMPLE = 128  # samples per ray
      self.POS_ENC_FREQ = 10  # positional encoding freq for location
      self.DIR_ENC_FREQ = 4   # positional encoding freq for direction

ray_params = RayParameters()


#define train function
def model_render_image(c2w, rays_cam, t_vals, ray_params, H, W, fxfy, nerf_model,
                       perturb_t, sigma_noise_std):
    """
    :param c2w:         (4, 4)                  pose to transform ray direction from cam to world.
    :param rays_cam:    (someH, someW, 3)       ray directions in camera coordinate, can be random selected
                                                rows and cols, or some full rows, or an entire image.
    :param t_vals:      (N_samples)             sample depth along a ray.
    :param perturb_t:   True/False              perturb t values.
    :param sigma_noise_std: float               add noise to raw density predictions (sigma).
    :return:            (someH, someW, 3)       volume rendered images for the input rays.
    """
    # KEY 2: sample the 3D volume using estimated poses and intrinsics online.
    # (H, W, N_sample, 3), (H, W, 3), (H, W, N_sam)
    sample_pos, _, ray_dir_world, t_vals_noisy = volume_sampling_ndc(c2w, rays_cam, t_vals, ray_params.NEAR,
                                                                     ray_params.FAR, H, W, fxfy, perturb_t)

    # encode position: (H, W, N_sample, (2L+1)*C = 63)
    pos_enc = encode_position(sample_pos, levels=ray_params.POS_ENC_FREQ, inc_input=True)

    # encode direction: (H, W, N_sample, (2L+1)*C = 27)
    ray_dir_world = F.normalize(ray_dir_world, p=2, dim=2)  # (H, W, 3)
    dir_enc = encode_position(ray_dir_world, levels=ray_params.DIR_ENC_FREQ, inc_input=True)  # (H, W, 27)
    dir_enc = dir_enc.unsqueeze(2).expand(-1, -1, ray_params.N_SAMPLE, -1)  # (H, W, N_sample, 27)

    # inference rgb and density using position and direction encoding.
    rgb_density = nerf_model(pos_enc, dir_enc)  # (H, W, N_sample, 4)

    render_result = volume_rendering(rgb_density, t_vals_noisy, sigma_noise_std, rgb_act_fn=torch.sigmoid)
    rgb_rendered = render_result['rgb']  # (H, W, 3)
    depth_map = render_result['depth_map']  # (H, W)

    result = {
        'rgb': rgb_rendered,  # (H, W, 3)
        'depth_map': depth_map,  # (H, W)
    }

    return result


def train_one_epoch(imgs, H, W, ray_params, opt_nerf, opt_focal,
                    opt_pose, nerf_model, focal_net, pose_param_net):
    nerf_model.train()
    focal_net.train()
    pose_param_net.train()

    t_vals = torch.linspace(ray_params.NEAR, ray_params.FAR, ray_params.N_SAMPLE, device=GPU_ID)  # (N_sample,) sample position
    L2_loss_epoch = []

    # shuffle the training imgs
    ids = np.arange(N_IMGS)
    np.random.shuffle(ids)

    for i in ids:
        fxfy = focal_net()

        # KEY 1: compute ray directions using estimated intrinsics online.
        ray_dir_cam = comp_ray_dir_cam_fxfy(H, W, fxfy[0], fxfy[1])
        img = imgs[i].to(GPU_ID)  # (H, W, 4)
        c2w = pose_param_net(i)  # (4, 4)

        # sample 32x32 pixel on an image and their rays for training.
        r_id = torch.randperm(H, device=GPU_ID)[:32]  # (N_select_rows)
        c_id = torch.randperm(W, device=GPU_ID)[:32]  # (N_select_cols)
        ray_selected_cam = ray_dir_cam[r_id][:, c_id]  # (N_select_rows, N_select_cols, 3)
        img_selected = img[r_id][:, c_id]  # (N_select_rows, N_select_cols, 3)

        # render an image using selected rays, pose, sample intervals, and the network
        render_result = model_render_image(c2w, ray_selected_cam, t_vals, ray_params,
                                           H, W, fxfy, nerf_model, perturb_t=True, sigma_noise_std=0.0)
        rgb_rendered = render_result['rgb']  # (N_select_rows, N_select_cols, 3)
        L2_loss = F.mse_loss(rgb_rendered, img_selected)  # loss for one image

        L2_loss.backward()
        opt_nerf.step()
        opt_focal.step()
        opt_pose.step()
        opt_nerf.zero_grad()
        opt_focal.zero_grad()
        opt_pose.zero_grad()

        L2_loss_epoch.append(L2_loss)

    L2_loss_epoch_mean = torch.stack(L2_loss_epoch).mean().item()
    return L2_loss_epoch_mean

#define evaluation function
def render_novel_view(c2w, H, W, fxfy, ray_params, nerf_model):
    nerf_model.eval()

    ray_dir_cam = comp_ray_dir_cam_fxfy(H, W, fxfy[0], fxfy[1])
    t_vals = torch.linspace(ray_params.NEAR, ray_params.FAR, ray_params.N_SAMPLE, device=GPU_ID)  # (N_sample,) sample position

    c2w = c2w.to(GPU_ID)  # (4, 4)

    # split an image to rows when the input image resolution is high
    rays_dir_cam_split_rows = ray_dir_cam.split(10, dim=0)  # input 10 rows each time
    rendered_img = []
    rendered_depth = []
    for rays_dir_rows in rays_dir_cam_split_rows:
        render_result = model_render_image(c2w, rays_dir_rows, t_vals, ray_params,
                                           H, W, fxfy, nerf_model,
                                           perturb_t=False, sigma_noise_std=0.0)
        rgb_rendered_rows = render_result['rgb']  # (num_rows_eval_img, W, 3)
        depth_map = render_result['depth_map']  # (num_rows_eval_img, W)

        rendered_img.append(rgb_rendered_rows)
        rendered_depth.append(depth_map)

    # combine rows to an image
    rendered_img = torch.cat(rendered_img, dim=0)  # (H, W, 3)
    rendered_depth = torch.cat(rendered_depth, dim=0)  # (H, W)
    return rendered_img, rendered_depth



if __name__ == "__main__":

   #clear GPU memory
   #torch.cuda.empty_cache()

   N_EPOCH = 30000  # 10K epochs are performed in original paper
   EVAL_INTERVAL = 50  # render an image to visualise for every this interval.

   # Initialise all trainabled parameters
   focal_net = LearnFocal(H, W, req_grad=True).cuda(int(GPU_ID[-1]))
   #focal_net = LearnFocal(H, W, req_grad=True)
   #focal_net.load_state_dict(torch.load(f"{model_weight_path}/{scene_name}_focal.pt"))
   #focal_net = focal_net.cuda(int(GPU_ID[-1]))
   
   pose_param_net = LearnPose(num_cams=N_IMGS, learn_R=True, learn_t=True).cuda(int(GPU_ID[-1]))
   #pose_param_net = LearnPose(num_cams=N_IMGS, learn_R=True, learn_t=True)
   #pose_param_net.load_state_dict(torch.load(f"{model_weight_path}/{scene_name}_pose.pt"))
   #pose_param_net = pose_param_net.cuda(int(GPU_ID[-1]))

   # Get a tiny NeRF model. Hidden dimension set to 256(128)
   nerf_model = TinyNerf(pos_in_dims=63, dir_in_dims=27, D=400).cuda(int(GPU_ID[-1]))

   # Set lr and scheduler: these are just stair-case exponantial decay lr schedulers.
   opt_nerf = torch.optim.Adam(nerf_model.parameters(), lr=0.001)
   opt_focal = torch.optim.Adam(focal_net.parameters(), lr=0.001)
   opt_pose = torch.optim.Adam(pose_param_net.parameters(), lr=0.001)

   from torch.optim.lr_scheduler import MultiStepLR
   scheduler_nerf = MultiStepLR(opt_nerf, milestones=list(range(0, 10000, 10)), gamma=0.9954)
   scheduler_focal = MultiStepLR(opt_focal, milestones=list(range(0, 10000, 100)), gamma=0.9)
   scheduler_pose = MultiStepLR(opt_pose, milestones=list(range(0, 10000, 100)), gamma=0.9)

   # Set tensorboard writer
   writer = SummaryWriter(log_dir=os.path.join('logs', scene_name, str(datetime.datetime.now().strftime('%y%m%d_%H%M%S'))))

   # Store poses to visualise them later
   pose_history = []

   #train set up
   step_i = 0

   # Training
   #print('Training... Check results in the tensorboard above.')
   for epoch_i in tqdm(range(N_EPOCH), desc='Training'):
       """
       if step_i == 2000:

           #save carmera model
           torch.save(focal_net.state_dict(),f"{model_weight_path}/{scene_name}_focal.pt")
           torch.save(pose_param_net.state_dict(),f"{model_weight_path}/{scene_name}_pose.pt")
           break
           #clear GPU memory
           del focal_net
           del pose_param_net
           del nerf_model
           torch.cuda.empty_cache()

           #reload carmera model
           focal_net = LearnFocal(H, W, req_grad=True)
           focal_net.load_state_dict(torch.load(f"{model_weight_path}/{scene_name}_focal.pt"))
           focal_net = focal_net.cuda(int(GPU_ID[-1]))

           pose_param_net = LearnPose(num_cams=N_IMGS, learn_R=True, learn_t=True)
           pose_param_net.load_state_dict(torch.load(f"{model_weight_path}/{scene_name}_pose.pt"))
           pose_param_net = pose_param_net.cuda(int(GPU_ID[-1]))

           #re-initialize Nerf
           nerf_model = TinyNerf(pos_in_dims=63, dir_in_dims=27, D=256).cuda(int(GPU_ID[-1]))
       """
       L2_loss = train_one_epoch(imgs, H, W, ray_params, opt_nerf, opt_focal,
                                 opt_pose, nerf_model, focal_net, pose_param_net)
       train_psnr = mse2psnr(L2_loss)

       writer.add_scalar('train/psnr', train_psnr, epoch_i)

       fxfy = focal_net()
       print('epoch {0:4d} Training PSNR {1:.3f}, estimated fx {2:.1f} fy {3:.1f}'.format(epoch_i, train_psnr, fxfy[0], fxfy[1]))

       scheduler_nerf.step()
       scheduler_focal.step()
       scheduler_pose.step()

       learned_c2ws = torch.stack([pose_param_net(i) for i in range(N_IMGS)])  # (N, 4, 4)
       pose_history.append(learned_c2ws[:, :3, 3])  # (N, 3) only store positions as we vis in 2D.
              
       with torch.no_grad():
                      
           if (epoch_i+1) % EVAL_INTERVAL == 0:
               eval_c2w = torch.eye(4, dtype=torch.float32)  # (4, 4)
               fxfy = focal_net()
               rendered_img, rendered_depth = render_novel_view(eval_c2w, H, W, fxfy, ray_params, nerf_model)
               writer.add_image('eval/img', rendered_img.permute(2, 0, 1), global_step=epoch_i)
               writer.add_image('eval/depth', rendered_depth.unsqueeze(0), global_step=epoch_i)
              
       #update step i
       step_i = step_i + 1

   pose_history = torch.stack(pose_history).detach().cpu().numpy()  # (N_epoch, N_img, 3)
   print('Training finished.')


   #novel view synthesis
   # Render novel views from a sprial camera trajectory.
   # The spiral trajectory generation function is modified from https://github.com/kwea123/nerf_pl.
   from nerfmm.utils.pose_utils import create_spiral_poses

   # Render full images are time consuming, especially on colab so we render a smaller version instead.
   resize_ratio = 1
   with torch.no_grad():
       optimised_poses = torch.stack([pose_param_net(i) for i in range(N_IMGS)])
       radii = np.percentile(np.abs(optimised_poses.cpu().numpy()[:, :3, 3]), q=75, axis=0)  # (3,)
       spiral_c2ws = create_spiral_poses(radii, focus_depth=3.5, n_poses=30, n_circle=1)
       spiral_c2ws = torch.from_numpy(spiral_c2ws).float()  # (N, 3, 4)

       # change intrinsics according to resize ratio
       fxfy = focal_net()
       novel_fxfy = fxfy / resize_ratio
       novel_H, novel_W = H // resize_ratio, W // resize_ratio

       print('NeRF trained in {0:d} x {1:d} for {2:d} epochs'.format(H, W, N_EPOCH))
       print('Rendering novel views in {0:d} x {1:d}'.format(novel_H, novel_W))

       novel_img_list, novel_depth_list = [], []
       for i in tqdm(range(spiral_c2ws.shape[0]), desc='novel view rendering'):
           novel_img, novel_depth = render_novel_view(spiral_c2ws[i], novel_H, novel_W, novel_fxfy,
                                                      ray_params, nerf_model)
           novel_img_list.append(novel_img)
           novel_depth_list.append(novel_depth)

       print('Novel view rendering done. Saving to GIF images...')
       novel_img_list = (torch.stack(novel_img_list) * 255).cpu().numpy().astype(np.uint8)
       novel_depth_list = (torch.stack(novel_depth_list) * 200).cpu().numpy().astype(np.uint8)  # depth is always in 0 to 1 in NDC

       #os.makedirs('nvs_results', exist_ok=True)
       imageio.mimwrite(os.path.join(f"{os.getcwd()}/nvs_result", scene_name + 'refined_img.gif'), novel_img_list, fps=30)
       imageio.mimwrite(os.path.join(f"{os.getcwd()}/nvs_result", scene_name + 'refined_depth.gif'), novel_depth_list, fps=30)
       print('GIF images saved.')
