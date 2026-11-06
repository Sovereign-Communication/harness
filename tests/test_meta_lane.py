"""Meta direct lane (#377/#378/#379): route, preset, key lookup, call shape,
budget, and the verify task-id self-offer.

Hermetic. A recording transport supplies every response. Settings and key
lookup run against a temp HOME and CONFIG_DIR, so the operator's real config
and key files are never read, and a socket guard proves nothing here opens a
connection.
"""
import io
import os
import socket
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from harness import config, service
from harness.chat import chat, chat_for_route, governed_completion
from harness.config import (META_CHAT_URL, META_PROVIDER, OPENROUTER_CHAT_URL,
                            load_settings, resolve_meta_key)
from harness.errors import HarnessError
from harness.fireworks import resolve_route
from harness.ledger import AutonomyLedger
from harness.meta import cost_estimate, resolve_offer
from harness.session import governor_for
from harness.spend import SpendGovernor

SPARK = "muse-spark-1.3"
WONDER = "muse-wonder-1.0"
UNCOMMITTED = "muse-vision-9.9"
OR_MODEL = "vendor/some-model"
MESSAGES = [{"role": "user", "content": "Reply with the single word: ok"}]
_META_ENV = ("META_API_KEY", "MUSE_API_KEY", "HARNESS_USE_META",
             "HARNESS_MODEL_PRESET", "HARNESS_META_ENABLED",
             "HARNESS_META_BUDGET_USD", "HARNESS_PANEL_POOL",
             "HARNESS_PANEL", "HARNESS_JUDGE", "HARNESS_APPLY_MODEL",
             "HARNESS_TRIAL_MODEL", "OPENROUTER_API_KEY",
             "FIREWORKS_API_KEY", "HARNESS_USE_FIREWORKS",
             "HARNESS_OPENROUTER_ENABLED", "HARNESS_FIREWORKS_ENABLED")


class RecordingTransport:
    """Returns one canned response and records every POST."""

    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    def post(self, url, api_key, payload, timeout=45):
        self.calls.append((url, api_key, payload))
        return 200, self.resp

    def get(self, url, api_key, timeout=15):
        raise AssertionError(f"Meta lane tests must not GET {url}")


def _resp(usage):
    return {"choices": [{"message": {"content": "ok"},
                         "finish_reason": "stop"}],
            "usage": usage}


class _IsolatedHome(unittest.TestCase):
    """Point HOME, USERPROFILE and CONFIG_DIR at a temp dir for each test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.config_dir = self.home / ".config" / "harness"
        env = {"HOME": str(self.home), "USERPROFILE": str(self.home)}
        self._env = mock.patch.dict(os.environ, env)
        self._env.start()
        for name in _META_ENV:
            os.environ.pop(name, None)
        self._cfg = mock.patch.object(config, "CONFIG_DIR",
                                      str(self.config_dir))
        self._cfg.start()
        self._sock = mock.patch.object(
            socket.socket, "connect",
            side_effect=AssertionError("network access in a hermetic test"))
        self._sock.start()

    def tearDown(self):
        self._sock.stop()
        self._cfg.stop()
        self._env.stop()
        self._tmp.cleanup()

    def _write(self, rel, text):
        path = self.home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


class RouteTests(unittest.TestCase):
    def test_muse_models_route_to_meta(self):
        for model in (SPARK, WONDER):
            route = resolve_route(model, openrouter_enabled=True,
                                  fireworks_enabled=False, meta_enabled=True)
            self.assertEqual(route.provider, META_PROVIDER)
            self.assertEqual(route.wire_model, model)
            offer = route.offer
            assert offer is not None  # a Meta route always carries its offer
            self.assertEqual(offer.model, model)

    def test_muse_model_with_meta_disabled_fails_closed(self):
        with self.assertRaisesRegex(HarnessError, "meta is disabled"):
            resolve_route(SPARK, openrouter_enabled=True,
                          fireworks_enabled=False)

    def test_uncommitted_muse_id_fails_closed(self):
        with self.assertRaisesRegex(HarnessError, "no committed Meta endpoint"):
            resolve_route(UNCOMMITTED, openrouter_enabled=True,
                          fireworks_enabled=False, meta_enabled=True)
        with self.assertRaisesRegex(HarnessError, "no committed Meta endpoint"):
            resolve_offer(UNCOMMITTED)

    def test_ordinary_models_stay_on_openrouter(self):
        route = resolve_route(OR_MODEL, openrouter_enabled=True,
                              fireworks_enabled=False, meta_enabled=True)
        self.assertEqual(route.provider, "openrouter")

    def test_cost_estimate_uses_committed_rates(self):
        offer = resolve_offer(SPARK)
        self.assertAlmostEqual(cost_estimate(offer, 1_000_000, 1_000_000),
                               1.25 + 4.25)
        offer = resolve_offer(WONDER)
        self.assertAlmostEqual(cost_estimate(offer, 1_000_000, 1_000_000),
                               4.0 + 20.0)


class KeyResolutionTests(_IsolatedHome):
    def test_env_meta_api_key(self):
        os.environ["META_API_KEY"] = "meta-env-key"
        with redirect_stderr(io.StringIO()):
            self.assertEqual(resolve_meta_key(), "meta-env-key")

    def test_env_muse_api_key_fallback(self):
        os.environ["MUSE_API_KEY"] = "muse-env-key"
        with redirect_stderr(io.StringIO()):
            self.assertEqual(resolve_meta_key(), "muse-env-key")

    def test_harness_meta_env_file(self):
        self._write(".config/harness/meta.env", "META_API_KEY=file-key\n")
        self.assertEqual(resolve_meta_key(), "file-key")

    def test_scmorc_file_beats_env(self):
        self._write(".config/scmorc/meta.env", "MUSE_API_KEY=scmorc-key\n")
        os.environ["META_API_KEY"] = "env-key"
        self.assertEqual(resolve_meta_key(), "scmorc-key")

    def test_no_key_resolves_none(self):
        self.assertIsNone(resolve_meta_key())


class PresetSettingsTests(_IsolatedHome):
    def _meta_settings(self, **env):
        os.environ["META_API_KEY"] = "preset-key"
        os.environ.update(env)
        with redirect_stderr(io.StringIO()):
            return load_settings()

    def test_use_meta_pins_every_surface(self):
        s = self._meta_settings(HARNESS_USE_META="true")
        self.assertTrue(s.use_meta)
        self.assertEqual(s.judge, WONDER)
        self.assertEqual(s.judge_top, WONDER)
        self.assertEqual(s.convergence_model, WONDER)
        self.assertEqual(s.panel_pool, [SPARK, WONDER])
        self.assertEqual(s.apply_pool, [SPARK, WONDER])
        self.assertEqual(s.escalation_pool, [SPARK, WONDER])
        for model in (s.panel_pool + s.apply_pool + s.escalation_pool
                      + s.specialist_pool):
            self.assertTrue(model.startswith("muse-"), model)

    def test_model_preset_muse_is_the_named_switch(self):
        s = self._meta_settings(HARNESS_MODEL_PRESET="muse")
        self.assertTrue(s.use_meta)
        self.assertEqual(s.judge, WONDER)

    def test_unknown_preset_fails_closed(self):
        with self.assertRaisesRegex(HarnessError, "unknown model_preset"):
            self._meta_settings(HARNESS_MODEL_PRESET="frontier")

    def test_use_meta_without_key_fails_closed(self):
        os.environ.pop("META_API_KEY", None)
        os.environ["HARNESS_USE_META"] = "true"
        with self.assertRaisesRegex(HarnessError, "requires a Meta API key"):
            load_settings()

    def test_non_muse_pool_override_is_refused_at_load(self):
        with self.assertRaisesRegex(HarnessError, "pins every surface"):
            self._meta_settings(HARNESS_USE_META="true",
                                HARNESS_PANEL_POOL=OR_MODEL)

    def test_non_muse_trial_model_is_refused_at_load(self):
        with self.assertRaisesRegex(HarnessError, "pins every surface"):
            self._meta_settings(HARNESS_MODEL_PRESET="muse",
                                HARNESS_TRIAL_MODEL=OR_MODEL)

    def test_uncommitted_muse_override_is_refused_at_load(self):
        with self.assertRaisesRegex(HarnessError, "no committed Meta endpoint"):
            self._meta_settings(HARNESS_USE_META="true",
                                HARNESS_JUDGE=UNCOMMITTED)

    def test_muse_pool_override_is_accepted(self):
        s = self._meta_settings(HARNESS_USE_META="true",
                                HARNESS_PANEL_POOL=WONDER)
        self.assertEqual(s.panel_pool, [WONDER])


class GovernorTests(unittest.TestCase):
    def _governor(self, budget=1.0):
        return SpendGovernor(RecordingTransport(_resp({})), "key", None, 1.0,
                             meta_budget_usd=budget)

    def test_pricing_comes_from_committed_offers_without_a_catalog_call(self):
        gov = self._governor()
        pricing = gov.fetch_pricing([SPARK, WONDER])
        self.assertEqual(pricing[SPARK], (1.25e-6, 4.25e-6))
        self.assertEqual(pricing[WONDER], (4.0e-6, 20.0e-6))

    def test_uncommitted_muse_pricing_fails_closed(self):
        with self.assertRaisesRegex(HarnessError, "no committed Meta endpoint"):
            self._governor().fetch_pricing([UNCOMMITTED])

    def test_meta_budget_assertion(self):
        gov = self._governor(budget=0.001)
        gov.assert_meta_budget(0.0005, SPARK)
        with self.assertRaisesRegex(HarnessError, "HARNESS_META_BUDGET_USD"):
            gov.assert_meta_budget(0.002, SPARK)

    def test_zero_meta_budget_refuses_every_call(self):
        with self.assertRaisesRegex(HarnessError, "would exceed the local budget"):
            self._governor(budget=0.0).assert_meta_budget(0.0001, SPARK)

    def test_record_actual_enforces_meta_budget(self):
        gov = self._governor(budget=0.001)
        gov.record_actual(0.0008, SPARK)
        self.assertAlmostEqual(gov.meta_spent(), 0.0008)
        with self.assertRaisesRegex(HarnessError, "Meta spend"):
            gov.record_actual(0.0008, SPARK)


class ChatRouteTests(unittest.TestCase):
    def _route(self, model=SPARK):
        return resolve_route(model, openrouter_enabled=True,
                             fireworks_enabled=False, meta_enabled=True)

    def test_meta_dispatch_posts_plain_payload_to_meta_url(self):
        transport = RecordingTransport(
            _resp({"prompt_tokens": 100, "completion_tokens": 50}))
        status, _ = chat_for_route(self._route(), transport, "or-key",
                                   "fw-key", MESSAGES, 256, meta_key="meta-key")
        self.assertEqual(status, 200)
        url, key, payload = transport.calls[0]
        self.assertEqual(url, META_CHAT_URL)
        self.assertEqual(key, "meta-key")
        self.assertEqual(set(payload), {"model", "messages", "max_tokens"})
        self.assertNotIn("provider", payload)

    def test_meta_dispatch_never_reaches_openrouter(self):
        transport = RecordingTransport(
            _resp({"prompt_tokens": 10, "completion_tokens": 5}))
        chat_for_route(self._route(), transport, "or-key", "fw-key",
                       MESSAGES, 64, meta_key="meta-key")
        self.assertTrue(all(url != OPENROUTER_CHAT_URL
                            for url, _, _ in transport.calls))

    def test_missing_meta_key_is_refused_before_dispatch(self):
        transport = RecordingTransport(_resp({}))
        with self.assertRaisesRegex(HarnessError, "Meta key missing"):
            chat_for_route(self._route(), transport, "or-key", "fw-key",
                           MESSAGES, 64)
        self.assertEqual(transport.calls, [])

    def test_missing_usage_accounting_fails_closed(self):
        transport = RecordingTransport(_resp({}))
        gov = SpendGovernor(transport, "key", None, 1.0, meta_budget_usd=1.0)
        with self.assertRaisesRegex(HarnessError, "refusing to bill blind"):
            chat_for_route(self._route(), transport, "or-key", "fw-key",
                           MESSAGES, 64, governor=gov, meta_key="meta-key")

    def test_usage_cost_is_estimated_from_committed_rates(self):
        transport = RecordingTransport(
            _resp({"prompt_tokens": 100, "completion_tokens": 50}))
        gov = SpendGovernor(transport, "key", None, 1.0, meta_budget_usd=1.0)
        _, resp = chat_for_route(self._route(), transport, "or-key", "fw-key",
                                 MESSAGES, 64, governor=gov, meta_key="meta-key")
        self.assertAlmostEqual(resp["usage"]["cost"],
                               100 * 1.25e-6 + 50 * 4.25e-6)
        self.assertTrue(resp["usage"]["cost_estimated"])

    def test_worst_case_over_budget_is_never_sent(self):
        transport = RecordingTransport(
            _resp({"prompt_tokens": 10, "completion_tokens": 5}))
        gov = SpendGovernor(transport, "key", None, 1.0, meta_budget_usd=0.0)
        with self.assertRaisesRegex(HarnessError, "refusing to send"):
            chat_for_route(self._route(), transport, "or-key", "fw-key",
                           MESSAGES, 64, governor=gov, meta_key="meta-key")
        self.assertEqual(transport.calls, [])


class GovernedCompletionTests(unittest.TestCase):
    def test_ask_path_completion_runs_on_meta_end_to_end(self):
        transport = RecordingTransport(
            _resp({"prompt_tokens": 100, "completion_tokens": 50}))
        gov = SpendGovernor(transport, "meta-key", None, 1.0,
                            meta_budget_usd=1.0)
        out = governed_completion(transport, "meta-key", gov, SPARK,
                                  "What is 2+2?", 256, label="ask")
        self.assertEqual(out["content"], "ok")
        self.assertAlmostEqual(out["cost"], 100 * 1.25e-6 + 50 * 4.25e-6)
        self.assertTrue(out["cost_estimated"])
        url, key, _ = transport.calls[0]
        self.assertEqual(url, META_CHAT_URL)
        self.assertEqual(key, "meta-key")
        self.assertAlmostEqual(gov.meta_spent(), out["cost"])


class DirectChatTests(_IsolatedHome):
    """#447: the lanes that call chat() directly (panel, judge,
    specialist, apply) hold Muse ids under the preset but posted to
    OpenRouter. chat() itself now dispatches a bare Muse id to the
    Meta lane, in the lanes' own positional call shape, so no lane
    needs its own routing code."""

    def _governor(self, transport, budget=1.0):
        return SpendGovernor(transport, "meta-key", None, 1.0,
                             meta_budget_usd=budget)

    def test_panel_shaped_call_posts_to_meta_never_openrouter(self):
        transport = RecordingTransport(
            _resp({"prompt_tokens": 100, "completion_tokens": 50}))
        gov = self._governor(transport)
        status, _ = chat(transport, "meta-key", SPARK, MESSAGES, 512,
                         "auto", 0.4, gov)
        self.assertEqual(status, 200)
        self.assertEqual(len(transport.calls), 1)
        url, key, payload = transport.calls[0]
        self.assertEqual(url, META_CHAT_URL)
        self.assertEqual(key, "meta-key")
        self.assertEqual(set(payload), {"model", "messages", "max_tokens"})
        self.assertNotIn("provider", payload)

    def test_judge_shaped_call_posts_wonder_to_meta(self):
        transport = RecordingTransport(
            _resp({"prompt_tokens": 10, "completion_tokens": 5}))
        gov = self._governor(transport)
        status, _ = chat(transport, "meta-key", WONDER, MESSAGES, 256,
                         "auto", 0.4, gov)
        self.assertEqual(status, 200)
        url, _, payload = transport.calls[0]
        self.assertEqual(url, META_CHAT_URL)
        self.assertEqual(payload["model"], WONDER)

    def test_uncommitted_muse_id_fails_closed_before_any_post(self):
        transport = RecordingTransport(_resp({}))
        gov = self._governor(transport)
        with self.assertRaisesRegex(HarnessError, "no committed Meta endpoint"):
            chat(transport, "meta-key", UNCOMMITTED, MESSAGES, 256,
                 "auto", 0.4, gov)
        self.assertEqual(transport.calls, [])

    def test_meta_budget_refuses_before_send(self):
        transport = RecordingTransport(
            _resp({"prompt_tokens": 10, "completion_tokens": 5}))
        gov = self._governor(transport, budget=0.0)
        with self.assertRaisesRegex(HarnessError,
                                    "would exceed the local budget"):
            chat(transport, "meta-key", SPARK, MESSAGES, 256,
                 "auto", 0.4, gov)
        self.assertEqual(transport.calls, [])

    def test_usage_cost_is_estimated_for_lane_accounting(self):
        # The panel books _reported_cost(resp) after chat(); the Meta
        # lane's token-only usage must arrive with the estimated cost
        # already filled from the committed rates.
        transport = RecordingTransport(
            _resp({"prompt_tokens": 100, "completion_tokens": 50}))
        gov = self._governor(transport)
        _, resp = chat(transport, "meta-key", SPARK, MESSAGES, 256,
                       "auto", 0.4, gov)
        self.assertAlmostEqual(resp["usage"]["cost"],
                               100 * 1.25e-6 + 50 * 4.25e-6)
        self.assertTrue(resp["usage"]["cost_estimated"])

    def test_ordinary_model_still_posts_to_openrouter(self):
        transport = RecordingTransport(
            _resp({"prompt_tokens": 10, "completion_tokens": 5,
                   "cost": 0.001}))
        status, _ = chat(transport, "or-key", OR_MODEL, MESSAGES, 256)
        self.assertEqual(status, 200)
        url, key, _ = transport.calls[0]
        self.assertEqual(url, OPENROUTER_CHAT_URL)
        self.assertEqual(key, "or-key")

    def test_preset_lane_end_to_end(self):
        # Settings under the Muse preset resolve the Meta key through
        # governor_for; a panel vote and the judge call made with that
        # key and the preset's pinned ids both land on the Meta lane.
        os.environ["META_API_KEY"] = "preset-key"
        os.environ["HARNESS_MODEL_PRESET"] = "muse"
        os.environ["HARNESS_META_BUDGET_USD"] = "2.00"
        with redirect_stderr(io.StringIO()):
            settings = load_settings()
        key, gov = governor_for(settings)
        self.assertEqual(key, "preset-key")
        transport = RecordingTransport(
            _resp({"prompt_tokens": 10, "completion_tokens": 5}))
        for model in (settings.panel_pool[0], settings.judge):
            status, _ = chat(transport, key, model, MESSAGES, 256,
                             "auto", 0.4, gov)
            self.assertEqual(status, 200)
        self.assertEqual(len(transport.calls), 2)
        for url, seen_key, _ in transport.calls:
            self.assertEqual(url, META_CHAT_URL)
            self.assertEqual(seen_key, "preset-key")


class VerifyTaskIdTests(_IsolatedHome):
    """Item (d): a local claims-verify run ensures its own task id."""

    def _run(self, task_id, **kwargs):
        captured = {}

        def fake_panel_judge(**kw):
            captured["task_id"] = kw.get("task_id")
            return {"consensus": {"defer": False}}

        def fake_governor(settings, max_cost_override=None):
            gov = mock.Mock()
            gov.max_cost = 1.0
            gov.spent = 0.0
            gov.cost_by_model.return_value = {}
            return "key", gov

        ledger = AutonomyLedger(str(self.home / "ledger.jsonl"))
        with redirect_stderr(io.StringIO()):
            settings = load_settings()
        with mock.patch.object(service, "governor_for",
                               side_effect=fake_governor), \
             mock.patch.object(service, "ledger_for", return_value=ledger), \
             mock.patch.object(service, "panel_judge",
                               side_effect=fake_panel_judge), \
             mock.patch.object(service, "HttpTransport"):
            result = service.run_verify(
                settings, prompt="hi", task_id=task_id,
                attach_meta=False, **kwargs)
        return result, captured, ledger

    def test_supplied_task_id_is_refused_by_default(self):
        with self.assertRaisesRegex(HarnessError, "unknown task_id"):
            self._run("civicscope-ai-answer")

    def test_supplied_task_id_self_offers_when_ensured(self):
        _, captured, ledger = self._run("civicscope-ai-answer",
                                        ensure_task_id=True)
        self.assertEqual(captured["task_id"], "civicscope-ai-answer")
        offers = [e for e in ledger.entries() if e.get("event") == "offer"]
        self.assertEqual([e["task_id"] for e in offers],
                         ["civicscope-ai-answer"])
        # A second run with the same label reuses the recorded offer.
        self._run("civicscope-ai-answer", ensure_task_id=True)
        offers = [e for e in ledger.entries() if e.get("event") == "offer"]
        self.assertEqual(len(offers), 1)

    def test_cli_verify_seam_ensures_its_task_id(self):
        from harness import cli

        def fake_panel_judge(**kw):
            return {"consensus": {"defer": False}}

        def fake_governor(settings, max_cost_override=None):
            gov = mock.Mock()
            gov.max_cost = 1.0
            gov.spent = 0.0
            gov.cost_by_model.return_value = {}
            return "key", gov

        ledger = AutonomyLedger(str(self.home / "ledger.jsonl"))
        with redirect_stderr(io.StringIO()):
            settings = load_settings()
        with mock.patch.object(service, "governor_for",
                               side_effect=fake_governor), \
             mock.patch.object(service, "ledger_for", return_value=ledger), \
             mock.patch.object(service, "panel_judge",
                               side_effect=fake_panel_judge), \
             mock.patch.object(service, "HttpTransport"):
            cli._run_claims_verify(settings, prompt="hi",
                                   task_id="civicscope-ai-meta-client")
        offers = [e for e in ledger.entries() if e.get("event") == "offer"]
        self.assertEqual([e["task_id"] for e in offers],
                         ["civicscope-ai-meta-client"])


if __name__ == "__main__":
    unittest.main()


class _RepeatingTransport:
    """Answers every POST with a fresh 200 body; records URL and key.

    The lane tests below drive real lane entries, so one canned response is
    not enough and a shared mutable body is not safe: each POST builds its own.
    """

    def __init__(self, content="ok"):
        self.content = content
        self.calls = []

    def post(self, url, api_key, payload, timeout=45):
        self.calls.append((url, api_key, payload))
        return 200, {"choices": [{"message": {"content": self.content},
                                  "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

    def get(self, url, api_key, timeout=15):
        if url.endswith("/models"):
            return {"data": [{"id": SPARK,
                              "pricing": {"prompt": "0.00000125",
                                          "completion": "0.00000425"}},
                             {"id": WONDER,
                              "pricing": {"prompt": "0.000004",
                                          "completion": "0.00002"}}]}
        raise AssertionError(f"direct-provider lanes must not GET {url}")


class DirectChatLaneTests(_IsolatedHome):
    """#447: panel, apply and verify call chat() directly.

    Under the Muse preset those lanes hold Muse ids, and chat() used to post a
    Muse id to OpenRouter -- so a Muse-only run was not Muse-transport-only on
    any of the three. Each test drives the real lane entry and reads the
    transport, so a passing config assertion cannot stand in for the dispatch.
    """

    def _assert_meta_only(self, transport, lane):
        self.assertTrue(transport.calls, f"{lane} lane made no call at all")
        self.assertTrue(all(url == META_CHAT_URL for url, _, _ in transport.calls),
                        f"{lane} lane left Meta: {transport.calls}")
        self.assertTrue(all(url != OPENROUTER_CHAT_URL
                            for url, _, _ in transport.calls))
        self.assertTrue(all(key == "meta-key" for _, key, _ in transport.calls),
                        transport.calls)
        self.assertTrue(all(payload["model"] == SPARK
                            for _, _, payload in transport.calls))

    def test_panel_lane_routes_muse_ids_to_meta(self):
        from harness.panel import panel_judge
        transport = _RepeatingTransport("panel take")
        gov = SpendGovernor(transport, "meta-key", None, 1.0,
                            meta_budget_usd=1.0)
        panel_judge(transport=transport, api_key="meta-key", governor=gov,
                    prompt="Q?", panel=[SPARK], judge=SPARK,
                    max_panelists=1, free_tier=False)
        self._assert_meta_only(transport, "panel")

    def test_apply_lane_routes_muse_ids_to_meta(self):
        from harness.apply import ApplyEngine
        from harness.router import Router
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "math.py")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("def add(a, b):\n    return a + b\n")
            transport = _RepeatingTransport(
                "def add(a, b):\n    return a + b + 0\n")
            ledger = AutonomyLedger(os.path.join(tmp, "ledger.jsonl"))
            ledger.ensure_offered_task_id("t1", source="test_setup")
            gov = SpendGovernor(transport, "meta-key", None, 1.0,
                                meta_budget_usd=1.0)
            engine = ApplyEngine(transport, "meta-key", gov, ledger,
                                 Router([SPARK], SPARK, SPARK),
                                 default_require_consent=False)
            try:
                engine.apply_edit(task_id="t1", file_path=path,
                                  instruction="add +0", require_consent=False)
            except HarnessError:
                pass  # the provider dispatch is what this pin is about
            self._assert_meta_only(transport, "apply")

    def test_verify_lane_routes_muse_ids_to_meta(self):
        transport = _RepeatingTransport('{"verdict": "agree"}')
        with redirect_stderr(io.StringIO()):
            settings = load_settings()
        ledger = AutonomyLedger(str(self.home / "ledger.jsonl"))
        gov = SpendGovernor(transport, "meta-key", None, 1.0,
                            meta_budget_usd=1.0)
        service.run_verify(settings, prompt="hi", governor=gov, ledger=ledger,
                           transport=transport, api_key="meta-key",
                           panel=[SPARK], judge=SPARK, attach_meta=False,
                           generate_task_id=True, max_panelists=1,
                           free_tier=False)
        self._assert_meta_only(transport, "verify")
