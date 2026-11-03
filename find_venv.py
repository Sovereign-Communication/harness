import subprocess, sys, zipfile, json
from pathlib import Path

def site_and_prefix(python_exe):
    try:
        out = subprocess.check_output(
            [python_exe, "-c",
             "import site, sys; print(site.USER_SITE or ''); print(sys.prefix)"],
            text=True, stderr=subprocess.PIPE
        ).strip().splitlines()
        return out[0] if len(out) > 0 else "", out[1] if len(out) > 1 else ""
    except Exception as e:
        return "", ""

def local_bin():
    from pathlib import Path
    base = Path.home() / ".local" / "bin"
    # which python3 resolves there?
    python3 = base / "python3.exe"
    return python3 if python3.exists() else None

for label, exe in [
    ("system314", r"C:\Python314\python.exe"),
    ("localbin", str(local_bin())),
    ("hermes", r"C:\Users\SCM\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe"),
]:
    if not exe or not Path(exe).exists():
        print(label, "exe missing")
        continue
    user_site, prefix = site_and_prefix(exe)
    print(label, "user_site:", user_site, "prefix:", prefix)

    # search user_site for pip / ruff / pyright / pytest dist-info
    us = Path(user_site)
    if us.exists():
        for pkg in ["pip", "ruff", "pyright", "pytest"]:
            matches = list(us.glob(pkg + "-*.dist-info"))
            top = us / pkg
            print(f"   {pkg}: dist-info={[m.name for m in matches]}, pkg_dir={top.is_dir()}")
    else:
        print("   (no user_site)")

PYEOF