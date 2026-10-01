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
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .brief import (
    MAX_TOTAL_WINDOW_CHARS,
    build_brief,
    estimate_brief_tokens,
    freshness_report,
    render_brief,
    validate_brief,
)
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
from .osal import read_text
from .token_budget import TokenBudget, budget_from_settings
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
                            root: Optional[str] = None,
                            ledger=None, task_id: Optional[str] = None,
                            ) -> Optional[str]:
    """Condensed signatures for the decompose prompt (HG-condense-decompose).

    ``decompose_via_llm`` / ``build_decomposition_prompt`` already accept
    ``repo_context``; this is the plan lane's producer: distill candidate
    files into signatures so the cheap decomposer never sees raw bodies.
    When a ledger is supplied the condensation is recorded as HV-2
    evidence (``brief_built``, site=hourglass) like every other lane.
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
    if ledger is not None:
        ledger.append("brief_built", task_id=task_id, site="hourglass",
                      schema=2, **brief.ledger_fields())
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
                 issue_sort_pack=None,
                 allow_heuristic_preview: bool = False,
                 token_budget=None,
                 stages: Optional[Sequence[str]] = None,
                 supplied_brief: bool = False,
                 supplied_plan: bool = False,
                 brief: Optional[Dict[str, Any]] = None,
                 brief_tokens: Optional[int] = None,
                 reader=None) -> Dict[str, Any]:
    """ONE owner of the plan-lane flow (CLI and MCP call this).

    Order: optional cheap-LLM decomposition (M1, condensed signatures) ->
    tier classification -> single-pass chunking -> optional cheap plan
    consensus -> optional waist confirmation (M2) over the plan that will
    actually run -> composed pyramid ceiling check before execute spend.

    Decomposition failures retry once, then:
    * ``execute=True`` -- always fall back loudly to the heuristic
      (decomposition='heuristic'; an orchestration event + stderr note).
      The run spends anyway, so a loud fallback keeps it going.
    * ``execute=False`` (plan-only preview) -- FAILS CLOSED by default: the
      operator asked for LLM planning, and silently handing back the
      heuristic plan would be dishonest (raises HarnessError, non-zero
      exit). Passing ``allow_heuristic_preview=True`` (CLI:
      ``--allow-heuristic-preview``) opts into the same loud heuristic
      fallback as execute mode instead -- decomposition='heuristic', a
      loud stderr note, and the waist confirmation step (``confirm``) is
      skipped rather than confirming a plan the operator never got the
      LLM decomposition they asked for: the envelope's ``confirmation``
      is explicitly ``verdict: "skipped"``, never ``"approved"``.

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

    ``token_budget`` + ``stages`` (HV-4): when the caller supplies the run's
    ``TokenBudget``, the plan lane composes its stages against it and records
    the decision on the envelope as ``composition`` -- which stages ran, what
    each was allowed to spend, and which were bypassed and why. With no
    budget the lane behaves exactly as before (no composition key), because
    composition may not invent an allowance. ``brief_tokens`` is the measured
    size of a brief the caller already curated; supplying it preflights the
    later stages against the evidence they will actually read.

    ``brief`` (HV-2-use) is that brief itself: the ``context`` stage's
    artifact, handed in by the run's caller as its intake evidence. Passing
    the pack rather than only the ``supplied_brief`` flag is what lets the
    ``context`` bypass be decided on real evidence -- the pack's own
    freshness, and the re-condense trigger whenever the caller also reports
    stage evidence -- instead of on a boolean the caller asserts. It is also
    reported as ``completed``, not ``skipped``: composition budgets stages,
    it does not perform them, so a caller that actually produced the
    artifact is the one that can say the stage is done.
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
    last_exc = None
    # DF-HG-3b: set only when a plan-only preview degraded to the heuristic
    # after an opted-in LLM decomposition failure -- the waist confirmation
    # step is then skipped rather than confirming (and reporting as
    # approved) a plan the operator never got the LLM decomposition for.
    _degraded_preview_heuristic = False
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
                    plan_goal, candidate_files, root=root, ledger=ledger)
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
            # DF-HG-3b (Board ruling on PR #68): a plan-only preview stays
            # FAIL-CLOSED by default -- the operator asked for LLM planning,
            # and silently handing back the heuristic plan would misreport
            # what actually ran. --execute always falls back loudly (the
            # run spends anyway); a preview only degrades when the operator
            # opted in with allow_heuristic_preview.
            if not execute and not allow_heuristic_preview:
                raise HarnessError(
                    f"LLM decomposition failed twice ({last_exc}); refusing "
                    "to silently degrade a plan-only preview to the "
                    "heuristic decomposition. Pass --allow-heuristic-preview "
                    "to opt into a loud heuristic fallback, or rerun with "
                    "--execute.") from last_exc
            from . import events as _events
            _events.emit(
                "orchestration_note",
                note=f"LLM decomposition failed ({last_exc}); loud heuristic fallback")
            eprint(f"[plan] LLM decomposition failed ({last_exc}); loud heuristic fallback")
            decomposition = "heuristic"
            decomposed = None
            if not execute:
                _degraded_preview_heuristic = True

    plan_result = plan_task(
        goal=plan_goal, candidate_files=candidate_files,
        custom_frontier=frontier_model, use_free=use_free,
        decomposed_dag=decomposed, root=root, run_gate=run_gate,
        allow_escalation=allow_escalation, jev_route=jev_route_feed)
    plan_result["decomposition"] = decomposition
    if last_exc is not None:
        plan_result["degraded"] = True
        plan_result["degrade_reason"] = "decomposition_unavailable"
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

    if confirm and _degraded_preview_heuristic:
        # DF-HG-3b: the operator opted into a heuristic-degraded preview,
        # which never ran the LLM decomposition the waist would confirm.
        # Report the skip explicitly -- never "approved" for a plan that
        # was not actually reviewed against the requested decomposition.
        plan_result["confirmation"] = {
            "verdict": "skipped",
            "model": None,
            "rounds": 0,
            "reason": "plan-only preview degraded to heuristic decomposition "
                      "via --allow-heuristic-preview; waist confirmation "
                      "over an LLM decomposition never happened, so it is "
                      "skipped rather than confirming an unreviewed plan.",
            "cost": 0.0,
        }
    elif confirm:
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
                        critique_prompt, candidate_files, root=root,
                        ledger=ledger)
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
            degraded["degraded"] = True
            degraded["degrade_reason"] = "waist_unavailable"
            resume_hash = hashlib.sha256(
                f"{opts_goal}:{plan_result.get('total_nodes', 1)}".encode("utf-8")
            ).hexdigest()[:16]
            resume_token = f"waist-resume-{resume_hash}"
            degraded["resume_token"] = resume_token
            degraded["confirmation"] = {
                "verdict": "unavailable",
                "model": first_model,
                "rounds": 0,
                "reason": "waist confirmation unreachable across the full ladder; "
                          "executing under local structural gate only",
                "evidence": str(last_exc) if last_exc is not None else "",
                "cost": 0.0,
                "degraded": True,
                "degrade_reason": "waist_unavailable",
                "resume_token": resume_token,
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
    if token_budget is not None:
        # HV-4: the composition decision is evidence, so it rides on the
        # envelope rather than living only in the caller's head. Attached
        # last, so it survives the degraded/refused envelope copies above.
        composition = compose_stages(
            budget=token_budget, declared=stages,
            supplied_brief=supplied_brief, supplied_plan=supplied_plan,
            brief=brief, brief_tokens=brief_tokens, reader=reader)
        states = stage_states(composition)
        planning_stage = next(
            (entry for entry in composition["stages"]
             if entry["stage"] == STAGE_PLANNING), None)
        if planning_stage is not None and plan_result.get("status") != "refused":
            # HV-5: composition selects and budgets the planning stage; the
            # waist remains its sole production owner. Give it the composed
            # child allowance and the same intake artifact/reader the lane
            # used to make the composition decision.
            planning = run_planning(
                goal=opts_goal,
                budget=token_budget,
                stage_budget=planning_stage["budget"],
                files=candidate_files or (), reader=reader, brief=brief,
                jev_policy=jev_policy)
            planning_payload = planning.to_dict()
            plan_result["planning"] = planning_payload
            if isinstance(planning_payload, dict) and planning_payload.get("jev_signals"):
                if "stage_judgments" not in plan_result:
                    plan_result["stage_judgments"] = {}
                plan_result["stage_judgments"]["planning"] = planning_payload["jev_signals"]
            states[STAGE_PLANNING] = STATE_COMPLETED
            # Only sufficiency authorizes the already-composed outer DAG.
            # A proposed planning DAG has no adapter into that outer plan,
            # and an evidence request/defer explicitly says the available
            # evidence cannot support execution. Fail closed while preserving
            # the full typed result for the caller.
            kind = (planning_payload.get("kind")
                    if isinstance(planning_payload, dict) else None)
            if kind != OUTCOME_SUFFICIENT:
                if kind == OUTCOME_DEFER:
                    reason = (planning_payload.get("reason")
                              or "planning deferred execution")
                elif kind == OUTCOME_EVIDENCE_REQUEST:
                    reason = (planning_payload.get("reason")
                              or "planning requires additional evidence")
                elif kind == OUTCOME_PLAN:
                    reason = (
                        "planning produced a separate plan without an adapter "
                        "to the composed execution DAG")
                else:
                    reason = "planning returned an unsupported outcome"
                plan_result["status"] = "refused"
                plan_result["confirmation"] = {
                    "verdict": "refused",
                    "model": "planning-outcome",
                    "rounds": 0,
                    "reason": reason,
                    "cost": 0.0,
                }
        context_stage = next(
            (entry for entry in composition["stages"]
             if entry["stage"] == STAGE_CONTEXT), None)
        if context_stage is not None and plan_result.get("status") != "refused":
            if jev_policy is not None and hasattr(jev_policy, "evaluate_hourglass_stage"):
                if "stage_judgments" not in plan_result:
                    plan_result["stage_judgments"] = {}
                if "context" not in plan_result["stage_judgments"]:
                    state = {
                        "request": opts_goal,
                        "goal": opts_goal,
                        "files": list(candidate_files or []),
                    }
                    if brief is not None:
                        state["brief_tokens"] = brief_tokens or brief.get("estimated_tokens")
                    try:
                        eval_res = jev_policy.evaluate_hourglass_stage(
                            "context_intake", state, site="hourglass-intake")
                        if isinstance(eval_res, tuple) and len(eval_res) >= 2:
                            _res, structural = eval_res[0], eval_res[1]
                        else:
                            _res, structural = None, getattr(eval_res, "structural", {}) or {}
                        from .jev_packs import HOURGLASS_STAGE_DIMENSIONS
                        declared = HOURGLASS_STAGE_DIMENSIONS["context_intake"]["signals"]
                        signals = {name: (structural or {}).get(name) for name in declared
                                   if (structural or {}).get(name) is not None}
                        plan_result["stage_judgments"]["context"] = {
                            "dimension": "context_intake",
                            "signals": signals,
                            "native": bool((structural or {}).get("native")),
                        }
                    except (HarnessError, TypeError, ValueError):
                        plan_result["stage_judgments"]["context"] = {
                            "dimension": "context_intake", "signals": {}, "native": False
                        }
        if brief is not None and STAGE_CONTEXT in (composition.get("bypassed")
                                                  or {}):
            # HV-2-use: the brief IS the context stage's artifact, and the
            # caller produced it, so a granted bypass means that stage is
            # **done** rather than *skipped*. Only a granted bypass: when the
            # pack is stale the bypass is denied, the stage genuinely has to
            # run, and its own `pending` is the honest report.
            states[STAGE_CONTEXT] = STATE_COMPLETED
        plan_result["composition"] = composition_envelope(composition,
                                                          states=states)
        plan_result["stage_states"] = states
        plan_result["token_budget"] = token_budget.snapshot()
    if brief is not None:
        plan_result["brief"] = brief
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


# ---- HV-4: stage composition and the planning waist contract -------------
#
# Composition is a *selection* concern, not a reimplementation: the brief
# schema stays with ``brief``, the token arithmetic with ``token_budget``,
# plan validation with ``dag``, and Jev's dimensions with ``jev_policy``.
# This section only decides WHICH stages run, what each one may spend, and
# what the planning waist is allowed to emit.
#
# This module is the ONE owner of composition. The canon's ``HV-4`` row
# names ``harness/waist.py`` as the composition owner, so the alternative
# single-purpose ``harness/stages.py`` was dissolved into this section
# rather than left beside it: two owners of ``resolve_stages`` would be
# exactly the second implementation the row forbids.

STAGE_CONTEXT = "context"
STAGE_PLANNING = "planning"
STAGE_EXECUTION = "execution"
STAGE_VERIFICATION = "verification"
STAGE_ORDER = (STAGE_CONTEXT, STAGE_PLANNING,
               STAGE_EXECUTION, STAGE_VERIFICATION)
# Hourglass compatibility: with nothing declared, every stage runs, exactly
# as the lanes behaved before composition was selectable.
HOURGLASS_DEFAULT_STAGES = STAGE_ORDER

# A supplied artifact means its stage already happened upstream. Composition
# records the bypass instead of silently re-running or silently dropping it.
STAGE_BYPASS_REASONS = {
    STAGE_CONTEXT: "a brief was supplied; intake already ran",
    STAGE_PLANNING: "a plan was supplied; decomposition already ran",
}

# A supplied brief that no longer matches its pins buys no bypass. Planning on
# evidence that drifted from what it cited is exactly what the brief's
# freshness contract exists to prevent, so the context stage runs, rebuilds,
# and the caller can see that its supply was refused.
STAGE_DENIED_BYPASS = {
    STAGE_CONTEXT: "supplied brief is not fresh; intake runs again",
}

# Per-stage states a composition may report. A stage that is selected and
# silently absent is the one thing a composition must never be, so every
# declared stage is always visible in exactly one of these states.
STATE_COMPLETED = "completed"
STATE_SKIPPED = "skipped"
STATE_PENDING = "pending"
STAGE_STATES = (STATE_COMPLETED, STATE_SKIPPED, STATE_PENDING)

# Successive stages plan over less than the one before: the brief is curated
# down as it is consumed, so a later stage's ceiling is the earlier stage's
# smaller share. This is a *narrowing* factor applied on top of the run
# budget, never a budget of its own.
STAGE_INPUT_DECAY = 0.5
STAGE_OUTPUT_DECAY = 0.5
MIN_STAGE_INPUT_TOKENS = 1024
MIN_STAGE_OUTPUT_TOKENS = 256

# The planning ladder narrows by this share per round. Below 1.0 by
# construction: the ladder narrows, it never widens.
PLANNING_SHRINK = 0.5

# A round below this many input tokens cannot ask a model anything useful, so
# the ladder stops before it spends a reservation on a truncated request.
MIN_ROUND_TOKENS = 1024

# The planning waist may stop, plan, ask, or defer -- and nothing else.
OUTCOME_SUFFICIENT = "sufficient"
OUTCOME_PLAN = "plan"
OUTCOME_EVIDENCE_REQUEST = "evidence_request"
OUTCOME_DEFER = "defer"
PLAN_OUTCOMES = (OUTCOME_SUFFICIENT, OUTCOME_PLAN,
                 OUTCOME_EVIDENCE_REQUEST, OUTCOME_DEFER)

MAX_EVIDENCE_QUESTIONS = 5
MAX_EVIDENCE_QUESTION_CHARS = 400
MAX_EVIDENCE_REQUEST_CHARS = MAX_EVIDENCE_QUESTIONS * MAX_EVIDENCE_QUESTION_CHARS
MAX_DEFER_REASON_CHARS = 400

#: A plan is accepted only if it validates AND stays inside this bound. The
#: number is declared here, not negotiated in a prompt.
DEFAULT_MAX_PLAN_NODES = 12

#: Code-owned stopping rule, not a model judgment. Jev answers "is more
#: evidence required?"; code decides what counts as yes. A request is taken
#: at or above this confidence and ignored below it, so a borderline call
#: ends the ladder instead of buying another round of curation.
SUFFICIENCY_NOUL_THRESHOLD = 0.5

#: How narrow the window search may get, in characters. A brief's size is
#: monotone in its window budget, so a bounded bisection finds the largest
#: window that fits; the cap keeps the rebuild count finite and cheap (every
#: step is a hermetic re-read, never a model call).
_WINDOW_SEARCH_FLOOR = 256


def stage_selection_from_settings(settings, *, default=None) -> List[str]:
    """The configured stage selection (``HARNESS_HOURGLASS_STAGES``).

    The default keeps every stage selected: the hourglass is the product's
    default posture, and compatibility requires it to stay on. Validation of
    the names themselves belongs to :func:`resolve_stages` -- config only
    decides what an operator asks for, so an unknown name is refused where
    the ladder is resolved rather than silently dropped here.
    """
    raw = getattr(settings, "hourglass_stages", None)
    if raw in (None, "", []):
        raw = default
    if raw in (None, "", []):
        return list(HOURGLASS_DEFAULT_STAGES)
    if isinstance(raw, str):
        return [part.strip() for part in raw.split(",") if part.strip()]
    return list(raw)


def tree_reader(root):
    """Read a run's files relative to the tree that run edits.

    An agent lane's candidate files are relative to ITS root, not to the
    process CWD, so building the intake brief with the default reader would
    either miss the file or -- worse -- silently read a same-named file from
    wherever the process happens to be. Reading goes through ``osal`` (the
    ONE owner of evidence bytes) so the sha256 a pack pins matches the bytes
    on disk on every platform.

    An absolute path is left alone: a caller that names one means it.
    """
    base = str(root)

    def read(path):
        name = path if os.path.isabs(str(path)) else os.path.join(base,
                                                                str(path))
        return read_text(name)

    return read
def compose_arguments(settings, *, goal, files, root=None,
                      reader=None, stages=None, brief=None,
                      token_budget=None,
                      max_input_tokens: Optional[int] = None,
                      max_output_tokens: Optional[int] = None) -> Dict[str, Any]:
    """Everything ``compose_plan`` needs to compose one run (HV-2-use / HV-6).

    ONE owner for *how a lane composes*, so the CLI, MCP and agent lanes
    cannot drift apart on what a composed run is:

    * the allowance from ``budget_from_settings`` or explicit overrides;
    * the stage subset from ``stage_selection_from_settings`` or caller overrides;
    * when supplied or ``context`` is selected, the intake brief pack.
    """
    if stages is not None:
        if isinstance(stages, str):
            stage_candidates = [s.strip() for s in stages.split(",") if s.strip()]
        else:
            stage_candidates = list(stages)
        resolved = resolve_stages(stage_candidates)
        stages_subset = list(resolved["stages"])
    else:
        stages_subset = stage_selection_from_settings(settings)

    if reader is None and root is not None:
        reader = tree_reader(root)

    if token_budget is not None:
        tb = token_budget
    elif settings is not None:
        tb = budget_from_settings(settings, max_input_tokens=max_input_tokens,
                                 max_output_tokens=max_output_tokens)
    else:
        tb = None

    arguments: Dict[str, Any] = {
        "token_budget": tb,
        "stages": stages_subset,
        "reader": reader,
    }

    brief_pack = None
    if brief is not None:
        if isinstance(brief, str):
            if os.path.isfile(brief):
                with open(brief, encoding="utf-8") as f:
                    brief_pack = json.load(f)
            else:
                try:
                    brief_pack = json.loads(brief)
                except Exception:
                    brief_pack = None
        elif isinstance(brief, dict):
            brief_pack = brief

    if brief_pack is not None:
        arguments["brief"] = brief_pack
        arguments["brief_tokens"] = estimate_brief_tokens(brief_pack)
        arguments["supplied_brief"] = True
    elif STAGE_CONTEXT in stages_subset:
        intake = intake_brief(goal, files, reader=reader)
        arguments["brief"] = intake["brief"]
        arguments["brief_tokens"] = intake["tokens"]
        arguments["supplied_brief"] = True
    return arguments


def intake_brief(goal, files, *, reader=None, jev_policy=None,
                 site="hourglass-intake") -> Dict[str, Any]:
    """The ``context`` stage's artifact for a composed plan run (HV-2-use).

    ONE owner for "the brief a plan run starts from", so no lane invents its
    own shape, its own reader, or its own token number:

    * the pack comes from :func:`harness.brief.build_brief`, the same owner
      the planning ladder curates through -- there is no second brief
      artifact;
    * the size is MEASURED with ``estimate_brief_tokens`` over the bytes the
      pack actually ships, because that number is what caps every later
      stage (``compose_plan(brief_tokens=...)``); and
    * the grounding lint runs here, through ``validate_brief``, so an
      ungrounded pack is visible at the moment it is built.

    ``issues`` are returned rather than raised: a caller decides whether an
    imperfect brief should stop the run, and the fact belongs on the
    envelope either way. An empty list means the pack passed the lint.

    A candidate the run cannot read is **excluded, not fatal**. A plan lane is
    allowed to name a file that does not exist yet -- a goal whose first node
    creates it is an ordinary goal -- and ``build_brief`` refuses to invent
    content for a path it cannot read, so the two cannot simply be handed to
    each other. The exclusion goes through the pack's own vocabulary for it
    (``scope.excluded``, and therefore ``coverage``), so an unreadable
    candidate stays visible instead of being dropped on the floor, and the
    returned ``excluded`` list names them for the caller and the ledger.

    A brief over no readable source is still a brief: it is simply not
    *evidence*, which is why the composition refuses its bypass rather than
    treating an empty pack as a fresh one.
    """
    probe = reader or read_text
    usable: List[str] = []
    excluded: List[str] = []
    for path in list(files or []):
        try:
            probe(path)
        except OSError:
            excluded.append(path)
        else:
            usable.append(path)
    pack = build_brief(goal, usable, reader=reader, scope=excluded)
    judgment = None
    if jev_policy is not None and hasattr(jev_policy, "evaluate_hourglass_stage"):
        state = {
            "request": goal,
            "goal": goal,
            "sources": usable,
            "omitted": excluded,
            "brief_render": render_brief(pack),
        }
        try:
            eval_res = jev_policy.evaluate_hourglass_stage(
                "context_intake", state, site=site)
            if isinstance(eval_res, tuple) and len(eval_res) >= 2:
                _result, structural = eval_res[0], eval_res[1]
            else:
                _result, structural = None, getattr(eval_res, "structural", {}) or {}
            from .jev_packs import HOURGLASS_STAGE_DIMENSIONS
            declared = HOURGLASS_STAGE_DIMENSIONS["context_intake"]["signals"]
            signals = {name: (structural or {}).get(name) for name in declared
                       if (structural or {}).get(name) is not None}
            judgment = {
                "dimension": "context_intake",
                "signals": signals,
                "native": bool((structural or {}).get("native")),
            }
        except (HarnessError, TypeError, ValueError):
            judgment = {"dimension": "context_intake", "signals": {}, "native": False}
    return {
        "brief": pack,
        "tokens": estimate_brief_tokens(pack),
        "issues": list(validate_brief(pack, reader=reader) or []),
        "excluded": excluded,
        "judgment": judgment,
    }


# ---- GAP-recondense: when a stage-mutated tree invalidates the brief -----
#
# A supplied brief buys the ``context`` bypass only while it is still
# evidence. ``brief.freshness_report`` answers one half of that -- has a
# cited file changed? -- and a *composed* run has two more, neither of which
# a pin check can see: a stage gate that failed, which voids the green-tree
# basis the plan was written against, and an executed node that wrote a path
# the brief describes, which makes the brief's account of that path stale
# even while every file it pinned is byte-identical. The trigger vocabulary
# is declared here so a caller reads a fixed set of reasons instead of
# inventing one, and precedence is code's rather than a model's: a terminal
# fact outranks a drifted pin, and a drifted pin outranks a heuristic overlap.

RECONDENSE_GATE_FAILURE = "gate_failure"
RECONDENSE_PIN_DRIFT = "pin_drift"
RECONDENSE_EXECUTED_OVERLAP = "executed_overlap"
RECONDENSE_TRIGGERS = (RECONDENSE_GATE_FAILURE, RECONDENSE_PIN_DRIFT,
                       RECONDENSE_EXECUTED_OVERLAP)
RECONDENSE_REASONS = {
    RECONDENSE_GATE_FAILURE: ("a stage gate failed, so the green-tree basis "
                              "the plan was written against is void"),
    RECONDENSE_PIN_DRIFT: "a cited source no longer matches its pin",
    RECONDENSE_EXECUTED_OVERLAP: ("an executed node wrote a path the brief "
                                  "describes"),
}


def _brief_path(value):
    """One comparable path: separators normalized, no leading ``./``.

    Deliberately not a filesystem operation. This only decides whether two
    names a brief and an executed node both report are the same path, and it
    refuses to guess about case or symlinks rather than silently matching on
    one platform and missing on the other -- a false "no overlap" is a stale
    brief nobody notices, so the rule stays exact and is stated here.
    """
    if not isinstance(value, str):
        return None
    text = value.strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text or None


def _recondense_failures(gate_failures):
    """The stage gates that failed, refusing a bare string.

    A string here would silently iterate into characters and count as one
    failure per letter, which is the same class of mistake ``resolve_stages``
    already refuses for a declared-stage value.
    """
    if gate_failures is None:
        return []
    if isinstance(gate_failures, str):
        raise HarnessError(
            "gate_failures must be a sequence of gate names, not a string: "
            "{0!r}".format(gate_failures))
    return [f for f in gate_failures if f]


def brief_covered_paths(pack) -> List[str]:
    """Every path a brief describes, read from its own declared shape.

    A v2 pack names its evidence in ``grounding.sources[].path`` and its
    scope in ``scope.included``. Both are read; nothing is inferred from
    prose, because an overlap the brief never declared is not one this owner
    can honestly claim to have found.
    """
    if not isinstance(pack, dict):
        return []
    candidates = []
    for source in (pack.get("grounding") or {}).get("sources") or []:
        if isinstance(source, dict):
            candidates.append(source.get("path"))
    scope = pack.get("scope")
    if isinstance(scope, dict):
        candidates.extend(scope.get("included") or [])
    covered: List[str] = []
    for value in candidates:
        path = _brief_path(value)
        if path and path not in covered:
            covered.append(path)
    return covered


def recondense_decision(*, brief, freshness=None, touched_paths=(),
                        gate_failures=(), reader=None) -> Dict[str, Any]:
    """Must the brief be rebuilt before the run continues? Code owns the call.

    Returns the decision, the highest-precedence ``trigger``, *every* reason
    that applies, and the evidence behind each, so a refresh is never a bare
    boolean: a caller is expected to report why it paid to re-condense.

    ``freshness`` lets a caller that already measured the brief (for instance
    to decide a bypass once) reuse that report instead of re-reading the
    tree; omitted, it is measured here. ``touched_paths`` is what executed
    nodes actually wrote, and ``gate_failures`` the gates that failed -- both
    are supplied by a caller that ran a stage, which is why this owner cannot
    discover them itself.
    """
    if freshness is None:
        freshness = freshness_report(brief, reader=reader)
    failures = _recondense_failures(gate_failures)
    triggers: List[str] = []
    details: Dict[str, Any] = {}
    if failures:
        triggers.append(RECONDENSE_GATE_FAILURE)
        details[RECONDENSE_GATE_FAILURE] = {
            "gates": [str(f) for f in failures]}
    if not freshness.get("fresh"):
        triggers.append(RECONDENSE_PIN_DRIFT)
        details[RECONDENSE_PIN_DRIFT] = {
            "checked": freshness.get("checked"),
            "stale": [s.get("id") for s in freshness.get("stale") or []
                      if isinstance(s, dict)],
            "missing": [s.get("id") for s in freshness.get("missing") or []
                        if isinstance(s, dict)],
            "reasons": list(freshness.get("reasons") or []),
        }
    touched = {_brief_path(value) for value in (touched_paths or ())}
    overlap = sorted(set(brief_covered_paths(brief))
                     & {p for p in touched if p})
    if overlap:
        triggers.append(RECONDENSE_EXECUTED_OVERLAP)
        details[RECONDENSE_EXECUTED_OVERLAP] = {"paths": overlap}
    trigger = triggers[0] if triggers else None
    return {
        "refresh": trigger is not None,
        "trigger": trigger,
        "triggers": triggers,
        "reasons": [RECONDENSE_REASONS[t] for t in triggers],
        "details": details,
    }


def resolve_stages(declared: Optional[Sequence[str]] = None, *,
                   supplied_brief: bool = False,
                   supplied_plan: bool = False,
                   brief: Optional[Dict[str, Any]] = None,
                   reader=None,
                   touched_paths=None,
                   gate_failures=None,
                   freshness=None,
                   ) -> Dict[str, Any]:
    """Select the stages that will actually run, in pipeline order.

    ``declared`` is the operator's (or config's) subset of ``STAGE_ORDER``;
    ``None`` keeps the Hourglass default of every stage. Order is the
    pipeline's, not the caller's -- a caller may drop a stage, never reorder
    one into a shape the lanes do not support. An unknown name, a duplicate,
    and a bare string that is really a config value are each refused rather
    than dropped.

    A supplied brief or plan bypasses the stage that would have produced it
    (see ``STAGE_BYPASS_REASONS``), which is how a caller reuses upstream
    work without paying for intake or decomposition twice. The bypass is
    returned, not hidden: a stage that did not run is visible evidence.

    A bypass backed by an actual brief *pack* is stronger evidence than the
    bare ``supplied_brief`` flag, so it is checked: while the pack is fresh
    the bypass stands, and once it has drifted the bypass is denied, the
    stage runs, and the refusal is reported under ``denied``.

    ``touched_paths``/``gate_failures``/``freshness`` (GAP-recondense) are
    the evidence a *composed* run has that a pin check cannot see. Supplying
    any of them routes the bypass decision through
    :func:`recondense_decision` -- the ONE owner of the trigger vocabulary --
    and returns its decision under ``recondense``. Supplying none of them
    keeps this function's historical behaviour exactly: a tri-state sentinel,
    where ``touched_paths=None`` means "no stage ran, nothing to report" and
    ``touched_paths=()`` means "a stage ran and wrote nothing".
    """
    if declared is None:
        wanted = list(HOURGLASS_DEFAULT_STAGES)
    else:
        if isinstance(declared, str):
            raise HarnessError(
                "declared stages must be a sequence of stage names, not a "
                "string: {0!r}".format(declared))
        wanted = []
        for name in declared:
            stage = str(name or "").strip().lower()
            if not stage:
                raise HarnessError("declared stage names cannot be blank")
            if stage not in STAGE_ORDER:
                raise HarnessError(
                    "unknown stage {0!r}; the composable stages are {1}"
                    .format(stage, ", ".join(STAGE_ORDER)))
            if stage in wanted:
                raise HarnessError(
                    "stage {0!r} is declared twice; each stage composes at "
                    "most once per run".format(stage))
            wanted.append(stage)

    bypassed: Dict[str, str] = {}
    denied: Dict[str, str] = {}
    decision: Optional[Dict[str, Any]] = None
    # Tri-state: None means no stage has run, so there is no decision to make.
    stage_evidence = (touched_paths is not None or freshness is not None
                      or bool(_recondense_failures(gate_failures)))
    if (supplied_brief or brief is not None) and STAGE_CONTEXT in wanted:
        if brief is None:
            bypassed[STAGE_CONTEXT] = STAGE_BYPASS_REASONS[STAGE_CONTEXT]
        else:
            if stage_evidence:
                decision = recondense_decision(
                    brief=brief, freshness=freshness, reader=reader,
                    touched_paths=touched_paths or (),
                    gate_failures=gate_failures or ())
                drifted = bool(decision["refresh"])
            else:
                drifted = not freshness_report(brief, reader=reader).get("fresh")
            if not drifted:
                bypassed[STAGE_CONTEXT] = STAGE_BYPASS_REASONS[STAGE_CONTEXT]
            else:
                reason = STAGE_DENIED_BYPASS[STAGE_CONTEXT]
                if decision is not None and decision.get("trigger"):
                    # The refusal names its trigger, so a reader can tell a
                    # re-condense paid for by a failed gate from one paid for
                    # by a drifted pin without diffing the tree.
                    reason = "{0} (trigger: {1})".format(
                        reason, decision["trigger"])
                denied[STAGE_CONTEXT] = reason
    if supplied_plan and STAGE_PLANNING in wanted:
        bypassed[STAGE_PLANNING] = STAGE_BYPASS_REASONS[STAGE_PLANNING]

    # A *denied* bypass is not a skip: it is the reason the stage runs. Only a
    # granted bypass removes a stage from the run.
    stages = [s for s in STAGE_ORDER if s in wanted and s not in bypassed]
    resolved = {"stages": stages, "bypassed": bypassed, "denied": denied,
                "declared": list(wanted)}
    if decision is not None:
        resolved["recondense"] = decision
    return resolved


def _narrow(previous: int, factor: float, floor: int) -> int:
    """The next stage's ceiling: strictly smaller, never larger."""
    nxt = int(previous * factor)
    if nxt >= previous:
        nxt = previous - 1
    if previous < floor:
        return max(1, nxt)
    return max(floor, nxt)


def compose_stages(*, budget, declared: Optional[Sequence[str]] = None,
                   supplied_brief: bool = False, supplied_plan: bool = False,
                   brief_tokens: Optional[int] = None,
                   brief: Optional[Dict[str, Any]] = None,
                   reader=None,
                   touched_paths=None,
                   gate_failures=None,
                   freshness=None) -> Dict[str, Any]:
    """Compose the run's stages against ONE ``TokenBudget`` (HV-3).

    ``budget`` is the run's own budget object -- composition never invents an
    allowance, it only asks each stage to narrow the one above it, which is
    what makes "a stage cannot raise its own limits" a structural property
    rather than a promise. Successive stages get strictly smaller ceilings,
    and when the caller measured the curated brief it just produced
    (``brief_tokens``) each later stage is additionally capped at that brief,
    so planning is preflighted against the evidence it will actually read.

    Returns the stage list, the bypassed stages with their reasons, and each
    stage's child budget, so a caller reserves from exactly the right one.
    """
    if budget is None:
        raise HarnessError(
            "stage composition needs the run's TokenBudget; it does not "
            "create one (harness/token_budget.py is the one owner)")
    selection = resolve_stages(declared, supplied_brief=supplied_brief,
                               supplied_plan=supplied_plan, brief=brief,
                               reader=reader, touched_paths=touched_paths,
                               gate_failures=gate_failures,
                               freshness=freshness)
    if brief_tokens is not None:
        brief_tokens = int(brief_tokens)
        if brief_tokens < 0:
            raise HarnessError(
                "brief_tokens cannot be negative: {0}".format(brief_tokens))

    stages: List[Dict[str, Any]] = []
    prev_in = budget.max_input_tokens
    prev_out = budget.max_output_tokens
    for stage in selection["stages"]:
        if not stages:
            # The first stage inherits the run's own ceilings; it is the
            # parent, so it cannot exceed them by construction.
            want_in, want_out = prev_in, prev_out
        else:
            want_in = _narrow(prev_in, STAGE_INPUT_DECAY,
                              MIN_STAGE_INPUT_TOKENS)
            want_out = _narrow(prev_out, STAGE_OUTPUT_DECAY,
                               MIN_STAGE_OUTPUT_TOKENS)
            if brief_tokens is not None:
                # Plan over the brief that exists, not over a guess of it.
                want_in = min(want_in,
                              max(brief_tokens, MIN_STAGE_INPUT_TOKENS))
        if want_in > prev_in or want_out > prev_out:
            raise HarnessError(
                "stage {0!r} asked for ({1}, {2}) above its own ceiling "
                "({3}, {4}); a stage may not raise its own limits"
                .format(stage, want_in, want_out, prev_in, prev_out))
        child = budget.stage(stage, max_input_tokens=want_in,
                             max_output_tokens=want_out)
        stages.append({"stage": stage, "budget": child,
                       "max_input_tokens": want_in,
                       "max_output_tokens": want_out})
        prev_in, prev_out = want_in, want_out

    composed = {"stages": stages, "bypassed": selection["bypassed"],
                "denied": selection["denied"],
                "declared": selection["declared"],
                "run_budget": budget.label}
    if "recondense" in selection:
        # The decision rides with the composition, so "did this run re-condense,
        # and why" is answerable from the envelope alone.
        composed["recondense"] = selection["recondense"]
    return composed


def stage_budget(composition: Dict[str, Any], stage: str):
    """The child budget for one stage, or ``None`` when it did not run."""
    for entry in composition.get("stages") or []:
        if entry.get("stage") == stage:
            return entry.get("budget")
    return None


def stage_states(composition: Dict[str, Any]) -> Dict[str, str]:
    """What happened to each declared stage, for every stage in the contract.

    Composition has *resolved* every stage it budgets; it has not *run* it.
    So a stage this owner only allowed for reports ``pending`` and a stage
    that did not compose reports ``skipped``. A selected stage that is
    silently absent from this map is the one thing a composition must never
    produce, so the map always carries all four declared stages.

    ``completed`` is in the declared vocabulary but has no producer here:
    this owner budgets stages, it does not perform them. A caller that
    actually ran a stage reports that through
    ``composition_envelope(..., states=...)``; dispatching a work package is
    ``HV-5``'s contract.
    """
    composed = {entry.get("stage") for entry in composition.get("stages") or []}
    return {stage: (STATE_PENDING if stage in composed else STATE_SKIPPED)
            for stage in STAGE_ORDER}


def composition_envelope(composition: Dict[str, Any], *,
                         states: Optional[Dict[str, str]] = None
                         ) -> Dict[str, Any]:
    """The serialisable view of a composition -- what lands on the envelope.

    The live child budgets stay out of it on purpose: an envelope is written
    to JSON and compared across runs, and a budget object is neither. What
    survives is the decision -- which stages ran, what each was allowed to
    spend, which were bypassed and why, and where each ended up -- which is
    the part a reader of the plan needs in order to believe the run was
    composed rather than defaulted.
    """
    resolved = states if states is not None else stage_states(composition)
    return {
        "run_budget": composition.get("run_budget"),
        "stages": [
            {"stage": entry.get("stage"),
             "max_input_tokens": entry.get("max_input_tokens"),
             "max_output_tokens": entry.get("max_output_tokens"),
             "state": resolved.get(entry.get("stage"), STATE_PENDING)}
            for entry in composition.get("stages") or []
        ],
        "skipped": [name for name, state in resolved.items()
                    if state == STATE_SKIPPED],
        # Caller-reported completions surface here for the same reason
        # `skipped` does. A stage that did not compose is absent from
        # `stages`, so without this list a stage a caller actually ran -- the
        # `context` stage whose brief the caller curated, say -- would land in
        # NO bucket, and "every declared stage sits in exactly one of
        # completed/skipped/pending" is the invariant this envelope keeps.
        "completed": [name for name, state in resolved.items()
                      if state == STATE_COMPLETED
                      and name not in {entry.get("stage")
                                       for entry in composition.get("stages") or []}],
        "bypassed": dict(composition.get("bypassed") or {}),
        "denied_bypass": dict(composition.get("denied") or {}),
        # Added only when a stage actually ran and supplied its evidence: a
        # stable key would have to say *something* for the pre-composition
        # callers that never make this decision, and "no decision" is not the
        # same fact as "no drift".
        **({"recondense": composition["recondense"]}
           if composition.get("recondense") is not None else {}),
    }


def plan_outcome(kind: str, *, plan: Optional[Dict[str, Any]] = None,
                 questions: Optional[Sequence[str]] = None,
                 reason: Optional[str] = None) -> Dict[str, Any]:
    """The planning waist's terminal contract, fail-closed on every branch.

    Planning either stops (``sufficient``), emits a validated bounded plan,
    asks a bounded evidence request, or defers honestly. A plan is validated
    by ``dag.TaskDAG`` -- the one owner -- so composition cannot bless a DAG
    the executor would reject. A defer must say why: a silent empty plan is
    not an outcome, it is a swallowed failure.

    This is the *validator* the waist emits through. :func:`run_planning`
    routes its own terminal branches back through here, so these bounds are
    live on a real run rather than a contract nothing calls.
    """
    outcome = str(kind or "").strip().lower()
    if outcome not in PLAN_OUTCOMES:
        raise HarnessError(
            "unknown planning outcome {0!r}; the waist may only stop, plan, "
            "request evidence, or defer".format(kind))
    if outcome == OUTCOME_SUFFICIENT:
        return {"outcome": OUTCOME_SUFFICIENT}
    if outcome == OUTCOME_PLAN:
        if not isinstance(plan, dict) or not plan.get("nodes"):
            raise HarnessError(
                "a plan outcome must carry a non-empty plan; an empty plan is "
                "a defer with a reason, not a plan")
        dag = TaskDAG.from_dict(plan)  # validates; raises on a bad DAG
        return {"outcome": OUTCOME_PLAN, "plan": dag.to_dict()}
    if outcome == OUTCOME_EVIDENCE_REQUEST:
        asked = [str(q or "").strip() for q in (questions or [])]
        asked = [q for q in asked if q]
        if not asked:
            raise HarnessError(
                "an evidence request must actually ask something; use a defer "
                "with a reason when there is nothing to ask about")
        if len(asked) > MAX_EVIDENCE_QUESTIONS:
            raise HarnessError(
                "evidence request asks {0} questions, over the bound of {1}"
                .format(len(asked), MAX_EVIDENCE_QUESTIONS))
        for question in asked:
            if len(question) > MAX_EVIDENCE_QUESTION_CHARS:
                raise HarnessError(
                    "evidence question is {0} chars, over the bound of {1}"
                    .format(len(question), MAX_EVIDENCE_QUESTION_CHARS))
        if sum(len(q) for q in asked) > MAX_EVIDENCE_REQUEST_CHARS:
            raise HarnessError(
                "evidence request is over its total character bound")
        return {"outcome": OUTCOME_EVIDENCE_REQUEST, "questions": asked}
    why = str(reason or "").strip()
    if not why:
        raise HarnessError(
            "a defer must state why it deferred; an unexplained defer is a "
            "silent failure")
    if len(why) > MAX_DEFER_REASON_CHARS:
        raise HarnessError(
            "defer reason is {0} chars, over the bound of {1}"
            .format(len(why), MAX_DEFER_REASON_CHARS))
    return {"outcome": OUTCOME_DEFER, "reason": why}


# ------------------------------------------------------------- planning


def planning_ladder(stage_budget, *, rounds=2, shrink=PLANNING_SHRINK):
    """Successively narrower budgets for successive planning rounds.

    Each entry is a child of the previous one, so the ladder is structurally
    unable to widen: :class:`~harness.token_budget.TokenBudget` refuses a
    child whose maxima exceed its parent's. A shrink at or above 1.0 is
    refused outright -- a "decreasing" ladder that may hold still is a budget
    that quietly re-widened -- and the ladder stops as soon as a round would
    fall below :data:`MIN_ROUND_TOKENS`, because a request that small cannot
    be asked anything.
    """
    if not isinstance(stage_budget, TokenBudget):
        raise HarnessError("a planning ladder needs a TokenBudget stage")
    if rounds < 1:
        raise HarnessError("a planning ladder needs at least one round")
    if not 0 < shrink < 1:
        raise HarnessError(
            "planning shrink must narrow the ladder (0 < shrink < 1); got "
            "{0!r}".format(shrink))
    ladder = [stage_budget]
    while len(ladder) < rounds:
        previous = ladder[-1]
        nxt = int(previous.max_input_tokens * shrink)
        if nxt < MIN_ROUND_TOKENS or nxt >= previous.max_input_tokens:
            break
        ladder.append(previous.stage(
            "planning-round-{0}".format(len(ladder) + 1),
            max_input_tokens=nxt))
    return ladder


def _curated_brief(goal, files, *, reader, allowance_tokens):
    """The largest brief whose MEASURED token estimate fits the allowance.

    :func:`harness.brief.build_brief` estimates over the bytes it actually
    ships, so the fit is checked against the real number rather than a
    characters-per-token guess. A brief's size is monotone in its window
    budget, so the window is bisected down to the largest value that fits --
    which is the only way to get this right, because the estimate is
    dominated by the pack's own metadata (sources, coverage, rules) rather
    than by the window: a step sized from the token excess leaves the pack
    byte-identical until the window drops below the content, so a larger
    allowance can fail where a smaller one succeeded.

    ``None`` means no honest brief exists at any window -- the round defers
    rather than shipping a brief it cannot pay for.
    """
    def build(window):
        pack = build_brief(goal, files, reader=reader, max_total_chars=window)
        return pack, estimate_brief_tokens(pack)

    smallest, smallest_tokens = build(0)
    if smallest_tokens > allowance_tokens:
        return None, 0
    low, high = 0, MAX_TOTAL_WINDOW_CHARS
    best = smallest
    while high - low > _WINDOW_SEARCH_FLOOR:
        mid = (low + high) // 2
        pack, tokens = build(mid)
        if tokens <= allowance_tokens:
            low, best = mid, pack
        else:
            high = mid
    return best, low


def _unrepresented(pack) -> List[str]:
    """The brief's own admission of what it could not represent."""
    return list(pack.get("omitted") or [])


def _conflicts(pack) -> List[Dict[str, Any]]:
    return [c for c in (pack.get("conflicts") or []) if isinstance(c, dict)]


def _evidence_request(pack, *, max_items=MAX_EVIDENCE_QUESTIONS):
    """A BOUNDED request for the evidence the brief says it is missing."""
    items: List[Dict[str, Any]] = [
        {"source": path, "reason": "not represented in the brief"}
        for path in _unrepresented(pack)]
    for conflict in _conflicts(pack):
        items.append({
            "source": ", ".join(str(s) for s in conflict.get("source_ids") or []),
            "reason": str(conflict.get("description") or "declared conflict"),
        })
        if len(items) >= max_items:
            break
    return items[:max_items]


def _jev_sufficiency(jev_policy, goal, pack, *, site):
    """Ask the one declared dimension that owns 'is this enough to plan on?'.

    Returns ``(signals, native)``. A policy that fails, falls back, or
    answers out of vocabulary yields ``native=False`` with whatever signals it
    produced, and the caller reads that as "not sufficient". Composition never
    re-implements the judgment and never promotes a fallback to native.

    The signal names come from the pack that declared them, not from a local
    list, so a new dimension's vocabulary cannot drift from this reader.
    """
    from .jev_packs import HOURGLASS_STAGE_DIMENSIONS
    state = {
        "goal": goal,
        "represented_sources": len(
            ((pack.get("grounding") or {}).get("sources") or [])),
        "omitted": _unrepresented(pack),
        "conflicts": [c.get("description") for c in _conflicts(pack)],
        "brief_render": render_brief(pack),
    }
    _result, structural = jev_policy.evaluate_hourglass_stage(
        "plan_soundness", state, site=site)
    structural = structural or {}
    declared = HOURGLASS_STAGE_DIMENSIONS["plan_soundness"]["signals"]
    signals = {name: structural.get(name) for name in declared
               if structural.get(name) is not None}
    return signals, bool(structural.get("native"))


def _validated_plan(plan_data, *, max_nodes=DEFAULT_MAX_PLAN_NODES):
    """A plan is accepted only if it validates AND stays inside its bound."""
    dag = plan_data if isinstance(plan_data, TaskDAG) else TaskDAG.from_dict(
        plan_data)
    count = len(dag.nodes)
    if count == 0:
        raise HarnessError("a plan must declare at least one node")
    if count > max_nodes:
        raise HarnessError(
            "plan declares {0} node(s), over the bound of {1}"
            .format(count, max_nodes))
    return dag


@dataclass(frozen=True)
class PlanningOutcome:
    """One planning run's result: exactly one of :data:`PLAN_OUTCOMES`.

    The run evidence (per-round allowances, what each round measured, the
    Jev signals, the budget snapshot) rides along with the terminal verdict
    so a reader can see *how* the waist decided, not only what it decided.
    The verdict itself is always produced through :func:`plan_outcome`, so
    the bounds on questions, defer reasons and DAG shape are live here too.
    """

    kind: str
    reason: Optional[str] = None
    brief: Optional[Dict[str, Any]] = None
    plan: Any = None
    evidence_request: Sequence[Dict[str, Any]] = ()
    rounds: Sequence[Dict[str, Any]] = ()
    jev_signals: Dict[str, Any] = field(default_factory=dict)
    budget: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.kind not in PLAN_OUTCOMES:
            raise HarnessError(
                "planning outcome must be one of {0}; got {1!r}"
                .format(list(PLAN_OUTCOMES), self.kind))
        object.__setattr__(self, "evidence_request",
                           tuple(self.evidence_request or ()))
        object.__setattr__(self, "rounds", tuple(self.rounds or ()))
        if self.kind == OUTCOME_EVIDENCE_REQUEST and not self.evidence_request:
            raise HarnessError(
                "an evidence request must actually ask something; use a defer "
                "with a reason when there is nothing to ask about")
        if self.kind == OUTCOME_DEFER and not str(self.reason or "").strip():
            raise HarnessError(
                "a defer must state why it deferred; an unexplained defer is a "
                "silent failure")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "reason": self.reason,
            "brief_tokens": (self.brief or {}).get("estimated_tokens"),
            "plan": (self.plan.to_dict() if isinstance(self.plan, TaskDAG)
                     else self.plan),
            "evidence_request": [dict(item) for item in self.evidence_request],
            "rounds": [dict(item) for item in self.rounds],
            "jev_signals": dict(self.jev_signals),
            "budget": dict(self.budget),
        }


def run_planning(*, goal, budget, stage_budget=None, files=(), reader=None, brief=None,
                 jev_policy=None, planner=None, rounds=2,
                 max_nodes=DEFAULT_MAX_PLAN_NODES,
                 max_evidence_items=MAX_EVIDENCE_QUESTIONS,
                 site="hourglass-planning"):
    """Run the planning stage: curate, judge sufficiency, and stop honestly.

    One round is one allowance, and the round's allowance is what its brief
    must fit inside. The ladder either finds the evidence sufficient (and
    stops there, spending nothing further), asks for a bounded piece of
    evidence, or -- when a ``planner`` is supplied -- emits a validated
    bounded plan. When it is exhausted the outcome is an honest ``defer``
    naming what was still missing, never a plan built on evidence the stage
    admitted it could not represent.
    """
    if jev_policy is not None and not hasattr(jev_policy,
                                               "evaluate_hourglass_stage"):
        raise HarnessError(
            "jev_policy must be a JevPolicy-like owner exposing "
            "evaluate_hourglass_stage")
    if planner is not None and not callable(planner):
        raise HarnessError("planner must be callable")
    if not isinstance(budget, TokenBudget):
        raise HarnessError("planning needs a TokenBudget to spend from")
    if stage_budget is not None and not isinstance(stage_budget, TokenBudget):
        raise HarnessError("planning stage_budget must be a TokenBudget")
    if stage_budget is not None and (
            stage_budget.label != STAGE_PLANNING
            or stage_budget._parent is not budget):
        raise HarnessError(
            "planning stage_budget must be the planning child of budget")

    stage = stage_budget or budget.stage(STAGE_PLANNING)
    ladder = planning_ladder(stage, rounds=rounds)
    round_log: List[Dict[str, Any]] = []
    jev_signals: Dict[str, Any] = {}
    pack = brief

    for index, round_budget in enumerate(ladder, start=1):
        allowance = round_budget.max_input_tokens
        if index > 1 and not files:
            # Nothing left to curate. A second round would judge the IDENTICAL
            # brief again, and a judgment that cannot change its answer must
            # not be paid for -- so the ladder stops here and says why.
            round_log.append({"round": index, "outcome": STATE_SKIPPED,
                              "reason": "nothing_left_to_curate",
                              "max_input_tokens": allowance})
            break
        if pack is None or index > 1:
            # Round one reads a supplied brief as data; every round after the
            # first curates its own, because a narrower allowance has to buy a
            # narrower brief. Curating reads the sources, so a caller that
            # supplies both a brief and unreadable files gets the reader's own
            # error rather than a silently different evidence set.
            pack, _ = _curated_brief(goal, files, reader=reader,
                                     allowance_tokens=allowance)
        if pack is None:
            round_log.append({"round": index, "outcome": STATE_SKIPPED,
                              "reason": "brief_exceeds_allowance",
                              "max_input_tokens": allowance})
            # A round with no honest brief is not a stopping point by itself:
            # a narrower round may still fit. The ladder decides.
            continue

        issues = validate_brief(pack, reader=reader)
        missing = _unrepresented(pack)
        conflicts = _conflicts(pack)
        sources = len(((pack.get("grounding") or {}).get("sources") or []))
        lint_clean = not issues
        # A brief citing no source is not evidence of anything: without this
        # bound an empty pack reads as a clean, gapless, conflictless brief and
        # planning would stop on it.
        sufficient = lint_clean and sources > 0 and not missing and not conflicts
        native = False
        # The semantic question is only asked when the artifact's own facts
        # are inconclusive, a failed lint always wins over a Jev "yes", and a
        # brief citing nothing is not asked about at all -- that question has
        # no answer worth a reservation.
        if lint_clean and sources > 0 and jev_policy is not None \
                and not sufficient:
            try:
                jev_signals, native = _jev_sufficiency(jev_policy, goal, pack,
                                                      site=site)
            except HarnessError:
                jev_signals, native = {}, False
            if native and sources > 0:
                requested = jev_signals.get("plan_evidence_requested")
                # The semantic answer is Jev's; what counts as "yes, ask for
                # more" is code's. A missing signal is not a quiet yes.
                if isinstance(requested, (int, float)) \
                        and not isinstance(requested, bool) \
                        and requested < SUFFICIENCY_NOUL_THRESHOLD:
                    sufficient = True
        round_log.append({
            "round": index,
            "outcome": (OUTCOME_SUFFICIENT if sufficient
                        else OUTCOME_EVIDENCE_REQUEST),
            "max_input_tokens": allowance,
            "brief_tokens": pack.get("estimated_tokens"),
            "sources": sources,
            "omitted": len(missing),
            "conflicts": len(conflicts),
            "grounding_issues": len(issues),
            "jev_native": native,
        })
        if sufficient:
            # Route through the one terminal contract, so the verdict a run
            # reports is the same checked verdict a caller would get.
            plan_outcome(OUTCOME_SUFFICIENT)
            return PlanningOutcome(
                OUTCOME_SUFFICIENT, reason="brief_covers_the_request",
                brief=pack, rounds=round_log, jev_signals=jev_signals,
                budget=stage.snapshot())

    if planner is not None and pack is not None:
        try:
            dag = _validated_plan(planner(goal, pack), max_nodes=max_nodes)
        except HarnessError as exc:
            return PlanningOutcome(
                OUTCOME_DEFER, reason="plan_rejected: {0}".format(exc),
                brief=pack, rounds=round_log, jev_signals=jev_signals,
                budget=stage.snapshot())
        plan_outcome(OUTCOME_PLAN, plan=dag.to_dict())
        return PlanningOutcome(
            OUTCOME_PLAN, reason="validated_bounded_plan", brief=pack,
            plan=dag, rounds=round_log, jev_signals=jev_signals,
            budget=stage.snapshot())

    request = _evidence_request(pack, max_items=max_evidence_items) \
        if pack is not None else []
    if request:
        # The bound is the waist's, so an over-long question is refused here
        # rather than trimmed into something the caller never asked.
        plan_outcome(OUTCOME_EVIDENCE_REQUEST,
                     questions=["{0}: {1}".format(item.get("source"),
                                                 item.get("reason"))
                                for item in request])
        reason = "bounded_evidence_request"
    elif pack is None:
        reason = "no_brief_fit_any_round"
    elif not ((pack.get("grounding") or {}).get("sources") or []):
        reason = "no_evidence_cited"
    else:
        reason = "nothing_left_to_request"
    if not request:
        plan_outcome(OUTCOME_DEFER, reason=reason)
    return PlanningOutcome(
        OUTCOME_EVIDENCE_REQUEST if request else OUTCOME_DEFER,
        reason=reason, brief=pack, evidence_request=request, rounds=round_log,
        jev_signals=jev_signals, budget=stage.snapshot())
