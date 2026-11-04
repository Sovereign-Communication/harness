"""Issue #448: a read-only workspace audit is scoped and preflighted, never
answered with a ledger-chain verdict and never dispatched on a guess.

The specimen prompt from the issue is the acceptance input here: it must reach
the workspace-audit lane, enumerate the repository scope, price the complete
run with a stated buffer, and either allow the cap or return the exact raise
for approval. Nothing in this file opens a socket or reads a live key.
"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from harness.agent import AutonomousAgent
from harness.repo_scope import audit_scope
from harness.request_intent import (classify_prompt_intent,
                                    is_workspace_audit_request)
from harness.workspace_audit import audit_workspace, estimate_audit_run

AUDIT_SPECIMEN = (
    "verify all aspects of the workspace ... audit with jev ... "
    "Read-only just audit and create a report."
)
LEDGER_AUDIT = "audit ledger"


def _settings(**overrides):
    base = dict(
        jev_api_key=None,
        jev_disabled=False,
        allow_escalation=False,
        max_cost=0.25,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _tree(root: Path) -> Path:
    (root / "harness").mkdir(parents=True, exist_ok=True)
    (root / "harness" / "engine.py").write_text("x = 1\n", encoding="utf-8")
    (root / "docs").mkdir(exist_ok=True)
    (root / "docs" / "guide.md").write_text("# Guide\n", encoding="utf-8")
    (root / "node_modules").mkdir(exist_ok=True)
    (root / "node_modules" / "dep.js").write_text(
        "const secret = 'do-not-read-me';\n", encoding="utf-8")
    return root


class WorkspaceAuditIntentTests(unittest.TestCase):
    def test_specimen_is_a_workspace_audit(self):
        self.assertTrue(is_workspace_audit_request(AUDIT_SPECIMEN))
        self.assertEqual(classify_prompt_intent(AUDIT_SPECIMEN), "audit")

    def test_ledger_audit_is_not_a_workspace_audit(self):
        # The chain question keeps its own lane; it must not be re-routed.
        self.assertFalse(is_workspace_audit_request(LEDGER_AUDIT))
        self.assertTrue(classify_prompt_intent(LEDGER_AUDIT), "audit")

    def test_write_request_is_not_a_workspace_audit(self):
        self.assertFalse(is_workspace_audit_request(
            "Add a read-only audit mode to the workspace CLI"))


class ScopeTests(unittest.TestCase):
    def test_scope_lists_included_paths_and_exclusion_reasons(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _tree(Path(tmp))
            scope = audit_scope(root)
        included = scope["included"]
        self.assertIn("harness/engine.py", included)
        self.assertIn("docs/guide.md", included)
        self.assertNotIn("node_modules/dep.js", included)
        reasons = {row["path"]: row["reason"] for row in scope["excluded"]}
        self.assertEqual(reasons.get("node_modules"), "skipped_directory")
        self.assertEqual(scope["excluded_count"], 1)

    def test_scope_never_reads_excluded_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _tree(Path(tmp))
            rendered = repr(audit_scope(root))
        self.assertNotIn("do-not-read-me", rendered)

    def test_capped_listing_is_reported_as_truncated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _tree(Path(tmp))
            scope = audit_scope(root, limit=1)
        self.assertTrue(scope["truncated"])
        self.assertEqual(len(scope["included"]), 1)

    def test_estimate_carries_a_stated_buffer(self):
        scope = {"included_chars": 40_000, "included_chunks_estimate": 10}
        estimate = estimate_audit_run(scope)
        self.assertEqual(estimate["safe_buffer"], 0.25)
        self.assertGreater(estimate["input_tokens_with_buffer"],
                           estimate["estimated_input_tokens"])
        self.assertGreater(estimate["worst_case_cost_usd"], 0.0)


class PreflightTests(unittest.TestCase):
    def test_underfunded_run_asks_for_the_exact_raise_and_dispatches_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _tree(Path(tmp))
            settings = _settings(max_cost=0.0)
            report = audit_workspace(root, settings)
        self.assertEqual(report["status"], "approval_required")
        self.assertEqual(report["preflight"]["dispatch_attempts"], 0)
        self.assertGreater(report["preflight"]["approval_required_usd"], 0.0)
        self.assertEqual(settings.max_cost, 0.0)
        self.assertIn("Approve raising", report["next_step"])

    def test_funded_run_is_ready_and_still_dispatches_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _tree(Path(tmp))
            report = audit_workspace(root, _settings(max_cost=5.0))
        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["preflight"]["dispatch_attempts"], 0)
        self.assertIn("harness source-audit", report["next_step"])

    def test_disabled_jev_blocks_with_an_actionable_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _tree(Path(tmp))
            report = audit_workspace(root, _settings(jev_disabled=True))
        self.assertEqual(report["status"], "blocked")
        self.assertIn("Jev is disabled", report["next_step"])

    def test_truncated_scope_is_never_called_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _tree(Path(tmp))
            report = audit_workspace(root, _settings(max_cost=5.0))
        self.assertNotIn("NOT a complete", report["summary"])


class HandlerRoutingTests(unittest.TestCase):
    def _agent(self, tmp):
        settings = _settings(max_cost=5.0,
                             ledger_path=str(Path(tmp) / "ledger.jsonl"))
        return AutonomousAgent(settings=cast(Any, settings),
                               history_dir=Path(tmp), root_dir=Path(tmp))

    def test_workspace_specimen_reaches_the_workspace_lane_not_the_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            _tree(Path(tmp))
            agent = self._agent(tmp)
            result = agent._handle_audit(AUDIT_SPECIMEN, "448-workspace")
        self.assertEqual(result["lane"], "workspace_audit")
        self.assertEqual(result["intent"], "audit")
        self.assertNotIn("ledger chain", result["response"].lower())
        self.assertEqual(result["preflight"]["dispatch_attempts"], 0)
        self.assertGreater(result["scope"]["included_count"], 0)
        self.assertTrue(result["next_step"])

    def test_ledger_prompt_keeps_the_chain_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(tmp)
            result = agent._handle_audit(LEDGER_AUDIT, "448-ledger")
        self.assertNotIn("lane", result)
        self.assertIn("ledger chain", result["response"].lower())


if __name__ == "__main__":
    unittest.main()
