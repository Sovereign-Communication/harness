"""HV-2: the evidence-bearing brief.

The contract, in the canon's words: extend the condenser/brief path to a
reusable, bounded artifact carrying source identity/freshness, scope
coverage, grounded references, uncertainties/conflicts, and a token
estimate; preserve useful evidence under pruning; make truncation and
omission visible; and give callers independent create/validate/render
behavior so a brief can be supplied without running intake.

Every assertion here is about HONESTY under pressure -- a drifted source, a
budget that ran out, a conflict with no citation. A brief that lies about
what it contains is worse than no brief, because a downstream seat bills
tokens against it.
"""
import unittest

from harness.brief import (
    BRIEF_SCHEMA_VERSION,
    build_brief,
    estimate_brief_tokens,
    freshness_report,
    render_brief,
    validate_brief,
)
from harness.errors import HarnessError

BIG = "".join("line {0}\n".format(n) for n in range(1, 3001))


def _reader(files):
    def read(path):
        return files[path]
    return read


class EvidenceFieldsTests(unittest.TestCase):
    def test_pack_is_versioned_and_carries_the_four_evidence_families(self):
        pack = build_brief("audit the ledger", ["a.py"],
                           reader=_reader({"a.py": "def f():\n    return 1\n"}),
                           now=1_700_000_000.0)
        self.assertEqual(pack["schema_version"], BRIEF_SCHEMA_VERSION)
        # freshness / source identity
        self.assertEqual(pack["built_at"], "2023-11-14T22:13:20Z")
        (source,) = pack["grounding"]["sources"]
        self.assertIn("bytes", source)
        self.assertIn("observed_at", source)
        # scope coverage
        self.assertIn("scope", pack)
        self.assertIn("coverage", pack)
        # uncertainty + conflicts are declared, even when empty
        self.assertEqual(pack["grounding"]["unknowns"], [])
        self.assertEqual(pack["conflicts"], [])
        # size
        self.assertIsInstance(pack["estimated_tokens"], int)
        self.assertGreater(pack["estimated_tokens"], 0)
        self.assertEqual(validate_brief(
            pack, reader=_reader({"a.py": "def f():\n    return 1\n"})), [])

    def test_token_estimate_is_measured_on_the_shipped_bytes(self):
        small = build_brief("g", ["a.py"],
                            reader=_reader({"a.py": "one line\n"}))
        large = build_brief("g", ["a.py"],
                            reader=_reader({"a.py": "one line\n" * 5000}))
        self.assertLess(small["estimated_tokens"], large["estimated_tokens"])
        # The recorded value is measured BEFORE the field itself is added --
        # a number cannot include its own serialization. So a recompute over
        # the finished pack is a lower bound, and close to it. What must
        # never happen is a wild disagreement, which would mean the recorded
        # number does not describe the pack a consumer is holding.
        recomputed = estimate_brief_tokens(small)
        self.assertLessEqual(small["estimated_tokens"], recomputed)
        self.assertLess(recomputed - small["estimated_tokens"],
                        small["estimated_tokens"] // 10 + 1)

    def test_an_unserializable_pack_reports_zero_tokens_instead_of_raising(self):
        # A downstream seat preflights a budget against this number. A pack it
        # cannot serialize must not crash the preflight -- and must not
        # invent a plausible number either.
        self.assertEqual(estimate_brief_tokens({"windows": {"a", "b"}}), 0)

    def test_a_budget_below_one_character_omits_rather_than_overruns(self):
        files = {"a.py": "alpha\n"}
        pack = build_brief("g", ["a.py"], reader=_reader(files),
                           max_total_chars=0.5)
        self.assertEqual(pack["windows"], [])
        self.assertEqual(pack["omitted"], ["a.py"])
        self.assertEqual(pack["coverage"]["omitted"], 1)
        self.assertIn("a.py", render_brief(pack))


class OmissionVisibilityTests(unittest.TestCase):
    def test_a_source_the_budget_cannot_fit_is_recorded_as_omitted(self):
        files = {"a.py": "small\n", "b.py": "x" * 9000, "c.py": "y" * 9000}
        pack = build_brief("g", list(files), reader=_reader(files),
                           max_total_chars=300)
        # The budget is genuinely binding now, not decorative.
        self.assertLessEqual(
            sum(len(w["content"]) for w in pack["windows"]), 300)
        # a.py fits whole, b.py fits as an honestly labeled head excerpt, and
        # c.py has no budget left at all -- so it is OMITTED, and stays named.
        self.assertEqual(sorted(pack["omitted"]), ["c.py"])
        by_path = {w["path"]: w for w in pack["windows"]}
        self.assertFalse(by_path["a.py"]["truncated"])
        self.assertTrue(by_path["b.py"]["truncated"])
        self.assertIn("ONLY these lines", by_path["b.py"]["note"])
        self.assertEqual(pack["coverage"]["omitted"], 1)
        self.assertEqual(pack["coverage"]["gaps"], pack["omitted"])
        # And the omission is visible in the rendered artifact a human reads.
        self.assertIn("c.py", render_brief(pack))
        self.assertEqual(validate_brief(pack, reader=_reader(files)), [])

    def test_truncation_stays_labeled_and_span_exact(self):
        pack = build_brief("g", ["big.py"], reader=_reader({"big.py": BIG}))
        (window,) = pack["windows"]
        self.assertTrue(window["truncated"])
        self.assertIn("ONLY these lines", window["note"])
        self.assertLess(window["end_line"], 3001)
        self.assertEqual(pack["coverage"]["truncated_windows"], 1)
        # The window is still a verifiable span of its pinned source.
        self.assertEqual(validate_brief(
            pack, reader=_reader({"big.py": BIG})), [])


class FreshnessTests(unittest.TestCase):
    def test_a_pack_whose_sources_still_match_is_fresh(self):
        files = {"a.py": "v1\n"}
        pack = build_brief("g", ["a.py"], reader=_reader(files))
        report = freshness_report(pack, reader=_reader(files))
        self.assertTrue(report["fresh"])
        self.assertEqual(report["checked"], 1)
        self.assertEqual(report["stale"], [])
        self.assertEqual(report["reasons"], [])

    def test_a_drifted_source_is_reported_not_raised(self):
        files = {"a.py": "v1\n"}
        pack = build_brief("g", ["a.py"], reader=_reader(files))
        files["a.py"] = "v2 drifted\n"
        report = freshness_report(pack, reader=_reader(files))
        self.assertFalse(report["fresh"])
        self.assertEqual([s["id"] for s in report["stale"]], ["s1"])
        self.assertIn("pinned sha256", report["reasons"][0])

    def test_an_unreadable_source_is_reported_not_raised(self):
        files = {"a.py": "v1\n"}
        pack = build_brief("g", ["a.py"], reader=_reader(files))

        def missing(path):
            raise FileNotFoundError(path)

        report = freshness_report(pack, reader=missing)
        self.assertFalse(report["fresh"])
        self.assertEqual(len(report["missing"]), 1)
        self.assertEqual(report["missing"][0]["error"], "FileNotFoundError")
        self.assertIn("could not be read", report["reasons"][0])

    def test_a_brief_with_no_sources_is_not_vacuously_fresh(self):
        report = freshness_report({"grounding": {"sources": []}})
        self.assertFalse(report["fresh"])
        self.assertIn("unknown", report["reasons"][0])
        self.assertFalse(freshness_report("not a pack")["fresh"])


class ConflictAndValidationTests(unittest.TestCase):
    def _pack(self, **kwargs):
        return build_brief("g", ["a.py", "b.py"],
                           reader=_reader({"a.py": "alpha\n", "b.py": "beta\n"}),
                           **kwargs)

    def test_the_rendered_brief_names_the_scope_that_was_excluded(self):
        pack = build_brief("g", ["a.py"],
                           reader=_reader({"a.py": "alpha\n"}),
                           scope=["vendor/", "docs/"])
        self.assertEqual(pack["scope"]["excluded"], ["vendor/", "docs/"])
        self.assertEqual(pack["scope"]["included"], ["a.py"])
        self.assertIn("excluded from scope: vendor/, docs/",
                      render_brief(pack))

    def test_a_conflict_must_cite_the_sources_it_came_from(self):
        pack = self._pack(conflicts=[
            {"description": "a.py and b.py disagree on the default",
             "source_ids": ["s1", "s2"]}])
        self.assertEqual(len(pack["conflicts"]), 1)
        self.assertEqual(validate_brief(
            pack, reader=_reader({"a.py": "alpha\n", "b.py": "beta\n"})), [])
        self.assertIn("s1, s2", render_brief(pack))

    def test_uncited_and_unknown_source_conflicts_are_refused(self):
        files = {"a.py": "alpha\n", "b.py": "beta\n"}
        uncited = self._pack(conflicts=[{"description": "vibes"}])
        self.assertTrue(any("uncited conflict" in i
                            for i in validate_brief(uncited, reader=_reader(files))))
        unknown = self._pack(conflicts=[
            {"description": "x", "source_ids": ["s99"]}])
        self.assertTrue(any("unknown source" in i
                            for i in validate_brief(unknown, reader=_reader(files))))

    def test_coverage_counters_must_not_disagree_with_the_artifact(self):
        files = {"a.py": "alpha\n", "b.py": "beta\n"}
        pack = self._pack()
        pack["coverage"]["sources_declared"] = 99
        self.assertTrue(any("disagrees" in i
                            for i in validate_brief(pack, reader=_reader(files))))
        pack = self._pack()
        pack["coverage"]["omitted"] = 7
        self.assertTrue(any("disagrees" in i
                            for i in validate_brief(pack, reader=_reader(files))))
        pack = self._pack()
        pack["coverage"]["gaps"] = ["ghost.py"]
        self.assertTrue(any("gaps must match" in i
                            for i in validate_brief(pack, reader=_reader(files))))

    def test_malformed_evidence_fields_are_refused_rather_than_trusted(self):
        files = {"a.py": "alpha\n", "b.py": "beta\n"}
        pack = self._pack()
        pack["omitted"] = "a.py"  # a bare string hides what it stands for
        self.assertTrue(any("omitted must be a list" in i for i in validate_brief(
            pack, reader=_reader(files))))
        for bad in ("a.py disagrees with b.py", 7):
            pack = self._pack(conflicts=[bad])
            self.assertTrue(any("must be an object" in i for i in validate_brief(
                pack, reader=_reader(files))), bad)
        pack = self._pack(conflicts=[{"source_ids": ["s1"]}])
        self.assertTrue(any("needs a description" in i for i in validate_brief(
            pack, reader=_reader(files))))

    def test_a_bad_token_estimate_or_schema_is_refused(self):
        files = {"a.py": "alpha\n", "b.py": "beta\n"}
        for bad in (None, -1, "many", True, 1.5):
            pack = self._pack()
            pack["estimated_tokens"] = bad
            self.assertTrue(any("estimated_tokens" in i for i in validate_brief(
                pack, reader=_reader(files))), bad)
        pack = self._pack()
        pack["schema_version"] = 1
        self.assertTrue(any("schema_version" in i for i in validate_brief(
            pack, reader=_reader(files))))


class IndependentUseTests(unittest.TestCase):
    """A caller may be HANDED a brief and still use it without intake."""

    def test_render_and_validate_need_no_intake_for_a_foreign_pack(self):
        # Produced elsewhere, with a reader that must never be consulted.
        foreign = {
            "schema_version": BRIEF_SCHEMA_VERSION,
            "goal": "review the migration",
            "built_at": "2026-01-01T00:00:00Z",
            "grounding": {"rules": [], "sources": [
                {"id": "s1", "path": "gone.py", "sha256": "0" * 64,
                 "lines": 1, "bytes": 4, "observed_at": 1.0}],
                "claims": [], "unknowns": ["the schema of the old table"]},
            "windows": [{"source_id": "s1", "path": "gone.py",
                         "start_line": 1, "end_line": 1, "truncated": False,
                         "content": "alpha\n"}],
            "scope": {"included": ["gone.py"], "excluded": []},
            "coverage": {"sources_declared": 1, "windows_cited": 1,
                         "omitted": 0, "truncated_windows": 0, "gaps": []},
            "omitted": [], "conflicts": [],
            "estimated_tokens": 12,
        }

        reads = []

        def counting_reader(path):
            reads.append(path)
            raise FileNotFoundError(path)

        rendered = render_brief(foreign)
        self.assertIn("review the migration", rendered)
        self.assertIn("unknowns:", rendered)
        self.assertIn("the schema of the old table", rendered)
        # Rendering a supplied brief reads nothing: no intake, no I/O.
        self.assertEqual(reads, [])

        # Validation, by contrast, MUST re-read and notice the source is gone.
        issues = validate_brief(foreign, reader=counting_reader)
        self.assertTrue(any("unreadable" in i for i in issues))
        self.assertTrue(reads, "validation must re-read the cited source")

    def test_render_rejects_a_non_object_pack(self):
        for bad in (None, [], "brief", 3):
            with self.assertRaises(HarnessError):
                render_brief(bad)

    def test_a_supplied_brief_can_be_re_rendered_after_being_carried(self):
        files = {"a.py": "alpha\n"}
        pack = build_brief("g", ["a.py"], reader=_reader(files))
        # Simulate a transport hop: the pack survives as data alone.
        import json
        carried = json.loads(json.dumps(pack))
        self.assertEqual(render_brief(carried), render_brief(pack))
        self.assertEqual(validate_brief(
            carried, reader=_reader(files)), [])


class MicroBriefReconciliationTests(unittest.TestCase):
    """HV-2: condenser.MicroBrief is reconciled onto the v2 schema.

    Every MicroBrief must carry the schema-v2 evidence pack for the exact
    bytes it condensed -- same files, same sha256 pins -- so the condensed
    view and the evidence record can never drift apart. These assertions
    are the condensation-parity gate: the two views describe one artifact.
    """

    FILES = {
        "a.py": "def f():\n    return 1\n",
        "b.py": "class B:\n    def m(self):\n        return 2\n",
    }

    def _micro(self, **kwargs):
        from harness.condenser import distill_context
        return distill_context(files=dict(self.FILES),
                               summary="audit the ledger",
                               now=1_700_000_000.0, **kwargs)

    def test_micro_brief_carries_a_valid_v2_pack(self):
        micro = self._micro()
        pack = micro.to_brief_pack()
        self.assertEqual(pack["schema_version"], BRIEF_SCHEMA_VERSION)
        self.assertEqual(validate_brief(pack, reader=_reader(self.FILES)), [])

    def test_pack_pins_the_exact_condensed_sources(self):
        micro = self._micro()
        pack = micro.to_brief_pack()
        cited = {s["path"]: s for s in pack["grounding"]["sources"]}
        condensed = dict(micro.file_signatures)
        self.assertEqual(set(cited), set(condensed))
        for path, source in cited.items():
            self.assertEqual(source["sha256"], _sha(self.FILES[path]))
            self.assertEqual(source["bytes"],
                             len(self.FILES[path].encode("utf-8")))

    def test_pruned_condensation_still_cites_every_source(self):
        # Under a brutal token cap the prompt view shrinks, but the evidence
        # pack still names every condensed file: pruning may narrow what
        # ships, never what is acknowledged.
        micro = self._micro(max_tokens=1)
        pack = micro.to_brief_pack()
        self.assertEqual({s["path"] for s in pack["grounding"]["sources"]},
                         set(self.FILES))
        self.assertEqual(validate_brief(pack, reader=_reader(self.FILES)), [])

    def test_freshness_reports_drift_as_a_finding_not_a_crash(self):
        pack = self._micro().to_brief_pack()
        changed = dict(self.FILES)
        changed["a.py"] = "def f():\n    return 999  # drifted\n"
        report = freshness_report(pack, reader=_reader(changed))
        self.assertFalse(report["fresh"])
        self.assertEqual([s["path"] for s in report["stale"]], ["a.py"])
        intact = freshness_report(pack, reader=_reader(self.FILES))
        self.assertTrue(intact["fresh"])

    def test_pack_estimate_is_measured_by_the_v2_owner(self):
        pack = self._micro().to_brief_pack()
        # The v2 owner measures the bytes the pack ships (the estimate field
        # cannot measure itself), and the condenser's prompt estimate is a
        # different, separately-tracked number.
        shipped = {k: v for k, v in pack.items() if k != "estimated_tokens"}
        self.assertEqual(pack["estimated_tokens"], estimate_brief_tokens(shipped))
        self.assertGreater(pack["estimated_tokens"], 0)

    def test_render_brief_needs_no_io_for_the_condensed_pack(self):
        pack = self._micro().to_brief_pack()
        rendered = render_brief(pack)
        self.assertIn("audit the ledger", rendered)
        self.assertIn("schema: v{0}".format(BRIEF_SCHEMA_VERSION), rendered)
        self.assertIn("a.py", rendered)

    def test_to_prompt_context_shape_is_unchanged_by_the_reconciliation(self):
        text = self._micro().to_prompt_context()
        self.assertIn("CONTEXT SUMMARY", text)
        self.assertIn("CONDENSED FILE INTERFACES", text)
        self.assertIn("File: a.py", text)


def _sha(text):
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


if __name__ == "__main__":
    unittest.main()
