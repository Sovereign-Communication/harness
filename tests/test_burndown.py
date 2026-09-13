"""SCMessenger handoff burndown (docs/scmessenger-burndown.md), hermetic:

P0 judge-seat fallback rotation + truncation detection (panel.py),
P1 gpt-5/o1 reasoning hints (chat.py), P1 utf-8-sig inbound readers
(claims.py/cli.py), P3 ledger defer-stats (ledger.py + CLI + server).

No network, no key: every judge-failure mode is a canned transport body.
"""
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from harness import events
from harness.chat import looks_reasoning
from harness.claims import load_claims_manifest
from harness.ledger import AutonomyLedger
from harness.chat import looks_truncated
from harness.panel import panel_judge

from tests._fake import FakeTransport, _gov, comp, m

JUDGE = "inclusionai/ling-2.6-flash"
P1 = "meta-llama/llama-3.1-8b-instruct"
P2 = "ibm-granite/granite-4.1-8b"
FREE_FALLBACK = "google/gemma-4-31b-it:free"

GOOD_SYNTHESIS = ('{"verdict":"APPROVE","agreement":"high",'
                  '"confidence":0.9,"disagreements":[],"defer":false}')
# The Sep-11 seat-gate failure verbatim: a fence that opens and stops.
TRUNCATED_SYNTHESIS = '```json\n{\n  "verdict": "APPROVE",\n  "agreement": "high",\n'


class ReasoningHintTests(unittest.TestCase):
    """P1: the live gpt-5 judge failures were reasoning-only output because
    looks_reasoning('openai/gpt-5') was False and auto sent no reasoning cap."""

    def test_gpt5_and_o1_are_recognized(self):
        self.assertTrue(looks_reasoning("openai/gpt-5"))
        self.assertTrue(looks_reasoning("openai/gpt-5-mini"))
        self.assertTrue(looks_reasoning("openai/o1"))

    def test_non_reasoning_models_unchanged(self):
        # Pinned by the shipped pools: these must stay non-reasoning.
        self.assertFalse(looks_reasoning("cohere/north-mini-code:free"))
        self.assertFalse(looks_reasoning("google/gemma-4-31b-it:free"))
        self.assertFalse(looks_reasoning("meta-llama/llama-3.1-8b-instruct"))


class TruncationHeuristicTests(unittest.TestCase):
    def test_sep11_body_is_truncated(self):
        self.assertTrue(looks_truncated(TRUNCATED_SYNTHESIS))

    def test_balanced_fenced_json_is_not_truncated(self):
        body = '```json\n{"verdict": "APPROVE", "defer": false}\n```'
        self.assertFalse(looks_truncated(body))

    def test_prose_with_embedded_json_is_not_truncated(self):
        body = 'Here is my view: {"a": [1, 2]} and some prose after.'
        self.assertFalse(looks_truncated(body))

    def test_unbalanced_brackets_are_truncated(self):
        self.assertTrue(looks_truncated('{"verdict": "x", "list": [1, 2'))


class JudgeRotationTests(unittest.TestCase):
    """P0: every mode that lost a verdict in the handoff now has a second
    path -- or an honest named failure -- and never fabricates a verdict."""

    def _run(self, posts, **kw):
        # 3-model pool, 2 seats: mirrors the handoff geometry (a pool larger
        # than the panel, leaving an un-voted member as a fallback candidate).
        fake = FakeTransport(
            models=[m(P1), m(P2), m(JUDGE),
                    m(FREE_FALLBACK, prompt="0", completion="0")],
            posts=posts)
        gov = _gov(fake)
        opts = dict(transport=fake, api_key="k", governor=gov, prompt="Q?",
                    panel=[P1, P2, FREE_FALLBACK], judge=JUDGE, max_panelists=2)
        opts.update(kw)
        result = panel_judge(**opts)
        return result, fake

    def test_reasoning_only_primary_rotates_to_free_fallback(self):
        # Today's live gpt-5 failure mode: reasoning trace instead of content.
        reasoning_body = comp(None, reasoning="thinking... a lot")
        result, fake = self._run(
            [comp("take one"), comp("take two"), reasoning_body,
             comp(GOOD_SYNTHESIS)])
        self.assertEqual(result["judge_synthesis_status"], "parseable")
        self.assertEqual(result["judge_model"], FREE_FALLBACK)
        self.assertFalse(result["consensus"]["defer"])
        self.assertEqual(len(fake.chat_posts()), 4)

    def test_truncated_primary_rotates(self):
        # The Sep-11 seat-gate loss: a 57-char body cut mid-JSON.
        result, fake = self._run(
            [comp("take one"), comp("take two"),
             comp(TRUNCATED_SYNTHESIS), comp(GOOD_SYNTHESIS)])
        self.assertEqual(result["judge_synthesis_status"], "parseable")
        self.assertFalse(result["consensus"]["defer"])

    def test_unparseable_complete_body_is_not_called_truncated(self):
        result, _ = self._run(
            [comp("take one"), comp("take two"), comp("plain prose, no json"),
             comp(GOOD_SYNTHESIS)])
        self.assertEqual(result["judge_synthesis_status"], "parseable")

    def test_transient_502_retries_same_seat_then_succeeds(self):
        # The round7 01_is_poison failure: judge http_502, raw outputs only.
        result, fake = self._run(
            [comp("take one"), comp("take two"),
             (502, {"error": {"message": "bad gateway"}}),
             comp(GOOD_SYNTHESIS)])
        self.assertEqual(result["judge_synthesis_status"], "parseable")
        self.assertEqual(result["judge_model"], JUDGE)
        self.assertFalse(result["consensus"]["defer"])
        # Third post is the failed judge, fourth the bounded retry.
        self.assertEqual(len(fake.chat_posts()), 4)

    def test_permanent_500_rotates_to_fallback(self):
        result, _ = self._run(
            [comp("take one"), comp("take two"),
             (500, {"error": {"message": "down"}}),  # primary
             (500, {"error": {"message": "down"}}),  # transient retry
             comp(GOOD_SYNTHESIS)])                  # fallback candidate
        self.assertEqual(result["judge_synthesis_status"], "parseable")
        self.assertEqual(result["judge_model"], FREE_FALLBACK)

    def test_exhausted_seat_still_defers_with_raw_outputs(self):
        # The contract that must never break: no verdict is ever fabricated.
        # Both fallback attempts fail too (two candidates, two failures).
        result, _ = self._run(
            [comp("take one"), comp("take two"),
             (500, {"error": {"message": "down"}}),   # primary judge
             (500, {"error": {"message": "down"}}),   # transient retry
             (500, {"error": {"message": "down"}}),   # fallback candidate 1
             (500, {"error": {"message": "down"}})])  # fallback candidate 2
        self.assertIsNone(result["judge_synthesis"])
        self.assertEqual(result["judge_synthesis_status"], "http_500")
        self.assertIn("raw panel outputs", result["verdict"])
        self.assertTrue(result["consensus"]["defer"])

    def test_panelists_who_voted_are_not_fallback_candidates(self):
        # Pool = [P1, P2] with 2 seats: both vote; the judge seat is P2's
        # paid id and 500s twice (primary + transient retry). No candidate
        # exists, so exactly two judge posts happen: a voted panelist must
        # never be called again as the judge.
        _, fake = self._run(
            [comp("take one"), comp("take two"),
             (500, {"error": {"message": "down"}}),
             (500, {"error": {"message": "down"}})],
            panel=[P1, P2], judge=P2, max_panelists=2)
        self.assertEqual(len(fake.chat_posts()), 4)
        # The judge seat ended on P2 without any third judge attempt.
        models = [p["model"] for p in fake.payloads()]
        self.assertEqual(models[-2:], [P2, P2])

    def test_failed_panelist_is_eligible_fallback_candidate(self):
        # A pool member below the seat target stays un-voted and remains a
        # judge candidate; here the primary succeeds so it is never used.
        fake = FakeTransport(models=[m(P1), m(FREE_FALLBACK), m(JUDGE)],
                             posts=[comp("take one"),
                                    comp(GOOD_SYNTHESIS),   # fallback panelist
                                    comp(GOOD_SYNTHESIS)])  # judge seat: fine
        gov = _gov(fake)
        result = panel_judge(transport=fake, api_key="k", governor=gov,
                             prompt="Q?", panel=[P1, FREE_FALLBACK], judge=JUDGE)
        self.assertEqual(result["judge_synthesis_status"], "parseable")
        self.assertEqual(result["judge_model"], JUDGE)  # primary succeeded

    def test_fallback_rotation_is_event_streamed(self):
        captured = []
        events._sinks.append(captured.append)
        try:
            self._run([comp("take one"), comp("take two"),
                       comp(TRUNCATED_SYNTHESIS), comp(GOOD_SYNTHESIS)])
        finally:
            events._sinks.remove(captured.append)
        reasons = [e.get("reason") for e in captured
                   if e.get("type") == "rotation"]
        self.assertIn("judge_fallback", reasons)


class BomToleranceTests(unittest.TestCase):
    """P1: handoff claims.json was BOM'd by a PowerShell redirect and failed
    json.load; the 119a9c2 fix covered local_fit only."""

    def _write(self, tmp, name, bom):
        path = os.path.join(tmp, name)
        with open(path, "wb") as f:
            f.write(b'\xef\xbb\xbf' if bom else b'')
            f.write(json.dumps({
                "context": "unit",
                "claims": [{"id": "C1", "text": "alpha() returns None",
                            "kind": "defect", "source_refs": [1]}],
            }).encode())
        return path

    def test_bomd_claims_manifest_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "claims.json", bom=True)
            ctx, claims = load_claims_manifest(path)
            self.assertEqual(ctx, "unit")
            self.assertEqual(claims[0].claim_id, "C1")

    def test_plain_claims_manifest_still_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "claims.json", bom=False)
            _, claims = load_claims_manifest(path)
            self.assertEqual(len(claims), 1)

    def test_bomd_source_and_state_files_read_cleanly(self):
        from harness.cli import _read_json, _read_text
        with tempfile.TemporaryDirectory() as tmp:
            text_path = os.path.join(tmp, "source.txt")
            with open(text_path, "wb") as f:
                f.write(b'\xef\xbb\xbfalpha() returns None\n')
            self.assertEqual(_read_text(text_path, "--source-file"),
                             "alpha() returns None\n")
            json_path = os.path.join(tmp, "state.json")
            with open(json_path, "wb") as f:
                f.write(b'\xef\xbb\xbf{"rounds": []}')
            self.assertEqual(_read_json(json_path, "--state"), {"rounds": []})


class DeferStatsTests(unittest.TestCase):
    """P3: operator view of WHY runs deferred (the round7 ask)."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "ledger.jsonl")
        self.ledger = AutonomyLedger(self.path)

    def tearDown(self):
        self.dir.cleanup()

    def _seed(self):
        lg = self.ledger
        lg.append("model_result", task_id="r1", event_note="panel", model="a",
                  status="ok")
        lg.append("complete", task_id="r1", event_note="panel_judge",
                  model="j", agreement="high", status="ok")
        lg.append("complete", task_id="r2", event_note="panel_judge",
                  model="j", agreement="unknown", status="ok")  # lost judge
        lg.append("defer_midtask", task_id="r3", category="verify_failed",
                  reason="gate")
        lg.append("consent_defer", task_id="r4", model="a")

    def test_shape_and_counts(self):
        self._seed()
        s = self.ledger.defer_stats()
        self.assertEqual(s["panel_runs"], 2)
        self.assertEqual(s["panel_deferred"], 1)
        self.assertEqual(s["panel_defer_rate"], 0.5)
        self.assertEqual(s["agreements"], {"high": 1, "unknown": 1})
        self.assertEqual(s["defer_midtask_by_category"],
                         {"verify_failed": 1})
        self.assertEqual(s["consent_by_outcome"], {"consent_defer": 1})
        self.assertEqual(s["consent_blocked_total"], 1)
        # 1 lost-judge panel + 1 midtask + 1 consent-blocked.
        self.assertEqual(s["defer_total"], 3)

    def test_empty_ledger_is_json_safe_zeroes(self):
        s = self.ledger.defer_stats()
        self.assertEqual(s["panel_runs"], 0)
        self.assertIsNone(s["panel_defer_rate"])
        self.assertEqual(s["defer_total"], 0)

    def test_window_bounds_the_scan(self):
        self._seed()
        s = self.ledger.defer_stats(window=2)
        self.assertEqual(s["window"], 2)
        self.assertEqual(s["panel_runs"], 0)  # seeded rows are older


class DeferStatsCLITests(unittest.TestCase):
    def test_cli_defer_stats_wires_window(self):
        import io
        from harness import cli
        calls = {}
        ledger = mock.Mock()
        ledger.defer_stats.side_effect = \
            lambda window: calls.setdefault("window", window)
        out = io.StringIO()
        with mock.patch.object(cli, "_ledger", return_value=ledger), \
             mock.patch.object(cli, "_emit",
                               side_effect=lambda p, o: out.write(json.dumps(p))):
            cli.main(["ledger", "defer-stats", "50"])
        self.assertEqual(calls.get("window"), 50)

    def test_cli_defer_stats_rejects_nonpositive_window(self):
        # main() presents HarnessError as [FATAL] + exit 1.
        from harness import cli
        with mock.patch("sys.stderr", new_callable=io.StringIO), \
                self.assertRaises(SystemExit) as ctx:
            cli.main(["ledger", "defer-stats", "0"])
        self.assertEqual(ctx.exception.code, 1)


class DeferStatsServerTests(unittest.TestCase):
    def test_defer_stats_endpoint(self):
        from harness import server as ui_server
        ledger = mock.Mock()
        ledger.defer_stats.return_value = {"panel_runs": 3, "defer_total": 1}
        with mock.patch.object(ui_server, "load_settings"), \
             mock.patch.object(ui_server, "ledger_for", return_value=ledger):
            httpd = ui_server.make_server("127.0.0.1", 0)
            port = httpd.server_address[1]
            import threading
            import http.client
            t = threading.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
            try:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                conn.request("GET", "/api/ledger/defer-stats?window=50",
                             headers={"Host": "127.0.0.1"})
                resp = conn.getresponse()
                data = json.loads(resp.read())
                self.assertEqual(resp.status, 200)
                self.assertEqual(data["defer_stats"]["panel_runs"], 3)
                ledger.defer_stats.assert_called_once_with(window=50)
            finally:
                httpd.shutdown()
                httpd.server_close()
                t.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
