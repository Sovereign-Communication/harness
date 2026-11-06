"""Overlay verification: run one suite against a composed tree.

A probe answers a question about the machine it runs on. An overlay answers a
question about a tree that does not exist yet: take the shipped base, lay a
change over it, and run the project's own command against the result. The
base is never written to -- composition happens in a scratch directory -- so a
verification run cannot leave the checkout looking like an edit.

This is deliberately one small surface, not a build system. It composes whole
trees, refuses symlinks (a link in an overlay can point anywhere, and a
verification scratch is not the place to discover that), runs argv lists
through :mod:`harness.osal` with no shell, and reports the command's own exit
status as the verdict. Nothing here interprets tests: green means the project's
command said so.
"""
import os
import shutil
import tempfile
from typing import Any, Dict, Optional, Sequence

from .errors import HarnessError
from .osal import CommandResult, run as _osal_run


def _require_tree(path: str, label: str) -> str:
    if not path or not isinstance(path, str):
        raise HarnessError(f"overlay: {label} must be a directory path")
    if not os.path.isdir(path):
        raise HarnessError(f"overlay: {label} is not a directory: {path}")
    return os.path.abspath(path)


def _copy_overlay(source: str, dest: str) -> int:
    """Copy ``source`` over ``dest``; return the number of entries written.

    Symlinks are refused rather than followed: an overlay that can point
    outside the scratch tree is not a tree, and copying a link verbatim would
    make the composed result depend on the machine's filesystem state.
    """
    written = 0
    for root, dirs, files in os.walk(source):
        rel = os.path.relpath(root, source)
        target_root = dest if rel == "." else os.path.join(dest, rel)
        os.makedirs(target_root, exist_ok=True)
        for name in list(dirs):
            full = os.path.join(root, name)
            if os.path.islink(full):
                raise HarnessError(
                    f"overlay: {os.path.join(rel, name)} is a symlink; "
                    "composed trees take real files only")
        for name in files:
            full = os.path.join(root, name)
            if os.path.islink(full):
                raise HarnessError(
                    f"overlay: {os.path.join(rel, name)} is a symlink; "
                    "composed trees take real files only")
            target = os.path.join(target_root, name)
            if os.path.isdir(target) and not os.path.islink(target):
                raise HarnessError(
                    f"overlay: {os.path.join(rel, name)} is a directory in "
                    "the base and a file in the overlay")
            shutil.copy2(full, target)
            written += 1
    return written


def run_overlay(base: str, overlay: str, command: Sequence[str], *,
                cwd: Optional[str] = None, timeout: Optional[float] = None,
                keep_scratch: bool = False) -> Dict[str, Any]:
    """Compose ``base`` with ``overlay`` in a scratch tree and run ``command``.

    ``command`` is an argv list, never a string. ``cwd`` is interpreted inside
    the composed tree, so a suite that expects to run from a subdirectory can
    say so without naming a path outside the scratch.

    The returned payload is the evidence: the composed paths, the argv, the
    exit code, both streams, and ``ok`` -- true only when the command exited 0
    and nothing timed out. The scratch tree is removed unless
    ``keep_scratch`` is set, in which case its path is reported.
    """
    if isinstance(command, str):
        raise HarnessError(
            "overlay: command must be an argv list, not a string "
            "(a string is a shell waiting to happen)")
    argv = [str(part) for part in (command or ())]
    if not argv:
        raise HarnessError("overlay: command is empty; nothing to run")
    base_abs = _require_tree(base, "base")
    overlay_abs = _require_tree(overlay, "overlay")

    scratch = tempfile.mkdtemp(prefix="harness-overlay-")
    composed = os.path.join(scratch, "tree")
    try:
        shutil.copytree(base_abs, composed)
        files_written = _copy_overlay(overlay_abs, composed)

        inside = composed
        if cwd:
            candidate = os.path.abspath(os.path.join(composed, cwd))
            if os.path.commonpath([composed, candidate]) != composed:
                raise HarnessError(
                    f"overlay: cwd {cwd!r} escapes the composed tree")
            if not os.path.isdir(candidate):
                raise HarnessError(
                    f"overlay: cwd {cwd!r} is not a directory in the "
                    "composed tree")
            inside = candidate

        result: CommandResult = _osal_run(argv, cwd=inside, timeout=timeout)
        # osal reports a timed-out run as 124, the same code a gate runner
        # would see; that is the only signal the record carries.
        timed_out = int(result.returncode) == 124
        payload: Dict[str, Any] = {
            "base": base_abs,
            "overlay": overlay_abs,
            "command": argv,
            "cwd": os.path.relpath(inside, composed),
            "returncode": int(result.returncode),
            "timed_out": timed_out,
            "ok": result.returncode == 0 and not timed_out,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "overlay_files": files_written,
            "scratch": scratch if keep_scratch else None,
        }
        return payload
    finally:
        if not keep_scratch:
            shutil.rmtree(scratch, ignore_errors=True)
