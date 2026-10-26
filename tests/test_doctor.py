"""Hermetic CLI coverage for the real ``harness doctor`` invocation."""
import contextlib
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from harness import cli, doctor
from harness.errors import HarnessError


def _ok(name="stub", detail="healthy"):
    return {"name": name, "status": "OK", "detail": detail}


class DoctorCliTests(unittest.TestCase):
    def _fixed_non_network_checks(self):
        return [
            mock.patch.object(doctor, "check_python", return_value=_ok("python")),
            mock.patch.object(doctor, "check_ruff", return_value=_ok("ruff")),
            mock.patch.object(
                doctor, "check_dns",
                return_value={"name": "dns", "status": "OK", "detail": "public",
                              "hosts": {doctor.OPENROUTER_HOST: {"class": "public"},
                                        "api.typesafe.ai": {"class": "public"}}}),
            mock.patch.object(doctor, "check_typesafe", return_value=_ok("typesafe")),
            mock.patch.object(doctor, "check_driver", return_value=_ok("driver")),
            mock.patch.object(doctor, "check_gui_port", return_value=_ok("gui_port")),
            mock.patch.object(doctor, "check_ledger", return_value=_ok("ledger")),
            mock.patch.object(doctor, "check_mcp_stdio", return_value=_ok("mcp_stdio")),
        ]

    def _enter(self, patches):
        stack = contextlib.ExitStack()
        for item in patches:
            stack.enter_context(item)
        return stack

    def test_real_cli_json_reports_missing_keys_and_nonzero(self):
        settings = SimpleNamespace(
            jev_endpoint="https://api.typesafe.ai/v1/systemone",
            ledger_path="unused-ledger.jsonl", mcp_auth_token=None,
            jev_disabled=False)
        stdout = io.StringIO()
        patches = self._fixed_non_network_checks()
        patches += [
            mock.patch.object(cli, "load_settings", return_value=settings),
            mock.patch.object(doctor.config, "resolve_api_key_with_source",
                              return_value=(None, None)),
            mock.patch.object(doctor.config, "resolve_jev_key", return_value=None),
        ]
        with self._enter(patches), contextlib.redirect_stdout(stdout), \
                self.assertRaises(SystemExit) as raised:
            cli.main(["doctor", "--json"])

        self.assertEqual(raised.exception.code, 1)
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["status"], "MISSING")
        by_name = {item["name"]: item for item in report["checks"]}
        self.assertEqual(by_name["openrouter_key"]["status"], "MISSING")
        self.assertEqual(by_name["jev_key"]["status"], "MISSING")
        self.assertIn("OPENROUTER_API_KEY", by_name["openrouter_key"]["fix"])
        self.assertIn("TYPESAFE_API_KEY", by_name["jev_key"]["fix"])

    def test_real_cli_never_serializes_provider_key(self):
        settings = SimpleNamespace(
            jev_endpoint="https://api.typesafe.ai/v1/systemone",
            ledger_path="unused-ledger.jsonl", mcp_auth_token=None,
            jev_disabled=False)
        stdout = io.StringIO()
        patches = self._fixed_non_network_checks()
        patches += [
            mock.patch.object(cli, "load_settings", return_value=settings),
            mock.patch.object(
                doctor.config, "resolve_api_key_with_source",
                return_value=("fixture-secret-do-not-print",
                              {"kind": "environment", "name": "OPENROUTER_API_KEY"})),
            mock.patch.object(doctor, "_openrouter_key_info",
                              return_value=(200, {"data": {"limit": 8, "limit_remaining": 7}})),
            mock.patch.object(doctor.config, "resolve_jev_key", return_value="jev-fixture"),
        ]
        with self._enter(patches), contextlib.redirect_stdout(stdout):
            cli.main(["doctor", "--json"])

        rendered = stdout.getvalue()
        self.assertNotIn("fixture-secret-do-not-print", rendered)
        report = json.loads(rendered)
        openrouter = next(item for item in report["checks"]
                          if item["name"] == "openrouter_key")
        self.assertEqual(openrouter["source"]["name"], "OPENROUTER_API_KEY")
        self.assertEqual(openrouter["limit"], 8)

    def test_real_cli_defaults_to_human_report(self):
        settings = SimpleNamespace(
            jev_endpoint="https://api.typesafe.ai/v1/systemone",
            ledger_path="unused-ledger.jsonl", mcp_auth_token=None,
            jev_disabled=False)
        stdout = io.StringIO()
        patches = self._fixed_non_network_checks()
        patches += [
            mock.patch.object(cli, "load_settings", return_value=settings),
            mock.patch.object(
                doctor.config, "resolve_api_key_with_source",
                return_value=("fixture-secret", {"kind": "environment",
                                                  "name": "OPENROUTER_API_KEY"})),
            mock.patch.object(doctor, "_openrouter_key_info",
                              return_value=(200, {"data": {"limit": 8}})),
            mock.patch.object(doctor.config, "resolve_jev_key", return_value="jev-fixture"),
        ]
        with self._enter(patches), contextlib.redirect_stdout(stdout):
            cli.main(["doctor"])
        self.assertIn("Harness environment preflight", stdout.getvalue())
        self.assertIn("Result: OK (exit 0)", stdout.getvalue())
        self.assertNotIn("fixture-secret", stdout.getvalue())

    def test_python_resolves_python3_only_path(self):
        with tempfile.TemporaryDirectory() as temp:
            python3 = Path(temp) / "python3"
            python3.touch()

            def which(name):
                return str(python3) if name == "python3" else None

            with mock.patch.dict("os.environ", {"PATH": temp}, clear=True), \
                    mock.patch("harness.tools.osal.python_exe", return_value=None), \
                    mock.patch("harness.tools.osal.which", side_effect=which):
                result = doctor.check_python()
            self.assertEqual(result["status"], "OK")
            self.assertIn(str(python3), result["detail"])

    def test_fake_ip_dns_is_degraded_and_blocks_credential_probe(self):
        def fake_resolution(host, *args, **kwargs):
            return [(None, None, None, None, ("198.18.0.1", 0))]

        settings = SimpleNamespace(jev_endpoint="https://api.typesafe.ai/v1/systemone")
        with mock.patch.object(doctor.socket, "getaddrinfo", side_effect=fake_resolution), \
                mock.patch.object(
                    doctor.config, "resolve_api_key_with_source",
                    return_value=("fixture-secret-do-not-print", {"kind": "environment"})), \
                mock.patch.object(doctor, "_openrouter_key_info") as probe:
            dns = doctor.check_dns(settings)
            result = doctor.check_openrouter_key(dns)

        self.assertEqual(dns["status"], "DEGRADED")
        self.assertNotIn("198.18.0.1", json.dumps(dns))
        self.assertEqual(result["status"], "DEGRADED")
        probe.assert_not_called()

    def test_degraded_only_report_keeps_zero_exit(self):
        settings = SimpleNamespace(
            jev_endpoint="https://api.typesafe.ai/v1/systemone",
            ledger_path="unused-ledger.jsonl", mcp_auth_token=None,
            jev_disabled=False)
        patches = self._fixed_non_network_checks()
        patches[2] = mock.patch.object(
            doctor, "check_dns",
            return_value={"name": "dns", "status": "DEGRADED", "detail": "VPN DNS",
                          "hosts": {doctor.OPENROUTER_HOST: {"class": "public"},
                                    "api.typesafe.ai": {"class": "public"}}})
        patches += [
            mock.patch.object(doctor, "check_openrouter_key", return_value=_ok("openrouter_key")),
            mock.patch.object(doctor, "check_jev_key", return_value=_ok("jev_key")),
        ]
        with self._enter(patches):
            report = doctor.run_doctor(settings)
        self.assertEqual(report["status"], "DEGRADED")
        self.assertEqual(report["exit_code"], 0)

    def test_cli_uses_safe_settings_fallback_when_provider_startup_refuses(self):
        settings = SimpleNamespace(jev_endpoint="https://api.typesafe.ai/v1/systemone",
                                   ledger_path="unused-ledger.jsonl",
                                   mcp_auth_token=None, jev_disabled=False)
        with mock.patch.object(cli, "load_settings",
                               side_effect=[HarnessError("provider is not configured"),
                                            settings]) as load, \
                mock.patch.object(doctor, "run_doctor",
                                  return_value={"status": "OK", "checks": [],
                                                "exit_code": 0}) as run, \
                mock.patch.object(doctor, "print_report"):
            cli.main(["doctor"])
        self.assertEqual(load.call_count, 2)
        self.assertEqual(load.call_args_list[1],
                         mock.call(overrides={"use_fireworks": False}))
        self.assertTrue(run.call_args.kwargs["config_error"])

    def test_cli_uses_minimal_settings_if_normal_and_safe_load_fail(self):
        settings = SimpleNamespace(jev_endpoint="https://api.typesafe.ai/v1/systemone",
                                   ledger_path="unused-ledger.jsonl",
                                   mcp_auth_token=None, jev_disabled=False)
        with mock.patch.object(cli, "load_settings",
                               side_effect=[HarnessError("invalid config"),
                                            HarnessError("still invalid")]), \
                mock.patch.object(doctor, "_minimal_settings",
                                  return_value=settings) as minimal, \
                mock.patch.object(doctor, "run_doctor",
                                  return_value={"status": "OK", "checks": [],
                                                "exit_code": 0}) as run, \
                mock.patch.object(doctor, "print_report"):
            cli.main(["doctor"])
        minimal.assert_called_once_with()
        self.assertTrue(run.call_args.kwargs["config_error"])

    def test_api_key_source_reports_file_environment_and_missing_without_value(self):
        from harness import config

        home = str(Path.home())
        first_file = os.path.join(os.path.expanduser("~/.config/scmorc"),
                                  "openrouter_fusion.env")
        base_env = {"HOME": home, "USERPROFILE": home}
        with mock.patch.dict("os.environ", base_env, clear=True), \
                mock.patch.object(config, "_read_key_file",
                                  return_value="fixture-secret") as read, \
                mock.patch.object(config, "_warn_insecure_keyfile"):
            key, source = config.resolve_api_key_with_source()
        self.assertEqual(key, "fixture-secret")
        self.assertEqual(source, {"kind": "file", "path": first_file})
        read.assert_called_once()

        stderr = io.StringIO()
        env = dict(base_env, OPENROUTER_API_KEY="fixture-secret")
        with mock.patch.dict("os.environ", env, clear=True), \
                mock.patch.object(config, "_read_key_file", return_value=None), \
                contextlib.redirect_stderr(stderr):
            key, source = config.resolve_api_key_with_source()
        self.assertEqual(key, "fixture-secret")
        self.assertEqual(source, {"kind": "environment", "name": "OPENROUTER_API_KEY"})
        self.assertNotIn("fixture-secret", stderr.getvalue())

        with mock.patch.dict("os.environ", base_env, clear=True), \
                mock.patch.object(config, "_read_key_file", return_value=None):
            self.assertEqual(config.resolve_api_key_with_source(), (None, None))


class DoctorChecksTests(unittest.TestCase):
    @staticmethod
    def _settings(**values):
        return SimpleNamespace(**values)

    @staticmethod
    def _response(status=200, body=b"{}"):
        class Response:
            def __init__(self):
                self.status = status
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size=-1):
                return self.body
        return Response()

    @staticmethod
    def _dns(public=True):
        host_class = "public" if public else "non-public"
        return {"hosts": {doctor.OPENROUTER_HOST: {"class": host_class},
                          "api.typesafe.ai": {"class": host_class}}}

    def test_python_ruff_and_endpoint_resolution_failures(self):
        with mock.patch.object(doctor.tools, "resolve_python",
                               side_effect=HarnessError("missing")):
            self.assertEqual(doctor.check_python()["status"], "MISSING")
        with mock.patch.object(doctor.tools, "ruff_argv", return_value=["ruff"]):
            self.assertEqual(doctor.check_ruff()["status"], "OK")
        with mock.patch.object(doctor.tools, "ruff_argv",
                               side_effect=HarnessError("missing")):
            self.assertEqual(doctor.check_ruff()["status"], "MISSING")
        self.assertIsNone(doctor._safe_host("https://[broken"))
        self.assertEqual(doctor._safe_host("https://example.test/path"), "example.test")

    def test_dns_classifies_literals_resolution_failures_and_bad_rows(self):
        self.assertEqual(doctor._dns_class(None), {"class": "unresolved", "count": 0})
        self.assertEqual(doctor._dns_class("8.8.8.8")["class"], "public")
        self.assertEqual(doctor._dns_class("127.0.0.1")["class"], "non-public")
        with mock.patch.object(doctor.socket, "getaddrinfo", return_value=[
                (None, None, None, None, ("8.8.8.8", 0)),
                (None, None, None, None, ("127.0.0.1", 0))]):
            self.assertEqual(doctor._dns_class("example.test")["class"], "mixed")
        with mock.patch.object(doctor.socket, "getaddrinfo", side_effect=OSError):
            self.assertEqual(doctor._dns_class("unresolvable.test")["class"], "unresolved")
        with mock.patch.object(doctor.socket, "getaddrinfo", return_value=[
                (None, None, None, None, ("not-an-ip", 0))]):
            self.assertEqual(doctor._dns_class("bad-row.test"),
                             {"class": "unresolved", "count": 0})

    def test_dns_report_and_host_gate(self):
        settings = self._settings()
        with mock.patch.object(doctor, "_dns_class", return_value={
                "class": "public", "count": 1}):
            report = doctor.check_dns(settings)
        self.assertEqual(report["status"], "OK")
        self.assertTrue(doctor._dns_host_is_public(report, doctor.OPENROUTER_HOST))
        with mock.patch.object(doctor, "_dns_class", return_value={
                "class": "mixed", "count": 2}):
            report = doctor.check_dns(self._settings(jev_endpoint="not a URL"))
        self.assertEqual(report["status"], "DEGRADED")
        self.assertFalse(doctor._dns_host_is_public(report, "missing.test"))

    def test_openrouter_request_and_http_error_are_safe(self):
        response = self._response(200, b'{"data": {"limit": 2}}')
        with mock.patch.object(doctor.urllib.request, "build_opener") as build:
            build.return_value.open.return_value = response
            status, payload = doctor._openrouter_key_info("fixture-secret")
            handler = build.call_args.args[0]
            self.assertIsNone(handler.redirect_request(None, None, 302, "found", {},
                                                       "https://elsewhere.test"))
        self.assertEqual((status, payload), (200, {"data": {"limit": 2}}))
        request = build.return_value.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Authorization"), "Bearer fixture-secret")
        error = urllib.error.HTTPError("https://example.test", 403, "denied", {}, None)
        with mock.patch.object(doctor.urllib.request, "build_opener") as build:
            build.return_value.open.side_effect = error
            self.assertEqual(doctor._openrouter_key_info("fixture-secret"), (403, None))

    def test_number_rejects_bad_values_and_allows_negative_remaining(self):
        for value in (True, None, object(), "nope", float("inf"), float("nan"), -1):
            self.assertIsNone(doctor._number(value))
        self.assertEqual(doctor._number("-1", allow_negative=True), -1)
        self.assertEqual(doctor._number("2.5"), 2.5)

    def test_openrouter_key_missing_degraded_and_success_paths(self):
        with mock.patch.object(doctor.config, "resolve_api_key_with_source",
                               side_effect=OSError):
            self.assertEqual(doctor.check_openrouter_key()["status"], "MISSING")
        with mock.patch.object(doctor.config, "resolve_api_key_with_source",
                               return_value=(None, None)):
            self.assertEqual(doctor.check_openrouter_key()["status"], "MISSING")
        source = {"kind": "environment", "name": "OPENROUTER_API_KEY"}
        with mock.patch.object(doctor.config, "resolve_api_key_with_source",
                               return_value=("fixture-secret", source)), \
                mock.patch.object(doctor, "_openrouter_key_info") as probe:
            result = doctor.check_openrouter_key(self._dns(False))
        self.assertEqual(result["status"], "DEGRADED")
        probe.assert_not_called()
        with mock.patch.object(doctor.config, "resolve_api_key_with_source",
                               return_value=("fixture-secret", source)), \
                mock.patch.object(doctor, "_openrouter_key_info", side_effect=TimeoutError):
            self.assertEqual(doctor.check_openrouter_key(self._dns())["status"], "DEGRADED")
        for status in (401, 403):
            with mock.patch.object(doctor.config, "resolve_api_key_with_source",
                                   return_value=("fixture-secret", source)), \
                    mock.patch.object(doctor, "_openrouter_key_info",
                                      return_value=(status, None)):
                self.assertEqual(doctor.check_openrouter_key(self._dns())["status"], "MISSING")
        for status, payload in ((503, None), (200, []), (200, {}),
                                (200, {"data": []}),
                                (200, {"data": {"limit": "bad"}}),
                                (200, {"data": {"limit": 4, "limit_remaining": 0}})):
            with mock.patch.object(doctor.config, "resolve_api_key_with_source",
                                   return_value=("fixture-secret", source)), \
                    mock.patch.object(doctor, "_openrouter_key_info",
                                      return_value=(status, payload)):
                result = doctor.check_openrouter_key(self._dns())
            self.assertIn(result["status"], ("DEGRADED", "MISSING"))
        with mock.patch.object(doctor.config, "resolve_api_key_with_source",
                               return_value=("fixture-secret", source)), \
                mock.patch.object(doctor, "_openrouter_key_info",
                                  return_value=(200, {"data": {"limit": 4}})):
            result = doctor.check_openrouter_key(self._dns())
        self.assertEqual(result["status"], "OK")
        self.assertIsNone(result["remaining"])

    def test_jev_and_typesafe_checks(self):
        self.assertEqual(doctor.check_jev_key(self._settings(jev_disabled=True))["status"],
                         "DEGRADED")
        with mock.patch.object(doctor.config, "resolve_jev_key", side_effect=OSError):
            self.assertEqual(doctor.check_jev_key(self._settings())["status"], "MISSING")
        with mock.patch.object(doctor.config, "resolve_jev_key", return_value="fixture"):
            self.assertEqual(doctor.check_jev_key(self._settings())["status"], "OK")
        self.assertEqual(doctor.check_jev_key(self._settings(jev_api_key="fixture"))["status"],
                         "OK")
        response = self._response(204)
        with mock.patch.object(doctor.urllib.request, "urlopen", return_value=response):
            self.assertEqual(doctor._typesafe_probe("https://typesafe.test"), 204)
        error = urllib.error.HTTPError("https://typesafe.test", 405, "method", {}, None)
        with mock.patch.object(doctor.urllib.request, "urlopen", side_effect=error):
            self.assertEqual(doctor._typesafe_probe("https://typesafe.test"), 405)
        settings = self._settings(jev_endpoint="https://api.typesafe.ai/v1/systemone")
        self.assertEqual(doctor.check_typesafe(settings, self._dns(False))["status"],
                         "DEGRADED")
        with mock.patch.object(doctor, "_typesafe_probe", return_value=405):
            self.assertEqual(doctor.check_typesafe(settings, self._dns())["status"], "OK")
        with mock.patch.object(doctor, "_typesafe_probe", return_value=503):
            self.assertEqual(doctor.check_typesafe(settings, self._dns())["status"],
                             "DEGRADED")
        with mock.patch.object(doctor, "_typesafe_probe", side_effect=OSError):
            self.assertEqual(doctor.check_typesafe(settings, self._dns())["status"],
                             "DEGRADED")

    def test_driver_health_paths(self):
        from harness import perception_client

        with mock.patch.object(perception_client, "PerceptionAdapter",
                               side_effect=perception_client.PerceptionUnavailable):
            self.assertEqual(doctor.check_driver()["status"], "MISSING")
        with mock.patch.object(perception_client, "PerceptionAdapter") as adapter:
            adapter.return_value.health.return_value = {"status": "up", "version": "v1"}
            result = doctor.check_driver()
        self.assertEqual(result["status"], "OK")
        self.assertIn("v1", result["detail"])
        with mock.patch.object(perception_client, "PerceptionAdapter") as adapter:
            adapter.return_value.health.return_value = {"status": "down"}
            self.assertEqual(doctor.check_driver()["status"], "DEGRADED")
        with mock.patch.object(perception_client, "PerceptionAdapter") as adapter:
            adapter.return_value.health.return_value = None
            self.assertEqual(doctor.check_driver()["status"], "DEGRADED")

    def test_gui_port_status_and_transport_paths(self):
        self.assertEqual(doctor.check_gui_port(True)["status"], "MISSING")
        self.assertEqual(doctor.check_gui_port(0)["status"], "MISSING")
        with mock.patch.object(doctor.urllib.request, "urlopen",
                               return_value=self._response(200)):
            self.assertEqual(doctor.check_gui_port(1234)["status"], "OK")
        auth_error = urllib.error.HTTPError("http://127.0.0.1", 401, "auth", {},
                                             io.BytesIO(b"X-Harness-Auth required"))
        with mock.patch.object(doctor.urllib.request, "urlopen", side_effect=auth_error):
            self.assertEqual(doctor.check_gui_port(1234)["status"], "OK")
        other_error = urllib.error.HTTPError("http://127.0.0.1", 503, "busy", {},
                                              io.BytesIO(b"busy"))
        with mock.patch.object(doctor.urllib.request, "urlopen", side_effect=other_error):
            self.assertEqual(doctor.check_gui_port(1234)["status"], "DEGRADED")
        refused = urllib.error.URLError(ConnectionRefusedError())
        self.assertTrue(doctor._connection_refused(refused))
        with mock.patch.object(doctor.urllib.request, "urlopen", side_effect=refused):
            self.assertEqual(doctor.check_gui_port(1234)["status"], "OK")
        with mock.patch.object(doctor.urllib.request, "urlopen",
                               side_effect=urllib.error.URLError(TimeoutError())):
            self.assertEqual(doctor.check_gui_port(1234)["status"], "DEGRADED")
        with mock.patch.object(doctor.urllib.request, "urlopen", side_effect=OSError):
            self.assertEqual(doctor.check_gui_port(1234)["status"], "DEGRADED")

    def test_ledger_checks_existing_missing_and_temporary_paths(self):
        self.assertEqual(doctor.check_ledger(None)["status"], "MISSING")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            directory = root / "directory"
            directory.mkdir()
            self.assertEqual(doctor.check_ledger(str(directory))["status"], "MISSING")
            missing_parent = root / "absent" / "ledger.jsonl"
            self.assertEqual(doctor.check_ledger(str(missing_parent))["status"], "MISSING")
            ledger = root / "ledger.jsonl"
            ledger.touch()
            self.assertEqual(doctor.check_ledger(str(ledger))["status"], "OK")
            with mock.patch("builtins.open", side_effect=PermissionError):
                self.assertEqual(doctor.check_ledger(str(ledger))["status"], "MISSING")
            with mock.patch.object(doctor.tempfile, "NamedTemporaryFile",
                                   side_effect=OSError):
                self.assertEqual(doctor.check_ledger(str(root / "new.jsonl"))["status"],
                                 "MISSING")
            with mock.patch.object(doctor.os, "unlink", side_effect=OSError):
                result = doctor.check_ledger(str(root / "cleanup.jsonl"))
            self.assertEqual(result["status"], "OK")

    @staticmethod
    def _mcp_stdout(*, init=None, listing=None, call=None):
        frames = [
            {"jsonrpc": "2.0", "id": 1, **(init or {"result": {}})},
            {"jsonrpc": "2.0", "id": 2, **(listing or {"result": {
                "tools": [{"name": "ledger_status"}, {"name": "other"}]}})},
            {"jsonrpc": "2.0", "id": 3, **(call or {"result": {
                "structuredContent": {"verified": {"ok": True}}}})},
        ]
        return "\n".join(json.dumps(frame) for frame in frames)

    def test_mcp_stdio_real_protocol_payload_and_failures(self):
        completed = SimpleNamespace(returncode=0, stdout=self._mcp_stdout())
        with mock.patch.object(doctor.tools, "resolve_python", return_value="python"), \
                mock.patch.object(doctor.osal, "run", return_value=completed) as run:
            result = doctor.check_mcp_stdio("fixture-auth")
        self.assertEqual(result["status"], "OK")
        payload = run.call_args.kwargs["input_text"]
        self.assertIn('"harness_token": "fixture-auth"', payload)
        self.assertNotIn("fixture-auth", result["detail"])
        with mock.patch.object(doctor.tools, "resolve_python",
                               side_effect=HarnessError("missing")):
            self.assertEqual(doctor.check_mcp_stdio()["status"], "MISSING")
        cases = [
            SimpleNamespace(returncode=1, stdout=self._mcp_stdout()),
            SimpleNamespace(returncode=0, stdout="not json"),
            SimpleNamespace(returncode=0, stdout=self._mcp_stdout(init={"error": {}})),
            SimpleNamespace(returncode=0, stdout=self._mcp_stdout(listing={"error": {}})),
            SimpleNamespace(returncode=0, stdout=self._mcp_stdout(listing={
                "result": {"tools": []}})),
            SimpleNamespace(returncode=0, stdout=self._mcp_stdout(call={"error": {}})),
            SimpleNamespace(returncode=0, stdout=self._mcp_stdout(call={"result": {
                "structuredContent": {"verified": {"ok": False}}}})),
            SimpleNamespace(returncode=0, stdout=self._mcp_stdout(call={"result": {
                "structuredContent": {"verified": {"ok": True}}, "isError": True}})),
        ]
        for failed in cases:
            with self.subTest(failed=failed.stdout[:24]), \
                    mock.patch.object(doctor.tools, "resolve_python", return_value="python"), \
                    mock.patch.object(doctor.osal, "run", return_value=failed):
                self.assertEqual(doctor.check_mcp_stdio()["status"], "MISSING")

    def test_minimal_settings_reads_file_environment_and_invalid_config(self):
        with tempfile.TemporaryDirectory() as temp:
            config_file = Path(temp) / "config.json"
            config_file.write_text(json.dumps({
                "jev_endpoint": "https://file.test", "ledger_path": "file-ledger",
                "mcp_auth_token": "file-token"}), encoding="utf-8")
            with mock.patch.object(doctor.config, "CONFIG_DIR", temp), \
                    mock.patch.dict("os.environ", {}, clear=True):
                settings = doctor._minimal_settings()
            self.assertEqual(settings.jev_endpoint, "https://file.test")
            self.assertEqual(settings.ledger_path, "file-ledger")
            self.assertEqual(settings.mcp_auth_token, "file-token")
            with mock.patch.object(doctor.config, "CONFIG_DIR", temp), \
                    mock.patch.dict("os.environ", {
                        "HARNESS_JEV_ENDPOINT": "https://env.test",
                        "HARNESS_LEDGER": "env-ledger",
                        "HARNESS_MCP_AUTH_TOKEN": "env-token",
                        "HARNESS_JEV_DISABLE": "1"}, clear=True):
                settings = doctor._minimal_settings()
            self.assertEqual(settings.jev_endpoint, "https://env.test")
            self.assertEqual(settings.ledger_path, "env-ledger")
            self.assertEqual(settings.mcp_auth_token, "env-token")
            self.assertTrue(settings.jev_disabled)
            config_file.write_text("invalid", encoding="utf-8")
            with mock.patch.object(doctor.config, "CONFIG_DIR", temp), \
                    mock.patch.dict("os.environ", {}, clear=True):
                settings = doctor._minimal_settings()
            self.assertEqual(settings.jev_endpoint, doctor.DEFAULT_JEV_ENDPOINT)

    def test_report_status_config_failure_and_renderers(self):
        settings = self._settings(jev_endpoint="https://api.typesafe.ai/v1/systemone",
                                  ledger_path="unused", mcp_auth_token=None)
        patches = DoctorCliTests()._fixed_non_network_checks()
        patches += [
            mock.patch.object(doctor, "check_openrouter_key", return_value=_ok("openrouter_key")),
            mock.patch.object(doctor, "check_jev_key", return_value=_ok("jev_key")),
        ]
        with DoctorCliTests()._enter(patches):
            report = doctor.run_doctor(settings)
        self.assertEqual((report["status"], report["exit_code"]), ("OK", 0))
        with DoctorCliTests()._enter(patches), mock.patch.object(
                doctor, "check_driver", return_value={"name": "driver", "status": "MISSING",
                                                       "detail": "down"}):
            report = doctor.run_doctor(settings, config_error=True)
        self.assertEqual((report["status"], report["exit_code"]), ("MISSING", 1))
        self.assertEqual(report["checks"][0]["name"], "config")
        rendered = io.StringIO()
        with contextlib.redirect_stdout(rendered):
            doctor.print_report(report, json_output=True)
        self.assertEqual(json.loads(rendered.getvalue()), report)
        rendered = io.StringIO()
        with contextlib.redirect_stdout(rendered):
            doctor.print_report(report)
        self.assertIn("[MISSING] config", rendered.getvalue())
        self.assertIn("Fix:", rendered.getvalue())


if __name__ == "__main__":
    unittest.main()
