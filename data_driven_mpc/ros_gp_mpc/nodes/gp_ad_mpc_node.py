#!/usr/bin/env python3.6
""" ROS node for the data-augmented MPC, to use in the Gazebo simulator and real world experiments.

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

import json
import time
import rospy
import threading
import numpy as np
import pandas as pd
from tqdm import tqdm
from std_msgs.msg import Bool, Empty
from geometry_msgs.msg import PoseStamped
from ackermann_msgs.msg import AckermannDrive
from autoware_msgs.msg import Lane, Waypoint
from carla_msgs.msg import CarlaEgoVehicleStatus
from ad_mpc.create_ros_ad_mpc import ROSGPMPC
from utils.utils import jsonify, interpol_mse, quaternion_state_mse, load_pickled_models, v_dot_q, \
    separate_variables
from utils.visualization import trajectory_tracking_results, mse_tracking_experiment_plot, \
    load_past_experiments, get_experiment_files
from experiments.point_tracking_and_record import make_record_dict, get_record_file_and_dir, check_out_data
from model_fitting.rdrv_fitting import load_rdrv

from utils.utils import quaternion_to_euler, skew_symmetric, v_dot_q, unit_quat, quaternion_inverse
class GPMPCWrapper:
    def __init__(self,environment="carla"):

        # Control at 50 (sim) or 60 (real) hz. Use time horizon=1 and 10 nodes
        self.n_mpc_nodes = rospy.get_param('~n_nodes', default=10)
        self.t_horizon = rospy.get_param('~t_horizon', default=1.0)
        self.control_freq_factor = rospy.get_param('~control_freq_factor', default=5 if environment == "carla" else 6)
        self.opt_dt = self.t_horizon / (self.n_mpc_nodes * self.control_freq_factor)
#################################################################        
        # Initialize GP MPC for point tracking
        # self.gp_mpc = ROSGPMPC(self.t_horizon, self.n_mpc_nodes, self.opt_dt)
#################################################################
        # Last state obtained from odometry
        self.x = None
        self.environment = environment
        # Elapsed time between two recordings
        self.last_update_time = time.time()

        # Last references. Use hovering activation as input reference
        self.last_x_ref = None
        self.last_u_ref = None

        # Reference trajectory variables
        self.x_ref = None
        self.t_ref = None
        self.u_ref = None
        self.current_idx = 0
        self.quad_trajectory = None
        self.quad_controls = None
        self.w_control = None

        # To measure optimization elapsed time
        self.optimization_dt = 0

        # Thread for MPC optimization
        self.mpc_thread = threading.Thread()

        # Trajectory tracking experiment. Dims: seed x av_v x n_samples
        
        
        self.mse_exp = np.zeros((0, 0, 0, 1))
        self.t_opt = np.zeros((0, 0, 0))
        self.mse_exp_v_max = np.zeros((0, 0))
        self.ref_traj_name = ""
        self.ref_v = 0
        self.run_traj_counter = 0

        # Keep track of status of MPC object
        self.vehicle_status_available = False
        self.pose_available = False 
        self.waypoint_available = False 
        # Binary variable to run MPC only once every other odometry callback
        self.optimize_next = True

        #  Remember the sequence number of the last odometry message received.
        self.last_odom_seq_number = 0

        # Setup node publishers and subscribers. The odometry (sub) and control (pub) topics will vary depending on
        # Assume Carla simulation environment         
        if self.environment == "carla":
            pose_topic = "/ndt_pose"
            vehicle_status_topic = "/carla/ego_vehicle/vehicle_status"
            control_topic = "/carla/ego_vehicle/ackermann_cmd"            
            waypoint_topic = "/final_waypoints"
        else:
            # Real world setup
            pose_topic = "/ndt_pose"
            vehicle_status_topic = "/hmcl_vehicle_status"
            control_topic = "/hmcl_ctrl_cmd"            
            waypoint_topic = "/final_waypoints"
        
        status_topic = "is_mpc_busy"
        # Publishers
        self.control_pub = rospy.Publisher(control_topic, AckermannDrive, queue_size=1, tcp_nodelay=True)
        # Subscribers
        self.pose_sub = rospy.Subscriber(pose_topic, PoseStamped, self.pose_callback)
        self.vehicle_status_sub = rospy.Subscriber(vehicle_status_topic, CarlaEgoVehicleStatus, self.vehicle_status_callback)
        self.waypoint_sub = rospy.Subscriber(waypoint_topic, Lane, self.waypoint_callback)
        self.status_pub = rospy.Publisher(status_topic, Bool, queue_size=1)

        rate = rospy.Rate(1)
        while not rospy.is_shutdown():
            # Publish if MPC is busy with a current trajectory
            msg = Bool()
            msg.data = not (self.x_ref is None and self.pose_available)
            self.status_pub.publish(msg)
            rate.sleep()

 
    
    # def run_mpc(self, odom, recording=True):
    #     """
    #     :param odom: message from subscriber.
    #     :type odom: Odometry
    #     :param recording: If False, some messages were skipped between this and last optimization. Don't record any data
    #     during this optimization if in recording mode.
    #     """
    #     if not self.pose_available or not self.vehicle_status_available:
    #         return
    #     if not self.waypoint_available:
    #         return
    #     # Measure time between initial state was checked in and now
    #     dt = odom.header.stamp.to_time() - self.last_update_time

    #     # model_data, x_guess, u_guess = self.set_reference()        
    #     model_data = self.gp_mpc.set_reference(self.x_ref, self.y_ref,self.yaw_ref,self.vel_ref)
    
    #     # Run MPC and publish control
    #     try:
    #         tic = time.time()
    #         next_control, w_opt = self.gp_mpc.optimize(model_data)
    #         self.optimization_dt += time.time() - tic
    #         # print("MPC thread. Seq: %d. Topt: %.4f" % (odom.header.seq, (time.time() - tic) * 1000))
    #         self.control_pub.publish(next_control)
    #         if self.x_initial_reached and self.current_idx < self.w_control.shape[0]:
    #             self.w_control[self.current_idx, 0] = next_control.bodyrates.x
    #             self.w_control[self.current_idx, 1] = next_control.bodyrates.y
    #             self.w_control[self.current_idx, 2] = next_control.bodyrates.z

    #     except KeyError:
    #         self.recording_warmup = True
    #         # Should not happen anymore.
    #         rospy.logwarn("Tried to run an MPC optimization but MPC is not ready yet.")
    #         return

    #     if w_opt is not None:
    #         # Check out final states. self.recording_warmup can only be true in recording mode.
    #         if not self.recording_warmup and recording and self.x_initial_reached:
    #             x_out = np.array(self.x)[np.newaxis, :]
    #             self.rec_dict = check_out_data(self.rec_dict, x_out, None, self.w_opt, dt)

    #         self.w_opt = w_opt
    #         if self.x_initial_reached and self.current_idx < self.quad_controls.shape[0]:
    #             self.quad_controls[self.current_idx, :] = np.expand_dims(self.w_opt[:4], axis=0)

    def vehicle_status_callback(self,msg):
        if msg.velocity is None:
            return
        self.velocity = msg.velocity
        if self.vehicle_status_available is False:
            self.vehicle_status_available = True        
        

    def waypoint_callback(self, msg):
        """
        :type msg: autoware_msgs/Lane 
        """        
        if not self.waypoint_available:
            self.waypoint_available = True
            
        if len(msg.waypoints) > 0:                         
            self.x_ref = [msg.waypoints[i].pose.pose.position.x for i in range(len(msg.waypoints))]
            self.y_ref = [msg.waypoints[i].pose.pose.position.y for i in range(len(msg.waypoints))]            
            quat_to_euler_lambda = lambda o: quaternion_to_euler([o[0], o[1], o[2], o[3]])            
            self.yaw_ref = [quat_to_euler_lambda([msg.waypoints[i].pose.pose.orientation.w,msg.waypoints[i].pose.pose.orientation.x,msg.waypoints[i].pose.pose.orientation.y,msg.waypoints[i].pose.pose.orientation.z])[2] for i in range(len(msg.waypoints))]            
            self.vel_ref = [msg.waypoints[i].twist.twist.linear.x for i in range(len(msg.waypoints))]
            rospy.loginfo(self.yaw_ref)
        else:
            rospy.loginfo("Waypoints are empty")
        # rospy.loginfo("New trajectory received. Time duration: %.2f s" % self.t_ref[-1])
        rospy.loginfo("New waypoints received")
  
    def pose_callback(self, msg):
        """                
        :type msg: PoseStamped
        """        
        self.cur_x = msg.pose.position.x
        self.cur_y = msg.pose.position.y
        self.cur_z = msg.pose.position.z                
        cur_euler = quaternion_to_euler([msg.pose.orientation.w,msg.pose.orientation.x,msg.pose.orientation.y,msg.pose.orientation.z])            
        self.cur_yaw = cur_euler[2]
        
        # try:
        #     # Update the state estimate of the quad
        #     self.gp_mpc.set_state(self.cur_x,self.cur_y,self.cur_z,self.cur_yaw)
        # except AttributeError:
        #     rospy.loginfo("mpc_node......set_state fail")
        #     return

        if self.pose_available is False:
            self.pose_available = True        

        # We only optimize once every two odometry messages
        if not self.optimize_next:
            self.mpc_thread.join()
            # Count how many messages were skipped (ideally 0)
            skipped_messages = int(msg.header.seq - self.last_odom_seq_number - 1)                       
            if skipped_messages > 1:
                # Run MPC now
                # self.run_mpc(msg)
                self.last_odom_seq_number = msg.header.seq
                self.optimize_next = False
                return
            self.optimize_next = True            
            return

        def _thread_func():
            # self.run_mpc(msg)
            tt= 2
        self.mpc_thread = threading.Thread(target=_thread_func(), args=(), daemon=True)
        self.mpc_thread.start()
        self.last_odom_seq_number = msg.header.seq
        self.optimize_next = False
        rospy.loginfo("pose subscribed")


def main():
    rospy.init_node("gp_mpc")
    env = rospy.get_param('~environment', default='gazebo')
    GPMPCWrapper(env)

if __name__ == "__main__":
    main()