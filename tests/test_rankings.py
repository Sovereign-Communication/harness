"""Rankings-driven candidate refresh (ruling 7): the daily rankings API is
the evidence source; candidates must pass the ONE-vote probe before they are
proposed as probed. All hermetic (fake transport, no network)."""
import unittest

from harness.rankings import (_probe_vote, aggregate_rankings,
                              candidates_from_rankings, fetch_rankings)
from tests._fake import FakeTransport, comp, m

RANK_URL = "https://openrouter.ai/api/v1/datasets/rankings-daily"


def _rows():
    rows = []
    # climber: 100/day -> 300/day over the window
    for i in range(10):
        rows.append({"date": f"2026-09-{10 + i:02d}",
                     "model_permaslug": "deepseek-v4.1-flash",
                     "total_tokens": 100 + 20 * i})
    # steady high-volume incumbent
    for i in range(10):
        rows.append({"date": f"2026-09-{10 + i:02d}",
                     "model_permaslug": "gpt-5.6-luna",
                     "total_tokens": 1000})
    # falling: 300/day -> 100/day
    for i in range(10):
        rows.append({"date": f"2026-09-{10 + i:02d}",
                     "model_permaslug": "old-gen-v1",
                     "total_tokens": 300 - 20 * i})
    return rows


class FetchTests(unittest.TestCase):
    def test_fetch_parses_rows(self):
        fake = FakeTransport(models=[])
        fake.calls = []
        # fetch_rankings uses transport.get directly; emulate it.
        class T:
            def get(self, url, api_key, timeout=30):
                self.url = url
                return {"data": _rows()}
        t = T()
        rows = fetch_rankings(t, "k")
        self.assertEqual(t.url, RANK_URL)
        self.assertEqual(len(rows), 30)
        self.assertEqual(rows[0]["slug"], "deepseek-v4.1-flash")

    def test_fetch_raises_on_empty(self):
        from harness.errors import HarnessError

        class T:
            def get(self, url, api_key, timeout=30):
                return {"data": []}
        with self.assertRaises(HarnessError):
            fetch_rankings(T(), "k")

    def test_fetch_drops_malformed_rows(self):
        class T:
            def get(self, url, api_key, timeout=30):
                return {"data": [{"date": "2026-09-10"}, 17,
                                 {"date": "2026-09-11",
                                  "model_permaslug": "x/y",
                                  "total_tokens": "5"}]}
        rows = fetch_rankings(T(), "k")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["total_tokens"], 5.0)


class AggregateTests(unittest.TestCase):
    def test_totals_sorted_with_trends(self):
        agg = aggregate_rankings(_rows())
        self.assertEqual(agg["window"]["days"], 10)
        top = agg["totals"]
        self.assertEqual(top[0]["slug"], "gpt-5.6-luna")
        by_slug = {t["slug"]: t["trend"] for t in top}
        self.assertEqual(by_slug["deepseek-v4.1-flash"], "climbing")
        self.assertEqual(by_slug["gpt-5.6-luna"], "stable")
        self.assertEqual(by_slug["old-gen-v1"], "falling")

    def test_climbers_listed(self):
        agg = aggregate_rankings(_rows())
        self.assertIn("deepseek-v4.1-flash", agg["climbers"])
        self.assertNotIn("old-gen-v1", agg["climbers"])


class CandidateTests(unittest.TestCase):
    def test_slug_matches_catalog_tail(self):
        from harness.rankings import _slug_matches_model
        self.assertTrue(_slug_matches_model("deepseek-v4.1-flash",
                                            "deepseek/deepseek-v4.1-flash"))
        self.assertTrue(_slug_matches_model("gpt-5.6-luna",
                                            "openai/gpt-5.6-luna"))
        self.assertFalse(_slug_matches_model("gpt-5.6-luna",
                                             "openai/gpt-5.6-luna-mini"))

    def test_candidates_intersect_catalog_and_shipped(self):
        agg = aggregate_rankings(_rows())
        catalog = ["deepseek/deepseek-v4.1-flash", "openai/gpt-5.6-luna",
                   "vendor/old-gen-v1"]
        out = candidates_from_rankings(
            agg["totals"], catalog,
            shipped_ids=["openai/gpt-5.6-luna"])
        ids = {c["model_id"] for c in out["ranked_in_catalog"]}
        self.assertEqual(ids, {"deepseek/deepseek-v4.1-flash",
                               "openai/gpt-5.6-luna",
                               "vendor/old-gen-v1"})
        # gpt-5.6-luna is shipped coverage; old-gen-v1 is a falling new name
        # but still a proposal (traffic evidence alone does not bless it).
        proposed = {c["model_id"] for c in out["proposed"]}
        self.assertIn("vendor/old-gen-v1", proposed)
        self.assertNotIn("openai/gpt-5.6-luna", proposed)


class ProbeGateTests(unittest.TestCase):
    def _gov(self, fake):
        from harness.spend import SpendGovernor
        import os
        import tempfile
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        return SpendGovernor(fake, "sk-test", byok_prefixes_path=os.path.join(
            td.name, "byok.json"))

    def _report_gov(self, fake):
        """A governor whose live /models fetch is stubbed to a tiny catalog
        (the standard FakeTransport covers it; no per-call injection needed)."""
        import os
        import tempfile
        from harness.spend import SpendGovernor
        fake.models = [m("deepseek/deepseek-v4.1-flash"),
                       m("openai/gpt-5.6-luna")]
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        return SpendGovernor(fake, "sk-test", byok_prefixes_path=os.path.join(
            td.name, "byok.json"))

    def test_report_end_to_end_via_module_seam(self):
        """The full report with the shipped-pool default resolved through the
        module-level seam (the one hermetic patch point; no injected loader)."""
        import unittest.mock
        from harness import rankings
        fake = FakeTransport(posts=[])
        fake.get = lambda url, api_key, timeout=30: (
            {"data": _rows()} if url.endswith("rankings-daily")
            else {"data": fake.models})
        gov = self._report_gov(fake)
        shipped = ["openai/gpt-5.6-luna"]
        with unittest.mock.patch.object(rankings, "shipped_model_ids",
                                        return_value=shipped):
            report = rankings.build_rankings_report(fake, "k", gov)
        self.assertEqual(report["window"]["days"], 10)
        self.assertEqual(report["top"][0]["slug"], "gpt-5.6-luna")
        self.assertIn("deepseek-v4.1-flash", report["climbers"])
        self.assertEqual(
            {c["model_id"] for c in report["ranked_in_catalog"]},
            {"deepseek/deepseek-v4.1-flash", "openai/gpt-5.6-luna"})
        # luna is shipped coverage; the v4.1-flash seat is proposed.
        self.assertEqual([c["model_id"] for c in report["proposed_candidates"]],
                         ["deepseek/deepseek-v4.1-flash"])
        self.assertEqual(report["probed_candidates"], [])

    def test_probe_passes_on_parseable_json(self):
        fake = FakeTransport(models=[m("deepseek/deepseek-v4.1-flash")],
                             posts=[comp('{"claim_1": {"real": false}}')])
        gov = self._gov(fake)
        ok, detail, _cost = _probe_vote(fake, "k", gov,
                                        "deepseek/deepseek-v4.1-flash")
        self.assertTrue(ok)
        self.assertIn("parseable", detail)
        payload = fake.chat_posts()[0][2]
        # The one-vote probe runs the vote-lane contract: reasoning disabled.
        self.assertEqual(payload["reasoning"], {"effort": "none"})
        self.assertGreaterEqual(payload["max_tokens"], 4096)

    def test_probe_fails_on_prose(self):
        fake = FakeTransport(models=[m("x/a")], posts=[comp("some prose")])
        ok, detail, _cost = _probe_vote(fake, "k", self._gov(fake), "x/a")
        self.assertFalse(ok)
        self.assertIn("parseable", detail)

    def test_probe_fails_on_http_error(self):
        fake = FakeTransport(models=[m("x/a")],
                             posts=[(503, {"error": {"message": "down"}})])
        ok, detail, _cost = _probe_vote(fake, "k", self._gov(fake), "x/a")
        self.assertFalse(ok)
        self.assertIn("503", detail)

    def test_probe_fails_on_reasoning_only(self):
        fake = FakeTransport(models=[m("x/a")],
                             posts=[comp("", reasoning="thinking...")])
        ok, detail, _cost = _probe_vote(fake, "k", self._gov(fake), "x/a")
        self.assertFalse(ok)


class UiContractPinTests(unittest.TestCase):
    """Mechanizes the rankings envelope-to-UI field contract: every key the
    web view reads must exist on an envelope produced by rankings.py's real
    builder driven through the real endpoint assembler -- not a hand-written
    fixture. A rename of any consumed key (top[].slug/total_tokens/trend,
    ranked_in_catalog[].model_id, proposed_candidates[].probe.ok/detail,
    window.start/end/days) fails the battery instead of silently blanking
    the Rankings view (audit find: only an audit probe could catch it).

    Two layers: a key-level pin that always runs, and -- when a node runtime
    exists -- execution of the real loadRankings extracted from app.js.
    """

    _envelope = None

    @classmethod
    def _real_envelope(cls):
        if cls._envelope is not None:
            return cls._envelope
        import json as _json
        import unittest.mock
        from harness import rankings, server

        fake = FakeTransport(posts=[])
        fake.get = lambda url, api_key, timeout=30: (
            {"data": _rows()} if url.endswith("rankings-daily")
            else {"data": fake.models})
        fake.models = [m("deepseek/deepseek-v4.1-flash"),
                       m("openai/gpt-5.6-luna")]
        import os
        import tempfile
        from harness.spend import SpendGovernor
        td = tempfile.TemporaryDirectory()
        cls.addClassCleanup(td.cleanup)
        gov = SpendGovernor(fake, "sk-test", byok_prefixes_path=os.path.join(
            td.name, "byok.json"))
        with unittest.mock.patch.object(rankings, "shipped_model_ids",
                                        return_value=["openai/gpt-5.6-luna"]):
            report = rankings.build_rankings_report(fake, "k", gov)
        # Attach a real probe verdict shape (the probe.ok/detail keys the UI
        # reads) without spending: the one-vote gate's documented success
        # contract is HTTP 200 + parseable JSON (rankings._probe_vote).
        for cand in report["proposed_candidates"]:
            cand["probe"] = {"ok": True, "detail": "parseable JSON vote",
                             "cost": 0.0}
            report["probed_candidates"].append(cand)

        class _Resp:
            def __init__(self):
                self.obj = None

            def _send_json(self, obj, code=200):
                # Mirror the real serializer so the envelope is shaped like
                # what the browser parses (server._send_json).
                self.obj = _json.loads(_json.dumps(obj, indent=2))

        resp = _Resp()
        # The endpoint's own open()/json.load path must run: write the real
        # report to a real file and point _rankings_reports at it. A bare
        # filename stub makes the endpoint honestly report available=false.
        import os
        import tempfile
        report_dir = tempfile.TemporaryDirectory(prefix="ui-pin-")
        cls.addClassCleanup(report_dir.cleanup)
        report_path = os.path.join(report_dir.name, "rankings-2026-01-01.json")
        with open(report_path, "w", encoding="utf-8") as f:
            _json.dump(report, f)
        with unittest.mock.patch.object(server, "_rankings_reports",
                                        return_value=[report_path]):
            server.UiRequestHandler._api_rankings(resp)
        cls._envelope = resp.obj
        return cls._envelope

    _JS_API_MEMBERS = frozenset((
        "length", "map", "filter", "join", "includes", "slice", "split",
        "forEach", "push",
    ))

    @staticmethod
    def _loadrankings_source():
        import re
        with open("harness/ui/app.js", encoding="utf-8") as f:
            src = f.read()
        return re.search(r"(function loadRankings[\s\S]*?\n\})", src).group(1)

    def test_ui_consumed_keys_exist_on_real_envelope(self):
        """The always-on layer: every member read the real loadRankings
        makes, derived from app.js source rather than a hand-list, must
        resolve on the real envelope -- the available path through the real
        endpoint and both unavailable fallbacks. A key rename on either
        side fails here even where node is unavailable."""
        import os
        import re
        import tempfile
        import unittest.mock
        from harness import server
        env = self._real_envelope()
        fn = self._loadrankings_source()
        reads = {}
        for var, key in re.findall(r"\b(r|rep|w|t|c|p)\.([a-z_][a-z_0-9]*)",
                                   fn):
            if key not in self._JS_API_MEMBERS:
                reads.setdefault(var, set()).add(key)
        rep = env["report"]
        # The structural spine is pinned exactly: UI-side additions or
        # renames of these reads require a conscious pin update.
        self.assertEqual(reads["rep"], {"window", "top",
                                        "ranked_in_catalog",
                                        "proposed_candidates"})
        self.assertEqual(reads["r"], {"available", "report", "latest",
                                      "reports", "error", "note"})
        for key in ("available", "report", "latest", "reports"):
            self.assertIn(key, env)
        for key in reads["w"]:
            self.assertIn(key, rep["window"], f"window.{key} missing")
        for key in reads["t"]:
            self.assertIn(key, rep["top"][0], f"top[].{key} missing")
        rows = (rep["ranked_in_catalog"][0], rep["proposed_candidates"][0])
        for key in reads["c"]:
            self.assertTrue(any(key in row for row in rows),
                            f"candidate row key .{key} missing")
        for key in reads["p"]:
            self.assertIn(key, rep["proposed_candidates"][0]["probe"],
                          f"probe.{key} missing")

        class _Resp:
            obj = None

            def _send_json(self, obj, code=200):
                self.obj = obj

        # The fallback branches are real contract too, driven through the
        # real endpoint: no reports on disk, and an unreadable report file.
        resp = _Resp()
        with unittest.mock.patch.object(server, "_rankings_reports",
                                        return_value=[]):
            server.UiRequestHandler._api_rankings(resp)
        fallback = {"note": resp.obj}
        with tempfile.TemporaryDirectory() as td:
            bad = os.path.join(td, "broken.json")
            with open(bad, "w", encoding="utf-8") as f:
                f.write("{not json")
            resp2 = _Resp()
            with unittest.mock.patch.object(server, "_rankings_reports",
                                            return_value=[bad]):
                server.UiRequestHandler._api_rankings(resp2)
        fallback["error"] = resp2.obj
        for key in ({"error", "note"} & reads["r"]):
            self.assertIn(key, fallback[key],
                          f"fallback envelope missing .{key}")

    def test_real_loadrankings_renders_real_envelope(self):
        """The proof layer where node exists: the real loadRankings source,
        extracted from app.js, executed against the real envelope, with
        per-row value co-occurrence -- each rendered row must contain its
        own envelope row's values, so a renamed key on either side blanks
        exactly one cell and fails even when the same value appears
        elsewhere on the page (the global-substring blind spot).
        Vacuity-proven by planted renames during development."""
        import json as _json
        import re
        import shutil
        import subprocess
        if shutil.which("node") is None:
            self.skipTest("node runtime not installed (optional deps: JS "
                          "render layer; the key-contract pin still runs)")
        env = self._real_envelope()
        with open("harness/ui/app.js", encoding="utf-8") as f:
            app = f.read()
        fn = re.search(r"(function loadRankings[\s\S]*?\n\})", app).group(1)
        esc = re.search(
            r"(const esc = [^\n]+\n  \(c\) => \([^\n]+\);)", app).group(1)
        script = (
            esc + "\n"
            "const html = {};\n"
            'const $ = (id) => ({ set innerHTML(v) { html[id] = v; } });\n'
            'const api = async () => globalThis.__resp;\n'
            "globalThis.__resp = " + _json.dumps(env) + ";\n"
            "const loadRankings = async " + fn + ";\n"
            "await loadRankings();\n"
            "const out = html['#rankings-out'] || '';\n"
            "const seg = (from, to) => out.slice(out.indexOf(from),\n"
            "  to ? out.indexOf(to) : undefined);\n"
            "const rows = (h) => h.split('<tr>').slice(1)\n"
            "  .filter((s) => s.includes('<td>')).map((s) => s.split('</tr>')[0]);\n"
            "const rep = globalThis.__resp.report;\n"
            "const fail = [];\n"
            "const check = (where, row, vals) => vals.forEach((v) => {\n"
            "  if (!row.includes(String(v))) fail.push(where + ' lost value: ' + v);\n"
            "});\n"
            "rep.top.forEach((t, i) => check('top[' + i + ']',\n"
            "  rows(seg('Top by traffic', 'Ranked'))[i] || '',\n"
            "  [t.slug, String(t.total_tokens), t.trend]));\n"
            "rep.ranked_in_catalog.forEach((c, i) => check('ranked[' + i + ']',\n"
            "  rows(seg('Ranked ∩ live catalog', 'Proposed candidates'))[i] || '',\n"
            "  [c.slug, c.model_id, String(c.total_tokens), c.trend]));\n"
            "rep.proposed_candidates.forEach((c, i) => check('proposed[' + i + ']',\n"
            "  rows(seg('Proposed candidates', ''))[i] || '',\n"
            "  [c.model_id, String(c.total_tokens),\n"
            "   (c.probe && c.probe.ok ? 'pass (' : 'fail — ') + c.probe.detail + ')']));\n"
            "const head = out.slice(0, out.indexOf('<h2>'));\n"
            "check('window', head,\n"
            "  [rep.window.start, rep.window.end, String(rep.window.days)]);\n"
            "if (fail.length) {\n"
            "  console.log('CONTRACT FAIL: ' + fail.join(' | '));\n"
            "  process.exit(1);\n"
            "}\n"
            "console.log('UI-CONTRACT OK: per-row co-occurrence satisfied');\n"
        )
        r = subprocess.run(["node", "--input-type=module", "-e", script],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(
            r.returncode, 0,
            "real loadRankings failed on the real envelope: "
            + (r.stdout or "") + (r.stderr or ""))
        self.assertIn("UI-CONTRACT OK", r.stdout)


if __name__ == "__main__":
    unittest.main()
