"""Conversation context: durable facts, budgeted history, Jev recall gate.

The regression these tests pin is a real GUI session (2026-10-23): the
operator named ``www.freeoffgridcalculator.com`` in one turn, asked to audit
"the same site" in the next, and the model answered that no site had ever
been provided.  The URL turn was longer than the old fixed 2,400-character
history budget, so it was dropped whole.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness.config import load_settings
from harness.errors import HarnessError
from harness.jev_policy import policy_for
from harness.ledger import AutonomyLedger
from harness.spend import SpendGovernor
from tests._fake import FakeTransport
from harness.conversation_context import (
    assemble_history,
    extract_facts,
    facts_block,
    load_memory,
    memory_path,
    needs_earlier_context,
    policy_recall_judge,
    referential,
    remember_turn,
    sole_site,
)

LONG_RESPONSE = ("## Jev Usability Assessment\n\n" + "The page is clear. " * 400)
SITE = "freeoffgridcalculator.com"


class ExtractFactsTests(unittest.TestCase):
    def test_url_and_bare_site_are_both_kept(self):
        facts = extract_facts("audit www.freeoffgridcalculator.com please")
        kinds = {(f["kind"], f["value"]) for f in facts}
        self.assertIn(("site", SITE), kinds)

    def test_full_url_contributes_url_and_site(self):
        facts = extract_facts("see https://www.freeoffgridcalculator.com/tool?x=1.")
        values = {f["value"] for f in facts if f["kind"] in ("url", "site")}
        self.assertIn("https://www.freeoffgridcalculator.com/tool?x=1", values)
        self.assertIn(SITE, values)

    def test_filenames_and_abbreviations_are_not_sites(self):
        facts = extract_facts("open calc.py, e.g. the tests, and read notes.txt")
        self.assertFalse([f for f in facts if f["kind"] == "site"])

    def test_windows_and_posix_paths_are_kept(self):
        facts = extract_facts(r"look in C:\repo\harness\server.py and ./tests/test_chat.py")
        paths = {f["value"] for f in facts if f["kind"] == "path"}
        self.assertIn(r"C:\repo\harness\server.py", paths)
        self.assertIn("./tests/test_chat.py", paths)


class MemoryTests(unittest.TestCase):
    def test_site_survives_into_a_later_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remember_turn(
                "sess_first",
                "audit www.freeoffgridcalculator.com for usability",
                "Here is the audit.",
                history_dir=root,
            )
            memory = load_memory(root)
            block = facts_block(memory, budget_chars=2000)
            self.assertIn(SITE, block)
            self.assertIn("sess_first", block)
            self.assertIn("durable facts", block)
            self.assertTrue(memory_path(root).exists())

    def test_repeated_site_is_deduped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remember_turn("s1", "audit www.example.com", "ok", history_dir=root)
            remember_turn("s2", "audit example.com again", "ok", history_dir=root)
            memory = load_memory(root)
            sites = [f for f in memory["facts"] if f["kind"] == "site"]
            self.assertEqual(len(sites), 1)
            self.assertEqual(sites[0]["value"], "example.com")

    def test_memory_file_is_valid_json_after_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remember_turn("s1", "audit www.example.com", "ok", history_dir=root)
            remember_turn("s1", "now look at C:\\repo\\x.py", "ok", history_dir=root)
            doc = json.loads(memory_path(root).read_text(encoding="utf-8"))
            self.assertEqual(doc["schema"], "harness-conversation-memory-v1")
            self.assertTrue(doc["sessions"]["s1"]["last_prompt"].startswith("now look"))


class AssembleHistoryTests(unittest.TestCase):
    def _memory_with_site(self):
        return {
            "schema": "harness-conversation-memory-v1",
            "facts": [{
                "kind": "site", "value": SITE, "source": "user",
                "session_id": "sess_first", "first_seen": "2026-10-23T04:45:00+00:00",
                "last_seen": "2026-10-23T04:45:00+00:00",
            }],
            "sessions": {},
        }

    def test_long_turn_that_named_the_site_is_not_dropped(self):
        turns = [
            {"prompt": "can you audit this site and tell me how user friendly it is?",
             "response": "Request needs clarification."},
            {"prompt": "oh that's a great catch - www.freeoffgridcalculator.com - and Jev is a part of the harness tool",
             "response": LONG_RESPONSE},
        ]
        messages = assemble_history(
            "there is actually a progress bar - please audit again and nitpick every detail",
            turns, budget_chars=8000, memory=self._memory_with_site())
        joined = "\n".join(m["content"] for m in messages)
        self.assertIn(SITE, joined)
        self.assertTrue(any(m["role"] == "assistant" for m in messages))

    def test_oversized_turn_is_clipped_head_and_tail(self):
        turns = [{"prompt": "the site is www.example.com", "response": LONG_RESPONSE}]
        messages = assemble_history(
            "audit that again", turns, budget_chars=900,
            memory=self._memory_with_site())
        text = "\n".join(m["content"] for m in messages)
        self.assertIn("characters elided", text)
        self.assertIn("## Jev Usability Assessment", text)
        self.assertIn("The page is clear.", text)

    def test_no_three_turn_cap_when_the_budget_allows(self):
        turns = [{"prompt": f"turn {i} about the site", "response": f"answer {i}"}
                 for i in range(10)]
        messages = assemble_history(
            "same site", turns, budget_chars=50_000,
            memory=self._memory_with_site(), expand=True)
        self.assertEqual(sum(1 for m in messages if m["role"] == "user"), 10)

    def test_budget_is_the_only_ceiling(self):
        turns = [{"prompt": f"turn {i} " + "x" * 400, "response": "answer"} for i in range(10)]
        small = assemble_history("same site", turns, budget_chars=1200, expand=True)
        large = assemble_history("same site", turns, budget_chars=20_000, expand=True)
        self.assertLess(len(small), len(large))

    def test_fact_bearing_turn_is_admitted_even_when_budget_is_tight(self):
        # The fact-bearing turn is not the immediate prior, so it is admitted
        # because it carries the remembered site -- not because recency saved it.
        turns = [
            {"prompt": f"the site is www.{SITE}", "response": LONG_RESPONSE},
            {"prompt": "status?", "response": "still here"},
        ]
        messages = assemble_history(
            "audit that site again", turns, budget_chars=300,
            memory=self._memory_with_site())
        self.assertIn(SITE, "\n".join(m["content"] for m in messages))


class SoleSiteTests(unittest.TestCase):
    @staticmethod
    def _memory(*sites, session_id="s1"):
        return {"facts": [
            {"kind": "site", "value": site, "session_id": session_id,
             "first_seen": "2026-10-23", "last_seen": "2026-10-23"}
            for site in sites
        ]}

    def test_one_site_resolves_the_referent(self):
        self.assertEqual(sole_site(self._memory(SITE), session_id="s1"), SITE)

    def test_two_sites_stay_ambiguous(self):
        self.assertEqual(sole_site(self._memory(SITE, "example.com")), "")

    def test_current_session_wins_over_older_sessions(self):
        memory = self._memory("old.example", session_id="s0")
        memory["facts"].append({"kind": "site", "value": SITE,
                                "session_id": "s1"})
        self.assertEqual(sole_site(memory, session_id="s1"), SITE)


class PolicyRecallJudgeTests(unittest.TestCase):
    class _Policy:
        keyed = True

        def __init__(self, needed):
            self.needed = needed
            self.calls = []

        def evaluate_context_need(self, prompt, **kwargs):
            self.calls.append((prompt, kwargs))
            return object(), {
                "needs_earlier_context": self.needed,
                "target_is_remembered": True,
                "probabilities": {"needs_earlier_context": 0.9,
                                  "target_is_remembered": 0.8},
            }

    def test_unkeyed_policy_is_not_a_judge(self):
        class Unkeyed:
            keyed = False

        self.assertIsNone(policy_recall_judge(Unkeyed(), "facts", "recent"))

    def test_keyed_policy_answers_with_its_probabilities(self):
        policy = self._Policy(True)
        judge = policy_recall_judge(policy, "facts", "recent", task_id="sid")
        needed, reason = judge("audit it again")
        self.assertTrue(needed)
        self.assertIn("0.9", reason)
        self.assertEqual(policy.calls[0][0], "audit it again")
        self.assertEqual(policy.calls[0][1]["facts"], "facts")

    def test_missing_verdict_raises_for_the_fallback(self):
        class Blank(self._Policy):
            def evaluate_context_need(self, prompt, **kwargs):
                return object(), {"needs_earlier_context": None}

        judge = policy_recall_judge(Blank(False), "facts", "recent")
        with self.assertRaises(HarnessError):
            judge("q")


class RecallGateTests(unittest.TestCase):
    def test_referential_prompts_are_detected(self):
        self.assertTrue(referential("same site as before"))
        self.assertTrue(referential("audit it again"))
        self.assertFalse(referential("write a haiku about rain"))

    def test_jev_verdict_is_authoritative_when_present(self):
        need = needs_earlier_context(
            "audit it again", judge=lambda prompt: (False, "facts block is enough"))
        self.assertFalse(need.needed)
        self.assertTrue(need.native)
        self.assertIn("facts block is enough", need.reason)

    def test_jev_true_widens_context(self):
        need = needs_earlier_context(
            "what did you find?", judge=lambda prompt: {"needed": True, "reason": "prior result"})
        self.assertTrue(need.needed)
        self.assertTrue(need.native)

    def test_broken_judge_falls_back_without_raising(self):
        def judge(_prompt):
            raise RuntimeError("provider down")

        need = needs_earlier_context("same site as before", judge=judge)
        self.assertTrue(need.needed)
        self.assertFalse(need.native)
        self.assertIn("provider down", need.reason)

    def test_budget_bounds_facts_block(self):
        memory = self._memory_with_site()
        block = facts_block(memory, budget_chars=10_000)
        self.assertIn(SITE, block)
        tiny = facts_block(memory, budget_chars=10)
        self.assertEqual(tiny, "")

    @staticmethod
    def _memory_with_site():
        return {
            "schema": "harness-conversation-memory-v1",
            "facts": [{"kind": "site", "value": SITE, "source": "user",
                       "session_id": "s", "first_seen": "2026-10-23",
                       "last_seen": "2026-10-23"}],
            "sessions": {},
        }


class _ContextTransport:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def post(self, url, key, payload, timeout=45):
        self.calls.append(payload)
        if self.error is not None:
            raise self.error
        return 200, self.response


class ContextRecallPolicyTests(unittest.TestCase):
    """The real keyed/unkeyed policy paths behind the recall gate."""

    def _policy(self, transport, *, keyed=True):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        settings = load_settings({})
        settings.jev_api_key = "jev-key" if keyed else None
        return policy_for(
            settings, transport=transport,
            governor=SpendGovernor(FakeTransport(), "sk-test", max_cost=1.0),
            ledger=AutonomyLedger(str(root / "ledger.jsonl")))

    def test_unkeyed_policy_reports_no_native_signal(self):
        policy = self._policy(_ContextTransport(), keyed=False)
        result, structural = policy.evaluate_context_need(
            "same site", facts="- site: example.com", recent="")
        self.assertTrue(result.is_fallback)
        self.assertIsNone(structural["needs_earlier_context"])
        self.assertIn("no key", structural["reason"])

    def test_keyed_policy_normalizes_noul_answers(self):
        transport = _ContextTransport({
            "model": "jev-test", "usage": {"input_tokens": 40,
                                           "output_tokens": 6},
            "answers": {
                "needs_earlier_context": {"type": "noul", "noul": 0.9},
                "target_is_remembered": {"type": "noul", "noul": 0.8},
            },
        })
        policy = self._policy(transport)
        result, structural = policy.evaluate_context_need(
            "same site", facts="- site: example.com", recent="user: hi")
        self.assertFalse(result.is_fallback)
        self.assertTrue(structural["needs_earlier_context"])
        self.assertTrue(structural["target_is_remembered"])
        self.assertTrue(structural["native"])
        self.assertEqual(structural["probabilities"]["needs_earlier_context"], 0.9)
        sent = transport.calls[0]
        self.assertIn("example.com", str(sent))

    def test_cancellation_and_spend_limit_are_never_swallowed(self):
        from harness.errors import ToolCancelled
        from harness.provider_errors import ProviderSpendLimitError

        for error in (ToolCancelled("cancelled"),
                      ProviderSpendLimitError("out of funds")):
            policy = self._policy(_ContextTransport(error=error))
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(type(error)):
                    policy.evaluate_context_need(
                        "same site", facts="- site: example.com")

    def test_provider_failure_leaves_an_honest_refusal(self):
        policy = self._policy(_ContextTransport(error=HarnessError("boom")))
        result, structural = policy.evaluate_context_need(
            "same site", facts="- site: example.com", recent="user: hi")
        self.assertTrue(result.is_fallback)
        self.assertTrue(str(structural.get("reason") or
                            result.fallback_reason or "").strip())


class SaveProjectionTests(unittest.TestCase):
    def test_memory_failure_never_breaks_the_transcript_save(self):
        from harness.history import load_chat_history, save_chat_turn

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def boom(*_args, **_kwargs):
                raise RuntimeError("projection failed")

            with mock.patch(
                    "harness.conversation_context.remember_turn", side_effect=boom):
                save_chat_turn("s1", {"prompt": "audit www.example.com",
                                      "response": "ok"}, root)
            turns = load_chat_history("s1", history_dir=root)
            self.assertEqual(turns[0]["prompt"], "audit www.example.com")


if __name__ == "__main__":
    unittest.main()
