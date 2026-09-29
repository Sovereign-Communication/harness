#!/usr/bin/env bash
# Lane 3 verification battery.
#
# Usage: scripts/l3-verify.sh <worktree-root>
#
# Runs the exact gate set the mission names, in a worktree the caller has
# already checked out at the PR head, with a private HARNESS_LEDGER so a
# verification run can never append to the operator's real ledger.
#
# Every step prints its own `== <name> ==` banner and the caller gets the
# exit code of the last one, so a single invocation is a single verdict.
set -uo pipefail

ROOT="${1:?usage: l3-verify.sh <worktree-root>}"
PY="${L3_PYTHON:-C:/Users/SCM/Documents/GitHub/Harness/.venv/Scripts/python.exe}"

if [ ! -x "$PY" ]; then
  echo "FATAL: operator python not found at $PY" >&2
  exit 127
fi

# Private ledger. One file per verification run, outside the repo, so the
# hermetic suite's fake events (Job 2.1) land here and never in ~/.harness.
LEDGER_DIR="$(mktemp -d 2>/dev/null || mktemp -d -t l3ledger)"
export HARNESS_LEDGER="$LEDGER_DIR/verify.jsonl"
export HARNESS_USE_FREE=false
# The audit and the phase bar must never reach OpenRouter from here.
export OPENROUTER_API_KEY=""

cleanup() { rm -rf "$LEDGER_DIR"; }
trap cleanup EXIT

cd "$ROOT" || exit 1
echo "== worktree =="
git rev-parse --short HEAD
git status --porcelain --untracked-files=no | head -20
echo "== ledger =="
echo "$HARNESS_LEDGER"

fail=0
step() {
  local name="$1"; shift
  echo ""
  echo "== $name =="
  if "$@"; then
    echo "== $name: OK =="
  else
    echo "== $name: FAILED (exit $?) =="
    fail=1
  fi
}

step "ruff"        "$PY" -m ruff check harness tests examples/oc_handoff audits
step "ruff-audits" "$PY" -m ruff check audits
step "compileall"  "$PY" -m compileall -q harness tests examples/oc_handoff audits
step "suite-a-j"   "$PY" -W error::ResourceWarning -m unittest discover -s tests -p "test_[a-j]*.py"
step "suite-k-z"   "$PY" -W error::ResourceWarning -m unittest discover -s tests -p "test_[k-z]*.py"
step "audit"       "$PY" audits/self/audit.py

echo ""
echo "== ledger hygiene =="
if [ -f "$HARNESS_LEDGER" ]; then
  wc -l < "$HARNESS_LEDGER" | tr -d ' ' | sed 's/^/events written to private ledger: /'
else
  echo "private ledger never created (unexpected: the suite should write events)"
fi

echo ""
if [ "$fail" -eq 0 ]; then
  echo "L3-GATE: ALL GREEN"
else
  echo "L3-GATE: RED"
fi
exit "$fail"
