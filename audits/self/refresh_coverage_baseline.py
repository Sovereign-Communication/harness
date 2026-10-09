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
import os
import subprocess
import sys
import threading
import time
import trace
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent


def main():
    root = str(ROOT)

    battery_output = []

    def run_battery():
        loader = unittest.defaultTestLoader
        suite = loader.discover(start_dir=root + chr(47) + "tests",
                                top_level_dir=root)
        stream = io.StringIO()
        runner = unittest.TextTestRunner(verbosity=1, stream=stream)
        result = runner.run(suite)
        if not result.wasSuccessful():
            battery_output.append(stream.getvalue())
        return result.wasSuccessful()

    print("tracing full battery (2-4 min)...")
    tr = trace.Trace(count=1, trace=0,
                     ignoredirs=[sys.prefix, sys.exec_prefix])
    t0 = time.time()
    # trace.runfunc installs only sys.settrace; worker threads (the
    # executor's parallel dispatch) would run untraced and D12 would
    # report phantom coverage gaps for thread-executed lines.
    previous_refresh_flag = os.environ.get("HARNESS_REFRESHING_COVERAGE_BASELINE")
    os.environ["HARNESS_REFRESHING_COVERAGE_BASELINE"] = "1"
    threading.settrace(tr.globaltrace)
    try:
        ok = tr.runfunc(run_battery)
    finally:
        threading.settrace(None)
        if previous_refresh_flag is None:
            os.environ.pop("HARNESS_REFRESHING_COVERAGE_BASELINE", None)
        else:
            os.environ["HARNESS_REFRESHING_COVERAGE_BASELINE"] = previous_refresh_flag
    dt = time.time() - t0
    if not ok:
        print("ABORTED: battery failed under trace; no baseline written")
        if battery_output:
            print(battery_output[0])
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
    # Keep the generated artifact diffable by module; the baseline is a
    # reviewable contract, not a minified blob.
    data = json.dumps(doc, sort_keys=True, indent=1) + chr(10)
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

    # The baseline-pin tests are necessarily skipped while their own input is
    # being regenerated. Run them again against the completed artifact before
    # reporting success, so bootstrap cannot leave a stale pin or a red D12.
    suite = unittest.defaultTestLoader.discover(
        start_dir=root + chr(47) + "tests",
        pattern="test_audit_d12_coverage.py",
        top_level_dir=root)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    if not result.wasSuccessful():
        print("ABORTED: regenerated baseline failed its D12 pin checks")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
