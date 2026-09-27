"""File-safety primitives for the apply engine.

Every mutation of a real file on disk goes through this module: the atomic
write (symlink-refusing, mode-preserving), the out-of-tree backup (content +
permission mode, capped per file), and the verification-gate policy.

The OS-facing mechanics moved to their single owners during platform
unification (``PLAT-osal-module`` / ``PLAT-cmd-data``): the atomic write and
newline detection live in :mod:`harness.osal`, the shell-free gate runner in
:mod:`harness.gate_runner`. This module keeps the apply engine's *policy*
about them, so engine callers and their tests are unaffected.
"""
import hashlib
import os
import shutil
import tempfile
import uuid

from .errors import HarnessError
from .gate_runner import VERIFY_TIMEOUT, run_gate, validate_gate
from . import osal
from .output import eprint

# Backups kept per target file before the oldest is pruned.
MAX_BACKUPS_PER_FILE = 20
def default_run_verify(command, timeout=VERIFY_TIMEOUT, cwd=None):
    """Run a verify command WITHOUT a shell and report ``(returncode, output)``.

    The single runner (:func:`harness.gate_runner.run_gate`) tokenizes the
    command in a Windows-aware way and executes an argv list, so shell
    metacharacters (&&, |, ;, backticks, $()) are inert AND a Windows path
    survives tokenizing. `timeout` kills a hung gate instead of hanging the
    run.
    """
    return run_gate(command, timeout=timeout, cwd=cwd)



def validate_verify_command(command, require_executable=True):
    """Preflight a --verify gate WITHOUT executing it: the command must
    tokenize and (when require_executable) its interpreter/tool must be
    findable. Honesty, not a guarantee -- a gate that exists can still fail
    at runtime. Engine callers pass require_executable=False so hermetic
    library stubs stay usable; dogfood/CLI keep the PATH check."""
    return validate_gate(command, require_executable=require_executable)



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
        raise HarnessError(f"cannot hash target file: {path} ({e})") from e


def _line_count(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return sum(1 for _ in f)


# The atomic write itself is OS contact, so osal owns the mechanism; this
# alias keeps the engine's existing except-clauses and tests honest about
# which error they are catching (one class, one definition).
_AtomicWriteError = osal.AtomicWriteError


def detect_newline(path):
    """The target file's own line-ending style (or None) -- see osal."""
    return osal.detect_newline(path)


def _atomic_write(path, content, *, follow=False, newline="preserve"):
    """Atomically replace `path` with `content`, refusing unsafe targets.

    The mechanism (symlink refusal, same-directory staging, permission-mode
    preservation, newline policy) belongs to :func:`harness.osal.atomic_write_text`;
    this is the apply engine's one call site for it, kept as a name so the
    engine's policy reads in one place.
    """
    return osal.atomic_write_text(path, content, follow=follow, newline=newline)


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

    TOCTOU hardening: the destination is created with O_CREAT|O_EXCL so a
    pre-planted file or symlink at the predictable name cannot be followed
    or overwritten.
    """
    d = os.path.join(tempfile.gettempdir(), "harness-backups")
    try:
        if os.path.islink(d):
            # Hijacked backup dir (a pre-planted symlink): every backup
            # write below would land wherever the link points. Fail closed.
            raise OSError(f"backup dir is a symlink, refusing: {d}")
        os.makedirs(d, exist_ok=True)
        st = os.stat(file_path)
        # Task ids may contain separators (bench names its tasks
        # 'bench/<name>'), which would land in the backup FILENAME and
        # break open() on every platform. Flatten them.
        safe_task = str(task_id).replace("/", "_").replace("\\", "_")
        # Unique dest so a prior backup (or a hostile plant) never collides
        # with O_EXCL; prune still keys off the task+basename prefix.
        unique = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        dest = os.path.join(
            d, f"{safe_task}-r{round_no}-{unique}-{os.path.basename(file_path)}")
        # O_EXCL: never open an existing path (including a symlink).
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(dest, flags, 0o600)
        except FileExistsError as e:
            raise OSError(f"backup destination already exists, refusing: {dest}") from e
        try:
            with os.fdopen(fd, "wb") as out, open(file_path, "rb") as src:
                shutil.copyfileobj(src, out)
        except Exception:
            try:
                os.unlink(dest)
            except OSError:
                pass
            raise
        os.chmod(dest, st.st_mode & 0o777)  # preserve mode for faithful restore
        # Prune oldest backups of this file beyond the cap. Never unlink the
        # just-created dest (unique names can sort first).
        prefix = f"{safe_task}-"
        base = os.path.basename(file_path)
        siblings = sorted(fn for fn in os.listdir(d)
                          if fn.startswith(prefix) and fn.endswith("-" + base)
                          and fn != os.path.basename(dest)
                          and os.path.isfile(os.path.join(d, fn)))
        # Keep at most MAX_BACKUPS_PER_FILE-1 older siblings plus the new one.
        for fn in siblings[:-(max(0, MAX_BACKUPS_PER_FILE - 1))]:
            try:
                os.unlink(os.path.join(d, fn))
            except OSError:
                pass
        return dest
    except OSError as e:
        eprint(f"[warn] backup failed for {file_path}: {e}")
        return None
