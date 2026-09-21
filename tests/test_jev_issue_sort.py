"""Hermetic JEV-P5 issue-sort contract tests (operator packs, 0 hallucination)."""
import inspect
import json
import os
import tempfile
import unittest
from unittest import mock

from harness.config import load_settings
from harness.jev import JevEvaluationResult
from harness.jev_packs import (issue_sort_question_pack, match_keywords,
                               sort_notes_into_buckets,
                               validate_operator_pack)
from harness.jev_policy import JevPolicy, policy_for
from harness.ledger import AutonomyLedger
from harness.waist import compose_plan


def sample_pack():
    return {
        "id": "ops-attention-v1",
        "buckets": {
            "auth": {
                "label": "Auth / token trouble",
                "kind": "trouble_area",
                "path_id": "path/auth",
                "keywords": ["auth", "token", "login"],
                "suggested_next_action": "open auth backlog",
                "attention": "high",
            },
            "perf": {
                "label": "Performance alternate path",
                "kind": "alternate_path",
                "path_id": "path/perf",
                "keywords": ["latency", "slow", "timeout"],
                "suggested_next_action": "profile hot path",
                "attention": "medium",
            },
            "driver": {
                "label": "Orchestration driver",
                "kind": "orchestration_driver",
                "path_id": "path/driver",
                "keywords": ["orchestr", "deferral", "handoff"],
                "suggested_next_action": None,
                "attention": "low",
            },
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


class _CountingEvaluator:
    api_key = None
    model = "jev-test"

    def __init__(self, answers=None, keyed=False):
        self.api_key = "jev-key" if keyed else None
        self.calls = 0
        self.answers = answers or {}

    def evaluate(self, state, questions=None):
        self.calls += 1
        return JevEvaluationResult(
            "pass", 0.0, 1.0, dict(self.answers), ["fallback"],
            is_fallback=True, model=self.model)


def _unkeyed_settings():
    settings = load_settings()
    settings.jev_api_key = None
    return settings


def _ledger(tmpdir):
    return AutonomyLedger(os.path.join(tmpdir, "ledger.jsonl"))


def _jev_evals(ledger):
    return [e for e in ledger.entries() if e["event"] == "jev_eval"]


class OperatorPackSchemaTests(unittest.TestCase):
    def test_validate_rejects_bad_kinds_and_missing_fields(self):
        with self.assertRaises(ValueError):
            validate_operator_pack({"id": "p", "buckets": {}})
        bad = sample_pack()
        bad["buckets"]["auth"]["kind"] = "invented_kind"
        with self.assertRaises(ValueError):
            validate_operator_pack(bad)
        missing = sample_pack()
        del missing["buckets"]["auth"]["path_id"]
        with self.assertRaises(ValueError):
            validate_operator_pack(missing)

    def test_question_pack_criteria_are_operator_labels_only(self):
        questions = issue_sort_question_pack(sample_pack())
        criteria = questions["bucket"]["criteria"]
        self.assertEqual(set(criteria), {"auth", "perf", "driver"})
        self.assertEqual(criteria["auth"], "Auth / token trouble")
        self.assertEqual(questions["bucket"]["type"], "choice")

    def test_match_keywords_only_uses_pack_keywords(self):
        pack = validate_operator_pack(sample_pack())
        bid, score, evidence = match_keywords("login token expired", pack)
        self.assertEqual(bid, "auth")
        self.assertGreaterEqual(score, 1)
        self.assertTrue(any(k in evidence for k in pack["buckets"]["auth"]["keywords"]))
        none_id, none_score, none_ev = match_keywords("unrelated fluff", pack)
        self.assertIsNone(none_id)
        self.assertEqual(none_score, 0)
        self.assertEqual(none_ev, [])


class EvaluateIssueSortTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = _ledger(self.tmp.name)
        self.pack = sample_pack()

    def test_keyed_valid_choice_uses_pack_path_action_and_one_jev_eval(self):
        transport = _JevTransport({
            "model": "jev-test",
            "answers": {
                "bucket": {
                    "type": "choice",
                    "choice": "auth",
                    "probabilities": {"auth": 0.7, "perf": 0.2, "driver": 0.1},
                    "confidence": 0.88,
                }
            },
            "usage": {"input_tokens": 40, "output_tokens": 2},
        })
        gov = _CountingGovernor()
        settings = load_settings({"jev_api_key": "jev-key"})
        policy = policy_for(settings, transport=transport, governor=gov,
                            ledger=self.ledger)
        result, structural, combo = policy.evaluate_issue_sort(
            {"issue": "auth token rejected on login"}, self.pack)
        self.assertFalse(result.is_fallback)
        self.assertFalse(combo["is_fallback"])
        self.assertEqual(combo["bucket"], "auth")
        self.assertEqual(combo["path_id"], "path/auth")
        self.assertEqual(combo["suggested_next_action"], "open auth backlog")
        self.assertEqual(combo["kind"], "trouble_area")
        self.assertEqual(combo["attention"], "high")
        self.assertEqual(combo["pack_id"], "ops-attention-v1")
        self.assertEqual(structural["site"], "issue_sort")
        self.assertEqual(combo["structural"]["site"], "issue_sort")
        self.assertEqual(len(transport.calls), 1)
        events = _jev_evals(self.ledger)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["site"], "issue_sort")
        self.assertFalse(events[0]["is_fallback"])
        # criteria sent to TypeSafe are operator bucket ids only
        questions = transport.calls[0]["questions"]
        self.assertEqual(set(questions["bucket"]["criteria"]),
                         {"auth", "perf", "driver"})

    def test_keyed_out_of_pack_refused_never_invents_bucket(self):
        """Even a hand-crafted answers dict with an out-of-pack choice must
        not become a successful sort with an invented bucket."""
        class _OutOfPackEvaluator:
            api_key = "jev-key"
            model = "jev-test"

            def evaluate(self, state, questions=None):
                return JevEvaluationResult(
                    "pass", 0.9, 0.9,
                    {"bucket": {"type": "choice", "choice": "invented",
                                "probabilities": {"invented": 1.0},
                                "confidence": 0.9}},
                    ["bucket (choice): invented"],
                    is_fallback=False, model=self.model)

        gov = _CountingGovernor()
        policy = JevPolicy(
            load_settings({"jev_api_key": "jev-key"}),
            evaluator=_OutOfPackEvaluator(), governor=gov, ledger=self.ledger)
        result, structural, combo = policy.evaluate_issue_sort(
            "unrelated text with no pack keywords", self.pack)
        self.assertNotEqual(combo["bucket"], "invented")
        self.assertIsNone(combo["bucket"])
        self.assertTrue(combo["is_fallback"])
        self.assertTrue(any("out-of-pack choice refused" in r
                            for r in result.reasons))
        self.assertEqual(structural["site"], "issue_sort")
        self.assertEqual(len(_jev_evals(self.ledger)), 1)
        # Transport-shaped out-of-pack also fails closed through _parse_answer
        transport = _JevTransport({
            "model": "jev-test",
            "answers": {
                "bucket": {
                    "type": "choice",
                    "choice": "invented",
                    "probabilities": {
                        "auth": 0.3, "perf": 0.3, "invented": 0.4},
                    "confidence": 0.9,
                }
            },
            "usage": {"input_tokens": 30, "output_tokens": 1},
        })
        gov2 = _CountingGovernor()
        policy2 = policy_for(
            load_settings({"jev_api_key": "jev-key"}),
            transport=transport, governor=gov2, ledger=self.ledger)
        _r2, _s2, combo2 = policy2.evaluate_issue_sort(
            "unrelated fluff", self.pack)
        self.assertNotEqual(combo2["bucket"], "invented")
        self.assertTrue(combo2["is_fallback"])
        self.assertNotIn("invented", self.pack["buckets"])

    def test_issue_sort_text_edge_shapes(self):
        policy = policy_for(_unkeyed_settings(), ledger=self.ledger)
        self.assertEqual(JevPolicy._issue_sort_text({"note": "hi"}), "hi")
        self.assertEqual(JevPolicy._issue_sort_text({"empty": 1}), "")
        self.assertEqual(JevPolicy._issue_sort_text(None), "")
        self.assertEqual(JevPolicy._issue_sort_text(42), "42")
        _r, _s, combo = policy.evaluate_issue_sort(None, self.pack)
        self.assertIsNone(combo["bucket"])
        self.assertTrue(combo["is_fallback"])

    def test_unkeyed_keyword_match_is_fallback_declared_bucket_only(self):
        policy = policy_for(_unkeyed_settings(), ledger=self.ledger)
        result, structural, combo = policy.evaluate_issue_sort(
            "login token expired on auth page", self.pack)
        self.assertTrue(result.is_fallback)
        self.assertTrue(combo["is_fallback"])
        self.assertTrue(structural["is_fallback"])
        self.assertEqual(combo["bucket"], "auth")
        self.assertEqual(combo["path_id"], "path/auth")
        self.assertEqual(combo["suggested_next_action"], "open auth backlog")
        self.assertEqual(combo["pack_id"], "ops-attention-v1")
        self.assertEqual(len(_jev_evals(self.ledger)), 1)

    def test_unmatched_keyword_yields_none_bucket(self):
        policy = policy_for(_unkeyed_settings(), ledger=self.ledger)
        result, structural, combo = policy.evaluate_issue_sort(
            "totally unrelated fluff about weather", self.pack)
        self.assertTrue(combo["is_fallback"])
        self.assertIsNone(combo["bucket"])
        self.assertIsNone(combo["path_id"])
        self.assertIsNone(combo["suggested_next_action"])
        self.assertIsNone(combo["kind"])
        self.assertEqual(structural["site"], "issue_sort")
        self.assertEqual(len(_jev_evals(self.ledger)), 1)

    def test_suggested_next_action_always_equals_pack_when_set(self):
        policy = policy_for(_unkeyed_settings(), ledger=self.ledger)
        cases = [
            ("auth token fail", "auth", "open auth backlog"),
            ("slow latency timeout", "perf", "profile hot path"),
            ("orchestrator deferral handoff note", "driver", None),
        ]
        for text, expected_bucket, expected_action in cases:
            _r, _s, combo = policy.evaluate_issue_sort(text, self.pack)
            self.assertEqual(combo["bucket"], expected_bucket)
            self.assertEqual(combo["suggested_next_action"], expected_action)
            if combo["bucket"] is not None:
                pack_action = self.pack["buckets"][combo["bucket"]].get(
                    "suggested_next_action")
                self.assertEqual(combo["suggested_next_action"], pack_action)

    def test_no_second_jev_client(self):
        import harness.jev_packs as jev_packs
        pack_src = inspect.getsource(jev_packs)
        policy_src = inspect.getsource(JevPolicy.evaluate_issue_sort)
        self.assertNotIn("JevEvaluator(", pack_src)
        self.assertNotIn("JevEvaluator(", policy_src)
        evaluator = _CountingEvaluator()
        policy = JevPolicy(_unkeyed_settings(), evaluator=evaluator)
        self.assertIs(policy.evaluator, evaluator)
        _r, _s, combo = policy.evaluate_issue_sort("auth token", self.pack)
        self.assertEqual(combo["bucket"], "auth")
        # unkeyed path never constructs a second client or posts live
        self.assertEqual(evaluator.calls, 0)

    def test_keyed_missing_governor_falls_back_honestly(self):
        settings = load_settings({"jev_api_key": "jev-key"})
        policy = policy_for(settings, ledger=self.ledger)
        result, structural, combo = policy.evaluate_issue_sort(
            "auth token fail", self.pack)
        self.assertTrue(combo["is_fallback"])
        self.assertEqual(combo["bucket"], "auth")
        self.assertEqual(structural["site"], "issue_sort")
        self.assertEqual(len(_jev_evals(self.ledger)), 1)

    def test_invalid_pack_refuses_without_inventing(self):
        policy = policy_for(_unkeyed_settings(), ledger=self.ledger)
        result, structural, combo = policy.evaluate_issue_sort(
            "auth token", {"id": "", "buckets": {}})
        self.assertEqual(result.verdict, "fail")
        self.assertTrue(combo["is_fallback"])
        self.assertIsNone(combo["bucket"])
        self.assertIsNone(combo["path_id"])

    def test_sort_notes_helper_uses_pack_path_id_only(self):
        policy = policy_for(_unkeyed_settings(), ledger=self.ledger)
        combos = sort_notes_into_buckets(
            ["auth token broken", "slow latency", "unrelated fluff"],
            self.pack, policy)
        self.assertEqual(len(combos), 3)
        self.assertEqual(combos[0]["bucket"], "auth")
        self.assertEqual(combos[0]["path_id"], "path/auth")
        self.assertEqual(combos[1]["path_id"], "path/perf")
        self.assertIsNone(combos[2]["bucket"])
        for combo in combos:
            if combo["bucket"] is not None:
                self.assertEqual(
                    combo["path_id"],
                    self.pack["buckets"][combo["bucket"]]["path_id"])


class IssueSortEnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pack = sample_pack()
        self.pack_path = os.path.join(self.tmp.name, "pack.json")
        with open(self.pack_path, "w", encoding="utf-8") as handle:
            json.dump(self.pack, handle)
        self.out_path = os.path.join(self.tmp.name, "out.json")

    def test_cli_envelope_carries_combo_and_structural(self):
        from harness import cli
        settings = load_settings()
        settings.jev_api_key = None
        settings.ledger_path = os.path.join(self.tmp.name, "ledger.jsonl")
        with mock.patch("harness.cli.load_settings", return_value=settings):
            cli.main([
                "issue-sort",
                "--issue", "auth token rejected",
                "--pack", self.pack_path,
                "--out", self.out_path,
            ])
        with open(self.out_path, encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertIn("combo", data)
        self.assertIn("structural", data)
        self.assertEqual(data["combo"]["bucket"], "auth")
        self.assertEqual(data["combo"]["path_id"], "path/auth")
        self.assertEqual(data["structural"]["site"], "issue_sort")
        self.assertTrue(data["combo"]["is_fallback"])
        self.assertEqual(data["suggested_next_action"], "open auth backlog")

    def test_mcp_envelope_carries_combo_and_structural(self):
        from harness.mcp import McpServer
        from types import SimpleNamespace
        settings = load_settings()
        settings.jev_api_key = None
        settings.ledger_path = os.path.join(self.tmp.name, "mcp-ledger.jsonl")
        policy = policy_for(settings, ledger=AutonomyLedger(settings.ledger_path))
        engine = SimpleNamespace(jev_policy=policy, settings=settings,
                                 reasoning_effort="auto",
                                 reasoning_token_budget=0.4)
        server = McpServer(
            transport=mock.MagicMock(), api_key="key",
            governor=mock.MagicMock(), ledger=mock.MagicMock(),
            router=mock.MagicMock(), engine=engine)
        result = server._invoke("issue_sort", {
            "issue": "auth token rejected",
            "pack": self.pack,
        })
        self.assertIn("combo", result)
        self.assertIn("structural", result)
        self.assertEqual(result["combo"]["bucket"], "auth")
        self.assertEqual(result["combo"]["path_id"], "path/auth")
        self.assertEqual(result["structural"]["site"], "issue_sort")
        self.assertEqual(result["suggested_next_action"], "open auth backlog")

    def test_mcp_issue_sort_requires_pack_and_policy(self):
        from harness.errors import HarnessError
        from harness.mcp import McpServer
        from types import SimpleNamespace
        settings = load_settings()
        settings.jev_api_key = None
        engine = SimpleNamespace(jev_policy=None, settings=None)
        server = McpServer(
            transport=mock.MagicMock(), api_key="key",
            governor=mock.MagicMock(), ledger=mock.MagicMock(),
            router=mock.MagicMock(), engine=engine)
        with self.assertRaises(HarnessError):
            server._invoke("issue_sort", {"issue": "x"})  # missing pack
        with self.assertRaises(HarnessError):
            server._invoke("issue_sort", {"issue": "x", "pack": self.pack})
        # engine has settings but no jev_policy → policy_for composition path
        engine2 = SimpleNamespace(jev_policy=None, settings=settings)
        server2 = McpServer(
            transport=mock.MagicMock(), api_key="key",
            governor=mock.MagicMock(), ledger=mock.MagicMock(),
            router=mock.MagicMock(), engine=engine2)
        result = server2._invoke("issue_sort", {
            "issue": "auth token", "pack": self.pack})
        self.assertEqual(result["combo"]["bucket"], "auth")
        self.assertEqual(result["structural"]["site"], "issue_sort")

    def test_mcp_tool_schema_declares_issue_sort(self):
        from harness.mcp_schemas import TOOL_SCHEMAS
        names = [t["name"] for t in TOOL_SCHEMAS]
        self.assertIn("issue_sort", names)

    def test_waist_attaches_issue_sort_when_pack_provided(self):
        settings = _unkeyed_settings()
        settings.hourglass_confirm = False
        policy = JevPolicy(settings)
        result = compose_plan(
            transport=None, api_key=None, governor=None, ledger=None,
            opts_goal="auth token login bug", candidate_files=[],
            jev_policy=policy, issue_sort_pack=self.pack)
        self.assertIn("issue_sort", result)
        combo = result["issue_sort"]
        self.assertEqual(combo["bucket"], "auth")
        self.assertEqual(combo["path_id"], "path/auth")
        self.assertEqual(combo["structural"]["site"], "issue_sort")

    def test_waist_reattaches_issue_sort_after_confirmation(self):
        settings = _unkeyed_settings()
        settings.hourglass_confirm = True
        policy = JevPolicy(settings)

        def fake_confirm(**kwargs):
            confirmed = dict(kwargs["plan_result"])
            confirmed["status"] = "approved"
            confirmed.pop("issue_sort", None)
            return confirmed

        gov = mock.MagicMock()
        gov.max_cost = 1.0
        gov.spent = 0.0
        with mock.patch("harness.waist.resolve_waist_ladder",
                        return_value=["judge/model"]), \
             mock.patch("harness.waist.confirm_plan", side_effect=fake_confirm):
            result = compose_plan(
                transport=mock.MagicMock(), api_key="key", governor=gov,
                ledger=mock.MagicMock(),
                opts_goal="auth token login bug", candidate_files=[],
                confirm=True, jev_policy=policy, issue_sort_pack=self.pack)
        self.assertEqual(result["status"], "approved")
        self.assertIn("issue_sort", result)
        self.assertEqual(result["issue_sort"]["bucket"], "auth")


if __name__ == "__main__":
    unittest.main()
