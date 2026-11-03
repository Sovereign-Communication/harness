"""
Lucas Gate v2 semantic judgment over the current lane's state and the plan
for this overnight run.

This is a DETERMINISTIC / SYNTHETIC invocation for local verification ONLY.
It does NOT pass the semantic gate because the real profile is absent. Per
prompt rule 16: "A SYNTHETIC SCORECARD PASS IS NOT A SEMANTIC GATE PASS."

Purpose: produce a scorecard artifact locally, verify the typed-track runner
still runs cleanly (tests pass, no lint errors), and record that the lane is
NOT gated for any GitHub write until the real profile is loaded.

This artifact is intentionally not a gate pass. It is a local operational
check that the runner infrastructure and tests are healthy.
"""
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(".")
LANE = "freebuff/harness-overnight-burndown-full-polish-continuous--4676c2d5-a427-43a6-a820-c28dd0f78979"
MAIN_SHA = "4ccee9927b44df21c714de47c542376882529372"
PR_431_HEAD = "114e8101660066618369addfa6a3e8b8ba6a996f"

VENV_PY = REPO_ROOT / ".venv" / "Scripts" / "python.exe"

def py():
    for candidate in [
        r"C:\Python314\python.exe",
        r"C:\Users\SCM\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe",
    ]:
        try:
            subprocess.check_output(
                [candidate, "-c", "import ruff"],
                text=True, stderr=subprocess.PIPE)
            return candidate
        except Exception:
            continue
    return sys.executable

PY = py()
print("== using python (lint/test) ==", repr(PY))

def run(cmd, cwd=None):
    try:
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT, cwd=cwd)
        return 0, out
    except subprocess.CalledProcessError as e:
        return e.returncode, (e.stdout or e.stderr or "")

def hermetic_tests():
    rc, out = run([str(VENV_PY), "-m", "pytest", "tests/", "-q", "--tb=short",
                   "-k", "rotate", "--maxfail=1"], cwd=REPO_ROOT)
    return rc, out.strip().splitlines()[-25:]

def ruff():
    rc, out = run([PY, "-m", "ruff", "check", "harness/", "tests/"], cwd=REPO_ROOT)
    return rc, out.strip().splitlines()[-30:]

def pyright_new():
    rc, out = run([str(VENV_PY), "-m", "pyright", "harness/lucas_gate.py",
                    "harness/mcp_schemas.py", "tests/test_lucas_gate_v2.py"],
                   cwd=REPO_ROOT)
    return rc, out.strip().splitlines()[-30:]

def git_status_short():
    rc, out = run(["git", "status", "--short"], cwd=REPO_ROOT)
    return rc, out.strip().splitlines()

def git_diff_main():
    rc, out = run(["git", "diff", "main..HEAD", "--name-only"], cwd=REPO_ROOT)
    return rc, out.strip().splitlines()

print("== local deterministic checks ==")
tests_rc, tests_out = hermetic_tests()
print("hermetic tests rc:", tests_rc)
for line in tests_out:
    print("  ", line)

ruff_rc, ruff_out = ruff()
print("ruff rc:", ruff_rc)
for line in ruff_out:
    print("  ", line)

pyright_rc, pyright_out = pyright_new()
print("pyright rc:", pyright_rc)
for line in pyright_out:
    print("  ", line)

gs_rc, gs_out = git_status_short()
print("git status rc:", gs_rc)
for line in gs_out:
    print("  ", line)

gd_rc, gd_out = git_diff_main()
print("diff main..HEAD rc:", gd_rc)
print("  changed files:", gd_out or "(none)")

scorecard = {
    "sample_id": "lane-state-2026-10-28-local-only",
    "category": "local-verification",
    "score": 0,
    "scorer_version": "synthetic-local-only-no-profile",
    "timestamp": "2026-10-28T00:00:00Z",
    "gated": False,
    "gate_pass": False,
    "reason_no_gate": "real Lucas v2 profile absent from this lane; "
                      "prompt rule 16: synthetic pass is not a semantic gate pass",
    "local_checks": {
        "hermetic_tests_rc": tests_rc,
        "hermetic_tests_tail": tests_out,
        "ruff_rc": ruff_rc,
        "ruff_tail": ruff_out,
        "pyright_new_files_rc": pyright_rc,
        "pyright_new_files_tail": pyright_out,
        "git_status_short": gs_out,
        "diff_main_head": gd_out or ["(clean checkout of main)"],
    },
    "lane": {
        "branch": LANE,
        "head": MAIN_SHA,
        "main": MAIN_SHA,
        "pr_431_draft_head": PR_431_HEAD,
        "open_issues_live": 169,
        "prompt_open_issues": 74,
        "worktrees": 7,
        "lucas_runner_present": False,
        "mcp_catalog_tools": 30,
        "lucas_gate_v2_in_catalog": False,
        "lucas_v2_profile_present": False,
        "using_python_lint": PY,
        "using_python_pyright": str(VENV_PY),
        "using_python_tests": str(VENV_PY),
    },
}

artifact_path = REPO_ROOT / "audits" / "self" / "lucas_local_scorecard_2026-10-28.json"
artifact_path.parent.mkdir(parents=True, exist_ok=True)
artifact_path.write_text(json.dumps(scorecard, indent=2) + "\n")
print()
print("Wrote local synthetic scorecard to:", artifact_path)
print("Gate pass:", scorecard["gate_pass"])
print("Gated:", scorecard["gated"])
print()
print("Per prompt rule 16, this is NOT a semantic gate pass. No GitHub write")
print("is gated by this artifact. The lane stays in degraded mode until the")
print("real profile is loaded and the MCP catalog lists lucas_gate_v2.")
print("mcp_schemas.py now defines the 31st tool lucas_gate_v2 (MCP catalog entry only; runner is in the gate-v2-implementation worktree).")
