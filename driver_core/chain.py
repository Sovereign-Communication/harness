"""Which source answers: the one place the tier order is decided.

This module owns the single most consequential decision in the perception
tier -- that **structured input is tried before pixels**, every time.

A CLI's stdout, an MCP tool's JSON result, and a document's readable text are
all already the state a human would reason about, in a form a program can
read exactly. A screenshot of the same application is a lossy, unlabelled
rendering of it that costs real money to interpret and can be read two
different ways by two different extractors. When a structured source exists
it is strictly better on every axis that matters here -- determinism,
testability, cost, and accuracy.

So the order is not an optimisation, it is the design:

    CLI  ->  MCP  ->  DOM  ->  screen pixels

and pixels are reached only when the three structured sources cannot answer.
That single choice is what keeps most runs free, hermetic, and reproducible,
and it is why the vision adapter is a small, isolated, last-resort component
rather than the core of the system.

**The ordering is structural, not advisory.** An earlier version tried the
structured tiers in order and then fell back to a screen capture -- correct in
practice, but the guarantee lived in the shape of a ``for`` loop, so nothing
stopped a caller registering a screen source that answered a DOM target, and
nothing could *prove* the claim. Three of the four target classes can be
served without pixels at all, and that deserves to be a property of the types
rather than a promise in a docstring:

* a :class:`~driver_core.observation.Target` carries the class it was declared
  to be (:data:`~driver_core.observation.CLI`,
  :data:`~driver_core.observation.MCP`, :data:`~driver_core.observation.DOM`
  or :data:`~driver_core.observation.GUI`);
* every source declares which classes it can answer, in ``serves``;
* :func:`select_capture` builds its candidate set *first* and filters by
  class, so for a ``cli``, ``mcp`` or ``dom`` target the screen source is never
  a candidate and is therefore never called.

The consequence is that "this run never spent a vision token" is checkable
rather than aspirational: a source that would have violated the ordering
cannot be reached, and a test can assert it with a source that raises if
touched.

This module knows no adapter. It is handed a list of things that have a
``name``, a ``serves`` and a ``capture``, and it never imports
:mod:`driver_core.adapters` -- which is what makes "an adapter cannot see the
order" true rather than aspirational, and what lets a new source be added
without editing the policy. It reads the order from
:data:`~driver_core.observation.SOURCE_ORDER` and the class names from the
same place, and those are the only things it knows about the tier.

The refusals are part of the policy, not an afterthought: an operator who is
told "every source declined" and an operator who is told "no source serves
that class" have different problems and different fixes, and the message is
the only place that can tell them apart.
"""
from .errors import PerceptionUnavailable
from .observation import (
    SCREEN_SOURCE, SOURCE_ORDER, VISION_CLASS,
    as_target,
)


def select_capture(target, sources, *, prefer=()):
    """Return the first usable capture, structured sources first.

    ``prefer`` reorders *within* the structured set only. It cannot promote
    pixels past a structured source, because that would reintroduce exactly
    the cost and unreliability the ordering exists to avoid -- and it cannot
    promote anything at all past a class it does not serve.

    When nothing answers, the refusal names what was tried and, crucially,
    whether the screen tier was *excluded by class* or merely declined. Those
    are different operational problems: the first means the caller declared
    the wrong class, and the second means the machine genuinely had nothing.
    A declared class that nothing serves is a third fact again -- *nothing was
    tried* -- and an operator who configured a CLI source and asked for
    pixels has a wiring mistake rather than an observation failure, so the
    message says which of the three happened.
    """
    target = as_target(target)
    candidates = [s for s in sources if s.can_serve(target)]
    excluded = [s.name for s in sources if not s.can_serve(target)]

    structured_order = [s for s in SOURCE_ORDER if s != SCREEN_SOURCE]
    structured_order.sort(
        key=lambda name: prefer.index(name) if name in prefer else len(prefer))
    tried = []

    for name in structured_order:
        for source in candidates:
            if source.name != name:
                continue
            capture = source.capture(target)
            tried.append((source.name, capture.detail or "declined"))
            if capture.ok:
                return capture

    for source in candidates:
        if source.name != SCREEN_SOURCE:
            continue
        capture = source.capture(target)
        tried.append((source.name, capture.detail or "declined"))
        if capture.ok:
            return capture

    attempted = ", ".join(f"{name}: {detail}" for name, detail in tried) or "none"
    declared_on = sorted({s.name for s in sources}) or ["none"]

    if target.declared and not candidates:
        # Nothing was even tried, because every configured source serves some
        # other class. "Every source declined" would be a lie about what
        # happened and would send an operator to inspect the wrong tier; the
        # fix is to configure the class they actually asked for.
        note = ""
        if excluded:
            note += (f"; excluded because they serve another class: "
                     f"{sorted(set(excluded))}")
        if SCREEN_SOURCE in excluded:
            note += (f" The screen tier serves {VISION_CLASS!r} only, so it was "
                     f"therefore not reached.")
        raise PerceptionUnavailable(
            f"target {target.ref!r} is declared {target.target_class!r} and no "
            f"configured source serves that class; nothing was tried. "
            f"Configured sources: {declared_on}{note}.")

    if target.declared and target.structured and SCREEN_SOURCE in excluded:
        # The loud, correct version: pixels were not merely unhelpful here,
        # they were unreachable. Saying so is what turns a confusing stop
        # into an obvious fix (declare the real class, or register a source
        # that serves it).
        raise PerceptionUnavailable(
            f"target {target.ref!r} is declared {target.target_class!r}, which "
            f"is answerable without pixels; the screen tier was therefore not "
            f"reached. Tried: {attempted}. Configured sources: {declared_on}.")

    raise PerceptionUnavailable(
        f"no source could observe the target {target.ref!r}; every source "
        f"either declined or failed. Tried: {attempted}. Configured "
        f"sources: {declared_on}")
