"""Drop-fails tests for the #292 driver_core drift check."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from scripts.check_driver_core_drift import (
    DEFAULT_PIN, DEFAULT_VENDORED, DriftError, check_drift, main,
    version_from_text,
)

PIN = {
    "repository": "Treystu/driver-core",
    "version": "3.5.0",
    "commit": "47d4362",
    "source": "https://raw.githubusercontent.com/Treystu/driver-core/main/driver_core/__init__.py",
}
VENDORED = '__version__ = "3.5.0"\n'


class DriftFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vendored = self.root / "driver_core" / "__init__.py"
        self.vendored.parent.mkdir()
        self.vendored.write_text(VENDORED, encoding="utf-8")
        self.pin = self.root / "UPSTREAM.json"
        self.write_pin(PIN)

    def write_pin(self, payload):
        self.pin.write_text(json.dumps(payload), encoding="utf-8")

    def check(self, **kwargs):
        return check_drift(pin_path=self.pin, vendored_path=self.vendored, **kwargs)


class VersionParsingTests(unittest.TestCase):
    def test_reads_single_and_double_quoted_versions(self):
        self.assertEqual(version_from_text('__version__ = "3.5.0"'), "3.5.0")
        self.assertEqual(version_from_text("__version__ = '3.5.0'"), "3.5.0")

    def test_missing_version_is_a_named_error(self):
        with self.assertRaises(DriftError):
            version_from_text("VERSION = '3.5.0'", "fixture")


class CheckTests(DriftFixture):
    def test_shipping_pin_matches_the_vendored_copy(self):
        result = check_drift()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["vendored"], "3.5.0")
        self.assertEqual(result["pinned"], "3.5.0")

    def test_pin_mismatch_fails_offline(self):
        self.write_pin(dict(PIN, version="3.4.0"))
        result = self.check()
        self.assertFalse(result["ok"])
        self.assertIn("pin declares 3.4.0", " ".join(result["failures"]))

    def test_vendored_mismatch_fails(self):
        self.vendored.write_text('__version__ = "3.4.0"\n', encoding="utf-8")
        result = self.check()
        self.assertFalse(result["ok"])
        self.assertIn("vendored driver_core is 3.4.0", result["failures"][0])

    def test_remote_match_passes_and_mismatch_fails(self):
        matched = self.check(remote_text='__version__ = "3.5.0"\n')
        self.assertTrue(matched["ok"], matched)
        self.assertTrue(matched["remote_checked"])
        mismatched = self.check(remote_text='__version__ = "3.6.0"\n')
        self.assertFalse(mismatched["ok"])
        self.assertIn("upstream driver_core is 3.6.0", " ".join(mismatched["failures"]))

    def test_remote_fetch_failure_is_a_drift_failure(self):
        def fails(url):
            raise OSError("network unavailable")
        result = self.check(remote_url=PIN["source"], fetch=fails)
        self.assertFalse(result["ok"])
        self.assertIn("remote_unavailable", " ".join(result["failures"]))

    def test_remote_without_version_is_a_failure(self):
        result = self.check(remote_text="VERSION = '3.5.0'\n")
        self.assertFalse(result["ok"])
        self.assertIn("does not declare", " ".join(result["failures"]))

    def test_malformed_pin_fails_closed(self):
        self.pin.write_text("not json", encoding="utf-8")
        result = self.check()
        self.assertFalse(result["ok"])
        self.assertIn("not valid JSON", " ".join(result["failures"]))

    def test_cli_exit_codes_and_json(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main([
                "--pin", str(self.pin), "--vendored", str(self.vendored),
                "--json"]), 0)
        self.assertEqual(json.loads(out.getvalue())["ok"], True)
        self.write_pin(dict(PIN, version="3.4.0"))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main([
                "--pin", str(self.pin), "--vendored", str(self.vendored)]), 1)

    def test_shipping_docs_and_ci_name_the_drift_check(self):
        docs = (DEFAULT_PIN.parents[1] / "docs" / "driver.md").read_text(
            encoding="utf-8")
        workflow = (DEFAULT_PIN.parents[1] / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8")
        self.assertIn("`3.5.0` as vendored", docs)
        self.assertIn("scripts/check_driver_core_drift.py", docs)
        self.assertNotIn("3.4.0 as vendored", docs)
        self.assertNotIn("not yet in CI", docs)
        self.assertIn("scripts/check_driver_core_drift.py --remote", workflow)
        self.assertEqual(DEFAULT_VENDORED.name, "__init__.py")


if __name__ == "__main__":
    unittest.main()
