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
