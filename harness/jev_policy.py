"""The single Jev policy owner for all Harness decision lanes (JEV-P1/P3).

The P0 evaluator owns TypeSafe parsing and code-owned mechanics. This module
owns lane policy: when a typed call may dispatch, its bounded spend, one ledger
event, and the structural envelope shared by apply, plan, waist, and agent
lanes. P3 utilization packs live in :mod:`harness.jev_packs` and are imported
here — still ONE policy owner, never a second Jev client.
"""
import atexit
import difflib
import os
import threading
import weakref
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import (Any, Callable, Dict, Iterable, List, Optional, Sequence,
                    Tuple)

from .errors import HarnessError, ToolCancelled
from .events import submit_with_context
from .config import HARD_MAX_COST
from .jev import (ACTIVE_GUARD, ACTIVE_RESERVER, ACTIVE_SITE, BREAKER_COOLDOWN_SECONDS,
                  BREAKER_FAILURE_THRESHOLD, CircuitBreakers,
                  JevEvaluationResult, JevEvaluator, _digest, jev_cost,
                  triage_question_pack)
from .output import eprint
from .route_pack import (ROUTE_QUERY_SITE, choose_rung_for_tier, fallback_route,
                         route_combo,
                         route_question_pack as route_query_pack,
                         tier_floor_for_goal, tier_rank,
                         validate_route_pack)
from .jev_packs import (
    ANSWER_PACK_VERSION,
    AUDIT_DIMENSIONS_SITE,
    HUL_SCOPE_SITE,
    LOG_FACTOR_SITE,
    PHASE_COMPLETION_SITE,
    PROVISION_SITE,
    REPO_SUMMARY_SITE,
    HOURGLASS_STAGE_DIMENSIONS,
    HOURGLASS_STAGE_PACK_ID,
    HOURGLASS_STAGE_PACK_VERSION,
    HOURGLASS_STAGE_SITE,
    hourglass_stage_question_pack,
    normalize_restart_target,
    stage_judgment_requirement,
    VISION_ASSESSMENT_SITE,
    VISION_ASSESSMENT_MAX_REQUEST_TOKENS,
    VISION_ASSESSMENT_MAX_STATE_QUESTION_TOKENS,
    VISION_ASSESSMENT_PACK_ID,
    VISION_ASSESSMENT_PACK_VERSION,
    VisionAssessmentEnvelope,
    VisionCategoryAssessment,
    SCOPE_COVERAGE_HOLD,
    SCOPE_NOUL_HOLD,
    answer_question_pack,
    claim_support_question_pack,
    claims_from_payload,
    completion_bar_question_pack,
    escalation_decision_pack,
    completion_question_pack,
    file_relevance_question_pack,
    heuristic_file_relevance,
    heuristic_repo_axes,
    heuristic_requires_iteration,
    heuristic_route,
    hul_scope_question_pack,
    issue_sort_question_pack,
    log_factor_question_pack,
    match_completion_keywords,
    match_keywords,
    named_artifact_status,
    normalize_complexity_class,
    normalize_route,
    repo_summary_question_pack,
    route_question_pack,
    DEFAULT_VISION_ASSESSMENT_PACK,
    DECISION_CONFIDENCE_THRESHOLD,
    DECISION_DISPOSITION_MARGIN,
    DECISION_PACK_VERSION,
    DECISION_SITE,
    compose_decision_verdict,
    decision_question_pack,
    validate_vision_assessment_answers,
    validate_vision_assessment_pack,
    vision_assessment_preflight,
    vision_assessment_question_pack,
    sanitize_vision_state,
    scope_in_scope_holds,
    validate_candidates,
    validate_completion_pack,
    validate_log_pack,
    validate_operator_pack,
    validate_repo_summary_pack,
)

JEV_MAX_INPUT_TOKENS = 1024

# Safety net: a fallback row never lands without a reason. Sites name their
# own; this value only ever appears if a new path forgets to.
UNATTRIBUTED_FALLBACK = "unattributed_fallback"
# Fallback ledger-row bounds: one row each time an identical (site, reason,
# state) fallback count reaches a power of two, and past this many distinct
# states per (site, reason) only power-of-two totals are written.
FALLBACK_DISTINCT_ROW_CAP = 64
FALLBACK_TRACKED_KEYS = 4096
FANOUT_MAX_WORKERS = 4
# A live route choice below this confidence carries no usable signal: it is
# discarded (billed) rather than honored by the confidence-aware acceptance.
LOW_CONFIDENCE_FLOOR = 0.05


# Every policy, so unwritten deduped-fallback tails can be flushed at the end
# of a run, a command, a server, or the process (policies are per request).
_LIVE_POLICIES: "weakref.WeakSet" = weakref.WeakSet()


ATEXIT_FLUSH_TIMEOUT_SECONDS = 3.0


def _live_policies() -> tuple:
    """A snapshot of the live policies that concurrent creation cannot break.

    Iterating a WeakSet while another thread adds to it can raise; retry, and
    give up with an empty snapshot rather than ever raising.
    """
    for _ in range(8):
        try:
            return tuple(_LIVE_POLICIES)
        except Exception:
            continue
    return ()


def flush_all_fallbacks() -> int:
    """Best-effort flush of every live policy's unwritten fallback tail.

    Never raises: it runs in ``finally`` blocks and at exit, where an error
    here must not mask the command's own result.
    """
    written = 0
    for policy in _live_policies():
        try:
            written += policy.flush_fallbacks()
        except Exception:
            pass  # a flush must never break shutdown
    return written


def _flush_at_exit() -> None:
    """Flush at interpreter exit, bounded: a stuck ledger cannot hang exit."""
    try:
        worker = threading.Thread(target=flush_all_fallbacks, daemon=True,
                                  name="jev-exit-flush")
        worker.start()
        worker.join(ATEXIT_FLUSH_TIMEOUT_SECONDS)
    except Exception:
        try:
            flush_all_fallbacks()  # no thread available this late
        except Exception:
            pass


atexit.register(_flush_at_exit)


class _Reservation:
    """A reservation acquired lazily, just before a request is dispatched."""

    def __init__(self, policy, site: str, max_input_tokens: int):
        self.policy = policy
        self.site = site
        self.max_input_tokens = max_input_tokens
        self.token = None
        self.acquired = False
        self.reserved = False
        self.used = False
        self._lock = threading.Lock()

    def consume(self) -> bool:
        """True exactly once, and only after a successful reservation."""
        with self._lock:
            if self.reserved and not self.used:
                self.used = True
                return True
            return False

    def acquire(self, estimate: int = 0) -> None:
        """Reserve for the call's bound, or its payload if that is larger.

        Atomic: concurrent callers reserve exactly once, and nobody sees
        ``reserved`` before the token exists.
        """
        with self._lock:
            if not self.acquired:
                self.acquired = True
                self.token = self.policy._reserve(
                    self.site, max(self.max_input_tokens, int(estimate or 0)))
                self.reserved = True


class _Escaped:
    """A HarnessError carried out of a fan-out worker thread."""

    def __init__(self, exc: BaseException):
        self.exc = exc


def _refusal_reason(exc: Exception) -> str:
    """The ``fallback_reason`` for a HarnessError raised around a call."""
    return getattr(exc, "fallback_reason", None) or "preflight_refused"


def _is_power_of_two(value: int) -> bool:
    return value > 0 and value & (value - 1) == 0


def jev_cost_ceiling(max_input_tokens: int = JEV_MAX_INPUT_TOKENS) -> float:
    """Return the worst-case input-token charge for one Jev decision."""
    return jev_cost(max_input_tokens)


def aggregate_structural(
    results: Iterable[Dict[str, Any]], site: str
) -> Optional[Dict[str, Any]]:
    """Aggregate evaluated child envelopes without inventing an empty verdict."""
    values = [
        result.get("structural")
        for result in results
        if isinstance(result, dict)
        and isinstance(result.get("structural"), dict)
    ]
    if not values:
        return None
    verdicts = {value.get("verdict") for value in values}
    if "fail" in verdicts:
        verdict = "fail"
    elif "defer" in verdicts:
        verdict = "defer"
    elif verdicts == {"pass"}:
        verdict = "pass"
    else:
        verdict = "fail"
    models = {value.get("model") for value in values}
    return {
        "verdict": verdict,
        "confidence": min(float(value.get("confidence") or 0.0)
                           for value in values),
        "supported": min(float(value.get("supported") or 0.0)
                          for value in values),
        "cost": round(sum(float(value.get("cost") or 0.0)
                          for value in values), 6),
        "input_tokens": sum(int(value.get("input_tokens") or 0)
                            for value in values),
        "is_fallback": all(bool(value.get("is_fallback"))
                           for value in values),
        "fallback_reason": ";".join(sorted({str(value.get("fallback_reason"))
                                               for value in values
                                               if value.get("fallback_reason")})) or None,
        "model": next(iter(models)) if len(models) == 1 else "mixed",
        "site": site,
    }


class JevPolicy:
    """Coordinate typed Jev calls, spend, ledger evidence, and envelopes."""

    def __init__(self, settings, *, transport=None, governor=None, ledger=None,
                 evaluator=None, breaker_threshold: Optional[int] = None,
                 breaker_cooldown: Optional[float] = None,
                 clock: Optional[Callable[[], float]] = None):
        if settings is None:
            raise HarnessError("Jev policy requires settings")
        self.settings = settings
        self.transport = transport
        self.governor = governor
        self.ledger = ledger
        # (site, reason, state) -> [calls, unwritten]; (site, reason) ->
        # [calls, distinct states, unwritten once capped].
        self._fallback_counts: "OrderedDict[Tuple[str, str, str], List[int]]" = OrderedDict()
        self._fallback_site_counts: Dict[Tuple[str, str], List[int]] = {}
        self._evicted_pending: Dict[Tuple[str, str], int] = {}
        self._state_lock = threading.Lock()
        self._tl = threading.local()
        _LIVE_POLICIES.add(self)
        # The breaker board is shared process-wide on the real transport
        # (policies are built per request); explicit knobs get a private one.
        breakers = None
        if (breaker_threshold is not None or breaker_cooldown is not None
                or clock is not None):
            breakers = CircuitBreakers(
                BREAKER_FAILURE_THRESHOLD if breaker_threshold is None
                else breaker_threshold,
                BREAKER_COOLDOWN_SECONDS if breaker_cooldown is None
                else breaker_cooldown,
                clock or time.monotonic)
        explicitly_disabled = bool(getattr(settings, "jev_disabled", False))
        self.evaluator = (JevEvaluator(
            api_key=None,
            endpoint=getattr(settings, "jev_endpoint", None),
            transport=transport,
            settings=settings,
            breakers=breakers,
        ) if explicitly_disabled else evaluator or JevEvaluator(
            api_key=getattr(settings, "jev_api_key", None),
            endpoint=getattr(settings, "jev_endpoint", None),
            transport=transport,
            settings=settings,
            breakers=breakers,
        ))

    @property
    def keyed(self) -> bool:
        return bool(self.evaluator.api_key)

    def available(self, site: str,
                  max_input_tokens: int = JEV_MAX_INPUT_TOKENS) -> bool:
        """True when a keyed call at ``site`` could be dispatched right now.

        Checks key presence, that the shared governor could still afford the
        worst-case call (advisory -- the reservation in ``_preflight`` stays
        the enforcing gate), and that the site's breaker is not open. It never
        reserves or dispatches. A half-open breaker reports available without
        consuming its probe slot.
        """
        if not self.keyed:
            return False
        remaining = getattr(self.governor, "remaining", None)
        if callable(remaining):
            try:
                if float(remaining()) < jev_cost(max_input_tokens):
                    return False
            except (TypeError, ValueError, HarnessError):
                pass
        breakers = getattr(self.evaluator, "breakers", None)
        return breakers is None or breakers.would_allow(site)

    def _dedupe(self, site: str, reason: str, state_hash: str) -> Tuple[bool, int]:
        """Return ``(write_row, repeat_count)`` for one free fallback/refusal.

        A loop that keeps asking the same unanswerable question must not
        append a ledger row per call. The first occurrence of each
        (site, reason, state) is written, then only counts 2, 4, 8, ...; past
        ``FALLBACK_DISTINCT_ROW_CAP`` distinct states for one (site, reason)
        the same power-of-two rule applies to the site total. A written row's
        ``repeat_count`` is the number of calls it stands for since the last
        row, so summing rows is the true total up to the last write (see
        :meth:`flush_fallbacks` for the unwritten tail).
        """
        key = (site, reason, state_hash)
        with self._state_lock:
            entry = self._fallback_counts.pop(key, None) or [0, 0]
            self._fallback_counts[key] = entry
            while len(self._fallback_counts) > FALLBACK_TRACKED_KEYS:
                old_key, old = self._fallback_counts.popitem(last=False)
                if old[1] > 0:
                    # Never lose calls to eviction: the unwritten tally of the
                    # forgotten state folds into its (site, reason) bucket.
                    self._evicted_pending[old_key[:2]] = (
                        self._evicted_pending.get(old_key[:2], 0) + old[1])
            totals = self._fallback_site_counts.setdefault(key[:2], [0, 0, 0])
            entry[0] += 1
            if entry[0] == 1:
                totals[1] += 1  # a new distinct state for this site/reason
            totals[0] += 1
            if totals[1] <= FALLBACK_DISTINCT_ROW_CAP:
                entry[1] += 1
                write = _is_power_of_two(entry[0])
                repeat = entry[1] if write else 0
                if write:
                    entry[1] = 0
            else:
                totals[2] += 1
                write = _is_power_of_two(totals[0])
                repeat = totals[2] if write else 0
                if write:
                    totals[2] = 0
        return write, max(1, repeat)

    def _dedupe_fallback(self, result: JevEvaluationResult,
                         site: str) -> Tuple[bool, int]:
        """Billed or discarded fallbacks are never deduped: real spend always
        gets its own row."""
        if (not result.is_fallback or result.discarded
                or float(result.cost or 0.0) > 0.0 or result.input_tokens):
            return True, 1
        state_hash = result.state_hash or _digest(
            result.verdict, result.answers, result.reasons)
        return self._dedupe(
            site, result.fallback_reason or UNATTRIBUTED_FALLBACK, state_hash)

    def flush_fallbacks(self) -> int:
        """Write one row per still-unwritten deduped repeat run; return how many.

        Dedupe lags between powers of two. A caller that wants an exact total
        (end of a CLI run, a report) flushes first. Nothing is written (and
        nothing is forgotten) if the ledger's directory is gone, so a flush
        never resurrects a deleted ledger; and if an append fails the
        unwritten tallies are restored before the error propagates.
        """
        path = getattr(self.ledger, "path", None)
        if path and not os.path.isdir(os.path.dirname(path) or "."):
            return 0
        with self._state_lock:
            pending = []
            for (site, reason, _state), entry in self._fallback_counts.items():
                if entry[1] > 0:
                    pending.append((site, reason, entry[1]))
                    entry[1] = 0
            for (site, reason), totals in self._fallback_site_counts.items():
                if totals[2] > 0:
                    pending.append((site, reason, totals[2]))
                    totals[2] = 0
            for (site, reason), count in self._evicted_pending.items():
                pending.append((site, reason, count))
            self._evicted_pending.clear()
        if self.ledger is None:
            return len(pending)
        for index, (site, reason, repeat) in enumerate(pending):
            try:
                self.ledger.append(
                    "jev_eval", site=site, model=self.evaluator.model,
                    verdict="fail", supported=0.0, confidence=0.0,
                    input_tokens=0, output_tokens=0, cost=0.0,
                    is_fallback=True, fallback_reason=reason,
                    repeat_count=repeat, discarded=False, flush=True,
                    note="flush of deduped fallback repeats")
            except BaseException:
                with self._state_lock:
                    for lost_site, lost_reason, lost in pending[index:]:
                        key = (lost_site, lost_reason)
                        self._evicted_pending[key] = (
                            self._evicted_pending.get(key, 0) + lost)
                raise
        return len(pending)

    def fan_out(self, jobs: Sequence[Tuple], *,
                max_workers: int = FANOUT_MAX_WORKERS) -> List[Any]:
        """Run independent typed calls over one state concurrently.

        ``jobs`` is a sequence of ``(site, call)`` or ``(site, call,
        on_failure)``; ``call`` takes no arguments and returns whatever the
        site's ``evaluate_*`` returns. Results come back in ``jobs`` order no
        matter which finishes first. On the threaded (keyed) path each
        question fails closed on its own: a call that raises a plain
        ``Exception`` yields a local fallback (``fallback_reason=
        "fanout_exception"``, one ledger row) and does not disturb its
        siblings. A ``HarnessError`` is a run-level signal and propagates:
        every sibling finishes first, then the first one in job order is
        re-raised; ``on_failure(result, structural)``
        reshapes that pair when a site returns something other than
        ``(result, structural)``. An unkeyed policy, one job, or
        ``max_workers <= 1`` runs inline exactly like sequential calls: a bug
        propagates instead of being swallowed. Either way a reservation the
        failing call still holds is released, never leaked. Concurrency stays
        bounded by ``max_workers`` and every call still reserves/settles
        through the shared governor.
        """
        jobs = list(jobs)
        workers = min(int(max_workers), len(jobs), 8)

        def settle_failure(job, exc: BaseException):
            site = job[0]
            result = JevEvaluationResult(
                "fail", 0.0, 0.0, {},
                ["fan-out call raised {}: {}".format(type(exc).__name__, exc)],
                is_fallback=True, model=self.evaluator.model,
                fallback_reason="fanout_exception")
            try:
                structural = self._account(result, site=site)
            except Exception as ledger_exc:
                eprint("[jev] fan-out failure at {!r} was not ledgered: {}: {}"
                       .format(site, type(ledger_exc).__name__, ledger_exc))
                structural = self._structural(result, site)
            if len(job) > 2 and callable(job[2]):
                return job[2](result, structural)
            return result, structural

        def run(job, *, fail_closed: bool):
            self._tl.token = None
            try:
                return job[1]()
            except BaseException as exc:
                self._release_reservation(exc)
                if isinstance(exc, ToolCancelled):
                    raise
                if not fail_closed or not isinstance(exc, Exception):
                    raise
                if isinstance(exc, HarnessError):
                    # Ruling: a HarnessError (a hard budget "Aborting", a
                    # ledger lock) is a run-level signal, never swallowed. It
                    # is carried out of the worker and re-raised after every
                    # sibling finished and released its reservation.
                    return _Escaped(exc)
                return settle_failure(job, exc)

        if workers <= 1 or not self.keyed:
            return [run(job, fail_closed=False) for job in jobs]
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix="jev-fanout") as pool:
            futures = [submit_with_context(
                pool, run, job, fail_closed=True) for job in jobs]
            results = [future.result() for future in futures]
        for item in results:
            if isinstance(item, _Escaped):
                raise item.exc  # first HarnessError in job order
        return results

    def _release_reservation(self, error=None) -> None:
        """Release this thread's reservation if a call died holding it."""
        handle = getattr(self._tl, "token", None)
        if handle is not None:
            if isinstance(error, ToolCancelled):
                self._settle_cancelled(handle, error)
            else:
                self._release(handle)
        else:
            self._clear_dispatch_state()

    def _preflight(self, *, site: str, max_input_tokens: int,
                   eager: bool = False):
        """Reserve one bounded Jev call before its network dispatch.

        An evaluator that supports it (``lazy_reserve``) reserves at the last
        moment instead: after the cache or a single-flight wait could have
        answered for free, and immediately before a request would leave the
        machine. The returned handle is what every site passes to
        ``_account``/``_release``; it holds no token until a request is
        really about to be sent, so a free answer is never refused for budget
        and a billed request is never sent unreserved. ``eager`` reserves
        now (strict one-attempt calls that bypass the cache). Also names ``site``
        for the dispatcher's per-site circuit breaker.
        """
        if not self.keyed:
            return None
        if self.governor is None:
            raise HarnessError(
                "keyed Jev evaluation requires the shared spend governor")
        ACTIVE_SITE.set(site)
        if not eager and getattr(self.evaluator, "lazy_reserve", False):
            handle = _Reservation(self, site, max_input_tokens)
            ACTIVE_RESERVER.set(handle.acquire)
            ACTIVE_GUARD.set(handle.consume)
            self._tl.token = handle
            return handle
        token = self._reserve(site, max_input_tokens)
        self._tl.token = token
        return token

    @staticmethod
    def _sent(reservation) -> bool:
        """Was the call past its reservation (so a failure is post-dispatch)?"""
        return reservation.reserved if isinstance(reservation, _Reservation) else True

    @staticmethod
    def _token_of(reservation):
        return reservation.token if isinstance(reservation, _Reservation) else reservation

    def _release(self, reservation) -> None:
        """Settle a reservation at zero (the call was refused or failed)."""
        # ``_account`` clears the active handle immediately after settlement.
        # An exception raised later (for example while appending the ledger
        # row) must not reconcile that already-settled token a second time.
        if getattr(self._tl, "token", None) is not reservation:
            self._clear_dispatch_state()
            return
        token = self._token_of(reservation)
        if token is not None and self.governor is not None:
            try:
                self.governor.reconcile(token, 0.0)
            except HarnessError:
                pass  # already settled on the call's own path
        self._clear_dispatch_state(reservation)

    def _settle_cancelled(self, reservation, error) -> None:
        """Book known billed spend once, release liability, keep cancellation."""
        if getattr(self._tl, "token", None) is not reservation:
            # The normal accounting path already settled and cleared this
            # handle; a later cancellation (for example, ledger I/O) must not
            # reconcile the same token again.
            self._clear_dispatch_state()
            return
        if getattr(error, "cost_accounted", False):
            self._release(reservation)
            return
        cost = max(0.0, float(getattr(error, "known_cost", 0.0) or 0.0))
        token = self._token_of(reservation)
        model = getattr(self.evaluator, "model", None) or "jev"
        try:
            if token is not None and self.governor is not None:
                self.governor.reconcile(token, cost)
            elif self.governor is not None and cost > 0.0:
                self.governor.record_actual(cost, model)
        except HarnessError:
            book = getattr(self.governor, "record_overrun", None)
            if cost > 0.0 and callable(book):
                book(cost, model)
        finally:
            self._clear_dispatch_state(reservation)
        error.cost_accounted = True

    def _clear_dispatch_state(self, reservation=None) -> None:
        ACTIVE_SITE.set("")
        ACTIVE_RESERVER.set(None)
        ACTIVE_GUARD.set(None)
        if getattr(self._tl, "token", None) is reservation:
            self._tl.token = None

    def _reserve(self, site: str, max_input_tokens: int):
        if self.governor is None:
            raise HarnessError(
                "keyed Jev evaluation requires the shared spend governor")
        label = "jev:" + site
        worst = jev_cost(max_input_tokens)
        reserve = getattr(self.governor, "reserve", None)
        if callable(reserve):
            return reserve(worst, label)
        preflight = getattr(self.governor, "preflight_jev", None)
        if callable(preflight):
            preflight(max_input_tokens, label=label)
            return None
        ceiling = getattr(self.governor, "max_cost", None)
        spent = getattr(self.governor, "spent", 0.0)
        outstanding = getattr(self.governor, "outstanding", 0.0)
        if (ceiling is not None
                and float(spent) + float(outstanding) + worst > float(ceiling)):
            raise HarnessError(
                f"Jev worst-case cost ${worst:.6f} exceeds the remaining budget")
        return None

    def _no_key_reason(self) -> str:
        return ("explicit_disable"
                if getattr(self.evaluator, "explicitly_disabled", False)
                else "missing_key")

    @staticmethod
    def _fallback_reason_of(result: JevEvaluationResult, default: str) -> str:
        """Why a result is being replaced by a local answer; never ``None``."""
        if result.fallback_reason:
            return result.fallback_reason
        return "response_discarded" if result.discarded else default

    @staticmethod
    def _local_from(result: JevEvaluationResult, answers: Dict[str, Any],
                    reasons: Sequence[str], reason: str, *, billed: bool = True,
                    verdict: str = "pass") -> JevEvaluationResult:
        """A local fallback that keeps the live call's real cost and identity.

        A billed call whose answer was discarded stays settled at its real
        cost and is flagged ``discarded`` so the loss is reported, never
        hidden behind a free-looking fallback.
        """
        spent = billed and float(result.cost or 0.0) > 0.0
        return JevEvaluationResult(
            verdict, 0.0, 1.0 if verdict == "pass" else 0.0, answers,
            list(reasons), cost=result.cost if billed else 0.0,
            input_tokens=result.input_tokens if billed else 0,
            output_tokens=result.output_tokens if billed else 0,
            is_fallback=True, model=result.model,
            discarded=bool(result.discarded or spent),
            fallback_reason=reason, state_hash=result.state_hash)

    def _with_reason(self, result: JevEvaluationResult,
                     reason: Optional[str], state: Any = None
                     ) -> JevEvaluationResult:
        """Stamp a closure-built fallback with its reason and billed state.

        ``state`` is the text the site judged; it keys the fallback dedupe so
        two different inputs never collapse into one ledger row.
        """
        cost = float(result.cost or 0.0)
        return replace(
            result, fallback_reason=reason or result.fallback_reason,
            discarded=bool(result.discarded or cost > 0.0),
            state_hash=result.state_hash or (
                None if state is None else _digest(state)))

    def _book_overrun(self, cost: float, result: JevEvaluationResult) -> None:
        """Record spend the governor refused to settle (it is real money)."""
        book = getattr(self.governor, "record_overrun", None)
        if cost > 0.0 and callable(book):  # zero spend is never an overrun
            try:
                book(cost, result.model or "jev")
            except Exception as exc:
                eprint("[jev] overrun of ${:.6f} could not be booked: {}"
                       .format(cost, exc))

    def _structural(self, result: JevEvaluationResult, site: str) -> Dict[str, Any]:
        return {
            "verdict": result.verdict,
            "confidence": result.confidence,
            "supported": result.supported,
            "cost": float(result.cost or 0.0),
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "is_fallback": result.is_fallback,
            "fallback_reason": result.fallback_reason,
            "model": result.model,
            "site": site,
            # DF-JEV-3: a billed call whose answer could not be used. Carried
            # in the structural envelope so a run can total its own losses.
            "discarded": bool(result.discarded),
        }

    def _record_refusal(self, reason: str, *, site: str,
                        task_id: Optional[str] = None,
                        node_id: Optional[str] = None,
                        event_metadata: Optional[Dict[str, Any]] = None,
                        code: str = "preflight_refused"):
        result = JevEvaluationResult(
            "fail", 0.0, 0.0, {}, [reason], cost=0.0,
            input_tokens=0, output_tokens=0, is_fallback=False,
            model=self.evaluator.model,
        )
        structural = self._structural(result, site)
        write_row, repeat = self._dedupe(site, "refusal:" + code, _digest(reason))
        if self.ledger is not None and write_row:
            metadata = dict(event_metadata or {})
            if repeat > 1:
                metadata["repeat_count"] = repeat
            self.ledger.append(
                "jev_refusal", task_id=task_id, node_id=node_id, site=site,
                model=metadata.get("observed_model", result.model),
                reason=reason, reason_code=code, cost=0.0,
                input_tokens=0, is_fallback=False,
                **metadata,
            )
        return result, structural

    def _account(self, result: JevEvaluationResult, *, site: str,
                 task_id: Optional[str] = None,
                 node_id: Optional[str] = None,
                 reservation=None,
                 settlement_cost: Optional[float] = None,
                 event_metadata: Optional[Dict[str, Any]] = None,
                 preserve_event_on_settlement_error: bool = False) -> Dict[str, Any]:
        """Settle live spend and append exactly one hash-chained jev_eval."""
        if result.is_fallback and not result.fallback_reason:
            result = replace(result, fallback_reason=UNATTRIBUTED_FALLBACK)
        cost = (float(result.cost or 0.0) if settlement_cost is None
                else float(settlement_cost))
        settlement_error = None
        token = self._token_of(reservation)
        try:
            # Settle the REAL cost even when the answer was discarded or
            # replaced by a local fallback: a billed call is spend either way.
            if token is not None and self.governor is not None:
                self.governor.reconcile(token, cost)
            elif (self.governor is not None
                  and (not result.is_fallback or cost > 0.0)):
                # A governor without reservations (or a call that was
                # answered without one) still gets one actual settlement.
                self.governor.record_actual(cost, result.model or "jev")
        except Exception as exc:
            if not isinstance(exc, HarnessError):
                raise
            # The provider already billed this call: the money is spent
            # whether or not it fits the ceiling, so it is booked (and flagged
            # as an overrun) and never allowed to vanish. Settlement failure
            # is NOT a reason to throw away an answer that was paid for, so
            # nothing is raised: the result stands and is flagged below.
            self._book_overrun(cost, result)
            settlement_error = "{}: {}".format(type(exc).__name__, exc)
        self._clear_dispatch_state(reservation)
        structural = self._structural(result, site)
        # Mutated in place on purpose: callers read ``result_state`` back out
        # of the dict they passed to learn that settlement failed.
        metadata = event_metadata if event_metadata is not None else {}
        if settlement_error is not None and preserve_event_on_settlement_error:
            metadata["result_state"] = "unassessed"
            metadata["settlement_error"] = settlement_error
        elif settlement_error is not None:
            # Ruling: a parsed answer is honored after an overrun (a paid
            # "fail" still fails, a paid "pass" passes) and the overrun is
            # flagged on the single billed row, in the reasons and in the
            # envelope. Only the budget REFUSAL (nothing sent) hard-stops.
            metadata["settlement_overrun"] = True
            metadata["settlement_error"] = settlement_error
            structural["settlement_overrun"] = True
            if isinstance(result.reasons, list):
                result.reasons.append(
                    "settlement overrun: billed cost exceeded the budget "
                    "ceiling and was booked (" + settlement_error + ")")
        if result.cache_hit:
            metadata["cache_hit"] = True
            metadata["note"] = ("cache hit: identical state/questions served "
                                "locally; no request sent, cost 0")
            structural["cache_hit"] = True
        write_row, repeat = self._dedupe_fallback(result, site)
        if repeat > 1:
            metadata["repeat_count"] = repeat
        unassessed = (settlement_error is not None
                      and preserve_event_on_settlement_error)
        if self.ledger is not None and write_row:
            self.ledger.append(
                "jev_eval", task_id=task_id, node_id=node_id, site=site,
                model=metadata.get("observed_model", result.model),
                verdict="fail" if unassessed else result.verdict,
                supported=0.0 if unassessed else result.supported,
                confidence=0.0 if unassessed else result.confidence,
                input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                cost=cost, is_fallback=result.is_fallback,
                fallback_reason=result.fallback_reason,
                discarded=bool(result.discarded),
                **metadata,
            )
        return structural

    def evaluate_diff(self, diff: str, instruction: str, file_path: str,
                      *, candidate: Optional[str] = None,
                      site: str = "apply", task_id: Optional[str] = None,
                      node_id: Optional[str] = None,
                      max_input_tokens: int = JEV_MAX_INPUT_TOKENS):
        """Run local mechanics first, then the answerable semantic pack."""
        reservation = None
        try:
            def reserve_for_evaluator(state=None):
                nonlocal reservation
                reservation = self._preflight(
                    site=site, max_input_tokens=max_input_tokens)

            result = self.evaluator.verify_diff_mechanics(
                diff, instruction, file_path, candidate=candidate,
                preflight=reserve_for_evaluator,
            )
            structural = self._account(
                result, site=site, task_id=task_id, node_id=node_id,
                reservation=reservation,
            )
            reservation = None
            return result, structural
        except ToolCancelled as exc:
            self._settle_cancelled(reservation, exc)
            raise
        except HarnessError as exc:
            self._release(reservation)
            return self._record_refusal(
                str(exc), code=_refusal_reason(exc), site=site, task_id=task_id, node_id=node_id)

    def evaluate_hourglass_stage(
            self, dimension: str, state: Any, *,
            site: str = HOURGLASS_STAGE_SITE,
            task_id: Optional[str] = None,
            node_id: Optional[str] = None,
            max_input_tokens: int = JEV_MAX_INPUT_TOKENS,
            subject_supplied: bool = True,
            superseded: bool = False):
        """Judge ONE declared Hourglass stage dimension (HV-1).

        Selectable typed integrations, one owner, one contract:

        - ``dimension`` MUST be one of the operator-declared dimensions in
          ``HOURGLASS_STAGE_DIMENSIONS``; an unknown dimension raises rather
          than inventing a question set;
        - every signal is read through the official answer shapes. An
          unkeyed, transport-failed, malformed, or out-of-vocabulary answer
          leaves that signal ``None`` and the whole judgment ``native=False``
          -- a fallback is never promoted to a native signal;
        - ``restart_target`` may only ever yield a DECLARED stage; an
          out-of-vocabulary choice is reported ``None``. Deciding whether
          that recommendation is an allowed transition belongs to code
          (``validate_restart_request``), not to Jev;
        - one preflight reservation, one dispatch, one settlement, one
          metadata-only ledger ``jev_eval`` on every path.

        ``subject_supplied``/``superseded`` are the code-owned facts behind
        HV-1's "avoid redundant calls when no decision is needed". When they
        say no call is required, this returns BEFORE any preflight: no
        reservation, no dispatch, no settlement, and no ``jev_eval`` event,
        because nothing was evaluated -- a ``jev_eval`` for a call that never
        happened would be a false record. The skip is reported in the returned
        envelope instead (``result_state="not_required"``, every signal
        ``None``, ``native=False``), so it can never be mistaken for a
        judgment that passed or failed. Which facts are supplied is Lane 2's
        wiring decision (#119); this owner only reads the pack's verdict.

        ``dispatched`` in the returned envelope is the honest answer to "did
        a Jev call actually leave this machine": only the live path sets it
        true. The unkeyed and pre-dispatch-refusal paths DID require a
        judgment and could not make one -- ``judgment_required`` is true and
        ``result_state`` is ``unavailable`` -- which is a different fact from
        one that was asked and answered.

        Returns ``(result, structural)`` where ``structural`` carries the
        declared signals, the capability/pack identity, and the honest
        native/fallback state.
        """
        questions = hourglass_stage_question_pack(dimension)
        spec = HOURGLASS_STAGE_DIMENSIONS[dimension]
        signals = list(spec["signals"])

        def noul_value(answers, key):
            value = (answers or {}).get(key)
            if isinstance(value, dict) and "noul" in value:
                value = value.get("noul")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            value = float(value)
            return value if 0.0 <= value <= 1.0 else None

        def choice_value(answers, key):
            value = (answers or {}).get(key)
            if not isinstance(value, dict):
                return None
            declared = set(questions[key].get("criteria") or {})
            choice = normalize_restart_target(value.get("choice"))
            if choice is None or choice not in declared:
                return None
            confidence = value.get("confidence")
            if (isinstance(confidence, bool)
                    or not isinstance(confidence, (int, float))):
                return None
            return {"target": choice, "confidence": float(confidence),
                    "unmatched_options": list(value.get("unmatched_options") or [])}

        def read(answers):
            out: Dict[str, Any] = {}
            for key in signals:
                if key == "restart_target":
                    out[key] = choice_value(answers, key)
                else:
                    out[key] = noul_value(answers, key)
            return out

        def envelope_payload(result, values, live):
            return {**values, "pack_version": HOURGLASS_STAGE_PACK_VERSION,
                    "native": bool(live)}

        # HV-1 redundancy guard, read from the pack (never decided here).
        # Placed before the preflight so a suppressed call costs nothing.
        requirement = stage_judgment_requirement(
            dimension, subject_supplied=subject_supplied,
            superseded=superseded)
        if not requirement["required"]:
            values = {key: None for key in signals}
            skipped = JevEvaluationResult(
                "skip", 0.0, 0.0, envelope_payload(None, values, False),
                [requirement["reason"]],
                is_fallback=False, model=self.evaluator.model)
            return skipped, {
                "capability": HOURGLASS_STAGE_SITE,
                "pack_id": HOURGLASS_STAGE_PACK_ID,
                "pack_version": HOURGLASS_STAGE_PACK_VERSION,
                "dimension": dimension,
                "declared_signals": signals,
                "native": False,
                "result_state": "not_required",
                "judgment_required": False,
                "skip_reason": requirement["reason"],
                "code_owned_fact": requirement["code_owned_fact"],
                # No reservation, no dispatch, no settlement, no ledger event.
                "dispatched": False,
                "cost": 0.0,
                **{key: None for key in signals},
            }

        def finish(result, values, live, reservation=None):
            structural = self._account(
                result, site=site, task_id=task_id, node_id=node_id,
                reservation=reservation,
                event_metadata={
                    "capability": HOURGLASS_STAGE_SITE,
                    "pack_id": HOURGLASS_STAGE_PACK_ID,
                    "pack_version": HOURGLASS_STAGE_PACK_VERSION,
                    "dimension": dimension,
                    "result_state": "judged" if live else "unavailable",
                    "fallback_state": (
                        "not_used" if live else
                        "fallback" if result.is_fallback else "invalid"),
                    "native": bool(live),
                })
            structural.update({
                "capability": HOURGLASS_STAGE_SITE,
                "pack_id": HOURGLASS_STAGE_PACK_ID,
                "pack_version": HOURGLASS_STAGE_PACK_VERSION,
                "dimension": dimension,
                "declared_signals": signals,
                "native": bool(live),
                # finish() is reached only by paths that did NOT dispatch
                # (unkeyed, and a pre-dispatch HarnessError refusal). A
                # judgment WAS required and could not be made; saying
                # otherwise would make "tried and unavailable" look like
                # "asked and answered". ``result_state`` mirrors the
                # event_metadata above, so the envelope alone distinguishes
                # skip / unavailable / judged without reading the ledger.
                "judgment_required": True,
                "dispatched": False,
                "result_state": "unavailable",
                **{key: values.get(key) for key in signals},
            })
            return result, structural

        if not self.keyed:
            values = {key: None for key in signals}
            fallback = JevEvaluationResult(
                "fail", 0.0, 0.0, envelope_payload(None, values, False),
                ["unkeyed Jev cannot judge " + dimension],
                is_fallback=True, model=self.evaluator.model,
                fallback_reason=self._no_key_reason())
            return finish(fallback, values, False)

        reservation = None
        try:
            reservation = self._preflight(site=site, max_input_tokens=max_input_tokens)
            raw = self.evaluator.evaluate(state, questions)
            answers = raw.answers if isinstance(raw.answers, dict) else {}
            values = read(answers)
            live = (not raw.is_fallback
                    and all(values.get(key) is not None for key in signals))
            if not live:
                values = {key: None for key in signals}
            result = JevEvaluationResult(
                raw.verdict if raw.verdict in ("pass", "fail") else "fail",
                float(raw.confidence or 0.0),
                raw.supported, envelope_payload(raw, values, live),
                list(raw.reasons or []),
                cost=raw.cost, input_tokens=raw.input_tokens,
                output_tokens=raw.output_tokens,
                is_fallback=bool(raw.is_fallback), model=raw.model,
                discarded=bool(raw.discarded), cache_hit=raw.cache_hit)
            settled = self._account(
                result, site=site, task_id=task_id, node_id=node_id,
                reservation=reservation,
                event_metadata={
                    "capability": HOURGLASS_STAGE_SITE,
                    "pack_id": HOURGLASS_STAGE_PACK_ID,
                    "pack_version": HOURGLASS_STAGE_PACK_VERSION,
                    "dimension": dimension,
                    "result_state": "judged" if live else "unavailable",
                    "fallback_state": (
                        "not_used" if live else
                        "fallback" if raw.is_fallback else "invalid"),
                    "native": bool(live),
                })
            reservation = None
            structural = settled
            structural.update({
                "capability": HOURGLASS_STAGE_SITE,
                "pack_id": HOURGLASS_STAGE_PACK_ID,
                "pack_version": HOURGLASS_STAGE_PACK_VERSION,
                "dimension": dimension,
                "declared_signals": signals,
                "native": bool(live),
                "judgment_required": True,
                "dispatched": True,
                "result_state": "judged" if live else "unavailable",
                **{key: values.get(key) for key in signals},
            })
            return result, structural
        except ToolCancelled as exc:
            self._settle_cancelled(reservation, exc)
            raise
        except HarnessError as exc:
            self._release(reservation)
            values = {key: None for key in signals}
            fallback = JevEvaluationResult(
                "fail", 0.0, 0.0, envelope_payload(None, values, False),
                ["hourglass stage judgment unavailable: " + str(exc)],
                is_fallback=False, model=self.evaluator.model)
            return finish(fallback, values, False)

    def evaluate_answer(
            self, prompt: str, answer: str, context: str = "", *,
            site: str = "answer", task_id: Optional[str] = None,
            max_input_tokens: int = JEV_MAX_INPUT_TOKENS):
        """Assess a candidate answer and expose explicit loop signals.

        This is deliberately a narrow Jev capability rather than a second
        completion engine.  The caller owns model selection, retries, the
        confidence threshold, and any plan transition; Jev only judges the
        bounded candidate state.  A fallback or malformed response is never
        allowed to look like a sufficient/native answer.
        """
        state = {
            "request": str(prompt or "")[:1400],
            "candidate_answer": str(answer or "")[:1800],
            "retained_context": str(context or "")[:1400],
        }
        questions = answer_question_pack()

        def probability(answers, key):
            value = (answers or {}).get(key)
            if isinstance(value, dict) and "noul" in value:
                value = value.get("noul")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            value = float(value)
            if value < 0.0 or value > 1.0:
                return None
            return value

        def normalize(result):
            raw = result.answers if isinstance(result.answers, dict) else {}
            values = {key: probability(raw, key) for key in questions}
            valid = not result.is_fallback and all(v is not None for v in values.values())
            if not valid:
                reasons = list(result.reasons or [])
                if not reasons:
                    reasons = ["answer judgment unavailable or malformed"]
                fallback = JevEvaluationResult(
                    "fail", 0.0, 0.0,
                    {**{key: None for key in questions}, "pack_version": ANSWER_PACK_VERSION},
                    reasons, cost=result.cost, input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    # A malformed live response is not a heuristic fallback,
                    # but it is still not a usable native judgment. Preserve
                    # this distinction so reported provider usage is settled.
                    is_fallback=bool(result.is_fallback),
                    model=result.model,
                    discarded=bool(result.discarded),
                    fallback_reason=result.fallback_reason,
                )
                return fallback, values, False
            normalized = {
                "answer_sufficient": values["answer_sufficient"],
                "iteration_required": values["iteration_required"] >= 0.5,
                "plan_required": values["plan_required"] >= 0.5,
                "raw": raw,
                "pack_version": ANSWER_PACK_VERSION,
            }
            verdict = result.verdict if result.verdict in ("pass", "fail") else "fail"
            return JevEvaluationResult(
                verdict, float(result.confidence or 0.0),
                min(values.values()), normalized, list(result.reasons or []),
                cost=result.cost, input_tokens=result.input_tokens,
                output_tokens=result.output_tokens, is_fallback=False,
                model=result.model, cache_hit=result.cache_hit,
            ), values, True

        if not self.keyed:
            fallback = JevEvaluationResult(
                "fail", 0.0, 0.0,
                {key: None for key in questions} | {"pack_version": ANSWER_PACK_VERSION},
                ["unkeyed Jev cannot establish answer sufficiency"],
                is_fallback=True, model=self.evaluator.model,
                fallback_reason=("explicit_disable"
                                 if getattr(self.evaluator,
                                            "explicitly_disabled", False)
                                 else "missing_key"),
            )
            structural = self._account(fallback, site=site, task_id=task_id)
            structural.update({
                "capability": "answer",
                "pack_version": ANSWER_PACK_VERSION,
                "native": False,
                "answer_sufficient": None,
                "iteration_required": True,
                "plan_required": None,
            })
            return fallback, structural

        reservation = None
        dispatched = False
        try:
            reservation = self._preflight(site=site, max_input_tokens=max_input_tokens)
            dispatched = True
            raw_result = self.evaluator.evaluate(state, questions)
            result, values, live = normalize(raw_result)
            structural = self._account(
                result, site=site, task_id=task_id, reservation=reservation)
            reservation = None
            structural.update({
                "capability": "answer",
                "pack_version": ANSWER_PACK_VERSION,
                "native": bool(live),
                "answer_sufficient": values.get("answer_sufficient"),
                "iteration_required": (
                    True if values.get("iteration_required") is None
                    else values["iteration_required"] >= 0.5),
                "plan_required": (
                    None if values.get("plan_required") is None
                    else values["plan_required"] >= 0.5),
            })
            return result, structural
        except ToolCancelled as exc:
            self._settle_cancelled(reservation, exc)
            raise
        except HarnessError as exc:
            self._release(reservation)
            # Only a settlement failure happens AFTER a request was billed; a
            # refused reservation (budget) is a pre-dispatch refusal even
            # though it is raised from inside the dispatcher.
            post_dispatch = dispatched and self._sent(reservation)
            refusal, structural = self._record_refusal(
                str(exc), code=_refusal_reason(exc), site=site, task_id=task_id)
            refusal = JevEvaluationResult(
                "fail", 0.0, 0.0,
                {key: None for key in questions} | {"pack_version": ANSWER_PACK_VERSION},
                list(refusal.reasons), is_fallback=post_dispatch,
                model=refusal.model,
                fallback_reason="transport_failure" if post_dispatch else None,
            )
            structural.update({
                "capability": "answer",
                "pack_version": ANSWER_PACK_VERSION,
                "native": False,
                "answer_sufficient": None,
                "iteration_required": True,
                "plan_required": None,
                "is_fallback": refusal.is_fallback,
                "fallback_reason": refusal.fallback_reason,
            })
            return refusal, structural

    def evaluate_candidate(self, original: str, candidate: str, instruction: str,
                           file_path: str, *, site: str = "apply",
                           task_id: Optional[str] = None,
                           node_id: Optional[str] = None,
                           max_input_tokens: int = JEV_MAX_INPUT_TOKENS):
        """Evaluate a candidate diff before the verification gate can write it."""
        diff = "".join(difflib.unified_diff(
            original.splitlines(keepends=True),
            candidate.splitlines(keepends=True),
            fromfile="a/" + os.path.basename(file_path),
            tofile="b/" + os.path.basename(file_path),
        ))
        return self.evaluate_diff(
            diff, instruction, file_path, candidate=candidate, site=site,
            task_id=task_id, node_id=node_id, max_input_tokens=max_input_tokens,
        )

    def evaluate_triage(self, prompt: str, target_files=None, *,
                        site: str = "triage", task_id: Optional[str] = None):
        """Return a bounded route choice plus iteration signal for Pillar 1."""
        reservation = None
        try:
            reservation = self._preflight(site=site, max_input_tokens=JEV_MAX_INPUT_TOKENS)
            result = self.evaluator.evaluate(
                {"prompt": prompt or "", "target_files": list(target_files or [])},
                triage_question_pack())
            if result.is_fallback and "route" not in result.answers:
                lower = (prompt or "").lower()
                iterative = any(word in lower for word in
                                ("iterat", "loop", "branch", "recur", "algorithm", "architect"))
                route = "frontier" if iterative else (
                    "diff" if len(target_files or []) > 1 else "free-distill")
                result = self._local_from(
                    result, {"route": route, "requires_iteration": iterative},
                    result.reasons,
                    self._fallback_reason_of(result, "triage_unanswered"))
            structural = self._account(result, site=site, task_id=task_id,
                                       reservation=reservation)
            return result, structural
        except ToolCancelled as exc:
            self._settle_cancelled(reservation, exc)
            raise
        except HarnessError as exc:
            self._release(reservation)
            # Honest local triage is heuristic only; it never pretends to be live.
            lower = (prompt or "").lower()
            iterative = any(word in lower for word in
                            ("iterat", "loop", "branch", "recur", "algorithm", "architect"))
            route = "frontier" if iterative else ("diff" if len(target_files or []) > 1 else "free-distill")
            fallback = JevEvaluationResult(
                "pass", 0.0, 1.0,
                {"route": route, "requires_iteration": iterative}, [str(exc)],
                is_fallback=True, model=self.evaluator.model,
                fallback_reason=_refusal_reason(exc))
            return fallback, self._account(fallback, site=site, task_id=task_id)

    def evaluate_escalation_decision(
            self, failure_context: str, *, site: str = "escalation-decision",
            task_id: Optional[str] = None,
            max_input_tokens: int = JEV_MAX_INPUT_TOKENS):
        """Jev-directed escalation signals for the P2 decision pipeline
        (JEV-P2-dead-code: wire, not delete).

        Two nouls over the code-owned failure context (verify-output tail +
        attempt history -- never model output treated as state):

        - ``escalation_decision``: the ``decide_probe_verify_escalate``
          noul. Its calibrated confidence IS the confidence the dead
          function's ``confidence`` parameter was always meant to receive.
        - ``capability_budget``: the ``should_abstain`` noul (remaining
          attempt budget worth another same-tier retry?).

        Unkeyed runs dispatch no network and resolve to the evaluator's
        honest local fallback (``is_fallback=True``), so callers treat a
        fallback result as "no Jev signal available" and keep the
        status-quo walk. Live transport/contract failures refund the
        reservation and refuse honestly. Returns ``(result, structural)``.
        """
        reservation = None
        try:
            reservation = self._preflight(
                site=site, max_input_tokens=max_input_tokens)
            result = self.evaluator.evaluate(
                {"context": failure_context or ""},
                escalation_decision_pack())
            structural = self._account(
                result, site=site, task_id=task_id, reservation=reservation)
            reservation = None
            return result, structural
        except ToolCancelled as exc:
            self._settle_cancelled(reservation, exc)
            raise
        except HarnessError as exc:
            self._release(reservation)
            return self._record_refusal(
                str(exc), code=_refusal_reason(exc), site=site, task_id=task_id)

    def evaluate_decision(self, action: str, end_state: str, context: str = "",
                          *, site: str = DECISION_SITE,
                          task_id: Optional[str] = None,
                          node_id: Optional[str] = None,
                          threshold: float = DECISION_CONFIDENCE_THRESHOLD,
                          margin: float = DECISION_DISPOSITION_MARGIN,
                          max_input_tokens: int = JEV_MAX_INPUT_TOKENS):
        """Typed pre-escalation decision gate (issue #106).

        Jev advises on three split dimensions over code-owned state
        (``action``, ``end_state``, ``context`` -- never model output);
        CODE composes the verdict via ``compose_decision_verdict``:

        - ``is_destructive >= 0.5`` -> escalate, at any confidence;
        - no Jev signal (unkeyed / transport failure) or unusable answers
          -> escalate (fail closed, never proceed);
        - disposition ``escalate`` -> escalate;
        - the disposition does not lead the runner-up by ``margin`` ->
          escalate. A Choice's ``confidence`` is distribution concentration
          and does NOT gate: holding it to an absolute ``threshold`` demanded
          near-unanimity from a three-way question and escalated on almost
          every well-evidenced action. The disposition's concentration is
          still reported as ``disposition_confidence`` for telemetry;
        - ``advances_goal`` < threshold -> escalate. This one stays absolute
          because ``advances_goal`` is a Noul, whose value is a real
          yes-probability;
        - disposition ``needs_improvement`` -> revise: the caller fixes the
          action and re-gates (bounded by ``DECISION_MAX_REVISIONS``);
        - otherwise -> proceed.

        Authorization gates are not bypassed: the gate can only add a
        reason to escalate, never remove one. Returns ``(verdict,
        structural)`` where ``verdict`` is the composed dict
        (``verdict`` / ``reasons`` / the three signals / ``disposition_lead``
        / ``threshold`` / ``margin`` / ``pack_version``) and ``structural``
        is the shared Jev envelope; the ``jev_eval`` ledger event carries
        the composed verdict so the gate is measurable like every other Jev
        surface.
        """
        reservation = None
        try:
            reservation = self._preflight(
                site=site, max_input_tokens=max_input_tokens)
            result = self.evaluator.evaluate(
                {"action": action or "", "end_state": end_state or "",
                 "context": context or ""},
                decision_question_pack())
            verdict = compose_decision_verdict(
                result, threshold=threshold, margin=margin)
            structural = self._account(
                result, site=site, task_id=task_id, node_id=node_id,
                reservation=reservation,
                event_metadata={
                    "decision_verdict": verdict["verdict"],
                    "decision_disposition": verdict["disposition"],
                    "decision_confidence": verdict["disposition_confidence"],
                    "decision_lead": verdict["disposition_lead"],
                    "decision_advances_goal": verdict["advances_goal"],
                    "decision_destructive": verdict["is_destructive"],
                    "decision_threshold": threshold,
                    "decision_margin": margin,
                    "decision_pack": DECISION_PACK_VERSION,
                })
            reservation = None
            return verdict, structural
        except ToolCancelled as exc:
            self._settle_cancelled(reservation, exc)
            raise
        except HarnessError as exc:
            self._release(reservation)
            result, structural = self._record_refusal(
                str(exc), code=_refusal_reason(exc), site=site, task_id=task_id, node_id=node_id)
            verdict = compose_decision_verdict(
                result, threshold=threshold, margin=margin)
            verdict["reasons"] = (["evaluation refused: " + str(exc)]
                                  + verdict["reasons"])
            return verdict, structural

    def evaluate_plan(self, prompt: str, target_files=None, *, site: str = "waist",
                      task_id: Optional[str] = None, node_id: Optional[str] = None,
                      max_input_tokens: int = JEV_MAX_INPUT_TOKENS):
        """Evaluate the bounded plan pack and account it like every other site."""
        reservation = None
        try:
            reservation = self._preflight(
                site=site, max_input_tokens=max_input_tokens)
            result = self.evaluator.evaluate_plan_requirements(
                prompt, target_files)
            structural = self._account(
                result, site=site, task_id=task_id, node_id=node_id,
                reservation=reservation,
            )
            reservation = None
            return result, structural
        except ToolCancelled as exc:
            self._settle_cancelled(reservation, exc)
            raise
        except HarnessError as exc:
            self._release(reservation)
            return self._record_refusal(
                str(exc), code=_refusal_reason(exc), site=site, task_id=task_id, node_id=node_id)

    def evaluate_route(self, prompt: str, target_files=None, *,
                       site: str = "route", task_id: Optional[str] = None,
                       node_id: Optional[str] = None,
                       max_input_tokens: int = JEV_MAX_INPUT_TOKENS):
        """JEV-P3-route: typed route choice over the shared vocabulary.

        Keyed answers use the route pack; unkeyed/transport failure returns
        the existing heuristic with ``is_fallback=True`` (never brand ids).
        """
        reservation = None
        try:
            reservation = self._preflight(
                site=site, max_input_tokens=max_input_tokens)
            result = self.evaluator.evaluate(
                {"prompt": prompt or "", "target_files": list(target_files or [])},
                route_question_pack())
            answers = dict(result.answers or {})
            if result.is_fallback or "route" not in answers:
                route = heuristic_route(prompt, target_files)
                iterative = heuristic_requires_iteration(prompt)
                answers = {
                    "route": route,
                    "requires_iteration": iterative,
                    **{k: v for k, v in answers.items() if k not in ("route", "requires_iteration")},
                }
                result = self._local_from(
                    result, answers, result.reasons,
                    self._fallback_reason_of(result, "route_unanswered"))
            else:
                # Normalize live choice into the vocabulary (or fall back).
                normalized = normalize_route(
                    answers.get("route", {}).get("choice")
                    if isinstance(answers.get("route"), dict)
                    else answers.get("route"))
                if normalized is None:
                    route = heuristic_route(prompt, target_files)
                    answers = dict(answers)
                    answers["route"] = route
                    result = replace(
                        result, answers=answers,
                        reasons=list(result.reasons) + [
                            "live route outside vocabulary; heuristic applied"],
                        is_fallback=True, discarded=result.input_tokens > 0,
                        fallback_reason="route_out_of_vocabulary")
                else:
                    answers = dict(answers)
                    answers["route"] = normalized
                    raw_iter = answers.get("requires_iteration")
                    if isinstance(raw_iter, dict) and "noul" in raw_iter:
                        answers["requires_iteration"] = float(raw_iter["noul"]) >= 0.5
                    elif not isinstance(raw_iter, bool):
                        answers["requires_iteration"] = heuristic_requires_iteration(prompt)
                    result = replace(result, answers=answers, is_fallback=False)
            structural = self._account(
                result, site=site, task_id=task_id, node_id=node_id,
                reservation=reservation)
            return result, structural
        except ToolCancelled as exc:
            self._settle_cancelled(reservation, exc)
            raise
        except HarnessError as exc:
            self._release(reservation)
            route = heuristic_route(prompt, target_files)
            answers = {
                "route": route,
                "requires_iteration": heuristic_requires_iteration(prompt),
            }
            fallback = JevEvaluationResult(
                "pass", 0.0, 1.0, answers, [str(exc)],
                is_fallback=True, model=self.evaluator.model,
                fallback_reason=_refusal_reason(exc))
            return fallback, self._account(fallback, site=site, task_id=task_id)

    def evaluate_file_triage(self, goal: str, candidates: Sequence[str],
                             known_files: Optional[Sequence[str]] = None, *,
                             site: str = "triage-files",
                             task_id: Optional[str] = None,
                             max_files: int = 15,
                             max_input_tokens: int = JEV_MAX_INPUT_TOKENS):
        """JEV-P3-triage-files: noul relevance over orchestrator candidates.

        Every kept path is validated against ``known_files`` when supplied.
        Unkeyed path uses the keyword heuristic with honest ``is_fallback``.
        """
        listing = list(known_files) if known_files is not None else None
        scoped = validate_candidates(candidates, listing)[:max_files]
        if not scoped:
            empty = JevEvaluationResult(
                "pass", 0.0, 1.0,
                {"files": [], "is_fallback": True},
                ["no candidates to triage"], is_fallback=True,
                model=self.evaluator.model, fallback_reason="no_candidates")
            structural = self._structural(empty, site)
            structural["files"] = []
            return empty, structural
        reservation = None
        try:
            reservation = self._preflight(
                site=site, max_input_tokens=max_input_tokens)
            result = self.evaluator.evaluate(
                {"goal": goal or "", "files": list(scoped)},
                file_relevance_question_pack(scoped))
            picked: List[str] = []
            if result.is_fallback:
                picked = heuristic_file_relevance(goal, scoped, max_files=max_files)
                answers = {"files": picked, "heuristic": True}
                result = self._local_from(
                    result, answers, result.reasons,
                    self._fallback_reason_of(result, "triage_unanswered"))
            else:
                for index, path in enumerate(scoped):
                    key = f"file_{index}_relevant"
                    answer = (result.answers or {}).get(key)
                    prob = None
                    if isinstance(answer, dict):
                        prob = answer.get("noul")
                    elif isinstance(answer, (int, float)):
                        prob = answer
                    if isinstance(prob, (int, float)) and float(prob) >= 0.5:
                        picked.append(path)
                if not picked:
                    # Live pack said nothing relevant — keep honest empty list.
                    picked = []
                answers = {"files": picked}
                result = replace(result, answers=answers, is_fallback=False)
            # Defense in depth: never emit a path outside the real listing.
            result.answers["files"] = validate_candidates(picked, listing)
            structural = self._account(
                result, site=site, task_id=task_id, reservation=reservation)
            structural["files"] = list(result.answers["files"])
            return result, structural
        except ToolCancelled as exc:
            self._settle_cancelled(reservation, exc)
            raise
        except HarnessError as exc:
            self._release(reservation)
            picked = heuristic_file_relevance(goal, scoped, max_files=max_files)
            picked = validate_candidates(picked, listing)
            fallback = JevEvaluationResult(
                "pass", 0.0, 1.0,
                {"files": picked, "heuristic": True}, [str(exc)],
                is_fallback=True, model=self.evaluator.model,
                fallback_reason=_refusal_reason(exc))
            structural = self._account(fallback, site=site, task_id=task_id)
            structural["files"] = picked
            return fallback, structural

    def evaluate_claim_support(self, claims, source_context: str, *,
                               enabled: bool = False,
                               site: str = "claims",
                               task_id: Optional[str] = None,
                               max_input_tokens: int = JEV_MAX_INPUT_TOKENS):
        """JEV-P3-claims: optional lean support checks before panel judge.

        Default off (``enabled=False``). Does not own claims lint — that stays
        in :mod:`harness.claims`. This only adds advisory typed flags.
        """
        normalized = claims_from_payload(claims)
        if not enabled or not normalized:
            skipped = JevEvaluationResult(
                "pass", 0.0, 1.0,
                {"enabled": bool(enabled), "claims": [], "skipped": True},
                ["claim-support checks disabled or empty"],
                is_fallback=True, model=self.evaluator.model,
                fallback_reason="claims_skipped")
            structural = self._structural(skipped, site)
            structural["claim_flags"] = []
            structural["skipped"] = True
            return skipped, structural
        reservation = None
        try:
            reservation = self._preflight(
                site=site, max_input_tokens=max_input_tokens)
            result = self.evaluator.evaluate(
                {
                    "claims": [{"id": c["id"], "text": c["text"]}
                               for c in normalized],
                    "evidence": (source_context or "")[:4000],
                },
                claim_support_question_pack(len(normalized)))
            flags = []
            if result.is_fallback:
                # Unkeyed: no support claim either way — advisory unknown.
                flags = [{"id": c["id"], "supported": None, "fallback": True}
                         for c in normalized]
                result = JevEvaluationResult(
                    "pass", 0.0, 1.0,
                    {"claim_flags": flags, "skipped": False},
                    result.reasons, cost=result.cost,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    is_fallback=True, model=result.model,
                    discarded=result.discarded,
                    fallback_reason=self._fallback_reason_of(
                        result, "claims_unanswered"))
            else:
                for index, claim in enumerate(normalized):
                    key = f"claim_{index}_supported"
                    answer = (result.answers or {}).get(key)
                    prob = None
                    if isinstance(answer, dict):
                        prob = answer.get("noul")
                    elif isinstance(answer, (int, float)):
                        prob = answer
                    supported = None
                    if isinstance(prob, (int, float)):
                        supported = float(prob) >= 0.5
                    flags.append({"id": claim["id"], "supported": supported,
                                  "noul": prob, "fallback": False})
                result = JevEvaluationResult(
                    result.verdict, result.confidence, result.supported,
                    {"claim_flags": flags, "skipped": False}, result.reasons,
                    cost=result.cost, input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    is_fallback=False, model=result.model,
                    cache_hit=result.cache_hit)
            structural = self._account(
                result, site=site, task_id=task_id, reservation=reservation)
            structural["claim_flags"] = flags
            structural["skipped"] = False
            return result, structural
        except ToolCancelled as exc:
            self._settle_cancelled(reservation, exc)
            raise
        except HarnessError as exc:
            self._release(reservation)
            flags = [{"id": c["id"], "supported": None, "fallback": True}
                     for c in normalized]
            fallback = JevEvaluationResult(
                "pass", 0.0, 1.0,
                {"claim_flags": flags, "skipped": False}, [str(exc)],
                is_fallback=True, model=self.evaluator.model,
                fallback_reason=_refusal_reason(exc))
            structural = self._account(fallback, site=site, task_id=task_id)
            structural["claim_flags"] = flags
            structural["skipped"] = False
            return fallback, structural

    def evaluate_completion_nouls(self, goal: str, state_summary: str,
                                  named_artifacts=None, root_dir=None, *,
                                  site: str = "completion",
                                  task_id: Optional[str] = None,
                                  max_input_tokens: int = JEV_MAX_INPUT_TOKENS):
        """JEV-P3-completion: artifact/goal nouls before the generative judge.

        Missing named artifact is code-owned truth: the envelope cannot
        complete regardless of live answers.
        """
        from pathlib import Path as _Path
        if named_artifacts is None:
            artifact_facts = named_artifact_status(goal, root_dir=root_dir)
        else:
            base = _Path(root_dir) if root_dir is not None else _Path.cwd()
            artifact_facts = []
            for item in named_artifacts:
                if isinstance(item, dict):
                    path = str(item.get("path") or "").replace("\\", "/")
                    present = bool(item.get("present"))
                    if path and not item.get("present") and "present" not in item:
                        present = (base / path).is_file()
                    artifact_facts.append({
                        "path": path,
                        "present": present,
                        "lines": item.get("lines"),
                    })
                else:
                    path = str(item).replace("\\", "/").strip("`'\" .")
                    present = bool(path) and (base / path).is_file()
                    artifact_facts.append({
                        "path": path, "present": present, "lines": None,
                    })
        missing = [item["path"] for item in artifact_facts
                   if item.get("path") and not item.get("present")]
        artifacts_payload = artifact_facts
        if missing:
            # Code-owned refuse: no spend required to know completion is false.
            answers = {
                "named_artifacts_present": 0.0,
                "goal_achieved": 0.0,
                "missing_artifacts": missing,
                "artifacts": artifacts_payload,
            }
            result = JevEvaluationResult(
                "fail", 0.0, 0.0, answers,
                [f"named artifact missing: {p}" for p in missing],
                is_fallback=False, model=self.evaluator.model)
            structural = self._account(
                result, site=site, task_id=task_id, reservation=None)
            structural["cannot_complete"] = True
            structural["missing_artifacts"] = missing
            return result, structural
        reservation = None
        dispatched = False
        try:
            reservation = self._preflight(
                site=site, max_input_tokens=max_input_tokens)
            dispatched = True
            result = self.evaluator.evaluate(
                {
                    "goal": goal or "",
                    "execution_state": (state_summary or "")[:4000],
                    "named_artifacts": artifacts_payload,
                },
                completion_question_pack())
            answers = dict(result.answers or {})
            cannot = False
            if result.is_fallback:
                # Unkeyed: artifacts exist; generative judge remains the seat.
                answers.setdefault("named_artifacts_present", 1.0)
                answers.setdefault("goal_achieved", None)
                answers["artifacts"] = artifacts_payload
                result = JevEvaluationResult(
                    "pass", 0.0, 1.0, answers, result.reasons,
                    cost=result.cost, input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    is_fallback=True, model=result.model,
                    fallback_reason=result.fallback_reason)
            else:
                present = answers.get("named_artifacts_present")
                achieved = answers.get("goal_achieved")
                present_p = float(present.get("noul", 0.0)) if isinstance(present, dict) else float(present or 0.0)
                achieved_p = float(achieved.get("noul", 0.0)) if isinstance(achieved, dict) else float(achieved or 0.0)
                answers = dict(answers)
                answers["named_artifacts_present"] = present_p
                answers["goal_achieved"] = achieved_p
                answers["artifacts"] = artifacts_payload
                verdict = "pass" if present_p >= 0.5 and achieved_p >= 0.5 else "fail"
                result = JevEvaluationResult(
                    verdict, result.confidence,
                    min(present_p, achieved_p),
                    answers, result.reasons,
                    cost=result.cost, input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    is_fallback=False, model=result.model,
                    cache_hit=result.cache_hit)
                cannot = present_p < 0.5 or achieved_p < 0.5
            structural = self._account(
                result, site=site, task_id=task_id, reservation=reservation)
            structural["cannot_complete"] = bool(cannot)
            structural["missing_artifacts"] = missing
            return result, structural
        except ToolCancelled as exc:
            self._settle_cancelled(reservation, exc)
            raise
        except HarnessError as exc:
            self._release(reservation)
            post_dispatch = dispatched and self._sent(reservation)
            answers = {
                "named_artifacts_present": 1.0,
                "goal_achieved": 0.5,
                "artifacts": artifacts_payload,
            }
            fallback = JevEvaluationResult(
                "pass", 0.0, 0.5, answers, [str(exc)],
                is_fallback=post_dispatch, model=self.evaluator.model,
                fallback_reason="transport_failure" if post_dispatch else None)
            if post_dispatch:
                structural = self._account(
                    fallback, site=site, task_id=task_id)
            else:
                # Refused before dispatch: leave (deduped) ledger evidence.
                self._record_refusal(
                    str(exc), code=_refusal_reason(exc), site=site,
                    task_id=task_id)
                structural = self._structural(fallback, site)
            structural["cannot_complete"] = True
            structural["missing_artifacts"] = missing
            structural["reason"] = str(exc)
            return fallback, structural

    @staticmethod
    def _scope_noul(answers: Dict[str, Any], key: str) -> Optional[float]:
        val = (answers or {}).get(key)
        if isinstance(val, dict) and "noul" in val:
            try:
                return float(val["noul"])
            except (TypeError, ValueError):
                return None
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            return None
        return float(val)

    @staticmethod
    def _scope_score(answers: Dict[str, Any], key: str) -> Optional[float]:
        val = (answers or {}).get(key)
        if isinstance(val, dict) and "score" in val:
            try:
                return float(val["score"])
            except (TypeError, ValueError):
                return None
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            return None
        return float(val)

    @staticmethod
    def _scope_choice(answers: Dict[str, Any], key: str) -> Optional[str]:
        val = (answers or {}).get(key)
        if isinstance(val, dict):
            return normalize_complexity_class(val.get("choice"))
        return normalize_complexity_class(val)

    @staticmethod
    def _scope_determination(
        *,
        verifier_holds: bool,
        success_met: Optional[float],
        coverage: Optional[float],
        claims: Optional[float],
        needs_human: Optional[float],
        complexity: Optional[str],
        is_fallback: bool,
        scope_declared: bool,
        reasons: List[str],
    ) -> Dict[str, Any]:
        """HUL-C determination: complete only when every hold is true.

        Unkeyed / fallback evaluations may NEVER alone mark complete.
        """
        success_ok = (
            success_met is not None
            and success_met >= SCOPE_NOUL_HOLD
            and not is_fallback
        )
        claims_ok = (
            claims is not None
            and claims >= SCOPE_NOUL_HOLD
            and not is_fallback
        )
        coverage_ok = (
            coverage is not None
            and coverage >= SCOPE_COVERAGE_HOLD
            and not is_fallback
        )
        human_clear = (
            needs_human is None
            or needs_human < SCOPE_NOUL_HOLD
        )
        scope_holds = bool(
            scope_declared and coverage_ok and claims_ok and human_clear
        )
        complete = bool(
            verifier_holds
            and success_ok
            and scope_holds
            and not is_fallback
        )
        out_reasons = list(reasons)
        if not scope_declared:
            out_reasons.append("missing scope.in_scope — cannot complete")
        if is_fallback:
            out_reasons.append(
                "unkeyed fallback cannot alone mark mission complete")
        if verifier_holds is False:
            out_reasons.append("verifier does not hold")
        if success_met is not None and success_met < SCOPE_NOUL_HOLD:
            out_reasons.append("success_definition_met is low")
        if claims is not None and claims < SCOPE_NOUL_HOLD:
            out_reasons.append("claims_supported is low")
        if coverage is not None and coverage < SCOPE_COVERAGE_HOLD:
            out_reasons.append("scope_coverage is low")
        if needs_human is not None and needs_human >= SCOPE_NOUL_HOLD:
            out_reasons.append("needs_human is true")
        return {
            "complete": complete,
            "verifier_holds": bool(verifier_holds),
            "success_definition_met": success_ok,
            "scope_holds": scope_holds,
            "scope_coverage": coverage,
            "claims_supported": claims,
            "needs_human": (
                None if needs_human is None
                else bool(needs_human >= SCOPE_NOUL_HOLD)),
            "complexity_class": complexity,
            "is_fallback": bool(is_fallback),
            "site": HUL_SCOPE_SITE,
            "reasons": out_reasons,
        }

    @staticmethod
    def _scope_text(state: Any) -> str:
        if isinstance(state, str):
            return state
        if isinstance(state, dict):
            for key in ("evidence_summary", "state_summary", "request",
                        "text", "prompt"):
                value = state.get(key)
                if isinstance(value, str) and value.strip():
                    return value
            return ""
        return "" if state is None else str(state)

    @staticmethod
    def _scope_state_facts(state: Any) -> Dict[str, Any]:
        if not isinstance(state, dict):
            return {
                "mission_id": None,
                "request": "",
                "success_definition": "",
                "scope": {"in_scope": [], "out_of_scope": []},
                "verifier_holds": False,
                "evidence_summary": JevPolicy._scope_text(state),
            }
        scope = state.get("scope")
        if not isinstance(scope, dict):
            scope = {"in_scope": [], "out_of_scope": []}
        in_scope = scope.get("in_scope")
        out_scope = scope.get("out_of_scope")
        return {
            "mission_id": state.get("mission_id"),
            "request": str(state.get("request") or ""),
            "success_definition": str(state.get("success_definition") or ""),
            "scope": {
                "in_scope": list(in_scope) if isinstance(in_scope, (list, tuple)) else [],
                "out_of_scope": list(out_scope) if isinstance(out_scope, (list, tuple)) else [],
            },
            "verifier_holds": bool(state.get("verifier_holds", False)),
            "evidence_summary": JevPolicy._scope_text(state),
        }

    def evaluate_scope(self, mission_state, *, site: str = HUL_SCOPE_SITE,
                       task_id: Optional[str] = None,
                       node_id: Optional[str] = None,
                       max_input_tokens: int = JEV_MAX_INPUT_TOKENS):
        """HUL-C: mission scope gate via the ONE policy owner.

        Determination contract:
        - complete only if verifier_holds AND success_definition_met AND
          scope hold (declared in_scope + coverage + claims + no human block)
        - unkeyed / fallback may NOT alone mark complete
        - missing scope or low success → complete false
        - ledger gets one ``jev_eval``; mission packs store the same via
          ``mission_record.append_jev_eval`` (no second Jev client)

        Returns ``(result, structural, determination)``.
        """
        facts = self._scope_state_facts(mission_state)
        scope_declared = scope_in_scope_holds(facts["scope"])
        payload = {
            "mission_id": facts["mission_id"],
            "request": facts["request"],
            "success_definition": facts["success_definition"],
            "scope": facts["scope"],
            "evidence_summary": facts["evidence_summary"][:4000],
            "verifier_holds": facts["verifier_holds"],
        }
        questions = hul_scope_question_pack()

        if not self.keyed:
            answers = {
                "scope_coverage": {"type": "score", "score": 0.0},
                "success_definition_met": {"type": "noul", "noul": 0.0},
                "claims_supported": {"type": "noul", "noul": 0.0},
                "needs_human": {"type": "noul", "noul": 0.0},
                "complexity_class": {"type": "choice", "choice": None},
            }
            determination = self._scope_determination(
                verifier_holds=facts["verifier_holds"],
                success_met=0.0,
                coverage=0.0,
                claims=0.0,
                needs_human=0.0,
                complexity=None,
                is_fallback=True,
                scope_declared=scope_declared,
                reasons=["unkeyed: scope pack not evaluated live"],
            )
            result = JevEvaluationResult(
                "fail" if not determination["complete"] else "pass",
                0.0, 0.0, answers, determination["reasons"],
                is_fallback=True, model=self.evaluator.model,
                fallback_reason=("explicit_disable"
                                 if getattr(self.evaluator,
                                            "explicitly_disabled", False)
                                 else "missing_key"))
            structural = self._account(
                result, site=site, task_id=task_id, node_id=node_id)
            structural["determination"] = determination
            return result, structural, determination

        reservation = None
        try:
            reservation = self._preflight(
                site=site, max_input_tokens=max_input_tokens)
            result = self.evaluator.evaluate(payload, questions)
            answers = dict(result.answers or {}) if isinstance(result.answers, dict) else {}
            is_fallback = bool(result.is_fallback)
            coverage = self._scope_score(answers, "scope_coverage")
            success_met = self._scope_noul(answers, "success_definition_met")
            claims = self._scope_noul(answers, "claims_supported")
            needs_human = self._scope_noul(answers, "needs_human")
            complexity = self._scope_choice(answers, "complexity_class")
            if is_fallback:
                # Transport/fallback: never allow complete from unkeyed path.
                success_met = min(success_met, 0.0) if success_met is not None else 0.0
                coverage = min(coverage, 0.0) if coverage is not None else 0.0
                claims = min(claims, 0.0) if claims is not None else 0.0
            determination = self._scope_determination(
                verifier_holds=facts["verifier_holds"],
                success_met=success_met,
                coverage=coverage,
                claims=claims,
                needs_human=needs_human,
                complexity=complexity,
                is_fallback=is_fallback,
                scope_declared=scope_declared,
                reasons=list(result.reasons or []),
            )
            normalized = dict(answers)
            normalized["scope_coverage"] = coverage
            normalized["success_definition_met"] = success_met
            normalized["claims_supported"] = claims
            normalized["needs_human"] = determination["needs_human"]
            normalized["complexity_class"] = complexity
            normalized["determination"] = determination
            verdict = "pass" if determination["complete"] else "fail"
            supported = min(
                [x for x in (success_met, claims, coverage) if x is not None]
                or [0.0])
            result = JevEvaluationResult(
                verdict,
                float(result.confidence or 0.0),
                float(supported),
                normalized,
                determination["reasons"] or list(result.reasons or []),
                cost=result.cost,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                is_fallback=is_fallback,
                model=result.model,
                discarded=result.discarded,
                cache_hit=result.cache_hit,
                fallback_reason=result.fallback_reason)
            structural = self._account(
                result, site=site, task_id=task_id, node_id=node_id,
                reservation=reservation)
            structural["determination"] = determination
            return result, structural, determination
        except ToolCancelled as exc:
            self._settle_cancelled(reservation, exc)
            raise
        except HarnessError as exc:
            self._release(reservation)
            determination = self._scope_determination(
                verifier_holds=facts["verifier_holds"],
                success_met=0.0,
                coverage=0.0,
                claims=0.0,
                needs_human=0.0,
                complexity=None,
                is_fallback=True,
                scope_declared=scope_declared,
                reasons=[str(exc), "scope evaluation failed closed"],
            )
            fallback = JevEvaluationResult(
                "fail", 0.0, 0.0, {}, determination["reasons"],
                is_fallback=True, model=self.evaluator.model,
                fallback_reason=_refusal_reason(exc))
            structural = self._account(fallback, site=site, task_id=task_id)
            structural["determination"] = determination
            structural["reason"] = str(exc)
            return fallback, structural, determination

    @staticmethod
    def _issue_sort_text(state: Any) -> str:
        if isinstance(state, str):
            return state
        if isinstance(state, dict):
            for key in ("issue", "text", "prompt", "note", "reason"):
                value = state.get(key)
                if isinstance(value, str):
                    return value
            return ""
        return "" if state is None else str(state)

    @staticmethod
    def _issue_sort_empty_combo(*, is_fallback: bool = True,
                                structural: Optional[Dict[str, Any]] = None,
                                evidence=None,
                                pack_id: Optional[str] = None) -> Dict[str, Any]:
        return {
            "bucket": None,
            "path_id": None,
            "confidence": 0.0,
            "evidence_refs": list(evidence or []),
            "suggested_next_action": None,
            "is_fallback": bool(is_fallback),
            "pack_id": pack_id,
            "kind": None,
            "attention": None,
            "structural": structural,
        }

    @staticmethod
    def _issue_sort_combo(bucket_id, pack_doc, *, confidence, evidence,
                          is_fallback, structural) -> Dict[str, Any]:
        """Bind the combo to pack fields only — never invent path/action."""
        pack_id = (pack_doc or {}).get("id")
        buckets = (pack_doc or {}).get("buckets") or {}
        entry = buckets.get(bucket_id) if bucket_id else None
        if not entry:
            combo = JevPolicy._issue_sort_empty_combo(
                is_fallback=True, structural=structural, evidence=evidence,
                pack_id=pack_id)
            return combo
        return {
            "bucket": bucket_id,
            "path_id": entry["path_id"],
            "confidence": float(confidence or 0.0),
            "evidence_refs": list(evidence or []),
            "suggested_next_action": entry.get("suggested_next_action"),
            "is_fallback": bool(is_fallback),
            "pack_id": pack_id,
            "kind": entry["kind"],
            "attention": entry.get("attention"),
            "structural": structural,
        }

    def evaluate_issue_sort(self, state, pack, *, site: str = "issue_sort",
                            task_id: Optional[str] = None,
                            node_id: Optional[str] = None,
                            max_input_tokens: int = JEV_MAX_INPUT_TOKENS):
        """Sort an issue into an operator-declared bucket (JEV-P5 owner).

        0-hallucination contract:
        - choice criteria = operator bucket labels only (via jev_packs);
          ``_parse_answer`` already requires choice ∈ criteria.
        - unkeyed / transport fail / out-of-pack → ``is_fallback=true``;
          keyword match only against pack keywords.
        - no match → ``bucket=None``, ``path_id=None``.
        - ``suggested_next_action`` always equals ``pack[bucket]`` when set.
        ONE owner: this method + ``structural.site=issue_sort`` + one
        ledger ``jev_eval`` per call.
        Returns ``(result, structural, combo)``.
        """
        issue_text = self._issue_sort_text(state)

        try:
            pack_doc = validate_operator_pack(pack)
        except ValueError as exc:
            result = JevEvaluationResult(
                "fail", 0.0, 0.0, {}, [str(exc)], cost=0.0,
                input_tokens=0, output_tokens=0, is_fallback=True,
                model=self.evaluator.model, fallback_reason="invalid_pack")
            structural = self._account(
                result, site=site, task_id=task_id, node_id=node_id)
            combo = self._issue_sort_empty_combo(
                is_fallback=True, structural=structural,
                evidence=result.reasons)
            return result, structural, combo

        def local_result(bucket_id, score, evidence, reasons, *, model=None):
            evidence_refs = list(reasons or []) + list(evidence or [])
            if bucket_id:
                return JevEvaluationResult(
                    "pass", 0.0, 1.0,
                    {"bucket": bucket_id, "score": score},
                    (reasons or ["keyword match against operator pack"]),
                    is_fallback=True,
                    model=model or self.evaluator.model), evidence_refs
            return JevEvaluationResult(
                "fail", 0.0, 0.0, {"bucket": None},
                (list(reasons or []) + ["no declared pack keyword match"]),
                is_fallback=True,
                model=model or self.evaluator.model), evidence_refs

        def keyword_sort(reasons, *, model=None, cost=0.0, input_tokens=0,
                         output_tokens=0, reservation=None, reason=None):
            bucket_id, score, evidence = match_keywords(issue_text, pack_doc)
            result, evidence_refs = local_result(
                bucket_id, score, evidence, reasons, model=model)
            if cost or input_tokens or output_tokens:
                result = JevEvaluationResult(
                    result.verdict, result.confidence, result.supported,
                    result.answers, result.reasons, cost=cost,
                    input_tokens=input_tokens, output_tokens=output_tokens,
                    is_fallback=True, model=result.model)
            result = self._with_reason(result, reason, issue_text)
            # ONE ledger jev_eval per evaluate_issue_sort call.
            structural = self._account(
                result, site=site, task_id=task_id, node_id=node_id,
                reservation=reservation)
            combo = self._issue_sort_combo(
                bucket_id, pack_doc, confidence=0.0, evidence=evidence_refs,
                is_fallback=True, structural=structural)
            return result, structural, combo

        if not self.keyed:
            # Unkeyed: skip live; code-owned keyword match only.
            return keyword_sort(["unkeyed: keyword match only"],
                                reason=self._no_key_reason())

        reservation = None
        try:
            questions = issue_sort_question_pack(pack_doc)
            reservation = self._preflight(
                site=site, max_input_tokens=max_input_tokens)
            result = self.evaluator.evaluate(
                {"issue": issue_text, "pack_id": pack_doc["id"]},
                questions)
        except ToolCancelled as exc:
            self._settle_cancelled(reservation, exc)
            raise
        except HarnessError as exc:
            return keyword_sort([str(exc)], reservation=reservation,
                                reason=_refusal_reason(exc))

        answers = result.answers if isinstance(result.answers, dict) else {}
        bucket_ans = answers.get("bucket")
        choice = bucket_ans.get("choice") if isinstance(bucket_ans, dict) else None
        pack_ids = set(pack_doc["buckets"])
        declared = (not result.is_fallback
                    and isinstance(choice, str)
                    and choice in pack_ids)

        if declared and result.verdict == "pass":
            structural = self._account(
                result, site=site, task_id=task_id, node_id=node_id,
                reservation=reservation)
            combo = self._issue_sort_combo(
                choice, pack_doc,
                confidence=result.confidence,
                evidence=list(result.reasons) + [f"choice:{choice}"],
                is_fallback=False, structural=structural)
            return result, structural, combo

        # Out-of-pack / transport fail / fallback / unparseable choice:
        # never invent a bucket. Keyword match against pack only.
        reasons = list(result.reasons) if result.reasons else []
        if isinstance(choice, str) and choice not in pack_ids:
            reasons = reasons + [
                f"out-of-pack choice refused: {choice!r}"]
        return keyword_sort(
            reasons, model=result.model,
            cost=float(result.cost or 0.0),
            input_tokens=int(result.input_tokens or 0),
            output_tokens=int(result.output_tokens or 0),
            reservation=reservation,
            reason=self._fallback_reason_of(result, "out_of_pack"))

    @staticmethod
    def _log_item_text(state: Any) -> str:
        """Text extraction for log items (accepts the ``item`` key)."""
        if isinstance(state, str):
            return state
        if isinstance(state, dict):
            for key in ("item", "text", "issue", "reason", "note"):
                value = state.get(key)
                if isinstance(value, str):
                    return value
            return ""
        return "" if state is None else str(state)

    @staticmethod
    def _log_judgment(bucket_id, pack_doc, *, score_level=None,
                      score_value=None, score_confidence=0.0,
                      evidence=None, is_fallback, structural):
        """Bind the log judgment to pack fields only — never invent."""
        pack_doc = pack_doc or {}
        entry = (pack_doc.get("buckets") or {}).get(bucket_id) \
            if isinstance(bucket_id, str) else None
        if entry is None:
            bucket_id = None
        return {
            "bucket": bucket_id,
            "path_id": entry.get("path_id") if entry else None,
            "kind": entry.get("kind") if entry else None,
            "attention": entry.get("attention") if entry else None,
            "suggested_next_action": entry.get("suggested_next_action") if entry else None,
            "score": {
                "id": (pack_doc.get("score") or {}).get("id"),
                "level": score_level,
                "value": score_value,
                "confidence": float(score_confidence or 0.0),
            },
            "evidence_refs": list(evidence or []),
            "is_fallback": bool(is_fallback),
            "pack_id": pack_doc.get("id"),
            "structural": structural,
        }

    def evaluate_log_item(self, state, pack, *, site=LOG_FACTOR_SITE,
                          task_id: Optional[str] = None,
                          node_id: Optional[str] = None,
                          max_input_tokens: int = JEV_MAX_INPUT_TOKENS):
        """Jev audit of one log item against an operator log pack (JEV-LOG).

        0-hallucination contract (same class as ``evaluate_issue_sort``):
        - ``bucket`` ∈ operator pack keys, else ``None`` — out-of-pack,
          unparseable, transport-failed, or unkeyed runs fall back to the
          code-owned keyword matcher against pack keywords only.
        - ``score.level`` ∈ operator score levels, else ``None`` — the live
          level is the declared level string with the highest probability.
        - ``path_id`` / ``suggested_next_action`` always come from the pack.
        - ONE ledger ``jev_eval`` per call; ``structural.site=log_factor``.
        Returns ``(result, structural, judgment)``.
        """
        text = self._log_item_text(state)
        try:
            pack_doc = validate_log_pack(pack)
        except ValueError as exc:
            result = JevEvaluationResult(
                "fail", 0.0, 0.0, {}, [str(exc)], cost=0.0,
                input_tokens=0, output_tokens=0, is_fallback=True,
                model=self.evaluator.model, fallback_reason="invalid_pack")
            structural = self._account(
                result, site=site, task_id=task_id, node_id=node_id)
            judgment = self._log_judgment(
                None, None, is_fallback=True, structural=structural,
                evidence=result.reasons)
            return result, structural, judgment

        def keyword_judgment(reasons, *, model=None, cost=0.0,
                             input_tokens=0, output_tokens=0,
                             reservation=None, reason=None):
            bucket_id, hits, evidence = match_keywords(text, pack_doc)
            evidence_refs = list(reasons or []) + list(evidence or [])
            if bucket_id:
                result = JevEvaluationResult(
                    "pass", 0.0, 1.0,
                    {"bucket": bucket_id},
                    (reasons or ["keyword match against operator pack"]),
                    cost=cost, input_tokens=input_tokens,
                    output_tokens=output_tokens, is_fallback=True,
                    model=model or self.evaluator.model)
            else:
                result = JevEvaluationResult(
                    "fail", 0.0, 0.0, {"bucket": None},
                    (list(reasons or []) + ["no declared pack keyword match"]),
                    cost=cost, input_tokens=input_tokens,
                    output_tokens=output_tokens, is_fallback=True,
                    model=model or self.evaluator.model)
            result = self._with_reason(result, reason, text)
            # ONE ledger jev_eval per evaluate_log_item call.
            structural = self._account(
                result, site=site, task_id=task_id, node_id=node_id,
                reservation=reservation)
            judgment = self._log_judgment(
                bucket_id, pack_doc, is_fallback=True,
                structural=structural, evidence=evidence_refs)
            return result, structural, judgment

        if not self.keyed:
            # Unkeyed: skip live; code-owned keyword match only.
            return keyword_judgment(["unkeyed: keyword match only"],
                                    reason=self._no_key_reason())

        reservation = None
        try:
            questions = log_factor_question_pack(pack_doc)
            reservation = self._preflight(
                site=site, max_input_tokens=max_input_tokens)
            result = self.evaluator.evaluate(
                {"item": text, "pack_id": pack_doc["id"]}, questions)
        except ToolCancelled as exc:
            self._settle_cancelled(reservation, exc)
            raise
        except HarnessError as exc:
            return keyword_judgment([str(exc)], reservation=reservation,
                                    reason=_refusal_reason(exc))

        answers = result.answers if isinstance(result.answers, dict) else {}
        score_id = pack_doc["score"]["id"]
        bucket_ans = answers.get("bucket")
        choice = bucket_ans.get("choice") if isinstance(bucket_ans, dict) else None
        pack_ids = set(pack_doc["buckets"])
        declared = (not result.is_fallback
                    and isinstance(choice, str)
                    and choice in pack_ids)

        # Live score: the DECLARED level string (via the official legend:
        # anchors -> criteria strings) with the highest probability, else None.
        score_level = score_value = None
        score_conf = 0.0
        score_ans = answers.get(score_id)
        if not result.is_fallback and isinstance(score_ans, dict):
            probs = score_ans.get("probabilities")
            legend = score_ans.get("legend")
            pack_levels = set(pack_doc["score"]["levels"])
            if isinstance(probs, dict) and isinstance(legend, dict):
                for anchor, level in legend.items():
                    if not isinstance(level, str) or level not in pack_levels:
                        continue
                    prob = probs.get(str(anchor))
                    if isinstance(prob, (int, float)) and (
                            score_value is None or float(prob) > score_value):
                        score_level, score_value = level, float(prob)
            raw_conf = score_ans.get("confidence")
            if isinstance(raw_conf, (int, float)):
                score_conf = float(raw_conf)

        if declared and result.verdict == "pass":
            structural = self._account(
                result, site=site, task_id=task_id, node_id=node_id,
                reservation=reservation)
            judgment = self._log_judgment(
                choice, pack_doc, score_level=score_level,
                score_value=score_value, score_confidence=score_conf,
                evidence=list(result.reasons) + [f"choice:{choice}"],
                is_fallback=False, structural=structural)
            return result, structural, judgment

        # Out-of-pack / transport fail / fallback / unparseable: never invent
        # a bucket or a score level. Keyword match against the pack only.
        reasons = list(result.reasons) if result.reasons else []
        if isinstance(choice, str) and choice not in pack_ids:
            reasons = reasons + [f"out-of-pack choice refused: {choice!r}"]
        return keyword_judgment(
            reasons, model=result.model,
            cost=float(result.cost or 0.0),
            input_tokens=int(result.input_tokens or 0),
            output_tokens=int(result.output_tokens or 0),
            reservation=reservation,
            reason=self._fallback_reason_of(result, "out_of_pack"))

    @staticmethod
    def _repo_element_text(state: Any) -> str:
        """Bounded text view of one element state for keyword fallback."""
        if isinstance(state, str):
            return state[:1200]
        if not isinstance(state, dict):
            return str(state)[:1200]
        parts = [str(state.get("path") or ""), str(state.get("kind") or ""),
                 str(state.get("summary") or ""),
                 " ".join(str(s) for s in (state.get("symbols") or [])[:18]),
                 " ".join(str(h) for h in (state.get("headings") or [])[:12])]
        if state.get("element_kind") == "symbol":
            parts.append(str(state.get("symbol") or ""))
            parts.append(str(state.get("module_summary") or ""))
        return " ".join(p for p in parts if p)[:1200]

    @staticmethod
    def _repo_judgment(pack_doc, axes, level, value, confidence, nouls, *,
                       is_fallback: bool, evidence,
                       axis_confidence=None) -> Dict[str, Any]:
        """The stable JEV-P6 judgment shape (declared ids or None only).

        ``axis_confidence`` carries each choice's distribution confidence
        (TypeSafe-derived) so consumers can tell a settled axis from a
        near-tie that the seat may flip run-to-run (measured non-zero on
        identical input; see the 2026-09-22 determinism probe).
        """
        pack_doc = pack_doc if isinstance(pack_doc, dict) else {}
        score = pack_doc.get("score") if isinstance(pack_doc.get("score"), dict) else {}
        return {
            "pack_id": pack_doc.get("id"),
            "axes": dict(axes or {}),
            "axis_confidence": {
                axis: (float(value)
                       if isinstance(value, (int, float))
                       and not isinstance(value, bool) else None)
                for axis, value in (axis_confidence or {}).items()},
            "attention": {"id": score.get("id"), "level": level,
                          "value": value, "confidence": confidence},
            "nouls": dict(nouls or {}),
            "is_fallback": bool(is_fallback),
            "evidence": list(evidence or []),
        }

    def evaluate_repo_summary(self, state, pack, *, site=REPO_SUMMARY_SITE,
                              task_id: Optional[str] = None,
                              node_id: Optional[str] = None,
                              max_input_tokens: int = JEV_MAX_INPUT_TOKENS):
        """Judge ONE repo element against the operator repo pack (JEV-P6).

        0-hallucination contract (same class as ``evaluate_log_item``):
        - every axis value \u2208 operator criteria keys, else ``None`` -- an
          out-of-vocabulary choice is reported, never replaced by a guess;
        - ``attention.level`` \u2208 operator score levels (via the official
          legend), else ``None``; ``attention.value`` is the winning
          probability;
        - nouls ride as raw probabilities -- a low noul is an answer, not a
          parse failure, so verdict thresholds never discard classifications;
        - unkeyed / transport-fail / shape-invalid paths fall back to the
          code-owned keyword matcher (``keywords`` in the pack only);
        - ONE ledger ``jev_eval`` per call; ``structural.site=repo_summary``.
        Returns ``(result, structural, judgment)``.
        """
        text = self._repo_element_text(state)
        try:
            pack_doc = validate_repo_summary_pack(pack)
        except ValueError as exc:
            result = JevEvaluationResult(
                "fail", 0.0, 0.0, {}, [str(exc)], cost=0.0,
                input_tokens=0, output_tokens=0, is_fallback=True,
                model=self.evaluator.model, fallback_reason="invalid_pack")
            structural = self._account(
                result, site=site, task_id=task_id, node_id=node_id)
            judgment = self._repo_judgment(
                None, None, None, None, None, None,
                is_fallback=True, evidence=result.reasons)
            return result, structural, judgment

        axis_ids = {axis: set(spec["criteria"])
                    for axis, spec in pack_doc["axes"].items()}
        level_ids = set(pack_doc["score"]["levels"])

        def fallback_judgment(reasons, *, model=None, cost=0.0,
                              input_tokens=0, output_tokens=0,
                              reservation=None, discarded=False,
                              reason=None):
            axes = heuristic_repo_axes(text, pack_doc)
            matched = any(v for v in axes.values())
            evidence = list(reasons or []) + [
                f"{axis}:{value}" for axis, value in sorted(axes.items())
                if value is not None]
            result = JevEvaluationResult(
                "pass" if matched else "fail", 0.0, 1.0 if matched else 0.0,
                {"axes": axes}, evidence,
                cost=cost, input_tokens=input_tokens,
                output_tokens=output_tokens, is_fallback=True,
                model=model or self.evaluator.model, discarded=discarded)
            result = self._with_reason(result, reason, text)
            structural = self._account(
                result, site=site, task_id=task_id, node_id=node_id,
                reservation=reservation)
            judgment = self._repo_judgment(
                pack_doc, axes, None, None, None, None,
                is_fallback=True, evidence=evidence)
            return result, structural, judgment

        if not self.keyed:
            return fallback_judgment(
                ["unkeyed: code-owned keyword fallback only"],
                reason=self._no_key_reason())

        reservation = None
        try:
            questions = repo_summary_question_pack(pack_doc)
            payload = state if isinstance(state, dict) else {"item": str(state)}
            reservation = self._preflight(
                site=site, max_input_tokens=max_input_tokens)
            result = self.evaluator.evaluate(payload, questions)
        except ToolCancelled as exc:
            self._settle_cancelled(reservation, exc)
            raise
        except HarnessError as exc:
            return fallback_judgment([str(exc)], reservation=reservation,
                                     reason=_refusal_reason(exc))

        answers = result.answers if isinstance(result.answers, dict) else {}
        if result.is_fallback or not answers:
            # Transport fail or shape-invalid response: never present a
            # heuristic classification as a live one. ``discarded`` rides
            # through so the run can report the paid calls it could not use
            # (DF-JEV-3) instead of presenting them as clean fallback rows.
            return fallback_judgment(
                list(result.reasons or ["invalid TypeSafe response"]),
                model=result.model,
                cost=float(result.cost or 0.0),
                input_tokens=int(result.input_tokens or 0),
                output_tokens=int(result.output_tokens or 0),
                reservation=reservation,
                discarded=bool(result.discarded),
                reason=self._fallback_reason_of(result, "invalid_answer"))

        axes: Dict[str, Optional[str]] = {}
        axis_confidence: Dict[str, Optional[float]] = {}
        evidence: List[str] = []
        for axis in pack_doc["axes"]:
            answer = answers.get(axis)
            choice = answer.get("choice") if isinstance(answer, dict) else None
            raw_axis_conf = (answer.get("confidence")
                             if isinstance(answer, dict) else None)
            axis_confidence[axis] = (
                float(raw_axis_conf)
                if isinstance(raw_axis_conf, (int, float))
                and not isinstance(raw_axis_conf, bool) else None)
            if isinstance(choice, str) and choice in axis_ids[axis]:
                axes[axis] = choice
                evidence.append(f"{axis}:{choice}")
            else:
                axes[axis] = None
                evidence.append(f"{axis}:unmatched")

        level = None
        value = None
        confidence = 0.0
        score_id = pack_doc["score"]["id"]
        score_answer = answers.get(score_id)
        if isinstance(score_answer, dict):
            probabilities = score_answer.get("probabilities")
            legend = score_answer.get("legend")
            if isinstance(probabilities, dict) and isinstance(legend, dict):
                for anchor, label in legend.items():
                    if not isinstance(label, str) or label not in level_ids:
                        continue
                    probability = probabilities.get(str(anchor))
                    if isinstance(probability, (int, float)) and (
                            value is None or float(probability) > value):
                        level, value = label, float(probability)
            raw_confidence = score_answer.get("confidence")
            if isinstance(raw_confidence, (int, float)):
                confidence = float(raw_confidence)
        if level is None:
            evidence.append("attention:unmatched")

        nouls: Dict[str, Optional[float]] = {}
        for name in pack_doc["nouls"]:
            answer = answers.get(name)
            raw = answer.get("noul") if isinstance(answer, dict) else None
            nouls[name] = (float(raw)
                           if isinstance(raw, (int, float))
                           and not isinstance(raw, bool) else None)
            if nouls[name] is None:
                evidence.append(f"{name}:unmatched")

        if not any(v is not None for v in axes.values()) and level is None:
            return fallback_judgment(
                evidence + list(result.reasons or []), model=result.model,
                cost=float(result.cost or 0.0),
                input_tokens=int(result.input_tokens or 0),
                output_tokens=int(result.output_tokens or 0),
                reservation=reservation, discarded=bool(result.discarded),
                reason=self._fallback_reason_of(result, "no_declared_axes"))

        structural = self._account(
            result, site=site, task_id=task_id, node_id=node_id,
            reservation=reservation)
        judgment = self._repo_judgment(
            pack_doc, axes, level, value, confidence, nouls,
            is_fallback=False, evidence=evidence,
            axis_confidence=axis_confidence)
        return result, structural, judgment

    @staticmethod
    def _route_query_text(state: Any) -> str:
        """Text extraction for route queries (accepts the ``goal`` key)."""
        if isinstance(state, str):
            return state
        if isinstance(state, dict):
            for key in ("goal", "query", "prompt", "issue", "text"):
                value = state.get(key)
                if isinstance(value, str):
                    return value
            return ""
        return "" if state is None else str(state)

    def evaluate_model_route(self, state, pack, *, site: str = ROUTE_QUERY_SITE,
                             task_id: Optional[str] = None,
                             node_id: Optional[str] = None,
                             max_input_tokens: int = JEV_MAX_INPUT_TOKENS):
        """Route a user query onto the declared model ladder (SITE-2).

        0-hallucination contract (same class as ``evaluate_issue_sort``):
        - choice criteria = declared rung ids only (via ``route_pack``);
          ``_parse_answer`` already requires choice ∈ criteria.
        - unkeyed / transport fail / out-of-ladder → the code-owned tier
          heuristic answers with ``is_fallback=True``; no rung is invented.
        - ladder cannot satisfy the heuristic floor → ``rung_id=None`` (the
          honest "no declared rung can do this" answer).
        - ONE ledger ``jev_eval`` per call; ``structural.site=model_route``.
        - a live in-ladder choice below ``min_confidence`` is still honored
          when it is escalation-safe: the final rung is never cheaper than the
          heuristic floor (``max(jev rung, floor)``), the combo stays
          ``is_fallback=False`` and its reasons record the confidence. The
          global ``min_confidence`` is not touched, so no other site changes.
        Extends (never modifies) ``evaluate_route``: lane choice stays there;
        this method chooses the cheapest capable DECLARED rung.
        Returns ``(result, structural, combo)``.
        """
        goal_text = self._route_query_text(state)

        try:
            pack_doc = validate_route_pack(pack)
        except ValueError as exc:
            result = JevEvaluationResult(
                "fail", 0.0, 0.0, {}, [str(exc)], cost=0.0,
                input_tokens=0, output_tokens=0, is_fallback=True,
                model=self.evaluator.model, fallback_reason="invalid_pack")
            structural = self._account(
                result, site=site, task_id=task_id, node_id=node_id)
            combo = route_combo(
                None, None, tier=None, reasons=[str(exc)],
                is_fallback=True, structural=structural)
            return result, structural, combo

        def heuristic_route(reasons, reason, *, model=None, cost=0.0,
                            input_tokens=0, output_tokens=0,
                            reservation=None, live_confidence=0.0,
                            discarded=False, state_hash=None):
            rung_id, tier, fb_reasons = fallback_route(goal_text, pack_doc)
            # ``cost`` is what the provider really billed for a live call
            # whose answer was discarded; it is settled and ledgered as-is.
            result = JevEvaluationResult(
                "pass" if rung_id else "fail", 0.0, 1.0 if rung_id else 0.0,
                {"rung": rung_id, "tier": tier},
                list(reasons or []) + list(fb_reasons),
                cost=cost, input_tokens=input_tokens,
                output_tokens=output_tokens, is_fallback=True,
                model=model or self.evaluator.model,
                discarded=bool(discarded or cost > 0.0),
                fallback_reason=reason,
                state_hash=state_hash or _digest(goal_text, pack_doc["id"]))
            # ONE ledger jev_eval per evaluate_model_route call.
            structural = self._account(
                result, site=site, task_id=task_id, node_id=node_id,
                reservation=reservation)
            combo = route_combo(
                rung_id, pack_doc, tier=tier,
                reasons=list(result.reasons), confidence=live_confidence,
                is_fallback=True, structural=structural)
            return result, structural, combo

        if not self.keyed:
            return heuristic_route(
                ["unkeyed: deterministic tier heuristic only"],
                self._no_key_reason())

        reservation = None
        try:
            questions = route_query_pack(pack_doc)
            reservation = self._preflight(
                site=site, max_input_tokens=max_input_tokens)
            result = self.evaluator.evaluate(
                {"goal": goal_text, "pack_id": pack_doc["id"]},
                questions)
        except ToolCancelled as exc:
            self._settle_cancelled(reservation, exc)
            raise
        except HarnessError as exc:
            return heuristic_route(
                [str(exc)], _refusal_reason(exc), reservation=reservation)

        answers = result.answers if isinstance(result.answers, dict) else {}
        rung_ans = answers.get("rung")
        choice = rung_ans.get("choice") if isinstance(rung_ans, dict) else None
        declared = {r["rung_id"] for r in pack_doc["rungs"]}
        in_ladder = (not result.is_fallback
                     and isinstance(choice, str)
                     and choice in declared)

        if in_ladder and result.verdict == "pass":
            structural = self._account(
                result, site=site, task_id=task_id, node_id=node_id,
                reservation=reservation)
            entry = next(r for r in pack_doc["rungs"]
                         if r["rung_id"] == choice)
            combo = route_combo(
                choice, pack_doc, tier=entry["tier"],
                reasons=list(result.reasons or []) + [f"choice:{choice}"],
                confidence=result.confidence, is_fallback=False,
                structural=structural)
            return result, structural, combo

        min_confidence = float(getattr(self.evaluator, "min_confidence", 0.70))
        live_confidence = float(result.confidence or 0.0)
        if (in_ladder and result.verdict == "fail"
                and LOW_CONFIDENCE_FLOOR <= live_confidence < min_confidence):
            # Confidence-aware acceptance: a declared rung chosen with low
            # confidence is still a real, in-vocabulary answer. Honor it only
            # when escalation-safe -- never cheaper than the heuristic floor.
            floor = tier_floor_for_goal(goal_text)
            entry = next(r for r in pack_doc["rungs"]
                         if r["rung_id"] == choice)
            final = (choice if tier_rank(entry["tier"]) >= tier_rank(floor)
                     else choose_rung_for_tier(pack_doc, floor))
            if final is not None:
                final_entry = next(r for r in pack_doc["rungs"]
                                   if r["rung_id"] == final)
                reasons = list(result.reasons or []) + [
                    f"choice:{final}",
                    (f"low-confidence live choice {choice!r} accepted at "
                     f"confidence {live_confidence:.2f} < {min_confidence:.2f}"
                     f" (escalation-safe; heuristic floor {floor})")]
                if final != choice:
                    reasons.append(
                        f"raised {choice!r} -> {final!r} to meet heuristic "
                        f"floor {floor}")
                result = replace(result, verdict="pass", reasons=reasons)
                structural = self._account(
                    result, site=site, task_id=task_id, node_id=node_id,
                    reservation=reservation)
                combo = route_combo(
                    final, pack_doc, tier=final_entry["tier"],
                    reasons=list(reasons), confidence=live_confidence,
                    is_fallback=False, structural=structural)
                return result, structural, combo

        # Out-of-ladder / transport fail / fallback / unparseable choice:
        # never invent a rung. Deterministic heuristic answers instead.
        reasons = list(result.reasons) if result.reasons else []
        if isinstance(choice, str) and choice not in declared:
            reasons = reasons + [
                f"out-of-ladder choice refused: {choice!r}"]
            default_reason = "route_out_of_vocabulary"
        elif in_ladder and live_confidence < LOW_CONFIDENCE_FLOOR:
            default_reason = "low_confidence"
            reasons = reasons + [
                f"live confidence {live_confidence:.2f} below the "
                f"{LOW_CONFIDENCE_FLOOR:.2f} floor; choice discarded"]
        elif in_ladder:
            default_reason = "below_floor_unsatisfiable"
        else:
            default_reason = "route_unanswered"
        return heuristic_route(
            reasons, self._fallback_reason_of(result, default_reason),
            model=result.model,
            cost=float(result.cost or 0.0),
            input_tokens=int(result.input_tokens or 0),
            output_tokens=int(result.output_tokens or 0),
            reservation=reservation,
            live_confidence=live_confidence,
            discarded=bool(result.discarded),
            state_hash=result.state_hash)

    @staticmethod
    def _completion_state_text(state: Any) -> str:
        """Bounded text view of one phase's evidence for the JEV-BAR TypeSafe
        payload and the code-owned keyword fallback."""
        if isinstance(state, str):
            return state[:2400]
        if not isinstance(state, dict):
            return str(state)[:2400]
        parts = [
            str(state.get("phase") or ""),
            str(state.get("status_row") or ""),
            " ".join(str(b) for b in (state.get("open_blockers") or [])[:12]),
            " ".join(str(t) for t in (state.get("tests_missing") or [])[:12]),
            f"pr_merged={state.get('pr_merged')}",
            f"local_gates_green={state.get('local_gates_green')}",
            f"ci_green={state.get('ci_green')}",
            " ".join(str(n) for n in (state.get("notes") or [])[:6]),
        ]
        return " ".join(p for p in parts if p)[:2400]

    @staticmethod
    def _completion_judgment(pack_doc, live_levels, live_confidence, primary_gap,
                             *, is_fallback: bool, evidence) -> Dict[str, Any]:
        pack_doc = pack_doc if isinstance(pack_doc, dict) else {}
        return {
            "pack_id": pack_doc.get("id"),
            "live_levels": dict(live_levels or {}),
            "live_confidence": dict(live_confidence or {}),
            "primary_gap": primary_gap,
            "is_fallback": bool(is_fallback),
            "evidence": list(evidence or []),
        }

    def evaluate_phase_completion(self, state, pack, *, site=PHASE_COMPLETION_SITE,
                                  task_id: Optional[str] = None,
                                  node_id: Optional[str] = None,
                                  max_input_tokens: int = JEV_MAX_INPUT_TOKENS):
        """Judge one mission phase's evidence against the operator
        phase-completion sentiment pack (JEV-BAR).

        0-hallucination contract (same class as ``evaluate_log_item`` /
        ``evaluate_repo_summary``):
        - every axis's ``live_levels`` value is an index into the pack's own
          declared ``sentiment.levels`` (via the official legend: anchors ->
          criteria strings, highest probability wins), else ``None`` -- a
          missing/invalid answer never invents a level;
        - ``primary_gap`` ∈ declared bucket ids, else ``None``; the choice
          ``\"none\"`` also maps to ``None`` (no improvement needed); an
          out-of-pack choice is refused (reason recorded) without failing the
          whole call;
        - unkeyed / invalid-pack / transport-fail / all-axes-invalid paths
          fall back to the code-owned keyword matcher (``buckets[].keywords``
          in the pack only) for ``primary_gap``; ``live_levels`` stay ``None``
          across the board -- code's own heuristic (``jev_packs.
          heuristic_completion_sentiment``) lives outside this call and is
          mixed in by the caller (``jev_completion.score_phase_completion``),
          never invented here;
        - ONE ledger ``jev_eval`` per call; ``structural.site=phase_completion``.
        Returns ``(result, structural, judgment)``.
        """
        text = self._completion_state_text(state)
        try:
            pack_doc = validate_completion_pack(pack)
        except ValueError as exc:
            result = JevEvaluationResult(
                "fail", 0.0, 0.0, {}, [str(exc)], cost=0.0,
                input_tokens=0, output_tokens=0, is_fallback=True,
                model=self.evaluator.model, fallback_reason="invalid_pack")
            structural = self._account(
                result, site=site, task_id=task_id, node_id=node_id)
            judgment = self._completion_judgment(
                None, None, None, None, is_fallback=True, evidence=result.reasons)
            return result, structural, judgment

        axis_ids = list(pack_doc["axes"])
        level_ids = list(pack_doc["sentiment"]["levels"])
        bucket_ids = set(pack_doc["buckets"])
        none_live_levels = {axis: None for axis in axis_ids}
        none_live_confidence = {axis: None for axis in axis_ids}

        def fallback_judgment(reasons, *, model=None, cost=0.0,
                              input_tokens=0, output_tokens=0,
                              reservation=None, reason=None,
                              discarded=False):
            bucket_id, _hits, kw_evidence = match_completion_keywords(text, pack_doc)
            evidence_refs = list(reasons or []) + list(kw_evidence or [])
            result = JevEvaluationResult(
                "pass" if bucket_id else "fail", 0.0, 1.0 if bucket_id else 0.0,
                {"primary_gap": bucket_id}, evidence_refs,
                cost=cost, input_tokens=input_tokens,
                output_tokens=output_tokens, is_fallback=True,
                model=model or self.evaluator.model, discarded=discarded)
            result = self._with_reason(result, reason, text)
            # ONE ledger jev_eval per evaluate_phase_completion call.
            structural = self._account(
                result, site=site, task_id=task_id, node_id=node_id,
                reservation=reservation)
            judgment = self._completion_judgment(
                pack_doc, none_live_levels, none_live_confidence, bucket_id,
                is_fallback=True, evidence=evidence_refs)
            return result, structural, judgment

        if not self.keyed:
            return fallback_judgment(["unkeyed: keyword match only"],
                                     reason=self._no_key_reason())

        reservation = None
        try:
            questions = completion_bar_question_pack(pack_doc)
            payload = {
                "phase": state.get("phase") if isinstance(state, dict) else None,
                "evidence": text, "pack_id": pack_doc["id"],
            }
            reservation = self._preflight(
                site=site, max_input_tokens=max_input_tokens)
            result = self.evaluator.evaluate(payload, questions)
        except ToolCancelled as exc:
            self._settle_cancelled(reservation, exc)
            raise
        except HarnessError as exc:
            return fallback_judgment([str(exc)], reservation=reservation,
                                     reason=_refusal_reason(exc))

        answers = result.answers if isinstance(result.answers, dict) else {}
        if result.is_fallback or not answers:
            # Transport fail or shape-invalid response: never present a
            # heuristic classification as a live one.
            return fallback_judgment(
                list(result.reasons or ["invalid TypeSafe response"]),
                model=result.model,
                cost=float(result.cost or 0.0),
                input_tokens=int(result.input_tokens or 0),
                output_tokens=int(result.output_tokens or 0),
                reservation=reservation, discarded=bool(result.discarded),
                reason=self._fallback_reason_of(result, "invalid_answer"))

        live_levels: Dict[str, Optional[int]] = {}
        live_confidence: Dict[str, Optional[float]] = {}
        evidence: List[str] = []
        any_axis_valid = False
        for axis in axis_ids:
            answer = answers.get(axis)
            level = None
            value = None
            conf = None
            if isinstance(answer, dict):
                probs = answer.get("probabilities")
                legend = answer.get("legend")
                if isinstance(probs, dict) and isinstance(legend, dict):
                    for anchor, lvl in legend.items():
                        if not isinstance(lvl, str) or lvl not in level_ids:
                            continue
                        prob = probs.get(str(anchor))
                        if isinstance(prob, (int, float)) and (
                                value is None or float(prob) > value):
                            level, value = lvl, float(prob)
                raw_conf = answer.get("confidence")
                if isinstance(raw_conf, (int, float)) and not isinstance(raw_conf, bool):
                    conf = float(raw_conf)
            if level is not None:
                any_axis_valid = True
                live_levels[axis] = level_ids.index(level)
                evidence.append(f"{axis}:{level}")
            else:
                live_levels[axis] = None
                evidence.append(f"{axis}:unmatched")
            live_confidence[axis] = conf

        primary_gap: Optional[str] = None
        gap_answer = answers.get("primary_gap")
        choice = gap_answer.get("choice") if isinstance(gap_answer, dict) else None
        if isinstance(choice, str) and choice == "none":
            evidence.append("primary_gap:none")
        elif isinstance(choice, str) and choice in bucket_ids:
            primary_gap = choice
            evidence.append(f"primary_gap:{choice}")
        elif isinstance(choice, str):
            evidence.append(f"out-of-pack primary_gap refused: {choice!r}")
        else:
            evidence.append("primary_gap:unmatched")

        if not any_axis_valid:
            # Every axis was missing/invalid: present the whole call as a
            # fallback rather than a live judgment with nothing declared.
            return fallback_judgment(
                evidence + list(result.reasons or []), model=result.model,
                cost=float(result.cost or 0.0),
                input_tokens=int(result.input_tokens or 0),
                output_tokens=int(result.output_tokens or 0),
                reservation=reservation, discarded=bool(result.discarded),
                reason=self._fallback_reason_of(result, "no_declared_axes"))

        structural = self._account(
            result, site=site, task_id=task_id, node_id=node_id,
            reservation=reservation)
        judgment = self._completion_judgment(
            pack_doc, live_levels, live_confidence, primary_gap,
            is_fallback=False, evidence=evidence)
        return result, structural, judgment

    @staticmethod
    def _audit_dimension_text(dimension_evidence: Any) -> str:
        lines = ["Harness 4-Dimensional Self-Audit Evidence:"]
        for dim in ("A", "R", "SM", "SD"):
            ev = (dimension_evidence or {}).get(dim, {})
            score = ev.get("score", 0.0) if isinstance(ev, dict) else 0.0
            satisfied = ev.get("checks_satisfied", 0)
            total = ev.get("checks_count", 0)
            lines.append(f"Dimension {dim}: score={score:.2f}/10 ({satisfied}/{total} checks fully satisfied)")
            for ch in (ev.get("checks") or [])[:5]:
                cid = ch.get("id", "")
                mark = "pass" if ch.get("score", 0) >= 1 else "part"
                label = ch.get("label", "")
                lines.append(f"  [{cid}] {mark} {label}")
        return "\n".join(lines)

    def evaluate_audit_dimensions(
        self,
        dimension_evidence: Dict[str, Any],
        pack: Any = None,
        *,
        site: str = AUDIT_DIMENSIONS_SITE,
        task_id: Optional[str] = "audit",
    ) -> Dict[str, Any]:
        """Evaluate the 4 self-audit dimensions with Jev as the authoritative gate.

        0-hallucination / honesty contract (same class as
        ``evaluate_log_item`` / ``evaluate_phase_completion``):

        - a dimension ABSENT from ``dimension_evidence`` (a partial ``--dim``
          run) is reported ``not_evaluated`` -- ``level_index``/``score`` are
          ``None`` -- never a false 0.0/"failing" score for a check that
          never ran;
        - a live ``dim_<id>`` answer resolves to a level via the official
          legend (anchor -> declared level string) + highest probability,
          exactly like every other score-typed site in this module -- never
          a raw score-as-index guess. An unmatched/invalid live answer for
          one EVALUATED dimension falls back to that dimension's own
          evidence-calibrated heuristic; if every evaluated dimension is
          unmatched, the whole call is presented as a fallback rather than a
          live judgment with nothing actually declared by Jev;
        - ``bar_95_pass`` is true only when ALL FOUR declared dimensions
          were evaluated AND every one scores >= 9.5 -- fail closed, never a
          false PASS on a partial run;
        - preflight reservation + EXACTLY ONE ledger ``jev_eval`` per call
          (``structural.site=audit_dimensions``) on every path -- unkeyed,
          governor-missing, transport-error, and invalid-response fallbacks
          included -- via the shared ``_account`` accounting helper (never a
          bare ``jev_refusal`` standing in for the one required
          ``jev_eval``).
        """
        from .jev_packs import (
            audit_dimensions_question_pack,
            heuristic_audit_dimensions,
            validate_audit_pack,
        )

        pack_doc = validate_audit_pack(pack)
        questions = audit_dimensions_question_pack(pack_doc)
        levels = pack_doc["sentiment"]["levels"]
        ordinals = pack_doc["sentiment"]["ordinals"]
        bar_idx = pack_doc["sentiment"].get("bar_met_index", 3)
        dim_ids = list(pack_doc["dimensions"])
        evidence_is_dict = isinstance(dimension_evidence, dict)

        model_name = getattr(self.evaluator, "model", "jev-latest")

        def dim_evaluated(dim: str) -> bool:
            return (not evidence_is_dict) or (dim in dimension_evidence)

        def bar_pass(dim_results: Dict[str, Any]) -> bool:
            if set(dim_results) != set(dim_ids):
                return False
            if any(not d.get("evaluated") for d in dim_results.values()):
                return False
            scores = [d.get("score") for d in dim_results.values()]
            if any(s is None for s in scores):
                return False
            return all(float(s) >= 9.5 for s in scores)

        def fallback_judgment(reasons, *, model=None, cost=0.0, input_tokens=0,
                              output_tokens=0, reservation=None, reason=None):
            dim_results = heuristic_audit_dimensions(dimension_evidence, pack_doc)
            scores = {dim: d.get("score") for dim, d in dim_results.items()}
            evaluated_scores = [s for s in scores.values() if s is not None]
            passed = bar_pass(dim_results)
            fallback_result = JevEvaluationResult(
                "pass" if passed else "fail", 0.0,
                1.0 if evaluated_scores else 0.0,
                {f"dim_{dim}": dim_results[dim] for dim in dim_ids},
                list(reasons), cost=cost, input_tokens=input_tokens,
                output_tokens=output_tokens, is_fallback=True,
                model=model or model_name)
            fallback_result = self._with_reason(
                fallback_result, reason,
                self._audit_dimension_text(dimension_evidence))
            self._account(fallback_result, site=site, task_id=task_id,
                          reservation=reservation)
            return {
                "dimensions": dim_results,
                "scores": scores,
                "bar_95_pass": passed,
                "min_score": min(evaluated_scores) if evaluated_scores else 0.0,
                "is_fallback": True,
                "reasons": list(reasons),
                "model": model or model_name,
                "cost": cost,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            }

        if not self.keyed or self.governor is None:
            return fallback_judgment(
                ["unkeyed" if not self.keyed else "governor_not_provided"],
                reason=(self._no_key_reason() if not self.keyed
                        else "governor_not_provided"))

        text = self._audit_dimension_text(dimension_evidence)
        reservation = None
        try:
            payload = {"evidence": text, "pack_id": pack_doc["id"]}
            reservation = self._preflight(
                site=site, max_input_tokens=JEV_MAX_INPUT_TOKENS)
            result = self.evaluator.evaluate(payload, questions)
        except ToolCancelled as exc:
            self._settle_cancelled(reservation, exc)
            raise
        except HarnessError as exc:
            return fallback_judgment(
                [f"transport_error: {exc}"], reservation=reservation,
                reason=_refusal_reason(exc))
        except Exception as exc:  # unexpected transport failure -- fail closed
            return fallback_judgment(
                [f"transport_error: {exc}"], reservation=reservation,
                reason="transport_failure")

        answers = result.answers if isinstance(result.answers, dict) else {}
        if result.is_fallback or not answers:
            return fallback_judgment(
                list(result.reasons or ["invalid TypeSafe response"]),
                model=result.model or model_name,
                cost=float(result.cost or 0.0),
                input_tokens=int(result.input_tokens or 0),
                output_tokens=int(result.output_tokens or 0),
                reservation=reservation,
                reason=self._fallback_reason_of(result, "invalid_answer"),
            )

        dim_results = {}
        any_dim_valid = False
        evaluated_dim_ids = []
        for dim, spec in pack_doc["dimensions"].items():
            if not dim_evaluated(dim):
                dim_results[dim] = {
                    "name": spec["name"],
                    "level_index": None,
                    "level": "not_evaluated",
                    "score": None,
                    "bar_met": False,
                    "confidence": None,
                    "evaluated": False,
                }
                continue
            evaluated_dim_ids.append(dim)
            ans = answers.get(f"dim_{dim}")
            level = None
            value = None
            conf = None
            if isinstance(ans, dict):
                probs = ans.get("probabilities")
                legend = ans.get("legend")
                if isinstance(probs, dict) and isinstance(legend, dict):
                    for anchor, lvl in legend.items():
                        if not isinstance(lvl, str) or lvl not in levels:
                            continue
                        prob = probs.get(str(anchor))
                        if isinstance(prob, (int, float)) and (
                                value is None or float(prob) > value):
                            level, value = lvl, float(prob)
                raw_conf = ans.get("confidence")
                if isinstance(raw_conf, (int, float)) and not isinstance(raw_conf, bool):
                    conf = float(raw_conf)
            if level is not None:
                idx = levels.index(level)
                any_dim_valid = True
            else:
                # Declared answer missing/invalid: fall back to this ONE
                # dimension's own evidence-calibrated index -- never an
                # invented level.
                ev = dimension_evidence.get(dim) if evidence_is_dict else None
                ev_score = float(ev.get("score", 0.0)) if isinstance(ev, dict) else 0.0
                idx = (4 if ev_score >= 9.99 else
                      3 if ev_score >= 9.5 else
                      2 if ev_score >= 8.5 else
                      1 if ev_score >= 7.0 else 0)
            dim_results[dim] = {
                "name": spec["name"],
                "level_index": idx,
                "level": levels[idx],
                "score": round(ordinals[idx], 2),
                "bar_met": idx >= bar_idx,
                "confidence": conf,
                "evaluated": True,
            }

        if evaluated_dim_ids and not any_dim_valid:
            # Every evaluated dimension's live answer was unmatched/invalid:
            # present the whole call as a fallback rather than a live
            # judgment with nothing actually declared by Jev.
            return fallback_judgment(
                [f"dim_{dim}: unmatched" for dim in evaluated_dim_ids]
                + list(result.reasons or []),
                model=result.model, cost=float(result.cost or 0.0),
                input_tokens=int(result.input_tokens or 0),
                output_tokens=int(result.output_tokens or 0),
                reservation=reservation,
                reason=self._fallback_reason_of(result, "no_declared_axes"))

        self._account(result, site=site, task_id=task_id, reservation=reservation)
        scores = {dim: d.get("score") for dim, d in dim_results.items()}
        evaluated_scores = [s for s in scores.values() if s is not None]
        return {
            "dimensions": dim_results,
            "scores": scores,
            "bar_95_pass": bar_pass(dim_results),
            "min_score": min(evaluated_scores) if evaluated_scores else 0.0,
            "is_fallback": False,
            "reasons": [],
            "model": result.model or model_name,
            "cost": float(result.cost or 0.0),
            "input_tokens": int(result.input_tokens or 0),
            "output_tokens": int(result.output_tokens or 0),
        }

    def evaluate_vision_assessment(
            self, state: Any, *, task_id: Optional[str] = None, pack: Any = None):
        """Run the one-request HV-0 assessment through the shared policy owner.

        The result is advisory design evidence only. It has no phase,
        readiness, or completion authority. Any preflight or response failure
        leaves every category unassessed.
        """
        pack_doc = validate_vision_assessment_pack(
            DEFAULT_VISION_ASSESSMENT_PACK if pack is None else pack)
        questions = vision_assessment_question_pack(pack_doc)
        model_name = getattr(self.evaluator, "model", None)
        if not isinstance(model_name, str) or not model_name.strip():
            model_name = "jev-latest"
        try:
            sanitized_state = sanitize_vision_state(state)
            metrics = vision_assessment_preflight(
                sanitized_state, model_name, pack_doc)
        except (TypeError, ValueError) as exc:
            metrics = {
                "payload_outline": {}, "payload_utf8_bytes": 0,
                "estimated_input_tokens": 0,
                "estimated_state_longest_question_tokens": 0,
                "request_margin_tokens": VISION_ASSESSMENT_MAX_REQUEST_TOKENS,
                "state_longest_question_margin_tokens": VISION_ASSESSMENT_MAX_STATE_QUESTION_TOKENS,
                "fits_context": False,
            }
            refusal_reason = "assessment state is invalid: " + str(exc)
        else:
            refusal_reason = None

        category_ids = list(pack_doc["categories"])
        categories: Dict[str, Optional[VisionCategoryAssessment]] = {
            category_id: None for category_id in category_ids
        }
        threshold = float(pack_doc["confidence_threshold"])

        def usage_source(result=None):
            if result is None:
                return "unavailable"
            if result.input_tokens_observed and result.output_tokens_observed:
                return "actual"
            if result.input_tokens_observed or result.output_tokens_observed:
                return "actual_partial"
            return "unavailable"

        def event_metadata(result_state: str, fallback_state: str,
                           result=None) -> Dict[str, Any]:
            return {
                "capability": VISION_ASSESSMENT_SITE,
                "pack_id": VISION_ASSESSMENT_PACK_ID,
                "pack_version": VISION_ASSESSMENT_PACK_VERSION,
                "result_state": result_state,
                "fallback_state": fallback_state,
                "observed_model": (result.model if result
                                   and result.model_observed else None),
                "model_observed": bool(result and result.model_observed),
                "usage_source": usage_source(result),
                "cost_source": ("actual_input" if result
                                and result.input_tokens_observed else
                                "estimated_input" if result else "unavailable"),
                "input_tokens_observed": bool(
                    result and result.input_tokens_observed),
                "output_tokens_observed": bool(
                    result and result.output_tokens_observed),
                "estimated_input_tokens": int(metrics["estimated_input_tokens"]),
                "payload_utf8_bytes": int(metrics["payload_utf8_bytes"]),
                "question_count": len(questions),
                "request_margin_tokens": int(metrics["request_margin_tokens"]),
                "state_longest_question_margin_tokens": int(
                    metrics["state_longest_question_margin_tokens"]),
            }

        def make_envelope(status: str, *, result=None,
                          reasons: Optional[List[str]] = None,
                          perfect: bool = False) -> VisionAssessmentEnvelope:
            source = usage_source(result)
            return VisionAssessmentEnvelope(
                status=status,
                pack_id=VISION_ASSESSMENT_PACK_ID,
                pack_version=VISION_ASSESSMENT_PACK_VERSION,
                confidence_threshold=threshold,
                model=(result.model if result and result.model_observed else None),
                model_observed=bool(result and result.model_observed),
                fallback_state=("not_dispatched" if result is None else
                                "not_used" if not result.is_fallback else "fallback"),
                usage_source=source,
                input_tokens=(int(result.input_tokens)
                              if result and result.input_tokens_observed else None),
                output_tokens=(int(result.output_tokens)
                               if result and result.output_tokens_observed else None),
                estimated_input_tokens=int(metrics["estimated_input_tokens"]),
                estimated_state_longest_question_tokens=int(
                    metrics["estimated_state_longest_question_tokens"]),
                payload_utf8_bytes=int(metrics["payload_utf8_bytes"]),
                request_margin_tokens=int(metrics["request_margin_tokens"]),
                state_longest_question_margin_tokens=int(
                    metrics["state_longest_question_margin_tokens"]),
                cost_usd=(float(result.cost or 0.0) if result
                          and result.input_tokens_observed else
                          jev_cost(int(metrics["estimated_input_tokens"]))
                          if result else 0.0),
                cost_source=("actual_input" if result
                             and result.input_tokens_observed else
                             "estimated_input" if result else "unavailable"),
                perfect=perfect,
                categories=categories,
                payload_outline=dict(metrics["payload_outline"]),
                reasons=list(reasons or []),
            )

        def refuse(reason: str):
            self._record_refusal(
                reason, site=VISION_ASSESSMENT_SITE, task_id=task_id,
                event_metadata=event_metadata("unassessed", "not_dispatched"))
            return make_envelope("unassessed", reasons=[reason])

        if refusal_reason:
            return refuse(refusal_reason)
        if not metrics["fits_context"]:
            return refuse("full assessment request exceeds a published context limit")
        if self.ledger is None:
            return refuse("autonomy ledger unavailable; assessment not dispatched")
        if not self.keyed:
            return refuse("TypeSafe key unavailable; assessment not dispatched")
        if not callable(getattr(self.evaluator, "evaluate_once", None)):
            return refuse("one-attempt TypeSafe evaluator is unavailable")
        transport = getattr(self.evaluator, "transport", None)
        if not callable(getattr(transport, "post_once", None)):
            return refuse("one-attempt TypeSafe transport is unavailable")
        reserve_cost = jev_cost(int(metrics["estimated_input_tokens"]))
        if reserve_cost > HARD_MAX_COST:
            return refuse("estimated assessment reserve exceeds HARD_MAX_COST")

        reservation = None
        try:
            reservation = self._preflight(
                site=VISION_ASSESSMENT_SITE,
                max_input_tokens=int(metrics["estimated_input_tokens"]),
                eager=True)
        except HarnessError as exc:
            return refuse(str(exc))

        try:
            result = self.evaluator.evaluate_once(sanitized_state, questions)
        except ToolCancelled as exc:
            self._settle_cancelled(reservation, exc)
            raise
        except Exception as exc:
            # An injected evaluator should not turn an exception into an
            # implicit fallback score; record the dispatched attempt once.
            result = JevEvaluationResult(
                "fail", 0.0, 0.0, {},
                ["TypeSafe evaluation failed (" + type(exc).__name__ + ")"],
                cost=0.0, input_tokens=0, output_tokens=0,
                is_fallback=False, model=None)

        assessed = False
        reasons = list(result.reasons or [])
        if result.is_fallback:
            reasons = ["fallback result cannot assess the vision"]
        elif not result.model_observed:
            reasons = ["response did not include an observed model identity"]
        elif not result.usage_observed:
            reasons = ["response did not include complete observed token usage"]
        else:
            try:
                validated = validate_vision_assessment_answers(
                    result.answers, pack_doc)
            except (TypeError, ValueError) as exc:
                reasons = ["assessment response is invalid: " + str(exc)]
            else:
                assessed = True
                for category_id, spec in pack_doc["categories"].items():
                    answer = validated[category_id]
                    probabilities = answer["probabilities"]
                    top_probability = max(probabilities.values())
                    # A tie deliberately chooses the lower level ordinal.
                    selected = min(
                        int(level) for level, probability in probabilities.items()
                        if probability == top_probability)
                    review_required = answer["confidence"] < threshold
                    buckets = spec["improvement_buckets"]
                    actions = spec["improvement_actions"]
                    bucket = buckets[selected] if selected < len(buckets) else None
                    action = actions[selected] if selected < len(actions) else None
                    categories[category_id] = VisionCategoryAssessment(
                        score=answer["score"], selected_level=selected,
                        selected_score_10=round(
                            10.0 * selected / (len(spec["levels"]) - 1), 2),
                        probabilities=probabilities,
                        confidence=answer["confidence"],
                        evidence_refs=list(spec["evidence_refs"]),
                        improvement_bucket=bucket,
                        suggested_next_action=action,
                        review_required=review_required)
                perfect = all(
                    category is not None
                    and category.selected_level == len(
                        pack_doc["categories"][category_id]["levels"]) - 1
                    and category.confidence >= threshold
                    and category.improvement_bucket is None
                    and not category.review_required
                    for category_id, category in categories.items())

        metadata = event_metadata(
            "assessed" if assessed else "unassessed",
            "not_used" if not result.is_fallback else "fallback", result)
        # Nothing was sent (breaker open, no key): it costs nothing. Only a
        # call that may have reached the wire with unobserved usage settles
        # at the conservative estimate.
        settlement_cost = (
            float(result.cost or 0.0)
            if result.input_tokens_observed or result.is_fallback
            else jev_cost(int(metrics["estimated_input_tokens"]))
        )
        self._account(
            result, site=VISION_ASSESSMENT_SITE, task_id=task_id,
            reservation=reservation, settlement_cost=settlement_cost,
            event_metadata=metadata,
            preserve_event_on_settlement_error=True)
        settlement_error = metadata.get("settlement_error")
        if settlement_error:
            assessed = False
            perfect = False
            reasons = ["spend settlement failed after dispatch: " + settlement_error]
            categories = {category_id: None for category_id in category_ids}
        return make_envelope(
            "assessed" if assessed else "unassessed", result=result,
            reasons=reasons, perfect=bool(assessed and perfect))

    def evaluate_provision(self, state, questions, *, site: str = PROVISION_SITE,
                           task_id: Optional[str] = None,
                           node_id: Optional[str] = None,
                           max_input_tokens: int = JEV_MAX_INPUT_TOKENS):
        """Run one provisioning question pack (recipe choice or plan review).

        ``harness/provision.py`` owns the packs' meaning and refuses any
        answer outside its declared candidates; this method only owns the
        dispatch, bounded spend, and the ONE ledger ``jev_eval``. Unkeyed
        returns an honest ``is_fallback=True`` result with NO answers -- the
        planner then falls back to its deterministic order, and nothing is
        ever approved by a missing judgment. Returns ``(result, structural)``.
        """
        if not self.keyed:
            result = JevEvaluationResult(
                "fail", 0.0, 0.0, {},
                ["unkeyed: provisioning falls back to the deterministic plan"],
                is_fallback=True, model=self.evaluator.model,
                fallback_reason="missing_key")
            structural = self._account(
                result, site=site, task_id=task_id, node_id=node_id)
            return result, structural
        reservation = None
        try:
            reservation = self._preflight(
                site=site, max_input_tokens=max_input_tokens)
            result = self.evaluator.evaluate(state, questions)
            structural = self._account(
                result, site=site, task_id=task_id, node_id=node_id,
                reservation=reservation)
            reservation = None
            return result, structural
        except ToolCancelled as exc:
            self._settle_cancelled(reservation, exc)
            raise
        except HarnessError as exc:
            self._release(reservation)
            return self._record_refusal(
                str(exc), site=site, task_id=task_id, node_id=node_id)

    @staticmethod
    def attach(envelope: Dict[str, Any], structural: Optional[Dict[str, Any]]):
        """Attach the stable structural block without changing status."""
        if structural is not None:
            envelope["structural"] = dict(structural)
        return envelope


def policy_for(settings, *, transport=None, governor=None, ledger=None,
               evaluator=None, **options) -> JevPolicy:
    """Construct the shared policy at a session composition boundary.

    ``options`` are the policy's resilience knobs (``breaker_threshold``,
    ``breaker_cooldown``, ``clock``); omit them for the defaults.
    """
    return JevPolicy(
        settings, transport=transport, governor=governor,
        ledger=ledger, evaluator=evaluator, **options,
    )
