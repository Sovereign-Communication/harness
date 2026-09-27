"""PLAT-parity-tests: the site export bundle's bytes, identical everywhere.

The public Proof Bench bundle is the one artifact that leaves this machine,
so "the same ledger exports to the same bytes" is a promise about *bytes*.
The exporter already wrote LF; this test pins the digest so a future edit
that reintroduces a platform newline (or a wall-clock field that leaks into
the canonical core) fails here on whichever platform it appears.
"""
import hashlib
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness.ledger import AutonomyLedger
from harness.site_export import (CONSENT_SCHEMA, export_bundle, write_bundle)

# The bundle's own identity: a frozen clock and a fixed ledger make the
# digest a function of the evidence, not of when or where it was exported.
FROZEN_ISO = "2026-09-26T12:00:00+00:00"
FROZEN_STRFTIME = "20260926T120000.000000"

EXPECTED_SHA256 = "55af4d87936d176fa16b735928c0a7fdff52a84074effb9bf0db112fd4051466"


class _FrozenDatetime(object):
    @staticmethod
    def now(tz=None):
        import datetime as _dt
        return _dt.datetime(2026, 9, 26, 12, 0, 0,
                            tzinfo=_dt.timezone.utc if tz is not None else None)

    @staticmethod
    def strftime(_fmt):
        return FROZEN_STRFTIME


def _write_consent(dirpath):
    path = os.path.join(dirpath, "consent.json")
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump({"schema": CONSENT_SCHEMA,
                   "accepted_at": "2026-09-26T12:00:00Z",
                   "surface": "site_optin_page"}, handle)
    return path


def _write_ledger(dirpath):
    path = os.path.join(dirpath, "ledger.jsonl")
    with mock.patch("harness.ledger.datetime", _FrozenDatetime):
        ledger = AutonomyLedger(path)
        ledger.append("consent_accept", task_id="parity-site", model="test/paid-flash",
                      surface="site", reason="opted in")
        ledger.append("dispatch_start", task_id="parity-site",
                      model="test/paid-flash", surface="site")
        ledger.append("model_result", task_id="parity-site",
                      model="test/paid-flash", cost=0.000042,
                      tokens_in=1000, tokens_out=200, surface="site")
        ledger.append("jev_eval", task_id="parity-site", model="test/paid-flash",
                      cost=0.000001, is_fallback=False, confidence=0.91,
                      surface="site")
        ledger.append("verify_round", task_id="parity-site",
                      model="test/paid-flash", passed=True, surface="site")
        ledger.append("complete", task_id="parity-site", model="test/paid-flash",
                      status="ok", rounds=1, surface="site")
    return path


class SiteExportParityTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.ledger_path = _write_ledger(self.dir.name)
        self.consent_path = _write_consent(self.dir.name)
        self.out_path = os.path.join(self.dir.name, "bundle.json")

    def _export(self):
        with mock.patch("harness.site_export._utc_now_iso",
                        return_value=FROZEN_ISO.replace("+00:00", "Z")):
            bundle = export_bundle(self.ledger_path, self.consent_path)
        write_bundle(bundle, self.out_path)
        with open(self.out_path, "rb") as handle:
            return bundle, handle.read()

    def test_bundle_bytes_are_byte_identical_across_platforms(self):
        _, raw = self._export()
        self.assertEqual(
            hashlib.sha256(raw).hexdigest(), EXPECTED_SHA256,
            "the exported bundle's bytes changed; re-pin only when the content "
            "change is intended and all three CI platforms agree")

    def test_bundle_is_lf_only(self):
        _, raw = self._export()
        self.assertNotIn(b"\r\n", raw)

    def test_bundle_id_is_content_not_wall_clock(self):
        bundle, _ = self._export()
        again, _raw = self._export()
        self.assertEqual(bundle["bundle_id"], again["bundle_id"])
        self.assertEqual(bundle["generated_at"], FROZEN_ISO.replace("+00:00", "Z"))
        self.assertEqual(len(bundle["runs"]), 1)
        run = bundle["runs"][0]
        self.assertEqual(run["primary_model"], "test/paid-flash")
        self.assertEqual(run["outcome"], "pass")
        self.assertEqual(run["rounds"], 1)
        self.assertTrue(run["gated"])
        self.assertEqual(run["ts"], FROZEN_ISO)
        self.assertEqual(run["jev_evals"], {"count": 1, "cost": 0.000001,
                                            "fallback": 0})

    def test_exported_json_round_trips(self):
        _, raw = self._export()
        parsed = json.loads(raw.decode("utf-8"))
        self.assertEqual(parsed["schema"], bundle_schema())
        self.assertIn("chain", parsed)


def bundle_schema():
    from harness.site_export import BUNDLE_SCHEMA
    return BUNDLE_SCHEMA


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
