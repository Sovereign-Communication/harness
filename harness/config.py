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
live-validated and rotated at runtime (see harness.core.discover_models and
the rotation logic in apply/panel). `openrouter/free` is OpenRouter's own
free router and serves as a final fallback lane.
"""
import json
import os
import sys

CONFIG_DIR = os.path.expanduser("~/.config/harness")

# OpenRouter endpoints
OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

# MorphLite-compatible transformation backend. Selecting the `morph` backend
# explicitly opts into this model; ordinary Harness routing remains unchanged.
MORPH_MODEL = "morph/morph-v3-fast"

# Cost ceilings. The philosophy (inherited from fusion_lite.py): worst-case
# cost is a *guarantee*, computed before any network call, not an estimate.
HARD_MAX_COST = 0.10        # per-call ceiling can never be raised past this
DEFAULT_MAX_COST = 0.02     # default per-call ceiling
HARD_TASK_MAX_COST = 0.25   # per-task (multi-round apply) hard ceiling
DEFAULT_TASK_MAX_COST = 0.05
# Verify token budget. On the free tier cost is $0 regardless, so this is
# intentionally generous -- it is NOT a cost cap. It exists so long audit/
# analysis prompts get a full answer instead of truncating (a 300-token default
# made reasoning-heavy free models burn the budget on hidden thinking and
# return empty content). Individual free models still impose their own hard
# per-request output ceilings; anything above a provider's cap is simply
# ignored/truncated by OpenRouter, so a large value here is safe.
DEFAULT_MAX_TOKENS = 2048
DEFAULT_APPLY_MAX_TOKENS = 4096

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
        with open(path, "r", encoding="utf-8") as f:
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
# openrouter/free. Run `harness spend --models` (or discover_models) to
# refresh against the live list.
FREE_PANEL_POOL = [
    "google/gemma-4-31b-it:free",
    "minimax/minimax-m3:free",
    "inclusionai/ling-3.0-flash-fin:free",
    "z-ai/glm-5.2:free",
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
    "minimax/minimax-m3:free",
    "z-ai/glm-5.2:free",
    "inclusionai/ling-3.0-flash-fin:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "cohere/north-mini-code:free",
    "openrouter/free",
]

# Convergence-specialist fallback ladder, tried in order after the primary
# (which defaults to the judge). GLM-5.2 is frontier-class and the strongest
# free reasoner on the router, so it leads; gemma and minimax are the most
# JSON-reliable free emitters behind it. The specialist rotates down this
# ladder when the primary returns an HTTP error, a paid-BYOK route,
# reasoning-only output, truncation, or unparseable JSON.
SPECIALIST_POOL_FREE = [
    "z-ai/glm-5.2:free",
    "google/gemma-4-31b-it:free",
    "minimax/minimax-m3:free",
]

# ---- Paid lanes (use_free=False) ----
DEFAULT_PANEL_PAID = [
    "inclusionai/ling-2.6-flash",
    "meta-llama/llama-3.1-8b-instruct",
    "ibm-granite/granite-4.1-8b",
]
DEFAULT_JUDGE_PAID = "inclusionai/ling-2.6-flash"
DEFAULT_APPLY_MODEL_PAID = "deepseek/deepseek-chat"

# Paid-lane specialist fallbacks (after the primary): strong JSON emitters
# first.
SPECIALIST_POOL_PAID = [
    "deepseek/deepseek-chat",
]

_ENV_NAMES = {
    "use_free": "HARNESS_USE_FREE",
    "panel": "HARNESS_PANEL",
    "panel_pool": "HARNESS_PANEL_POOL",
    "judge": "HARNESS_JUDGE",
    "convergence_model": "HARNESS_CONVERGENCE_MODEL",
    "specialist_pool": "HARNESS_SPECIALIST_POOL",
    "apply_model": "HARNESS_APPLY_MODEL",
    "apply_pool": "HARNESS_APPLY_POOL",
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
}


def _read_key_file(path):
    """Parse an `OPENROUTER_API_KEY=...` line out of an env file."""
    try:
        with open(path, "r", encoding="utf-8") as f:
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
        print(f"[warn] key file {path} is group/world readable (mode "
              f"{oct(mode)}); restrict it with chmod 600.", file=sys.stderr)


def resolve_api_key():
    for p in (
        os.path.join(os.path.expanduser("~/.config/scmorc"), "openrouter_fusion.env"),
        os.path.join(os.path.expanduser("~/.config/scmorc"), "openrouter.env"),
        os.path.join(CONFIG_DIR, "openrouter.env"),
    ):
        k = _read_key_file(p)
        if k:
            _warn_insecure_keyfile(p)
            return k
    return os.environ.get("OPENROUTER_API_KEY")


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
    def __init__(self, use_free, panel, panel_pool, judge, convergence_model, specialist_pool,
                 apply_model, apply_pool, escalation_model, max_cost, task_max_cost, max_tokens,
                 apply_max_tokens, reasoning_effort, reasoning_token_budget,
                 max_panelists, max_rotations, renew_consent, ledger_path,
                 expect_key_label, default_require_consent, allow_escalation):
        self.use_free = use_free
        self.panel = list(panel)
        self.panel_pool = list(panel_pool)
        self.judge = judge
        # Convergence specialist defaults to the same model as the judge.
        self.convergence_model = convergence_model or judge
        # Ordered fallback ladder for the specialist: the primary is tried
        # first, then these, strongest first.
        self.specialist_pool = list(specialist_pool or [])
        self.apply_model = apply_model
        self.apply_pool = list(apply_pool)
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

    def to_dict(self):
        return {k: getattr(self, k) for k in (
            "use_free", "panel", "panel_pool", "judge", "convergence_model",
            "specialist_pool",
            "apply_model", "apply_pool", "escalation_model", "max_cost",
            "task_max_cost", "max_tokens", "apply_max_tokens",
            "reasoning_effort", "reasoning_token_budget", "max_panelists",
            "max_rotations", "renew_consent", "ledger_path", "expect_key_label",
            "default_require_consent", "allow_escalation")}


def load_settings(overrides=None):
    cfg = {}
    cfg_path = os.path.join(CONFIG_DIR, "config.json")
    if os.path.exists(cfg_path):
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)

    def get(key, default):
        if overrides and key in overrides:
            return overrides[key]
        env = os.environ.get(_ENV_NAMES[key])
        if env is not None:
            return env
        return cfg.get(key, default)

    use_free = _as_bool(get("use_free", True))
    if use_free:
        default_panel = FREE_PANEL_POOL
        default_judge = FREE_JUDGE
        default_apply_pool = FREE_APPLY_POOL
        default_specialist_pool = SPECIALIST_POOL_FREE
    else:
        default_panel = DEFAULT_PANEL_PAID
        default_judge = DEFAULT_JUDGE_PAID
        default_apply_pool = [DEFAULT_APPLY_MODEL_PAID]
        default_specialist_pool = SPECIALIST_POOL_PAID

    panel = _split_list(str(get("panel", ",".join(default_panel)))) or default_panel
    panel_pool = _split_list(str(get("panel_pool", ",".join(panel)))) or panel
    apply_model = str(get("apply_model", default_apply_pool[0]))
    apply_pool = _split_list(str(get("apply_pool", ",".join(
        _dedup([apply_model] + default_apply_pool))))) or [apply_model]

    return Settings(
        use_free=use_free,
        panel=panel,
        panel_pool=panel_pool,
        judge=str(get("judge", default_judge)),
        convergence_model=get("convergence_model", None),
        specialist_pool=_split_list(str(get("specialist_pool", ",".join(default_specialist_pool)))) or default_specialist_pool,
        apply_model=apply_model,
        apply_pool=apply_pool,
        escalation_model=get("escalation_model", None),
        max_cost=float(get("max_cost", DEFAULT_MAX_COST)),
        task_max_cost=float(get("task_max_cost", DEFAULT_TASK_MAX_COST)),
        max_tokens=int(get("max_tokens", DEFAULT_MAX_TOKENS)),
        apply_max_tokens=int(get("apply_max_tokens", DEFAULT_APPLY_MAX_TOKENS)),
        reasoning_effort=str(get("reasoning_effort", "auto")),
        reasoning_token_budget=float(get("reasoning_token_budget", 0.4)),
        max_panelists=int(get("max_panelists", 3)),
        max_rotations=int(get("max_rotations", 3)),
        renew_consent=_as_bool(get("renew_consent", True)),
        ledger_path=str(get("ledger_path", os.path.join(CONFIG_DIR, "ledger.jsonl"))),
        expect_key_label=get("expect_key_label", os.environ.get("FUSIONLITE_EXPECT_KEY_LABEL")),
        default_require_consent=_as_bool(get("default_require_consent", True)),
        allow_escalation=_as_bool(get("allow_escalation", False)),
    )