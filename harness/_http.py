"""stdlib HTTP helpers (urllib). No third-party dependencies.

The transport is an injectable seam so every engine path can be tested
hermetically (no network) by substituting a fake transport.
"""
import json
import urllib.error
import urllib.request


class Transport:
    """Minimal HTTP seam. get/post return parsed JSON."""

    def get(self, url, api_key, timeout=15):  # pragma: no cover - interface
        raise NotImplementedError

    def post(self, url, api_key, payload, timeout=45):  # pragma: no cover - interface
        raise NotImplementedError


class HttpTransport(Transport):
    def get(self, url, api_key, timeout=15):
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def post(self, url, api_key, payload, timeout=45):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.getcode(), json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8")
            try:
                return e.code, json.loads(body)
            except json.JSONDecodeError:
                return e.code, {"error": {"message": body}}