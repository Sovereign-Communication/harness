"""Model capability layer: what each model can actually do, and how that
feeds routing and the reliability score.

Philosophy: capability is a *hypothesis* drawn from the live /models metadata
(declared context length, reasoning support, structured-JSON support), and the
ledger's observed behavior is the *evidence* that corrects it. A declared
capability is a prior; observed JSON and known-answer success update that prior
in both directions, so a model that declares structured output but fails real
calls is demoted while an undeclared model that reliably emits JSON can rise.

Routing rule (per product direction): when cost is equal -- i.e. the whole free
tier, where every model is $0 -- order by the corrected reliability view. The
paid tier keeps cheap-first (cost ascending), with corrected reliability
breaking cost ties.

The capability score itself is task-aware: this is a text/code harness, so
input modality confers no task capability, structured-JSON output does (that's
what the panel/judge need), reasoning matters for complex code, and context
length bounds whether a model can even see the quoted window.
"""
import json
import math
import os
import time

from .output import eprint

# ---- score weights (task-aware) ----------------------------------------------
# Capability is blended from three declared signals. Structured JSON is the
# most valuable for THIS harness (the panel and judge must emit parseable JSON);
# context bounds the work; reasoning helps on hard code. Input modality is
# deliberately NOT weighted -- a text/code harness gains nothing from vision.
# (Weights sum to 1.0.)
_W_CTX = 0.35
_W_REASON = 0.25
_W_JSON = 0.40

# Reference bounds for log-scaling context length (tokens).
_CTX_FLOOR = 8192        # ~8k context scores 0
_CTX_CEIL = 1_048_576    # ~1M context scores 1
# Safe fraction of context reserved for the quoted source window (hard gate).
CONTEXT_WINDOW_FRACTION = 0.6

# Composite reliability weights: capability fitness, observed calibration,
# observed success rate. Sum to 1.0.
W_CAP = 0.4
W_CAL = 0.3
W_SUCC = 0.3

# No-data prior: a model with no observed samples sits at this value for
# calibration/success, and its effective weight is shrunk by n/(n+_SHRINK) so a
# fresh model's reliability starts at its capability and converges to evidence.
# _SHRINK=2: evidence converges quickly, so observed behavior (probe + ledger)
# corrects an over-declared capability without needing many samples.
NO_DATA_PRIOR = 0.5
_SHRINK = 2

# ---- registry persistence ----------------------------------------------------
DEFAULT_TTL = 24 * 3600  # refresh /models capabilities at most once / TTL


class CapabilityProfile:
    """Declared capability of a single model, derived from a /models entry."""

    __slots__ = ("model_id", "context_length", "free", "prompt_price",
                 "completion_price", "supports_reasoning",
                 "supports_structured_json", "supports_json_schema",
                 "supports_response_format", "tool_use", "input_modalities",
                 "name", "_fetched_at")

    def __init__(self, model_id, context_length=None, free=False,
                 prompt_price=0.0, completion_price=0.0, supports_reasoning=False,
                 supports_structured_json=False, supports_json_schema=False,
                 supports_response_format=False, tool_use=False,
                 input_modalities=None, name=None):
        self.model_id = model_id
        self.context_length = int(context_length) if context_length else 0
        self.free = free
        self.prompt_price = float(prompt_price or 0.0)
        self.completion_price = float(completion_price or 0.0)
        self.supports_reasoning = supports_reasoning
        self.supports_structured_json = supports_structured_json
        self.supports_json_schema = supports_json_schema
        self.supports_response_format = supports_response_format
        self.tool_use = tool_use
        self.input_modalities = list(input_modalities) if input_modalities else []
        self.name = name

    # -- JSON capability: declared ---------------------------------------------------
    @property
    def declared_json(self):
        """0..1 declared structured-JSON support: full > schema > response_format."""
        if self.supports_structured_json or self.supports_json_schema:
            return 1.0
        if self.supports_response_format:
            return 0.7
        return 0.0

    @property
    def max_source_tokens(self):
        """Hard gate: how much quoted source this model can realistically hold."""
        return int(self.context_length * CONTEXT_WINDOW_FRACTION)

    def to_dict(self):
        return {
            "model_id": self.model_id, "name": self.name,
            "context_length": self.context_length, "free": self.free,
            "prompt_price": self.prompt_price, "completion_price": self.completion_price,
            "supports_reasoning": self.supports_reasoning,
            "supports_structured_json": self.supports_structured_json,
            "supports_json_schema": self.supports_json_schema,
            "supports_response_format": self.supports_response_format,
            "tool_use": self.tool_use, "input_modalities": self.input_modalities,
        }

    @classmethod
    def from_dict(cls, d):
        return cls(
            model_id=d.get("model_id"), name=d.get("name"),
            context_length=d.get("context_length"), free=d.get("free"),
            prompt_price=d.get("prompt_price"), completion_price=d.get("completion_price"),
            supports_reasoning=d.get("supports_reasoning"),
            supports_structured_json=d.get("supports_structured_json"),
            supports_json_schema=d.get("supports_json_schema"),
            supports_response_format=d.get("supports_response_format"),
            tool_use=d.get("tool_use"), input_modalities=d.get("input_modalities"),
        )

    @classmethod
    def from_model(cls, entry):
        """Build a profile from a raw /models entry dict (tolerates missing keys)."""
        supported = entry.get("supported_parameters") or []
        if not isinstance(supported, list):
            supported = []
        supported = set(supported)
        pricing = entry.get("pricing") or {}
        pp = float(pricing.get("prompt") or 0)
        cp = float(pricing.get("completion") or 0)
        return cls(
            model_id=entry.get("id", ""), name=entry.get("name"),
            context_length=entry.get("context_length"),
            free=entry.get("id", "").endswith(":free") or (pp == 0 and cp == 0),
            prompt_price=pp, completion_price=cp,
            supports_reasoning="reasoning" in supported,
            supports_structured_json="structured_outputs" in supported,
            supports_json_schema="json_schema" in supported,
            supports_response_format="response_format" in supported,
            tool_use="tools" in supported,
            input_modalities=entry.get("input_modalities") or
                           (entry.get("architecture") or {}).get("input_modalities"),
        )

    def __repr__(self):  # pragma: no cover - debug aid
        return (f"<CapabilityProfile {self.model_id} ctx={self.context_length} "
                f"reason={self.supports_reasoning} json={self.declared_json:.2f}>")


# ------------------------- scoring --------------------------------------------

def _clamp01(x):
    return max(0.0, min(1.0, x))


def context_score(context_length):
    """Log-scale context length to 0..1 between the floor and ceiling."""
    if not context_length:
        return 0.0
    return _clamp01((math.log2(max(1, context_length)) - math.log2(_CTX_FLOOR)) /
                    (math.log2(_CTX_CEIL) - math.log2(_CTX_FLOOR)))


def capability_score(profile, json_weight=_W_JSON, json_value=None):
    """Declared-capability hypothesis, 0..1 (task-agnostic default).

    `json_value` overrides the JSON term with an observed-corrected value (e.g.
    `json_reliable`), so the score reflects what a model actually does, not just
    what it declares."""
    c = context_score(profile.context_length)
    r = 1.0 if profile.supports_reasoning else 0.0
    j = profile.declared_json if json_value is None else json_value
    # Re-normalize the remaining weight onto the provided json_weight so callers
    # can shift emphasis without changing the total.
    rest = 1.0 - json_weight
    wc = _W_CTX / (_W_CTX + _W_REASON)
    wr = _W_REASON / (_W_CTX + _W_REASON)
    return _clamp01(rest * (wc * c + wr * r) + json_weight * j)


# Task profiles: which declared signals dominate for a given role.
TASK_WEIGHTS = {
    "default": _W_JSON,
    "structured": 0.6,   # panel/judge MUST emit parseable JSON -- weight it heavily
    "code": 0.30,        # apply: context + reasoning matter more than JSON
}


def capability_fitness(profile, task="default", json_value=None):
    """Task-aware capability. 'structured' = claims panel / judge (JSON-critical)."""
    return capability_score(profile, json_weight=TASK_WEIGHTS.get(task, TASK_WEIGHTS["default"]),
                            json_value=json_value)


# ------------------------- observed layer -------------------------------------

def observed_json_reliability(ledger, model_id):
    """Observed structured-JSON emission rate from model_result events."""
    hits = 0
    total = 0
    for e in ledger.entries():
        if e.get("event") == "model_result" and e.get("model") == model_id and e.get("json_expected"):
            total += 1
            if e.get("json_ok"):
                hits += 1
    return (hits / total) if total else None


def json_reliable(profile, ledger, model_id):
    """Observed-corrected structured-JSON capability, 0..1.

    Declared capability is the PRIOR; observed behavior (probe + model_result
    events) UPDATES it -- upward for a model that emits JSON reliably in
    practice but declares no structured output (the north-mini judge), downward
    for a model that declares it but fails on real calls (probe-falsified GLM).
    This is the capability<->reliability bridge that makes declared capability
    a hypothesis corrected by evidence, in both directions."""
    declared = profile.declared_json if profile else 0.0
    observed = observed_json_reliability(ledger, model_id)
    if observed is None:
        return declared
    n = observed_samples(ledger, model_id)
    w = n / (n + _SHRINK)
    return _clamp01((1 - w) * declared + w * observed)


def observed_samples(ledger, model_id):
    return sum(1 for e in ledger.entries()
               if e.get("event") == "model_result" and e.get("model") == model_id
               and e.get("json_expected"))


def _shrink_weight(n):
    return n / (n + _SHRINK)


def model_reliability(model_id, profile, report, ledger=None, task="default"):
    """SINGLE OWNER of the per-model reliability computation.

    Returns every component so routing, the CLI, and reports consume one
    calculation. Structured tasks use observed-corrected JSON capability and
    known-answer success from probe `model_result` events; code tasks use the
    verify-gate success rate.
    """
    json_value = None
    if task == "structured" and ledger is not None and profile is not None:
        json_value = json_reliable(profile, ledger, model_id)
    capability = capability_fitness(profile, task, json_value=json_value) if profile else 1.0
    cal = (report or {}).get("calibration", {}).get(model_id, {})
    calibration = cal.get("confidence_precision")
    if task == "structured":
        success = cal.get("structured_success_rate")
        samples = cal.get("structured_samples", 0)
    else:
        success = cal.get("success_rate")
        samples = cal.get("samples", 0)
    return {
        "model_id": model_id,
        "capability": capability,
        "json_reliable": (json_value if json_value is not None
                           else (profile.declared_json if profile else None)),
        "calibration": calibration,
        "success": success,
        "samples": samples,
        "reliability": composite_reliability(capability, calibration, success, samples),
    }


def composite_reliability(capability, calibration, success, n_samples):
    """Weighted blend of static capability + observed calibration + observed
    success, with prior-shrink when evidence is thin. Hard-gated to 0 when the
    model lacks a required capability (capability == 0).
    """
    if capability is None or capability <= 0:
        return 0.0
    w = _shrink_weight(n_samples or 0)
    cal = _clamp01(calibration if calibration is not None else NO_DATA_PRIOR)
    succ = _clamp01(success if success is not None else NO_DATA_PRIOR)
    # Shrink calibration/success toward their priors with sparse data; capability
    # is a prior itself and needs no shrink (it IS the prior).
    observed_cal = (1 - w) * NO_DATA_PRIOR + w * cal
    observed_succ = (1 - w) * NO_DATA_PRIOR + w * succ
    return _clamp01(W_CAP * capability + W_CAL * observed_cal + W_SUCC * observed_succ)


# ------------------------- registry persistence -------------------------------

CAPABILITIES_SCHEMA_VERSION = 1


def load_profiles(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if data.get("schema_version") != CAPABILITIES_SCHEMA_VERSION:
                return {}, None  # foreign or older schema: treat as stale, refetch
        out = {}
        for mid, d in (data.get("models") or {}).items():
            p = CapabilityProfile.from_dict(d)
            p._fetched_at = data.get("fetched_at")
            out[mid] = p
        return out, data.get("fetched_at")
    except (OSError, ValueError, AttributeError):
        return {}, None


def save_profiles(path, profiles, fetched_at=None):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    data = {
        "schema_version": CAPABILITIES_SCHEMA_VERSION,
        "fetched_at": fetched_at or time.time(),
        "models": {mid: p.to_dict() for mid, p in profiles.items()},
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def build_profiles_from_models(models):
    """Build a {model_id: CapabilityProfile} dict from a list of /models entries."""
    return {m.get("id"): CapabilityProfile.from_model(m) for m in models if m.get("id")}


def ensure_profiles(path, fetch_models_fn, ttl=DEFAULT_TTL, force=False):
    """Return (profiles, fetched_at, refreshed). Uses the registry unless stale
    (or force), in which case it fetches fresh /models and persists."""
    profiles, fetched_at = load_profiles(path)
    fresh_enough = fetched_at is not None and (time.time() - fetched_at) < ttl
    if force or not fresh_enough:
        models = fetch_models_fn()
        profiles = build_profiles_from_models(models)
        fetched_at = time.time()
        save_profiles(path, profiles, fetched_at)
        return profiles, fetched_at, True
    return profiles, fetched_at, False


# ------------------------- routing --------------------------------------------

# ------------------------- empirical probe ------------------------------------
# A minimal known-answer probe that measures a model's OBSERVED structured-JSON
# emission + arithmetic correctness on the free tier. This is the "prove the
# hypothesis" step: it tests whether a model actually does what its declared
# capability claims, and feeds observed evidence back into routing/reliability.

_PROBE_QUESTIONS = [
    ("Respond with ONLY JSON: {\"answer\": 2 + 2}", 4),
    ("What is 7 * 8? Respond with ONLY JSON {\"answer\": <int>}", 56),
    ("Is 101 a prime number? Respond with ONLY JSON {\"answer\": true|false}", True),
    ("Compute 2**10. Respond with ONLY JSON {\"answer\": <int>}", 1024),
    ("How many letters in 'abracadabra'? Respond with ONLY JSON {\"answer\": <int>}", 11),
]


def probe_json_reliability(transport, api_key, governor, models, max_tokens=256,
                           reasoning_effort=None, ledger=None, profiles=None,
                           task_id=None):
    """Run a small known-answer JSON-emission probe over each model.

    Returns {model: {"calls", "json_ok_rate", "correct_rate", "errors"}}. Uses
    the harness's own `chat` (spend-governed, BYOK-guarded) so it is safe to run
    on the free tier. When a `ledger` is supplied, every call is persisted as a
    `model_result` event (json_expected=True, json_ok/correct), so probe results
    feed observed json reliability and routing -- the "prove it" evidence loop.
    Reasoning models are probed WITH a reasoning effort (`low`) so the probe is
    fair to them; non-reasoning models run with reasoning off.
    """
    from .chat import chat, extract_content_and_cost, _extract_json
    results = {}
    tid = task_id or "bench/probe"
    for m in models:
        eff = reasoning_effort
        if eff is None:
            declares_reasoning = bool(profiles and profiles.get(m) and
                                      profiles[m].supports_reasoning)
            eff = "low" if declares_reasoning else "off"
        governor.check_byok(m)
        json_ok = 0
        correct = 0
        errors = 0
        calls = 0
        for q, want in _PROBE_QUESTIONS:
            calls += 1
            status = None
            resp = {}
            error_message = None
            call_cost = 0.0
            try:
                # Every probe question is preflighted against the ceiling so
                # the loop can never spend through it (same contract as every
                # other governed lane).
                governor.preflight(q, [(f"probe {m}", m, max_tokens, 0)])
                status, resp = chat(transport, api_key, m,
                                    [{"role": "user", "content": q}], max_tokens,
                                    eff, 0.4, governor)
            except Exception as exc:
                error_message = str(exc)
            if status == 200:
                # Probe calls are ordinary governed calls: account for their
                # reported cost before evaluating the response. The small
                # fallback keeps the hermetic test seam compatible with minimal
                # fake governors that only implement check_byok().
                try:
                    call_cost = float((resp.get("usage") or {}).get("cost") or 0.0)
                except (AttributeError, TypeError, ValueError):
                    call_cost = 0.0
                record_actual = getattr(governor, "record_actual", None)
                if record_actual is not None:
                    record_actual(call_cost, m)
            ok = False
            corr = False
            if status == 200:
                try:
                    content, *_ = extract_content_and_cost(resp)
                    parsed = _extract_json(content) if content else None
                    if isinstance(parsed, dict):
                        ok = True
                        corr = parsed.get("answer") == want
                except Exception as exc:
                    error_message = str(exc)
            # A successful HTTP response with empty/non-JSON content is still a
            # failed probe call. Record it as an error and continue to the next
            # bounded question; never retry in a loop for malformed model output.
            if status != 200 or not ok:
                errors += 1
            if ok:
                json_ok += 1
            if corr:
                correct += 1
            if ledger is not None:
                ledger.append("model_result", task_id=tid, event_note="probe",
                              model=m, task_type="structured", json_expected=True,
                              json_ok=ok, correct=corr,
                              status="ok" if status == 200 and ok else "error",
                              error=error_message)
        results[m] = {
            "calls": calls, "errors": errors,
            "json_ok_rate": round(json_ok / calls, 3) if calls else None,
            "correct_rate": round(correct / calls, 3) if calls else None,
        }
    return results


def order_pool(pool, profiles, report, ledger=None, task="default", free_tier=None):
    """Order a model pool for routing, driven by the observed-CORRECTED view.

    Free tier (all $0, so cost is equal): sort by reliability descending, where
    reliability embeds the observed-corrected capability (declared capability as
    a prior, corrected by probe/ledger evidence) -- so a probe-falsified model
    that merely *declares* capability no longer ranks first. Paid tier: cost
    ascending first (cheap lane), reliability breaks cost ties. Models that fail
    the hard capability gate (capability <= 0) are dropped.
    """
    if free_tier is None:
        raise ValueError("order_pool requires the caller's explicit use_free/free_tier flag")
    # Observed demotion (single policy place): a model the ledger shows
    # producing unusable apply output -- HTTP 200 but no usable content, the
    # reasoning-only fallback the engine refuses to write (participation_report
    #    surfaces it as unusable_outputs) -- sorts below every unproven model.
    # Demoted, not banned: when the pool exhausts the usable ones, it still
    # rotates (and the verification gate still guards what it produces).
    # Strike policy: demotion requires TWO unusable events. A single event can
    # be one flaky response from an otherwise good model; one strike must not
    # flip live pool order. (429/401 tier faults and fail-closed verify runs
    # are deliberately NOT demotion evidence -- see ledger.participation_report.)
    # Consent-unusable events (empty/reasoning-only/unparseable consent answers,
    # surfaced by the same report) join the same strike count: a judge the
    # sovereignty gate cannot parse is demoted like one whose apply output the
    # engine cannot write.
    UNUSABLE_DEMOTE_STRIKES = 2

    def demotion(model):
        cal = (report or {}).get("calibration", {}).get(model, {})
        strikes = (cal.get("unusable_outputs") or 0) + (cal.get("consent_unusable") or 0)
        return 1 if strikes >= UNUSABLE_DEMOTE_STRIKES else 0
    scored = []
    for m in pool:
        profile = (profiles or {}).get(m)
        info = model_reliability(m, profile, report, ledger, task)
        if profile is not None and info["capability"] <= 0:
            continue  # hard gate
        price = (profile.prompt_price + profile.completion_price) if profile else 0.0
        scored.append((m, info["reliability"], info["capability"], price))
    if free_tier:
        scored.sort(key=lambda x: (demotion(x[0]), -x[1], -x[2]))
    else:
        scored.sort(key=lambda x: (demotion(x[0]), x[3], -x[1], -x[2]))
    return [m for m, _, _, _ in scored]


def ordered_pool(pool, *, governor, ledger, task, free_tier, profiles=None,
                 call_lane="apply"):
    """The ONE call site for capability-aware pool ordering.

    Builds capability profiles from the governor's cached /models catalog (a
    run fetches it once via ``fetch_models()``), orders via :func:`order_pool`,
    and degrades gracefully to the caller's order when capability data is
    unavailable or ordering empties the pool. ``profiles`` may be supplied by
    callers that already hold them. ``call_lane="panel"`` marks a lane's
    internal self-serving request (vs an engine's apply-lane request) for
    troubleshooting output only.

    Returns ``(ordered_pool, profiles_or_None)``: the pool to route with, and
    the profiles to hand downstream (None when unavailable). Never raises.
    """
    try:
        if profiles is None:
            profiles_ = build_profiles_from_models(governor.fetch_models())
        else:
            profiles_ = profiles
        if not profiles_:
            # An empty catalog yields an empty profile map: that is NOT an
            # informed ordering. Signal unavailable so callers keep their own
            # configured order/model rather than trusting a prior-less echo.
            return list(pool or []), None
        pool_ = list(pool or [])
        if profiles_:
            # An id absent from the live catalog can never be routed (its
            # pricing lookup hard-fails the whole run) -- drop it at this
            # routing boundary so one stale configured id cannot kill a
            # session that has other models available.
            known = set(profiles_)
            pool_ = [m_ for m_ in pool_ if m_ in known]
        report = ledger.participation_report() if ledger is not None else None
        ordered = order_pool(pool_, profiles_, report, ledger=ledger,
                             task=task, free_tier=free_tier)
        if not ordered:
            # Ordering produced nothing (e.g. every model hard-gated to a
            # zero capability prior) -- signal 'no informed ordering' rather
            # than an echo that would displace a caller's configured model.
            return list(pool), None
        return ordered, profiles_
    except Exception as e:
        eprint(f"[capability] unavailable ({e}); routing on the given order.")
        return list(pool or []), None
