"""Rankings-driven candidate refresh (ruling 7): the daily rankings API is
the evidence source; candidates must pass the ONE-vote probe before they are
proposed as probed. All hermetic (fake transport, no network)."""
import unittest

from harness.rankings import (_probe_vote, aggregate_rankings,
                              candidates_from_rankings, fetch_rankings)
from tests._fake import FakeTransport, comp, m

RANK_URL = "https://openrouter.ai/api/v1/datasets/rankings-daily"


def _rows():
    rows = []
    # climber: 100/day -> 300/day over the window
    for i in range(10):
        rows.append({"date": f"2026-09-{10 + i:02d}",
                     "model_permaslug": "deepseek-v4.1-flash",
                     "total_tokens": 100 + 20 * i})
    # steady high-volume incumbent
    for i in range(10):
        rows.append({"date": f"2026-09-{10 + i:02d}",
                     "model_permaslug": "gpt-5.6-luna",
                     "total_tokens": 1000})
    # falling: 300/day -> 100/day
    for i in range(10):
        rows.append({"date": f"2026-09-{10 + i:02d}",
                     "model_permaslug": "old-gen-v1",
                     "total_tokens": 300 - 20 * i})
    return rows


class FetchTests(unittest.TestCase):
    def test_fetch_parses_rows(self):
        fake = FakeTransport(models=[])
        fake.calls = []
        # fetch_rankings uses transport.get directly; emulate it.
        class T:
            def get(self, url, api_key, timeout=30):
                self.url = url
                return {"data": _rows()}
        t = T()
        rows = fetch_rankings(t, "k")
        self.assertEqual(t.url, RANK_URL)
        self.assertEqual(len(rows), 30)
        self.assertEqual(rows[0]["slug"], "deepseek-v4.1-flash")

    def test_fetch_raises_on_empty(self):
        from harness.errors import HarnessError

        class T:
            def get(self, url, api_key, timeout=30):
                return {"data": []}
        with self.assertRaises(HarnessError):
            fetch_rankings(T(), "k")

    def test_fetch_drops_malformed_rows(self):
        class T:
            def get(self, url, api_key, timeout=30):
                return {"data": [{"date": "2026-09-10"}, 17,
                                 {"date": "2026-09-11",
                                  "model_permaslug": "x/y",
                                  "total_tokens": "5"}]}
        rows = fetch_rankings(T(), "k")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["total_tokens"], 5.0)


class AggregateTests(unittest.TestCase):
    def test_totals_sorted_with_trends(self):
        agg = aggregate_rankings(_rows())
        self.assertEqual(agg["window"]["days"], 10)
        top = agg["totals"]
        self.assertEqual(top[0]["slug"], "gpt-5.6-luna")
        by_slug = {t["slug"]: t["trend"] for t in top}
        self.assertEqual(by_slug["deepseek-v4.1-flash"], "climbing")
        self.assertEqual(by_slug["gpt-5.6-luna"], "stable")
        self.assertEqual(by_slug["old-gen-v1"], "falling")

    def test_climbers_listed(self):
        agg = aggregate_rankings(_rows())
        self.assertIn("deepseek-v4.1-flash", agg["climbers"])
        self.assertNotIn("old-gen-v1", agg["climbers"])


class CandidateTests(unittest.TestCase):
    def test_slug_matches_catalog_tail(self):
        from harness.rankings import _slug_matches_model
        self.assertTrue(_slug_matches_model("deepseek-v4.1-flash",
                                            "deepseek/deepseek-v4.1-flash"))
        self.assertTrue(_slug_matches_model("gpt-5.6-luna",
                                            "openai/gpt-5.6-luna"))
        self.assertFalse(_slug_matches_model("gpt-5.6-luna",
                                             "openai/gpt-5.6-luna-mini"))

    def test_candidates_intersect_catalog_and_shipped(self):
        agg = aggregate_rankings(_rows())
        catalog = ["deepseek/deepseek-v4.1-flash", "openai/gpt-5.6-luna",
                   "vendor/old-gen-v1"]
        out = candidates_from_rankings(
            agg["totals"], catalog,
            shipped_ids=["openai/gpt-5.6-luna"])
        ids = {c["model_id"] for c in out["ranked_in_catalog"]}
        self.assertEqual(ids, {"deepseek/deepseek-v4.1-flash",
                               "openai/gpt-5.6-luna",
                               "vendor/old-gen-v1"})
        # gpt-5.6-luna is shipped coverage; old-gen-v1 is a falling new name
        # but still a proposal (traffic evidence alone does not bless it).
        proposed = {c["model_id"] for c in out["proposed"]}
        self.assertIn("vendor/old-gen-v1", proposed)
        self.assertNotIn("openai/gpt-5.6-luna", proposed)


class ProbeGateTests(unittest.TestCase):
    def _gov(self, fake):
        from harness.spend import SpendGovernor
        import os
        import tempfile
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        return SpendGovernor(fake, "sk-test", byok_prefixes_path=os.path.join(
            td.name, "byok.json"))

    def _report_gov(self, fake):
        """A governor whose live /models fetch is stubbed to a tiny catalog
        (the standard FakeTransport covers it; no per-call injection needed)."""
        import os
        import tempfile
        from harness.spend import SpendGovernor
        fake.models = [m("deepseek/deepseek-v4.1-flash"),
                       m("openai/gpt-5.6-luna")]
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        return SpendGovernor(fake, "sk-test", byok_prefixes_path=os.path.join(
            td.name, "byok.json"))

    def test_report_end_to_end_via_module_seam(self):
        """The full report with the shipped-pool default resolved through the
        module-level seam (the one hermetic patch point; no injected loader)."""
        import unittest.mock
        from harness import rankings
        fake = FakeTransport(posts=[])
        fake.get = lambda url, api_key, timeout=30: (
            {"data": _rows()} if url.endswith("rankings-daily")
            else {"data": fake.models})
        gov = self._report_gov(fake)
        shipped = ["openai/gpt-5.6-luna"]
        with unittest.mock.patch.object(rankings, "shipped_model_ids",
                                        return_value=shipped):
            report = rankings.build_rankings_report(fake, "k", gov)
        self.assertEqual(report["window"]["days"], 10)
        self.assertEqual(report["top"][0]["slug"], "gpt-5.6-luna")
        self.assertIn("deepseek-v4.1-flash", report["climbers"])
        self.assertEqual(
            {c["model_id"] for c in report["ranked_in_catalog"]},
            {"deepseek/deepseek-v4.1-flash", "openai/gpt-5.6-luna"})
        # luna is shipped coverage; the v4.1-flash seat is proposed.
        self.assertEqual([c["model_id"] for c in report["proposed_candidates"]],
                         ["deepseek/deepseek-v4.1-flash"])
        self.assertEqual(report["probed_candidates"], [])

    def test_probe_passes_on_parseable_json(self):
        fake = FakeTransport(models=[m("deepseek/deepseek-v4.1-flash")],
                             posts=[comp('{"claim_1": {"real": false}}')])
        gov = self._gov(fake)
        ok, detail, _cost = _probe_vote(fake, "k", gov,
                                        "deepseek/deepseek-v4.1-flash")
        self.assertTrue(ok)
        self.assertIn("parseable", detail)
        payload = fake.chat_posts()[0][2]
        # The one-vote probe runs the vote-lane contract: reasoning disabled.
        self.assertEqual(payload["reasoning"], {"effort": "none"})
        self.assertGreaterEqual(payload["max_tokens"], 4096)

    def test_probe_fails_on_prose(self):
        fake = FakeTransport(models=[m("x/a")], posts=[comp("some prose")])
        ok, detail, _cost = _probe_vote(fake, "k", self._gov(fake), "x/a")
        self.assertFalse(ok)
        self.assertIn("parseable", detail)

    def test_probe_fails_on_http_error(self):
        fake = FakeTransport(models=[m("x/a")],
                             posts=[(503, {"error": {"message": "down"}})])
        ok, detail, _cost = _probe_vote(fake, "k", self._gov(fake), "x/a")
        self.assertFalse(ok)
        self.assertIn("503", detail)

    def test_probe_fails_on_reasoning_only(self):
        fake = FakeTransport(models=[m("x/a")],
                             posts=[comp("", reasoning="thinking...")])
        ok, detail, _cost = _probe_vote(fake, "k", self._gov(fake), "x/a")
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
