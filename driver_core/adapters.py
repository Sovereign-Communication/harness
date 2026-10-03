"""Concrete sources: how to read one system, and nothing else.

Each class here answers a single question -- *given this target, what did that
system say?* -- and knows nothing about the other tiers, the order they are
tried in, or what happens when it declines. It does not need to. A reader of
this file learns what a CLI probe, an MCP tool call, a fetched document and a
screenshot each produce, and nothing else about the perception tier.

That ignorance is the point rather than a limitation. An adapter that could
see the order could be written to prefer itself; one that cannot be asked to
is not a thing a future change can do by accident. The vocabulary they share
-- the class names, :class:`~driver_core.observation.Target`,
:class:`~driver_core.observation.Capture` -- lives in
:mod:`driver_core.observation`, and the choice between them lives in
:mod:`driver_core.chain`.

Two of them decode something and delegate the decoding rather than doing it:
:class:`McpSource` hands a stream to
:func:`~driver_core.parsing._last_reply` and
:func:`~driver_core.parsing._tool_payload`, and :class:`DomSource` hands a
document to :func:`~driver_core.parsing.read_document`. Neither module
imports the other, so a change to either a wire format or a source is a
change to one file.

Every one of them reaches the outside world through
:mod:`driver_core.osal` and nothing else, which is what lets a test prove
that no adapter here is doing its own process, socket or screen handling.
"""
import base64
import json

from . import osal
from .errors import PerceptionUnavailable
from .observation import (
    CLI, DOM, GUI, MCP, SCREEN_SOURCE, Capture, fingerprint,
)
from .parsing import _last_reply, _tool_payload, read_document

#: How many argv entries the launch diagnostic echoes before it abbreviates.
#: The first entry is the one that failed; the rest exist so an operator can
#: see where a declared command was split.
_ARGV_ECHO = 6


def _launch_failure(prefix, result, argv):
    """Say what was actually attempted, not just that it was not found.

    A bare ``not found: C:Python314python.exe`` is the least useful message
    this package can produce: it names a path the operator never typed and
    gives them nothing to compare it against. Echoing the resolved argv turns
    it into a diagnosis -- when the first entry looks truncated, or when a
    path has clearly been split across two entries, the cause is visible in
    the same line as the failure.

    Nothing here guesses what went wrong. It reports what was run and names
    the two things that produce this failure, so the operator decides.
    """
    detail = f"{prefix}: {result.error}"
    if result.reason != "not_found":
        return detail
    shown = list(argv[:_ARGV_ECHO])
    if len(argv) > _ARGV_ECHO:
        shown.append(f"... and {len(argv) - _ARGV_ECHO} more")
    detail = f"{detail} -- attempted {shown!r}"
    if _path_like(argv[0] if argv else ""):
        detail += (
            ". A declared command is split on whitespace with nothing "
            "expanded or escaped, so quote any argument containing a space: "
            '"C:\\path with spaces\\prog.exe"')
    return detail


def _path_like(token):
    """Whether a token reads as a path rather than a bare command name.

    Only decides whether *mentioning* the quoting fix is worth it. Missing a
    case only costs a slightly plainer message; claiming a path where there
    is none would send an operator after the wrong problem, so it asks for a
    drive prefix or a separator rather than inferring from length or dots.
    """
    if not token:
        return False
    drive = len(token) >= 2 and token[0].isalpha() and token[1] == ":"
    return drive or "/" in token or "\\" in token


class ScreenSource:
    """The last resort: pixels.

    Serves :data:`GUI` and only :data:`GUI`. That single declaration is what
    keeps the vision tier out of reach for the three structured classes: a
    ``dom`` target cannot reach this object, so it cannot be called, billed,
    or quietly preferred.

    Registration is explicit and failure is named. A driver that cannot
    capture returns a reason, and the caller routes to a structured source or
    stops -- it never hands a vision model a path that does not exist.
    """

    serves = (GUI,)

    def __init__(self, *, encoder=None):
        self.name = SCREEN_SOURCE
        self._encoder = encoder or _default_encoder

    def can_serve(self, target):
        # Same rule as StructuredSource, but it cannot be overridden: this is
        # the one source whose reachability is the ordering guarantee.
        if not target.declared:
            return True
        return target.target_class == GUI

    def capture(self, target=None):
        path, detail = osal.capture_screen()
        if not path:
            return Capture(SCREEN_SOURCE, target, None, detail=detail)
        try:
            payload = self._encoder(path)
        except Exception as exc:
            return Capture(SCREEN_SOURCE, target, None,
                           detail=f"could not read the capture: {exc}")
        if payload is None:
            return Capture(SCREEN_SOURCE, target, None,
                           detail="capture produced no readable bytes")
        return Capture(SCREEN_SOURCE, target, payload,
                       fingerprint=fingerprint(payload))


class CliSource:
    """Tier 1: a real subprocess, read through :mod:`driver_core.osal`.

    A command either produced output or it did not. There is no model, no
    pixels and no interpretation in this tier, which is why it is first and
    why three of the four target classes can avoid the vision extractor
    entirely.

    The target is appended as its **own argv element** rather than
    substituted into the command. There is no shell here, so this is not a
    shell-injection surface -- but splicing a caller-supplied string into the
    middle of a command *is* argument injection if the target happens to look
    like a flag. Appending it means a hostile target can only ever be one
    argument, which is a much smaller thing to reason about.
    """

    name = CLI
    serves = (CLI,)

    def __init__(self, command, *, cwd=None, timeout=None, extra_env=None):
        if isinstance(command, str):
            raise PerceptionUnavailable(
                "CliSource takes an argv list, not a string; a string would "
                "be handed to the platform shell")
        argv = list(command)
        if not argv:
            raise PerceptionUnavailable("CliSource needs a non-empty command")
        self.command = argv
        self.cwd = cwd
        self.timeout = timeout
        self.extra_env = dict(extra_env or {})

    def can_serve(self, target):
        if not target.declared:
            return True
        return target.target_class == CLI

    def capture(self, target):
        argv = list(self.command) + [str(target)]
        kwargs = {}
        if self.cwd is not None:
            kwargs["cwd"] = self.cwd
        if self.timeout is not None:
            kwargs["timeout"] = self.timeout
        if self.extra_env:
            kwargs["env"] = self.extra_env
        result = osal.run(argv, **kwargs)
        if not result.ok and result.reason:
            # "not found" and "timed out" are failures to ask, not answers.
            return Capture(self.name, target, None,
                           detail=_launch_failure("command did not run",
                                                  result, argv))
        return Capture(
            self.name, target,
            {"exit_code": result.returncode,
             "stdout": result.stdout,
             "stderr": result.stderr},
            fingerprint=fingerprint({"exit_code": result.returncode,
                                     "stdout": result.stdout,
                                     "stderr": result.stderr}))


class McpSource:
    """Tier 2: a real MCP tool call over stdio JSON-RPC.

    Speaks newline-delimited JSON-RPC to a declared child process through
    :func:`driver_core.osal.run`: ``initialize``, then
    ``notifications/initialized``, then ``tools/call``. The handshake is not
    ceremony -- a compliant server is entitled to refuse a ``tools/call``
    that was never initialised, and this package would then report an empty
    result as though the tool had answered.

    Zero dependencies is the whole reason this is spelled out rather than
    delegated: an MCP client is the sort of thing that arrives as a package,
    and a driver that acts on a machine is the last place to add one.
    """

    name = MCP
    serves = (MCP,)

    def __init__(self, command, tool, *, timeout=30, extra_env=None,
                 client_name="driver-core"):
        if isinstance(command, str):
            raise PerceptionUnavailable(
                "McpSource takes an argv list, not a string")
        argv = list(command)
        if not argv:
            raise PerceptionUnavailable("McpSource needs a non-empty command")
        self.command = argv
        self.tool = tool
        self.timeout = timeout
        self.extra_env = dict(extra_env or {})
        self.client_name = client_name

    def can_serve(self, target):
        if not target.declared:
            return True
        return target.target_class == MCP

    def _envelope(self, payload):
        """Newline-delimited JSON-RPC, which is the MCP stdio framing.

        *Writing* the frame belongs to the adapter that holds the
        conversation; :mod:`driver_core.parsing` is about reading what
        somebody else wrote, and a stream it cannot read is one this module
        has already produced.
        """
        return json.dumps(payload) + "\n"

    def capture(self, target):
        request = "".join((
            self._envelope({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05",
                           "capabilities": {},
                           "clientInfo": {"name": self.client_name,
                                          "version": "1.0.0"}}}),
            self._envelope({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            self._envelope({
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": self.tool,
                           "arguments": {"target": str(target)}}}),
        ))
        kwargs = {"timeout": self.timeout, "input_text": request}
        if self.extra_env:
            kwargs["env"] = self.extra_env
        result = osal.run(self.command, **kwargs)
        if not result.ok and result.reason:
            return Capture(self.name, target, None,
                           detail=_launch_failure("mcp server did not run",
                                                  result, self.command))
        reply = _last_reply(result.stdout)
        if reply is None:
            return Capture(self.name, target, None,
                           detail="mcp server returned no parseable response")
        if "error" in reply:
            return Capture(self.name, target, None,
                           detail=f"mcp error: {reply['error']}")
        payload = _tool_payload(reply.get("result"))
        if payload is None:
            return Capture(self.name, target, None,
                           detail="mcp tool returned no structured content")
        return Capture(self.name, target, payload, fingerprint=fingerprint(payload))


class DomSource:
    """Tier 3: the document tree, read without a browser engine.

    Fetches a declared URL and extracts the document title and visible text
    with the standard library's own HTML parser -- no engine, no headless
    browser, no dependency. That is enough to answer a declared schema for a
    DOM target, which is the point: if a DOM target can be answered here,
    it never reaches pixels.

    The target is **not** substituted into the URL. Splicing a caller string
    into a URL is how a driver ends up fetching whatever it was told to, and
    a declared URL is both safer and more auditable. The target is carried as
    a label on the capture, which is all it is needed for.
    """

    name = DOM
    serves = (DOM,)

    def __init__(self, url, *, timeout=20, opener=None, title_tag="title"):
        self.url = url
        self.timeout = timeout
        self._opener = opener
        self.title_tag = title_tag

    def can_serve(self, target):
        if not target.declared:
            return True
        return target.target_class == DOM

    def capture(self, target):
        opener = self._opener or osal.http_get
        try:
            status, body = opener(self.url, timeout=self.timeout)
        except Exception as exc:
            return Capture(self.name, target, None,
                           detail=f"document could not be fetched: {exc}")
        if status != 200:
            return Capture(self.name, target, None,
                           detail=f"document returned HTTP {status}")
        try:
            payload = read_document(body, title_tag=self.title_tag)
        except Exception as exc:
            return Capture(self.name, target, None,
                           detail=f"document could not be read: {exc}")
        if not payload:
            return Capture(self.name, target, None,
                           detail="document had no readable text")
        return Capture(self.name, target, payload, fingerprint=fingerprint(payload))


def _default_encoder(path):
    with open(path, "rb") as handle:
        return base64.b64encode(handle.read()).decode("ascii")
