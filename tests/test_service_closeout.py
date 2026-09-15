"""Structured close-out: the non-authoritative tally artifact for an
exhausted judge, and CLI/UI verify-lane parity through the canonical
service layer."""
import tempfile
import unittest


class _Sink:
    def __init__(self):
        self.events = []

    def __call__(self, event):
        self.events.append(event)


class TallyArtifactTests(unittest.TestCase):
    """2026-09-13 ruling: an exhausted structured judge still produces a
    reviewable artifact -- the deterministic tally, clearly marked as
    carrying NO verdict authority."""

    def _run(self, judge_post, spec_post=None):
        import json
        from tests._fake import FakeTransport, comp, m
        from harness.spend import SpendGovernor
        from harness.panel import panel_judge
        import os
        vote = comp(json.dumps({"c1": {"real": False, "confidence": 0.9}}))
        if spec_post is None:
            spec_post = comp("specialist prose, no json")
        with tempfile.TemporaryDirectory() as td:
            fake = FakeTransport(models=[m("x/a"), m("x/j")],
                                 posts=[vote, judge_post, spec_post])
            gov = SpendGovernor(fake, "sk-test", byok_prefixes_path=os.path.join(
                td, "byok.json"))
            return panel_judge(transport=fake, api_key="k", governor=gov,
                               prompt="Q?", panel=["x/a"], judge="x/j",
                               run_convergence=True)

    def test_artifact_present_on_exhausted_judge(self):
        from tests._fake import comp
        result = self._run(comp("prose without json"))
        conv = result["convergence"]
        self.assertEqual(conv["status"], "error")
        artifact = result["consensus"]["tally_artifact"]
        self.assertFalse(artifact["authoritative"])
        self.assertIn("not a verdict", artifact["note"])
        self.assertEqual(artifact["judge_synthesis_status"], "unparseable")
        self.assertIn("c1", artifact["claims"])
        self.assertEqual(artifact["claims"]["c1"]["verdict"], "not_real")

    def test_artifact_present_when_specialist_ladder_fails(self):
        # The specialist lane returns its own error envelope; the artifact
        # must still render from the deterministic tally.
        from tests._fake import comp
        result = self._run(comp("no json here either"))
        artifact = result["consensus"]["tally_artifact"]
        self.assertFalse(artifact["authoritative"])
        self.assertEqual(artifact["voted_by"], 1)
        self.assertEqual(artifact["of_panel"], 1)

    def test_authoritative_judge_has_no_artifact(self):
        import json
        from tests._fake import comp
        judge = comp(json.dumps({"verdict": "clean", "agreement": "high",
                                 "confidence": 0.95, "defer": False}))
        spec = comp(json.dumps({"converged": True, "agreement": "high",
                                "confidence": 0.95, "claims": {}}))
        result = self._run(judge, spec_post=spec)
        self.assertNotIn("tally_artifact", result["consensus"])
        self.assertEqual(result["judge_synthesis_status"], "parseable")


class ServiceParityTests(unittest.TestCase):
    """CLI verify and the UI server's verify runner consume the SAME service
    assembly: one prompt builder, one cancelled envelope, one lane policy."""

    def _settings(self):
        # Complete enough for router_for (the service's specialist-pool
        # default) to build a Router from this stub.
        return type("S", (), {"use_free": True, "panel_pool": ["m/a"],
                              "panel": ["m/a"], "apply_model": "m/apply",
                              "apply_pool": None, "specialist_pool": None,
                              "escalation_model": None, "escalation_pool": [],
                              "judge_top": None, "allow_escalation": False,
                              "convergence_model": None,
                              "judge": "m/j", "reasoning_effort": "auto",
                              "reasoning_token_budget": 0.4,
                              "max_panelists": 2, "max_cost": 0.02,
                              "expect_key_label": False})()

    def test_build_verify_prompt_sources(self):
        import os
        from harness.service import build_verify_prompt
        with tempfile.TemporaryDirectory() as td:
            pf = os.path.join(td, "prompt.txt")
            with open(pf, "w", encoding="utf-8") as f:
                f.write("\ufeffhello prompt")  # BOM: PowerShell redirects
            prompt, lint = build_verify_prompt(prompt_file=pf)
            self.assertEqual(prompt, "hello prompt")
            self.assertIsNone(lint)
            prompt, _lint = build_verify_prompt(prompt="inline")
            self.assertEqual(prompt, "inline")
            with self.assertRaises(ValueError):
                build_verify_prompt()

    def test_claims_prompt_requires_source(self):
        from harness.service import build_verify_prompt
        with self.assertRaises(ValueError):
            build_verify_prompt(claims_file="x.json")

    def test_run_verify_returns_cancelled_envelope(self):
        import unittest.mock
        from harness import service
        from harness.errors import ToolCancelled

        def cancelled(**kwargs):
            raise ToolCancelled()
        gov = type("G", (), {"spent": 0.0007, "max_cost": 0.02,
                             "cost_by_model": lambda self: {"m/a": 0.0007}})()
        with unittest.mock.patch.object(service, "governor_for",
                                        return_value=("k", gov)), \
             unittest.mock.patch.object(service, "ledger_for",
                                        return_value=unittest.mock.Mock()), \
             unittest.mock.patch.object(service, "pre_run_warning"), \
             unittest.mock.patch.object(service, "panel_judge",
                                        side_effect=cancelled):
            result = service.run_verify(self._settings(), prompt="hi",
                                        task_id="t/1")
        self.assertEqual(result["status"], "cancelled")
        self.assertAlmostEqual(result["actual_cost"], 0.0007, places=9)
        self.assertIn("meta", result)

    def test_run_verify_attaches_cost_and_meta(self):
        import unittest.mock
        from harness import service

        def fake_panel_judge(**kwargs):
            self.assertIsNone(kwargs["cancel_check"])
            return {"status": "ok", "verdict": "x", "actual_cost": 0.001,
                    "max_cost_ceiling": 0.02}
        gov = type("G", (), {"spent": 0.001, "max_cost": 0.02,
                             "cost_by_model": lambda self: {"m/a": 0.001}})()
        with unittest.mock.patch.object(service, "governor_for",
                                        return_value=("k", gov)), \
             unittest.mock.patch.object(service, "ledger_for",
                                        return_value=unittest.mock.Mock()), \
             unittest.mock.patch.object(service, "pre_run_warning"), \
             unittest.mock.patch.object(service, "panel_judge",
                                        side_effect=fake_panel_judge):
            result = service.run_verify(self._settings(), prompt="hi",
                                        task_id="t/1")
        self.assertAlmostEqual(result["actual_cost"], 0.001, places=9)
        self.assertEqual(result["meta"]["judge"], "m/j")

    def test_cancelled_envelope_shape_matches_terminal_envelopes(self):
        from harness.service import cancelled_envelope
        gov = type("G", (), {"spent": 0.01, "max_cost": 0.02,
                             "cost_by_model": lambda self: {}})()
        env = cancelled_envelope(gov, self._settings())
        for key in ("status", "verdict", "judge_synthesis_status",
                    "panel_results", "panel_failures", "actual_cost",
                    "max_cost_ceiling", "cost_by_model", "meta"):
            self.assertIn(key, env)

    def test_cli_verify_and_server_runner_same_assembly(self):
        """CLI == server parity at the ONE seam: the CLI's verify command and
        the UI server's verify runner must drive panel_judge with the same
        kwargs (mod lane inputs) -- the unification contract that replaced
        the CLI's own assembly."""
        import unittest.mock
        from harness import cli, service
        captured = {}

        def fake_panel_judge(**kwargs):
            captured.update(kwargs)
            return {"status": "ok", "verdict": {"decision": "yes"},
                    "actual_cost": 0.0, "max_cost_ceiling": 0.02}
        gov = type("G", (), {"spent": 0.0, "max_cost": 0.02,
                             "cost_by_model": lambda self: {}})()
        with unittest.mock.patch.object(service, "governor_for",
                                        return_value=("k", gov)), \
             unittest.mock.patch.object(service, "ledger_for",
                                        return_value=unittest.mock.Mock()), \
             unittest.mock.patch.object(service, "pre_run_warning"), \
             unittest.mock.patch.object(service, "panel_judge",
                                        side_effect=fake_panel_judge):
            settings = self._settings()
            cli._run_claims_verify(settings, prompt="hi", task_id="t/cli")
            cli_kwargs = dict(captured)
            captured.clear()
            service.run_verify(settings, prompt="hi", task_id="t/ui")
            ui_kwargs = dict(captured)
        # Only the task id, the per-call transport instance, and the
        # CLI-only convergence inputs may differ; every assembly kwarg
        # must be identical.
        for k in ("task_id", "transport", "convergence_model",
                  "specialist_pool", "run_convergence", "claim_polarity"):
            cli_kwargs.pop(k, None)
            ui_kwargs.pop(k, None)
        self.assertEqual(cli_kwargs, ui_kwargs,
                         "CLI and server must assemble the verify lane "
                         "identically (service.run_verify is the one owner)")
        self.assertIn("panel", cli_kwargs)
        self.assertIn("judge", cli_kwargs)

    def test_cli_verify_delegates_to_service(self):
        """The CLI keeps a named seam but must not assemble the lane: it
        delegates to service.run_verify (one owner of the assembly)."""
        import unittest.mock
        from harness import cli
        called = {}

        def fake_verify(settings, **kwargs):
            called.update(kwargs)
            return {"status": "ok"}
        with unittest.mock.patch.object(cli, "_service_verify",
                                        side_effect=fake_verify):
            cli._run_claims_verify(self._settings(), prompt="hi",
                                   task_id="t/1", converge=True,
                                   reassurance_claims="r1, r2")
        # Pure forwarding: unset flags pass through as None and the
        # SERVICE defaults them (router pool, convergence model, ids).
        self.assertEqual(called["task_id"], "t/1")
        self.assertTrue(called["converge"])
        self.assertEqual(called["reassurance_claims"], "r1, r2")
        self.assertIsNone(called["specialist_pool"])
        self.assertIsNone(called["max_tokens"])
        self.assertIsNone(called["convergence_model"])
        self.assertIsNone(called["judge"])


if __name__ == "__main__":
    unittest.main()
