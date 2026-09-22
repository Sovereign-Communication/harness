"""JEV-P6 extract gate tests (tests/test_repo_items.py).

Code-owned inventory facts only: kinds, sizes, symbols, imports, headings,
test/gate facts, centrality ranking, mechanical tallies, state bounds, and
the additive soft-skip enumeration flag. No model, no network.
"""
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from harness.repo_items import (
    build_elements,
    element_kind,
    element_state,
    import_centrality,
    mechanical_tallies,
    rank_symbol_elements,
    summary_listing,
    symbol_state,
)
from harness.repo_scope import enumerate_repo_files

FIXTURE = {
    "pkg/__init__.py": "'''Package.'''\n",
    "pkg/core.py": (
        "'''Core module: condenses the brief and dispatches gates.'''\n"
        "import os\n"
        "from pkg import helper\n"
        "class Engine:\n"
        "    '''The engine.'''\n"
        "    def run(self, x):\n"
        "        return x\n"
        "def top_level(a, b=1):\n"
        "    '''Do the thing.'''\n"
        "    return helper(a, b)\n"
    ),
    "pkg/helper.py": "def helper(a, b=1):\n    return a\n",
    "tests/test_core.py": "from pkg.core import Engine\n\ndef test_run():\n    assert Engine().run(1) == 1\n",
    "docs/design.md": "# Design\n## Waist\nbody\n",
    "data.json": '{"k": 1}\n',
    "audits/self/audit.py": "def check():\n    return True\n",
    "logs/run.log": "WARN boom\n",
}


def _tree(root):
    for rel, text in FIXTURE.items():
        path = Path(root) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


class ElementKindTests(unittest.TestCase):
    def test_kinds_are_pure_path_facts(self):
        self.assertEqual(element_kind("harness/waist.py"), "python")
        self.assertEqual(element_kind("tests/test_waist.py"), "test")
        self.assertEqual(element_kind("audits/self/audit.py"), "audit")
        self.assertEqual(element_kind("docs/x.md"), "doc")
        self.assertEqual(element_kind("packs/pack.json"), "config")
        self.assertEqual(element_kind("site/app.js"), "ui")
        self.assertEqual(element_kind("LICENSE"), "doc")
        self.assertEqual(element_kind("weird.bin"), "other")


class EnumerationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        _tree(self.tmp.name)

    def test_default_relevance_pass_still_skips_soft_content(self):
        files = enumerate_repo_files(Path(self.tmp.name), limit=1000)
        self.assertNotIn("audits/self/audit.py", files)
        self.assertNotIn("logs/run.log", files)
        self.assertIn("pkg/core.py", files)

    def test_summary_listing_includes_soft_skipped_content(self):
        files = summary_listing(Path(self.tmp.name))
        self.assertIn("audits/self/audit.py", files)
        self.assertIn("logs/run.log", files)
        self.assertIn("docs/design.md", files)
        self.assertNotIn("__pycache__", " ".join(files))

    def test_summary_listing_limit(self):
        files = summary_listing(Path(self.tmp.name), limit=2)
        self.assertEqual(len(files), 2)


class BuildElementsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        _tree(self.tmp.name)
        self.files = summary_listing(Path(self.tmp.name))
        self.elements = build_elements(Path(self.tmp.name), self.files)
        self.by_path = {e["path"]: e for e in self.elements}

    def test_measured_facts_only(self):
        core = self.by_path["pkg/core.py"]
        self.assertEqual(core["kind"], "python")
        self.assertGreater(core["loc"], 5)
        self.assertGreater(core["bytes"], 50)
        self.assertGreater(core["est_tokens"], 10)
        self.assertTrue(core["summary"].startswith("Core module"))
        names = [s["name"] for s in core["symbols"]]
        self.assertIn("Engine", names)
        self.assertIn("top_level", names)
        self.assertIn("os", core["imports"])
        self.assertIn("pkg", core["imports"])

    def test_symbols_carry_bounded_signatures(self):
        core = self.by_path["pkg/core.py"]
        for symbol in core["symbols"]:
            self.assertLessEqual(len(symbol["sig"]), 90)
            self.assertGreater(symbol["line"], 0)

    def test_headings_for_docs_and_none_for_python(self):
        self.assertEqual(self.by_path["docs/design.md"]["headings"][:2],
                         ["Design", "Waist"])
        self.assertEqual(self.by_path["pkg/core.py"]["headings"], [])

    def test_test_counterpart_and_gate(self):
        core = self.by_path["pkg/core.py"]
        self.assertTrue(core["test"])
        # no harness/-prefixed path: the derived gate is the compile check
        self.assertIn("core.py", core["gate"] or "")
        self.assertNotIn("py_compile", self.by_path["tests/test_core.py"]["kind"])
        test_row = self.by_path["tests/test_core.py"]
        self.assertEqual(test_row["kind"], "test")

    def test_gate_present_for_python_targets(self):
        gate = self.by_path["pkg/core.py"]["gate"]
        self.assertTrue(gate)
        self.assertIn("core.py", gate)


class CentralityAndRankingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        _tree(self.tmp.name)
        self.elements = build_elements(
            Path(self.tmp.name), summary_listing(Path(self.tmp.name)))

    def test_centrality_counts_importers(self):
        degree = import_centrality(self.elements)
        # test_core does `from pkg.core import Engine` -> core has an importer
        self.assertGreaterEqual(degree["pkg/core.py"], 1)
        # core does `from pkg import helper` -> helper has an importer
        self.assertEqual(degree["pkg/helper.py"], 1)

    def test_symbol_rank_is_deterministic_and_bounded(self):
        first = rank_symbol_elements(self.elements, 5)
        second = rank_symbol_elements(self.elements, 5)
        self.assertEqual([r["id"] for r in first], [r["id"] for r in second])
        self.assertLessEqual(len(first), 5)
        degrees = [r["module_degree"] for r in first]
        self.assertEqual(degrees, sorted(degrees, reverse=True))
        for row in first:
            self.assertTrue(row["id"].startswith("symbol:"))

    def test_symbol_zero_disables(self):
        self.assertEqual(rank_symbol_elements(self.elements, 0), [])

    def test_states_are_bounded(self):
        core = next(e for e in self.elements if e["path"] == "pkg/core.py")
        state = element_state(core)
        self.assertLessEqual(len(state["symbols"]), 18)
        self.assertLessEqual(len(state["summary"]), 200)
        self.assertLessEqual(len(state["imports"]), 10)
        ranked = rank_symbol_elements(self.elements, 3)
        if ranked:
            sym = symbol_state(ranked[0])
            self.assertLessEqual(len(sym["module_summary"]), 160)
            self.assertEqual(sym["element_kind"], "symbol")


class TalliesTests(unittest.TestCase):
    def test_tallies_sum_to_totals(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        _tree(tmp.name)
        elements = build_elements(tmp.name, summary_listing(tmp.name))
        tallies = mechanical_tallies(elements)
        totals = tallies["totals"]
        self.assertEqual(totals["files"], len(elements))
        self.assertEqual(sum(tallies["by_kind"].values()), len(elements))
        self.assertEqual(totals["bytes"], sum(e["bytes"] for e in elements))
        self.assertEqual(totals["loc"], sum(e["loc"] for e in elements))
        self.assertEqual(totals["symbols"],
                         sum(len(e["symbols"]) for e in elements))


class DefensivePathTests(unittest.TestCase):
    """Every defensive branch: broken input, unreadable files, caps, patches."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return rel

    def test_audits_doc_kind_is_audit(self):
        self.assertEqual(element_kind("audits/self/report.md"), "audit")

    def test_broken_syntax_falls_back_to_first_line(self):
        rel = self._write("pkg/broken.py", "def broken(:\nnot python\n")
        element = build_elements(self.root, [rel])[0]
        self.assertEqual(element["summary"], "def broken(:")
        self.assertEqual(element["symbols"], [])
        self.assertEqual(element["imports"], [])

    def test_empty_file_summary_is_empty_string(self):
        rel = self._write("pkg/empty.py", "")
        element = build_elements(self.root, [rel])[0]
        self.assertEqual(element["summary"], "")

    def test_unreadable_path_yields_empty_facts(self):
        element = build_elements(self.root, ["missing/none.py"])[0]
        self.assertEqual(element["loc"], 0)
        self.assertEqual(element["bytes"], 0)

    def test_dunder_methods_are_skipped_but_init_is_kept(self):
        rel = self._write(
            "pkg/du.py",
            "class A:\n"
            "    def __init__(self):\n        pass\n"
            "    def __repr__(self):\n        return 'A'\n"
            "    def real(self):\n        return 1\n")
        names = [s["name"] for s in build_elements(self.root, [rel])[0]["symbols"]]
        self.assertIn("__init__", names)
        self.assertNotIn("__repr__", names)
        self.assertIn("real", names)

    def test_import_cap_breaks_at_twice_display_bound(self):
        imports = "".join(f"import mod_{i}\n" for i in range(30))
        rel = self._write("pkg/many.py", imports)
        found = build_elements(self.root, [rel])[0]["imports"]
        self.assertEqual(len(found), 20)  # SUMMARY_MAX_IMPORTS * 2

    def test_heading_cap_breaks_at_display_bound(self):
        headings = "".join(f"# Heading {i}\n" for i in range(20))
        rel = self._write("doc.md", headings)
        found = build_elements(self.root, [rel])[0]["headings"]
        self.assertEqual(len(found), 12)  # SUMMARY_MAX_HEADINGS

    def test_symbol_cap_breaks_in_ranking_inputs(self):
        source = "".join(f"def fn_{i}():\n    return {i}\n" for i in range(30))
        rel = self._write("pkg/many_fn.py", source)
        element = build_elements(self.root, [rel])[0]
        self.assertEqual(len(element["symbols"]), 30)

    def test_unparse_failure_keeps_an_empty_signature(self):
        self._write("pkg/core2.py", "def alpha(a):\n    return a\n")
        with mock.patch("ast.unparse", side_effect=RuntimeError("boom")):
            element = build_elements(self.root, ["pkg/core2.py"])[0]
        self.assertEqual(element["symbols"][0]["sig"], "alpha()")

    def test_gate_failure_degrades_to_none(self):
        self._write("pkg/core3.py", "def beta():\n    return 2\n")
        with mock.patch("harness.repo_items.gate_for_targets",
                        side_effect=OSError("gate boom")):
            element = build_elements(self.root, ["pkg/core3.py"])[0]
        self.assertIsNone(element["gate"])

    def test_empty_rel_path_has_no_test_counterpart(self):
        from harness.repo_items import _test_counterpart
        self.assertFalse(_test_counterpart("", self.root))


if __name__ == "__main__":
    unittest.main()
