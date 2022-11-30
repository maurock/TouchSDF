import numpy as np
import torch
import results
import os
import meshplot as mp
mp.offline()
import model.sdf_model as sdf_model
import argparse
from tqdm import tqdm 
import utils.utils as utils
from torch.utils.tensorboard import SummaryWriter
from datetime import datetime
import json
from mesh_to_sdf import sample_sdf_near_surface
import trimesh
import plotly.graph_objects as go

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

def initialise_latent_code(latent_size, results_dict):
    """Initialise latent code as the average over all the obtained latent codes"""
    #latent_code = torch.mean(torch.from_numpy(results_dict['train']['best_latent_codes']), dim=0).view(1, -1).to(device)
    #latent_code.requires_grad = True
    latent_code = torch.normal(0, 0.01, size = (1, latent_size), dtype=torch.float32, requires_grad=True, device=device)
    return latent_code

def get_data(args, test_path):
    """Return x and y. Sample N points from the desired object.
    
    Params:
        - idx_obj_dict: index of the object in the dictionary. e.g. 0, 1, 2.. the first, second, third object in objs_dict.
                       If None, a random object is selected.
        - test_path: path where to save the touch point cloud
    
    Returns:
        - coords: points samples on the object
        - sdf_gt: ground truth for the signed distance function 
    """
    # dictionaries
    objs_dict = np.load(os.path.join(os.path.dirname(results.__file__), 'objs_dict.npy'), allow_pickle=True).item()
    samples_dict = np.load(os.path.join(os.path.dirname(results.__file__), 'samples_dict.npy'), allow_pickle=True).item()

    # list of objects
    objs = list(samples_dict.keys())

    # select random object
    if args.index_objs_dict != -1 and args.index_objs_dict < len(objs):
        random_obj = objs[args.index_objs_dict]
    else:
        random_obj = objs[np.random.randint(0, len(objs))]

    # mesh for random object
    mesh_dict = objs_dict[random_obj]

    mesh = trimesh.Trimesh(mesh_dict['verts'], mesh_dict['faces'])
    coords_temp, sdf_temp = sample_sdf_near_surface(mesh, number_of_points=args.num_samples, sign_method='depth')
    coords_array = coords_temp[(sdf_temp < 0.001) & (sdf_temp>-0.001)]
    sdf_gt_array = sdf_temp[(sdf_temp < 0.001) & (sdf_temp>-0.001)]

    # simulate touch point clouds
    if args.touches > 0:
        _coords_all = np.copy(coords_array)              # store for plotting

        voxel_coords_all = np.array([], dtype=np.float32).reshape(0, 3)
        voxel_sdf_all = np.array([], dtype=np.float32).reshape(0)

        for _ in range(0, args.touches):
            # sample the centre of a voxel
            idx_centre = np.random.choice(np.arange(0, coords_array.shape[0]))
            centre = coords_array[idx_centre, :]

            # create a vixel and collect data inside it
            upper_bound = centre + 0.06
            lower_bound = centre - 0.06
            condition = [i.all() for i in ((coords_array <= upper_bound) & (coords_array >= lower_bound))]
            voxel_coords_all = np.vstack((voxel_coords_all, coords_array[condition, :]))
            voxel_sdf_all = np.hstack((voxel_sdf_all, sdf_gt_array[condition]))
            
        coords_array = voxel_coords_all
        sdf_gt_array = voxel_sdf_all       
        _debug_plot(_coords_all, coords_array, test_path)

    coords = torch.from_numpy(coords_array).to(device)
    sdf_gt = torch.from_numpy(sdf_gt_array).to(device)

    if args.clamp:
        sdf_gt = torch.clamp(sdf_gt, -args.clamp_value, args.clamp_value)

    return coords, sdf_gt

def _debug_plot(_coords_all, coords_array, test_path):
    fig = go.Figure(
    [
        go.Scatter3d(x=_coords_all[:, 0], y=_coords_all[:, 1],z=_coords_all[:, 2], mode='markers', marker=dict(size=2)),
        go.Scatter3d(x=coords_array[:, 0], y=coords_array[:, 1],z=coords_array[:, 2], mode='markers', marker=dict(size=2))
    ]
    )
    fig.write_html(os.path.join(test_path, "touches_gt.html"))
    fig = go.Figure(
    [
        go.Scatter3d(x=coords_array[:, 0], y=coords_array[:, 1],z=coords_array[:, 2], mode='markers', marker=dict(size=2))
    ]
    )
    fig.write_html(os.path.join(test_path, "touches.html"))

def main(args):
    folder = args.folder

    # Logging
    test_path = os.path.join(os.path.dirname(results.__file__), 'runs', folder, 'test', datetime.now().strftime('%d_%m_%H%M%S'))
    writer = SummaryWriter(log_dir=test_path)
    log_path = os.path.join(test_path, 'settings.txt')
    args_dict = vars(args)  # convert args to dict to write them as json
    with open(log_path, mode='a') as log:
        log.write('Settings:\n')
        log.write(json.dumps(args_dict).replace(', ', ',\n'))
        log.write('\n\n')

    model = sdf_model.SDFModelMulti(num_layers=8, no_skip_connections=False).to(device)

    # Load weights
    weights_path = os.path.join(os.path.dirname(results.__file__), 'runs', folder, 'weights.pt')
    model.load_state_dict(torch.load(weights_path, map_location=device))

    # Load results dictionary
    results_dict_path = os.path.join(os.path.dirname(results.__file__), 'runs', folder, 'results.npy')
    results_dict = np.load(results_dict_path, allow_pickle=True).item()

    # Initialise latent code and optimiser
    latent_code = initialise_latent_code(args.latent_size, results_dict)
    optim_adam = torch.optim.Adam([latent_code], lr=args.lr)
    optim_lbfgs = torch.optim.LBFGS([latent_code], lr=0.0001, max_iter=args.LBFGS_maxiter )

    if args.lr_scheduler:
        scheduler_latent = torch.optim.lr_scheduler.ReduceLROnPlateau(optim_adam, mode='min', 
                                                factor=args.lr_multiplier, 
                                                patience=args.patience, 
                                                threshold=0.0001, threshold_mode='rel')
    
    # create dataset
    coords, sdf_gt = get_data(args, test_path)

    best_loss = 1000000

    # prediction
    for epoch in tqdm(range(0, args.epochs)):
        latent_code_tile = torch.tile(latent_code, (coords.shape[0], 1))
        x = torch.hstack((latent_code_tile, coords))

        # Adam 
        if args.args.lr > args.lr * (args.lr_multiplier)**5:    # if less than 5 updates

            optim_adam.zero_grad()

            predictions = model(x)

            if args.clamp:
                predictions = torch.clamp(predictions, -args.clamp_value, args.clamp_value)

            loss_value = utils.SDFLoss_multishape(sdf_gt, predictions, x[:, :args.latent_size], sigma=args.sigma_regulariser)
            loss_value.backward()
            
            #  add langevin noise (optional)
            if args.langevin_noise > 0:
                noise = torch.normal(0, args.langevin_noise, size = (1, args.latent_size), dtype=torch.float32, requires_grad=False, device=device)
                latent_code.grad = latent_code.grad + noise

            optim_adam.step()

        # LBFGS
        else:
            args.lr_scheduler = False

            def closure():
                optim_lbfgs.zero_grad()

                predictions = model(x)

                if args.clamp:
                    predictions = torch.clamp(predictions, -args.clamp_value, args.clamp_value)

                loss_value = utils.SDFLoss_multishape(sdf_gt, predictions, x[:, :args.latent_size], sigma=args.sigma_regulariser)
                loss_value.backward()

                return loss_value

            optim_lbfgs.step(closure)

            loss_value = closure()

        if loss_value.detach().cpu().item() < best_loss:
            best_loss = loss_value.detach().cpu().item()
            best_latent_code = latent_code.clone()

        # step scheduler and store on tensorboard (optional)
        if args.lr_scheduler:
            scheduler_latent.step(loss_value.item())
            writer.add_scalar('Learning rate', scheduler_latent._last_lr[0], epoch)

        # logging
        writer.add_scalar('Training loss', loss_value.detach().cpu().item(), epoch)
        # store latent codes and their gradient on tensorboard
        tag = f"latent_code_0"
        writer.add_histogram(tag, latent_code, global_step=epoch)
        tag = f"grad_latent_code_0"
        writer.add_histogram(tag, latent_code.grad, global_step=epoch)

    # Save optimised latent_code
    latent_code_path = os.path.join(test_path, 'latent_code.pt')
    torch.save(best_latent_code, latent_code_path)

    # Extract mesh obtained with the latent code optimised at inference
    coords, grad_size_axis = utils.get_volume_coords(args.resolution)

    sdf = utils.predict_sdf(best_latent_code, coords, model)
    vertices, faces = utils.extract_mesh(grad_size_axis, sdf)

    # save mesh using meshplot
    mesh_path = os.path.join(test_path, f'mesh.html')
    utils.save_meshplot(vertices, faces, mesh_path)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--folder", type=str, default='', help="Folder that contains the network parameters"
    )
    parser.add_argument(
        "--latent_size", type=int, default=128, help="Size of the latent code"
    )
    parser.add_argument(
        "--lr", type=float, default=0.001, help="Initial learning rate"
    )
    parser.add_argument(
        "--epochs", type=int, default=3000, help="Number of epochs"
    )    
    parser.add_argument(
        "--sigma_regulariser", type=float, default=0.01, help="Sigma value for the regulariser in the loss function"
    )
    parser.add_argument(
        "--lr_multiplier", type=float, default=0.5, help="Multiplier for the learning rate scheduling"
    )  
    parser.add_argument(
        "--patience", type=int, default=20, help="Patience for the learning rate scheduling"
    )  
    parser.add_argument(
        "--lr_scheduler", default=False, action='store_true', help="Turn on lr_scheduler"
    )
    parser.add_argument(
        "--num_layers", type=int, default=8, help="Num network layers"
    )    
    parser.add_argument(
        "--no_skip_connections", default=False, action='store_true', help="Do not skip connections"
    ) 
    parser.add_argument(
        "--num_samples", type=int, default=5000, help="Number of points to sample on the object surface"
    )    
    parser.add_argument(
        "--index_objs_dict", type=int, default=-1, help="Index of the object in the dictionary. Set this higher than -1 to sample from a specific object"
    )  
    parser.add_argument(
        "--resolution", type=int, default=50, help="Resolution of the extracted mesh"
    )
    parser.add_argument(
        "--clamp", default=False, action='store_true', help="Clip the network prediction"
    )
    parser.add_argument(
        "--clamp_value", type=float, default=0.1, help="Value of the clip"
    )
    parser.add_argument(
        "--touches", type=int, default=0, help="Simulated touches if this value is higher than 0, otherwise sample uniformly on the surface."
    )
    parser.add_argument(
        "--langevin_noise", type=float, default=0, help="If this value is higher than 0, it adds noise to the latent space after every update."
    )
    parser.add_argument(
        "--optimiser", type=str, default='Adam', help="Choose the optimiser out of [Adam, LBFGS]"
    )
    parser.add_argument(
        "--LBFGS_maxiter", type=int, default=20, help="Maximum interations for the LBFGS optimiser"
    )
    args = parser.parse_args()

    main(args)
