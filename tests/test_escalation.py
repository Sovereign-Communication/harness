"""Regression tests for multi-rung escalation, --out parent dirs, claims defs."""
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from harness.claims import _DEFN_RE, _defined_in
from harness.cli_report import _emit
from harness.router import Router


class ClaimsDefnTests(unittest.TestCase):
    def test_rust_and_python_defs_are_detected(self):
        src = (
            "pub fn negotiate_suite() {}\n"
            "pub(crate) fn helper() {}\n"
            "def bar():\n"
            "    pass\n"
            "class Baz:\n"
            "    pass\n"
            "const MAX_SKIP: usize = 8;\n"
        )
        names = _defined_in(src)
        self.assertIn("negotiate_suite", names)
        self.assertIn("helper", names)
        self.assertIn("bar", names)
        self.assertIn("Baz", names)
        self.assertIn("MAX_SKIP", names)

    def test_word_boundary_not_backspace(self):
        self.assertIn(r"\b", _DEFN_RE.pattern)
        self.assertNotIn("\\x08", _DEFN_RE.pattern)


class EmitOutTests(unittest.TestCase):
    def test_emit_creates_parent_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "nested", "deep", "result.json")
            _emit({"status": "ok", "n": 1}, dest)
            with open(dest, encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data["status"], "ok")


class RouterEscalationTests(unittest.TestCase):
    def _router(self, **kw):
        return Router(panel=["a"], judge="j", apply_model="a",
                      escalation_model=None, allow_escalation=True,
                      escalation_pool=["r0", "r1", "r2"], **kw)

    def test_rung_walk_and_deescalate(self):
        r = self._router()
        self.assertEqual(r.escalation()["model"], "r0")
        self.assertTrue(r.advance_escalation_rung())
        self.assertEqual(r.escalation()["model"], "r1")
        self.assertTrue(r.de_escalate_to_rung(0))
        self.assertEqual(r.escalation()["model"], "r0")
        r.reset_escalation()
        self.assertEqual(r.current_escalation_rung(), (0, "r0"))

    def test_not_allowed_returns_none(self):
        r = Router(panel=["a"], judge="j", apply_model="a",
                   allow_escalation=False, escalation_pool=["r0"])
        self.assertIsNone(r.escalation())
        self.assertIsNone(r.escalation(override=False))

    def test_ladder_exhausted_returns_none(self):
        r = self._router()
        r._escalation_rung = 2
        self.assertTrue(r.advance_escalation_rung() is False)
        r._escalation_rung = 5
        self.assertIsNone(r.escalation())


class FakeGov:
    def __init__(self):
        self.spent = 0.0
        self.max_cost = 1.0
        self.preflight_calls = []

    def preflight(self, prompt, calls):
        self.preflight_calls.append(calls)

    def record_actual(self, cost, model):
        self.spent += float(cost or 0)

    def record_byok(self, model):
        pass

    def is_free(self, model):
        return str(model).endswith(":free")


class EscalationDriverTests(unittest.TestCase):
    def test_driver_walks_rung_until_gate_passes(self):
        from harness.escalation import EscalationDriver
        from harness.apply_state import RunState

        router = Router(panel=["a"], judge="j", apply_model="a",
                        allow_escalation=True,
                        escalation_pool=["free/a:free", "paid/b"])
        gov = FakeGov()
        state = RunState(rounds=[], history=[], current_content="x")
        calls = []

        def base_prompt_fn(st, rung_context):
            return f"prompt-{len(calls)}-{rung_context[:10]}"

        def finish_fn(model, content, cost):
            calls.append((model, content))
            if model == "paid/b":
                return {"status": "ok", "model": model}
            return None

        def fake_chat(transport, api_key, model, messages, max_tokens,
                      effort, budget, governor):
            content = f"content-from-{model}"
            return 200, {
                "choices": [{"message": {"content": content},
                             "finish_reason": "stop"}],
                "usage": {"cost": 0.0},
            }

        driver = EscalationDriver(router, transport=None, api_key="k",
                                  governor=gov, ledger=None, task_id="t1")
        req = SimpleNamespace(allow_escalation=True)
        with mock.patch("harness.escalation.chat", side_effect=fake_chat):
            result = driver.run_with_escalation(req, state, base_prompt_fn,
                                                finish_fn)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["model"], "paid/b")
        self.assertEqual([m for m, _ in calls], ["free/a:free", "paid/b"])
        self.assertTrue(gov.preflight_calls)

    def test_driver_respects_allow_escalation_false(self):
        from harness.escalation import EscalationDriver
        from harness.apply_state import RunState

        router = Router(panel=["a"], judge="j", apply_model="a",
                        allow_escalation=True,
                        escalation_pool=["free/a:free", "paid/b"])
        driver = EscalationDriver(router, transport=None, api_key="k",
                                  governor=FakeGov(), ledger=None, task_id="t")
        req = SimpleNamespace(allow_escalation=False)
        result = driver.run_with_escalation(
            req, RunState(rounds=[], history=[], current_content="x"),
            lambda s, c: "p",
            lambda m, c, cost: {"status": "ok"})
        self.assertIsNone(result)

    def test_driver_saturation_rotates_to_next_rung(self):
        # A 429 on a rung is "busy", not "impossible": the ladder walks on
        # to the next, more capable rung -- this is the free-tier-saturation
        # path into the lowest paid rung.
        from harness.escalation import EscalationDriver
        from harness.apply_state import RunState

        router = Router(panel=["a"], judge="j", apply_model="a",
                        allow_escalation=True,
                        escalation_pool=["free/a:free", "paid/b"])
        gov = FakeGov()
        state = RunState(rounds=[], history=[], current_content="x")
        attempted = []

        def fake_chat(transport, api_key, model, messages, max_tokens,
                      effort, budget, governor):
            attempted.append(model)
            if model == "free/a:free":
                return 429, "rate limited"
            return 200, {"choices": [{"message": {"content": "c"},
                                      "finish_reason": "stop"}],
                         "usage": {"cost": 0.0}}

        driver = EscalationDriver(router, transport=None, api_key="k",
                                  governor=gov, ledger=None, task_id="t")
        req = SimpleNamespace(allow_escalation=True)
        with mock.patch("harness.escalation.chat", side_effect=fake_chat):
            result = driver.run_with_escalation(
                req, state, lambda s, c: "p",
                lambda m, c, cost: {"status": "ok", "model": m})
        self.assertEqual(result["model"], "paid/b")
        self.assertEqual(attempted, ["free/a:free", "paid/b"])

    def test_driver_auth_failure_still_fails_closed(self):
        # Auth/transport failures stop the ladder: retrying into the next
        # rung cannot fix a dead key.
        from harness.escalation import EscalationDriver
        from harness.apply_state import RunState

        router = Router(panel=["a"], judge="j", apply_model="a",
                        allow_escalation=True,
                        escalation_pool=["free/a:free", "paid/b"])
        attempted = []

        def fake_chat(transport, api_key, model, messages, max_tokens,
                      effort, budget, governor):
            attempted.append(model)
            return 401, "bad key"

        driver = EscalationDriver(router, transport=None, api_key="k",
                                  governor=FakeGov(), ledger=None, task_id="t")
        req = SimpleNamespace(allow_escalation=True)
        with mock.patch("harness.escalation.chat", side_effect=fake_chat):
            result = driver.run_with_escalation(
                req, RunState(rounds=[], history=[], current_content="x"),
                lambda s, c: "p", lambda m, c, cost: {"status": "ok"})
        self.assertIsNone(result)
        self.assertEqual(attempted, ["free/a:free"])


class SettingsEscalationTests(unittest.TestCase):
    def test_load_settings_ships_escalation_pool_and_judge_top(self):
        from harness.config import load_settings
        s = load_settings({"use_free": True})
        self.assertTrue(s.escalation_pool)
        self.assertTrue(s.judge_top)
        s2 = load_settings({"use_free": False})
        self.assertTrue(s2.escalation_pool)
        self.assertTrue(any(not x.endswith(":free") for x in s2.escalation_pool),
                        f"paid escalation pool should include non-free models: {s2.escalation_pool}")

    def test_paid_key_defaults_escalation_on_with_saturation_ladder(self):
        # The saturation ladder is the default when a paid key is connected:
        # free rungs first, then the paid ladder cheapest-first, escalation
        # armed. Without a key: free-only ladder, escalation disarmed.
        # Hermetic: CONFIG_DIR stays patched for every scenario so the
        # machine's config.json (which may pin its own escalation_pool /
        # allow_escalation) never leaks into the assertions.
        from harness.config import (ESCALATION_POOL_FREE, ESCALATION_POOL_PAID,
                                    load_settings)
        with tempfile.TemporaryDirectory() as cfg, \
                mock.patch("harness.config.CONFIG_DIR", cfg):
            with mock.patch("harness.config.resolve_api_key",
                            return_value="k"):
                armed = load_settings({"use_free": True})
            self.assertTrue(armed.allow_escalation)
            self.assertEqual(armed.escalation_pool,
                             ESCALATION_POOL_FREE + ESCALATION_POOL_PAID)

            with mock.patch("harness.config.resolve_api_key",
                            return_value=None):
                free_only = load_settings({"use_free": True})
            self.assertFalse(free_only.allow_escalation)
            self.assertEqual(free_only.escalation_pool, ESCALATION_POOL_FREE)

            # An explicit false still disarms it even with a key connected.
            with mock.patch("harness.config.resolve_api_key",
                            return_value="k"):
                disarmed = load_settings({"use_free": True,
                                          "allow_escalation": False})
            self.assertFalse(disarmed.allow_escalation)


class ApplyLadderE2ETests(unittest.TestCase):
    """Paid apply-ladder dogfood with fakes: gate-finished rungs, no live spend."""

    def test_ladder_finishes_through_gate_on_second_rung(self):
        from harness.apply import ApplyEngine
        from harness.apply_state import RunState
        from harness.filesafety import _atomic_write
        from harness.router import Router

        class Gov:
            spent = 0.0
            max_cost = 1.0

            def preflight(self, *a, **k):
                return None

            def record_actual(self, cost, model):
                self.spent += float(cost or 0)

            def record_byok(self, model):
                pass

            def is_free(self, model):
                return str(model).endswith(":free")

            def check_byok(self, model):
                return None

            def fetch_pricing(self, models):
                return None

            def fetch_models(self):
                return []

        class Led:
            def append(self, *a, **k):
                return None

            def participation_report(self, *a, **k):
                return {}

        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "t.py")
            _atomic_write(target, "x = 1\n")
            router = Router(
                panel=["p"], judge="j", apply_model="free/a:free",
                allow_escalation=True,
                escalation_pool=["free/cheap:free", "paid/strong"],
            )
            engine = ApplyEngine(
                transport=None, api_key="k", governor=Gov(), ledger=Led(),
                router=router, default_require_consent=False,
            )
            from harness.apply_state import ApplyRequest
            req = ApplyRequest(
                task_id="t", file_path=target, instruction="fix",
                edit_snippet=None, verify_cmd="python -c \"pass\"",
                backend="harness", verify_only=False, max_lines=500,
                max_rounds=1, max_tokens=256, task_max_cost=0.05, max_rot=0,
                reasoning="off", renew=False, allow_escalation=True,
                model="free/a:free", ordered=["free/cheap:free"],
                profiles=None, want_consent=False, original="x = 1\n",
                task_start_spent=0.0, continuation={}, continuation_gate=None,
                task_runner=None, cancel_check=None,
            )
            state = RunState(rounds=[{
                "round": 1, "model": "free/a:free", "status": "verify_failed",
                "verify_output": "AssertionError", "cost": 0.0,
            }], history=[], current_content="x = 1\n")
            state.gate_broken = False

            prompts = []

            def base_prompt_fn(st, rung_context):
                prompts.append(rung_context)
                return "prompt"

            def finish_fn(model, content, cost):
                if model == "free/cheap:free":
                    return None  # gate fail first rung
                _atomic_write(target, "x = 2\n")
                return {
                    "status": "ok", "task_id": "t", "changed": True,
                    "rounds": state.rounds, "cost": 0.01, "rotations": 0,
                    "backend": "harness", "verify": {"passed": True},
                    "escalated": True,
                }

            def fake_chat(transport, api_key, model, messages, max_tokens,
                          effort, budget, governor):
                return 200, {
                    "choices": [{"message": {"content": "x = 2\n"},
                                 "finish_reason": "stop"}],
                    "usage": {"cost": 0.0},
                }

            from harness.escalation import EscalationDriver
            with mock.patch("harness.escalation.chat", side_effect=fake_chat):
                driver = EscalationDriver(
                    router, None, "k", engine.governor, None, "t",
                    max_tokens=256, task_start_spent=0.0, task_max_cost=0.05,
                )
                result = driver.run_with_escalation(
                    req, state, base_prompt_fn, finish_fn)
            self.assertIsNotNone(result)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(len(prompts), 2)


    def test_pool_exhaustion_escalates_to_paid_rung_instead_of_dying(self):
        # THE saturation contract: the cheap tier 429s across its whole pool.
        # The lane must walk the escalation ladder into the lowest paid rung
        # (which answers) -- never raise "pool exhausted" past the ladder,
        # which is what let a saturated free tier kill orchestrator nodes
        # while the paid key sat unused. With escalation disarmed, the same
        # saturation lands in the honest terminal failure (still no raise).
        from harness.apply import ApplyEngine
        from harness.filesafety import _atomic_write

        class Gov:
            spent = 0.0
            max_cost = 1.0

            def preflight(self, *a, **k):
                return None

            def record_actual(self, cost, model):
                self.spent += float(cost or 0)

            def record_byok(self, model):
                pass

            def is_free(self, model):
                return str(model).endswith(":free")

            def check_byok(self, model):
                return None

            def fetch_pricing(self, models):
                return None

            def fetch_models(self):
                return []

            def learned_blocked(self, model):
                return False

        class Led:
            def append(self, *a, **k):
                return None

            def participation_report(self, *a, **k):
                return {}

        def build_engine(escalation_pool, allow_escalation):
            router = Router(
                panel=["p"], judge="j", apply_model="free/a:free",
                allow_escalation=allow_escalation,
                escalation_pool=escalation_pool,
            )
            return ApplyEngine(
                transport=None, api_key="k", governor=Gov(), ledger=Led(),
                router=router, default_require_consent=False,
            )

        def saturated_chat(transport, api_key, model, messages, max_tokens,
                           effort, budget, governor):
            if str(model).endswith(":free"):
                return 429, "rate limited"
            return 200, {
                "choices": [{"message": {"content": "x = 2\n"},
                             "finish_reason": "stop"}],
                "usage": {"cost": 0.0},
            }

        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "t.py")
            _atomic_write(target, "x = 1\n")
            from harness.apply_state import ApplyRequest

            def make_req():
                return ApplyRequest(
                    task_id="t", file_path=target, instruction="fix",
                    edit_snippet=None, verify_cmd="python -c \"pass\"",
                    backend="harness", verify_only=False, max_lines=500,
                    max_rounds=3, max_tokens=256, task_max_cost=0.05,
                    max_rot=0, reasoning="off", renew=False,
                    allow_escalation=None, model="free/a:free",
                    ordered=["free/a:free"], profiles=None,
                    want_consent=False, original="x = 1\n",
                    task_start_spent=0.0, continuation={},
                    continuation_gate=None, task_runner=None,
                    cancel_check=None,
                )

            # Armed ladder: saturation rotates into the paid rung, which
            # answers, and the gate finishes it ok.
            engine = build_engine(["paid/strong"], True)
            with mock.patch("harness.apply_policy.chat",
                            side_effect=saturated_chat), \
                 mock.patch("harness.escalation.chat",
                            side_effect=saturated_chat), \
                 mock.patch("harness.apply_policy.consent_renew",
                            return_value={"decision": "accept"}):
                result = engine.apply_edit(
                    file_path=target, instruction="fix",
                    verify_cmd="python -c \"pass\"", allow_verify=True,
                    require_consent=False)
            self.assertEqual(result["status"], "ok")
            self.assertTrue(result.get("escalated"))
            with open(target, encoding="utf-8") as f:
                self.assertEqual(f.read(), "x = 2\n")

            # Disarmed: the same saturation lands in an honest structured
            # terminal failure -- never an escaped exception.
            engine2 = build_engine([], False)
            with mock.patch("harness.apply_policy.chat",
                            side_effect=saturated_chat), \
                 mock.patch("harness.escalation.chat",
                            side_effect=saturated_chat), \
                 mock.patch("harness.apply_policy.consent_renew",
                            return_value={"decision": "accept"}):
                failed = engine2.apply_edit(
                    file_path=target, instruction="fix",
                    verify_cmd="python -c \"pass\"", allow_verify=True,
                    require_consent=False)
            self.assertNotEqual(failed["status"], "ok")
            self.assertIn(failed["status"],
                          ("failed", "verify_failed", "consent_blocked"))


if __name__ == "__main__":
    unittest.main()
