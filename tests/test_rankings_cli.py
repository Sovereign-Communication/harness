"""Pin the rankings workflow's CLI invocations against the real parser.

Why this module exists: `.github/workflows/rankings.yml` is `schedule`-triggered
(Mondays 06:00 UTC) and is *not* part of the required `ci` workflow, so a broken
invocation there merges green and is only discovered when a scheduled run fails.
That is not hypothetical: the workflow passed `--json` to `harness rankings`,
which has no such flag, and every run since 2026-09-21 exited 2 with
"unrecognized arguments" -- so the weekly evidence artifact had not been
produced for a week and nothing in the merge path noticed.

The defect class is "a workflow calls a subcommand with a flag the subcommand
does not define". Rather than pinning the two literal lines that happen to be
broken today, these tests parse every `harness ...` invocation in the workflow
and check each flag against `harness.cli_parser`'s real subparser, so the next
flag rename or subcommand change is caught by the required suite instead of by a
Monday morning alert.

Deliberately stdlib-only, for the same reason as `tests/test_publish_workflow.py`:
the project declares zero runtime dependencies, CI installs only the `dev`
extras, and PyYAML is not among them -- so `import yaml` here would pass on a
developer machine and go red in CI. The reader below is a narrow scanner for the
subset of YAML this workflow uses, and it raises on a shape it does not
understand rather than silently matching less, because a reader that quietly
stopped matching would turn every assertion below into a no-op that always
passes -- the precise failure these tests exist to prevent.
"""

import re
import shlex
import unittest
from pathlib import Path

from harness.cli_parser import build_parser

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "rankings.yml"

# `harness <subcommand> ...` at the start of a `run:` script line, with
# optional line-continuation backslashes already joined by the caller.
_INVOCATION = re.compile(
    r"^\s*(?:harness|python\s+-m\s+harness\.cli)\s+"
    r"(?P<args>[^\n#]+)$", re.MULTILINE)


def _run_script_lines():
    """Yield the text of every `run: |` block in the workflow, continuations
    joined so a wrapped invocation is seen as the single command a shell runs.
    """
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if stripped in ("run: |", "run: |-", "run: >-"):
            base = len(lines[index]) - len(lines[index].lstrip(" "))
            body = []
            index += 1
            while index < len(lines):
                raw = lines[index]
                if raw.strip() and not raw.lstrip().startswith("#"):
                    indent = len(raw) - len(raw.lstrip(" "))
                    if indent <= base:
                        break
                    body.append(raw[base + 2:] if raw[base:base + 2] == "  " else raw.strip())
                index += 1
            joined = ""
            for entry in body:
                if joined.endswith("\\"):
                    joined = joined[:-1] + " " + entry.strip()
                else:
                    joined = entry if not joined else joined + "\n" + entry
            yield joined
        else:
            index += 1


def _parse_invocation_args(args):
    """Return argv for a CLI invocation, rejecting unresolved shell variables."""
    # GitHub expressions and command substitutions are deliberately replaced
    # below; any remaining `$` is an unresolved shell expansion. Treat it as
    # a parse error instead of letting a variable token disappear from the
    # flag checks.
    cleaned = re.sub(r"\$\{\{[^}]*\}\}", "PLACEHOLDER", args)
    cleaned = re.sub(r"\$\([^)]*\)", "PLACEHOLDER", cleaned)
    cleaned = cleaned.rstrip("\\").strip()
    if "$" in cleaned:
        raise ValueError(f"unresolved shell variable in CLI invocation: {cleaned}")
    return shlex.split(cleaned)


def _invocations():
    """[(subcommand, [flags])] for each CLI call in the workflow."""
    found = []
    for script in _run_script_lines():
        for line in script.splitlines():
            match = _INVOCATION.match(line)
            if not match:
                continue
            tokens = _parse_invocation_args(match.group("args"))
            if not tokens:
                continue
            subcommand = tokens[0]
            flags = [t for t in tokens[1:] if t.startswith("--")]
            found.append((subcommand, flags))
    return found


def _subparser(subcommand):
    """The real argparse subparser for `subcommand`, or None if absent."""
    parser = build_parser()
    for action in parser._subparsers._group_actions if parser._subparsers else []:
        if subcommand in action.choices:
            return action.choices[subcommand]
    return None


class RankingsWorkflowInvocationTests(unittest.TestCase):
    """The workflow's own invocations, checked against the shipped parser."""

    def test_workflow_actually_invokes_the_cli(self):
        """Guard the reader itself: no parsed invocations means every test
        below is vacuous, which is the failure mode this module exists to
        avoid."""
        invocations = _invocations()
        self.assertTrue(invocations,
                        "found no `harness ...` invocations in rankings.yml; "
                        "the reader stopped matching and these tests would "
                        "silently pass")
        self.assertIn("rankings", [sub for sub, _ in invocations])

    def test_unresolved_shell_variables_are_parse_errors(self):
        with self.assertRaisesRegex(ValueError, "unresolved shell variable"):
            _parse_invocation_args("rankings $EXTRA --out report.json")

    def test_quoted_paths_and_python_module_invocations_keep_flags_visible(self):
        cases = (
            ('harness rankings --out "reports/weekly report.json" --bogus',
             "reports/weekly report.json"),
            ("python -m harness.cli rankings --out reports/out.json --bogus",
             "reports/out.json"),
        )
        for command, expected_path in cases:
            with self.subTest(command=command):
                match = _INVOCATION.match(command)
                self.assertIsNotNone(match)
                tokens = _parse_invocation_args(match.group("args"))
                subcommand = tokens[0]
                flags = [token for token in tokens[1:]
                         if token.startswith("--")]
                self.assertEqual(subcommand, "rankings")
                self.assertIn(expected_path, tokens)
                self.assertEqual(flags, ["--out", "--bogus"])
                accepted = _subparser(subcommand)._option_string_actions
                self.assertIn("--out", accepted)
                self.assertNotIn("--bogus", accepted)

    def test_every_invoked_subcommand_exists(self):
        for subcommand, _ in _invocations():
            with self.subTest(subcommand=subcommand):
                self.assertIsNotNone(
                    _subparser(subcommand),
                    f"rankings.yml invokes `harness {subcommand}`, which is "
                    "not a subcommand of the shipped CLI")

    def test_every_flag_is_accepted_by_its_subcommand(self):
        """The regression this module exists for: a workflow passing a flag
        the subcommand does not define fails only when a scheduled run
        executes, weeks later and outside the merge path."""
        for subcommand, flags in _invocations():
            subparser = _subparser(subcommand)
            if subparser is None:
                continue
            accepted = set()
            for action in subparser._actions:
                accepted.update(action.option_strings)
            for flag in flags:
                with self.subTest(subcommand=subcommand, flag=flag):
                    self.assertIn(
                        flag, accepted,
                        f"rankings.yml passes {flag} to `harness {subcommand}`, "
                        f"which does not accept it (accepts: {sorted(accepted)})")

    def test_rankings_is_not_given_a_capabilities_only_json_flag(self):
        """`--json` on `capabilities` suppresses a table renderer; `rankings`
        has none, so the flag is meaningless there rather than merely
        misplaced. This is the literal regression -- a workflow and a subcommand
        whose surfaces had drifted apart."""
        subparser = _subparser("rankings")
        self.assertIsNotNone(subparser)
        self.assertNotIn("--json", subparser._option_string_actions,
                         "if `rankings` grows --json, update the workflow and "
                         "drop this assertion deliberately")
        for subcommand, flags in _invocations():
            if subcommand == "rankings":
                self.assertNotIn("--json", flags)

    def test_both_report_variants_write_an_artifact(self):
        """The no-probe and one-vote-probe runs are separate `if:` steps. The
        broken invocation lived in both, so pin that both still emit the JSON
        the artifact upload collects -- a fix that only corrected the default
        path would leave the operator-invoked probe path red."""
        with_out = [flags for sub, flags in _invocations()
                    if sub == "rankings" and "--out" in flags]
        self.assertEqual(len(with_out), 2,
                         "expected the no-probe and probe invocations to both "
                         "pass --out")

    def test_report_json_is_validated_before_required_artifact_upload(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        validate = workflow.index("- name: Validate generated report JSON")
        upload = workflow.index("- name: Upload report artifact")
        self.assertLess(validate, upload)
        validation_step = workflow[validate:upload]
        self.assertIn('glob.glob("rankings/rankings-*.json")', validation_step)
        self.assertIn("json.load(stream)", validation_step)
        upload_step = workflow[upload:]
        self.assertIn("if-no-files-found: error", upload_step)


if __name__ == "__main__":
    unittest.main()
