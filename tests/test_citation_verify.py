"""Hermetic tests for deterministic citation identity and claim provenance (#422)."""
import hashlib
import unittest

from harness.citation_verify import (
    CitationRecord, SourceRecord, assess_claim, audit_citations,
    may_label_citation_verified, normalize_doi, verify_citation,
)
from harness.errors import HarnessError

DOI = "10.1234/abc"
META = {
    "doi": DOI,
    "title": "A Study of Things",
    "authors": ["Ada Lovelace", "Grace Hopper"],
    "year": 2024,
    "venue": "Journal of Tests",
    "version": "published",
}
CITATION = {
    "citation_id": "ref-1",
    "doi": "https://doi.org/10.1234/ABC",
    "title": "A Study of Things",
    "authors": ["Grace Hopper", "Ada Lovelace"],
    "year": 2024,
    "venue": "Journal of Tests",
    "version": "published",
}
PASSAGE = "The measured effect was 12 percent across all three cohorts."
SOURCE = {
    "source_id": "src-1",
    "url": "https://example.test/paper",
    "source_class": "journal",
    "retrieved_at": "2026-11-04T00:00:00Z",
    "doi": DOI,
    "locator": "results, paragraph 3",
    "passage": PASSAGE,
    "request_id": "req-1",
}
CLAIM = {"claim_id": "claim-1", "source_ids": ["src-1"],
         "text": "The measured effect was 12 percent."}


def resolver(metadata=None, **extra):
    def _resolve(doi):
        result = {"found": True, "doi": DOI,
                  "metadata": dict(META if metadata is None else metadata),
                  "metadata_source": "crossref:fixture"}
        result.update(extra)
        return result
    return _resolve


class DoiTests(unittest.TestCase):
    def test_doi_forms_normalize_to_one_canonical_value(self):
        for raw in ("10.1234/ABC", "https://doi.org/10.1234/abc",
                    "http://dx.doi.org/10.1234/abc", "doi: 10.1234/abc"):
            self.assertEqual(normalize_doi(raw), DOI)
        self.assertIsNone(normalize_doi(""))
        self.assertIsNone(normalize_doi("not-a-doi"))


class CitationIdentityTests(unittest.TestCase):
    def test_exact_match_is_supported_and_verified(self):
        result = verify_citation(CITATION, resolver())
        self.assertEqual(result["status"], "supported", result)
        self.assertEqual(result["canonical_doi"], DOI)
        self.assertEqual(result["reasons"], [])
        self.assertTrue(result["checks"]["title"]["match"])

    def test_wrong_doi_is_contradicted_not_verified(self):
        result = verify_citation(CITATION, resolver(doi="10.9999/other"))
        self.assertEqual(result["status"], "contradicted")
        self.assertIn("doi_mismatch", result["reasons"])

    def test_title_mismatch_surfaces_the_field(self):
        meta = dict(META, title="A Different Study")
        result = verify_citation(CITATION, resolver(meta))
        self.assertEqual(result["status"], "contradicted")
        self.assertIn("field_mismatch:title", result["reasons"])
        self.assertFalse(result["checks"]["title"]["match"])

    def test_author_and_year_mismatch_are_contradicted(self):
        for meta in (dict(META, authors=["Someone Else"]),
                     dict(META, year=1999)):
            result = verify_citation(CITATION, resolver(meta))
            self.assertEqual(result["status"], "contradicted", result)
            self.assertIn("field_mismatch", " ".join(result["reasons"]))

    def test_preprint_versus_published_is_partial_not_verified(self):
        citation = dict(CITATION, venue="bioRxiv", version="preprint")
        result = verify_citation(citation, resolver())
        self.assertEqual(result["status"], "partially_supported", result)
        self.assertIn("field_mismatch:venue", result["reasons"])
        self.assertIn("field_mismatch:version", result["reasons"])

    def test_missing_resolved_field_is_not_verifiable(self):
        meta = dict(META)
        meta.pop("venue")
        result = verify_citation(CITATION, resolver(meta))
        self.assertEqual(result["status"], "not_verifiable")
        self.assertIn("metadata_missing:venue", result["reasons"])

    def test_doi_less_web_source_keeps_its_url_and_stays_unknown(self):
        citation = {"citation_id": "web-1", "url": "https://example.test/report",
                    "title": "A web report"}
        result = verify_citation(citation)
        self.assertEqual(result["status"], "not_verifiable")
        self.assertIn("doi_absent", result["reasons"])
        self.assertEqual(result["url"], "https://example.test/report")
        self.assertIsNone(result["canonical_doi"])

    def test_resolver_failures_are_evidence_not_exceptions(self):
        def raises(doi):
            raise TimeoutError("upstream timed out")
        result = verify_citation(CITATION, raises)
        self.assertEqual(result["status"], "not_verifiable")
        self.assertIn("resolver_error", " ".join(result["reasons"]))
        result = verify_citation(CITATION, lambda doi: {"status": "rate_limited"})
        self.assertIn("resolver_rate_limited", result["reasons"])
        result = verify_citation(CITATION, lambda doi: {"found": False})
        self.assertIn("doi_not_found", result["reasons"])

    def test_conflicting_registry_metadata_is_not_verifiable(self):
        result = verify_citation(CITATION, resolver(conflict=True))
        self.assertEqual(result["status"], "not_verifiable")
        self.assertIn("metadata_conflict", result["reasons"])


class ClaimSupportTests(unittest.TestCase):
    def test_supported_claim_needs_passage_locator_and_semantic_advice(self):
        semantic = {"status": "supported", "reason": "fixture"}
        result = assess_claim(CLAIM, [SOURCE], semantic=semantic)
        self.assertEqual(result["status"], "supported", result)
        self.assertEqual(result["evidence"][0]["locator"], "results, paragraph 3")
        self.assertFalse(assess_claim(CLAIM, [dict(SOURCE, passage="")],
                                      semantic=semantic)["status"] == "supported")
        self.assertFalse(assess_claim(CLAIM, [dict(SOURCE, locator="")],
                                      semantic=semantic)["status"] == "supported")
        self.assertEqual(assess_claim(CLAIM, [SOURCE])["status"], "not_verifiable")

    def test_partial_support_is_partial_not_verified(self):
        semantic = {"status": "partially_supported"}
        result = assess_claim(CLAIM, [SOURCE], semantic=semantic)
        self.assertEqual(result["status"], "partially_supported")
        self.assertIn("semantic_partial_support", result["reasons"])

    def test_semantic_contradiction_wins_even_with_provenance(self):
        result = assess_claim(CLAIM, [SOURCE],
                              semantic={"status": "contradicted"})
        self.assertEqual(result["status"], "contradicted")

    def test_paywalled_or_missing_source_is_not_verifiable(self):
        paywalled = dict(SOURCE, retrieval_status="paywalled")
        result = assess_claim(CLAIM, [paywalled],
                              semantic={"status": "supported"})
        self.assertEqual(result["status"], "not_verifiable")
        self.assertIn("source_paywalled", " ".join(result["reasons"]))
        missing = assess_claim({"claim_id": "c2", "source_ids": ["absent"]},
                               [SOURCE], semantic={"status": "supported"})
        self.assertIn("source_missing:absent", missing["reasons"])

    def test_evidence_pins_the_full_passage_hash_and_bounds_the_excerpt(self):
        long = "x" * 2500
        source = SourceRecord.from_mapping(dict(SOURCE, passage=long))
        evidence = source.evidence()
        self.assertEqual(evidence["passage_sha256"],
                         hashlib.sha256(long.encode("utf-8")).hexdigest())
        self.assertEqual(len(evidence["passage"]), 2000)
        self.assertTrue(evidence["passage_truncated"])
        self.assertEqual(evidence["request_id"], "req-1")


class ReportGateTests(unittest.TestCase):
    def _audit(self, citations=None, claims=None, sources=None, semantic=None):
        return audit_citations(
            citations if citations is not None else [CITATION],
            claims if claims is not None else [CLAIM],
            sources if sources is not None else [SOURCE],
            resolver=resolver(), semantic_by_claim=semantic or {"claim-1": {"status": "supported"}},
        )

    def test_all_supported_is_the_only_label_pass(self):
        audit = self._audit()
        self.assertTrue(audit["ok"])
        self.assertTrue(audit["citation_verified"])
        self.assertTrue(may_label_citation_verified(audit))
        self.assertEqual(audit["unresolved"], [])

    def test_partial_or_unresolved_never_carries_the_label(self):
        audit = self._audit(
            citations=[dict(CITATION, venue="bioRxiv", version="preprint")])
        self.assertTrue(audit["ok"])
        self.assertFalse(audit["citation_verified"])
        self.assertFalse(may_label_citation_verified(audit))
        self.assertEqual(audit["unresolved"][0]["kind"], "citation")
        self.assertEqual(audit["unresolved"][0]["status"], "partially_supported")

    def test_missing_claim_evidence_blocks_the_label_and_lists_it(self):
        audit = self._audit(sources=[dict(SOURCE, passage="")])
        self.assertFalse(audit["citation_verified"])
        self.assertEqual(audit["unresolved"][0]["kind"], "claim")
        self.assertIn("passage_missing:src-1", audit["unresolved"][0]["reasons"])

    def test_empty_report_is_not_verified(self):
        audit = audit_citations([], [], [], resolver=resolver())
        self.assertFalse(audit["ok"])
        self.assertFalse(audit["citation_verified"])
        self.assertFalse(may_label_citation_verified(audit))

    def test_label_helper_refuses_non_audit_values(self):
        self.assertFalse(may_label_citation_verified(None))
        self.assertFalse(may_label_citation_verified({"citation_verified": "yes"}))


class InputValidationTests(unittest.TestCase):
    def test_citation_needs_an_identifier_and_a_doi_or_url(self):
        with self.assertRaises(HarnessError):
            CitationRecord.from_mapping({"title": "no id"})
        with self.assertRaises(HarnessError):
            CitationRecord.from_mapping({"citation_id": "c1", "title": "no locator"})

    def test_source_requires_lifecycle_fields_and_closed_classes(self):
        for bad in (dict(SOURCE, source_class=""),
                    dict(SOURCE, source_class="blog"),
                    dict(SOURCE, retrieval_status="maybe"),
                    dict(SOURCE, retrieved_at="")):
            with self.assertRaises(HarnessError):
                SourceRecord.from_mapping(bad)

    def test_dict_sources_are_accepted(self):
        result = assess_claim(CLAIM, {"src-1": SOURCE},
                              semantic={"status": "supported"})
        self.assertEqual(result["status"], "supported")


if __name__ == "__main__":
    unittest.main()
