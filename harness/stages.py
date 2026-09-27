"""Hourglass stage composition: the ONE owner of WHICH stages run, in what
order, under which allowances (HV-4).

Four optional stages -- ``context``, ``planning``, ``execution``,
``verification`` -- composed from the shared contracts the earlier slices
published, never re-implementing them:

* **context** builds the evidence-bearing brief (``harness.brief``);
* **planning** consumes successively curated briefs under **decreasing**
  explicit token allowances (``harness.token_budget``), and stops on a
  sufficient answer or emits a bounded plan / bounded evidence request / an
  honest defer;
* **execution** and **verification** are *resolved* here and reported, not
  performed: dispatching a work package is ``HV-5``'s contract, and
  verification is independent completion authority. A selected stage this
  slice does not run is reported ``pending`` rather than quietly absent.

Three rules the contract states, implemented literally:

1. **Planning cannot raise its own limits.** Every round's budget is a
   *child* of the one before it, and
   :class:`harness.token_budget.TokenBudget` refuses a child that widens its
   parent. The ladder additionally refuses to continue when a round's
   allowance would not actually shrink.
2. **A supplied brief or plan bypasses the stage it replaces** -- and only
   that stage. A supplied brief bypasses ``context`` only while it is still
   *fresh* (:func:`harness.brief.freshness_report`): planning on evidence
   that drifted from its pin is exactly what the brief's freshness contract
   exists to prevent, so a stale supplied brief buys no bypass and the
   context is rebuilt. A supplied plan bypasses ``planning`` and implies
   nothing else -- it does not make execution safe or verification optional.
3. **Sufficiency is judged where the evidence is.** The code-owned bound is
   a fact about the artifact (unrepresented sources, declared conflicts, a
   failed grounding lint). The *semantic* question -- is this evidence enough
   to plan on -- goes to the one declared dimension that owns it
   (``plan_soundness``) when a Jev policy is supplied. No policy means the
   code bound decides; a Jev call that fails or falls back is non-native,
   which the fail-closed direction reads as "not sufficient": an evidence
   request, not a plan built on evidence nobody vouched for. A Jev judgment
   can never paper over a failed grounding lint.

Hermetic by construction: no network, no key, no model. Anything that could
reach a provider arrives as an injected callable.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .brief import (
    MAX_TOTAL_WINDOW_CHARS,
    build_brief,
    estimate_brief_tokens,
    freshness_report,
    render_brief,
    validate_brief,
)
from .errors import HarnessError
from .token_budget import DEFAULT_RUN_INPUT_TOKENS, TokenBudget

#: The declared stage ladder, in the only order it may run.
STAGE_ORDER = ("context", "planning", "execution", "verification")
_STAGE_SET = frozenset(STAGE_ORDER)

#: Per-stage states a composition may report.
STATE_COMPLETED = "completed"
STATE_SKIPPED = "skipped"
STATE_PENDING = "pending"
STAGE_STATES = (STATE_COMPLETED, STATE_SKIPPED, STATE_PENDING)

#: Outcome kinds the planning stage may emit. Exactly one per run.
PLANNING_SUFFICIENT = "sufficient"
PLANNING_PLAN = "plan"
PLANNING_EVIDENCE_REQUEST = "evidence_request"
PLANNING_DEFER = "defer"
PLANNING_OUTCOMES = (PLANNING_SUFFICIENT, PLANNING_PLAN,
                     PLANNING_EVIDENCE_REQUEST, PLANNING_DEFER)

#: Each round keeps this share of the previous round's input allowance. Below
#: 1.0 by construction: the ladder narrows, it never widens.
PLANNING_SHRINK = 0.5

#: A round below this many input tokens cannot ask a model anything useful, so
#: the ladder stops before it spends a reservation on a truncated request.
MIN_ROUND_TOKENS = 1024

#: Bounds the plan and the evidence request a planning stage may emit. Both
#: are bounded because the number is declared here, not because a prompt
#: asked nicely.
DEFAULT_MAX_PLAN_NODES = 12
DEFAULT_MAX_EVIDENCE_ITEMS = 5

#: Code-owned stopping rule, not a model judgment. Jev answers "is more
#: evidence required?"; code decides what counts as yes. A request is taken
#: at or above this confidence and ignored below it, so a borderline call
#: ends the ladder instead of buying another round of curation.
SUFFICIENCY_NOUL_THRESHOLD = 0.5

#: How narrow the window search may get, in characters. A brief's size is
#: monotone in its window budget, so a bounded bisection finds the largest
#: window that fits; the cap keeps the rebuild count finite and cheap (every
#: step is a hermetic re-read, never a model call).
_WINDOW_SEARCH_FLOOR = 256


# ---------------------------------------------------------------- stages


@dataclass(frozen=True)
class StageSpec:
    """One resolved stage: in this run, or not -- and if not, why.

    ``denied_bypass`` records the case where a caller *supplied* the input
    that would have skipped this stage and the supply was refused -- a stale
    brief, for instance. The stage then runs, and the caller can see why its
    supplied artifact was not used.
    """

    name: str
    selected: bool
    skip_reason: Optional[str] = None
    denied_bypass: Optional[str] = None

    def __post_init__(self):
        if self.name not in _STAGE_SET:
            raise HarnessError(
                f"unknown Hourglass stage {self.name!r}; declared stages are "
                f"{list(STAGE_ORDER)}")
        if self.selected and self.skip_reason:
            raise HarnessError(
                f"stage {self.name!r} cannot be both selected and skipped")
        if not self.selected and not self.skip_reason:
            raise HarnessError(
                f"stage {self.name!r} is not selected and carries no reason; a "
                f"skip must say why")
        if self.denied_bypass and not self.selected:
            raise HarnessError(
                f"stage {self.name!r} was skipped, so no bypass was denied to "
                f"it; denied_bypass belongs to a stage that still runs")

    def to_dict(self):
        return {"stage": self.name, "selected": self.selected,
                "skip_reason": self.skip_reason,
                "denied_bypass": self.denied_bypass}


@dataclass(frozen=True)
class StagePlan:
    """The validated composition contract for one run.

    ``states`` records what happened to each stage: ``completed``, ``skipped``
    (the reason lives on its :class:`StageSpec`), or ``pending`` -- selected,
    but performed by a later slice (``HV-5`` dispatches execution, ``HV-6``
    surfaces the result).
    """

    goal: str
    specs: Tuple[StageSpec, ...]
    states: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if not str(self.goal or "").strip():
            raise HarnessError("a stage plan requires a goal")
        names = [spec.name for spec in self.specs]
        if len(set(names)) != len(names):
            raise HarnessError("a stage may appear at most once in a plan")
        # No name check here: StageSpec refuses an undeclared name in its own
        # __post_init__, so a spec that reached this point is already valid.
        for name, state in self.states.items():
            if name not in names:
                raise HarnessError(
                    f"state recorded for undeclared stage {name!r}")
            if state not in STAGE_STATES:
                raise HarnessError(
                    f"stage {name!r} has unknown state {state!r}; declared "
                    f"states are {list(STAGE_STATES)}")

    def selected(self) -> Tuple[str, ...]:
        return tuple(spec.name for spec in self.specs if spec.selected)

    def skipped(self) -> Tuple[str, ...]:
        return tuple(spec.name for spec in self.specs if not spec.selected)

    def spec(self, name) -> Optional[StageSpec]:
        for item in self.specs:
            if item.name == name:
                return item
        return None
    def state(self, name) -> Optional[str]:
        return self.states.get(name)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "goal": self.goal,
            "stages": [dict(spec.to_dict(), state=self.states.get(spec.name,
                                                                 STATE_PENDING))
                       for spec in self.specs],
            "selected": list(self.selected()),
            "skipped": list(self.skipped()),
        }


def _selection(stages):
    """Normalize an explicit selection to a declared-ordered subset."""
    if stages is None:
        return list(STAGE_ORDER)
    if isinstance(stages, str):
        stages = [part.strip() for part in stages.split(",") if part.strip()]
    wanted = []
    for name in stages:
        if name not in _STAGE_SET:
            raise HarnessError(
                f"unknown Hourglass stage {name!r}; declared stages are "
                f"{list(STAGE_ORDER)}")
        if name not in wanted:
            wanted.append(name)
    # Selection is a subset, never a reordering: running a stage before a
    # stage it depends on is a different contract, not a choice.
    return [name for name in STAGE_ORDER if name in wanted]


def resolve_stages(*, goal, stages=None, brief=None, plan=None, reader=None):
    """Resolve which stages this run composes, and record why.

    ``stages`` is the operator's explicit selection: an ordered subset of
    :data:`STAGE_ORDER`. Omitted means the full hourglass, which is the
    posture every lane already runs -- this slice adds opt-out, never
    opt-in.
    """
    chosen = _selection(stages)
    specs = []
    for name in STAGE_ORDER:
        if name not in chosen:
            specs.append(StageSpec(name, False, "not_selected"))
            continue
        skip = None
        denied = None
        if name == "context" and brief is not None:
            if _brief_is_fresh(brief, reader):
                skip = "brief_supplied"
            else:
                # A drifted brief is not a bypass: the context stage runs and
                # rebuilds, and the caller is told its supply was refused.
                denied = "supplied_brief_is_not_fresh"
        elif name == "planning" and plan is not None:
            skip = "plan_supplied"
        specs.append(StageSpec(name, skip is None, skip, denied))
    return StagePlan(goal, tuple(specs))


def _brief_is_fresh(brief, reader=None):
    """A supplied brief buys a context bypass only while it is still fresh."""
    return bool(freshness_report(brief, reader=reader).get("fresh"))


def stage_selection_from_settings(settings, *, default=None) -> List[str]:
    """The configured stage selection (``HARNESS_HOURGLASS_STAGES``).

    The default keeps every stage selected: the hourglass is the product's
    default posture and compatibility requires it to stay on.
    """
    raw = getattr(settings, "hourglass_stages", None)
    if raw in (None, "", []):
        raw = default
    if raw in (None, "", []):
        return list(STAGE_ORDER)
    if isinstance(raw, str):
        return [part.strip() for part in raw.split(",") if part.strip()]
    return list(raw)


# -------------------------------------------------------------- planning


@dataclass(frozen=True)
class PlanningOutcome:
    """The planning stage's single result: one of :data:`PLANNING_OUTCOMES`."""

    kind: str
    reason: Optional[str] = None
    brief: Optional[Dict[str, Any]] = None
    plan: Any = None
    evidence_request: Tuple[Dict[str, Any], ...] = ()
    rounds: Tuple[Dict[str, Any], ...] = ()
    jev_signals: Dict[str, Any] = field(default_factory=dict)
    budget: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.kind not in PLANNING_OUTCOMES:
            raise HarnessError(
                f"planning outcome must be one of {list(PLANNING_OUTCOMES)}; "
                f"got {self.kind!r}")
        object.__setattr__(self, "evidence_request",
                           tuple(self.evidence_request or ()))
        object.__setattr__(self, "rounds", tuple(self.rounds or ()))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "reason": self.reason,
            "brief_tokens": (self.brief or {}).get("estimated_tokens"),
            "plan": self.plan.to_dict() if hasattr(self.plan, "to_dict")
                    else self.plan,
            "evidence_request": [dict(item) for item in self.evidence_request],
            "rounds": [dict(item) for item in self.rounds],
            "jev_signals": dict(self.jev_signals),
            "budget": dict(self.budget),
        }


def planning_ladder(stage_budget, *, rounds=2, shrink=PLANNING_SHRINK):
    """Successively narrower budgets for successive planning rounds.

    Each entry is a child of the previous one, so the ladder is structurally
    unable to widen: :class:`~harness.token_budget.TokenBudget` refuses a
    child whose maxima exceed its parent's. A shrink at or above 1.0 is
    refused outright -- a "decreasing" ladder that may hold still is a budget
    that quietly re-widened -- and the ladder stops as soon as a round would
    fall below :data:`MIN_ROUND_TOKENS`, because a request that small cannot
    be asked anything.
    """
    if not isinstance(stage_budget, TokenBudget):
        raise HarnessError("a planning ladder needs a TokenBudget stage")
    if rounds < 1:
        raise HarnessError("a planning ladder needs at least one round")
    if not 0 < shrink < 1:
        raise HarnessError(
            f"planning shrink must narrow the ladder (0 < shrink < 1); got "
            f"{shrink!r}")
    ladder = [stage_budget]
    while len(ladder) < rounds:
        previous = ladder[-1]
        nxt = int(previous.max_input_tokens * shrink)
        if nxt < MIN_ROUND_TOKENS or nxt >= previous.max_input_tokens:
            break
        ladder.append(previous.stage(
            f"planning-round-{len(ladder) + 1}", max_input_tokens=nxt))
    return ladder


def _curated_brief(goal, files, *, reader, allowance_tokens):
    """The largest brief whose MEASURED token estimate fits the allowance.

    :func:`harness.brief.build_brief` estimates over the bytes it actually
    ships, so the fit is checked against the real number rather than a
    characters-per-token guess. A brief's size is monotone in its window
    budget, so the window is bisected down to the largest value that fits --
    which is the only way to get this right, because the estimate is
    dominated by the pack's own metadata (sources, coverage, rules) rather
    than by the window: a step sized from the token excess leaves the pack
    byte-identical until the window drops below the content, so a larger
    allowance can fail where a smaller one succeeded.

    ``None`` means no honest brief exists at any window -- the round defers
    rather than shipping a brief it cannot pay for.
    """
    def build(window):
        pack = build_brief(goal, files, reader=reader, max_total_chars=window)
        return pack, estimate_brief_tokens(pack)

    smallest, smallest_tokens = build(0)
    if smallest_tokens > allowance_tokens:
        return None, 0
    low, high = 0, MAX_TOTAL_WINDOW_CHARS
    best = smallest
    while high - low > _WINDOW_SEARCH_FLOOR:
        mid = (low + high) // 2
        pack, tokens = build(mid)
        if tokens <= allowance_tokens:
            low, best = mid, pack
        else:
            high = mid
    return best, low


def _unrepresented(pack) -> List[str]:
    """The brief's own admission of what it could not represent."""
    return list(pack.get("omitted") or [])


def _conflicts(pack) -> List[Dict[str, Any]]:
    return [c for c in (pack.get("conflicts") or []) if isinstance(c, dict)]


def _evidence_request(pack, *, max_items=DEFAULT_MAX_EVIDENCE_ITEMS):
    """A BOUNDED request for the evidence the brief says it is missing."""
    items: List[Dict[str, Any]] = [
        {"source": path, "reason": "not represented in the brief"}
        for path in _unrepresented(pack)]
    for conflict in _conflicts(pack):
        items.append({
            "source": ", ".join(str(s) for s in conflict.get("source_ids") or []),
            "reason": str(conflict.get("description") or "declared conflict"),
        })
        if len(items) >= max_items:
            break
    return items[:max_items]


def _jev_sufficiency(jev_policy, goal, pack, *, site):
    """Ask the one declared dimension that owns 'is this enough to plan on?'.

    Returns ``(signals, native)``. A policy that fails, falls back, or
    answers out of vocabulary yields ``native=False`` with whatever signals it
    produced, and the caller reads that as "not sufficient". Composition never
    re-implements the judgment and never promotes a fallback to native.

    The signal names come from the pack that declared them, not from a local
    list, so a new dimension's vocabulary cannot drift from this reader.
    """
    from .jev_packs import HOURGLASS_STAGE_DIMENSIONS
    state = {
        "goal": goal,
        "represented_sources": len(
            ((pack.get("grounding") or {}).get("sources") or [])),
        "omitted": _unrepresented(pack),
        "conflicts": [c.get("description") for c in _conflicts(pack)],
        "brief_render": render_brief(pack),
    }
    _result, structural = jev_policy.evaluate_hourglass_stage(
        "plan_soundness", state, site=site)
    structural = structural or {}
    declared = HOURGLASS_STAGE_DIMENSIONS["plan_soundness"]["signals"]
    signals = {name: structural.get(name) for name in declared
               if structural.get(name) is not None}
    return signals, bool(structural.get("native"))


def _validated_plan(plan_data, *, max_nodes=DEFAULT_MAX_PLAN_NODES):
    """A plan is accepted only if it validates AND stays inside its bound."""
    from .dag import TaskDAG  # local: keeps this module importable without the DAG
    dag = plan_data if isinstance(plan_data, TaskDAG) else TaskDAG.from_dict(
        plan_data)
    count = len(dag.nodes)
    if count == 0:
        raise HarnessError("a plan must declare at least one node")
    if count > max_nodes:
        raise HarnessError(
            f"plan declares {count} node(s), over the bound of {max_nodes}")
    return dag


def run_planning(*, goal, budget, files=(), reader=None, brief=None,
                 jev_policy=None, planner=None, rounds=2,
                 max_nodes=DEFAULT_MAX_PLAN_NODES,
                 max_evidence_items=DEFAULT_MAX_EVIDENCE_ITEMS,
                 site="stages-planning"):
    """Run the planning stage: curate, judge sufficiency, and stop honestly.

    One round is one allowance, and the round's allowance is what its brief
    must fit inside. The ladder either finds the evidence sufficient (and
    stops there, spending nothing further), asks for a bounded piece of
    evidence, or -- when a ``planner`` is supplied -- emits a validated
    bounded plan. When it is exhausted the outcome is an honest ``defer``
    naming what was still missing, never a plan built on evidence the stage
    admitted it could not represent.
    """
    if jev_policy is not None and not hasattr(jev_policy,
                                               "evaluate_hourglass_stage"):
        raise HarnessError(
            "jev_policy must be a JevPolicy-like owner exposing "
            "evaluate_hourglass_stage")
    if planner is not None and not callable(planner):
        raise HarnessError("planner must be callable")
    if not isinstance(budget, TokenBudget):
        raise HarnessError("planning needs a TokenBudget to spend from")

    stage_budget = budget.stage("planning")
    ladder = planning_ladder(stage_budget, rounds=rounds)
    round_log: List[Dict[str, Any]] = []
    jev_signals: Dict[str, Any] = {}
    pack = brief

    for index, round_budget in enumerate(ladder, start=1):
        allowance = round_budget.max_input_tokens
        if index > 1 and not files:
            # Nothing left to curate. A second round would judge the IDENTICAL
            # brief again, and a judgment that cannot change its answer must
            # not be paid for -- so the ladder stops here and says why.
            round_log.append({"round": index, "outcome": STATE_SKIPPED,
                              "reason": "nothing_left_to_curate",
                              "max_input_tokens": allowance})
            break
        if pack is None or index > 1:
            # Round one reads a supplied brief as data; every round after the
            # first curates its own, because a narrower allowance has to buy a
            # narrower brief. Curating reads the sources, so a caller that
            # supplies both a brief and unreadable files gets the reader's own
            # error rather than a silently different evidence set.
            pack, _ = _curated_brief(goal, files, reader=reader,
                                     allowance_tokens=allowance)
        if pack is None:
            round_log.append({"round": index, "outcome": STATE_SKIPPED,
                              "reason": "brief_exceeds_allowance",
                              "max_input_tokens": allowance})
            # A round with no honest brief is not a stopping point by itself:
            # a narrower round may still fit. The ladder decides.
            continue

        issues = validate_brief(pack, reader=reader)
        missing = _unrepresented(pack)
        conflicts = _conflicts(pack)
        sources = len(((pack.get("grounding") or {}).get("sources") or []))
        lint_clean = not issues
        # A brief citing no source is not evidence of anything: without this
        # bound an empty pack reads as a clean, gapless, conflictless brief and
        # planning would stop on it.
        sufficient = lint_clean and sources > 0 and not missing and not conflicts
        native = False
        # The semantic question is only asked when the artifact's own facts
        # are inconclusive, a failed lint always wins over a Jev "yes", and a
        # brief citing nothing is not asked about at all -- that question has
        # no answer worth a reservation.
        if lint_clean and sources > 0 and jev_policy is not None \
                and not sufficient:
            try:
                jev_signals, native = _jev_sufficiency(jev_policy, goal, pack,
                                                      site=site)
            except HarnessError:
                jev_signals, native = {}, False
            if native and sources > 0:
                requested = jev_signals.get("plan_evidence_requested")
                # The semantic answer is Jev's; what counts as "yes, ask for
                # more" is code's. A missing signal is not a quiet yes.
                if isinstance(requested, (int, float)) \
                        and not isinstance(requested, bool) \
                        and requested < SUFFICIENCY_NOUL_THRESHOLD:
                    sufficient = True
        round_log.append({
            "round": index,
            "outcome": (PLANNING_SUFFICIENT if sufficient
                        else PLANNING_EVIDENCE_REQUEST),
            "max_input_tokens": allowance,
            "brief_tokens": pack.get("estimated_tokens"),
            "sources": sources,
            "omitted": len(missing),
            "conflicts": len(conflicts),
            "grounding_issues": len(issues),
            "jev_native": native,
        })
        if sufficient:
            return PlanningOutcome(
                PLANNING_SUFFICIENT, reason="brief_covers_the_request",
                brief=pack, rounds=round_log, jev_signals=jev_signals,
                budget=stage_budget.snapshot())
        # Insufficient: the ladder's next round is NARROWER, so it re-curates
        # a smaller brief and asks again. That is the whole point of the
        # ladder -- each round buys less evidence than the last, and the stage
        # stops when what is left is not enough rather than looping.

    if planner is not None and pack is not None:
        try:
            dag = _validated_plan(planner(goal, pack), max_nodes=max_nodes)
        except HarnessError as exc:
            return PlanningOutcome(
                PLANNING_DEFER, reason=f"plan_rejected: {exc}", brief=pack,
                rounds=round_log, jev_signals=jev_signals,
                budget=stage_budget.snapshot())
        return PlanningOutcome(
            PLANNING_PLAN, reason="validated_bounded_plan", brief=pack,
            plan=dag, rounds=round_log, jev_signals=jev_signals,
            budget=stage_budget.snapshot())

    request = _evidence_request(pack, max_items=max_evidence_items) \
        if pack is not None else []
    if request:
        reason = "bounded_evidence_request"
    elif pack is None:
        reason = "no_brief_fit_any_round"
    elif not ((pack.get("grounding") or {}).get("sources") or []):
        reason = "no_evidence_cited"
    else:
        reason = "nothing_left_to_request"
    return PlanningOutcome(
        PLANNING_EVIDENCE_REQUEST if request else PLANNING_DEFER,
        reason=reason, brief=pack, evidence_request=request, rounds=round_log,
        jev_signals=jev_signals, budget=stage_budget.snapshot())


# ------------------------------------------------------------- composition


@dataclass(frozen=True)
class Composition:
    """What one composed run resolved, decided, and left pending."""

    plan: StagePlan
    planning: Optional[PlanningOutcome] = None
    brief: Optional[Dict[str, Any]] = None
    budget: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan": self.plan.to_dict(),
            "planning": self.planning.to_dict() if self.planning else None,
            "brief_tokens": (self.brief or {}).get("estimated_tokens"),
            "budget": dict(self.budget),
        }


def compose_stages(*, goal, budget=None, stages=None, files=(), reader=None,
                   brief=None, plan=None, jev_policy=None, planner=None,
                   rounds=2, site="stages-planning") -> Composition:
    """Compose the selected stages for one run and report what each did.

    ``context`` is completed only when this call actually built the brief: a
    supplied brief is the caller's evidence, so the bypass is recorded on the
    plan rather than re-derived here. ``planning`` runs. ``execution`` and
    ``verification`` are reported ``pending`` when selected -- dispatching a
    work package belongs to ``HV-5``, and verification is independent
    completion authority -- because a stage that is selected and silently
    absent is the one thing a composition must never be.
    """
    if budget is None:
        budget = TokenBudget("run", max_input_tokens=DEFAULT_RUN_INPUT_TOKENS,
                             max_output_tokens=0)
    stage_plan = resolve_stages(goal=goal, stages=stages, brief=brief,
                                plan=plan, reader=reader)
    # Every stage reports a state, so a stage that is not selected is visibly
    # skipped rather than simply absent from the envelope.
    states = {name: (STATE_PENDING if spec.selected else STATE_SKIPPED)
              for name, spec in ((s.name, s) for s in stage_plan.specs)}
    built = None

    context = stage_plan.spec("context")
    if context.selected:
        built, _ = _curated_brief(goal, files, reader=reader,
                                   allowance_tokens=budget.max_input_tokens)
        states["context"] = STATE_COMPLETED if built is not None \
            else STATE_SKIPPED
    else:
        states["context"] = STATE_SKIPPED
        built = brief

    outcome = None
    if stage_plan.spec("planning").selected:
        outcome = run_planning(goal=goal, budget=budget, files=files,
                               reader=reader, brief=built,
                               jev_policy=jev_policy, planner=planner,
                               rounds=rounds, site=site)
        states["planning"] = STATE_COMPLETED
    else:
        states["planning"] = STATE_SKIPPED

    return Composition(plan=StagePlan(stage_plan.goal, stage_plan.specs,
                                      states),
                       planning=outcome, brief=built,
                       budget=budget.snapshot())
