#!/usr/bin/env python

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import argparse
import json
import os
import pickle
import collections

import gym
import ray
from ray.rllib.agents.registry import get_agent_class
from ray.rllib.evaluation.sampler import clip_action
from ray.tune.util import merge_dicts

EXAMPLE_USAGE = """
Example Usage via RLlib CLI:
    rllib rollout /tmp/ray/checkpoint_dir/checkpoint-0 --run DQN
    --env CartPole-v0 --steps 1000000 --out rollouts.pkl

Example Usage via executable:
    ./rollout.py /tmp/ray/checkpoint_dir/checkpoint-0 --run DQN
    --env CartPole-v0 --steps 1000000 --out rollouts.pkl
"""

# Note: if you use any custom models or envs, register them here first, e.g.:
#
# ModelCatalog.register_custom_model("pa_model", ParametricActionsModel)
# register_env("pa_cartpole", lambda _: ParametricActionCartpole(10))

from ray.tune.registry import register_env

from tls.environment.sumo import SUMOEnv
from tls.agents.models import register_model

_NETWORK_PATH = '/home/gosha/workspace/pycharm/adaptive-tls/networks/montgomery_county/'

register_env('SUMOEnv-v0', lambda _: SUMOEnv(net_file=_NETWORK_PATH + 'moco.net.xml',
                                             config_file=_NETWORK_PATH + 'testmap.sumocfg',
                                             additional_file=_NETWORK_PATH + 'moco.det.xml',
                                             use_gui=True))
register_model()


def create_parser(parser_creator=None):
    parser_creator = parser_creator or argparse.ArgumentParser
    parser = parser_creator(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Roll out a reinforcement learning agent "
        "given a checkpoint.",
        epilog=EXAMPLE_USAGE)

    parser.add_argument(
        "checkpoint", type=str, help="Checkpoint from which to roll out.")
    required_named = parser.add_argument_group("required named arguments")
    required_named.add_argument(
        "--run",
        type=str,
        required=True,
        help="The algorithm or model to train. This may refer to the name "
        "of a built-on algorithm (e.g. RLLib's DQN or PPO), or a "
        "user-defined trainable function or class registered in the "
        "tune registry.")
    required_named.add_argument(
        "--env", type=str, help="The gym environment to use.")
    parser.add_argument(
        "--no-render",
        default=False,
        action="store_const",
        const=True,
        help="Surpress rendering of the environment.")
    parser.add_argument(
        "--steps", default=10000, help="Number of steps to roll out.")
    parser.add_argument("--out", default=None, help="Output filename.")
    parser.add_argument(
        "--config",
        default="{}",
        type=json.loads,
        help="Algorithm-specific configuration (e.g. env, hyperparams). "
        "Surpresses loading of configuration from checkpoint.")
    return parser


def run(args, parser):
    config = {}
    # Load configuration from file
    config_dir = os.path.dirname(args.checkpoint)
    config_path = os.path.join(config_dir, "params.pkl")
    if not os.path.exists(config_path):
        config_path = os.path.join(config_dir, "../params.pkl")
    if not os.path.exists(config_path):
        if not args.config:
            raise ValueError(
                "Could not find params.pkl in either the checkpoint dir or "
                "its parent directory.")
    else:
        with open(config_path, 'rb') as f:
            config = pickle.load(f)
    if "num_workers" in config:
        del config["num_workers"]
    if "num_gpus_per_worker" in config:
        del config["num_gpus_per_worker"]
    config = merge_dicts(config, args.config)
    if not args.env:
        if not config.get("env"):
            parser.error("the following arguments are required: --env")
        args.env = config.get("env")

    ray.init()

    cls = get_agent_class(args.run)
    agent = cls(env=args.env, config=config)
    agent.restore(args.checkpoint)
    num_steps = int(args.steps)
    rollout(agent, args.env, num_steps, args.out, args.no_render)


def rollout(agent, env_name, num_steps, out=None, no_render=True):
    if hasattr(agent, "local_evaluator"):
        env = agent.local_evaluator.env
        multiagent = agent.local_evaluator.multiagent
        if multiagent:
            policy_agent_mapping = agent.config["multiagent"][
                "policy_mapping_fn"]
            mapping_cache = {}
        policy_map = agent.local_evaluator.policy_map
        state_init = {p: m.get_initial_state() for p, m in policy_map.items()}
        use_lstm = {p: len(s) > 0 for p, s in state_init.items()}
    else:
        env = gym.make(env_name)
        multiagent = False
        use_lstm = {'default': False}

    steps = 0
    if out is not None:
        rollout = []
    state = env.reset()
    done = False
    reward_total = 0.0
    reward_for_each = collections.defaultdict(int)
    statistics = []
    time = -5
    while not done and steps < (num_steps or steps + 1):
        if multiagent:
            action_dict = {}
            for agent_id in state.keys():
                a_state = state[agent_id]
                if a_state is not None:
                    policy_id = mapping_cache.setdefault(
                        agent_id, policy_agent_mapping(agent_id))
                    p_use_lstm = use_lstm[policy_id]
                    if p_use_lstm:
                        a_action, p_state_init, _ = agent.compute_action(
                            a_state,
                            state=state_init[policy_id],
                            policy_id=policy_id)
                        state_init[policy_id] = p_state_init
                    else:
                        a_action = agent.compute_action(
                            a_state, policy_id=policy_id)
                    action_dict[agent_id] = a_action
            action = action_dict
        else:
            if use_lstm["default"]:
                action, state_init, _ = agent.compute_action(
                    state, state=state_init)
            else:
                action = agent.compute_action(state)

        if agent.config["clip_actions"]:
            clipped_action = clip_action(action, env.action_space)
            next_state, reward, done, stat = env.step(clipped_action)
        else:
            next_state, reward, done, stat = env.step(action)

        if multiagent:
            done = done["__all__"]
            reward_total += sum(reward.values())
            for k, v in reward.items():
                reward_for_each[k] += v

            time += 5

            # Collect statistics
            statistics.append({
                'statistics': stat.copy(),
                'episode_reward': reward_for_each.copy(),
                'timestamp': time,
            })
        else:
            reward_total += reward
        if not no_render:
            env.render()
        if out is not None:
            rollout.append([state, action, next_state, reward, done])
        steps += 1
        state = next_state

    print(f'Episode end statistics {env.collect_statistics_after_simulation()}')
    print("Episode reward", reward_total)
    print("Reward for each agent", reward_for_each)
    with open('simulation_statistics.json', 'w') as f:
        json.dump(statistics, f)

    if out is not None:
        pickle.dump(rollout, open(out, "wb"))


if __name__ == "__main__":
    parser = create_parser()
    args = parser.parse_args()
    run(args, parser)
