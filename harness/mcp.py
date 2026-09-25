"""Native MCP stdio server.

This module owns JSON-RPC framing, dispatch, cancellation lifecycle, and
response serialization; the tool contracts (schemas) live in
harness/mcp_schemas.py as pure data, and the lane-scheduling policy
(which serial worker runs each tool) lives in harness/mcp_lanes.py.
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
from . import events as _events
from . import trust as trust_policy
from .batch import BatchOptions
from .consent import probe_consent
from .continuation import validate_continuation
from .config import resolve_hourglass
from .dag import TaskDAG
from .waist import compose_plan
from .errors import HarnessError, ToolCancelled
from .executor import DEFAULT_PLAN_WORKERS, PlanExecutor
from .mcp_lanes import LANES, lane_for
from .mcp_schemas import TOOL_SCHEMAS
from .jev_completion import dogfood_phase, score_all_phases
from .jev_policy import aggregate_structural, policy_for
from .jev_packs import validate_log_pack, validate_operator_pack
from .route_pack import validate_route_pack
from .log_analysis import analyze_log
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

# Default per-tool deadline (seconds): cooperative, tripped through the
# same cancel_check as notifications/cancelled. Configurable via
# HARNESS_MCP_TOOL_TIMEOUT (60..7200).
MCP_TOOL_TIMEOUT_DEFAULT = 1800


def _valid_rpc_id(value):
    """JSON-RPC request ids are finite strings, numbers, or null."""
    return (value is None or isinstance(value, str)
            or (isinstance(value, int) and not isinstance(value, bool))
            or (isinstance(value, float) and math.isfinite(value)))


def _valid_progress_token(value):
    """MCP progress tokens are strings or integers (never bool)."""
    return isinstance(value, str) or (isinstance(value, int)
                                      and not isinstance(value, bool))


def _progress_message(event):
    """One human-readable progress line from a typed run event (the spec's
    message SHOULD be human-readable; it never carries the key label)."""
    etype = str(event.get("type") or "event")
    bits = []
    if event.get("model"):
        bits.append(str(event["model"]))
    if event.get("status"):
        bits.append(str(event["status"]))
    if event.get("reason"):
        bits.append(str(event["reason"]))
    cost = event.get("cost")
    if isinstance(cost, (int, float)):
        bits.append(f"${float(cost):.6f}")
    return f"{etype}: {' — '.join(bits)}" if bits else etype


def _reject_nonstandard_json(value):
    raise ValueError(f"non-standard JSON constant: {value}")


class McpServer:
    """Lane workers (one serial worker per lane) with an independent stdio reader."""

    def __init__(self, *, transport, api_key, governor, ledger, router, engine,
                 max_panelists=3, use_free=True, stdin=None, stdout=None,
                 allow_verify=False, allow_write=False, allowed_roots=None,
                 tool_timeout=None, caller=None, auth_token=None,
                 hourglass=None):
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
        # progressToken streaming: request id -> registered events sink,
        # only while that request is in flight. A request whose client did
        # not send params._meta.progressToken never appears here.
        self._progress = {}
        self.tool_timeout = (MCP_TOOL_TIMEOUT_DEFAULT if tool_timeout is None
                             else tool_timeout)
        # Optional shared secret. When set, tools/call must present
        # params._meta.harness_token (or params.harness_token) matching.
        # Empty = inherited stdio authority (documented trust model).
        self.auth_token = auth_token or None
        # Auto-scaling hourglass defaults for plan_and_execute (waist
        # confirmation, parallel stages, worktree isolation, diff-bound
        # write attestation). All on unless the settings file turns one
        # off; a per-request argument still wins. main() seeds these from
        # config.resolve_hourglass -- the ONE mapping every lane reads.
        self.hourglass = {
            "confirm": True, "isolate": True, "parallel": True,
            "require_diff_authorization": True,
            # HG-decompose-default: rides the hourglass (True when active).
            "decompose": True,
        }
        self.hourglass.update(hourglass or {})
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

    # ---------------- deadlines + cancellation ----------------
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
                 for lane in LANES}
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
                    pools[lane_for(tool_name)].submit(
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
                self._remove_progress(request_id)
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
        # MCP lifecycle: echo a supported version; otherwise answer with the
        # version this server speaks and let the client decide to proceed or
        # disconnect. Erroring here locked out newer clients (Claude Code
        # 2.1 sends 2025-11-25) that happily speak 2025-06-18.
        if requested is None:
            return PROTOCOL_VERSION
        if not isinstance(requested, str) or not requested:
            raise HarnessError("protocolVersion must be a non-empty string")
        return PROTOCOL_VERSION

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
        if "id" in msg:
            # Opt-in live progress (MCP 2025-06-18 utilities/progress): a
            # client that includes params._meta.progressToken receives one
            # notifications/progress frame per typed run event until the
            # response frame is written. Without the token, zero progress
            # frames -- the historical behavior.
            meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
            token = meta.get("progressToken")
            if _valid_progress_token(token):
                self._register_progress(request_id, token)
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
            # _write_tool_response: never leak deadline clocks or progress
            # sinks (progress MUST stop after completion).
            self._forget_start(request_id)
            self._remove_progress(request_id)

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
            for key in ("require_consent", "renew_consent", "allow_escalation",
                        "require_diff_authorization"):
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
                files or [None], task_id=task_id_arg, cancel_check=cancel_check,
                options=BatchOptions(
                    instruction=instruction or "", edit_snippet=edit_snippet,
                    verify_cmd=effective_verify_cmd, max_rounds=max_rounds,
                    require_consent=apply_flags.get("require_consent"),
                    max_tokens=max_tokens, model=model_arg,
                    task_max_cost=task_max_cost,
                    allow_escalation=apply_flags.get("allow_escalation"),
                    reasoning_effort=reasoning,
                    renew_consent=apply_flags.get("renew_consent"),
                    require_diff_authorization=apply_flags.get(
                        "require_diff_authorization"),
                    max_rotations=max_rotations, backend=backend,
                    verify_only=verify_only, max_lines=max_lines,
                    continuation=continuation))

        if name == "continue_work":
            # Thin face over the existing continue lane (apply_edit's own
            # continuation path, harness.cli._cmd_continue, and the UI's
            # Continue pane all resume through the SAME
            # engine.apply_batch(continuation=...) call -- no second
            # resume implementation). Write-gated exactly like apply_edit:
            # refuses without allow_write, and the continuation's target
            # must be inside an allowed root.
            continuation = validate_continuation(args.get("continuation"))
            if not continuation:
                raise HarnessError("continue_work requires 'continuation'")
            effective_verify_cmd = args.get("verify_cmd")
            if effective_verify_cmd is not None:
                effective_verify_cmd = validate_text(
                    effective_verify_cmd, "verify_cmd", 10000, required=True)
            if not effective_verify_cmd and continuation.get("verify_cmd"):
                effective_verify_cmd = continuation["verify_cmd"]
            allow_verify = validate_mcp_bool(args.get("allow_verify", False), "allow_verify")
            allow_write = validate_mcp_bool(args.get("allow_write", False), "allow_write")
            verify_only = bool(continuation.get("verify_only", False))
            if effective_verify_cmd and not verify_only and not (self.allow_verify or allow_verify):
                self._refuse(
                    "mcp verify gate without allow_verify",
                    "verify_cmd was supplied but verify gates are not enabled for this MCP "
                    "session; re-send with allow_verify=true to confirm, or configure the "
                    "server with allow_verify=True",
                    task_id=args.get("task_id"))
            if not verify_only and not (self.allow_write or allow_write):
                self._refuse(
                    "mcp file write without allow_write",
                    "MCP file writes are disabled for this session; re-send with "
                    "allow_write=true or configure allow_write=True explicitly",
                    task_id=args.get("task_id"))
            if not self.allowed_roots:
                self._refuse(
                    "mcp continue with no allowed roots configured",
                    "MCP continue_work requires at least one configured allowed root",
                    task_id=args.get("task_id"))
            target = os.path.realpath(os.path.abspath(continuation["file_path"]))
            if not any(target == root or target.startswith(root + os.sep)
                       for root in self.allowed_roots):
                self._refuse(
                    "mcp file outside allowed roots",
                    "continuation target is outside every allowed root for this MCP session",
                    task_id=args.get("task_id"), severity="hostile")
            instruction = args.get("instruction")
            if instruction is not None:
                instruction = validate_mcp_prompt(instruction)
            max_rounds = bounded_int(args.get("max_rounds", 3), "max_rounds", 1, MAX_ROUNDS)
            task_id_arg = (validate_mcp_task_id(args.get("task_id"))
                           if args.get("task_id") is not None else None)
            return self.engine.apply_batch(
                [None], task_id=task_id_arg, cancel_check=cancel_check,
                options=BatchOptions(
                    instruction=instruction, verify_cmd=effective_verify_cmd,
                    max_rounds=max_rounds, continuation=continuation))

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
                fallback_pool=self.router.panel_pool,
                min_confidence=getattr(getattr(self, "settings", None),
                                       "min_confidence", 0.70))

        if name == "defer_work":
            task_id = validate_mcp_task_id(args.get("task_id"))
            reason = validate_text(args.get("reason"), "reason", 10000)
            category = validate_text(args.get("category"), "category", 256)
            self.ledger.append("defer_midtask", task_id=task_id, reason=reason,
                               category=category, model="(deferral)")
            return {"status": "deferred", "task_id": task_id,
                    "reason": reason, "note": "partial work preserved",
                    "participation": self.ledger.participation_report()}
        if name == "issue_sort":
            # Thin face over the ONE policy owner (jev_policy.evaluate_issue_sort).
            # No second Jev client; combo fields come only from the operator pack.
            issue = validate_text(args.get("issue"), "issue", 100000, required=True)
            raw_pack = args.get("pack")
            if raw_pack is None:
                raise HarnessError(
                    "issue_sort requires 'pack' (operator-declared bucket pack)")
            pack = validate_operator_pack(raw_pack)
            jev_policy = getattr(self.engine, "jev_policy", None)
            if jev_policy is None:
                settings = getattr(self.engine, "settings", None)
                if settings is not None:
                    jev_policy = policy_for(
                        settings, transport=self.transport,
                        governor=self.governor, ledger=self.ledger)
            if jev_policy is None:
                raise HarnessError("issue_sort requires a Jev policy on the engine")
            result, structural, combo = jev_policy.evaluate_issue_sort(
                {"issue": issue}, pack, site="issue_sort")
            return {
                "status": "ok" if combo.get("bucket") else "unmatched",
                "combo": combo,
                "structural": structural,
                "answers": getattr(result, "answers", {}),
                "reasons": getattr(result, "reasons", []),
                "is_fallback": bool(combo.get("is_fallback")),
                "bucket": combo.get("bucket"),
                "path_id": combo.get("path_id"),
                "suggested_next_action": combo.get("suggested_next_action"),
                "pack_id": combo.get("pack_id"),
            }
        if name == "route_query":
            # Thin face over the ONE policy owner
            # (jev_policy.evaluate_model_route). No second Jev client; combo
            # fields come only from the declared route pack.
            goal = validate_text(args.get("goal"), "goal", 100000, required=True)
            raw_pack = args.get("pack")
            if raw_pack is None:
                raise HarnessError(
                    "route_query requires 'pack' (operator-declared rung ladder)")
            try:
                pack = validate_route_pack(raw_pack)
            except ValueError as exc:
                raise HarnessError(f"route pack invalid: {exc}") from exc
            jev_policy = getattr(self.engine, "jev_policy", None)
            if jev_policy is None:
                settings = getattr(self.engine, "settings", None)
                if settings is not None:
                    jev_policy = policy_for(
                        settings, transport=self.transport,
                        governor=self.governor, ledger=self.ledger)
            if jev_policy is None:
                raise HarnessError("route_query requires a Jev policy on the engine")
            result, structural, combo = jev_policy.evaluate_model_route(
                {"goal": goal}, pack, site="model_route")
            return {
                "status": "ok" if combo.get("rung_id") else "unroutable",
                "route": combo,
                "structural": structural,
                "answers": getattr(result, "answers", {}),
                "reasons": getattr(result, "reasons", []),
                "is_fallback": bool(combo.get("is_fallback")),
                "rung_id": combo.get("rung_id"),
                "tier": combo.get("tier"),
                "model": combo.get("model"),
                "cost_class": combo.get("cost_class"),
                "pack_id": combo.get("pack_id"),
            }
        if name == "log_judgment":
            # Thin face over the ONE policy owner (jev_policy.evaluate_log_item)
            # + code-owned aggregation (log_analysis). Never invents buckets,
            # levels, paths, or actions; unmatched stays honest.
            log_text = validate_text(args.get("log_text"), "log_text", 8_000_000,
                                     required=True)
            raw_pack = args.get("pack")
            if raw_pack is None:
                raise HarnessError(
                    "log_judgment requires 'pack' (frozen operator log pack)")
            pack = validate_log_pack(raw_pack)
            jev_policy = getattr(self.engine, "jev_policy", None)
            if jev_policy is None:
                settings = getattr(self.engine, "settings", None)
                if settings is not None:
                    jev_policy = policy_for(
                        settings, transport=self.transport,
                        governor=self.governor, ledger=self.ledger)
            if jev_policy is None:
                raise HarnessError("log_judgment requires a Jev policy on the engine")
            analysis = analyze_log(
                log_text, pack, jev_policy,
                info_sample=int(args.get("info_sample") or 0),
                task_id=validate_mcp_task_id(args.get("task_id"))
                if args.get("task_id") else None)
            return {
                "status": "ok",
                "analysis": analysis,
                "pack_id": analysis.get("pack_id"),
                "coverage": analysis.get("coverage"),
            }
        if name == "mission_status":
            # Thin face over the HUL-A mission pack (harness.mission_record):
            # same read-only refresh `harness mission status` runs
            # (regenerate STATUS.md/INDEX.md, return pack_summary). Never
            # mutates budget, receipts, or resume state.
            from . import mission_record as mr
            mission_id = validate_text(args.get("mission_id"), "mission_id", 256,
                                       required=True)
            root = validate_text(args.get("root"), "root", 4096) or "missions"
            pack = mr.load_mission_pack(root, mission_id)
            mr.write_status(pack)
            mr.write_index(pack)
            return mr.pack_summary(pack)
        if name == "jev_phase":
            # Thin face over harness.jev_completion (the same engine
            # `harness jev-phase --local-only` runs). Always local-only:
            # never builds a live Jev policy, never mutates STATUS/the repo.
            repo_root = validate_text(args.get("repo_root"), "repo_root", 4096) or "."
            all_phases = validate_mcp_bool(args.get("all", False), "all")
            min_score = finite_number(args.get("min_score", 85.0), "min_score",
                                      0.0, 100.0)
            if all_phases:
                return score_all_phases(repo_root, jev_policy=None,
                                        min_score=min_score)
            phase = validate_text(args.get("phase"), "phase", 256, required=True)
            return dogfood_phase(repo_root, phase, settings=None,
                                 use_live_jev=False, min_score=min_score)
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
        if name == "plan_and_execute":
            goal = validate_text(args.get("goal"), "goal", 20000, required=True)
            execute = validate_mcp_bool(args.get("execute", False), "execute")
            parallel = validate_mcp_bool(
                args.get("parallel", self.hourglass["parallel"]), "parallel")
            allow_write = validate_mcp_bool(args.get("allow_write", False), "allow_write")
            allow_escalation = validate_mcp_bool(args.get("allow_escalation", False), "allow_escalation")
            decompose_llm = validate_mcp_bool(
                args.get("decompose_llm", self.hourglass["decompose"]),
                "decompose_llm")
            confirm = validate_mcp_bool(
                args.get("confirm", self.hourglass["confirm"]), "confirm")
            plan_consensus = validate_mcp_bool(
                args.get("plan_consensus", self.hourglass.get("plan_consensus", False)),
                "plan_consensus")
            require_auth = validate_mcp_bool(
                args.get("require_diff_authorization",
                         self.hourglass["require_diff_authorization"]),
                "require_diff_authorization")
            final_gate = args.get("final_gate", None)
            if final_gate is not None and not isinstance(final_gate, str):
                final_gate = validate_mcp_bool(final_gate, "final_gate")
            max_workers = int(args.get("max_workers", DEFAULT_PLAN_WORKERS)
                              or DEFAULT_PLAN_WORKERS)
            frontier_model = validate_mcp_model(args.get("frontier_model"), "frontier_model")
            raw_files = args.get("file")
            candidate_files = validate_mcp_files(raw_files) if raw_files is not None else []

            if execute and not (self.allow_write or allow_write):
                self._refuse(
                    "mcp file write without allow_write",
                    "plan_and_execute file writes are disabled for this session; "
                    "re-send with allow_write=true or configure allow_write=True explicitly",
                    model=frontier_model)

            jev_policy = getattr(self.engine, "jev_policy", None)
            if jev_policy is None:
                settings = getattr(self.engine, "settings", None)
                if settings is not None:
                    jev_policy = policy_for(
                        settings, transport=self.transport,
                        governor=self.governor, ledger=self.ledger)
            plan_result = compose_plan(
                transport=self.transport, api_key=self.api_key,
                governor=self.governor, ledger=self.ledger, opts_goal=goal,
                candidate_files=candidate_files, frontier_model=frontier_model,
                use_free=self.use_free, decompose_llm=decompose_llm,
                confirm=confirm, execute=execute,
                allow_escalation=allow_escalation,
                plan_consensus=plan_consensus,
                jev_policy=jev_policy)
            if plan_result.get("status") == "refused":
                # Waist refusal / composed-ceiling / unreachable-waist is
                # terminal evidence: the plan never executes.
                return plan_result
            if not execute:
                if isinstance(plan_result.get("structural"), dict):
                    plan_result["structural"] = dict(plan_result["structural"])
                    plan_result["structural"]["site"] = "mcp"
                return plan_result

            dag = TaskDAG.from_dict(plan_result["dag"])
            node_routes = {n.get("node_id"): n for n in plan_result["nodes"]}
            run_gate = None
            for n in plan_result.get("nodes") or ():
                if isinstance(n, dict) and n.get("local_gate"):
                    run_gate = n["local_gate"]
                    break
            # ONE execution assembly for every lane (executor.PlanExecutor):
            # the same object the CLI plan lane and the agent's edit lane
            # build, so parallelism/isolation/reservation/final-gate policy
            # is derived once.
            plan_exec = PlanExecutor(
                self.engine, node_routes,
                parallel=parallel, isolate=self.hourglass["isolate"],
                max_workers=max_workers,
                # The write-attestation switch: this assembly threads it into
                # every node write it dispatches.
                require_diff_authorization=require_auth,
                base_apply_kwargs={
                    "allow_verify": self.allow_verify,
                    "require_consent": False,
                },
                # This server's real budget, so a node reservation can never
                # be bounded by an unrelated nominal default instead.
                run_ceiling=self.governor.max_cost,
                final_gate=final_gate,
                run_gate=run_gate)
            all_results = plan_exec.execute(dag)
            summary = PlanExecutor.summarize(all_results)
            output = {
                "status": "ok" if summary["all_ok"] else "failed",
                "goal": goal,
                "total_nodes": len(dag.nodes),
                "completed_nodes": summary["completed"],
                "results": [r for key, r in all_results.items()
                            if key != "final_gate"],
                "cost": summary["total_cost"],
                "dag": plan_result["dag"],
                "composed_worst_case": plan_result.get("composed_worst_case"),
                "final_gate": summary.get("final_gate"),
            }
            structural = aggregate_structural(
                list(all_results.values()), site="mcp")
            if structural is None and isinstance(plan_result.get("structural"), dict):
                structural = dict(plan_result["structural"])
                structural["site"] = "mcp"
            if structural is not None:
                output["structural"] = structural
            return output
        raise ValueError(f"unknown tool: {name}")

    # ---------------- notifications/progress streaming ----------------
    def _progress_sink(self, token):
        """One events sink bound to a request's progressToken: each typed
        run event (panel_call, gate_end, rotation, ...) becomes a
        notifications/progress frame with a monotonically increasing
        progress value. No total is sent (the lanes don't know one).
        Events are connection-scoped; the token is what the host uses to
        correlate them. A failing frame write is the events bus's problem
        (broken sinks are dropped, never raised into the lane)."""
        state = {"n": 0}

        def sink(event):
            state["n"] += 1
            self._write({
                "jsonrpc": "2.0",
                "method": "notifications/progress",
                "params": {
                    "progressToken": token,
                    "progress": state["n"],
                    "message": _progress_message(event),
                },
            })

        return sink

    def _register_progress(self, request_id, token):
        """Opt-in: the client asked for progress on this request. Refused
        registration (sink table full) simply means no progress for that
        request -- the result envelope is unaffected."""
        sink = _events.add_sink(self._progress_sink(token))
        if sink is not None:
            with self._cancel_lock:
                self._progress[request_id] = sink

    def _remove_progress(self, request_id):
        with self._cancel_lock:
            sink = self._progress.pop(request_id, None)
        if sink is not None:
            _events.remove_sink(sink)

    def _write(self, obj):
        with self._write_lock:
            self.stdout.write(json.dumps(obj) + "\n")
            self.stdout.flush()


def _handle_cli_flags(argv, *, stdout=None):
    """DF-DOCS-2: `harness-mcp --help` / `--version` must answer and exit
    without touching stdio-JSON-RPC mode -- an operator checking the entry
    point (or a packaging smoke test) should never block on a server that's
    waiting for a JSON-RPC frame on stdin. Returns an exit code (int) if a
    flag was handled, or None to fall through to serving. Kept separate
    from main() so it can be exercised without starting a server."""
    stdout = stdout or sys.stdout
    args = list(argv if argv is not None else sys.argv[1:])
    if "--version" in args:
        stdout.write("harness-mcp {}\n".format(__version__))
        return 0
    if "--help" in args or "-h" in args:
        stdout.write(
            "usage: harness-mcp [--help] [--version]\n\n"
            "Native MCP stdio server (JSON-RPC 2.0 over stdin/stdout).\n"
            "Run with no arguments to serve: it then blocks reading "
            "JSON-RPC requests from stdin until the client closes the "
            "pipe or sends 'shutdown'/'exit'.\n\n"
            "  --help, -h   show this message and exit\n"
            "  --version    print the server version and exit\n"
        )
        return 0
    return None


def main(argv=None):  # pragma: no cover - thin wiring; exercised via smoke test
    exit_code = _handle_cli_flags(argv)
    if exit_code is not None:
        return exit_code

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
        hourglass=resolve_hourglass(settings),
    ).serve_forever()


if __name__ == "__main__":
    main()
