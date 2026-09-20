"""Hermetic unit tests for AutonomousAgent and conversational orchestration (#PR-Chat-1)."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from harness.agent import (
    AutonomousAgent,
    classify_prompt_intent,
    discover_target_files,
    discover_verification_gate,
    enumerate_repo_files,
    load_chat_history,
    save_chat_turn,
)
from harness.history import get_default_history_dir
from harness.config import load_settings
from harness.errors import HarnessError, ToolCancelled
from tests._fake import FakeTransport, _gov


# The agent suite must remain hermetic on CI: production governor_for correctly
# refuses without a real key, while these tests replace the transport boundary.
_TEST_GOVERNOR = _gov(FakeTransport(), max_cost=0.05)
_TEST_GOVERNOR_PATCH = patch("harness.agent.governor_for",
                            return_value=(None, _TEST_GOVERNOR))
_TEST_GOVERNOR_PATCH.start()


def tearDownModule():
    _TEST_GOVERNOR_PATCH.stop()


def _lane_settings(**overrides):
    """Agent-lane settings with the hourglass DISARMED explicitly.

    These tests pin lane mechanics (round drive, healing retry, artifact
    truth, escalation evidence) that are independent of the plan gate, and
    the hourglass switch must come from the test -- never from whatever
    config file happens to be on the machine running the suite. The armed
    lane is covered by TestHourglassLane, which scripts the waist verdict.
    """
    settings = load_settings()
    settings.hourglass_confirm = False
    settings.hourglass_isolate = False
    settings.hourglass_parallel = False
    settings.hourglass_require_attestation = False
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


def _lane_settings(**overrides):
    """Agent-lane settings with the hourglass DISARMED explicitly.

    These tests pin lane mechanics (round drive, healing retry, artifact
    truth, escalation evidence) that are independent of the plan gate, and
    the hourglass switch must come from the test -- never from whatever
    config file happens to be on the machine running the suite. The armed
    lane is covered by TestHourglassLane, which scripts the waist verdict.
    """
    settings = load_settings()
    settings.hourglass_confirm = False
    settings.hourglass_isolate = False
    settings.hourglass_parallel = False
    settings.hourglass_require_attestation = False
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


class TestAgentClassificationAndDiscovery(unittest.TestCase):
    def test_classify_prompt_intent(self):
        # Conversational
        self.assertEqual(classify_prompt_intent("How does the router work?"), "conversation")
        self.assertEqual(classify_prompt_intent("What models are available in Tier 0"), "conversation")
        self.assertEqual(classify_prompt_intent("Explain the difference between Scout and Distiller"), "conversation")
        self.assertEqual(classify_prompt_intent("verify the claim about the riemann"), "conversation")

        # Edit / Mutation
        self.assertEqual(classify_prompt_intent("How can I refactor executor.py?"), "edit")
        self.assertEqual(classify_prompt_intent("Fix the bug in executor.py"), "edit")
        self.assertEqual(classify_prompt_intent("Implement rate limiting in session.py"), "edit")
        self.assertEqual(classify_prompt_intent("Refactor concurrency architecture"), "edit")
        self.assertEqual(classify_prompt_intent("Add tests for config.py"), "edit")
        self.assertEqual(classify_prompt_intent("Update README.md"), "edit")

        # Audit
        self.assertEqual(classify_prompt_intent("Verify chain and check ledger integrity"), "audit")
        self.assertEqual(classify_prompt_intent("Audit ledger status"), "audit")

    def test_discover_target_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "harness").mkdir()
            (root / "harness" / "executor.py").write_text("# executor", encoding="utf-8")
            (root / "harness" / "dag.py").write_text("# dag", encoding="utf-8")

            # 1. Explicit path in prompt
            targets = discover_target_files("Fix bug in harness/executor.py now", root_dir=root)
            self.assertEqual(targets, ["harness/executor.py"])

            # 2. Heuristic stem matching
            targets2 = discover_target_files("Refactor dag and optimize stages", root_dir=root)
            self.assertEqual(targets2, ["harness/dag.py"])

            # 3. No matches
            targets3 = discover_target_files("Explain quantum mechanics", root_dir=root)
            self.assertEqual(targets3, [])

    def test_discover_verification_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "harness").mkdir()
            (root / "tests").mkdir()
            (root / "harness" / "mod_a.py").write_text("# a", encoding="utf-8")
            (root / "tests" / "test_mod_a.py").write_text("# test a", encoding="utf-8")
            (root / "harness" / "mod_b.py").write_text("# b", encoding="utf-8")

            # Test file exists -- absolute path: the gate runner's CWD is
            # the server's, not the agent's chosen root.
            gate_a = discover_verification_gate(["harness/mod_a.py"], root_dir=root)
            self.assertEqual(
                gate_a,
                f"python -m unittest {root / 'tests' / 'test_mod_a.py'}")

            # Test file does not exist -> fallback to py_compile (absolute)
            gate_b = discover_verification_gate(["harness/mod_b.py"], root_dir=root)
            self.assertEqual(
                gate_b,
                f"python -m py_compile \"{root / 'harness' / 'mod_b.py'}\"")

            # No targets
            self.assertIsNone(discover_verification_gate([], root_dir=root))

    def test_chat_history_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            hdir = Path(tmp)
            turn1 = {"role": "user", "content": "Hello"}
            turn2 = {"role": "agent", "content": "Hi there!"}

            save_chat_turn("session_123", turn1, history_dir=hdir)
            save_chat_turn("session_123", turn2, history_dir=hdir)

            loaded = load_chat_history("session_123", history_dir=hdir)
            self.assertEqual(len(loaded), 2)
            self.assertEqual(loaded[0]["content"], "Hello")
            self.assertEqual(loaded[1]["content"], "Hi there!")

            # Non-existent session
            self.assertEqual(load_chat_history("unknown_session", history_dir=hdir), [])


class TestAutonomousAgent(unittest.TestCase):
    def test_empty_prompt_raises(self):
        agent = AutonomousAgent()
        with self.assertRaises(HarnessError):
            agent.run_prompt("   ")

    def test_cancellation_check(self):
        agent = AutonomousAgent()
        with self.assertRaises(ToolCancelled):
            agent.run_prompt("Explain router", cancel_check=lambda: True)

    def test_handle_conversation(self):
        with tempfile.TemporaryDirectory() as tmp:
            hdir = Path(tmp)
            agent = AutonomousAgent(history_dir=hdir)

            mock_resp = {
                "choices": [{"message": {"content": "The router routes models."}}],
                "usage": {"cost": 0.0001},
            }

            with patch("harness.agent.chat", return_value=(200, mock_resp)), \
                 patch("harness.agent.governor_for", return_value=(None, MagicMock())):
                res = agent.run_prompt("How does the router work?", session_id="s1")
                self.assertEqual(res["status"], "ok")
                self.assertEqual(res["intent"], "conversation")
                self.assertIn("The router routes models.", res["response"])
                self.assertAlmostEqual(res["cost"], 0.0001)

            # Check persisted history
            history = load_chat_history("s1", history_dir=hdir)
            self.assertEqual(len(history), 1)

    def test_handle_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            hdir = Path(tmp)
            agent = AutonomousAgent(history_dir=hdir)

            mock_ledger = MagicMock()
            mock_ledger.verify.return_value = (True, None)
            mock_ledger.chain_status.return_value = {"verified": True, "latest_seq": 42}

            with patch("harness.agent.ledger_for", return_value=mock_ledger):
                res = agent.run_prompt("Verify chain and check ledger integrity", session_id="s2")
                self.assertEqual(res["status"], "ok")
                self.assertEqual(res["intent"], "audit")
                self.assertIn("clean and verified", res["response"])
                self.assertEqual(res["cost"], 0.0)

    def test_handle_edit_preview_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "harness").mkdir()
            (root / "harness" / "calc.py").write_text("def add(a, b): return a + b\n", encoding="utf-8")

            agent = AutonomousAgent(settings=_lane_settings(), root_dir=root,
                                    history_dir=root)
            with patch.object(AutonomousAgent, "_orchestrator_chat_fn",
                              side_effect=HarnessError("hermetic test")):
                res = agent.run_prompt("Refactor harness/calc.py to add typing", auto_apply=False)

            self.assertEqual(res["status"], "preview_ready")
            self.assertEqual(res["intent"], "edit")
            self.assertIn("harness/calc.py", res["target_files"])
            self.assertIn("dag", res)

    def test_handle_edit_autonomous_execution_and_diff(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "harness").mkdir()
            calc_file = root / "harness" / "calc.py"
            calc_file.write_text("def add(a, b): return a + b\n", encoding="utf-8")

            agent = AutonomousAgent(settings=_lane_settings(), root_dir=root,
                                    history_dir=root)

            def fake_apply_edit(file_path, instruction, **kwargs):
                # Simulate modifying file and returning the candidate that Jev verifies.
                content = "def add(a: int, b: int) -> int: return a + b\n"
                (root / file_path).write_text(content, encoding="utf-8")
                return {
                    "status": "ok", "cost": 0.002, "content": content,
                    "diff": "--- a/harness/calc.py\n+++ b/harness/calc.py\n@@ -1 +1 @@\n-def add(a, b): return a + b\n+def add(a: int, b: int) -> int: return a + b\n",
                }

            mock_engine = MagicMock()
            mock_engine.apply_edit.side_effect = fake_apply_edit
            from harness.jev import JevEvaluator
            jev = MagicMock(wraps=JevEvaluator())

            with patch("harness.agent.apply_session", return_value=mock_engine), \
                 patch("harness.agent.jev_for", return_value=jev), \
                 patch.object(AutonomousAgent, "_orchestrator_chat_fn",
                              side_effect=HarnessError("hermetic test")):
                res = agent.run_prompt("Update harness/calc.py with type annotations", auto_apply=True)
                self.assertEqual(res["status"], "ok")
                self.assertEqual(res["intent"], "edit")
                self.assertIn("Orchestrator complete after 1 round(s)", res["response"])
                self.assertEqual(res["orchestrator_rounds"], 1)
                self.assertIn("harness/calc.py", res["target_files"])
                self.assertIn("+def add(a: int, b: int) -> int:", res["diff"])
                self.assertAlmostEqual(res["cost"], 0.002)
                jev.verify_diff_mechanics.assert_called_once()
                self.assertEqual(jev.verify_diff_mechanics.call_args.kwargs["candidate"],
                                 "def add(a: int, b: int) -> int: return a + b\n")

    def test_handle_edit_self_healing_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "harness").mkdir()
            calc_file = root / "harness" / "calc.py"
            calc_file.write_text("def add(a, b): return a + b\n", encoding="utf-8")

            agent = AutonomousAgent(settings=_lane_settings(), root_dir=root,
                                    history_dir=root)

            # First attempt fails verification gate, second attempt succeeds
            attempts = [
                {"status": "verify_failed", "error": "AssertionError: 3 != 4", "cost": 0.001},
                {"status": "ok", "cost": 0.001},
            ]
            mock_engine = MagicMock()
            mock_engine.apply_edit.side_effect = attempts

            with patch("harness.agent.apply_session", return_value=mock_engine), \
                 patch.object(AutonomousAgent, "_orchestrator_chat_fn",
                              side_effect=HarnessError("hermetic test")):
                res = agent.run_prompt("Fix calculation bug in harness/calc.py", auto_apply=True)
                self.assertEqual(res["status"], "ok")
                self.assertEqual(mock_engine.apply_edit.call_count, 2)
                self.assertAlmostEqual(res["cost"], 0.002)

    def test_edge_cases_and_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with patch.dict(os.environ, {"HARNESS_CONFIG_DIR": str(tmp_path)}):
                d = get_default_history_dir()
                self.assertTrue(d.exists())

            # discover_verification_gate with unknown file returns None
            self.assertIsNone(discover_verification_gate("unknown_file.xyz", root_dir=tmp_path))

            # classify_prompt_intent fallback to conversation
            intent = classify_prompt_intent("simple statement without punctuation")
            self.assertEqual(intent, "conversation")

            # corrupt line in chat history JSONL
            hist_file = tmp_path / "corrupt-test.jsonl"
            hist_file.write_text('{"prompt": "valid"}\ncorrupt json line\n', encoding="utf-8")
            loaded = load_chat_history("corrupt-test", history_dir=tmp_path)
            self.assertEqual(len(loaded), 1)

            # audit failure path
            agent = AutonomousAgent(history_dir=tmp_path)
            mock_ledger = MagicMock()
            mock_ledger.verify.return_value = (False, 42)
            mock_ledger.chain_status.return_value = {"latest_seq": 41}
            with patch("harness.agent.ledger_for", return_value=mock_ledger):
                res = agent.run_prompt("audit ledger status")
                self.assertEqual(res["status"], "audit_failed")
                self.assertIn("failed at record sequence 42", res["response"])

    def test_conversation_multi_turn_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            save_chat_turn("sess_1", {"prompt": "what is 2+2?", "response": "2+2 is 4"}, history_dir=tmp_path)
            agent = AutonomousAgent(history_dir=tmp_path)
            with patch("harness.agent.chat") as mock_chat, \
                 patch("harness.agent.governor_for", return_value=(None, MagicMock())):
                mock_chat.return_value = (200, {"choices": [{"message": {"content": "It is 4"}}], "usage": {"cost": 0.0}})
                res = agent.run_prompt("and what is that plus 2?", session_id="sess_1")
                self.assertEqual(res["status"], "ok")
                chat_args = mock_chat.call_args[1]
                messages = chat_args["messages"]
                self.assertEqual(len(messages), 4)
                self.assertEqual(messages[1]["content"], "what is 2+2?")
                self.assertEqual(messages[2]["content"], "2+2 is 4")
                self.assertEqual(messages[3]["content"], "and what is that plus 2?")

    def test_force_conversation_bypasses_classifier(self):
        """UI chat path: force_conversation=True always routes to _handle_conversation,
        even for prompts that look like edit requests (e.g. contain 'fix', 'verify'),
        ensuring session history is always loaded."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            save_chat_turn("ui_sess", {"prompt": "what is 2+2?", "response": "4"}, history_dir=tmp_path)
            agent = AutonomousAgent(history_dir=tmp_path)
            with patch("harness.agent.chat") as mock_chat, \
                 patch("harness.agent.governor_for", return_value=(None, MagicMock())):
                mock_chat.return_value = (200, {
                    "choices": [{"message": {"content": "Verified: still 4"}}],
                    "usage": {"cost": 0.0},
                })
                # "verify this" would normally classify as 'edit' â€” but force_conversation overrides
                res = agent.run_prompt("verify this", session_id="ui_sess", force_conversation=True)
                self.assertEqual(res["status"], "ok")
                self.assertEqual(res["intent"], "conversation")
                chat_args = mock_chat.call_args[1]
                messages = chat_args["messages"]
                # system + prior user + prior assistant + current user = 4
                self.assertEqual(len(messages), 4)
                self.assertEqual(messages[1]["content"], "what is 2+2?")
                self.assertEqual(messages[2]["content"], "4")
                self.assertEqual(messages[3]["content"], "verify this")


class TestWebCapabilityDisclosure(unittest.TestCase):
    """The Riemann incident: the chat lane must never roleplay a search it
    cannot perform. Web evidence is attached only when the run opts in, and
    every source (or failure) is disclosed to the model and in provenance."""

    def _agent(self, hdir):
        return AutonomousAgent(history_dir=hdir)

    @staticmethod
    def _mock_resp(content="answer"):
        return {"choices": [{"message": {"content": content}}],
                "usage": {"cost": 0.0}}

    def test_no_web_flag_discloses_no_web_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(Path(tmp))
            with patch("harness.agent.chat", return_value=(200, self._mock_resp())) as m, \
                 patch("harness.agent.governor_for", return_value=(None, MagicMock())):
                agent.run_prompt("can you search to verify?", session_id="w1")
                sysmsg = m.call_args[1]["messages"][0]["content"]
        self.assertIn("NO web tools", sysmsg)
        self.assertIn("NO internet access", sysmsg)

    def test_all_fetches_failed_falls_back_to_search(self):
        # The fetch-failed incident: a URL-prompt turn where the fetch died
        # left the model with ONLY the failure note. Now a search follows so
        # the turn still carries evidence, with the FAILED fetch kept honest.
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(Path(tmp))
            with patch("harness.web.fetch_url",
                       side_effect=HarnessError("web fetch HTTP 503")), \
                 patch("harness.web.search_web",
                       return_value=[{"url": "https://www.anthropic.com/rz",
                                      "title": "Riemann",
                                      "snippet": "bound moved to 67.2%"}]), \
                 patch("harness.agent.chat",
                       return_value=(200, self._mock_resp())) as m, \
                 patch("harness.agent.governor_for",
                       return_value=(None, MagicMock())):
                res = agent.run_prompt(
                    "https://www.anthropic.com/research/riemann-zeta",
                    session_id="w2", web=True)
                sysmsg = m.call_args[1]["messages"][0]["content"]
        self.assertEqual(res["status"], "ok")
        self.assertIn("SOURCE (fetch) FAILED", sysmsg)
        self.assertIn("web fetch HTTP 503", sysmsg)
        self.assertIn("SOURCE (search): Riemann", sysmsg)
        self.assertIn("bound moved to 67.2%", sysmsg)

    def test_web_on_system_prompt_states_capability_not_denial(self):
        # The "it says web is on..." incident: the base prompt claimed
        # "You have NO internet access" even on web-enabled runs, so the
        # model denied the attached tools. Web-on prompts must state what
        # is attached (search + allowlist hosts) and never deny access.
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(Path(tmp))
            with patch("harness.web.search_web", return_value=[]), \
                 patch("harness.agent.chat",
                       side_effect=HarnessError("stop at the system prompt")) as mc, \
                 patch("harness.agent.governor_for",
                       return_value=(None, MagicMock())):
                with self.assertRaises(HarnessError):
                    agent.run_prompt("can you access google.com?",
                                     session_id="w1b", web=True)
                sysmsg = mc.call_args[1]["messages"][0]["content"]
        self.assertNotIn("NO internet access", sysmsg)
        self.assertIn("Web tools ARE attached", sysmsg)
        self.assertIn("www.anthropic.com", sysmsg)

    def test_web_true_search_success_attaches_sources_and_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(Path(tmp))
            fake_results = [{"title": "Bound moved", "url": "https://e.example/a",
                             "snippet": "zero-free region extended"}]
            with patch("harness.web.search_web", return_value=fake_results) as ms, \
                 patch("harness.agent.chat", return_value=(200, self._mock_resp())) as mc, \
                 patch("harness.agent.governor_for", return_value=(None, MagicMock())):
                res = agent.run_prompt("search for the riemann bound", session_id="w2", web=True)
                ms.assert_called_once()
                sysmsg = mc.call_args[1]["messages"][0]["content"]
                self.assertIn("Web tool results for this turn", sysmsg)
                self.assertIn("zero-free region extended", sysmsg)
                self.assertTrue(res["web_used"])
                self.assertEqual(res["web_sources"][0]["url"], "https://e.example/a")

    def test_web_true_url_prompt_fetches_allowlisted_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(Path(tmp))
            page = {"url": "https://www.anthropic.com/r", "title": "T", "text": "page body"}
            with patch("harness.web.fetch_url", return_value=page) as mf, \
                 patch("harness.agent.chat", return_value=(200, self._mock_resp())) as mc, \
                 patch("harness.agent.governor_for", return_value=(None, MagicMock())):
                agent.run_prompt("https://www.anthropic.com/r", session_id="w3", web=True)
                mf.assert_called_once()
                sysmsg = mc.call_args[1]["messages"][0]["content"]
        self.assertIn("page body", sysmsg)

    def test_web_failure_is_disclosed_not_hidden(self):
        from harness.errors import HarnessError as _HE
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(Path(tmp))
            with patch("harness.web.search_web", side_effect=_HE("web search failed: down")), \
                 patch("harness.agent.chat", return_value=(200, self._mock_resp())) as mc, \
                 patch("harness.agent.governor_for", return_value=(None, MagicMock())):
                res = agent.run_prompt("search the web", session_id="w4", web=True)
                sysmsg = mc.call_args[1]["messages"][0]["content"]
                self.assertIn("FAILED", sysmsg)
                self.assertIn("web search failed: down", sysmsg)
                self.assertFalse(res["web_used"])

    def test_web_never_kills_the_chat_lane(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(Path(tmp))
            with patch("harness.web.search_web", side_effect=RuntimeError("boom")), \
                 patch("harness.agent.chat", return_value=(200, self._mock_resp())) as mc, \
                 patch("harness.agent.governor_for", return_value=(None, MagicMock())):
                res = agent.run_prompt("search the web", session_id="w5", web=True)
        self.assertEqual(res["status"], "ok")
        self.assertIn("web tools error", mc.call_args[1]["messages"][0]["content"])

    def test_web_fetch_failure_is_disclosed(self):
        # A URL prompt whose fetch is refused: the refusal lands in the
        # model's context as a FAILED source -- never silently dropped, and
        # still there alongside the fallback search results.
        from harness.errors import HarnessError as _HE
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(Path(tmp))
            with patch("harness.web.find_urls",
                       return_value=["https://www.anthropic.com/x"]), \
                 patch("harness.web.fetch_url",
                       side_effect=_HE("web fetch refused: host not allowed")), \
                 patch("harness.web.search_web",
                       return_value=[{"url": "https://www.anthropic.com/x",
                                      "title": "X", "snippet": "s"}]), \
                 patch("harness.agent.chat", return_value=(200, self._mock_resp())) as mc, \
                 patch("harness.agent.governor_for", return_value=(None, MagicMock())):
                res = agent.run_prompt("https://www.anthropic.com/x",
                                       session_id="w6", web=True)
                sysmsg = mc.call_args[1]["messages"][0]["content"]
        self.assertIn("FAILED", sysmsg)
        self.assertIn("web fetch refused", sysmsg)
        self.assertIn("SOURCE (search): X", sysmsg)
        self.assertTrue(res["web_used"])

    def test_cancel_during_web_gather_raises(self):
        # Cancellation observed at the web-gather checkpoint specifically:
        # the flag flips False -> True after the outer (force_conversation)
        # checkpoint has already passed, so only the in-web branch fires.
        calls = {"n": 0}

        def flip_late():
            calls["n"] += 1
            return calls["n"] > 1

        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(Path(tmp))
            with patch("harness.agent.governor_for", return_value=(None, MagicMock())):
                with self.assertRaises(ToolCancelled):
                    agent.run_prompt("search stuff", session_id="w7", web=True,
                                     cancel_check=flip_late)


class TestChatLadderRotation(unittest.TestCase):
    """The 429 incident: the chat lane pinned one free model, ignored the
    provider status, and rendered a fake "I was unable to formulate a
    response." It must rotate (free panel, then paid rungs when escalation
    is allowed), bill through the governor per attempt, and raise honestly
    when every rung fails."""

    _OK = {"choices": [{"message": {"content": "fallback answer"}}],
           "usage": {"cost": 0.0}}
    _429 = {"error": {"message": "rate-limited upstream", "code": 429}}

    def _agent(self, hdir, **overrides):
        from harness.config import load_settings
        return AutonomousAgent(settings=load_settings(overrides),
                               history_dir=hdir)

    def test_rotates_to_next_pool_model_on_429(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(Path(tmp), judge="m1:free",
                                panel_pool="m2:free,m3:free",
                                allow_escalation=False)
            with patch("harness.agent.chat",
                       side_effect=[(429, self._429), (200, self._OK)]) as mc, \
                 patch("harness.agent.governor_for",
                       return_value=(None, MagicMock())):
                res = agent.run_prompt("hello", session_id="r1")
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["model"], "m2:free")
        self.assertEqual(res["response"], "fallback answer")
        self.assertEqual(mc.call_args[1]["model"], "m2:free")

    def test_escalates_to_paid_rung_when_free_exhausted(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(Path(tmp), judge="m1:free",
                                panel_pool="m2:free",
                                allow_escalation=True,
                                escalation_pool="paid-1,paid-2")
            paid = {"choices": [{"message": {"content": "paid answer"}}],
                    "usage": {"cost": 0.01}}
            with patch("harness.agent.chat",
                       side_effect=[(429, self._429), (429, self._429),
                                    (200, paid)]), \
                 patch("harness.agent.governor_for",
                       return_value=(None, MagicMock())) as mg:
                res = agent.run_prompt("hello", session_id="r2")
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["model"], "paid-1")
        self.assertEqual(res["response"], "paid answer")
        gov = mg.return_value[1]
        gov.record_actual.assert_called_once_with(0.01, "paid-1")

    def test_raises_honestly_when_every_rung_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(Path(tmp), judge="m1:free",
                                panel_pool="m2:free", allow_escalation=True,
                                escalation_pool="paid-1")
            with patch("harness.agent.chat",
                       return_value=(429, self._429)), \
                 patch("harness.agent.governor_for",
                       return_value=(None, MagicMock())):
                with self.assertRaises(HarnessError) as ctx:
                    agent.run_prompt("hello", session_id="r3")
        self.assertIn("HTTP 429", str(ctx.exception))
        self.assertIn("m1:free", str(ctx.exception))
        # no fake apology turn is persisted for the failed walk
        self.assertEqual(load_chat_history("r3", Path(tmp)), [])

    def test_empty_body_rotates(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(Path(tmp), judge="m1:free",
                                panel_pool="m2:free", allow_escalation=False)
            empty = {"choices": [{"message": {"content": "   "}}],
                     "usage": {"cost": 0.0}}
            with patch("harness.agent.chat",
                       side_effect=[(200, empty), (200, self._OK)]) as mc, \
                 patch("harness.agent.governor_for",
                       return_value=(None, MagicMock())):
                res = agent.run_prompt("hello", session_id="r4")
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["model"], "m2:free")
        self.assertEqual(mc.call_count, 2)

    def test_preflight_refusal_rotates(self):
        # A governor refusal on one rung (ceiling/saturation) is an attempt
        # failure, not a lane crash: the walk advances and records the note.
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(Path(tmp), judge="m1:free",
                                panel_pool="m2:free", allow_escalation=False)
            gov = MagicMock()
            gov.preflight.side_effect = [HarnessError("ceiling refused"),
                                         (0.0, [])]
            with patch("harness.agent.chat",
                       return_value=(200, self._OK)) as mc, \
                 patch("harness.agent.governor_for",
                       return_value=(None, gov)):
                res = agent.run_prompt("hello", session_id="r5")
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["model"], "m2:free")
        self.assertEqual(mc.call_count, 1)

    def test_byok_routed_response_rotates(self):
        # A BYOK-routed response carries untracked spend: refused as an
        # answer source, recorded, and the walk advances.
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(Path(tmp), judge="m1:free",
                                panel_pool="m2:free", allow_escalation=False)
            byok = {"choices": [{"message": {"content": "off-the-books"}}],
                    "usage": {"cost": 0.0, "is_byok": True}}
            with patch("harness.agent.chat",
                       side_effect=[(200, byok), (200, self._OK)]), \
                 patch("harness.agent.governor_for",
                       return_value=(None, MagicMock())) as mg:
                res = agent.run_prompt("hello", session_id="r6")
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["model"], "m2:free")
        self.assertNotEqual(res["response"], "off-the-books")
        mg.return_value[1].record_byok.assert_called_once_with("m1:free")

    def test_mid_walk_cancellation_raises(self):
        # The cancellation checkpoint inside the ladder walk: the flag flips
        # after the first rung's attempt, before the second rung starts.
        calls = {"n": 0}

        def flip_after_first():
            calls["n"] += 1
            return calls["n"] > 2

        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(Path(tmp), judge="m1:free",
                                panel_pool="m2:free", allow_escalation=False)
            with patch("harness.agent.chat",
                       side_effect=[(429, self._429), (200, self._OK)]), \
                 patch("harness.agent.governor_for",
                       return_value=(None, MagicMock())):
                with self.assertRaises(ToolCancelled):
                    agent.run_prompt("hello", session_id="r7",
                                     cancel_check=flip_after_first)


class TestChatDeferral(unittest.TestCase):
    """The chat lane honors the same deferral contract as apply: a
    HARNESS_DEFER marker is the model handing the decision back with a reason
    instead of guessing -- recorded in the ledger, returned as a deferred
    result with the plan-lane resume path, never a bare "I can't"."""

    _DEFER = {"choices": [{"message": {"content":
        "That is a multi-step repo refactor, larger than this lane can "
        "honestly do.\n\nHARNESS_DEFER: multi-step repo work exceeds the "
        "conversation lane"}}], "usage": {"cost": 0.0}}

    def test_defer_marker_becomes_deferred_result_with_resume_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = AutonomousAgent(settings=_lane_settings(),
                                    history_dir=Path(tmp))
            fake_ledger = MagicMock()
            with patch("harness.agent.chat", return_value=(200, self._DEFER)), \
                 patch("harness.agent.governor_for",
                       return_value=(None, MagicMock())), \
                 patch("harness.agent.ledger_for", return_value=fake_ledger), \
                 patch.object(AutonomousAgent, "_auto_escalation_armed",
                              return_value=False):
                res = agent.run_prompt("refactor the whole engine",
                                       session_id="d1", auto_apply=False,
                                       force_conversation=True)
            # the deferred turn is persisted like any completed turn (read
            # while the temp history dir still exists)
            turns = load_chat_history("d1", Path(tmp))
        self.assertEqual(res["status"], "deferred")
        self.assertEqual(res["defer_reason"],
                         "multi-step repo work exceeds the conversation lane")
        self.assertIn("plan lane", res["next_step"])
        # the prose before the marker is kept; the marker line is stripped
        self.assertIn("multi-step repo refactor", res["response"])
        self.assertNotIn("HARNESS_DEFER:", res["response"])
        fake_ledger.append.assert_called_once()
        args, kwargs = fake_ledger.append.call_args
        self.assertEqual(args[0], "model_result")
        self.assertEqual(kwargs["status"], "deferred")
        self.assertEqual(kwargs["category"], "capability")
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["status"], "deferred")

    def test_bare_marker_defers_with_default_reason(self):
        bare = {"choices": [{"message": {"content": "HARNESS_DEFER:"}}],
                "usage": {"cost": 0.0}}
        with tempfile.TemporaryDirectory() as tmp:
            agent = AutonomousAgent(settings=_lane_settings(),
                                    history_dir=Path(tmp))
            with patch("harness.agent.chat", return_value=(200, bare)), \
                 patch("harness.agent.governor_for",
                       return_value=(None, MagicMock())), \
                 patch("harness.agent.ledger_for", return_value=MagicMock()), \
                 patch.object(AutonomousAgent, "_auto_escalation_armed",
                              return_value=False):
                res = agent.run_prompt("do everything", session_id="d2",
                                       force_conversation=True)
        self.assertEqual(res["status"], "deferred")
        self.assertEqual(res["defer_reason"],
                         "request exceeds the conversation lane's capability")
        # a bare marker still saves visible text in the turn
        self.assertEqual(res["response"],
                         "Deferred: request exceeds the conversation "
                         "lane's capability")

    def test_system_prompt_teaches_the_deferral_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = AutonomousAgent(settings=_lane_settings(),
                                    history_dir=Path(tmp))
            with patch("harness.agent.chat",
                       side_effect=HarnessError("stop at the prompt")) as mc, \
                 patch("harness.agent.governor_for",
                       return_value=(None, MagicMock())):
                with self.assertRaises(HarnessError):
                    agent.run_prompt("hello", session_id="d3")
                sysmsg = mc.call_args[1]["messages"][0]["content"]
        self.assertIn("HARNESS_DEFER:", sysmsg)
        self.assertIn("first-class", sysmsg)

    def test_prose_refusal_without_evidence_becomes_deferral(self):
        # Free models usually defer in prose: "I can't do that." With no
        # successful tool evidence, that IS a deferral -- wrap it in the
        # honest envelope with the model's own words as the reason.
        refuse = {"choices": [{"message": {"content":
            "I can't do that. That task requires repository access and a "
            "compute cluster that this conversation does not have."}}],
            "usage": {"cost": 0.0}}
        with tempfile.TemporaryDirectory() as tmp:
            agent = AutonomousAgent(settings=_lane_settings(),
                                    history_dir=Path(tmp))
            fake_ledger = MagicMock()
            with patch("harness.agent.chat", return_value=(200, refuse)), \
                 patch("harness.agent.governor_for",
                       return_value=(None, MagicMock())), \
                 patch("harness.agent.ledger_for", return_value=fake_ledger), \
                 patch.object(AutonomousAgent, "_auto_escalation_armed",
                              return_value=False):
                res = agent.run_prompt("do the impossible", session_id="d4",
                                       force_conversation=True)
        self.assertEqual(res["status"], "deferred")
        self.assertTrue(res["defer_reason"].lower().startswith("i can't"))
        self.assertIn("reframe within this lane", res["next_step"])
        fake_ledger.append.assert_called_once()

    def test_refusal_caveat_inside_successful_web_turn_stays_ok(self):
        # A turn that DID retrieve evidence is not a deferral even when its
        # prose contains a limitation caveat.
        caveat = {"choices": [{"message": {"content":
            "I cannot verify the live page right now, but the search results "
            "show the bound moved to 67.2%."}}],
            "usage": {"cost": 0.0}}
        with tempfile.TemporaryDirectory() as tmp:
            agent = AutonomousAgent(settings=_lane_settings(),
                                    history_dir=Path(tmp))
            fake_ledger = MagicMock()
            with patch("harness.agent.chat", return_value=(200, caveat)), \
                 patch("harness.agent.governor_for",
                       return_value=(None, MagicMock())), \
                 patch("harness.agent.ledger_for", return_value=fake_ledger), \
                 patch("harness.web.search_web",
                       return_value=[{"url": "https://www.anthropic.com/rz",
                                      "title": "Riemann", "snippet": "67.2%"}]):
                res = agent.run_prompt("verify the claim", session_id="d5",
                                       web=True, force_conversation=True)
        self.assertEqual(res["status"], "ok")
        fake_ledger.append.assert_not_called()


class TestChatTruncation(unittest.TestCase):
    """The "hitting a limit" incident: long-generation turns came back cut
    mid-body (providers do not always report finish_reason "length"), were
    saved as status=ok, and neither rotated nor deferred -- the user hit the
    cap every turn with no escalation and no honest handoff."""

    def _truncated(self, content):
        return {"choices": [{"message": {"content": content},
                             "finish_reason": None}],
                "usage": {"cost": 0.0}}

    _COMPLETE = {"choices": [{"message": {"content": "concise full answer"},
                              "finish_reason": "stop"}],
                 "usage": {"cost": 0.0}}

    def test_cut_body_rotates_even_when_provider_hides_length(self):
        # Rung 1 returns a body with an unbalanced code fence (cut mid-file)
        # and finish_reason=null; the walk must treat it as unusable and
        # rotate to rung 2.
        cut = "```lean\ndef S_alpha (x : Real) : Real :="
        with tempfile.TemporaryDirectory() as tmp:
            agent = AutonomousAgent(settings=_lane_settings(),
                                    history_dir=Path(tmp))
            with patch("harness.agent.chat",
                       side_effect=[(200, self._truncated(cut)),
                                    (200, self._COMPLETE)]) as mc, \
                 patch("harness.agent.governor_for",
                       return_value=(None, MagicMock())):
                res = agent.run_prompt("write the file", session_id="t1",
                                       auto_apply=False,
                                       force_conversation=True)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["response"], "concise full answer")
        self.assertEqual(mc.call_count, 2)

    def test_every_rung_truncated_defers_with_content_kept(self):
        # All rungs cut: an honest deferral that KEEPS the longest cut-off
        # body, records the ledger entry, and hands back a continuation path
        # -- never a mangled status=ok turn and never a bare error.
        cut1 = "```lean\ndef a := 1"
        cut2 = "```lean\ndef a := 1\ndef b := 2"
        with tempfile.TemporaryDirectory() as tmp:
            agent = AutonomousAgent(settings=_lane_settings(),
                                    history_dir=Path(tmp))
            fake_ledger = MagicMock()
            with patch("harness.agent.chat",
                       side_effect=[(200, self._truncated(cut1))]
                                   + [(200, self._truncated(cut2))] * 20), \
                 patch("harness.agent.governor_for",
                       return_value=(None, MagicMock())), \
                 patch("harness.agent.ledger_for", return_value=fake_ledger):
                res = agent.run_prompt("continue until complete",
                                       session_id="t2",
                                       force_conversation=True)
        self.assertEqual(res["status"], "deferred")
        self.assertIn("token cap", res["defer_reason"])
        self.assertEqual(res["response"], cut2)
        self.assertIn("continue", res["next_step"])
        self.assertIn("plan lane", res["next_step"])
        fake_ledger.append.assert_called_once()
        kwargs = fake_ledger.append.call_args[1]
        self.assertEqual(kwargs["status"], "deferred")

    def test_truncation_defer_persists_the_turn(self):
        cut = "```lean\ndef a := 1"
        with tempfile.TemporaryDirectory() as tmp:
            agent = AutonomousAgent(settings=_lane_settings(),
                                    history_dir=Path(tmp))
            with patch("harness.agent.chat",
                       return_value=(200, self._truncated(cut))), \
                 patch("harness.agent.governor_for",
                       return_value=(None, MagicMock())), \
                 patch("harness.agent.ledger_for", return_value=MagicMock()):
                agent.run_prompt("continue", session_id="t3",
                                 force_conversation=True)
            turns = load_chat_history("t3", Path(tmp))
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["status"], "deferred")

    def test_system_prompt_bans_tool_call_markup(self):
        # The fake "<tool_call>web_search" incident: the model roleplayed a
        # tool syntax it does not have and the raw markup reached the chat.
        with tempfile.TemporaryDirectory() as tmp:
            agent = AutonomousAgent(settings=_lane_settings(),
                                    history_dir=Path(tmp))
            with patch("harness.agent.chat",
                       side_effect=HarnessError("stop at the prompt")) as mc, \
                 patch("harness.agent.governor_for",
                       return_value=(None, MagicMock())):
                with self.assertRaises(HarnessError):
                    agent.run_prompt("hello", session_id="t4")
                sysmsg = mc.call_args[1]["messages"][0]["content"]
        self.assertIn("never emit <tool_call>", sysmsg)


class TestChatAutoEscalation(unittest.TestCase):
    """A capability defer with the paid key armed does not stop at a handoff
    note: the request routes itself into the hourglass plan lane
    (frontier-planned DAG, governed apply with paid escalation rungs)."""

    _DEFER = {"choices": [{"message": {"content":
        "Too big for this lane.\n\nHARNESS_DEFER: exceeds the lane"}}],
        "usage": {"cost": 0.0}}

    def _defer_agent(self, tmp):
        agent = AutonomousAgent(settings=_lane_settings(),
                                history_dir=Path(tmp))
        return agent

    def test_capability_defer_auto_escalates_into_plan_lane(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._defer_agent(tmp)
            plan_result = {"status": "ok", "intent": "edit",
                           "response": "done", "cost": 0.01}
            with patch("harness.agent.chat", return_value=(200, self._DEFER)), \
                 patch("harness.agent.governor_for",
                       return_value=(None, MagicMock())), \
                 patch("harness.agent.ledger_for", return_value=MagicMock()), \
                 patch.object(AutonomousAgent, "_auto_escalation_armed",
                              return_value=True), \
                 patch.object(AutonomousAgent, "_handle_edit",
                              return_value=plan_result) as he:
                res = agent.run_prompt("refactor the whole engine",
                                       session_id="e1", auto_apply=False,
                                       force_conversation=True)
        self.assertEqual(res, plan_result)
        kwargs = he.call_args[1]
        self.assertTrue(kwargs["auto_apply"])
        self.assertEqual(kwargs["escalation_note"], "exceeds the lane")

    def test_escalation_note_without_a_rung_walk_is_never_stamped(self):
        # A handoff note is NOT evidence. The stubbed plan has no executable
        # nodes and no escalation rung can have run, so the old label
        # ("escalated" on a run that touched no rung) must not appear -- in
        # the result, the response, or the persisted turn.
        with tempfile.TemporaryDirectory() as tmp:
            agent = AutonomousAgent(settings=_lane_settings(),
                                    history_dir=Path(tmp), root_dir=Path(tmp))
            mock_engine = MagicMock()
            mock_engine.apply_edit.return_value = {"status": "ok", "cost": 0.0}
            with patch("harness.agent.apply_session", return_value=mock_engine), \
                 patch("harness.agent.discover_target_files",
                       return_value=["util.py"]), \
                 patch("harness.waist.plan_task",
                       return_value={"dag": {"nodes": []}, "nodes": [],
                                     "total_nodes": 0,
                                     "total_cost_ceiling": 0.0}), \
                 patch("harness.agent.discover_verification_gate",
                       return_value="echo ok"), \
                 patch.object(AutonomousAgent, "_orchestrator_chat_fn",
                              side_effect=HarnessError("hermetic test")):
                res = agent._handle_edit("refactor util.py", "e5", True,
                                         escalation_note="exceeds the lane")
            turns = load_chat_history("e5", Path(tmp))
        # the stubbed plan has no executable nodes: the orchestrator stops
        # honestly instead of claiming a completion it cannot see
        self.assertEqual(res["status"], "failed")
        self.assertIn("planner produced no executable nodes",
                      res["remaining_scope"])
        self.assertNotIn("escalated_from_defer", res)
        self.assertNotIn("escalated_model", res)
        self.assertIn("no escalation rung actually ran", res["response"])
        self.assertNotIn("escalated_from_defer", turns[0])

    def _escalation_agent(self, root):
        (root / "harness").mkdir(exist_ok=True)
        (root / "harness" / "calc.py").write_text(
            "def add(a, b): return a + b\n", encoding="utf-8")
        return AutonomousAgent(settings=_lane_settings(), root_dir=root,
                               history_dir=root)

    def _run_escalated_node(self, node_result, session_id):
        """Drive one node whose engine result claims the given escalation."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent = self._escalation_agent(root)

            def fake_apply_edit(file_path, instruction, **kwargs):
                (root / file_path).write_text(
                    "def add(a: int, b: int) -> int: return a + b\n",
                    encoding="utf-8")
                return dict(node_result)

            mock_engine = MagicMock()
            mock_engine.apply_edit.side_effect = fake_apply_edit
            with patch("harness.agent.apply_session",
                       return_value=mock_engine), \
                 patch.object(AutonomousAgent, "_orchestrator_chat_fn",
                              side_effect=HarnessError("hermetic test")):
                res = agent._handle_edit(
                    "Update harness/calc.py with type annotations",
                    session_id, True, escalation_note="exceeds the lane")
            turns = load_chat_history(session_id, root)
        return res, turns

    def test_node_apply_kwargs_carry_escalation_and_verifier(self):
        # Requirement: an agent-lane node must carry the escalation arming and
        # the verifier identity explicitly (not inherit them silently), so its
        # escalation path is auditable from the lane. session.attest_model_for
        # is the ONE owner of the verifier identity, shared with the Router.
        from harness.session import attest_model_for

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent = self._escalation_agent(root)
            seen = {}

            def fake_apply_edit(file_path, instruction, **kwargs):
                seen.update(kwargs)
                return {"status": "ok", "cost": 0.0}

            mock_engine = MagicMock()
            mock_engine.apply_edit.side_effect = fake_apply_edit
            with patch("harness.agent.apply_session",
                       return_value=mock_engine), \
                 patch.object(AutonomousAgent, "_orchestrator_chat_fn",
                              side_effect=HarnessError("hermetic test")):
                agent._handle_edit(
                    "Update harness/calc.py with type annotations",
                    "kw1", True)
        self.assertEqual(seen.get("allow_escalation"),
                         agent.settings.allow_escalation)
        self.assertEqual(seen.get("attest_model"),
                         attest_model_for(agent.settings))

    def test_escalation_stamp_requires_a_cross_family_rung_walk(self):
        # The label may only appear when a gate-passed escalation actually ran
        # a rung in a DIFFERENT family -- and the envelope must carry the model
        # id and pool family so a reader can verify the claim.
        res, turns = self._run_escalated_node({
            "status": "ok", "cost": 0.002, "escalated": True,
            "escalated_from": "ling-3.0-flash-fin:free",
            "escalated_to": "z-ai/glm-5.3-flash",
            "escalation_rungs": ["ling-3.0-flash-fin:free",
                                 "z-ai/glm-5.3-flash"],
        }, "esc1")
        self.assertEqual(res["escalated_from_defer"], "exceeds the lane")
        self.assertEqual(res["escalated_model"], "z-ai/glm-5.3-flash")
        self.assertEqual(res["escalated_from_model"],
                         "ling-3.0-flash-fin:free")
        self.assertEqual(res["escalation_family"], "z-ai")
        self.assertIn("escalation rung ran: free -> z-ai", res["response"])
        self.assertEqual(turns[0]["escalated_from_defer"],
                         "exceeds the lane")
        self.assertEqual(turns[0]["escalation_family"], "z-ai")

    def test_escalation_stamp_refused_when_the_rung_stays_free(self):
        # A free rung rotated to another free rung is a rung walk, but the
        # family never changed: reporting it as escalated is the exact defect
        # the operator hit ("said escalated, ran ling/gemma 100% of the time").
        res, turns = self._run_escalated_node({
            "status": "ok", "cost": 0.001, "escalated": True,
            "escalated_from": "ling-3.0-flash-fin:free",
            "escalated_to": "nvidia/nemotron-3-super-120b-a12b:free",
            "escalation_rungs": ["ling-3.0-flash-fin:free",
                                 "nvidia/nemotron-3-super-120b-a12b:free"],
        }, "esc2")
        self.assertNotIn("escalated_from_defer", res)
        self.assertNotIn("escalated_model", res)
        self.assertNotIn("escalation_family", res)
        self.assertIn("no escalation rung actually ran", res["response"])
        self.assertNotIn("escalated_from_defer", turns[0])

    def test_escalation_failure_falls_back_to_honest_defer(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._defer_agent(tmp)
            with patch("harness.agent.chat", return_value=(200, self._DEFER)), \
                 patch("harness.agent.governor_for",
                       return_value=(None, MagicMock())), \
                 patch("harness.agent.ledger_for", return_value=MagicMock()), \
                 patch.object(AutonomousAgent, "_auto_escalation_armed",
                              return_value=True), \
                 patch.object(AutonomousAgent, "_handle_edit",
                              side_effect=HarnessError("no target files")):
                res = agent.run_prompt("refactor the whole engine",
                                       session_id="e2", auto_apply=False,
                                       force_conversation=True)
        self.assertEqual(res["status"], "deferred")
        self.assertIn("auto-escalation to the plan lane failed: no target files",
                      res["response"])

    def test_truncation_defer_does_not_auto_escalate(self):
        cut = "```lean\ndef a := 1"
        truncated = {"choices": [{"message": {"content": cut},
                                  "finish_reason": None}],
                     "usage": {"cost": 0.0}}
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._defer_agent(tmp)
            with patch("harness.agent.chat",
                       return_value=(200, truncated)), \
                 patch("harness.agent.governor_for",
                       return_value=(None, MagicMock())), \
                 patch("harness.agent.ledger_for", return_value=MagicMock()), \
                 patch.object(AutonomousAgent, "_auto_escalation_armed",
                              return_value=True), \
                 patch.object(AutonomousAgent, "_handle_edit") as he:
                res = agent.run_prompt("continue", session_id="e3",
                                       force_conversation=True)
        self.assertEqual(res["status"], "deferred")
        he.assert_not_called()

    def test_disarmed_defer_stays_deferred(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._defer_agent(tmp)
            with patch("harness.agent.chat", return_value=(200, self._DEFER)), \
                 patch("harness.agent.governor_for",
                       return_value=(None, MagicMock())), \
                 patch("harness.agent.ledger_for", return_value=MagicMock()), \
                 patch.object(AutonomousAgent, "_auto_escalation_armed",
                              return_value=False), \
                 patch.object(AutonomousAgent, "_handle_edit") as he:
                res = agent.run_prompt("refactor the whole engine",
                                       session_id="e4", auto_apply=False,
                                       force_conversation=True)
        self.assertEqual(res["status"], "deferred")
        he.assert_not_called()

    def test_auto_escalation_armed_requires_gate_and_key(self):
        # The real arming logic: escalation gate AND a resolved paid key.
        with tempfile.TemporaryDirectory() as tmp:
            gated_off = AutonomousAgent(
                settings=load_settings({"allow_escalation": False}),
                history_dir=Path(tmp))
            gated_on = AutonomousAgent(settings=load_settings(
                {"allow_escalation": True}), history_dir=Path(tmp))
            with patch("harness.agent.resolve_api_key", return_value=None):
                self.assertFalse(gated_on._auto_escalation_armed())
            with patch("harness.agent.resolve_api_key",
                       return_value="sk-test"):
                self.assertFalse(gated_off._auto_escalation_armed())
                self.assertTrue(gated_on._auto_escalation_armed())


class TestOrchestratorDrive(unittest.TestCase):
    """The orchestrator loop: plan -> execute every node -> completion judge
    -> re-plan remaining scope -- until complete or the round budget is
    spent. Failures feed the judge instead of aborting (keep_going).

    The orchestration chat seam is scripted by prompt marker (decomposition
    vs triage vs judge), so the REAL plan_task/assess_completion logic runs
    hermetically."""

    _DECOMPOSE_JSON = ('{"nodes": [{"node_id": "n1", "instruction": "do the '
                       'chunk", "target_files": ["util.py"], "dependencies": []}]}')

    @staticmethod
    def _scripted_seam(verdicts):
        it = iter(verdicts)

        def fake_chat_fn(gov):
            def chat_fn(prompt_text):
                if "completion judge" in prompt_text:
                    v = next(it)
                    import json as _json
                    return _json.dumps(v)
                if "file-triage" in prompt_text:
                    return '{"files": ["util.py"]}'
                return TestOrchestratorDrive._DECOMPOSE_JSON
            return chat_fn
        return patch.object(AutonomousAgent, "_orchestrator_chat_fn",
                            side_effect=fake_chat_fn)

    def test_drive_runs_until_judge_says_complete(self):
        verdicts = [{"complete": False, "remaining": "add the second function",
                     "reason": "half done"},
                    {"complete": True, "remaining": "", "reason": "done"}]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "util.py").write_text("x = 1\n", encoding="utf-8")
            agent = AutonomousAgent(settings=_lane_settings(), root_dir=root,
                                    history_dir=root)
            engine = MagicMock()
            engine.apply_edit.return_value = {"status": "ok", "cost": 0.001}
            with patch("harness.agent.apply_session", return_value=engine), \
                 self._scripted_seam(verdicts):
                res = agent.run_prompt("Update util.py", auto_apply=True)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["orchestrator_rounds"], 2)
        # round 2 was planned from the judge's remaining scope
        self.assertEqual(res["orchestrator_history"][1]["goal"],
                         "add the second function")

    def test_node_failure_feeds_judge_not_abort(self):
        # keep_going: a failed node still lets the rest of the plan run, and
        # the judge sees the failure as state; the judge never says complete,
        # so the round budget (3) is spent and the result stays honest.
        verdicts = [{"complete": False, "remaining": f"fix the gate ({n})",
                     "reason": "node failed"} for n in range(3)]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "util.py").write_text("x = 1\n", encoding="utf-8")
            agent = AutonomousAgent(settings=_lane_settings(), root_dir=root,
                                    history_dir=root)
            engine = MagicMock()
            engine.apply_edit.return_value = {"status": "verify_failed",
                                              "error": "gate boom",
                                              "cost": 0.001}
            with patch("harness.agent.apply_session", return_value=engine), \
                 self._scripted_seam(verdicts):
                res = agent.run_prompt("Update util.py", auto_apply=True)
        self.assertEqual(res["status"], "failed")
        self.assertEqual(res["remaining_scope"], "fix the gate (2)")
        self.assertEqual(res["orchestrator_rounds"], 3)

    def test_judge_unavailable_degrades_to_node_statuses(self):
        # The judge seam dies -> the verdict is None -> the loop degrades to
        # node statuses instead of inventing a completion.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "util.py").write_text("x = 1\n", encoding="utf-8")
            agent = AutonomousAgent(settings=_lane_settings(), root_dir=root,
                                    history_dir=root)
            engine = MagicMock()
            engine.apply_edit.return_value = {"status": "ok", "cost": 0.001}

            def dead_judge(gov):
                def chat_fn(prompt_text):
                    raise HarnessError("judge down")
                return chat_fn
            with patch("harness.agent.apply_session", return_value=engine), \
                 patch.object(AutonomousAgent, "_orchestrator_chat_fn",
                              side_effect=dead_judge):
                res = agent.run_prompt("Update util.py", auto_apply=True)
        # all nodes ok -> honest completion without an invented verdict
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["orchestrator_rounds"], 1)


class TestOrchestratorWiring(unittest.TestCase):
    """The orchestrator wiring the scripted-seam drive tests patch over:
    the real repo enumeration, the real orchestration chat seam (ladder
    iteration over governed_text), the triage scope decision (explicit >
    LLM triage > keywords), and the loop's cancel / judge-down edges."""

    def test_enumerate_repo_files_bounded_and_junk_free(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pkg").mkdir()
            (root / "pkg" / "mod.py").write_text("x = 1", encoding="utf-8")
            (root / "util.py").write_text("x = 2", encoding="utf-8")
            (root / "junk.pyc").write_bytes(b"\x00")
            (root / "run.log").write_text("log", encoding="utf-8")
            (root / "data.jsonl").write_text("{}", encoding="utf-8")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "dep.py").write_text("x = 3", encoding="utf-8")
            (root / "__pycache__").mkdir()
            (root / "__pycache__" / "mod.cpython.pyc").write_bytes(b"\x00")
            (root / ".git").mkdir()
            (root / ".git" / "config").write_text("g", encoding="utf-8")
            files = enumerate_repo_files(root)
            limited = enumerate_repo_files(root, limit=1)
        self.assertIn("util.py", files)
        self.assertIn("pkg/mod.py", files)
        for junk in ("junk.pyc", "run.log", "data.jsonl"):
            self.assertNotIn(junk, files)
        self.assertFalse(any("node_modules" in f or ".git" in f.split("/")
                             or "__pycache__" in f for f in files))
        self.assertEqual(len(limited), 1)

    def _agent(self, root):
        return AutonomousAgent(settings=_lane_settings(), root_dir=root,
                               history_dir=root)

    def test_orchestration_chat_fn_walks_the_ladder(self):
        # model-a 429s, model-b answers: chat_fn returns model-b's text.
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(Path(tmp))
            with patch("harness.agent.resolve_scout_ladder",
                       return_value=["model-a", "model-b"]), \
                 patch("harness.agent.resolve_api_key", return_value="k"), \
                 patch("harness.agent.governed_text",
                       side_effect=[HarnessError("HTTP 429"),
                                    ("answer text", 0.0)]):
                chat_fn = agent._orchestrator_chat_fn("gov")
                self.assertEqual(chat_fn("prompt"), "answer text")

    def test_orchestration_chat_fn_raises_last_ladder_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(Path(tmp))
            with patch("harness.agent.resolve_scout_ladder",
                       return_value=["model-a", "model-b"]), \
                 patch("harness.agent.resolve_api_key", return_value="k"), \
                 patch("harness.agent.governed_text",
                       side_effect=HarnessError("HTTP 429: rate limited")):
                chat_fn = agent._orchestrator_chat_fn("gov")
                with self.assertRaises(HarnessError):
                    chat_fn("prompt")

    def test_triage_scope_llm_pass_over_the_whole_repo(self):
        # No explicit filename in the prompt: the whole-repo listing goes to
        # the triage pass; hallucinated picks are dropped by the real listing.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "util.py").write_text("x = 1", encoding="utf-8")
            (root / "unrelated.py").write_text("y = 2", encoding="utf-8")
            agent = self._agent(root)

            def fake_fn(gov):
                def chat_fn(prompt_text):
                    assert "file-triage" in prompt_text
                    return '{"files": ["util.py", "ghost.py"]}'
                return chat_fn
            with patch.object(AutonomousAgent, "_orchestrator_chat_fn",
                              side_effect=fake_fn):
                picked = agent._triage_scope("make the util helper better")
        self.assertEqual(picked, ["util.py"])

    def test_triage_scope_falls_back_to_keywords_when_model_dies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "util.py").write_text("x = 1", encoding="utf-8")
            agent = self._agent(root)

            def dead_fn(gov):
                def chat_fn(prompt_text):
                    raise HarnessError("429")
                return chat_fn
            with patch.object(AutonomousAgent, "_orchestrator_chat_fn",
                              side_effect=dead_fn):
                picked = agent._triage_scope("fix the util module")
        self.assertEqual(picked, ["util.py"])

    def test_triage_scope_empty_repo_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(Path(tmp))
            self.assertEqual(agent._triage_scope("fix the util module"), [])

    def test_cancel_between_rounds_raises_tool_cancelled(self):
        # The round-loop top checks cancellation before re-planning: the
        # second round never starts once the operator cancelled mid-drive.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "util.py").write_text("x = 1\n", encoding="utf-8")
            agent = self._agent(root)
            engine = MagicMock()
            engine.apply_edit.return_value = {"status": "ok", "cost": 0.001}
            state = {"judged": False}

            def fake_fn(gov):
                def chat_fn(prompt_text):
                    if "completion judge" in prompt_text:
                        state["judged"] = True
                        return ('{"complete": false, "remaining": "second half",'
                                ' "reason": "partial"}')
                    return TestOrchestratorDrive._DECOMPOSE_JSON
                return chat_fn
            with patch("harness.agent.apply_session", return_value=engine), \
                 patch.object(AutonomousAgent, "_orchestrator_chat_fn",
                              side_effect=fake_fn):
                with self.assertRaises(ToolCancelled):
                    agent.run_prompt("Update util.py", auto_apply=True,
                                     cancel_check=lambda: state["judged"])

    def test_force_conversation_edit_intent_routes_to_orchestrator(self):
        # The GUI forces the conversation lane -- but an edit-intent prompt
        # in Auto mode is repo work: it drives the orchestrator loop rather
        # than the chat model narrating code it will never write.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "util.py").write_text("x = 1\n", encoding="utf-8")
            agent = self._agent(root)
            engine = MagicMock()
            engine.apply_edit.return_value = {"status": "ok", "cost": 0.001}
            with patch("harness.agent.apply_session", return_value=engine), \
                 TestOrchestratorDrive._scripted_seam(
                     [{"complete": True, "remaining": "", "reason": "done"}]):
                res = agent.run_prompt("Update util.py", auto_apply=True,
                                       force_conversation=True)
        self.assertEqual(res["intent"], "edit")
        self.assertEqual(res["status"], "ok")

    def test_force_conversation_question_stays_conversational(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(Path(tmp))
            with patch.object(AutonomousAgent, "_handle_conversation",
                              return_value={"status": "ok",
                                            "intent": "conversation"}) as h:
                res = agent.run_prompt("How does the router work?",
                                       auto_apply=True,
                                       force_conversation=True)
            h.assert_called_once()
        self.assertEqual(res["intent"], "conversation")

    def test_node_targets_resolve_against_agent_root_not_cwd(self):
        # The engine resolves file_path against process CWD: the orchestrator
        # must hand it the absolute target under the agent's root, or GUI
        # runs with a chosen workDir edit the wrong tree.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "util.py").write_text("x = 1\n", encoding="utf-8")
            agent = self._agent(root)
            engine = MagicMock()
            engine.apply_edit.return_value = {"status": "ok", "cost": 0.001}
            seen = {}
            def capture(**kwargs):
                seen.update(kwargs)
                return {"status": "ok", "cost": 0.001}
            engine.apply_edit.side_effect = capture
            with patch("harness.agent.apply_session", return_value=engine), \
                 TestOrchestratorDrive._scripted_seam(
                     [{"complete": True, "remaining": "", "reason": "done"}]):
                res = agent.run_prompt("Update util.py", auto_apply=True)
        self.assertEqual(res["status"], "ok")
        self.assertTrue(Path(seen["file_path"]).is_absolute())
        self.assertEqual(Path(seen["file_path"]).name, "util.py")
        self.assertEqual(Path(seen["file_path"]).parent, root)
        # An existing .py target is gated by the round discovery.
        self.assertIn("py_compile", seen["verify_cmd"])

    def test_new_file_node_gets_a_compile_gate(self):
        # A node that CREATES its target has no discovery gate (the file
        # does not exist yet, so the round gate is None too) -- but a .py
        # node is always verifiable after the write: the orchestrator
        # assigns a compile gate, because a gateless node is refused at
        # mutation time by trust policy. Hermetic: an empty sandbox, so
        # the seam's triage pick validates down to nothing.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent = self._agent(root)
            engine = MagicMock()
            seen = {}
            def capture(**kwargs):
                seen.update(kwargs)
                fp = Path(str(kwargs["file_path"]))
                fp.parent.mkdir(parents=True, exist_ok=True)
                fp.write_text("", encoding="utf-8")
                return {"status": "ok", "cost": 0.001}
            engine.apply_edit.side_effect = capture
            verdict = [{"complete": True, "remaining": "", "reason": "done"}]
            with patch("harness.agent.apply_session", return_value=engine), \
                 patch.object(TestOrchestratorDrive, "_DECOMPOSE_JSON",
                              ('{"nodes": [{"node_id": "n1", "instruction": '
                               '"write it", "target_files": ["new_mod.py"], '
                               '"dependencies": []}]}')), \
                 TestOrchestratorDrive._scripted_seam(verdict):
                res = agent.run_prompt("Create new_mod.py", auto_apply=True)
        self.assertEqual(res["status"], "ok")
        self.assertIn("py_compile", str(seen.get("verify_cmd")))
        self.assertEqual(Path(str(seen["verify_cmd"]).split('"')[1]).parent,
                         root)

    def test_large_file_nodes_route_to_diff_backend(self):
        # Whole-file rewrites cap at MAX_FILE_LINES by engine policy; the
        # orchestrator sends large-file nodes straight to the diff backend
        # instead of dying as "out of scope" fatals. (The scripted seam's
        # nodes target util.py, so the oversized file is util.py.)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "util.py").write_text("x = 1\n" * 600, encoding="utf-8")
            agent = self._agent(root)
            engine = MagicMock()
            seen = {}
            def capture(**kwargs):
                seen.update(kwargs)
                return {"status": "ok", "cost": 0.001}
            engine.apply_edit.side_effect = capture
            with patch("harness.agent.apply_session", return_value=engine), \
                 TestOrchestratorDrive._scripted_seam(
                     [{"complete": True, "remaining": "", "reason": "done"}]):
                res = agent.run_prompt("Update util.py", auto_apply=True)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(seen.get("backend"), "diff")

    def test_complete_verdict_overridden_while_named_artifact_missing(self):
        # A judge verdict cannot make a missing artifact exist: with the
        # prompt naming test_util.py and only util.py on disk, a lazy
        # "complete" is overridden and the remaining scope names the truth.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "util.py").write_text("def slugify(t):\n    return t\n",
                                          encoding="utf-8")
            agent = self._agent(root)
            engine = MagicMock()
            engine.apply_edit.return_value = {"status": "ok", "cost": 0.001}
            rounds = {"n": 0}

            def lazy_judge(gov):
                def chat_fn(prompt_text):
                    if "completion judge" in prompt_text:
                        rounds["n"] += 1
                        return ('{"complete": true, "remaining": "",'
                                ' "reason": "looks done"}')
                    return TestOrchestratorDrive._DECOMPOSE_JSON
                return chat_fn
            with patch("harness.agent.apply_session", return_value=engine), \
                 patch.object(AutonomousAgent, "_orchestrator_chat_fn",
                              side_effect=lazy_judge):
                res = agent.run_prompt(
                    "Update util.py and create test_util.py",
                    auto_apply=True)
        self.assertEqual(res["status"], "failed")
        self.assertIn("test_util.py", res["remaining_scope"])
        self.assertEqual(rounds["n"], 3)

    def test_judge_down_with_failed_nodes_reports_honest_scope(self):
        # Judge dies AND nodes fail: the loop must not fake a completion --
        # the remaining scope names the judge outage, per-node failures shown.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "util.py").write_text("x = 1\n", encoding="utf-8")
            agent = self._agent(root)
            engine = MagicMock()
            engine.apply_edit.return_value = {"status": "verify_failed",
                                              "error": "gate boom",
                                              "cost": 0.001}

            def dead_judge(gov):
                def chat_fn(prompt_text):
                    raise HarnessError("judge down")
                return chat_fn
            with patch("harness.agent.apply_session", return_value=engine), \
                 patch.object(AutonomousAgent, "_orchestrator_chat_fn",
                              side_effect=dead_judge):
                res = agent.run_prompt("Update util.py", auto_apply=True)
        self.assertEqual(res["status"], "failed")
        self.assertIn("completion judge unavailable", res["remaining_scope"])


class TestHourglassLane(unittest.TestCase):
    """The lane the GUI actually drives (server.run_chat_task ->
    AutonomousAgent._handle_edit) must run the SAME hourglass the CLI/MCP
    lanes run: waist confirmation through the ONE plan composer, then the
    shared execution assembly (parallel workers, per-node reservations,
    worktree isolation, write attestation) resolved from the settings file.

    The orchestration seam scripts decomposition/triage/judge; the waist
    gate is scripted where it really runs (harness.waist.governed_text), so
    these tests exercise the armed path without a network call.
    """

    _DAG_JSON = ('{"nodes": [{"node_id": "n1", "instruction": "do the chunk", '
                 '"target_files": ["util.py"], "dependencies": []}]}')

    @staticmethod
    def _armed(**overrides):
        settings = load_settings()
        settings.hourglass_confirm = True
        settings.hourglass_isolate = True
        settings.hourglass_parallel = True
        settings.hourglass_require_attestation = True
        for key, value in overrides.items():
            setattr(settings, key, value)
        return settings

    @staticmethod
    def _decompose_seam():
        """Decomposition answers with a DAG; nothing else is scripted."""
        def fake_chat_fn(gov):
            def chat_fn(_prompt_text):
                return TestHourglassLane._DAG_JSON
            return chat_fn
        return patch.object(AutonomousAgent, "_orchestrator_chat_fn",
                            side_effect=fake_chat_fn)

    @staticmethod
    def _waist_seam(verdict):
        return patch("harness.waist.governed_text",
                     return_value=(verdict, 0.0))

    def _lane(self, root):
        (root / "util.py").write_text("x = 1\n", encoding="utf-8")
        agent = AutonomousAgent(settings=self._armed(), root_dir=root,
                                history_dir=root)
        engine = MagicMock()
        engine.apply_edit.return_value = {"status": "ok", "cost": 0.001}
        return agent, engine

    def test_armed_lane_confirms_then_runs_the_shared_assembly(self):
        import harness.executor as executor_module

        captured = {}

        def spy(engine_arg, routes, **kwargs):
            captured.update(kwargs)
            captured["plan_exec"] = executor_module.PlanExecutor(
                engine_arg, routes, **kwargs)
            return captured["plan_exec"]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent, engine = self._lane(root)
            with patch("harness.agent.apply_session", return_value=engine), \
                 patch("harness.agent.PlanExecutor", side_effect=spy), \
                 self._decompose_seam(), \
                 self._waist_seam('{"verdict": "approve"}'):
                res = agent._handle_edit("Update util.py", "hg1", True)

        # 1. the waist gate ran, and its verdict rides the envelope
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["confirmation"]["verdict"], "approved")
        # 2. the shared assembly, armed from the settings file, rooted at
        # the lane's own tree (never the process CWD)
        self.assertTrue(captured["parallel"])
        self.assertTrue(captured["isolate"])
        self.assertTrue(captured["require_diff_authorization"])
        self.assertEqual(captured["repo"], str(root))
        # keep_going: node failures are state for the judge, not aborts
        self.assertTrue(captured["keep_going"])
        self.assertEqual(captured["plan_exec"].workers, 4)
        self.assertIsNotNone(captured["plan_exec"].reserver)
        # 2b. the lane passes the budget it is really running under, so a
        # node reservation is bounded by that and not by a nominal default
        self.assertEqual(captured["run_ceiling"], agent.settings.max_cost)
        self.assertEqual(captured["plan_exec"].reserver.run_ceiling,
                         agent.settings.max_cost)
        # 3. the write attestation the hourglass armed reached the node write
        self.assertTrue(
            engine.apply_edit.call_args[1]["require_diff_authorization"])

    def test_armed_lane_dispatches_a_free_node_under_the_default_ceiling(self):
        """The reported defect, at the lane that had it. A free-tier node
        declares a $0.00 route ceiling; the request drops that $0 (a zero
        task budget would refuse the escalation ladder), so the reserver read
        "no ceiling" and reserved the engine's nominal $0.10 default -- over
        the default $0.05 run ceiling, which refused EVERY node before any
        work. The armed GUI lane must dispatch the node and spend $0.00."""
        from tests._fake import FakeTransport, _gov

        class _StubEngine:
            def __init__(self, governor):
                self.governor = governor
                self.default_task_max_cost = 0.10
                self.calls = []

            def apply_edit(self, **kwargs):
                self.calls.append(kwargs)
                return {"status": "ok", "cost": 0.0, "changed": True}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "util.py").write_text("x = 1\n", encoding="utf-8")
            agent = AutonomousAgent(settings=self._armed(), root_dir=root,
                                    history_dir=root)
            gov = _gov(FakeTransport(), max_cost=0.05)
            self.assertEqual(agent.settings.max_cost, 0.05)
            engine = _StubEngine(gov)
            with patch("harness.agent.apply_session", return_value=engine), \
                 self._decompose_seam(), \
                 self._waist_seam('{"verdict": "approve"}'):
                res = agent._handle_edit("Update util.py", "hg4", True)

        self.assertEqual(res["status"], "ok")
        self.assertTrue(engine.calls, "the node never reached the engine")
        self.assertEqual([r.get("status") for r in res["results"]], ["ok"])
        # Nothing was reserved for a free node, and the run's ceiling is
        # untouched -- the free tier bills $0.00 on every rung.
        self.assertEqual(gov.outstanding, 0.0)
        self.assertEqual(gov.spent, 0.0)
        self.assertEqual(gov.remaining(), 0.05)

    def test_each_node_gates_the_file_it_edits(self):
        """The run-level gate is discovered from the ORIGINAL prompt files, so
        a later round's node targeting another file used to run a gate about
        the first file: it proved nothing about that node's write and made
        two concurrent nodes compile the same path (an intermittent
        verify_failed that also cost the node its merge). Every node must
        gate its own declared target."""
        two_nodes = ('{"nodes": [{"node_id": "n1", "instruction": "add a '
                     'docstring to b.py", "target_files": ["b.py"], '
                     '"dependencies": []}, {"node_id": "n2", "instruction": '
                     '"add a docstring to c.py", "target_files": ["c.py"], '
                     '"dependencies": []}]}')

        def seam(_gov):
            def chat_fn(_prompt_text):
                return two_nodes
            return chat_fn

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent = AutonomousAgent(settings=self._armed(), root_dir=root,
                                    history_dir=root)
            for name in ("a.py", "b.py", "c.py"):
                (root / name).write_text("x = 1\n", encoding="utf-8")
            engine = MagicMock()
            engine.apply_edit.return_value = {"status": "ok", "cost": 0.0}
            with patch("harness.agent.apply_session", return_value=engine), \
                 patch.object(AutonomousAgent, "_orchestrator_chat_fn",
                              side_effect=seam), \
                 self._waist_seam('{"verdict": "approve"}'):
                res = agent._handle_edit("Update a.py", "hg5", True)

        self.assertEqual(res["status"], "ok")
        gates = [call[1]["verify_cmd"]
                 for call in engine.apply_edit.call_args_list]
        self.assertEqual(len(gates), 2)
        self.assertTrue(any("b.py" in g for g in gates), gates)
        self.assertTrue(any("c.py" in g for g in gates), gates)
        # Nothing gated the file the prompt named while editing another.
        self.assertEqual(sum("a.py" in g for g in gates), 0, gates)

    def test_waist_refusal_dispatches_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, engine = self._lane(Path(tmp))
            with patch("harness.agent.apply_session", return_value=engine), \
                 self._decompose_seam(), \
                 self._waist_seam('{"verdict": "refuse", '
                                  '"reason": "scope is too broad", '
                                  '"evidence": "brief: util.py has no tests"}'):
                res = agent._handle_edit("Update util.py", "hg2", True)
        self.assertEqual(res["status"], "refused")
        self.assertIn("scope is too broad", res["response"])
        engine.apply_edit.assert_not_called()
        self.assertEqual(res["cost"], 0.0)

    def test_jev_preplanning_injects_algorithmic_guideline(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, engine = self._lane(Path(tmp))
            planned_prompts = []
            orig_plan_round = agent._plan_round

            def track_plan(goal, candidate_files, gov, confirm=None):
                planned_prompts.append(goal)
                return orig_plan_round(goal, candidate_files, gov, confirm=False)

            with patch.object(agent, "_plan_round", side_effect=track_plan), \
                 self._decompose_seam():
                agent._handle_edit("Implement an iterative convergence loop over util.py", "sid_jev", False)

        self.assertTrue(len(planned_prompts) > 0)
        self.assertIn("[STRUCTURAL GUIDELINE]", planned_prompts[0])
        self.assertIn("iterative control flow", planned_prompts[0])

    def test_agent_lane_measures_its_own_root(self):
        """The planning owner reads the LANE's tree: the target's size is
        measured against the agent's root (the server's CWD has no big.py),
        and a small edit to a large file stays ONE diff-hinted node."""
        json_dag = ('{"nodes": [{"node_id": "n1", "instruction": "Add a '
                    'module docstring", "target_files": ["big.py"], '
                    '"dependencies": []}]}')

        def seam(_gov):
            def chat_fn(_prompt_text):
                return json_dag
            return chat_fn

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "big.py").write_text(
                "".join(f"line_{i} = {i}\n" for i in range(1300)),
                encoding="utf-8")
            agent = AutonomousAgent(settings=self._armed(), root_dir=root,
                                    history_dir=root)
            with patch.object(AutonomousAgent, "_orchestrator_chat_fn",
                              side_effect=seam):
                plan = agent._plan_round("Add a module docstring", ["big.py"],
                                         MagicMock(), confirm=False)
        # Found in the agent's root at all -> the lane's tree is the one
        # measured; and a small edit is one node with the bounded-hunk hint.
        self.assertEqual(plan["total_nodes"], 1)
        self.assertEqual(plan["nodes"][0]["backend"], "diff")
        self.assertNotIn("chunking", plan)

    def test_disarmed_lane_stays_serial_single_tree(self):
        import harness.executor as executor_module

        captured = {}

        def spy(engine_arg, routes, **kwargs):
            captured.update(kwargs)
            captured["plan_exec"] = executor_module.PlanExecutor(
                engine_arg, routes, **kwargs)
            return captured["plan_exec"]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "util.py").write_text("x = 1\n", encoding="utf-8")
            agent = AutonomousAgent(settings=_lane_settings(), root_dir=root,
                                    history_dir=root)
            engine = MagicMock()
            engine.apply_edit.return_value = {"status": "ok", "cost": 0.001}
            with patch("harness.agent.apply_session", return_value=engine), \
                 patch("harness.agent.PlanExecutor", side_effect=spy), \
                 self._decompose_seam():
                res = agent._handle_edit("Update util.py", "hg3", True)
        # Disarmed: the historical lane -- one tree, one worker, no gate.
        self.assertFalse(captured["parallel"])
        self.assertFalse(captured["isolate"])
        self.assertFalse(captured["require_diff_authorization"])
        self.assertEqual(captured["plan_exec"].workers, 1)
        self.assertIsNone(captured["plan_exec"].isolator)
        self.assertNotIn("confirmation", res)
        self.assertFalse(
            engine.apply_edit.call_args[1]["require_diff_authorization"])

    def test_apply_node_jev_structural_evaluation_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "foo.py").write_text("def f():\n    pass\n", encoding="utf-8")
            agent = AutonomousAgent(settings=_lane_settings(), root_dir=root, history_dir=root)
            engine = MagicMock()
            broken_diff = "just conversation without any unified diff headers"
            fixed_diff = "--- a/foo.py\n+++ b/foo.py\n@@ -1,2 +1,2 @@\n-def f():\n+def f():\n     return 42\n"
            engine.apply_edit.side_effect = [
                {"status": "ok", "diff": broken_diff, "cost": 0.001},
                {"status": "ok", "diff": fixed_diff, "cost": 0.001},
            ]
            with patch("harness.agent.apply_session", return_value=engine), \
                 self._decompose_seam():
                res = agent._handle_edit("Update foo.py", "hg_jev", True)
            self.assertEqual(res["status"], "ok")
            self.assertEqual(engine.apply_edit.call_count, 2)
            self.assertIn("STRUCTURAL EVALUATION FAILED", engine.apply_edit.call_args[1]["instruction"])




if __name__ == "__main__":
    unittest.main()
