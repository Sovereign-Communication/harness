"""Hermetic gate tests for JEV-BAR sentiment buckets (site=phase_completion).

Covers:
- Pack JSON == in-code default; validator accepts it; each ValueError branch rejected.
- Question pack: one score per axis with criteria == declared levels; primary_gap
  choice keys == bucket ids + "none".
- Heuristic per axis for: merged+tests (proven), open PR (at_risk), missing tests
  (blocking), no required tests (mixed), residual row (at_risk), residual "not blocking"
  (mixed), user_facing w/o dogfood (at_risk), no status row.
- Policy keyed path with fake evaluator + fake governor + fake ledger: valid legend
  answers -> live levels; code-authority axis cannot be raised by Jev; jev-authority
  axis can; out-of-pack primary_gap refused with reason; one axis invalid -> that axis
  None only; all invalid -> is_fallback; transport HarnessError -> fallback and
  reservation reconciled; EXACTLY ONE ledger jev_eval with site=phase_completion
  per call in every path.
- Unkeyed policy: no evaluator network call, one jev_eval is_fallback=True.
- Score: hard gates pass but an axis blocking -> bar fails; improvements carry declared
  bucket actions; hard gate failures map to buckets; primary flag.
- Regression: "OpenRouter" / "reopened" rows do not trigger the open blocker;
  "deferred" alone is not a hard blocker.
- JEV-P6 row "**complete — this PR**" (no PR #65/merge) -> bar fails with merge_pending.
- P4-like row "**complete** | PR #46 MERGED a021cfc; residual: dogfood A/B" ->
  residual_untracked improvement present.
- CLI: single phase text shows improvements; --all --json on a temp repo; --all exits
  nonzero only on false_complete.
"""
import copy
import io
import json
import os
import tempfile
from types import SimpleNamespace
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from harness.cli import _cmd_jev_phase, _write_jev_phase_out
from harness.cli_parser import build_parser
from harness.config import load_settings
from harness.errors import HarnessError
from harness.jev import JevEvaluationResult
from harness.jev_completion import (
    DEFAULT_COMPLETION_PACK,
    load_evidence_file,
    score_phase_completion,
)
from harness.jev_packs import (
    DEFAULT_PHASE_COMPLETION_PACK,
    PHASE_COMPLETION_SITE,
    completion_bar_question_pack,
    heuristic_completion_sentiment,
    load_completion_pack,
    match_completion_keywords,
    phase_status_claims_complete,
    phase_status_has_blocker,
    phase_status_has_residual,
    validate_completion_pack,
)
from harness.jev_policy import policy_for
from harness.ledger import AutonomyLedger


class _CountingGovernor:
    def __init__(self, max_cost=1.0):
        self.reserved = []
        self.reconciled = []
        self.actual = []
        self.max_cost = max_cost
        self.spent = 0.0

    def reserve(self, worst, label):
        self.reserved.append((worst, label))
        return {"label": label, "worst": worst}

    def reconcile(self, reservation, cost):
        self.reconciled.append((reservation, cost))
        self.spent += float(cost or 0.0)

    def record_actual(self, cost, model):
        self.actual.append((cost, model))
        self.spent += float(cost or 0.0)


class _FailingGovernor:
    def reserve(self, worst, label):
        raise HarnessError(f"budget exceeded for {label}")

    def reconcile(self, reservation, cost):
        pass


class _JevTransport:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, key, payload, timeout=45):
        self.calls.append(payload)
        return 200, self.response


def _build_live_answers(pack_doc, axis_indices=None, primary_gap="merge_pending"):
    levels = pack_doc["sentiment"]["levels"]
    answers = {}
    for axis in pack_doc["axes"]:
        chosen_idx = 4
        if axis_indices and axis in axis_indices:
            chosen_idx = axis_indices[axis]
        anchors = [str(i) for i in range(len(levels))]
        legend = {str(i): levels[i] for i in range(len(levels))}
        n = len(levels)
        probs = {a: (0.8 if int(a) == chosen_idx else round(0.2 / max(1, n - 1), 6))
                 for a in anchors}
        total = sum(probs.values())
        probs = {k: round(v / total, 6) for k, v in probs.items()}
        drift = round(1.0 - sum(probs.values()), 6)
        first = anchors[0]
        probs[first] = round(probs[first] + drift, 6)
        answers[axis] = {
            "type": "score",
            "score": float(chosen_idx),
            "legend": legend,
            "probabilities": probs,
            "confidence": 0.95,
        }

    all_buckets = list(pack_doc["buckets"]) + ["none"]
    b_probs = {b: (0.7 if b == primary_gap else round(0.3 / max(1, len(all_buckets) - 1), 6))
               for b in all_buckets}
    b_total = sum(b_probs.values())
    b_probs = {k: round(v / b_total, 6) for k, v in b_probs.items()}
    b_drift = round(1.0 - sum(b_probs.values()), 6)
    b_first = all_buckets[0]
    b_probs[b_first] = round(b_probs[b_first] + b_drift, 6)
    answers["primary_gap"] = {
        "type": "choice",
        "choice": primary_gap,
        "probabilities": b_probs,
        "confidence": 0.90,
    }
    return answers


class TestPackValidation(unittest.TestCase):
    def test_json_file_equals_in_code_default(self):
        repo_root = Path(__file__).resolve().parents[1]
        json_path = repo_root / DEFAULT_PHASE_COMPLETION_PACK
        self.assertTrue(json_path.is_file(), f"missing canonical pack JSON: {json_path}")
        with open(json_path, encoding="utf-8") as fh:
            loaded_json = json.load(fh)
        self.assertEqual(loaded_json, DEFAULT_COMPLETION_PACK)
        validated = validate_completion_pack(loaded_json)
        self.assertEqual(validated["id"], "harness-phase-completion-v1")
        self.assertEqual(len(validated["sentiment"]["levels"]), 5)

    def test_validate_completion_pack_returns_deep_copy(self):
        pack = copy.deepcopy(DEFAULT_COMPLETION_PACK)
        validated = validate_completion_pack(pack)
        self.assertEqual(validated["id"], pack["id"])
        validated["sentiment"]["levels"].append("extra")
        self.assertEqual(len(pack["sentiment"]["levels"]), 5)

    def test_validation_error_branches(self):
        valid = copy.deepcopy(DEFAULT_COMPLETION_PACK)

        def assert_bad(mutator_or_val, msg_substr):
            if callable(mutator_or_val):
                bad = copy.deepcopy(valid)
                mutator_or_val(bad)
            else:
                bad = mutator_or_val
            with self.assertRaises(ValueError) as ctx:
                validate_completion_pack(bad)
            self.assertIn(msg_substr, str(ctx.exception))

        assert_bad(None, "must be an object")
        assert_bad(123, "must be an object")

        # sentiment
        assert_bad(lambda p: p.update({"sentiment": []}), "sentiment block object")
        assert_bad(lambda p: p["sentiment"].update({"levels": ["only_one"]}), "at least two")
        assert_bad(lambda p: p["sentiment"].update({"levels": ["a", 123]}), "at least two")
        assert_bad(lambda p: p["sentiment"].update({"levels": ["a", "a"]}), "unique")
        assert_bad(lambda p: p["sentiment"].update({"ordinals": [0, 50]}), "matching levels length")
        assert_bad(lambda p: p["sentiment"].update({"ordinals": [0, True, 50, 75, 100]}), "must be numbers")
        assert_bad(lambda p: p["sentiment"].update({"ordinals": [-1, 35, 60, 85, 100]}), "within 0..100")
        assert_bad(lambda p: p["sentiment"].update({"ordinals": [0, 35, 60, 85, 105]}), "within 0..100")
        assert_bad(lambda p: p["sentiment"].update({"ordinals": [0, 50, 40, 85, 100]}), "non-decreasing")
        assert_bad(lambda p: p["sentiment"].update({"blocking_max_index": True}), "in-range int")
        assert_bad(lambda p: p["sentiment"].update({"blocking_max_index": 10}), "in-range int")
        assert_bad(lambda p: p["sentiment"].update({"improve_below_index": -1}), "in-range int")
        assert_bad(lambda p: p["sentiment"].update({"improve_below_index": 10}), "in-range int")

        # buckets
        assert_bad(lambda p: p.update({"buckets": {}}), "non-empty buckets map")
        assert_bad(lambda p: p.update({"buckets": "bad"}), "non-empty buckets map")
        assert_bad(lambda p: p["buckets"].update({"": {"label": "x"}}), "bucket ids must be non-empty")
        assert_bad(lambda p: p["buckets"].update({"none": {"label": "x"}}), "reserved")
        assert_bad(lambda p: p["buckets"].update({"b1": "not-dict"}), "must be an object")
        assert_bad(lambda p: p["buckets"]["merge_pending"].update({"label": ""}), "requires a non-empty label")
        assert_bad(lambda p: p["buckets"]["merge_pending"].update({"path_id": ""}), "requires a non-empty path_id")
        assert_bad(lambda p: p["buckets"]["merge_pending"].update({"keywords": ["ok", ""]}), "list of non-empty strings")
        assert_bad(lambda p: p["buckets"]["merge_pending"].update({"suggested_next_action": ""}), "non-empty suggested_next_action")

        # axes
        assert_bad(lambda p: p.update({"axes": {}}), "non-empty axes map")
        assert_bad(lambda p: p["axes"].update({"": {"authority": "code"}}), "axis ids must be non-empty")
        assert_bad(lambda p: p["axes"].update({"primary_gap": {"authority": "code"}}), "reserved")
        assert_bad(lambda p: p["axes"].update({"a1": "not-dict"}), "must be an object")
        assert_bad(lambda p: p["axes"]["merge_evidence"].update({"authority": "bad"}), "must be 'code' or 'jev'")
        assert_bad(lambda p: p["axes"]["merge_evidence"].update({"bucket": "unknown_bucket"}), "declared bucket id")
        assert_bad(lambda p: p["axes"]["merge_evidence"].update({"instructions": ""}), "non-empty instructions")

    def test_load_completion_pack(self):
        repo_root = Path(__file__).resolve().parents[1]
        loaded = load_completion_pack(str(repo_root))
        self.assertEqual(loaded["id"], "harness-phase-completion-v1")

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(HarnessError) as ctx:
                load_completion_pack(tmp, "nonexistent.json")
            self.assertIn("cannot read completion pack", str(ctx.exception))

            bad_json = Path(tmp) / "bad.json"
            bad_json.write_text("{not valid json", encoding="utf-8")
            with self.assertRaises(HarnessError) as ctx:
                load_completion_pack(tmp, "bad.json")
            self.assertIn("invalid completion pack JSON", str(ctx.exception))

            bad_pack = Path(tmp) / "bad_pack.json"
            bad_pack.write_text(json.dumps({"id": ""}), encoding="utf-8")
            with self.assertRaises(HarnessError) as ctx:
                load_completion_pack(tmp, "bad_pack.json")
            self.assertIn("invalid completion pack", str(ctx.exception))

    def test_completion_bar_question_pack(self):
        qpack = completion_bar_question_pack(DEFAULT_COMPLETION_PACK)
        self.assertIn("primary_gap", qpack)
        self.assertEqual(qpack["primary_gap"]["type"], "choice")
        self.assertEqual(qpack["primary_gap"]["criteria"]["none"], "no improvement needed")
        for bid in DEFAULT_COMPLETION_PACK["buckets"]:
            self.assertIn(bid, qpack["primary_gap"]["criteria"])
        for axis in DEFAULT_COMPLETION_PACK["axes"]:
            self.assertIn(axis, qpack)
            self.assertEqual(qpack[axis]["type"], "score")
            self.assertEqual(qpack[axis]["criteria"], DEFAULT_COMPLETION_PACK["sentiment"]["levels"])


class TestHeuristicSentiment(unittest.TestCase):
    def setUp(self):
        self.pack = copy.deepcopy(DEFAULT_COMPLETION_PACK)

    def test_heuristic_merge_evidence(self):
        # pr_merged -> 4
        ev = {"pr_merged": True, "status_row": "| JEV-P1 | complete | PR #35 MERGED |"}
        res = heuristic_completion_sentiment(ev, self.pack)
        self.assertEqual(res["merge_evidence"], 4)

        # status mentions pr but not merged -> 1
        ev = {"pr_merged": False, "status_row": "| JEV-P1 | in progress | PR #35 OPEN |",
              "pr_pattern": r"PR #35"}
        res = heuristic_completion_sentiment(ev, self.pack)
        self.assertEqual(res["merge_evidence"], 1)

        # no status row or no pr mentioned -> 0
        ev = {"pr_merged": False, "status_row": "| JEV-P1 | in progress | no pr |"}
        res = heuristic_completion_sentiment(ev, self.pack)
        self.assertEqual(res["merge_evidence"], 0)

        ev = {"pr_merged": False, "status_row": ""}
        res = heuristic_completion_sentiment(ev, self.pack)
        self.assertEqual(res["merge_evidence"], 0)

    def test_heuristic_gate_tests(self):
        # tests present -> 4
        ev = {"required_tests": ["t1.py"], "tests_missing": []}
        res = heuristic_completion_sentiment(ev, self.pack)
        self.assertEqual(res["gate_tests"], 4)

        # missing tests -> 0
        ev = {"required_tests": ["t1.py"], "tests_missing": ["t1.py"]}
        res = heuristic_completion_sentiment(ev, self.pack)
        self.assertEqual(res["gate_tests"], 0)

        # no required tests declared -> 2 (mixed)
        ev = {"required_tests": [], "tests_missing": []}
        res = heuristic_completion_sentiment(ev, self.pack)
        self.assertEqual(res["gate_tests"], 2)

    def test_heuristic_verification(self):
        # missing tests -> 0
        ev = {"tests_missing": ["t1.py"], "local_gates_green": True, "ci_green": True}
        res = heuristic_completion_sentiment(ev, self.pack)
        self.assertEqual(res["verification"], 0)

        # explicit gate_output/ci_run + local + ci -> 4
        ev = {"gate_output": "OK", "local_gates_green": True, "ci_green": True}
        res = heuristic_completion_sentiment(ev, self.pack)
        self.assertEqual(res["verification"], 4)

        ev = {"ci_run": "https://ci/1", "local_gates_green": True, "ci_green": True}
        res = heuristic_completion_sentiment(ev, self.pack)
        self.assertEqual(res["verification"], 4)

        # local + ci but no explicit gate_output/ci_run -> 3
        ev = {"local_gates_green": True, "ci_green": True}
        res = heuristic_completion_sentiment(ev, self.pack)
        self.assertEqual(res["verification"], 3)

        # not green -> 1
        ev = {"local_gates_green": False, "ci_green": True}
        res = heuristic_completion_sentiment(ev, self.pack)
        self.assertEqual(res["verification"], 1)

    def test_heuristic_status_honesty(self):
        # no status row -> 0
        res = heuristic_completion_sentiment({}, self.pack)
        self.assertEqual(res["status_honesty"], 0)

        # claims complete + blocker -> 0
        ev = {"status_row": "| P1 | **complete** | blocked on CI |"}
        res = heuristic_completion_sentiment(ev, self.pack)
        self.assertEqual(res["status_honesty"], 0)

        # claims complete, no blocker -> 4
        ev = {"status_row": "| P1 | **complete** | PR #35 MERGED |"}
        res = heuristic_completion_sentiment(ev, self.pack)
        self.assertEqual(res["status_honesty"], 4)

        # honest open row (no complete claim) -> 4
        ev = {"status_row": "| P1 | open | in progress |"}
        res = heuristic_completion_sentiment(ev, self.pack)
        self.assertEqual(res["status_honesty"], 4)

    def test_heuristic_residual_scope(self):
        # residual without "not blocking" -> 1
        ev = {"status_row": "| P4 | **complete** | residual: dogfood A/B |"}
        res = heuristic_completion_sentiment(ev, self.pack)
        self.assertEqual(res["residual_scope"], 1)

        # residual with "not blocking" -> 2
        ev = {"status_row": "| P2 | **complete** | JEV-P2-jury deferred (not blocking) |"}
        res = heuristic_completion_sentiment(ev, self.pack)
        self.assertEqual(res["residual_scope"], 2)

        # no residual -> 4
        ev = {"status_row": "| P1 | **complete** | all clean |"}
        res = heuristic_completion_sentiment(ev, self.pack)
        self.assertEqual(res["residual_scope"], 4)

    def test_heuristic_dogfood(self):
        # not user facing -> 4
        ev = {"user_facing": False}
        res = heuristic_completion_sentiment(ev, self.pack)
        self.assertEqual(res["dogfood"], 4)

        # user facing with dogfood evidence -> 3
        ev = {"user_facing": True, "status_row": "| P5 | complete | live dogfood OK |"}
        res = heuristic_completion_sentiment(ev, self.pack)
        self.assertEqual(res["dogfood"], 3)

        ev = {"user_facing": True, "origin_evidence": "smoke receipts green"}
        res = heuristic_completion_sentiment(ev, self.pack)
        self.assertEqual(res["dogfood"], 3)

        # user facing without dogfood evidence -> 1
        ev = {"user_facing": True, "status_row": "| P5 | complete | PR #42 MERGED |"}
        res = heuristic_completion_sentiment(ev, self.pack)
        self.assertEqual(res["dogfood"], 1)

    def test_heuristic_unknown_axis_defaults_to_mixed(self):
        pack = copy.deepcopy(self.pack)
        pack["axes"]["custom_operator_axis"] = {
            "authority": "code",
            "bucket": "merge_pending",
            "instructions": "custom",
        }
        res = heuristic_completion_sentiment({"status_row": ""}, pack)
        self.assertEqual(res["custom_operator_axis"], 2)

    def test_match_completion_keywords(self):
        bid, count, hits = match_completion_keywords("PR is not merged, draft pending", self.pack)
        self.assertEqual(bid, "merge_pending")
        self.assertGreater(count, 0)
        self.assertIn("not merged", hits)

        bid, count, hits = match_completion_keywords("all clean and green", self.pack)
        self.assertIsNone(bid)
        self.assertEqual(count, 0)
        self.assertEqual(hits, [])


class TestRegressionBlockers(unittest.TestCase):
    def test_openrouter_and_reopened_not_blockers(self):
        # OpenRouter mentions must not match word-boundary open
        self.assertFalse(phase_status_has_blocker("OpenRouter model pool updated"))
        # reopened must not match word-boundary open
        self.assertFalse(phase_status_has_blocker("HG reopened on two confirmed defects"))
        # word-boundary open matches
        self.assertTrue(phase_status_has_blocker("status is open"))
        self.assertTrue(phase_status_has_blocker("| track | open |"))

    def test_deferred_alone_is_not_hard_blocker(self):
        self.assertFalse(phase_status_has_blocker("JEV-P2-jury deferred (follow-up)"))
        self.assertTrue(phase_status_has_residual("JEV-P2-jury deferred (follow-up)"))

    def test_phase_status_claims_complete(self):
        self.assertTrue(phase_status_claims_complete("| P1 | **complete** | PR #35 |"))
        self.assertTrue(phase_status_claims_complete("| P1 | complete | PR #35 |"))
        self.assertFalse(phase_status_claims_complete("| P1 | in progress | PR #35 |"))


class TestKeyedPolicyPath(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = AutonomyLedger(os.path.join(self.tmp.name, "ledger.jsonl"))
        self.pack = copy.deepcopy(DEFAULT_COMPLETION_PACK)

    def test_unkeyed_policy_path(self):
        settings = load_settings()
        settings.jev_api_key = None
        policy = policy_for(settings, transport=None, governor=None, ledger=self.ledger)
        state = {"phase": "JEV-P1", "status_row": "not merged draft PR"}
        result, structural, judgment = policy.evaluate_phase_completion(state, self.pack)
        self.assertTrue(judgment["is_fallback"])
        self.assertEqual(judgment["primary_gap"], "merge_pending")
        self.assertEqual(structural["site"], PHASE_COMPLETION_SITE)

        # verify ONE jev_eval entry in ledger
        with open(os.path.join(self.tmp.name, "ledger.jsonl"), encoding="utf-8") as fh:
            lines = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["event"], "jev_eval")
        self.assertEqual(lines[0]["site"], PHASE_COMPLETION_SITE)
        self.assertTrue(lines[0]["is_fallback"])

    def test_keyed_live_path_valid_answers(self):
        answers = _build_live_answers(self.pack, axis_indices={"merge_evidence": 4, "dogfood": 3},
                                      primary_gap="dogfood_missing")
        response = {"model": "jev-test", "answers": answers,
                    "usage": {"input_tokens": 150, "output_tokens": 50}}
        transport = _JevTransport(response)
        gov = _CountingGovernor()
        settings = load_settings({"jev_api_key": "test-key"})
        policy = policy_for(settings, transport=transport, governor=gov, ledger=self.ledger)

        state = {"phase": "JEV-P5", "status_row": "| JEV-P5 | **complete** | PR #42 MERGED |"}
        result, structural, judgment = policy.evaluate_phase_completion(state, self.pack)
        self.assertFalse(judgment["is_fallback"])
        self.assertEqual(judgment["live_levels"]["merge_evidence"], 4)
        self.assertEqual(judgment["live_levels"]["dogfood"], 3)
        self.assertEqual(judgment["primary_gap"], "dogfood_missing")
        self.assertEqual(structural["site"], PHASE_COMPLETION_SITE)

        # governor accounting
        self.assertEqual(len(gov.reserved), 1)
        self.assertEqual(len(gov.reconciled), 1)
        self.assertAlmostEqual(gov.reconciled[0][1], result.cost)

        # exactly one jev_eval in ledger
        with open(os.path.join(self.tmp.name, "ledger.jsonl"), encoding="utf-8") as fh:
            lines = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual(len(lines), 1)
        self.assertFalse(lines[0]["is_fallback"])

    def test_code_vs_jev_authority_in_score_phase_completion(self):
        # Code authority: merge_evidence heuristic is 1 (at_risk).
        # Even if live Jev gives 4 (proven), effective CANNOT be raised above code (min(1, 4) = 1).
        # Jev authority: status_honesty heuristic is 4 (proven).
        # If live Jev gives 1 (at_risk), live replaces code (effective = 1).
        answers = _build_live_answers(
            self.pack,
            axis_indices={"merge_evidence": 4, "status_honesty": 1},
            primary_gap="status_dishonest",
        )
        response = {"model": "jev-test", "answers": answers,
                    "usage": {"input_tokens": 120, "output_tokens": 40}}
        transport = _JevTransport(response)
        gov = _CountingGovernor()
        settings = load_settings({"jev_api_key": "test-key"})
        policy = policy_for(settings, transport=transport, governor=gov, ledger=self.ledger)

        evidence = {
            "phase": "JEV-P1",
            "repo_root": ".",
            "pr_merged": False,
            "status_row": "| JEV-P1 | in progress | PR #35 OPEN |",
            "pr_pattern": r"PR #35",
            "open_blockers": [],
            "required_tests": ["test.py"],
            "tests_missing": [],
            "local_gates_green": True,
            "ci_green": True,
            "origin_evidence": "PR #35",
        }
        res = score_phase_completion(evidence, jev_policy=policy, pack=self.pack)
        axes = res["sentiment"]["axes"]

        # Code authority (merge_evidence) was heuristic 1; live was 4 -> effective remains 1
        self.assertEqual(axes["merge_evidence"]["index"], 1)
        self.assertEqual(axes["merge_evidence"]["source"], "code")

        # Jev authority (status_honesty) was heuristic 4; live was 1 -> effective lowered to 1
        self.assertEqual(axes["status_honesty"]["index"], 1)
        self.assertEqual(axes["status_honesty"]["source"], "live")

    def test_out_of_pack_primary_gap_refused(self):
        answers = _build_live_answers(self.pack, primary_gap="merge_pending")
        # Override primary gap choice with out-of-pack value
        answers["primary_gap"]["choice"] = "invented_category"
        # To avoid JevEvaluator choice validation failing, patch evaluate result
        settings = load_settings({"jev_api_key": "test-key"})
        policy = policy_for(settings, transport=None, governor=_CountingGovernor(), ledger=self.ledger)

        fake_result = JevEvaluationResult(
            "pass", 0.9, 1.0, answers, [], cost=0.001, input_tokens=100, output_tokens=30,
            is_fallback=False, model="jev-test",
        )
        with patch.object(policy.evaluator, "evaluate", return_value=fake_result):
            state = {"phase": "JEV-P1"}
            _r, _s, judgment = policy.evaluate_phase_completion(state, self.pack)
            self.assertIsNone(judgment["primary_gap"])
            self.assertTrue(any("out-of-pack primary_gap refused" in ev for ev in judgment["evidence"]))

    def test_one_axis_invalid_yields_none_for_that_axis_only(self):
        answers = _build_live_answers(self.pack)
        # Corrupt one axis answer
        answers["dogfood"] = "invalid-type"
        settings = load_settings({"jev_api_key": "test-key"})
        policy = policy_for(settings, transport=None, governor=_CountingGovernor(), ledger=self.ledger)

        fake_result = JevEvaluationResult(
            "pass", 0.9, 1.0, answers, [], cost=0.001, input_tokens=100, output_tokens=30,
            is_fallback=False, model="jev-test",
        )
        with patch.object(policy.evaluator, "evaluate", return_value=fake_result):
            state = {"phase": "JEV-P1"}
            _r, _s, judgment = policy.evaluate_phase_completion(state, self.pack)
            self.assertFalse(judgment["is_fallback"])
            self.assertIsNone(judgment["live_levels"]["dogfood"])
            self.assertIsNotNone(judgment["live_levels"]["merge_evidence"])

    def test_all_axes_invalid_yields_whole_call_fallback(self):
        answers = {axis: "invalid" for axis in self.pack["axes"]}
        answers["primary_gap"] = "none"
        settings = load_settings({"jev_api_key": "test-key"})
        policy = policy_for(settings, transport=None, governor=_CountingGovernor(), ledger=self.ledger)

        fake_result = JevEvaluationResult(
            "pass", 0.9, 1.0, answers, [], cost=0.001, input_tokens=100, output_tokens=30,
            is_fallback=False, model="jev-test",
        )
        with patch.object(policy.evaluator, "evaluate", return_value=fake_result):
            state = {"phase": "JEV-P1"}
            _r, _s, judgment = policy.evaluate_phase_completion(state, self.pack)
            self.assertTrue(judgment["is_fallback"])

    def test_transport_harness_error_reconciles_reservation(self):
        gov = _CountingGovernor()
        settings = load_settings({"jev_api_key": "test-key"})
        policy = policy_for(settings, transport=None, governor=gov, ledger=self.ledger)

        with patch.object(policy.evaluator, "evaluate", side_effect=HarnessError("network down")):
            state = {"phase": "JEV-P1", "status_row": "draft not merged"}
            _r, _s, judgment = policy.evaluate_phase_completion(state, self.pack)
            self.assertTrue(judgment["is_fallback"])
            self.assertEqual(len(gov.reserved), 1)
            self.assertEqual(len(gov.reconciled), 1)
            self.assertEqual(gov.reconciled[0][1], 0.0)


class TestScoreAndBarPass(unittest.TestCase):
    def setUp(self):
        self.pack = copy.deepcopy(DEFAULT_COMPLETION_PACK)

    def test_blocking_axis_fails_bar_even_if_hard_gates_pass(self):
        # All hard gates pass, but merge_evidence is at index 0 (blocking)
        evidence = {
            "phase": "JEV-P1",
            "repo_root": ".",
            "pr_merged": True,
            "origin_evidence": "PR #35",
            "required_tests": ["t.py"],
            "tests_missing": [],
            "local_gates_green": True,
            "ci_green": True,
            "open_blockers": [],
            "status_row": "",  # no status row -> status_honesty index = 0 (blocking)
        }
        res = score_phase_completion(evidence, pack=self.pack)
        self.assertFalse(res["bar"]["pass"])
        self.assertFalse(res["can_mark_complete"])
        self.assertIn("status_honesty", res["bar"]["blocking_axes"])

    def test_failed_hard_gates_map_to_buckets_with_hard_gate_source(self):
        evidence = {
            "phase": "JEV-P1",
            "repo_root": ".",
            "pr_merged": False,
            "origin_evidence": None,
            "required_tests": ["t.py"],
            "tests_missing": ["t.py"],
            "local_gates_green": False,
            "ci_green": False,
            "open_blockers": ["repair needed"],
            "status_row": "| P1 | **complete** | blocked |",
        }
        res = score_phase_completion(evidence, pack=self.pack)
        self.assertFalse(res["bar"]["pass"])
        buckets = {imp["bucket"]: imp for imp in res["improvements"]}
        self.assertIn("merge_pending", buckets)
        self.assertEqual(buckets["merge_pending"]["source"], "hard_gate")
        self.assertIn("tests_missing", buckets)
        self.assertEqual(buckets["tests_missing"]["source"], "hard_gate")
        self.assertIn("gates_unverified", buckets)
        self.assertEqual(buckets["gates_unverified"]["source"], "hard_gate")
        self.assertIn("status_dishonest", buckets)

    def test_jev_p6_complete_this_pr_fails_bar_with_merge_pending(self):
        evidence = {
            "phase": "JEV-P6",
            "repo_root": ".",
            "pr_merged": False,
            "origin_evidence": None,
            "required_tests": ["tests/test_repo_items.py"],
            "tests_missing": [],
            "local_gates_green": True,
            "ci_green": True,
            "open_blockers": [],
            "status_row": "| JEV-P6 | **complete — this PR** | in review |",
        }
        res = score_phase_completion(evidence, pack=self.pack)
        self.assertFalse(res["bar"]["pass"])
        self.assertTrue(any(imp["bucket"] == "merge_pending" for imp in res["improvements"]))

    def test_p4_residual_work_improvement_present(self):
        evidence = {
            "phase": "JEV-P4",
            "repo_root": ".",
            "pr_merged": True,
            "origin_evidence": "PR #46",
            "required_tests": ["tests/test_jev_p4_ops_exit.py"],
            "tests_missing": [],
            "local_gates_green": True,
            "ci_green": True,
            "open_blockers": [],
            "status_row": "| JEV-P4 | **complete** | PR #46 MERGED a021cfc; residual: dogfood A/B |",
            "user_facing": True,
        }
        res = score_phase_completion(evidence, pack=self.pack)
        self.assertTrue(any(imp["bucket"] == "residual_untracked" for imp in res["improvements"]))

    def test_load_evidence_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "ev.json"
            p.write_text(json.dumps({"phase": "P1", "status_row": "ok"}), encoding="utf-8")
            data = load_evidence_file(str(p))
            self.assertEqual(data["phase"], "P1")

            with self.assertRaises(HarnessError):
                load_evidence_file(str(Path(tmp) / "nonexistent.json"))

            bad_p = Path(tmp) / "bad.json"
            bad_p.write_text('["not a dict"]', encoding="utf-8")
            with self.assertRaises(HarnessError):
                load_evidence_file(str(bad_p))

            invalid_p = Path(tmp) / "invalid.json"
            invalid_p.write_text("{not valid json", encoding="utf-8")
            with self.assertRaises(HarnessError):
                load_evidence_file(str(invalid_p))


class TestCliJevPhase(unittest.TestCase):
    def setUp(self):
        self.parser = build_parser()

    def test_cli_parser_flags(self):
        opts = self.parser.parse_args(["jev-phase", "--all", "--local-only", "--json"])
        self.assertTrue(opts.all)
        self.assertTrue(opts.local_only)
        self.assertTrue(opts.json)

        opts = self.parser.parse_args(["jev-phase", "--phase", "JEV-P1", "--min-score", "90"])
        self.assertEqual(opts.phase, "JEV-P1")
        self.assertEqual(opts.min_score, 90.0)

    def test_cli_requires_phase_or_all(self):
        opts = self.parser.parse_args(["jev-phase"])
        with self.assertRaises(HarnessError) as ctx:
            _cmd_jev_phase(opts, settings=None)
        self.assertIn("requires --phase or --all", str(ctx.exception))

    def test_cli_single_phase_text_and_json(self):
        repo_root = Path(__file__).resolve().parents[1]
        opts = self.parser.parse_args([
            "jev-phase", "--phase", "JEV-P1", "--repo-root", str(repo_root), "--local-only"
        ])
        buf = io.StringIO()
        with redirect_stdout(buf):
            _cmd_jev_phase(opts, settings=None)
        output = buf.getvalue()
        self.assertIn("phase=JEV-P1", output)
        self.assertIn("hard_gates:", output)
        self.assertIn("sentiment:", output)

        opts_json = self.parser.parse_args([
            "jev-phase", "--phase", "JEV-P1", "--repo-root", str(repo_root), "--local-only", "--json"
        ])
        buf_json = io.StringIO()
        with redirect_stdout(buf_json):
            _cmd_jev_phase(opts_json, settings=None)
        parsed = json.loads(buf_json.getvalue())
        self.assertEqual(parsed["phase"], "JEV-P1")
        self.assertTrue(parsed["can_mark_complete"])

    def test_cli_all_phases_on_real_repo(self):
        repo_root = Path(__file__).resolve().parents[1]
        opts = self.parser.parse_args([
            "jev-phase", "--all", "--repo-root", str(repo_root), "--local-only", "--json"
        ])
        buf = io.StringIO()
        with redirect_stdout(buf):
            _cmd_jev_phase(opts, settings=None)
        board = json.loads(buf.getvalue())
        self.assertIn("phases", board)
        self.assertIn("passing", board)
        self.assertIn("failing", board)
        self.assertIn("false_complete", board)
        # Should not raise HarnessError unless false_complete is non-empty
        self.assertEqual(board["false_complete"], [])

    def test_cli_all_stays_local_even_when_settings_are_available(self):
        opts = self.parser.parse_args(["jev-phase", "--all", "--json"])
        board = {"phases": {}, "passing": [], "failing": [], "false_complete": []}
        with patch("harness.cli.score_all_phases", return_value=board) as score, \
             patch("harness.cli.policy_for", side_effect=AssertionError("provider path")):
            with redirect_stdout(io.StringIO()):
                _cmd_jev_phase(opts, settings=SimpleNamespace(max_cost=1.0))
        self.assertIsNone(score.call_args.kwargs["jev_policy"])

    def test_single_phase_composes_bounded_governor_and_ledger(self):
        opts = self.parser.parse_args(["jev-phase", "--phase", "JEV-P1", "--json"])
        settings = SimpleNamespace(max_cost=0.02)
        governor, ledger = object(), object()
        result = {"phase": "JEV-P1", "score": 0, "min_score": 85,
                  "can_mark_complete": True}
        with patch("harness.cli.jev_face_governor", return_value=governor) as make_gov, \
             patch("harness.cli._ledger", return_value=ledger) as make_ledger, \
             patch("harness.cli.dogfood_phase", return_value=result) as dogfood:
            with redirect_stdout(io.StringIO()):
                _cmd_jev_phase(opts, settings=settings)
        make_gov.assert_called_once_with(settings, 0.02)
        make_ledger.assert_called_once_with(settings)
        kwargs = dogfood.call_args.kwargs
        self.assertIs(kwargs["governor"], governor)
        self.assertIs(kwargs["ledger"], ledger)
        self.assertIsNotNone(kwargs["transport"])

    def test_single_phase_caps_high_configured_cost_at_jev_ceiling(self):
        opts = self.parser.parse_args(["jev-phase", "--phase", "JEV-P1", "--json"])
        settings = SimpleNamespace(max_cost=0.10)
        result = {"phase": "JEV-P1", "score": 0, "min_score": 85,
                  "can_mark_complete": True}
        with patch("harness.cli.jev_face_governor") as make_gov, \
             patch("harness.cli._ledger", return_value=object()), \
             patch("harness.cli.dogfood_phase", return_value=result):
            with redirect_stdout(io.StringIO()):
                _cmd_jev_phase(opts, settings=settings)
        make_gov.assert_called_once_with(settings, 0.05)

    def test_single_phase_local_only_does_not_compose_provider(self):
        opts = self.parser.parse_args(["jev-phase", "--phase", "JEV-P1", "--local-only", "--json"])
        result = {"phase": "JEV-P1", "score": 0, "min_score": 85,
                  "can_mark_complete": True}
        with patch("harness.cli.jev_face_governor", side_effect=AssertionError("provider path")), \
             patch("harness.cli._ledger", side_effect=AssertionError("ledger path")), \
             patch("harness.cli.dogfood_phase", return_value=result) as dogfood:
            with redirect_stdout(io.StringIO()):
                _cmd_jev_phase(opts, settings=SimpleNamespace(max_cost=2.0))
        kwargs = dogfood.call_args.kwargs
        self.assertIsNone(kwargs["settings"])
        self.assertIsNone(kwargs["governor"])
        self.assertIsNone(kwargs["ledger"])

    def test_cli_all_phases_raises_on_false_complete(self):
        fake_board = {
            "phases": {
                "JEV-BAD": {
                    "score": 40.0,
                    "bar": {"pass": False},
                    "improvements": [{"bucket": "status_dishonest", "suggested_next_action": "fix"}],
                }
            },
            "passing": [],
            "failing": ["JEV-BAD"],
            "false_complete": ["JEV-BAD"],
        }
        opts = self.parser.parse_args(["jev-phase", "--all", "--local-only"])
        with patch("harness.cli.score_all_phases", return_value=fake_board):
            buf = io.StringIO()
            with redirect_stdout(buf):
                with self.assertRaises(HarnessError) as ctx:
                    _cmd_jev_phase(opts, settings=None)
            self.assertIn("phases claim complete but fail the JEV bar", str(ctx.exception))
            self.assertIn("false_complete: JEV-BAD", buf.getvalue())

    def test_write_jev_phase_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_file = os.path.join(tmp, "sub", "result.json")
            opts = self.parser.parse_args(["jev-phase", "--all", "--out", out_file])
            _write_jev_phase_out(opts, {"test": True})
            self.assertTrue(os.path.isfile(out_file))
            with open(out_file, encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertTrue(data.get("test"))


if __name__ == "__main__":
    unittest.main()
