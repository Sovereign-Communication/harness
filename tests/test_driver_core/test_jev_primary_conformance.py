"""Conformance seed for #453 S3c/S4: driver_core judgment surfaces.

Finding (code reads, 2026-11-06): driver_core has NO LLM-backed
extractor. Its pools are deterministic StructuredExtractors (no model,
no cost) and Jev VisionExtractors — the judgment surfaces are already
Jev-served or deterministic, so S3c needs no re-pointing. These tests
pin that fact: a future LLM-backed extractor entering a configured pool
fails here, under the mode or not.
"""
import unittest

from driver_core.config import Settings
from driver_core.extractors.structured import StructuredExtractor
from driver_core.extractors.vision import VisionExtractor
from driver_core.wiring import configured_pools


def _pools(**overrides):
    settings = Settings().with_overrides(**overrides)
    return configured_pools(settings, budget=None, audit=None)


class DriverCoreJudgmentSurfaceTests(unittest.TestCase):
    def test_every_configured_extractor_is_deterministic_or_jev(self):
        for screen in (False, True):
            pools = _pools(screen_enabled=screen)
            for cls, pool in pools.items():
                self.assertTrue(pool.extractors)
                for extractor in pool.extractors:
                    self.assertIsInstance(
                        extractor, (StructuredExtractor, VisionExtractor),
                        f"{cls} pool holds a non-Jev, non-deterministic "
                        f"extractor: {type(extractor).__name__}")

    def test_vision_pool_is_jev_only_when_screen_enabled(self):
        pools = _pools(screen_enabled=True)
        gui = [c for c, p in pools.items()
               if any(isinstance(e, VisionExtractor) for e in p.extractors)]
        self.assertTrue(gui)
        for cls in gui:
            for extractor in pools[cls].extractors:
                self.assertIsInstance(extractor, VisionExtractor)


if __name__ == "__main__":
    unittest.main()
