"""Foreign wire formats: how this package reads what somebody else wrote.

Two formats, both of which arrive as text and both of which have a specific
right answer about what counts as data:

* **JSON-RPC over stdio**, as an MCP server speaks it -- a stream with
  banners and progress lines in it, where the last well-formed response is
  the one that counts (:func:`_last_reply`), and where a ``tools/call`` result
  has either structured content or a text rendering, of which only the first
  is read (:func:`_tool_payload`).
* **HTML**, as a document server renders it -- where the answerable text is
  the title and the body, and the script and style bodies are the parts of a
  page most likely to contain the very words a schema asks about
  (:func:`read_document`).

Neither parser knows what a source is, what a target is, or what a tier is.
Each takes text and returns a plain dict or ``None``, and refusing is a
normal return value rather than an exception: a malformed document or an
unparseable stream is an observation that came back empty, not a crash in the
middle of a step.

This module has no dependency on :mod:`driver_core.adapters` or
:mod:`driver_core.chain`, so a scraping bug is findable here rather than
beside tier selection, and neither of those modules has to know a line of HTML
or JSON-RPC.
"""
import json
from html.parser import HTMLParser


def _last_reply(stdout):
    """The final JSON-RPC response on a server's stdout.

    A server is free to print banners and progress lines, so the stream is
    scanned line by line and the last well-formed response wins. Parsing the
    whole stream as one document -- the obvious implementation -- breaks on
    the first server that prints a ready-marker, which is most of them.
    """
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict) and payload.get("jsonrpc"):
            return payload
    return None


def _tool_payload(result):
    """The structured content of a ``tools/call`` result, or ``None``.

    Text content blocks are deliberately *not* coerced. They are the MCP
    equivalent of reading pixels -- a lossy rendering a program has to parse
    by guessing -- and parsing one into fields here would reintroduce the
    unreliable tier at the end of a chain whose whole argument is that the
    structured tiers are not guesses.
    """
    if not isinstance(result, dict):
        return None
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and structured:
        return structured
    return None


class _DocumentReader(HTMLParser):
    """The HTML reader, at module level rather than inside the function.

    A closure would have worked and cost the same, but a parser defined
    inside its own entry point cannot be read without also reading the
    function that calls it, cannot be exercised on its own, and looks like
    part of whatever the function does next. It is a component, so it is a
    class at module level with the module as its only context.
    """

    def __init__(self, title_tag):
        super().__init__(convert_charrefs=True)
        self.title_tag = title_tag
        self.title = []
        self.text = []
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        if tag == self.title_tag:
            self._in_title = True

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        if tag == self.title_tag:
            self._in_title = False

    def handle_data(self, data):
        if self._skip:
            return
        text = " ".join(data.split())
        if not text:
            return
        if self._in_title:
            self.title.append(text)
        else:
            self.text.append(text)


def read_document(body, *, title_tag="title"):
    """Extract the declared fields from an HTML document.

    Returns ``{"window_title": ..., "visible_text": ...}``. Script and style
    bodies are dropped: they are the parts of a page most likely to contain
    the word "error" or "Save" in a string literal, and a vision model does
    not read them either -- leaving them in would make this tier disagree
    with the pixel tier for no good reason.
    """
    reader = _DocumentReader(title_tag)
    reader.feed(body or "")
    reader.close()
    payload = {}
    title = " ".join(reader.title).strip()
    if title:
        payload["window_title"] = title
    visible = " ".join(reader.text).strip()
    if visible:
        payload["visible_text"] = visible
    return payload
