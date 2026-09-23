"""Panel/judge lane: rotation, retry, cost accounting, judge fallback, vote fidelity."""
import json
import os
import tempfile
import unittest

from harness.errors import HarnessError
from harness.panel import panel_judge
from tests._fake import FakeTransport, m, comp, _gov, P1, P2, JUDGE


class PanelJudgeTests(unittest.TestCase):
    def test_happy_path(self):
        fake = FakeTransport(models=[m(P1), m(P2), m(JUDGE)],
                             posts=[comp("take one"), comp("take two"),
                                    comp("verdict: agree")])
        gov = _gov(fake)
        result = panel_judge(transport=fake, api_key="k", governor=gov, prompt="Q?",
                             panel=[P1, P2], judge=JUDGE)
        self.assertEqual(len(result["panel_results"]), 2)
        self.assertEqual(result["judge_synthesis"], "verdict: agree")
        self.assertGreater(result["actual_cost"], 0.0)

    def test_panel_failure_skips_and_continues(self):
        fake = FakeTransport(models=[m(P1), m(P2), m(JUDGE)],
                             posts=[(500, {"error": {"message": "boom"}}),
                                    comp("take two"), comp("verdict")])
        gov = _gov(fake)
        result = panel_judge(transport=fake, api_key="k", governor=gov, prompt="Q?",
                             panel=[P1, P2], judge=JUDGE)
        self.assertEqual(len(result["panel_results"]), 1)

    def test_ordinary_ledger_run_reports_without_convergence_state(self):
        """The convergence tally is optional; an ordinary ledger-backed run must
        still complete and report null panel counts rather than referencing an
        uninitialized convergence variable."""
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "ledger.jsonl")
            fake = FakeTransport(models=[m(P1), m(P2), m(JUDGE)],
                                 posts=[comp("one"), comp("two"), comp("judge")])
            gov = _gov(fake)
            ledger = __import__("harness.ledger", fromlist=["AutonomyLedger"]).AutonomyLedger(path)
            result = panel_judge(transport=fake, api_key="k", governor=gov, prompt="Q?",
                                 panel=[P1, P2], judge=JUDGE, ledger=ledger,
                                 task_id="ordinary-ledger")
            self.assertEqual(result["judge_synthesis"], "judge")
            complete = ledger.entries()[-1]
            self.assertEqual(complete["event"], "complete")
            self.assertIsNone(complete["voted_by"])
            self.assertIsNone(complete["of_panel"])

    def test_panel_429_retry_is_bounded_and_billed(self):
        """A transient panel 429 gets one bounded retry, and both provider
        charges are reflected in governor and the ledger."""
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "ledger.jsonl")
            fake = FakeTransport(
                models=[m(P1), m(P2), m(JUDGE)],
                posts=[
                    (429, {"error": {"message": "rate limited"},
                           "usage": {"cost": 0.0002}}),
                    comp("retried panel", cost=0.0003),
                    comp("judge", cost=0.0001),
                ])
            gov = _gov(fake, max_cost=0.001)
            ledger = __import__("harness.ledger", fromlist=["AutonomyLedger"]).AutonomyLedger(path)
            result = panel_judge(transport=fake, api_key="k", governor=gov, prompt="Q?",
                                 panel=[P1, P2], judge=JUDGE, max_panelists=1,
                                 ledger=ledger, task_id="429-cost")
            self.assertEqual([r["model"] for r in result["panel_results"]], [P1])
            self.assertEqual(len(fake.chat_posts()), 3)
            self.assertAlmostEqual(gov.spent, 0.0006, places=9)
            self.assertAlmostEqual(ledger.participation_report()["tracked_cost"],
                                   gov.spent, places=9)
            self.assertLessEqual(gov.spent, gov.max_cost)
            retry_events = [e for e in ledger.entries()
                            if e["event"] == "model_result" and e.get("retry")]
            self.assertEqual(len(retry_events), 1)
            self.assertAlmostEqual(retry_events[0]["cost"], 0.0002, places=9)

    def test_rotation_cost_is_recorded_and_stays_within_ceiling(self):
        """A failed panel slot and its replacement are both billable ledger
        events, and the reported total matches governor.spent."""
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "ledger.jsonl")
            fake = FakeTransport(
                models=[m(P1), m(P2), m(JUDGE)],
                posts=[
                    (500, {"error": {"message": "down"},
                           "usage": {"cost": 0.0002}}),
                    comp("replacement", cost=0.0003),
                    comp("judge", cost=0.0001),
                ])
            gov = _gov(fake, max_cost=0.001)
            ledger = __import__("harness.ledger", fromlist=["AutonomyLedger"]).AutonomyLedger(path)
            result = panel_judge(transport=fake, api_key="k", governor=gov, prompt="Q?",
                                 panel=[P1, P2], judge=JUDGE, max_panelists=1,
                                 ledger=ledger, task_id="rotation-cost")
            report = ledger.participation_report()
            self.assertAlmostEqual(gov.spent, 0.0006, places=9)
            self.assertAlmostEqual(result["actual_cost"], gov.spent, places=9)
            self.assertAlmostEqual(report["tracked_cost"], gov.spent, places=9)
            self.assertLessEqual(gov.spent, gov.max_cost)
            self.assertEqual(len(result["panel_failures"]), 1)

    def test_all_panel_failures_abort(self):
        fake = FakeTransport(models=[m(P1), m(P2), m(JUDGE)],
                             posts=[(500, {"error": {"message": "x"}}),
                                    (500, {"error": {"message": "y"}})])
        gov = _gov(fake)
        with self.assertRaises(HarnessError):
            panel_judge(transport=fake, api_key="k", governor=gov, prompt="Q?",
                        panel=[P1, P2], judge=JUDGE)

    def test_judge_failure_falls_back_to_raw_panels(self):
        # The seat contract: a judge that cannot produce a verdict never
        # fabricates one -- the run defers with raw panel outputs. Rotation
        # gives the seat a bounded transient retry first (two 500s here) and
        # both panelists voted, so no fallback candidate remains.
        fake = FakeTransport(models=[m(P1), m(P2), m(JUDGE)],
                             posts=[comp("take one"), comp("take two"),
                                    (500, {"error": {"message": "judge down"}}),
                                    (500, {"error": {"message": "judge still down"}})])
        gov = _gov(fake)
        result = panel_judge(transport=fake, api_key="k", governor=gov, prompt="Q?",
                             panel=[P1, P2], judge=JUDGE)
        self.assertIsNone(result["judge_synthesis"])
        self.assertIn("raw panel outputs", result["verdict"])
        self.assertEqual(len(result["panel_results"]), 2)

    def test_truncation_flag_flows_into_judge_prompt(self):
        fake = FakeTransport(models=[m(P1), m(P2), m(JUDGE)],
                             posts=[comp("cut short", finish="length"),
                                    comp("fine"), comp("verdict")])
        gov = _gov(fake)
        result = panel_judge(transport=fake, api_key="k", governor=gov, prompt="Q?",
                             panel=[P1, P2], judge=JUDGE)
        truncated = {r["model"]: r["truncated"] for r in result["panel_results"]}
        self.assertTrue(truncated[P1])
        self.assertFalse(truncated[P2])
        judge_payload = fake.payloads()[-1]
        judge_user = judge_payload["messages"][-1]["content"]
        self.assertIn("NOTE: cut off by token limit", judge_user)


class PanelSeatingVisibilityTests(unittest.TestCase):
    """DF-BOD-1: a requested panelist the capability hard gate (or the
    catalog-unknown / learned-BYOK filters) drops BEFORE dispatch must be
    reported in ``panel_failures`` -- never silently absent from the seat
    count. ``max_panelists=N`` with fewer than N eligible models must seat
    exactly the eligible count, and say why the rest are missing."""

    GOOD = ["provider-a/capable-1", "provider-b/capable-2",
           "provider-c/capable-3", "provider-d/capable-4"]
    DROPPED = "google/gemini-3.8-flash"

    @staticmethod
    def _capable(model_id):
        # context_length + a declared JSON capability -> capability_score > 0
        # (harness/capability.py capability_score/context_score).
        return {"id": model_id,
                "pricing": {"prompt": "0.000001", "completion": "0.000002"},
                "context_length": 128000,
                "supported_parameters": ["structured_outputs"]}

    @staticmethod
    def _uncapable(model_id):
        # No context_length, no declared JSON support -> capability_score
        # == 0 -> order_pool's hard capability gate drops it entirely
        # (harness/capability.py order_pool: "capability <= 0) are dropped").
        return {"id": model_id,
                "pricing": {"prompt": "0.000001", "completion": "0.000002"}}

    def test_hard_gated_panelist_is_reported_not_silently_dropped(self):
        models = [self._capable(mid) for mid in self.GOOD] + [self._uncapable(self.DROPPED)]
        judge = self.GOOD[0]
        fake = FakeTransport(
            models=models,
            posts=[comp(f"vote {i}") for i in range(len(self.GOOD))] + [comp("verdict")])
        gov = _gov(fake, max_cost=1.0)
        panel = self.GOOD + [self.DROPPED]
        result = panel_judge(transport=fake, api_key="k", governor=gov, prompt="Q?",
                             panel=panel, judge=judge, max_panelists=5)
        # 5 requested, only 4 eligible (capability hard gate drops the 5th):
        # max_panelists=N with fewer than N eligible models must seat exactly
        # the eligible count.
        self.assertEqual(result["required_panelists"], len(self.GOOD))
        self.assertEqual(len(result["panel_results"]), len(self.GOOD))
        seated = {r["model"] for r in result["panel_results"]}
        self.assertNotIn(self.DROPPED, seated)
        # The dropped panelist must be visible in panel_failures, not silent.
        failure_models = {f["model"] for f in result["panel_failures"]}
        self.assertIn(self.DROPPED, failure_models,
                     "a requested panelist removed pre-dispatch by the "
                     "capability hard gate must be reported in panel_failures")


class VoteFidelityTests(unittest.TestCase):
    """#14: panel votes must reach the judge/specialist untruncated."""

    LONG = json.dumps({
        f"c{i}": {"real": i % 2 == 0, "confidence": 0.9,
                  "notes": "x" * 600}
        for i in range(4)
    })  # > 4500 chars once per panelist

    def test_judge_sees_full_votes(self):
        fake = FakeTransport(
            models=[m(P1), m(P2), m(JUDGE)],
            posts=[comp(self.LONG), comp(self.LONG),
                   comp(json.dumps({"verdict": "ok", "agreement": "high",
                                    "confidence": 1.0, "disagreements": [],
                                    "defer": False}))])
        gov = _gov(fake, max_cost=1.0)
        panel_judge(transport=fake, api_key="k", governor=gov,
                             prompt="Q?", panel=[P1, P2], judge=JUDGE)
        judge_payload = fake.payloads()[-1]["messages"][-1]["content"]
        self.assertIn("c3", judge_payload, "later claims must not be cut off")
        self.assertNotIn("...[truncated]", judge_payload)
        self.assertIn(self.LONG[:200], judge_payload)

    def test_specialist_sees_full_votes(self):
        spec = json.dumps({"converged": True, "agreement": "high",
                           "confidence": 1.0, "claims": {}})
        fake = FakeTransport(
            models=[m(P1), m(P2), m(JUDGE)],
            posts=[comp(self.LONG), comp(self.LONG), comp("judge"), comp(spec)])
        gov = _gov(fake, max_cost=1.0)
        panel_judge(transport=fake, api_key="k", governor=gov, prompt="Q?",
                    panel=[P1, P2], judge=JUDGE, run_convergence=True)
        spec_payload = fake.payloads()[-1]["messages"][-1]["content"]
        self.assertIn("c3", spec_payload)
        self.assertIn(self.LONG[:200], spec_payload)
        self.assertGreater(len(spec_payload), 4000)


class TallyFirstTests(unittest.TestCase):
    """H12: a resource-cap trim is not a transport shortfall. The tally
    counts full votes; only the judge prompt is trimmed, and the trim is
    reported as trimmed_for_judge -- never as panel_shortfall."""

    SPEC = "{\"converged\":true,\"agreement\":\"high\",\"confidence\":1.0,\"claims\":{}}"

    def _windowed(self):
        old_vote = json.dumps({"c1": {"real": True, "confidence": 0.9}})
        old_vote += "\nOLD-" + "x" * 12000
        new_vote = json.dumps({"c1": {"real": True, "confidence": 0.9}})
        new_vote += "\nNEW-" + "y" * 12000

        def _entry(mid, ctx):
            return {"id": mid, "pricing": {"prompt": "0.00000001",
                                           "completion": "0.00000002"},
                    "context_length": ctx}
        # NOTE: JUDGE == P1 in the shared fakes, so the judge gets its own
        # narrow-window id; the panelists share wide identical profiles, so
        # the tie keeps pool order (deterministic on every interpreter): P1
        # is seat 1 and casts old_vote, P2 is seat 2 and casts new_vote.
        self._judge = "judge/small-window"
        fake = FakeTransport(
            models=[_entry(P1, 200000), _entry(P2, 200000),
                    _entry(self._judge, 8192)],
            posts=[comp(old_vote), comp(new_vote), comp("judge"),
                   comp(self.SPEC)])
        return fake, _gov(fake, max_cost=1.0)

    def test_trim_does_not_shortfall_the_gate(self):
        fake, gov = self._windowed()
        result = panel_judge(
            transport=fake, api_key="k", governor=gov, prompt="Q?",
            panel=[P1, P2], judge=self._judge, run_convergence=True,
            convergence_model=self._judge)
        tally = result["convergence"]["tally"]
        # Full votes: 2/2 responders, no transport failure anywhere.
        self.assertEqual(tally["voted_by"], 2)
        self.assertFalse(tally["panel_shortfall"])
        self.assertTrue(tally["converged"])
        self.assertTrue(result["convergence"]["tally"]["claims"]["c1"]
                        ["gate_unanimous"])
        # ...while the judge prompt took the trim, disclosed separately.
        self.assertEqual(result["trimmed_for_judge"], [P1])
        self.assertEqual(len(result["panel_results"]), 2)
        self.assertFalse(result["consensus"]["defer"])
        self.assertNotEqual(result["consensus"].get("defer_reason"),
                            "panel_shortfall")

    def test_no_trim_reports_empty_list(self):
        fake = FakeTransport(
            models=[m(P1), m(P2), m(JUDGE)],
            posts=[comp(json.dumps({"c1": {"real": True, "confidence": 0.9}})),
                   comp(json.dumps({"c1": {"real": True, "confidence": 0.9}})),
                   comp("judge"), comp(self.SPEC)])
        gov = _gov(fake, max_cost=1.0)
        result = panel_judge(
            transport=fake, api_key="k", governor=gov, prompt="Q?",
            panel=[P1, P2], judge=JUDGE, run_convergence=True)
        self.assertEqual(result["trimmed_for_judge"], [])
        self.assertTrue(result["convergence"]["tally"]["converged"])


if __name__ == "__main__":
    unittest.main()
