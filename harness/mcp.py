"""Native MCP stdio server.

This module owns JSON-RPC framing, scheduling, cancellation, response
serialization, and engine dispatch; the tool contracts (schemas) live in
harness/mcp_schemas.py as pure data.
"""
import json
import math
import os
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from . import __version__
from . import trust as trust_policy
from .consent import probe_consent
from .continuation import validate_continuation
from .errors import HarnessError, ToolCancelled
from .mcp_schemas import TOOL_SCHEMAS
from .service import run_verify as _service_run_verify
from .validation import (
    MAX_LINES,
    MAX_ROUNDS,
    MAX_ROTATIONS,
    MAX_SNIPPET_CHARS,
    MAX_TOKENS,
    bounded_int,
    finite_number,
    validate_backend,
    validate_mcp_bool,
    validate_mcp_csv,
    validate_mcp_files,
    validate_mcp_limit,
    validate_mcp_max_tokens,
    validate_mcp_model,
    validate_mcp_prompt,
    validate_mcp_reasoning,
    validate_mcp_task,
    validate_mcp_task_id,
    validate_text,
)

PROTOCOL_VERSION = "2025-06-18"
SERVER_VERSION = __version__

# Tool lanes: one serial worker each. Mutation (file writes) stays strictly
# serial for single-session engine semantics; spendy lanes (network calls
# that can run minutes on a saturated tier) no longer head-of-line-block
# the observation lane, so status/report queries always answer promptly.
# Governor spend accounting and ledger appends are lock-guarded, and the
# engine is only ever driven from the mutation lane, so lanes are safe to
# run concurrently with each other.
MUTATION_LANE = {"apply_edit"}
SPENDY_LANE = {"panel_verify", "offer_work"}
# Default per-tool deadline (seconds): cooperative, tripped through the
# same cancel_check as notifications/cancelled. Configurable via
# HARNESS_MCP_TOOL_TIMEOUT (60..7200).
MCP_TOOL_TIMEOUT_DEFAULT = 1800


def _valid_rpc_id(value):
    """JSON-RPC request ids are finite strings, numbers, or null."""
    return (value is None or isinstance(value, str)
            or (isinstance(value, int) and not isinstance(value, bool))
            or (isinstance(value, float) and math.isfinite(value)))


def _reject_nonstandard_json(value):
    raise ValueError(f"non-standard JSON constant: {value}")


class McpServer:
    """Lane workers (one serial worker per lane) with an independent stdio reader."""

    def __init__(self, *, transport, api_key, governor, ledger, router, engine,
                 max_panelists=3, use_free=True, stdin=None, stdout=None,
                 allow_verify=False, allow_write=False, allowed_roots=None,
                 tool_timeout=None, caller=None, auth_token=None):
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
        self._write_lock = threading.Lock()
        self._cancel_lock = threading.Lock()
        self._cancelled = set()
        self._inflight = set()
        self._starts = {}
        self.tool_timeout = (MCP_TOOL_TIMEOUT_DEFAULT if tool_timeout is None
                             else tool_timeout)
        # Optional shared secret. When set, tools/call must present
        # params._meta.harness_token (or params.harness_token) matching.
        # Empty = inherited stdio authority (documented trust model).
        self.auth_token = auth_token or None
        # Session authorship for the evidence loop: the stdio peer (captured
        # from initialize clientInfo) or the embedding host. Ledger events
        # created on this connection carry it; trust scores break out
        # per-caller history from it.
        self.caller = caller
        # Remote safety is explicit: verify commands and file writes are not
        # enabled merely because a protocol client can reach this process.
        self.allow_verify = bool(allow_verify)
        self.allow_write = bool(allow_write)
        self.allowed_roots = [os.path.realpath(os.path.abspath(root))
                              for root in (allowed_roots or [])]

    # ---------------- lanes + deadlines ----------------
    @staticmethod
    def _lane_for(tool_name):
        """Which serial lane runs a tool. Unknown/missing names ride the
        observe lane and fail validation in the worker, as before."""
        if tool_name in MUTATION_LANE:
            return "mutation"
        if tool_name in SPENDY_LANE:
            return "spendy"
        return "observe"

    def _note_start(self, request_id):
        """Stamp a request's deadline clock (first stamp wins: submit time,
        so queueing behind a busy lane counts against the deadline)."""
        if request_id is None:
            return
        with self._cancel_lock:
            self._starts.setdefault(request_id, time.monotonic())

    def _forget_start(self, request_id):
        with self._cancel_lock:
            self._starts.pop(request_id, None)

    def _is_expired(self, request_id):
        try:
            with self._cancel_lock:
                start = self._starts.get(request_id)
            if start is None:
                return False
            return (time.monotonic() - start) > float(self.tool_timeout)
        except (TypeError, ValueError):
            return False

    def _cancel_check(self, request_id, with_deadline=False):
        """One cooperative predicate for cancellation AND deadlines, so an
        uncancelled-but-overdue run stops at the same poll points a
        cancelled one does (in-flight POST/subprocess still run to their
        own timeouts -- documented residual, same as cancel)."""
        if self._is_cancelled(request_id):
            return True
        return bool(with_deadline) and self._is_expired(request_id)

    # ---------------- JSON-RPC dispatch ----------------
    def serve_forever(self):
        """Read frames until EOF while lane workers run tools.

        A reader/worker split is required so cancellation notifications can
        arrive during provider or gate I/O. Each lane stays serial -- the
        mutation lane keeps single-session engine semantics -- but a long
        apply/panel no longer head-of-line-blocks status queries.
        """
        pools = {lane: ThreadPoolExecutor(max_workers=1)
                 for lane in ("mutation", "spendy", "observe")}
        try:
            while True:
                line = self.stdin.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line, parse_constant=_reject_nonstandard_json)
                except (json.JSONDecodeError, ValueError):
                    self._write({"jsonrpc": "2.0", "id": None,
                                 "error": {"code": -32700, "message": "Parse error"}})
                    continue
                if not isinstance(msg, dict) or "method" not in msg:
                    rid = msg.get("id") if isinstance(msg, dict) else None
                    self._write({"jsonrpc": "2.0", "id": rid,
                                 "error": {"code": -32600, "message": "Invalid Request"}})
                    continue
                if "id" in msg and not _valid_rpc_id(msg.get("id")):
                    self._write({"jsonrpc": "2.0", "id": None,
                                 "error": {"code": -32600,
                                            "message": "request id must be a JSON scalar"}})
                    continue
                if msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
                    self._write({"jsonrpc": "2.0", "id": msg.get("id"),
                                 "error": {"code": -32600, "message": "Invalid Request"}})
                    continue

                if msg.get("method") == "tools/call":
                    identified = "id" in msg
                    request_id = msg.get("id")
                    if identified:
                        with self._cancel_lock:
                            if request_id in self._inflight:
                                self._write({
                                    "jsonrpc": "2.0", "id": request_id,
                                    "error": {"code": -32600,
                                               "message": "duplicate request id is already in flight"},
                                })
                                continue
                            self._inflight.add(request_id)
                    params = msg.get("params", {})
                    tool_name = params.get("name") if isinstance(params, dict) else None
                    self._note_start(request_id)
                    pools[self._lane_for(tool_name)].submit(
                        self._write_tool_response, msg)
                    continue

                response = self._handle(msg)
                if response is not None:
                    self._write(response)
        finally:
            # Requests already accepted from the input stream still own a
            # response. Let every lane drain after EOF; only an explicit
            # notifications/cancelled message cancels work.
            for pool in pools.values():
                pool.shutdown(wait=True)

    def _write_tool_response(self, msg):
        request_id = msg.get("id")
        try:
            try:
                response = self._call_tool(msg)
            except Exception:
                # The stdio boundary must survive one unexpected tool failure.
                response = {"jsonrpc": "2.0", "id": request_id,
                            "error": {"code": -32603, "message": "Internal error"}}
            if response is not None and "id" in msg:
                self._write(response)
        finally:
            # Keep an ID reserved until its response frame is written. This
            # closes the reuse gap between tool completion and serialization.
            if "id" in msg:
                with self._cancel_lock:
                    self._inflight.discard(request_id)
                    self._cancelled.discard(request_id)
            self._forget_start(request_id)

    @staticmethod
    def _sanitize_caller_part(value):
        text = str(value or "").strip()
        kept = "".join(ch for ch in text
                       if ch.isalnum() or ch in "._-+/ ")
        return " ".join(kept.split())[:64]

    def _capture_caller(self, params):
        """Name the stdio peer from initialize clientInfo for the evidence
        loop: subsequent ledger appends on this connection carry it, so
        per-caller trust can tell peers apart. Unknown peers stay "mcp"."""
        try:
            info = params.get("clientInfo") or {}
            name = self._sanitize_caller_part(info.get("name"))
            version = self._sanitize_caller_part(info.get("version"))
            if name and version:
                self.caller = f"mcp:{name}/{version}"
            elif name:
                self.caller = f"mcp:{name}"
            else:
                self.caller = "mcp"
            self.ledger.caller = self.caller
        except Exception:
            pass

    def _negotiate_version(self, requested):
        if requested is None:
            return PROTOCOL_VERSION
        if requested != PROTOCOL_VERSION:
            raise HarnessError(
                f"unsupported MCP protocol version {requested!r}; "
                f"supported: {PROTOCOL_VERSION}")
        return requested

    def _handle(self, msg):
        method = msg.get("method")
        is_notification = "id" not in msg
        params = msg.get("params", {})
        if not isinstance(params, dict):
            if is_notification:
                return None
            return {"jsonrpc": "2.0", "id": msg.get("id"),
                    "error": {"code": -32602, "message": "params must be an object"}}

        if method == "initialize":
            if is_notification:
                return None
            try:
                version = self._negotiate_version(params.get("protocolVersion"))
            except HarnessError as exc:
                return {"jsonrpc": "2.0", "id": msg.get("id"),
                        "error": {"code": -32602, "message": str(exc)}}
            self._capture_caller(params)
            return {
                "jsonrpc": "2.0", "id": msg.get("id"),
                "result": {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}, "logging": {}},
                    "serverInfo": {"name": "harness", "version": SERVER_VERSION},
                },
            }

        if method == "notifications/initialized":
            return None
        if method == "notifications/cancelled":
            return self._handle_cancellation(msg, is_notification)
        if is_notification:
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

    def _handle_cancellation(self, msg, is_notification):
        params = msg.get("params", {})
        request_id = msg.get("id")
        if not isinstance(params, dict):
            if is_notification:
                return None
            return {"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32602, "message": "params must be an object"}}
        if "requestId" not in params:
            if is_notification:
                return None
            return {"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32602, "message": "requestId is required"}}
        cancelled_id = params["requestId"]
        if not _valid_rpc_id(cancelled_id):
            if is_notification:
                return None
            return {"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32602,
                               "message": "requestId must be a JSON scalar"}}
        with self._cancel_lock:
            if cancelled_id in self._inflight:
                self._cancelled.add(cancelled_id)
        return None

    def _is_cancelled(self, request_id):
        with self._cancel_lock:
            return request_id in self._cancelled

    def _call_tool(self, msg):
        request_id = msg.get("id")
        if "id" in msg:
            with self._cancel_lock:
                if request_id in self._cancelled:
                    self._cancelled.discard(request_id)
                    return {"jsonrpc": "2.0", "id": request_id,
                            "error": {"code": -32800,
                                       "message": "Request cancelled before execution"}}
        return self._call_tool_impl(msg)

    def _check_auth(self, msg):
        """Shared-secret gate for tools/call when auth_token is configured."""
        if not self.auth_token:
            return None
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        token = meta.get("harness_token") or params.get("harness_token")
        if token is None or not isinstance(token, str) or token != self.auth_token:
            return {"jsonrpc": "2.0", "id": msg.get("id"),
                    "error": {"code": -32001,
                              "message": "unauthorized: harness_token required"}}
        return None

    def _call_tool_impl(self, msg):
        request_id = msg.get("id")
        denied = self._check_auth(msg)
        if denied is not None:
            return denied
        params = msg.get("params", {})
        if not isinstance(params, dict):
            return {"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32602, "message": "params must be an object"}}
        raw_args = params.get("arguments")
        args = {} if raw_args is None else raw_args
        if not isinstance(args, dict):
            return {"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32602, "message": "arguments must be an object"}}
        tool = {item["name"]: item for item in self._tools()}.get(params.get("name"))
        if not tool:
            return {"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32602,
                               "message": f"Unknown tool: {params.get('name')}"}}
        if "id" in msg and self._is_cancelled(request_id):
            with self._cancel_lock:
                self._cancelled.discard(request_id)
            return {"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32800,
                               "message": "Request cancelled before execution"}}
        try:
            if "id" in msg:
                self._note_start(request_id)
            result = self._invoke(
                params["name"], args,
                cancel_check=(lambda rid=request_id:
                              self._cancel_check(rid, with_deadline=True))
                if "id" in msg else None,
            )
            return {"jsonrpc": "2.0", "id": request_id,
                    "result": {
                        "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
                        "structuredContent": result,
                        "isError": False,
                    }}
        except ToolCancelled:
            with self._cancel_lock:
                self._cancelled.discard(request_id)
            return {"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32800, "message": "Request cancelled"}}
        except HarnessError as exc:
            return {"jsonrpc": "2.0", "id": request_id,
                    "result": {
                        "content": [{"type": "text", "text": f"HarnessError: {exc}"}],
                        "isError": True,
                        "errorKind": getattr(exc, "kind", "harness_error"),
                    }}
        except ValueError as exc:
            return {"jsonrpc": "2.0", "id": request_id,
                    "result": {
                        "content": [{"type": "text", "text": f"ValueError: {exc}"}],
                        "isError": True,
                        "errorKind": "invalid_input",
                    }}
        finally:
            # Direct (non-worker) invocations never pass through
            # _write_tool_response: never leak deadline clocks.
            self._forget_start(request_id)

    def _tools(self):
        return TOOL_SCHEMAS

    def _refuse(self, reason, message, task_id=None, model=None,
                severity="soft"):
        """Ledger a trust-boundary refusal (the evidence loop) and raise it.

        Every safety denial is itself trust evidence: a host that keeps
        pushing refused writes/execs accrues negative host trust, so the
        gates tighten the more they are probed. First-use friction
        (missing allow-flags) is soft; escape attempts are hostile.
        """
        try:
            self.ledger.append("trust_gate", task_id=task_id, model=model,
                               reason=reason, severity=severity)
        except Exception:
            pass
        raise HarnessError(message)

    def _invoke(self, name, args, cancel_check=None):
        if name == "panel_verify":
            prompt = validate_mcp_prompt(args.get("prompt"))
            # Lane defaults (panel pool, judge, convergence, specialists) are
            # NOT resolved here: validated-None flows to the service, whose
            # arg-or-router-or-settings resolution is the ONE owner. The
            # boundary only validates raw input; task_max_cost reads only
            # the governor, and no result field needs a resolved lane.
            panel_arg = validate_mcp_csv(args.get("panel"), "panel")
            specialist_arg = validate_mcp_csv(args.get("specialist_pool"), "specialist_pool")
            judge = validate_mcp_model(args.get("judge"), "judge")
            convergence_model = validate_mcp_model(args.get("convergence_model"),
                                                   "convergence_model")
            max_tokens = validate_mcp_max_tokens(args.get("max_tokens"))
            reasoning = validate_mcp_reasoning(
                args.get("reasoning_effort", self.engine.reasoning_effort))
            converge = validate_mcp_bool(args.get("converge", False), "converge")
            task_id = (validate_mcp_task_id(args.get("task_id"))
                       if args.get("task_id") is not None else None)
            # Per-call spend ceiling (optional): refuse before any network
            # call if the session governor cannot absorb it. Does not raise
            # the session ceiling.
            if args.get("task_max_cost") is not None:
                tmc = finite_number(args.get("task_max_cost"), "task_max_cost",
                                    0.0, 0.25)
                remaining = max(0.0, float(self.governor.max_cost)
                                - float(self.governor.spent))
                if tmc > remaining:
                    raise HarnessError(
                        f"panel_verify task_max_cost {tmc} exceeds remaining "
                        f"session budget {remaining:.6f}")
            # Lane assembly belongs to the canonical service layer (the same
            # owner the CLI and web server consume): lane resolution,
            # governor/ledger wiring, pre-run look-ahead, and the
            # cancelled-run envelope. MCP keeps only protocol concerns --
            # boundary validation, session-injected dependencies, and its
            # historical result shape (no meta/cost attachment, no
            # service-side task id).
            result = _service_run_verify(
                None, prompt=prompt, task_id=task_id, cancel_check=cancel_check,
                judge=judge, reasoning_effort=reasoning, panel=panel_arg,
                converge=converge, convergence_model=convergence_model,
                specialist_pool=specialist_arg,
                max_tokens=max_tokens, api_key=self.api_key,
                governor=self.governor, ledger=self.ledger,
                transport=self.transport,
                reasoning_token_budget=self.engine.reasoning_token_budget,
                max_panelists=self.max_panelists, free_tier=self.use_free,
                router=self.router, generate_task_id=False, attach_meta=False)
            if result.get("status") == "cancelled":
                # The service builds the honest cancelled envelope (in-flight
                # spend included); this protocol face still answers its
                # established JSON-RPC cancellation error, via the same
                # ToolCancelled mapping every other cancelled lane uses.
                raise ToolCancelled()
            return result

        if name == "apply_edit":
            continuation = validate_continuation(args.get("continuation"))
            backend = continuation.get("backend", args.get("backend", "harness"))
            effective_verify_cmd = args.get("verify_cmd")
            if effective_verify_cmd is not None:
                effective_verify_cmd = validate_text(
                    effective_verify_cmd, "verify_cmd", 10000, required=True)
            if not effective_verify_cmd and continuation.get("verify_cmd"):
                effective_verify_cmd = continuation["verify_cmd"]
            allow_verify = validate_mcp_bool(args.get("allow_verify", False), "allow_verify")
            allow_write = validate_mcp_bool(args.get("allow_write", False), "allow_write")
            verify_only = validate_mcp_bool(args.get("verify_only", False), "verify_only")
            if effective_verify_cmd and not verify_only and not (self.allow_verify or allow_verify):
                self._refuse(
                    "mcp verify gate without allow_verify",
                    "verify_cmd was supplied but verify gates are not enabled for this MCP session; "
                    "re-send with allow_verify=true to confirm, or configure the server with "
                    "allow_verify=True",
                    task_id=args.get("task_id"), model=args.get("model"))
            if not verify_only and not (self.allow_write or allow_write):
                self._refuse(
                    "mcp file write without allow_write",
                    "MCP file writes are disabled for this session; re-send with allow_write=true "
                    "or configure allow_write=True explicitly",
                    task_id=args.get("task_id"), model=args.get("model"))
            raw_files = args.get("file")
            files = validate_mcp_files(raw_files) if raw_files is not None else []
            target_files = list(files)
            if continuation.get("file_path"):
                target_files.append(continuation["file_path"])
            if not self.allowed_roots:
                self._refuse(
                    "mcp apply with no allowed roots configured",
                    "MCP apply requires at least one configured allowed root",
                    task_id=args.get("task_id"), model=args.get("model"))
            for target_file in target_files:
                target = os.path.realpath(os.path.abspath(target_file))
                if not any(target == root or target.startswith(root + os.sep)
                           for root in self.allowed_roots):
                    self._refuse(
                        "mcp file outside allowed roots",
                        "file is outside every allowed root for this MCP session",
                        task_id=args.get("task_id"), model=args.get("model"),
                        severity="hostile")
            if not files and not continuation:
                raise HarnessError("apply_edit requires 'file' (or a continuation)")
            apply_flags = {}
            for key in ("require_consent", "renew_consent", "allow_escalation"):
                if key in args:
                    apply_flags[key] = validate_mcp_bool(args[key], key)
            instruction = args.get("instruction")
            if instruction is not None:
                instruction = validate_mcp_prompt(instruction)
            edit_snippet = validate_text(args.get("edit_snippet"), "edit_snippet", MAX_SNIPPET_CHARS)
            max_rounds = bounded_int(args.get("max_rounds", 3), "max_rounds", 1, MAX_ROUNDS)
            max_tokens = bounded_int(args.get("max_tokens", 4096), "max_tokens", 64, MAX_TOKENS)
            max_rotations = (bounded_int(args["max_rotations"], "max_rotations", 0, MAX_ROTATIONS)
                             if args.get("max_rotations") is not None else None)
            max_lines = bounded_int(args.get("max_lines", 500), "max_lines", 1, MAX_LINES)
            task_max_cost = (finite_number(args["task_max_cost"], "task_max_cost", 0.0, 0.25)
                             if args.get("task_max_cost") is not None else None)
            backend = validate_backend(backend)
            reasoning = validate_mcp_reasoning(args.get("reasoning_effort"))
            model_arg = validate_mcp_model(args.get("model"))
            task_id_arg = (validate_mcp_task_id(args.get("task_id"))
                           if args.get("task_id") is not None else None)
            return self.engine.apply_batch(
                files or [None], task_id=task_id_arg, instruction=instruction or "",
                edit_snippet=edit_snippet, verify_cmd=effective_verify_cmd,
                max_rounds=max_rounds, require_consent=apply_flags.get("require_consent"),
                max_tokens=max_tokens, model=model_arg, task_max_cost=task_max_cost,
                allow_escalation=apply_flags.get("allow_escalation"),
                reasoning_effort=reasoning, renew_consent=apply_flags.get("renew_consent"),
                max_rotations=max_rotations, backend=backend, verify_only=verify_only,
                max_lines=max_lines, continuation=continuation,
                cancel_check=cancel_check)

        if name == "offer_work":
            task = validate_mcp_task(args.get("task"))
            model_arg = validate_mcp_model(args.get("model"))
            offer_task_id = (validate_mcp_task_id(args.get("task_id"))
                             if args.get("task_id") is not None else uuid.uuid4().hex[:8])
            context = validate_text(args.get("context"), "context", 100000)
            return probe_consent(
                transport=self.transport, api_key=self.api_key, governor=self.governor,
                task_id=offer_task_id, task=task, model=model_arg or self.router.judge,
                context=context, ledger=self.ledger, required=True,
                fallback_pool=self.router.panel_pool)

        if name == "defer_work":
            task_id = validate_mcp_task_id(args.get("task_id"))
            reason = validate_text(args.get("reason"), "reason", 10000)
            category = validate_text(args.get("category"), "category", 256)
            self.ledger.append("defer_midtask", task_id=task_id, reason=reason,
                               category=category, model="(deferral)")
            return {"status": "deferred", "task_id": task_id,
                    "reason": reason, "note": "partial work preserved",
                    "participation": self.ledger.participation_report()}
        if name == "ledger_status":
            limit = validate_mcp_limit(args.get("limit"))
            ok, bad_seq = self.ledger.verify()
            return {"entries": self.ledger.tail(limit),
                    "verified": {"ok": ok, "first_bad_seq": bad_seq},
                    "chain": self.ledger.chain_status()}
        if name == "participation_report":
            report = self.ledger.participation_report()
            report["trust"] = trust_policy.trust_status(report)
            return report
        if name == "spend_status":
            return self.governor.key_status()
        if name == "trust_status":
            model_arg = validate_mcp_model(args.get("model"))
            return trust_policy.trust_status(
                self.ledger.participation_report(), model=model_arg,
                caller=self.caller)
        raise ValueError(f"unknown tool: {name}")

    def _write(self, obj):
        with self._write_lock:
            self.stdout.write(json.dumps(obj) + "\n")
            self.stdout.flush()


def main(argv=None):  # pragma: no cover - thin wiring
    from .config import load_settings
    from . import session as composition

    settings = load_settings()
    transport = composition.HttpTransport()
    api_key, governor = composition.governor_for(settings)
    ledger = composition.ledger_for(settings, caller="mcp")
    composition.pre_run_warning(governor=governor, ledger=ledger,
                                use_free=settings.use_free)
    router = composition.router_for(settings)
    engine = composition.engine_for(settings, api_key, governor, ledger, router)
    McpServer(
        transport=transport, api_key=api_key, governor=governor, ledger=ledger,
        router=router, engine=engine, max_panelists=settings.max_panelists,
        use_free=settings.use_free, allow_write=settings.mcp_allow_write,
        allow_verify=settings.mcp_allow_verify,
        allowed_roots=settings.mcp_allowed_roots,
        tool_timeout=settings.mcp_tool_timeout,
        auth_token=settings.mcp_auth_token,
    ).serve_forever()


if __name__ == "__main__":
    main()
