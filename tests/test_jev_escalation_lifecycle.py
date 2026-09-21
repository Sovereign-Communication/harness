"""Lifecycle (integration) tests for Jev-directed escalation (JEV-P2-dead-code).

The unit tests in ``test_jev_escalation_driver.py`` prove each piece; this
module drives the WHOLE production seam: a real ``Router``, the real
``ApplyEngine`` apply loop and ``_escalate`` path, the real
``EscalationDriver``, ``ApplyGate``, ``AutonomyLedger``, and the real
``SpendGovernor`` -- including its live model-list validation of ladder
models. Fakes sit ONLY at the two documented doctrine seams: the chat
transport (``FakeTransport``) and the Jev evaluator (a scripted duck policy
mirroring the real ``JevPolicy`` surface).

The verify gate is content-keyed: it passes iff the file on disk actually
equals the intended edit, so every asserted pass is a REAL state transition
(the rung's answer landed and the gate let it through), not a stubbed return
code. No network anywhere; each scenario runs a full apply in well under a
second, so the module lives in the default battery.
"""
import os
import tempfile
import unittest
from types import SimpleNamespace

from harness.apply import ApplyEngine
from harness.jev import JevEvaluationResult
from harness.ledger import AutonomyLedger
from harness.router import Router
from harness.spend import SpendGovernor
from tests._fake import FakeTransport, comp, m
from tests.test_jev_lane_parity import hermetic_settings

PRIMARY = "cheap/a"
POOL = ["rung0/mid", "rung1/deep", "rung2/frontier"]
ORIGINAL = "x = 1\n"
CHANGED = "x = 2\n"
WRONGS = ["x = 10\n", "x = 20\n", "x = 30\n"]
MARKER = "JEV-DIRECTED ESCALATION"


def _jev_result(confidence, budget=None, fallback=False):
    """A decision result in the exact live noul-pack answer shape."""
    answers = {"escalation_decision": {"type": "noul", "noul": confidence}}
    if budget is not None:
        answers["capability_budget"] = {"type": "noul", "noul": budget}
    return JevEvaluationResult("pass" if confidence >= 0.7 else "fail",
                               confidence, confidence, answers=answers,
                               is_fallback=fallback, model="jev-test")


class _ScriptedJevPolicy:
    """Duck policy: canned decision nouls; candidate nouls pass (pre-write)."""

    def __init__(self, result):
        self.settings = hermetic_settings()
        self.evaluator = SimpleNamespace(model="jev-test")
        self._result = result
        self.decision_calls = []

    def evaluate_escalation_decision(self, context, **kwargs):
        self.decision_calls.append(context)
        return self._result, {"site": "escalation-decision",
                              "verdict": self._result.verdict}

    def evaluate_candidate(self, *args, **kwargs):
        return (JevEvaluationResult("pass", 0.95, 0.95, {}, [],
                                    is_fallback=False, model="jev-test"),
                {"site": "apply", "verdict": "pass"})


class _LifecycleCase(unittest.TestCase):
    """Shared full-engine environment (one apply per scenario)."""

    def _env(self, decision_result, bodies):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        target = os.path.join(tmp.name, "x.py")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(ORIGINAL)
        transport = FakeTransport(
            models=[m(PRIMARY), m("judge/j")] + [m(p) for p in POOL],
            posts=[comp(b) for b in bodies])
        ledger = AutonomyLedger(os.path.join(tmp.name, "ledger.jsonl"))
        policy = _ScriptedJevPolicy(decision_result)
        engine = ApplyEngine(
            transport, "sk-test", SpendGovernor(transport, "sk-test"),
            ledger,
            Router([PRIMARY], "judge/j", PRIMARY,
                   escalation_pool=list(POOL), allow_escalation=True),
            default_require_consent=False, default_renew_consent=False,
            jev_policy=policy, min_confidence=0.70)

        def verify(cmd, timeout=None, cwd=None):
            with open(target, encoding="utf-8") as fh:
                content = fh.read()
            return ((0, "ok") if content == CHANGED
                    else (1, "content mismatch"))

        engine.run_verify = verify
        return _Env(engine=engine, policy=policy, ledger=ledger,
                    target=target, transport=transport)

    def _apply(self, env):
        return env.engine.apply_edit(
            task_id="lifecycle", file_path=env.target,
            instruction="change x", verify_cmd='python -c "pass"',
            require_consent=False, edit_snippet=CHANGED, max_rounds=2)

    @staticmethod
    def _posted_models(transport):
        return [p["model"].split(":")[0] for p in transport.payloads()]

    @staticmethod
    def _prompt_of(transport, model):
        for p in transport.payloads():
            if p["model"].split(":")[0] == model:
                msgs = p.get("messages") or []
                return "".join(str(x.get("content")) for x in msgs
                               if isinstance(x, dict))
        return None

    def _events(self, ledger, name):
        return [e for e in ledger.entries() if e["event"] == name]

    def _disk(self, target):
        with open(target, encoding="utf-8") as fh:
            return fh.read()


class _Env:
    """Bundle passed around by the scenario cases."""

    def __init__(self, engine, policy, ledger, target, transport):
        self.engine = engine
        self.policy = policy
        self.ledger = ledger
        self.target = target
        self.transport = transport


class JevDirectedEscalateTests(_LifecycleCase):
    """Low decision noul -> Jev directs the walk; failed rungs never re-bought."""

    def test_conf_005_directs_walk_to_top_rung_and_lands_edit(self):
        # conf 0.05 < 0.075: the mapper takes the ladder's TOP rung; the
        # already-failed rung 0 (and every other rung) must never be bought.
        env = self._env(_jev_result(0.05, budget=0.90),
                        [WRONGS[0], WRONGS[1], CHANGED])
        result = self._apply(env)
        models = self._posted_models(env.transport)
        self.assertEqual(models[:2], [PRIMARY, PRIMARY],
                         f"primary should exhaust both retries first: {models}")
        self.assertEqual(len(env.policy.decision_calls), 1,
                         f"Jev consulted exactly once: {env.policy.decision_calls}")
        self.assertIn("rounds tried: 2", env.policy.decision_calls[0])
        self.assertIn("last verify output:", env.policy.decision_calls[0])
        self.assertEqual(models[2:], ["rung2/frontier"],
                         f"walk must start (and stop) at the top rung: {models}")
        self.assertTrue(result.get("escalated"),
                        f"engine must report escalation: {result}")
        self.assertEqual(result.get("escalated_to"), "rung2/frontier")
        prompt = self._prompt_of(env.transport, "rung2/frontier") or ""
        self.assertIn(MARKER, prompt, "Jev evidence marker must reach the prompt")
        self.assertIn("content mismatch", prompt,
                      "code-owned failure evidence must reach the prompt")
        self.assertEqual(self._disk(env.target), CHANGED,
                         "the rung's passing edit must REALLY land on disk")
        self.assertTrue(any(e.get("to_model") == "rung2/frontier"
                            for e in self._events(env.ledger, "escalate")),
                        "a real escalate event must be ledgered")
        self.assertIsNone(result.get("pending_jev_directive"),
                          "directive must be consumed (one-shot)")

    def test_conf_030_starts_second_from_top_rung(self):
        # conf 0.30 in [0.075, 0.5): second-from-top rung, still skipping the
        # already-failed rung 0; the ladder remains the executor.
        env = self._env(_jev_result(0.30, budget=0.90),
                        [WRONGS[0], WRONGS[1], CHANGED])
        result = self._apply(env)
        models = self._posted_models(env.transport)
        self.assertEqual(models[2:], ["rung1/deep"],
                         f"walk must start at rung 1: {models}")
        self.assertTrue(result.get("escalated"))
        self.assertEqual(result.get("escalated_to"), "rung1/deep")
        self.assertIn(MARKER, self._prompt_of(env.transport, "rung1/deep") or "")
        self.assertEqual(self._disk(env.target), CHANGED,
                         "the rung's passing edit must REALLY land on disk")


class JevAbstainTests(_LifecycleCase):
    """Low budget noul -> should_abstain retires the walk (zero rung spend)."""

    def test_budget_noul_abstains_and_retires_walk(self):
        env = self._env(_jev_result(0.90, budget=0.05),
                        [WRONGS[0], WRONGS[1]])
        result = self._apply(env)
        self.assertEqual(len(env.policy.decision_calls), 1,
                         "Jev must be consulted once (decision made)")
        self.assertEqual(self._posted_models(env.transport),
                         [PRIMARY, PRIMARY],
                         "zero rung spend: only the primary retries")
        self.assertFalse(result.get("escalated"))
        self.assertEqual(self._events(env.ledger, "escalate"), [],
                         "no escalate event may be ledgered")
        self.assertIsNone(result.get("pending_jev_directive"),
                          "directive must be consumed (one-shot)")


class FallbackWalkTests(_LifecycleCase):
    """Fallback Jev -> honest status-quo walk from rung 0; ladder still lands."""

    def test_unkeyed_fallback_walks_status_quo_from_rung_0(self):
        env = self._env(_jev_result(0.05, budget=0.90, fallback=True),
                        [WRONGS[0], WRONGS[1], WRONGS[2], CHANGED])
        result = self._apply(env)
        models = self._posted_models(env.transport)
        self.assertEqual(models[2:], ["rung0/mid", "rung1/deep"],
                         f"status-quo walk climbs from rung 0: {models}")
        for p in POOL:
            self.assertNotIn(MARKER, self._prompt_of(env.transport, p) or "",
                             "no Jev marker in a status-quo walk")
        self.assertEqual(result.get("escalated_to"), "rung1/deep",
                         f"passed on the frontier rung: {result}")
        self.assertEqual(self._disk(env.target), CHANGED,
                         "the ladder must still land the edit")
        self.assertTrue(any(e.get("to_model") == "rung1/deep"
                            for e in self._events(env.ledger, "escalate")),
                        "a real escalate event must be ledgered")


if __name__ == "__main__":
    unittest.main()
