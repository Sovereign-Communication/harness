"""EV-7: the Fireworks lane's route, toggles, key lookup, call shape, and budget.

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

from harness import config
from harness.chat import chat_for_route
from harness.config import FIREWORKS_CHAT_URL, OPENROUTER_CHAT_URL, load_settings
from harness.errors import HarnessError
from harness.fireworks import (PROVIDER_FIREWORKS,
                               cost_estimate, resolve_offer, resolve_route)
from harness.spend import SpendGovernor

NEMO = "accounts/fireworks/models/nemotron-lightning-3p5-30b-a3b"
UNCONFIRMED = "accounts/fireworks/models/ember-1"
OR_MODEL = "vendor/some-model"
MESSAGES = [{"role": "user", "content": "Reply with the single word: ok"}]
_EV7_ENV = ("FIREWORKS_API_KEY", "HARNESS_OPENROUTER_ENABLED",
            "HARNESS_FIREWORKS_ENABLED", "HARNESS_FIREWORKS_BUDGET_USD")


class RecordingTransport:
    """Returns one canned response and records every POST."""

    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    def post(self, url, api_key, payload, timeout=45):
        self.calls.append((url, api_key, payload))
        return 200, self.resp

    def get(self, url, api_key, timeout=15):
        raise AssertionError(f"EV-7 tests must not GET {url}")


def _resp(usage):
    return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
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
        for name in _EV7_ENV:
            os.environ.pop(name, None)
        self._cfg = mock.patch.object(config, "CONFIG_DIR", str(self.config_dir))
        self._cfg.start()

    def tearDown(self):
        self._cfg.stop()
        self._env.stop()
        self._tmp.cleanup()

    def _write(self, rel, text):
        path = self.home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


class RouteTests(unittest.TestCase):
    def test_ordinary_models_stay_on_openrouter(self):
        route = resolve_route(OR_MODEL, openrouter_enabled=True,
                              fireworks_enabled=False)
        self.assertEqual(route.provider, "openrouter")
        self.assertEqual(route.wire_model, OR_MODEL)

    def test_fireworks_path_is_refused_while_fireworks_is_disabled(self):
        with self.assertRaisesRegex(HarnessError, "fireworks is disabled"):
            resolve_route(NEMO, openrouter_enabled=True, fireworks_enabled=False)

    def test_confirmed_fireworks_path_routes_to_fireworks_when_enabled(self):
        route = resolve_route(NEMO, openrouter_enabled=True, fireworks_enabled=True)
        self.assertEqual(route.provider, PROVIDER_FIREWORKS)
        self.assertEqual(route.wire_model, NEMO)
        self.assertEqual(route.offer.model, "Nemotron Lightning 3.5 30B A3B")

    def test_unconfirmed_fireworks_path_raises_instead_of_guessing(self):
        with self.assertRaisesRegex(HarnessError, "no confirmed Fireworks endpoint"):
            resolve_route(UNCONFIRMED, openrouter_enabled=True,
                          fireworks_enabled=True)

    def test_a_fireworks_path_never_falls_back_to_openrouter(self):
        with self.assertRaises(HarnessError):
            resolve_route(UNCONFIRMED, openrouter_enabled=True,
                          fireworks_enabled=False)

    def test_ordinary_model_with_openrouter_disabled_raises(self):
        with self.assertRaisesRegex(HarnessError, "OpenRouter is disabled"):
            resolve_route(OR_MODEL, openrouter_enabled=False,
                          fireworks_enabled=True)

    def test_offer_lookup_needs_a_routable_row(self):
        self.assertEqual(resolve_offer(NEMO).model, "Nemotron Lightning 3.5 30B A3B")
        with self.assertRaises(HarnessError):
            resolve_offer(UNCONFIRMED)


class ToggleTests(_IsolatedHome):
    def test_fireworks_is_off_by_default_with_a_zero_budget(self):
        s = load_settings()
        self.assertTrue(s.openrouter_enabled)
        self.assertFalse(s.fireworks_enabled)
        self.assertEqual(s.fireworks_budget_usd, 0.0)

    def test_both_providers_off_is_refused_before_anything_loads(self):
        with self.assertRaisesRegex(HarnessError, "at least one provider"):
            load_settings(overrides={"openrouter_enabled": False,
                                     "fireworks_enabled": False})

    def test_fireworks_can_be_the_only_enabled_provider(self):
        s = load_settings(overrides={"openrouter_enabled": False,
                                     "fireworks_enabled": True,
                                     "fireworks_budget_usd": 0.05})
        self.assertFalse(s.openrouter_enabled)
        self.assertTrue(s.fireworks_enabled)
        self.assertEqual(s.fireworks_budget_usd, 0.05)

    def test_env_var_turns_fireworks_on(self):
        os.environ["HARNESS_FIREWORKS_ENABLED"] = "1"
        os.environ["HARNESS_FIREWORKS_BUDGET_USD"] = "0.02"
        s = load_settings()
        self.assertTrue(s.fireworks_enabled)
        self.assertEqual(s.fireworks_budget_usd, 0.02)

    def test_toggles_are_settings_but_no_key_is(self):
        s = load_settings(overrides={"fireworks_enabled": True,
                                     "fireworks_budget_usd": 0.05})
        d = s.to_dict()
        self.assertEqual(d["fireworks_enabled"], True)
        self.assertEqual(d["fireworks_budget_usd"], 0.05)
        self.assertFalse(any("fireworks" in k and "key" in k for k in d))


class KeyTests(_IsolatedHome):
    def test_scmorc_file_wins_over_harness_file_and_env(self):
        self._write(".config/scmorc/fireworks.env", "FIREWORKS_API_KEY=scmorc-key\n")
        self._write(".config/harness/fireworks.env", "FIREWORKS_API_KEY=harness-key\n")
        os.environ["FIREWORKS_API_KEY"] = "env-key"
        self.assertEqual(config.resolve_fireworks_key(), "scmorc-key")

    def test_harness_file_is_the_second_source(self):
        self._write(".config/harness/fireworks.env", "FIREWORKS_API_KEY=harness-key\n")
        self.assertEqual(config.resolve_fireworks_key(), "harness-key")

    def test_env_is_used_last_and_its_value_is_never_logged(self):
        os.environ["FIREWORKS_API_KEY"] = "env-secret-value"
        err = io.StringIO()
        with redirect_stderr(err):
            key = config.resolve_fireworks_key()
        self.assertEqual(key, "env-secret-value")
        self.assertIn("FIREWORKS_API_KEY", err.getvalue())
        self.assertNotIn("env-secret-value", err.getvalue())

    def test_openrouter_key_in_the_file_is_not_read_as_fireworks(self):
        self._write(".config/harness/fireworks.env", "OPENROUTER_API_KEY=or-key\n")
        self.assertIsNone(config.resolve_fireworks_key())

    def test_no_key_anywhere_resolves_to_none(self):
        self.assertIsNone(config.resolve_fireworks_key())


class ChatTests(unittest.TestCase):
    def _governor(self, transport, budget=1.0):
        return SpendGovernor(transport, "or-key", max_cost=1.0,
                             fireworks_budget_usd=budget)

    def test_fireworks_call_goes_to_the_fireworks_url_with_a_plain_payload(self):
        transport = RecordingTransport(_resp({"cost": 0.0}))
        route = resolve_route(NEMO, openrouter_enabled=True, fireworks_enabled=True)
        chat_for_route(route, transport, "or-key", "fw-key", MESSAGES, 8,
                       governor=self._governor(transport))
        url, key, payload = transport.calls[0]
        self.assertEqual(url, FIREWORKS_CHAT_URL)
        self.assertEqual(key, "fw-key")
        self.assertEqual(set(payload), {"model", "messages", "max_tokens"})
        self.assertEqual(payload["model"], NEMO)
        self.assertEqual(payload["max_tokens"], 8)

    def test_openrouter_route_still_calls_openrouter(self):
        transport = RecordingTransport(_resp({"cost": 0.0}))
        route = resolve_route(OR_MODEL, openrouter_enabled=True,
                              fireworks_enabled=False)
        chat_for_route(route, transport, "or-key", None, MESSAGES, 8,
                       governor=self._governor(transport))
        self.assertEqual(transport.calls[0][0], OPENROUTER_CHAT_URL)
        self.assertEqual(transport.calls[0][1], "or-key")

    def test_missing_fireworks_cost_is_estimated_from_the_pack_rates(self):
        transport = RecordingTransport(
            _resp({"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}))
        route = resolve_route(NEMO, openrouter_enabled=True, fireworks_enabled=True)
        _, resp = chat_for_route(route, transport, "or-key", "fw-key", MESSAGES, 8,
                                 governor=self._governor(transport))
        # Nemotron Standard: $0.05 in, $0.20 out per 1M tokens.
        self.assertAlmostEqual(resp["usage"]["cost"], 0.25)
        self.assertTrue(resp["usage"]["cost_estimated"])

    def test_fireworks_usage_with_no_cost_and_no_tokens_is_refused(self):
        transport = RecordingTransport(_resp({}))
        route = resolve_route(NEMO, openrouter_enabled=True, fireworks_enabled=True)
        with self.assertRaisesRegex(HarnessError, "refusing to bill blind"):
            chat_for_route(route, transport, "or-key", "fw-key", MESSAGES, 8,
                           governor=self._governor(transport))

    def test_worst_case_over_the_local_budget_is_refused_before_dispatch(self):
        transport = RecordingTransport(_resp({"cost": 0.0}))
        route = resolve_route(NEMO, openrouter_enabled=True, fireworks_enabled=True)
        with self.assertRaisesRegex(HarnessError, "local budget"):
            chat_for_route(route, transport, "or-key", "fw-key", MESSAGES, 8,
                           governor=self._governor(transport, budget=0.0))
        self.assertEqual(transport.calls, [])

    def test_missing_fireworks_key_is_refused_before_dispatch(self):
        transport = RecordingTransport(_resp({"cost": 0.0}))
        route = resolve_route(NEMO, openrouter_enabled=True, fireworks_enabled=True)
        with self.assertRaisesRegex(HarnessError, "Fireworks key missing"):
            chat_for_route(route, transport, "or-key", None, MESSAGES, 8,
                           governor=self._governor(transport))
        self.assertEqual(transport.calls, [])


class BudgetTests(unittest.TestCase):
    def test_only_fireworks_labelled_spend_counts_against_the_fireworks_cap(self):
        gov = SpendGovernor(RecordingTransport({}), "k", max_cost=1.0,
                            fireworks_budget_usd=1.0)
        gov.record_actual(0.4, NEMO)
        gov.record_actual(0.5, OR_MODEL)
        self.assertAlmostEqual(gov.fireworks_spent(), 0.4)

    def test_recording_fireworks_spend_past_the_cap_is_refused(self):
        gov = SpendGovernor(RecordingTransport({}), "k", max_cost=1.0,
                            fireworks_budget_usd=0.3)
        with self.assertRaisesRegex(HarnessError, "local budget"):
            gov.record_actual(0.4, NEMO)

    def test_openrouter_spend_does_not_consume_the_fireworks_budget(self):
        gov = SpendGovernor(RecordingTransport({}), "k", max_cost=1.0,
                            fireworks_budget_usd=0.0)
        gov.record_actual(0.01, OR_MODEL)
        self.assertEqual(gov.fireworks_spent(), 0.0)

    def test_cost_estimate_is_linear_in_both_token_counts(self):
        offer = resolve_offer(NEMO)
        self.assertAlmostEqual(cost_estimate(offer, 1_000_000, 0), 0.05)
        self.assertAlmostEqual(cost_estimate(offer, 0, 1_000_000), 0.20)


class NoNetworkTests(unittest.TestCase):
    def test_routing_chat_and_budget_open_no_socket(self):
        def refuse(*_a, **_k):
            raise AssertionError("EV-7 must not touch the network")

        transport = RecordingTransport(
            _resp({"prompt_tokens": 10, "completion_tokens": 10}))
        route = resolve_route(NEMO, openrouter_enabled=True, fireworks_enabled=True)
        with mock.patch.object(socket, "socket", side_effect=refuse), \
                mock.patch.object(socket, "create_connection", side_effect=refuse), \
                mock.patch.object(socket, "getaddrinfo", side_effect=refuse):
            gov = SpendGovernor(transport, "or-key", max_cost=1.0,
                                fireworks_budget_usd=1.0)
            chat_for_route(route, transport, "or-key", "fw-key", MESSAGES, 8,
                           governor=gov)


if __name__ == "__main__":
    unittest.main()
