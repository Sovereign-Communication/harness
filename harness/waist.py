"""Plan-confirmation waist (M2) and LLM decomposition lane (M1).

The wide base plans (heuristic, or a cheap model via
``decompose_via_llm``); the waist confirms or repairs the plan with ONE
frontier model round-trip budget before any execution spend; execution then
honors the confirmed routing (``node_apply_kwargs``).

Cost discipline: the frontier's brief (file signatures + bounded source
windows) is its ONLY repo access -- bounded ``request_windows`` round-trips
replace open-ended reading, capped at ``MAX_WAIST_ROUNDS``. Every call is
preflighted and billed through the run's ONE governor (the caller's, so
decomposition + confirmation + execution share a single ceiling). Every
terminal verdict is ledgered with its round count and cost.

Sovereignty: the confirming model can refuse -- and a refusal is evidence,
ledgered, and stops execution (fail-closed: a refused plan never dispatches).
"""
import hashlib
import json
import math
import os
import re
from typing import Any, Dict, List, Optional, Sequence

from .capability import load_profiles, source_budget_for
from .chat import governed_text
from .condenser import distill_context
from .config import CAPABILITIES_PATH, DEFAULT_APPLY_MAX_TOKENS, ESCALATION_POOL_FREE, ESCALATION_POOL_PAID, FREE_JUDGE
from .dag import DAGNode, TaskDAG
from .errors import HarnessError
from .output import eprint
from .prompts import MAX_FILE_LINES
from .repo_scope import discover_verification_gate, gate_for_targets
from .sliding_scale import resolve_frontier_model, resolve_sliding_scale_route
from .tokens import estimate_prompt_tokens
from .validation import MAX_INSTRUCTION_CHARS
from .jev_packs import build_context_pack
from .jev_policy import JevPolicy

MAX_WAIST_ROUNDS = 2
MAX_WINDOWS_PER_ROUND = 8
MAX_WINDOW_LINES = 200
WAIST_MAX_TOKENS = 1500
DECOMPOSE_MAX_TOKENS = 1024
MAX_BRIEF_FILES = 12


def single_pass_output_tokens(max_tokens: Optional[int] = None) -> int:
    """The output budget ONE model pass really has for a node.

    The lane's pinned ``max_tokens`` when it has one, otherwise the apply
    lane's own default (``config.DEFAULT_APPLY_MAX_TOKENS`` -- the exact
    number ``apply_edit`` falls back to). Chunking measures against this
    budget instead of inventing one of its own.
    """
    try:
        pinned = int(max_tokens)
    except (TypeError, ValueError):
        pinned = 0
    return pinned if pinned > 0 else DEFAULT_APPLY_MAX_TOKENS


def _split_text(text: str, limit: int) -> List[str]:
    """Greedy word-boundary split into pieces of at most ``limit`` chars.

    Nothing is lost or duplicated: every piece concatenates back to the
    original (inter-word whitespace is preserved; only the whitespace at a
    break point is trimmed). A single unbreakable token longer than the
    limit is hard-split, so the bound always holds.
    """
    limit = max(1, int(limit))
    parts: List[str] = []
    current = ""
    for token in re.findall(r"\S+\s*", text):
        while len(token) > limit:
            if current:
                parts.append(current.rstrip())
                current = ""
            parts.append(token[:limit])
            token = token[limit:]
        if current and len(current) + len(token) > limit:
            parts.append(current.rstrip())
            current = ""
        current += token
    if current.strip():
        parts.append(current.rstrip())
    return parts or [text]


def _target_text(root: Optional[str], rel: str) -> Optional[str]:
    """The target file's text from the plan lane's tree (None if unreadable)."""
    path = rel if os.path.isabs(rel) else os.path.join(root or os.getcwd(), rel)
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def _passes_to_read(text: str, source_tokens: int) -> int:
    """How many passes are needed to even READ one target in full.

    This is the only file-size reason to split: when one pass cannot hold
    the file's source. A file that is merely larger than the rewrite cap is
    NOT split -- a bounded-hunk (diff) edit emits only what it changes, so
    it fits one pass. A budget of 0 means UNMEASURED (the rung's declared
    context is unknown), and an unmeasured rung is never given an invented
    limit: nothing is split on the file axis.
    """
    if source_tokens <= 0:
        return 1
    return max(1, math.ceil(estimate_prompt_tokens(text) / source_tokens))


def rung_read_budgets(plan_result: Dict[str, Any],
                      profiles: Optional[Dict[str, Any]] = None
                      ) -> Dict[str, int]:
    """Per-node source-read budget from each node's OWN routing rung.

    The rung is the model the node will actually run on, so its declared
    context (via the capability registry -- offline cache, no fetch) bounds
    what one pass can read. An unknown rung yields 0 (unmeasured), and the
    chunk policy then leaves the file axis alone rather than guessing.
    """
    if profiles is None:
        profiles, _ = load_profiles(CAPABILITIES_PATH)
    budgets: Dict[str, int] = {}
    for detail in plan_result.get("nodes", ()):
        if not isinstance(detail, dict):
            continue
        route = detail.get("route")
        ladder = route.get("ladder") if isinstance(route, dict) else None
        head = str(ladder[0]).strip() if ladder else ""
        profile = profiles.get(head) if head else None
        context = getattr(profile, "context_length", None)
        budgets[str(detail.get("node_id") or "")] = (
            source_budget_for(context) if context else 0)
    return budgets


def _line_ranges(line_count: int, passes: int) -> List[tuple]:
    """Contiguous 1-based line ranges covering the file in EXACTLY ``passes``
    slices.

    The count is fixed by the final chunk count, which can exceed what the
    file's own size implies (a long instruction split over a small file), so
    the tail is padded: the extra passes share the last slice rather than
    leaving a chunk with no range to name.
    """
    passes = max(1, int(passes))
    per = max(1, math.ceil(line_count / passes)) if line_count else 1
    ranges: List[tuple] = []
    start = 1
    while start <= line_count:
        end = min(line_count, start + per - 1)
        ranges.append((start, end))
        start = end + 1
    while len(ranges) < passes:
        ranges.append(ranges[-1] if ranges else (1, 1))
    return ranges[:passes]


def chunk_oversized_nodes(
    dag: TaskDAG,
    *,
    root: Optional[str] = None,
    max_tokens: Optional[int] = None,
    max_lines: int = MAX_FILE_LINES,
    source_tokens=None,
) -> TaskDAG:
    """Split nodes that cannot fit ONE model pass into ordered chunks.

    ONE owner of the chunking policy, and it lives in the plan lane so no
    lane can drift from it. Work is chunked only when one pass genuinely
    cannot do it:

    * the instruction is longer than the apply validation ceiling
      (``validation.MAX_INSTRUCTION_CHARS`` -- such a request could never be
      sent, so it must become several requests), or
    * one pass cannot even READ the target (more estimated tokens than the
      pass's quoted-source budget, ``capability.source_budget_for``; a node
      whose rung declares no context is left alone -- see ``source_tokens``).

    A target past the engine's whole-file rewrite cap (``max_lines``) -- or
    one whose rewrite could not fit the pass's output budget at all -- is
    NOT split: it becomes ONE node carrying the ``backend="diff"`` hint, so
    the pass emits bounded hunks instead of a whole-file rewrite and the
    small edit stays a single call.

    A split node becomes ``max(axes)`` chunks, each inside budget, chained
    in order so the existing DAG dispatch runs them one pass at a time;
    every dependent of the original node now depends on its last chunk, so
    the graph stays acyclic and correctly ordered.

    ``source_tokens`` is the measured read budget: either one int for every
    node, or a ``{node_id: tokens}`` mapping (the plan lane passes each
    node's own rung budget); 0 or a missing entry means unmeasured.

    Returns the SAME dag object when nothing needs splitting, so ordinary
    plans take no detour through this policy.
    """
    if not dag.nodes:
        return dag
    output_tokens = single_pass_output_tokens(max_tokens)

    def budget_for(node_id: str) -> int:
        if isinstance(source_tokens, dict):
            return int(source_tokens.get(node_id) or 0)
        return int(source_tokens or 0)

    chunked: Dict[str, List[DAGNode]] = {}
    replacements: Dict[str, str] = {}
    single_pass_hints: Dict[str, str] = {}
    for node_id, node in dag.nodes.items():
        target = node.target_files[0] if node.target_files else None
        text = _target_text(root, target) if target else None
        line_count = len(text.splitlines()) if text is not None else 0
        passes = 1
        ranges: List[tuple] = []
        if text is not None:
            file_tokens = estimate_prompt_tokens(text)
            # A rewrite that cannot fit one pass is solved by emitting bounded
            # hunks, not by splitting: each range pass would still emit the
            # whole file, so splitting would only add calls.
            if (line_count > max_lines or file_tokens > output_tokens) \
                    and not node.backend:
                single_pass_hints[node_id] = "diff"
            # Splitting is for what one pass cannot read at all.
            passes = _passes_to_read(text, budget_for(node_id))
            if passes > 1:
                ranges = _line_ranges(line_count, passes)

        # Both axes must fit the same chunk set: grow the count until the
        # instruction (split at word boundaries) plus the longest slice
        # marker a chunk will carry stays inside MAX_INSTRUCTION_CHARS.
        while True:
            budget = max(1, MAX_INSTRUCTION_CHARS
                         - len(_slice_marker(passes, ranges, target)))
            if len(_split_text(node.instruction, budget)) <= passes:
                break
            passes += 1
            ranges = _line_ranges(line_count, passes) if line_count else []
        if passes <= 1:
            continue

        budget = max(1, MAX_INSTRUCTION_CHARS
                     - len(_slice_marker(passes, ranges, target)))
        parts = _split_text(node.instruction, budget)
        # Range passes edit bounded hunks of a file that no single pass can
        # hold; instruction-only passes keep the node's own backend.
        backend = (node.backend or single_pass_hints.get(node_id)
                   or ("diff" if line_count > max_lines else None))
        chunks: List[DAGNode] = []
        for index in range(1, passes + 1):
            part = parts[((index - 1) * len(parts)) // passes]
            chunks.append(DAGNode(
                node_id=f"{node_id}.{index}",
                instruction=part + _slice_marker(passes, ranges, target, index),
                target_files=node.target_files,
                dependencies=((f"{node_id}.{index - 1}",) if index > 1
                              else node.dependencies),
                local_gate=node.local_gate,
                complexity_tier=node.complexity_tier,
                backend=backend,
            ))
        chunked[node_id] = chunks
        replacements[node_id] = chunks[-1].node_id

    if not chunked and not single_pass_hints:
        return dag

    nodes: Dict[str, DAGNode] = {}
    for node_id, node in dag.nodes.items():
        if node_id in chunked:
            for chunk in chunked[node_id]:
                nodes[chunk.node_id] = chunk
            continue
        deps = tuple(replacements.get(dep, dep) for dep in node.dependencies)
        backend = node.backend or single_pass_hints.get(node_id)
        if deps == node.dependencies and backend == node.backend:
            nodes[node_id] = node
            continue
        nodes[node_id] = DAGNode(
            node_id=node.node_id,
            instruction=node.instruction,
            target_files=node.target_files,
            dependencies=deps,
            local_gate=node.local_gate,
            complexity_tier=node.complexity_tier,
            backend=backend,
        )
    return TaskDAG(nodes=nodes)


def _slice_marker(passes: int, ranges: List[tuple], target,
                  index: Optional[int] = None) -> str:
    """The slice marker a chunk carries (and the string the budget reserves).

    Called with ``index=None`` it returns the LONGEST marker the split will
    need, so reserving its length keeps every instruction plus marker within
    the apply validation ceiling.
    """
    position = index if index is not None else passes
    marker = f"\n\n[pass {position}/{passes}"
    if ranges and target:
        start, end = ranges[position - 1]
        marker += f"; apply ONLY {target} lines {start}-{end} of {ranges[-1][1]}"
    return marker + "]"

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
        "2. EVERY node MUST end in a file being written or modified: each node",
        "   is executed by a code-writing engine that receives the node's",
        "   instruction and edits its target file. Do NOT create analysis,",
        "   research, inspection, planning, or review nodes -- the planning",
        "   pass already gathered that context. An instruction like 'inspect",
        "   X' or 'determine what to test' is ALWAYS WRONG: fold whatever it",
        "   was meant to learn into the writing node's instruction instead.",
        "3. For each subtask, declare:",
        "   - 'node_id': unique alphanumeric identifier (e.g. 'task_1', 'task_2').",
        "   - 'instruction': the COMPLETE, self-contained change to make to the",
        "     target file in this step (the executor sees no other context):",
        "     what to write, with the exact behavior, signatures, and cases.",
        "   - 'target_files': list of exact files modified by this subtask (at most 1-2 files per node).",
        "   - 'dependencies': list of node_ids that MUST pass before this subtask can begin.",
        "   - 'local_gate': automated verification command run in the target",
        "     file's directory (e.g. 'python -m unittest test_types') or null.",
        "   - 'complexity_tier': 0 (scout/simple fix), 1 (standard logic), 2 (deep architecture/frontier).",
        "4. Ensure the graph is strictly ACYCLIC (no circular dependencies).",
        "5. Independent tasks should have empty dependencies so they can run concurrently.",
        "",
        "Output ONLY valid JSON matching this schema:",
        "```json",
        "{",
        '  "nodes": [',
        '    {',
        '      "node_id": "task_1",',
        '      "instruction": "Create pkg/types.py defining the ShipmentsFilter dataclass with fields query (str), limit (int, default 20); include a __repr__.",',
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
        target = tuple(files)
        if not target:
            # If no files were provided, check if the goal mentions a specific file
            found = re.findall(r"\b[\w-]+\.(?:py|rs|go|ts|js|md|json|toml|yaml|yml|c|cpp|h)\b", goal)
            if found:
                target = (found[0],)
            else:
                # Default deliverable target for analytical / open-ended tasks
                target = ("docs/reports/analysis.md",)
        nodes["task_1"] = DAGNode(
            node_id="task_1",
            instruction=goal.strip(),
            target_files=target,
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



def _call_worst_case(governor, model, max_tokens) -> float:
    """Worst-case dollar cost of ONE bounded chat call on ``model``.

    Completion-side only (prompt tokens are typically small relative to the
    pinned output budget for plan-lane calls). Free / unpriceable models
    contribute $0.0 -- the node ceilings still bind the run.
    """
    if governor is None or not model:
        return 0.0
    try:
        pricing = governor.fetch_pricing([model])
        _pp, cp = pricing[model]
    except Exception:
        return 0.0
    try:
        return max(0.0, float(max_tokens) * float(cp))
    except (TypeError, ValueError):
        return 0.0


def composed_worst_case(
    plan_result: Dict[str, Any],
    *,
    governor=None,
    decompose_llm: bool = False,
    confirm: bool = False,
    decompose_model: Optional[str] = None,
    frontier_model: Optional[str] = None,
    use_free: bool = True,
    allow_escalation: bool = False,
    plan_consensus: bool = False,
) -> Dict[str, Any]:
    """Composed pyramid ceiling: decompose + consensus + waist + node sum.

    ONE owner of the pre-execute worst-case budget. Called before execute
    spend when a governor is present; if the composed total exceeds
    ``governor.remaining()`` the plan lane REFUSES instead of dispatching
    into an unfunded pyramid. The envelope always carries the breakdown so
    an operator can see which rung would blow the ceiling.
    """
    node_ceiling = 0.0
    try:
        node_ceiling = float(plan_result.get("total_cost_ceiling") or 0.0)
    except (TypeError, ValueError):
        node_ceiling = 0.0
    if node_ceiling <= 0.0:
        for detail in plan_result.get("nodes") or ():
            if not isinstance(detail, dict):
                continue
            route = detail.get("route") if isinstance(detail.get("route"), dict) else {}
            try:
                node_ceiling += float(route.get("cost_ceiling") or detail.get("cost_ceiling") or 0.0)
            except (TypeError, ValueError):
                continue

    decompose_cost = 0.0
    waist_cost = 0.0
    consensus_cost = 0.0
    if governor is not None:
        if decompose_llm:
            model = decompose_model
            if not model:
                try:
                    model = resolve_scout_ladder(use_free=use_free,
                                                 custom_frontier=frontier_model)[0]
                except Exception:
                    model = None
            decompose_cost = _call_worst_case(governor, model, DECOMPOSE_MAX_TOKENS)
        if plan_consensus:
            model = decompose_model
            if not model:
                try:
                    model = resolve_scout_ladder(use_free=use_free,
                                                 custom_frontier=frontier_model)[0]
                except Exception:
                    model = None
            consensus_cost = _call_worst_case(governor, model, 256)
        if confirm:
            try:
                ladder = resolve_waist_ladder(
                    use_free=use_free, custom_frontier=frontier_model,
                    allow_escalation=allow_escalation)
            except Exception:
                ladder = [frontier_model] if frontier_model else []
            # Worst-case: every ladder rung could be attempted across the
            # window-round budget before one lands.
            for rung in ladder[:4]:
                waist_cost += _call_worst_case(
                    governor, rung, WAIST_MAX_TOKENS * MAX_WAIST_ROUNDS)

    composed = round(node_ceiling + decompose_cost + waist_cost + consensus_cost, 6)
    remaining = None
    if governor is not None and callable(getattr(governor, "remaining", None)):
        try:
            remaining = float(governor.remaining())
        except Exception:
            remaining = None
    plan_ceiling = None
    if governor is not None and getattr(governor, "max_cost", None) is not None:
        try:
            plan_ceiling = round(float(governor.max_cost), 6)
        except (TypeError, ValueError):
            plan_ceiling = None
    return {
        "composed_worst_case": composed,
        "node_ceiling": round(node_ceiling, 6),
        "decompose": round(decompose_cost, 6),
        "waist": round(waist_cost, 6),
        "consensus": round(consensus_cost, 6),
        "remaining": remaining,
        "plan_ceiling": plan_ceiling,
        "exceeds_remaining": (
            None if remaining is None else bool(composed > remaining + 1e-12)),
    }


def _refuse_composed_ceiling(plan_result: Dict[str, Any], composed: Dict[str, Any]) -> Dict[str, Any]:
    """Fail-closed envelope when the composed pyramid ceiling cannot fit."""
    refused = dict(plan_result)
    refused["status"] = "refused"
    refused["composed_worst_case"] = composed
    reason = (
        f"composed worst-case ${composed['composed_worst_case']:.6f} exceeds "
        f"remaining budget ${composed['remaining']:.6f}")
    refused["confirmation"] = {
        "verdict": "refused",
        "model": "composed-ceiling",
        "rounds": 0,
        "reason": reason,
        "evidence": (
            f"nodes={composed['node_ceiling']:.6f} "
            f"decompose={composed['decompose']:.6f} "
            f"waist={composed['waist']:.6f} "
            f"consensus={composed['consensus']:.6f}"),
        "cost": 0.0,
    }
    return refused


def _decompose_repo_context(goal: str,
                            candidate_files: Optional[Sequence[str]],
                            root: Optional[str] = None) -> Optional[str]:
    """Condensed signatures for the decompose prompt (HG-condense-decompose).

    ``decompose_via_llm`` / ``build_decomposition_prompt`` already accept
    ``repo_context``; this is the plan lane's producer: distill candidate
    files into signatures so the cheap decomposer never sees raw bodies.
    """
    if not candidate_files:
        return None
    files: Dict[str, str] = {}
    for rel in candidate_files:
        rel_s = str(rel)
        path = rel_s if os.path.isabs(rel_s) else os.path.join(root or os.getcwd(), rel_s)
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                files[rel_s] = handle.read()
        except OSError:
            continue
    if not files:
        return None
    brief = distill_context(files, summary=goal or "")
    return brief.to_prompt_context()


def build_consensus_prompt(plan_result: Dict[str, Any]) -> str:
    """Cheap plan-soundness prompt (HG-plan-consensus)."""
    nodes = [
        {k: n[k] for k in ("node_id", "instruction", "target_files",
                           "dependencies", "local_gate", "complexity_tier")
         if k in n}
        for n in plan_result.get("nodes", [])
        if isinstance(n, dict)
    ]
    return "\n".join([
        "You are a cheap soundness checker for an autonomous coding plan.",
        "Decide whether this DAG can actually achieve the goal. You do NOT",
        "confirm routing quality (the waist does that); you only catch",
        "structurally unsound plans before confirmation spend grows.",
        "",
        "GOAL:",
        (plan_result.get("goal") or "").strip(),
        "",
        "PLANNED DAG (JSON):",
        json.dumps({"nodes": nodes}, indent=2),
        "",
        "Respond with ONLY one JSON object:",
        '  {"sound": true, "reasons": []}',
        '  {"sound": false, "reasons": ["..."]}',
        "sound=false when the DAG cannot achieve the goal, has cycles,",
        "omits critical write steps, or targets files unrelated to the work.",
    ])


def parse_plan_consensus(response_text: str) -> Dict[str, Any]:
    """Parse the plan-consensus verdict (strict; fail-closed)."""
    data = _parse_json_object(response_text, "plan consensus")
    if "sound" not in data:
        raise HarnessError("plan consensus verdict requires 'sound'")
    reasons = data.get("reasons") or []
    if not isinstance(reasons, list):
        reasons = [reasons]
    return {"sound": bool(data["sound"]), "reasons": [str(r) for r in reasons]}


def plan_consensus(*, transport, api_key, governor, ledger, plan_result,
                   model, chat_fn=None) -> Dict[str, Any]:
    """Optional cheap soundness check that runs BEFORE the waist.

    Returns ``{sound, reasons, cost, model}``. Ledger event:
    ``plan_consensus``. Cost is accounted on the governor (when the default
    governed chat_fn runs) and reported in the envelope.
    """
    if not model:
        raise HarnessError("plan consensus requires a model")
    if chat_fn is None:
        def chat_fn(prompt):
            return governed_text(transport, api_key, governor, model, prompt,
                                 256, label="plan_consensus")
    spent_before = governor.spent if governor is not None else 0.0
    raw = chat_fn(build_consensus_prompt(plan_result))
    if isinstance(raw, tuple):
        text = raw[0]
    else:
        text = raw
    verdict = parse_plan_consensus(text)
    cost = _verdict_cost(governor, spent_before)
    verdict["cost"] = cost
    verdict["model"] = model
    if ledger is not None:
        ledger.append(
            "plan_consensus", task_id=plan_task_id(plan_result),
            sound=verdict["sound"], reasons=verdict["reasons"],
            model=model, cost=cost)
    return verdict


# Alias: compose_plan's boolean arm shares the public name ``plan_consensus``.
plan_consensus_check = plan_consensus


def plan_task(
    goal: str,
    candidate_files: Optional[Sequence[str]] = None,
    repo_context: Optional[str] = None,
    custom_frontier: Optional[str] = None,
    use_free: bool = True,
    decomposed_dag: Optional[TaskDAG] = None,
    root: Optional[str] = None,
    run_gate: Optional[str] = None,
    allow_escalation: bool = False,
    jev_route: Optional[str] = None,
) -> Dict[str, Any]:
    # Formulate a TaskDAG and classify sliding-scale tiers for each node.
    # decomposed_dag: a pre-built DAG (LLM-authored via decompose_via_llm or
    # waist-amended) replacing the heuristic decomposition; tier
    # classification and ceiling math are identical for either origin.
    # root/run_gate feed the ONE gate rule (repo_scope.gate_for_targets):
    # every node leaves this function with a verification gate derived from
    # its OWN declared target, so cli, mcp and the agent lane all dispatch
    # gated nodes instead of each lane re-deriving the rule (or omitting
    # it: an ungated write is refused at unknown trust, so the MCP lane's
    # plan_and_execute could not land anything at all).
    # jev_route: optional JEV-P3 typed route (vocabulary only) used as a
    # tier floor when keyed; unkeyed callers leave it None.
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
        gate = gate_for_targets(node.target_files, root,
                               declared=node.local_gate, run_gate=run_gate)
        route = resolve_sliding_scale_route(
            instruction=node.instruction,
            target_files=node.target_files,
            dependency_depth=depth,
            is_leaf=is_leaf,
            use_free=use_free,
            custom_frontier=custom_frontier,
            allow_escalation=allow_escalation,
            jev_route=jev_route,
        )
        total_ceiling += route.cost_ceiling
        classified_nodes[node_id] = DAGNode(
            node_id=node.node_id,
            instruction=node.instruction,
            target_files=node.target_files,
            dependencies=node.dependencies,
            local_gate=gate,
            complexity_tier=route.classification.tier,
            backend=node.backend,
        )
        node_details.append({
            "node_id": node.node_id,
            "instruction": node.instruction,
            "target_files": list(node.target_files),
            "dependencies": list(node.dependencies),
            "local_gate": gate,
            "complexity_tier": route.classification.tier,
            "recommended_model": route.classification.recommended_model,
            "cost_ceiling": route.cost_ceiling,
            "backend": node.backend,
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
    allow_escalation: Optional[bool] = None,
    attest_model: Optional[str] = None,
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
    Unknown, missing, or MALFORMED route detail degrades to today's
    defaults ({}): non-mapping detail/route, a non-sequence ladder (a
    string would otherwise become per-character "model ids"), and
    non-finite ceilings are all refused silently -- the governor's
    key-level ceiling still binds, so degradation never fails open.

    ``allow_escalation`` and ``attest_model`` are threaded verbatim when the
    caller supplies them, so a lane whose node can fail a gate carries the
    escalation arming and the verifier identity explicitly instead of
    inheriting them silently from the session defaults. ``None`` omits the
    key, leaving the engine/settings default in charge.
    """
    if explicit_model is not None:
        return {}
    detail = node_detail if isinstance(node_detail, dict) else {}
    route = detail.get("route")
    if not isinstance(route, dict):
        route = {}
    kwargs = {}
    raw_ladder = route.get("ladder")
    if isinstance(raw_ladder, (list, tuple)):
        ladder = [str(m).strip() for m in raw_ladder if str(m).strip()]
        if ladder:
            kwargs["apply_pool"] = ladder
    if explicit_task_max_cost is None:
        try:
            ceiling = float(route.get("cost_ceiling"))
        except (TypeError, ValueError):
            ceiling = 0.0
        if ceiling > 0.0 and math.isfinite(ceiling):
            kwargs["task_max_cost"] = ceiling
    if allow_escalation is not None:
        kwargs["allow_escalation"] = bool(allow_escalation)
    if attest_model:
        kwargs["attest_model"] = str(attest_model)
    # A chunked large-file node carries the engine's own backend vocabulary;
    # anything else stays out (the engine validates it again at the boundary).
    backend = detail.get("backend")
    if backend in ("harness", "morph", "diff"):
        kwargs["backend"] = backend
    return kwargs




def build_waist_prompt(
    plan_result: Dict[str, Any],
    brief_context: str = "",
    window_context: str = "",
    consensus: Optional[Dict[str, Any]] = None,
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
    if consensus is not None and not consensus.get("sound", True):
        lines.extend([
            "PLAN CONSENSUS (cheap pre-check marked this plan UNSOUND):",
            json.dumps({"sound": False,
                        "reasons": list(consensus.get("reasons") or [])}, indent=2),
            "You MUST return 'amend' with a corrected replacement DAG. Do not",
            "approve an unsound plan.",
            "",
        ])
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
        "AMENDMENT vs REFUSAL POLICY:",
        "- DO NOT REFUSE simply because the planned DAG is incomplete, lacks iteration loops,",
        "  has missing steps, or needs different granularity/ordering. If the planned DAG is",
        "  suboptimal, REPAIR IT: return 'amend' with the complete, corrected replacement DAG nodes.",
        "- 'refuse' is STRICTLY reserved for requests that are genuinely impossible, out of scope,",
        "  destructive, or malicious. Refusing an executable coding task is a failure.",
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
    length), ``{"verdict": "split", "dag": TaskDAG}`` (same shape and
    re-validation as amend; recorded distinctly so a planner that
    subdivides a node gets its own ledgered kind -- MR-4's four-way
    enum), ``{"verdict": "refuse", "reason", "evidence"}``, or
    ``{"verdict": "request_windows", "file_window_requests": [...]}``.
    """
    data = _parse_json_object(response_text, "waist plan verdict")
    verdict = str(data.get("verdict") or "").strip()
    if verdict == "approve":
        return {"verdict": "approve"}
    if verdict in ("amend", "split"):
        nodes = data.get("nodes")
        if not isinstance(nodes, list) or not nodes:
            raise HarnessError(
                f"waist {verdict} verdict requires a non-empty 'nodes' list")
        amended = TaskDAG.from_dict({"nodes": nodes})
        for node in amended.nodes.values():
            if len(node.instruction) > MAX_INSTRUCTION_CHARS:
                raise HarnessError(
                    f"amended node {node.node_id!r} instruction exceeds "
                    f"{MAX_INSTRUCTION_CHARS} chars (would fail apply validation)")
        return {"verdict": verdict, "dag": amended}
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



def plan_task_id(plan_result: Dict[str, Any]) -> str:
    """Deterministic ledger task id for a plan confirmation."""
    digest = hashlib.sha256(
        (plan_result.get("goal") or "").encode("utf-8")).hexdigest()[:12]
    return f"plan_{digest}"


def read_window(path: str, start_line=None, end_line=None) -> str:
    """Bounded source-window read for the brief (cwd-contained).

    Model-chosen paths are contained to the working tree (absolute paths
    and traversal outside the cwd are refused in-band, as an honest
    "refused" note the model can react to); missing files answer in-band
    too -- the window round-trip stays cheap and non-fatal.
    """
    raw = str(path)
    refused = "WINDOW REFUSED: only paths inside the working tree are readable"
    if os.path.isabs(raw) or ".." in raw.split(os.sep) or ".." in raw.split("/"):
        return refused
    root = os.path.realpath(os.getcwd())
    full = os.path.realpath(os.path.join(root, raw))
    if full != root and not full.startswith(root + os.sep):
        return refused
    try:
        with open(full, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return f"FILE NOT FOUND: {raw}"
    total = len(lines)
    start = max(int(start_line or 1), 1)
    if start > total:
        return f"WINDOW EMPTY: {raw} has {total} lines (requested start {start})"
    end = min(int(end_line or total), total, start + MAX_WINDOW_LINES - 1)
    body = "".join(lines[start - 1:end])
    return f"--- WINDOW: {raw} lines {start}-{end} of {total} ---\n{body}"


def _brief_for(plan_result: Dict[str, Any]) -> str:
    """Condensed signatures for the planned target files (the ONE brief)."""
    files: Dict[str, str] = {}
    for node in plan_result.get("nodes", []):
        for path in node.get("target_files", ()):
            if path in files or len(files) >= MAX_BRIEF_FILES:
                continue
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    files[path] = f.read()
            except OSError:
                continue
    if not files:
        return "(no readable target files; the plan names files that do not exist yet)"
    brief = distill_context(files, summary=plan_result.get("goal", ""))
    return brief.to_prompt_context()


def _verdict_cost(governor, spent_before: float) -> float:
    try:
        return round(governor.spent - spent_before, 6)
    except AttributeError:
        return 0.0


def confirm_plan(*, transport, api_key, governor, ledger, plan_result,
                 model, use_free=True, custom_frontier=None,
                 chat_fn=None, reader=read_window,
                 root=None, run_gate=None,
                 consensus=None,
                 max_rounds=MAX_WAIST_ROUNDS) -> Dict[str, Any]:
    """Run the waist: frontier confirms/repairs the plan; return the plan.

    Returns the (possibly amended) plan_result with a ``confirmation``
    field, or a ``status: "refused"`` envelope when the model refuses
    (callers must not execute it -- ``terminal_exit_code`` maps the
    unknown status to 2 fail-closed). Raises HarnessError when the model
    keeps requesting windows past ``max_rounds`` (the plan stays
    unconfirmed and unexecuted), on any transport/parse failure, or when
    ``model`` is unset (confirmation cannot silently skip itself).

    ``chat_fn(prompt) -> (text, cost)`` and ``reader(path, start, end) ->
    str`` are injectable seams for hermetic tests.
    """
    if not model:
        raise HarnessError(
            "waist confirmation requires a frontier model "
            "(--frontier-model or HARNESS_FRONTIER_MODEL)")
    if chat_fn is None:
        def chat_fn(prompt):
            return governed_text(transport, api_key, governor, model, prompt,
                                 WAIST_MAX_TOKENS, label="waist")
    brief = _brief_for(plan_result)
    task_id = plan_task_id(plan_result)
    window_context = ""
    spent_before = governor.spent if governor is not None else 0.0

    for round_no in range(1, max_rounds + 1):
        prompt = build_waist_prompt(plan_result, brief, window_context,
                                    consensus=consensus)
        text, _cost = chat_fn(prompt)
        try:
            verdict = parse_waist_verdict(text)
        except HarnessError:
            # Fail closed on a malformed verdict: the spend already happened,
            # the plan stays unconfirmed, and the raw body is the evidence.
            if ledger is not None:
                ledger.append("plan_verdict", task_id=task_id, verdict="unparseable",
                              model=model, round=round_no)
            raise

        kind = verdict["verdict"]
        cost = _verdict_cost(governor, spent_before)
        if kind == "approve":
            plan_result["confirmation"] = {
                "verdict": "approved", "model": model, "rounds": round_no,
                "cost": cost}
            if ledger is not None:
                ledger.append("plan_verdict", task_id=task_id, verdict="approved",
                              model=model, rounds=round_no, cost=cost)
            return plan_result
        if kind == "refuse":
            refused = dict(plan_result)
            refused["status"] = "refused"
            refused["confirmation"] = {
                "verdict": "refused", "model": model, "rounds": round_no,
                "reason": verdict["reason"], "evidence": verdict["evidence"],
                "cost": cost}
            if ledger is not None:
                ledger.append("plan_verdict", task_id=task_id, verdict="refused",
                              model=model, rounds=round_no, cost=cost,
                              reason=verdict["reason"])
            return refused
        if kind in ("amend", "split"):
            amended = plan_task(
                plan_result.get("goal", ""),
                custom_frontier=custom_frontier, use_free=use_free,
                decomposed_dag=verdict["dag"], root=root, run_gate=run_gate)
            confirmed_kind = "amended" if kind == "amend" else "split"
            amended["confirmation"] = {
                "verdict": confirmed_kind, "model": model, "rounds": round_no,
                "cost": cost}
            if ledger is not None:
                ledger.append("plan_verdict", task_id=task_id, verdict=confirmed_kind,
                              model=model, rounds=round_no, cost=cost)
            return amended

        # request_windows: attach the bounded windows and spend another round
        blocks = [reader(req["path"], req.get("start_line"), req.get("end_line"))
                  for req in verdict["file_window_requests"][:MAX_WINDOWS_PER_ROUND]]
        window_context = "\n\n".join(blocks)
        if ledger is not None:
            ledger.append("plan_verdict", task_id=task_id,
                          verdict="request_windows", model=model, round=round_no,
                          windows=len(verdict["file_window_requests"]))
    raise HarnessError(
        f"waist requested file windows beyond the {max_rounds}-round budget; "
        "plan stays unconfirmed and will not execute")


def compose_plan(*, transport, api_key, governor, ledger, opts_goal,
                 candidate_files=None, frontier_model=None, use_free=True,
                 decompose_llm=False, confirm=False, decompose_model=None,
                 chat_fn=None, max_cost=None, keep_going=False, out=None,
                 execute=False, root=None, max_tokens=None,
                 allow_escalation: bool = False,
                 plan_consensus: bool = False,
                 jev_policy=None,
                 issue_sort_pack=None) -> Dict[str, Any]:
    """ONE owner of the plan-lane flow (CLI and MCP call this).

    Order: optional cheap-LLM decomposition (M1, condensed signatures) ->
    tier classification -> single-pass chunking -> optional cheap plan
    consensus -> optional waist confirmation (M2) over the plan that will
    actually run -> composed pyramid ceiling check before execute spend.
    Decomposition failures retry once, then fall back loudly to the
    heuristic in both preview and execute modes (with an orchestration
    event and decomposition='heuristic:llm_failed').

    Fail-closed rules (hourglass composition):
    * confirm=True and EVERY waist rung unreachable on execute -> REFUSE
      (never proceed under the local gate silently).
    * composed_worst_case exceeds governor.remaining() -> REFUSE before
      execute dispatch (spend stays 0 on that path when planning itself
      was injected/hermetic).

    ``root`` is the tree the plan edits (defaults to the process CWD, which
    is what the CLI/MCP lanes edit); ``max_tokens`` is the lane's pinned
    output budget when it has one -- both feed the chunk policy's real
    per-pass budget, none of them add a budget of their own.

    ``issue_sort_pack`` (optional operator bucket pack): when provided with
    a ``jev_policy``, attach the issue-sort combo on the plan envelope as
    ``issue_sort`` via the ONE policy owner (path_id from pack only).
    """
    if (decompose_llm or confirm or plan_consensus) and governor is None:
        raise HarnessError("LLM plan features require a governor")

    plan_goal = opts_goal
    plan_structural = None
    plan_triage = None
    plan_issue_sort = None
    jev_route_feed = None
    repo_context = None
    if isinstance(jev_policy, JevPolicy):
        # JEV-P3-route: one typed route choice through the policy owner.
        route_eval, route_envelope = jev_policy.evaluate_route(
            opts_goal, candidate_files, site="route")
        plan_eval, plan_structural = jev_policy.evaluate_plan(
            opts_goal, candidate_files, site="waist")
        plan_triage = dict(route_envelope)
        plan_triage["route"] = route_eval.answers.get("route", "free-distill")
        plan_triage["route_is_fallback"] = bool(route_eval.is_fallback)
        plan_triage["requires_iteration"] = bool(
            plan_eval.answers.get("requires_iteration")
            or route_eval.answers.get("requires_iteration", False))
        if not route_eval.is_fallback:
            jev_route_feed = plan_triage["route"]
        if plan_eval.answers.get("requires_iteration") or plan_triage["requires_iteration"]:
            plan_goal = (
                f"{opts_goal}\n\n[STRUCTURAL GUIDELINE]: This goal requires iterative "
                "control flow, conditional branching, or multi-step execution. "
                "Represent those dependencies explicitly in the executable DAG.")
        if issue_sort_pack is not None:
            _sort_result, _sort_structural, plan_issue_sort = (
                jev_policy.evaluate_issue_sort(
                    {"issue": opts_goal}, issue_sort_pack, site="issue_sort"))
    # JEV-P3-context-pack: distilled decision-relevant state before generative
    # seats that lack a pack. Smallest seam — pass into LLM decompose.
    if decompose_llm and not repo_context:
        repo_context = build_context_pack(
            opts_goal, candidate_files=candidate_files)

    # The run-level gate: the goal's own candidate files. It is the
    # last-resort arm of the ONE gate rule (repo_scope.gate_for_targets),
    # so a node whose own target yields no gate still inherits the run's
    # verification instead of dispatching an unverifiable write.
    run_gate = discover_verification_gate(list(candidate_files or []), root)

    decomposed = None
    decomposition = "heuristic"
    if decompose_llm:
        if chat_fn is None:
            if not decompose_model:
                scout = resolve_scout_ladder(use_free=use_free,
                                             custom_frontier=frontier_model)
                decompose_model = scout[0]
            def chat_fn(prompt):
                return governed_text(transport, api_key, governor, decompose_model,
                                     prompt, DECOMPOSE_MAX_TOKENS, label="decompose")
        # DF-HG-3: one strict retry on LLM decomposition failure, then loud heuristic fallback in preview too
        last_exc = None
        for attempt in (1, 2):
            try:
                # chat_fn's contract is (text, cost) -- decomposition consumes
                # the text only; the cost stays on the governor/caller side.
                # HG-condense-decompose + JEV-P3-context-pack: signatures AND
                # decision pack; never raw file bodies; prompt still labels
                # REPOSITORY CONTEXT: via build_decomposition_prompt.
                sig_ctx = _decompose_repo_context(
                    plan_goal, candidate_files, root=root)
                decision_ctx = build_context_pack(
                    opts_goal, candidate_files=candidate_files)
                if sig_ctx and str(sig_ctx).strip():
                    repo_context = decision_ctx + "\n\n" + str(sig_ctx).strip()
                else:
                    repo_context = decision_ctx
                decomposed = decompose_via_llm(lambda p: chat_fn(p)[0], plan_goal,
                                               candidate_files=candidate_files,
                                               repo_context=repo_context)
                decomposition = (f"llm:{decompose_model}"
                                 if decompose_model else "llm:injected")
                last_exc = None
                break
            except HarnessError as exc:
                last_exc = exc
                if attempt == 1:
                    eprint(f"[plan] LLM decomposition attempt 1 failed ({exc}); retrying")
        if last_exc is not None:
            from . import events as _events
            _events.emit(
                "orchestration_note",
                note=f"LLM decomposition failed ({last_exc}); loud heuristic fallback")
            eprint(f"[plan] LLM decomposition failed ({last_exc}); loud heuristic fallback")
            decomposition = "heuristic"
            decomposed = None

    plan_result = plan_task(
        goal=plan_goal, candidate_files=candidate_files,
        custom_frontier=frontier_model, use_free=use_free,
        decomposed_dag=decomposed, root=root, run_gate=run_gate,
        allow_escalation=allow_escalation, jev_route=jev_route_feed)
    plan_result["decomposition"] = decomposition
    if plan_triage is not None:
        plan_result["triage"] = plan_triage
    if plan_structural is not None:
        plan_result["structural"] = plan_structural
    if plan_issue_sort is not None:
        plan_result["issue_sort"] = plan_issue_sort
    # Chunk anything that cannot fit ONE model pass before the gate sees it,
    # so the waist confirms the plan that will actually run.
    plan_result = _fit_plan_to_single_pass(
        plan_result, goal=opts_goal, candidate_files=candidate_files,
        custom_frontier=frontier_model, use_free=use_free, root=root,
        run_gate=run_gate, max_tokens=max_tokens,
        allow_escalation=allow_escalation)
    if plan_structural is not None:
        plan_result["structural"] = plan_structural
    if plan_issue_sort is not None:
        plan_result["issue_sort"] = plan_issue_sort

    # HG-plan-consensus: optional cheap soundness check BEFORE the waist.
    # It always uses its OWN governed call (or an injected consensus seam),
    # never the decompose chat_fn -- those are different contracts.
    consensus = None
    if plan_consensus and confirm:
        consensus_model = decompose_model
        if not consensus_model:
            try:
                consensus_model = resolve_scout_ladder(
                    use_free=use_free, custom_frontier=frontier_model)[0]
            except Exception:
                consensus_model = frontier_model
        try:
            # chat_fn=None -> plan_consensus builds the governed_text call.
            consensus = plan_consensus_check(
                transport=transport, api_key=api_key, governor=governor,
                ledger=ledger, plan_result=plan_result,
                model=consensus_model, chat_fn=None)
        except HarnessError as exc:
            # Fail closed on unparseable consensus when the operator armed it.
            if not execute:
                raise
            eprint(f"[plan] plan consensus failed ({exc}); continuing to waist")
            consensus = {"sound": True, "reasons": [f"consensus_unavailable: {exc}"],
                         "cost": 0.0, "model": consensus_model}
        if consensus is not None:
            plan_result["consensus"] = consensus

    if confirm:
        ladder = resolve_waist_ladder(
            use_free=use_free, custom_frontier=frontier_model,
            allow_escalation=allow_escalation)
        plan_result_confirmed = None
        last_exc = None
        for rung_idx, candidate_model in enumerate(ladder):
            try:
                res = confirm_plan(
                    transport=transport, api_key=api_key, governor=governor,
                    ledger=ledger, plan_result=plan_result, model=candidate_model,
                    use_free=use_free, custom_frontier=frontier_model,
                    root=root, run_gate=run_gate, chat_fn=None,
                    consensus=consensus)
                plan_result_confirmed = res
                break
            except HarnessError as exc:
                last_exc = exc
                from . import events as _events
                _events.emit("rotation", model=candidate_model, reason=str(exc),
                             note=f"waist confirmation failed on rung {rung_idx + 1}/{len(ladder)}")
                continue

        if plan_result_confirmed is not None:
            plan_result = plan_result_confirmed
            if plan_structural is not None:
                plan_result["structural"] = plan_structural
            if consensus is not None:
                plan_result["consensus"] = consensus
            if plan_issue_sort is not None:
                plan_result["issue_sort"] = plan_issue_sort
            # When in autonomous execution mode and the waist refused,
            # do not immediately halt. Attempt critique-driven re-planning if decomposition
            # was LLM-based, feeding the frontier's architectural critique back to the planner.
            if plan_result.get("status") == "refused" and execute and decompose_llm:
                refusal_reason = plan_result.get("confirmation", {}).get("reason", "")
                from . import events as _events
                _events.emit("orchestration_note",
                             note=f"Waist refused plan ('{refusal_reason}'); re-planning with critique")
                critique_prompt = (
                    f"{opts_goal}\n\n"
                    f"[ARCHITECTURAL REVIEW CRITIQUE]: The previous plan was rejected: "
                    f"'{refusal_reason}'. "
                    f"Address this critique directly: ensure all iteration loops, conditional branching, "
                    f"dependencies, and granular steps are properly structured into the DAG."
                )
                try:
                    critique_context = _decompose_repo_context(
                        critique_prompt, candidate_files, root=root)
                    re_decomposed = decompose_via_llm(
                        lambda p: chat_fn(p)[0], critique_prompt,
                        candidate_files=candidate_files,
                        repo_context=critique_context)
                    re_plan = plan_task(
                        goal=plan_goal, candidate_files=candidate_files,
                        custom_frontier=frontier_model, use_free=use_free,
                        decomposed_dag=re_decomposed, root=root, run_gate=run_gate,
                        allow_escalation=allow_escalation)
                    re_plan["decomposition"] = decomposition + ":critique_replan"
                    re_plan = _fit_plan_to_single_pass(
                        re_plan, goal=opts_goal, candidate_files=candidate_files,
                        custom_frontier=frontier_model, use_free=use_free, root=root,
                        run_gate=run_gate, max_tokens=max_tokens,
                        allow_escalation=allow_escalation)
                    for candidate_model in ladder:
                        try:
                            re_res = confirm_plan(
                                transport=transport, api_key=api_key, governor=governor,
                                ledger=ledger, plan_result=re_plan, model=candidate_model,
                                use_free=use_free, custom_frontier=frontier_model,
                                root=root, run_gate=run_gate, chat_fn=None,
                                consensus=consensus)
                            if re_res.get("status") != "refused":
                                plan_result = re_res
                                break
                        except HarnessError:
                            continue
                except HarnessError as exc:
                    eprint(f"[plan] Critique re-planning failed ({exc}); retaining initial result")

            if plan_result.get("status") != "refused":
                # An amend/split verdict replaces the node set: re-fit it, or the
                # gate's own repair could hand execution an oversized node.
                plan_result = _fit_plan_to_single_pass(
                    plan_result, goal=opts_goal, candidate_files=candidate_files,
                    custom_frontier=frontier_model, use_free=use_free, root=root,
                    run_gate=run_gate, max_tokens=max_tokens,
                    allow_escalation=allow_escalation)
        else:
            # HG: Confirm-armed waist UNREACHABLE across the full ladder.
            # Operator ruling (2026-09-22): an unreachable seat is an
            # AVAILABILITY failure, not a policy refusal -- the only two
            # things allowed to stop a run are the monetary cap and required
            # user input. Degrade to executing under the local structural
            # gate (which plan_task already ran) with explicit provenance:
            # every downstream envelope records that NO model confirmed this
            # plan. Plan-only mode (execute=False) still raises: there is no
            # execution to protect there, and the caller asked a question
            # whose honest answer is "the gate could not run".
            first_model = ladder[0] if ladder else (
                frontier_model or resolve_frontier_model(None, use_free=use_free))
            message = (
                f"waist confirmation could not run on {first_model} "
                f"(plan NOT executed): {last_exc}")
            if not execute:
                raise HarnessError(message) from last_exc
            from . import events as _events
            _events.emit(
                "orchestration_note",
                note=(f"Waist confirmation unreachable across ladder ({last_exc}); "
                      "degrading to local-gate execution per operator "
                      "no-interruptions ruling"))
            eprint(f"[waist] Confirmation unreachable across ladder ({last_exc}); "
                   f"degrading to local-gate execution (verdict=unavailable)")
            degraded = dict(plan_result)
            degraded["status"] = "planned"
            degraded["confirmation"] = {
                "verdict": "unavailable",
                "model": first_model,
                "rounds": 0,
                "reason": "waist confirmation unreachable across the full ladder; "
                          "executing under local structural gate only",
                "evidence": str(last_exc) if last_exc is not None else "",
                "cost": 0.0,
            }
            plan_result = degraded

    # HG-composed-ceiling: ALWAYS compute the pyramid envelope; refuse execute
    # when the composed worst case cannot fit the governor's remaining budget.
    composed = composed_worst_case(
        plan_result,
        governor=governor,
        decompose_llm=decompose_llm,
        confirm=confirm,
        decompose_model=decompose_model,
        frontier_model=frontier_model,
        use_free=use_free,
        allow_escalation=allow_escalation,
        plan_consensus=plan_consensus,
    )
    plan_result["composed_worst_case"] = composed
    if (execute and governor is not None
            and plan_result.get("status") != "refused"
            and composed.get("exceeds_remaining")):
        from . import events as _events
        _events.emit(
            "orchestration_note",
            note=("composed pyramid ceiling exceeds remaining budget; "
                  "refusing before execute spend"))
        plan_result = _refuse_composed_ceiling(plan_result, composed)
    return plan_result


def _fit_plan_to_single_pass(plan_result: Dict[str, Any], *, goal,
                             candidate_files, custom_frontier, use_free,
                             root, run_gate, max_tokens,
                             allow_escalation: bool = False) -> Dict[str, Any]:
    """Apply the chunk policy to a plan result, re-planning when it split.

    The chunk policy itself lives in :func:`chunk_oversized_nodes` (ONE
    owner); this wrapper only re-derives the tier/ceiling details through
    ``plan_task`` when the DAG changed, so node details keep exactly one
    producer. Returns the plan unchanged when nothing was oversized.
    """
    if plan_result.get("status") == "refused":
        return plan_result
    dag = TaskDAG.from_dict(plan_result["dag"])
    fitted = chunk_oversized_nodes(dag, root=root, max_tokens=max_tokens,
                                   source_tokens=rung_read_budgets(plan_result))
    if fitted is dag:
        return plan_result
    rebuilt = plan_task(goal=goal, candidate_files=candidate_files,
                        custom_frontier=custom_frontier, use_free=use_free,
                        decomposed_dag=fitted, root=root, run_gate=run_gate,
                        allow_escalation=allow_escalation)
    rebuilt["decomposition"] = plan_result.get("decomposition", "heuristic")
    if len(fitted.nodes) != len(dag.nodes):
        # Visible evidence that the planner chunked: a reader can see how
        # many nodes the goal originally needed versus how many passes it
        # was split into, instead of wondering why the node count grew.
        rebuilt["chunking"] = {
            "required": True,
            "unchunked_nodes": plan_result.get("total_nodes", 0)}
    return rebuilt


def resolve_scout_ladder(use_free=True, custom_frontier=None):
    """The tier-0 ladder from the same sliding-scale policy that classifies
    nodes -- the decomposition model is simply the cheapest rung, with no
    new routing policy of its own."""
    from .sliding_scale import resolve_sliding_scale_route
    route = resolve_sliding_scale_route(
        instruction="decompose the goal into subtasks",
        target_files=(), dependency_depth=0, is_leaf=True,
        use_free=use_free, custom_frontier=custom_frontier)
    return list(route.ladder)


def resolve_waist_ladder(use_free=True, custom_frontier=None, allow_escalation=False):
    """The model ladder for waist plan confirmation (cheapest / free first).

    - If custom_frontier is specified, it is placed at the head of the ladder.
    - If use_free is True:
        - Primary free judge (FREE_JUDGE, e.g. google/gemma-4-31b-it:free)
        - Free alternatives from ESCALATION_POOL_FREE
        - If allow_escalation is True:
            - Paid frontier model: resolve_frontier_model(custom_frontier, use_free=False)
            - Paid escalation models from ESCALATION_POOL_PAID
    - If use_free is False:
        - Paid frontier model, then ESCALATION_POOL_PAID.
    """
    out = []
    if custom_frontier:
        resolved = resolve_frontier_model(custom_frontier, use_free=use_free)
        if resolved and resolved not in out:
            out.append(resolved)
    if use_free:
        for m in [FREE_JUDGE] + list(ESCALATION_POOL_FREE):
            if m not in out:
                out.append(m)
        if allow_escalation:
            frontier_paid = resolve_frontier_model(custom_frontier, use_free=False)
            if frontier_paid not in out:
                out.append(frontier_paid)
            for m in ESCALATION_POOL_PAID:
                if m not in out:
                    out.append(m)
    else:
        frontier_paid = resolve_frontier_model(custom_frontier, use_free=False)
        if frontier_paid not in out:
            out.append(frontier_paid)
        for m in ESCALATION_POOL_PAID:
            if m not in out:
                out.append(m)
    return out


def resolve_planner_ladder(use_free=True, custom_frontier=None, allow_paid=False):
    """The model ladder for task decomposition and complex planning."""
    return resolve_waist_ladder(use_free=use_free, custom_frontier=custom_frontier, allow_escalation=allow_paid)

