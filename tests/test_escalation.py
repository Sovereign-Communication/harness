"""Regression tests for multi-rung escalation, --out parent dirs, claims defs."""
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from harness.claims import _DEFN_RE, _defined_in
from harness.cli import _emit
from harness.errors import HarnessError
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


class SettingsEscalationTests(unittest.TestCase):
    def test_load_settings_ships_escalation_pool_and_judge_top(self):
        from harness.config import load_settings
        s = load_settings({"use_free": True})
        self.assertTrue(s.escalation_pool)
        self.assertTrue(s.judge_top)
        s2 = load_settings({"use_free": False})
        self.assertTrue(any(not x.endswith(":free") for x in s2.escalation_pool)
                        or s2.escalation_pool)


if __name__ == "__main__":
    unittest.main()
