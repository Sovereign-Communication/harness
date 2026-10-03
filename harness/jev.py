"""Pure-stdlib TypeSafe System One adapter (JEV-P0).

Code owns exact mechanics (paths, syntax, hunk shape and thresholds); Jev owns
only bounded semantic judgments. The API contract is deliberately strict:
questions are only ``noul``, ``choice`` or ``score`` and answers must use the
official typed shapes from docs.typesafe.ai/api.md.
"""
import ast
import contextvars
import copy
import hashlib
import json
import math
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Tuple

from ._http import HttpTransport

# TypeSafe System One Jev pricing: $42 per billion tokens = $0.042 per million input tokens.
# Output tokens are free ($0.00). Monthly included account credit: $5.00 (~119M input tokens).
JEV_INPUT_PRICE_PER_MILLION = 0.042
JEV_MONTHLY_CREDIT_USD = 5.00
_PRIMITIVES = frozenset(("noul", "choice", "score"))


def jev_cost(input_tokens: int) -> float:
    """Return TypeSafe's input-only price; output tokens are free."""
    if isinstance(input_tokens, bool) or not isinstance(input_tokens, int) or input_tokens < 0:
        raise ValueError("input_tokens must be a non-negative integer")
    return input_tokens * JEV_INPUT_PRICE_PER_MILLION / 1_000_000


def jev_credit_status(input_tokens: Optional[int] = None) -> Dict[str, Any]:
    """Return TypeSafe Jev spend and monthly credit tracking status."""
    tokens = 0 if input_tokens is None else max(0, int(input_tokens))
    cost = jev_cost(tokens)
    return {
        "price_per_million_input": JEV_INPUT_PRICE_PER_MILLION,
        "output_cost": 0.0,
        "monthly_credit_usd": JEV_MONTHLY_CREDIT_USD,
        "input_tokens": tokens,
        "cost_usd": round(cost, 6),
        "remaining_credit_usd": round(max(0.0, JEV_MONTHLY_CREDIT_USD - cost), 6),
        "used_percent": round((cost / JEV_MONTHLY_CREDIT_USD) * 100.0, 2),
    }


@dataclass(frozen=True)
class JevEvaluationResult:
    """An honest typed evaluation envelope."""

    verdict: str
    # Confidence is only populated from Choice/Score confidence. Noul values
    # remain probabilities in ``supported``/``answers`` and are never renamed.
    confidence: float
    supported: float
    answers: Dict[str, Any] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)
    cost: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    is_fallback: bool = False
    model: Optional[str] = None
    usage_observed: bool = False
    model_observed: bool = False
    input_tokens_observed: bool = False
    output_tokens_observed: bool = False
    # DF-JEV-3: the provider billed this call and we could not use the answer.
    # Cost stays settled and honest; this flag is what lets a run report its
    # effective coverage instead of quietly presenting a paid call as signal.
    discarded: bool = False
    # Set only when the result is a local fallback. Values describe why live
    # Jev did not provide this judgment.
    fallback_reason: Optional[str] = None
    # True when this result was served from the evaluator's in-process cache:
    # no request left the machine, so cost and tokens are zero. Policy records
    # it distinctly in the ledger so a cache hit is never mistaken for a call.
    cache_hit: bool = False
    # Stable hash of the (state, questions) the result answers. Lets the
    # policy dedupe repeated identical fallbacks without re-hashing the state.
    state_hash: Optional[str] = None

    def is_passing(self, min_confidence: float = 0.70) -> bool:
        """Apply the configured action threshold without conflating signals."""
        return (self.verdict == "pass" and self.supported >= min_confidence
                and (self.confidence <= 0.0 or self.confidence >= min_confidence))


def _noul(question: str, yes: str, no: str) -> Dict[str, Any]:
    return {"type": "noul", "instructions": question,
            "criteria": {"true": yes, "false": no}}


def diff_question_pack() -> Dict[str, Dict[str, Any]]:
    """The one semantic question answerable from the diff and instruction.

    Paths, hunk shape, change presence, and AST validity are code-owned facts,
    so they are enforced locally rather than asking Jev to re-judge them.
    """
    return {
        "instruction_matches": _noul(
            "Does the changed code implement the supplied instruction?",
            "The changed behavior directly addresses the instruction.",
            "The changed behavior does not address, or contradicts, the instruction."),
    }


def triage_question_pack() -> Dict[str, Dict[str, Any]]:
    """Choice-based complexity route for Pillar 1; no arithmetic or paths."""
    return {
        "route": {
            "type": "choice",
            "instructions": "Choose the least capable execution route that can safely complete this task.",
            "criteria": {
                "free-distill": "bounded single-step or low-risk edit",
                "diff": "mechanical or multi-file diff-shaped edit",
                "frontier": "iterative, architectural, or high-dependency task",
            },
        },
        "requires_iteration": _noul(
            "Does the task require iterative control flow or dependent steps?",
            "The task requires iteration or dependent steps.",
            "The task is a bounded single-step edit."),
    }


def plan_question_pack() -> Dict[str, Dict[str, Any]]:
    """A narrow plan-site pack; no generic confidence question."""
    return {
        "requires_iteration": _noul(
            "Does the stated coding task require iterative control flow or multiple dependent steps?",
            "The task requires iteration or dependent steps.",
            "The task is a bounded declarative or single-step edit."),
        "requirement_complexity": {
            "type": "score",
            "instructions": "Rate the task's execution complexity from the supplied prompt and target list.",
            "criteria": ["single bounded edit", "several dependent edits", "iterative or algorithmic work"],
        },
    }


def _validate_questions(questions: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    if not isinstance(questions, dict) or not questions:
        raise ValueError("questions must be a non-empty map")
    out = {}
    for key, question in questions.items():
        if not isinstance(key, str) or not isinstance(question, dict):
            raise ValueError("questions must map string ids to objects")
        kind = question.get("type")
        if kind not in _PRIMITIVES:
            raise ValueError(f"unsupported TypeSafe primitive: {kind!r}")
        if "instructions" not in question:
            raise ValueError(f"question {key} is missing instructions")
        if kind in ("choice", "score") and "criteria" not in question:
            raise ValueError(f"question {key} is missing criteria")
        if kind == "choice" and (not isinstance(question["criteria"], dict) or not question["criteria"]):
            raise ValueError("choice criteria must be a non-empty map")
        if kind == "score" and (not isinstance(question["criteria"], list) or len(question["criteria"]) < 2):
            raise ValueError("score criteria must contain at least two levels")
        out[key] = {k: question[k] for k in ("type", "instructions", "criteria") if k in question}
    return out


def _number(value: Any, name: str, lo: float = 0.0, hi: Optional[float] = 1.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result) or result < lo or (hi is not None and result > hi):
        raise ValueError(f"{name} outside range")
    return result


def _probabilities(value: Any, name: str) -> Dict[str, float]:
    if not isinstance(value, dict) or not value:
        raise ValueError(f"{name} must be a non-empty map")
    parsed = {str(k): _number(v, name) for k, v in value.items()}
    if abs(sum(parsed.values()) - 1.0) > 1e-6:
        raise ValueError(f"{name} must sum to 1")
    return parsed


def _parse_answer(answer: Any, expected: str, key: str,
                 question: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(answer, dict) or answer.get("type") != expected:
        raise ValueError(f"answer {key} is not an official {expected} answer")
    if expected == "noul":
        return {"type": "noul", "noul": _number(answer["noul"], key + ".noul")}
    if expected == "choice":
        choice = answer.get("choice")
        parsed_probs = _probabilities(answer.get("probabilities"), key + ".probabilities")
        criteria = question.get("criteria")
        if not isinstance(choice, str) or choice not in parsed_probs:
            raise ValueError(f"choice answer {key} has an invalid choice")
        unmatched: List[str] = []
        if isinstance(criteria, dict):
            declared = {str(name) for name in criteria}
            unknown = set(parsed_probs) - declared
            if unknown:
                # An option the operator never declared is fatal: it cannot be
                # recorded as an honest unmatched, it is out of vocabulary.
                raise ValueError(
                    f"choice answer {key} declares undeclared options: "
                    + ", ".join(sorted(unknown)))
            # DF-JEV-3: a subset that already normalizes onto the declared
            # criteria is a RECOVERABLE shape, not a rejection. The model put
            # zero on the options it omitted; recording them as None keeps the
            # answer usable and never invents a probability for them. Measured
            # 2026-09-25: the old hard reject discarded 94.1% of answers on a
            # 7-criteria pack -- the stricter the vocabulary, the more reliably
            # the tool paid for answers it threw away.
            unmatched = sorted(declared - set(parsed_probs))
            for option in unmatched:
                parsed_probs[option] = None
        return {"type": "choice", "choice": choice, "probabilities": parsed_probs,
                "confidence": _number(answer["confidence"], key + ".confidence"),
                "unmatched_options": unmatched}
    score = answer.get("score")
    legend = answer.get("legend")
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not isinstance(legend, dict) or not legend:
        raise ValueError(f"score answer {key} is missing required fields")
    parsed_probs = _probabilities(answer.get("probabilities"), key + ".probabilities")
    if set(parsed_probs) != {str(k) for k in legend}:
        # Unlike a choice, a Score's legend is the provider's OWN answer, not
        # an operator-declared vocabulary, so a key-set mismatch here is
        # genuinely malformed rather than an omitted option. Stay strict.
        raise ValueError(f"score answer {key} legend does not match probabilities")
    return {"type": "score", "score": float(score), "legend": dict(legend),
            "probabilities": parsed_probs,
            "confidence": _number(answer["confidence"], key + ".confidence")}


# Bump when the typed answer contract changes so stale cached answers can
# never be replayed against a newer pack/parser.
JEV_CACHE_VERSION = 1
JEV_CACHE_MAX_ENTRIES = 256
JEV_CACHE_TTL_SECONDS = 1800.0


def _digest(*parts: Any) -> str:
    """Total sha256 identity over any parts; it never raises.

    Canonical JSON when possible, otherwise a ``repr`` (mixed-type keys,
    circular state). Used for dedupe identity, where a collision only costs a
    merged ledger row. Cache keys use :func:`_strict_digest` instead.
    """
    try:
        blob = json.dumps(parts, sort_keys=True, default=str,
                          separators=(",", ":"))
    except Exception:
        try:
            blob = repr(parts)
        except Exception:
            # Cannot even describe itself: identity by type and object id, so
            # only the same live object (or an equal-shaped one) collides.
            blob = "|".join("{}#{}".format(type(part).__name__, id(part))
                            for part in parts)
    return hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()


def _strict_digest(*parts: Any) -> Optional[str]:
    """Canonical sha256 over strictly JSON-able parts, else ``None``.

    ``None`` means "do not cache": a value that is not plain JSON could
    otherwise collapse onto a different value that stringifies the same.
    """
    try:
        blob = json.dumps(parts, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError):
        return None
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class _Flight:
    """One in-flight request that identical callers wait on."""

    def __init__(self):
        self.event = threading.Event()
        self.value: Any = None


class JevCache:
    """Bounded, thread-safe LRU of successful typed answers with a TTL.

    Only parsed live answers are stored (see ``JevEvaluator.evaluate``), never
    transport failures, rejections or local fallbacks, so a cache hit can only
    ever replay something Jev really said about exactly this state.
    """

    def __init__(self, max_entries: int = JEV_CACHE_MAX_ENTRIES,
                 ttl: float = JEV_CACHE_TTL_SECONDS, clock=time.monotonic):
        self.max_entries = max(0, int(max_entries))
        self.ttl = float(ttl)
        self._clock = clock
        self._lock = threading.Lock()
        self._items: "OrderedDict[str, Any]" = OrderedDict()
        self._inflight: Dict[str, "_Flight"] = {}
        self.hits = 0
        self.misses = 0

    def get(self, key: str):
        with self._lock:
            entry = self._items.get(key)
            if entry is None:
                self.misses += 1
                return None
            stored_at, value = entry
            if self.ttl > 0 and self._clock() - stored_at > self.ttl:
                del self._items[key]
                self.misses += 1
                return None
            self._items.move_to_end(key)
            self.hits += 1
            # A private copy: callers may mutate answers/reasons freely
            # without ever poisoning what the next hit replays.
            return copy.deepcopy(value)

    def begin(self, key: str) -> Tuple["_Flight", bool]:
        """Single-flight: ``(flight, True)`` for the one caller that should
        dispatch this key; ``(flight, False)`` for those that should wait.

        The flight carries the leader's answer itself, so waiters coalesce
        even when the cache stores nothing (``max_entries=0``).
        """
        with self._lock:
            flight = self._inflight.get(key)
            if flight is not None:
                return flight, False
            flight = self._inflight[key] = _Flight()
            return flight, True

    def finish(self, key: str, value: Any = None) -> None:
        """Release waiters; ``value`` (a parsed live answer) is handed to them."""
        with self._lock:
            flight = self._inflight.pop(key, None)
        if flight is not None:
            flight.value = copy.deepcopy(value)
            flight.event.set()

    def put(self, key: str, value: Any) -> None:
        if self.max_entries <= 0:
            return
        value = copy.deepcopy(value)
        with self._lock:
            self._items[key] = (self._clock(), value)
            self._items.move_to_end(key)
            while len(self._items) > self.max_entries:
                self._items.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self.hits = self.misses = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


# The site circuit breaker lives with the dispatcher so an open breaker
# degrades EXACTLY like a transport failure at every site: the same local
# fallback shape, only the reason (``circuit_open``) differs.
BREAKER_FAILURE_THRESHOLD = 3
BREAKER_COOLDOWN_SECONDS = 30.0
# Wait for a concurrent identical request instead of paying for it twice.
SINGLE_FLIGHT_WAIT_SECONDS = 5.0
SINGLE_FLIGHT_ROUNDS = 3
SHARED_BREAKER_CAP = 64
# The policy names the site it is about to dispatch for; the evaluator reads
# it here so the breaker is per-site without changing any call signature.
ACTIVE_SITE: "contextvars.ContextVar[str]" = contextvars.ContextVar(
    "jev_active_site", default="")
# One-shot hook the policy installs so the spend reservation happens inside
# the evaluator, AFTER a cache hit or single-flight wait could have answered
# the call for free and immediately BEFORE a request would leave the machine:
# a billed request is never dispatched unreserved.
ACTIVE_RESERVER: "contextvars.ContextVar[Any]" = contextvars.ContextVar(
    "jev_active_reserver", default=None)


class CircuitBreakers:
    """Per-site breakers: consecutive transport failures open one.

    Only a live, parsed round trip is a success; only transport/HTTP failures
    are failures; local rejections, 401/422 and unparseable answers are
    neither. After the cooldown exactly one probe is let through.
    """

    def __init__(self, threshold: int = BREAKER_FAILURE_THRESHOLD,
                 cooldown: float = BREAKER_COOLDOWN_SECONDS,
                 clock=time.monotonic):
        self.threshold = max(1, int(threshold))
        self.cooldown = float(cooldown)
        self._clock = clock
        self._lock = threading.Lock()
        self._sites: Dict[str, Dict[str, Any]] = {}

    def acquire(self, site: str) -> Tuple[Optional[str], int]:
        """``(None, generation)`` to proceed, else ``(why refused, 0)``.

        Pass the generation back to :meth:`record`: an outcome from a call
        that began before the breaker last changed state is stale and can
        neither close it, clear a probe nor extend its cooldown.
        """
        with self._lock:
            state = self._sites.setdefault(site, {
                "failures": 0, "opened_at": None, "probing": False,
                "probe_at": 0.0, "generation": 0})
            if state["opened_at"] is None:
                return None, state["generation"]
            now = self._clock()
            ready = now - state["opened_at"] >= self.cooldown
            stale = state["probing"] and now - state["probe_at"] >= self.cooldown
            if ready and (not state["probing"] or stale):
                state["probing"] = True
                state["probe_at"] = now
                state["generation"] += 1
                return None, state["generation"]
            return ("Jev circuit open for site {!r} after {} consecutive "
                    "transport failures".format(site, state["failures"]), 0)

    def record(self, site: str, outcome: str, generation: Optional[int] = None) -> None:
        """``outcome`` is ``success``, ``failure`` or ``neutral``."""
        with self._lock:
            state = self._sites.setdefault(site, {
                "failures": 0, "opened_at": None, "probing": False,
                "probe_at": 0.0, "generation": 0})
            if generation is not None and generation != state["generation"]:
                return  # stale: this call predates the current breaker state
            probing = state["probing"]
            state["probing"] = False
            if outcome == "success":
                if state["opened_at"] is not None:
                    state["generation"] += 1
                state["failures"] = 0
                state["opened_at"] = None
            elif outcome == "failure":
                state["failures"] += 1
                if probing or state["failures"] >= self.threshold:
                    if state["opened_at"] is None or probing:
                        state["generation"] += 1
                    state["opened_at"] = self._clock()

    def would_allow(self, site: str) -> bool:
        """Side-effect-free: could a call be dispatched right now?"""
        with self._lock:
            state = self._sites.get(site)
            if not state or state["opened_at"] is None:
                return True
            now = self._clock()
            if state["probing"] and now - state["probe_at"] < self.cooldown:
                return False  # a probe is already in flight
            return now - state["opened_at"] >= self.cooldown

    def snapshot(self, site: str) -> Dict[str, Any]:
        with self._lock:
            return dict(self._sites.get(site) or {})


_SHARED_BREAKERS: "OrderedDict[str, CircuitBreakers]" = OrderedDict()
_SHARED_BREAKERS_LOCK = threading.Lock()


def shared_breakers(endpoint: str, api_key: Optional[str]) -> CircuitBreakers:
    """The process-wide breaker board for one endpoint + key.

    Policies are built per request, so an instance-local breaker could never
    accumulate the consecutive failures it exists to count.
    """
    ident = hashlib.sha256(
        "{}\0{}".format(endpoint, api_key or "").encode("utf-8")).hexdigest()
    with _SHARED_BREAKERS_LOCK:
        board = _SHARED_BREAKERS.pop(ident, None) or CircuitBreakers()
        _SHARED_BREAKERS[ident] = board
        while len(_SHARED_BREAKERS) > SHARED_BREAKER_CAP:
            _SHARED_BREAKERS.popitem(last=False)
        return board


# One cache for the whole process: every request builds its own policy and
# evaluator, so an instance-local cache could never see a repeat. It is shared
# only by evaluators on the real network transport; an injected transport (a
# test double or custom wire) gets a private cache so replay can never cross
# between unrelated senders.
PROCESS_CACHE = JevCache()


class JevEvaluator:
    #: The policy may install ``ACTIVE_RESERVER`` and expect it to be called
    #: right before a request would be dispatched (never on a cache hit).
    lazy_reserve = True

    def __init__(self, api_key: Optional[str] = None, endpoint: str = "https://api.typesafe.ai/v1/systemone",
                 transport: Optional[HttpTransport] = None, settings: Optional[Any] = None,
                 cache: Optional[JevCache] = None,
                 breakers: Optional[CircuitBreakers] = None):
        self.explicitly_disabled = bool(getattr(settings, "jev_disabled", False)) if settings else False
        self.api_key = (None if self.explicitly_disabled else
                        api_key or (getattr(settings, "jev_api_key", None)
                                    if settings else None))
        self.endpoint = (getattr(settings, "jev_endpoint", endpoint) if settings else endpoint) or endpoint
        self.model = getattr(settings, "jev_model", "jev-latest") if settings else "jev-latest"
        self.min_confidence = getattr(settings, "min_confidence", 0.70) if settings else 0.70
        self.transport = transport or HttpTransport()
        real_wire = type(self.transport) is HttpTransport
        if cache is not None:
            self.cache = cache
        elif real_wire:
            self.cache = PROCESS_CACHE
        else:
            self.cache = JevCache()
        if breakers is not None:
            self.breakers = breakers
        elif real_wire:
            self.breakers = shared_breakers(self.endpoint, self.api_key)
        else:
            self.breakers = CircuitBreakers()
        self._led = threading.local()

    def _cache_key(self, state: Any, active: Dict[str, Any]) -> Optional[str]:
        key_id = hashlib.sha256((self.api_key or "").encode("utf-8")).hexdigest()[:16]
        return _strict_digest(JEV_CACHE_VERSION, self.endpoint, self.model,
                              key_id, state, active)

    @staticmethod
    def _cache_hit(cached: "JevEvaluationResult") -> "JevEvaluationResult":
        # Identical state + questions + model: replay the parsed answer. Zero
        # tokens and zero cost, flagged so the ledger records a hit rather
        # than a phantom paid call.
        return replace(
            cached, cost=0.0, input_tokens=0, output_tokens=0,
            usage_observed=False, input_tokens_observed=False,
            output_tokens_observed=False, cache_hit=True,
            reasons=list(cached.reasons) + [
                "jev cache hit: identical state/questions/model; no request sent"])

    def evaluate(self, state: Any, questions: Optional[Dict[str, Any]] = None) -> JevEvaluationResult:
        raw = diff_question_pack() if questions is None else questions
        try:
            active = _validate_questions(raw)
        except ValueError as exc:
            return self._failure(str(exc), fallback=False)
        local_state = state if isinstance(state, dict) else {"content": str(state)}
        state_hash = _digest(state, active)

        def local(reason: str) -> JevEvaluationResult:
            return replace(self._local_structural_eval(
                local_state, fallback_reason=reason), state_hash=state_hash)

        if not self.api_key:
            return local("explicit_disable" if self.explicitly_disabled
                         else "missing_key")
        cache_key = self._cache_key(state, active)
        led = self._led.__dict__.setdefault("keys", set())
        leader = False
        if cache_key is not None and cache_key not in led:
            # Cache, then single-flight: identical callers wait for the
            # leader's answer (no reservation, no request of their own). If
            # the leader failed, exactly one waiter is re-elected.
            for _ in range(SINGLE_FLIGHT_ROUNDS):
                cached = self.cache.get(cache_key)
                if cached is not None:
                    return self._cache_hit(cached)
                flight, leader = self.cache.begin(cache_key)
                if leader:
                    led.add(cache_key)
                    break
                if not flight.event.wait(SINGLE_FLIGHT_WAIT_SECONDS):
                    break  # leader stalled or died: dispatch for ourselves
                if flight.value is not None:
                    return self._cache_hit(copy.deepcopy(flight.value))
        elif cache_key is not None:
            cached = self.cache.get(cache_key)
            if cached is not None:
                return self._cache_hit(cached)
        site = ACTIVE_SITE.get()
        shared = None
        generation = None
        outcome = "neutral"
        try:
            refusal, generation = self.breakers.acquire(site)
            if refusal is not None:
                generation = None
                # Refused locally: neither a success nor a failure, and the
                # shape is exactly a transport failure's, so every site
                # degrades as it already does when Jev is unreachable.
                return local("circuit_open")
            reserver = ACTIVE_RESERVER.get()
            if reserver is not None:
                ACTIVE_RESERVER.set(None)
                reserver()  # may raise HarnessError: a refused reservation
            try:
                status, resp = self.transport.post(
                    self.endpoint, self.api_key,
                    {"model": self.model, "state": state, "questions": active})
                if status == 200 and isinstance(resp, dict):
                    try:
                        parsed = replace(self._parse_jev_response(resp, active),
                                         state_hash=state_hash)
                    except Exception as exc:
                        # A billed response we cannot use: settle the real
                        # usage and flag it discarded. The wire worked, so
                        # this is neutral for the breaker, never a failure.
                        usage = resp.get("usage") if isinstance(resp.get("usage"), dict) else {}
                        input_tokens = usage.get("input_tokens", 0)
                        output_tokens = usage.get("output_tokens", 0)
                        if (isinstance(input_tokens, bool) or not isinstance(input_tokens, int)
                                or input_tokens < 0):
                            input_tokens = 0
                        if (isinstance(output_tokens, bool) or not isinstance(output_tokens, int)
                                or output_tokens < 0):
                            output_tokens = 0
                        # DF-JEV-3: the provider billed this response and we
                        # cannot use the answer. Mark it discarded so the
                        # caller can report the loss rather than degrading
                        # as if nothing was spent.
                        return replace(self._failure(
                            "invalid TypeSafe response: " + str(exc), fallback=False,
                            input_tokens=input_tokens, output_tokens=output_tokens,
                            input_tokens_observed=input_tokens > 0,
                            output_tokens_observed=output_tokens > 0,
                            discarded=input_tokens > 0), state_hash=state_hash)
                    outcome = "success"
                    shared = parsed
                    # Only a fully parsed live answer is cacheable; every
                    # failure stays uncached so a retry can succeed.
                    if cache_key is not None:
                        self.cache.put(cache_key, parsed)
                    return parsed
                if status in (401, 422):
                    return replace(self._failure(
                        f"TypeSafe request rejected (HTTP {status})", fallback=False),
                        state_hash=state_hash)
                outcome = "failure"
                return local("http_fallback")
            except Exception:
                outcome = "failure"
                return local("transport_failure")
        finally:
            if generation is not None:
                self.breakers.record(site, outcome, generation)
            if leader:
                led.discard(cache_key)
                self.cache.finish(cache_key, shared)

    def evaluate_once(self, state: Any,
                      questions: Dict[str, Any]) -> JevEvaluationResult:
        """One strict request; it respects and feeds the site breaker."""
        if not self.api_key:
            return self._evaluate_once(state, questions)
        site = ACTIVE_SITE.get()
        refusal, generation = self.breakers.acquire(site)
        if refusal is not None:
            return self._failure(refusal, fallback=True,
                                 fallback_reason="circuit_open")
        outcome = "neutral"
        try:
            result = self._evaluate_once(state, questions)
            if not result.is_fallback:
                reason = result.reasons[0] if result.reasons else ""
                if result.verdict != "fail" or result.answers:
                    outcome = "success"
                elif (reason.startswith("TypeSafe transport failed")
                      or (reason.startswith("TypeSafe request failed (HTTP ")
                          and not reason.endswith(("401)", "422)")))):
                    outcome = "failure"
            return result
        finally:
            self.breakers.record(site, outcome, generation)

    def _evaluate_once(self, state: Any,
                       questions: Dict[str, Any]) -> JevEvaluationResult:
        """Make one strict, no-fallback TypeSafe request.

        This path is for assessments where a heuristic answer would be
        misleading. It dispatches through ``post_once`` when the transport
        supports it, rejects missing or extra answer ids and missing model
        identity, and never retries or converts failure into local approval.
        """
        try:
            active = _validate_questions(questions)
        except (TypeError, ValueError) as exc:
            return self._failure("invalid TypeSafe question pack: " + str(exc),
                                 fallback=False)
        if not self.api_key:
            reason = "explicit_disable" if self.explicitly_disabled else "missing_key"
            return self._failure("TypeSafe key unavailable", fallback=True,
                                 fallback_reason=reason)

        payload = {"model": self.model, "state": state,
                   "questions": active}
        post_once = getattr(self.transport, "post_once", None)
        if not callable(post_once):
            return self._failure(
                "TypeSafe transport does not support a one-attempt request",
                fallback=False)
        try:
            status, response = post_once(
                self.endpoint, self.api_key, payload)
        except Exception as exc:
            return self._failure(
                "TypeSafe transport failed (" + type(exc).__name__ + ")",
                fallback=False)

        usage = response.get("usage") if isinstance(response, dict) else None
        (input_tokens, output_tokens, input_observed,
         output_observed) = self._observed_usage(usage)
        usage_observed = input_observed and output_observed
        model = response.get("model") if isinstance(response, dict) else None
        model_observed = isinstance(model, str) and bool(model.strip())
        if status != 200:
            return self._failure(
                "TypeSafe request failed (HTTP {})".format(status),
                fallback=False, input_tokens=input_tokens,
                output_tokens=output_tokens, usage_observed=usage_observed,
                model=model if model_observed else self.model,
                model_observed=model_observed,
                input_tokens_observed=input_observed,
                output_tokens_observed=output_observed)
        if not isinstance(response, dict):
            return self._failure(
                "invalid TypeSafe response: expected an object",
                fallback=False, input_tokens=input_tokens,
                output_tokens=output_tokens, usage_observed=usage_observed,
                input_tokens_observed=input_observed,
                output_tokens_observed=output_observed)
        if not model_observed:
            return self._failure(
                "invalid TypeSafe response: missing observed model identity",
                fallback=False, input_tokens=input_tokens,
                output_tokens=output_tokens, usage_observed=usage_observed,
                input_tokens_observed=input_observed,
                output_tokens_observed=output_observed)
        answers = response.get("answers")
        if not isinstance(answers, dict) or set(answers) != set(active):
            return self._failure(
                "invalid TypeSafe response: answer ids do not match the pack",
                fallback=False, input_tokens=input_tokens,
                output_tokens=output_tokens, usage_observed=usage_observed,
                model=model, model_observed=True,
                input_tokens_observed=input_observed,
                output_tokens_observed=output_observed)
        try:
            result = self._parse_jev_response(response, active)
        except (KeyError, TypeError, ValueError) as exc:
            return self._failure(
                "invalid TypeSafe response: " + str(exc), fallback=False,
                input_tokens=input_tokens, output_tokens=output_tokens,
                usage_observed=usage_observed, model=model,
                model_observed=True,
                input_tokens_observed=input_observed,
                output_tokens_observed=output_observed,
                # DF-JEV-3: billed but unusable. Same discipline on the strict
                # one-attempt path -- real usage settles, and the loss is named.
                discarded=input_observed)
        # _parse_jev_response already returns a frozen result with these
        # observed flags set after validating both usage fields and the
        # response model. Do not mutate the frozen dataclass here.
        return result

    @staticmethod
    def _observed_usage(usage):
        if not isinstance(usage, dict):
            return 0, 0, False, False
        values = []
        observed = []
        for usage_field in ("input_tokens", "output_tokens"):
            value = usage.get(usage_field)
            if (isinstance(value, bool) or not isinstance(value, int)
                    or value < 0):
                values.append(None)
                observed.append(False)
            else:
                values.append(value)
                observed.append(True)
        return (values[0] or 0, values[1] or 0,
                observed[0], observed[1])

    def _failure(self, reason: str, fallback: bool, input_tokens: int = 0,
                 output_tokens: int = 0, usage_observed: bool = False,
                 model: Optional[str] = None,
                 model_observed: bool = False,
                 input_tokens_observed: bool = False,
                 output_tokens_observed: bool = False,
                 discarded: bool = False,
                 fallback_reason: Optional[str] = None) -> JevEvaluationResult:
        return JevEvaluationResult(
            "fail", 0.0, 0.0, {}, [reason],
            cost=jev_cost(input_tokens), input_tokens=input_tokens,
            output_tokens=output_tokens, is_fallback=fallback,
            model=model or self.model, usage_observed=usage_observed,
            model_observed=model_observed,
            input_tokens_observed=input_tokens_observed,
            output_tokens_observed=output_tokens_observed,
            discarded=discarded, fallback_reason=fallback_reason)

    def _parse_jev_response(self, resp: Dict[str, Any], questions: Dict[str, Any]) -> JevEvaluationResult:
        if not isinstance(resp.get("answers"), dict) or not isinstance(resp.get("usage"), dict):
            raise ValueError("response requires answers and usage")
        usage = resp["usage"]
        input_tokens = usage["input_tokens"]
        output_tokens = usage["output_tokens"]
        if isinstance(input_tokens, bool) or not isinstance(input_tokens, int) or input_tokens < 0:
            raise ValueError("usage.input_tokens must be a non-negative integer")
        if isinstance(output_tokens, bool) or not isinstance(output_tokens, int) or output_tokens < 0:
            raise ValueError("usage.output_tokens must be a non-negative integer")
        expected = _validate_questions(questions)
        answers = {}
        for key, question in expected.items():
            if key not in resp["answers"]:
                raise ValueError(f"response is missing answer {key}")
            answers[key] = _parse_answer(resp["answers"][key], question["type"], key, question)
        nouls = [a["noul"] for a in answers.values() if a["type"] == "noul"]
        supported = min(nouls) if nouls else 1.0
        action_confidences = [a["confidence"] for a in answers.values() if a["type"] in ("choice", "score")]
        confidence = min(action_confidences) if action_confidences else 0.0
        # With no Choice/Score action confidence, noul probabilities are the
        # separate supported signal. A purely-noul pack is still thresholded
        # by supported in is_passing; confidence remains explicitly absent/0.
        verdict = "pass" if supported >= self.min_confidence and (not action_confidences or confidence >= self.min_confidence) else "fail"
        reasons = []
        for key, answer in answers.items():
            if answer["type"] == "noul":
                reasons.append(f"{key} (noul): {answer['noul']}")
            elif answer["type"] == "choice":
                reasons.append(f"{key} (choice): {answer['choice']} (conf: {answer['confidence']})")
            else:
                reasons.append(f"{key} (score): {answer['score']} (conf: {answer['confidence']})")
        return JevEvaluationResult(verdict, confidence, supported, answers, reasons,
                                   cost=jev_cost(input_tokens), input_tokens=input_tokens,
                                   output_tokens=output_tokens, model=resp.get("model", self.model),
                                   usage_observed=True,
                                   model_observed=(isinstance(resp.get("model"), str)
                                                   and bool(resp.get("model", "").strip())),
                                   input_tokens_observed=True,
                                   output_tokens_observed=True)

    def _local_structural_eval(self, state: Dict[str, Any], fallback: bool = True,
                               fallback_reason: Optional[str] = "missing_key") -> JevEvaluationResult:
        code = state.get("code") or state.get("content") or ""
        json_content = state.get("json_content") or ""
        if json_content:
            try:
                json.loads(json_content)
            except Exception as exc:
                return self._failure(f"Invalid JSON: {exc}", fallback, fallback_reason=fallback_reason if fallback else None)
        if code and not state.get("diff"):
            try:
                ast.parse(code)
            except SyntaxError as exc:
                return self._failure(f"Python SyntaxError: {exc.msg} at line {exc.lineno}", fallback, fallback_reason=fallback_reason if fallback else None)
        if state.get("diff"):
            facts = state
            checks = [("hunk_shape_ok", bool(facts.get("hunk_shape_ok", False)), "diff hunk shape"),
                      ("has_changes", bool(facts.get("has_changes", False)), "diff change"),
                      ("path_matches", bool(facts.get("path_matches", False)), "target path")]
            if facts.get("ast_parse_ok") is not None:
                checks.append(("ast_parse_ok", bool(facts["ast_parse_ok"]), "AST parse"))
            for _, ok, label in checks:
                if not ok:
                    return JevEvaluationResult("fail", 0.0, 0.0, {}, [f"Code-owned {label} check failed."], is_fallback=fallback, model=self.model, fallback_reason=fallback_reason if fallback else None)
        elif not code and not json_content and not state.get("response") and not state.get("prompt"):
            return self._failure("Empty candidate state returned.", fallback, fallback_reason=fallback_reason if fallback else None)
        return JevEvaluationResult("pass", 0.0, 1.0,
                                   {"mechanical_checks": "passed"},
                                   ["All local code-owned structural checks passed."], is_fallback=fallback, model=self.model, fallback_reason=fallback_reason if fallback else None)

    def check_diff_mechanics(self, diff: str, instruction: str = "", file_path: str = "",
                             candidate: Optional[str] = None):
        """Return the code-owned diff state and its local verdict."""
        state = _diff_state(diff or "", instruction or "", file_path or "", candidate=candidate)
        fallback = not bool(self.api_key)
        fallback_reason = ("explicit_disable" if self.explicitly_disabled
                           else "missing_key") if fallback else None
        return state, self._local_structural_eval(
            state, fallback=fallback, fallback_reason=fallback_reason)

    def verify_diff_mechanics(self, diff: str, instruction: str = "", file_path: str = "",
                              candidate: Optional[str] = None, preflight=None) -> JevEvaluationResult:
        state, mechanical = self.check_diff_mechanics(
            diff, instruction, file_path, candidate=candidate)
        if mechanical.verdict != "pass":
            return mechanical
        if preflight is not None:
            preflight(state)
        return self.evaluate(state)

    def evaluate_plan_requirements(self, prompt: str, target_files: Optional[List[str]] = None) -> JevEvaluationResult:
        prompt = prompt or ""
        questions = plan_question_pack()
        result = self.evaluate({"prompt": prompt, "target_files": list(target_files or [])}, questions)
        if not result.is_fallback:
            answer = result.answers.get("requires_iteration", {"noul": 0.0})
            return JevEvaluationResult(result.verdict, result.confidence, result.supported,
                                       {"requires_iteration": answer["noul"] >= 0.5, "raw": result.answers},
                                       result.reasons, result.cost, result.input_tokens,
                                       result.output_tokens, False, result.model,
                                       discarded=result.discarded,
                                       fallback_reason=result.fallback_reason,
                                       cache_hit=result.cache_hit,
                                       state_hash=result.state_hash)
        lower = prompt.lower()
        has_iter = any(word in lower for word in ("loop", "iterat", "branch", "recur", "dag", "retry", "traverse", "graph", "algorithm", "cycle"))
        return JevEvaluationResult("pass", 0.0, 1.0, {"requires_iteration": has_iter},
                                   ["Detected iterative/algorithmic requirements" if has_iter else "Standard declarative edit flow"],
                                   is_fallback=True, model=result.model,
                                   fallback_reason=result.fallback_reason or "plan_heuristic",
                                   state_hash=result.state_hash)


def _looks_like_diff(diff: str) -> bool:
    return bool(diff.strip()) and diff.lstrip().startswith(("---", "diff "))


def _valid_hunks(lines: List[str]) -> bool:
    """Require a hunk header, valid body prefixes, and at least one change."""
    saw_hunk = saw_change = False
    in_hunk = False
    for line in lines:
        if line.startswith("@@"):
            saw_hunk = in_hunk = True
            continue
        if not in_hunk:
            continue
        if line.startswith(("--- ", "+++ ", "diff ")):
            in_hunk = False
            continue
        if line.startswith(("+", "-")):
            saw_change = True
        elif not line.startswith((" ", "\\")):
            return False
    return saw_hunk and saw_change


def _path_matches(file_path: str, paths: List[str]) -> bool:
    if not file_path or not paths:
        return bool(not file_path)
    target = file_path.replace("\\", "/").rstrip("/")
    for path in paths:
        path = path.replace("\\", "/").rstrip("/")
        if target == path or target.endswith("/" + path) or path.endswith("/" + target):
            return True
    return False


def _diff_state(diff: str, instruction: str, file_path: str,
                candidate: Optional[str] = None) -> Dict[str, Any]:
    lines = diff.splitlines()
    headers = [line[4:].strip() for line in lines if line.startswith(("--- ", "+++ "))]
    paths = [p[2:] if p.startswith(("a/", "b/")) else p for p in headers]
    changed = [line for line in lines
               if line.startswith(("+", "-"))
               and not line.startswith(("--- ", "+++ "))]
    ast_ok = None
    if isinstance(candidate, str) and file_path.lower().endswith(".py"):
        try:
            ast.parse(candidate)
            ast_ok = True
        except SyntaxError:
            ast_ok = False
    return {"diff": diff, "instruction": instruction, "file_path": file_path,
            "path_matches": _path_matches(file_path, paths),
            "hunk_shape_ok": _looks_like_diff(diff) and _valid_hunks(lines),
            "ast_parse_ok": ast_ok, "has_changes": bool(changed)}
