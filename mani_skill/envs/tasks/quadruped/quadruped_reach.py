from typing import Any, Dict

import numpy as np
import sapien
import torch
from transforms3d.euler import euler2quat

from mani_skill.agents.robots.anymal.anymal_c import ANYmalC
from mani_skill.envs.sapien_env import BaseEnv
from mani_skill.sensors.camera import CameraConfig
from mani_skill.utils import sapien_utils
from mani_skill.utils.building import actors
from mani_skill.utils.building.ground import build_ground
from mani_skill.utils.registration import register_env
from mani_skill.utils.structs.pose import Pose
from mani_skill.utils.structs.types import GPUMemoryConfig, SceneConfig, SimConfig


class QuadrupedReachEnv(BaseEnv):
    SUPPORTED_ROBOTS = ["anymal-c"]
    agent: ANYmalC

    def __init__(self, *args, robot_uids="anymal-c", **kwargs):
        super().__init__(*args, robot_uids=robot_uids, **kwargs)

    @property
    def _default_sim_config(self):
        return SimConfig(
            gpu_memory_cfg=GPUMemoryConfig(max_rigid_contact_count=2**20),
            scene_cfg=SceneConfig(
                solver_position_iterations=4, solver_velocity_iterations=0
            ),
        )

    @property
    def _default_sensor_configs(self):
        pose = sapien_utils.look_at(eye=[0.5, 0, 0.1], target=[1.0, 0, 0.0])
        return [
            CameraConfig(
                "base_camera",
                pose=pose,
                width=128,
                height=128,
                fov=np.pi / 2,
                near=0.01,
                far=100,
                mount=self.agent.robot.links[0],
            )
        ]

    @property
    def _default_human_render_camera_configs(self):
        pose = sapien_utils.look_at([-2.5, 0.5, 2], [0.0, 0.0, 0])
        return [
            CameraConfig(
                "render_camera",
                pose=pose,
                width=512,
                height=512,
                fov=1,
                near=0.01,
                far=100,
                mount=self.agent.robot.links[0],
            )
        ]

    def _load_scene(self, options: dict):
        self.ground = build_ground(self._scene)
        self.goal = actors.build_sphere(
            self._scene,
            radius=0.2,
            color=[0, 1, 0, 1],
            name="goal",
            add_collision=False,
            body_type="kinematic",
        )

    def _initialize_episode(self, env_idx: torch.Tensor, options: dict):
        with torch.device(self.device):
            b = len(env_idx)
            keyframe = self.agent.keyframes["standing"]
            self.agent.robot.set_pose(keyframe.pose)
            self.agent.robot.set_qpos(keyframe.qpos)
            # sample random goal
            xyz = torch.zeros((b, 3))
            noise_scale = 1
            xyz[:, 0] = torch.rand(size=(b,)) * noise_scale - noise_scale / 2 + 2.5
            noise_scale = 4
            xyz[:, 1] = torch.rand(size=(b,)) * noise_scale - noise_scale / 2
            self.goal.set_pose(Pose.create_from_pq(xyz))

    def evaluate(self):
        is_fallen = self.agent.is_fallen()
        robot_to_goal_dist = torch.linalg.norm(
            self.goal.pose.p[:, :2] - self.agent.robot.pose.p[:, :2], axis=1
        )
        reached_goal = robot_to_goal_dist < 0.35
        return {
            "success": reached_goal & ~is_fallen,
            "fail": is_fallen,
            "robot_to_goal_dist": robot_to_goal_dist,
            "reached_goal": reached_goal,
            "is_fallen": is_fallen,
        }

    def _get_obs_extra(self, info: Dict):
        obs = dict()
        if self.obs_mode in ["state", "state_dict"]:
            obs.update(
                goal_pos=self.goal.pose.p[:, :2],
                robot_to_goal=self.goal.pose.p[:, :2] - self.agent.robot.pose.p[:, :2],
            )
        return obs

    def compute_dense_reward(self, obs: Any, action: torch.Tensor, info: Dict):
        robot_to_goal_dist = info["robot_to_goal_dist"]
        reaching_reward = 1 - torch.tanh(1 * robot_to_goal_dist)

        # various penalties:
        lin_vel_z_l2 = torch.square(self.agent.robot.root_linear_velocity[:, 2])
        ang_vel_xy_l2 = (
            torch.square(self.agent.robot.root_angular_velocity[:, :2])
        ).sum(axis=1)
        penalties = lin_vel_z_l2 * -0.15 + ang_vel_xy_l2 * -0.05
        reward = reaching_reward + penalties
        return reward

    def compute_normalized_dense_reward(
        self, obs: Any, action: torch.Tensor, info: Dict
    ):
        max_reward = 1.0
        return self.compute_dense_reward(obs=obs, action=action, info=info) / max_reward


# @register_env("AnymalC-Reach-v1", max_episode_steps=200)
class AnymalCReachEnv(QuadrupedReachEnv):
    def __init__(self, *args, robot_uids="anymal-c", **kwargs):
        super().__init__(*args, robot_uids=robot_uids, **kwargs)
