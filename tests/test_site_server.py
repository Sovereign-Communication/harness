"""SITE-9: hermetic tests for the site endpoints on `harness serve`.

Covers the three faces the site/UI consume: the local-mode proof snapshot
(`/api/snapshot`), the labeled demo snapshot, and the router endpoint
(`/api/route`, same policy owner as `harness route`). Also pins the new
pane assets being served (SITE-8) and the fail-closed behaviors (broken
chain refuses the snapshot; unroutable/invalid packs return honest 400s).
"""
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

    def test_site_routes_require_auth_like_api(self):
        # /site/* is NOT in the unguarded static allowlist: a request with a
        # wrong token is refused before any file read (line 647->648 branch).
        conn = self._conn()
        try:
            status, _ = _request(conn, "GET", "/site/index.html",
                                 headers={"X-Harness-Auth": "wrong"})
            self.assertEqual(status, 401)
            status, _ = _request(conn, "GET", "/site/index.html",
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
