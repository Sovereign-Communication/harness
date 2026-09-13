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
import json
import os
import re
import sys
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import events as _events
from .config import HARD_TASK_MAX_COST, HARD_MAX_COST, load_settings
from .errors import HarnessError, ToolCancelled
from .session import (apply_session, governor_for, ledger_for, run_meta)

UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
MAX_EVENT_BUFFER = 4000
RUN_ID_RE = re.compile(r"^/api/runs/([A-Za-z0-9_-]{1,64})$")
RUN_SUB_RE = re.compile(r"^/api/runs/([A-Za-z0-9_-]{1,64})/(result|events|cancel)$")

STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
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
    if kind not in ("apply", "verify", "continue", "bench"):
        raise HarnessError(f"unknown dispatch kind '{kind}'")
    args = dict(args or {})
    if kind == "apply":
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
        [args["file"]], task_id=task_id, instruction=args["instruction"],
        edit_snippet=args.get("edit_snippet"), verify_cmd=args.get("verify"),
        max_rounds=args.get("max_rounds") or 3,
        require_consent=args.get("require_consent"),
        model=args.get("model"),
        task_max_cost=args.get("task_max_cost"),
        backend=args.get("backend") or "harness",
        verify_only=bool(args.get("verify_only")),
        cancel_check=cancel_check)
    if isinstance(result, dict):
        result["meta"] = run_meta(settings, engine.governor)
    return result


def run_verify_task(task_id, args, cancel_check):
    """The verify lane: same assembly as the CLI's _run_claims_verify.
    Covers both the prompt lane and the structured-claims lane (claims
    manifest + source window + optional definitions); lint runs pre-network
    and an ungrounded claim set finishes the run as ``rejected``.
    """
    from .panel import panel_judge
    from ._http import HttpTransport
    from .saturation import pre_run_warning
    from .claims import (build_claims_prompt, load_claims_manifest,
                         load_definitions_file)
    settings = load_settings()
    if args.get("prompt_file"):
        with open(args["prompt_file"], encoding="utf-8-sig") as f:
            prompt = f.read()
    elif args.get("claims_file"):
        with open(args["source_file"], encoding="utf-8-sig") as f:
            quoted = f.read()
        manifest_ctx, claims = load_claims_manifest(args["claims_file"])
        defs = (load_definitions_file(args["definitions_file"])
                if args.get("definitions_file") else {})
        context = (args.get("claim_context") if args.get("claim_context")
                   else manifest_ctx)
        prompt, claims_lint = build_claims_prompt(
            claims, quoted, source_index=defs, context=context)
        if not claims_lint["ok"]:
            return {"status": "rejected", "lint": claims_lint, "verdict": None,
                    "actual_cost": 0.0}
    else:
        prompt = args["prompt"]
    api_key, gov = governor_for(settings, args.get("max_cost"))
    ledger = ledger_for(settings)
    pre_run_warning(governor=gov, ledger=ledger, use_free=settings.use_free)
    try:
        result = panel_judge(
            transport=HttpTransport(), api_key=api_key, governor=gov, prompt=prompt,
            panel=list(settings.panel_pool), judge=args.get("judge") or settings.judge,
            max_tokens=None,
            reasoning_effort=args.get("reasoning_effort") or settings.reasoning_effort,
            reasoning_token_budget=settings.reasoning_token_budget,
            task_id=task_id, ledger=ledger,
            max_panelists=settings.max_panelists,
            free_tier=settings.use_free,
            cancel_check=cancel_check)
    except ToolCancelled:
        # Spend honesty: in-flight calls that billed before the cooperative
        # cancel landed are real spend and must reach the envelope.
        return {"status": "cancelled", "verdict": None,
                "judge_synthesis_status": "cancelled",
                "panel_results": [], "panel_failures": [],
                "actual_cost": gov.spent, "max_cost_ceiling": gov.max_cost,
                "cost_by_model": gov.cost_by_model(),
                "meta": run_meta(settings, gov)}
    result["cost_by_model"] = gov.cost_by_model()
    result["meta"] = run_meta(settings, gov)
    return result


def run_continue_task(task_id, args, cancel_check):
    from .apply import validate_continuation
    settings = load_settings()
    with open(args["state"], encoding="utf-8") as f:
        continuation = validate_continuation(json.load(f))
    engine = apply_session(settings)
    return engine.apply_batch(
        [None], task_id=task_id,
        instruction=args.get("instruction"),
        verify_cmd=args.get("verify"),
        max_rounds=args.get("max_rounds") or 3,
        cancel_check=cancel_check, continuation=continuation)


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


RUNNERS = {
    "apply": run_apply_task,
    "verify": run_verify_task,
    "continue": run_continue_task,
    "bench": run_bench_task,
}


class UiState:
    """One server's shared state: run registry, event ring, auth."""

    CACHE_TTL = 15.0  # seconds: dashboard polls beat on /api/spend et al.

    def __init__(self, auth_token=None):
        self.lock = threading.Lock()
        self.runs = {}
        self.events = deque(maxlen=MAX_EVENT_BUFFER)
        self.last_seq = 0
        self.auth_token = auth_token or None
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
    from .config import load_settings
    d = load_settings().to_dict()
    if d.get("mcp_auth_token"):
        d["mcp_auth_token"] = None
        d["mcp_auth_token_present"] = True
    if d.get("expect_key_label"):
        # presence only; the label is never echoed (audit #9b rule)
        d["expect_key_label"] = None
        d["expect_key_label_present"] = True
    return d


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
        return got == self.ui.auth_token

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
            if parsed.path == "/api/runs":
                return self._api_dispatch(body)
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

    def _api_spend(self):
        def build():
            _, gov = governor_for(load_settings())
            d = gov.key_status()
            d["session"] = {"spent": gov.spent, "ceiling": gov.max_cost,
                            "remaining": max(0.0, gov.max_cost - gov.spent)}
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


def make_server(host="127.0.0.1", port=8765, auth_token=None):
    """Build the ThreadingHTTPServer with its UiState attached."""
    ui = UiState(auth_token=auth_token)
    httpd = ThreadingHTTPServer((host, port), UiRequestHandler)
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
        import webbrowser
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[interrupted] UI server stopped", file=sys.stderr)


if __name__ == "__main__":
    main()
