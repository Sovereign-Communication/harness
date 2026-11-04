"""The v1 gate's operative record must not resurrect the confidence bar.

`docs/JEV_GATE_POLICY_V1.md` is the ratified policy: Choice confidence is
telemetry. `docs/jev-calibration.md` predates it and describes a 0.95
threshold that was never the disposition gate. These checks fail if the
supersession note is dropped, if the policy's own sentence is removed, or if
an operative prompt starts stating a confidence minimum as permission.
"""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
POLICY = ROOT / "docs" / "JEV_GATE_POLICY_V1.md"
CALIBRATION = ROOT / "docs" / "jev-calibration.md"
PROMPTS = (
    ROOT / "CLAUDE.md",
    ROOT / "docs" / "jev-mission-prompt.md",
    ROOT / ".claude" / "skills" / "isolated-mission" / "SKILL.md",
    ROOT / ".claude" / "agents" / "harness-scout.md",
    ROOT / ".claude" / "agents" / "harness-implementer.md",
    ROOT / ".claude" / "agents" / "harness-verifier.md",
)

# A prompt that turns a Choice-confidence floor into permission to act.
BANNED = re.compile(
    r"confidence\s*(?:>=|≥|at least|minimum|min)\s*0?\.\d+"
    r"|0?\.95\s*(?:choice[-\s]?)?confidence"
    r"|min_confidence\s*(?:=|of)\s*0",
    re.IGNORECASE,
)


class PolicyRecordTests(unittest.TestCase):
    def test_the_policy_states_confidence_is_telemetry(self):
        text = POLICY.read_text(encoding="utf-8")
        self.assertIn("reported as telemetry but does NOT gate", text)

    def test_the_calibration_evidence_is_marked_superseded_for_the_gate(self):
        text = CALIBRATION.read_text(encoding="utf-8")
        self.assertIn("Superseded for the decision gate", text)
        self.assertIn("not a Choice-confidence", text)
        self.assertIn("advances_goal", text)

    def test_operative_prompts_do_not_state_a_confidence_gate(self):
        offenders = []
        for path in PROMPTS:
            text = path.read_text(encoding="utf-8")
            for match in BANNED.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                offenders.append(
                    "{}:{}: {}".format(
                        path.relative_to(ROOT), line, match.group(0)))
        self.assertEqual(offenders, [], "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()
