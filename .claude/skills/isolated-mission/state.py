#!/usr/bin/env python3
"""Mission state for /isolated-mission, stored as a Harness HUL mission pack.

One owner: every write goes through ``harness.mission_record`` (HUL-A pack,
HUL-B dual budget). This helper only adapts the skill's phases (scout / plan /
execute / evaluate) onto that pack; it never writes pack files itself.

Subcommands (all print one JSON object):
  init     --id --request --success --max-cost [--reserve] [--in-scope] [--out-of-scope]
  receipt  --id --phase --round --model [--cost] [--tokens] --status --summary [--artifact]
  artifact --id --name --file
  bar      --id --file            # append a `jev-phase` result to jev_evals.jsonl
  show     --id
  terminal --id --outcome complete|failed|blocked|stalled --findings-file
Default pack root: tmp/claude/missions (tmp/ is gitignored); override with --root.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import shutil
import sys
from pathlib import Path

PHASES = ("scout", "plan", "execute", "evaluate", "verify", "loop")
DEFAULT_ROOT = "tmp/claude/missions"
# .claude/skills/isolated-mission/state.py -> repo root. Import this checkout's
# harness (a worktree), not whatever the venv's editable install points at.
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _mr():
    try:
        from harness import mission_record
    except ImportError as exc:  # pragma: no cover - environment guard
        raise SystemExit(
            "isolated-mission: harness is not importable; run with the repo "
            f"venv python from the Harness root ({exc})")
    return mission_record


def _pack(opts):
    mr = _mr()
    pack = mr.MissionPack(Path(opts.root), opts.id)
    if not pack.exists():
        raise SystemExit(f"isolated-mission: no mission pack at {pack.dir}")
    return mr, pack


def _emit(obj) -> int:
    print(json.dumps(obj, ensure_ascii=False, default=str))
    return 0


def cmd_init(opts) -> int:
    from harness.cli import main as harness_main
    argv = ["mission", "init", "--id", opts.id, "--request", opts.request,
            "--success", opts.success, "--max-cost", str(opts.max_cost),
            "--terminal-reserve", str(opts.reserve), "--root", opts.root,
            "--verifier-kind", "claude-isolated-mission", "--quiet"]
    if opts.in_scope:
        argv += ["--in-scope", opts.in_scope]
    if opts.out_of_scope:
        argv += ["--out-of-scope", opts.out_of_scope]
    # `mission init` prints its own report; keep this helper's stdout to ONE object.
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        rc = harness_main(argv)
    if rc not in (0, None):
        return _emit({"ok": False, "error": captured.getvalue()[-2000:] or f"exit {rc}"}) or int(rc)
    mr, pack = _pack(opts)
    mr.ensure_findings_placeholder(pack)
    mr.write_resume(pack, mr.build_resume_state(pack, round=0, phase="init"))
    mr.write_status(pack)
    return _emit({"ok": True, "mission_id": opts.id, "pack_dir": str(pack.dir)})


def cmd_receipt(opts) -> int:
    mr, pack = _pack(opts)
    receipt = {
        "kind": "attempt" if opts.phase == "execute" else opts.phase,
        "phase": opts.phase, "round": opts.round, "model": opts.model,
        "cost_usd": opts.cost, "tokens": opts.tokens, "status": opts.status,
        "summary": opts.summary, "artifact": opts.artifact,
    }
    if opts.cost:
        # HUL-B dual budget: refuses spend that would eat the terminal reserve.
        mr.record_spend(pack, opts.cost)
    body = mr.append_receipt(pack, receipt)
    mr.write_resume(pack, mr.build_resume_state(
        pack, round=opts.round, phase=opts.phase, last_status=opts.status))
    mr.write_status(pack)
    return _emit({"ok": True, "receipt": body, "budget": mr.load_budget(pack)})


def cmd_artifact(opts) -> int:
    _mr_mod, pack = _pack(opts)
    pack.artifacts_dir.mkdir(parents=True, exist_ok=True)
    name = Path(opts.name).name
    dest = pack.artifacts_dir / name
    shutil.copyfile(opts.file, dest)
    _mr_mod.write_status(pack)
    return _emit({"ok": True, "artifact": str(dest)})


def cmd_bar(opts) -> int:
    mr, pack = _pack(opts)
    with open(opts.file, encoding="utf-8-sig") as fh:
        text = fh.read()
    start = text.find("{")
    bar = json.loads(text[start:text.rindex("}") + 1]) if start >= 0 else {}
    entry = {
        "site": "phase_completion", "phase": bar.get("phase"),
        "score": bar.get("score"), "bar_pass": bar.get("can_mark_complete"),
        "improvements": [
            {k: i.get(k) for k in ("bucket", "axis", "level", "suggested_next_action", "source")}
            for i in (bar.get("improvements") or [])],
        "is_fallback": (bar.get("semantic") or {}).get("is_fallback"),
        "cost_usd": (bar.get("semantic") or {}).get("cost", 0.0),
    }
    body = mr.append_jev_eval(pack, entry)
    mr.write_status(pack)
    return _emit({"ok": True, "jev_eval": body})


def cmd_show(opts) -> int:
    mr, pack = _pack(opts)
    receipts = mr.load_receipts(pack)
    evals = mr.load_jev_evals(pack)
    artifacts = sorted(p.name for p in pack.artifacts_dir.iterdir()) \
        if pack.artifacts_dir.is_dir() else []
    return _emit({
        "ok": True, "mission_id": opts.id, "pack_dir": str(pack.dir),
        "spec": pack.spec(), "budget": mr.load_budget(pack),
        "resume": mr.load_resume(pack) if pack.resume_path.is_file() else {},
        "terminal": mr.is_terminal(pack), "receipts": len(receipts),
        "last_receipts": receipts[-5:], "last_bar": evals[-1] if evals else None,
        "artifacts": artifacts,
    })


def cmd_terminal(opts) -> int:
    mr, pack = _pack(opts)
    with open(opts.findings_file, encoding="utf-8") as fh:
        findings = fh.read()
    resume = mr.mark_terminal(pack, outcome=opts.outcome, findings=findings)
    return _emit({"ok": True, "resume": resume})


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="isolated-mission state (HUL pack)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--id", required=True)
        p.add_argument("--root", default=DEFAULT_ROOT)
        return p

    p = common(sub.add_parser("init"))
    p.add_argument("--request", required=True)
    p.add_argument("--success", required=True)
    p.add_argument("--max-cost", type=float, required=True)
    p.add_argument("--reserve", type=float, default=0.0)
    p.add_argument("--in-scope")
    p.add_argument("--out-of-scope")
    p = common(sub.add_parser("receipt"))
    p.add_argument("--phase", choices=PHASES, required=True)
    p.add_argument("--round", type=int, required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--cost", type=float, default=0.0)
    p.add_argument("--tokens", type=int, default=0)
    p.add_argument("--status", required=True)
    p.add_argument("--summary", required=True)
    p.add_argument("--artifact")
    p = common(sub.add_parser("artifact"))
    p.add_argument("--name", required=True)
    p.add_argument("--file", required=True)
    p = common(sub.add_parser("bar"))
    p.add_argument("--file", required=True)
    common(sub.add_parser("show"))
    p = common(sub.add_parser("terminal"))
    p.add_argument("--outcome", choices=("complete", "failed", "blocked", "stalled"),
                   required=True)
    p.add_argument("--findings-file", required=True)

    opts = ap.parse_args(argv)
    handler = {"init": cmd_init, "receipt": cmd_receipt, "artifact": cmd_artifact,
               "bar": cmd_bar, "show": cmd_show, "terminal": cmd_terminal}[opts.cmd]
    try:
        return handler(opts)
    except SystemExit:
        raise
    except Exception as exc:  # surface HarnessError (e.g. reserve refusal) as data
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
