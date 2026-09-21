"""Operator bucket packs for JEV-P5 issue-sort (schema + matcher only).

The operator declares buckets; code owns matching. TypeSafe/Jev choice
criteria are exactly the operator bucket labels — this module never invents
buckets, path_ids, or suggested actions. Unkeyed / transport-fail /
out-of-pack paths use ``match_keywords`` against pack keywords only.
"""
from typing import Any, Dict, List, Optional, Tuple

BUCKET_KINDS = frozenset(
    ("trouble_area", "alternate_path", "orchestration_driver"))


def validate_operator_pack(pack: Any) -> Dict[str, Any]:
    """Validate an operator-declared issue-sort pack; return a clean copy.

    Required shape:
      {id: str, buckets: {bucket_id: {
          label: str,
          kind: trouble_area|alternate_path|orchestration_driver,
          path_id: str,
          keywords: [str, ...],
          suggested_next_action: str|None,
          attention: scalar|None,
      }}}
    """
    if not isinstance(pack, dict):
        raise ValueError("operator pack must be an object")
    pack_id = pack.get("id")
    if not isinstance(pack_id, str) or not pack_id:
        raise ValueError("operator pack requires a non-empty string id")
    buckets = pack.get("buckets")
    if not isinstance(buckets, dict) or not buckets:
        raise ValueError("operator pack requires a non-empty buckets map")
    out_buckets: Dict[str, Dict[str, Any]] = {}
    for bid, bucket in buckets.items():
        if not isinstance(bid, str) or not bid:
            raise ValueError("bucket ids must be non-empty strings")
        if not isinstance(bucket, dict):
            raise ValueError(f"bucket {bid!r} must be an object")
        label = bucket.get("label")
        if not isinstance(label, str) or not label:
            raise ValueError(f"bucket {bid!r} requires a non-empty label")
        kind = bucket.get("kind")
        if kind not in BUCKET_KINDS:
            raise ValueError(
                f"bucket {bid!r} kind must be one of {sorted(BUCKET_KINDS)}")
        path_id = bucket.get("path_id")
        if not isinstance(path_id, str) or not path_id:
            raise ValueError(f"bucket {bid!r} requires a non-empty path_id")
        keywords = bucket.get("keywords", [])
        if not isinstance(keywords, list) or any(
                not isinstance(k, str) or not k for k in keywords):
            raise ValueError(
                f"bucket {bid!r} keywords must be a list of non-empty strings")
        action = bucket.get("suggested_next_action")
        if action is not None and not isinstance(action, str):
            raise ValueError(
                f"bucket {bid!r} suggested_next_action must be a string when set")
        attention = bucket.get("attention")
        if attention is not None and not isinstance(
                attention, (str, int, float, bool)):
            raise ValueError(
                f"bucket {bid!r} attention must be a scalar when set")
        out_buckets[bid] = {
            "label": label,
            "kind": kind,
            "path_id": path_id,
            "keywords": list(keywords),
            "suggested_next_action": action,
            "attention": attention,
        }
    return {"id": pack_id, "buckets": out_buckets}


def issue_sort_question_pack(pack: Any) -> Dict[str, Dict[str, Any]]:
    """Build the TypeSafe choice pack: criteria keys = operator bucket ids,
    criteria values = operator labels only. No invented keys."""
    pack_doc = validate_operator_pack(pack)
    criteria = {
        bid: entry["label"] for bid, entry in pack_doc["buckets"].items()
    }
    return {
        "bucket": {
            "type": "choice",
            "instructions": (
                "Choose the operator-declared bucket that best matches this "
                "issue. Select only from the declared criteria keys; do not "
                "invent categories."),
            "criteria": criteria,
        }
    }


def match_keywords(
    text: Any, pack: Any
) -> Tuple[Optional[str], int, List[str]]:
    """Match issue text against pack keywords only.

    Returns ``(bucket_id|None, score, evidence)``. Score is the hit count for
    the winning bucket; no match yields ``(None, 0, [])``. Deterministic:
    highest score wins; ties keep the lexicographically first bucket id.
    """
    pack_doc = validate_operator_pack(pack)
    lower = (text or "").lower() if isinstance(text, str) else ""
    best_id: Optional[str] = None
    best_score = 0
    best_ev: List[str] = []
    for bid in sorted(pack_doc["buckets"]):
        keywords = pack_doc["buckets"][bid].get("keywords") or []
        hits = [kw for kw in keywords if kw.lower() in lower]
        score = len(hits)
        if score > best_score:
            best_id, best_score, best_ev = bid, score, hits
    if best_score <= 0 or best_id is None:
        return None, 0, []
    return best_id, best_score, best_ev


def sort_notes_into_buckets(
    notes: Any, pack: Any, policy: Any, *, site: str = "issue_sort"
) -> List[Dict[str, Any]]:
    """Thin orchestrator/agent helper: sort open issues / deferral notes into
    declared attention buckets. ``path_id`` always comes from the pack via
    ``evaluate_issue_sort`` — never invented here."""
    if not notes:
        return []
    items = notes if isinstance(notes, (list, tuple)) else [notes]
    out: List[Dict[str, Any]] = []
    for note in items:
        if isinstance(note, str):
            text = note
        elif isinstance(note, dict):
            text = (note.get("text") or note.get("reason")
                    or note.get("issue") or note.get("note") or "")
        else:
            text = str(note)
        _result, _structural, combo = policy.evaluate_issue_sort(
            {"issue": text}, pack, site=site)
        out.append(combo)
    return out
