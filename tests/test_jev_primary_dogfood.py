"""#453 S5 dogfood: one full service-level verify run under
jev_primary_mode, with the dual-envelope meter read.

Exercises the real composition — run_verify -> policy_for -> JevPolicy ->
JevEvaluator -> panel_judge — over a FakeTransport that serves both the
panel's chat calls and the TypeSafe Jev calls (routed by URL, answered
with the suite's hermetic jev_resp builder). This is the service-level
seam S3a-2 wired; the panel-level route naming is pinned separately in
tests/test_panel_jev_primary.py.

Meter read asserted here (the S5 record filed on #453 carries the same
figures from this run shape):
- Jev envelope: the claim_support structural's cost / token counts,
  is_fallback False, model named;
- LLM envelope: result cost_by_model plus the governor snapshot's
  dual-envelope fields (working spent vs terminal reserve).
"""
import os
import tempfile
import unittest
from unittest import mock

from tests._fake import FakeTransport, jev_resp, m, comp, P1, P2, JUDGE


class DogfoodTransport(FakeTransport):
    """FakeTransport with Jev calls routed to the hermetic builder
    regardless of the chat-post queue state."""

    def post(self, url, api_key, payload, timeout=45):
        if "typesafe.ai" in url or "systemone" in url:
            self.calls.append(("POST", url, payload))
            qs = payload.get("questions") if isinstance(payload, dict) else None
            return 200, jev_resp(questions=qs)
        return super().post(url, api_key, payload, timeout=timeout)


def _settings(tmp):
    from harness import config
    with mock.patch.object(config, "CONFIG_DIR", tmp), \
         mock.patch.object(config, "resolve_api_key", return_value=None), \
         mock.patch.object(config, "resolve_fireworks_key",
                           return_value=None), \
         mock.patch.object(config, "resolve_jev_key",
                           return_value="dogfood-jev-key"):
        return config.load_settings({"jev_primary_mode": True})


class JevPrimaryDogfoodTests(unittest.TestCase):
    def test_verify_run_routes_jev_primary_and_meters_both_envelopes(self):
        import json
        from harness import service
        from harness.ledger import AutonomyLedger
        from harness.spend import SpendGovernor

        with tempfile.TemporaryDirectory() as tmp:
            source_path = os.path.join(tmp, "source.txt")
            with open(source_path, "w", encoding="utf-8") as f:
                f.write("the gateway retries three times before failing\n")
            claims_path = os.path.join(tmp, "claims.json")
            with open(claims_path, "w", encoding="utf-8") as f:
                json.dump([{"id": "c1",
                            "text": "the gateway retries three times",
                            "kind": "defect", "source_refs": [1]}], f)
            prepared = service.prepare_verify(
                claims_file=claims_path, source_file=source_path)
            self.assertEqual(prepared["claim_texts"],
                             [{"id": "c1",
                               "text": "the gateway retries three times"}])

            fake = DogfoodTransport(
                models=[m(P1), m(P2), m(JUDGE),
                        m("google/gemma-4-31b-it:free")],
                posts=[comp(json.dumps({"c1": {"real": True,
                                                "confidence": 0.9}})),
                       comp(json.dumps({"c1": {"real": True,
                                                "confidence": 0.8}})),
                       comp("verdict: agree"),
                       comp(json.dumps({"c1": {"real": True,
                                                "confidence": 0.9}}))])
            gov = SpendGovernor(
                fake, "sk-test",
                byok_prefixes_path=os.path.join(tmp, "byok.json"),
                terminal_reserve=0.005)
            ledger = AutonomyLedger(os.path.join(tmp, "ledger.jsonl"))
            settings = _settings(tmp)
            self.assertTrue(settings.jev_primary_mode)

            result = service.run_verify(
                settings, prompt=prepared["prompt"], panel=[P1, P2],
                judge=JUDGE, transport=fake, api_key="sk-test", governor=gov,
                ledger=ledger, converge=prepared["converge"],
                reassurance_claims=prepared["reassurance_claims"],
                claim_texts=prepared["claim_texts"])

            # The Jev route actually served at service level.
            self.assertEqual(result["judgment_route"], "jev_primary")
            self.assertNotIn("judgment_route_reason", result)

            # Jev envelope: a real (hermetic) Jev evaluation, not a fallback.
            support = result["claim_support"]
            self.assertFalse(support["is_fallback"])
            self.assertTrue(support["model"])
            self.assertGreaterEqual(support["cost"], 0.0)
            self.assertGreater(support["input_tokens"], 0)

            # LLM envelope: per-model costs on the result, and the
            # governor snapshot's dual-envelope fields (working remaining
            # net of the terminal reserve).
            self.assertTrue(result["cost_by_model"])
            snap = gov.snapshot()
            self.assertAlmostEqual(snap["terminal_reserve"], 0.005)
            self.assertIn("working_remaining", snap)
            self.assertAlmostEqual(
                snap["working_remaining"],
                snap["ceiling"] - snap["spent"] - snap["terminal_reserve"],
                places=9)


if __name__ == "__main__":
    unittest.main()
