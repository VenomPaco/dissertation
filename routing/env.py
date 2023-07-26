
import copy
import itertools
from abc import ABC, abstractmethod
from collections import OrderedDict
from collections.abc import MutableMapping, Iterable
from typing import Optional, Any, SupportsFloat, Iterator, Self, Generic, TypeAlias, Literal, TypeVar

import gymnasium as gym
import numpy as np
import rustworkx as rx
from gymnasium import spaces
from nptyping import NDArray, Int8
from ordered_set import OrderedSet
from qiskit import QuantumCircuit, AncillaRegister
from qiskit.circuit import Qubit, Operation
from qiskit.circuit.library import SwapGate, CXGate
from qiskit.converters import circuit_to_dag, dag_to_circuit
from qiskit.dagcircuit import DAGOpNode
from qiskit.transpiler.passes import CommutationAnalysis

from dag_utils import dag_layers
from routing.noise import NoiseConfig
from utils import qubits_to_indices, indices_to_qubits

RoutingObsType: TypeAlias = dict[str, NDArray]
GateSchedulingList: TypeAlias = list[tuple[DAGOpNode, tuple[int, ...]]]
BridgeArgs: TypeAlias = tuple[DAGOpNode, tuple[int, int, int]]


class RoutingEnv(gym.Env[RoutingObsType, int], ABC):
    """
    Base qubit routing environment.

    :param coupling_map: Graph representing the connectivity of the target device.
    :param circuit: Quantum circuit to compile. This argument is optional during environment construction but a valid
                    circuit must be assigned before the :py:meth:`reset` method is called.
    :param initial_mapping: Initial mapping from physical nodes to logical qubits. If ``None``, a random initial mapping
                            will be used for each training iteration.
    :param allow_bridge_gate: Allow the use of BRIDGE gates when routing.
    :param commutation_analysis: Use commutation rules to schedule additional gates at each time step.
    :param error_rates: Array of two-qubit gate error rates.
    :param noise_config: Allows configuration of rewards in noise-aware environments. If ``None``, routing will be
                         noise-unaware.
    :param obs_modules: Observation modules that define the key-value pairs in observations.

    :ivar node_to_qubit: Current mapping from physical nodes to logical qubits.
    :ivar qubit_to_node: Current mapping from logical qubits to physical nodes.
    :ivar routed_gates: List of already routed gates, including additional SWAP and BRIDGE operations.
    :ivar log_reliabilities: Array of logarithms of two-qubit gate reliabilities (``reliability = 1 - error_rate``)
    :ivar edge_to_log_reliability: Dictionary that maps edges to their corresponding log reliability values.
    """

    observation_space: spaces.Dict
    action_space: spaces.Discrete

    initial_mapping: NDArray
    error_rates: Optional[NDArray]
    obs_modules: list['ObsModule']

    routed_gates: list[tuple[Operation, tuple[Qubit, ...]]]
    routed_op_nodes: set[DAGOpNode]
    log_reliabilities: NDArray
    edge_to_log_reliability: dict[tuple[int, int], float]

    def __init__(
        self,
        coupling_map: rx.PyGraph,
        circuit: Optional[QuantumCircuit] = None,
        initial_mapping: Optional[NDArray] = None,
        allow_bridge_gate: bool = True,
        commutation_analysis: bool = True,
        error_rates: Optional[NDArray] = None,
        noise_config: Optional[NoiseConfig] = None,
        obs_modules: Optional[list['ObsModule']] = None,
    ):
        num_qubits = coupling_map.num_nodes()
        num_edges = coupling_map.num_edges()

        if initial_mapping is None:
            initial_mapping = np.arange(num_qubits)
        if circuit is None:
            circuit = QuantumCircuit(num_qubits)
        if error_rates is None:
            error_rates = np.zeros(num_edges)

        if initial_mapping.shape != (num_qubits,):
            raise ValueError('Initial mapping has invalid shape for the provided coupling map')
        if error_rates.shape != (num_edges,):
            raise ValueError('Error rates have invalid shape for the provided coupling map')

        self.coupling_map = coupling_map
        self.circuit = circuit
        self.initial_mapping = initial_mapping
        self.allow_bridge_gate = allow_bridge_gate
        self.commutation_analysis = commutation_analysis
        self.error_rates = error_rates
        self.noise_config = noise_config
        self.obs_modules = [] if obs_modules is None else obs_modules

        # Computations using coupling map
        self.num_qubits = num_qubits
        self.num_edges = num_edges
        self.edge_list = list(coupling_map.edge_list())  # type: ignore

        self.shortest_paths = rx.graph_all_pairs_dijkstra_shortest_paths(coupling_map, lambda _: 1.0)
        self.distance_matrix = rx.graph_distance_matrix(coupling_map).astype(np.int32)

        if self.allow_bridge_gate:
            self.bridge_pairs = sorted(
                (j, i) for i in range(self.num_qubits) for j in range(i) if self.distance_matrix[j, i] == 2
            )
        else:
            self.bridge_pairs = []

        self.commutation_pass = CommutationAnalysis()

        # State information
        self.node_to_qubit = initial_mapping.copy()
        self.qubit_to_node = np.zeros_like(self.node_to_qubit)
        for node, qubit in enumerate(self.node_to_qubit):
            self.qubit_to_node[qubit] = node

        self.dag = circuit_to_dag(self.circuit)
        self.routed_gates = []
        self.routed_op_nodes = set()

        # Noise-awareness information
        if self.noise_aware:
            self.obs_modules.append(LogReliabilities())
            self.calibrate(self.error_rates)

        bridge_circuit = QuantumCircuit(3)
        for _ in range(2):
            bridge_circuit.cx(1, 2)
            bridge_circuit.cx(0, 1)

        self.bridge_gate = bridge_circuit.to_gate(label='BRIDGE')
        self.bridge_gate.name = 'bridge'
        self.swap_gate = SwapGate()

        self._num_scheduled_2q_gates = 0
        self._scheduling_reward = 0.0

    @property
    def noise_aware(self) -> bool:
        return self.noise_config is not None

    @property
    def terminated(self) -> bool:
        return self.dag.size() == 0

    def reset(
        self,
        *,
        seed: Optional[int] = None,
        options: Optional[dict[str, Any]] = None,
    ) -> tuple[RoutingObsType, dict[str, Any]]:
        if self.circuit.num_qubits < self.num_qubits:
            self.circuit.add_register(AncillaRegister(self.num_qubits - self.circuit.num_qubits))

        self.dag = circuit_to_dag(self.circuit)
        self.routed_gates = []
        self.routed_op_nodes = set()

        if self.commutation_analysis:
            self.commutation_pass.run(self.dag)

        self._reset_state()

        return self.current_obs(), {}

    def calibrate(self, error_rates: NDArray):
        """
        Given an array of edge error rates, calculates the log reliabilities according to the noise
        configuration, which can, in turn, be used to obtain agent rewards.

        :param error_rates: Array of two-qubit gate error rates.
        """
        self.error_rates = error_rates.copy()
        if self.noise_config is not None:
            self.log_reliabilities = self.noise_config.calculate_log_reliabilities(error_rates)

            self.edge_to_log_reliability = {}
            for edge, value in zip(self.edge_list, self.log_reliabilities):
                self.edge_to_log_reliability[edge] = value
                self.edge_to_log_reliability[edge[::-1]] = value

    def copy(self) -> Self:
        """
        Returns a copy of the environment. Attributes that are not mutated when stepping through the environment are
        copied by reference; other attributes are copied by value.
        """
        env = copy.copy(self)

        env.node_to_qubit = self.node_to_qubit.copy()
        env.qubit_to_node = self.qubit_to_node.copy()

        env.dag = self.dag.copy_empty_like()
        env.dag.compose(self.dag)

        env.routed_gates = self.routed_gates.copy()

        return env

    def current_obs(self) -> RoutingObsType:
        return {
            'action_mask': self.action_mask(),
            'true_obs': {module.key(): module.obs(self) for module in self.obs_modules},
        }

    def routed_circuit(self) -> QuantumCircuit:
        routed_dag = self.dag.copy_empty_like()
        for op, qargs in self.routed_gates:
            routed_dag.apply_operation_back(op, qargs)
        return dag_to_circuit(routed_dag)

    @abstractmethod
    def action_mask(self) -> NDArray[Literal['*'], Int8]:
        """
        Returns a boolean NumPy array where the ith element is true if the ith action is valid in the current state.
        """
        raise NotImplementedError

    @abstractmethod
    def _update_state(self):
        raise NotImplementedError

    def _obs_spaces(self) -> dict[str, spaces.Space]:
        return {
            'action_mask': spaces.Box(0, 1, shape=(self.action_space.n,), dtype=np.int8),
            'true_obs': spaces.Dict({module.key(): module.space(self) for module in self.obs_modules}),
        }

    def _remove_op_node(self, op_node: DAGOpNode):
        self.dag.remove_op_node(op_node)
        self.routed_op_nodes.add(op_node)

    def _schedule_gates(self, gates: GateSchedulingList):
        for op_node, nodes in gates:
            qargs = indices_to_qubits(self.circuit, nodes)
            self.routed_gates.append((op_node.op, qargs))
            self._remove_op_node(op_node)

        self._num_scheduled_2q_gates = len(gates)
        self._scheduling_reward = sum(self._gate_reward(nodes) for _, nodes in gates if len(nodes) == 2)  # type: ignore

    def _schedulable_gates(self, only_front_layer: bool = False) -> GateSchedulingList:
        gates = OrderedSet()
        layers = [self.dag.front_layer()] if only_front_layer else dag_layers(self.dag)
        locked_nodes: set[int] = set()

        commutation_set: Optional[dict] = (
            self.commutation_pass.property_set['commutation_set'] if self.commutation_analysis
            else None
        )

        op_node: DAGOpNode
        for op_node in itertools.chain(*layers):
            indices = qubits_to_indices(self.circuit, op_node.qargs)
            nodes = tuple(self.qubit_to_node[i] for i in indices)

            if self._is_schedulable(nodes, locked_nodes):
                gates.add((op_node, nodes))
            else:
                if self.commutation_analysis:
                    free_qargs = (q for q in op_node.qargs if self.circuit.find_bit(q)[0] not in locked_nodes)
                    for qubit in free_qargs:
                        idx = commutation_set[(op_node, qubit)]

                        commuting_op_nodes: OrderedSet[DAGOpNode] = OrderedSet(commutation_set[qubit][idx])
                        commuting_op_nodes.difference_update({op_node}, self.routed_op_nodes)

                        for commuting_op_node in commuting_op_nodes:
                            commuting_indices = qubits_to_indices(self.circuit, commuting_op_node.qargs)
                            commuting_nodes = tuple(self.qubit_to_node[i] for i in commuting_indices)

                            if self._is_schedulable(commuting_nodes, locked_nodes):
                                gates.add((commuting_op_node, commuting_nodes))

                locked_nodes.update(nodes)

            if len(locked_nodes) == self.num_qubits:
                break

        return list(gates)

    def _is_schedulable(self, nodes: tuple[int, ...], locked_nodes: set[int]) -> bool:
        valid_under_current_mapping = len(nodes) != 2 or self.coupling_map.has_edge(nodes[0], nodes[1])
        not_blocked = not locked_nodes.intersection(nodes)

        return valid_under_current_mapping and not_blocked

    def _bridge(self, control: int, middle: int, target: int):
        indices = (control, middle, target)
        qargs = indices_to_qubits(self.circuit, indices)

        self.routed_gates.append((self.bridge_gate, qargs))

    def _bridge_args(self, pair: tuple[int, int]) -> Optional[BridgeArgs]:
        for op_node in self.dag.front_layer():
            if isinstance(op_node.op, CXGate):
                indices = qubits_to_indices(self.circuit, op_node.qargs)
                nodes = tuple(self.qubit_to_node[i] for i in indices)

                if tuple(sorted(nodes)) == pair:
                    control, target = nodes
                    return op_node, tuple(self.shortest_paths[control][target])

        return None

    def _swap(self, edge: tuple[int, int]):
        qargs = indices_to_qubits(self.circuit, edge)

        self.routed_gates.append((self.swap_gate, qargs))

        # Update mappings
        node_a, node_b = edge
        qubit_a, qubit_b = (self.node_to_qubit[n] for n in edge)

        self.node_to_qubit[node_a], self.node_to_qubit[node_b] = qubit_b, qubit_a
        self.qubit_to_node[qubit_a], self.qubit_to_node[qubit_b] = node_b, node_a

    def _gate_reward(self, edge: tuple[int, int]) -> float:
        if self.noise_aware:
            return self.edge_to_log_reliability[edge] + self.noise_config.added_gate_reward

        return 1.0

    def _swap_reward(self, edge: tuple[int, int]) -> float:
        if self.noise_aware:
            return 3.0 * self.edge_to_log_reliability[edge]

        return -3.0

    def _bridge_reward(self, control: int, middle: int, target: int) -> float:
        if self.noise_aware:
            return 2.0 * (
                self.edge_to_log_reliability[(middle, target)] +
                self.edge_to_log_reliability[(control, middle)]
            ) + self.noise_config.added_gate_reward

        return -2.0

    def _reset_state(self):
        self.node_to_qubit = self.initial_mapping.copy()

        for node, qubit in enumerate(self.node_to_qubit):
            self.qubit_to_node[qubit] = node

        self._update_state()


class SequentialRoutingEnv(RoutingEnv):
    """
    Sequential routing environment, where SWAP and BRIDGE operations are iteratively added to the routed circuit.

    :param restrict_swaps_to_front_layer: Restrict SWAP operations to edges involving qubits that are part of two-qubit
        operations in the front (first) layer of the original circuit.
    """

    _blocked_swap: Optional[int]

    def __init__(
        self,
        coupling_map: rx.PyGraph,
        circuit: Optional[QuantumCircuit] = None,
        initial_mapping: Optional[NDArray] = None,
        allow_bridge_gate: bool = True,
        commutation_analysis: bool = True,
        error_rates: Optional[NDArray] = None,
        noise_config: Optional[NoiseConfig] = None,
        obs_modules: Optional[list['ObsModule']] = None,
        restrict_swaps_to_front_layer: bool = True,
    ):
        super().__init__(coupling_map, circuit, initial_mapping, allow_bridge_gate, commutation_analysis,
                         error_rates, noise_config, obs_modules)

        self.restrict_swaps_to_front_layer = restrict_swaps_to_front_layer

        self._blocked_swap = None

        num_actions = self.num_edges + len(self.bridge_pairs)
        self.action_space = spaces.Discrete(num_actions)

        self.observation_space = spaces.Dict(self._obs_spaces())

    def step(self, action: int) -> tuple[RoutingObsType, SupportsFloat, bool, bool, dict[str, Any]]:
        if self.terminated:
            # Environment terminated immediately and does not require routing
            return self.current_obs(), 0.0, True, False, {}

        action_is_swap = action < self.num_edges

        if action_is_swap:
            # SWAP action
            edge = self.edge_list[action]
            self._swap(edge)
            reward = self._swap_reward(edge)
        else:
            # BRIDGE action
            pair = self.bridge_pairs[action - self.num_edges]
            args = self._bridge_args(pair)

            if args is not None:
                op_node, nodes = args

                self._remove_op_node(op_node)
                self._bridge(*nodes)
                reward = self._bridge_reward(*nodes)
            else:
                print('Invalid BRIDGE action was selected')
                reward = 0.0

        self._update_state()
        reward += self._scheduling_reward
        self._blocked_swap = action if action_is_swap and self._num_scheduled_2q_gates == 0 else None

        return self.current_obs(), reward, self.terminated, False, {}

    def action_mask(self) -> NDArray[Literal['*'], Int8]:
        mask = np.ones(self.action_space.n, dtype=Int8)

        # Swap actions
        if self.restrict_swaps_to_front_layer:
            front_layer_nodes = set()

            for op_node in self.dag.front_layer():
                indices = qubits_to_indices(self.circuit, op_node.qargs)
                nodes = (self.qubit_to_node[i] for i in indices)
                front_layer_nodes.update(nodes)

            invalid_swap_actions = [
                i for i, edge in enumerate(self.edge_list)
                if not front_layer_nodes.intersection(edge)
            ]
            mask[invalid_swap_actions] = 0

        # Disallow redundant consecutive SWAPs
        if self._blocked_swap is not None:
            mask[self._blocked_swap] = 0

        # Bridge actions
        invalid_bridge_actions = [
            self.num_edges + i for i, pair in enumerate(self.bridge_pairs)
            if self._bridge_args(pair) is None
        ]
        mask[invalid_bridge_actions] = 0

        return mask

    def _update_state(self):
        gates = self._schedulable_gates()
        self._schedule_gates(gates)


class CircuitMatrixRoutingEnv(SequentialRoutingEnv):
    """
    Environment using circuit representations from `Optimizing quantum circuit placement via machine learning
    <https://dl.acm.org/doi/10.1145/3489517.3530403>`_. The circuit observation consists of a matrix where each row
    corresponds to a physical node. The element at (i, j) corresponds to the qubit targeted by the logical
    qubit currently occupying the i-th node, at time step (layer) j. A value of -1 is used if the qubit is not involved
    in an interaction at that time step.

    :param depth: Number of two-qubit gate layers in the matrix circuit representation.
    """

    def __init__(
        self,
        coupling_map: rx.PyGraph,
        depth: int,
        circuit: Optional[QuantumCircuit] = None,
        initial_mapping: Optional[NDArray] = None,
        allow_bridge_gate: bool = True,
        commutation_analysis: bool = True,
        error_rates: Optional[NDArray] = None,
        noise_config: Optional[NoiseConfig] = None,
        restrict_swaps_to_front_layer: bool = True,
    ):
        super().__init__(coupling_map, circuit, initial_mapping, allow_bridge_gate, commutation_analysis, error_rates,
                         noise_config, [CircuitMatrix(depth)], restrict_swaps_to_front_layer)


class SchedulingMap(MutableMapping[int, int]):
    _map: dict[int, int]

    def __init__(self):
        self._map = {}

    def __setitem__(self, k: int, v: int):
        if v == 0:
            if k in self._map:
                del self._map[k]
        else:
            self._map.__setitem__(k, v)

    def __delitem__(self, k: int):
        self._map.__delitem__(k)

    def __getitem__(self, k: int) -> int:
        return self._map.__getitem__(k)

    def __len__(self) -> int:
        return self._map.__len__()

    def __iter__(self) -> Iterator[int]:
        return self._map.__iter__()

    def __repr__(self):
        return f'<{self.__class__.__name__}: {self._map}>'

    def decrement(self):
        for k in list(self._map):
            self[k] -= 1

    def update_all(self, keys: Iterable[int], value: int = 1):
        self.update((k, value) for k in keys)

    def copy(self) -> Self:
        return copy.deepcopy(self)


class LayeredRoutingEnv(RoutingEnv):
    def __init__(
        self,
        coupling_map: rx.PyGraph,
        circuit: Optional[QuantumCircuit] = None,
        initial_mapping: Optional[NDArray] = None,
        allow_bridge_gate: bool = True,
        commutation_analysis: bool = True,
        error_rates: Optional[NDArray] = None,
        noise_config: Optional[NoiseConfig] = None,
        obs_modules: Optional[list['ObsModule']] = None,
        use_decomposed_actions: bool = False,
    ):
        super().__init__(coupling_map, circuit, initial_mapping, allow_bridge_gate, commutation_analysis, error_rates,
                         noise_config, obs_modules)

        self.use_decomposed_actions = use_decomposed_actions
        self.scheduling_map = SchedulingMap()

        # Account for COMMIT / FINISH action in addition to SWAP and BRIDGE gates
        num_actions = self.num_edges + len(self.bridge_pairs) + 1
        self.action_space = spaces.Discrete(num_actions)

        self.observation_space = spaces.Dict(self._obs_spaces())

    def step(self, action: int) -> tuple[RoutingObsType, SupportsFloat, bool, bool, dict[str, Any]]:
        if action < self.num_edges:
            # SWAP action
            edge = self.edge_list[action]

            self._swap(edge)
            reward = self._swap_reward(edge)
        elif action < self.num_edges + len(self.bridge_pairs):
            # BRIDGE action
            pair = self.bridge_pairs[action - self.num_edges]
            op_node, nodes = self._bridge_args(pair)

            self._remove_op_node(op_node)
            self._bridge(*nodes)
            reward = self._bridge_reward(*nodes)
        else:
            # COMMIT action
            self._update_state()
            reward = self._scheduling_reward

        return self.current_obs(), reward, self.terminated, False, {}

    def action_mask(self) -> NDArray[Literal['*'], Int8]:
        mask = np.ones(self.action_space.n, dtype=Int8)

        # Swap actions
        invalid_swap_actions = [
            i for i, edge in enumerate(self.edge_list)
            if self.scheduling_map.keys() & edge
        ]

        mask[invalid_swap_actions] = 0

        # Bridge actions
        invalid_bridge_actions = []
        for i, pair in enumerate(self.bridge_pairs):
            args = self._bridge_args(pair)
            if args is None or self.scheduling_map.keys() & args[1]:
                invalid_bridge_actions.append(self.num_edges + i)

        mask[invalid_bridge_actions] = 0

        # Cannot COMMIT an empty layer
        if not self.scheduling_map:
            mask[-1] = 0

        return mask

    def copy(self) -> Self:
        env = super().copy()
        env.scheduling_map = self.scheduling_map.copy()
        return env

    def _bridge(self, control: int, middle: int, target: int):
        super()._bridge(control, middle, target)
        self.scheduling_map.update_all((control, middle, target))

    def _swap(self, edge: tuple[int, int]):
        super()._swap(edge)
        self.scheduling_map.update_all(edge)

    def _schedule_gates(self, gates: GateSchedulingList):
        super()._schedule_gates(gates)
        self.scheduling_map.update_all(itertools.chain(*(nodes for _, nodes in gates)))

    def _schedulable_gates(self, only_front_layer: bool = False) -> GateSchedulingList:
        gates = super()._schedulable_gates(only_front_layer)
        return [(op_node, nodes) for (op_node, nodes) in gates if not self.scheduling_map.keys() & nodes]

    def _reset_state(self):
        self.scheduling_map.clear()
        super()._reset_state()

    def _update_state(self):
        self.scheduling_map.decrement()

        gates = self._schedulable_gates(only_front_layer=True)
        self._schedule_gates(gates)


_RoutingEnvType = TypeVar('_RoutingEnvType', bound=RoutingEnv)


class RoutingEnvCreator(Generic[_RoutingEnvType]):
    def __init__(self, env_class: type[_RoutingEnvType], **kwargs):
        self.env_class = env_class
        self.kwargs = kwargs

    def create(self) -> _RoutingEnvType:
        return self.env_class(**self.kwargs)


class ObsModule(ABC, Generic[_RoutingEnvType]):
    @staticmethod
    @abstractmethod
    def key() -> str:
        raise NotImplementedError

    @abstractmethod
    def space(self, env: _RoutingEnvType) -> spaces.Box:
        raise NotImplementedError

    @abstractmethod
    def obs(self, env: _RoutingEnvType) -> NDArray:
        raise NotImplementedError


class LogReliabilities(ObsModule[RoutingEnv]):
    @staticmethod
    def key() -> str:
        return 'log_reliabilities'

    def space(self, env: RoutingEnv) -> spaces.Box:
        return spaces.Box(env.noise_config.min_log_reliability, 0.0, shape=env.log_reliabilities.shape)

    def obs(self, env: RoutingEnv) -> NDArray:
        return env.log_reliabilities.copy()


class CircuitMatrix(ObsModule[SequentialRoutingEnv]):
    def __init__(self, depth: int = 8):
        if depth <= 0:
            raise ValueError(f'Depth must be positive, got {depth}')

        self.depth = depth

    @staticmethod
    def key() -> str:
        return 'circuit_matrix'

    def space(self, env: SequentialRoutingEnv) -> spaces.Box:
        return spaces.Box(-1, env.num_qubits - 1, (env.num_qubits, self.depth), dtype=np.int32)

    def obs(self, env: SequentialRoutingEnv) -> NDArray:
        space = self.space(env)
        circuit = np.full(space.shape, -1, dtype=space.dtype)

        layer_idx = 0
        layer_qubits = set()

        for op_node in env.dag.two_qubit_ops():
            indices = qubits_to_indices(env.circuit, op_node.qargs)

            if layer_qubits.intersection(indices):
                layer_qubits = set(indices)
                layer_idx += 1

                if layer_idx == circuit.shape[1]:
                    break
            else:
                layer_qubits.update(indices)

            idx_a, idx_b = indices

            circuit[idx_a, layer_idx] = env.qubit_to_node[idx_b]
            circuit[idx_b, layer_idx] = env.qubit_to_node[idx_a]

        mapped_circuit = np.zeros_like(circuit)
        for q in range(env.num_qubits):
            mapped_circuit[q] = circuit[env.node_to_qubit[q]]

        return mapped_circuit


class QubitInteractions(ObsModule[RoutingEnv]):
    def __init__(self, max_depth: int = 8):
        self.max_depth = max_depth

    @staticmethod
    def key() -> str:
        return 'qubit_interactions'

    def space(self, env: LayeredRoutingEnv) -> spaces.Box:
        return spaces.Box(-1, self.max_depth, shape=(env.num_qubits * (env.num_qubits - 1) // 2,), dtype=np.int32)

    def obs(self, env: LayeredRoutingEnv) -> NDArray:
        qubit_interactions = OrderedDict()
        for i in range(env.num_qubits):
            for j in range(i):
                qubit_interactions[(j, i)] = -1

        for i, layer in enumerate(dag_layers(env.dag)[:self.max_depth]):
            for op_node in layer:
                if len(op_node.qargs) == 2:
                    indices = tuple(sorted(qubits_to_indices(env.circuit, op_node.qargs)))

                    if qubit_interactions[indices] == -1:
                        qubit_interactions[indices] = i

        return np.array(list(qubit_interactions.values()))


class LockedEdges(ObsModule[LayeredRoutingEnv]):
    @staticmethod
    def key() -> str:
        return 'locked_edges'

    def space(self, env: LayeredRoutingEnv) -> spaces.Box:
        return spaces.Box(0, 4 if env.use_decomposed_actions else 1, shape=(env.num_edges,), dtype=np.int32)

    def obs(self, env: LayeredRoutingEnv) -> NDArray:
        return np.array([max(env.scheduling_map.get(q, 0) for q in edge) for edge in env.edge_list])
