"""Hand-rolled stdio MCP server (newline-delimited JSON-RPC 2.0).

Spec: https://modelcontextprotocol.io/specification/2025-06-18
Dispatch is native: any MCP host (Claude Code, Cursor, your other agents) can
call the harness tools directly. Zero dependencies. The server writes ONLY
valid MCP messages to stdout; all human logging goes to stderr via harness.output.
"""
import json
import os
import sys
import uuid

from .errors import HarnessError, ToolCancelled
from .panel import panel_judge
from .continuation import validate_continuation
from .consent import probe_consent

from . import __version__

PROTOCOL_VERSION = "2025-06-18"
SERVER_VERSION = __version__


class McpServer:
    def __init__(self, *, transport, api_key, governor, ledger, router, engine,
                 max_panelists=3, use_free=True, stdin=None, stdout=None,
                 allow_verify=False, allow_write=False, allowed_roots=None):
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
        self._cancelled = set()  # request ids aborted via notifications/cancelled
        # Remote-safety gates: verify gates run real commands, and apply_edit
        # writes real files. Over MCP (a remote-dispatch surface) both require
        # explicit opt-in per request (allow_verify) or server configuration
        # (allowed_roots) rather than trusting the caller blindly (#5/#6).
        self.allow_verify = bool(allow_verify)
        self.allow_write = bool(allow_write)
        self.allowed_roots = [os.path.realpath(os.path.abspath(r))
                              for r in (allowed_roots or [])]

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
                    "specialist_pool": {"type": "string", "description": "Comma-separated specialist fallback ladder, strongest first (default: configured pool)"},
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
                    "file": {"type": "array", "items": {"type": "string"},
                             "description": "Path(s) to the file(s) to edit; repeat for a "
                                            "multi-file batch (one governed session per file, "
                                            "shared task budget, fail-fast)"},
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

    def _negotiate_version(self, requested):
        supported = {PROTOCOL_VERSION}
        if requested is None:
            return PROTOCOL_VERSION
        if requested not in supported:
            raise HarnessError(
                f"unsupported MCP protocol version {requested!r}; "
                f"supported: {PROTOCOL_VERSION}")
        return requested

    def _handle(self, msg):
        method = msg.get("method")
        if method == "initialize":
            try:
                version = self._negotiate_version(
                    msg.get("params", {}).get("protocolVersion"))
            except HarnessError as exc:
                return {"jsonrpc": "2.0", "id": msg.get("id"),
                        "error": {"code": -32602, "message": str(exc)}}
            return {
                "jsonrpc": "2.0", "id": msg.get("id"),
                "result": {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}, "logging": {}},
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
            result = self._invoke(name, args,
                                   cancel_check=lambda: rid in self._cancelled)
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

    def _invoke(self, name, args, cancel_check=None):
        if name == "panel_verify":
            # Panel ordering (catalog seed, capability sort, degrade to the
            # given order) is the panel lane's own job -- same recipe as the
            # CLI's verify lane, one owner in panel.py.
            panel = (args.get("panel") or ",".join(self.router.panel_pool)).split(",")
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
                free_tier=self.use_free, cancel_check=cancel_check)
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
            # Remote write containment (#6): writes are opt-in and every
            # target file (including every member of a batch) must resolve
            # inside a configured allowed root.
            if not (self.allow_write or args.get("allow_write")):
                raise HarnessError(
                    "MCP file writes are disabled for this session; re-send with "
                    "allow_write=true or configure allow_write=True explicitly")
            raw_target = args.get("file")
            target_files = ([raw_target] if isinstance(raw_target, str)
                            else list(raw_target) if isinstance(raw_target, list) else [])
            if continuation.get("file_path"):
                target_files.append(continuation["file_path"])
            if not self.allowed_roots:
                raise HarnessError(
                    "MCP apply requires at least one configured allowed root")
            for target_file in target_files:
                t = os.path.realpath(os.path.abspath(target_file))
                if not any(t == root or t.startswith(root + os.sep)
                           for root in self.allowed_roots):
                    raise HarnessError(
                        "file is outside every allowed root for this MCP session")
            # Batch parity with the CLI (#12): 'file' may be a string or a
            # list; the engine owns the batch loop and routing. NOTE: the
            # router is NEVER mutated here -- ordering happens per request
            # inside the engine, so one session's routing cannot leak into
            # the next.
            raw_files = args.get("file")
            files = ([raw_files] if isinstance(raw_files, str)
                     else list(raw_files) if isinstance(raw_files, list) else [])
            if not files and not continuation:
                raise HarnessError("apply_edit requires 'file' (or a continuation)")
            return self.engine.apply_batch(
                files or [None],
                task_id=args.get("task_id"),
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
                backend=backend,
                verify_only=bool(args.get("verify_only", False)),
                max_lines=args.get("max_lines", 500),
                continuation=continuation,
                cancel_check=cancel_check)
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
            ok, bad_seq = self.ledger.verify()
            return {"entries": self.ledger.tail(args.get("limit", 20)),
                    "verified": {"ok": ok, "first_bad_seq": bad_seq}}
        if name == "participation_report":
            return self.ledger.participation_report()
        if name == "spend_status":
            return self.governor.key_status()
        raise ValueError(f"unknown tool: {name}")

    def _write(self, obj):
        self.stdout.write(json.dumps(obj) + "\n")
        self.stdout.flush()


def main(argv=None):  # pragma: no cover - thin wiring
    from .config import load_settings
    from . import session as composition

    settings = load_settings()
    transport = composition.HttpTransport()
    # Interface parity by construction: every dependency is built by the ONE
    # composition owner (harness/session.py), so an engine-kwarg or tier-policy
    # change can no longer land in the CLI and miss MCP.
    api_key, governor = composition.governor_for(settings)
    ledger = composition.ledger_for(settings)
    # Pre-spend look-ahead; stderr advice only, never the protocol channel.
    composition.pre_run_warning(governor=governor, ledger=ledger,
                                use_free=settings.use_free)
    router = composition.router_for(settings)
    engine = composition.engine_for(settings, api_key, governor, ledger, router)
    server = McpServer(transport=transport, api_key=api_key, governor=governor,
                       ledger=ledger, router=router, engine=engine,
                       max_panelists=settings.max_panelists, use_free=settings.use_free,
                       allow_write=settings.mcp_allow_write,
                       allow_verify=settings.mcp_allow_verify,
                       allowed_roots=settings.mcp_allowed_roots)
    server.serve_forever()


if __name__ == "__main__":
    main()
