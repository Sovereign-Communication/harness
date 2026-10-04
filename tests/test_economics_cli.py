"""The `harness economics` face: parser surface + the two invariants it must
not lose.

EV-0 ships a read-only evidence face. Two things about it are load-bearing
and worth pinning:

* the flags exist against the REAL parser (a renamed flag on a face nobody
  runs in CI is exactly how `rankings.yml` silently produced no artifact for
  a week -- see `tests/test_rankings_cli.py` for that incident);
* it never mutates a pool, a lane default, or a ceiling. This phase is
  evidence only; the ability to *apply* anything is EV-3/EV-4, and a face
  that quietly did it here would land a behaviour change nobody gated.
"""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from harness.cli_parser import build_parser


class ParserSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.parser = build_parser()

    def _opts(self, argv):
        return self.parser.parse_args(argv)

    def test_economics_subcommand_exists(self):
        opts = self._opts(["economics"])
        self.assertIsNone(opts.models)
        self.assertIsNone(opts.probe_model)
        self.assertFalse(opts.record)
        self.assertIsNone(opts.state_path)
        self.assertIsNone(opts.receipt_dir)

    def test_probe_and_record_flags_parse(self):
        opts = self._opts(["economics", "--probe-model", "acme/sol",
                           "--record", "--state-path", "/tmp/e.json"])
        self.assertEqual(opts.probe_model, "acme/sol")
        self.assertTrue(opts.record)
        self.assertEqual(opts.state_path, "/tmp/e.json")

    def test_report_flags_parse(self):
        opts = self._opts(["economics", "--models", "a/b,c/d",
                           "--max-fetches", "5", "--receipt-dir", "receipts"])
        self.assertEqual(opts.models, "a/b,c/d")
        self.assertEqual(opts.max_fetches, 5)
        self.assertEqual(opts.receipt_dir, "receipts")

    def test_command_is_dispatchable(self):
        from harness.cli import _DISPATCH
        self.assertIn("economics", _DISPATCH)


class SplitIdsTests(unittest.TestCase):
    def test_blank_and_empty_inputs_are_none(self):
        from harness.cli import _split_ids
        self.assertIsNone(_split_ids(None))
        self.assertIsNone(_split_ids(""))
        self.assertIsNone(_split_ids(" , ,"))

    def test_ids_are_trimmed_and_order_preserved(self):
        """Trimming is the CLI's job; dedup belongs to the owner below it
        (`fetch_endpoints_for`), which dedupes after stripping variant
        suffixes so `a/m` and `a/m:free` cost one request between them."""
        from harness.cli import _split_ids
        self.assertEqual(_split_ids(" a/b , c/d "), ["a/b", "c/d"])


class ProbeFaceTests(unittest.TestCase):
    """`--record` persists only a conclusive verdict."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self._tmp.name, "economics.json")
        self.addCleanup(self._tmp.cleanup)

    def _run(self, probe_record, argv=None):
        parser = build_parser()
        opts = parser.parse_args(argv or ["economics", "--probe-model",
                                          "acme/sol", "--record",
                                          "--state-path", self.state])
        from harness.cli import _cmd_economics
        buffer = io.StringIO()
        gov = mock.Mock()
        with mock.patch("harness.cli._governor", return_value=("key", gov)), \
             mock.patch("harness.cli._run_discount_probe",
                        return_value=probe_record) as probe:
            with redirect_stdout(buffer):
                _cmd_economics(opts, mock.Mock())
        payload = json.loads(buffer.getvalue())
        return payload, probe

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


class ReadOnlyTests(unittest.TestCase):
    def test_report_face_writes_no_configuration(self):
        """EV-0 is evidence only: the report path may not touch a pool, a
        lane default, or a ceiling."""
        from harness.cli import _cmd_economics
        parser = build_parser()
        opts = parser.parse_args(["economics", "--models", "a/b"])

        settings = mock.Mock()
        gov = mock.Mock()
        gov.cost_by_model.return_value = {}
        with mock.patch("harness.cli._governor", return_value=("key", gov)), \
             mock.patch("harness.cli._ledger", return_value=mock.Mock()), \
             mock.patch("harness.cli._run_meta", return_value={}), \
             mock.patch("harness.cli._economics_report",
                        return_value={"schema": 1}) as report, \
             mock.patch("harness.cli.freeze_jev_settings") as write_config:
            with redirect_stdout(io.StringIO()):
                _cmd_economics(opts, settings)

        self.assertTrue(report.called)
        self.assertFalse(write_config.called)
