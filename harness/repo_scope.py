"""File-scope discovery helpers (single owner).

Moved verbatim from harness/agent.py: _REPO_SKIP_DIRS,
_REPO_SKIP_SUFFIXES, discover_target_files, enumerate_repo_files,
discover_verification_gate. Bodies are copied verbatim; only the
module location changed.
"""
import os
import re
from pathlib import Path
from typing import List, Optional, Sequence

def discover_target_files(prompt: str, root_dir: Optional[Path] = None) -> List[str]:
    # Autonomously identify candidate target files from prompt or repository
    root = root_dir or Path.cwd()
    candidates: List[str] = []

    # 1. Regex search for explicit filenames in prompt
    matches = re.findall(r"\b[a-zA-Z0-9_./-]+\.(?:py|rs|go|ts|js|md|json|toml|yaml|yml|c|cpp|h)\b", prompt)
    for m in matches:
        clean_path = m.strip("`'\" \t\r\n")
        full_path = root / clean_path
        if full_path.exists() and clean_path not in candidates:
            candidates.append(clean_path.replace("\\", "/"))

    if candidates:
        return candidates

    # 2. Heuristic search: check if prompt mentions existing python module names
    words = set(re.findall(r"\b[a-zA-Z_0-9-]+\b", prompt.lower()))
    harness_dir = root / "harness"
    if harness_dir.is_dir():
        for p in sorted(harness_dir.glob("*.py")):
            stem = p.stem.lower()
            if stem in words and stem != "__init__":
                rel = p.relative_to(root).as_posix()
                if rel not in candidates:
                    candidates.append(rel)

    return candidates


_REPO_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
                   "dist", "build", "audits", ".ruff_cache", "chat_history"}
_REPO_SKIP_SUFFIXES = {".pyc", ".pyo", ".log", ".lock", ".jsonl"}


def enumerate_repo_files(root_dir: Optional[Path] = None, limit: int = 300) -> List[str]:
    """The whole-repo listing for the relevance first pass (bounded, junk-free)."""
    root = (root_dir or Path.cwd()).resolve()
    files: List[str] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if any(part in _REPO_SKIP_DIRS for part in p.parts):
            continue
        if p.suffix in _REPO_SKIP_SUFFIXES:
            continue
        files.append(p.relative_to(root).as_posix())
        if len(files) >= limit:
            break
    return files


def discover_verification_gate(target_files: Sequence[str], root_dir=None) -> Optional[str]:
    # Autonomously resolve a verification gate command for targeted files.
    # ``root_dir`` arrives as a Path from callers that hold one and as a str
    # from lanes whose plan carries its root verbatim, so it is coerced here
    # (the owner) instead of at every call site.
    root = Path(root_dir) if root_dir else Path.cwd()
    if not target_files:
        return None

    # Check primary target file
    primary = str(target_files[0]).replace("\\", "/")
    if primary.startswith("harness/") and primary.endswith(".py"):
        mod_name = Path(primary).stem
        test_path = root / "tests" / f"test_{mod_name}.py"
        if test_path.exists():
            # Absolute: the gate runner's CWD is the server's, not the
            # agent's chosen root -- a relative path compiles/tests the
            # wrong tree (or nothing) for GUI runs with a workDir.
            return f"python -m unittest {test_path}"

    # Generic check: syntax compile
    if primary.endswith(".py") and (root / primary).exists():
        return f"python -m py_compile \"{root / primary}\""

    return None


def gate_for_targets(target_files: Sequence[str],
                     root_dir: Optional[Path] = None,
                     declared: Optional[str] = None,
                     run_gate: Optional[str] = None) -> Optional[str]:
    """ONE owner of the rule that gives a work item its verification gate.

    Precedence (the rule the agent lane used to own privately): the item's
    own declared gate wins; else a gate derived from its own targets (a
    test module, or a compile check); else the caller's run-level gate;
    else -- for a file nothing can be derived from yet, such as one the
    item CREATES -- its own compile check.

    The plan lane assigns this at plan time (:func:`harness.waist.plan_task`),
    so every lane dispatches gated nodes and no lane re-derives the rule. A
    gateless write is refused at mutation time at unknown trust, so losing
    this rule costs the write -- it never silently writes unverified.
    """
    root = Path(root_dir) if root_dir else Path.cwd()
    targets = [str(t) for t in (target_files or []) if t]
    gate = str(declared).strip() if declared else None
    if not gate:
        gate = discover_verification_gate(targets, root)
    gate = gate or run_gate
    if not gate:
        primary = targets[0] if targets else None
        if primary and primary.endswith(".py"):
            gate = f'python -m py_compile "{root / primary}"'
    return gate


def _rebase_path(path, from_root, to_root) -> str:
    """Map a planned absolute/relative target into the execution checkout."""
    raw = str(path)
    source = Path(from_root).resolve() if from_root else Path.cwd().resolve()
    destination = Path(to_root).resolve() if to_root else source
    candidate = Path(raw)
    if candidate.is_absolute():
        try:
            return str(destination / candidate.relative_to(source))
        except ValueError:
            return raw
    return str(destination / candidate)


def rebase_gate(gate: Optional[str], from_root, to_root) -> Optional[str]:
    """Re-root a gate command's tree paths into the checkout it runs in.

    A planned node's gate is derived against the tree the plan was made in,
    but an isolated node executes inside a git worktree. Replace the source
    root exactly once, using the path spelling that occurs in the command;
    applying multiple slash variants to the already-rebased result would
    recursively replace the source prefix and produce a bogus doubled path.
    """
    if not gate or not from_root or not to_root:
        return gate
    raw_source = str(from_root).rstrip("\\/")
    raw_destination = str(to_root).rstrip("\\/")
    if os.path.normcase(raw_source) == os.path.normcase(raw_destination):
        return gate
    variants = []
    for old, new in (
        (raw_source, raw_destination),
        (raw_source.replace("\\", "/"), raw_destination.replace("\\", "/")),
        (raw_source.replace("/", "\\"), raw_destination.replace("/", "\\")),
    ):
        if (old, new) not in variants:
            variants.append((old, new))
    for old, new in variants:
        if old and old in gate:
            return gate.replace(old, new)
    return gate
