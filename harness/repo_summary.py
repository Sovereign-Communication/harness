"""JEV-P6 envelope: batch repo-element judgment + REPO-MAP rendering.

Stage D (Jev judgment via the ONE policy owner) and stage E (code-owned
aggregation) of the whole-repo summary. Code owns enumeration order, spend
accounting, resume, and every aggregate number; Jev answers ride in each
row's ``judgment``/``structural`` exactly as the policy returned them.
Contract rules carried over from the log track:

- every keyed call is preflighted, settled, and ledgered by the policy
  owner (site=repo_summary) -- this driver never touches the network;
- ``make_policy(cumulative_cost)`` composes the policy (and, above one
  governor's hard-capped ceiling, a fresh chunk) or returns ``None`` when
  the operator's cumulative run budget cannot cover another worst-case
  call -- the run then stops honestly with ``stop_reason=run_budget``;
- the judgments JSONL is append-only resume state: a budget stop is never
  persisted as a classification, so the next invocation retries it;
- fallback rows are reported, never smoothed into live coverage.
"""
import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .errors import HarnessError
from .jev_packs import REPO_SUMMARY_SITE, validate_repo_summary_pack
from .repo_items import (
    build_elements,
    element_state,
    mechanical_tallies,
    rank_symbol_elements,
    summary_listing,
    symbol_state,
)

SCHEMA = "repo-summary-v1"
NOUL_TRUE_AT = 0.5
UNMATCHED = "unmatched"

PolicyFactory = Callable[[float], Any]


def load_judgment_rows(path: Optional[str]) -> List[Dict[str, Any]]:
    """Read append-only resume rows; a truncated tail line is tolerated."""
    if not path or not os.path.isfile(path):
        return []
    rows: List[Dict[str, Any]] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                break  # interrupted append; the valid prefix is the state
            if isinstance(row, dict) and row.get("id"):
                rows.append(row)
    return rows


def append_judgment_row(path: str, row: Dict[str, Any]) -> None:
    """Persist one judged element (flushed: interrupt-safe resume)."""
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")
        handle.flush()


def _budget_refusal(structural: Dict[str, Any], judgment: Dict[str, Any]) -> bool:
    """A governor reservation refusal: not a classification, never persisted."""
    if not judgment.get("is_fallback"):
        return False
    if float(structural.get("cost") or 0.0) != 0.0:
        return False
    evidence = " ".join(str(e) for e in (judgment.get("evidence") or []))
    return ("exceed" in evidence) or ("over ceiling" in evidence)


def _row_for(element: Dict[str, Any], structural: Dict[str, Any],
             judgment: Dict[str, Any]) -> Dict[str, Any]:
    axes = judgment.get("axes") or {}
    attention = judgment.get("attention") or {}
    return {
        "id": element.get("id"),
        "element_kind": element.get("element_kind"),
        "path": element.get("path"),
        "kind": element.get("kind"),
        "loc": int(element.get("loc") or element.get("module_loc") or 0),
        "est_tokens": int(element.get("est_tokens") or 0),
        "axes": dict(axes),
        "attention": {"level": attention.get("level"),
                      "value": attention.get("value"),
                      "confidence": attention.get("confidence")},
        "nouls": dict(judgment.get("nouls") or {}),
        "is_fallback": bool(judgment.get("is_fallback")),
        "cost": float(structural.get("cost") or 0.0),
        "input_tokens": int(structural.get("input_tokens") or 0),
        "output_tokens": int(structural.get("output_tokens") or 0),
        "model": structural.get("model"),
        "site": structural.get("site"),
        "pack_id": judgment.get("pack_id"),
    }


def analyze_repo(root: Any, pack: Any, make_policy: PolicyFactory, *,
                 state_path: Optional[str] = None,
                 file_limit: Optional[int] = None,
                 symbol_limit: int = 200,
                 run_budget: Optional[float] = None,
                 task_id: Optional[str] = None,
                 exclude: Optional[List[str]] = None,
                 generated_at: Optional[str] = None) -> Dict[str, Any]:
    """Inventory the tree ($0), judge pending elements, aggregate.

    Files are judged first; centrality-ranked symbols consume whatever the
    run budget leaves. Prior rows from ``state_path`` are kept verbatim so
    multiple invocations compose into one envelope. ``exclude`` drops the
    run's own output paths (envelope, map, judgments state) from the
    inventory so a resumed run never judges its own artifacts.
    """
    pack_doc = validate_repo_summary_pack(pack)
    root_path = Path(root) if root is not None else Path.cwd()
    exclude_rel = set()
    for raw in exclude or ():
        if not raw:
            continue
        try:
            rel = os.path.relpath(str(raw), str(root_path)).replace("\\", "/")
        except ValueError:
            continue
        if not rel.startswith("../") and rel != "..":
            exclude_rel.add(rel)
    files = [f for f in summary_listing(root_path, limit=file_limit)
             if f not in exclude_rel]
    elements = build_elements(root_path, files)
    symbols = rank_symbol_elements(elements, symbol_limit)

    candidates: List[Dict[str, Any]] = list(elements) + list(symbols)
    prior_rows = load_judgment_rows(state_path)
    judged: Dict[str, Dict[str, Any]] = {row["id"]: row for row in prior_rows}
    prior_count = len(judged)
    cumulative = sum(float(row.get("cost") or 0.0) for row in judged.values())

    stop_reason = "complete"
    this_run = 0
    live_this_run = 0
    for element in candidates:
        if element["id"] in judged:
            continue
        policy = make_policy(cumulative)
        if policy is None:
            stop_reason = "run_budget"
            break
        state = (element_state(element)
                 if element.get("element_kind") != "symbol"
                 else symbol_state(element))
        try:
            _result, structural, judgment = policy.evaluate_repo_summary(
                state, pack_doc, task_id=task_id)
        except HarnessError:
            # Reservation/settlement refused mid-call: retry on resume.
            stop_reason = "run_budget"
            break
        if _budget_refusal(structural, judgment):
            stop_reason = "run_budget"
            break
        row = _row_for(element, structural, judgment)
        if state_path:
            append_judgment_row(state_path, row)
        judged[element["id"]] = row
        cumulative += row["cost"]
        this_run += 1
        if not row["is_fallback"]:
            live_this_run += 1

    rows = [judged[element["id"]] for element in candidates
            if element["id"] in judged]
    pending_ids = [element["id"] for element in candidates
                   if element["id"] not in judged]
    return aggregate_repo_summary(
        rows=rows,
        pending_ids=pending_ids,
        pack_doc=pack_doc,
        elements=elements,
        symbols=symbols,
        stop_reason=stop_reason,
        prior_count=prior_count,
        this_run=this_run,
        live_this_run=live_this_run,
        cumulative=cumulative,
        run_budget=run_budget,
        generated_at=generated_at,
    )


def aggregate_repo_summary(*, rows, pending_ids, pack_doc, elements, symbols,
                           stop_reason, prior_count, this_run, live_this_run,
                           cumulative, run_budget, generated_at) -> Dict[str, Any]:
    """Code-owned Stage E artifact. Pure arithmetic over persisted rows."""
    declared = {axis: list(spec["criteria"])
                for axis, spec in pack_doc["axes"].items()}
    axes_tally: Dict[str, Dict[str, int]] = {
        axis: {criterion: 0 for criterion in criteria}
        for axis, criteria in declared.items()
    }
    for axis in axes_tally:
        axes_tally[axis][UNMATCHED] = 0
    attention_by_level = {level: 0 for level in pack_doc["score"]["levels"]}
    attention_by_level[UNMATCHED] = 0
    attention_values: List[float] = []
    noul_tally = {name: {"true": 0, "false": 0, "unanswered": 0}
                  for name in pack_doc["nouls"]}
    live = fallbacks = 0
    spend_cost = spend_input = spend_output = calls = 0
    # spend_output sums the policy's structural output_tokens (free, but
    # reported honestly rather than assumed zero)
    element_rows: List[Dict[str, Any]] = []
    symbol_rows: List[Dict[str, Any]] = []
    for row in rows:
        for axis, value in (row.get("axes") or {}).items():
            bucket = axes_tally.get(axis)
            if bucket is None:
                continue
            if value in bucket:
                bucket[value] += 1
            else:
                bucket[UNMATCHED] += 1
        attention = row.get("attention") or {}
        level = attention.get("level")
        if level in attention_by_level:
            attention_by_level[level] += 1
        else:
            attention_by_level[UNMATCHED] += 1
        value = attention.get("value")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            attention_values.append(float(value))
        for name, answer in (row.get("nouls") or {}).items():
            tally = noul_tally.get(name)
            if tally is None:
                continue
            if isinstance(answer, (int, float)) and not isinstance(answer, bool):
                tally["true" if float(answer) >= NOUL_TRUE_AT else "false"] += 1
            else:
                tally["unanswered"] += 1
        if row.get("is_fallback"):
            fallbacks += 1
        else:
            live += 1
        spend_cost += float(row.get("cost") or 0.0)
        spend_input += int(row.get("input_tokens") or 0)
        spend_output += int(row.get("output_tokens") or 0)
        calls += 1
        target = symbol_rows if row.get("element_kind") == "symbol" else element_rows
        target.append(row)

    envelope = {
        "schema": SCHEMA,
        "generated_at": generated_at or time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "pack_id": pack_doc["id"],
        "declared": {"axes": declared,
                     "score": {"id": pack_doc["score"]["id"],
                               "levels": list(pack_doc["score"]["levels"])},
                     "nouls": list(pack_doc["nouls"])},
        "coverage": {
            "files_listed": len(elements),
            "candidates": len(elements) + len(symbols),
            "judged": len(rows),
            "judged_files": sum(1 for r in rows
                                if r.get("element_kind") != "symbol"),
            "judged_symbols": sum(1 for r in rows
                                  if r.get("element_kind") == "symbol"),
            "pending": len(pending_ids),
            "pending_sample": pending_ids[:20],
            "live_judged": live,
            "fallbacks": fallbacks,
            "prior_rows": prior_count,
            "judged_this_run": this_run,
            "live_this_run": live_this_run,
            "stop_reason": stop_reason,
        },
        "spend": {
            "run_budget": run_budget,
            "cost_usd": round(spend_cost, 6),
            "input_tokens": spend_input,
            "output_tokens": spend_output,
            "calls": calls,
            "ledger_site": REPO_SUMMARY_SITE,
        },
        "axes": axes_tally,
        "attention": {
            "by_level": attention_by_level,
            "mean_value": (round(sum(attention_values) / len(attention_values), 6)
                           if attention_values else None),
        },
        "nouls": noul_tally,
        "mechanical": mechanical_tallies(elements),
        "elements": element_rows,
        "symbols": symbol_rows,
        "structural": {
            "site": REPO_SUMMARY_SITE,
            "verdict": "pass" if (calls and not fallbacks) else (
                "fail" if not calls else "defer"),
            "cost": round(spend_cost, 6),
            "input_tokens": spend_input,
            "is_fallback": bool(calls) and fallbacks == calls,
            "model": (rows[-1].get("model") if rows else None),
        },
    }
    return envelope


_STAGE_ORDER = ("prep", "condense", "waist", "dispatch", "adjudicate",
                "cross_cutting")
_ATTENTION_MARK = ("central", "notable")


def render_repo_map(envelope: Dict[str, Any]) -> str:
    """REPO-MAP.md: the condensed, ledger-backed map of the whole repo."""
    coverage = envelope.get("coverage") or {}
    spend = envelope.get("spend") or {}
    declared = envelope.get("declared") or {}
    rows = list(envelope.get("elements") or [])
    lines = [
        "# REPO-MAP — Harness through the hourglass",
        "",
        f"- generated: {envelope.get('generated_at')}  |  pack: "
        f"`{envelope.get('pack_id')}`  |  schema: {envelope.get('schema')}",
        f"- coverage: {coverage.get('judged', 0)} judged "
        f"({coverage.get('live_judged', 0)} live / "
        f"{coverage.get('fallbacks', 0)} fallback), "
        f"{coverage.get('pending', 0)} pending, stop=`{coverage.get('stop_reason')}`",
        f"- spend: ${spend.get('cost_usd', 0.0):.6f} over "
        f"{spend.get('calls', 0)} calls ({spend.get('input_tokens', 0)} in / "
        f"{spend.get('output_tokens', 0)} out tokens)"
        + (f" of ${spend.get('run_budget')} run budget"
           if spend.get("run_budget") is not None else ""),
        f"- every keyed call was preflighted, settled, and appended to the "
        f"autonomy ledger at site=`{spend.get('ledger_site')}` "
        f"(`harness ledger verify` proves the chain)",
        "",
        "## Axis tallies (declared keys only; `unmatched` is honest, never smoothed)",
        "",
    ]
    axes = envelope.get("axes") or {}
    for axis in list(declared.get("axes") or axes):
        counts = axes.get(axis) or {}
        total = sum(counts.values())
        parts = ", ".join(f"{key}={value}" for key, value in sorted(
            counts.items(), key=lambda kv: (-kv[1], kv[0])) if value)
        lines.append(f"- **{axis}** ({total}): {parts or 'none'}")
    attention = envelope.get("attention") or {}
    lines += ["", "## Attention", ""]
    for level, count in sorted((attention.get("by_level") or {}).items(),
                               key=lambda kv: (-kv[1], kv[0])):
        if count:
            lines.append(f"- {level}: {count}")
    if attention.get("mean_value") is not None:
        lines.append(f"- mean winning probability: {attention['mean_value']}")
    lines += ["", "## Nouls (threshold 0.5, code-owned)", ""]
    for name, tally in sorted((envelope.get("nouls") or {}).items()):
        lines.append(f"- {name}: true={tally['true']} false={tally['false']} "
                     f"unanswered={tally['unanswered']}")
    stage_axes = (declared.get("axes") or {}).get("stage") or list(_STAGE_ORDER)
    by_stage: Dict[str, List[Dict[str, Any]]] = {stage: [] for stage in stage_axes}
    unjudged: List[Dict[str, Any]] = []
    for row in rows:
        stage = (row.get("axes") or {}).get("stage")
        if stage in by_stage:
            by_stage[stage].append(row)
        else:
            unjudged.append(row)
    for stage in stage_axes:
        lines += ["", f"## Stage: {stage}", ""]
        stage_rows = sorted(
            by_stage[stage],
            key=lambda r: (r.get("attention", {}).get("level") not in _ATTENTION_MARK,
                           -(r.get("attention", {}).get("value") or 0.0),
                           r.get("path") or ""))
        if not stage_rows:
            lines.append("_none judged into this stage yet_")
        for row in stage_rows:
            axes_part = ", ".join(
                f"{axis}={value}" for axis, value in sorted(
                    (row.get("axes") or {}).items())
                if axis != "stage" and value)
            level = (row.get("attention") or {}).get("level") or "unmatched"
            flag = " **[fallback]**" if row.get("is_fallback") else ""
            waist = (row.get("nouls") or {}).get("waist_relevant")
            waist_part = (", waist_relevant=yes"
                          if isinstance(waist, (int, float))
                          and not isinstance(waist, bool)
                          and waist >= NOUL_TRUE_AT else "")
            lines.append(f"- `{row.get('path')}` — attention: {level}"
                         f"{waist_part}"
                         + (f" — {axes_part}" if axes_part else "") + flag)
    if unjudged:
        lines += ["", "## Judged without a declared stage (honest unmatched)", ""]
        for row in unjudged:
            lines.append(f"- `{row.get('path')}` — "
                         f"{'fallback' if row.get('is_fallback') else 'live'}")
    pending = coverage.get("pending") or 0
    if pending:
        lines += ["", f"## Pending ({pending}) — rerun `harness repo-summary` "
                      "to continue under the run budget", ""]
        for element_id in (coverage.get("pending_sample") or []):
            lines.append(f"- {element_id}")
    symbols = list(envelope.get("symbols") or [])
    if symbols:
        lines += ["", "## Central symbols (centrality-ranked, judged)", ""]
        for row in symbols[:20]:
            level = (row.get("attention") or {}).get("level") or "unmatched"
            handling = (row.get("axes") or {}).get("handling") or "?"
            lines.append(f"- `{row.get('path')}` — {level} / {handling}"
                         + (" **[fallback]**" if row.get("is_fallback") else ""))
    mechanical = (envelope.get("mechanical") or {}).get("totals") or {}
    lines += ["", "## Mechanical totals (code-owned)", "",
              f"- files={mechanical.get('files', 0)} "
              f"loc={mechanical.get('loc', 0)} "
              f"bytes={mechanical.get('bytes', 0)} "
              f"est_tokens={mechanical.get('est_tokens', 0)} "
              f"symbols={mechanical.get('symbols', 0)}",
              ""]
    return "\n".join(lines)


def write_envelope(envelope: Dict[str, Any], out_path: str) -> str:
    """Persist the aggregate envelope JSON (utf-8, no BOM; POSIX newlines)."""
    directory = os.path.dirname(os.path.abspath(out_path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(envelope, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return out_path


def write_map(text: str, out_path: str) -> str:
    """Persist the REPO-MAP markdown (utf-8, no BOM; POSIX newlines)."""
    directory = os.path.dirname(os.path.abspath(out_path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text if text.endswith("\n") else text + "\n")
    return out_path
