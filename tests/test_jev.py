"""Unit tests for Jev and System One structural evaluation adapter (#PR-Cost-2)."""
import unittest

from harness.config import load_settings
from harness.jev import JevEvaluationResult, JevEvaluator


class FakeTransport:
    def __init__(self, status=200, resp=None):
        self.status = status
        self.resp = resp or {}
        self.calls = []

    def post(self, url, api_key, payload):
        self.calls.append({"url": url, "api_key": api_key, "payload": payload})
        return self.status, self.resp


class JevEvaluatorTests(unittest.TestCase):
    def test_result_is_passing(self):
        res_pass = JevEvaluationResult(verdict="pass", confidence=0.85, supported=0.90)
        self.assertTrue(res_pass.is_passing(min_confidence=0.70))

        res_low_conf = JevEvaluationResult(verdict="pass", confidence=0.65, supported=0.90)
        self.assertFalse(res_low_conf.is_passing(min_confidence=0.70))

        res_low_supp = JevEvaluationResult(verdict="pass", confidence=0.85, supported=0.60)
        self.assertFalse(res_low_supp.is_passing(min_confidence=0.70))

        res_fail = JevEvaluationResult(verdict="fail", confidence=0.90, supported=0.90)
        self.assertFalse(res_fail.is_passing(min_confidence=0.70))

    def test_local_structural_eval_python_syntax(self):
        evaluator = JevEvaluator()

        # Clean Python syntax passes
        res_clean = evaluator.evaluate({"code": "def add(a, b):\n    return a + b\n"})
        self.assertEqual(res_clean.verdict, "pass")
        self.assertTrue(res_clean.is_fallback)
        self.assertGreaterEqual(res_clean.confidence, 0.70)

        # Broken syntax fails immediately
        res_broken = evaluator.evaluate({"code": "def add(a, b\n    return a + b\n"})
        self.assertEqual(res_broken.verdict, "fail")
        self.assertTrue(res_broken.is_fallback)
        self.assertLess(res_broken.confidence, 0.30)
        self.assertTrue(any("SyntaxError" in r for r in res_broken.reasons))

    def test_local_structural_eval_json(self):
        evaluator = JevEvaluator()

        # Valid JSON passes
        res_valid = evaluator.evaluate({"json_content": '{"status": "ok", "value": 42}'})
        self.assertEqual(res_valid.verdict, "pass")

        # Invalid JSON fails
        res_invalid = evaluator.evaluate({"json_content": '{"status": "ok", invalid}'})
        self.assertEqual(res_invalid.verdict, "fail")
        self.assertTrue(any("Invalid JSON" in r for r in res_invalid.reasons))

    def test_verify_diff_mechanics(self):
        evaluator = JevEvaluator()

        # Valid unified diff
        diff_ok = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-def old(): pass\n+def new(): pass\n"
        res_ok = evaluator.verify_diff_mechanics(diff=diff_ok, instruction="rename old to new", file_path="foo.py")
        self.assertEqual(res_ok.verdict, "pass")

        # Broken diff structure
        diff_bad = "just some random prose that is not a diff"
        res_bad = evaluator.verify_diff_mechanics(diff=diff_bad, instruction="rename old to new")
        self.assertEqual(res_bad.verdict, "fail")

        # Empty diff
        res_empty = evaluator.verify_diff_mechanics(diff="")
        self.assertEqual(res_empty.verdict, "fail")

    def test_native_jev_api_dispatch(self):
        mock_resp = {
            "answers": {
                "supported": {"probability": 0.94, "rationale": "Code matches instructions."},
                "confidence": {"score": 0.88},
                "syntax_clean": True,
            },
            "usage": {"cost": 0.00004},
            "reason": "Draft verified against provided context.",
        }
        transport = FakeTransport(status=200, resp=mock_resp)
        evaluator = JevEvaluator(
            api_key="jev-secret-key",
            endpoint="https://api.typesafe.ai/v1/eval",
            transport=transport,
        )

        res = evaluator.evaluate({"diff": "+print('hello')"})
        self.assertEqual(len(transport.calls), 1)
        call = transport.calls[0]
        self.assertEqual(call["url"], "https://api.typesafe.ai/v1/eval")
        self.assertEqual(call["api_key"], "jev-secret-key")
        self.assertEqual(res.verdict, "pass")
        self.assertAlmostEqual(res.confidence, 0.88)
        self.assertAlmostEqual(res.supported, 0.94)
        self.assertFalse(res.is_fallback)
        self.assertAlmostEqual(res.cost, 0.00004)

    def test_native_jev_api_fallback_on_network_error(self):
        # Transport returns 500 error; evaluator falls back cleanly to local structural check
        transport = FakeTransport(status=500, resp={"error": "server error"})
        evaluator = JevEvaluator(api_key="jev-key", transport=transport)

        res = evaluator.evaluate({"code": "x = 10\n"})
        self.assertEqual(res.verdict, "pass")
        self.assertTrue(res.is_fallback)

    def test_evaluator_with_settings(self):
        settings = load_settings({
            "jev_api_key": "custom-key",
            "jev_endpoint": "https://custom.eval/v1",
            "min_confidence": 0.80,
        })
        evaluator = JevEvaluator(settings=settings)
        self.assertEqual(evaluator.api_key, "custom-key")
        self.assertEqual(evaluator.endpoint, "https://custom.eval/v1")
        self.assertEqual(evaluator.min_confidence, 0.80)

    def test_session_jev_for(self):
        from harness.session import jev_for
        settings = load_settings({
            "jev_api_key": "custom-key",
            "jev_endpoint": "https://custom.eval/v1",
        })
        transport = FakeTransport()
        evaluator = jev_for(settings, transport=transport)
        self.assertEqual(evaluator.api_key, "custom-key")
        self.assertEqual(evaluator.endpoint, "https://custom.eval/v1")
        self.assertEqual(evaluator.transport, transport)


if __name__ == "__main__":
    unittest.main()

