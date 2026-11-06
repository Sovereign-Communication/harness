"""One bounded source-resolution tile search after exhausted Box repair."""
import math

from .. import transport
from ..consensus import BOX_IOU_FLOOR, box_iou
from ..locate import BOX_REPAIR_STEP, MIN_BOX_CONFIDENCE, LocatedTarget, locate


def regions(width, height, pad=BOX_REPAIR_STEP):
    """Four overlapping quadrants, using the existing repair padding."""
    x0, y0 = int(width * (.5 - pad)), int(height * (.5 - pad))
    x1, y1 = math.ceil(width * (.5 + pad)), math.ceil(height * (.5 + pad))
    return tuple(dict.fromkeys(
        (x, y, right - x, bottom - y)
        for y, bottom in ((0, y1), (y0, height))
        for x, right in ((0, x1), (x0, width))))


def search_tiles(results, size, prepare, dispatch, thresholds):
    """At most four crops / twelve calls per opinion, with no recursion.

    Search every tile before accepting a unique location. Unknown tiles,
    partial targets or failed dispatches cannot be silently treated as absent.
    All candidates return to original-source fractions before comparison.
    """
    pending = [r for r in results if r.reason == "confidence_below_threshold"]
    if not pending:
        return
    candidates = {r.target: [] for r in pending}
    unresolved = set()
    ambiguous = set()
    width, height = size
    confidence_floor = thresholds.get("confidence_floor", MIN_BOX_CONFIDENCE)
    for region in regions(width, height):
        prepared, _why = prepare(region)
        if prepared is None:
            unresolved.update(candidates)
            continue
        counts = {}

        def ask(questions, prepared=prepared, counts=counts):
            questions = dict(questions)
            for target in candidates:
                if f"present::{target}" in questions:
                    questions[f"count::{target}"] = transport.choice(
                        f"How many distinct instances of the {target} are visible "
                        "in `screen`? Count objects, not fragments of one object.",
                        {"none": "No instance", "one": "Exactly one instance",
                         "several": "More than one distinct instance"})
            answers = dispatch(questions, prepared)
            if isinstance(answers, dict):
                for target in candidates:
                    answer = answers.get(f"count::{target}")
                    if isinstance(answer, dict) and answer.get("type") == "choice":
                        confidence = answer.get("confidence")
                        if (type(confidence) in (int, float)
                                and confidence_floor <= confidence <= 1):
                            counts[target] = answer.get("choice")
            return answers

        located = locate(candidates, ask, **thresholds)
        x, y, crop_width, crop_height = region
        for result in located:
            target = result.target
            if counts.get(target) == "several":
                ambiguous.add(target)
                continue
            if result.reason == "absent" and counts.get(target) == "none":
                continue
            if not result.located or counts.get(target) != "one":
                unresolved.add(target)
                continue
            box = prepared.geometry.source_box(result.box)
            if box is None or (
                (x > 0 and box["left"] <= 0)
                or (y > 0 and box["top"] <= 0)
                or (x + crop_width < width and box["right"] >= 1)
                or (y + crop_height < height and box["bottom"] >= 1)
            ):
                unresolved.add(target)
                continue
            result.box = {
                "left": (x + box["left"] * crop_width) / width,
                "right": (x + box["right"] * crop_width) / width,
                "top": (y + box["top"] * crop_height) / height,
                "bottom": (y + box["bottom"] * crop_height) / height}
            candidates[target].append(result)
    for original in pending:
        matches = candidates[original.target]
        if original.target in ambiguous or any(
            box_iou(a.box, b.box) < BOX_IOU_FLOOR
            for i, a in enumerate(matches) for b in matches[i+1:]
        ):
            original.reason = "extraction_disagreement"
        elif matches and original.target not in unresolved:
            chosen = matches[0]
            replacement = LocatedTarget(original.target, presence=chosen.presence,
                                        box=chosen.box, confidence=chosen.confidence,
                                        repaired=True, cut_edges=chosen.cut_edges)
            results[results.index(original)] = replacement
