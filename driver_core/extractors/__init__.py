"""The extraction tier: N independent extractors over one capture.

Independence is the only thing this tier is for. The consensus tally can
only say "two or more independent observers saw the same thing" if the
observers really are independent, so the rules below are structural rather
than advisory:

* **An extractor never sees another's answer.** Each is handed the same
  capture and the same schema and nothing else. There is no shared
  conversation, no seed answer, no "the previous extractor said" hint. A
  second model told what the first one said is a rubber stamp, not a second
  observation.
* **Failures are isolated.** One extractor dying must not affect the others;
  a pool that stops at the first error is not a pool.
* **Cost is recorded per slot**, including for slots that failed after the
  provider billed them.

Two kinds of extractor exist, and the distinction is the single biggest
lever on cost and reliability:

:class:`StructuredExtractor` is deterministic and free. It handles CLI
output, DOM/accessibility trees and MCP results, where the "state" is
already structured. Three of the four target classes need no vision at all,
and this is the class that makes them work.

:class:`VisionExtractor` spends money and can be wrong. It exists for native
GUI pixels, where there is no structured source to read. It reaches the
model through the same client primitives the decision tier uses -- one
endpoint, one key, one budget, one price list -- and it is the only extractor
whose spend reaches the run ceiling.

The concerns land in their own modules, one owner each, and this package is
the surface they are reached through:

* :mod:`driver_core.extractors.structured` -- the free, deterministic tier.
* :mod:`driver_core.extractors.frames` -- the cross-step frame history.
* :mod:`driver_core.extractors.questions` -- the typed questions and the parse.
* :mod:`driver_core.extractors.sizing` -- what a dispatch costs to reserve.
* :mod:`driver_core.extractors.locate_rungs` -- the locate ladder's rungs.
* :mod:`driver_core.extractors.vision` -- the billed words-and-pixels slot.
* :mod:`driver_core.extractors.pool` -- running and building pools.
"""
from .frames import STABILITY_WINDOW, FrameHistory, Stability
from .pool import ExtractorPool, build_vision_pool
from .questions import JUDGMENT_TYPES, _extraction_questions, _observed_state
from .structured import Extraction, StructuredExtractor
from .vision import VisionExtractor

__all__ = [
    "Extraction",
    "ExtractorPool",
    "FrameHistory",
    "JUDGMENT_TYPES",
    "STABILITY_WINDOW",
    "Stability",
    "StructuredExtractor",
    "VisionExtractor",
    "_extraction_questions",
    "_observed_state",
    "build_vision_pool",
]


#: Harness-local export: the per-image request token bound used by the
#: capture adapter; kept as a named constant so the adapter and tests agree.
IMAGE_REQUEST_TOKEN_BOUND = 65_536

#: Re-exported so a host importing the extraction tier can name the one
#: endpoint without reaching into the decision tier.
from ..jev_client import SYSTEM_ONE_URL  # noqa: E402,F401
