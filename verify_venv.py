import subprocess, sys
from pathlib import Path

venv = Path(".venv") / "Scripts" / "python.exe"
print("venv python exists:", venv.exists())

def has(python_exe, *modules):
    try:
        subprocess.check_output([python_exe, "-c", "import " + ", ".join(modules)],
                                text=True, stderr=subprocess.PIPE)
        return True
    except Exception:
        return False

print("venv pytest:", has(venv, "pytest"))
print("venv ruff  :", has(venv, "ruff"))
print("venv pyright:", has(venv, "pyright"))
print("system pytest:", has(sys.executable, "pytest"))
print("system ruff  :", has(sys.executable, "ruff"))
print("system pyright:", has(sys.executable, "pyright"))
