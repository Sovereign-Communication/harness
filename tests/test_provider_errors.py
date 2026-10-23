"""Hermetic contract tests for provider spend-limit classification and handling (DF-API-1)."""
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from harness.ledger import AutonomyLedger
from harness.provider_errors import (
    ProviderSpendLimitError,
    account_provider_spend_limit,
    provider_spend_limit_error,
    raise_for_provider_spend_limit,
    redact_secrets,
)
from harness import events
from harness.spend import SpendGovernor
from tests._fake import FakeTransport


class ProviderSpendLimitClassificationTests(unittest.TestCase):
    def test_http_402_is_unconditionally_classified(self):
        err = provider_spend_limit_error(402, {"error": {"message": "Payment required"}}, known_cost=0.000015)
        self.assertIsNotNone(err)
        self.assertIsInstance(err, ProviderSpendLimitError)
        self.assertEqual(err.kind, "provider_spend_limit")
        self.assertEqual(err.http_status, 402)
        self.assertAlmostEqual(err.known_cost, 0.000015)
        self.assertIn("Upstream provider spend limit exhausted (HTTP 402)", str(err))

    def test_http_429_with_spend_hint_is_classified(self):
        hints = [
            "account balance is too low",
            "account has insufficient balance",
            "add more credits",
            "billing hard limit",
            "credit balance is too low",
            "credit balance too low",
            "exceeded your current quota, please check your plan and billing",
            "insufficient balance",
            "insufficient credit",
            "insufficient credits",
            "insufficient quota",
            "key spend limit",
            "monthly spend limit",
            "out of credits",
            "payment required",
            "spend limit",
            "spending limit",
        ]
        for hint in hints:
            err = provider_spend_limit_error(
                429,
                {"error": {"message": f"Error: {hint}. Please top up."}},
                known_cost=0.00002,
            )
            self.assertIsNotNone(err, f"Expected classification for hint: {hint}")
            self.assertEqual(err.kind, "provider_spend_limit")
            self.assertEqual(err.http_status, 429)

    def test_http_429_with_spend_code_is_classified(self):
        codes = [
            "billing_hard_limit_reached",
            "billing_not_active",
            "budget_exceeded",
            "credit_balance_too_low",
            "insufficient_credits",
            "insufficient_quota",
            "payment_required",
            "spend_limit_exceeded",
        ]
        for code in codes:
            err = provider_spend_limit_error(
                400,
                {"error": {"code": code, "message": "Transaction declined."}},
                known_cost=0.0,
            )
            self.assertIsNotNone(err, f"Expected classification for code: {code}")
            self.assertEqual(err.kind, "provider_spend_limit")

    def test_generic_429_rate_limit_is_not_classified(self):
        rate_limits = [
            "Rate limit reached for requests per minute",
            "Too Many Requests",
            "Rate limit exceeded: please slow down",
            "server is temporarily overloaded",
        ]
        for msg in rate_limits:
            err = provider_spend_limit_error(
                429,
                {"error": {"message": msg, "code": "rate_limit_exceeded"}},
            )
            self.assertIsNone(err, f"Generic rate limit should not match: {msg}")

    def test_auth_failure_401_is_not_classified(self):
        err = provider_spend_limit_error(
            401,
            {"error": {"message": "Invalid API key provided", "code": "invalid_api_key"}},
        )
        self.assertIsNone(err)

    def test_model_unavailable_503_is_not_classified(self):
        err = provider_spend_limit_error(
            503,
            {"error": {"message": "Model provider endpoint down", "code": "provider_unavailable"}},
        )
        self.assertIsNone(err)

    def test_successful_200_discussing_billing_is_not_classified(self):
        content = "You can manage your spend limit and add credits in account settings."
        err = provider_spend_limit_error(
            200,
            {"choices": [{"message": {"content": content}}]},
        )
        self.assertIsNone(err)

    def test_nested_metadata_spend_hint_is_classified(self):
        err = provider_spend_limit_error(
            429,
            {"error": {"message": "Provider error", "metadata": {"raw": "account has insufficient balance"}}},
            known_cost=0.000005,
        )
        self.assertIsNotNone(err)
        self.assertEqual(err.kind, "provider_spend_limit")

    def test_raise_for_provider_spend_limit_raises_only_when_matched(self):
        with self.assertRaises(ProviderSpendLimitError):
            raise_for_provider_spend_limit(402, {"error": {"message": "Payment required"}})

        # Should not raise on regular 429
        raise_for_provider_spend_limit(429, {"error": {"message": "Rate limit exceeded"}})


class SecretRedactionTests(unittest.TestCase):
    def test_redact_secrets_strips_api_keys(self):
        raw = "Error with key sk-or-v1-abcdef0123456789abcdef and Bearer token1234567890abcdef"
        cleaned = redact_secrets(raw)
        self.assertNotIn("sk-or-v1-abcdef0123456789abcdef", cleaned)
        self.assertNotIn("token1234567890abcdef", cleaned)
        self.assertIn("[REDACTED]", cleaned)

    def test_error_message_does_not_contain_api_key(self):
        secret = "sk-live-0123456789abcdef0123456789abcdef"
        err = ProviderSpendLimitError(f"Exhausted key for Bearer {secret}", http_status=402)
        rendered = str(err)
        self.assertNotIn(secret, rendered)
        self.assertIn("Upstream provider spend limit exhausted", rendered)


class AccountingAndLedgerTests(unittest.TestCase):
    def test_account_provider_spend_limit_records_governor_and_ledger(self):
        gov = SpendGovernor(FakeTransport(), "sk-test", max_cost=0.10)
        with tempfile.TemporaryDirectory() as td:
            ledger_path = os.path.join(td, "ledger.jsonl")
            settings = SimpleNamespace(ledger_path=ledger_path)
            err = ProviderSpendLimitError("out of credits", http_status=402, known_cost=0.000042)

            with mock.patch("harness.config.load_settings", return_value=settings):
                account_provider_spend_limit(err, gov, "deepseek/deepseek-v4.1-flash")

            self.assertTrue(err.cost_accounted)
            self.assertTrue(err.ledger_recorded)
            self.assertAlmostEqual(gov.spent, 0.000042)

            ledger = AutonomyLedger(ledger_path)
            receipts = [r for r in ledger.entries() if r.get("event_note") == "provider_spend_limit"]
            self.assertEqual(len(receipts), 1)
            self.assertEqual(receipts[0]["error_kind"], "provider_spend_limit")
            self.assertAlmostEqual(receipts[0]["cost"], 0.000042)

            # Idempotent: second call does not double count
            with mock.patch("harness.config.load_settings", return_value=settings):
                account_provider_spend_limit(err, gov, "deepseek/deepseek-v4.1-flash")
            self.assertAlmostEqual(gov.spent, 0.000042)
            ledger2 = AutonomyLedger(ledger_path)
            self.assertEqual(len([r for r in ledger2.entries() if r.get("event_note") == "provider_spend_limit"]), 1)

    def test_account_provider_spend_limit_excludes_jev_duplicate_ledger(self):
        gov = SpendGovernor(FakeTransport(), "sk-test", max_cost=0.10)
        with tempfile.TemporaryDirectory() as td:
            ledger_path = os.path.join(td, "ledger.jsonl")
            settings = SimpleNamespace(ledger_path=ledger_path)
            err = ProviderSpendLimitError("out of credits", http_status=402, known_cost=0.000010)

            with mock.patch("harness.config.load_settings", return_value=settings):
                account_provider_spend_limit(err, gov, "jev-1.13.0")

            self.assertTrue(err.cost_accounted)
            # Ledger is NOT recorded by account_provider_spend_limit because Jev handles its own jev_eval
            self.assertFalse(err.ledger_recorded)
            self.assertAlmostEqual(gov.spent, 0.000010)
            self.assertFalse(os.path.exists(ledger_path))


class SpendLimitLatchTests(unittest.TestCase):
    def test_latch_trips_and_resets_for_execution_context(self):
        with events.task_context("test-task"):
            # Initially not limited
            events.raise_if_provider_spend_limited()

            # Tripping the latch
            err = ProviderSpendLimitError("balance exhausted", http_status=402)
            events.mark_provider_spend_limit(err)

            with self.assertRaises(ProviderSpendLimitError):
                events.raise_if_provider_spend_limited()

            # Resetting latch allows subsequent execution
            events.reset_provider_spend_limit_latch()
            events.raise_if_provider_spend_limited()

    def test_executor_latch_reset_allows_executor_reuse(self):
        executor = SimpleNamespace()
        # Simulate tripped latch on executor
        executor._harness_terminal_spend_limit = {"error": {"http_status": 402}}
        self.assertTrue(bool(executor._harness_terminal_spend_limit))

        events.reset_executor_spend_limit(executor)
        self.assertEqual(executor._harness_terminal_spend_limit, {})


class ProviderErrorsEdgeCasesTests(unittest.TestCase):
    def test_redact_secrets_empty_returns_empty(self):
        self.assertEqual(redact_secrets(""), "")
        self.assertEqual(redact_secrets(None), "")

    def test_provider_spend_limit_invalid_known_cost(self):
        err = ProviderSpendLimitError("cap", known_cost="invalid")
        self.assertEqual(err.known_cost, 0.0)

    def test_provider_spend_limit_nested_depth_and_lists(self):
        err = provider_spend_limit_error(
            429,
            {"error": ["item1", {"detail": "insufficient credits"}]},
            known_cost=0.00001,
        )
        self.assertIsNotNone(err)
        self.assertEqual(err.http_status, 429)

        deep = {"error": {"a": {"b": {"c": {"d": {"e": "insufficient credits"}}}}}}
        err_deep = provider_spend_limit_error(429, deep)
        self.assertIsNone(err_deep)

        err_bad_status = provider_spend_limit_error("not_an_int", {"error": "out of credits"})
        self.assertIsNone(err_bad_status)

    def test_account_provider_spend_limit_with_task_id(self):
        gov = SpendGovernor(FakeTransport(), "sk-test", max_cost=0.10)
        with tempfile.TemporaryDirectory() as td:
            ledger_path = os.path.join(td, "ledger.jsonl")
            settings = SimpleNamespace(ledger_path=ledger_path)
            err = ProviderSpendLimitError("out of credits", http_status=402, known_cost=0.000042)

            with events.task_context("task-xyz"), \
                 mock.patch("harness.config.load_settings", return_value=settings):
                account_provider_spend_limit(err, gov, "deepseek/deepseek-v4.1-flash")

            ledger = AutonomyLedger(ledger_path)
            receipts = [r for r in ledger.entries() if r.get("event_note") == "provider_spend_limit"]
            self.assertEqual(len(receipts), 1)
            self.assertEqual(receipts[0].get("task_id"), "task-xyz")

    def test_account_provider_spend_limit_ledger_write_failure_is_swallowed(self):
        gov = SpendGovernor(FakeTransport(), "sk-test", max_cost=0.10)
        with tempfile.TemporaryDirectory() as td:
            ledger_path = os.path.join(td, "ledger.jsonl")
            settings = SimpleNamespace(ledger_path=ledger_path)
            err = ProviderSpendLimitError("out of credits", http_status=402, known_cost=0.000042)

            with mock.patch("harness.config.load_settings", return_value=settings), \
                 mock.patch("harness.ledger.AutonomyLedger.append", side_effect=RuntimeError("disk full")), \
                 mock.patch("harness.output.eprint") as mock_eprint:
                account_provider_spend_limit(err, gov, "deepseek/deepseek-v4.1-flash")
                mock_eprint.assert_called_once()
                self.assertIn("provider spend receipt was not written", mock_eprint.call_args[0][0])
            self.assertTrue(err.cost_accounted)
            self.assertFalse(err.ledger_recorded)

    def test_account_provider_spend_limit_overrun_recording(self):
        gov = mock.MagicMock()
        from harness.errors import HarnessError
        gov.record_actual.side_effect = HarnessError("limit exceeded")
        err = ProviderSpendLimitError("out of credits", http_status=402, known_cost=0.000042)
        err.ledger_recorded = True
        account_provider_spend_limit(err, gov, "deepseek/deepseek-v4.1-flash")
        gov.record_overrun.assert_called_once_with(0.000042, "deepseek/deepseek-v4.1-flash")
        self.assertTrue(err.cost_accounted)


if __name__ == "__main__":
    unittest.main()

