"""MCP tool schemas: pure data, no logic.

This module exists so the protocol adapter (harness/mcp.py) reads as
framing + lanes + dispatch. The dicts are byte-for-byte the tool contracts
previously inlined in McpServer._tools(); the single consumer imports
TOOL_SCHEMAS and returns it. Keep this module free of imports and code --
schemas only.
"""

TOOL_SCHEMAS = [
    {
        "name": "panel_verify",
        "title": "Multi-model verification",
        "description": "Panel of cheap/free models answers a self-contained question, then a judge "
                       "synthesizes a structured verdict (agreement, confidence, disagreements, "
                       "defer). Cost-bounded. No web/file tools by design.",
        "inputSchema": {"type": "object", "properties": {
            "prompt": {"type": "string", "description": "Self-contained question + context"},
            "panel": {"type": "string", "description": "Comma-separated model pool. Defaults to configured panel pool."},
            "judge": {"type": "string", "description": "Judge model id. Defaults to configured judge."},
            "max_tokens": {"type": "integer", "default": 2048,
                           "description": "Per-call output budget. The vote lane enforces its "
                                          "own minimum (4096) and the judge/specialist lanes "
                                          "theirs (8192); an explicit value here wins."},
            "reasoning_effort": {"type": "string", "enum": ["auto", "off", "none", "low", "medium", "high", "on"]},
            "converge": {"type": "boolean", "description": "Run the convergence specialist on per-claim votes (requires per-claim JSON panel output)"},
            "convergence_model": {"type": "string", "description": "Primary specialist model (default: judge)"},
            "specialist_pool": {"type": "string", "description": "Comma-separated specialist fallback ladder, strongest first (default: configured pool)"},
            "task_id": {"type": "string"},
            "task_max_cost": {"type": "number", "minimum": 0, "maximum": 0.25,
                              "description": "Optional per-call cost ceiling (USD), capped at HARD_TASK_MAX_COST"},
        }, "required": ["prompt"]},
    },
    {
        "name": "apply_edit",
        "title": "Scoped code edit with verification",
        "description": "Make a single, scoped (<500-line file) code change, run a verification "
                       "gate, retry up to max_rounds, renew consent each round, defer instead of "
                       "guessing at the capability limit, and rotate models on error. Returns a "
                       "continuation state when deferred.",
        "inputSchema": {"type": "object", "properties": {
            "file": {"anyOf": [
                         {"type": "string"},
                         {"type": "array", "items": {"type": "string"},
                          "minItems": 1},
                     ],
                     "description": "Path to a file, or paths for a multi-file batch "
                                    "(one governed session per file, shared task budget, "
                                    "fail-fast)"},
            "instruction": {"type": "string", "description": "What to change (<=1000 chars)"},
            "edit_snippet": {"type": "string", "description": "Intent anchor snippet (<=2000 chars)"},
            "verify_cmd": {"type": "string", "description": "Shell command gate, e.g. 'cargo check' (requires server allow_verify)"},
            "allow_verify": {"type": "boolean", "description": "Explicit confirmation to run a verify gate in this request (required when allow_verify is not enabled server-side)"},
            "max_rounds": {"type": "integer", "default": 3, "minimum": 1, "maximum": 20},
            "max_tokens": {"type": "integer", "minimum": 64, "maximum": 200000,
                            "description": "Maximum output tokens per model call"},
            "require_consent": {"type": "boolean", "description": "Ask the model if it accepts the work first"},
            "renew_consent": {"type": "boolean", "description": "Re-check consent before each round (continued consensus)"},
            "require_diff_authorization": {"type": "boolean", "description": "An independent verifier model must allow the EXACT resulting content before every write (diff-bound attestation, fail-closed)"},
            "max_rotations": {"type": "integer", "minimum": 0, "maximum": 20,
                               "description": "How many model rotations to allow on error"},
            "allow_escalation": {"type": "boolean", "description": "Permit escalation to the configured stronger model"},
            "reasoning_effort": {"type": "string", "enum": ["auto", "off", "none", "low", "medium", "high", "on"]},
            "model": {"type": "string", "description": "Explicit model override; otherwise the corrected apply route is used"},
            "backend": {"type": "string", "enum": ["harness", "morph", "diff"], "default": "harness", "description": "morph: MorphLite-compatible structured editing; diff: strict unified-diff editing (no file-size ceiling)"},
            "verify_only": {"type": "boolean", "description": "Return the proposal without writing or running the verification gate"},
            "max_lines": {"type": "integer", "default": 500, "minimum": 1, "maximum": 500,
                           "description": "Per-file line ceiling (1-500)"},
            "task_max_cost": {"type": "number", "description": "Per-task cost ceiling"},
            "allow_write": {"type": "boolean", "description": "Explicit confirmation that this MCP request may write files"},
            "continuation": {"type": "object", "description": "State from a deferred run to resume"},
            "task_id": {"type": "string"},
        }, "anyOf": [
            {"required": ["instruction"]},
            {"required": ["continuation"]},
        ]},
    },
    {
        "name": "offer_work",
        "title": "Ask for consent on a work item",
        "description": "Probe whether a model accepts, declines, defers, or redirects a work item. "
                       "Dispatch is allowed only on 'accept'.",
        "inputSchema": {"type": "object", "properties": {
            "task": {"type": "string", "description": "Work item description"},
            "task_id": {"type": "string"},
            "model": {"type": "string", "description": "Model to ask (defaults to configured judge)"},
            "context": {"type": "string"},
        }, "required": ["task"]},
    },
    {
        "name": "defer_work",
        "title": "Revoke consent mid-task",
        "description": "A model (or operator) may defer/revoke consent at any point. Partial work is "
                       "preserved; the task returns to the queue with the reason recorded. This is the "
                       "continued-consensus hook: the sovereign model can call it to stop work.",
        "inputSchema": {"type": "object", "properties": {
            "task_id": {"type": "string"},
            "reason": {"type": "string"},
            "category": {"type": "string", "description": "e.g. capability, consent, alignment"},
        }, "required": ["task_id"]},
    },
    {
        "name": "ledger_status",
        "title": "Autonomy ledger",
        "description": "Tail of the append-only, hash-chained autonomy/participation ledger, plus "
                       "chain-integrity status.",
        "inputSchema": {"type": "object", "properties": {
            "limit": {"type": "integer", "default": 20, "minimum": 1},
        }},
    },
    {
        "name": "participation_report",
        "title": "Autonomy & participation metrics",
        "description": "Aggregate metrics: offers, accept/decline/defer/redirect rates, completions, "
                       "deferral points, per-model participation, and a degenerate-consent flag.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "spend_status",
        "title": "Key & spend status",
        "description": "OpenRouter key identity, spend limit, remaining balance, and harness session spend.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "trust_status",
        "title": "Trust & correctness standing",
        "description": "Bipolar trust scores (-11 extreme distrust, 0 unknown, +11 extreme "
                       "trust) for the calling host and optionally one model, plus the "
                       "correctness level that rations spend ceilings. Read-only.",
        "inputSchema": {"type": "object", "properties": {
            "model": {"type": "string", "description": "Model id to score (defaults to none: host only)"},
        }},
    },
    {
        "name": "plan_and_execute",
        "title": "Autonomous DAG task planning and sliding-scale execution",
        "description": "Decompose a high-level goal into an executable Directed Acyclic Graph (DAG), "
                       "classify subtask complexity into sliding-scale model tiers (Scout/Distiller/Frontier), "
                       "and optionally execute independent subtasks concurrently with file mutual exclusion.",
        "inputSchema": {"type": "object", "properties": {
            "goal": {"type": "string", "description": "High-level goal or instruction to plan and accomplish"},
            "execute": {"type": "boolean", "default": False, "description": "Execute the DAG if true; preview/plan only if false"},
            "parallel": {"type": "boolean", "default": True, "description": "Execute independent subtasks concurrently in parallel (default: on; isolation per the session's hourglass_isolate setting)"},
            "max_workers": {"type": "integer", "default": 4, "minimum": 1, "maximum": 16},
            "frontier_model": {"type": "string", "description": "Frontier model or alias for Tier 2 nodes (e.g. fable-5.1, gpt-6)"},
            "decompose_llm": {"type": "boolean", "default": True,
                              "description": "Author the DAG with the cheapest tier-appropriate model "
                                             "(schema-validated; default: on when the hourglass is active; "
                                             "heuristic fallback when execute=true)"},
            "confirm": {"type": "boolean", "default": True, "description": "Confirm the plan at the frontier waist before execution (brief + bounded file-window rounds; approve/amend/split/refuse verdict; a refused plan never executes). Default: on"},
            "plan_consensus": {"type": "boolean", "default": False,
                               "description": "Cheap plan-soundness check before the waist "
                                              "(JSON sound/reasons; unsound forces the waist "
                                              "amend path; ledgered plan_consensus)"},
            "final_gate": {"anyOf": [{"type": "boolean"}, {"type": "string"}],
                           "description": "Post-DAG final verification gate: false disables, "
                                          "true/auto uses a discovered/declared verify command, "
                                          "or pass an explicit command string"},
            "require_diff_authorization": {"type": "boolean", "default": True, "description": "An independent verifier model must allow the exact resulting content of every node write before it lands (default: on)"},
            "file": {"anyOf": [
                         {"type": "string"},
                         {"type": "array", "items": {"type": "string"}, "minItems": 1},
                     ],
                     "description": "Optional allowed file paths"},
            "max_cost": {"type": "number", "minimum": 0, "maximum": 0.25},
            "allow_write": {"type": "boolean", "description": "Confirm permission to write files when execute=true"},
        }, "required": ["goal"]},
    },
]
