"""harness.media_client: thin adapter to the sovereign-media service.

Hermetic: the only network seam is ``urllib.request.urlopen``, patched in
every test below (or ``MediaAdapter._request``/``MediaAdapter`` itself for
tests that only care about dispatch). No live network, no key, no filesystem
writes outside a temp dir.
"""
import json
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

from harness import media_client
from harness.media_client import MediaAdapter, MediaUnavailable, run_cli


def _http_response(payload):
    body = json.dumps(payload).encode("utf-8")
    resp = mock.MagicMock()
    resp.read.return_value = body
    resp.__enter__ = mock.MagicMock(return_value=resp)
    resp.__exit__ = mock.MagicMock(return_value=False)
    return resp


class LoadEndpointTests(unittest.TestCase):
    def test_default_endpoint_with_no_config_file(self):
        with mock.patch.object(media_client, "CONFIG_PATH", "/nonexistent/media.json"), \
             mock.patch.dict(os.environ, {}, clear=True):
            base, token = media_client._load_endpoint()
        self.assertEqual(base, media_client.DEFAULT_BASE)
        self.assertIsNone(token)

    def test_env_token_used_when_no_config_file(self):
        with mock.patch.object(media_client, "CONFIG_PATH", "/nonexistent/media.json"), \
             mock.patch.dict(os.environ, {"MEDIA_TOKEN": "envtok"}, clear=True):
            base, token = media_client._load_endpoint()
        self.assertEqual(base, media_client.DEFAULT_BASE)
        self.assertEqual(token, "envtok")

    def test_config_file_overrides_base_and_token(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = os.path.join(d, "media.json")
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump({"base_url": "http://example.test:9", "token": "cfgtok"}, f)
            with mock.patch.object(media_client, "CONFIG_PATH", cfg_path):
                base, token = media_client._load_endpoint()
        self.assertEqual(base, "http://example.test:9")
        self.assertEqual(token, "cfgtok")

    def test_malformed_config_file_falls_back_to_default(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = os.path.join(d, "media.json")
            with open(cfg_path, "w", encoding="utf-8") as f:
                f.write("{not json")
            with mock.patch.object(media_client, "CONFIG_PATH", cfg_path), \
                 mock.patch.dict(os.environ, {}, clear=True):
                base, token = media_client._load_endpoint()
        self.assertEqual(base, media_client.DEFAULT_BASE)
        self.assertIsNone(token)


class MediaAdapterInitTests(unittest.TestCase):
    def test_explicit_base_and_token_win_over_endpoint(self):
        with mock.patch.object(media_client, "_load_endpoint",
                               return_value=("http://cfg", "cfgtok")):
            adapter = MediaAdapter(base_url="http://explicit/", token="explicit-tok")
        self.assertEqual(adapter.base, "http://explicit")  # trailing slash stripped
        self.assertEqual(adapter.token, "explicit-tok")

    def test_falls_back_to_loaded_endpoint(self):
        with mock.patch.object(media_client, "_load_endpoint",
                               return_value=("http://cfg", "cfgtok")):
            adapter = MediaAdapter()
        self.assertEqual(adapter.base, "http://cfg")
        self.assertEqual(adapter.token, "cfgtok")


class RequestTransportTests(unittest.TestCase):
    """The one urllib seam: success, budget refusal, hard HTTP error, and
    unreachable-service all route through MediaAdapter._request."""

    def _adapter(self):
        with mock.patch.object(media_client, "_load_endpoint",
                               return_value=("http://svc", "tok")):
            return MediaAdapter()

    def test_success_returns_decoded_json(self):
        adapter = self._adapter()
        with mock.patch.object(media_client.urllib.request, "urlopen",
                               return_value=_http_response({"id": "j1"})):
            result = adapter._request("GET", "/v1/jobs/j1")
        self.assertEqual(result, {"id": "j1"})

    def test_budget_refused_returns_refusal_envelope_not_raise(self):
        adapter = self._adapter()
        err_body = json.dumps({"error": "budget_refused", "message": "over ceiling",
                               "math": {"max": 1}}).encode()
        http_err = urllib.error.HTTPError("http://svc/x", 402, "refused", {}, None)
        http_err.read = mock.MagicMock(return_value=err_body)
        with mock.patch.object(media_client.urllib.request, "urlopen", side_effect=http_err):
            result = adapter._request("POST", "/v1/jobs", {"kind": "image"})
        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["error"], "over ceiling")
        self.assertEqual(result["math"], {"max": 1})

    def test_http_error_without_budget_refusal_raises_media_unavailable(self):
        adapter = self._adapter()
        http_err = urllib.error.HTTPError("http://svc/x", 500, "boom", {}, None)
        http_err.read = mock.MagicMock(return_value=b"not json")
        with mock.patch.object(media_client.urllib.request, "urlopen", side_effect=http_err):
            with self.assertRaises(MediaUnavailable):
                adapter._request("GET", "/v1/jobs/j1")

    def test_unreachable_service_raises_media_unavailable(self):
        adapter = self._adapter()
        with mock.patch.object(media_client.urllib.request, "urlopen",
                               side_effect=urllib.error.URLError("refused")):
            with self.assertRaisesRegex(MediaUnavailable, "media serve"):
                adapter._request("GET", "/v1/jobs/j1")


class GenerateAndWaitTests(unittest.TestCase):
    def _adapter(self):
        with mock.patch.object(media_client, "_load_endpoint",
                               return_value=("http://svc", None)):
            return MediaAdapter()

    def test_image_no_wait_returns_job_envelope_unwrapped(self):
        adapter = self._adapter()
        with mock.patch.object(adapter, "_request",
                               return_value={"id": "j1", "status": "queued"}) as req:
            result = adapter.image("a cat", project="p", wait=False)
        self.assertEqual(result, {"id": "j1", "status": "queued"})
        req.assert_called_once_with("POST", "/v1/jobs", {
            "kind": "image", "prompt": "a cat", "project": "p", "params": {},
        })

    def test_generate_short_circuits_on_budget_refusal_even_when_waiting(self):
        adapter = self._adapter()
        refusal = {"status": "refused", "error": "over ceiling"}
        with mock.patch.object(adapter, "_request", return_value=refusal), \
             mock.patch.object(adapter, "wait") as fake_wait:
            result = adapter.video("a dog", wait=True)
        self.assertEqual(result, refusal)
        fake_wait.assert_not_called()

    def test_generate_waits_for_terminal_status(self):
        adapter = self._adapter()
        with mock.patch.object(adapter, "_request",
                               return_value={"id": "j2", "status": "queued"}), \
             mock.patch.object(adapter, "wait", return_value={"status": "succeeded"}) as fake_wait:
            result = adapter.image("a cat", wait=True, timeout=5)
        fake_wait.assert_called_once_with("j2", timeout=5)
        self.assertEqual(result, {"status": "succeeded"})

    def test_wait_polls_until_terminal_status(self):
        adapter = self._adapter()
        calls = [{"status": "running"}, {"status": "succeeded", "id": "j3"}]
        with mock.patch.object(adapter, "_request", side_effect=calls), \
             mock.patch("time.sleep"):
            result = adapter.wait("j3", timeout=10, interval=0)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["job_id"], "j3")

    def test_wait_times_out_when_never_terminal(self):
        adapter = self._adapter()
        with mock.patch.object(adapter, "_request", return_value={"status": "running"}), \
             mock.patch("time.time", side_effect=[0, 1, 100]), \
             mock.patch("time.sleep"):
            result = adapter.wait("j4", timeout=5, interval=0)
        self.assertEqual(result, {"status": "timeout", "job_id": "j4"})


class JobsAndBalanceTests(unittest.TestCase):
    def _adapter(self):
        with mock.patch.object(media_client, "_load_endpoint",
                               return_value=("http://svc", None)):
            return MediaAdapter()

    def test_job_wraps_single_envelope(self):
        adapter = self._adapter()
        raw = {"id": "j1", "status": "succeeded", "kind": "image",
               "artifact_paths": ["/tmp/a.png"]}
        with mock.patch.object(adapter, "_request", return_value=raw) as req:
            env = adapter.job("j1")
        req.assert_called_once_with("GET", "/v1/jobs/j1")
        self.assertEqual(env["job_id"], "j1")
        self.assertEqual(env["artifacts"], ["/tmp/a.png"])

    def test_jobs_lists_and_quotes_project(self):
        adapter = self._adapter()
        with mock.patch.object(adapter, "_request",
                               return_value={"jobs": [{"id": "a"}, {"id": "b"}]}) as req:
            envs = adapter.jobs(project="my project", limit=5)
        self.assertEqual([e["job_id"] for e in envs], ["a", "b"])
        called_path = req.call_args[0][1]
        self.assertIn("limit=5", called_path)
        self.assertIn("project=my%20project", called_path)

    def test_jobs_without_project_omits_filter(self):
        adapter = self._adapter()
        with mock.patch.object(adapter, "_request", return_value={"jobs": []}) as req:
            adapter.jobs()
        self.assertNotIn("project=", req.call_args[0][1])

    def test_balance_with_and_without_project(self):
        adapter = self._adapter()
        with mock.patch.object(adapter, "_request", return_value={"usd": 1.0}) as req:
            adapter.balance("proj")
        self.assertEqual(req.call_args[0][1], "/v1/balance?project=proj")
        with mock.patch.object(adapter, "_request", return_value={"usd": 1.0}) as req:
            adapter.balance()
        self.assertEqual(req.call_args[0][1], "/v1/balance")

    def test_envelope_defaults_missing_artifacts_to_empty_list(self):
        env = MediaAdapter._envelope({"id": "j9", "status": "failed"})
        self.assertEqual(env["artifacts"], [])
        self.assertIsNone(env["error"])


class RunCliTests(unittest.TestCase):
    """`harness media ...` -- one hermetic path per subcommand, plus the
    honest-defer exit on an unreachable service."""

    def _patched_adapter(self, **method_returns):
        fake = mock.MagicMock()
        for name, value in method_returns.items():
            getattr(fake, name).return_value = value
        return mock.patch.object(media_client, "MediaAdapter", return_value=fake), fake

    def test_image_success_exits_zero(self):
        patcher, fake = self._patched_adapter(
            image={"status": "succeeded", "job_id": "j1", "cost": 0.01, "artifacts": []})
        with patcher:
            rc = run_cli(["image", "a cabin", "--project", "p"])
        self.assertEqual(rc, 0)
        fake.image.assert_called_once()
        self.assertEqual(fake.image.call_args.kwargs["project"], "p")

    def test_image_non_succeeded_status_exits_one(self):
        patcher, _fake = self._patched_adapter(
            image={"status": "failed", "job_id": "j1", "error": "boom"})
        with patcher:
            rc = run_cli(["image", "a cabin"])
        self.assertEqual(rc, 1)

    def test_video_dispatch_passes_seconds_param(self):
        patcher, fake = self._patched_adapter(
            video={"status": "succeeded", "job_id": "j2"})
        with patcher:
            rc = run_cli(["video", "a dog running", "--seconds", "8"])
        self.assertEqual(rc, 0)
        self.assertEqual(fake.video.call_args.kwargs["seconds"], 8)

    def test_job_dispatch(self):
        patcher, fake = self._patched_adapter(job={"status": "succeeded", "job_id": "j3"})
        with patcher:
            rc = run_cli(["job", "j3"])
        self.assertEqual(rc, 0)
        fake.job.assert_called_once_with("j3")

    def test_jobs_dispatch_prints_each_envelope(self):
        patcher, fake = self._patched_adapter(
            jobs=[{"status": "succeeded", "job_id": "a"},
                  {"status": "failed", "job_id": "b", "error": "x"}])
        with patcher:
            rc = run_cli(["jobs", "--limit", "2"])
        self.assertEqual(rc, 0)
        fake.jobs.assert_called_once_with(project=None, limit=2)

    def test_balance_dispatch_prints_json(self):
        patcher, fake = self._patched_adapter(balance={"usd": 3.5})
        with patcher:
            rc = run_cli(["balance", "--project", "p"])
        self.assertEqual(rc, 0)
        fake.balance.assert_called_once_with("p")

    def test_media_unavailable_is_an_honest_defer(self):
        fake = mock.MagicMock()
        fake.image.side_effect = MediaUnavailable("service down")
        with mock.patch.object(media_client, "MediaAdapter", return_value=fake):
            rc = run_cli(["image", "a cabin"])
        self.assertEqual(rc, 4)


if __name__ == "__main__":
    unittest.main()
