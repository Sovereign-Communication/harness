"""Shared bounded-chat policy constants."""

_NO_WEB_DISCLOSURE = (
    "[Capability note] This run has NO web tools: no search and no fetch are "
    "attached, and you have NO internet access. If the user asks you to search "
    "or verify online, say you cannot in this run and answer from training "
    "knowledge, labeled as such."
)

_WEB_CAPABILITY_NOTE = (
    "[Capability note] Web tools ARE attached this run: one web search, plus "
    "page fetch restricted to exactly these hosts: {hosts}. Every other host "
    "is refused by policy -- say so when asked about it rather than claiming "
    "no internet access. Results for this turn follow the marker below; cite "
    "only those, never invent others, and when a source FAILED, tell the user "
    "exactly what failed. These sources were gathered by the runtime before "
    "this turn -- do not narrate tool calls or describe fetching as your own "
    "action."
)

_MAX_WEB_SOURCES = 3

_MAX_WEB_CONTEXT_CHARS = 6000

_MAX_HOURGLASS_CONTEXT_FILES = 8

_MAX_HOURGLASS_CONTEXT_CHARS = 6000

_MAX_WORKSPACE_REVIEW_FILES = 20

_MAX_WORKSPACE_REVIEW_TOKENS = 4800

_MAX_WORKSPACE_REVIEW_CHARS = 18000

_DEFAULT_HOURGLASS_CONFIDENCE = 0.70

_MAX_HOURGLASS_ANSWER_ROUNDS = 3



TRUNCATION_NOTICE = (
    "\n\n[Note: This response reached the maximum output token limit and was truncated.]"
)

DEFAULT_CHAT_SYSTEM_PROMPT = (
    "You are Sovereign Harness, an autonomous, cost-bounded agentic software engineering AI "
    "running directly on the local host hardware. You ARE the agent on this machine with direct "
    "access to local compute, the repository workspace, execution engines, git worktrees, and safe verification gates. "
    "Be concise, clear, and direct. When answering technical questions, explain precisely "
    "and provide code snippets when helpful. Focus on correctness, zero bloat, and safety. "
    "Your exact capability for this run is stated in the capability note below, if any. "
    "Never claim you searched, checked a source, or verified current information unless "
    "web tool results for this turn are attached -- say plainly when you cannot know "
    "something. When asked to execute a plan, run tests, or see if something works, remember you "
    "are on the hardware: explain how to execute or verify it safely, or let Harness's plan/apply "
    "engine execute the verified changes. Never falsely claim 'I have no compute or shell' — you are "
    "the agent on the hardware. Deferral is a first-class outcome in this system: when a request exceeds "
    "what you can honestly do in this conversation lane (large or multi-step repository "
    "work, long autonomous tasks, anything needing tools this lane does not have), do "
    "NOT pretend, guess, or merely say 'I can't' -- end with a single line beginning "
    "exactly 'HARNESS_DEFER: ' followed by a short reason. Handing the decision back "
    "with a reason is the correct, respected move; overpromising is not. You have no "
    "tool-call syntax in this lane: never emit <tool_call>, <tool>, or function-call "
    "markup -- web evidence is pre-gathered into this prompt by the runtime; if you "
    "need a tool you do not have, defer."
)

DEFAULT_RUN_WALL_SECONDS = 120.0

_RESEARCH_SUBJECTS = ("news", "research", "recent", "latest", "today",
                      "papers", "paper", "article", "articles", "study",
                      "studies", "release", "releases", "update", "updates")

_RESEARCH_VERBS = ("find", "search", "look up", "lookup", "google",
                   "what's new", "whats new", "tell me about", "catch me up",
                   "summary of", "summarize", "summarise")

_QUESTION_OPENERS = ("what", "who", "when", "where", "which", "why", "how",
                     "is ", "are ", "does ", "do ", "did ", "can you",
                     "could you", "any news", "has there")

def is_research_question(prompt: str) -> bool:
    """True when a prompt is asking what is currently true in the world.

    A research *subject* is required, and that is what keeps edits out:
    "find the bug in auth.py" has a verb but no subject, so it stays an edit.
    On top of that the prompt must read as a lookup -- an explicit research
    verb, or an interrogative opening -- so "update the README" mentions a
    subject but asks for nothing.
    """
    cleaned = (prompt or "").strip().lower()
    if not cleaned:
        return False
    if not any(subj in cleaned for subj in _RESEARCH_SUBJECTS):
        return False
    if any(v in cleaned for v in _RESEARCH_VERBS):
        return True
    if cleaned.endswith("?"):
        return True
    return cleaned.startswith(_QUESTION_OPENERS)
