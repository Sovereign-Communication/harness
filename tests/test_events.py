"""Phase 1 UI groundwork: the typed event stream (harness/events.py), the
TTY-pretty renderer (harness/render.py), and the envelope enrichments.

All hermetic: no network, no key. Events are advisory telemetry -- every
test here also pins that a broken or absent sink can never change a run.
"""
import contextlib
import io
import json
import os
import tempfile
import unittest

from harness import events, render


def _collect():
    """A hermetic sink: captures events in a list (with a lock-free append,
    safe for the single-threaded tests here)."""
    captured = []
    return captured, captured.append


class EventsBusTests(unittest.TestCase):
    def setUp(self):
        self._sinks = list(events._sinks)
        self._seq = events._seq
        events._sinks.clear()
        events._seq = 0

    def tearDown(self):
        events._sinks.clear()
        events._sinks.extend(self._sinks)
        events._seq = self._seq

    def test_emit_with_no_sinks_is_silent_noop(self):
        # Must not raise and must not touch the seq counter.
        events.emit("panel_call", model="m")
        self.assertEqual(events._seq, 0)

    def test_emit_reaches_every_sink_with_seq_and_type(self):
        got, sink = _collect()
        events.add_sink(sink)
        events.emit("panel_call", task_id="t1", model="m")
        self.assertEqual(len(got), 1)
        ev = got[0]
        self.assertEqual(ev["type"], "panel_call")
        self.assertEqual(ev["task_id"], "t1")
        self.assertEqual(ev["seq"], 1)
        self.assertIsInstance(ev["ts"], float)

    def test_seq_increases_across_events(self):
        got, sink = _collect()
        events.add_sink(sink)
        events.emit("a")
        events.emit("b")
        self.assertEqual([e["seq"] for e in got], [1, 2])

    def test_broken_sink_is_dropped_not_raised(self):
        got, sink = _collect()

        def broken(_ev):
            raise RuntimeError("sink exploded")

        events.add_sink(broken)
        events.add_sink(sink)
        events.emit("preflight")
        # The healthy sink still received the event; the broken one was culled.
        self.assertEqual([e["type"] for e in got], ["preflight"])
        self.assertEqual(events.sink_count(), 1)

    def test_duplicate_sink_refused(self):
        _, sink = _collect()
        self.assertEqual(events.add_sink(sink), sink)
        self.assertIsNone(events.add_sink(sink))

    def test_jsonl_sink_appends_parseable_lines_and_lazily_creates(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "events.jsonl")
            self.assertFalse(os.path.exists(path))  # lazy: no file yet
            sink = events.add_jsonl_sink(path)
            try:
                events.emit("spend_check", spent=0.0, ceiling=0.02)
                with open(path, encoding="utf-8") as f:
                    lines = [json.loads(x) for x in f if x.strip()]
            finally:
                events.remove_sink(sink)
                sink.close()
            self.assertEqual(len(lines), 1)
            self.assertEqual(lines[0]["type"], "spend_check")
            self.assertEqual(lines[0]["ceiling"], 0.02)

    def test_jsonl_sink_same_path_not_registered_twice(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "events.jsonl")
            first = events.add_jsonl_sink(path)
            try:
                self.assertIsNone(events.add_jsonl_sink(path))
            finally:
                events.remove_sink(first)
                first.close()

    def test_sink_cap_enforced(self):
        sinks = []
        for _ in range(events.MAX_SINKS + 3):
            s = events.add_sink(lambda ev: None)
            if s is not None:
                sinks.append(s)
        self.assertEqual(len(sinks), events.MAX_SINKS)


class RenderTests(unittest.TestCase):
    def _pretty(self, result, **kw):
        err, out = io.StringIO(), io.StringIO()
        os.environ["HARNESS_FORCE_PRETTY"] = "1"
        try:
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
                rendered = render.pretty_print(result, **kw)
        finally:
            os.environ.pop("HARNESS_FORCE_PRETTY", None)
        return rendered, err.getvalue(), out.getvalue()

    def test_force_pretty_renders_apply_timeline(self):
        result = {
            "status": "ok", "task_id": "t1", "file": "a.py", "cost": 0.0,
            "rounds": [{"round": 1, "model": "m1", "status": "ok", "cost": 0.0,
                        "verify_output": "", "changed": True, "verify_passed": True}],
            "verify": {"command": "python -m py_compile a.py", "passed": True},
        }
        rendered, err, out = self._pretty(result)
        self.assertTrue(rendered)
        self.assertIn("apply", err)
        self.assertIn("m1", err)
        self.assertIn("gate PASS", err.replace("\x1b[32m", "").replace("\x1b[0m", ""))
        self.assertIn("summary", err)

    def test_piped_mode_returns_false_without_rendering(self):
        # _should_pretty consults stdout.isatty; a redirected stdout is not a
        # TTY, so with no force flag this must decline and print nothing.
        err, out = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            rendered = render.pretty_print({"status": "ok", "rounds": []})
        self.assertFalse(rendered)
        self.assertEqual(err.getvalue(), "")

    def test_no_color_strips_ansi(self):
        render.set_color_enabled(False)
        try:
            result = {"status": "verify_failed", "rounds": [], "cost": 0.0}
            rendered, err, _ = self._pretty(result)
            self.assertTrue(rendered)
            self.assertNotIn("\x1b[", err)
            self.assertIn("verify_failed", err)
        finally:
            render.set_color_enabled(True)

    def test_verify_tally_rendered(self):
        result = {
            "verdict": "Deterministic panel tally: c1=real (3R/0NR of 3)",
            "consensus": {"agreement": "high", "confidence": 1.0, "defer": False},
            "convergence": {"tally": {"claims": {
                "c1": {"verdict": "real", "real_votes": 3, "not_real_votes": 0,
                       "voted_by": 3, "of_panel": 3}}}},
            "panel_results": [], "panel_failures": [],
        }
        rendered, err, _ = self._pretty(result)
        self.assertTrue(rendered)
        self.assertIn("c1", err)
        self.assertIn("3R/0NR", err)

    def test_ledger_tail_and_bench_shapes(self):
        tail = {"entries": [{"seq": 3, "ts": 1.0, "event": "complete",
                             "model": "m"}], "count": 1}
        rendered, err, _ = self._pretty(tail)
        self.assertTrue(rendered)
        self.assertIn("complete", err)
        bench = {"bench": {"results": [{"name": "add", "status": "ok",
                                        "cost": 0.0}],
                           "statuses": {"ok": 1}}, "cost": 0.0}
        rendered, err, _ = self._pretty(bench)
        self.assertTrue(rendered)
        self.assertIn("add", err)

    def test_unknown_shape_falls_back_to_json_not_hidden(self):
        weird = {"definitely_not_a_known_shape": {"nested": [1, 2, 3]}}
        rendered, err, _ = self._pretty(weird)
        self.assertTrue(rendered)
        self.assertIn("definitely_not_a_known_shape", err)

    def test_render_never_mutates_the_result(self):
        result = {"status": "ok", "rounds": [{"round": 1}], "cost": 0.0}
        before = json.dumps(result, sort_keys=True)
        self._pretty(result)
        self.assertEqual(json.dumps(result, sort_keys=True), before)


class EnrichmentTests(unittest.TestCase):
    def test_models_envelope_has_meta_and_catalog_rows(self):
        """With --all, ids become catalog rows (id/context/prices) and the
        envelope grows a meta block."""
        import unittest.mock as m
        from harness import cli
        captured = {}
        gov = m.Mock()
        gov.max_cost = 0.02
        gov.fetch_models.return_value = [
            {"id": "b/model-b", "context_length": 8192,
             "pricing": {"prompt": "0", "completion": "0"}},
            {"id": "a/model-a", "context_length": 32768,
             "pricing": {"prompt": "0.000001", "completion": "0.000002"}},
        ]
        settings = type("S", (), {"use_free": True, "panel_pool": ["a/model-a"],
                                  "expect_key_label": None})()
        opts = type("O", (), {"limit": 5, "all": True, "out": None})()
        with m.patch.object(cli, "_governor", return_value=("k", gov)), \
                m.patch.object(cli, "discover_free_models",
                               return_value=["a/model-a"]) as disc, \
                m.patch.object(cli, "_emit",
                               side_effect=lambda r, o: captured.update(r=r)):
            cli._cmd_models(opts, settings)
        disc.assert_called_once()  # hermetic: no real transport /models call
        r = captured["r"]
        self.assertEqual(r["count"], 2)
        self.assertEqual([row["id"] for row in r["models"]],
                         ["a/model-a", "b/model-b"])
        self.assertEqual(r["models"][0]["context"], 32768)
        self.assertEqual(r["meta"]["source"], "openrouter:/models")

    def test_run_meta_never_contains_label_or_key(self):
        from harness import cli

        class Gov:
            max_cost = 0.02

        class S:
            use_free = True
            panel = ["p"]
            judge = "j"
            apply_model = "a"
            reasoning_effort = "auto"
            expect_key_label = "secret-label"

        meta = cli._run_meta(S(), Gov())
        blob = json.dumps(meta)
        self.assertNotIn("secret-label", blob)
        self.assertEqual(meta["max_cost_ceiling"], 0.02)

    def test_run_meta_survives_mock_ceiling(self):
        from harness import cli
        import unittest.mock as m
        meta = cli._run_meta(type("S", (), {"use_free": True, "panel": [],
                                            "judge": "j", "apply_model": "a",
                                            "reasoning_effort": "auto",
                                            "expect_key_label": None})(),
                             m.Mock())
        self.assertIsNone(meta["max_cost_ceiling"])


if __name__ == "__main__":
    unittest.main()
