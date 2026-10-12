"""Hermetic D12 coverage: reapable HTTP child protocol + transport (#198).

``cancellable_request`` runs the real worker in a child process, so error
paths are pinned with a scriptable fake child (no network, no sleeps).
``harness/_http_worker.py`` itself runs in a subprocess in production --
invisible to the parent's trace -- so it is imported in-process and
``main()`` is driven with mocked stdio (plus one ``runpy`` pass with
``run_name="__main__"`` for the entry-point line).
"""
import base64
import io
import json
import sys
import unittest
from unittest import mock

from harness import osal
from harness.errors import ProviderUsageUnknown, ToolCancelled


class _FakeChild:
    """Scriptable stand-in for the reapable HTTP child process."""

    def __init__(self, stdout=b"", stderr=b"", returncode=0, polls=None,
                 communicate_exc=None):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self._polls = list(polls) if polls is not None else []
        self.communicate_exc = communicate_exc

    def communicate(self, input=None):  # noqa: A002 - mirrors Popen API
        if self.communicate_exc is not None:
            raise self.communicate_exc
        return (self._stdout, self._stderr)

    def poll(self):
        if self._polls:
            self.returncode = self._polls.pop(0)
        return self.returncode


def _child_result(status=200, body=b'{"ok": true}',
                  retry_after=None, final_url="http://127.0.0.1/"):
    return {"status": status,
            "body_b64": base64.b64encode(body).decode("ascii"),
            "retry_after": retry_after, "final_url": final_url}


class TestHttpWorkerCover(unittest.TestCase):
    def _run_main(self, payload, opener=None):
        import harness._http_worker as worker

        stdin = mock.Mock()
        stdin.buffer.read.return_value = json.dumps(payload).encode("utf-8")
        stdout = io.StringIO()
        with mock.patch.object(worker.sys, "stdin", stdin), \
                mock.patch.object(worker.sys, "stdout", stdout):
            if opener is not None:
                with mock.patch.object(worker.urllib.request,
                                       "build_opener",
                                       return_value=opener):
                    worker.main()
            else:
                worker.main()
        return json.loads(stdout.getvalue())

    def _payload(self, **over):
        body = {"url": "http://127.0.0.1:1/", "timeout": 5}
        body.update(over)
        return body

    def test_main_reports_transport_error(self):
        result = self._run_main(self._payload())
        self.assertIn("exception", result)
        self.assertEqual(result["exception"]["type"], "URLError")

    def test_main_no_redirect_and_max_bytes(self):
        seen = {}

        class FakeResp:
            headers = {"Retry-After": "2"}
            _url = "http://127.0.0.1/final"

            def getcode(self):
                return 200

            def read(self, n=None):
                seen["n"] = n
                data = b"0123456789"
                return data if n is None else data[:n]

            def geturl(self):
                return self._url

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        opener = mock.Mock()
        opener.open.return_value = FakeResp()
        result = self._run_main(
            self._payload(no_redirect=True, max_bytes=4,
                          headers={"X-Test": "1"}, data_b64="aGk=",
                          method="POST"),
            opener=opener)
        self.assertEqual(result["status"], 200)
        self.assertEqual(base64.b64decode(result["body_b64"]), b"0123")
        self.assertEqual(seen["n"], 4)
        self.assertEqual(result["final_url"], "http://127.0.0.1/final")

    def test_main_http_error_is_a_normal_result(self):
        import urllib.error

        opener = mock.Mock()
        err = urllib.error.HTTPError(
            "http://127.0.0.1/", 429, "limited", {"Retry-After": "7"},
            io.BytesIO(b"slow"))
        opener.open.side_effect = err
        result = self._run_main(self._payload(), opener=opener)
        self.assertEqual(result["status"], 429)
        self.assertEqual(base64.b64decode(result["body_b64"]), b"slow")
        self.assertEqual(result["retry_after"], "7")

    def test_main_as_script_entry(self):
        import runpy
        from pathlib import Path as _Path

        path = str(_Path("harness", "_http_worker.py"))
        stdin = mock.Mock()
        stdin.buffer.read.return_value = json.dumps(
            self._payload()).encode("utf-8")
        stdout = io.StringIO()
        with mock.patch.object(sys, "stdin", stdin), \
                mock.patch.object(sys, "stdout", stdout):
            runpy.run_path(path, run_name="__main__")
        result = json.loads(stdout.getvalue())
        self.assertIn("exception", result)


class TestCancellableRequestCover(unittest.TestCase):
    def test_validation_requires_a_check(self):
        from harness._http import cancellable_request

        with self.assertRaisesRegex(ValueError, "cancellation check"):
            cancellable_request("GET", "http://127.0.0.1/")

    def test_pre_cancelled_raises(self):
        from harness._http import cancellable_request

        with self.assertRaises(ToolCancelled):
            cancellable_request("GET", "http://127.0.0.1/",
                                cancel_check=lambda: True)

    def test_child_communication_failure(self):
        from harness._http import cancellable_request

        child = _FakeChild(communicate_exc=RuntimeError("pipe gone"),
                           polls=[0])
        with mock.patch.object(osal, "spawn_piped_process",
                               return_value=child):
            with self.assertRaisesRegex(OSError, "communication failed"):
                cancellable_request("GET", "http://127.0.0.1/",
                                    cancel_check=lambda: False)

    def test_child_invalid_response(self):
        from harness._http import cancellable_request

        child = _FakeChild(stdout=b"\xff not json",
                           stderr=b"traceback here", polls=[0])
        with mock.patch.object(osal, "spawn_piped_process",
                               return_value=child):
            with self.assertRaisesRegex(OSError, "invalid response"):
                cancellable_request("GET", "http://127.0.0.1/",
                                    cancel_check=lambda: False)

    def test_child_nonzero_exit(self):
        from harness._http import cancellable_request

        child = _FakeChild(stdout=json.dumps(_child_result()).encode(),
                           polls=[None, 1], returncode=1)
        with mock.patch.object(osal, "spawn_piped_process",
                               return_value=child):
            with self.assertRaisesRegex(OSError, "exited with status 1"):
                cancellable_request("GET", "http://127.0.0.1/",
                                    cancel_check=lambda: False)

    def test_child_result_exception(self):
        from harness._http import cancellable_request

        payload = {"exception": {"type": "URLError", "message": "refused"}}
        child = _FakeChild(stdout=json.dumps(payload).encode(), polls=[0])
        with mock.patch.object(osal, "spawn_piped_process",
                               return_value=child):
            with self.assertRaisesRegex(OSError, "URLError"):
                cancellable_request("GET", "http://127.0.0.1/",
                                    cancel_check=lambda: False)

    def test_child_invalid_body(self):
        from harness._http import cancellable_request

        payload = {"status": 200, "body_b64": "!!!",
                   "final_url": "http://127.0.0.1/"}
        child = _FakeChild(stdout=json.dumps(payload).encode(), polls=[0])
        with mock.patch.object(osal, "spawn_piped_process",
                               return_value=child):
            with self.assertRaisesRegex(OSError, "invalid body"):
                cancellable_request("GET", "http://127.0.0.1/",
                                    cancel_check=lambda: False)

    def test_stuck_child_pipes_raise_and_reap(self):
        from harness import _http as http_mod
        from harness._http import cancellable_request

        child = _FakeChild(polls=[None, None, None])

        class StuckThread:
            def __init__(self, target=None, name=None, daemon=None):
                self.target = target

            def start(self):
                pass

            def is_alive(self):
                return True

            def join(self, timeout=None):
                pass

        terminated = []
        killed = []
        checks = []

        def staged_check():
            checks.append(1)
            return len(checks) > 1

        with mock.patch.object(http_mod.threading, "Thread", StuckThread), \
                mock.patch.object(osal, "spawn_piped_process",
                                  return_value=child), \
                mock.patch.object(
                    osal, "terminate_and_reap",
                    side_effect=lambda c: terminated.append(c)), \
                mock.patch.object(
                    osal, "kill_and_reap",
                    side_effect=lambda c: killed.append(c)):
            with self.assertRaisesRegex(OSError, "did not release its pipes"):
                cancellable_request("GET", "http://127.0.0.1/",
                                    cancel_check=staged_check)
        self.assertEqual(terminated, [child])
        self.assertEqual(killed, [child])

    def test_interruptible_sleep_completes(self):
        from harness._http import interruptible_sleep

        interruptible_sleep(0.02, lambda: False)
        interruptible_sleep(0.0, lambda: False)


class _StubDelegate:
    """Minimal transport double for the cancellation-bound view."""

    def __init__(self):
        self.calls = []

    def get(self, *args, **kwargs):
        self.calls.append(("get", args, kwargs))
        return ("got", args)

    def post(self, *args, **kwargs):
        self.calls.append(("post", args, kwargs))
        return ("posted", args)

    def post_once(self, *args, **kwargs):
        self.calls.append(("post_once", args, kwargs))
        return ("once", args)


class TestBoundTransportCover(unittest.TestCase):
    def test_bound_view_delegates_calls(self):
        from harness._http import HttpTransport

        delegate = _StubDelegate()
        view = HttpTransport().with_cancel(lambda: False)
        view._delegate = delegate
        self.assertEqual(view.get("u", "k")[0], "got")
        self.assertEqual(view.post_once("u", "k", {})[0], "once")
        self.assertEqual([c[0] for c in delegate.calls],
                         ["get", "post_once"])


class _CannedResp:
    """Context-manager response double for urlopen/opener patching."""

    def __init__(self, code=200, body=b"{}", headers=None):
        self._code = code
        self._body = body
        self.headers = headers

    def getcode(self):
        return self._code

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(url, code, body, headers=None):
    import urllib.error
    return urllib.error.HTTPError(url, code, "err", headers,
                                  io.BytesIO(body))


class TestHttpGetCover(unittest.TestCase):
    def _wire(self, check):
        from harness._http import HttpTransport
        return HttpTransport(cancel_check=check)

    def test_pre_cancelled_get_raises(self):
        with self.assertRaises(ToolCancelled):
            self._wire(lambda: True).get("http://127.0.0.1/", "k")

    def test_cancellable_get_retries_transient(self):
        wire = self._wire(lambda: False)
        calls = []

        def canned(method, url, api_key, payload, timeout, check):
            calls.append(1)
            if len(calls) == 1:
                return 500, "busy", None
            return 200, '{"ok": true}', None

        with mock.patch.object(wire, "_request_once_cancellable",
                               side_effect=canned), \
                mock.patch.object(wire, "_retry_wait") as waited:
            self.assertEqual(wire.get("http://127.0.0.1/", "k"),
                             {"ok": True})
        self.assertEqual(len(calls), 2)
        self.assertEqual(waited.call_count, 1)

    def test_cancellable_get_non_json_body(self):
        wire = self._wire(lambda: False)
        with mock.patch.object(
                wire, "_request_once_cancellable",
                return_value=(200, "not-json", None)):
            self.assertEqual(wire.get("http://127.0.0.1/", "k"),
                             {"error": {"message": "not-json"}})

    def test_legacy_get_retries_http_error(self):
        import urllib.request
        wire = self._wire(None)
        with mock.patch.object(urllib.request, "urlopen") as opened, \
                mock.patch.object(wire, "_retry_wait") as waited:
            opened.side_effect = [
                _http_error("http://127.0.0.1/", 500, b"oops"),
                _CannedResp(200, b'{"ok": 1}')]
            self.assertEqual(wire.get("http://127.0.0.1/", "k"),
                             {"ok": 1})
        self.assertEqual(waited.call_count, 1)

    def test_legacy_get_returns_http_error_body(self):
        import urllib.request
        wire = self._wire(None)
        with mock.patch.object(urllib.request, "urlopen") as opened:
            opened.side_effect = _http_error(
                "http://127.0.0.1/", 400, b'{"error": "no"}')
            status = wire.get("http://127.0.0.1/", "k")
        self.assertEqual(status, {"error": "no"})

    def test_legacy_get_retries_url_error(self):
        import urllib.error
        import urllib.request
        wire = self._wire(None)
        with mock.patch.object(urllib.request, "urlopen") as opened, \
                mock.patch.object(wire, "_retry_wait") as waited:
            opened.side_effect = [
                urllib.error.URLError("down"),
                _CannedResp(200, b'{"ok": 2}')]
            self.assertEqual(wire.get("http://127.0.0.1/", "k"),
                             {"ok": 2})
        self.assertEqual(waited.call_count, 1)


class TestHttpCostFnsCover(unittest.TestCase):
    def test_retry_cost_of_invalid(self):
        from harness._http import HttpTransport
        self.assertEqual(HttpTransport._retry_cost_of(None), 0.0)
        self.assertEqual(HttpTransport._retry_cost_of("x"), 0.0)

    def test_carry_unparseable_cost(self):
        from harness._http import HttpTransport
        out = HttpTransport._carry_retry_cost({"usage": {"cost": "bad"}},
                                              1.0)
        self.assertNotIn("cost", out["usage"])
        self.assertEqual(out["usage"]["retry_cost"], 1.0)

    def test_carry_unparseable_retry_cost(self):
        from harness._http import HttpTransport
        out = HttpTransport._carry_retry_cost(
            {"usage": {"retry_cost": "bad"}}, 1.0)
        self.assertEqual(out["usage"]["retry_cost"], 1.0)

    def test_carry_negative_retry_cost(self):
        from harness._http import HttpTransport
        out = HttpTransport._carry_retry_cost(
            {"usage": {"retry_cost": -2.0}}, 1.0)
        self.assertEqual(out["usage"]["retry_cost"], 1.0)


class TestHttpPostCover(unittest.TestCase):
    def _wire(self, check=None):
        from harness._http import HttpTransport
        return HttpTransport(cancel_check=check)

    def test_post_outer_http_error_retry(self):
        import urllib.request
        from harness import _http as http_mod

        wire = self._wire()
        real_loads = json.loads
        url = "http://127.0.0.1/"
        calls = []

        def flaky(body, *args, **kwargs):
            if not calls:
                calls.append(1)
                raise _http_error(url, 500, b"oops")
            return real_loads(body, *args, **kwargs)

        with mock.patch.object(urllib.request, "urlopen",
                               return_value=_CannedResp(
                                   200, b'{"ok": true}')), \
                mock.patch.object(http_mod.json, "loads",
                                  side_effect=flaky), \
                mock.patch.object(wire, "_retry_wait") as waited:
            status, parsed = wire.post(url, "k", {"model": "m"})
        self.assertEqual(status, 200)
        self.assertEqual(parsed, {"ok": True})
        self.assertEqual(waited.call_count, 1)

    def test_post_cancelled_progress_reraises(self):
        wire = self._wire(lambda: False)
        with mock.patch.object(
                wire, "_request_once_cancellable",
                side_effect=ToolCancelled("stop")), \
                mock.patch.object(wire, "_retry_wait"):
            with self.assertRaises(ToolCancelled):
                wire.post("http://127.0.0.1/", "k", {"model": "m"})


class TestHttpPostOnceCover(unittest.TestCase):
    def _boom_opener(self, exc):
        opener = mock.Mock()
        opener.open.side_effect = exc
        return opener

    def test_post_once_url_error_is_unknown_usage(self):
        import urllib.error
        import urllib.request
        from harness._http import HttpTransport

        wire = HttpTransport()
        with mock.patch.object(urllib.request, "build_opener",
                               return_value=self._boom_opener(
                                   urllib.error.URLError("down"))):
            with self.assertRaises(ProviderUsageUnknown):
                wire.post_once("http://127.0.0.1/", "k", {"model": "m"})

    def test_post_once_cancel_after_dispatch(self):
        import urllib.error
        import urllib.request
        from harness._http import HttpTransport

        wire = HttpTransport(cancel_check=lambda: True)
        with mock.patch.object(urllib.request, "build_opener",
                               return_value=self._boom_opener(
                                   urllib.error.URLError("down"))):
            with self.assertRaises(ToolCancelled):
                wire.post_once("http://127.0.0.1/", "k", {"model": "m"})

