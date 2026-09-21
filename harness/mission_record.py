"""HUL-A mission pack: mission.yaml schema, pack layout, STATUS, receipts, budget, resume.

Track B product code. On ``origin/main`` there was zero mission runtime; this
module is the single owner of the on-disk mission pack under ``missions/<id>/``.

Pack layout (every mission owns all of these paths):

    mission.yaml       schema source of truth (min fields below)
    STATUS.md          regenerated from pack state (never hand-author)
    FINDINGS.md        placeholder until the terminal helper runs (HUL-D driver)
    receipts.jsonl     append-only attempt/evidence receipts (history/ledger style)
    jev_evals.jsonl    append-only Jev evaluation records (no second Jev client)
    budget.json        spent + ceiling + terminal_reserve (dual budget is HUL-B)
    resume.json        continuation-style resumable state
    artifacts/         per-attempt artifacts
    INDEX.md           pack file index

mission.yaml minimum fields::

    id, request,
    scope.in_scope, scope.out_of_scope,
    success_definition,
    limits.max_cost_usd,
    terminal_reserve.cost_usd,
    persistence.root,
    verifier.kind

Dual-budget *enforcement* is deliberately out of scope (HUL-B). This module
only stores ``terminal_reserve`` and honestly computes::

    working_remaining = max_cost_usd - spent - terminal_reserve.cost_usd

Failures raise ``HarnessError``. JSONL appends and file rewrites follow the
history/ledger append style and ``filesafety._atomic_write``.
"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .errors import HarnessError
from .filesafety import _atomic_write

RESUME_SCHEMA_VERSION = 1
PACK_FILES = (
    "mission.yaml",
    "STATUS.md",
    "FINDINGS.md",
    "receipts.jsonl",
    "jev_evals.jsonl",
    "budget.json",
    "resume.json",
    "artifacts",
    "INDEX.md",
)
_TERMINAL_STATUSES = frozenset(("terminal", "complete", "failed", "blocked"))
_OPEN_STATUSES = frozenset(("in_progress", "open", "created"))
_MISSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def validate_mission_id(mission_id: str) -> str:
    """Mission ids are path-safe single segments (no separators, no '..')."""
    if not isinstance(mission_id, str) or not mission_id.strip():
        raise HarnessError("mission id must be a non-empty string")
    mid = mission_id.strip()
    if "/" in mid or "\\" in mid or ".." in mid:
        raise HarnessError(
            f"invalid mission id {mission_id!r}: path separators and '..' are not allowed")
    if not _MISSION_ID_RE.match(mid):
        raise HarnessError(
            f"invalid mission id {mission_id!r}: use letters, digits, '.', '_', '-'")
    return mid


def pack_dir_for(root: Any, mission_id: str) -> Path:
    """Resolve the pack directory: ``<root>/<mission_id>``."""
    mid = validate_mission_id(mission_id)
    if root is None or root == "":
        raise HarnessError("mission pack root is required")
    return Path(root) / mid


def _require_mapping(value: Any, what: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise HarnessError(f"{what} must be a mapping")
    return value


def _require_str(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HarnessError(f"{what} must be a non-empty string")
    return value.strip()


def _require_number(value: Any, what: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HarnessError(f"{what} must be a number")
    num = float(value)
    if num < minimum:
        raise HarnessError(f"{what} must be >= {minimum}")
    return num


def _require_str_list(value: Any, what: str) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise HarnessError(f"{what} must be a list of strings")
    out: List[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise HarnessError(f"{what} entries must be non-empty strings")
        out.append(item.strip())
    return out


def validate_mission_spec(spec: Any) -> Dict[str, Any]:
    """Validate and normalize a mission spec. Rejects missing success/limits.

    Returns a new normalized dict; the caller's object is not mutated.
    """
    raw = _require_mapping(spec, "mission spec")
    mission_id = validate_mission_id(raw.get("id"))
    request = _require_str(raw.get("request"), "mission request")
    success = _require_str(raw.get("success_definition"), "success_definition")
    if "limits" not in raw:
        raise HarnessError("mission limits are required (limits.max_cost_usd)")
    limits = _require_mapping(raw.get("limits"), "limits")
    if "max_cost_usd" not in limits:
        raise HarnessError("mission limits.max_cost_usd is required")
    max_cost = _require_number(limits.get("max_cost_usd"), "limits.max_cost_usd")
    if "terminal_reserve" not in raw:
        raise HarnessError("mission terminal_reserve is required "
                           "(terminal_reserve.cost_usd)")
    reserve = _require_mapping(raw.get("terminal_reserve"), "terminal_reserve")
    if "cost_usd" not in reserve:
        raise HarnessError("mission terminal_reserve.cost_usd is required")
    reserve_cost = _require_number(reserve.get("cost_usd"), "terminal_reserve.cost_usd")
    if reserve_cost > max_cost:
        raise HarnessError(
            "terminal_reserve.cost_usd must not exceed limits.max_cost_usd")
    scope = _require_mapping(raw.get("scope") or {}, "scope")
    in_scope = _require_str_list(scope.get("in_scope"), "scope.in_scope")
    out_scope = _require_str_list(scope.get("out_of_scope"), "scope.out_of_scope")
    persistence = _require_mapping(raw.get("persistence") or {}, "persistence")
    root = _require_str(persistence.get("root") or "missions", "persistence.root")
    verifier = _require_mapping(raw.get("verifier") or {}, "verifier")
    kind = _require_str(verifier.get("kind") or "unspecified", "verifier.kind")
    return {
        "id": mission_id,
        "request": request,
        "scope": {"in_scope": in_scope, "out_of_scope": out_scope},
        "success_definition": success,
        "limits": {"max_cost_usd": max_cost},
        "terminal_reserve": {"cost_usd": reserve_cost},
        "persistence": {"root": root},
        "verifier": {"kind": kind},
    }


def build_mission_spec(
    *,
    mission_id: str,
    request: str,
    success_definition: str,
    max_cost_usd: float,
    terminal_reserve_cost_usd: float = 0.0,
    in_scope: Optional[List[str]] = None,
    out_of_scope: Optional[List[str]] = None,
    persistence_root: str = "missions",
    verifier_kind: str = "unspecified",
) -> Dict[str, Any]:
    """Assemble a spec from CLI/library fields, then validate it."""
    return validate_mission_spec({
        "id": mission_id,
        "request": request,
        "scope": {
            "in_scope": list(in_scope or []),
            "out_of_scope": list(out_of_scope or []),
        },
        "success_definition": success_definition,
        "limits": {"max_cost_usd": max_cost_usd},
        "terminal_reserve": {"cost_usd": terminal_reserve_cost_usd},
        "persistence": {"root": persistence_root},
        "verifier": {"kind": verifier_kind},
    })


# ---------------------------------------------------------------- yaml
# Minimal YAML subset (stdlib only — package has zero runtime deps).
# Supports nested maps (2-space indent), lists of scalars, quoted scalars
# with \\n/\\\" escapes, plain scalars, and int/float/bool.


def _dump_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    text = str(value)
    needs_quote = (
        text == ""
        or text != text.strip()
        or any(ch in text for ch in "\n:\"'{}[]#&*!|>%@`")
        or text in ("true", "false", "null", "~")
        or text.startswith("-")
    )
    if not needs_quote:
        try:
            float(text)
            needs_quote = True
        except ValueError:
            pass
    if not needs_quote:
        return text
    escaped = (text.replace("\\", "\\\\")
                   .replace('"', '\\"')
                   .replace("\n", "\\n")
                   .replace("\r", "\\r")
                   .replace("\t", "\\t"))
    return f'"{escaped}"'


def dump_mission_yaml(spec: Dict[str, Any]) -> str:
    """Serialize a normalized mission spec to the pack's YAML subset."""
    norm = validate_mission_spec(spec)
    lines: List[str] = [
        f"id: {_dump_scalar(norm['id'])}",
        f"request: {_dump_scalar(norm['request'])}",
        "scope:",
    ]
    if norm["scope"]["in_scope"]:
        lines.append("  in_scope:")
        for item in norm["scope"]["in_scope"]:
            lines.append(f"    - {_dump_scalar(item)}")
    else:
        lines.append("  in_scope: []")
    if norm["scope"]["out_of_scope"]:
        lines.append("  out_of_scope:")
        for item in norm["scope"]["out_of_scope"]:
            lines.append(f"    - {_dump_scalar(item)}")
    else:
        lines.append("  out_of_scope: []")
    lines.extend([
        f"success_definition: {_dump_scalar(norm['success_definition'])}",
        "limits:",
        f"  max_cost_usd: {norm['limits']['max_cost_usd']}",
        "terminal_reserve:",
        f"  cost_usd: {norm['terminal_reserve']['cost_usd']}",
        "persistence:",
        f"  root: {_dump_scalar(norm['persistence']['root'])}",
        "verifier:",
        f"  kind: {_dump_scalar(norm['verifier']['kind'])}",
        "",
    ])
    return "\n".join(lines)


def _parse_scalar(token: str) -> Any:
    s = token.strip()
    if s == "[]":
        return []
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        body = s[1:-1]
        out = []
        i = 0
        while i < len(body):
            ch = body[i]
            if ch == "\\" and i + 1 < len(body):
                nxt = body[i + 1]
                out.append({"n": "\n", "r": "\r", "t": "\t",
                            '"': '"', "\\": "\\"}.get(nxt, nxt))
                i += 2
                continue
            out.append(ch)
            i += 1
        return "".join(out)
    if s in ("true", "True"):
        return True
    if s in ("false", "False"):
        return False
    if s in ("null", "~", ""):
        return None
    try:
        if re.fullmatch(r"[+-]?\d+", s):
            return int(s)
        if re.fullmatch(r"[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?", s):
            return float(s)
    except ValueError:
        pass
    return s


def _split_key_value(line: str) -> Optional[tuple]:
    if ":" not in line:
        return None
    key, _, rest = line.partition(":")
    key = key.strip()
    if not key or " " in key:
        return None
    return key, rest.strip()


def load_mission_yaml(text: str) -> Dict[str, Any]:
    """Parse the pack YAML subset back into a nested dict (unvalidated)."""
    if text is None:
        raise HarnessError("mission.yaml is empty")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    root: Dict[str, Any] = {}
    # stack of (indent, container). Container is dict or list.
    stack: List[tuple] = [(-1, root)]
    pending_list_key: Optional[tuple] = None  # (indent, parent_dict, key)

    def _container_for(indent: int) -> Any:
        while len(stack) > 1 and stack[-1][0] >= indent:
            stack.pop()
        return stack[-1][1]

    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        stripped = raw.strip()
        if stripped.startswith("- "):
            item = _parse_scalar(stripped[2:])
            if pending_list_key is not None and pending_list_key[0] < indent:
                _pindent, parent, key = pending_list_key
                if key not in parent or not isinstance(parent[key], list):
                    parent[key] = []
                parent[key].append(item)
                continue
            # list item under a list container on the stack
            container = _container_for(indent)
            if isinstance(container, list):
                container.append(item)
                continue
            raise HarnessError(f"mission.yaml list item without a list parent: {raw!r}")
        if stripped == "[]":
            if pending_list_key is not None:
                _pindent, parent, key = pending_list_key
                parent[key] = []
                pending_list_key = None
            continue
        pair = _split_key_value(stripped)
        if pair is None:
            raise HarnessError(f"mission.yaml line is not key: value: {raw!r}")
        key, rest = pair
        container = _container_for(indent)
        if not isinstance(container, dict):
            raise HarnessError(f"mission.yaml map key under non-map: {raw!r}")
        if rest == "":
            # nested map or upcoming list
            container[key] = {}
            stack.append((indent, container[key]))
            pending_list_key = (indent, container, key)
            # peek: if the next non-empty content is a list under this key,
            # we replace the empty map when list items arrive
            continue
        value = _parse_scalar(rest)
        container[key] = value
        pending_list_key = None

    # second pass: keys that received only list items left an empty map placeholder
    def _fix_lists(node: Any) -> Any:
        return node

    return _fix_lists(root)


def load_mission_yaml_file(path: Path) -> Dict[str, Any]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise HarnessError(f"cannot read mission.yaml: {path} ({e})") from e
    return load_mission_yaml(text)


# ---------------------------------------------------------------- paths


class MissionPack:
    """Filesystem handle for one mission pack under ``root / id``."""

    def __init__(self, root: Any, mission_id: str):
        self.root = Path(root)
        self.id = validate_mission_id(mission_id)
        self.dir = pack_dir_for(self.root, self.id)

    @property
    def mission_yaml(self) -> Path:
        return self.dir / "mission.yaml"

    @property
    def status_md(self) -> Path:
        return self.dir / "STATUS.md"

    @property
    def findings_md(self) -> Path:
        return self.dir / "FINDINGS.md"

    @property
    def receipts_path(self) -> Path:
        return self.dir / "receipts.jsonl"

    @property
    def jev_evals_path(self) -> Path:
        return self.dir / "jev_evals.jsonl"

    @property
    def budget_path(self) -> Path:
        return self.dir / "budget.json"

    @property
    def resume_path(self) -> Path:
        return self.dir / "resume.json"

    @property
    def artifacts_dir(self) -> Path:
        return self.dir / "artifacts"

    @property
    def index_md(self) -> Path:
        return self.dir / "INDEX.md"

    def spec(self) -> Dict[str, Any]:
        return validate_mission_spec(load_mission_yaml_file(self.mission_yaml))

    def exists(self) -> bool:
        return self.mission_yaml.is_file()


# ---------------------------------------------------------------- jsonl


def _append_jsonl(path: Path, record: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(record, dict):
        raise HarnessError("receipt/eval record must be a mapping")
    body = dict(record)
    body.setdefault("ts", _now_iso())
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(body, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError as e:
        raise HarnessError(f"cannot append JSONL: {path} ({e})") from e
    return body


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not Path(path).exists():
        return []
    out: List[Dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict):
                    out.append(entry)
    except OSError as e:
        raise HarnessError(f"cannot read JSONL: {path} ({e})") from e
    return out


def append_receipt(pack: MissionPack, receipt: Dict[str, Any]) -> Dict[str, Any]:
    """Append one receipt. Append-only: never rewrites prior lines."""
    if not pack.exists():
        raise HarnessError(f"mission pack not found: {pack.dir}")
    if not isinstance(receipt, dict):
        raise HarnessError("receipt/eval record must be a mapping")
    body = dict(receipt)
    body.setdefault("mission_id", pack.id)
    body.setdefault("kind", "attempt")
    return _append_jsonl(pack.receipts_path, body)


def load_receipts(pack: MissionPack) -> List[Dict[str, Any]]:
    return _load_jsonl(pack.receipts_path)


def append_jev_eval(pack: MissionPack, entry: Dict[str, Any]) -> Dict[str, Any]:
    """Append one Jev evaluation record (storage only — no client fork)."""
    if not pack.exists():
        raise HarnessError(f"mission pack not found: {pack.dir}")
    if not isinstance(entry, dict):
        raise HarnessError("receipt/eval record must be a mapping")
    body = dict(entry)
    body.setdefault("mission_id", pack.id)
    return _append_jsonl(pack.jev_evals_path, body)


def load_jev_evals(pack: MissionPack) -> List[Dict[str, Any]]:
    return _load_jsonl(pack.jev_evals_path)


# ---------------------------------------------------------------- budget


def working_remaining(
    max_cost_usd: float,
    spent: float,
    terminal_reserve_cost_usd: float,
) -> float:
    """Honest remaining budget after spent and terminal reserve.

    Full dual-budget *enforcement* (attempts never eat reserve) is HUL-B;
    this helper only computes the stored field honestly.
    """
    return max(
        0.0,
        _require_number(max_cost_usd, "max_cost_usd")
        - _require_number(spent, "spent")
        - _require_number(terminal_reserve_cost_usd, "terminal_reserve_cost_usd"),
    )


def budget_from_spec(spec: Dict[str, Any]) -> Dict[str, Any]:
    norm = validate_mission_spec(spec)
    max_cost = norm["limits"]["max_cost_usd"]
    reserve = norm["terminal_reserve"]["cost_usd"]
    spent = 0.0
    return {
        "mission_id": norm["id"],
        "max_cost_usd": max_cost,
        "terminal_reserve_cost_usd": reserve,
        "spent": spent,
        "working_remaining": working_remaining(max_cost, spent, reserve),
        "updated_at": _now_iso(),
    }


def write_budget(pack: MissionPack, budget: Dict[str, Any]) -> Dict[str, Any]:
    body = _require_mapping(budget, "budget")
    max_cost = _require_number(body.get("max_cost_usd"), "budget.max_cost_usd")
    reserve = _require_number(
        body.get("terminal_reserve_cost_usd"), "budget.terminal_reserve_cost_usd")
    spent = _require_number(body.get("spent", 0.0), "budget.spent")
    out = {
        "mission_id": pack.id,
        "max_cost_usd": max_cost,
        "terminal_reserve_cost_usd": reserve,
        "spent": spent,
        "working_remaining": working_remaining(max_cost, spent, reserve),
        "updated_at": _now_iso(),
    }
    _atomic_write(str(pack.budget_path), json.dumps(out, indent=2, sort_keys=True) + "\n")
    return out


def load_budget(pack: MissionPack) -> Dict[str, Any]:
    if not pack.budget_path.is_file():
        raise HarnessError(f"budget.json missing for mission {pack.id}")
    try:
        body = json.loads(pack.budget_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise HarnessError(f"budget.json unreadable for mission {pack.id}: {e}") from e
    return _require_mapping(body, "budget.json")


def record_spend(pack: MissionPack, amount: float) -> Dict[str, Any]:
    """Record attempt spend against the mission budget and rewrite budget.json."""
    delta = _require_number(amount, "spend amount")
    budget = load_budget(pack)
    spent = _require_number(budget.get("spent", 0.0), "budget.spent") + delta
    return write_budget(pack, {
        "max_cost_usd": budget.get("max_cost_usd"),
        "terminal_reserve_cost_usd": budget.get("terminal_reserve_cost_usd"),
        "spent": spent,
    })


# ---------------------------------------------------------------- resume


def build_resume_state(pack: MissionPack, **extra: Any) -> Dict[str, Any]:
    receipts = load_receipts(pack)
    budget = load_budget(pack) if pack.budget_path.is_file() else {}
    state = {
        "schema_version": RESUME_SCHEMA_VERSION,
        "mission_id": pack.id,
        "pack_dir": str(pack.dir),
        "status": "in_progress",
        "receipts_seq": len(receipts),
        "attempts": sum(1 for r in receipts if r.get("kind") == "attempt"),
        "spent": float(budget.get("spent", 0.0) or 0.0),
        "updated_at": _now_iso(),
    }
    state.update(extra)
    return state


def write_resume(pack: MissionPack, state: Dict[str, Any]) -> Dict[str, Any]:
    body = validate_resume(state, expected_id=pack.id)
    _atomic_write(str(pack.resume_path), json.dumps(body, indent=2, sort_keys=True) + "\n")
    return body


def load_resume(pack: MissionPack) -> Dict[str, Any]:
    if not pack.resume_path.is_file():
        raise HarnessError(f"resume.json missing for mission {pack.id}")
    try:
        body = json.loads(pack.resume_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise HarnessError(f"resume.json unreadable for mission {pack.id}: {e}") from e
    return body


def validate_resume(state: Any, *, expected_id: Optional[str] = None) -> Dict[str, Any]:
    """Continuation-style validation before any resume work begins."""
    body = _require_mapping(state, "resume state")
    version = body.get("schema_version", RESUME_SCHEMA_VERSION)
    if version != RESUME_SCHEMA_VERSION:
        raise HarnessError(f"unsupported resume schema_version: {version!r}")
    mission_id = _require_str(body.get("mission_id"), "resume.mission_id")
    if expected_id is not None and mission_id != validate_mission_id(expected_id):
        raise HarnessError(
            f"resume mission_id {mission_id!r} does not match pack {expected_id!r}")
    status = body.get("status", "in_progress")
    if not isinstance(status, str) or not status.strip():
        raise HarnessError("resume.status must be a non-empty string")
    if status not in _OPEN_STATUSES and status not in _TERMINAL_STATUSES:
        raise HarnessError(f"resume.status is not recognized: {status!r}")
    for key in ("receipts_seq", "attempts"):
        val = body.get(key, 0)
        if isinstance(val, bool) or not isinstance(val, int) or val < 0:
            raise HarnessError(f"resume.{key} must be a non-negative integer")
    spent = body.get("spent", 0.0)
    _require_number(spent, "resume.spent")
    pack_dir = body.get("pack_dir")
    if pack_dir is not None and not isinstance(pack_dir, str):
        raise HarnessError("resume.pack_dir must be a string when present")
    return {
        "schema_version": RESUME_SCHEMA_VERSION,
        "mission_id": mission_id,
        "pack_dir": pack_dir,
        "status": status.strip(),
        "receipts_seq": body.get("receipts_seq", 0),
        "attempts": body.get("attempts", 0),
        "spent": float(spent),
        "updated_at": body.get("updated_at") or _now_iso(),
    }


# ---------------------------------------------------------------- status / findings


def is_terminal(pack: MissionPack) -> bool:
    if not pack.resume_path.is_file():
        return False
    try:
        state = load_resume(pack)
    except HarnessError:
        return False
    return str(state.get("status", "")) in _TERMINAL_STATUSES


def generate_status_md(pack: MissionPack) -> str:
    """STATUS.md body from pack state. Regenerated after every material change."""
    spec = pack.spec()
    receipts = load_receipts(pack)
    evals = load_jev_evals(pack)
    budget = load_budget(pack) if pack.budget_path.is_file() else {}
    resume = load_resume(pack) if pack.resume_path.is_file() else {}
    terminal = is_terminal(pack)
    phase = resume.get("status", "in_progress")
    artifacts = []
    if pack.artifacts_dir.is_dir():
        artifacts = sorted(p.name for p in pack.artifacts_dir.iterdir() if p.is_file())
    lines = [
        f"# STATUS — {spec['id']}",
        "",
        f"- **phase:** {phase}",
        f"- **request:** {spec['request']}",
        f"- **success_definition:** {spec['success_definition']}",
        f"- **scope.in_scope:** {', '.join(spec['scope']['in_scope']) or '(none)'}",
        f"- **scope.out_of_scope:** {', '.join(spec['scope']['out_of_scope']) or '(none)'}",
        f"- **limits.max_cost_usd:** {spec['limits']['max_cost_usd']}",
        f"- **terminal_reserve.cost_usd:** {spec['terminal_reserve']['cost_usd']}",
        f"- **verifier.kind:** {spec['verifier']['kind']}",
        f"- **budget.spent:** {budget.get('spent', 0.0)}",
        f"- **budget.working_remaining:** {budget.get('working_remaining', 0.0)}",
        f"- **receipts:** {len(receipts)}",
        f"- **jev_evals:** {len(evals)}",
        f"- **resume.attempts:** {resume.get('attempts', 0)}",
        f"- **artifacts:** {', '.join(artifacts) or '(none)'}",
        f"- **terminal:** {'yes' if terminal else 'no'}",
        "",
    ]
    return "\n".join(lines)


def write_status(pack: MissionPack) -> Path:
    body = generate_status_md(pack)
    _atomic_write(str(pack.status_md), body)
    return pack.status_md


def findings_placeholder_md(mission_id: str) -> str:
    return (
        f"# FINDINGS — {mission_id}\n\n"
        "Not terminal. Findings are written only when the mission reaches a "
        "terminal outcome via mark_terminal (HUL-D owns the until-limits driver).\n"
    )


def ensure_findings_placeholder(pack: MissionPack) -> Path:
    if pack.findings_md.is_file():
        return pack.findings_md
    _atomic_write(str(pack.findings_md), findings_placeholder_md(pack.id))
    return pack.findings_md


def write_findings(pack: MissionPack, body: str) -> Path:
    """Write FINDINGS.md. Callers other than mark_terminal must not use this
    to fake a terminal mission — the terminal helper owns the phase flip."""
    if not isinstance(body, str) or not body.strip():
        raise HarnessError("findings body must be a non-empty string")
    _atomic_write(str(pack.findings_md), body if body.endswith("\n") else body + "\n")
    return pack.findings_md


def mark_terminal(
    pack: MissionPack,
    *,
    outcome: str = "complete",
    findings: Optional[str] = None,
) -> Dict[str, Any]:
    """Terminal helper (stub contract for HUL-D). Writes FINDINGS and flips resume.

    Until HUL-D ships the until-limits driver, this helper is the only
    supported way to generate real FINDINGS content and mark the pack terminal.
    """
    if not pack.exists():
        raise HarnessError(f"mission pack not found: {pack.dir}")
    if outcome not in _TERMINAL_STATUSES:
        raise HarnessError(
            f"terminal outcome must be one of {sorted(_TERMINAL_STATUSES)}; got {outcome!r}")
    text = findings if findings is not None else (
        f"# FINDINGS — {pack.id}\n\n"
        f"Terminal outcome: `{outcome}` at {_now_iso()}.\n\n"
        "Generated by mark_terminal (HUL-D driver will replace this stub body "
        "with the until-limits report).\n"
    )
    write_findings(pack, text)
    resume = load_resume(pack) if pack.resume_path.is_file() else build_resume_state(pack)
    resume["status"] = outcome
    resume["updated_at"] = _now_iso()
    write_resume(pack, resume)
    write_status(pack)
    return resume


# ---------------------------------------------------------------- index


def generate_index_md(pack: MissionPack) -> str:
    entries = []
    for name in PACK_FILES:
        path = pack.dir / name
        if path.is_dir():
            entries.append(f"- `{name}/` — directory")
        elif path.is_file():
            try:
                size = path.stat().st_size
            except OSError:
                size = -1
            entries.append(f"- `{name}` — {size} bytes")
        else:
            entries.append(f"- `{name}` — missing")
    return (
        f"# INDEX — {pack.id}\n\n"
        "Pack files (HUL-A layout):\n\n"
        + "\n".join(entries)
        + "\n"
    )


def write_index(pack: MissionPack) -> Path:
    _atomic_write(str(pack.index_md), generate_index_md(pack))
    return pack.index_md


# ---------------------------------------------------------------- init / load


def init_mission_pack(root: Any, spec: Dict[str, Any]) -> MissionPack:
    """Create the full pack layout under ``root/id``. Fails if already present."""
    norm = validate_mission_spec(spec)
    pack = MissionPack(root, norm["id"])
    if pack.dir.exists():
        raise HarnessError(f"mission pack already exists: {pack.dir}")
    pack.artifacts_dir.mkdir(parents=True, exist_ok=False)
    _atomic_write(str(pack.mission_yaml), dump_mission_yaml(norm))
    _atomic_write(str(pack.budget_path), json.dumps(budget_from_spec(norm), indent=2,
                                                    sort_keys=True) + "\n")
    ensure_findings_placeholder(pack)
    _atomic_write(str(pack.receipts_path), "")
    _atomic_write(str(pack.jev_evals_path), "")
    write_resume(pack, build_resume_state(pack))
    write_status(pack)
    write_index(pack)
    return pack


def load_mission_pack(root: Any, mission_id: str) -> MissionPack:
    pack = MissionPack(root, mission_id)
    if not pack.exists():
        raise HarnessError(f"mission pack not found: {pack.dir}")
    pack.spec()  # fail closed on corrupt mission.yaml
    return pack


def pack_summary(pack: MissionPack) -> Dict[str, Any]:
    """Machine-readable status payload for CLI / MCP consumers."""
    spec = pack.spec()
    receipts = load_receipts(pack)
    evals = load_jev_evals(pack)
    budget = load_budget(pack)
    resume = load_resume(pack)
    return {
        "id": pack.id,
        "pack_dir": str(pack.dir),
        "phase": resume.get("status", "in_progress"),
        "terminal": is_terminal(pack),
        "request": spec["request"],
        "success_definition": spec["success_definition"],
        "scope": spec["scope"],
        "limits": spec["limits"],
        "terminal_reserve": spec["terminal_reserve"],
        "verifier": spec["verifier"],
        "budget": budget,
        "receipts_count": len(receipts),
        "jev_evals_count": len(evals),
        "receipts": receipts,
        "jev_evals": evals,
        "resume": resume,
        "status_md_path": str(pack.status_md),
        "findings_md_path": str(pack.findings_md),
    }
