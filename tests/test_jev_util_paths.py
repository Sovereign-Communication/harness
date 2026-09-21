"""Extra JEV-P3 policy-path tests that execute refusal/fallback lines."""
import os
import tempfile
import unittest
from pathlib import Path

from harness.config import load_settings
from harness.jev import JevEvaluationResult
from harness.jev_packs import (
    claims_from_payload,
    heuristic_file_relevance,
    named_artifact_status,
    validate_candidates,
)
from harness.jev_policy import JevPolicy, policy_for
from harness.ledger import AutonomyLedger
from harness.spend import SpendGovernor
from tests._fake import FakeTransport, m


class _BoomTransport:
    def post(self, url, key, payload, timeout=45):
        raise RuntimeError("provider down")


class _EmptyEval:
    api_key = "jev-key"
    model = "jev-latest"

    def evaluate(self, state, questions):
        return JevEvaluationResult("fail", 0.0, 0.0, {}, ["boom"],
                                   is_fallback=False, model=self.model)


def _gov():
    return SpendGovernor(
        FakeTransport(models=[m("jev-test", prompt="0", completion="0")]),
        "sk-test", max_cost=0.50)


def _keyed():
    return load_settings({"jev_api_key": "jev-key"})


class PolicyRefusalPathTests(unittest.TestCase):
    def test_route_transport_failure_uses_heuristic_fallback(self):
        policy = policy_for(_keyed(), transport=_BoomTransport(), governor=_gov())
        result, structural = policy.evaluate_route(
            "loop forever", ["a.py"], site="route")
        self.assertTrue(result.is_fallback)
        self.assertTrue(structural["is_fallback"])
        self.assertEqual(result.answers["route"], "frontier")
        self.assertEqual(structural["site"], "route")

    def test_file_triage_transport_failure_keyword_fallback(self):
        policy = policy_for(_keyed(), transport=_BoomTransport(), governor=_gov())
        files = ["harness/dag.py", "README.md"]
        result, structural = policy.evaluate_file_triage(
            "fix dag", files, known_files=files, site="triage-files")
        self.assertTrue(result.is_fallback)
        self.assertIn("harness/dag.py", result.answers["files"])
        self.assertEqual(structural["files"], result.answers["files"])

    def test_claim_support_transport_failure_advisory_unknown(self):
        policy = policy_for(_keyed(), transport=_BoomTransport(), governor=_gov())
        result, structural = policy.evaluate_claim_support(
            [{"id": "c1", "text": "something"}], "ctx",
            enabled=True, site="claims")
        self.assertTrue(result.is_fallback)
        self.assertEqual(structural["claim_flags"][0]["supported"], None)

    def test_completion_transport_failure_degrades_to_fallback_not_live(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "ok.md").write_text("x\n", encoding="utf-8")
        policy = policy_for(_keyed(), transport=_BoomTransport(), governor=_gov())
        result, structural = policy.evaluate_completion_nouls(
            "write ok.md", "state", named_artifacts=["ok.md"],
            root_dir=root, site="completion")
        # Transport failure is not live judgment: honest fallback, artifacts
        # present so code does not invent a missing-artifact refuse.
        self.assertTrue(result.is_fallback)
        self.assertFalse(structural["cannot_complete"])
        self.assertEqual(structural["missing_artifacts"], [])

    def test_completion_missing_artifact_still_cannot_complete_on_transport_failure(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        policy = policy_for(_keyed(), transport=_BoomTransport(), governor=_gov())
        result, structural = policy.evaluate_completion_nouls(
            "write ghost.md", "state", named_artifacts=["ghost.md"],
            root_dir=root, site="completion")
        self.assertTrue(structural["cannot_complete"])
        self.assertIn("ghost.md", structural["missing_artifacts"])

    def test_keyed_eval_fail_answers_still_accounted(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        ledger = AutonomyLedger(os.path.join(td.name, "led.jsonl"))
        policy = policy_for(_keyed(), transport=_BoomTransport(),
                            governor=_gov(), ledger=ledger,
                            evaluator=_EmptyEval())
        result, structural = policy.evaluate_file_triage(
            "goal", ["a.py"], known_files=["a.py"], site="triage-files")
        self.assertFalse(result.is_fallback)
        self.assertEqual(result.answers["files"], [])
        self.assertEqual(structural["files"], [])
        sites = [e.get("site") for e in ledger.entries() if e["event"] == "jev_eval"]
        self.assertIn("triage-files", sites)


class PacksHelperCoverageTests(unittest.TestCase):
    def test_validate_candidates_without_listing_keeps_unique(self):
        self.assertEqual(
            validate_candidates(["a.py", "a.py", " b.py ", ""], None),
            ["a.py", "b.py"])

    def test_heuristic_file_relevance_empty_goal(self):
        self.assertEqual(heuristic_file_relevance("", ["a.py"]), [])

    def test_claims_from_payload_dict_and_plain(self):
        self.assertEqual(
            claims_from_payload({"claims": [{"id": "x", "text": "t"}]}),
            [{"id": "x", "text": "t"}])
        self.assertEqual(claims_from_payload(["plain"]), [{"id": "claim_0", "text": "plain"}])
        self.assertEqual(claims_from_payload(None), [])
        self.assertEqual(claims_from_payload(42), [])

    def test_named_artifact_status_from_goal_and_targets(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "real.py").write_text("print(1)\n", encoding="utf-8")
        facts = named_artifact_status(
            "Create real.py and ghost.py", target_files=["real.py"],
            root_dir=root)
        paths = {f["path"]: f["present"] for f in facts}
        self.assertTrue(paths.get("real.py"))
        self.assertFalse(paths.get("ghost.py", True))

    def test_jev_policy_settings_required(self):
        from harness.errors import HarnessError
        with self.assertRaises(HarnessError):
            JevPolicy(None)


if __name__ == "__main__":
    unittest.main()
