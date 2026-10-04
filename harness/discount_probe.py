"""EV-0a -- the discount-truth probe: what does a published `discount` mean?

The finding that gates every later cost decision.  OpenRouter publishes a
per-endpoint ``discount`` beside the listed rates without documenting whether
the promotion is already baked in.  The two readings differ by exactly 2x at
``discount=0.5``: every ranking, reserve, and future auto-rotation decision
inverts on the answer, so it is settled by MEASUREMENT here, not by reading
docs.

One tiny governed call, then the provider-reported charge is compared against
both hypotheses across every published offer -- the request may have been
served by any one of them.  Outcomes: ``listed_is_effective`` /
``discount_is_multiplier`` / ``ambiguous`` / ``unresolved``.  The last two are
reported honestly rather than rounded to a guess, because provider price
aliasing can make both readings fit one charge.

Two refusals worth naming:

* the POST is issued directly rather than through ``chat``, because ``chat``
  fills a missing ``usage.cost`` from the ``/models`` aggregate -- and
  synthesizing the very number under test would make the probe circular;
* the endpoint feed is fetched THROUGH
  :func:`~harness.endpoint_pricing.fetch_endpoints_for`, the one owner of the
  per-run fetch bound, because a probe that looped the single-model fetch
  would be the DF-EV-9 shape all over again.

A conclusive verdict becomes committed repo evidence through
``discount_gate``; this module only measures and reports.

Part 3 of 4 in EV-0's evidence layer: ``endpoint_pricing``,
``benchmark_ingest``, ``discount_probe`` (this module), ``discount_gate``.
Canon lives in ``docs/jev-roadmap.md`` (``EV-*``).
"""
import time

from .chat import extract_content_and_cost
from .config import (DISCOUNT_AMBIGUOUS, DISCOUNT_IS_MULTIPLIER,
                     DISCOUNT_LISTED_IS_EFFECTIVE, DISCOUNT_PROBE_MAX_TOKENS,
                     DISCOUNT_PROBE_PROMPT, DISCOUNT_PROBE_TOLERANCE,
                     DISCOUNT_UNRESOLVED, ECONOMICS_SCHEMA_VERSION,
                     OPENROUTER_CHAT_URL)
from .endpoint_pricing import fetch_endpoints_for
from .errors import HarnessError
from .events import emit
from .output import eprint
from .routing_table import floor_model, strip_variant_suffix
from .validation import optional_float


# The discount-semantics probe
# --------------------------------------------------------------------------
def _matches(actual, predicted, tolerance):
    if predicted is None:
        return False
    return abs(actual - predicted) <= max(1e-12, tolerance * abs(predicted))


def run_discount_probe(transport, api_key, governor, model_id, *,
                       max_tokens=DISCOUNT_PROBE_MAX_TOKENS,
                       tolerance=DISCOUNT_PROBE_TOLERANCE,
                       prompt=DISCOUNT_PROBE_PROMPT):
    """Measure one tiny call and decide what ``discount`` means.

    The question is binary -- does the published rate already include the
    promotion, or must it be multiplied down -- and it is answered by
    comparing the provider-reported charge against both hypotheses across
    every published endpoint, since the request may have been served by any
    one of them.

    The POST is issued directly rather than through ``chat`` on purpose:
    ``chat`` fills a missing ``usage.cost`` from the ``/models`` aggregate,
    and synthesizing the very number under test would make the probe
    circular. A response without a provider-reported cost is therefore
    reported as unresolved instead of being patched up.

    Ambiguity is a real outcome, not a bug to paper over. Providers alias
    prices (one endpoint's listed rate equals another's post-discount rate),
    and then both hypotheses fit. The probe reports ``ambiguous`` with the
    matching endpoints named, and the gate keeps refusing until a model is
    found whose offers do not collide.
    """
    canonical = strip_variant_suffix(model_id)
    # Through the bound owner, never around it. One probe is one GET, and the
    # owner is what keeps the metered feed intact when a caller fans out.
    fetched = fetch_endpoints_for(transport, api_key, [model_id],
                                 max_fetches=1)
    if canonical not in fetched["priced"]:
        # fetch_endpoints_for isolates a per-model failure so one dead model
        # cannot cost a whole report its prices. A probe has no other model to
        # fall back on, so re-raise instead of reporting an unresolved verdict
        # for a measurement that never happened.
        raise HarnessError(fetched["errors"].get(
            canonical, f"endpoint feed returned no usable offers for "
                       f"{model_id}"))
    endpoints = fetched["priced"][canonical]
    if not endpoints.has_discount:
        raise HarnessError(
            f"{model_id} publishes no discount on any endpoint "
            f"(max={endpoints.max_discount}); it cannot settle discount "
            f"semantics. Pick a model with a running promotion.")
    if governor is not None:
        governor.check_byok(canonical)
        governor.preflight(prompt, [(f"discount-probe {canonical}",
                                     canonical, max_tokens, 0)])

    payload = {"model": floor_model(model_id), "max_tokens": max_tokens,
               "messages": [{"role": "user", "content": prompt}]}
    emit("economics_discount_probe", model=canonical, phase="start",
         max_discount=endpoints.max_discount)
    status, resp = transport.post(OPENROUTER_CHAT_URL, api_key, payload, timeout=60)

    record = {
        "schema": ECONOMICS_SCHEMA_VERSION,
        "model": canonical,
        "observed_at": time.time(),
        "tolerance": tolerance,
        "max_discount": endpoints.max_discount,
        "endpoints": [e.to_dict() for e in endpoints.endpoints],
    }

    if status != 200:
        record.update({"semantics": DISCOUNT_UNRESOLVED,
                       "reason": f"probe returned HTTP {status}",
                       "detail": str(resp)[:300]})
        emit("economics_discount_probe", model=canonical, phase="end",
             semantics=DISCOUNT_UNRESOLVED, status=status)
        return record

    usage = resp.get("usage") if isinstance(resp, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    actual = optional_float(usage.get("cost"))
    prompt_tokens = optional_float(usage.get("prompt_tokens"))
    completion_tokens = optional_float(usage.get("completion_tokens"))

    # BYOK routing makes the charge invisible to this key: the response price
    # is not what the account pays, so it cannot settle anything.
    _content, _finish, _cost, is_byok = extract_content_and_cost(resp)
    if is_byok:
        record.update({"semantics": DISCOUNT_UNRESOLVED,
                       "reason": "probe response was BYOK-routed; the charge "
                                 "is not on this key"})
        emit("economics_discount_probe", model=canonical, phase="end",
             semantics=DISCOUNT_UNRESOLVED, reason="byok")
        return record

    if actual is None or actual <= 0:
        record.update({"semantics": DISCOUNT_UNRESOLVED,
                       "reason": "response carried no provider-reported "
                                 "usage.cost; synthesizing one from the "
                                 "/models aggregate would be circular"})
        emit("economics_discount_probe", model=canonical, phase="end",
             semantics=DISCOUNT_UNRESOLVED, reason="no_reported_cost")
        return record
    if prompt_tokens is None or completion_tokens is None:
        record.update({"semantics": DISCOUNT_UNRESOLVED,
                       "reason": "response carried no itemized token usage"})
        emit("economics_discount_probe", model=canonical, phase="end",
             semantics=DISCOUNT_UNRESOLVED, reason="no_token_usage")
        return record

    in_t, out_t = int(prompt_tokens), int(completion_tokens)
    predictions = {}
    for semantics in (DISCOUNT_LISTED_IS_EFFECTIVE, DISCOUNT_IS_MULTIPLIER):
        matched, per_endpoint = [], {}
        for endpoint in endpoints.endpoints:
            predicted = endpoint.blended_usd(in_t, out_t, semantics=semantics)
            per_endpoint[endpoint.name] = predicted
            if _matches(actual, predicted, tolerance):
                matched.append(endpoint.name)
        predictions[semantics] = {"matched": matched, "per_endpoint": per_endpoint}

    record["measured"] = {
        "cost_basis": "provider_reported_usage.cost",
        "actual_cost_usd": actual,
        "prompt_tokens": in_t,
        "completion_tokens": out_t,
        "predictions": predictions,
    }

    listed_hits = predictions[DISCOUNT_LISTED_IS_EFFECTIVE]["matched"]
    multiplier_hits = predictions[DISCOUNT_IS_MULTIPLIER]["matched"]
    if listed_hits and multiplier_hits:
        semantics, reason = DISCOUNT_AMBIGUOUS, (
            "both hypotheses match a published offer -- provider prices "
            "alias, so this model cannot settle the question")
    elif listed_hits:
        semantics, reason = DISCOUNT_LISTED_IS_EFFECTIVE, (
            "measured charge matches the listed rate, so the promotion is "
            "already applied")
    elif multiplier_hits:
        semantics, reason = DISCOUNT_IS_MULTIPLIER, (
            "measured charge matches the listed rate times (1 - discount), "
            "so the promotion must still be applied")
    else:
        semantics, reason = DISCOUNT_UNRESOLVED, (
            "measured charge matches no published offer under either "
            "hypothesis")

    record.update({"semantics": semantics, "reason": reason})
    if semantics in (DISCOUNT_LISTED_IS_EFFECTIVE, DISCOUNT_IS_MULTIPLIER):
        # Fingerprint the promotion this verdict was measured against. The
        # question is a property of how the platform accounts, but the
        # answer is only valid while that promotion is the one running: a
        # model whose discount has since changed may not satisfy the same
        # code path, so the gate re-checks it rather than trusting an old
        # receipt forever.
        record["fingerprint"] = {"model": canonical,
                                 "max_discount": endpoints.max_discount}

    emit("economics_discount_probe", model=canonical, phase="end",
         semantics=semantics, actual_cost_usd=actual)
    eprint(f"[economics] discount probe {canonical}: {semantics} "
           f"(measured ${actual:.8f} over {in_t}in/{out_t}out; {reason})")
    return record
