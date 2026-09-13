"""Human-facing rich rendering of result envelopes: the ONE pretty-printer.

Machine JSON on stdout is the contract scripts build on; when stdout is a
TTY (and ``--out``/``--json`` did not force the machine shape) the CLI may
render the same envelope as human tables on stderr. Rules:

- Rendering is advisory and read-only: it must never mutate a result's data.
- Auto-detection: TTY-pretty, piped-JSON. ``HARNESS_FORCE_PRETTY=1`` forces
  it on; ``NO_COLOR=1`` or ``--no-color`` strips ANSI.
- Total over every envelope: an unrecognized shape falls back to compact
  JSON on stderr, so the printer never hides a result.
"""
import json
import os
import sys

from .output import eprint

# --- ANSI (stripped when NO_COLOR/--no-color, never on piped stdout) ------
_RESET = "\x1b[0m"
_BOLD = "\x1b[1m"
_DIM = "\x1b[2m"
_GREEN = "\x1b[32m"
_YELLOW = "\x1b[33m"
_RED = "\x1b[31m"
_CYAN = "\x1b[36m"

_COLOR = True


def set_color_enabled(on):
    global _COLOR
    _COLOR = bool(on)


def _c(code, text):
    return f"{code}{text}{_RESET}" if _COLOR else str(text)


def _should_pretty(result, out, force_json=False):
    """TTY-pretty / piped-JSON policy. A --out file is always the machine
    shape; ``--json`` forces the machine shape even on a TTY."""
    if out is not None or force_json:
        return False
    if os.environ.get("HARNESS_FORCE_PRETTY") == "1":
        return True
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


def _fmt_cost(v):
    return f"${v:.6f}" if isinstance(v, (int, float)) else str(v)


def _status_color(status):
    if status in ("ok", "preview", "accept", "pass"):
        return _GREEN
    if status in ("deferred", "defer", "preview_exhausted", "panel_shortfall"):
        return _YELLOW
    if status in ("verify_failed", "error", "decline", "reject"):
        return _RED
    return _CYAN


def _emit(text):
    """All rendering lands on stderr; stdout keeps the machine JSON."""
    eprint(text)


def _summarize(result):
    """One compact shape-driven summary line."""
    status = result.get("status")
    if status:
        return (f"status: {_c(_status_color(status), str(status))}  "
                f"cost: {_fmt_cost(result.get('cost'))}")
    if "verified" in result:
        return f"ledger chain: {'OK' if result.get('verified') else _c(_RED, 'BROKEN')}"
    if "models" in result:
        return f"{result.get('count', len(result['models']))} models"
    return ""


def _render_apply(result):
    _emit(_c(_BOLD, f"== apply: {result.get('file', result.get('task_id', '?'))} =="))
    for r in result.get("rounds", []):
        status = r.get("status", "?")
        badge = _c(_status_color(status), status)
        cost = _fmt_cost(r.get("cost"))
        head = f"  r{r.get('round', '?')}  {str(r.get('model', '?')):<40} {badge}  {cost}"
        extra = []
        if r.get("verify_passed") is not None:
            extra.append("gate PASS" if r["verify_passed"] else "gate FAIL")
        if r.get("changed"):
            extra.append("changed")
        if extra:
            head += "  " + "  ".join(extra)
        _emit(head)
        verify_output = (r.get("verify_output") or "").strip()
        if verify_output:
            for line in verify_output.splitlines()[-3:]:
                _emit(f"      {_c(_DIM, line[:96])}")
    verify = result.get("verify")
    if isinstance(verify, dict):
        passed = verify.get("passed")
        label = (_c(_GREEN, "PASS") if passed
                 else _c(_RED, "FAIL") if passed is False else _c(_DIM, "n/a"))
        _emit(f"  gate: {label}  {_c(_DIM, verify.get('command') or '')}")
    if result.get("continuation"):
        _emit(_c(_YELLOW, "  deferred: partial work preserved; resume with: "
                  "harness continue --state <out.json>"))
    if result.get("status") == "preview":
        _emit(_c(_DIM, "  preview: no file written, gate not run (verify-only)"))


def _render_verify(result):
    _emit(_c(_BOLD, "== verify =="))
    verdict = result.get("verdict")
    if verdict:
        _emit(f"  verdict: {str(verdict)[:200]}")
    consensus = result.get("consensus", {})
    if isinstance(consensus, dict):
        parts = []
        for key in ("agreement", "confidence", "gate_converged",
                    "panel_shortfall", "defer"):
            if key in consensus:
                v = consensus[key]
                if isinstance(v, float):
                    v = f"{v:.2f}"
                parts.append(f"{key}={v}")
        if parts:
            _emit("  " + "  ".join(parts))
    tally = (result.get("convergence") or {}).get("tally")
    if isinstance(tally, dict):
        for cid, entry in (tally.get("claims") or {}).items():
            real_n = entry.get("real_votes")
            nr_n = entry.get("not_real_votes")
            vote_note = (f"{real_n}R/{nr_n}NR" if real_n is not None and nr_n is not None
                         else f"{entry.get('voted_by', '?')}/{entry.get('of_panel', '?')}")
            verd = entry.get("verdict", "?")
            color = _RED if verd == "real" else _GREEN if verd == "not_real" else _CYAN
            _emit(f"    {cid}: {_c(color, verd)} ({vote_note})")
        if tally.get("panel_shortfall"):
            s = tally.get("shortfall") or {}
            _emit(_c(_YELLOW, f"    SHORTFALL {s.get('voted_by', '?')}/{s.get('of_panel', '?')} "
                     f"slots voted; merge gate deferred"))
    for f in result.get("panel_failures", []) or []:
        _emit(_c(_YELLOW, f"  ! {f.get('model', '?')}: {f.get('reason', '?')}"))


def _render_ledger_report(result):
    _emit(_c(_BOLD, "== ledger participation =="))
    models = result.get("models")
    if isinstance(models, dict):
        for model, m in models.items():
            if not isinstance(m, dict):
                _emit(f"  {model}: {m}")
                continue
            parts = [f"{k}={v}" for k, v in sorted(m.items())
                     if isinstance(v, (int, float, str, bool))]
            _emit(f"  {model:<42} {'  '.join(parts)[:160]}")
    else:
        _emit(_c(_DIM, "  " + json.dumps(models)[:160] if models else "  (no history)"))
    trust = result.get("trust")
    if isinstance(trust, dict):
        score = trust.get("score")
        if isinstance(score, (int, float)):
            color = _GREEN if score > 0 else _RED if score < 0 else _CYAN
            _emit(f"  trust: {_c(color, str(score))}  "
                  f"{'; '.join(str(r) for r in (trust.get('reasons') or [])[:2])[:120]}")
        else:
            _emit(f"  trust: {json.dumps(trust, sort_keys=True)[:120]}")


def _render_ledger_tail(result):
    _emit(_c(_BOLD, f"== ledger tail ({result.get('count', '?')} entries) =="))
    for entry in result.get("entries", []):
        seq = entry.get("seq", "?")
        ts = str(entry.get("ts", ""))[:19]
        event = entry.get("event", entry.get("type", "?"))
        who = entry.get("model") or entry.get("caller") or ""
        _emit(f"  {seq:>4}  {ts:<19} {str(event):<16} {who}")


def _render_bench(result):
    _emit(_c(_BOLD, "== bench =="))
    bench = result.get("bench", {})
    for r in bench.get("results", []) or []:
        status = r.get("status", "?")
        _emit(f"  {str(r.get('name', '?')):<20} {_c(_status_color(status), status)}  "
              f"cost {_fmt_cost(r.get('cost'))}")
    st = bench.get("statuses")
    if isinstance(st, dict):
        _emit("  " + "  ".join(f"{k}={v}" for k, v in st.items()))
    if result.get("calibration"):
        _emit(_c(_DIM, "  calibration data in envelope (see ledger report)"))


def _render_capabilities(result):
    _emit(_c(_BOLD, "== capabilities =="))
    for r in result.get("models", []):
        if not isinstance(r, dict):
            _emit(f"  {r}")
            continue
        ctx = r.get("context")
        ctxs = f"{ctx:>9,}" if isinstance(ctx, int) else f"{'?':>9}"
        _emit(f"  {str(r.get('model', '?')):<42} ctx {ctxs}  "
              f"cap={r.get('capability', 0):.2f}  "
              f"rel={r.get('reliability_structured', 0):.2f}")


def _render_models(result):
    ids = result.get("models") or []
    meta = result.get("meta")
    _emit(_c(_BOLD, f"== models ({result.get('count', len(ids))}) =="))
    for m in ids:
        if isinstance(m, dict):
            ctx = m.get("context")
            ctxs = f"{ctx:>9,}" if isinstance(ctx, int) else f"{'?':>9}"
            _emit(f"  {str(m.get('id', '?')):<44} {ctxs}  "
                  f"{_c(_DIM, m.get('note') or '')}")
        else:
            _emit(f"  {m}")
    if meta:
        _emit(_c(_DIM, f"  source: {meta.get('source', '?')} "
                      f"(fetched {meta.get('fetched_at', '?')})"))


def _render(result):
    status = result.get("status")
    if isinstance(status, str) and "rounds" in result:
        _render_apply(result)
        return
    if "consensus" in result:
        _render_verify(result)
        return
    if "bench" in result:
        _render_bench(result)
        return
    if "entries" in result:
        _render_ledger_tail(result)
        return
    if "models" in result and "events" in result:
        _render_ledger_report(result)
        return
    if "models" in result and "capability" in json.dumps(
            result.get("models", [])[:1]):
        _render_capabilities(result)
        return
    if "models" in result:
        _render_models(result)
        return
    if "verified" in result:
        ok = result.get("verified")
        _emit(f"ledger chain: {'OK' if ok else _c(_RED, 'BROKEN')}")
        if result.get("first_bad_seq") is not None:
            _emit(f"  first bad seq: {result['first_bad_seq']}")
        return
    # Unknown shape: compact JSON keeps every field visible.
    _emit(json.dumps(result, indent=2)[:4000])


def pretty_print(result, out=None, force_json=False):
    """Render one result envelope for humans when appropriate. Returns True
    when it rendered (the caller suppresses the bare JSON print)."""
    if not isinstance(result, dict) or not _should_pretty(result, out, force_json):
        return False
    try:
        _render(result)
        summary = _summarize(result)
        _emit(_c(_BOLD, "-- summary: ") + (summary or "(see above)"))
        _emit(_c(_DIM, "(full envelope on stdout as JSON)"))
    except Exception as exc:  # rendering must never replace the result
        _emit(f"[render] fell back to raw JSON ({exc})")
        return False
    return True
