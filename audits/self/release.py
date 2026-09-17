#!/usr/bin/env python3
"""Scripted release driver: mechanizes docs/releasing.md steps in order.

THE SPLIT (explicit per CONTRIBUTING): the script automates the MECHANICS
and gates each step on the previous one; the HUMAN still decides WHEN to
release, reviews the PR, and approves merge/tag/publish. Every verification
step here calls the repo's own checks -- ruff, compileall, the full unittest
battery under -W error::ResourceWarning (with the R13 leak-signature scan),
and the self-audit, which MUST report BAR MET. The driver never bypasses or
reimplements an audit check.

  python audits/self/release.py --dry-run              # verify, no mutations
  python audits/self/release.py --dry-run --full-matrix
  python audits/self/release.py --version 0.3.3        # real edits + battery

Steps (each fails loudly before the next starts):
  state     clean tree; HEAD == origin/main; CI on main printed ONCE per
            invocation (never polled -- a non-terminal run exits 3 with
            "still queued, re-run me"; a failed run aborts).
  edits     CHANGELOG flatten ([Unreleased] -> dated section, fresh
            "Nothing yet."), version bump in BOTH single-source sites
            (pyproject.toml + harness/__init__.py), editable reinstall with
            a live-metadata check. Skipped by --dry-run.
  battery   ruff once; then per interpreter (local default + every uv-managed
            CI interpreter found: 3.9/3.11/3.13; --full-matrix asks uv for
            all it knows): compileall + the full battery with the leak
            scan; then the self-audit with a hard BAR MET gate and the
            round2_scores.json restore verified via git status.
  publish   (real runs) build + twine check + outside-repo venv smoke with
            the direct site-packages/harness/ui asset probe; then prints
            the tag-gate reminder: tag ONLY after main post-merge CI is
            completed/success. PR creation/merge/tag/publish stay
            human-run gh steps. --dry-run prints the commands instead.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
AUDIT = HERE / "audit.py"
NL = chr(13) + chr(10)
LEAK_SIGNATURES = ("ResourceWarning", "unclosed file")
UI_ASSETS = ("index.html", "app.js", "app.css")
DIST_NAME = "sovereign-harness"


def fail(step, msg):
    print("[release] ABORT at step '" + step + "': " + msg, file=sys.stderr)
    raise SystemExit(2)


def run(cmd, cwd=ROOT):
    """argv-list subprocess with inherited stdio (injection-safe)."""
    print("[release] $ " + " ".join(cmd))
    return subprocess.run(cmd, cwd=str(cwd), check=True)


def capture(cmd, cwd=ROOT):
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def venv_python(venv):
    """The smoke venv's interpreter: Scripts/python.exe on Windows,
    bin/python on POSIX. Resolved by existence, not sys.platform, so the
    smoke step works on any release machine."""
    for cand in (os.path.join(venv, "Scripts", "python.exe"),
                 os.path.join(venv, "bin", "python")):
        if os.path.exists(cand):
            return cand
    fail("publish", "no python executable in the smoke venv at "
         + repr((venv, "Scripts/python.exe", "bin/python")))


def interpreters(full=False):
    """Battery matrix: local default + uv-managed CI interpreters.

    Found via 'uv python find X.Y' (PATH fallback), so a release battery
    statement covers the CI matrix (3.9/3.11/3.13) by direct execution.
    --full-matrix asks uv for every interpreter it knows.
    """
    found = [(sys.executable, "local default")]
    if full:
        try:
            out = capture(["uv", "python", "list", "--only-installed"])
        except FileNotFoundError:
            out = None
        for line in (out.stdout.splitlines() if out is not None else []):
            m = re.match(r"cpython-(\d+)\.(\d+)\.\d+", line.strip())
            if m:
                found.append((line.split()[-1],
                              "cpython " + m.group(1) + "." + m.group(2)))
        return found
    for ver in ("3.9", "3.11", "3.13"):
        try:
            p = capture(["uv", "python", "find", ver])
        except FileNotFoundError:
            p = None  # uv not installed here: fall through to PATH, then the honest note
        path = (p.stdout.strip()
                if p is not None and p.returncode == 0 and p.stdout.strip()
                else None)
        if path and Path(path).exists():
            found.append((path, "cpython " + ver + " (uv-managed)"))
        else:
            alt = shutil.which("python" + ver)
            if alt:
                found.append((alt, "cpython " + ver + " (PATH)"))
            else:
                print("[release] note: no " + ver + " interpreter -- "
                      "the battery statement will NOT cover it")
    return found


def step_state():
    dirty = capture(["git", "status", "--short"]).stdout
    tracked = [line for line in dirty.splitlines()
               if line.strip() and not line.startswith("??")]
    if tracked:
        fail("state", "uncommitted tracked changes: " + repr(tracked))
    head = capture(["git", "rev-parse", "HEAD"]).stdout.strip()[:10]
    run(["git", "fetch", "origin", "main", "--quiet"])
    origin = capture(["git", "rev-parse", "origin/main"]).stdout.strip()[:10]
    print("[release] head=" + head + " origin/main=" + origin)
    if head != origin:
        fail("state", "HEAD " + head + " != origin/main " + origin
             + " -- get the branch merged/updated first")
    out = capture(["gh", "run", "list", "--branch", "main", "--limit", "1",
                   "--json", "headSha,status,conclusion"]).stdout.strip()
    try:
        info = json.loads(out)[0]
    except (ValueError, IndexError):
        print("[release] CI on main: UNKNOWN (no run data)")
        return
    print("[release] CI on main at " + info.get("headSha", "")[:10] + ": "
          + info.get("status", "?") + " "
          + str(info.get("conclusion") or "pending") + "  (one look; never polled)")
    if info.get("status") != "completed":
        print("[release] still queued, re-run me")
        raise SystemExit(3)
    if info.get("conclusion") != "success":
        fail("state", "main CI is " + str(info.get("conclusion"))
             + " -- fix forward before releasing")


def _current_version():
    m = re.search(r'version\s*=\s*"(\d+\.\d+\.\d+)"',
                  (ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    if not m:
        fail("edits", "no version literal found in pyproject.toml")
    return m.group(1)


def step_edits(dry, version):
    old = _current_version()
    today = date.today().isoformat()
    chg = ROOT / "CHANGELOG.md"
    src = chg.read_bytes().decode("utf-8")
    i = src.find("## [Unreleased]")
    if i < 0:
        fail("edits", "no [Unreleased] section found")
    if "Nothing yet." in src[i:i + 200]:
        fail("edits", "[Unreleased] reads 'Nothing yet.' -- nothing to release")
    j = src.find("## [", i + 5)
    if j < 0:
        fail("edits", "no section after [Unreleased] to anchor the flatten")
    body = src[i:j].rstrip()
    if dry:
        print("[release] dry-run: would flatten [Unreleased] ("
              + str(body.count(NL) + 1) + " lines) into [" + version + "] -- "
              + today + ", and reset [Unreleased] to 'Nothing yet.'")
        print("[release] dry-run: would bump version " + old + " -> " + version
              + " in pyproject.toml and harness/__init__.py")
        print("[release] dry-run: would run: python -m pip install -e . --no-deps")
        return
    # preserve the file header (Keep a Changelog / semver statements,
    # everything above [Unreleased]) -- D6 checks it; only the section
    # region is rewritten
    section = (body[len("## [Unreleased]"):]).strip()
    new_head = ("## [Unreleased]" + NL + NL + "Nothing yet." + NL + NL
                + "## [" + version + "] " + chr(0x2014) + " " + today + NL + NL
                + section + NL + NL)
    chg.write_bytes((src[:i] + new_head + src[j:]).encode("utf-8"))
    print("[release] CHANGELOG flattened to [" + version + "] " + today)
    for site in ("pyproject.toml", "harness/__init__.py"):
        p = ROOT / site
        txt = p.read_bytes().decode("utf-8")
        if old not in txt:
            fail("edits", "current version " + old + " not found in " + site)
        p.write_bytes(txt.replace(old, version, 1).encode("utf-8"))
        print("[release] version bumped in " + site + ": " + old + " -> " + version)
    run([sys.executable, "-m", "pip", "install", "-e", ".", "--no-deps"])
    got = capture([sys.executable, "-c",
                   "from importlib import metadata as m; "
                   "print(m.version('" + DIST_NAME + "'))"])
    if got.stdout.strip() != version:
        fail("edits", "editable-install metadata " + repr(got.stdout.strip())
             + " != release version " + version)
    print("[release] editable install reports " + version + " (D5 satisfied)")


def _battery_one(path, label):
    run([path, "-m", "compileall", "-q", "harness", "tests", "audits"])
    out = capture([path, "-W", "error::ResourceWarning", "-m", "unittest",
                   "discover", "-s", "tests", "-t", "."])
    blob = (out.stdout or "") + (out.stderr or "")
    leaked = [sig for sig in LEAK_SIGNATURES if sig in blob]
    if leaked:
        fail("battery", label + ": leak signature(s) " + repr(leaked)
             + " in suite output")
    if out.returncode != 0:
        tail = [line for line in blob.strip().splitlines() if line.strip()][-4:]
        fail("battery", label + ": unittest failed: " + " | ".join(tail))
    m = re.search(r"Ran (\d+) tests.*?(OK|FAILED)", blob, re.S)
    sk = re.search(r"skipped=(\d+)", blob)
    print("[release] battery " + label + ": "
          + (m.group(0).replace("\r", " ").replace("\n", " ") if m else "?")
          + ("" if not sk else " (leak scan: clean)"))


def step_battery(dry, full=False):
    if dry:
        print("[release] dry-run: battery WOULD run here (ruff once; then "
              "compileall + unittest + leak scan per interpreter; then the "
              "self-audit with a hard BAR MET gate):")
        for path, label in interpreters(full):
            print("[release]   - " + label + " -> " + path)
        print("[release] dry-run: self-audit BAR MET gate + scores restore "
              "would run (restore verified via git status)")
        return
    run([sys.executable, "-m", "ruff", "check", "harness", "tests", "audits"])
    for path, label in interpreters(full):
        _battery_one(path, label)
    print("[release] self-audit (hard BAR MET gate):")
    fd, backup = tempfile.mkstemp(suffix=".bak")
    os.close(fd)
    scores = ROOT / "audits" / "self" / "round2_scores.json"
    shutil.copyfile(scores, backup)
    try:
        run([sys.executable, str(AUDIT)])
        verdict = json.loads(scores.read_text(encoding="utf-8")).get("verdict", "")
        if "bar met" not in verdict.lower():
            fail("battery", "self-audit verdict not BAR MET: " + verdict)
        print("[release] self-audit: BAR MET")
    finally:
        shutil.copyfile(backup, scores)
        os.unlink(backup)
    dirty = [line for line in capture(["git", "status", "--short"]).stdout.splitlines()
             if "round2_scores.json" in line]
    if dirty:
        fail("battery", "round2_scores.json not restored: " + repr(dirty))
    print("[release] round2_scores.json restored (git status clean of it)")


def step_publish(dry, version):
    if dry:
        print("[release] dry-run: publish steps WOULD run, in order:")
        print("[release]   (human) commit the release edits, open + review the "
              "PR, merge per precedent")
        print("[release]   gh run list --branch main --limit 1  # ONE look; "
              "tag ONLY on completed success")
        print("[release]   python -m build && python -m twine check dist/*")
        print("[release]   venv smoke OUTSIDE the repo: wheel version, console "
              "scripts, direct site-packages/harness/ui asset probe")
        print("[release]   git tag -a v" + version + " && push the tag to BOTH "
              "remotes")
        print("[release]   gh release create v" + version
              + " dist/* with notes extracted from the tagged changelog")
        return
    # stale artifacts would poison twine/smoke selection; dist/ is a
    # gitignored build dir, regenerated wholesale here
    shutil.rmtree(ROOT / "dist", ignore_errors=True)
    run([sys.executable, "-m", "build"])
    # no shell: glob here so twine gets real paths on every platform
    dist_files = sorted(str(p) for p in (ROOT / "dist").glob("*"))
    if not dist_files:
        fail("publish", "dist/ is empty after build")
    run([sys.executable, "-m", "twine", "check"] + dist_files)
    wheels = [w for w in sorted((ROOT / "dist").glob("*.whl"))
           if version in w.name]
    if not wheels:
        fail("publish", "no wheel for version " + version + " in dist/")
    with tempfile.TemporaryDirectory() as td:
        venv = os.path.join(td, "venv")
        run([sys.executable, "-m", "venv", venv])
        vpy = venv_python(venv)
        run([vpy, "-m", "pip", "install", "--no-index", str(wheels[0])])
        got = capture([vpy, "-c", "from importlib import metadata as m; "
                       "print(m.version('" + DIST_NAME + "'))"])
        if got.stdout.strip() != version:
            fail("publish", "wheel smoke version mismatch: "
                 + repr(got.stdout.strip()))
        sp = capture([vpy, "-c", "import pathlib, harness; "
                      "print(pathlib.Path(harness.__file__).parent)"])
        ui = Path(sp.stdout.strip()) / "ui"
        missing = [n for n in UI_ASSETS if not (ui / n).exists()]
        if missing:
            fail("publish", "UI assets missing from the wheel: " + repr(missing))
        print("[release] venv smoke OK: version " + version + " + UI assets "
              + repr(UI_ASSETS) + " (direct site-packages probe)")
    print("[release] TAG GATE: tag only after main post-merge CI is "
          "completed/success (one gh look at a time, never polled).")
    print("[release] REMAINING HUMAN STEPS: PR review/merge, tag, GitHub "
          "Release publish (see docs/releasing.md).")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Scripted release driver: mechanics only -- humans decide "
                    "timing, review the PR, merge, tag, and publish.")
    ap.add_argument("--dry-run", action="store_true",
                    help="run every read-only verification; skip all mutations")
    ap.add_argument("--version",
                    help="release version, e.g. 0.3.3 (required for real edits)")
    ap.add_argument("--full-matrix", action="store_true",
                    help="extend the battery to every interpreter uv knows")
    args = ap.parse_args(argv)
    if not args.dry_run and not args.version:
        ap.error("--version is required without --dry-run")
    step_state()
    step_edits(args.dry_run, args.version or "0.0.0")
    step_battery(args.dry_run, args.full_matrix)
    step_publish(args.dry_run, args.version or "0.0.0")
    print("[release] done"
          + (" (dry-run: no mutations performed)" if args.dry_run else ""))


if __name__ == "__main__":
    main()
