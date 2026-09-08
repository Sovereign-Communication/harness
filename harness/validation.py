"""Shared validation for untrusted CLI, MCP, batch, and library inputs.

The command-line parser is not a security boundary: callers can reach the
engine directly and MCP arguments arrive as JSON.  All safety-sensitive
limits therefore pass through these helpers before any model call or file
mutation.
"""
import math
import os

from .errors import HarnessError

MAX_ROUNDS = 20
MAX_TOKENS = 200000
MAX_ROTATIONS = 20
MAX_LINES = 500
MAX_INSTRUCTION_CHARS = 1000
MAX_SNIPPET_CHARS = 2000


def _integer(value, name, minimum, maximum):
    if isinstance(value, bool):
        raise HarnessError(f"{name} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        raise HarnessError(f"{name} must be an integer")
    # Do not silently turn 1.5 into 1 or accept strings with trailing junk.
    if isinstance(value, float) and value != result:
        raise HarnessError(f"{name} must be an integer")
    if result < minimum or result > maximum:
        raise HarnessError(f"{name} must be between {minimum} and {maximum}")
    return result


def bounded_int(value, name, minimum, maximum):
    return _integer(value, name, minimum, maximum)


def finite_number(value, name, minimum=0.0, maximum=None, *, allow_zero=True):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise HarnessError(f"{name} must be a number")
    if not math.isfinite(result):
        raise HarnessError(f"{name} must be finite")
    if not allow_zero and result <= minimum:
        raise HarnessError(f"{name} must be greater than {minimum}")
    if result < minimum or (maximum is not None and result > maximum):
        upper = "" if maximum is None else f", {maximum}"
        raise HarnessError(f"{name} must be in [{minimum}{upper}]")
    return result


def validate_cost(value, name="cost", maximum=None):
    return finite_number(value, name, 0.0, maximum)


def validate_reasoning_effort(value):
    result = str(value or "auto").lower()
    allowed = ("auto", "off", "none", "low", "medium", "high", "on")
    if result not in allowed:
        raise HarnessError(f"reasoning_effort {value!r} is not valid")
    return result


def validate_backend(value):
    result = str(value or "harness")
    if result not in ("harness", "morph", "diff"):
        raise HarnessError("backend must be 'harness', 'morph', or 'diff'")
    return result


def validate_text(value, name, maximum, *, required=False):
    if value is None:
        if required:
            raise HarnessError(f"{name} is required")
        return value
    if not isinstance(value, str):
        raise HarnessError(f"{name} must be a string")
    if required and not value.strip():
        raise HarnessError(f"{name} is required")
    if len(value) > maximum:
        raise HarnessError(f"{name} exceeds {maximum} chars")
    return value


def validate_apply_request(*, max_rounds, max_tokens, task_max_cost,
                           max_rotations, max_lines, instruction,
                           edit_snippet=None, reasoning_effort="auto",
                           backend="harness", hard_task_max_cost=0.25):
    """Validate and normalize the complete apply request."""
    return {
        "max_rounds": bounded_int(max_rounds, "max_rounds", 1, MAX_ROUNDS),
        "max_tokens": bounded_int(max_tokens, "max_tokens", 64, MAX_TOKENS),
        "task_max_cost": finite_number(task_max_cost, "task_max_cost", 0.0,
                                        hard_task_max_cost),
        "max_rotations": bounded_int(max_rotations, "max_rotations", 0, MAX_ROTATIONS),
        "max_lines": bounded_int(max_lines, "max_lines", 1, MAX_LINES),
        "instruction": validate_text(instruction, "instruction",
                                      MAX_INSTRUCTION_CHARS, required=True),
        "edit_snippet": validate_text(edit_snippet, "edit_snippet",
                                       MAX_SNIPPET_CHARS),
        "reasoning_effort": validate_reasoning_effort(reasoning_effort),
        "backend": validate_backend(backend),
    }


def validate_batch_files(files):
    if not isinstance(files, (list, tuple)) or not files:
        raise HarnessError("file must contain at least one path")
    result = []
    for path in files:
        if not isinstance(path, str) or not path.strip():
            raise HarnessError("every file path must be a non-empty string")
        result.append(os.path.abspath(path))
    return result


def validate_mcp_prompt(value):
    """Validate the required prompt at the MCP trust boundary."""
    return validate_text(value, "prompt", 100000, required=True)


def validate_mcp_task(value):
    """Validate the required work-item description at the MCP boundary."""
    return validate_text(value, "task", 100000, required=True)


def validate_mcp_task_id(value):
    """Validate the required task identity for MCP lifecycle operations."""
    return validate_text(value, "task_id", 512, required=True)


def validate_mcp_limit(value, name="limit", default=20):
    """Validate bounded positive pagination values before ledger slicing."""
    if value is None:
        value = default
    return bounded_int(value, name, 1, 1000)


def validate_mcp_max_tokens(value, default=300):
    """Validate the optional MCP output-token budget."""
    if value is None:
        value = default
    return bounded_int(value, "max_tokens", 1, MAX_TOKENS)


def validate_mcp_csv(value, name):
    """Validate an optional comma-separated model list at the MCP boundary."""
    if value is None:
        return None
    text = validate_text(value, name, 10000, required=True)
    models = [item.strip() for item in text.split(",")]
    if any(not model for model in models):
        raise HarnessError(f"{name} must contain non-empty model ids")
    return models


def validate_mcp_model(value, name="model"):
    """Validate an optional MCP model identifier."""
    return validate_text(value, name, 512, required=True) if value is not None else None


def validate_mcp_reasoning(value):
    """Validate the MCP reasoning-effort enum."""
    if value is None:
        return "auto"
    if not isinstance(value, str):
        raise HarnessError("reasoning_effort must be a string")
    return validate_reasoning_effort(value)


def validate_mcp_bool(value, name):
    """Validate a JSON boolean rather than applying Python truthiness."""
    if not isinstance(value, bool):
        raise HarnessError(f"{name} must be a boolean")
    return value


def validate_mcp_files(value):
    """Normalize the MCP file argument to the engine's batch contract."""
    if isinstance(value, str):
        value = [value]
    return validate_batch_files(value)
