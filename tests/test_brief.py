"""The `harness brief` builder + grounding lint (MR-8 spec).

The one rule under test throughout: a generated brief asserts nothing
beyond its goal -- every factual line is a cited, hash-pinned window, and
the lint rejects drifted sources, non-span windows, unknown citations,
and uncited claims.
"""
import os
import tempfile
import unittest

from harness.brief import build_brief, validate_brief
from harness.errors import HarnessError


def _reader(files):
    def read(path):
        return files[path]
    return read


class BuildBriefTests(unittest.TestCase):
    def test_pack_asserts_nothing_beyond_the_goal(self):
        pack = build_brief("make x faster", ["a.py"],
                           reader=_reader({"a.py": "def x(): pass\n"}))
        self.assertEqual(pack["goal"], "make x faster")
        self.assertEqual(pack["grounding"]["claims"], [])
        self.assertEqual(pack["grounding"]["unknowns"], [])
        # Every window is cited and hash-pinned to its source.
        (source,) = pack["grounding"]["sources"]
        (window,) = pack["windows"]
        self.assertEqual(window["source_id"], source["id"])
        self.assertEqual(window["content"], "def x(): pass\n")
        self.assertFalse(window["truncated"])

    def test_goal_required(self):
        with self.assertRaises(HarnessError):
            build_brief("   ", ["a.py"])

    def test_truncation_is_honest_and_span_labeled(self):
        content = "".join(f"line {n}\n" for n in range(1, 3001))
        pack = build_brief("g", ["big.py"], reader=_reader({"big.py": content}))
        (window,) = pack["windows"]
        self.assertTrue(window["truncated"])
        self.assertIn("ONLY these lines", window["note"])
        self.assertLess(window["end_line"], 3001)
        # The window content is a span of the source (the lint's own check).
        self.assertEqual(validate_brief(pack, reader=_reader(
            {"big.py": content})), [])

    def test_total_budget_stops_adding_windows(self):
        files = {f"f{n}.py": "x" * 20000 for n in range(3)}
        pack = build_brief("g", list(files), reader=_reader(files))
        self.assertLessEqual(
            sum(len(w["content"]) for w in pack["windows"]), 48000)
        self.assertEqual(len(pack["grounding"]["sources"]), 3)


class ValidateBriefTests(unittest.TestCase):
    def test_drifted_source_is_rejected(self):
        files = {"a.py": "v1\n"}
        pack = build_brief("g", ["a.py"], reader=_reader(files))
        files["a.py"] = "v2 drifted\n"
        issues = validate_brief(pack, reader=_reader(files))
        self.assertTrue(any("drifted" in i for i in issues))

    def test_non_span_window_is_rejected(self):
        files = {"a.py": "alpha\nbeta\n"}
        pack = build_brief("g", ["a.py"], reader=_reader(files))
        pack["windows"][0]["content"] = "not in the file\n"
        issues = validate_brief(pack, reader=_reader(files))
        self.assertTrue(any("not a span" in i for i in issues))

    def test_unknown_window_citation_is_rejected(self):
        files = {"a.py": "x\n"}
        pack = build_brief("g", ["a.py"], reader=_reader(files))
        pack["windows"][0]["source_id"] = "s9"
        self.assertTrue(validate_brief(pack, reader=_reader(files)))

    def test_uncited_claim_is_invalid(self):
        pack = build_brief("g", [], reader=_reader({}))
        pack["grounding"]["claims"] = [{"text": "vibes", "source_ids": []}]
        issues = validate_brief(pack, reader=_reader({}))
        self.assertTrue(any("uncited claim" in i for i in issues))

    def test_clean_pack_lints_empty(self):
        files = {"a.py": "x\n"}
        pack = build_brief("g", ["a.py"], reader=_reader(files))
        pack["grounding"]["claims"] = [
            {"text": "a.py defines x", "source_ids": ["s1"]}]
        self.assertEqual(validate_brief(pack, reader=_reader(files)), [])


class CliBriefTests(unittest.TestCase):
    def test_cmd_brief_builds_and_lints_hermetically(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        from harness.cli import _cmd_brief

        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "a.py")
            with open(target, "w", encoding="utf-8") as f:
                f.write("def x(): pass\n")
            opts = SimpleNamespace(goal="make x better", files=[target],
                                   validate=True, out=None, quiet=True)
            with patch("harness.cli._emit_by_status") as emit:
                _cmd_brief(opts)
        out = emit.call_args[0][0]
        pack = out["brief"]
        self.assertEqual(pack["goal"], "make x better")
        self.assertEqual(pack["grounding"]["claims"], [])
        self.assertEqual(out["grounding_issues"], [])
        self.assertTrue(out["ok"])


if __name__ == "__main__":
    unittest.main()
