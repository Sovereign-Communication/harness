"""Pin the security-relevant invariants of the tag-triggered publish workflow.

Why this module exists: ``.github/workflows/publish.yml`` is the only workflow
in this repository that no test touches, and the only one that can upload a
package to a public index under a short-lived OIDC credential. A future edit
that re-introduced a long-lived PyPI token, or that inverted the dry-run
guard so a rehearsal uploaded for real, would merge green today with nothing
to notice. These tests are that notice.

Deliberately stdlib-only. The project declares zero runtime dependencies, CI
installs only the ``dev`` extras (ruff, build, twine), and PyYAML is not among
them -- so ``import yaml`` here would pass on a developer machine and go red in
CI. The reader below is a narrow scanner for the exact subset of YAML this
workflow uses (block maps, block sequences, and scalars). It raises when it
meets a shape it does not understand rather than returning a partial result,
because a reader that silently stopped matching would turn every test below
into a no-op that always passes -- which is the precise failure these tests
exist to prevent.
"""

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "publish.yml"


def _lines():
    """(indent, text) for each meaningful line, comments and blanks dropped."""
    text = WORKFLOW.read_text(encoding="utf-8")
    out = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        out.append((len(raw) - len(raw.lstrip(" ")), stripped))
    return out


def _find(lines, key, indent):
    """Index of ``key:`` at exactly ``indent``, or None."""
    target = key + ":"
    for index, (ind, text) in enumerate(lines):
        if ind == indent and (text == target or text.startswith(target + " ")):
            return index
    return None


def _children(lines, index):
    """The lines nested under the block opened at ``index``."""
    base = lines[index][0]
    out = []
    for ind, text in lines[index + 1:]:
        if ind <= base:
            break
        out.append((ind, text))
    return out


def _mapping(children):
    """Parse ``key: value`` children into a dict, rejecting nesting."""
    out = {}
    for _, text in children:
        if ":" not in text:
            raise AssertionError(f"unexpected nested entry in mapping: {text!r}")
        key, _, value = text.partition(":")
        out[key.strip()] = value.strip()
    return out


def _steps(lines, job_indent):
    """Each step as a dict, with block scalars under ``run:`` kept verbatim."""
    anchor = None
    for index, (ind, text) in enumerate(lines):
        if ind == job_indent + 2 and text == "steps:":
            anchor = index
            break
    if anchor is None:
        raise AssertionError("publish job has no steps")

    step_indent = lines[anchor + 1][0]
    steps = []
    current = None
    for ind, text in lines[anchor + 1:]:
        if ind < step_indent:
            break
        if ind == step_indent and text.startswith("- "):
            current = {}
            steps.append(current)
            text = text[2:]
        if current is None:
            raise AssertionError("steps block does not start with a list item")
        if text in ("run: |", "run: |-", "run: >"):
            # A block scalar: everything more deeply indented is its body.
            continue
        if text.endswith(": |"):
            continue
        if ":" in text:
            key, _, value = text.partition(":")
            current[key.strip()] = value.strip()
        else:
            current.setdefault("_block", []).append(text)
    return steps


def _block_body(lines, step_index, step_indent):
    """The literal body of a step's ``run: |`` scalar.

    Starts past the step's own ``- name:`` line, which is at the step indent
    and would otherwise end the scan immediately.
    """
    body = []
    for ind, text in lines[step_index + 1:]:
        if ind <= step_indent:
            break
        body.append(text)
    return "\n".join(body)


def _publish_step():
    """The single step that performs the upload."""
    lines = _lines()
    jobs = _find(lines, "jobs", 0)
    if jobs is None:
        raise AssertionError("workflow has no jobs key")
    publish = None
    for ind, text in _children(lines, jobs):
        if ind == 2 and text.startswith("publish:"):
            publish = True
    if publish is None:
        raise AssertionError("workflow has no publish job")
    step_indent = None
    for ind, _ in _children(lines, jobs):
        step_indent = max(step_indent or 0, ind)
    for step in _steps(lines, 2):
        uses = str(step.get("uses", ""))
        if "gh-action-pypi-publish" in uses:
            return step
    raise AssertionError("no step uses pypa/gh-action-pypi-publish")


class PublishWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lines = _lines()
        cls.text = WORKFLOW.read_text(encoding="utf-8")

    # -- credentials -----------------------------------------------------
    def test_no_secret_is_referenced(self):
        """Trusted publishing is the whole mechanism: no stored credential.

        Scans the effective YAML, not the whole file: a comment saying "no
        password" is documentation, not a credential, and flagging it would
        only train a future maintainer to delete the explanation.
        """
        effective = "\n".join(text for _, text in self.lines).lower()
        self.assertNotIn("secrets.", effective)
        for forbidden in ("password", "pypi_api_token", "twine_", "api_key",
                          "api-key", "apikey", "username"):
            self.assertNotIn(
                forbidden, effective,
                f"publish workflow must not reference {forbidden!r}")

    def test_id_token_grant_is_present_and_is_the_only_one(self):
        """The legitimate credential must survive the scan above."""
        granted = _mapping(_children(self.lines, _find(self.lines, "permissions", 0)))
        self.assertNotIn("id-token", granted,
                         "top level must not request an OIDC token")

    def test_publish_action_is_a_pinned_version_ref(self):
        """A floating ref would let a third party change what CI uploads."""
        step = _publish_step()
        uses = str(step.get("uses", ""))
        self.assertTrue(uses.startswith("pypa/gh-action-pypi-publish@"), uses)
        ref = uses.split("@", 1)[1]
        self.assertNotIn("main", ref, f"unpinned action ref: {ref!r}")
        self.assertRegex(ref, r"v\d+", f"action ref carries no version: {ref!r}")

    # -- least privilege -------------------------------------------------
    def test_job_permissions_are_least_privilege(self):
        """contents:read plus id-token:write, and nothing broader."""
        lines = self.lines
        jobs = _find(lines, "jobs", 0)
        if jobs is None:
            self.fail("workflow has no jobs key")
        publish_at = None
        for offset, (ind, text) in enumerate(_children(lines, jobs)):
            if ind == 2 and text.startswith("publish:"):
                publish_at = jobs + 1 + offset
                break
        if publish_at is None:
            self.fail("workflow has no publish job")
        perms_at = None
        for offset, (ind, text) in enumerate(_children(lines, publish_at)):
            if ind == 4 and text == "permissions:":
                perms_at = publish_at + 1 + offset
                break
        if perms_at is None:
            self.fail("publish job declares no permissions")
        granted = _mapping(_children(lines, perms_at))
        self.assertEqual(granted.get("id-token"), "write")
        self.assertEqual(granted.get("contents"), "read")
        self.assertEqual(set(granted), {"contents", "id-token"},
                         f"unexpected permissions: {sorted(granted)}")

    def test_top_level_permissions_are_read_only(self):
        index = _find(self.lines, "permissions", 0)
        self.assertIsNotNone(index, "workflow declares no top-level permissions")
        granted = _mapping(_children(self.lines, index))
        self.assertEqual(granted, {"contents": "read"})

    # -- the dry run must never upload -----------------------------------
    def test_upload_step_is_unreachable_during_a_dry_run(self):
        step = _publish_step()
        condition = str(step.get("if", ""))
        self.assertIn("dry_run", condition,
                      f"upload step is not guarded by dry_run: {condition!r}")
        self.assertIn("!", condition,
                      f"upload guard does not negate the dry-run case: {condition!r}")
        # The guard must reference the dispatch event, so a real tag push
        # (which has no dry_run input at all) still uploads.
        self.assertIn("workflow_dispatch", condition)

    def test_dry_run_defaults_to_true(self):
        """Second safety leg: even a bad dispatch cannot upload by default."""
        on = _find(self.lines, "on", 0)
        self.assertIsNotNone(on, "workflow has no triggers")
        self.assertIn("default: true", self.text)
        dispatch = None
        for ind, text in _children(self.lines, on):
            if ind == 2 and text.startswith("workflow_dispatch:"):
                dispatch = True
        self.assertTrue(dispatch, "workflow_dispatch trigger missing")

    def test_release_tag_trigger_covers_version_tags(self):
        on = _find(self.lines, "on", 0)
        triggers = "\n".join(t for _, t in _children(self.lines, on))
        self.assertIn("v*", triggers,
                      "no v* tag trigger, so a release would not publish")
        self.assertIn("push", triggers)

    # -- version safety --------------------------------------------------
    def test_tag_version_parity_is_enforced(self):
        """PyPI cannot overwrite a release, so a mismatched tag must fail."""
        lines = self.lines
        found = False
        for index, (ind, text) in enumerate(lines):
            if ind == 6 and text.startswith("- name: Version parity"):
                body = _block_body(lines, index, 6)
                found = True
                self.assertIn("assert tag == expected", body)
                self.assertIn("does not match pyproject version", body)
                break
        self.assertTrue(found, "no version-parity step in the publish job")

    def test_publish_job_builds_no_artifact_matrix(self):
        """A matrix would emit colliding names and the upload action would fail."""
        lines = self.lines
        jobs = _find(lines, "jobs", 0)
        for ind, text in _children(lines, jobs):
            if ind >= 2 and (text.startswith("strategy:") or "matrix:" in text):
                self.fail(f"publish job declares a build matrix: {text!r}")

    # -- documentation ---------------------------------------------------
    def test_runbook_documents_the_owner_step(self):
        """The owner step is the one thing an agent cannot do; it must be written down."""
        runbook = (REPO_ROOT / "docs" / "releasing.md").read_text(encoding="utf-8")
        lowered = runbook.lower()
        self.assertIn("publish.yml", runbook)
        self.assertIn("publisher", lowered)
        self.assertIn("403", runbook,
                      "runbook must name the 403 an unregistered publisher yields")
        self.assertIn("dry_run", runbook,
                      "runbook must document the rehearsal that uploads nothing")


if __name__ == "__main__":
    unittest.main()
