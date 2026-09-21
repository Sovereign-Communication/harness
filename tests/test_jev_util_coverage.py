"""JEV-P3 D12 path coverage: execute remaining policy/waist/orch/panel lines."""
import os
import tempfile
import unittest
from pathlib import Path

from harness import orchestrator as orch
from harness.config import load_settings
from harness.jev import JevEvaluationResult
from harness.jev_policy import policy_for
from harness.ledger import AutonomyLedger
from harness.sliding_scale import classify_task_tier
from harness.waist import compose_plan
from tests._fake import FakeTransport, m, comp, _gov, P1, P2, JUDGE


class _LiveRouteEval:
    """Keyed evaluator that returns a chosen route payload without network."""
    api_key = "jev-key"
    model = "jev-test"

    def __init__(self, route="diff", requires_iteration="maybe"):
        self.route = route
        self.requires_iteration = requires_iteration

    def evaluate(self, state, questions):
        return JevEvaluationResult(
            "pass", 0.85, 0.9,
            {"route": self.route, "requires_iteration": self.requires_iteration},
            ["live"], cost=0.0001, input_tokens=2, output_tokens=1,
            is_fallback=False, model=self.model)

    def evaluate_plan_requirements(self, prompt, target_files=None):
        return JevEvaluationResult(
            "pass", 0.8, 0.9,
            {"requires_iteration": True, "raw": {}},
            ["plan"], cost=0.0001, input_tokens=2, output_tokens=1,
            is_fallback=False, model=self.model)


class _BudgetGovernor:
    def __init__(self):
        self.spent = 0.0

    def reserve(self, amount, label):
        raise __import__("harness.errors", fromlist=["HarnessError"]).HarnessError(
            "budget exhausted")

    def reconcile(self, reservation, amount):
        return None


class _ReserveGovernor:
    def __init__(self):
        self.reconciled = []

    def reserve(self, amount, label):
        return {"label": label, "amount": amount}

    def reconcile(self, reservation, amount):
        self.reconciled.append((reservation, amount))


class _LateHarnessErrorEval:
    api_key = "jev-key"
    model = "jev-test"

    def evaluate(self, state, questions):
        from harness.errors import HarnessError
        raise HarnessError("late policy failure")


def _gov_ok():
    from harness.spend import SpendGovernor
    return SpendGovernor(
        FakeTransport(models=[m("jev-test", prompt="0", completion="0")]),
        "sk-test", max_cost=0.50)


class PolicyExceptAndNormalizeCoverage(unittest.TestCase):
    def test_route_preflight_refusal_hits_harnesserror_fallback(self):
        settings = load_settings({"jev_api_key": "jev-key"})
        policy = policy_for(settings, governor=_BudgetGovernor(),
                            evaluator=_LiveRouteEval())
        result, structural = policy.evaluate_route("loop algorithm", ["a.py"])
        self.assertTrue(result.is_fallback)
        self.assertTrue(structural["is_fallback"])
        self.assertEqual(result.answers["route"], "frontier")

    def test_route_reservation_released_on_late_harness_error(self):
        settings = load_settings({"jev_api_key": "jev-key"})
        gov = _ReserveGovernor()
        policy = policy_for(settings, governor=gov,
                            evaluator=_LateHarnessErrorEval())
        result, structural = policy.evaluate_route("fix typo", ["a.py"])
        self.assertTrue(result.is_fallback)
        self.assertTrue(structural["is_fallback"])
        self.assertTrue(gov.reconciled)
        self.assertEqual(gov.reconciled[-1][1], 0.0)

    def test_file_triage_reservation_released_on_late_harness_error(self):
        settings = load_settings({"jev_api_key": "jev-key"})
        gov = _ReserveGovernor()
        policy = policy_for(settings, governor=gov,
                            evaluator=_LateHarnessErrorEval())
        files = ["a.py"]
        result, structural = policy.evaluate_file_triage(
            "fix a.py", files, known_files=files)
        self.assertTrue(result.is_fallback)
        self.assertTrue(gov.reconciled)

    def test_claim_support_reservation_released_on_late_harness_error(self):
        settings = load_settings({"jev_api_key": "jev-key"})
        gov = _ReserveGovernor()
        policy = policy_for(settings, governor=gov,
                            evaluator=_LateHarnessErrorEval())
        result, structural = policy.evaluate_claim_support(
            [{"id": "c1", "text": "t"}], "ctx", enabled=True)
        self.assertTrue(result.is_fallback)
        self.assertTrue(gov.reconciled)

    def test_completion_reservation_released_on_late_harness_error(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "ok.md").write_text("x\n", encoding="utf-8")
        settings = load_settings({"jev_api_key": "jev-key"})
        gov = _ReserveGovernor()
        policy = policy_for(settings, governor=gov,
                            evaluator=_LateHarnessErrorEval())
        result, structural = policy.evaluate_completion_nouls(
            "write ok.md", "state", named_artifacts=["ok.md"], root_dir=root)
        self.assertTrue(result.is_fallback)
        self.assertTrue(structural["cannot_complete"])
        self.assertTrue(gov.reconciled)

    def test_route_live_outside_vocab_uses_heuristic(self):
        settings = load_settings({"jev_api_key": "jev-key"})
        policy = policy_for(settings, governor=_gov_ok(),
                            evaluator=_LiveRouteEval(route="gpt-6-brand",
                                                     requires_iteration=True))
        result, _ = policy.evaluate_route("fix handler", ["a.py", "b.py"])
        self.assertTrue(result.is_fallback)
        self.assertEqual(result.answers["route"], "diff")

    def test_route_live_valid_route_nonbool_iteration_uses_heuristic_iter(self):
        settings = load_settings({"jev_api_key": "jev-key"})
        policy = policy_for(settings, governor=_gov_ok(),
                            evaluator=_LiveRouteEval(route="frontier",
                                                     requires_iteration="maybe"))
        result, _ = policy.evaluate_route("algorithm loop", ["a.py"])
        self.assertFalse(result.is_fallback)
        self.assertEqual(result.answers["route"], "frontier")
        self.assertTrue(result.answers["requires_iteration"])

    def test_file_triage_and_claims_and_completion_preflight_refusal(self):
        settings = load_settings({"jev_api_key": "jev-key"})
        policy = policy_for(settings, governor=_BudgetGovernor(),
                            evaluator=_LiveRouteEval())
        files = ["a.py", "b.py"]
        r1, s1 = policy.evaluate_file_triage("fix a.py", files, known_files=files)
        self.assertTrue(r1.is_fallback)
        self.assertIn("a.py", s1["files"])
        r2, s2 = policy.evaluate_claim_support(
            [{"id": "c1", "text": "t"}], "ctx", enabled=True)
        self.assertTrue(r2.is_fallback)
        self.assertIsNone(s2["claim_flags"][0]["supported"])
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "ok.md").write_text("x\n", encoding="utf-8")
        r3, s3 = policy.evaluate_completion_nouls(
            "write ok.md", "state", named_artifacts=["ok.md"], root_dir=root)
        self.assertTrue(r3.is_fallback)
        self.assertTrue(s3["cannot_complete"])

    def test_completion_named_artifacts_none_scans_goal(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "real.py").write_text("print(1)\n", encoding="utf-8")
        settings = load_settings()
        settings.jev_api_key = None
        policy = policy_for(settings)
        result, structural = policy.evaluate_completion_nouls(
            "Create real.py and ghost.py", "state",
            named_artifacts=None, root_dir=root)
        self.assertTrue(structural["cannot_complete"])
        self.assertIn("ghost.py", structural["missing_artifacts"])


class OrchestratorCoveragePaths(unittest.TestCase):
    def test_policy_live_empty_files_returns_empty_not_keyword(self):
        class _EmptyFilesEval:
            api_key = "jev-key"
            model = "jev-test"

            def evaluate(self, state, questions):
                return JevEvaluationResult(
                    "pass", 0.9, 0.9, {"files": []}, ["none relevant"],
                    is_fallback=False, model=self.model)

        settings = load_settings({"jev_api_key": "jev-key"})
        policy = policy_for(settings, governor=_gov_ok(), evaluator=_EmptyFilesEval())
        files = ["harness/dag.py", "README.md"]
        picked = orch.triage_files("fix dag", files, chat_fn=None, jev_policy=policy)
        self.assertEqual(picked, [])

    def test_policy_triage_raises_falls_through_to_keyword(self):
        from harness.errors import HarnessError

        class _RaisePolicy:
            def evaluate_file_triage(self, *a, **k):
                raise HarnessError("policy transport failure")

        files = ["harness/dag.py"]
        picked = orch.triage_files("fix dag planner", files, chat_fn=None,
                                   jev_policy=_RaisePolicy())
        self.assertEqual(picked, ["harness/dag.py"])

    def test_assess_completion_nouls_keyed_semantic_refuse(self):
        class _RefuseEval:
            api_key = "jev-key"
            model = "jev-test"

            def evaluate(self, state, questions):
                return JevEvaluationResult(
                    "fail", 0.4, 0.3,
                    {"named_artifacts_present": {"type": "noul", "noul": 0.9},
                     "goal_achieved": {"type": "noul", "noul": 0.2}},
                    ["goal not achieved"], is_fallback=False, model=self.model)

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "out.md").write_text("x\n", encoding="utf-8")
        settings = load_settings({"jev_api_key": "jev-key"})
        policy = policy_for(settings, governor=_gov_ok(), evaluator=_RefuseEval())
        verdict = orch.assess_completion_nouls(
            "write out.md", "state", jev_policy=policy,
            named_artifacts=["out.md"], root_dir=root)
        self.assertTrue(verdict["cannot_complete"])
        self.assertEqual(verdict["missing_artifacts"], [])
        self.assertIn("nouls", verdict["reason"])

    def test_pure_assess_completion_nouls_none_artifacts_scans_goal(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        verdict = orch.assess_completion_nouls(
            "Create present.py and absent.py", "state",
            jev_policy=None, named_artifacts=None, root_dir=root)
        self.assertTrue(verdict["cannot_complete"])
        self.assertIn("absent.py", verdict["missing_artifacts"])
        # dict-shaped facts also accepted
        verdict2 = orch.assess_completion_nouls(
            "g", "s", jev_policy=None,
            named_artifacts=[{"path": "present.py", "present": False}],
            root_dir=root)
        self.assertTrue(verdict2["cannot_complete"])

    def test_keyword_exposed_helper(self):
        self.assertEqual(
            orch.triage_files_keyword("update web tools",
                                      ["harness/web.py", "README.md"]),
            ["harness/web.py"])


class SlidingScaleAndWaistCoverage(unittest.TestCase):
    def test_jev_route_noted_when_tier_already_frontier(self):
        cls = classify_task_tier(
            "refactor concurrency architecture",
            target_files=["a.py", "b.py", "c.py", "d.py"],
            dependency_depth=3, is_leaf=False, previous_failures=2,
            jev_route="free-distill")
        self.assertEqual(cls.tier, 2)
        self.assertTrue(any("jev route" in r and "stays" in r for r in cls.reasons))

    def test_compose_plan_keyed_route_feeds_plan_task(self):
        class _KeyedRoute(_LiveRouteEval):
            def evaluate_plan_requirements(self, prompt, target_files=None):
                return JevEvaluationResult(
                    "pass", 0.8, 0.9, {"requires_iteration": False},
                    ["plan"], is_fallback=False, model=self.model)

        settings = load_settings({"jev_api_key": "jev-key"})
        policy = policy_for(settings, governor=_gov_ok(),
                            evaluator=_KeyedRoute(route="frontier",
                                                  requires_iteration=True))
        result = compose_plan(
            transport=None, api_key=None, governor=_gov_ok(), ledger=None,
            opts_goal="refactor the algorithm loop",
            candidate_files=["harness/sync.py"],
            decompose_llm=False, jev_policy=policy)
        self.assertEqual(result["triage"]["route"], "frontier")
        self.assertFalse(result["triage"]["route_is_fallback"])
        self.assertTrue(result["triage"]["requires_iteration"])
        self.assertIn("STRUCTURAL GUIDELINE", result["goal"])
        self.assertFalse(result["structural"]["is_fallback"])


class LedgerAnalyticsCoveragePaths(unittest.TestCase):
    def _ledger(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        return AutonomyLedger(os.path.join(td.name, "ledger.jsonl"))

    def test_calibration_handles_bad_numeric_and_mid_bucket(self):
        led = self._ledger()
        led.append("jev_eval", task_id="t1", site="apply", verdict="pass",
                   confidence="not-a-number", supported=None,
                   is_fallback=False, input_tokens=1, cost=0.0)
        led.append("verify_round", task_id="t1", round=1, passed=False)
        led.append("jev_eval", task_id="t2", site="waist", verdict="pass",
                   confidence=0.6, supported=0.65, is_fallback=False,
                   input_tokens=1, cost=0.0)
        led.append("verify_round", task_id="t2", round=1, passed=True)
        led.append("jev_eval", task_id="t3", site="route", verdict="fail",
                   confidence=0.2, supported=0.2, is_fallback=False,
                   input_tokens=1, cost=0.0)
        # verify for a task with no jev — should not crash the join
        led.append("verify_round", task_id="t-orphan", round=1, passed=True)
        # jev without verify — skipped in buckets
        led.append("jev_eval", task_id="t-noverify", site="completion",
                   verdict="pass", confidence=0.9, supported=0.9,
                   is_fallback=False, input_tokens=1, cost=0.0)
        report = led.jev_calibration_report()
        self.assertEqual(report["jev_evals"], 4)
        mid = report["confidence_buckets"]["mid_supported_0.5_0.8"]
        self.assertEqual(mid["evals"], 1)
        self.assertEqual(mid["verify_pass_rate"], 1.0)
        low = report["confidence_buckets"]["low_supported_lt_0.5"]
        self.assertEqual(low["evals"], 1)
        high = report["confidence_buckets"]["high_supported_ge_0.8"]
        self.assertEqual(high["evals"], 0)


class PanelClaimSupportCoverage(unittest.TestCase):
    def test_panel_judge_attaches_claim_support_when_flag_on(self):
        from harness.panel import panel_judge
        settings = load_settings({"jev_api_key": "jev-key"})

        class _CS:
            api_key = "jev-key"
            model = "jev-test"

            def evaluate(self, state, questions):
                return JevEvaluationResult(
                    "pass", 0.9, 0.9,
                    {"claim_0_supported": {"type": "noul", "noul": 0.9}},
                    ["ok"], is_fallback=False, model=self.model)

        policy = policy_for(settings, governor=_gov_ok(), evaluator=_CS())
        fake = FakeTransport(models=[m(P1), m(P2), m(JUDGE)],
                             posts=[comp("one"), comp("two"), comp("verdict")])
        gov = _gov(fake)
        result = panel_judge(
            transport=fake, api_key="k", governor=gov, prompt="Q?",
            panel=[P1, P2], judge=JUDGE,
            jev_policy=policy, jev_claim_support=True,
            claim_texts=[{"id": "c1", "text": "something"}],
            claim_evidence="evidence window")
        self.assertIn("claim_support", result)
        flags = result["claim_support"]["claim_flags"]
        self.assertEqual(flags[0]["supported"], True)
        self.assertFalse(result["claim_support"]["skipped"])

    def test_panel_judge_claim_support_error_is_advisory_not_fatal(self):
        from harness.panel import panel_judge

        class _BoomPolicy:
            def evaluate_claim_support(self, *a, **k):
                raise RuntimeError("policy exploded")

        fake = FakeTransport(models=[m(P1), m(P2), m(JUDGE)],
                             posts=[comp("one"), comp("two"), comp("verdict")])
        gov = _gov(fake)
        result = panel_judge(
            transport=fake, api_key="k", governor=gov, prompt="Q?",
            panel=[P1, P2], judge=JUDGE,
            jev_policy=_BoomPolicy(), jev_claim_support=True,
            claim_texts=["x"], claim_evidence="y")
        self.assertTrue(result["claim_support"]["skipped"])
        self.assertEqual(result["panel_results"], result["panel_results"])


if __name__ == "__main__":
    unittest.main()
