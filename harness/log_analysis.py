"""JEV-LOG-envelope: batch log judgment + aggregate JSON artifact.

Stage D (Jev audit via the ONE policy owner) and Stage E (code-owned
aggregation) of the promoted log-factor analysis track. Code owns the
aggregate; the model never invents buckets, levels, paths, or actions --
unmatched items stay honestly ``unmatched`` and fallback counts are reported,
never smoothed into a pass rate.
"""
import os
from typing import Any, Dict, List, Optional, Sequence

from .log_items import extract_log_items, mechanical_tallies

MAX_LOG_CHARS = 8_000_000


def load_log_text(path: str) -> str:
    """Read a raw log dump as text (bounded; BOM-tolerant on Windows dumps)."""
    with open(path, encoding="utf-8-sig", errors="replace") as handle:
        return handle.read(MAX_LOG_CHARS)


def analyze_log(
    log_text: str, pack: Any, policy: Any, *, levels: Sequence[str] = ("warn", "error"),
    info_sample: int = 0, max_items: Optional[int] = None,
    task_id: Optional[str] = None,
) -> Dict[str, Any]:
    """One single-pass analysis: extract, judge every item, aggregate.

    ``policy`` must expose ``evaluate_log_item`` (the ONE owner, JEV-LOG-
    judgment). Stage E adds only code-owned arithmetic; Jev answers ride in
    each item's ``judgment``/``structural`` exactly as the policy returned
    them.
    """
    items = extract_log_items(log_text, levels=levels, info_sample=info_sample,
                              max_items=max_items)
    judged: List[Dict[str, Any]] = []
    for item in items:
        result, structural, judgment = policy.evaluate_log_item(
            {"item": item["text"]}, pack, task_id=task_id)
        judged.append({
            **item,
            "bucket": judgment.get("bucket"),
            "score": judgment.get("score"),
            "is_fallback": bool(judgment.get("is_fallback")),
            "path_id": judgment.get("path_id"),
            "suggested_next_action": judgment.get("suggested_next_action"),
            "structural": structural,
        })
    return aggregate_log_items(items=judged, pack=pack, log_text=log_text)


def aggregate_log_items(
    *, items: Sequence[Dict[str, Any]], pack: Any, log_text: str,
) -> Dict[str, Any]:
    """Code-owned Stage E artifact. Pure arithmetic -- no model calls.

    Coverage rule (addendum §5): every item is bucketed, explicitly
    ``unmatched``, or (on fallback) ``unmatched`` with a ``keyword_hit`` when
    a pack keyword fired. No silent drops; Jev reasons never replace the
    mechanical tallies.
    """
    pack_doc = pack if isinstance(pack, dict) else {}
    pack_id = pack_doc.get("id")
    bucketed: Dict[str, int] = {}
    unmatched = 0
    fallbacks = 0
    scores: Dict[str, int] = {}
    score_values: List[float] = []
    rows: List[Dict[str, Any]] = []
    for item in items:
        bucket = item.get("bucket")
        if bucket:
            bucketed[bucket] = bucketed.get(bucket, 0) + 1
        else:
            unmatched += 1
        if item.get("is_fallback"):
            fallbacks += 1
        score = item.get("score") or {}
        level = score.get("level")
        if level:
            scores[level] = scores.get(level, 0) + 1
        value = score.get("value")
        if isinstance(value, (int, float)):
            score_values.append(float(value))
        rows.append({
            "id": item.get("id"),
            "line_index": item.get("line_index"),
            "level": item.get("level"),
            "module": item.get("module"),
            "evidence": item.get("evidence"),
            "bucket": bucket,
            "path_id": item.get("path_id"),
            "suggested_next_action": item.get("suggested_next_action"),
            "score": {
                "id": score.get("id"),
                "level": level,
                "value": value,
                "confidence": score.get("confidence"),
            },
            "is_fallback": bool(item.get("is_fallback")),
        })
    declared = sorted((pack_doc.get("buckets") or {}))
    total = len(rows)
    mean_score = (round(sum(score_values) / len(score_values), 6)
                  if score_values else None)
    return {
        "pack_id": pack_id,
        "declared_buckets": declared,
        "items": rows,
        "coverage": {
            "total_items": total,
            "bucketed": total - unmatched,
            "unmatched": unmatched,
            "fallbacks": fallbacks,
            "live_judged": total - fallbacks,
        },
        "buckets": dict(sorted(bucketed.items())),
        "scores": {
            "by_level": dict(sorted(scores.items())),
            "mean_value": mean_score,
        },
        "mechanical": mechanical_tallies(items, log_text),
    }


def stage_b_pack_prompt(sample_texts: Sequence[str], vocabulary: str) -> List[Dict[str, str]]:
    """Build the Stage B (cheap generative) pack-draft prompt.

    The seat proposes buckets + score levels as a JSON pack draft for the
    OPERATOR to freeze. Output is a draft only: it is never valid for Stage D
    until an operator approves it (FRP: the proposer is never the approver).
    """
    samples = "\n\n".join("- " + t for t in sample_texts)
    user = (
        "Propose a DRAFT operator log-analysis pack as strict JSON (no "
        "commentary). The draft is provisional: an operator must review and "
        "freeze it before any Jev judgment run.\n"
        "Shape exactly:\n"
        '{"id": "<kebab-case-id>", "buckets": {"<bucket-id>": {"label": str, '
        '"kind": "trouble_area"|"alternate_path"|"orchestration_driver", '
        '"path_id": str, "keywords": [str, ...], "suggested_next_action": str|null, '
        '"attention": str|null}}, "score": {"id": str, "instructions": str, '
        '"levels": [str, ...]}}\n'
        "Rules: bucket ids/labels/keywords come from the sample log items only; "
        "score levels must be ordered severity strings; 3-8 buckets.\n"
        "Vocabulary/context from the operator:\n" + (vocabulary or "") + "\n"
        "Sample log items:\n" + samples)
    return [{"role": "user", "content": user}]


def freeze_pack_draft(draft: Any, *, approved: bool) -> Dict[str, Any]:
    """Stage C gate: a draft becomes a frozen pack only with operator approval.

    ``approved=False`` (or any invalid draft) refuses; freezing never mutates
    content -- validation happens again at the policy owner's edge.
    """
    if not approved:
        raise ValueError("pack draft requires explicit operator approval to freeze")
    if not isinstance(draft, dict) or not isinstance(draft.get("buckets"), dict):
        raise ValueError("pack draft must be an object with a buckets map")
    frozen = {"id": draft.get("id"), "buckets": draft.get("buckets")}
    if isinstance(draft.get("score"), dict):
        frozen["score"] = draft["score"]
    return frozen


def write_analysis(analysis: Dict[str, Any], out_path: str) -> str:
    """Persist the aggregate JSON artifact (utf-8, no BOM; POSIX newlines)."""
    import json
    directory = os.path.dirname(os.path.abspath(out_path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(analysis, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return out_path
