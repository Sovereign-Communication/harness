"""The `harness economics` face: the EV-0a discount-truth probe and nothing else.

EV-0 ships one CLI face, and it exists for one reason: a probe nobody can run
is not shipped. The shortlist-pricing report that used to sit beside it
(`--models`, `--max-fetches`, `--receipt-dir`, `_split_ids`, and a receipt
writer) was unrequested scope and is gone with its canon rows (DF-EV-12), so
these tests cover what remains and pin that the removed flags are really gone.

Two things about what remains are load-bearing:

* the flags exist against the REAL parser (a renamed flag on a face nobody
  runs in CI is exactly how `rankings.yml` silently produced no artifact for
  a week -- see `tests/test_rankings_cli.py` for that incident);
* `--record` is the only write, and it writes repo evidence -- never a pool, a
  lane default, or a ceiling. EV-0 is evidence only; the ability to *apply*
  anything is EV-3/EV-4, and a face that quietly did it here would land a
  behaviour change nobody gated.
"""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from harness.cli_parser import build_parser
from harness.errors import HarnessError


class ParserSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.parser = build_parser()

    def test_probe_and_record_flags_parse(self):
        opts = self.parser.parse_args(["economics", "--probe-model", "acme/sol",
                                       "--record", "--verdict-path",
                                       "/tmp/e.json"])
        self.assertEqual(opts.probe_model, "acme/sol")
        self.assertTrue(opts.record)
        self.assertEqual(opts.verdict_path, "/tmp/e.json")

    def test_the_removed_report_flags_are_really_gone(self):
        """The report was unrequested surface (DF-EV-12). Its flags must not
        linger as no-ops someone keeps scripting against."""
        opts = self.parser.parse_args(["economics"])
        for flag in ("models", "max_fetches", "receipt_dir"):
            self.assertFalse(hasattr(opts, flag), f"--{flag} still parses")
        for flag in ("--models", "--max-fetches", "--receipt-dir"):
            with self.assertRaises(SystemExit):
                with redirect_stdout(io.StringIO()), \
                        redirect_stderr(io.StringIO()):
                    self.parser.parse_args(["economics", flag, "x"])

    def test_the_verdict_flag_names_repo_evidence_not_local_state(self):
        """The flag this replaced was `--state-path`, pointed at
        ~/.config/harness/economics.json, and that naming is part of what let
        the gate drift into machine-local state unnoticed."""
        buffer = io.StringIO()
        with redirect_stdout(buffer), self.assertRaises(SystemExit):
            build_parser().parse_args(["economics", "--help"])
        text = buffer.getvalue()
        self.assertIn("EV0A_DISCOUNT_SEMANTICS.json", text)
        self.assertIn("repo evidence", text)
        self.assertNotIn("~/.config", text)

    def test_command_is_dispatchable(self):
        from harness.cli import _DISPATCH
        self.assertIn("economics", _DISPATCH)


class ProbeFaceTests(unittest.TestCase):
    """`--record` persists only a conclusive verdict, as committed evidence."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self._tmp.name, "economics.json")
        self.addCleanup(self._tmp.cleanup)

    def _run(self, probe_record, argv=None, record_flag=True):
        parser = build_parser()
        argv = argv or ["economics", "--probe-model", "acme/sol"]
        if record_flag:
            argv = argv + ["--record", "--verdict-path", self.state]
        opts = parser.parse_args(argv)
        from harness.cli import _cmd_economics
        buffer = io.StringIO()
        gov = mock.Mock()
        with mock.patch("harness.cli._governor", return_value=("key", gov)), \
             mock.patch("harness.cli._run_discount_probe",
                        return_value=probe_record) as probe:
            with redirect_stdout(buffer):
                _cmd_economics(opts, mock.Mock())
        return json.loads(buffer.getvalue()), probe

    def test_conclusive_verdict_is_recorded(self):
        record = {"semantics": "listed_is_effective", "model": "acme/sol",
                  "fingerprint": {"model": "acme/sol", "max_discount": 0.5}}
        payload, _probe = self._run(record)
        self.assertEqual(payload["semantics"], "listed_is_effective")
        self.assertTrue(os.path.isfile(self.state))
        with open(self.state, encoding="utf-8") as stream:
            self.assertEqual(json.load(stream)["semantics"],
                             "listed_is_effective")

    def test_ambiguous_verdict_is_not_recorded(self):
        """A refusal to decide is not a decision; caching one would let a
        later run believe the price gate had been satisfied."""
        record = {"semantics": "ambiguous", "model": "acme/sol",
                  "reason": "provider prices alias"}
        payload, _probe = self._run(record)
        self.assertIsNone(payload["recorded"])
        self.assertEqual(payload["probe"]["semantics"], "ambiguous")
        self.assertFalse(os.path.exists(self.state))

    def test_unresolved_verdict_is_not_recorded(self):
        record = {"semantics": "unresolved", "model": "acme/sol",
                  "reason": "no provider-reported cost"}
        payload, _probe = self._run(record)
        self.assertIsNone(payload["recorded"])
        self.assertFalse(os.path.exists(self.state))

    def test_the_probe_without_record_writes_nothing(self):
        """EV-0 is evidence only: the probe itself must not touch the tree,
        let alone configuration."""
        from harness.cli import _cmd_economics
        record = {"semantics": "listed_is_effective", "model": "acme/sol",
                  "fingerprint": {"model": "acme/sol", "max_discount": 0.5}}
        settings = mock.Mock()
        gov = mock.Mock()
        with mock.patch("harness.cli._governor", return_value=("key", gov)), \
             mock.patch("harness.cli._run_discount_probe",
                        return_value=record), \
             mock.patch("harness.cli.freeze_jev_settings") as write_config:
            with redirect_stdout(io.StringIO()):
                _cmd_economics(build_parser().parse_args(
                    ["economics", "--probe-model", "acme/sol"]), settings)
        self.assertFalse(os.path.exists(self.state))
        self.assertFalse(write_config.called)
        self.assertFalse(settings.write.called if hasattr(settings, "write")
                         else False)

    def test_a_bare_invocation_refuses_and_says_why(self):
        """There is no report to produce any more, so the face says what it
        does instead of silently emitting an empty artifact."""
        from harness.cli import _cmd_economics
        opts = build_parser().parse_args(["economics"])
        with mock.patch("harness.cli._governor") as governor:
            with self.assertRaises(HarnessError) as ctx:
                _cmd_economics(opts, mock.Mock())
        message = str(ctx.exception)
        self.assertIn("--probe-model", message)
        self.assertIn("EV-1", message)
        # ...and it refuses BEFORE any key lookup or spend preflight.
        self.assertFalse(governor.called)


class EconomicsReportAndExportTests(unittest.TestCase):
    """EV-5 CLI and site-export --economics surfaces."""

    def test_check_fresh_emits_tier_snapshot_freshness(self):
        from harness.cli import _cmd_economics
        opts = build_parser().parse_args(["economics", "--check-fresh", "--json"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            _cmd_economics(opts, mock.Mock())
        payload = json.loads(buf.getvalue())
        self.assertTrue(payload["ok"])
        self.assertIn("age_days", payload)

    def test_report_and_propose_emit_full_economics_payload(self):
        from harness.cli import _cmd_economics
        from harness.ledger import AutonomyLedger
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = os.path.join(tmp, "ledger.jsonl")
            ledger = AutonomyLedger(ledger_path)
            ledger.append("model_result", task_id="t1", model="deepseek/deepseek-v4.1-flash",
                          lane="apply", cost=0.0005, input_tokens=1000, output_tokens=200)
            settings = mock.Mock()
            settings.apply_pool = ["deepseek/deepseek-v4.1-flash", "openai/gpt-4o-mini"]
            opts = build_parser().parse_args(["economics", "--report", "--propose", "--json"])
            buf = io.StringIO()
            with mock.patch("harness.cli._ledger", return_value=ledger), \
                 redirect_stdout(buf):
                _cmd_economics(opts, settings)
            payload = json.loads(buf.getvalue())
            self.assertEqual(payload["status"], "ok")
            self.assertIn("freshness", payload)
            self.assertIn("tiers", payload)
            self.assertIn("seat_share", payload)
            self.assertIn("rotation_proposal", payload)
            self.assertIn("cost_summary", payload)
            self.assertIn("by_lane", payload["cost_summary"])
            self.assertFalse(payload["applied"])

    def test_site_export_with_economics_attaches_sanitized_summary(self):
        from harness.ledger import AutonomyLedger
        from harness.site_export import CONSENT_SCHEMA, export_bundle
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = os.path.join(tmp, "ledger.jsonl")
            consent_path = os.path.join(tmp, "consent.json")
            ledger = AutonomyLedger(ledger_path)
            ledger.append("dispatch_start", task_id="t1", model="deepseek/deepseek-v4.1-flash")
            ledger.append("model_result", task_id="t1", model="deepseek/deepseek-v4.1-flash",
                          lane="apply", cost=0.0005, input_tokens=1000, output_tokens=200)
            ledger.append("complete", task_id="t1", model="deepseek/deepseek-v4.1-flash",
                          status="ok", rounds=1)
            with open(consent_path, "w", encoding="utf-8") as f:
                json.dump({
                    "schema": CONSENT_SCHEMA,
                    "accepted_at": "2026-10-18T00:00:00Z",
                    "surface": "cli",
                }, f)
            bundle = export_bundle(
                ledger_path, consent_path,
                harness_version="0.4.2",
                include_economics=True,
            )
            self.assertIn("economics", bundle)
            econ = bundle["economics"]
            self.assertIn("tiers_freshness", econ)
            self.assertIn("tier_counts", econ)
            self.assertIn("cost_summary", econ)
            self.assertIn("by_lane", econ["cost_summary"])


class EconomicsSurfaceBranchCoverageTests(unittest.TestCase):
    """Comprehensive branch coverage for EV-1..EV-8 surfaces (CLI, MCP, HTTP, config, spend, routing)."""

    def test_cli_fireworks_provider_table_and_verify_branches(self):
        from harness.cli import _cmd_economics, _cmd_fireworks_verify
        opts = build_parser().parse_args(["economics", "--provider", "fireworks"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            _cmd_economics(opts, mock.Mock())
        self.assertIn("Fireworks", buf.getvalue())

        # _cmd_fireworks_verify error branches and happy path
        opts_no_confirm = build_parser().parse_args(
            ["economics", "--provider", "fireworks", "--model", "deepseek-v3p2"]
        )
        with self.assertRaises(HarnessError):
            _cmd_economics(opts_no_confirm, mock.Mock())

        opts_no_model = mock.Mock(confirm_single_paid_call=True, model=None, max_cost=0.01)
        with self.assertRaises(HarnessError):
            _cmd_fireworks_verify(opts_no_model, mock.Mock())

        opts_no_cost = mock.Mock(confirm_single_paid_call=True, model="deepseek-v3p2", max_cost=None)
        with self.assertRaises(HarnessError):
            _cmd_fireworks_verify(opts_no_cost, mock.Mock())

        opts_ok = build_parser().parse_args([
            "economics", "--provider", "fireworks",
            "--confirm-single-paid-call", "--model", "deepseek-v3p2",
            "--max-cost", "0.01", "--json",
        ])
        gov = mock.Mock()
        out_buf = io.StringIO()
        with mock.patch("harness.cli._governor", return_value=("fw-key", gov)), \
             mock.patch("harness.cli._ledger", return_value=mock.Mock()), \
             mock.patch("harness.cli.run_fireworks_probe", return_value={"status": "ok"}) as probe_fn, \
             redirect_stdout(out_buf):
            _cmd_economics(opts_ok, mock.Mock())
        self.assertTrue(probe_fn.called)
        self.assertEqual(json.loads(out_buf.getvalue())["status"], "ok")

    def test_cli_check_fresh_stale_and_apply_pool_rotation(self):
        from harness.cli import _cmd_economics
        from harness.ledger import AutonomyLedger
        opts_stale = build_parser().parse_args(["economics", "--check-fresh", "--json"])
        with mock.patch("harness.cli.check_tier_snapshot_freshness",
                        return_value={"ok": False, "age_days": 99}), \
             redirect_stdout(io.StringIO()), \
             self.assertRaises(SystemExit) as ctx:
            _cmd_economics(opts_stale, mock.Mock())
        self.assertEqual(ctx.exception.code, 2)

        with tempfile.TemporaryDirectory() as tmp:
            ledger = AutonomyLedger(os.path.join(tmp, "ledger.jsonl"))
            settings = mock.Mock(apply_pool=["m1", "m2"])
            opts_apply = build_parser().parse_args(["economics", "--apply", "--json"])
            buf = io.StringIO()
            with mock.patch("harness.cli._ledger", return_value=ledger), \
                 mock.patch("harness.cli.evaluate_pool_rotation",
                            return_value={"changed": True, "proposed_pool": ["m2", "m1"]}), \
                 mock.patch("harness.config.update_config") as upd, \
                 redirect_stdout(buf):
                _cmd_economics(opts_apply, settings)
            self.assertTrue(upd.called)
            self.assertTrue(json.loads(buf.getvalue())["applied"])

    def test_cli_report_print_cost_table_by_lane(self):
        from harness.cli_report import _print_cost_table
        err = io.StringIO()
        with redirect_stderr(err):
            _print_cost_table({
                "total_cost": 0.01,
                "events_count": 2,
                "billable_calls": 1,
                "free_calls": 1,
                "by_lane": {
                    "apply": {"calls": 2, "cost": 0.01, "input_tokens": 500, "output_tokens": 100},
                },
            })
        self.assertIn("Spend by Lane:", err.getvalue())

    def test_mcp_and_server_economics_and_by_lane_cost(self):
        from harness.ledger import AutonomyLedger
        from harness.mcp import McpServer
        from harness.server import UiRequestHandler
        with tempfile.TemporaryDirectory() as tmp:
            ledger = AutonomyLedger(os.path.join(tmp, "ledger.jsonl"))
            ledger.append("verify_round", task_id="t1", model="deepseek/deepseek-v4.1-flash",
                          passed=True, cost=0.001, input_tokens=100, output_tokens=50)
            ledger.append("offer", task_id="t1", model="deepseek/deepseek-v4.1-flash", cost=0.0001)
            ledger.append("model_result", task_id="t1", model="deepseek/deepseek-v4.1-flash",
                          event_note="probe", cost=0.0001)
            ledger.append("model_result", task_id="t1", model="deepseek/deepseek-v4.1-flash",
                          task_type="structured", cost=0.0001)
            router = mock.Mock(apply_pool=["deepseek/deepseek-v4.1-flash", "openai/gpt-4o-mini"])
            srv = McpServer(
                transport=mock.Mock(),
                api_key="test-key",
                governor=mock.Mock(),
                ledger=ledger,
                router=router,
                engine=mock.Mock(),
                settings=mock.Mock(apply_pool=["deepseek/deepseek-v4.1-flash"]),
            )
            res = srv._invoke("economics_report", {"allow_cost_increase": False})
            self.assertEqual(res["status"], "ok")
            self.assertIn("rotation_proposal", res)

            # Server _api_economics and _api_cost?by_lane=1
            sent = []
            handler = mock.Mock(spec=UiRequestHandler)
            handler._send_json = lambda obj, code=200, close=False: sent.append((code, obj))
            with mock.patch("harness.server.load_settings",
                            return_value=mock.Mock(apply_pool=["deepseek/deepseek-v4.1-flash"])), \
                 mock.patch("harness.server.ledger_for", return_value=ledger):
                UiRequestHandler._api_cost(handler, {"by_lane": ["1"], "last": ["24h"]})
                UiRequestHandler._api_economics(handler)
            self.assertEqual(len(sent), 2)
            self.assertIn("by_lane", sent[0][1])
            self.assertEqual(sent[1][1]["status"], "ok")

    def test_config_session_spend_and_routing_branches(self):
        from harness.capability import (
            cost_per_capability,
            evaluate_pool_rotation,
            rotate_pool_by_streak,
            seat_share_report,
        )
        from harness.config import load_settings
        from harness.endpoint_pricing import effective_price
        from harness.routing_table import (
            check_tier_snapshot_freshness,
            classify_model_tier,
            get_tier_route,
        )
        from harness.session import governor_for
        from harness.spend import SpendGovernor

        with mock.patch.dict(os.environ, {
            "HARNESS_USE_FIREWORKS": "true",
            "FIREWORKS_API_KEY": "fw-secret",
            "HARNESS_TRIAL_MODEL": "accounts/fireworks/models/deepseek-v3p2",
        }, clear=False):
            s = load_settings()
            self.assertTrue(s.use_fireworks)
            self.assertEqual(s.apply_model, "accounts/fireworks/models/deepseek-v3p2")

        with mock.patch.dict(os.environ, {
            "HARNESS_USE_FIREWORKS": "true",
            "FIREWORKS_API_KEY": "",
        }, clear=False), mock.patch("harness.config.resolve_fireworks_key", return_value=None):
            with self.assertRaises(HarnessError):
                load_settings()

        # session.governor_for with fireworks provider
        fw_settings = mock.Mock(
            use_fireworks=True, fireworks_key="", max_cost=0.05,
            expect_key_label=None, fireworks_budget_usd=0.05,
        )
        with mock.patch("harness.session.resolve_fireworks_key", return_value=None):
            with self.assertRaises(HarnessError):
                governor_for(fw_settings)

        fw_settings.fireworks_key = "fw-secret"
        key, gov = governor_for(fw_settings)
        self.assertEqual(key, "fw-secret")
        key2, gov2 = governor_for(fw_settings, max_cost_override=0.02)
        self.assertEqual(gov2.max_cost, 0.02)
        self.assertTrue(gov2.supports_reasoning("accounts/fireworks/models/deepseek-v3p2"))

        # spend.py fetch_pricing for fireworks models
        gov_fw = SpendGovernor(mock.Mock(), "fw-key", None, max_cost=0.05)
        gov_fw.fetch_pricing(["accounts/fireworks/models/nemotron-lightning-3p5-30b-a3b"])
        self.assertIn("accounts/fireworks/models/nemotron-lightning-3p5-30b-a3b", gov_fw._pricing_cache)
        with self.assertRaises(HarnessError):
            gov_fw.fetch_pricing(["accounts/fireworks/models/nonexistent-xyz"])

        # endpoint_pricing effective_price validation
        with self.assertRaises(HarnessError):
            effective_price(None, 100, 100)
        with self.assertRaises(HarnessError):
            effective_price(mock.Mock(endpoints=[mock.Mock()]), -1, 100)

        # routing_table branches
        r = get_tier_route("T1", trial_model="custom/trial-model:free")
        self.assertEqual(r["models"][0], "custom/trial-model:free")
        with mock.patch.dict("harness.routing_table.SNAPSHOT_PRICE_TABLE", {"unknown/m": {"tier": "T2"}}):
            self.assertEqual(classify_model_tier("unknown/m"), "T2")
        self.assertEqual(classify_model_tier("unknown/free", profile=mock.Mock(free=True)), "T0")
        self.assertEqual(
            classify_model_tier("unknown/t1", profile=mock.Mock(free=False, completion_price=0.5e-6)),
            "T1",
        )
        self.assertEqual(
            classify_model_tier("unknown/t2", profile=mock.Mock(free=False, completion_price=2.0e-6)),
            "T2",
        )
        self.assertEqual(
            classify_model_tier("unknown/t3", profile=mock.Mock(free=False, completion_price=9.0e-6)),
            "T3",
        )
        from datetime import datetime
        with mock.patch("harness.routing_table.load_tier_snapshot", return_value={"captured_at": ""}):
            self.assertFalse(check_tier_snapshot_freshness()["ok"])
        with mock.patch("harness.routing_table.load_tier_snapshot", return_value={"captured_at": "bad-iso"}):
            self.assertFalse(check_tier_snapshot_freshness()["ok"])
        with mock.patch("harness.routing_table.load_tier_snapshot",
                        return_value={"captured_at": "2026-10-01T00:00:00"}):
            fresh = check_tier_snapshot_freshness(now=datetime(2026, 10, 2, 0, 0))
            self.assertTrue(fresh["ok"])

        # capability branches
        econ_j = cost_per_capability(None, lane="judge", benchmark_rows=[{"score": 50.0}])
        econ_p = cost_per_capability(None, lane="panel", benchmark_rows=[{"score": 60.0}])
        self.assertEqual(econ_j["effective_usd"], 0.0)
        self.assertEqual(econ_p["effective_usd"], 0.0)
        rot = evaluate_pool_rotation(
            current_pool=["m1", "m2"],
            candidate_metrics=[
                {"id": "m1", "effective_capability": 0.5, "cost_usd": 0.01, "samples": 2},
                {"id": "m2", "effective_capability": 0.8, "cost_usd": 0.005, "samples": 10},
            ],
            ledger_entries=[],
        )
        self.assertIn("changed", rot)
        rebal0 = rotate_pool_by_streak(["m1", "m2"], {"m1": 0.0, "m2": 0.5}, head_streak=4)
        self.assertEqual(rebal0[0], "m2")
        rebal_none = rotate_pool_by_streak(["m1", "m2"], {"m1": 1.0, "m2": 0.1}, head_streak=4)
        self.assertEqual(rebal_none[0], "m1")
        mock_ledger = mock.Mock()
        mock_ledger.entries.return_value = [{"event": "other"}, "not-a-dict"]
        ss = seat_share_report(mock_ledger)
        self.assertIsInstance(ss, dict)


