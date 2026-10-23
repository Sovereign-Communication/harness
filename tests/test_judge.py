"""Judge selection and lane demotion policy (regression-pinned model ids)."""
import unittest

from harness._http import HttpTransport
from harness.config import (DEFAULT_JUDGE_PAID, FREE_JUDGE,
                            resolve_api_key)
from harness.spend import SpendGovernor


class ShippedModelFreshnessTests(unittest.TestCase):
    """Shipped default lanes: shape/fixture pins here cover what is hermetic;
    LIVE catalog contact is machine-checked by `capabilities --check-shipped`
    and auto-run below when a key is present (the stale-id defect class
    recurred twice before that check existed)."""

    def test_no_shipped_id_is_byok_denylisted(self):
        """A denylisted shipped id would hard-fatal at check_byok on every
        BYOK run -- the check applies to BOTH tier policies."""
        from harness.config import BYOK_DENYLIST_PREFIXES, shipped_model_ids
        for mid in shipped_model_ids():
            for prefix in BYOK_DENYLIST_PREFIXES:
                self.assertFalse(mid.startswith(prefix),
                                 f"{mid} matches denylist prefix {prefix}")

    def test_shipped_enumeration_covers_paid_lanes(self):
        """The fixture this test used to pin against froze in time; the live
        check replaced it. This pin keeps the paid lanes inside the
        enumeration so the live check cannot silently skip them."""
        from harness.config import (DEFAULT_APPLY_MODEL_PAID, DEFAULT_JUDGE_PAID,
                                    DEFAULT_PANEL_PAID, SPECIALIST_POOL_PAID,
                                    shipped_model_ids)
        shipped = (set(DEFAULT_PANEL_PAID) | {DEFAULT_JUDGE_PAID}
                   | {DEFAULT_APPLY_MODEL_PAID} | set(SPECIALIST_POOL_PAID))
        self.assertTrue(shipped <= shipped_model_ids())

    @unittest.skipUnless(resolve_api_key(), "live catalog check needs an API key")
    def test_shipped_ids_resolve_in_live_catalog(self):
        """The real freshness gate, auto-run wherever a key exists (this
        includes the dev machine and any keyed CI): a shipped id missing from
        today's /models fails here exactly as `--check-shipped` would."""
        from harness.config import shipped_model_ids
        gov = SpendGovernor(HttpTransport(), resolve_api_key())
        catalog = {m["id"] for m in gov.fetch_models(refresh=True)}
        stale = sorted(shipped_model_ids() - catalog)
        self.assertEqual(stale, [],
                         f"stale shipped ids (run `capabilities --check-shipped`): {stale}")


class JudgeRecurationTests(unittest.TestCase):
    def test_default_judge_is_the_proven_emitter(self):
        """north-mini burned its budget on hidden reasoning in live runs; the
        default judge must be the model with the best JSON track record."""
        self.assertEqual(FREE_JUDGE, "google/gemma-4-31b-it:free")
        self.assertNotEqual(DEFAULT_JUDGE_PAID, "cohere/north-mini-code")

    def test_north_mini_demoted_in_free_lanes(self):
        from harness.config import FREE_PANEL_POOL, FREE_APPLY_POOL
        for pool in (FREE_PANEL_POOL, FREE_APPLY_POOL):
            self.assertNotEqual(pool[0], "cohere/north-mini-code:free",
                                "a reasoning-fragile model must not lead a lane")
            self.assertIn("cohere/north-mini-code:free", pool,
                          "demoted, not removed")
            self.assertEqual(pool[-1], "openrouter/free")


if __name__ == "__main__":
    unittest.main()
