"""Running a pool of extractors against one capture, and building one.

The pool is the independence guarantee made structural: one capture in,
every extractor asked separately, one vote per slot out -- and the vision
pool's builder, so no caller assembles its own billed clients.
"""
from ..consensus import Vote
from ..perception import VISION_CLASS
from .vision import VisionExtractor


class ExtractorPool:
    """Runs every extractor against one capture and collects votes.

    Sequential rather than parallel on purpose for the first version: the
    number of extractors is small, the cost is already the dominant cost, and
    a serial pool produces a deterministic ordering of records for the audit
    log, which is worth more here than the wall-clock saving.

    A pool declares the target classes it serves, and
    :meth:`refuses_class` is the structural half of "three of the four target
    classes never reach the vision tier": a vision pool handed a ``dom``
    capture returns no votes rather than billing for an opinion nobody asked
    for.
    """

    def __init__(self, extractors, *, serves=()):
        self.extractors = list(extractors)
        self.serves = tuple(serves)

    @property
    def size(self):
        return len(self.extractors)

    def refuses_class(self, target_class):
        """Whether this pool declines to answer ``target_class``.

        ``None`` -- an undeclared target -- is always answered. A pool that
        declares nothing is a pool that serves everything, which is the
        historical behaviour and the reason the declaration is optional.
        """
        if not self.serves:
            return False
        return target_class is not None and target_class not in self.serves

    def for_target(self, target):
        """This pool, or ``None`` if it does not serve the target's class."""
        target_class = getattr(target, "target_class", None)
        return None if self.refuses_class(target_class) else self

    @property
    def reads_captures(self):
        """Whether anything in this pool will read a capture payload.

        A vision pool answers no: its extractors read the window through
        the operating system and declare ``uses_capture = False``. That
        declaration is what lets a screen capture be skipped instead of
        performed, written to disk, base64-encoded into memory and
        fingerprinted for a payload nothing looks at -- which also removes
        the unrelated environment variable that used to be a precondition
        of the vision tier working at all.

        An empty pool answers yes. It has no extractors to read anything,
        but it is the fallback for an undeclared target class, and reading
        its silence as "pixels are not needed" would quietly change what
        an existing caller observes.
        """
        if not self.extractors:
            return True
        return any(getattr(e, "uses_capture", True)
                   for e in self.extractors)

    def stability(self):
        """The pool's stability judgment: the least settled of its slots.

        A guard is a property of the situation rather than a vote, so slots
        that see different movement are resolved toward blocking. A slot
        with no judgment yet means the pool has none -- the decision tier
        keeps asking its own question until every history is warm -- and a
        pool with no frame-aware extractor at all answers ``None`` always,
        which is the words-mode and structured-tier behaviour, untouched.
        """
        judgments = []
        for extractor in self.extractors:
            get = getattr(extractor, "stability", None)
            if get is None:
                continue
            judgment = get()
            if judgment is None:
                return None
            judgments.append(judgment)
        if not judgments:
            return None
        return min(judgments, key=lambda judgment: judgment.p)

    def run(self, capture, schema):
        """Return one Vote per extractor. Never raises for a slot failure."""
        votes = []
        for extractor in self.extractors:
            slot = getattr(extractor, "slot", f"slot-{len(votes)}")
            try:
                result = (extractor.extract(capture, schema)
                          if extractor.uses_capture
                          else extractor.extract(schema))
            except Exception as exc:  # isolation is the point
                votes.append(Vote.error(slot, f"extractor raised: {exc}"))
                continue
            if result.ok:
                votes.append(Vote.ok(slot, result.state, cost=result.cost,
                                     usage_source=result.usage_source))
            else:
                # A model that answered but produced nothing usable is
                # malformed, not merely unavailable: it occupied a slot and
                # spent money, and the tally must reflect that.
                votes.append(Vote.malformed(slot, result.reason or "no state",
                                            cost=result.cost,
                                            usage_source=result.usage_source))
        return votes


def build_vision_pool(settings, *, budget, audit, slots=1,
                      transport_module=None, timeout=90, describer=None,
                      image_preparer=None):
    """A pool of independent vision slots, for the ``gui`` target class.

    Built here rather than by the caller so that the vision pool is
    constructed the same way every time: same endpoint, same budget, same
    audit log as the decision tier. A caller who wants two independent
    opinions asks for two slots; they do not assemble their own clients.
    """
    names = (f"vision-{i}" for i in range(max(1, int(slots))))
    return ExtractorPool(
        [VisionExtractor(name, settings, budget=budget, audit=audit,
                         transport_module=transport_module, timeout=timeout,
                         describer=describer, image_preparer=image_preparer)
         for name in names],
        serves=(VISION_CLASS,))
