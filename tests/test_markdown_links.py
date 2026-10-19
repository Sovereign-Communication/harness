"""Catch broken relative Markdown links in the published repository docs."""

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")


class MarkdownLinkTest(unittest.TestCase):
    def test_relative_markdown_links_resolve(self):
        pages = [REPO_ROOT / "README.md", *sorted((REPO_ROOT / "docs").rglob("*.md"))]
        broken = []
        for page in pages:
            for match in LINK_RE.finditer(page.read_text(encoding="utf-8")):
                target = match.group(1).strip().split("#", 1)[0].split("?", 1)[0]
                if not target or target.startswith(("http://", "https://", "mailto:")):
                    continue
                if not (page.parent / target.strip("<>")).exists():
                    broken.append(f"{page.relative_to(REPO_ROOT)}: {target}")
        self.assertEqual(broken, [], "unresolved relative Markdown links:\n  " + "\n  ".join(broken))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
