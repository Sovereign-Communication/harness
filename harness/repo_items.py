"""JEV-P6 extract: code-owned whole-repo element inventory ($0 model spend).

Code owns enumeration, sizes, AST symbol records, imports, headings, test and
gate facts, import-graph centrality, and mechanical tallies. Jev only judges
elements against an operator repo-summary pack later
(``JevPolicy.evaluate_repo_summary``, site=repo_summary). Nothing here
invents judgments: every field is measured from the tree, and every string
bound keeps one element's state small enough that a keyed call stays under
the worst-case reserve (JEV_MAX_INPUT_TOKENS).
"""
import ast
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .repo_scope import enumerate_repo_files, gate_for_targets
from .tokens import estimate_prompt_tokens

SUMMARY_MAX_SUMMARY_CHARS = 200
SUMMARY_MAX_SYMBOLS = 18
SUMMARY_MAX_IMPORTS = 10
SUMMARY_MAX_HEADINGS = 12
SUMMARY_MAX_SYMBOL_SIG_CHARS = 90

_KIND_BY_SUFFIX = {
    ".py": "python", ".md": "doc", ".json": "config", ".yml": "config",
    ".yaml": "config", ".toml": "config", ".in": "config", ".txt": "data",
    ".jsonl": "data", ".sql": "data", ".js": "ui", ".css": "ui",
    ".html": "ui",
}
_SUFFIX_BY_SUFFIX = set(_KIND_BY_SUFFIX)
# Extensionless tracked files (LICENSE, .gitignore-style names carry their
# own suffix via Path.suffix == '' only for truly extensionless names).
_NO_SUFFIX_KIND = "doc"


def element_kind(rel_path: str) -> str:
    """Code-owned element kind: pure path/suffix facts, no model involved."""
    posix = str(rel_path).replace("\\", "/")
    parts = posix.split("/")
    suffix = Path(posix).suffix.lower()
    if suffix == ".py":
        if parts and parts[0] in ("tests", "test"):
            return "test"
        if parts and parts[0] == "audits":
            return "audit"
        return "python"
    if suffix in _SUFFIX_BY_SUFFIX:
        kind = _KIND_BY_SUFFIX[suffix]
        if parts and parts[0] == "audits" and kind == "doc":
            return "audit"
        return kind
    if not suffix:
        return _NO_SUFFIX_KIND
    return "other"


def _one_line(text: Any, limit: int) -> str:
    line = " ".join(str(text or "").split())
    return line[:limit]


def _module_summary(source: str, rel_path: str) -> str:
    """First docstring line (py), first heading/text line (text), bounded."""
    if element_kind(rel_path) == "python":
        try:
            doc = ast.get_docstring(ast.parse(source))
        except SyntaxError:
            doc = None
        if doc:
            return _one_line(doc, SUMMARY_MAX_SUMMARY_CHARS)
    for raw in source.splitlines():
        stripped = raw.strip()
        if stripped:
            return _one_line(stripped, SUMMARY_MAX_SUMMARY_CHARS)
    return ""


def _symbol_records(source: str) -> List[Dict[str, Any]]:
    """Structured def/class inventory: name, kind, line, bounded signature."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    out: List[Dict[str, Any]] = []

    def add(node, kind: str, prefix: str = "") -> None:
        try:
            args = ast.unparse(node.args)
        except Exception:
            args = ""
        sig = f"{prefix}{node.name}({args})"
        out.append({
            "name": node.name,
            "kind": kind,
            "line": int(getattr(node, "lineno", 0) or 0),
            "sig": sig[:SUMMARY_MAX_SYMBOL_SIG_CHARS],
        })

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            add(node, "function", "async " if isinstance(node, ast.AsyncFunctionDef) else "")
        elif isinstance(node, ast.ClassDef):
            out.append({
                "name": node.name,
                "kind": "class",
                "line": int(getattr(node, "lineno", 0) or 0),
                "sig": f"class {node.name}"[:SUMMARY_MAX_SYMBOL_SIG_CHARS],
            })
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if item.name.startswith("__") and item.name != "__init__":
                        continue
                    add(item, "method", "")
    return out


def _module_import_roots(source: str) -> List[str]:
    """Full import module names and from-import aliases, de-duplicated.

    Full dotted names (not just roots) let :func:`import_centrality` resolve
    ``from pkg.core import Engine`` onto ``pkg/core.py``; aliases catch
    ``from pkg import helper``. Bounded twice the display cap so ranking
    sees a little more than the Jev state ever shows.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    roots: List[str] = []

    def note(name: Optional[str]) -> None:
        if name and name not in roots:
            roots.append(name)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                note(alias.name)
        elif isinstance(node, ast.ImportFrom):
            note(node.module)
            for alias in node.names:
                note(alias.name)
        if len(roots) >= SUMMARY_MAX_IMPORTS * 2:
            break
    return roots[:SUMMARY_MAX_IMPORTS * 2]


def _headings(source: str) -> List[str]:
    out: List[str] = []
    for raw in source.splitlines():
        stripped = raw.strip()
        if stripped.startswith("#"):
            out.append(_one_line(stripped.lstrip("#"), 70))
        if len(out) >= SUMMARY_MAX_HEADINGS:
            break
    return out


def build_elements(root_dir: Any, rel_paths: Sequence[str]) -> List[Dict[str, Any]]:
    """One inventory row per file: measured facts only."""
    root = Path(root_dir) if root_dir is not None else Path.cwd()
    elements: List[Dict[str, Any]] = []
    for rel in rel_paths:
        path = root / rel
        try:
            raw = path.read_bytes()
            text = raw.decode("utf-8", errors="replace")
        except OSError:
            raw, text = b"", ""
        kind = element_kind(rel)
        is_py = kind in ("python", "test", "audit")
        element: Dict[str, Any] = {
            "id": f"file:{rel}",
            "element_kind": "file",
            "path": rel.replace("\\", "/"),
            "kind": kind,
            "bytes": len(raw),
            "loc": len(text.splitlines()),
            "est_tokens": int(estimate_prompt_tokens(text)) if text else 0,
            "summary": _module_summary(text, rel) if text else "",
            "symbols": _symbol_records(text) if is_py else [],
            "imports": _module_import_roots(text) if is_py else [],
            "headings": _headings(text) if kind in ("doc", "audit") else [],
        }
        element["test"] = _test_counterpart(rel, root) if kind == "python" else (
            kind == "test")
        try:
            element["gate"] = gate_for_targets([rel], root)
        except Exception:
            element["gate"] = None
        elements.append(element)
    return elements


def _test_counterpart(rel: str, root: Path) -> bool:
    stem = Path(rel).stem
    if not stem:
        return False
    return (root / "tests" / f"test_{stem}.py").is_file()


def import_centrality(elements: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    """In-repo import degree per python element (code-owned ranking signal).

    Resolution per imported name: full dotted path first (``pkg.core`` ->
    ``pkg/core.py``), then bare stem, then the name's last segment -- so
    absolute, relative, and aliased imports all land on a real file.
    """
    by_dotted: Dict[str, str] = {}
    by_stem: Dict[str, str] = {}
    for element in elements:
        path = element["path"]
        if element.get("kind") not in ("python", "test", "audit"):
            continue
        if path.endswith(".py"):
            dotted = path[:-3].replace("/", ".")
            by_dotted[dotted] = path
            # package-rooted modules resolve from any prefix depth
            parts = dotted.split(".")
            for index in range(1, len(parts)):
                by_dotted.setdefault(".".join(parts[index:]), path)
        by_stem[Path(path).stem] = path
    degree: Dict[str, int] = {element["path"]: 0 for element in elements}
    for element in elements:
        seen = set()
        for name in element.get("imports") or ():
            target = (by_dotted.get(str(name))
                      or by_stem.get(str(name))
                      or by_dotted.get(str(name).split(".")[-1])
                      or by_stem.get(str(name).split(".")[-1]))
            if target and target != element["path"] and target not in seen:
                seen.add(target)
                degree[target] += 1
    return degree


def mechanical_tallies(elements: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate measured facts; never derived from a model answer."""
    by_kind: Dict[str, int] = {}
    totals = {"files": 0, "bytes": 0, "loc": 0, "est_tokens": 0, "symbols": 0}
    for element in elements:
        kind = element.get("kind") or "other"
        by_kind[kind] = by_kind.get(kind, 0) + 1
        totals["files"] += 1
        totals["bytes"] += int(element.get("bytes") or 0)
        totals["loc"] += int(element.get("loc") or 0)
        totals["est_tokens"] += int(element.get("est_tokens") or 0)
        totals["symbols"] += len(element.get("symbols") or [])
    return {
        "totals": totals,
        "by_kind": dict(sorted(by_kind.items())),
    }


def element_state(element: Dict[str, Any]) -> Dict[str, Any]:
    """The compact state one Jev call sees for a file element (bounded)."""
    return {
        "path": element.get("path"),
        "kind": element.get("kind"),
        "loc": int(element.get("loc") or 0),
        "est_tokens": int(element.get("est_tokens") or 0),
        "summary": element.get("summary") or "",
        "symbols": [s.get("sig") for s in (element.get("symbols") or [])][:SUMMARY_MAX_SYMBOLS],
        "imports": list(element.get("imports") or [])[:SUMMARY_MAX_IMPORTS],
        "headings": list(element.get("headings") or [])[:SUMMARY_MAX_HEADINGS],
        "test": bool(element.get("test")),
        "gate": element.get("gate"),
    }


def rank_symbol_elements(elements: Sequence[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    """Symbol elements ordered by import centrality, then size, then name.

    ``limit`` keeps the keyed symbol pass inside the operator run budget;
    files are judged first by the driver, symbols consume what remains.
    """
    if limit <= 0:
        return []
    degree = import_centrality(elements)
    rows: List[Dict[str, Any]] = []
    for element in elements:
        if element.get("kind") not in ("python", "audit"):
            continue
        degree_value = int(degree.get(element["path"], 0))
        for symbol in element.get("symbols") or ():
            rows.append({
                "id": f"symbol:{element['path']}#{symbol.get('name')}",
                "element_kind": "symbol",
                "path": element.get("path"),
                "kind": symbol.get("kind") or "function",
                "name": symbol.get("name"),
                "sig": symbol.get("sig"),
                "line": int(symbol.get("line") or 0),
                "module_summary": element.get("summary") or "",
                "module_degree": degree_value,
                "module_loc": int(element.get("loc") or 0),
            })
    rows.sort(key=lambda r: (-r["module_degree"], -r["module_loc"],
                             r["path"], r["line"], r["name"] or ""))
    return rows[:limit]


def symbol_state(symbol_element: Dict[str, Any]) -> Dict[str, Any]:
    """The compact state one Jev call sees for a symbol element (bounded)."""
    return {
        "path": symbol_element.get("path"),
        "element_kind": "symbol",
        "symbol": symbol_element.get("sig") or symbol_element.get("name"),
        "symbol_kind": symbol_element.get("kind"),
        "module_summary": _one_line(symbol_element.get("module_summary"), 160),
        "kind": "python",
    }


def summary_listing(root_dir: Any = None, *, limit: Optional[int] = None) -> List[str]:
    """Every soft-skip-included file in the tree (JEV-P6 inventory scope)."""
    return enumerate_repo_files(
        Path(root_dir) if root_dir is not None else None,
        limit=limit if limit is not None else 100000,
        include_soft_skipped=True,
    )
