""" Implementation of the nonlinear optimizer for the data-augmented MPC.

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version.
This program is distributed in the hope that it will be useful, but WITHOUT
ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
You should have received a copy of the GNU General Public License along with
this program. If not, see <http://www.gnu.org/licenses/>.
"""


import os
import sys
import shutil
import casadi as cs
import numpy as np
from copy import copy
from acados_template import AcadosOcp, AcadosOcpSolver, AcadosModel
from ad_mpc.ad_3d import AD3D
from model_fitting.gp import GPEnsemble
from utils.utils import skew_symmetric, v_dot_q, safe_mkdir_recursive, quaternion_inverse
# from utils.quad_3d_opt_utils import discretize_dynamics_and_cost


class AD3DOptimizer:
    def __init__(self, ad, t_horizon=1, n_nodes=20, q_cost=None, r_cost=None, model_name="ad_3d_acados_mpc", solver_options=None):
        """
        :param quad: ad object
        :type quad: AD3D
        :param t_horizon: time horizon for MPC optimization
        :param n_nodes: number of optimization nodes until time horizon
        :param q_cost: diagonal of Q matrix for LQR cost of MPC cost function. Must be a numpy array of length 12.
        :param r_cost: diagonal of R matrix for LQR cost of MPC cost function. Must be a numpy array of length 4.
        :param solver_options: Optional set of extra options dictionary for solvers.        
        """

        # Weighted squared error loss function q = (p_xyz, a_xyz, v_xyz, r_xyz), r = (u1, u2, u3, u4)
        if q_cost is None:
            q_cost = np.array([1.0, 1.0, 10., 5.0])
        if r_cost is None:
            r_cost = np.array([10.0, 100.0])             

        self.T = t_horizon  # Time horizon
        self.N = n_nodes  # number of control nodes within horizon


        self.ad = ad
    
        self.steering_min = ad.steering_min
        self.steering_max = ad.steering_max
        self.acc_min = ad.acc_min
        self.acc_max = ad.acc_max

        # Declare model variables
        self.p = cs.MX.sym('p', 2)  # position
        self.s = cs.MX.sym('s', 1)  # psi 
        self.v = cs.MX.sym('v', 1)  # velocity        

        # Full state vector (4-dimensional)
        self.x = cs.vertcat(self.p, self.s, self.v)
        self.state_dim = 4

        # Control input vector
        u1 = cs.MX.sym('u1')
        u2 = cs.MX.sym('u2')
        
        self.u = cs.vertcat(u1, u2)

        # Nominal model equations symbolic function (no GP)
        self.ad_xdot_nominal = self.ad_dynamics()

        # Initialize objective function, 0 target state and integration equations
        self.L = None
        self.target = None


        # Build full model. Will have 4 variables. self.dyn_x contains the symbolic variable that
        # should be used to evaluate the dynamics function. It corresponds to self.x if there are no GP's, or
        # self.x_with_gp otherwise
        acados_models, nominal_with_gp = self.acados_setup_model(
            self.ad_xdot_nominal(x=self.x, u=self.u)['x_dot'], model_name)

        
        # Convert dynamics variables to functions of the state and input vectors
        self.ad_xdot = {}
        for dyn_model_idx in nominal_with_gp.keys():
            dyn = nominal_with_gp[dyn_model_idx]
            self.ad_xdot[dyn_model_idx] = cs.Function('x_dot', [self.x, self.u], [dyn], ['x', 'u'], ['x_dot'])

        # ### Setup and compile Acados OCP solvers ### #
        self.acados_ocp_solver = {}

        
        # Ensure current working directory is current folder
        os.chdir(os.path.dirname(os.path.realpath(__file__)))
        self.acados_models_dir = '../../acados_models'
        safe_mkdir_recursive(os.path.join(os.getcwd(), self.acados_models_dir))

        for key, key_model in zip(acados_models.keys(), acados_models.values()):
            nx = key_model.x.size()[0]
            nu = key_model.u.size()[0]
            ny = nx + nu
            n_param = key_model.p.size()[0] if isinstance(key_model.p, cs.MX) else 0

            acados_source_path = os.environ['ACADOS_SOURCE_DIR']
            sys.path.insert(0, '../common')

            # Create OCP object to formulate the optimization
            ocp = AcadosOcp()
            ocp.acados_include_path = acados_source_path + '/include'
            ocp.acados_lib_path = acados_source_path + '/lib'
            ocp.model = key_model
            ocp.dims.N = self.N
            ocp.solver_options.tf = t_horizon

            # Initialize parameters
            ocp.dims.np = n_param
            ocp.parameter_values = np.zeros(n_param)

            ocp.cost.cost_type = 'LINEAR_LS'
            ocp.cost.cost_type_e = 'LINEAR_LS'

            ocp.cost.W = np.diag(np.concatenate((q_cost, r_cost)))
            ocp.cost.W_e = np.diag(q_cost)
            terminal_cost = 0 if solver_options is None or not solver_options["terminal_cost"] else 1
            ocp.cost.W_e *= terminal_cost

            ocp.cost.Vx = np.zeros((ny, nx))
            ocp.cost.Vx[:nx, :nx] = np.eye(nx)
            ocp.cost.Vu = np.zeros((ny, nu))
            ocp.cost.Vu[-2:, -2:] = np.eye(nu)

            ocp.cost.Vx_e = np.eye(nx)

            # Initial reference trajectory (will be overwritten)
            x_ref = np.zeros(nx)
            ocp.cost.yref = np.concatenate((x_ref, np.array([0.0, 0.0])))
            ocp.cost.yref_e = x_ref

            # Initial state (will be overwritten)
            ocp.constraints.x0 = x_ref

            # Set constraints
            ocp.constraints.lbu = np.array([self.acc_min, self.steering_min])
            ocp.constraints.ubu = np.array([self.acc_max, self.steering_max])
            ocp.constraints.idxbu = np.array([0, 1])

            # Solver options
            ocp.solver_options.qp_solver = 'FULL_CONDENSING_HPIPM'
            ocp.solver_options.hessian_approx = 'GAUSS_NEWTON'
            ocp.solver_options.integrator_type = 'ERK'
            ocp.solver_options.print_level = 0
            ocp.solver_options.nlp_solver_type = 'SQP_RTI' if solver_options is None else solver_options["solver_type"]

            # Compile acados OCP solver if necessary
            json_file = os.path.join(self.acados_models_dir, key_model.name + '_acados_ocp.json')
            self.acados_ocp_solver[key] = AcadosOcpSolver(ocp, json_file=json_file)

    def clear_acados_model(self):
        """
        Removes previous stored acados models to avoid name conflicts.
        """

        json_file = os.path.join(self.acados_models_dir, 'acados_ocp.json')
        if os.path.exists(json_file):
            os.remove(os.path.join(os.getcwd(), json_file))
        compiled_model_dir = os.path.join(os.getcwd(), 'c_generated_code')
        if os.path.exists(compiled_model_dir):
            shutil.rmtree(compiled_model_dir)

   
    def acados_setup_model(self, nominal, model_name):
        """
        Builds an Acados symbolic models using CasADi expressions.
        :param model_name: name for the acados model. Must be different from previously used names or there may be
        problems loading the right model.
        :param nominal: CasADi symbolic nominal model of the AD: f(self.x, self.u) = x_dot, dimensions 4x1.
        :return: Returns a total of three outputs, where m is the number of GP's in the GP ensemble, or 1 if no GP:
            - A dictionary of m AcadosModel of the GP-augmented AD
            - A dictionary of m CasADi symbolic nominal dynamics equations with GP mean value augmentations (if with GP)
        :rtype: dict, dict, cs.MX
        """
        def fill_in_acados_model(x, u, p, dynamics, name):

            x_dot = cs.MX.sym('x_dot', dynamics.shape)
            f_impl = x_dot - dynamics

            # Dynamics model
            model = AcadosModel()
            model.f_expl_expr = dynamics
            model.f_impl_expr = f_impl
            model.x = x
            model.xdot = x_dot
            model.u = u
            model.p = p
            model.name = name

            return model

        acados_models = {}
        dynamics_equations = {}


            # No available GP so return nominal dynamics
        dynamics_equations[0] = nominal

        x_ = self.x
        dynamics_ = nominal

        acados_models[0] = fill_in_acados_model(x=x_, u=self.u, p=[], dynamics=dynamics_, name=model_name)

        return acados_models, dynamics_equations

    def ad_dynamics(self):
        """
        Symbolic dynamics of the 2D AD model. The state consists on: [p_xy, psi, speed]^T.
        The input of the system is: [u_1, u_2], i.e. acceleration & delta(steering angle)

        :return: CasADi function that computes the analytical differential state dynamics of the quadrotor model.
        Inputs: 'x' state of AD (4x1) and 'u' control input (2x1). Output: differential state vector 'x_dot'
        (4x1)
        """
        x_dot = cs.vertcat(self.p_dynamics(), self.s_dynamics(), self.v_dynamics())
        return cs.Function('x_dot', [self.x[:4], self.u], [x_dot], ['x', 'u'], ['x_dot'])

    def p_dynamics(self):
        beta = cs.atan(self.ad.L_R / (self.ad.L_F + self.ad.L_R) * cs.tan(self.u[1]))        
        return cs.vertcat(self.v * cs.cos(self.s + beta), self.v * cs.sin(self.s+beta))

    def s_dynamics(self):
        beta = cs.atan(self.ad.L_R / (self.ad.L_F + self.ad.L_R) * cs.tan(self.u[1]))                
        return self.v/self.ad.L_R*cs.sin(beta)

    def v_dynamics(self):        
        return self.u[0]   

    def set_reference_state(self, x_target=None, u_target=None):
        """
        Sets the target state and pre-computes the integration dynamics with cost equations
        :param x_target: 4-dimensional target state (p_xyz, a_wxyz, v_xyz, r_xyz)
        :param u_target: 2-dimensional target control input vector (u_1, u_2, u_3, u_4)
        """
        if x_target is None:
            x_target = [[0, 0, 0, 0]]
            return
        if u_target is None:
            u_target = [0, 0]
            return
        # Set new target state
        self.target = copy(x_target)
        gp_ind = 0
        ref = np.concatenate((x_target, u_target))
        x_target = np.array(x_target)
        for j in range(self.N):
            self.acados_ocp_solver[gp_ind].set(j, "yref", ref)
        self.acados_ocp_solver[gp_ind].set(self.N, "yref", x_target)
        return gp_ind

    def set_reference_trajectory(self, x_target, u_target):
        """
        Sets the reference trajectory and pre-computes the cost equations for each point in the reference sequence.
        :param x_target: Nx4-dimensional reference trajectory (p_xy, psi, vel). It is passed in the
        form of a 3-length list, where the first element is a Nx2 numpy array referring to the position targets, the
        second is a Nx1 array referring to the yaw, one Nx1 arrays for the speed.
        :param u_target: Nx2-dimensional target control input vector (u1, u2)
        """
        ##########################################################################################
        # if u_target is not None:
        #     assert x_target[0].shape[0] == (u_target.shape[0] + 1) or x_target[0].shape[0] == u_target.shape[0]

        # # If not enough states in target sequence, append last state until required length is met
        while x_target.shape[0] < self.N+1:
            x_target = np.vstack((x_target,x_target[-1,:]))
            u_target = np.vstack((u_target,u_target[-1,:]))
            
            # x_target = [np.concatenate(x_target, x_target[-1,:], 0) for x in x_target]
            # if u_target is not None:
            #     u_target = np.concatenate((u_target, np.expand_dims(u_target[-1, :], 0)), 0)

        # stacked_x_target = np.concatenate([x for x in x_target], 1)
        ##########################################################################################
        gp_ind = 0
        # tmp = x_target[:,2] 
        # np.place(tmp,tmp < -3, tmp+2*np.pi)
        # x_target[:,2] = tmp
        self.target = copy(x_target)       
        print(x_target)        
        stacked_x_target = x_target 
        
        
        for j in range(self.N):
            ref = stacked_x_target[j, :]
            ref = np.concatenate((ref, u_target[j, :]))
            self.acados_ocp_solver[gp_ind].set(j, "yref", ref)
        # the last MPC node has only a state reference but no input reference
        self.acados_ocp_solver[gp_ind].set(self.N, "yref", stacked_x_target[self.N, :])
        return gp_ind

    
    def run_optimization(self, initial_state=None, use_model=0, return_x=False, gp_regression_state=None):
        """
        Optimizes a trajectory to reach the pre-set target state, starting from the input initial state, that minimizes
        the quadratic cost function and respects the constraints of the system

        :param initial_state: 13-element list of the initial state. If None, 0 state will be used
        :param use_model: integer, select which model to use from the available options.
        :param return_x: bool, whether to also return the optimized sequence of states alongside with the controls.
        :param gp_regression_state: 13-element list of state for GP prediction. If None, initial_state will be used.
        :return: optimized control input sequence (flattened)
        """

        if initial_state is None:
            initial_state = [0.0, 0.0] + [0.0]+ [0.0]

        # Set initial state. Add gp state if needed
        x_init = initial_state
        x_init = np.stack(x_init)
        x_init = x_init.squeeze()

        # Set initial condition, equality constraint
        self.acados_ocp_solver[use_model].set(0, 'lbx', x_init)
        self.acados_ocp_solver[use_model].set(0, 'ubx', x_init)
        
        # Solve OCPacados_ocp_solver
        self.acados_ocp_solver[use_model].solve()

        # Get u
        w_opt_acados = np.ndarray((self.N, 2))
        x_opt_acados = np.ndarray((self.N + 1, len(x_init)))
        x_opt_acados[0, :] = self.acados_ocp_solver[use_model].get(0, "x")
        for i in range(self.N):
            w_opt_acados[i, :] = self.acados_ocp_solver[use_model].get(i, "u")
            x_opt_acados[i + 1, :] = self.acados_ocp_solver[use_model].get(i + 1, "x")

        w_opt_acados = np.reshape(w_opt_acados, (-1))
        return w_opt_acados if not return_x else (w_opt_acados, x_opt_acados)
