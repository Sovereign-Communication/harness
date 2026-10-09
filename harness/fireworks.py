"""EV-7: the Fireworks lane's routing decision and cost estimate.

A model reaches Fireworks only when it is named by its confirmed serverless
path (``accounts/fireworks/models/<slug>``), that path has a routable row in
the committed pack, and the Fireworks provider is enabled. Every other model
stays on OpenRouter, so no existing pool, panel, or apply lane changes
dispatch because this module exists.

Cross-provider cheapest-whole-endpoint selection is EV-1's index, which is not
built yet. This module therefore does not compare providers on price. It routes
by explicit model path, which is the conservative choice; the canon's EV-7 row
records the price-based selection as deferred to EV-1.
"""
from dataclasses import dataclass
from typing import Optional

from .config import FIREWORKS_MODEL_PREFIX
from .endpoint_pricing import FireworksOffer, fireworks_offers
from .errors import HarnessError

PROVIDER_FIREWORKS = "fireworks"


@dataclass(frozen=True)
class Route:
    provider: str
    wire_model: str
    offer: Optional[FireworksOffer] = None


def resolve_offer(model):
    """The routable Fireworks offer for a confirmed model path, else raise."""
    for offer in fireworks_offers():
        if offer.path == model and offer.routable:
            return offer
    raise HarnessError(f"no confirmed Fireworks endpoint for '{model}'")


def resolve_route(model, *, openrouter_enabled, fireworks_enabled):
    """Pick the provider for one model. Fails closed rather than falling back.

    A Fireworks path with Fireworks disabled raises instead of quietly moving
    to OpenRouter, because the operator asked for that provider by name.
    """
    if isinstance(model, str) and model.startswith(FIREWORKS_MODEL_PREFIX):
        if not fireworks_enabled:
            raise HarnessError(
                f"model '{model}' is a Fireworks path but fireworks is disabled "
                "(HARNESS_FIREWORKS_ENABLED)")
        return Route(PROVIDER_FIREWORKS, model, resolve_offer(model))
    if not openrouter_enabled:
        raise HarnessError(
            f"model '{model}' has no enabled provider: OpenRouter is disabled "
            "(HARNESS_OPENROUTER_ENABLED)")
    return Route("openrouter", model)


def cost_estimate(offer, prompt_tokens, completion_tokens):
    """Estimated USD from the offer's Standard per-token rates.

    Cached input is billed at the full input rate here, so the estimate is
    an upper bound on the prompt side.
    """
    return (float(prompt_tokens) * offer.price.prompt
            + float(completion_tokens) * offer.price.completion)
