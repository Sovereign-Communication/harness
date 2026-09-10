"""Read-only seat extractor for existing Harness run JSON.

This module only reads existing run files and emits structured seat rows.
It does not modify any source file and does not depend on live network.

Run JSON shape in this repo is not a fixed public standard, so the extractor
is intentionally defensive: it treats missing fields as absent rather than
crashing on schema drift.

Seat model
-----------
One row per model *seat* from an existing run JSON. A seat is one model
attempt inside a run: a panel seat, a panel-failure seat, a specialist
convergence seat, etc.

Labels are mutually exclusive and severity-ordered:
    unusable > truncated > usable_stop
"""

import json
import os
import re
from typing import Any, Dict, Iterable, List, Tuple

from .schema import (
    SeatRow,
    resolve_label,
)




def guess_task_type(run: Dict[str, Any], keys: List[str]) -> str:
    """Best-effort task type from run shape and keys."""
    if "panel_results" in run and run.get("panel_results"):
        if run.get("convergence") and isinstance(run["convergence"], dict):
            return "structured_claims" if _has_claims(run) else "verify_panel"
        return "verify_panel"
    if "rounds" in run or "verify" in run:
        return "apply"
    if "task_id" in run and str(run.get("task_id", "")).startswith("bench"):
        return "bench"
    if "run_id" in run and "probe" in str(keys):
        return "probe"
    if "consent" in keys:
        return "consent"
    return "verify_panel"


def _has_claims(run: Dict[str, Any]) -> bool:
    conv = run.get("convergence")
    if not isinstance(conv, dict):
        return False
    claims = conv.get("specialist")
    if not isinstance(claims, dict):
        return False
    return bool(claims.get("claims"))


def guess_seat_role(run: Dict[str, Any], seat: Dict[str, Any], seat_kind: str = "panel") -> str:
    """Best-effort seat role from run shape and seat kind.

    seat_kind is "panel", "panel_failure", or "specialist".
    """
    if seat_kind == "specialist":
        return "specialist"
    if seat_kind == "panel_failure":
        return "panel_failure"
    return "panel"


def parse_json_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (int, float, str, bool)):
        return value
    if isinstance(value, dict):
        return {k: parse_json_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [parse_json_value(v) for v in value]
    return str(value)


def extract_seats_from_run(
    path: str,
    run: Dict[str, Any],
    observed: Dict[str, Dict[str, Any]],
    seat_index_offset: int = 0,
) -> Tuple[List[SeatRow], int]:
    """Extract seat rows from one run JSON.

    Returns (rows, new_seat_index_offset).

    Emits one row per seat: each panel_results entry, each panel_failures entry,
    and an optional specialist convergence seat when present.
    """
    rows: List[SeatRow] = []
    task_type = guess_task_type(run, list(run.keys()))
    panel = run.get("panel_results") or []
    if not isinstance(panel, list):
        panel = []
    panel_failures = run.get("panel_failures") or []
    if not isinstance(panel_failures, list):
        panel_failures = []
    conv = run.get("convergence")
    has_specialist = isinstance(conv, dict) and isinstance(conv.get("specialist"), dict)

    # Panel seats
    for idx, seat in enumerate(panel):
        model = seat.get("model")
        if not model:
            continue
        content = seat.get("content")
        content_present = bool(content) and isinstance(content, str) and content.strip()
        resp_chars = len(content) if isinstance(content, str) else 0
        finish_reason = seat.get("finish_reason")
        status = seat.get("status")
        truncated_flag = bool(seat.get("truncated"))

        structured_required = _structured_required_for_run(run, task_type)
        parseable = _parseable_for_seat(seat, structured_required)

        row_dict = {
            "finish_reason": finish_reason,
            "status": status,
            "content_present": content_present,
            "parseable": parseable,
            "resp_chars": resp_chars,
            "prompt_chars": len(run.get("prompt") or ""),
            "structured_required": structured_required,
        }

        label = resolve_label(row_dict, structured_required, parseable, truncated_flag)

        features = _build_features(
            task_type=task_type,
            seat_role="panel",
            model=model,
            observed=observed,
            prompt_chars=len(run.get("prompt") or ""),
            max_tokens_requested=_max_tokens_for_run(run),
            reasoning_effort=_reasoning_effort_for_run(run),
            source_window_attached=_source_window_attached(run),
            claims_count=_claims_count(run),
            convergence_expected=_convergence_expected(run),
            is_iterative=_is_iterative_run(run),
            truncated_flag=truncated_flag,
        )

        rows.append(
            SeatRow(
                run=path,
                seat_index=seat_index_offset + idx,
                features=features,
                label=label,
                model=model,
                task_type=task_type,
                seat_role="panel",
                finish_reason=finish_reason,
                status=status,
                content_present=content_present,
                parseable=parseable,
                truncated_flag=truncated_flag,
                cost=float((seat.get("cost") if isinstance(seat.get("cost"), (int, float, str)) else 0) or 0),
                prompt_chars=len(run.get("prompt") or ""),
                resp_chars=resp_chars,
            )
        )

    # Panel-failure seats: models that were tried but produced unusable output.
    for fail_idx, fail in enumerate(panel_failures):
        model = fail.get("model")
        if not model:
            continue
        status = fail.get("status") or "invalid_output"
        content_present = False
        resp_chars = 0
        finish_reason = None
        truncated_flag = False
        parseable = False
        structured_required = _structured_required_for_run(run, task_type)

        row_dict = {
            "finish_reason": finish_reason,
            "status": status,
            "content_present": content_present,
            "parseable": parseable,
            "resp_chars": resp_chars,
            "prompt_chars": len(run.get("prompt") or ""),
            "structured_required": structured_required,
        }

        label = resolve_label(row_dict, structured_required, parseable, truncated_flag)
        features = _build_features(
            task_type=task_type,
            seat_role="panel_failure",
            model=model,
            observed=observed,
            prompt_chars=len(run.get("prompt") or ""),
            max_tokens_requested=_max_tokens_for_run(run),
            reasoning_effort=_reasoning_effort_for_run(run),
            source_window_attached=_source_window_attached(run),
            claims_count=_claims_count(run),
            convergence_expected=_convergence_expected(run),
            is_iterative=_is_iterative_run(run),
            truncated_flag=False,
        )

        rows.append(
            SeatRow(
                run=path,
                seat_index=seat_index_offset + len(panel) + fail_idx,
                features=features,
                label=label,
                model=model,
                task_type=task_type,
                seat_role="panel_failure",
                finish_reason=finish_reason,
                status=status,
                content_present=content_present,
                parseable=parseable,
                truncated_flag=truncated_flag,
                cost=float((fail.get("cost") if isinstance(fail.get("cost"), (int, float, str)) else 0) or 0),
                prompt_chars=len(run.get("prompt") or ""),
                resp_chars=resp_chars,
            )
        )

    # Specialist seat, if present
    if has_specialist:
        spec = conv.get("specialist")
        model = spec.get("model") or spec.get("model_id") or "_"
        if isinstance(model, str) and model.strip():
            claims = spec.get("claims") or {}
            raw = spec.get("raw")
            raw_present = bool(raw) and isinstance(raw, str) and raw.strip()
            # The specialist seat's primary structured output is the claims dict.
            # content_present reflects whether the seat produced its expected
            # structured output, not whether the optional raw synthesis text exists.
            claims_ok = _parseable_specialist_claims(claims)
            content_present = claims_ok
            resp_chars = len(raw) if raw_present else 0
            structured_required = True
            parseable = claims_ok

            row_dict = {
                "finish_reason": "stop",
                "status": spec.get("status"),
                "content_present": content_present,
                "parseable": parseable,
                "resp_chars": resp_chars,
                "prompt_chars": len(run.get("prompt") or ""),
                "structured_required": structured_required,
            }

            label = resolve_label(row_dict, structured_required, parseable, truncated_flag=False)
            features = _build_features(
                task_type="structured_claims",
                seat_role="specialist",
                model=model,
                observed=observed,
                prompt_chars=len(run.get("prompt") or ""),
                max_tokens_requested=_max_tokens_for_run(run),
                reasoning_effort=_reasoning_effort_for_run(run),
                source_window_attached=_source_window_attached(run),
                claims_count=len(claims) if isinstance(claims, dict) else 0,
                convergence_expected=True,
                is_iterative=False,
                truncated_flag=False,
            )

            rows.append(
                SeatRow(
                    run=path,
                    seat_index=seat_index_offset + len(panel) + len(panel_failures) + 1,
                    features=features,
                    label=label,
                    model=model,
                    task_type="structured_claims",
                    seat_role="specialist",
                    finish_reason="stop",
                    status=spec.get("status"),
                    content_present=content_present,
                    parseable=parseable,
                    cost=float((spec.get("cost") if isinstance(spec.get("cost"), (int, float, str)) else 0) or 0),
                    prompt_chars=len(run.get("prompt") or ""),
                    resp_chars=resp_chars,
                )
            )

    return rows, seat_index_offset + len(panel) + len(panel_failures) + (1 if has_specialist else 0)


def _structured_required_for_run(run: Dict[str, Any], task_type: str) -> bool:
    if task_type == "structured_claims":
        return True
    if run.get("convergence") and isinstance(run["convergence"], dict):
        return True
    if run.get("run_type") == "structured":
        return True
    return False


def _parseable_for_seat(seat: Dict[str, Any], structured_required: bool) -> bool:
    if not structured_required:
        return True
    content = seat.get("content")
    if not isinstance(content, str):
        return False
    # The model output may be wrapped in markdown fences (e.g. ```json ... ```).
    # Strip a single leading/trailing fence block before testing parseability,
    # because the seat is still functionally usable structured output.
    stripped = _strip_json_markdown_fence(content)
    try:
        json.loads(stripped)
        return True
    except (ValueError, TypeError):
        return False


def _strip_json_markdown_fence(text: str) -> str:
    """Remove a single leading/ending markdown code fence if present."""
    import re
    m = re.match(r"^\s*```\w*\s*\n?(.*?)\n?```\s*$", text, re.DOTALL)
    if m:
        return m.group(1)
    return text


def _parseable_specialist_claims(claims: Any) -> bool:
    """A specialist seat is parseable when its claims payload is a non-empty dict.

    The claims dict is the structured per-claim output the specialist seat is
    expected to produce.
    """
    if not isinstance(claims, dict):
        return False
    if not claims:
        return False
    return True


def _max_tokens_for_run(run: Dict[str, Any]) -> int:
    for k in ("max_tokens", "max_tokens_per_response", "max_tokens_requested"):
        v = run.get(k)
        if isinstance(v, (int, float)):
            return int(v)
    return 2048


def _reasoning_effort_for_run(run: Dict[str, Any]) -> str:
    for k in ("reasoning_effort", "reasoning"):
        v = run.get(k)
        if isinstance(v, str):
            return v
    if run.get("reasoning", {}):
        return "low"
    return "auto"


def _source_window_attached(run: Dict[str, Any]) -> bool:
    for key in ("source", "window", "source_window", "source_text"):
        if run.get(key):
            return True
    if run.get("prompt") and _looks_like_code_window(run.get("prompt")):
        return True
    return False


def _looks_like_code_window(text: str) -> bool:
    if not isinstance(text, str):
        return False
    return bool(re.search(r"(?m)^[0-9]+\s*\|", text))


def _claims_count(run: Dict[str, Any]) -> int:
    conv = run.get("convergence")
    if isinstance(conv, dict):
        spec = conv.get("specialist")
        if isinstance(spec, dict):
            claims = spec.get("claims")
            if isinstance(claims, dict):
                return len(claims)
    if run.get("claims") and isinstance(run["claims"], dict):
        return len(run["claims"])
    return 0


def _convergence_expected(run: Dict[str, Any]) -> bool:
    return bool(run.get("convergence") and isinstance(run["convergence"], dict))


def _is_iterative_run(run: Dict[str, Any]) -> bool:
    return bool(run.get("rounds") or run.get("round") is not None)


def _build_features(
    *,
    task_type: str,
    seat_role: str,
    model: str,
    observed: Dict[str, Dict[str, Any]],
    prompt_chars: int,
    max_tokens_requested: int,
    reasoning_effort: str,
    source_window_attached: bool,
    claims_count: int,
    convergence_expected: bool,
    is_iterative: bool,
    truncated_flag: bool = False,
) -> Dict[str, Any]:
    """Extractor-side feature build.

    Delegates to the shared, dependency-free builder in features.py so the
    dispatch-side (capability.order_pool) and extraction-side feature dicts
    are produced by exactly one implementation and cannot drift. The parity
    pin test (tests/test_local_fit_features.py) proves the two call shapes
    produce identical dicts for equivalent seats.
    """
    from .features import build_dispatch_features

    obs = observed.get(model, {})
    observed_row = {
        "usable_rate": obs.get("usable_rate", 0.0),
        "truncation_rate": obs.get("truncation_rate", 0.0),
        "unusable_rate": obs.get("unusable_rate", 0.0),
        "mean_resp_chars": obs.get("mean_resp_chars", 0.0),
        "median_resp_chars": obs.get("median_resp_chars", 0.0),
        "max_resp_chars": obs.get("max_resp_chars", 0.0),
        "n": obs.get("n", 0),
    }
    return build_dispatch_features(
        model,
        # Map the extractor's run-shape task_type onto the dispatch task
        # vocabulary: structured_claims -> "structured", else "apply" for
        # non-panel lanes; call_lane mirrors seat_role's panel/apply split.
        task=("structured" if task_type == "structured_claims"
              else ("default" if seat_role == "panel" else "apply")),
        free_tier=bool(obs.get("free_tier", False)),
        profile=None,
        calibration=None,
        call_lane=("panel" if seat_role == "panel" else "apply"),
        observed=observed_row,
        # Extraction knows richer context than dispatch; these keyword hooks
        # exist so the shared builder can consume them without a shape change.
        _extract_overrides={
            "seat_role": seat_role,
            "task_type": task_type,
            "structured_output_required": task_type in ("structured_claims",),
            "max_tokens_requested": max_tokens_requested,
            "reasoning_effort": reasoning_effort,
            "prompt_chars": prompt_chars,
            "source_window_attached": source_window_attached,
            "claims_count": claims_count,
            "convergence_expected": convergence_expected,
            "is_iterative": is_iterative,
            "declared_context_length": obs.get("context_length", 0),
            "declared_structured_json": obs.get("declared_structured_json", False),
            "declared_reasoning": obs.get("declared_reasoning", False),
        },
    )


def _free_tier(obs: Dict[str, Any]) -> bool:
    return bool(obs.get("free_tier", False))


def build_observed_map(rows: List[SeatRow]) -> Dict[str, Dict[str, Any]]:
    """Build per-model observed statistics from extracted rows.

    This is intended for offline dataset building, then persisted as part of the
    model metadata so inference uses stable observed stats.
    """
    by_model: Dict[str, List[SeatRow]] = {}
    for r in rows:
        by_model.setdefault(r.model, []).append(r)

    out: Dict[str, Dict[str, Any]] = {}
    for model, rs in by_model.items():
        n = len(rs)
        usable = sum(1 for r in rs if r.label == "usable_stop")
        trunc = sum(1 for r in rs if r.label == "truncated")
        unusable = sum(1 for r in rs if r.label == "unusable")
        resp_lens = [r.resp_chars for r in rs]
        resp_lens_sorted = sorted(resp_lens)
        mid = resp_lens_sorted[n // 2] if resp_lens_sorted else 0
        out[model] = {
            "n": n,
            "usable_rate": usable / n if n else 0.0,
            "truncation_rate": trunc / n if n else 0.0,
            "unusable_rate": unusable / n if n else 0.0,
            "mean_resp_chars": float(mean(resp_lens)) if resp_lens else 0.0,
            "median_resp_chars": float(mid),
            "max_resp_chars": float(max(resp_lens)) if resp_lens else 0.0,
            "free_tier": False,
            "context_length": 0,
            "declared_structured_json": False,
            "declared_reasoning": False,
        }
    return out


def mean(values: List[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def extract(
    run_paths: Iterable[str],
    row_filter=None,
) -> List[SeatRow]:
    """Extract seat rows from a set of existing run JSON paths (read-only).

    `row_filter` is an optional callable used for debugging subset extraction.
    """
    observed: Dict[str, Dict[str, Any]] = {}
    all_rows: List[SeatRow] = []

    # Two-pass: first collect all rows, then build observed stats, then enrich.
    for path in run_paths:
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8") as f:
            run = json.load(f)
        rows, _ = extract_seats_from_run(path, run, observed)
        all_rows.extend(rows)

    observed = build_observed_map(all_rows)

    # Second pass: rebuild with observed stats populated.
    enriched: List[SeatRow] = []
    offset = 0
    for path in run_paths:
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8") as f:
            run = json.load(f)
        rows, new_offset = extract_seats_from_run(path, run, observed, seat_index_offset=offset)
        for r in rows:
            base = observed.get(r.model, {})
            r.features["observed_usable_rate"] = base.get("usable_rate", 0.0)
            r.features["observed_truncation_rate"] = base.get("truncation_rate", 0.0)
            r.features["observed_unusable_rate"] = base.get("unusable_rate", 0.0)
            r.features["observed_mean_resp_chars"] = base.get("mean_resp_chars", 0.0)
            r.features["observed_median_resp_chars"] = base.get("median_resp_chars", 0.0)
            r.features["observed_max_resp_chars"] = base.get("max_resp_chars", 0.0)
            r.features["observed_sample_count"] = base.get("n", 0)
        enriched.extend(rows)
        offset = new_offset

    if row_filter:
        enriched = [r for r in enriched if row_filter(r)]
    return enriched


def all_run_files(root: str = "audits") -> List[str]:
    """Return all run JSON files found under audits/*/_runs/*/*.json.

    This is the single top-level entry point for 'train over everything that
    exists in the clone today'. It is generic and not hardcoded to v4.

    Only files under *_runs/ directories are returned, so ledger/summary
    files such as audits/self/round2_scores.json are intentionally excluded.
    """
    files: List[str] = []
    if not os.path.isdir(root):
        return files
    for audit in sorted(os.listdir(root)):
        audit_path = os.path.join(root, audit)
        if not os.path.isdir(audit_path):
            continue
        for sub in sorted(os.listdir(audit_path)):
            sub_path = os.path.join(audit_path, sub)
            if not os.path.isdir(sub_path):
                continue
            # sub_path is e.g. audits/scmessenger/_runs ; descend one more level
            for inner in sorted(os.listdir(sub_path)):
                inner_path = os.path.join(sub_path, inner)
                if not os.path.isdir(inner_path):
                    continue
                for entry in sorted(os.listdir(inner_path)):
                    if not entry.endswith(".json"):
                        continue
                    files.append(os.path.join(inner_path, entry))
    return files


def write_rows_csv(rows: List[SeatRow], path: str) -> None:
    import csv

    fieldnames = ["run", "seat_index", "model", "task_type", "seat_role", "label",
                  "finish_reason", "status", "content_present", "parseable",
                  "cost", "prompt_chars", "resp_chars"] + sorted(rows[0].features.keys()) if rows else []
    if not fieldnames:
        return
    # Canonical order: fixed fields then feature fields.
    fixed = ["run", "seat_index", "model", "task_type", "seat_role", "label",
             "finish_reason", "status", "content_present", "parseable",
             "cost", "prompt_chars", "resp_chars"]
    feature_fields = [f for f in fieldnames if f not in fixed]
    out_fields = fixed + sorted(feature_fields)

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=out_fields)
        writer.writeheader()
        for r in rows:
            row = {k: r.__dict__.get(k) for k in out_fields}
            row.update(r.features)
            writer.writerow(row)
