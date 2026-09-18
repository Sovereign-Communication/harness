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
    get_default_history_dir,
    load_chat_history,
    save_chat_turn,
)
from harness.errors import HarnessError, ToolCancelled


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

            # Test file exists
            gate_a = discover_verification_gate(["harness/mod_a.py"], root_dir=root)
            self.assertEqual(gate_a, "python -m unittest tests/test_mod_a.py")

            # Test file does not exist -> fallback to py_compile
            gate_b = discover_verification_gate(["harness/mod_b.py"], root_dir=root)
            self.assertEqual(gate_b, "python -m py_compile harness/mod_b.py")

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

            agent = AutonomousAgent(root_dir=root, history_dir=root)
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

            agent = AutonomousAgent(root_dir=root, history_dir=root)

            def fake_apply_edit(file_path, instruction, **kwargs):
                # Simulate modifying file
                (root / file_path).write_text("def add(a: int, b: int) -> int: return a + b\n", encoding="utf-8")
                return {"status": "ok", "cost": 0.002}

            mock_engine = MagicMock()
            mock_engine.apply_edit.side_effect = fake_apply_edit

            with patch("harness.agent.apply_session", return_value=mock_engine):
                res = agent.run_prompt("Update harness/calc.py with type annotations", auto_apply=True)
                self.assertEqual(res["status"], "ok")
                self.assertEqual(res["intent"], "edit")
                self.assertIn("Successfully completed task", res["response"])
                self.assertIn("harness/calc.py", res["target_files"])
                self.assertIn("+def add(a: int, b: int) -> int:", res["diff"])
                self.assertAlmostEqual(res["cost"], 0.002)

    def test_handle_edit_self_healing_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "harness").mkdir()
            calc_file = root / "harness" / "calc.py"
            calc_file.write_text("def add(a, b): return a + b\n", encoding="utf-8")

            agent = AutonomousAgent(root_dir=root, history_dir=root)

            # First attempt fails verification gate, second attempt succeeds
            attempts = [
                {"status": "verify_failed", "error": "AssertionError: 3 != 4", "cost": 0.001},
                {"status": "ok", "cost": 0.001},
            ]
            mock_engine = MagicMock()
            mock_engine.apply_edit.side_effect = attempts

            with patch("harness.agent.apply_session", return_value=mock_engine):
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
                # "verify this" would normally classify as 'edit' — but force_conversation overrides
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

    def test_web_on_system_prompt_states_capability_not_denial(self):
        # The "it says web is on..." incident: the base prompt claimed
        # "You have NO internet access" even on web-enabled runs, so the
        # model denied the attached tools. Web-on prompts must state what
        # is attached (search + allowlist hosts) and never deny access.
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(Path(tmp))
            with patch("harness.agent.search_web", return_value=[]), \
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
            with patch("harness.agent.search_web", return_value=fake_results) as ms, \
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
            with patch("harness.agent.fetch_url", return_value=page) as mf, \
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
            with patch("harness.agent.search_web", side_effect=_HE("web search failed: down")), \
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
            with patch("harness.agent.search_web", side_effect=RuntimeError("boom")), \
                 patch("harness.agent.chat", return_value=(200, self._mock_resp())) as mc, \
                 patch("harness.agent.governor_for", return_value=(None, MagicMock())):
                res = agent.run_prompt("search the web", session_id="w5", web=True)
        self.assertEqual(res["status"], "ok")
        self.assertIn("web tools error", mc.call_args[1]["messages"][0]["content"])

    def test_web_fetch_failure_is_disclosed(self):
        # A URL prompt whose fetch is refused: the refusal lands in the model's
        # context as a FAILED source -- never silently dropped.
        from harness.errors import HarnessError as _HE
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(Path(tmp))
            with patch("harness.agent.find_urls",
                       return_value=["https://www.anthropic.com/x"]), \
                 patch("harness.agent.fetch_url",
                       side_effect=_HE("web fetch refused: host not allowed")), \
                 patch("harness.agent.chat", return_value=(200, self._mock_resp())) as mc, \
                 patch("harness.agent.governor_for", return_value=(None, MagicMock())):
                res = agent.run_prompt("https://www.anthropic.com/x",
                                       session_id="w6", web=True)
                sysmsg = mc.call_args[1]["messages"][0]["content"]
        self.assertIn("FAILED", sysmsg)
        self.assertIn("web fetch refused", sysmsg)
        self.assertFalse(res["web_used"])

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


if __name__ == "__main__":
    unittest.main()
