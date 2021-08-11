# Copyright (c) 2021 Horizon Robotics and ALF Contributors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Phasic Policy Gradient Algorithm."""

import torch
from typing import Optional, Tuple

import alf
from alf.algorithms.off_policy_algorithm import OffPolicyAlgorithm
from alf.algorithms.ppo_algorithm import PPOAlgorithm
from alf.algorithms.config import TrainerConfig
from alf.algorithms.ppo_loss import PPOLoss
from alf.networks.encoding_networks import EncodingNetwork
from alf.networks.network import Network
from alf.networks.projection_networks import NormalProjectionNetwork, CategoricalProjectionNetwork
from alf.data_structures import namedtuple, TimeStep, AlgStep
from alf.tensor_specs import TensorSpec
import alf.layers as layers

from alf.utils import common, dist_utils, value_ops, tensor_utils
from alf.utils.losses import element_wise_huber_loss

PPGState = namedtuple(
    'PPGState', ['actor', 'value', 'aux_value'], default_value=())

PPGRolloutInfo = namedtuple(
    'PPGRolloutInfo', [
        'action_distribution',
        'action',
        'value',
        'step_type',
        'discount',
        'reward',
        'reward_weights',
    ],
    default_value=())

PPGTrainInfo = namedtuple(
    'PPGTrainInfo',
    [
        'action',  # Sampled from the behavior policy
        'rollout_action_distribution',  # Evaluation of the behavior policy
        'action_distribution',  # Evaluation of the target policy
        'value',
        'step_type',
        'discount',
        'reward',
        'advantages',
        'returns',
        'reward_weights',
    ],
    default_value=())


def merge_rollout_into_train_info(rollout_info: PPGRolloutInfo,
                                  train_info: PPGTrainInfo) -> PPGTrainInfo:
    return train_info._replace(
        action=rollout_info.action,
        action_distribution=rollout_info.action_distribution,
        value=rollout_info.value,
        step_type=rollout_info.step_type,
        discount=rollout_info.discount,
        reward=rollout_info.reward,
        reward_weights=rollout_info.reward_weights)


@alf.configurable
class PPGAlgorithm(OffPolicyAlgorithm):
    """PPG Algorithm.
    """

    def __init__(self,
                 observation_spec: TensorSpec,
                 action_spec: TensorSpec,
                 reward_spec=TensorSpec(()),
                 encoding_network_ctor: callable = EncodingNetwork,
                 env=None,
                 config: Optional[TrainerConfig] = None,
                 debug_summaries: bool = False,
                 optimizer: Optional[torch.optim.Optimizer] = None,
                 name: str = "PPGAlgorithm"):
        """
        Args:
            observation_spec (nested TensorSpec): representing the observations.
            action_spec (nested BoundedTensorSpec): representing the actions.
            reward_spec (TensorSpec): a rank-1 or rank-0 tensor spec representing
                the reward(s).
            reward_weights (None|list[float]): this is only used when the reward is
                multidimensional. In that case, the weighted sum of the v values
                is used for training the actor if reward_weights is not None.
                Otherwise, the sum of the v values is used.
            env (Environment): The environment to interact with. env is a batched
                environment, which means that it runs multiple simulations
                simultateously. env only needs to be provided to the root
                Algorithm.
            epsilon_greedy (float): a floating value in [0,1], representing the
                chance of action sampling instead of taking argmax. This can
                help prevent a dead loop in some deterministic environment like
                Breakout. Only used for evaluation. If None, its value is taken
                from ``alf.get_config_value(TrainerConfig.epsilon_greedy)``
            config (TrainerConfig): config for training. config only needs to be
                provided to the algorithm which performs ``train_iter()`` by
                itself.
            actor_network_ctor (Callable): Function to construct the actor network.
                ``actor_network_ctor`` needs to accept ``input_tensor_spec`` and
                ``action_spec`` as its arguments and return an actor network.
                The constructed network will be called with ``forward(observation, state)``.
            value_network_ctor (Callable): Function to construct the value network.
                ``value_network_ctor`` needs to accept ``input_tensor_spec`` as
                its arguments and return a value netwrok. The contructed network
                will be called with ``forward(observation, state)`` and returns
                value tensor for each observation given observation and network
                state.
            loss (None|ActorCriticLoss): an object for calculating loss. If
                None, a default loss of class loss_class will be used.
            loss_class (type): the class of the loss. The signature of its
                constructor: ``loss_class(debug_summaries)``
            optimizer (torch.optim.Optimizer): The optimizer for training
            debug_summaries (bool): True if debug summaries should be created.
            name (str): Name of this algorithm.
        """

        encoding_net = encoding_network_ctor(
            input_tensor_spec=observation_spec)
        policy_head = alf.nn.Sequential(
            encoding_net,
            CategoricalProjectionNetwork(
                input_size=encoding_net.output_spec.shape[0],
                action_spec=action_spec))
        value_head = alf.nn.Sequential(
            encoding_net,
            layers.FC(
                input_size=encoding_net.output_spec.shape[0], output_size=1),
            layers.Reshape(shape=()))
        aux_value_head = alf.nn.Sequential(
            encoding_net, layers.Detach(),
            layers.FC(
                input_size=encoding_net.output_spec.shape[0], output_size=1),
            layers.Reshape(shape=()))

        super().__init__(
            config=config,
            env=env,
            observation_spec=observation_spec,
            action_spec=action_spec,
            reward_spec=reward_spec,
            predict_state_spec=PPGState(actor=policy_head.state_spec),
            # TODO(breakds): Value heads need state as well
            train_state_spec=PPGState(
                actor=policy_head.state_spec,
                value=value_head.state_spec,
                aux_value=aux_value_head.state_spec),
            optimizer=optimizer)

        # TODO(breakds): Make this more flexible to allow recurrent networks
        # TODO(breakds): Make this more flexible to allow separate networks
        # TODO(breakds): Add other more complicated network parameters
        self._encoding_net = encoding_net
        # TODO(breakds): Contiuous cases should be handled
        self._policy_head = policy_head
        self._value_head = value_head
        self._aux_value_head = aux_value_head
        # TODO(breakds): Put this to the configuration
        self._loss = PPOLoss(
            entropy_regularization=1e-4,
            gamma=0.98,
            td_error_loss_fn=element_wise_huber_loss,
            debug_summaries=debug_summaries)

    @property
    def on_policy(self) -> bool:
        return False

    def rollout_step(self, inputs: TimeStep, state: PPGState) -> AlgStep:
        value, value_state = self._value_head(
            inputs.observation, state=state.value)

        action_distribution, actor_state = self._policy_head(
            inputs.observation, state=state.actor)

        action = dist_utils.sample_action_distribution(action_distribution)

        return AlgStep(
            output=action,
            state=PPGState(actor=actor_state, value=value_state),
            info=PPGRolloutInfo(
                action_distribution=action_distribution,
                action=common.detach(action),
                value=value,
                step_type=inputs.step_type,
                discount=inputs.discount,
                reward=inputs.reward,
                reward_weights=()))

    def preprocess_experience(
            self,
            inputs: TimeStep,  # nest of [B, T, ...]
            rollout_info: PPGRolloutInfo,
            batch_info) -> Tuple[TimeStep, PPGTrainInfo]:
        # Here inputs is a nest of tensors representing a batch of trajectories.
        # Each tensor is expected to be of shape [B, T] or [B, T, ...], where T
        # stands for the temporal extent, where B is the the size of the batch.

        discounts = rollout_info.discount * self._loss.gamma

        advantages = value_ops.generalized_advantage_estimation(
            rewards=rollout_info.reward,
            values=rollout_info.value,
            step_types=rollout_info.step_type,
            discounts=discounts,
            td_lambda=self._loss._lambda,
            time_major=False)
        advantages = tensor_utils.tensor_extend_zero(advantages, dim=1)
        returns = rollout_info.value + advantages

        return inputs, merge_rollout_into_train_info(
            rollout_info,
            PPGTrainInfo(
                rollout_action_distribution=rollout_info.action_distribution,
                returns=returns,
                advantages=advantages))

    def train_step(self, inputs: TimeStep, state: PPGState,
                   prev_train_info: PPGTrainInfo) -> AlgStep:
        alg_step = self._rollout_step(inputs, state)
        return alg_step._replace(
            info=merge_rollout_into_train_info(alg_step.info, prev_train_info))

    def calc_loss(self, info: PPGTrainInfo):
        return self._loss(info)
