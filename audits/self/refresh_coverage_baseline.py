#!/usr/bin/env python3
"""Refresh audits/self/coverage_baseline.json -- the coverage ritual.

Runs the full test battery ONCE under stdlib trace (count mode,
line events only) and records, for every harness module, the line
numbers executed by the suite. D12 (sd_coverage_changed) uses this
as a fast path only when its ``commit`` is the runtime merge-base;
otherwise it regenerates a temporary document for the tree being
audited. A committed file is a reviewable hint, not a required
pre-merge artifact (see CONTRIBUTING.md, "Coverage baseline").

  python audits/self/refresh_coverage_baseline.py [--ref REF] [--out PATH]

``--ref`` is the commit the coverage document describes; default ``HEAD``.
D12 resolves the merge-base at audit time and regenerates coverage itself
when the committed document does not name that base, so this script is no
longer a manual pre-merge ritual. ``--out`` writes to a temporary path (the
CI path uses it so the tracked file is not rewritten); without it the
reviewable ``audits/self/coverage_baseline.json`` fast path is updated.

Cost: one traced battery run, roughly two to five minutes -- run this when
you want a committed fast-path document, not per commit. The trace artifacts
stay in-memory; nothing is written except the coverage JSON.
"""
import io
import json
import subprocess
import sys
import threading
import time
import trace
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main():
    root = str(ROOT)
    try:
        sys.stdout.reconfigure(line_buffering=True, write_through=True)
        sys.stderr.reconfigure(line_buffering=True, write_through=True)
    except Exception:
        pass

    class ProgressStream:
        def __init__(self, target):
            self.target = target
            self.count = 0
            self.total = None

        def write(self, s):
            if not s:
                return
            self.target.write(s)
            self.target.flush()
            dots = s.count(".") + s.count("s") + s.count("F") + s.count("E")
            if dots:
                self.count += dots
                if self.total and (self.count % 50 == 0 or self.count == self.total):
                    pct = int(self.count * 100 / self.total)
                    self.target.write(f" [{self.count}/{self.total} - {pct}%]\n")
                    self.target.flush()

        def flush(self):
            self.target.flush()

    battery_output = []

    def run_battery():
        import contextlib
        import harness.output
        orig_quiet = harness.output.QUIET
        harness.output.QUIET = True
        err_stream = io.StringIO()
        try:
            loader = unittest.defaultTestLoader
            suite = loader.discover(start_dir=root + chr(47) + "tests",
                                    top_level_dir=root)
            total = suite.countTestCases()
            stream = ProgressStream(sys.stdout)
            stream.total = total
            print(f"Discovered {total} tests. Starting execution under trace...", flush=True)
            runner = unittest.TextTestRunner(verbosity=1, stream=stream)
            with contextlib.redirect_stderr(err_stream):
                result = runner.run(suite)
            if not result.wasSuccessful():
                err_text = []
                for test, err in result.errors:
                    err_text.append(f"ERROR: {test}\n{err}")
                for test, fail in result.failures:
                    err_text.append(f"FAIL: {test}\n{fail}")
                battery_output.append("\n".join(err_text) or err_stream.getvalue())
            return result.wasSuccessful()
        finally:
            harness.output.QUIET = orig_quiet

    print("tracing full battery (2-4 min)...", flush=True)
    tests_dir = str(ROOT / "tests")
    tr = trace.Trace(count=1, trace=0,
                     ignoredirs=[sys.prefix, sys.exec_prefix, tests_dir])
    t0 = time.time()
    # trace.runfunc installs only sys.settrace; worker threads (the
    # executor's parallel dispatch) would run untraced and D12 would
    # report phantom coverage gaps for thread-executed lines.
    threading.settrace(tr.globaltrace)
    try:
        ok = tr.runfunc(run_battery)
    finally:
        threading.settrace(None)
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
    if "--out" in sys.argv[1:]:
        idx = sys.argv.index("--out")
        if idx + 1 < len(sys.argv):
            out = Path(sys.argv[idx + 1])
            if not out.is_absolute():
                out = ROOT / out
    old = out.read_bytes() if out.exists() else None
    ref = "HEAD"
    if "--ref" in sys.argv[1:]:
        idx = sys.argv.index("--ref")
        if idx + 1 < len(sys.argv):
            ref = sys.argv[idx + 1]
    head = subprocess.run(["git", "rev-parse", ref], cwd=str(ROOT),
                          capture_output=True, text=True)
    if head.returncode != 0 or not head.stdout.strip():
        print(f"[FATAL] cannot resolve --ref {ref}: {head.stderr.strip()}")
        return 2
    doc = {"_comment": "Lines executed by the full battery under stdlib "
                       "trace; regenerate only via "
                       "refresh_coverage_baseline.py and commit as a "
                       "reviewable diff.",
           "commit": head.stdout.strip(),
           "modules": baseline}
    # write_text(newline=...) needs 3.10+; the support floor is 3.9.
    data = json.dumps(doc, sort_keys=True, indent=2) + chr(10)
    with open(out, "w", encoding="utf-8", newline=chr(10)) as f:
        f.write(data)
    nmods = len(baseline)
    nlines = sum(len(v) for v in baseline.values())
    label = str(out.relative_to(ROOT)) if out.is_relative_to(ROOT) else str(out)
    if old is None:
        print("coverage written:", label, nmods, "modules,",
              nlines, "executed lines,", round(dt, 1), "s traced")
    elif old == out.read_bytes():
        print("coverage unchanged:", label, nmods, "modules,", nlines, "lines")
    else:
        print("coverage refreshed:", label, nmods, "modules,", nlines,
              "lines -- commit a fast-path document only if you intend it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
