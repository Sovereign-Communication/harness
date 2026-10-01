"""DRV-1: hermetic coverage for harness.perception_client.

No network, no screen, no model, no driver process involved:
`PerceptionAdapter` takes an injectable `opener` seam and every test here
supplies a fake transport.

The tests are weighted toward one distinction, because it is the one this
adapter exists to keep and the one a copy of the media adapter would get
wrong: **a refusal is a successful call.** The driver answers HTTP 200 with
`ok: false` and a closed-set reason when it declines to act, so
`step()` returns that envelope. Only transport failures and contract
violations raise. Getting this backwards makes a caller retry a decision,
or retry an outage as though the driver had decided something.
"""
import io
import json
import os
import pathlib
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

from harness.perception_client import (
    STOP_REASONS, PerceptionAdapter, PerceptionUnavailable, run_cli,
)


class _FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _RawResponse:
    """A response whose body is not JSON, for the non-JSON guard."""

    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_opener(responses):
    """Build an opener(req, timeout=...) that pops queued responses in order.

    Queued items are a dict (200 JSON body), a `_RawResponse`, or an
    exception instance/class to raise. `opener.calls` records
    (method, url, parsed body, headers) so a test can assert on what the
    adapter actually sent -- which is the only way to prove the adapter
    never manufactures a consent envelope.
    """
    calls = []

    def opener(req, timeout=None):
        raw = req.data.decode("utf-8") if getattr(req, "data", None) else None
        calls.append({
            "method": req.get_method(),
            "url": req.full_url,
            "body": json.loads(raw) if raw else None,
            "headers": dict(req.headers),
        })
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, type) and issubclass(item, Exception):
            raise item()
        if isinstance(item, _RawResponse):
            return item
        return _FakeResponse(item)

    opener.calls = calls
    return opener


def _http_error(code, payload):
    body = json.dumps(payload).encode("utf-8")
    return urllib.error.HTTPError(
        url="http://example.invalid", code=code, msg="err",
        hdrs=None, fp=io.BytesIO(body),
    )


def _http_error_raw(code, body):
    return urllib.error.HTTPError(
        url="http://example.invalid", code=code, msg="err",
        hdrs=None, fp=io.BytesIO(body),
    )


def _ok_envelope(**over):
    env = {
        "step_id": "abc123", "ok": True, "stopped_at": None, "reason": None,
        "detail": "", "capture": {"fingerprint": "sha256:deadbeef"},
        "agreement": None,
        "decision": {"action": "read_text"},
        "execution": {"ok": True, "executor": "local"},
        "receipt": None, "cost_usd": 0.0000642,
    }
    env.update(over)
    return env


def _adapter(responses, **kw):
    opener = _fake_opener(responses)
    return PerceptionAdapter(base_url="http://127.0.0.1:8791", token="t0k",
                             opener=opener, **kw), opener


class SuccessPathTests(unittest.TestCase):
    def test_step_returns_the_envelope_verbatim(self):
        env = _ok_envelope()
        adapter, opener = _adapter([env])
        got = adapter.step("file-manager", schema="screen")
        self.assertTrue(got["ok"])
        self.assertEqual(got["step_id"], "abc123")
        self.assertEqual(got["cost_usd"], 0.0000642)
        self.assertEqual(opener.calls[0]["method"], "POST")
        self.assertEqual(opener.calls[0]["url"], "http://127.0.0.1:8791/step")

    def test_step_targets_the_declared_route(self):
        adapter, opener = _adapter([_ok_envelope()])
        adapter.step("file-manager", schema="screen")
        self.assertEqual(opener.calls[0]["body"]["target"], "file-manager")
        self.assertEqual(opener.calls[0]["body"]["schema"], "screen")

    def test_unset_fields_are_absent_rather_than_null(self):
        """The service must be able to tell "not asked for" from
        "explicitly asked for null"; an absent key is the only encoding
        that makes that distinction without a second field."""
        adapter, opener = _adapter([_ok_envelope()])
        adapter.step("file-manager", schema="screen")
        body = opener.calls[0]["body"]
        for key in ("consent", "prefer", "step_id"):
            self.assertNotIn(key, body,
                             "{} should be absent, not null".format(key))

    def test_read_routes_hit_their_own_paths(self):
        adapter, opener = _adapter([{"ok": True, "status": "up"},
                                    {"ok": True, "schemas": []},
                                    {"ok": True, "vocabulary": {}},
                                    {"ok": True, "audit": {"ok": True}}])
        adapter.health()
        adapter.schemas()
        adapter.vocabulary()
        adapter.verify()
        self.assertEqual([c["url"] for c in opener.calls], [
            "http://127.0.0.1:8791/health",
            "http://127.0.0.1:8791/schemas",
            "http://127.0.0.1:8791/vocabulary",
            "http://127.0.0.1:8791/verify",
        ])

    def test_token_is_sent_as_a_bearer_header(self):
        adapter, opener = _adapter([_ok_envelope()])
        adapter.step("file-manager")
        self.assertEqual(opener.calls[0]["headers"]["Authorization"],
                         "Bearer t0k")


class RefusalIsNotFailureTests(unittest.TestCase):
    """The centre of this module. Each of these is a 200."""

    def test_a_refusal_is_returned_not_raised(self):
        adapter, _ = _adapter([
            _ok_envelope(ok=False, stopped_at="agree",
                         reason="insufficient_agreement",
                         detail="1 of 3 slots answered")])
        env = adapter.step("file-manager")
        self.assertFalse(env["ok"])
        self.assertEqual(env["reason"], "insufficient_agreement")
        self.assertEqual(env["stopped_at"], "agree")

    def test_every_declared_reason_survives_the_boundary(self):
        for reason in STOP_REASONS:
            with self.subTest(reason=reason):
                adapter, _ = _adapter([_ok_envelope(ok=False, reason=reason)])
                self.assertEqual(adapter.step("file-manager")["reason"], reason)

    def test_a_refusal_is_not_retried_by_the_adapter(self):
        """One request in, one request out: the adapter must not treat a
        declined decision as worth re-asking."""
        adapter, opener = _adapter([
            _ok_envelope(ok=False, stopped_at="decide",
                         reason="decision_not_usable")])
        adapter.step("file-manager")
        self.assertEqual(len(opener.calls), 1)

    def test_a_refusal_still_reports_the_spend_it_caused(self):
        """A refusal is not free: the extraction and the decision were both
        paid for before the driver declined to act. An adapter that
        'helpfully' zeroed cost on a refusal would under-report the
        operator's bill for every declined step."""
        adapter, _ = _adapter([_ok_envelope(ok=False, stopped_at="decide",
                                           reason="confidence_below_threshold",
                                           cost_usd=0.0000642)])
        env = adapter.step("file-manager")
        self.assertFalse(env["ok"])
        self.assertEqual(env["cost_usd"], 0.0000642)

    def test_a_refusal_is_not_reported_as_a_success(self):
        adapter, _ = _adapter([_ok_envelope(ok=False, reason="no_capture")])
        self.assertFalse(adapter.step("file-manager")["ok"])


class TransportFailureTests(unittest.TestCase):
    def test_unreachable_service_raises_unavailable(self):
        adapter, _ = _adapter([urllib.error.URLError("connection refused")])
        with self.assertRaises(PerceptionUnavailable) as ctx:
            adapter.step("file-manager")
        self.assertIn("driver-core serve", str(ctx.exception))

    def test_an_http_error_is_unavailable_not_a_refusal(self):
        """The service answers 200 for every refusal, so an HTTP error means
        the request never became a decision at all."""
        adapter, _ = _adapter([_http_error(500, {"error": "boom"})])
        with self.assertRaises(PerceptionUnavailable):
            adapter.step("file-manager")

    def test_a_malformed_request_response_is_unavailable(self):
        adapter, _ = _adapter([_http_error(400, {"error": "target is required"})])
        with self.assertRaises(PerceptionUnavailable) as ctx:
            adapter.step("file-manager")
        self.assertIn("HTTP 400", str(ctx.exception))

    def test_non_json_bytes_are_unavailable_not_a_refusal(self):
        adapter, _ = _adapter([_RawResponse(b"<html>not json</html>")])
        with self.assertRaises(PerceptionUnavailable):
            adapter.step("file-manager")

    def test_an_http_error_with_a_non_json_body_still_names_the_code(self):
        """A proxy or an unrelated service can answer an error with HTML.
        Losing the status code there would leave the caller with no way to
        tell a 401 from a 500."""
        adapter, _ = _adapter([_http_error_raw(502, b"<html>bad gateway</html>")])
        with self.assertRaises(PerceptionUnavailable) as ctx:
            adapter.step("file-manager")
        self.assertIn("HTTP 502", str(ctx.exception))


class ContractViolationTests(unittest.TestCase):
    """A response that contradicts the service's documented shape is
    caught here rather than in every future caller."""

    def test_a_refusal_with_no_reason_is_refused_at_the_boundary(self):
        adapter, _ = _adapter([{"ok": False, "stopped_at": "decide"}])
        with self.assertRaises(PerceptionUnavailable) as ctx:
            adapter.step("file-manager")
        self.assertIn("no reason", str(ctx.exception))

    def test_an_undeclared_reason_is_loud_rather_than_coerced(self):
        """A new reason means the service moved ahead of this adapter.
        Folding it into a known bucket would hide exactly that."""
        adapter, _ = _adapter([_ok_envelope(ok=False, reason="meteor_strike")])
        with self.assertRaises(PerceptionUnavailable) as ctx:
            adapter.step("file-manager")
        self.assertIn("undeclared", str(ctx.exception))

    def test_a_missing_ok_is_a_contract_violation(self):
        adapter, _ = _adapter([{"step_id": "x", "stopped_at": "capture"}])
        with self.assertRaises(PerceptionUnavailable):
            adapter.step("file-manager")

    def test_a_non_object_response_is_refused(self):
        adapter, _ = _adapter([[1, 2, 3]])
        with self.assertRaises(PerceptionUnavailable) as ctx:
            adapter.step("file-manager")
        self.assertIn("JSON object", str(ctx.exception))

    def test_a_successful_response_is_not_inspected_for_reasons(self):
        """Success carries no reason; `_check_contract` must not invent a
        requirement that only refusals meet."""
        adapter, _ = _adapter([_ok_envelope()])
        self.assertTrue(adapter.step("file-manager")["ok"])


class ConsentIsNeverInventedTests(unittest.TestCase):
    """Consent binds to an action AND its params, and is the operator's to
    give. The adapter has no code path that synthesises one, so a step
    without consent is a read-only observation."""

    def test_no_consent_is_sent_when_none_was_given(self):
        adapter, opener = _adapter([_ok_envelope()])
        adapter.step("file-manager")
        self.assertNotIn("consent", opener.calls[0]["body"])

    def test_consent_is_passed_through_verbatim(self):
        consent = {"granted": True, "action": "open_window",
                   "params": {"path": "~/notes"}, "by": "operator"}
        adapter, opener = _adapter([_ok_envelope()])
        adapter.step("file-manager", consent=consent)
        self.assertEqual(opener.calls[0]["body"]["consent"], consent)

    def test_consent_params_are_never_defaulted(self):
        """An empty param set is a decision about those exact params. The
        adapter must pass what it was given, not what it thinks is safe."""
        adapter, opener = _adapter([_ok_envelope()])
        adapter.step("f", consent={"granted": True, "action": "delete",
                                   "params": {}, "by": "op"})
        self.assertEqual(opener.calls[0]["body"]["consent"]["params"], {})

    def test_prefer_and_stability_are_forwarded(self):
        adapter, opener = _adapter([_ok_envelope()])
        adapter.step("f", prefer=("dom", "cli"), require_stable=False)
        body = opener.calls[0]["body"]
        self.assertEqual(body["prefer"], ["dom", "cli"])
        self.assertFalse(body["require_stable"])


class EndpointResolutionTests(unittest.TestCase):
    def test_explicit_args_beat_environment(self):
        with mock.patch.dict(os.environ, {"DRIVER_BASE_URL": "http://env:1",
                                         "DRIVER_TOKEN": "env-tok"}):
            adapter = PerceptionAdapter(base_url="http://arg:2", token="arg-tok")
        self.assertEqual(adapter.base, "http://arg:2")
        self.assertEqual(adapter.token, "arg-tok")

    def test_a_trailing_slash_is_not_doubled(self):
        adapter = PerceptionAdapter(base_url="http://127.0.0.1:8791/",
                                    token="t")
        self.assertEqual(adapter.base, "http://127.0.0.1:8791")

    def test_config_file_is_consulted_and_env_is_the_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "driver.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"base_url": "http://cfg:3", "token": "cfg-tok"},
                          handle)
            with mock.patch.dict(os.environ, {"DRIVER_CONFIG_PATH": path}):
                adapter = PerceptionAdapter()
            self.assertEqual(adapter.base, "http://cfg:3")
            self.assertEqual(adapter.token, "cfg-tok")

    def test_config_token_falls_back_to_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "driver.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"base_url": "http://cfg:3"}, handle)
            with mock.patch.dict(os.environ, {"DRIVER_CONFIG_PATH": path,
                                              "DRIVER_TOKEN": "env-tok"}):
                adapter = PerceptionAdapter()
            self.assertEqual(adapter.token, "env-tok")

    def test_a_corrupt_config_file_falls_back_instead_of_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "driver.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            with mock.patch.dict(os.environ, {"DRIVER_CONFIG_PATH": path,
                                              "DRIVER_BASE_URL": "http://env:1"}):
                adapter = PerceptionAdapter()
            self.assertEqual(adapter.base, "http://env:1")

    def test_the_module_names_no_provider_brand(self):
        """Canon rule: no provider brands in phase code. The driver owns
        its models; this adapter resolves an endpoint and nothing else."""
        import harness.perception_client as mod
        source = pathlib.Path(mod.__file__).read_text(encoding="utf-8").lower()
        for brand in ("openrouter", "anthropic", "openai", "google",
                      "gemini", "claude", "deepseek", "glm", "ling-"):
            self.assertNotIn(brand, source,
                             "{} must not name a provider".format(brand))


class NonInterferenceTests(unittest.TestCase):
    """driver-core is configured entirely under ``DRIVER_*`` and reads
    nothing from this project's environment. This adapter is the only
    place that boundary could be crossed, so it is asserted here."""

    def test_the_adapter_reads_only_driver_namespaced_environment(self):
        import re
        import harness.perception_client as mod
        source = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
        for name in re.findall(r"os\.environ(?:\.get)?\(?\s*[\"']([^\"']+)",
                               source):
            self.assertTrue(
                name.startswith("DRIVER_"),
                "{} is outside the DRIVER_ namespace".format(name))

    def test_no_harness_state_file_is_written(self):
        adapter, _ = _adapter([_ok_envelope()])
        before = sorted(os.listdir("."))
        adapter.step("file-manager")
        self.assertEqual(sorted(os.listdir(".")), before)


class SummarizeTests(unittest.TestCase):
    def test_a_refusal_summarizes_to_the_tier_and_reason(self):
        text = PerceptionAdapter.summarize(
            {"ok": False, "stopped_at": "agree",
             "reason": "extraction_disagreement"})
        self.assertIn("agree", text)
        self.assertIn("extraction_disagreement", text)

    def test_a_success_summarizes_to_the_action_and_cost(self):
        text = PerceptionAdapter.summarize(_ok_envelope())
        self.assertIn("read_text", text)
        self.assertIn("0.000064", text)

    def test_summarize_survives_a_bare_envelope(self):
        self.assertIsInstance(PerceptionAdapter.summarize({"ok": True}), str)


class CliFaceTests(unittest.TestCase):
    def _run(self, argv, responses):
        out, err = io.StringIO(), io.StringIO()
        injected = _fake_opener(responses)

        def fake_init(self, base_url=None, token=None, timeout=120, opener=None):
            self.base = "http://127.0.0.1:8791"
            self.token = token or "t0k"
            self.timeout = timeout
            self._opener = opener or injected

        with mock.patch.object(PerceptionAdapter, "__init__", fake_init):
            with redirect_stdout(out), redirect_stderr(err):
                code = run_cli(argv)
        return code, out.getvalue(), err.getvalue(), injected

    def test_health_prints_json_and_exits_zero(self):
        payload = {"ok": True, "status": "up", "keyed": False}
        code, out, _, opener = self._run(["health"], [payload])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["status"], "up")
        self.assertTrue(opener.calls[0]["url"].endswith("/health"))

    def test_a_refused_step_exits_one_not_zero(self):
        code, out, _, _ = self._run(
            ["step", "file-manager"],
            [_ok_envelope(ok=False, stopped_at="decide",
                          reason="confidence_below_threshold",
                          detail="0.42 < 0.70")])
        self.assertEqual(code, 1)
        self.assertIn("confidence_below_threshold", out)
        self.assertIn("0.42 < 0.70", out)

    def test_a_successful_step_exits_zero(self):
        code, out, _, _ = self._run(["step", "file-manager"], [_ok_envelope()])
        self.assertEqual(code, 0)
        self.assertIn("read_text", out)

    def test_an_unreachable_service_exits_four_with_a_defer_message(self):
        code, _, err, _ = self._run(["health"],
                                    [urllib.error.URLError("refused")])
        self.assertEqual(code, 4)
        self.assertIn("[defer]", err)

    def test_a_step_without_an_action_sends_no_consent(self):
        _, _, _, opener = self._run(["step", "file-manager"], [_ok_envelope()])
        self.assertNotIn("consent", opener.calls[0]["body"])

    def test_a_step_with_an_action_sends_that_consent(self):
        _, _, _, opener = self._run(
            ["step", "file-manager", "--action", "open_window",
             "--params", '{"path": "~/notes"}', "--by", "operator"],
            [_ok_envelope()])
        self.assertEqual(opener.calls[0]["body"]["consent"], {
            "granted": True, "action": "open_window",
            "params": {"path": "~/notes"}, "by": "operator"})

    def test_bad_params_json_is_refused_before_any_request(self):
        code, _, err, opener = self._run(
            ["step", "f", "--action", "open_window", "--params", "{oops"],
            [_ok_envelope()])
        self.assertEqual(code, 2)
        self.assertIn("--params is not JSON", err)
        self.assertEqual(opener.calls, [],
                         "a malformed --params must not reach the service")

    def test_params_that_are_not_an_object_are_refused(self):
        code, _, err, opener = self._run(
            ["step", "f", "--action", "click", "--params", '["a"]'],
            [_ok_envelope()])
        self.assertEqual(code, 2)
        self.assertIn("must be a JSON object", err)
        self.assertEqual(opener.calls, [])

    def test_raw_prints_the_whole_envelope(self):
        code, out, _, _ = self._run(["step", "f", "--raw"], [_ok_envelope()])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["step_id"], "abc123")

    def test_verify_reports_a_broken_chain_with_a_nonzero_exit(self):
        code, out, _, _ = self._run(
            ["verify"], [{"ok": True, "audit": {"ok": False,
                                                "reason": "chain_altered"}}])
        self.assertEqual(code, 1)
        self.assertIn("chain_altered", out)

    def test_verify_exits_zero_on_a_clean_chain(self):
        code, out, _, _ = self._run(
            ["verify"], [{"ok": True,
                          "audit": {"ok": True, "records": 3},
                          "budget": {"spent_usd": 0.0000642}}])
        self.assertEqual(code, 0)
        self.assertIn("records", out)

    def test_schemas_prints_the_declared_schemas(self):
        code, out, _, opener = self._run(["schemas"],
                                         [{"ok": True,
                                           "schemas": [{"name": "screen"}]}])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["schemas"][0]["name"], "screen")
        self.assertTrue(opener.calls[0]["url"].endswith("/schemas"))

    def test_vocabulary_prints_the_closed_action_vocabulary(self):
        code, out, _, opener = self._run(
            ["vocabulary"], [{"ok": True, "vocabulary": {"actions": []}}])
        self.assertEqual(code, 0)
        self.assertIn("vocabulary", out)
        self.assertTrue(opener.calls[0]["url"].endswith("/vocabulary"))

    def test_allow_unstable_inverts_require_stable(self):
        _, _, _, opener = self._run(["step", "f", "--allow-unstable"],
                                    [_ok_envelope()])
        self.assertFalse(opener.calls[0]["body"]["require_stable"])


if __name__ == "__main__":
    unittest.main()
