#  ___________________________________________________________________________
#
#  Pyomo: Python Optimization Modeling Objects
#  Copyright 2017 National Technology and Engineering Solutions of Sandia, LLC
#  Under the terms of Contract DE-NA0003525 with National Technology and
#  Engineering Solutions of Sandia, LLC, the U.S. Government retains certain
#  rights in this software.
#  This software is distributed under the 3-clause BSD License.
#  ___________________________________________________________________________

import itertools
from pyomo.environ import SolverFactory
from pyomo.core.base.var import Var
from pyomo.core.base.constraint import Constraint
from pyomo.core.base.objective import Objective
from pyomo.core.expr.visitor import identify_variables
from pyomo.common.collections import ComponentSet
from pyomo.util.subsystems import (
        create_subsystem_block,
        TemporarySubsystemManager,
        )
from pyomo.contrib.pynumero.interfaces.pyomo_nlp import PyomoNLP
from pyomo.contrib.pynumero.interfaces.utils import CondensedSparseSummation
from pyomo.contrib.pynumero.interfaces.external_grey_box import (
        ExternalGreyBoxModel,
        )
import numpy as np
import scipy.sparse as sps


def _dense_to_full_sparse(matrix):
    """
    Used to convert a dense matrix (2d NumPy array) to SciPy sparse matrix
    with explicit coordinates for every entry, including zeros. This is
    used because _ExternalGreyBoxAsNLP methods rely on receiving sparse
    matrices where sparsity structure does not change between calls.
    This is difficult to achieve for matrices obtained via the implicit
    function theorem unless an entry is returned for every coordinate
    of the matrix.

    Note that this does not mean that the Hessian of the entire NLP will
    be dense, only that the block corresponding to this external model
    will be dense.
    """
    # TODO: Allow methods to hard-code Jacobian/Hessian sparsity structure
    # in the case it is known a priori.
    # TODO: Decompose matrices to infer maximum-fill-in sparsity structure.
    nrow, ncol = matrix.shape
    row = []
    col = []
    data = []
    for i, j in itertools.product(range(nrow), range(ncol)):
        row.append(i)
        col.append(j)
        data.append(matrix[i,j])
    row = np.array(row)
    col = np.array(col)
    data = np.array(data)
    return sps.coo_matrix((data, (row, col)), shape=(nrow, ncol))


def get_hessian_of_constraint(constraint, wrt1=None, wrt2=None):
    constraints = [constraint]
    if wrt1 is None and wrt2 is None:
        variables = list(identify_variables(constraint.expr, include_fixed=False))
        wrt1 = variables
        wrt2 = variables
    elif wrt1 is not None and wrt2 is not None:
        variables = wrt1 + wrt2
    elif wrt1 is not None: # but wrt2 is None
        wrt2 = wrt1
        variables = wrt1
    else:
        # wrt2 is not None and wrt1 is None
        wrt1 = wrt2
        variables = wrt1

    block = create_subsystem_block(constraints, variables=variables)
    # Could fix input_vars so I don't evaluate the Hessian with respect
    # to variables I don't care about...

    # HUGE HACK: Variables not included in a constraint are not written
    # to the nl file, so we cannot take the derivative with respect to
    # them, even though we know this derivative is zero. To work around,
    # we make sure all variables appear on the block in the form of a
    # dummy constraint. Then we can take derivatives of any constraint
    # with respect to them. Conveniently, the extract_submatrix_
    # call deals with extracting the variables and constraint we care
    # about, in the proper order.
    block._dummy_var = Var()
    block._dummy_con = Constraint(expr=sum(variables) == block._dummy_var)
    block._obj = Objective(expr=0.0)
    nlp = PyomoNLP(block)

    # NOTE: This makes some assumption about how the Lagrangian is constructed.
    # TODO: Define the convention we assume and convert if necessary.
    duals = [0.0, 0.0]
    idx = nlp.get_constraint_indices(constraints)[0]
    duals[idx] = 1.0
    nlp.set_duals(duals)
    # NOTE: The returned matrix preserves explicit zeros. I.e. it contains
    # coordinates for every entry that could possibly be nonzero.
    return nlp.extract_submatrix_hessian_lag(wrt1, wrt2)


class ExternalPyomoModel(ExternalGreyBoxModel):
    """
    This is an ExternalGreyBoxModel used to create an exteral model
    from existing Pyomo components. Given a system of variables and
    equations partitioned into "input" and "external" variables and
    "residual" and "external" equations, this class computes the
    residual of the "residual equations," as well as their Jacobian
    and Hessian, as a function of only the inputs.

    Pyomo components:
        f(x, y) == 0 # "Residual equations"
        g(x, y) == 0 # "External equations", dim(g) == dim(y)

    Effective constraint seen by this "external model":
        F(x) == f(x, y(x)) == 0
        where y(x) solves g(x, y) == 0

    """

    def __init__(self,
            input_vars,
            external_vars,
            residual_cons,
            external_cons,
            solver=None,
            ):
        if solver is None:
            solver = SolverFactory("ipopt")
        self._solver = solver

        # We only need this block to construct the NLP, which wouldn't
        # be necessary if we could compute Hessians of Pyomo constraints.
        self._block = create_subsystem_block(
                residual_cons+external_cons,
                input_vars+external_vars,
                )
        self._block._obj = Objective(expr=0.0)
        self._nlp = PyomoNLP(self._block)

        assert len(external_vars) == len(external_cons)

        self.input_vars = input_vars
        self.external_vars = external_vars
        self.residual_cons = residual_cons
        self.external_cons = external_cons

        self.residual_con_multipliers = [None for _ in residual_cons]

    def n_inputs(self):
        return len(self.input_vars)

    def n_equality_constraints(self):
        return len(self.residual_cons)

    # I would like to try to get by without using the following "name" methods.
    def input_names(self):
        return ["input_%i" % i for i in range(self.n_inputs())]
    def equality_constraint_names(self):
        return ["residual_%i" % i for i in range(self.n_equality_constraints())]

    def set_input_values(self, input_values):
        solver = self._solver
        external_cons = self.external_cons
        external_vars = self.external_vars
        input_vars = self.input_vars

        for var, val in zip(input_vars, input_values):
            var.set_value(val)

        _temp = create_subsystem_block(external_cons, variables=external_vars)
        possible_input_vars = ComponentSet(input_vars)
        for var in _temp.input_vars.values():
            assert var in possible_input_vars

        with TemporarySubsystemManager(to_fix=input_vars):
            solver.solve(_temp)

        # Should we create the NLP from the original block or the temp block?
        # Need to create it from the original block because temp block won't
        # have residual constraints, whose derivatives are necessary.
        self._nlp = PyomoNLP(self._block)

    def set_equality_constraint_multipliers(self, eq_con_multipliers):
        for i, val in enumerate(eq_con_multipliers):
            self.residual_con_multipliers[i] = val

    def evaluate_equality_constraints(self):
        return self._nlp.extract_subvector_constraints(self.residual_cons)

    def evaluate_jacobian_equality_constraints(self):
        nlp = self._nlp
        x = self.input_vars
        y = self.external_vars
        f = self.residual_cons
        g = self.external_cons
        jfx = nlp.extract_submatrix_jacobian(x, f)
        jfy = nlp.extract_submatrix_jacobian(y, f)
        jgx = nlp.extract_submatrix_jacobian(x, g)
        jgy = nlp.extract_submatrix_jacobian(y, g)

        nf = len(f)
        nx = len(x)
        n_entries = nf*nx

        # TODO: Does it make sense to cast dydx to a sparse matrix?
        # My intuition is that it does only if jgy is "decomposable"
        # in the strongly connected component sense, which is probably
        # not usually the case.
        dydx = -1 * sps.linalg.splu(jgy.tocsc()).solve(jgx.toarray())
        # NOTE: PyNumero block matrices require this to be a sparse matrix
        # that contains coordinates for every entry that could possibly
        # be nonzero. Here, this is all of the entries.
        dfdx = jfx + jfy.dot(dydx)

        return _dense_to_full_sparse(dfdx)

    def evaluate_jacobian_external_variables(self):
        nlp = self._nlp
        x = self.input_vars
        y = self.external_vars
        g = self.external_cons
        jgx = nlp.extract_submatrix_jacobian(x, g)
        jgy = nlp.extract_submatrix_jacobian(y, g)
        jgy_csc = jgy.tocsc()
        dydx = -1 * sps.linalg.splu(jgy_csc).solve(jgx.toarray())
        return dydx

    def evaluate_hessian_external_variables(self):
        nlp = self._nlp
        x = self.input_vars
        y = self.external_vars
        g = self.external_cons
        jgx = nlp.extract_submatrix_jacobian(x, g)
        jgy = nlp.extract_submatrix_jacobian(y, g)
        jgy_csc = jgy.tocsc()
        jgy_fact = sps.linalg.splu(jgy_csc)
        dydx = -1 * jgy_fact.solve(jgx.toarray())

        ny = len(y)
        nx = len(x)

        hgxx = [get_hessian_of_constraint(con, x) for con in g]
        hgxy = [get_hessian_of_constraint(con, x, y) for con in g]
        hgyy = [get_hessian_of_constraint(con, y) for con in g]

        # Each term should be a length-ny list of nx-by-nx matrices
        # TODO: Make these 3-d numpy arrays.
        term1 = hgxx # Sparse matrix
        term2 = []
        for hessian in hgxy:
            # Sparse matrix times dense matrix. The result is sparse
            # if the sparse matrix is low rank.
            _prod = hessian.dot(dydx)
            term2.append(_prod + _prod.transpose())
        # Dense matrix times sparse matrix times dense matrix.
        # I believe the product is always dense.
        term3 = [dydx.transpose().dot(hessian.toarray()).dot(dydx)
                for hessian in hgyy]

        # List of nx-by-nx matrices
        sum_ = [t1 + t2 + t3 for t1, t2, t3 in zip(term1, term2, term3)]

        # TODO: Store this as 3d array, use np.reshape to perform
        # backsolve in a single call.
        vectors = [[np.array([matrix[i, j] for matrix in sum_])
                for j in range(nx)] for i in range(nx)]
        solved_vectors = [[jgy_fact.solve(vector) for vector in vlist]
            for vlist in vectors]
        d2ydx2 = [
            -np.array([[vec[i] for vec in vlist]
            for vlist in solved_vectors])
            for i in range(ny)
            ]

        return d2ydx2

    def evaluate_hessians_of_residuals(self):
        """
        This method computes the Hessian matrix of each equality
        constraint individually, rather than the sum of Hessians
        times multipliers.
        """
        nlp = self._nlp
        x = self.input_vars
        y = self.external_vars
        f = self.residual_cons
        g = self.external_cons
        jfx = nlp.extract_submatrix_jacobian(x, f)
        jfy = nlp.extract_submatrix_jacobian(y, f)

        dydx = self.evaluate_jacobian_external_variables()

        nf = len(f)
        nx = len(x)

        hfxx = [get_hessian_of_constraint(con, x) for con in f]
        hfxy = [get_hessian_of_constraint(con, x, y) for con in f]
        hfyy = [get_hessian_of_constraint(con, y) for con in f]

        d2ydx2 = self.evaluate_hessian_external_variables()

        # Each term should be a length-ny list of nx-by-nx matrices
        # TODO: Make these 3-d numpy arrays.
        term1 = hfxx
        term2 = []
        for hessian in hfxy:
            _prod = hessian.dot(dydx)
            term2.append(_prod + _prod.transpose())
        term3 = [dydx.transpose().dot(hessian.toarray()).dot(dydx)
                for hessian in hfyy]

        # Extract each of the nx^2 vectors from d2ydx2
        vectors = [[np.array([matrix[i, j] for matrix in d2ydx2])
                for j in range(nx)] for i in range(nx)]
        # Multiply by jfy
        product_vectors = [[jfy.dot(vector) for vector in vlist]
            for vlist in vectors]
        term4 = [
            np.array([[vec[i] for vec in vlist]
            for vlist in product_vectors])
            for i in range(nf)
            ]

        # List of nx-by-nx matrices
        d2fdx2 = [t1 + t2 + t3 + t4
                for t1, t2, t3, t4 in zip(term1, term2, term3, term4)]
        return d2fdx2

    def evaluate_hessian_equality_constraints(self):
        """
        This method actually evaluates the sum of Hessians times
        multipliers, i.e. the term in the Hessian of the Lagrangian
        due to these equality constraints.
        """
        d2fdx2 = self.evaluate_hessians_of_residuals()
        multipliers = self.residual_con_multipliers

        sum_ = sum(mult*matrix for mult, matrix in zip(multipliers, d2fdx2))
        # Return a sparse matrix with every entry accounted for because it
        # is difficult to determine rigorously which coordinates
        # _could possibly_ be nonzero.
        sparse = _dense_to_full_sparse(sum_)
        return sps.tril(sparse)
