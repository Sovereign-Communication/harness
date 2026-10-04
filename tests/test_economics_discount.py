"""EV-0 discount semantics: the probe that settles what `discount` means,
and the gate that refuses to produce a price until it has an answer.

OpenRouter publishes a per-endpoint `discount` beside the listed rates
without documenting whether the promotion is already baked in. At
discount=0.5 the two readings differ by exactly 2x, and every ranking,
reserve, and future auto-rotation decision inverts on the answer -- so it is
measured, recorded, and enforced rather than guessed.

All hermetic: a fake transport supplies the published offers and the
provider-reported charge.
"""
import json
import os
import tempfile
import unittest

from harness.config import (DISCOUNT_AMBIGUOUS, DISCOUNT_IS_MULTIPLIER,
                            DISCOUNT_LISTED_IS_EFFECTIVE,
                            DISCOUNT_NOT_APPLICABLE, DISCOUNT_UNRESOLVED)
from harness.economics import (effective_price, load_discount_semantics,
                               record_discount_semantics,
                               resolve_discount_semantics, run_discount_probe)
from harness.errors import HarnessError


def _offer(name, prompt, completion, discount=0.0):
    return {"name": name, "provider_name": name,
            "pricing": {"prompt": str(prompt), "completion": str(completion),
                        "discount": discount},
            "context_length": 128000, "max_completion_tokens": 32000}


class ProbeTransport:
    """Publishes endpoint offers and returns a caller-chosen charge."""

    def __init__(self, offers, *, usage, status=200):
        self.offers = offers
        self.usage = usage
        self.status = status
        self.posts = []

    def get(self, url, api_key, timeout=15):
        model = url.split("/models/", 1)[1].rsplit("/endpoints", 1)[0]
        return {"data": {"id": model, "endpoints": self.offers}}

    def post(self, url, api_key, payload, timeout=60):
        self.posts.append((url, payload))
        body = {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        if self.usage is not None:
            body["usage"] = self.usage
        return self.status, body


def _charge_for(offers, tokens, apply_discount):
    """The cost a given endpoint would charge for `tokens`."""
    prompt_tokens, completion_tokens = tokens
    for offer in offers:
        pricing = offer["pricing"]
        rate_in = float(pricing["prompt"])
        rate_out = float(pricing["completion"])
        if apply_discount and pricing.get("discount"):
            keep = 1.0 - float(pricing["discount"])
            rate_in *= keep
            rate_out *= keep
        return prompt_tokens * rate_in + completion_tokens * rate_out
    return None


class ProbeDecisionTests(unittest.TestCase):
    """Each branch of the binary question, from a measured charge."""

    TOKENS = (100, 40)

    def _usage(self, offers, apply_discount):
        cost = _charge_for(offers, self.TOKENS, apply_discount)
        return {"cost": cost, "prompt_tokens": self.TOKENS[0],
                "completion_tokens": self.TOKENS[1]}

    def test_listed_price_already_includes_the_discount(self):
        offers = [_offer("Only", 2e-6, 10e-6, discount=0.5)]
        transport = ProbeTransport(offers, usage=self._usage(offers, apply_discount=False))
        record = run_discount_probe(transport, "k", None, "acme/sol")

        self.assertEqual(record["semantics"], DISCOUNT_LISTED_IS_EFFECTIVE)
        self.assertEqual(record["measured"]["cost_basis"],
                         "provider_reported_usage.cost")
        self.assertEqual(record["measured"]["prompt_tokens"], 100)
        self.assertEqual(record["fingerprint"]["max_discount"], 0.5)

    def test_discount_must_still_be_applied(self):
        offers = [_offer("Only", 2e-6, 10e-6, discount=0.5)]
        transport = ProbeTransport(offers, usage=self._usage(offers, apply_discount=True))
        record = run_discount_probe(transport, "k", None, "acme/sol")

        self.assertEqual(record["semantics"], DISCOUNT_IS_MULTIPLIER)
        self.assertEqual(record["fingerprint"]["max_discount"], 0.5)

    def test_aliased_prices_are_ambiguous_not_guessed(self):
        """The real GPT-5.6 Sol shape: one offer's listed rate equals
        another's post-discount rate, so BOTH hypotheses fit a charge."""
        offers = [_offer("OpenAI", 2e-6, 10e-6, discount=0.5),
                  _offer("OpenAI/flex", 1e-6, 5e-6, discount=0.5)]
        # Charged at the `openai` discounted rate -- which is exactly the
        # `flex` listed rate, so the reading cannot be recovered here.
        transport = ProbeTransport(offers, usage=self._usage(offers, apply_discount=True))
        record = run_discount_probe(transport, "k", None, "acme/sol")

        self.assertEqual(record["semantics"], DISCOUNT_AMBIGUOUS)
        self.assertNotIn("fingerprint", record)
        self.assertTrue(record["measured"]["predictions"]
                        [DISCOUNT_LISTED_IS_EFFECTIVE]["matched"])
        self.assertTrue(record["measured"]["predictions"]
                        [DISCOUNT_IS_MULTIPLIER]["matched"])

    def test_charge_matching_no_offer_is_unresolved(self):
        offers = [_offer("Only", 2e-6, 10e-6, discount=0.5)]
        transport = ProbeTransport(
            offers, usage={"cost": 0.5, "prompt_tokens": 100,
                           "completion_tokens": 40})
        record = run_discount_probe(transport, "k", None, "acme/sol")
        self.assertEqual(record["semantics"], DISCOUNT_UNRESOLVED)
        self.assertNotIn("fingerprint", record)

    def test_missing_reported_cost_is_unresolved_not_manufactured(self):
        """Synthesizing a charge from the /models aggregate would be
        circular -- the probe tests exactly that number."""
        offers = [_offer("Only", 2e-6, 10e-6, discount=0.5)]
        transport = ProbeTransport(offers, usage={"prompt_tokens": 100,
                                                  "completion_tokens": 40})
        record = run_discount_probe(transport, "k", None, "acme/sol")
        self.assertEqual(record["semantics"], DISCOUNT_UNRESOLVED)
        self.assertIn("circular", record["reason"])

    def test_missing_token_usage_is_unresolved(self):
        offers = [_offer("Only", 2e-6, 10e-6, discount=0.5)]
        transport = ProbeTransport(offers, usage={"cost": 1e-5})
        record = run_discount_probe(transport, "k", None, "acme/sol")
        self.assertEqual(record["semantics"], DISCOUNT_UNRESOLVED)
        self.assertIn("itemized", record["reason"])

    def test_byok_response_is_unresolved(self):
        offers = [_offer("Only", 2e-6, 10e-6, discount=0.5)]
        transport = ProbeTransport(
            offers,
            usage={"cost": 1e-5, "prompt_tokens": 100, "completion_tokens": 40,
                   "is_byok": True})
        record = run_discount_probe(transport, "k", None, "acme/sol")
        self.assertEqual(record["semantics"], DISCOUNT_UNRESOLVED)
        self.assertIn("BYOK", record["reason"])

    def test_http_failure_is_unresolved_not_raised(self):
        offers = [_offer("Only", 2e-6, 10e-6, discount=0.5)]
        transport = ProbeTransport(offers, usage=None, status=400)
        record = run_discount_probe(transport, "k", None, "acme/sol")
        self.assertEqual(record["semantics"], DISCOUNT_UNRESOLVED)
        self.assertIn("HTTP 400", record["reason"])

    def test_model_without_a_promotion_cannot_settle_the_question(self):
        transport = ProbeTransport([_offer("Only", 2e-6, 10e-6)],
                                   usage={"cost": 1e-5, "prompt_tokens": 1,
                                          "completion_tokens": 1})
        with self.assertRaises(HarnessError) as ctx:
            run_discount_probe(transport, "k", None, "acme/plain")
        self.assertIn("no discount", str(ctx.exception))

    def test_probe_posts_once_with_a_bounded_budget(self):
        offers = [_offer("Only", 2e-6, 10e-6, discount=0.5)]
        transport = ProbeTransport(offers, usage=self._usage(offers, False))
        run_discount_probe(transport, "k", None, "acme/sol", max_tokens=64)
        self.assertEqual(len(transport.posts), 1)
        _url, payload = transport.posts[0]
        self.assertEqual(payload["max_tokens"], 64)
        # :floor routing is deliberate -- it routes to whichever provider is
        # actually cheapest, which is the charge being measured.
        self.assertTrue(payload["model"].endswith(":floor"))


class RecordingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "economics.json")
        self.addCleanup(self._tmp.cleanup)

    def _endpoints(self, discount=0.5, prompt=2e-6, completion=10e-6):
        from harness.economics import fetch_endpoints
        transport = ProbeTransport([_offer("Only", prompt, completion, discount)],
                                   usage=None)
        return fetch_endpoints(transport, "k", "acme/sol")

    def test_absent_verdict_loads_as_none(self):
        self.assertIsNone(load_discount_semantics(self.path))

    def test_only_conclusive_verdicts_are_recorded(self):
        for semantics in (DISCOUNT_UNRESOLVED, DISCOUNT_AMBIGUOUS):
            with self.assertRaises(HarnessError) as ctx:
                record_discount_semantics({"semantics": semantics}, self.path)
            self.assertIn("conclusive verdict", str(ctx.exception))
        self.assertIsNone(load_discount_semantics(self.path))

    def test_conclusive_verdict_round_trips(self):
        record = {"semantics": DISCOUNT_LISTED_IS_EFFECTIVE, "model": "acme/sol",
                  "fingerprint": {"model": "acme/sol", "max_discount": 0.5}}
        target = record_discount_semantics(record, self.path)
        loaded = load_discount_semantics(self.path)
        self.assertEqual(loaded["semantics"], DISCOUNT_LISTED_IS_EFFECTIVE)
        self.assertEqual(loaded["fingerprint"]["max_discount"], 0.5)
        self.assertEqual(target, self.path)

    def test_recorded_file_is_lf_even_on_windows(self):
        record = {"semantics": DISCOUNT_IS_MULTIPLIER, "model": "acme/sol",
                  "fingerprint": {"model": "acme/sol", "max_discount": 0.5}}
        record_discount_semantics(record, self.path)
        with open(self.path, "rb") as stream:
            raw = stream.read()
        self.assertNotIn(b"\r\n", raw)
        json.loads(raw.decode("utf-8"))

    def test_verdict_without_a_fingerprint_is_not_trustworthy(self):
        with open(self.path, "w", encoding="utf-8") as stream:
            json.dump({"semantics": DISCOUNT_LISTED_IS_EFFECTIVE}, stream)
        self.assertIsNone(load_discount_semantics(self.path))

    def test_unreadable_or_malformed_state_is_treated_as_absent(self):
        for content in ("{ not json", "[]", '"a string"'):
            with open(self.path, "w", encoding="utf-8") as stream:
                stream.write(content)
            self.assertIsNone(load_discount_semantics(self.path))


class GateTests(unittest.TestCase):
    """The gate is the point of EV-0: no recorded, still-applicable verdict
    means no number at all."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "economics.json")
        self.addCleanup(self._tmp.cleanup)

    def _endpoints(self, discount=0.5, prompt=2e-6, completion=10e-6):
        from harness.economics import fetch_endpoints
        transport = ProbeTransport([_offer("Only", prompt, completion, discount)],
                                   usage=None)
        return fetch_endpoints(transport, "k", "acme/sol")

    def _record(self, semantics, discount=0.5):
        record_discount_semantics(
            {"semantics": semantics, "model": "acme/sol",
             "fingerprint": {"model": "acme/sol", "max_discount": discount}},
            self.path)

    def test_gate_refuses_with_no_verdict(self):
        endpoints = self._endpoints()
        with self.assertRaises(HarnessError) as ctx:
            effective_price(endpoints, 8000, 1000, path=self.path)
        message = str(ctx.exception)
        self.assertIn("no recorded semantics verdict", message)
        self.assertIn("2.00x", message)

    def test_resolve_refuses_when_the_promotion_changed(self):
        self._record(DISCOUNT_LISTED_IS_EFFECTIVE, discount=0.5)
        endpoints = self._endpoints(discount=0.75)
        with self.assertRaises(HarnessError) as ctx:
            resolve_discount_semantics(endpoints, path=self.path)
        self.assertIn("may no longer hold", str(ctx.exception))

    def test_no_promotion_means_the_question_does_not_arise(self):
        endpoints = self._endpoints(discount=0.0)
        self.assertEqual(resolve_discount_semantics(endpoints, path=self.path),
                         DISCOUNT_NOT_APPLICABLE)
        endpoint, cost = effective_price(endpoints, 8000, 1000, path=self.path)
        self.assertAlmostEqual(cost, 8000 * 2e-6 + 1000 * 10e-6)
        self.assertEqual(endpoint.provider_name, "Only")

    def test_verdict_decides_whether_the_discount_is_applied(self):
        self._record(DISCOUNT_LISTED_IS_EFFECTIVE)
        _endpoint, listed = effective_price(self._endpoints(), 8000, 1000,
                                            path=self.path)
        self.assertAlmostEqual(listed, 8000 * 2e-6 + 1000 * 10e-6)

        self._record(DISCOUNT_IS_MULTIPLIER)
        _endpoint, discounted = effective_price(self._endpoints(), 8000, 1000,
                                                path=self.path)
        self.assertAlmostEqual(discounted, (8000 * 1e-6) + (1000 * 5e-6))
        # Exactly the 2x the ambiguity was worth measuring for.
        self.assertAlmostEqual(listed / discounted, 2.0, places=6)

    def test_gate_picks_the_cheapest_offer_without_mixing_rates(self):
        """The inverted in/out pair from DF-EV-2: no blending of the cheapest
        input from one provider with the cheapest output from another.

        At 8000in/1000out the CheapIn offer really is cheaper overall
        ($0.00028 vs $0.00405), yet ExpensiveIn has the cheaper *output*
        rate. Mixing the two per-field would report $0.00013 -- a phantom
        price no provider would charge, and the number a cost index built
        per-field would publish.
        """
        from harness.economics import fetch_endpoints
        transport = ProbeTransport(
            [_offer("CheapInExpensiveOut", 1e-8, 2e-7, discount=0.0),
             _offer("ExpensiveInCheapOut", 5e-7, 5e-8, discount=0.0)],
            usage=None)
        endpoints = fetch_endpoints(transport, "k", "acme/mix")
        endpoint, cost = effective_price(endpoints, 8000, 1000, path=self.path)

        self.assertEqual(endpoint.provider_name, "CheapInExpensiveOut")
        # The winner is whole: its own output rate, not the cheaper one.
        self.assertAlmostEqual(cost, 8000 * 1e-8 + 1000 * 2e-7)
        cheapest_out = endpoints.endpoints[1]
        self.assertLess(cheapest_out.completion, endpoint.completion)
        phantom = 8000 * endpoint.prompt + 1000 * cheapest_out.completion
        self.assertLess(phantom, cost)

    def test_ineligible_offer_is_not_chosen_as_the_cheap_one(self):
        from harness.economics import fetch_endpoints
        transport = ProbeTransport([
            dict(_offer("Degraded", 1e-9, 1e-9, discount=0.0),
                 uptime_last_1d=40.0),
            dict(_offer("Healthy", 2e-6, 4e-6, discount=0.0),
                 uptime_last_1d=99.9),
        ], usage=None)
        endpoints = fetch_endpoints(transport, "k", "acme/health")
        endpoint, _cost = effective_price(endpoints, 8000, 1000, path=self.path,
                                          min_uptime=90.0)
        self.assertEqual(endpoint.provider_name, "Healthy")

    def test_cached_input_is_the_cheaper_rate_when_eligible(self):
        from harness.economics import fetch_endpoints
        offer = _offer("Cached", 2e-6, 10e-6, discount=0.0)
        offer["pricing"]["input_cache_read"] = 2e-7
        transport = ProbeTransport([offer], usage=None)
        endpoints = fetch_endpoints(transport, "k", "acme/cache")

        _e, uncached = effective_price(endpoints, 8000, 1000, path=self.path)
        _e, cached = effective_price(endpoints, 8000, 1000, path=self.path,
                                     cache_eligible=True)
        self.assertAlmostEqual(cached, 8000 * 2e-7 + 1000 * 10e-6)
        self.assertLess(cached, uncached)
