"""Opt-in resumable full-source Jev audit (issue #423).

The audit owns deterministic Git-backed inventory, safe exclusions, chunk
identity, resume receipts, and aggregation. Judgments always go through the
shared JEV-P6 policy owner; this module never creates a Jev client and never
stores source text in its state or report.
"""
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .errors import HarnessError
from .jev import jev_cost
from .jev_packs import validate_repo_summary_pack
from .jev_policy import JEV_MAX_INPUT_TOKENS
from .workspace import is_sensitive_path

SCHEMA = "source-audit-v1"
CHUNK_ALGORITHM = "utf8-lines-greedy-v1"
DEFAULT_CHUNK_CHARS = 4000
MIN_CHUNK_CHARS = 512
MAX_CHUNK_CHARS = 8000
MAX_FILE_BYTES = 64 * 1024 * 1024
DISPOSITIONS = frozenset(("verified", "false_positive",
                          "historical_fixture", "unresolved"))

_CODE_SUFFIXES = frozenset((
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".rs", ".go",
    ".java", ".c", ".h", ".cc", ".cpp", ".hpp", ".rb", ".php",
    ".sh", ".ps1", ".bat", ".sql", ".css", ".toml", ".ini", ".cfg",
    ".conf", ".yaml", ".yml", ".json", ".jsonc", ".xml", ".gradle",
))
_DOC_SUFFIXES = frozenset((".md", ".rst", ".adoc", ".txt"))
_EVIDENCE_SUFFIXES = frozenset((".jsonl", ".ndjson", ".csv", ".tsv"))
_BINARY_SUFFIXES = frozenset((
    ".7z", ".a", ".bin", ".class", ".db", ".dll", ".doc", ".docx",
    ".dylib", ".exe", ".gif", ".gz", ".ico", ".jar", ".jpeg", ".jpg",
    ".lib", ".mp3", ".mp4", ".o", ".pdf", ".png", ".pyc", ".pyd",
    ".so", ".sqlite", ".sqlite3", ".tar", ".wasm", ".webp", ".whl",
    ".zip",
))
_GENERATED_DIRS = frozenset((
    ".cache", ".harness", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".tox", ".venv", "__pycache__", "artifacts", "build", "coverage",
    "dist", "generated", "node_modules", "site-packages", "venv",
))
_GENERATED_SUFFIXES = frozenset((".pyc", ".pyo", ".lock", ".log"))
_CONFIG_BASENAMES = frozenset((
    "dockerfile", "makefile", "gnumakefile", "procfile", ".gitignore",
    ".gitattributes", ".editorconfig", ".dockerignore", "requirements.txt",
))
_SECRET_ASSIGNMENT = re.compile(
    r"(?im)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|"
    r"client[_-]?secret|password|passwd|private[_-]?key|secret|token)\b"
    r"\s*[:=]\s*[\"']?([^\s\"'`,;]{4,})")
_SECRET_TOKEN_PATTERNS = (
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|"
               r"github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|"
               r"AIza[0-9A-Za-z_-]{30,}|xox[baprs]-[A-Za-z0-9-]{10,})\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{8,}\b"),
)
_SECRET_PLACEHOLDERS = frozenset((
    "changeme", "example", "false", "none", "null", "redacted", "test",
    "true", "your_api_key", "your-api-key", "your_token", "your-token",
))


class SourceAuditError(HarnessError):
    """A deterministic or safety prerequisite for a source audit failed."""


def _canonical_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _git(root: Path, *args: str, input_bytes: Optional[bytes] = None) -> bytes:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(root), input=input_bytes,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    except OSError as exc:
        raise SourceAuditError("source audit requires an available Git executable") from exc
    if proc.returncode != 0:
        raise SourceAuditError("source audit requires a valid Git repository snapshot")
    return proc.stdout


def _nul_paths(raw: bytes) -> List[str]:
    return [os.fsdecode(value) for value in raw.split(b"\0") if value]


def _is_link_or_reparse(path: Path) -> bool:
    """Inspect the directory entry itself, including dangling links."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise SourceAuditError("cannot safely inspect source audit output path") from exc
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & 0x400)


def _repo_snapshot(root: Path) -> Tuple[str, List[str], set]:
    try:
        top = Path(os.fsdecode(_git(root, "rev-parse", "--show-toplevel").strip()))
        commit = os.fsdecode(_git(root, "rev-parse", "HEAD").strip())
    except OSError as exc:
        raise SourceAuditError("cannot read the Git repository snapshot") from exc
    if top.resolve() != root.resolve():
        raise SourceAuditError("source audit root must be the Git repository root")
    tracked_and_unignored = _nul_paths(_git(
        root, "ls-files", "--cached", "--others", "--exclude-standard", "-z"))
    ignored_untracked = set(_nul_paths(_git(
        root, "ls-files", "--others", "--ignored", "--exclude-standard", "-z")))
    if tracked_and_unignored:
        payload = b"\0".join(os.fsencode(path) for path in tracked_and_unignored) + b"\0"
        try:
            proc = subprocess.run(
                ["git", "check-ignore", "--no-index", "--stdin", "-z"],
                cwd=str(root), input=payload, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, check=False)
        except OSError as exc:
            raise SourceAuditError(
                "source audit requires an available Git executable") from exc
        if proc.returncode not in (0, 1):
            raise SourceAuditError("cannot read Git ignored-path rules")
        ignored_tracked = set(_nul_paths(proc.stdout))
    else:
        ignored_tracked = set()
    return commit, sorted(set(tracked_and_unignored) | ignored_untracked,
                          key=lambda item: (item.casefold(), item)), (
                              ignored_untracked | ignored_tracked)


def _relative_output(root: Path, raw: Optional[Any]) -> Optional[str]:
    if not raw:
        return None
    path = Path(raw)
    candidate = path if path.is_absolute() else root / path
    if _is_link_or_reparse(candidate):
        raise SourceAuditError(
            "source audit output paths cannot be symlinks or reparse points")
    resolved_parent = candidate.parent.resolve()
    try:
        rel_parent = resolved_parent.relative_to(root)
    except ValueError as exc:
        raise SourceAuditError("source audit state and report paths must stay inside the repository") from exc
    rel = (rel_parent / candidate.name).as_posix()
    if rel == ".." or rel.startswith("../"):
        raise SourceAuditError("source audit output paths must stay inside the repository")
    return rel


def _pass_for(rel_path: str) -> Optional[str]:
    path = Path(rel_path)
    suffix = path.suffix.casefold()
    parts = [part.casefold() for part in path.parts]
    basename = path.name.casefold()
    if suffix in _EVIDENCE_SUFFIXES or (
            suffix in (".json", ".yaml", ".yml", ".toml")
            and any(part in ("data", "evidence", "fixtures") for part in parts)):
        return "evidence"
    if suffix in _DOC_SUFFIXES:
        return "docs"
    if suffix in _CODE_SUFFIXES or basename in _CONFIG_BASENAMES:
        return "code"
    return None


def _exclusion_reason(rel_path: str, path: Path, ignored: set,
                      explicit_excludes: set) -> Optional[str]:
    if rel_path in explicit_excludes:
        return "audit_output_or_state"
    if is_sensitive_path(rel_path):
        return "sensitive_path"
    parts = [part.casefold() for part in Path(rel_path).parts]
    if any(part in _GENERATED_DIRS for part in parts):
        return "generated_artifact"
    if Path(rel_path).suffix.casefold() in _GENERATED_SUFFIXES:
        return "generated_artifact"
    if rel_path in ignored:
        return "git_ignored"
    try:
        info = path.lstat()
    except OSError:
        return "unavailable_path"
    if stat.S_ISLNK(info.st_mode) or bool(
            getattr(info, "st_file_attributes", 0) & 0x400):
        return "link_or_reparse_point"
    if not stat.S_ISREG(info.st_mode):
        return "non_regular_file"
    if info.st_size > MAX_FILE_BYTES:
        return "file_too_large"
    if Path(rel_path).suffix.casefold() in _BINARY_SUFFIXES:
        return "binary_file"
    return None


def _read_text(path: Path) -> Tuple[bytes, Optional[str]]:
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
    except OSError:
        return b"", "unreadable_file"
    if len(data) > MAX_FILE_BYTES:
        return b"", "file_too_large"
    if b"\0" in data:
        return data, "binary_file"
    try:
        data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return data, "non_utf8_file"
    return data, None


def _contains_credentials(text: str) -> bool:
    """Conservatively exclude obvious embedded credential values, without echoing them."""
    if any(pattern.search(text) for pattern in _SECRET_TOKEN_PATTERNS):
        return True
    for match in _SECRET_ASSIGNMENT.finditer(text):
        value = match.group(1).strip().strip("\"'").casefold()
        if value in _SECRET_PLACEHOLDERS:
            continue
        if value.startswith(("${", "os.getenv(", "os.environ", "getenv(")):
            continue
        return True
    return False


def _iter_text_chunks(text: str, max_chars: int) -> Iterable[Tuple[str, int, int]]:
    """Greedy line-preserving UTF-8 text chunks with one-based line ranges."""
    lines = text.splitlines(keepends=True)
    if not lines:
        return
    buffer: List[str] = []
    buffer_chars = 0
    buffer_start = 1
    for line_number, original_line in enumerate(lines, 1):
        line = original_line
        if buffer and buffer_chars + len(line) > max_chars:
            yield "".join(buffer), buffer_start, line_number - 1
            buffer, buffer_chars, buffer_start = [], 0, line_number
        while len(line) > max_chars:
            if buffer:
                yield "".join(buffer), buffer_start, line_number
                buffer, buffer_chars = [], 0
            yield line[:max_chars], line_number, line_number
            line = line[max_chars:]
        if line:
            if not buffer:
                buffer_start = line_number
            buffer.append(line)
            buffer_chars += len(line)
    if buffer:
        yield "".join(buffer), buffer_start, len(lines)


def _chunk_id(pass_name: str, path: str, source_hash: str,
              chunk_hash: str, line_start: int, line_end: int,
              pack_hash: str, max_chars: int) -> str:
    return _canonical_hash({
        "algorithm": CHUNK_ALGORITHM, "pass": pass_name, "path": path,
        "source_hash": source_hash, "chunk_hash": chunk_hash,
        "line_start": line_start, "line_end": line_end,
        "pack_hash": pack_hash, "max_chars": max_chars,
    })


def _prepared_packs(packs: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    prepared = {}
    for pass_name in ("code", "docs", "evidence"):
        raw = packs.get(pass_name)
        if raw is None:
            continue
        try:
            doc = validate_repo_summary_pack(raw)
        except ValueError as exc:
            raise SourceAuditError("invalid {} pass pack: {}".format(pass_name, exc)) from exc
        try:
            pack_source = json.dumps(raw, sort_keys=True, ensure_ascii=True)
            pack_hash = _canonical_hash(raw)
        except (TypeError, ValueError) as exc:
            raise SourceAuditError("{} pass pack is not canonical JSON".format(pass_name)) from exc
        if _contains_credentials(pack_source):
            raise SourceAuditError(
                "{} pass pack contains credential-like content".format(pass_name))
        version = raw.get("version", raw.get("pack_version")) if isinstance(raw, dict) else None
        prepared[pass_name] = {
            "document": doc,
            "pack_id": doc["id"],
            "pack_hash": pack_hash,
            "pack_version": str(version) if version is not None else pack_hash,
        }
    if set(prepared) != {"code", "docs", "evidence"}:
        raise SourceAuditError(
            "source audit requires operator packs for code, docs, and evidence passes")
    return prepared


def build_manifest(root: Any, packs: Mapping[str, Any], *,
                   chunk_chars: int = DEFAULT_CHUNK_CHARS,
                   exclude: Optional[Sequence[Any]] = None) -> Dict[str, Any]:
    """Build a complete deterministic, Git-backed included/excluded manifest.

    Excluded sensitive/generated/ignored/binary files are classified before
    their contents are opened. Included source is hashed and counted only.
    """
    root_path = Path(root).resolve(strict=True)
    if not root_path.is_dir():
        raise SourceAuditError("source audit root must be a directory")
    if isinstance(chunk_chars, bool) or not isinstance(chunk_chars, int) or not (
            MIN_CHUNK_CHARS <= chunk_chars <= MAX_CHUNK_CHARS):
        raise SourceAuditError("chunk_chars must be between 512 and 8000")
    prepared = _prepared_packs(packs)
    commit, paths, ignored = _repo_snapshot(root_path)
    explicit_excludes = set()
    for raw in exclude or ():
        rel = _relative_output(root_path, raw)
        if rel:
            explicit_excludes.add(rel)

    entries = []
    for rel_path in paths:
        normalized = rel_path.replace("\\", "/")
        target = root_path / Path(rel_path)
        reason = _exclusion_reason(normalized, target, ignored, explicit_excludes)
        if reason:
            entries.append({"path": normalized, "included": False,
                            "pass": None, "reason": reason})
            continue
        pass_name = _pass_for(normalized)
        if pass_name is None:
            entries.append({"path": normalized, "included": False,
                            "pass": None, "reason": "unsupported_file_type"})
            continue
        if pass_name not in prepared:
            entries.append({"path": normalized, "included": False,
                            "pass": None, "reason": "pass_not_requested"})
            continue
        raw_bytes, content_reason = _read_text(target)
        if content_reason:
            entries.append({"path": normalized, "included": False,
                            "pass": None, "reason": content_reason})
            continue
        text = raw_bytes.decode("utf-8", errors="strict")
        if _contains_credentials(text):
            entries.append({"path": normalized, "included": False,
                            "pass": None, "reason": "credential_content"})
            continue
        chunks = list(_iter_text_chunks(text, chunk_chars))
        entries.append({
            "path": normalized,
            "included": True,
            "pass": pass_name,
            "size_bytes": len(raw_bytes),
            "source_hash": hashlib.sha256(raw_bytes).hexdigest(),
            "chunk_count": len(chunks),
            "pack_id": prepared[pass_name]["pack_id"],
            "pack_hash": prepared[pass_name]["pack_hash"],
        })
    manifest_core = {
        "schema": SCHEMA,
        "commit": commit,
        "root_name": root_path.name,
        "chunking": {"algorithm": CHUNK_ALGORITHM, "max_chars": chunk_chars},
        "passes": {name: {"pack_id": row["pack_id"],
                           "pack_hash": row["pack_hash"],
                           "pack_version": row["pack_version"]}
                   for name, row in sorted(prepared.items())},
        "files": entries,
    }
    manifest_core["manifest_hash"] = _canonical_hash(manifest_core)
    return manifest_core


def _open_state(state_path: Path) -> sqlite3.Connection:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    if _is_link_or_reparse(state_path):
        raise SourceAuditError(
            "source audit state path cannot be a symlink or reparse point")
    try:
        connection = sqlite3.connect(str(state_path), timeout=30)
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS chunks ("
            "chunk_id TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT NOT NULL)")
        connection.commit()
        return connection
    except sqlite3.Error as exc:
        raise SourceAuditError("cannot open source audit resume state") from exc


def _state_get(db: sqlite3.Connection, chunk_id: str) -> Optional[Dict[str, Any]]:
    row = db.execute("SELECT status, payload FROM chunks WHERE chunk_id=?",
                     (chunk_id,)).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row[1])
    except (TypeError, ValueError) as exc:
        raise SourceAuditError("source audit resume state is corrupt") from exc
    if not isinstance(payload, dict):
        raise SourceAuditError("source audit resume state is corrupt")
    payload["status"] = row[0]
    return payload


def _state_claim(db: sqlite3.Connection, chunk_id: str,
                 identity: Dict[str, Any]) -> Tuple[str, Dict[str, Any], bool]:
    try:
        db.execute("BEGIN IMMEDIATE")
        inserted = db.execute(
            "INSERT OR IGNORE INTO chunks(chunk_id,status,payload) "
            "VALUES(?, 'in_flight', ?)",
            (chunk_id, json.dumps(identity, sort_keys=True))).rowcount == 1
        row = db.execute("SELECT status,payload FROM chunks WHERE chunk_id=?",
                         (chunk_id,)).fetchone()
        db.commit()
    except sqlite3.Error as exc:
        db.rollback()
        raise SourceAuditError("cannot claim source audit chunk") from exc
    status = row[0]
    try:
        payload = json.loads(row[1])
    except (TypeError, ValueError) as exc:
        raise SourceAuditError("source audit resume state is corrupt") from exc
    return status, payload, inserted


def _state_finish(db: sqlite3.Connection, chunk_id: str,
                  status: str, payload: Dict[str, Any]) -> None:
    if status not in ("complete", "failed"):
        raise SourceAuditError("invalid source audit receipt state")
    try:
        db.execute("UPDATE chunks SET status=?,payload=? WHERE chunk_id=?",
                   (status, json.dumps(payload, sort_keys=True), chunk_id))
        db.commit()
    except sqlite3.Error as exc:
        db.rollback()
        raise SourceAuditError("cannot persist source audit chunk receipt") from exc


def _receipt_identity(*, chunk_id: str, pass_name: str, path: str,
                      source_hash: str, chunk_hash: str, line_start: int,
                      line_end: int, pack_id: str, pack_hash: str) -> Dict[str, Any]:
    return {"chunk_id": chunk_id, "pass": pass_name, "path": path,
            "source_hash": source_hash, "chunk_hash": chunk_hash,
            "line_start": line_start, "line_end": line_end,
            "pack_id": pack_id, "pack_hash": pack_hash}


def _safe_receipt(identity: Dict[str, Any], structural: Dict[str, Any],
                  judgment: Dict[str, Any], reserved_usd: float) -> Dict[str, Any]:
    axes = judgment.get("axes") if isinstance(judgment, dict) else {}
    attention = judgment.get("attention") if isinstance(judgment, dict) else {}
    nouls = judgment.get("nouls") if isinstance(judgment, dict) else {}
    return {
        **identity,
        "status": "complete",
        "axes": dict(axes or {}),
        "attention": {key: (attention or {}).get(key)
                      for key in ("level", "value", "confidence")},
        "nouls": dict(nouls or {}),
        "is_fallback": bool(judgment.get("is_fallback")),
        "discarded": bool(structural.get("discarded")),
        "cache_hit": bool(structural.get("cache_hit")),
        "model": structural.get("model"),
        "site": structural.get("site"),
        "cost_usd": float(structural.get("cost") or 0.0),
        "reserved_usd": float(reserved_usd),
        "input_tokens": int(structural.get("input_tokens") or 0),
        "output_tokens": int(structural.get("output_tokens") or 0),
    }


def _lead_id(pass_name: str, pack_hash: str, chunk_hash: str,
             axes: Dict[str, Any]) -> str:
    return _canonical_hash({"pass": pass_name, "pack_hash": pack_hash,
                            "chunk_hash": chunk_hash, "axes": axes})


def _aggregate_leads(receipts: Sequence[Dict[str, Any]],
                     prepared: Dict[str, Dict[str, Any]],
                     dispositions: Optional[Mapping[str, str]]) -> List[Dict[str, Any]]:
    grouped = {}
    for receipt in receipts:
        if receipt.get("status") != "complete" or receipt.get("discarded"):
            continue
        pass_name = receipt["pass"]
        pack = prepared[pass_name]["document"]
        axes = receipt.get("axes") or {}
        signature = _lead_id(pass_name, receipt["pack_hash"],
                             receipt["chunk_hash"], axes)
        item = grouped.setdefault(signature, {
            "lead_id": signature,
            "pass": pass_name,
            "pack_id": receipt["pack_id"],
            "pack_hash": receipt["pack_hash"],
            "axes": axes,
            "axis_labels": {
                axis: pack["axes"].get(axis, {}).get("criteria", {}).get(value)
                for axis, value in axes.items()
            },
            "attention": receipt.get("attention"),
            "nouls": receipt.get("nouls"),
            "is_fallback": bool(receipt.get("is_fallback")),
            "disposition": "unresolved",
            "locations": [],
        })
        item["locations"].append({
            "path": receipt["path"], "source_hash": receipt["source_hash"],
            "chunk_hash": receipt["chunk_hash"],
            "line_start": receipt["line_start"],
            "line_end": receipt["line_end"],
        })
    dispositions = dispositions or {}
    unknown = set(dispositions) - set(grouped)
    if unknown:
        raise SourceAuditError("dispositions contain unknown lead ids")
    leads = []
    for lead_id in sorted(grouped):
        lead = grouped[lead_id]
        disposition = dispositions.get(lead_id, "unresolved")
        if disposition not in DISPOSITIONS:
            raise SourceAuditError("invalid source audit lead disposition")
        lead["disposition"] = disposition
        lead["locations"].sort(key=lambda item: (
            item["path"].casefold(), item["path"], item["line_start"], item["line_end"]))
        leads.append(lead)
    return leads


def run_source_audit(root: Any, packs: Mapping[str, Any], make_policy, *,
                     state_path: Any,
                     run_budget: float,
                     chunk_chars: int = DEFAULT_CHUNK_CHARS,
                     exclude: Optional[Sequence[Any]] = None,
                     dispositions: Optional[Mapping[str, str]] = None,
                     task_id: str = "source-audit") -> Dict[str, Any]:
    """Run/resume independently named passes, storing only safe receipts.

    ``make_policy(actual_spend)`` must return the shared JevPolicy owner or
    ``None`` when the cumulative run ceiling cannot reserve another call.
    """
    if isinstance(run_budget, bool) or not isinstance(run_budget, (int, float)) or run_budget <= 0:
        raise SourceAuditError("run_budget must be a positive USD ceiling")
    root_path = Path(root).resolve(strict=True)
    state_rel = _relative_output(root_path, state_path)
    if state_rel is None:
        raise SourceAuditError("a persistent source audit state path is required")
    state_file = root_path / Path(state_rel)
    exclude_all = list(exclude or ()) + [state_file]
    prepared = _prepared_packs(packs)
    manifest = build_manifest(root_path, packs, chunk_chars=chunk_chars,
                              exclude=exclude_all)
    entries = [entry for entry in manifest["files"] if entry.get("included")]
    db = _open_state(state_file)
    receipts_by_id: Dict[str, Dict[str, Any]] = {}
    pending = []
    stop_reason = "complete"
    stop_detail = None
    run_calls = 0
    run_cost = 0.0
    run_reserved = 0.0
    resume_cursor = None
    try:
        for entry in entries:
            path = root_path / Path(entry["path"])
            raw_bytes, reason = _read_text(path)
            if reason or hashlib.sha256(raw_bytes).hexdigest() != entry["source_hash"]:
                stop_reason = "source_changed"
                stop_detail = "{} changed after the manifest was captured".format(entry["path"])
                resume_cursor = {"path": entry["path"], "line_start": 1}
                break
            source_text = raw_bytes.decode("utf-8", errors="strict")
            pass_name = entry["pass"]
            pack = prepared[pass_name]
            for chunk_text, line_start, line_end in _iter_text_chunks(
                    source_text, chunk_chars):
                chunk_hash = hashlib.sha256(
                    chunk_text.encode("utf-8")).hexdigest()
                chunk_id = _chunk_id(
                    pass_name, entry["path"], entry["source_hash"],
                    chunk_hash, line_start, line_end, pack["pack_hash"], chunk_chars)
                identity = _receipt_identity(
                    chunk_id=chunk_id, pass_name=pass_name, path=entry["path"],
                    source_hash=entry["source_hash"], chunk_hash=chunk_hash,
                    line_start=line_start, line_end=line_end,
                    pack_id=pack["pack_id"], pack_hash=pack["pack_hash"])
                existing = _state_get(db, chunk_id)
                if existing and existing.get("status") == "complete":
                    receipts_by_id[chunk_id] = existing
                    run_cost += float(existing.get("cost_usd") or 0.0)
                    run_reserved += float(existing.get("reserved_usd") or 0.0)
                    continue
                if existing:
                    pending.append({"chunk_id": chunk_id, "path": entry["path"],
                                    "line_start": line_start, "line_end": line_end,
                                    "status": existing.get("status")})
                    if resume_cursor is None:
                        resume_cursor = {"chunk_id": chunk_id,
                                         "path": entry["path"],
                                         "line_start": line_start,
                                         "line_end": line_end}
                    continue
                policy = make_policy(run_cost)
                if policy is None:
                    stop_reason = "run_budget"
                    resume_cursor = {"chunk_id": chunk_id,
                                     "path": entry["path"],
                                     "line_start": line_start,
                                     "line_end": line_end}
                    break
                status, stored, claimed = _state_claim(db, chunk_id, identity)
                if not claimed or status != "in_flight" or stored != identity:
                    # Another process already owns this receipt, or an earlier
                    # interrupted call has unknown billing state. Never repeat it.
                    pending.append({"chunk_id": chunk_id, "path": entry["path"],
                                    "line_start": line_start, "line_end": line_end,
                                    "status": status})
                    if resume_cursor is None:
                        resume_cursor = {"chunk_id": chunk_id,
                                         "path": entry["path"],
                                         "line_start": line_start,
                                         "line_end": line_end}
                    continue
                reserved = (jev_cost(JEV_MAX_INPUT_TOKENS)
                            if bool(getattr(policy, "keyed", False)) else 0.0)
                state = {
                    "path": entry["path"], "kind": pass_name,
                    "element_kind": "source_chunk", "summary": chunk_text,
                    "source_hash": entry["source_hash"],
                    "chunk_hash": chunk_hash, "line_start": line_start,
                    "line_end": line_end, "pass": pass_name,
                    "pack_id": pack["pack_id"], "pack_hash": pack["pack_hash"],
                }
                try:
                    _result, structural, judgment = policy.evaluate_repo_summary(
                        state, pack["document"],
                        site="source_audit_" + pass_name,
                        task_id=task_id, node_id=chunk_id,
                        max_input_tokens=JEV_MAX_INPUT_TOKENS)
                    receipt = _safe_receipt(identity, structural, judgment, reserved)
                    _state_finish(db, chunk_id, "complete", receipt)
                except Exception as exc:
                    _state_finish(db, chunk_id, "failed", {
                        **identity, "status": "failed",
                        "error_type": type(exc).__name__,
                        "reserved_usd": reserved,
                    })
                    stop_reason = "error"
                    stop_detail = "{}: {}".format(entry["path"], type(exc).__name__)
                    pending.append({"chunk_id": chunk_id, "path": entry["path"],
                                    "line_start": line_start, "line_end": line_end,
                                    "status": "failed"})
                    if resume_cursor is None:
                        resume_cursor = {"chunk_id": chunk_id,
                                         "path": entry["path"],
                                         "line_start": line_start,
                                         "line_end": line_end}
                    break
                receipts_by_id[chunk_id] = receipt
                run_calls += 1
                run_cost += float(receipt.get("cost_usd") or 0.0)
                run_reserved += float(receipt.get("reserved_usd") or 0.0)
            if stop_reason != "complete":
                break
    finally:
        db.close()

    if stop_reason == "complete" and pending:
        stop_reason = "resume_pending"
    receipts = [receipts_by_id[key] for key in sorted(receipts_by_id)]
    leads = _aggregate_leads(receipts, prepared, dispositions)
    pass_coverage = {}
    for name in ("code", "docs", "evidence"):
        selected = [entry for entry in manifest["files"]
                    if entry.get("included") and entry.get("pass") == name]
        requested = name in prepared
        expected = sum(entry["chunk_count"] for entry in selected)
        completed = sum(1 for receipt in receipts
                        if receipt.get("pass") == name)
        pass_coverage[name] = {
            "status": ("not_requested" if not requested else
                       "complete" if completed == expected else "partial"),
            "pack_id": prepared[name]["pack_id"] if requested else None,
            "pack_hash": prepared[name]["pack_hash"] if requested else None,
            "files": len(selected), "chunks": expected, "completed": completed,
        }
    excluded_count = sum(1 for entry in manifest["files"]
                         if not entry.get("included"))
    fallbacks = sum(1 for receipt in receipts if receipt.get("is_fallback"))
    discarded = [receipt for receipt in receipts if receipt.get("discarded")]
    released_reservations = sum(max(
        0.0, float(row.get("reserved_usd") or 0.0)
        - float(row.get("cost_usd") or 0.0)) for row in receipts)
    reservation_overrun = sum(max(
        0.0, float(row.get("cost_usd") or 0.0)
        - float(row.get("reserved_usd") or 0.0)) for row in receipts)
    report = {
        "schema": SCHEMA,
        "manifest": manifest,
        "coverage": {
            "passes": pass_coverage,
            "included_files": len(entries),
            "excluded_files": excluded_count,
            "chunks": sum(value["chunks"] for value in pass_coverage.values()),
            "completed_chunks": len(receipts),
            "pending_chunks": max(
                0, sum(value["chunks"] for value in pass_coverage.values())
                - len(receipts)),
            "resume_cursor": resume_cursor,
            "stop_reason": stop_reason,
            "stop_detail": stop_detail,
            "complete": (stop_reason == "complete" and not pending
                         and all(value["status"] == "complete"
                                 for value in pass_coverage.values()
                                 if value["status"] != "not_requested")
                         and pass_coverage["evidence"]["status"] != "not_requested"),
        },
        "spend": {
            "run_budget_usd": float(run_budget),
            "actual_spend_usd": round(run_cost, 6),
            "conservative_reservations_usd": round(run_reserved, 6),
            "released_reservations_usd": round(released_reservations, 6),
            "reservation_overrun_usd": round(reservation_overrun, 6),
            "input_tokens": sum(int(row.get("input_tokens") or 0) for row in receipts),
            "output_tokens": sum(int(row.get("output_tokens") or 0) for row in receipts),
            "calls": len(receipts),
            "calls_this_run": run_calls,
            "fallbacks": fallbacks,
            "discarded_calls": len(discarded),
            "discarded_spend_usd": round(sum(
                float(row.get("cost_usd") or 0.0) for row in discarded), 6),
            "ledger_sites": ["source_audit_code", "source_audit_docs",
                             "source_audit_evidence"],
        },
        "receipts": receipts,
        "leads": leads,
    }
    return report


def source_audit_policy_factory(settings: Any, run_budget: float, ledger: Any):
    """Build bounded per-call shared JevPolicy instances for one audit run."""
    from ._http import HttpTransport
    from .session import jev_face_governor
    from .jev_policy import JEV_MAX_INPUT_TOKENS, policy_for

    state: Dict[str, Any] = {"governor": None, "policy": None}
    worst = jev_cost(JEV_MAX_INPUT_TOKENS)
    per_governor_cap = min(float(run_budget), float(getattr(
        settings, "max_cost", run_budget)))
    transport = HttpTransport()

    def make_policy(cumulative_actual: float):
        if not getattr(settings, "jev_api_key", None):
            if state["policy"] is None:
                state["policy"] = policy_for(
                    settings, transport=transport, governor=None, ledger=ledger)
            return state["policy"]
        if (per_governor_cap < worst
                or float(cumulative_actual) + worst > float(run_budget)):
            return None
        if (state["governor"] is None
                or state["governor"].remaining() < worst):
            state["governor"] = jev_face_governor(
                settings, per_governor_cap)
            if state["governor"] is None:
                return None
            state["policy"] = policy_for(
                settings, transport=transport, governor=state["governor"],
                ledger=ledger)
        return state["policy"]

    return make_policy


def write_report(report: Dict[str, Any], path: Any) -> str:
    """Atomically write the source-safe JSON report (never source text)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if _is_link_or_reparse(target):
        raise SourceAuditError(
            "source audit report path cannot be a symlink or reparse point")
    fd, temp_name = tempfile.mkstemp(prefix=".source-audit-", suffix=".tmp",
                                     dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(report, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
    except OSError as exc:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise SourceAuditError("cannot write source audit report") from exc
    return str(target)
