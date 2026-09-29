"""HV-2 contract tests for evidence-bearing, reusable context briefs."""
import itertools
import json
import unittest
from datetime import datetime, timedelta, timezone, tzinfo
from unittest.mock import patch

from harness.context_brief import (
    CONTEXT_BRIEF_SCHEMA_VERSION,
    CONTEXT_BRIEF_STAGES,
    create_context_brief,
    render_context_brief,
    validate_context_brief,
)
from harness.errors import HarnessError


OBSERVED_AT = "2026-09-25T12:00:00Z"
SAMPLE_SOURCE = "def evaluate(value: int) -> bool:\n    return bool(value)\n"

# Sentinel for "remove this key" in the mutation tables below.
UNSET = object()


def put(root, path, value):
    """Return `root` with one field set by dotted path ('evidence.0.kind').

    A `None` path replaces the whole artifact, which is how the "not even a
    brief" refusals are expressed in the same table as the field-level ones.
    """
    if path is None:
        return value
    parts = path.split(".")
    node = root
    for part in parts[:-1]:
        node = node[int(part)] if isinstance(node, list) else node[part]
    last = parts[-1]
    if isinstance(node, list):
        node[int(last)] = value
    elif value is UNSET:
        del node[last]
    else:
        node[last] = value
    return root


class ContextBriefSchemaTests(unittest.TestCase):
    def _brief(self):
        return create_context_brief(
            "Check evaluate behavior",
            {"src/evaluate.py": SAMPLE_SOURCE},
            claims=[{
                "text": "evaluate accepts an integer and returns a boolean",
                "source_paths": ["src/evaluate.py"],
            }],
            uncertainties=["Runtime behavior is not independently tested here."],
            conflicts=["The request calls for a behavior change; source does not specify the rule."],
            source_modified_at={"src/evaluate.py": "2026-09-24T11:30:00-04:00"},
            observed_at=OBSERVED_AT,
            max_tokens=2500,
        )

    def test_versioned_schema_has_every_contract_field(self):
        brief = self._brief()
        self.assertEqual(brief["schema_version"], CONTEXT_BRIEF_SCHEMA_VERSION)
        self.assertEqual(brief["stage"], "context")
        self.assertEqual(brief["goal"], "Check evaluate behavior")
        self.assertTrue(brief["grounded_claims"][0]["evidence_refs"])
        self.assertEqual(len(brief["source_identity"]["sources"]), 1)
        self.assertEqual(brief["coverage"]["requested_sources"], 1)
        self.assertEqual(brief["coverage"]["included_source_ids"], brief["included_scope"])
        self.assertEqual(brief["excluded_scope"], [])
        self.assertEqual(brief["uncertainties"], [
            "Runtime behavior is not independently tested here.",
        ])
        self.assertEqual(len(brief["conflicts"]), 1)
        self.assertEqual(brief["source_identity"]["observed_at"], OBSERVED_AT)
        source = brief["source_identity"]["sources"][0]
        self.assertEqual(source["source_modified_at"], "2026-09-24T15:30:00Z")
        self.assertEqual(source["freshness"], "source_timestamp_available")
        self.assertEqual(set(brief), {
            "schema_version", "stage", "goal", "grounded_claims", "evidence",
            "included_scope", "excluded_scope", "coverage", "source_identity",
            "uncertainties", "conflicts", "omissions", "token_estimate",
        })
        self.assertEqual(validate_context_brief(brief), [])

    def test_create_validate_render_round_trip(self):
        brief = self._brief()
        rendered = render_context_brief(brief, max_tokens=2500)
        self.assertEqual(json.loads(rendered), brief)
        self.assertEqual(validate_context_brief(json.loads(rendered)), [])

    def test_validation_rejects_missing_fields_and_unknown_version(self):
        brief = self._brief()
        missing = dict(brief)
        del missing["conflicts"]
        self.assertTrue(any("missing required fields" in issue
                            for issue in validate_context_brief(missing)))

        unsupported = dict(brief, schema_version=CONTEXT_BRIEF_SCHEMA_VERSION + 1)
        self.assertTrue(any("schema_version" in issue
                            for issue in validate_context_brief(unsupported)))

    def test_source_freshness_check_detects_drift(self):
        brief = self._brief()
        self.assertEqual(validate_context_brief(
            brief, source_contents={"src/evaluate.py": SAMPLE_SOURCE}), [])
        self.assertTrue(any("hash drifted" in issue for issue in validate_context_brief(
            brief, source_contents={"src/evaluate.py": "def evaluate(): ...\n"})))

    def test_validation_returns_issues_for_malformed_external_artifacts(self):
        brief = json.loads(json.dumps(self._brief()))
        brief["source_identity"]["sources"][0]["path"] = []
        brief["grounded_claims"][0]["evidence_refs"] = [[]]
        brief["evidence"][0]["id"] = []

        issues = validate_context_brief(brief, source_contents={})
        self.assertTrue(issues)
        self.assertTrue(all(isinstance(issue, str) for issue in issues))

    def test_stage_declares_the_stage_the_brief_is_minted_for(self):
        planning = create_context_brief(
            "Decide whether to refactor", {"src.py": SAMPLE_SOURCE},
            stage="planning", observed_at=OBSERVED_AT, max_tokens=2000,
        )
        self.assertEqual(planning["stage"], "planning")
        self.assertEqual(validate_context_brief(planning), [])
        self.assertEqual(json.loads(render_context_brief(planning))["stage"], "planning")
        self.assertEqual(self._brief()["stage"], "context")

    def test_undeclared_stage_is_rejected_on_create_and_validate(self):
        with self.assertRaises(HarnessError):
            create_context_brief(
                "Plan the work", {"src.py": SAMPLE_SOURCE}, stage="review",
                observed_at=OBSERVED_AT, max_tokens=2000,
            )
        retagged = json.loads(json.dumps(self._brief()))
        retagged["stage"] = "waist"
        self.assertTrue(any("stage must be one of" in issue
                            for issue in validate_context_brief(retagged)))

    def test_uncited_or_unknown_claims_are_rejected(self):
        with self.assertRaises(HarnessError):
            create_context_brief(
                "Inspect source", {"src.py": "def f(): ..."},
                claims=[{"text": "unsupported fact"}],
                observed_at=OBSERVED_AT, max_tokens=1800,
            )
        with self.assertRaises(HarnessError):
            create_context_brief(
                "Inspect source", {"src.py": "def f(): ..."},
                claims=[{"text": "unknown citation", "evidence_refs": ["ev:unknown"]}],
                observed_at=OBSERVED_AT, max_tokens=1800,
            )


class SuppliedContextBriefTests(unittest.TestCase):
    def test_caller_supplied_brief_validates_and_renders_without_intake(self):
        brief = create_context_brief(
            "Inspect a function", {"src.py": "def f() -> int: ...\n"},
            observed_at=OBSERVED_AT, max_tokens=2000,
        )
        with patch("harness.context_brief.create_context_brief",
                   side_effect=AssertionError("intake must not run")):
            issues = validate_context_brief(brief)
            rendered = render_context_brief(brief)
        self.assertEqual(issues, [])
        self.assertEqual(json.loads(rendered), brief)


class ContextBriefPruningTests(unittest.TestCase):
    def test_pruning_keeps_claim_cited_decision_critical_evidence(self):
        noise = "\n".join(
            f"def unrelated_{index}(value: int) -> int: ..."
            for index in range(120)
        )
        brief = create_context_brief(
            "Preserve the requested contract",
            {"00-critical.py": SAMPLE_SOURCE, "99-background.py": noise},
            claims=[{
                "text": "evaluate accepts an integer and returns a boolean",
                "source_paths": ["00-critical.py"],
            }],
            observed_at=OBSERVED_AT,
            max_tokens=1400,
        )
        claim = brief["grounded_claims"][0]
        retained = {item["id"]: item for item in brief["evidence"]}
        self.assertTrue(set(claim["evidence_refs"]).issubset(retained))
        for evidence_id in claim["evidence_refs"]:
            self.assertTrue(retained[evidence_id]["decision_critical"])
            self.assertFalse(retained[evidence_id]["truncated"])
        self.assertLessEqual(brief["token_estimate"]["value"], 1400)
        self.assertTrue(
            brief["coverage"]["truncated_evidence_ids"]
            or brief["coverage"]["excluded_source_ids"],
            "the large background source must exercise the pruning path",
        )
        self.assertEqual(validate_context_brief(brief, max_tokens=1400), [])


class ContextBriefCoverageTests(unittest.TestCase):
    NO_EVIDENCE_REASON = "no extractable interface or failure evidence"
    BUDGET_REASON = "source evidence omitted to fit max_tokens"

    def test_inspected_sources_excludes_sources_that_yielded_no_evidence(self):
        brief = create_context_brief(
            "Check the module surface",
            {"src/api.py": SAMPLE_SOURCE, "src/imports_only.py": "import os\n"},
            observed_at=OBSERVED_AT, max_tokens=2500,
        )
        coverage = brief["coverage"]
        self.assertEqual(coverage["requested_sources"], 2)
        self.assertEqual(coverage["inspected_sources"], 1)
        self.assertEqual(len(brief["included_scope"]), 1)
        self.assertEqual([item["reason"] for item in brief["excluded_scope"]],
                         [self.NO_EVIDENCE_REASON])
        self.assertFalse(coverage["complete"])
        self.assertEqual(validate_context_brief(brief), [])

    def test_budget_pruned_source_still_counts_as_inspected(self):
        noise = "\n".join(
            f"def unrelated_{index}(value: int) -> int: ..."
            for index in range(120)
        )
        brief = create_context_brief(
            "Bound the context", {"api.py": SAMPLE_SOURCE, "noise.py": noise},
            observed_at=OBSERVED_AT, max_tokens=1400,
        )
        coverage = brief["coverage"]
        self.assertEqual(coverage["requested_sources"], 2)
        # The budget pruned noise.py, but extraction still ran against it, so it
        # is an inspected source rather than an unextracted gap.
        self.assertEqual(coverage["inspected_sources"], 2)
        self.assertEqual(len(coverage["truncated_evidence_ids"]), 1)
        self.assertEqual(brief["excluded_scope"], [])
        self.assertLessEqual(coverage["requested_sources"], coverage["inspected_sources"])
        self.assertEqual(validate_context_brief(brief, max_tokens=1400), [])

    def test_inspected_sources_must_match_the_reported_exclusions(self):
        brief = create_context_brief(
            "Check the module surface",
            {"src/api.py": SAMPLE_SOURCE, "src/imports_only.py": "import os\n"},
            observed_at=OBSERVED_AT, max_tokens=2500,
        )
        inflated = json.loads(json.dumps(brief))
        inflated["coverage"]["inspected_sources"] = 2
        self.assertTrue(any("inspected_sources" in issue
                            for issue in validate_context_brief(inflated)))

    def test_truncation_and_omission_are_visible_and_bounded(self):
        source = "\n".join(
            ["class ImportantAPI:"]
            + [f"def helper_{index}(value: int) -> int: ..." for index in range(100)]
        )
        brief = create_context_brief(
            "Keep the context small", {"api.py": source},
            observed_at=OBSERVED_AT, max_tokens=1100,
        )
        self.assertLessEqual(brief["token_estimate"]["value"], 1100)
        truncated_ids = set(brief["coverage"]["truncated_evidence_ids"])
        excluded_ids = set(brief["coverage"]["excluded_source_ids"])
        omission_refs = {(item["kind"], item["ref"]) for item in brief["omissions"]}
        for evidence in brief["evidence"]:
            self.assertIn(("evidence", evidence["id"]), omission_refs)
            if evidence["truncated"]:
                self.assertIn("truncated", evidence["content"])
        for source_id in excluded_ids:
            self.assertIn(("source", source_id), omission_refs)
        self.assertTrue(truncated_ids or excluded_ids)
        self.assertFalse(brief["coverage"]["complete"])
        self.assertEqual(validate_context_brief(brief), [])

    def test_preexisting_extraction_caps_are_visible_as_truncation(self):
        source = "\n".join(f"def api_{index}(): ..." for index in range(81))
        brief = create_context_brief(
            "Inspect the exported API", {"api.ts": source},
            observed_at=OBSERVED_AT, max_tokens=2500,
        )

        evidence = brief["evidence"][0]
        self.assertTrue(evidence["truncated"])
        self.assertIn("truncated", evidence["content"])
        self.assertEqual(brief["coverage"]["truncated_evidence_ids"], [evidence["id"]])
        self.assertFalse(brief["coverage"]["complete"])
        self.assertEqual(validate_context_brief(brief), [])

    def test_condensed_long_error_trace_marks_omitted_text(self):
        error_log = "\n".join(f"diagnostic line {index}" for index in range(200))
        brief = create_context_brief(
            "Understand the failure", {}, error_log=error_log,
            observed_at=OBSERVED_AT, max_tokens=2000,
        )

        evidence = brief["evidence"][0]
        self.assertEqual(evidence["kind"], "failure_trace")
        self.assertTrue(evidence["truncated"])
        self.assertIn("truncated", evidence["content"])
        self.assertIn(evidence["id"], brief["coverage"]["truncated_evidence_ids"])
        self.assertEqual(validate_context_brief(brief), [])

    def test_token_estimate_is_labeled_estimated_not_measured(self):
        brief = self._brief_for_estimate()
        estimate = brief["token_estimate"]
        self.assertEqual(set(estimate), {"value", "kind"})
        self.assertGreater(estimate["value"], 0)
        self.assertEqual(estimate["kind"], "estimated")

        falsely_measured = json.loads(json.dumps(brief))
        falsely_measured["token_estimate"]["kind"] = "measured"
        self.assertTrue(any("must be 'estimated'" in issue
                            for issue in validate_context_brief(falsely_measured)))

    @staticmethod
    def _brief_for_estimate():
        return create_context_brief(
            "Estimate this brief", {"src.py": "def work(): ...\n"},
            observed_at=OBSERVED_AT, max_tokens=1800,
        )

    def test_too_small_budget_refuses_instead_of_silently_exceeding(self):
        with self.assertRaises(HarnessError):
            create_context_brief("g", {"src.py": "def f(): ..."}, max_tokens=20)

    def test_claim_cannot_cite_precondensed_evidence(self):
        with self.assertRaises(HarnessError):
            create_context_brief(
                "Keep the full basis", {"api.ts": "\n".join(
                    f"function api_{index}() {{}}" for index in range(81)
                )},
                claims=[{"text": "api_0 exists", "source_paths": ["api.ts"]}],
                observed_at=OBSERVED_AT, max_tokens=2500,
            )

    def test_claim_evidence_must_fit_whole(self):
        with self.assertRaises(HarnessError):
            create_context_brief(
                "Keep the full basis", {"api.py": "\n".join(
                    f"def api_{index}(value: int) -> int: ..." for index in range(100)
                )},
                claims=[{"text": "The module defines api_0", "source_paths": ["api.py"]}],
                observed_at=OBSERVED_AT, max_tokens=1000,
            )

    def test_input_validation_rejects_string_focus_list_and_empty_files_safely(self):
        for files, focus_symbols in (({"bad.py": 1}, None), ({}, "function")):
            with self.subTest(files=files, focus_symbols=focus_symbols):
                with self.assertRaises(HarnessError):
                    create_context_brief(
                        "Check input", files, focus_symbols=focus_symbols,
                        observed_at=OBSERVED_AT, max_tokens=1200,
                    )
        empty = create_context_brief(
            "Check no supplied sources", {}, observed_at=OBSERVED_AT,
            max_tokens=900,
        )
        self.assertEqual(empty["evidence"], [])
        self.assertEqual(empty["source_identity"]["sources"], [])
        self.assertEqual(validate_context_brief(empty), [])

    def test_noncanonical_timestamps_are_rejected(self):
        brief = self._brief_for_estimate()
        malformed_time = json.loads(json.dumps(brief))
        malformed_time["source_identity"]["observed_at"] = "2026-09-25T12:00:00+00:00"
        self.assertTrue(any("canonical UTC" in issue for issue in validate_context_brief(
            malformed_time)))


class _ExplodingOffset(tzinfo):
    """A tzinfo whose offset lookup fails, as a hostile artifact would."""

    def utcoffset(self, dt):
        raise ValueError("offset unavailable")


class _EmptyOffset(tzinfo):
    """A tzinfo that reports no offset at all."""

    def utcoffset(self, dt):
        return None


def two_source_brief():
    """A complete two-source brief: one grounded claim, nothing pruned."""
    return create_context_brief(
        "Bound the context for a behavior question",
        {
            "src/evaluate.py": SAMPLE_SOURCE,
            "src/registry.py": "REGISTRY = {}\n\ndef register(name: str) -> None: ...\n",
        },
        claims=[{
            "text": "evaluate accepts an integer and returns a boolean",
            "source_paths": ["src/evaluate.py"],
        }],
        uncertainties=["Runtime behavior is not independently tested here."],
        conflicts=["The request asks for a change the source does not specify."],
        source_modified_at={
            "src/evaluate.py": "2026-09-24T11:30:00-04:00",
            "src/registry.py": "2026-09-24T11:31:00-04:00",
        },
        observed_at=OBSERVED_AT,
        max_tokens=3000,
    )


class ContextBriefRejectionContractTests(unittest.TestCase):
    """The refusals themselves are the contract, so they are tested one by one.

    A later stage is handed a brief without re-running intake, so a validator
    that quietly accepts a drifted artifact is worse than no validator. Each
    case breaks exactly one field of a known-good brief (or one create
    argument) and asserts the specific issue raised. Cascading issues are fine
    and expected -- the assertion is that the intended refusal is among them.
    """

    BUDGET_REASON = "source evidence omitted to fit max_tokens"
    NO_EVIDENCE_REASON = "no extractable interface or failure evidence"

    def _good_brief(self):
        """Two sources, one grounded claim, complete and internally consistent."""
        brief = two_source_brief()
        self.assertEqual(validate_context_brief(brief), [])
        return brief

    def _assert_rejects(self, path, value, expected):
        brief = put(json.loads(json.dumps(self._good_brief())), path, value)
        issues = validate_context_brief(brief)
        self.assertTrue(expected in issues,
                        f"expected {expected!r} among {issues!r}")
        return issues

    def test_validator_refuses_artifacts_that_are_not_briefs(self):
        self.assertEqual(validate_context_brief("not a brief"),
                         ["brief must be a JSON object"])
        self.assertEqual(validate_context_brief([1, 2, 3]),
                         ["brief must be a JSON object"])
        self.assertEqual(validate_context_brief(None),
                         ["brief must be a JSON object"])
        self.assertEqual(validate_context_brief({1: "numeric key"}),
                         ["brief object keys must be strings"])
        self.assertEqual(
            validate_context_brief({"goal": {"a", "b"}}),
            ["brief must contain JSON-compatible values"])

    def test_validator_refuses_broken_top_level_fields(self):
        cases = [
            ("extra", 1, "brief has unsupported fields: extra"),
            ("schema_version", True,
             f"schema_version must be {CONTEXT_BRIEF_SCHEMA_VERSION}"),
            ("schema_version", "1",
             f"schema_version must be {CONTEXT_BRIEF_SCHEMA_VERSION}"),
            ("stage", 7, "stage must be one of " + ", ".join(CONTEXT_BRIEF_STAGES)),
            ("stage", "waist", "stage must be one of " + ", ".join(CONTEXT_BRIEF_STAGES)),
            ("goal", "   ", "goal must be a non-empty string"),
            ("goal", None, "goal must be a non-empty string"),
            ("conflicts", UNSET,
             "brief is missing required fields: conflicts"),
            ("uncertainties", "prose", "uncertainties must be a list of non-empty strings"),
            ("uncertainties", [""], "uncertainties must be a list of non-empty strings"),
            ("conflicts", ["   "], "conflicts must be a list of non-empty strings"),
        ]
        for path, value, expected in cases:
            with self.subTest(path=path, value=value):
                self._assert_rejects(path, value, expected)

    def test_validator_refuses_broken_source_identity(self):
        cases = [
            ("source_identity", {},
             "source_identity must contain observed_at, manifest_sha256, and sources only"),
            ("source_identity", {"sources": []},
             "source_identity must contain observed_at, manifest_sha256, and sources only"),
            # An unusable identity must not crash the validator: the missing
            # observed_at surfaces as its own issue instead of an exception.
            ("source_identity.observed_at", "nonsense",
             "source_identity.observed_at must be an ISO-8601 timestamp"),
            ("source_identity.observed_at", 1758782400,
             "source_identity.observed_at must be an ISO-8601 timestamp"),
            ("source_identity.observed_at", "2026-09-25T12:00:00",
             "source_identity.observed_at must include a timezone"),
            ("source_identity.manifest_sha256", "not-a-digest",
             "source_identity.manifest_sha256 must be a SHA-256 hex digest"),
            ("source_identity.manifest_sha256", "0" * 64,
             "source_identity.manifest_sha256 does not match the source catalog"),
            ("source_identity.sources", "src/evaluate.py",
             "source_identity.sources must be a list"),
        ]
        for path, value, expected in cases:
            with self.subTest(path=path, value=value):
                self._assert_rejects(path, value, expected)

    def test_validator_refuses_broken_source_entries(self):
        prefix = "source_identity.sources[0]"
        cases = [
            ("source_identity.sources.0", {"id": "src:x", "path": "a.py"},
             prefix + " has an invalid schema"),
            ("source_identity.sources.0", "src/evaluate.py",
             prefix + " has an invalid schema"),
            ("source_identity.sources.0.path", "  ",
             prefix + ".path must be non-empty"),
            ("source_identity.sources.0.sha256", "not-a-digest",
             prefix + ".sha256 must be a SHA-256 hex digest"),
            ("source_identity.sources.0.id", "src:forged",
             prefix + ".id does not match path and content hash"),
            ("source_identity.sources.0.source_modified_at", "2026-09-24T15:30:00+00:00",
             prefix + ".source_modified_at is not canonical UTC"),
            ("source_identity.sources.0.source_modified_at", "yesterday",
             prefix + ".source_modified_at must be an ISO-8601 timestamp"),
            ("source_identity.sources.0.freshness", "capture_time_only",
             prefix + ".freshness contradicts its timestamp"),
        ]
        for path, value, expected in cases:
            with self.subTest(path=path, value=value):
                self._assert_rejects(path, value, expected)

    def test_validator_refuses_duplicate_and_unsorted_source_catalogs(self):
        brief = json.loads(json.dumps(self._good_brief()))
        sources = brief["source_identity"]["sources"]
        self.assertEqual(len(sources), 2)

        duplicated = json.loads(json.dumps(brief))
        duplicated["source_identity"]["sources"].append(json.loads(json.dumps(sources[0])))
        issues = validate_context_brief(duplicated)
        self.assertTrue(
            any("duplicate source identity or path at source index 2" in issue
                for issue in issues), issues)

        unsorted = json.loads(json.dumps(brief))
        unsorted["source_identity"]["sources"].reverse()
        issues = validate_context_brief(unsorted)
        self.assertTrue(any("must be sorted by source id" in issue for issue in issues),
                        issues)

    def test_validator_refuses_broken_grounded_claims(self):
        cases = [
            ("grounded_claims", "prose", "grounded_claims must be a list"),
            ("grounded_claims", {},
             "grounded_claims must be a list"),
            ("grounded_claims.0", {"text": "t", "evidence_refs": [], "extra": 1},
             "grounded_claims[0] must contain text and evidence_refs only"),
            ("grounded_claims.0", "t", "grounded_claims[0] must contain text and evidence_refs only"),
            ("grounded_claims.0.text", "",
             "grounded_claims[0] requires text and evidence references"),
            ("grounded_claims.0.evidence_refs", "ev:whatever",
             "grounded_claims[0] requires text and evidence references"),
        ]
        for path, value, expected in cases:
            with self.subTest(path=path, value=value):
                self._assert_rejects(path, value, expected)

        duplicated = json.loads(json.dumps(self._good_brief()))
        refs = duplicated["grounded_claims"][0]["evidence_refs"]
        duplicated["grounded_claims"][0]["evidence_refs"] = refs + refs
        issues = validate_context_brief(duplicated)
        self.assertTrue(any("duplicate evidence references" in issue for issue in issues),
                        issues)

    def test_validator_refuses_broken_evidence_entries(self):
        cases = [
            ("evidence", "prose", "evidence must be a list"),
            ("evidence", {}, "evidence must be a list"),
            ("evidence.0", {"id": "ev:forged"},
             "evidence[0] has an invalid schema"),
            ("evidence.0", "ev:forged", "evidence[0] has an invalid schema"),
            ("evidence.0.source_id", "src:forged", "evidence[0] references an unknown source"),
            ("evidence.0.id", "ev:forged", "evidence[0].id does not match its source and kind"),
            ("evidence.0.id", "forged", "evidence[0].id must be an evidence identifier"),
            ("evidence.0.kind", "guess", "evidence[0].kind is unsupported"),
            ("evidence.0.content", "  ", "evidence[0].content must be non-empty"),
            ("evidence.0.sha256", "0" * 64, "evidence[0].sha256 does not match its content"),
            ("evidence.0.truncated", True,
             "evidence[0] marked truncated must end with the truncation marker"),
            ("evidence.0.truncated", "yes", "evidence[0].truncated must be a boolean"),
            ("evidence.0.decision_critical", "yes",
             "evidence[0].decision_critical must be a boolean"),
        ]
        for path, value, expected in cases:
            with self.subTest(path=path, value=value):
                self._assert_rejects(path, value, expected)

    def test_validator_refuses_broken_scope_and_coverage(self):
        cases = [
            ("included_scope", "src", "included_scope must be a list of source ids"),
            ("included_scope", [7], "included_scope must be a list of source ids"),
            ("excluded_scope", "src", "excluded_scope must be a list"),
            ("excluded_scope", [{"source_id": "src:forged"}],
             "excluded_scope[0] must contain a source id and reason"),
            ("excluded_scope", [{"source_id": "src:forged", "reason": "   "}],
             "excluded_scope[0] must contain a source id and reason"),
            ("coverage", {}, "coverage has an invalid schema"),
            ("coverage", "complete", "coverage has an invalid schema"),
            ("coverage.included_source_ids", [],
             "coverage.included_source_ids does not match included_scope"),
            ("coverage.excluded_source_ids", ["src:forged"],
             "coverage.excluded_source_ids does not match excluded_scope"),
            ("coverage.truncated_evidence_ids", ["ev:forged"],
             "coverage.truncated_evidence_ids does not match evidence"),
            ("coverage.complete", "yes", "coverage.complete must be a boolean"),
            ("coverage.complete", False,
             "coverage.complete does not reflect omissions and truncation"),
        ]
        for path, value, expected in cases:
            with self.subTest(path=path, value=value):
                self._assert_rejects(path, value, expected)

    def test_validator_refuses_scope_that_does_not_partition_the_catalog(self):
        brief = json.loads(json.dumps(self._good_brief()))
        one_id = brief["included_scope"][0]

        duplicated = json.loads(json.dumps(brief))
        duplicated["included_scope"] = brief["included_scope"] + [one_id]
        issues = validate_context_brief(duplicated)
        self.assertTrue(any("duplicate source ids" in issue for issue in issues), issues)

        overlapping = json.loads(json.dumps(brief))
        overlapping["excluded_scope"] = [{"source_id": one_id, "reason": "budget"}]
        issues = validate_context_brief(overlapping)
        self.assertTrue(any("must partition the source catalog" in issue
                            for issue in issues), issues)

        repeated = json.loads(json.dumps(brief))
        repeated["excluded_scope"] = [{"source_id": one_id, "reason": "budget"}] * 2
        issues = validate_context_brief(repeated)
        self.assertTrue(any("contains duplicate source id" in issue for issue in issues),
                        issues)

    def test_validator_refuses_missing_or_incoherent_omission_records(self):
        brief = json.loads(json.dumps(self._good_brief()))
        cases = [
            ("omissions", "prose", "omissions must be a list"),
            ("omissions", {"kind": "source"}, "omissions must be a list"),
            ("omissions", [{"kind": "source", "ref": "src:forged"}],
             "omissions[0] must contain kind, ref, and reason"),
            ("omissions", [{"kind": "source", "ref": "", "reason": "why"}],
             "omissions[0] must contain kind, ref, and reason"),
            ("omissions", [{"kind": "guess", "ref": "src:x", "reason": "why"}],
             "omissions[0] must contain kind, ref, and reason"),
            ("omissions", [{"kind": "source", "ref": "src:forged", "reason": "why"}],
             "omissions[0] references unknown source 'src:forged'"),
        ]
        for path, value, expected in cases:
            with self.subTest(path=path, value=value):
                self._assert_rejects(path, value, expected)

        # An omitted-but-included source contradicts the scope it sits beside.
        included_id = brief["included_scope"][0]
        contradicts = json.loads(json.dumps(brief))
        contradicts["omissions"].append(
            {"kind": "source", "ref": included_id, "reason": "budget"})
        issues = validate_context_brief(contradicts)
        self.assertTrue(any("marks an included source as omitted" in issue
                            for issue in issues), issues)

        repeated = json.loads(json.dumps(brief))
        repeated["omissions"].append(json.loads(json.dumps(repeated["omissions"][0])))
        issues = validate_context_brief(repeated)
        self.assertTrue(any("contains duplicate record" in issue for issue in issues),
                        issues)

        # Condensed evidence is a visible omission; a record for a kind the
        # schema does not know is not a substitute for it.
        orphan = json.loads(json.dumps(brief))
        orphan["omissions"].append({"kind": "guess", "ref": "x", "reason": ""})
        issues = validate_context_brief(orphan)
        self.assertTrue(any("records without a valid kind/ref pair" in issue
                            for issue in issues), issues)

    def test_validator_refuses_claims_whose_evidence_is_not_decision_critical(self):
        brief = json.loads(json.dumps(self._good_brief()))
        cited = brief["grounded_claims"][0]["evidence_refs"][0]
        for item in brief["evidence"]:
            if item["id"] == cited:
                item["decision_critical"] = False
        issues = validate_context_brief(brief)
        self.assertIn(
            f"grounded_claims[0] evidence {cited!r} is not marked decision-critical",
            issues)

    def test_validator_requires_an_omission_record_for_every_omission(self):
        brief = json.loads(json.dumps(self._good_brief()))
        dropped = json.loads(json.dumps(brief))
        dropped["omissions"] = dropped["omissions"][1:]
        issues = validate_context_brief(dropped)
        self.assertTrue(any("lacks an omission record" in issue for issue in issues),
                        issues)

        gap = create_context_brief(
            "Check the module surface",
            {"src/api.py": SAMPLE_SOURCE, "src/imports_only.py": "import os\n"},
            observed_at=OBSERVED_AT, max_tokens=2500,
        )
        excluded_id = gap["excluded_scope"][0]["source_id"]
        no_record = json.loads(json.dumps(gap))
        no_record["omissions"] = [
            item for item in no_record["omissions"]
            if not (item["kind"] == "source" and item["ref"] == excluded_id)
        ]
        issues = validate_context_brief(no_record)
        self.assertIn(f"excluded source {excluded_id!r} lacks an omission record",
                      issues)

    def test_validator_requires_a_truncation_reason_for_truncated_evidence(self):
        brief = create_context_brief(
            "Keep the context small",
            {"api.py": "\n".join(["class ImportantAPI:"]
                                 + [f"def helper_{index}(value: int) -> int: ..."
                                    for index in range(100)])},
            observed_at=OBSERVED_AT, max_tokens=1100,
        )
        truncated_id = brief["coverage"]["truncated_evidence_ids"][0]

        no_record = json.loads(json.dumps(brief))
        no_record["omissions"] = [
            item for item in no_record["omissions"]
            if not (item["kind"] == "evidence" and item["ref"] == truncated_id)
        ]
        issues = validate_context_brief(no_record)
        self.assertIn(f"truncated evidence {truncated_id!r} lacks an omission record",
                      issues)

        vague = json.loads(json.dumps(brief))
        for item in vague["omissions"]:
            if item["kind"] == "evidence" and item["ref"] == truncated_id:
                item["reason"] = "source bodies and other text omitted"
        issues = validate_context_brief(vague)
        self.assertIn(f"truncated evidence {truncated_id!r} lacks a truncation reason",
                      issues)

    def test_validator_refuses_broken_token_estimates(self):
        cases = [
            ("token_estimate", {"value": 5}, "token_estimate must contain value and kind only"),
            ("token_estimate", "estimated", "token_estimate must contain value and kind only"),
            ("token_estimate.value", 0, "token_estimate.value must be a positive integer"),
            ("token_estimate.value", True, "token_estimate.value must be a positive integer"),
            ("token_estimate.value", 999999,
             "token_estimate.value does not match the rendered estimate"),
        ]
        for path, value, expected in cases:
            with self.subTest(path=path, value=value):
                self._assert_rejects(path, value, expected)


class ContextBriefCreateRejectionTests(unittest.TestCase):
    """Creation refuses bad input loudly instead of minting a weak brief."""

    def _refuses(self, expected, goal="Check the input", **kwargs):
        kwargs.setdefault("observed_at", OBSERVED_AT)
        kwargs.setdefault("max_tokens", 2000)
        with self.assertRaises(HarnessError) as caught:
            create_context_brief(goal, {"src.py": SAMPLE_SOURCE}, **kwargs)
        self.assertIn(expected, str(caught.exception))
        return str(caught.exception)

    def test_create_refuses_unusable_scalars(self):
        self._refuses("context brief requires a non-empty goal", goal="   ")
        self._refuses("context brief requires a non-empty goal", goal=None)
        self._refuses("stage must be one of " + ", ".join(CONTEXT_BRIEF_STAGES),
                      stage="waist")
        self._refuses("max_tokens must be a positive integer", max_tokens=0)
        self._refuses("max_tokens must be a positive integer", max_tokens=True)
        with self.assertRaises(HarnessError) as caught:
            create_context_brief("Check the input", ["src.py"],
                                 observed_at=OBSERVED_AT, max_tokens=2000)
        self.assertIn("files must map source paths to text", str(caught.exception))
        self._refuses("error_log must be text", error_log=object())

    def test_create_refuses_unusable_focus_symbols(self):
        self._refuses("focus_symbols must be a sequence of symbol names",
                      focus_symbols=5)
        self._refuses("focus_symbols must be a sequence of symbol names, not text",
                      focus_symbols="evaluate")
        self._refuses("focus_symbols must contain non-empty strings",
                      focus_symbols=["evaluate", 5])
        self._refuses("focus_symbols must contain non-empty strings",
                      focus_symbols=["evaluate", ""])

    def test_create_refuses_unusable_source_timestamps(self):
        self._refuses("source_modified_at must map paths to timestamps",
                      source_modified_at="2026-09-24T15:30:00Z")
        self._refuses("source_modified_at keys must be source paths",
                      source_modified_at={7: "2026-09-24T15:30:00Z"})
        self._refuses("source_modified_at contains a path outside the supplied scope",
                      source_modified_at={"src/other.py": "2026-09-24T15:30:00Z"})

    def test_create_refuses_the_reserved_error_log_path(self):
        # context:error_log is how a failure trace is carried; letting a caller
        # shadow it would make one source's evidence masquerade as another's.
        with self.assertRaises(HarnessError) as caught:
            create_context_brief(
                "Check the input", {"context:error_log": "boom\n"},
                observed_at=OBSERVED_AT, max_tokens=2000,
            )
        self.assertIn("source path 'context:error_log' is reserved",
                      str(caught.exception))

    def test_create_refuses_timestamps_it_cannot_canonicalize(self):
        cases = [
            ("nonsense", "observed_at must be an ISO-8601 timestamp"),
            (1758782400, "observed_at must be an ISO-8601 timestamp"),
            ("2026-09-25T12:00:00", "observed_at must include a timezone"),
            (datetime(2026, 9, 25, 12, tzinfo=_ExplodingOffset()),
             "observed_at must be a valid timezone-aware timestamp"),
            (datetime(2026, 9, 25, 12, tzinfo=_EmptyOffset()),
             "observed_at must include a timezone"),
            (datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=2))),
             "observed_at must be a valid timezone-aware timestamp"),
            (datetime(9999, 12, 31, 23, tzinfo=timezone(timedelta(hours=-2))),
             "observed_at must be a valid timezone-aware timestamp"),
        ]
        for observed_at, expected in cases:
            with self.subTest(observed_at=observed_at):
                self._refuses(expected, observed_at=observed_at)

    def test_create_refuses_unusable_uncertainty_and_conflict_lists(self):
        for field in ("uncertainties", "conflicts"):
            with self.subTest(field=field):
                self._refuses(f"{field} must be a list of non-empty strings",
                              **{field: "prose"})
                self._refuses(f"{field}[0] must be a non-empty string",
                              **{field: ["   "]})

    def test_create_refuses_claims_that_are_not_grounded(self):
        cases = [
            ("prose", "claims must be a list of grounded claim objects"),
            (["prose"], "claim 0 must be an object"),
            ([{"text": "t", "confidence": "high"}], "claim 0 has unsupported fields"),
            ([{"text": "   "}], "claim 0 requires non-empty text"),
            ([{"text": "t", "evidence_refs": "ev:x"}],
             "claim 0 evidence_refs and source_paths must be lists"),
            ([{"text": "t", "source_paths": [7]}],
             "claim 0 source paths must be strings"),
            ([{"text": "t", "source_paths": ["src/absent.py"]}],
             "claim 0 source path has no extractable evidence: 'src/absent.py'"),
            ([{"text": "t", "evidence_refs": [7]}],
             "claim 0 evidence references must be strings"),
            ([{"text": "t"}], "claim 0 requires at least one evidence reference"),
        ]
        for claims, expected in cases:
            with self.subTest(claims=claims):
                with self.assertRaises(HarnessError) as caught:
                    create_context_brief(
                        "Check the input", {"src.py": SAMPLE_SOURCE},
                        claims=claims, observed_at=OBSERVED_AT, max_tokens=2000,
                    )
                self.assertIn(expected, str(caught.exception))

    def test_create_refuses_rather_than_reporting_an_unconverged_estimate(self):
        # The estimate is derived from the artifact, so a non-converging
        # estimator means the number on the artifact would be a guess.
        with patch("harness.context_brief.estimate_prompt_tokens",
                   side_effect=itertools.count(1)):
            with self.assertRaises(HarnessError) as caught:
                create_context_brief(
                    "Check the input", {"src.py": SAMPLE_SOURCE},
                    observed_at=OBSERVED_AT, max_tokens=2000,
                )
        self.assertIn("brief token estimate did not converge", str(caught.exception))


class ContextBriefExcerptTests(unittest.TestCase):
    """Budget pressure degrades evidence visibly instead of dropping it quietly."""

    BUDGET_REASON = "source evidence omitted to fit max_tokens"

    @staticmethod
    def _smallest_budget(files, accept, low=200, high=1600, step=10, **kwargs):
        """Smallest budget that still mints a brief satisfying `accept`."""
        for budget in range(low, high, step):
            try:
                brief = create_context_brief(
                    "Bound the context", files, observed_at=OBSERVED_AT,
                    max_tokens=budget, **kwargs)
            except HarnessError:
                continue
            if accept(brief):
                return budget, brief
        return None, None

    def test_a_declaration_longer_than_the_budget_is_cut_visibly(self):
        long_signature = "function api_0(" + ", ".join(
            f"argument{index:02d}" for index in range(40)) + ") {}"
        source = "\n".join(
            [long_signature] + [f"function api_{index}() {{}}" for index in range(1, 6)])
        budget, brief = self._smallest_budget(
            {"api.ts": source}, lambda item: bool(item["coverage"]["truncated_evidence_ids"]))
        self.assertIsNotNone(budget, "no scanned budget forced a truncation")

        evidence = brief["evidence"][0]
        self.assertTrue(evidence["truncated"])
        self.assertIn("truncated", evidence["content"])
        self.assertLess(len(evidence["content"]), len(source))
        self.assertEqual(validate_context_brief(brief, max_tokens=budget), [])

    def test_evidence_with_no_declarations_still_yields_an_excerpt(self):
        # A comment-only file has nothing "important" to keep, so the excerpt
        # falls back to a leading slice -- which still has to carry the marker.
        source = "\n".join(f"// note {index}: implementation detail"
                           for index in range(20))
        budget, brief = self._smallest_budget(
            {"notes.ts": source}, lambda item: bool(item["coverage"]["truncated_evidence_ids"]))
        self.assertIsNotNone(budget, "no scanned budget forced a truncation")

        evidence = brief["evidence"][0]
        self.assertTrue(evidence["truncated"])
        self.assertIn("// note 0", evidence["content"])
        self.assertIn("truncated", evidence["content"])
        self.assertEqual(validate_context_brief(brief, max_tokens=budget), [])

    def test_evidence_too_short_to_excerpt_is_excluded_by_budget_not_dropped(self):
        source = "// a\n// b\n// c\n"
        budget, brief = self._smallest_budget(
            {"src/evaluate.py": SAMPLE_SOURCE, "notes.ts": source},
            lambda item: any(row["source_id"] not in item["included_scope"]
                             for row in item["excluded_scope"]),
            claims=[{"text": "evaluate returns a boolean",
                     "source_paths": ["src/evaluate.py"]}],
        )
        self.assertIsNotNone(budget, "no scanned budget excluded the short source")

        excluded = brief["excluded_scope"]
        self.assertEqual(len(excluded), 1)
        self.assertEqual(excluded[0]["reason"], self.BUDGET_REASON)
        self.assertIn(("source", excluded[0]["source_id"]),
                      [(item["kind"], item["ref"]) for item in brief["omissions"]])
        self.assertEqual(brief["coverage"]["complete"], False)
        self.assertEqual(validate_context_brief(brief, max_tokens=budget), [])


class SuppliedBriefGuardTests(unittest.TestCase):
    """Guards on a brief handed in from outside this process.

    A later stage validates and renders without re-running intake, so the
    optional budget and freshness arguments are the only place a supplied
    artifact can be checked against live inputs -- and they have to refuse
    unusable arguments instead of trusting them.
    """

    def test_max_tokens_bound_is_checked_against_the_estimate_on_file(self):
        brief = two_source_brief()
        self.assertEqual(validate_context_brief(
            brief, max_tokens=brief["token_estimate"]["value"]), [])
        issues = validate_context_brief(brief, max_tokens=0)
        self.assertIn("max_tokens must be a positive integer", issues)
        issues = validate_context_brief(brief, max_tokens=1)
        self.assertTrue(any("exceeds max_tokens" in issue for issue in issues), issues)
        with self.assertRaises(HarnessError) as caught:
            render_context_brief(brief, max_tokens=1)
        self.assertIn("exceeds max_tokens", str(caught.exception))

    def test_source_contents_must_be_text_keyed_by_path(self):
        brief = two_source_brief()
        for contents in (["src/evaluate.py"], {7: 5}, {"src/evaluate.py": 5}):
            with self.subTest(contents=contents):
                issues = validate_context_brief(brief, source_contents=contents)
                self.assertTrue(
                    any("source_contents" in issue for issue in issues), issues)

    def test_freshness_sweep_reports_every_unsupplied_source(self):
        brief = two_source_brief()
        issues = validate_context_brief(brief, source_contents={})
        for source in brief["source_identity"]["sources"]:
            self.assertIn(f"source {source['path']!r} was not supplied "
                          "for freshness validation", issues)

        # A catalog the shape check already rejected must not derail the
        # freshness sweep on its way to reporting the same problems.
        broken = json.loads(json.dumps(brief))
        broken["source_identity"]["sources"] = ["not-a-source"]
        self.assertTrue(validate_context_brief(
            broken, source_contents={"src/evaluate.py": SAMPLE_SOURCE}))

    def test_render_refuses_an_invalid_supplied_brief(self):
        broken = json.loads(json.dumps(two_source_brief()))
        broken["goal"] = "   "
        with self.assertRaises(HarnessError) as caught:
            render_context_brief(broken)
        message = str(caught.exception)
        self.assertIn("cannot render invalid context brief", message)
        self.assertIn("goal must be a non-empty string", message)


if __name__ == "__main__":
    unittest.main()
