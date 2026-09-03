"""Hand-rolled stdio MCP server (newline-delimited JSON-RPC 2.0).

Spec: https://modelcontextprotocol.io/specification/2025-06-18
Dispatch is native: any MCP host (Claude Code, Cursor, your other agents) can
call the harness tools directly. Zero dependencies. The server writes ONLY
valid MCP messages to stdout; all human logging goes to stderr via core.eprint.
"""
import json
import sys
import uuid

from .core import HarnessError, panel_judge
from .consent import probe_consent

PROTOCOL_VERSION = "2025-06-18"
SERVER_VERSION = "0.1.0"


class McpServer:
    def __init__(self, *, transport, api_key, governor, ledger, router, engine,
                 stdin=None, stdout=None):
        self.transport = transport
        self.api_key = api_key
        self.governor = governor
        self.ledger = ledger
        self.router = router
        self.engine = engine
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout

    # ---------------- tool definitions ----------------
    def _tools(self):
        return [
            {
                "name": "panel_verify",
                "title": "Multi-model verification",
                "description": "Panel of cheap models answers a self-contained question, then a judge "
                               "synthesizes agreement/disagreement into a verdict. Cost-bounded. No "
                               "web/file tools by design.",
                "inputSchema": {"type": "object", "properties": {
                    "prompt": {"type": "string", "description": "Self-contained question + context"},
                    "panel": {"type": "string", "description": "Comma-separated model ids (2-4). Defaults to configured panel."},
                    "judge": {"type": "string", "description": "Judge model id. Defaults to configured judge."},
                    "max_tokens": {"type": "integer", "default": 300},
                    "max_cost": {"type": "number", "description": "Per-run cost ceiling (USD)."},
                    "reasoning_effort": {"type": "string", "enum": ["none", "low", "medium", "high"]},
                    "task_id": {"type": "string"},
                }, "required": ["prompt"]},
            },
            {
                "name": "apply_edit",
                "title": "Scoped code edit with verification",
                "description": "Make a single, scoped (<500-line file) code change, run a verification "
                               "gate, and retry up to max_rounds. Honors consent if required.",
                "inputSchema": {"type": "object", "properties": {
                    "file": {"type": "string", "description": "Path to the file to edit"},
                    "instruction": {"type": "string", "description": "What to change (<=1000 chars)"},
                    "edit_snippet": {"type": "string", "description": "Intent anchor snippet (<=2000 chars)"},
                    "verify_cmd": {"type": "string", "description": "Shell command gate, e.g. 'cargo check'"},
                    "max_rounds": {"type": "integer", "default": 3},
                    "require_consent": {"type": "boolean", "description": "Ask the model if it accepts the work first"},
                    "task_id": {"type": "string"},
                }, "required": ["file", "instruction"]},
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
        try:
            result = self._invoke(name, args)
            return {
                "jsonrpc": "2.0", "id": rid, "result": {
                    "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
                    "structuredContent": result,
                    "isError": False,
                },
            }
        except (HarnessError, ValueError) as e:
            return {
                "jsonrpc": "2.0", "id": rid, "result": {
                    "content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}],
                    "isError": True,
                },
            }

    def _invoke(self, name, args):
        if name == "panel_verify":
            return panel_judge(
                transport=self.transport, api_key=self.api_key, governor=self.governor,
                prompt=args["prompt"],
                panel=(args.get("panel") or ",".join(self.router.panel)).split(","),
                judge=args.get("judge") or self.router.judge,
                max_tokens=args.get("max_tokens"),
                reasoning_effort=args.get("reasoning_effort", "low"),
                task_id=args.get("task_id"), ledger=self.ledger)
        if name == "apply_edit":
            return self.engine.apply_edit(
                task_id=args.get("task_id") or uuid.uuid4().hex[:8],
                file_path=args["file"], instruction=args["instruction"],
                edit_snippet=args.get("edit_snippet"), verify_cmd=args.get("verify_cmd"),
                max_rounds=args.get("max_rounds", 3),
                require_consent=args.get("require_consent"),
                max_tokens=args.get("max_tokens") or 4096,
                allow_escalation=args.get("allow_escalation"))
        if name == "offer_work":
            return probe_consent(
                transport=self.transport, api_key=self.api_key, governor=self.governor,
                task_id=args.get("task_id") or uuid.uuid4().hex[:8],
                task=args["task"], model=args.get("model") or self.router.judge,
                context=args.get("context"), ledger=self.ledger, required=True)
        if name == "defer_work":
            self.ledger.append("defer_midtask", task_id=args.get("task_id"),
                               reason=args.get("reason"), model="(deferral)")
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
                    settings.escalation_model, settings.allow_escalation)
    engine = ApplyEngine(transport, api_key, governor, ledger, router,
                         settings.default_require_consent)
    server = McpServer(transport=transport, api_key=api_key, governor=governor,
                       ledger=ledger, router=router, engine=engine)
    server.serve_forever()