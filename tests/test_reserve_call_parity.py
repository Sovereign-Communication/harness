"""Reserve == call worst case: the preflight contract, per lane.

The ONE rule (spend.py preflight): a reserve row overstates the ceiling the
moment its (max_tokens, slot count) exceeds what the call it represents can
actually spend -- and understates it when a bounded attempt is missing. These
tests pin every governed lane's reserve to its call's true worst case:

  * panel votes          -- reserve tokens == the vote payload's max_tokens
  * judge primary seat   -- transient-retry slot present, tokens == payload
  * judge fallback seats -- each candidate's own reasoning-rejection slots
  * convergence spec     -- specialist reserve tokens == specialist call
                            (regression: it reserved the judge budget)
  * escalation rungs     -- rung reserve == rung chat
  * consent probes       -- explicit-disable slots == the 400-retry worst case
  * apply attempts       -- reserve tokens == the apply payload

Routing, model selection, payloads, and envelopes are out of scope here.
"""
import os
import tempfile
import unittest
from unittest import mock

from harness.chat import _chat_reservation_slots
from harness.panel import panel_judge
from harness.spend import SpendGovernor
from tests._applyfixture import ApplyFixture
from tests._fake import FakeTransport, comp, consent, m


class _PreflightSpy:
    """Capture reserve rows while keeping the real ceiling enforcement."""

    def __init__(self, gov):
        self.rows = []
        original = gov.preflight

        def spy(prompt, calls):
            self.rows.extend(calls)
            return original(prompt, calls)

        gov.preflight = spy


class PanelReserveParityTests(unittest.TestCase):
    """panel_judge reserves: votes, judge seat, fallbacks, specialist."""

    def _run(self, posts, panel=("x/a",), judge="x/j", **kw):
        with tempfile.TemporaryDirectory() as td:
            fake = FakeTransport(
                models=[m("x/a"), m("x/j"), m("deepseek/deepseek-r1:free",
                                              prompt="0", completion="0")],
                posts=list(posts))
            gov = SpendGovernor(fake, "sk-test", byok_prefixes_path=os.path.join(
                td, "byok.json"))
            spy = _PreflightSpy(gov)
            result = panel_judge(transport=fake, api_key="k", governor=gov,
                                 prompt="Q?", panel=list(panel), judge=judge,
                                 **kw)
        return result, fake, spy.rows

    def test_vote_reserve_equals_vote_call(self):
        _result, fake, rows = self._run(
            [comp('{"c1": {"real": false}}'),
             comp("prose judge")])
        payloads = fake.payloads()
        vote_tokens = payloads[0]["max_tokens"]
        self.assertEqual(vote_tokens, 4096)  # vote floor, no caller cap
        panel_rows = [c for c in rows if "panel attempt" in c[0]]
        self.assertTrue(panel_rows)
        for row in panel_rows:
            self.assertEqual(row[2], vote_tokens,
                             "vote reserve must equal the vote call's cap")

    def test_judge_seat_reserve_covers_transient_retry(self):
        _result, fake, rows = self._run(
            [comp('{"c1": {"real": false}}'),
             comp("prose judge")])
        judge_payload_tokens = fake.payloads()[1]["max_tokens"]
        judge_rows = [c for c in rows if "judge attempt" in c[0]]
        slots = _chat_reservation_slots("x/j", "auto")
        # 1 reasoning-param slot + the bounded transient retry (the loop can
        # always make this attempt on http_5xx/408/429).
        self.assertEqual(len(judge_rows), slots + 1)
        for row in judge_rows:
            self.assertEqual(row[2], judge_payload_tokens)

    def test_judge_fallback_reserve_uses_candidate_own_slots(self):
        # max_panelists=1 keeps the free reasoning candidate un-voted, so it
        # stays a fallback candidate; its reserve must carry ITS OWN
        # param-rejection slots (2 for a reasoning id under "auto"), not a
        # single generic row.
        _result, fake, rows = self._run(
            [comp('{"c1": {"real": false}}'),
             comp("prose judge"),                       # primary seat fails
             comp("fallback prose")],                   # rotation candidate
            panel=("x/a", "deepseek/deepseek-r1:free"), max_panelists=1)
        fb_rows = [c for c in rows if "judge fallback reserve" in c[0]]
        self.assertTrue(fb_rows)
        expected = _chat_reservation_slots("deepseek/deepseek-r1:free", "auto")
        self.assertEqual(expected, 2)  # sanity: reasoning id under "auto"
        candidate_rows = [r for r in fb_rows
                          if r[1] == "deepseek/deepseek-r1:free"]
        self.assertEqual(len(candidate_rows), expected)
        for row in candidate_rows:
            self.assertEqual(row[2], 8192 + 200)

    def test_specialist_reserve_equals_specialist_call(self):
        # Regression: the specialist preflight reserved judge_max_tokens
        # (8384) while the call spends spec_tokens (4096) -- overstating the
        # ceiling in exactly the lane the budget work targeted.
        _result, fake, rows = self._run(
            [comp('{"c1": {"real": false}}'),
             comp("prose, unparseable judge"),          # judge exhausted...
             comp("specialist prose, no json")],        # ...so specialist runs
            run_convergence=True)
        payloads = fake.payloads()
        self.assertEqual(len(payloads), 3)
        judge_tokens = payloads[1]["max_tokens"]
        spec_tokens = payloads[2]["max_tokens"]
        self.assertEqual(judge_tokens, 8192 + 200)   # synthesis lane
        self.assertEqual(spec_tokens, 4096)          # specialist lane
        spec_rows = [c for c in rows if "convergence attempt" in c[0]]
        self.assertTrue(spec_rows)
        for row in spec_rows:
            self.assertEqual(row[2], spec_tokens,
                             "specialist reserve must equal the specialist "
                             "call's cap, not the judge budget")
            self.assertNotEqual(row[2], judge_tokens)


class EscalationReserveParityTests(unittest.TestCase):
    """EscalationDriver: rung reserve == rung chat worst case."""

    def test_rung_reserve_equals_rung_call(self):
        from harness.apply_state import RunState
        from harness.escalation import EscalationDriver
        from harness.router import Router

        class FakeGov:
            def __init__(self):
                self.spent = 0.0
                self.max_cost = 1.0
                self.captured = []

            def preflight(self, prompt, calls):
                self.captured.append(calls)

            def record_actual(self, cost, model):
                self.spent += float(cost or 0)

            def record_byok(self, model):
                pass

            def is_free(self, model):
                return str(model).endswith(":free")

        chat_tokens = []

        def fake_chat(transport, api_key, model, messages, max_tokens,
                      effort, budget, governor):
            chat_tokens.append(max_tokens)
            return 200, {"choices": [{"message": {"content": f"from-{model}"},
                                      "finish_reason": "stop"}],
                         "usage": {"cost": 0.0}}

        router = Router(panel=["a"], judge="j", apply_model="a",
                        allow_escalation=True,
                        escalation_pool=["free/a:free", "paid/b"])
        gov = FakeGov()
        state = RunState(rounds=[], history=[], current_content="x")
        calls = []

        def finish_fn(model, content, cost):
            calls.append(model)
            if model == "paid/b":
                return {"status": "ok", "model": model}
            return None

        driver = EscalationDriver(router, transport=None, api_key="k",
                                  governor=gov, ledger=None, task_id="t1",
                                  max_tokens=4096)
        req = mock.Mock()
        req.allow_escalation = True
        with mock.patch("harness.escalation.chat", side_effect=fake_chat):
            result = driver.run_with_escalation(
                req, state, lambda st, ctx: "prompt", finish_fn)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(gov.captured)
        self.assertEqual(len(chat_tokens), len(
            [row for rung in gov.captured for row in rung]))
        for rung_rows, called_tokens in zip(gov.captured, chat_tokens):
            for row in rung_rows:
                self.assertEqual(row[2], called_tokens,
                                 "rung reserve must equal the rung call's cap")
            self.assertEqual(len(rung_rows),
                             _chat_reservation_slots(rung_rows[0][1],
                                                     driver.reasoning_effort))


class ConsentReserveParityTests(unittest.TestCase):
    """Consent probe: explicit-disable slots == the 400-retry worst case."""

    def test_probe_reserve_equals_call_worst_case(self):
        from harness.consent import probe_consent
        with tempfile.TemporaryDirectory() as td:
            fake = FakeTransport(
                models=[m("inclusionai/ling-2.6-flash"),
                        m("meta-llama/llama-3.1-8b-instruct")],
                posts=[consent("accept")])
            gov = SpendGovernor(fake, "sk-test", byok_prefixes_path=os.path.join(
                td, "byok.json"))
            spy = _PreflightSpy(gov)
            probe_consent(transport=fake, api_key="k", governor=gov,
                          task_id="pf", task="Do the work",
                          model="inclusionai/ling-2.6-flash", ledger=None,
                          fallback_pool=["meta-llama/llama-3.1-8b-instruct"])
        payloads = fake.payloads()
        self.assertTrue(payloads)
        for row in spy.rows:
            self.assertEqual(row[1], row[1])  # shape: (label, model, tokens, extra)
            self.assertEqual(row[2], payloads[0]["max_tokens"])
        # The probe runs reasoning-disabled; "none" is a reasoning parameter a
        # mandatory-reasoning route may reject, so TWO slots per candidate is
        # the exact worst case (1 slot per candidate was the old under-cover).
        self.assertEqual(len(spy.rows), 4)


class ApplyReserveParityTests(ApplyFixture):
    """Apply attempts: reserve tokens == the apply payload's max_tokens."""

    def test_apply_reserve_equals_apply_call(self):
        from tests._applyfixture import APPLY, CHANGED, scripted_run
        p = self.make_file()
        fake, gov, _ledger, engine = self.make_env(
            posts=[comp(CHANGED)], run=scripted_run([(0, "")]))
        spy = _PreflightSpy(gov)
        result = engine.apply_edit(
            task_id="t1", file_path=p, instruction="add +0",
            verify_cmd="python -m py_compile math.py",
            require_consent=False)
        self.assertEqual(result["status"], "ok")
        apply_rows = [c for c in spy.rows if "apply attempt" in c[0]]
        self.assertTrue(apply_rows)
        payload_tokens = [pl["max_tokens"] for pl in fake.payloads()
                          if pl.get("model", "").replace(":floor", "") == APPLY]
        self.assertTrue(payload_tokens)
        for row in apply_rows:
            self.assertIn(row[2], payload_tokens,
                          "apply reserve must equal an apply call's cap")


if __name__ == "__main__":
    unittest.main()
