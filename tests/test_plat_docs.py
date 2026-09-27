"""PLAT-docs: the platform story in the docs must match the code.

The spec for this row is a lint rule: links in docs resolve, and no command
example uses a form the code does not support. Documentation drifts silently
-- a doc that says a control exists when the code dropped it is worse than no
doc, because a reviewer trusts it. These tests make the two claims that are
checkable, checkable:

- every relative markdown link in the platform-facing docs resolves;
- the documented platform divergences (key-file perms on Windows, port-reuse
  on Windows, LF evidence, case-insensitive roots) each name the osal symbol
  that implements them, so a doc cannot describe a control the code lacks;
- no doc carries a hardcoded interpreter path for a gate command, because that
  is exactly what made the old docs correct on one OS only.
"""
import os
import re
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DOCS = ("README.md", "CONTRIBUTING.md", "CLAUDE.md", "AGENTS.md",
        "docs/security.md", "docs/mcp.md", "docs/architecture.md")

# (doc, substring, osal symbol that must exist) -- the doc may only claim a
# platform behaviour if the code still implements it.
CLAIMS = (
    ("docs/security.md", "not modelled", "keyfile_mode"),
    ("docs/security.md", "HARDEN_REUSE", "HARDEN_REUSE"),
    ("docs/security.md", "is_within", "is_within"),
    ("README.md", "HARDEN_REUSE", "HARDEN_REUSE"),
    ("README.md", "keyfile_mode", "keyfile_mode"),
    ("README.md", "is_within", "is_within"),
)

GATE_COMMAND_RE = re.compile(
    r"[\w./\\-]*(\.venv[/\\]Scripts[/\\]python(\.exe)?|\.venv[/\\]bin[/\\]python)"
    r"[^\n]*?(-m\s+ruff|compileall|unittest\s+discover|jev-phase|audit\.py)")


def _read(rel):
    with open(os.path.join(REPO_ROOT, rel), encoding="utf-8") as handle:
        return handle.read()


class DocLinkTest(unittest.TestCase):
    def test_relative_links_resolve(self):
        broken = []
        for rel in DOCS:
            path = os.path.join(REPO_ROOT, rel)
            base = os.path.dirname(path)
            for match in re.finditer(r"\[[^\]]+\]\(([^)]+)\)", _read(rel)):
                target = match.group(1).split("#")[0].strip()
                if not target or target.startswith(("http://", "https://", "mailto:")):
                    continue
                if not os.path.exists(os.path.join(base, target)):
                    broken.append(f"{rel}: {target}")
        self.assertEqual(broken, [], "unresolved doc links:\n  " + "\n  ".join(broken))

    def test_the_security_anchor_the_docs_link_to_exists(self):
        text = _read("docs/security.md")
        self.assertIn("## Platform-specific protections (and their limits)", text)
        # mcp.md links to that heading by anchor.
        self.assertIn("security.md#platform-specific-protections-and-their-limits",
                      _read("docs/mcp.md"))


class DocClaimMatchesCodeTest(unittest.TestCase):
    def test_every_claimed_divergence_names_a_live_symbol(self):
        from harness import osal
        for rel, needle, symbol in CLAIMS:
            self.assertIn(needle, _read(rel),
                          f"{rel} must document {needle!r}")
            self.assertTrue(hasattr(osal, symbol),
                            f"{rel} claims {symbol}, which harness/osal.py "
                            "no longer has -- fix the doc or restore the control")

    def test_platform_table_covers_every_os(self):
        readme = _read("README.md")
        self.assertIn("## Platform support", readme)
        for platform in ("Linux", "macOS", "Windows"):
            self.assertIn(platform, readme)

    def test_keyfile_and_port_divergences_are_stated_in_the_right_docs(self):
        security = _read("docs/security.md")
        self.assertIn("Windows has", security)          # key-file ACL reality
        self.assertIn("second", security.lower())        # port hijack reasoning
        self.assertIn("TIME_WAIT", security)             # why POSIX keeps it

    def test_mcp_doc_covers_both_venv_layouts(self):
        mcp = _read("docs/mcp.md")
        self.assertIn("Scripts/python.exe", mcp)
        self.assertIn("bin/python", mcp)
        self.assertIn("-m harness.mcp", mcp)

    def test_contributing_states_the_line_ending_rule(self):
        contributing = _read("CONTRIBUTING.md")
        self.assertIn("Line endings", contributing)
        self.assertIn("LF", contributing)
        self.assertIn(".gitattributes", contributing)
        self.assertIn("eol=lf", contributing)

    def test_contributing_states_the_osal_boundary(self):
        contributing = _read("CONTRIBUTING.md")
        self.assertIn("harness/osal.py", contributing)
        for banned in ("subprocess", "os.name", "sys.platform", "webbrowser"):
            self.assertIn(banned, contributing)


class NoPlatformSpecificGateCommandTest(unittest.TestCase):
    def test_docs_never_hardcode_an_interpreter_for_a_gate(self):
        """PLAT-cmd-data: the commands are data; docs point at `harness gates`."""
        offenders = []
        for rel in DOCS:
            for line in _read(rel).splitlines():
                if GATE_COMMAND_RE.search(line):
                    # A line that also names `harness gates` is a pointer, not
                    # a second copy of the command.
                    if "harness gates" in line or "harness.cli gates" in line:
                        continue
                    offenders.append(f"{rel}: {line.strip()}")
        self.assertEqual(
            offenders, [],
            "a documented gate command hardcodes an interpreter path:\n  "
            + "\n  ".join(offenders))

    def test_the_gates_command_is_what_the_docs_point_at(self):
        for rel in ("CLAUDE.md", "CONTRIBUTING.md", "README.md"):
            self.assertIn("harness gates", _read(rel).replace("harness.cli gates",
                                                               "harness gates"),
                          f"{rel} must point contributors at the gates command")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
