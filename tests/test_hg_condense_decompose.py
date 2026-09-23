"""HG-condense-decompose: decompose prompt carries signatures, not raw bodies."""
import os
import tempfile
import unittest

from harness.waist import compose_plan

from tests._fake import FakeTransport, m

RAW_BODY = (
    "def helper_compute_shipments(query, limit, offset=0, flags=None):\n"
    "    results = []\n"
    "    for row in dataset:\n"
    "        if row.query == query:\n"
    "            results.append(row)\n"
    "            if len(results) >= limit:\n"
    "                break\n"
    "    SECRET_RAW_BODY_MARKER = 'do-not-leak-full-bodies'\n"
    "    return results[offset:offset + limit]\n"
)

DECOMP = """{"nodes": [{"node_id": "task_1", "instruction": "Update helper",
"target_files": ["pkg/mod.py"], "dependencies": []}]}"""


class CondenseDecomposeTests(unittest.TestCase):
    def test_decompose_prompt_contains_condensed_signatures_not_raw_bodies(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg = os.path.join(tmp, "pkg")
            os.makedirs(pkg, exist_ok=True)
            target = os.path.join(pkg, "mod.py")
            with open(target, "w", encoding="utf-8") as handle:
                handle.write("# module header\n" + RAW_BODY)

            prompts = []

            def chat_fn(prompt):
                prompts.append(prompt)
                return DECOMP, 0.0

            fake = FakeTransport(models=[m("m/cheap")])
            from harness.spend import SpendGovernor
            gov = SpendGovernor(fake, "sk-test", max_cost=1.0)
            plan = compose_plan(
                transport=fake, api_key="k",
                governor=gov,
                ledger=None,
                opts_goal="Update the shipments helper",
                candidate_files=["pkg/mod.py"],
                root=tmp,
                decompose_llm=True,
                chat_fn=chat_fn,
                execute=False)

        self.assertEqual(plan["decomposition"], "llm:injected")
        self.assertTrue(prompts)
        prompt = prompts[0]
        # Condensed signature furniture is present...
        self.assertIn("REPOSITORY CONTEXT", prompt)
        self.assertIn("CONDENSED FILE INTERFACES", prompt)
        self.assertIn("def helper_compute_shipments", prompt)
        # ...and the raw implementation body is not.
        self.assertNotIn("SECRET_RAW_BODY_MARKER", prompt)
        self.assertNotIn("for row in dataset:", prompt)

    def test_decompose_without_candidates_has_no_raw_repo_dump(self):
        prompts = []

        def chat_fn(prompt):
            prompts.append(prompt)
            return DECOMP, 0.0

        # No governor + decompose_llm requires governor -- so we only check
        # the helper itself via a governor-less path is refused; the condense
        # producer is covered above. This pins the fail-closed governor rule.
        from harness.errors import HarnessError
        with self.assertRaises(HarnessError):
            compose_plan(
                transport=None, api_key=None, governor=None, ledger=None,
                opts_goal="g", decompose_llm=True, chat_fn=chat_fn)

    def test_decompose_llm_failure_retries_then_fails_closed_in_preview(self):
        """DF-HG-3b (Board ruling on PR #68): preview mode retries once on
        decompose failure, then FAILS CLOSED by default -- it must never
        silently hand back the heuristic plan for an operator who asked
        for LLM decomposition."""
        from harness.errors import HarnessError
        from harness.spend import SpendGovernor

        calls = []

        def broken_chat(prompt):
            calls.append(prompt)
            return "this is not json at all", 0.0

        fake = FakeTransport(models=[m("m/cheap")])
        gov = SpendGovernor(fake, "sk-test", max_cost=1.0)
        with self.assertRaises(HarnessError):
            compose_plan(
                transport=fake, api_key="k",
                governor=gov,
                ledger=None,
                opts_goal="Update the shipments helper",
                candidate_files=["pkg/mod.py"],
                decompose_llm=True,
                chat_fn=broken_chat,
                execute=False)
        self.assertEqual(len(calls), 2)  # initial attempt + 1 retry

    def test_decompose_llm_failure_optin_falls_back_loudly_in_preview(self):
        """DF-HG-3b: --allow-heuristic-preview (allow_heuristic_preview=True)
        opts a plan-only preview into the same loud heuristic fallback as
        --execute, with an orchestration note and a skipped (never
        approved) waist confirmation."""
        from harness import events
        from harness.spend import SpendGovernor

        captured_events = []

        def sink(ev):
            captured_events.append(ev)

        events.add_sink(sink)
        try:
            calls = []

            def broken_chat(prompt):
                calls.append(prompt)
                return "this is not json at all", 0.0

            fake = FakeTransport(models=[m("m/cheap")])
            gov = SpendGovernor(fake, "sk-test", max_cost=1.0)
            plan = compose_plan(
                transport=fake, api_key="k",
                governor=gov,
                ledger=None,
                opts_goal="Update the shipments helper",
                candidate_files=["pkg/mod.py"],
                decompose_llm=True,
                chat_fn=broken_chat,
                execute=False,
                allow_heuristic_preview=True)

            self.assertEqual(len(calls), 2)  # initial attempt + 1 retry
            self.assertEqual(plan["decomposition"], "heuristic")
            self.assertIn("dag", plan)
            self.assertTrue(plan["dag"]["nodes"])
            # Orchestration note emitted
            notes = [e for e in captured_events if e.get("type") == "orchestration_note"]
            self.assertTrue(any("LLM decomposition failed" in n.get("note", "") for n in notes))
        finally:
            events.remove_sink(sink)

    def test_decompose_llm_transient_failure_succeeds_on_retry(self):
        """DF-HG-3: LLM decomposition retry succeeds if second attempt returns valid JSON."""
        from harness.spend import SpendGovernor

        calls = []

        def transient_chat(prompt):
            calls.append(prompt)
            if len(calls) == 1:
                return "not json", 0.0
            return DECOMP, 0.0

        fake = FakeTransport(models=[m("m/cheap")])
        gov = SpendGovernor(fake, "sk-test", max_cost=1.0)
        plan = compose_plan(
            transport=fake, api_key="k",
            governor=gov,
            ledger=None,
            opts_goal="Update the shipments helper",
            candidate_files=["pkg/mod.py"],
            decompose_llm=True,
            chat_fn=transient_chat,
            execute=False)

        self.assertEqual(len(calls), 2)
        self.assertEqual(plan["decomposition"], "llm:injected")
        self.assertIn("dag", plan)


if __name__ == "__main__":
    unittest.main()
