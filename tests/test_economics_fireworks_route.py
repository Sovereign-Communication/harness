"""Production two-provider contracts: real chat dispatcher, hermetic transport."""
import threading
import unittest
from unittest import mock

from harness.chat import chat, record_response
from harness.config import FIREWORKS_CHAT_URL, OPENROUTER_CHAT_URL, load_settings
from harness.errors import HarnessError
from harness.fireworks import resolve_offer
from harness.spend import SpendGovernor

MODEL = "z-ai/glm-5.3-flash"
DIRECT = "accounts/fireworks/models/nemotron-lightning-3p5-30b-a3b"
MESSAGES = [{"role": "user", "content": "Reply OK"}]


class Wire:
    """Prices and completions are supplied, not retrieved from a provider."""

    def __init__(self, rates=(1e-6, 2e-6), responses=None):
        self.rates = rates
        self.responses = list(responses or [])
        self.calls = []

    def get(self, url, key, timeout=15):
        return {"data": {"endpoints": [{"provider_name": "confirmed-provider",
                 "pricing": {"prompt": self.rates[0], "completion": self.rates[1]}}]}}

    def post(self, url, key, payload):
        self.calls.append((url, key, payload))
        if self.responses:
            item = self.responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return 200, {"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": 5, "completion_tokens": 2}}


def gov(wire=None, cap=0.01):
    wire = wire or Wire()
    return SpendGovernor(wire, "or-key", max_cost=0.02, fireworks_key="fw-key",
                         fireworks_budget_usd=cap, fireworks_enabled=True)


class DispatchTests(unittest.TestCase):
    def setUp(self):
        from datetime import date
        clock = mock.patch("harness.endpoint_pricing.date", wraps=date)
        clock.start().today.return_value = date(2026, 10, 10)
        self.addCleanup(clock.stop)

    def test_stale_price_pack_refuses_before_dispatch(self):
        from datetime import date
        with mock.patch("harness.endpoint_pricing.date", wraps=date) as clock:
            clock.today.return_value = date(2026, 10, 20)
            g = gov()
            with self.assertRaisesRegex(HarnessError, "stale"):
                chat(g.transport, "or-key", DIRECT, MESSAGES, 8, governor=g)
            self.assertEqual(g.transport.calls, [])

    def test_cheaper_fireworks_is_production_dispatch_and_bills_once(self):
        g = gov()
        status, response = chat(g.transport, "or-key", MODEL, MESSAGES, 8, governor=g)
        self.assertEqual(status, 200)
        self.assertEqual(g.transport.calls[0][0], FIREWORKS_CHAT_URL)
        self.assertEqual(g.transport.calls[0][1], "fw-key")
        self.assertEqual(set(g.transport.calls[0][2]), {"model", "messages", "max_tokens"})
        expected = 5 * 0.15e-6 + 2 * 0.5e-6
        self.assertAlmostEqual(g.spent, expected)
        record_response(g, response, response["usage"]["cost"], MODEL)
        self.assertAlmostEqual(g.spent, expected)
        self.assertEqual(g.outstanding, 0)
        self.assertEqual(len(g.route_comparisons[0]["offers"]), 2)

    def test_cheaper_openrouter_pins_whole_endpoint(self):
        wire = Wire(rates=(1e-8, 2e-8))
        g = gov(wire)
        with mock.patch.object(g, "is_free", return_value=False):
            status, response = chat(wire, "or-key", MODEL, MESSAGES, 8, governor=g)
        self.assertEqual(status, 200)
        url, key, payload = wire.calls[0]
        self.assertEqual((url, key), (OPENROUTER_CHAT_URL, "or-key"))
        self.assertEqual(payload["model"], MODEL)
        self.assertEqual(payload["provider"]["only"], ["confirmed-provider"])
        self.assertFalse(payload["provider"]["allow_fallbacks"])
        self.assertAlmostEqual(response["usage"]["cost"], 5e-8 + 4e-8)

    def test_429_failover_uses_other_provider_without_extra_model(self):
        wire = Wire(responses=[(429, {"error": {"message": "rate limited"}})])
        g = gov(wire)
        with mock.patch.object(g, "is_free", return_value=False), \
                mock.patch.object(g, "fetch_pricing", return_value={MODEL: wire.rates}):
            status, _ = chat(wire, "or-key", MODEL, MESSAGES, 8, governor=g)
        self.assertEqual(status, 200)
        self.assertEqual([c[0] for c in wire.calls], [FIREWORKS_CHAT_URL, OPENROUTER_CHAT_URL])
        self.assertEqual(g.outstanding, 0)

    def test_explicit_path_never_fails_over(self):
        wire = Wire(responses=[(503, {"error": {"message": "unavailable"}})])
        g = gov(wire)
        status, _ = chat(wire, "or-key", DIRECT, MESSAGES, 8, governor=g)
        self.assertEqual(status, 503)
        self.assertEqual(len(wire.calls), 1)

    def test_disabled_and_unknown_path_refuse_without_post(self):
        g = gov()
        g.fireworks_enabled = False
        with self.assertRaisesRegex(HarnessError, "disabled"):
            chat(g.transport, "or-key", DIRECT, MESSAGES, 8, governor=g)
        g.fireworks_enabled = True
        with self.assertRaisesRegex(HarnessError, "no confirmed"):
            chat(g.transport, "or-key", "accounts/fireworks/models/unknown", MESSAGES, 8, governor=g)
        self.assertEqual(g.transport.calls, [])

    def test_missing_key_and_zero_budget_refuse_before_post(self):
        g = gov(cap=0)
        with self.assertRaisesRegex(HarnessError, "local budget"):
            chat(g.transport, "or-key", DIRECT, MESSAGES, 8, governor=g)
        g.fireworks_key = None
        with self.assertRaisesRegex(HarnessError, "key missing"):
            chat(g.transport, "or-key", DIRECT, MESSAGES, 8, governor=g)
        self.assertEqual(g.transport.calls, [])

    def test_missing_usage_keeps_liability_and_stops(self):
        wire = Wire(responses=[(200, {"choices": []})])
        g = gov(wire)
        with self.assertRaisesRegex(HarnessError, "bill blind"):
            chat(wire, "or-key", DIRECT, MESSAGES, 8, governor=g)
        self.assertGreater(g.spent, 0)
        self.assertEqual(g.outstanding, 0)

    def test_ambiguous_timeout_does_not_retry_and_keeps_liability(self):
        wire = Wire(responses=[TimeoutError("uncertain dispatch")])
        g = gov(wire)
        with self.assertRaises(TimeoutError):
            chat(wire, "or-key", MODEL, MESSAGES, 8, governor=g)
        self.assertEqual(len(wire.calls), 1)
        self.assertGreater(g.spent, 0)
        self.assertEqual(g.outstanding, 0)

    def test_reported_overrun_is_booked_then_next_call_stops(self):
        wire = Wire(responses=[(200, {"usage": {"cost": 0.011}})])
        g = gov(wire)
        with self.assertRaisesRegex(HarnessError, "local budget"):
            chat(wire, "or-key", DIRECT, MESSAGES, 8, governor=g)
        self.assertEqual(g.spent, 0.011)
        self.assertEqual(g.outstanding, 0)
        with self.assertRaises(HarnessError):
            chat(wire, "or-key", DIRECT, MESSAGES, 8, governor=g)
        self.assertEqual(len(wire.calls), 1)

    def test_reviewed_mapping_preserves_same_model_identity(self):
        offer = resolve_offer("accounts/fireworks/models/glm-5p3-flash")
        self.assertEqual(offer.canonical_model, MODEL)

    def test_direct_pricing_never_queries_openrouter(self):
        g = gov()
        with mock.patch.object(g.transport, "get", side_effect=AssertionError("network")):
            prices = g.fetch_pricing([DIRECT])
            g.preflight("prompt", [("direct", DIRECT, 8, 0)])
        self.assertAlmostEqual(prices[DIRECT][0], 0.05e-6, places=15)
        self.assertAlmostEqual(prices[DIRECT][1], 0.2e-6, places=15)

    def test_provider_only_governor_dispatches_direct_path(self):
        g = gov()
        g.openrouter_enabled = False
        g.api_key = None
        status, _ = chat(g.transport, None, DIRECT, MESSAGES, 8, governor=g)
        self.assertEqual(status, 200)
        self.assertEqual(g.transport.calls[0][0], FIREWORKS_CHAT_URL)

    def test_concurrent_budget_checks_include_outstanding(self):
        g = gov(cap=0.001)
        tokens = []
        lock = threading.Lock()
        def reserve():
            try:
                token = g.reserve(0.0006, DIRECT)
            except HarnessError:
                return
            with lock:
                tokens.append(token)
        threads = [threading.Thread(target=reserve) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(tokens), 1)
        g.reconcile(tokens[0], 0.0005)
        self.assertAlmostEqual(g.spent, 0.0005)

    def test_settings_carries_caps_not_fireworks_key(self):
        with mock.patch("harness.config.resolve_api_key", return_value=None), \
                mock.patch("harness.config.resolve_jev_key", return_value=None):
            settings = load_settings(overrides={"fireworks_enabled": True,
                                               "fireworks_budget_usd": 0.01})
        self.assertTrue(settings.fireworks_enabled)
        self.assertEqual(settings.fireworks_budget_usd, 0.01)
        self.assertNotIn("fireworks_key", settings.to_dict())


if __name__ == "__main__":
    unittest.main()
