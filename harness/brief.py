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
import json
import time
from typing import Dict, List

from .errors import HarnessError
from .tokens import estimate_prompt_tokens

BRIEF_SCHEMA_VERSION = 2
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


# --------------------------------------------------------------------------
# HV-2: an evidence-bearing brief.
#
# v1 produced a grounded pack of cited windows.  The Hourglass contract also
# needs the brief to be honest about FOUR more things, none of which a
# generative pass may invent:
#
#   freshness   -- which sources still match their pinned hash, and when the
#                  pack was built (a consumer can tell stale from current);
#   coverage    -- which declared scope items are actually represented, and
#                  which were omitted, so "omission is visible" is a fact in
#                  the artifact rather than a hope;
#   uncertainty -- explicit unknowns AND conflicts between sources, both
#                  required to cite the sources they came from;
#   size        -- an estimated token count measured on the bytes actually
#                  shipped, so a downstream token budget can be preflighted.
#
# Every field is additive and code-owned.  ``brief`` is a separate owner from
# Jev policy: this module never calls the network and never asks a model.
# --------------------------------------------------------------------------


def _source_identity(path, content, source_id, *, now=None):
    """A cited source's identity plus its freshness observation.

    The pinned sha256 is the authority; ``observed_at`` and ``bytes`` are
    recorded so a consumer can tell how old the observation is, and
    ``freshness`` reports whether the bytes still hash to the pin.
    """
    stamp = now if now is not None else time.time()
    return {
        "id": source_id,
        "path": path,
        "sha256": _sha256(content),
        "lines": content.count("\n") + 1,
        "bytes": len(content.encode("utf-8")),
        "observed_at": round(float(stamp), 3),
    }


def freshness_report(pack, *, reader=None, now=None):
    """Re-check every cited source against its pinned hash (HV-2).

    Returns a typed report; never raises on a drifted or missing file, because
    "this evidence went stale" is a finding a consumer must be able to read,
    not a crash.

    ``fresh`` is true only when every source still matches its pin.  A brief
    with no sources at all reports ``fresh=False`` with an explicit reason,
    never a vacuous pass.
    """
    if not isinstance(pack, dict):
        return {"fresh": False, "checked": 0, "stale": [], "missing": [],
                "reasons": ["brief must be a JSON object"]}
    read = reader or _default_reader
    sources = (pack.get("grounding") or {}).get("sources") or []
    stale, missing, reasons = [], [], []
    if not sources:
        return {"fresh": False, "checked": 0, "stale": [], "missing": [],
                "reasons": ["brief cites no sources, so freshness is unknown"]}
    for source in sources:
        source_id = source.get("id")
        try:
            content = read(source.get("path"))
        except OSError as exc:
            missing.append({"id": source_id, "path": source.get("path"),
                            "error": type(exc).__name__})
            continue
        if _sha256(content) != source.get("sha256"):
            stale.append({"id": source_id, "path": source.get("path")})
    if missing:
        reasons.append("{0} cited source(s) could not be read".format(len(missing)))
    if stale:
        reasons.append(
            "{0} cited source(s) no longer match their pinned sha256".format(
                len(stale)))
    return {"fresh": not stale and not missing, "checked": len(sources),
            "stale": stale, "missing": missing, "reasons": reasons,
            "built_at": pack.get("built_at")}


def estimate_brief_tokens(pack):
    """Estimated token count over the bytes this pack actually ships.

    Measured on the serialized pack, not estimated from the sources, so the
    number a token budget is preflighted against is the number it will pay.
    """
    try:
        return estimate_prompt_tokens(json.dumps(pack, sort_keys=True))
    except (TypeError, ValueError):
        return 0


def render_brief(pack):
    """Render a brief for a human or a downstream seat -- no intake, no I/O.

    Independent of ``build_brief`` on purpose: a caller may be handed a pack
    produced elsewhere (or by an earlier run) and still get a faithful,
    honest rendering without re-reading any source.
    """
    if not isinstance(pack, dict):
        raise HarnessError("brief pack must be a JSON object")
    grounding = pack.get("grounding") or {}
    coverage = pack.get("coverage") or {}
    scope = pack.get("scope") or {}
    lines = ["# Brief — " + str(pack.get("goal") or "(no goal)"), ""]
    built_at = pack.get("built_at")
    lines.append("- schema: v{0}{1}".format(
        pack.get("schema_version"),
        "  |  built: " + str(built_at) if built_at else ""))
    lines.append("- estimated tokens: {0}".format(
        pack.get("estimated_tokens")))
    lines.append(
        "- scope: {0} included, {1} excluded".format(
            len(scope.get("included") or []),
            len(scope.get("excluded") or [])))
    if scope.get("excluded"):
        lines.append("- excluded from scope: " + ", ".join(
            str(item) for item in scope["excluded"]))
    lines.append(
        "- coverage: {0} cited source(s), {1} omitted, {2} truncated window(s)"
        .format(len(grounding.get("sources") or []),
                len(pack.get("omitted") or []),
                sum(1 for w in (pack.get("windows") or []) if w.get("truncated"))))
    if coverage.get("gaps"):
        lines.append("- coverage gaps: " + ", ".join(
            str(gap) for gap in coverage["gaps"]))
    if pack.get("omitted"):
        lines.append("- omitted (visible, not silently dropped):")
        lines.extend("  - " + str(item) for item in pack["omitted"])
    if grounding.get("unknowns"):
        lines.append("- unknowns:")
        lines.extend("  - " + str(item) for item in grounding["unknowns"])
    conflicts = pack.get("conflicts") or []
    if conflicts:
        lines.append("- conflicts between sources:")
        lines.extend("  - {0} (sources: {1})".format(
            item.get("description"), ", ".join(item.get("source_ids") or []))
            for item in conflicts)
    if pack.get("windows"):
        lines.extend(["", "## Cited windows", ""])
        for window in pack["windows"]:
            marker = " [TRUNCATED]" if window.get("truncated") else ""
            lines.append("### {0} (lines {1}-{2}){3}".format(
                window.get("path"), window.get("start_line"),
                window.get("end_line"), marker))
            lines.extend(["", "```", window.get("content") or "", "```", ""])
    return "\n".join(lines).rstrip() + "\n"


def _window(source_id, path, content, remaining):
    """One cited window: whole file when it fits, an honestly labeled head
    excerpt when it does not. The span always covers exactly the bytes
    shown, so the consumer can hash-verify the citation.

    The excerpt is bounded by BOTH the per-window cap and the caller's
    remaining budget.  Bounding only by the per-window cap let a truncated
    window exceed the run budget whenever ``remaining`` was small, which
    made ``max_total_chars`` decorative on exactly the inputs most likely to
    need it.

    Called only with a positive integer ``remaining``, so it always shows at
    least one character: a source is either represented (possibly truncated,
    always labeled) or recorded as omitted by the caller.  It never returns
    nothing, which is what lets the caller keep its omission accounting in
    one place.
    """
    budget = min(WHOLE_WINDOW_CHARS, max(remaining, 0))
    if len(content) <= budget:
        return {"source_id": source_id, "path": path,
                "start_line": 1, "end_line": content.count("\n") + 1,
                "truncated": False, "content": content}
    head = content[:min(HEAD_WINDOW_CHARS, max(remaining, 0))]
    shown = head[:head.rfind("\n") + 1] if "\n" in head else head
    return {"source_id": source_id, "path": path,
            "start_line": 1, "end_line": shown.count("\n"),
            "truncated": True,
            "note": ("head excerpt; the window covers ONLY these lines -- "
                     "the rest of the file is unseen"),
            "content": shown}


def build_brief(goal, file_paths, *, reader=None,
                max_total_chars=MAX_TOTAL_WINDOW_CHARS,
                scope=None, conflicts=None, now=None):
    """Build the evidence-bearing context pack (HV-2).

    Every window is cited to a source whose sha256 pins the exact content it
    came from, and the pack asserts nothing beyond the goal.  On top of the
    v1 grounding contract it also records, code-owned: each source's
    identity and ``observed_at``, what scope was deliberately excluded, what
    was OMITTED (never silently dropped), any declared conflicts, and an
    estimated token count over the shipped bytes.
    """
    if not goal or not str(goal).strip():
        raise HarnessError("brief requires a goal")
    read = reader or _default_reader
    stamp = now if now is not None else time.time()
    sources: List[Dict] = []
    windows: List[Dict] = []
    omitted: List[str] = []
    # Whole characters only: a fractional char budget has no honest meaning,
    # and floor() keeps the ceiling a ceiling.
    remaining = int(max_total_chars)
    for i, path in enumerate(file_paths or []):
        content = read(path)
        source_id = f"s{i + 1}"
        sources.append(_source_identity(path, content, source_id, now=stamp))
        if remaining <= 0:
            # The budget ran out. Record the omission so the consumer can see
            # that this source exists and was NOT represented.
            omitted.append(path)
            continue
        window = _window(source_id, path, content, remaining)
        windows.append(window)
        remaining -= len(window["content"])
    included = [path for path in (file_paths or []) if path not in set(omitted)]
    pack = {
        "schema_version": BRIEF_SCHEMA_VERSION,
        "goal": str(goal).strip(),
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp)),
        "grounding": {
            "rules": GROUNDING_RULES,
            "sources": sources,
            "claims": [],
            "unknowns": [],
        },
        "windows": windows,
        "scope": {
            "included": list(included),
            "excluded": list(scope or []),
        },
        "coverage": {
            "sources_declared": len(sources),
            "windows_cited": len(windows),
            "omitted": len(omitted),
            "truncated_windows": sum(
                1 for w in windows if w.get("truncated")),
            "gaps": list(omitted),
        },
        "omitted": omitted,
        "conflicts": list(conflicts or []),
    }
    pack["estimated_tokens"] = estimate_brief_tokens(pack)
    return pack


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
    issues.extend(_evidence_issues(pack, sources))
    return issues


def _evidence_issues(pack, sources):
    """HV-2 honesty checks on the evidence-bearing fields.

    These are fail-closed: a consumer that cannot verify the pack's own
    claims about coverage, conflicts, and size gets an issue, not a
    silently-trustworthy artifact.
    """
    issues = []
    version = pack.get("schema_version")
    if version != BRIEF_SCHEMA_VERSION:
        issues.append(
            f"brief schema_version {version!r} is not the declared "
            f"{BRIEF_SCHEMA_VERSION}")
    omitted = pack.get("omitted")
    if not isinstance(omitted, list):
        issues.append("brief omitted must be a list so omission stays visible")
    else:
        coverage = pack.get("coverage") or {}
        declared = coverage.get("sources_declared")
        if isinstance(declared, int) and declared != len(sources):
            issues.append(
                f"coverage.sources_declared {declared} disagrees with the "
                f"{len(sources)} cited source(s)")
        counted = coverage.get("omitted")
        if isinstance(counted, int) and counted != len(omitted):
            issues.append(
                f"coverage.omitted {counted} disagrees with the {len(omitted)} "
                "omitted path(s) actually listed")
        gaps = coverage.get("gaps")
        if isinstance(gaps, list) and sorted(str(g) for g in gaps) != sorted(
                str(o) for o in omitted):
            issues.append("coverage.gaps must match the omitted list exactly")
    for conflict in pack.get("conflicts") or []:
        if not isinstance(conflict, dict):
            issues.append("each conflict must be an object")
            continue
        if not conflict.get("description"):
            issues.append("each conflict needs a description")
        cited = conflict.get("source_ids") or []
        if not cited:
            issues.append(
                "uncited conflict is invalid: " + repr(conflict.get("description")))
            continue
        for source_id in cited:
            if source_id not in sources:
                issues.append(f"conflict cites unknown source {source_id!r}")
    tokens = pack.get("estimated_tokens")
    if (isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0):
        issues.append("brief estimated_tokens must be a non-negative integer")
    return issues
