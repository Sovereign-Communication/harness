"""Meta direct lane: the Muse models' committed offers and cost estimate.

A model reaches Meta only when it is named by its bare Meta API id
(``muse-<...>``) and that id has a committed offer below. Every other model
stays on its existing provider, so no pool, panel, or apply lane changes
dispatch because this module exists. An id carrying the ``muse-`` prefix
but no committed offer fails closed: the prefix claims the Meta lane, and
an unpriced model must never be routed on a guess or booked as free.

Prices are Meta's published per-token list rates (Oct 2026: Spark
$1.25/$4.25 per 1M input/output; Wonder $4/$20 per 1M), stored per token.
The Meta API's usage block carries token counts but no cost, so accounting
estimates from these rates exactly as the Fireworks lane estimates from
its pack -- an upper bound whenever cached input would bill lower.
"""
from dataclasses import dataclass

from .errors import HarnessError


@dataclass(frozen=True)
class MetaOffer:
    model: str
    prompt: float      # USD per input token
    completion: float  # USD per output token


META_OFFERS = {
    "muse-spark-1.3": MetaOffer("muse-spark-1.3", 1.25e-6, 4.25e-6),
    "muse-wonder-1.0": MetaOffer("muse-wonder-1.0", 4.0e-6, 20.0e-6),
}


def resolve_offer(model):
    """The committed Meta offer for a Muse model id, else raise."""
    offer = META_OFFERS.get(model)
    if offer is None:
        raise HarnessError(f"no committed Meta endpoint for '{model}'")
    return offer


def cost_estimate(offer, prompt_tokens, completion_tokens):
    """Estimated USD from the offer's per-token rates.

    Cached input is billed at the full input rate here, so the estimate is
    an upper bound on the prompt side.
    """
    return (float(prompt_tokens) * offer.prompt
            + float(completion_tokens) * offer.completion)
