"""Conformance seed for #453 S3b/S4: harness judgment-surface provenance.

Finding (code reads, 2026-11-06): harness/jev_assessment.py is pure
deterministic composition — AspectJudgment/AssessmentVerdict over
payloads produced by Jev runs (its one production consumer, cli.py,
feeds it a payload JSON). It makes no model calls, so S3b needs no
re-pointing. This test pins the provenance: if a future change wires a
chat/transport call into the assessment module, the surface stops being
Jev-only by construction and this test fails.
"""
import ast
import os
import unittest

import harness.jev_assessment as assessment


class JevAssessmentProvenanceTests(unittest.TestCase):
    def test_assessment_module_has_no_model_transport(self):
        path = os.path.abspath(assessment.__file__)
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported.add(node.module)
        banned = {"harness.chat", "harness.panel", "harness._http",
                  "chat", "panel", "_http"}
        self.assertFalse(imported & banned,
                         f"jev_assessment imports model transport: "
                         f"{sorted(imported & banned)}")
        called = {node.func.attr for node in ast.walk(tree)
                  if isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Attribute)}
        self.assertNotIn("chat", called)
        self.assertNotIn("chat_routed", called)

    def test_verdict_composition_is_deterministic(self):
        probs = {">=90": 0.95, "85-89": 0.05, "<85": 0.0}
        payload = {"aspects": {name: {"probabilities": dict(probs)}
                               for name in assessment.ASPECTS}}
        first = assessment.verdict_for_payload(payload)
        second = assessment.verdict_for_payload(payload)
        self.assertEqual(first.to_dict(), second.to_dict())


if __name__ == "__main__":
    unittest.main()
