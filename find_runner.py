"""
Locate a python that can run pytest and pyright, or install into the repo's
own .venv. The intent is to leave this lane's deterministic checks able to run
hermetic tests + ruff + pyright so any later lane work can be verified without
asking the operator for a python toolchain.

Strategy:
1. If a repo-local .venv exists with pytest+pyright, use it.
2. Otherwise, attempt to create .venv from the system python314 (which has pip
   and ruff already) and install only pytest + pyright into it.
3. If that fails (no internet, blocked, etc.), record the failure explicitly in
   the scorecard instead of silently falling back to a tool-less python.
"""
import subprocess
import sys
import json
from pathlib import Path

REPO = Path(".")
VENV = REPO / ".venv"
SYSTEM_PY = r"C:\Python314\python.exe"

def run(cmd, cwd=None, env=None):
    try:
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT, cwd=cwd, env=env)
        return 0, out.strip()
    except subprocess.CalledProcessError as e:
        return e.returncode, (e.stdout or e.stderr or "").strip()

def has(python_exe, *modules):
    rc, out = run([python_exe, "-c", "import " + ", ".join(modules)], cwd=REPO)
    return rc == 0

def best_python():
    # Prefer a venv python that already has what we need.
    candidates = []
    if VENV.is_dir():
        vpy = VENV / "Scripts" / "python.exe"
        if vpy.exists():
            candidates.append(("repo .venv", vpy))
    candidates.append(("system314", Path(SYSTEM_PY)))
    for label, py in candidates:
        if py.exists() and has(py, "pip"):
            return label, py
    return None, None

label, py = best_python()
print("python candidate:", label, py)

def ensure():
    # If repo .venv exists but missing pytest/pyright, install into it.
    vpy = VENV / "Scripts" / "python.exe"
    if VENV.is_dir() and vpy.exists():
        if has(vpy, "pytest", "pyright"):
            return True, vpy, "exists-with-tools"
        print("repo .venv exists but missing pytest/pyright; installing...")
        rc, out = run([str(vpy), "-m", "pip", "install", "-q", "pytest", "pyright"], cwd=REPO)
        if rc == 0 and has(vpy, "pytest", "pyright"):
            return True, vpy, "installed-into-existing-venv"
        print("install into existing venv failed:", rc, out[-500:])
        return False, vpy, out[-300:]

    # No usable venv: try to create one from system314.
    print("no usable venv; creating .venv from system314")
    rc, out = run([SYSTEM_PY, "-m", "venv", str(VENV)], cwd=REPO)
    if rc != 0:
        return False, vpy, "venv-create-failed: " + out[-300:]
    vpy = VENV / "Scripts" / "python.exe"
    rc, out = run([str(vpy), "-m", "pip", "install", "-q", "pytest", "pyright"], cwd=REPO)
    if rc == 0 and has(vpy, "pytest", "pyright"):
        return True, vpy, "created-and-installed"
    return False, vpy, "install-after-venv-failed: " + out[-300:]

ok, chosen, status = ensure()
print("ensure result:", status)
print("chosen python:", chosen)

result = {
    "python_label": label,
    "python_path": str(py) if py else None,
    "ensure_status": status,
    "ensure_ok": ok,
    "chosen_python": str(chosen) if chosen else None,
    "pytest_ok": has(chosen, "pytest") if chosen else False,
    "pyright_ok": has(chosen, "pyright") if chosen else False,
    "ruff_ok": has(chosen, "ruff") if chosen else False,
}

out_path = REPO / "docs" / "runner_env_status.json"
out_path.write_text(json.dumps(result, indent=2) + "\n")
print("wrote", out_path)
