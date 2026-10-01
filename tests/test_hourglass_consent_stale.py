"""Hermetic unit tests for GAP-consent-stale: Consent staleness enum & validation.

Validates that changes in instruction, target files, model assignment, budget,
restart targeting, or Jev freshness signal cause consent to become stale fail-closed,
and that renewal records the staleness reason in both the ledger and emitted events.
"""
import unittest

from harness.consent import (
    ConsentStalenessReason,
    CONSENT_STALENESS_REASONS,
    check_consent_staleness,
    consent_renew,
)
from tests._fake import FakeTransport, m, comp, _gov


class _MockLedger:
    def __init__(self):
        self.entries = []

    def append(self, event, **kwargs):
        self.entries.append((event, kwargs))


class TestConsentStalenessReasons(unittest.TestCase):
    """Test the declared ConsentStalenessReason enum."""

    def test_enum_values_and_tuple(self):
        expected_values = {
            "instruction_amended",
            "target_files_changed",
            "model_upgraded",
            "budget_exceeded",
            "restart_targeted",
            "expired",
            "unspecified",
        }
        actual_values = {r.value for r in ConsentStalenessReason}
        self.assertEqual(actual_values, expected_values)
        self.assertEqual(set(CONSENT_STALENESS_REASONS), expected_values)


class TestCheckConsentStaleness(unittest.TestCase):
    """Test the check_consent_staleness function across all trigger conditions."""

    def test_no_previous_assignment(self):
        stale, reason = check_consent_staleness(None, {"instruction": "fix bug"})
        self.assertFalse(stale)
        self.assertIsNone(reason)

    def test_missing_current_assignment(self):
        stale, reason = check_consent_staleness({"instruction": "fix bug"}, None)
        self.assertTrue(stale)
        self.assertEqual(reason, ConsentStalenessReason.UNSPECIFIED)

    def test_instruction_amended(self):
        prev = {"instruction": "fix bug in parser"}
        curr = {"instruction": "fix bug in parser and add auth"}
        stale, reason = check_consent_staleness(prev, curr)
        self.assertTrue(stale)
        self.assertEqual(reason, ConsentStalenessReason.INSTRUCTION_AMENDED)

    def test_goal_amended(self):
        prev = {"goal": "optimize loop"}
        curr = {"goal": "delete loop"}
        stale, reason = check_consent_staleness(prev, curr)
        self.assertTrue(stale)
        self.assertEqual(reason, ConsentStalenessReason.INSTRUCTION_AMENDED)

    def test_target_files_changed(self):
        prev = {"target_files": ["harness/apply.py"]}
        curr = {"target_files": ["harness/apply.py", "harness/gate.py"]}
        stale, reason = check_consent_staleness(prev, curr)
        self.assertTrue(stale)
        self.assertEqual(reason, ConsentStalenessReason.TARGET_FILES_CHANGED)

    def test_file_path_changed(self):
        prev = {"file_path": "harness/a.py"}
        curr = {"file_path": "harness/b.py"}
        stale, reason = check_consent_staleness(prev, curr)
        self.assertTrue(stale)
        self.assertEqual(reason, ConsentStalenessReason.TARGET_FILES_CHANGED)

    def test_model_upgraded(self):
        prev = {"model": "deepseek/deepseek-v4.1-flash"}
        curr = {"model": "qwen/qwen3.8-max-0902"}
        stale, reason = check_consent_staleness(prev, curr)
        self.assertTrue(stale)
        self.assertEqual(reason, ConsentStalenessReason.MODEL_UPGRADED)

    def test_restart_targeted(self):
        prev = {"instruction": "fix bug", "model": "m"}
        curr = {"instruction": "fix bug", "model": "m", "restart_target": "planning"}
        stale, reason = check_consent_staleness(prev, curr)
        self.assertTrue(stale)
        self.assertEqual(reason, ConsentStalenessReason.RESTART_TARGETED)

    def test_budget_exceeded(self):
        prev = {"instruction": "fix bug", "max_cost": 0.05}
        curr = {"instruction": "fix bug", "max_cost": 0.15}
        stale, reason = check_consent_staleness(prev, curr)
        self.assertTrue(stale)
        self.assertEqual(reason, ConsentStalenessReason.BUDGET_EXCEEDED)

    def test_jev_freshness_expired(self):
        prev = {"instruction": "fix bug"}
        curr = {"instruction": "fix bug"}
        stale, reason = check_consent_staleness(prev, curr, consent_freshness=0.55, min_freshness=0.70)
        self.assertTrue(stale)
        self.assertEqual(reason, ConsentStalenessReason.EXPIRED)

    def test_identical_assignment_fresh(self):
        prev = {
            "instruction": "fix bug",
            "target_files": ["harness/apply.py"],
            "model": "deepseek/deepseek-v4.1-flash",
            "max_cost": 0.10,
        }
        curr = {
            "instruction": "fix bug",
            "target_files": ["harness/apply.py"],
            "model": "deepseek/deepseek-v4.1-flash",
            "max_cost": 0.10,
        }
        stale, reason = check_consent_staleness(prev, curr, consent_freshness=0.92, min_freshness=0.70)
        self.assertFalse(stale)
        self.assertIsNone(reason)


class TestConsentRenewStalenessIntegration(unittest.TestCase):
    """Test that consent_renew records staleness reason in ledger and results."""

    def test_renew_records_staleness_reason_accept(self):
        ledger = _MockLedger()
        canned_response = (
            '{"decision": "accept", "reason": "Scope amended but looks reasonable", "confidence": 0.85}'
        )
        transport = FakeTransport(
            models=[m("judge/v1")],
            posts=[comp(canned_response)],
        )
        governor = _gov(transport)

        res = consent_renew(
            transport=transport,
            api_key="sk-fake",
            governor=governor,
            task_id="task-123",
            task="Update verification gate",
            model="judge/v1",
            ledger=ledger,
            staleness_reason=ConsentStalenessReason.INSTRUCTION_AMENDED,
        )

        self.assertEqual(res["decision"], "accept")
        self.assertEqual(res["staleness_reason"], "instruction_amended")

        # Verify ledger recorded staleness_reason
        self.assertEqual(len(ledger.entries), 1)
        event_name, kwargs = ledger.entries[0]
        self.assertEqual(event_name, "consent_renew_accept")
        self.assertEqual(kwargs["staleness_reason"], "instruction_amended")
        self.assertEqual(kwargs["task_id"], "task-123")

    def test_renew_records_staleness_reason_defer(self):
        ledger = _MockLedger()
        canned_response = (
            '{"decision": "defer", "reason": "Budget exceeded beyond acceptable bounds", "confidence": 0.90}'
        )
        transport = FakeTransport(
            models=[m("judge/v1")],
            posts=[comp(canned_response)],
        )
        governor = _gov(transport)

        res = consent_renew(
            transport=transport,
            api_key="sk-fake",
            governor=governor,
            task_id="task-456",
            task="High cost refactor",
            model="judge/v1",
            ledger=ledger,
            staleness_reason=ConsentStalenessReason.BUDGET_EXCEEDED,
        )

        self.assertEqual(res["decision"], "defer")
        self.assertEqual(res["staleness_reason"], "budget_exceeded")

        # Verify ledger recorded staleness_reason
        self.assertEqual(len(ledger.entries), 1)
        event_name, kwargs = ledger.entries[0]
        self.assertEqual(event_name, "consent_renew_defer")
        self.assertEqual(kwargs["staleness_reason"], "budget_exceeded")


if __name__ == "__main__":
    unittest.main()
