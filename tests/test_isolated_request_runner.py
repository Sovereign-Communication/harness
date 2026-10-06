"""Hermetic contract tests for the headless isolated-request runner."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest import TestCase, mock


_SCRIPT = (Path(__file__).resolve().parents[1]
           / ".claude" / "skills" / "isolated-request" / "run.py")
_SPEC = importlib.util.spec_from_file_location("isolated_request_runner", _SCRIPT)
assert _SPEC and _SPEC.loader
runner = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(runner)


class IsolatedRequestRunnerTests(TestCase):
    def test_default_model_is_inherited_by_omitting_model_flag(self):
        opts = SimpleNamespace(
            write=False, model=None, budget=1.0, allow=None, json_schema=None,
        )

        command = runner.build_command(opts, "claude")

        self.assertNotIn("--model", command)

    def test_explicit_arbitrary_model_is_forwarded(self):
        opts = SimpleNamespace(
            write=False, model="custom/provider-model", budget=1.0,
            allow=None, json_schema=None,
        )

        command = runner.build_command(opts, "claude")

        self.assertEqual(command[command.index("--model") + 1], "custom/provider-model")

    def test_result_reports_model_from_cli_usage_envelope(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            prompt = Path(temp_dir) / "prompt.txt"
            prompt.write_text("hello", encoding="utf-8")
            payload = {
                "is_error": False,
                "result": "done",
                "modelUsage": {"claude-model-actual": {"inputTokens": 1}},
                "total_cost_usd": 0.01,
            }
            completed = SimpleNamespace(
                returncode=0, stdout=json.dumps(payload), stderr="",
            )
            stdout = io.StringIO()
            with mock.patch.object(runner.shutil, "which", return_value="claude"), \
                    mock.patch.object(runner.subprocess, "run", return_value=completed) as run, \
                    contextlib.redirect_stdout(stdout):
                exit_code = runner.main(["--prompt-file", str(prompt)])

        self.assertEqual(exit_code, 0)
        self.assertNotIn("--model", run.call_args.args[0])
        self.assertEqual(json.loads(stdout.getvalue())["model"], "claude-model-actual")
