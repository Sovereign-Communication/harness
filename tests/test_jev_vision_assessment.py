"""HV-0 typed assessment, strict transport, spend, and privacy contracts."""
import io
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from harness._http import HttpTransport
from harness.config import HARD_MAX_COST, load_settings
from harness.errors import HarnessError
from harness.jev import JevEvaluationResult, JevEvaluator, jev_cost
from harness.jev_packs import (
    DEFAULT_VISION_ASSESSMENT_PACK,
    VISION_ASSESSMENT_CONFIDENCE_THRESHOLD,
    VISION_ASSESSMENT_MAX_REQUEST_TOKENS,
    VISION_ASSESSMENT_MAX_STATE_QUESTION_TOKENS,
    build_vision_assessment_state,
    sanitize_vision_state,
    validate_vision_assessment_answers,
    validate_vision_assessment_pack,
    vision_assessment_preflight,
    vision_assessment_question_pack,
)
from harness.jev_policy import policy_for
from harness.ledger import AutonomyLedger
from harness.tokens import estimate_prompt_tokens


def _answer(levels, selected=None, *, confidence=0.99, probabilities=None):
    top = len(levels) - 1
    selected = top if selected is None else selected
    probs = probabilities or {
        str(i): (1.0 if i == selected else 0.0) for i in range(len(levels))
    }
    return {
        "type": "score",
        "score": sum(int(key) * value for key, value in probs.items()),
        "confidence": confidence,
        "legend": {str(i): level for i, level in enumerate(levels)},
        "probabilities": probs,
    }


def _response(*, selected=None, confidence=0.99):
    questions = vision_assessment_question_pack()
    return {
        "model": "jev-1.13.0",
        "answers": {
            category: _answer(
                question["criteria"], selected=selected,
                confidence=confidence)
            for category, question in questions.items()
        },
        "usage": {"input_tokens": 321, "output_tokens": 44},
    }


class RecordingTransport:
    def __init__(self, status=200, response=None, error=None):
        self.status = status
        self.response = response if response is not None else _response()
        self.error = error
        self.calls = []

    def post_once(self, url, key, payload, timeout=120):
        self.calls.append((url, key, payload))
        if self.error:
            raise self.error
        return self.status, self.response

    def post(self, *args, **kwargs):
        raise AssertionError("HV-0 must use the one-attempt transport path")


class PostOnlyTransport:
    """A conventional retry-capable transport without the strict seam."""
    def __init__(self):
        self.calls = 0

    def post(self, *args, **kwargs):
        self.calls += 1
        return 200, _response()


class InjectedEvaluator:
    def __init__(self, result=None, error=None, *, transport=None, model="jev-test"):
        self.result = result
        self.error = error
        self.transport = transport or RecordingTransport()
        self.model = model
        self.api_key = "test-key"
        self.calls = []

    def evaluate_once(self, state, questions):
        self.calls.append((state, questions))
        if self.error is not None:
            raise self.error
        return self.result


class RecordingGovernor:
    def __init__(self, error=None):
        self.error = error
        self.reservations = []
        self.settlements = []

    def reserve(self, amount, label):
        if self.error:
            raise self.error
        token = object()
        self.reservations.append((token, amount, label))
        return token

    def reconcile(self, token, amount):
        self.settlements.append((token, amount))


class VisionAssessmentPackTests(unittest.TestCase):
    def test_runtime_pack_matches_json_and_has_exactly_ten_scores(self):
        pack_path = Path(__file__).resolve().parents[1] / "packs" / \
            "hourglass_vision_assessment.pack.json"
        with pack_path.open(encoding="utf-8") as stream:
            self.assertEqual(json.load(stream), DEFAULT_VISION_ASSESSMENT_PACK)
        questions = vision_assessment_question_pack()
        self.assertEqual(len(questions), 10)
        self.assertEqual(set(questions), set(DEFAULT_VISION_ASSESSMENT_PACK["categories"]))
        self.assertTrue(all(q["type"] == "score" for q in questions.values()))
        self.assertTrue(all(2 <= len(q["criteria"]) <= 10
                            for q in questions.values()))
        self.assertEqual(VISION_ASSESSMENT_CONFIDENCE_THRESHOLD, 0.80)

    def test_pack_rejects_invalid_header_and_category_fields(self):
        cases = []
        bad_id = json.loads(json.dumps(DEFAULT_VISION_ASSESSMENT_PACK))
        bad_id["id"] = "wrong"
        cases.append(("id", bad_id, "pack id"))
        bad_version = json.loads(json.dumps(DEFAULT_VISION_ASSESSMENT_PACK))
        bad_version["version"] = "2"
        cases.append(("version", bad_version, "pack version"))
        for invalid_threshold in (True, float("nan"), 1.1):
            bad_threshold = json.loads(json.dumps(DEFAULT_VISION_ASSESSMENT_PACK))
            bad_threshold["confidence_threshold"] = invalid_threshold
            cases.append(("threshold", bad_threshold, "confidence threshold"))
        mutations = (
            ("category_object", lambda spec: None),
            ("instructions", lambda spec: spec.update({"instructions": " "})),
            ("levels", lambda spec: spec.update({"levels": []})),
            ("evidence_refs", lambda spec: spec.update({"evidence_refs": []})),
            ("actions", lambda spec: spec.update({"improvement_actions": []})),
        )
        for name, mutate in mutations:
            bad_pack = json.loads(json.dumps(DEFAULT_VISION_ASSESSMENT_PACK))
            category = next(iter(bad_pack["categories"]))
            if name == "category_object":
                bad_pack["categories"][category] = None
                message = "category must be an object"
            else:
                mutate(bad_pack["categories"][category])
                message = {
                    "instructions": "instructions are required",
                    "levels": "Score levels",
                    "evidence_refs": "evidence refs are required",
                    "actions": "requires one declared action",
                }[name]
            cases.append((name, bad_pack, message))
        for name, pack, message in cases:
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, message):
                    validate_vision_assessment_pack(pack)

    def test_pack_rejects_missing_category_and_missing_bucket(self):
        pack = json.loads(json.dumps(DEFAULT_VISION_ASSESSMENT_PACK))
        del pack["categories"]["modularity"]
        with self.assertRaisesRegex(ValueError, "exactly the ten"):
            validate_vision_assessment_pack(pack)
        pack = json.loads(json.dumps(DEFAULT_VISION_ASSESSMENT_PACK))
        pack["categories"]["modularity"]["improvement_buckets"].pop()
        with self.assertRaisesRegex(ValueError, "below-top"):
            validate_vision_assessment_pack(pack)
        pack = json.loads(json.dumps(DEFAULT_VISION_ASSESSMENT_PACK))
        pack["categories"]["modularity"]["instructions"] = "tampered v1"
        with self.assertRaisesRegex(ValueError, "differ from the declared v1"):
            validate_vision_assessment_pack(pack)

    def test_answer_validator_rejects_every_malformed_score_component(self):
        mutations = {
            "not_score": lambda answer: answer.update({"type": "noul"}),
            "wrong_legend": lambda answer: answer["legend"].update({"0": "wrong"}),
            "bad_score": lambda answer: answer.update({"score": 99.0}),
            "probability_keys": lambda answer: answer.update({"probabilities": {"0": 1.0}}),
            "bad_probability": lambda answer: answer["probabilities"].update({"0": float("nan")}),
            "bad_sum": lambda answer: answer["probabilities"].update({"0": 0.5}),
            "bad_confidence": lambda answer: answer.update({"confidence": 2.0}),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                answers = json.loads(json.dumps(_response()["answers"]))
                mutate(answers["planning"])
                with self.assertRaises(ValueError):
                    validate_vision_assessment_answers(answers)

    def test_answer_validator_requires_exact_ids_and_rejects_non_objects(self):
        with self.assertRaisesRegex(ValueError, "exactly match"):
            validate_vision_assessment_answers(None)
        answers = _response()["answers"]
        answers["planning"] = None
        with self.assertRaisesRegex(ValueError, "not a Score"):
            validate_vision_assessment_answers(answers)

    def test_answer_rejects_score_inconsistent_with_distribution(self):
        response = _response()
        response["answers"]["planning"]["score"] = 0.0
        with self.assertRaisesRegex(ValueError, "differs from its probability"):
            validate_vision_assessment_answers(response["answers"])

    def test_state_sanitizer_redacts_key_ip_and_home_path(self):
        state = sanitize_vision_state({
            "safe": "Bearer sk-exampleabcdefghijklmnop",
            "key": "api_key=secret_value_1234567890",
            "json": '{"api_key": "json-secret-value-12345"}',
            "openrouter": "OPENROUTER_API_KEY=or-secret-value-123456789",
            "jev": 'HARNESS_JEV_API_KEY="jev-secret-value-123456789"',
            "access": "SERVICE_ACCESS_TOKEN: token-secret-value-12345",
            "aws": "AWS_SECRET_ACCESS_KEY=aws-secret-value-12345",
            "github": "GITHUB_TOKEN=gh-secret-value-123456",
            "session": "AWS_SESSION_TOKEN=aws-session-secret-123456",
            "gh": "GH_TOKEN=gh-short-secret-value-123456",
            "personal": "GITHUB_PAT=github-pat-secret-value-123456",
            "django_inline": "DJANGO_SECRET_KEY='django-secret-key-value-123456'",
            "django_punctuated": 'DJANGO_SECRET_KEY="django@!#key/value=tail"',
            "secret_with_spaces": "SECRET_KEY='space @#! value'",
            "short_inline_secret": "SECRET_KEY=x@#!",
            "escaped_quote_secret": r'SECRET_KEY="first\"@#!second"',
            "structured": {
                "OPENROUTER_API_KEY": "nested-secret-value-123456",
                "AWS_SECRET_ACCESS_KEY": "aws-structured-secret-123456",
                "GITHUB_TOKEN": "github-structured-secret-123456",
                "AWS_SESSION_TOKEN": "session-structured-secret-123456",
                "GH_TOKEN": "gh-structured-secret-123456",
                "CLIENT_SECRET": "client-structured-secret-123456",
                "DJANGO_SECRET_KEY": "django@!#structured-secret",
                "Authorization": "authorization-secret-123456",
            },
            "address": "192.0.2.9",
            "path": r"C:\Users\Alice\Documents\private.txt",
        })
        rendered = json.dumps(state)
        self.assertNotIn("sk-example", rendered)
        self.assertNotIn("secret_value", rendered)
        self.assertNotIn("json-secret-value", rendered)
        self.assertNotIn("or-secret-value", rendered)
        self.assertNotIn("jev-secret-value", rendered)
        self.assertNotIn("token-secret-value", rendered)
        self.assertNotIn("aws-secret-value", rendered)
        self.assertNotIn("gh-secret-value", rendered)
        self.assertNotIn("aws-session-secret", rendered)
        self.assertNotIn("gh-short-secret", rendered)
        self.assertNotIn("github-pat-secret", rendered)
        self.assertNotIn("django-secret-key-value", rendered)
        self.assertNotIn("django@!#key/value=tail", rendered)
        self.assertNotIn("space @#! value", rendered)
        self.assertNotIn("x@#!", rendered)
        self.assertNotIn("first", rendered)
        self.assertNotIn("second", rendered)
        self.assertNotIn("nested-secret-value", rendered)
        self.assertNotIn("aws-structured-secret", rendered)
        self.assertNotIn("github-structured-secret", rendered)
        self.assertNotIn("session-structured-secret", rendered)
        self.assertNotIn("gh-structured-secret", rendered)
        self.assertNotIn("client-structured-secret", rendered)
        self.assertNotIn("django@!#structured-secret", rendered)
        self.assertNotIn("authorization-secret", rendered)
        self.assertNotIn("192.0.2.9", rendered)
        self.assertNotIn("C:\\Users\\Alice", rendered)
        self.assertIn("[REDACTED]", rendered)
        self.assertIn("[IP_ADDRESS]", rendered)
        self.assertIn("[LOCAL_PATH]", rendered)

    def test_state_sanitizer_accepts_finite_json_primitives_and_rejects_invalid_values(self):
        value = {"n": None, "b": True, "i": 7, "f": 1.25, "s": "safe", "items": [False, 2]}
        self.assertEqual(sanitize_vision_state(value), value)
        for bad in (float("inf"), {1: "bad-key"}, {"value": object()}):
            with self.subTest(value=repr(bad)):
                with self.assertRaises(ValueError):
                    sanitize_vision_state(bad)

    def test_repo_state_rejects_missing_canonical_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            docs = Path(tmp) / "docs"
            docs.mkdir()
            (docs / "hourglass-vision.md").write_text("Vision", encoding="utf-8")
            (docs / "jev-roadmap.md").write_text("No realization section", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "realization section"):
                build_vision_assessment_state(tmp)

    def test_repo_state_rejects_symlinked_canonical_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            docs = Path(tmp) / "docs"
            docs.mkdir()
            (docs / "hourglass-vision.md").write_text("vision", encoding="utf-8")
            (docs / "jev-roadmap.md").write_text(
                "### Hourglass vision realization (`HV-*`)\n", encoding="utf-8")
            actual = Path.is_symlink

            def mark_docs_as_symlink(path):
                if path == docs:
                    return True
                return actual(path)

            with mock.patch.object(Path, "is_symlink", mark_docs_as_symlink):
                with self.assertRaisesRegex(ValueError, "symlink"):
                    build_vision_assessment_state(tmp)

    def test_repo_state_rejects_resolved_source_outside_root(self):
        with tempfile.TemporaryDirectory() as tmp, \
                tempfile.TemporaryDirectory() as outside:
            docs = Path(tmp) / "docs"
            docs.mkdir()
            vision_path = docs / "hourglass-vision.md"
            vision_path.write_text("vision", encoding="utf-8")
            (docs / "jev-roadmap.md").write_text(
                "### Hourglass vision realization (`HV-*`)\n", encoding="utf-8")
            outside_path = Path(outside) / "vision.md"
            resolve = Path.resolve

            def escape_vision_source(path, *args, **kwargs):
                if path == vision_path:
                    return outside_path
                return resolve(path, *args, **kwargs)

            with mock.patch.object(Path, "resolve", escape_vision_source):
                with self.assertRaisesRegex(ValueError, "escapes"):
                    build_vision_assessment_state(tmp)

    def test_preflight_measures_exact_dispatched_serialization(self):
        state = {"sources": [{"ref": "docs/example", "content": "café"}]}
        model = "jev-test"
        questions = vision_assessment_question_pack()
        serialized = json.dumps({"model": model, "state": state,
                                 "questions": questions})
        longest = max(questions.values(), key=lambda q: len(json.dumps(q)))
        state_question = json.dumps({"state": state, "question": longest})
        measured = vision_assessment_preflight(state, model)
        self.assertEqual(measured["payload_utf8_bytes"],
                         len(serialized.encode("utf-8")))
        self.assertEqual(measured["estimated_input_tokens"],
                         estimate_prompt_tokens(serialized))
        self.assertEqual(measured["estimated_state_longest_question_tokens"],
                         estimate_prompt_tokens(state_question))
        self.assertGreater(measured["request_margin_tokens"], 0)
        self.assertGreater(measured["state_longest_question_margin_tokens"], 0)
        self.assertLess(measured["estimated_input_tokens"],
                        VISION_ASSESSMENT_MAX_REQUEST_TOKENS)
        self.assertLess(measured["estimated_state_longest_question_tokens"],
                        VISION_ASSESSMENT_MAX_STATE_QUESTION_TOKENS)

    def test_preflight_state_sanitization_is_measured_not_raw(self):
        measured = vision_assessment_preflight(
            {"private": "Bearer secretabcdefghijklmnop"}, "jev-test")
        self.assertGreater(measured["payload_utf8_bytes"], 0)
        self.assertEqual(measured["payload_outline"]["question_count"], 10)

    def test_repo_state_uses_only_canonical_hourglass_sources(self):
        root = Path(__file__).resolve().parents[1]
        state = build_vision_assessment_state(str(root))
        refs = [source["ref"] for source in state["sources"]]
        self.assertEqual(refs, [
            "docs/hourglass-vision.md",
            "docs/jev-roadmap.md#hourglass-vision-realization",
        ])
        self.assertEqual(state["current_status"]["canon_next_slice"], "HV-0")
        self.assertTrue(all(len(source["sha256"]) == 64 for source in state["sources"]))


def _valid_result(*, fallback=False):
    return JevEvaluationResult(
        "pass", 0.99, 1.0, _response()["answers"], [],
        cost=jev_cost(321), input_tokens=321, output_tokens=44,
        is_fallback=fallback, model="jev-1.13.0", usage_observed=True,
        model_observed=True, input_tokens_observed=True,
        output_tokens_observed=True)


class VisionAssessmentPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._ledger_number = 0

    def _run(self, transport, *, keyed=True, governor=None, state=None,
             evaluator=None):
        # The host profile is private and may be outside the workspace. Keep
        # tests hermetic by pointing the settings loader at this test's temp
        # directory and by making every invocation use a fresh ledger.
        with mock.patch("harness.config.CONFIG_DIR", self.tmp.name), \
                mock.patch("harness.config.resolve_api_key", return_value=None):
            settings = load_settings({
                "jev_api_key": "test-key" if keyed else None,
                "jev_model": "jev-test"})
        ledger_path = os.path.join(
            self.tmp.name, "ledger-{}.jsonl".format(self._ledger_number))
        self._ledger_number += 1
        ledger = AutonomyLedger(ledger_path)
        policy = policy_for(
            settings, transport=transport,
            governor=governor if governor is not None else RecordingGovernor(),
            ledger=ledger, evaluator=evaluator)
        envelope = policy.evaluate_vision_assessment(
            {"assessment": "hourglass"} if state is None else state)
        return envelope, ledger

    def test_assessment_refuses_to_dispatch_without_ledger(self):
        transport = RecordingTransport()
        governor = RecordingGovernor()
        with mock.patch("harness.config.CONFIG_DIR", self.tmp.name), \
                mock.patch("harness.config.resolve_api_key", return_value=None):
            settings = load_settings({
                "jev_api_key": "test-key", "jev_model": "jev-test"})
        policy = policy_for(
            settings, transport=transport, governor=governor, ledger=None)
        envelope = policy.evaluate_vision_assessment(
            {"assessment": "hourglass"})
        self.assertEqual(envelope.status, "unassessed")
        self.assertIn("ledger unavailable", envelope.reasons[0])
        self.assertEqual(transport.calls, [])
        self.assertEqual(governor.reservations, [])

    def test_keyed_assessment_has_one_call_one_settlement_one_metadata_event(self):
        transport = RecordingTransport()
        governor = RecordingGovernor()
        envelope, ledger = self._run(transport, governor=governor)
        self.assertEqual(envelope.status, "assessed")
        self.assertTrue(envelope.perfect)
        self.assertEqual(len(envelope.categories), 10)
        self.assertTrue(all(item.selected_score_10 == 10.0
                            for item in envelope.categories.values()))
        self.assertEqual(len(transport.calls), 1)
        payload = transport.calls[0][2]
        self.assertEqual(len(payload["questions"]), 10)
        self.assertEqual(len(governor.reservations), 1)
        self.assertEqual(len(governor.settlements), 1)
        self.assertIs(governor.reservations[0][0], governor.settlements[0][0])
        expected_actual = jev_cost(321)
        self.assertAlmostEqual(governor.settlements[0][1], expected_actual)
        events = ledger.entries()
        self.assertEqual([row["event"] for row in events], ["jev_eval"])
        event = events[0]
        self.assertEqual(event["site"], "hourglass_vision_assessment")
        self.assertEqual(event["pack_id"], DEFAULT_VISION_ASSESSMENT_PACK["id"])
        self.assertEqual(event["pack_version"], DEFAULT_VISION_ASSESSMENT_PACK["version"])
        self.assertEqual(event["result_state"], "assessed")
        self.assertEqual(event["usage_source"], "actual")
        self.assertEqual(event["cost_source"], "actual_input")
        self.assertEqual(event["observed_model"], "jev-1.13.0")
        self.assertEqual(event["input_tokens"], 321)
        self.assertAlmostEqual(event["cost"], expected_actual)
        self.assertNotIn("payload", event)
        self.assertNotIn("api_key", event)
        serialized = json.dumps(envelope.to_dict())
        self.assertNotIn("can_mark_complete", serialized)
        self.assertNotIn("readiness", serialized)
        self.assertNotIn("phase_status", serialized)

    def test_over_ceiling_settlement_is_ledgered_and_returns_unassessed(self):
        transport = RecordingTransport()

        class OverCeilingGovernor(RecordingGovernor):
            def reconcile(self, token, amount):
                self.settlements.append((token, amount))
                raise HarnessError("actual running cost exceeds ceiling")

        governor = OverCeilingGovernor()
        envelope, ledger = self._run(transport, governor=governor)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(len(governor.settlements), 1)
        self.assertEqual(envelope.status, "unassessed")
        self.assertFalse(envelope.perfect)
        self.assertTrue(all(category is None
                            for category in envelope.categories.values()))
        self.assertIn("settlement failed", envelope.reasons[0])
        events = [event for event in ledger.entries()
                  if event["event"] == "jev_eval"
                  and event["site"] == "hourglass_vision_assessment"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["result_state"], "unassessed")
        self.assertEqual(events[0]["verdict"], "fail")
        self.assertEqual(events[0]["cost"], jev_cost(321))
        self.assertIn("settlement_error", events[0])

    def test_unkeyed_is_unassessed_and_records_refusal_not_evaluation(self):
        transport = RecordingTransport()
        governor = RecordingGovernor()
        envelope, ledger = self._run(transport, keyed=False, governor=governor)
        self.assertEqual(envelope.status, "unassessed")
        self.assertEqual(envelope.fallback_state, "not_dispatched")
        self.assertTrue(all(item is None for item in envelope.categories.values()))
        self.assertEqual(transport.calls, [])
        self.assertEqual(governor.reservations, [])
        self.assertEqual(governor.settlements, [])
        self.assertEqual([row["event"] for row in ledger.entries()], ["jev_refusal"])

    def test_budget_refusal_prevents_dispatch_and_settled_event(self):
        transport = RecordingTransport()
        governor = RecordingGovernor(HarnessError("budget denied"))
        envelope, ledger = self._run(transport, governor=governor)
        self.assertEqual(envelope.status, "unassessed")
        self.assertIn("budget denied", envelope.reasons[0])
        self.assertEqual(transport.calls, [])
        self.assertEqual([row["event"] for row in ledger.entries()], ["jev_refusal"])

    def test_retry_capable_transport_is_refused_before_reservation(self):
        transport = PostOnlyTransport()
        governor = RecordingGovernor()
        envelope, ledger = self._run(transport, governor=governor)
        self.assertEqual(envelope.status, "unassessed")
        self.assertIn("one-attempt TypeSafe transport", envelope.reasons[0])
        self.assertEqual(transport.calls, 0)
        self.assertEqual(governor.reservations, [])
        self.assertEqual(governor.settlements, [])
        self.assertEqual([row["event"] for row in ledger.entries()],
                         ["jev_refusal"])

    def test_over_context_request_refuses_without_reservation_or_dispatch(self):
        transport = RecordingTransport()
        governor = RecordingGovernor()
        huge_state = {"text": "word " * (VISION_ASSESSMENT_MAX_REQUEST_TOKENS * 4)}
        envelope, ledger = self._run(
            transport, governor=governor, state=huge_state)
        self.assertEqual(envelope.status, "unassessed")
        self.assertLessEqual(envelope.request_margin_tokens, 0)
        self.assertEqual(transport.calls, [])
        self.assertEqual(governor.reservations, [])
        self.assertEqual([row["event"] for row in ledger.entries()], ["jev_refusal"])

    def test_invalid_state_and_policy_capability_refusals_do_not_dispatch(self):
        cases = (
            ("invalid state", object(), None, "state is invalid"),
            ("missing one-shot evaluator", {"ok": True},
             type("NoEvaluator", (), {"model": "jev-test", "api_key": "test-key", "transport": RecordingTransport()})(),
             "one-attempt TypeSafe evaluator"),
            ("missing model and evaluator", {"ok": True},
             type("NoModelEvaluator", (), {"model": None, "api_key": "test-key", "transport": RecordingTransport()})(),
             "one-attempt TypeSafe evaluator"),
        )
        for name, state, evaluator, reason in cases:
            with self.subTest(name=name):
                transport = RecordingTransport()
                governor = RecordingGovernor()
                injected = evaluator
                if injected is None:
                    envelope, ledger = self._run(
                        transport, state={"bad": state}, governor=governor)
                else:
                    envelope, ledger = self._run(
                        transport, state=state, governor=governor,
                        evaluator=injected)
                self.assertEqual(envelope.status, "unassessed")
                self.assertIn(reason, envelope.reasons[0])
                self.assertEqual(governor.reservations, [])
                self.assertEqual(transport.calls, [])
                self.assertEqual(ledger.entries()[0]["event"], "jev_refusal")

    def test_hard_cost_cap_refuses_before_reservation(self):
        transport = RecordingTransport()
        governor = RecordingGovernor()
        evaluator = InjectedEvaluator(_valid_result(), transport=transport)
        with mock.patch("harness.jev_policy.HARD_MAX_COST", 0.0):
            envelope, ledger = self._run(
                transport, governor=governor, evaluator=evaluator)
        self.assertEqual(envelope.status, "unassessed")
        self.assertIn("HARD_MAX_COST", envelope.reasons[0])
        self.assertEqual(governor.reservations, [])
        self.assertEqual(evaluator.calls, [])
        self.assertEqual(ledger.entries()[0]["event"], "jev_refusal")

    def test_injected_evaluator_exception_and_fallback_never_assess(self):
        for name, evaluator in (
                ("exception", InjectedEvaluator(error=RuntimeError("provider"))),
                ("fallback", InjectedEvaluator(_valid_result(fallback=True)))):
            with self.subTest(name=name):
                transport = evaluator.transport
                governor = RecordingGovernor()
                envelope, ledger = self._run(
                    transport, governor=governor, evaluator=evaluator)
                self.assertEqual(envelope.status, "unassessed")
                self.assertTrue(all(item is None for item in envelope.categories.values()))
                self.assertEqual(len(governor.reservations), 1)
                self.assertEqual(len(governor.settlements), 1)
                self.assertEqual(ledger.entries()[0]["event"], "jev_eval")
                self.assertEqual(ledger.entries()[0]["result_state"], "unassessed")

    def test_retryable_http_and_transport_failures_are_one_attempt_unassessed(self):
        for status, response, error in (
                (429, {"error": {"message": "rate limited"}}, None),
                (503, {"error": {"message": "unavailable"}}, None),
                (200, None, OSError("offline"))):
            with self.subTest(status=status, error=bool(error)):
                transport = RecordingTransport(status, response, error)
                governor = RecordingGovernor()
                envelope, ledger = self._run(transport, governor=governor)
                self.assertEqual(envelope.status, "unassessed")
                self.assertTrue(all(item is None for item in envelope.categories.values()))
                self.assertEqual(len(transport.calls), 1)
                self.assertEqual(len(governor.reservations), 1)
                self.assertEqual(len(governor.settlements), 1)
                self.assertEqual(len(ledger.entries()), 1)
                self.assertEqual(ledger.entries()[0]["event"], "jev_eval")
                self.assertEqual(ledger.entries()[0]["result_state"], "unassessed")
                self.assertEqual(ledger.entries()[0]["fallback_state"], "not_used")
                estimated = jev_cost(envelope.estimated_input_tokens)
                self.assertEqual(envelope.cost_source, "estimated_input")
                self.assertAlmostEqual(governor.settlements[0][1], estimated)

    def test_malformed_assessment_is_atomic_but_settles_reported_usage(self):
        mutations = {
            "missing_id": lambda r: r["answers"].pop("planning"),
            "extra_id": lambda r: r["answers"].update({"extra": {}}),
            "wrong_legend": lambda r: r["answers"]["planning"]["legend"].update({"0": "wrong"}),
            "bad_score": lambda r: r["answers"]["planning"].update({"score": 99.0}),
            "nan_probability": lambda r: r["answers"]["planning"]["probabilities"].update({"0": float("nan")}),
            "bad_probability_sum": lambda r: r["answers"]["planning"]["probabilities"].update({"0": 0.5}),
            "bad_confidence": lambda r: r["answers"]["planning"].update({"confidence": 2.0}),
            "missing_model": lambda r: r.pop("model"),
            "missing_usage": lambda r: r.pop("usage"),
            "partial_usage": lambda r: r["usage"].pop("output_tokens"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                response = _response()
                mutate(response)
                transport = RecordingTransport(response=response)
                governor = RecordingGovernor()
                envelope, ledger = self._run(transport, governor=governor)
                self.assertEqual(envelope.status, "unassessed")
                self.assertTrue(all(item is None for item in envelope.categories.values()))
                self.assertEqual(len(transport.calls), 1)
                self.assertEqual(len(governor.reservations), 1)
                self.assertEqual(len(governor.settlements), 1)
                self.assertEqual(len(ledger.entries()), 1)
                self.assertEqual(ledger.entries()[0]["event"], "jev_eval")
                self.assertEqual(ledger.entries()[0]["result_state"], "unassessed")
                if name == "missing_usage":
                    estimated = jev_cost(envelope.estimated_input_tokens)
                    self.assertEqual(envelope.usage_source, "unavailable")
                    self.assertEqual(envelope.cost_source, "estimated_input")
                    self.assertAlmostEqual(envelope.cost_usd, estimated)
                    self.assertAlmostEqual(governor.settlements[0][1], estimated)
                    self.assertEqual(ledger.entries()[0]["cost_source"],
                                     "estimated_input")
                if name == "missing_model":
                    self.assertIsNone(ledger.entries()[0]["model"])
                    self.assertFalse(ledger.entries()[0]["model_observed"])
                if name == "partial_usage":
                    actual = jev_cost(321)
                    self.assertEqual(envelope.usage_source, "actual_partial")
                    self.assertEqual(envelope.cost_source, "actual_input")
                    self.assertAlmostEqual(envelope.cost_usd, actual)
                    self.assertAlmostEqual(governor.settlements[0][1], actual)

    def test_tied_probabilities_choose_lower_level_and_require_review(self):
        response = _response(selected=0, confidence=0.5)
        for answer in response["answers"].values():
            answer["probabilities"] = {"0": 0.0, "1": 0.5, "2": 0.5, "3": 0.0}
            answer["score"] = 1.5
        envelope, _ = self._run(RecordingTransport(response=response))
        self.assertEqual(envelope.status, "assessed")
        self.assertFalse(envelope.perfect)
        for category in envelope.categories.values():
            self.assertEqual(category.selected_level, 1)
            self.assertTrue(category.review_required)
            self.assertIsNotNone(category.improvement_bucket)

    def test_shared_account_price_is_below_hard_ceiling(self):
        self.assertAlmostEqual(jev_cost(1_000_000), 0.0042)
        self.assertLessEqual(jev_cost(VISION_ASSESSMENT_MAX_REQUEST_TOKENS),
                             HARD_MAX_COST)

    def test_evaluator_once_refuses_invalid_pack_missing_key_and_non_object_response(self):
        transport = RecordingTransport()
        evaluator = JevEvaluator(api_key="key", transport=transport)
        invalid = evaluator.evaluate_once({}, {"broken": {"type": "unknown"}})
        self.assertIn("invalid TypeSafe question pack", invalid.reasons[0])
        self.assertEqual(transport.calls, [])
        missing_key = JevEvaluator(api_key=None, transport=transport).evaluate_once(
            {}, vision_assessment_question_pack())
        self.assertTrue(missing_key.is_fallback)
        self.assertIn("key unavailable", missing_key.reasons[0])
        transport.response = None
        invalid_response = JevEvaluator(api_key="key", transport=transport).evaluate_once(
            {}, vision_assessment_question_pack())
        self.assertIn("expected an object", invalid_response.reasons[0])
        self.assertEqual(len(transport.calls), 1)

    def test_http_post_once_parses_success_and_non_json_http_error(self):
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def getcode(self):
                return 200
            def read(self):
                return b'{"ok":true}'
        opener = mock.Mock()
        opener.open.return_value = Response()
        with mock.patch("harness._http.urllib.request.build_opener",
                        return_value=opener):
            status, payload = HttpTransport().post_once("https://example.invalid", "key", {})
        self.assertEqual((status, payload), (200, {"ok": True}))
        error = urllib.error.HTTPError(
            "https://example.invalid", 503, "bad gateway", {}, io.BytesIO(b"upstream text"))
        opener.open.side_effect = error
        with mock.patch("harness._http.urllib.request.build_opener",
                        return_value=opener):
            status, payload = HttpTransport().post_once("https://example.invalid", "key", {})
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"]["message"], "upstream text")

    def test_evaluator_once_preserves_actual_usage_on_http_error(self):
        transport = RecordingTransport(
            429, {"usage": {"input_tokens": 100, "output_tokens": 0}})
        evaluator = JevEvaluator(api_key="key", transport=transport)
        result = evaluator.evaluate_once({}, vision_assessment_question_pack())
        self.assertEqual(len(transport.calls), 1)
        self.assertFalse(result.is_fallback)
        self.assertEqual(result.input_tokens, 100)
        self.assertEqual(result.output_tokens, 0)
        self.assertTrue(result.usage_observed)
        self.assertAlmostEqual(result.cost, jev_cost(100))

    def test_evaluator_once_refuses_transport_without_one_shot_method(self):
        transport = PostOnlyTransport()
        result = JevEvaluator(api_key="key", transport=transport).evaluate_once(
            {}, vision_assessment_question_pack())
        self.assertFalse(result.is_fallback)
        self.assertIn("one-attempt", result.reasons[0])
        self.assertEqual(transport.calls, 0)

    def test_http_post_once_does_not_retry_429(self):
        error = urllib.error.HTTPError(
            "https://api.typesafe.ai/v1/systemone", 429, "rate limit", {},
            io.BytesIO(b'{"error":{"message":"rate limited"}}'))
        opener = mock.Mock()
        opener.open.side_effect = error
        with mock.patch("harness._http.urllib.request.build_opener",
                        return_value=opener):
            status, response = HttpTransport().post_once(
                "https://api.typesafe.ai/v1/systemone", "key", {})
        self.assertEqual(status, 429)
        self.assertIn("error", response)
        self.assertEqual(opener.open.call_count, 1)

    def test_http_post_once_does_not_follow_redirect(self):
        error = urllib.error.HTTPError(
            "https://api.typesafe.ai/v1/systemone", 302, "found",
            {"Location": "https://elsewhere.invalid/redirect"},
            io.BytesIO(b"redirect"))
        opener = mock.Mock()
        opener.open.side_effect = error
        with mock.patch("harness._http.urllib.request.build_opener",
                        return_value=opener) as build_opener:
            status, response = HttpTransport().post_once(
                "https://api.typesafe.ai/v1/systemone", "key", {})
        self.assertEqual(status, 302)
        self.assertEqual(opener.open.call_count, 1)
        handler = build_opener.call_args.args[0]
        self.assertIsInstance(handler, urllib.request.HTTPRedirectHandler)
        self.assertIsNone(handler.redirect_request(
            None, None, 302, "found", {"Location": "https://elsewhere.invalid"},
            "https://elsewhere.invalid"))


class VisionAssessmentCliTests(unittest.TestCase):
    def test_preflight_only_refuses_over_limit_without_policy_construction(self):
        from harness.cli import _cmd_jev_vision_assessment
        from harness.cli_parser import build_parser

        opts = build_parser().parse_args([
            "jev-vision-assessment", "--preflight-only", "--json"])
        measured = {
            "fits_context": False, "estimated_input_tokens": 99,
            "estimated_state_longest_question_tokens": 20,
            "request_margin_tokens": -1,
            "state_longest_question_margin_tokens": 5,
            "payload_utf8_bytes": 50, "payload_outline": {"question_count": 10},
        }
        output = StringIO()
        with mock.patch("harness.cli.build_vision_assessment_state", return_value={}), \
                mock.patch("harness.cli.vision_assessment_preflight", return_value=measured), \
                mock.patch("harness.cli.policy_for") as policy_for, \
                redirect_stdout(output):
            with self.assertRaisesRegex(HarnessError, "preflight refused"):
                _cmd_jev_vision_assessment(opts, type("S", (), {"jev_model": "jev-test"})())
        policy_for.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["dispatch_attempts"], 0)

    def test_dispatch_emits_assessed_result_and_raises_after_unassessed_result(self):
        from harness.cli import _cmd_jev_vision_assessment
        from harness.cli_parser import build_parser

        opts = build_parser().parse_args(["jev-vision-assessment", "--json"])
        settings = type("S", (), {"jev_model": "jev-test"})()
        class Envelope:
            def __init__(self, status):
                self.status = status
            def to_dict(self):
                return {"status": self.status}
        class Policy:
            def __init__(self, status):
                self.status = status
                self.calls = []
            def evaluate_vision_assessment(self, state, *, task_id):
                self.calls.append((state, task_id))
                return Envelope(self.status)
        preflight = {
            "fits_context": True, "estimated_input_tokens": 10,
            "estimated_state_longest_question_tokens": 5,
            "request_margin_tokens": 10,
            "state_longest_question_margin_tokens": 5,
            "payload_utf8_bytes": 50, "payload_outline": {"question_count": 10},
        }
        for status in ("assessed", "unassessed"):
            with self.subTest(status=status):
                policy = Policy(status)
                output = StringIO()
                with mock.patch("harness.cli.build_vision_assessment_state", return_value={"state": 1}), \
                        mock.patch("harness.cli.vision_assessment_preflight", return_value=preflight), \
                        mock.patch("harness.cli.policy_for", return_value=policy) as factory, \
                        mock.patch("harness.cli.jev_face_governor", return_value=object()), \
                        mock.patch("harness.cli._ledger", return_value=object()), \
                        redirect_stdout(output):
                    if status == "assessed":
                        _cmd_jev_vision_assessment(opts, settings)
                    else:
                        with self.assertRaisesRegex(HarnessError, "unassessed"):
                            _cmd_jev_vision_assessment(opts, settings)
                factory.assert_called_once()
                self.assertEqual(policy.calls[0][0], {"state": 1})
                self.assertEqual(policy.calls[0][1], "hv-0-vision-assessment")
                self.assertEqual(json.loads(output.getvalue())["status"], status)

    def test_preflight_only_reports_measured_request_without_dispatch(self):
        from harness.cli import _cmd_jev_vision_assessment
        from harness.cli_parser import build_parser

        root = Path(__file__).resolve().parents[1]
        opts = build_parser().parse_args([
            "jev-vision-assessment", "--repo-root", str(root),
            "--preflight-only", "--json"])
        settings = type("Settings", (), {"jev_model": "jev-test"})()
        output = StringIO()
        with redirect_stdout(output):
            _cmd_jev_vision_assessment(opts, settings)
        report = json.loads(output.getvalue())
        self.assertEqual(report["status"], "preflight_only")
        self.assertEqual(report["dispatch_attempts"], 0)
        self.assertEqual(report["preflight"]["model"], "jev-test")
        self.assertEqual(report["preflight"]["payload_outline"]["question_count"], 10)
        self.assertGreater(report["preflight"]["payload_utf8_bytes"], 0)
        self.assertGreater(report["preflight"]["request_margin_tokens"], 0)
        self.assertGreater(
            report["preflight"]["state_longest_question_margin_tokens"], 0)

    def test_preflight_only_honors_zero_cost_ceiling(self):
        from harness.cli import _cmd_jev_vision_assessment
        from harness.cli_parser import build_parser

        opts = build_parser().parse_args([
            "jev-vision-assessment", "--preflight-only", "--max-cost", "0",
            "--json"])
        output = StringIO()
        with mock.patch("harness.cli.build_vision_assessment_state", return_value={}), \
                redirect_stdout(output):
            with self.assertRaisesRegex(HarnessError, "preflight refused"):
                _cmd_jev_vision_assessment(
                    opts, type("S", (), {"jev_model": "jev-test", "max_cost": 0.05})())
        report = json.loads(output.getvalue())
        self.assertEqual(report["effective_max_cost_usd"], 0.0)
        self.assertGreater(report["worst_case_reserve_usd"], 0.0)
        self.assertEqual(report["dispatch_attempts"], 0)

    def test_main_preflight_does_not_load_dispatch_settings_or_resolve_key(self):
        from harness.cli import main
        import tempfile

        root = Path(__file__).resolve().parents[1]
        output = StringIO()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("harness.config.CONFIG_DIR", tmp), \
                mock.patch("harness.cli.load_settings",
                           side_effect=AssertionError("dispatch settings loaded")), \
                mock.patch("harness.config.resolve_jev_key",
                           side_effect=AssertionError("credential resolution used")), \
                redirect_stdout(output):
            main(["jev-vision-assessment", "--repo-root", str(root),
                  "--preflight-only", "--json"])
        report = json.loads(output.getvalue())
        self.assertEqual(report["status"], "preflight_only")
        self.assertEqual(report["dispatch_attempts"], 0)

    def test_preflight_settings_read_only_relevant_fields_and_allow_cli_cap(self):
        from harness.config import load_vision_preflight_settings

        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "config.json").write_text(json.dumps({
                "jev_model": "jev-configured",
                "max_cost": 0.012,
                "jev_api_key": "must-never-resolve",
            }), encoding="utf-8")
            with mock.patch("harness.config.CONFIG_DIR", tmp), \
                    mock.patch("harness.config.resolve_jev_key",
                               side_effect=AssertionError("credential resolution used")):
                configured = load_vision_preflight_settings()
                overridden = load_vision_preflight_settings(
                    max_cost_override=0.001)
        self.assertEqual(configured.jev_model, "jev-configured")
        self.assertEqual(configured.max_cost, 0.012)
        self.assertEqual(overridden.max_cost, 0.001)


if __name__ == "__main__":
    unittest.main()
