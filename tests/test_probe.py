"""Known-answer capability probe: ceilings and error accounting."""
import unittest

from harness.capability import probe_json_reliability
from harness.errors import HarnessError
from tests._fake import FakeTransport, m


class ProbeCeilingTests(unittest.TestCase):
    def test_probe_preflights_against_ceiling(self):
        """#4: the capability probe loop must not spend through the ceiling."""
        captured = []

        class Gov:
            max_cost = 0.02
            spent = 0.015  # only $0.005 remains

            def check_byok(self, m): pass

            def preflight(self, prompt_text, calls):
                captured.append((prompt_text, calls))
                if self.spent + 0.006 > self.max_cost:
                    raise HarnessError("worst-case estimate exceeds ceiling. Refusing.")

        _fake = FakeTransport(models=[m("m1")])
        res = probe_json_reliability("t", "k", Gov(), ["m1"], max_tokens=16)
        self.assertEqual(len(captured), 5, "each question is preflighted")
        self.assertEqual(res["m1"]["errors"], 5, "blocked questions count as errors")
        self.assertEqual(res["m1"]["calls"], 5)
        self.assertEqual(res["m1"]["json_ok_rate"], 0.0)


if __name__ == "__main__":
    unittest.main()
