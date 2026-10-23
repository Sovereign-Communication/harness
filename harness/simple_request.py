"""Pure helpers for the bounded ordinary-answer lifecycle.

This module decides only whether a request can be answered without repository
intake or a current-data/tool action.  It never selects provider brands or
authorizes writes.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List


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
_SIMPLE_OPENERS = (
    "what ", "how ", "why ", "explain ", "describe ", "tell me ",
    "can you explain ", "could you explain ", "summarize ", "summarise ",
    "rewrite ", "rephrase ",
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


def is_simple_answer_request(prompt: str, *, intent: str = "conversation") -> bool:
    """Return whether *prompt* is a stable, no-tool, direct-answer request."""
    text = (prompt or "").strip()
    lowered = text.lower()
    if intent != "conversation" or not text or len(text) > 2000:
        return False
    if FILE_TARGET_RE.search(text) or is_current_information_request(text):
        return False
    words = set(_WORD_RE.findall(lowered))
    if words & _REPO_ACTION_WORDS:
        return False
    # Require an answer-shaped request.  Bare fragments remain on the normal
    # conversation lane instead of being guessed into this lifecycle.
    return bool(
        lowered.endswith("?")
        or lowered.startswith(_SIMPLE_OPENERS)
        or is_supplied_text_rewrite(text)
    )


def relevant_history(prompt: str, turns: Iterable[Dict[str, Any]], *,
                     max_turns: int = 3, max_chars: int = 2400) -> List[Dict[str, str]]:
    """Keep a small, lexical-overlap slice of prior conversational turns."""
    prompt_terms = set(_WORD_RE.findall((prompt or "").lower())) - _STOP_WORDS
    kept: List[Dict[str, str]] = []
    used = 0
    for turn in reversed(list(turns)):
        prior_prompt = str(turn.get("prompt") or "")
        response = str(turn.get("response") or "")
        terms = set(_WORD_RE.findall(prior_prompt.lower())) - _STOP_WORDS
        if prompt_terms and not prompt_terms.intersection(terms):
            continue
        size = len(prior_prompt) + len(response)
        if not prior_prompt or used + size > max_chars:
            continue
        kept.append({"prompt": prior_prompt, "response": response})
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
