"""The evidence-bearing context brief contract (Hourglass `HV-2`).

One owner for the portable brief artifact: a versioned, bounded JSON object
carrying the goal, hash-pinned source identity and freshness, scope coverage,
evidence references that ground each claim, explicit uncertainties and
conflicts, an honest token estimate, and a visible record of every omission
and truncation. Create, validate, and render are independent, so a later stage
can be handed a brief without running context intake again.

Extraction belongs to `harness.condenser` (AST signatures, heuristic
declarations, error-log condensation), which also keeps the legacy
`MicroBrief` path; this module owns only the artifact. Downstream phases bind
to the names published here -- `create_context_brief`, `validate_context_brief`,
`render_context_brief`, `CONTEXT_BRIEF_SCHEMA_VERSION`, `CONTEXT_BRIEF_STAGES` --
instead of reaching into the condenser.

Hermetic by construction: supplied text in, JSON brief out. No filesystem, no
network, no key, and no model call; validation never re-runs intake.
"""
import hashlib
import json
from datetime import datetime, timezone
from typing import Mapping

from .condenser import condense_error_log, extract_source_evidence
from .errors import HarnessError
from .tokens import estimate_prompt_tokens

CONTEXT_BRIEF_SCHEMA_VERSION = 1
CONTEXT_BRIEF_STAGES = ("context", "planning", "execution", "verification")
_CONTEXT_ERROR_PATH = "context:error_log"
_CONTEXT_EVIDENCE_KINDS = ("interface_signature", "failure_trace")
_CONTEXT_TRUNCATION_MARKER = "\n[… truncated; lower-priority evidence omitted]"
# An excluded source is either inspected and dropped by the budget, or never
# yielded extractable evidence at all; the two gaps are different to a reader,
# so the reason strings are the contract the validator checks, not prose.
_CONTEXT_OMISSION_REASONS = {
    "budget": "source evidence omitted to fit max_tokens",
    "no_evidence": "no extractable interface or failure evidence",
}
_CONTEXT_BRIEF_FIELDS = {
    "schema_version", "stage", "goal", "grounded_claims", "evidence",
    "included_scope", "excluded_scope", "coverage", "source_identity",
    "uncertainties", "conflicts", "omissions", "token_estimate",
}


def _canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _brief_json(brief):
    """Portable JSON rendering; its size is the artifact's token estimate."""
    return json.dumps(brief, sort_keys=True, indent=2, allow_nan=False)


def _manifest_digest(sources):
    """Pin only the source catalog fields that define content identity."""
    rows = sorted(
        ({"id": source["id"], "path": source["path"], "sha256": source["sha256"]}
         for source in sources),
        key=lambda row: row["id"],
    )
    return _sha256_text(_canonical_json(rows))


def _known_text_list(values, field):
    """Normalize a caller-supplied uncertainty or conflict list."""
    if values is None:
        return []
    if not isinstance(values, (list, tuple)):
        raise HarnessError(f"{field} must be a list of non-empty strings")
    result = []
    for index, value in enumerate(values):
        if not isinstance(value, str) or not value.strip():
            raise HarnessError(f"{field}[{index}] must be a non-empty string")
        result.append(value.strip())
    return result


def _sha256_text(value):
    return hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()


def _source_id(path, digest):
    payload = _canonical_json({"path": path, "sha256": digest})
    return "src:" + _sha256_text(payload)


def _evidence_id(source_id, kind):
    return "ev:" + _sha256_text(source_id + "\0" + kind)


def _timestamp(value, field):
    """Canonicalize a timezone-aware timestamp without inventing freshness."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise HarnessError(f"{field} must be an ISO-8601 timestamp") from None
    else:
        raise HarnessError(f"{field} must be an ISO-8601 timestamp")
    if parsed.tzinfo is None:
        raise HarnessError(f"{field} must include a timezone")
    try:
        offset = parsed.utcoffset()
    except (OverflowError, TypeError, ValueError):
        raise HarnessError(f"{field} must be a valid timezone-aware timestamp") from None
    if offset is None:
        raise HarnessError(f"{field} must include a timezone")
    try:
        canonical = parsed.astimezone(timezone.utc)
    except (OverflowError, TypeError, ValueError):
        raise HarnessError(f"{field} must be a valid timezone-aware timestamp") from None
    timespec = "microseconds" if canonical.microsecond else "seconds"
    return canonical.isoformat(timespec=timespec).replace("+00:00", "Z")


def _set_token_estimate(brief):
    """Estimate the exact rendered artifact, including its estimate label."""
    value = 0
    for _ in range(16):
        brief["token_estimate"] = {"value": value, "kind": "estimated"}
        updated = estimate_prompt_tokens(_brief_json(brief))
        if updated == value:
            return value
        value = updated
    raise HarnessError("brief token estimate did not converge")


def _excerpt_evidence(text, max_chars):
    """Retain declaration lines first, then visibly mark lower-priority loss."""
    if max_chars <= len(_CONTEXT_TRUNCATION_MARKER):
        return None
    budget = max_chars - len(_CONTEXT_TRUNCATION_MARKER)
    selected = []
    used = 0
    for line in text.splitlines():
        stripped = line.strip()
        important = (
            stripped.startswith(("class ", "def ", "async def ", "fn ",
                                 "pub fn ", "function ", "interface "))
            or ("=" in stripped and stripped.split("=", 1)[0].strip().isupper())
        )
        if not important:
            continue
        cost = len(line) + (1 if selected else 0)
        if used + cost <= budget:
            selected.append(line)
            used += cost
        elif not selected and budget:
            selected.append(line[:budget])
            break
    if not selected and budget:
        selected.append(text[:budget])
    excerpt = "\n".join(selected).rstrip() + _CONTEXT_TRUNCATION_MARKER
    return excerpt if selected else None


def _normalise_claims(claims, candidates_by_id, evidence_by_path):
    """Resolve source paths to stable references and reject unsupported claims."""
    result = []
    if claims is None:
        claims = []
    if not isinstance(claims, (list, tuple)):
        raise HarnessError("claims must be a list of grounded claim objects")
    for index, claim in enumerate(claims):
        if not isinstance(claim, Mapping):
            raise HarnessError(f"claim {index} must be an object")
        if set(claim) - {"text", "evidence_refs", "source_paths"}:
            raise HarnessError(f"claim {index} has unsupported fields")
        text = claim.get("text")
        if not isinstance(text, str) or not text.strip():
            raise HarnessError(f"claim {index} requires non-empty text")
        raw_refs = claim.get("evidence_refs") or []
        source_paths = claim.get("source_paths") or []
        if (not isinstance(raw_refs, (list, tuple))
                or not isinstance(source_paths, (list, tuple))):
            raise HarnessError(f"claim {index} evidence_refs and source_paths must be lists")
        refs = list(raw_refs)
        for path in source_paths:
            if not isinstance(path, str):
                raise HarnessError(f"claim {index} source paths must be strings")
            ref = evidence_by_path.get(path)
            if ref is None:
                raise HarnessError(f"claim {index} source path has no extractable evidence: {path!r}")
            refs.append(ref)
        if any(not isinstance(ref, str) for ref in refs):
            raise HarnessError(f"claim {index} evidence references must be strings")
        refs = sorted(set(refs))
        if not refs:
            raise HarnessError(f"claim {index} requires at least one evidence reference")
        if any(not isinstance(ref, str) or ref not in candidates_by_id for ref in refs):
            raise HarnessError(f"claim {index} contains an unknown evidence reference")
        if any(candidates_by_id[ref]["truncated"] for ref in refs):
            raise HarnessError(f"claim {index} cannot cite condensed or truncated source evidence")
        result.append({"text": text.strip(), "evidence_refs": refs})
    return result


def _assemble_context_brief(goal, stage, source_identity, candidates, selected,
                            claims, uncertainties, conflicts):
    truncated_ids = sorted(
        evidence_id for evidence_id, item in selected.items() if item["truncated"]
    )
    included_ids = sorted({item["source_id"] for item in selected.values()})
    all_source_ids = {source["id"] for source in source_identity["sources"]}
    excluded_ids = sorted(all_source_ids - set(included_ids))
    by_source = {item["source_id"]: item for item in candidates}
    excluded_scope = []
    omissions = []
    for source_id in excluded_ids:
        if source_id in by_source:
            reason = _CONTEXT_OMISSION_REASONS["budget"]
        else:
            reason = _CONTEXT_OMISSION_REASONS["no_evidence"]
        excluded_scope.append({"source_id": source_id, "reason": reason})
        omissions.append({"kind": "source", "ref": source_id, "reason": reason})

    evidence = []
    for evidence_id in sorted(selected):
        item = selected[evidence_id]
        evidence.append({
            "id": evidence_id,
            "source_id": item["source_id"],
            "kind": item["kind"],
            "content": item["content"],
            "sha256": _sha256_text(item["content"]),
            "truncated": item["truncated"],
            "decision_critical": item["decision_critical"],
        })
        omitted_parts = []
        if item["kind"] == "interface_signature":
            omitted_parts.append(
                "selected interface/signature lines retained; source bodies and other text omitted"
            )
        elif item["kind"] == "failure_trace":
            omitted_parts.append(
                "condensed error-log excerpt retained; surrounding context may be omitted"
            )
        if item["truncated"]:
            omitted_parts.append("lower-priority evidence text was truncated")
        if omitted_parts:
            omissions.append({
                "kind": "evidence", "ref": evidence_id,
                "reason": "; ".join(omitted_parts),
            })

    complete = not excluded_ids and not truncated_ids
    return {
        "schema_version": CONTEXT_BRIEF_SCHEMA_VERSION,
        "stage": stage,
        "goal": goal,
        "grounded_claims": list(claims),
        "evidence": evidence,
        "included_scope": included_ids,
        "excluded_scope": excluded_scope,
        "coverage": {
            "requested_sources": len(source_identity["sources"]),
            # Sources extraction actually produced evidence for; the
            # remainder were inspected but yielded nothing extractable.
            "inspected_sources": len(by_source),
            "included_source_ids": included_ids,
            "excluded_source_ids": excluded_ids,
            "truncated_evidence_ids": truncated_ids,
            "complete": complete,
        },
        "source_identity": source_identity,
        "uncertainties": list(uncertainties),
        "conflicts": list(conflicts),
        "omissions": omissions,
        "token_estimate": {"value": 0, "kind": "estimated"},
    }


def _validate_context_brief_shape(brief):
    """Validate the portable schema without intake, I/O, or network access."""
    if not isinstance(brief, dict):
        return ["brief must be a JSON object"]
    try:
        _brief_json(brief)
    except (TypeError, ValueError, OverflowError, RecursionError):
        return ["brief must contain JSON-compatible values"]
    if any(not isinstance(key, str) for key in brief):
        return ["brief object keys must be strings"]
    issues = []
    missing = _CONTEXT_BRIEF_FIELDS - set(brief)
    extra = set(brief) - _CONTEXT_BRIEF_FIELDS
    if missing:
        issues.append("brief is missing required fields: " + ", ".join(sorted(missing)))
    if extra:
        issues.append("brief has unsupported fields: " + ", ".join(sorted(extra)))
    if missing:
        return issues

    version = brief["schema_version"]
    if not isinstance(version, int) or isinstance(version, bool) or version != CONTEXT_BRIEF_SCHEMA_VERSION:
        issues.append(f"schema_version must be {CONTEXT_BRIEF_SCHEMA_VERSION}")
    if not isinstance(brief["stage"], str) or brief["stage"] not in CONTEXT_BRIEF_STAGES:
        issues.append("stage must be one of " + ", ".join(CONTEXT_BRIEF_STAGES))
    if not isinstance(brief["goal"], str) or not brief["goal"].strip():
        issues.append("goal must be a non-empty string")

    identity = brief["source_identity"]
    source_ids, source_paths = set(), set()
    if not isinstance(identity, dict) or set(identity) != {
            "observed_at", "manifest_sha256", "sources"}:
        issues.append("source_identity must contain observed_at, manifest_sha256, and sources only")
        identity = {}
    try:
        observed_at = identity.get("observed_at")
        if _timestamp(observed_at, "source_identity.observed_at") != observed_at:
            issues.append("source_identity.observed_at must be canonical UTC")
    except HarnessError as exc:
        issues.append(str(exc))
    digest = identity.get("manifest_sha256")
    if not isinstance(digest, str) or len(digest) != 64 or any(
            char not in "0123456789abcdef" for char in digest):
        issues.append("source_identity.manifest_sha256 must be a SHA-256 hex digest")
    sources = identity.get("sources")
    if not isinstance(sources, list):
        issues.append("source_identity.sources must be a list")
        sources = []
    for index, source in enumerate(sources):
        fields = {"id", "path", "sha256", "source_modified_at", "freshness"}
        if not isinstance(source, dict) or set(source) != fields:
            issues.append(f"source_identity.sources[{index}] has an invalid schema")
            continue
        source_id, path, content_hash = source["id"], source["path"], source["sha256"]
        if not isinstance(path, str) or not path.strip():
            issues.append(f"source_identity.sources[{index}].path must be non-empty")
            continue
        if not isinstance(content_hash, str) or len(content_hash) != 64 or any(
                char not in "0123456789abcdef" for char in content_hash):
            issues.append(f"source_identity.sources[{index}].sha256 must be a SHA-256 hex digest")
            continue
        if not isinstance(source_id, str) or source_id != _source_id(path, content_hash):
            issues.append(f"source_identity.sources[{index}].id does not match path and content hash")
            continue
        if index:
            previous = sources[index - 1]
            previous_id = previous.get("id") if isinstance(previous, dict) else None
            if isinstance(previous_id, str) and previous_id > source_id:
                issues.append("source_identity.sources must be sorted by source id")
        if source_id in source_ids or path in source_paths:
            issues.append(f"duplicate source identity or path at source index {index}")
        source_ids.add(source_id)
        source_paths.add(path)
        modified = source["source_modified_at"]
        if modified is not None:
            try:
                canonical = _timestamp(modified, f"source_identity.sources[{index}].source_modified_at")
                if canonical != modified:
                    issues.append(f"source_identity.sources[{index}].source_modified_at is not canonical UTC")
            except HarnessError as exc:
                issues.append(str(exc))
        expected_freshness = "source_timestamp_available" if modified is not None else "capture_time_only"
        if source["freshness"] != expected_freshness:
            issues.append(f"source_identity.sources[{index}].freshness contradicts its timestamp")
    if all(
        isinstance(row, dict)
        and all(isinstance(row.get(field), str) for field in ("id", "path", "sha256"))
        for row in sources
    ):
        if digest != _manifest_digest(sources):
            issues.append("source_identity.manifest_sha256 does not match the source catalog")

    claims = brief["grounded_claims"]
    if not isinstance(claims, list):
        issues.append("grounded_claims must be a list")
        claims = []
    for index, claim in enumerate(claims):
        if not isinstance(claim, dict) or set(claim) != {"text", "evidence_refs"}:
            issues.append(f"grounded_claims[{index}] must contain text and evidence_refs only")
            continue
        refs = claim["evidence_refs"]
        if (not isinstance(claim["text"], str) or not claim["text"].strip()
                or not isinstance(refs, list) or not refs
                or any(not isinstance(ref, str) for ref in refs)):
            issues.append(f"grounded_claims[{index}] requires text and evidence references")
        elif len(set(refs)) != len(refs):
            issues.append(f"grounded_claims[{index}] contains duplicate evidence references")

    evidence = brief["evidence"]
    if not isinstance(evidence, list):
        issues.append("evidence must be a list")
        evidence = []
    evidence_ids, evidence_sources, truncated_ids = set(), set(), set()
    evidence_by_id = {}
    for index, item in enumerate(evidence):
        fields = {"id", "source_id", "kind", "content", "sha256", "truncated", "decision_critical"}
        if not isinstance(item, dict) or set(item) != fields:
            issues.append(f"evidence[{index}] has an invalid schema")
            continue
        evidence_id, source_id, kind = item["id"], item["source_id"], item["kind"]
        content, content_hash = item["content"], item["sha256"]
        source_known = isinstance(source_id, str) and source_id in source_ids
        if not source_known:
            issues.append(f"evidence[{index}] references an unknown source")
        elif isinstance(kind, str):
            if evidence_id != _evidence_id(source_id, kind):
                issues.append(f"evidence[{index}].id does not match its source and kind")
            else:
                evidence_sources.add(source_id)
        if not isinstance(kind, str) or kind not in _CONTEXT_EVIDENCE_KINDS:
            issues.append(f"evidence[{index}].kind is unsupported")
        if not isinstance(evidence_id, str) or not evidence_id.startswith("ev:"):
            issues.append(f"evidence[{index}].id must be an evidence identifier")
        if not isinstance(content, str) or not content.strip():
            issues.append(f"evidence[{index}].content must be non-empty")
        else:
            if not isinstance(content_hash, str) or content_hash != _sha256_text(content):
                issues.append(f"evidence[{index}].sha256 does not match its content")
            if (item["truncated"] is True
                    and not content.endswith(_CONTEXT_TRUNCATION_MARKER)):
                issues.append(f"evidence[{index}] marked truncated must end with the truncation marker")
        if not isinstance(item["truncated"], bool):
            issues.append(f"evidence[{index}].truncated must be a boolean")
        elif item["truncated"] and isinstance(evidence_id, str):
            truncated_ids.add(evidence_id)
        if not isinstance(item["decision_critical"], bool):
            issues.append(f"evidence[{index}].decision_critical must be a boolean")
        if not isinstance(evidence_id, str) or evidence_id in evidence_ids:
            issues.append(f"evidence[{index}] has a duplicate or invalid evidence id")
        else:
            evidence_ids.add(evidence_id)
            evidence_by_id[evidence_id] = item

    claimed_evidence_ids = set()
    for index, claim in enumerate(claims):
        if not isinstance(claim, dict) or not isinstance(claim.get("evidence_refs"), list):
            continue
        for ref in claim["evidence_refs"]:
            if not isinstance(ref, str) or ref not in evidence_by_id:
                issues.append(f"grounded_claims[{index}] references unknown evidence {ref!r}")
            elif evidence_by_id[ref].get("truncated"):
                issues.append(f"grounded_claims[{index}] cites truncated evidence {ref!r}")
            else:
                claimed_evidence_ids.add(ref)
                if not evidence_by_id[ref].get("decision_critical"):
                    issues.append(f"grounded_claims[{index}] evidence {ref!r} is not marked decision-critical")
    for evidence_id, item in evidence_by_id.items():
        if item.get("decision_critical") != (evidence_id in claimed_evidence_ids):
            issues.append(f"evidence {evidence_id!r} has an inconsistent decision-critical flag")

    included = brief["included_scope"]
    if not isinstance(included, list) or any(not isinstance(ref, str) for ref in included):
        issues.append("included_scope must be a list of source ids")
        included = []
    included_ids = set(included)
    if len(included_ids) != len(included):
        issues.append("included_scope contains duplicate source ids")
    excluded = brief["excluded_scope"]
    if not isinstance(excluded, list):
        issues.append("excluded_scope must be a list")
        excluded = []
    excluded_ids = set()
    no_evidence_ids = set()
    for index, item in enumerate(excluded):
        if (not isinstance(item, dict) or set(item) != {"source_id", "reason"}
                or not isinstance(item.get("source_id"), str)
                or not isinstance(item.get("reason"), str) or not item["reason"].strip()):
            issues.append(f"excluded_scope[{index}] must contain a source id and reason")
            continue
        if item["source_id"] in excluded_ids:
            issues.append(f"excluded_scope contains duplicate source id {item['source_id']!r}")
        excluded_ids.add(item["source_id"])
        if item["reason"] == _CONTEXT_OMISSION_REASONS["no_evidence"]:
            no_evidence_ids.add(item["source_id"])
    if included_ids & excluded_ids or included_ids | excluded_ids != source_ids:
        issues.append("included_scope and excluded_scope must partition the source catalog")
    if included_ids != evidence_sources:
        issues.append("included_scope must match the sources with retained evidence")

    coverage = brief["coverage"]
    coverage_fields = {"requested_sources", "inspected_sources", "included_source_ids",
                       "excluded_source_ids", "truncated_evidence_ids", "complete"}
    if not isinstance(coverage, dict) or set(coverage) != coverage_fields:
        issues.append("coverage has an invalid schema")
    else:
        counts_ok = all(isinstance(coverage[key], int) and not isinstance(coverage[key], bool)
                        for key in ("requested_sources", "inspected_sources"))
        if not counts_ok or coverage["requested_sources"] != len(source_ids):
            issues.append("coverage.requested_sources does not match the source catalog")
        elif coverage["inspected_sources"] != len(source_ids) - len(no_evidence_ids):
            issues.append("coverage.inspected_sources does not match the sources that yielded evidence")
        if coverage["included_source_ids"] != sorted(included_ids):
            issues.append("coverage.included_source_ids does not match included_scope")
        if coverage["excluded_source_ids"] != sorted(excluded_ids):
            issues.append("coverage.excluded_source_ids does not match excluded_scope")
        if coverage["truncated_evidence_ids"] != sorted(truncated_ids):
            issues.append("coverage.truncated_evidence_ids does not match evidence")
        if not isinstance(coverage["complete"], bool):
            issues.append("coverage.complete must be a boolean")

    for field in ("uncertainties", "conflicts"):
        values = brief[field]
        if not isinstance(values, list) or any(not isinstance(item, str) or not item.strip() for item in values):
            issues.append(f"{field} must be a list of non-empty strings")

    omissions = brief["omissions"]
    if not isinstance(omissions, list):
        issues.append("omissions must be a list")
        omissions = []
    omission_keys = set()
    for index, item in enumerate(omissions):
        fields = {"kind", "ref", "reason"}
        if (not isinstance(item, dict) or set(item) != fields
                or item.get("kind") not in ("source", "evidence")
                or not isinstance(item.get("ref"), str) or not item["ref"]
                or not isinstance(item.get("reason"), str) or not item["reason"].strip()):
            issues.append(f"omissions[{index}] must contain kind, ref, and reason")
            continue
        key = (item["kind"], item["ref"])
        known_refs = source_ids if item["kind"] == "source" else evidence_ids
        if item["ref"] not in known_refs:
            issues.append(f"omissions[{index}] references unknown {item['kind']} {item['ref']!r}")
        elif item["kind"] == "source" and item["ref"] in included_ids:
            issues.append(f"omissions[{index}] marks an included source as omitted")
        if key in omission_keys:
            issues.append(f"omissions contains duplicate record {key!r}")
        omission_keys.add(key)
    for source_id in excluded_ids:
        if ("source", source_id) not in omission_keys:
            issues.append(f"excluded source {source_id!r} lacks an omission record")
    for evidence_id in evidence_ids:
        kind = evidence_by_id[evidence_id]["kind"]
        if kind in ("interface_signature", "failure_trace") and (
                "evidence", evidence_id) not in omission_keys:
            issues.append(f"condensed evidence {evidence_id!r} lacks an omission record")
    omission_by_key = {
        (item.get("kind"), item.get("ref")): item
        for item in omissions
        if isinstance(item, dict) and isinstance(item.get("kind"), str)
        and isinstance(item.get("ref"), str)
    }
    for evidence_id in truncated_ids:
        omission = omission_by_key.get(("evidence", evidence_id))
        if omission is None:
            issues.append(f"truncated evidence {evidence_id!r} lacks an omission record")
        elif (not isinstance(omission.get("reason"), str)
              or "truncat" not in omission["reason"].lower()):
            issues.append(f"truncated evidence {evidence_id!r} lacks a truncation reason")
    if isinstance(coverage, dict) and isinstance(coverage.get("complete"), bool):
        expected_complete = not excluded_ids and not truncated_ids
        if coverage["complete"] != expected_complete:
            issues.append("coverage.complete does not reflect omissions and truncation")
    omission_keys_present = set(omission_by_key)
    if omission_keys_present != omission_keys:
        issues.append("omissions contain records without a valid kind/ref pair")

    estimate = brief["token_estimate"]
    if not isinstance(estimate, dict) or set(estimate) != {"value", "kind"}:
        issues.append("token_estimate must contain value and kind only")
    else:
        value = estimate["value"]
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            issues.append("token_estimate.value must be a positive integer")
        if estimate["kind"] != "estimated":
            issues.append("token_estimate.kind must be 'estimated', not measured")
        elif isinstance(value, int) and value != estimate_prompt_tokens(_brief_json(brief)):
            issues.append("token_estimate.value does not match the rendered estimate")
    return issues


def create_context_brief(
    goal, files, *, stage="context", error_log=None, max_tokens=1500,
    focus_symbols=None, claims=None, uncertainties=None, conflicts=None,
    source_modified_at=None, observed_at=None,
):
    """Build a bounded context artifact with hash-pinned, grounded evidence.

    Claims may reference evidence ids or source paths. Claim-cited evidence is
    kept whole and selected before other evidence; if it cannot fit, creation
    fails rather than silently dropping the decision-critical support.

    `stage` declares which Hourglass stage this brief is minted for; a partial
    run states the stages it selected instead of implying all of them ran.
    """
    if not isinstance(goal, str) or not goal.strip():
        raise HarnessError("context brief requires a non-empty goal")
    if not isinstance(stage, str) or stage not in CONTEXT_BRIEF_STAGES:
        raise HarnessError("stage must be one of " + ", ".join(CONTEXT_BRIEF_STAGES))
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens <= 0:
        raise HarnessError("max_tokens must be a positive integer")
    if not isinstance(files, Mapping):
        raise HarnessError("files must map source paths to text")
    if error_log is not None and not isinstance(error_log, str):
        raise HarnessError("error_log must be text")
    if focus_symbols is not None:
        if isinstance(focus_symbols, str):
            raise HarnessError("focus_symbols must be a sequence of symbol names, not text")
        try:
            focus_symbols = tuple(focus_symbols)
        except TypeError:
            raise HarnessError("focus_symbols must be a sequence of symbol names") from None
        if any(not isinstance(symbol, str) or not symbol for symbol in focus_symbols):
            raise HarnessError("focus_symbols must contain non-empty strings")
    captured = _timestamp(
        observed_at if observed_at is not None else datetime.now(timezone.utc),
        "observed_at",
    )
    modified_times = {} if source_modified_at is None else source_modified_at
    if not isinstance(modified_times, Mapping):
        raise HarnessError("source_modified_at must map paths to timestamps")
    if any(not isinstance(path, str) for path in modified_times):
        raise HarnessError("source_modified_at keys must be source paths")

    entries = list(files.items())
    for path, content in entries:
        if not isinstance(path, str) or not path.strip() or not isinstance(content, str):
            raise HarnessError("source paths must be non-empty strings and contents must be text")
        if path == _CONTEXT_ERROR_PATH:
            raise HarnessError(f"source path {_CONTEXT_ERROR_PATH!r} is reserved")

    inputs = []
    for path, content in sorted(entries, key=lambda item: item[0]):
        inputs.append((path, content, False))
    if error_log and error_log.strip():
        inputs.append((_CONTEXT_ERROR_PATH, error_log, True))
    valid_paths = {path for path, _content, _is_error in inputs}
    if set(modified_times) - valid_paths:
        raise HarnessError("source_modified_at contains a path outside the supplied scope")

    sources = []
    source_by_path = {}
    for path, content, _is_error in inputs:
        modified = modified_times.get(path)
        modified = _timestamp(modified, f"source_modified_at[{path!r}]") if modified is not None else None
        digest = _sha256_text(content)
        source = {
            "id": _source_id(path, digest), "path": path, "sha256": digest,
            "source_modified_at": modified,
            "freshness": "source_timestamp_available" if modified is not None else "capture_time_only",
        }
        sources.append(source)
        source_by_path[path] = source
    sources.sort(key=lambda item: item["id"])
    source_identity = {
        "observed_at": captured,
        "manifest_sha256": _manifest_digest(sources),
        "sources": sources,
    }

    candidates, evidence_by_path = [], {}
    for path, content, is_error in inputs:
        if is_error:
            evidence_text = condense_error_log(content)
            already_truncated = evidence_text != content.strip()
            kind = "failure_trace"
        else:
            evidence_text, already_truncated = extract_source_evidence(
                path, content, focus_symbols=focus_symbols)
            kind = "interface_signature"
        if already_truncated:
            evidence_text = evidence_text.rstrip() + _CONTEXT_TRUNCATION_MARKER
        if not evidence_text.strip():
            continue
        source_id = source_by_path[path]["id"]
        candidate = {
            "id": _evidence_id(source_id, kind), "source_id": source_id,
            "path": path, "kind": kind, "content": evidence_text,
            "truncated": already_truncated, "decision_critical": False,
        }
        candidates.append(candidate)
        evidence_by_path[path] = candidate["id"]
    candidates_by_id = {item["id"]: item for item in candidates}
    normalised_claims = _normalise_claims(claims, candidates_by_id, evidence_by_path)
    critical_ids = {ref for claim in normalised_claims for ref in claim["evidence_refs"]}
    for item in candidates:
        item["decision_critical"] = item["id"] in critical_ids
    candidates.sort(key=lambda item: (
        not item["decision_critical"], item["kind"] != "failure_trace", item["path"]
    ))

    uncertainty_list = _known_text_list(uncertainties, "uncertainties")
    conflict_list = _known_text_list(conflicts, "conflicts")
    selected = {}
    empty = _assemble_context_brief(
        goal.strip(), stage, source_identity, candidates, selected,
        normalised_claims, uncertainty_list, conflict_list,
    )
    if _set_token_estimate(empty) > max_tokens:
        raise HarnessError("brief metadata exceeds max_tokens; narrow scope or raise the bound")

    def trial(candidate, text, truncated):
        current = dict(candidate, content=text, truncated=truncated)
        proposal = dict(selected)
        proposal[candidate["id"]] = current
        brief = _assemble_context_brief(
            goal.strip(), stage, source_identity, candidates, proposal,
            normalised_claims, uncertainty_list, conflict_list,
        )
        return _set_token_estimate(brief), current

    for candidate in candidates:
        if candidate["decision_critical"] and candidate["truncated"]:
            raise HarnessError("decision-critical evidence is already condensed or truncated")
        full_estimate, full = trial(candidate, candidate["content"], candidate["truncated"])
        if full_estimate <= max_tokens:
            selected[candidate["id"]] = full
            continue
        if candidate["decision_critical"]:
            raise HarnessError("decision-critical evidence does not fit max_tokens")

        low, high, best = 1, len(candidate["content"]) - 1, None
        while low <= high:
            char_limit = (low + high) // 2
            excerpt = _excerpt_evidence(candidate["content"], char_limit)
            if excerpt is None:
                low = char_limit + 1
                continue
            estimate, partial = trial(candidate, excerpt, True)
            if estimate <= max_tokens:
                best = partial
                low = char_limit + 1
            else:
                high = char_limit - 1
        if best is not None:
            selected[candidate["id"]] = best

    result = _assemble_context_brief(
        goal.strip(), stage, source_identity, candidates, selected,
        normalised_claims, uncertainty_list, conflict_list,
    )
    estimate = _set_token_estimate(result)
    if estimate > max_tokens:
        raise HarnessError("brief metadata exceeds max_tokens; narrow scope or raise the bound")
    issues = _validate_context_brief_shape(result)
    if issues:
        raise HarnessError("created invalid context brief: " + "; ".join(issues))
    return result


def validate_context_brief(brief, *, max_tokens=None, source_contents=None):
    """Validate a supplied brief offline; optionally compare current source hashes."""
    issues = _validate_context_brief_shape(brief)
    if not isinstance(brief, dict):
        return issues

    if max_tokens is not None:
        if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens <= 0:
            issues.append("max_tokens must be a positive integer")
        elif isinstance(brief.get("token_estimate"), dict):
            value = brief["token_estimate"].get("value")
            if isinstance(value, int) and value > max_tokens:
                issues.append(f"brief estimate {value} exceeds max_tokens {max_tokens}")
    if source_contents is not None:
        if not isinstance(source_contents, Mapping):
            issues.append("source_contents must map source paths to text")
        elif any(not isinstance(path, str) or not isinstance(content, str)
                 for path, content in source_contents.items()):
            issues.append("source_contents paths and values must be strings")
        elif isinstance(brief.get("source_identity"), dict):
            sources = brief["source_identity"].get("sources")
            if isinstance(sources, list):
                for source in sources:
                    if not isinstance(source, dict):
                        continue
                    path = source.get("path")
                    if not isinstance(path, str):
                        continue
                    if path not in source_contents:
                        issues.append(f"source {path!r} was not supplied for freshness validation")
                    elif (not isinstance(source_contents[path], str)
                          or _sha256_text(source_contents[path]) != source.get("sha256")):
                        issues.append(f"source {path!r} content hash drifted")
    return issues


def render_context_brief(brief, *, max_tokens=None):
    """Render a validated supplied brief without running context intake."""
    issues = validate_context_brief(brief, max_tokens=max_tokens)
    if issues:
        raise HarnessError("cannot render invalid context brief: " + "; ".join(issues))
    return _brief_json(brief)
