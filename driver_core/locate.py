"""Locating things in a screen: presence, a box, and an honest stop.

A Box answer says *where* -- four edges as fractions of the image -- and
this module is the discipline around it, because the primitive has two
sharp edges the docs are explicit about:

* **A box is never "not found".** A Box question about something absent
  still returns four edges. So every target gets a presence :func:`noul`
  beside its box, in the same request, and a low presence answer voids
  the box rather than reporting a location for something that is not
  there -- the "a slot that did not answer is not a vote" discipline
  applied to presence.
* **A box can cut its target.** The docs' repair is a deterministic
  escalation ladder driven by the box's own confidence: below the floor,
  ask one Noul per edge ("does the target continue past this edge?"),
  move out every edge answered yes, and ask again. The docs caught 30 of
  36 known cuts this way. A repaired box that still lands below the floor
  is an honest ``confidence_below_threshold`` stop: a location that is
  probably wrong is reported as not located, never as a location.

A box is an *observation*. Nothing here acts on one: coordinates reaching
an executor is a different subsystem's problem and a later decision.

The ladder is pure. :func:`locate` takes an ``ask`` callable that performs
one billed dispatch and returns the answers map, so every rung is testable
without a model, a screen, or a budget -- the wiring lives on the pixels
extractor that owns the capture.

This is a build against the *dreamer-ready* image surface: the request
format and answers can change between releases, and the thresholds below
are the docs' own worked examples ("tune it on your images"), not
measurements of this system's screens.
"""
from . import transport

#: The box's four edges, in the endpoint's own names. Fractions of the
#: image measured from the top-left: left/right of the width, top/bottom
#: of the height. ``left < right`` and ``top < bottom`` are invariants.
EDGES = ("left", "top", "right", "bottom")

#: Below this presence probability the box is ignored and the target is
#: reported absent. The docs' rule: ignore the box when the companion
#: Noul answer is low.
PRESENCE_FLOOR = 0.5

#: Below this box confidence the repair ladder runs. The docs publish no
#: recommended threshold ("there is no recommended threshold yet, so tune
#: it on your own images"); 0.6 is their worked example.
MIN_BOX_CONFIDENCE = 0.6

#: How far a cut edge moves out before the box is asked again. The docs'
#: pad constant ("fraction of the image to add on each side. Tune for
#: your images"), reused for the same reason it was published: a crop
#: that must not cut the target cannot be tightened by guessing.
BOX_REPAIR_STEP = 0.05


class LocatedTarget:
    """What is known about one target's location, and why it is not more."""

    __slots__ = ("target", "presence", "box", "confidence", "repaired",
                 "cut_edges", "reason")

    def __init__(self, target, *, presence=None, box=None, confidence=None,
                 repaired=False, cut_edges=(), reason=""):
        self.target = target
        #: The companion Noul's answer, or None when it was not answered.
        self.presence = presence
        #: Four edges as image fractions -- only ever a box this system can
        #: stand behind. ``None`` is "not located", which is the honest
        #: answer far more often than a guessed rectangle.
        self.box = box
        self.confidence = confidence
        #: True when the edge-question ladder repaired the answer.
        self.repaired = bool(repaired)
        #: Which edges the ladder found the first box cutting.
        self.cut_edges = tuple(cut_edges)
        #: Why this target is not located. Empty when it is.
        self.reason = reason

    @property
    def located(self):
        return self.box is not None

    def to_dict(self):
        return {"target": self.target, "presence": self.presence,
                "box": self.box, "confidence": self.confidence,
                "repaired": self.repaired,
                "cut_edges": list(self.cut_edges), "reason": self.reason}

    def __repr__(self):
        return (f"LocatedTarget({self.target!r}, "
                f"{'located' if self.located else self.reason or 'no box'})")


def _present_key(target):
    return f"present::{target}"


def _box_key(target):
    return f"box::{target}"


def _edge_key(target, edge):
    return f"edge::{target}::{edge}"


def _rebox_key(target):
    return f"rebox::{target}"


def locate_questions(targets, image="screen"):
    """Rung one: a presence Noul and a Box per target, in one request.

    Batched on purpose -- the parallel-questions pattern measured 12.2x
    cheaper and 10.0x faster than one call per question -- and the two
    questions per target always travel together, because either one alone
    is a half-answer the docs warn against.
    """
    questions = {}
    for target in targets:
        questions[_present_key(target)] = transport.noul(
            f"Is there a {target} in `{image}`?",
            true=f"A {target} is visible in `{image}`.",
            false=f"There is no {target} in `{image}`.")
        questions[_box_key(target)] = transport.box(
            f"the {target} in `{image}`")
    return questions


def edge_questions(target, box, image="screen"):
    """Rung two: one Noul per edge -- does the target continue past it?

    The docs ask these on a crop of the box ("does the mug in `crop`
    continue past the left edge of the image?"). This build has no crop
    step within this pure ladder -- the pixels judged are the current
    prepared image -- so the region is named numerically; the judgment is the
    docs' own, one edge at a time. The extraction caller separately searches
    bounded source-resolution crops if this ladder exhausts its repair.
    """
    questions = {}
    for edge in EDGES:
        questions[_edge_key(target, edge)] = transport.noul(
            f"Consider the region of `{image}` bounded by left "
            f"{box['left']:.3f}, right {box['right']:.3f}, top "
            f"{box['top']:.3f}, bottom {box['bottom']:.3f} (fractions of "
            f"the image). Does the {target} continue past the {edge} edge "
            f"of that region?",
            true=f"The {target} is cut at the {edge} edge; part of it lies "
                 f"outside the region.",
            false=f"The whole {target} lies within the {edge} edge.")
    return questions


def rebox_question(target, box, cut_edges, image="screen"):
    """Rung three: ask again, naming where the first box cut the target.

    The docs' repair is "move out each edge where the Noul answer is yes,
    and ask again" -- the moved-out box is the region of the second ask,
    and the answer to believe is the second box, not the first.
    """
    cut = ", ".join(cut_edges) if cut_edges else "no"
    return transport.box(
        f"the full extent of the {target} in `{image}`. The previous box "
        f"(left {box['left']:.3f}, right {box['right']:.3f}, top "
        f"{box['top']:.3f}, bottom {box['bottom']:.3f}) cut the target at "
        f"the {cut} edge(s); return the smallest box containing the whole "
        f"{target}.")


def parse_box(answer):
    """``(edges, confidence)`` from a Box answer, or ``(None, None)``.

    The four edges must be numeric fractions of the image with left <
    right and top < bottom -- the invariant the endpoint declares -- and
    the confidence must be a probability. Anything else is not a box this
    system can stand behind, and a malformed answer is a non-answer, not
    a dissenting one.
    """
    if not isinstance(answer, dict) or answer.get("type") != "box":
        return None, None
    raw = answer.get("box")
    if not isinstance(raw, dict):
        return None, None
    try:
        edges = {name: float(raw[name]) for name in EDGES}
        confidence = float(answer.get("confidence"))
    except (KeyError, TypeError, ValueError):
        return None, None
    if not all(0.0 <= value <= 1.0 for value in edges.values()):
        return None, None
    if not 0.0 <= confidence <= 1.0:
        return None, None
    if not edges["left"] < edges["right"]:
        return None, None
    if not edges["top"] < edges["bottom"]:
        return None, None
    return edges, confidence


def _presence(answer):
    """The Noul's probability, or None when the answer is not one."""
    if not isinstance(answer, dict) or answer.get("type") != "noul":
        return None
    try:
        value = float(answer.get("noul"))
    except (TypeError, ValueError):
        return None
    return value if 0.0 <= value <= 1.0 else None


def _answered_yes(answer):
    """A Noul answered yes: the probability is at least half."""
    value = _presence(answer)
    return value is not None and value >= 0.5


def _moved_out(box, cut_edges, step):
    """The first box with every cut edge moved out one step, clamped."""
    moved = dict(box)
    if "left" in cut_edges:
        moved["left"] = max(0.0, box["left"] - step)
    if "top" in cut_edges:
        moved["top"] = max(0.0, box["top"] - step)
    if "right" in cut_edges:
        moved["right"] = min(1.0, box["right"] + step)
    if "bottom" in cut_edges:
        moved["bottom"] = min(1.0, box["bottom"] + step)
    return moved


def locate(targets, ask, *, image="screen", presence_floor=PRESENCE_FLOOR,
           confidence_floor=MIN_BOX_CONFIDENCE, repair_step=BOX_REPAIR_STEP):
    """Locate each target: presence and a box, repaired when unsure.

    ``ask(questions) -> answers`` dispatches one batched request and
    returns the answers map; the caller owns capture, billing and
    records. The rungs are deterministic and numbered by the docs:

    1. one request: a presence Noul and a Box per target;
    2. every box below ``confidence_floor`` gets the four edge questions;
    3. every edge answered yes is moved out by ``repair_step``, and the
       box is asked once more.

    A repaired box that still lands below the floor stops honestly at
    ``confidence_below_threshold``. A target whose presence question was
    not answered is not vouched for and gets no location either.
    """
    targets = list(targets)
    try:
        answers = ask(locate_questions(targets, image=image))
    except Exception as exc:
        return [LocatedTarget(target,
                              reason=f"locate dispatch failed: {exc}")
                for target in targets]
    answers = answers if isinstance(answers, dict) else {}

    results = {}
    pending = []
    for target in targets:
        presence = _presence(answers.get(_present_key(target)))
        if presence is None:
            results[target] = LocatedTarget(
                target, reason="presence not answered")
            continue
        edges, confidence = parse_box(answers.get(_box_key(target)))
        if presence < presence_floor:
            # The companion Noul's whole purpose: a box about something
            # absent is four arbitrary edges, and reporting them would be
            # a location-shaped lie.
            results[target] = LocatedTarget(target, presence=presence,
                                            reason="absent")
            continue
        if edges is None:
            results[target] = LocatedTarget(target, presence=presence,
                                            reason="no parseable box")
            continue
        if confidence >= confidence_floor:
            results[target] = LocatedTarget(target, presence=presence,
                                            box=edges, confidence=confidence)
            continue
        pending.append((target, presence, edges, confidence))

    if not pending:
        return [results[target] for target in targets]

    edge_map = {}
    for target, _presence_value, edges, _confidence in pending:
        edge_map.update(edge_questions(target, edges, image=image))
    try:
        edge_answers = ask(edge_map)
    except Exception as exc:
        for target, presence, _edges, _confidence in pending:
            results[target] = LocatedTarget(
                target, presence=presence,
                reason=f"locate dispatch failed: {exc}")
        return [results[target] for target in targets]
    edge_answers = edge_answers if isinstance(edge_answers, dict) else {}

    cuts = {}
    regions = {}
    for target, _presence_value, edges, _confidence in pending:
        cut = tuple(edge for edge in EDGES
                    if _answered_yes(edge_answers.get(_edge_key(target, edge))))
        cuts[target] = cut
        regions[target] = _moved_out(edges, cut, repair_step)

    try:
        again = ask({_rebox_key(target): rebox_question(target, regions[target],
                                            cuts[target], image=image)
                     for target in regions})
    except Exception as exc:
        for target, presence, _edges, _confidence in pending:
            results[target] = LocatedTarget(
                target, presence=presence,
                reason=f"locate dispatch failed: {exc}")
        return [results[target] for target in targets]
    again = again if isinstance(again, dict) else {}

    for target, presence, _edges, _confidence in pending:
        new_edges, new_confidence = parse_box(again.get(_rebox_key(target)))
        if new_edges is not None and new_confidence >= confidence_floor:
            results[target] = LocatedTarget(
                target, presence=presence, box=new_edges,
                confidence=new_confidence, repaired=True,
                cut_edges=cuts[target])
        else:
            results[target] = LocatedTarget(
                target, presence=presence,
                reason="confidence_below_threshold")
    return [results[target] for target in targets]
