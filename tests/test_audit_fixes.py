"""Regressions for the audit-driven fix pass (hermetic, no network)."""
import json
import os
import tempfile
import unittest

from harness.capability import probe_json_reliability
from harness.core import (
    SpendGovernor, estimate_prompt_tokens, panel_judge,
)
from harness.config import DEFAULT_JUDGE_PAID, FREE_JUDGE
from harness.ledger import AutonomyLedger
from tests._fake import FakeTransport, m, comp

P1 = "inclusionai/ling-2.6-flash"
P2 = "meta-llama/llama-3.1-8b-instruct"
JUDGE = "inclusionai/ling-2.6-flash"


def _gov(fake, **kw):
    return SpendGovernor(fake, "sk-test", **kw)


class JudgeRecurationTests(unittest.TestCase):
    def test_default_judge_is_the_proven_emitter(self):
        """north-mini burned its budget on hidden reasoning in live runs; the
        default judge must be the model with the best JSON track record."""
        self.assertEqual(FREE_JUDGE, "google/gemma-4-31b-it:free")
        self.assertNotEqual(DEFAULT_JUDGE_PAID, "cohere/north-mini-code")

    def test_north_mini_demoted_in_free_lanes(self):
        from harness.config import FREE_PANEL_POOL, FREE_APPLY_POOL
        for pool in (FREE_PANEL_POOL, FREE_APPLY_POOL):
            self.assertNotEqual(pool[0], "cohere/north-mini-code:free",
                                "a reasoning-fragile model must not lead a lane")
            self.assertIn("cohere/north-mini-code:free", pool,
                          "demoted, not removed")
            self.assertEqual(pool[-1], "openrouter/free")


class TokenEstimateTests(unittest.TestCase):
    def test_symbol_dense_text_not_undercounted(self):
        """Regression: words*1.5 undercounts symbol-dense source; chars/4 must
        dominate there so preflight ceilings stay honest."""
        code = "{" * 300 + "x" + "}" * 300  # 1 'word', 601 chars
        est = estimate_prompt_tokens(code)
        self.assertGreaterEqual(est, len(code) // 4,
                                "chars/4 floor must apply to dense code")
        prose = " ".join(["word"] * 300)
        self.assertGreater(estimate_prompt_tokens(prose), 400)

    def test_empty_and_prose(self):
        self.assertEqual(estimate_prompt_tokens(""), 50)
        self.assertGreater(estimate_prompt_tokens("hello world"), 0)


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


class LedgerCorruptionTests(unittest.TestCase):
    def test_torn_trailing_line_quarantined_not_crash(self):
        """The chain must stay readable and appendable after a torn write."""
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "led.jsonl")
            led = AutonomyLedger(path)
            led.append("offer", task_id="t1", model="m")
            with open(path, "a", encoding="utf-8") as f:
                f.write('{"seq": 2, "hash": "abc", "trunc')  # torn line
            led2 = AutonomyLedger(path)  # must not raise
            self.assertEqual(len(led2.entries()), 1)
            self.assertEqual(led2.quarantined, 1)
            # The repaired chain stays appendable and internally consistent.
            led2.append("complete", task_id="t1", model="m")
            ok, bad = led2.verify()
            self.assertTrue(ok, "intact prefix + new entry must hash-chain")
            self.assertIsNone(bad)

    def test_shape_broken_line_quarantined(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "led.jsonl")
            with open(path, "w", encoding="utf-8") as f:
                f.write('"just a string"\n[1, 2]\n')
            led = AutonomyLedger(path)
            self.assertEqual(len(led.entries()), 0)
            self.assertEqual(led.quarantined, 2)


class ProbeCeilingTests(unittest.TestCase):
    def test_probe_preflights_against_ceiling(self):
        """#4: the capability probe loop must not spend through the ceiling."""
        captured = []

        class Gov:
            max_cost = 0.02
            spent = 0.015  # only $0.005 remains

            def check_byok(self, m): pass

            def preflight(self, prompt_text, calls):
                captured.append((prompt_text, calls))
                if self.spent + 0.006 > self.max_cost:
                    from harness.core import HarnessError
                    raise HarnessError("worst-case estimate exceeds ceiling. Refusing.")

        _fake = FakeTransport(models=[m("m1")])
        res = probe_json_reliability("t", "k", Gov(), ["m1"], max_tokens=16)
        self.assertEqual(len(captured), 5, "each question is preflighted")
        self.assertEqual(res["m1"]["errors"], 5, "blocked questions count as errors")
        self.assertEqual(res["m1"]["calls"], 5)
        self.assertEqual(res["m1"]["json_ok_rate"], 0.0)


class CliCeilingWiringTests(unittest.TestCase):
    def _run(self, argv, captured):
        from unittest import mock
        from harness import cli

        def fake_governor(settings, max_cost_override=None):
            captured["override"] = max_cost_override
            gov = mock.Mock()
            gov.verify_key.return_value = None
            gov.max_cost = max_cost_override or settings.max_cost
            gov.spent = 0.0
            gov.preflight.return_value = (0.0, [])
            gov.check_byok.return_value = None
            gov.learned_blocked.return_value = False
            gov.record_actual.return_value = None
            gov.is_free.return_value = True
            gov.fetch_models.return_value = []
            return "key", gov

        with mock.patch.object(cli, "_governor", side_effect=fake_governor), \
             mock.patch.object(cli, "HttpTransport"), \
             mock.patch.object(cli, "AutonomyLedger", mock.MagicMock()):
            cli.main(argv)

    def test_bench_max_cost_reaches_governor(self):
        captured = {}
        from unittest import mock
        from harness import cli
        with mock.patch.object(cli, "load_manifest", return_value=[]), \
             mock.patch.object(cli, "run_bench", return_value={"bench": {"statuses": {}}}), \
             mock.patch.object(cli, "ApplyEngine"):
            self._run(["bench", "tasks.json", "--max-cost", "0.004"], captured)
        self.assertEqual(captured["override"], 0.004)

    def test_capabilities_max_cost_reaches_governor(self):
        captured = {}
        from unittest import mock
        from harness import cli
        with mock.patch.object(cli, "_capability_context",
                               return_value=(None, None)), \
             mock.patch("harness.capability.ensure_profiles",
                        return_value=({}, 0.0, False)):
            self._run(["capabilities", "--max-cost", "0.03"], captured)
        self.assertEqual(captured["override"], 0.03)

    def test_default_ceiling_when_flag_absent(self):
        captured = {}
        from unittest import mock
        from harness import cli
        with mock.patch.object(cli, "_capability_context",
                               return_value=(None, None)), \
             mock.patch("harness.capability.ensure_profiles",
                        return_value=({}, 0.0, False)):
            self._run(["capabilities"], captured)
        self.assertIsNone(captured["override"])


if __name__ == "__main__":
    unittest.main()
