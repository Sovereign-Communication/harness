"""Regression floor for D12 (`sd_coverage_changed`) — the coverage ritual.

`DF-AUDIT-3`: the committed baseline named a commit that did not exist in the
repository, so `git diff` failed, D12 returned its fail-open `SKIP` with a
perfect score, and the "changed harness lines are suite-executed" rule was not
running at all — locally *or* in CI. Nothing caught it, because no test
asserted that D12 *evaluates* rather than skips.

These tests pin three things:

1. the committed baseline names a commit that is really in this repository;
2. D12 computes a real verdict against it instead of returning a fail-open
   SKIP; and
3. an unreachable reference fails closed in CI while staying a *visible* SKIP
   locally.

A shallow checkout (CI's test job checks out one commit; the audit job fetches
full history) cannot resolve the baseline object, and says so rather than
reporting a false failure.
"""

import importlib.util
import os
import subprocess
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_AUDIT_PATH = _ROOT / "audits" / "self" / "audit.py"
_BASELINE_PATH = _ROOT / "audits" / "self" / "coverage_baseline.json"


def _load_audit():
    spec = importlib.util.spec_from_file_location("_d12_audit", _AUDIT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(*args):
    return subprocess.run(["git", *args], cwd=str(_ROOT), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


class D12BaselinePinTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.audit = _load_audit()

    def test_baseline_names_a_commit_present_in_this_repository(self):
        if os.environ.get("HARNESS_REFRESHING_COVERAGE_BASELINE") == "1":
            self.skipTest("baseline refresh: pin is rechecked after regeneration")
        import json
        ref = json.loads(_BASELINE_PATH.read_text(encoding="utf-8-sig")).get(
            "commit", "")
        self.assertEqual(len(ref), 40, "baseline must name a full commit id")
        exists = _git("cat-file", "-e", ref + "^{commit}").returncode == 0
        if not exists and _git("rev-parse", "--is-shallow-repository").stdout.strip() == "true":
            self.skipTest("shallow clone (platform checkout default): "
                          "baseline object unavailable")
        self.assertTrue(
            exists,
            f"coverage_baseline.json names {ref[:12]}, which is not in this "
            "repository -- D12 cannot compute a ratio and would fail open "
            "(DF-AUDIT-3). Re-point and regenerate the baseline.")

    def test_d12_evaluates_rather_than_skipping(self):
        if os.environ.get("HARNESS_REFRESHING_COVERAGE_BASELINE") == "1":
            self.skipTest("baseline refresh: D12 is rechecked after regeneration")
        score, evidence = self.audit.sd_coverage_changed()
        if "not reachable" in evidence and \
                _git("rev-parse", "--is-shallow-repository").stdout.strip() == "true":
            self.skipTest("shallow clone (platform checkout default): "
                          "D12 cannot evaluate here")
        self.assertNotIn("fail-open", evidence,
                         f"D12 skipped instead of evaluating: {evidence}")
        self.assertNotIn("not reachable", evidence,
                         f"D12 could not reach its baseline: {evidence}")
        self.assertIn(score, (0.0, 1.0))
        # "Evaluating" has to be observable in the evidence, not inferred from
        # the score: DF-AUDIT-3 was a perfect score with no ratio at all. So
        # either D12 reports what it measured, or it states that the tree has
        # no executable harness changes since the baseline.
        self.assertTrue(
            "changed-line coverage" in evidence
            or "no executable harness changes" in evidence,
            f"D12 returned a verdict without saying what it measured: {evidence}")

    def test_unreachable_reference_fails_closed_in_ci(self):
        score, evidence = self.audit.d12_unreachable("f" * 40, in_ci=True)
        self.assertEqual(score, 0.0, evidence)
        self.assertTrue(evidence.startswith("FAIL"), evidence)
        self.assertIn("refresh_coverage_baseline.py", evidence)

    def test_unreachable_reference_is_a_visible_skip_locally(self):
        score, evidence = self.audit.d12_unreachable("f" * 40, in_ci=False)
        self.assertEqual(score, 1.0, evidence)
        self.assertTrue(evidence.startswith("SKIP (fail-open)"), evidence)

    def test_each_skip_path_emits_an_audible_warning(self):
        import contextlib
        import io

        for in_ci, marker in ((False, "[warn]"), (True, "[FATAL]")):
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                self.audit.d12_unreachable("f" * 40, in_ci=in_ci)
            self.assertIn(marker, buf.getvalue(),
                          f"no {marker} warning emitted (in_ci={in_ci})")

    def test_in_ci_flag_is_derived_from_the_environment_variable(self):
        # The module recomputes this at import; assert the derivation itself
        # so a refactor cannot quietly hardcode it.
        source = _AUDIT_PATH.read_text(encoding="utf-8")
        self.assertIn('os.environ.get("CI"', source)
        self.assertIsInstance(self.audit._IN_CI, bool)


if __name__ == "__main__":
    unittest.main()
