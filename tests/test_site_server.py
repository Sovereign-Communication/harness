"""SITE-9: hermetic tests for the site endpoints on `harness serve`.

Covers the three faces the site/UI consume: the local-mode proof snapshot
(`/api/snapshot`), the labeled demo snapshot, and the router endpoint
(`/api/route`, same policy owner as `harness route`). Also pins the new
pane assets being served (SITE-8) and the fail-closed behaviors (broken
chain refuses the snapshot; unroutable/invalid packs return honest 400s).
"""
import unittest
from unittest import mock

from harness.config import load_settings

from tests.test_server import ServerHarness, _request


def _unkeyed_settings():
    """Real settings with the Jev key stripped: the route endpoint then
    answers via the code-owned heuristic (is_fallback=True) with zero
    network — the tests stay hermetic on keyed operator machines."""
    settings = load_settings()
    settings.jev_api_key = None
    return settings


class SiteGuardTests(ServerHarness):
    token = "guard-token-1"

    def test_site_static_does_not_require_auth_header(self):
        # DF-SITE-1: /site/* static assets carry no secrets and must be reachable
        # in a standard browser without custom X-Harness-Auth headers, even when
        # HARNESS_UI_AUTH_TOKEN is configured.
        conn = self._conn()
        try:
            status, data = _request(conn, "GET", "/site/index.html")
            self.assertEqual(status, 200)
            self.assertIn("Proof Bench", data.get("raw", ""))
            # Wrong or absent token still serves static assets cleanly
            status, data = _request(conn, "GET", "/site/index.html",
                                 headers={"X-Harness-Auth": "wrong"})
            self.assertEqual(status, 200)
            # Rebinding protection still guards host
            status, _ = _request(conn, "GET", "/site/index.html",
                                 headers={"Host": "attacker.com"})
            self.assertEqual(status, 403)
        finally:
            conn.close()

    def test_api_routes_still_require_auth_when_token_set(self):
        # JSON API routes remain strictly guarded by X-Harness-Auth token
        conn = self._conn()
        try:
            status, _ = _request(conn, "GET", "/api/snapshot",
                                 headers={"X-Harness-Auth": "wrong"})
            self.assertEqual(status, 401)
            status, _ = _request(conn, "GET", "/api/snapshot",
                                 headers={"X-Harness-Auth": self.token})
            self.assertEqual(status, 200)
        finally:
            conn.close()


class SiteStaticAssetsTests(ServerHarness):
    def test_pane_assets_served(self):
        for path, marker in (("/panes.js", "pane-tabs"),
                             ("/panes.css", "pane-tab")):
            conn = self._conn()
            try:
                status, data = _request(conn, "GET", path)
                self.assertEqual(status, 200)
                self.assertIn(marker, data.get("raw", ""))
            finally:
                conn.close()


class SitePageServingTests(ServerHarness):
    """SITE local mode: `harness serve` is the single local entrypoint for
    the legacy UI AND the Proof Bench pages (under /site/). Traversal and
    unknown extensions are refused, not guessed."""

    def test_site_index_served(self):
        conn = self._conn()
        try:
            status, data = _request(conn, "GET", "/site/index.html")
            self.assertEqual(status, 200)
            self.assertIn("Proof Bench", data.get("raw", ""))
        finally:
            conn.close()

    def test_site_directory_index_resolves(self):
        """Pretty URLs: /site/ and /site/<page>[/] serve that page's
        index.html instead of dead-ending on `no such site file`."""
        for path in ("/site/", "/site/methodology", "/site/methodology/"):
            conn = self._conn()
            try:
                status, data = _request(conn, "GET", path)
                self.assertEqual(status, 200, path)
                self.assertIn("Proof Bench", data.get("raw", ""), path)
            finally:
                conn.close()

    def test_site_directory_index_still_refuses_misses(self):
        """A directory without an index.html (or a missing directory) stays
        an honest 404 -- the index fallback never guesses."""
        for path in ("/site/assets/", "/site/nope/"):
            conn = self._conn()
            try:
                status, _ = _request(conn, "GET", path)
                self.assertEqual(status, 404, path)
            finally:
                conn.close()

    def test_site_local_mode_never_guesses_serve_port(self):
        """Regression: app.js once gated local mode on port 8787 -- a port
        `harness serve` never serves (README default 8765, desktop 8766) --
        so under the documented local entrypoint the site never tried
        /api/snapshot and rendered 'No snapshot available yet' forever.
        Mode is now an explicit ?mode=public opt-out, never a port guess."""
        with open("site/public/assets/app.js", encoding="utf-8") as handle:
            src = handle.read()
        self.assertNotIn("8787", src)
        self.assertNotIn("location.port", src)
        self.assertNotIn("location.hostname", src)
        self.assertIn("mode=public", src)
        self.assertIn("/api/snapshot", src)

    def test_site_asset_served(self):
        conn = self._conn()
        try:
            status, data = _request(conn, "GET", "/site/assets/app.js")
            self.assertEqual(status, 200)
            self.assertIn("renderTraces", data.get("raw", ""))
        finally:
            conn.close()

    def test_site_traversal_refused(self):
        for evil in ("/site/../harness/server.py", "/site/..%2Fserver.py",
                     "/site/....//server.py", "/site/\\..\\server.py"):
            conn = self._conn()
            try:
                status, _ = _request(conn, "GET", evil)
                self.assertEqual(status, 404, evil)
            finally:
                conn.close()

    def test_site_prefix_stripping_cannot_escape(self):
        # A path whose stripped remainder is absolute (drive letter) or
        # normalizes outside SITE_ROOT is refused by the isabs / prefix
        # guards, independent of the '..' check.
        conn = self._conn()
        try:
            for evil in ("/site/C:/windows/win.ini", "/site//" + "a/" * 30 + "x.html"):
                status, _ = _request(conn, "GET", evil)
                self.assertEqual(status, 404, evil)
        finally:
            conn.close()

    def test_site_unknown_extension_refused(self):
        conn = self._conn()
        try:
            status, _ = _request(conn, "GET", "/site/data/demo/.gitignore")
            self.assertEqual(status, 404)
        finally:
            conn.close()

    def test_site_missing_known_type_refused(self):
        # A known extension at a nonexistent path hits the OSError branch
        # (a plain 404, never a traceback or a fabricated body).
        for path in ("/site/assets/missing.js", "/site/nope/index.html"):
            conn = self._conn()
            try:
                status, _ = _request(conn, "GET", path)
                self.assertEqual(status, 404, path)
            finally:
                conn.close()

    def test_site_index_redirect_free_paths_stay_404(self):
        # Without the /site/ prefix these are legacy-UI routes, never the
        # site pages (the two surfaces stay distinct).
        conn = self._conn()
        try:
            status, _ = _request(conn, "GET", "/tiers/index.html")
            self.assertEqual(status, 404)
        finally:
            conn.close()


class DemoSnapshotTests(ServerHarness):
    def test_demo_snapshot_is_labeled_demo(self):
        conn = self._conn()
        try:
            status, data = _request(conn, "GET", "/api/site/demo-snapshot")
            self.assertEqual(status, 200)
            self.assertTrue(data.get("demo"))
            self.assertEqual(data.get("schema"), "site-snapshot-v1")
            self.assertIn("demo_note", data)
        finally:
            conn.close()


class SiteSnapshotTests(ServerHarness):
    def test_snapshot_contract(self):
        conn = self._conn()
        try:
            status, data = _request(conn, "GET", "/api/snapshot")
            self.assertEqual(status, 200)
            self.assertEqual(data.get("schema"), "site-snapshot-v1")
            self.assertEqual(data.get("contributors"), 1)
            session = (data.get("sessions") or [{}])[0]
            self.assertIn("metrics", session)
            metrics = session["metrics"]
            for key in ("runs", "gated_runs", "cost_per_gated_task",
                        "run_depth_distribution", "frontier_warrant_rate",
                        "hourglass_savings", "escalation_escape_rate"):
                self.assertIn(key, metrics)
        finally:
            conn.close()


class RouteEndpointTests(ServerHarness):
    PACK = {
        "id": "test-ladder",
        "rungs": [
            {"rung_id": "scout", "tier": "T0", "model": "helper-lite",
             "cost_class": "free", "guidance": ["typo", "rename", "docstring"]},
            {"rung_id": "worker", "tier": "T1", "model": "worker-1",
             "cost_class": "cheap", "guidance": ["algorithm", "parse"]},
            {"rung_id": "scout3", "tier": "T3", "model": "frontier-1",
             "cost_class": "premium", "guidance": ["proof", "architecture"]},
        ],
    }

    def _post(self, body):
        conn = self._conn()
        try:
            with mock.patch("harness.server.load_settings",
                            return_value=_unkeyed_settings()), \
                 mock.patch("harness.server.governor_for",
                            return_value=(None, None)):
                return _request(conn, "POST", "/api/route", body=body)
        finally:
            conn.close()

    def test_route_ok(self):
        status, data = self._post({
            "goal": "fix a typo in the docstring",
            "pack": self.PACK,
        })
        self.assertEqual(status, 200)
        self.assertEqual(data.get("status"), "ok")
        route = data.get("route") or {}
        self.assertEqual(route.get("tier"), "T0")
        self.assertEqual(route.get("rung_id"), "scout")
        self.assertTrue(data.get("is_fallback"),
                        "unkeyed route must answer via the heuristic fallback")

    def test_route_requires_goal_and_pack(self):
        for body in ({}, {"goal": "x"}, {"pack": self.PACK}):
            status, data = self._post(body)
            self.assertEqual(status, 400, body)
            self.assertIn("error", data)

    def test_route_rejects_invalid_pack(self):
        status, data = self._post({
            "goal": "anything", "pack": {"pack_id": "x"}})
        self.assertEqual(status, 400)
        self.assertIn("pack invalid", data.get("error", ""))

    def test_route_unmatched_goal_honest_fallback(self):
        status, data = self._post({
            "goal": "zzzqqq unrelated verbage",
            "pack": {"id": "narrow", "rungs": [
                {"rung_id": "only", "tier": "T0", "model": "m",
                 "cost_class": "free", "guidance": ["typo"]}]},
        })
        self.assertEqual(status, 200)
        self.assertIn(data.get("status"), ("ok", "unroutable"))
        self.assertTrue(data.get("is_fallback"),
                        "unkeyed route must never present itself as a live Jev answer")


class SiteAppJsElTests(unittest.TestCase):
    """Real-JS behavioral pin (node when present, skipped cleanly when not):
    el() must append child ARRAYS as nodes. The dogfood regression -- barChart
    and the router reasons list pass arrays; Element.append() stringifies an
    array to "[object HTMLDivElement],..." so Proof Bench charts silently
    rendered garbage. Identity checks make the pin vacuity-proof: a
    stringifying el() leaves strings in kids and fails."""

    def _run_app(self, body):
        import os
        import shutil
        import subprocess
        import tempfile
        from pathlib import Path
        node = shutil.which("node")
        if node is None:
            self.skipTest("platform: node not available; JS behavior asserted in CI")
        uri = Path("site/public/assets/app.js").resolve().as_uri()
        script = (
            "globalThis.location = { search: '' };\n"
            "const mk = (tag) => ({ tag, kids: [],\n"
            "  append(...xs) { this.kids.push(...xs); },\n"
            "  setAttribute() {}, addEventListener() {} });\n"
            "globalThis.document = { createElement: mk };\n"
            "const { el } = await import('" + uri + "');\n" + body)
        with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False) as f:
            f.write(script)
            path = f.name
        try:
            proc = subprocess.run([node, path], capture_output=True, text=True,
                                  timeout=30)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        self.assertEqual(proc.returncode, 0, proc.stderr[-300:])
        return proc.stdout

    def test_el_appends_child_arrays_as_nodes(self):
        import json
        out = self._run_app(
            "const a = mk('li'), b = mk('li');\n"
            "const ul = el('ul', {}, [a, b]);\n"
            "const c = mk('span'), d = mk('span');\n"
            "const div = el('div', {}, [[c], [d]]);\n"
            "process.stdout.write(JSON.stringify({\n"
            "  ul: ul.kids.length, div: div.kids.length,\n"
            "  same: ul.kids[0] === a && ul.kids[1] === b\n"
            "    && div.kids[0] === c && div.kids[1] === d,\n"
            "  nodesOnly: [...ul.kids, ...div.kids]\n"
            "    .every((k) => typeof k === 'object' && k !== null),\n"
            "}));\n")
        data = json.loads(out)
        self.assertEqual(data["ul"], 2, "flat child array must append both nodes")
        self.assertEqual(data["div"], 2, "nested child arrays must flatten")
        self.assertTrue(data["same"], "kids must be the node objects, not strings")
        self.assertTrue(data["nodesOnly"])

    def test_el_still_skips_null_children_in_arrays(self):
        import json
        out = self._run_app(
            "const a = mk('li');\n"
            "const ul = el('ul', {}, [null, a, undefined]);\n"
            "process.stdout.write(JSON.stringify({ n: ul.kids.length }));\n")
        self.assertEqual(json.loads(out)["n"], 1)
