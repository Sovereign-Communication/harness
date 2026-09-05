import json
import os
import tempfile
import unittest
from harness.core import (
    SpendGovernor, HarnessError, estimate_prompt_tokens, extract_content_and_cost,
    panel_judge, tally_convergence, extract_claim_verdicts,
    run_convergence_specialist, _chat_reservation_slots,
)
from harness.config import OPENROUTER_CHAT_URL, OPENROUTER_MODELS_URL, OPENROUTER_KEY_URL
from tests._fake import FakeTransport, m, comp

P1 = "inclusionai/ling-2.6-flash"
P2 = "meta-llama/llama-3.1-8b-instruct"
JUDGE = "inclusionai/ling-2.6-flash"


def _gov(fake, **kw):
    return SpendGovernor(fake, "sk-test", **kw)


class CostMathTests(unittest.TestCase):
    def test_pricing_is_per_token_not_per_million(self):
        """Regression: OpenRouter pricing fields are per-token dollars. An
        earlier SCMessenger version divided by 1e6 a second time and
        undercounted worst-case cost ~1,000,000x. Costs here must land in the
        ~1e-5..1e-4 range, not ~1e-10."""
        fake = FakeTransport(models=[m(P1), m(P2), m(JUDGE)])
        gov = _gov(fake)
        prompt = " ".join(["word"] * 200)  # ~350 estimated tokens
        calls = [(P1, P1, 300, 0), (P2, P2, 300, 0), ("judge", JUDGE, 350, 700)]
        total, breakdown = gov.preflight(prompt, calls)
        pt = estimate_prompt_tokens(prompt)
        expected = (pt * 1e-8 + 300 * 2e-8) * 2 + (pt + 700) * 1e-8 + 350 * 2e-8
        self.assertAlmostEqual(total, expected, places=12)
        for _, model, cost in breakdown:
            self.assertGreater(cost, 1e-9, "cost is mis-scaled by orders of magnitude")
        self.assertEqual(gov.spent, 0.0, "preflight must not spend anything")

    def test_preflight_refuses_when_over_ceiling(self):
        fake = FakeTransport(models=[m(P1, "0.0001", "0.0002")])
        gov = _gov(fake, max_cost=0.01)
        with self.assertRaises(HarnessError):
            gov.preflight(" ".join(["word"] * 5000), [(P1, P1, 300, 0)])

    def test_unknown_model_refused(self):
        fake = FakeTransport(models=[m(P1)])
        gov = _gov(fake)
        with self.assertRaises(HarnessError):
            gov.preflight("hi", [("x", "nope/model", 10, 0)])

    def test_key_must_have_finite_limit(self):
        fake = FakeTransport(key={"label": "sk-test", "limit": None})
        gov = _gov(fake)
        with self.assertRaises(HarnessError):
            gov.verify_key()

    def test_expect_key_label_mismatch(self):
        fake = FakeTransport(key={"label": "sk-or-v1-aaaa", "limit": 1.0})
        gov = _gov(fake, expect_key_label="bbbb")
        with self.assertRaises(HarnessError):
            gov.verify_key()

    def test_expect_key_label_match(self):
        fake = FakeTransport(key={"label": "sk-or-v1-aaaa", "limit": 1.0,
                                  "limit_remaining": 0.5})
        gov = _gov(fake, expect_key_label="aaaa")
        info = gov.verify_key()
        self.assertEqual(info["label"], "sk-or-v1-aaaa")


class GuardTests(unittest.TestCase):
    def test_no_tools_key_ever(self):
        fake = FakeTransport(models=[m(P1), m(P2), m(JUDGE)],
                             posts=[comp("a"), comp("b"), comp("verdict")])
        gov = _gov(fake)
        panel_judge(transport=fake, api_key="k", governor=gov, prompt="Q?",
                    panel=[P1, P2], judge=JUDGE)
        for payload in fake.payloads():
            self.assertNotIn("tools", payload)

    def test_byok_denied_before_any_post(self):
        fake = FakeTransport(models=[m(P1), m("anthropic/claude-3.5-sonnet")])
        gov = _gov(fake)
        with self.assertRaises(HarnessError) as ctx:
            panel_judge(transport=fake, api_key="k", governor=gov, prompt="Q?",
                        panel=[P1, "anthropic/claude-3.5-sonnet"], judge=JUDGE)
        self.assertIn("BYOK", str(ctx.exception))
        self.assertEqual(fake.chat_posts(), [], "no chat call may go out")

    def test_mid_batch_fail_closed(self):
        fake = FakeTransport(models=[m(P1), m(P2), m(JUDGE)],
                             posts=[comp("a", cost=0.0009), comp("b", cost=0.0009)])
        gov = _gov(fake, max_cost=0.001)
        with self.assertRaises(HarnessError):
            panel_judge(transport=fake, api_key="k", governor=gov, prompt="Q?",
                        panel=[P1, P2], judge=JUDGE)
        # judge must never have been called
        self.assertEqual(len(fake.chat_posts()), 2)


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
        fake = FakeTransport(models=[m(P1), m(P2), m(JUDGE)],
                             posts=[comp("take one"), comp("take two"),
                                    (500, {"error": {"message": "judge down"}})])
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


class ConvergenceTests(unittest.TestCase):
    def panel(self, claims):
        return [{"model": P1, "content": __import__("json").dumps(claims)},
                {"model": P2, "content": __import__("json").dumps(claims)}]

    def test_tally_unanimous_converges(self):
        claims = {"c1": {"real": True, "confidence": 0.9},
                  "c2": {"real": False, "confidence": 0.8},
                  "c3": {"real": True, "confidence": 0.95}}
        tally = tally_convergence(self.panel(claims))
        self.assertTrue(tally["converged"])
        self.assertEqual(tally["converged_claims"], 3)
        self.assertEqual(tally["total_claims"], 3)
        self.assertEqual(tally["convergence_rate"], 1.0)
        self.assertEqual(tally["claims"]["c1"]["verdict"], "real")
        self.assertEqual(tally["claims"]["c2"]["verdict"], "not_real")

    def test_tally_split_does_not_converge(self):
        a = {"c1": {"real": True, "confidence": 0.9}, "c2": {"real": False, "confidence": 0.8}}
        b = {"c1": {"real": False, "confidence": 0.7}, "c2": {"real": False, "confidence": 0.8}}
        tally = tally_convergence([{"model": P1, "content": __import__("json").dumps(a)},
                                   {"model": P2, "content": __import__("json").dumps(b)}])
        self.assertFalse(tally["converged"])
        self.assertEqual(tally["converged_claims"], 1)
        self.assertEqual(tally["convergence_rate"], 0.5)

    def test_malformed_panelist_is_shortfall_not_disagreement(self):
        """A valid responder's unanimous vote is agreement, not disagreement,
        but the missing required slot keeps the merge gate closed."""
        claims = {"c1": {"real": False, "confidence": 0.9}}
        fake = FakeTransport(
            models=[m(P1), m(P2), m(JUDGE)],
            posts=[
                comp("not valid claim JSON"),
                comp(json.dumps(claims)),
                comp('{"verdict":"ok","agreement":"high","confidence":0.9,'
                     '"disagreements":[],"defer":false}'),
                comp('{"converged":false,"agreement":"low","confidence":0.5,"claims":{}}'),
            ])
        gov = _gov(fake)
        result = panel_judge(transport=fake, api_key="k", governor=gov, prompt="Q?",
                             panel=[P1, P2], judge=JUDGE, max_panelists=2,
                             run_convergence=True)
        tally = result["convergence"]["tally"]
        self.assertFalse(tally["converged"])
        self.assertTrue(tally["responder_converged"])
        self.assertFalse(tally["disagreement"])
        self.assertTrue(tally["panel_shortfall"])
        self.assertEqual(tally["voted_by"], 1)
        self.assertEqual(tally["of_panel"], 2)
        self.assertEqual(tally["missing_votes"], 1)
        self.assertEqual(result["consensus"]["agreement"], "high")
        self.assertEqual(result["consensus"]["confidence"], 1.0)
        self.assertTrue(result["consensus"]["defer"])
        self.assertEqual(len(result["panel_failures"]), 1)
        self.assertEqual(result["panel_failures"][0]["status"], "invalid_output")

    def test_reassurance_claims_excluded_from_gate(self):
        """Identical substance on a reassurance claim encoded with opposite real
        polarity (statement-truth vs defect-presence) must NOT split the tally:
        reassurance claims are excluded from the convergence gate."""
        # c1 is a defect claim both agree on; c2 is reassurance: one model reads
        # real:true as "the correctness statement holds", the other as "no defect"
        # -- i.e. they AGREE substantively but encode it oppositely.
        a = {"c1": {"real": True, "confidence": 0.9},
             "c2": {"real": True, "confidence": 0.99}}   # statement-truth convention
        b = {"c1": {"real": True, "confidence": 0.85},
             "c2": {"real": False, "confidence": 0.99}}  # defect-presence convention
        tally = tally_convergence([{"model": P1, "content": __import__("json").dumps(a)},
                                   {"model": P2, "content": __import__("json").dumps(b)}],
                                  claim_polarity={"c2": "reassurance"})
        self.assertTrue(tally["converged"], "reassurance split must not block convergence")
        self.assertEqual(tally["converged_claims"], 1)
        self.assertEqual(tally["total_claims"], 1, "only defect claims gate the tally")
        self.assertEqual(tally["convergence_rate"], 1.0)
        self.assertIn("c2", tally["reassurance"])
        self.assertIn("excluded", tally["reassurance"]["c2"]["note"])

    def test_undeclared_reassurance_claim_still_gates(self):
        """Without a claim_polarity declaration the claim is treated as a defect
        proposition (real:true = defect present), so opposite real votes split."""
        a = {"c1": {"real": True, "confidence": 0.9}}
        b = {"c1": {"real": False, "confidence": 0.8}}
        tally = tally_convergence([{"model": P1, "content": __import__("json").dumps(a)},
                                   {"model": P2, "content": __import__("json").dumps(b)}])
        self.assertFalse(tally["converged"])
        self.assertEqual(tally["total_claims"], 1)

    def test_extract_claim_verdicts_ignores_non_claim_json(self):
        self.assertEqual(extract_claim_verdicts('{"verdict":"x","note":"y"}'), {})
        self.assertEqual(extract_claim_verdicts("no json here"), {})
        got = extract_claim_verdicts('{"c1":{"real":true,"confidence":0.9}}')
        self.assertEqual(got["c1"]["real"], True)

    def test_convergence_step_overrides_consensus_when_converged(self):
        claims = {"c1": {"real": True, "confidence": 0.9},
                  "c2": {"real": False, "confidence": 0.85}}
        spec = ("{\"converged\":true,\"agreement\":\"high\",\"confidence\":1.0,"
                "\"claims\":{}}")
        fake = FakeTransport(models=[m(P1), m(P2), m(JUDGE)],
                             posts=[comp(__import__("json").dumps(claims)),
                                    comp(__import__("json").dumps(claims)),
                                    comp("{\"verdict\":\"x\",\"agreement\":\"medium\","
                                         "\"confidence\":0.6,\"disagreements\":[],"
                                         "\"defer\":false}"),
                                    comp(spec)])
        gov = _gov(fake)
        result = panel_judge(transport=fake, api_key="k", governor=gov, prompt="Q?",
                             panel=[P1, P2], judge=JUDGE, run_convergence=True)
        conv = result["convergence"]
        self.assertTrue(conv["tally"]["converged"])
        # converged 5/5 => consensus lifted to high / 1.0 (the ground truth rate)
        self.assertEqual(result["consensus"]["agreement"], "high")
        self.assertEqual(result["consensus"]["confidence"], 1.0)
        self.assertEqual(conv["model"], JUDGE, "specialist defaults to the judge model")

    def test_convergence_step_keeps_consensus_when_split(self):
        a = {"c1": {"real": True, "confidence": 0.9}}
        b = {"c1": {"real": False, "confidence": 0.7}}
        judge_v = ("{\"verdict\":\"x\",\"agreement\":\"low\",\"confidence\":0.4,"
                    "\"disagreements\":[\"d\"],\"defer\":true}")
        fake = FakeTransport(models=[m(P1), m(P2), m(JUDGE)],
                             posts=[comp(__import__("json").dumps(a)),
                                    comp(__import__("json").dumps(b)),
                                    comp(judge_v),
                                    comp("{\"converged\":false,\"agreement\":\"low\","
                                         "\"confidence\":0.4,\"claims\":{}}")])
        gov = _gov(fake)
        result = panel_judge(transport=fake, api_key="k", governor=gov, prompt="Q?",
                             panel=[P1, P2], judge=JUDGE, run_convergence=True)
        self.assertFalse(result["convergence"]["tally"]["converged"])
        self.assertEqual(result["consensus"]["agreement"], "low", "judge verdict kept when split")


class SpecialistRotationTests(unittest.TestCase):
    """The convergence specialist is a rotating lane, not a single shot."""

    SPEC = "{\"converged\":true,\"agreement\":\"high\",\"confidence\":1.0,\"claims\":{}}"

    def _panel(self, fake, spec_model):
        """Run a 2-panelist + judge convergence run; return (result, fake)."""
        claims = {"c1": {"real": False, "confidence": 0.9}}
        gov = _gov(fake)
        result = panel_judge(
            transport=fake, api_key="k", governor=gov, prompt="Q?",
            panel=[P1, P2], judge=JUDGE, run_convergence=True,
            convergence_model=spec_model,
            specialist_pool=["z-ai/glm-5.2:free"])
        return result

    def test_specialist_rotates_on_http_error(self):
        """A specialist that HTTP-errors is rotated to the fallback ladder."""
        fake = FakeTransport(
            models=[m(P1), m(P2), m(JUDGE), m("z-ai/glm-5.2:free")],
            posts=[comp(json.dumps({"c1": {"real": False, "confidence": 0.9}})),
                   comp(json.dumps({"c1": {"real": False, "confidence": 0.9}})),
                   comp("judge"),
                   (500, {"error": {"message": "overloaded"}}),
                   comp(self.SPEC)])
        result = self._panel(fake, JUDGE)
        conv = result["convergence"]
        self.assertEqual(conv["status"], "ok")
        self.assertEqual(conv["model"], "z-ai/glm-5.2:free",
                         "specialist must land on the fallback after the primary failed")
        statuses = [a["status"] for a in conv.get("attempts", [])]
        self.assertEqual(statuses, ["error", "ok"])

    def test_specialist_rotates_on_reasoning_only(self):
        """Reasoning-only output must be treated as imperfect and rotated, never
        JSON-mined from the hidden trace."""
        fake = FakeTransport(
            models=[m(P1), m(P2), m(JUDGE), m("z-ai/glm-5.2:free")],
            posts=[comp(json.dumps({"c1": {"real": False, "confidence": 0.9}})),
                   comp(json.dumps({"c1": {"real": False, "confidence": 0.9}})),
                   comp("judge"),
                   comp(None, reasoning='{"converged":true,"agreement":"high"}'),
                   comp(self.SPEC)])
        result = self._panel(fake, JUDGE)
        conv = result["convergence"]
        self.assertEqual(conv["status"], "ok")
        self.assertEqual(conv["model"], "z-ai/glm-5.2:free")
        failed = [a for a in conv.get("attempts", []) if a["status"] == "error"]
        self.assertEqual(len(failed), 1)
        self.assertIn("reasoning-only", failed[0]["error"])

    def test_specialist_rotates_on_unparseable_then_recovers(self):
        """Prose with no JSON rotates to the next candidate."""
        fake = FakeTransport(
            models=[m(P1), m(P2), m(JUDGE), m("z-ai/glm-5.2:free")],
            posts=[comp(json.dumps({"c1": {"real": False, "confidence": 0.9}})),
                   comp(json.dumps({"c1": {"real": False, "confidence": 0.9}})),
                   comp("judge"),
                   comp("I think the models broadly agree, nice work everyone."),
                   comp(self.SPEC)])
        result = self._panel(fake, JUDGE)
        conv = result["convergence"]
        self.assertEqual(conv["status"], "ok")
        self.assertEqual(conv["model"], "z-ai/glm-5.2:free")

    def test_specialist_rotation_keeps_ledger_cost_consistent(self):
        """Every billed attempt appears in the ledger and total cost accumulates
        across rotations; a fully-failed lane still reports the tally."""
        import tempfile
        from harness.ledger import AutonomyLedger
        with tempfile.TemporaryDirectory() as td:
            led_path = os.path.join(td, "led.jsonl")
            ledger = AutonomyLedger(led_path)
            fake = FakeTransport(
                models=[m(P1), m(P2), m(JUDGE), m("z-ai/glm-5.2:free")],
                posts=[comp(json.dumps({"c1": {"real": False, "confidence": 0.9}}), cost=0.0),
                       comp(json.dumps({"c1": {"real": False, "confidence": 0.9}}), cost=0.0),
                       comp("judge", cost=0.0),
                       comp("prose no json", cost=0.000003),
                       comp(self.SPEC, cost=0.000004)])
            gov = _gov(fake)
            result = panel_judge(
                transport=fake, api_key="k", governor=gov, prompt="Q?",
                panel=[P1, P2], judge=JUDGE, run_convergence=True,
                convergence_model=JUDGE, specialist_pool=["z-ai/glm-5.2:free"],
                ledger=ledger, task_id="rot1")
            conv = result["convergence"]
            self.assertEqual(conv["status"], "ok")
            # The failed attempt's cost plus the successful one's must land in
            # the governor total and the ledger.
            self.assertAlmostEqual(result["actual_cost"], 0.000007, places=9)
            events = [json.loads(l) for l in open(led_path, encoding="utf-8")]
            conv_events = [e for e in events if e.get("event_note") == "convergence"]
            self.assertEqual(len(conv_events), 2)
            self.assertAlmostEqual(sum(e.get("cost", 0) for e in conv_events),
                                   0.000007, places=9)
            statuses = sorted(e["status"] for e in conv_events)
            self.assertEqual(statuses, ["error", "ok"])

    def test_specialist_prompt_discloses_caps(self):
        """The specialist prompt must state the output-token cap, the hidden
        reasoning cap, and the rotate-on-truncation consequence."""
        claims = {"c1": {"real": False, "confidence": 0.9}}
        fake = FakeTransport(
            models=[m(P1), m(P2), m(JUDGE)],
            posts=[comp(json.dumps(claims)),
                   comp(json.dumps(claims)),
                   comp("judge"),
                   comp(self.SPEC)])
        result = self._panel(fake, JUDGE)
        specialist_post = fake.chat_posts()[-1]
        prompt = specialist_post[2]["messages"][0]["content"]
        self.assertIn("RESOURCE CAP", prompt)
        self.assertIn("output tokens", prompt)
        self.assertIn("reasoning itself capped at", prompt)
        self.assertIn("rotated to another model", prompt)

    def test_specialist_unknown_fallback_is_skipped_not_fatal(self):
        """A fallback model missing from the live list is skipped; the run
        still completes on the primary."""
        claims = {"c1": {"real": False, "confidence": 0.9}}
        fake = FakeTransport(
            models=[m(P1), m(P2), m(JUDGE)],
            posts=[comp(json.dumps(claims)),
                   comp(json.dumps(claims)),
                   comp("judge"),
                   comp(self.SPEC)])
        gov = _gov(fake)
        result = panel_judge(
            transport=fake, api_key="k", governor=gov, prompt="Q?",
            panel=[P1, P2], judge=JUDGE, run_convergence=True,
            convergence_model=JUDGE,
            specialist_pool=["ghost/model-that-does-not-exist"])
        conv = result["convergence"]
        self.assertEqual(conv["status"], "ok")
        self.assertEqual(conv["model"], JUDGE)

    def test_reservation_covers_reasoning_fallback_slots(self):
        """A reasoning-capable specialist reserves 2 preflight slots (the
        possible no-reasoning retry), a plain model reserves 1."""
        self.assertEqual(_chat_reservation_slots("z-ai/glm-5.2:free"), 2)
        self.assertEqual(_chat_reservation_slots(P1), 1)


class ExtractionTests(unittest.TestCase):
    def test_reasoning_fallback(self):
        content, finish, cost, is_byok = extract_content_and_cost(
            comp(None, reasoning="deep thinking here"))
        self.assertIn("[NOTE]", content)
        self.assertIn("deep thinking", content)

    def test_extraction_garbage(self):
        content, finish, cost, is_byok = extract_content_and_cost({})
        self.assertIsNone(content)
        self.assertIsNone(finish)


if __name__ == "__main__":
    unittest.main()