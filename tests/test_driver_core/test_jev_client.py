"""Decision-client tests: the fail-closed contract and honest spend.

The theme is that every degraded path is *distinguishable* and *cheap*. A
response that named an undeclared action, a response with no distribution,
a transport failure and a missing key are four different situations, and a
caller needs to tell them apart -- both to react correctly and to be able to
say afterwards what actually happened.
"""
import unittest

from driver_core.actions import DEFAULT_VOCABULARY
from driver_core.audit import MemoryAuditLog
from driver_core.budget import Budget
from driver_core.config import load_settings
from driver_core.jev_client import (
    MALFORMED, NATIVE, UNAVAILABLE, UNKEYED, JevClient, build_questions,
    validate_answers,
)
from driver_core.transport import Response
from driver_core.ev import (
    FakeTransport, action_answer, no_usage_response, ok_response,
)

STATE = {"window_title": "report - editor", "foreground_app": "editor",
         "error_dialog_present": False}


def _answers(action="observe", confidence=0.9, **over):
    """A realistic response body.

    The distribution covers *every* declared option, not just the chosen one
    -- which is what the real endpoint returns, and what makes the
    "sums to 1" check meaningful. A body that listed only the winner would be
    rejected, correctly: a distribution missing the alternatives cannot
    express the confidence the model claims.
    """
    options = DEFAULT_VOCABULARY.names()
    others = [name for name in options if name != action]
    share = (1.0 - confidence) / len(others) if others else 0.0
    distribution = {name: (confidence if name == action else share)
                    for name in options}
    answer = {
        "action": {"type": "choice", "choice": action,
                   "probabilities": distribution,
                   "confidence": confidence},
        "state_is_stable": {"type": "noul", "noul": 0.95},
        "a_blocking_choice_is_required": {"type": "noul", "noul": 0.2},
    }
    answer.update(over)
    return answer


class ValidationTests(unittest.TestCase):

    def _validate(self, answers):
        questions = build_questions(STATE, DEFAULT_VOCABULARY)
        return validate_answers(answers, questions, DEFAULT_VOCABULARY)

    def test_a_well_formed_response_validates(self):
        action, confidence, probs, guards, reasons = self._validate(
            _answers())
        self.assertEqual(reasons, [])
        self.assertEqual(action, "observe")
        self.assertAlmostEqual(confidence, 0.9)
        self.assertEqual(guards["state_is_stable"], 0.95)

    def test_an_undeclared_action_is_refused(self):
        action, _, _, _, reasons = self._validate(_answers("rm_rf"))
        self.assertIsNone(action)
        self.assertTrue(any("not a declared action" in r for r in reasons))

    def test_a_missing_guard_is_a_refusal_not_a_default(self):
        answers = _answers()
        del answers["state_is_stable"]
        action, _, _, guards, reasons = self._validate(answers)
        self.assertIsNone(action)
        self.assertIn("missing answer(s) ['state_is_stable']", reasons)

    def test_a_probability_distribution_that_does_not_sum_to_one_is_refused(self):
        answers = _answers()
        answers["action"]["probabilities"] = {"observe": 0.9, "click": 0.9}
        action, _, _, _, reasons = self._validate(answers)
        self.assertIsNone(action)
        self.assertTrue(any("not 1" in r for r in reasons))

    def test_a_chosen_option_absent_from_its_own_distribution_is_refused(self):
        """A response that names an option its own distribution does not
        contain is internally inconsistent, and its confidence cannot be
        trusted."""
        answers = _answers()
        answers["action"]["choice"] = "click"
        answers["action"]["probabilities"] = {
            k: v for k, v in answers["action"]["probabilities"].items()
            if k != "click"}
        action, _, _, _, reasons = self._validate(answers)
        self.assertIsNone(action)
        self.assertTrue(any("absent from the distribution" in r for r in reasons))

    def test_a_confidence_outside_the_unit_interval_is_refused(self):
        answers = _answers()
        answers["action"]["confidence"] = 1.4
        action, _, _, _, reasons = self._validate(answers)
        self.assertIsNone(action)
        self.assertTrue(any("outside [0, 1]" in r for r in reasons))

    def test_a_type_mismatch_is_refused(self):
        answers = _answers()
        answers["state_is_stable"] = {"type": "noul", "noul": "high"}
        action, _, _, _, reasons = self._validate(answers)
        self.assertIsNone(action)
        self.assertTrue(reasons)

    def test_a_partial_response_yields_nothing_at_all(self):
        """All-or-nothing: a partial answer returns no action, not a partial."""
        answers = _answers()
        del answers["a_blocking_choice_is_required"]
        action, confidence, _, _, reasons = self._validate(answers)
        self.assertIsNone(action)
        self.assertIsNone(confidence)
        self.assertTrue(reasons)


class ClientPathTests(unittest.TestCase):

    def _client(self, *responses, key="k", ceiling=1.0, settings=None):
        settings = settings or load_settings(env={}, jev_api_key=key)
        self.audit = MemoryAuditLog()
        self.budget = Budget(ceiling, step_ceiling_usd=ceiling)
        return JevClient(settings, budget=self.budget, audit=self.audit,
                         transport_module=FakeTransport(*responses))

    def test_a_keyed_call_settles_once_and_records_once(self):
        client = self._client(ok_response(_answers(), input_tokens=1000))
        decision = client.decide(STATE, DEFAULT_VOCABULARY, step_id="s1")
        self.assertEqual(decision.status, NATIVE)
        self.assertTrue(decision.usable)
        self.assertEqual(decision.usage_source, "actual")
        self.assertAlmostEqual(decision.cost, 1000 * 0.042 / 1_000_000)
        self.assertEqual(len(self.audit.read_all()), 1)
        self.assertAlmostEqual(self.budget.spent, decision.cost)
        self.assertEqual(self.budget.reserved, 0.0)

    def test_an_unkeyed_client_never_dispatches(self):
        client = self._client(ok_response(_answers()), key="")
        decision = client.decide(STATE, DEFAULT_VOCABULARY)
        self.assertEqual(decision.status, UNKEYED)
        self.assertFalse(decision.native)
        self.assertIsNone(decision.recommended_action)
        self.assertEqual(self.budget.spent, 0.0)
        self.assertEqual(len(self.audit.read_all()), 1)

    def test_a_transport_failure_charges_the_reservation_not_zero(self):
        client = self._client(Response(0, "transport_error", detail="down"))
        decision = client.decide(STATE, DEFAULT_VOCABULARY)
        self.assertEqual(decision.status, UNAVAILABLE)
        self.assertGreater(decision.cost, 0.0)
        self.assertEqual(decision.usage_source, "unavailable")
        self.assertAlmostEqual(self.budget.spent, decision.cost)

    def test_a_response_with_no_usage_is_charged_the_full_reservation(self):
        """The load-bearing spend rule: a cost we cannot read is charged at
        the estimate, never at zero."""
        client = self._client(no_usage_response(_answers()))
        decision = client.decide(STATE, DEFAULT_VOCABULARY)
        self.assertEqual(decision.status, NATIVE)
        self.assertEqual(decision.usage_source, "unavailable")
        self.assertGreater(decision.cost, 0.0)

    def test_a_malformed_response_settles_the_call_it_still_made(self):
        """A billed call whose answer was unusable is still billed, and
        flagged so a coverage report can be honest about it."""
        client = self._client(ok_response(_answers(action="nonsense"),
                                          input_tokens=500))
        decision = client.decide(STATE, DEFAULT_VOCABULARY)
        self.assertEqual(decision.status, MALFORMED)
        self.assertFalse(decision.usable)
        self.assertIsNone(decision.recommended_action)
        self.assertGreater(decision.cost, 0.0)

    def test_a_budget_refusal_dispatches_nothing_and_records_a_refusal(self):
        client = self._client(ok_response(_answers()), ceiling=1e-9)
        decision = client.decide(STATE, DEFAULT_VOCABULARY)
        self.assertEqual(decision.status, UNAVAILABLE)
        self.assertIn("budget refused", decision.stop_reason)
        record = self.audit.read_all()[0]
        self.assertEqual(record["kind"], "decision")
        self.assertEqual(record["status"], UNAVAILABLE)

    def test_every_path_writes_exactly_one_record(self):
        """No silent holes in the ledger, including on the failure paths."""
        cases = [
            self._client(ok_response(_answers())),
            self._client(Response(500, "http_error", detail="boom")),
            self._client(Response(0, "transport_error")),
            self._client(no_usage_response(_answers(action="bogus"))),
            self._client(ok_response(_answers()), key=""),
        ]
        for client in cases:
            client.decide(STATE, DEFAULT_VOCABULARY, step_id="s")
            self.assertEqual(len(client.audit.read_all()), 1)

    def test_the_audit_record_carries_metadata_not_state(self):
        """A decision record must not become a transcript of the screen."""
        client = self._client(ok_response(_answers()))
        client.decide(STATE, DEFAULT_VOCABULARY)
        record = client.audit.read_all()[0]
        self.assertNotIn("state", record)
        self.assertNotIn("window_title", str(record))
        self.assertEqual(record["recommended_action"], "observe")


class EnvelopeTests(unittest.TestCase):

    def test_usable_requires_every_guard_to_have_answered(self):
        decision = action_answer("observe", confidence=0.9,
                                 guards={"state_is_stable": 1.0})
        self.assertFalse(decision.usable)

    def test_unusable_envelope_exposes_no_confidence(self):
        from driver_core.jev_client import Decision, UNAVAILABLE
        decision = Decision(UNAVAILABLE)
        self.assertFalse(decision.usable)
        self.assertIsNone(decision.confidence)
        self.assertIsNone(decision.recommended_action)


if __name__ == "__main__":
    unittest.main()
