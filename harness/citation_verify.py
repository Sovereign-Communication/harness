"""Deterministic citation identity and claim-to-source verification (#422).

The research lifecycle (#342/#344) owns retrieval and request binding.  This
module accepts only the source records that lifecycle admitted; it never
fetches by itself, so tests stay hermetic and a resolver failure is recorded
as ``not_verifiable`` rather than silently promoted.

The gate is code-owned.  A report may carry the citation-verified label only
when every bibliography item is ``supported`` and every claim is
``supported`` with an exact passage (or a hash-pinned bounded excerpt) plus a
locator.  A Jev semantic assessment may advise support, but it cannot
manufacture provenance: missing metadata, an inaccessible source, a missing
passage, or an unassessed claim stays unresolved.
"""
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Union

from .errors import HarnessError

STATUSES = ("supported", "partially_supported", "contradicted", "not_verifiable")
SOURCE_CLASSES = ("journal", "preprint", "book", "report", "web", "dataset", "other")
RETRIEVAL_STATUSES = (
    "ok", "paywalled", "unavailable", "rate_limited", "http_error", "timeout",
    "blocked",
)
MAX_PASSAGE_CHARS = 2000
_DOI_PREFIX_RE = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.I)
_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$", re.I)


def normalize_doi(value):
    """Return a canonical ``10.x/...`` DOI, or ``None`` when absent/invalid."""
    if not value:
        return None
    text = str(value).strip()
    text = _DOI_PREFIX_RE.sub("", text).strip()
    if not text or not _DOI_RE.match(text):
        return None
    return text.rstrip(".,;").lower()


def _text(value):
    return " ".join(str(value or "").split()).strip()


def _norm_field(value):
    return re.sub(r"[^a-z0-9]+", " ", _text(value).casefold()).strip()


def _norm_authors(value):
    if value is None:
        return []
    if isinstance(value, str):
        parts = re.split(r"[;,]", value)
    else:
        parts = list(value)
    out = []
    for part in parts:
        name = _norm_field(part)
        if name:
            out.append(name)
    return sorted(set(out))


def _norm_year(value):
    if value in (None, ""):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return _text(value).casefold()


def _require(value, label):
    text = _text(value)
    if not text:
        raise HarnessError(f"citation verification: {label} is required")
    return text


@dataclass
class CitationRecord:
    """One bibliography item, with the metadata the author supplied."""
    citation_id: str
    doi: Optional[str] = None
    title: Optional[str] = None
    authors: List[str] = field(default_factory=list)
    year: Optional[Any] = None
    venue: Optional[str] = None
    version: Optional[str] = None
    url: Optional[str] = None

    @classmethod
    def from_mapping(cls, raw):
        if not isinstance(raw, Mapping):
            raise HarnessError("citation verification: citation must be an object")
        cid = raw.get("citation_id") or raw.get("id")
        cid = _require(cid, "citation_id")
        doi = normalize_doi(raw.get("doi"))
        url = _text(raw.get("url"))
        if not doi and not url:
            raise HarnessError(
                f"citation verification: citation '{cid}' needs a DOI or stable URL")
        return cls(
            citation_id=cid, doi=doi, title=_text(raw.get("title")) or None,
            authors=_norm_authors(raw.get("authors")), year=raw.get("year"),
            venue=_text(raw.get("venue")) or None,
            version=_text(raw.get("version")) or None, url=url or None,
        )

    def supplied(self):
        out = {}
        if self.title:
            out["title"] = self.title
        if self.authors:
            out["authors"] = list(self.authors)
        if self.year not in (None, ""):
            out["year"] = self.year
        if self.venue:
            out["venue"] = self.venue
        if self.version:
            out["version"] = self.version
        return out


@dataclass
class SourceRecord:
    """A retrieved source admitted by the research lifecycle (#342/#344)."""
    source_id: str
    url: str
    source_class: str
    retrieved_at: str
    passage: Optional[str] = None
    locator: Optional[str] = None
    passage_sha256: Optional[str] = None
    doi: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    retrieval_status: str = "ok"
    request_id: Optional[str] = None

    @classmethod
    def from_mapping(cls, raw):
        if not isinstance(raw, Mapping):
            raise HarnessError("citation verification: source must be an object")
        sid = _require(raw.get("source_id") or raw.get("id"), "source_id")
        url = _require(raw.get("url"), f"source '{sid}' URL")
        source_class = _require(raw.get("source_class"), f"source '{sid}' source_class").lower()
        if source_class not in SOURCE_CLASSES:
            raise HarnessError(
                f"citation verification: source '{sid}' source_class "
                f"'{source_class}' is not one of {', '.join(SOURCE_CLASSES)}")
        retrieved_at = _require(raw.get("retrieved_at"), f"source '{sid}' retrieved_at")
        status = _text(raw.get("retrieval_status") or "ok").lower()
        if status not in RETRIEVAL_STATUSES:
            raise HarnessError(
                f"citation verification: source '{sid}' retrieval_status "
                f"'{status}' is not one of {', '.join(RETRIEVAL_STATUSES)}")
        metadata = raw.get("metadata") or {}
        if not isinstance(metadata, Mapping):
            raise HarnessError(f"citation verification: source '{sid}' metadata must be an object")
        return cls(
            source_id=sid, url=url, source_class=source_class,
            retrieved_at=retrieved_at, passage=raw.get("passage"),
            locator=_text(raw.get("locator")) or None,
            passage_sha256=_text(raw.get("passage_sha256")) or None,
            doi=normalize_doi(raw.get("doi") or metadata.get("doi")),
            metadata=dict(metadata), retrieval_status=status,
            request_id=_text(raw.get("request_id")) or None,
        )

    def evidence(self):
        """Return the provenance that a report is allowed to cite."""
        passage = self.passage or ""
        digest = self.passage_sha256 or hashlib.sha256(
            passage.encode("utf-8")).hexdigest()
        excerpt = passage[:MAX_PASSAGE_CHARS]
        return {
            "source_id": self.source_id,
            "url": self.url,
            "doi": self.doi,
            "source_class": self.source_class,
            "retrieved_at": self.retrieved_at,
            "request_id": self.request_id,
            "locator": self.locator,
            "passage": excerpt,
            "passage_sha256": digest,
            "passage_truncated": len(passage) > MAX_PASSAGE_CHARS,
            "retrieval_status": self.retrieval_status,
        }


Resolver = Union[None, Any]


def _resolve(resolver, doi):
    if resolver is None:
        return {"error": "no DOI resolver was supplied"}
    try:
        if hasattr(resolver, "resolve"):
            result = resolver.resolve(doi)
        else:
            result = resolver(doi)
    except Exception as exc:  # transport/parse failures are evidence, not crashes
        return {"error": f"{type(exc).__name__}: {exc}"}
    if result is None:
        return {"found": False}
    if not isinstance(result, Mapping):
        return {"error": "resolver returned a non-object result"}
    return dict(result)


def verify_citation(citation, resolver=None):
    """Compare supplied metadata against one resolved DOI record.

    The resolver contract is either a callable ``resolver(doi)`` or an object
    with ``resolve(doi)``.  It returns ``{"found": bool, "doi": str,
    "metadata": {...}, "metadata_source": str, "conflict": bool,
    "status": str, "error": str}``.  No network is performed here.
    """
    record = citation if isinstance(citation, CitationRecord) else CitationRecord.from_mapping(citation)
    supplied = record.supplied()
    out = {
        "citation_id": record.citation_id,
        "supplied_doi": record.doi,
        "canonical_doi": record.doi,
        "url": record.url,
        "status": "not_verifiable",
        "reasons": [],
        "checks": {},
        "metadata_source": "none",
    }
    if not record.doi:
        out["reasons"].append("doi_absent")
        return out
    resolved = _resolve(resolver, record.doi)
    out["metadata_source"] = _text(resolved.get("metadata_source")) or "injected-resolver"
    if resolved.get("error"):
        out["reasons"].append(f"resolver_error: {resolved['error']}")
        return out
    if resolved.get("conflict"):
        out["reasons"].append("metadata_conflict")
        return out
    status = _text(resolved.get("status")).lower()
    if status in RETRIEVAL_STATUSES and status != "ok":
        out["reasons"].append(f"resolver_{status}")
        return out
    if resolved.get("found") is False:
        out["reasons"].append("doi_not_found")
        return out
    metadata = resolved.get("metadata") or {}
    if not isinstance(metadata, Mapping):
        out["reasons"].append("resolver_metadata_not_an_object")
        return out
    resolved_doi = normalize_doi(resolved.get("doi") or metadata.get("doi") or record.doi)
    out["canonical_doi"] = resolved_doi
    if resolved_doi != record.doi:
        out["status"] = "contradicted"
        out["reasons"].append("doi_mismatch")
        return out
    if not supplied:
        out["reasons"].append("metadata_absent")
        return out
    unresolved = []
    hard_mismatch = False
    partial_mismatch = False
    for name in ("title", "authors", "year", "venue", "version"):
        if name not in supplied:
            continue
        want = supplied[name]
        got = metadata.get(name)
        if got in (None, "", []):
            unresolved.append(f"metadata_missing:{name}")
            out["checks"][name] = {"supplied": want, "resolved": None, "match": None}
            continue
        if name == "title":
            match = _norm_field(want) == _norm_field(got)
        elif name == "authors":
            match = _norm_authors(want) == _norm_authors(got)
        elif name == "year":
            match = _norm_year(want) == _norm_year(got)
        else:
            match = _norm_field(want) == _norm_field(got)
        out["checks"][name] = {"supplied": want, "resolved": got, "match": match}
        if not match:
            if name in ("title", "authors", "year"):
                hard_mismatch = True
                out["reasons"].append(f"field_mismatch:{name}")
            else:
                partial_mismatch = True
                out["reasons"].append(f"field_mismatch:{name}")
    if unresolved:
        out["reasons"].extend(unresolved)
        return out
    if hard_mismatch:
        out["status"] = "contradicted"
    elif partial_mismatch:
        out["status"] = "partially_supported"
    else:
        out["status"] = "supported"
    return out


def assess_claim(claim, sources, semantic=None):
    """Assess one claim against lifecycle-admitted source records.

    ``sources`` may be a mapping id -> record or a sequence of records.
    ``semantic`` is the optional advisory assessment, with a closed ``status``
    from :data:`STATUSES`; without it the claim is not promoted to supported.
    """
    if not isinstance(claim, Mapping):
        raise HarnessError("citation verification: claim must be an object")
    cid = _require(claim.get("claim_id") or claim.get("id"), "claim_id")
    source_ids = claim.get("source_ids") or []
    if isinstance(source_ids, str):
        source_ids = [source_ids]
    source_ids = [_text(s) for s in source_ids if _text(s)]
    if isinstance(sources, Mapping):
        by_id = dict(sources)
    else:
        by_id = {}
        for raw in sources or []:
            record = raw if isinstance(raw, SourceRecord) else SourceRecord.from_mapping(raw)
            by_id[record.source_id] = record
    out = {
        "claim_id": cid,
        "status": "not_verifiable",
        "source_ids": source_ids,
        "evidence": [],
        "reasons": [],
        "semantic": dict(semantic) if isinstance(semantic, Mapping) else None,
    }
    hard_gap = False
    if not source_ids:
        out["reasons"].append("no_source_records")
        hard_gap = True
    for sid in source_ids:
        record = by_id.get(sid)
        if record is None:
            out["reasons"].append(f"source_missing:{sid}")
            hard_gap = True
            continue
        if not isinstance(record, SourceRecord):
            record = SourceRecord.from_mapping(record)
        out["evidence"].append(record.evidence())
        if record.retrieval_status != "ok":
            out["reasons"].append(f"source_{record.retrieval_status}:{sid}")
            hard_gap = True
        if not _text(record.passage):
            out["reasons"].append(f"passage_missing:{sid}")
            hard_gap = True
        if not record.locator:
            out["reasons"].append(f"locator_missing:{sid}")
            hard_gap = True
    if semantic is None:
        out["reasons"].append("semantic_support_unassessed")
        hard_gap = True
    else:
        sem_status = _text(semantic.get("status")).lower()
        if sem_status not in STATUSES:
            out["reasons"].append("semantic_status_invalid")
            hard_gap = True
        elif sem_status == "contradicted":
            out["status"] = "contradicted"
            out["reasons"].append("semantic_contradiction")
            return out
        elif sem_status == "not_verifiable":
            out["reasons"].append("semantic_not_verifiable")
            hard_gap = True
        elif sem_status == "partially_supported":
            # Advisory, not a provenance gap: complete evidence can still be
            # partially supported, and the label gate stays closed.
            out["reasons"].append("semantic_partial_support")
    if hard_gap:
        return out
    out["status"] = ("supported" if _text((semantic or {}).get("status")).lower()
                     == "supported" else "partially_supported")
    return out


def audit_citations(citations, claims, sources, resolver=None,
                    semantic_by_claim=None):
    """Audit a report's bibliography and claims.

    ``citation_verified`` is the only label gate and requires every citation
    and claim to be strictly ``supported``.  ``ok`` is weaker: it allows
    partial support but still refuses contradictions and missing evidence.
    Unresolved items are always listed, never dropped.
    """
    semantic_by_claim = semantic_by_claim or {}
    citation_records = [
        c if isinstance(c, CitationRecord) else CitationRecord.from_mapping(c)
        for c in citations or []
    ]
    source_records = {}
    for raw in sources or []:
        record = raw if isinstance(raw, SourceRecord) else SourceRecord.from_mapping(raw)
        source_records[record.source_id] = record
    citation_results = [verify_citation(c, resolver=resolver) for c in citation_records]
    claim_results = [
        assess_claim(c, source_records,
                     semantic=semantic_by_claim.get(_text(c.get("claim_id") or c.get("id"))))
        for c in claims or []
    ]
    unresolved = []
    for result in citation_results:
        if result["status"] != "supported":
            unresolved.append({"kind": "citation", "id": result["citation_id"],
                               "status": result["status"],
                               "reasons": list(result["reasons"])})
    for result in claim_results:
        if result["status"] != "supported":
            unresolved.append({"kind": "claim", "id": result["claim_id"],
                               "status": result["status"],
                               "reasons": list(result["reasons"])})
    citation_verified = (
        bool(citation_results) and bool(claim_results)
        and all(r["status"] == "supported" for r in citation_results)
        and all(r["status"] == "supported" for r in claim_results)
    )
    ok = (
        bool(citation_results) and bool(claim_results)
        and all(r["status"] in ("supported", "partially_supported")
                for r in citation_results + claim_results)
    )
    return {
        "ok": ok,
        "citation_verified": citation_verified,
        "citations": citation_results,
        "claims": claim_results,
        "unresolved": unresolved,
        "summary": {
            "citations": len(citation_results),
            "claims": len(claim_results),
            "supported": sum(1 for r in citation_results + claim_results
                             if r["status"] == "supported"),
            "partially_supported": sum(1 for r in citation_results + claim_results
                                       if r["status"] == "partially_supported"),
            "contradicted": sum(1 for r in citation_results + claim_results
                                if r["status"] == "contradicted"),
            "not_verifiable": sum(1 for r in citation_results + claim_results
                                  if r["status"] == "not_verifiable"),
        },
    }


def may_label_citation_verified(audit):
    """The only supported way for a renderer to claim citation verification."""
    return bool(isinstance(audit, Mapping) and audit.get("citation_verified") is True)
