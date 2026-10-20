#!/usr/bin/env python3
"""Full-battery coverage, isolated BEFORE discovery, bound to measured content.

Default output remains coverage_baseline.json. --output can capture reviewable
scratch evidence without touching the committed baseline. --diff-base pins the
comparison revision independently of measured HEAD (default: merge-base with
origin/main). Failed tests, leaks, egress or source drift never write evidence.
Use --check-isolation before authorizing a full trace. No live lane is supported.
"""
import argparse
import contextlib
import hashlib
import io
import json
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import threading
import time
import trace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from coverage_evidence import changed_line_coverage, source_hashes
from coverage_isolation import install_guard, isolated_env

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent


def git(*args):
    return subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          check=True).stdout.strip()


def guard_events(directory):
    return [json.loads(line) for path in Path(directory).glob("*.jsonl")
            for line in path.read_text(encoding="utf-8").splitlines()]


def check_isolation(directory):
    """A fresh guarded process proves env/home and grandchild boundaries."""
    code = (
        "import json,os,subprocess,sys;from harness.config import resolve_api_key,resolve_jev_key;"
        "assert resolve_api_key() is None and resolve_jev_key() is None;"
        "assert all(os.environ.get(k)=='0' for k in "
        "['HARNESS_TEST_ALLOW_NETWORK','HARNESS_JEV_LIVE_SMOKE','HARNESS_AUDIT_LIVE']);"
        "assert 'DRIVER_JEV_API_KEY' not in os.environ;"
        "assert 'OPENROUTER_API_KEY_FILE' not in os.environ;"
        "r=subprocess.run([sys.executable,'-I','-S','-c',"
        "\"import socket;socket.getaddrinfo('provider.invalid',443)\"],capture_output=True,text=True);"
        "assert r.returncode!=0 and 'provider-free coverage refused' in r.stderr;"
        "print('ISOLATION OK: credentials absent; live opt-ins off; grandchild DNS refused')"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True,
                            text=True, encoding="utf-8", timeout=30)
    print(result.stdout, end="")
    if result.returncode:
        print(result.stderr, end="", file=sys.stderr)
        return result.returncode
    import shutil
    node = shutil.which("node")
    if node:
        for code, event in (
            ("require('node:dns').lookup('provider.invalid',()=>{})", "node lookup"),
            ("require('node:child_process').spawn(process.execPath,['-e','process.exit(0)'])", "node child"),
        ):
            check = subprocess.run([node, "-e", code], capture_output=True,
                                   text=True, encoding="utf-8", timeout=30)
            if check.returncode == 0 or "provider-free coverage refused" not in check.stderr:
                raise RuntimeError("Node descendant isolation failed")
            print("ISOLATION OK: " + event + " refused")
    refused = [e["event"] for e in guard_events(directory) if e["kind"] == "refused"]
    expected = ["socket.getaddrinfo"] + (["node lookup", "node child"] if node else [])
    if sorted(refused) != sorted(expected):
        raise RuntimeError("descendant isolation evidence missing or unexpected")
    return 0


def trace_can_publish(successful, leaked, violations, network_violations, drift):
    """Never publish trace hits after any suite/isolation/identity failure."""
    return successful and not (leaked or violations or network_violations or drift)


def child(opts):
    sys.path.insert(0, str(ROOT))
    violations = install_guard(opts.sandbox, opts.events)
    if opts.check_isolation:
        return check_isolation(opts.events)
    # Resolve only the fresh private config before any test discovery.
    from harness.config import resolve_api_key, resolve_jev_key
    if resolve_api_key() is not None or resolve_jev_key() is not None:
        raise RuntimeError("credential isolation failed before discovery")
    before = source_hashes(ROOT)
    head, tree = git("rev-parse", "HEAD"), git("rev-parse", "HEAD^{tree}")
    diff_base = git("rev-parse", opts.diff_base) if opts.diff_base else git(
        "merge-base", "HEAD", "origin/main")
    workdiff = git("diff", "--no-ext-diff", "--no-textconv", "HEAD", "--")
    discovered, completed = [], []
    captured = io.StringIO()

    class RecordingResult(unittest.TextTestResult):
        def startTest(self, test):
            completed.append(test.id())
            super().startTest(test)

    def flatten(suite):
        for item in suite:
            if isinstance(item, unittest.TestSuite):
                yield from flatten(item)
            else:
                yield item.id()

    def run_battery():
        loader = unittest.TestLoader()
        suite = (loader.loadTestsFromNames(opts.tests) if opts.tests else
                 loader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT)))
        discovered.extend(flatten(suite))
        return unittest.TextTestRunner(verbosity=1, stream=captured,
                                       resultclass=RecordingResult).run(suite)

    print("tracing provider-free " + ("focused tests" if opts.tests else "full battery") + "...", flush=True)
    tracer = trace.Trace(count=1, trace=0, ignoredirs=[sys.prefix, sys.exec_prefix])
    started = time.monotonic()
    threading.settrace(tracer.globaltrace)
    try:
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            result = tracer.runfunc(run_battery)
    finally:
        threading.settrace(None)
    output = captured.getvalue()
    log = Path(str(opts.output) + ".log")
    log.write_bytes(output.encode("utf-8"))
    log_sha256 = hashlib.sha256(log.read_bytes()).hexdigest()
    print(output[-4500:], end="")
    events = guard_events(opts.events)
    refused = [e for e in events if e["kind"] == "refused"]
    import tests
    leaked = any(x in output for x in ("ResourceWarning", "unclosed file"))
    drift = source_hashes(ROOT) != before or git("rev-parse", "HEAD") != head
    if not trace_can_publish(result.wasSuccessful(), leaked, violations + refused,
                             tests.NETWORK_VIOLATIONS, drift):
        print("ABORTED: tests/leaks/egress/source identity failed; no trace/baseline written")
        print(json.dumps({"tests_run": result.testsRun, "errors": len(result.errors),
                          "failures": len(result.failures), "leaked": leaked,
                          "violations": refused, "source_drift": drift}))
        return 1
    imported = {}
    for name, module in list(sys.modules.items()):
        if name == "harness" or name.startswith(("harness.", "driver_core.", "tests.")):
            file = getattr(module, "__file__", None)
            if file:
                resolved = Path(file).resolve()
                resolved.relative_to(ROOT)  # another worktree/editable import must fail
                imported[name] = str(resolved)
    counts = {}
    for (file, line), count in tracer.results().counts.items():
        try:
            relative = Path(file).resolve().relative_to(ROOT).as_posix()
        except ValueError:
            continue
        if relative.startswith("harness/"):
            counts.setdefault(relative, {})[str(line)] = count
    modules = {p: sorted(int(n) for n in counts.get(p, {}))
               for p in before if p.startswith("harness/") and p.endswith(".py")}
    if opts.tests:
        print("FOCUSED CHECK OK; not eligible to replace full-battery trace")
        return 0
    doc = {"schema": "harness-coverage-v2", "commit": diff_base,
           "execution": {"head": head, "tree": tree,
                         "working_diff_sha256": hashlib.sha256(workdiff.encode()).hexdigest(),
                         "python": sys.version, "executable": sys.executable,
                         "platform": platform.platform(), "argv": sys.argv},
           "source_hashes": before, "imported_modules": imported,
           "suite": {"successful": True, "discovered": discovered, "executed": completed,
                     "tests_run": result.testsRun, "skipped": [(t.id(), reason) for t, reason in result.skipped],
                     "seconds": time.monotonic() - started,
                     "log_sha256": log_sha256},
           "isolation": {"violations": [], "children": events,
                         "mode": "fresh home/config; allowlisted env; loopback only; guarded descendants",
                         "thread_trace": True, "child_trace": False},
           "modules": modules, "line_counts": counts}
    doc["changed_lines"] = changed_line_coverage(ROOT, diff_base, modules)
    with open(opts.output, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(doc, sort_keys=True) + "\n")
    report = doc["changed_lines"]
    print("TRACE SAVED:", opts.output, "; changed-line coverage", report["executed"],
          "/", report["checked"], "; gaps", report["gaps"], flush=True)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=HERE / "coverage_baseline.json")
    parser.add_argument("--diff-base")
    parser.add_argument("--check-isolation", action="store_true")
    parser.add_argument("--tests", nargs="+", help="focused pre-trace checks; never write baseline")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--sandbox", help=argparse.SUPPRESS)
    parser.add_argument("--events", help=argparse.SUPPRESS)
    opts = parser.parse_args()
    opts.output = opts.output.resolve()
    if opts.child:
        return child(opts)
    with tempfile.TemporaryDirectory(prefix="harness-coverage-") as sandbox:
        env = isolated_env(sandbox)
        events = str(Path(sandbox) / "events")
        candidate = Path(sandbox) / "trace.json"
        forward = []
        if opts.diff_base:
            forward += ["--diff-base", opts.diff_base]
        if opts.check_isolation:
            forward += ["--check-isolation"]
        if opts.tests:
            forward += ["--tests", *opts.tests]
        argv = [sys.executable, "-I", "-S", "-X", "utf8", "-B", "-W",
                "error::ResourceWarning", str(Path(__file__).resolve()), *forward,
                "--output", str(candidate), "--child", "--sandbox", sandbox, "--events", events]
        result = subprocess.run(argv, cwd=str(ROOT), env=env, capture_output=True,
                                text=True, encoding="utf-8", errors="replace")
        output = result.stdout + result.stderr
        print(output, end="", flush=True)
        Path(str(opts.output) + ".runner.log").write_text(output, encoding="utf-8")
        if Path(str(candidate) + ".log").exists():
            Path(str(opts.output) + ".log").write_bytes(Path(str(candidate) + ".log").read_bytes())
        Path(str(opts.output) + ".guard.json").write_text(
            json.dumps(guard_events(events), indent=2) + "\n", encoding="utf-8")
        # Shutdown-time unraisable leaks must not publish an apparently clean trace.
        leaked = any(s in output for s in ("ResourceWarning", "unclosed file"))
        if result.returncode or leaked:
            return result.returncode or 1
        if candidate.exists():
            opts.output.write_bytes(candidate.read_bytes())
            print("Published reviewable trace:", opts.output, flush=True)
        return 0


if __name__ == "__main__":
    sys.exit(main())
