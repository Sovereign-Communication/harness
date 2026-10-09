"""stdlib HTTP helpers (urllib). No third-party dependencies.

The transport is an injectable seam so every engine path can be tested
hermetically (no network) by substituting a fake transport.
"""
import base64
import json
import threading
import time
import urllib.error
import urllib.request
from . import osal
from .errors import ToolCancelled
from .provider_errors import raise_for_provider_spend_limit


def interruptible_sleep(delay, cancel_check):
    """Wait for ``delay`` seconds, checking a run's cancellation signal."""
    if cancel_check is None:
        time.sleep(delay)
        return
    from .errors import ToolCancelled
    deadline = time.monotonic() + delay
    while True:
        if cancel_check():
            raise ToolCancelled("Prompt execution was cancelled by user")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.05, remaining))


def cancellable_request(method, url, *, headers=None, data=None, timeout=15,
                        cancel_check=None, max_bytes=None, no_redirect=False):
    """Perform one HTTP request in a killable child process.

    The child owns the blocking socket while the caller owns cancellation.
    Request headers/body travel through stdin, never process arguments.
    """
    from .errors import ToolCancelled
    if cancel_check is None:
        from .events import current_cancel_check
        cancel_check = current_cancel_check()
    if cancel_check is None:
        raise ValueError("cancellable_request requires a cancellation check")
    if cancel_check():
        raise ToolCancelled("Prompt execution was cancelled by user")

    command = [osal.python_exe(), "-m", "harness._http_worker"]
    child = osal.spawn_piped_process(command)
    completed = {}
    request = {
        "method": method,
        "url": url,
        "headers": headers or {},
        "data_b64": base64.b64encode(data).decode("ascii") if data is not None else None,
        "timeout": timeout,
        "max_bytes": max_bytes,
        "no_redirect": no_redirect,
    }

    def communicate():
        try:
            completed["output"] = child.communicate(
                input=json.dumps(request).encode("utf-8"))
        except BaseException as exc:  # thread must always report completion
            completed["exception"] = exc

    waiter = threading.Thread(target=communicate,
                              name="harness-http-child-wait", daemon=True)
    waiter.start()
    try:
        while waiter.is_alive():
            if cancel_check():
                if child.poll() is None:
                    osal.terminate_and_reap(child)
                waiter.join(timeout=2.0)
                if waiter.is_alive():
                    raise OSError("cancelled HTTP child did not release its pipes")
                raise ToolCancelled("Prompt execution was cancelled by user")
            waiter.join(timeout=0.04)
    finally:
        if child.poll() is None:
            osal.kill_and_reap(child)
        if waiter.is_alive():
            waiter.join(timeout=2.0)
    if "exception" in completed:
        raise OSError(f"HTTP child communication failed: {completed['exception']}")
    stdout, stderr = completed.get("output", (b"", b""))
    try:
        result = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        detail = stderr.decode("utf-8", errors="replace")[-500:]
        raise OSError(f"HTTP child returned an invalid response: {detail}") from exc
    if child.returncode != 0:
        raise OSError(f"HTTP child exited with status {child.returncode}")
    if "exception" in result:
        info = result["exception"]
        raise OSError(f"HTTP request failed ({info.get('type', 'error')}): "
                      f"{info.get('message', '')}")
    try:
        body = base64.b64decode(result.get("body_b64", ""), validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise OSError("HTTP child returned an invalid body") from exc
    return (int(result["status"]), body, result.get("retry_after"),
            result.get("final_url", url))


class Transport:
    """Minimal HTTP seam. get/post return parsed JSON."""

    def get(self, url, api_key, timeout=15):  # pragma: no cover - interface
        raise NotImplementedError

    # Free-tier models can take well over 45s on long audit prompts with a
    # high --max-tokens output budget (the 45s default caused read timeouts on
    # the SCMessenger consensus audits). 120s gives room without hanging
    # indefinitely on a dead connection.
    def post(self, url, api_key, payload, timeout=120):  # pragma: no cover - interface
        raise NotImplementedError


class HttpTransport(Transport):
    # urllib.request.urlopen releases the GIL during I/O and opens a fresh
    # connection per call, so concurrent POSTs from the panel fan-out are safe.
    parallel_safe = True
    """stdlib HTTP with bounded transient-failure handling (#18):

    * GET retries idempotent failures (network errors, 429, 5xx) with
      Retry-After honor and capped exponential backoff.
    * POST is retried ONLY for 429/5xx responses received before a body was
      consumed -- a request that reached the model is never blindly re-sent
      by the transport (the engine lanes own their retry semantics).
    """

    MAX_RETRIES = 3

    def __init__(self, cancel_check=None):
        self.cancel_check = cancel_check

    def with_cancel(self, cancel_check):
        """Return an otherwise identical transport bound to one run's stop flag.

        A transport can be shared by normal callers; cancellation is therefore
        request-scoped instead of mutating a possibly shared instance.
        """
        return type(self)(cancel_check=cancel_check)

    def _active_cancel_check(self):
        if self.cancel_check is not None:
            return self.cancel_check
        from .events import current_cancel_check
        return current_cancel_check()

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        """Keep one-attempt calls from silently becoming multiple requests."""
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    def _retry_delay(self, attempt, retry_after):
        if retry_after:
            try:
                return min(float(retry_after), 6.0)
            except (TypeError, ValueError):
                pass
        return min(0.75 * (2 ** attempt), 6.0)

    @staticmethod
    def _transient(status):
        return status == 429 or 500 <= status <= 599

    def _header(self, resp, name):
        return resp.headers.get(name) if resp.headers else None

    def get(self, url, api_key, timeout=15):
        cancel_check = self._active_cancel_check()
        last_err = None
        for attempt in range(self.MAX_RETRIES + 1):
            from .events import raise_if_provider_spend_limited
            raise_if_provider_spend_limited()
            if cancel_check and cancel_check():
                from .errors import ToolCancelled
                raise ToolCancelled("Prompt execution was cancelled by user")
            req = urllib.request.Request(
                url, headers={"Authorization": f"Bearer {api_key}"})
            try:
                if cancel_check:
                    status, body, retry_after = self._request_once_cancellable(
                        "GET", url, api_key, None, timeout, cancel_check)
                    try:
                        parsed = json.loads(body)
                    except json.JSONDecodeError:
                        parsed = {"error": {"message": body}}
                    raise_for_provider_spend_limit(status, parsed)
                    if self._transient(status) and attempt < self.MAX_RETRIES:
                        last_err = OSError(f"transient HTTP status {status}")
                        self._retry_wait(self._retry_delay(attempt, retry_after),
                                         cancel_check)
                        continue
                    return parsed
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace")
                e.close()  # release the error stream; code/headers stay readable
                try:
                    parsed = json.loads(body)
                except json.JSONDecodeError:
                    parsed = {"error": {"message": body}}
                raise_for_provider_spend_limit(e.code, parsed)
                if self._transient(e.code) and attempt < self.MAX_RETRIES:
                    last_err = e
                    self._retry_wait(self._retry_delay(
                        attempt, e.headers.get("Retry-After") if e.headers else None),
                        cancel_check)
                    continue
                return parsed
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                if attempt < self.MAX_RETRIES:
                    last_err = e
                    self._retry_wait(self._retry_delay(attempt, None),
                                     cancel_check)
                    continue
                raise
        raise last_err

    @staticmethod
    def _retry_cost_of(parsed):
        """Billable cost reported by an error response, else 0."""
        try:
            usage = parsed.get("usage") or {}
            cost = float(usage.get("cost") or 0.0)
            if cost:
                return max(0.0, cost)
            # TypeSafe reports input-token counts rather than USD in some
            # terminal error bodies. Preserve that known charge as well.
            tokens = usage.get("input_tokens")
            if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens > 0:
                from .jev import jev_cost
                return jev_cost(tokens)
            return 0.0
        except (AttributeError, TypeError, ValueError):
            return 0.0

    @classmethod
    def _carry_retry_cost(cls, parsed, amount):
        """Fold dropped transient-attempt spend into the final response so
        the governor bills every attempt the provider metered (same
        contract as the reasoning-param retry in chat)."""
        if not amount or not isinstance(parsed, dict):
            return parsed
        usage = parsed.setdefault("usage", {})
        if not isinstance(usage, dict):
            return parsed
        try:
            current = float(usage.get("cost") or 0.0)
        except (TypeError, ValueError):
            current = 0.0
        usage["cost"] = current + amount
        usage["retry_cost"] = usage.get("retry_cost", 0.0) + amount
        return parsed

    def _request_once_cancellable(self, method, url, api_key, payload, timeout,
                                  cancel_check, no_redirect=False):
        """Run one provider request through the reapable HTTP child."""
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        status, body, retry_after, _ = cancellable_request(
            method, url,
            headers={"Authorization": f"Bearer {api_key}",
                     **({"Content-Type": "application/json"}
                        if data is not None else {})},
            data=data, timeout=timeout, cancel_check=cancel_check,
            no_redirect=no_redirect)
        return status, body.decode("utf-8", errors="replace"), retry_after

    @staticmethod
    def _retry_wait(delay, cancel_check, known_cost=0.0):
        try:
            return interruptible_sleep(delay, cancel_check)
        except ToolCancelled as exc:
            exc.add_known_cost(known_cost)
            raise

    def post(self, url, api_key, payload, timeout=120, cancel_check=None):
        cancel_check = cancel_check or self._active_cancel_check()
        data = json.dumps(payload).encode("utf-8")
        dropped = 0.0
        for attempt in range(self.MAX_RETRIES + 1):
            from .events import raise_if_provider_spend_limited
            raise_if_provider_spend_limited(known_cost=dropped)
            if cancel_check and cancel_check():
                raise ToolCancelled(known_cost=dropped)
            req = urllib.request.Request(
                url, data=data,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                method="POST",
            )
            try:
                if cancel_check:
                    try:
                        status, body, retry_after = self._request_once_cancellable(
                            "POST", url, api_key, payload, timeout, cancel_check)
                    except ToolCancelled as exc:
                        exc.add_known_cost(dropped)
                        raise
                else:
                    try:
                        with urllib.request.urlopen(req, timeout=timeout) as resp:
                            status = resp.getcode()
                            body = resp.read().decode("utf-8", errors="replace")
                            retry_after = (resp.headers.get("Retry-After")
                                           if resp.headers else None)
                    except urllib.error.HTTPError as e:
                        status = e.code
                        body = e.read().decode("utf-8", errors="replace")
                        retry_after = (e.headers.get("Retry-After")
                                       if e.headers else None)
                        e.close()
                try:
                    parsed = json.loads(body)
                except json.JSONDecodeError:
                    parsed = {"error": {"message": body}}
                raise_for_provider_spend_limit(
                    status, parsed,
                    known_cost=dropped + self._retry_cost_of(parsed))
                if self._transient(status) and attempt < self.MAX_RETRIES:
                    dropped += self._retry_cost_of(parsed)
                    self._retry_wait(self._retry_delay(attempt, retry_after),
                                     cancel_check, known_cost=dropped)
                    continue
                return status, self._carry_retry_cost(parsed, dropped)
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace")
                e.close()  # release the error stream; code/headers stay readable
                try:
                    parsed = json.loads(body)
                except json.JSONDecodeError:
                    parsed = {"error": {"message": body}}
                raise_for_provider_spend_limit(
                    e.code, parsed,
                    known_cost=dropped + self._retry_cost_of(parsed))
                if self._transient(e.code) and attempt < self.MAX_RETRIES:
                    dropped += self._retry_cost_of(parsed)
                    self._retry_wait(self._retry_delay(
                        attempt, e.headers.get("Retry-After") if e.headers else None),
                        cancel_check, known_cost=dropped)
                    continue
                return e.code, self._carry_retry_cost(parsed, dropped)
        raise OSError("unreachable: retries exhausted without a response")

    def post_once(self, url, api_key, payload, timeout=120):
        """POST exactly once, including on transient HTTP failures.

        Most Harness lanes deliberately use :meth:`post`, whose bounded
        retry behavior is useful for their idempotent/explicit retry policy.
        HV-0's live assessment contract instead permits one request only, so
        it uses this method and treats every transport/status failure as an
        unassessed result.
        """
        cancel_check = self._active_cancel_check()
        from .events import raise_if_provider_spend_limited
        raise_if_provider_spend_limited()
        if cancel_check:
            status, body, _retry_after = self._request_once_cancellable(
                "POST", url, api_key, payload, timeout, cancel_check,
                no_redirect=True)
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                parsed = {"error": {"message": body}}
            raise_for_provider_spend_limit(
                status, parsed, known_cost=self._retry_cost_of(parsed))
            return status, parsed

        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            method="POST",
        )
        try:
            opener = urllib.request.build_opener(self._NoRedirect())
            with opener.open(req, timeout=timeout) as resp:
                status = resp.getcode()
                parsed = json.loads(resp.read().decode("utf-8"))
                raise_for_provider_spend_limit(
                    status, parsed, known_cost=self._retry_cost_of(parsed))
                return status, parsed
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            exc.close()
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                parsed = {"error": {"message": body}}
            raise_for_provider_spend_limit(
                exc.code, parsed, known_cost=self._retry_cost_of(parsed))
            return exc.code, parsed
