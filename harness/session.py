"""Composition owner: how a run gets its dependencies.

Every interface command (and the MCP server) needs the same objects -- an
API key + spend governor, the autonomy ledger, the model router, and the
apply engine configured with the settings' tier policy. This module is the
ONE place that wires them, so a new engine kwarg or a tier-policy change
lands here exactly once. The history that motivates it: round-1 dogfooding
caught an engine built without its api_key, and the MCP copy of the
construction block missed the use_free threading when the CLI got it.

Direction of dependency: interfaces import session; session imports engines
and primitives; nothing here imports interfaces.
"""
from ._http import HttpTransport
from .apply import ApplyEngine
from .jev_policy import JevPolicy, policy_for
from .ledger import AutonomyLedger
from .router import Router
from .saturation import pre_run_warning
from .spend import SpendGovernor
from .config import HARD_MAX_COST, resolve_api_key
from .errors import HarnessError
from .validation import finite_number


def governor_for(settings, max_cost_override=None):
    """Resolve the API key and verify it against OpenRouter, returning
    ``(api_key, governor)``. The key is threaded to every engine-routed
    chat call by engine_for -- regression-pinned (the round-1 401 failure)."""
    api_key = resolve_api_key()
    if not api_key:
        raise HarnessError(
            "no OpenRouter API key found (OPENROUTER_API_KEY env, "
            "~/.config/scmorc/openrouter*.env, or ~/.config/harness/openrouter.env).")
    if max_cost_override is None:
        max_cost = settings.max_cost
    else:
        # The CLI override is untrusted input like any other: it is capped
        # at the same HARD_MAX_COST the settings path enforces, so an
        # explicit --max-cost can never raise the ceiling past hard.
        max_cost = finite_number(max_cost_override, "max_cost", 0.0,
                                 HARD_MAX_COST)
    gov = SpendGovernor(HttpTransport(), api_key, settings.expect_key_label,
                        max_cost)
    gov.verify_key()
    return api_key, gov


def jev_face_governor(settings, max_cost_override=None):
    """Spend governor for the Jev-only CLI faces (issue-sort, route,
    log-judgment).

    Keyed Jev dispatch always requires the shared spend governor
    (``JevPolicy._preflight`` refuses to reserve against nothing), so these
    faces must compose one or they silently degrade to keyword fallback on a
    keyed machine -- the MCP server and the web UI already pass their session
    governor. Same ceiling rule as :func:`governor_for`: ``settings.max_cost``
    by default, and an explicit override above ``HARD_MAX_COST`` is refused
    fail-closed (never silently clamped), but no OpenRouter key
    verification: these faces dispatch TypeSafe Jev calls only, and
    ``SpendGovernor.reserve``/``reconcile`` are pure USD arithmetic. Returns
    ``None`` when no Jev key resolves -- unkeyed runs never preflight, so they
    need no governor and must keep their hermetic behavior.
    """
    if not getattr(settings, "jev_api_key", None):
        return None
    if max_cost_override is None:
        max_cost = settings.max_cost
    else:
        max_cost = finite_number(max_cost_override, "max_cost", 0.0,
                                 HARD_MAX_COST)
    return SpendGovernor(HttpTransport(), resolve_api_key(),
                         settings.expect_key_label, max_cost)


def ledger_for(settings, caller="cli"):
    """The run's autonomy ledger (hash-chained JSONL at the configured path).

    caller tags every appended event for per-caller trust attribution
    ("cli" for CLI runs, "mcp..." for MCP sessions, None to leave history
    untagged as before).
    """
    return AutonomyLedger(settings.ledger_path, caller=caller)


def jev_for(settings, transport=None, governor=None, ledger=None) -> JevPolicy:
    """Session composition for Jev: ONE policy owner (JEV-P4).

    Historically this returned a raw ``JevEvaluator`` — an orphan client
    outside ``jev_policy``. It now routes through ``policy_for`` and returns
    the shared :class:`~harness.jev_policy.JevPolicy`. Lane code must not
    construct ``JevEvaluator`` directly; if an injected evaluator is required
    (tests), pass it via ``policy_for(..., evaluator=...)``.
    """
    return policy_for(settings, transport=transport, governor=governor,
                      ledger=ledger)


def attest_model_for(settings):
    """The verifier identity a run uses: the same judge seat the Router gets.

    ONE owner for that choice: ``router_for`` builds the Router's judge from
    this, and lanes that thread an explicit ``attest_model`` (so the node's
    verifier is auditable rather than inherited) read it from here too -- an
    explicitly passed verifier can therefore never disagree with the engine
    default. Prefers the smartest per-tier judge only when escalation is
    actually armed.
    """
    return (settings.judge_top or settings.judge) if settings.allow_escalation \
        else settings.judge


def router_for(settings, *, jev_policy=None):
    """The lane router from the settings' curated pools.

    Legacy single ``escalation_model`` keeps precedence over the multi-rung
    ladder when both are configured, so an explicit override is never
    silently ignored by a non-empty default pool. When escalation is allowed
    and ``judge_top`` is set, the smartest per-tier judge is used.
    """
    ladder = None if settings.escalation_model else settings.escalation_pool
    # Prefer the smartest per-tier judge only when escalation is actually
    # allowed; otherwise keep the configured settings.judge.
    judge = attest_model_for(settings)
    return Router(settings.panel, judge, settings.apply_model,
                  settings.escalation_model, settings.allow_escalation,
                  panel_pool=settings.panel_pool, apply_pool=settings.apply_pool,
                  specialist_pool=settings.specialist_pool,
                  convergence_model=settings.convergence_model or judge,
                  escalation_pool=ladder,
                  frontier_model=getattr(settings, "frontier_model", None),
                  use_free=settings.use_free,
                  cheap_judge=settings.judge,
                  jev_policy=jev_policy)


def engine_for(settings, api_key, gov, ledger, router, transport=None):
    """The ApplyEngine with every settings-level policy applied. Only
    per-request knobs (instruction, ceilings for THIS task) are passed at
    the engine call site -- construction-level policy lives here."""
    wire = transport or HttpTransport()
    policy = policy_for(settings, transport=wire, governor=gov, ledger=ledger)
    if router is not None:
        if getattr(router, "jev_policy", None) is None:
            router.jev_policy = policy
        if getattr(router, "cheap_judge", None) is None:
            router.cheap_judge = settings.judge
    return ApplyEngine(
        wire, api_key=api_key, governor=gov, ledger=ledger, router=router,
        jev_policy=policy,
        default_require_consent=settings.default_require_consent,
        default_renew_consent=settings.renew_consent,
        reasoning_effort=settings.reasoning_effort,
        reasoning_token_budget=settings.reasoning_token_budget,
        default_max_rotations=settings.max_rotations,
        default_task_max_cost=settings.task_max_cost,
        use_free=settings.use_free,
        allowed_roots=settings.mcp_allowed_roots,
        min_confidence=settings.min_confidence)


def apply_session(settings, max_cost=None, transport=None):
    """ONE assembly step for engine-running commands (apply, continue,
    dogfood's apply phase, bench): key+governor, ledger, the pre-spend
    saturation look-ahead (advice only, never a gate), router, engine.
    Returns the engine; the callers' other needs come from the builders
    above. Only max_cost varies; other per-command knobs go to the engine
    call, not into the session. The verify lane assembles its own governor
    (panel lane, no engine) but shares governor_for/ledger_for."""
    api_key, gov = governor_for(settings, max_cost)
    ledger = ledger_for(settings)
    pre_run_warning(governor=gov, ledger=ledger, use_free=settings.use_free)
    return engine_for(settings, api_key, gov, ledger, router_for(settings),
                      transport=transport)


def run_meta(settings, governor):
    """Run metadata for a result envelope's ``meta`` block: the settings
    snapshot an interface (CLI, UI server) attaches so a consumer can
    reproduce the run, plus the ceiling. Deliberately excludes the key
    label (never echoed) and any secret material. Advisory: a non-numeric
    ceiling (test fakes) degrades to None instead of failing the run."""
    try:
        ceiling = round(float(governor.max_cost), 6)
    except (TypeError, ValueError):
        ceiling = None
    return {"use_free": settings.use_free,
            "panel": settings.panel, "judge": settings.judge,
            "apply_model": settings.apply_model,
            "reasoning_effort": settings.reasoning_effort,
            "max_cost_ceiling": ceiling,
            "key_label_present": bool(settings.expect_key_label)}
