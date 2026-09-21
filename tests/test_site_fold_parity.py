"""Fold-parity tests: the Python rollup fold vs the worker's JS fold.

The worker folds accepted bundles into a KV rollup at the edge; the nightly
CI rebuild recomputes authoritatively from D1 with the Python fold. These
tests pin the parity twice: (1) against shared fixture vectors both sides
consume, (2) live, by executing the worker's actual JS through node when
available (skipped cleanly when node is absent, e.g. minimal CI).
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness.site_aggregate import fold_rollup

_VECTORS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "fixtures", "site_fold_vectors.json")
_WORKER_JS = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "site", "worker", "index.js")


def _cases():
    with open(_VECTORS, encoding="utf-8") as f:
        return json.load(f)["cases"]


def _worker_url():
    r"""file:// URL form of the worker path (ESM on Windows rejects C:\ paths)."""
    return "file:///" + _WORKER_JS.replace("\\", "/").lstrip("/").replace(
        " ", "%20") if os.name == "nt" else "file://" + quote(_WORKER_JS)


class FoldParityPythonTests(unittest.TestCase):
    def test_python_fold_matches_expected_vectors(self):
        for case in _cases():
            with self.subTest(case=case["name"]):
                out = fold_rollup(case["previous"], case["bundle"])
                self.assertEqual(out, case["expected"])

    def test_fold_is_pure_and_chainable(self):
        case = _cases()[0]
        first = fold_rollup(case["previous"], case["bundle"])
        again = fold_rollup(case["previous"], case["bundle"])
        self.assertEqual(first, again)
        chained = fold_rollup(first, case["bundle"])  # idempotent per bundle_id
        self.assertEqual(chained["contributors"], first["contributors"])


class FoldParityWorkerTests(unittest.TestCase):
    """Execute the worker's real foldRollup over the same vectors via node."""

    def setUp(self):
        self.node = shutil.which("node")
        if not self.node:
            self.skipTest("platform: node not available; JS parity asserted in CI")

    def _run_worker_fold(self, previous, bundle):
        harness_js = (
            f"import {{ foldRollup }} from '{_worker_url()}';\n"
            f"const previous = {json.dumps(previous)};\n"
            f"const bundle = {json.dumps(bundle)};\n"
            f"process.stdout.write(JSON.stringify(foldRollup(previous, bundle)));\n")
        with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False) as f:
            f.write(harness_js)
            path = f.name
        try:
            proc = subprocess.run([self.node, path], capture_output=True,
                                  text=True, timeout=30)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        if proc.returncode != 0:
            self.fail(f"worker fold failed: {proc.stderr[-300:]}")
        return json.loads(proc.stdout)

    def test_worker_fold_matches_expected_vectors(self):
        for case in _cases():
            with self.subTest(case=case["name"]):
                out = self._run_worker_fold(case["previous"], case["bundle"])
                self.assertEqual(out, case["expected"])


class WorkerValidationParityTests(unittest.TestCase):
    """The worker's essential validate/floor mirrors must match the Python
    contract on the vectors that matter (refusals + floor scan order)."""

    def setUp(self):
        if not shutil.which("node"):
            self.skipTest("platform: node not available; JS parity asserted in CI")

    def _eval(self, expr):
        path = tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False)
        path.write(
            f"import {{ validateBundle, tierFloorForGoal }} from "
            f"'{_worker_url()}';\n"
            f"process.stdout.write(JSON.stringify({expr}));\n")
        path.close()
        try:
            proc = subprocess.run([shutil.which("node"), path.name],
                                  capture_output=True, text=True, timeout=30)
        finally:
            os.unlink(path.name)
        self.assertEqual(proc.returncode, 0, proc.stderr[-300:])
        return json.loads(proc.stdout)

    def test_worker_accepts_valid_bundle_shape(self):
        bundle = {
            "schema": "site-bundle-v1", "bundle_id": "0123456789abcdef",
            "consent": {"schema": "site-consent-v1",
                        "accepted_at": "2026-09-21T00:00:00Z",
                        "surface": "test"},
            "chain": {"verified_claim": True, "head_hash": "h", "entries": 1},
            "runs": [{"run_id": "r", "task_ref": "t", "lane": "bench",
                      "outcome": "pass", "cost": 0.0, "gated": True}],
        }
        self.assertIsNone(self._eval(f"validateBundle({json.dumps(bundle)})"))

    def test_worker_refuses_consentless_and_secrets(self):
        bundle = {"schema": "site-bundle-v1", "bundle_id": "0123456789abcdef",
                  "runs": []}
        err = self._eval(f"validateBundle({json.dumps(bundle)})")
        self.assertIn("consent", err)
        bundle["consent"] = {"schema": "site-consent-v1",
                             "accepted_at": "x", "surface": "y"}
        bundle["chain"] = {"verified_claim": True, "head_hash": "h"}
        bundle["runs"] = []
        bundle["capabilities"] = [{"model": "sk-or-v1-0123456789abcdef0123"}]
        err = self._eval(f"validateBundle({json.dumps(bundle)})")
        self.assertIn("credential", err)

    def test_worker_floor_scan_order_matches_python(self):
        cases = {
            "fix a typo in the comment": "T0",
            "fix the race in the token bucket": "T2",
            "design the architecture for the protocol": "T3",
            "implement a retry queue": "T1",
            "": "T0",
        }
        for goal, expected in cases.items():
            with self.subTest(goal=goal):
                got = self._eval(f"tierFloorForGoal({json.dumps(goal)})")
                self.assertEqual(got, expected)


if __name__ == "__main__":
    unittest.main()
