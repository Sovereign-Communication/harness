import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from harness import cli, session
from harness.config import load_settings
from harness.errors import HarnessError
from harness.router import Router


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

        with mock.patch.object(cli, "_governor", side_effect=fake_governor), \
             mock.patch.object(cli, "panel_judge", return_value={}), \
             mock.patch.object(session, "HttpTransport"):
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

        with mock.patch.object(cli, "_governor", side_effect=fake_governor), \
             mock.patch.object(cli, "panel_judge", return_value={}), \
             mock.patch.object(session, "HttpTransport"):
            cli.main(["verify", "--prompt", "hi"])
        self.assertIsNone(captured["override"])


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
    """Dogfooding round 1 (audits/self): cli._engine() (the deduped ApplyEngine
    construction site) dropped api_key, so EVERY engine-routed chat call went
    out with no Authorization header and 401'd -- while panel-lane calls, which
    receive the key directly, worked. Hermetic tests build engines directly, so
    the suite never noticed. Pin the wiring: the engine must carry the same key
    the governor verified."""

    def test_engine_receives_the_verified_api_key(self):
        settings = load_settings()
        engine = cli._engine(settings, "sk-the-real-key", object(), object(),
                             Router(["a"], "j", "m"))
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

            # Bench composes through session.apply_session; patch the
            # governor seam at its owner (session), so this proves the
            # session-composed engine carries the verified key end to end.
            with mock.patch.object(session, "governor_for", side_effect=fake_governor), \
                 mock.patch.object(session, "HttpTransport", TransportStub):
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


if __name__ == "__main__":
    unittest.main()
