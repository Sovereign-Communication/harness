"""Lane budget policy: ONE owner (config.effective_lane_policy) and its
consumers (panel vote/judge/specialist, escalation rungs).

Rulings 2 + 6 (2026-09-13): votes are cheap decisions -- >=4096 output
tokens with reasoning explicitly disabled; judge synthesis and deep
escalation rungs get >=8192 with bounded depth. Explicit caller
configuration always wins. Apply keeps its own 4096 behavior.
"""
import tempfile
import unittest

from harness.config import (MIN_SYNTHESIS_TOKENS, MIN_VOTE_TOKENS,
                            effective_lane_policy)


class LanePolicyUnitTests(unittest.TestCase):
    def test_vote_floor(self):
        tokens, effort = effective_lane_policy("vote", max_tokens=600)
        self.assertEqual(tokens, MIN_VOTE_TOKENS)
        self.assertEqual(effort, "off")

    def test_vote_explicit_caller_wins(self):
        tokens, effort = effective_lane_policy("vote", max_tokens=2048,
                                               reasoning_effort="low")
        self.assertEqual(tokens, MIN_VOTE_TOKENS)
        self.assertEqual(effort, "low")

    def test_judge_floor_and_default_effort(self):
        tokens, effort = effective_lane_policy("judge", max_tokens=2048)
        self.assertEqual(tokens, MIN_SYNTHESIS_TOKENS)
        self.assertEqual(effort, "auto")

    def test_escalation_floor(self):
        tokens, effort = effective_lane_policy("escalation", max_tokens=4096)
        self.assertEqual(tokens, MIN_SYNTHESIS_TOKENS)
        self.assertEqual(effort, "auto")

    def test_apply_unchanged(self):
        tokens, effort = effective_lane_policy("apply", max_tokens=4096,
                                               reasoning_effort="medium")
        self.assertEqual(tokens, 4096)
        self.assertEqual(effort, "medium")

    def test_unknown_role_raises(self):
        with self.assertRaises(ValueError):
            effective_lane_policy("specialist")


class PanelLanePolicyTests(unittest.TestCase):
    """panel_judge resolves the lane policy; verify it via the preflight
    reservation the panel makes against the governor."""

    def _run(self, max_tokens=None, reasoning_effort=None, extra_models=(),
             panel=None):
        from tests._fake import FakeTransport, comp, m
        from harness.spend import SpendGovernor
        from harness.panel import panel_judge
        models = [m("m/a"), m("m/j")]
        posts = [comp('{"claim_1": {"real": false}}'),
                 comp('{"verdict": "ok", "agreement": "high", '
                      '"confidence": 0.9, "defer": false}')]
        fake = FakeTransport(models=models, posts=posts)
        # Isolated byok store: the operator's real learned prefixes must not
        # leak into hermetic runs (and vice versa).
        with tempfile.TemporaryDirectory() as td:
            gov = SpendGovernor(fake, "sk-test", byok_prefixes_path=
                                __import__("os").path.join(td, "byok.json"))
            preflight = []
            orig = gov.preflight

            def spy(prompt, calls):
                preflight.extend(calls)
                return orig(prompt, calls)
            gov.preflight = spy
            result = panel_judge(transport=fake, api_key="k", governor=gov,
                                 prompt="Q?", panel=panel or ["m/a"], judge="m/j",
                                 max_tokens=max_tokens,
                                 reasoning_effort=reasoning_effort)
        return result, fake, preflight

    def test_vote_budget_raised_to_lane_minimum(self):
        _result, fake, preflight = self._run(max_tokens=600)
        panel_calls = [c for c in preflight if "panel" in c[0]]
        self.assertTrue(panel_calls)
        self.assertEqual(panel_calls[0][2], 4096)
        payload = fake.chat_posts()[0][2]
        self.assertEqual(payload["reasoning"], {"effort": "none"})

    def test_judge_budget_raised_to_synthesis_minimum(self):
        _result, fake, preflight = self._run(max_tokens=600)
        judge_calls = [c for c in preflight if "judge" in c[0]]
        self.assertTrue(judge_calls)
        self.assertGreaterEqual(judge_calls[0][2], 8192 + 200)
        judge_post = fake.chat_posts()[-1][2]
        # "auto" on a non-reasoning judge id omits the reasoning key.
        self.assertNotIn("reasoning", judge_post)

    def test_auto_judge_on_reasoning_id_gets_low_with_cap(self):
        _result, fake, _preflight = self._run(max_tokens=600)
        # m/j is not a reasoning id; use the explicit-medium case for the
        # pass-through proof and check the cap math on a reasoning id here
        # via the payload of a reasoning-named judge run.
        from tests._fake import FakeTransport, comp, m
        from harness.spend import SpendGovernor
        from harness.panel import panel_judge
        import os
        with tempfile.TemporaryDirectory() as td:
            fake2 = FakeTransport(
                models=[m("m/a"), m("z-ai/glm-5.3-flash")],
                posts=[comp('{"c": {"real": false}}'),
                       comp('{"verdict": "ok", "agreement": "high", '
                            '"confidence": 0.9, "defer": false}')])
            gov = SpendGovernor(fake2, "sk-test", byok_prefixes_path=
                                os.path.join(td, "byok.json"))
            panel_judge(transport=fake2, api_key="k", governor=gov,
                        prompt="Q?", panel=["m/a"], judge="z-ai/glm-5.3-flash",
                        max_tokens=600, reasoning_effort="auto")
            rp = fake2.chat_posts()[-1][2]["reasoning"]
            self.assertEqual(rp["effort"], "low")
            self.assertEqual(rp["max_tokens"], int((8192 + 200) * 0.4))

    def test_explicit_reasoning_effort_passes_through(self):
        _result, fake, _preflight = self._run(max_tokens=2048,
                                              reasoning_effort="medium")
        payload = fake.chat_posts()[0][2]
        self.assertEqual(payload["reasoning"]["effort"], "medium")


if __name__ == "__main__":
    unittest.main()
