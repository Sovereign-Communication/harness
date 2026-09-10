"""Pins for minority-dissent demotion and MCP auth token."""
import json
import unittest
from types import SimpleNamespace

from harness.capability import order_pool, CapabilityProfile
from harness.convergence import tally_convergence
from harness.mcp import McpServer


def _panel(model, claims):
    body = {cid: {"real": real, "confidence": 0.9} for cid, real in claims.items()}
    return {"model": model, "content": json.dumps(body), "finish_reason": "stop"}


class MinorityDissentTests(unittest.TestCase):
    def test_tally_names_minority_models(self):
        panel = [
            _panel("alpha", {"C1": True}),
            _panel("beta", {"C1": True}),
            _panel("gemma", {"C1": False}),
        ]
        t = tally_convergence(panel, of_panel=3)
        self.assertEqual(t["claims"]["C1"]["minority_models"], ["gemma"])

    def test_unanimous_has_no_minority(self):
        panel = [
            _panel("a", {"C1": True}),
            _panel("b", {"C1": True}),
            _panel("c", {"C1": True}),
        ]
        t = tally_convergence(panel, of_panel=3)
        self.assertEqual(t["claims"]["C1"]["minority_models"], [])

    def test_order_pool_demotes_repeated_minority_dissent(self):
        good = CapabilityProfile("good:free", context_length=200000, free=True,
                                 prompt_price=0, completion_price=0,
                                 supports_reasoning=False,
                                 supports_structured_json=True)
        dissent = CapabilityProfile("dissent:free", context_length=200000,
                                    free=True, prompt_price=0, completion_price=0,
                                    supports_reasoning=False,
                                    supports_structured_json=True)
        profiles = {"good:free": good, "dissent:free": dissent}
        report = {"calibration": {
            "good:free": {"samples": 20, "success_rate": 0.9,
                          "unusable_outputs": 0, "consent_unusable": 0,
                          "minority_dissent": 0},
            "dissent:free": {"samples": 20, "success_rate": 0.9,
                             "unusable_outputs": 0, "consent_unusable": 0,
                             "minority_dissent": 3},
        }}
        ordered = order_pool(["dissent:free", "good:free"], profiles, report,
                             task="structured", free_tier=True)
        self.assertEqual(ordered[-1], "dissent:free")


class McpAuthTests(unittest.TestCase):
    def _server(self, token=None):
        return McpServer(
            transport=None, api_key="k", governor=SimpleNamespace(
                spent=0.0, max_cost=1.0), ledger=None,
            router=None, engine=None, auth_token=token)

    def test_no_token_configured_allows(self):
        s = self._server(None)
        self.assertIsNone(s._check_auth({"method": "tools/call", "id": 1,
                                         "params": {"name": "spend_status"}}))

    def test_token_required_when_configured(self):
        s = self._server("secret")
        denied = s._check_auth({"method": "tools/call", "id": 1,
                                "params": {"name": "spend_status"}})
        self.assertIsNotNone(denied)
        self.assertEqual(denied["error"]["code"], -32001)
        ok = s._check_auth({
            "method": "tools/call", "id": 1,
            "params": {"name": "spend_status",
                       "_meta": {"harness_token": "secret"}},
        })
        self.assertIsNone(ok)
        ok2 = s._check_auth({
            "method": "tools/call", "id": 1,
            "params": {"name": "spend_status", "harness_token": "secret"},
        })
        self.assertIsNone(ok2)

    def test_wrong_token_denied(self):
        s = self._server("secret")
        denied = s._check_auth({
            "method": "tools/call", "id": 1,
            "params": {"_meta": {"harness_token": "nope"}},
        })
        self.assertIsNotNone(denied)


if __name__ == "__main__":
    unittest.main()
