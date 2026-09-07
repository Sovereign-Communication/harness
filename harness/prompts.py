"""Prompt contracts for the apply engine, and response parsing for them.

The builders here are pure functions: file path, instruction, current content
in, prompt text out. The response side owns the wire protocol — the
HARNESS_READY readiness marker (conservative: an explicit defer anywhere in
the response wins) and the strict unified-diff contract, where every hunk
must match the source exactly before anything is written.
"""
import os
import re

from .consent import consent_preview
from .errors import HarnessError

MAX_FILE_LINES = 500
MAX_INSTRUCTION_CHARS = 1000
MAX_SNIPPET_CHARS = 2000
MAX_APPLY_ROUNDS = 3
CAPABILITY_MARKER = "HARNESS_DEFER:"
READY_MARKER = "HARNESS_READY:"


def consent_mechanics_text(file_path, content, instruction):
    """The consent prompt text shared by the initial probe and every renewal.
    The model can only make an honest accept/defer call if it sees the work
    as the dispatcher will run it: the file content it is being asked to
    change, not a bare instruction stripped of context. State the mechanics
    explicitly -- several models otherwise read 'edit file' as requiring
    direct filesystem access and decline.
    (Dogfood finding: a 2400-char head-only excerpt blinded the gate --
    config.py's pools sit past line 60, and the model correctly deferred on
    'I can only see the first 60 lines'. Show the same content the apply
    model will edit, honestly labeled.)"""
    n_lines, label, preview = consent_preview(content)
    return (
        "WORK MECHANICS: you do NOT need any tool or filesystem access. "
        "The file content is shown here in this prompt; you will reply "
        "with the complete new file content as plain text, and the "
        "dispatcher writes it and runs an automated verification gate.\n"
        f"FILE {file_path} ({n_lines} lines; {label}):\n"
        f"{preview}\n"
        f"REQUESTED CHANGE: {instruction[:1200]}")

_READY_INSTRUCTION = (
    "Your response MUST begin with exactly one line of the form "
    "'HARNESS_READY: confident' or 'HARNESS_READY: defer'. Declare "
    "'HARNESS_READY: defer' (with a short reason on that same line) if you are "
    "not certain you can complete this change correctly -- do not guess. "
    "Declare 'HARNESS_READY: confident' only if you are sure, then output the "
    "COMPLETE new file content on the lines after that marker.")

_DIFF_READY_INSTRUCTION = (
    "Your response MUST contain exactly one readiness line of the form "
    "'HARNESS_READY: confident' or 'HARNESS_READY: defer', placed on the "
    "FIRST line, before the diff. Declare 'HARNESS_READY: defer' (with a "
    "short reason on that same line) if you cannot produce a diff that "
    "matches the current content exactly -- do not guess at hunk contents. "
    "Otherwise declare 'HARNESS_READY: confident' on the first line and put "
    "the unified diff on the lines after that marker.")

_DEFER_INSTRUCTION = (
    "Do your best and assume nothing. You have no file or web access; the file "
    "content below is all you can see. If you reach the limit of your capability "
    "to complete this change correctly, do NOT guess or invent. Stop, keep the "
    "partial work, and emit a single line beginning exactly with 'HARNESS_DEFER:' "
    "followed by a JSON object "
    "{\"remaining_scope\": \"what still needs doing\", \"reason\": \"why you can't continue\"}. "
    "The partial file is preserved and the next model continues from it.")

_CONTINUATION_PREAMBLE = (
    "CONTINUATION of a deferred task. A prior model stopped because: {reason}. "
    "Remaining scope to complete: {scope}. Continue from the current file content.")


def _preamble(continuation):
    if not continuation:
        return None
    return _CONTINUATION_PREAMBLE.format(
        reason=continuation.get("reason") or "unknown",
        scope=continuation.get("remaining_scope") or "complete the change")


def build_apply_prompt(file_path, instruction, edit_snippet, original,
                       round_ctx=None, continuation=None, backend="harness"):
    """Build the per-round apply prompt for the given backend contract."""
    lang = os.path.splitext(file_path)[1].lstrip(".")
    pre = _preamble(continuation)
    if backend == "morph":
        # Preserve MorphLite's contract while keeping the request inside the
        # governed chat path: the model sees instruction/code/update tags and
        # still gets Harness sovereignty, retry feedback, and cost guards.
        parts = []
        if pre:
            parts.append(pre)
        parts.extend((
            f"<instruction>{instruction}</instruction>",
            f"<code>{original}</code>",
            f"<update>{edit_snippet or instruction}</update>",
        ))
        if round_ctx:
            parts.append(round_ctx)
        parts.append(
            "Return ONLY the complete transformed file content. Do not add markdown "
            "or explanation. If you cannot complete the change correctly, return "
            "HARNESS_READY: defer <reason> instead of guessing.")
        return "\n\n".join(parts)
    if backend == "diff":
        # Unified-diff mode (#11): the model returns a strict diff instead
        # of the whole file, so files beyond the 500-line rewrite ceiling
        # become editable and unchanged lines cost zero output tokens.
        lines = [
            "You are making a single, scoped code change AS A UNIFIED DIFF.",
            "The complete current file content is provided below; work from "
            "that content only and reply with text.",
            f"File: {file_path} (language: {lang or 'text'})",
            "",
            "OUTPUT CONTRACT (strict):",
            "- Reply with ONE unified diff (git-style), nothing else.",
            "- Context lines and removed lines must match the current file "
            "EXACTLY, character for character. No fuzz matching exists; any "
            "mismatch aborts the change.",
            "- The source below is numbered (NNN| prefix). The number IS the "
            "line number: @@ headers must use it, and it is already stripped "
            "from the content you quote.",
            "- Keep hunks minimal: touch only the lines the instruction "
            "requires.",
            "",
            f"INSTRUCTION: {instruction}",
            f"EDIT SNIPPET (intent anchor): {edit_snippet or 'none'}",
            "",
            _DIFF_READY_INSTRUCTION,
            "",
            _DEFER_INSTRUCTION,
            "",
            "CURRENT FILE CONTENT (line numbers are reference only; never "
            "include them in the diff):",
            "```",
            *_number_lines(original),
            "```",
        ]
        if pre:
            lines.insert(0, pre)
        prompt = "\n".join(lines)
        if round_ctx:
            prompt += "\n\n" + round_ctx
        return prompt
    lines = [
        "You are making a single, scoped code change.",
        "The complete current file content is provided below in this prompt; "
        "you do NOT need (and do not have) filesystem or tool access — work "
        "entirely from the content shown here and reply with text only.",
        f"File: {file_path} (language: {lang or 'text'})",
        f"The file is {original.count(chr(10)) + 1} lines. Output the COMPLETE new file "
        f"content -- preserve all unchanged parts exactly.",
        "",
        f"INSTRUCTION: {instruction}",
        f"EDIT SNIPPET (intent anchor): {edit_snippet or 'none'}",
        "",
        _READY_INSTRUCTION,
        "",
        _DEFER_INSTRUCTION,
        "",
        "Respond with ONLY the file content (optionally wrapped in one fenced code block).",
        "",
        "CURRENT FILE CONTENT:",
        f"```\n{original}\n```",
    ]
    if pre:
        lines.insert(0, pre)
    prompt = "\n".join(lines)
    if round_ctx:
        prompt += "\n\n" + round_ctx
    return prompt


def _extract_file_content(text):
    """Pull the new file body from a model response.

    If the response is a single fenced code block, take its contents;
    otherwise treat the whole response as the file body, stripping any stray
    HARNESS_READY marker line -- protocol text must never land in the file.
    """
    if not text:
        return ""
    lines = text.splitlines(keepends=True)
    start = end = None
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith("```"):
            if start is None:
                start = i
            else:
                end = i
                break
    if start is not None and end is not None:
        return "".join(lines[start + 1:end])
    # Protocol marker lines must never land in the file, wherever the model
    # put them (with or without a reason after the colon, mid-body, etc.).
    return "".join(
        ln for ln in lines if not ln.lstrip().startswith(READY_MARKER)
    )


def _parse_ready(content):
    """Parse the inline HARNESS_READY verdict.

    The marker is usually the first line, but models regularly emit leading
    blank lines (or place it just before the file body) -- search the first
    few lines so a stray preamble never leaks the protocol marker into the
    written file. Returns (decision, reason, rest) where decision is
    'confident', 'defer', or 'missing' (no marker; treat as confident with a
    warning so the verify gate + DEFER backstop still protect us). rest is the
    content with the marker line removed.
    """
    if not content:
        return "missing", "", content or ""
    lines = content.splitlines(keepends=True)
    # Scan the WHOLE response: models legitimately place the readiness marker
    # after the payload (e.g. following a unified diff). A trailing
    # 'HARNESS_READY: defer' that went unseen would apply the edit anyway,
    # so missing-marker detection must never be window-bound. When several
    # markers appear (a model hedging both ways), the CONSERVATIVE one wins:
    # defer beats confident regardless of position.
    first = None
    for i, ln in enumerate(lines):
        line = ln.strip()
        if line.startswith(READY_MARKER):
            decision_raw = line[len(READY_MARKER):].strip()
            decision, _, reason = decision_raw.partition(" ")
            decision = decision.strip().lower()
            if decision not in ("confident", "defer"):
                continue
            if decision == "defer":
                # Sovereignty: an explicit defer anywhere in the response
                # immediately wins, wherever it appears.
                rest = "".join(lines[:i]) + "".join(lines[i + 1:])
                return "defer", reason.strip(), rest
            if first is None:
                first = (i, reason.strip())
    if first is not None:
        i, reason = first
        rest = "".join(lines[:i]) + "".join(lines[i + 1:])
        return "confident", reason, rest
    return "missing", "", content


_DIFF_HUNK_RE = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _number_lines(text):
    """Prefix each line with its 1-based line number (NNN| format).

    Models asked to produce @@ headers against unnumbered content consistently
    miscount (observed: off-by-one hunks on consecutive retries); numbering
    the source turns line arithmetic into copying.
    """
    width = len(str(text.count("\n") + 1))
    return [f"{i:0{width}d}|{line}"
            for i, line in enumerate(text.split("\n"), 1)]


def _apply_unified_diff(current, diff_text):
    """Apply a strict unified diff to `current`; return the new content (#11).

    Every hunk must match the source EXACTLY - no fuzz. A mismatch raises
    HarnessError so the round can be retried with the error as feedback
    instead of writing a silently corrupt merge. Standard git-style
    headers are tolerated; prose around the diff is skipped.
    """
    text = diff_text.replace("\\r\\n", "\n")
    lines = text.split("\n")
    hunks = []
    i = 0
    n = len(lines)
    while i < n:
        m = _DIFF_HUNK_RE.match(lines[i])
        if not m:
            i += 1
            continue
        old_start = int(m.group(1))
        old_len = int(m.group(2)) if m.group(2) is not None else 1
        new_len = int(m.group(4)) if m.group(4) is not None else 1
        i += 1
        old_body, new_body = [], []
        while i < n and (len(old_body) < old_len or len(new_body) < new_len):
            ln = lines[i]
            if ln.startswith("@@"):
                break
            tag, rest = (ln[0], ln[1:]) if ln else (' ', '')
            if tag == ' ':
                old_body.append(rest)
                new_body.append(rest)
            elif tag == '-':
                old_body.append(rest)
            elif tag == '+':
                new_body.append(rest)
            elif tag == chr(92):
                pass  # no-newline marker
            else:
                raise HarnessError(f"malformed diff line: {ln[:60]!r}")
            i += 1
        if len(old_body) != old_len or len(new_body) != new_len:
            raise HarnessError(
                f"hunk at line {old_start} is truncated: expected -{old_len}/+{new_len}, "
                f"got -{len(old_body)}/{len(new_body)}")
        hunks.append((old_start, old_len, old_body, new_body))
    if not hunks:
        raise HarnessError("no unified-diff hunks found in model output")

    src = current.split("\n")
    out = []
    pos = 0
    for old_start, old_len, old_body, new_body in hunks:
        idx = old_start - 1 if old_len else old_start
        if idx < pos or idx > len(src):
            raise HarnessError(
                f"hunk at line {old_start} overlaps or exceeds the source (pos={pos})")
        if old_len and src[idx:idx + old_len] != old_body:
            raise HarnessError(
                f"hunk at line {old_start} does not match the source exactly; "
                "regenerate the diff against the current content")
        out.extend(src[pos:idx])
        out.extend(new_body)
        pos = idx + old_len
    out.extend(src[pos:])
    return "\n".join(out)
