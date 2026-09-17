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
from .sliding_scale import resolve_sliding_scale_route
from .validation import MAX_INSTRUCTION_CHARS


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


def _parse_json_object(response_text: str, what: str) -> Dict[str, Any]:
    """Extract a JSON object from an LLM response: markdown fenced block
    first, then the outermost brace span, else the raw text. ONE owner of
    that extraction (decomposition and waist verdicts share it)."""
    if not response_text or not response_text.strip():
        raise HarnessError(f"empty response for {what}")

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
        raise HarnessError(f"failed to parse JSON from {what}: {exc}") from exc
    if not isinstance(data, dict):
        raise HarnessError(f"{what} must be a JSON object")
    return data


def parse_decomposition_response(response_text: str) -> TaskDAG:
    """Extract and parse a TaskDAG from an LLM's response text."""
    data = _parse_json_object(response_text, "task decomposition")
    return TaskDAG.from_dict(data)


def heuristic_decompose_goal(goal: str, candidate_files: Optional[Sequence[str]] = None) -> TaskDAG:
    # Deterministic heuristic decomposition into a TaskDAG
    if not goal or not goal.strip():
        raise HarnessError("goal cannot be empty")

    files = [f.strip() for f in (candidate_files or []) if str(f).strip()]
    nodes: Dict[str, DAGNode] = {}

    # Check for numbered or bulleted steps in goal
    steps = [s.strip() for s in re.split(r"(?:^|\n)\s*(?:\d+[\.\)]|[-*])\s+", goal.strip()) if s.strip()]

    if len(steps) > 1:
        # Multi-step goal
        prev_id = None
        for i, step in enumerate(steps, 1):
            node_id = f"task_{i}"
            deps = (prev_id,) if prev_id else ()
            target = (files[i - 1],) if i - 1 < len(files) else ()
            nodes[node_id] = DAGNode(
                node_id=node_id,
                instruction=step,
                target_files=target,
                dependencies=deps,
                complexity_tier=1,
            )
            prev_id = node_id
    elif len(files) > 1:
        # Multi-file goal: one subtask per file
        test_files = [f for f in files if "test" in f.lower()]
        impl_files = [f for f in files if f not in test_files]

        impl_ids = []
        for i, f in enumerate(impl_files, 1):
            node_id = f"task_{i}"
            impl_ids.append(node_id)
            nodes[node_id] = DAGNode(
                node_id=node_id,
                instruction=f"{goal.strip()} for {f}",
                target_files=(f,),
                dependencies=(),
                complexity_tier=1,
            )

        start_test_idx = len(impl_files) + 1
        for j, f in enumerate(test_files, start_test_idx):
            node_id = f"task_{j}"
            nodes[node_id] = DAGNode(
                node_id=node_id,
                instruction=f"Update tests in {f} for {goal.strip()}",
                target_files=(f,),
                dependencies=tuple(impl_ids),
                complexity_tier=1,
            )
    else:
        # Single node goal
        nodes["task_1"] = DAGNode(
            node_id="task_1",
            instruction=goal.strip(),
            target_files=tuple(files),
            dependencies=(),
            complexity_tier=1,
        )

    return TaskDAG(nodes=nodes)


def decompose_via_llm(
    chat_fn,
    goal: str,
    candidate_files: Optional[Sequence[str]] = None,
    repo_context: Optional[str] = None,
) -> TaskDAG:
    """M1 seam: a cheap model authors the DAG; strict schema validation.

    ``chat_fn(prompt) -> response text`` is injected (governed upstream via
    ``chat.governed_text``), so this stays pure and hermetically testable.
    Raises HarnessError on empty/invalid responses, an empty node set, or
    nodes that would fail apply-time validation (instruction length). The
    heuristic decomposition stays the caller's fallback: planning may
    degrade loudly, spending may not degrade silently.
    """
    prompt = build_decomposition_prompt(
        goal, repo_context=repo_context, candidate_files=candidate_files)
    dag = parse_decomposition_response(chat_fn(prompt))
    if not dag.nodes:
        raise HarnessError("LLM decomposition returned no nodes")
    for node in dag.nodes.values():
        if len(node.instruction) > MAX_INSTRUCTION_CHARS:
            raise HarnessError(
                f"LLM decomposition node {node.node_id!r} instruction exceeds "
                f"{MAX_INSTRUCTION_CHARS} chars (would fail apply validation)")
    return dag


def plan_task(
    goal: str,
    candidate_files: Optional[Sequence[str]] = None,
    repo_context: Optional[str] = None,
    custom_frontier: Optional[str] = None,
    use_free: bool = True,
    decomposed_dag: Optional[TaskDAG] = None,
) -> Dict[str, Any]:
    # Formulate a TaskDAG and classify sliding-scale tiers for each node.
    # decomposed_dag: a pre-built DAG (LLM-authored via decompose_via_llm or
    # waist-amended) replacing the heuristic decomposition; tier
    # classification and ceiling math are identical for either origin.
    dag = (decomposed_dag if decomposed_dag is not None
           else heuristic_decompose_goal(goal, candidate_files))
    node_details: List[Dict[str, Any]] = []
    total_ceiling = 0.0

    classified_nodes: Dict[str, DAGNode] = {}
    batches = dag.topological_batches()
    depth_map: Dict[str, int] = {}
    for depth, batch in enumerate(batches):
        for n in batch:
            depth_map[n.node_id] = depth

    for node_id, node in dag.nodes.items():
        is_leaf = len(node.dependencies) == 0
        depth = depth_map.get(node_id, 0)
        route = resolve_sliding_scale_route(
            instruction=node.instruction,
            target_files=node.target_files,
            dependency_depth=depth,
            is_leaf=is_leaf,
            use_free=use_free,
            custom_frontier=custom_frontier,
        )
        total_ceiling += route.cost_ceiling
        classified_nodes[node_id] = DAGNode(
            node_id=node.node_id,
            instruction=node.instruction,
            target_files=node.target_files,
            dependencies=node.dependencies,
            local_gate=node.local_gate,
            complexity_tier=route.classification.tier,
        )
        node_details.append({
            "node_id": node.node_id,
            "instruction": node.instruction,
            "target_files": list(node.target_files),
            "dependencies": list(node.dependencies),
            "local_gate": node.local_gate,
            "complexity_tier": route.classification.tier,
            "recommended_model": route.classification.recommended_model,
            "cost_ceiling": route.cost_ceiling,
            "classification": {
                "tier": route.classification.tier,
                "score": route.classification.score,
                "reasons": list(route.classification.reasons),
                "estimated_cost_tier": route.classification.estimated_cost_tier,
            },
            "route": {
                "ladder": list(route.ladder),
                "cost_ceiling": route.cost_ceiling,
            },
        })

    enriched_dag = TaskDAG(nodes=classified_nodes)
    return {
        "status": "planned",
        "goal": goal.strip(),
        "total_nodes": len(enriched_dag.nodes),
        "batches": [[n.node_id for n in b] for b in enriched_dag.topological_batches()],
        "nodes": node_details,
        "total_cost_ceiling": round(total_ceiling, 4),
        "dag": enriched_dag.to_dict(),
    }


def node_apply_kwargs(
    node_detail: Optional[Dict[str, Any]] = None,
    explicit_model: Optional[str] = None,
    explicit_task_max_cost: Optional[float] = None,
) -> Dict[str, Any]:
    """Per-request apply kwargs for executing one planned DAG node.

    Threads a node's sliding-scale route (from :func:`plan_task`'s
    ``nodes`` entries) into :meth:`harness.apply.ApplyEngine.apply_edit`:
    the tier ladder becomes the request's ``apply_pool`` -- the engine
    orders it at the routing boundary via ``capability.ordered_pool``
    (interfaces never pre-order a pool) and rotation stays inside the
    tier-appropriate models. The tier cost ceiling becomes the per-task
    ceiling only when it is a real bound (paid tiers): a $0 free-tier
    ceiling is deliberately NOT passed, because a zero task budget would
    refuse the escalation ladder before it could rescue a hard node
    (``spent - start < 0.0`` is never true); free-tier cost discipline is
    the governor's job (every attempted model bills $0.0).

    An explicit model pin wins outright (manual routing, no pool override);
    an explicit ``task_max_cost`` pin suppresses only the ceiling.
    Unknown or missing route detail degrades to today's defaults ({}).
    """
    if explicit_model is not None:
        return {}
    detail = node_detail or {}
    route = detail.get("route") or {}
    kwargs = {}
    ladder = [str(m).strip() for m in (route.get("ladder") or []) if str(m).strip()]
    if ladder:
        kwargs["apply_pool"] = ladder
    if explicit_task_max_cost is None:
        try:
            ceiling = float(route.get("cost_ceiling"))
        except (TypeError, ValueError):
            ceiling = 0.0
        if ceiling > 0.0:
            kwargs["task_max_cost"] = ceiling
    return kwargs



def build_waist_prompt(
    plan_result: Dict[str, Any],
    brief_context: str = "",
    window_context: str = "",
) -> str:
    """Build the frontier waist-confirmation prompt (M2).

    The brief (file signatures + bounded windows + failure evidence) is the
    confirming model's ONLY repo access: bounded file-window round-trips
    replace open-ended reading. The verdict contract is strict JSON, one of::

        {"verdict": "approve"}
        {"verdict": "amend", "nodes": [ ...same node schema as decomposition... ]}
        {"verdict": "refuse", "reason": "...", "evidence": "cited brief section"}
        {"verdict": "request_windows",
         "file_window_requests": [{"path": "p/x.py", "start_line": 1, "end_line": 80}]}

    ``amend`` replaces the whole node set (subdividing a node is just an
    amend), and the replacement re-validates through the same schema as the
    original plan. A refusal must cite the brief section that fails --
    honest evidence, not vibes.
    """
    nodes = [
        {k: n[k] for k in ("node_id", "instruction", "target_files",
                           "dependencies", "local_gate", "complexity_tier")
         if k in n}
        for n in plan_result.get("nodes", [])
    ]
    lines = [
        "You are the plan-confirmation gate for an autonomous coding harness.",
        "A cheaper model decomposed the goal below into an executable DAG.",
        "Confirm the plan BEFORE execution spend: check decomposition quality,",
        "tier assignments (0=scout/simple, 1=standard, 2=deep/frontier),",
        "dependency ordering, and target-file scoping.",
        "",
        "GOAL:",
        plan_result.get("goal", "").strip(),
        "",
        "PLANNED DAG (JSON):",
        json.dumps({"nodes": nodes}, indent=2),
        "",
    ]
    if brief_context:
        lines.extend(["REPOSITORY BRIEF (signatures; your only repo access):",
                      brief_context.strip(), ""])
    if window_context:
        lines.extend(["REQUESTED FILE WINDOWS (attached this round):",
                      window_context.strip(), ""])
    lines.extend([
        "VERDICT CONTRACT -- respond with ONLY one JSON object:",
        '  {"verdict": "approve"}                                   plan is sound as routed',
        '  {"verdict": "amend", "nodes": [...]}                     full replacement node set,',
        '                                                           same schema, re-validated;',
        '                                                           subdividing a node is an amend',
        '  {"verdict": "refuse", "reason": "...", "evidence": "..."}  cite the brief section',
        '                                                           that fails; evidence required',
        '  {"verdict": "request_windows", "file_window_requests": [...]}  need bounded source',
        '                                                           windows before deciding',
        '                                                            ({"path", "start_line",',
        '                                                              "end_line"}; 1-based,',
        '                                                              end inclusive)',
        "Rules: the DAG must stay acyclic; node instructions must be precise and",
        "scoped (they are executed verbatim by cheaper models); do not request",
        "more windows than you need -- round-trips are budgeted.",
    ])
    return "\n".join(lines)


def _waist_window_request(raw) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise HarnessError("waist file_window_requests entries must be objects")
    path = str(raw.get("path") or "").strip()
    if not path:
        raise HarnessError("waist file_window_request missing 'path'")
    request: Dict[str, Any] = {"path": path}
    for key in ("start_line", "end_line"):
        value = raw.get(key)
        if value is None:
            continue
        try:
            line = int(value)
        except (TypeError, ValueError):
            raise HarnessError(
                f"waist file_window_request {key} must be an integer") from None
        if line < 1:
            raise HarnessError(f"waist file_window_request {key} must be >= 1")
        request[key] = line
    if ("start_line" in request or "end_line" in request) and \
            request.get("start_line", 1) > request.get("end_line", 1 << 30):
        raise HarnessError("waist file_window_request start_line exceeds end_line")
    return request


def parse_waist_verdict(response_text: str) -> Dict[str, Any]:
    """Parse and validate a waist verdict (strict; fail-closed).

    Returns one of ``{"verdict": "approve"}``, ``{"verdict": "amend",
    "dag": TaskDAG}`` (the replacement node set, re-validated for unique
    ids, unknown dependencies, cycles, and apply-time instruction
    length), ``{"verdict": "refuse", "reason", "evidence"}``, or
    ``{"verdict": "request_windows", "file_window_requests": [...]}``.
    """
    data = _parse_json_object(response_text, "waist plan verdict")
    verdict = str(data.get("verdict") or "").strip()
    if verdict == "approve":
        return {"verdict": "approve"}
    if verdict == "amend":
        nodes = data.get("nodes")
        if not isinstance(nodes, list) or not nodes:
            raise HarnessError(
                "waist amend verdict requires a non-empty 'nodes' list")
        amended = TaskDAG.from_dict({"nodes": nodes})
        for node in amended.nodes.values():
            if len(node.instruction) > MAX_INSTRUCTION_CHARS:
                raise HarnessError(
                    f"amended node {node.node_id!r} instruction exceeds "
                    f"{MAX_INSTRUCTION_CHARS} chars (would fail apply validation)")
        return {"verdict": "amend", "dag": amended}
    if verdict == "refuse":
        reason = str(data.get("reason") or "").strip()
        evidence = str(data.get("evidence") or "").strip()
        if not reason or not evidence:
            raise HarnessError(
                "waist refuse verdict requires non-empty 'reason' and "
                "'evidence' (cite the brief section that fails)")
        return {"verdict": "refuse", "reason": reason, "evidence": evidence}
    if verdict == "request_windows":
        requests = data.get("file_window_requests")
        if not isinstance(requests, list) or not requests:
            raise HarnessError(
                "waist request_windows verdict requires a non-empty "
                "'file_window_requests' list")
        return {"verdict": "request_windows",
                "file_window_requests": [_waist_window_request(r) for r in requests]}
    raise HarnessError(f"unknown waist verdict {verdict!r}")
