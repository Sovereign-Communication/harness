"""Hermetic parity checks for the shared JEV-P1 lane envelope.

These tests pin the unkeyed structural envelope on every lane. They must
pass on operator machines with live harness settings AND on clean CI.

Hermetic contract (test doubles / settings isolation -- production
fail-closed readiness is NOT weakened):
- Jev policy is constructed from an isolated settings object with
  ``jev_api_key=None`` so the local structural fallback is what runs.
- Consent is off; readiness is treated as confident without extra model
  calls (missing HARNESS_READY is production behavior; the gate still
  guards). FakeTransport posts are sized for every chat call apply makes,
  or the scripted gate runner prevents a second round entirely.
- ``run_verify`` is scripted (never host-shell ``true``), so Windows and
  Linux share one deterministic gate outcome.
"""
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from harness.apply import ApplyEngine
from harness.batch import BatchOptions
from harness.errors import HarnessError
from harness.jev_policy import JevPolicy, policy_for
from harness.ledger import AutonomyLedger
from harness.router import Router
from harness.spend import SpendGovernor
from harness.waist import compose_plan
from tests._fake import FakeTransport, comp, m


ORIGINAL = "x = 1\n"
CHANGED = "x = 2\n"
MODEL = "test/model"
JUDGE = "judge/model"


def hermetic_settings(**overrides):
    """Isolated settings: never inherit live jev keys or hourglass defaults.

    Start from a real Settings object (CLI/MCP need the full attribute set)
    then FORCE the disarmed envelope. ``load_settings()`` on an operator
    machine resolves real keys from config/env; post-assignment is required
    because ``jev_api_key=get(...) or resolve_jev_key()`` re-arms a None
    override from the live key files.
    """
    from harness.config import load_settings
    settings = load_settings()
    settings.jev_api_key = None
    settings.min_confidence = 0.70
    settings.hourglass_confirm = False
    settings.hourglass_isolate = False
    settings.hourglass_parallel = False
    settings.hourglass_require_attestation = False
    settings.frontier_model = None
    settings.use_free = True
    settings.allow_escalation = False
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


def scripted_run(results=None):
    """Deterministic gate runner: no host shell, no network, no chat."""
    seq = list(results if results is not None else [(0, "")])
    state = {"n": 0}

    def runner(cmd, timeout=None, cwd=None):
        rc, out = seq[min(state["n"], len(seq) - 1)]
        state["n"] += 1
        return rc, out

    return runner


class LaneParityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.target = os.path.join(self.tmp.name, "x.py")
        with open(self.target, "w", encoding="utf-8") as stream:
            stream.write(ORIGINAL)
        self.ledger = AutonomyLedger(os.path.join(self.tmp.name, "ledger.jsonl"))
        self.settings = hermetic_settings(
            ledger_path=os.path.join(self.tmp.name, "settings-ledger.jsonl"))

    def _policy(self):
        return policy_for(self.settings)

    def _engine(self, *, posts=None, policy=None):
        """Disarmed hermetic apply engine.

        - unkeyed (or caller-supplied) jev policy
        - consent / renew off so apply makes exactly one chat POST
        - scripted verify gate (Windows-safe; no second-round chat)
        - FakeTransport posts sized for every chat call apply will make
        """
        transport = FakeTransport(
            models=[m(MODEL), m(JUDGE)], posts=posts)
        governor = SpendGovernor(transport, "sk-test")
        engine = ApplyEngine(
            transport, "sk-test", governor, self.ledger,
            Router([MODEL], JUDGE, MODEL),
            default_require_consent=False, default_renew_consent=False,
            jev_policy=policy if policy is not None else self._policy(),
            min_confidence=0.70,
        )
        engine.run_verify = scripted_run([(0, "")])
        return engine, transport

    def test_apply_envelope_contains_structural_for_unkeyed_policy(self):
        engine, _ = self._engine(posts=[comp(CHANGED)], policy=self._policy())
        result = engine.apply_edit(
            task_id="apply", file_path=self.target, instruction="change x",
            verify_cmd="python -c \"pass\"", require_consent=False,
            edit_snippet=CHANGED,
        )
        self.assertIn("structural", result)
        self.assertTrue(result["structural"]["is_fallback"])
        # Unkeyed policy never claims a live TypeSafe model.
        self.assertNotEqual(result["structural"]["model"], "live-hallucination")

    def test_waist_envelope_contains_structural(self):
        policy = self._policy()
        result = compose_plan(
            transport=None, api_key=None, governor=None, ledger=None,
            opts_goal="Update x.py", candidate_files=[self.target],
            execute=False, confirm=False, jev_policy=policy,
        )
        self.assertIn("structural", result)
        self.assertTrue(result["structural"]["is_fallback"])

    def test_cli_plan_preview_exposes_structural_envelope(self):
        from harness.cli import _cmd_plan
        settings = self.settings
        opts = SimpleNamespace(
            goal="Update x.py", file=[self.target], frontier_model=None,
            execute=False, out=None, decompose_llm=False,
            allow_escalation=False, max_tokens=None, max_cost=None,
        )
        with patch("harness.cli._emit") as emit:
            _cmd_plan(opts, settings)
        result = emit.call_args[0][0]
        self.assertIn("structural", result)
        self.assertTrue(result["structural"]["is_fallback"])
        self.assertEqual(result["structural"]["site"], "cli")

    def test_mcp_plan_preview_exposes_structural_envelope(self):
        from harness.mcp import McpServer
        policy = self._policy()
        engine = MagicMock()
        engine.jev_policy = policy
        server = McpServer(
            transport=MagicMock(), api_key=None,
            governor=MagicMock(), ledger=self.ledger,
            router=MagicMock(judge=JUDGE, panel_pool=[]), engine=engine,
        )
        result = server._invoke("plan_and_execute", {
            "goal": "Update x.py", "file": [self.target],
            "execute": False, "confirm": False,
        })
        self.assertIn("structural", result)
        self.assertTrue(result["structural"]["is_fallback"])
        self.assertEqual(result["structural"]["site"], "mcp")

    def test_batch_aggregates_child_structural_envelopes(self):
        engine, transport = self._engine(
            posts=[comp(CHANGED)], policy=self._policy())
        result = engine.apply_batch(
            [self.target], options=BatchOptions(
                instruction="change x", verify_cmd="python -c \"pass\"",
                require_consent=False, edit_snippet=CHANGED))
        # A one-file batch intentionally returns the child terminal shape; the
        # child is the parity surface and carries the same stable block.
        self.assertIn("structural", result)
        self.assertTrue(result["structural"]["is_fallback"])
        # Exactly one chat POST for the single apply edit; gate is scripted.
        self.assertEqual(len(transport.chat_posts()), 1)

    def test_surplus_posts_do_not_break_envelope(self):
        """Transport that has spare canned replies still yields the envelope.

        Apply may emit extra non-chat work; a surplus of canned posts must
        not change the parity contract.
        """
        engine, transport = self._engine(
            posts=[comp(CHANGED), comp(CHANGED), comp(CHANGED)],
            policy=self._policy())
        result = engine.apply_edit(
            task_id="apply", file_path=self.target, instruction="change x",
            verify_cmd="python -c \"pass\"", require_consent=False,
            edit_snippet=CHANGED,
        )
        self.assertIn("structural", result)
        self.assertTrue(result["structural"]["is_fallback"])


class D12CoverageTests(unittest.TestCase):
    """Focused hermetic tests that EXECUTE previously untested changed lines.

    These must run the real production paths -- not mock the lines away --
    so refresh_coverage_baseline.py can honestly mark them executed.
    """

    def test_apply_request_default_min_confidence(self):
        """apply_state.ApplyRequest.min_confidence field default (D12)."""
        from harness.apply_state import ApplyRequest
        fields = dict(
            task_id="t", file_path="x.py", instruction="edit",
            edit_snippet=None, verify_cmd=None, backend="harness",
            verify_only=False, max_lines=10, max_rounds=1,
            max_tokens=100, task_max_cost=0.1, max_rot=0,
            reasoning="auto", renew=False, allow_escalation=None,
            model="m", ordered=None, profiles=None, want_consent=False,
            original="", task_start_spent=0.0, continuation=None,
            continuation_gate=None, task_runner=None, cancel_check=None,
        )
        default_req = ApplyRequest(**fields)
        self.assertEqual(default_req.min_confidence, 0.70)
        explicit = ApplyRequest(**{**fields, "min_confidence": 0.83})
        self.assertEqual(explicit.min_confidence, 0.83)

    def test_apply_engine_propagates_min_confidence_to_request(self):
        """Engine constructor → ApplyRequest.min_confidence wiring (D12)."""
        from harness.apply_state import ApplyRequest
        with tempfile.TemporaryDirectory() as td:
            ledger = AutonomyLedger(os.path.join(td, "ledger.jsonl"))
            target = os.path.join(td, "x.py")
            with open(target, "w", encoding="utf-8") as stream:
                stream.write(ORIGINAL)
            transport = FakeTransport(models=[m(MODEL), m(JUDGE)])
            governor = SpendGovernor(transport, "sk-test")
            engine = ApplyEngine(
                transport, "sk-test", governor, ledger,
                Router([MODEL], JUDGE, MODEL),
                default_require_consent=False,
                min_confidence=0.42,
            )
            self.assertEqual(engine.min_confidence, 0.42)
            req = engine._prepare({
                "task_id": "t", "file_path": target,
                "instruction": "x", "require_consent": False,
                "edit_snippet": CHANGED,
            })
            self.assertIsInstance(req, ApplyRequest)
            self.assertEqual(req.min_confidence, 0.42)

    def test_consent_low_confidence_accept_defer_path_executes(self):
        """consent.py confidence parse + low-confidence defer (D12)."""
        import json as json_mod
        from harness.consent import probe_consent
        body = {"decision": "accept", "confidence": 0.35,
                "reason": "unsure", "redirect_model": None,
                "scope_suggestion": None}
        with tempfile.TemporaryDirectory() as td:
            ledger = AutonomyLedger(os.path.join(td, "ledger.jsonl"))
            transport = FakeTransport(
                models=[m("judge")], posts=[comp(json_mod.dumps(body))])
            governor = SpendGovernor(transport, "sk-test")
            result = probe_consent(
                transport=transport, api_key="k", governor=governor,
                task_id="t", task="work", model="judge", ledger=ledger,
                min_confidence=0.70)
            self.assertEqual(result["decision"], "defer")
            self.assertFalse(result["dispatched"])
            self.assertEqual(result["confidence"], 0.35)
            self.assertIn("0.350", result["reason"])
            self.assertIn("0.700", result["reason"])

    def test_consent_nonfinite_confidence_is_stripped(self):
        """consent.py rejects NaN/inf without authorizing accept (D12)."""
        import json as json_mod
        from harness.consent import probe_consent
        for bad in (float("nan"), float("inf"), float("-inf")):
            body = {"decision": "accept", "confidence": bad,
                    "reason": "odd", "redirect_model": None,
                    "scope_suggestion": None}
            transport = FakeTransport(
                models=[m("judge")],
                posts=[comp(json_mod.dumps(body))])
            result = probe_consent(
                transport=transport, api_key="k",
                governor=SpendGovernor(transport, "sk-test"),
                task_id="t", task="work", model="judge")
            self.assertIsNone(result["confidence"])
            self.assertEqual(result["decision"], "accept")

    def test_evaluate_triage_keyed_exception_releases_reservation(self):
        """jev_policy.evaluate_triage exception path (D12 235/239/241/242).

        Preflight reserves, evaluate raises HarnessError, reconcile is
        attempted and swallowed, then the honest local heuristic fallback
        is constructed (never pretending to be live).
        """
        class _ExplodingGovernor:
            def __init__(self):
                self.reservations = []
                self.reconciled = []

            def reserve(self, amount, label):
                token = {"amount": amount, "label": label}
                self.reservations.append(token)
                return token

            def reconcile(self, reservation, amount):
                self.reconciled.append((reservation, amount))
                raise HarnessError("reconcile boom")

        class _KeyedRaiseEvaluator:
            api_key = "jev-key"
            model = "jev-test"

            def evaluate(self, state, questions):
                raise HarnessError("eval boom")

        settings = hermetic_settings(jev_api_key="jev-key")
        governor = _ExplodingGovernor()
        policy = JevPolicy(settings, governor=governor,
                           evaluator=_KeyedRaiseEvaluator())
        result, structural = policy.evaluate_triage(
            "refactor the algorithm loop", ["a.py"], site="triage")
        self.assertTrue(result.is_fallback)
        self.assertTrue(structural["is_fallback"])
        self.assertEqual(result.answers["route"], "frontier")
        self.assertTrue(result.answers["requires_iteration"])
        self.assertEqual(structural["site"], "triage")
        self.assertTrue(governor.reservations)
        self.assertTrue(governor.reconciled)

    def test_evaluate_triage_keyed_eval_error_no_governor_still_heuristic(self):
        """HarnessError with no governor still builds local heuristic (D12)."""

        class _KeyedRaiseEvaluator:
            api_key = "jev-key"
            model = "jev-test"

            def evaluate(self, state, questions):
                raise HarnessError("eval boom")

        settings = hermetic_settings(jev_api_key="jev-key")
        policy = JevPolicy(settings, governor=None,
                           evaluator=_KeyedRaiseEvaluator())
        # Without a governor, keyed preflight refuses before evaluate.
        result, structural = policy.evaluate_triage(
            "loop forever", ["a.py", "b.py"], site="triage")
        self.assertTrue(result.is_fallback)
        self.assertTrue(structural["is_fallback"])
        self.assertEqual(result.answers["route"], "frontier")

    def test_evaluate_triage_keyed_transport_error_falls_back(self):
        """Keyed live call that never returns still yields local heuristic."""
        class _DeadTransport:
            def post(self, url, key, payload, timeout=45):
                raise OSError("network down")

        settings = hermetic_settings(jev_api_key="jev-key")
        policy = policy_for(settings, transport=_DeadTransport())
        # evaluator is live-keyed but transport exceptions are swallowed
        # inside JevEvaluator.evaluate → local structural eval → heuristic.
        result, structural = policy.evaluate_triage(
            "simple rename of one function", ["only.py"], site="triage")
        self.assertTrue(result.is_fallback)
        self.assertTrue(structural["is_fallback"])
        self.assertEqual(result.answers["route"], "free-distill")
        self.assertFalse(result.answers["requires_iteration"])

    def test_waist_triage_requires_iteration_wiring(self):
        """waist.compose_plan triage envelope wiring (D12 1018-1020)."""
        settings = hermetic_settings()
        policy = policy_for(settings)
        result = compose_plan(
            transport=None, api_key=None, governor=None, ledger=None,
            opts_goal="design an algorithm loop over the graph",
            candidate_files=["a.py"], execute=False, confirm=False,
            jev_policy=policy,
        )
        self.assertIn("triage", result)
        self.assertTrue(result["triage"]["is_fallback"])
        self.assertEqual(result["triage"]["route"], "frontier")
        self.assertTrue(result["triage"]["requires_iteration"])
        self.assertIn("structural", result)
        self.assertTrue(result["structural"]["is_fallback"])
        self.assertIn("STRUCTURAL GUIDELINE", result["goal"])

    def test_waist_triage_non_iterative_route(self):
        """Non-iterative single-file goal wires free-distill + requires_iteration False."""
        settings = hermetic_settings()
        policy = policy_for(settings)
        result = compose_plan(
            transport=None, api_key=None, governor=None, ledger=None,
            opts_goal="rename a variable in one helper",
            candidate_files=["only.py"], execute=False, confirm=False,
            jev_policy=policy,
        )
        self.assertEqual(result["triage"]["route"], "free-distill")
        self.assertFalse(result["triage"]["requires_iteration"])


if __name__ == "__main__":
    unittest.main()
