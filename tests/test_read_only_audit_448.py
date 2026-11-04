"""Issue #448 acceptance tests: read-only audits, scope, no-tools, 402 cause.

The two prompts below are the specimen prompts recorded on the issue. They are
acceptance inputs, not illustrations: the read-only audit request must not
enter paid edit planning, and the LIVE_OK probe must not be given retrieval
after the operator forbade tools.
"""
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from harness.agent import AutonomousAgent
from harness.provider_errors import ProviderSpendLimitError, provider_spend_limit_error
from harness.repo_scope import discover_target_files
from harness.request_intent import (
    classify_prompt_intent,
    is_read_only_audit_request,
    no_tools_requested,
)

AUDIT_SPECIMEN = (
    "verify all aspects of the workspace ... audit with jev ... "
    "Read-only just audit and create a report."
)
NO_TOOLS_SPECIMEN = (
    "Read-only live smoke check: reply exactly LIVE_OK. "
    "Do not inspect files, call tools, or make changes."
)


def _lane_settings(**overrides):
    from types import SimpleNamespace
    base = dict(
        jev_api_key=None,
        jev_disabled=True,
        allow_escalation=False,
        max_cost=0.05,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class ReadOnlyAuditIntentTests(unittest.TestCase):
    def test_audit_specimen_is_an_audit_not_an_edit(self):
        self.assertTrue(is_read_only_audit_request(AUDIT_SPECIMEN))
        self.assertEqual(classify_prompt_intent(AUDIT_SPECIMEN), "audit")

    def test_create_a_report_is_the_artifact_not_a_write(self):
        # The exact pre-fix defect: "create" is in MUTATION_KEYWORDS.
        prompt = "Read-only audit of this workspace, then create a report."
        self.assertEqual(classify_prompt_intent(prompt), "audit")

    def test_write_request_using_the_read_only_adjective_stays_an_edit(self):
        prompt = "Add a read-only audit mode to the CLI"
        self.assertFalse(is_read_only_audit_request(prompt))
        self.assertEqual(classify_prompt_intent(prompt), "edit")

    def test_ordinary_write_request_is_unaffected(self):
        self.assertEqual(classify_prompt_intent("Fix the bug in executor.py"),
                         "edit")
        self.assertEqual(classify_prompt_intent("Refactor concurrency architecture"),
                         "edit")

    def test_plain_review_without_audit_noun_is_not_the_audit_lane(self):
        # Only an audit outranks the mutation keywords; a conversational review
        # keeps its historical classification.
        self.assertFalse(is_read_only_audit_request(
            "review only the docs and check for errors"))
        self.assertEqual(classify_prompt_intent(
            "review only the docs and check for errors"), "conversation")

    def test_audit_specimen_never_reaches_the_edit_lane(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = AutonomousAgent(settings=cast(Any, _lane_settings()),
                                    history_dir=Path(tmp), root_dir=Path(tmp))
            sentinel = {"status": "ok", "intent": "audit"}
            with patch("harness.request_router.load_memory", return_value={}), \
                 patch("harness.request_router.sole_site", return_value=""), \
                 patch("harness.request_router.emit"), \
                 patch.object(AutonomousAgent, "run_hourglass_request",
                              return_value=sentinel) as hourglass, \
                 patch.object(AutonomousAgent, "_handle_edit",
                              side_effect=AssertionError("read-only audit reached the edit lane")):
                result = agent.run_prompt(AUDIT_SPECIMEN,
                                          session_id="448-audit",
                                          force_conversation=True)
        self.assertEqual(result, sentinel)
        self.assertEqual(hourglass.call_count, 1)
        self.assertFalse(hourglass.call_args.kwargs.get("web"))


class NoToolsConstraintTests(unittest.TestCase):
    def test_live_ok_specimen_is_recognized_as_no_tools(self):
        self.assertTrue(no_tools_requested(NO_TOOLS_SPECIMEN))

    def test_tool_request_is_not_suppressed(self):
        self.assertFalse(no_tools_requested("search the web for today's rates"))
        self.assertFalse(no_tools_requested("read the workspace files"))

    def test_no_tools_specimen_is_not_sent_to_retrieval(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = AutonomousAgent(settings=cast(Any, _lane_settings()),
                                    history_dir=Path(tmp), root_dir=Path(tmp))
            sentinel = {"status": "ok", "intent": "conversation"}
            with patch("harness.request_router.load_memory", return_value={}), \
                 patch("harness.request_router.sole_site", return_value=""), \
                 patch("harness.request_router.emit"), \
                 patch("harness.request_router.is_simple_answer_request",
                       return_value=False), \
                 patch.object(AutonomousAgent, "_live_jev_available",
                              return_value=True), \
                 patch.object(AutonomousAgent, "run_hourglass_request",
                              return_value=sentinel) as hourglass:
                result = agent.run_prompt(NO_TOOLS_SPECIMEN,
                                          session_id="448-notools",
                                          force_conversation=True,
                                          web=True)
        self.assertEqual(result, sentinel)
        self.assertFalse(hourglass.call_args.kwargs.get("web"))


class WorkspaceAuditScopeTests(unittest.TestCase):
    def _tree(self, root: Path):
        (root / "harness").mkdir(parents=True, exist_ok=True)
        (root / "harness" / "jev.py").write_text("x = 1\n", encoding="utf-8")
        (root / "harness" / "workspace.py").write_text("y = 2\n", encoding="utf-8")
        (root / "docs").mkdir(exist_ok=True)
        (root / "docs" / "guide.md").write_text("# Guide\n", encoding="utf-8")
        return root

    def test_module_stem_collision_does_not_narrow_a_workspace_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(Path(tmp))
            # Pre-fix this returned harness/jev.py + harness/workspace.py.
            self.assertEqual(discover_target_files(AUDIT_SPECIMEN, root), [])

    def test_explicit_path_still_wins_for_a_named_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(Path(tmp))
            self.assertEqual(
                discover_target_files("Read-only audit of harness/jev.py", root),
                ["harness/jev.py"])

    def test_edit_scope_discovery_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(Path(tmp))
            self.assertEqual(discover_target_files("update workspace.py", root),
                             ["harness/workspace.py"])


class ProviderPaymentRequiredCauseTests(unittest.TestCase):
    def _error(self, status, response):
        err = provider_spend_limit_error(status, response)
        self.assertIsInstance(err, ProviderSpendLimitError)
        assert err is not None
        return err

    def test_funding_402_keeps_the_actionable_diagnosis(self):
        err = self._error(402, {"error": {"message": "Insufficient credits"}})
        self.assertEqual(err.cause, "account_funding")
        self.assertEqual(err.kind, "provider_spend_limit")
        self.assertIn("spend limit exhausted", str(err))
        self.assertTrue(err.next_step)

    def test_payment_required_code_is_funding(self):
        err = self._error(402, {"error": {"code": "payment_required"}})
        self.assertEqual(err.cause, "account_funding")

    def test_route_restriction_402_does_not_claim_balance(self):
        err = self._error(
            402, {"error": {"message": "No endpoints found for model",
                            "code": "model_not_found"}})
        self.assertEqual(err.cause, "route_or_model_restriction")
        self.assertNotIn("add credits", str(err).lower())
        self.assertIn("route", str(err).lower())

    def test_unattributable_402_is_recorded_as_unknown(self):
        err = self._error(402, {"error": {"message": "request refused"}})
        self.assertEqual(err.cause, "unknown")
        self.assertIsNone(err.next_step)

    def test_unsignalled_reason_phrases_on_200_are_not_spend_errors(self):
        self.assertIsNone(provider_spend_limit_error(
            200, {"choices": [{"message": {"content": "your bill is due"}}]}))


if __name__ == "__main__":
    unittest.main()
