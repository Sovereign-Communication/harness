"""stdlib HTTP helpers (urllib). No third-party dependencies.

The transport is an injectable seam so every engine path can be tested
hermetically (no network) by substituting a fake transport.
"""
import base64
import http.client
import json
import math
import threading
import time
import uuid
import urllib.error
import urllib.request
from pathlib import Path
from . import osal
from .errors import ProviderUsageUnknown, ToolCancelled


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

    # Run the trusted worker by its absolute file path in isolated mode. A
    # target repository may contain a top-level ``harness`` package; ``-m``
    # would let that package shadow this worker and read credentials from the
    # request pipe.
    worker = Path(__file__).resolve().with_name("_http_worker.py")
    command = [osal.python_exe(), "-I", str(worker)]
    child = osal.spawn_piped_process(
        command, cwd=str(worker.parent.parent))
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
                raise ToolCancelled("Prompt execution was cancelled by user",
                                    usage_unknown=True)
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


class _CancellationBoundTransport(Transport):
    """Per-run cancellation view that preserves a stateful transport."""

    def __init__(self, delegate, cancel_check):
        self._delegate = delegate
        self._cancel_check = cancel_check

    def __getattr__(self, name):
        return getattr(self._delegate, name)

    def _call(self, method, *args, **kwargs):
        from .events import cancellation_context
        with cancellation_context(self._cancel_check):
            return getattr(self._delegate, method)(*args, **kwargs)

    def get(self, *args, **kwargs):
        return self._call("get", *args, **kwargs)

    def post(self, *args, **kwargs):
        return self._call("post", *args, **kwargs)

    def post_once(self, *args, **kwargs):
        return self._call("post_once", *args, **kwargs)


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

    @staticmethod
    def is_production_transport(transport):
        """Identify the real HTTP client through the per-run cancel view.

        Keep this exact-type check narrow: test doubles and HttpTransport
        subclasses may carry their own state and must not share process-wide
        Jev cache entries or breaker history.
        """
        return (type(transport) is HttpTransport
                or (type(transport) is _CancellationBoundTransport
                    and type(getattr(transport, "_delegate", None))
                    is HttpTransport))

    def with_cancel(self, cancel_check):
        """Return a run-scoped view over this exact transport instance.

        The view delegates to the existing instance so injected subclasses,
        fixtures, pools, and other constructor state are preserved. It binds
        cancellation through a ContextVar for only the delegated call.
        """
        return _CancellationBoundTransport(self, cancel_check)

    def _active_cancel_check(self):
        from .events import current_cancel_check
        active = current_cancel_check()
        return active if active is not None else self.cancel_check

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
        if not amount:
            return parsed
        if not isinstance(parsed, dict):
            # Preserve spend even when the provider's final body is not an
            # object. The evaluator will reject the synthetic envelope as an
            # answer, while still settling the retry charge.
            return {"_http_response": parsed,
                    "usage": {"retry_cost": amount}}
        usage = parsed.get("usage")
        if not isinstance(usage, dict):
            parsed["usage"] = {"retry_cost": amount}
            return parsed
        if "cost" in usage:
            try:
                current = float(usage.get("cost") or 0.0)
            except (TypeError, ValueError):
                current = None
            if current is not None and math.isfinite(current) and current >= 0.0:
                usage["cost"] = current + amount
            else:
                usage.pop("cost", None)
        try:
            retry_cost = float(usage.get("retry_cost") or 0.0)
        except (TypeError, ValueError):
            retry_cost = 0.0
        if retry_cost < 0.0 or not math.isfinite(retry_cost):
            retry_cost = 0.0
        usage["retry_cost"] = retry_cost + amount
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
        from .events import current_provider_request, emit
        request_context = current_provider_request()
        request_id = request_context.get("request_id") or uuid.uuid4().hex
        model = request_context.get("model")
        if not model and isinstance(payload, dict):
            model = payload.get("model")
        model = model if isinstance(model, str) and len(model) <= 120 else "provider"

        def progress(phase, attempt, **fields):
            emit("provider_http_attempt", request_id=request_id, model=model,
                 model_attempt=request_context.get("attempt"), phase=phase,
                 wire_attempt=attempt + 1, max_wire_attempts=self.MAX_RETRIES + 1,
                 **fields)

        for attempt in range(self.MAX_RETRIES + 1):
            if cancel_check and cancel_check():
                raise ToolCancelled(known_cost=dropped)
            req = urllib.request.Request(
                url, data=data,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                method="POST",
            )
            request_started = time.monotonic()
            progress("start", attempt)
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
                progress("response", attempt, http_status=status,
                         duration_s=round(time.monotonic() - request_started, 2))
                if self._transient(status) and attempt < self.MAX_RETRIES:
                    dropped += self._retry_cost_of(parsed)
                    delay = self._retry_delay(attempt, retry_after)
                    reason = "rate_limited" if status == 429 else "server_error"
                    progress("retry_wait", attempt, http_status=status,
                             retry_reason=reason, delay_s=delay)
                    self._retry_wait(delay,
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
                progress("response", attempt, http_status=e.code,
                         duration_s=round(time.monotonic() - request_started, 2))
                if self._transient(e.code) and attempt < self.MAX_RETRIES:
                    dropped += self._retry_cost_of(parsed)
                    delay = self._retry_delay(
                        attempt, e.headers.get("Retry-After") if e.headers else None)
                    reason = "rate_limited" if e.code == 429 else "server_error"
                    progress("retry_wait", attempt, http_status=e.code,
                             retry_reason=reason, delay_s=delay)
                    self._retry_wait(delay,
                        cancel_check, known_cost=dropped)
                    continue
                return e.code, self._carry_retry_cost(parsed, dropped)
            except ToolCancelled as exc:
                progress("cancelled", attempt,
                         usage_unknown=exc.usage_unknown,
                         duration_s=round(time.monotonic() - request_started, 2))
                raise
            except (urllib.error.URLError, OSError, TimeoutError,
                    http.client.HTTPException) as exc:
                progress("error", attempt, error_type=type(exc).__name__,
                         usage_unknown=True,
                         duration_s=round(time.monotonic() - request_started, 2))
                if cancel_check and cancel_check():
                    raise ToolCancelled(
                        "Prompt execution was cancelled after provider dispatch",
                        known_cost=dropped, usage_unknown=True) from exc
                raise ProviderUsageUnknown(
                    "Provider response was lost after dispatch; usage is "
                    "unknown and no automatic retransmission was made "
                    f"({type(exc).__name__}).",
                    known_cost=dropped) from exc
        raise OSError("unreachable: retries exhausted without a response")

    def post_once(self, url, api_key, payload, timeout=120):
        """POST exactly once, including on transient HTTP failures.

        Most Harness lanes deliberately use :meth:`post`, whose bounded
        retry behavior is useful for their idempotent/explicit retry policy.
        HV-0's live assessment contract instead permits one request only, so
        it uses this method and treats every transport/status failure as an
        unassessed result.
        """
        from .events import current_provider_request, emit

        cancel_check = self._active_cancel_check()
        context = current_provider_request()
        request_id = context.get("request_id") or uuid.uuid4().hex
        model = context.get("model") or (
            payload.get("model") if isinstance(payload, dict) else None)
        model = model if isinstance(model, str) and len(model) <= 120 else "provider"

        def progress(phase, **fields):
            emit("provider_http_attempt", request_id=request_id, model=model,
                 model_attempt=context.get("attempt"), phase=phase,
                 wire_attempt=1, max_wire_attempts=1, **fields)

        request_started = time.monotonic()
        progress("start")
        try:
            if cancel_check:
                status, body, _retry_after = self._request_once_cancellable(
                    "POST", url, api_key, payload, timeout, cancel_check,
                    no_redirect=True)
            else:
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
                        body = resp.read().decode("utf-8", errors="replace")
                except urllib.error.HTTPError as exc:
                    status = exc.code
                    body = exc.read().decode("utf-8", errors="replace")
                    exc.close()
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                parsed = {"error": {"message": body}}
        except ToolCancelled as exc:
            progress("cancelled", usage_unknown=exc.usage_unknown,
                     duration_s=round(time.monotonic() - request_started, 2))
            raise
        except (urllib.error.URLError, OSError, TimeoutError,
                http.client.HTTPException) as exc:
            progress("error", error_type=type(exc).__name__, usage_unknown=True,
                     duration_s=round(time.monotonic() - request_started, 2))
            if cancel_check and cancel_check():
                raise ToolCancelled(
                    "Prompt execution was cancelled after provider dispatch",
                    usage_unknown=True) from exc
            raise ProviderUsageUnknown(
                "Provider response was lost after dispatch; usage is unknown "
                f"and no automatic retransmission was made ({type(exc).__name__})."
            ) from exc
        progress("response", http_status=status,
                 duration_s=round(time.monotonic() - request_started, 2))
        return status, parsed
