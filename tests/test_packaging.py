"""Packaging invariants for a PyPI release.

These are hermetic: no network, no build. They pin the three failures that
only show up after upload -- relative links that 404 on the PyPI page,
operator paths in the sdist, and runtime data missing from the wheel -- plus
the version/changelog state a release commit must have.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
PYPROJECT = ROOT / "pyproject.toml"
MANIFEST = ROOT / "MANIFEST.in"
CHANGELOG = ROOT / "CHANGELOG.md"
FALLBACK_VERSION = ROOT / "harness" / "__init__.py"


class PyPIPackagingTests(unittest.TestCase):
    def test_readme_links_are_absolute(self):
        relative = [
            target
            for target in re.findall(r"\]\(([^)]+)\)", README.read_text(encoding="utf-8"))
            if not re.match(r"^(https?:|mailto:|#)", target)
        ]
        self.assertEqual(
            relative, [],
            "README relative links resolve against pypi.org and 404: "
            + repr(relative),
        )

    def test_manifest_excludes_operator_lane_docs(self):
        manifest = MANIFEST.read_text(encoding="utf-8")
        for name in ("docs/claude-context.md", "docs/jev-dogfood.md",
                     "docs/jev-roadmap.md"):
            with self.subTest(name=name):
                self.assertIn("exclude " + name, manifest)
        self.assertNotIn("C:\\Users\\", manifest)

    def test_wheel_declares_runtime_data_and_ui(self):
        pyproject = PYPROJECT.read_text(encoding="utf-8")
        data_block = pyproject.split("[tool.setuptools.package-data]", 1)[1]
        for pattern in ('"ui/*.html"', '"ui/*.js"', '"ui/*.css"',
                        '"data/*.md"', '"data/*.json"'):
            with self.subTest(pattern=pattern):
                self.assertIn(pattern, data_block)
        for name in ("task_families.json", "experience.md",
                     "refine_propose_prompt.md"):
            with self.subTest(name=name):
                self.assertTrue((ROOT / "harness" / "data" / name).is_file())

    def test_version_single_source_parity(self):
        pyproject = PYPROJECT.read_text(encoding="utf-8")
        version = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.M)
        fallback = re.search(
            r'_FALLBACK_VERSION\s*=\s*"([^"]+)"',
            FALLBACK_VERSION.read_text(encoding="utf-8"),
        )
        self.assertIsNotNone(version)
        self.assertIsNotNone(fallback)
        self.assertEqual(fallback.group(1), version.group(1))

    def test_unreleased_section_is_flattened(self):
        version = re.search(
            r'^version\s*=\s*"([^"]+)"',
            PYPROJECT.read_text(encoding="utf-8"), re.M,
        )
        self.assertIsNotNone(version)
        changelog = CHANGELOG.read_text(encoding="utf-8")
        start = changelog.index("## [Unreleased]")
        end = changelog.index("## [", start + 5)
        self.assertEqual(
            changelog[start:end].strip(),
            "## [Unreleased]\n\nNothing yet.",
            "a release commit must flatten [Unreleased] into a dated version",
        )
        release = re.search(r"^## \[(\d+\.\d+\.\d+)\] \u2014 \d{4}-\d{2}-\d{2}$",
                            changelog, re.M)
        self.assertIsNotNone(release)
        self.assertEqual(release.group(1), version.group(1))
