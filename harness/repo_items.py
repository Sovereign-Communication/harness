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
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .repo_scope import enumerate_repo_files, gate_for_targets
from .tokens import estimate_prompt_tokens

SUMMARY_MAX_SUMMARY_CHARS = 200
SUMMARY_MAX_SYMBOLS = 10
SUMMARY_MAX_IMPORTS = 10
SUMMARY_MAX_HEADINGS = 12
SUMMARY_MAX_SYMBOL_SIG_CHARS = 90
# Condensed state caps (JEV-P6): the keyed call sees short sigs only --
# extraction keeps the full lists for centrality ranking and tallies.
SUMMARY_STATE_SIG_CHARS = 60
# Kind-aware condensed-state caps (JEV-P6): docs carry structure, config
# carries keys, unsaturated python carries import roots -- every optional
# field exists only when its signal exists, so no state pays for noise.
STATE_HEADING_LIMIT = 4
STATE_KEY_LIMIT = 6
STATE_IMPORT_LIMIT = 3
# The construction bound every state respects; pinned by test so the
# priced seat never sees an unbounded payload.
STATE_CHAR_BUDGET = 1400

_KIND_BY_SUFFIX = {
    ".py": "python", ".md": "doc", ".json": "config", ".yml": "config",
    ".yaml": "config", ".toml": "config", ".in": "config", ".txt": "data",
    ".jsonl": "data", ".sql": "data", ".js": "ui", ".css": "ui",
    ".html": "ui",
}
_SUFFIX_BY_SUFFIX = set(_KIND_BY_SUFFIX)
# Extensionless tracked files (LICENSE-style names) are docs; these known
# basenames are configuration despite having no suffix (Path.suffix == '').
_NO_SUFFIX_KIND = "doc"
_EXTENSIONLESS_CONFIG = frozenset((
    ".gitignore", ".gitattributes", ".editorconfig", ".flake8",
    ".npmrc", ".nvmrc", ".python-version", ".env",
    "makefile", "gnumakefile", "dockerfile", "procfile",
))


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
        name = posix.rsplit("/", 1)[-1].lower()
        return "config" if name in _EXTENSIONLESS_CONFIG else _NO_SUFFIX_KIND
    return "other"


def _one_line(text: Any, limit: int) -> str:
    """Whitespace-collapsed single line, visibly clipped at ``limit``."""
    line = " ".join(str(text or "").split())
    return _clip(line, limit)


def _clip(text: str, limit: int) -> str:
    """Bounded text with an ellipsis so truncation is visible, never silent."""
    return text if len(text) <= limit else text[: max(limit - 1, 0)] + "…"


_NOISE_LINE_PREFIXES = ("{", "}", "<!", "<?", "---", "/*",
                        "*/", "``")
_BRACKET_ONLY_RE = re.compile(r"\[[^\]]{1,60}\]")
_TAG_ONLY_RE = re.compile(r"</?[A-Za-z][A-Za-z0-9-]*(\s[^<>]*)?/?>\s*$")
_TITLE_RE = re.compile(r"<title>([^<]{2,120})</title>",
                       re.IGNORECASE | re.DOTALL)


def _is_noise_line(stripped: str, suffix: str = "") -> bool:
    """Structural/punctuation lines that carry no semantic signal.

    Bracketed log prefixes (``[OK] ...``) are signal, not noise -- only a
    line that is nothing but a bracketed token (TOML section, array item)
    qualifies. A CSS block opener is structural; a JS function head is not.
    """
    if not stripped:
        return True
    if stripped.startswith(_NOISE_LINE_PREFIXES):
        return True
    if _TAG_ONLY_RE.fullmatch(stripped) or _BRACKET_ONLY_RE.fullmatch(stripped):
        return True
    if suffix == ".css" and stripped.endswith("{"):
        return True
    if not any(ch.isalnum() for ch in stripped):
        return True
    return False


def _strip_frontmatter(lines: List[str]) -> List[str]:
    """Drop a leading YAML frontmatter block (``--- ... ---``) if present."""
    if lines and lines[0].strip() == "---":
        for index in range(1, min(len(lines), 40)):
            if lines[index].strip() == "---":
                return lines[index + 1:]
    return lines


def _module_summary(source: str, rel_path: str) -> str:
    """Best bounded one-line summary, noise-stripped (JEV-P6 condense).

    Python sources: module docstring first (any ``.py`` -- tests and
    audits included, not just kind=python). HTML: the ``<title>`` inner
    text (the page's actual name). Everything else (and the python
    fallback): first non-noise line after frontmatter, with ATX heading
    markers stripped -- never ``{``, ``<!DOCTYPE``, or ``---``.
    An empty/whitespace-only source is labeled, never an unlabeled ``""``.
    """
    if not source.strip():
        return "(empty file)"
    suffix = Path(rel_path).suffix.lower()
    if suffix == ".py":
        try:
            doc = ast.get_docstring(ast.parse(source))
        except SyntaxError:
            doc = None
        if doc:
            return _one_line(doc, SUMMARY_MAX_SUMMARY_CHARS)
    if suffix in (".html", ".htm"):
        title = _TITLE_RE.search(source)
        if title:
            return _one_line(title.group(1), SUMMARY_MAX_SUMMARY_CHARS)
    lines = _strip_frontmatter(source.splitlines())
    for raw in lines[:40]:
        stripped = raw.strip()
        if stripped.startswith("#") and not stripped.startswith("#!"):
            stripped = stripped.lstrip("#").strip()
        if _is_noise_line(stripped, suffix):
            continue
        return _one_line(stripped, SUMMARY_MAX_SUMMARY_CHARS)
    return ""


_KEY_SUFFIXES = frozenset((".json", ".jsonl", ".yml", ".yaml", ".toml"))
_KEY_LINE_RE = re.compile(
    r"^[\{\[]?\s*\"?([A-Za-z_][\w.\-/ ]{0,39})\"?\s*[:=]")
_SECTION_RE = re.compile(r"^\[([A-Za-z_][\w.\-]{0,39})\]\s*$")


def config_keys(source: str, rel_path: str) -> List[str]:
    """Top-level key/section hints for structured config and data files.

    Mechanical only: strict JSON parse when possible, then line-anchored
    regex hints (YAML, TOML sections, JSONL records, malformed JSON).
    Never raises; bounded to ``STATE_KEY_LIMIT``.
    """
    if Path(rel_path).suffix.lower() not in _KEY_SUFFIXES:
        return []
    keys: List[str] = []
    if Path(rel_path).suffix.lower() == ".json" and len(source) <= 400_000:
        try:
            doc = json.loads(source)
        except Exception:
            doc = None
        if isinstance(doc, dict):
            keys = [str(k)[:40] for k in list(doc)[:STATE_KEY_LIMIT]]
    if Path(rel_path).suffix.lower() == ".jsonl":
        for raw in source.splitlines()[:5]:
            try:
                record = json.loads(raw)
            except Exception:
                continue
            if isinstance(record, dict):
                keys = [str(k)[:40] for k in list(record)[:STATE_KEY_LIMIT]]
                break
    if keys:
        return keys[:STATE_KEY_LIMIT]
    seen: List[str] = []
    for raw in source.splitlines()[:200]:
        line = raw.strip()
        match = _SECTION_RE.match(line) or _KEY_LINE_RE.match(line)
        if match:
            key = match.group(1).strip()
            if key and key not in seen:
                seen.append(key)
        if len(seen) >= STATE_KEY_LIMIT:
            break
    return seen


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
            "sig": _clip(sig, SUMMARY_MAX_SYMBOL_SIG_CHARS),
        })

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            add(node, "function", "async " if isinstance(node, ast.AsyncFunctionDef) else "")
        elif isinstance(node, ast.ClassDef):
            out.append({
                "name": node.name,
                "kind": "class",
                "line": int(getattr(node, "lineno", 0) or 0),
                "sig": _clip(f"class {node.name}", SUMMARY_MAX_SYMBOL_SIG_CHARS),
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
        read_ok = True
        try:
            raw = path.read_bytes()
            text = raw.decode("utf-8", errors="replace")
        except OSError:
            raw, text, read_ok = b"", "", False
        # A UTF-8 BOM would masquerade as content (and defeat the `#`
        # heading / `---` frontmatter checks on line 1): strip it at source.
        text = text.lstrip("\ufeff")
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
            # Unreadable never masquerades as empty: distinct honest labels.
            "summary": _module_summary(text, rel) if read_ok else "(unreadable)",
            "symbols": _symbol_records(text) if is_py else [],
            "imports": _module_import_roots(text) if is_py else [],
            "headings": _headings(text) if kind in ("doc", "audit") else [],
            "keys": config_keys(text, rel),
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
    """The condensed state one Jev call sees for a file element (bounded).

    Kind-aware signal within a construction budget (``STATE_CHAR_BUDGET``):
    every element carries path/kind/size/summary/symbols; docs add their
    headings, structured config/data adds key hints, and python sources
    add capped import roots (the coupling signal).
    ``gate`` and other code-owned inventory facts never ship -- the
    hourglass condenses before it dispatches.
    """
    symbols = element.get("symbols") or []
    headings = element.get("headings") or []
    keys = element.get("keys") or []
    imports = element.get("imports") or []
    state: Dict[str, Any] = {
        "path": element.get("path"),
        "kind": element.get("kind"),
        "loc": int(element.get("loc") or 0),
        "est_tokens": int(element.get("est_tokens") or 0),
        "summary": element.get("summary") or "",
        "symbols": [_clip(str(s.get("sig") or ""), SUMMARY_STATE_SIG_CHARS)
                    for s in symbols][:SUMMARY_MAX_SYMBOLS],
        "symbol_count": len(symbols),
        "test": bool(element.get("test")),
    }
    if headings:
        state["headings"] = list(headings)[:STATE_HEADING_LIMIT]
    elif keys:
        state["keys"] = list(keys)[:STATE_KEY_LIMIT]
    if imports:
        # Import roots stay even when the symbol inventory saturates: they
        # are the module's coupling signal (fidelity probe 2026-09-22:
        # dropping them flipped stage on harness/jev_policy.py).
        state["imports"] = list(imports)[:STATE_IMPORT_LIMIT]
    return state


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
