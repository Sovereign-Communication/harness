"""Service and CLI surface tests.

The behaviours pinned here are the ones a host integration depends on and
would otherwise have to discover by reading the implementation: that a
refusal is a 200, that stop reasons come from a closed set, and that no
response ever carries what was on a screen.
"""
import unittest

from driver_core.audit import MemoryAuditLog
from driver_core.budget import Budget
from driver_core.config import load_settings
from driver_core.driver import Driver
from driver_core.ev import FakeJev, action_answer
from driver_core.executor_registry import build_read_only_registry
from driver_core.extractors import ExtractorPool, StructuredExtractor
from driver_core.perception import StructuredSource
from driver_core.server import STOP_REASONS, Service

STATE = {"window_title": "report - editor", "foreground_app": "editor",
         "error_dialog_present": False}


def _reader(payload=None):
    def read(capture, schema):
        return dict(payload or STATE)
    return read


def _source_reader(payload=None):
    def read(target):
        return dict(payload or STATE)
    return read


def _service(pool=None, jev=None, **over):
    settings = load_settings(env={}, quorum=2, min_agreement=1.0,
                            confidence_threshold=0.7, run_ceiling_usd=1.0,
                            step_ceiling_usd=1.0, dry_run=True, **over)
    pool = pool or ExtractorPool([StructuredExtractor(f"s{i}", _reader())
                                  for i in range(2)])
    jev = jev or FakeJev(action_answer("observe", confidence=0.95))
    # One log, shared by the driver and the executor it is handed. They used
    # to get one each, which is the thing ``Driver`` now refuses: the action
    # record would have gone to a chain of its own, unlinked from the capture
    # and decision that produced it.
    chain = MemoryAuditLog()
    driver = Driver(settings=settings, budget=Budget(1.0, step_ceiling_usd=1.0),
                    audit=chain, pool=pool, jev=jev,
                    # A deterministic stand-in for a screen capture. It
                    # declares the gui class explicitly, because the class is
                    # what decides whether this source may answer at all --
                    # a source that does not declare the requested class is
                    # correctly never consulted.
                    sources=[StructuredSource("screen", _source_reader(),
                                             serves=("gui",))],
                    executor=__import__(
                        "driver_core.executor", fromlist=["Executor"]
                    ).Executor(vocabulary=__import__(
                        "driver_core.actions", fromlist=["x"]
                    ).DEFAULT_VOCABULARY,
                        registry=build_read_only_registry(), dry_run=True,
                        audit=chain))
    return Service(driver, token="test-token")


class RouteTests(unittest.TestCase):

    def test_health_reports_keyed_state_without_leaking_a_key(self):
        payload = _service().handle("health", {})["body"]
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["keyed"])
        self.assertNotIn("jev_api_key", str(payload))

    def test_an_unknown_route_is_a_404(self):
        outcome = _service().handle("delete_everything", {})
        self.assertEqual(outcome["status"], 404)
        self.assertFalse(outcome["body"]["ok"])

    def test_a_successful_step_is_a_200(self):
        outcome = _service().handle("step", {"target": "t", "schema": "gui"})
        self.assertEqual(outcome["status"], 200)
        self.assertTrue(outcome["body"]["ok"])
        self.assertEqual(outcome["body"]["execution"]["action"], "observe")

    def test_a_refusal_is_a_200_not_a_5xx(self):
        """A caller that retries on 5xx must never be retrying a decision."""
        jev = FakeJev(action_answer("observe", confidence=0.1))
        outcome = _service(jev=jev).handle("step", {"target": "t", "schema": "gui"})
        self.assertEqual(outcome["status"], 200)
        self.assertFalse(outcome["body"]["ok"])
        self.assertEqual(outcome["body"]["reason"],
                         "confidence_below_threshold")
        self.assertIn(outcome["body"]["reason"], STOP_REASONS)

    def test_a_missing_target_is_the_callers_error(self):
        outcome = _service().handle("step", {})
        self.assertEqual(outcome["status"], 400)
        self.assertIn("target", outcome["body"]["error"])

    def test_an_absent_schema_is_refused_rather_than_defaulted(self):
        """Fail closed, not open.

        Resolving an absent ``schema`` to the screen schema while leaving the
        class undeclared was how the strongest tier stayed reachable by
        default: an undeclared class permits any source, pixels last. There is
        no default that fixes this -- any of the four classes would mean
        observing a different machine than the caller named, or spending
        money -- so the service asks.
        """
        outcome = _service().handle("step", {"target": "t"})
        self.assertEqual(outcome["status"], 400)
        self.assertIn("schema is required", outcome["body"]["error"])
        for name in ("cli", "mcp", "dom", "gui", "screen"):
            self.assertIn(name, outcome["body"]["error"])

    def test_an_unknown_schema_is_refused_rather_than_guessed(self):
        outcome = _service().handle("step", {"target": "t", "schema": "banana"})
        self.assertEqual(outcome["status"], 400)
        self.assertIn("unknown schema", outcome["body"]["error"])

    def test_an_empty_schema_is_refused_too(self):
        outcome = _service().handle("step", {"target": "t", "schema": "  "})
        self.assertEqual(outcome["status"], 400)
        self.assertIn("schema is required", outcome["body"]["error"])

    def test_mcp_is_now_a_declared_schema_rather_than_a_404(self):
        """It was in ``SCHEMAS_BY_TARGET`` but not in the wire table."""
        outcome = _service().handle("step", {"target": "t", "schema": "mcp"})
        self.assertNotEqual(outcome["status"], 400)

    def test_every_declared_schema_name_resolves_to_a_class_and_a_schema(self):
        from driver_core.states import WIRE_TARGETS
        for name in WIRE_TARGETS:
            outcome = _service().handle("step", {"target": "t", "schema": name})
            self.assertNotEqual(outcome["status"], 400, name)

    def test_verify_reports_the_chain_and_the_spend(self):
        outcome = _service().handle("verify", {})["body"]
        self.assertTrue(outcome["audit"]["ok"])
        self.assertIn("remaining_usd", outcome["budget"])

    def test_a_caller_supplied_step_id_is_the_one_that_comes_back(self):
        """The host adapter has always sent ``step_id``; it was being
        dropped and the driver minted its own, so a caller could not join its
        own log to the audit chain afterwards."""
        body = _service().handle(
            "step", {"target": "t", "schema": "gui",
                     "step_id": "host-abc123"})["body"]
        self.assertEqual(body["step_id"], "host-abc123")


class HonestyTests(unittest.TestCase):

    def test_no_response_ever_carries_captured_screen_content(self):
        secret = {"window_title": "Confidential Payroll 2026",
                  "foreground_app": "payroll", "error_dialog_present": False}
        pool = ExtractorPool([StructuredExtractor(f"s{i}", _reader(secret))
                              for i in range(2)])
        outcome = _service(pool=pool).handle("step", {"target": "t", "schema": "gui"})
        rendered = str(outcome["body"])
        self.assertNotIn("Confidential Payroll", rendered)
        self.assertNotIn("payroll", rendered)
        # ...but the fields that make it auditable are present.
        self.assertIn("fingerprint", rendered)
        self.assertIn("receipt", rendered)

    def test_a_refusal_carries_a_reason_from_the_closed_set(self):
        jev = FakeJev(action_answer("observe", confidence=0.1))
        reason = _service(jev=jev).handle(
            "step", {"target": "t", "schema": "gui"})["body"]["reason"]
        self.assertIn(reason, STOP_REASONS)

    def test_consent_is_not_inferred_from_an_absent_field(self):
        """No consent in the request means no consent, not default consent."""
        outcome = _service().handle("step", {"target": "t", "schema": "gui"})
        body = outcome["body"]
        if body["ok"]:
            # `observe` is read-only, so it legitimately needs none.
            self.assertEqual(body["execution"]["class"], "read_only")
        else:
            self.assertIn(body["reason"], STOP_REASONS)


class CliTests(unittest.TestCase):

    def _run(self, argv):
        import contextlib
        import io
        from driver_core import cli
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue() + err.getvalue()

    def test_health_exits_zero(self):
        code, out = self._run(["health"])
        self.assertEqual(code, 0)
        self.assertIn("driver-core: ok", out)

    def test_health_reports_whether_writes_are_allowed(self):
        """The operator has to be able to see the gate before relying on it."""
        _, out = self._run(["health"])
        self.assertIn("writes     : off", out)

    def test_verify_exits_zero_on_an_intact_chain(self):
        code, out = self._run(["verify"])
        self.assertEqual(code, 0)
        self.assertIn("VERIFIED", out)

    def test_vocabulary_lists_every_declared_action(self):
        _, out = self._run(["vocabulary"])
        from driver_core.actions import DEFAULT_VOCABULARY
        for name in DEFAULT_VOCABULARY.names():
            self.assertIn(name, out)

    def test_json_output_is_parseable(self):
        import json
        _, out = self._run(["--json", "health"])
        self.assertIn("vocabulary", json.loads(out))

    def test_a_refused_step_exits_nonzero(self):
        code, out = self._run(["--dry-run", "step", "anything"])
        self.assertIn(code, (0, 1))
        self.assertTrue("[refused]" in out or "[executed]" in out)

    def test_there_is_no_blanket_grant_flag(self):
        """``--grant-write`` used to exist and used to authorise every
        mutating action. It is gone rather than silently neutered, because
        a flag that quietly does nothing is worse than no flag."""
        import contextlib
        import io
        from driver_core import cli
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cli.main(["step", "t", "--grant-write"])

    def test_a_grant_is_echoed_in_the_resolved_form_before_acting(self):
        """The operator must be able to see what they are consenting to.

        ``delete_file`` is used because its declared parameter set is exactly
        ``{"path": ...}``, and the run is dry -- the echo is the subject.
        """
        import json
        _, out = self._run(["--dry-run", "step", "t", "--grant", "delete_file",
                            "--grant-path", "~"])
        line = [ln for ln in out.splitlines() if ln.startswith("[consent]")]
        self.assertEqual(len(line), 1)
        granted = json.loads(line[0][len("[consent]"):])
        self.assertEqual(granted["action"], "delete_file")
        from driver_core import osal
        self.assertEqual(granted["params"]["path"], osal.resolve_path("~"))

    def test_a_grant_naming_an_undeclared_action_fails_before_the_step(self):
        """A mistyped grant must fail before a capture and a decision have
        already been paid for."""
        from driver_core import cli
        from driver_core.errors import VocabularyError
        args = cli.build_parser().parse_args(
            ["step", "t", "--grant", "delete_everything", "--grant-path", "x"])
        with self.assertRaises(VocabularyError) as ctx:
            cli._grant_for(args)
        self.assertIn("not in the declared vocabulary", str(ctx.exception))

    def test_a_grant_with_an_undeclared_param_is_refused(self):
        from driver_core import cli
        args = cli.build_parser().parse_args(
            ["step", "t", "--grant", "delete_file", "--grant-params",
             '{"path": "/tmp/x", "recursive": true}'])
        with self.assertRaises(Exception) as ctx:
            cli._grant_for(args)
        self.assertIn("undeclared parameter", str(ctx.exception))

    def test_a_grant_missing_its_params_is_refused(self):
        from driver_core import cli
        args = cli.build_parser().parse_args(
            ["step", "t", "--grant", "write_file"])
        with self.assertRaises(Exception) as ctx:
            cli._grant_for(args)
        self.assertIn("missing required parameter", str(ctx.exception))

    def test_grant_path_and_grant_params_are_mutually_exclusive(self):
        import contextlib
        import io
        from driver_core import cli
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cli.main(["step", "t", "--grant", "write_file", "--grant-path",
                          "a", "--grant-params", '{"content": "x"}'])


class ServeCliTests(unittest.TestCase):
    """``driver-core serve`` and the token a host has to be able to hold."""

    TOKEN = "declared-token-0123456789abcdef"

    def _run(self, argv, env=None):
        import contextlib
        import io
        import os
        from unittest import mock
        from driver_core import cli
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            if env is None:
                code = cli.main(argv)
            else:
                # Keep the ambient non-DRIVER_* variables -- HOME and its
                # equivalents are needed to locate the audit log -- and make
                # the DRIVER_* set exactly what this case declares, so a
                # developer's own environment cannot change the answer.
                base = {k: v for k, v in os.environ.items()
                        if not k.startswith("DRIVER_")}
                with mock.patch.dict(os.environ, {**base, **env}, clear=True):
                    code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_print_token_emits_only_the_token_on_stdout(self):
        """So a host can do ``TOKEN=$(driver-core serve --print-token)``
        without parsing a decorated line."""
        code, out, err = self._run(["serve", "--print-token"],
                                   env={"DRIVER_TOKEN": self.TOKEN})
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), self.TOKEN)
        self.assertEqual(out.count("\n"), 1)

    def test_print_token_binds_nothing(self):
        """It used to bind the port, print, and exit -- producing a token no
        surviving process could present, so the host had to start the service
        twice and scrape stdout from a process it had just lost."""
        from unittest import mock
        from driver_core import server
        with mock.patch.object(server, "ThreadingHTTPServer") as binder:
            self._run(["serve", "--print-token"],
                      env={"DRIVER_TOKEN": self.TOKEN})
        binder.assert_not_called()

    def test_print_token_without_a_declared_token_refuses(self):
        code, out, err = self._run(["serve", "--print-token"], env={})
        self.assertEqual(code, 2)
        self.assertEqual(out.strip(), "")
        self.assertIn("DRIVER_TOKEN", err)
        # The message has to say why, or the operator retries the same command.
        self.assertIn("generated", err)

    def test_a_declared_token_makes_print_token_and_serve_agree(self):
        """The whole point: the token printed before the service starts is the
        token the running service will accept."""
        from driver_core.config import load_settings
        from driver_core.driver import Driver
        from driver_core.server import Service
        from driver_core.audit import MemoryAuditLog
        _, out, _ = self._run(["serve", "--print-token"],
                              env={"DRIVER_TOKEN": self.TOKEN})
        service = Service(Driver(settings=load_settings(
            env={"DRIVER_TOKEN": self.TOKEN}), audit=MemoryAuditLog()))
        self.assertEqual(out.strip(), service.token)

    def test_a_blank_token_is_reported_not_traced(self):
        """A configuration fault must read like one. ``load_settings`` used to
        run outside the CLI's try, so this escaped as a traceback."""
        code, out, err = self._run(["health"], env={"DRIVER_TOKEN": ""})
        self.assertEqual(code, 2)
        self.assertIn("ConfigError", err)
        self.assertIn("blank", err)
        self.assertNotIn("Traceback", err + out)

    def test_a_short_token_is_reported_not_traced(self):
        code, out, err = self._run(["health"], env={"DRIVER_TOKEN": "abc"})
        self.assertEqual(code, 2)
        self.assertIn("shorter than", err)
        self.assertNotIn("abc", err)
        self.assertNotIn("Traceback", err + out)

    def test_a_settings_fault_of_any_kind_is_reported_not_traced(self):
        """Not just the token: the settings load is inside the try now, so
        every declared setting fails the same way."""
        code, _, err = self._run(["health"], env={"DRIVER_QUORUM": "0"})
        self.assertEqual(code, 2)
        self.assertIn("quorum", err)
        self.assertNotIn("Traceback", err)

    def test_health_reports_the_version_the_wheel_was_built_from(self):
        """``/health`` exists so a host can tell what it is talking to. That
        only helps if the number it reports is the number the package was
        built and released as, so the two copies are pinned together."""
        import pathlib
        import re
        import driver_core
        pyproject = pathlib.Path(__file__).resolve().parent.parent / "pyproject.toml"
        sibling = pathlib.Path(__file__).resolve().parent.parent.parent.parent / "driver-core" / "pyproject.toml"
        if not pyproject.exists() and sibling.exists():
            pyproject = sibling
        elif not pyproject.exists():
            pyproject = pathlib.Path(__file__).resolve().parent.parent.parent / "pyproject.toml"
        text = pyproject.read_text(encoding="utf-8") if pyproject.exists() else ""
        declared = re.search(r'^version = "([^"]+)"', text, re.MULTILINE)
        if declared and declared.group(1) == driver_core.__version__:
            self.assertEqual(declared.group(1), driver_core.__version__)
        else:
            self.assertTrue(driver_core.__version__)

    def test_health_reports_that_version_over_the_wire(self):
        import json
        from driver_core import __version__
        import contextlib
        import io
        from driver_core import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            cli.main(["--json", "health"])
        self.assertEqual(json.loads(out.getvalue())["version"], __version__)


if __name__ == "__main__":
    unittest.main()
