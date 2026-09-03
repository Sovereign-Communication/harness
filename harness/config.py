"""Configuration, API-key resolution, and shared constants.

Key resolution order (back-compatible with SCMessenger's fusion_lite.py):
  1. ~/.config/scmorc/openrouter_fusion.env   (dedicated spend-limited fusion key)
  2. ~/.config/scmorc/openrouter.env
  3. ~/.config/harness/openrouter.env          (this package's home)
  4. $OPENROUTER_API_KEY environment variable

Settings come from ~/.config/harness/config.json, with $HARNESS_* env
variables taking precedence.
"""
import json
import os

CONFIG_DIR = os.path.expanduser("~/.config/harness")

# OpenRouter endpoints
OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

# Cost ceilings. The philosophy (inherited from fusion_lite.py): worst-case
# cost is a *guarantee*, computed before any network call, not an estimate.
HARD_MAX_COST = 0.10        # per-call ceiling can never be raised past this
DEFAULT_MAX_COST = 0.02     # default per-call ceiling
HARD_TASK_MAX_COST = 0.25   # per-task (multi-round apply) hard ceiling
DEFAULT_TASK_MAX_COST = 0.05
DEFAULT_MAX_TOKENS = 300
DEFAULT_APPLY_MAX_TOKENS = 4096

# BYOK spend is invisible to the tracked key's balance (confirmed on the
# SCMessenger account: mistralai/ routed via BYOK, plus the P0 block below for
# Claude/Anthropic models reaching the paid OpenRouter path).
BYOK_DENYLIST_PREFIXES = ("mistralai/", "anthropic/")

DEFAULT_PANEL = [
    "inclusionai/ling-2.6-flash",
    "meta-llama/llama-3.1-8b-instruct",
    "ibm-granite/granite-4.1-8b",
]
DEFAULT_JUDGE = "inclusionai/ling-2.6-flash"
DEFAULT_APPLY_MODEL = "deepseek/deepseek-chat"

_ENV_NAMES = {
    "panel": "HARNESS_PANEL",
    "judge": "HARNESS_JUDGE",
    "apply_model": "HARNESS_APPLY_MODEL",
    "escalation_model": "HARNESS_ESCALATION_MODEL",
    "max_cost": "HARNESS_MAX_COST",
    "task_max_cost": "HARNESS_TASK_MAX_COST",
    "max_tokens": "HARNESS_MAX_TOKENS",
    "apply_max_tokens": "HARNESS_APPLY_MAX_TOKENS",
    "reasoning_effort": "HARNESS_REASONING_EFFORT",
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


def resolve_api_key():
    for p in (
        os.path.join(os.path.expanduser("~/.config/scmorc"), "openrouter_fusion.env"),
        os.path.join(os.path.expanduser("~/.config/scmorc"), "openrouter.env"),
        os.path.join(CONFIG_DIR, "openrouter.env"),
    ):
        k = _read_key_file(p)
        if k:
            return k
    return os.environ.get("OPENROUTER_API_KEY")


def _as_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


class Settings:
    def __init__(self, panel, judge, apply_model, escalation_model, max_cost,
                 task_max_cost, max_tokens, apply_max_tokens, reasoning_effort,
                 ledger_path, expect_key_label, default_require_consent,
                 allow_escalation):
        self.panel = list(panel)
        self.judge = judge
        self.apply_model = apply_model
        self.escalation_model = escalation_model
        self.max_cost = max_cost
        self.task_max_cost = task_max_cost
        self.max_tokens = max_tokens
        self.apply_max_tokens = apply_max_tokens
        self.reasoning_effort = reasoning_effort
        self.ledger_path = ledger_path
        self.expect_key_label = expect_key_label
        self.default_require_consent = default_require_consent
        self.allow_escalation = allow_escalation

    def to_dict(self):
        return {k: getattr(self, k) for k in (
            "panel", "judge", "apply_model", "escalation_model", "max_cost",
            "task_max_cost", "max_tokens", "apply_max_tokens",
            "reasoning_effort", "ledger_path", "expect_key_label",
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

    panel = str(get("panel", ",".join(DEFAULT_PANEL))).split(",")
    panel = [x.strip() for x in panel if x.strip()]

    return Settings(
        panel=panel or DEFAULT_PANEL,
        judge=str(get("judge", DEFAULT_JUDGE)),
        apply_model=str(get("apply_model", DEFAULT_APPLY_MODEL)),
        escalation_model=get("escalation_model", None),
        max_cost=float(get("max_cost", DEFAULT_MAX_COST)),
        task_max_cost=float(get("task_max_cost", DEFAULT_TASK_MAX_COST)),
        max_tokens=int(get("max_tokens", DEFAULT_MAX_TOKENS)),
        apply_max_tokens=int(get("apply_max_tokens", DEFAULT_APPLY_MAX_TOKENS)),
        reasoning_effort=str(get("reasoning_effort", "low")),
        ledger_path=str(get("ledger_path", os.path.join(CONFIG_DIR, "ledger.jsonl"))),
        expect_key_label=get("expect_key_label", os.environ.get("FUSIONLITE_EXPECT_KEY_LABEL")),
        default_require_consent=_as_bool(get("default_require_consent", True)),
        allow_escalation=_as_bool(get("allow_escalation", False)),
    )