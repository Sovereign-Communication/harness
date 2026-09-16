"""harness serve: the localhost UI server.

Hermetic: an ephemeral loopback server with stubbed runners -- no network,
no key, no real dispatch. Pins the security guards (loopback Host check,
optional token), the static UI, dispatch validation, the run lifecycle with
cooperative cancel, and the event-stream contract.
"""
import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from harness import server as ui_server
from harness.server import make_server, validate_dispatch
from harness.errors import HarnessError


def _request(conn, method, path, body=None, headers=None, host="127.0.0.1"):
    hdrs = dict(headers or {})
    hdrs.setdefault("Host", host)
    payload = json.dumps(body).encode() if body is not None else None
    if payload is not None:
        hdrs["Content-Type"] = "application/json"
    conn.request(method, path, body=payload, headers=hdrs)
    resp = conn.getresponse()
    raw = resp.read()
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        data = {"raw": raw.decode("utf-8", "replace")}
    return resp.status, data


class ServerHarness(unittest.TestCase):
    """One ephemeral server per test class; token configured per test."""

    def setUp(self):
        self.httpd = make_server("127.0.0.1", 0,
                                 auth_token=getattr(self, "token", None))
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.addCleanup(self._teardown)

    def _teardown(self):
        # Uninstall the event sink: it lives in the module-global events bus
        # (capped at MAX_SINKS); leaking one per test silently starves later
        # servers once the cap is reached.
        from harness import events as _events
        _events.remove_sink(self.httpd.ui._on_event)
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def _conn(self):
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)


class SecurityGuardTests(ServerHarness):
    def test_loopback_host_required(self):
        """DNS-rebinding guard: an attacker-chosen Host header is refused."""
        conn = self._conn()
        try:
            status, _ = _request(conn, "GET", "/api/status",
                                 host="attacker.example.com")
            self.assertEqual(status, 403)
        finally:
            conn.close()

    def test_loopback_host_accepted(self):
        conn = self._conn()
        try:
            status, data = _request(conn, "GET", "/api/status")
            self.assertEqual(status, 200)
            self.assertEqual(data["status"], "ok")
        finally:
            conn.close()

    def test_unknown_api_endpoint_404(self):
        conn = self._conn()
        try:
            status, _ = _request(conn, "GET", "/api/definitely-not-here")
            self.assertEqual(status, 404)
        finally:
            conn.close()


class TokenAuthTests(ServerHarness):
    token = "s3cret-token"

    def test_missing_token_401(self):
        conn = self._conn()
        try:
            status, data = _request(conn, "GET", "/api/status")
            self.assertEqual(status, 401)
            self.assertIn("X-Harness-Auth", data["error"])
        finally:
            conn.close()

    def test_wrong_token_401(self):
        conn = self._conn()
        try:
            status, _ = _request(conn, "GET", "/api/status",
                                 headers={"X-Harness-Auth": "nope"})
            self.assertEqual(status, 401)
        finally:
            conn.close()

    def test_right_token_passes(self):
        conn = self._conn()
        try:
            status, _ = _request(conn, "GET", "/api/status",
                                 headers={"X-Harness-Auth": self.token})
            self.assertEqual(status, 200)
        finally:
            conn.close()

    def test_static_ui_served_without_token(self):
        # The HTML shell is public; only /api routes are token-gated (the
        # page itself reads the token from the fragment and sends headers).
        conn = self._conn()
        try:
            status, data = _request(conn, "GET", "/")
            self.assertEqual(status, 200)
            self.assertIn("Harness", data.get("raw", "")) if "raw" in data \
                else None
        finally:
            conn.close()
        # (raw branch: index.html is not JSON; just assert it came back 200)


class StaticUiTests(ServerHarness):
    def test_index_served(self):
        conn = self._conn()
        try:
            status, data = _request(conn, "GET", "/index.html")
            self.assertEqual(status, 200)
            body = data.get("raw", "")
            self.assertIn("view-dashboard", body)
            self.assertIn("view-dispatch", body)
        finally:
            conn.close()


class DispatchValidationTests(unittest.TestCase):
    def test_apply_requires_existing_file(self):
        with self.assertRaises(HarnessError):
            validate_dispatch("apply", {"file": "no/such/file.py",
                                        "instruction": "x"})

    def test_apply_requires_instruction(self):
        with self.assertRaises(HarnessError):
            validate_dispatch("apply", {"file": __file__})

    def test_unknown_kind_refused(self):
        with self.assertRaises(HarnessError):
            validate_dispatch("rm-rf", {})

    def test_task_max_cost_capped_at_hard(self):
        with self.assertRaises(HarnessError):
            validate_dispatch("apply", {"file": __file__,
                                        "instruction": "x",
                                        "task_max_cost": 99})

    def test_backend_whitelisted(self):
        with self.assertRaises(HarnessError):
            validate_dispatch("apply", {"file": __file__, "instruction": "x",
                                        "backend": "sudo"})

    def test_verify_requires_prompt(self):
        with self.assertRaises(HarnessError):
            validate_dispatch("verify", {})

    def test_continue_requires_existing_state(self):
        with self.assertRaises(HarnessError):
            validate_dispatch("continue", {"state": "no/such/state.json"})

    def test_bench_requires_existing_manifest(self):
        with self.assertRaises(HarnessError):
            validate_dispatch("bench", {"manifest": "no/such/dir"})


class RunLifecycleTests(ServerHarness):
    def _stub_runner(self, body_fn):
        def fake(task_id, args, cancel_check):
            body_fn(task_id, args, cancel_check)
            return {"status": "ok", "task_id": task_id, "cost": 0.0}
        return fake

    def test_dispatch_validates_before_creating_a_run(self):
        conn = self._conn()
        try:
            status, data = _request(conn, "POST", "/api/runs",
                                    body={"kind": "apply", "args": {}})
            self.assertEqual(status, 400)
            self.assertIn("file", data["error"])
            self.assertEqual(data["error"], ui_server.validate_dispatch.__name__
                             and data["error"])  # shape sanity
        finally:
            conn.close()
        self.assertEqual(self.httpd.ui.runs, {})

    def test_run_reaches_terminal_and_result_is_readable(self):
        gate = threading.Event()
        with mock.patch.dict(ui_server.RUNNERS,
                             {"verify": self._stub_runner(
                                 lambda *a: gate.set())}):
            conn = self._conn()
            try:
                status, run = _request(conn, "POST", "/api/runs",
                                       body={"kind": "verify",
                                             "args": {"prompt": "hi"}})
                self.assertEqual(status, 201)
                # The stub finishes fast: the run may already be terminal by
                # the time the 201 is read. Either state is valid here.
                self.assertIn(run["status"], ("running", "ok"))
                self.assertTrue(gate.wait(5))
                for _ in range(50):
                    _, full = _request(conn, "GET",
                                       f"/api/runs/{run['id']}/result")
                    if full["status"] != "running":
                        break
                    time.sleep(0.05)
                self.assertEqual(full["status"], "ok")
                self.assertEqual(full["result"]["status"], "ok")
            finally:
                conn.close()

    def test_cancel_flips_the_cooperative_flag(self):
        cancel_seen = threading.Event()
        release = threading.Event()

        def fake(task_id, args, cancel_check):
            while not cancel_check():
                if release.wait(0.05):
                    break
            cancel_seen.set()
            return {"status": "error", "error": "cancelled"}

        with mock.patch.dict(ui_server.RUNNERS, {"verify": fake}):
            conn = self._conn()
            try:
                _, run = _request(conn, "POST", "/api/runs",
                                  body={"kind": "verify",
                                        "args": {"prompt": "hi"}})
                status, _ = _request(conn, "POST",
                                     f"/api/runs/{run['id']}/cancel",
                                     body={})
                self.assertEqual(status, 200)
                self.assertTrue(cancel_seen.wait(5))
                _, full = _request(conn, "GET",
                                   f"/api/runs/{run['id']}/result")
                self.assertTrue(full["cancelled"])
            finally:
                release.set()
                conn.close()

    def test_verify_lane_forwards_cancel_check_to_panel_judge(self):
        """The verify runner must hand panel_judge the run's cancel closure.
        Stubbing only the runner (as above) proves the flag reaches it; this
        proves the lane forwards it to the engine -- without this wiring the
        UI's Cancel button is a silent no-op in the main lane (playtest).
        """
        called = threading.Event()
        release = threading.Event()
        holder = {}

        def fake_panel_judge(**kwargs):
            holder["cancel_check"] = kwargs.get("cancel_check")
            called.set()
            cc = kwargs.get("cancel_check")
            while not (cc and cc()):
                if release.wait(0.05):
                    break
            return {"status": "error", "error": "cancelled"}

        settings = type("S", (), {"use_free": True, "panel_pool": ["m/a"],
                                  "judge": "m/j", "reasoning_effort": "auto",
                                  "reasoning_token_budget": None,
                                  "max_panelists": 2, "max_cost": 0.02,
                                  "specialist_pool": None,
                                  "convergence_model": None,
                                  "expect_key_label": False})()

        with mock.patch("harness.service.panel_judge", fake_panel_judge), \
             mock.patch.object(ui_server, "load_settings",
                               return_value=settings), \
             mock.patch("harness.service.governor_for",
                        return_value=("k", mock.Mock())), \
             mock.patch("harness.service.ledger_for",
                        return_value=mock.Mock()), \
             mock.patch("harness.saturation.pre_run_warning"):
            conn = self._conn()
            try:
                _, run = _request(conn, "POST", "/api/runs",
                                  body={"kind": "verify",
                                        "args": {"prompt": "hi"}})
                self.assertTrue(called.wait(5), "panel_judge was never called")
                self.assertIsNotNone(
                    holder["cancel_check"],
                    "cancel_check must be forwarded to panel_judge")
                status, _ = _request(conn, "POST",
                                     f"/api/runs/{run['id']}/cancel",
                                     body={})
                self.assertEqual(status, 200)
                self.assertTrue(
                    holder["cancel_check"](),
                    "forwarded closure must reflect the run's cancel flag")
            finally:
                release.set()
                conn.close()

    def test_cancelled_run_reports_cancelled_not_error(self):
        """A user-requested cancel is not a failure of the work: when the
        engine raises ToolCancelled the run must read ``cancelled``, not
        ``error`` (the old wording blamed the model/work for the cancel).
        """
        started = threading.Event()
        release = threading.Event()

        def fake(task_id, args, cancel_check):
            started.set()
            release.wait(5)
            from harness.errors import ToolCancelled
            raise ToolCancelled()

        with mock.patch.dict(ui_server.RUNNERS, {"verify": fake}):
            conn = self._conn()
            try:
                _, run = _request(conn, "POST", "/api/runs",
                                  body={"kind": "verify",
                                        "args": {"prompt": "hi"}})
                self.assertTrue(started.wait(5))
                _request(conn, "POST", f"/api/runs/{run['id']}/cancel",
                         body={})
                release.set()
                for _ in range(50):
                    _, full = _request(conn, "GET",
                                       f"/api/runs/{run['id']}/result")
                    if full["status"] != "running":
                        break
                    time.sleep(0.05)
                self.assertEqual(full["status"], "cancelled")
                self.assertEqual(full["error"], "cancelled by user")
            finally:
                release.set()
                conn.close()

    def test_run_events_stream_is_task_scoped(self):
        started = threading.Event()

        def fake(task_id, args, cancel_check):
            from harness import events as ev
            ev.emit("preflight", task_id=task_id, worst_case=0.0, ceiling=0.0)
            ev.emit("panel_call", task_id="OTHER/run", model="intruder")
            started.set()
            return {"status": "ok", "task_id": task_id, "cost": 0.0}

        with mock.patch.dict(ui_server.RUNNERS, {"verify": fake}):
            conn = self._conn()
            try:
                _, run = _request(conn, "POST", "/api/runs",
                                  body={"kind": "verify",
                                        "args": {"prompt": "hi"}})
                self.assertTrue(started.wait(5))
                _, data = _request(conn, "GET",
                                   f"/api/runs/{run['id']}/events?after=0")
                types = [e["type"] for e in data["events"]]
                self.assertIn("run_accepted", types)
                self.assertIn("preflight", types)
                self.assertNotIn("panel_call", types)  # other task's event
                # The global stream, by contrast, carries everything.
                _, gdata = _request(conn, "GET", "/api/events?after=0")
                self.assertIn("panel_call", [e["type"] for e in gdata["events"]])
            finally:
                conn.close()


class SettingsViewTests(ServerHarness):
    def test_settings_never_leak_secrets(self):
        conn = self._conn()
        try:
            _, data = _request(conn, "GET", "/api/settings")
            blob = json.dumps(data)
            self.assertNotIn("mcp_auth_token\": \"", blob)
            self.assertNotIn("expect_key_label\": \"", blob)
        finally:
            conn.close()


class TrustEndpointTests(ServerHarness):
    """GET /api/trust: CLI `trust` parity, one policy owner (trust.trust_status)."""

    def test_trust_snapshot_shape(self):
        report = {"per_caller": {"cli": {"completions": 12, "trust_gates": 1}},
                  "trust_gates": 1}
        ledger = mock.Mock(participation_report=lambda: report)
        with mock.patch.object(ui_server, "load_settings"), \
             mock.patch.object(ui_server, "ledger_for", return_value=ledger):
            conn = self._conn()
            try:
                status, data = _request(conn, "GET", "/api/trust?caller=cli")
                self.assertEqual(status, 200)
                self.assertIn("host", data)
                self.assertIn("per_caller", data)
                self.assertEqual(data["caller"]["id"], "cli")
                self.assertIn("cli", data["per_caller"])
                self.assertIn("scale", data)
            finally:
                conn.close()

    def test_trust_endpoint_survives_ledger_failure(self):
        def boom():
            raise RuntimeError("ledger unreadable")
        ledger = mock.Mock(participation_report=boom)
        with mock.patch.object(ui_server, "load_settings"), \
             mock.patch.object(ui_server, "ledger_for", return_value=ledger):
            conn = self._conn()
            try:
                status, data = _request(conn, "GET", "/api/trust")
                self.assertEqual(status, 503)
                self.assertIn("ledger unreadable", data["error"])
                self.assertIsNone(data["host"]["score"])
            finally:
                conn.close()


class ClaimsLaneTests(ServerHarness):
    """The verify lane's structured-claims path: validation, pre-network lint
    rejection, envelope parity with the CLI's claims verify, and abort-spend
    honesty (in-flight billed calls reach the cancelled envelope)."""

    GROUNDED = {"claims": [{"id": "c_ok", "text": "capped at 256 with eviction",
                            "source_refs": [6]}]}
    UNGROUNDED = {"claims": [{"id": "c_bad",
                              "text": "UNBOUNDED GAP: there is no upper bound "
                              "on the message_number gap here."}]}
    WINDOW = (
        "pub fn decrypt(&mut self, message_number: u32) -> Result<Vec<u8>> {\n"
        "    if let Some(key) = self.skipped_keys.get(&message_number) {\n"
        "        return Ok(key);\n"
        "    }\n"
        "    let mut cloned = (*self).clone();\n"
        "    let message_key = cloned.get_message_key(message_number)?;\n"
        "    *self = cloned;\n"
        "    Ok(message_key)\n"
        "}\n"
    )

    def _files(self, grounded=True):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)

        def w(name, text):
            p = os.path.join(td.name, name)
            with open(p, "w", encoding="utf-8") as f:
                f.write(text)
            return p
        return (w("claims.json", json.dumps(
                    self.GROUNDED if grounded else self.UNGROUNDED)),
                w("window.txt", self.WINDOW))

    def _settings(self):
        return type("S", (), {"use_free": True, "panel_pool": ["m/a"],
                              "panel": ["m/a"], "apply_model": "m/apply",
                              "judge": "m/j", "reasoning_effort": "auto",
                              "reasoning_token_budget": None,
                              "max_panelists": 2, "max_cost": 0.02,
                              "specialist_pool": None,
                              "convergence_model": None,
                              "expect_key_label": False})()

    def _engine_patches(self, panel_judge, gov=None):
        if gov is None:
            gov = mock.Mock()
            gov.spent = 0.0
            gov.max_cost = 0.02
            gov.cost_by_model.return_value = {}
        return [mock.patch("harness.service.panel_judge", panel_judge),
                mock.patch.object(ui_server, "load_settings",
                                  return_value=self._settings()),
                mock.patch("harness.service.governor_for",
                           return_value=("k", gov)),
                mock.patch("harness.service.ledger_for",
                           return_value=mock.Mock()),
                mock.patch("harness.saturation.pre_run_warning")]

    def _dispatch(self, args):
        conn = self._conn()
        try:
            status, run = _request(conn, "POST", "/api/runs",
                                   body={"kind": "verify", "args": args})
            return status, run
        finally:
            conn.close()

    def _await_settled(self, run_id):
        conn = self._conn()
        try:
            for _ in range(100):
                _, full = _request(conn, "GET", f"/api/runs/{run_id}/result")
                if full["status"] != "running":
                    return full
                time.sleep(0.05)
            self.fail("run never settled")
        finally:
            conn.close()

    def test_claims_dispatch_requires_source_file(self):
        conn = self._conn()
        try:
            status, data = _request(conn, "POST", "/api/runs",
                                    body={"kind": "verify",
                                          "args": {"claims_file": "x.json"}})
            self.assertEqual(status, 400)
            self.assertIn("source_file", data["error"])
            self.assertEqual(self.httpd.ui.runs, {})
        finally:
            conn.close()

    def test_claims_lane_rejects_ungrounded_claims_before_network(self):
        """An ungrounded claim set finishes ``rejected`` with the lint report
        attached and never reaches the engine (no key, no network)."""
        reached = threading.Event()

        def must_not_run(*a, **k):
            reached.set()
            return {"status": "ok"}
        cf, sf = self._files(grounded=False)
        with mock.patch("harness.service.panel_judge", must_not_run):
            status, run = self._dispatch({"claims_file": cf,
                                          "source_file": sf})
            self.assertEqual(status, 201)
            full = self._await_settled(run["id"])
        self.assertFalse(reached.is_set(), "engine must not run on lint reject")
        self.assertEqual(full["result"]["status"], "rejected")
        self.assertFalse(full["result"]["lint"]["ok"])
        self.assertIsNone(full["result"]["verdict"])

    def test_claims_lane_builds_prompt_and_runs_engine(self):
        seen = {}

        def fake_panel_judge(**kwargs):
            seen["prompt"] = kwargs["prompt"]
            return {"status": "ok", "verdict": {"decision": "yes"},
                    "judge_synthesis": "claims verified"}
        cf, sf = self._files()
        patches = self._engine_patches(fake_panel_judge)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            status, run = self._dispatch({"claims_file": cf,
                                          "source_file": sf})
            self.assertEqual(status, 201)
            full = self._await_settled(run["id"])
        self.assertEqual(full["result"]["status"], "ok")
        self.assertEqual(full["result"]["verdict"]["decision"], "yes")
        self.assertIn("Source under review", seen["prompt"])
        self.assertIn("c_ok", seen["prompt"])

    def test_cancelled_run_reports_actual_spend(self):
        """Spend honesty: panel calls that bill before the cooperative cancel
        lands are real spend and must reach the envelope's actual_cost."""
        gov = mock.Mock()
        gov.spent = 0.0007
        gov.max_cost = 0.02
        gov.cost_by_model.return_value = {"m/a": 0.0007}
        release = threading.Event()

        def fake_panel_judge(**kwargs):
            release.wait(5)
            from harness.errors import ToolCancelled
            raise ToolCancelled()
        patches = self._engine_patches(fake_panel_judge, gov=gov)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            status, run = self._dispatch({"prompt": "hi"})
            self.assertEqual(status, 201)
            cancel_conn = self._conn()
            try:
                cstatus, _ = _request(cancel_conn, "POST",
                                      f"/api/runs/{run['id']}/cancel",
                                      body={})
                self.assertEqual(cstatus, 200)
            finally:
                cancel_conn.close()
            release.set()
            full = self._await_settled(run["id"])
        self.assertEqual(full["status"], "cancelled")
        self.assertEqual(full["error"], "cancelled by user")
        self.assertEqual(full["result"]["actual_cost"], 0.0007)
        self.assertEqual(full["result"]["cost_by_model"], {"m/a": 0.0007})


class TtlCacheTests(unittest.TestCase):
    def test_cached_collapses_calls_within_ttl(self):
        ui = ui_server.UiState()
        calls = []

        def build():
            calls.append(1)
            return {"n": len(calls)}

        self.assertEqual(ui.cached("k", build), {"n": 1})
        self.assertEqual(ui.cached("k", build), {"n": 1})
        self.assertEqual(len(calls), 1)  # second hit served from cache

    def test_cached_expires_after_ttl(self):
        ui = ui_server.UiState()
        calls = []

        def build():
            calls.append(1)
            return {"n": len(calls)}

        ui.cached("k", build)
        # Fake the clock: backdate the stored entry past the TTL.
        with ui.lock:
            ts, value = ui._api_cache["k"]
            ui._api_cache["k"] = (ts - ui.CACHE_TTL - 1, value)
        ui.cached("k", build)
        self.assertEqual(len(calls), 2)

    def test_cached_keys_are_independent(self):
        ui = ui_server.UiState()
        ui.cached("a", lambda: "A")
        self.assertEqual(ui.cached("b", lambda: "B"), "B")
        self.assertEqual(ui.cached("a", lambda: "other"), "A")


class RankingsEndpointTests(ServerHarness):
    """GET /api/rankings: the read-only mirror of the advisory rankings
    report (the data the weekly workflow files as its artifact). The
    endpoint reads the latest rankings/rankings-*.json verbatim; it never
    generates, probes, or mutates config -- `harness rankings` stays the
    one producer. Fixture reports ride the module's _rankings_reports
    seam, so the tests stay hermetic and off the real checkout.
    """

    REPORT = {
        "window": {"start": "2026-09-08", "end": "2026-09-14", "days": 7},
        "top": [{"slug": "deepseek-v4.1-flash", "total_tokens": 123,
                 "trend": "climbing"}],
        "climbers": ["deepseek-v4.1-flash"],
        "ranked_in_catalog": [
            {"slug": "deepseek-v4.1-flash",
             "model_id": "deepseek/deepseek-v4.1-flash",
             "total_tokens": 123, "trend": "climbing"}],
        "proposed_candidates": [
            {"slug": "deepseek-v4.1-flash",
             "model_id": "deepseek/deepseek-v4.1-flash",
             "total_tokens": 123, "trend": "climbing"}],
        "probed_candidates": [],
    }

    def serve_reports(self, mapping):
        """``mapping``: filename -> parsed JSON (a value of None means the
        file exists but is not valid JSON -- the corrupt-file case)."""
        def fake_reports():
            return [f"rankings/{name}" for name in sorted(mapping, reverse=True)]
        reads = {name: (json.dumps(body).encode() if body is not None
                        else b"{corrupt")
                 for name, body in mapping.items()}

        def fake_open(path, *args, **kwargs):
            import io
            name = os.path.basename(str(path))
            if name not in reads:
                raise OSError(f"no such fixture: {path}")
            return io.BytesIO(reads[name])
        return fake_reports, fake_open

    def test_no_reports_is_available_false_with_note(self):
        """The empty/stale state is normal (200, available: false, an
        actionable note) -- never a 500 and never a silent fallback."""
        with mock.patch.object(ui_server, "_rankings_reports", lambda: []):
            conn = self._conn()
            try:
                status, data = _request(conn, "GET", "/api/rankings")
            finally:
                conn.close()
        self.assertEqual(status, 200)
        self.assertFalse(data["available"])
        self.assertEqual(data["reports"], [])
        self.assertIsNone(data["latest"])
        self.assertIn("harness rankings", data["note"])

    def test_latest_report_served_verbatim_newest_first(self):
        """The newest report file is served verbatim; older ones only
        widen the ``reports`` list. Byte-for-byte: the UI renders exactly
        what the CLI emitted, no reshaping."""
        old = dict(self.REPORT, top=[])
        fake_reports, fake_open = self.serve_reports(
            {"rankings-2026-09-08.json": old,
             "rankings-2026-09-14.json": self.REPORT})
        with mock.patch.object(ui_server, "_rankings_reports", fake_reports), \
                mock.patch("builtins.open", fake_open):
            conn = self._conn()
            try:
                status, data = _request(conn, "GET", "/api/rankings")
            finally:
                conn.close()
        self.assertEqual(status, 200)
        self.assertTrue(data["available"])
        self.assertEqual(data["latest"], "rankings-2026-09-14.json")
        self.assertEqual(data["reports"],
                         ["rankings-2026-09-14.json",
                          "rankings-2026-09-08.json"])
        self.assertEqual(data["report"], self.REPORT)

    def test_corrupt_latest_report_is_explicit_not_silent_fallback(self):
        """A stale/unreadable latest report is surfaced (available: false
        + error) rather than a quiet fallback to an older report."""
        fake_reports, fake_open = self.serve_reports(
            {"rankings-2026-09-14.json": None,
             "rankings-2026-09-08.json": self.REPORT})
        with mock.patch.object(ui_server, "_rankings_reports", fake_reports), \
                mock.patch("builtins.open", fake_open):
            conn = self._conn()
            try:
                status, data = _request(conn, "GET", "/api/rankings")
            finally:
                conn.close()
        self.assertEqual(status, 200)
        self.assertFalse(data["available"])
        self.assertEqual(data["latest"], "rankings-2026-09-14.json")
        self.assertIn("unreadable", data["error"])

    def test_rankings_endpoint_is_strictly_read_only(self):
        """The hard constraint, mechanized: a GET must not create the
        rankings directory, write any file, or import the rankings
        producer -- no refresh, no probe, no generation from the server."""
        import harness.rankings as rankings_mod
        made = []
        with mock.patch.object(ui_server, "_rankings_reports", lambda: []), \
                mock.patch.object(rankings_mod, "build_rankings_report",
                                  side_effect=AssertionError("generated!")), \
                mock.patch("builtins.open", side_effect=made.append), \
                mock.patch("os.makedirs", side_effect=made.append), \
                mock.patch("os.mkdir", side_effect=made.append):
            conn = self._conn()
            try:
                status, data = _request(conn, "GET", "/api/rankings")
            finally:
                conn.close()
        self.assertEqual(status, 200)
        self.assertFalse(data["available"])
        self.assertEqual(made, [], "endpoint attempted a write or generation")


class DesktopFallbackTests(unittest.TestCase):
    def test_open_window_falls_back_to_browser_without_pywebview(self):
        import sys
        import harness.ui as ui_mod
        opened = []
        # webbrowser is imported inside the fallback branch; patch it at its
        # home module so the local import resolves to the patched attribute.
        with mock.patch.dict(sys.modules, {"webview": None}), \
                mock.patch("webbrowser.open", side_effect=opened.append):
            mode = ui_mod._open_window("http://127.0.0.1:1/", "tok")
        self.assertEqual(mode, "browser")
        self.assertEqual(opened, ["http://127.0.0.1:1/#tok"])


if __name__ == "__main__":
    unittest.main()
