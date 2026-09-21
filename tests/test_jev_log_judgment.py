"""JEV-LOG judgment gate tests (tests/test_jev_log_judgment.py).

``JevPolicy.evaluate_log_item``: keyed live choice+score, unkeyed keyword
fallback, 0-hallucination (bucket ∈ pack keys, score level ∈ pack levels),
one ledger ``jev_eval`` per call, site=log_factor. Hermetic doubles only.
"""
import os
import tempfile
import unittest

from harness.config import load_settings
from harness.jev import JevEvaluationResult
from harness.jev_policy import JevPolicy, policy_for
from harness.ledger import AutonomyLedger


def sample_log_pack():
    """Operator log pack fixture (kept local so hermetic runs stay
    import-mode safe; the schema test owns the canonical shape rules)."""
    return {
        "id": "scmessenger-ops-log-v1",
        "buckets": {
            "transport": {
                "label": "Transport / swarm health",
                "kind": "trouble_area",
                "path_id": "log/transport",
                "keywords": ["dial", "swarm", "listener", "negotiation",
                             "yamux", "disconnected"],
                "suggested_next_action": "review dial/relay policy",
                "attention": "high",
            },
            "delivery": {
                "label": "Outbox / inbox delivery",
                "kind": "trouble_area",
                "path_id": "log/delivery",
                "keywords": ["outbox", "inbox", "delivered", "history"],
                "suggested_next_action": "trace message lifecycle",
                "attention": "medium",
            },
            "ble": {
                "label": "Bluetooth adapter",
                "kind": "trouble_area",
                "path_id": "log/ble",
                "keywords": ["ble", "bluetooth", "btleplug", "gatt"],
                "suggested_next_action": None,
                "attention": "low",
            },
        },
        "score": {
            "id": "sentiment",
            "instructions": ("Rate operational severity/attention for this "
                             "log item from the stated levels only."),
            "levels": [
                "benign — routine info, no operator attention",
                "elevated — degraded but bounded behavior",
                "actionable — likely defect or policy issue",
                "critical — security, data loss, or hard outage signal",
            ],
        },
    }


class _JevTransport:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, key, payload, timeout=45):
        self.calls.append(payload)
        return 200, self.response


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


class EvaluateLogItemTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = AutonomyLedger(
            os.path.join(self.tmp.name, "ledger.jsonl"))
        self.pack = sample_log_pack()
        self.levels = self.pack["score"]["levels"]

    def _keyed_transport(self, choice="ble", probs=None, score_probs=None,
                         score_conf=0.86, usage=(40, 2)):
        # Official score answer shape: probabilities keyed by the legend's
        # anchor strings; legend maps anchors -> the operator level strings.
        score_probs = score_probs or {"0.0": 0.05, "0.5": 0.62, "1.0": 0.33}
        legend = {"0.0": self.levels[0], "0.5": self.levels[1],
                  "1.0": self.levels[2]}
        return _JevTransport({
            "model": "jev-test",
            "answers": {
                "bucket": {
                    "type": "choice",
                    "choice": choice,
                    "probabilities": probs or {"ble": 0.7, "transport": 0.2,
                                               "delivery": 0.1},
                    "confidence": 0.88,
                },
                "sentiment": {
                    "type": "score",
                    "score": 0.5,
                    "legend": legend,
                    "probabilities": score_probs,
                    "confidence": score_conf,
                },
            },
            "usage": {"input_tokens": usage[0], "output_tokens": usage[1]},
        })

    def test_keyed_live_judgment_uses_pack_fields_and_one_jev_eval(self):
        transport = self._keyed_transport()
        gov = _CountingGovernor()
        settings = load_settings({"jev_api_key": "jev-key"})
        policy = policy_for(settings, transport=transport, governor=gov,
                            ledger=self.ledger)
        result, structural, judgment = policy.evaluate_log_item(
            {"item": "BLE GATT central unavailable (no adapter)"},
            self.pack)
        self.assertFalse(result.is_fallback)
        self.assertFalse(judgment["is_fallback"])
        self.assertEqual(judgment["bucket"], "ble")
        self.assertEqual(judgment["path_id"], "log/ble")
        self.assertEqual(judgment["kind"], "trouble_area")
        self.assertIsNone(judgment["suggested_next_action"])  # pack says None
        self.assertEqual(judgment["score"]["id"], "sentiment")
        self.assertEqual(judgment["score"]["level"], self.levels[1])
        self.assertAlmostEqual(judgment["score"]["value"], 0.62)
        self.assertEqual(judgment["score"]["confidence"], 0.86)
        self.assertEqual(structural["site"], "log_factor")
        self.assertEqual(judgment["structural"]["site"], "log_factor")
        self.assertEqual(len(transport.calls), 1)
        questions = transport.calls[0]["questions"]
        self.assertEqual(set(questions["bucket"]["criteria"]),
                         {"transport", "delivery", "ble"})
        self.assertEqual(questions["sentiment"]["criteria"], self.levels)
        events = [e for e in self.ledger.entries() if e["event"] == "jev_eval"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["site"], "log_factor")
        self.assertFalse(events[0]["is_fallback"])

    def test_score_level_is_declared_string_with_highest_probability(self):
        transport = self._keyed_transport(score_probs={
            "0.0": 0.10, "0.5": 0.35, "1.0": 0.55})
        settings = load_settings({"jev_api_key": "jev-key"})
        policy = policy_for(settings, transport=transport, governor=_CountingGovernor(),
                            ledger=self.ledger)
        _r, _s, judgment = policy.evaluate_log_item(
            {"item": "custody sweep expired 0 of 198"}, self.pack)
        self.assertEqual(judgment["score"]["level"], self.levels[2])

    def test_out_of_pack_choice_refused_and_score_never_invented(self):
        class _OutOfPackEvaluator:
            api_key = "jev-key"
            model = "jev-test"

            def evaluate(self, state, questions=None):
                return JevEvaluationResult(
                    "pass", 0.9, 0.9,
                    {"bucket": {"type": "choice", "choice": "invented",
                                "probabilities": {"invented": 1.0},
                                "confidence": 0.9},
                     "sentiment": {"type": "score", "score": 0.9,
                                   "legend": {"0.0": "x", "1.0": "y"},
                                   "probabilities": {"invented-level": 1.0},
                                   "confidence": 0.9}},
                    ["bucket (choice): invented"],
                    is_fallback=False, model=self.model)

        policy = JevPolicy(
            load_settings({"jev_api_key": "jev-key"}),
            evaluator=_OutOfPackEvaluator(), governor=_CountingGovernor(),
            ledger=self.ledger)
        result, _s, judgment = policy.evaluate_log_item(
            "text with no pack keywords", self.pack)
        self.assertNotEqual(judgment["bucket"], "invented")
        self.assertIsNone(judgment["bucket"])
        self.assertIsNone(judgment["path_id"])
        self.assertIsNone(judgment["score"]["level"])
        self.assertIsNone(judgment["score"]["value"])
        self.assertTrue(judgment["is_fallback"])
        self.assertTrue(any("out-of-pack choice refused" in r
                            for r in result.reasons))
        self.assertEqual(len([e for e in self.ledger.entries()
                              if e["event"] == "jev_eval"]), 1)

    def test_unkeyed_keyword_fallback_bucket_from_pack_only(self):
        policy = policy_for(_unkeyed_settings(), ledger=self.ledger)
        result, structural, judgment = policy.evaluate_log_item(
            {"item": "swarm disconnected from peer; yamux dial failed"},
            self.pack)
        self.assertTrue(result.is_fallback)
        self.assertTrue(judgment["is_fallback"])
        self.assertTrue(structural["is_fallback"])
        self.assertEqual(judgment["bucket"], "transport")
        self.assertEqual(judgment["path_id"], "log/transport")
        self.assertEqual(judgment["suggested_next_action"],
                         "review dial/relay policy")
        self.assertEqual(judgment["score"]["level"], None)
        self.assertEqual(structural["site"], "log_factor")
        self.assertEqual(len([e for e in self.ledger.entries()
                              if e["event"] == "jev_eval"]), 1)

    def test_unmatched_keyword_yields_none_bucket(self):
        policy = policy_for(_unkeyed_settings(), ledger=self.ledger)
        result, _s, judgment = policy.evaluate_log_item(
            "totally unrelated fluff about weather", self.pack)
        self.assertTrue(judgment["is_fallback"])
        self.assertIsNone(judgment["bucket"])
        self.assertIsNone(judgment["path_id"])
        self.assertIsNone(judgment["score"]["level"])

    def test_invalid_pack_refuses_without_inventing(self):
        policy = policy_for(_unkeyed_settings(), ledger=self.ledger)
        result, structural, judgment = policy.evaluate_log_item(
            "auth token", {"id": "", "buckets": {}})
        self.assertEqual(result.verdict, "fail")
        self.assertTrue(judgment["is_fallback"])
        self.assertIsNone(judgment["bucket"])
        self.assertEqual(structural["site"], "log_factor")

    def test_no_second_jev_client(self):
        import inspect
        import harness.jev_packs as jev_packs
        self.assertNotIn("JevEvaluator(",
                         inspect.getsource(jev_packs))
        self.assertNotIn("JevEvaluator(",
                         inspect.getsource(JevPolicy.evaluate_log_item))
        evaluator = _OutOfPackSentinel()
        policy = JevPolicy(_unkeyed_settings(), evaluator=evaluator)
        _r, _s, judgment = policy.evaluate_log_item(
            "swarm yamux dial", self.pack)
        self.assertEqual(judgment["bucket"], "transport")
        self.assertEqual(evaluator.calls, 0)  # unkeyed path never posts

    def test_keyed_transport_error_falls_back_to_keywords(self):
        class _BoomTransport:
            def post(self, url, key, payload, timeout=45):
                raise OSError("network down")

        policy = policy_for(load_settings({"jev_api_key": "jev-key"}),
                            transport=_BoomTransport(),
                            governor=_CountingGovernor(), ledger=self.ledger)
        result, structural, judgment = policy.evaluate_log_item(
            {"item": "gatt adapter probe failed"}, self.pack)
        self.assertTrue(judgment["is_fallback"])
        self.assertEqual(judgment["bucket"], "ble")
        self.assertEqual(structural["site"], "log_factor")
        self.assertEqual(len([e for e in self.ledger.entries()
                              if e["event"] == "jev_eval"]), 1)


class _OutOfPackSentinel:
    api_key = None
    model = "jev-test"
    calls = 0

    def evaluate(self, state, questions=None):
        _OutOfPackSentinel.calls += 1
        raise AssertionError("unkeyed path must not evaluate live")


if __name__ == "__main__":
    unittest.main()
