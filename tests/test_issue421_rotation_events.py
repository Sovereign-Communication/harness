"""Regression coverage for issue #421 consent rotation evidence."""

import os
import tempfile
import unittest
from unittest.mock import patch

from harness.consent import consent_renew, probe_consent
from harness.ledger import AutonomyLedger
from harness.spend import SpendGovernor
from harness.token_budget import TokenBudget, USAGE_UNAVAILABLE
from tests._fake import JUDGE, P2, FakeTransport, comp, consent, m

FALLBACK = P2


class ConsentRotationEventTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.ledger = AutonomyLedger(
            os.path.join(self.temp_dir.name, "ledger.jsonl"))
        self.models = [m(JUDGE), m(FALLBACK)]

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def _rotation_events(emit):
        return [call.kwargs for call in emit.call_args_list
                if call.args and call.args[0] == "rotation"]

    def _probe(self, task_id, posts):
        transport = FakeTransport(models=self.models, posts=posts)
        result = probe_consent(
            transport=transport,
            api_key="k",
            governor=SpendGovernor(transport, "sk-test"),
            task_id=task_id,
            task="Review the scoped change",
            model=JUDGE,
            ledger=self.ledger,
            fallback_pool=[FALLBACK],
        )
        return result

    def test_one_unusable_candidate_has_one_cost_matched_event_and_row(self):
        task_id = "issue-421-single-rotation"
        with patch("harness.consent._events.emit") as emit:
            result = self._probe(task_id, [
                comp("not a consent decision", cost=0.000003),
                consent("accept", "fits my context"),
            ])

        events = self._rotation_events(emit)
        rows = [row for row in self.ledger.entries()
                if row["event"] == "consent_rotate"]
        self.assertEqual(result["decision"], "accept")
        self.assertEqual(len(events), 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(events[0]["task_id"], task_id)
        self.assertEqual(events[0]["model"], JUDGE)
        self.assertEqual(events[0]["reason"], "consent_unusable")
        self.assertEqual(rows[0]["task_id"], task_id)
        self.assertEqual(rows[0]["model"], JUDGE)
        self.assertEqual(events[0]["detail"], rows[0]["reason"])
        self.assertEqual(events[0]["cost"], rows[0]["cost"])

    def test_two_unusable_candidates_have_ordered_matching_events_and_rows(self):
        task_id = "issue-421-two-rotations"
        with patch("harness.consent._events.emit") as emit:
            result = self._probe(task_id, [
                comp("not a consent decision", cost=0.000003),
                comp("still not a consent decision", cost=0.000004),
            ])

        events = self._rotation_events(emit)
        rows = [row for row in self.ledger.entries()
                if row["event"] == "consent_rotate"]
        self.assertEqual(result["decision"], "defer")
        self.assertEqual(len(events), 2)
        self.assertEqual(len(rows), 2)
        self.assertEqual([event["model"] for event in events],
                         [JUDGE, FALLBACK])
        for event, row in zip(events, rows):
            self.assertEqual(event["task_id"], row["task_id"])
            self.assertEqual(event["model"], row["model"])
            self.assertEqual(event["reason"], "consent_unusable")
            self.assertEqual(event["detail"], row["reason"])
            self.assertEqual(event["cost"], row["cost"])

    def test_parsed_decisions_never_emit_rotation_or_rotation_rows(self):
        for decision in ("accept", "decline", "defer", "redirect"):
            task_id = f"issue-421-parsed-{decision}"
            with self.subTest(decision=decision):
                with patch("harness.consent._events.emit") as emit:
                    result = self._probe(task_id, [consent(decision)])

                rows = [row for row in self.ledger.entries()
                        if row["task_id"] == task_id
                        and row["event"] == "consent_rotate"]
                self.assertEqual(result["decision"], decision)
                self.assertEqual(self._rotation_events(emit), [])
                self.assertEqual(rows, [])

    def test_transport_exception_settles_reserved_tokens_as_unavailable(self):
        transport = FakeTransport(models=self.models, posts=[])
        budget = TokenBudget(max_input_tokens=1000, max_output_tokens=1000)
        governor = SpendGovernor(transport, "sk-test")

        with patch("harness.consent.chat",
                   side_effect=RuntimeError("transport unavailable")):
            with self.assertRaisesRegex(RuntimeError, "transport unavailable"):
                probe_consent(
                    transport=transport,
                    api_key="k",
                    governor=governor,
                    task_id="issue-421-token-budget-exception",
                    task="Review the scoped change",
                    model=JUDGE,
                    token_budget=budget,
                )

        snapshot = budget.snapshot()
        self.assertEqual(snapshot["usage_sources"][USAGE_UNAVAILABLE], 1)
        self.assertGreater(snapshot["used_input_tokens"], 0)
        self.assertEqual(snapshot["open_allowances"], 0)

    def test_renew_preserves_truthy_string_staleness_reason(self):
        transport = FakeTransport(
            models=self.models, posts=[consent("accept", "still fits")])
        result = consent_renew(
            transport=transport,
            api_key="k",
            governor=SpendGovernor(transport, "sk-test"),
            task_id="issue-421-string-staleness",
            task="Review the scoped change",
            model=JUDGE,
            staleness_reason="legacy stale reason",
        )

        self.assertEqual(result["decision"], "accept")
        self.assertEqual(result["staleness_reason"], "legacy stale reason")


if __name__ == "__main__":
    unittest.main()
