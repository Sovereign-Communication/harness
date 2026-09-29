"""Hermetic contract for consent staleness event vocabulary and bindings."""
import unittest

from harness.consent import (
    ConsentStalenessEvent, consent_binding_changes,
    consent_binding_is_fresh, consent_is_fresh,
    consent_staleness_events, make_consent_binding, valid_consent_binding,
)
from harness.token_budget import TokenBudget


class ConsentStalenessTests(unittest.TestCase):
    def setUp(self):
        self.bindings = {
            "changed_files": ("a.py", "b.py"),
            "instruction": "fix the issue",
            "selected_model": "worker-a",
            "token_limit": 4096,
            "monetary_limit": 0.25,
        }

    def test_vocabulary_and_serialized_values_are_stable(self):
        self.assertEqual(
            [event.value for event in ConsentStalenessEvent],
            ["changed_files", "context", "instruction", "selected_model",
             "token_limit", "monetary_limit"])
        self.assertEqual(consent_staleness_events("instruction"),
                         frozenset({ConsentStalenessEvent.INSTRUCTION}))
        self.assertEqual(consent_staleness_events(["selected_model"]),
                         frozenset({ConsentStalenessEvent.SELECTED_MODEL}))

    def test_unchanged_declared_bindings_remain_fresh(self):
        self.assertTrue(consent_is_fresh(
            self.bindings, dict(self.bindings), list(self.bindings)))

    def test_each_declared_change_makes_consent_stale(self):
        for event in ConsentStalenessEvent:
            with self.subTest(event=event.value):
                current = dict(self.bindings)
                current[event.value] = object()
                self.assertFalse(consent_is_fresh(
                    self.bindings, current, [event.value]))

    def test_invalid_event_input_is_refused(self):
        for invalid in ("unknown", ["instruction", "unknown"], [None], None):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                consent_staleness_events(invalid)
        with self.assertRaises(ValueError):
            consent_is_fresh(self.bindings, self.bindings, ["unknown"])

    def test_missing_bindings_fail_closed(self):
        self.assertFalse(consent_is_fresh({}, {}, ["instruction"]))

    def _package(self, **overrides):
        values = {
            "file_path": "repo/a.py",
            "source_content": "x = 1\n",
            "instruction": "change x",
            "context": {"request": "make x safe"},
            "package_id": "run/node-1",
            "selected_model": "worker-a",
            "max_tokens": 1024,
            "token_budget": TokenBudget(
                "exec", max_input_tokens=8000, max_output_tokens=1200),
            "task_max_cost": 0.05,
            "run_max_cost": 0.10,
        }
        values.update(overrides)
        return make_consent_binding(**values)

    def test_binding_is_deterministic_and_integrity_checked(self):
        first = self._package(context={"request": "make x safe", "n": 1})
        reordered = self._package(context={"n": 1, "request": "make x safe"})
        self.assertEqual(first, reordered)
        self.assertTrue(valid_consent_binding(first))
        self.assertTrue(consent_binding_is_fresh(first, reordered))
        changed = dict(first)
        changed["instruction"] = "0" * 64
        self.assertFalse(valid_consent_binding(changed))
        self.assertFalse(consent_binding_is_fresh(first, changed))

    def test_each_package_dimension_invalidates_binding(self):
        initial = self._package()
        changes = [
            {"source_content": "x = 2\n"},
            {"context": {"request": "different request"}},
            {"instruction": "different instruction"},
            {"selected_model": "worker-b"},
            {"max_tokens": 2048},
            {"task_max_cost": 0.02},
            {"token_budget": TokenBudget(
                "exec", max_input_tokens=4000, max_output_tokens=600)},
        ]
        for change in changes:
            with self.subTest(change=change):
                current = self._package(**change)
                self.assertFalse(consent_binding_is_fresh(initial, current))
                self.assertTrue(consent_binding_changes(initial, current))


if __name__ == "__main__":
    unittest.main()
