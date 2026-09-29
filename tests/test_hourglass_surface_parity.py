"""Parser and MCP contract for the public Hourglass plan inputs."""
import contextlib
import io
import unittest

from harness.cli_parser import HOURGLASS_STAGES, build_parser
from harness.mcp_schemas import TOOL_SCHEMAS


class HourglassSurfaceParityTests(unittest.TestCase):
    def setUp(self):
        self.plan_schema = next(
            tool["inputSchema"] for tool in TOOL_SCHEMAS
            if tool["name"] == "plan_and_execute")
        self.plan_parser = build_parser()

    def test_cli_and_mcp_expose_equivalent_names_and_stage_values(self):
        cli_action_names = {
            action.dest for action in self.plan_parser._subparsers._group_actions[0]
            .choices["plan"]._actions
        }
        self.assertTrue({"hourglass_stages", "max_input_tokens",
                         "max_output_tokens"}.issubset(cli_action_names))
        properties = self.plan_schema["properties"]
        self.assertEqual(
            properties["hourglass_stages"]["items"]["enum"],
            list(HOURGLASS_STAGES))
        self.assertEqual(properties["hourglass_stages"]["type"], "array")
        self.assertTrue(properties["hourglass_stages"]["uniqueItems"])
        self.assertTrue({"hourglass_stages", "max_input_tokens",
                         "max_output_tokens"}.issubset(properties))
        for name in ("max_input_tokens", "max_output_tokens"):
            self.assertEqual(properties[name]["type"], "integer")
            self.assertEqual(properties[name]["minimum"], 0)

    def test_cli_accepts_repeated_stage_selection_and_token_caps(self):
        args = self.plan_parser.parse_args([
            "plan", "--goal", "ship", "--hourglass-stage", "planning",
            "--hourglass-stage", "execution", "--max-input-tokens", "1200",
            "--max-output-tokens", "300"])
        self.assertEqual(args.hourglass_stages, ["planning", "execution"])
        self.assertEqual(args.max_input_tokens, 1200)
        self.assertEqual(args.max_output_tokens, 300)

    def test_cli_rejects_unknown_stage_and_invalid_token_caps(self):
        for option, value in (("--hourglass-stage", "invented"),
                              ("--max-input-tokens", "-1"),
                              ("--max-output-tokens", "1.5")):
            with self.subTest(option=option, value=value):
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    self.plan_parser.parse_args(["plan", "--goal", "x", option, value])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.plan_parser.parse_args([
                "plan", "--goal", "x", "--hourglass-stage", "planning",
                "--hourglass-stage", "planning"])

if __name__ == "__main__":
    unittest.main()
