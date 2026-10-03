"""The shared vocabulary of the perception tier: what is looked at, and what
looked.

This module owns the nouns every other part of the tier speaks in:

* the four **target classes** -- ``cli``, ``mcp``, ``dom``, ``gui`` -- and the
  three of them that are answerable from structured input;
* the four **source names**, which are *not* the same strings: a source named
  ``screen`` is how pixels enter, and the class it serves is ``gui``;
* :class:`Target`, which carries the class a caller declared, and
  :class:`Capture`, which carries one observation and never its payload's
  contents into a log;
* :class:`StructuredSource`, the contract an adapter implements.

What this module deliberately does not contain is the reason any of it
exists. There is no tier *order* here, no concrete adapters, and no foreign
wire formats. :mod:`driver_core.chain` decides which source answers,
:mod:`driver_core.adapters` knows how to read one system, and
:mod:`driver_core.parsing` knows how to decode one foreign format. Each needs
this vocabulary and none needs the others, which is the whole reason they are
apart: a change to how the chain picks a source does not require reading a
document parser, and a scraping bug is not sitting beside tier selection
where it can be mistaken for part of it.

The one structural claim that belongs here rather than in the chain is that
**three of the four classes need no pixels**.
:data:`STRUCTURED_CLASSES` declares it as data so the guarantee can be
asserted against a declaration rather than a comment.

A bare string target is still accepted, and is deliberately *weaker*: an
undeclared class permits any source, with pixels last. That preserves every
existing caller while making the strong form available to callers who want
it, and it is why :class:`Target` exists rather than the string having been
changed outright.

Every capture is **opaque and short-lived**. A path, a handle, a base64 blob
-- whichever the source produced -- is passed forward and never copied into
an audit record, because a log of screen captures is a log of everything the
operator has looked at.
"""
import hashlib

from .errors import PerceptionUnavailable

#: The four target classes the driver can be pointed at. Three are answerable
#: from structured input; only :data:`GUI` has no structured representation
#: this package can read without a browser engine.
CLI = "cli"
MCP = "mcp"
DOM = "dom"
GUI = "gui"

TARGET_CLASSES = (CLI, MCP, DOM, GUI)

#: The classes that never need pixels. Declared as data so the guarantee can be
#: asserted against the declaration rather than against a comment.
STRUCTURED_CLASSES = (CLI, MCP, DOM)

#: The one class that falls through to a screen capture.
VISION_CLASS = GUI

#: The name of the pixels source. Declared separately from :data:`GUI`
#: because a *source* and a *target class* are different things, and the
#: earlier version that treated ``"screen"`` as an inline string is how the
#: two came to be confused about what a request was asking for.
SCREEN_SOURCE = "screen"

#: The source names, in the order the chain tries them. Data, not policy:
#: :mod:`driver_core.chain` reads the order and nothing here reads the chain.
#: Note that ``gui`` is absent and ``screen`` present.
SOURCE_ORDER = (CLI, MCP, DOM, SCREEN_SOURCE)



class Target:
    """What to observe, and the class it was declared to be.

    The class is the load-bearing half. Without it this is just a string,
    and the only thing the chain can do is try things in order until one
    answers -- which is a weaker property, because "tried in order" is a
    statement about the code path taken rather than about what was reachable.

    ``target_class=None`` means undeclared, and is accepted for compatibility
    with callers that pass a plain string. It permits any source to answer,
    pixels last. Prefer a declared class: it is the difference between
    "pixels were not needed" and "pixels were not reached".
    """

    __slots__ = ("ref", "target_class")

    def __init__(self, ref, target_class=None):
        if target_class is not None and target_class not in TARGET_CLASSES:
            raise PerceptionUnavailable(
                f"unknown target class {target_class!r}; declared classes are "
                f"{list(TARGET_CLASSES)}")
        self.ref = ref
        self.target_class = target_class

    @property
    def declared(self):
        return self.target_class is not None

    @property
    def structured(self):
        """True when this class is answerable without pixels."""
        return self.target_class in STRUCTURED_CLASSES

    def to_dict(self):
        return {"target": self.ref, "class": self.target_class}

    def __str__(self):
        return str(self.ref)

    def __repr__(self):
        return f"Target({self.ref!r}, {self.target_class!r})"



def as_target(value):
    """Accept either a :class:`Target` or a bare string, unchanged behaviour."""
    if isinstance(value, Target):
        return value
    return Target(value, None)



class Capture:
    """One observation of the target.

    ``fingerprint`` is a short digest of the bytes observed. It exists so two
    steps can be proven to have looked at the *same* screen without storing
    the screen -- which is how a re-plan after an extraction disagreement can
    tell "the window changed under us" from "the extractors just disagreed
    about an unchanged window".
    """

    __slots__ = ("source", "target", "target_class", "payload", "detail",
             "fingerprint")

    def __init__(self, source, target, payload=None, detail="", fingerprint=""):
        self.source = source
        # Coerced here rather than trusted from callers, because this value
        # is written straight into the audit log. A Target object reaching
        # json.dumps would raise at the moment a capture succeeded, which is
        # the worst possible time to discover it.
        self.target = target.ref if isinstance(target, Target) else target
        #: Carried alongside the capture so the extraction tier can select a
        #: pool without re-deriving the target the caller handed in.
        self.target_class = (target.target_class
                             if isinstance(target, Target) else None)
        self.payload = payload
        self.detail = detail
        self.fingerprint = fingerprint

    @property
    def ok(self):
        return self.payload is not None

    @property
    def cost_usd(self):
        """A capture's own cost. Zero for every structured tier."""
        return 0.0

    def summary(self):
        """A description safe to write to a log. Never carries the payload."""
        return {"source": self.source, "target": self.target,
                "ok": self.ok, "detail": self.detail,
                "fingerprint": self.fingerprint}

    def __repr__(self):
        return f"Capture({self.source!r}, ok={self.ok})"



def fingerprint(payload):
    """A short, stable digest of an observed payload."""
    if payload is None:
        return ""
    if isinstance(payload, bytes):
        material = payload
    else:
        material = str(payload).encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:16]



class StructuredSource:
    """A source that already has the state in machine-readable form.

    ``reader`` is a callable ``(target) -> payload | None``, and ``target``
    is the plain ref -- never a :class:`Target`. The declared class governs
    which sources the chain will *ask*; it is not something a reader needs
    in order to read.

    ``serves`` declares which target classes this source can answer. It
    defaults to the source's own name, so ``StructuredSource("dom", ...)``
    serves ``dom`` and nothing else unless told otherwise.
    """

    def __init__(self, name, reader, *, serves=None, cost_usd=0.0):
        if name not in SOURCE_ORDER:
            raise PerceptionUnavailable(
                f"unknown structured source {name!r}; declared sources are "
                f"{list(SOURCE_ORDER)}")
        self.name = name
        self._reader = reader
        self.cost = float(cost_usd)
        self.serves = self._declared_serves(name, serves)

    @staticmethod
    def _declared_serves(name, serves):
        if serves is None:
            # A structured source serves its own class. Declaring it here
            # rather than defaulting to "everything" is what makes the
            # ordering enforceable instead of merely intended.
            return (name,) if name in TARGET_CLASSES else ()
        unknown = sorted(set(serves) - set(TARGET_CLASSES))
        if unknown:
            raise PerceptionUnavailable(
                f"source {name!r} serves unknown target class(es) {unknown}; "
                f"declared classes are {list(TARGET_CLASSES)}")
        return tuple(serves)

    def can_serve(self, target):
        """Whether this source is even a candidate for ``target``.

        An undeclared target class permits anything -- that is the
        compatibility path, and it is why a bare string is weaker. A declared
        class is a commitment: a source that does not serve it is not asked,
        so it cannot be reached, paid for, or accidentally preferred.
        """
        if not self.serves:
            return False
        if not target.declared:
            return True
        return target.target_class in self.serves

    def capture(self, target):
        # The reader is handed the *ref*, not the Target. A reader wants the
        # thing being observed; the declared class is bookkeeping that
        # concerns the chain, not the source. Passing the Target through
        # would break every reader that does anything real with the value --
        # open(path), argv.append(target), urljoin(base, target) -- with a
        # TypeError from somewhere deep inside a caller's code.
        ref = target.ref if isinstance(target, Target) else target
        try:
            payload = self._reader(ref)
        except Exception as exc:
            return Capture(self.name, target, None,
                           detail=f"reader failed: {exc}")
        if payload is None:
            return Capture(self.name, target, None, detail="source had nothing")
        return Capture(self.name, target, payload,
                       fingerprint=fingerprint(payload))

