"""Request-kind predicates: what a natural-language prompt is asking for.

Single owner of every "what kind of request is this" decision --
conversation vs edit vs audit vs driver vs simple-action, plus the
lookup sub-kinds (site-check, research, current-info) and the
supplied-prose carve-out. ``harness.agent`` (lanes) and ``harness.server``
(dispatch) consume these; they are re-exported from ``harness.agent``
for backward compatibility, but new code imports from here.

Direction of dependency: this module imports nothing from harness at
import time (the one ``web`` use is a function-local import), so lanes
can depend on it without cycles. Pure functions of the prompt string:
cheap, deterministic, no I/O -- safe to call more than once, though
callers should still prefer computing once and threading the answer.
"""

import re

# ONE spelling of "names a repo file": every predicate below consults
# this constant, so a change to what counts as a file target lands in
# exactly one place (it previously lived as five copy-pasted literals).
FILE_EXT_RE = re.compile(
    r"\b[a-zA-Z0-9_\-./]+\.(?:py|rs|go|ts|js|md|json|toml|yaml|yml|c|cpp|h)\b"
)

_WORD_RE = re.compile(r"\b[a-z_0-9-]+\b")


def _cleaned(prompt: str) -> str:
    """Lowercased stripped prompt; empty string when there is no prompt."""
    return (prompt or "").strip().lower()


def _words(text: str) -> list:
    """Word tokens for keyword-membership tests (one spelling, one place)."""
    return _WORD_RE.findall(text)

MUTATION_KEYWORDS = frozenset({
    "fix", "implement", "add", "refactor", "update", "change", "write",
    "modify", "create", "delete", "remove", "clean", "patch", "repair",
    "correct", "optimize", "rewrite", "replace", "build", "execute", "apply",
})

# Request-lifecycle #200: action verbs and uptime phrasings that mark a
# site-availability check. Question form alone must not outrank an explicit
# action verb plus a URL/host target.
_SITE_CHECK_VERBS = ("check", "verify", "test", "confirm", "look up",
                     "lookup", "see")
_SITE_UP_PHRASES = ("is up", "is down", "up right now", "still up",
                    "loads", "loading", "reachable", "resolves",
                    "online", "responding")
_SITE_QUESTION_STATE_RE = re.compile(
    r"\b(is|are|does|did|has)\b.*\b(up|down|loading|loads|reachable|resolves|online|responding|working)\b")


def is_site_check_prompt(prompt: str) -> bool:
    """True when a prompt asks whether a site/host is up (issue #200).

    Requires a URL/host target plus an action verb or an uptime phrasing.
    Genuine questions ("what is...?", "explain...") without a target, and
    repo-centered directives (code extensions, mutation verbs on code,
    plan-execution phrases, audit/driver keywords) never match."""
    from .web import extract_site_target as _extract_target
    cleaned = _cleaned(prompt)
    if not cleaned:
        return False
    if any(p in cleaned for p in EXECUTION_PHRASES):
        return False
    if _extract_target(prompt) is None:
        return False
    if FILE_EXT_RE.search(prompt):
        return False
    words = _words(cleaned)
    if any(w in MUTATION_KEYWORDS for w in words):
        return False
    if any(v in cleaned for v in _SITE_CHECK_VERBS):
        return True
    if any(p in cleaned for p in _SITE_UP_PHRASES):
        return True
    # Question form with the verb and the state split around the host:
    # "is X up?", "is X down right now?", "does X resolve?".
    return bool(_SITE_QUESTION_STATE_RE.search(cleaned))

EXECUTION_PHRASES = frozenset({
    "execute the plan", "execute plan", "run the plan", "run plan",
    "apply the plan", "apply plan", "test the plan", "test plan",
    "see if it works", "test if it works", "check if it works",
    "run the test", "run the tests", "run tests", "run test",
    "execute tests", "execute test", "execute it", "run it", "apply it",
    "proceed with plan", "proceed with the plan", "execute this", "run this",
    "test this", "verify the plan", "test the implementation",
    "execute implementation", "run the implementation",
    "execute the code", "run the code", "test the code",
})

CONVERSATION_STARTERS = frozenset({
    "what", "how", "why", "explain", "describe", "tell", "can", "is", "are",
    "does", "who", "when", "where", "list", "show", "help",
})

# Asking what the world currently says is a research question, not an edit.
# The dogfood prompt ("can you find news about ...") was routed to the
# conversation lane but never gathered sources, so the run iterated on a
# question it has no evidence for.
_RESEARCH_SUBJECTS = ("news", "research", "recent", "latest", "today",
                      "papers", "paper", "article", "articles", "study",
                      "studies", "release", "releases", "update", "updates")
_RESEARCH_VERBS = ("find", "search", "look up", "lookup", "google",
                   "what's new", "whats new", "tell me about", "catch me up",
                   "summary of", "summarize", "summarise")
_QUESTION_OPENERS = ("what", "who", "when", "where", "which", "why", "how",
                     "is ", "are ", "does ", "do ", "did ", "can you",
                     "could you", "any news", "has there")

# Time-window/currentness terms for the agnostic current-info detector.
# Topic words (weather, traffic, events, ...) never appear here: any subject
# paired with a time window earns retrieval the same way.
_CURRENT_INFO_TERMS = ("tonight", "tomorrow", "this week", "next week",
                       "this weekend", "next few days", "over the next",
                       "currently", "right now", "forecast", "outlook",
                       "upcoming")
# Imperative lookup openings ("give me the outlook for next week").
_CURRENT_INFO_IMPERATIVES = ("give me", "show me", "tell me", "get me",
                             "find ")


def is_research_question(prompt: str) -> bool:
    """True when a prompt is asking what is currently true in the world.

    A research *subject* is required, and that is what keeps edits out:
    "find the bug in auth.py" has a verb but no subject, so it stays an edit.
    On top of that the prompt must read as a lookup -- an explicit research
    verb, or an interrogative opening -- so "update the README" mentions a
    subject but asks for nothing.
    """
    cleaned = _cleaned(prompt)
    if not cleaned:
        return False
    if not any(subj in cleaned for subj in _RESEARCH_SUBJECTS):
        return False
    if any(v in cleaned for v in _RESEARCH_VERBS):
        return True
    if cleaned.endswith("?"):
        return True
    return cleaned.startswith(_QUESTION_OPENERS)


def is_current_info_request(prompt: str) -> bool:
    """True when a prompt asks about time-sensitive current state.

    Deliberately topic-agnostic: it matches time-window/currentness terms
    ("next week", "tomorrow", "forecast", ...) plus a lookup shape, never
    a subject keyword -- a Pahoa week-weather question, a weekend-events
    question, and a traffic-tomorrow question all earn retrieval the same
    way, with no per-topic routing and no weather-specific module.
    """
    cleaned = _cleaned(prompt)
    if not cleaned or len(cleaned) > 2000:
        return False
    if not any(term in cleaned for term in _CURRENT_INFO_TERMS):
        return False
    if FILE_EXT_RE.search(prompt):
        return False
    if any(w in MUTATION_KEYWORDS for w in _words(cleaned)):
        return False
    if is_site_check_prompt(prompt):
        return False
    if cleaned.endswith("?"):
        return True
    if cleaned.startswith(_QUESTION_OPENERS):
        return True
    if cleaned.startswith(_CURRENT_INFO_IMPERATIVES):
        return True
    return any(v in cleaned for v in _RESEARCH_VERBS)


_TEXT_REWRITE_MARKERS = ("this", "these", "following", "supplied",
                         "tone", "summarize", "summarise", "summary",
                         "rephrase", "rewrite")


def _is_supplied_prose_rewrite(prompt: str) -> bool:
    """True when the request rewrites supplied prose, not repo files.

    Single owner of the supplied-prose carve-out: the intent classifier
    and the simple-request predicate both honor it, so a text rewrite
    cannot route to the edit lane in one place and the simple lane in
    another (the live failure: "rewrite this in a friendlier tone"
    classified edit, spent ~160s in plan/execute, ended refused). Only
    rewrite/replace verbs, text markers, and no file target qualify --
    any other mutation verb or a file extension stays repo work.
    """
    cleaned = (prompt or "").strip()
    if not cleaned or len(cleaned) > 2000:
        return False
    lowered = cleaned.lower()
    if FILE_EXT_RE.search(prompt):
        return False
    mut = set(_words(lowered)) & MUTATION_KEYWORDS
    if not mut or mut - {"rewrite", "replace"}:
        return False
    return any(marker in lowered for marker in _TEXT_REWRITE_MARKERS)


def is_simple_answer_request(prompt: str) -> bool:
    """True for ordinary no-tool answers (agnostic simple-request lane).

    Covers everyday instructions ("How do I make a peanut butter and jelly
    sandwich?"), stable factual explanations, small calculations, and text
    rewrites of supplied text: conversation-intent prompts with no file
    extension, no mutation verb, no repo path signal, no site-check target,
    no research subject, and no current-info time window needing the web
    lane. Held-out paraphrases must hit this path -- never prompt-specific
    routing, never a per-topic special case (weather is one regression
    example, not the objective).
    """
    cleaned = (prompt or "").strip()
    if not cleaned or len(cleaned) > 2000:
        return False
    if (is_site_check_prompt(prompt) or is_research_question(prompt)
            or is_current_info_request(prompt)):
        return False
    lowered = cleaned.lower()
    if FILE_EXT_RE.search(prompt):
        return False
    word_set = set(_words(lowered))
    if word_set & MUTATION_KEYWORDS:
        # Narrow carve-out: rewriting/replacing SUPPLIED prose ("rewrite this
        # in a friendlier tone") is a text transformation, not repo work --
        # shared predicate, see _is_supplied_prose_rewrite.
        if _is_supplied_prose_rewrite(prompt):
            return True
        return False
    if classify_prompt_intent(prompt) != "conversation":
        return False
    return True


# Module-level matchers so classify builds no per-call tuples or regexes.
_AUDIT_PHRASES = ("verify chain", "audit ledger", "ledger status", "check ledger")
_DRIVER_PHRASES = ("driver task", "drive request", "run driver", "machine drive", "perceive dom", "perceive cli", "driver perception", "perception step")
_CONVERSATION_EXCLUSIONS = ("refactor ", "implement ", "fix bug ", "add test", "execute ", "run ")
_EXEC_DIRECTIVE_RE = re.compile(
    r"\b(?:execute|run|apply)\b.*?\b(?:plan|code|test|tests|implementation|script)\b")


def classify_prompt_intent(prompt: str) -> str:
    # Classify a natural language prompt into conversation, edit, or audit
    cleaned = prompt.strip().lower()
    words = _words(cleaned)
    first_word = words[0] if words else ""

    if any(w in cleaned for w in _AUDIT_PHRASES):
        return "audit"

    if any(w in cleaned for w in _DRIVER_PHRASES):
        return "driver"

    # Execution directives: commands to execute/test a plan, code, or tests on local hardware
    if any(p in cleaned for p in EXECUTION_PHRASES) or _EXEC_DIRECTIVE_RE.search(cleaned):
        return "edit"

    # Check for explicit file extension occurrences
    has_file_ext = bool(FILE_EXT_RE.search(prompt))

    # Request-lifecycle #200: an action verb plus a URL/host target is a
    # site-availability check, even in question form. Ordinary questions
    # without a target keep the conversation lane below.
    if is_site_check_prompt(prompt):
        return "simple-action"

    # Prompts asking conversational questions (or ending with ?) take precedence unless explicit files/paths are given
    if first_word in CONVERSATION_STARTERS or cleaned.endswith("?"):
        if not has_file_ext and not any(w in cleaned for w in _CONVERSATION_EXCLUSIONS):
            return "conversation"

    # Supplied-prose rewrites are conversation, not repo work -- the same
    # carve-out the simple-request predicate honors, so the two agree.
    if _is_supplied_prose_rewrite(prompt):
        return "conversation"

    has_mutation_verb = any(w in MUTATION_KEYWORDS for w in words)

    if has_mutation_verb or has_file_ext:
        return "edit"

    return "conversation"
