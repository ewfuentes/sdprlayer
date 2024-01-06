#!/bin/bash/python
# boilerplate
import numpy as np
import matplotlib.pyplot as plt
import warnings
import pandas as pd

# SDPRlayer import
from sdprlayer import SDPRLayer
import torch

# Theseus
import theseus as th

# Stereo problem imports
from mwcerts.stereo_problems import (
    Localization,
    skew,
)
import spatialmath.base as sm
from pylgmath.se3.transformation import Transformation as Trans


class Camera:
    def __init__(
        c, f_u=200.0, f_v=200.0, c_u=0.0, c_v=0.0, b=0.05, sigma_u=0.5, sigma_v=0.5
    ):
        c.f_u = f_u
        c.f_v = f_v
        c.c_u = c_u
        c.c_v = c_v
        c.b = b
        c.sigma_u = sigma_u
        c.sigma_v = sigma_v

    def get_intrinsic_mat(c, M=None):
        c.M = np.array(
            [
                [c.f_u, 0.0, c.c_u, 0.0],
                [0.0, c.f_v, 0.0, c.c_v, 0.0],
                [c.f_u, 0.0, c.c_u, -c.f_u * c.b],
                [0.0, c.f_v, 0.0, c.c_v, 0.0],
            ]
        )

    def forward(c, p_inC):
        """forward camera model, points to pixels"""
        z = p_inC[2, :]
        x = p_inC[0, :] / z
        y = p_inC[1, :] / z
        assert all(z > 0), "Negative depth in data"
        # noise
        noise = np.random.randn(4, len(x))
        # pixel measurements
        ul = c.f_u * x + c.c_u + c.sigma_u * noise[0, :]
        vl = c.f_v * y + c.c_v + c.sigma_v * noise[1, :]
        ur = ul - c.f_u * c.b / z + c.sigma_u * noise[2, :]
        vr = vl + c.sigma_v * noise[3, :]

        return ul, vl, ur, vr

    def inverse(c, pixel_meass: torch.Tensor):
        """inverse camera model, pixels to points. Assumes inputs are torch tensors.
        Output meas is a torch tensor of shape (N_batch, 3, N) where N is the number of measurements.
        Output weights is a torch tensor of shape (N_batch, 3, 3, N) where N is the number of measurements.
        """
        # unpack pixel measurements
        ul = pixel_meass[:, 0, :]
        vl = pixel_meass[:, 1, :]
        ur = pixel_meass[:, 2, :]
        vr = pixel_meass[:, 3, :]

        # compute disparity
        d = ul - ur
        # assert torch.all(d >= 0), "Negative disparity in data"
        Sigma = torch.zeros((3, 3))
        # Define pixel covariance
        Sigma[0, 0] = c.sigma_u**2
        Sigma[1, 1] = c.sigma_v**2
        Sigma[2, 2] = 2 * c.sigma_u**2
        Sigma[0, 2] = c.sigma_u**2
        Sigma[2, 0] = c.sigma_u**2

        # compute euclidean measurement coordinates
        ratio = c.b / d
        x = (ul - c.c_u) * ratio
        y = (vl - c.c_v) * ratio * c.f_u / c.f_v
        z = ratio * c.f_u
        meas = torch.stack([x, y, z], dim=1)
        # compute weights
        G = torch.zeros((ul.size(0), ul.size(1), 3, 3), dtype=torch.double)
        # Define G
        G[:, :, 0, 0] = z / c.f_u
        G[:, :, 1, 1] = z / c.f_v
        G[:, :, 0, 2] = -x * z / c.f_u / c.b
        G[:, :, 1, 2] = -y * z / c.f_v / c.b
        G[:, :, 2, 2] = -(z**2) / c.f_u / c.b

        # Covariance matrix (matrix mult last two dims)
        Sigma = Sigma.expand((ul.size(0), ul.size(1), 3, 3))
        Cov = torch.einsum("bnij,bnjk,bnlj->bnil", G, Sigma, G)

        # Check if any of the matrices are not full rank
        ranks = torch.linalg.matrix_rank(Cov)
        if torch.any(ranks < 3):
            warnings.warn("At least one covariance matrix is not full rank")
            Cov = torch.eye(3, dtype=torch.double).expand_as(Cov)
        # Compute weights by inverting covariance matrices
        W = torch.linalg.inv(Cov)
        # Symmetrize
        weights = 0.5 * (W + W.transpose(-2, -1))

        return meas, weights


def get_gt_setup(
    traj_type="circle",  # Trajectory format [clusters,circle]
    N_batch=1,  # Number of poses
    N_map=10,  # number of landmarks
    offs=np.array([[0, 0, 2]]).T,  # offset between poses and landmarks
    n_turns=0.1,  # (circle) number of turns around the cluster
    lm_bound=1.0,  # Bounding box of uniform landmark distribution.
):
    """Used to generate a trajectory of ground truth pose data"""

    # Ground Truth Map Points
    # Cluster at the origin
    r_l = lm_bound * (np.random.rand(3, N_map) - 0.5)
    # Ground Truth Poses
    r_ps = []
    C_p0s = []
    if traj_type == "clusters":
        # Ground Truth Poses
        for i in range(N_batch):
            r_ps += [0.1 * np.random.randn(3, 1)]
            C_p0s += [sm.roty(0.1 * np.random.randn(1)[0])]
        # Offset from the origin
        r_l = r_l + offs
    elif traj_type == "circle":
        # GT poses equally spaced along n turns of a circle
        radius = np.linalg.norm(offs)
        assert radius > 0.2, "Radius of trajectory circle should be larger"
        if N_batch > 1:
            delta_phi = n_turns * 2 * np.pi / (N_batch - 1)
        else:
            delta_phi = n_turns
        phi = delta_phi
        for i in range(N_batch):
            # Location
            r = radius * np.array([[np.cos(phi), np.sin(phi), 0]]).T
            r_ps += [r]
            # Z Axis points at origin
            z = -r / np.linalg.norm(r)
            x = np.array([[0.0, 0.0, 1.0]]).T
            y = skew(z) @ x
            C_p0s += [np.hstack([x, y, z]).T]
            # Update angle
            phi = (phi + delta_phi) % (2 * np.pi)
    r_ps = np.stack(r_ps)
    C_p0s = np.stack(C_p0s)

    return r_ps, C_p0s, r_l


def get_prob_data(camera=Camera(), N_map=30, N_batch=1):
    # get ground truth information
    r_ps, C_p0s, r_l = get_gt_setup(N_map=N_map, N_batch=N_batch)

    # generate measurements
    pixel_meass = []
    r_ls = []
    for i in range(N_batch):
        r_p = r_ps[i]
        C_p0 = C_p0s[i]
        r_ls += [r_l]
        r_l_inC = C_p0 @ (r_l - r_p)
        pixel_meass += [camera.forward(r_l_inC)]
    pixel_meass = np.stack(pixel_meass)
    r_ls = np.stack(r_ls)

    return r_ps, C_p0s, r_ls, pixel_meass


def kron(A, B):
    # kronecker workaround for matrices
    # https://github.com/pytorch/pytorch/issues/74442
    return (A[:, None, :, None] * B[None, :, None, :]).reshape(
        A.shape[0] * B.shape[0], A.shape[1] * B.shape[1]
    )


def get_data_mat(cam_torch: Camera, r_ls, pixel_meass):
    """Get a batch of data matrices for stereo calibration problem."""
    if not isinstance(pixel_meass, torch.Tensor):
        pixel_meass = torch.tensor(pixel_meass)
    # Get euclidean measurements from pixels
    meas, weights = cam_torch.inverse(pixel_meass)
    N_batch = meas.shape[0]
    # Indices
    h = [0]
    c = slice(1, 10)
    t = slice(10, 13)
    Q_batch = []
    for b in range(N_batch):
        Q_es = []
        for i in range(meas.shape[-1]):
            W_ij = weights[b, i]
            m_j0_0 = torch.tensor(r_ls[b, :, [i]].T)
            y_ji_i = meas[b, :, [i]]
            # Define matrix
            Q_e = torch.zeros(13, 13, dtype=torch.double)
            # Diagonals
            Q_e[c, c] = kron(m_j0_0 @ m_j0_0.T, W_ij)
            Q_e[t, t] = W_ij
            Q_e[h, h] = y_ji_i.T @ W_ij @ y_ji_i
            # Off Diagonals
            Q_e[c, t] = -kron(m_j0_0, W_ij)
            Q_e[t, c] = Q_e[c, t].T
            Q_e[c, h] = -kron(m_j0_0, W_ij @ y_ji_i)
            Q_e[h, c] = Q_e[c, h].T
            Q_e[t, h] = W_ij @ y_ji_i
            Q_e[h, t] = Q_e[t, h].T

            # Add to overall matrix
            Q_es += [Q_e]
        Q = torch.stack(Q_es).sum(dim=0)
        # Rescale
        Q[0, 0] = 0.0
        Q = Q / torch.norm(Q, p="fro")
        Q[0, 0] = 1.0
        Q_batch += [Q]

    return torch.stack(Q_batch)


# Loss Function
def get_loss_from_sols(Xs, r_p_in0, C_p0):
    "Get ground truth loss over multiple solutions"
    if Xs.ndim == 2:
        Xs = Xs.unsqueeze(0)
    losses = []
    for i in range(Xs.shape[0]):
        losses += [get_loss_from_sol(Xs[i], r_p_in0[i], C_p0[i])]
    loss = torch.stack(losses).sum()
    return loss


def get_loss_from_sol(X, r_p_in0, C_p0):
    "Get ground truth loss from solution"
    # Check rank
    sorted_eigs = np.sort(np.linalg.eigvalsh(X.detach().numpy()))
    assert sorted_eigs[-1] / sorted_eigs[-2] > 1e4, "X is not rank-1"
    # Convert to tensors
    C_p0 = torch.tensor(C_p0, dtype=torch.float64)
    r_p_in0 = torch.tensor(r_p_in0, dtype=torch.float64)
    # Extract solution (assume Rank-1)
    r = (X[10:, [0]] + X[[0], 10:].T) / 2.0
    C_vec = (X[1:10, [0]] + X[[0], 1:10].T) / 2.0
    C = C_vec.reshape((3, 3)).T
    loss = (
        torch.norm(r - C_p0 @ r_p_in0) ** 2
        + torch.norm(C.T @ C_p0 - torch.eye(3), p="fro") ** 2
    )
    return loss


term_crit_def = {
    "max_iter": 500,
    "tol_grad_sq": 1e-14,
    "tol_loss": 1e-12,
}  # Optimization termination criteria


def get_constraints(r_ps, C_p0s, r_ls):
    """Generate constraints for problem"""
    r_ls_b = [r_ls[0, :, [i]] for i in range(r_ls.shape[2])]
    prob = Localization([r_ps[0]], [C_p0s[0]], r_ls_b)
    prob.generate_constraints()
    prob.generate_redun_constraints()
    constraints = prob.constraints + prob.constraints_r
    constraints_list = [(c.A.get_matrix(prob.var_list), c.b) for c in constraints]
    return constraints_list


def tune_stereo_params_sdpr(
    cam_torch: Camera,
    params,
    opt,
    r_ps,
    C_p0s,
    r_ls,
    pixel_meass,
    term_crit=term_crit_def,
    verbose=False,
    solver="SCS",
):
    # Define a localization class to get the constraints
    constraints_list = get_constraints(r_ps, C_p0s, r_ls)
    # Build Layer
    sdpr_layer = SDPRLayer(13, constraints=constraints_list, use_dual=True)

    # define closure
    def closure_fcn():
        # zero grad
        opt.zero_grad()
        # generate loss
        Q = get_data_mat(cam_torch, r_ls, pixel_meass)
        if solver == "SCS":
            solver_args = {"solve_method": "SCS", "eps": 1e-9}
        elif solver == "mosek":
            solver_args = {"solve_method": "mosek"}
        else:
            raise ValueError("Invalid solver")
        Xs = sdpr_layer(Q, solver_args=solver_args)[0]
        loss = get_loss_from_sols(Xs, r_ps, C_p0s)
        # backprop
        loss.backward()
        return loss

    # Optimization loop
    max_iter = term_crit["max_iter"]
    tol_grad_sq = term_crit["tol_grad_sq"]
    tol_loss = term_crit["tol_loss"]
    grad_sq = np.inf
    n_iter = 0
    loss_stored = []
    iter_info = []
    loss = torch.tensor(np.inf)
    while grad_sq > tol_grad_sq and n_iter < max_iter and loss > tol_loss:
        loss = opt.step(closure_fcn)
        loss_stored += [loss.item()]
        grad = np.vstack([p.grad for p in params])
        grad_sq = np.sum([g**2 for g in grad])
        if verbose:
            print(f"Iter:\t{n_iter}\tLoss:\t{loss_stored[-1]}\tgrad_sq:\t{grad_sq}")
            print(f"Params:\t{params}")
        iter_info += [
            dict(
                params=np.stack([p.detach().numpy() for p in params]),
                loss=loss_stored[-1],
                grad_sq=grad_sq,
                n_iter=n_iter,
            )
        ]
        n_iter += 1

    return pd.DataFrame(iter_info)


def build_theseus_layer(cam_torch: Camera, N_map, N_batch=1):
    """Build theseus layer for stereo problem

    Args:
        cam_torch (Camera): _description_
        r_l (_type_): _description_
        pixel_meas (_type_): _description_
    """
    # Optimization variables
    C_p0s = th.SO3(name="C_p0s")
    r_p0s = th.Point3(name="r_p0s")
    # Auxillary (data) variables (pixel measurements and landmarks)
    pixel_meass = th.Variable(torch.zeros(N_batch, 4, N_map), name="pixel_meass")
    r_ls = th.Variable(torch.zeros(N_batch, 3, N_map), name="r_ls")

    # Define cost function
    def error_fn(optim_vars, aux_vars):
        C_p0s, r_p0s = optim_vars
        pixel_meass, r_ls = aux_vars
        # run inverse cam model on pixels
        meas, weights = cam_torch.inverse(pixel_meass)

        # construct errors
        errors = []
        for j in range(meas.shape[-1]):
            # get measurement
            meas_j = meas[:, :, [j]]
            # get weight
            W_ij = weights[:, j, :, :]
            W_ij_half = torch.linalg.cholesky(W_ij)
            # get measurement in camera frame
            cam_to_lm = r_ls.tensor[:, :, [j]] - r_p0s.tensor.T
            err = meas_j - torch.bmm(C_p0s.tensor, cam_to_lm)
            # get error
            errors += [torch.bmm(W_ij_half, err)]
        error_stack = torch.cat(errors, dim=1)
        return error_stack

    objective = th.Objective()
    optim_vars = [C_p0s, r_p0s]
    aux_vars = [pixel_meass, r_ls]
    cost_function = th.AutoDiffCostFunction(
        optim_vars=optim_vars,
        dim=N_map * 3,
        err_fn=error_fn,
        aux_vars=aux_vars,
        cost_weight=th.ScaleCostWeight(1.0),
        name="registration_cost",
    )
    objective.add(cost_function)

    layer = th.TheseusLayer(th.GaussNewton(objective, max_iterations=10))

    return layer


def tune_stereo_params_theseus(
    cam_torch: Camera,
    params,
    opt,
    r_ps,
    C_p0s,
    r_ls,
    pixel_meass,
    term_crit=term_crit_def,
    verbose=False,
):
    # define closure
    def closure_fcn():
        # zero grad
        opt.zero_grad()
        losses = []
        # Loop over instances
        for i, pixel_meas in enumerate(pixel_meass):
            # generate loss based on landmarks
            meas, weights = cam_torch.inverse(*pixel_meas)
            # Check that measurements are correct (should be exact with no noise)
            meas_gt = torch.tensor(C_p0s[i] @ (np.hstack(r_ls[i]) - r_ps[i]))

            for k in range(meas.shape[1]):
                losses += [
                    (meas[:, [k]] - meas_gt[:, [k]]).T
                    @ weights[k]
                    @ (meas[:, [k]] - meas_gt[:, [k]])
                ]
        loss = torch.stack(losses).sum()
        # backprop
        loss.backward()
        return loss

    # Optimization loop
    max_iter = term_crit["max_iter"]
    tol_grad_sq = term_crit["tol_grad_sq"]
    tol_loss = term_crit["tol_loss"]
    grad_sq = np.inf
    n_iter = 0
    loss_stored = []
    iter_info = []
    loss = torch.tensor(np.inf)
    while grad_sq > tol_grad_sq and n_iter < max_iter and loss > tol_loss:
        loss = opt.step(closure_fcn)
        loss_stored += [loss.item()]
        grad = np.vstack([p.grad for p in params])
        grad_sq = np.sum([g**2 for g in grad])
        if verbose:
            print(f"Iter:\t{n_iter}\tLoss:\t{loss_stored[-1]}\tgrad_sq:\t{grad_sq}")
            print(f"Params:\t{params}")
        iter_info += [
            dict(
                params=np.stack([p.detach().numpy() for p in params]),
                loss=loss_stored[-1],
                grad_sq=grad_sq,
                n_iter=n_iter,
            )
        ]
        n_iter += 1

    return pd.DataFrame(iter_info)


def tune_stereo_params_no_opt(
    cam_torch: Camera,
    params,
    opt,
    r_ps,
    C_p0s,
    r_ls,
    pixel_meass,
    term_crit=term_crit_def,
    verbose=False,
):
    pixel_meass = torch.tensor(pixel_meass)

    # define closure
    def closure_fcn():
        # zero grad
        opt.zero_grad()
        losses = []
        # generate loss based on landmarks
        meas, weights = cam_torch.inverse(pixel_meass)
        # Loop over instances
        for i in range(meas.shape[0]):
            # Get ground truth landmark measurements
            meas_gt = torch.tensor(C_p0s[i] @ (r_ls[i] - r_ps[i]))

            for k in range(meas.shape[-1]):
                losses += [
                    (meas[i, :, [k]] - meas_gt[:, [k]]).T
                    @ weights[i, k, :, :]
                    @ (meas[i, :, [k]] - meas_gt[:, [k]])
                ]
        loss = torch.stack(losses).sum()
        # backprop
        loss.backward()
        return loss

    # Optimization loop
    max_iter = term_crit["max_iter"]
    tol_grad_sq = term_crit["tol_grad_sq"]
    tol_loss = term_crit["tol_loss"]
    grad_sq = np.inf
    n_iter = 0
    loss_stored = []
    iter_info = []
    loss = torch.tensor(np.inf)
    while grad_sq > tol_grad_sq and n_iter < max_iter and loss > tol_loss:
        loss = opt.step(closure_fcn)
        loss_stored += [loss.item()]
        grad = np.vstack([p.grad for p in params])
        grad_sq = np.sum([g**2 for g in grad])
        if verbose:
            print(f"Iter:\t{n_iter}\tLoss:\t{loss_stored[-1]}\tgrad_sq:\t{grad_sq}")
            print(f"Params:\t{params}")
        iter_info += [
            dict(
                params=np.stack([p.detach().numpy() for p in params]),
                loss=loss_stored[-1],
                grad_sq=grad_sq,
                n_iter=n_iter,
            )
        ]
        n_iter += 1

    return pd.DataFrame(iter_info)
