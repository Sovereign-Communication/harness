import unittest

from harness.chat import (
    chat,
    looks_reasoning,
    _build_reasoning_param,
    _effort_to_send,
)
from harness.convergence import _parse_consensus
from harness.panel import panel_judge
from harness.spend import (
    SpendGovernor,
    discover_free_models,
)
from tests._fake import FakeTransport, m, comp

JUDGE = "inclusionai/ling-2.6-flash"
P1 = "meta-llama/llama-3.1-8b-instruct"
P2 = "ibm-granite/granite-4.1-8b"
P3 = "google/gemma-4-31b-it:free"


class ReasoningTests(unittest.TestCase):
    def test_looks_reasoning_heuristic(self):
        self.assertTrue(looks_reasoning("deepseek/deepseek-r1"))
        self.assertTrue(looks_reasoning("thinkingmachines/inkling-small:free"))
        self.assertTrue(looks_reasoning("nvidia/nemotron-3-nano-reasoning:free"))
        self.assertFalse(looks_reasoning("cohere/north-mini-code:free"))

    def test_effort_to_send(self):
        # auto: reasoning models get capped low, others omit
        self.assertEqual(_effort_to_send("auto", "x-r1"), "low")
        self.assertIsNone(_effort_to_send("auto", "cohere/north-mini-code:free"))
        # off/none resolve to the EXPLICIT disable ("none"): omitting the
        # reasoning key means the provider default (reasoning ON) for
        # reasoning-native models. 2026-09-13 operator ruling 6 + handoff s1.
        self.assertEqual(_effort_to_send("none", "x-r1"), "none")
        self.assertEqual(_effort_to_send("off", "x-r1"), "none")
        # explicit efforts pass through
        self.assertEqual(_effort_to_send("medium", "cohere/x"), "medium")
        self.assertEqual(_effort_to_send("on", "cohere/x"), "high")

    def test_reasoning_param_caps_tokens(self):
        rp = _build_reasoning_param("x-r1", "low", 300, 0.4)
        self.assertEqual(rp, {"effort": "low", "max_tokens": 120})
        self.assertIsNone(_build_reasoning_param("cohere/x", "auto", 300, 0.4))
        # The explicit disable sends {"effort": "none"} with NO max_tokens
        # cap (a cap is meaningless when reasoning is off).
        self.assertEqual(_build_reasoning_param("x-r1", "off", 300, 0.4),
                         {"effort": "none"})
        self.assertEqual(_build_reasoning_param("x-r1", "none", 300, 0.4),
                         {"effort": "none"})

    def test_chat_sends_reasoning_only_when_requested(self):
        fake = FakeTransport(models=[], posts=[comp("ok")])
        status, resp = chat(fake, "k", "x-r1", [{"role": "user", "content": "hi"}], 300,
                            reasoning_effort="low")
        payload = fake.payloads()[0]
        self.assertEqual(payload["reasoning"]["effort"], "low")
        self.assertEqual(payload["reasoning"]["max_tokens"], 120)

    def test_chat_auto_omits_for_non_reasoning(self):
        fake = FakeTransport(models=[], posts=[comp("ok")])
        chat(fake, "k", "cohere/north-mini-code:free",
             [{"role": "user", "content": "hi"}], 300, reasoning_effort="auto")
        self.assertNotIn("reasoning", fake.payloads()[0])

    def test_chat_retries_without_reasoning_on_param_error(self):
        fake = FakeTransport(models=[],
                             posts=[(400, {"error": {"message": "unsupported parameter: reasoning"}}),
                                    comp("ok")])
        status, resp = chat(fake, "k", "x-r1", [{"role": "user", "content": "hi"}], 300,
                            reasoning_effort="high")
        self.assertEqual(status, 200)
        self.assertEqual(len(fake.chat_posts()), 2)
        self.assertNotIn("reasoning", fake.payloads()[1])


class DiscoveryTests(unittest.TestCase):
    def test_discover_free_models_prefers_curated(self):
        fake = FakeTransport(models=[
            {"id": "z/paid", "pricing": {"prompt": "0.0001", "completion": "0.0002"}},
            {"id": "x/aa:free", "pricing": {"prompt": "0", "completion": "0"}},
            {"id": "y/bb:free", "pricing": {"prompt": "0", "completion": "0"}},
        ])
        ids = discover_free_models(fake, "k", prefer=["y/bb:free"])
        self.assertEqual(ids, ["y/bb:free", "x/aa:free"])

    def test_discover_drops_prefer_not_free(self):
        fake = FakeTransport(models=[
            {"id": "x/aa:free", "pricing": {"prompt": "0", "completion": "0"}},
            {"id": "y/bb:free", "pricing": {"prompt": "0", "completion": "0"}},
        ])
        ids = discover_free_models(fake, "k", prefer=["z/gone:free"])
        self.assertEqual(ids, ["x/aa:free", "y/bb:free"])


class PanelRotationTests(unittest.TestCase):
    def test_failed_panel_member_rotates_to_next(self):
        fake = FakeTransport(models=[m(P1), m(P2), m(JUDGE)],
                             posts=[(500, {"error": {"message": "down"}}),
                                    comp("take two"), comp("verdict json")])
        gov = SpendGovernor(fake, "sk-test")
        result = panel_judge(transport=fake, api_key="k", governor=gov, prompt="Q?",
                             panel=[P1, P2], judge=JUDGE)
        self.assertEqual(len(result["panel_results"]), 1)
        self.assertEqual(result["panel_results"][0]["model"], P2)

    def test_panel_uses_up_to_max_panelists(self):
        fake = FakeTransport(models=[m(P1), m(P2), m(P3), m(JUDGE)],
                             posts=[comp("one"), comp("two"), comp("verdict")])
        gov = SpendGovernor(fake, "sk-test")
        result = panel_judge(transport=fake, api_key="k", governor=gov, prompt="Q?",
                             panel=[P1, P2, P3], judge=JUDGE, max_panelists=2)
        self.assertEqual(len(result["panel_results"]), 2)


class ConsensusTests(unittest.TestCase):
    def test_parse_consensus_json(self):
        c = _parse_consensus('{"verdict": "do X", "agreement": "medium", '
                             '"confidence": 0.7, "disagreements": ["edge case"], '
                             '"defer": false}')
        self.assertEqual(c["agreement"], "medium")
        self.assertEqual(c["confidence"], 0.7)
        self.assertEqual(c["disagreements"], ["edge case"])
        self.assertFalse(c["defer"])

    def test_low_agreement_defers(self):
        c = _parse_consensus('{"verdict": "unknown", "agreement": "low", "defer": false}')
        self.assertTrue(c["defer"])

    def test_unparseable_falls_back_unknown(self):
        c = _parse_consensus("just some prose")
        self.assertEqual(c["agreement"], "unknown")
        self.assertEqual(c["verdict"], "just some prose")

    def test_panel_judge_includes_consensus(self):
        fake = FakeTransport(models=[m(P1), m(P2), m(JUDGE)],
                             posts=[comp("a"), comp("b"),
                                    comp('{"verdict":"agree","agreement":"high",'
                                         '"confidence":0.9,"defer":false}')])
        gov = SpendGovernor(fake, "sk-test")
        result = panel_judge(transport=fake, api_key="k", governor=gov, prompt="Q?",
                             panel=[P1, P2], judge=JUDGE)
        self.assertEqual(result["consensus"]["agreement"], "high")
        self.assertEqual(result["consensus"]["confidence"], 0.9)
        self.assertFalse(result["consensus"]["defer"])
        self.assertEqual(result["verdict"], "agree")


if __name__ == "__main__":
    unittest.main()
