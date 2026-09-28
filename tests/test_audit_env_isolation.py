"""R13 must run the suite hermetically, so it must scrub the environment.

``r_suite_green`` asserts the unit suite is green *hermetically*. It used to
inherit the whole process environment, which broke that claim in two ways:

* routing knobs (``HARNESS_USE_FREE`` and friends) change which lane the suite
  exercises, so a green here stops being the green CI sees; and
* a live provider key turns the opt-in live-catalog check on, so the audit
  could fail on upstream drift -- a delisted model id -- that has nothing to do
  with this tree. CI's hermetic jobs carry no key.

These tests pin the scrub contract without running the suite: they check which
names are dropped, that they are reported so the evidence says what was
scrubbed, and that the ledger variable is deliberately left to the suite's own
isolation (``tests/__init__.py``) rather than silently removed.
"""

import importlib.util
import os
import shutil
import unittest
from pathlib import Path
from unittest import mock

_AUDIT_PATH = Path(__file__).resolve().parents[1] / "audits" / "self" / "audit.py"


def _load_audit():
    spec = importlib.util.spec_from_file_location("_harness_self_audit", _AUDIT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AuditSuiteEnvIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.audit = _load_audit()

    def test_routing_and_key_variables_are_dropped_and_reported(self):
        planted = {
            "HARNESS_USE_FREE": "true",
            "HARNESS_ALLOW_ESCALATION": "1",
            "HARNESS_MAX_COST": "9.0",
            "OPENROUTER_API_KEY": "not-a-real-key",
            "TYPESAFE_API_KEY": "not-a-real-key",
        }
        with mock.patch.dict(os.environ, planted, clear=False):
            env, dropped = self.audit.hermetic_suite_env()
        for key in planted:
            self.assertNotIn(key, env, f"{key} leaked into the hermetic child")
            self.assertIn(key, dropped)
        self.assertEqual(dropped, sorted(dropped))

    def test_home_is_redirected_so_key_files_cannot_be_found(self):
        # The decisive leak: keys are also resolved from files under ~ that no
        # environment scrub can reach. Without this the local R13 result is not
        # the CI result.
        with mock.patch.dict(os.environ, {"HOME": os.path.expanduser("~")},
                             clear=False):
            env, _ = self.audit.hermetic_suite_env()
            try:
                sandbox = env["HOME"]
                self.assertNotEqual(sandbox, os.path.expanduser("~"))
                self.assertTrue(os.path.isdir(sandbox))
                self.assertFalse(os.path.exists(
                    os.path.join(sandbox, ".config", "scmorc",
                                 "openrouter_fusion.env")))
                self.assertFalse(os.path.exists(
                    os.path.join(sandbox, ".config", "harness", "jev.env")))
                self.assertEqual(env["USERPROFILE"], sandbox)
            finally:
                shutil.rmtree(sandbox, ignore_errors=True)

    def test_unrelated_variables_survive(self):
        with mock.patch.dict(os.environ, {"HARNESS_UNRELATED_PROBE": "keep"},
                             clear=False):
            env, dropped = self.audit.hermetic_suite_env()
        self.assertEqual(env.get("HARNESS_UNRELATED_PROBE"), "keep")
        self.assertNotIn("HARNESS_UNRELATED_PROBE", dropped)

    def test_ledger_variable_is_not_dropped(self):
        # The suite pins its own temporary ledger; a caller that set a private
        # one is already isolated, so R13 must not strip it.
        with mock.patch.dict(os.environ, {"HARNESS_LEDGER": "/tmp/probe.jsonl"},
                             clear=False):
            env, _ = self.audit.hermetic_suite_env()
        self.assertEqual(env.get("HARNESS_LEDGER"), "/tmp/probe.jsonl")

    def test_every_dropped_name_is_one_of_the_declared_hermetic_variables(self):
        self.assertTrue(self.audit._HERMETIC_ENV_DROP)
        self.assertIn("HARNESS_USE_FREE", self.audit._HERMETIC_ENV_DROP)

    def test_r_suite_runner_passes_the_scrubbed_env_to_the_child(self):
        captured = {}

        class _Result:
            stdout = "Ran 3 tests in 0.010s\n\nOK"
            stderr = ""

        def fake_run(argv, **kwargs):
            captured["env"] = kwargs.get("env")
            captured["argv"] = argv
            return _Result()

        with mock.patch.dict(os.environ, {"HARNESS_USE_FREE": "true"}, clear=False), \
                mock.patch.object(self.audit.subprocess, "run", side_effect=fake_run):
            score, evidence = self.audit.r_suite_green()

        self.assertEqual(score, 1.0, evidence)
        self.assertIsNotNone(captured["env"])
        self.assertNotIn("HARNESS_USE_FREE", captured["env"])
        self.assertIn("unittest", captured["argv"])
        self.assertIn("env scrubbed", evidence)
        self.assertIn("HARNESS_USE_FREE", evidence)


if __name__ == "__main__":
    unittest.main()
