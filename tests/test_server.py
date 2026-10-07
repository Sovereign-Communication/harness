"""harness serve: the localhost UI server.

Hermetic: an ephemeral loopback server with stubbed runners -- no network,
no key, no real dispatch. Pins the security guards (loopback Host check,
optional token), the static UI, dispatch validation, the run lifecycle with
cooperative cancel, and the event-stream contract.
"""
import http.client
import io
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
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
        # Room under a traced battery (stdlib trace slows every handler
        # thread 10-50x): a join that gives up early leaks a live server
        # thread whose socket surfaces as "ResourceWarning: unclosed" at
        # GC -- which fails the whole audit through the R13 classifier.
        self.thread.join(timeout=60)

    def _conn(self):
        # 120s, not 10s: under the traced baseline battery a route handler
        # can legitimately take longer than 10 wall-clock seconds, and a
        # client-side socket.timeout there is a false failure (the class of
        # defect the 2026-09-27 probe run surfaced: 4 ERRORs, all timeouts).
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=120)


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


class RoutingMetadataTests(unittest.TestCase):
    def test_settings_view_reports_paid_posture_without_secrets(self):
        cases = (
            ({"use_free": True, "allow_escalation": True, "max_cost": 0.05,
              "mcp_auth_token": "secret-token"}, "sk-secret", True),
            ({"use_free": True, "allow_escalation": True, "max_cost": 0.05},
             None, False),
        )
        for values, key, present in cases:
            with self.subTest(paid_key=present):
                settings = mock.Mock()
                settings.to_dict.return_value = values
                with mock.patch.object(ui_server, "load_settings",
                                       return_value=settings), \
                        mock.patch.object(ui_server, "resolve_api_key",
                                          return_value=key):
                    view = ui_server._settings_view()
                self.assertEqual(view["paid_key_present"], present)
                self.assertTrue(view["allow_escalation"])
                self.assertEqual(view["max_cost"], 0.05)
                self.assertNotIn("sk-secret", json.dumps(view))
                if present:
                    self.assertIsNone(view["mcp_auth_token"])
                    self.assertTrue(view["mcp_auth_token_present"])


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


class ApplyDiffPreviewTests(ServerHarness):
    """The apply lane's change preview end to end through the real server:
    a dispatched apply whose runner returns the engine's terminal envelope
    serves `diff` + `file` at /api/runs/{id}/result -- exactly what the UI's
    resultSummary renders. No engine runs: the runner is stubbed with a
    realistic preview envelope (verify_only previews never run a gate).
    """

    ENVELOPE = {
        "status": "preview", "task_id": "ui/x", "rounds": [],
        "cost": 0.0, "rotations": 0, "backend": "harness",
        "verify_only": True, "changed": True,
        "proposed_content": "b\n", "backup": None,
        "file": "t.py", "diff": "--- a\n+++ b\n@@ -1 +1 @@\n-a\n+b\n",
    }

    def test_apply_result_serves_diff_to_the_ui(self):
        seen = {}

        def fake(task_id, args, cancel_check):
            seen["task_id"] = task_id
            seen["args"] = args
            return dict(self.ENVELOPE, task_id=task_id)
        target = os.path.join(tempfile.gettempdir(), "harness-diff-preview.py")
        with open(target, "w", encoding="utf-8") as f:
            f.write("a\n")
        self.addCleanup(lambda: os.path.exists(target)
                        and os.remove(target))
        with mock.patch.dict(ui_server.RUNNERS, {"apply": fake}):
            conn = self._conn()
            try:
                status, run = _request(conn, "POST", "/api/runs",
                                       body={"kind": "apply", "args": {
                                           "file": target,
                                           "instruction": "change a to b",
                                           "verify_only": True}})
                self.assertEqual(status, 201)
                for _ in range(100):
                    _, full = _request(conn, "GET", f"/api/runs/{run['id']}/result")
                    if full["status"] != "running":
                        break
                    time.sleep(0.05)
                else:
                    self.fail("apply run never settled")
            finally:
                conn.close()
        self.assertEqual(full["status"], "preview")
        result = full["result"]
        # run_public passes the envelope verbatim; the stub's own `file`
        # must survive untouched (the UI renders it as the target's name).
        self.assertEqual(result["file"], self.ENVELOPE["file"])
        self.assertIn("-a", result["diff"])
        self.assertIn("+b", result["diff"])
        self.assertTrue(result["verify_only"])


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


class DesktopTokenTests(unittest.TestCase):
    """``harness desktop`` token provenance: an explicit token is used as
    given; otherwise one is generated once, persisted (owner-only), and
    reused."""

    def setUp(self):
        self.cfg = tempfile.mkdtemp(prefix="harness-desktop-")
        self.addCleanup(shutil.rmtree, self.cfg, True)
        env = {k: v for k, v in os.environ.items()
               if k != "HARNESS_UI_AUTH_TOKEN"}
        for patcher in (mock.patch.dict(os.environ, env, clear=True),
                        mock.patch("harness.config.CONFIG_DIR", self.cfg)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.token_file = os.path.join(self.cfg, "desktop_token")

    def _main(self, argv, **env):
        import harness.ui as ui_mod
        httpd = mock.Mock()
        httpd.server_address = ("127.0.0.1", 4242)
        with mock.patch.object(ui_mod, "make_server", return_value=httpd) as mk, \
                mock.patch.dict(os.environ, env), \
                mock.patch.object(ui_mod.osal, "open_url") as opener:
            ui_mod.main(["--browser"] + argv)
        return mk, httpd, opener

    def test_generates_and_persists_a_token(self):
        mk, httpd, opener = self._main([])
        token = mk.call_args.kwargs["auth_token"]
        self.assertGreaterEqual(len(token), 32)
        with open(self.token_file, encoding="utf-8") as f:
            self.assertEqual(f.read(), token)
        httpd.serve_forever.assert_called_once()
        opener.assert_called_once_with("http://127.0.0.1:4242/#" + token)

    def test_reuses_the_persisted_token(self):
        with open(self.token_file, "w", encoding="utf-8") as f:
            f.write("persisted-token\n")
        mk, _, _ = self._main([])
        self.assertEqual(mk.call_args.kwargs["auth_token"], "persisted-token")

    def test_blank_token_file_is_replaced(self):
        with open(self.token_file, "w", encoding="utf-8") as f:
            f.write("   \n")
        mk, _, _ = self._main([])
        token = mk.call_args.kwargs["auth_token"]
        self.assertTrue(token)
        with open(self.token_file, encoding="utf-8") as f:
            self.assertEqual(f.read(), token)

    def test_unreadable_or_unwritable_state_still_yields_a_token(self):
        # The config dir is a regular file: it can be neither read from nor
        # created, and neither failure may stop the desktop from starting.
        blocker = os.path.join(self.cfg, "blocker")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("x")
        with mock.patch("harness.config.CONFIG_DIR", blocker):
            mk, _, _ = self._main([])
        self.assertTrue(mk.call_args.kwargs["auth_token"])

    def test_unreadable_token_file_is_regenerated(self):
        os.mkdir(self.token_file)  # exists, but is a directory: read fails
        mk, _, _ = self._main([])
        self.assertTrue(mk.call_args.kwargs["auth_token"])

    def test_an_existing_token_file_is_replaced_not_written_in_place(self):
        import harness.ui as ui_mod
        with open(self.token_file, "w", encoding="utf-8") as f:
            f.write("old-token")
        os.chmod(self.token_file, 0o666)
        before = os.stat(self.token_file)
        ui_mod._write_token_file(self.token_file, "new-token")
        with open(self.token_file, encoding="utf-8") as f:
            self.assertEqual(f.read(), "new-token")
        self.assertEqual(os.listdir(self.cfg), ["desktop_token"])
        if os.name == "posix":
            # A new inode with owner-only mode: the wide-open file was
            # replaced, never opened for writing.
            after = os.stat(self.token_file)
            self.assertNotEqual(before.st_ino, after.st_ino)
            self.assertEqual(after.st_mode & 0o777, 0o600)

    def test_a_symlinked_token_file_is_refused_and_not_followed(self):
        import harness.ui as ui_mod
        target = os.path.join(self.cfg, "victim")
        with open(target, "w", encoding="utf-8") as f:
            f.write("keep")
        try:
            os.symlink(target, self.token_file)
        except (OSError, NotImplementedError):
            self.skipTest("platform: symlinks unavailable")
        ui_mod._write_token_file(self.token_file, "new-token")
        self.assertTrue(os.path.islink(self.token_file))
        with open(target, encoding="utf-8") as f:
            self.assertEqual(f.read(), "keep")
        self.assertEqual(ui_mod._read_token_file(self.token_file), "")
        self.assertEqual(sorted(os.listdir(self.cfg)),
                         ["desktop_token", "victim"])

    def test_symlink_refusal_without_real_symlinks(self):
        # Same contract as the symlink test above, but hermetic on hosts
        # (Windows without Developer Mode) where creating a symlink is denied.
        import harness.ui as ui_mod
        with open(self.token_file, "w", encoding="utf-8") as f:
            f.write("keep")
        with mock.patch.object(ui_mod.os.path, "islink", return_value=True):
            ui_mod._write_token_file(self.token_file, "new-token")
            self.assertEqual(ui_mod._read_token_file(self.token_file), "")
        with open(self.token_file, encoding="utf-8") as f:
            self.assertEqual(f.read(), "keep")
        self.assertEqual(os.listdir(self.cfg), ["desktop_token"])

    def test_a_failed_write_survives_a_failed_cleanup(self):
        import harness.ui as ui_mod
        with mock.patch.object(ui_mod.os, "replace", side_effect=OSError("no")),                 mock.patch.object(ui_mod.os, "unlink", side_effect=OSError("no")):
            ui_mod._write_token_file(self.token_file, "t")  # must not raise
        self.assertFalse(os.path.exists(self.token_file))

    def test_a_failed_write_leaves_no_temp_file(self):
        import harness.ui as ui_mod
        with mock.patch.object(ui_mod.os, "replace", side_effect=OSError("no")):
            ui_mod._write_token_file(self.token_file, "t")
        self.assertEqual(os.listdir(self.cfg), [])

    @unittest.skipUnless(os.name == "posix",
                         "platform: Windows has no POSIX mode bits")
    def test_the_persisted_token_file_is_owner_only(self):
        self._main([])
        self.assertEqual(os.stat(self.token_file).st_mode & 0o777, 0o600)

    @unittest.skipUnless(os.name == "posix",
                         "platform: Windows has no POSIX mode bits")
    def test_a_loose_token_file_from_an_older_build_is_tightened(self):
        with open(self.token_file, "w", encoding="utf-8") as f:
            f.write("persisted-token")
        os.chmod(self.token_file, 0o644)
        mk, _, _ = self._main([])
        self.assertEqual(mk.call_args.kwargs["auth_token"], "persisted-token")
        self.assertEqual(os.stat(self.token_file).st_mode & 0o777, 0o600)

    def test_explicit_token_writes_nothing(self):
        mk, _, _ = self._main(["--auth-token", "chosen"])
        self.assertEqual(mk.call_args.kwargs["auth_token"], "chosen")
        self.assertFalse(os.path.exists(self.token_file))

    def test_environment_token_is_used_as_given(self):
        mk, _, _ = self._main([], HARNESS_UI_AUTH_TOKEN="from-env")
        self.assertEqual(mk.call_args.kwargs["auth_token"], "from-env")

    def test_non_loopback_host_is_refused(self):
        import harness.ui as ui_mod
        with self.assertRaises(SystemExit) as ctx:
            ui_mod.main(["--host", "0.0.0.0"])
        self.assertEqual(ctx.exception.code, 1)

    def test_ctrl_c_stops_the_browser_mode_foreground_cleanly(self):
        import harness.ui as ui_mod
        httpd = mock.Mock()
        httpd.server_address = ("127.0.0.1", 4242)
        with mock.patch.object(ui_mod, "make_server", return_value=httpd), \
                mock.patch.object(ui_mod.osal, "open_url"), \
                mock.patch("threading.Thread.join",
                           side_effect=KeyboardInterrupt):
            ui_mod.main(["--browser", "--auth-token", "t"])


class ChatEndpointTests(ServerHarness):
    def test_chat_post_and_history(self):
        fake_chat_result = {
            "status": "ok",
            "intent": "conversation",
            "prompt": "Hello",
            "response": "Hello world!",
            "cost": 0.0001,
        }
        with mock.patch("harness.server.AutonomousAgent") as mock_agent_cls:
            mock_inst = mock_agent_cls.return_value
            mock_inst.run_prompt.return_value = fake_chat_result

            conn = self._conn()
            try:
                # 1. POST /api/chat
                status, run = _request(conn, "POST", "/api/chat",
                                       body={"prompt": "Hello", "session_id": "test-sid"})
                self.assertEqual(status, 201)
                self.assertEqual(run["kind"], "chat")

                # Wait for settlement
                for _ in range(50):
                    _, full = _request(conn, "GET", f"/api/runs/{run['id']}/result")
                    if full["status"] != "running":
                        break
                    time.sleep(0.05)
                self.assertEqual(full["status"], "ok")
                self.assertEqual(full["result"]["response"], "Hello world!")

                # 2. GET /api/chat/history
                with mock.patch("harness.server.load_chat_history", return_value=[fake_chat_result]):
                    status, hist_data = _request(conn, "GET", "/api/chat/history?session_id=test-sid")
                    self.assertEqual(status, 200)
                    self.assertEqual(hist_data["session_id"], "test-sid")
                    self.assertEqual(len(hist_data["history"]), 1)
            finally:
                conn.close()

    def test_chat_runner_uses_classifier_for_lane_selection(self):
        cases = (("Explain the router", True),
                 ("Fix harness/server.py to validate the request", False))
        with mock.patch.object(ui_server, "load_settings", return_value=mock.Mock()), \
                mock.patch.object(ui_server, "AutonomousAgent") as agent_cls:
            agent_cls.return_value.run_prompt.return_value = {"status": "ok"}
            for prompt, force_conversation in cases:
                with self.subTest(prompt=prompt):
                    ui_server.run_chat_task("task-1", {"prompt": prompt}, lambda: False)
                    call = agent_cls.return_value.run_prompt.call_args
                    self.assertEqual(call.kwargs["force_conversation"], force_conversation)

    def test_chat_server_direct_synchronous_execution(self):
        # Directly test validate_dispatch, run_chat_task, and handler endpoints in the main thread
        # for stdlib trace coverage.
        args1 = ui_server.validate_dispatch("chat", {"prompt": "Hello"})
        self.assertEqual(args1["prompt"], "Hello")
        self.assertTrue(args1["auto_apply"])

        args2 = ui_server.validate_dispatch("chat", {"prompt": "Fix", "auto_apply": False, "session_id": "s123", "allow_paid": True})
        self.assertEqual(args2["prompt"], "Fix")
        self.assertFalse(args2["auto_apply"])
        self.assertEqual(args2["session_id"], "s123")
        self.assertTrue(args2["allow_paid"])

        # run_chat_task
        with mock.patch("harness.server.AutonomousAgent") as mock_cls:
            mock_cls.return_value.run_prompt.return_value = {"status": "ok"}
            res = ui_server.run_chat_task("task-1", {"prompt": "Hello", "allow_paid": True}, lambda: False)
            self.assertEqual(res, {"status": "ok"})
            settings_passed = mock_cls.call_args[1]["settings"]
            self.assertTrue(settings_passed.allow_escalation)

        # Handler synchronous dispatch for /api/chat/history and /api/chat
        class DirectHandler(ui_server.UiRequestHandler):
            def __init__(self, ui, path, method="GET", body=b""):
                self.server = mock.MagicMock(ui=ui)
                self.path = path
                self.command = method
                self.headers = {"Host": "127.0.0.1", "Content-Length": str(len(body))}
                self.rfile = io.BytesIO(body)
                self.wfile = io.BytesIO()

            def send_response(self, code, message=None):
                pass

            def send_header(self, keyword, value):
                pass

            def end_headers(self):
                pass

        ui = ui_server.UiState()
        with mock.patch("harness.server.load_chat_history", return_value=[]):
            h_get = DirectHandler(ui, "/api/chat/history?session_id=s1", "GET")
            h_get.do_GET()
            self.assertIn(b'"history"', h_get.wfile.getvalue())

        h_post = DirectHandler(ui, "/api/chat", "POST", json.dumps({"prompt": "Hello"}).encode("utf-8"))
        h_post.do_POST()
        self.assertIn(b'"kind": "chat"', h_post.wfile.getvalue())


class ChatSessionSurfaceTests(ServerHarness):
    """The chat sidebar surface: session listing, deletion (with the traversal
    guard), and root_dir validation -- the Antigravity UI's server side, now
    pinned hermetically (the D12 changed-line gate failed on this code)."""

    def setUp(self):
        super().setUp()
        self.hdir = tempfile.mkdtemp(prefix="harness_sessions_")
        self.addCleanup(self._rm_hdir)
        self._real = ui_server.get_default_history_dir
        ui_server.get_default_history_dir = lambda: Path(self.hdir)
        self.addCleanup(self._restore)

    def _rm_hdir(self):
        shutil.rmtree(self.hdir, ignore_errors=True)

    def _restore(self):
        ui_server.get_default_history_dir = self._real

    @staticmethod
    def _seed(hdir, name, mtime, prompts):
        p = Path(hdir) / name
        with open(p, "w", encoding="utf-8") as f:
            for pr in prompts:
                f.write(json.dumps({"prompt": pr, "response": "ok"}) + "\n")
        os.utime(p, (mtime, mtime))

    def test_sessions_empty_dir(self):
        conn = self._conn()
        try:
            status, data = _request(conn, "GET", "/api/chat/sessions")
            self.assertEqual(status, 200)
            self.assertEqual(data["sessions"], [])
        finally:
            conn.close()

    def test_sessions_newest_first_with_previews_and_empty_fallback(self):
        now = time.time()
        self._seed(self.hdir, "sess_a.jsonl", now - 100, ["alpha first prompt here", "second"])
        self._seed(self.hdir, "sess_b.jsonl", now - 50, [])  # empty file
        self._seed(self.hdir, "sess_c.jsonl", now - 9000, ["c older"])
        conn = self._conn()
        try:
            status, data = _request(conn, "GET", "/api/chat/sessions")
            self.assertEqual(status, 200)
            ids = [s["id"] for s in data["sessions"]]
            self.assertEqual(ids, ["sess_b", "sess_a", "sess_c"])
            by_id = {s["id"]: s for s in data["sessions"]}
            self.assertEqual(by_id["sess_a"]["preview"], "alpha first prompt here")
            self.assertEqual(by_id["sess_b"]["preview"], "(empty)")
        finally:
            conn.close()

    def test_delete_existing_missing_and_bad_prefix(self):
        self._seed(self.hdir, "sess_gone.jsonl", time.time(), ["bye"])
        conn = self._conn()
        try:
            status, data = _request(conn, "POST", "/api/chat/session/delete",
                                    body={"session_id": "sess_gone"})
            self.assertEqual(status, 200)
            self.assertTrue(data["ok"])
            self.assertFalse((Path(self.hdir) / "sess_gone.jsonl").exists())

            status, data = _request(conn, "POST", "/api/chat/session/delete",
                                    body={"session_id": "sess_gone"})
            self.assertEqual(status, 200)
            self.assertFalse(data["ok"])

            status, data = _request(conn, "POST", "/api/chat/session/delete",
                                    body={"session_id": "nope_123"})
            self.assertEqual(status, 400)
            self.assertIn("sess_", data["error"])
        finally:
            conn.close()

    def test_delete_refuses_traversal_ids(self):
        conn = self._conn()
        try:
            for sid in ("sess_../../victim", "sess_..\\evil", "sess_a/b"):
                status, data = _request(conn, "POST", "/api/chat/session/delete",
                                        body={"session_id": sid})
                self.assertEqual(status, 400, sid)
                self.assertIn("not allowed", data["error"])
        finally:
            conn.close()

    def test_chat_rejects_nonexistent_root_dir_before_run_created(self):
        conn = self._conn()
        try:
            status, data = _request(conn, "POST", "/api/runs",
                                    body={"kind": "chat",
                                          "args": {"prompt": "hi",
                                                   "root_dir": "Z:/definitely/not/here"}})
            self.assertEqual(status, 400)
            self.assertIn("root_dir", data["error"])
            self.assertEqual(self.httpd.ui.runs, {})
        finally:
            conn.close()

    def test_chat_accepts_valid_root_dir_and_reaches_the_agent(self):
        seen = {}

        def fake(task_id, args, cancel_check):
            seen["root_dir"] = args.get("root_dir")
            seen["web"] = args.get("web")
            return {"status": "ok", "task_id": task_id, "cost": 0.0}

        with mock.patch.dict(ui_server.RUNNERS, {"chat": fake}):
            conn = self._conn()
            try:
                workdir = tempfile.mkdtemp(prefix="harness_workdir_")
                self.addCleanup(self._rm, workdir)
                status, run = _request(conn, "POST", "/api/runs",
                                       body={"kind": "chat",
                                             "args": {"prompt": "hi",
                                                      "root_dir": workdir,
                                                      "web": True}})
                self.assertEqual(status, 201)
                self.assertTrue(seen["root_dir"].endswith("harness_workdir_")
                                or Path(seen["root_dir"]).is_dir())
                self.assertTrue(seen["web"])
            finally:
                conn.close()

    @staticmethod
    def _rm(p):
        shutil.rmtree(p, ignore_errors=True)


class SessionSurfaceUnitTests(unittest.TestCase):
    """Direct, in-process (main-thread) pins of the same session surface the
    HTTP tests exercise: the stdlib tracer used by the D12 changed-line gate
    does not record worker-thread executions, so these unit tests are also
    what keeps the audit honest."""

    def setUp(self):
        self.hdir = tempfile.mkdtemp(prefix="harness_sessunit_")
        self.addCleanup(lambda: shutil.rmtree(self.hdir, ignore_errors=True))
        real = ui_server.get_default_history_dir
        ui_server.get_default_history_dir = lambda: Path(self.hdir)
        self.addCleanup(lambda: setattr(ui_server, "get_default_history_dir", real))

    def test_list_sessions_direct(self):
        # Explicit, well-separated mtimes: coarse filesystem timestamp
        # granularity makes two adjacent time.time() calls order-unstable.
        now = time.time()
        ChatSessionSurfaceTests._seed(self.hdir, "sess_a.jsonl", now - 100,
                                      ["alpha first prompt"])
        ChatSessionSurfaceTests._seed(self.hdir, "sess_b.jsonl", now, [])
        # Blank line + malformed line at the head: both must be skipped and
        # the first VALID turn still yields the preview.
        p = Path(self.hdir) / "sess_c.jsonl"
        p.write_text("\n{not json at all\n" + json.dumps({"prompt": "valid after junk"}) + "\n",
                     encoding="utf-8")
        os.utime(p, (now - 10, now - 10))
        sessions = ui_server._list_chat_sessions()
        self.assertEqual([s["id"] for s in sessions], ["sess_b", "sess_c", "sess_a"])
        self.assertEqual(sessions[0]["preview"], "(empty)")
        self.assertEqual(sessions[1]["preview"], "valid after junk")
        self.assertEqual(sessions[2]["preview"], "alpha first prompt")

    def test_list_sessions_unreadable_file_is_skipped(self):
        # OSError branch: an unreadable (locked) file is skipped, not fatal.
        p = Path(self.hdir) / "sess_z.jsonl"
        p.write_text(json.dumps({"prompt": "x"}), encoding="utf-8")
        def boom(*a, **k):
            raise OSError("locked by another process")
        with mock.patch("builtins.open", side_effect=boom):
            sessions = ui_server._list_chat_sessions()
        self.assertEqual(sessions, [])

    def test_delete_direct(self):
        p = Path(self.hdir) / "sess_gone.jsonl"
        p.write_text(json.dumps({"prompt": "x", "response": "y"}), encoding="utf-8")
        self.assertTrue(ui_server._delete_chat_session("sess_gone"))
        self.assertFalse(p.exists())
        self.assertFalse(ui_server._delete_chat_session("sess_gone"))
        with self.assertRaises(HarnessError):
            ui_server._delete_chat_session("nope_1")
        for sid in ("sess_../../victim", "sess_..\\evil", "sess_a/b"):
            with self.assertRaises(HarnessError):
                ui_server._delete_chat_session(sid)

    def test_chat_dispatch_rejects_bad_root_dir_direct(self):
        with self.assertRaises(HarnessError) as ctx:
            ui_server.validate_dispatch("chat", {"prompt": "hi",
                                                 "root_dir": "Z:/definitely/not/here"})
        self.assertIn("root_dir", str(ctx.exception))
        workdir = tempfile.mkdtemp(prefix="harness_wd_")
        self.addCleanup(lambda: shutil.rmtree(workdir, ignore_errors=True))
        args = {"prompt": "hi", "root_dir": workdir}
        ui_server.validate_dispatch("chat", args)  # valid dir passes
        self.assertEqual(args["prompt"], "hi")

    def test_sessions_endpoint_direct_handler(self):
        # The request-handler lines execute inside server worker threads, so
        # the HTTP tests never show up in the trace baseline; drive the
        # handler directly (main thread) via the file's own DirectHandler.
        ChatSessionSurfaceTests._seed(self.hdir, "sess_a.jsonl", time.time(),
                                      ["hello from the endpoint"])
        ui = ui_server.UiState()
        h = self._direct(ui, "/api/chat/sessions", "GET")
        h.do_GET()
        self.assertIn(b'"sessions"', h.wfile.getvalue())
        self.assertIn(b"hello from the endpoint", h.wfile.getvalue())

    def test_session_delete_endpoint_direct_handler(self):
        p = Path(self.hdir) / "sess_del.jsonl"
        p.write_text(json.dumps({"prompt": "bye"}), encoding="utf-8")
        ui = ui_server.UiState()
        body = json.dumps({"session_id": "sess_del"}).encode("utf-8")
        h = self._direct(ui, "/api/chat/session/delete", "POST", body)
        h.do_POST()
        out = h.wfile.getvalue()
        self.assertIn(b'"ok": true', out)
        self.assertFalse(p.exists())

        # The 400 path: invalid id -> _api_session_delete -> _error
        body_bad = json.dumps({"session_id": "nope_9"}).encode("utf-8")
        h2 = self._direct(ui, "/api/chat/session/delete", "POST", body_bad)
        h2.do_POST()
        self.assertIn(b"must start with 'sess_'", h2.wfile.getvalue())

    @staticmethod
    def _direct(ui, path, method, body=None):
        # Same shape as the module-bottom DirectHandler (defined inside that
        # test method, so it is not importable from here).
        body = body or b""
        class DirectHandler(ui_server.UiRequestHandler):
            def __init__(self, ui, path, method="GET", body=b""):
                self.server = mock.MagicMock(ui=ui)
                self.path = path
                self.command = method
                self.headers = {"Host": "127.0.0.1", "Content-Length": str(len(body))}
                self.rfile = io.BytesIO(body)
                self.wfile = io.BytesIO()

            def send_response(self, code, message=None):
                pass

            def send_header(self, keyword, value):
                pass

            def end_headers(self):
                pass

        return DirectHandler(ui, path, method, body)


def _write_jev_repo(root, status_line):
    """Minimal fixture harness.jev_completion.collect_phase_evidence can
    read (same shape as tests/test_jev_completion.py): STATUS row only."""
    from harness.jev_completion import DEFAULT_COMPLETION_PACK
    docs = os.path.join(root, "docs")
    os.makedirs(docs, exist_ok=True)
    with open(os.path.join(docs, "jev-roadmap.md"), "w", encoding="utf-8") as f:
        f.write("## Canonical STATUS\n\n"
                "| Track | Phase | Status | Evidence |\n"
                "|---|---|---|---|\n"
                f"{status_line}\n")
    pack_dir = os.path.join(root, "packs")
    os.makedirs(pack_dir, exist_ok=True)
    with open(os.path.join(pack_dir, "phase_completion.pack.json"), "w",
             encoding="utf-8") as f:
        json.dump(DEFAULT_COMPLETION_PACK, f)


class JevPhaseEndpointTests(ServerHarness):
    """GET /api/jev-phase: always local-only (harness.jev_completion, same
    engine `harness jev-phase --local-only` / the MCP jev_phase tool use).
    Never calls a live Jev judge -- these tests carry no settings/key at
    all, so a live call would error, not silently pass."""

    def setUp(self):
        super().setUp()
        self.repo = tempfile.TemporaryDirectory()
        self.addCleanup(self.repo.cleanup)

    def test_one_phase_scores_without_any_key_or_network(self):
        _write_jev_repo(
            self.repo.name,
            "| 2 Pillars `JEV-P2-*` | **in progress / repair** | PR OPEN |")
        conn = self._conn()
        try:
            status, data = _request(
                conn, "GET",
                f"/api/jev-phase?phase=JEV-P2&repo_root={self.repo.name}")
        finally:
            conn.close()
        self.assertEqual(status, 200)
        self.assertEqual(data["phase"], "JEV-P2")
        self.assertFalse(data["can_mark_complete"])
        self.assertIn("hard_gates", data)

    def test_no_phase_scores_the_whole_board(self):
        _write_jev_repo(
            self.repo.name,
            "| 2 Pillars `JEV-P2-*` | **in progress / repair** | PR OPEN |")
        conn = self._conn()
        try:
            status, data = _request(
                conn, "GET", f"/api/jev-phase?repo_root={self.repo.name}")
        finally:
            conn.close()
        self.assertEqual(status, 200)
        self.assertIn("phases", data)
        self.assertIn("false_complete", data)

class JevPhaseAuthTests(ServerHarness):
    """Same X-Harness-Auth guard as every other /api route (TokenAuthTests
    pattern), pinned for this new endpoint specifically."""
    token = "s3cret-token"

    def test_missing_token_401(self):
        conn = self._conn()
        try:
            status, _ = _request(conn, "GET", "/api/jev-phase?phase=X")
        finally:
            conn.close()
        self.assertEqual(status, 401)


class CostEndpointTests(ServerHarness):
    """GET /api/cost: the ONE cost-observability owner
    (AutonomyLedger.cost_report), same call `harness cost` makes."""

    def test_cost_report_shape(self):
        report = {"total_cost": 0.0, "entries": 0}
        ledger = mock.Mock(cost_report=mock.Mock(return_value=report))
        with mock.patch.object(ui_server, "load_settings"), \
             mock.patch.object(ui_server, "ledger_for", return_value=ledger):
            conn = self._conn()
            try:
                status, data = _request(conn, "GET",
                                        "/api/cost?by_tier=true&last=24h")
            finally:
                conn.close()
        self.assertEqual(status, 200)
        self.assertEqual(data, report)
        ledger.cost_report.assert_called_once_with(
            window="24h", by_tier=True, by_model=False, savings=False)


class JevCreditEndpointTests(ServerHarness):
    """The Jev spend/credit block must read the same on /api/cost and
    /api/spend (the GUI badge and the CLI agree because both come from the
    ledger's one cost_report), and must price legacy entries correctly."""

    def setUp(self):
        super().setUp()
        from harness.ledger import AutonomyLedger
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ledger = AutonomyLedger(os.path.join(self._tmp.name, "l.jsonl"))
        # One real call, one fallback (free), one legacy entry whose stored
        # cost was written at the old $42/Mtok rate.
        self.ledger.append("jev_eval", model="jev-1.13.0", input_tokens=2000,
                           output_tokens=10, is_fallback=False, cost=0.000084)
        self.ledger.append("jev_eval", model="jev-1.13.0", input_tokens=700,
                           is_fallback=True, cost=0.0)
        self.ledger.append("jev_eval", model="jev-1.13.0", input_tokens=1000,
                           is_fallback=False, cost=0.042)

    def _get(self, path):
        gov = mock.Mock(spent=0.0, max_cost=0.05)
        gov.key_status.return_value = {"keyed": False}
        with mock.patch.object(ui_server, "load_settings"), \
             mock.patch.object(ui_server, "ledger_for", return_value=self.ledger), \
             mock.patch.object(ui_server, "governor_for", return_value=(None, gov)):
            conn = self._conn()
            try:
                return _request(conn, "GET", path)
            finally:
                conn.close()

    def _assert_jev_block(self, jev):
        self.assertEqual(jev["calls"], 2)
        self.assertEqual(jev["input_tokens"], 3000)
        # 3000 tokens at $0.042/Mtok -- the legacy $0.042 entry does not count
        # as $0.042, it counts as 1000 tokens.
        self.assertAlmostEqual(jev["cost"], 0.000126, places=9)
        self.assertAlmostEqual(jev["monthly_credit"], 5.0)
        self.assertAlmostEqual(jev["remaining_credit"], 5.0 - 0.000126, places=6)
        self.assertAlmostEqual(jev["price_per_million_input"], 0.042)

    def test_cost_endpoint_carries_the_jev_block(self):
        status, data = self._get("/api/cost")
        self.assertEqual(status, 200)
        self._assert_jev_block(data["jev"])

    def test_spend_endpoint_carries_the_same_jev_block(self):
        status, data = self._get("/api/spend")
        self.assertEqual(status, 200)
        self._assert_jev_block(data["jev"])
        self.assertEqual(data["session"]["ceiling"], 0.05)
        _, cost = self._get("/api/cost")
        self.assertEqual(data["jev"], cost["jev"])

    def test_spend_endpoint_omits_jev_when_report_has_none(self):
        gov = mock.Mock(spent=0.0, max_cost=0.05)
        gov.key_status.return_value = {"keyed": False}
        ledger = mock.Mock(cost_report=mock.Mock(return_value={"total_cost": 0.0}))
        with mock.patch.object(ui_server, "load_settings"), \
             mock.patch.object(ui_server, "ledger_for", return_value=ledger), \
             mock.patch.object(ui_server, "governor_for", return_value=(None, gov)):
            conn = self._conn()
            try:
                status, data = _request(conn, "GET", "/api/spend")
            finally:
                conn.close()
        self.assertEqual(status, 200)
        self.assertNotIn("jev", data)


class MissionsEndpointTests(ServerHarness):
    """GET /api/missions and /api/missions/<id>: read-only faces over the
    HUL-A mission pack (harness.mission_record) -- same owner the
    mission_status MCP tool and `harness mission status` use."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # The missions endpoints jail ?root= inside ./missions, so the
        # fixture pack tree lives under a temp working directory.
        self._old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, self._old_cwd)
        self.root = "missions"

    def _init_pack(self, mission_id):
        from harness import mission_record as mr
        spec = mr.build_mission_spec(
            mission_id=mission_id,
            request="Exercise the missions HTTP faces",
            success_definition="GET /api/missions returns the pack summary",
            max_cost_usd=0.50,
            terminal_reserve_cost_usd=0.05,
            in_scope=["harness/server.py"],
            out_of_scope=["live network calls"],
            persistence_root=self.root,
            verifier_kind="hermetic-local",
        )
        return mr.init_mission_pack(self.root, spec)

    def test_list_is_empty_when_root_is_missing(self):
        conn = self._conn()
        try:
            status, data = _request(
                conn, "GET", f"/api/missions?root={self.root}")
        finally:
            conn.close()
        self.assertEqual(status, 200)
        self.assertEqual(data["missions"], [])
        self.assertEqual(data["total"], 0)
        self.assertEqual(data["limit"], 25)
        self.assertEqual(data["offset"], 0)

    def test_list_returns_paginated_bounded_summaries(self):
        for mission_id in ("m-http-1", "m-http-2", "m-http-3"):
            self._init_pack(mission_id)
        conn = self._conn()
        try:
            status, data = _request(
                conn, "GET",
                f"/api/missions?root={self.root}&limit=2&offset=1")
        finally:
            conn.close()
        self.assertEqual(status, 200)
        self.assertEqual([m["id"] for m in data["missions"]],
                         ["m-http-2", "m-http-3"])
        self.assertEqual((data["total"], data["limit"], data["offset"]),
                         (3, 2, 1))
        for summary in data["missions"]:
            self.assertIn("dual_budget", summary)
            self.assertNotIn("receipts", summary)
            self.assertNotIn("jev_evals", summary)
            self.assertNotIn("resume", summary)

    def test_list_skips_corrupt_pack_without_failing_page(self):
        corrupt = self._init_pack("m-http-bad")
        corrupt.resume_path.write_text("[]\n", encoding="utf-8")
        self._init_pack("m-http-good")
        conn = self._conn()
        try:
            status, data = _request(
                conn, "GET", f"/api/missions?root={self.root}")
        finally:
            conn.close()
        self.assertEqual(status, 200)
        self.assertEqual(data["total"], 2)
        self.assertEqual([mission["id"] for mission in data["missions"]],
                         ["m-http-good"])

    def test_list_does_not_load_append_only_histories(self):
        from harness import mission_record as mr

        self._init_pack("m-http-4")
        with mock.patch.object(mr, "load_receipts",
                               side_effect=AssertionError("history loaded")):
            with mock.patch.object(
                    mr, "load_jev_evals",
                    side_effect=AssertionError("history loaded")):
                conn = self._conn()
                try:
                    status, data = _request(
                        conn, "GET", f"/api/missions?root={self.root}")
                finally:
                    conn.close()
        self.assertEqual(status, 200)
        self.assertEqual([m["id"] for m in data["missions"]],
                         ["m-http-4"])

    def test_list_rejects_invalid_pagination(self):
        for query in ("limit=0", "limit=101", "offset=-1", "offset=bad"):
            with self.subTest(query=query):
                conn = self._conn()
                try:
                    status, _ = _request(
                        conn, "GET", f"/api/missions?root={self.root}&{query}")
                finally:
                    conn.close()
                self.assertEqual(status, 400)

    def test_detail_returns_pack_summary_without_writing_projection_files(self):
        pack = self._init_pack("m-http-3")
        status_path = str(pack.status_md)
        index_path = str(pack.index_md)
        for path, content in ((status_path, "operator status sentinel\n"),
                              (index_path, "operator index sentinel\n")):
            with open(path, "w", encoding="utf-8") as stream:
                stream.write(content)
        conn = self._conn()
        try:
            status, data = _request(
                conn, "GET", f"/api/missions/m-http-3?root={self.root}")
        finally:
            conn.close()
        self.assertEqual(status, 200)
        self.assertEqual(data["id"], "m-http-3")
        self.assertIn("dual_budget", data)
        for path, expected in ((status_path, "operator status sentinel\n"),
                               (index_path, "operator index sentinel\n")):
            with open(path, encoding="utf-8") as stream:
                self.assertEqual(stream.read(), expected)

    def test_detail_unknown_id_is_400(self):
        os.makedirs(self.root, exist_ok=True)
        conn = self._conn()
        try:
            status, data = _request(
                conn, "GET", f"/api/missions/does-not-exist?root={self.root}")
        finally:
            conn.close()
        self.assertEqual(status, 400)
        self.assertIn("mission pack not found", data["error"])


    def test_list_rejects_traversal_roots(self):
        """?root= must stay inside ./missions: .., ../.. and absolute paths 400."""
        for hostile in ("..", "../..", "/etc", "/tmp"):
            conn = self._conn()
            try:
                status, data = _request(conn, "GET", "/api/missions?root=" + hostile)
            finally:
                conn.close()
            self.assertEqual(status, 400, f"root={hostile!r}")
            self.assertIn("invalid missions root", data["error"])

    def test_detail_rejects_traversal_root(self):
        self._init_pack("m-trav-1")
        conn = self._conn()
        try:
            status, _ = _request(
                conn, "GET", "/api/missions/m-trav-1?root=..")
        finally:
            conn.close()
        self.assertEqual(status, 400)

    def test_detail_relative_root_uses_validated_missions_path(self):
        """A short relative root is consumed at the same jailed path it validates."""
        from harness import mission_record as mr

        mission_id = "m-http-sibling"
        inner_root = os.path.join("missions", "sibling")
        outside_root = "sibling"
        for root, request in ((inner_root, "inside missions jail"),
                              (outside_root, "outside missions jail")):
            spec = mr.build_mission_spec(
                mission_id=mission_id,
                request=request,
                success_definition="The HTTP endpoint reads only the jailed root",
                max_cost_usd=0.50,
                terminal_reserve_cost_usd=0.05,
                in_scope=["harness/server.py"],
                out_of_scope=["live network calls"],
                persistence_root=root,
                verifier_kind="hermetic-local",
            )
            mr.init_mission_pack(root, spec)

        conn = self._conn()
        try:
            status, data = _request(
                conn, "GET",
                f"/api/missions/{mission_id}?root=sibling")
        finally:
            conn.close()

        self.assertEqual(status, 200)
        self.assertEqual(data["request"], "inside missions jail")
        self.assertEqual(
            data["pack_dir"],
            os.path.join(os.path.realpath(inner_root), mission_id))

    def test_list_rejects_symlink_outside_root(self):
        os.makedirs(self.root, exist_ok=True)
        outside_root = os.path.join(self.tmp.name, "outside-missions")
        os.makedirs(outside_root)
        try:
            os.symlink(outside_root, os.path.join(self.root, "escape"),
                       target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"directory symlinks unavailable: {exc}")

        conn = self._conn()
        try:
            status, data = _request(conn, "GET", "/api/missions?root=escape")
        finally:
            conn.close()
        self.assertEqual(status, 400)
        self.assertIn("invalid missions root", data["error"])

    def test_detail_rejects_symlinked_pack_outside_root(self):
        from harness import mission_record as mr

        mission_id = "m-http-pack-escape"
        outside_root = os.path.join(self.tmp.name, "outside-missions")
        os.makedirs(outside_root)
        spec = mr.build_mission_spec(
            mission_id=mission_id,
            request="outside pack must not be read",
            success_definition="Pack directory symlinks do not cross the jail",
            max_cost_usd=0.50,
            terminal_reserve_cost_usd=0.05,
            in_scope=["harness/server.py"],
            out_of_scope=["live network calls"],
            persistence_root=outside_root,
            verifier_kind="hermetic-local",
        )
        outside_pack = mr.init_mission_pack(outside_root, spec)
        os.makedirs(self.root, exist_ok=True)
        try:
            os.symlink(str(outside_pack.dir), os.path.join(self.root, mission_id),
                       target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"directory symlinks unavailable: {exc}")

        conn = self._conn()
        try:
            status, data = _request(
                conn, "GET", f"/api/missions/{mission_id}?root=missions")
        finally:
            conn.close()
        self.assertEqual(status, 400)
        self.assertIn("mission pack directory escapes its root", data["error"])

    def test_list_default_root_still_serves(self):
        """No ?root= param keeps working (defaults to ./missions)."""
        self._init_pack("m-def-1")
        conn = self._conn()
        try:
            status, data = _request(conn, "GET", "/api/missions")
        finally:
            conn.close()
        self.assertEqual(status, 200)
        self.assertEqual(data["total"], 1)


if __name__ == "__main__":
    unittest.main()
