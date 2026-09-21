"""Cost-bounded spend: the governor that makes ceilings guarantees, not hopes.

Everything billable passes through :class:`SpendGovernor`: pre-flight
worst-case math against live per-token pricing, mid-batch fail-closed checks,
BYOK denylist/learning, and the key-identity gate. Free-model discovery also
lives here because it is pricing policy over the same live catalog.

HUL-B dual envelope (ONE owner of the dollar formula and enforcement)::

    working_remaining = max_cost_usd - spent - terminal_reserve.cost_usd

Non-terminal *attempt* work never spends into ``terminal_reserve``. Terminal
FINDINGS may spend up to the reserve only when the mission is already
terminal. Mission packs store the same numbers via :mod:`harness.mission_record`
which delegates the formula here — no second governor.
"""
import threading
import time

from .config import (
    OPENROUTER_KEY_URL, OPENROUTER_MODELS_URL,
    BYOK_DENYLIST_PREFIXES, BYOK_PREFIXES_PATH, load_byok_prefixes,
    save_byok_prefixes, DEFAULT_MAX_COST,
)
from .errors import HarnessError
from .output import eprint
from .tokens import estimate_prompt_tokens
from .validation import finite_number

# Dual-budget phases. Attempts are the default and never eat the reserve.
PHASE_ATTEMPT = "attempt"
PHASE_TERMINAL = "terminal"
_PHASES = frozenset((PHASE_ATTEMPT, PHASE_TERMINAL))


def normalize_phase(phase):
    """Validate a dual-budget phase name (attempt | terminal)."""
    if phase is None:
        return PHASE_ATTEMPT
    text = str(phase).strip().lower()
    if text not in _PHASES:
        raise HarnessError(
            f"spend phase must be one of {sorted(_PHASES)}; got {phase!r}")
    return text


def working_remaining(max_cost_usd, spent, terminal_reserve_cost_usd):
    """HOLY remaining attempt budget after spent and terminal reserve.

    ``working_remaining = max(0, max_cost - spent - terminal_reserve)``.

    This is the ONE formula owner. Mission ``budget.json`` and
    :class:`SpendGovernor` both call here so the dual envelope cannot drift.
    """
    max_cost = finite_number(max_cost_usd, "max_cost_usd", 0.0)
    spent_f = finite_number(spent, "spent", 0.0)
    reserve = finite_number(terminal_reserve_cost_usd,
                            "terminal_reserve_cost_usd", 0.0)
    return max(0.0, max_cost - spent_f - reserve)


def dual_budget_envelope(max_cost_usd, spent, terminal_reserve_cost_usd,
                         *, phase=PHASE_ATTEMPT, outstanding=0.0):
    """Honest dual-envelope numbers for budget.json / governor snapshots.

    ``working_remaining`` always uses the attempt formula (reserve excluded).
    ``phase_remaining`` is what the current phase may still commit:
    attempts exclude the reserve; terminal findings may spend into it.
    """
    max_cost = finite_number(max_cost_usd, "max_cost_usd", 0.0)
    spent_f = finite_number(spent, "spent", 0.0)
    reserve = finite_number(terminal_reserve_cost_usd,
                            "terminal_reserve_cost_usd", 0.0)
    outstanding_f = finite_number(outstanding, "outstanding", 0.0)
    if reserve > max_cost:
        raise HarnessError(
            "terminal_reserve.cost_usd must not exceed max_cost_usd")
    phase_n = normalize_phase(phase)
    work = max(0.0, max_cost - spent_f - reserve)
    if phase_n == PHASE_TERMINAL:
        phase_ceiling = max_cost
        terminal_available = max(0.0, max_cost - spent_f - outstanding_f)
    else:
        phase_ceiling = max(0.0, max_cost - reserve)
        terminal_available = max(0.0, max_cost - spent_f - outstanding_f)
    phase_rem = max(0.0, phase_ceiling - spent_f - outstanding_f)
    return {
        "max_cost_usd": max_cost,
        "spent": spent_f,
        "outstanding": outstanding_f,
        "terminal_reserve_cost_usd": reserve,
        "working_remaining": work,
        "phase": phase_n,
        "phase_ceiling": phase_ceiling,
        "phase_remaining": phase_rem,
        "terminal_available": terminal_available,
    }


def assert_spend_allowed(max_cost_usd, spent, amount, terminal_reserve_cost_usd,
                         *, phase=PHASE_ATTEMPT, outstanding=0.0, label="spend"):
    """Fail-closed dual-budget check. Returns the validated amount.

    Attempt phase refuses any worst-case/actual that would eat into
    ``terminal_reserve``. Terminal phase may spend up to ``max_cost``
    (reserve unlocked only for terminal findings / terminal mission work).
    """
    env = dual_budget_envelope(
        max_cost_usd, spent, terminal_reserve_cost_usd,
        phase=phase, outstanding=outstanding)
    amount_f = finite_number(amount, "amount", 0.0)
    if amount_f == 0.0:
        # $0 (free-tier / unkeyed fallback) never eats the reserve; keep the
        # numbers honest instead of inventing a refusal for a non-spend.
        return amount_f
    ceiling = env["phase_ceiling"]
    if env["spent"] + env["outstanding"] + amount_f > ceiling:
        if env["phase"] == PHASE_ATTEMPT and env["terminal_reserve_cost_usd"] > 0.0:
            raise HarnessError(
                f"{label} worst-case ${amount_f:.6f} would eat terminal_reserve "
                f"${env['terminal_reserve_cost_usd']:.6f}; attempt working remaining "
                f"is ${env['phase_remaining']:.6f} "
                f"(max=${env['max_cost_usd']:.6f}, spent=${env['spent']:.6f}, "
                f"outstanding=${env['outstanding']:.6f}). Refusing.")
        raise HarnessError(
            f"{label} ${amount_f:.6f} would exceed phase ceiling ${ceiling:.6f} "
            f"(spent=${env['spent']:.6f}, outstanding=${env['outstanding']:.6f}, "
            f"phase={env['phase']}). Refusing.")
    return amount_f


class SpendGovernor:
    """Enforces the guarantees that make sub-cent runs a *guarantee*, not a hope."""

    def __init__(self, transport, api_key, expect_key_label=None,
                 max_cost=DEFAULT_MAX_COST, byok_prefixes_path=BYOK_PREFIXES_PATH,
                 terminal_reserve=0.0):
        self.transport = transport
        self.api_key = api_key
        self.expect_key_label = expect_key_label
        self.max_cost = finite_number(max_cost, "max_cost", 0.0)
        self.terminal_reserve = finite_number(
            terminal_reserve, "terminal_reserve", 0.0)
        if self.terminal_reserve > self.max_cost:
            raise HarnessError(
                "terminal_reserve must not exceed max_cost")
        self._phase = PHASE_ATTEMPT
        self.spent = 0.0
        self._outstanding = 0.0
        self._reservations = []
        self._cost_by_model = {}
        # Fan-out safety (#10): spend mutations happen from panel threads.
        self._spend_lock = threading.RLock()
        self.key_info = None
        self._pricing_cache = {}
        self._pricing_fetched_at = 0.0
        self._models_fetched_at = 0.0
        self._CATALOG_TTL = 900.0  # refresh the live catalog mid-run every 15 min
        self._models = None
        self._byok_path = byok_prefixes_path
        self._learned_byok = set(load_byok_prefixes(byok_prefixes_path))

    # 4 + 6
    def verify_key(self):
        try:
            info = self.transport.get(OPENROUTER_KEY_URL, self.api_key)
        except Exception as e:
            raise HarnessError(f"could not verify key limit: {e}") from e
        data = info.get("data", {})
        limit = data.get("limit")
        label = data.get("label", "<no label>")
        if limit is None:
            raise HarnessError(
                f"key '{label}' has NO spend limit configured. Refusing to run.")
        remaining = data.get("limit_remaining", 0)
        eprint(f"[OK] using key '{label}', limit=${limit}, remaining=${remaining:.6f} "
               f"(resets: {data.get('limit_reset')})")
        from . import events as _events
        _events.emit("spend_check", lane="key", limit=limit, remaining=remaining,
                     reset=data.get("limit_reset"), expect_label=bool(self.expect_key_label))
        if self.expect_key_label is not None and label != self.expect_key_label:
            # Exact match (audit #9b): a substring match let a wrong-but-
            # similarly-named key through, and echoing labels into errors is
            # needless exposure. Only a binary match/mismatch is reported.
            raise HarnessError(
                "resolved key label does not exactly match --expect-key-label. "
                "Refusing to run -- fix the key source, or drop --expect-key-label "
                "if this key is actually correct.")
        self.key_info = {
            "label": label, "limit": limit, "remaining": remaining,
            "limit_reset": data.get("limit_reset"),
        }
        return self.key_info

    def key_status(self):
        if not self.key_info:
            self.verify_key()
        return dict(self.key_info, session_spent=self.spent)

    def set_phase(self, phase):
        """Set the dual-budget phase: attempt (default) or terminal.

        Terminal unlocks spending into ``terminal_reserve``. Mission packs
        additionally require the pack itself to be terminal before they
        record findings spend (mission_record owns that gate).
        """
        self._phase = normalize_phase(phase)
        return self._phase

    @property
    def phase(self):
        return self._phase

    def dual_envelope(self):
        """Honest dual-budget snapshot from the one formula owner."""
        with self._spend_lock:
            return dual_budget_envelope(
                self.max_cost, self.spent, self.terminal_reserve,
                phase=self._phase, outstanding=self._outstanding)

    def working_remaining(self):
        """Attempt budget still available — reserve never included."""
        with self._spend_lock:
            return working_remaining(
                self.max_cost, self.spent + self._outstanding,
                self.terminal_reserve)

    def _phase_ceiling(self):
        """Dollar ceiling the current phase may spend up to."""
        with self._spend_lock:
            if self._phase == PHASE_TERMINAL:
                return float(self.max_cost)
            return max(0.0, float(self.max_cost) - float(self.terminal_reserve))

    def snapshot(self):
        """Return the synchronized spend state for result envelopes.

        Dual-envelope fields appear only when a terminal reserve or a
        non-default phase is in force, so non-mission runs keep the
        historical spent/outstanding/ceiling shape.
        """
        with self._spend_lock:
            out = {
                "spent": round(float(self.spent), 6),
                "outstanding": round(float(self._outstanding), 6),
                "ceiling": round(float(self.max_cost), 6),
            }
            if self.terminal_reserve != 0.0 or self._phase != PHASE_ATTEMPT:
                env = dual_budget_envelope(
                    self.max_cost, self.spent, self.terminal_reserve,
                    phase=self._phase, outstanding=self._outstanding)
                out["terminal_reserve"] = round(env["terminal_reserve_cost_usd"], 6)
                out["working_remaining"] = round(env["working_remaining"], 6)
                out["phase"] = env["phase"]
                out["phase_remaining"] = round(env["phase_remaining"], 6)
            return out

    def cost_by_model(self):
        """Actual session spend per model label, for per-run cost reports."""
        return dict(sorted(self._cost_by_model.items(), key=lambda kv: -kv[1]))

    # 3
    def check_byok(self, model_id):
        """Hard block: raises for prefixes that must NEVER run (P0)."""
        for prefix in BYOK_DENYLIST_PREFIXES:
            if model_id.startswith(prefix):
                raise HarnessError(
                    f"model '{model_id}' matches BYOK denylist prefix '{prefix}'. Refusing.")

    def learned_blocked(self, model_id):
        """True if this org-prefix was observed routing via BYOK on this account."""
        return any(model_id.startswith(p) for p in self._learned_byok)

    def record_byok(self, model_id):
        """Persist an observed BYOK org-prefix so future runs skip it."""
        prefix = model_id.split("/", 1)[0] + "/"
        self._learned_byok.add(prefix)
        try:
            save_byok_prefixes(self._byok_path, self._learned_byok)
        except OSError:
            pass

    def is_free(self, model_id):
        """True if the model's per-token pricing is zero (no spend to leak)."""
        pp, cp = self.fetch_pricing([model_id])[model_id]
        return pp == 0.0 and cp == 0.0

    # 1
    def assert_no_tools(self, payload, label):
        if "tools" in payload:
            raise HarnessError(
                f"payload for {label} contains a 'tools' key -- refusing to send.")

    # 2
    def fetch_pricing(self, model_ids):
        missing = [m_ for m_ in model_ids if m_ not in self._pricing_cache
                   or time.time() - self._pricing_fetched_at > self._CATALOG_TTL]
        if missing:
            models = self.fetch_models(refresh=True)
            by_id = {m_["id"]: m_ for m_ in models}
            for mid in missing:
                if mid not in by_id:
                    raise HarnessError(f"model '{mid}' not found in live OpenRouter model list.")
                p = by_id[mid].get("pricing", {})
                try:
                    # OpenRouter pricing fields are already PER-TOKEN dollar prices
                    # (e.g. "0.00000001" = $0.01/M). Do NOT divide by 1e6 again —
                    # an earlier version did and undercounted cost ~1,000,000x.
                    prompt_price = finite_number(p.get("prompt", "0"),
                                                   f"pricing.prompt for '{mid}'", 0.0)
                    completion_price = finite_number(p.get("completion", "0"),
                                                      f"pricing.completion for '{mid}'", 0.0)
                    self._pricing_cache[mid] = (prompt_price, completion_price)
                except (TypeError, ValueError):
                    raise HarnessError(f"could not parse pricing for '{mid}': {p}") from None
            self._pricing_fetched_at = time.time()
        return {m_: self._pricing_cache[m_] for m_ in model_ids}

    def fetch_models(self, refresh=False):
        """Live GET /models, cached per governor instance. The cache expires
        after _CATALOG_TTL so a long run picks up pricing/pool changes
        mid-run instead of trusting a stale snapshot forever (#18)."""
        stale = (self._models is None
                 or time.time() - self._models_fetched_at > self._CATALOG_TTL)
        if refresh or stale:
            try:
                self._models = self.transport.get(OPENROUTER_MODELS_URL, self.api_key,
                                                  timeout=20).get("data", [])
                self._models_fetched_at = time.time()
            except Exception as e:
                if self._models is not None:
                    eprint(f"[warn] model list refresh failed, using cached catalog: {e}")
                else:
                    raise HarnessError(f"could not fetch model list: {e}") from e
        return self._models

    def preflight_jev(self, input_tokens, label="jev"):
        """Preflight a TypeSafe call at its fixed input-token price.

        Dual-budget aware: attempt-phase preflight never spends into
        terminal_reserve; terminal phase may use the full remaining ceiling.
        """
        from .jev import jev_cost
        worst = jev_cost(input_tokens)
        with self._spend_lock:
            ceiling = self._phase_ceiling()
            if self.spent + self._outstanding + worst > ceiling:
                raise HarnessError(
                    f"Jev worst-case ${worst:.6f} for {label} exceeds remaining "
                    f"budget ${self.remaining():.6f} "
                    f"(phase={self._phase}, ceiling=${ceiling:.6f}); refusing.")
        return worst

    def preflight(self, prompt_text, calls):
        """calls = [(label, model, max_tokens, extra_input)] -> (total, breakdown).

        Worst-case: every call maxes its max_tokens. extra_input accounts for
        tokens a later call consumes beyond the base prompt (e.g. the judge
        reading panel outputs). The estimate is checked against the *remaining*
        phase ceiling (attempt ceiling excludes terminal_reserve), so a later
        rotation cannot spend through an earlier call's budget or into the
        terminal reserve. Callers should include every bounded replacement they
        may try in ``calls``.
        """
        if not calls:
            # Pre-guard kept for clarity: an empty call list costs nothing.
            return 0.0, []
        models = [m_ for _, m_, _, _ in calls]
        pricing = self.fetch_pricing(models)
        prompt_tokens = estimate_prompt_tokens(prompt_text)
        total = 0.0
        breakdown = []
        for label, model, max_tokens, extra in calls:
            pp, cp = pricing[model]
            cost = (prompt_tokens + extra) * pp + max_tokens * cp
            breakdown.append((label, model, cost))
            total += cost
        with self._spend_lock:
            ceiling = self._phase_ceiling()
            if self.spent + self._outstanding + total > ceiling:
                if (self._phase == PHASE_ATTEMPT
                        and self.terminal_reserve > 0.0):
                    raise HarnessError(
                        f"worst-case estimate ${total:.6f} would eat "
                        f"terminal_reserve ${self.terminal_reserve:.6f}; "
                        f"attempt working remaining is ${self.remaining():.6f}. "
                        f"Refusing.")
                raise HarnessError(
                    f"worst-case estimate ${self.spent + self._outstanding + total:.6f} "
                    f"exceeds remaining ceiling ${ceiling:.6f} "
                    f"(outstanding reservations: ${self._outstanding:.6f}; "
                    f"phase={self._phase}). Refusing.")
        return total, breakdown

    # 4b
    def reserve(self, amount, label):
        """Atomically reserve worst-case spend for an in-flight call.

        Parallel dispatch (the DAG executor) cannot rely on per-call
        preflight alone: W workers can each preflight against the full
        remaining ceiling before any of them records, and the billed sum
        overspends. A reservation is real liability: ``spent +
        outstanding + new`` must stay under the ceiling or the reserve is
        refused, and every preflight sees outstanding reservations.
        Returns a reservation token for :meth:`reconcile`.
        """
        amount = finite_number(amount or 0.0, "reservation", 0.0)
        with self._spend_lock:
            ceiling = self._phase_ceiling()
            if self.spent + self._outstanding + amount > ceiling:
                if (self._phase == PHASE_ATTEMPT
                        and self.terminal_reserve > 0.0):
                    raise HarnessError(
                        f"reservation ${amount:.6f} ({label}) would eat "
                        f"terminal_reserve ${self.terminal_reserve:.6f}; "
                        f"attempt working remaining is ${self.remaining():.6f}. "
                        f"Refusing.")
                raise HarnessError(
                    f"reservation ${amount:.6f} would put spent+outstanding at "
                    f"${self.spent + self._outstanding + amount:.6f}, over ceiling "
                    f"${ceiling:.6f} "
                    f"(${self.remaining():.6f} still unreserved; "
                    f"phase={self._phase}). Refusing.")
            self._outstanding += amount
            token = (label, amount)
            self._reservations.append(token)
            return token

    def reconcile(self, token, actual):
        """Settle a reservation: release the worst-case liability, record
        the billed actual. The reservation is released even when ``actual``
        would fail the ceiling check (the liability was already counted)."""
        with self._spend_lock:
            try:
                self._reservations.remove(token)
            except ValueError:
                raise HarnessError(
                    "reconcile of an unknown reservation token") from None
            self._outstanding = max(0.0, self._outstanding - token[1])
        label = token[0]
        try:
            actual_f = finite_number(actual or 0.0, "reported cost", 0.0)
        except HarnessError:
            raise HarnessError(f"invalid reported cost {actual!r} (after '{label}').") from None
        with self._spend_lock:
            ceiling = self._phase_ceiling()
            if self.spent + actual_f > ceiling:
                if (self._phase == PHASE_ATTEMPT
                        and self.terminal_reserve > 0.0):
                    raise HarnessError(
                        f"actual running cost ${self.spent + actual_f:.6f} would "
                        f"exceed ceiling ${ceiling:.6f} by eating terminal_reserve "
                        f"${self.terminal_reserve:.6f} (after '{label}'). Aborting.")
                raise HarnessError(
                    f"actual running cost ${self.spent + actual_f:.6f} would exceed ceiling "
                    f"${ceiling:.6f} (after '{label}'; phase={self._phase}). Aborting.")
            self.spent += actual_f
            self._cost_by_model[label] = (self._cost_by_model.get(label, 0.0) + actual_f)

    @property
    def outstanding(self):
        """Worst-case liability currently reserved by in-flight calls."""
        return self._outstanding

    def remaining(self):
        """The budget this run can still commit under the current phase, in dollars.

        ``spent`` and outstanding reservations both count, because a
        reservation is real liability. Attempt phase additionally excludes
        ``terminal_reserve`` (dual envelope); terminal phase may still spend
        the reserve. This is the ONE accessor for "what can this run still
        afford", so a lane that bounds a dispatch by the remaining budget
        (the DAG reserver) does not re-derive the arithmetic from three
        public fields.
        """
        with self._spend_lock:
            ceiling = self._phase_ceiling()
            return max(0.0, ceiling - (self.spent + self._outstanding))

    # 5
    def record_actual(self, cost, label):
        """Record a billable response without ever moving ``spent`` over the
        phase ceiling (attempt ceiling excludes terminal_reserve)."""
        try:
            actual = finite_number(cost or 0.0, "reported cost", 0.0)
        except HarnessError:
            raise HarnessError(f"invalid reported cost {cost!r} (after '{label}').") from None
        with self._spend_lock:
            ceiling = self._phase_ceiling()
            if self.spent + self._outstanding + actual > ceiling:
                if (self._phase == PHASE_ATTEMPT
                        and self.terminal_reserve > 0.0):
                    raise HarnessError(
                        f"actual running cost ${self.spent + self._outstanding + actual:.6f} "
                        f"would exceed ceiling ${ceiling:.6f} by eating terminal_reserve "
                        f"${self.terminal_reserve:.6f} (after '{label}'). Aborting.")
                raise HarnessError(
                    f"actual running cost ${self.spent + self._outstanding + actual:.6f} "
                    f"would exceed ceiling ${ceiling:.6f} "
                    f"(outstanding reservations: ${self._outstanding:.6f}; "
                    f"after '{label}'; phase={self._phase}). Aborting.")
            self.spent += actual
            self._cost_by_model[label] = (self._cost_by_model.get(label, 0.0) + actual)


# ------------------------- live discovery -------------------------

class NodeReserver:
    """Cost-liability seam for parallel DAG dispatch (MR-6 verdict): each
    node's worst case is reserved before dispatch and reconciled against the
    billed actual, so W concurrent preflights can never overspend the shared
    ceiling.

    The amount is the node's OWN worst case, read from its plan route:

    * a declared route ceiling is used verbatim -- including a $0.00
      free-tier ceiling, which is a real ceiling (every rung on that ladder
      bills $0.00). Passing $0.00 as a request's ``task_max_cost`` is
      deliberately suppressed by :func:`harness.waist.node_apply_kwargs`
      (a zero task budget would refuse the escalation ladder), so reading
      the route's declared ceiling *here* is what keeps a free node
      reserving $0.00 instead of the nominal default;
    * a node with no declared ceiling falls back to ``default_amount``,
      itself bounded by ``run_ceiling`` -- the budget the lane is really
      running under. An unrelated default ($0.10) larger than the run
      ceiling ($0.05) used to refuse every node before any work, free nodes
      included: a fallback is only ever a bound, so it is capped by the
      run's own ceiling rather than trusted as a cost.
    * the reservation is never more than what the run can still afford, and
      an amount larger than that remaining budget is REFUSED rather than
      trimmed to fit: trimming would let a call that may bill its full
      ceiling dispatch while only part of it is reserved, and the overspend
      would surface at reconcile time -- after the money was spent.
      Fail-closed means refuse before dispatch (MR-6).
    """

    def __init__(self, governor, node_routes, default_amount,
                 route_kwargs_fn=None, run_ceiling=None):
        self.governor = governor
        self.node_routes = node_routes or {}
        self._route_kwargs_fn = route_kwargs_fn
        ceiling = run_ceiling
        if ceiling is None:
            ceiling = getattr(governor, "max_cost", None)
        ceiling = _dollar_amount(ceiling)
        fallback = _dollar_amount(default_amount)
        if fallback is not None and ceiling is not None:
            fallback = min(fallback, ceiling)
        self.run_ceiling = ceiling
        self.default_amount = fallback

    def declared_ceiling(self, node):
        """This node's own worst case from its plan route, or None.

        A route's cost ceiling is the tier ceiling the planner computed, so
        $0.00 is returned as $0.00 (free tier) and is NOT confused with
        "unknown". Routes the plan never classified (or a lane running an
        unplanned node) return None and fall back to ``default_amount``.
        """
        detail = self.node_routes.get(node.node_id)
        route = detail.get("route") if isinstance(detail, dict) else None
        if isinstance(route, dict):
            declared = _dollar_amount(route.get("cost_ceiling"))
            if declared is not None:
                return declared
        if self._route_kwargs_fn is not None:
            kwargs = self._route_kwargs_fn(detail) or {}
            if "task_max_cost" in kwargs:
                return _dollar_amount(kwargs.get("task_max_cost"))
        return None

    def reserve(self, node):
        amount = self.declared_ceiling(node)
        reason = "planned route ceiling"
        if amount is None:
            amount = self.default_amount
            reason = "fallback bound"
        if amount is None:
            # No route and no usable nominal bound (a test fake engine): the
            # reservation is zero and the governor's own ceiling check at
            # preflight/reconcile stays the gate.
            amount = 0.0
        remaining = _dollar_amount(self.remaining())
        if remaining is not None and amount > remaining:
            ceiling = "?" if self.run_ceiling is None else f"${self.run_ceiling:.6f}"
            raise HarnessError(
                f"node '{node.node_id}' worst case ${amount:.6f} ({reason}) does not "
                f"fit the ${remaining:.6f} this run can still afford (ceiling "
                f"{ceiling}). Refusing to dispatch it.")
        return self.governor.reserve(amount, node.node_id)

    def remaining(self):
        """What this run can still commit, or None if the governor cannot say."""
        remaining = getattr(self.governor, "remaining", None)
        if callable(remaining):
            return remaining()
        return None

    def reconcile(self, token, actual):
        self.governor.reconcile(token, actual)


def _dollar_amount(value):
    """``value`` as a non-negative finite dollar amount, or None.

    Advisory boundary: a caller (or a test fake) may hand over something
    that is not a number at all, which is "unknown", never "zero" and never
    a crash -- the governor's ceiling check remains the gate either way.
    """
    try:
        return finite_number(value, "amount", 0.0)
    except HarnessError:
        return None


def _is_free_model(m):
    mid = m.get("id", "")
    p = m.get("pricing", {})
    return mid.endswith(":free") or (str(p.get("prompt")) == "0" and str(p.get("completion")) == "0")


def discover_free_models(transport, api_key, prefer=None, limit=40):
    """Return the ordered list of free model ids, curated preference first.

    prefer is an ordered list of ids to rank highest; other free models are
    appended after. Any prefer entry that is no longer free/existing is
    dropped. Sorted by name for a stable tail.
    """
    gov = SpendGovernor(transport, api_key)
    models = gov.fetch_models()
    free = [m_["id"] for m_ in models if _is_free_model(m_)]
    free_set = set(free)
    ordered = []
    for pid in (prefer or []):
        if pid in free_set and pid not in ordered:
            ordered.append(pid)
    for mid in sorted(free):
        if mid not in ordered:
            ordered.append(mid)
    return ordered[:limit]
