#!/usr/bin/env python3
"""Refresh audits/self/corpus_manifest.json -- the pinned-corpus ritual.

The self-audit's authority rests on read-only evidence files (the dogfood
corpus and the audit reports). sd_corpus_integrity verifies every pinned
file's SHA-256 (LF-normalized, so eol normalization in checkouts cannot
false-positive) against this manifest before the evidence is trusted: a
silent or casual edit fails the audit deterministically. The ONLY honest
path to a corpus change is this scripted refresh, committed as a
reviewable diff (see CONTRIBUTING.md, "Audit corpus integrity").

  python audits/self/refresh_corpus_manifest.py

Pinned set: every git-tracked file under audits/self/dogfood/ plus the
audit reports (audit_report.md, round2_report.md, round2_rubric.md).
Deliberately NOT pinned: round2_scores.json (rewritten by every audit
run; restore discipline lives in CONTRIBUTING) and _runs/ (untracked
scratch).
"""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent

REPORTS = {
    "audits/self/audit_report.md",
    "audits/self/round2_report.md",
    "audits/self/round2_rubric.md",
}


def pinned_files():
    tracked = subprocess.run(
        ["git", "ls-files", "audits/self"], cwd=str(ROOT),
        capture_output=True, text=True, check=True).stdout.split()
    return sorted(
        f for f in tracked
        if f.startswith("audits/self/dogfood/") or f in REPORTS
    )


def main():
    entries = {}
    for f in pinned_files():
        data = (ROOT / f).read_bytes()
        entries[f] = hashlib.sha256(
            data.replace(bytes([13, 10]), bytes([10]))).hexdigest()
    manifest = {
        "_comment": "SHA-256 pin (LF-normalized) of the audit evidence corpus; "
                    "regenerate only via refresh_corpus_manifest.py and commit as a reviewable diff.",
        "files": entries,
    }
    out = HERE / "corpus_manifest.json"
    old = out.read_bytes() if out.exists() else None
    out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + chr(10),
                   encoding="utf-8", newline=chr(10))
    if old is None:
        print(f"created {out.name}: {len(entries)} files pinned")
    elif old == out.read_bytes():
        print(f"manifest unchanged: {len(entries)} files pinned")
    else:
        print(f"manifest refreshed: {len(entries)} files pinned -- "
              "commit the corpus change and manifest as one diff")
    return 0


if __name__ == "__main__":
    sys.exit(main())
