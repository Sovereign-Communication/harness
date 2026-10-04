#!/usr/bin/env bash
# Hermetic test environment for local gate runs.
#
# Clearing HOME alone is not enough on Windows. os.path.expanduser("~")
# prefers USERPROFILE, so harness.config.resolve_api_key() still finds the
# operator's real ~/.config/scmorc/openrouter_fusion.env. That un-skips
# test_shipped_ids_resolve_in_live_catalog, which then attempts a real
# openrouter.ai resolve; the hermetic guard refuses it and R13 reports a
# network violation that has nothing to do with the tree under test.
#
# This is the same defect class as DF-AUDIT-4: the suite inheriting the
# operator's routing environment and failing for reasons unrelated to the code.
#
# Usage:  scripts/hermetic-env.sh <worktree-root> -- <command...>
set -euo pipefail

ROOT="$1"; shift
[ "${1:-}" = "--" ] && shift

mkdir -p "$ROOT/.hermetic/home" "$ROOT/.hermetic/tmp"

exec env -u OPENROUTER_API_KEY -u HARNESS_JEV_API_KEY -u HARNESS_JEV_DISABLE \
  HOME="$ROOT/.hermetic/home" \
  USERPROFILE="$ROOT/.hermetic/home" \
  HOMEDRIVE= HOMEPATH= \
  TMPDIR="$ROOT/.hermetic/tmp" TEMP="$ROOT/.hermetic/tmp" TMP="$ROOT/.hermetic/tmp" \
  HARNESS_LEDGER="$ROOT/.hermetic/ledger.jsonl" \
  "$@"