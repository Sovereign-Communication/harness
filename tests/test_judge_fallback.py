"""Hermetic unit tests for GAP-judge-fallback: deterministic tally fallback contract.

Validates that when all panel judges fail or truncate:
1. Deterministic per-claim tally fallback activates with `judge_fallback: True`,
   `deterministic_tally_fallback: True`, `draw: True`, and `verdict_status: "inconclusive"`.
2. Deterministic tally fallback can NEVER approve unverified code (`defer: True`).
3. Specialist fallback lane records honest inconclusive draw when all attempts fail.
"""
import json
import unittest

from harness.convergence import _parse_consensus, run_convergence_specialist
from harness.panel import panel_judge
from tests._fake import FakeTransport, m, comp, _gov, P1, P2, JUDGE


class TestJudgeFallback(unittest.TestCase):
    """Test deterministic judge fallback contracts."""

    def test_parse_consensus_unparseable_fallback_fields(self):
        """_parse_consensus marks unparseable output as an inconclusive draw fallback."""
        parsed = _parse_consensus("This is prose without any JSON at all.")
        self.assertTrue(parsed.get("judge_fallback"))
        self.assertEqual(parsed.get("verdict_status"), "inconclusive")
        self.assertTrue(parsed.get("draw"))
        self.assertTrue(parsed.get("defer"))
        self.assertEqual(parsed.get("defer_reason"), "unparseable_judge_output")

    def test_judge_unparseable_convergence_fallback(self):
        """When judges and specialist fail in convergence mode, tally fallback activates as draw."""
        claims = {"c1": {"real": False, "confidence": 0.95}}
        fake = FakeTransport(
            models=[m(P1), m(P2), m(JUDGE)],
            posts=[
                comp(json.dumps(claims)),
                comp(json.dumps(claims)),
                comp("judge says: unparseable prose response"),
                comp("specialist also failed to produce valid JSON"),
            ],
        )
        gov = _gov(fake)
        result = panel_judge(
            transport=fake,
            api_key="k",
            governor=gov,
            prompt="Is this diff correct?",
            panel=[P1, P2],
            judge=JUDGE,
            max_panelists=2,
            run_convergence=True,
        )

        consensus = result["consensus"]
        self.assertTrue(consensus.get("judge_fallback"))
        self.assertTrue(consensus.get("deterministic_tally_fallback"))
        self.assertEqual(consensus.get("verdict_status"), "inconclusive")
        self.assertTrue(consensus.get("draw"))
        # Must NEVER approve unverified code without a judge: defer must be True
        self.assertTrue(consensus.get("defer"))

        # Verify tally_artifact carries the same honest non-authoritative draw
        artifact = consensus.get("tally_artifact")
        self.assertIsNotNone(artifact)
        self.assertFalse(artifact["authoritative"])
        self.assertTrue(artifact.get("judge_fallback"))
        self.assertTrue(artifact.get("draw"))
        self.assertEqual(artifact.get("verdict_status"), "inconclusive")

    def test_judge_unparseable_unstructured_fallback(self):
        """In unstructured mode (run_convergence=False), unparseable judge marks draw fallback."""
        fake = FakeTransport(
            models=[m(P1), m(JUDGE)],
            posts=[
                comp("panel output"),
                comp("judge unparseable prose"),
            ],
        )
        gov = _gov(fake)
        result = panel_judge(
            transport=fake,
            api_key="k",
            governor=gov,
            prompt="Check diff",
            panel=[P1],
            judge=JUDGE,
            max_panelists=1,
            run_convergence=False,
        )

        consensus = result["consensus"]
        self.assertTrue(consensus.get("judge_fallback"))
        self.assertTrue(consensus.get("defer"))
        self.assertEqual(consensus.get("verdict_status"), "inconclusive")
        self.assertTrue(consensus.get("draw"))

    def test_convergence_specialist_exhausted_fallback(self):
        """run_convergence_specialist marks honest inconclusive draw when candidates fail."""
        fake = FakeTransport(
            models=[m(JUDGE)],
            posts=[
                comp("not json"),
            ],
        )
        gov = _gov(fake)
        panel_results = [{"model": P1, "content": '{"c1":{"real":true}}'}]
        res = run_convergence_specialist(
            transport=fake,
            api_key="k",
            governor=gov,
            panel_results=panel_results,
            model=JUDGE,
        )
        self.assertEqual(res["status"], "error")
        self.assertTrue(res.get("judge_fallback"))
        self.assertEqual(res.get("verdict_status"), "inconclusive")
        self.assertTrue(res.get("draw"))
        self.assertEqual(res.get("degrade_reason"), "specialist_unavailable")


if __name__ == "__main__":
    unittest.main()
