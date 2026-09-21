"""HG-ms-parity: no ad-hoc model fallback strings in chat/frontier resolution."""
import ast
import unittest
from pathlib import Path
from types import SimpleNamespace

from harness.chat import chat_ladder
from harness.config import DEFAULT_FRONTIER_PAID, FREE_JUDGE
from harness.sliding_scale import resolve_frontier_model

PKG = Path(__file__).resolve().parents[1] / "harness"


class ModelSlateParityTests(unittest.TestCase):
    def test_chat_ladder_last_resort_comes_from_settings_pool_or_config(self):
        settings = SimpleNamespace(
            tier1_model=None, judge=None,
            panel_pool=["custom/pool-head", "custom/pool-next"],
            allow_escalation=False, escalation_pool=None)
        ladder = chat_ladder(settings)
        self.assertEqual(ladder[0], "custom/pool-head")

        empty = SimpleNamespace(
            tier1_model=None, judge=None, panel_pool=None,
            allow_escalation=False, escalation_pool=None)
        self.assertEqual(chat_ladder(empty)[0], FREE_JUDGE)

        judged = SimpleNamespace(
            tier1_model=None, judge="settings/judge", panel_pool=["p/1"],
            allow_escalation=False, escalation_pool=None)
        self.assertEqual(chat_ladder(judged)[0], "settings/judge")

    def test_resolve_frontier_model_default_from_config_constant(self):
        self.assertEqual(resolve_frontier_model(None, use_free=False),
                         DEFAULT_FRONTIER_PAID)
        self.assertEqual(DEFAULT_FRONTIER_PAID, "qwen/qwen3.8-max-0902")
        self.assertEqual(resolve_frontier_model(None, use_free=True), FREE_JUDGE)

    def test_no_ad_hoc_fallback_string_in_chat_py(self):
        source = (PKG / "chat.py").read_text(encoding="utf-8")
        self.assertNotIn("inclusionai/ling-3.0-flash-fin:free", source)
        self.assertNotIn("qwen/qwen3.8-max-0902", source)
        # No bare model-id string literals assigned as ladder heads.
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                value = node.value
                if "/" in value and value.count("/") >= 1 and (
                        ":free" in value or value.startswith(("openai/", "qwen/", "google/", "inclusionai/", "z-ai/", "deepseek/"))):
                    self.fail(f"chat.py hardcodes provider model id {value!r}")


if __name__ == "__main__":
    unittest.main()
