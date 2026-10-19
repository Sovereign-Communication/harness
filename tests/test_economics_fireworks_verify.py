"""EV-8: the single paid Fireworks verification probe, without paying.

Hermetic. A recording transport stands in for Fireworks, the marker and
receipt go to temp paths (never the real CONFIG_DIR or dogfood dir), and a
stub ledger records rows. A socket guard proves no test connects anywhere.
The real paid call runs only with the confirmation list answered in writing
and a budget set -- nothing here sends it.
"""
import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness.cli_parser import build_parser
from harness.errors import HarnessError
from harness.fireworks_probe import (VERIFY_MAX_TOKENS, run_fireworks_probe)
from harness.spend import SpendGovernor

NEMO = "accounts/fireworks/models/nemotron-lightning-3p5-30b-a3b"
UNCONFIRMED = "accounts/fireworks/models/ember-1"


class RecordingTransport:
    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    def post(self, url, api_key, payload, timeout=45):
        self.calls.append((url, api_key, payload))
        status, body = self.resp
        return status, body

    def get(self, url, api_key, timeout=15):
        raise AssertionError(f"EV-8 tests must not GET {url}")


class StubLedger:
    def __init__(self):
        self.rows = []

    def append(self, event, task_id=None, **fields):
        self.rows.append({"event": event, "task_id": task_id, **fields})


def _ok_resp(usage):
    return (200, {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                  "usage": usage})


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.marker = str(Path(self._tmp.name) / "marker.json")
        self.receipt = str(Path(self._tmp.name) / "receipt.json")
        self.ledger = StubLedger()

    def tearDown(self):
        self._tmp.cleanup()

    def _gov(self, transport, budget=1.0):
        return SpendGovernor(transport, "or-key", max_cost=1.0,
                             fireworks_budget_usd=budget)

    def _run(self, transport, **kw):
        args = dict(model=NEMO, max_cost=0.01, fireworks_enabled=True,
                    marker_path=self.marker, receipt_path=self.receipt)
        args.update(kw)
        return run_fireworks_probe(transport, "or-key", "fw-key",
                                   self._gov(transport), self.ledger, **args)

    def test_success_reports_only_status_okness_counts_and_cost(self):
        t = RecordingTransport(_ok_resp({"cost": 0.0001}))
        report = self._run(t)
        self.assertEqual(report["status"], 200)
        self.assertTrue(report["contains_ok"])
        self.assertEqual(set(report),
                         {"status", "contains_ok", "prompt_tokens",
                          "completion_tokens", "estimated_cost",
                          "cost_estimated", "model", "offer_model"})
        self.assertNotIn("fw-key", json.dumps(report))

    def test_success_writes_one_ledger_row_with_the_route_fields(self):
        t = RecordingTransport(_ok_resp({"cost": 0.0001}))
        self._run(t)
        self.assertEqual(len(self.ledger.rows), 1)
        row = self.ledger.rows[0]
        self.assertEqual(row["event"], "fireworks_verify")
        self.assertEqual(row["provider"], "fireworks")
        self.assertEqual(row["wire_model"], NEMO)
        self.assertEqual(row["route_reason"], "verify")

    def test_success_writes_marker_and_receipt(self):
        t = RecordingTransport(_ok_resp({"cost": 0.0001}))
        self._run(t)
        self.assertTrue(os.path.exists(self.marker))
        receipt = json.loads(Path(self.receipt).read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], 200)
        self.assertTrue(receipt["contains_ok"])

    def test_estimated_cost_path_flags_the_estimate(self):
        t = RecordingTransport(
            _ok_resp({"prompt_tokens": 1_000_000, "completion_tokens": 0}))
        report = self._run(t)
        self.assertAlmostEqual(report["estimated_cost"], 0.05)
        self.assertTrue(report["cost_estimated"])
        self.assertTrue(self.ledger.rows[0]["cost_estimated"])

    def test_second_run_is_refused_without_touching_the_network(self):
        t = RecordingTransport(_ok_resp({"cost": 0.0001}))
        self._run(t)
        calls = len(t.calls)
        with self.assertRaisesRegex(HarnessError, "already ran"):
            self._run(t)
        self.assertEqual(len(t.calls), calls)

    def test_marker_is_written_before_dispatch_so_failure_stays_one_shot(self):
        t = RecordingTransport((500, {"error": {"message": "boom"}}))
        with self.assertRaisesRegex(HarnessError, "HTTP 500"):
            self._run(t)
        self.assertTrue(os.path.exists(self.marker))
        with self.assertRaisesRegex(HarnessError, "already ran"):
            self._run(t)

    def test_failure_body_is_sanitized_and_never_carries_the_key(self):
        t = RecordingTransport(
            (500, {"error": {"message": "bad key fw-key " + "x" * 2000}}))
        try:
            self._run(t)
            self.fail("expected HarnessError")
        except HarnessError as e:
            self.assertIn("HTTP 500", str(e))
            self.assertNotIn("fw-key", str(e))
            self.assertIn("[redacted]", str(e))
            self.assertIn("[truncated]", str(e))

    def test_unconfirmed_model_is_refused_before_any_marker(self):
        t = RecordingTransport(_ok_resp({"cost": 0.0}))
        with self.assertRaises(HarnessError):
            self._run(t, model=UNCONFIRMED)
        self.assertFalse(os.path.exists(self.marker))
        self.assertEqual(t.calls, [])

    def test_non_path_model_is_a_governed_refusal_not_an_attribute_error(self):
        for bad in ("", None, 123, "vendor/some-model"):
            t = RecordingTransport(_ok_resp({"cost": 0.0}))
            with self.assertRaises(HarnessError, msg=f"model={bad!r}"):
                self._run(t, model=bad)
            self.assertEqual(t.calls, [])

    def test_success_without_any_usage_accounting_is_refused_not_free(self):
        # Each refusal burns the one shot (marker precedes dispatch), so
        # every subcase gets fresh marker/receipt paths.
        for usage in (None, {}, {"prompt_tokens": 0, "completion_tokens": 0}):
            with tempfile.TemporaryDirectory() as tmp:
                t = RecordingTransport(_ok_resp(usage))
                gov = SpendGovernor(t, "or-key", max_cost=1.0,
                                     fireworks_budget_usd=1.0)
                with self.assertRaisesRegex(HarnessError, "bill blind"):
                    run_fireworks_probe(
                        t, "or-key", "fw-key", gov, StubLedger(),
                        model=NEMO, max_cost=0.01, fireworks_enabled=True,
                        marker_path=str(Path(tmp) / "m"),
                        receipt_path=str(Path(tmp) / "r"))
        with tempfile.TemporaryDirectory() as tmp:
            t = RecordingTransport((200, "just a string"))
            gov = SpendGovernor(t, "or-key", max_cost=1.0,
                                 fireworks_budget_usd=1.0)
            with self.assertRaisesRegex(HarnessError, "bill blind"):
                run_fireworks_probe(
                    t, "or-key", "fw-key", gov, StubLedger(),
                    model=NEMO, max_cost=0.01, fireworks_enabled=True,
                    marker_path=str(Path(tmp) / "m"),
                    receipt_path=str(Path(tmp) / "r"))

    def test_max_cost_above_one_cent_is_refused(self):
        t = RecordingTransport(_ok_resp({"cost": 0.0}))
        with self.assertRaisesRegex(HarnessError, r"outside \(0,"):
            self._run(t, max_cost=0.05)
        self.assertFalse(os.path.exists(self.marker))
        self.assertEqual(t.calls, [])

    def test_zero_max_cost_is_refused(self):
        t = RecordingTransport(_ok_resp({"cost": 0.0}))
        with self.assertRaises(HarnessError):
            self._run(t, max_cost=0.0)
        self.assertEqual(t.calls, [])

    def test_disabled_fireworks_is_refused(self):
        t = RecordingTransport(_ok_resp({"cost": 0.0}))
        with self.assertRaisesRegex(HarnessError, "disabled"):
            self._run(t, fireworks_enabled=False)
        self.assertEqual(t.calls, [])

    def test_missing_key_is_refused_without_dispatch(self):
        t = RecordingTransport(_ok_resp({"cost": 0.0}))
        with self.assertRaisesRegex(HarnessError, "key missing"):
            run_fireworks_probe(t, "or-key", None, self._gov(t), self.ledger,
                                model=NEMO, max_cost=0.01,
                                fireworks_enabled=True,
                                marker_path=self.marker,
                                receipt_path=self.receipt)

    def test_request_is_the_eight_token_ok_prompt(self):
        t = RecordingTransport(_ok_resp({"cost": 0.0}))
        self._run(t)
        _url, _key, payload = t.calls[0]
        self.assertEqual(payload["max_tokens"], VERIFY_MAX_TOKENS)
        self.assertEqual(payload["messages"],
                         [{"role": "user", "content": "Reply with the single word: ok"}])


class ParserTests(unittest.TestCase):
    def test_economics_accepts_the_verify_flags(self):
        opts = build_parser().parse_args(
            ["economics", "--provider", "fireworks",
             "--confirm-single-paid-call", "--model", NEMO,
             "--max-cost", "0.01"])
        self.assertEqual(opts.provider, "fireworks")
        self.assertTrue(opts.confirm_single_paid_call)
        self.assertEqual(opts.model, NEMO)
        self.assertEqual(opts.max_cost, 0.01)

    def test_economics_defaults_to_the_openrouter_probe(self):
        opts = build_parser().parse_args(["economics", "--probe-model", "x/y"])
        self.assertEqual(opts.provider, "openrouter")
        self.assertFalse(opts.confirm_single_paid_call)


class NoNetworkTests(unittest.TestCase):
    def test_probe_and_refusals_open_no_socket(self):
        def refuse(*_a, **_k):
            raise AssertionError("EV-8 must not touch the network")

        with tempfile.TemporaryDirectory() as tmp:
            marker = str(Path(tmp) / "m.json")
            receipt = str(Path(tmp) / "r.json")
            t = RecordingTransport(_ok_resp({"cost": 0.0}))
            gov = SpendGovernor(t, "or-key", max_cost=1.0,
                                fireworks_budget_usd=1.0)
            with mock.patch.object(socket, "socket", side_effect=refuse), \
                    mock.patch.object(socket, "create_connection",
                                      side_effect=refuse), \
                    mock.patch.object(socket, "getaddrinfo", side_effect=refuse):
                run_fireworks_probe(t, "or-key", "fw-key", gov, StubLedger(),
                                    model=NEMO, max_cost=0.01,
                                    fireworks_enabled=True,
                                    marker_path=marker, receipt_path=receipt)


if __name__ == "__main__":
    unittest.main()
