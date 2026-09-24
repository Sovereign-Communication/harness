import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from harness import cli, service, session
from harness.config import load_settings
from harness.errors import HarnessError
from harness.router import Router


class PanelWiringTests(unittest.TestCase):
    def test_verify_panel_flag_reaches_run_verify(self):
        """DF-CLI-2: ``harness verify --panel`` was declared in cli_parser.py
        but cli.py never read opts.panel -- the flag was a no-op. The
        comma-separated ids must reach service.panel_judge as a list, in
        order, through the ONE owner (service.run_verify)."""
        captured = {}

        def fake_governor(settings, max_cost_override=None):
            gov = mock.Mock()
            gov.verify_key.return_value = None
            gov.max_cost = max_cost_override or settings.max_cost
            gov.spent = 0.0
            gov.preflight.return_value = (0.0, [])
            gov.check_byok.return_value = None
            gov.learned_blocked.return_value = False
            gov.record_actual.return_value = None
            gov.cost_by_model.return_value = {}
            gov.is_free.return_value = True
            return "key", gov

        def fake_panel_judge(**kwargs):
            captured["panel"] = kwargs.get("panel")
            return {}

        with mock.patch.object(service, "governor_for",
                               side_effect=fake_governor), \
             mock.patch.object(service, "panel_judge",
                               side_effect=fake_panel_judge), \
             mock.patch.object(service, "HttpTransport"):
            cli.main(["verify", "--prompt", "hi",
                     "--panel", "model/one, model/two ,model/three"])
        self.assertEqual(captured["panel"],
                         ["model/one", "model/two", "model/three"])

    def test_verify_without_panel_flag_uses_configured_default(self):
        """No --panel given: run_verify falls back to the configured/router
        default pool (None reaches ResolvedVerifyInputs, not an empty list
        that would starve the panel)."""
        captured = {}

        def fake_governor(settings, max_cost_override=None):
            gov = mock.Mock()
            gov.verify_key.return_value = None
            gov.max_cost = max_cost_override or settings.max_cost
            gov.spent = 0.0
            gov.preflight.return_value = (0.0, [])
            gov.check_byok.return_value = None
            gov.learned_blocked.return_value = False
            gov.record_actual.return_value = None
            gov.cost_by_model.return_value = {}
            gov.is_free.return_value = True
            return "key", gov

        def fake_panel_judge(**kwargs):
            captured["panel"] = kwargs.get("panel")
            return {}

        with mock.patch.object(service, "governor_for",
                               side_effect=fake_governor), \
             mock.patch.object(service, "panel_judge",
                               side_effect=fake_panel_judge), \
             mock.patch.object(service, "HttpTransport"):
            cli.main(["verify", "--prompt", "hi"])
        self.assertNotEqual(captured["panel"], [])


class PlanAllowHeuristicPreviewWiringTests(unittest.TestCase):
    """DF-HG-3b: --allow-heuristic-preview must reach compose_plan's
    allow_heuristic_preview kwarg through the ONE owner (_plan_compose),
    and default to False when the flag is absent."""

    def _settings(self):
        from types import SimpleNamespace
        # Plain namespace (not Mock): hasattr(settings, "ledger_path") /
        # hasattr(settings, "jev_api_key") must be False here, exactly as a
        # real plan-only, non-ledgered settings object would be -- a Mock
        # auto-creates every attribute and would wrongly take the ledger/
        # jev_policy branches in _plan_compose.
        return SimpleNamespace(use_free=True, allow_escalation=False)

    def test_flag_true_reaches_compose_plan(self):
        from types import SimpleNamespace
        captured = {}

        def fake_compose_plan(**kwargs):
            captured.update(kwargs)
            return {}

        opts = SimpleNamespace(goal="g", allow_escalation=None,
                              max_tokens=None, plan_consensus=False)
        with mock.patch.object(cli, "_compose_plan",
                               side_effect=fake_compose_plan):
            cli._plan_compose(
                self._settings(), opts, None, None, None,
                candidate_files=None, frontier_model=None, execute=False,
                confirm=False, decompose_llm=False, plan_consensus=False,
                hourglass={"confirm": False, "decompose": False},
                allow_heuristic_preview=True)
        self.assertTrue(captured["allow_heuristic_preview"])

    def test_flag_defaults_false_when_omitted(self):
        from types import SimpleNamespace
        captured = {}

        def fake_compose_plan(**kwargs):
            captured.update(kwargs)
            return {}

        opts = SimpleNamespace(goal="g", allow_escalation=None,
                              max_tokens=None, plan_consensus=False)
        with mock.patch.object(cli, "_compose_plan",
                               side_effect=fake_compose_plan):
            cli._plan_compose(
                self._settings(), opts, None, None, None,
                candidate_files=None, frontier_model=None, execute=False,
                confirm=False, decompose_llm=False, plan_consensus=False,
                hourglass={"confirm": False, "decompose": False})
        self.assertFalse(captured["allow_heuristic_preview"])

    def test_cmd_plan_reads_opts_flag_into_plan_compose(self):
        """The CLI entry (_cmd_plan) must read opts.allow_heuristic_preview
        (set by --allow-heuristic-preview) and forward it into
        _plan_compose, not just _plan_compose's own default."""
        from types import SimpleNamespace
        captured = {}

        def fake_plan_compose(settings, opts, gov, transport, api_key, **kwargs):
            captured.update(kwargs)
            return {"status": "planned", "decomposition": "heuristic",
                   "nodes": []}

        opts = SimpleNamespace(
            goal="g", file=None, frontier_model=None, execute=False,
            plan_consensus=False, resume=None, task_max_cost=None,
            max_cost=None, allow_heuristic_preview=True,
            decompose_llm=None, confirm=None, out=None,
            allow_escalation=None, max_tokens=None)
        settings = SimpleNamespace(
            use_free=True, allow_escalation=False,
            hourglass_confirm=False, hourglass_parallel=False)
        with mock.patch.object(cli, "_plan_compose",
                               side_effect=fake_plan_compose), \
             mock.patch.object(cli, "_emit_by_status"):
            cli._cmd_plan(opts, settings)
        self.assertTrue(captured["allow_heuristic_preview"])


class MaxCostWiringTests(unittest.TestCase):
    def test_verify_max_cost_reaches_governor(self):
        """Regression: verify --max-cost was parsed but never wired, so the
        configured default ceiling silently applied instead."""
        captured = {}

        def fake_governor(settings, max_cost_override=None):
            captured["override"] = max_cost_override
            gov = mock.Mock()
            gov.verify_key.return_value = None
            gov.max_cost = max_cost_override or settings.max_cost
            gov.spent = 0.0
            gov.preflight.return_value = (0.0, [])
            gov.check_byok.return_value = None
            gov.learned_blocked.return_value = False
            gov.record_actual.return_value = None
            gov.cost_by_model.return_value = {}
            gov.is_free.return_value = True
            return "key", gov

        with mock.patch.object(service, "governor_for",
                               side_effect=fake_governor), \
             mock.patch.object(service, "panel_judge", return_value={}), \
             mock.patch.object(service, "HttpTransport"):
            cli.main(["verify", "--prompt", "hi", "--max-cost", "0.005"])
        self.assertEqual(captured["override"], 0.005)

    def test_verify_default_ceiling_when_flag_absent(self):
        captured = {}

        def fake_governor(settings, max_cost_override=None):
            captured["override"] = max_cost_override
            gov = mock.Mock()
            gov.verify_key.return_value = None
            gov.preflight.return_value = (0.0, [])
            gov.learned_blocked.return_value = False
            gov.is_free.return_value = True
            gov.cost_by_model.return_value = {}
            return "key", gov

        with mock.patch.object(service, "governor_for",
                               side_effect=fake_governor), \
             mock.patch.object(service, "panel_judge", return_value={}), \
             mock.patch.object(service, "HttpTransport"):
            cli.main(["verify", "--prompt", "hi"])
        self.assertIsNone(captured["override"])

    def test_max_cost_help_documents_preflight_reserve_sizing(self):
        """DF-MS-2b: verify CLI documentation describes the preflight reserve
        as sized to the retry plan actually dispatched, not a fixed dollar
        minimum (DF-MS-2's ~$0.036 figure was the over-reservation bug)."""
        parser = cli.build_parser()
        verify_sub = parser._subparsers._actions[1].choices.get("verify")
        self.assertIsNotNone(verify_sub)
        help_text = ""
        for action in verify_sub._actions:
            if "--max-cost" in action.option_strings:
                help_text = action.help or ""
                break
        self.assertNotIn("0.036", help_text)
        self.assertIn("max_panelists", help_text)
        self.assertIn("worst case", help_text)



class FriendlyInputErrorTests(unittest.TestCase):
    def test_read_json_missing(self):
        import os
        import tempfile
        missing = os.path.join(tempfile.gettempdir(), "harness-playtest-missing.json")
        if os.path.exists(missing):
            os.unlink(missing)
        with self.assertRaises(HarnessError) as cm:
            cli._read_json(missing, "--state continuation")
        self.assertIn("not readable", str(cm.exception))

    def test_read_json_invalid_content(self):
        import os
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write("{not json")
            path = f.name
        try:
            with self.assertRaises(HarnessError) as cm:
                cli._read_json(path, "--state continuation")
            self.assertIn("not valid JSON", str(cm.exception))
        finally:
            os.unlink(path)

    def test_read_text_missing(self):
        import os
        import tempfile
        missing = os.path.join(tempfile.gettempdir(), "harness-playtest-missing.txt")
        if os.path.exists(missing):
            os.unlink(missing)
        with self.assertRaises(HarnessError) as cm:
            cli._read_text(missing, "--source-file")
        self.assertIn("--source-file not readable", str(cm.exception))


class EngineKeyWiringTests(unittest.TestCase):
    """Dogfooding round 1 (audits/self): an ApplyEngine construction site
    dropped api_key, so EVERY engine-routed chat call went out with no
    Authorization header and 401'd -- while panel-lane calls, which receive
    the key directly, worked. Hermetic tests build engines directly, so the
    suite never noticed. Pin the wiring at the one owner (session.engine_for):
    the engine must carry the same key the governor verified."""

    def test_engine_receives_the_verified_api_key(self):
        settings = load_settings()
        engine = session.engine_for(settings, "sk-the-real-key", object(),
                                    object(), Router(["a"], "j", "m"))
        self.assertEqual(engine.api_key, "sk-the-real-key")

    def test_bench_engine_key_reaches_apply_loop(self):
        """End-to-end hermetic proof through cli.main: a bench run must send
        Authorization headers (the dogfood failure showed 401 'Missing
        Authentication header' on every engine call)."""
        # load_manifest raises for the missing manifest before key setup;
        # instead, drive the parser through main with a stub manifest dir.
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "b1"))
            with open(os.path.join(d, "b1", "a.py"), "w", encoding="utf-8") as f:
                f.write("print(1)\n")
            task = {"name": "b1", "file": "b1/a.py", "instruction": "x",
                    "verify": "python -c ''"}
            with open(os.path.join(d, "task.json"), "w", encoding="utf-8") as f:
                json.dump(task, f)
            captured = {}

            def fake_governor(settings, max_cost_override=None):
                gov = mock.Mock()
                gov.verify_key.return_value = None
                gov.spent = 0.0
                gov.preflight.return_value = (0.0, [])
                gov.fetch_models.return_value = [
                    {"id": "google/gemma-4-31b-it:free",
                     "pricing": {"prompt": "0", "completion": "0"}}]
                gov.fetch_pricing.side_effect = (
                    lambda ids: {mid: (0.0, 0.0) for mid in ids})
                gov.record_actual.return_value = None
                gov.is_free.return_value = True
                gov.learned_blocked.return_value = False
                gov.check_byok.return_value = None
                gov.attach_mock(mock.Mock(), "assert_no_tools")
                gov.record_byok.return_value = None

                def record_actual(amount, model_id):
                    captured.setdefault("posts", [])
                gov.record_actual.side_effect = record_actual
                return "sk-the-real-key", gov

            posts = []

            class TransportStub:
                def get(self, url, api_key, timeout=15):
                    captured["get_key"] = api_key
                    if url.endswith("/models"):
                        return {"data": [
                            {"id": "google/gemma-4-31b-it:free",
                             "pricing": {"prompt": "0", "completion": "0"}},
                        ]}
                    raise AssertionError(f"unexpected GET {url}")

                def post(self, url, api_key, payload, timeout=45):
                    posts.append(api_key)
                    return 401, {"error": {"message": "nope"}}

            from harness.ledger import AutonomyLedger
            # Bench composes through session.apply_session; patch the
            # governor seam at its owner (session), so this proves the
            # session-composed engine carries the verified key end to end.
            # The ledger is temp-isolated too: trust scores read history,
            # so the machine's real ledger must never decide a hermetic run.
            led = AutonomyLedger(os.path.join(d, "ledger.jsonl"))
            # JEV-P2-dead-code hermetic isolation: the engine now carries a
            # Jev policy (Jev-directed escalation). On an operator machine
            # load_settings() resolves a live System One key and a Jev noul
            # call would ride the session transport with its own credential;
            # the key-wiring assertion below is about ENGINE chat calls, so
            # the Jev key is disarmed at the session composition seam per
            # the documented doctrine ("never inherit live jev keys").
            real_policy_for = session.policy_for

            def _hermetic_policy_for(settings, **kwargs):
                settings.jev_api_key = None
                return real_policy_for(settings, **kwargs)

            with mock.patch.object(session, "governor_for", side_effect=fake_governor), \
                 mock.patch.object(session, "HttpTransport", TransportStub), \
                 mock.patch.object(session, "ledger_for", return_value=led), \
                 mock.patch.object(session, "policy_for",
                                   side_effect=_hermetic_policy_for):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as ctx:
                        cli.main(["bench", os.path.join(d, "task.json"),
                                  "--max-cost", "0.02"])
            self.assertEqual(ctx.exception.code, 2)  # honest verify_failed
            self.assertTrue(posts, "apply loop must issue chat posts")
            self.assertTrue(all(k == "sk-the-real-key" for k in posts),
                            "every engine chat call must carry the verified key")

    def test_cli_rejects_ungated_continuation_before_key_setup(self):
        """The CLI boundary must reject a failed state before touching credentials
        (moved from test_apply.py: this is a CLI-boundary behavior, and it must
        keep holding no matter how the engine assembles its dependencies)."""
        with tempfile.TemporaryDirectory() as td:
            state_path = os.path.join(td, "state.json")
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump({"file_path": os.path.join(td, "math.py"),
                           "verify_only": False,
                           "verification_required": True}, f)
            with mock.patch("harness.cli._governor",
                            side_effect=AssertionError("key setup must not run")):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as ctx:
                        cli.main(["apply", "--continue-from", state_path])
            self.assertEqual(ctx.exception.code, 1)


class CheckShippedTests(unittest.TestCase):
    """`capabilities --check-shipped`: the shipped default lanes validated
    against the live catalog. The stale-id defect class (a shipped pool id
    leaving OpenRouter's catalog, hard-fatals at fetch_pricing) recurred
    twice before this became machine-checked; these pins cover the check's
    own logic hermetically -- the live catalog contact is pinned in
    test_judge.CheckShippedLiveTests (skipped without a key)."""

    def _catalog(self, *missing):
        from harness.config import shipped_model_ids
        return [{"id": mid} for mid in shipped_model_ids() if mid not in missing]

    def test_healthy_catalog_passes(self):
        captured = {}
        gov = mock.Mock()
        gov.fetch_models.return_value = self._catalog()
        with mock.patch.object(cli, "_governor", return_value=("key", gov)), \
             mock.patch.object(cli, "_emit",
                               side_effect=lambda payload, out: captured.update(payload)):
            cli.main(["capabilities", "--check-shipped"])
        self.assertTrue(captured["ok"])
        self.assertEqual(captured["stale"], [])

    def test_stale_id_exits_2_and_names_it(self):
        import harness.config as cfg
        captured = {}
        # The live catalog has dropped one SHIPPED id (the real defect:
        # a formerly-live default id vanishing from /models).
        vanished = cfg.FREE_PANEL_POOL[0]
        gov = mock.Mock()
        gov.fetch_models.return_value = self._catalog(vanished)
        with mock.patch.object(cli, "_governor", return_value=("key", gov)), \
             mock.patch.object(cli, "_emit",
                               side_effect=lambda payload, out: captured.update(payload)), \
             contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit) as ctx:
                cli.main(["capabilities", "--check-shipped"])
        self.assertEqual(ctx.exception.code, 2)
        self.assertFalse(captured["ok"])
        self.assertEqual(captured["stale"], [vanished])
        self.assertIn(vanished, err.getvalue())

    def test_shipped_enumeration_is_complete(self):
        """shipped_model_ids must cover every pool constant: a future pool
        added to config.py but not to the enumeration would silently escape
        the freshness check."""
        import harness.config as cfg
        self.assertIn(cfg.FREE_PANEL_POOL[0], cfg.shipped_model_ids())
        self.assertIn(cfg.FREE_APPLY_POOL[0], cfg.shipped_model_ids())
        self.assertIn(cfg.SPECIALIST_POOL_FREE[0], cfg.shipped_model_ids())
        self.assertIn(cfg.DEFAULT_PANEL_PAID[0], cfg.shipped_model_ids())
        self.assertIn(cfg.DEFAULT_JUDGE_PAID, cfg.shipped_model_ids())
        self.assertIn(cfg.DEFAULT_APPLY_MODEL_PAID, cfg.shipped_model_ids())
        self.assertIn(cfg.SPECIALIST_POOL_PAID[0], cfg.shipped_model_ids())
        self.assertIn(cfg.FREE_JUDGE, cfg.shipped_model_ids())


class LedgerTailCountTests(unittest.TestCase):
    """Playtest finding: the obvious `ledger tail 5` errored with
    "unrecognized arguments" while bare `ledger tail` silently capped at 20."""

    def _run_tail(self, argv):
        calls = {}
        ledger = mock.Mock()
        ledger.tail.side_effect = lambda n: calls.setdefault("n", n)
        ledger.entries.return_value = list(range(50))
        ledger.chain_status.return_value = {"segments": 1, "entries": 50}
        out = io.StringIO()
        with mock.patch.object(cli, "_ledger", return_value=ledger), \
             mock.patch.object(cli, "_emit",
                               side_effect=lambda p, o: out.write(json.dumps(p))):
            cli.main(argv)
        return calls.get("n"), json.loads(out.getvalue())

    def test_tail_honors_positional_count(self):
        n, report = self._run_tail(["ledger", "tail", "5"])
        self.assertEqual(n, 5)
        self.assertEqual(report["count"], 50)

    def test_tail_default_is_20(self):
        n, _ = self._run_tail(["ledger", "tail"])
        self.assertEqual(n, 20)

    def test_tail_rejects_nonpositive_count(self):
        """tail(0) is [-0:] == the whole ledger and negative n slices from
        the front -- both must fail loudly, not return silent nonsense."""
        err = io.StringIO()
        with mock.patch.object(cli, "_ledger") as ledger:
            with contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit) as ctx:
                    cli.main(["ledger", "tail", "0"])
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("positive", err.getvalue())
        ledger.assert_not_called()

    def test_ledger_verify_exits_zero_on_valid_chain(self):
        ledger = mock.Mock()
        ledger.verify.return_value = (True, None)
        ledger.chain_status.return_value = {"ok": True, "entries": 10}
        out = io.StringIO()
        with mock.patch.object(cli, "_ledger", return_value=ledger), \
             mock.patch.object(cli, "_emit",
                               side_effect=lambda p, o: out.write(json.dumps(p))):
            cli.main(["ledger", "verify"])
        payload = json.loads(out.getvalue())
        self.assertTrue(payload["verified"])

    def test_ledger_verify_exits_2_on_broken_chain(self):
        # DF-CLI-1: harness ledger verify exits 2 on broken chain
        ledger = mock.Mock()
        ledger.verify.return_value = (False, 4)
        ledger.chain_status.return_value = {"ok": False, "first_bad_seq": 4}
        out = io.StringIO()
        with mock.patch.object(cli, "_ledger", return_value=ledger), \
             mock.patch.object(cli, "_emit",
                               side_effect=lambda p, o: out.write(json.dumps(p))):
            with self.assertRaises(SystemExit) as ctx:
                cli.main(["ledger", "verify"])
        self.assertEqual(ctx.exception.code, 2)
        payload = json.loads(out.getvalue())
        self.assertFalse(payload["verified"])
        self.assertEqual(payload["first_bad_seq"], 4)

    def test_models_rejects_nonpositive_limit(self):
        """Playtest finding: --limit -5 silently mis-sliced the catalog."""
        err = io.StringIO()
        with mock.patch.object(cli, "_governor") as gov, \
             contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                cli.main(["models", "--limit", "0"])
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("positive", err.getvalue())
        gov.assert_not_called()


class SelfHostImportTests(unittest.TestCase):
    """Live dogfood finding: a deferred self-edit left harness/bench.py
    unimportable (model dropped load_manifest), and `python -m harness.cli
    continue` died at ITS import line before reaching the resume logic.
    The bench import is guarded at module level, so a broken bench module
    breaks only `bench` (clean HarnessError, before key/session setup) --
    every other command, notably `continue`, still starts."""

    def test_cli_imports_without_bench(self):
        import importlib
        import sys
        import harness.cli as cli_mod
        from harness.errors import HarnessError
        with mock.patch.dict(sys.modules, {"harness.bench": None}):
            reloaded = importlib.reload(cli_mod)
        try:
            self.assertTrue(hasattr(reloaded, "main"))
            self.assertIsNone(reloaded.load_manifest)
            # Only `bench` itself may fail, cleanly and before setup.
            with mock.patch.object(
                    reloaded, "_session",
                    side_effect=AssertionError("session must not run")):
                with self.assertRaisesRegex(HarnessError, "failed to import"):
                    reloaded._cmd_bench(mock.Mock(), mock.Mock())
        finally:
            importlib.reload(cli_mod)


class ApplyBatchKeepGoingCliTests(unittest.TestCase):
    """Batch fail-soft through the REAL CLI entry point (parser -> dispatch
    -> engine -> batch loop -> exit policy): --keep-going continues past a
    failed file and the batch still exits honestly; the default (no flag)
    stays fail-fast byte-for-byte."""

    def _run(self, d, a, b, extra):
        from harness.apply import ApplyEngine
        from harness.ledger import AutonomyLedger
        from harness.router import Router
        from harness.spend import SpendGovernor
        from tests._applyfixture import scripted_run
        from tests._fake import FakeTransport, comp, m

        CHANGED = "def add(a, b):\n    return a + b + 0\n"
        fake = FakeTransport(models=[m("deepseek/deepseek-chat"),
                                     m("inclusionai/ling-2.6-flash")],
                             posts=[comp(CHANGED), comp(CHANGED)])
        gov = SpendGovernor(fake, "sk-test")
        gov.max_cost = 0.05
        ledger = AutonomyLedger(os.path.join(d, "ledger.jsonl"))
        router = Router(["a", "b"], "inclusionai/ling-2.6-flash",
                        "deepseek/deepseek-chat")
        engine = ApplyEngine(fake, "k", gov, ledger, router,
                             default_require_consent=False,
                             default_renew_consent=False)
        engine.run_verify = scripted_run([(1, "boom"), (0, "")])
        out_path = os.path.join(d, "result.json")
        argv = ["apply", "--file", a, "--file", b, "--instruction", "change",
                "--verify", "check", "--max-rounds", "1",
                "--out", out_path] + extra
        with mock.patch.object(cli, "_session", return_value=engine), \
             mock.patch.object(session, "governor_for", return_value=("k", gov)), \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                cli.main(argv)
        with open(out_path, encoding="utf-8") as f:
            return ctx.exception.code, json.load(f)

    def test_keep_going_runs_remaining_files_and_exits_honestly(self):
        with tempfile.TemporaryDirectory() as d:
            a = os.path.join(d, "a.py")
            b = os.path.join(d, "b.py")
            for p in (a, b):
                with open(p, "w", encoding="utf-8") as f:
                    f.write("def add(a, b):\n    return a + b\n")
            code, result = self._run(d, a, b, ["--keep-going"])
            # Honest exit: a mixed batch is still a failure.
            self.assertEqual(code, 2)
            self.assertEqual(result["status"], "verify_failed")
            self.assertEqual(result["statuses"],
                             {"verify_failed": 1, "ok": 1})
            self.assertEqual(len(result["results"]), 2)
            self.assertFalse(result["verify"]["passed"])
            with open(b, encoding="utf-8") as f:
                self.assertEqual(f.read(),
                                 "def add(a, b):\n    return a + b + 0\n")

    def test_default_without_flag_stays_fail_fast(self):
        with tempfile.TemporaryDirectory() as d:
            a = os.path.join(d, "a.py")
            b = os.path.join(d, "b.py")
            for p in (a, b):
                with open(p, "w", encoding="utf-8") as f:
                    f.write("def add(a, b):\n    return a + b\n")
            code, result = self._run(d, a, b, [])
            self.assertEqual(code, 2)
            self.assertEqual(result["status"], "verify_failed")
            self.assertEqual(result["statuses"], {"verify_failed": 1})
            self.assertEqual(len(result["results"]), 1)
            with open(b, encoding="utf-8") as f:
                self.assertEqual(f.read(), "def add(a, b):\n    return a + b\n")


class CliResumeE2eTests(unittest.TestCase):
    """A SUCCESSFUL resume through the real entry point (parser -> dispatch
    -> engine -> run_batch -> _prepare): the per-file payload must carry the
    validated continuation and the saved task identity. The dual-mode
    options path silently dropped the continuation to None and survived two
    commits because only the rejection path was e2e-tested."""

    def _run(self, d, target, argv_extra):
        from harness.apply import ApplyEngine
        from harness.continuation import gate_id
        from harness.ledger import AutonomyLedger
        from harness.router import Router
        from harness.spend import SpendGovernor
        from tests._applyfixture import scripted_run
        from tests._fake import FakeTransport, comp, m

        CHANGED = "def add(a, b):\n    return a + b + 0\n"
        state_path = os.path.join(d, "state.json")
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump({"file_path": target, "task_id": "orig-task",
                       "verify_cmd": "check", "verify_gate_id": gate_id("check"),
                       "verify_only": False, "verification_required": True,
                       "remaining_scope": "make add() defensive",
                       "rounds": []}, f)
        fake = FakeTransport(models=[m("deepseek/deepseek-chat")],
                             posts=[comp(CHANGED)])
        gov = SpendGovernor(fake, "sk-test")
        gov.max_cost = 0.05
        ledger = AutonomyLedger(os.path.join(d, "ledger.jsonl"))
        engine = ApplyEngine(fake, "k", gov, ledger,
                             Router(["a"], "a", "deepseek/deepseek-chat"),
                             default_require_consent=False,
                             default_renew_consent=False)
        engine.run_verify = scripted_run([(0, "")])
        out_path = os.path.join(d, "result.json")
        seen = {}
        real_prepare = ApplyEngine._prepare

        def spying_prepare(self, kwargs):
            seen.update(kwargs)
            return real_prepare(self, kwargs)

        argv = argv_extra + ["--continue-from" if argv_extra[:1] == ["apply"]
                             else "--state", state_path,
                             "--verify", "check", "--out", out_path]
        with mock.patch.object(cli, "_session", return_value=engine), \
             mock.patch.object(session, "governor_for", return_value=("k", gov)), \
             mock.patch.object(ApplyEngine, "_prepare", spying_prepare), \
             contextlib.redirect_stderr(io.StringIO()):
            cli.main(argv)
        with open(out_path, encoding="utf-8") as f:
            return seen, json.load(f)

    def test_apply_continue_from_completes_resume_with_saved_identity(self):
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "math.py")
            with open(target, "w", encoding="utf-8") as f:
                f.write("def add(a, b):\n    return a + b\n")
            seen, result = self._run(d, target, ["apply"])
            # The saved task identity rides the per-file payload: a resume
            # is the same task, not a new one.
            self.assertEqual(seen["task_id"], "orig-task")
            self.assertEqual(seen["file_path"], target)
            self.assertEqual(seen["continuation"]["task_id"], "orig-task")
            # The resume completed: gate passed, edit written, honest exit.
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["task_id"], "orig-task")
            self.assertTrue(result["verify"]["passed"])
            with open(target, encoding="utf-8") as f:
                self.assertEqual(f.read(),
                                 "def add(a, b):\n    return a + b + 0\n")

    def test_continue_face_completes_resume_with_saved_identity(self):
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "math.py")
            with open(target, "w", encoding="utf-8") as f:
                f.write("def add(a, b):\n    return a + b\n")
            seen, result = self._run(d, target, ["continue"])
            self.assertEqual(seen["task_id"], "orig-task")
            self.assertEqual(seen["continuation"]["task_id"], "orig-task")
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["task_id"], "orig-task")

    def test_continue_face_with_instruction_overrides_continuation_scope(self):
        # DF-APPLY-1: harness continue --instruction X overrides continuation remaining_scope
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "math.py")
            with open(target, "w", encoding="utf-8") as f:
                f.write("def add(a, b):\n    return a + b\n")
            seen, result = self._run(d, target, ["continue", "--instruction", "refactor add cleanly"])
            self.assertEqual(seen["task_id"], "orig-task")
            self.assertEqual(seen["instruction"], "refactor add cleanly")
            self.assertEqual(seen["continuation"]["remaining_scope"], "refactor add cleanly")
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["task_id"], "orig-task")


class CliParserSurfaceTests(unittest.TestCase):
    """The parser owner registers every dispatchable command and no more;
    handler aliases stay importable from cli (the established test seams)."""

    def test_parser_registers_exactly_the_dispatchable_commands(self):
        from harness.cli import _DISPATCH
        from harness.cli_parser import build_parser

        import argparse
        sub_action = next(a for a in build_parser()._actions
                          if isinstance(a, argparse._SubParsersAction))
        registered = set(sub_action.choices)
        self.assertEqual(registered, set(_DISPATCH) | {"serve", "desktop", "media"})

    def test_handler_seams_stay_importable_from_cli(self):
        import harness.cli as cli

        for name in ("_cmd_apply", "_cmd_verify", "_run_claims_verify",
                     "_read_text", "_governor", "_session"):
            self.assertTrue(hasattr(cli, name), name)


class CliPresentationOwnerTests(unittest.TestCase):
    """S6 mirror pin: presentation lives in cli_report (the ONE owner of
    the report/exit-code rendering contract); cli.py re-exports the seams
    so commands and tests keep their established patch points."""

    def test_presentation_importable_from_owner_and_cli(self):
        import harness.cli as cli
        import harness.cli_report as owner

        for name in ("_emit", "_emit_by_status", "_print_capabilities_table"):
            fn = getattr(owner, name, None)
            self.assertTrue(callable(fn), name)
            # cli.py consumes the owner (one def site), and the cli seam
            # stays the same object so patches keep working.
            self.assertIs(getattr(cli, name), fn, name)


if __name__ == "__main__":
    unittest.main()
