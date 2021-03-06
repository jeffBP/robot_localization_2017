#!/usr/bin/env python

""" This is the starter code for the robot localization project """

import rospy

from std_msgs.msg import Header, String
from sensor_msgs.msg import LaserScan, PointCloud
from geometry_msgs.msg import PoseStamped, PoseWithCovarianceStamped, PoseArray, Pose, Point, Quaternion
from nav_msgs.srv import GetMap
from nav_msgs.msg import OccupancyGrid
from copy import deepcopy

import tf
from tf import TransformListener
from tf import TransformBroadcaster
from tf.transformations import euler_from_quaternion, rotation_matrix, quaternion_from_matrix
from random import gauss

import math
import time

import numpy as np
from numpy.random import random_sample
from sklearn.neighbors import NearestNeighbors
from occupancy_field import OccupancyField

from helper_functions import (convert_pose_inverse_transform,
                              convert_translation_rotation_to_pose,
                              convert_pose_to_xy_and_theta,
                              angle_diff)

class Particle(object):
    """ Represents a hypothesis (particle) of the robot's pose consisting of x,y and theta (yaw)
        Attributes:
            x: the x-coordinate of the hypothesis relative to the map frame
            y: the y-coordinate of the hypothesis relative ot the map frame
            theta: the yaw of the hypothesis relative to the map frame
            w: the particle weight (the class does not ensure that particle weights are normalized
    """

    def __init__(self,x=0.0,y=0.0,theta=0.0,w=1.0):
        """ Construct a new Particle
            x: the x-coordinate of the hypothesis relative to the map frame
            y: the y-coordinate of the hypothesis relative ot the map frame
            theta: the yaw of the hypothesis relative to the map frame
            w: the particle weight (the class does not ensure that particle weights are normalized """
        self.w = w
        self.theta = theta
        self.x = x
        self.y = y

    def projectScan(self, scanList):
        """
        Project LIDAR scan onto particle
        scanList: LIDAR ranges
        projection: list of x, y coordinates of LIDAR scan ranges on top
        of particle
        """
        projection = []

        for i, dist in enumerate(scanList):
            #Loop through LIDAR scan
            if dist:
                #If scan exists

                #Add angle of scan to angle of particle, modulo 360 to re-standardize
                newAngle = (math.degrees(self.theta) + i % 360)

                #get x and y position of LIDAR scan at angle newAngle
                projX, projY = self.getXY(newAngle, dist)

                #Add x and y to projection list
                projection.append((projX+self.x, projY+self.y))

        return projection


    @staticmethod
    def getXY(theta, r):
        """
        Converts polar to cartesian
        theta: angle from origin
        r: distance from origin
        """
        x = r*math.cos(math.radians(theta)) #x = rcos(theta)
        y = r*math.sin(math.radians(theta)) #y = rsin(theta)
        return x, y

    def as_pose(self):
        """ A helper function to convert a particle to a geometry_msgs/Pose message """
        orientation_tuple = tf.transformations.quaternion_from_euler(0,0,self.theta)
        return Pose(position=Point(x=self.x,y=self.y,z=0), orientation=Quaternion(x=orientation_tuple[0], y=orientation_tuple[1], z=orientation_tuple[2], w=orientation_tuple[3]))


class ParticleFilter:
    """ The class that represents a Particle Filter ROS Node
        Attributes list:
            initialized: a Boolean flag to communicate to other class methods that initializaiton is complete
            base_frame: the name of the robot base coordinate frame (should be "base_link" for most robots)
            map_frame: the name of the map coordinate frame (should be "map" in most cases)
            odom_frame: the name of the odometry coordinate frame (should be "odom" in most cases)
            scan_topic: the name of the scan topic to listen to (should be "scan" in most cases)
            n_particles: the number of particles in the filter
            d_thresh: the amount of linear movement before triggering a filter update
            a_thresh: the amount of angular movement before triggering a filter update
            laser_max_distance: the maximum distance to an obstacle we should use in a likelihood calculation
            pose_listener: a subscriber that listens for new approximate pose estimates (i.e. generated through the rviz GUI)
            particle_pub: a publisher for the particle cloud
            laser_subscriber: listens for new scan data on topic self.scan_topic
            tf_listener: listener for coordinate transforms
            tf_broadcaster: broadcaster for coordinate transforms
            particle_cloud: a list of particles representing a probability distribution over robot poses
            current_odom_xy_theta: the pose of the robot in the odometry frame when the last filter update was performed.
                                   The pose is expressed as a list [x,y,theta] (where theta is the yaw)
            map: the map we will be localizing ourselves in.  The map should be of type nav_msgs/OccupancyGrid
    """
    def __init__(self):
        self.initialized = False        # make sure we don't perform updates before everything is setup
        rospy.init_node('pf')           # tell roscore that we are creating a new node named "pf"

        self.base_frame = "base_link"   # the frame of the robot base
        self.map_frame = "map"          # the name of the map coordinate frame
        self.odom_frame = "odom"        # the name of the odometry coordinate frame
        self.scan_topic = "scan"        # the topic where we will get laser scans from
        self.kP = 0.5
        self.n_particles = 100          # the number of particles to use

        self.d_thresh = 0.2             # the amount of linear movement before performing an update
        self.a_thresh = math.pi/6       # the amount of angular movement before performing an update

        self.laser_max_distance = 2.0   # maximum penalty to assess in the likelihood field model

        self.particle_pos_std = .025     # standard deviation for gaussian partical position distribution (meters)
        self.particle_angle_std = np.pi/8 # standard deviation for guassian particle angle  distribution (radians)

        self.pos_update_std = .04        # standard deviation for updating each particle's location
        self.angle_update_std = np.pi/10# standard deviation for updating each particle's angle
        # TODO: define additional constants if needed

        # Setup pubs and subs

        # pose_listener responds to selection of a new approximate robot location (for instance using rviz)
        rospy.Subscriber("initialpose", PoseWithCovarianceStamped, self.update_initial_pose)

        # publish the current particle cloud.  This enables viewing particles in rviz.
        self.particle_pub = rospy.Publisher("particlecloud", PoseArray, queue_size=10)

        # laser_subscriber listens for data from the lidar
        rospy.Subscriber(self.scan_topic, LaserScan, self.scan_received)

        # enable listening for and broadcasting coordinate transforms
        self.tf_listener = TransformListener()
        self.tf_broadcaster = TransformBroadcaster()

        self.particle_cloud = []

        # change use_projected_stable_scan to True to use point clouds instead of laser scans
        self.use_projected_stable_scan = False
        self.last_projected_stable_scan = None
        if self.use_projected_stable_scan:
            # subscriber to the odom point cloud
            rospy.Subscriber("projected_stable_scan", PointCloud, self.projected_scan_received)

        self.current_odom_xy_theta = []

        # request the map from the map server, the map should be of type nav_msgs/OccupancyGrid
        self.map = None
        self.get_map_client()

        # for now we have commented out the occupancy field initialization until you can successfully fetch the map
        #self.occupancy_field = OccupancyField(map)
        self.initialized = True



    def get_map_client(self):
        """
        Gets the map from map_server
        """
        print("Getting Map")
        rospy.wait_for_service('static_map')  #Waits for map on topic static_map
        try:
            #try to get map
            get_map = rospy.ServiceProxy('static_map', GetMap)

            #store map
            recieved_map = get_map()

            #store occupancy field
            self.map = OccupancyField(recieved_map.map)

            print("We have the map.")
        except rospy.ServiceException, e:
            #If map doesn't exist
            print("Map couldn't be retrieved.")

    def update_robot_pose(self):
        """ Update the estimate of the robot's pose given the updated particles.
            There are two logical methods for this:
                (1): compute the mean pose
                (2): compute the most likely pose (i.e. the mode of the distribution)
        """
        # first make sure that the particle weights are normalized
        self.normalize_particles()

        # TODO: assign the lastest pose into self.robot_pose as a geometry_msgs.Pose object
        # just to get started we will fix the robot's pose to always be at the origin
        xSum = 0
        ySum = 0
        thetaSum = 0

        #Average unit vector for x, y coordinates
        average_x_dir = np.mean([pt.w*np.cos(pt.theta) for pt in self.particle_cloud])
        average_y_dir = np.mean([pt.w*np.sin(pt.theta) for pt in self.particle_cloud])
        #find angle of average unit vector
        thetaSum = math.atan2(average_y_dir, average_x_dir)

        for i, pt in enumerate(self.particle_cloud):
            #average x and y positions of particles
            xSum += pt.x*pt.w
            ySum += pt.y*pt.w

        #change robot pose to average angle and position
        orientation_tuple = tf.transformations.quaternion_from_euler(0,0,thetaSum)
        self.robot_pose = Pose(position=Point(x=xSum, y=ySum, z=0),
                orientation=Quaternion(x=orientation_tuple[0], y=orientation_tuple[1], z=orientation_tuple[2], w=orientation_tuple[3]))



    def projected_scan_received(self, msg):
        """
        Store LIDAR scan
        msg: LIDAR ranges
        """

        self.last_projected_stable_scan = msg

    def update_particles_with_odom(self, msg):
        """ Update the particles using the newly given odometry pose.
            The function computes the value delta which is a tuple (x,y,theta)
            that indicates the change in position and angle between the odometry
            when the particles were last updated and the current odometry.

            msg: this is not really needed to implement this, but is here just in case.
        """
        new_odom_xy_theta = convert_pose_to_xy_and_theta(self.odom_pose.pose)
        # compute the change in x,y,theta since our last update
        if self.current_odom_xy_theta:
            old_odom_xy_theta = self.current_odom_xy_theta
            delta = (new_odom_xy_theta[0] - self.current_odom_xy_theta[0],
                     new_odom_xy_theta[1] - self.current_odom_xy_theta[1],
                     new_odom_xy_theta[2] - self.current_odom_xy_theta[2])

            self.current_odom_xy_theta = new_odom_xy_theta
        else:
            self.current_odom_xy_theta = new_odom_xy_theta
            return


        #Assume robot rotates, moves forward, then rotates again
        #Calculate first rotation angle
        r1 = math.atan2(delta[1], delta[0]) - self.current_odom_xy_theta[2]

        #calculate distance of movement
        d = math.hypot(delta[0], delta[1])

        #calculate second rotation angle
        r2 = delta[2] - r1

        for i, pt in enumerate(self.particle_cloud):
            #Change each particle's coordinates

            #Update particle angle with r1
            pt.theta += np.random.normal(r1, self.angle_update_std)

            #Add noise to distance moved
            dist = np.random.normal(d, self.particle_pos_std)

            #Adjust x and y for distance moved
            pt.x += dist*math.cos(pt.theta)
            pt.y += dist*math.sin(pt.theta)

            #Update particle angle with r2
            pt.theta += np.random.normal(r2, self.angle_update_std)


    def map_calc_range(self,x,y,theta):
        """ Difficulty Level 3: implement a ray tracing likelihood model... Let me know if you are interested """
        # TODO: nothing unless you want to try this alternate likelihood model
        pass


    def resample_particles(self):
        """ Resample the particles according to the new particle weights.
            The weights stored with each particle should define the probability that a particular
            particle is selected in the resampling step.  You may want to make use of the given helper
            function draw_random_sample.
        """
        #Make sure the distribution is normalized
        self.normalize_particles()
        #Get random sample of particles
        self.particle_cloud = self.draw_random_sample(self.particle_cloud, [p.w for p in self.particle_cloud], len(self.particle_cloud))


    def update_particles_with_laser(self, msg):
        """ Updates the particle weights in response to the scan contained in the msg """
        print("particles updated.")
        #min_distance = min([d for d in msg.ranges if d]) #minimum of all nonzero points
        for i, p in enumerate(self.particle_cloud):
            #Loop through particles
            hits = 0
            projection = p.projectScan(msg.ranges) #Project LIDAR Scan onto particle

            for pt in projection:
                #If point is within a certain distance from the wall
                 if self.map.get_closest_obstacle_distance(pt[0],pt[1]) < 0.05:
                     #Increment hits
                     hits += 1
            print(hits)
            if hits:
                #If hits exists, set the weight of particle equal to it
                p.w = hits
            else:
                #If hits doesn't exist, set it's weight
                p.w = .6


    @staticmethod
    def draw_random_sample(choices, probabilities, n):
        """ Return a random sample of n elements from the set choices with the specified probabilities
            choices: the values to sample from represented as a list
            probabilities: the probability of selecting each element in choices represented as a list
            n: the number of samples
        """
        values = np.array(range(len(choices)))
        probs = np.array(probabilities)
        bins = np.add.accumulate(probs)
        inds = values[np.digitize(random_sample(n), bins)]
        samples = []
        for i in inds:
            samples.append(deepcopy(choices[int(i)]))
        return samples

    def update_initial_pose(self, msg):
        """ Callback function to handle re-initializing the particle filter based on a pose estimate.
            These pose estimates could be generated by another ROS Node or could come from the rviz GUI """
        xy_theta = convert_pose_to_xy_and_theta(msg.pose.pose)
        self.initialize_particle_cloud(xy_theta)
        self.fix_map_to_odom_transform(msg)

    def initialize_particle_cloud(self, xy_theta=None):
        """ Initialize the particle cloud.
            Arguments
            xy_theta: a triple consisting of the mean x, y, and theta (yaw) to initialize the
                      particle cloud around.  If this input is ommitted, the odometry will be used """


        if xy_theta == None:
            xy_theta = convert_pose_to_xy_and_theta(self.odom_pose.pose)

        self.particle_cloud = []

        for i in xrange(self.n_particles):
            #Create n_particles number of particles

            #Set x, y, theta of particle to sample from normal distribution
            #around robot
            x = np.random.normal(xy_theta[0], self.particle_pos_std)
            y = np.random.normal(xy_theta[1], self.particle_pos_std)
            theta = np.random.normal(xy_theta[2], self.particle_angle_std)

            #Add particle to the list
            self.particle_cloud.append(Particle(x, y, theta))

        #Normalize particles and update robot position
        self.normalize_particles()
        self.update_robot_pose()

    def normalize_particles(self):
        """ Make sure the particle weights define a valid distribution (i.e. sum to 1.0) """
        total_weight = sum([p.w for p in self.particle_cloud])
        for i, p in enumerate(self.particle_cloud):
            self.particle_cloud[i].w = p.w/float(total_weight)

    def publish_particles(self, msg):
        """
        Publishes particles as array of Poses
        for visualization in rviz
        """
        particles_conv = []
        for p in self.particle_cloud:
            particles_conv.append(p.as_pose())
        # actually send the message so that we can view it in rviz
        self.particle_pub.publish(PoseArray(header=Header(stamp=rospy.Time.now(),
                                            frame_id=self.map_frame),
                                  poses=particles_conv))

    def scan_received(self, msg):
        """ This is the default logic for what to do when processing scan data.
            Feel free to modify this, however, I hope it will provide a good
            guide.  The input msg is an object of type sensor_msgs/LaserScan """
        if not(self.initialized):
            # wait for initialization to complete
            return

        if not(self.tf_listener.canTransform(self.base_frame,msg.header.frame_id,msg.header.stamp)):
            # need to know how to transform the laser to the base frame
            # this will be given by either Gazebo or neato_node
            return

        if not(self.tf_listener.canTransform(self.base_frame,self.odom_frame,msg.header.stamp)):
            # need to know how to transform between base and odometric frames
            # this will eventually be published by either Gazebo or neato_node
            return

        # calculate pose of laser relative ot the robot base
        p = PoseStamped(header=Header(stamp=rospy.Time(0),
                                      frame_id=msg.header.frame_id))
        self.laser_pose = self.tf_listener.transformPose(self.base_frame,p)

        # find out where the robot thinks it is based on its odometry
        p = PoseStamped(header=Header(stamp=msg.header.stamp,
                                      frame_id=self.base_frame),
                        pose=Pose())
        self.odom_pose = self.tf_listener.transformPose(self.odom_frame, p)
        # store the the odometry pose in a more convenient format (x,y,theta)
        new_odom_xy_theta = convert_pose_to_xy_and_theta(self.odom_pose.pose)
        if not(self.particle_cloud):
            # now that we have all of the necessary transforms we can update the particle cloud
            self.initialize_particle_cloud()
            # cache the last odometric pose so we can only update our particle filter if we move more than self.d_thresh or self.a_thresh
            self.current_odom_xy_theta = new_odom_xy_theta
            # update our map to odom transform now that the particles are initialized
            self.fix_map_to_odom_transform(msg)
        elif (math.fabs(new_odom_xy_theta[0] - self.current_odom_xy_theta[0]) > self.d_thresh or
              math.fabs(new_odom_xy_theta[1] - self.current_odom_xy_theta[1]) > self.d_thresh or
              math.fabs(new_odom_xy_theta[2] - self.current_odom_xy_theta[2]) > self.a_thresh):
            print("Doing an update.")
            # we have moved far enough to do an update!
            self.update_particles_with_odom(msg)    # update based on odometry
            if self.last_projected_stable_scan:
                last_projected_scan_timeshift = deepcopy(self.last_projected_stable_scan)
                last_projected_scan_timeshift.header.stamp = msg.header.stamp
                self.scan_in_base_link = self.tf_listener.transformPointCloud("base_link", last_projected_scan_timeshift)

            self.update_particles_with_laser(msg)   # update based on laser scan
            self.update_robot_pose()                # update robot's pose
            self.resample_particles()               # resample particles to focus on areas of high density
            self.fix_map_to_odom_transform(msg)     # update map to odom transform now that we have new particles
        # publish particles (so things like rviz can see them)
        self.publish_particles(msg)

    def fix_map_to_odom_transform(self, msg):
        """ This method constantly updates the offset of the map and
            odometry coordinate systems based on the latest results from
            the localizer
            TODO: if you want to learn a lot about tf, reimplement this... I can provide
                  you with some hints as to what is going on here. """
        (translation, rotation) = convert_pose_inverse_transform(self.robot_pose)
        p = PoseStamped(pose=convert_translation_rotation_to_pose(translation,rotation),
                        header=Header(stamp=msg.header.stamp,frame_id=self.base_frame))
        self.tf_listener.waitForTransform(self.base_frame, self.odom_frame, msg.header.stamp, rospy.Duration(1.0))
        self.odom_to_map = self.tf_listener.transformPose(self.odom_frame, p)
        (self.translation, self.rotation) = convert_pose_inverse_transform(self.odom_to_map.pose)

    def broadcast_last_transform(self):
        """ Make sure that we are always broadcasting the last map
            to odom transformation.  This is necessary so things like
            move_base can work properly. """
        if not(hasattr(self,'translation') and hasattr(self,'rotation')):
            return
        self.tf_broadcaster.sendTransform(self.translation,
                                          self.rotation,
                                          rospy.get_rostime(),
                                          self.odom_frame,
                                          self.map_frame)

if __name__ == '__main__':
    n = ParticleFilter()
    r = rospy.Rate(5)

    while not(rospy.is_shutdown()):
        # in the main loop all we do is continuously broadcast the latest map to odom transform
        n.broadcast_last_transform()
        r.sleep()
