"""Pure helpers for the bounded ordinary-answer lifecycle.

This module decides only whether a request can be answered without repository
intake or a current-data/tool action.  It never selects provider brands or
authorizes writes.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional


FILE_TARGET_RE = re.compile(
    r"\b[a-zA-Z0-9_\-./]+\.(?:py|rs|go|ts|js|md|json|toml|yaml|yml|c|cpp|h)\b"
)
_WORD_RE = re.compile(r"\b[a-z0-9][a-z0-9_-]*\b")
_CURRENT_TERMS = (
    "tonight", "tomorrow", "this week", "next week", "this weekend",
    "next few days", "over the next", "currently", "right now",
    "forecast", "outlook", "upcoming", "latest", "recent", "today",
)
_LOOKUP_TERMS = (
    "find", "search", "look up", "lookup", "news", "what's new",
    "whats new", "catch me up",
)
_REWRITE_VERBS = ("rewrite", "rephrase", "summarize", "summarise")
_SUPPLIED_TEXT_MARKERS = (
    "this:", "following:", "text:", "paragraph:", "sentence:",
    "in a friendlier tone", "in a clearer tone", "more concise",
)
_WORKSPACE_REFERENCE_RE = re.compile(
    r"\b(?:this|the|our|my|current|selected)\s+"
    r"(?:repo(?:sitory)?|codebase|code\s+base|workspace|project)\b"
    r"|\b(?:repo(?:sitory)?|codebase|code\s+base|workspace|project)\s+"
    r"(?:audit|review|inspection|contents?|files?)\b"
)
_WORKSPACE_REVIEW_RE = re.compile(
    r"\b(?:audit|inspect|review|scan|analy[sz]e|examine|assess|"
    r"determine|identify|find|what|which|how)\b"
)
_WORKSPACE_WRITE_DIRECTIVE_RE = re.compile(
    r"\b(?:edit|modify|change|update|fix|add|delete|remove|create|write|"
    r"implement|refactor|rename|move|migrate|install|configure|enable|"
    r"disable|deploy|publish|commit)\b"
)
_SIMPLE_OPENERS = (
    "what ", "how ", "why ", "who ", "where ", "when ", "which ",
    "explain ", "describe ", "define ", "tell me ", "give me ", "list ",
    "can you explain ", "could you explain ", "summarize ", "summarise ",
    "rewrite ", "rephrase ",
    "calculate ", "compute ", "convert ",
    "please tell me ", "please calculate ", "please compute ", "please convert ",
    "can you calculate ", "can you compute ", "can you convert ",
    "could you calculate ", "could you compute ", "could you convert ",
)
_REPO_ACTION_WORDS = frozenset({
    "fix", "implement", "add", "refactor", "update", "change", "modify",
    "create", "delete", "remove", "patch", "repair", "optimize", "build",
    "execute", "apply", "deploy", "commit", "push", "merge",
})
_STOP_WORDS = frozenset({
    "a", "an", "and", "are", "can", "could", "do", "for", "how", "i",
    "in", "is", "it", "me", "of", "on", "please", "the", "this", "to",
    "what", "why", "with", "you",
})
_ANAPHORA_WORDS = frozenset({
    "that", "this", "these", "those", "it", "its", "he", "him", "his",
    "she", "her", "hers", "they", "them", "their", "theirs", "which",
    "who", "whom", "whose", "why", "how", "where", "when", "first",
    "second", "third", "last", "former", "latter", "remember", "context",
    "again", "earlier", "previous", "same", "another", "more",
})

_RETRY_REQUEST_RE = re.compile(
    r"^(?:(?:please|can you|could you)\s+)?"
    r"(?:try again|retry(?: that)?|answer again|"
    r"(?:fulfil|fulfill) (?:the )?original (?:request|question)(?: in full)?|"
    r"answer (?:the )?original (?:request|question)(?: in full)?)"
    r"(?:\s+please)?"
    r"[.!?\s]*$", re.IGNORECASE)
_ANAPHORIC_FOLLOWUP_RE = re.compile(
    r"\b(?:that|this|it|they|them|those|these|there|same|again|"
    r"above|earlier|previous(?:ly)?|original)\b", re.IGNORECASE)


# A clock question asks what time it is *now* -- answerable from the
# machine clock with zero model calls.  The phrasing must be
# self-referential (it/now/today/current); anything asking about an
# external event ("what time is the meeting", "what date was the
# treaty signed") falls through to the normal lane.
_CLOCK_QUESTION_RE = re.compile(
    r"\bwhat(?:'s| is) the (?:current )?(?:time|date|day)\b"
    r"|\bwhat (?:time|day) is it\b"
    r"|\bwhat day is today\b"
    r"|\bcurrent (?:date|time|daytime)\b"
    r"|\btoday'?s date\b"
    r"|\bthe (?:current )?(?:time|date|day) (?:now|currently|right now)\b"
    r"|\btell me the (?:time|date)\b"
)

# Fixed offsets keep explicit abbreviations deterministic across platforms.
# HST is UTC-10 year-round; callers asking for PDT/EDT etc name the daylight
# abbreviation explicitly and receive its corresponding fixed offset.
_TZ_FIXED_OFFSETS = {
    "HST": -10.0, "AKST": -9.0, "AKDT": -8.0, "PST": -8.0,
    "PDT": -7.0, "MST": -7.0, "MDT": -6.0, "CST": -6.0,
    "CDT": -5.0, "EST": -5.0, "EDT": -4.0, "AST": -4.0,
    "GMT": 0.0, "UTC": 0.0, "CET": 1.0, "CEST": 2.0,
    "JST": 9.0, "IST": 5.5, "AEST": 10.0,
}
_TZ_RE = re.compile(
    r"\b(HST|AKST|AKDT|PST|PDT|MST|MDT|CST|CDT|EST|EDT|AST|"
    r"GMT|UTC|CET|CEST|JST|IST|AEST)\b"
)


def parse_clock_request(prompt: str) -> Optional[str]:
    """Return the requested tz abbreviation (or "" for machine-local).

    Returns None when *prompt* is not a self-referential clock question,
    so event-time questions keep the normal model lane.
    """
    text = (prompt or "").strip()
    if not text or FILE_TARGET_RE.search(text):
        return None
    lowered = text.lower()
    if not _CLOCK_QUESTION_RE.search(lowered):
        return None
    words = set(_WORD_RE.findall(lowered))
    if words & _REPO_ACTION_WORDS:
        return None
    match = _TZ_RE.search(text.upper())
    if match:
        return match.group(1)
    return "HST" if re.search(r"\bhawaii\b", lowered) else ""


def answer_clock(prompt: str, now: Optional[datetime] = None) -> Optional[str]:
    """Answer a clock question from the machine clock; None if not one.

    The answer names its source honestly: the machine clock, never a
    lookup.  *now* is injectable so tests stay hermetic and deterministic.
    """
    tz_name = parse_clock_request(prompt)
    if tz_name is None:
        return None
    moment = now if now is not None else datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    if not tz_name:
        local = moment.astimezone()
        stamp = local.strftime("%I:%M %p %Z on %A, %B %d, %Y").lstrip("0")
        return (f"It is {stamp} (machine-local time, "
                f"per this machine's clock; no lookup was run).")
    offset = _TZ_FIXED_OFFSETS[tz_name]
    tzinfo = timezone(timedelta(hours=offset))
    zoned = moment.astimezone(tzinfo)
    stamp = zoned.strftime("%I:%M %p on %A, %B %d, %Y").lstrip("0")
    offset_h = zoned.utcoffset()
    hours = offset_h.total_seconds() / 3600 if offset_h is not None else 0.0
    sign = "+" if hours >= 0 else "-"
    return (f"It is {stamp} {tz_name} (UTC{sign}{abs(hours):g}; "
            f"per this machine's clock; no lookup was run).")


def is_current_information_request(prompt: str) -> bool:
    """Recognize retrieval need by time/lookup shape, without topic rules."""
    text = (prompt or "").strip().lower()
    if not text or FILE_TARGET_RE.search(text):
        return False
    return any(term in text for term in _CURRENT_TERMS + _LOOKUP_TERMS)


def is_supplied_text_rewrite(prompt: str) -> bool:
    """A rewrite of inline prose is an answer, while a file rewrite is work."""
    text = (prompt or "").strip().lower()
    if not text or FILE_TARGET_RE.search(text):
        return False
    if not text.startswith(_REWRITE_VERBS):
        return False
    return any(marker in text for marker in _SUPPLIED_TEXT_MARKERS)


def is_workspace_inspection_request(prompt: str) -> bool:
    """Return whether answering requires reading the selected workspace.

    This distinguishes questions about *this* project from general questions
    about repositories. It is a capability hint only; it grants no write or
    execution authority.
    """
    text = (prompt or "").strip().lower()
    if not text:
        return False
    if FILE_TARGET_RE.search(text):
        if _WORKSPACE_WRITE_DIRECTIVE_RE.search(text):
            return False
        return bool(_WORKSPACE_REVIEW_RE.search(text) or text.endswith("?"))
    if not _WORKSPACE_REFERENCE_RE.search(text):
        return False
    return bool(_WORKSPACE_REVIEW_RE.search(text) or text.endswith("?"))


def is_comprehensive_workspace_inspection_request(prompt: str) -> bool:
    """Return whether a workspace review asks for repository-wide coverage."""
    text = (prompt or "").strip().lower()
    return bool(
        not FILE_TARGET_RE.search(text)
        and is_workspace_inspection_request(text))


def is_simple_answer_request(prompt: str, *, intent: str = "conversation") -> bool:
    """Return whether *prompt* is a stable, no-tool, direct-answer request."""
    text = (prompt or "").strip()
    lowered = text.lower()
    if intent != "conversation" or not text or len(text) > 2000:
        return False
    if FILE_TARGET_RE.search(text):
        return False
    if is_workspace_inspection_request(text):
        return False
    words = set(_WORD_RE.findall(lowered))
    if words & _REPO_ACTION_WORDS:
        return False
    # A self-referential clock question is deterministic machine data, even
    # though "today" normally sends a request to the current-data lane.
    if parse_clock_request(text) is not None:
        return True
    if is_current_information_request(text):
        return False
    if is_answer_retry_request(text):
        return True
    # Require an answer-shaped request.  Bare fragments remain on the normal
    # conversation lane instead of being guessed into this lifecycle.
    stripped = lowered.lstrip("?!.,;: \t\r\n")
    return bool(
        lowered.endswith("?")
        or lowered.startswith("?")
        or stripped.endswith("?")
        or stripped.startswith(_SIMPLE_OPENERS)
        or is_supplied_text_rewrite(text)
    )


def is_answer_retry_request(prompt: str) -> bool:
    """Recognize short retries that should reuse the ordinary answer lane."""
    return bool(_RETRY_REQUEST_RE.fullmatch((prompt or "").strip()))


def resolve_answer_retry(prompt: str, turns: Iterable[Dict[str, Any]]) -> str:
    """Resolve a retry to the referenced substantive request in-session.

    Short retry controls refer to the latest request with an answer. Explicit
    "original request" wording refers to the first substantive request.
    Skip prior retry controls so repeated retries do not target the control
    text itself.
    """
    if not is_answer_retry_request(prompt):
        return prompt
    substantive = []
    for turn in turns:
        metadata = turn.get("simple_request") or {}
        prior = str(turn.get("resolved_prompt")
                    or metadata.get("resolved_prompt")
                    or turn.get("prompt") or "").strip()
        if prior and not is_answer_retry_request(prior):
            substantive.append(prior)
    if not substantive:
        return prompt
    if re.search(r"\boriginal\b", prompt, re.IGNORECASE):
        return substantive[0]
    return substantive[-1]


def relevant_history(prompt: str, turns: Iterable[Dict[str, Any]], *,
                     max_turns: int = 3, max_chars: int = 2400) -> List[Dict[str, str]]:
    """Keep a small topical slice, retaining the antecedent for follow-ups."""
    prompt_words = set(_WORD_RE.findall((prompt or "").lower()))
    prompt_terms = prompt_words - _STOP_WORDS
    has_anaphora = bool(prompt_words.intersection(_ANAPHORA_WORDS))
    anaphoric = bool(_ANAPHORIC_FOLLOWUP_RE.search(prompt or ""))
    kept: List[Dict[str, str]] = []
    used = 0
    turns_list = list(turns)
    antecedent = next(
        (len(turns_list) - 1 - i for i, turn in enumerate(reversed(turns_list))
         if turn.get("prompt") and turn.get("response")), None)
    for idx, turn in enumerate(reversed(turns_list)):
        index = len(turns_list) - 1 - idx
        prior_prompt = str(turn.get("prompt") or "")
        response = str(turn.get("response") or "")
        terms = (
            (set(_WORD_RE.findall(prior_prompt.lower()))
             | set(_WORD_RE.findall(response.lower())))
            - _STOP_WORDS
        )
        is_immediate_prior = (idx == 0)
        has_overlap = bool(prompt_terms and prompt_terms.intersection(terms))
        retry_control = is_answer_retry_request(prior_prompt)
        if retry_control and not response:
            continue
        if not (has_overlap or (is_immediate_prior and has_anaphora)
                or (anaphoric and index == antecedent)):
            continue
        size = len(prior_prompt) + len(response)
        if not prior_prompt or used + size > max_chars:
            continue
        kept.append({"prompt": str(turn.get("resolved_prompt") or prior_prompt),
                     "response": response})
        used += size
        if len(kept) >= max_turns:
            break
    kept.reverse()
    return kept


def answer_prompt(prompt: str, turns: Iterable[Dict[str, str]]) -> str:
    """Build the single-generation prompt with honest capability boundaries."""
    chunks = [
        "Answer the user's ordinary request directly and concisely. This is a "
        "no-tool answer: do not claim current lookup, web access, file access, "
        "execution, or verification. If the request cannot be answered safely "
        "without those capabilities, state that limitation plainly. A refusal "
        "is a valid answer when required. For arithmetic, do not describe the "
        "result as calculator-verified because this lane did not run one.",
    ]
    turns = list(turns)
    if turns:
        chunks.append("Relevant earlier conversation:")
        for turn in turns:
            chunks.append("User: " + turn["prompt"])
            if turn.get("response"):
                chunks.append("Assistant: " + turn["response"])
    chunks.append("Current user request:\n" + prompt)
    return "\n\n".join(chunks)
