"""Unit tests for task-to-model routing table and OpenRouter floor price controls (#PR-Cost-1)."""
import unittest

from harness.chat import chat
from harness.routing_table import (
    TIER_0,
    TIER_1,
    TIER_2,
    TIER_3,
    classify_task_tier,
    floor_model,
    get_tier_route,
    next_tier,
    strip_variant_suffix,
)


class FakeTransport:
    def __init__(self, status=200, resp=None):
        self.status = status
        self.resp = resp or {"choices": [{"message": {"content": "ok"}}], "usage": {"cost": 0.0001}}
        self.calls = []

    def post(self, url, api_key, payload):
        self.calls.append({"url": url, "api_key": api_key, "payload": payload})
        return self.status, self.resp


class RoutingTableTests(unittest.TestCase):
    def test_floor_model(self):
        # Paid models without suffix receive :floor
        self.assertEqual(floor_model("deepseek/deepseek-chat"), "deepseek/deepseek-chat:floor")
        self.assertEqual(floor_model("qwen/qwen-2.5-coder-32b-instruct"), "qwen/qwen-2.5-coder-32b-instruct:floor")

        # Models with existing variants are left untouched
        self.assertEqual(floor_model("deepseek/deepseek-chat:free"), "deepseek/deepseek-chat:free")
        self.assertEqual(floor_model("meta-llama/llama-3.3-70b-instruct:free"), "meta-llama/llama-3.3-70b-instruct:free")
        self.assertEqual(floor_model("anthropic/claude-3.7-sonnet:floor"), "anthropic/claude-3.7-sonnet:floor")
        self.assertEqual(floor_model("openai/o3-mini:nitro"), "openai/o3-mini:nitro")

        # Disabled floor leaves slug untouched
        self.assertEqual(floor_model("deepseek/deepseek-chat", enable_floor=False), "deepseek/deepseek-chat")
        self.assertEqual(floor_model("", enable_floor=True), "")

    def test_strip_variant_suffix(self):
        self.assertEqual(strip_variant_suffix("deepseek/deepseek-chat:floor"), "deepseek/deepseek-chat")
        self.assertEqual(strip_variant_suffix("google/gemini-2.0-flash-exp:free"), "google/gemini-2.0-flash-exp")
        self.assertEqual(strip_variant_suffix("openai/o3-mini:nitro"), "openai/o3-mini")
        self.assertEqual(strip_variant_suffix("anthropic/claude-3.7-sonnet"), "anthropic/claude-3.7-sonnet")

    def test_classify_task_tier(self):
        # T0: simple queries, docstrings, formatting
        self.assertEqual(classify_task_tier("fix typo in docstring of helper.py"), TIER_0)
        self.assertEqual(classify_task_tier("summarize this comment"), TIER_0)

        # T1: standard updates and local edits
        self.assertEqual(classify_task_tier("add input validation to user endpoint"), TIER_1)

        # T2: multi-file modifications, plan/dag, complex debugging
        self.assertEqual(classify_task_tier("plan DAG decomposition for pipeline refactor"), TIER_2)
        self.assertEqual(classify_task_tier("investigate failing test and traceback"), TIER_2)
        self.assertEqual(classify_task_tier("update files", target_files=["a.py", "b.py", "c.py"]), TIER_2)

        # T3: architectural, concurrency, cryptographic invariants
        self.assertEqual(classify_task_tier("fix deadlock in mutex lock acquisition"), TIER_3)
        self.assertEqual(classify_task_tier("verify cryptographic hash chain invariant"), TIER_3)

    def test_next_tier_ladder(self):
        self.assertEqual(next_tier(TIER_0), TIER_1)
        self.assertEqual(next_tier(TIER_1), TIER_2)
        self.assertEqual(next_tier(TIER_2), TIER_3)
        self.assertIsNone(next_tier(TIER_3))
        self.assertIsNone(next_tier("INVALID_TIER"))

    def test_get_tier_route(self):
        route_t0 = get_tier_route(TIER_0)
        self.assertEqual(route_t0["tier"], TIER_0)
        self.assertEqual(route_t0["cost_band"], "$0")
        # Free models preserve :free without :floor
        self.assertTrue(all(":free" in m for m in route_t0["models"]))
        self.assertEqual(route_t0["max_price"]["prompt"], 0.0)

        route_t1 = get_tier_route(TIER_1, enable_floor=True)
        self.assertEqual(route_t1["tier"], TIER_1)
        self.assertTrue(all(m.endswith(":floor") for m in route_t1["models"]))
        self.assertEqual(route_t1["max_price"]["prompt"], 0.35)

    def test_chat_floor_and_provider_payload(self):
        transport = FakeTransport()
        chat(
            transport=transport,
            api_key="sk-test",
            model="deepseek/deepseek-chat",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=100,
            enable_floor=True,
            max_price={"prompt": 0.50, "completion": 2.00},
            provider_sort="price",
        )
        self.assertEqual(len(transport.calls), 1)
        payload = transport.calls[0]["payload"]
        self.assertEqual(payload["model"], "deepseek/deepseek-chat:floor")
        self.assertEqual(payload["provider"]["sort"], "price")
        self.assertEqual(payload["provider"]["max_price"], {"prompt": 0.50, "completion": 2.00})


if __name__ == "__main__":
    unittest.main()
