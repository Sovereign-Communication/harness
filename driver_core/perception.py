"""The perception tier's public surface, in one import.

Nothing is defined here. This module is the **façade**: the seam, kept stable
so that a caller who has learned where things live does not have to learn it
again. Every name below is the same object, with the same name and the same
behaviour, that it was when the whole tier fitted in one file.

Each name has exactly one owner, and that owner is the module a change belongs
in:

* :mod:`driver_core.observation` -- the target classes, ``Target``,
  ``Capture``, ``StructuredSource``. Knows nothing about tier order,
  adapters or wire formats.
* :mod:`driver_core.adapters` -- ``CliSource``, ``McpSource``,
  ``DomSource``, ``ScreenSource``. Knows nothing about the order, the other
  adapters, or tier selection.
* :mod:`driver_core.parsing` -- ``read_document``, ``_last_reply``,
  ``_tool_payload``. Knows nothing about sources, targets or tiers.
* :mod:`driver_core.chain` -- ``select_capture``: which source answers, and
  what a refusal says. Knows no concrete adapter.

The dependency arrows all point one way, downward, into
:mod:`driver_core.observation`: adapters import the vocabulary, the chain
imports the vocabulary, parsing imports nothing from this package, and nothing
imports the chain except :class:`~driver_core.driver.Driver`. So an adapter
genuinely cannot see the order even if it wanted to, and a scraping bug is not
findable next to tier selection.

The split is also what keeps the OS boundary meaningful. Every adapter reaches
a subprocess, a socket or a screen through :mod:`driver_core.osal` and nothing
else, and the AST scan that proves it walks the top-level modules of this
package -- so all four of these files are scanned, unchanged and unloosened.
"""
from .adapters import CliSource, DomSource, McpSource, ScreenSource
from .chain import select_capture
from .observation import (
    CLI, DOM, GUI, MCP, SCREEN_SOURCE, SOURCE_ORDER, STRUCTURED_CLASSES,
    TARGET_CLASSES, VISION_CLASS, Capture, StructuredSource, Target, as_target,
    fingerprint,
)
from .parsing import _last_reply, _tool_payload, read_document

__all__ = [
    # vocabulary
    "SOURCE_ORDER", "TARGET_CLASSES", "STRUCTURED_CLASSES", "VISION_CLASS",
    "SCREEN_SOURCE", "CLI", "MCP", "DOM", "GUI",
    "Target", "as_target", "Capture", "fingerprint", "StructuredSource",
    # adapters
    "CliSource", "McpSource", "DomSource", "ScreenSource",
    # wire formats
    "read_document", "_last_reply", "_tool_payload",
    # policy
    "select_capture",
]
