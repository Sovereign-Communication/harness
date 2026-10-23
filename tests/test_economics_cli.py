"""The `harness economics` face: the EV-0a discount-truth probe and nothing else.

EV-0 ships one CLI face, and it exists for one reason: a probe nobody can run
is not shipped. The shortlist-pricing report that used to sit beside it
(`--models`, `--max-fetches`, `--receipt-dir`, `_split_ids`, and a receipt
writer) was unrequested scope and is gone with its canon rows (DF-EV-12), so
these tests cover what remains and pin that the removed flags are really gone.

Two things about what remains are load-bearing:

* the flags exist against the REAL parser (a renamed flag on a face nobody
  runs in CI is exactly how `rankings.yml` silently produced no artifact for
  a week -- see `tests/test_rankings_cli.py` for that incident);
* `--record` is the only write, and it writes repo evidence -- never a pool, a
  lane default, or a ceiling. EV-0 is evidence only; the ability to *apply*
  anything is EV-3/EV-4, and a face that quietly did it here would land a
  behaviour change nobody gated.
"""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from harness.cli_parser import build_parser
from harness.errors import HarnessError


class ParserSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.parser = build_parser()

    def test_probe_and_record_flags_parse(self):
        opts = self.parser.parse_args(["economics", "--probe-model", "acme/sol",
                                       "--record", "--verdict-path",
                                       "/tmp/e.json"])
        self.assertEqual(opts.probe_model, "acme/sol")
        self.assertTrue(opts.record)
        self.assertEqual(opts.verdict_path, "/tmp/e.json")

    def test_the_removed_report_flags_are_really_gone(self):
        """The report was unrequested surface (DF-EV-12). Its flags must not
        linger as no-ops someone keeps scripting against."""
        opts = self.parser.parse_args(["economics"])
        for flag in ("models", "max_fetches", "receipt_dir"):
            self.assertFalse(hasattr(opts, flag), f"--{flag} still parses")
        for flag in ("--models", "--max-fetches", "--receipt-dir"):
            with self.assertRaises(SystemExit):
                with redirect_stdout(io.StringIO()), \
                        redirect_stderr(io.StringIO()):
                    self.parser.parse_args(["economics", flag, "x"])

    def test_the_verdict_flag_names_repo_evidence_not_local_state(self):
        """The flag this replaced was `--state-path`, pointed at
        ~/.config/harness/economics.json, and that naming is part of what let
        the gate drift into machine-local state unnoticed."""
        buffer = io.StringIO()
        with redirect_stdout(buffer), self.assertRaises(SystemExit):
            build_parser().parse_args(["economics", "--help"])
        text = buffer.getvalue()
        self.assertIn("EV0A_DISCOUNT_SEMANTICS.json", text)
        self.assertIn("repo evidence", text)
        self.assertNotIn("~/.config", text)

    def test_command_is_dispatchable(self):
        from harness.cli import _DISPATCH
        self.assertIn("economics", _DISPATCH)


class ProbeFaceTests(unittest.TestCase):
    """`--record` persists only a conclusive verdict, as committed evidence."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self._tmp.name, "economics.json")
        self.addCleanup(self._tmp.cleanup)

    def _run(self, probe_record, argv=None, record_flag=True):
        parser = build_parser()
        argv = argv or ["economics", "--probe-model", "acme/sol"]
        if record_flag:
            argv = argv + ["--record", "--verdict-path", self.state]
        opts = parser.parse_args(argv)
        from harness.cli import _cmd_economics
        buffer = io.StringIO()
        gov = mock.Mock()
        with mock.patch("harness.cli._governor", return_value=("key", gov)), \
             mock.patch("harness.cli._run_discount_probe",
                        return_value=probe_record) as probe:
            with redirect_stdout(buffer):
                _cmd_economics(opts, mock.Mock())
        return json.loads(buffer.getvalue()), probe

    def test_conclusive_verdict_is_recorded(self):
        record = {"semantics": "listed_is_effective", "model": "acme/sol",
                  "fingerprint": {"model": "acme/sol", "max_discount": 0.5}}
        payload, _probe = self._run(record)
        self.assertEqual(payload["semantics"], "listed_is_effective")
        self.assertTrue(os.path.isfile(self.state))
        with open(self.state, encoding="utf-8") as stream:
            self.assertEqual(json.load(stream)["semantics"],
                             "listed_is_effective")

    def test_ambiguous_verdict_is_not_recorded(self):
        """A refusal to decide is not a decision; caching one would let a
        later run believe the price gate had been satisfied."""
        record = {"semantics": "ambiguous", "model": "acme/sol",
                  "reason": "provider prices alias"}
        payload, _probe = self._run(record)
        self.assertIsNone(payload["recorded"])
        self.assertEqual(payload["probe"]["semantics"], "ambiguous")
        self.assertFalse(os.path.exists(self.state))

    def test_unresolved_verdict_is_not_recorded(self):
        record = {"semantics": "unresolved", "model": "acme/sol",
                  "reason": "no provider-reported cost"}
        payload, _probe = self._run(record)
        self.assertIsNone(payload["recorded"])
        self.assertFalse(os.path.exists(self.state))

    def test_the_probe_without_record_writes_nothing(self):
        """EV-0 is evidence only: the probe itself must not touch the tree,
        let alone configuration."""
        from harness.cli import _cmd_economics
        record = {"semantics": "listed_is_effective", "model": "acme/sol",
                  "fingerprint": {"model": "acme/sol", "max_discount": 0.5}}
        settings = mock.Mock()
        gov = mock.Mock()
        with mock.patch("harness.cli._governor", return_value=("key", gov)), \
             mock.patch("harness.cli._run_discount_probe",
                        return_value=record), \
             mock.patch("harness.cli.freeze_jev_settings") as write_config:
            with redirect_stdout(io.StringIO()):
                _cmd_economics(build_parser().parse_args(
                    ["economics", "--probe-model", "acme/sol"]), settings)
        self.assertFalse(os.path.exists(self.state))
        self.assertFalse(write_config.called)
        self.assertFalse(settings.write.called if hasattr(settings, "write")
                         else False)

    def test_a_bare_invocation_refuses_and_says_why(self):
        """There is no report to produce any more, so the face says what it
        does instead of silently emitting an empty artifact."""
        from harness.cli import _cmd_economics
        opts = build_parser().parse_args(["economics"])
        with mock.patch("harness.cli._governor") as governor:
            with self.assertRaises(HarnessError) as ctx:
                _cmd_economics(opts, mock.Mock())
        message = str(ctx.exception)
        self.assertIn("--probe-model", message)
        self.assertIn("EV-1", message)
        # ...and it refuses BEFORE any key lookup or spend preflight.
        self.assertFalse(governor.called)
