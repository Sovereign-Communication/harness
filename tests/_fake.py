"""Shared fakes for hermetic (no-network) tests."""
import json

from harness.spend import SpendGovernor

# Shared lane model ids (free-tier panel/judge conventions).
P1 = "inclusionai/ling-2.6-flash"
P2 = "meta-llama/llama-3.1-8b-instruct"
JUDGE = "inclusionai/ling-2.6-flash"


JEV_URL = "https://api.typesafe.ai/v1/systemone"


def _gov(fake, **kw):
    """A SpendGovernor over ``fake`` with the standard test key."""
    return SpendGovernor(fake, "sk-test", **kw)


class FakeTransport:
    def __init__(self, models=None, key=None, posts=None, jev_posts=None):
        self.models = models or []
        self.key = key or {"label": "sk-or-v1-test", "limit": 1.0,
                           "limit_remaining": 0.99, "limit_reset": "daily"}
        self.posts = list(posts or [])
        self.jev_posts = list(jev_posts) if jev_posts is not None else None
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
        if "typesafe.ai" in url or "systemone" in url or url.endswith("/jev"):
            if self.jev_posts is not None:
                if self.jev_posts:
                    item = self.jev_posts.pop(0)
                    if isinstance(item, tuple):
                        return item
                    return 200, item
                raise AssertionError("no canned jev response left")
            if self.posts:
                item = self.posts.pop(0)
                if isinstance(item, tuple):
                    return item
                return 200, item
            qs = payload.get("questions") if isinstance(payload, dict) else None
            return 200, jev_resp(questions=qs)
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

    def jev_calls(self):
        return [c for c in self.calls if c[0] == "POST" and ("typesafe.ai" in c[1] or "systemone" in c[1] or c[1].endswith("/jev"))]


def jev_resp(answers=None, *, noul=0.95, confidence=0.9, choice="direct", score=1.0,
             input_tokens=100, output_tokens=10, model="jev-latest", questions=None):
    """Hermetic Jev response fixture matching the TypeSafe / System One schema."""
    if answers is None:
        if questions:
            answers = {}
            for q_name, q in questions.items():
                qtype = q.get("type")
                if qtype == "noul":
                    answers[q_name] = {
                        "type": "noul",
                        "noul": noul,
                        "probabilities": {"pass": noul, "fail": round(1.0 - noul, 6)},
                    }
                elif qtype == "choice":
                    criteria = q.get("criteria", {})
                    c = choice if choice in criteria else (next(iter(criteria)) if criteria else choice)
                    probs = {k: (1.0 if k == c else 0.0) for k in criteria}
                    answers[q_name] = {
                        "type": "choice",
                        "choice": c,
                        "probabilities": probs,
                        "confidence": confidence,
                    }
                elif qtype == "score":
                    crit = q.get("criteria", ["low", "high"])
                    legend = {str(i): c for i, c in enumerate(crit)}
                    s_int = int(score) if 0 <= int(score) < len(crit) else 0
                    probs = {str(i): (1.0 if i == s_int else 0.0) for i in range(len(crit))}
                    answers[q_name] = {
                        "type": "score",
                        "score": float(s_int),
                        "legend": legend,
                        "probabilities": probs,
                        "confidence": confidence,
                    }
        else:
            answers = {
                "instruction_matches": {
                    "type": "noul",
                    "noul": noul,
                    "probabilities": {"pass": noul, "fail": round(1.0 - noul, 6)},
                },
                "route": {
                    "type": "choice",
                    "choice": choice if choice in ("free-distill", "diff", "frontier") else "free-distill",
                    "probabilities": {"free-distill": 1.0, "diff": 0.0, "frontier": 0.0},
                    "confidence": confidence,
                },
                "requires_iteration": {
                    "type": "noul",
                    "noul": 0.05,
                    "probabilities": {"pass": 0.05, "fail": 0.95},
                },
            }
    return {
        "model": model,
        "answers": answers,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
    }


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
