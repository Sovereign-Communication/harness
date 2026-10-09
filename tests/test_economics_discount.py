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
import io
import json
import inspect
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

import harness.discount_gate
import harness.discount_probe
from harness.config import (CONFIG_DIR, DISCOUNT_AMBIGUOUS,
                            DISCOUNT_IS_MULTIPLIER, DISCOUNT_LISTED_IS_EFFECTIVE,
                            DISCOUNT_NOT_APPLICABLE, DISCOUNT_UNRESOLVED,
                            ECONOMICS_SCHEMA_VERSION, ECONOMICS_VERDICT_PATH)
from harness.discount_gate import (REPO_ROOT, discount_verdict_path,
                                   load_discount_semantics,
                                   record_discount_semantics,
                                   resolve_discount_semantics)
from harness.discount_probe import run_discount_probe
from harness.errors import HarnessError
from harness.provider_errors import ProviderSpendLimitError


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
        self.gets = []

    def get(self, url, api_key, timeout=15):
        model = url.split("/models/", 1)[1].rsplit("/endpoints", 1)[0]
        self.gets.append(model)
        if self.offers is None:
            return {"error": {"message": f"no such model {model}"}}
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

    def test_direct_spend_limit_response_is_classified_and_accounted(self):
        offers = [_offer("Only", 2e-6, 10e-6, discount=0.5)]

        class CappedTransport(ProbeTransport):
            def post(self, url, api_key, payload, timeout=60):
                return 402, {
                    "error": {"code": "payment_required",
                              "message": "Payment required"},
                    "usage": {"cost": 0.002},
                }

        class Governor:
            def __init__(self):
                self.actual = []

            def check_byok(self, _model):
                pass

            def preflight(self, *_args):
                pass

            def record_actual(self, cost, model):
                self.actual.append((cost, model))

        governor = Governor()
        with self.assertRaises(ProviderSpendLimitError) as ctx:
            run_discount_probe(CappedTransport(offers, usage=None), "k",
                               governor, "acme/sol")
        self.assertEqual(ctx.exception.kind, "provider_spend_limit")
        self.assertAlmostEqual(ctx.exception.known_cost, 0.002)
        self.assertTrue(ctx.exception.cost_accounted)
        self.assertEqual(governor.actual, [(0.002, "acme/sol")])

    def test_successful_probe_accounts_provider_reported_cost(self):
        offers = [_offer("Only", 2e-6, 10e-6, discount=0.5)]
        cost = self._usage(offers, apply_discount=False)

        class Governor:
            def __init__(self):
                self.actual = []

            def check_byok(self, _model):
                pass

            def preflight(self, *_args):
                pass

            def record_actual(self, amount, model):
                self.actual.append((amount, model))

        governor = Governor()
        transport = ProbeTransport(offers, usage=cost)
        run_discount_probe(transport, "k", governor, "acme/sol")
        self.assertEqual(governor.actual, [(cost["cost"], "acme/sol")])


class ProbeFetchBoundTests(unittest.TestCase):
    """The probe is the only shipped caller of endpoint fetch, so the bound
    has to hold ON THE PROBE -- not merely in a helper the tests call.

    That is the exact shape of DF-EV-9: the guard was real and reachable only
    from tests while the shipped surface looped `fetch_endpoints` around it.
    `run_discount_probe` now goes through `fetch_endpoints_for`, and these
    tests drive the probe itself.
    """

    def _transport(self, *, missing=False):
        offers = None if missing else [_offer("Only", 2e-6, 10e-6,
                                               discount=0.5)]
        return ProbeTransport(offers, usage={"cost": 1e-5, "prompt_tokens": 100,
                                             "completion_tokens": 40})

    def test_one_probe_is_exactly_one_endpoint_get(self):
        transport = self._transport()
        run_discount_probe(transport, "k", None, "acme/sol:free")
        # Stripped to the canonical id, fetched once, for the model asked for.
        self.assertEqual(transport.gets, ["acme/sol"])

    def test_a_failing_feed_raises_and_is_announced(self):
        """A probe has no other model to fall back on, so a dead feed is a
        raised error -- never an `unresolved` verdict for a measurement that
        never happened -- and it is announced, not swallowed."""
        transport = self._transport(missing=True)
        buffer = io.StringIO()
        with redirect_stderr(buffer):
            with self.assertRaises(HarnessError) as ctx:
                run_discount_probe(transport, "k", None, "acme/sol")
        self.assertIn("no such model acme/sol", str(ctx.exception))
        self.assertIn("[warn] economics: endpoint feed failed for acme/sol",
                      buffer.getvalue())
        self.assertEqual(transport.posts, [])

    def test_the_owner_would_refuse_a_fan_out_before_spending_anything(self):
        """The bound is one GET per model inside a fixed run budget, and an
        over-budget list is refused rather than truncated."""
        from harness.endpoint_pricing import fetch_endpoints_for
        transport = self._transport()
        with self.assertRaises(HarnessError) as ctx:
            fetch_endpoints_for(transport, "k",
                                [f"acme/m{i}" for i in range(3)],
                                max_fetches=2)
        self.assertIn("No requests were issued", str(ctx.exception))
        self.assertEqual(transport.gets, [])

    def test_the_probe_cannot_loop_the_single_model_fetch(self):
        """Source-level, because with one model the bypass is otherwise
        invisible: DF-EV-9 was exactly a call site looping `fetch_endpoints`
        while the bound sat in a helper only the tests called."""
        source = inspect.getsource(harness.discount_probe.run_discount_probe)
        self.assertIn("fetch_endpoints_for(", source)
        self.assertNotIn("fetch_endpoints(",
                         source.replace("fetch_endpoints_for(", "OWNER("))


class RecordingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "economics.json")
        self.addCleanup(self._tmp.cleanup)

    def _endpoints(self, discount=0.5, prompt=2e-6, completion=10e-6):
        from harness.endpoint_pricing import fetch_endpoints
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
        from harness.endpoint_pricing import fetch_endpoints
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
            resolve_discount_semantics(endpoints, path=self.path)
        message = str(ctx.exception)
        self.assertIn("committed semantics verdict", message)
        self.assertIn("2.00x", message)

    def test_resolve_refuses_when_the_promotion_changed(self):
        self._record(DISCOUNT_LISTED_IS_EFFECTIVE, discount=0.5)
        endpoints = self._endpoints(discount=0.75)
        with self.assertRaises(HarnessError) as ctx:
            resolve_discount_semantics(endpoints, path=self.path)
        self.assertIn("may no longer hold", str(ctx.exception))

    def test_no_promotion_means_the_question_does_not_arise(self):
        endpoints = self._endpoints(discount=0.0)
        semantics = resolve_discount_semantics(endpoints, path=self.path)
        self.assertEqual(semantics, DISCOUNT_NOT_APPLICABLE)
        # Listed rates, used as-is: no verdict is needed to price an
        # unpromoted endpoint.
        self.assertAlmostEqual(
            endpoints.endpoints[0].blended_usd(8000, 1000, semantics=semantics),
            8000 * 2e-6 + 1000 * 10e-6)

    def test_the_verdict_is_what_the_two_readings_worth_2x(self):
        """EV-0's whole reason for existing, stated on the surviving
        arithmetic: the same offer priced under the two committed verdicts.

        Which endpoint wins, and whether an offer may be routed to at all, is
        EV-1's selection policy (DF-EV-12) -- but the 2x it inverts on is
        measured here and nowhere else.
        """
        self._record(DISCOUNT_LISTED_IS_EFFECTIVE)
        listed = resolve_discount_semantics(self._endpoints(), path=self.path)
        endpoints = self._endpoints()
        as_listed = endpoints.endpoints[0].blended_usd(
            8000, 1000, semantics=listed)
        self.assertAlmostEqual(as_listed, 8000 * 2e-6 + 1000 * 10e-6)

        self._record(DISCOUNT_IS_MULTIPLIER)
        multiplier = resolve_discount_semantics(self._endpoints(), path=self.path)
        as_multiplier = endpoints.endpoints[0].blended_usd(
            8000, 1000, semantics=multiplier)
        self.assertAlmostEqual(as_multiplier, (8000 * 1e-6) + (1000 * 5e-6))
        # Exactly the 2x the ambiguity was worth measuring for.
        self.assertAlmostEqual(as_listed / as_multiplier, 2.0, places=6)

    def test_every_offer_is_priced_from_its_own_rates(self):
        """Canon invariant 3 (DF-EV-2): never mix per-endpoint prices. The
        arithmetic is per endpoint by construction, which is what lets EV-1
        pick a whole offer rather than a phantom blended one."""
        from harness.endpoint_pricing import fetch_endpoints
        transport = ProbeTransport(
            [_offer("CheapInExpensiveOut", 1e-8, 2e-7, discount=0.0),
             _offer("ExpensiveInCheapOut", 5e-7, 5e-8, discount=0.0)],
            usage=None)
        endpoints = fetch_endpoints(transport, "k", "acme/mix")
        cheap_in, cheap_out = endpoints.endpoints
        self.assertAlmostEqual(
            cheap_in.blended_usd(8000, 1000, semantics=DISCOUNT_NOT_APPLICABLE),
            8000 * 1e-8 + 1000 * 2e-7)
        self.assertAlmostEqual(
            cheap_out.blended_usd(8000, 1000, semantics=DISCOUNT_NOT_APPLICABLE),
            8000 * 5e-7 + 1000 * 5e-8)
        # The phantom a per-field blend would report is cheaper than any
        # real offer, which is exactly why nobody is allowed to build one.
        phantom = 8000 * cheap_in.prompt + 1000 * cheap_out.completion
        self.assertLess(phantom, cheap_in.blended_usd(
            8000, 1000, semantics=DISCOUNT_NOT_APPLICABLE))


class CommittedVerdictTests(unittest.TestCase):
    """The verdict is REPO EVIDENCE, not machine state.

    The gate used to read ``~/.config/harness/economics.json``, so the answer
    to "is the price gate satisfied?" lived on one operator's disk: two
    checkouts could disagree, and a verdict nobody could review in a PR
    governed the cost model of the whole system. The receipt is committed
    instead, under ``audits/self/dogfood/`` -- where the DoD puts live
    receipts and where D11 already SHA-256-pins every tracked file, so a
    hand-edited verdict fails the audit.

    Fail-closed is unchanged, and that is the part that must not drift: no
    receipt, no number. Only *where the receipt lives* moved.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def _endpoints(self, discount=0.5, model="acme/sol"):
        from harness.endpoint_pricing import fetch_endpoints
        transport = ProbeTransport(
            [_offer("Only", 2e-6, 10e-6, discount=discount)], usage=None)
        return fetch_endpoints(transport, "k", model)

    def _record(self, semantics=DISCOUNT_IS_MULTIPLIER, discount=0.5,
                model="acme/sol", **kw):
        return record_discount_semantics(
            {"semantics": semantics, "model": model,
             "fingerprint": {"model": model, "max_discount": discount}},
            **kw)

    def test_the_default_receipt_is_in_the_checkout_not_the_config_dir(self):
        target = os.path.abspath(discount_verdict_path())
        self.assertEqual(target,
                         os.path.abspath(os.path.join(REPO_ROOT,
                                                      ECONOMICS_VERDICT_PATH)))
        # Inside the checkout, and nowhere near the operator's config dir --
        # the whole point: the answer travels with the code.
        self.assertEqual(os.path.commonpath([REPO_ROOT, target]),
                         os.path.abspath(REPO_ROOT))
        self.assertFalse(target.startswith(os.path.abspath(CONFIG_DIR)))
        # ...and inside the directory D11 hash-pins, so a silent edit to the
        # verdict is a deterministic audit failure rather than a quiet change
        # to the cost model.
        self.assertEqual(ECONOMICS_VERDICT_PATH.replace("\\", "/").split("/")[:3],
                         ["audits", "self", "dogfood"])

    def test_the_gate_reads_no_machine_local_state(self):
        """A machine-global fallback would quietly restore the disagreement
        this change exists to remove, so it is pinned at the source."""
        with open(harness.discount_gate.__file__, encoding="utf-8") as stream:
            source = stream.read()
        self.assertNotIn("CONFIG_DIR", source)
        self.assertNotIn("expanduser", source)

    def test_a_machine_local_verdict_no_longer_satisfies_the_gate(self):
        """A conclusive verdict exactly where the old gate looked must not
        unblock the gate.

        HOME points at this temp tree for the duration, so any `~/.config`
        resolution the module might ever grow lands on a file this test
        controls -- the refusal below is then a real statement about the
        resolution, not about an absent file somewhere else on the disk.
        """
        endpoints = self._endpoints()
        with mock.patch.dict(os.environ, {"HOME": self.repo,
                                          "USERPROFILE": self.repo}):
            machine_local = os.path.join(
                os.path.expanduser("~/.config/harness"), "economics.json")
            os.makedirs(os.path.dirname(machine_local), exist_ok=True)
            with open(machine_local, "w", encoding="utf-8") as stream:
                json.dump({"semantics": DISCOUNT_IS_MULTIPLIER,
                           "model": "acme/sol",
                           "fingerprint": {"model": "acme/sol",
                                           "max_discount": 0.5}}, stream)
            with self.assertRaises(HarnessError) as ctx:
                resolve_discount_semantics(endpoints, repo_root=self.repo)

        message = str(ctx.exception)
        self.assertIn("committed semantics verdict", message)
        # The refusal points at the repo receipt, i.e. at what to produce.
        self.assertIn(discount_verdict_path(repo_root=self.repo), message)

    def test_recording_writes_repo_evidence_that_the_gate_reads_back(self):
        endpoints = self._endpoints()
        buffer = io.StringIO()
        with redirect_stderr(buffer):
            target = self._record(repo_root=self.repo)

        self.assertEqual(target, discount_verdict_path(repo_root=self.repo))
        self.assertTrue(os.path.isfile(target))
        with open(target, "rb") as stream:
            raw = stream.read()
        # LF, because this file gets committed and CRLF would dirty the tree
        # immediately after the gate that is meant to prove it clean.
        self.assertNotIn(b"\r\n", raw)
        self.assertEqual(json.loads(raw.decode("utf-8"))["semantics"],
                         DISCOUNT_IS_MULTIPLIER)
        # The operator is told it is evidence they own, not local state.
        self.assertIn("commit it", buffer.getvalue())

        # Round trip with NO path override: the committed receipt is enough.
        semantics = resolve_discount_semantics(endpoints, repo_root=self.repo)
        self.assertEqual(semantics, DISCOUNT_IS_MULTIPLIER)
        self.assertAlmostEqual(
            endpoints.endpoints[0].blended_usd(8000, 1000, semantics=semantics),
            8000 * 1e-6 + 1000 * 5e-6)

    def test_an_explicit_path_still_overrides_for_hermetic_runs(self):
        """`--verdict-path` exists so tests and CI never write the checkout."""
        endpoints = self._endpoints()
        scratch = os.path.join(self.repo, "scratch-verdict.json")
        self._record(path=scratch)
        self.assertFalse(os.path.exists(
            os.path.normpath(os.path.join(self.repo, ECONOMICS_VERDICT_PATH))))
        semantics = resolve_discount_semantics(endpoints, path=scratch,
                                               repo_root=self.repo)
        self.assertEqual(semantics, DISCOUNT_IS_MULTIPLIER)


class ReceiptIdentityTests(unittest.TestCase):
    """A receipt is only evidence about the model and the format it names.

    The gate already refused a missing receipt and a stale promotion. Two
    ways to get a wrong number anyway sat right beside them: ``fingerprint``
    recorded which model a verdict was measured on and never compared it, so a
    receipt measured on ``acme/model-A`` unblocked ``acme/model-B`` at the same
    discount depth; and ``ECONOMICS_SCHEMA_VERSION`` was stamped into every
    receipt and never read, so a receipt in a format this code has never seen
    was honoured as authoritative. Both are the 2x error arriving by the door
    the probe does not watch.

    Every test here drives the real gate. The loader is the thing under
    suspicion: it returned the record happily in both cases above.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def _endpoints(self, discount=0.5, model="acme/sol"):
        from harness.endpoint_pricing import fetch_endpoints
        transport = ProbeTransport(
            [_offer("Only", 2e-6, 10e-6, discount=discount)], usage=None)
        return fetch_endpoints(transport, "k", model)

    def _record(self, semantics=DISCOUNT_IS_MULTIPLIER, discount=0.5,
                model="acme/sol", **kw):
        return record_discount_semantics(
            {"semantics": semantics, "model": model,
             "fingerprint": {"model": model, "max_discount": discount}},
            **kw)

    def _handwritten(self, record):
        """A receipt the recorder would never write -- because it is exactly
        what a hand-edit or a future probe leaves in the tree."""
        target = os.path.join(self.repo, "handwritten.json")
        with open(target, "w", encoding="utf-8") as stream:
            json.dump(record, stream)
        return target

    def test_an_inconclusive_receipt_is_refused_by_name(self):
        """`unresolved`/`ambiguous` are never recorded on purpose, but a receipt
        can be hand-edited, and one that declines to decide is not a decision."""
        target = self._handwritten(
            {"schema": ECONOMICS_SCHEMA_VERSION,
             "semantics": DISCOUNT_AMBIGUOUS, "model": "acme/sol",
             "fingerprint": {"model": "acme/sol", "max_discount": 0.5}})
        with self.assertRaises(HarnessError) as ctx:
            resolve_discount_semantics(self._endpoints(), path=target)
        message = str(ctx.exception)
        self.assertIn("not conclusive", message)
        self.assertIn(DISCOUNT_AMBIGUOUS, message)
        self.assertIn(target, message)

    def test_a_receipt_with_an_empty_fingerprint_is_refused_by_name(self):
        """An empty fingerprint cannot be re-checked against the live
        promotion, so it can never be shown to still apply."""
        target = self._handwritten(
            {"schema": ECONOMICS_SCHEMA_VERSION,
             "semantics": DISCOUNT_IS_MULTIPLIER, "model": "acme/sol",
             "fingerprint": {}})
        with self.assertRaises(HarnessError) as ctx:
            resolve_discount_semantics(self._endpoints(), path=target)
        message = str(ctx.exception)
        self.assertIn("promotion fingerprint", message)
        self.assertIn(target, message)

    def test_a_receipt_recording_no_promotion_is_refused_by_name(self):
        """The model matches, but there is no promotion recorded to re-check
        against -- so 'the answer may no longer hold' can never be asked."""
        target = self._handwritten(
            {"schema": ECONOMICS_SCHEMA_VERSION,
             "semantics": DISCOUNT_IS_MULTIPLIER, "model": "acme/sol",
             "fingerprint": {"model": "acme/sol"}})
        with self.assertRaises(HarnessError) as ctx:
            resolve_discount_semantics(self._endpoints(), path=target)
        self.assertIn("promotion fingerprint", str(ctx.exception))

    def test_a_verdict_measured_on_another_model_does_not_unblock_this_one(self):
        """Same depth, same promotion, different model, still not an answer."""
        self._record(model="acme/model-A", repo_root=self.repo)
        endpoints = self._endpoints(model="acme/model-B")
        with self.assertRaises(HarnessError) as ctx:
            resolve_discount_semantics(endpoints, repo_root=self.repo)
        message = str(ctx.exception)
        # Names BOTH models: "borrowed a verdict" and "the promotion changed"
        # are different operator problems with different fixes.
        self.assertIn("acme/model-A", message)
        self.assertIn("acme/model-B", message)
        self.assertIn("2.00x", message)
        self.assertIn(discount_verdict_path(repo_root=self.repo), message)

    def test_a_variant_suffix_is_the_same_model_to_the_feed(self):
        """The guard above must not become a new refusal: the probe stamps the
        canonical id, so `a/m:free` has to match a receipt measured on `a/m`."""
        self._record(model="acme/sol", repo_root=self.repo)
        endpoints = self._endpoints(model="acme/sol:free")
        self.assertEqual(
            resolve_discount_semantics(endpoints, repo_root=self.repo),
            DISCOUNT_IS_MULTIPLIER)

    def test_a_receipt_in_a_schema_this_code_cannot_read_is_refused(self):
        """`schema: 99` is a file from the future. Its fields cannot be
        guessed at, so it is not evidence about anything."""
        target = os.path.join(self.repo, "future.json")
        with open(target, "w", encoding="utf-8") as stream:
            json.dump({"schema": 99, "semantics": DISCOUNT_IS_MULTIPLIER,
                       "model": "acme/sol",
                       "fingerprint": {"model": "acme/sol",
                                       "max_discount": 0.5}}, stream)
        with self.assertRaises(HarnessError) as ctx:
            resolve_discount_semantics(self._endpoints(), path=target)
        message = str(ctx.exception)
        self.assertIn("schema", message)
        self.assertIn("99", message)
        self.assertIn(target, message)
        self.assertIn(str(ECONOMICS_SCHEMA_VERSION), message)

    def test_recording_refuses_a_receipt_the_gate_could_not_honour(self):
        """Writing evidence that the gate will reject is the same defect as
        shipping a verdict nothing can read, so the recorder is the second
        half of the same pin."""
        with self.assertRaises(HarnessError) as ctx:
            record_discount_semantics(
                {"schema": 99, "semantics": DISCOUNT_IS_MULTIPLIER,
                 "model": "acme/sol",
                 "fingerprint": {"model": "acme/sol", "max_discount": 0.5}},
                repo_root=self.repo)
        self.assertIn("could not honour", str(ctx.exception))
        self.assertFalse(os.path.exists(
            discount_verdict_path(repo_root=self.repo)))

    def test_a_stamped_schema_is_written_so_the_receipt_can_come_back(self):
        """The gate refuses an unstamped receipt, so the recorder writes one;
        otherwise its own output would be unreadable."""
        target = self._record(repo_root=self.repo)
        with open(target, encoding="utf-8") as stream:
            written = json.load(stream)
        self.assertEqual(written["schema"], ECONOMICS_SCHEMA_VERSION)
        self.assertEqual(
            resolve_discount_semantics(self._endpoints(), repo_root=self.repo),
            DISCOUNT_IS_MULTIPLIER)

    def test_the_verdict_path_the_operator_reads_has_no_mixed_separators(self):
        """`C:\\repo/audits/self/...` is what every refusal quotes and what the
        "wrote repo evidence" line prints: the one string here most likely to
        be copied into a shell."""
        target = discount_verdict_path(repo_root=self.repo)
        self.assertEqual(target, os.path.normpath(target))
        self.assertNotIn("/\\", target)
        self.assertNotIn("\\/", target)
        # And the default the operator actually sees, not just a temp root.
        self.assertEqual(discount_verdict_path(),
                         os.path.normpath(discount_verdict_path()))
        with self.assertRaises(HarnessError) as ctx:
            resolve_discount_semantics(self._endpoints(), repo_root=self.repo)
        self.assertIn(target, str(ctx.exception))
