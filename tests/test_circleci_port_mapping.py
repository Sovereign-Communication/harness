"""The CircleCI port's check mapping must stay true to the workflow it mirrors.

`docs/circleci-port.md` records which CircleCI job will carry each of the 11
required branch-protection contexts, read from the protection API on 2026-11-05.
A mapping that quietly stops matching `.github/workflows/ci.yml` is worse than
no mapping: the port would be built against a workflow that no longer exists.
These pins fail when the workflow renames a job, drops a matrix leg, or changes
a gate command the mapping claims to preserve.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "circleci-port.md"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

#: The merge bar, read from `GET /branches/main/protection` on 2026-11-05.
#: Changing this set is a branch-protection change, which is owner-reserved.
REQUIRED_CONTEXTS = (
    "Checkout hygiene",
    "audit",
    "hermetic suite (macos-latest, py3.11)",
    "hermetic suite (ubuntu-22.04, py3.9)",
    "hermetic suite (ubuntu-latest, py3.11)",
    "hermetic suite (ubuntu-latest, py3.13)",
    "hermetic suite (windows-latest, py3.11)",
    "installed package (macos-latest)",
    "installed package (ubuntu-latest)",
    "installed package (windows-latest)",
    "package",
)

#: context -> (github job id, planned CircleCI job name)
MAPPING = {
    "Checkout hygiene": ("checkout-hygiene", "checkout-hygiene"),
    "audit": ("audit", "audit"),
    "hermetic suite (macos-latest, py3.11)": ("test", "hermetic-macos-311"),
    "hermetic suite (ubuntu-22.04, py3.9)": ("test", "hermetic-ubuntu2204-39"),
    "hermetic suite (ubuntu-latest, py3.11)": ("test", "hermetic-ubuntu-311"),
    "hermetic suite (ubuntu-latest, py3.13)": ("test", "hermetic-ubuntu-313"),
    "hermetic suite (windows-latest, py3.11)": ("test", "hermetic-windows-311"),
    "installed package (macos-latest)": ("package-installed",
                                        "package-installed-macos"),
    "installed package (ubuntu-latest)": ("package-installed",
                                          "package-installed-ubuntu"),
    "installed package (windows-latest)": ("package-installed",
                                           "package-installed-windows"),
    "package": ("package", "package"),
}

#: Lane-only jobs: ported for parity, but not required contexts today.
LANE_ONLY = {
    "Change scope": ("changes", "change-scope"),
    "changed-file typecheck (pyright)": ("typecheck-changed",
                                         "typecheck-changed"),
    "Driving hygiene (deterministic)": ("driving-hygiene", "driving-hygiene"),
    "UI Playwright smoke (single worker)": ("ui-playwright-smoke",
                                            "ui-playwright-smoke"),
    "handoff scope (advisory)": ("handoff-scope", "handoff-scope"),
    "hermetic suite (ubuntu-latest, py3.14)": ("test", "hermetic-ubuntu-314"),
}

GATES = (
    "python scripts/check_driver_core_drift.py --remote",
    "python -m ruff check audits",
    "python audits/self/audit.py",
    "python -m ruff check harness tests examples/oc_handoff scripts",
    "python -m compileall -q harness tests examples/oc_handoff",
    "python -m harness.repo_cards --check",
    "python -W error::ResourceWarning -m unittest discover -s tests -v",
    "python -m twine check dist/*",
)


def _workflow():
    return WORKFLOW.read_text(encoding="utf-8")


def _job_ids(text):
    """Job identifiers at the two-space indent inside the `jobs:` block."""
    match = re.search(r"^jobs:\n(.*)", text, re.S | re.M)
    if not match:
        raise AssertionError("ci.yml has no jobs: block")
    return set(re.findall(r"^  ([a-z0-9-]+):$", match.group(1), re.M))


class MappingTests(unittest.TestCase):

    def test_the_mapping_covers_exactly_the_required_contexts(self):
        self.assertEqual(tuple(MAPPING), REQUIRED_CONTEXTS)
        self.assertEqual(len(REQUIRED_CONTEXTS), 11)
        self.assertEqual(len(set(REQUIRED_CONTEXTS)), 11)

    def test_every_github_job_in_the_mapping_still_exists(self):
        jobs = _job_ids(_workflow())
        for context, (job, _circle) in {**MAPPING, **LANE_ONLY}.items():
            with self.subTest(context=context, job=job):
                self.assertIn(job, jobs)

    def test_circleci_job_names_are_unique_and_shell_safe(self):
        names = [circle for _github, circle in
                 list(MAPPING.values()) + list(LANE_ONLY.values())]
        self.assertEqual(len(names), len(set(names)))
        for name in names:
            with self.subTest(name=name):
                self.assertRegex(name, r"^[a-z0-9][a-z0-9-]*$")

    def test_every_claimed_gate_command_is_still_in_the_workflow(self):
        text = _workflow()
        for gate in GATES:
            with self.subTest(gate=gate):
                # The workflow wraps some of these across lines; compare on
                # collapsed whitespace so a reflow is not a false drift.
                collapsed = " ".join(text.split())
                self.assertIn(" ".join(gate.split()), collapsed)

    def test_the_hermetic_matrix_legs_are_still_declared(self):
        text = _workflow()
        # 3.11 on all three hosts comes from the base matrix; the rest are
        # explicit include entries. Both shapes have to survive a rename.
        self.assertIn("os: [ubuntu-latest, windows-latest, macos-latest]",
                      text)
        self.assertIn('python-version: ["3.11"]', text)
        for os_name, version in (("ubuntu-22.04", "3.9"),
                                 ("ubuntu-latest", "3.13"),
                                 ("ubuntu-latest", "3.14")):
            with self.subTest(os=os_name, version=version):
                pair = re.compile(
                    r"- os: " + re.escape(os_name)
                    + r"\n\s+python-version: \"" + re.escape(version) + "\"")
                self.assertRegex(text, pair)


class DocumentTests(unittest.TestCase):

    def test_the_document_names_every_required_context_and_circleci_job(self):
        text = DOC.read_text(encoding="utf-8")
        for context, (_github, circle) in MAPPING.items():
            with self.subTest(context=context):
                self.assertIn(f"`{context}`", text)
                self.assertIn(f"`{circle}`", text)

    def test_the_document_does_not_claim_the_port_exists(self):
        text = DOC.read_text(encoding="utf-8")
        self.assertIn("`.circleci/config.yml` not written yet", text)
        self.assertIn("owner-reserved", text)

    def test_the_document_states_the_activation_preconditions(self):
        text = DOC.read_text(encoding="utf-8")
        for needed in ("CircleCI VCS", "jev-gates", "concurrency 1",
                       "before** protection is switched"):
            with self.subTest(needed=needed):
                self.assertIn(needed, text)


if __name__ == "__main__":  # pragma: no cover - manual runs
    unittest.main()
