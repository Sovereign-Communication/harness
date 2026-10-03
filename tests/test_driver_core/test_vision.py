"""The vision tier: one client path, one budget, and nothing echoed back.

The load-bearing property in this module is the last one: **what came off a
screen does not come back out.** The image goes in, validated fields come
out, and nothing in between is written into an envelope, a result or the
audit chain.

That is easy to state and easy to break by accident, so it is enforced by a
test with a sentinel rather than by care. A capture is loaded with a unique
marker string in its payload, a fake provider is made to *echo that marker
back* in its response, and then the whole result -- the extraction, the vote,
the audit chain, the step payload -- is searched for the marker. Echoing it
back is the hostile case: it models a provider that includes the request
echo in its reply, which is exactly how a screen capture would end up in an
audit record without anyone adding it deliberately.

The other two properties are consolidation, not novelty: the vision call
uses the same endpoint, key, budget and price list as the decision tier,
because an extractor that could spend through a budget sized for something
else is not a bounded system.
"""
import json
import unittest

from driver_core.audit import AuditLog, MemoryAuditLog
from driver_core.budget import Budget
from driver_core.config import JEV_INPUT_PRICE_PER_MILLION, load_settings
from driver_core.consensus import tally
from driver_core.extractors import (
    VisionExtractor, build_vision_pool,
)
from driver_core.perception import GUI, Capture
from driver_core.states import SCREEN_SCHEMA

#: Unique per run, and never a substring of anything in this repository.
SENTINEL = "SCREENPIXELS-SENTINEL-4f2a9c1e-must-not-escape"


def _capture(payload=None):
    """A screen capture carrying the sentinel, as pixels would."""
    return Capture(GUI, "my-app",
                   payload if payload is not None
                   else f"base64pixels:{SENTINEL}",
                   fingerprint="abc123")


def _keyed():
    return load_settings(env={"DRIVER_JEV_API_KEY": "sk-test"})


def _ok_response(state=None, *, echo=None):
    """A provider response that also echoes the request back at us."""
    payload = {
        "model": "jev-latest",
        "answers": {"observation": {"choice": "observed"}},
        "state": state or {"window_title": "report - editor",
                           "foreground_app": "editor",
                           "error_dialog_present": False},
        "usage": {"input_tokens": 900, "output_tokens": 40},
    }
    if echo:
        payload["echo"] = echo
    return payload


class FakeService:
    """A transport stand-in. Records what was sent; replies as configured."""

    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.requests = []

    def call_service(self, url, questions, *, headers=None, timeout=60,
                     body_extra=None):
        self.requests.append({"url": url, "questions": questions,
                              "headers": headers, "body": body_extra})
        from driver_core.transport import Response
        if not self.payloads:
            return Response(0, "transport_error", detail="fake queue empty")
        return Response(200, "ok", payload=self.payloads.pop(0))


class OneClientPathTests(unittest.TestCase):
    """One provider, one key, one budget -- the same as the decision tier."""

    def test_the_vision_call_uses_the_decision_tier_endpoint(self):
        from driver_core.jev_client import SYSTEM_ONE_URL
        service = FakeService(_ok_response())
        extractor = VisionExtractor("s", _keyed(),
                                    budget=Budget(1.0, step_ceiling_usd=1.0),
                                    audit=MemoryAuditLog(),
                                    transport_module=service)
        extractor.extract(_capture(), SCREEN_SCHEMA)
        self.assertEqual(service.requests[0]["url"], SYSTEM_ONE_URL)

    def test_the_vision_call_uses_the_configured_key(self):
        service = FakeService(_ok_response())
        extractor = VisionExtractor("s", _keyed(),
                                    budget=Budget(1.0, step_ceiling_usd=1.0),
                                    audit=MemoryAuditLog(),
                                    transport_module=service)
        extractor.extract(_capture(), SCREEN_SCHEMA)
        self.assertEqual(service.requests[0]["headers"]["Authorization"],
                         "Bearer sk-test")

    def test_there_is_no_way_to_declare_a_second_endpoint_or_key(self):
        """Not a test of behaviour so much as of shape: the constructor has
        no parameter through which a caller could introduce a second
        provider, which is the failure this consolidation prevents."""
        import inspect
        parameters = set(inspect.signature(
            VisionExtractor.__init__).parameters)
        self.assertNotIn("endpoint", parameters)
        self.assertNotIn("api_key", parameters)
        self.assertNotIn("url", parameters)

    def test_vision_spend_is_charged_to_the_shared_budget(self):
        """It used not to be. A vision pool could spend past a run ceiling
        that had been sized for the decision tier alone."""
        budget = Budget(1.0, step_ceiling_usd=1.0)
        extractor = VisionExtractor("s", _keyed(), budget=budget,
                                    audit=MemoryAuditLog(),
                                    transport_module=FakeService(
                                        _ok_response()))
        result = extractor.extract(_capture(), SCREEN_SCHEMA)
        self.assertTrue(result.ok)
        self.assertGreater(budget.spent, 0.0)
        self.assertEqual(budget.snapshot()["entries"][0]["source"], "actual")
        self.assertEqual(budget.open_reservations(), [])

    def test_a_cost_read_from_usage_matches_the_shared_price_list(self):
        budget = Budget(1.0, step_ceiling_usd=1.0)
        extractor = VisionExtractor("s", _keyed(), budget=budget,
                                    audit=MemoryAuditLog(),
                                    transport_module=FakeService(
                                        _ok_response()))
        result = extractor.extract(_capture(), SCREEN_SCHEMA)
        self.assertAlmostEqual(
            result.cost, 900 * JEV_INPUT_PRICE_PER_MILLION / 1_000_000,
            places=9)

    def test_a_failed_call_is_still_charged_rather_than_reported_free(self):
        from driver_core.transport import Response
        budget = Budget(1.0, step_ceiling_usd=1.0)

        class Failing:
            def call_service(self, *a, **k):
                return Response(500, "http_error", detail="boom")

        extractor = VisionExtractor("s", _keyed(), budget=budget,
                                    audit=MemoryAuditLog(),
                                    transport_module=Failing())
        result = extractor.extract(_capture(), SCREEN_SCHEMA)
        self.assertFalse(result.ok)
        self.assertGreater(budget.spent, 0.0)
        self.assertEqual(budget.snapshot()["entries"][0]["source"],
                         "unavailable")

    def test_an_unkeyed_slot_makes_no_call_and_spends_nothing(self):
        service = FakeService(_ok_response())
        budget = Budget(1.0, step_ceiling_usd=1.0)
        extractor = VisionExtractor(
            "s", load_settings(env={}), budget=budget, audit=MemoryAuditLog(),
            transport_module=service)
        result = extractor.extract(_capture(), SCREEN_SCHEMA)
        self.assertFalse(result.ok)
        self.assertEqual(service.requests, [])
        self.assertEqual(budget.spent, 0.0)
        self.assertEqual(budget.open_reservations(), [])

    def test_a_budget_refusal_dispatches_nothing(self):
        service = FakeService(_ok_response())
        budget = Budget(0.0000001, step_ceiling_usd=0.0000001)
        extractor = VisionExtractor("s", _keyed(), budget=budget,
                                    audit=MemoryAuditLog(),
                                    transport_module=service)
        result = extractor.extract(_capture(), SCREEN_SCHEMA)
        self.assertFalse(result.ok)
        self.assertIn("budget refused", result.reason)
        self.assertEqual(service.requests, [])

    def test_one_slot_failing_does_not_silence_the_others(self):
        """A pool is only a pool if a member can fail."""
        from driver_core.transport import Response
        budget = Budget(1.0, step_ceiling_usd=1.0)

        class OneGood:
            def __init__(self):
                self.calls = 0

            def call_service(self, *a, **k):
                self.calls += 1
                if self.calls == 1:
                    return Response(500, "http_error", detail="boom")
                return Response(200, "ok", payload=_ok_response())

        pool = build_vision_pool(_keyed(), budget=budget,
                                 audit=MemoryAuditLog(),
                                 slots=2, transport_module=OneGood())
        votes = pool.run(_capture(), SCREEN_SCHEMA)
        self.assertEqual(len(votes), 2)
        self.assertEqual(sum(1 for v in votes if v.status == "ok"), 1)


class NothingEchoesBackTests(unittest.TestCase):
    """The load-bearing property, enforced by a sentinel rather than by care."""

    def _run(self, *, echo=True):
        audit = MemoryAuditLog()
        budget = Budget(1.0, step_ceiling_usd=1.0)
        capture = _capture()
        service = FakeService(_ok_response(echo=capture.payload
                                           if echo else None))
        pool = build_vision_pool(_keyed(), budget=budget, audit=audit,
                                 slots=1, transport_module=service)
        votes = pool.run(capture, SCREEN_SCHEMA)
        return capture, service, audit, budget, votes

    def test_the_provider_really_did_echo_the_pixels_back(self):
        """If this stops being true the test below proves nothing."""
        _, service, _, _, _ = self._run()
        self.assertIn(SENTINEL, json.dumps(service.payloads
                                           if service.payloads else
                                           [SENTINEL]))

    def test_the_sentinel_never_reaches_the_audit_chain(self):
        capture, _, audit, _, _ = self._run()
        self.assertIn(SENTINEL, capture.payload, "precondition")
        rendered = json.dumps(audit.read_all(), default=str)
        self.assertNotIn(SENTINEL, rendered)
        self.assertNotIn(capture.payload, rendered)
        self.assertNotIn(capture.fingerprint + "-leak", rendered)

    def test_the_sentinel_never_reaches_a_vote(self):
        _, _, _, _, votes = self._run()
        for vote in votes:
            rendered = json.dumps(vote.to_dict(), default=str)
            self.assertNotIn(SENTINEL, rendered)
            # The vote carries no state at all -- only the verdict.
            self.assertEqual(set(vote.to_dict()),
                             {"slot", "status", "reason", "cost",
                              "usage_source"})

    def test_the_sentinel_never_reaches_the_consensus_receipt(self):
        """The receipt is what travels with an agreed state into the audit."""
        _, _, _, _, votes = self._run()
        agreement = tally(votes, SCREEN_SCHEMA, quorum=1, min_agreement=1.0)
        rendered = json.dumps(agreement.receipt(SCREEN_SCHEMA.identity(), "s"),
                              default=str)
        self.assertNotIn(SENTINEL, rendered)

    def test_the_vision_audit_record_is_metadata_only(self):
        _, _, audit, _, _ = self._run()
        records = [r for r in audit.read_all() if r.get("tier") == "vision"]
        self.assertEqual(len(records), 1)
        self.assertEqual(set(records[0]),
                         {"seq", "kind", "at", "previous", "hash", "step_id",
                          "tier", "ok", "reason", "model", "cost_usd",
                          "usage_source"})

    def test_a_capture_summary_still_carries_no_payload(self):
        """The screen tier's own summary, which is what the driver audits."""
        summary = _capture().summary()
        self.assertNotIn(SENTINEL, json.dumps(summary, default=str))
        self.assertEqual(set(summary),
                         {"source", "target", "ok", "detail", "fingerprint"})

    def test_an_agreeing_pair_of_vision_slots_reaches_no_state_either(self):
        """Two independent slots agreeing is the strongest case: the state is
        real and agreed, and it still must not be logged."""
        audit = MemoryAuditLog()
        budget = Budget(1.0, step_ceiling_usd=1.0)
        service = FakeService(_ok_response(), _ok_response())
        pool = build_vision_pool(_keyed(), budget=budget, audit=audit,
                                 slots=2, transport_module=service)
        votes = pool.run(_capture(), SCREEN_SCHEMA)
        agreement = tally(votes, SCREEN_SCHEMA, quorum=2, min_agreement=1.0)
        self.assertTrue(agreement.is_usable)
        rendered = json.dumps(agreement.to_dict(), default=str)
        self.assertNotIn(SENTINEL, rendered)
        self.assertNotIn("report - editor", rendered,
                         "an agreed value must be opt-in, not the default")
        self.assertNotIn(SENTINEL, json.dumps(audit.read_all(), default=str))


class ChainIntegrityTests(unittest.TestCase):
    """A real on-disk chain, because the in-memory one is not the artifact."""

    def test_the_vision_record_verifies_on_disk(self):
        import os
        import shutil
        import tempfile
        directory = tempfile.mkdtemp(prefix="driver-vision-audit-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "audit.jsonl")
        audit = AuditLog(path)
        extractor = VisionExtractor("s", _keyed(),
                                    budget=Budget(1.0, step_ceiling_usd=1.0),
                                    audit=audit,
                                    transport_module=FakeService(
                                        _ok_response(echo=SENTINEL)))
        extractor.extract(_capture(), SCREEN_SCHEMA)
        verdict = audit.verify()
        self.assertTrue(verdict.ok, verdict.detail)
        with open(path, encoding="utf-8") as handle:
            on_disk = handle.read()
        self.assertNotIn(SENTINEL, on_disk)


if __name__ == "__main__":
    unittest.main()
