"""Hermetic CLI coverage for the real ``harness doctor`` invocation."""
import contextlib
import io
import json
import os
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
