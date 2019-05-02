"""
Copyright 2013 Steven Diamond, 2017 Robin Verschueren, 2017 Akshay Agrawal

Licensed under the Apache License, Version 2.0 (the "License");

you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import cvxpy.interface as intf
import cvxpy.settings as s
from cvxpy.constraints import PSD, SOC, ExpCone
from cvxpy.reductions.solution import failure_solution, Solution
from cvxpy.reductions.solvers.conic_solvers.conic_solver import (ConeDims,
                                                                 ConicSolver)
from cvxpy.reductions.solvers import utilities
from cvxpy.reductions.utilities import group_constraints
import numpy as np
import scipy.sparse as sp


# Utility method for formatting a ConeDims instance into a dictionary
# that can be supplied to scs.
def dims_to_solver_dict(cone_dims):
    cones = {
        "f": cone_dims.zero,
        "l": cone_dims.nonpos,
        "q": cone_dims.soc,
        "ep": cone_dims.exp,
        "s": cone_dims.psd,
    }
    return cones


# Utility methods for special handling of semidefinite constraints.
def scaled_lower_tri(size):
    """Returns an expression representing the lower triangular entries

    Scales the strictly lower triangular entries by sqrt(2), as required
    by SCS.

    Parameters
    ----------
    size : The dimension of the PSD constraint.

    Returns
    -------
    Expression
        An expression representing the (scaled) lower triangular part of
        the supplied matrix expression.
    """
    rows = cols = size
    entries = rows * (cols + 1)//2

    row_arr = np.arange(0, entries)

    lower_diag_indices = np.tril_indices(rows)
    col_arr = np.sort(np.ravel_multi_index(lower_diag_indices, (rows, cols), order='F'))

    val_arr = np.zeros((rows, cols))
    val_arr[lower_diag_indices] = 1/np.sqrt(2)
    np.fill_diagonal(val_arr, 0.5)
    val_arr = np.ravel(val_arr, order='F')
    val_arr = val_arr[np.nonzero(val_arr)]

    shape = (entries, rows*cols)
    return sp.csc_matrix((val_arr, (row_arr, col_arr)), shape)


def tri_to_full(lower_tri, n):
    """Expands n*(n+1)//2 lower triangular to full matrix

    Scales off-diagonal by 1/sqrt(2), as per the SCS specification.

    Parameters
    ----------
    lower_tri : numpy.ndarray
        A NumPy array representing the lower triangular part of the
        matrix, stacked in column-major order.
    n : int
        The number of rows (columns) in the full square matrix.

    Returns
    -------
    numpy.ndarray
        A 2-dimensional ndarray that is the scaled expansion of the lower
        triangular array.
    """
    full = np.zeros((n, n))
    full[np.triu_indices(n)] = lower_tri
    full[np.tril_indices(n)] = lower_tri

    full[np.tril_indices(n, k=-1)] /= np.sqrt(2)
    full[np.triu_indices(n, k=1)] /= np.sqrt(2)

    return np.reshape(full, n*n, order="F")


class SCS(ConicSolver):
    """An interface for the SCS solver.
    """

    # Solver capabilities.
    MIP_CAPABLE = False
    SUPPORTED_CONSTRAINTS = ConicSolver.SUPPORTED_CONSTRAINTS + [SOC,
                                                                 ExpCone,
                                                                 PSD]
    REQUIRES_CONSTR = True

    # Map of SCS status to CVXPY status.
    STATUS_MAP = {"Solved": s.OPTIMAL,
                  "Solved/Inaccurate": s.OPTIMAL_INACCURATE,
                  "Unbounded": s.UNBOUNDED,
                  "Unbounded/Inaccurate": s.UNBOUNDED_INACCURATE,
                  "Infeasible": s.INFEASIBLE,
                  "Infeasible/Inaccurate": s.INFEASIBLE_INACCURATE,
                  "Failure": s.SOLVER_ERROR,
                  "Indeterminate": s.SOLVER_ERROR,
                  "Interrupted": s.SOLVER_ERROR}

    # Order of exponential cone arguments for solver.
    EXP_CONE_ORDER = [0, 1, 2]

    def name(self):
        """The name of the solver.
        """
        return s.SCS

    def import_solver(self):
        """Imports the solver.
        """
        import scs
        scs  # For flake8

    def psd_format_mat(self, constr):
        """Return a matrix to multiply by PSD constraint coefficients.

        Special cases PSD constraints, as SCS expects constraints to be
        imposed on solely the lower triangular part of the variable matrix.
        Moreover, it requires the off-diagonal coefficients to be scaled by
        sqrt(2).
        """
        rows = cols = constr.expr.shape[0]
        entries = rows * (cols + 1)//2

        row_arr = np.arange(0, entries)

        lower_diag_indices = np.tril_indices(rows)
        col_arr = np.sort(np.ravel_multi_index(lower_diag_indices,
                                               (rows, cols),
                                               order='F'))

        val_arr = np.zeros((rows, cols))
        val_arr[lower_diag_indices] = 1/np.sqrt(2)
        np.fill_diagonal(val_arr, 0.5)
        val_arr = np.ravel(val_arr, order='F')
        val_arr = val_arr[np.nonzero(val_arr)]

        shape = (entries, rows*cols)
        return sp.csc_matrix((val_arr, (row_arr, col_arr)), shape)
        # TODO work in expr + expr.T
        # return scaled_lower_tri(expr + expr.T)/2

    def apply(self, problem):
        """Returns a new problem and data for inverting the new solution.

        Returns
        -------
        tuple
            (dict of arguments needed for the solver, inverse data)
        """
        data = {}
        inv_data = {self.VAR_ID: problem.x.id}

        # SCS requires constraints to be specified in the following order:
        # 1. zero cone
        # 2. non-negative orthant
        # 3. soc
        # 4. psd
        # 5. exponential
        constr_map = group_constraints(problem.constraints)
        data[ConicSolver.DIMS] = ConeDims(constr_map)
        inv_data[ConicSolver.DIMS] = data[ConicSolver.DIMS]

        # Format the constraints.
        formatted = self.format_constraints(problem, self.EXP_CONE_ORDER)
        data[s.PARAM_PROB] = formatted

        # Apply parameter values.
        # Obtain A, b such that Ax + s = b, s \in cones.
        #
        # Note that scs mandates that the cones MUST be ordered with
        # zero cones first, then non-nonnegative orthant, then SOC,
        # then PSD, then exponential.
        prob = formatted.apply_parameters()
        data[s.C] = prob.c[:-1]
        inv_data[s.OFFSET] = prob.c[-1]
        data[s.A] = -prob.A[:, :-1]
        data[s.B] = prob.A[:, -1].A.flatten()
        return data, inv_data

    def extract_dual_value(self, result_vec, offset, constraint):
        """Extracts the dual value for constraint starting at offset.

        Special cases PSD constraints, as per the SCS specification.
        """
        if isinstance(constraint, PSD):
            dim = constraint.shape[0]
            lower_tri_dim = dim * (dim + 1) // 2
            new_offset = offset + lower_tri_dim
            lower_tri = result_vec[offset:new_offset]
            full = tri_to_full(lower_tri, dim)
            return full, new_offset
        else:
            return utilities.extract_dual_value(result_vec, offset,
                                                constraint)

    def invert(self, solution, inverse_data):
        """Returns the solution to the original problem given the inverse_data.
        """
        status = self.STATUS_MAP[solution["info"]["status"]]

        attr = {}
        attr[s.SOLVE_TIME] = solution["info"]["solveTime"]
        attr[s.SETUP_TIME] = solution["info"]["setupTime"]
        attr[s.NUM_ITERS] = solution["info"]["iter"]

        if status in s.SOLUTION_PRESENT:
            primal_val = solution["info"]["pobj"]
            opt_val = primal_val + inverse_data[s.OFFSET]
            primal_vars = {
                inverse_data[SCS.VAR_ID]:
                intf.DEFAULT_INTF.const_to_matrix(solution["x"])
            }
            dual_vars = {
                SCS.DUAL_VAR_ID:
                intf.DEFAULT_INTF.const_to_matrix(solution["y"])
            }
            return Solution(status, opt_val, primal_vars, dual_vars, attr)
        else:
            return failure_solution(status)

    def solve_via_data(self, data, warm_start, verbose, solver_opts, solver_cache=None):
        """Returns the result of the call to the solver.

        Parameters
        ----------
        data : dict
            Data generated via an apply call.
        warm_start : Bool
            Whether to warm_start SCS.
        verbose : Bool
            Control the verbosity.
        solver_opts : dict
            SCS-specific solver options.

        Returns
        -------
        The result returned by a call to scs.solve().
        """
        import scs
        args = {"A": data[s.A], "b": data[s.B], "c": data[s.C]}
        if warm_start and solver_cache is not None and \
           self.name() in solver_cache:
            args["x"] = solver_cache[self.name()]["x"]
            args["y"] = solver_cache[self.name()]["y"]
            args["s"] = solver_cache[self.name()]["s"]
        cones = dims_to_solver_dict(data[ConicSolver.DIMS])
        # Default to eps = 1e-4 instead of 1e-3.
        solver_opts['eps'] = solver_opts.get('eps', 1e-4)
        results = scs.solve(
            args,
            cones,
            verbose=verbose,
            **solver_opts)
        if solver_cache is not None:
            solver_cache[self.name()] = results
        return results
