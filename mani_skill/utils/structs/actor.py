from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, List, Literal, Union

import numpy as np
import sapien
import sapien.physx as physx
import sapien.render
import torch

from mani_skill.utils import common
from mani_skill.utils.structs.base import PhysxRigidDynamicComponentStruct
from mani_skill.utils.structs.pose import Pose, to_sapien_pose, vectorize_pose
from mani_skill.utils.structs.types import Array

if TYPE_CHECKING:
    from mani_skill.envs.scene import ManiSkillScene


@dataclass
class Actor(PhysxRigidDynamicComponentStruct[sapien.Entity]):
    """
    Wrapper around sapien.Entity objects mixed in with useful properties from the RigidBodyDynamicComponent components

    At the moment, on GPU and CPU one can query pose, linear velocity, and angular velocity easily

    On CPU, more properties are available
    """

    px_body_type: Literal["kinematic", "static", "dynamic"] = None
    hidden: bool = False

    initial_pose: Pose = None
    """
    The initial pose of this Actor, as defined when creating the actor via the ActorBuilder. It is necessary to track
    this pose to ensure the actor is still at the correct pose once gpu system is initialized. It may also be useful
    to help reset a environment to an initial state without having to manage initial poses yourself
    """
    name: str = None

    merged: bool = False
    """Whether this object is a view of other actors as a result of Actor.merge"""

    def __hash__(self):
        return self._objs[0].__hash__()

    def __str__(self):
        return f"<{self.name}: struct of type {self.__class__}; managing {self._num_objs} {self._objs[0].__class__} objects>"

    def __repr__(self):
        return self.__str__()

    @classmethod
    def create_from_entities(
        cls,
        entities: List[sapien.Entity],
        scene: ManiSkillScene,
        scene_idxs: torch.Tensor,
    ):

        shared_name = "_".join(entities[0].name.split("_")[1:])
        bodies = [
            ent.find_component_by_type(physx.PhysxRigidDynamicComponent)
            for ent in entities
        ]

        # Objects with collision shapes have either PhysxRigidDynamicComponent (Kinematic, Dynamic) or PhysxRigidStaticComponent (Static)
        px_body_type = "static"
        if bodies[0] is not None:
            if bodies[0].kinematic:
                px_body_type = "kinematic"
            else:
                px_body_type = "dynamic"
        else:
            bodies = [
                ent.find_component_by_type(physx.PhysxRigidStaticComponent)
                for ent in entities
            ]
        return cls(
            _objs=entities,
            _scene=scene,
            _scene_idxs=scene_idxs,
            px_body_type=px_body_type,
            _bodies=bodies,
            _body_data_name="cuda_rigid_body_data"
            if isinstance(scene.px, physx.PhysxGpuSystem)
            else None,
            name=shared_name,
        )

    @classmethod
    def merge(cls, actors: List["Actor"], name: str = None):
        """
        Merge actors together under one view so that they can all be managed by one python dataclass object.
        This can be useful for e.g. randomizing the asset loaded into a task and being able to do object.pose to fetch the pose of all randomized assets
        or object.set_pose to change the pose of each of the different assets, despite the assets not being uniform across all sub-scenes.

        For example usage of this method, see mani_skill /envs/tasks/pick_single_ycb.py

        Args:
            actors (List[Actor]): The actors to merge into one actor object to manage
            name (str): A new name to give the merged actors. If none, the name will default to the first actor's name
        """
        objs = []
        scene = actors[0]._scene
        _builder_initial_poses = []
        merged_scene_idxs = []
        actors[0]._num_objs
        for actor in actors:
            objs += actor._objs
            merged_scene_idxs.append(actor._scene_idxs)
            _builder_initial_poses.append(actor.initial_pose.raw_pose)
        merged_scene_idxs = torch.concat(merged_scene_idxs)
        merged_actor = Actor.create_from_entities(objs, scene, merged_scene_idxs)
        merged_actor.name = name
        merged_actor.initial_pose = Pose.create(torch.vstack(_builder_initial_poses))
        merged_actor.merged = True
        scene.actor_views[merged_actor.name] = merged_actor
        return merged_actor

    # -------------------------------------------------------------------------- #
    # Additional useful functions not in SAPIEN original API
    # -------------------------------------------------------------------------- #

    def get_state(self):
        pose = self.pose
        if self.px_body_type != "dynamic":
            vel = torch.zeros((self._num_objs, 3), device=self.device)
            ang_vel = torch.zeros((self._num_objs, 3), device=self.device)
        else:
            vel = self.get_linear_velocity()  # [N, 3]
            ang_vel = self.get_angular_velocity()  # [N, 3]
        return torch.hstack([pose.p, pose.q, vel, ang_vel])

    def set_state(self, state: Array):
        if physx.is_gpu_enabled():
            state = common.to_tensor(state)
            self.set_pose(Pose.create(state[:, :7]))
            self.set_linear_velocity(state[:, 7:10])
            self.set_angular_velocity(state[:, 10:13])
        else:
            state = common.to_numpy(state[0])
            self.set_pose(sapien.Pose(state[0:3], state[3:7]))
            if self.px_body_type == "dynamic":
                self.set_linear_velocity(state[7:10])
                self.set_angular_velocity(state[10:13])

    @cache
    def has_collision_shapes(self):
        assert (
            not self.merged
        ), "Check if a merged actor has collision shape is not supported as the managed objects could all be very different"
        return (
            len(
                self._objs[0]
                .find_component_by_type(physx.PhysxRigidDynamicComponent)
                .collision_shapes
            )
            > 0
        )

    def hide_visual(self):
        """
        Hides this actor from view. In CPU simulation the visual body is simply set to visibility 0

        For GPU simulation, currently this is implemented by moving the actor very far away as visiblity cannot be changed on the fly.
        As a result we do not permit hiding and showing visuals of objects with collision shapes as this affects the actual simulation.
        Note that this operation can also be fairly slow as we need to run px.gpu_apply_rigid_dynamic_data and px.gpu_fetch_rigid_dynamic_data.
        """
        assert not self.has_collision_shapes()
        if self.hidden:
            return
        if physx.is_gpu_enabled():
            self.before_hide_pose = self.px.cuda_rigid_body_data.torch()[
                self._body_data_index, :7
            ].clone()

            temp_pose = self.pose.raw_pose
            temp_pose[..., :3] += 99999
            self.pose = temp_pose
            self.px.gpu_apply_rigid_dynamic_data()
            self.px.gpu_fetch_rigid_dynamic_data()
        else:
            for obj in self._objs:
                obj.find_component_by_type(
                    sapien.render.RenderBodyComponent
                ).visibility = 0
        # set hidden *after* setting/getting so not applied to self.before_hide_pose erroenously
        self.hidden = True

    def show_visual(self):
        assert not self.has_collision_shapes()
        if not self.hidden:
            return
        # set hidden *before* setting/getting so not applied to self.before_hide_pose erroenously
        self.hidden = False
        if physx.is_gpu_enabled():
            if hasattr(self, "before_hide_pose"):
                self.pose = self.before_hide_pose
                self.px.gpu_apply_rigid_dynamic_data()
                self.px.gpu_fetch_rigid_dynamic_data()
        else:
            for obj in self._objs:
                obj.find_component_by_type(
                    sapien.render.RenderBodyComponent
                ).visibility = 1

    def is_static(self, lin_thresh=1e-2, ang_thresh=1e-1):
        """
        Checks if this actor is static within the given linear velocity threshold `lin_thresh` and angular velocity threshold `ang_thresh`
        """
        return torch.logical_and(
            torch.linalg.norm(self.linear_velocity, axis=1) <= lin_thresh,
            torch.linalg.norm(self.angular_velocity, axis=1) <= ang_thresh,
        )

    # -------------------------------------------------------------------------- #
    # Exposed actor properties, getters/setters that automatically handle
    # CPU and GPU based actors
    # -------------------------------------------------------------------------- #
    def remove_from_scene(self):
        if physx.is_gpu_enabled():
            raise RuntimeError(
                "Cannot physically remove object from scene during GPU simulation. This can only be done in CPU simulation. If you wish to remove an object physically, the best way is to move the object far away."
            )
        else:
            [obj.remove_from_scene() for obj in self._objs]

    @property
    def pose(self) -> Pose:
        if physx.is_gpu_enabled():
            if self.px_body_type == "static":
                # NOTE (stao): usually _builder_initial_pose is just one pose, but for static objects in GPU sim we repeat it if necessary so it can be used
                # as part of observations if needed
                return self.initial_pose
            else:
                if self.hidden:
                    return Pose.create(self.before_hide_pose)
                else:
                    raw_pose = self.px.cuda_rigid_body_data.torch()[
                        self._body_data_index, :7
                    ]
                    return Pose.create(raw_pose)
        else:
            return Pose.create([obj.pose for obj in self._objs])

    @pose.setter
    def pose(self, arg1: Union[Pose, sapien.Pose, Array]) -> None:
        if physx.is_gpu_enabled():
            if not isinstance(arg1, torch.Tensor):
                arg1 = vectorize_pose(arg1)
            if self.hidden:
                self.before_hide_pose[self._scene._reset_mask[self._scene_idxs]] = arg1
            else:
                self.px.cuda_rigid_body_data.torch()[
                    self._body_data_index[self._scene._reset_mask[self._scene_idxs]], :7
                ] = arg1
        else:
            if isinstance(arg1, sapien.Pose):
                for obj in self._objs:
                    obj.pose = arg1
            else:
                if len(arg1.shape) == 2:
                    for i, obj in enumerate(self._objs):
                        obj.pose = to_sapien_pose(arg1[i])
                else:
                    arg1 = to_sapien_pose(arg1)

    def set_pose(self, arg1: Union[Pose, sapien.Pose]) -> None:
        self.pose = arg1
