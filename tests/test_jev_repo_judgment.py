"""JEV-P6 judgment gate tests (tests/test_jev_repo_judgment.py).

``JevPolicy.evaluate_repo_summary``: keyed live axes/attention/nouls,
0-hallucination (declared ids only, else None), unkeyed keyword fallback,
shape-invalid responses never presented as live, ONE ledger ``jev_eval``
per call at site=repo_summary, reserve/settle accounting. Hermetic doubles.
"""
import os
import tempfile
import unittest

from harness.config import load_settings
from harness.errors import HarnessError
from harness.jev import JevEvaluationResult, jev_cost
from harness.jev_policy import JevPolicy, policy_for
from harness.ledger import AutonomyLedger


def repo_pack():
    """Operator pack fixture (local, so hermetic import modes stay safe)."""
    return {
        "id": "fixture-repo-v1",
        "axes": {
            "stage": {
                "instructions": "Choose the hourglass stage.",
                "criteria": {
                    "prep": "inventory and planning inputs",
                    "waist": "frontier confirmation",
                    "adjudicate": "verification and evidence",
                },
            },
            "handling": {
                "instructions": "Choose the least capable handling tier.",
                "criteria": {
                    "code_owned": "no model needed",
                    "scout": "bounded mechanical work",
                    "frontier": "architectural attention",
                },
            },
        },
        "score": {
            "id": "attention",
            "instructions": "Rate centrality to the hourglass.",
            "levels": ["background", "notable", "central"],
        },
        "nouls": {
            "waist_relevant": {
                "instructions": "Must the waist brief cite this element?",
                "true": "Citation needed.",
                "false": "Not needed.",
            },
        },
        "keywords": {
            "stage": {
                "adjudicate": ["verify", "ledger", "test"],
                "waist": ["waist", "verdict"],
            },
            "handling": {"frontier": ["architect"]},
        },
    }


class _JevTransport:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, key, payload, timeout=45):
        self.calls.append(payload)
        return 200, self.response


class _BrokenGovernor:
    """Reserve refuses like a spent SpendGovernor (budget exhaustion)."""

    max_cost = 0.0
    spent = 0.0

    def reserve(self, amount, label):
        raise HarnessError(
            f"reservation ${amount:.6f} ({label}) would put spent+outstanding "
            "over ceiling $0.000000; refusing.")

    def reconcile(self, token, actual):
        raise AssertionError("no reservation to reconcile")

    def record_actual(self, cost, model):
        raise AssertionError("fallback must not bill actuals")


class _CountingGovernor:
    def __init__(self):
        self.reserved = []
        self.reconciled = []
        self.actual = []
        self.max_cost = 1.0
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


def _unkeyed_settings():
    settings = load_settings()
    settings.jev_api_key = None
    return settings


def _live_response(pack, usage=(700, 60), stage="adjudicate"):
    pack_doc = pack
    axes = {}
    for axis, spec in pack_doc["axes"].items():
        criteria = list(spec["criteria"])
        # The stage axis honors ``stage`` verbatim so an out-of-vocabulary
        # value can be sent on purpose (the parser must refuse it).
        chosen = stage if axis == "stage" else criteria[0]
        dominant = chosen if chosen in criteria else criteria[0]
        probabilities = {
            c: (0.8 if c == dominant else 0.2 / max(1, len(criteria) - 1))
            for c in criteria}
        total = sum(probabilities.values())
        probabilities = {k: round(v / total, 6) for k, v in probabilities.items()}
        drift = round(1.0 - sum(probabilities.values()), 6)
        first = next(iter(probabilities))
        probabilities[first] = round(probabilities[first] + drift, 6)
        axes[axis] = {
            "type": "choice", "choice": chosen,
            "probabilities": probabilities, "confidence": 0.9}
    levels = pack_doc["score"]["levels"]
    anchors = [str(i) for i in range(len(levels))]
    legend = {anchor: levels[i] for i, anchor in enumerate(anchors)}
    score_probs = {anchors[0]: 0.05, anchors[1]: 0.15, anchors[2]: 0.80}
    answers = dict(axes)
    answers[pack_doc["score"]["id"]] = {
        "type": "score", "score": 1.75, "legend": legend,
        "probabilities": score_probs, "confidence": 0.87}
    for name in pack_doc["nouls"]:
        answers[name] = {"type": "noul", "noul": 0.8}
    return {"model": "jev-test", "answers": answers,
            "usage": {"input_tokens": usage[0], "output_tokens": usage[1]}}


STATE = {"path": "harness/ledger.py", "kind": "python", "loc": 400,
         "summary": "hash-chained autonomy ledger and verify gates",
         "symbols": ["append(entry)", "verify_chain()"],
         "imports": ["json", "hashlib"], "headings": [], "test": True,
         "gate": "python -m unittest tests/test_ledger.py"}


class KeyedLiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = AutonomyLedger(os.path.join(self.tmp.name, "ledger.jsonl"))
        self.pack = repo_pack()

    def test_live_axes_attention_nouls_and_one_ledger_event(self):
        transport = _JevTransport(_live_response(self.pack))
        gov = _CountingGovernor()
        settings = load_settings({"jev_api_key": "jev-key"})
        policy = policy_for(settings, transport=transport, governor=gov,
                            ledger=self.ledger)
        result, structural, judgment = policy.evaluate_repo_summary(
            STATE, self.pack)
        self.assertFalse(result.is_fallback)
        self.assertFalse(judgment["is_fallback"])
        self.assertEqual(judgment["pack_id"], "fixture-repo-v1")
        self.assertEqual(judgment["axes"]["stage"], "adjudicate")
        self.assertIn(judgment["axes"]["handling"],
                      set(self.pack["axes"]["handling"]["criteria"]))
        self.assertEqual(judgment["attention"]["level"], "central")
        self.assertEqual(judgment["attention"]["value"], 0.80)
        self.assertEqual(judgment["attention"]["confidence"], 0.87)
        self.assertEqual(judgment["nouls"]["waist_relevant"], 0.8)
        self.assertEqual(structural["site"], "repo_summary")
        self.assertEqual(structural["input_tokens"], 700)
        self.assertEqual(structural["output_tokens"], 60)
        # accounting: one reserve, one settle at the honest actual
        self.assertEqual(len(gov.reserved), 1)
        self.assertEqual(len(gov.reconciled), 1)
        self.assertAlmostEqual(gov.reconciled[0][1], result.cost)
        # exactly one jev_eval per call, read from the temp ledger directly
        import json
        with open(os.path.join(self.tmp.name, "ledger.jsonl"), encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        jev_rows = [r for r in rows if r.get("event") == "jev_eval"]
        self.assertEqual(len(jev_rows), 1)
        self.assertEqual(jev_rows[0]["site"], "repo_summary")
        self.assertEqual(jev_rows[0]["input_tokens"], 700)
        self.assertEqual(jev_rows[0]["output_tokens"], 60)

    def test_shape_invalid_response_falls_back_never_lies(self):
        bad = _live_response(self.pack)
        bad["answers"][self.pack["score"]["id"]]["probabilities"] = {
            "0": 0.9, "1": 0.9, "2": 0.9}  # does not sum to 1
        settings = load_settings({"jev_api_key": "jev-key"})
        policy = policy_for(settings, transport=_JevTransport(bad),
                            governor=_CountingGovernor(), ledger=self.ledger)
        result, structural, judgment = policy.evaluate_repo_summary(
            STATE, self.pack)
        self.assertTrue(judgment["is_fallback"])
        self.assertEqual(judgment["attention"]["level"], None)
        # the provider billed input tokens for the malformed response; the
        # envelope reports that honestly instead of zeroing it
        self.assertAlmostEqual(structural["cost"], jev_cost(700))
        self.assertEqual(structural["input_tokens"], 700)
        self.assertIn("invalid TypeSafe response", " ".join(judgment["evidence"])
                      + " " + " ".join(result.reasons))

    def test_out_of_vocabulary_choice_is_reported_not_invented(self):
        resp = _live_response(self.pack, stage="invented-stage")
        settings = load_settings({"jev_api_key": "jev-key"})
        policy = policy_for(settings, transport=_JevTransport(resp),
                            governor=_CountingGovernor(), ledger=self.ledger)
        _result, _structural, judgment = policy.evaluate_repo_summary(
            STATE, self.pack)
        # evaluator refuses choices outside criteria -> whole response falls
        # back to declared-keyword heuristics; the invented value never lands
        self.assertTrue(judgment["is_fallback"])
        stage = judgment["axes"].get("stage")
        self.assertNotEqual(stage, "invented-stage")
        self.assertIn(stage,
                      set(self.pack["axes"]["stage"]["criteria"]) | {None})

    def test_budget_refusal_is_honest_fallback_with_zero_cost(self):
        settings = load_settings({"jev_api_key": "jev-key"})
        policy = JevPolicy(settings, transport=_JevTransport({}),
                           governor=_BrokenGovernor(), ledger=self.ledger)
        result, structural, judgment = policy.evaluate_repo_summary(
            STATE, self.pack)
        self.assertTrue(judgment["is_fallback"])
        self.assertEqual(structural["cost"], 0.0)
        evidence = " ".join(judgment["evidence"])
        self.assertIn("over ceiling", evidence)


class UnkeyedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = AutonomyLedger(os.path.join(self.tmp.name, "ledger.jsonl"))
        self.pack = repo_pack()

    def test_unkeyed_keyword_fallback_still_ledgers(self):
        settings = _unkeyed_settings()
        policy = policy_for(settings, transport=None, governor=None,
                            ledger=self.ledger)
        result, structural, judgment = policy.evaluate_repo_summary(
            STATE, self.pack)
        self.assertTrue(result.is_fallback)
        self.assertTrue(judgment["is_fallback"])
        self.assertIsNone(judgment["attention"]["level"])
        self.assertEqual(judgment["nouls"], {})
        self.assertIn(structural["site"], "repo_summary")
        import json
        with open(os.path.join(self.tmp.name, "ledger.jsonl"), encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event"], "jev_eval")
        self.assertTrue(rows[0]["is_fallback"])
        self.assertEqual(rows[0]["cost"], 0.0)

    def test_invalid_pack_refuses_with_ledger_event(self):
        settings = _unkeyed_settings()
        policy = policy_for(settings, transport=None, governor=None,
                            ledger=self.ledger)
        result, structural, judgment = policy.evaluate_repo_summary(STATE, {"id": "x"})
        self.assertTrue(result.is_fallback)
        self.assertEqual(judgment["axes"], {})
        import json
        with open(os.path.join(self.tmp.name, "ledger.jsonl"), encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual(len(rows), 1)


class PreflightRequirementTests(unittest.TestCase):
    def test_keyed_without_governor_refuses_fail_closed(self):
        settings = load_settings({"jev_api_key": "jev-key"})
        policy = policy_for(settings, transport=_JevTransport({}),
                            governor=None, ledger=None)
        # _preflight raises -> fallback path, never a silent live dispatch
        result, _structural, judgment = policy.evaluate_repo_summary(
            STATE, repo_pack())
        self.assertTrue(result.is_fallback)
        self.assertTrue(judgment["is_fallback"])


class _FakeEvaluator:
    """Bypasses transport parsing: returns a crafted JevEvaluationResult so
    the policy's own defensive shape handling is exercised directly."""

    api_key = "jev-key"
    model = "jev-fake"
    min_confidence = 0.70

    def __init__(self, answers):
        self.answers = answers

    def evaluate(self, state, questions):
        return JevEvaluationResult(
            "pass", 0.9, 0.9, dict(self.answers), ["fake evaluator"],
            cost=0.004, input_tokens=100, output_tokens=10,
            model="jev-fake")


class EvaluatorShapeTests(unittest.TestCase):
    """Defensive parse branches: partial answers, undeclared legends, str/None
    state, and the all-unmatched final fallback."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = AutonomyLedger(os.path.join(self.tmp.name, "ledger.jsonl"))
        self.pack = repo_pack()
        self.settings = load_settings({"jev_api_key": "jev-key"})

    def _policy(self, answers):
        return JevPolicy(
            self.settings, transport=None, governor=_CountingGovernor(),
            ledger=self.ledger, evaluator=_FakeEvaluator(answers))

    def test_partial_answers_stay_live_and_report_unmatched(self):
        policy = self._policy({
            "stage": {"type": "choice", "choice": "adjudicate",
                      "probabilities": {"adjudicate": 1.0},
                      "confidence": 0.9},
            # handling / attention / waist_relevant all missing
        })
        result, structural, judgment = policy.evaluate_repo_summary(STATE, self.pack)
        self.assertFalse(result.is_fallback)
        self.assertEqual(judgment["axes"]["stage"], "adjudicate")
        self.assertIsNone(judgment["axes"]["handling"])  # unmatched branch
        self.assertEqual(judgment["attention"]["level"], None)
        evidence = " ".join(judgment["evidence"])
        self.assertIn("handling:unmatched", evidence)
        self.assertIn("attention:unmatched", evidence)
        self.assertIn("waist_relevant:unmatched", evidence)
        self.assertIsNone(judgment["nouls"]["waist_relevant"])

    def test_undeclared_legend_labels_never_become_a_level(self):
        policy = self._policy({
            "stage": {"type": "choice", "choice": 123,  # not a string
                      "probabilities": {}, "confidence": 0.5},
            "handling": {"type": "choice", "choice": None,
                         "probabilities": {}, "confidence": 0.5},
            "attention": {"type": "score", "score": 1.0,
                          "legend": {"0": "invented-a",
                                     "1": "invented-b"},
                          "probabilities": {"0": 0.5, "1": 0.5},
                          "confidence": 0.6},
            "waist_relevant": {"type": "noul", "noul": 0.9},
        })
        result, structural, judgment = policy.evaluate_repo_summary(STATE, self.pack)
        # no live axis and no declared level -> final fallback: the honest
        # keyword match fills DECLARED axis ids only (stage hits "adjudicate"
        # keywords in state); is_fallback stays True and the invented score
        # labels never become a level.
        self.assertTrue(judgment["is_fallback"])
        self.assertEqual(judgment["axes"],
                         {"stage": "adjudicate", "handling": None})
        self.assertEqual(judgment["attention"]["level"], None)
        evidence = " ".join(judgment["evidence"])
        self.assertIn("attention:unmatched", evidence)
        # cost/tokens of the live call remain honest even on final fallback
        self.assertAlmostEqual(structural["cost"], 0.004)
        self.assertEqual(structural["input_tokens"], 100)

    def test_string_state_is_truncated_text(self):
        settings = _unkeyed_settings()
        policy = policy_for(settings, transport=None, governor=None,
                            ledger=self.ledger)
        result, _s, judgment = policy.evaluate_repo_summary(
            "ledger verify gates " * 100, self.pack)
        self.assertTrue(result.is_fallback)
        self.assertEqual(judgment["axes"].get("stage"), "adjudicate")

    def test_non_mapping_state_never_crashes(self):
        settings = _unkeyed_settings()
        policy = policy_for(settings, transport=None, governor=None,
                            ledger=self.ledger)
        result, _s, _j = policy.evaluate_repo_summary(None, self.pack)
        self.assertTrue(result.is_fallback)


if __name__ == "__main__":
    unittest.main()
