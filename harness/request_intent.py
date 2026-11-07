"""Deterministic prompt-intent rules, separated from request dispatch."""

import re
from typing import Optional

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

# Issue #448: an explicit read-only workspace audit must not be routed through
# paid edit planning. "create a report" names the request's output artifact, not
# a repository write, so it cannot be allowed to flip the intent to "edit".
_READ_ONLY_MARKERS = (
    "read-only",
    "read only",
    "readonly",
    "audit only",
    "just audit",
    "review only",
    "inspect only",
    "hands-off",
    "hands off",
    "non-mutating",
    "no writes",
    "no write",
    "do not change",
    "don't change",
    "do not modify",
    "don't modify",
    "do not edit",
    "don't edit",
    "no edits",
    "no changes",
    "without changing",
    "without modifying",
)
_REPORT_ARTIFACT_RE = re.compile(
    r"\b(?:create|write|produce|generate|make|emit|save)\s+"
    r"(?:me\s+)?(?:a|an|the)?\s*(?:short\s+|full\s+|final\s+|single\s+)?"
    r"(?:report|summary|findings)\b"
)
_WRITE_DIRECTIVE_RE = re.compile(
    r"\b(?:edit|modify|change|update|fix|add|delete|remove|create|write|"
    r"implement|refactor|rename|move|migrate|install|configure|enable|"
    r"disable|deploy|publish|commit|apply|patch|repair|correct|optimize|"
    r"replace)\b"
)
_AUDIT_NOUN_RE = re.compile(r"\baudit(?:s|ing)?\b|\bassessment\b")
_NO_TOOLS_RE = re.compile(
    r"\b(?:do\s+not|don'?t|never|no)\s+(?:call|use|invoke|run)\s+"
    r"(?:any\s+)?tools?\b"
    r"|\b(?:do\s+not|don'?t)\s+(?:inspect|read|open|search)\s+"
    r"(?:any\s+)?(?:files?|the\s+files?|folders?|the\s+workspace|"
    r"the\s+repo(?:sitory)?)\b"
    r"|\bwithout\s+(?:calling|using|invoking)\s+(?:any\s+)?tools?\b"
    r"|\bno\s+tools?\b"
)


def is_read_only_audit_request(prompt: Optional[str]) -> bool:
    """True when the prompt asks for an audit it explicitly forbids itself to write.

    Both halves are required: an explicit no-write marker *and* the audit noun.
    A genuine write request that merely uses the adjective ("add a read-only
    audit mode") keeps its write directive and therefore stays out of this
    predicate, so it still classifies as an edit.
    """
    text = " ".join((prompt or "").split()).lower()
    if not text:
        return False
    if not any(marker in text for marker in _READ_ONLY_MARKERS):
        return False
    if not _AUDIT_NOUN_RE.search(text):
        return False
    remainder = _REPORT_ARTIFACT_RE.sub(" ", text)
    return not _WRITE_DIRECTIVE_RE.search(remainder)


def is_workspace_audit_request(prompt: str) -> bool:
    """True when a read-only audit names the repository itself (issue #448).

    The audit lane has two different questions: "audit ledger" asks about the
    cryptographic chain, while "audit all aspects of the workspace" asks about
    the source tree. Only the second one can be answered by enumerating and
    preflighting a repository scope; answering it with a chain verdict is the
    misroute this predicate exists to prevent.
    """
    text = " ".join((prompt or "").split()).lower()
    if not is_read_only_audit_request(prompt):
        return False
    return any(marker in text for marker in (
        "workspace", "repository", "repo", "codebase", "source tree",
        "all aspects", "whole tree", "project",
    ))


def no_tools_requested(prompt: str) -> bool:
    """True when the prompt explicitly forbids tool use (issue #448).

    An operator constraint outranks every advisory route that would add
    retrieval, so callers must consult this before enabling web or tools.
    """
    return bool(_NO_TOOLS_RE.search(" ".join((prompt or "").split()).lower()))


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

    # A read-only workspace audit outranks every mutation keyword inside it:
    # the operator forbade writes, so "create a report" must not become paid
    # edit planning (issue #448).
    if is_read_only_audit_request(prompt):
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
