"""EV-0 -- model economics evidence: benchmark ingest, per-provider endpoint
pricing, and the discount-semantics gate.

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

``/api/v1/benchmarks`` publishes capability evidence that did not exist here
at all (DF-EV-1): Artificial Analysis ``coding_index``/``agentic_index``/
``intelligence_index``, Design Arena, and OpenRouter's own tau-bench / GPQA /
web-search evals with ``accuracy`` and ``avg_cost_per_task``.

Two invariants this module enforces
-----------------------------------
1. **Never mix per-endpoint prices.**  Cheapest-in from one provider plus
   cheapest-out from another is a number no provider will charge.  Every cost
   is computed *per endpoint*; only then is a best endpoint chosen.

2. **The discount gate.**  OpenRouter publishes ``discount`` next to the
   listed rates without documenting whether the discount is already baked
   into them.  The two readings differ by exactly 2x at ``discount=0.5``:

   * ``listed_is_effective`` -- the listed rate is what you pay.
   * ``discount_is_multiplier`` -- you pay ``listed * (1 - discount)``.

   Every ranking, reserve, and future auto-rotation decision inverts on that
   answer, so it is settled by MEASUREMENT (:func:`run_discount_probe`), not
   by reading docs, and :func:`effective_price` refuses to return a number
   until the verdict is in the COMMITTED receipt and still applicable.  An
   unknown verdict is a hard failure -- never a default, because a default
   here is a silent 2x error in the cost model of the whole system.

   The verdict's *location* is part of that.  It is a repo-committed receipt
   under ``audits/self/dogfood/`` (see :func:`discount_verdict_path`), not
   state under the operator's config dir: a verdict that authorizes every
   downstream cost decision has to be reviewable evidence travelling with the
   code, and two checkouts must never disagree about whether the gate is
   satisfied.  It is fail-closed in exactly the same three cases as before --
   missing, unreadable, or measured against a different promotion.

Scope
-----
EV-0 only: evidence in, evidence out.  There is deliberately no
cost-per-capability index, no tier snapshot, no routing change, and no
automatic pool movement here; those are EV-1..EV-4 and they consume this
module through :func:`effective_price`, which is why the gate has to be real
before they land.
"""
import json
import os
import time
from dataclasses import dataclass, field

from ._http import HttpTransport
from .chat import extract_content_and_cost
from .config import (
    BENCHMARK_SOURCES,
    DISCOUNT_AMBIGUOUS,
    DISCOUNT_IS_MULTIPLIER,
    DISCOUNT_LISTED_IS_EFFECTIVE,
    DISCOUNT_NOT_APPLICABLE,
    DISCOUNT_PROBE_MAX_TOKENS,
    DISCOUNT_PROBE_PROMPT,
    DISCOUNT_PROBE_TOLERANCE,
    DISCOUNT_SEMANTICS_VALUES,
    DISCOUNT_UNRESOLVED,
    ECONOMICS_RECEIPT_DIR,
    ECONOMICS_SCHEMA_VERSION,
    ECONOMICS_VERDICT_PATH,
    MAX_ENDPOINT_FETCHES_PER_RUN,
    OPENROUTER_BENCHMARKS_URL,
    OPENROUTER_CHAT_URL,
    OPENROUTER_ENDPOINTS_URL,
    shipped_model_ids,
)
from .errors import HarnessError
from .events import emit
from .osal import write_text
from .output import eprint
from .routing_table import floor_model, strip_variant_suffix
from .validation import finite_number

#: The Harness checkout that owns the committed discount-verdict receipt.
#: Resolved from this file rather than the CWD so the gate asks the same
#: question in every checkout (an installed wheel resolves to site-packages,
#: which has no receipt -- and the gate then refuses, which is correct: a
#: wheel carries code, not evidence).
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _opt_float(value):
    """A missing/blank/garbage optional number is None, never an exception.

    ``/models`` and ``/endpoints`` both ship string prices and omit keys
    freely (``input_cache_read`` and ``discount`` are absent on most rows).
    Absent is normal data here; a malformed *required* price is not, and that
    path raises through ``finite_number`` instead.
    """
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    # NaN/inf are not prices.
    return result if result == result and result not in (float("inf"), float("-inf")) else None


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
        set_(self, "input_cache_read", _opt_float(self.input_cache_read))
        set_(self, "context_length", int(self.context_length or 0))
        set_(self, "max_completion_tokens", int(self.max_completion_tokens or 0))
        set_(self, "uptime_1d", _opt_float(self.uptime_1d))
        set_(self, "uptime_30m", _opt_float(self.uptime_30m))
        set_(self, "latency_30m", _opt_float(self.latency_30m))
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
                rate_in = _opt_float(override.get("prompt"))
                rate_out = _opt_float(override.get("completion"))
        if rate_in is None:
            rate_in = self.prompt
        if rate_out is None:
            rate_out = self.completion
        if apply_discount and semantics == DISCOUNT_IS_MULTIPLIER and self.discount:
            keep = 1.0 - self.discount
            rate_in *= keep
            rate_out *= keep
        return rate_in, rate_out

    def blended_usd(self, input_tokens, output_tokens, *, semantics,
                    cache_eligible=False):
        """Dollar cost of one call at this endpoint. Never mixes endpoints."""
        rate_in, rate_out = self.rates_for(input_tokens, True, semantics)
        if cache_eligible and self.input_cache_read is not None:
            rate_in = self.input_cache_read
        return (input_tokens * rate_in) + (output_tokens * rate_out)

    @property
    def name(self):
        """Human-facing identity used in reports and probe match lists."""
        return f"{self.provider_name}|{self.tag}" if self.tag else self.provider_name

    def is_eligible(self, *, min_uptime=0.0):
        """Whether this offer can be routed to at all.

        A provider reporting degraded uptime is not a cheaper model, it is an
        unavailable one, and picking it converts a price win into a 429.
        """
        if min_uptime and self.uptime_1d is not None:
            return self.uptime_1d >= min_uptime
        return True

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
    fetched_at: float = 0.0

    def __post_init__(self):
        object.__setattr__(self, "endpoints", tuple(self.endpoints or ()))
        object.__setattr__(self, "fetched_at", float(self.fetched_at or 0.0))

    @property
    def max_discount(self):
        """The deepest published promotion, or 0.0 when none is running."""
        return max((e.discount for e in self.endpoints), default=0.0)

    @property
    def has_discount(self):
        return self.max_discount > 0.0

    def to_dict(self):
        return {"model": self.model_id,
                "endpoints": [e.to_dict() for e in self.endpoints],
                "max_discount": self.max_discount}

    def __repr__(self):  # pragma: no cover - debug aid
        return f"<ModelEndpoints {self.model_id} n={len(self.endpoints)}>"


# --------------------------------------------------------------------------
# Endpoint ingest
# --------------------------------------------------------------------------
def _parse_endpoints(model_id, payload, fetched_at=None):
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
                        _opt_float(override.get("min_prompt_tokens")) or 0),
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
    return ModelEndpoints(model_id, tuple(parsed), fetched_at=fetched_at)


def fetch_endpoints(transport, api_key, model_id, *, timeout=20):
    """One GET for one model's per-provider offers.

    Deliberately one request per model: the benchmark account budget is 500
    requests/day and this feed is the reason ``fetch_endpoints_for`` refuses
    a catalog-wide sweep by default.
    """
    canonical = strip_variant_suffix(model_id)
    url = OPENROUTER_ENDPOINTS_URL.format(slug=canonical)
    payload = transport.get(url, api_key, timeout=timeout)
    if isinstance(payload, dict) and payload.get("error"):
        raise HarnessError(f"endpoints feed for {model_id} returned an error: "
                           f"{payload['error'].get('message', payload['error'])}")
    endpoints = _parse_endpoints(model_id, payload, fetched_at=time.time())
    emit("economics_endpoints", model=canonical, endpoints=len(endpoints.endpoints),
         max_discount=endpoints.max_discount)
    return endpoints


def fetch_endpoints_for(transport, api_key, model_ids, *,
                        max_fetches=MAX_ENDPOINT_FETCHES_PER_RUN):
    """Fetch endpoints for an explicit shortlist. THE ONE OWNER OF THE BOUND.

    Every caller goes through here. That is the whole point: when the budget
    check lived here but :func:`build_economics_report` looped
    :func:`fetch_endpoints` directly, the production path issued one GET per
    requested model while reporting the budget it had been given (and with no
    flag, ``[:None]`` made the default unbounded). A guard that only the tests
    called is not a guard.

    The bound is checked BEFORE any request is issued, and it refuses rather
    than truncating: a silently partial report reads as full coverage, which
    is the failure this module exists to prevent. It is also fail-closed for
    the implicit default, so if the shipped pool ever outgrows
    ``max_fetches`` the operator is told instead of being handed a truncated
    artifact.

    Returns ``{"priced": {model_id: ModelEndpoints}, "errors": {model_id:
    message}}``. Per-model failures are isolated rather than raised, because
    one model leaving the catalog must not cost the operator every other
    price in the report -- but the caller is expected to make each error
    VISIBLE, since a shipped model quietly absent from a report is exactly the
    silent-degradation defect this module's own docstring rejects.
    """
    # Strip BEFORE deduping: `a/m` and `a/m:free` are one model, and issuing
    # a GET for each would spend the daily request budget twice on one row.
    ids = list(dict.fromkeys(strip_variant_suffix(m)
                             for m in (model_ids or [])))
    if len(ids) > max_fetches:
        raise HarnessError(
            f"endpoint fetch budget is {max_fetches} models per run; {len(ids)} "
            f"requested. Endpoint coverage is shortlist-scoped by design "
            f"(DF-EV-2): pass the ids you actually route to, or raise "
            f"max_fetches explicitly. No requests were issued.")
    priced, errors = {}, {}
    for model_id in ids:
        try:
            priced[model_id] = fetch_endpoints(transport, api_key, model_id)
        except HarnessError as exc:
            errors[model_id] = str(exc)
            # Announced HERE, at the point of isolation, because this is the
            # only place the failure is guaranteed to be seen by someone. A
            # shipped model that quietly drops out of a report is precisely
            # the silent degradation this module's docstring rejects, and a
            # live run did exactly that: `cohere/north-mini-code` errored into
            # `endpoint_errors` with a clean stderr and a plausible artifact.
            eprint(f"[warn] economics: endpoint feed failed for {model_id}: {exc}")
    return {"priced": priced, "errors": errors}


# --------------------------------------------------------------------------
# Benchmark ingest
# --------------------------------------------------------------------------
def fetch_benchmarks(transport, api_key, *, source=None, task_type=None,
                     timeout=30):
    """One GET for the unified benchmark feed.

    Shape follows the source: Artificial Analysis rows carry index fields
    (0-100), OpenRouter rows carry ``accuracy``/``avg_cost_per_task`` for a
    named ``benchmark_type``. Both are normalized onto one row shape so a
    caller never branches on source; the untouched payload is kept in
    ``raw`` so a future field is never lost to this module's schema.
    """
    if source is not None and source not in BENCHMARK_SOURCES:
        raise HarnessError(f"unknown benchmark source {source!r}; "
                           f"expected one of {', '.join(BENCHMARK_SOURCES)}")
    url = OPENROUTER_BENCHMARKS_URL
    params = []
    if source:
        params.append(f"source={source}")
    if task_type:
        params.append(f"task_type={task_type}")
    if params:
        url = f"{url}?{'&'.join(params)}"
    payload = transport.get(url, api_key, timeout=timeout)
    if not isinstance(payload, dict):
        raise HarnessError(f"benchmark feed returned {type(payload).__name__}, "
                           f"not an object")
    if payload.get("error"):
        raise HarnessError("benchmark feed returned an error: "
                           f"{payload['error'].get('message', payload['error'])}")
    raw_rows = payload.get("data")
    if not isinstance(raw_rows, list) or not raw_rows:
        raise HarnessError("benchmark feed returned no data rows")

    rows = [_normalize_benchmark(r) for r in raw_rows if isinstance(r, dict)]
    # A row with no model identity cannot be joined to anything downstream,
    # so it is dropped rather than carried as a None slug.
    rows = [r for r in rows if r.get("model_permaslug")]
    if not rows:
        raise HarnessError("benchmark feed returned only malformed rows")
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    emit("economics_benchmarks", rows=len(rows),
         as_of=meta.get("as_of"), sources=sorted({r["source"] for r in rows}))
    return {"rows": rows, "meta": meta, "source": source, "task_type": task_type}


def _normalize_benchmark(raw):
    """One benchmark row onto a shape that does not depend on its source."""
    return {
        "source": raw.get("source"),
        "model_permaslug": raw.get("model_permaslug"),
        "display_name": raw.get("display_name"),
        # Artificial Analysis indices (0-100).
        "coding_index": _opt_float(raw.get("coding_index")),
        "agentic_index": _opt_float(raw.get("agentic_index")),
        "intelligence_index": _opt_float(raw.get("intelligence_index")),
        # OpenRouter's own evaluations.
        "benchmark_type": raw.get("benchmark_type"),
        "accuracy": _opt_float(raw.get("accuracy")),
        "accuracy_stddev": _opt_float(raw.get("accuracy_stddev")),
        "avg_cost_per_task": _opt_float(raw.get("avg_cost_per_task")),
        "total_tasks": raw.get("total_tasks"),
        "last_run_timestamp": raw.get("last_run_timestamp"),
        "raw": raw,
    }


def benchmarks_by_model(report):
    """Group a benchmark report by permaslug for O(1) lookup per model."""
    grouped = {}
    for row in report.get("rows", []):
        slug = row.get("model_permaslug")
        if slug:
            grouped.setdefault(slug, []).append(row)
    return grouped


# --------------------------------------------------------------------------
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
    endpoints = fetch_endpoints(transport, api_key, model_id)
    if not endpoints.has_discount:
        raise HarnessError(
            f"{model_id} publishes no discount on any endpoint "
            f"(max={endpoints.max_discount}); it cannot settle discount "
            f"semantics. Pick a model with a running promotion.")

    canonical = strip_variant_suffix(model_id)
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
    actual = _opt_float(usage.get("cost"))
    prompt_tokens = _opt_float(usage.get("prompt_tokens"))
    completion_tokens = _opt_float(usage.get("completion_tokens"))

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


# --------------------------------------------------------------------------
# Recording + the gate
# --------------------------------------------------------------------------
def discount_verdict_path(path=None, repo_root=None):
    """Where the verdict lives: a REPO-COMMITTED receipt.

    Explicit ``path`` wins (hermetic tests, CI, an operator inspecting a
    checkout). Otherwise the path is resolved against the checkout root, not
    ``~/.config``: the verdict authorizes every downstream cost decision, so
    it is evidence that has to travel with the code and be reviewable in a PR.

    There is no machine-local fallback, and that absence is the fix. While the
    gate read ``~/.config/harness/economics.json`` it asked the operator's
    filesystem rather than the repository, so two checkouts could disagree
    about whether the price gate was satisfied -- and a verdict nobody could
    review governed the cost model of the whole system.
    """
    if path:
        return path
    return os.path.join(repo_root or REPO_ROOT, ECONOMICS_VERDICT_PATH)


def record_discount_semantics(record, path=None, repo_root=None):
    """Write a probe verdict into the committed receipt. Conclusive only.

    ``unresolved``/``ambiguous`` are written nowhere on purpose: a refusal to
    decide is not a decision, and caching one would let a later run believe
    the question had been answered.

    The write lands in the working tree, which is the point: the verdict is
    repo evidence and is meant to be committed with the code it governs (and,
    under ``audits/self/dogfood/``, SHA-256-pinned by the corpus manifest, so
    a later hand-edit of it fails the audit).
    """
    semantics = record.get("semantics")
    if semantics not in DISCOUNT_SEMANTICS_VALUES:
        raise HarnessError(
            f"refusing to record discount semantics {semantics!r}: only a "
            f"conclusive verdict ({', '.join(DISCOUNT_SEMANTICS_VALUES)}) can "
            f"be stored. Re-run the probe on an unambiguous model.")
    target = discount_verdict_path(path, repo_root)
    directory = os.path.dirname(target)
    if directory:
        os.makedirs(directory, exist_ok=True)
    write_text(target, json.dumps(record, indent=2, sort_keys=True) + "\n")
    emit("economics_discount_recorded", model=record.get("model"),
         semantics=semantics)
    eprint(f"[economics] wrote repo evidence {target}; commit it -- a verdict "
           f"that lives on one machine is not repo evidence and the gate "
           f"reads only the committed receipt.")
    return target


def load_discount_semantics(path=None, repo_root=None):
    """The committed verdict, or None when there is none.

    Absence is a normal state, not an error: a checkout with no committed
    verdict has nothing to trust, and downstream price computation stays
    refused until the probe runs and its receipt is committed.
    """
    target = discount_verdict_path(path, repo_root)
    try:
        with open(target, encoding="utf-8") as stream:
            record = json.load(stream)
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    if record.get("semantics") not in DISCOUNT_SEMANTICS_VALUES:
        return None
    if not record.get("fingerprint"):
        # A verdict with no fingerprint cannot be re-checked against the
        # live promotion, so it can never be trusted to still apply.
        return None
    return record


def resolve_discount_semantics(endpoints, path=None, repo_root=None):
    """The semantics to use for THIS model's prices, or a refusal.

    Three states, in order:

    * no promotion running -> ``not_applicable``; listed rates are used
      as-is and the question never arises;
    * a conclusive verdict in the COMMITTED receipt whose fingerprint still
      matches the live promotion -> that verdict;
    * otherwise -> :class:`HarnessError`. There is no default, because the
      two candidates differ by 2x and guessing is the one failure mode this
      gate exists to remove.

    Fail-closed is unchanged by where the verdict lives: a checkout with no
    committed receipt, an unreadable one, or one measured against a different
    promotion all refuse, and each refusal names the receipt path so the
    operator knows exactly what is missing.
    """
    if not endpoints.has_discount:
        return DISCOUNT_NOT_APPLICABLE
    record = load_discount_semantics(path, repo_root)
    if record is None:
        raise HarnessError(
            f"{endpoints.model_id} has a running discount "
            f"({endpoints.max_discount:g}) but no committed semantics verdict "
            f"at {discount_verdict_path(path, repo_root)}. OpenRouter does "
            f"not document whether the published rate already includes the "
            f"promotion, and the two readings differ by "
            f"{1 / max(1e-9, 1 - endpoints.max_discount):.2f}x. Run "
            f"`harness economics --probe-model <id> --record` to measure it "
            f"and commit the receipt; refusing rather than guessing.")
    fingerprint = record.get("fingerprint") or {}
    recorded_discount = fingerprint.get("max_discount")
    if recorded_discount is None:
        raise HarnessError(
            f"committed discount verdict for {endpoints.model_id} carries no "
            f"promotion fingerprint; re-run the probe and commit the new "
            f"receipt.")
    if abs(float(recorded_discount) - endpoints.max_discount) > 1e-9:
        raise HarnessError(
            f"committed discount verdict was measured against a "
            f"{float(recorded_discount):g} promotion but "
            f"{endpoints.model_id} now publishes {endpoints.max_discount:g}. "
            f"The answer may no longer hold; re-run "
            f"`harness economics --probe-model <id> --record`.")
    return record["semantics"]


def effective_price(endpoints, input_tokens, output_tokens, *, path=None,
                    repo_root=None, cache_eligible=False, min_uptime=0.0):
    """THE GATE. Cheapest real cost for a call, or a refusal.

    Chooses among *per-endpoint* blended costs (never mixing rates across
    providers) and refuses outright when the model's promotion has no
    committed, still-applicable verdict. Every EV-1+ consumer goes through
    here rather than reading an endpoint price directly.
    """
    semantics = resolve_discount_semantics(endpoints, path=path,
                                           repo_root=repo_root)
    eligible = [e for e in endpoints.endpoints if e.is_eligible(min_uptime=min_uptime)]
    if not eligible:
        eligible = list(endpoints.endpoints)
    if not eligible:
        raise HarnessError(f"no provider offer available for {endpoints.model_id}")
    best = min(eligible, key=lambda e: e.blended_usd(
        input_tokens, output_tokens, semantics=semantics,
        cache_eligible=cache_eligible))
    return best, best.blended_usd(input_tokens, output_tokens,
                                  semantics=semantics,
                                  cache_eligible=cache_eligible)


# --------------------------------------------------------------------------
# Receipt
# --------------------------------------------------------------------------
def build_economics_report(governor, ledger, *, api_key=None, transport=None,
                           benchmark_ids=None, probe_model=None,
                           max_fetches=None, verdict_path=None):
    """Assemble the EV-0 evidence artifact.

    Read-only with respect to configuration: it ingests and reports, and it
    never mutates a pool, a lane default, or a ceiling. Whether to *apply*
    anything is EV-3/EV-4's job, and this report is their only input.

    Endpoint pricing is delegated to :func:`fetch_endpoints_for` and must stay
    that way. This function used to loop :func:`fetch_endpoints` itself, which
    meant the report issued one GET per requested model while writing the
    budget it had been given into the artifact -- the budget was real in the
    helper and inert in production. Any future fan-out added here inherits
    that: call the owner, not the single-model fetch.
    """
    if transport is None:
        transport = HttpTransport()
    benchmarks = fetch_benchmarks(transport, api_key)
    catalog_ids = None
    if governor is not None:
        try:
            catalog_ids = [m.get("id") for m in governor.fetch_models()
                           if m.get("id")]
        except HarnessError as exc:
            eprint(f"[warn] economics: catalog unavailable ({exc})")

    # An unset bound is still a bound. The run cap exists to protect a
    # metered feed, so it is not opt-in: the CLI passes `None` when
    # --max-fetches is absent, and resolving it here is what stops `[:None]`
    # from turning the default into an unbounded sweep. The number reported
    # below is therefore always the number that was enforced.
    budget = (MAX_ENDPOINT_FETCHES_PER_RUN if max_fetches is None
              else int(max_fetches))
    shortlist = [strip_variant_suffix(m) for m in (benchmark_ids or [])]
    if not shortlist:
        # Default shortlist: the ids this install actually routes to. Those
        # are the ones whose real price decides a cost decision, so they are
        # the slice worth spending the daily request budget on. Catalog-wide
        # coverage is /models (unmetered); endpoint coverage is this list.
        # NOT truncated to the budget here: a quietly shortened list produces
        # a partial report that reads as full coverage. The guard below
        # refuses instead, and says so.
        shortlist = sorted(strip_variant_suffix(m) for m in shipped_model_ids())
        eprint(f"[info] economics: no ids given; pricing the "
               f"{len(shortlist)} shipped lane models against a budget of "
               f"{budget} endpoint fetches (endpoint coverage is "
               f"shortlist-scoped by design)")

    endpoint_rows, endpoint_errors = {}, {}
    if shortlist:
        fetched = fetch_endpoints_for(transport, api_key, shortlist,
                                      max_fetches=budget)
        endpoint_rows = {model_id: ep.to_dict()
                         for model_id, ep in fetched["priced"].items()}
        endpoint_errors = dict(fetched["errors"])
        if endpoint_errors:
            eprint(f"[warn] economics: {len(endpoint_errors)} of "
                   f"{len(shortlist)} requested models have no endpoint price; "
                   f"they are listed under endpoint_errors in the receipt, so "
                   f"this run does not cover them")

    probe_record = None
    if probe_model:
        probe_record = run_discount_probe(transport, api_key, governor,
                                           probe_model)

    report = {
        "schema": ECONOMICS_SCHEMA_VERSION,
        "captured_at": time.time(),
        "benchmark_meta": benchmarks.get("meta"),
        "benchmarks": benchmarks.get("rows"),
        "benchmark_count": len(benchmarks.get("rows") or []),
        "catalog_size": len(catalog_ids or []),
        "endpoint_models": endpoint_rows,
        "endpoint_errors": endpoint_errors,
        "shortlist_size": len(shortlist),
        "max_fetches": budget,
        "discount_semantics_recorded": load_discount_semantics(verdict_path),
        "discount_probe": probe_record,
    }
    if ledger is not None:
        try:
            report["cost_by_model"] = governor.cost_by_model()
        except Exception:  # a governor without spend history is not an error
            report["cost_by_model"] = {}
    return report


def write_receipt(report, directory=ECONOMICS_RECEIPT_DIR, *, name=None):
    """Write the artifact through the ONE owner of evidence bytes (LF).

    LF is not cosmetic here: a CRLF receipt on Windows shows up as a dirty
    tree immediately after the gate that is meant to prove it clean, which is
    the same defect class ``GAP-freeze-face`` closed for the audit receipt.
    """
    stamp = name or time.strftime("%Y-%m-%d", time.gmtime(report.get("captured_at") or 0))
    directory = os.path.join(directory, "") if directory else ""
    target = os.path.join(directory, f"economics-{stamp}.json")
    os.makedirs(directory, exist_ok=True)
    write_text(target, json.dumps(report, indent=2, sort_keys=True) + "\n")
    latest = os.path.join(directory, "latest.json")
    write_text(latest, json.dumps(report, indent=2, sort_keys=True) + "\n")
    return target
