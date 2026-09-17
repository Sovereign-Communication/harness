"""Task decomposition and dependency DAG engine (#PR-1).

Decomposes high-level user instructions into a directed acyclic graph (DAG)
of atomic subtasks with explicit dependencies, target files, local verification
gates, and complexity tiers.

Provides topological sorting and concurrent batching (grouping independent
subtask leaves into parallelizable stages).
"""
from dataclasses import dataclass
import json
import re
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "instruction": self.instruction,
            "target_files": list(self.target_files),
            "dependencies": list(self.dependencies),
            "local_gate": self.local_gate,
            "complexity_tier": self.complexity_tier,
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

        return cls(
            node_id=node_id,
            instruction=instruction,
            target_files=target_files,
            dependencies=dependencies,
            local_gate=local_gate_str,
            complexity_tier=complexity_tier,
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


def build_decomposition_prompt(
    goal: str,
    repo_context: Optional[str] = None,
    candidate_files: Optional[Sequence[str]] = None,
) -> str:
    """Build a prompt instructing a model to decompose a goal into a TaskDAG JSON."""
    lines = [
        "You are an expert software architect decomposing a development task into an atomic, dependency-ordered work graph.",
        "",
        "GOAL:",
        goal.strip(),
        "",
    ]
    if candidate_files:
        lines.append("AVAILABLE / CANDIDATE FILES:")
        for cf in sorted(candidate_files):
            lines.append(f"  - {cf}")
        lines.append("")

    if repo_context:
        lines.append("REPOSITORY CONTEXT:")
        lines.append(repo_context.strip())
        lines.append("")

    lines.extend([
        "INSTRUCTIONS:",
        "1. Break the goal into small, focused, verifiable subtasks (DAG nodes).",
        "2. For each subtask, declare:",
        "   - 'node_id': unique alphanumeric identifier (e.g. 'task_1', 'task_2').",
        "   - 'instruction': precise, scoped instruction for editing or implementing that step.",
        "   - 'target_files': list of exact files modified by this subtask (at most 1-2 files per node).",
        "   - 'dependencies': list of node_ids that MUST pass before this subtask can begin.",
        "   - 'local_gate': automated verification command (e.g. 'pytest tests/test_foo.py') or null.",
        "   - 'complexity_tier': 0 (scout/simple fix), 1 (standard logic), 2 (deep architecture/frontier).",
        "3. Ensure the graph is strictly ACYCLIC (no circular dependencies).",
        "4. Independent tasks should have empty dependencies so they can run concurrently.",
        "",
        "Output ONLY valid JSON matching this schema:",
        "```json",
        "{",
        '  "nodes": [',
        '    {',
        '      "node_id": "task_1",',
        '      "instruction": "Define core types and dataclasses",',
        '      "target_files": ["pkg/types.py"],',
        '      "dependencies": [],',
        '      "local_gate": "python -m unittest tests.test_types",',
        '      "complexity_tier": 0',
        '    }',
        '  ]',
        "}",
        "```"
    ])
    return "\n".join(lines)


def parse_decomposition_response(response_text: str) -> TaskDAG:
    """Extract and parse a TaskDAG from an LLM's response text."""
    if not response_text or not response_text.strip():
        raise HarnessError("empty response for task decomposition")

    text = response_text.strip()
    # 1. Try markdown fenced code block: ```json ... ``` or ``` ... ```
    m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
    if m:
        candidate = m.group(1).strip()
    else:
        # 2. Look for outermost '{' ... '}'
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidate = text[start:end + 1]
        else:
            candidate = text

    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise HarnessError(f"failed to parse JSON from decomposition response: {exc}") from exc

    return TaskDAG.from_dict(data)
