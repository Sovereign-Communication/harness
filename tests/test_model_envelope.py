"""MS envelope + keyed Jev CLI-face governor parity.

Two contracts this module pins:

1. The model-selection (MS) envelope — ``model_requested`` vs
   ``model_observed`` — rides every apply/agent/batch result so consumers
   (CLI, MCP, GUI, site export) can see rotation/trust-step/escalation
   divergence without re-deriving it from ``rounds``.
2. A keyed thin Jev CLI face (issue-sort, route, log-judgment) composes the
   shared spend governor via ``session.jev_face_governor`` — the same
   preflight MCP and the web UI already get — instead of silently degrading
   to keyword fallback on a keyed machine. Unkeyed stays governor-free.

Everything here is hermetic: fake transports and a stubbed policy owner.
"""
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from harness.batch import BatchOptions
from harness.config import load_settings
from harness.results import model_envelope
from harness.session import jev_face_governor
from harness.spend import SpendGovernor
from tests._applyfixture import ApplyFixture, CHANGED, scripted_run
from tests._fake import comp
from tests.test_jev_issue_sort import sample_pack
from tests.test_jev_log_envelope import LOG_TEXT, log_pack


def _route_pack():
    return {
        "id": "route-test-v1",
        "rungs": [
            {"rung_id": "scout", "tier": "T0", "model": "helper-lite",
             "cost_class": "free",
             "guidance": ["typo", "rename", "docstring", "format"]},
        ],
    }


class ModelEnvelopeHelperTests(unittest.TestCase):
    def test_dedupes_and_drops_empty_models(self):
        env = model_envelope(model_requested="req/model",
                             model_observed=["a/m", None, "a/m", "b/m"])
        self.assertEqual(env, {"model_requested": "req/model",
                               "model_observed": ["a/m", "b/m"]})

    def test_defaults_are_honest_empty(self):
        env = model_envelope()
        self.assertIsNone(env["model_requested"])
        self.assertEqual(env["model_observed"], [])


class ApplyModelEnvelopeTests(ApplyFixture):
    def test_single_apply_result_carries_ms_envelope(self):
        p = self.make_file()
        _, _, _, engine = self.make_env(posts=[comp(CHANGED)],
                                        run=scripted_run([(0, "")]))
        result = engine.apply_edit(task_id="ms1", file_path=p,
                                   instruction="change", verify_cmd="check",
                                   require_consent=False)
        self.assertEqual(result["status"], "ok")
        self.assertIsInstance(result["model_requested"], str)
        self.assertTrue(result["model_requested"])
        # Observed = the models that actually served, from the rounds list.
        observed = []
        for r in result["rounds"]:
            if r.get("model") and r["model"] not in observed:
                observed.append(r["model"])
        self.assertEqual(result["model_observed"], observed)

    def test_batch_envelope_merges_child_ms_envelopes(self):
        a = self.make_file()
        b = os.path.join(self.dir.name, "other.py")
        with open(b, "w", encoding="utf-8") as f:
            f.write("def mul(a, b):\n    return a * b\n")
        _, _, _, engine = self.make_env(posts=[comp(CHANGED), comp(CHANGED)],
                                        run=scripted_run([(0, "")]))
        result = engine.apply_batch(
            [a, b], options=BatchOptions(instruction="change",
                                         verify_cmd="check",
                                         require_consent=False))
        self.assertEqual(result["status"], "ok")
        for child in result["results"]:
            self.assertIn("model_requested", child)
            self.assertIn("model_observed", child)
        # Envelope requested = first child's requested primary.
        self.assertEqual(result["model_requested"],
                         next(c["model_requested"] for c in result["results"]
                              if c.get("model_requested")))
        observed = []
        for child in result["results"]:
            for model in child["model_observed"]:
                if model not in observed:
                    observed.append(model)
        self.assertEqual(result["model_observed"], observed)


class JevFaceGovernorTests(unittest.TestCase):
    def _settings(self, keyed):
        settings = load_settings()
        settings.jev_api_key = "k" if keyed else None
        return settings

    def test_unkeyed_face_needs_no_governor(self):
        self.assertIsNone(jev_face_governor(self._settings(False)))
        self.assertIsNone(jev_face_governor(self._settings(False), 0.05))

    def test_keyed_face_defaults_to_configured_ceiling(self):
        settings = self._settings(True)
        gov = jev_face_governor(settings)
        self.assertIsInstance(gov, SpendGovernor)
        self.assertEqual(gov.max_cost, settings.max_cost)

    def test_override_is_capped_at_hard_max(self):
        # Same rule as governor_for: an override past HARD_MAX_COST is
        # refused fail-closed, never silently clamped.
        from harness.errors import HarnessError
        with self.assertRaises(HarnessError):
            jev_face_governor(self._settings(True), 5.0)
        gov = jev_face_governor(self._settings(True), 0.04)
        self.assertEqual(gov.max_cost, 0.04)


class KeyedCliFaceParityTests(unittest.TestCase):
    """Keyed CLI faces must compose the shared governor (parity with MCP)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _keyed_settings(self):
        settings = load_settings()
        settings.jev_api_key = "k"
        settings.ledger_path = os.path.join(self.tmp.name, "ledger.jsonl")
        return settings

    def _run_face(self, argv, stub):
        """Run a keyed CLI face with a stubbed policy owner; return the
        kwargs the face passed to policy_for (governor wiring assertions)."""
        from harness import cli
        owner = mock.MagicMock(return_value=stub)
        with mock.patch("harness.cli.load_settings", return_value=self._keyed_settings()), \
                mock.patch("harness.cli.policy_for", owner):
            cli.main(argv)
        return owner.call_args.kwargs

    def test_keyed_route_face_gets_governor_and_honors_max_cost(self):
        stub = mock.MagicMock()
        stub.evaluate_model_route.return_value = (
            SimpleNamespace(answers={}, reasons=[]),
            {"verdict": "pass", "site": "model_route"},
            {"rung_id": "scout", "tier": "T0", "model": "helper-lite",
             "cost_class": "free", "is_fallback": True,
             "pack_id": "route-test-v1"})
        pack_path = os.path.join(self.tmp.name, "route.json")
        with open(pack_path, "w", encoding="utf-8") as handle:
            json.dump(_route_pack(), handle)
        kwargs = self._run_face(
            ["route", "--goal", "fix a typo", "--pack", pack_path,
             "--max-cost", "0.04",
             "--out", os.path.join(self.tmp.name, "route-out.json")],
            stub)
        self.assertIsInstance(kwargs["governor"], SpendGovernor)
        self.assertEqual(kwargs["governor"].max_cost, 0.04)

    def test_keyed_log_judgment_face_gets_governor_capped_at_hard(self):
        stub = mock.MagicMock()
        stub.evaluate_log_item.side_effect = lambda *a, **k: (
            SimpleNamespace(), {},
            {"bucket": None, "score": {}, "is_fallback": True,
             "path_id": None, "suggested_next_action": None})
        log_path = os.path.join(self.tmp.name, "run.log")
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write(LOG_TEXT)
        pack_path = os.path.join(self.tmp.name, "pack.json")
        with open(pack_path, "w", encoding="utf-8") as handle:
            json.dump(log_pack(), handle)
        out_path = os.path.join(self.tmp.name, "analysis.json")
        kwargs = self._run_face(
            ["log-judgment", "--log", log_path, "--pack", pack_path,
             "--max-cost", "0.08", "--save-to", out_path,
             "--out", os.path.join(self.tmp.name, "stdout.json")],
            stub)
        self.assertIsInstance(kwargs["governor"], SpendGovernor)
        self.assertEqual(kwargs["governor"].max_cost, 0.08)
        # The batch still judged every extracted item through the policy owner.
        with open(out_path, encoding="utf-8") as handle:
            analysis = json.load(handle)
        self.assertEqual(analysis["coverage"]["total_items"],
                         stub.evaluate_log_item.call_count)
        self.assertGreater(analysis["coverage"]["total_items"], 0)
        # An override above the hard ceiling fails closed at parse/run time.
        from harness import cli
        settings = self._keyed_settings()
        with mock.patch("harness.cli.load_settings", return_value=settings):
            with self.assertRaises(SystemExit):
                cli.main(["log-judgment", "--log", log_path,
                          "--pack", pack_path, "--max-cost", "3.0",
                          "--out", os.path.join(self.tmp.name, "x.json")])

    def test_keyed_issue_sort_face_gets_governor(self):
        stub = mock.MagicMock()
        stub.evaluate_issue_sort.return_value = (
            SimpleNamespace(answers={}, reasons=[]),
            {"verdict": "pass", "site": "issue_sort"},
            {"bucket": "auth", "path_id": "path/auth",
             "suggested_next_action": "open auth backlog",
             "pack_id": "ops-attention-v1", "is_fallback": True})
        pack_path = os.path.join(self.tmp.name, "issue-pack.json")
        with open(pack_path, "w", encoding="utf-8") as handle:
            json.dump(sample_pack(), handle)
        kwargs = self._run_face(
            ["issue-sort", "--issue", "auth token expired",
             "--pack", pack_path,
             "--out", os.path.join(self.tmp.name, "issue-out.json")],
            stub)
        self.assertIsInstance(kwargs["governor"], SpendGovernor)
        self.assertEqual(kwargs["governor"].max_cost,
                         self._keyed_settings().max_cost)

    def test_unkeyed_face_stays_governor_free(self):
        stub = mock.MagicMock()
        stub.evaluate_model_route.return_value = (
            SimpleNamespace(answers={}, reasons=[]),
            {"verdict": "pass", "site": "model_route"},
            {"rung_id": "scout", "tier": "T0", "model": "helper-lite",
             "cost_class": "free", "is_fallback": True,
             "pack_id": "route-test-v1"})
        pack_path = os.path.join(self.tmp.name, "route2.json")
        with open(pack_path, "w", encoding="utf-8") as handle:
            json.dump(_route_pack(), handle)
        from harness import cli
        settings = load_settings()
        settings.jev_api_key = None
        settings.ledger_path = os.path.join(self.tmp.name, "u-ledger.jsonl")
        owner = mock.MagicMock(return_value=stub)
        with mock.patch("harness.cli.load_settings", return_value=settings), \
                mock.patch("harness.cli.policy_for", owner):
            cli.main(["route", "--goal", "typo", "--pack", pack_path,
                      "--out", os.path.join(self.tmp.name, "u-out.json")])
        self.assertIsNone(owner.call_args.kwargs["governor"])


if __name__ == "__main__":
    unittest.main()
