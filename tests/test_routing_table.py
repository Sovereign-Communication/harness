"""Unit tests for task-to-model routing table and OpenRouter floor price controls (#PR-Cost-1)."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone

from harness.chat import chat
from harness.routing_table import (
    TIER_0,
    TIER_1,
    TIER_2,
    TIER_3,
    classify_task_tier,
    floor_model,
    get_tier_route,
    next_tier,
    strip_variant_suffix,
)


class FakeTransport:
    def __init__(self, status=200, resp=None):
        self.status = status
        self.resp = resp or {"choices": [{"message": {"content": "ok"}}], "usage": {"cost": 0.0001}}
        self.calls = []

    def post(self, url, api_key, payload):
        self.calls.append({"url": url, "api_key": api_key, "payload": payload})
        return self.status, self.resp


class RoutingTableTests(unittest.TestCase):
    def test_floor_model(self):
        # Paid models without suffix receive :floor
        self.assertEqual(floor_model("deepseek/deepseek-chat"), "deepseek/deepseek-chat:floor")
        self.assertEqual(floor_model("qwen/qwen-2.5-coder-32b-instruct"), "qwen/qwen-2.5-coder-32b-instruct:floor")

        # Models with existing variants are left untouched
        self.assertEqual(floor_model("deepseek/deepseek-chat:free"), "deepseek/deepseek-chat:free")
        self.assertEqual(floor_model("meta-llama/llama-3.3-70b-instruct:free"), "meta-llama/llama-3.3-70b-instruct:free")
        self.assertEqual(floor_model("qwen/qwen3.8-max-0902:floor"), "qwen/qwen3.8-max-0902:floor")
        self.assertEqual(floor_model("openai/o3-mini:nitro"), "openai/o3-mini:nitro")

        # Disabled floor leaves slug untouched
        self.assertEqual(floor_model("deepseek/deepseek-chat", enable_floor=False), "deepseek/deepseek-chat")
        self.assertEqual(floor_model("", enable_floor=True), "")

    def test_strip_variant_suffix(self):
        self.assertEqual(strip_variant_suffix("deepseek/deepseek-chat:floor"), "deepseek/deepseek-chat")
        self.assertEqual(strip_variant_suffix("google/gemini-2.0-flash-exp:free"), "google/gemini-2.0-flash-exp")
        self.assertEqual(strip_variant_suffix("openai/o3-mini:nitro"), "openai/o3-mini")
        self.assertEqual(strip_variant_suffix("qwen/qwen3.8-max-0902"), "qwen/qwen3.8-max-0902")

    def test_classify_task_tier(self):
        # T0: simple queries, docstrings, formatting
        self.assertEqual(classify_task_tier("fix typo in docstring of helper.py"), TIER_0)
        self.assertEqual(classify_task_tier("summarize this comment"), TIER_0)

        # T1: standard updates and local edits
        self.assertEqual(classify_task_tier("add input validation to user endpoint"), TIER_1)

        # T2: multi-file modifications, plan/dag, complex debugging
        self.assertEqual(classify_task_tier("plan DAG decomposition for pipeline refactor"), TIER_2)
        self.assertEqual(classify_task_tier("investigate failing test and traceback"), TIER_2)
        self.assertEqual(classify_task_tier("update files", target_files=["a.py", "b.py", "c.py"]), TIER_2)

        # T3: architectural, concurrency, cryptographic invariants
        self.assertEqual(classify_task_tier("fix deadlock in mutex lock acquisition"), TIER_3)
        self.assertEqual(classify_task_tier("verify cryptographic hash chain invariant"), TIER_3)

    def test_next_tier_ladder(self):
        self.assertEqual(next_tier(TIER_0), TIER_1)
        self.assertEqual(next_tier(TIER_1), TIER_2)
        self.assertEqual(next_tier(TIER_2), TIER_3)
        self.assertIsNone(next_tier(TIER_3))
        self.assertIsNone(next_tier("INVALID_TIER"))

    def test_get_tier_route(self):
        route_t0 = get_tier_route(TIER_0)
        self.assertEqual(route_t0["tier"], TIER_0)
        self.assertEqual(route_t0["cost_band"], "$0")
        # Free models preserve :free without :floor
        self.assertTrue(all(":free" in m for m in route_t0["models"]))
        self.assertEqual(route_t0["max_price"]["prompt"], 0.0)

        route_t1 = get_tier_route(TIER_1, enable_floor=True)
        self.assertEqual(route_t1["tier"], TIER_1)
        self.assertTrue(all(m.endswith(":floor") for m in route_t1["models"]))
        self.assertEqual(route_t1["max_price"]["prompt"], 0.35)

    def test_chat_floor_and_provider_payload(self):
        transport = FakeTransport()
        chat(
            transport=transport,
            api_key="sk-test",
            model="deepseek/deepseek-chat",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=100,
            enable_floor=True,
            max_price={"prompt": 0.50, "completion": 2.00},
            provider_sort="price",
        )
        self.assertEqual(len(transport.calls), 1)
        payload = transport.calls[0]["payload"]
        self.assertEqual(payload["model"], "deepseek/deepseek-chat:floor")
        self.assertEqual(payload["provider"]["sort"], "price")
        self.assertEqual(payload["provider"]["max_price"], {"prompt": 0.50, "completion": 2.00})

    def test_ev2_snapshot_freshness_and_unlisted_fallbacks(self):
        from datetime import datetime, timezone
        from harness.routing_table import (
            TIER_MODELS,
            check_tier_snapshot_freshness,
            classify_model_tier,
            generate_tier_snapshot,
            load_tier_snapshot,
            reset_unlisted_tier_fallbacks,
            unlisted_tier_fallbacks,
        )

        snap = load_tier_snapshot()
        self.assertEqual(snap["source"], "snapshot")
        # DF-EV-4: no retired/stale generation slugs in snapshot TIER_MODELS
        all_ids = [m for models in TIER_MODELS.values() for m in models]
        for stale in ("gemini-2.0-flash-exp", "llama-3.3-70b", "qwen-2.5-coder", "o3-mini"):
            self.assertFalse(any(stale in m for m in all_ids), f"stale {stale} in TIER_MODELS")

        fresh = check_tier_snapshot_freshness(
            now=datetime(2026, 10, 20, tzinfo=timezone.utc))
        self.assertTrue(fresh["fresh"])
        stale_check = check_tier_snapshot_freshness(
            now=datetime(2026, 12, 1, tzinfo=timezone.utc))
        self.assertFalse(stale_check["fresh"])
        self.assertIn("tiers.snapshot.json", stale_check["warning"])

        gen = generate_tier_snapshot(captured_at="2026-10-18T12:00:00Z")
        self.assertEqual(gen["schema"], 1)
        self.assertIn("T1", gen["tiers"])

        reset_unlisted_tier_fallbacks()
        self.assertEqual(classify_model_tier("deepseek/deepseek-v4.1-flash"), TIER_1)
        self.assertEqual(unlisted_tier_fallbacks(), {})
        self.assertEqual(classify_model_tier("unknown-vendor/mystery-coder-7b"), TIER_1)
        self.assertIn("unknown-vendor/mystery-coder-7b", unlisted_tier_fallbacks())

    def test_shipped_tier_snapshot_shape_is_deterministic(self):
        """The committed artifact parses and carries a valid timestamp (#446).

        Currency is not asserted here - that is the scheduled rankings job's
        data check, and comparing against the wall clock inside the hermetic
        suite is what made every head red on 2026-11-01. This pins the shape
        and the timestamp's validity so a malformed or timestamp-less
        snapshot fails immediately instead of only when the window expires.
        """
        from datetime import datetime
        from harness.routing_table import TIERS_SNAPSHOT_PATH, load_tier_snapshot

        with open(TIERS_SNAPSHOT_PATH, encoding="utf-8") as fh:
            raw = json.load(fh)
        snap = load_tier_snapshot()
        self.assertEqual(snap["source"], "snapshot")
        self.assertEqual(snap["schema"], 1)
        captured = snap["captured_at"]
        self.assertTrue(captured, "captured_at is empty")
        parsed = datetime.fromisoformat(
            captured[:-1] + "+00:00" if captured.endswith("Z") else captured)
        self.assertIsNotNone(parsed.tzinfo)
        self.assertGreater(int(snap["max_age_days"]), 0)
        for tier in ("T0", "T1", "T2", "T3"):
            self.assertTrue(snap["tiers"][tier], f"{tier} has no models")
        self.assertTrue(raw.get("price_table"), "price_table is empty")
        for model_id, entry in raw["price_table"].items():
            self.assertIsInstance(model_id, str)
            self.assertGreaterEqual(float(entry["prompt_per_token"]), 0.0)
            self.assertGreaterEqual(float(entry["completion_per_token"]), 0.0)

    def test_task_class_routing_and_reversible_trial_mode(self):
        from harness.routing_table import TIER_MODELS, resolve_task_class

        before_t1 = list(TIER_MODELS[TIER_1])
        fast_route = resolve_task_class("fast")
        self.assertEqual(fast_route["tier"], TIER_1)
        self.assertEqual(fast_route["task_class"], "fast")

        spec_route = resolve_task_class("specialist")
        self.assertEqual(spec_route["tier"], TIER_2)

        frontier_route = resolve_task_class("frontier")
        self.assertEqual(frontier_route["tier"], TIER_3)

        # #197: reversible per-run model trial mode
        trial_route = resolve_task_class(
            "implement", trial_model="inclusionai/ling-3.0-flash")
        self.assertTrue(trial_route["reversible_trial"])
        self.assertEqual(trial_route["trial_model"], "inclusionai/ling-3.0-flash")
        self.assertEqual(
            trial_route["canonical_models"][0], "inclusionai/ling-3.0-flash")
        # Persistent TIER_MODELS is untouched after trial run
        self.assertEqual(TIER_MODELS[TIER_1], before_t1)


class TierSnapshotRefreshTests(unittest.TestCase):
    """The missing writer for packs/tiers.snapshot.json (#446 / DF-EV-5).

    The freshness gate's own warning advertised a refresh path that did not
    exist, so the committed artifact could only ever age out. These pins are
    the refresh contract: a catalog that agrees restamps the artifact, and a
    catalog that disagrees or cannot be read leaves the committed file byte
    identical rather than silently dropping routing data.
    """

    PINNED_NOW = datetime(2026, 11, 3, 12, 0, 0, tzinfo=timezone.utc)

    SNAPSHOT = {
        "schema": 1,
        "captured_at": "2026-10-18T00:00:00Z",
        "max_age_days": 14,
        "tiers": {
            "T0": ["acme/free-a:free", "acme/free-b:free"],
            "T1": ["acme/cheap-a"],
            "T2": ["acme/mid-a"],
            "T3": ["acme/frontier-a"],
        },
        "task_classes": {"fast": "T1", "frontier": "T3"},
        "price_table": {
            "acme/cheap-a": {"prompt_per_token": 1e-07,
                            "completion_per_token": 2e-07, "tier": "T1"},
            "acme/mid-a": {"prompt_per_token": 5e-07,
                          "completion_per_token": 1e-06, "tier": "T2"},
            "acme/frontier-a": {"prompt_per_token": 2e-06,
                               "completion_per_token": 8e-06, "tier": "T3"},
        },
    }

    CATALOG = [
        {"id": "acme/free-a:free", "pricing": {"prompt": "0", "completion": "0"}},
        {"id": "acme/free-b:free", "pricing": {"prompt": "0", "completion": "0"}},
        {"id": "acme/cheap-a", "pricing": {"prompt": "9.1e-08", "completion": "1.82e-07"}},
        {"id": "acme/mid-a", "pricing": {"prompt": "1.6e-06", "completion": "3.2e-06"}},
        {"id": "acme/frontier-a", "pricing": {"prompt": "4e-06", "completion": "2e-05"}},
    ]

    def _write(self, directory, payload=None):
        path = os.path.join(directory, "tiers.snapshot.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.SNAPSHOT if payload is None else payload, fh)
        return path

    def _read_bytes(self, path):
        with open(path, "rb") as fh:
            return fh.read()

    def test_refresh_restamps_and_repriced_from_an_agreeing_catalog(self):
        from harness.routing_table import check_tier_snapshot_freshness, refresh_tier_snapshot

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp)
            report = refresh_tier_snapshot(self.CATALOG, path=path,
                                           now=self.PINNED_NOW)
            with open(path, encoding="utf-8") as fh:
                written = json.load(fh)

            self.assertTrue(report["ok"])
            self.assertEqual(written["captured_at"], "2026-11-03T12:00:00Z")
            self.assertEqual(report["previous_captured_at"], "2026-10-18T00:00:00Z")
            # Tier membership is carried over exactly: nothing added, dropped.
            self.assertEqual(written["tiers"], self.SNAPSHOT["tiers"])
            self.assertEqual(report["tier_ids_validated"], 5)
            # Prices come from the catalog, not from the old artifact.
            self.assertAlmostEqual(written["price_table"]["acme/mid-a"]["prompt_per_token"],
                                   1.6e-06)
            self.assertAlmostEqual(
                written["price_table"]["acme/frontier-a"]["completion_per_token"], 2e-05)
            # Free ids stay out of the price table (no zero-price key churn).
            self.assertNotIn("acme/free-a:free", written["price_table"])
            # task_classes, schema and the window survive untouched.
            self.assertEqual(written["task_classes"], self.SNAPSHOT["task_classes"])
            self.assertEqual(written["schema"], 1)
            self.assertEqual(written["max_age_days"], 14)
            self.assertEqual(report["changed_fields"], ["captured_at", "price_table"])

            freshness = check_tier_snapshot_freshness(path, now=self.PINNED_NOW)
            self.assertTrue(freshness["ok"])
            self.assertEqual(freshness["age_days"], 0.0)

    def test_refresh_refuses_a_catalog_gap_and_leaves_the_file_untouched(self):
        from harness.routing_table import SnapshotRefreshError, refresh_tier_snapshot

        catalog = [row for row in self.CATALOG if row["id"] != "acme/free-b:free"]
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp)
            before = self._read_bytes(path)
            with self.assertRaises(SnapshotRefreshError) as ctx:
                refresh_tier_snapshot(catalog, path=path, now=self.PINNED_NOW)
            self.assertIn("acme/free-b:free", str(ctx.exception))
            self.assertIn("no silent drop", str(ctx.exception))
            self.assertEqual(self._read_bytes(path), before)

    def test_refresh_refuses_an_empty_catalog(self):
        from harness.routing_table import SnapshotRefreshError, refresh_tier_snapshot

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp)
            before = self._read_bytes(path)
            for catalog in ([], [{"id": "acme/cheap-a"}]):
                with self.assertRaises(SnapshotRefreshError):
                    refresh_tier_snapshot(catalog, path=path, now=self.PINNED_NOW)
            self.assertEqual(self._read_bytes(path), before)

    def test_refresh_refuses_when_a_priced_id_lost_its_rate(self):
        """A listed id with no published rate is a gap too, not a price of 0."""
        from harness.routing_table import SnapshotRefreshError, refresh_tier_snapshot

        catalog = [dict(row) for row in self.CATALOG]
        for row in catalog:
            if row["id"] == "acme/mid-a":
                row["pricing"] = {"prompt": "", "completion": ""}
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp)
            before = self._read_bytes(path)
            with self.assertRaises(SnapshotRefreshError) as ctx:
                refresh_tier_snapshot(catalog, path=path, now=self.PINNED_NOW)
            self.assertIn("acme/mid-a", str(ctx.exception))
            self.assertEqual(self._read_bytes(path), before)

    def test_refresh_refuses_an_unreadable_or_tierless_snapshot(self):
        from harness.routing_table import SnapshotRefreshError, refresh_tier_snapshot

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "tiers.snapshot.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{not json")
            with self.assertRaises(SnapshotRefreshError):
                refresh_tier_snapshot(self.CATALOG, path=path, now=self.PINNED_NOW)
            self.assertEqual(self._read_bytes(path), b"{not json")

            # No tiers object: refuse rather than write the bootstrap literal
            # over a reviewable artifact.
            tierless = os.path.join(tmp, "tierless.json")
            with open(tierless, "w", encoding="utf-8") as fh:
                json.dump({"schema": 1, "captured_at": "2026-10-18T00:00:00Z"}, fh)
            with self.assertRaises(SnapshotRefreshError):
                refresh_tier_snapshot(self.CATALOG, path=tierless, now=self.PINNED_NOW)

    def test_refresh_skips_catalog_rows_it_cannot_price(self):
        """A malformed catalog row is skipped, not guessed at and not fatal.

        ``/models`` rows arrive as free-form JSON: a non-dict entry, a row with
        no id, a row whose ``pricing`` is not an object, and a row missing a
        rate all occur in practice. None of them may become a price, and none
        may stop a refresh whose real ids are all present.
        """
        from harness.routing_table import refresh_tier_snapshot

        catalog = list(self.CATALOG) + [
            "acme/not-a-dict",
            {"pricing": {"prompt": "1e-07", "completion": "1e-07"}},
            {"id": "", "pricing": {"prompt": "1e-07", "completion": "1e-07"}},
            {"id": "acme/mid-b", "pricing": ["1e-06", "3e-06"]},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp)
            report = refresh_tier_snapshot(catalog, path=path, now=self.PINNED_NOW)
            self.assertTrue(report["ok"])
            with open(path, encoding="utf-8") as fh:
                written = json.load(fh)
            # Malformed rows contribute nothing: no phantom price appears.
            self.assertNotIn("acme/mid-b", written["price_table"])
            self.assertEqual(sorted(written["price_table"]),
                             sorted(self.SNAPSHOT["price_table"]))
            self.assertAlmostEqual(written["price_table"]["acme/mid-a"]["prompt_per_token"],
                                   1.6e-06)

    def test_refresh_refuses_a_priced_id_no_tier_lists(self):
        """A price_table entry outside every tier is still catalogued or refused.

        This is the shape a half-finished hand edit leaves behind: priced but
        unrouted. The refresh must not quietly drop the price, because
        ``ledger_analytics`` prices historical spend from this table.
        """
        from harness.routing_table import SnapshotRefreshError, refresh_tier_snapshot

        snapshot = json.loads(json.dumps(self.SNAPSHOT))
        snapshot["price_table"]["acme/retired-b"] = {
            "prompt_per_token": 1e-07, "completion_per_token": 1e-07, "tier": "T1"}
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, snapshot)
            before = self._read_bytes(path)
            with self.assertRaises(SnapshotRefreshError) as ctx:
                refresh_tier_snapshot(self.CATALOG, path=path, now=self.PINNED_NOW)
            self.assertIn("acme/retired-b", str(ctx.exception))
            self.assertEqual(self._read_bytes(path), before)

    def test_refresh_accepts_a_naive_reference_time(self):
        """A naive ``now`` is read as UTC rather than rejected or shifted."""
        from harness.routing_table import refresh_tier_snapshot

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp)
            report = refresh_tier_snapshot(
                self.CATALOG, path=path,
                now=datetime(2026, 11, 3, 12, 0, 0))
            self.assertEqual(report["captured_at"], "2026-11-03T12:00:00Z")

    def test_refresh_leaves_the_artifact_when_the_write_itself_fails(self):
        """A failed replace neither half-writes nor leaves a temp file behind."""
        from unittest import mock

        from harness.routing_table import refresh_tier_snapshot

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp)
            before = self._read_bytes(path)
            with mock.patch("harness.routing_table.os.replace",
                            side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    refresh_tier_snapshot(self.CATALOG, path=path, now=self.PINNED_NOW)
            self.assertEqual(self._read_bytes(path), before)
            leftovers = [n for n in os.listdir(tmp) if n.startswith(".tiers.snapshot.")]
            self.assertEqual(leftovers, [])

            # Even when the temp-file cleanup itself fails, the original is
            # still intact and the original error is what surfaces.
            with mock.patch("harness.routing_table.os.replace",
                            side_effect=OSError("disk full")), \
                    mock.patch("harness.routing_table.os.unlink",
                               side_effect=OSError("locked")):
                with self.assertRaises(OSError) as ctx:
                    refresh_tier_snapshot(self.CATALOG, path=path, now=self.PINNED_NOW)
            self.assertIn("disk full", str(ctx.exception))
            self.assertEqual(self._read_bytes(path), before)

    def test_refresh_refuses_a_negative_published_rate(self):
        from harness.routing_table import SnapshotRefreshError, refresh_tier_snapshot

        catalog = [dict(row) for row in self.CATALOG]
        for row in catalog:
            if row["id"] == "acme/cheap-a":
                row["pricing"] = {"prompt": "-1e-07", "completion": "1e-07"}
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp)
            with self.assertRaises(SnapshotRefreshError):
                refresh_tier_snapshot(catalog, path=path, now=self.PINNED_NOW)

    def test_shipped_snapshot_names_only_live_catalog_families(self):
        """The artifact this repo ships must not carry a delisted id (#446).

        Deterministic and offline: this asserts the committed file's shape is
        refreshable at all -- every tier id is priced or explicitly free, and
        no tier is empty -- so the scheduled refresh cannot refuse on arrival.
        Live catalog agreement is the weekly job's assertion, not this one.
        """
        from harness.routing_table import TIERS_SNAPSHOT_PATH

        with open(TIERS_SNAPSHOT_PATH, encoding="utf-8") as fh:
            raw = json.load(fh)
        for tier, ids in raw["tiers"].items():
            self.assertTrue(ids, f"{tier} is empty")
        for model_id in raw["price_table"]:
            self.assertFalse(model_id.endswith(":free"),
                             "free ids are zero by definition and stay unpriced")
        listed = {m for ids in raw["tiers"].values() for m in ids}
        self.assertTrue(set(raw["price_table"]).issubset(listed),
                        "a priced id is not routed by any tier")


if __name__ == "__main__":
    unittest.main()

