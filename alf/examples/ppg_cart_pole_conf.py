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

import alf
from alf.examples import ppg_conf
from alf.algorithms.data_transformer import RewardScaling
from alf.algorithms.ppg_algorithm import PPGAlgorithm
from alf.networks.encoding_networks import EncodingNetwork
from alf.utils.losses import element_wise_huber_loss

# Environment Configuration
alf.config(
    'create_environment', env_name='CartPole-v0', num_parallel_environments=8)

# Reward Scailing
alf.config('TrainerConfig', data_transformer_ctor=RewardScaling)
alf.config('RewardScaling', scale=0.01)

# algorithm config
alf.config('EncodingNetwork', fc_layer_params=(100, ))

alf.config(
    'PPGAlgorithm',
    encoding_network_ctor=EncodingNetwork,
    optimizer=alf.optimizers.AdamTF(lr=1e-3))

# training config
alf.config(
    'TrainerConfig',
    algorithm_ctor=PPGAlgorithm,
    whole_replay_buffer_training=True,
    clear_replay_buffer=True)

alf.config(
    'TrainerConfig',
    mini_batch_length=1,
    unroll_length=32,
    mini_batch_size=128,
    num_updates_per_train_iter=4,
    num_iterations=200,
    num_checkpoints=5,
    evaluate=True,
    eval_interval=50,
    debug_summaries=False,
    summary_interval=5)
