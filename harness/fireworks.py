"""Confirmed second-lane dispatch and same-model whole-endpoint comparison.

Capability remains the existing model ladder's decision. This owner compares
only reviewed identities of the SAME model, never substitutes a cheaper model.
OpenRouter endpoint rates pass the existing discount gate, and selected offers
are pinned on the wire. Unknown paths, prices, or disabled lanes fail closed.
Reservations and billing remain owned by SpendGovernor, including overruns.
"""
import json
import time
from dataclasses import dataclass
from typing import Optional

from .config import (FIREWORKS_CHAT_URL, MAX_ENDPOINT_FETCHES_PER_RUN,
                     is_fireworks_model_path)
from .endpoint_pricing import FireworksOffer, fireworks_offers, fetch_endpoints_for
from .errors import HarnessError
from .tokens import estimate_prompt_tokens
from .validation import finite_number

PROVIDER_OPENROUTER = "openrouter"
PROVIDER_FIREWORKS = "fireworks"


@dataclass(frozen=True)
class Route:
    """One confirmed wire identity with one whole endpoint's prices."""

    provider: str
    wire_model: str
    route_reason: str
    offer: Optional[FireworksOffer] = None
    endpoint: object = None
    rates: tuple = ()


def resolve_offer(model):
    """Refuse a path absent from the reviewed routable price pack."""
    for offer in fireworks_offers():
        if offer.path == model and offer.routable:
            return offer
    raise HarnessError(f"no confirmed Fireworks endpoint for '{model}'")


def resolve_route(model, *, openrouter_enabled, fireworks_enabled):
    """Explicit paths never silently switch providers."""
    if is_fireworks_model_path(model):
        if not fireworks_enabled:
            raise HarnessError("fireworks is disabled")
        return Route(PROVIDER_FIREWORKS, model, "explicit_fireworks_path", resolve_offer(model))
    if not openrouter_enabled:
        raise HarnessError("OpenRouter is disabled")
    return Route(PROVIDER_OPENROUTER, model, "openrouter_default")


def cost_estimate(offer, prompt_tokens, completion_tokens):
    """Uncached upper bound; no optimistic cache-hit assumption."""
    return prompt_tokens * offer.price.prompt + completion_tokens * offer.price.completion


def comparable_model(model):
    """Whether a reviewed same-model mapping exists in the current pack."""
    return any(o.routable and o.canonical_model == model for o in fireworks_offers())


def routes_for(governor, model, input_tokens, output_tokens):
    """Cheapest-first, same-model offers; one bounded endpoint fetch per identity."""
    if is_fireworks_model_path(model):
        route = resolve_route(model, openrouter_enabled=governor.openrouter_enabled,
                              fireworks_enabled=governor.fireworks_enabled)
        if (route.offer.price.context_length
                and input_tokens + output_tokens > route.offer.price.context_length):
            raise HarnessError("Fireworks model context limit exceeded")
        return [(cost_estimate(route.offer, input_tokens, output_tokens), route)]
    offers = [o for o in fireworks_offers() if o.routable and o.canonical_model == model]
    candidates = []
    if governor.fireworks_enabled and governor.fireworks_key:
        for offer in offers:
            if offer.price.context_length and input_tokens + output_tokens > offer.price.context_length:
                continue
            route = Route(PROVIDER_FIREWORKS, offer.path, "same_model_endpoint", offer)
            candidates.append((cost_estimate(offer, input_tokens, output_tokens), route))
    if governor.openrouter_enabled and governor.api_key:
        if offers:
            from .discount_gate import resolve_discount_semantics
            with governor._spend_lock:
                if model not in governor._endpoint_cache:
                    if len(governor._endpoint_cache) >= MAX_ENDPOINT_FETCHES_PER_RUN:
                        raise HarnessError("endpoint fetch budget exhausted")
                    fetched = fetch_endpoints_for(governor.transport, governor.api_key, [model])
                    if model in fetched["errors"]:
                        governor._endpoint_cache[model] = None
                    else:
                        governor._endpoint_cache[model] = fetched["priced"][model]
                endpoints = governor._endpoint_cache[model]
            if endpoints is not None:
                semantics = resolve_discount_semantics(endpoints)
                for endpoint in endpoints.endpoints:
                    if endpoint.context_length and input_tokens + output_tokens > endpoint.context_length:
                        continue
                    if endpoint.max_completion_tokens and output_tokens > endpoint.max_completion_tokens:
                        continue
                    if endpoint.uptime_30m is not None and endpoint.uptime_30m <= 0:
                        continue
                    rates = endpoint.rates_for(input_tokens, True, semantics)
                    cost = input_tokens * rates[0] + output_tokens * rates[1]
                    candidates.append((cost, Route(PROVIDER_OPENROUTER, model,
                                                   "same_model_endpoint", endpoint=endpoint,
                                                   rates=rates)))
        else:
            rates = governor.fetch_pricing([model])[model]
            candidates.append((input_tokens * rates[0] + output_tokens * rates[1],
                               Route(PROVIDER_OPENROUTER, model, "openrouter_default",
                                     rates=rates)))
    if not candidates:
        raise HarnessError(f"no confirmed enabled route for '{model}'")
    return sorted(candidates, key=lambda row: (row[0], row[1].provider, row[1].wire_model))


def _account_response(route, status, response):
    """Require valid usage on successful paid calls; estimate only from reported counts."""
    if not isinstance(response, dict):
        raise HarnessError("provider returned no response object; refusing to bill blind")
    usage = response.get("usage")
    if not isinstance(usage, dict):
        if status == 200:
            raise HarnessError("provider omitted usage; refusing to bill blind")
        return 0.0
    if usage.get("cost") is None:
        try:
            prompt = finite_number(usage.get("prompt_tokens"), "prompt_tokens", 0)
            completion = finite_number(usage.get("completion_tokens"), "completion_tokens", 0)
        except HarnessError:
            raise HarnessError("provider omitted token accounting; refusing to bill blind") from None
        if route.offer:
            cost = cost_estimate(route.offer, prompt, completion)
        else:
            cost = prompt * route.rates[0] + completion * route.rates[1]
        usage.update(cost=cost, cost_estimated=True)
    return finite_number(usage["cost"], "reported cost", 0)


def dispatch(transport, api_key, model, messages, max_tokens, governor,
             reasoning_effort="auto", reasoning_token_budget=0.4, max_price=None,
             enable_floor=True, provider_sort="price"):
    """Bounded provider failover with one reservation and settlement per attempt.

    Only an explicit HTTP 429/5xx with zero billed usage may fail over. Ambiguous
    transport failures book the reserved upper bound and stop, rather than
    retrying a potentially executed request or making its liability vanish.
    Legacy callers use record_response so settled responses are not billed twice.
    """
    if governor is None:
        raise HarnessError("second-provider dispatch requires a SpendGovernor")
    from .chat import _chat_openrouter
    from .events import emit
    input_tokens = estimate_prompt_tokens(json.dumps(messages, ensure_ascii=False))
    candidates = routes_for(governor, model, input_tokens, max_tokens)
    comparison = {"model": model, "input_tokens_estimated": input_tokens,
                  "max_output_tokens": max_tokens,
                  "offers": [{"provider": r.provider, "wire_model": r.wire_model,
                              "endpoint": r.endpoint.name if r.endpoint else None,
                              "worst_case": cost} for cost, r in candidates]}
    governor.route_comparisons.append(comparison)
    emit("provider_comparison", **comparison)
    for index, (worst, route) in enumerate(candidates):
        if route.provider == PROVIDER_FIREWORKS and not governor.fireworks_key:
            raise HarnessError("Fireworks key missing")
        governor.check_byok(model)
        if max_price:
            pp, cp = ((route.offer.price.prompt, route.offer.price.completion)
                      if route.offer else route.rates)
            if (pp * 1e6 > max_price.get("prompt", float("inf"))
                    or cp * 1e6 > max_price.get("completion", float("inf"))):
                if index + 1 < len(candidates):
                    continue
                raise HarnessError("selected endpoint exceeds max_price")
        try:
            token = governor.reserve(worst, route.wire_model)
        except HarnessError:
            if not is_fireworks_model_path(model) and index + 1 < len(candidates):
                continue
            raise
        start = time.monotonic()
        try:
            if route.provider == PROVIDER_FIREWORKS:
                payload = {"model": route.wire_model, "messages": messages, "max_tokens": max_tokens}
                governor.assert_no_tools(payload, model)
                post = getattr(transport, "post_once", transport.post)
                status, response = post(FIREWORKS_CHAT_URL, governor.fireworks_key, payload)
            else:
                status, response = _chat_openrouter(
                    transport, api_key, route.wire_model, messages, max_tokens,
                    reasoning_effort, reasoning_token_budget, governor,
                    enable_floor=enable_floor if route.endpoint is None else False,
                    max_price=max_price, provider_sort=provider_sort,
                    provider_name=route.endpoint.provider_name if route.endpoint else None)
            cost = _account_response(route, status, response)
            if bool((response.get("usage") or {}).get("is_byok")):
                raise HarnessError("provider response used BYOK; spend cannot be verified")
        except Exception:
            governor.reconcile(token, worst)
            raise
        try:
            governor.reconcile(token, cost)
        except HarnessError:
            governor.record_overrun(cost, route.wire_model)
            raise
        governor._accounted_responses[id(response)] = response
        receipt = {"provider": route.provider, "wire_model": route.wire_model,
                   "endpoint": route.endpoint.name if route.endpoint else None,
                   "cost": cost, "cost_estimated": bool((response.get("usage") or {}).get("cost_estimated")),
                   "elapsed_s": round(time.monotonic() - start, 3), "http_status": status}
        comparison.setdefault("attempts", []).append(receipt)
        response["provider_route"] = receipt
        emit("provider_result", model=model, **receipt)
        if (not is_fireworks_model_path(model) and cost == 0
                and (status == 429 or 500 <= status <= 599) and index + 1 < len(candidates)):
            continue
        return status, response
    raise HarnessError("provider candidates exhausted")
