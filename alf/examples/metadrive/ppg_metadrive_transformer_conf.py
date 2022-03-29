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

from functools import partial

import torch

import alf

import alf.examples.metadrive.base_conf
from alf.examples import ppg_conf

from alf.examples.networks import impala_cnn_encoder
from alf.utils.losses import element_wise_squared_loss
from alf.algorithms.ppg_algorithm import PPGAuxOptions, PPGAlgorithm
from alf.environments import suite_metadrive
from alf.networks import StableNormalProjectionNetwork, TruncatedProjectionNetwork, BetaProjectionNetwork, EncodingNetwork

alf.config(
    'create_environment', env_name='Vectorized', num_parallel_environments=36)

alf.config(
    'metadrive.sensors.VectorizedObservation',
    segment_resolution=2.0,
    polyline_size=4,
    polyline_limit=128)


class ObservationCombiner(torch.nn.Module):
    """This module combines the vectorized map, agents and ego inputs so as to
    prepare them to feed the transformer.

    The input should be a tuple of 3, (map_sequence, ego, agents), where

    1. ``map_sequence`` is of shape [B, L, d_map_feature], where B is the batch
       size, L is the maximum number of polylines, and d_map_feature is the size
       of the polyline feature.

    2. ``ego`` is a single vector encodes the ego car trajectory and is of shape
       [B, d_ego_feature].

    3. ``agents`` is of shape [B, A, d_agent_feature], where A is the maximum
       number of agents being encoded, and d_agent_feature is the size of the
       per-agent feature.

    All the 3 inputs above will go through a fully connected layer respectively
    and generate a set of L vectors (map), a set of a single vector(ego), and a
    set of A vectors (agents) respectively, where all vectors will have the
    shape [d_model,]. The 3 sets will be combined (concatenated).

    """

    def __init__(self, d_map_feature: int, d_ego_feature: int,
                 d_agent_feature: int, d_model: int):
        super().__init__()

        self._map_fc = alf.layers.FC(
            d_map_feature, d_model, activation=torch.relu_)

        self._agent_fc = alf.layers.FC(
            d_agent_feature, d_model, activation=torch.relu_)

        self._ego_fc = alf.layers.FC(
            d_ego_feature, d_model, activation=torch.relu_)

    def forward(self, inputs):
        map_sequence, ego, agents = inputs

        x0 = self._ego_fc(ego).unsqueeze(1)  # [B, 1, d_model]

        # The input ``sequence`` is [B, L, d_map_feature]
        x1 = self._map_fc(map_sequence)  # [B, L, d_model]

        x2 = agents.view(*agents.shape[:2], -1)
        x2 = self._agent_fc(x2)  # [B, A, d_model]

        return torch.cat([x0, x1, x2], dim=1)


def encoding_network_ctor(input_tensor_spec):
    d_model = 128
    num_heads = 8
    memory_size = (input_tensor_spec['map'].shape[0] +
                   input_tensor_spec['agents'].shape[0] + 1)

    layers = [
        lambda x: (x['map'], x['ego'], x['agents']),
        ObservationCombiner(
            d_map_feature=input_tensor_spec['map'].shape[-1],
            d_ego_feature=input_tensor_spec['ego'].shape[-1],
            d_agent_feature=input_tensor_spec['agents'].shape[1] *
            input_tensor_spec['agents'].shape[2],
            d_model=d_model),
    ]

    for i in range(3):
        layers.append(
            alf.layers.TransformerBlock(
                d_model=d_model,
                num_heads=num_heads,
                memory_size=memory_size,
                positional_encoding='none'))

    # Take the corresponding transformer output of the first vector in the
    # sequence (corresponding to "ego") as the final output of the encoder.
    layers.append(lambda x: x[:, 0, :])
    layers.append(alf.layers.Reshape(-1))

    return alf.nn.Sequential(*layers, input_tensor_spec=input_tensor_spec)


alf.config('ReplayBuffer.gather_all', convert_to_default_device=False)

stable_normal_proj_net = partial(
    StableNormalProjectionNetwork,
    state_dependent_std=True,
    squash_mean=False,
    scale_distribution=True,
    min_std=1e-3,
    max_std=10.0)

# NOTE: replace stable_normal_proj_net with the other projection
alf.config(
    'DisjointPolicyValueNetwork',
    continuous_projection_net_ctor=stable_normal_proj_net,
    is_sharing_encoder=True)

alf.config(
    'PPGAlgorithm',
    encoding_network_ctor=encoding_network_ctor,
    policy_optimizer=alf.optimizers.AdamTF(lr=5e-5),
    aux_optimizer=alf.optimizers.AdamTF(lr=5e-5),
    aux_options=PPGAuxOptions(
        enabled=True,
        interval=32,
        mini_batch_length=None,  # None means use unroll_length as
        # mini_batch_length for aux phase
        mini_batch_size=18,
        num_updates_per_train_iter=6,
    ))

alf.config(
    'PPOLoss',
    compute_advantages_internally=True,
    entropy_regularization=0.01,
    gamma=0.999,
    td_lambda=0.95,
    td_loss_weight=0.5)

alf.config(
    'PPGAuxPhaseLoss',
    td_error_loss_fn=element_wise_squared_loss,
    policy_kl_loss_weight=1.0,
    gamma=0.999,
    td_lambda=0.95)

# training config
alf.config(
    'TrainerConfig',
    enable_amp=True,
    unroll_length=64,
    # This means that mini_batch_length will set to equal to the
    # length of the batches taken from the replay buffer, and in this
    # case it will be adjusted unroll_length.
    mini_batch_length=None,
    mini_batch_size=18,
    num_updates_per_train_iter=3,
    num_iterations=4800,
    num_checkpoints=20,
    evaluate=False,
    eval_interval=50,
    debug_summaries=True,
    summarize_grads_and_vars=True,
    summarize_action_distributions=True,
    summary_interval=40)