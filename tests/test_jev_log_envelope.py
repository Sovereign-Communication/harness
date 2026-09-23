"""JEV-LOG envelope + factor-pass gate tests (tests/test_jev_log_envelope.py).

Stage B draft prompt + operator freeze gate, Stage D batch via the ONE policy
owner, Stage E code-owned aggregate artifact, thin CLI/MCP faces. Hermetic:
unkeyed policy (keyword fallback) everywhere; the aggregate must stay honest
about fallbacks and unmatched items.
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from harness.config import load_settings
from harness.jev_policy import policy_for
from harness.ledger import AutonomyLedger
from harness.log_analysis import (
    aggregate_log_items,
    analyze_log,
    freeze_pack_draft,
    stage_b_pack_prompt,
    write_analysis,
)
from harness.log_items import extract_log_items


LOG_TEXT = "\n".join([
    "2026-09-21T00:37:26.714850Z  WARN scmessenger_cli::ble_daemon: btleplug BLE adapter probe error=No Bluetooth adapter found",
    "2026-09-21T00:37:26.772270Z  WARN scmessenger_cli::ble_mesh: BLE central unavailable",
    "2026-09-21T00:37:52.809649Z  INFO scmessenger_core::transport::swarm: [ERROR] Disconnected from 12D3KooWAAA",
    "2026-09-21T00:37:53.000000Z  WARN scmessenger_core::transport::swarm: swarm disconnected; yamux dial failed",
    "2026-09-21T00:38:10.000000Z  WARN misc::thing: totally unrelated fluff about weather",
    "2026-09-21T00:39:00.000000Z ERROR misc::other: malformed nothing",
])


def log_pack():
    return {
        "id": "scmessenger-ops-log-v1",
        "buckets": {
            "transport": {
                "label": "Transport / swarm health", "kind": "trouble_area",
                "path_id": "log/transport",
                "keywords": ["swarm", "yamux", "dial", "disconnected"],
                "suggested_next_action": "review dial/relay policy",
                "attention": "high",
            },
            "ble": {
                "label": "Bluetooth adapter", "kind": "trouble_area",
                "path_id": "log/ble",
                "keywords": ["ble", "bluetooth", "btleplug", "gatt"],
                "suggested_next_action": None, "attention": "low",
            },
        },
        "score": {
            "id": "sentiment",
            "instructions": ("Rate operational severity/attention for this "
                             "log item from the stated levels only."),
            "levels": [
                "benign — routine info, no operator attention",
                "elevated — degraded but bounded behavior",
                "actionable — likely defect or policy issue",
                "critical — security, data loss, or hard outage signal",
            ],
        },
    }


def _unkeyed_policy(tmpdir):
    settings = load_settings()
    settings.jev_api_key = None
    settings.ledger_path = os.path.join(tmpdir, "ledger.jsonl")
    return policy_for(settings, ledger=AutonomyLedger(settings.ledger_path))


class StageBPromptTests(unittest.TestCase):
    def test_prompt_is_single_user_message_and_declares_draft_status(self):
        messages = stage_b_pack_prompt(["WARN m::a: dial failed"], "swarm ops")
        self.assertEqual([m["role"] for m in messages], ["user"])
        body = messages[0]["content"]
        self.assertIn("strict JSON", body)
        self.assertIn("WARN m::a: dial failed", body)
        self.assertIn("swarm ops", body)

    def test_prompt_never_claims_the_draft_is_approved(self):
        body = stage_b_pack_prompt(["x"], "v")[0]["content"]
        self.assertNotIn("approved", body.lower())
        self.assertIn("draft", body.lower())


class FreezeGateTests(unittest.TestCase):
    def test_refuses_without_explicit_approval(self):
        with self.assertRaises(ValueError):
            freeze_pack_draft(log_pack(), approved=False)

    def test_refuses_non_pack_shapes(self):
        with self.assertRaises(ValueError):
            freeze_pack_draft("nope", approved=True)
        with self.assertRaises(ValueError):
            freeze_pack_draft({"id": "x"}, approved=True)

    def test_approved_freeze_preserves_content(self):
        pack = log_pack()
        frozen = freeze_pack_draft(pack, approved=True)
        self.assertEqual(frozen["id"], pack["id"])
        self.assertEqual(frozen["buckets"], pack["buckets"])
        self.assertEqual(frozen["score"], pack["score"])

    def test_frozen_pack_is_valid_at_the_policy_edge(self):
        from harness.jev_packs import validate_log_pack
        frozen = freeze_pack_draft(log_pack(), approved=True)
        self.assertEqual(validate_log_pack(frozen)["id"], "scmessenger-ops-log-v1")


class AnalyzeLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.policy = _unkeyed_policy(self.tmp.name)
        self.pack = log_pack()

    def test_single_pass_artifact_shape_and_honest_coverage(self):
        analysis = analyze_log(LOG_TEXT, self.pack, self.policy)
        self.assertEqual(analysis["pack_id"], "scmessenger-ops-log-v1")
        self.assertEqual(analysis["coverage"]["total_items"], 5)
        # The INFO line mentioning [ERROR] must NOT be an item (header rules).
        self.assertNotIn("12D3KooWAAA",
                         json.dumps(analysis["items"]))
        # Every item resolved: bucketed or explicitly unmatched.
        self.assertEqual(analysis["coverage"]["bucketed"]
                         + analysis["coverage"]["unmatched"],
                         analysis["coverage"]["total_items"])
        # Unkeyed run: every judgment is fallback and honest about it.
        self.assertEqual(analysis["coverage"]["fallbacks"],
                         analysis["coverage"]["total_items"])
        self.assertEqual(analysis["coverage"]["live_judged"], 0)
        # Buckets came from the pack keyword matcher only.
        self.assertEqual(analysis["buckets"], {"ble": 2, "transport": 1})
        self.assertIn("transport", analysis["declared_buckets"])
        self.assertEqual(analysis["scores"]["by_level"], {})  # unkeyed: no live score
        self.assertIsNone(analysis["scores"]["mean_value"])
        self.assertGreater(analysis["mechanical"]["selected_items"], 0)

    def test_rows_carry_code_owned_evidence_and_pack_fields(self):
        analysis = analyze_log(LOG_TEXT, self.pack, self.policy)
        swarm = [r for r in analysis["items"]
                 if r["module"] == "scmessenger_core::transport::swarm"
                 and r["level"] == "warn"][0]
        self.assertEqual(swarm["bucket"], "transport")
        self.assertEqual(swarm["path_id"], "log/transport")
        self.assertEqual(swarm["suggested_next_action"],
                         "review dial/relay policy")
        self.assertTrue(swarm["evidence"].startswith("line "))
        unmatched = [r for r in analysis["items"] if r["bucket"] is None]
        self.assertTrue(unmatched)
        self.assertIsNone(unmatched[0]["path_id"])

    def test_keyed_double_scores_flow_into_the_artifact(self):
        class _KeyedTransport:
            def post(self, url, key, payload, timeout=45):
                levels = log_pack()["score"]["levels"]
                return 200, {
                    "model": "jev-test",
                    "answers": {
                        "bucket": {
                            "type": "choice", "choice": "transport",
                            "probabilities": {"transport": 0.6, "ble": 0.4},
                            "confidence": 0.9,
                        },
                        "sentiment": {
                            "type": "score", "score": 0.5,
                            "legend": {"0.0": levels[0], "0.5": levels[1],
                                       "1.0": levels[2]},
                            "probabilities": {"0.0": 0.2, "0.5": 0.3, "1.0": 0.5},
                            "confidence": 0.9,
                        },
                    },
                    "usage": {"input_tokens": 42, "output_tokens": 3},
                }

        settings = load_settings({"jev_api_key": "jev-key"})
        settings.ledger_path = os.path.join(self.tmp.name, "led.jsonl")

        class _CountingGovernor:
            max_cost = 1.0
            spent = 0.0

            def reserve(self, worst, label):
                return {"label": label, "worst": worst}

            def reconcile(self, reservation, cost):
                self.spent += float(cost or 0.0)

            def record_actual(self, cost, model):
                self.spent += float(cost or 0.0)

        policy = policy_for(settings, transport=_KeyedTransport(),
                            governor=_CountingGovernor(),
                            ledger=AutonomyLedger(settings.ledger_path))
        analysis = analyze_log(LOG_TEXT, self.pack, policy)
        self.assertEqual(analysis["coverage"]["fallbacks"], 0)
        self.assertEqual(analysis["scores"]["by_level"],
                         {log_pack()["score"]["levels"][2]: 5})
        self.assertAlmostEqual(analysis["scores"]["mean_value"], 0.5)

    def test_aggregate_is_pure_arithmetic(self):
        items = extract_log_items(LOG_TEXT)
        rows = [{**item, "bucket": "transport", "is_fallback": False,
                 "score": {"id": "s", "level": "high", "value": 0.5,
                           "confidence": 0.9},
                 "path_id": "log/transport",
                 "suggested_next_action": "act"} for item in items]
        analysis = aggregate_log_items(items=rows, pack=self.pack,
                                       log_text=LOG_TEXT)
        self.assertEqual(analysis["coverage"]["bucketed"], len(items))
        self.assertEqual(analysis["buckets"], {"transport": len(items)})
        self.assertEqual(analysis["scores"]["by_level"], {"high": len(items)})
        self.assertAlmostEqual(analysis["scores"]["mean_value"], 0.5)

    def test_write_analysis_persists_utf8_no_bom(self):
        analysis = analyze_log(LOG_TEXT, self.pack, self.policy)
        out = os.path.join(self.tmp.name, "nested", "analysis.json")
        write_analysis(analysis, out)
        with open(out, "rb") as handle:
            raw = handle.read()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        data = json.loads(raw.decode("utf-8"))
        self.assertEqual(data["pack_id"], "scmessenger-ops-log-v1")


class LogJudgmentCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log_path = os.path.join(self.tmp.name, "run.log")
        with open(self.log_path, "w", encoding="utf-8") as handle:
            handle.write(LOG_TEXT)
        self.pack_path = os.path.join(self.tmp.name, "pack.json")
        with open(self.pack_path, "w", encoding="utf-8") as handle:
            json.dump(log_pack(), handle)
        self.out_path = os.path.join(self.tmp.name, "out.json")

    def test_cli_end_to_end_unkeyed(self):
        from harness import cli
        settings = load_settings()
        settings.jev_api_key = None
        settings.ledger_path = os.path.join(self.tmp.name, "ledger.jsonl")
        with mock.patch("harness.cli.load_settings", return_value=settings):
            cli.main([
                "log-judgment",
                "--log", self.log_path,
                "--pack", self.pack_path,
                "--save-to", self.out_path,
            ])
        with open(self.out_path, encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(data["pack_id"], "scmessenger-ops-log-v1")
        self.assertEqual(data["coverage"]["total_items"], 5)
        self.assertEqual(data["buckets"], {"ble": 2, "transport": 1})
        self.assertEqual(data["coverage"]["fallbacks"], 5)

    def test_cli_warns_on_zero_matched_items(self):
        # DF-DOCS-3: a log dump with lines but none matching the documented
        # `tracing` header shape must print an honest stderr note instead of
        # silently emitting an empty-looking analysis.
        import contextlib
        import io
        from harness import cli
        nonmatching_log = os.path.join(self.tmp.name, "nonmatching.log")
        with open(nonmatching_log, "w", encoding="utf-8") as handle:
            handle.write("this is not a tracing-shaped log line\n"
                         "neither is this one\n")
        settings = load_settings()
        settings.jev_api_key = None
        settings.ledger_path = os.path.join(self.tmp.name, "ledger2.jsonl")
        stderr = io.StringIO()
        with mock.patch("harness.cli.load_settings", return_value=settings):
            with contextlib.redirect_stderr(stderr):
                cli.main(["log-judgment", "--log", nonmatching_log,
                          "--pack", self.pack_path,
                          "--save-to", self.out_path])
        self.assertIn("matched 0 log items", stderr.getvalue())
        with open(self.out_path, encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(data["coverage"]["total_items"], 0)

    def test_cli_requires_valid_pack(self):
        from harness import cli
        settings = load_settings()
        settings.jev_api_key = None
        bad = os.path.join(self.tmp.name, "bad.json")
        with open(bad, "w", encoding="utf-8") as handle:
            json.dump({"id": "x"}, handle)
        with mock.patch("harness.cli.load_settings", return_value=settings):
            # cli.main renders HarnessError as [FATAL] + exit 1.
            with self.assertRaises(SystemExit):
                cli.main(["log-judgment", "--log", self.log_path,
                          "--pack", bad])


class LogJudgmentMcpTests(unittest.TestCase):
    def test_keyword_fixture_is_substring_safe(self):
        # The P5 keyword matcher is substring-based; operators must declare
        # matcher-safe keywords ("ble" alone would match inside "unparseable").
        items = extract_log_items(LOG_TEXT)
        text_blob = " ".join(i["text"] for i in items)
        self.assertIn("malformed", text_blob)
        analysis = analyze_log(LOG_TEXT, log_pack(), _unkeyed_policy(
            tempfile.mkdtemp()))
        self.assertNotIn("misc::other", [r["module"] for r in analysis["items"]
                                          if r["bucket"] == "ble"])

    def test_mcp_envelope_carries_analysis_and_coverage(self):
        from types import SimpleNamespace
        from harness.mcp import McpServer
        settings = load_settings()
        settings.jev_api_key = None
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        settings.ledger_path = os.path.join(tmp.name, "mcp-ledger.jsonl")
        policy = policy_for(settings, ledger=AutonomyLedger(settings.ledger_path))
        engine = SimpleNamespace(jev_policy=policy, settings=settings)
        server = McpServer(
            transport=mock.MagicMock(), api_key="key",
            governor=mock.MagicMock(), ledger=mock.MagicMock(),
            router=mock.MagicMock(), engine=engine)
        result = server._invoke("log_judgment", {
            "log_text": LOG_TEXT, "pack": log_pack()})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["coverage"]["total_items"], 5)
        self.assertEqual(result["analysis"]["buckets"], {"ble": 2, "transport": 1})

    def test_mcp_requires_pack_and_policy(self):
        from types import SimpleNamespace
        from harness.errors import HarnessError
        from harness.mcp import McpServer
        engine = SimpleNamespace(jev_policy=None, settings=None)
        server = McpServer(
            transport=mock.MagicMock(), api_key="key",
            governor=mock.MagicMock(), ledger=mock.MagicMock(),
            router=mock.MagicMock(), engine=engine)
        with self.assertRaises(HarnessError):
            server._invoke("log_judgment", {"log_text": LOG_TEXT})  # missing pack
        with self.assertRaises(HarnessError):
            server._invoke("log_judgment", {"log_text": LOG_TEXT,
                                            "pack": log_pack()})

    def test_mcp_tool_schema_declares_log_judgment(self):
        from harness.mcp_schemas import TOOL_SCHEMAS
        names = [t["name"] for t in TOOL_SCHEMAS]
        self.assertIn("log_judgment", names)


if __name__ == "__main__":
    unittest.main()
