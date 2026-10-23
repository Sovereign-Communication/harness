"""Source identity and changed-line coverage for the traced battery (no models)."""
import ast
import hashlib
from pathlib import Path
import subprocess


def source_hashes(root):
    root = Path(root)
    files = set()
    for folder in ("harness", "driver_core", "tests", "packs", "docs", "examples"):
        files.update(p for p in (root / folder).rglob("*")
                     if p.is_file() and p.suffix in (".py", ".json", ".md", ".js", ".html"))
    files.update((root / "audits" / "self").glob("*.py"))
    files.update((root / "audits" / "self").glob("*.cjs"))
    files.add(root / "pyproject.toml")
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(files)}


def validate_trace(doc, root):
    """Old or content-mismatched hits are not proof of current execution."""
    if doc.get("schema") != "harness-coverage-v2":
        raise ValueError("stale trace lacks measured source-content provenance")
    execution = doc.get("execution", {})
    if len(execution.get("head", "")) != 40 or not execution.get("tree"):
        raise ValueError("trace lacks measured execution revision")
    if not doc.get("suite", {}).get("successful") or doc.get("isolation", {}).get("violations"):
        raise ValueError("trace did not pass the provider-free suite")
    measured = doc.get("source_hashes", {})
    current = source_hashes(root)
    if not measured or measured != current:
        changed = sorted(k for k in measured.keys() | current.keys()
                         if measured.get(k) != current.get(k))
        raise ValueError("stale measured source content: " + ", ".join(changed[:8]))
    if not isinstance(doc.get("modules"), dict):
        raise ValueError("trace lacks executed module lines")
    return True


def changed_line_coverage(root, diff_base, modules):
    """Use current AST statement lines and a meaningful independently pinned base."""
    root = Path(root)
    diff = subprocess.run(
        ["git", "diff", "--no-ext-diff", "--no-textconv", "--unified=0", diff_base,
         "--", "harness/"], cwd=str(root), capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=True)
    added, rel, new_line = {}, None, 0
    for line in diff.stdout.splitlines():
        if line.startswith("+++ b/"):
            rel = line[6:]
        elif line.startswith("@@"):
            new_line = int(line.split()[2][1:].split(",")[0])
        elif rel is not None and line.startswith("+") and not line.startswith("+++"):
            added.setdefault(rel, set()).add(new_line)
            new_line += 1
        elif rel is not None and line.startswith(" "):
            new_line += 1
    checked, executed, gaps, statements = 0, 0, {}, {}
    for rel, lines in sorted(added.items()):
        tree = ast.parse((root / rel).read_text(encoding="utf-8"))
        stmts = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.stmt):
                continue
            if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)):
                continue
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                first = node.body[0]
                if not (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                        and isinstance(first.value.value, str)):
                    stmts.add(first.lineno)
            else:
                stmts.add(node.lineno)
        selected = sorted(lines & stmts)
        statements[rel] = selected
        hits = set(modules.get(rel, []))
        checked += len(selected)
        executed += len(set(selected) & hits)
        missing = sorted(set(selected) - hits)
        if missing:
            gaps[rel] = missing
    return {"diff_base": diff_base, "checked": checked, "executed": executed,
            "ratio": executed / checked if checked else None,
            "gaps": gaps, "statements": statements, "bar": 0.95,
            "bar_met": bool(checked and executed / checked >= 0.95)}
