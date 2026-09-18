"""Configuration, API-key resolution, and shared constants.

Key resolution order (back-compatible with SCMessenger's fusion_lite.py):
  1. ~/.config/scmorc/openrouter_fusion.env   (dedicated spend-limited fusion key)
  2. ~/.config/scmorc/openrouter.env
  3. ~/.config/harness/openrouter.env          (this package's home)
  4. $OPENROUTER_API_KEY environment variable

Settings come from ~/.config/harness/config.json, with $HARNESS_* env
variables taking precedence.

Free-tier routing: by default (`use_free=True`) every lane uses the best
current free OpenRouter models. Model slugs go stale, so the pools below are
live-validated and rotated at runtime (see harness.spend.discover_free_models and
the rotation logic in apply/panel). `openrouter/free` is OpenRouter's own
free router and serves as a final fallback lane.
"""
import json
import os
from dataclasses import dataclass

from .errors import HarnessError
from .output import eprint
from .validation import finite_number

CONFIG_DIR = os.path.expanduser("~/.config/harness")

# OpenRouter endpoints
OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
# Daily usage rankings (model_permaslug + total_tokens per day). The
# evidence source for pool-candidate refresh (see harness/rankings.py).
OPENROUTER_RANKINGS_URL = "https://openrouter.ai/api/v1/datasets/rankings-daily"

# MorphLite-compatible transformation backend. Selecting the `morph` backend
# explicitly opts into this model; ordinary Harness routing remains unchanged.
MORPH_MODEL = "morph/morph-v3-fast"

# Cost ceilings. The philosophy (inherited from fusion_lite.py): worst-case
# cost is a *guarantee*, computed before any network call, not an estimate.
HARD_MAX_COST = 0.10        # per-call ceiling can never be raised past this
# Default per-call (per-lane-run) ceiling. Raised from $0.02 with the verified
# 2026-09-13 paid slates: the wider vote pool's worst-case preflight reserve is
# ~$0.04 (actual verified cost of a 5-vote paid panel: $0.0038 -- see
# docs/MODEL_SELECTION_HANDOFF_2026-09-13.md). HARD_MAX_COST is unchanged.
DEFAULT_MAX_COST = 0.05
HARD_TASK_MAX_COST = 0.25   # per-task (multi-round apply) hard ceiling
# Raised from $0.05 with default-on paid escalation: the unknown-correctness
# ration (0.4 of the hard cap) must fund one worst-case paid rescue call
# (~$0.084 on the cheapest paid rung) or the saturation ladder starves the
# rung it just stepped up to. Still 2.5x under the hard ceiling.
DEFAULT_TASK_MAX_COST = 0.10
# Verify token budget. On the free tier cost is $0 regardless, so this is
# intentionally generous -- it is NOT a cost cap. It exists so long audit/
# analysis prompts get a full answer instead of truncating (a 300-token default
# made reasoning-heavy free models burn the budget on hidden thinking and
# return empty content). Individual free models still impose their own hard
# per-request output ceilings; anything above a provider's cap is simply
# ignored/truncated by OpenRouter, so a large value here is safe.
DEFAULT_MAX_TOKENS = 2048
DEFAULT_APPLY_MAX_TOKENS = 4096

# ---- lane budgets and reasoning modes (ONE policy owner) -------------------
# Task-shaped token budgets (2026-09-13 operator ruling: "stop using reasoning
# if it's not needed here, or if it is, then appropriately allocate tokens").
# A vote is a cheap decision: hidden thinking starves the visible JSON, so
# structured votes run with reasoning explicitly disabled and a >=4096 output
# budget (600-700 starved even non-reasoning emitters into truncation). Judge
# synthesis, the convergence specialist, and escalation rungs need bounded
# depth: >=8192 with the caller's reasoning mode (auto by default; the
# auto/low heuristic lives in chat.py). Explicit caller configuration always
# wins over these defaults. Apply keeps its own 4096 budget untouched.
MIN_VOTE_TOKENS = 4096
MIN_SYNTHESIS_TOKENS = 8192
# Structured claim votes are frequently longer than ordinary prose; the vote
# lane raises its floor when convergence is requested (kept next to the other
# lane minima as the ONE policy home; convergence.py imports it back for its
# own consumers).
MIN_CONVERGENCE_PANEL_TOKENS = 4096


def effective_lane_policy(role, max_tokens=None, reasoning_effort=None):
    """The ONE owner of per-lane token budgets and reasoning modes.

    role is one of "vote" (panel/claim votes), "judge" (judge synthesis and
    the convergence specialist), "escalation" (deep rungs), or "apply"
    (unchanged historical behavior). Returns ``(max_tokens, reasoning_effort)``
    with the lane minimum applied and the reasoning mode resolved: an explicit
    caller effort always passes through; otherwise votes disable reasoning
    ("off" -- an explicit ``{"effort": "none"}`` payload post-patch) and
    synthesis/escalation lanes default to "auto".
    """
    requested = int(max_tokens or 0)
    if role == "vote":
        effort = reasoning_effort or "off"
        return max(requested, MIN_VOTE_TOKENS), effort
    if role in ("judge", "escalation"):
        effort = reasoning_effort or "auto"
        return max(requested, MIN_SYNTHESIS_TOKENS), effort
    if role == "apply":
        return requested, (reasoning_effort or "auto")
    raise ValueError(f"unknown lane role: {role}")


@dataclass(frozen=True)
class LanePolicy:
    """One lane's resolved spend: the max_tokens floor and reasoning effort."""

    tokens: int
    effort: str


@dataclass(frozen=True)
class PanelLanePolicy:
    """Immutable resolved lane policy for one ``panel_judge`` run.

    Construction applies every caller override EXACTLY ONCE through
    :func:`effective_lane_policy` (still the single policy source); consumers
    -- reservation computations and chat calls -- read
    ``policy.vote.tokens`` / ``policy.judge.tokens`` /
    ``policy.specialist.tokens`` (and ``.effort``) instead of parallel
    token/effort locals threaded through the lane. This is the struct that
    would have prevented the specialist reserve/call mismatch: there is one
    place to look up what a lane spends.

    Attributes (post-override):
        vote: panel votes; when convergence is requested the vote floor is
            the structured-claims minimum (a caller's lower bound is kept but
            raised to avoid truncation in the vote JSON).
        judge: judge synthesis; reserve rows use ``judge_reserve_tokens``
            (the synthesis budget plus the fixed headroom the prompt-builder
            adds), never a different lane's budget.
        specialist: the convergence specialist's JSON-rendering lane -- at the
            vote floor by ruling, NOT the synthesis floor (an 8192 budget
            defeats the window-aware vote trim on small-window specialists).
    """

    vote: LanePolicy
    judge: LanePolicy
    specialist: LanePolicy

    def __init__(self, *, max_tokens=None, reasoning_effort="auto",
                 run_convergence=False):
        vote_tokens, vote_effort = effective_lane_policy(
            "vote", max_tokens=max_tokens, reasoning_effort=reasoning_effort)
        if run_convergence:
            vote_tokens = max(vote_tokens, MIN_CONVERGENCE_PANEL_TOKENS)
        judge_tokens, judge_effort = effective_lane_policy(
            "judge", max_tokens=max_tokens, reasoning_effort=reasoning_effort)
        spec_tokens, spec_effort = effective_lane_policy(
            "vote", max_tokens=max_tokens, reasoning_effort=reasoning_effort)
        object.__setattr__(self, "vote", LanePolicy(vote_tokens, vote_effort))
        object.__setattr__(self, "judge", LanePolicy(judge_tokens, judge_effort))
        object.__setattr__(self, "specialist", LanePolicy(spec_tokens, spec_effort))

# BYOK spend is invisible to the tracked key's balance (confirmed on the
# SCMessenger account: mistralai/ routed via BYOK, plus the P0 block below for
# Claude/Anthropic models reaching the paid OpenRouter path).
#
# HARD prefixes are never used, period (P0). Additional org-prefixes observed
# to route via BYOK on a given account are LEARNED at runtime and persisted to
# byok_prefixes_path, so paid BYOK models rotate away automatically while free
# BYOK models (which cost $0 and leak nothing) are still usable.
BYOK_DENYLIST_PREFIXES = ("mistralai/", "anthropic/")
BYOK_PREFIXES_PATH = os.path.join(CONFIG_DIR, "byok_prefixes.json")

# Model capability registry (declared /models metadata + composite reliability).
CAPABILITIES_PATH = os.path.join(CONFIG_DIR, "capabilities.json")
CAPABILITIES_TTL = 24 * 3600  # refresh /models capability profiles at most once / TTL


def load_byok_prefixes(path=BYOK_PREFIXES_PATH):
    try:
        with open(path, encoding="utf-8") as f:
            return set(json.load(f))
    except (OSError, ValueError):
        return set()


def save_byok_prefixes(path, prefixes):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sorted(prefixes), f, indent=2)

# ---- Free-tier lanes (default) ----
# Curated from the live OpenRouter free list (Sept 2026), ordered by observed
# track record in real audits and bench probes: JSON-emission reliability,
# willingness to defer, and truncation behavior. north-mini-code is DEMOTED
# from the judge seat (and the panel head): as a reasoning model it burned
# its budget on hidden thinking and returned reasoning-only output in most
# live runs. The router rotates down the list and falls back to
# openrouter/free. Run `harness models` or `harness capabilities --refresh` to
# refresh against the live list.
FREE_PANEL_POOL = [
    "google/gemma-4-31b-it:free",
    "inclusionai/ling-3.0-flash-fin:free",
    "google/gemma-4-26b-a4b-it:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "cohere/north-mini-code:free",
    "openrouter/free",
]
# Judge must emit strict JSON. gemma is the most JSON-reliable free emitter in
# live runs (structured claims, consent, and specialist lanes all included);
# a reasoning-heavy judge burns its budget on hidden thinking instead.
FREE_JUDGE = "google/gemma-4-31b-it:free"
FREE_APPLY_POOL = [
    "google/gemma-4-31b-it:free",
    "google/gemma-4-26b-a4b-it:free",
    "inclusionai/ling-3.0-flash-fin:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "cohere/north-mini-code:free",
    "openrouter/free",
]

# Convergence-specialist fallback ladder, tried in order after the primary
# (which defaults to the judge). Ordered by observed track record rather than
# declared capability; only catalog-validated ids belong in shipped defaults.
# Rotation happens before the next start, never after a first failure the
# ledger already predicted. The specialist rotates down this ladder on HTTP
# error, paid-BYOK route, reasoning-only output, truncation, or unparseable JSON.
SPECIALIST_POOL_FREE = [
    "google/gemma-4-31b-it:free",
    "google/gemma-4-26b-a4b-it:free",
]

# ---- Paid lanes (use_free=False) ----
# Curated from the live OpenRouter catalog and per-call probes (2026-09-13;
# evidence: docs/MODEL_SELECTION_HANDOFF_2026-09-13.md). Same policy as the
# free pools -- stale ids hard-fatal at fetch_pricing, so re-validate before
# shipping a change here (the round-1 free-pool fix is the precedent).
# Vote pool: reasoning-OFF emitters verified parseable at 4096 budgets,
# ordered cheapest first (gemini-3.8-flash supersedes the banned
# gemini-2.5-pro; the V3-era deepseek ids and granite/llama-3.1-8b are
# dropped as superseded/oldest-generation).
DEFAULT_PANEL_PAID = [
    "deepseek/deepseek-v4-flash",
    "deepseek/deepseek-v4.1-flash",
    "inclusionai/ling-3.0-flash",
    "openai/gpt-4o-mini",
    "openai/gpt-5-mini",
    "openai/gpt-5.6-luna",
    "google/gemini-3.8-flash",
]
# Value-oriented deep-thinking judge (operator-endorsed); its route is
# MANDATORY-reasoning, which the chat lane supports via the param-rejection
# retry (an explicit disable draws one free 400, then the provider default).
DEFAULT_JUDGE_PAID = "z-ai/glm-5.3-flash"
# Apply primary: the operator pick (rankings #3 and climbing), with verified
# cheaper/fallback candidates.
DEFAULT_APPLY_MODEL_PAID = "deepseek/deepseek-v4.1-flash"
DEFAULT_APPLY_POOL_PAID = [
    "deepseek/deepseek-v4.1-flash",
    "deepseek/deepseek-v4-flash",
    "openai/gpt-4o-mini",
    "openai/gpt-5.6-luna",
]

# Paid-lane specialist fallbacks (after the primary): strong JSON emitters
# first.
SPECIALIST_POOL_PAID = [
    "deepseek/deepseek-v4.1-flash",
]

# ---- Escalation ladders (judge-driven auto-escalation with de-escalation) ----
# Ordered from cheapest to most capable. The judge condenses context and
# directs escalation rung-by-rung; after the escalated model produces a plan,
# the system de-escalates back to the last tier that needed escalation.
# Each rung has an implicit per-rung cost cap enforced by SpendGovernor.
ESCALATION_POOL_FREE = [
    "google/gemma-4-26b-a4b-it:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "cohere/north-mini-code:free",
]

# Paid escalation ladder: curated from the verified deep-think tier
# (2026-09-13 probes), cheapest -> most capable. The top rung is the
# "smartest per price tier" capstone.
# Per-rung cost caps are advisory (enforced by SpendGovernor preflight).
# Only catalog-validated ids belong here (stale ids hard-fatal at fetch_pricing).
ESCALATION_POOL_PAID = [
    "z-ai/glm-5.3-flash",
    "deepseek/deepseek-v4-pro",
    "openai/gpt-4.1",
    "openai/gpt-5.6-sol",
]

# The smartest judge available per tier (used when judge auto-selection is on).
# Free tier: gemma-4-31b-it:free (already DEFAULT_JUDGE for free).
# Paid tier: flagship-class from the verified deep-think tier.
DEFAULT_JUDGE_PAID_TOP = "openai/gpt-5.6-sol"

# Per-rung advisory cost caps (USD). Documented policy for operators and the
# judge's worth-gating decision; the hard enforcement is still
# HARD_TASK_MAX_COST / HARD_MAX_COST via SpendGovernor preflight.
ESCALATION_RUNG_CAPS = {
    "free": [0.0, 0.0, 0.0],
    "paid": [0.03, 0.06, 0.10, 0.20],
}


def shipped_model_ids():
    """Every default lane model id this install ships with, across both tier
    policies -- the complete set `capabilities --check-shipped` validates
    against the live catalog (a stale shipped id hard-fatals at
    fetch_pricing; the round-1 free-pool fix and the stale paid default are
    the precedent). One enumeration: add a pool constant here, not to every
    freshness test."""
    return (set(FREE_PANEL_POOL) | {FREE_JUDGE} | set(FREE_APPLY_POOL)
            | set(SPECIALIST_POOL_FREE)
            | set(DEFAULT_PANEL_PAID) | {DEFAULT_JUDGE_PAID}
            | set(DEFAULT_APPLY_POOL_PAID) | set(SPECIALIST_POOL_PAID)
            | set(ESCALATION_POOL_FREE) | set(ESCALATION_POOL_PAID)
            | {DEFAULT_JUDGE_PAID_TOP})

_ENV_NAMES = {
    "use_free": "HARNESS_USE_FREE",
    "panel": "HARNESS_PANEL",
    "panel_pool": "HARNESS_PANEL_POOL",
    "judge": "HARNESS_JUDGE",
    "judge_top": "HARNESS_JUDGE_TOP",
    "convergence_model": "HARNESS_CONVERGENCE_MODEL",
    "specialist_pool": "HARNESS_SPECIALIST_POOL",
    "apply_model": "HARNESS_APPLY_MODEL",
    "apply_pool": "HARNESS_APPLY_POOL",
    "escalation_pool": "HARNESS_ESCALATION_POOL",
    "escalation_model": "HARNESS_ESCALATION_MODEL",
    "max_cost": "HARNESS_MAX_COST",
    "task_max_cost": "HARNESS_TASK_MAX_COST",
    "max_tokens": "HARNESS_MAX_TOKENS",
    "apply_max_tokens": "HARNESS_APPLY_MAX_TOKENS",
    "reasoning_effort": "HARNESS_REASONING_EFFORT",
    "reasoning_token_budget": "HARNESS_REASONING_TOKEN_BUDGET",
    "max_panelists": "HARNESS_MAX_PANELISTS",
    "max_rotations": "HARNESS_MAX_ROTATIONS",
    "renew_consent": "HARNESS_RENEW_CONSENT",
    "ledger_path": "HARNESS_LEDGER",
    "expect_key_label": "HARNESS_EXPECT_KEY_LABEL",
    "default_require_consent": "HARNESS_DEFAULT_REQUIRE_CONSENT",
    "allow_escalation": "HARNESS_ALLOW_ESCALATION",
    "mcp_allow_write": "HARNESS_MCP_ALLOW_WRITE",
    "mcp_allow_verify": "HARNESS_MCP_ALLOW_VERIFY",
    "mcp_allowed_roots": "HARNESS_MCP_ALLOWED_ROOTS",
    "mcp_tool_timeout": "HARNESS_MCP_TOOL_TIMEOUT",
    "mcp_auth_token": "HARNESS_MCP_AUTH_TOKEN",
    "frontier_model": "HARNESS_FRONTIER_MODEL",
    "hourglass_confirm": "HARNESS_HOURGLASS_CONFIRM",
    "hourglass_isolate": "HARNESS_HOURGLASS_ISOLATE",
    "hourglass_parallel": "HARNESS_HOURGLASS_PARALLEL",
    "hourglass_require_attestation": "HARNESS_HOURGLASS_REQUIRE_ATTESTATION",
}


def _read_key_file(path):
    """Parse an `OPENROUTER_API_KEY=...` line out of an env file."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    if k.strip() == "OPENROUTER_API_KEY" and v.strip():
                        return v.strip().strip('"').strip("'")
    except OSError:
        return None
    return None


def _warn_insecure_keyfile(path):
    """Loudly warn when a key file is group/world readable (POSIX only).

    A leaked OpenRouter key spends real money, so a permissive key file is a
    silent credential hazard. We warn rather than refuse: the user's working
    setup must not break, but the failure mode must not be silent.
    """
    if os.name == "nt":
        return
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        return
    if mode & 0o077:
        eprint(f"[warn] key file {path} is group/world readable (mode "
               f"{oct(mode)}); restrict it with chmod 600.")


def resolve_api_key():
    """Resolve the OpenRouter key: env file first, then the environment.

    Key identity is enforced by SpendGovernor.verify_key (exact match of
    the live /key label, never echoed into errors). An earlier revision
    tried to pre-check the label here by splitting the key string itself,
    which can never yield a label -- that dead guard is gone rather than
    left to reject every real key the day someone passes it a label.
    """
    key = None
    for p in (
        os.path.join(os.path.expanduser("~/.config/scmorc"), "openrouter_fusion.env"),
        os.path.join(os.path.expanduser("~/.config/scmorc"), "openrouter.env"),
        os.path.join(CONFIG_DIR, "openrouter.env"),
    ):
        k = _read_key_file(p)
        if k:
            _warn_insecure_keyfile(p)
            key = k
            break
    if key is None:
        key = os.environ.get("OPENROUTER_API_KEY")
        if key:
            eprint("[warn] using OPENROUTER_API_KEY from the process environment; "
                   "prefer a 0600 key file for interactive use.")
    return key


def _as_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _split_list(value):
    return [x.strip() for x in value.split(",") if x.strip()]


def _dedup(seq):
    out = []
    for x in seq:
        if x not in out:
            out.append(x)
    return out


class Settings:
    def __init__(self, use_free, panel, panel_pool, judge, judge_top, convergence_model, specialist_pool,
                 apply_model, apply_pool, escalation_pool, escalation_model, max_cost, task_max_cost, max_tokens,
                 apply_max_tokens, reasoning_effort, reasoning_token_budget,
                 max_panelists, max_rotations, renew_consent, ledger_path,
                  expect_key_label, default_require_consent, allow_escalation,
                  mcp_allow_write=False, mcp_allow_verify=False, mcp_allowed_roots=None,
                  mcp_tool_timeout=1800, mcp_auth_token=None, frontier_model=None,
                  hourglass_confirm=True, hourglass_isolate=True,
                  hourglass_parallel=True, hourglass_require_attestation=True):
        self.use_free = use_free
        self.panel = list(panel)
        self.panel_pool = list(panel_pool)
        self.judge = judge
        # The smartest judge available for the current tier (free or paid).
        self.judge_top = judge_top
        # Convergence specialist defaults to the same model as the judge.
        self.convergence_model = convergence_model or judge
        # Ordered fallback ladder for the specialist: the primary is tried
        # first, then these, strongest first.
        self.specialist_pool = list(specialist_pool or [])
        self.apply_model = apply_model
        self.apply_pool = list(apply_pool)
        # Escalation ladder (ordered cheapest->most capable). Used by the
        # auto-escalation driver for judge-driven rung stepping.
        self.escalation_pool = list(escalation_pool or [])
        # Single escalation_model retained for backward-compat (single-rung mode).
        self.escalation_model = escalation_model
        self.max_cost = max_cost
        self.task_max_cost = task_max_cost
        self.max_tokens = max_tokens
        self.apply_max_tokens = apply_max_tokens
        self.reasoning_effort = reasoning_effort
        self.reasoning_token_budget = reasoning_token_budget
        self.max_panelists = max_panelists
        self.max_rotations = max_rotations
        self.renew_consent = renew_consent
        self.ledger_path = ledger_path
        self.expect_key_label = expect_key_label
        self.default_require_consent = default_require_consent
        self.allow_escalation = allow_escalation
        self.mcp_allow_write = mcp_allow_write
        self.mcp_allow_verify = mcp_allow_verify
        self.mcp_allowed_roots = list(mcp_allowed_roots or [])
        self.mcp_tool_timeout = mcp_tool_timeout
        # Shared secret for the stdio MCP peer. Empty/None = no token check
        # (stdio inherits host authority; documented trust model). When set,
        # every tools/call must present matching params._meta.harness_token.
        self.mcp_auth_token = mcp_auth_token or None
        self.frontier_model = frontier_model
        # Auto-scaling hourglass defaults (CLI plan lane + MCP/GUI
        # plan_and_execute): waist confirmation, parallel stages, worktree
        # isolation, and diff-bound write attestation are ON by default;
        # each is opt-out via its flag (--no-*) or this settings file.
        self.hourglass_confirm = hourglass_confirm
        self.hourglass_isolate = hourglass_isolate
        self.hourglass_parallel = hourglass_parallel
        self.hourglass_require_attestation = hourglass_require_attestation

    def to_dict(self):
        return {k: getattr(self, k) for k in (
            "use_free", "panel", "panel_pool", "judge", "judge_top", "convergence_model",
            "specialist_pool",
            "apply_model", "apply_pool", "escalation_pool", "escalation_model", "max_cost",
            "task_max_cost", "max_tokens", "apply_max_tokens",
            "reasoning_effort", "reasoning_token_budget", "max_panelists",
            "max_rotations", "renew_consent", "ledger_path", "expect_key_label",
            "default_require_consent", "allow_escalation", "mcp_allow_write",
            "mcp_allow_verify", "mcp_allowed_roots", "mcp_tool_timeout",
            "mcp_auth_token", "frontier_model", "hourglass_confirm",
            "hourglass_isolate", "hourglass_parallel",
            "hourglass_require_attestation")}


def load_settings(overrides=None):
    cfg = {}
    cfg_path = os.path.join(CONFIG_DIR, "config.json")
    if os.path.exists(cfg_path):
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
    unknown = set(cfg) - set(_ENV_NAMES)
    if unknown:
        eprint("[warn] unknown config keys ignored: " + ", ".join(sorted(unknown))
               + f" (valid keys are in {cfg_path})")

    def get(key, default):
        if overrides and key in overrides:
            return overrides[key]
        env = os.environ.get(_ENV_NAMES[key])
        if env is not None:
            return env
        return cfg.get(key, default)

    use_free = _as_bool(get("use_free", True))
    # Auto-escalation defaults ON when a paid key is connected: when the
    # cheap tier saturates (429 across the pool) or exhausts its verify
    # budget, the judge walks the escalation ladder into the lowest paid
    # rung instead of failing the task. An explicit allow_escalation=false
    # (config/env/overrides) still disarms it, and with no paid key there
    # is nothing to walk into -- the default stays off.
    paid_key = resolve_api_key()
    if use_free:
        default_panel = FREE_PANEL_POOL
        default_judge = FREE_JUDGE
        default_judge_top = FREE_JUDGE
        default_apply_pool = FREE_APPLY_POOL
        default_specialist_pool = SPECIALIST_POOL_FREE
        default_escalation_pool = ESCALATION_POOL_FREE
        # Saturation ladder: free rungs first (they cost nothing), then the
        # paid ladder cheapest-first -- a busy free rung rotates onward.
        if paid_key:
            default_escalation_pool = (ESCALATION_POOL_FREE
                                       + ESCALATION_POOL_PAID)
    else:
        default_panel = DEFAULT_PANEL_PAID
        default_judge = DEFAULT_JUDGE_PAID
        default_judge_top = DEFAULT_JUDGE_PAID_TOP
        default_apply_pool = list(DEFAULT_APPLY_POOL_PAID)
        default_specialist_pool = SPECIALIST_POOL_PAID
        default_escalation_pool = ESCALATION_POOL_PAID

    panel = _split_list(str(get("panel", ",".join(default_panel)))) or default_panel
    panel_pool = _split_list(str(get("panel_pool", ",".join(panel)))) or panel
    apply_model = str(get("apply_model", default_apply_pool[0]))
    apply_pool = _split_list(str(get("apply_pool", ",".join(
        _dedup([apply_model] + default_apply_pool))))) or [apply_model]
    judge_top = str(get("judge_top", default_judge_top))
    escalation_pool = _split_list(str(get("escalation_pool", ",".join(default_escalation_pool)))) or default_escalation_pool

    # -- numeric range validation (fail closed on nonsense) ------------------
    # Cost ceilings are HARD: HARD_MAX_COST / HARD_TASK_MAX_COST are absolute
    # per-call / per-task ceilings that no configuration (config.json, env, or
    # CLI override) can raise past. A hostile or misconfigured value must be
    # refused before any network call, not silently applied.
    def _num(key, cast, lo, hi, default):
        raw = get(key, default)
        try:
            value = cast(raw)
        except (TypeError, ValueError, OverflowError):
            raise HarnessError(key + " must be a valid number") from None
        value = finite_number(value, key, lo, hi)
        # finite_number always returns float: integer settings (panelists,
        # tokens, rotations) must go back to int, or range()/max_workers
        # crash the live lanes the hermetic suite never exercises.
        return int(value) if cast is int else value

    max_cost = _num("max_cost", float, 0, HARD_MAX_COST, DEFAULT_MAX_COST)
    task_max_cost = _num("task_max_cost", float, 0, HARD_TASK_MAX_COST, DEFAULT_TASK_MAX_COST)
    max_tokens = _num("max_tokens", int, 64, 200000, DEFAULT_MAX_TOKENS)
    apply_max_tokens = _num("apply_max_tokens", int, 64, 200000, DEFAULT_APPLY_MAX_TOKENS)
    reasoning_token_budget = _num("reasoning_token_budget", float, 0.05, 1, 0.4)
    max_panelists = _num("max_panelists", int, 1, 10, 3)
    max_rotations = _num("max_rotations", int, 0, 20, 3)
    # Per-tool deadline for the MCP server: cooperative (trips cancel_check,
    # same path as notifications/cancelled), so an uncancelled-but-overdue
    # run still stops at the next poll point instead of holding a lane.
    mcp_tool_timeout = _num("mcp_tool_timeout", int, 60, 7200, 1800)

    return Settings(
        use_free=use_free,
        panel=panel,
        panel_pool=panel_pool,
        judge=str(get("judge", default_judge)),
        judge_top=judge_top,
        convergence_model=get("convergence_model", None),
        specialist_pool=_split_list(str(get("specialist_pool", ",".join(default_specialist_pool)))) or default_specialist_pool,
        apply_model=apply_model,
        apply_pool=apply_pool,
        escalation_pool=escalation_pool,
        escalation_model=get("escalation_model", None),
        max_cost=max_cost,
        task_max_cost=task_max_cost,
        max_tokens=max_tokens,
        apply_max_tokens=apply_max_tokens,
        reasoning_effort=str(get("reasoning_effort", "auto")),
        reasoning_token_budget=reasoning_token_budget,
        max_panelists=max_panelists,
        max_rotations=max_rotations,
        renew_consent=_as_bool(get("renew_consent", True)),
        ledger_path=str(get("ledger_path", os.path.join(CONFIG_DIR, "ledger.jsonl"))),
        expect_key_label=get("expect_key_label", os.environ.get("FUSIONLITE_EXPECT_KEY_LABEL")),
        default_require_consent=_as_bool(get("default_require_consent", True)),
        allow_escalation=_as_bool(get("allow_escalation", bool(paid_key))),
        mcp_allow_write=_as_bool(get("mcp_allow_write", False)),
        mcp_allow_verify=_as_bool(get("mcp_allow_verify", False)),
        mcp_allowed_roots=_split_list(str(get("mcp_allowed_roots", ""))),
        mcp_tool_timeout=mcp_tool_timeout,
        mcp_auth_token=get("mcp_auth_token", None) or None,
        frontier_model=get("frontier_model", None),
        hourglass_confirm=_as_bool(get("hourglass_confirm", True)),
        hourglass_isolate=_as_bool(get("hourglass_isolate", True)),
        hourglass_parallel=_as_bool(get("hourglass_parallel", True)),
        hourglass_require_attestation=_as_bool(
            get("hourglass_require_attestation", True)),
    )
