#!/usr/bin/env python3
import math
import numpy as np
from common.numpy_fast import interp

import cereal.messaging as messaging
from common.conversions import Conversions as CV
from common.filter_simple import FirstOrderFilter
from common.realtime import DT_MDL
from selfdrive.modeld.constants import T_IDXS
from selfdrive.controls.lib.longcontrol import LongCtrlState
from selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc import LongitudinalMpc
from selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc import T_IDXS as T_IDXS_MPC
from selfdrive.controls.lib.drive_helpers import V_CRUISE_MAX, CONTROL_N
from system.swaglog import cloudlog
from selfdrive.controls.lib.vision_turn_controller import VisionTurnController
from selfdrive.controls.lib.speed_limit_controller import SpeedLimitController, SpeedLimitResolver
from selfdrive.controls.lib.turn_speed_controller import TurnSpeedController
from selfdrive.controls.lib.events import Events

LON_MPC_STEP = 0.2  # first step is 0.2s
AWARENESS_DECEL = -0.2  # car smoothly decel at .2m/s^2 when user is distracted
A_CRUISE_MIN = -1.2
A_CRUISE_MAX_VALS = [1.2, 1.2, 0.8, 0.6]
A_CRUISE_MAX_BP = [0., 15., 25., 40.]

# Lookup table for turns
_A_TOTAL_MAX_V = [1.7, 3.2]
_A_TOTAL_MAX_BP = [20., 40.]

#DP_FOLLOWING_DIST = {
#  0: 1.0,
#  1: 1.2,
#  2: 1.4,
#  3: 1.8,
#}

DP_ACCEL_ECO = 0
DP_ACCEL_NORMAL = 1
DP_ACCEL_SPORT = 2

# accel profile by @arne182 modified by cgw
_DP_CRUISE_MIN_V = [-0.05, -0.1, -0.3, -0.4, -0.4, -0.23, -0.1]
_DP_CRUISE_MIN_V_ECO = [-0.01, -0.1, -0.2, -0.3, -0.4, -0.2, -0.09]
_DP_CRUISE_MIN_V_SPORT = [-0.1, -0.2, -0.4, -0.5, -0.5, -0.25, -0.1]
_DP_CRUISE_MIN_BP = [0.0, 3.0, 5.0, 20.0, 33.3, 40.0, 55.0]

_DP_CRUISE_MAX_V = [3.5, 1.7, 1.31, 0.95, 0.77, 0.67, 0.55, 0.47, 0.31, 0.13]
_DP_CRUISE_MAX_V_ECO = [3.5, 1.65, 1.25, 0.85, 0.65, 0.55, 0.45, 0.42, 0.24, 0.09]
_DP_CRUISE_MAX_V_SPORT = [3.5, 3.5, 2.5, 1.5, 2.0, 2.0, 2.0, 1.5, 1.0, 0.5]
_DP_CRUISE_MAX_BP = [0., 3, 6., 8., 11., 15., 20., 25., 30., 55.]

def dp_calc_cruise_accel_limits(v_ego, dp_profile):
  if dp_profile == DP_ACCEL_ECO:
    a_cruise_min = interp(v_ego, _DP_CRUISE_MIN_BP, _DP_CRUISE_MIN_V_ECO)
    a_cruise_max = interp(v_ego, _DP_CRUISE_MAX_BP, _DP_CRUISE_MAX_V_ECO)
  elif dp_profile == DP_ACCEL_SPORT:
    a_cruise_min = interp(v_ego, _DP_CRUISE_MIN_BP, _DP_CRUISE_MIN_V_SPORT)
    a_cruise_max = interp(v_ego, _DP_CRUISE_MAX_BP, _DP_CRUISE_MAX_V_SPORT)
  else:
    a_cruise_min = interp(v_ego, _DP_CRUISE_MIN_BP, _DP_CRUISE_MIN_V)
    a_cruise_max = interp(v_ego, _DP_CRUISE_MAX_BP, _DP_CRUISE_MAX_V)
  return a_cruise_min, a_cruise_max

def get_max_accel(v_ego):
  return interp(v_ego, A_CRUISE_MAX_BP, A_CRUISE_MAX_VALS)


def limit_accel_in_turns(v_ego, angle_steers, a_target, CP):
  """
  This function returns a limited long acceleration allowed, depending on the existing lateral acceleration
  this should avoid accelerating when losing the target in turns
  """

  # FIXME: This function to calculate lateral accel is incorrect and should use the VehicleModel
  # The lookup table for turns should also be updated if we do this
  a_total_max = interp(v_ego, _A_TOTAL_MAX_BP, _A_TOTAL_MAX_V)
  a_y = v_ego ** 2 * angle_steers * CV.DEG_TO_RAD / (CP.steerRatio * CP.wheelbase)
  a_x_allowed = math.sqrt(max(a_total_max ** 2 - a_y ** 2, 0.))

  return [a_target[0], min(a_target[1], a_x_allowed)]


class Planner:
  def __init__(self, CP, init_v=0.0, init_a=0.0):
    self.CP = CP
    self.mpc = LongitudinalMpc()

    self.fcw = False

    self.a_desired = init_a
    self.v_desired_filter = FirstOrderFilter(init_v, 2.0, DT_MDL)

    self.v_desired_trajectory = np.zeros(CONTROL_N)
    self.a_desired_trajectory = np.zeros(CONTROL_N)
    self.j_desired_trajectory = np.zeros(CONTROL_N)
    self.solverExecutionTime = 0.0

    # dp
    self.dp_accel_profile_ctrl = False
    self.dp_accel_profile = DP_ACCEL_ECO
    self.cruise_source = 'cruise'
    self.vision_turn_controller = VisionTurnController(CP)
    self.speed_limit_controller = SpeedLimitController()
    self.events = Events()
    self.turn_speed_controller = TurnSpeedController()

  def update(self, sm):
    v_ego = sm['carState'].vEgo

    # dp
    self.dp_accel_profile_ctrl = sm['dragonConf'].dpAccelProfileCtrl
    self.dp_accel_profile = sm['dragonConf'].dpAccelProfile

    v_cruise_kph = sm['controlsState'].vCruise
    v_cruise_kph = min(v_cruise_kph, V_CRUISE_MAX)
    v_cruise = v_cruise_kph * CV.KPH_TO_MS

    long_control_off = sm['controlsState'].longControlState == LongCtrlState.off
    force_slow_decel = sm['controlsState'].forceDecel

    # Reset current state when not engaged, or user is controlling the speed
    reset_state = long_control_off if self.CP.openpilotLongitudinalControl else not sm['controlsState'].enabled

    # No change cost when user is controlling the speed, or when standstill
    prev_accel_constraint = not (reset_state or sm['carState'].standstill)

    if reset_state:
      self.v_desired_filter.x = v_ego
      self.a_desired = 0.0

    # Prevent divergence, smooth in current v_ego
    self.v_desired_filter.x = max(0.0, self.v_desired_filter.update(v_ego))

    # Get acceleration and active solutions for custom long mpc.
    self.cruise_source, a_min_sol, v_cruise_sol = self.cruise_solutions(not reset_state, self.v_desired_filter.x,
                                                                        self.a_desired, v_cruise, sm)

    if not self.dp_accel_profile_ctrl:
      accel_limits = [A_CRUISE_MIN, get_max_accel(v_ego)]
    else:
      accel_limits = dp_calc_cruise_accel_limits(v_ego, self.dp_accel_profile)
    accel_limits_turns = limit_accel_in_turns(v_ego, sm['carState'].steeringAngleDeg, accel_limits, self.CP)
    if force_slow_decel:
      # if required so, force a smooth deceleration
      accel_limits_turns[1] = min(accel_limits_turns[1], AWARENESS_DECEL)
      accel_limits_turns[0] = min(accel_limits_turns[0], accel_limits_turns[1])

    # clip limits, cannot init MPC outside of bounds
    accel_limits_turns[0] = min(accel_limits_turns[0], self.a_desired + 0.05, a_min_sol)
    accel_limits_turns[1] = max(accel_limits_turns[1], self.a_desired - 0.05)

    self.mpc.set_accel_limits(accel_limits_turns[0], accel_limits_turns[1])
    self.mpc.set_cur_state(self.v_desired_filter.x, self.a_desired)
    self.mpc.update(sm['carState'], sm['radarState'], v_cruise_sol, prev_accel_constraint)

    self.v_desired_trajectory = np.interp(T_IDXS[:CONTROL_N], T_IDXS_MPC, self.mpc.v_solution)
    self.a_desired_trajectory = np.interp(T_IDXS[:CONTROL_N], T_IDXS_MPC, self.mpc.a_solution)
    self.j_desired_trajectory = np.interp(T_IDXS[:CONTROL_N], T_IDXS_MPC[:-1], self.mpc.j_solution)

    # TODO counter is only needed because radar is glitchy, remove once radar is gone
    self.fcw = self.mpc.crash_cnt > 5
    if self.fcw:
      cloudlog.info("FCW triggered")

    # Interpolate 0.05 seconds and save as starting point for next iteration
    a_prev = self.a_desired
    self.a_desired = float(interp(DT_MDL, T_IDXS[:CONTROL_N], self.a_desired_trajectory))
    self.v_desired_filter.x = self.v_desired_filter.x + DT_MDL * (self.a_desired + a_prev) / 2.0

  def publish(self, sm, pm):
    plan_send = messaging.new_message('longitudinalPlan')

    plan_send.valid = sm.all_checks(service_list=['carState', 'controlsState'])

    longitudinalPlan = plan_send.longitudinalPlan
    longitudinalPlan.modelMonoTime = sm.logMonoTime['modelV2']
    longitudinalPlan.processingDelay = (plan_send.logMonoTime / 1e9) - sm.logMonoTime['modelV2']

    longitudinalPlan.speeds = self.v_desired_trajectory.tolist()
    longitudinalPlan.accels = self.a_desired_trajectory.tolist()
    longitudinalPlan.jerks = self.j_desired_trajectory.tolist()

    longitudinalPlan.hasLead = sm['radarState'].leadOne.status
    longitudinalPlan.longitudinalPlanSource = self.mpc.source if self.mpc.source != 'cruise' else self.cruise_source
    longitudinalPlan.fcw = self.fcw

    longitudinalPlan.solverExecutionTime = self.mpc.solve_time

    longitudinalPlan.visionTurnControllerState = self.vision_turn_controller.state
    longitudinalPlan.visionTurnSpeed = float(self.vision_turn_controller.v_turn)

    longitudinalPlan.speedLimitControlState = self.speed_limit_controller.state
    longitudinalPlan.speedLimit = float(self.speed_limit_controller.speed_limit)
    longitudinalPlan.speedLimitOffset = float(self.speed_limit_controller.speed_limit_offset)
    longitudinalPlan.distToSpeedLimit = float(self.speed_limit_controller.distance)
    longitudinalPlan.isMapSpeedLimit = bool(self.speed_limit_controller.source == SpeedLimitResolver.Source.map_data)
    longitudinalPlan.eventsDEPRECATED = self.events.to_msg()

    longitudinalPlan.turnSpeedControlState = self.turn_speed_controller.state
    longitudinalPlan.turnSpeed = float(self.turn_speed_controller.speed_limit)
    longitudinalPlan.distToTurn = float(self.turn_speed_controller.distance)
    longitudinalPlan.turnSign = int(self.turn_speed_controller.turn_sign)

    pm.send('longitudinalPlan', plan_send)

  def cruise_solutions(self, enabled, v_ego, a_ego, v_cruise, sm):
    # Update controllers
    self.vision_turn_controller.update(enabled, v_ego, a_ego, v_cruise, sm)
    self.events = Events()
    self.speed_limit_controller.update(enabled, v_ego, a_ego, sm, v_cruise, self.events)
    self.turn_speed_controller.update(enabled, v_ego, a_ego, sm)

    # Pick solution with lowest velocity target.
    a_solutions = {'cruise': float("inf")}
    v_solutions = {'cruise': v_cruise}

    if self.vision_turn_controller.is_active:
      a_solutions['turn'] = self.vision_turn_controller.a_target
      v_solutions['turn'] = self.vision_turn_controller.v_turn

    if self.speed_limit_controller.is_active:
      a_solutions['limit'] = self.speed_limit_controller.a_target
      v_solutions['limit'] = self.speed_limit_controller.speed_limit_offseted

    if self.turn_speed_controller.is_active:
      a_solutions['turnlimit'] = self.turn_speed_controller.a_target
      v_solutions['turnlimit'] = self.turn_speed_controller.speed_limit

    source = min(v_solutions, key=v_solutions.get)

    return source, a_solutions[source], v_solutions[source]
