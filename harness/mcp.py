"""Hand-rolled stdio MCP server (newline-delimited JSON-RPC 2.0).

Spec: https://modelcontextprotocol.io/specification/2025-06-18
Dispatch is native: any MCP host (Claude Code, Cursor, your other agents) can
call the harness tools directly. Zero dependencies. The server writes ONLY
valid MCP messages to stdout; all human logging goes to stderr via core.eprint.
"""
import json
import os
import sys
import uuid

from .core import HarnessError, ToolCancelled, panel_judge
from .apply import validate_continuation
from .consent import probe_consent

from . import __version__

PROTOCOL_VERSION = "2025-06-18"
SERVER_VERSION = __version__


class McpServer:
    def __init__(self, *, transport, api_key, governor, ledger, router, engine,
                 max_panelists=3, use_free=True, stdin=None, stdout=None,
                 allow_verify=False, allowed_roots=None):
        self.transport = transport
        self.api_key = api_key
        self.governor = governor
        self.ledger = ledger
        self.router = router
        self.engine = engine
        self.max_panelists = max_panelists
        self.use_free = use_free
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout
        self._capability = None  # lazily-built profiles; report is always fresh
        self._cancelled = set()  # request ids aborted via notifications/cancelled
        # Remote-safety gates: verify gates run real commands, and apply_edit
        # writes real files. Over MCP (a remote-dispatch surface) both require
        # explicit opt-in per request (allow_verify) or server configuration
        # (allowed_roots) rather than trusting the caller blindly (#5/#6).
        self.allow_verify = bool(allow_verify)
        self.allowed_roots = [os.path.abspath(r) for r in (allowed_roots or [])]

    # ---------------- tool definitions ----------------
    def _tools(self):
        return [
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
                    "max_tokens": {"type": "integer", "default": 300},
                    "reasoning_effort": {"type": "string", "enum": ["auto", "off", "none", "low", "medium", "high", "on"]},
                    "converge": {"type": "boolean", "description": "Run the convergence specialist on per-claim votes (requires per-claim JSON panel output)"},
                    "convergence_model": {"type": "string", "description": "Primary specialist model (default: judge)"},
                    "specialist_pool": {"type": "string", "description": "Comma-separated specialist fallback ladder, strongest first (default: configured pool; free lane leads with GLM-5.2)"},
                    "task_id": {"type": "string"},
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
                    "file": {"type": "string", "description": "Path to the file to edit"},
                    "instruction": {"type": "string", "description": "What to change (<=1000 chars)"},
                    "edit_snippet": {"type": "string", "description": "Intent anchor snippet (<=2000 chars)"},
                    "verify_cmd": {"type": "string", "description": "Shell command gate, e.g. 'cargo check' (requires server allow_verify)"},
                    "allow_verify": {"type": "boolean", "description": "Explicit confirmation to run a verify gate in this request (required when allow_verify is not enabled server-side)"},
                    "max_rounds": {"type": "integer", "default": 3},
                    "require_consent": {"type": "boolean", "description": "Ask the model if it accepts the work first"},
                    "renew_consent": {"type": "boolean", "description": "Re-check consent before each round (continued consensus)"},
                    "max_rotations": {"type": "integer", "description": "How many model rotations to allow on error"},
                    "reasoning_effort": {"type": "string", "enum": ["auto", "off", "none", "low", "medium", "high", "on"]},
                    "model": {"type": "string", "description": "Explicit model override; otherwise the corrected apply route is used"},
                    "backend": {"type": "string", "enum": ["harness", "morph", "diff"], "default": "harness", "description": "morph: MorphLite-compatible structured editing; diff: strict unified-diff editing (no file-size ceiling)"},
                    "verify_only": {"type": "boolean", "description": "Return the proposal without writing or running the verification gate"},
                    "max_lines": {"type": "integer", "default": 500, "description": "Per-file line ceiling (1-500)"},
                    "task_max_cost": {"type": "number", "description": "Per-task cost ceiling"},
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
                    "limit": {"type": "integer", "default": 20},
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
        ]

    # ---------------- JSON-RPC dispatch ----------------
    def serve_forever(self):
        while True:
            line = self.stdin.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                self._write({"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32700, "message": "Parse error"}})
                continue
            if not isinstance(msg, dict) or "method" not in msg:
                rid = msg.get("id") if isinstance(msg, dict) else None
                self._write({"jsonrpc": "2.0", "id": rid,
                             "error": {"code": -32600, "message": "Invalid Request"}})
                continue
            resp = self._handle(msg)
            if resp is not None:
                self._write(resp)

    def _handle(self, msg):
        method = msg.get("method")
        if method == "initialize":
            return {
                "jsonrpc": "2.0", "id": msg.get("id"),
                "result": {
                    "protocolVersion": (msg.get("params", {}).get("protocolVersion")
                                        or PROTOCOL_VERSION),
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "harness", "version": SERVER_VERSION},
                },
            }
        if method in ("notifications/initialized", "notifications/cancelled"):
            if method == "notifications/cancelled":
                # Audit #13: cancel must actually abort. Mark the in-flight
                # request id cancelled; the tool loop checks this between
                # rounds and raises ToolCancelled so the engine unwinds.
                cancelled_id = (msg.get("params") or {}).get("requestId")
                if cancelled_id is not None:
                    self._cancelled.add(cancelled_id)
            return None
        if method == "ping":
            return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": msg.get("id"),
                    "result": {"tools": self._tools()}}
        if method == "tools/call":
            return self._call_tool(msg)
        return {"jsonrpc": "2.0", "id": msg.get("id"),
                "error": {"code": -32601, "message": f"Method not found: {method}"}}

    def _call_tool(self, msg):
        rid = msg.get("id")
        params = msg.get("params", {})
        name = params.get("name")
        args = params.get("arguments") or {}
        tool = {t["name"]: t for t in self._tools()}.get(name)
        if not tool:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32602, "message": f"Unknown tool: {name}"}}
        if rid in self._cancelled:
            self._cancelled.discard(rid)
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32800, "message": "Request cancelled before execution"}}
        try:
            result = self._invoke(name, args)
            return {
                "jsonrpc": "2.0", "id": rid, "result": {
                    "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
                    "structuredContent": result,
                    "isError": False,
                },
            }
        except ToolCancelled:
            self._cancelled.discard(rid)
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32800, "message": "Request cancelled"}}
        except HarnessError as e:
            # Structured error codes (#13): stable machine-readable kinds
            # instead of prose-only failures.
            return {
                "jsonrpc": "2.0", "id": rid, "result": {
                    "content": [{"type": "text", "text": f"HarnessError: {e}"}],
                    "isError": True,
                    "errorKind": getattr(e, "kind", "harness_error"),
                },
            }
        except ValueError as e:
            return {
                "jsonrpc": "2.0", "id": rid, "result": {
                    "content": [{"type": "text", "text": f"ValueError: {e}"}],
                    "isError": True,
                    "errorKind": "invalid_input",
                },
            }

    def _capability_context(self):
        """Lazily build profiles for capability-aware routing and refresh the
        ledger report on every invocation so new probe evidence is immediately
        visible to MCP routing."""
        if self._capability is None:
            try:
                from .capability import build_profiles_from_models
                self._capability = build_profiles_from_models(self.governor.fetch_models())
            except Exception:
                self._capability = {}
        return (self._capability or None, self.ledger.participation_report())

    def _invoke(self, name, args):
        if name == "panel_verify":
            profiles, report = self._capability_context()
            panel = (args.get("panel") or ",".join(self.router.panel_pool)).split(",")
            if profiles is not None:
                from .capability import order_pool as _op
                ordered = _op(panel, profiles, report, ledger=self.ledger,
                              task="default", free_tier=self.use_free)
                if ordered:
                    panel = ordered
            return panel_judge(
                transport=self.transport, api_key=self.api_key, governor=self.governor,
                prompt=args["prompt"],
                panel=panel,
                judge=args.get("judge") or self.router.judge,
                max_tokens=args.get("max_tokens"),
                reasoning_effort=args.get("reasoning_effort", self.engine.reasoning_effort),
                reasoning_token_budget=self.engine.reasoning_token_budget,
                task_id=args.get("task_id"), ledger=self.ledger,
                max_panelists=self.max_panelists,
                run_convergence=bool(args.get("converge", False)),
                convergence_model=(args.get("convergence_model")
                                   or self.router.convergence_model),
                specialist_pool=(args.get("specialist_pool").split(",")
                                 if args.get("specialist_pool")
                                 else self.router.specialist_pool),
                capability_profiles=profiles, report=report, free_tier=self.use_free)
        if name == "apply_edit":
            # Reject ungated persisted state before capability/model setup, just
            # like the CLI and direct library paths. Saved metadata also owns
            # backend and preview mode on resume.
            continuation = validate_continuation(args.get("continuation"))
            backend = continuation.get("backend", args.get("backend", "harness"))
            # Remote verify gates (#5): running real commands over MCP requires
            # either the explicit per-request confirmation flag or a server
            # that was configured with allow_verify=True.
            effective_verify_cmd = args.get("verify_cmd")
            if not effective_verify_cmd and continuation.get("verify_cmd"):
                effective_verify_cmd = continuation["verify_cmd"]
            if effective_verify_cmd and not (self.allow_verify or args.get("allow_verify")):
                raise HarnessError(
                    "verify_cmd was supplied but verify gates are not enabled for this "
                    "MCP session; re-send with allow_verify=true to confirm, or configure "
                    "the server with allow_verify=True")
            # Remote write containment (#6): when the server declares allowed
            # roots, every target file (and continuation target) must live in
            # one of them.
            target_file = args.get("file") or continuation.get("file_path")
            if self.allowed_roots and target_file:
                t = os.path.abspath(target_file)
                if not any(t == r or t.startswith(r + os.sep) for r in self.allowed_roots):
                    raise HarnessError(
                        f"file {target_file!r} is outside every allowed root for this "
                        "MCP session")
            profiles, report = self._capability_context()
            if profiles is not None and backend == "harness":
                from .capability import order_pool as _op
                ordered = _op(self.router.apply_pool, profiles, report,
                              ledger=self.ledger, task="code", free_tier=self.use_free)
                if ordered:
                    self.router.apply_pool = ordered
                    if not args.get("model"):
                        self.router.apply_model = ordered[0]
            return self.engine.apply_edit(
                task_id=args.get("task_id"),
                file_path=args.get("file"),
                instruction=args.get("instruction") or "",
                edit_snippet=args.get("edit_snippet"), verify_cmd=args.get("verify_cmd"),
                max_rounds=args.get("max_rounds", 3),
                require_consent=args.get("require_consent"),
                max_tokens=args.get("max_tokens") or 4096,
                model=args.get("model"),
                task_max_cost=args.get("task_max_cost"),
                allow_escalation=args.get("allow_escalation"),
                reasoning_effort=args.get("reasoning_effort"),
                renew_consent=args.get("renew_consent"),
                max_rotations=args.get("max_rotations"),
                continuation=continuation,
                backend=backend,
                verify_only=bool(args.get("verify_only", False)),
                max_lines=args.get("max_lines", 500))
        if name == "offer_work":
            return probe_consent(
                transport=self.transport, api_key=self.api_key, governor=self.governor,
                task_id=args.get("task_id") or uuid.uuid4().hex[:8],
                task=args["task"], model=args.get("model") or self.router.judge,
                context=args.get("context"), ledger=self.ledger, required=True,
                fallback_pool=self.router.panel_pool)
        if name == "defer_work":
            self.ledger.append("defer_midtask", task_id=args.get("task_id"),
                               reason=args.get("reason"), category=args.get("category"),
                               model="(deferral)")
            return {"status": "deferred", "task_id": args.get("task_id"),
                    "reason": args.get("reason"), "note": "partial work preserved",
                    "participation": self.ledger.participation_report()}
        if name == "ledger_status":
            return {"entries": self.ledger.tail(args.get("limit", 20)),
                    "verified": self.ledger.verify()}
        if name == "participation_report":
            return self.ledger.participation_report()
        if name == "spend_status":
            return self.governor.key_status()
        raise ValueError(f"unknown tool: {name}")

    def _write(self, obj):
        self.stdout.write(json.dumps(obj) + "\n")
        self.stdout.flush()


def main(argv=None):  # pragma: no cover - thin wiring
    from .config import load_settings, resolve_api_key
    from .core import SpendGovernor
    from .ledger import AutonomyLedger
    from .router import Router
    from .apply import ApplyEngine
    from ._http import HttpTransport

    settings = load_settings()
    api_key = resolve_api_key()
    transport = HttpTransport()
    governor = SpendGovernor(transport, api_key, settings.expect_key_label,
                             settings.max_cost)
    governor.verify_key()
    ledger = AutonomyLedger(settings.ledger_path)
    router = Router(settings.panel, settings.judge, settings.apply_model,
                    settings.escalation_model, settings.allow_escalation,
                    panel_pool=settings.panel_pool, apply_pool=settings.apply_pool,
                    specialist_pool=settings.specialist_pool,
                    convergence_model=settings.convergence_model)
    engine = ApplyEngine(transport, api_key, governor, ledger, router,
                         default_require_consent=settings.default_require_consent,
                         default_renew_consent=settings.renew_consent,
                         reasoning_effort=settings.reasoning_effort,
                         reasoning_token_budget=settings.reasoning_token_budget,
                         default_max_rotations=settings.max_rotations,
                         default_task_max_cost=settings.task_max_cost)
    server = McpServer(transport=transport, api_key=api_key, governor=governor,
                       ledger=ledger, router=router, engine=engine,
                       max_panelists=settings.max_panelists, use_free=settings.use_free)
    server.serve_forever()
