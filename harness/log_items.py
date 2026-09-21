"""JEV-LOG-parse: code-owned runtime-log item extraction ($0 model spend).

Code owns parsing and mechanical tallies; Jev only judges items against an
operator pack later (``evaluate_log_item``, JEV-LOG-judgment). Levels come
from the log header token ONLY -- message text that merely mentions
``[ERROR]`` never changes an item's level. Items carry code-owned evidence
refs (line index, level, module); reasons from any model stay advisory.
"""
import re
from typing import Any, Dict, List, Optional, Sequence

# Rust `tracing` header, e.g.:
#   2026-09-21T00:37:26.567713Z  WARN scmessenger_core::store::relay_custody: msg
_TRACING_LINE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T[0-9:.]+Z?)\s+"
    r"(?P<level>INFO|WARN|ERROR|DEBUG|TRACE)\s+"
    r"(?P<module>[A-Za-z_][\w:.-]*)\s*:\s?(?P<msg>.*)$")

MAX_ITEM_CHARS = 2000
MAX_CONTINUATION_LINES = 4
DEFAULT_INFO_SAMPLE = 0  # WARN/ERROR only; every Nth INFO when N > 0


def _selected(level: str, levels: Sequence[str], info_sample: int,
              info_seen: int) -> bool:
    if level in levels:
        return True
    # "every Nth INFO" (1-based count): N=2 selects the 2nd, 4th, ...
    return level == "info" and info_sample > 0 and info_seen % info_sample == 0


def extract_log_items(
    text: Any, *, levels: Optional[Sequence[str]] = None,
    info_sample: int = DEFAULT_INFO_SAMPLE,
    max_items: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Extract selected log items from a raw log dump. Deterministic, $0.

    Non-header lines attach as continuation text to the most recent *selected*
    item (bounded by MAX_CONTINUATION_LINES / MAX_ITEM_CHARS). Headers of
    unselected lines close the previous item so continuation text never leaks
    across an unselected record. ``levels`` are lowercase header levels.
    """
    if text is None:
        return []
    if not isinstance(text, str):
        raise ValueError("log text must be a string")
    wanted = tuple(levels) if levels is not None else ("warn", "error")
    items: List[Dict[str, Any]] = []
    info_seen = 0
    line_no = 0
    for raw_line in text.splitlines():
        line_no += 1
        match = _TRACING_LINE.match(raw_line)
        if match is None:
            current = items[-1] if items else None
            if (current is not None and not current.get("_closed")
                    and len(current["text"]) < MAX_ITEM_CHARS
                    and current["_cont"] < MAX_CONTINUATION_LINES):
                current["text"] = (current["text"] + "\n" + raw_line)[:MAX_ITEM_CHARS]
                current["_cont"] += 1
            continue
        level = match.group("level").lower()
        if level == "info":
            info_seen += 1
        if not _selected(level, wanted, info_sample, info_seen):
            if items:
                items[-1]["_closed"] = True
            continue
        module = match.group("module")
        items.append({
            "id": "item-{:06d}".format(len(items) + 1),
            "level": level,
            "module": module,
            "ts": match.group("ts"),
            "line_index": line_no,
            "evidence": "line {} [{}] {}".format(line_no, level, module),
            "text": match.group("msg")[:MAX_ITEM_CHARS],
            "_cont": 0,
            "_closed": False,
        })
        if max_items is not None and len(items) >= max_items:
            break
    for item in items:
        item.pop("_cont", None)
        item.pop("_closed", None)
    return items


def mechanical_tallies(items: Sequence[Dict[str, Any]],
                       raw_text: str) -> Dict[str, Any]:
    """Code-owned tallies over the raw dump and the selected items.

    Present without any model spend -- Jev adds judgment on top, not instead.
    """
    counts: Dict[str, int] = {}
    module_counts: Dict[str, int] = {}
    for item in items:
        counts[item["level"]] = counts.get(item["level"], 0) + 1
        module_counts[item["module"]] = module_counts.get(item["module"], 0) + 1
    top_modules = sorted(module_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    return {
        "total_lines": len(raw_text.splitlines()) if isinstance(raw_text, str) else 0,
        "selected_items": len(items),
        "by_level": dict(sorted(counts.items())),
        "top_modules": [{"module": m, "count": c} for m, c in top_modules],
    }
