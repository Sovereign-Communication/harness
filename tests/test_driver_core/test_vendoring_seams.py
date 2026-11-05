"""Pin the two harness patches a wholesale vendoring copy silently drops.

The 2026-11-04 #292 attempt replaced ``driver_core`` wholesale with upstream
3.5.0 and lost two harness patches that nothing failed on until the whole
driver was exercised: the vision pool's image-preparer seam
(``build_vision_pool`` lost its ``image_preparer`` keyword and the extractor
arity drifted) and the input-capability lock definition
(``_INPUT_CAPABILITY_LOCK`` was used by ``freeze_input_backend`` but never
defined, which raised ``NameError`` for every capability test).

These are the seams named in the recorded S1 order. The tests assert the
contract rather than the implementation: the pool builder keeps the seam,
every extractor accepts the arguments the pool actually sends, and the
registry is guarded by a reentrant lock that exists.
"""
import inspect
import unittest

from driver_core import osal
from driver_core.extractors.pool import ExtractorPool, build_vision_pool
from driver_core.extractors.structured import StructuredExtractor
from driver_core.extractors.vision import VisionExtractor


class _Result:
    """The minimum an extractor result must carry for the pool to tally it."""

    ok = True
    state = {"pinned": True}
    cost = 0.0
    usage_source = "test"



class VisionPoolSeamTests(unittest.TestCase):
    def test_the_pool_builder_still_accepts_the_image_preparer_seam(self):
        params = inspect.signature(build_vision_pool).parameters
        self.assertIn(
            "image_preparer", params,
            "build_vision_pool lost the harness image-preparation seam in a "
            "vendoring copy; the pool must prepare an asset before it bills")
        self.assertIsNone(params["image_preparer"].default)

    def test_every_extractor_accepts_the_arguments_the_pool_sends(self):
        for cls in (StructuredExtractor, VisionExtractor):
            with self.subTest(extractor=cls.__name__):
                signature = inspect.signature(cls.extract)
                positional = [
                    p for p in list(signature.parameters.values())[1:]
                    if p.kind in (inspect.Parameter.POSITIONAL_ONLY,
                                  inspect.Parameter.POSITIONAL_OR_KEYWORD)]
                if getattr(cls, "uses_capture", True):
                    self.assertGreaterEqual(
                        len(positional), 2,
                        f"{cls.__name__}.extract must take (capture, schema); "
                        f"got {signature}")
                else:
                    self.assertGreaterEqual(
                        len(positional), 1,
                        f"{cls.__name__}.extract must take schema; "
                        f"got {signature}")
                    self.assertIn(
                        "capture", signature.parameters,
                        f"{cls.__name__} declares uses_capture=False but must "
                        f"still accept a capture for the prepared-asset path; "
                        f"got {signature}")

    def test_the_pool_passes_capture_first_and_schema_second(self):
        seen = []

        class WithCapture:
            uses_capture = True

            def extract(self, capture, schema):
                seen.append((capture, schema))
                return _Result()

        class WithoutCapture:
            uses_capture = False

            def extract(self, schema, capture=None):
                seen.append((schema, capture))
                return _Result()

        votes = ExtractorPool([WithCapture(), WithoutCapture()]).run(
            "CAPTURE", "SCHEMA")
        self.assertEqual(len(votes), 2)
        self.assertEqual(seen[0], ("CAPTURE", "SCHEMA"))
        self.assertEqual(seen[1], ("SCHEMA", None))


class InputCapabilityLockTests(unittest.TestCase):
    def test_the_lock_is_defined_and_reentrant(self):
        self.assertTrue(
            hasattr(osal, "_INPUT_CAPABILITY_LOCK"),
            "freeze_input_backend uses _INPUT_CAPABILITY_LOCK; a vendoring "
            "copy that drops its definition breaks every capability path")
        lock = osal._INPUT_CAPABILITY_LOCK
        self.assertTrue(lock.acquire(blocking=False))
        try:
            self.assertTrue(
                lock.acquire(blocking=False),
                "the lock must be reentrant: register_input_backend freezes "
                "through freeze_input_backend while already holding it")
            lock.release()
        finally:
            lock.release()

    def test_registry_access_is_guarded_by_the_lock(self):
        class Recorder:
            def __init__(self):
                self.entries = 0

            def __enter__(self):
                self.entries += 1
                return self

            def __exit__(self, *exc):
                return False

        original = osal._INPUT_CAPABILITY_LOCK
        recorder = Recorder()
        platform = osal.platform_name()
        osal._INPUT_CAPABILITY_LOCK = recorder
        try:
            osal.register_input_backend(platform, lambda *a, **k: True)
            osal.input_backends()
            osal.disable_input_backend(platform)
        finally:
            osal._INPUT_CAPABILITY_LOCK = original
            osal._INPUT_BACKENDS.pop(platform, None)
        self.assertGreaterEqual(
            recorder.entries, 3,
            "register, read and disable must each take the capability lock")


if __name__ == "__main__":
    unittest.main()
