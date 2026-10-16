"""DRV-1 Protocol Conformance Tests.

Executes the 7 protocol assertions against a live, real driver-core service
over local loopback sockets, proving route names, bearer auth, refusal-as-200,
4xx/5xx boundary -> PerceptionUnavailable, reason set byte-identity,
and step_id round-tripping.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
import urllib.error
import urllib.request
from unittest import mock

import driver_core.config as dc_config
import driver_core.server as dc_server
from driver_core.audit import MemoryAuditLog
from driver_core.budget import Budget
from driver_core.consent_ledger import (
    ConsentLedgerUnavailable, RestConsentLedger,
)
from driver_core.driver import Driver, driver_from_settings
from driver_core.ev import FakeJev, action_answer
from driver_core.executor import Consent
from driver_core.extractors import ExtractorPool, StructuredExtractor
from driver_core.perception import StructuredSource, Target
from driver_core.states import CLI_SCHEMA
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
        # Absent, empty and whitespace-only schemas share the established
        # required-schema refusal on the real loopback HTTP surface.
        for step_body in (
                {"target": "cli"},
                {"target": "cli", "schema": ""},
                {"target": "cli", "schema": "  "}):
            req = urllib.request.Request(
                f"{self.base_url}/step",
                data=json.dumps(step_body).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.token}",
                },
                method="POST",
            )
            with self.subTest(body=step_body):
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


class DriverInputConformanceTest(unittest.TestCase):
    """Consent and backend policy hold across the live loopback surface."""

    def setUp(self):
        self.token = "input-conformance-token-1234"
        self.work = tempfile.mkdtemp(prefix="drv-consent-")
        self.addCleanup(shutil.rmtree, self.work, True)
        self.consent_ledger_path = os.path.join(
            self.work, "rest-consents.sqlite3")
        ledger_path_patch = mock.patch(
            "driver_core.server.default_consent_ledger_path",
            return_value=self.consent_ledger_path)
        ledger_path_patch.start()
        self.addCleanup(ledger_path_patch.stop)
        self.calls = []
        self.submit_entered = threading.Event()
        self.submit_release = threading.Event()
        self.submit_fail_next = False

        def backend(kind, value=None, target=None):
            self.calls.append((kind, value, target))
            if kind == "submit":
                if self.submit_fail_next:
                    self.submit_fail_next = False
                    return False, "planned test refusal"
                self.submit_entered.set()
                if not self.submit_release.wait(3):
                    return False, "test backend timed out"
                return True, "submitted test target"
            if kind != "focus":
                return False, f"test backend does not support {kind}"
            return True, "focused test target"

        backend.supported_kinds = ("focus", "submit")
        settings = dc_config.load_settings(
            env={}, allow_write=True, quorum=2, min_agreement=1.0,
            confidence_threshold=0.7, run_ceiling_usd=1.0,
            step_ceiling_usd=1.0,
            audit_path=os.path.join(self.work, "audit.jsonl"))
        state = {"exit_code": 0, "stdout": "", "stderr": ""}
        pool = ExtractorPool([
            StructuredExtractor(f"input-{i}",
                                lambda capture, schema: dict(state))
            for i in range(2)
        ])
        source = StructuredSource("cli", lambda target: dict(state),
                                  serves=("cli",))
        self.driver = Driver(
            settings=settings, budget=Budget(1.0, step_ceiling_usd=1.0),
            audit=MemoryAuditLog(), pool=pool,
            jev=FakeJev(action_answer("focus", confidence=0.99)),
            sources=[source], input_backend=backend)
        self.httpd, _ = dc_server.serve(
            host="127.0.0.1", port=0,
            service=dc_server.Service(self.driver, token=self.token),
            block=False)
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.adapter = PerceptionAdapter(
            base_url=f"http://127.0.0.1:{self.httpd.server_address[1]}",
            token=self.token)

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    @staticmethod
    def _consent(action="focus", target="test window", consent_id=None):
        consent = {"granted": True, "action": action,
                   "params": {"target": target}, "by": "conformance"}
        if consent_id is not None:
            consent["consent_id"] = consent_id
        return consent

    def _loopback_step(self, service, consent):
        httpd, _ = dc_server.serve(
            host="127.0.0.1", port=0, service=service, block=False)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            adapter = PerceptionAdapter(
                base_url=f"http://127.0.0.1:{httpd.server_address[1]}",
                token=service.token)
            return adapter.step(
                "test target", schema="cli", consent=consent)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)

    def _loopback_post(self, service, body):
        httpd, _ = dc_server.serve(
            host="127.0.0.1", port=0, service=service, block=False)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{httpd.server_address[1]}"
            return self._post_step(base_url, service.token, body)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)

    @staticmethod
    def _post_step(base_url, token, body):
        request = urllib.request.Request(
            f"{base_url}/step", data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {token}"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, json.loads(
                    response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.loads(
                    error.read().decode("utf-8"))
            finally:
                error.close()

    def test_exact_consent_and_supported_backend_perform_the_action(self):
        envelope = self.adapter.step(
            "test target", schema="cli",
            consent=self._consent(target="test window"))
        self.assertTrue(envelope["ok"], envelope)
        self.assertEqual(envelope["execution"]["action"], "focus")
        self.assertEqual(self.calls, [("focus", None, "test window")])

    def test_missing_consent_refuses_before_the_backend(self):
        envelope = self.adapter.step("test target", schema="cli")
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["reason"], "execution_refused")
        self.assertIn("no consent was supplied", envelope["detail"])
        self.assertEqual(self.calls, [])

    def test_malformed_consent_is_a_client_error_before_the_backend(self):
        consent = self._consent()
        string_grant = {**consent, "granted": "false"}
        integer_grant = {**consent, "granted": 1}
        list_params = {**consent, "params": []}
        null_params = {**consent, "params": None}
        list_by = {**consent, "by": []}
        empty_by = {**consent, "by": " "}
        empty_id = {**consent, "consent_id": "  "}
        long_id = {**consent, "consent_id": "x" * 129}
        for invalid in (string_grant, integer_grant, list_params,
                        null_params, list_by, empty_by, empty_id,
                        long_id, []):
            with self.subTest(consent=invalid):
                with self.assertRaises(PerceptionUnavailable):
                    self.adapter.step(
                        "test target", schema="cli", consent=invalid)
        self.assertEqual(self.calls, [])

    def test_step_rejects_unknown_and_malformed_wire_fields(self):
        base = {"target": "test target", "schema": "cli"}
        invalid_bodies = (
            {**base, "unexpected": True},
            {**base, "consent": {"granted": False, "unexpected": True}},
            {**base, "prefer": "mcp"},
            {**base, "prefer": ["mcp", 1]},
            {**base, "require_stable": "false"},
            {**base, "step_id": 123},
            {**base, "target": 123},
            {**base, "consent": None},
        )
        for body in invalid_bodies:
            with self.subTest(body=body):
                status, response = self._post_step(
                    self.adapter.base, self.token, body)
                self.assertEqual(status, 400)
                self.assertFalse(response["ok"])
        self.assertEqual(self.calls, [])

    def test_unavailable_consent_ledger_is_server_error_not_bad_request(self):
        os.mkdir(self.consent_ledger_path)
        valid_body = {
            "target": "test target",
            "schema": "cli",
            "consent": self._consent(consent_id="storage-failure"),
        }
        storage_status, storage_response = self._post_step(
            self.adapter.base, self.token, valid_body)

        malformed_body = {
            **valid_body,
            "consent": {**valid_body["consent"], "granted": "false"},
        }
        malformed_status, malformed_response = self._post_step(
            self.adapter.base, self.token, malformed_body)

        self.assertEqual(storage_status, 503)
        self.assertFalse(storage_response["ok"])
        self.assertIn("ledger is unavailable", storage_response["error"])
        self.assertEqual(malformed_status, 400)
        self.assertFalse(malformed_response["ok"])
        self.assertIn("boolean", malformed_response["error"])
        self.assertEqual(self.calls, [])

    def test_full_consent_ledger_is_server_error_not_bad_request(self):
        occupied = RestConsentLedger(self.consent_ledger_path, max_records=1)
        occupied.bind("capacity-occupied", {
            "granted": True, "action": "submit_irreversible",
            "params": {"target": "Existing"}, "by": "test",
        })
        with mock.patch(
                "driver_core.server.RestConsentLedger",
                side_effect=lambda path: RestConsentLedger(
                    path, max_records=1)):
            service = dc_server.Service(self.driver, token=self.token)
            valid_body = {
                "target": "test target", "schema": "cli",
                "consent": self._consent(
                    action="submit_irreversible", target="Publish",
                    consent_id="ledger-capacity-refusal"),
            }
            full_status, full_response = self._loopback_post(
                service, valid_body)
            malformed_status, malformed_response = self._loopback_post(
                service, {
                    **valid_body,
                    "consent": {**valid_body["consent"],
                                "granted": "false"},
                })

        self.assertEqual(full_status, 503)
        self.assertFalse(full_response["ok"])
        self.assertIn("ledger is full", full_response["error"])
        self.assertEqual(malformed_status, 400)
        self.assertFalse(malformed_response["ok"])
        self.assertIn("boolean", malformed_response["error"])
        self.assertEqual(self.calls, [])

    def test_finalization_failure_is_503_and_keeps_replay_blocked(self):
        self.driver.jev = FakeJev(action_answer(
            "submit_irreversible", confidence=0.99))
        self.submit_release.set()
        consent = self._consent(
            action="submit_irreversible", target="Publish",
            consent_id="publish-finalization-failure")
        body = {"target": "test target", "schema": "cli",
                "consent": consent}

        with mock.patch.object(
                RestConsentLedger, "_finish",
                side_effect=ConsentLedgerUnavailable(
                    "injected ledger finalization failure")):
            status, response = self._post_step(
                self.adapter.base, self.token, body)

        self.assertEqual(status, 503)
        self.assertFalse(response["ok"])
        self.assertIn("action succeeded", response["error"])
        self.assertIn("replay remains blocked", response["error"])
        self.assertEqual(self.calls, [("submit", None, "Publish")])

        retry_status, retry = self._post_step(
            self.adapter.base, self.token, body)
        self.assertEqual(retry_status, 200)
        self.assertFalse(retry["ok"])
        self.assertEqual(retry["reason"], "execution_refused")
        self.assertEqual(self.calls, [("submit", None, "Publish")])

    def test_mismatched_action_consent_refuses_before_the_backend(self):
        envelope = self.adapter.step(
            "test target", schema="cli",
            consent=self._consent(action="click"))
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["reason"], "execution_refused")
        self.assertIn("does not authorise 'focus'", envelope["detail"])
        self.assertEqual(self.calls, [])

    def test_mismatched_parameters_refuse_before_the_backend(self):
        result = self.driver.step(
            Target("test target", "cli"), schema=CLI_SCHEMA,
            consent=Consent(True, "focus", {"target": "consented window"}),
            params={"target": "different window"})
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "execution_refused")
        self.assertIn("does not authorise 'focus'", result.detail)
        self.assertEqual(self.calls, [])

    def test_empty_consent_params_refuse_before_the_backend(self):
        envelope = self.adapter.step(
            "test target", schema="cli",
            consent={"granted": True, "action": "click", "params": {},
                     "by": "conformance"})
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["reason"], "execution_refused")
        self.assertIn("missing required parameter", envelope["detail"])
        self.assertEqual(self.calls, [])

    def test_irreversible_rest_consent_requires_a_nonempty_id(self):
        self.driver.jev = FakeJev(action_answer(
            "submit_irreversible", confidence=0.99))
        with self.assertRaises(PerceptionUnavailable):
            self.adapter.step(
                "test target", schema="cli",
                consent=self._consent(
                    action="submit_irreversible", target="Publish"))
        self.assertEqual(self.calls, [])

    def test_failed_irreversible_attempt_releases_the_rest_consent(self):
        self.driver.jev = FakeJev(action_answer(
            "submit_irreversible", confidence=0.99))
        self.submit_fail_next = True
        self.submit_release.set()
        consent = self._consent(
            action="submit_irreversible", target="Publish",
            consent_id="publish-retry-1")

        failed = self.adapter.step(
            "test target", schema="cli", consent=consent)
        retried = self.adapter.step(
            "test target", schema="cli", consent=consent)

        self.assertFalse(failed["ok"])
        self.assertEqual(failed["reason"], "execution_refused")
        self.assertTrue(retried["ok"], retried)
        self.assertEqual(len(self.calls), 2)

    def test_consent_id_is_bound_to_one_exact_grant(self):
        consent = self._consent(consent_id="focus-confirmation-1")
        first = self.adapter.step(
            "test target", schema="cli", consent=consent)
        self.assertTrue(first["ok"], first)
        with self.assertRaises(PerceptionUnavailable):
            self.adapter.step(
                "test target", schema="cli",
                consent=self._consent(
                    target="different window",
                    consent_id="focus-confirmation-1"))
        self.assertEqual(self.calls, [("focus", None, "test window")])

    def test_irreversible_rest_consent_is_atomic_across_retries(self):
        self.driver.jev = FakeJev(action_answer(
            "submit_irreversible", confidence=0.99))
        consent = self._consent(
            action="submit_irreversible", target="Publish",
            consent_id="publish-confirmation-1")
        first_result = []
        first = threading.Thread(target=lambda: first_result.append(
            self.adapter.step("test target", schema="cli",
                              consent=consent)))
        first.start()
        try:
            self.assertTrue(self.submit_entered.wait(2),
                            "first request did not reach the backend")
            concurrent_retry = self.adapter.step(
                "test target", schema="cli", consent=consent)
        finally:
            self.submit_release.set()
            first.join(timeout=3)

        self.assertFalse(first.is_alive(), "first request did not finish")
        self.assertEqual(len(first_result), 1)
        self.assertTrue(first_result[0]["ok"], first_result[0])
        self.assertFalse(concurrent_retry["ok"])
        self.assertEqual(concurrent_retry["reason"], "execution_refused")

        replay = self.adapter.step(
            "test target", schema="cli", consent=consent)
        self.assertFalse(replay["ok"])
        self.assertEqual(replay["reason"], "execution_refused")
        self.assertEqual(
            self.calls, [("submit", None, "Publish")])

    def test_spent_consent_id_stays_spent_after_service_recreation(self):
        self.driver.jev = FakeJev(action_answer(
            "submit_irreversible", confidence=0.99))
        self.submit_release.set()
        consent = self._consent(
            action="submit_irreversible", target="Publish",
            consent_id=f"publish-recreated-{uuid.uuid4().hex}")
        first = self.adapter.step(
            "test target", schema="cli", consent=consent)
        self.assertTrue(first["ok"], first)
        self.assertEqual(self.calls, [("submit", None, "Publish")])

        # Recreate the service boundary around the same driver and bearer
        # credential, then replay through a second real loopback endpoint.
        service = dc_server.Service(self.driver, token=self.token)
        httpd, _ = dc_server.serve(
            host="127.0.0.1", port=0, service=service, block=False)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            adapter = PerceptionAdapter(
                base_url=f"http://127.0.0.1:{httpd.server_address[1]}",
                token=self.token)
            replay = adapter.step(
                "test target", schema="cli", consent=consent)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)

        self.assertFalse(replay["ok"])
        self.assertEqual(replay["reason"], "execution_refused")
        self.assertEqual(self.calls, [("submit", None, "Publish")])

    def test_spent_consent_id_stays_spent_when_generated_token_changes(self):
        self.driver.jev = FakeJev(action_answer(
            "submit_irreversible", confidence=0.99))
        self.submit_release.set()
        consent = self._consent(
            action="submit_irreversible", target="Publish",
            consent_id=f"publish-token-rotation-{uuid.uuid4().hex}")

        first_service = dc_server.Service(self.driver)
        first = self._loopback_step(first_service, consent)
        second_service = dc_server.Service(self.driver)
        replay = self._loopback_step(second_service, consent)

        self.assertNotEqual(first_service.token, second_service.token)
        self.assertTrue(first["ok"], first)
        self.assertFalse(replay["ok"], replay)
        self.assertEqual(replay["reason"], "execution_refused")
        self.assertEqual(self.calls, [("submit", None, "Publish")])

    def test_spent_consent_id_stays_spent_when_audit_path_changes(self):
        self.driver.jev = FakeJev(action_answer(
            "submit_irreversible", confidence=0.99))
        self.submit_release.set()
        consent = self._consent(
            action="submit_irreversible", target="Publish",
            consent_id=f"publish-audit-rotation-{uuid.uuid4().hex}")

        first_service = dc_server.Service(self.driver, token=self.token)
        first = self._loopback_step(first_service, consent)
        old_audit_path = self.driver.settings.audit_path
        self.driver.settings = self.driver.settings.with_overrides(
            audit_path=os.path.join(self.work, "rotated-audit.jsonl"))
        second_service = dc_server.Service(self.driver, token=self.token)
        replay = self._loopback_step(second_service, consent)

        self.assertNotEqual(old_audit_path, self.driver.settings.audit_path)
        self.assertTrue(first["ok"], first)
        self.assertFalse(replay["ok"], replay)
        self.assertEqual(replay["reason"], "execution_refused")
        self.assertEqual(self.calls, [("submit", None, "Publish")])

    def test_spent_consent_id_stays_spent_after_process_restart(self):
        script = """
import json, sys
from driver_core.audit import MemoryAuditLog
from driver_core.budget import Budget
from driver_core.config import load_settings
from driver_core.driver import Driver
from driver_core.ev import FakeJev, action_answer
from driver_core.extractors import ExtractorPool, StructuredExtractor
from driver_core.perception import StructuredSource
import driver_core.server as server

path, ledger_path, token, consent_id = sys.argv[1:]
server.default_consent_ledger_path = lambda: ledger_path
settings = load_settings(
    env={}, allow_write=True, quorum=2, min_agreement=1.0,
    confidence_threshold=0.7, run_ceiling_usd=1.0,
    step_ceiling_usd=1.0, audit_path=path)
state = {"exit_code": 0, "stdout": "", "stderr": ""}
pool = ExtractorPool([
    StructuredExtractor(f"restart-{i}", lambda capture, schema: dict(state))
    for i in range(2)
])
source = StructuredSource("cli", lambda target: dict(state), serves=("cli",))
calls = []
def backend(kind, value=None, target=None):
    calls.append((kind, value, target))
    return True, "submitted"
backend.supported_kinds = ("submit",)
driver = Driver(
    settings=settings, budget=Budget(1.0, step_ceiling_usd=1.0),
    audit=MemoryAuditLog(), pool=pool,
    jev=FakeJev(action_answer("submit_irreversible", confidence=0.99)),
    sources=[source], input_backend=backend)
body = {"target": "restart target", "schema": "cli", "consent": {
    "granted": True, "action": "submit_irreversible",
    "params": {"target": "Publish"}, "by": "restart-test",
    "consent_id": consent_id}}
outcome = server.Service(driver, token=token).handle("step", body)
print(json.dumps({"status": outcome["status"], "body": outcome["body"],
                  "calls": calls}))
"""
        consent_id = f"process-restart-{uuid.uuid4().hex}"
        command = [sys.executable, "-c", script,
                   os.path.join(self.work, "restart-audit.jsonl"),
                   self.consent_ledger_path, self.token, consent_id]

        first = json.loads(subprocess.run(
            command, cwd=os.getcwd(), check=True, capture_output=True,
            text=True, timeout=15).stdout)
        replay = json.loads(subprocess.run(
            command, cwd=os.getcwd(), check=True, capture_output=True,
            text=True, timeout=15).stdout)

        self.assertEqual(first["status"], 200)
        self.assertTrue(first["body"]["ok"], first)
        self.assertEqual(first["calls"], [["submit", None, "Publish"]])
        self.assertEqual(replay["status"], 200)
        self.assertFalse(replay["body"]["ok"], replay)
        self.assertEqual(replay["body"]["reason"], "execution_refused")
        self.assertEqual(replay["calls"], [])

    def test_unsupported_backend_action_is_refused(self):
        self.driver.jev = FakeJev(action_answer("click", confidence=0.99))
        envelope = self.adapter.step(
            "test target", schema="cli", consent=self._consent(action="click"))
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["reason"], "execution_refused")
        self.assertIn("does not support 'click'", envelope["detail"])
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
