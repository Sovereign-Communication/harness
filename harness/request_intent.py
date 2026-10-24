"""Deterministic prompt-intent rules, separated from request dispatch."""

import re

from .edit_lifecycle import EXECUTION_PHRASES
from .simple_request import is_supplied_text_rewrite

MUTATION_KEYWORDS = frozenset(
    {
        "fix",
        "implement",
        "add",
        "refactor",
        "update",
        "change",
        "write",
        "modify",
        "create",
        "delete",
        "remove",
        "clean",
        "patch",
        "repair",
        "correct",
        "optimize",
        "rewrite",
        "replace",
        "build",
        "execute",
        "apply",
    }
)
_SITE_CHECK_VERBS = ("check", "verify", "test", "confirm", "look up", "lookup", "see")
_SITE_UP_PHRASES = (
    "is up",
    "is down",
    "up right now",
    "still up",
    "loads",
    "loading",
    "reachable",
    "resolves",
    "online",
    "responding",
)


def is_site_check_prompt(prompt: str) -> bool:
    """True when a prompt asks whether a site/host is up (issue #200).

    Requires a URL/host target plus an action verb or an uptime phrasing.
    Genuine questions ("what is...?", "explain...") without a target, and
    repo-centered directives (code extensions, mutation verbs on code,
    plan-execution phrases, audit/driver keywords) never match."""
    from .web import extract_site_target as _extract_target

    cleaned = (prompt or "").strip().lower()
    if not cleaned:
        return False
    if any(p in cleaned for p in EXECUTION_PHRASES):
        return False
    if _extract_target(prompt) is None:
        return False
    # An audit/assessment/driving request is not an availability probe even
    # though it contains "verify" or "check" and a URL.  Treating it as a
    # simple-action check refuses the real request before any evidence is
    # gathered.
    if re.search(
        r"\baudit\b|\busability\b|\buser[- ]friend(?:ly|liness)\b|\bux\b"
        r"|\bjev\b|\bassessment\b|\bnitpick\b|\bdrive\s+the\s+(?:site|website|page)\b",
        cleaned,
    ):
        return False
    if re.search(
        r"\b[a-zA-Z0-9_\-./]+\.(?:py|rs|go|ts|js|md|json|toml|yaml|yml|c|cpp|h)\b", prompt
    ):
        return False
    words = re.findall(r"\b[a-z_0-9-]+\b", cleaned)
    if any(w in MUTATION_KEYWORDS for w in words):
        return False
    if any(v in cleaned for v in _SITE_CHECK_VERBS):
        return True
    if any(p in cleaned for p in _SITE_UP_PHRASES):
        return True
    # Question form with the verb and the state split around the host:
    # "is X up?", "is X down right now?", "does X resolve?".
    return bool(
        re.search(
            r"\b(is|are|does|did|has)\b.*\b(up|down|loading|loads|reachable|resolves|online|responding|working)\b",
            cleaned,
        )
    )


CONVERSATION_STARTERS = frozenset(
    {
        "what",
        "how",
        "why",
        "explain",
        "describe",
        "tell",
        "can",
        "is",
        "are",
        "does",
        "who",
        "when",
        "where",
        "list",
        "show",
        "help",
    }
)
_LITERAL_GREETING_RE = re.compile(
    r"^(?:test\s*[-:,]\s*)?(?:please\s+)?(?:just\s+)?"
    r"(?:say|reply(?:\s+with)?|respond(?:\s+with)?)\s+"
    r"(?:hello|hi|hey)(?:\s+and\s+(?:stop|nothing else))?[.!]?$",
    re.IGNORECASE,
)


def is_literal_greeting_request(prompt: str) -> bool:
    """Recognize a command whose complete requested output is a greeting."""
    cleaned = " ".join((prompt or "").split())
    return bool(_LITERAL_GREETING_RE.fullmatch(cleaned))


def classify_prompt_intent(prompt: str) -> str:
    # Classify a natural language prompt into conversation, edit, or audit
    cleaned = prompt.strip().lower()
    words = re.findall(r"\b[a-z_0-9-]+\b", cleaned)
    first_word = words[0] if words else ""

    if any(w in cleaned for w in ("verify chain", "audit ledger", "ledger status", "check ledger")):
        return "audit"

    if any(
        w in cleaned
        for w in (
            "driver task",
            "drive request",
            "run driver",
            "machine drive",
            "perceive dom",
            "perceive cli",
            "driver perception",
            "perception step",
        )
    ):
        return "driver"

    if any(
        w in cleaned
        for w in (
            "provision host",
            "probe host",
            "plan provision",
            "setup host",
            "provision environment",
            "host provisioning",
        )
    ):
        return "provision"

    # Execution directives: commands to execute/test a plan, code, or tests on local hardware
    if any(p in cleaned for p in EXECUTION_PHRASES) or any(
        re.search(rf"\b{action}\b.*?\b(?:plan|code|test|tests|implementation|script)\b", cleaned)
        for action in ("execute", "run", "apply")
    ):
        return "edit"

    # Check for explicit file extension occurrences
    has_file_ext = bool(
        re.search(
            r"\b[a-zA-Z0-9_\-./]+\.(?:py|rs|go|ts|js|md|json|toml|yaml|yml|c|cpp|h)\b", prompt
        )
    )

    # Request-lifecycle #200: an action verb plus a URL/host target is a
    # site-availability check, even in question form. Ordinary questions
    # without a target keep the conversation lane below.
    if is_site_check_prompt(prompt):
        return "simple-action"

    # Prompts asking conversational questions (or ending with ?) take precedence unless explicit files/paths are given
    if first_word in CONVERSATION_STARTERS or cleaned.endswith("?"):
        if not has_file_ext and not any(
            w in cleaned
            for w in ("refactor ", "implement ", "fix bug ", "add test", "execute ", "run ")
        ):
            return "conversation"

    # Transforming prose supplied in the prompt is conversational work.  A
    # named source file (or a second repo mutation verb) remains an edit.
    if is_supplied_text_rewrite(prompt):
        return "conversation"

    has_mutation_verb = any(w in MUTATION_KEYWORDS for w in words)

    if has_mutation_verb or has_file_ext:
        return "edit"

    return "conversation"
