#!/usr/bin/env python3
"""Refresh audits/self/coverage_baseline.json -- the coverage ritual.

Runs the full test battery ONCE under stdlib trace (count mode,
line events only) and records, for every harness module, the line
numbers executed by the suite. D12 (sd_coverage_changed) compares
changed harness lines against this baseline: changed lines that the
traced suite never executed fail the audit. Like the corpus
manifest, this file is generated only by this script and committed
as a reviewable diff (see CONTRIBUTING.md, "Coverage baseline").

  python audits/self/refresh_coverage_baseline.py

Cost: one traced battery run, roughly two to four minutes -- run
this when landing substantive code changes, not per audit.
The trace artifacts stay in-memory; nothing is written except the
baseline JSON.
"""
import io
import json
import subprocess
import sys
import time
import trace
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent


def main():
    root = str(ROOT)

    def run_battery():
        loader = unittest.defaultTestLoader
        suite = loader.discover(start_dir=root + chr(47) + "tests",
                                top_level_dir=root)
        stream = io.StringIO()
        runner = unittest.TextTestRunner(verbosity=0, stream=stream)
        result = runner.run(suite)
        return result.wasSuccessful()

    print("tracing full battery (2-4 min)...")
    tr = trace.Trace(count=1, trace=0,
                     ignoredirs=[sys.prefix, sys.exec_prefix])
    t0 = time.time()
    ok = tr.runfunc(run_battery)
    dt = time.time() - t0
    if not ok:
        print("ABORTED: battery failed under trace; no baseline written")
        return 1
    pkg = ROOT / "harness"
    executed = {}
    for (f, ln) in tr.results().counts:
        fp = Path(str(f))
        try:
            rel = fp.relative_to(ROOT).as_posix()
        except ValueError:
            continue
        if rel == "harness" or rel.startswith("harness" + chr(47)):
            executed.setdefault(rel, set()).add(ln)
    baseline = {}
    for mod in sorted(pkg.rglob("*.py")):
        rel = mod.relative_to(ROOT).as_posix()
        baseline[rel] = sorted(executed.get(rel, []))
    out = HERE / "coverage_baseline.json"
    old = out.read_bytes() if out.exists() else None
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(ROOT),
                          capture_output=True, text=True)
    doc = {"_comment": "Lines executed by the full battery under stdlib "
                       "trace; regenerate only via "
                       "refresh_coverage_baseline.py and commit as a "
                       "reviewable diff.",
           "commit": head.stdout.strip(),
           "modules": baseline}
    # write_text(newline=...) needs 3.10+; the support floor is 3.9.
    data = json.dumps(doc, sort_keys=True) + chr(10)
    with open(out, "w", encoding="utf-8", newline=chr(10)) as f:
        f.write(data)
    nmods = len(baseline)
    nlines = sum(len(v) for v in baseline.values())
    if old is None:
        print("created coverage_baseline.json:", nmods, "modules,",
              nlines, "executed lines,", round(dt, 1), "s traced")
    elif old == out.read_bytes():
        print("baseline unchanged:", nmods, "modules,", nlines, "lines")
    else:
        print("baseline refreshed:", nmods, "modules,", nlines,
              "lines -- commit the code change and baseline as one diff")
    return 0


if __name__ == "__main__":
    sys.exit(main())
