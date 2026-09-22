"""JEV-P6 envelope gate tests (tests/test_jev_repo_envelope.py).

Aggregation math (declared keys only, fallbacks never smoothed, spend ==
sum of persisted rows), resumable budget stops (refusals never persisted),
REPO-MAP rendering, artifact writers (POSIX newlines), and the hermetic
CLI face (unkeyed, temp ledger).
"""
import argparse
import json
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from harness.cli import _cmd_repo_summary, _repo_summary_policy_factory
from harness.config import load_settings
from harness.errors import HarnessError
from harness.jev import JevEvaluationResult
from harness.jev_packs import validate_repo_summary_pack
from harness.repo_summary import (
    SCHEMA,
    analyze_repo,
    load_judgment_rows,
    render_repo_map,
    write_envelope,
    write_map,
)


def repo_pack():
    """Operator pack fixture (local, so hermetic import modes stay safe)."""
    return {
        "id": "fixture-repo-v1",
        "axes": {
            "stage": {
                "instructions": "Choose the hourglass stage.",
                "criteria": {
                    "prep": "inventory and planning inputs",
                    "waist": "frontier confirmation",
                    "adjudicate": "verification and evidence",
                },
            },
            "handling": {
                "instructions": "Choose the least capable handling tier.",
                "criteria": {
                    "code_owned": "no model needed",
                    "scout": "bounded mechanical work",
                    "frontier": "architectural attention",
                },
            },
        },
        "score": {
            "id": "attention",
            "instructions": "Rate centrality to the hourglass.",
            "levels": ["background", "notable", "central"],
        },
        "nouls": {
            "waist_relevant": {
                "instructions": "Must the waist brief cite this element?",
                "true": "Citation needed.",
                "false": "Not needed.",
            },
        },
        "keywords": {
            "stage": {
                "adjudicate": ["verify", "ledger", "test"],
                "waist": ["waist", "verdict"],
            },
            "handling": {"frontier": ["architect"]},
        },
    }

FIXTURE = {
    "harness/ledger.py": (
        "'''Hash-chained autonomy ledger with verify gates.'''\n"
        "import json\n"
        "def append(event):\n"
        "    return event\n"
        "def verify_chain():\n"
        "    return True\n"
    ),
    "harness/waist.py": (
        "'''Frontier plan confirmation and verdict contract.'''\n"
        "def compose_plan(goal):\n"
        "    return goal\n"
    ),
    "tests/test_ledger.py": "def test_append():\n    assert True\n",
    "docs/design.md": "# Design\n## Dispatch\nbody\n",
    "pack.json": '{"k": 1}\n',
}


def _tree(root):
    for rel, text in FIXTURE.items():
        path = Path(root) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _stub_policy_factory(count=None, cost=0.001):
    """Unkeyed-shaped stub rows via the real policy-free path: a factory
    returning a fake policy with canned judgments, optionally stopping."""
    state = {"n": 0}

    class _FakePolicy:
        def evaluate_repo_summary(self, state_el, pack, task_id=None):
            state["n"] += 1
            axes = {"stage": "adjudicate" if "ledger" in str(state_el.get("path"))
                    else "prep",
                    "handling": "code_owned",
                    "waist_relevant": True, "parallel_safe": False}
            nouls = {"waist_relevant": 0.9, "parallel_safe": 0.2}
            structural = {"verdict": "pass", "confidence": 0.9,
                          "supported": 0.9, "cost": cost,
                          "input_tokens": 700, "output_tokens": 50,
                          "is_fallback": False, "model": "jev-test",
                          "site": "repo_summary"}
            judgment = {"pack_id": pack["id"],
                        "axes": {"stage": axes["stage"],
                                 "handling": axes["handling"]},
                        "attention": {"id": "attention", "level": "central",
                                      "value": 0.8, "confidence": 0.9},
                        "nouls": nouls, "is_fallback": False,
                        "evidence": ["stage:" + axes["stage"]]}
            result = JevEvaluationResult(
                "pass", 0.9, 0.9, {}, [], cost=cost, input_tokens=700,
                output_tokens=50, model="jev-test")
            return result, structural, judgment

    def factory(cumulative):
        if count is not None and state["n"] >= count:
            return None
        return _FakePolicy()

    return factory


class AnalyzeRepoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        _tree(self.tmp.name)
        self.pack = validate_repo_summary_pack(repo_pack())
        # resume state lives OUTSIDE the inventory root: a run never judges
        # its own state file (the CLI additionally passes exclude= for the
        # envelope/map paths inside the root)
        self.state_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.state_dir.cleanup)
        self.state_path = os.path.join(self.state_dir.name, "judged.jsonl")

    def test_complete_run_aggregates_honestly(self):
        env = analyze_repo(self.tmp.name, self.pack, _stub_policy_factory(),
                           state_path=self.state_path, symbol_limit=5,
                           generated_at="T")
        self.assertEqual(env["schema"], SCHEMA)
        self.assertEqual(env["coverage"]["stop_reason"], "complete")
        self.assertEqual(env["coverage"]["pending"], 0)
        self.assertEqual(env["coverage"]["judged"],
                         env["coverage"]["judged_files"] + env["coverage"]["judged_symbols"])
        self.assertEqual(env["coverage"]["live_judged"], env["coverage"]["judged"])
        self.assertEqual(env["coverage"]["fallbacks"], 0)
        expected_cost = 0.001 * env["coverage"]["judged"]
        self.assertAlmostEqual(env["spend"]["cost_usd"], expected_cost, places=6)
        self.assertEqual(env["spend"]["input_tokens"],
                         700 * env["coverage"]["judged"])
        self.assertEqual(env["spend"]["output_tokens"],
                         50 * env["coverage"]["judged"])
        self.assertEqual(env["spend"]["calls"], env["coverage"]["judged"])
        # axis totals over declared keys + honest unmatched
        stage_total = sum(env["axes"]["stage"].values())
        self.assertEqual(stage_total, env["coverage"]["judged"])
        self.assertEqual(set(env["axes"]["stage"]),
                         set(self.pack["axes"]["stage"]["criteria"]) | {"unmatched"})
        rows = load_judgment_rows(self.state_path)
        self.assertEqual(len(rows), env["coverage"]["judged"])

    def test_budget_stop_persists_progress_and_resumes(self):
        env1 = analyze_repo(self.tmp.name, self.pack,
                            _stub_policy_factory(count=2),
                            state_path=self.state_path, symbol_limit=0,
                            run_budget=10.0, generated_at="T1")
        self.assertEqual(env1["coverage"]["stop_reason"], "run_budget")
        self.assertEqual(env1["coverage"]["judged"], 2)
        self.assertGreater(env1["coverage"]["pending"], 0)
        env2 = analyze_repo(self.tmp.name, self.pack, _stub_policy_factory(),
                            state_path=self.state_path, symbol_limit=0,
                            run_budget=10.0, generated_at="T2")
        self.assertEqual(env2["coverage"]["stop_reason"], "complete")
        self.assertEqual(env2["coverage"]["prior_rows"], 2)
        total = env1["coverage"]["candidates"]
        self.assertEqual(env2["coverage"]["judged"], total)
        self.assertEqual(len(load_judgment_rows(self.state_path)), total)

    def test_harness_error_mid_run_is_a_clean_budget_stop(self):
        class _Flaky:
            def __init__(self):
                self.n = 0

            def evaluate_repo_summary(self, state_el, pack, task_id=None):
                self.n += 1
                if self.n > 1:
                    raise HarnessError("reservation over ceiling")
                structural = {"verdict": "pass", "confidence": 0.9,
                              "supported": 0.9, "cost": 0.001,
                              "input_tokens": 700, "output_tokens": 50,
                              "is_fallback": False, "model": "jev-test",
                              "site": "repo_summary"}
                judgment = {"pack_id": pack["id"],
                            "axes": {"stage": "prep"}, "attention": {},
                            "nouls": {}, "is_fallback": False,
                            "evidence": []}
                return (JevEvaluationResult("pass", 0.9, 0.9, {}, [],
                                            cost=0.001, input_tokens=700,
                                            output_tokens=50),
                        structural, judgment)

        flaky = _Flaky()
        env = analyze_repo(self.tmp.name, self.pack,
                           lambda c: flaky,
                           state_path=self.state_path, symbol_limit=0)
        self.assertEqual(env["coverage"]["stop_reason"], "run_budget")
        # only the successful call persisted; the refusal is retried later
        self.assertEqual(env["coverage"]["judged"], 1)
        self.assertEqual(len(load_judgment_rows(self.state_path)), 1)


class RenderAndWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        _tree(self.tmp.name)
        self.pack = validate_repo_summary_pack(repo_pack())
        self.env = analyze_repo(self.tmp.name, self.pack, _stub_policy_factory(),
                                symbol_limit=3, generated_at="T")

    def test_map_covers_stages_budget_and_fallback_flags(self):
        md = render_repo_map(self.env)
        for header in ("# REPO-MAP", "## Stage: prep", "## Stage: waist",
                       "## Axis tallies", "## Attention", "## Mechanical totals"):
            self.assertIn(header, md)
        self.assertIn("site=`repo_summary`", md)
        self.assertIn("ledger verify", md)
        # declared vocabulary only: never an invented axis name in tallies
        self.assertNotIn("bogus", md)

    def test_writers_emit_posix_newlines_and_valid_json(self):
        json_path = os.path.join(self.tmp.name, "out", "env.json")
        write_envelope(self.env, json_path)
        raw = Path(json_path).read_bytes()
        self.assertNotIn(b"\r", raw)
        self.assertEqual(json.loads(raw.decode())["schema"], SCHEMA)
        map_path = os.path.join(self.tmp.name, "out", "MAP.md")
        write_map(render_repo_map(self.env), map_path)
        self.assertNotIn(b"\r", Path(map_path).read_bytes())


class CliFaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        _tree(self.tmp.name)
        self.settings = load_settings()
        self.settings.jev_api_key = None  # hermetic: never live in tests
        self.settings.ledger_path = os.path.join(self.tmp.name, "ledger.jsonl")
        self.pack_path = os.path.join(self.tmp.name, "pack.json")
        Path(self.pack_path).write_text(
            json.dumps(repo_pack()), encoding="utf-8")

    def _opts(self, **overrides):
        base = dict(pack=self.pack_path, root=self.tmp.name,
                    save_to=os.path.join(self.tmp.name, "env.json"),
                    map=os.path.join(self.tmp.name, "MAP.md"),
                    state=None, limit=None, symbols=3, run_budget=None,
                    max_cost=None,
                    out=os.path.join(self.tmp.name, "report.json"))
        base.update(overrides)
        return argparse.Namespace(**base)

    def test_unkeyed_cli_face_writes_all_artifacts_hermetically(self):
        opts = self._opts()
        _cmd_repo_summary(opts, self.settings)
        env = json.loads(Path(opts.save_to).read_text(encoding="utf-8"))
        self.assertEqual(env["coverage"]["stop_reason"], "complete")
        self.assertGreater(env["coverage"]["judged"], 0)
        self.assertEqual(env["coverage"]["live_judged"], 0)
        self.assertGreater(env["coverage"]["fallbacks"], 0)
        self.assertEqual(env["spend"]["cost_usd"], 0.0)
        self.assertTrue(Path(opts.map).is_file())
        self.assertIn("# REPO-MAP", Path(opts.map).read_text(encoding="utf-8"))
        # default state derived from --save-to
        self.assertEqual(len(load_judgment_rows(str(opts.save_to) + ".judgments.jsonl")),
                         env["coverage"]["judged"])
        # ledgered hermetically to the temp path only
        rows = [json.loads(line) for line in
                Path(self.settings.ledger_path).read_text(encoding="utf-8").splitlines()
                if line.strip()]
        self.assertEqual(len(rows), env["coverage"]["judged"])
        self.assertTrue(all(r["site"] == "repo_summary" for r in rows))

    def test_limit_bounds_the_pilot(self):
        opts = self._opts(limit=2, save_to=None, map=None)
        _cmd_repo_summary(opts, self.settings)
        report = json.loads(Path(opts.out).read_text(encoding="utf-8"))
        self.assertEqual(report["coverage"]["files_listed"], 2)
        self.assertEqual(report["coverage"]["judged_files"], 2)


def _single_row_policy(judgment, structural):
    """A stub policy returning one canned (result, structural, judgment)."""

    class _Policy:
        def evaluate_repo_summary(self, state_el, pack, task_id=None):
            result = JevEvaluationResult(
                "pass" if not judgment.get("is_fallback") else "fail",
                0.5, 0.5, {}, [],
                cost=float(structural.get("cost") or 0.0),
                input_tokens=int(structural.get("input_tokens") or 0),
                output_tokens=int(structural.get("output_tokens") or 0),
                is_fallback=bool(judgment.get("is_fallback")))
            return result, dict(structural), dict(judgment)

    return _Policy()


class DefensiveAggregateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        _tree(self.tmp.name)
        self.pack = validate_repo_summary_pack(repo_pack())
        self.state_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.state_dir.cleanup)
        self.state_path = os.path.join(self.state_dir.name, "judged.jsonl")

    def test_resume_state_tolerates_blank_lines_and_a_broken_tail(self):
        from harness.repo_summary import load_judgment_rows
        with open(self.state_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps({"id": "file:a"}) + "\n")
            fh.write("\n")
            fh.write(json.dumps({"id": "file:b"}) + "\n")
            fh.write('{"id": "file:c", "trunc')  # interrupted append
        rows = load_judgment_rows(self.state_path)
        self.assertEqual([r["id"] for r in rows], ["file:a", "file:b"])
        # nonexistent path -> no rows
        self.assertEqual(load_judgment_rows(None), [])
        self.assertEqual(
            load_judgment_rows(os.path.join(self.state_dir.name, "none.jsonl")),
            [])

    def test_foreign_drive_exclude_is_skipped_not_fatal(self):
        env = analyze_repo(
            self.tmp.name, self.pack, _stub_policy_factory(),
            state_path=self.state_path, symbol_limit=0,
            exclude=["Z:\\nope\\env.json", None, ""], generated_at="T")
        self.assertEqual(env["coverage"]["stop_reason"], "complete")

    def test_fallback_with_real_cost_is_persisted_not_a_budget_stop(self):
        structural = {"verdict": "fail", "confidence": 0.0, "supported": 0.0,
                      "cost": 0.01, "input_tokens": 240, "output_tokens": 30,
                      "is_fallback": True, "model": "jev-test",
                      "site": "repo_summary"}
        judgment = {"pack_id": "fixture-repo-v1",
                    "axes": {"stage": "adjudicate", "handling": None},
                    "attention": {"level": None, "value": None,
                                  "confidence": 0.0},
                    "nouls": {"waist_relevant": None},
                    "is_fallback": True,
                    "evidence": ["unkeyed: exceeds nothing"]}
        policy = _single_row_policy(judgment, structural)
        env = analyze_repo(self.tmp.name, self.pack, lambda c: policy,
                           state_path=self.state_path, symbol_limit=0,
                           generated_at="T")
        # cost > 0 -> NOT a budget refusal: every candidate gets judged
        self.assertEqual(env["coverage"]["stop_reason"], "complete")
        self.assertEqual(env["coverage"]["judged"],
                         env["coverage"]["candidates"])
        self.assertEqual(env["coverage"]["fallbacks"], env["coverage"]["judged"])
        # unknown axis is skipped in aggregation; noul None is unanswered
        self.assertEqual(env["spend"]["cost_usd"],
                         round(0.01 * env["coverage"]["judged"], 6))
        self.assertEqual(env["nouls"]["waist_relevant"]["unanswered"],
                         env["coverage"]["judged"])

    def test_unknown_axis_in_rows_is_skipped_not_counted(self):
        structural = {"verdict": "pass", "confidence": 0.9, "supported": 0.9,
                      "cost": 0.0, "input_tokens": 10, "output_tokens": 1,
                      "is_fallback": False, "model": "jev-test",
                      "site": "repo_summary"}
        judgment = {"pack_id": "fixture-repo-v1",
                    "axes": {"stage": "prep", "bogus_axis": "x"},
                    "attention": {"level": "central", "value": 0.9,
                                  "confidence": 0.9},
                    "nouls": {"waist_relevant": 0.7},
                    "is_fallback": False, "evidence": []}
        policy = _single_row_policy(judgment, structural)
        env = analyze_repo(self.tmp.name, self.pack, lambda c: policy,
                           state_path=self.state_path, symbol_limit=0,
                           generated_at="T")
        self.assertNotIn("bogus_axis", env["axes"])
        self.assertEqual(env["axes"]["stage"]["prep"], env["coverage"]["judged"])
        self.assertEqual(env["nouls"]["waist_relevant"]["true"],
                         env["coverage"]["judged"])

    def test_zero_cost_budget_refusal_stops_without_persisting_and_renders_pending(self):
        structural = {"verdict": "fail", "confidence": 0.0, "supported": 0.0,
                      "cost": 0.0, "input_tokens": 0, "output_tokens": 0,
                      "is_fallback": True, "model": "jev-test",
                      "site": "repo_summary"}
        judgment = {"pack_id": "fixture-repo-v1", "axes": {},
                    "attention": {"level": None}, "nouls": {},
                    "is_fallback": True,
                    "evidence": ["Jev worst-case $0.04 exceeds remaining budget"]}
        calls = {"n": 0}
        good_structural = {"verdict": "pass", "confidence": 0.9,
                           "supported": 0.9, "cost": 0.002,
                           "input_tokens": 48, "output_tokens": 5,
                           "is_fallback": False, "model": "jev-test",
                           "site": "repo_summary"}
        good_judgment = {"pack_id": "fixture-repo-v1",
                         "axes": {"stage": "prep"},
                         "attention": {"level": "notable"},
                         "nouls": {}, "is_fallback": False,
                         "evidence": []}

        def factory(cumulative):
            calls["n"] += 1
            if calls["n"] == 1:
                return _single_row_policy(good_judgment, good_structural)
            return _single_row_policy(judgment, structural)

        env = analyze_repo(self.tmp.name, self.pack, factory,
                           state_path=self.state_path, symbol_limit=0,
                           run_budget=0.05, generated_at="T")
        # first row is a real judgment, second is the refusal -> stop
        self.assertEqual(env["coverage"]["stop_reason"], "run_budget")
        self.assertEqual(env["coverage"]["judged"], 1)
        self.assertEqual(len(load_judgment_rows(self.state_path)), 1)
        # rendering a stopped envelope emits the pending section
        md = render_repo_map(env)
        self.assertIn("## Pending", md)
        self.assertIn("file:", md)


class CliFaceEdgeTests(CliFaceTests):
    def test_invalid_pack_refuses_with_fatal_error(self):
        bad_path = os.path.join(self.tmp.name, "bad-pack.json")
        Path(bad_path).write_text(json.dumps({"id": "x"}), encoding="utf-8")
        opts = self._opts(pack=bad_path, save_to=None, map=None)
        with self.assertRaises(HarnessError):
            _cmd_repo_summary(opts, self.settings)

    def test_factory_returns_none_when_governor_cannot_compose(self):
        settings = load_settings({"jev_api_key": "jev-key"})
        with mock.patch("harness.cli.jev_face_governor", return_value=None):
            factory = _repo_summary_policy_factory(settings, None, None, None)
            self.assertIsNone(factory(0.0))


class PolicyFactoryTests(unittest.TestCase):
    def test_run_budget_stops_before_composing_another_policy(self):
        settings = load_settings({"jev_api_key": "jev-key"})
        factory = _repo_summary_policy_factory(settings, 0.10, 0.05, None)
        # below the budget a governor-backed policy composes (no network)...
        policy = factory(0.0)
        self.assertIsNotNone(policy)
        self.assertIsNotNone(policy.governor)
        self.assertLessEqual(policy.governor.max_cost, 0.10)
        # ...and cumulative + worst-case beyond the run budget refuses.
        from harness.jev_policy import jev_cost_ceiling
        self.assertIsNone(factory(0.05 - 0.0001 + jev_cost_ceiling()))

    def test_unkeyed_factory_never_budget_stops(self):
        settings = load_settings()
        settings.jev_api_key = None
        factory = _repo_summary_policy_factory(settings, None, 0.0, None)
        policy = factory(999.0)
        self.assertIsNotNone(policy)
        self.assertFalse(policy.keyed)


if __name__ == "__main__":
    unittest.main()
