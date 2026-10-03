"""Turning declared configuration into a live driver.

This module exists because of a specific failure: the perception tiers were
built, wired to each other, and provable -- and unreachable. A default
``Driver()`` carried no sources, so ``driver-core step`` answered ``Tried:
none`` and every ``POST /step`` ended in ``no_capture``. The chain worked
only for a caller writing Python by hand, which makes it a library feature
rather than a product one. It is also the one place that reads declared
sources out of :class:`~driver_core.config.Settings`, so a reader looking
for "what can this driver observe" finds it in one file.

Two rules the wiring holds to, both of which are the reason it exists:

* **A source is off unless it was named.** Nothing is inferred from another
  setting's presence, and the vision tier is never enabled by anything other
  than its own explicit switch.
* **Commands are tokenised, never executed through a shell.** :func:`parse_argv`
  splits on whitespace and quotes and nothing else -- no globbing, no variable
  expansion, no redirection. A setting that reached a shell would be the first
  place in this package where a configuration string could become code.
"""
from .config import ConfigError
from .extractors import ExtractorPool, StructuredExtractor, build_vision_pool
from .perception import (
    CLI, DOM, GUI, MCP, CliSource, DomSource, McpSource, ScreenSource,
)

#: What ends one token and begins the next, outside quotes.
_SEPARATORS = " \t\r\n"


def parse_argv(text):
    """Split a declared command into an argv list. Tokenisation only.

    **One rule: a backslash is never an escape character. There are no
    escapes.** Quotes group, and every character between them is literal.

    This replaced ``shlex.split``, which is a POSIX shell word-splitter and so
    reads ``\\`` as an escape everywhere outside quotes. On Windows that meant
    ``C:\\Python314\\python.exe server.py`` resolved to
    ``C:Python314python.exe`` -- the backslashes were eaten, the source
    appeared configured, and the step then failed at run time with a ``not
    found`` naming a path nobody had typed. That disables the ``cli`` and
    ``mcp`` tiers on the platform this project tests on most.

    Dropping escapes entirely is also what keeps the rule total. Keeping
    ``\\"`` as the one exception would have reintroduced the identical bug for
    a Windows path ending in a separator: ``"C:\\Users\\me\\"`` would have its
    closing quote consumed and its last backslash lost. A rule with no
    exceptions cannot be misapplied in a way nobody can predict, and a host
    that needs a quote character inside an argument is told so rather than
    having it silently mangled.

    What this still does *not* do is what a shell does: no globbing, no
    ``$VAR`` interpolation, no ``|``, no ``&&``, no redirection.
    :func:`driver_core.osal.run` takes an argv list precisely so that no
    configuration string can become code.
    """
    if not text or not text.strip():
        raise ConfigError("a declared command is empty")

    argv, current, started, quote = [], [], False, None
    for ch in text:
        if quote is not None:
            if ch == quote:
                quote = None
            else:
                current.append(ch)
            continue
        if ch in "\"'":
            quote = ch
            started = True
        elif ch in _SEPARATORS:
            if started:
                argv.append("".join(current))
                current, started = [], False
        else:
            current.append(ch)
            started = True

    if quote is not None:
        # Refused rather than guessed at. Closing the quote would be a guess,
        # and a wrong guess here is a source that looks configured and then
        # fails at run time -- which is the failure this function exists to
        # prevent, reproduced one level up.
        raise ConfigError(
            f"declared command {text!r} could not be tokenised: unterminated "
            f"{quote} quote. Nothing is expanded or escaped here, so quote the "
            f"whole argument if it contains a space: "
            f'"C:\\path with spaces\\prog.exe"')
    if started:
        argv.append("".join(current))
    if not argv:
        raise ConfigError(f"declared command {text!r} has no executable")
    return argv


def configured_sources(settings):
    """The live perception sources this configuration enables.

    Returned in the declared tier order so ``/health`` and the refusal
    messages describe the chain the way it actually runs. An unparseable
    command raises rather than being skipped: a source the operator asked
    for and cannot run is a configuration fault, not an absence that looks
    identical to "not configured".
    """
    sources = []
    if settings.cli_command:
        sources.append(CliSource(parse_argv(settings.cli_command)))
    if settings.mcp_command and settings.mcp_tool:
        sources.append(McpSource(parse_argv(settings.mcp_command),
                                 settings.mcp_tool))
    if settings.dom_url:
        sources.append(DomSource(settings.dom_url))
    if settings.screen_enabled:
        # The vision tier is separate from the structured tiers because it is
        # the only one that spends money and the only one that cannot be
        # re-derived from state that was already available exactly.
        sources.append(ScreenSource())
    return sources


def configured_pools(settings, *, budget, audit):
    """Extractor pools keyed by the target class each one serves.

    Each pool is sized by the configured quorum, which is what makes the
    declared tiers usable rather than merely present: a pool with fewer slots
    than the quorum can never satisfy it, so a driver built from settings
    would refuse every step with ``insufficient_agreement`` the moment an
    operator asked for a third opinion. The structured slots are
    deterministic and free, so the two opinions the default quorum wants cost
    nothing -- which is the practical payoff of the tier ordering.
    """
    slots = max(1, int(settings.quorum))
    pools = {cls: ExtractorPool(
        [StructuredExtractor(f"{cls}-{i}", _declared_reader())
         for i in range(slots)], serves=(cls,))
        for cls in (CLI, MCP, DOM)}
    if settings.screen_enabled:
        pools[GUI] = build_vision_pool(settings, budget=budget, audit=audit,
                                       slots=slots)
    return pools


def _declared_reader():
    """A reader that reports only the schema's declared fields, and only
    those the payload actually contains.

    It does not default a missing field and it does not carry an undeclared
    one through. Both are the same mistake in opposite directions: inventing
    a value the source never saw, and passing along something outside the
    contract. What the source genuinely cannot supply stays absent, so the
    tally reports a shortfall rather than agreement nobody observed.
    """
    def read(capture, schema):
        payload = capture.payload
        if not isinstance(payload, dict):
            return None
        declared = frozenset(schema.field_names())
        return {k: v for k, v in payload.items() if k in declared}

    return read
