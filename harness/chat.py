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
import math
import time
import uuid

from .config import OPENROUTER_CHAT_URL
from .errors import HarnessError, ProviderUsageUnknown, ToolCancelled
from .events import emit, provider_request_context
from .output import eprint
from .routing_table import floor_model, strip_variant_suffix
from .tokens import estimate_prompt_tokens

REASONING_FALLBACK_PREFIX = "[NOTE] model returned no content"


def _extract_json(text):
    """Extract the first balanced {...} object from arbitrary model output.

    Reasoning-style models (e.g. the Ling family) prose/think-wrap their
    answer -- ``<think>{draft notes}</think>{"verdict":"allow"}`` -- so the
    first ``{`` is not necessarily the payload. Advance to the next ``{``
    candidate whenever the current one fails to parse (or never closes)
    instead of giving up on the first miss.
    """
    if not text:
        return None
    start = text.find("{")
    while start != -1:
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
                        break
        start = text.find("{", start + 1)
    return None


def _reported_cost(resp):
    """Read a provider-reported cost even when the HTTP response is an error."""
    try:
        usage = resp.get("usage") or {}
        value = usage.get("cost")
        if value is None:
            value = usage.get("retry_cost")
        if isinstance(value, bool) or value is None:
            return 0.0
        cost = float(value)
        return cost if math.isfinite(cost) and cost >= 0.0 else 0.0
    except (AttributeError, TypeError, ValueError):
        return 0.0


def _retry_cost_value(usage):
    if not isinstance(usage, dict):
        return 0.0
    value = usage.get("retry_cost", 0.0)
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0.0):
        return 0.0
    return float(value)


def _merge_retry_cost(resp, prior_cost):
    """Carry a billable failed reasoning attempt into the retry response."""
    if not prior_cost:
        return resp
    if not isinstance(resp, dict):
        # Keep known spend visible even if the retry body is not an object;
        # downstream extraction will see a malformed answer and the charge.
        return {"_http_response": resp,
                "usage": {"retry_cost": prior_cost}}
    usage = resp.get("usage")
    if not isinstance(usage, dict):
        resp["usage"] = {"retry_cost": prior_cost}
        return resp
    retry_cost = _retry_cost_value(usage)
    usage["retry_cost"] = retry_cost + prior_cost
    if "cost" in usage:
        value = usage.get("cost")
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value < 0.0):
            usage.pop("cost", None)
        else:
            usage["cost"] = float(value) + prior_cost
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
    if not (content or "").strip():
        return False, "empty response"
    if content.startswith(REASONING_FALLBACK_PREFIX):
        return False, "reasoning-only output (no visible content)"
    if finish_reason == "length" and not allow_truncated:
        return False, "truncated (hit the token cap)"
    return True, None


def looks_truncated(text):
    """Heuristic for a body cut off mid-JSON (never promoted to a verdict).

    A fence that opens and never closes, or unbalanced braces/brackets,
    means the provider stopped mid-object (the Sep-2026 seat-gate loss was a
    57-char body that just stopped). Prose around balanced JSON is not
    truncation.
    """
    if not text:
        return False
    if text.count("```") % 2 == 1:
        return True
    body = text
    if "```" in body:
        body = body.split("```", 2)[1]
        if body.lstrip().lower().startswith("json") and "\n" in body:
            body = body.split("\n", 1)[1]
    return ((body.count("{") != body.count("}"))
            or (body.count("[") != body.count("]")))


# ------------------------- reasoning / effort -------------------------

_REASONING_HINTS = ("reason", "thinking", "inkling", "qwq", "r1", "o3", "o4",
                    "o1", "gpt-5", "deepseek", "kimi", "glm-4.6", "glm-5.2", "glm-5.3",
                    "glm-5.6", "minimax-reason", "nemotron", "openrouter/free",
                    # Ling 3.x think/prose-wraps its answer (DF-LING-1); no
                    # bare "ling" (would false-positive on unrelated ids) and
                    # no vendor-prefixed "inclusionai/ling-3" (chat.py's own
                    # hardcoded-model-id guard bans "/"-qualified vendor
                    # strings here -- see test_hg_ms_parity).
                    "ling-3")
_EFFORT_VALUES = ("auto", "off", "none", "low", "medium", "high", "on")
# Hints matched against a provider's error body when the reasoning parameter
# may have caused the rejection. "mandatory" covers the mandatory-reasoning
# routes that reject an explicit {"effort": "none"} disable with HTTP 400
# ("Reasoning is mandatory for this endpoint and cannot be disabled").
_REASONING_PARAM_ERR_HINTS = ("reasoning", "mandatory",
                              "unsupported parameter",
                              "unknown parameter", "unexpected parameter")


# Models that have rejected the reasoning parameter in this process. Learned
# from the provider's own rejection text -- never from a provider brand list --
# so a newly routed model is treated the same as a known one.
#
# The dogfood logged 18 reasoning-param rejections in a single session: the
# rejection was detected and retried correctly, but every later call paid for
# the same doomed first attempt again. Remembering it makes the SECOND call
# onwards single-flight, which is the whole saving.
#
# Process-lifetime, like the site breaker board: one relearning call per
# process, not one per request. `reset_reasoning_param_memory` exists so tests
# (and an operator who has changed a route) can drop it.
_REASONING_PARAM_REJECTED = set()


def note_reasoning_param_rejection(model_id):
    """Remember that this model rejected the reasoning parameter."""
    canonical = strip_variant_suffix(model_id or "")
    if canonical:
        _REASONING_PARAM_REJECTED.add(canonical)


def reasoning_param_rejected(model_id):
    """True when this model is known to reject the reasoning parameter."""
    return strip_variant_suffix(model_id or "") in _REASONING_PARAM_REJECTED


def reset_reasoning_param_memory():
    """Forget every learned rejection (tests; operator route changes)."""
    _REASONING_PARAM_REJECTED.clear()


def looks_reasoning(model_id):
    """Heuristic: does this model id smell like a reasoning model?"""
    m = model_id.lower()
    return any(h in m for h in _REASONING_HINTS)


def _effort_to_send(reasoning_effort, model_id):
    """Resolve the reasoning effort string to send, or None to omit.

    "auto" sends a capped low effort for reasoning-named models and omits the
    key entirely for everyone else. "off"/"none" resolve to the explicit
    disable ("none"): for reasoning-native models (deepseek/*, z-ai/glm-*,
    moonshotai/kimi-*), OMITTING the reasoning key means the provider default
    -- reasoning ON -- which starves the visible output at vote budgets. A
    mandatory-reasoning route rejects the explicit disable with HTTP 400 and
    the standard param-rejection retry then runs the provider default.
    """
    e = (reasoning_effort or "auto").lower()
    if e in ("off", "none"):
        return "none"
    if e == "auto":
        return "low" if looks_reasoning(model_id) else None
    if e == "on":
        return "high"
    if e in ("low", "medium", "high"):
        return e
    return None


def _build_reasoning_param(model_id, reasoning_effort, max_tokens, budget):
    """Return the reasoning dict to embed in the payload, or None.

    An explicit disable sends {"effort": "none"} with no max_tokens cap (a
    token cap is meaningless when reasoning is off; OpenRouter's documented
    effort vocabulary is low|medium|high|none).
    """
    effort = _effort_to_send(reasoning_effort, model_id)
    if effort is None:
        return None
    if effort == "none":
        return {"effort": "none"}
    cap = max(1, int(max_tokens * budget))
    return {"effort": effort, "max_tokens": cap}


def _chat_reservation_slots(model_id, reasoning_effort="auto", max_429_retries=0):
    """Upper-bound provider calls for one governed logical request.

    A provider may reject a reasoning parameter, causing ``chat`` to make one
    fallback request. Panel calls may also make one bounded 429 retry. The
    preflight reservation must cover both possibilities or the ceiling is only
    approximate for reasoning models. An explicit disable ("none") is a
    single-call request -- EXCEPT it may draw the mandatory-reasoning 400 and
    its retry, which the generic two-slot branch already covers ("none" is a
    reasoning parameter and can be rejected like any other).
    """
    effort = _effort_to_send(reasoning_effort, model_id)
    reasoning_slots = 2 if effort is not None else 1
    return reasoning_slots * (max(0, int(max_429_retries)) + 1)


def _ensure_accounted(governor, model, resp, usage):
    """Fill a missing usage.cost before any lane can bill the response.

    A reported zero stays zero (the free tier). A *missing* cost on a free
    model is 0. On a paid model it is estimated from the reported token
    counts against live pricing and flagged cost_estimated; with neither
    cost nor counts the call fails closed -- billing blind is how a paid
    lane silently free-rides past its ceiling.
    """
    if governor.is_free(model):
        usage["cost"] = 0.0
        return
    try:
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
    except (TypeError, ValueError):
        prompt_tokens = completion_tokens = 0
    if not prompt_tokens and not completion_tokens:
        from .errors import HarnessError
        raise HarnessError(
            f"provider omitted usage accounting (no cost, no token counts) "
            f"for paid model '{model}'; refusing to bill blind")
    prompt_price, completion_price = governor.fetch_pricing([model])[model]
    usage["cost"] = prompt_tokens * prompt_price + \
        completion_tokens * completion_price
    usage["cost_estimated"] = True


def chat(transport, api_key, model, messages, max_tokens, reasoning_effort="auto",
         reasoning_token_budget=0.4, governor=None, enable_floor=True,
         max_price=None, provider_sort="price"):
    """One chat completion with the spend governor's payload guards.

    Reasoning is included whenever the effort mode resolves to a value --
    including the explicit disable ("off"/"none" => {"effort": "none"}) --
    and a provider rejection triggers one retry without the reasoning key
    (mandatory-reasoning routes reject the disable with HTTP 400; the retry
    then runs the provider default). A rejected attempt's billable cost is
    merged into the retry response. Every 200 response also passes cost
    accounting: a missing usage.cost is resolved here, once, so no lane can
    bill a paid call as $0.
    """
    canonical_model = strip_variant_suffix(model)
    if governor:
        governor.check_byok(canonical_model)

    def build(with_reasoning):
        model_to_send = floor_model(model, enable_floor=enable_floor)
        payload = {"model": model_to_send, "messages": messages, "max_tokens": max_tokens}
        provider_obj = {}
        if provider_sort:
            provider_obj["sort"] = provider_sort
        if max_price:
            provider_obj["max_price"] = max_price
        if provider_obj:
            payload["provider"] = provider_obj
        if with_reasoning:
            rp = _build_reasoning_param(canonical_model, reasoning_effort, max_tokens,
                                        reasoning_token_budget)
            if rp:
                payload["reasoning"] = rp
        if governor:
            try:
                governor.assert_no_tools(payload, canonical_model)
            except AttributeError:
                pass
        return payload

    def _account(status, resp, payload):
        if status != 200:
            return status, resp

        usage = resp.get("usage") if isinstance(resp, dict) else None
        retry_cost = _retry_cost_value(usage)
        if isinstance(usage, dict):
            cost = usage.get("cost")
            valid_cost = (isinstance(cost, (int, float))
                          and not isinstance(cost, bool)
                          and math.isfinite(cost) and cost >= 0.0)
            if not valid_cost:
                usage.pop("cost", None)
                if governor is not None:
                    try:
                        _ensure_accounted(governor, canonical_model, resp, usage)
                    except HarnessError as exc:
                        unknown = ProviderUsageUnknown(
                            f"provider omitted usage accounting for paid model "
                            f"'{canonical_model}'; refusing to accept a response "
                            "whose cost cannot be established",
                            known_cost=retry_cost)
                        account_unknown_usage(unknown, payload)
                        raise unknown from exc
                elif retry_cost > 0.0:
                    unknown = ProviderUsageUnknown(
                        "provider omitted final usage after a billed retry; "
                        "final request cost is unknown",
                        known_cost=retry_cost)
                    account_unknown_usage(unknown, payload)
                    raise unknown
                else:
                    return status, resp
                # Retry cost is separate until the final attempt's missing
                # provider cost is estimated from its token counts.
                usage["cost"] += retry_cost
            return status, resp

        # A successful HTTP status without an object-shaped usage record is
        # still an unpriced provider call. Preserve any retry charges carried
        # in the synthetic envelope, book those once, and hold liability for
        # this final attempt instead of letting callers accept its content.
        if governor is not None and not governor.is_free(canonical_model):
            unknown = ProviderUsageUnknown(
                f"provider returned no usable usage accounting for paid model "
                f"'{canonical_model}'; refusing to bill blind",
                known_cost=retry_cost)
            account_unknown_usage(unknown, payload)
            raise unknown
        if retry_cost > 0.0:
            unknown = ProviderUsageUnknown(
                "provider omitted final usage after a billed retry; "
                "final request cost is unknown",
                known_cost=retry_cost)
            account_unknown_usage(unknown, payload)
            raise unknown
        return status, resp

    # A model already known to reject the reasoning parameter is called
    # without it from the first attempt, rather than paying for a doomed
    # request and a retry every single time.
    want_reasoning = (_effort_to_send(reasoning_effort, model) is not None
                      and not reasoning_param_rejected(canonical_model))

    def account_unknown_usage(exc, payload, prior_cost=0.0):
        if prior_cost and not getattr(exc, "_chat_prior_cost_added", False):
            exc.add_known_cost(prior_cost)
            exc._chat_prior_cost_added = True
        if not getattr(exc, "usage_unknown", False):
            return
        known_cost = max(0.0, float(getattr(exc, "known_cost", 0.0) or 0.0))
        reserved_cost = 0.0
        if governor is not None and not getattr(exc, "cost_accounted", False):
            if known_cost > 0.0:
                try:
                    governor.record_actual(known_cost, canonical_model)
                except HarnessError:
                    governor.record_overrun(known_cost, canonical_model)
            try:
                prompt_text = "\n".join(
                    str(message.get("content") or "")
                    for message in (payload.get("messages") or [])
                    if isinstance(message, dict))
                prompt_price, completion_price = governor.fetch_pricing(
                    [canonical_model])[canonical_model]
                reserved_cost = (
                    estimate_prompt_tokens(prompt_text) * prompt_price
                    + max(0, int(max_tokens)) * completion_price)
            except Exception:
                # If price data is unavailable after an already-dispatched
                # call, hold the whole configured budget to prevent another
                # paid request from reusing it.
                try:
                    reserved_cost = max(
                        float(governor.max_cost), float(governor.remaining()))
                except Exception:
                    reserved_cost = max(0.01, float(getattr(governor, "max_cost", 0.0) or 0.0))
            retain = getattr(governor, "retain_unknown", None)
            if callable(retain) and reserved_cost > 0.0:
                retain(reserved_cost, canonical_model)
            exc.cost_accounted = True
            exc.reserved_cost = reserved_cost
        emit("provider_usage_unknown", model=canonical_model,
             known_cost=known_cost, reserved_cost=reserved_cost,
             phase="openrouter")

    def request(payload, attempt, prior_cost=0.0):
        started = time.monotonic()
        request_id = uuid.uuid4().hex
        emit("model_request_start", model=model, attempt=attempt,
             request_id=request_id)
        try:
            with provider_request_context(request_id, model, attempt):
                status, resp = transport.post(
                    OPENROUTER_CHAT_URL, api_key, payload)
        except (ToolCancelled, ProviderUsageUnknown) as exc:
            if getattr(exc, "usage_unknown", False):
                account_unknown_usage(exc, payload, prior_cost)
            emit("model_request_end", model=model, attempt=attempt,
                 request_id=request_id,
                 outcome=("cancelled" if isinstance(exc, ToolCancelled)
                          else "error"),
                 usage_unknown=bool(getattr(exc, "usage_unknown", False)),
                 duration_s=round(time.monotonic() - started, 2))
            raise
        except (OSError, TimeoutError) as exc:
            unknown = ProviderUsageUnknown(
                "Provider response was lost after dispatch; usage is unknown "
                "and this request will not be retransmitted automatically.",
                known_cost=prior_cost)
            account_unknown_usage(unknown, payload)
            emit("model_request_end", model=model, attempt=attempt,
                 request_id=request_id, outcome="error", usage_unknown=True,
                 duration_s=round(time.monotonic() - started, 2))
            raise unknown from exc
        except Exception as exc:
            emit("model_request_end", model=model, attempt=attempt,
                 request_id=request_id,
                 outcome=("cancelled" if type(exc).__name__ == "ToolCancelled"
                          else "error"),
                 duration_s=round(time.monotonic() - started, 2))
            raise
        emit("model_request_end", model=model, attempt=attempt,
             request_id=request_id,
             outcome="response", http_status=status,
             duration_s=round(time.monotonic() - started, 2))
        return status, resp

    def account_cancelled(exc, additional_cost=0.0):
        if getattr(exc, "cost_accounted", False):
            return
        exc.add_known_cost(additional_cost)
        if (governor is not None and exc.known_cost > 0.0
                and not exc.cost_accounted):
            try:
                governor.record_actual(exc.known_cost, canonical_model)
            except HarnessError:
                # The provider has already billed this response. Preserve the
                # real spend even when it crossed the configured ceiling, and
                # keep cancellation as the user-visible outcome.
                governor.record_overrun(exc.known_cost, canonical_model)
            exc.cost_accounted = True

    try:
        status, resp = request(build(want_reasoning), "primary")
    except ToolCancelled as exc:
        account_cancelled(exc)
        raise
    if want_reasoning and status != 200:
        err = str(resp.get("error", {}).get("message", resp)
                  if isinstance(resp, dict) else resp).lower()
        if any(h in err for h in _REASONING_PARAM_ERR_HINTS):
            eprint(f"[retry] {model} rejected reasoning param; retrying without it.")
            note_reasoning_param_rejection(canonical_model)
            from . import events as _events
            _events.emit("rotation", model=model, reason="reasoning_param_rejected",
                         note="provider retry without the reasoning parameter")
            prior_cost = _reported_cost(resp)
            try:
                retry_status, retry_resp = request(
                    build(False), "reasoning_retry", prior_cost=prior_cost)
            except ToolCancelled as exc:
                account_cancelled(exc, prior_cost)
                raise
            retry_resp = _merge_retry_cost(retry_resp, prior_cost)
            return _account(retry_status, retry_resp, build(False))
    return _account(status, resp, build(want_reasoning))


def governed_text(transport, api_key, governor, model, prompt, max_tokens,
                  label="ask", reasoning_effort="auto",
                  reasoning_token_budget=0.4):
    """Single-shot governed text call: preflight -> chat -> bill -> extract.

    ONE owner of the request-lane mechanics the panel/judge paths inline
    for their own shapes; plan-lane calls (LLM decomposition, waist
    confirmation) share this instead of re-deriving preflight/billing.
    Returns ``(content, cost)``; content is the raw body (a reasoning-only
    trace still returns, prefixed with REASONING_FALLBACK_PREFIX -- the
    CALLER decides whether a trace satisfies its contract). Raises
    HarnessError on HTTP failure, BYOK routing (recorded, spend invisible),
    or an empty body.
    """
    if governor is None:
        from .errors import HarnessError
        raise HarnessError("governed_text requires a SpendGovernor")
    from .errors import HarnessError
    governor.check_byok(model)
    governor.preflight(prompt, [(label, model, max_tokens, 0)])
    status, resp = chat(transport, api_key, model,
                        [{"role": "user", "content": prompt}],
                        max_tokens, reasoning_effort=reasoning_effort,
                        reasoning_token_budget=reasoning_token_budget,
                        governor=governor)
    if status != 200:
        from .results import _http_error
        raise HarnessError(_http_error(status, resp))
    content, _, cost, is_byok = extract_content_and_cost(resp)
    if is_byok:
        governor.record_byok(model)
        raise HarnessError(
            f"response for {model} was BYOK-routed; spend is not tracked on "
            "this key, so the call is refused")
    if cost:
        governor.record_actual(cost, model)
    if not content or not content.strip():
        raise HarnessError(f"empty response body from {model}")
    return content, cost

def chat_ladder(settings) -> list:
    """Single-owner chat lane ladder: tier-1 head + free panel + paid escalation when allowed.

    HG-ms-parity: the last-resort head comes from the settings pool / config
    constants -- chat.py never hardcodes a provider model id.
    """
    from .config import FREE_JUDGE, FREE_PANEL_POOL
    head = None
    for candidate in (
        getattr(settings, "tier1_model", None),
        getattr(settings, "judge", None),
        (list(getattr(settings, "panel_pool", []) or []) or [None])[0],
        FREE_JUDGE,
        (list(FREE_PANEL_POOL) or [None])[0],
    ):
        if candidate:
            head = candidate
            break
    models = [head] if head else []
    for m in list(getattr(settings, "panel_pool", []) or []):
        if m not in models:
            models.append(m)
    if getattr(settings, "allow_escalation", False) and getattr(settings, "escalation_pool", None):
        for m in getattr(settings, "escalation_pool"):
            if m not in models:
                models.append(m)
    return models
