import unittest

from harness.router import Router


class RouterTests(unittest.TestCase):
    def test_route_verify_and_code(self):
        r = Router(["a", "b"], "judge", "coder")
        spec = r.route("verify")
        self.assertEqual(spec["tier"], "panel")
        self.assertEqual(spec["panel"], ["a", "b"])
        self.assertEqual(r.route("code")["model"], "coder")

    def test_no_escalation_by_default(self):
        r = Router(["a"], "judge", "coder")
        self.assertIsNone(r.escalation())
        self.assertIsNone(r.escalation(override=False))

    def test_escalation_gated_on_allow(self):
        r = Router(["a"], "judge", "coder", escalation_model="big/model")
        self.assertIsNone(r.escalation())
        self.assertEqual(r.escalation(override=True)["model"], "big/model")
        r2 = Router(["a"], "judge", "coder", escalation_model="big/model",
                    allow_escalation=True)
        self.assertEqual(r2.escalation()["model"], "big/model")

    def test_escalation_needs_model(self):
        r = Router(["a"], "judge", "coder", allow_escalation=True)
        self.assertIsNone(r.escalation())


if __name__ == "__main__":
    unittest.main()