from copy import deepcopy

import cvxpy as cp
import matplotlib.pyplot as plt
import numpy as np
import scipy.linalg as la
import scipy.sparse as sp
import sparseqr as sqr
import torch
from cvxpylayers.torch import CvxpyLayer
from diffcp import cones

# GLOBAL PARAMETERS
mosek_params_dflt = {
    "MSK_IPAR_INTPNT_MAX_ITERATIONS": 1000,
    "MSK_DPAR_INTPNT_CO_TOL_PFEAS": 1e-10,
    "MSK_DPAR_INTPNT_CO_TOL_REL_GAP": 1e-10,
    "MSK_DPAR_INTPNT_CO_TOL_MU_RED": 1e-12,
    "MSK_DPAR_INTPNT_CO_TOL_INFEAS": 1e-10,
    "MSK_DPAR_INTPNT_CO_TOL_DFEAS": 1e-10,
}

# When recomputing the lagrange multipliers, tolerance is the smallest singular value of the
# constraint gradient matrix that is treated as non-zero.
LICQ_TOL = 1e-7
# Tolerance for norm of residuals of the KKT conditions.
ATOL_KKT = 1e-5
# Tolerance for residuals in LSQR solve
# (see https://docs.scipy.org/doc/scipy/reference/generated/scipy.sparse.linalg.lsqr.html#lsqr)
LSQR_TOL = 1e-10
# Minimum Eigenvalue Ratio for Tightness Check.
ER_MIN = 1e5


class SDPRLayer(CvxpyLayer):
    """
    This class represents a differentiable, semidefinite relaxation layer for
    non-convex QCQPs. The forward function call returns the (differentiable)
    solution to the QCQP.

    """

    def __init__(
        self,
        n_vars,
        constraints,
        objective=None,
        homogenize=False,
        use_dual=True,
        diff_qcqp=True,
        compute_multipliers=False,
        licq_tol=LICQ_TOL,
        lsqr_tol=LSQR_TOL,
        redun_list=[],
    ):
        """Initialize the SDPRLayer class. This functions sets up the SDP relaxation
        using CVXPY, adding in parameters to be filled in later during forward function
        call. If homogenize input is True, it is assumed that the problem is in the standard
        non-convex, non-homogenized form.

        min x^T F x + fvec^T x + f
        s.t. x^T G_i x + gvec_i^T x + g_i = 0

        These matrices are then converted into homogenized form (below).
        If homogenize flag is set False then it is assumed that the problem is already in
        homogenized form:

        min x^T Q x
        s.t. x^T A_i x = 0,
             x^T A_0 x = 1,

        NOTE: The homogenization variable is assumed to be at the first position of x.
        NOTE: The homogenization constraint is assumed to come last.
        If any of the objective or constraints are meant to be parameterized, then they should
        be set to None. Otherwise, they should be set to the fixed values.

        Args:
            n_vars (int): dimension of variable vector x
            constraints (list): list of constraints, either 3-tuple or matrices
            objective (tuple or array): objective function, either 3-tuple or matrix
            homogenize (boolean): defaults to False. If true, constraints and objective are converted to matrices from 3-tuples
            use_dual=True,
        """
        # Store information
        self.homogenize = homogenize
        self.constr_list = constraints
        self.use_dual = use_dual
        self.diff_qcqp = diff_qcqp
        self.redun_list = redun_list
        self.compute_multipliers = compute_multipliers
        self.licq_tol = licq_tol
        self.lsqr_tol = lsqr_tol
        # Add homogenization variable
        if homogenize:
            n_vars = n_vars + 1
        self.n_vars = n_vars
        # parameter list (for cvxpylayers)
        # NOTE: parameters are the optimization matrices and are always
        # stored in homogenized form
        params = []
        # objective matrix
        if objective is None:
            Q = cp.Parameter((n_vars, n_vars), symmetric=True)
            params += [Q]
        elif self.homogenize:
            assert (
                type(objective) is tuple
            ), "objective input must be tuple if homogenize flag is active"
            Q = self.homog_matrix(*objective)
        else:
            Q = objective
        # Store objective matrix (for use in certifier)
        self.Q = Q

        # check constraints set to None are asssumed to be parameterized
        for iConstr in range(len(constraints)):
            if self.constr_list[iConstr] is None:  # Parameterized constraint
                self.constr_list[iConstr] = cp.Parameter(
                    (n_vars, n_vars), symmetric=True
                )
                # Add constraint to the list of parameters
                params += [self.constr_list[iConstr]]
            else:  # Fixed Constraint
                if self.homogenize:
                    assert (
                        type(self.constr_list[iConstr]) is tuple
                    ), "constraint must be list of tuples if homogenize flag is active"
                    self.constr_list[iConstr] = self.homog_matrix(
                        *self.constr_list[iConstr]
                    )
                else:  # otherwise constraint is already set up properly
                    continue

        # Set Standard Formulation (Homogenized SDP)
        # If using local solver then must use dual formulation to avoid
        # definition of extra slacks by CVXPY
        N_constrs = len(self.constr_list)
        # homenization constraint matrix
        self.A_0 = sp.lil_array((n_vars, n_vars))
        self.A_0[0, 0] = 1.0
        if use_dual is not None:
            y = cp.Variable(shape=(N_constrs + 1,))
            rho = y[-1]
            objective = cp.Maximize(rho)
            LHS = cp.sum([y[i] * Ai for (i, Ai) in enumerate(self.constr_list)])
            LHS += rho * self.A_0
            constraint = LHS << Q
            problem = cp.Problem(objective, [constraint])
            variables = []
            constraints_ = [constraint]
            self.H = Q - LHS
        else:
            # NOTE: CVXPY adds new constraints when canonicalizing if
            # the problem is defined using the primal form.
            X = cp.Variable((n_vars, n_vars), symmetric=True)
            constraints = [X >> 0]
            for A in self.constr_list:
                constraints += [cp.trace(A @ X) == 0.0]
            constraints += [cp.trace(self.A_0 @ X) == 1.0]
            objective_cvx = cp.Minimize(cp.trace(Q @ X))
            problem = cp.Problem(objective=objective_cvx, constraints=constraints)
            variables = [X]
            constraints_ = []
        assert problem.is_dpp()
        # store problem and parameters
        self.problem = problem
        self.parameters = params
        assert len(params) > 0, ValueError("No parameters defined")
        # Call CvxpyLayers init
        super(SDPRLayer, self).__init__(
            problem=problem,
            variables=variables,
            constraints=constraints_,
            parameters=params,
        )

    def forward(self, *param_vals, **kwargs):
        """Solve problem (or a batch of problems) corresponding to param_vals
        Args:
          param_vals: a sequence of torch Tensors. If the "homogenize" flag was
                  set to true for the problem, then these tensors are assumed
                  to come in triplets that define the parameterized objective
                  and constraints. That is,
                    param_vals = F, fvec, f, G_1, gvec_1, g_1, ... , G_m, gvec_m, g_m
                  If the homoginize flag is set to False for the problem,
                  then these tensors must be the homogenized objective and
                  constraint matrices. That is,
                    param_vals = Q, A_1, ..., A_m
                  These Tensors can have either 2 or 3 dimenstions. If a
                  Tensor has 3 dimensions, then its first dimension is
                  interpreted as the batch size. These Tensors must all have
                  the same dtype and device.
          kwargs: key word arguments to be passed into the cvxpylayer. For
                  example, `solver_args'

        Returns:
          a list of optimal variable values, one for each CVXPY Variable
          supplied to the constructor.

        """
        # homogenize if required
        if self.homogenize:
            assert len(self.param_ids) * 3 == len(
                param_vals
            ), "Expected 3 inputs per parameter to homogenize constraints"
            param_vals_h = []
            ind = 0
            while ind < len(param_vals):
                # Unpack
                mat, vec, const = param_vals[ind : ind + 3]
                # check dimensions
                ndims = mat.ndim
                assert (
                    ndims == vec.ndim and ndims == const.ndim
                ), "Inputs must be 2 or 3 dimensional"
                # check batch dimension
                if ndims > 2:
                    if ind == 0:  # get batch dimension
                        N_batch = mat.shape[0]
                    assert (
                        mat.shape[0] == N_batch
                        and vec.shape[0] == N_batch
                        and const.shape[0] == N_batch
                    ), "Inconsistent batch dimesion"
                    # Homogenize
                    param_vals_h += [torch.vmap(self.homog_matrix)(mat, vec, const)]
                else:
                    param_vals_h += [self.homog_matrix(mat, vec, const)]
                # Increment index
                ind += 3
        else:  # problem already homogenized
            param_vals_h = list(param_vals)
            if len(param_vals_h) > 0:
                # Check dimensions and ensure consistency
                ndims = param_vals_h[0].ndim
                if ndims > 2:
                    N_batch = param_vals_h[0].shape[0]
                else:
                    N_batch = 1
                for i in range(len(param_vals_h)):
                    assert (
                        param_vals_h[i].ndim == ndims
                    ), "Parameter dimensions inconsistent"
                    if ndims > 2:
                        assert (
                            param_vals_h[i].shape[0] == N_batch
                        ), "Inconsistent batch dimension"
                    else:
                        param_vals_h[i] = param_vals_h[i].unsqueeze(0)

        # Make input parameters symmetric
        param_vals_h = [make_symmetric(param_val) for param_val in param_vals_h]

        # Define new kwargs to not affect original
        kwargs_new = deepcopy(kwargs)

        # This section constructs a solution using an 'external' solver (MOSEK or
        # user-provided local solver). The solution is then injected into diffcp to
        # compute the gradients.
        if "solver_args" in kwargs and "solve_method" in kwargs["solver_args"]:
            method = kwargs["solver_args"]["solve_method"]
            # Check if we are injecting a solution
            if method == "mosek":
                assert self.use_dual, "Primal not implemented. Set use_dual=True"
                # TODO this loop should be set up so that we can run in parallel.
                ext_vars_list = []
                for iBatch in range(N_batch):
                    # Populate CVXPY Parameters with batch values
                    parameters = self.problem.parameters()
                    for iParam in range(len(parameters)):
                        parameters[iParam].value = (
                            param_vals_h[i][iBatch].cpu().detach().numpy()
                        )
                    # Solve the problem
                    assert "MOSEK" in cp.installed_solvers(), "MOSEK not installed"
                    # Get parameters for mosek
                    verbose = kwargs["solver_args"].get("verbose", False)
                    mosek_params = kwargs["solver_args"].get(
                        "mosek_params", mosek_params_dflt
                    )
                    self.problem.solve(
                        solver=cp.MOSEK, verbose=verbose, mosek_params=mosek_params
                    )
                    # Solver check
                    if not self.problem.status == "optimal":
                        raise ValueError("MOSEK did not converge")
                    # Extract primal and dual variables
                    X = np.array(self.problem.constraints[0].dual_value)
                    H = np.array(self.H.value)
                    mults = self.problem.variables()[0].value

                    # Add to list of solutions
                    ext_vars_list += [
                        dict(
                            x=mults,
                            y=cones.vec_symm(X),
                            s=cones.vec_symm(H),
                        )
                    ]

                # Update solver arguments (copy required here)
                solver_args = dict(solve_method="external", ext_vars_list=ext_vars_list)
                if "solver_args" in kwargs_new:
                    kwargs_new["solver_args"].update(solver_args)
                else:
                    kwargs_new["solver_args"] = solver_args

        # QCQP Backpropagation
        if self.diff_qcqp:
            # Get CvxpyLayers to return diffcp solution
            kwargs_new["solver_args"]["ret_diffcp_soln"] = True
            # Get torch tensor from CvxpyLayers
            soln = super().forward(*param_vals_h, **kwargs_new)
            Xs = soln[0]
            # Extract solutions
            xs = self.recovery_map(Xs)
            # Lagrange multipliers - sign is flipped since cvxpy uses Ax+s=b (Ay+H=Q) conic canonical form
            mults = [-vals for vals in soln[1]]
            # NOTE: unclear why the slack var (certificate) is always the same output, but may
            # have to do with the way the problem is canonicalized.
            if self.use_dual:
                hs = soln[3]
            else:
                hs = soln[3]
            # Unvectorize certificate matrices
            Hs = [cones.unvec_symm(h, self.n_vars) for h in hs]
            # Check that the whole batch is tight.
            alltight = True
            for X in Xs:
                tight, ER = self.check_tightness(X)
                if not tight:
                    alltight = False
                    break
            # If using nonconvex backprop, overwrite solution IF all problems are tight.
            if alltight:
                # Overwrite solution using QCQP autograd function
                constraints = self.constr_list + [
                    self.A_0
                ]  # add homogenizing constraint
                # Call Differentiable QCQP function
                qcqp_func = _QCQPDiffFn(
                    xs,
                    Hs,
                    mults,
                    self.Q,
                    constraints,
                    self.n_vars,
                    self.redun_list,
                    self.licq_tol,
                    self.compute_multipliers,
                    self.lsqr_tol,
                )
                xs = qcqp_func(*param_vals_h)
            else:
                xs = None
        else:
            # Get torch tensor from CvxpyLayers
            soln = super().forward(*param_vals_h, **kwargs_new)
            Xs = soln[0]
            # Check that the whole batch is tight.
            alltight = True
            for X in Xs:
                tight, ER = self.check_tightness(X)
                if not tight:
                    alltight = False
                    break
            if alltight:
                # Extract solutions
                xs = self.recovery_map(Xs)
            else:
                xs = None

        # Adjust dimensions
        if ndims < 3:
            if xs is not None:
                xs = xs.squeeze(0)
            Xs = Xs.squeeze(0)

        return Xs, xs

    @staticmethod
    def recovery_map(Xs, method="column"):
        """Extract the rank-1 solution from the SDP matrix solution"""
        # Expand dimension if not batched.
        if Xs.ndim == 2:
            Xs = Xs.unsqueeze(0)
        # recovery vector QCQP variable
        if method == "column":
            round_func = torch.vmap(SDPRLayer.extract_column)
        elif method == "eig":
            round_func = torch.vmap(SDPRLayer.eig_round)
        else:
            raise ("Solution recovery function unknown.")

        return round_func(Xs)

    @staticmethod
    def homog_matrix(F, fvec, f):
        """Convert quadratic function to homogenized form (matrix)"""
        if torch.is_tensor(F) and torch.is_tensor(fvec) and torch.is_tensor(f):
            Q_left = torch.vstack([f, 0.5 * fvec])
            Q_right = torch.vstack([0.5 * fvec.T, F])
            Q = torch.hstack([Q_left, Q_right])
        else:
            Q = np.block([[f, 0.5 * fvec.T], [0.5 * fvec, F]])

        return Q

    @staticmethod
    def check_tightness(X, ER_min=ER_MIN):
        if torch.is_tensor(X):
            X = X.detach().cpu().numpy()
        # Check rank
        sorted_eigs = np.sort(np.linalg.eigvalsh(X))
        sorted_eigs = np.abs(sorted_eigs)
        ER = sorted_eigs[-1] / sorted_eigs[-2]
        tight = ER > ER_min
        return tight, ER

    @staticmethod
    def extract_column(X):
        """Assumes that the homogenized variable corresponds to the first row/col"""
        x = (X[0:, [0]] + X[[0], 0:].T) / 2.0
        return x

    @staticmethod
    def eig_round(X):
        """Use eigenvalue decomposition to extract the best solution"""
        vals, vecs = torch.linalg.eigh(X)
        x = vecs[:, -1] * torch.sqrt(vals[-1])
        return x

    @staticmethod
    def find_constraints(feas_samples, tolerance=1e-5):
        """Find all possible constraints given a list of sampled values from
        the feasible set. This function is a simplified version of AutoTight
        introduced by Duembgen et al. in "Toward Globally Optimal State Estimation
        Using Automatically Tightened Semidefinite Relaxations".
        Note that it may be more efficient to use the AutoTemplate method for
        larger SDPs.

        Args:
            feas_samples (list): a list of 1d numpy arrays of feasible samples
            tolerance (float): tolerance on singular values considered part of nullspace
        Returns:
            constraints (list): A (maximal) list of constraints for the problem.
            Note that these constraints are always returned in homogenized form
        """
        # Convert samples to lifted form
        Y = []
        for x in feas_samples:
            # homogenize samples if not done already
            if not x[0] == 1.0:
                x = np.append(1.0, x)
            # lift the sample into PSD space
            x_lift = cones.vec_symm(x[:, None] @ x[None, :])
            Y += [x_lift]
        Y = np.vstack(Y)
        dim = len(x)
        # Find nullspace basis
        basis, info = get_nullspace(Y, tolerance=tolerance)
        # Convert basis to constraint list
        constraints = [cones.unvec_symm(b, dim) for b in basis]

        return constraints


def make_symmetric(X):
    return (X + X.transpose(-1, -2)) / 2


def _QCQPDiffFn(
    xs,
    Hs,
    mults,
    objective,
    constraints,
    nvars,
    redundant_inds,
    licq_tol,
    compute_multipliers,
    lsqr_tol,
):
    class DiffQCQP(torch.autograd.Function):
        @staticmethod
        def forward(ctx, *params):
            """Forward function is basically a dummy to store the required information for implicit backward pass."""
            # keep track of which parts of the problem are parameterized
            param_dict = dict(objective=False, constraints=[])
            param_ind = 0
            # Add objective
            if isinstance(objective, cp.Parameter):
                ctx.objective = params[0].detach().cpu().numpy()
                param_dict["objective"] = True
                param_ind += 1
            else:
                ctx.objective = objective.detach().cpu().numpy()

            # Add Constraints
            constraint_mats = []
            for iConstr, A in enumerate(constraints):
                if isinstance(A, cp.Parameter):
                    # Add parameter value to constraint list
                    A_val = params[param_ind].detach().cpu().numpy()
                    constraint_mats.append(A_val)
                    param_dict["constraints"].append(iConstr)
                    param_ind += 1
                else:
                    # Add to constraint value to list
                    constraint_mats.append(A)
            # Store constraints and redundant constraint list
            ctx.constraints = constraint_mats
            ctx.redundant_inds = redundant_inds
            # Check that all parameters have been used
            assert param_ind == len(params), ValueError(
                "All parameters have not been used in QCQP Forward!"
            )
            ctx.param_dict = param_dict

            # Store solution and certificate matrix
            ctx.xs = xs.detach().cpu().numpy()
            ctx.Hs = Hs
            ctx.mults = mults
            # Store parameters
            ctx.licq_tol = licq_tol  # Relative tolerance for constraint removal to satisfy LICQ in QCQP differentiation
            ctx.compute_multipliers = compute_multipliers  # Flag to recompute lagrange multipliers of non-redundant constraints.
            ctx.lsqr_tol = lsqr_tol  # Tolerance for LSQR residual
            return xs

        @staticmethod
        def backward(ctx, grad_output):
            """Compute gradients by implicit differentiation of the QCQP KKT conditions."""
            device = grad_output.device
            grad_output = grad_output.cpu()
            # Certified Solution
            xs = ctx.xs
            batch_dim = xs.shape[0]
            # NOTE: should reimplement the following without a loop (vectorized)
            # Loop through batches and compute gradient information
            dH_bar, dA_quad, dy_bar_2_list, mult_list = [], [], [], []
            for b in range(batch_dim):
                x = xs[b]
                # Construct the constraint gradients
                G = []  # All constraint gradients
                G_r = []  # Linearly independent constraint gradients
                for iConst, A in enumerate(ctx.constraints):
                    if len(A.shape) > 2:
                        A = A[b]
                    c_grad = x.T @ A
                    G.append(c_grad)
                    if iConst not in ctx.redundant_inds:
                        G_r.append(c_grad)
                G = np.vstack(G)
                G_r = np.vstack(G_r)

                # Solve for multipliers
                if ctx.compute_multipliers or ctx.mults is None:
                    # Get Objective
                    if len(ctx.objective.shape) > 2:
                        Q = ctx.objective[b]
                    else:
                        Q = ctx.objective
                    q_bar = Q @ x

                    # Solve for Lagrange Multipliers (for all constraints)
                    res = np.linalg.lstsq(G_r.T, -q_bar, rcond=ctx.licq_tol)
                    mults = res[0]
                    rank = res[2]

                    # Construct Certificate matrix and set redundant Lagrange multipliers to zero
                    H_list = [sp.csc_array(Q)]
                    all_mults = []
                    cnt = 0
                    for i, A in enumerate(ctx.constraints):
                        # Only add non-redundant constraints
                        if i not in ctx.redundant_inds:
                            all_mults.append(mults[cnt, 0])
                            cnt += 1
                        else:
                            all_mults.append(0.0)
                        if len(A.shape) > 2:
                            A = A[b]
                        H_list.append(A * all_mults[i])
                    H_r = sum(H_list).toarray()
                    assert np.linalg.norm(H_r @ x) < ATOL_KKT, ValueError(
                        "KKT conditions cannot be satisfied! Try increasing the tolerance for the LICQ condition constraint removal."
                    )
                    mult_list.append([all_mults])
                    # Construct Jacobian Matrix Function
                    M = make_jac_linop(H=H_r, G=G_r, G_r=G_r)
                    # Pad incoming gradient (derivative of loss wrt multipliers is zero)
                    dz_bar = np.vstack([-grad_output[b], np.zeros((G_r.shape[0], 1))])
                else:
                    # Get certificate
                    H = Hs[b]
                    # Construct Jacobian Matrix Function
                    M = make_jac_linop(H=H, G=G, G_r=G_r)
                    # Pad incoming gradient (derivative of loss wrt multipliers is zero)
                    dz_bar = np.vstack([-grad_output[b], np.zeros((G.shape[0], 1))])
                # Backprop to KKT RHS
                ls_sol = sp.linalg.lsqr(
                    M.T, dz_bar, atol=ctx.lsqr_tol, btol=ctx.lsqr_tol
                )
                dy_bar = ls_sol[0][:, None]
                dy_bar_1 = dy_bar[:nvars, :]
                # Fill with zeros at redundant entries
                dy_bar_2 = []
                cnt = 0
                for i, val in enumerate(ctx.constraints):
                    if i in ctx.redundant_inds:
                        dy_bar_2.append(0.0)
                    else:
                        dy_bar_2.append(dy_bar[nvars + cnt, 0])
                        cnt += 1
                dy_bar_2_list.append(dy_bar_2)
                # backprop to H
                dH_bar.append(2 * x @ dy_bar_1.T)
                # Compute grad ( x^T A x )
                dA_quad.append(x @ x.T)
            # Stack batch dim
            dH_bar = np.stack(dH_bar, axis=0)
            dA_quad = np.stack(dA_quad, axis=0)
            dy_bar_2 = np.stack(dy_bar_2_list, axis=0)
            if ctx.compute_multipliers or ctx.mults is None:
                mults = np.stack(mult_list, axis=0)
            else:
                mults = np.stack(ctx.mults, axis=0)
            # Set up dims for broadcast
            mults = mults[:, :, None]
            dy_bar_2 = dy_bar_2[:, :, None]
            # Compute gradients
            param_grads = []
            if ctx.param_dict["objective"]:
                param_grads.append(torch.tensor(dH_bar))
            for ind in ctx.param_dict["constraints"]:
                if ind in ctx.redundant_inds:
                    raise ValueError(
                        "Cannot compute derivative of redundant constraint"
                    )
                else:
                    dA = dH_bar * mults[:, [ind], :] + dA_quad * dy_bar_2[:, [ind], :]
                    param_grads.append(torch.tensor(dA))
            # Push back to original device
            param_grads = [grad.to(device) for grad in param_grads]
            return tuple(param_grads)

    return DiffQCQP.apply


def make_jac_linop(H, G, G_r):
    """Construct Linear Operator corresponding to KKT Jacobian
    NOTE: This operator is symmetric.
    The equivalent matrix is as follows:
    M = 2 * np.block([[H, G.T], [G_r, zero_blk]])
    """
    # Concatenate upper blocks of matrix
    nvars = H.shape[0]

    # Define matrix vector product
    def matvec(x):
        if len(x.shape) < 2:
            x = x[:, None]
        return 2 * np.vstack([H @ x[:nvars] + G.T @ x[nvars:], G_r @ x[:nvars]])

    # Define adjoint (same since symmetric)
    def rmatvec(x):
        if len(x.shape) < 2:
            x = x[:, None]
        return 2 * np.vstack([H @ x[:nvars] + G_r.T @ x[nvars:], G @ x[:nvars]])

    shape = (H.shape[0] + G_r.shape[0], H.shape[1] + G.shape[0])
    linop = sp.linalg.LinearOperator(shape=shape, matvec=matvec, rmatvec=rmatvec)
    return linop


def get_nullspace(A_dense, method="qrp", tolerance=1e-5):
    """Function for finding the sparse nullspace basis of a given matrix"""
    info = {}

    if method != "qrp":
        print("Warning: method other than qrp is not recommended.")

    if method == "svd":
        U, S, Vh = np.linalg.svd(
            A_dense
        )  # nullspace of A_dense is in last columns of V / last rows of Vh
        rank = np.sum(np.abs(S) > tolerance)
        basis = Vh[rank:, :]
    elif method == "qr":
        # if A_dense.T = QR, the last n-r columns
        # of R make up the nullspace of A_dense.
        Q, R = np.linalg.qr(A_dense.T)
        S = np.abs(np.diag(R))
        sorted_idx = np.argsort(S)[::-1]
        S = S[sorted_idx]
        rank = np.where(S < tolerance)[0][0]
        # decreasing order
        basis = Q[:, sorted_idx[rank:]].T
    elif method == "qrp":
        # Based on Section 5.5.5 "Basic Solutions via QR with Column Pivoting" from Golub and Van Loan.
        # assert A_dense.shape[0] >= A_dense.shape[1], "only tall matrices supported"
        Q, R, P = la.qr(A_dense, pivoting=True, mode="economic")
        np.testing.assert_almost_equal(Q @ R - A_dense[:, P], 0)

        S = np.abs(np.diag(R))
        rank = np.sum(S > tolerance)
        R1 = R[:rank, :]
        R11, R12 = R1[:, :rank], R1[:, rank:]
        # [R11  R12]  @  [R11^-1 @ R12] = [R12 - R12]
        # [0    0 ]       [    -I    ]    [0]
        N = np.vstack([la.solve_triangular(R11, R12), -np.eye(R12.shape[1])])

        # Inverse permutation
        Pinv = np.zeros(len(P), int)
        for k, p in enumerate(P):
            Pinv[p] = k
        LHS = R1[:, Pinv]

        info["Q1"] = Q[:, :rank]
        info["LHS"] = LHS

        basis = np.zeros(N.T.shape)
        basis[:, P] = N.T
    else:
        raise ValueError(method)

    # test that it is indeed a null space
    error = A_dense @ basis.T
    info["values"] = S
    info["error"] = error
    return basis, info
