"""#284: every served MCP tool states When / In / Out, and a bad one fails."""
import copy
import unittest
from unittest import mock

from driver_core.audit import AuditError, lint_tool_descriptions
from harness.mcp import McpServer
from harness.mcp_schemas import TOOL_SCHEMAS


class McpDescriptionFormulaTests(unittest.TestCase):
    def test_every_registered_tool_states_when_in_and_out(self):
        checked = lint_tool_descriptions(TOOL_SCHEMAS)
        self.assertEqual(checked, len(TOOL_SCHEMAS))
        self.assertEqual(len(TOOL_SCHEMAS), 33)
        for schema in TOOL_SCHEMAS:
            text = " ".join(schema["description"].split())
            self.assertTrue(text.startswith("When:"), schema["name"])
            self.assertLess(text.index("When:"), text.index("In:"), schema["name"])
            self.assertLess(text.index("In:"), text.index("Out:"), schema["name"])

    def test_serving_the_catalog_runs_the_lint(self):
        self.assertEqual(McpServer._tools(object()), TOOL_SCHEMAS)

    def test_catalog_serving_fails_on_a_non_conforming_catalog(self):
        broken = copy.deepcopy(TOOL_SCHEMAS)
        broken[0]["description"] = "Does the thing when asked."
        with mock.patch("harness.mcp.TOOL_SCHEMAS", broken):
            with self.assertRaises(AuditError):
                McpServer._tools(object())

    def test_deliberately_non_conforming_description_fails_the_audit(self):
        broken = copy.deepcopy(TOOL_SCHEMAS)
        broken[0]["description"] = "Does the thing when asked."
        with self.assertRaises(AuditError) as caught:
            lint_tool_descriptions(broken)
        message = str(caught.exception)
        self.assertIn(broken[0]["name"], message)
        self.assertIn("When:", message)

    def test_label_order_is_enforced(self):
        broken = copy.deepcopy(TOOL_SCHEMAS)
        broken[1]["description"] = "In: a prompt. When: needed. Out: an answer."
        with self.assertRaises(AuditError) as caught:
            lint_tool_descriptions(broken)
        self.assertIn("out of order", str(caught.exception))

    def test_empty_segment_is_refused(self):
        broken = copy.deepcopy(TOOL_SCHEMAS)
        broken[2]["description"] = "When: a question. In: Out: an answer."
        with self.assertRaises(AuditError) as caught:
            lint_tool_descriptions(broken)
        self.assertIn("In: is empty", str(caught.exception))

    def test_missing_description_is_refused(self):
        broken = copy.deepcopy(TOOL_SCHEMAS)
        broken[3]["description"] = ""
        with self.assertRaises(AuditError) as caught:
            lint_tool_descriptions(broken)
        self.assertIn("description is missing", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
