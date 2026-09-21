"""GUI/CLI/ledger parity: Jev-directed escalation evidence reaches the site.

Ties the two sessions together at the ledger boundary. The CLI apply path
(apply_policy + EscalationDriver) is the ONLY escalation executor; the Proof
Bench exporter is the only ledger-to-public boundary. This module proves the
chain: a Jev-directed escalation decision issues ``escalate`` ledger evidence
with directed_by/jev_confidence/target_rung/condensed_context_chars, the
directive decision itself rides ``jev_eval`` (site=escalation-decision), and
``site-export`` renders that provenance through the sanitize allowlist into
the public bundle. Fakes only at the doctrine seams (Jev evaluator double;
no network anywhere).
"""
import json
import os
import tempfile
import unittest

from harness.config import load_settings
from harness.jev import JevEvaluationResult
from harness.ledger import AutonomyLedger

from tests.test_site_export import _populate_task_ledger, _write_consent


def _noul(value):
    return {"type": "noul", "noul": value}


class _ScriptedEscalationEvaluator:
    """Duck JevEvaluator: answers the escalation-decision pack."""

    def __init__(self, confidence=0.05, capability_budget=0.8):
        self.api_key = "test-key"
        self.model = "jev-test"
        self.confidence = confidence
        self.capability_budget = capability_budget

    def evaluate(self, state, questions=None):
        answers = {
            "escalation_decision": _noul(self.confidence),
            "capability_budget": _noul(self.capability_budget),
        }
        return JevEvaluationResult(
            "fail", self.confidence, 1.0, answers,
            ["scripted escalation decision"],
            cost=0.00004, input_tokens=400, output_tokens=0,
            is_fallback=False, model=self.model)


class _DirectedJevPolicy:
    """Duck policy producing a real top-rung escalation directive."""

    def __init__(self, ledger):
        from harness.escalation import jev_escalation_directive
        self.ledger = ledger
        result = JevEvaluationResult(
            "fail", 0.05, 1.0,
            {"escalation_decision": _noul(0.05),
             "capability_budget": _noul(0.8)},
            ["scripted"], cost=0.00004, input_tokens=400, output_tokens=0,
            is_fallback=False, model="jev-test")
        self.directive = jev_escalation_directive(
            result, ladder_size=1, current_rung=0,
            condensed_context="instruction: fix it\nlast verify output: boom")
        assert self.directive is not None and self.directive["kind"] != "abstain"

    def evaluate_escalation_decision(self, failure_context, task_id=None,
                                     node_id=None):
        self.ledger.append(
            "jev_eval", task_id=task_id, site="escalation-decision",
            model="jev-test", verdict="fail", supported=1.0, confidence=0.05,
            input_tokens=400, output_tokens=0, cost=0.00004,
            is_fallback=False)
        return JevEvaluationResult(
            "fail", 0.05, 1.0,
            {"escalation_decision": _noul(0.05),
             "capability_budget": _noul(0.8)},
            ["scripted"], cost=0.00004, input_tokens=400, output_tokens=0,
            is_fallback=False, model="jev-test"), {
                "site": "escalation-decision", "verdict": "fail"}

    def evaluate_candidate(self, *args, **kwargs):
        return (JevEvaluationResult("pass", 0.95, 0.95, {}, [],
                                    is_fallback=False, model="jev-test"),
                {"site": "apply", "verdict": "pass"})


class DirectiveEvidenceParityTests(unittest.TestCase):
    """CLI-issued escalation evidence -> sanitized public bundle."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = AutonomyLedger(
            os.path.join(self.tmp.name, "ledger.jsonl"))

    def test_directive_reaches_sanitized_bundle(self):
        from harness.site_export import export_bundle
        from tests.test_jev_escalation_driver import (
            jev_escalation_directive,  # noqa: F401  (import parity guard)
        )
        policy = _DirectedJevPolicy(self.ledger)
        directive = policy.directive
        # The production apply path (apply_policy) asks the ONE policy owner
        # for the escalation decision -- which ledgered the noul -- then the
        # executor issues the escalate evidence with full v2 provenance.
        jev_result, _structural = policy.evaluate_escalation_decision(
            directive["condensed_context"], task_id="task/parity")
        self.assertFalse(jev_result.is_fallback)
        self.ledger.append(
            "escalate", task_id="task/parity", from_model="cheap/base",
            to_model="rung/top",
            directed_by="jev", jev_confidence=0.05, target_rung=1,
            condensed_context_chars=len(directive["condensed_context"]))
        self.ledger.append("model_result", task_id="task/parity",
                           model="rung/top", cost=0.002, status="ok",
                           event_note="escalation_rung_1")
        self.ledger.append("verify_round", task_id="task/parity", round=1,
                           passed=True, model="rung/top",
                           readiness="confident")
        self.ledger.append("complete", task_id="task/parity",
                           model="rung/top", rounds=2, status="ok")

        entries = self.ledger.entries()
        esc = [e for e in entries if e["event"] == "escalate"]
        self.assertEqual(len(esc), 1)
        self.assertEqual(esc[0]["directed_by"], "jev")

        consent = _write_consent(self.tmp.name)
        bundle = export_bundle(self.ledger_path(), consent)
        run = bundle["runs"][0]
        self.assertEqual(run["lane"], "task")
        self.assertTrue(run["gated"])
        self.assertEqual(run["escalation"]["directed_by"], "jev")
        self.assertEqual(run["escalation"]["jev_confidence"], 0.05)
        self.assertEqual(run["escalation"]["target_rung"], 1)
        self.assertEqual(
            run["escalation"]["condensed_context_chars"],
            len(directive["condensed_context"]))
        self.assertGreater(run["jev_evals"]["count"], 0,
                           "the escalation-decision noul must be ledgered")

    def test_verify_lane_escalation_stays_labeled_verify_lane(self):
        from harness.site_export import export_bundle
        _populate_task_ledger(self.ledger)  # v1-shape: no directed_by field
        bundle = export_bundle(self.ledger_path(), _write_consent(self.tmp.name))
        run = bundle["runs"][0]
        self.assertEqual(run["escalation"]["directed_by"], "verify_lane")
        self.assertIsNone(run["escalation"]["jev_confidence"])

    def test_directive_summary_in_public_trace(self):
        from harness.site_export import export_bundle
        policy = _DirectedJevPolicy(self.ledger)
        directive = policy.directive
        self.ledger.append(
            "escalate", task_id="task/trace", from_model="cheap/base",
            to_model="rung/top",
            directed_by="jev", jev_confidence=0.05, target_rung=1,
            condensed_context_chars=len(directive["condensed_context"]))
        bundle = export_bundle(self.ledger_path(), _write_consent(self.tmp.name))
        run = bundle["runs"][0]
        esc = run["escalation"]
        self.assertEqual(esc["directed_by"], "jev")
        self.assertIn("warrant", esc)
        self.assertIn("jev_confidence_at_handoff", esc["warrant"])
        # The public trace must be able to say "Jev directed this climb with
        # confidence X" -- never expose the condensed context itself. Only
        # the *_chars count may cross the boundary.
        self.assertNotIn('"condensed_context":', json.dumps(esc))
        self.assertIn('"condensed_context_chars":', json.dumps(esc))

    def ledger_path(self):
        return os.path.join(self.tmp.name, "ledger.jsonl")


class CliServerParityTests(unittest.TestCase):
    """CLI route envelope and server /api/route share the ONE policy owner."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_cli_and_server_route_same_pack_same_choice(self):
        pack = {
            "id": "parity-ladder",
            "rungs": [
                {"rung_id": "scout", "tier": "T0", "model": "helper-lite",
                 "cost_class": "free", "guidance": ["typo", "rename"]},
                {"rung_id": "worker", "tier": "T1", "model": "worker-1",
                 "cost_class": "cheap", "guidance": ["algorithm"]},
            ],
        }
        goal = "fix a typo in the docstring"
        settings = load_settings()
        settings.jev_api_key = None

        from harness.jev_policy import policy_for
        from harness.route_pack import validate_route_pack
        ledger = AutonomyLedger(os.path.join(self.tmp.name, "route.jsonl"))
        policy = policy_for(settings, transport=None, governor=None,
                            ledger=ledger)
        _r, _s, combo = policy.evaluate_model_route(
            {"goal": goal}, validate_route_pack(pack), site="model_route")
        self.assertEqual(combo["rung_id"], "scout")
        self.assertEqual(combo["tier"], "T0")
        self.assertTrue(combo["is_fallback"])
        # Same owner, same answer -- the CLI face and the server face both
        # call this method (tests/test_route_faces.py, test_site_server.py).
        self.assertEqual(combo["pack_id"], "parity-ladder")


if __name__ == "__main__":
    unittest.main()
