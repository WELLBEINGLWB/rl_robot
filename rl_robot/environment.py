import sys
import math
import random
import time

import vrep
from pybrain.rl.environments.environment import Environment as PybrainEnvironment

from joint_constants import *


# Note, this is tightly coupled with the currently used scene file. It's not ideal
# to do this like this, but it'll work for now.
POTENTIAL_GOAL_AREAS = [
    [[-0.225, -1.525, 0.7], [0.6, -0.5, -0.1]],
    [[-0.3, -1.25, 0.1], [0.275, -0.825, 0.425]],
    [[0.325, -1.25, 0.1], [0.25, -0.825, 0.425]]
]


class ConnectionException(Exception):
    pass


class SimulatorException(Exception):
    pass


class Environment(PybrainEnvironment):

    def __init__(self, address, port, scene_file):
        self._scene_file = scene_file
        self._client_id = vrep.simxStart(address, port, True, True, 5000, 5)

        if self._client_id == -1:
            raise ConnectionException('Could not connect')

        # set joint angles and start streaming
        self.reset()
        self.getSensors()

    def isColliding(self):
        return self._is_colliding

    def distanceFromGoal(self):
        return self._distance_from_goal

    def getSensors(self):
        # If we haven't moved since the last sensor reading, our old sensor readings are still correct.
        if self._current_sensor_step == self._current_action_step:
            return self._sensor_data_vector

        self._check_for_collisions()
        self._get_goal_distance_data()
        self._get_proximity_sensor_distances()
        self._generate_sensor_data_vector()

        self._current_sensor_step = self._current_action_step

        return self._sensor_data_vector

    def performAction(self, deltas):
        # just pass in a list of joint angle changes that matches the order in state (see: self._joint_positions)
        if len(deltas) != len(JOINTS):
            raise SimulatorException('Given deltas object length [{}] does not match num joints [{}]').format(len(deltas), len(JOINTS))

        # Increment joint positions by action and apply to the robot
        # TODO figure out how to specify how many deltas Learner can provide to env and max / min delta value
        # TODO figure out how to set joint limits as well
        self._joint_positions = [(jp[0], jp[1] + djp) for jp, djp in zip(self._joint_positions, deltas)]
        self._apply_all_joint_positions()
        self._current_action_step += 1

    def reset(self):
        # first load up the original scene
        scene_did_load = vrep.simxLoadScene(self._client_id, self._scene_file, 0, vrep.simx_opmode_blocking)
        if scene_did_load != vrep.simx_return_ok:
            raise SimulatorException('Could not load scene')

        # get collision handles, joint handles, etc.
        self._scene_handles = self._load_scene_handles()

        self._goal_point = [0, 0, 0]
        self._current_action_step = 0
        self._current_sensor_step = None
        # Tuple of joint handle and current position
        self._joint_positions = [(None, 0)] * len(JOINTS)
        self._distance_from_goal = None
        # TODO how do we represent this?
        self._angle_to_goal = None
        self._proximity_sensor_distances = [sys.maxint] * len(PROXIMITY_SENSORS)
        self._is_colliding = False
        self._sensor_data_vector = None

        self._generate_goal_position()

        vrep.simxStartSimulation(self._client_id, vrep.simx_opmode_blocking)
        time.sleep(0.1)

        # get collision handles, joint handles, etc.
        self._scene_handles = self._load_scene_handles()
        self._start_streaming()

        # sleep after streaming starts for data to roll in
        time.sleep(1)

        # We want to get sensors once so that things like _distance_from_goal are properly initialized
        self.getSensors()

    def teardown(self):
        print 'Stopping data streaming for all collisions.'
        for collision in COLLISION_OBJECTS:
            collision_handle = self._scene_handles[collision]
            code = vrep.simxReadCollision(self._client_id, collision_handle, vrep.simx_opmode_discontinue)[0]
            if code != vrep.simx_return_ok:
                raise Exception('Failed to stop streaming from a collision object.')

        print 'Stopping data streaming for all proximity sensors.'
        for sensor in PROXIMITY_SENSORS:
            sensor_handle = self._scene_handles[sensor]
            code = vrep.simxReadProximitySensor(self._client_id, sensor_handle, vrep.simx_opmode_discontinue)[0]
            if code != vrep.simx_return_ok:
                raise Exception('Failed to stop streaming from a sensor.')

        # pray that graceful shutdown actually happened
        print 'Stopping simulation.'

        vrep.simxStopSimulation(self._client_id, vrep.simx_opmode_blocking)
        time.sleep(0.1)

        return True

    # Scene Configuration Helper Functions ----------------------------------------------------------------
    def _load_scene_handles(self):
        def handle_failure(name):
            raise SimulatorException('Failed to get handle for {}'.format(name))

        # load all objects
        print 'Loading all objects...'
        object_handles = {}
        for obj_name in LINKS + JOINTS + PROXIMITY_SENSORS:
            code, handle = vrep.simxGetObjectHandle(self._client_id, obj_name, vrep.simx_opmode_blocking)

            print code
            if code == vrep.simx_return_ok:
                object_handles[obj_name] = handle
            else:
                handle_failure(obj_name)
        # load collisions -- use the same map as for objects
        print 'Loading all collisions...'
        for coll_name in COLLISION_OBJECTS:
            print code
            code, handle = vrep.simxGetCollisionHandle(self._client_id, coll_name, vrep.simx_opmode_blocking)

            if code == vrep.simx_return_ok:
                object_handles[coll_name] = handle
            else:
                handle_failure(coll_name)

        for index, joint in enumerate(JOINTS):
            self._joint_positions[index] = (object_handles[joint], 0)

        return object_handles

    def _start_streaming(self):
        print 'Beginning to stream all collisions.'
        for collision in COLLISION_OBJECTS:
            collision_handle = self._scene_handles[collision]
            code = vrep.simxReadCollision(self._client_id, collision_handle, vrep.simx_opmode_streaming)[0]
            if code != vrep.simx_return_novalue_flag:
                raise SimulatorException('Failed to start streaming for collision object {}'.format(collision))

        print 'Beginning to stream data for all proximity sensors.'
        for sensor in PROXIMITY_SENSORS:
            sensor_handle = self._scene_handles[sensor]
            code = vrep.simxReadProximitySensor(self._client_id, sensor_handle, vrep.simx_opmode_streaming)[0]
            if code != vrep.simx_return_novalue_flag:
                raise SimulatorException('Failed to start streaming for proximity sensor {}'.format(sensor))


    def _generate_goal_position(self):
        goal_area = random.choice(POTENTIAL_GOAL_AREAS)
        lower_bound = goal_area[0]
        ranges = goal_area[1]

        for i in range(3):
            self._goal_point[i] = lower_bound[i] + (random.random() * self.ranges[i])

    def _get_goal_distance_data(self):
        # TODO
        pass

    def _get_proximity_sensor_distances(self):
        self._normals = []
        self._distances = []

        for sensor in PROXIMITY_SENSORS:
            sensor_handle = self._scene_handles[sensor]
            code, state, point, handle, normal = vrep.simxReadProximitySensor(self._client_id, sensor_handle, vrep.simx_opmode_buffer)
            if code == vrep.simx_return_ok:
                self._normals.append(normal)
                self._distance_from_sensor_to_point(sensor, point)
            else:
                raise SimulatorException('Failed to stream from sensor [{}].'.format(sensor))

        print 'Finished loading all normals and distances...'


    def _generate_sensor_data_vector(self):
        # TODO
        pass

    def _apply_all_joint_positions(self):
        print 'Moving robot to new configuration: {}'.format(map(lambda x: x[1], positions_to_apply))
        vrep.simxPauseCommunication(self._client_id, True)

        for joint_handle, position in self._joint_positions:
            code = vrep.simxSetJointPosition(self._client_id, joint_handle, position, vrep.simx_opmode_blocking)

            if code != vrep.simx_return_ok:
                raise SimulatorException('[Code {}] Failed to move joint {} with handle {} to position {}.')\
                    .format(code, joint, object_handle, position)

        vrep.simxPauseCommunication(self._client_id, True)

    def _check_for_collisions(self):
        self._is_colliding = False

        for collision_object_name in COLLISION_OBJECTS:
            collision_handle = self._scene_handles[collision_object_name]
            code, state = vrep.simxReadCollision(self._client_id, collision_handle, vrep.simx_opmode_buffer)

            if code == vrep.simx_return_ok and state:
                print 'COLLISION DETECTED WITH {}'.format(collision_object_name)
                self._is_colliding = True
                return

    # Helper function -------------------------------------------------------------------------------------
    def _distance_from_sensor_to_point(self, sensor_name, point):
        # TODO make this function get the distance between the sensor with sensor_name and the point (x, y, z)
        print 'Sensor Name {}'.format(sensor_name)
        print 'Point {}'.format(point)
        return 1
