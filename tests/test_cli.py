import unittest
from unittest import mock

from harness import cli
from harness.core import HarnessError


class MaxCostWiringTests(unittest.TestCase):
    def test_verify_max_cost_reaches_governor(self):
        """Regression: verify --max-cost was parsed but never wired, so the
        configured default ceiling silently applied instead."""
        captured = {}

        def fake_governor(settings, max_cost_override=None):
            captured["override"] = max_cost_override
            gov = mock.Mock()
            gov.verify_key.return_value = None
            gov.max_cost = max_cost_override or settings.max_cost
            gov.spent = 0.0
            gov.preflight.return_value = (0.0, [])
            gov.check_byok.return_value = None
            gov.learned_blocked.return_value = False
            gov.record_actual.return_value = None
            gov.cost_by_model.return_value = {}
            gov.is_free.return_value = True
            return "key", gov

        with mock.patch.object(cli, "_governor", side_effect=fake_governor), \
             mock.patch.object(cli, "panel_judge", return_value={}), \
             mock.patch.object(cli, "HttpTransport"), \
             mock.patch.object(cli, "_capability_context", return_value=(None, None)):
            cli.main(["verify", "--prompt", "hi", "--max-cost", "0.005"])
        self.assertEqual(captured["override"], 0.005)

    def test_verify_default_ceiling_when_flag_absent(self):
        captured = {}

        def fake_governor(settings, max_cost_override=None):
            captured["override"] = max_cost_override
            gov = mock.Mock()
            gov.verify_key.return_value = None
            gov.preflight.return_value = (0.0, [])
            gov.learned_blocked.return_value = False
            gov.is_free.return_value = True
            gov.cost_by_model.return_value = {}
            return "key", gov

        with mock.patch.object(cli, "_governor", side_effect=fake_governor), \
             mock.patch.object(cli, "panel_judge", return_value={}), \
             mock.patch.object(cli, "HttpTransport"), \
             mock.patch.object(cli, "_capability_context", return_value=(None, None)):
            cli.main(["verify", "--prompt", "hi"])
        self.assertIsNone(captured["override"])


class FriendlyInputErrorTests(unittest.TestCase):
    def test_read_json_missing(self):
        import os
        import tempfile
        missing = os.path.join(tempfile.gettempdir(), "harness-playtest-missing.json")
        if os.path.exists(missing):
            os.unlink(missing)
        with self.assertRaises(HarnessError) as cm:
            cli._read_json(missing, "--state continuation")
        self.assertIn("not readable", str(cm.exception))

    def test_read_json_invalid_content(self):
        import os
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write("{not json")
            path = f.name
        try:
            with self.assertRaises(HarnessError) as cm:
                cli._read_json(path, "--state continuation")
            self.assertIn("not valid JSON", str(cm.exception))
        finally:
            os.unlink(path)

    def test_read_text_missing(self):
        import os
        import tempfile
        missing = os.path.join(tempfile.gettempdir(), "harness-playtest-missing.txt")
        if os.path.exists(missing):
            os.unlink(missing)
        with self.assertRaises(HarnessError) as cm:
            cli._read_text(missing, "--source-file")
        self.assertIn("--source-file not readable", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
