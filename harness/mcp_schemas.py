"""MCP tool schemas: pure data, no logic.

This module exists so the protocol adapter (harness/mcp.py) reads as
framing + lanes + dispatch. The dicts are byte-for-byte the tool contracts
previously inlined in McpServer._tools(); the single consumer imports
TOOL_SCHEMAS and returns it. Keep this module free of imports and code --
schemas only.
"""

TOOL_SCHEMAS = [
    {
        "name": "ask",
        "title": "Answer an ordinary request",
        "description": (
            "When: an ordinary conversational request or a short follow-up to "
            "the shared session. "
            "In: prompt plus optional session_id. "
            "Out: one lifecycle answer; never repository-write scope."),
        "inputSchema": {"type": "object", "properties": {
            "prompt": {"type": "string", "description": "Question or short ordinary request"},
            "session_id": {"type": "string", "description": "Optional shared chat-history session id"},
        }, "required": ["prompt"]},
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
    {
        "name": "site_check",
        "title": "Check one public site's availability",
        "description": (
            "When: a current public-site availability question. "
            "In: prompt plus optional session_id. "
            "Out: a Jev-selected workflow with at most one bounded HTTPS "
            "probe and a judged response; no page-content research or "
            "mutation."),
        "inputSchema": {"type": "object", "properties": {
            "prompt": {"type": "string",
                       "description": "Natural-language request to check one public site"},
            "session_id": {"type": "string",
                          "description": "Optional chat-history session id"},
        }, "required": ["prompt"]},
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
    {
        "name": "panel_verify",
        "title": "Multi-model verification",
        "description": (
            "When: a self-contained question needs multi-model agreement and "
            "a structured verdict. "
            "In: prompt plus optional panel, judge, max_tokens, "
            "reasoning_effort, converge, convergence_model, specialist_pool, "
            "task_id, and task_max_cost. "
            "Out: agreement, confidence, disagreements, and defer within the "
            "cost bound; no web or file tools."),
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
        "description": (
            "When: one scoped code change needs a verified edit loop. "
            "In: file, instruction, and optional edit_snippet plus gate, "
            "budget, consent, and backend controls. "
            "Out: the applied change or a continuation state when deferred, "
            "after verification, consent renewal, and model rotation on "
            "error."),
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
        "description": (
            "When: a work item needs a model's accept, decline, defer, or "
            "redirect decision. "
            "In: the work-item fields declared by the schema. "
            "Out: the model's choice; dispatch is allowed only on accept."),
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
        "description": (
            "When: a model or operator defers or revokes consent mid-task. "
            "In: the task and reason fields declared by the schema. "
            "Out: partial work preserved, the task returned to the queue with "
            "the reason recorded, and work stopped."),
        "inputSchema": {"type": "object", "properties": {
            "task_id": {"type": "string"},
            "reason": {"type": "string"},
            "category": {"type": "string", "description": "e.g. capability, consent, alignment"},
        }, "required": ["task_id"]},
    },
    {
        "name": "ledger_status",
        "title": "Autonomy ledger",
        "description": (
            "When: a caller needs the tail and integrity of the "
            "autonomy/participation ledger. "
            "In: the optional tail and filter fields declared by the schema. "
            "Out: recent append-only, hash-chained records plus chain- "
            "integrity status."),
        "inputSchema": {"type": "object", "properties": {
            "limit": {"type": "integer", "default": 20, "minimum": 1},
        }},
    },
    {
        "name": "participation_report",
        "title": "Autonomy & participation metrics",
        "description": (
            "When: a caller needs aggregate consent and participation "
            "metrics. "
            "In: the optional window and filter fields declared by the "
            "schema. "
            "Out: offers, accept/decline/defer/redirect rates, completions, "
            "deferral points, per-model participation, and a degenerate- "
            "consent flag."),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "spend_status",
        "title": "Key & spend status",
        "description": (
            "When: a caller needs key identity and spend standing. "
            "In: no inputs beyond the request. "
            "Out: OpenRouter key identity, spend limit, remaining balance, "
            "and harness session spend."),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "trust_status",
        "title": "Trust & correctness standing",
        "description": (
            "When: a caller needs trust and correctness standing. "
            "In: the optional host and model selectors declared by the "
            "schema. "
            "Out: bipolar trust scores from -11 to +11 plus the correctness "
            "level that rations spend ceilings; read-only."),
        "inputSchema": {"type": "object", "properties": {
            "model": {"type": "string", "description": "Model id to score (defaults to none: host only)"},
        }},
    },
    {
        "name": "plan_and_execute",
        "title": "Autonomous DAG task planning and sliding-scale execution",
        "description": (
            "When: a high-level goal needs decomposition into an executable "
            "DAG. "
            "In: the goal plus optional planning, tier, budget, and execution "
            "controls. "
            "Out: the classified DAG and, when requested, concurrently "
            "executed independent subtasks with file mutual exclusion."),
        "inputSchema": {"type": "object", "properties": {
            "goal": {"type": "string", "description": "High-level goal or instruction to plan and accomplish"},
            "root": {"type": "string", "description": "Selected workspace inside configured MCP allowed roots; required for execution or file access"},
            "execute": {"type": "boolean", "default": False, "description": "Execute the DAG if true; preview/plan only if false"},
            "parallel": {"type": "boolean", "default": True, "description": "Execute independent subtasks concurrently in parallel (default: on; isolation per the session's hourglass_isolate setting)"},
            "max_workers": {"type": "integer", "default": 4, "minimum": 1, "maximum": 16},
            "frontier_model": {"type": "string", "description": "Frontier model or alias for Tier 2 nodes (e.g. gpt-6, qwen-max)"},
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
                     "description": "Optional source or target paths, all confined to root"},
            "stages": {
                "anyOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}, "minItems": 1},
                ],
                "description": "Subset of pipeline stages to execute (context, planning, execution, verification)",
            },
            "brief": {
                "anyOf": [
                    {"type": "object"},
                    {"type": "string"},
                ],
                "description": "Supplied context brief artifact or JSON pack file path inside root",
            },
            "token_budget_input": {
                "type": "integer",
                "minimum": 1024,
                "description": "Override input token allowance ceiling for the composed run",
            },
            "token_budget_output": {
                "type": "integer",
                "minimum": 256,
                "description": "Override output token allowance ceiling for the composed run",
            },
            "max_cost": {"type": "number", "minimum": 0, "maximum": 0.25},
            "allow_write": {"type": "boolean", "description": "Confirm permission to write files when execute=true"},
        }, "required": ["goal"]},
    },
    {
        "name": "issue_sort",
        "title": "Sort an issue into an operator-declared bucket",
        "description": (
            "When: operator-declared issue buckets must be matched without "
            "invention. "
            "In: the issue text plus the operator bucket pack. "
            "Out: operator-only labels, with unkeyed, transport-failed, or "
            "out-of-pack results marked is_fallback and no match yielding "
            "bucket=null."),
        "inputSchema": {"type": "object", "properties": {
            "issue": {"type": "string", "description": "Issue or deferral note text to sort"},
            "pack": {"type": "object",
                     "description": "Operator bucket pack: {id, buckets: {id: {label, "
                                    "kind: trouble_area|alternate_path|orchestration_driver, "
                                    "path_id, keywords[], suggested_next_action, attention}}}"},
            "task_id": {"type": "string"},
        }, "required": ["issue", "pack"]},
    },
    {
        "name": "source_audit",
        "title": "Run a resumable full-source Jev audit",
        "description": (
            "When: an opt-in, cost-capped full-source audit over a Git "
            "snapshot is requested. "
            "In: operator code, documentation, and evidence/data packs plus "
            "the cost ceiling and resume options. "
            "Out: a resumable manifest of paths, hashes, line ranges, typed "
            "results, usage receipts, and lead dispositions; never source "
            "text."),
        "inputSchema": {"type": "object", "properties": {
            "root": {"type": "string",
                     "description": "Selected Git repository root inside configured allowed roots"},
            "code_pack": {"type": "object",
                          "description": "Operator repo-summary pack for code/configuration"},
            "docs_pack": {"type": "object",
                          "description": "Operator repo-summary pack for documentation"},
            "evidence_pack": {"type": "object",
                              "description": "Operator repo-summary pack for evidence/data"},
            "run_budget": {"type": "number", "minimum": 0.000001, "maximum": 0.25,
                            "description": "Required cumulative USD ceiling for this audit run"},
            "chunk_chars": {"type": "integer", "default": 4000,
                            "minimum": 512, "maximum": 8000},
            "state_path": {"type": "string",
                           "description": "SQLite resume state path inside the selected repository"},
            "report_path": {"type": "string",
                            "description": "Optional source-safe JSON report path inside the selected repository"},
            "dispositions": {"type": "object",
                             "description": "Optional lead-id map to verified, false_positive, historical_fixture, or unresolved"},
            "task_id": {"type": "string"},
            "allow_write": {"type": "boolean",
                            "description": "Explicit permission to persist resume state and optional report"},
        }, "required": ["code_pack", "docs_pack", "evidence_pack",
                        "run_budget", "state_path", "allow_write"]},
        "annotations": {"readOnlyHint": False, "openWorldHint": True},
    },
    {
        "name": "route_query",
        "title": "Route a query onto the declared model ladder",
        "description": (
            "When: a request needs the cheapest capable rung from the "
            "declared ladder. "
            "In: the request plus the declared rung ladder. "
            "Out: a declared rung id, or rung=null when unsatisfiable; "
            "unkeyed, transport-failed, or out-of-ladder results are marked "
            "is_fallback and never invent rungs or model ids."),
        "inputSchema": {"type": "object", "properties": {
            "goal": {"type": "string", "description": "The user request to route"},
            "pack": {"type": "object",
                     "description": "Operator route pack: {id, rungs: [{rung_id, tier: "
                                    "T0|T1|T2|T3, model, cost_class: free|cheap|moderate|"
                                    "expensive|premium, observed_success?, samples?, "
                                    "guidance?, notes?}]}"},
            "task_id": {"type": "string"},
        }, "required": ["goal", "pack"]},
    },
    {
        "name": "mission_status",
        "title": "Mission pack status (read-only)",
        "description": (
            "When: a caller needs machine-readable HUL-A mission pack state. "
            "In: the mission pack selector declared by the schema. "
            "Out: regenerated STATUS.md and INDEX.md summaries with phase, "
            "dual budget, receipts, and resume state, without mutating "
            "progress, spend, or resume status."),
        "inputSchema": {"type": "object", "properties": {
            "mission_id": {"type": "string", "description": "Mission id (pack directory name)"},
            "root": {"type": "string", "default": "missions",
                     "description": "Mission pack root directory"},
        }, "required": ["mission_id"]},
    },
    {
        "name": "continue_work",
        "title": "Resume a deferred/failed apply from its continuation state",
        "description": (
            "When: a gated apply must resume from saved continuation state. "
            "In: the continuation state plus allow_write and an allowed "
            "target root. "
            "Out: the resumed apply result; refuses without allow_write or "
            "when the target is outside an allowed root."),
        "inputSchema": {"type": "object", "properties": {
            "continuation": {"type": "object", "description": "Saved continuation state "
                             "from a deferred/failed apply_edit or `harness apply`"},
            "instruction": {"type": "string", "description": "Instruction override (<=1000 chars)"},
            "verify_cmd": {"type": "string", "description": "Verify command override "
                          "(requires server allow_verify; defaults to the continuation's own gate)"},
            "allow_verify": {"type": "boolean", "description": "Explicit confirmation to run a "
                             "verify gate in this request (required when allow_verify is not "
                             "enabled server-side)"},
            "max_rounds": {"type": "integer", "default": 3, "minimum": 1, "maximum": 20},
            "allow_write": {"type": "boolean", "description": "Explicit confirmation that this "
                            "MCP request may write files"},
            "task_id": {"type": "string"},
        }, "required": ["continuation"]},
    },
    {
        "name": "script_run",
        "title": "Run one Python script inside an allowed workspace",
        "description": (
            "When: governed scripting is enabled and a caller needs to run "
            "source under a declared root. "
            "In: source, root, consent_sha, and optional timeout and Jev- "
            "alignment fields, with HARNESS_SCRIPT_ENABLED=1. "
            "Out: a receipt with exit code, stdout, stderr, sha, and duration "
            "after a scrubbed-environment run with process-tree cleanup; "
            "refuses when consent_sha does not match sha256(source)."),
        "inputSchema": {"type": "object", "properties": {
            "source": {"type": "string",
                       "description": "Python source to run (<=200,000 chars)"},
            "root": {"type": "string",
                     "description": "Workspace root; must be one of the allowed roots"},
            "allow_exec": {"type": "boolean",
                           "description": "Explicit consent to execute this source"},
            "consent_sha": {"type": "string",
                            "description": "sha256 of source; approval for different "
                                           "bytes is refused"},
            "goal": {"type": "string",
                     "description": "What the script is for (<=2000 chars)"},
            "timeout_s": {"type": "integer", "minimum": 1, "maximum": 120},
            "score": {"type": "boolean",
                      "description": "Ask Jev to score alignment with goal "
                                     "(default: true)"},
            "task_id": {"type": "string"},
        }, "required": ["source", "root", "allow_exec", "consent_sha"]},
        "annotations": {"readOnlyHint": False, "destructiveHint": True,
                        "openWorldHint": True},
    },
    {
        "name": "log_judgment",
        "title": "Single-pass log-factor analysis against a frozen operator pack",
        "description": (
            "When: code-extracted log items need one owner's Jev judgment and "
            "aggregation. "
            "In: the operator bucket pack plus the extracted items. "
            "Out: judged bucket choices and scores aggregated into a JSON "
            "artifact, with unmatched items and fallback counts reported, "
            "never smoothed."),
        "inputSchema": {"type": "object", "properties": {
            "log_text": {"type": "string", "description": "Raw runtime log dump text"},
            "pack": {"type": "object",
                     "description": "Frozen operator log pack: P5 bucket pack + one "
                                    "score block {id, instructions, levels[]}"},
            "info_sample": {"type": "integer", "minimum": 0,
                            "description": "Also judge every Nth INFO record (0 = WARN/ERROR only)"},
            "task_id": {"type": "string"},
        }, "required": ["log_text", "pack"]},
    },
    {
        "name": "lucas_gate_v2",
        "title": "Lucas Gate v2 typed-track scorecard runner",
        "description": (
            "When: a caller has a typed-track Lucas Gate v2 assessment to "
            "validate deterministically. "
            "In: scores and evidence for applicable categories plus explicit "
            "reasons for non-applicable ones. "
            "Out: a typed pass or fail against the declared bars and bias "
            "floors; never calls Jev or a model and does not persist "
            "evidence."),
        "inputSchema": {"type": "object", "additionalProperties": False,
            "properties": {
                "track": {"type": "string", "enum": ["typed"]},
                "artifact_text": {"type": "string", "minLength": 1,
                                  "maxLength": 100000},
                "overall_average": {"type": "number", "minimum": 0, "maximum": 10},
                "actual_risk": {"type": "boolean"},
                "investigated_reason": {"type": "string", "maxLength": 4000},
                "category_scores": {"type": "array", "minItems": 10, "maxItems": 10,
                    "items": {"type": "object", "additionalProperties": False,
                        "properties": {
                            "id": {"type": "string", "enum": [
                                "1", "2", "3", "4", "5", "6", "8", "9", "10", "11"]},
                            "applicable": {"type": "boolean"},
                            "not_applicable_reason": {
                                "type": "string", "minLength": 1, "maxLength": 4000},
                            "score": {"type": "number", "minimum": 0, "maximum": 10},
                            "category_average": {
                                "type": "number", "minimum": 0, "maximum": 10},
                            "evidence": {"type": "string", "minLength": 1,
                                         "maxLength": 4000},
                            "precedent": {"type": "string", "minLength": 1,
                                          "maxLength": 4000},
                        },
                        "required": ["id"],
                        "allOf": [{
                            "if": {"properties": {"applicable": {"const": False}},
                                   "required": ["applicable"]},
                            "then": {"required": ["not_applicable_reason"],
                                     "not": {"anyOf": [
                                         {"required": ["score"]},
                                         {"required": ["category_average"]},
                                         {"required": ["evidence"]},
                                         {"required": ["precedent"]},
                                     ]}},
                            "else": {"required": ["score", "category_average",
                                                  "evidence", "precedent"],
                                     "not": {"required": ["not_applicable_reason"]}},
                        }],
                    }},
                "bias_checks": {"type": "array", "minItems": 8, "maxItems": 8,
                    "items": {"type": "object", "additionalProperties": False,
                        "properties": {
                            "id": {"type": "string", "enum": [
                                "B1", "B3", "B4", "B5", "B8", "B9", "B10", "B11"]},
                            "score": {"type": "number", "minimum": 0, "maximum": 10},
                            "rationale": {"type": "string", "minLength": 1,
                                          "maxLength": 4000},
                        }, "required": ["id", "score", "rationale"]}},
            },
            "required": ["track", "artifact_text", "actual_risk",
                         "category_scores", "bias_checks"]},
        "annotations": {"readOnlyHint": True, "destructiveHint": False,
                        "openWorldHint": False},
    },
    {
        "name": "jev_phase",
        "title": "JEV completion bar (local-only, read-only)",
        "description": (
            "When: a caller needs phase or board completion scoring locally. "
            "In: the phase id or board selection. "
            "Out: hard gates plus sentiment bucket scores; always local-only, "
            "never calls a live Jev judge, and never mutates STATUS or the "
            "repo."),
        "inputSchema": {"type": "object", "properties": {
            "repo_root": {"type": "string",
                          "description": "Workspace inside configured MCP allowed roots; defaults only when exactly one root is configured"},
            "phase": {"type": "string", "description": "Phase id, e.g. DF-UI-2 "
                     "(required unless 'all' is set)"},
            "all": {"type": "boolean", "default": False,
                    "description": "Score every declared phase contract"},
            "min_score": {"type": "number", "minimum": 0, "maximum": 100,
                          "description": "Completion score threshold (default 85.0)"},
        }, "anyOf": [
            {"required": ["phase"]},
            {"required": ["all"],
             "properties": {"all": {"const": True}}},
        ]},
    },
    {
        "name": "jev_freeze",
        "title": "Freeze JEV model pin + confidence threshold",
        "description": (
            "When: a caller needs to preview or persist the Jev settings "
            "freeze. "
            "In: persist=false for preview, or persist=true plus allow_write "
            "to write the operator config. "
            "Out: the pin and threshold the freeze would apply, or the "
            "written config; refuses fail-closed when the environment pins "
            "the same key."),
        "inputSchema": {"type": "object", "properties": {
            "model": {"type": "string",
                      "description": "Model id to pin (e.g. jev-1.13.0); omit "
                                     "to keep the current pin"},
            "min_confidence": {"type": "number", "minimum": 0, "maximum": 1,
                               "description": "Abstain/escalate threshold; omit "
                                              "to keep the current value"},
            "persist": {"type": "boolean", "default": False,
                        "description": "Write the named keys into the operator "
                                       "config.json (requires allow_write)"},
            "allow_write": {"type": "boolean",
                            "description": "Explicit confirmation for a config "
                                           "write in this request"},
            "task_id": {"type": "string"},
        }},
    },
    {
        "name": "dogfood",
        "title": "Three-phase self-hosting dogfood audit and gated repair",
        "description": (
            "When: the self-hosting ground-verify-apply loop must run over "
            "claims and a source window. "
            "In: the claims and source-window fields declared by the schema. "
            "Out: the dogfood loop result."),
        "inputSchema": {"type": "object", "properties": {
            "root": {"type": "string",
                     "description": "Selected workspace inside configured MCP allowed roots"},
            "file": {"type": "string",
                     "description": "Target file to repair under apply gate"},
            "instruction": {"type": "string",
                            "description": "Apply repair instruction"},
            "verify": {"type": "string",
                       "description": "Verify gate command for apply phase"},
            "claims_file": {"type": "string",
                            "description": "Path to claims JSON file"},
            "source_file": {"type": "string",
                            "description": "Path to source text window file"},
            "definitions_file": {"type": "string",
                                 "description": "Optional path to definitions JSON file"},
            "claim_context": {"type": "string",
                              "description": "Optional context override string"},
            "max_cost": {"type": "number", "minimum": 0, "maximum": 0.25},
            "max_rounds": {"type": "integer", "minimum": 1, "maximum": 8, "default": 3},
            "allow_write": {"type": "boolean",
                            "description": "Confirm permission to write target file"},
            "task_id": {"type": "string"},
        }, "required": ["file", "instruction"]},
    },
    {
        "name": "generate_brief",
        "title": "Build an evidence-bearing context pack",
        "description": (
            "When: a goal needs a grounded HV-2 context pack from source "
            "files. "
            "In: the goal and source scope. "
            "Out: exact cited windows, token estimates, freshness checks, and "
            "tracked omissions; hermetic, with no network or model call."),
        "inputSchema": {"type": "object", "properties": {
            "goal": {"type": "string", "description": "High-level goal or instruction to brief"},
            "root": {"type": "string",
                     "description": "Selected workspace inside configured MCP allowed roots when source files are supplied"},
            "files": {"type": "array", "items": {"type": "string"},
                      "description": "Source file paths to inspect and cite"},
            "budget": {"type": "integer", "default": 48000,
                       "description": "Maximum character budget for cited windows (default: 48000)"},
            "freshness": {"type": "boolean", "default": False,
                          "description": "Evaluate and attach source freshness report"},
            "render": {"type": "boolean", "default": False,
                       "description": "Render markdown prose representation instead of raw JSON"},
        }, "required": ["goal"]},
    },
    {
        "name": "driver_step",
        "title": "Run verified extraction, Jev decision, and action step",
        "description": (
            "When: one verified extraction, Jev decision, and deterministic "
            "action step must run through driver-core. "
            "In: the target, schema, and optional consent and parameters. "
            "Out: an envelope with ok=true on execution or ok=false on "
            "refusal at HTTP 200; only transport failures raise."),
        "inputSchema": {"type": "object", "properties": {
            "target": {"type": "string", "description": "Target window, process, URL or selector"},
            "schema": {"type": "string", "enum": ["cli", "dom", "gui", "screen", "mcp"],
                       "description": "Extraction schema (default: 'cli')"},
            "action": {"type": "string", "description": "Declared action to consent to (optional)"},
            "params": {"type": "object", "description": "Action parameter object (optional)"},
            "by": {"type": "string", "description": "Consent grantor label (default: the MCP peer, e.g. mcp:<client>). 'operator' needs allow_write=true"},
            "require_stable": {"type": "boolean", "default": True,
                               "description": "Require stable state before execution"},
            "task_id": {"type": "string"},
        }, "required": ["target"]},
    },
    {
        "name": "driver_health",
        "title": "Check driver-core service status and declared sources",
        "description": (
            "When: a caller needs driver-core liveness and declared "
            "perception sources. "
            "In: no inputs beyond the request. "
            "Out: liveness, version, and declared perception sources."),
        "inputSchema": {"type": "object", "properties": {
            "task_id": {"type": "string"},
        }},
    },
    {
        "name": "driver_vocabulary",
        "title": "List driver-core declared action vocabulary",
        "description": (
            "When: a caller needs driver-core's closed action vocabulary. "
            "In: no inputs beyond the request. "
            "Out: the closed 14-action vocabulary, tier mutability, and "
            "normalizers."),
        "inputSchema": {"type": "object", "properties": {
            "task_id": {"type": "string"},
        }},
    },
    {
        "name": "driver_verify",
        "title": "Verify cryptographic audit chain and spend budget",
        "description": (
            "When: a caller needs the driver audit log's chain integrity and "
            "spend totals. "
            "In: no inputs beyond the request. "
            "Out: the hash-chain verdict and spend totals."),
        "inputSchema": {"type": "object", "properties": {
            "task_id": {"type": "string"},
        }},
    },
    {
        "name": "host_provision_probe",
        "title": "Probe host system facts for provisioning",
        "description": (
            "When: host capabilities must be discovered read-only. "
            "In: no inputs beyond the request. "
            "Out: hardware, operating system, interpreter, GPU, and package- "
            "manager facts."),
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
    {
        "name": "host_provision_plan",
        "title": "Plan host provisioning for a goal",
        "description": (
            "When: a goal needs an ordered, typed host-provisioning plan. "
            "In: the goal and optional constraints. "
            "Out: a dry-run, advisory Plan; execution still requires consent."),
        "inputSchema": {"type": "object", "properties": {
            "goal": {"type": "string", "description": "Provisioning goal text (e.g. 'install ruff')"},
            "root": {"type": "string", "description": "Workspace inside configured MCP allowed roots; defaults only when exactly one root is configured"},
        }, "required": ["goal"]},
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
    {
        "name": "repo_cards",
        "title": "Repo philosophy cards generator/checker",
        "description": (
            "When: deterministic repo philosophy cards must be generated or "
            "checked. "
            "In: the committed repository config. "
            "Out: the generated or checked cards."),
        "inputSchema": {"type": "object", "properties": {
            "root": {"type": "string", "description": "Selected repo or parent workspace inside configured MCP allowed roots"},
            "check": {"type": "boolean", "default": True, "description": "True to verify cards match without writing (default)"},
            "repo": {"type": "string", "description": "Optional specific repository name"},
            "allow_write": {"type": "boolean", "description": "Explicit permission to write generated cards when check=false"},
        }},
        "annotations": {"openWorldHint": True},
    },
    {
        "name": "jev_calibration",
        "title": "Jev judgment calibration analysis",
        "description": (
            "When: a caller needs Jev confidence calibration from ledger "
            "history. "
            "In: the ledger window fields declared by the schema. "
            "Out: the confidence distribution and recommended threshold "
            "bands; read-only."),
        "inputSchema": {"type": "object", "properties": {
            "window": {"type": "integer", "default": 1000, "description": "Number of recent ledger entries to inspect"},
        }},
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
    {
        "name": "economics_report",
        "title": "Economics & value routing report",
        "description": (
            "When: a caller needs economics and pool-rotation standing. "
            "In: no inputs beyond the request. "
            "Out: tier snapshot freshness, seat-share diversity, per-lane and "
            "per-tier cost metering, and bounded pool-rotation proposals; "
            "read-only."),
        "inputSchema": {"type": "object", "properties": {
            "allow_cost_increase": {"type": "boolean", "default": False, "description": "Allow proposing a candidate with higher expected cost"},
        }},
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
    {
        "name": "flush_run",
        "title": "Run a saturation (flush) campaign or canary gate",
        "description": (
            "When: a saturation campaign or planted-defect canary suite must "
            "be replayed. "
            "In: exactly one of campaign or canary_suite, plus optional "
            "ledger, extended_instruments, cold_run, root, and allow_write. "
            "Out: the same campaign state the CLI reports, or the fail-closed "
            "canary gate verdict; no code is written."),
        "inputSchema": {"type": "object", "properties": {
            "campaign": {"type": "string", "description": "Campaign JSON path inside an allowed root"},
            "canary_suite": {"type": "string", "description": "Canary suite JSON path inside an allowed root"},
            "ledger": {"type": "string", "description": "Optional event ledger path inside an allowed root"},
            "extended_instruments": {"type": "boolean", "default": False, "description": "Allow the FLUSH-2/3 panel, deterministic, and cold instruments"},
            "cold_run": {"type": "boolean", "default": False, "description": "Require a clean ledger and a cold run in the final pass"},
            "root": {"type": "string", "description": "Selected workspace inside configured MCP allowed roots"},
            "allow_write": {"type": "boolean", "description": "Explicit permission to append the event ledger"},
        }},
        "annotations": {"openWorldHint": True},
    },
]

