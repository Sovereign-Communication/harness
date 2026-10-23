"""DRV-1 Protocol Conformance Tests.

Executes the 7 protocol assertions against a live, real driver-core service
over local loopback sockets, proving route names, bearer auth, refusal-as-200,
4xx/5xx boundary -> PerceptionUnavailable, reason set byte-identity,
and step_id round-tripping.
"""
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

import driver_core.config as dc_config
import driver_core.server as dc_server
from driver_core.driver import driver_from_settings
from harness.perception_client import (
    STOP_REASONS,
    PerceptionAdapter,
    PerceptionUnavailable,
)


class DriverLiveConformanceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.mkdtemp(prefix="drv-conf-")
        cls.token = "conformance-probe-token-1234"
        cls.audit_path = os.path.join(cls.work, "audit.jsonl")

        # Configure environment for ephemeral port, test token, and clean tiers
        cls.orig_env = dict(os.environ)
        os.environ["DRIVER_TOKEN"] = cls.token
        os.environ["DRIVER_PORT"] = "0"
        os.environ["DRIVER_AUDIT_PATH"] = cls.audit_path
        for name in ("DRIVER_CLI_COMMAND", "DRIVER_DOM_URL", "DRIVER_MCP_COMMAND",
                     "DRIVER_MCP_TOOL", "DRIVER_SCREEN", "DRIVER_HOST"):
            os.environ.pop(name, None)

        settings = dc_config.load_settings()
        driver = driver_from_settings(settings)
        service = dc_server.Service(driver, token=cls.token)
        cls.httpd, _ = dc_server.serve(port=0, service=service, block=False)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

        cls.port = cls.httpd.server_address[1]
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        cls.adapter = PerceptionAdapter(base_url=cls.base_url, token=cls.token)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.httpd.shutdown()
            cls.httpd.server_close()
        except Exception:
            pass
        os.environ.clear()
        os.environ.update(cls.orig_env)
        shutil.rmtree(cls.work, ignore_errors=True)

    def test_assertion_1_route_names_and_404(self):
        """1. Route names: all 5 answer, unknown route returns 404."""
        health = self.adapter.health()
        self.assertEqual(health.get("status"), "up")
        self.assertIn("version", health)

        vocab = self.adapter.vocabulary()
        self.assertTrue(vocab.get("ok"))
        self.assertEqual(len(vocab.get("vocabulary", {}).get("actions", [])), 14)

        schemas = self.adapter.schemas()
        self.assertTrue(schemas.get("ok"))
        self.assertIn("schemas", schemas)

        verify = self.adapter.verify()
        self.assertTrue(verify.get("ok"))

        # Unknown route returns 404
        req = urllib.request.Request(
            f"{self.base_url}/not-a-real-route",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 404)
        ctx.exception.close()

    def test_assertion_2_auth_rejections_and_success(self):
        """2. Auth: missing token -> 401; short token -> 401; valid token -> 200."""
        # Missing token
        req_no_auth = urllib.request.Request(f"{self.base_url}/health")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req_no_auth)
        self.assertEqual(ctx.exception.code, 401)
        ctx.exception.close()

        # Short token (< 16 chars)
        req_short = urllib.request.Request(
            f"{self.base_url}/health",
            headers={"Authorization": "Bearer short"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req_short)
        self.assertEqual(ctx.exception.code, 401)
        ctx.exception.close()

        # Valid token answers 200
        req_valid = urllib.request.Request(
            f"{self.base_url}/health",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        with urllib.request.urlopen(req_valid) as res:
            self.assertEqual(res.status, 200)

    def test_assertion_3_and_4_refusal_as_200_closed_reasons(self):
        """3 & 4. Refusal-as-200 with no key:

        - With no source declared -> 200 ok: false, reason: "no_capture"
        - The adapter returns this envelope rather than raising.
        """
        # Step with no sources declared returns "no_capture" envelope
        envelope = self.adapter.step(target="cli", schema="cli", step_id="step-conf-001")
        self.assertIsInstance(envelope, dict)
        self.assertFalse(envelope.get("ok"))
        self.assertEqual(envelope.get("reason"), "no_capture")
        self.assertEqual(envelope.get("stopped_at"), "capture")
        # 11 keys in response contract
        for key in ("step_id", "ok", "stopped_at", "reason", "detail", "capture",
                    "agreement", "decision", "execution", "receipt", "cost_usd"):
            self.assertIn(key, envelope)

    def test_assertion_5_4xx_boundary_raises_perception_unavailable(self):
        """5. 4xx boundary -> PerceptionUnavailable (e.g. absent schema)."""
        # Absent schema is a 400 bad request from server, adapter must raise
        req = urllib.request.Request(
            f"{self.base_url}/step",
            data=json.dumps({"target": "cli"}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 400)
        body = json.loads(ctx.exception.read().decode("utf-8"))
        self.assertFalse(body.get("ok"))
        self.assertIn("schema is required", body.get("error", ""))
        ctx.exception.close()

        # Through adapter, absent schema raises PerceptionUnavailable
        with self.assertRaises(PerceptionUnavailable):
            self.adapter.step(target="cli", schema=None)

    def test_assertion_6_reason_set_byte_identical(self):
        """6. STOP_REASONS is byte-identical between driver_core and harness adapter."""
        expected = (
            "no_capture",
            "insufficient_agreement",
            "extraction_disagreement",
            "confidence_below_threshold",
            "state_not_stable",
            "decision_not_usable",
            "no_action_recommended",
            "undeclared_action",
            "execution_refused",
        )
        self.assertEqual(STOP_REASONS, expected)
        self.assertEqual(dc_server.STOP_REASONS, expected)
        self.assertEqual(STOP_REASONS, dc_server.STOP_REASONS)

    def test_assertion_7_step_id_round_trips(self):
        """7. step_id echoed verbatim in the envelope to preserve audit join."""
        test_id = "conformance-step-echo-987654"
        envelope = self.adapter.step(target="cli", schema="cli", step_id=test_id)
        self.assertEqual(envelope.get("step_id"), test_id)

    def test_verify_audit_contract(self):
        """Audit verification report structure."""
        report = self.adapter.verify()
        self.assertTrue(report.get("ok"))
        self.assertIn("audit", report)
        self.assertIn("budget", report)
        self.assertTrue(report["audit"].get("ok"))


if __name__ == "__main__":
    unittest.main()
