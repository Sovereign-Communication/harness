"""The `harness brief` builder (MR-8 spec seed).

Generates the reusable context pack for a goal/model pair. The one rule
that makes a generated brief safe (MR-8, Grok): grounding rules for the
pack's OWN claims -- unsourced narrator facts are the poison vector, so
the builder emits NONE: every factual line is a cited file window, the
`grounding` object records the exact sources (path + content sha256) the
windows may cite, and `claims`/`unknowns` start empty for the frontier
consumer to fill under the same rule. Truncation is honest: a window that
does not fit is labeled, and its cited span covers only the bytes shown.

Hermetic by construction: files in, JSON pack out, no network, no key.
"""
import hashlib
from typing import Dict, List

from .errors import HarnessError

BRIEF_SCHEMA_VERSION = 1
WHOLE_WINDOW_CHARS = 12000
HEAD_WINDOW_CHARS = 9000
MAX_TOTAL_WINDOW_CHARS = 48000

GROUNDING_RULES = (
    "models may use only the cited windows below; every claim a consumer "
    "emits must carry source_ids citing those windows (or the goal); "
    "uncited assertions are invalid -- drop them or list them as unknowns; "
    "truncated windows cover only the bytes shown")


def _sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _window(source_id, path, content, remaining):
    """One cited window: whole file when it fits, an honestly labeled head
    excerpt when it does not. The span always covers exactly the bytes
    shown, so the consumer can hash-verify the citation."""
    budget = min(WHOLE_WINDOW_CHARS, remaining)
    if len(content) <= budget:
        return {"source_id": source_id, "path": path,
                "start_line": 1, "end_line": content.count("\n") + 1,
                "truncated": False, "content": content}
    head = content[:HEAD_WINDOW_CHARS]
    cut_ok = head.endswith("\n") or "\n" in head
    shown = head[:head.rfind("\n") + 1] if cut_ok else head
    return {"source_id": source_id, "path": path,
            "start_line": 1, "end_line": shown.count("\n"),
            "truncated": True,
            "note": ("head excerpt; the window covers ONLY these lines -- "
                     "the rest of the file is unseen"),
            "content": shown}


def build_brief(goal, file_paths, *, reader=None,
                max_total_chars=MAX_TOTAL_WINDOW_CHARS):
    """Build the grounded context pack. Every window is cited to a source
    whose sha256 pins the exact content it came from; the pack itself
    asserts nothing beyond the goal."""
    if not goal or not str(goal).strip():
        raise HarnessError("brief requires a goal")
    read = reader or _default_reader
    sources: List[Dict] = []
    windows: List[Dict] = []
    remaining = max_total_chars
    for i, path in enumerate(file_paths or []):
        content = read(path)
        source_id = f"s{i + 1}"
        sources.append({"id": source_id, "path": path,
                        "sha256": _sha256(content),
                        "lines": content.count("\n") + 1})
        if remaining <= 0:
            continue
        window = _window(source_id, path, content, remaining)
        windows.append(window)
        remaining -= len(window["content"])
    return {
        "schema_version": BRIEF_SCHEMA_VERSION,
        "goal": str(goal).strip(),
        "grounding": {
            "rules": GROUNDING_RULES,
            "sources": sources,
            "claims": [],
            "unknowns": [],
        },
        "windows": windows,
    }


def _default_reader(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def validate_brief(pack, *, reader=None):
    """Grounding lint (MR-8): every window cites a real source and matches
    its pinned sha256 over the bytes shown; every claim cites existing
    sources. Returns the list of issues (empty = valid)."""
    issues = []
    if not isinstance(pack, dict):
        return ["brief must be a JSON object"]
    grounding = pack.get("grounding") or {}
    sources = {s.get("id"): s for s in (grounding.get("sources") or [])}
    read = reader or _default_reader
    for window in pack.get("windows") or []:
        source = sources.get(window.get("source_id"))
        if source is None:
            issues.append(
                f"window cites unknown source {window.get('source_id')!r}")
            continue
        shown = window.get("content") or ""
        pinned = source.get("sha256")
        # The window must be a span of the pinned source content.
        try:
            content = read(source.get("path"))
        except OSError as exc:
            issues.append(f"source {source.get('id')} unreadable: {exc}")
            continue
        if _sha256(content) != pinned:
            issues.append(
                f"source {source.get('id')} content hash drifted from its "
                "pinned sha256")
            continue
        if content.find(shown) == -1:
            issues.append(
                f"window for {source.get('id')} is not a span of the pinned "
                "source content")
    for claim in grounding.get("claims") or []:
        if not claim.get("source_ids"):
            issues.append(f"uncited claim is invalid: {claim.get('text')!r}")
            continue
        for source_id in claim["source_ids"]:
            if source_id not in sources:
                issues.append(
                    f"claim cites unknown source {source_id!r}")
    return issues
