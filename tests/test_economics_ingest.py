"""EV-0 model economics: benchmark + per-provider endpoint ingest.

All hermetic (fake transport, no network). The endpoint feed is the only
source of `discount`, `overrides`, cached-input rates and published uptime,
so the parsing here is what every later cost number rests on -- and a
silently half-parsed price table would still look plausible downstream.
"""
import unittest
from contextlib import redirect_stderr
from io import StringIO

from harness.config import (DISCOUNT_IS_MULTIPLIER, DISCOUNT_LISTED_IS_EFFECTIVE,
                            MAX_ENDPOINT_FETCHES_PER_RUN,
                            OPENROUTER_BENCHMARKS_URL, OPENROUTER_ENDPOINTS_URL)
from harness.benchmark_ingest import benchmarks_by_model, fetch_benchmarks
from harness.endpoint_pricing import (EndpointPrice, fetch_endpoints,
                                      fetch_endpoints_for)
from harness.errors import HarnessError


def _endpoint_body(model, endpoints):
    return {"data": {"id": model, "endpoints": endpoints}}


def _offer(name, prompt, completion, **kw):
    pricing = {"prompt": str(prompt), "completion": str(completion)}
    pricing.update({k: v for k, v in kw.items() if k != "supported_parameters"})
    body = {"name": name, "provider_name": name, "pricing": pricing,
            "context_length": 128000, "max_completion_tokens": 32000}
    if "supported_parameters" in kw:
        body["supported_parameters"] = kw["supported_parameters"]
    body.update({k: v for k, v in kw.items()
                 if k in ("uptime_last_1d", "uptime_last_30m",
                          "latency_last_30m", "tag")})
    return body


class FakeEndpoints:
    """Serves a per-model endpoint table and records every GET."""

    def __init__(self, bodies, error_models=()):
        self.bodies = bodies
        self.error_models = set(error_models)
        self.gets = []

    def get(self, url, api_key, timeout=15):
        self.gets.append(url)
        model = url.split("/models/", 1)[1].rsplit("/endpoints", 1)[0]
        if model in self.error_models:
            return {"error": {"message": f"no such model {model}"}}
        return self.bodies[model]


class FetchEndpointsTests(unittest.TestCase):
    def test_parses_discount_overrides_cache_and_uptime(self):
        body = _endpoint_body("acme/model", [
            _offer("Relace", 0.000003, 0.0000024, discount=0.25,
                   input_cache_read=0.000000001, uptime_last_1d=99.5,
                   tag="relace"),
            _offer("OpenAI", 0.00002, 0.0000004, uptime_last_1d=98.0),
        ])
        got = fetch_endpoints(FakeEndpoints({"acme/model": body}), "k", "acme/model")

        self.assertEqual(got.model_id, "acme/model")
        self.assertEqual(len(got.endpoints), 2)
        self.assertAlmostEqual(got.max_discount, 0.25)
        self.assertTrue(got.has_discount)

        relace = got.endpoints[0]
        self.assertAlmostEqual(relace.prompt, 0.000003)
        self.assertAlmostEqual(relace.completion, 0.0000024)
        self.assertAlmostEqual(relace.input_cache_read, 0.000000001)
        self.assertAlmostEqual(relace.uptime_1d, 99.5)
        self.assertEqual(relace.tag, "relace")
        # An absent discount is 0.0, not a crash: most rows carry none.
        self.assertAlmostEqual(got.endpoints[1].discount, 0.0)
        self.assertIsNone(got.endpoints[1].input_cache_read)

    def test_parses_long_context_override_step(self):
        body = _endpoint_body("acme/sol", [
            _offer("OpenAI", 0.000002, 0.00001, discount=0.5, overrides=[
                {"min_prompt_tokens": 272000, "prompt": "0.000004",
                 "completion": "0.000015"}]),
        ])
        got = fetch_endpoints(FakeEndpoints({"acme/sol": body}), "k", "acme/sol")
        endpoint = got.endpoints[0]

        below = endpoint.rates_for(1000, True, DISCOUNT_LISTED_IS_EFFECTIVE)
        self.assertAlmostEqual(below[0], 0.000002)
        above = endpoint.rates_for(300000, True, DISCOUNT_LISTED_IS_EFFECTIVE)
        self.assertAlmostEqual(above[0], 0.000004)
        self.assertAlmostEqual(above[1], 0.000015)

    def test_discount_only_applies_under_the_multiplier_reading(self):
        endpoint = EndpointPrice("P", 2e-6, 10e-6, discount=0.5)
        as_listed = endpoint.rates_for(100, True, DISCOUNT_LISTED_IS_EFFECTIVE)
        self.assertAlmostEqual(as_listed[0], 2e-6)
        as_multiplier = endpoint.rates_for(100, True, DISCOUNT_IS_MULTIPLIER)
        self.assertAlmostEqual(as_multiplier[0], 1e-6)
        self.assertAlmostEqual(as_multiplier[1], 5e-6)

    def test_strips_variant_suffix_before_requesting(self):
        fake = FakeEndpoints({"acme/model": _endpoint_body(
            "acme/model", [_offer("P", 0, 0)])})
        got = fetch_endpoints(fake, "k", "acme/model:free")
        self.assertEqual(fake.gets[0],
                         "https://openrouter.ai/api/v1/models/acme/model/endpoints")
        self.assertFalse(got.has_discount)

    def test_error_body_raises(self):
        fake = FakeEndpoints({}, error_models={"acme/gone"})
        with self.assertRaises(HarnessError) as ctx:
            fetch_endpoints(fake, "k", "acme/gone")
        self.assertIn("returned an error", str(ctx.exception))

    def test_malformed_payloads_raise_rather_than_degrade(self):
        for body in ({}, {"data": {}}, {"data": {"endpoints": "nope"}},
                     _endpoint_body("acme/m", [])):
            with self.assertRaises(HarnessError):
                fetch_endpoints(FakeEndpoints({"acme/m": body}), "k", "acme/m")

    def test_non_object_payload_raises(self):
        with self.assertRaises(HarnessError):
            fetch_endpoints(FakeEndpoints({"acme/m": ["nope"]}), "k", "acme/m")

    def test_unparsable_required_price_raises(self):
        body = _endpoint_body("acme/m", [_offer("P", "not-a-price", 1e-6)])
        with self.assertRaises(HarnessError):
            fetch_endpoints(FakeEndpoints({"acme/m": body}), "k", "acme/m")

    def test_budget_refuses_an_unbounded_shortlist(self):
        """A full catalog is ~400 models; a fan-out would eat the day's
        500-request benchmark allowance on rows nobody routes to."""
        ids = [f"acme/m{i}" for i in range(MAX_ENDPOINT_FETCHES_PER_RUN + 1)]
        with self.assertRaises(HarnessError) as ctx:
            fetch_endpoints_for(FakeEndpoints({}), "k", ids)
        self.assertIn("shortlist-scoped", str(ctx.exception))

    def test_shortlist_within_budget_fetches_each_once(self):
        """A variant suffix is the SAME model: two GETs for one row would
        spend the daily request budget twice over."""
        ids = ["acme/a", "acme/b", "acme/a:free"]
        fake = FakeEndpoints({m: _endpoint_body(m, [_offer("P", 1e-6, 2e-6)])
                              for m in ("acme/a", "acme/b")})
        got = fetch_endpoints_for(fake, "k", ids)
        self.assertEqual(sorted(got["priced"]), ["acme/a", "acme/b"])
        self.assertEqual(got["errors"], {})
        self.assertEqual(len(fake.gets), 2)

    def test_a_failing_model_is_isolated_and_announced(self):
        """One model leaving the catalog must not cost the operator every
        other price -- and must not disappear silently while it does."""
        fake = FakeEndpoints(
            {"acme/ok": _endpoint_body("acme/ok", [_offer("P", 1e-6, 2e-6)])},
            error_models={"acme/gone"})
        buffer = StringIO()
        with redirect_stderr(buffer):
            got = fetch_endpoints_for(fake, "k", ["acme/ok", "acme/gone"])

        self.assertEqual(sorted(got["priced"]), ["acme/ok"])
        self.assertIn("acme/gone", got["errors"])
        self.assertIn("[warn] economics: endpoint feed failed for acme/gone",
                      buffer.getvalue())

    def test_endpoint_url_is_the_documented_feed(self):
        self.assertEqual(
            OPENROUTER_ENDPOINTS_URL,
            "https://openrouter.ai/api/v1/models/{slug}/endpoints")
        self.assertIn("acme/m", OPENROUTER_ENDPOINTS_URL.format(slug="acme/m"))


class FetchBenchmarksTests(unittest.TestCase):
    def _transport(self, body):
        class T:
            def __init__(self):
                self.url = None

            def get(self, url, api_key, timeout=30):
                self.url = url
                return body
        return T()

    def test_normalizes_both_source_shapes_onto_one_row(self):
        transport = self._transport({"data": [
            {"source": "artificial-analysis", "model_permaslug": "gpt-x",
             "display_name": "GPT X", "coding_index": 65.8,
             "agentic_index": 58.3, "intelligence_index": 71.2},
            {"source": "openrouter", "model_permaslug": "gpt-x",
             "benchmark_type": "gpqa_diamond", "accuracy": 0.72,
             "accuracy_stddev": 0.03, "avg_cost_per_task": 0.002,
             "total_tasks": 300, "last_run_timestamp": "2026-06-03T12:00:00Z"},
        ], "meta": {"as_of": "2026-06-03T12:00:00Z", "version": "v1"}})

        report = fetch_benchmarks(transport, "k")
        self.assertEqual(transport.url, OPENROUTER_BENCHMARKS_URL)
        self.assertEqual(len(report["rows"]), 2)

        aa, orow = report["rows"]
        self.assertAlmostEqual(aa["coding_index"], 65.8)
        self.assertAlmostEqual(aa["intelligence_index"], 71.2)
        self.assertIsNone(aa["accuracy"])
        self.assertAlmostEqual(orow["accuracy"], 0.72)
        self.assertAlmostEqual(orow["avg_cost_per_task"], 0.002)
        self.assertEqual(orow["benchmark_type"], "gpqa_diamond")
        # The untouched payload survives so a future field is never lost.
        self.assertEqual(aa["raw"]["display_name"], "GPT X")
        self.assertEqual(report["meta"]["version"], "v1")

        grouped = benchmarks_by_model(report)
        self.assertEqual(sorted(grouped), ["gpt-x"])
        self.assertEqual(len(grouped["gpt-x"]), 2)

    def test_query_params_are_passed_through(self):
        transport = self._transport({"data": [{"model_permaslug": "m"}]})
        fetch_benchmarks(transport, "k", source="artificial-analysis",
                         task_type="coding")
        self.assertEqual(
            transport.url,
            OPENROUTER_BENCHMARKS_URL + "?source=artificial-analysis&task_type=coding")

    def test_unknown_source_is_refused(self):
        transport = self._transport({"data": []})
        with self.assertRaises(HarnessError):
            fetch_benchmarks(transport, "k", source="made-up")

    def test_empty_and_error_payloads_raise(self):
        for body in ({}, {"data": []}, {"data": "nope"},
                     {"error": {"message": "rate limited"}}):
            with self.assertRaises(HarnessError):
                fetch_benchmarks(self._transport(body), "k")

    def test_malformed_rows_are_dropped_not_fatal(self):
        transport = self._transport({"data": [
            {"model_permaslug": "good"}, 17, None, {"no_slug": 1}]})
        report = fetch_benchmarks(transport, "k")
        self.assertEqual([r["model_permaslug"] for r in report["rows"]], ["good"])

