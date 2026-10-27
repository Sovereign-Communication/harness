import hashlib
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
        self.assertEqual(result["bias_ids"], list(BIAS_MINIMUMS))
        self.assertEqual(result["artifact_sha256"], hashlib.sha256(
            packet["artifact_text"].encode("utf-8")).hexdigest())
        self.assertFalse(result["model_called"])
        self.assertFalse(result["product_jev_run"])
        self.assertNotIn("precedent", result)
        self.assertNotIn("evidence", result)

    def test_rounding_exceptions_are_exact(self):
        packet = scorecard()
        packet["category_scores"][0].update(
            score=8.95, category_average=9.25)
        self.assertEqual(run_typed_track(**packet)["status"], "pass")

        packet["category_scores"][0]["category_average"] = 9.24
        packet["overall_average"] = 9.9
        self.assertEqual(run_typed_track(**packet)["status"], "revise")

        packet["category_scores"][0].update(score=8.9, category_average=9.5)
        packet["overall_average"] = 9.4
        self.assertEqual(run_typed_track(**packet)["status"], "pass")

        packet["category_scores"][0].update(score=8.8, category_average=8.8)
        packet["overall_average"] = 9.9
        self.assertEqual(run_typed_track(**packet)["status"], "pass")

    def test_single_investigated_near_miss_passes_with_note(self):
        packet = scorecard()
        packet["category_scores"][0]["score"] = 8.7
        packet["investigated_reason"] = "Reviewed the mismatch against the draft."
        result = run_typed_track(**packet)
        self.assertEqual(result["status"], "pass_with_note")
        self.assertTrue(result["investigated_exception"])

    def test_low_score_bias_failure_and_actual_risk_do_not_pass(self):
        packet = scorecard()
        packet["category_scores"][0]["score"] = 8.49
        packet["investigated_reason"] = "A reason does not waive the lower bar."
        self.assertEqual(run_typed_track(**packet)["status"], "revise")

        packet = scorecard()
        packet["bias_checks"][0]["score"] = 9.99
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
        self.assertIn("lucas_gate_v2", [tool["name"] for tool in TOOL_SCHEMAS])

        reply = server._call_tool_impl({
            "jsonrpc": "2.0", "id": "gate-smoke",
            "params": {"name": "lucas_gate_v2", "arguments": scorecard()},
        })
        self.assertFalse(reply["result"]["isError"])
        self.assertEqual(reply["result"]["structuredContent"]["status"], "pass")


if __name__ == "__main__":
    unittest.main()
