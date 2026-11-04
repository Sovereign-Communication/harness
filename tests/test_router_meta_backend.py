"""#447 router slice: Meta is a first-class routing backend.

Hermetic. A recording transport supplies every response, so nothing here reads
a live key or opens a connection: the acceptance claim is about WHICH provider
a model id resolves to and which endpoint dispatch reaches, not about a live
call. The judge seat is driven end to end; the convergence and panel-vote call
sites are driven through the router entry point they now import.
"""
import ast
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import harness.chat as chat_module
from harness.capability import probe_json_reliability
from harness.chat import chat_routed
from harness.consent import probe_consent
from harness.rankings import _probe_vote
from harness.config import META_CHAT_URL, META_PROVIDER, OPENROUTER_CHAT_URL
from harness.convergence import run_convergence_specialist
from harness.errors import HarnessError
from harness.meta import resolve_offer
from harness.panel import _run_judge_attempt
from harness.routing_table import (
    OPENROUTER_BACKEND,
    backend_for_model,
    backend_registry,
    route_for_model,
)
from harness.sliding_scale import (
    TIER_0_SCOUT,
    TIER_1_DISTILLER,
    preset_model_ladder,
    tier_model_ladder,
)
from harness.spend import SpendGovernor

SPARK = "muse-spark-1.3"
WONDER = "muse-wonder-1.0"
UNCOMMITTED = "muse-vision-9.9"
FIREWORKS_PATH = "accounts/fireworks/models/glm-5"
OR_MODEL = "vendor/some-model"
MESSAGES = [{"role": "user", "content": "Reply with the single word: ok"}]


class RecordingTransport:
    """Returns one canned response and records every POST."""

    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    def post(self, url, api_key, payload, timeout=45):
        self.calls.append((url, api_key, payload))
        return 200, self.resp

    def get(self, url, api_key, timeout=15):
        raise AssertionError(f"hermetic router tests must not GET {url}")


def _resp(usage=None):
    return {
        "choices": [{"message": {"content": "{\"verdict\": \"ok\"}"},
                     "finish_reason": "stop"}],
        "usage": usage or {"prompt_tokens": 100, "completion_tokens": 50},
    }


def _governor(transport, budget=1.0):
    return SpendGovernor(transport, "key", None, budget, meta_budget_usd=budget)


class BackendRegistryTests(unittest.TestCase):
    def test_each_backend_is_identified_by_the_model_id_shape(self):
        self.assertEqual(backend_for_model(SPARK), META_PROVIDER)
        self.assertEqual(backend_for_model(FIREWORKS_PATH), "fireworks")
        self.assertEqual(backend_for_model(OR_MODEL), OPENROUTER_BACKEND)

    def test_registry_names_every_backend_and_its_key_surface(self):
        registry = backend_registry()
        self.assertIn(META_PROVIDER, registry)
        self.assertIn(OPENROUTER_BACKEND, registry)
        self.assertIn("META_API_KEY", registry[META_PROVIDER])

    def test_empty_model_id_is_refused(self):
        with self.assertRaises(ValueError):
            backend_for_model("")


class TableResolvesMetaToARouteTests(unittest.TestCase):
    def test_muse_id_resolves_to_a_meta_route(self):
        route = route_for_model(SPARK)
        self.assertEqual(route.provider, META_PROVIDER)
        self.assertEqual(route.wire_model, SPARK)
        self.assertEqual(route.offer, resolve_offer(SPARK))

    def test_uncommitted_muse_id_fails_closed(self):
        with self.assertRaisesRegex(HarnessError, "no committed Meta endpoint"):
            route_for_model(UNCOMMITTED)

    def test_meta_disabled_refuses_rather_than_falling_back(self):
        with self.assertRaisesRegex(HarnessError, "meta is disabled"):
            route_for_model(SPARK, meta_enabled=False)

    def test_settings_supply_the_flags_when_given(self):
        settings = SimpleNamespace(openrouter_enabled=True,
                                   fireworks_enabled=False,
                                   meta_enabled=True)
        route = route_for_model(SPARK, settings=settings)
        self.assertEqual(route.provider, META_PROVIDER)

    def test_ordinary_model_still_resolves_to_openrouter(self):
        route = route_for_model(OR_MODEL)
        self.assertEqual(route.provider, OPENROUTER_BACKEND)


class RoutedDispatchTests(unittest.TestCase):
    def test_muse_id_dispatches_to_meta_and_never_openrouter(self):
        transport = RecordingTransport(_resp())
        status, _ = chat_routed(transport, "meta-key", SPARK, MESSAGES, 64)
        self.assertEqual(status, 200)
        url, key, payload = transport.calls[0]
        self.assertEqual(url, META_CHAT_URL)
        self.assertEqual(key, "meta-key")
        self.assertEqual(payload["model"], SPARK)
        self.assertTrue(all(call[0] != OPENROUTER_CHAT_URL
                            for call in transport.calls))

    def test_missing_meta_key_refuses_before_dispatch(self):
        transport = RecordingTransport(_resp())
        with self.assertRaisesRegex(HarnessError, "Meta key missing"):
            chat_routed(transport, None, SPARK, MESSAGES, 64)
        self.assertEqual(transport.calls, [])

    def test_ordinary_model_still_posts_to_openrouter(self):
        transport = RecordingTransport(_resp())
        chat_routed(transport, "or-key", OR_MODEL, MESSAGES, 64)
        self.assertEqual(transport.calls[0][0], OPENROUTER_CHAT_URL)


class MigratedCallSiteTests(unittest.TestCase):
    def test_judge_seat_dispatches_through_the_router_to_meta(self):
        transport = RecordingTransport(_resp())
        governor = _governor(transport)
        _run_judge_attempt(
            transport=transport, api_key="meta-key", governor=governor,
            judge_prompt="return JSON", model=SPARK, judge_reserve_tokens=64,
            reasoning_effort="auto", reasoning_token_budget=0.4,
            task_id=None, ledger=None, task_type="panel",
        )
        self.assertEqual(transport.calls[0][0], META_CHAT_URL)
        self.assertEqual(transport.calls[0][1], "meta-key")

    def test_convergence_specialist_dispatches_through_the_router(self):
        transport = RecordingTransport(_resp())
        with mock.patch("harness.convergence.chat_routed",
                        return_value=(200, _resp())) as routed:
            run_convergence_specialist(transport, "meta-key",
                                       _governor(transport), [], SPARK,
                                       max_tokens=64)
        self.assertTrue(routed.called)
        self.assertEqual(routed.call_args.args[2], SPARK)


class PresetAwareLadderTests(unittest.TestCase):
    def test_preset_ladder_returns_only_committed_muse_ids(self):
        settings = SimpleNamespace(meta_enabled=True)
        ladder = tier_model_ladder(TIER_0_SCOUT, settings=settings)
        self.assertEqual(ladder, [SPARK, WONDER])
        self.assertNotIn("openrouter/free", ladder)

    def test_every_preset_rung_resolves_to_the_meta_backend(self):
        settings = SimpleNamespace(meta_enabled=True)
        for tier in (TIER_0_SCOUT, TIER_1_DISTILLER, 2):
            ladder = tier_model_ladder(tier, settings=settings)
            self.assertTrue(ladder)
            for model_id in ladder:
                self.assertEqual(route_for_model(model_id).provider,
                                 META_PROVIDER)

    def test_without_the_preset_the_historical_ladder_is_unchanged(self):
        self.assertIsNone(preset_model_ladder(
            TIER_0_SCOUT, settings=SimpleNamespace(meta_enabled=False)))
        self.assertIn("openrouter/free", tier_model_ladder(TIER_0_SCOUT))

    def test_no_settings_keeps_the_historical_ladder(self):
        self.assertIsNone(preset_model_ladder(TIER_0_SCOUT))
        self.assertIn("openrouter/free", tier_model_ladder(TIER_0_SCOUT))


ROOT = Path(__file__).resolve().parent.parent

# Every lane that used to post a model id straight to chat(). The audit is by
# module, so a new direct chat() call in any of them fails this suite rather
# than silently reintroducing the OpenRouter fallthrough #447 removed.
MIGRATED_MODULES = [
    "apply_policy", "attest", "capability", "consent", "conversation_lifecycle",
    "escalation", "hourglass_lifecycle", "rankings",
]


def _tree(module_name):
    return ast.parse((ROOT / "harness" / f"{module_name}.py").read_text(
        encoding="utf-8"))


def _bare_chat_calls(tree):
    return [node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name) and node.func.id == "chat"]


def _functions_dispatching_via(tree, entry):
    names = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == entry):
                names.append(fn.name)
                break
    return names


class NoDirectChatBypassTests(unittest.TestCase):
    """AST audit: no migrated lane can reach chat() with a model id."""

    def test_no_migrated_module_calls_chat_directly(self):
        for module_name in MIGRATED_MODULES:
            with self.subTest(module=module_name):
                self.assertEqual(_bare_chat_calls(_tree(module_name)), [])

    def test_each_migrated_module_dispatches_through_the_router(self):
        for module_name in MIGRATED_MODULES:
            with self.subTest(module=module_name):
                sites = _functions_dispatching_via(_tree(module_name),
                                                   "chat_routed")
                self.assertTrue(sites, f"{module_name} has no routed site")

    def test_both_apply_policy_sites_dispatch_through_the_router(self):
        sites = _functions_dispatching_via(_tree("apply_policy"),
                                           "chat_routed")
        self.assertGreaterEqual(len(sites), 2)

    def test_the_entry_point_each_module_calls_is_the_real_router(self):
        for module_name in MIGRATED_MODULES:
            module = __import__(f"harness.{module_name}", fromlist=["x"])
            with self.subTest(module=module_name):
                if hasattr(module, "chat_routed"):
                    self.assertIs(module.chat_routed, chat_module.chat_routed)
                    continue
                # attest and capability import the entry point inside the
                # function; assert that import names the real router module.
                local = [node for node in ast.walk(_tree(module_name))
                         if isinstance(node, ast.ImportFrom)
                         and node.module == "chat" and node.level == 1
                         and any(alias.name == "chat_routed"
                                 for alias in node.names)]
                self.assertTrue(local, f"{module_name} never imports the router")


class RuntimeDispatchPerLaneTests(unittest.TestCase):
    """One live dispatch per lightweight lane, on its own function."""

    def _json_resp(self, body):
        return {"choices": [{"message": {"content": body},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 40, "completion_tokens": 20}}

    def test_rankings_probe_vote_posts_to_meta_for_a_muse_id(self):
        transport = RecordingTransport(self._json_resp(
            '{"claim_1": {"real": true, "confidence": 0.9}}'))
        _probe_vote(transport, "meta-key", _governor(transport), SPARK, 64)
        self.assertEqual(transport.calls[0][0], META_CHAT_URL)
        self.assertEqual(transport.calls[0][1], "meta-key")
        self.assertTrue(all(c[0] != OPENROUTER_CHAT_URL for c in transport.calls))

    def test_capability_probe_posts_to_meta_for_a_muse_id(self):
        transport = RecordingTransport(self._json_resp(
            '{"claim_1": {"real": true, "confidence": 0.9}}'))
        probe_json_reliability(transport, "meta-key", _governor(transport),
                               [SPARK], max_tokens=64)
        self.assertEqual(transport.calls[0][0], META_CHAT_URL)
        self.assertTrue(all(c[0] != OPENROUTER_CHAT_URL for c in transport.calls))

    def test_consent_probe_posts_to_meta_for_a_muse_id(self):
        transport = RecordingTransport(self._json_resp(
            '{"decision": "accept", "confidence": 0.95, "reason": "clear"}'))
        probe_consent(transport=transport, api_key="meta-key",
                      governor=_governor(transport), task_id="t-1",
                      task="summarize the diff", model=SPARK, max_tokens=64,
                      required=False)
        self.assertEqual(transport.calls[0][0], META_CHAT_URL)
        self.assertTrue(all(c[0] != OPENROUTER_CHAT_URL for c in transport.calls))


class KeySurfaceTests(unittest.TestCase):
    def test_settings_supply_the_meta_key_when_the_lane_has_none(self):
        transport = RecordingTransport(_resp())
        settings = SimpleNamespace(meta_enabled=True, meta_key="from-settings")
        chat_routed(transport, None, SPARK, MESSAGES, 64, settings=settings)
        self.assertEqual(transport.calls[0][1], "from-settings")

    def test_no_key_anywhere_refuses_before_dispatch(self):
        transport = RecordingTransport(_resp())
        with mock.patch("harness.chat.resolve_meta_key", return_value=""):
            with self.assertRaisesRegex(HarnessError, "Meta key missing"):
                chat_routed(transport, None, SPARK, MESSAGES, 64)
        self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main()
