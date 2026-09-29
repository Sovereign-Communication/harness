"""GAP-recondense hermetic gates: when a stage-mutated tree invalidates a brief.

The gap this closes is the one a pin check cannot see. `brief.freshness_report`
answers "has a cited file changed?"; a *composed* run has two more facts, and
`waist.resolve_stages` used to ignore both:

- a **stage gate failed**, which voids the green-tree basis the plan was
  written against, even though every pinned file is byte-identical; and
- an **executed node wrote a path the brief describes**, which makes the
  brief's account of that path stale for the work that remains.

`recondense_decision` is the ONE owner of the trigger vocabulary and its
precedence; `resolve_stages`/`compose_stages` consult it only when a caller
actually supplies stage-run evidence, so the pre-composition behaviour is
preserved exactly and pin-able. Every test here is hermetic: real temp files,
no network, no model, no Jev call.
"""
import os
import shutil
import tempfile
import unittest

from harness.brief import build_brief
from harness.errors import HarnessError
from harness.token_budget import TokenBudget
from harness.waist import (
    RECONDENSE_EXECUTED_OVERLAP,
    RECONDENSE_GATE_FAILURE,
    RECONDENSE_PIN_DRIFT,
    RECONDENSE_REASONS,
    RECONDENSE_TRIGGERS,
    STAGE_CONTEXT,
    brief_covered_paths,
    compose_stages,
    composition_envelope,
    recondense_decision,
    resolve_stages,
)


class RecondenseHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="harness-recondense-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cited = os.path.join(self.tmp, "cited.py")
        self.other = os.path.join(self.tmp, "other.py")
        self._write(self.cited, "x = 1\n")
        self._write(self.other, "y = 2\n")
        self.brief = build_brief("recondense the brief", [self.cited])

    def _write(self, path, text):
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)

    def _drift(self):
        """Change a cited file without changing its path."""
        self._write(self.cited, "x = 2\n")

    def _budget(self):
        return TokenBudget(max_input_tokens=8_000, max_output_tokens=2_000,
                           label="run")


class DecisionVocabularyTests(RecondenseHarness):
    def test_the_trigger_set_is_declared_and_every_trigger_has_a_reason(self):
        self.assertEqual(tuple(RECONDENSE_TRIGGERS),
                         (RECONDENSE_GATE_FAILURE, RECONDENSE_PIN_DRIFT,
                          RECONDENSE_EXECUTED_OVERLAP))
        for trigger in RECONDENSE_TRIGGERS:
            self.assertTrue(RECONDENSE_REASONS.get(trigger))

    def test_a_fresh_brief_with_no_stage_evidence_needs_no_refresh(self):
        decision = recondense_decision(brief=self.brief, touched_paths=(),
                                       gate_failures=())
        self.assertFalse(decision["refresh"])
        self.assertIsNone(decision["trigger"])
        self.assertEqual(decision["triggers"], [])
        self.assertEqual(decision["reasons"], [])

    def test_a_drifted_pin_triggers_a_refresh_with_its_evidence(self):
        self._drift()
        decision = recondense_decision(brief=self.brief, touched_paths=())
        self.assertTrue(decision["refresh"])
        self.assertEqual(decision["trigger"], RECONDENSE_PIN_DRIFT)
        detail = decision["details"][RECONDENSE_PIN_DRIFT]
        self.assertEqual(detail["checked"], 1)
        self.assertEqual(len(detail["stale"]), 1)
        self.assertTrue(detail["reasons"])

    def test_a_failed_gate_triggers_a_refresh_even_when_every_pin_matches(self):
        # The new case: the brief is byte-perfect and still buys no bypass,
        # because the plan's green-tree basis is what the gate voided.
        decision = recondense_decision(brief=self.brief, touched_paths=(),
                                       gate_failures=["verify"])
        self.assertTrue(decision["refresh"])
        self.assertEqual(decision["trigger"], RECONDENSE_GATE_FAILURE)
        self.assertEqual(
            decision["details"][RECONDENSE_GATE_FAILURE]["gates"], ["verify"])

    def test_an_executed_node_overlapping_the_brief_triggers_a_refresh(self):
        # Pins stay fresh because the bytes are identical; the brief is still
        # stale about this path, and that is exactly what a pin check misses.
        decision = recondense_decision(brief=self.brief,
                                       touched_paths=[self.cited])
        self.assertTrue(decision["refresh"])
        self.assertEqual(decision["trigger"], RECONDENSE_EXECUTED_OVERLAP)
        self.assertEqual(decision["details"][RECONDENSE_EXECUTED_OVERLAP]
                         ["paths"], [self.cited.replace("\\", "/")])

    def test_a_non_overlapping_executed_path_does_not_trigger(self):
        decision = recondense_decision(brief=self.brief,
                                       touched_paths=[self.other])
        self.assertFalse(decision["refresh"])
        self.assertEqual(decision["triggers"], [])

    def test_precedence_is_gate_then_pin_then_overlap(self):
        self._drift()
        decision = recondense_decision(brief=self.brief,
                                       touched_paths=[self.cited],
                                       gate_failures=["verify"])
        self.assertEqual(decision["triggers"],
                         [RECONDENSE_GATE_FAILURE, RECONDENSE_PIN_DRIFT,
                          RECONDENSE_EXECUTED_OVERLAP])
        self.assertEqual(decision["trigger"], RECONDENSE_GATE_FAILURE)
        # Every reason is reported, not only the winner: the audit trail is
        # how a reader tells a one-cause refresh from a compound one.
        self.assertEqual(len(decision["reasons"]), 3)

    def test_a_brief_citing_nothing_is_never_fresh(self):
        ack = recondense_decision(brief={"grounding": {"sources": []}},
                                  touched_paths=())
        self.assertTrue(ack["refresh"])
        self.assertEqual(ack["trigger"], RECONDENSE_PIN_DRIFT)

    def test_a_reused_freshness_report_is_honoured_not_recomputed(self):
        # A caller that already measured the brief must not pay to re-read
        # the tree, so the supplied report decides.
        self._drift()
        supplied = {"fresh": True, "checked": 1, "stale": [], "missing": [],
                    "reasons": []}
        decision = recondense_decision(brief=self.brief,
                                       freshness=supplied, touched_paths=())
        self.assertFalse(decision["refresh"])

    def test_path_forms_compare_after_normalization(self):
        decision = recondense_decision(
            brief=self.brief, touched_paths=["./" + self.cited,
                                             self.cited.replace("/", "\\")])
        self.assertEqual(decision["trigger"], RECONDENSE_EXECUTED_OVERLAP)

    def test_non_string_and_blank_paths_are_ignored_not_guessed(self):
        decision = recondense_decision(brief=self.brief,
                                       touched_paths=["", "   ", None, 5])
        self.assertFalse(decision["refresh"])

    def test_a_bare_string_of_gate_names_is_refused(self):
        # Iterating a string would count one failure per letter.
        with self.assertRaises(HarnessError):
            recondense_decision(brief=self.brief, gate_failures="verify")

    def test_covered_paths_read_both_declared_places(self):
        pack = {
            "grounding": {"sources": [{"path": "a/b.py"}, {"path": "a/b.py"}]},
            "scope": {"included": ["c.py", "a/b.py"]},
        }
        self.assertEqual(brief_covered_paths(pack), ["a/b.py", "c.py"])
        self.assertEqual(brief_covered_paths(None), [])


class BypassCompositionTests(RecondenseHarness):
    def test_no_stage_evidence_keeps_the_historical_behaviour(self):
        sel = resolve_stages(supplied_brief=True, brief=self.brief)
        self.assertIn(STAGE_CONTEXT, sel["bypassed"])
        self.assertNotIn(STAGE_CONTEXT, sel["denied"])
        # Nothing ran, so there is no decision to report -- and the key is
        # absent rather than a fabricated "no drift".
        self.assertNotIn("recondense", sel)

    def test_no_stage_evidence_still_denies_a_drifted_brief(self):
        self._drift()
        sel = resolve_stages(supplied_brief=True, brief=self.brief)
        self.assertIn(STAGE_CONTEXT, sel["denied"])
        self.assertNotIn(STAGE_CONTEXT, sel["bypassed"])

    def test_an_empty_touched_set_is_evidence_and_produces_a_decision(self):
        # Tri-state: None means "no stage ran"; () means "a stage ran and
        # wrote nothing". Only the first may skip the decision.
        sel = resolve_stages(supplied_brief=True, brief=self.brief,
                             touched_paths=())
        self.assertIn(STAGE_CONTEXT, sel["bypassed"])
        self.assertIn("recondense", sel)
        self.assertFalse(sel["recondense"]["refresh"])

    def test_a_failed_gate_denies_the_bypass_and_names_its_trigger(self):
        sel = resolve_stages(supplied_brief=True, brief=self.brief,
                             gate_failures=["verify"])
        self.assertNotIn(STAGE_CONTEXT, sel["bypassed"])
        self.assertIn(STAGE_CONTEXT, sel["denied"])
        self.assertIn(RECONDENSE_GATE_FAILURE, sel["denied"][STAGE_CONTEXT])
        # A denied context bypass is not a skip: the stage must run.
        self.assertIn(STAGE_CONTEXT, sel["stages"])

    def test_overlap_denies_the_bypass_while_the_pins_are_still_fresh(self):
        sel = resolve_stages(supplied_brief=True, brief=self.brief,
                             touched_paths=[self.cited])
        self.assertIn(RECONDENSE_EXECUTED_OVERLAP,
                      sel["denied"][STAGE_CONTEXT])

    def test_composition_carries_the_decision_and_the_envelope_surfaces_it(self):
        composition = compose_stages(
            budget=self._budget(), declared=["context", "planning"],
            supplied_brief=True, brief=self.brief,
            touched_paths=[self.cited])
        self.assertEqual(composition["recondense"]["trigger"],
                         RECONDENSE_EXECUTED_OVERLAP)
        self.assertIn(STAGE_CONTEXT, composition["denied"])
        envelope = composition_envelope(composition)
        self.assertEqual(envelope["recondense"]["trigger"],
                         RECONDENSE_EXECUTED_OVERLAP)
        self.assertIn(STAGE_CONTEXT, envelope["denied_bypass"])

    def test_the_envelope_omits_the_key_when_no_evidence_was_supplied(self):
        composition = compose_stages(
            budget=self._budget(), declared=["context", "planning"],
            supplied_brief=True, brief=self.brief)
        self.assertNotIn("recondense", composition)
        self.assertNotIn("recondense", composition_envelope(composition))

    def test_a_supplied_plan_bypasses_planning_while_context_is_denied(self):
        # The recondense decision belongs to `context`; a supplied plan's
        # bypass is a separate fact and must not be affected by it.
        sel = resolve_stages(supplied_brief=True, supplied_plan=True,
                             brief=self.brief, touched_paths=[self.cited])
        self.assertIn("planning", sel["bypassed"])
        self.assertIn(STAGE_CONTEXT, sel["denied"])
        # A denied bypass is not a skip, so `context` still runs.
        self.assertIn(STAGE_CONTEXT, sel["stages"])
        self.assertEqual(sel["recondense"]["trigger"],
                         RECONDENSE_EXECUTED_OVERLAP)

    def test_no_brief_means_no_decision_even_with_stage_evidence(self):
        # `supplied_plan` alone says nothing about the context stage, so
        # there is no bypass to decide and no decision to report.
        sel = resolve_stages(supplied_plan=True, touched_paths=[self.cited])
        self.assertIn("planning", sel["bypassed"])
        self.assertNotIn("recondense", sel)


if __name__ == "__main__":
    unittest.main()
