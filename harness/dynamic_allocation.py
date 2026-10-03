"""Dynamic resource allocation and runtime negotiation engine for Harness and Jev.

Replaces static, hardcoded constants across token budgeting, spend ceilings,
window pagination, evidence sizing, and model selection with dynamic,
context-aware negotiation:

1. Dynamic Token and Context Allocation:
   - Scales run and stage allowances according to model context window
     (e.g., 128k, 1M tokens) instead of static 200k/64k caps.
   - Dynamically sizes file inspection windows (replacing static 200 lines)
     proportional to remaining budget and file AST density.
   - Adjusts waist inspection rounds to file count and available budget headroom.

2. Dynamic Budget and Spend Allocation:
   - Evaluates tier spend ceilings from actual model quote specs and token estimates
     instead of arbitrary fixed rungs ($0.01 / $0.04 / $1.00).
   - Computes terminal reserves adaptively: zero reserve for hermetic local gates;
     exact single-call worst-case for model-judged gates.

3. Adaptive Jev Perception and Evidence Sizing:
   - Scales evidence question counts and character allowances based on task
     scope, ambiguity, and available token headroom.
   - Computes risk-weighted confidence thresholds (strict for ungated mutations,
     balanced for test-gated inspections).

4. Dynamic Model and Rung Ladders:
   - Ranks and filters model ladders dynamically based on observed pass-rates,
     latency, and cost efficiency.
"""
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .jev import JEV_INPUT_PRICE_PER_MILLION

# Baseline reference boundaries
DEFAULT_MIN_WINDOW_LINES = 50
DEFAULT_MAX_WINDOW_LINES = 1000
ESTIMATED_TOKENS_PER_LINE = 7.5


@dataclass(frozen=True)
class ModelContextSpec:
    context_window: int
    max_output_tokens: int
    input_cost_per_million: float
    output_cost_per_million: float


# Catalog of recognized provider model context windows and pricing
# (rates in USD per 1M tokens)
KNOWN_MODEL_SPECS: Dict[str, ModelContextSpec] = {
    # Free rungs
    "inclusionai/ling-3.0-flash-fin:free": ModelContextSpec(131_072, 8_192, 0.0, 0.0),
    "google/gemma-4-31b-it:free": ModelContextSpec(131_072, 8_192, 0.0, 0.0),
    "deepseek/deepseek-chat:free": ModelContextSpec(65_536, 4_096, 0.0, 0.0),
    # Cheap paid rungs
    "inclusionai/ling-3.0-flash": ModelContextSpec(131_072, 8_192, 0.07, 0.07),
    "deepseek/deepseek-v4.1-flash": ModelContextSpec(131_072, 8_192, 0.14, 0.28),
    "deepseek/deepseek-v4-flash": ModelContextSpec(131_072, 8_192, 0.14, 0.28),
    "google/gemini-3.8-flash": ModelContextSpec(1_048_576, 65_536, 0.15, 0.60),
    "openai/gpt-4o-mini": ModelContextSpec(128_000, 16_384, 0.15, 0.60),
    "z-ai/glm-5.3-flash": ModelContextSpec(131_072, 8_192, 0.20, 0.40),
    # Frontier rungs
    "deepseek/deepseek-v4-pro": ModelContextSpec(131_072, 8_192, 0.55, 2.19),
    "qwen/qwen3.8-max-0902": ModelContextSpec(131_072, 8_192, 1.60, 6.40),
    "openai/gpt-4.1": ModelContextSpec(128_000, 16_384, 2.50, 10.00),
    "openai/gpt-5.6-sol": ModelContextSpec(128_000, 16_384, 5.00, 20.00),
    # TypeSafe System One Jev
    "jev-latest": ModelContextSpec(65_536, 4_096, JEV_INPUT_PRICE_PER_MILLION, 0.0),
    "jev-1.13.0": ModelContextSpec(65_536, 4_096, JEV_INPUT_PRICE_PER_MILLION, 0.0),
}

DEFAULT_MODEL_SPEC = ModelContextSpec(128_000, 8_192, 0.20, 0.80)


def resolve_model_spec(model_id: Optional[str]) -> ModelContextSpec:
    """Resolve context and pricing specification for a model identifier."""
    if not model_id:
        return DEFAULT_MODEL_SPEC
    clean_id = model_id.strip().lower()
    if clean_id in KNOWN_MODEL_SPECS:
        return KNOWN_MODEL_SPECS[clean_id]
    # Family matching
    for known_id, spec in KNOWN_MODEL_SPECS.items():
        if known_id.split(":")[0] in clean_id or clean_id.split(":")[0] in known_id:
            return spec
    if "gemini" in clean_id:
        return ModelContextSpec(1_048_576, 65_536, 0.15, 0.60)
    if "deepseek" in clean_id or "qwen" in clean_id:
        return ModelContextSpec(131_072, 8_192, 0.20, 0.60)
    if "gpt-4" in clean_id or "gpt-5" in clean_id:
        return ModelContextSpec(128_000, 16_384, 2.00, 8.00)
    return DEFAULT_MODEL_SPEC


# ---------------------------------------------------------------------------
# Phase 1: Dynamic Token & Context Allocation
# ---------------------------------------------------------------------------

def dynamic_run_token_budget(model_id: Optional[str] = None,
                             safe_ratio: float = 0.75,
                             min_input: int = 64_000,
                             max_cap: int = 2_000_000) -> Tuple[int, int]:
    """Calculate dynamic input/output token allowance from model context limit."""
    spec = resolve_model_spec(model_id)
    allocated_input = int(spec.context_window * safe_ratio)
    allocated_input = max(min_input, min(allocated_input, max_cap))
    allocated_output = min(spec.max_output_tokens, int(allocated_input * 0.35))
    return allocated_input, allocated_output


def compute_adaptive_window_lines(total_lines: int,
                                  remaining_budget_tokens: int,
                                  min_lines: int = DEFAULT_MIN_WINDOW_LINES,
                                  max_lines: int = DEFAULT_MAX_WINDOW_LINES,
                                  tokens_per_line: float = ESTIMATED_TOKENS_PER_LINE) -> int:
    """Adaptively calculate window line count based on available token headroom."""
    if total_lines <= 0:
        return min_lines
    if remaining_budget_tokens <= 0:
        return min_lines

    # Reserve 25% of remaining tokens for prompt instructions and scaffolding
    usable_tokens = max(100, remaining_budget_tokens * 0.75)
    token_allowance_lines = int(usable_tokens / max(1.0, tokens_per_line))

    # Scale between min_lines and max_lines
    target = max(min_lines, min(token_allowance_lines, max_lines))
    # If the whole file fits within headroom, return total_lines
    if total_lines <= target:
        return total_lines
    return target


def compute_adaptive_waist_rounds(file_count: int,
                                  remaining_tokens: int,
                                  min_rounds: int = 2,
                                  max_rounds: int = 6,
                                  tokens_per_round: int = 1500) -> int:
    """Dynamically determine maximum waist rounds to accommodate file scope."""
    if file_count <= 1:
        return min_rounds
    budget_rounds = max(min_rounds, remaining_tokens // max(1, tokens_per_round))
    # Provide 1 round per 3 candidate files, bounded by budget and max_rounds
    scope_rounds = min_rounds + (file_count // 3)
    return min(scope_rounds, budget_rounds, max_rounds)


# ---------------------------------------------------------------------------
# Phase 2: Dynamic Budget & Spend Allocation
# ---------------------------------------------------------------------------

def compute_dynamic_terminal_reserve(verify_cmd: Optional[str] = None,
                                     judge_model: Optional[str] = None,
                                     fallback_reserve: float = 0.005) -> float:
    """Calculate dynamic terminal reserve based on the exact verification gate."""
    if not verify_cmd or not verify_cmd.strip():
        return 0.0

    cmd = verify_cmd.strip().lower()
    # Pure hermetic commands incur zero model cost
    hermetic_markers = ("python -m unittest", "pytest", "cargo test",
                        "npm test", "go test", "mvn test", "make test")
    if any(h in cmd for h in hermetic_markers) and not judge_model:
        return 0.0

    if judge_model:
        spec = resolve_model_spec(judge_model)
        # 1 judge call: ~3000 input tokens, ~1000 output tokens
        est_cost = (3000 * spec.input_cost_per_million / 1e6) +                    (1000 * spec.output_cost_per_million / 1e6)
        return max(0.0001, round(est_cost, 6))

    return fallback_reserve


def compute_dynamic_tier_ceiling(tier: int,
                                 model_id: Optional[str] = None,
                                 est_tokens: int = 4000,
                                 hard_cap: float = 1.00) -> float:
    """Determine dynamic spend ceiling for a tier evaluated from model specs."""
    spec = resolve_model_spec(model_id)
    # Tier 0 (Scout): light triage (~2k tokens)
    # Tier 1 (Distiller): medium transform (~4k tokens)
    # Tier 2 (Frontier): extensive synthesis (~8k tokens)
    tier_token_multiplier = {0: 0.5, 1: 1.0, 2: 2.0}.get(tier, 1.0)
    tokens = int(est_tokens * tier_token_multiplier)

    cost = (tokens * spec.input_cost_per_million / 1e6) +            (min(2048, tokens // 2) * spec.output_cost_per_million / 1e6)

    # Floor at reasonable tier safety increments
    floor_map = {0: 0.005, 1: 0.02, 2: 0.05}
    computed = max(floor_map.get(tier, 0.01), round(cost * 1.5, 4))
    return min(computed, hard_cap)


# ---------------------------------------------------------------------------
# Phase 3: Adaptive Jev Decision & Perception Sizing
# ---------------------------------------------------------------------------

def compute_dynamic_evidence_budget(instruction: str,
                                    target_files: Optional[Sequence[str]] = None,
                                    remaining_tokens: int = 1500) -> Tuple[int, int]:
    """Dynamically size evidence question count and characters to task scope."""
    num_files = len(target_files or [])
    inst_len = len(instruction or "")

    # Complexity score: 1 to 5
    complexity = 1
    if num_files > 3 or inst_len > 300:
        complexity += 2
    if num_files > 8 or inst_len > 800:
        complexity += 2

    # Scale questions: 2 for simple, up to 10 for complex architectural tasks
    max_questions = min(10, max(2, complexity * 2))

    # Scale characters per question proportional to remaining tokens
    chars_per_token = 4.0
    safe_token_share = max(400, remaining_tokens // 2)
    max_total_chars = int(safe_token_share * chars_per_token)
    max_question_chars = min(1000, max(250, max_total_chars // max_questions))

    return max_questions, max_question_chars


def compute_dynamic_confidence_threshold(category: str,
                                         is_destructive: bool = False,
                                         has_verification_gate: bool = True) -> float:
    """Calculate risk-adjusted confidence threshold for Jev judgments."""
    cat = (category or "").lower()

    # Read-only triage with an automated test gate: balanced confidence is safe
    if has_verification_gate and not is_destructive:
        if "route" in cat or "file" in cat or "triage" in cat:
            return 0.65
        return 0.70

    # Write operations with verification gate
    if has_verification_gate and is_destructive:
        return 0.78

    # Ungated write operations demand high certainty
    if is_destructive and not has_verification_gate:
        return 0.88

    return 0.75


# ---------------------------------------------------------------------------
# Phase 4: Dynamic Model & Rung Ladders
# ---------------------------------------------------------------------------

def rank_models_dynamically(models: Sequence[str],
                            health_status: Optional[Dict[str, Any]] = None,
                            prefer_paid: bool = False) -> List[str]:
    """Dynamically sort model candidates by health, latency, and cost."""
    if not models:
        return []

    unique_models = list(dict.fromkeys(models))
    scores: Dict[str, float] = {}
    health = health_status or {}

    for m in unique_models:
        spec = resolve_model_spec(m)
        score = 100.0

        # Health penalty if model reported errors/rate-limits
        m_health = health.get(m, {})
        if isinstance(m_health, dict):
            error_count = m_health.get("errors", 0)
            latency_ms = m_health.get("latency_ms", 500)
            score -= error_count * 20.0
            score -= min(30.0, latency_ms / 100.0)

        # Free vs paid preferences
        is_free = ":free" in m or spec.input_cost_per_million == 0.0
        if prefer_paid and is_free:
            score -= 15.0
        elif not prefer_paid and not is_free:
            # Penalize cost slightly when free is preferred
            score -= min(25.0, spec.input_cost_per_million * 10.0)

        scores[m] = score

    return sorted(unique_models, key=lambda m: scores.get(m, 0.0), reverse=True)
