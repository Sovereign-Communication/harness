#!/usr/bin/env python3
"""Run one prompt in a fresh, history-free headless Claude session.

Stdlib only. The prompt travels on stdin (no shell quoting), the session is
not persisted, spend is capped with --max-budget-usd, and the default
permission mode is read-only (``dontAsk``: anything the project allowlist in
.claude/settings.json does not pre-approve is refused, never prompted).

Prints ONE JSON object on stdout:
  {ok, result, model, cost_usd, num_turns, duration_ms, session_id,
   permission_mode, is_error, error}
Exit code 0 when the session succeeded, 1 otherwise.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

MODELS = ("haiku", "sonnet", "opus")
DEFAULT_MODEL = "sonnet"
DEFAULT_BUDGET_USD = 1.0
DEFAULT_TIMEOUT_S = 900


def build_command(opts, claude_bin: str) -> list:
    mode = "acceptEdits" if opts.write else "dontAsk"
    cmd = [
        claude_bin, "-p",
        "--model", opts.model,
        "--output-format", "json",
        "--no-session-persistence",
        "--max-budget-usd", str(opts.budget),
        "--permission-mode", mode,
    ]
    for rule in opts.allow or []:
        cmd += ["--allowedTools", rule]
    if opts.json_schema:
        cmd += ["--json-schema", opts.json_schema]
    return cmd


def read_prompt(opts) -> str:
    if opts.prompt_file:
        with open(opts.prompt_file, encoding="utf-8") as fh:
            text = fh.read()
    else:
        text = " ".join(opts.prompt or [])
    text = text.strip()
    if not text:
        raise SystemExit("isolated-request: empty prompt")
    return text


def parse_result(stdout: str) -> dict:
    """The -p json envelope is the last JSON object on stdout."""
    for line in reversed([ln for ln in stdout.splitlines() if ln.strip()]):
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    try:
        data = json.loads(stdout)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--model", choices=MODELS, default=DEFAULT_MODEL)
    ap.add_argument("--write", action="store_true",
                    help="allow edits (acceptEdits); default is read-only dontAsk")
    ap.add_argument("--budget", type=float, default=DEFAULT_BUDGET_USD,
                    help="--max-budget-usd for the session")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S)
    ap.add_argument("--allow", action="append",
                    help="extra permission rule for the session, e.g. 'Bash(git log *)'")
    ap.add_argument("--json-schema", help="JSON Schema string for structured output")
    ap.add_argument("--prompt-file", help="read the prompt from this file")
    ap.add_argument("--cwd", default=os.getcwd())
    ap.add_argument("--dry-run", action="store_true",
                    help="print the command that would run and exit")
    ap.add_argument("prompt", nargs=argparse.REMAINDER)
    opts = ap.parse_args(argv)

    prompt = read_prompt(opts)
    claude_bin = shutil.which("claude") or "claude"
    cmd = build_command(opts, claude_bin)
    if opts.dry_run:
        print(json.dumps({"ok": True, "dry_run": True, "command": cmd,
                          "prompt_chars": len(prompt)}))
        return 0

    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True,
            encoding="utf-8", errors="replace", cwd=opts.cwd,
            timeout=opts.timeout)
    except FileNotFoundError:
        print(json.dumps({"ok": False, "error": "claude CLI not found on PATH"}))
        return 1
    except subprocess.TimeoutExpired:
        print(json.dumps({"ok": False, "error": f"timed out after {opts.timeout}s"}))
        return 1

    data = parse_result(proc.stdout)
    is_error = bool(data.get("is_error")) or proc.returncode != 0 or not data
    out = {
        "ok": not is_error,
        "result": data.get("result"),
        "structured_output": data.get("structured_output"),
        "model": opts.model,
        "cost_usd": data.get("total_cost_usd"),
        "num_turns": data.get("num_turns"),
        "duration_ms": data.get("duration_ms")
        or int((time.monotonic() - started) * 1000),
        "session_id": data.get("session_id"),
        "permission_mode": cmd[cmd.index("--permission-mode") + 1],
        "is_error": is_error,
        "error": None if not is_error else (
            data.get("result") or proc.stderr.strip()[-2000:]
            or f"claude exited {proc.returncode}"),
    }
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
