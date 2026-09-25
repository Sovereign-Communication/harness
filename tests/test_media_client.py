"""MEDIA-1: hermetic coverage for harness.media_client.

No network or provider keys involved: `MediaAdapter` takes an injectable
`opener` seam, and every test here supplies a fake transport. Covers the
success path, an honest `MediaUnavailable` failure (service unreachable),
a budget-refusal envelope, and the `harness media ...` CLI face (including
its exit codes and the defer-style stderr message on failure).
"""
import io
import json
import os
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

from harness.media_client import MediaAdapter, MediaUnavailable, run_cli


class _FakeResponse:
    """Minimal stand-in for the object urllib.request.urlopen's context
    manager yields: a `.read()` returning bytes, usable as a `with` target.
    """

    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_opener(responses):
    """Build an opener(req, timeout=...) that pops queued responses in order.

    Each queued item is either a dict (returned as a 200 JSON body) or an
    exception instance/class to raise (e.g. urllib.error.HTTPError or
    urllib.error.URLError), so tests can script multi-step interactions
    (POST job -> GET job while running -> GET job succeeded) deterministically.
    """
    calls = []

    def opener(req, timeout=None):
        calls.append((req.get_method(), req.full_url))
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, type) and issubclass(item, Exception):
            raise item()
        return _FakeResponse(item)

    opener.calls = calls
    return opener


def _http_error(code, payload):
    body = json.dumps(payload).encode("utf-8")
    return urllib.error.HTTPError(
        url="http://example.invalid", code=code, msg="err",
        hdrs=None, fp=io.BytesIO(body),
    )


class MediaAdapterSuccessTests(unittest.TestCase):
    def test_image_success_wait(self):
        job = {
            "id": "job-1", "status": "queued", "kind": "image",
        }
        done = {
            "id": "job-1", "status": "succeeded", "kind": "image",
            "provider": "acme", "model": "gen-1",
            "cost_estimate": {"low": 0.01, "high": 0.02},
            "cost_actual": 0.015, "artifact_paths": ["/tmp/out.png"],
            "project": "demo",
        }
        opener = _fake_opener([job, done])
        adapter = MediaAdapter(base_url="http://media.local", opener=opener)
        env = adapter.image("a cabin in snowy woods", project="demo")

        self.assertEqual(env["status"], "succeeded")
        self.assertEqual(env["job_id"], "job-1")
        self.assertEqual(env["cost"], 0.015)
        self.assertEqual(env["artifacts"], ["/tmp/out.png"])
        # POST to create, then GET to poll once.
        self.assertEqual(opener.calls[0][0], "POST")
        self.assertEqual(opener.calls[1][0], "GET")

    def test_no_wait_returns_job_envelope_unresolved(self):
        job = {"id": "job-2", "status": "queued", "kind": "video"}
        opener = _fake_opener([job])
        adapter = MediaAdapter(base_url="http://media.local", opener=opener)
        result = adapter.video("ocean waves", project="demo", wait=False)
        self.assertEqual(result, job)
        self.assertEqual(len(opener.calls), 1)

    def test_balance(self):
        opener = _fake_opener([{"balance": 4.2, "project": "demo"}])
        adapter = MediaAdapter(base_url="http://media.local", opener=opener)
        result = adapter.balance(project="demo")
        self.assertEqual(result["balance"], 4.2)


class MediaAdapterFailureTests(unittest.TestCase):
    def test_service_unavailable_raises_media_unavailable(self):
        opener = _fake_opener([urllib.error.URLError("connection refused")])
        adapter = MediaAdapter(base_url="http://media.local", opener=opener)
        with self.assertRaises(MediaUnavailable) as ctx:
            adapter.image("prompt", project="demo")
        self.assertIn("media.local", str(ctx.exception))

    def test_budget_refused_returns_envelope_not_exception(self):
        err = _http_error(402, {
            "error": "budget_refused",
            "message": "over per-call ceiling",
            "math": {"ceiling": 0.5, "estimate": 0.56},
        })
        opener = _fake_opener([err])
        adapter = MediaAdapter(base_url="http://media.local", opener=opener)
        result = adapter.image("prompt", project="demo")
        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["error"], "over per-call ceiling")
        self.assertEqual(result["math"]["ceiling"], 0.5)

    def test_timeout_when_job_never_settles(self):
        job = {"id": "job-3", "status": "queued", "kind": "image"}
        running = {"id": "job-3", "status": "running", "kind": "image"}
        opener = _fake_opener([job, running])
        adapter = MediaAdapter(base_url="http://media.local", opener=opener)
        with mock.patch("time.time", side_effect=[0, 0, 100]):
            with mock.patch("time.sleep"):
                result = adapter.wait("job-3", timeout=1.0, interval=0.1)
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["job_id"], "job-3")


class MediaCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Isolate from any real ~/.config/harness/media.json on the host.
        self._env_patch = mock.patch.dict(os.environ, {
            "MEDIA_CONFIG_PATH": os.path.join(self.tmp.name, "media.json"),
            "MEDIA_BASE_URL": "http://media.local",
        }, clear=False)
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)

    def test_cli_image_success_exit_zero(self):
        job = {"id": "job-1", "status": "queued", "kind": "image"}
        done = {
            "id": "job-1", "status": "succeeded", "kind": "image",
            "provider": "acme", "model": "gen-1", "cost_actual": 0.01,
            "artifact_paths": ["/tmp/out.png"], "project": "demo",
        }
        opener = _fake_opener([job, done])
        out = io.StringIO()
        with mock.patch("harness.media_client.MediaAdapter",
                        lambda *a, **k: MediaAdapter(opener=opener)):
            with redirect_stdout(out):
                code = run_cli(["image", "a cabin", "--project", "demo"])
        self.assertEqual(code, 0)
        self.assertIn("succeeded", out.getvalue())
        self.assertIn("/tmp/out.png", out.getvalue())

    def test_cli_service_unavailable_exits_four_and_defers(self):
        opener = _fake_opener([urllib.error.URLError("refused")])
        err = io.StringIO()
        with mock.patch("harness.media_client.MediaAdapter",
                        lambda *a, **k: MediaAdapter(opener=opener)):
            with redirect_stderr(err):
                code = run_cli(["image", "a cabin", "--project", "demo"])
        self.assertEqual(code, 4)
        self.assertIn("[defer] media:", err.getvalue())

    def test_cli_budget_refused_exits_nonzero_and_shows_math(self):
        e = _http_error(402, {
            "error": "budget_refused", "message": "over ceiling",
            "math": {"ceiling": 0.5},
        })
        opener = _fake_opener([e])
        out = io.StringIO()
        with mock.patch("harness.media_client.MediaAdapter",
                        lambda *a, **k: MediaAdapter(opener=opener)):
            with redirect_stdout(out):
                code = run_cli(["video", "ocean waves", "--project", "demo"])
        self.assertEqual(code, 1)
        self.assertIn("refused", out.getvalue())
        self.assertIn("ceiling", out.getvalue())

    def test_cli_provider_and_model_are_free_strings_not_choices(self):
        # No hardcoded provider ladder in argparse: an arbitrary provider
        # name must parse without argparse rejecting it.
        job = {"id": "job-9", "status": "queued", "kind": "image"}
        done = {
            "id": "job-9", "status": "succeeded", "kind": "image",
            "provider": "some-new-provider", "model": "whatever",
            "cost_actual": 0.0, "artifact_paths": [], "project": "demo",
        }
        opener = _fake_opener([job, done])
        out = io.StringIO()
        with mock.patch("harness.media_client.MediaAdapter",
                        lambda *a, **k: MediaAdapter(opener=opener)):
            with redirect_stdout(out):
                code = run_cli([
                    "image", "prompt", "--project", "demo",
                    "--provider", "some-new-provider", "--model", "whatever",
                ])
        self.assertEqual(code, 0)

    def test_cli_dispatch_from_harness_cli_main(self):
        # Confirms harness.cli intercepts "media" before argparse's main
        # build_parser (mirrors the serve/desktop early-intercept pattern).
        from harness import cli as harness_cli
        with mock.patch("harness.media_client.run_cli", return_value=0) as m:
            code = harness_cli.main(["media", "balance", "--project", "demo"])
        self.assertEqual(code, 0)
        m.assert_called_once_with(["balance", "--project", "demo"])


if __name__ == "__main__":
    unittest.main()
