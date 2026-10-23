"""Hermetic cover for every settle-cancelled site and its branches (D12).

The GUI-cancellation lane wrapped every ``evaluate_*`` site with a
``except ProviderUsageUnknown`` / ``except ToolCancelled`` settlement pair
plus hardened internals in ``_settle_cancelled`` / ``_release``. Each pair
body and each internal branch must be executed by the suite. The call shapes
mirror the chaos suite's site table; every call is keyed with a fake
evaluator that raises, so no network is touched.
"""
import copy
import os
import tempfile
import unittest

from harness.config import load_settings
from harness.errors import HarnessError, ProviderUsageUnknown, ToolCancelled
from harness.jev_completion import DEFAULT_COMPLETION_PACK
from harness.jev_policy import JevPolicy
from harness.ledger import AutonomyLedger
from harness.provider_errors import ProviderSpendLimitError
from tests import test_jev_core_hardening as T
from tests.test_jev_issue_sort import sample_pack as issue_pack
from tests.test_jev_log_judgment import sample_log_pack
from tests.test_jev_repo_judgment import STATE, repo_pack

EVIDENCE = {dim: {"score": 10.0, "checks_satisfied": 12, "checks_count": 12,
                  "checks": []} for dim in ("A", "R", "SM", "SD")}

MODEL_PACK = T.PACK
DIFF = T.DIFF


class _DummyTransport:
    def post_once(self, url, key, payload, timeout=120):
        raise AssertionError("must not be reached")


class _RaisingEvaluator:
    """Fake evaluator raising the configured error from both entry points."""

    api_key = "jev-key"
    model = "jev-test"

    def __init__(self, error):
        self.error = error
        self.calls = 0
        self.transport = _DummyTransport()

    def _raise(self):
        self.calls += 1
        if isinstance(self.error, ProviderSpendLimitError):
            raise ProviderSpendLimitError(self.error.detail, http_status=self.error.http_status,
                                          known_cost=self.error.known_cost)
        raise self.error

    def evaluate(self, state, questions=None):
        self._raise()

    def evaluate_once(self, state, questions=None):
        self._raise()

    # Two sites dispatch through specialized evaluator entry points.
    def verify_diff_mechanics(self, *args, **kwargs):
        self._raise()

    def evaluate_plan_requirements(self, *args, **kwargs):
        self._raise()


def _unknown():
    return ProviderUsageUnknown(
        "provider response ended before usage arrived", known_cost=0.004)


def _cancelled():
    return ToolCancelled()


class _CountingGovernor:
    def __init__(self):
        self.reserved = []
        self.reconciled = []
        self.actual = []
        self.overruns = []
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

    def record_overrun(self, cost, label):
        self.overruns.append((cost, label))


class _ActualFailingGovernor(_CountingGovernor):
    def record_actual(self, cost, model):
        raise HarnessError("ledger busy")


class _ReclassifyFailingGovernor(_CountingGovernor):
    def reclassify_unknown(self, token):
        raise HarnessError("cannot reclassify")


class _ReconcileFailingGovernor(_CountingGovernor):
    def reconcile(self, reservation, cost):
        raise HarnessError("already settled")


class _ReserveCancellingGovernor(_CountingGovernor):
    def reserve(self, worst, label):
        raise ToolCancelled(known_cost=0.004)


def _sites():
    """Every wrapped site except answer/issue_sort/provision (covered)."""
    return {
        "diff": lambda p: p.evaluate_diff(DIFF, "set x to 1", "x.py"),
        "hourglass": lambda p: p.evaluate_hourglass_stage(
            "context_intake", {"request": "audit"}),
        "triage": lambda p: p.evaluate_triage("fix loop", ["a.py"]),
        "escalation": lambda p: p.evaluate_escalation_decision("fail ctx"),
        "decision": lambda p: p.evaluate_decision("act", "end", "ctx"),
        "plan": lambda p: p.evaluate_plan("fix loop", ["a.py"]),
        "route": lambda p: p.evaluate_route("fix loop", ["a.py"]),
        "file_triage": lambda p: p.evaluate_file_triage(
            "fix", ["a.py", "b.py"]),
        "claims": lambda p: p.evaluate_claim_support(
            ["sky"], "ctx", enabled=True),
        "completion": lambda p: p.evaluate_completion_nouls("goal", "state"),
        "scope": lambda p: p.evaluate_scope(
            {"goal": "g", "in_scope": ["a"], "state": "s"}),
        "model_route": lambda p: p.evaluate_model_route("do thing", MODEL_PACK),
        "log_item": lambda p: p.evaluate_log_item(
            {"item": "dial failed on swarm"}, sample_log_pack()),
        "repo_summary": lambda p: p.evaluate_repo_summary(STATE, repo_pack()),
        "audit_dims": lambda p: p.evaluate_audit_dimensions(EVIDENCE),
        "phase_completion": lambda p: p.evaluate_phase_completion(
            {"phase": "JEV-P5",
             "status_row": "| JEV-P5 | **complete** | PR #42 MERGED |"},
            copy.deepcopy(DEFAULT_COMPLETION_PACK)),
        "vision": lambda p: p.evaluate_vision_assessment(
            {"assessment": "hourglass"}),
    }


def _all_sites():
    sites = _sites()
    sites.update({
        "answer": lambda p: p.evaluate_answer("prompt", "answer"),
        "simple_strategy": lambda p: p.evaluate_simple_strategy("prompt"),
        "candidate": lambda p: p.evaluate_candidate("orig", "cand", "inst", file_path="x.py"),
        "issue_sort": lambda p: p.evaluate_issue_sort("auth token", issue_pack()),
        "provision": lambda p: p.evaluate_provision({"state": "s"}, {"q": {"type": "noul"}}),
        "reachability": lambda p: p.evaluate_site_reachability({"url": "http://x"}),
        "workflow": lambda p: p.evaluate_request_workflow({"request": "test"}),
        "typed_pack": lambda p: p.evaluate_typed_pack({"state": "s"}, {"q": {"type": "noul"}}, site="custom"),
    })
    return sites


class SettleSitesCoverTests(unittest.TestCase):
    def _policy(self, error, governor=None):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        ledger = AutonomyLedger(os.path.join(td.name, "ledger.jsonl"))
        governor = governor if governor is not None else _CountingGovernor()
        policy = JevPolicy(
            load_settings({"jev_api_key": "jev-key"}),
            evaluator=_RaisingEvaluator(error), governor=governor,
            ledger=ledger)
        return policy, governor

    def test_every_site_settles_lost_usage(self):
        for name, call in _sites().items():
            with self.subTest(site=name):
                policy, governor = self._policy(_unknown())
                with self.assertRaises(ProviderUsageUnknown) as raised:
                    call(policy)
                self.assertAlmostEqual(raised.exception.known_cost, 0.004)
                self.assertTrue(raised.exception.cost_accounted)
                self.assertAlmostEqual(governor.spent, 0.004)
                self.assertIsNone(getattr(policy._tl, "token", None))

    def test_every_site_releases_on_cancellation(self):
        for name, call in _sites().items():
            with self.subTest(site=name):
                policy, governor = self._policy(_cancelled())
                with self.assertRaises(ToolCancelled):
                    call(policy)
                # diff reserves lazily via preflight callback; a raise
                # before the callback leaves nothing to reconcile.
                expected = 0 if name == "diff" else 1
                self.assertEqual(len(governor.reconciled), expected)
                for _reservation, cost in governor.reconciled:
                    self.assertEqual(cost, 0.0)
                self.assertIsNone(getattr(policy._tl, "token", None))

    def test_unknown_usage_books_overrun_when_actual_fails(self):
        policy, governor = self._policy(
            _unknown(), governor=_ActualFailingGovernor())
        with self.assertRaises(ProviderUsageUnknown) as raised:
            policy.evaluate_answer("question", "candidate", "context")
        self.assertTrue(raised.exception.cost_accounted)
        self.assertEqual(len(governor.overruns), 1)
        self.assertAlmostEqual(governor.overruns[0][0], 0.004)
        self.assertIsNone(getattr(policy._tl, "token", None))

    def test_unknown_usage_keeps_liability_when_reclassify_fails(self):
        policy, governor = self._policy(
            _unknown(), governor=_ReclassifyFailingGovernor())
        with self.assertRaises(ProviderUsageUnknown) as raised:
            policy.evaluate_answer("question", "candidate", "context")
        self.assertTrue(raised.exception.cost_accounted)
        self.assertAlmostEqual(governor.spent, 0.004)
        self.assertIsNone(getattr(policy._tl, "token", None))

    def test_pre_accounted_cancellation_releases_without_rebooking(self):
        accounted = ToolCancelled(known_cost=0.004)
        accounted.cost_accounted = True
        policy, governor = self._policy(accounted)
        with self.assertRaises(ToolCancelled):
            policy.evaluate_answer("question", "candidate", "context")
        self.assertEqual(len(governor.reconciled), 1)
        self.assertEqual(governor.reconciled[0][1], 0.0)
        self.assertEqual(governor.actual, [])
        self.assertIsNone(getattr(policy._tl, "token", None))

    def test_unreserved_costly_cancellation_books_actual(self):
        policy, governor = self._policy(
            _cancelled(), governor=_ReserveCancellingGovernor())
        with self.assertRaises(ToolCancelled) as raised:
            policy.evaluate_answer("question", "candidate", "context")
        self.assertTrue(raised.exception.cost_accounted)
        self.assertEqual(len(governor.actual), 1)
        self.assertAlmostEqual(governor.actual[0][0], 0.004)
        self.assertIsNone(getattr(policy._tl, "token", None))

    def test_costly_cancellation_books_overrun_when_reconcile_fails(self):
        policy, governor = self._policy(
            ToolCancelled(known_cost=0.004),
            governor=_ReconcileFailingGovernor())
        with self.assertRaises(ToolCancelled) as raised:
            policy.evaluate_answer("question", "candidate", "context")
        self.assertTrue(raised.exception.cost_accounted)
        self.assertEqual(len(governor.overruns), 1)
        self.assertAlmostEqual(governor.overruns[0][0], 0.004)
        self.assertIsNone(getattr(policy._tl, "token", None))

    def test_stale_handle_release_is_a_guarded_noop(self):
        policy, _governor = self._policy(_cancelled())
        stale = object()
        policy._release(stale)  # nothing reserved: must not reconcile
        self.assertIsNone(getattr(policy._tl, "token", None))

    def test_every_site_releases_after_provider_spend_limit(self):
        for name, call in _all_sites().items():
            with self.subTest(site=name):
                err = ProviderSpendLimitError("limit reached", http_status=429, known_cost=0.002)
                policy, governor = self._policy(err)
                with self.assertRaises(ProviderSpendLimitError) as raised:
                    call(policy)
                self.assertAlmostEqual(raised.exception.known_cost, 0.002)

    def test_provider_spend_limit_zero_cost_dedupe_and_rescue(self):
        err = ProviderSpendLimitError("limit reached", http_status=429, known_cost=0.0)
        policy, governor = self._policy(err)
        # Calls 1-4: hits dedupe repeat (lines 550, 559)
        for _ in range(4):
            with self.assertRaises(ProviderSpendLimitError):
                policy.evaluate_answer("p1", "a1", task_id="t1")
        # Line 531: _release_reservation when thread-local token is set
        policy._tl.token = {"label": "rescue", "worst": 0.005}
        policy._release_reservation(error=err)


if __name__ == "__main__":
    unittest.main()

