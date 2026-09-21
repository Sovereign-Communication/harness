"""SITE-9: hermetic coverage for the site-export CLI face and the remaining
server/policy branches the battery had not executed.

CLI: the fail-closed `--yes` gate (refusal is a clean exit, never a partial
write) and the happy path through the real exporter + writer.
Policy: `_route_query_text` accepts raw strings, dicts, and non-text state.
Server: the demo-snapshot degrade-on-read-error path and the broken-chain
snapshot refusal.
"""
import json
import os
import tempfile
import unittest
from unittest import mock


from tests.test_site_export import (_populate_task_ledger,
                                    _write_consent, _write_pricing)
from tests.test_server import ServerHarness, _request


class SiteExportCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger_path = os.path.join(self.tmp.name, "ledger.jsonl")
        from harness.ledger import AutonomyLedger
        ledger = AutonomyLedger(self.ledger_path)
        _populate_task_ledger(ledger)
        self.consent_path = _write_consent(self.tmp.name)
        self.out_path = os.path.join(self.tmp.name, "bundle.json")

    def _settings(self):
        from harness.config import load_settings
        settings = load_settings()
        settings.jev_api_key = None
        return settings

    def test_site_export_requires_yes(self):
        from harness import cli
        with mock.patch("harness.cli.load_settings",
                        return_value=self._settings()):
            with self.assertRaises(SystemExit) as ctx:
                cli.main([
                    "site-export",
                    "--ledger", self.ledger_path,
                    "--consent", self.consent_path,
                    "--out", self.out_path,
                ])
        self.assertEqual(ctx.exception.code, 1)
        self.assertFalse(os.path.exists(self.out_path),
                         "a refusal must never leave a partial bundle")

    def test_site_export_writes_bundle(self):
        from harness import cli
        with mock.patch("harness.cli.load_settings",
                        return_value=self._settings()):
            cli.main([
                "site-export",
                "--ledger", self.ledger_path,
                "--consent", self.consent_path,
                "--out", self.out_path,
                "--yes",
            ])
        self.assertTrue(os.path.exists(self.out_path))
        with open(self.out_path, encoding="utf-8") as handle:
            bundle = json.load(handle)
        self.assertEqual(bundle["schema"], "site-bundle-v1")
        self.assertEqual(bundle["totals"]["runs"], 1)

    def test_site_export_with_pricing_snapshot(self):
        from harness import cli
        pricing_path = _write_pricing(self.tmp.name)
        with mock.patch("harness.cli.load_settings",
                        return_value=self._settings()):
            cli.main([
                "site-export",
                "--ledger", self.ledger_path,
                "--consent", self.consent_path,
                "--pricing", pricing_path,
                "--out", self.out_path,
                "--yes",
            ])
        with open(self.out_path, encoding="utf-8") as handle:
            bundle = json.load(handle)
        self.assertIn("pricing_snapshot", bundle)


class RouteQueryTextTests(unittest.TestCase):
    def _extract(self, state):
        from harness.jev_policy import JevPolicy
        return JevPolicy._route_query_text(state)

    def test_string_state_passes_through(self):
        self.assertEqual(self._extract("fix the typo"), "fix the typo")

    def test_dict_state_reads_goal_then_other_keys(self):
        self.assertEqual(self._extract({"goal": "g"}), "g")
        self.assertEqual(self._extract({"query": "q"}), "q")
        self.assertEqual(self._extract({"issue": "i"}), "i")
        self.assertEqual(self._extract({"prompt": "p"}), "p")

    def test_dict_without_text_is_empty(self):
        self.assertEqual(self._extract({"other": 1}), "")

    def test_none_is_empty_and_other_types_are_stringified(self):
        self.assertEqual(self._extract(None), "")
        self.assertEqual(self._extract(42), "42")


class RouteFallbackRewrapTests(unittest.TestCase):
    """The keyed-but-failed path: a live evaluator answer that falls back or
    chooses out-of-ladder is rewrapped with the real cost/usage attached."""

    def _policy_with_evaluator(self, result):
        from harness.jev_policy import JevPolicy
        policy = JevPolicy.__new__(JevPolicy)
        # keyed is a read-only property over evaluator.api_key
        policy.evaluator = mock.MagicMock()
        policy.evaluator.api_key = "test-key"
        policy.evaluator.model = "jev-test"
        policy.evaluator.evaluate.return_value = result
        policy.governor = mock.MagicMock()
        policy._preflight = mock.MagicMock(return_value=None)
        policy._account = mock.MagicMock(return_value={"site": "model_route"})
        policy.ledger = None
        return policy

    def _result(self, **kwargs):
        from harness.jev import JevEvaluationResult
        base = dict(verdict="fail", confidence=0.0, supported=0.0,
                    answers={}, reasons=["transport fail"],
                    cost=0.002, input_tokens=150, output_tokens=20,
                    is_fallback=True, model="jev-test")
        base.update(kwargs)
        return JevEvaluationResult(**base)

    def test_transport_fail_rewraps_with_cost(self):
        from harness.route_pack import validate_route_pack
        policy = self._policy_with_evaluator(self._result())
        result, structural, combo = policy.evaluate_model_route(
            {"goal": "fix a typo"}, validate_route_pack({
                "id": "p", "rungs": [
                    {"rung_id": "scout", "tier": "T0", "model": "m0",
                     "cost_class": "free", "guidance": ["typo"]},
                    {"rung_id": "worker", "tier": "T1", "model": "m1",
                     "cost_class": "cheap", "guidance": ["parse"]}],
            }))
        self.assertTrue(combo["is_fallback"])
        self.assertGreater(float(result.cost), 0.0,
                           "the failed live attempt's real cost must survive")
        self.assertGreater(int(result.input_tokens), 0)

    def test_out_of_ladder_choice_is_refused_not_invented(self):
        from harness.route_pack import validate_route_pack
        policy = self._policy_with_evaluator(self._result(
            verdict="pass", confidence=0.9, supported=1.0,
            answers={"rung": {"choice": "never-declared"}},
            is_fallback=False, reasons=["picked something"]))
        _result2, _structural, combo = policy.evaluate_model_route(
            {"goal": "fix a typo"}, validate_route_pack({
                "id": "p", "rungs": [
                    {"rung_id": "scout", "tier": "T0", "model": "m0",
                     "cost_class": "free", "guidance": ["typo"]}],
            }))
        self.assertTrue(combo["is_fallback"])
        self.assertTrue(any("out-of-ladder" in r for r in combo["reasons"]),
                        combo["reasons"])
        self.assertNotEqual(combo.get("rung_id"), "never-declared")


class SiteServerEdgeTests(ServerHarness):
    def test_demo_snapshot_degrades_on_read_error(self, *_):
        import harness.server as server_mod
        conn = self._conn()
        try:
            with mock.patch.object(server_mod, "open",
                                   side_effect=OSError("disk gone")):
                status, data = _request(conn, "GET",
                                        "/api/site/demo-snapshot")
            self.assertEqual(status, 200)
            self.assertEqual(data.get("contributors"), 0)
            self.assertIn("error", data)
        finally:
            conn.close()

    def test_snapshot_refuses_broken_chain(self):
        """A poisoned local ledger must fail the snapshot closed, with the
        exact refusal message, not a degraded or fabricated view."""
        import harness.server as server_mod
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ledger_path = os.path.join(tmp.name, "ledger.jsonl")
        from harness.ledger import AutonomyLedger
        ledger = AutonomyLedger(ledger_path)
        ledger.append("dispatch_start", task_id="t1", model="m")
        with open(ledger_path, "rb") as f:
            raw = f.read()
        with open(ledger_path, "ab") as f:
            f.write(raw.replace(b'"t1"', b'"t2"', 1))
        conn = self._conn()
        try:
            with mock.patch.object(server_mod, "ledger_for",
                                   return_value=AutonomyLedger(ledger_path)):
                with mock.patch.object(
                        server_mod, "load_settings",
                        return_value=self._fake_settings(ledger_path)):
                    status, data = _request(conn, "GET", "/api/snapshot")
            self.assertEqual(status, 400)
            self.assertIn("hash chain failed", data.get("error", ""))
        finally:
            conn.close()

    @staticmethod
    def _fake_settings(ledger_path):
        from harness.config import load_settings
        settings = load_settings()
        settings.jev_api_key = None
        settings.ledger_path = ledger_path
        return settings


if __name__ == "__main__":
    unittest.main()
