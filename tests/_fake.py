"""Shared fakes for hermetic (no-network) tests."""
import json

from harness.spend import SpendGovernor

# Shared lane model ids (free-tier panel/judge conventions).
P1 = "inclusionai/ling-2.6-flash"
P2 = "meta-llama/llama-3.1-8b-instruct"
JUDGE = "inclusionai/ling-2.6-flash"


def _gov(fake, **kw):
    """A SpendGovernor over ``fake`` with the standard test key."""
    return SpendGovernor(fake, "sk-test", **kw)


class FakeTransport:
    def __init__(self, models=None, key=None, posts=None):
        self.models = models or []
        self.key = key or {"label": "sk-or-v1-test", "limit": 1.0,
                           "limit_remaining": 0.99, "limit_reset": "daily"}
        self.posts = list(posts or [])
        self.calls = []  # ("GET"|"POST", url, [payload])

    def get(self, url, api_key, timeout=15):
        self.calls.append(("GET", url))
        if url.endswith("/models"):
            return {"data": self.models}
        if url.endswith("/key"):
            return {"data": self.key}
        raise AssertionError(f"unexpected GET {url}")

    def post(self, url, api_key, payload, timeout=45):
        self.calls.append(("POST", url, payload))
        if self.posts:
            item = self.posts.pop(0)
            if isinstance(item, tuple):
                return item          # (status, body)
            return 200, item         # bare body -> assume HTTP 200
        raise AssertionError("no canned chat response left")

    def chat_posts(self):
        return [c for c in self.calls if c[0] == "POST"]

    def payloads(self):
        return [c[2] for c in self.calls if c[0] == "POST"]


def m(model_id, prompt="0.00000001", completion="0.00000002"):
    """Model entry with per-token dollar pricing (prompt default $0.01/M)."""
    return {"id": model_id, "pricing": {"prompt": prompt, "completion": completion}}


def comp(content="hi", finish="stop", cost=0.000001, reasoning=None):
    msg = {"content": content}
    if reasoning:
        msg["reasoning"] = reasoning
    return {"choices": [{"message": msg, "finish_reason": finish}],
            "usage": {"cost": cost, "is_byok": False}}


def consent(decision, reason="ok"):
    return comp(json.dumps({"decision": decision, "reason": reason,
                            "redirect_model": None, "scope_suggestion": None}))


def chat_payload(post):
    """Pull the messages of a POST payload (or None)."""
    if post[0] == "POST":
        return post[2]
    return None
