"""Model input/output policy: the one chat-completion path and the one
assessment of what a model actually gave us.

Every lane (panel, judge, specialist, consent, apply, probe) sends payloads
through :func:`chat` -- the reasoning-parameter rules, the no-``tools``
guard, and the reasoning-retry live here exactly once. Every lane decides
whether a response is usable through :func:`assess_output` -- empty bodies,
reasoning-only traces, and truncation are protocol conditions, not content,
and must never be mined for votes, file bodies, or consent decisions.
"""
import json

from .config import OPENROUTER_CHAT_URL
from .output import eprint

REASONING_FALLBACK_PREFIX = "[NOTE] model returned no content"


def _extract_json(text):
    """Extract the first balanced {...} object from arbitrary model output."""
    if not text:
        return None
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _reported_cost(resp):
    """Read a provider-reported cost even when the HTTP response is an error."""
    try:
        return float((resp.get("usage") or {}).get("cost") or 0.0)
    except (AttributeError, TypeError, ValueError):
        return 0.0


def _merge_retry_cost(resp, prior_cost):
    """Carry a billable failed reasoning attempt into the retry response.

    ``chat`` keeps its small ``(status, response)`` API, so callers observe one
    response. Adding the prior attempt to ``usage.cost`` makes the governor and
    the ledger charge the complete provider-reported total without silently
    dropping a billable rejected request.
    """
    if not prior_cost or not isinstance(resp, dict):
        return resp
    usage = resp.setdefault("usage", {})
    try:
        current = float(usage.get("cost") or 0.0)
    except (TypeError, ValueError):
        current = 0.0
    usage["cost"] = current + prior_cost
    usage["retry_cost"] = prior_cost
    return resp


def extract_content_and_cost(resp):
    """Pull content, finish_reason, actual cost, is_byok from a completion.

    Falls back to the reasoning trace when a model emitted reasoning but no
    visible content — reasoning models otherwise "succeed" with empty output
    and a paid call is discarded with nothing to show for it. Callers that need
    *real* content (e.g. apply, which writes output to a file) must treat
    anything starting with REASONING_FALLBACK_PREFIX as "no usable output"
    rather than content.

    Usage is extracted independently of the choice body. Providers can return
    a billable, malformed/empty completion; losing that usage value would make
    the governor and the ledger disagree about spend.
    """
    usage = resp.get("usage") if isinstance(resp, dict) else {}
    usage = usage if isinstance(usage, dict) else {}
    cost = usage.get("cost", 0.0)
    is_byok = usage.get("is_byok", False)
    try:
        choice = resp["choices"][0]
        message = choice["message"]
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            # This text-only harness must never turn a malformed multimodal/list
            # body into file content or feed it to JSON parsing. Treat it as an
            # unusable response so the caller can rotate/fail closed.
            content = None
        if not (content or "").strip():
            reasoning = message.get("reasoning")
            if isinstance(reasoning, str) and reasoning.strip():
                content = (REASONING_FALLBACK_PREFIX + "; showing reasoning trace instead.\n\n"
                           + reasoning)
        finish_reason = choice.get("finish_reason", "unknown")
        return content, finish_reason, cost, is_byok
    except (AttributeError, KeyError, IndexError, TypeError):
        return None, None, cost, is_byok


def assess_output(content, finish_reason=None, allow_truncated=False):
    """The ONE usability verdict for a model response, shared by every lane.

    Returns ``(usable, reason)``. Empty and reasoning-only bodies are never
    usable: a reasoning trace can embed JSON-looking text that is not a verdict,
    and apply must never write protocol text into a file. A truncated response
    (``finish_reason == "length"``) is usable only when ``allow_truncated`` --
    the prose panel warns on truncation, while structured lanes (claims votes,
    specialist, consent) rotate instead of mining a cut-off body.
    """
    if not content:
        return False, "empty response"
    if content.startswith(REASONING_FALLBACK_PREFIX):
        return False, "reasoning-only output (no visible content)"
    if finish_reason == "length" and not allow_truncated:
        return False, "truncated (hit the token cap)"
    return True, None


# ------------------------- reasoning / effort -------------------------

_REASONING_HINTS = ("reason", "thinking", "inkling", "qwq", "r1", "o3", "o4",
                    "deepseek", "kimi", "glm-4.6", "glm-5.2", "glm-5.6", "minimax-reason")
_EFFORT_VALUES = ("auto", "off", "none", "low", "medium", "high", "on")
_REASONING_PARAM_ERR_HINTS = ("reasoning", "unsupported parameter",
                              "unknown parameter", "unexpected parameter")


def looks_reasoning(model_id):
    """Heuristic: does this model id smell like a reasoning model?"""
    m = model_id.lower()
    return any(h in m for h in _REASONING_HINTS)


def _effort_to_send(reasoning_effort, model_id):
    """Resolve the reasoning effort string to send, or None to omit.

    "auto" sends a capped low effort for reasoning-named models and omits the
    key entirely for everyone else. "off"/"none" always omit.
    """
    e = (reasoning_effort or "auto").lower()
    if e in ("off", "none"):
        return None
    if e == "auto":
        return "low" if looks_reasoning(model_id) else None
    if e == "on":
        return "high"
    if e in ("low", "medium", "high"):
        return e
    return None


def _build_reasoning_param(model_id, reasoning_effort, max_tokens, budget):
    """Return the reasoning dict to embed in the payload, or None."""
    effort = _effort_to_send(reasoning_effort, model_id)
    if effort is None:
        return None
    cap = max(1, int(max_tokens * budget))
    return {"effort": effort, "max_tokens": cap}


def _chat_reservation_slots(model_id, reasoning_effort="auto", max_429_retries=0):
    """Upper-bound provider calls for one governed logical request.

    A provider may reject a reasoning parameter, causing ``chat`` to make one
    fallback request. Panel calls may also make one bounded 429 retry. The
    preflight reservation must cover both possibilities or the ceiling is only
    approximate for reasoning models.
    """
    reasoning_slots = 2 if _effort_to_send(reasoning_effort, model_id) is not None else 1
    return reasoning_slots * (max(0, int(max_429_retries)) + 1)


def chat(transport, api_key, model, messages, max_tokens, reasoning_effort="auto",
         reasoning_token_budget=0.4, governor=None):
    """One chat completion with the spend governor's payload guards.

    Reasoning is only included when the effort mode calls for it (auto => only
    for reasoning-named models). If a provider rejects the reasoning
    parameter, we retry once without it.
    """
    if governor:
        governor.check_byok(model)

    def build(with_reasoning):
        payload = {"model": model, "messages": messages, "max_tokens": max_tokens}
        if with_reasoning:
            rp = _build_reasoning_param(model, reasoning_effort, max_tokens,
                                        reasoning_token_budget)
            if rp:
                payload["reasoning"] = rp
        if governor:
            governor.assert_no_tools(payload, model)
        return payload

    want_reasoning = _effort_to_send(reasoning_effort, model) is not None
    status, resp = transport.post(OPENROUTER_CHAT_URL, api_key, build(want_reasoning))
    if want_reasoning and status != 200:
        err = str(resp.get("error", {}).get("message", resp)
                  if isinstance(resp, dict) else resp).lower()
        if any(h in err for h in _REASONING_PARAM_ERR_HINTS):
            eprint(f"[retry] {model} rejected reasoning param; retrying without it.")
            prior_cost = _reported_cost(resp)
            retry_status, retry_resp = transport.post(
                OPENROUTER_CHAT_URL, api_key, build(False))
            return retry_status, _merge_retry_cost(retry_resp, prior_cost)
    return status, resp
