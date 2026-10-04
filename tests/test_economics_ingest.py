"""EV-0 model economics: benchmark + per-provider endpoint ingest.

All hermetic (fake transport, no network). The endpoint feed is the only
source of `discount`, `overrides`, cached-input rates and published uptime,
so the parsing here is what every later cost number rests on -- and a
silently half-parsed price table would still look plausible downstream.
"""
import unittest
from contextlib import redirect_stderr
from io import StringIO
from unittest import mock

from harness import output
from harness.config import (DISCOUNT_IS_MULTIPLIER, DISCOUNT_LISTED_IS_EFFECTIVE,
                            MAX_ENDPOINT_FETCHES_PER_RUN,
                            OPENROUTER_BENCHMARKS_URL, OPENROUTER_ENDPOINTS_URL)
from harness.economics import (EndpointPrice, benchmarks_by_model,
                               build_economics_report, fetch_benchmarks,
                               fetch_endpoints, fetch_endpoints_for)
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


class EligibilityTests(unittest.TestCase):
    def test_degraded_uptime_makes_an_offer_ineligible(self):
        healthy = EndpointPrice("P", 1e-6, 2e-6, uptime_1d=99.9)
        degraded = EndpointPrice("Q", 1e-6, 2e-6, uptime_1d=40.0)
        self.assertTrue(healthy.is_eligible(min_uptime=90.0))
        self.assertFalse(degraded.is_eligible(min_uptime=90.0))

    def test_unpublished_uptime_does_not_disqualify(self):
        endpoint = EndpointPrice("P", 1e-6, 2e-6, uptime_1d=None)
        self.assertTrue(endpoint.is_eligible(min_uptime=90.0))


class FakeFeeds:
    """Both feeds `build_economics_report` reads, plus a count of the
    endpoint GETs it ACTUALLY issued.

    The count is the whole point: a budget test that only reads the returned
    artifact proves nothing, because the artifact is written from the
    budget that was passed in rather than from the requests that were sent.
    """

    def __init__(self, bodies, error_models=()):
        self.bodies = bodies
        self.error_models = set(error_models)
        self.endpoint_gets = []

    def get(self, url, api_key, timeout=15):
        if url.startswith(OPENROUTER_BENCHMARKS_URL):
            return {"data": [{"source": "openrouter",
                              "model_permaslug": "acme/a",
                              "benchmark_type": "gpqa_diamond",
                              "accuracy": 0.71, "total_tasks": 300}],
                    "meta": {"as_of": "2026-10-03T00:00:00Z"}}
        model = url.split("/models/", 1)[1].rsplit("/endpoints", 1)[0]
        self.endpoint_gets.append(model)
        if model in self.error_models:
            return {"error": {"message": f"no such model {model}"}}
        return self.bodies[model]


class ReportBudgetTests(unittest.TestCase):
    """The PRODUCTION path, not the helper.

    `build_economics_report` used to loop `fetch_endpoints` directly, so the
    budget was enforced in a function nobody but the tests called: a live
    `harness economics --models a,b,c --max-fetches 1` reported
    `"max_fetches": 1` and priced all three, and with no flag the shortlist
    was sliced `[:None]` so the configured default never applied either. These
    tests call the report builder itself with a fake transport, so the guard
    cannot be bypassed again without one of them going red.
    """

    def _transport(self, models=("acme/a", "acme/b"), error_models=()):
        return FakeFeeds(
            {m: _endpoint_body(m, [_offer("P", 1e-6, 2e-6)]) for m in models},
            error_models=error_models)

    def _report(self, transport, **kw):
        """Run the real report path; return (report, stderr)."""
        buffer = StringIO()
        with redirect_stderr(buffer):
            report = build_economics_report(
                None, None, api_key="k", transport=transport, **kw)
        return report, buffer.getvalue()

    def test_explicit_shortlist_over_budget_is_refused_before_any_request(self):
        """The exact case the audit ran live: three models, a budget of one."""
        fake = self._transport(("acme/a", "acme/b", "acme/c"))
        buffer = StringIO()
        with redirect_stderr(buffer), self.assertRaises(HarnessError) as ctx:
            build_economics_report(None, None, api_key="k", transport=fake,
                                   benchmark_ids=["acme/a", "acme/b", "acme/c"],
                                   max_fetches=1)
        self.assertIn("budget is 1 models per run", str(ctx.exception))
        # Refusal means refusal: not a truncated report that reads as full
        # coverage, and not a request already spent to find that out.
        self.assertEqual(fake.endpoint_gets, [])

    def test_the_budget_in_the_artifact_is_the_budget_that_ran(self):
        fake = self._transport(("acme/a", "acme/b"))
        report, _err = self._report(fake, benchmark_ids=["acme/a", "acme/b"],
                                    max_fetches=2)

        self.assertEqual(sorted(report["endpoint_models"]), ["acme/a", "acme/b"])
        self.assertEqual(len(fake.endpoint_gets), 2)
        self.assertEqual(report["max_fetches"], 2)
        self.assertEqual(report["shortlist_size"], 2)
        self.assertEqual(report["endpoint_errors"], {})

    def test_an_unset_budget_is_the_configured_cap_not_an_unbounded_slice(self):
        """No --max-fetches means the CLI passes None. `[:None]` used to make
        that mean every shipped model, however many."""
        fake = self._transport(("acme/a", "acme/b", "acme/c"))
        with mock.patch("harness.economics.shipped_model_ids",
                        return_value=["acme/a", "acme/b:free", "acme/c"]), \
             mock.patch("harness.economics.MAX_ENDPOINT_FETCHES_PER_RUN", 2), \
             redirect_stderr(StringIO()), \
             self.assertRaises(HarnessError) as ctx:
            build_economics_report(None, None, api_key="k", transport=fake)
        self.assertIn("budget is 2 models per run", str(ctx.exception))
        self.assertEqual(fake.endpoint_gets, [])

    def test_shipped_pool_within_the_cap_is_priced_and_reports_the_cap(self):
        # The shipped list carries a variant suffix; the feed is keyed by the
        # canonical id, so the default path strips before it requests.
        fake = self._transport(("acme/a", "acme/b"))
        with mock.patch("harness.economics.shipped_model_ids",
                        return_value=["acme/a:free", "acme/b"]), \
             mock.patch("harness.economics.MAX_ENDPOINT_FETCHES_PER_RUN", 40):
            report, err = self._report(fake)
        self.assertEqual(sorted(report["endpoint_models"]), ["acme/a", "acme/b"])
        self.assertEqual(fake.endpoint_gets, ["acme/a", "acme/b"])
        self.assertEqual(report["max_fetches"], 40)
        self.assertIn("shipped lane models", err)

    def test_a_model_that_errors_is_visible_not_just_recorded(self):
        """`cohere/north-mini-code` left the catalog during a live run and the
        failure landed in `endpoint_errors` with a clean stderr and a
        plausible-looking artifact. The receipt entry is not enough: the
        operator has to be TOLD."""
        fake = self._transport(("acme/a", "acme/gone"),
                               error_models={"acme/gone"})
        report, err = self._report(fake, benchmark_ids=["acme/a", "acme/gone"],
                                   max_fetches=2)

        # Still isolated: one dead model does not cost the others their price.
        self.assertEqual(sorted(report["endpoint_models"]), ["acme/a"])
        self.assertIn("acme/gone", report["endpoint_errors"])
        # ...and never silent.
        self.assertIn("[warn] economics: endpoint feed failed for acme/gone", err)
        self.assertIn("acme/gone", err)
        self.assertIn("1 of 2 requested models have no endpoint price", err)

    def test_the_failure_warning_survives_quiet(self):
        """`--quiet` suppresses progress chatter. A swallowed model is not
        chatter: `[warn]` is in output._AUDIBLE_PREFIXES for this reason."""
        fake = self._transport(("acme/gone",), error_models={"acme/gone"})
        with mock.patch.object(output, "QUIET", True):
            _report, err = self._report(fake, benchmark_ids=["acme/gone"],
                                        max_fetches=1)
        self.assertIn("[warn] economics: endpoint feed failed for acme/gone", err)
