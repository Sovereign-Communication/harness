"""stdlib HTTP helpers (urllib). No third-party dependencies.

The transport is an injectable seam so every engine path can be tested
hermetically (no network) by substituting a fake transport.
"""
import json
import time
import urllib.error
import urllib.request


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
        last_err = None
        for attempt in range(self.MAX_RETRIES + 1):
            req = urllib.request.Request(
                url, headers={"Authorization": f"Bearer {api_key}"})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace")
                if self._transient(e.code) and attempt < self.MAX_RETRIES:
                    last_err = e
                    time.sleep(self._retry_delay(attempt, e.headers.get("Retry-After")
                                                 if e.headers else None))
                    continue
                try:
                    return json.loads(body)
                except json.JSONDecodeError:
                    return {"error": {"message": body}}
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                if attempt < self.MAX_RETRIES:
                    last_err = e
                    time.sleep(self._retry_delay(attempt, None))
                    continue
                raise
        raise last_err

    @staticmethod
    def _retry_cost_of(parsed):
        """Billable cost buried in a transient error body, else 0."""
        try:
            return float((parsed.get("usage") or {}).get("cost") or 0.0)
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

    def post(self, url, api_key, payload, timeout=120):
        data = json.dumps(payload).encode("utf-8")
        dropped = 0.0
        for attempt in range(self.MAX_RETRIES + 1):
            req = urllib.request.Request(
                url, data=data,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return (resp.getcode(),
                            self._carry_retry_cost(
                                json.loads(resp.read().decode("utf-8")),
                                dropped))
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace")
                try:
                    parsed = json.loads(body)
                except json.JSONDecodeError:
                    parsed = {"error": {"message": body}}
                if self._transient(e.code) and attempt < self.MAX_RETRIES:
                    dropped += self._retry_cost_of(parsed)
                    time.sleep(self._retry_delay(attempt, e.headers.get("Retry-After")
                                                 if e.headers else None))
                    continue
                return e.code, self._carry_retry_cost(parsed, dropped)
        raise OSError("unreachable: retries exhausted without a response")
