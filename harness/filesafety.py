"""File-safety primitives for the apply engine.

Every mutation of a real file on disk goes through this module: the atomic
write (symlink-refusing, mode-preserving), the out-of-tree backup (content +
permission mode, capped per file), and the shell-free verification-gate
runner (shlex-tokenized, timeout-killed -- metacharacters are inert).
"""
import hashlib
import os
import shlex
import stat
import subprocess
import shutil
import tempfile

from .errors import HarnessError
from .output import eprint

VERIFY_TIMEOUT = 300

# Backups kept per target file before the oldest is pruned.
MAX_BACKUPS_PER_FILE = 20


def _verify_argv(command):
    """Tokenize a verify command with POSIX-ish shlex. Raises HarnessError when
    quoting is unbalanced -- fail closed rather than guessing."""
    try:
        argv = shlex.split(command)
    except ValueError as e:
        raise HarnessError(f"verify_cmd is not shell-tokenizable ({e}); quote it properly.")
    if not argv:
        raise HarnessError("verify_cmd is empty.")
    return argv


def default_run_verify(command, timeout=VERIFY_TIMEOUT, cwd=None):
    """Run a verify command WITHOUT a shell. The command is tokenized with
    shlex and executed directly, so shell metacharacters (&&, |, ;, backticks,
    $()) are inert. `timeout` kills a hung gate instead of hanging the run.
    """
    argv = _verify_argv(command)
    try:
        result = subprocess.run(argv, shell=False, capture_output=True, text=True,
                                timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired:
        return 124, f"verify gate timed out after {timeout}s (killed): {command}"
    except FileNotFoundError:
        return 127, f"verify gate executable not found: {argv[0]}"
    except PermissionError:
        return 126, f"verify gate is not executable: {argv[0]}"
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def validate_verify_command(command):
    """Preflight a --verify gate WITHOUT executing it: the command must be
    shell-tokenizable and its interpreter/tool must be findable (shutil.which
    covers PATH entries and direct script paths alike). Honesty, not a
    guarantee -- a gate that exists can still fail at runtime. Its purpose is
    the dogfood preflight: a typo'd gate must be caught before the paid panel
    phase, exactly like a typo'd --file."""
    argv = _verify_argv(command)
    if shutil.which(argv[0]) is None:
        raise HarnessError(
            f"verify gate executable not found: {argv[0]} "
            "(the gate would fail every round; fix --verify before spending a live run)")


def file_content_hash(path):
    """Return a stable hash of a target file for continuation integrity.

    Hash bytes rather than decoded text so line endings and encoding changes
    cannot be mistaken for the same baseline.
    """
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError as e:
        raise HarnessError(f"cannot hash target file: {path} ({e})")


def _line_count(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return sum(1 for _ in f)


class _AtomicWriteError(OSError):
    """The target of an atomic write refused the operation (symlink, escape,
    or vanished directory) -- never follow through by writing anyway."""


def detect_newline(path):
    """The target file's own line-ending style, or None when unknown.

    Reads the first bytes only: a ``\\r\\n`` anywhere means CRLF (mixed
    files normalize to CRLF); bare ``\\n`` means LF; no newline at all
    (or an unreadable file) means None. Model output arrives with ``\\n``
    endings -- it never saw the tree's bytes -- so writes must aim at the
    target's style explicitly instead of laundering checkouts.
    """
    try:
        with open(path, "rb") as f:
            sample = f.read(8192)
    except OSError:
        return None
    if b"\r\n" in sample:
        return "\r\n"
    if b"\n" in sample:
        return "\n"
    return None


def _atomic_write(path, content, *, follow=False, newline="preserve"):
    """Atomically replace `path` with `content`, refusing unsafe targets.

    Without ``follow=True`` a pre-existing symlink is never followed (the
    classic dotfile-points-into-the-repo trick). The temp file is staged
    inside the target's directory so the final replace is atomic. The
    target's permission mode is preserved (tempfile.mkstemp creates 0600,
    which would otherwise silently strip an executable bit from a verify
    script or gate artifact and change the semantics of the working tree).

    ``newline="preserve"`` (default) translates the content to the target
    file's own detected style, so a model-written LF body never flips a
    CRLF checkout (and the failed-run rewind restores the exact original
    style, since preserved writes never change it in the first place).
    ``newline=None`` writes bytes exactly as given -- for snapshot restore,
    where the snapshot's bytes, not the tree's style, are authoritative.
    """
    d = os.path.dirname(os.path.abspath(path)) or "."
    if os.path.islink(path) and not follow:
        raise _AtomicWriteError(
            f"refusing to write through symlink: {path} "
            "(delete the link or pass follow_symlinks=True)")
    if newline == "preserve":
        style = detect_newline(path)
        if style == "\r\n":
            content = content.replace("\r\n", "\n").replace("\n", "\r\n")
    try:
        fd, tmp = tempfile.mkstemp(prefix=".harness-", suffix=".tmp", dir=d)
    except OSError as e:
        raise _AtomicWriteError(f"cannot stage temp file in {d}: {e}")
    try:
        # newline="" writes the string unchanged: with preserve mode the
        # translation above already ran, and with newline=None the caller
        # takes full responsibility for the bytes (snapshot restore).
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
            os.chmod(tmp, mode)
        except OSError:
            pass  # new file: keep the safe 0600 default
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def validate_target_file(file_path):
    """One owner of 'what is a valid edit target': must exist and be a regular
    file. Interfaces preflight this before spending live calls (a typo'd path
    must not burn a panel verify); apply_edit enforces the same policy at the
    engine boundary."""
    if not os.path.exists(file_path):
        raise HarnessError(f"file not found: {file_path}")
    if os.path.islink(file_path):
        raise HarnessError(
            f"symlink targets are not editable: {file_path} (use the real file path)")
    if not os.path.isfile(file_path):
        raise HarnessError(
            f"not a regular file: {file_path} (directories are not editable)")


def backup_file(file_path, task_id, round_no):
    """Preserve the pre-edit file (content + permission mode) outside the
    working tree so a restore never has to trust the tree itself (#6).
    Failure is non-fatal but never silent. Returns the backup path or None.
    """
    d = os.path.join(tempfile.gettempdir(), "harness-backups")
    try:
        if os.path.islink(d):
            # Hijacked backup dir (a pre-planted symlink): every backup
            # write below would land wherever the link points, and the
            # predictable dest names below would let a link inside a
            # hostile dir redirect to an arbitrary file. Fail closed.
            raise OSError(f"backup dir is a symlink, refusing: {d}")
        os.makedirs(d, exist_ok=True)
        st = os.stat(file_path)
        # Task ids may contain separators (bench names its tasks
        # 'bench/<name>'), which would land in the backup FILENAME and
        # break open() on every platform. Flatten them.
        safe_task = str(task_id).replace("/", "_").replace("\\", "_")
        dest = os.path.join(d, f"{safe_task}-r{round_no}-{os.path.basename(file_path)}")
        if os.path.islink(dest):
            # Planted link at a predictable name: never follow it.
            raise OSError(f"backup destination is a symlink, refusing: {dest}")
        with open(file_path, "rb") as src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out)
        os.chmod(dest, st.st_mode & 0o777)  # preserve mode for faithful restore
        # Prune oldest backups of this file beyond the cap.
        prefix = f"{safe_task}-"
        siblings = sorted(fn for fn in os.listdir(d)
                          if fn.startswith(prefix) and fn.endswith("-" + os.path.basename(file_path)))
        for fn in siblings[:-MAX_BACKUPS_PER_FILE]:
            try:
                os.unlink(os.path.join(d, fn))
            except OSError:
                pass
        return dest
    except OSError as e:
        eprint(f"[warn] backup failed for {file_path}: {e}")
        return None
