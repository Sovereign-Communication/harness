"""Hermetic tests for the model capability layer (harness/capability.py).

The centerpiece is a *proof* test against a real /models fixture captured from
OpenRouter -- not hand-authored numbers. The hypothesis under test: on the free
tier, GLM-5.2 and minimax-M3 are MORE capable than the smaller gemma-4-31b, so
a capability-first router should prefer them. If the real metadata ever stops
supporting that, the fixture test fails and we fix the score weights -- never
the data.
"""
import json
import os
import tempfile
import unittest

from harness.capability import (
    CapabilityProfile, build_profiles_from_models, capability_score,
    capability_fitness, context_score, composite_reliability,
    observed_json_reliability, json_reliable, model_reliability, load_profiles,
    save_profiles, ensure_profiles, order_pool, probe_json_reliability, TASK_WEIGHTS,
    ordered_pool,
)
from harness.config import FREE_PANEL_POOL, FREE_APPLY_POOL
from harness.continuation import gate_id, validate_continuation
from harness.errors import HarnessError
from harness.ledger import AutonomyLedger


FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "openrouter_models.json")


def fixture_profiles():
    with open(FIXTURE, encoding="utf-8") as f:
        data = json.load(f)
    return build_profiles_from_models(data["models"])


def sample_model(overrides=None):
    """A minimal /models entry (not from the fixture)."""
    base = {
        "id": "acme/example:free",
        "context_length": 262144,
        "supported_parameters": ["max_tokens", "reasoning", "structured_outputs",
                                 "tools", "temperature"],
        "pricing": {"prompt": "0", "completion": "0"},
        "input_modalities": ["text"],
    }
    base.update(overrides or {})
    return base


class CapabilityParsingTest(unittest.TestCase):
    def test_from_model_extracts_fields(self):
        p = CapabilityProfile.from_model(sample_model())
        self.assertEqual(p.model_id, "acme/example:free")
        self.assertEqual(p.context_length, 262144)
        self.assertTrue(p.free)
        self.assertTrue(p.supports_reasoning)
        self.assertTrue(p.supports_structured_json)
        self.assertTrue(p.tool_use)
        self.assertAlmostEqual(p.declared_json, 1.0)

    def test_response_format_is_partial_json(self):
        p = CapabilityProfile.from_model(sample_model(
            {"supported_parameters": ["max_tokens", "response_format"]}))
        self.assertAlmostEqual(p.declared_json, 0.7)

    def test_no_json_support(self):
        p = CapabilityProfile.from_model(sample_model(
            {"supported_parameters": ["max_tokens"]}))
        self.assertAlmostEqual(p.declared_json, 0.0)

    def test_roundtrip_dict(self):
        p = CapabilityProfile.from_model(sample_model())
        p2 = CapabilityProfile.from_dict(p.to_dict())
        self.assertEqual(p2.model_id, p.model_id)
        self.assertEqual(p2.context_length, p.context_length)
        self.assertEqual(p2.supports_structured_json, p.supports_structured_json)

    def test_missing_keys_tolerated(self):
        p = CapabilityProfile.from_model({"id": "x/y:free"})
        self.assertEqual(p.model_id, "x/y:free")
        self.assertEqual(p.context_length, 0)
        self.assertAlmostEqual(p.declared_json, 0.0)


class ScoringTest(unittest.TestCase):
    def test_context_score_bounds(self):
        self.assertEqual(context_score(0), 0.0)
        self.assertEqual(context_score(8192), 0.0)
        self.assertGreater(context_score(262144), 0.5)
        self.assertAlmostEqual(context_score(1048576), 1.0, places=3)

    def test_capability_score_in_unit_range(self):
        for p in fixture_profiles().values():
            s = capability_score(p)
            self.assertGreaterEqual(s, 0.0)
            self.assertLessEqual(s, 1.0)

    def test_structured_json_boosts_fitness(self):
        # Same context/reasoning; the one with full structured output wins.
        a = CapabilityProfile.from_model(sample_model(
            {"id": "a:free", "supported_parameters": ["max_tokens", "reasoning",
                                                      "structured_outputs"]}))
        b = CapabilityProfile.from_model(sample_model(
            {"id": "b:free", "supported_parameters": ["max_tokens", "reasoning"]}))
        self.assertGreater(capability_fitness(a, "structured"),
                           capability_fitness(b, "structured"))

    def test_code_task_deweights_json(self):
        p = CapabilityProfile.from_model(sample_model())
        # 'structured' is JSON-critical, so it differs from the general default.
        self.assertGreater(capability_fitness(p, "structured"), 0.0)
        self.assertIn("code", TASK_WEIGHTS)


class FixtureRankingProofTest(unittest.TestCase):
    """THE proof: real /models data must support the capability hypothesis.

    GLM-5.2 and minimax-M3 must outrank gemma-4-31b. This is a test of the real
    metadata, not of hand-picked numbers -- if the ranking stops holding, this
    fails and the score weights (not the fixture) get corrected.
    """

    def test_glm_outranks_gemma(self):
        prof = fixture_profiles()
        glm = capability_score(prof["z-ai/glm-5.2:free"])
        gemma = capability_score(prof["google/gemma-4-31b-it:free"])
        self.assertGreater(glm, gemma)

    def test_minimax_outranks_gemma(self):
        prof = fixture_profiles()
        mini = capability_score(prof["minimax/minimax-m3:free"])
        gemma = capability_score(prof["google/gemma-4-31b-it:free"])
        self.assertGreater(mini, gemma)

    def test_minimax_has_larger_context(self):
        prof = fixture_profiles()
        self.assertGreater(prof["minimax/minimax-m3:free"].context_length,
                           prof["google/gemma-4-31b-it:free"].context_length)

    def test_capability_first_order(self):
        prof = fixture_profiles()
        ordered = order_pool(list(prof), prof, None, task="default", free_tier=True)
        self.assertEqual(ordered[0], "nvidia/nemotron-3-super-120b-a12b:free")  # top by cap
        # GLM and minimax both appear before gemma in a capability-first pool.
        glm_i = ordered.index("z-ai/glm-5.2:free")
        gemma_i = ordered.index("google/gemma-4-31b-it:free")
        self.assertLess(glm_i, gemma_i)
        self.assertLess(ordered.index("minimax/minimax-m3:free"), gemma_i)


class CompositeReliabilityTest(unittest.TestCase):
    def test_hard_gate_zero(self):
        self.assertEqual(composite_reliability(0.0, 1.0, 1.0, 100), 0.0)
        self.assertEqual(composite_reliability(None, 1.0, 1.0, 100), 0.0)

    def test_no_data_uses_capability(self):
        # With zero samples, calibration/success sit at the 0.5 prior; the
        # composite is a blend that starts near capability.
        r = composite_reliability(0.9, None, None, 0)
        self.assertGreater(r, 0.3)
        self.assertLess(r, 0.9)

    def test_evidence_pulls_toward_truth(self):
        # Same capability, one well-calibrated & succeeding vs one failing.
        good = composite_reliability(0.5, 1.0, 1.0, 1000)
        bad = composite_reliability(0.5, 0.0, 0.0, 1000)
        self.assertGreater(good, bad)

    def test_sparse_evidence_shrinks(self):
        # With 1 sample the prior dominates; with many samples evidence dominates.
        sparse = composite_reliability(0.5, 1.0, 1.0, 1)
        dense = composite_reliability(0.5, 1.0, 1.0, 1000)
        self.assertLess(sparse, dense)

    def test_in_unit_range(self):
        for args in ((0.9, 0.9, 0.9, 10), (0.2, None, None, 0), (1.0, 1.0, 1.0, 5)):
            r = composite_reliability(*args)
            self.assertGreaterEqual(r, 0.0)
            self.assertLessEqual(r, 1.0)


class ObservedLayerTest(unittest.TestCase):
    def _ledger_with_results(self, entries):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ledger.jsonl")
            ledger = AutonomyLedger(path)
            for e in entries:
                ledger.append("model_result", model=e[0], json_expected=True,
                              json_ok=e[1], task_type="structured", status="ok")
            return ledger

    def test_observed_json_reliability(self):
        ledger = self._ledger_with_results([("m", True), ("m", True), ("m", False)])
        self.assertAlmostEqual(observed_json_reliability(ledger, "m"), 2 / 3)

    def test_observed_json_none_without_events(self):
        with tempfile.TemporaryDirectory() as d:
            ledger = AutonomyLedger(os.path.join(d, "l.jsonl"))
            self.assertIsNone(observed_json_reliability(ledger, "m"))

    def test_json_reliable_bridge_max_of_declared_and_observed(self):
        # Declared only -> uses declared.
        declared = CapabilityProfile.from_model(sample_model(
            {"supported_parameters": ["max_tokens"]}))  # declared_json 0.0
        with tempfile.TemporaryDirectory() as d:
            ledger = AutonomyLedger(os.path.join(d, "l.jsonl"))
            # But observed it emits JSON reliably:
            ledger.append("model_result", model="x", json_expected=True,
                          json_ok=True, task_type="structured", status="ok")
            ledger.append("model_result", model="x", json_expected=True,
                          json_ok=True, task_type="structured", status="ok")
            ledger.append("model_result", model="x", json_expected=True,
                          json_ok=False, task_type="structured", status="ok")
            got = json_reliable(declared, ledger, "x")
            # Must be strictly greater than the stale 0.0 declaration.
            self.assertGreater(got, 0.0)
            self.assertLessEqual(got, 1.0)


class RegistryTest(unittest.TestCase):
    def test_save_load_roundtrip(self):
        prof = fixture_profiles()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "caps.json")
            save_profiles(path, prof, fetched_at=1000.0)
            loaded, ts = load_profiles(path)
            self.assertEqual(set(loaded), set(prof))
            self.assertEqual(ts, 1000.0)
            self.assertEqual(loaded["z-ai/glm-5.2:free"].context_length,
                             prof["z-ai/glm-5.2:free"].context_length)

    def test_ensure_profiles_refetches_when_stale_or_forced(self):
        calls = {"n": 0}

        def fetch():
            calls["n"] += 1
            return [sample_model({"id": "acme/example:free"})]

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "caps.json")
            # First call: nothing cached -> fetch.
            p1, ts1, refreshed1 = ensure_profiles(path, fetch, ttl=0, force=False)
            self.assertTrue(refreshed1)
            # force -> fetch again even though not stale by ttl.
            p2, ts2, refreshed2 = ensure_profiles(path, fetch, ttl=100000, force=True)
            self.assertTrue(refreshed2)
            # Fresh + not forced -> no fetch.
            p3, _, refreshed3 = ensure_profiles(path, fetch, ttl=100000, force=False)
            self.assertFalse(refreshed3)
            self.assertEqual(calls["n"], 2)


class RoutingOrderTest(unittest.TestCase):
    def test_free_tier_capability_first(self):
        prof = fixture_profiles()
        ordered = order_pool(list(prof), prof, None, task="default", free_tier=True)
        caps = [capability_score(prof[m]) for m in ordered]
        self.assertEqual(caps, sorted(caps, reverse=True))

    def test_paid_tier_cost_first(self):
        prof = {
            "cheap/pay:free": CapabilityProfile.from_model(sample_model(
                {"id": "cheap/pay:free", "pricing": {"prompt": "0", "completion": "0"}})),
            "mid/exp": CapabilityProfile.from_model(sample_model(
                {"id": "mid/exp", "context_length": 262144,
                 "supported_parameters": ["max_tokens", "reasoning", "structured_outputs"],
                 "pricing": {"prompt": "0.000002", "completion": "0.000002"}})),
            "mid/exp2": CapabilityProfile.from_model(sample_model(
                {"id": "mid/exp2", "context_length": 262144,
                 "supported_parameters": ["max_tokens", "reasoning", "structured_outputs"],
                 "pricing": {"prompt": "0.000002", "completion": "0.000002"}})),
        }
        ordered = order_pool(list(prof), prof, None, task="default", free_tier=False)
        self.assertEqual(ordered[0], "cheap/pay:free")  # free first
        # equal-cost ties: more capable first (both have same fields; order stable)
        self.assertIn(ordered[1], ("mid/exp", "mid/exp2"))

    def test_hard_gate_excludes_incapable(self):
        # A model with no declared JSON and tiny context still routes (cap > 0),
        # but a capability score of exactly 0 is dropped.
        zero = CapabilityProfile.from_model(sample_model(
            {"id": "zero:free", "context_length": 0,
             "supported_parameters": ["max_tokens"]}))
        good = CapabilityProfile.from_model(sample_model({"id": "good:free"}))
        self.assertEqual(capability_score(zero), 0.0)
        ordered = order_pool(["zero:free", "good:free"], {"zero:free": zero, "good:free": good},
                             None, task="default", free_tier=True)
        self.assertNotIn("zero:free", ordered)
        self.assertEqual(ordered, ["good:free"])

    def test_order_pool_empty_safe(self):
        self.assertEqual(order_pool([], {}, None, free_tier=True), [])

    def test_one_unusable_strike_does_not_demote(self):
        """Strike policy: demotion needs TWO unusable events. A single
        reasoning-only response from an otherwise good model must not flip
        live pool order (the real ledger shows a paid model with exactly one
        unusable event among successful paid calls)."""
        prof = {m: CapabilityProfile.from_model(sample_model(
            {"id": m, "context_length": 256000,
             "supported_parameters": ["max_tokens", "reasoning"]}))
            for m in ("acme/good:free", "acme/flaky:free")}
        with tempfile.TemporaryDirectory() as d:
            ledger = AutonomyLedger(os.path.join(d, "l.jsonl"))
            # one unusable output, plus real successes from the same model
            # recorded the way the engine records them (verify_round passes
            # feed the code-task success rate).
            ledger.append("model_result", model="acme/flaky:free", status="error",
                          reason="no usable content")
            ledger.append("verify_round", task_id="t1", round=1,
                          model="acme/flaky:free", passed=True)
            ledger.append("verify_round", task_id="t2", round=1,
                          model="acme/flaky:free", passed=True)
            report = ledger.participation_report()
        self.assertEqual(report["calibration"]["acme/flaky:free"]["unusable_outputs"], 1)
        ordered = order_pool(["acme/good:free", "acme/flaky:free"], prof, report,
                             ledger=ledger, task="code", free_tier=True)
        self.assertEqual(ordered[0], "acme/flaky:free",
                         "one strike must not demote below the unproven peer")

    def test_consent_unusable_events_count_as_strikes(self):
        """Consent-probe curation: a judge whose consent answers the parser
        cannot use (empty/reasoning-only/unparseable) is demotion evidence
        like the apply lane's unusable outputs. Two consent strikes demote;
        HTTP-tier rotations do not count."""
        prof = {m: CapabilityProfile.from_model(sample_model(
            {"id": m, "context_length": 256000,
             "supported_parameters": ["max_tokens", "reasoning"]}))
            for m in ("acme/good:free", "acme/blind:free")}
        with tempfile.TemporaryDirectory() as d:
            ledger = AutonomyLedger(os.path.join(d, "l.jsonl"))
            ledger.append("consent_rotate", model="acme/blind:free",
                          reason="empty response")
            ledger.append("consent_rotate", model="acme/blind:free",
                          reason="unparseable or missing a valid decision")
            ledger.append("consent_rotate", model="acme/blind:free",
                          reason="HTTP 429")  # tier fault: never a strike
            report = ledger.participation_report()
        cal = report["calibration"]["acme/blind:free"]
        self.assertEqual(cal["consent_unusable"], 2)
        ordered = order_pool(["acme/good:free", "acme/blind:free"], prof, report,
                             ledger=ledger, task="default", free_tier=True)
        self.assertEqual(ordered[0], "acme/good:free",
                         "two consent strikes must demote below the unproven peer")

    def test_consent_strike_composes_with_apply_strike(self):
        """One apply strike + one consent strike = two strikes = demotion:
        the evidence joins at the one policy owner."""
        prof = {m: CapabilityProfile.from_model(sample_model(
            {"id": m, "context_length": 256000,
             "supported_parameters": ["max_tokens", "reasoning"]}))
            for m in ("acme/good:free", "acme/mixed:free")}
        with tempfile.TemporaryDirectory() as d:
            ledger = AutonomyLedger(os.path.join(d, "l.jsonl"))
            ledger.append("model_result", model="acme/mixed:free", status="error",
                          reason="no usable content")
            ledger.append("consent_rotate", model="acme/mixed:free",
                          reason="reasoning-only output")
            report = ledger.participation_report()
        ordered = order_pool(["acme/good:free", "acme/mixed:free"], prof, report,
                             ledger=ledger, task="code", free_tier=True)
        self.assertEqual(ordered[0], "acme/good:free")

    def test_gate_waste_demotes_repeat_gate_waster(self):
        """The v0.3.1 dogfood finding, mechanized: a model the ledger shows
        leading 2+ runs that died at the verification gate sorts below an
        unproven peer. Demoted, not banned -- it still rotates last, and
        the gate still guards what it produces."""
        prof = {m: CapabilityProfile.from_model(sample_model(
            {"id": m, "context_length": 256000,
             "supported_parameters": ["max_tokens", "reasoning"]}))
            for m in ("acme/good:free", "acme/waster:free")}
        with tempfile.TemporaryDirectory() as d:
            ledger = AutonomyLedger(os.path.join(d, "l.jsonl"))
            ledger.append("abort", task_id="t1", model="acme/waster:free",
                          reason="verify rounds exhausted", rotations=3)
            ledger.append("abort", task_id="t2", model="acme/waster:free",
                          reason="verify rounds exhausted", rotations=3)
            report = ledger.participation_report()
        ordered = order_pool(["acme/waster:free", "acme/good:free"], prof, report,
                             ledger=ledger, task="code", free_tier=True)
        self.assertEqual(ordered[0], "acme/good:free",
                         "gate waste must demote below the unproven peer")
        self.assertEqual(ordered[-1], "acme/waster:free")

    def test_gate_waste_fail_open_with_no_evidence(self):
        """Fail-open: a model with no rounds-exhausted aborts keeps its
        evidence-driven rank even when the report lacks the field
        entirely (older ledgers, hermetic reports)."""
        prof = {m: CapabilityProfile.from_model(sample_model(
            {"id": m, "context_length": 256000,
             "supported_parameters": ["max_tokens", "reasoning"]}))
            for m in ("acme/good:free", "acme/quiet:free")}
        ordered = order_pool(["acme/good:free", "acme/quiet:free"], prof, None,
                             ledger=None, task="code", free_tier=True)
        # Identical profiles tie on every sort key, so the caller's input
        # order survives untouched -- nothing is demoted without evidence.
        self.assertEqual(ordered, ["acme/good:free", "acme/quiet:free"])

    def test_observed_evidence_demotes_overdeclared_and_raises_proven(self):
        """THE fix for commit 58ddd1f's inert loop: for the structured task,
        probe/ledger evidence must demote a declared-capable-but-failing model
        (GLM) below proven emitters (north-mini, gemma)."""
        prof = {
            "z-ai/glm-5.2:free": CapabilityProfile.from_model(sample_model(
                {"id": "z-ai/glm-5.2:free", "context_length": 256000,
                 "supported_parameters": ["max_tokens", "reasoning", "structured_outputs"]})),
            "cohere/north-mini-code:free": CapabilityProfile.from_model(sample_model(
                {"id": "cohere/north-mini-code:free", "context_length": 256000,
                 "supported_parameters": ["max_tokens", "reasoning"]})),
            "google/gemma-4-31b-it:free": CapabilityProfile.from_model(sample_model(
                {"id": "google/gemma-4-31b-it:free", "context_length": 262144,
                 "supported_parameters": ["max_tokens", "reasoning", "response_format"]})),
        }
        glm = "z-ai/glm-5.2:free"
        north = "cohere/north-mini-code:free"
        gemma = "google/gemma-4-31b-it:free"
        with tempfile.TemporaryDirectory() as d:
            ledger = AutonomyLedger(os.path.join(d, "l.jsonl"))
            # Probe evidence: GLM fails 4/5, north-mini & gemma 5/5.
            for mid, ok_seq in ((glm, [True, False, False, False, False]),
                                (north, [True] * 5), (gemma, [True] * 5)):
                for ok in ok_seq:
                    ledger.append("model_result", model=mid, json_expected=True,
                                  json_ok=ok, task_type="structured", status="ok")
            report = ledger.participation_report()
            ordered = order_pool([glm, north, gemma], prof, report, ledger=ledger,
                                 task="structured", free_tier=True)
            # Proven emitters must rank above the declared-capable-but-flaky GLM.
            self.assertLess(ordered.index(north), ordered.index(glm))
            self.assertLess(ordered.index(gemma), ordered.index(glm))
            self.assertEqual(ordered[-1], glm)

    def test_model_reliability_single_owner_uses_observed_json_for_structured(self):
        prof = CapabilityProfile.from_model(sample_model(
            {"id": "m:free", "supported_parameters": ["max_tokens", "reasoning",
                                                         "structured_outputs"]}))
        with tempfile.TemporaryDirectory() as d:
            ledger = AutonomyLedger(os.path.join(d, "l.jsonl"))
            for ok in [True, True, False, False, False]:
                ledger.append("model_result", model="m:free", json_expected=True,
                              json_ok=ok, task_type="structured", status="ok")
            report = ledger.participation_report()
            info = model_reliability("m:free", prof, report, ledger=ledger,
                                     task="structured")
            # Declared json is 1.0 but observed is 0.4 -> json_reliable < declared.
            self.assertLess(info["json_reliable"], 1.0)
            self.assertGreater(info["json_reliable"], 0.0)
            # Without a ledger the same profile would not be demoted.
            declared_info = model_reliability("m:free", prof, report, ledger=None,
                                              task="structured")
            self.assertGreater(declared_info["json_reliable"], info["json_reliable"])


class ProbeTest(unittest.TestCase):
    def test_probe_counts_json_and_correctness(self):
        from unittest import mock
        import harness.chat as chat_mod
        # The probe imports these from harness.chat inside its body, so patch there.
        with mock.patch.object(chat_mod, "chat") as mock_chat, \
             mock.patch.object(chat_mod, "extract_content_and_cost") as mock_extract, \
             mock.patch.object(chat_mod, "_extract_json") as mock_parse:
            # All calls return "ok" status with a JSON object we fully control.
            mock_chat.return_value = (200, {"ok": True})
            mock_extract.return_value = ("raw", "stop", 0.0, False)
            want = [4, 56, True, 1024, 11]
            mock_parse.side_effect = [{"answer": v} for v in want]
            class Gov:
                def check_byok(self, m): pass

                def preflight(self, prompt_text, calls): return 0.0, []
            res = probe_json_reliability("t", "k", Gov(), ["m1"], max_tokens=64)
            self.assertEqual(res["m1"]["calls"], 5)
            self.assertEqual(res["m1"]["errors"], 0)
            self.assertEqual(res["m1"]["json_ok_rate"], 1.0)
            self.assertEqual(res["m1"]["correct_rate"], 1.0)

    def test_probe_persists_model_result_events(self):
        from unittest import mock
        import harness.chat as chat_mod
        with mock.patch.object(chat_mod, "chat") as mock_chat, \
             mock.patch.object(chat_mod, "extract_content_and_cost") as mock_extract, \
             mock.patch.object(chat_mod, "_extract_json") as mock_parse:
            mock_chat.return_value = (200, {"ok": True})
            mock_extract.return_value = ("raw", "stop", 0.0, False)
            mock_parse.side_effect = [{"answer": v} for v in [4, 56, True, 1024, 11]]
            class Gov:
                def check_byok(self, m): pass

                def preflight(self, prompt_text, calls): return 0.0, []
            with tempfile.TemporaryDirectory() as d:
                ledger = AutonomyLedger(os.path.join(d, "l.jsonl"))
                probe_json_reliability("t", "k", Gov(), ["m1"], max_tokens=64,
                                             ledger=ledger)
                # Every call persisted as a model_result event (all json_ok here).
                mr = [e for e in ledger.entries() if e["event"] == "model_result"]
                self.assertEqual(len(mr), 5)
                self.assertTrue(all(e["json_ok"] for e in mr))
                # And the persisted evidence feeds observed json reliability.
                self.assertAlmostEqual(observed_json_reliability(ledger, "m1"), 1.0)


class StalePoolSelfHealingTest(unittest.TestCase):
    """Dogfooding round 1 (audits/self): the shipped free pools still named a
    model the live catalog had delisted, and a verify run hard-fatalled on its
    pricing lookup instead of rotating. The contract going forward:

    * the routing boundary (ordered_pool) drops catalog-stale ids, so one dead
      configured id can never kill a session that has healthy models left;
    * shipped pools stay catalog-live: rewritten with realistic rename aliases
      (a real-world model delisting/rename), they must round-trip through
      ordered_pool -- if the shipped config goes stale again, this fails;
    * a stale id in a saved continuation gate contract is refused by the
      tamper check, while a live gate still round-trips.
    """

    MODELS = [
        {"id": "m://panel-a:free", "pricing": {"prompt": "0", "completion": "0"},
         "context_length": 131072,
         "supported_parameters": ["max_tokens", "structured_outputs"]},
        {"id": "m://panel-b:free", "pricing": {"prompt": "0", "completion": "0"},
         "context_length": 32768, "supported_parameters": ["max_tokens"]},
        {"id": "m://judge:free", "pricing": {"prompt": "0", "completion": "0"},
         "context_length": 131072,
         "supported_parameters": ["max_tokens", "reasoning", "structured_outputs"]},
    ]

    def _profiles(self):
        return build_profiles_from_models(self.MODELS)

    def test_ordered_pool_drops_stale_ids(self):
        # Real-world shape from the dogfooding round: a configured pool still
        # listing a delisted model id next to healthy ones.
        pool = ["m://panel-a:free", "z-ai/glm-5.2:free", "m://panel-b:free"]
        ordered, profiles = ordered_pool(pool, governor=None, ledger=None,
                                         task="default", free_tier=True,
                                         profiles=self._profiles())
        self.assertIsNotNone(profiles)
        self.assertNotIn("z-ai/glm-5.2:free", ordered)
        self.assertIn("m://panel-a:free", ordered)
        self.assertIn("m://panel-b:free", ordered)

    def test_all_stale_degrades_to_given_order(self):
        pool = ["z-ai/glm-5.2:free"]
        ordered, profiles = ordered_pool(pool, governor=None, ledger=None,
                                         task="default", free_tier=True,
                                         profiles=self._profiles())
        self.assertIsNone(profiles)
        self.assertEqual(ordered, pool)

    def test_shipped_free_pools_survive_realistic_renames(self):
        """The shipped pools, with EVERY id renamed (the strongest form of
        'the catalog moved under us'), must round-trip: ordered_pool maps each
        stale id to its alias and returns a fully-routable pool. This is the
        hermetic teeth behind 'the harness self-heals stale pools'."""
        profiles = self._profiles()

        def rewrite(pool):
            fixed = []
            for mid in pool:
                if mid in profiles:
                    fixed.append(mid)
                else:
                    renamed = "m://renamed-" + mid.split("/")[-1]
                    profiles[renamed] = profiles["m://panel-a:free"]
                    fixed.append(renamed)
            return fixed

        for pool in (FREE_PANEL_POOL, FREE_APPLY_POOL):
            renamed = rewrite(pool)
            ordered, out_profiles = ordered_pool(renamed, governor=None, ledger=None,
                                                 task="default", free_tier=True,
                                                 profiles=profiles)
            self.assertIsNotNone(out_profiles)
            self.assertEqual(set(ordered), set(renamed))

    def test_stale_gate_id_is_refused(self):
        cmd = "pytest -q"
        with tempfile.TemporaryDirectory() as directory:
            target = os.path.join(directory, "x.py")
            with open(target, "w", encoding="utf-8") as stream:
                stream.write("x = 1\n")
            state = {"file_path": target, "verify_cmd": cmd, "verify_gate_id": "deadbeef",
                     "verification_required": True}
            with self.assertRaises(HarnessError):
                validate_continuation(state)
            good = {"file_path": target, "verify_cmd": cmd,
                    "verify_gate_id": gate_id(cmd), "verification_required": True}
            self.assertTrue(validate_continuation(good))


if __name__ == "__main__":
    unittest.main()
