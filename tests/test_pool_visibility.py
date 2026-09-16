"""Pool-policy visibility (2026-09-13 handoff section 4): learned-BYOK
filtering and strike/demotion gating must be VISIBLE to the operator --
stderr notes plus `pool_filtered` events -- never silent shrinkage of a
dispatched pool."""
import tempfile
import unittest

from harness import events as _events


class _Sink:
    def __init__(self):
        self.events = []

    def __call__(self, event):
        self.events.append(event)


class ByokFilterVisibilityTests(unittest.TestCase):
    def test_learned_byok_removal_is_announced(self):
        from tests._fake import FakeTransport, comp, m
        from harness.spend import SpendGovernor
        from harness.panel import panel_judge
        with tempfile.TemporaryDirectory() as td:
            path = td + "/byok.json"
            fake = FakeTransport(models=[m("x/a"), m("y/b"), m("x/j")],
                                 posts=[comp("vote"),
                                        comp('{"verdict":"ok",'
                                             '"agreement":"high",'
                                             '"confidence":0.9,'
                                             '"defer":false}')])
            gov = SpendGovernor(fake, "sk-test", byok_prefixes_path=path)
            # The operator's account learned that x/ routes via paid BYOK
            # (record_byok learns the whole org prefix).
            gov.record_byok("x/a")
            sink = _Sink()
            _events.add_sink(sink)
            try:
                result = panel_judge(transport=fake, api_key="k", governor=gov,
                                     prompt="Q?", panel=["x/a", "y/b"],
                                     judge="x/j")
            finally:
                _events.remove_sink(sink)
            reasons = {e.get("reason") for e in sink.events
                       if e.get("type") == "pool_filtered"}
            self.assertIn("learned_byok", reasons)
            filt = [e for e in sink.events
                    if e.get("type") == "pool_filtered"
                    and e.get("reason") == "learned_byok"][0]
            self.assertIn("x/a", filt["models"])
            # The vote still came from a non-blocked seat.
            self.assertTrue(result["panel_results"])


class DemotionAnnotationTests(unittest.TestCase):
    def test_order_pool_demotes_below_unproven(self):
        from harness.capability import CapabilityProfile, order_pool
        profiles = {"a": CapabilityProfile("a", context_length=100000),
                    "b": CapabilityProfile("b", context_length=100000)}
        report = {"calibration": {
            "a": {"unusable_outputs": 2, "samples": 5,
                  "structured_success_rate": 0.0,
                  "structured_samples": 5},
            "b": {},
        }}
        ordered = order_pool(["a", "b"], profiles, report, task="structured",
                             free_tier=True)
        self.assertEqual(ordered[0], "b",
                         "a twice-struck model sorts below its unproven peer")
        self.assertIn("a", ordered)


if __name__ == "__main__":
    unittest.main()
