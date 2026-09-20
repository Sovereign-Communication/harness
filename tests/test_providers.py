"""Tests for modular provider registry and adapter interface."""
import unittest
from harness.providers import ProviderAdapter, ProviderRegistry


class DummyProvider(ProviderAdapter):
    def __init__(self, name: str, available: bool = True):
        self._name = name
        self._available = available

    @property
    def name(self) -> str:
        return self._name

    def is_available(self) -> bool:
        return self._available

    def execute_node(
        self,
        instruction: str,
        target_file=None,
        context_brief="",
        verification_gate=None,
        timeout=120,
    ):
        return {
            "status": "ok",
            "provider": self.name,
            "instruction": instruction,
            "target_file": target_file,
            "gate": verification_gate,
        }


class ProviderRegistryTests(unittest.TestCase):
    def test_register_and_get(self):
        p1 = DummyProvider("mock_cli", available=True)
        p2 = DummyProvider("mock_unavailable", available=False)

        ProviderRegistry.register(p1)
        ProviderRegistry.register(p2)

        self.assertEqual(ProviderRegistry.get("mock_cli"), p1)
        self.assertEqual(ProviderRegistry.get("mock_unavailable"), p2)
        self.assertIsNone(ProviderRegistry.get("nonexistent"))

        available = ProviderRegistry.available_providers()
        self.assertIn("mock_cli", available)
        self.assertNotIn("mock_unavailable", available)

    def test_adapter_execute(self):
        adapter = DummyProvider("test_driver")
        res = adapter.execute_node(
            instruction="refactor foo",
            target_file="foo.py",
            context_brief="code brief",
            verification_gate="pytest",
        )
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["provider"], "test_driver")
        self.assertEqual(res["target_file"], "foo.py")


if __name__ == "__main__":
    unittest.main()
