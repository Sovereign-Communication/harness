"""harness serve: the localhost web UI + JSON API over the ONE core.

The third face of the harness (library, CLI, MCP, and now a local web UI).
Design rules, inherited from the MCP server:

- **No new policy.** Dispatch endpoints call the same engine entry points the
  CLI calls (apply_session / engine.apply_batch / cli._run_claims_verify /
  bench.run_bench). Consent, spend preflight, verify-gate validation, and the
  trust gates all stay exactly where they are -- the UI adds a human approval
  step (the dispatch form) on top, never instead.
- **Loopback only.** The server binds 127.0.0.1 and rejects non-loopback
  Host headers (DNS-rebinding guard). An optional shared token
  (``--auth-token`` / ``HARNESS_UI_AUTH_TOKEN``) gates every /api route --
  the HARNESS_MCP_AUTH_TOKEN precedent.
- **Events, not scraping.** Run progress is the Phase-1 typed event stream
  (harness/events.py): the server registers one sink into a bounded ring
  buffer; the browser polls ``/api/events`` / ``/api/runs/{id}/events``.
- **Cooperative cancel.** Runs receive a cancel_check closure (the same
  seam the MCP server uses); ``POST /api/runs/{id}/cancel`` flips it.
"""
import argparse
import atexit
import json
import os
import re
import sys
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from . import events as _events
from .agent import AutonomousAgent, classify_prompt_intent
import harness.history as _history
from .batch import BatchOptions
from .config import (HARD_MAX_COST, HARD_TASK_MAX_COST, load_settings,
                     resolve_api_key, update_config)
from .errors import HarnessError, ToolCancelled
from . import osal
from .history import delete_chat_session, list_chat_sessions, load_chat_history as _history_load_chat_history
from .session import (apply_session, governor_for, ledger_for, run_meta)

# Single-owner delegation with patch-propagation: tests patch server.get_default_history_dir,
# so wrappers temporarily install that patched function into harness.history before delegating.
_orig_get_default_history_dir = _history.get_default_history_dir
def get_default_history_dir():
    return _orig_get_default_history_dir()

def _list_chat_sessions():
    orig = _history.get_default_history_dir
    try:
        _history.get_default_history_dir = get_default_history_dir
        return list_chat_sessions()
    finally:
        _history.get_default_history_dir = orig

def _delete_chat_session(session_id: str) -> bool:
    orig = _history.get_default_history_dir
    try:
        _history.get_default_history_dir = get_default_history_dir
        return delete_chat_session(session_id)
    finally:
        _history.get_default_history_dir = orig

def load_chat_history(session_id: str, history_dir=None):
    orig = _history.get_default_history_dir
    try:
        _history.get_default_history_dir = get_default_history_dir
        return _history_load_chat_history(session_id, history_dir)
    finally:
        _history.get_default_history_dir = orig


UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
# SITE local mode: the static Proof Bench pages (site/public) served next to
# the legacy UI, so `harness serve` is the single local entrypoint for both.
SITE_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir,
                         "site", "public")
SITE_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}
MAX_EVENT_BUFFER = 4000

# Rankings reports (advisory, read-only mirror): the `harness rankings`
# output layout -- rankings/rankings-YYYY-MM-DD.json -- the same files the
# weekly workflow uploads as its artifact. The UI renders the latest
# report; it never generates one (refresh stays CLI-only, so nothing
# auto-mutates).
RANKINGS_REPORT_DIR = "rankings"
RANKINGS_REPORT_RE = re.compile(r"^rankings-\d{4}-\d{2}-\d{2}.*\.json$")
RUN_ID_RE = re.compile(r"^/api/runs/([A-Za-z0-9_-]{1,64})$")
RUN_SUB_RE = re.compile(r"^/api/runs/([A-Za-z0-9_-]{1,64})/(result|events|cancel)$")
# DF-UI-2: mission id grammar mirrors mission_record._MISSION_ID_RE (the one
# owner for what a valid pack directory name is); an id this fails to match
# cannot exist as a pack, so load_mission_pack raises a clean 400/404 either
# way -- this regex only needs to isolate the path segment.
MISSION_ID_PATH_RE = re.compile(r"^/api/missions/([^/]+)$")


def _rankings_reports():
    """Available rankings reports, newest filename first. The test seam:
    patch this to serve fixture reports without touching the filesystem."""
    try:
        names = os.listdir(RANKINGS_REPORT_DIR)
    except OSError:
        return []
    return [os.path.join(RANKINGS_REPORT_DIR, n)
            for n in sorted((n for n in names if RANKINGS_REPORT_RE.match(n)),
                            reverse=True)]

STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    # SITE-8 panes (additive; legacy chat UI remains the default view)
    "/panes.js": ("panes.js", "application/javascript; charset=utf-8"),
    "/panes.css": ("panes.css", "text/css; charset=utf-8"),
}


def _finite_float(value, name, low, high):
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise HarnessError(f"{name} must be a number") from None
    if not (low <= v <= high):
        raise HarnessError(f"{name} must be between {low} and {high}")
    return v


def _opt_str(args, key, required=False):
    v = args.get(key)
    if v is None or v == "":
        if required:
            raise HarnessError(f"'{key}' is required")
        return None
    if not isinstance(v, str):
        raise HarnessError(f"'{key}' must be a string")
    return v


def _opt_int(args, key, low, high, default=None):
    v = args.get(key, default)
    if v is None:
        return None
    try:
        v = int(v)
    except (TypeError, ValueError):
        raise HarnessError(f"'{key}' must be an integer") from None
    if not (low <= v <= high):
        raise HarnessError(f"'{key}' must be between {low} and {high}")
    return v


def _opt_bool(args, key):
    v = args.get(key)
    return bool(v) if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes") if v else False


def validate_dispatch(kind, args):
    """CLI-boundary validation for UI dispatch. Same discipline as the CLI
    parsers: untrusted input is range-checked before anything runs."""
    if kind not in ("apply", "verify", "continue", "bench", "chat", "dogfood", "plan", "driver_task"):
        raise HarnessError(f"unknown dispatch kind '{kind}'")
    args = dict(args or {})
    if kind == "driver_task":
        _opt_str(args, "goal", required=True)
        _opt_str(args, "target")
        _opt_str(args, "schema")
        _opt_str(args, "verify")
        _opt_int(args, "max_steps", 1, 20, 5)
        args["auto_approve"] = _opt_bool(args, "auto_approve") if "auto_approve" in args else True
        args["require_stable"] = _opt_bool(args, "require_stable") if "require_stable" in args else True
        if args.get("max_cost") is not None:
            args["max_cost"] = _finite_float(args["max_cost"], "max_cost", 0.0, HARD_MAX_COST)
    elif kind == "plan":
        _opt_str(args, "goal", required=True)
        args["execute"] = _opt_bool(args, "execute")
        _opt_str(args, "frontier_model")
        _opt_int(args, "max_workers", 1, 16, 4)
        if args.get("task_max_cost") is not None:
            args["task_max_cost"] = _finite_float(args["task_max_cost"], "task_max_cost", 0.0, HARD_TASK_MAX_COST)
        if args.get("max_cost") is not None:
            args["max_cost"] = _finite_float(args["max_cost"], "max_cost", 0.0, HARD_MAX_COST)
    elif kind == "chat":
        _opt_str(args, "prompt", required=True)
        args["auto_apply"] = _opt_bool(args, "auto_apply") if "auto_apply" in args else True
        _opt_str(args, "session_id")
        rd = _opt_str(args, "root_dir")
        if rd and not os.path.isdir(rd):
            raise HarnessError(f"root_dir does not exist or is not a directory: {rd}")
        _opt_bool(args, "web")
        _opt_bool(args, "allow_paid")
        _opt_bool(args, "allow_escalation")
    elif kind == "dogfood":
        f = _opt_str(args, "file", required=True)
        if not os.path.isfile(f):
            raise HarnessError(f"--file target does not exist: {f}")
        _opt_str(args, "instruction", required=True)
        _opt_str(args, "verify")
        cf = _opt_str(args, "claims_file")
        if cf and not os.path.isfile(cf):
            raise HarnessError(f"claims_file does not exist: {cf}")
        sf = _opt_str(args, "source_file")
        if sf and not os.path.isfile(sf):
            raise HarnessError(f"source_file does not exist: {sf}")
        df = _opt_str(args, "definitions_file")
        if df and not os.path.isfile(df):
            raise HarnessError(f"definitions_file does not exist: {df}")
        _opt_str(args, "claim_context")
        _opt_int(args, "max_rounds", 1, 8, 3)
        if args.get("max_cost") is not None:
            args["max_cost"] = _finite_float(args["max_cost"], "max_cost",
                                             0.0, HARD_MAX_COST)
    elif kind == "apply":
        f = _opt_str(args, "file", required=True)
        if not os.path.isfile(f):
            raise HarnessError(f"--file target does not exist: {f}")
        _opt_str(args, "instruction", required=True)
        _opt_str(args, "verify")
        _opt_int(args, "max_rounds", 1, 8, 3)
        if args.get("backend") and args["backend"] not in ("harness", "morph", "diff"):
            raise HarnessError("'backend' must be harness|morph|diff")
        if args.get("task_max_cost") is not None:
            args["task_max_cost"] = _finite_float(args["task_max_cost"],
                                                  "task_max_cost", 0.0,
                                                  HARD_TASK_MAX_COST)
        args["verify_only"] = _opt_bool(args, "verify_only")
        args["require_consent"] = _opt_bool(args, "require_consent")
    elif kind == "verify":
        has_prompt = bool(_opt_str(args, "prompt"))
        pf = _opt_str(args, "prompt_file")
        cf = _opt_str(args, "claims_file")
        if not has_prompt and not pf and not cf:
            raise HarnessError("verify requires 'prompt', 'prompt_file', "
                               "or 'claims_file'")
        if pf and not os.path.isfile(pf):
            raise HarnessError(f"prompt_file does not exist: {pf}")
        if cf:
            sf = _opt_str(args, "source_file", required=True)
            if not os.path.isfile(sf):
                raise HarnessError(f"source_file does not exist: {sf}")
            df = _opt_str(args, "definitions_file")
            if df and not os.path.isfile(df):
                raise HarnessError(f"definitions_file does not exist: {df}")
        _opt_str(args, "claim_context")
        _opt_str(args, "judge")
        _opt_str(args, "panel")
        if args.get("max_cost") is not None:
            args["max_cost"] = _finite_float(args["max_cost"], "max_cost",
                                             0.0, HARD_MAX_COST)
    elif kind == "continue":
        s = _opt_str(args, "state", required=True)
        if not os.path.isfile(s):
            raise HarnessError(f"state file does not exist: {s}")
        _opt_str(args, "instruction")
        _opt_str(args, "verify")
        _opt_int(args, "max_rounds", 1, 8, None)
    else:  # bench
        m = _opt_str(args, "manifest", required=True)
        if not os.path.exists(m):
            raise HarnessError(f"manifest does not exist: {m}")
        _opt_int(args, "max_rounds", 1, 8, None)
    return args


# ---- runners: the same engine calls the CLI makes, nothing else ----------

def _split_list(value):
    """Same comma-list parse the CLI uses; duplicated here (one line) so the
    UI server does not import the CLI interface."""
    return [x.strip() for x in (value or "").split(",") if x.strip()]


def run_apply_task(task_id, args, cancel_check):
    settings = load_settings()
    engine = apply_session(settings)
    result = engine.apply_batch(
        [args["file"]], task_id=task_id, cancel_check=cancel_check,
        options=BatchOptions(
            instruction=args["instruction"],
            edit_snippet=args.get("edit_snippet"),
            verify_cmd=args.get("verify"),
            max_rounds=args.get("max_rounds") or 3,
            require_consent=args.get("require_consent"),
            model=args.get("model"),
            task_max_cost=args.get("task_max_cost"),
            backend=args.get("backend") or "harness",
            verify_only=bool(args.get("verify_only"))))
    if isinstance(result, dict):
        result["meta"] = run_meta(settings, engine.governor)
    return result


def run_verify_task(task_id, args, cancel_check):
    """The verify lane via the canonical service layer (harness.service):
    one assembly for the prompt lane and the structured-claims lane across
    CLI and UI -- lint runs pre-network, an ungrounded claim set finishes
    the run as ``rejected``, and a cooperative cancel returns the honest
    spend envelope."""
    from .service import build_verify_prompt, run_verify
    settings = load_settings()
    try:
        prompt, claims_lint = build_verify_prompt(
            prompt=args.get("prompt"),
            prompt_file=args.get("prompt_file"),
            claims_file=args.get("claims_file"),
            source_file=args.get("source_file"),
            definitions_file=args.get("definitions_file"),
            claim_context=args.get("claim_context"))
    except ValueError as e:
        raise HarnessError(str(e)) from None
    if claims_lint is not None and not claims_lint["ok"]:
        return {"status": "rejected", "lint": claims_lint, "verdict": None,
                "actual_cost": 0.0}
    return run_verify(settings, prompt=prompt, task_id=task_id,
                      cancel_check=cancel_check,
                      max_cost=args.get("max_cost"),
                      judge=args.get("judge"),
                      reasoning_effort=args.get("reasoning_effort"))


def run_continue_task(task_id, args, cancel_check):
    from .apply import validate_continuation
    settings = load_settings()
    with open(args["state"], encoding="utf-8") as f:
        continuation = validate_continuation(json.load(f))
    engine = apply_session(settings)
    return engine.apply_batch(
        [None], task_id=task_id, cancel_check=cancel_check,
        options=BatchOptions(
            instruction=args.get("instruction"),
            verify_cmd=args.get("verify"),
            max_rounds=args.get("max_rounds") or 3,
            continuation=continuation))


def run_bench_task(task_id, args, cancel_check):
    """Bench runner. Note: run_bench has no cancel seam yet (bench tasks are
    short); the cancel flag is accepted for runner-signature parity and
    simply unused here -- cancel takes effect between tasks at worst."""
    from .bench import load_manifest, run_bench
    settings = load_settings()
    engine = apply_session(settings)
    tasks = load_manifest(args["manifest"])
    if args.get("max_rounds"):
        for t in tasks:
            t.setdefault("max_rounds", args["max_rounds"])
    return run_bench(engine, tasks)


# Web access for chat is opt-in per run ("web": true) and host-restricted.
# The allowlist lives in harness/web.py (ONE owner); the boundary consumes it.






def run_chat_task(task_id, args, cancel_check):
    # Keep ordinary chat in the conversation lane, but let the existing
    # classifier hand explicit mutation requests to the governed edit lane.
    settings = load_settings()
    if args.get("allow_paid") is not None:
        settings.allow_escalation = bool(args["allow_paid"])
    elif args.get("allow_escalation") is not None:
        settings.allow_escalation = bool(args["allow_escalation"])
    prompt = args["prompt"]
    root_dir = Path(args["root_dir"]) if args.get("root_dir") else None
    agent = AutonomousAgent(settings=settings, root_dir=root_dir)
    return agent.run_prompt(
        prompt=prompt,
        auto_apply=args.get("auto_apply", True),
        web=bool(args.get("web", False)),
        session_id=args.get("session_id"),
        cancel_check=cancel_check,
        force_conversation=classify_prompt_intent(prompt) != "edit",
        max_tokens=args.get("max_tokens"),
        reasoning_effort=args.get("reasoning_effort"),
    )


def run_dogfood_task(task_id, args, cancel_check):
    from .service import run_dogfood
    settings = load_settings()
    return run_dogfood(
        settings,
        file=args["file"],
        instruction=args["instruction"],
        verify_cmd=args.get("verify"),
        claims_file=args.get("claims_file"),
        source_file=args.get("source_file"),
        definitions_file=args.get("definitions_file"),
        claim_context=args.get("claim_context"),
        task_id=task_id,
        max_cost=args.get("max_cost"),
        max_rounds=args.get("max_rounds") or 3,
        cancel_check=cancel_check)


def run_plan_task(task_id, args, cancel_check):
    """Run an asynchronous plan or plan+execute task (GAP-plan-http)."""
    from ._http import HttpTransport
    from .config import resolve_hourglass
    from .dag import TaskDAG
    from .executor import DEFAULT_PLAN_WORKERS, PlanExecutor
    from .jev_policy import policy_for
    from .waist import compose_arguments, compose_plan

    settings = load_settings()
    goal = args["goal"]
    execute = bool(args.get("execute", False))
    candidate_files = args.get("files") or args.get("file")
    frontier_model = args.get("frontier_model") or getattr(settings, "frontier_model", None)
    plan_ceiling = args.get("task_max_cost") or args.get("max_cost")

    if execute:
        engine = apply_session(settings, max_cost=plan_ceiling)
        gov, transport, api_key = engine.governor, engine.transport, engine.api_key
    else:
        api_key, gov = governor_for(settings, plan_ceiling)
        transport = HttpTransport()

    hourglass = resolve_hourglass(settings)
    confirm = args.get("confirm", hourglass.get("confirm", True))
    decompose_llm = args.get("decompose_llm", hourglass.get("decompose", True))
    plan_consensus = bool(args.get("plan_consensus", False))

    brief = args.get("brief")
    token_budget_input = args.get("token_budget_input")
    token_budget_output = args.get("token_budget_output")
    stages = args.get("stages")

    composition = compose_arguments(
        settings,
        goal=goal,
        files=candidate_files or [],
        stages=stages,
        brief=brief,
        max_input_tokens=token_budget_input,
        max_output_tokens=token_budget_output,
    )

    jev_policy = policy_for(settings, transport=transport, governor=gov)

    plan_result = compose_plan(
        goal,
        settings=settings,
        transport=transport,
        api_key=api_key,
        governor=gov,
        task_id=task_id,
        candidate_files=candidate_files,
        frontier_model=frontier_model,
        decompose_llm=decompose_llm,
        confirm=confirm,
        plan_consensus=plan_consensus,
        jev_policy=jev_policy,
        cancel_check=cancel_check,
        **composition,
    )

    if not execute or plan_result.get("status") == "refused":
        return plan_result

    dag_dict = plan_result.get("dag")
    if not dag_dict or not dag_dict.get("nodes"):
        return plan_result

    dag = TaskDAG.from_dict(dag_dict)
    executor = PlanExecutor(
        engine=engine,
        dag=dag,
        max_workers=int(args.get("max_workers") or DEFAULT_PLAN_WORKERS),
        parallel=bool(args.get("parallel", hourglass.get("parallel", True))),
        isolate=bool(args.get("isolate", True)),
        require_diff_authorization=bool(
            args.get("require_diff_authorization", hourglass.get("require_diff_authorization", True))
        ),
        final_gate=args.get("final_gate"),
        keep_going=bool(args.get("keep_going", False)),
        task_id=task_id,
        cancel_check=cancel_check,
    )
    exec_result = executor.execute()
    return {"plan": plan_result, "execution": exec_result, "status": exec_result.get("status", "ok")}


_DRIVER_DAEMON = {}
_DRIVER_PROBE_TIMEOUT = 2.0
_DRIVER_DAEMON_LOCK = threading.Lock()


def shutdown_driver_daemon():
    """Stop the in-process driver daemon, if this process started one.

    Idempotent. Closes the listening socket and joins the serve thread so
    nothing outlives the caller (a leaked socket is a ResourceWarning under
    the audit's hermetic run).
    """
    with _DRIVER_DAEMON_LOCK:
        daemon = dict(_DRIVER_DAEMON)
        _DRIVER_DAEMON.clear()
    if not daemon:
        return
    daemon["httpd"].shutdown()
    daemon["httpd"].server_close()
    daemon["thread"].join(timeout=5)


def ensure_driver_daemon(port=None):
    """Start (once) the in-process loopback driver and return its adapter.

    The ONE place the harness starts a driver. The bearer token is whatever
    the operator declared in ``DRIVER_TOKEN``; otherwise the driver service
    generates a random per-start token (``secrets.token_urlsafe(24)``). There
    is no fixed fallback token, and the token is held by the returned
    adapter, never put into ``os.environ`` or into an API response.
    ``port=None`` uses the configured driver port; ``0`` asks the OS.
    """
    from .perception_client import PerceptionAdapter
    import driver_core.config as _dc_config
    import driver_core.server as _dc_server
    with _DRIVER_DAEMON_LOCK:
        if not _DRIVER_DAEMON:
            settings = _dc_config.load_settings()
            service = _dc_server.Service(_dc_server.driver_from_settings(settings))
            httpd, _ = _dc_server.serve(
                host=settings.host,
                port=settings.port if port is None else port,
                service=service, block=False)
            thread = threading.Thread(target=httpd.serve_forever,
                                      name="harness-driver", daemon=True)
            thread.start()
            host, bound = httpd.server_address[:2]
            _DRIVER_DAEMON.update(
                httpd=httpd, thread=thread, token=service.token,
                base_url=f"http://{host}:{bound}")
            atexit.register(shutdown_driver_daemon)
        return PerceptionAdapter(base_url=_DRIVER_DAEMON["base_url"],
                                 token=_DRIVER_DAEMON["token"])


def _driver_adapter(autostart=True):
    """An adapter for the driver: an already-reachable one, else our own."""
    from .perception_client import PerceptionAdapter
    adapter = PerceptionAdapter()
    if not autostart:
        return adapter
    if _DRIVER_DAEMON:
        return ensure_driver_daemon()
    try:
        # A short probe: a loopback driver answers at once, and a socket that
        # accepts but never answers must not stall the caller for the full
        # request timeout before we decide to start our own.
        PerceptionAdapter(timeout=_DRIVER_PROBE_TIMEOUT).health()
        return adapter
    except Exception:
        pass
    try:
        return ensure_driver_daemon()
    except Exception:
        return adapter


def run_driver_task(task_id, args, cancel_check):
    """Executes a multi-step request driven iteratively by Jev and driver_core.

    Breaks the request into steps, iterates using the driver's perception,
    consensus, Jev decision, execution, and verification aspects, and returns
    a full breakdown of each iteration step.
    """
    from .perception_client import PerceptionUnavailable
    from . import events as _events

    goal = args.get("goal") or "execute driver request"
    target = args.get("target") or "cli"
    default_schema = args.get("schema") or "cli"
    max_steps = int(args.get("max_steps") or 5)
    verify_cmd = args.get("verify")
    require_stable = bool(args.get("require_stable", True))
    auto_approve = bool(args.get("auto_approve", True))

    adapter = _driver_adapter()

    steps = []
    completed = False
    cancelled = False
    current_target = target
    current_schema = default_schema
    total_cost = 0.0

    _events.emit("driver_task_start", task_id=task_id, goal=goal, max_steps=max_steps)

    for step_num in range(1, max_steps + 1):
        if cancel_check and cancel_check():
            cancelled = True
            break

        step_id = f"drv-{uuid.uuid4().hex[:8]}"
        _events.emit(
            "driver_step_start",
            task_id=task_id,
            step=step_num,
            step_id=step_id,
            target=current_target,
            schema=current_schema,
        )

        consent = None
        if auto_approve:
            consent = {
                "granted": True,
                "action": "open_window" if step_num == 1 else "type_text",
                "params": {"target": current_target, "goal": goal},
                "by": "operator",
            }

        try:
            env = adapter.step(
                current_target,
                schema=current_schema,
                consent=consent,
                require_stable=require_stable,
                step_id=step_id,
            )
        except PerceptionUnavailable as e:
            env = {
                "step_id": step_id,
                "ok": False,
                "stopped_at": "capture",
                "reason": "no_capture",
                "detail": str(e),
                "cost_usd": 0.0,
            }

        cost = float(env.get("cost_usd") or 0.0)
        total_cost += cost

        # Verification Aspect
        verify_result = None
        if verify_cmd:
            from .gate_runner import run_gate
            try:
                retcode, output = run_gate(verify_cmd, timeout=30)
                verify_result = {
                    "ok": retcode == 0,
                    "returncode": retcode,
                    "output": output[:2000],
                }
            except Exception as ex:
                verify_result = {"ok": False, "error": str(ex)}

        step_record = {
            "step_number": step_num,
            "step_id": step_id,
            "aspect": current_schema,
            "target": current_target,
            "envelope": env,
            "verification": verify_result,
            "cost_usd": cost,
        }
        steps.append(step_record)
        _events.emit(
            "driver_step_complete",
            task_id=task_id,
            step=step_num,
            ok=env.get("ok", False),
            reason=env.get("reason"),
        )

        if verify_result and verify_result.get("ok"):
            completed = True
            break
        if env.get("ok"):
            completed = True
            break

        # Progress pipeline across perception tiers if initial tier stopped
        if current_schema == "cli":
            current_schema = "mcp"
        elif current_schema == "mcp":
            current_schema = "dom"
        elif current_schema == "dom":
            current_schema = "screen"

    audit_status = None
    try:
        audit_status = adapter.verify()
    except Exception:
        pass

    status = ("done" if completed
              else "cancelled" if cancelled else "max_steps_reached")
    ok_steps = sum(1 for st in steps if st["envelope"].get("ok"))
    verified = any((st["verification"] or {}).get("ok") for st in steps)
    if completed and verified:
        summary = (f"Driver goal met: the verify command passed after "
                   f"{len(steps)} step(s) ({ok_steps} driver step(s) ok).")
    elif completed:
        summary = (f"Driver completed {len(steps)} step(s) with {ok_steps} ok; "
                   f"no verify command confirmed the result.")
    elif cancelled:
        summary = (f"Driver cancelled after {len(steps)} step(s); "
                   f"{ok_steps} step(s) ok, nothing verified.")
    else:
        summary = (f"Driver stopped after {len(steps)} step(s) without "
                   f"completing (max_steps={max_steps}); {ok_steps} step(s) ok, "
                   f"nothing verified.")
    return {
        "status": status,
        "task_id": task_id,
        "goal": goal,
        "total_steps": len(steps),
        "ok_steps": ok_steps,
        "steps": steps,
        "total_cost_usd": round(total_cost, 6),
        "audit": audit_status,
        "summary": summary,
    }


RUNNERS = {
    "apply": run_apply_task,
    "verify": run_verify_task,
    "continue": run_continue_task,
    "bench": run_bench_task,
    "chat": run_chat_task,
    "dogfood": run_dogfood_task,
    "plan": run_plan_task,
    "driver_task": run_driver_task,
}


class UiState:
    """One server's shared state: run registry, event ring, auth."""

    CACHE_TTL = 15.0  # seconds: dashboard polls beat on /api/spend et al.

    def __init__(self, auth_token=None, ephemeral_auth=False):
        self.lock = threading.Lock()
        self.runs = {}
        self.events = deque(maxlen=MAX_EVENT_BUFFER)
        self.last_seq = 0
        self.auth_token = auth_token or None
        self.ephemeral_auth = bool(ephemeral_auth)
        self.started_at = time.time()
        self._event_sink_installed = False
        self._api_cache = {}

    def cached(self, key, build):
        """Short-TTL cache for polled read endpoints. Each miss would call
        the live OpenRouter API (and emit a spend_check event), so a 5s
        dashboard poll without this floods both."""
        now = time.time()
        with self.lock:
            hit = self._api_cache.get(key)
            if hit and now - hit[0] < self.CACHE_TTL:
                return hit[1]
        value = build()  # computed outside the lock
        with self.lock:
            self._api_cache[key] = (now, value)
        return value

    # -- events ------------------------------------------------------------
    def install_event_sink(self):
        if not self._event_sink_installed:
            _events.add_sink(self._on_event)
            self._event_sink_installed = True

    def uninstall_event_sink(self):
        if self._event_sink_installed:
            _events.remove_sink(self._on_event)
            self._event_sink_installed = False

    def _on_event(self, event):
        with self.lock:
            self.events.append(event)
            self.last_seq = event.get("seq", self.last_seq)

    def events_after(self, after, task_id=None):
        with self.lock:
            evs = [e for e in self.events if e.get("seq", 0) > after]
        if task_id:
            # A run's stream: its own task_id, or (defensive) events that
            # carry none (e.g. the chat-lane reasoning retry) once the run
            # started. Task-scoped is the contract; None events are noise.
            evs = [e for e in evs if e.get("task_id") == task_id]
        return evs

    # -- runs --------------------------------------------------------------
    def create_run(self, kind, args):
        run_id = uuid.uuid4().hex[:12]
        task_id = f"ui/{run_id}"
        record = {
            "id": run_id, "kind": kind, "task_id": task_id, "args": args,
            "status": "running", "result": None, "error": None,
            "created_at": time.time(), "finished_at": None,
        }
        cancel_flag = threading.Event()
        record["_cancel"] = cancel_flag
        with self.lock:
            self.runs[run_id] = record
        t = threading.Thread(target=self._run_thread,
                             args=(record, task_id, cancel_flag),
                             name=f"harness-ui-{kind}-{run_id}", daemon=True)
        record["thread"] = t
        t.start()
        return record

    def _run_thread(self, record, task_id, cancel_flag):
        _events.emit("run_accepted", task_id=task_id, kind=record["kind"],
                     ui_run=record["id"])
        try:
            runner = RUNNERS[record["kind"]]
            result = runner(task_id, record["args"],
                            cancel_flag.is_set)
            record["result"] = result
            record["status"] = str(result.get("status") or "done")
            if record["status"] == "cancelled":
                # The lane caught ToolCancelled to build an honest spend
                # envelope; the run-level wording must still say who did it.
                record["error"] = "cancelled by user"
        except HarnessError as e:
            record["error"] = str(e)
            record["status"] = "error"
        except ToolCancelled:
            # The user cancelled the run; that is not a failure of the work.
            record["error"] = "cancelled by user"
            record["status"] = "cancelled"
        except Exception as e:  # never let a run thread die silently
            record["error"] = f"{type(e).__name__}: {e}"
            record["status"] = "error"
        record["finished_at"] = time.time()
        _events.emit("run_finished", task_id=task_id, kind=record["kind"],
                     ui_run=record["id"], status=record["status"])

    def run_public(self, record, with_result=False):
        out = {k: record.get(k) for k in
               ("id", "kind", "task_id", "status", "error", "created_at",
                "finished_at")}
        out["cancelled"] = bool(record["_cancel"].is_set())
        if with_result:
            out["result"] = record.get("result")
        return out


def _settings_view():
    """Read-only settings for the UI. Secrets become presence booleans."""
    settings = load_settings()
    d = settings.to_dict()
    # Expose routing posture, never the key itself: the UI must distinguish a
    # free primary lane from a paid escalation ladder.
    d["paid_key_present"] = bool(resolve_api_key())
    if d.get("mcp_auth_token"):
        d["mcp_auth_token"] = None
        d["mcp_auth_token_present"] = True
    if d.get("expect_key_label"):
        # presence only; the label is never echoed (audit #9b rule)
        d["expect_key_label"] = None
        d["expect_key_label_present"] = True
    return d


def _site_demo_snapshot():
    """The labeled demo snapshot (SITE local mode fallback source)."""
    from pathlib import Path
    demo = Path(__file__).resolve().parent.parent / "site" / "data" / "demo" \
        / "snapshot.json"
    try:
        import json
        with open(demo, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as exc:
        return {"schema": "site-snapshot-v1", "contributors": 0,
                "sessions": [], "error": f"demo snapshot unavailable: {exc}"}


# DF-UI-2: read-mostly faces over existing owners --------------------------
# jev-phase (harness.jev_completion), cost (ledger.cost_report), missions
# (harness.mission_record). Each is a thin composition of the same calls
# the CLI already makes; no new policy lives here.

def _api_jev_phase_payload(repo_root, phase, min_score=85.0):
    """Score one phase, or the whole board when ``phase`` is falsy. Always
    local-only: never builds a live Jev policy (settings=None), so this
    endpoint can never place a network call -- the same guarantee
    `harness jev-phase --local-only` and the MCP `jev_phase` tool give."""
    from .jev_completion import dogfood_phase, score_all_phases
    if not phase:
        return score_all_phases(repo_root, jev_policy=None, min_score=min_score)
    return dogfood_phase(repo_root, phase, settings=None, use_live_jev=False,
                         min_score=min_score)


def _list_missions(root, *, limit=25, offset=0):
    """Return one bounded page of compact mission summaries.

    Directory names are ordered for stable offset pagination. Only the
    selected page is loaded, and each result omits append-only history.
    Missing roots are the normal empty state.
    """
    from . import mission_record as mr
    try:
        names = sorted(os.listdir(root))
    except OSError:
        names = []
    candidates = [
        name for name in names
        if os.path.isfile(os.path.join(root, name, "mission.yaml"))
    ]
    total = len(candidates)
    out = []
    for name in candidates[offset:offset + limit]:
        try:
            pack = mr.load_mission_pack(root, name)
            out.append(mr.pack_list_summary(pack))
        except HarnessError:
            continue  # corrupt pack: skip it rather than fail the whole list
    return {"missions": out, "total": total, "limit": limit, "offset": offset}


class UiRequestHandler(BaseHTTPRequestHandler):
    server_version = "harness-ui/0.1"
    protocol_version = "HTTP/1.1"

    @property
    def ui(self) -> UiState:
        return self.server.ui

    # -- plumbing ----------------------------------------------------------
    def log_message(self, fmt, *args):  # route through the harness voice
        sys.stderr.write("[ui] %s\n" % (fmt % args))

    def _send_json(self, obj, code=200):
        body = json.dumps(obj, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code, message):
        self._send_json({"error": message}, code)

    def _authorized(self):
        if not self.ui.auth_token:
            return True
        got = self.headers.get("X-Harness-Auth", "")
        if got == self.ui.auth_token:
            return True
        auth_header = self.headers.get("Authorization", "")
        if auth_header.startswith("Bearer ") and auth_header[7:].strip() == self.ui.auth_token:
            return True
        parsed = urlparse(self.path)
        q = parse_qs(parsed.query)
        if q.get("token", [""])[0] == self.ui.auth_token:
            return True
        return False

    def _host_ok(self):
        """DNS-rebinding guard: with a loopback bind, the Host header must
        name loopback. A browser poisoned by a rebinding DNS answer sends an
        attacker's hostname here; that request is refused."""
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        return host in ("127.0.0.1", "localhost", "::1")

    def _guard(self):
        if not self._host_ok():
            self._error(403, "forbidden host (loopback only)")
            return False
        if not self._authorized():
            self._error(401, "missing or wrong X-Harness-Auth token")
            return False
        return True

    # -- GET ---------------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        q = parse_qs(parsed.query)
        if path in STATIC_FILES:
            return self._static(STATIC_FILES[path])
        if path.startswith("/site/"):
            return self._site_static(path)
        if not self._guard():
            return
        try:
            if path == "/api/status":
                return self._api_status()
            if path == "/api/trust":
                return self._api_trust(q)
            if path == "/api/runs":
                return self._api_runs()
            if path == "/api/events":
                return self._api_events(q)
            m = RUN_SUB_RE.match(path)
            if m:
                run_id, sub = m.group(1), m.group(2)
                return self._api_run_sub(run_id, sub, q)
            if path == "/api/settings":
                return self._send_json({"settings": _settings_view()})
            if path == "/api/spend":
                return self._api_spend()
            if path == "/api/ledger/tail":
                return self._api_ledger_tail(q)
            if path == "/api/ledger/verify":
                return self._api_ledger_verify()
            if path == "/api/ledger/report":
                return self._api_ledger_report()
            if path == "/api/ledger/defer-stats":
                return self._api_ledger_defer_stats(q)
            if path == "/api/capabilities":
                return self._api_capabilities()
            if path == "/api/models":
                return self._api_models(q)
            if path == "/api/rankings":
                return self._api_rankings()
            if path == "/api/snapshot":
                # SITE local mode: the static Proof Bench site reads its
                # snapshot here (same payload site/data snapshots use).
                return self._api_site_snapshot()
            if path == "/api/site/demo-snapshot":
                return self._send_json(_site_demo_snapshot())
            if path == "/api/chat/history":
                sid = (q.get("session_id") or ["default"])[0]
                return self._send_json({"session_id": sid, "history": load_chat_history(sid)})
            if path == "/api/chat/sessions":
                return self._send_json({"sessions": _list_chat_sessions()})
            if path == "/api/jev-phase":
                return self._api_jev_phase(q)
            if path == "/api/cost":
                return self._api_cost(q)
            if path == "/api/missions":
                return self._api_missions_list(q)
            if path == "/api/driver/health":
                return self._api_driver_health()
            if path == "/api/driver/vocabulary":
                return self._api_driver_vocabulary()
            if path == "/api/driver/schemas":
                return self._api_driver_schemas()
            if path == "/api/driver/verify":
                return self._api_driver_verify()
            m = MISSION_ID_PATH_RE.match(path)
            if m:
                return self._api_mission_detail(m.group(1), q)
            return self._error(404, f"no such endpoint: {path}")
        except HarnessError as e:
            return self._error(400, str(e))
        except Exception as e:
            return self._error(500, f"{type(e).__name__}: {e}")

    # -- POST --------------------------------------------------------------
    def do_POST(self):
        parsed = urlparse(self.path)
        if not self._guard():
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except ValueError:
            return self._error(400, "request body is not valid JSON")
        try:
            if parsed.path == "/api/chat":
                # Single chat box entry point: text in, text out
                return self._api_dispatch({"kind": "chat", "args": body})
            if parsed.path == "/api/chat/session/delete":
                return self._api_session_delete(body)
            if parsed.path == "/api/settings":
                return self._api_settings_update(body)
            if parsed.path == "/api/runs":
                return self._api_dispatch(body)
            if parsed.path == "/api/driver/step":
                return self._api_driver_step(body)
            if parsed.path == "/api/driver/start":
                return self._api_driver_start(body)
            if parsed.path == "/api/driver/drive":
                return self._api_driver_drive(body)
            if parsed.path == "/api/route":
                # SITE local mode: same policy owner as `harness route`.
                return self._api_site_route(body)
            m = RUN_SUB_RE.match(parsed.path)
            if m and m.group(2) == "cancel":
                return self._api_cancel(m.group(1))
            return self._error(404, f"no such endpoint: {parsed.path}")
        except HarnessError as e:
            return self._error(400, str(e))
        except Exception as e:
            return self._error(500, f"{type(e).__name__}: {e}")

    # -- static ------------------------------------------------------------
    def _static(self, entry):
        fname, ctype = entry
        path = os.path.join(UI_DIR, fname)
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            return self._error(500, f"UI file missing: {fname}")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _site_static(self, path):
        """SITE local mode: serve site/public assets under /site/.

        Resolution is rooted at SITE_ROOT and the resolved path must stay
        inside it (traversal guard); only known static extensions are
        served. Same loopback+_guard policy as every other route: this runs
        before the auth guard, matching the legacy UI's static handling.
        """
        if not self._host_ok():
            self._error(403, "forbidden host (loopback only)")
            return
        rel = os.path.normpath(path[len("/site/"):]).lstrip("\\/")
        if rel in (".", ""):
            rel = "index.html"
        if rel.startswith("..") or os.path.isabs(rel):
            return self._error(404, "no such site file")
        root = os.path.abspath(SITE_ROOT)
        full = os.path.abspath(os.path.join(root, rel))
        if not full.startswith(root + os.sep):
            return self._error(404, "no such site file")
        # Directory index: /site/ and /site/<page>[/] resolve to that page's
        # index.html so the served Proof Bench has working pretty URLs; the
        # explicit index.html links the pages use keep working unchanged.
        if os.path.isdir(full):
            full = os.path.join(full, "index.html")
        ext = os.path.splitext(full)[1].lower()
        ctype = SITE_TYPES.get(ext)
        if ctype is None:
            return self._error(404, "no such site file")
        try:
            with open(full, "rb") as f:
                body = f.read()
        except OSError:
            return self._error(404, "no such site file")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- api ----------------------------------------------------------------
    def _api_status(self):
        with self.ui.lock:
            runs = [self.ui.run_public(r) for r in self.ui.runs.values()]
        runs.sort(key=lambda r: r["created_at"], reverse=True)
        self._send_json({
            "status": "ok", "started_at": self.ui.started_at,
            "auth_required": bool(self.ui.auth_token),
            "events_buffered": len(self.ui.events), "runs": runs[:50],
        })

    def _api_trust(self, q):
        """Read-only trust snapshot (CLI `trust` parity): no key, no network,
        no ledger writes. One owner for the policy: trust.trust_status."""
        from . import trust as trust_policy
        caller = (q.get("caller") or [None])[0]
        try:
            ledger = ledger_for(load_settings())
            report = ledger.participation_report()
            return self._send_json(
                trust_policy.trust_status(report, caller=caller))
        except Exception as e:
            return self._send_json({"error": str(e), "host": {"score": None,
                                   "reasons": [str(e)]}}, code=503)

    def _api_runs(self):
        with self.ui.lock:
            runs = [self.ui.run_public(r) for r in self.ui.runs.values()]
        runs.sort(key=lambda r: r["created_at"], reverse=True)
        self._send_json({"runs": runs})

    def _api_events(self, q):
        """The global event stream (after = last seen seq). The dashboard
        poller consumes this; per-run streams filter the same ring by
        task_id at /api/runs/{id}/events."""
        try:
            after = int((q.get("after") or ["0"])[0] or 0)
        except ValueError:
            raise HarnessError("'after' must be an integer") from None
        evs = self.ui.events_after(after)
        last = evs[-1]["seq"] if evs else after
        return self._send_json({"events": evs, "last_seq": last})

    def _api_run_sub(self, run_id, sub, q):
        with self.ui.lock:
            record = self.ui.runs.get(run_id)
        if record is None:
            return self._error(404, f"no such run: {run_id}")
        if sub == "result":
            return self._send_json(self.ui.run_public(record, with_result=True))
        if sub == "events":
            try:
                after = int((q.get("after") or ["0"])[0] or 0)
            except ValueError:
                raise HarnessError("'after' must be an integer") from None
            evs = self.ui.events_after(after, task_id=record["task_id"])
            last = evs[-1]["seq"] if evs else after
            return self._send_json({"events": evs, "last_seq": last})
        return self._error(404, "unknown run subresource")

    def _api_cancel(self, run_id):
        with self.ui.lock:
            record = self.ui.runs.get(run_id)
        if record is None:
            return self._error(404, f"no such run: {run_id}")
        record["_cancel"].set()
        _events.emit("run_cancel_requested", task_id=record["task_id"],
                     ui_run=run_id)
        return self._send_json({"ok": True, "id": run_id,
                                "cancel_requested": True})

    def _api_dispatch(self, body):
        kind = body.get("kind")
        args = validate_dispatch(kind, body.get("args") or {})
        record = self.ui.create_run(kind, args)
        return self._send_json(self.ui.run_public(record), code=201)

    def _api_session_delete(self, body):
        sid = body.get("session_id", "")
        try:
            deleted = _delete_chat_session(sid)
        except HarnessError as e:
            return self._error(400, str(e))
        return self._send_json({"ok": deleted, "session_id": sid})

    def _api_settings_update(self, body):
        """Persist runtime-updatable settings from the UI (POST /api/settings).

        Delegates entirely to ``update_config`` -- the ONE config owner -- so
        validation and precedence live in exactly one place. The response is
        the post-write read-only view, so the UI re-renders from what the
        server actually persisted rather than what the client hoped for.
        """
        if not isinstance(body, dict):
            return self._error(400, "request body must be a JSON object")
        if not body:
            return self._error(400, "no settings to update")
        try:
            update_config(body)
        except HarnessError as e:
            return self._error(400, str(e))
        return self._send_json({"settings": _settings_view()})

    def _api_spend(self):
        def build():
            settings = load_settings()
            _, gov = governor_for(settings)
            d = gov.key_status()
            d["session"] = {"spent": gov.spent, "ceiling": gov.max_cost,
                            "remaining": max(0.0, gov.max_cost - gov.spent)}
            ledger = ledger_for(settings)
            c_report = ledger.cost_report()
            if "jev" in c_report:
                d["jev"] = c_report["jev"]
            return d
        return self._send_json(self.ui.cached("spend", build))

    def _api_ledger_tail(self, q):
        n = int((q.get("n") or ["20"])[0] or 20)
        if not 1 <= n <= 500:
            raise HarnessError("n must be 1..500")
        ledger = ledger_for(load_settings())
        return self._send_json({"entries": ledger.tail(n),
                                "count": len(ledger.entries()),
                                "chain": ledger.chain_status()})

    def _api_ledger_verify(self):
        def build():
            ledger = ledger_for(load_settings())
            ok, bad = ledger.verify()
            return {"verified": ok, "first_bad_seq": bad,
                    "chain": ledger.chain_status()}
        return self._send_json(self.ui.cached("ledger_verify", build))

    def _api_ledger_report(self):
        from . import trust as trust_policy
        ledger = ledger_for(load_settings())
        report = ledger.participation_report()
        report["trust"] = trust_policy.trust_status(report)
        return self._send_json(report)

    def _api_site_snapshot(self):
        """SITE local mode: aggregate snapshot over THIS operator's ledger
        (export logic reused, not re-derived; no consent required to READ
        your own aggregated view — consent gates only public release)."""
        settings = load_settings()
        ledger = ledger_for(settings)
        ok, _bad = ledger.verify()
        if not ok:
            raise HarnessError(
                "ledger hash chain failed verification; refusing snapshot")
        from .site_export import _allow, build_runs
        from .site_aggregate import compute_metrics
        runs = build_runs(_allow(ledger.entries()))
        metrics = compute_metrics(runs, None)
        return self._send_json({
            "schema": "site-snapshot-v1",
            "contributors": 1,
            "sessions": [{"bundle_id": "local", "runs": len(runs),
                          "metrics": metrics}],
        })

    def _api_site_route(self, body):
        """SITE local mode: route a query through the ONE policy owner.
        Mirrors `harness route` exactly; the demo site proxies this."""
        from .route_pack import validate_route_pack
        goal = str(body.get("goal") or "").strip()
        if not goal:
            raise HarnessError("route requires a non-empty 'goal'")
        raw_pack = body.get("pack")
        if raw_pack is None:
            raise HarnessError("route requires 'pack' (declared rung ladder)")
        try:
            pack = validate_route_pack(raw_pack)
        except ValueError as exc:
            raise HarnessError(f"route pack invalid: {exc}") from exc
        settings = load_settings()
        _, governor = governor_for(settings)
        from .jev_policy import policy_for
        policy = policy_for(settings, ledger=ledger_for(settings),
                            governor=governor)
        _result, _structural, combo = policy.evaluate_model_route(
            {"goal": goal}, pack, site="model_route")
        return self._send_json({
            "status": "ok" if combo.get("rung_id") else "unroutable",
            "route": combo,
            "is_fallback": bool(combo.get("is_fallback")),
        })

    def _api_ledger_defer_stats(self, q):
        window = int((q.get("window") or ["500"])[0] or 500)
        if not 1 <= window <= 5000:
            raise HarnessError("window must be 1..5000")
        def build():
            ledger = ledger_for(load_settings())
            return {"defer_stats": ledger.defer_stats(window=window)}
        return self._send_json(self.ui.cached("ledger_defer_stats", build))

    def _api_capabilities(self):
        from .capability import capabilities_payload
        settings = load_settings()
        _, gov = governor_for(settings)
        ledger = ledger_for(settings)
        payload = capabilities_payload(
            gov, ledger, panel_pool=settings.panel_pool,
            apply_pool=settings.apply_pool, judge=settings.judge,
            refresh=False, bench=False)
        return self._send_json(payload)

    def _api_models(self, q):
        from .spend import discover_free_models
        limit = int((q.get("limit") or ["40"])[0] or 40)
        if not 1 <= limit <= 500:
            raise HarnessError("limit must be 1..500")

        def build():
            settings = load_settings()
            api_key, gov = governor_for(settings)
            return {"free_only": True,
                    "models": discover_free_models(
                        gov.transport, api_key,
                        prefer=settings.panel_pool, limit=limit)}
        payload = self.ui.cached("models", build)
        return self._send_json({"count": len(payload["models"]), **payload})

    def _api_rankings(self):
        """Read-only mirror of the rankings report (the data the weekly
        workflow files as its artifact): latest report verbatim plus the
        list of available ones. Strictly read-only -- no generation, no
        probe, no config mutation; `harness rankings` stays the one
        producer. A missing or unreadable report is a 200 with
        ``available: false`` (the empty/stale state is normal, not an
        error) and never falls back to an older file silently."""
        reports = _rankings_reports()
        if not reports:
            return self._send_json({
                "reports": [], "latest": None, "available": False,
                "note": "no rankings report found; generate one with: "
                        "harness rankings (or the weekly workflow artifact)",
            })
        latest = reports[0]
        try:
            with open(latest, encoding="utf-8") as f:
                report = json.load(f)
        except (OSError, ValueError) as e:
            return self._send_json({
                "reports": [os.path.basename(p) for p in reports],
                "latest": os.path.basename(latest), "available": False,
                "error": f"latest rankings report is unreadable: {e}",
            })
        return self._send_json({
            "reports": [os.path.basename(p) for p in reports],
            "latest": os.path.basename(latest),
            "available": True, "report": report,
        })

    def _api_jev_phase(self, q):
        """GET /api/jev-phase[?phase=ID]: the JEV completion bar, always
        local-only (harness.jev_completion, same engine `harness jev-phase
        --local-only` and the MCP `jev_phase` tool use). No ``phase`` scores
        the whole board (JEV-BAR ``--all``). Never calls a live Jev judge."""
        phase = (q.get("phase") or [None])[0]
        min_score = _finite_float((q.get("min_score") or ["85.0"])[0],
                                  "min_score", 0.0, 100.0)
        repo_root = (q.get("repo_root") or ["."])[0]
        return self._send_json(_api_jev_phase_payload(repo_root, phase, min_score))

    def _api_cost(self, q):
        """GET /api/cost: the ONE cost-observability owner
        (AutonomyLedger.cost_report -- the same call `harness cost` makes),
        with the same window/breakdown flags."""
        def _flag(name):
            return str((q.get(name) or ["0"])[0]).lower() in ("1", "true", "yes")
        ledger = ledger_for(load_settings())
        report = ledger.cost_report(
            window=(q.get("last") or [None])[0],
            by_tier=_flag("by_tier"), by_model=_flag("by_model"),
            savings=_flag("savings"))
        return self._send_json(report)

    def _api_missions_list(self, q):
        """GET /api/missions: compact summaries with bounded pagination."""
        root = (q.get("root") or ["missions"])[0] or "missions"
        limit_raw = (q.get("limit") or ["25"])[0] or "25"
        offset_raw = (q.get("offset") or ["0"])[0] or "0"
        limit = _opt_int({"limit": limit_raw}, "limit", 1, 100, 25)
        offset = _opt_int({"offset": offset_raw}, "offset", 0, 1000000, 0)
        return self._send_json({
            "root": root, **_list_missions(root, limit=limit, offset=offset)
        })

    def _api_mission_detail(self, mission_id, q):
        """GET /api/missions/<id>: read the existing pack summary only.

        This route never regenerates STATUS.md or INDEX.md. The CLI and MCP
        mission_status commands retain their established refresh behavior.
        """
        from . import mission_record as mr
        root = (q.get("root") or ["missions"])[0] or "missions"
        pack = mr.load_mission_pack(root, mission_id)
        return self._send_json(mr.pack_summary(pack))

    def _get_driver_adapter(self, autostart=True):
        return _driver_adapter(autostart)

    def _api_driver_health(self):
        adapter = self._get_driver_adapter(autostart=True)
        try:
            return self._send_json(adapter.health())
        except Exception as e:
            return self._send_json({"ok": False, "status": "down", "error": str(e)})

    def _api_driver_vocabulary(self):
        adapter = self._get_driver_adapter(autostart=True)
        try:
            return self._send_json(adapter.vocabulary())
        except Exception as e:
            return self._error(503, str(e))

    def _api_driver_schemas(self):
        adapter = self._get_driver_adapter(autostart=True)
        try:
            return self._send_json(adapter.schemas())
        except Exception as e:
            return self._error(503, str(e))

    def _api_driver_verify(self):
        adapter = self._get_driver_adapter(autostart=True)
        try:
            return self._send_json(adapter.verify())
        except Exception as e:
            return self._error(503, str(e))

    def _api_driver_start(self, body):
        adapter = self._get_driver_adapter(autostart=True)
        try:
            health = adapter.health()
            return self._send_json({"status": "running", "health": health})
        except Exception as e:
            return self._error(500, f"failed to start driver: {e}")

    def _api_driver_step(self, body):
        from .perception_client import PerceptionUnavailable
        target = body.get("target")
        if not target or not isinstance(target, str):
            return self._error(400, "field 'target' is required and must be a string")
        schema = body.get("schema")
        consent = body.get("consent")
        prefer = tuple(body.get("prefer") or ())
        require_stable = bool(body.get("require_stable", True))
        adapter = self._get_driver_adapter(autostart=True)
        try:
            envelope = adapter.step(
                target, schema=schema, consent=consent, prefer=prefer,
                require_stable=require_stable)
            return self._send_json(envelope)
        except PerceptionUnavailable as e:
            code = 400 if "schema is required" in str(e) else 503
            return self._error(code, str(e))
        except Exception as e:
            return self._error(500, str(e))

    def _api_driver_drive(self, body):
        return self._api_dispatch({"kind": "driver_task", "args": body})

    def _api_site_route(self, body):
        """SITE local mode: route a query through the ONE policy owner.
        Mirrors `harness route` exactly; the demo site proxies this."""
        from .route_pack import validate_route_pack
        goal = str(body.get("goal") or "").strip()
        if not goal:
            raise HarnessError("route requires a non-empty 'goal'")
        raw_pack = body.get("pack")
        if raw_pack is None:
            raise HarnessError("route requires 'pack' (declared rung ladder)")
        try:
            pack = validate_route_pack(raw_pack)
        except ValueError as exc:
            raise HarnessError(f"route pack invalid: {exc}") from exc
        settings = load_settings()
        _, governor = governor_for(settings)
        from .jev_policy import policy_for
        policy = policy_for(settings, ledger=ledger_for(settings),
                            governor=governor)
        _result, _structural, combo = policy.evaluate_model_route(
            {"goal": goal}, pack, site="model_route")
        return self._send_json({
            "status": "ok" if combo.get("rung_id") else "unroutable",
            "route": combo,
            "is_fallback": bool(combo.get("is_fallback")),
        })



class UiServer(ThreadingHTTPServer):
    """Loopback UI server with the Windows port-hijack hole closed.

    ``ThreadingHTTPServer`` sets ``allow_reuse_address`` (SO_REUSEADDR), which
    on Windows lets a *second* process bind a port that is already being
    served -- so another process could take over the UI, and with it the auth
    token that authorizes /api writes. POSIX needs the flag to rebind after
    TIME_WAIT, so it stays there; Windows does not (osal.HARDEN_REUSE owns
    the platform answer).
    """

    allow_reuse_address = not osal.HARDEN_REUSE

    def server_close(self):
        super().server_close()
        if hasattr(self, "ui") and self.ui:
            self.ui.uninstall_event_sink()


def make_server(host="127.0.0.1", port=8765, auth_token=None, ephemeral_auth=False):
    """Build the UI server with its UiState attached."""
    ui = UiState(auth_token=auth_token, ephemeral_auth=ephemeral_auth)
    httpd = UiServer((host, port), UiRequestHandler)
    httpd.daemon_threads = True
    httpd.ui = ui
    ui.install_event_sink()
    return httpd


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="harness serve",
        description="Local web UI + JSON API over the harness core (loopback).")
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address (default and recommended: 127.0.0.1)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--auth-token", default=os.environ.get("HARNESS_UI_AUTH_TOKEN"),
                    help="require X-Harness-Auth: <token> on /api routes "
                         "(default: HARNESS_UI_AUTH_TOKEN or none)")
    ap.add_argument("--open", action="store_true",
                    help="open the UI in the default browser")
    opts = ap.parse_args(argv)
    if opts.host not in ("127.0.0.1", "localhost", "::1"):
        print("[FATAL] the UI server binds loopback only; pass 127.0.0.1, "
              "localhost, or ::1", file=sys.stderr)
        sys.exit(1)
    httpd = make_server(opts.host, opts.port, auth_token=opts.auth_token)
    host, port = httpd.server_address[:2]
    url = f"http://{host}:{port}/"
    print(f"[OK] harness UI serving at {url} (Ctrl-C to stop)", file=sys.stderr)
    if httpd.ui.auth_token:
        print("[OK] /api routes require header X-Harness-Auth: <your token>",
              file=sys.stderr)
    if opts.open:
        osal.open_url(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[interrupted] UI server stopped", file=sys.stderr)


if __name__ == "__main__":
    main()
