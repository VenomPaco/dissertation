
import functools
import multiprocessing
import operator
from argparse import ArgumentParser
from math import inf

import matplotlib.pyplot as plt
import numpy as np
import rustworkx as rx
import torch.nn as nn
from qiskit import QuantumCircuit, transpile
from qiskit.converters import dag_to_circuit
from qiskit.transpiler import CouplingMap
from rich import print
from rustworkx.visualization import mpl_draw
from sb3_contrib import MaskablePPO
from sb3_contrib.common.maskable.policies import MaskableMultiInputActorCriticPolicy
from stable_baselines3.common.vec_env import SubprocVecEnv, VecMonitor
from tqdm.rich import tqdm

from routing.circuit_gen import RandomCircuitGenerator
from routing.env import QcpRoutingEnv, NoiseConfig
from utils import qubits_to_indices


def t_topology() -> rx.PyGraph:
    g = rx.PyGraph()

    g.add_nodes_from(range(5))
    g.add_edges_from_no_data([(0, 1), (1, 2), (1, 3), (3, 4)])

    return g


def h_topology() -> rx.PyGraph:
    g = rx.PyGraph()

    g.add_nodes_from(range(7))
    g.add_edges_from_no_data([(0, 1), (1, 2), (1, 3), (3, 5), (4, 5), (5, 6)])

    return g


def grid_topology(rows: int, cols: int) -> rx.PyGraph:
    g = rx.PyGraph()

    g.add_nodes_from(range(rows * cols))

    for row in range(rows):
        for col in range(cols):
            if col != cols - 1:
                g.add_edge(row * cols + col, row * cols + col + 1, None)

            if row != rows - 1:
                g.add_edge(row * cols + col, (row + 1) * cols + col, None)

    return g


def main():
    parser = ArgumentParser(
        'qcp_routing',
        description='Qubit routing with deep reinforcement learning',
    )

    parser.add_argument('-m', '--model', metavar='M', help='name of the model', required=True)
    parser.add_argument('-l', '--learn', action='store_true', help='whether or not to train the agent')
    parser.add_argument('-t', '--training-iters', metavar='I', help='training iterations per environment', default=50,
                        type=int)
    parser.add_argument('-e', '--envs', help='number of environments (for vectorization)',
                        default=multiprocessing.cpu_count(), type=int)
    parser.add_argument('-r', '--routing-method', choices=['basic', 'stochastic', 'sabre'],
                        help='routing method for Qiskit compiler', default='sabre')
    parser.add_argument('-d', '--depth', help='depth of circuit observations', default=8, type=int)
    parser.add_argument('-i', '--iters', metavar='I', help='routing iterations for evaluation', default=20, type=int)
    parser.add_argument('--eval-circuits', metavar='C', help='number of evaluation circuits', default=100, type=int)
    parser.add_argument('--circuit-size', metavar='S', help='number of gates in random circuits', default=16, type=int)
    parser.add_argument('--show-topology', action='store_true', help='show circuit topology')
    parser.add_argument('--seed', metavar='S', help='seed for random number generation', type=int)

    args = parser.parse_args()
    args.model_path = f'models/{args.model}.model'

    # Parameters
    n_steps = 2048
    training_iterations = 4
    noise_config = NoiseConfig(1.0e-2, 3.0e-3, log_base=2.0)

    g = t_topology()
    circuit_generator = RandomCircuitGenerator(g.num_nodes(), args.circuit_size, seed=args.seed)

    if args.show_topology:
        rx.visualization.mpl_draw(g, with_labels=True)
        plt.show()

    def env_fn() -> QcpRoutingEnv:
        return QcpRoutingEnv(g, circuit_generator, args.depth, training_iterations=training_iterations,
                             noise_config=noise_config)

    try:
        model = MaskablePPO.load(args.model_path, tensorboard_log='logs/routing')

        args.depth = model.observation_space['circuit'].shape[1]
        vec_env = VecMonitor(SubprocVecEnv([env_fn] * args.envs))
        model.set_env(vec_env)

        reset = False
    except FileNotFoundError:
        vec_env = VecMonitor(SubprocVecEnv([env_fn] * args.envs))
        policy_kwargs = {
            'net_arch': [64, 64, 96],
            'activation_fn': nn.SiLU,
        }

        model = MaskablePPO(MaskableMultiInputActorCriticPolicy, vec_env, policy_kwargs=policy_kwargs, n_steps=n_steps,
                            tensorboard_log='logs/routing', learning_rate=5e-5)
        args.learn = True
        reset = True

    if args.learn:
        model.learn(args.envs * args.training_iters * n_steps, progress_bar=True, tb_log_name='ppo',
                    reset_num_timesteps=reset)
        model.save(args.model_path)

    model.set_random_seed(args.seed)

    env = env_fn()
    env.training = False
    env.recalibrate()

    # Generate a random initial mapping
    # TODO: improve random generation to facilitate reproducibility
    env.set_random_initial_mapping()
    env.reset()
    initial_layout = env.qubit_to_node.copy().tolist()

    reliability_map = {}
    for edge, value in zip(env.coupling_map.edge_list(), env.error_rates):
        value = 1.0 - value
        reliability_map[edge] = value
        reliability_map[edge[::-1]] = value

    def reliability(circuit: QuantumCircuit) -> float:
        return functools.reduce(operator.mul, [
            reliability_map[qubits_to_indices(circuit, instruction.qubits)]
            for instruction in circuit.get_instructions('cx')
        ])

    coupling_map = CouplingMap(g.to_directed().edge_list())

    avg_episode_reward = 0.0
    avg_swaps_rl, avg_bridges_rl, avg_swaps_qiskit = 0.0, 0.0, 0.0
    avg_cnots_rl, avg_cnots_qiskit = 0.0, 0.0
    avg_depth_rl, avg_depth_qiskit = 0.0, 0.0
    avg_reliability_rl, avg_reliability_qiskit = 0.0, 0.0

    print('[b yellow]  EVALUATION[/b yellow]')

    for _ in tqdm(range(args.eval_circuits)):
        env.generate_circuit()
        obs, _ = env.reset()

        best_reward = -inf
        routed_circuit = env.circuit.copy_empty_like()

        for _ in range(args.iters):
            terminated = False
            total_reward = 0.0

            while not terminated:
                action, _ = model.predict(obs, action_masks=env.action_masks(), deterministic=False)
                action = int(action)

                obs, reward, terminated, *_ = env.step(action)
                total_reward += reward

            if total_reward > best_reward:
                best_reward = total_reward
                routed_circuit = env.routed_circuit()

            obs, _ = env.reset()

        t_qc = transpile(env.circuit, coupling_map=coupling_map, initial_layout=initial_layout,
                         routing_method=args.routing_method, basis_gates=['u', 'swap', 'cx'], optimization_level=0,
                         seed_transpiler=args.seed)

        rl_ops = routed_circuit.count_ops()
        qiskit_ops = t_qc.count_ops()

        avg_episode_reward += best_reward

        avg_swaps_rl += rl_ops.get("swap", 0)
        avg_bridges_rl += rl_ops.get("bridge", 0)
        avg_swaps_qiskit += qiskit_ops.get("swap", 0)

        routed_circuit = routed_circuit.decompose()
        t_qc = t_qc.decompose()

        avg_cnots_rl += routed_circuit.count_ops().get("cx", 0)
        avg_cnots_qiskit += t_qc.count_ops().get("cx", 0)

        avg_depth_rl += routed_circuit.depth()
        avg_depth_qiskit += t_qc.depth()

        avg_reliability_rl += reliability(routed_circuit)
        avg_reliability_qiskit += reliability(t_qc)

    # Calculate averages
    avg_episode_reward /= args.eval_circuits

    avg_swaps_rl /= args.eval_circuits
    avg_bridges_rl /= args.eval_circuits
    avg_swaps_qiskit /= args.eval_circuits

    avg_cnots_rl /= args.eval_circuits
    avg_cnots_qiskit /= args.eval_circuits

    avg_depth_rl /= args.eval_circuits
    avg_depth_qiskit /= args.eval_circuits

    avg_reliability_rl /= args.eval_circuits
    avg_reliability_qiskit /= args.eval_circuits

    # Print results
    print(f'\nAverage episode reward: {avg_episode_reward:.3f}\n')

    print('[b blue]RL Routing[/b blue]')
    print(f'Average swaps: {avg_swaps_rl:.2f}')
    if env.allow_bridge_gate:
        print(f'Average bridges: {avg_bridges_rl:.2f}')

    print(f'Average CNOTs after decomposition: {avg_cnots_rl:.2f}')
    print(f'Average depth after decomposition: {avg_depth_rl:.2f}')
    print(f'Average reliability after decomposition: {avg_reliability_rl:.3%}\n')

    print(f'[b blue]Qiskit Compiler ({args.routing_method} routing)[/b blue]')
    print(f'Average swaps: {avg_swaps_qiskit:.3f}')

    print(f'Average CNOTs after decomposition: {avg_cnots_qiskit:.2f}')
    print(f'Average depth after decomposition: {avg_depth_qiskit:.2f}')
    print(f'Average reliability after decomposition: {avg_reliability_qiskit:.3%}\n')

    print(f'[b blue]RL vs Qiskit[/b blue]')
    avg_cnot_reduction = (avg_cnots_qiskit - avg_cnots_rl) / avg_cnots_qiskit
    print(f'Average CNOT reduction: [magenta]{avg_cnot_reduction:.3%}[/magenta]')
    avg_depth_reduction = (avg_depth_qiskit - avg_depth_rl) / avg_depth_qiskit
    print(f'Average depth reduction: [magenta]{avg_depth_reduction:.3%}[/magenta]')
    avg_reliability_increase = (avg_reliability_rl - avg_reliability_qiskit) / avg_reliability_qiskit
    print(f'Average reliability increase: [magenta]{avg_reliability_increase:.3%}[/magenta]')


if __name__ == '__main__':
    main()
