import hashlib
import io
import json
import unittest

from harness.lucas_gate import BIAS_MINIMUMS, CATEGORY_LABELS, run_typed_track
from harness.mcp import McpServer
from harness.mcp_schemas import TOOL_SCHEMAS


def scorecard():
    return {
        "track": "typed",
        "artifact_text": "Draft for a typed-track gate smoke check.",
        "overall_average": 9.4,
        "actual_risk": False,
        "category_scores": [
            {"id": category_id, "score": 9.0, "category_average": 9.0,
             "evidence": "A specific feature in the draft.",
             "precedent": "A matching profile precedent."}
            for category_id in CATEGORY_LABELS
        ],
        "bias_checks": [
            {"id": bias_id, "score": float(minimum),
             "rationale": "The draft satisfies this bias check."}
            for bias_id, minimum in BIAS_MINIMUMS.items()
        ],
    }


class LucasGateV2Tests(unittest.TestCase):
    def test_pass_binds_result_to_artifact_without_echoing_evidence(self):
        packet = scorecard()
        result = run_typed_track(**packet)

        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["category_ids"], list(CATEGORY_LABELS))
        self.assertEqual(result["applicable_category_ids"], list(CATEGORY_LABELS))
        self.assertEqual(result["not_applicable_categories"], [])
        self.assertEqual(result["bias_ids"], list(BIAS_MINIMUMS))
        self.assertEqual(result["artifact_sha256"], hashlib.sha256(
            packet["artifact_text"].encode("utf-8")).hexdigest())
        self.assertFalse(result["model_called"])
        self.assertFalse(result["product_jev_run"])
        self.assertNotIn("precedent", result)
        self.assertNotIn("evidence", result)

    def test_not_applicable_category_is_not_failed_or_scored(self):
        packet = scorecard()
        packet["category_scores"][0] = {
            "id": "1", "applicable": False,
            "not_applicable_reason": "This artifact contains no spoken sample.",
        }

        result = run_typed_track(**packet)

        self.assertEqual(result["status"], "pass")
        self.assertNotIn("1", result["applicable_category_ids"])
        self.assertEqual(result["not_applicable_categories"], ["1"])
        self.assertEqual(result["failing_categories"], [])
        self.assertNotIn("not_applicable_reason", result)

    def test_all_categories_not_applicable_is_not_a_failure_or_pass(self):
        packet = scorecard()
        packet["overall_average"] = None
        packet["category_scores"] = [
            {"id": category_id, "applicable": False,
             "not_applicable_reason": "No matching artifact evidence."}
            for category_id in CATEGORY_LABELS
        ]

        result = run_typed_track(**packet)

        self.assertEqual(result["status"], "not_applicable")
        self.assertEqual(result["failing_categories"], [])
        self.assertEqual(result["not_applicable_categories"],
                         list(CATEGORY_LABELS))

    def test_not_applicable_category_rejects_scores_and_missing_reason(self):
        packet = scorecard()
        packet["category_scores"][0].update(
            applicable=False, not_applicable_reason="No matching evidence.")
        with self.assertRaisesRegex(ValueError, "cannot include scores or evidence"):
            run_typed_track(**packet)

        packet["category_scores"][0] = {"id": "1", "applicable": False}
        with self.assertRaisesRegex(ValueError, "not_applicable_reason"):
            run_typed_track(**packet)

    def test_rounding_exceptions_are_exact(self):
        packet = scorecard()
        packet["category_scores"][0].update(
            score=8.95, category_average=9.25)
        self.assertEqual(run_typed_track(**packet)["status"], "pass")

        packet["category_scores"][0]["category_average"] = 9.24
        packet["overall_average"] = 9.9
        self.assertEqual(run_typed_track(**packet)["status"], "revise")

        packet["category_scores"][0].update(score=8.9, category_average=9.49)
        self.assertEqual(run_typed_track(**packet)["status"], "revise")

        packet["category_scores"][0].update(score=8.9, category_average=9.5)
        packet["overall_average"] = 9.4
        self.assertEqual(run_typed_track(**packet)["status"], "pass")

        packet["category_scores"][0].update(score=8.8, category_average=8.8)
        packet["overall_average"] = 9.89
        self.assertEqual(run_typed_track(**packet)["status"], "revise")

        packet["overall_average"] = 9.9
        self.assertEqual(run_typed_track(**packet)["status"], "pass")

    def test_rounding_transition_boundaries_do_not_round_up(self):
        packet = scorecard()
        packet["category_scores"][0].update(
            score=8.949, category_average=9.25)
        packet["overall_average"] = 9.49
        self.assertEqual(run_typed_track(**packet)["status"], "revise")
        packet["category_scores"][0]["category_average"] = 9.5
        self.assertEqual(run_typed_track(**packet)["status"], "pass")

        packet["category_scores"][0].update(
            score=8.899, category_average=9.5)
        packet["overall_average"] = 9.89
        self.assertEqual(run_typed_track(**packet)["status"], "revise")
        packet["overall_average"] = 9.9
        self.assertEqual(run_typed_track(**packet)["status"], "pass")

    def test_investigated_exception_includes_only_one_category_at_endpoints(self):
        for score in (8.5, 8.9):
            with self.subTest(score=score):
                packet = scorecard()
                packet["category_scores"][0]["score"] = score
                packet["investigated_reason"] = "Reviewed the mismatch."
                self.assertEqual(
                    run_typed_track(**packet)["status"], "pass_with_note")

        for score in (8.49, 8.91):
            with self.subTest(score=score):
                packet = scorecard()
                packet["category_scores"][0]["score"] = score
                packet["investigated_reason"] = "Reviewed the mismatch."
                self.assertEqual(run_typed_track(**packet)["status"], "revise")

        packet = scorecard()
        packet["category_scores"][0]["score"] = 8.7
        packet["category_scores"][1]["score"] = 8.7
        packet["investigated_reason"] = "Reviewed the mismatch."
        self.assertEqual(run_typed_track(**packet)["status"], "revise")

    def test_single_investigated_near_miss_passes_with_note(self):
        packet = scorecard()
        packet["category_scores"][0]["score"] = 8.7
        packet["investigated_reason"] = "Reviewed the mismatch against the draft."
        result = run_typed_track(**packet)
        self.assertEqual(result["status"], "pass_with_note")
        self.assertTrue(result["investigated_exception"])
        self.assertEqual(result["investigated_category"], "1")
        self.assertNotIn("investigated_reason", result)
        self.assertEqual(result["reasons"], [
            "one category passed the investigated 8.5\u20138.9 exception"])

    def test_revise_result_identifies_failed_category_and_bias_floor(self):
        packet = scorecard()
        packet["category_scores"][0]["score"] = 8.49
        packet["bias_checks"][0]["score"] = 9.99

        result = run_typed_track(**packet)

        self.assertEqual(result["status"], "revise")
        self.assertEqual(result["failing_categories"], [
            {"id": "1", "score": 8.49}])
        self.assertEqual(result["failing_bias_checks"], [
            {"id": "B1", "score": 9.99, "minimum": 10.0}])

    def test_low_score_bias_failure_and_actual_risk_do_not_pass(self):
        packet = scorecard()
        packet["category_scores"][0]["score"] = 8.49
        packet["investigated_reason"] = "A reason does not waive the lower bar."
        self.assertEqual(run_typed_track(**packet)["status"], "revise")

        for bias_id, minimum in BIAS_MINIMUMS.items():
            with self.subTest(bias_id=bias_id):
                packet = scorecard()
                row = next(item for item in packet["bias_checks"]
                           if item["id"] == bias_id)
                row["score"] = float(minimum) - 0.01
                self.assertEqual(run_typed_track(**packet)["status"], "revise")

        packet = scorecard()
        packet["actual_risk"] = True
        self.assertEqual(run_typed_track(**packet)["status"], "blocked")

    def test_rejects_incomplete_scorecards_and_non_typed_track(self):
        packet = scorecard()
        packet["category_scores"].pop()
        with self.assertRaisesRegex(ValueError, "missing required ids"):
            run_typed_track(**packet)

        packet = scorecard()
        packet["track"] = "planning"
        with self.assertRaisesRegex(ValueError, "only the typed track"):
            run_typed_track(**packet)

    def test_mcp_call_route_is_registered_and_callable(self):
        server = McpServer(transport=None, api_key=None, governor=None,
                           ledger=None, router=None, engine=None)
        schema = next(tool for tool in server._tools()
                      if tool["name"] == "lucas_gate_v2")
        self.assertTrue(schema["annotations"]["readOnlyHint"])
        category_schema = schema["inputSchema"]["properties"][
            "category_scores"]["items"]
        self.assertIn("applicable", category_schema["properties"])
        self.assertNotIn("overall_average", schema["inputSchema"]["required"])
        self.assertIn("lucas_gate_v2", [tool["name"] for tool in TOOL_SCHEMAS])

        reply = server._call_tool_impl({
            "jsonrpc": "2.0", "id": "gate-smoke",
            "params": {"name": "lucas_gate_v2", "arguments": scorecard()},
        })
        self.assertFalse(reply["result"]["isError"])
        self.assertEqual(reply["result"]["structuredContent"]["status"], "pass")

    def test_stdio_initialize_list_and_reordered_call(self):
        packet = scorecard()
        packet["category_scores"][0] = {
            "id": "1", "applicable": False,
            "not_applicable_reason": "No spoken sample in this artifact.",
        }
        packet["category_scores"].reverse()
        packet["bias_checks"].reverse()
        packet["artifact_text"] = "  {}\n".format(packet["artifact_text"])
        expected_artifact_hash = hashlib.sha256(
            packet["artifact_text"].encode("utf-8")).hexdigest()

        note_packet = scorecard()
        note_packet["category_scores"][0]["score"] = 8.7
        note_packet["investigated_reason"] = "SYNTHETIC_KEY_LIKE_SENTINEL_7f1a"

        revise_packet = scorecard()
        revise_packet["category_scores"][0]["score"] = 8.49
        revise_packet["bias_checks"][0]["score"] = 9.99

        empty_packet = scorecard()
        empty_packet["category_scores"] = []
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18",
                        "clientInfo": {"name": "gate-test", "version": "1"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "lucas_gate_v2", "arguments": packet}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
             "params": {"name": "lucas_gate_v2", "arguments": note_packet}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
             "params": {"name": "lucas_gate_v2", "arguments": revise_packet}},
            {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
             "params": {"name": "lucas_gate_v2", "arguments": empty_packet}},
        ]
        stdin = io.StringIO("\n".join(json.dumps(item) for item in requests) + "\n")
        stdout = io.StringIO()
        server = McpServer(transport=None, api_key=None, governor=None,
                           ledger=None, router=None, engine=None,
                           stdin=stdin, stdout=stdout)

        server.serve_forever()

        responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual([item["id"] for item in responses], [1, 2, 3, 4, 5, 6])
        listed_tools = responses[1]["result"]["tools"]
        self.assertIn("lucas_gate_v2", [item["name"] for item in listed_tools])
        pass_result = responses[2]["result"]["structuredContent"]
        self.assertEqual(pass_result["status"], "pass")
        self.assertEqual(pass_result["artifact_sha256"], expected_artifact_hash)

        note_result = responses[3]["result"]["structuredContent"]
        self.assertEqual(note_result["status"], "pass_with_note")
        self.assertNotIn("investigated_reason", note_result)
        self.assertNotIn(note_packet["investigated_reason"],
                         json.dumps(responses[3]))

        revise_result = responses[4]["result"]["structuredContent"]
        self.assertEqual(revise_result["status"], "revise")
        self.assertEqual(revise_result["failing_categories"],
                         [{"id": "1", "score": 8.49}])
        self.assertEqual(revise_result["failing_bias_checks"],
                         [{"id": "B1", "score": 9.99, "minimum": 10.0}])
        self.assertTrue(responses[5]["result"]["isError"])

        self.assertFalse(server._inflight)
        self.assertFalse(server._cancelled)
        self.assertFalse(server._starts)
        self.assertFalse(server._progress)

    def test_mcp_rejects_unrecognized_top_level_and_row_fields(self):
        server = McpServer(transport=None, api_key=None, governor=None,
                           ledger=None, router=None, engine=None)
        packets = []
        top_level = scorecard()
        top_level["unexpected"] = "extra"
        packets.append(("top-level", top_level))
        category_field = scorecard()
        category_field["category_scores"][0]["unexpected"] = "extra"
        packets.append(("category", category_field))
        bias_field = scorecard()
        bias_field["bias_checks"][0]["unexpected"] = "extra"
        packets.append(("bias", bias_field))

        for label, packet in packets:
            with self.subTest(case=label):
                reply = server._call_tool_impl({
                    "jsonrpc": "2.0", "id": "strict-input",
                    "params": {"name": "lucas_gate_v2", "arguments": packet},
                })
                self.assertTrue(reply["result"]["isError"])

    def test_empty_artifact_and_score_arrays_fail_through_mcp_call(self):
        server = McpServer(transport=None, api_key=None, governor=None,
                           ledger=None, router=None, engine=None)
        packets = [scorecard(), scorecard()]
        packets[0]["artifact_text"] = "   "
        packets[1]["category_scores"] = []

        for index, packet in enumerate(packets):
            with self.subTest(case=index):
                reply = server._call_tool_impl({
                    "jsonrpc": "2.0", "id": "invalid-input",
                    "params": {"name": "lucas_gate_v2", "arguments": packet},
                })
                self.assertTrue(reply["result"]["isError"])


if __name__ == "__main__":
    unittest.main()
