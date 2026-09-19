"""Task decomposition and dependency DAG engine (#PR-1).

Decomposes high-level user instructions into a directed acyclic graph (DAG)
of atomic subtasks with explicit dependencies, target files, local verification
gates, and complexity tiers.

Provides topological sorting and concurrent batching (grouping independent
subtask leaves into parallelizable stages).
"""
from dataclasses import dataclass
import json
from typing import Any, Dict, List, Optional, Set, Tuple

from .errors import HarnessError


@dataclass(frozen=True)
class DAGNode:
    """An atomic unit of work in a task decomposition graph."""
    node_id: str
    instruction: str
    target_files: Tuple[str, ...] = ()
    dependencies: Tuple[str, ...] = ()
    local_gate: Optional[str] = None
    complexity_tier: int = 1
    # Execution hint for a node whose pass cannot be a whole-file rewrite
    # (the plan lane sets it when it chunks an oversized target): the engine
    # accepts only its own backend vocabulary.
    backend: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "instruction": self.instruction,
            "target_files": list(self.target_files),
            "dependencies": list(self.dependencies),
            "local_gate": self.local_gate,
            "complexity_tier": self.complexity_tier,
            "backend": self.backend,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DAGNode":
        if not isinstance(data, dict):
            raise HarnessError("DAG node must be a dict")
        node_id = str(data.get("node_id") or "").strip()
        if not node_id:
            raise HarnessError("DAG node missing node_id")
        instruction = str(data.get("instruction") or "").strip()
        if not instruction:
            raise HarnessError(f"DAG node {node_id!r} missing instruction")
        target_files = tuple(str(f).strip() for f in data.get("target_files", ()) if str(f).strip())
        dependencies = tuple(str(d).strip() for d in data.get("dependencies", ()) if str(d).strip())
        local_gate = data.get("local_gate")
        local_gate_str = str(local_gate).strip() if local_gate is not None and str(local_gate).strip() else None
        tier = data.get("complexity_tier", 1)
        try:
            complexity_tier = int(tier)
            if complexity_tier not in (0, 1, 2):
                complexity_tier = 1
        except (TypeError, ValueError):
            complexity_tier = 1
        backend = str(data.get("backend") or "").strip() or None
        if backend not in ("harness", "morph", "diff"):
            backend = None

        return cls(
            node_id=node_id,
            instruction=instruction,
            target_files=target_files,
            dependencies=dependencies,
            local_gate=local_gate_str,
            complexity_tier=complexity_tier,
            backend=backend,
        )


@dataclass(frozen=True)
class TaskDAG:
    """A validated directed acyclic graph of subtasks."""
    nodes: Dict[str, DAGNode]

    def __post_init__(self):
        self.validate()

    def validate(self) -> None:
        """Validate DAG integrity: unknown dependencies and cycles."""
        for node_id, node in self.nodes.items():
            if node_id != node.node_id:
                raise HarnessError(f"node key {node_id!r} does not match node_id {node.node_id!r}")
            for dep in node.dependencies:
                if dep not in self.nodes:
                    raise HarnessError(f"node {node_id!r} has unknown dependency {dep!r}")
                if dep == node_id:
                    raise HarnessError(f"node {node_id!r} cannot depend on itself")

        # Cycle detection using Kahn's algorithm
        in_degree: Dict[str, int] = {k: 0 for k in self.nodes}
        for node in self.nodes.values():
            for _dep in node.dependencies:
                # dep must run before node, so edge is dep -> node
                in_degree[node.node_id] += 1

        # We can also verify reachability and cycles
        queue = [k for k, d in in_degree.items() if d == 0]
        visited = 0
        # Adjacency list: dep -> list of nodes depending on dep
        adj: Dict[str, List[str]] = {k: [] for k in self.nodes}
        for node in self.nodes.values():
            for dep in node.dependencies:
                adj[dep].append(node.node_id)

        while queue:
            curr = queue.pop(0)
            visited += 1
            for nxt in adj[curr]:
                in_degree[nxt] -= 1
                if in_degree[nxt] == 0:
                    queue.append(nxt)

        if visited < len(self.nodes):
            raise HarnessError("cycle detected in TaskDAG")

    def topological_batches(self) -> List[List[DAGNode]]:
        """Partition nodes into dependency-free batches that can run in parallel.

        Batch 0 has no dependencies.
        Batch k depends only on nodes in batches < k.
        """
        if not self.nodes:
            return []

        remaining: Dict[str, DAGNode] = dict(self.nodes)
        completed: Set[str] = set()
        batches: List[List[DAGNode]] = []

        while remaining:
            # Nodes whose dependencies are all satisfied
            current_batch = [
                node for node in remaining.values()
                if all(dep in completed for dep in node.dependencies)
            ]
            if not current_batch:
                raise HarnessError("deadlock in topological batching (unreachable nodes)")

            # Deterministic sorting within batch by node_id
            current_batch.sort(key=lambda n: n.node_id)
            batches.append(current_batch)

            for node in current_batch:
                completed.add(node.node_id)
                del remaining[node.node_id]

        return batches

    def topological_order(self) -> List[DAGNode]:
        """Return a linear execution order of all nodes."""
        result: List[DAGNode] = []
        for batch in self.topological_batches():
            result.extend(batch)
        return result

    def ready_nodes(self, completed: Set[str]) -> List[DAGNode]:
        """Return all nodes whose dependencies are satisfied but not yet completed."""
        ready = [
            node for node in self.nodes.values()
            if node.node_id not in completed and all(dep in completed for dep in node.dependencies)
        ]
        ready.sort(key=lambda n: n.node_id)
        return ready

    def to_dict(self) -> Dict[str, Any]:
        return {
            "nodes": [node.to_dict() for node in self.topological_order()]
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TaskDAG":
        if not isinstance(data, dict):
            raise HarnessError("TaskDAG data must be a dict")
        nodes_list = data.get("nodes")
        if not isinstance(nodes_list, list):
            raise HarnessError("TaskDAG 'nodes' must be a list")
        nodes_map = {}
        for item in nodes_list:
            node = DAGNode.from_dict(item)
            if node.node_id in nodes_map:
                raise HarnessError(f"duplicate node_id {node.node_id!r}")
            nodes_map[node.node_id] = node
        return cls(nodes=nodes_map)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_json(cls, text: str) -> "TaskDAG":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise HarnessError(f"malformed JSON for TaskDAG: {exc}") from exc
        return cls.from_dict(data)



# --- Lazy re-exports of plan policy now owned by waist (single owner) ---
# DAG remains pure data; policy lives in waist. Tests and callers that
# import these names from harness.dag keep working via these shims.

def build_decomposition_prompt(*args, **kwargs):
    from .waist import build_decomposition_prompt as _impl
    return _impl(*args, **kwargs)

def _parse_json_object(*args, **kwargs):
    from .waist import _parse_json_object as _impl
    return _impl(*args, **kwargs)

def parse_decomposition_response(*args, **kwargs):
    from .waist import parse_decomposition_response as _impl
    return _impl(*args, **kwargs)

def heuristic_decompose_goal(*args, **kwargs):
    from .waist import heuristic_decompose_goal as _impl
    return _impl(*args, **kwargs)

def decompose_via_llm(*args, **kwargs):
    from .waist import decompose_via_llm as _impl
    return _impl(*args, **kwargs)

def plan_task(*args, **kwargs):
    from .waist import plan_task as _impl
    return _impl(*args, **kwargs)

def node_apply_kwargs(*args, **kwargs):
    from .waist import node_apply_kwargs as _impl
    return _impl(*args, **kwargs)

def build_waist_prompt(*args, **kwargs):
    from .waist import build_waist_prompt as _impl
    return _impl(*args, **kwargs)

def _waist_window_request(*args, **kwargs):
    from .waist import _waist_window_request as _impl
    return _impl(*args, **kwargs)

def parse_waist_verdict(*args, **kwargs):
    from .waist import parse_waist_verdict as _impl
    return _impl(*args, **kwargs)
