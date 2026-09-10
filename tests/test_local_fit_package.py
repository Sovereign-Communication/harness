"""Package-binding and flag-contract tests for harness.local_fit (C1).

Guards the C1 repair: importing harness.local_fit must bind its submodules
(previously a nonexistent `.model` import was swallowed by a bare except, so
nothing bound), and the legacy HARVEST_LOCAL_FIT_* flag names must be inert.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


class TestPackageBinding(unittest.TestCase):
    def test_plain_import_binds_public_submodules(self):
        # Import via importlib from a clean module name so this test works
        # regardless of whether harness.local_fit was already imported.
        # dispatch is the live order_pool integration; the hook/dispatch_hook
        # prototype seam was removed (contradictory ordering semantics), and
        # train stays unbound (numpy at import time).
        import importlib
        pkg = importlib.import_module("harness.local_fit")
        for name in ("schema", "extract", "config", "model_loader", "infer",
                     "features", "dispatch"):
            self.assertTrue(hasattr(pkg, name), f"harness.local_fit.{name} not bound on import")
            self.assertIs(getattr(pkg, name), importlib.import_module(f"harness.local_fit.{name}"))

    def test_dispatch_callable_surface_reachable_from_package_import(self):
        import importlib
        pkg = importlib.import_module("harness.local_fit")
        self.assertTrue(callable(pkg.dispatch.maybe_order_pool))
        self.assertTrue(callable(pkg.config.is_enabled))


class TestFlagContract(unittest.TestCase):
    def test_legacy_harvest_flags_are_inert(self):
        from harness.local_fit import config
        old_env = {k: v for k, v in os.environ.items() if k.startswith("HARNESS_LOCAL_FIT")}
        os.environ.pop("HARNESS_LOCAL_FIT_ENABLE", None)
        os.environ.pop("HARNESS_LOCAL_FIT_MODEL_DIR", None)
        try:
            os.environ["HARVEST_LOCAL_FIT_ENABLE"] = "1"
            os.environ["HARVEST_LOCAL_FIT_MODEL_DIR"] = "/tmp"
            self.assertFalse(config.is_enabled(),
                             "legacy HARVEST_* flag must not enable the layer")
        finally:
            os.environ.pop("HARVEST_LOCAL_FIT_ENABLE", None)
            os.environ.pop("HARVEST_LOCAL_FIT_MODEL_DIR", None)
            os.environ.update(old_env)

    def test_new_flags_enable(self):
        from harness.local_fit import config
        old_env = {k: v for k, v in os.environ.items() if k.startswith(("HARNESS_LOCAL_FIT", "HARVEST_LOCAL_FIT"))}
        for k in list(old_env):
            os.environ.pop(k, None)
        try:
            self.assertFalse(config.is_enabled())
            os.environ["HARNESS_LOCAL_FIT_ENABLE"] = "1"
            self.assertFalse(config.is_enabled(), "ENABLE alone must not enable: MODEL_DIR is also required")
            os.environ["HARNESS_LOCAL_FIT_MODEL_DIR"] = os.getcwd()
            self.assertTrue(config.is_enabled())
        finally:
            for k in ("HARNESS_LOCAL_FIT_ENABLE", "HARNESS_LOCAL_FIT_MODEL_DIR", "HARVEST_LOCAL_FIT_ENABLE", "HARVEST_LOCAL_FIT_MODEL_DIR"):
                os.environ.pop(k, None)
            os.environ.update(old_env)


if __name__ == "__main__":
    unittest.main()
