import os
from pickle import dump, load

import matplotlib.pyplot as plt
import numpy as np
import torch
from pandas import DataFrame
from pylgmath.so3.operations import vec2rot

import sdprlayers.utils.stereo_tuner as st
from sdprlayers.utils.plot_tools import make_axes_transparent, make_dirs_safe, savefig
from sdprlayers.utils.stereo_tuner import skew

# Define camera ground truth
cam_gt = st.StereoCamera(
    f_u=484.5,
    f_v=484.5,
    c_u=0.0,
    c_v=0.0,
    b=0.24,
    sigma_u=0.5,
    sigma_v=0.5,
)

torch.set_default_dtype(torch.float64)


def set_seed(seed):
    np.random.seed(seed)
    torch.manual_seed(seed)


def get_points_in_cone(radius, beta, N_batch):
    """Generate N random points in a cone of radius and angle beta"""
    # Generate N random azimuthal angles between 0 and 2*pi
    polar = 2 * np.pi * np.random.rand(N_batch)

    # Generate N random polar angles between 0 and beta
    azimuth = beta * (2 * np.random.rand(N_batch) - 1)

    points = []
    for b in range(N_batch):
        aaxis_z = np.array([[0.0, 0.0, azimuth[b]]]).T
        Cz = vec2rot(aaxis_ba=aaxis_z)
        aaxis_x = np.array([[polar[b], 0.0, 0.0]]).T
        Cx = vec2rot(aaxis_ba=aaxis_x)
        C = Cx @ Cz
        points.append(C[:, [0]] * radius)

    return points


def get_cal_data(
    N_batch=3,  # Number of poses
    N_map=10,  # number of landmarks
    radius=2,  # offset between poses and landmarks
    n_turns=0.2,  # (circle) number of turns around the cluster
    board_dims=np.array([0.3, 0.3]),  # width and height of calibration board
    N_squares=[10, 10],  # number of squares in calibration board (width, height)
    setup="circle",  # setup of GT poses
    cone_angles=(np.pi / 4, np.pi / 4),  # camera FOV (alpha), region cone (beta)
    plot=False,
    plot_pixel_meas=False,
    cam=cam_gt,
):
    """Generate Ground truth pose and landmark data. Also generate pixel measurements"""
    # Ground Truth Map Points
    sq_size = board_dims / np.array(N_squares)
    r_l = []
    for i in range(N_squares[0]):
        for j in range(N_squares[1]):
            r_l += [np.array([[0.0, i * sq_size[0], j * sq_size[1]]]).T]
    r_l = np.hstack(r_l)
    r_l = r_l - np.mean(r_l, axis=1, keepdims=True)
    # Ground Truth Poses
    r_p0s = []
    C_p0s = []
    assert radius > 0.2, "Radius of trajectory circle should be larger"
    if setup == "circle":  # GT poses equally spaced along n turns of a circle
        offs = np.array([[0, 0, radius]]).T
        if N_batch > 1:
            delta_phi = n_turns * 2 * np.pi / (N_batch - 1)
            phi = 0.0
        else:
            delta_phi = n_turns
            phi = delta_phi
        for i in range(N_batch):
            # Location
            r = radius * np.array([[np.cos(phi), 0.0, np.sin(phi)]]).T
            r_p0s += [r]
            # Z Axis points at origin
            z = -r / np.linalg.norm(r)
            y = np.array([[0.0, 1.0, 0.0]]).T
            x = -skew(z) @ y
            C_p0s += [np.hstack([x, y, z]).T]
            # Update angle
            phi = (phi + delta_phi) % (2 * np.pi)
    elif setup == "cone":  # Setup GT poses in a cone
        alpha, beta = cone_angles  # Note FOV divided by 2
        # Pick location
        r_p0s = get_points_in_cone(radius, beta, N_batch)
        # FOV perturbations

        C_p0s = []
        for i in range(N_batch):
            # random orientation pointing at origin
            z = -r_p0s[i] / np.linalg.norm(r_p0s[i])
            y = np.random.randn(3, 1)
            y = y - y.T @ z * z
            y = y / np.linalg.norm(y)
            x = -skew(z) @ y
            C = np.hstack([x, y, z]).T
            # Perturb orientation
            aaxis_x = (2 * np.random.random() - 1) * alpha * np.array([[1, 0.0, 0.0]]).T
            Cx = vec2rot(aaxis_ba=aaxis_x)
            C_p0s += [Cx @ C]

    if plot:
        # Plot data
        fig = plt.figure()
        ax = fig.add_subplot(111, projection="3d")
        plot_poses(C_p0s, r_p0s, ax=ax)
        plot_map(r_l, ax=ax)
        ax.set_xlabel("X")
        ax.set_ylabel("Y")
        ax.set_zlabel("Z")
        r = np.linalg.norm(radius) * 1.1
        ax.set_xlim(-r, r)
        ax.set_ylim(-r, r)
        ax.set_zlim(-r, r)

    # Get pixel measurements
    pixel_meass = []
    r_ls = []
    for i in range(N_batch):
        r_p = r_p0s[i]
        C_p0 = C_p0s[i]
        r_ls += [r_l]
        r_l_inC = C_p0 @ (r_l - r_p)
        pixel_meass += [cam.forward(r_l_inC)]
        if plot_pixel_meas:
            plot_pixel_meas(pixel_meass[-1])
    pixel_meass = torch.tensor(np.stack(pixel_meass))
    r_ls = torch.tensor(np.stack(r_ls))
    C_p0s = torch.tensor(np.stack(C_p0s))
    r_p0s = torch.tensor(np.stack(r_p0s))

    if plot:
        plt.show()

    return r_p0s, C_p0s, r_ls, pixel_meass


def get_random_inits(radius, N_batch=3, plot=False):
    r_p0s = []
    C_p0s = []

    for i in range(N_batch):
        # random locations
        r_ = np.random.random((3, 1)) - 0.5
        r = radius * r_ / np.linalg.norm(r_)
        r_p0s += [r]
        # random orientation pointing at origin
        z = -r / np.linalg.norm(r)
        y = np.random.randn(3, 1)
        y = y - y.T @ z * z
        y = y / np.linalg.norm(y)
        x = -skew(z) @ y
        C_p0s += [np.hstack([x, y, z]).T]

    if plot:
        # Plot data
        fig = plt.figure()
        ax = fig.add_subplot(111, projection="3d")
        plot_poses(C_p0s, r_p0s, ax=ax)
        ax.set_xlabel("X")
        ax.set_ylabel("Y")
        ax.set_zlabel("Z")
        r = radius * 1.1
        ax.set_xlim(-r, r)
        ax.set_ylim(-r, r)
        ax.set_zlim(-r, r)
        plt.show()

    r_p0s = np.stack(r_p0s)
    C_p0s = np.stack(C_p0s)

    return r_p0s, C_p0s


def plot_poses(R_cw, t_cw_w, ax=None, **kwargs):
    if ax is None:
        fig = plt.figure()
        ax = fig.add_subplot(111, projection="3d")
    for i in range(len(R_cw)):
        origin = t_cw_w[i]
        directions = R_cw[i].T

        for j in range(3):
            if "color" in kwargs:
                ax.quiver(*origin, *directions[:, j], **kwargs)
            else:
                ax.quiver(
                    *origin, *directions[:, j], color=["r", "g", "b"][j], **kwargs
                )


def plot_pixel_meas(pixel_meas, cam=cam_gt, ax=None, **kwargs):
    if ax is None:
        fig = plt.figure()
        ax = fig.add_subplot(111)

    for i in range(len(pixel_meas)):
        u, v = pixel_meas[:2]
        ax.plot(u, v, "o", color="r")
        u, v = pixel_meas[2:]
        ax.plot(u, v, "o", color="b")
    plt.axis("equal")
    plt.xlim(-cam.f_u, cam.f_u)
    plt.ylim(-cam.f_v, cam.f_v)


def plot_map(r_l, ax=None, **kwargs):
    if ax is None:
        fig = plt.figure()
        ax = fig.add_subplot(111, projection="3d")

    ax.plot(*r_l, "*", color="k", markersize=2, **kwargs)


def run_sdpr_cal(r_p0s, C_p0s, r_ls, pixel_meass):
    # generate parameterized camera
    cam_torch = st.StereoCamera(
        f_u=torch.tensor(cam_gt.f_u, requires_grad=True),
        f_v=torch.tensor(cam_gt.f_v, requires_grad=True),
        c_u=torch.tensor(cam_gt.c_u, requires_grad=True),
        c_v=torch.tensor(cam_gt.c_v, requires_grad=True),
        b=torch.tensor(cam_gt.b, requires_grad=True),
        sigma_u=cam_gt.sigma_u,
        sigma_v=cam_gt.sigma_v,
    )
    params = [cam_torch.b]
    # Set up optimizer
    opt = torch.optim.Adam(params, lr=1e-3)

    # Termination criteria
    term_crit = {"max_iter": 2000, "tol_grad_sq": 1e-12, "tol_loss": 1e-12}

    # Run Tuner
    iter_info = st.tune_stereo_params_sdpr(
        cam_torch=cam_torch,
        params=params,
        opt=opt,
        term_crit=term_crit,
        r_p=r_p0s,
        C_p0=C_p0s,
        r_l=r_ls,
        pixel_meas=pixel_meass,
        verbose=True,
    )


def find_local_minima(N_inits=100, store_data=False, check=False, **kwargs):
    set_seed(5)
    radius = 3
    # Generate data
    r_p0s, C_p0s, r_ls, pixel_meass = get_cal_data(
        radius=radius,
        board_dims=[1.0, 1.0],
        N_squares=[8, 8],
        N_batch=1,
        plot=False,
        setup="cone",
        **kwargs,
    )
    r_p0s = torch.tensor(r_p0s)
    C_p0s = torch.tensor(C_p0s)
    r_ls = torch.tensor(r_ls)
    pixel_meass = torch.tensor(pixel_meass)
    N_map = r_ls.shape[2]
    # Convert to tensor

    # generate parameterized camera
    cam_torch = st.StereoCamera(
        f_u=torch.tensor(cam_gt.f_u, requires_grad=True),
        f_v=torch.tensor(cam_gt.f_v, requires_grad=True),
        c_u=torch.tensor(cam_gt.c_u, requires_grad=True),
        c_v=torch.tensor(cam_gt.c_v, requires_grad=True),
        b=torch.tensor(cam_gt.b, requires_grad=True),
        sigma_u=cam_gt.sigma_u,
        sigma_v=cam_gt.sigma_v,
    )
    # Get theseus layer
    theseus_opts = {
        "abs_err_tolerance": 1e-10,
        "rel_err_tolerance": 1e-8,
        "max_iterations": 300,
        "step_size": 0.2,
    }
    layer = st.build_theseus_layer(
        N_map=N_map, N_batch=N_inits, opt_kwargs_in=theseus_opts
    )
    # invert the camera measurements
    meas, weights = cam_torch.inverse(pixel_meass)
    # Generate random initializations
    r_p0s_init, C_p0s_init = get_random_inits(
        radius=radius, N_batch=N_inits, plot=False
    )
    r_p0s_init = torch.tensor(r_p0s_init)
    C_p0s_init = torch.tensor(C_p0s_init)

    with torch.no_grad():
        # Set initializations and measurements
        theseus_inputs = {
            "C_p0s": C_p0s_init,
            "r_p0s": r_p0s_init[:, :, 0],
            "r_ls": r_ls,
            "meas": meas,
            "weights": weights,
        }
        # Run theseus
        out, info = layer.forward(
            theseus_inputs,
            optimizer_kwargs={
                "track_best_solution": True,
                "verbose": True,
                "backward_mode": "implicit",
            },
        )
        # get optimal solutions
        C_sols = out["C_p0s"]
        r_sols = out["r_p0s"]
        # record optimal costs
        losses = info.best_err.detach().numpy()
    C_sols = C_sols.detach().numpy()
    r_sols = r_sols.detach().numpy()
    # Show loss distribution
    plt.figure()
    plt.hist(losses)
    plt.xlabel("Loss")
    # Plot final solutions
    fig = plt.figure()
    ax = fig.add_subplot(111, projection="3d")
    plot_poses(C_sols, r_sols, ax=ax, alpha=0.2)
    plot_poses(C_p0s, r_p0s, ax=ax, color="k")
    plot_map(r_ls[0].detach().numpy(), ax=ax, alpha=0.5)

    ax.set_xlabel("X")
    ax.set_ylabel("Y")
    ax.set_zlabel("Z")
    r = radius * 1.1
    ax.set_xlim(-r, r)
    ax.set_ylim(-r, r)
    ax.set_zlim(-r, r)

    # Find local minima
    loss_min = np.min(losses)
    ind_local = np.where(np.abs(losses - loss_min) > 5)[0]
    ind_global = np.where(np.abs(losses - loss_min) < 5)[0]

    r_p0s_init_l = r_p0s_init[ind_local]
    C_p0s_init_l = C_p0s_init[ind_local]
    r_p0s_init_g = r_p0s_init[ind_global]
    C_p0s_init_g = C_p0s_init[ind_global]

    # Double check
    if check:
        with torch.no_grad():
            # Set initializations and measurements
            theseus_inputs = {
                "C_p0s": C_p0s_init_l,
                "r_p0s": r_p0s_init_l[:, :, 0],
                "r_ls": r_ls,
                "meas": meas,
                "weights": weights,
            }
            # Run theseus
            assert len(r_p0s_init_l) > 0, "No local minima found"
            out, info = layer.forward(
                theseus_inputs,
                optimizer_kwargs={
                    "track_best_solution": True,
                    "verbose": True,
                    "backward_mode": "implicit",
                },
            )
            assert torch.all(
                info.best_err > 120.0
            ), "Local minima should have higher loss"

    # Store data
    if store_data:
        prob_data = dict(
            cam_torch=cam_torch,
            r_p0s=r_p0s,
            C_p0s=C_p0s,
            r_ls=r_ls,
            pixel_meass=pixel_meass,
            r_p0s_init_l=r_p0s_init_l,
            C_p0s_init_l=C_p0s_init_l,
            r_p0s_init_g=r_p0s_init_g,
            C_p0s_init_g=C_p0s_init_g,
            C_sols=C_sols,
            r_sols=r_sols,
        )
        folder = os.path.dirname(os.path.realpath(__file__))
        folder = os.path.join(folder, "outputs")
        dump(prob_data, open(folder + "/stereo_cal_local_min_init.pkl", "wb"))


def intialization_plots():
    """Generate initialization and setup plots"""
    folder = os.path.dirname(os.path.realpath(__file__))
    folder = os.path.join(folder, "outputs")
    data = load(open(folder + "/stereo_cal_local_min_init.pkl", "rb"))
    C_p0s = data["C_p0s"]
    r_p0s = data["r_p0s"]
    r_ls = data["r_ls"]
    C_p0s_init_l = data["C_p0s_init_l"]
    r_p0s_init_l = data["r_p0s_init_l"]
    C_p0s_init_g = data["C_p0s_init_g"]
    r_p0s_init_g = data["r_p0s_init_g"]
    C_sols = data["C_sols"]
    r_sols = data["r_sols"]

    fig_localmin = plt.figure(figsize=(10, 10))
    ax = fig_localmin.add_subplot(111, projection="3d")
    plot_poses(C_sols, r_sols, ax=ax, color="orange", alpha=0.8)
    plot_poses(C_p0s, r_p0s, ax=ax, color="magenta", linewidth=5)
    plot_map(r_ls[0].detach().numpy(), ax=ax)
    plot_poses(C_p0s_init_l, r_p0s_init_l, ax=ax, color="r", alpha=0.7)
    plot_poses(C_p0s_init_g, r_p0s_init_g, ax=ax, color="g", alpha=0.7)
    # plt.title("Local (red) and Global (green) Initializations")
    radius = 3
    r = radius * 0.9
    ax.set_xlim(-r, r)
    ax.set_ylim(-r, r)
    ax.set_zlim(-r, r)
    # ax.axis("equal")
    ax.axis("auto")
    make_axes_transparent(ax)

    fig_setup = plt.figure(figsize=(10, 10))
    ax = fig_setup.add_subplot(111, projection="3d")
    plot_map(r_ls[0].detach().numpy(), ax=ax)
    plot_poses(C_p0s, r_p0s, ax=ax)
    make_axes_transparent(ax)
    r = radius * 0.7
    ax.set_xlim(-r, r)
    ax.set_ylim(-r, r)
    ax.set_zlim(-r, r)
    plt.show()
    savefig(fig_localmin, "stereo_cal_local_min_init.png", dpi=400)
    savefig(fig_setup, "stereo_setup.png", dpi=400)


# BATCH TUNING COMPARISON


def tune_baseline(
    tuner="spdr",
    opt_select="sgd",
    b_offs=0.01,
    gt_init=False,
    N_batch=20,
    radius=3,
    prob_data=(),
    term_crit={},
):
    # Get problem data
    r_p0s, C_p0s, r_ls, pixel_meass = prob_data
    N_map = r_ls.shape[2]
    # termination criteria
    default_term_crit = {
        "max_iter": 15,
        "tol_grad_sq": 1e-8,
        "tol_loss": 1e-10,
    }
    default_term_crit.update(term_crit)
    term_crit = default_term_crit
    # generate parameterized camera
    cam_torch = st.StereoCamera(
        f_u=torch.tensor(cam_gt.f_u, requires_grad=True),
        f_v=torch.tensor(cam_gt.f_v, requires_grad=True),
        c_u=torch.tensor(cam_gt.c_u, requires_grad=True),
        c_v=torch.tensor(cam_gt.c_v, requires_grad=True),
        b=torch.tensor(cam_gt.b + b_offs, requires_grad=True),
        sigma_u=cam_gt.sigma_u,
        sigma_v=cam_gt.sigma_v,
    )

    # Define parameter to tune
    params = [cam_torch.b]
    # Define optimizer
    if opt_select == "sgd":
        opt = torch.optim.SGD(params, lr=1.0e-4)
    elif opt_select == "adam":
        opt = torch.optim.Adam(params, lr=1e-3)
    # Run Tuner
    if tuner == "spdr":
        iter_info = st.tune_stereo_params_sdpr(
            cam_torch=cam_torch,
            params=params,
            opt=opt,
            term_crit=term_crit,
            r_p0s_gt=r_p0s,
            C_p0s_gt=C_p0s,
            r_ls=r_ls,
            pixel_meass=pixel_meass,
            diff_qcqp=False,
            verbose=True,
        )
    elif tuner == "sdpr-dq":
        iter_info = st.tune_stereo_params_sdpr(
            cam_torch=cam_torch,
            params=params,
            opt=opt,
            term_crit=term_crit,
            r_p0s_gt=r_p0s,
            C_p0s_gt=C_p0s,
            r_ls=r_ls,
            pixel_meass=pixel_meass,
            diff_qcqp=True,
            verbose=True,
        )
    elif "theseus" in tuner:
        # Generate random initializations
        if gt_init:
            r_p0s_init = r_p0s.clone()
            C_p0s_init = C_p0s.clone()
        else:  # random init
            r_p0s_init, C_p0s_init = get_random_inits(
                radius=radius, N_batch=N_batch, plot=False
            )
            r_p0s_init = torch.tensor(r_p0s_init)
            C_p0s_init = torch.tensor(C_p0s_init)
        # opt parameters
        opt_kwargs = {
            "abs_err_tolerance": 1e-10,
            "rel_err_tolerance": 1e-6,
            "max_iterations": 500,
            "step_size": 0.2,
        }
        # Run Tuner
        iter_info = st.tune_stereo_params_theseus(
            cam_torch=cam_torch,
            params=params,
            opt=opt,
            term_crit=term_crit,
            r_p0s_gt=r_p0s,
            C_p0s_gt=C_p0s,
            r_ls=r_ls,
            pixel_meass=pixel_meass,
            r_p0s_init=r_p0s_init,
            C_p0s_init=C_p0s_init,
            verbose=True,
            opt_kwargs=opt_kwargs,
        )
    else:
        raise ValueError("tuner unknown!")

    return iter_info


def compare_tune_baseline(N_batch=20, N_runs=10, mode="prob_data"):
    """Compare tuning of baseline parameters with SDPR and Theseus.
    Run N_runs times with N_batch poses"""
    offset = 0.003  # offset for init baseline
    radius = 3
    opt_select = "sgd"  # optimizer to use
    # termination criteria
    term_crit = {
        "max_iter": 150,
        "tol_grad_sq": 1e-5,
        "tol_loss": 1e-10,
    }
    # Folder name
    folder = os.path.dirname(os.path.realpath(__file__))
    folder = os.path.join(folder, "outputs")
    offset_str = str(offset).replace(".", "p")
    folder = os.path.join(
        folder, f"str_tune_b{offset_str}_{opt_select}_{N_batch}b_{N_runs}r"
    )

    info = []
    if mode == "prob_data":
        for i in range(N_runs):
            print(f"Run {i+1} of {N_runs}: Gen Data")
            # Generate problem data
            prob_data = get_cal_data(
                radius=radius,
                board_dims=[1.0, 1.0],
                N_squares=[8, 8],
                N_batch=N_batch,
                setup="cone",
                plot=False,
            )
            info.append(prob_data)
    elif mode == "sdpr":
        info_p = load(open(folder + "/stereo_tune_prob_data.pkl", "rb"))
        for i in range(N_runs):
            # Run tuners
            print(f"Run {i+1} of {N_runs}: SDPR")
            info.append(
                tune_baseline(
                    "spdr",
                    opt_select=opt_select,
                    b_offs=offset,
                    N_batch=N_batch,
                    prob_data=info_p[i],
                    term_crit=term_crit,
                )
            )
    elif mode == "sdpr-dq":
        info_p = load(open(folder + "/stereo_tune_prob_data.pkl", "rb"))
        for i in range(N_runs):
            # Run tuners
            print(f"Run {i+1} of {N_runs}: SDPR-DiffQCQP")
            info.append(
                tune_baseline(
                    "sdpr-dq",
                    opt_select=opt_select,
                    b_offs=offset,
                    N_batch=N_batch,
                    prob_data=info_p[i],
                    term_crit=term_crit,
                )
            )
    elif mode == "theseus_gt":
        info_p = load(open(folder + "/stereo_tune_prob_data.pkl", "rb"))
        for i in range(N_runs):
            print(f"Run {i+1} of {N_runs}: Theseus (gt init)")
            info.append(
                tune_baseline(
                    "theseus",
                    opt_select=opt_select,
                    b_offs=offset,
                    gt_init=True,
                    N_batch=N_batch,
                    prob_data=info_p[i],
                    term_crit=term_crit,
                )
            )
    elif mode == "theseus_rand":
        info_p = load(open(folder + "/stereo_tune_prob_data.pkl", "rb"))
        for i in range(N_runs):
            # Need try clause because can sometimes diverge
            try:
                print(f"Run {i+1} of {N_runs}: Theseus (rand init)")
                res = tune_baseline(
                    "theseus",
                    opt_select=opt_select,
                    b_offs=offset,
                    gt_init=False,
                    N_batch=N_batch,
                    prob_data=info_p[i],
                    term_crit=term_crit,
                )
            except:
                print(f"Run {i+1} failed with random init")
                res = None
            # append
            info.append(res)

    # Save data
    filename = folder + f"/stereo_tune_{mode}.pkl"
    make_dirs_safe(filename)
    dump(
        info,
        open(
            filename,
            "wb",
        ),
    )


def compare_tune_baseline_pp(fname="str_tune_b0p003_sgd_20b_50r", ind=0):
    # Load data
    folder = os.path.join("_scripts/outputs", fname)

    info_s = load(open(folder + "/stereo_tune_sdpr-dq.pkl", "rb"))[ind]
    info_tl = load(open(folder + "/stereo_tune_theseus_rand.pkl", "rb"))[ind]
    info_tg = load(open(folder + "/stereo_tune_theseus_gt.pkl", "rb"))[ind]
    # info_tl = load(open(folder + "/stereo_tune_sdpr.pkl", "rb"))[ind]

    # Plot loss
    fig, axs = plt.subplots(2, 2, figsize=(10, 10))
    axs[0, 0].plot(info_tl["loss"], label="Theseus (rand init)")
    axs[0, 0].plot(info_tg["loss"], label="Theseus (gt init)")
    axs[0, 0].plot(info_s["loss"], label="SDPR")
    axs[0, 0].set_yscale("log")
    axs[0, 0].set_title("Outer Loss")
    axs[0, 0].legend()

    # process inner losses
    info_tl["loss_inner_sum"] = info_tl["loss_inner"].apply(lambda x: np.sum(x))
    info_tg["loss_inner_sum"] = info_tg["loss_inner"].apply(lambda x: np.sum(x))

    axs[1, 0].plot(info_tl["grad_sq"], label="Theseus (rand init)")
    axs[1, 0].plot(info_tg["grad_sq"], label="Theseus (gt init)")
    axs[1, 0].plot(info_s["grad_sq"], label="SDPR")
    axs[1, 0].set_title("Gradient Squared")
    axs[1, 0].set_xlabel("Iteration")
    axs[1, 0].set_yscale("log")
    axs[1, 0].legend()

    # Plot parameter values
    axs[0, 1].plot(info_tl["params"], label="Theseus (rand init)")
    axs[0, 1].plot(info_tg["params"], label="Theseus (gt init)")
    axs[0, 1].plot(info_s["params"], label="SDPR")
    axs[0, 1].axhline(cam_gt.b, color="k", linestyle="--", label="Actual Value")
    axs[0, 1].set_title("Baseline Error to GT")
    axs[0, 1].legend()

    # Inner loop optimization time
    axs[1, 1].plot(info_tl["time_inner"], label="Theseus (rand init)")
    axs[1, 1].plot(info_tg["time_inner"], label="Theseus (gt init)")
    axs[1, 1].plot(info_s["time_inner"], label="SDPR")
    axs[1, 1].set_yscale("log")
    axs[1, 1].legend()
    axs[1, 1].set_title("Inner Optimization Time")
    axs[1, 1].set_xlabel("Iteration")
    axs[1, 1].set_ylabel("Time (s)")
    plt.tight_layout()
    plt.show()


def plot_converged_vals(filename="compare_tune_b0p003_batch.pkl", ind=0):
    # Load data
    folder = os.path.dirname(os.path.realpath(__file__))
    folder = os.path.join(folder, "outputs")

    data = load(open(folder + "/" + filename, "rb"))
    # info_s = data["info_s"][ind]
    # info_tl = data["info_tl"][ind]
    info_tg = data["info_tg"][ind]
    info_p = data["info_p"][ind]

    # Plot final solutions
    r_p0s, C_p0s = info_tg.iloc[-1]["solution"]
    r_p0s = r_p0s.detach().numpy()
    C_p0s = C_p0s.detach().numpy()
    r_p0s_gt = info_p["r_p0s"].detach().numpy()
    C_p0s_gt = info_p["C_pos"].detach().numpy()

    # Plot final solutions
    fig = plt.figure()
    ax = fig.add_subplot(111, projection="3d")
    plot_poses(C_p0s_gt, r_p0s_gt, ax=ax, color="k")
    plot_poses(C_p0s, r_p0s, ax=ax)
    ax.set_xlabel("X")
    ax.set_ylabel("Y")
    ax.set_zlabel("Z")
    r = 3 * 1.1
    ax.set_xlim(-r, r)
    ax.set_ylim(-r, r)
    ax.set_zlim(-r, r)

    plt.show()


def get_statistics(fname="str_tune_b0p003_sgd_20b_50r"):
    """Generate statisitics tables for baseline tuning"""
    # Load data
    folder = os.path.dirname(os.path.realpath(__file__))
    folder = os.path.join(folder, "outputs")
    folder = os.path.join(folder, fname)

    info_s = load(open(folder + "/stereo_tune_sdpr-dq.pkl", "rb"))
    info_tl = load(open(folder + "/stereo_tune_theseus_rand.pkl", "rb"))
    info_tg = load(open(folder + "/stereo_tune_theseus_gt.pkl", "rb"))
    b_true = cam_gt.b
    # Get data arrays
    n_runs = len(info_s)
    param = np.zeros((3, n_runs))
    n_iters = np.zeros((3, n_runs))
    t_iter = np.zeros((3, n_runs))
    loss = np.zeros((3, n_runs))
    loss_inner = np.zeros((3, n_runs))
    for i in range(n_runs):
        param[0, i] = info_s[i]["params"].iloc[-1]
        param[1, i] = info_tl[i]["params"].iloc[-1]
        param[2, i] = info_tg[i]["params"].iloc[-1]
        n_iters[0, i] = info_s[i].shape[0]
        n_iters[1, i] = info_tl[i].shape[0]
        n_iters[2, i] = info_tg[i].shape[0]
        t_iter[0, i] = np.mean(info_s[i]["time_inner"].values)
        t_iter[1, i] = np.mean(info_tl[i]["time_inner"].values)
        t_iter[2, i] = np.mean(info_tg[i]["time_inner"].values)
        loss[0, i] = info_s[i]["loss"].values[-1]
        loss[1, i] = info_tl[i]["loss"].values[-1]
        loss[2, i] = info_tg[i]["loss"].values[-1]

    # Get stats
    param_err_mean = np.mean(param - b_true, axis=1)
    param_err_std = np.std(param - b_true, axis=1)
    n_iters_mean = np.mean(n_iters, axis=1)
    n_iters_std = np.std(n_iters, axis=1)
    t_iter_mean = np.mean(t_iter, axis=1)
    loss_mean = np.mean(loss, axis=1)
    desc = ["SDPR", "Theseus (rand init)", "Theseus (gt init)"]

    # Make dataframe

    # df = DataFrame(
    #     {
    #         "Method": desc,
    #         "Final Baseline (avg)": param_err_mean,
    #         "Final Baseline (std)": param_err_std,
    #         "Number of Iterations (avg)": n_iters_mean,
    #         "Number of Iterations (std)": n_iters_std,
    #         "Avg Time per Iter": t_iter_mean,
    #         "Outer Loss (avg)": loss_mean,
    #     }
    # )
    df = DataFrame(
        {
            "Method": desc,
            "Final Baseline (avg)": param_err_mean,
            "Final Baseline (std)": param_err_std,
            "Number of Iterations (avg)": n_iters_mean,
            "Outer Loss (avg)": loss_mean,
        }
    )
    # df.style.format(precision=3)
    print("Results:")
    print(df)
    print("Latex:")
    print(df.to_latex(float_format="{:0.3e}".format))


def baseline_param_plots(fname="str_tune_b0p003_sgd_20b_50r"):
    """Plots all parameter trajectories for baseline tuning"""
    # Load data
    folder = os.path.dirname(os.path.realpath(__file__))
    folder = os.path.join(folder, "outputs")
    folder = os.path.join(folder, fname)

    info_s = load(open(folder + "/stereo_tune_sdpr-dq.pkl", "rb"))
    info_tl = load(open(folder + "/stereo_tune_theseus_rand.pkl", "rb"))
    info_tg = load(open(folder + "/stereo_tune_theseus_gt.pkl", "rb"))
    err_init = 0.003
    # Get data arrays
    n_runs = len(info_s)

    def plot_figure():
        for i in range(n_runs):
            if i == 0:
                label1 = "SDPRLayer"
                label2 = "Theseus (rand init)"
                label3 = "Theseus (gt init)"
            else:
                label1 = "_SDPRLayer"
                label2 = "_Theseus (rand init)"
                label3 = "_Theseus (gt init)"
            alpha = 0.5
            linewidth = 1
            p_s = np.vstack([err_init, np.vstack(info_s[i]["params"].values) - 0.24])
            p_tl = np.vstack([err_init, np.vstack(info_tl[i]["params"].values) - 0.24])
            p_tg = np.vstack([err_init, np.vstack(info_tg[i]["params"].values) - 0.24])
            plt.plot(p_tl, color="r", alpha=alpha, linewidth=linewidth, label=label2)
            plt.plot(p_tg, color="b", alpha=alpha, linewidth=linewidth, label=label3)
            plt.plot(p_s, color="g", alpha=alpha, linewidth=linewidth, label=label1)

    fsize = 7
    fig = plt.figure(figsize=(5, 4))
    plot_figure()
    plt.legend(loc="upper right")
    plt.grid(True)
    plt.ylabel("Parameter Error")
    plt.xlabel("Iteration")
    plt.tight_layout()
    plt.show()
    # save
    savefig(fig, "baseline_param_err.png", dpi=500)
    # zoom into region of interest
    fig = plt.figure(figsize=(5, 2))
    plot_figure()
    plt.grid(True)
    plt.ylabel("Parameter Error")
    plt.xlabel("Iteration")
    plt.ylim([-2e-3, 4e-3])
    plt.xlim([0, 60])
    plt.tight_layout()
    plt.show()
    savefig(fig, "baseline_param_err_zoom.png", dpi=500)


def plot_grad_innerloss(fname="str_tune_b0p003_sgd_20b_50r", ind=0):
    # Load data
    folder = os.path.dirname(os.path.realpath(__file__))
    folder = os.path.join(folder, "outputs")
    folder = os.path.join(folder, fname)

    info_s = load(open(folder + "/stereo_tune_sdpr-dq.pkl", "rb"))[ind]
    info_tl = load(open(folder + "/stereo_tune_theseus_rand.pkl", "rb"))[ind]
    info_tg = load(open(folder + "/stereo_tune_theseus_gt.pkl", "rb"))[ind]
    # process inner losses
    info_tl["loss_inner_avg"] = info_tl["loss_inner"].apply(lambda x: np.mean(x))
    info_tg["loss_inner_avg"] = info_tg["loss_inner"].apply(lambda x: np.mean(x))
    info_s["loss_inner_avg"] = info_s["loss_inner"]  # already averaged

    fig, axs = plt.subplots(2, 1, figsize=(6, 4))
    axs[0].plot(info_tl["loss_inner_avg"], label="Theseus (rand init)")
    axs[0].plot(info_tg["loss_inner_avg"], label="Theseus (gt init)")
    axs[0].plot(info_s["loss_inner_avg"], label="SDPRLayer")
    axs[0].set_yscale("log")
    axs[0].set_ylabel("Inner Loss\n(Batch Avg)")
    axs[0].grid(True)
    axs[0].tick_params(
        axis="x",  # changes apply to the x-axis
        which="both",  # both major and minor ticks are affected
        bottom=False,  # ticks along the bottom edge are off
        top=False,  # ticks along the top edge are off
        labelbottom=False,
    )  # labels along the bottom edge are off
    axs[0].legend()

    # plot gradient
    axs[1].plot(info_tl["grad_sq"].apply(np.sqrt), label="Theseus (rand init)")
    axs[1].plot(info_tg["grad_sq"].apply(np.sqrt), label="Theseus (gt init)")
    axs[1].plot(info_s["grad_sq"].apply(np.sqrt), label="SDPRLayer")
    axs[1].set_ylabel("Gradient (Mag.)")
    axs[1].set_xlabel("Iteration")
    axs[1].set_yscale("log")
    axs[1].grid(True)
    plt.tight_layout()
    # Move plots closer together
    plt.subplots_adjust(hspace=0.1)
    plt.show()

    savefig(fig, "baseline_grad_innerloss.png", dpi=500)


def baseline_noise_analysis(N_batch=20, N_runs=10):
    """Compare tuning of baseline parameters with SDPR and Theseus.
    Run N_runs times with N_batch poses at each noise level"""
    noise_lvls = np.logspace(-3, 0, 5)
    offset = 0.003  # offset for init baseline
    n_iters = 100  # Number of max outer iterations
    opt_select = "sgd"  # optimizer to use
    # termination criteria
    term_crit = {
        "max_iter": 100,
        "tol_grad_sq": 1e-6,
        "tol_loss": 1e-10,
    }

    info_p, info_s, info_tg, info_tl = [], [], [], []
    noise_lvl = []
    set_seed(0)
    for noise in noise_lvls:
        cam_gt.sigma_u = noise
        cam_gt.sigma_v = noise
        print(f"NOISE LEVEL: {noise}")
        for i in range(N_runs):
            # Record noise value
            noise_lvl.append(noise)
            # Generate data
            print("__________________________________________________________")
            print(f"Run {i+1} of {N_runs}: Gen Data")
            radius = 3
            r_p0s, C_p0s, r_ls, pixel_meass = get_cal_data(
                radius=radius,
                board_dims=[0.6, 1.0],
                N_squares=[8, 8],
                N_batch=N_batch,
                setup="cone",
                plot=False,
            )
            info_p.append(
                dict(r_p0s=r_p0s, C_p0s=C_p0s, r_map=r_ls, pixel_meass=pixel_meass)
            )
            prob_data = (r_p0s, C_p0s, r_ls, pixel_meass)
            # Run tuners
            print(f"Run {i+1} of {N_runs}: SDPR")
            info_s.append(
                tune_baseline(
                    "spdr",
                    opt_select=opt_select,
                    b_offs=offset,
                    N_batch=N_batch,
                    prob_data=prob_data,
                    term_crit=term_crit,
                )
            )
            print(f"Run {i+1} of {N_runs}: Theseus (gt init)")
            info_tg.append(
                tune_baseline(
                    "theseus",
                    opt_select=opt_select,
                    b_offs=offset,
                    gt_init=True,
                    N_batch=N_batch,
                    prob_data=prob_data,
                    term_crit=term_crit,
                )
            )
            print(f"Run {i+1} of {N_runs}: Theseus (rand init)")
            info_tl.append(
                tune_baseline(
                    "theseus",
                    opt_select=opt_select,
                    b_offs=offset,
                    gt_init=False,
                    N_batch=N_batch,
                    prob_data=prob_data,
                    term_crit=term_crit,
                )
            )

    # Save data
    data = dict(
        noise_lvl=noise_lvl,
        info_p=info_p,
        info_s=info_s,
        info_tg=info_tg,
        info_tl=info_tl,
    )
    folder = os.path.dirname(os.path.realpath(__file__))
    folder = os.path.join(folder, "outputs")
    offset_str = str(offset).replace(".", "p")
    dump(
        data,
        open(
            folder
            + f"/baseline_noise_{offset_str}o_{opt_select}_{N_batch}b_{N_runs}r.pkl",
            "wb",
        ),
    )


if __name__ == "__main__":

    # Generation of calibration data
    # r_p0s, C_p0s, r_ls, pixel_meass = get_cal_data(
    #     setup="cone", cone_angles=(0.0, np.pi / 4), N_batch=100, plot=True
    # )
    # r_p0s, C_p0s, r_ls, pixel_meass = get_cal_data(
    #     setup="cone", cone_angles=(np.pi / 4, 0.0), N_batch=100, plot=True
    # )
    # r_p0s, C_p0s, r_ls, pixel_meass = get_cal_data(
    #     setup="cone", cone_angles=(np.pi / 4, np.pi / 4), N_batch=100, plot=True
    # )

    # Local minimum search and setup plots
    find_local_minima(store_data=True)
    intialization_plots()

    # Comparison over multiple instances (batch):
    # Run these to generate all of the data for comparison. Will
    # populate outputs folder.

    # compare_tune_baseline(N_batch=20, N_runs=50, mode="prob_data")
    # compare_tune_baseline(N_batch=20, N_runs=50, mode="spdr")
    # compare_tune_baseline(N_batch=20, N_runs=50, mode="sdpr-dq")
    # compare_tune_baseline(N_batch=20, N_runs=50, mode="theseus_gt")
    # compare_tune_baseline(N_batch=20, N_runs=50, mode="theseus_rand")

    # Post Processing scripts:
    # compare_tune_baseline_pp(ind=0)
    # get_statistics()
    # baseline_param_plots()
    # plot_grad_innerloss()

    # Noise analysis (This was not really used)
    # baseline_noise_analysis(N_batch=20, N_runs=10)

    # Testing
    # n_batch = 20
    # compare_tune_baseline(N_batch=n_batch, N_runs=1, mode="prob_data")
    # compare_tune_baseline(N_batch=n_batch, N_runs=1, mode="sdpr")
    # compare_tune_baseline(N_batch=n_batch, N_runs=1, mode="theseus_gt")
    # compare_tune_baseline(N_batch=n_batch, N_runs=1, mode="sdpr-dq")
    # compare_tune_baseline_pp(fname="str_tune_b0p003_sgd_20b_1r")
