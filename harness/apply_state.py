"""Data contracts for one apply request and its mutable run.

The apply engine owns the transaction; these records own the data crossing
its request, attempt, and gate phases.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ApplyRequest:
    """Validated, immutable inputs for one apply operation."""
    task_id: str
    file_path: str
    instruction: str
    edit_snippet: object
    verify_cmd: object
    backend: str
    verify_only: bool
    max_lines: int
    max_rounds: int
    max_tokens: int
    task_max_cost: float
    max_rot: int
    reasoning: str
    renew: bool
    allow_escalation: object
    model: str
    ordered: object
    profiles: object
    want_consent: bool
    original: str
    task_start_spent: float
    continuation: dict
    continuation_gate: object
    task_runner: object
    cancel_check: object
    trust_combined: int = 0
    trust_correctness: int = 0


@dataclass
class RunState:
    """Mutable state for one apply transaction, shared by its phases."""
    rounds: list
    history: list
    current_content: str
    round_no: int = 0
    round_ctx: object = None
    gate_broken: bool = False
    candidates: list = field(default_factory=list)
    failed_models: set = field(default_factory=set)
    deferred_models: dict = field(default_factory=dict)
    rotations: int = 0
    backup: object = None
    consent_attempts: list = field(default_factory=list)
    # Auto-escalation / de-escalation state
    escalation_condensed_context: str = ""
    de_escalation_target_rung: int = 0


@dataclass
class AttemptOutcome:
    """Normalized result of one candidate dispatch and any rotations."""
    model: object = None
    model_used: object = None
    content: object = None
    cost: float = 0.0
    ready: str = "missing"
    resp: object = None
    last_error: object = None
    last_defer_reason: object = None
