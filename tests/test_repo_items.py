"""JEV-P6 extract gate tests (tests/test_repo_items.py).

Code-owned inventory facts only: kinds, sizes, symbols, imports, headings,
test/gate facts, centrality ranking, mechanical tallies, state bounds,
condense quality (noise-free summaries, kind-aware state, visible
truncation, char budget), and the additive soft-skip enumeration flag.
No model, no network.
"""
import json
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from harness.repo_items import (
    STATE_CHAR_BUDGET,
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
        self.assertEqual(element_kind(".gitignore"), "config")
        self.assertEqual(element_kind(".gitattributes"), "config")
        self.assertEqual(element_kind("Makefile"), "config")
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
        self.assertLessEqual(len(state["symbols"]), 10)
        self.assertTrue(all(len(s) <= 60 for s in state["symbols"]))
        self.assertLessEqual(len(state["summary"]), 200)
        self.assertEqual(state["symbol_count"], len(core["symbols"]))
        # gate inventory never ships; unsaturated python adds import roots
        self.assertNotIn("gate", state)
        self.assertNotIn("headings", state)
        self.assertLessEqual(len(state["imports"]), 3)
        self.assertIn("os", state["imports"])
        ranked = rank_symbol_elements(self.elements, 3)
        if ranked:
            sym = symbol_state(ranked[0])
            self.assertLessEqual(len(sym["module_summary"]), 160)
            self.assertEqual(sym["element_kind"], "symbol")

    def test_doc_state_carries_headings(self):
        design = next(e for e in self.elements if e["path"] == "docs/design.md")
        state = element_state(design)
        self.assertEqual(state["headings"], ["Design", "Waist"])
        self.assertEqual(state["symbols"], [])
        self.assertNotIn("imports", state)

    def test_config_state_carries_keys(self):
        cfg = next(e for e in self.elements if e["path"] == "data.json")
        self.assertEqual(cfg["keys"], ["k"])
        state = element_state(cfg)
        self.assertEqual(state["keys"], ["k"])
        self.assertNotIn("headings", state)
        # single structural line -> honest empty summary, keys carry signal
        self.assertEqual(state["summary"], "")

    def test_saturated_python_inventory_keeps_import_roots(self):
        synth = {"path": "pkg/big.py", "kind": "python", "loc": 1,
                 "est_tokens": 1, "summary": "big",
                 "imports": [f"mod{i}" for i in range(9)],
                 "symbols": [{"sig": f"s{i}"} for i in range(14)],
                 "test": False}
        state = element_state(synth)
        # coupling signal survives symbol saturation, still capped
        self.assertEqual(len(state["imports"]), 3)
        self.assertEqual(len(state["symbols"]), 10)
        self.assertEqual(state["symbol_count"], 14)

    def test_every_fixture_state_respects_char_budget(self):
        for element in self.elements:
            self.assertLessEqual(len(json.dumps(element_state(element))),
                                 STATE_CHAR_BUDGET, element["path"])


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

    def test_empty_file_summary_is_labeled_not_blank(self):
        rel = self._write("pkg/empty.py", "")
        element = build_elements(self.root, [rel])[0]
        self.assertEqual(element["summary"], "(empty file)")

    def test_unreadable_path_yields_empty_facts_and_label(self):
        element = build_elements(self.root, ["missing/none.py"])[0]
        self.assertEqual(element["loc"], 0)
        self.assertEqual(element["bytes"], 0)
        self.assertEqual(element["summary"], "(unreadable)")

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


class CondenseQualityTests(unittest.TestCase):
    """Condense-phase quality contract: noise-free summaries, kind-aware
    signal, visible truncation, and the hard state character budget."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def _write(self, rel, text):
        path = Path(self.root) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return rel

    def test_summary_never_returns_structural_noise(self):
        rel = self._write("cfg.json", '{"name": "x", "version": 1}')
        element = build_elements(self.root, [rel])[0]
        self.assertEqual(element["summary"], "")  # single structural line
        self.assertEqual(element["keys"], ["name", "version"])
        page = self._write("page.html",
                           "<!DOCTYPE html>\n<html>\n<title>Doc</title>\n")
        element2 = build_elements(self.root, [page])[0]
        self.assertEqual(element2["summary"], "Doc")  # title, unwrapped
        log = self._write("run.log", "[OK] started fine\n[ui] GET / 200\n")
        element3 = build_elements(self.root, [log])[0]
        self.assertEqual(element3["summary"], "[OK] started fine")

    def test_bom_never_reaches_summary_or_breaks_heading_strip(self):
        rel = self._write("agents.md", "\ufeff# Real Title\nbody\n")
        element = build_elements(self.root, [rel])[0]
        self.assertNotIn("\ufeff", element["summary"])
        self.assertEqual(element["summary"], "Real Title")

    def test_frontmatter_and_heading_markers_are_clean(self):
        rel = self._write("note.md", "---\ntitle: X\n---\n# Real Title\nbody\n")
        element = build_elements(self.root, [rel])[0]
        self.assertEqual(element["summary"], "Real Title")
        self.assertIn("Real Title", element["headings"])

    def test_python_docstring_summary_for_test_kind(self):
        rel = self._write("tests/test_thing.py",
                          '"""Does the helpful thing."""\nimport os\n')
        element = build_elements(self.root, [rel])[0]
        self.assertEqual(element["kind"], "test")
        self.assertEqual(element["summary"], "Does the helpful thing.")

    def test_config_key_hints_for_yaml_toml_and_malformed_json(self):
        yml = self._write("cfg.yml", "name: x\nnested:\n  a: 1\n")
        self.assertEqual(build_elements(self.root, [yml])[0]["keys"][:1],
                         ["name"])
        toml = self._write("cfg.toml", "[tool.things]\nflag = 1\n")
        self.assertIn("tool.things",
                      build_elements(self.root, [toml])[0]["keys"])
        bad = self._write("bad.json", '{"alpha": 1,}')  # malformed
        self.assertEqual(build_elements(self.root, [bad])[0]["keys"],
                         ["alpha"])

    def test_long_signature_truncation_is_visible(self):
        args = ", ".join(f"arg_{i}=None" for i in range(20))
        rel = self._write("pkg/long.py", f"def sprawling({args}):\n    return 1\n")
        element = build_elements(self.root, [rel])[0]
        sig = element["symbols"][0]["sig"]
        self.assertLessEqual(len(sig), 90)
        self.assertTrue(sig.endswith("\u2026"))
        state_sig = element_state(element)["symbols"][0]
        self.assertLessEqual(len(state_sig), 60)
        self.assertTrue(state_sig.endswith("\u2026"))

    def test_long_summary_truncation_is_visible_not_midword(self):
        rel = self._write("doc.md", "word " * 120 + "tail\n")
        element = build_elements(self.root, [rel])[0]
        summary = element["summary"]
        self.assertLessEqual(len(summary), 200)
        self.assertTrue(summary.endswith("\u2026"), summary[-20:])

    def test_state_char_budget_holds_on_worst_case(self):
        worst = {
            "path": "a/very/long/path/to/enclosing/module_name.py",
            "kind": "python", "loc": 99999, "est_tokens": 123456,
            "summary": "x" * 200,
            "symbols": [{"sig": "s" * 60} for _ in range(12)],
            "imports": [f"mod{i}" for i in range(10)],
            "headings": ["h" * 70 for _ in range(6)],
            "test": True,
        }
        state = element_state(worst)
        self.assertLessEqual(len(json.dumps(state)), STATE_CHAR_BUDGET)

    def test_every_noise_rule_path_is_exercised(self):
        # blank line -> bare punctuation -> tag-only -> hr prefix -> fence
        blob = "\n===\n</div>\n---\n```\nend content\n"
        rel = self._write("mixed.txt", blob)
        element = build_elements(self.root, [rel])[0]
        self.assertEqual(element["summary"], "end content")
        # closing-brace prefix reached before any content line
        braces = self._write("brace.txt", "{\n}\nreal\n")
        self.assertEqual(build_elements(self.root, [braces])[0]["summary"],
                         "real")
        # leading frontmatter with no closing delimiter falls through
        unclosed = self._write("open.md", "---\nonly line\n")
        self.assertEqual(build_elements(self.root, [unclosed])[0]["summary"],
                         "only line")

    def test_css_block_opener_is_not_a_summary(self):
        rel = self._write("site.css", "/* palette */\n:root {\n  color: red;\n}\n")
        element = build_elements(self.root, [rel])[0]
        self.assertEqual(element["summary"], "color: red;")

    def test_xml_declaration_and_titleless_html_reach_content(self):
        xml = self._write("s.xml", "<?xml version=\"1.0\"?>\n<root>ok</root>\n")
        self.assertTrue(build_elements(self.root, [xml])[0]["summary"])
        html = self._write("plain.html", "<html lang='en'>\n<body>plain</body>\n")
        summary = build_elements(self.root, [html])[0]["summary"]
        self.assertEqual(summary, "<body>plain</body>")  # tag line skipped

    def test_jsonl_first_record_keys_with_malformed_first_line(self):
        rel = self._write("events.jsonl", "not-json\n{\"event\": \"call\"}\n")
        element = build_elements(self.root, [rel])[0]
        self.assertEqual(element["keys"], ["event"])


if __name__ == "__main__":
    unittest.main()
