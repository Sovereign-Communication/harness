"""EV-0 -- per-provider endpoint evidence: what a model actually costs.

Why this module exists
----------------------
Harness priced every model from ``/models``, which publishes a single
*aggregate* rate.  The per-provider feed ``/api/v1/models/{slug}/endpoints``
publishes the things that actually decide what a call costs, and none of them
were read (DF-EV-2):

* ``discount`` -- a promotion, per endpoint.
* ``overrides`` -- a long-context price step (e.g. above 272k prompt tokens
  the rate doubles), which no other source exposes.
* ``input_cache_read`` -- cached input is an order of magnitude cheaper and
  is invisible in the aggregate.
* published ``uptime``/``latency`` instead of discovering a 429 by suffering
  it.
* the real per-provider spread.  Live on 2026-10-03 the SAME model id
  ``deepseek/deepseek-v4.1-flash`` was ``$0.003/$2.40`` per Mtok on one
  provider and ``$0.02/$0.40`` on another -- inverted in/out.  Any reserve
  built from the aggregate is wrong by up to ~6x on output.

Invariant 1: **never mix per-endpoint prices.**  Cheapest-in from one provider
plus cheapest-out from another is a number no provider will charge.  Every
cost is computed *per endpoint* (:meth:`EndpointPrice.blended_usd`), so a
consumer chooses among whole offers rather than blending fields.

The feed is metered, so fetching is bounded rather than catalog-scoped:
:func:`fetch_endpoints_for` is the ONE owner of that bound and the only way a
caller is meant to reach the feed.  ``fetch_endpoints`` is the single-model
primitive underneath it, not a path around it.

Part 1 of 4 in EV-0's evidence layer: ``endpoint_pricing`` (this module),
``benchmark_ingest``, ``discount_probe``, ``discount_gate``.  Canon lives in
``docs/jev-roadmap.md`` (``EV-*``).
"""
import json
from dataclasses import dataclass, field

from pathlib import Path

from .config import (DISCOUNT_IS_MULTIPLIER, FIREWORKS_PROVIDER,
                      MAX_ENDPOINT_FETCHES_PER_RUN, OPENROUTER_ENDPOINTS_URL)
from .errors import HarnessError
from .events import emit
from .output import eprint
from .routing_table import strip_variant_suffix
from .validation import finite_number, optional_float


@dataclass(frozen=True)
class EndpointPrice:
    """One provider's offer for one model, exactly as published."""

    provider_name: str = "unknown"
    # Per-token dollars, exactly as OpenRouter publishes them. The
    # `fetch_pricing` comment records the earlier /1e6 bug: these are
    # already per-token and must never be divided again.
    prompt: float = 0.0
    completion: float = 0.0
    tag: object = None
    discount: float = 0.0
    overrides: tuple = ()
    # Published and parsed, deliberately not priced here: whether a given
    # call may use the cached-input rate is a routing decision that belongs
    # to EV-1's selection policy, not to ingest.
    input_cache_read: object = None
    context_length: int = 0
    max_completion_tokens: int = 0
    uptime_1d: object = None
    uptime_30m: object = None
    latency_30m: object = None
    mandatory_reasoning: bool = False

    def __post_init__(self):
        # Frozen dataclass: every normalized field goes through
        # object.__setattr__, the same coercion seam CapabilityProfile uses,
        # so the arithmetic below can trust its inputs.
        set_ = object.__setattr__
        set_(self, "provider_name", str(self.provider_name or "unknown"))
        set_(self, "prompt",
             finite_number(self.prompt, "endpoint pricing.prompt"))
        set_(self, "completion",
             finite_number(self.completion, "endpoint pricing.completion"))
        set_(self, "discount",
             finite_number(self.discount, "endpoint pricing.discount", 0.0, 1.0))
        set_(self, "overrides", tuple(self.overrides or ()))
        set_(self, "input_cache_read", optional_float(self.input_cache_read))
        set_(self, "context_length", int(self.context_length or 0))
        set_(self, "max_completion_tokens", int(self.max_completion_tokens or 0))
        set_(self, "uptime_1d", optional_float(self.uptime_1d))
        set_(self, "uptime_30m", optional_float(self.uptime_30m))
        set_(self, "latency_30m", optional_float(self.latency_30m))
        set_(self, "mandatory_reasoning", bool(self.mandatory_reasoning))

    # -- price arithmetic -------------------------------------------------
    def rates_for(self, input_tokens, apply_discount, semantics):
        """(prompt, completion) per-token rates for a call of this size.

        ``overrides`` are a published price *step* keyed on prompt volume:
        the deepest override whose ``min_prompt_tokens`` the call reaches
        wins.  ``apply_discount`` is decided by the caller from the recorded
        semantics verdict, never guessed here.
        """
        rate_in, rate_out = self.prompt, self.completion
        for override in sorted(self.overrides,
                               key=lambda o: int(o.get("min_prompt_tokens", 0))):
            if input_tokens >= int(override.get("min_prompt_tokens", 0)):
                rate_in = optional_float(override.get("prompt"))
                rate_out = optional_float(override.get("completion"))
        if rate_in is None:
            rate_in = self.prompt
        if rate_out is None:
            rate_out = self.completion
        if apply_discount and semantics == DISCOUNT_IS_MULTIPLIER and self.discount:
            keep = 1.0 - self.discount
            rate_in *= keep
            rate_out *= keep
        return rate_in, rate_out

    def blended_usd(self, input_tokens, output_tokens, *, semantics):
        """Dollar cost of one call at this endpoint. Never mixes endpoints."""
        rate_in, rate_out = self.rates_for(input_tokens, True, semantics)
        return (input_tokens * rate_in) + (output_tokens * rate_out)

    @property
    def name(self):
        """Human-facing identity, used in the probe's match lists."""
        return f"{self.provider_name}|{self.tag}" if self.tag else self.provider_name

    def to_dict(self):
        return {
            "provider": self.provider_name, "tag": self.tag,
            "name": self.name,
            "prompt_per_token": self.prompt, "completion_per_token": self.completion,
            "discount": self.discount, "overrides": list(self.overrides),
            "input_cache_read": self.input_cache_read,
            "context_length": self.context_length,
            "max_completion_tokens": self.max_completion_tokens,
            "uptime_1d": self.uptime_1d, "uptime_30m": self.uptime_30m,
            "latency_30m": self.latency_30m,
            "mandatory_reasoning": self.mandatory_reasoning,
        }

    def __repr__(self):  # pragma: no cover - debug aid
        return (f"<EndpointPrice {self.name} in={self.prompt * 1e6:.4f} "
                f"out={self.completion * 1e6:.4f}/Mtok disc={self.discount}>")


@dataclass(frozen=True)
class ModelEndpoints:
    """Every published provider offer for one model id."""

    model_id: str
    endpoints: tuple = field(default_factory=tuple)

    def __post_init__(self):
        object.__setattr__(self, "endpoints", tuple(self.endpoints or ()))

    @property
    def max_discount(self):
        """The deepest published promotion, or 0.0 when none is running."""
        return max((e.discount for e in self.endpoints), default=0.0)

    @property
    def has_discount(self):
        return self.max_discount > 0.0

    def __repr__(self):  # pragma: no cover - debug aid
        return f"<ModelEndpoints {self.model_id} n={len(self.endpoints)}>"


def _parse_endpoints(model_id, payload):
    """Build ModelEndpoints from a raw /endpoints body.

    Raises rather than degrading: a silently half-parsed price table is worse
    than no price table, because every number downstream would still look
    plausible.
    """
    if not isinstance(payload, dict):
        raise HarnessError(f"endpoints feed for {model_id} returned "
                           f"{type(payload).__name__}, not an object")
    body = payload.get("data")
    if isinstance(body, dict):
        body = body.get("endpoints")
    if not isinstance(body, list):
        raise HarnessError(f"endpoints feed for {model_id} has no endpoint list")

    parsed = []
    for raw in body:
        if not isinstance(raw, dict):
            continue
        pricing = raw.get("pricing")
        pricing = pricing if isinstance(pricing, dict) else {}
        overrides = []
        for override in pricing.get("overrides") or []:
            if isinstance(override, dict):
                overrides.append({
                    "min_prompt_tokens": int(
                        optional_float(override.get("min_prompt_tokens")) or 0),
                    "prompt": override.get("prompt"),
                    "completion": override.get("completion"),
                })
        parsed.append(EndpointPrice(
            raw.get("provider_name") or raw.get("name") or "unknown",
            pricing.get("prompt", 0), pricing.get("completion", 0),
            tag=raw.get("tag"),
            discount=pricing.get("discount", 0) or 0,
            overrides=overrides,
            input_cache_read=pricing.get("input_cache_read"),
            context_length=raw.get("context_length"),
            max_completion_tokens=raw.get("max_completion_tokens"),
            uptime_1d=raw.get("uptime_last_1d"),
            uptime_30m=raw.get("uptime_last_30m"),
            latency_30m=raw.get("latency_last_30m"),
            mandatory_reasoning="reasoning" in (raw.get("supported_parameters") or [])
            and bool(raw.get("mandatory_reasoning")),
        ))
    if not parsed:
        raise HarnessError(f"endpoints feed for {model_id} contained no "
                           f"usable provider offers")
    return ModelEndpoints(model_id, tuple(parsed))


def fetch_endpoints(transport, api_key, model_id, *, timeout=20):
    """One GET for one model's per-provider offers.

    Deliberately one request per model: the feed is metered at 30 requests
    per minute and 500 per day per account, and this feed is the reason
    ``fetch_endpoints_for`` refuses a catalog-wide sweep by default.
    """
    canonical = strip_variant_suffix(model_id)
    url = OPENROUTER_ENDPOINTS_URL.format(slug=canonical)
    payload = transport.get(url, api_key, timeout=timeout)
    if isinstance(payload, dict) and payload.get("error"):
        raise HarnessError(f"endpoints feed for {model_id} returned an error: "
                           f"{payload['error'].get('message', payload['error'])}")
    endpoints = _parse_endpoints(model_id, payload)
    emit("economics_endpoints", model=canonical, endpoints=len(endpoints.endpoints),
         max_discount=endpoints.max_discount)
    return endpoints


def fetch_endpoints_for(transport, api_key, model_ids, *,
                        max_fetches=MAX_ENDPOINT_FETCHES_PER_RUN):
    """Fetch endpoints for an explicit shortlist. THE ONE OWNER OF THE BOUND.

    Every caller goes through here. That is the whole point: when the bound
    check lived here while the shipped report looped :func:`fetch_endpoints`
    around it, the production path issued one GET per requested model and
    reported the budget it had been given (and with no flag, ``[:None]`` made
    the default unbounded). A guard that only the tests call is not a guard.
    That report has since been removed as unrequested scope (DF-EV-12); this
    function and its bound are what survive, and the probe now routes through
    here rather than around it.

    The bound is checked BEFORE any request is issued, and it refuses rather
    than truncating: a silently partial result reads as full coverage, which
    is the failure this module exists to prevent.

    Returns ``{"priced": {model_id: ModelEndpoints}, "errors": {model_id:
    message}}``. Per-model failures are isolated rather than raised, because
    one model leaving the catalog must not cost a caller every other price --
    but the caller is expected to make each error VISIBLE, since a model
    quietly absent from a result is exactly the silent-degradation defect this
    module's own docstring rejects.
    """
    # Strip BEFORE deduping: `a/m` and `a/m:free` are one model, and issuing
    # a GET for each would spend the daily request budget twice on one row.
    ids = list(dict.fromkeys(strip_variant_suffix(m)
                             for m in (model_ids or [])))
    if len(ids) > max_fetches:
        raise HarnessError(
            f"endpoint fetch budget is {max_fetches} models per run; {len(ids)} "
            f"requested. Endpoint coverage is shortlist-scoped by design "
            f"(DF-EV-2): pass only the ids you route to, or raise "
            f"max_fetches explicitly. No requests were issued.")
    priced, errors = {}, {}
    for model_id in ids:
        try:
            priced[model_id] = fetch_endpoints(transport, api_key, model_id)
        except HarnessError as exc:
            errors[model_id] = str(exc)
            # Announced HERE, at the point of isolation, because this is the
            # only place the failure is guaranteed to be seen by someone. A
            # shipped model that quietly drops out of a result is precisely
            # the silent degradation this module's docstring rejects, and a
            # live run did exactly that: `cohere/north-mini-code` errored with
            # a clean stderr and a plausible artifact.
            eprint(f"[warn] economics: endpoint feed failed for {model_id}: {exc}")
    return {"priced": priced, "errors": errors}


# -- EV-6: Fireworks as a second source of the same endpoint row ----------
# The committed pack (generated from the dated snapshot by
# audits/self/refresh_fireworks_pack.py) is the only file read here. Rates
# are published per 1M tokens and converted to per-token dollars, the unit
# EndpointPrice already uses. A model is routable only with a confirmed
# model path; every other row stays priced evidence.
FIREWORKS_PACK = Path(__file__).resolve().parent.parent / "packs" / "fireworks.endpoints.json"
_PER_MILLION = 1_000_000.0


@dataclass(frozen=True)
class FireworksOffer:
    """One live-confirmed Fireworks Standard offer for one model."""

    model: str
    path: object  # accounts/... when the model path is confirmed, else None
    price: EndpointPrice

    @property
    def routable(self):
        return self.path is not None


def fireworks_offers(pack_path=FIREWORKS_PACK):
    """Every Fireworks offer in the committed pack, as per-endpoint prices.

    A missing or malformed pack raises rather than returning an empty list:
    an empty answer would read as "Fireworks has no offers" when the
    evidence is actually missing.
    """
    try:
        text = Path(pack_path).read_text(encoding="utf-8")
    except FileNotFoundError:
        raise HarnessError(f"Fireworks price pack missing: {pack_path}") from None
    doc = json.loads(text)
    models = doc.get("models") if isinstance(doc, dict) else None
    if not isinstance(models, list):
        raise HarnessError(f"Fireworks price pack has no model list: {pack_path}")
    offers = []
    for row in models:
        price = EndpointPrice(
            FIREWORKS_PROVIDER,
            prompt=float(row["input"]) / _PER_MILLION,
            completion=float(row["output"]) / _PER_MILLION,
            input_cache_read=float(row["cached_input"]) / _PER_MILLION,
        )
        offers.append(FireworksOffer(row["name"], row.get("path"), price))
    return offers
