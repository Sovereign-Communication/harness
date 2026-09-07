"""Convergence lane: claim tallying, polarity gates, and consensus merges."""
import json
import unittest

from harness.convergence import (
    extract_claim_verdicts,
    tally_convergence,
)
from harness.panel import panel_judge
from tests._fake import FakeTransport, m, comp, _gov, P1, P2, JUDGE


class ConvergenceTests(unittest.TestCase):
    def panel(self, claims):
        return [{"model": P1, "content": json.dumps(claims)},
                {"model": P2, "content": json.dumps(claims)}]

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
        tally = tally_convergence([{"model": P1, "content": json.dumps(a)},
                                   {"model": P2, "content": json.dumps(b)}])
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
        tally = tally_convergence([{"model": P1, "content": json.dumps(a)},
                                   {"model": P2, "content": json.dumps(b)}],
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
        tally = tally_convergence([{"model": P1, "content": json.dumps(a)},
                                   {"model": P2, "content": json.dumps(b)}])
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
                             posts=[comp(json.dumps(claims)),
                                    comp(json.dumps(claims)),
                                    comp('{"verdict":"x","agreement":"medium",'
                                         '"confidence":0.6,"disagreements":[],'
                                         '"defer":false}'),
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
                             posts=[comp(json.dumps(a)),
                                    comp(json.dumps(b)),
                                    comp(judge_v),
                                    comp('{"converged":false,"agreement":"low",'
                                         '"confidence":0.4,"claims":{}}')])
        gov = _gov(fake)
        result = panel_judge(transport=fake, api_key="k", governor=gov, prompt="Q?",
                             panel=[P1, P2], judge=JUDGE, run_convergence=True)
        self.assertFalse(result["convergence"]["tally"]["converged"])
        self.assertEqual(result["consensus"]["agreement"], "low", "judge verdict kept when split")


if __name__ == "__main__":
    unittest.main()
