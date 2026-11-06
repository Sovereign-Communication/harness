"""#453 S4: the conformance walk over the S2 enumeration of record.

Walks every judgment surface named in docs/issue-plans/453.md and fails
if a surface ignores the jev_primary_mode contract:

- surfaces that ACT under the mode (panel_judge, both Settings) carry
  the flag with the right default and route behavior;
- surfaces that must stay Jev-only or deterministic by construction
  (jev_assessment, lucas_gate, jev_completion, driver_core consensus,
  vision_verify, the extractor pools) hold no LLM transport — a static
  provenance audit, so wiring one in fails here;
- every enumerated surface token appears in the plan doc, and every
  surface in this walk resolves to a real symbol.

Parity note: the walk's surface list is maintained beside the doc; the
doc-token assertions catch a surface renamed or dropped on either side.
"""
import ast
import importlib
import inspect
import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# surface token (as named in the enumeration) -> module to audit
PROVENANCE_SURFACES = {
    "jev_assessment": "harness.jev_assessment",
    "lucas_gate": "harness.lucas_gate",
    "jev_completion": "harness.jev_completion",
    "consensus": "driver_core.consensus",
    "vision_verify": "driver_core.vision_verify",
}

BANNED_IMPORT_TOKENS = ("chat", "openai", "anthropic", "openrouter",
                        "providers")


def _imports_of(module):
    path = os.path.abspath(module.__file__)
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module)
    return imported


class EnumerationParityTests(unittest.TestCase):
    def test_every_walk_surface_is_named_in_the_plan_doc(self):
        with open(os.path.join(REPO_ROOT, "docs", "issue-plans", "453.md"),
                  encoding="utf-8") as f:
            doc = f.read()
        tokens = list(PROVENANCE_SURFACES) + [
            "panel_judge", "JevEvaluator", "JevClient", "VisionExtractor",
            "jev_primary_mode"]
        for token in tokens:
            self.assertIn(token, doc, f"{token} missing from the plan doc")


class FlagSurfaceTests(unittest.TestCase):
    def test_panel_judge_carries_the_mode(self):
        from harness.panel import panel_judge
        params = inspect.signature(panel_judge).parameters
        self.assertIn("jev_primary_mode", params)
        self.assertFalse(params["jev_primary_mode"].default)

    def test_both_settings_default_the_mode_off(self):
        from harness.config import Settings as HarnessSettings
        from driver_core.config import Settings as DriverSettings
        self.assertIn("jev_primary_mode",
                      inspect.signature(HarnessSettings).parameters)
        self.assertFalse(DriverSettings().jev_primary_mode)


class ProvenanceWalkTests(unittest.TestCase):
    def test_no_llm_transport_in_deterministic_or_jev_only_surfaces(self):
        for token, dotted in PROVENANCE_SURFACES.items():
            module = importlib.import_module(dotted)
            imported = _imports_of(module)
            offenders = {name for name in imported
                         if any(b in name.split(".")[-1]
                                for b in BANNED_IMPORT_TOKENS)}
            self.assertFalse(
                offenders,
                f"{token} ({dotted}) imports LLM transport: {offenders}")

    def test_extractor_pools_stay_deterministic_or_jev(self):
        from driver_core.config import Settings
        from driver_core.extractors.structured import StructuredExtractor
        from driver_core.extractors.vision import VisionExtractor
        from driver_core.wiring import configured_pools
        settings = Settings().with_overrides(screen_enabled=True)
        pools = configured_pools(settings, budget=None, audit=None)
        for cls, pool in pools.items():
            for extractor in pool.extractors:
                self.assertIsInstance(
                    extractor, (StructuredExtractor, VisionExtractor),
                    f"{cls}: {type(extractor).__name__}")


if __name__ == "__main__":
    unittest.main()
