"""Convergence specialist rotation: fallback ladder, disclosure, cost integrity."""
import json
import os
import tempfile
import unittest

from harness.panel import panel_judge
from harness.chat import _chat_reservation_slots
from tests._fake import FakeTransport, m, comp, _gov, P1, P2, JUDGE


class SpecialistRotationTests(unittest.TestCase):
    """The convergence specialist is a rotating lane, not a single shot."""

    SPEC = "{\"converged\":true,\"agreement\":\"high\",\"confidence\":1.0,\"claims\":{}}"

    def _panel(self, fake, spec_model):
        """Run a 2-panelist + judge convergence run; return (result, fake)."""
        gov = _gov(fake)
        result = panel_judge(
            transport=fake, api_key="k", governor=gov, prompt="Q?",
            panel=[P1, P2], judge=JUDGE, run_convergence=True,
            convergence_model=spec_model,
            specialist_pool=["z-ai/glm-5.2:free"])
        return result

    def test_shortfall_panel_discloses_incomplete_coverage_in_specialist_block(self):
        """Live finding: on a 1-of-3 shortfall panel the specialist block read
        'converged: true, agreement: high' next to a tally that said
        converged: false. The specialist block must disclose the coverage gap
        so its consensus is never mistaken for coverage-complete."""
        judge_v = ("{\"verdict\":\"x\",\"agreement\":\"low\",\"confidence\":0.4,"
                   "\"claims\":{}}")
        rate_limited = (429, {"error": {"message": "rate limited"}})
        fake = FakeTransport(
            models=[m(P1), m(P2), m(JUDGE), m(JUDGE)],
            posts=[comp(json.dumps({"c1": {"real": True, "confidence": 0.9}})),
                   rate_limited, rate_limited, rate_limited, rate_limited,
                   comp(judge_v),
                   comp(self.SPEC)])
        gov = _gov(fake)
        result = panel_judge(transport=fake, api_key="k", governor=gov, prompt="Q?",
                             panel=[P1, P2], judge=JUDGE, run_convergence=True)
        conv = result["convergence"]
        self.assertTrue(conv["tally"]["panel_shortfall"])
        self.assertIn("NOT coverage-complete", conv["specialist"]["note"])

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
        led_path = os.path.join(tempfile.mkdtemp(), "led.jsonl")
        from harness.ledger import AutonomyLedger
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
        with open(led_path, encoding="utf-8") as ledger_file:
            events = [json.loads(led) for led in ledger_file]
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
        self._panel(fake, JUDGE)
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

    def test_specialist_prompt_names_reassurance_polarity(self):
        """The panel prompt marks reassurance claims as inverted polarity;
        the specialist prompt must carry the same note or it inverts them
        in prose while the tally stays right."""
        from harness.convergence import _polarity_note
        self.assertIn("no reassurance", _polarity_note(None).lower())
        self.assertIn("no reassurance", _polarity_note({}).lower())
        note = _polarity_note({"c1": "defect", "c2": "reassurance"})
        self.assertIn("c2", note)
        self.assertNotIn("c1", note)
        self.assertIn("opposite polarity", note)

    def test_specialist_trims_to_smallest_candidate_window(self):
        """A smaller-window specialist primary must see trimmed votes with
        a disclosure -- not a silently truncated prompt that burns the
        ladder on truncation-rotations."""
        old_vote = json.dumps({"c1": {"real": True, "confidence": 0.9}})
        old_vote += "\nOLD-" + "x" * 12000
        new_vote = json.dumps({"c1": {"real": True, "confidence": 0.9}})
        new_vote += "\nNEW-" + "y" * 12000

        def _entry(mid, ctx):
            return {"id": mid, "pricing": {"prompt": "0.00000001",
                                           "completion": "0.00000002"},
                    "context_length": ctx}
        spec_id = "spec/small-window"
        # Wide windows everywhere except the specialist primary: the judge
        # guard keeps both votes, so only the specialist trim fires. (Plain
        # fixture entries score zero capability and yield profiles=None,
        # which disables every budget guard -- hence the explicit windows.)
        fake = FakeTransport(
            models=[_entry(P1, 200000), _entry(P2, 200000),
                    _entry(spec_id, 8192)],
            posts=[comp(old_vote), comp(new_vote), comp("judge"),
                   comp(self.SPEC)])
        gov = _gov(fake)
        result = panel_judge(
            transport=fake, api_key="k", governor=gov, prompt="Q?",
            panel=[P1, P2], judge=JUDGE, run_convergence=True,
            convergence_model=spec_id, specialist_pool=[])
        conv = result["convergence"]
        self.assertEqual(conv["status"], "ok")
        # Panel fan-out completes in nondeterministic order, so either
        # seat may be the trimmed oldest -- but exactly one vote goes and
        # the tally (computed from full votes upstream) still decides.
        self.assertEqual(len(conv["dropped_votes_from_prompt"]), 1)
        prompt = fake.chat_posts()[-1][2]["messages"][0]["content"]
        markers = [m for m in ("OLD-", "NEW-") if m in prompt]
        self.assertEqual(len(markers), 1)
        self.assertIn("deterministic tally remains authoritative", prompt)
        self.assertEqual(len(conv["tally"]["claims"]), 1)

    def test_specialist_no_trim_without_known_windows(self):
        """Unknown windows mean no trim (cost is still preflighted and a
        truncation rotates fail-closed) -- never a crash on missing data."""
        from harness.convergence import _trim_votes_to_window
        lines = ["--- Model: a ---\n{}", "--- Model: b ---\n{}"]
        kept, dropped = _trim_votes_to_window(lines, None, ["m"], "head", 64)
        self.assertEqual(kept, lines)
        self.assertEqual(dropped, [])

    def test_reassurance_claim_ids_reach_specialist_prompt(self):
        votes = json.dumps({"c1": {"real": True, "confidence": 0.9},
                            "c2": {"real": True, "confidence": 0.8}})
        fake = FakeTransport(
            models=[m(P1), m(P2), m(JUDGE), m(JUDGE)],
            posts=[comp(votes), comp(votes), comp("judge"),
                   comp(self.SPEC)])
        gov = _gov(fake)
        panel_judge(transport=fake, api_key="k", governor=gov, prompt="Q?",
                    panel=[P1, P2], judge=JUDGE, run_convergence=True,
                    convergence_model=JUDGE,
                    claim_polarity={"c2": "reassurance"})
        prompt = fake.chat_posts()[-1][2]["messages"][0]["content"]
        self.assertIn("c2", prompt)
        self.assertIn("REASSURANCE", prompt)


if __name__ == "__main__":
    unittest.main()
