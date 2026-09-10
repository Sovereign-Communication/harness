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


def ledger_for(settings, caller="cli"):
    """The run's autonomy ledger (hash-chained JSONL at the configured path).

    caller tags every appended event for per-caller trust attribution
    ("cli" for CLI runs, "mcp..." for MCP sessions, None to leave history
    untagged as before).
    """
    return AutonomyLedger(settings.ledger_path, caller=caller)


def router_for(settings):
    """The lane router from the settings' curated pools."""
    return Router(settings.panel, settings.judge, settings.apply_model,
                  settings.escalation_model, settings.allow_escalation,
                  panel_pool=settings.panel_pool, apply_pool=settings.apply_pool,
                  specialist_pool=settings.specialist_pool,
                  convergence_model=settings.convergence_model,
                  escalation_pool=settings.escalation_pool)


def engine_for(settings, api_key, gov, ledger, router):
    """The ApplyEngine with every settings-level policy applied. Only
    per-request knobs (instruction, ceilings for THIS task) are passed at
    the engine call site -- construction-level policy lives here."""
    return ApplyEngine(
        HttpTransport(), api_key=api_key, governor=gov, ledger=ledger, router=router,
        default_require_consent=settings.default_require_consent,
        default_renew_consent=settings.renew_consent,
        reasoning_effort=settings.reasoning_effort,
        reasoning_token_budget=settings.reasoning_token_budget,
        default_max_rotations=settings.max_rotations,
        default_task_max_cost=settings.task_max_cost,
        use_free=settings.use_free)


def apply_session(settings, max_cost=None):
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
    return engine_for(settings, api_key, gov, ledger, router_for(settings))
