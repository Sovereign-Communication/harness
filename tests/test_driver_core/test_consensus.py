"""The consensus tests -- the spike that Segment 0 exists to produce.

The headline property, tested first and tested hardest, is that **an
unanswered slot is a shortfall and not an agreement**. Everything else in this
project is a feature; this one is a safety property. If it regresses, the
system reports a green light that was lit by fewer votes than it claims, and
every downstream guarantee built on it becomes decorative.
"""
import unittest

from driver_core.consensus import (
    AGREED, DISAGREED, INSUFFICIENT, Agreement, Vote, tally,
)
from driver_core.states import SCREEN_SCHEMA


def _state(title="Report - Editor", app="editor", **over):
    base = {
        "window_title": title,
        "foreground_app": app,
        "error_dialog_present": False,
    }
    base.update(over)
    return base


def _screen(title="Report - Editor", app="editor", **over):
    return Vote.ok("slot", _state(title=title, app=app, **over))


class ShortfallIsNotAgreementTests(unittest.TestCase):
    """The load-bearing property."""

    def test_two_agree_one_silent_with_quorum_three_is_insufficient(self):
        result = tally([_screen(), _screen(), Vote.error("c", "transport")],
                       SCREEN_SCHEMA, quorum=3)
        self.assertIs(result.is_usable, False)
        self.assertEqual(result.outcome, INSUFFICIENT)
        self.assertIsNone(result.state)
        self.assertEqual(result.answering, 2)
        self.assertEqual(list(result.unanswered), ["c"])
        self.assertIn("shortfall", result.detail)

    def test_one_answers_two_silent_is_insufficient_not_unanimous(self):
        """A 1-of-3 round must never be reported as a clean 1-of-1."""
        result = tally([_screen(),
                        Vote.error("b", "timeout"),
                        Vote.error("c", "rate limited")],
                       SCREEN_SCHEMA, quorum=2)
        self.assertEqual(result.outcome, INSUFFICIENT)
        self.assertEqual(result.answering, 1)
        self.assertIsNone(result.state)

    def test_one_answers_two_silent_at_quorum_one_agrees_but_records_shortfall(self):
        """With quorum 1 the two who answered do agree -- and the receipt
        still names the slot that did not, so the shortfall stays auditable
        rather than being quietly forgotten."""
        result = tally([_screen(),
                        Vote.error("b", "timeout"),
                        Vote.error("c", "timeout")],
                       SCREEN_SCHEMA, quorum=1)
        self.assertEqual(result.outcome, AGREED)
        self.assertIsNotNone(result.state)
        self.assertEqual(sorted(result.unanswered), ["b", "c"])
        receipt = result.receipt(SCREEN_SCHEMA.identity(), "x1")
        self.assertEqual(receipt["unanswered"], ["b", "c"])
        self.assertEqual(receipt["answering"], 1)
        self.assertEqual(receipt["asked"], 3)

    def test_no_votes_at_all_is_insufficient(self):
        result = tally([], SCREEN_SCHEMA, quorum=1)
        self.assertEqual(result.outcome, INSUFFICIENT)
        self.assertIsNone(result.state)
        self.assertEqual(result.asked, 0)

    def test_insufficient_agreement_object_refuses_to_carry_a_state(self):
        """Structural, not advisory: a non-agreed Agreement cannot be built
        holding a state, so no caller can read one by mistake."""
        with self.assertRaises(ValueError):
            Agreement(DISAGREED, state={"window_title": "x"})


class MalformedIsNotDisagreementTests(unittest.TestCase):
    """A garbage answer is a non-answer, not a dissenting answer."""

    def test_malformed_answer_is_excluded_from_the_denominator(self):
        result = tally(
            [_screen(),
             _screen(),
             Vote.malformed("c", "unparseable body")],
            SCREEN_SCHEMA, quorum=2, min_agreement=1.0)
        # If the malformed answer counted as dissent, this would be DISAGREED.
        # It is a non-answer, so the two real answers decide.
        self.assertEqual(result.outcome, AGREED)
        self.assertEqual(result.answering, 2)
        self.assertIn("c", result.unanswered)

    def test_state_failing_schema_validation_is_demoted_to_non_answer(self):
        good = _screen()
        bad = Vote.ok("c", {"window_title": "only this"})  # missing required
        result = tally([good, good, bad], SCREEN_SCHEMA, quorum=2)
        self.assertEqual(result.outcome, AGREED)
        self.assertEqual(result.answering, 2)
        slot_c = next(s for s in result.slots if s.slot == "c")
        self.assertEqual(slot_c.status, "malformed")
        self.assertIn("missing required field", slot_c.reason)

    def test_undeclared_field_makes_a_slot_malformed(self):
        result = tally(
            [_screen(), _screen(),
             Vote.ok("c", _state(invented="surprise"))],
            SCREEN_SCHEMA, quorum=2)
        self.assertEqual(result.outcome, AGREED)
        slot_c = next(s for s in result.slots if s.slot == "c")
        self.assertEqual(slot_c.status, "malformed")
        self.assertIn("undeclared field", slot_c.reason)

    def test_a_failed_slot_may_not_carry_state_into_the_tally(self):
        with self.assertRaises(ValueError):
            Vote("a", "error", reason="boom", state={"window_title": "ghost"})


class DisagreementTests(unittest.TestCase):

    def test_one_field_contested_is_named_not_merged(self):
        result = tally([_screen(title="Alpha"),
                        _screen(title="Beta"),
                        _screen(title="Alpha")],
                       SCREEN_SCHEMA, quorum=3, min_agreement=1.0)
        self.assertEqual(result.outcome, DISAGREED)
        self.assertIsNone(result.state)
        self.assertEqual(result.contested_fields, ("window_title",))
        self.assertEqual(result.agreed_fields,
                         ("foreground_app", "error_dialog_present"))

    def test_partial_agreement_below_threshold_is_contested(self):
        result = tally([_screen(title="Alpha"),
                        _screen(title="Alpha"),
                        _screen(title="Beta")],
                       SCREEN_SCHEMA, quorum=3, min_agreement=0.75)
        self.assertEqual(result.outcome, DISAGREED)
        field = next(f for f in result.fields if f.name == "window_title")
        self.assertEqual(field.agreeing, 2)
        self.assertEqual(field.answering, 3)
        self.assertAlmostEqual(field.ratio, 2 / 3)
        self.assertTrue(field.contested)

    def test_case_difference_is_not_a_disagreement(self):
        """Provenance doing its job: this is the same title."""
        result = tally([_screen(title="Report - Editor"),
                        _screen(title="  report - editor ")],
                       SCREEN_SCHEMA, quorum=2)
        self.assertEqual(result.outcome, AGREED)
        self.assertEqual(result.state["window_title"], "report - editor")

    def test_boolean_vocabulary_is_widened_not_equated(self):
        """Two extractors answering 'no' and 'unchecked' mean the same
        thing; the boolean provenance rule is what makes that visible."""
        result = tally(
            [_screen(error_dialog_present=False),
             _screen(error_dialog_present="unchecked")],
            SCREEN_SCHEMA, quorum=2)
        self.assertEqual(result.outcome, AGREED)
        self.assertIs(result.state["error_dialog_present"], False)

    def test_distribution_is_reported_so_a_disagreement_is_legible(self):
        result = tally([_screen(title="Alpha"), _screen(title="Beta")],
                       SCREEN_SCHEMA, quorum=2)
        field = next(f for f in result.fields if f.name == "window_title")
        self.assertEqual(field.distribution, {"alpha": 1, "beta": 1})


class DeterminismTests(unittest.TestCase):

    def test_tie_break_is_stable_across_input_order(self):
        a = _screen(title="Zeta")
        b = _screen(title="Alpha")
        first = tally([a, b], SCREEN_SCHEMA, quorum=2)
        second = tally([b, a], SCREEN_SCHEMA, quorum=2)
        self.assertEqual(first.contested_fields, second.contested_fields)
        f1 = next(f for f in first.fields if f.name == "window_title")
        f2 = next(f for f in second.fields if f.name == "window_title")
        self.assertEqual(f1.value, f2.value)
        self.assertEqual(f1.value, "alpha")

    def test_repeated_tally_is_byte_identical(self):
        votes = [_screen(title="A"), _screen(title="B"), _screen(title="A")]
        one = tally(votes, SCREEN_SCHEMA, quorum=3).to_dict()
        two = tally(votes, SCREEN_SCHEMA, quorum=3).to_dict()
        self.assertEqual(one, two)

    def test_agreed_state_is_the_canonical_form_the_tally_actually_verified(self):
        """The shipped state is the *normalised* form, not a raw variant.

        This is a deliberate choice with a real consequence: what the
        decision tier receives is byte-identical to the value the tally
        compared, so there is no gap between "what was verified" and "what is
        being acted on". A raw passthrough would preserve the extractor's
        original casing while having verified the casefolded one, which
        leaves a field that looks untouched by verification but is not.
        """
        states = [_state(title="One"), _state(title="One")]
        result = tally([Vote.ok("a", states[0]), Vote.ok("b", states[1])],
                       SCREEN_SCHEMA, quorum=2)
        self.assertEqual(result.state["window_title"], "one")
        # Identical to what ONE extractor produced once normalised -- the
        # state is taken, never merged, so nothing a second model said can
        # silently rewrite a field.
        from driver_core.schema import validate_state
        self.assertEqual(result.state,
                         validate_state(states[0], SCREEN_SCHEMA))


class CostHonestyTests(unittest.TestCase):

    def test_cost_is_summed_across_slots_including_failed_ones(self):
        """A slot that crashed after the provider billed it is still money
        spent, and the tally must not under-report the round."""
        result = tally([Vote.ok("a", _state(), cost=0.001),
                        Vote.error("b", "timeout", cost=0.002)],
                       SCREEN_SCHEMA, quorum=1)
        self.assertAlmostEqual(result.cost, 0.003)

    def test_unavailable_usage_is_not_reported_as_free(self):
        vote = Vote.error("a", "boom", cost=0.5)
        self.assertEqual(vote.usage_source, "unavailable")
        self.assertGreater(vote.cost, 0.0)


class GuardTests(unittest.TestCase):

    def test_quorum_below_one_is_refused(self):
        with self.assertRaises(ValueError):
            tally([_screen()], SCREEN_SCHEMA, quorum=0)

    def test_min_agreement_out_of_range_is_refused(self):
        with self.assertRaises(ValueError):
            tally([_screen()], SCREEN_SCHEMA, min_agreement=0)
        with self.assertRaises(ValueError):
            tally([_screen()], SCREEN_SCHEMA, min_agreement=1.5)


if __name__ == "__main__":
    unittest.main()
