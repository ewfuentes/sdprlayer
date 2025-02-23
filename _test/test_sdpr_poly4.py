import os

import cvxpy as cp
import matplotlib.pylab as plt
import numpy as np
import scipy.sparse as sp
import torch
from scipy.optimize import lsq_linear

from sdprlayers import SDPRLayer, SDPRLayerMosek

root_dir = os.path.abspath(os.path.dirname(__file__) + "/../")


def get_prob_data():
    # Define polynomial (lowest order first)
    p_vals = np.array([2, 2, -0.5, -2 / 3, 1 / 4])

    constraints = []
    A = sp.csc_array((3, 3))  # x^2 = x*x
    A[2, 0] = 1 / 2
    A[0, 2] = 1 / 2
    A[1, 1] = -1
    constraints += [A]

    # Candidate solution
    x_cand = np.array([[1.0000, -1.4871, 2.2115, -3.2888]]).T

    return dict(p_vals=p_vals, constraints=constraints, x_cand=x_cand)


def plot_polynomial(p_vals):
    x = np.linspace(-2.5, 2, 100)
    y = np.polyval(p_vals[::-1], x)
    plt.plot(x, y)


# Define Q tensor from polynomial parameters (there must be a better way to do this)
def build_data_mat(p):
    Q_tch = torch.zeros((3, 3), dtype=torch.double)
    Q_tch[0, 0] = p[0]
    Q_tch[[1, 0], [0, 1]] = p[1] / 2
    Q_tch[[2, 1, 0], [0, 1, 2]] = p[2] / 3
    Q_tch[[2, 1], [1, 2]] = p[3] / 2
    Q_tch[2, 2] = p[4]

    return Q_tch


def local_solver(p: torch.Tensor, x_init=0.0):
    # Detach parameters
    p_vals = p.cpu().detach().double().numpy()
    # Simple gradient descent solver
    grad_tol = 1e-12
    max_iters = 200
    n_iter = 0
    alpha = 1e-2
    grad_sq = np.inf
    x = x_init
    while grad_sq > grad_tol and n_iter < max_iters:
        # compute polynomial gradient
        p_deriv = np.array([p * i for i, p in enumerate(p_vals)])[1:]
        grad = np.polyval(p_deriv[::-1], x)
        grad_sq = grad**2
        # Descend
        x = x - alpha * grad
    # Convert to expected vector form
    x_hat = np.array([1, x, x**2])[:, None]
    return x_hat


def certifier(objective, constraints, x_cand):
    """compute lagrange multipliers and certificate given candidate solution"""
    Q = objective
    q = (Q @ x_cand).flatten()
    Ax = np.hstack([A @ x_cand for A, b in constraints])
    # Compute Multipliers
    res = lsq_linear(Ax, q, tol=1e-12)
    mults = res.x
    # Compute Certificate - diffcp assumes the form:  H = Q - A*mult
    H = Q - np.sum([mults[i] * A for i, (A, b) in enumerate(constraints)])
    return H, mults


def test_prob_sdp(display=False):
    """The goal of this script is to shift the optimum of the polynomial
    to a different point by using backpropagtion on rank-1 SDPs"""
    np.random.seed(2)
    # Get data from data function
    data = get_prob_data()
    constraints = data["constraints"]

    # Create SDPR Layer
    optlayer = SDPRLayer(n_vars=3, constraints=constraints)

    # Set up polynomial parameter tensor
    p = torch.tensor(data["p_vals"], requires_grad=True)

    # Define loss
    def gen_loss(p_val, x_target=-0.5, **kwargs):
        sdp_solver_args = {"eps": 1e-9}
        sol, x = optlayer(build_data_mat(p_val), solver_args=sdp_solver_args)
        loss = 1 / 2 * (sol[1, 0] - x_target) ** 2
        return loss, sol

    # Define Optimizer
    opt = torch.optim.Adam(params=[p], lr=1e-2)
    # Execute iterations
    losses = []
    max_iter = 1000
    X_init = None
    n_iter = 0
    loss_val = np.inf
    while loss_val > 1e-4 and n_iter < max_iter:
        # Update Loss
        opt.zero_grad()
        loss, sol = gen_loss(p)
        if n_iter == 0:
            X_init = sol.cpu().detach().numpy()
        # run optimizer
        loss.backward(retain_graph=True)
        opt.step()
        loss_val = loss.item()
        losses.append(loss_val)
        x_min = sol.detach().numpy()[0, 1]
        n_iter += 1
        if display:
            print(f"min:\t{x_min}\tloss:\t{losses[-1]}")
    if display:
        print(f"ITERATIonS: \t{n_iter}")
    # Check the rank of the solution
    X_new = sol.detach().numpy()
    evals_new = np.sort(np.linalg.eigvalsh(X_new))[::-1]
    evr_new = evals_new[0] / evals_new[1]
    if display:
        print(f"New Eigenvalue Ratio:\t{evr_new}")

    if display:
        plt.figure()
        plot_polynomial(p_vals=data["p_vals"])
        plot_polynomial(p_vals=p.detach().numpy())
        plt.axvline(x=X_init[0, 1], color="r", linestyle="--")
        plt.axvline(x=X_new[0, 1], color="b", linestyle="--")
        plt.legend(["initial poly", "new poly", "initial argmin", "new argmin"])
        plt.show()

    # # Check that nothing has changed
    # assert n_iter == 93, ValueError("Number of iterations was expected to be 93")
    # np.testing.assert_almost_equal(loss_val, 9.4637779e-5, decimal=9)
    # np.testing.assert_almost_equal(evr_new, 96614772541.3, decimal=1)


def test_grad_num(autograd_test=True, use_dual=True):
    """The goal of this script is to test the dual formulation of the SDPRLayer"""
    # Get data from data function
    data = get_prob_data()
    constraints = data["constraints"]

    # Set up polynomial parameter tensor
    p = torch.tensor(data["p_vals"], requires_grad=True)

    # Create SDPR Layer
    sdpr_args = dict(n_vars=3, constraints=constraints, use_dual=use_dual)
    optlayer = SDPRLayer(**sdpr_args)

    # Define loss
    def gen_loss(p_val, **kwargs):
        x_target = -0.5
        sol, x = optlayer(build_data_mat(p_val), **kwargs)
        x_val = (sol[1, 0] + sol[0, 1]) / 2
        loss = 1 / 2 * (x_val - x_target) ** 2
        return loss, sol

    # arguments for sdp solver
    sdp_solver_args = {"eps": 1e-9}

    # Check gradient w.r.t. parameter p
    if autograd_test:
        res = torch.autograd.gradcheck(
            lambda *x: gen_loss(*x, solver_args=sdp_solver_args)[0],
            [p],
            eps=1e-4,
            atol=1e-4,
            rtol=1e-3,
        )
        assert res is True

    # Manually compute and compare gradients
    stepsize = 1e-6
    # Compute Loss
    loss, sol = gen_loss(p, solver_args=sdp_solver_args)
    loss_init = loss.detach().numpy().copy()
    # Compute gradient
    loss.backward()
    grad_computed = p.grad.numpy().copy()
    # Get current parameter value
    p_val_init = p.cpu().detach().numpy().copy()
    # Compute gradients
    delta_loss = np.zeros(p_val_init.shape)
    for i in range(len(delta_loss)):
        delta_p = np.zeros(p_val_init.shape)
        delta_p[i] = stepsize
        p_val = torch.tensor(p_val_init + delta_p, requires_grad=True)
        loss, sol = gen_loss(p_val, solver_args=sdp_solver_args)
        loss_curr = loss.detach().numpy().copy()
        delta_loss[i] = loss_curr - loss_init
    grad_num = delta_loss / stepsize
    # check gradients
    np.testing.assert_allclose(grad_computed, grad_num, atol=1e-6, rtol=0)


def test_grad_qcqp_cost(use_dual=True):
    """Test SDPRLayer with MOSEK as the solver"""
    # Get data from data function
    data = get_prob_data()
    constraints = data["constraints"]

    # TEST COST GRADIENTS
    # Set up polynomial parameter tensor
    p = torch.tensor(data["p_vals"], requires_grad=True)
    # Mosek Parameters
    tol = 1e-12
    mosek_params = {
        "MSK_IPAR_INTPNT_MAX_ITERATIONS": 500,
        "MSK_DPAR_INTPNT_CO_TOL_PFEAS": tol,
        "MSK_DPAR_INTPNT_CO_TOL_REL_GAP": tol,
        "MSK_DPAR_INTPNT_CO_TOL_MU_RED": tol,
        "MSK_DPAR_INTPNT_CO_TOL_INFEAS": tol,
        "MSK_DPAR_INTPNT_CO_TOL_DFEAS": tol,
    }
    # Create SDPR Layer
    optlayer = SDPRLayerMosek(
        n_vars=3,
        constraints=constraints,
        use_dual=use_dual,
        diff_qcqp=True,
        compute_multipliers=False,
        mosek_params=mosek_params,
    )

    # Define loss
    # NOTE: we skip the derivative wrt p_0 since it should be identically zero. Numerical issues cause it to be different.
    p_0 = p[0]

    def gen_loss(p_val, **kwargs):
        p_vals = torch.hstack([p_0, p_val])
        X, x = optlayer(build_data_mat(p_vals), **kwargs)
        x_target = -1
        x_val = x[1, 0]
        loss = 1 / 2 * (x_val - x_target) ** 2
        return x[1:]

    # Check gradient w.r.t. parameter p
    torch.autograd.gradcheck(
        lambda *x: gen_loss(*x),
        [p[1:]],
        eps=1e-4,
        atol=1e-10,
        rtol=5e-3,
    )


def test_grad_qcqp_constraints(use_dual=True, n_batch=3):
    """Test diff through qcqp in SDPRLayer with MOSEK as the solver.
    Differentiate wrt constraints."""
    # Get data from data function
    data = get_prob_data()
    constraints = data["constraints"]

    # Make first constraint a parameter
    constraint_val = constraints[0].copy()
    constraints[0] = None
    # Fix the cost
    p = torch.tensor(data["p_vals"], requires_grad=False)
    Q = build_data_mat(p)
    # Mosek params
    tol = 1e-12
    mosek_params = {
        "MSK_IPAR_INTPNT_MAX_ITERATIONS": 500,
        "MSK_DPAR_INTPNT_CO_TOL_PFEAS": tol,
        "MSK_DPAR_INTPNT_CO_TOL_REL_GAP": tol,
        "MSK_DPAR_INTPNT_CO_TOL_MU_RED": tol,
        "MSK_DPAR_INTPNT_CO_TOL_INFEAS": tol,
        "MSK_DPAR_INTPNT_CO_TOL_DFEAS": tol,
    }
    # Create SDPR Layer
    optlayer = SDPRLayerMosek(
        n_vars=3,
        objective=Q,
        constraints=constraints,
        use_dual=use_dual,
        diff_qcqp=True,
        compute_multipliers=False,
        mosek_params=mosek_params,
    )

    def gen_loss_constraint(constraint, **kwargs):
        x_target = -1
        X, x = optlayer(constraint, **kwargs)
        x_val = x[:, 1, 0]
        loss = 1 / 2 * (x_val - x_target) ** 2
        return x[:, 1:]

    c_val = torch.tensor(constraint_val.toarray(), requires_grad=True)
    # add batch dimension
    c_val = c_val.repeat(n_batch, 1, 1)

    torch.autograd.gradcheck(
        lambda *x: gen_loss_constraint(*x),
        [c_val],
        eps=1e-3,
        atol=1e-10,
        rtol=1e-2,
    )


if __name__ == "__main__":
    # test_prob_sdp()
    # test_grad_num()
    # test_grad_local()
    test_grad_qcqp_constraints()
    test_grad_qcqp_cost()
