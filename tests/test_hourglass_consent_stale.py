"""HV-5 consent staleness event vocabulary."""
import json
import unittest

from harness.consent import (ConsentStalenessEvent, consent_binding,
                             consent_staleness)
from tests._applyfixture import (APPLY, ESC, JUDGE, ApplyFixture, CHANGED,
                                 ORIGINAL, scripted_run)
from tests._fake import comp, consent


class ConsentStalenessEventTests(unittest.TestCase):
    def test_membership_and_wire_values_are_exact(self):
        self.assertEqual(
            [event.value for event in ConsentStalenessEvent],
            [
                "changed_files",
                "changed_instruction",
                "changed_model",
                "changed_token_cost_limits",
                "changed_package",
            ],
        )

    def test_string_and_json_serialization_use_stable_value(self):
        event = ConsentStalenessEvent.CHANGED_INSTRUCTION
        self.assertEqual(str(event), "changed_instruction")
        self.assertEqual(json.dumps(event), '"changed_instruction"')

    def test_unknown_value_is_rejected(self):
        with self.assertRaises(ValueError):
            ConsentStalenessEvent("changed_prompt")

    def test_binding_reports_only_the_dimension_that_changed(self):
        facts = {
            "file_path": "src/app.py", "file_content": "before",
            "instruction": "edit the function", "edit_snippet": None,
            "backend": "harness", "max_lines": 500,
            "execution_models": [APPLY], "consent_models": [JUDGE],
            "max_tokens": 4096, "task_max_cost": 0.05,
        }
        baseline = consent_binding(**facts)
        cases = (
            ({"file_content": "after"},
             ConsentStalenessEvent.CHANGED_FILES),
            ({"proposed_content": "different partial candidate"},
             ConsentStalenessEvent.CHANGED_FILES),
            ({"instruction": "edit the other function"},
             ConsentStalenessEvent.CHANGED_INSTRUCTION),
            ({"execution_models": ["other/model"]},
             ConsentStalenessEvent.CHANGED_MODEL),
            ({"max_tokens": 2048},
             ConsentStalenessEvent.CHANGED_TOKEN_COST_LIMITS),
            ({"package": {"package_id": "other"}},
             ConsentStalenessEvent.CHANGED_PACKAGE),
        )
        for changes, expected in cases:
            with self.subTest(event=expected.value):
                updated = dict(facts, **changes)
                self.assertEqual(consent_staleness(
                    baseline, consent_binding(**updated)), (expected,))

    def test_legacy_continuation_requires_every_binding_dimension_again(self):
        current = consent_binding(
            file_path="app.py", file_content="x", instruction="edit",
            edit_snippet=None, backend="harness", max_lines=500,
            execution_models=[APPLY], consent_models=[JUDGE],
            max_tokens=4096, task_max_cost=0.05)
        self.assertEqual(consent_staleness(None, current),
                         tuple(ConsentStalenessEvent))


class ConsentContinuationFreshnessTests(ApplyFixture):
    def _failed_consent_task(self):
        path = self.make_file()
        fake, _, _, engine = self.make_env(
            posts=[consent("accept"), comp(CHANGED)],
            run=scripted_run([(1, "verify failed")]),
            default_consent=True, renew=False)
        result = engine.apply_edit(
            task_id="consent-resume", file_path=path, instruction="change",
            verify_cmd="check", require_consent=True, max_rounds=1)
        self.assertEqual(result["status"], "verify_failed")
        self.assertIn("consent_binding", result["continuation"])
        self.assertEqual(len(fake.chat_posts()), 2)
        return path, result["continuation"]

    def test_unchanged_consent_binding_reuses_decision_on_resume(self):
        _path, continuation = self._failed_consent_task()
        fake, _, _, engine = self.make_env(
            posts=[comp(CHANGED + "\n")], default_consent=True, renew=False)
        engine.run_verify = scripted_run([(0, "")])

        result = engine.apply_edit(
            continuation=continuation, instruction="change",
            require_consent=True, max_rounds=1)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(fake.chat_posts()), 1,
                         "matching work and limits reuse the consent decision")

    def test_changed_instruction_reprobes_before_dispatch(self):
        _path, continuation = self._failed_consent_task()
        fake, _, ledger, engine = self.make_env(
            posts=[consent("accept"), comp(CHANGED + "\n")],
            default_consent=True, renew=False)
        engine.run_verify = scripted_run([(0, "")])

        result = engine.apply_edit(
            continuation=continuation,
            instruction="change and add a type hint",
            require_consent=True, max_rounds=1)

        self.assertEqual(result["status"], "ok")
        posts = fake.chat_posts()
        self.assertEqual(len(posts), 2)
        self.assertIn("consent_stale",
                      [entry["event"] for entry in ledger.entries()])
        stale = next(entry for entry in ledger.entries()
                     if entry["event"] == "consent_stale")
        self.assertEqual(stale["events"], [
            ConsentStalenessEvent.CHANGED_INSTRUCTION.value])

    def test_changed_partial_candidate_reprobes_and_shows_candidate(self):
        _path, continuation = self._failed_consent_task()
        continuation["partial_content"] = "# changed partial candidate\n"
        fake, _, ledger, engine = self.make_env(
            posts=[consent("accept"), comp(CHANGED + "\n")],
            default_consent=True, renew=False)
        engine.run_verify = scripted_run([(0, "")])

        result = engine.apply_edit(
            continuation=continuation, instruction="change",
            require_consent=True, max_rounds=1)

        self.assertEqual(result["status"], "ok")
        posts = fake.chat_posts()
        self.assertEqual(len(posts), 2)
        self.assertIn("# changed partial candidate",
                      posts[0][2]["messages"][1]["content"])
        stale = next(entry for entry in ledger.entries()
                     if entry["event"] == "consent_stale")
        self.assertEqual(stale["events"], [
            ConsentStalenessEvent.CHANGED_FILES.value])

    def test_changed_explicit_execution_model_reprobes_before_dispatch(self):
        _path, continuation = self._failed_consent_task()
        fake, _, ledger, engine = self.make_env(
            posts=[consent("accept"), comp(CHANGED + "\n")],
            default_consent=True, renew=False)
        engine.run_verify = scripted_run([(0, "")])

        result = engine.apply_edit(
            continuation=continuation, instruction="change", model=ESC,
            require_consent=True, max_rounds=1)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(fake.chat_posts()), 2)
        stale = next(entry for entry in ledger.entries()
                     if entry["event"] == "consent_stale")
        self.assertEqual(stale["events"], [
            ConsentStalenessEvent.CHANGED_MODEL.value])


class PackageConsentTests(ApplyFixture):
    def test_package_and_context_are_shown_bound_and_typed_defer_stops_apply(self):
        class DeferConsentPolicy:
            def __init__(self):
                self.calls = []

            def evaluate_hourglass_stage(self, dimension, facts, **kwargs):
                self.calls.append((dimension, facts, kwargs))
                return object(), {
                    "native": True,
                    "consent_fresh": 0.99,
                    "consent_defer_required": 0.99,
                    "escalation_justified": 0.01,
                }

        package = {
            "package_id": "pkg-4",
            "target_files": ["src/app.py"],
            "dependencies": ["pkg-2"],
            "verification_gate": "python -m unittest",
            "execution_models": [APPLY],
            "limits": {"request_max_tokens": 1024, "task_max_cost": 0.01},
            "context": {"sha256": "context-digest", "excerpt": "relevant facts",
                        "truncated": False},
        }
        fake, _, _, engine = self.make_env(posts=[consent("accept")])
        policy = DeferConsentPolicy()
        engine.jev_policy = policy
        engine.gate.jev_policy = policy
        path = self.make_file()

        result = engine.apply_edit(
            task_id="package-consent", file_path=path, instruction="edit",
            consent_package=package, require_consent=True)

        self.assertEqual(result["status"], "deferred")
        with open(path, encoding="utf-8") as stream:
            self.assertEqual(stream.read(), ORIGINAL)
        self.assertEqual(len(fake.chat_posts()), 1,
                         "the apply model must not dispatch after typed defer")
        prompt = fake.chat_posts()[0][2]["messages"][1]["content"]
        for expected in ("WORK PACKAGE AND LIMITS", "pkg-4", "src/app.py",
                         "python -m unittest", "request_max_tokens", "context-digest"):
            self.assertIn(expected, prompt)
        self.assertEqual(policy.calls[0][0], "consent")
        self.assertEqual(policy.calls[0][1]["package"], package)
        self.assertTrue(result["hourglass_consent"]["native"])


if __name__ == "__main__":
    unittest.main()
