"""Provider-free coverage boundary; installed before discovery, not a live lane.

Python descendants are bootstrapped with the same guard (including -I/-S
children); only local Git operations are admitted natively. Unsupported native
children fail closed, never silently skip tests. This is containment for trusted
repository tests, not an OS sandbox against malicious Python/native extensions.
"""
import ipaddress
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys

_ESSENTIALS = ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PATHEXT")
_ROOTS = ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
          "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME")
_OPT_INS = ("HARNESS_TEST_ALLOW_NETWORK", "HARNESS_JEV_LIVE_SMOKE", "HARNESS_AUDIT_LIVE")
_GIT_READ = {"rev-parse", "cat-file", "diff", "diff-tree", "status", "ls-files",
             "log", "show", "rev-list", "merge-base", "check-ignore"}
_GIT_LOCAL = {"init", "config", "add", "commit", "merge", "worktree", "branch"}


def isolated_env(directory, inherited=None):
    """Allowlist OS essentials, isolate all config roots, never copy credentials."""
    source = os.environ if inherited is None else inherited
    env = {k: source[k] for k in _ESSENTIALS if k in source}
    # Bare `python` gates use this same measured interpreter, not another install.
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    home, temp = Path(directory) / "home", Path(directory) / "tmp"
    home.mkdir(parents=True, exist_ok=True)
    temp.mkdir(parents=True, exist_ok=True)
    env.update({k: str(home) for k in _ROOTS})
    env.update({k: str(temp) for k in ("TMP", "TEMP", "TMPDIR")})
    env.update({k: "0" for k in _OPT_INS})
    env.update(PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1",
               GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT="0", GIT_ALLOW_PROTOCOL="",
               HARNESS_LEDGER=str(Path(directory) / "ledger.jsonl"))
    return env


def loopback(host):
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if host in (None, "", "localhost", "0.0.0.0"):
        return True
    try:
        address = ipaddress.ip_address(str(host).strip("[]"))
    except ValueError:
        return False
    return (getattr(address, "ipv4_mapped", None) or address).is_loopback


def git_command(argv, cwd, sandbox):
    """Admit local git only, disabling hooks/helpers/config-driven execution."""
    args = list(argv[1:])
    repo = Path(cwd or os.getcwd()).resolve()
    while args and args[0] in ("-C", "-c"):
        if len(args) < 2:
            raise ValueError("incomplete git option")
        option, value = args[:2]
        if option == "-C":
            repo = (repo / value).resolve()
        elif not value.startswith(("user.name=", "user.email=")):
            raise ValueError("unadmitted git configuration")
        args = args[2:]
    if not args or args[0] not in _GIT_READ | _GIT_LOCAL:
        raise ValueError("only local git commands are admitted")
    if args[0] in _GIT_LOCAL:
        # Mutations are only fixtures under the fresh private temp root.
        target = (repo / args[-1]).resolve() if args[0] == "init" else repo
        target.relative_to(Path(sandbox).resolve())
    # An empty diff.external is still an external command to Git. Explicit
    # flags disable helpers rather than asking Git to spawn an empty program.
    if args[0] in {"diff", "diff-tree", "show", "log"}:
        command_index = len(argv) - len(args)
        argv = [*argv[:command_index + 1], "--no-ext-diff", "--no-textconv",
                *argv[command_index + 1:]]
    return [argv[0], "-c", "core.hooksPath=" + os.devnull,
            "-c", "core.fsmonitor=false", "-c", "commit.gpgsign=false",
            "-c", "tag.gpgsign=false", "-c", "diff.external=",
            "-c", "core.attributesFile=" + os.devnull, *argv[1:]]


def install_guard(sandbox, evidence_dir):
    """Guard external sockets and descendants before any product/test imports."""
    sandbox, evidence_dir = str(Path(sandbox).resolve()), Path(evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    violations = []
    events = evidence_dir / ("guard-" + str(os.getpid()) + ".jsonl")
    native_popen = subprocess.Popen

    def record(kind, **fields):
        with events.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"kind": kind, "pid": os.getpid(), **fields}) + "\n")

    def refuse(kind):
        violations.append(kind)
        record("refused", event=kind)
        raise OSError("provider-free coverage refused " + kind)

    def audit(event, args):
        if event in ("socket.connect", "socket.connect_ex", "socket.bind"):
            address = args[1]
            if not isinstance(address, tuple) or not loopback(address[0]):
                refuse(event)
        elif event in ("socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr"):
            if not loopback(args[0]):
                refuse(event)
        elif event in ("os.system", "os.posix_spawn", "os.spawn", "os.exec"):
            refuse(event)
        elif event == "subprocess.Popen":
            # The wrapper below supplies an internal marker; direct bypass is refused.
            env = args[3] or {}
            if env.get("HARNESS_COVERAGE_CHILD") != str(os.getpid()):
                refuse("uncontained subprocess")

    sys.addaudithook(audit)

    def guarded_popen(args, *positional, **kwargs):
        # Python 3.9 platform.win32_ver probes exactly these shell strings.
        # Admit only fixed `ver`, with cmd AutoRun disabled; no shell user input.
        if (os.name == "nt" and isinstance(args, str) and kwargs.get("shell")
                and args in ("ver", "command /c ver", "cmd /c ver")):
            args = [os.environ["COMSPEC"], "/d", "/c", "ver"]
            kwargs["shell"] = False
        if positional or isinstance(args, (str, bytes)) or kwargs.get("shell"):
            refuse("shell or positional subprocess")
        argv = [str(x) for x in args]
        if not argv:
            refuse("empty subprocess")
        executable = kwargs.get("executable") or shutil.which(argv[0]) or argv[0]
        executable = Path(executable).resolve()
        child_env = isolated_env(sandbox)
        # Preserve explicit harmless test variables, not inherited provider controls.
        for key, value in (kwargs.get("env") or {}).items():
            if key in ("RT_X",):
                child_env[key] = value
        child_env["HARNESS_COVERAGE_CHILD"] = str(os.getpid())
        if executable == Path(sys.executable).resolve():
            bootstrap = ("import runpy;runpy.run_path(" + repr(str(Path(__file__).resolve()))
                         + ",run_name='__coverage_child__')")
            argv = [str(executable), "-I", "-S", "-X", "utf8", "-W",
                    "error::ResourceWarning", "-c", bootstrap,
                    sandbox, str(evidence_dir), *argv[1:]]
        elif executable.name.lower() in ("git", "git.exe"):
            try:
                argv = git_command([str(executable), *argv[1:]], kwargs.get("cwd"), sandbox)
            except ValueError:
                refuse("unadmitted git invocation")
        elif (executable.name.lower() in ("uv", "uv.exe") and
              (argv[1:4] in (["python", "find", "3.9"], ["python", "find", "3.11"],
                             ["python", "find", "3.13"]) or
               argv[1:] == ["python", "list", "--only-installed"])):
            # Installed-interpreter enumeration only; never install/download/run.
            argv = [str(executable), "--offline", "--no-config", "--no-python-downloads", *argv[1:]]
        elif executable.name.lower() in ("node", "node.exe"):
            preload = Path(__file__).with_name("coverage_node_guard.cjs")
            child_env["HARNESS_COVERAGE_NODE_EVENTS"] = str(
                evidence_dir / ("node-" + str(os.getpid()) + ".jsonl"))
            argv = [str(executable), "--no-addons", "--require", str(preload), *argv[1:]]
        elif (os.name == "nt" and executable == Path(os.environ["COMSPEC"]).resolve()
              and argv[1:] == ["/d", "/c", "ver"]):
            pass  # fixed Windows version query; no AutoRun or arbitrary shell.
        elif executable.name.lower() in ("taskkill", "taskkill.exe") and argv[1:4] == ["/T", "/F", "/PID"]:
            pass  # Windows timeout cleanup, not arbitrary native work.
        elif not executable.exists() or executable.is_dir():
            pass  # Retain actual OS missing/not-executable error semantics.
        else:
            refuse("unsupported native child: " + executable.name)
        kwargs["env"] = child_env
        record("child", executable=executable.name)
        return native_popen(argv, **kwargs)

    # asyncio on Windows subclasses Popen at import time; retain that interface.
    class GuardedPopen(native_popen):
        def __new__(cls, args, *positional, **kwargs):
            return guarded_popen(args, *positional, **kwargs)

    subprocess.Popen = GuardedPopen
    return violations


def child_main():
    sandbox, evidence = sys.argv[1:3]
    argv = sys.argv[3:]
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    install_guard(sandbox, evidence)
    while argv and argv[0] in ("-I", "-S", "-B"):
        argv.pop(0)
    if argv[:1] == ["--version"]:
        print("Python " + sys.version.split()[0])
    elif argv[:1] == ["-c"] and len(argv) >= 2:
        sys.argv = ["-c", *argv[2:]]
        exec(compile(argv[1], "<string>", "exec"), {"__name__": "__main__"})
    elif argv[:1] == ["-m"] and len(argv) >= 2:
        sys.argv = argv[1:]
        runpy.run_module(argv[1], run_name="__main__", alter_sys=True)
    elif argv and not argv[0].startswith("-"):
        sys.argv = argv
        runpy.run_path(argv[0], run_name="__main__")
    else:
        raise OSError("unsupported Python child arguments")


if __name__ == "__coverage_child__":
    child_main()
