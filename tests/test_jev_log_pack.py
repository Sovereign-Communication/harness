"""JEV-LOG schema + parse gate tests (tests/test_jev_log_pack.py).

Operator log-pack validation (P5 pack + one score block) and the code-owned
log item extractor. Hermetic: the fixture mimics the real SCMessenger tracing
format (including the trap where message text mentions [ERROR] but the header
level is INFO).
"""
import unittest

from harness.jev_packs import (
    log_factor_question_pack,
    validate_log_pack,
)
from harness.log_items import extract_log_items, mechanical_tallies

LOG_FIXTURE = "\n".join([
    "2026-09-21T00:37:24.469880Z  INFO scmessenger_cli: SCMessenger CLI starting up... (CLI Version: 0.4.0)",
    "[INFO] Bounded auto-reply ENABLED: one acknowledgement per unique text message",
    "  body: [auto-reply] Node is on but unattended - I will read your message when back.",
    "2026-09-21T00:37:26.567713Z  INFO scmessenger_core::store::ledger_entry: migrated ledger peer_id event=\"ledger_canonical_hex_migration\"",
    "2026-09-21T00:37:26.714850Z  WARN scmessenger_cli::ble_daemon: btleplug BLE adapter probe route=\"ble_probe\" terminal_result=\"unavailable\" error=No Bluetooth adapter found",
    "  details: adapter probe failed twice",
    "2026-09-21T00:37:26.772270Z  WARN scmessenger_cli::ble_mesh: BLE central unavailable route=\"ble_gatt_central\" terminal_result=\"no_adapter\"",
    "2026-09-21T00:37:52.809649Z  INFO scmessenger_core::transport::swarm: [ERROR] Disconnected from 12D3KooWAHJRsbJekFW5WKTEo9DhmpD2vzdnLCdZFWv3Tp5daAQp",
    "2026-09-21T00:37:53.000000Z ERROR scmessenger_cli::ble_mesh: BLE: Windows GATT server error: Error { code: HRESULT(0x00000000) }",
    "  context line one",
    "  context line two",
    "2026-09-21T00:38:10.000000Z  WARN scmessenger_core::store::relay_custody: Custody retention sweep: expired 0 of 198 record(s)",
])


def sample_log_pack():
    return {
        "id": "scmessenger-ops-log-v1",
        "buckets": {
            "transport": {
                "label": "Transport / swarm health",
                "kind": "trouble_area",
                "path_id": "log/transport",
                "keywords": ["dial", "swarm", "listener", "negotiation",
                             "yamux", "disconnected"],
                "suggested_next_action": "review dial/relay policy",
                "attention": "high",
            },
            "delivery": {
                "label": "Outbox / inbox delivery",
                "kind": "trouble_area",
                "path_id": "log/delivery",
                "keywords": ["outbox", "inbox", "delivered", "history"],
                "suggested_next_action": "trace message lifecycle",
                "attention": "medium",
            },
            "ble": {
                "label": "Bluetooth adapter",
                "kind": "trouble_area",
                "path_id": "log/ble",
                "keywords": ["ble", "bluetooth", "btleplug", "gatt"],
                "suggested_next_action": None,
                "attention": "low",
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


class LogPackSchemaTests(unittest.TestCase):
    def test_valid_pack_passes_and_normalizes(self):
        doc = validate_log_pack(sample_log_pack())
        self.assertEqual(doc["id"], "scmessenger-ops-log-v1")
        self.assertEqual(set(doc["buckets"]), {"transport", "delivery", "ble"})
        self.assertEqual(doc["score"]["id"], "sentiment")
        self.assertEqual(len(doc["score"]["levels"]), 4)
        # Clean copy: bucket entries normalized by the P5 validator.
        self.assertEqual(doc["buckets"]["transport"]["path_id"], "log/transport")

    def test_bucket_rules_still_enforced_via_p5_validator(self):
        pack = sample_log_pack()
        pack["buckets"]["ble"]["kind"] = "invented_kind"
        with self.assertRaises(ValueError):
            validate_log_pack(pack)

    def test_missing_or_nonobject_score_block_refused(self):
        pack = sample_log_pack()
        del pack["score"]
        with self.assertRaises(ValueError):
            validate_log_pack(pack)
        pack["score"] = ["not", "an", "object"]
        with self.assertRaises(ValueError):
            validate_log_pack(pack)

    def test_score_id_rules(self):
        pack = sample_log_pack()
        pack["score"]["id"] = ""
        with self.assertRaises(ValueError):
            validate_log_pack(pack)
        pack["score"]["id"] = "bucket"
        with self.assertRaises(ValueError):
            validate_log_pack(pack)

    def test_score_instructions_required(self):
        pack = sample_log_pack()
        pack["score"]["instructions"] = ""
        with self.assertRaises(ValueError):
            validate_log_pack(pack)
        pack["score"]["instructions"] = 7
        with self.assertRaises(ValueError):
            validate_log_pack(pack)

    def test_score_levels_must_be_at_least_two_unique_strings(self):
        pack = sample_log_pack()
        pack["score"]["levels"] = ["only-one"]
        with self.assertRaises(ValueError):
            validate_log_pack(pack)
        pack["score"]["levels"] = ["a", ""]
        with self.assertRaises(ValueError):
            validate_log_pack(pack)
        pack["score"]["levels"] = ["a", "a"]
        with self.assertRaises(ValueError):
            validate_log_pack(pack)
        pack["score"]["levels"] = "not-a-list"
        with self.assertRaises(ValueError):
            validate_log_pack(pack)

    def test_question_pack_criteria_are_operator_declared_only(self):
        questions = log_factor_question_pack(sample_log_pack())
        self.assertEqual(questions["bucket"]["type"], "choice")
        self.assertEqual(set(questions["bucket"]["criteria"]),
                         {"transport", "delivery", "ble"})
        self.assertEqual(questions["bucket"]["criteria"]["transport"],
                         "Transport / swarm health")
        self.assertEqual(questions["sentiment"]["type"], "score")
        levels = sample_log_pack()["score"]["levels"]
        self.assertEqual(questions["sentiment"]["criteria"], levels)
        # Typed primitives only.
        for question in questions.values():
            self.assertIn(question["type"], ("noul", "choice", "score"))
            self.assertIn("instructions", question)
            self.assertIn("criteria", question)


class LogItemExtractorTests(unittest.TestCase):
    def test_warn_error_selected_info_not_by_default(self):
        items = extract_log_items(LOG_FIXTURE)
        levels = [item["level"] for item in items]
        self.assertEqual(levels, ["warn", "warn", "error", "warn"])

    def test_header_level_is_authoritative_over_message_text(self):
        """The real log contains an INFO record whose message mentions
        [ERROR]; the item must stay INFO (and therefore unselected)."""
        items = extract_log_items(LOG_FIXTURE)
        texts = " ".join(item["text"] for item in items)
        self.assertNotIn("Disconnected from 12D3KooW", texts)

    def test_continuation_lines_attach_only_to_selected_items(self):
        items = extract_log_items(LOG_FIXTURE)
        ble_daemon = items[0]
        self.assertIn("details: adapter probe failed twice", ble_daemon["text"])
        # The auto-reply INFO at the top is unselected; its body lines must
        # not leak into anything.
        for item in items:
            self.assertNotIn("auto-reply", item["text"])
        # Error item keeps its two bounded context lines.
        error_item = [i for i in items if i["level"] == "error"][0]
        self.assertIn("context line one", error_item["text"])
        self.assertIn("context line two", error_item["text"])

    def test_unselected_header_closes_continuation(self):
        # WARN followed by INFO (unselected) then junk lines: the junk must
        # not attach to the WARN.
        text = "\n".join([
            "2026-09-21T00:00:00.000000Z  WARN m::a: first problem",
            "2026-09-21T00:00:01.000000Z  INFO m::b: routine",
            "  junk that belongs to the INFO record",
        ])
        items = extract_log_items(text)
        self.assertEqual(len(items), 1)
        self.assertNotIn("junk", items[0]["text"])

    def test_continuation_bounded(self):
        lines = ["2026-09-21T00:00:00.000000Z  WARN m::a: problem"]
        lines += ["  extra {}".format(i) for i in range(10)]
        items = extract_log_items("\n".join(lines))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["text"].count("\n"), 4)  # MAX_CONTINUATION_LINES

    def test_evidence_refs_are_code_owned_line_numbers(self):
        items = extract_log_items(LOG_FIXTURE)
        first = items[0]
        self.assertEqual(first["line_index"], 5)
        self.assertEqual(first["evidence"], "line 5 [warn] scmessenger_cli::ble_daemon")
        self.assertTrue(first["id"])

    def test_info_sample_selects_every_nth_info(self):
        # Base selection is WARN/ERROR; ``info_sample=N`` ADDS every Nth INFO
        # (1-based) on top. The 3rd INFO in the fixture is line 8.
        items = extract_log_items(LOG_FIXTURE, levels=("warn", "error"),
                                  info_sample=3)
        info_items = [i for i in items if i["level"] == "info"]
        self.assertEqual([i["line_index"] for i in info_items], [8])

    def test_max_items_cap(self):
        items = extract_log_items(LOG_FIXTURE, max_items=2)
        self.assertEqual(len(items), 2)

    def test_input_validation(self):
        self.assertEqual(extract_log_items(None), [])
        with self.assertRaises(ValueError):
            extract_log_items(42)
        self.assertEqual(extract_log_items(""), [])

    def test_mechanical_tallies_without_model_spend(self):
        items = extract_log_items(LOG_FIXTURE)
        tallies = mechanical_tallies(items, LOG_FIXTURE)
        self.assertEqual(tallies["total_lines"], len(LOG_FIXTURE.splitlines()))
        self.assertEqual(tallies["selected_items"], 4)
        self.assertEqual(tallies["by_level"], {"error": 1, "warn": 3})
        self.assertEqual(tallies["top_modules"][0]["module"],
                         "scmessenger_cli::ble_mesh")
        self.assertEqual(tallies["top_modules"][0]["count"], 2)


if __name__ == "__main__":
    unittest.main()
