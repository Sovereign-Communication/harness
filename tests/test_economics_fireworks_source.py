"""EV-6: the Fireworks price pack is derived, checked, and read without network.

The snapshot (audits/self/dogfood/fireworks-price-snapshot.md) is the source
of truth; the generator writes packs/fireworks.endpoints.json from it and
the loader reads only that pack. These tests pin that chain: the committed
pack must equal what the generator produces, drift must fail, only
live-confirmed Standard rows may enter, a row is routable only with a
confirmed model path, and nothing here may open a socket.

All hermetic. The generator module is loaded from its file path because
audits/self is not a package.
"""
import importlib.util
import io
import json
import socket
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from harness.endpoint_pricing import (FIREWORKS_PACK, FIREWORKS_PROVIDER,
                                      fireworks_offers)
from harness.errors import HarnessError

ROOT = Path(__file__).resolve().parent.parent
GEN_PATH = ROOT / "audits" / "self" / "refresh_fireworks_pack.py"
_spec = importlib.util.spec_from_file_location("refresh_fireworks_pack", GEN_PATH)
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)

SNAPSHOT_TEXT = gen.SNAPSHOT.read_text(encoding="utf-8")

_HEADER = ("# Fireworks serverless price snapshot\n\n"
           "**Captured:** 2026-10-08, synthetic\n\n## Models\n\n"
           "| Model | Standard in / cached / out | Status | Model path | Priority |\n"
           "|---|---|---|---|---|\n")


def _snapshot(*rows):
    return _HEADER + "\n".join(rows) + "\n"


class PackParityTests(unittest.TestCase):
    def test_committed_pack_is_exactly_what_the_generator_writes(self):
        committed = FIREWORKS_PACK.read_text(encoding="utf-8")
        self.assertEqual(committed, gen.render(gen.build_pack(SNAPSHOT_TEXT)))

    def test_check_mode_passes_on_the_committed_pack(self):
        out = io.StringIO()
        with redirect_stderr(out):
            self.assertEqual(gen.main(["--check"]), 0)

    def test_pack_is_freshly_dated_from_the_snapshot_it_names(self):
        pack = json.loads(FIREWORKS_PACK.read_text(encoding="utf-8"))
        self.assertEqual(pack["source"], gen.SNAPSHOT_REL)
        self.assertEqual(pack["captured"], "2026-10-08")
        self.assertEqual(pack["unit"], "usd_per_1m_tokens")


class DriftTests(unittest.TestCase):
    def test_editing_a_snapshot_price_fails_the_check(self):
        drifted = SNAPSHOT_TEXT.replace("| 0.05 / 0.01 / 0.20 |",
                                        "| 0.06 / 0.01 / 0.20 |", 1)
        self.assertNotEqual(drifted, SNAPSHOT_TEXT)
        with tempfile.TemporaryDirectory() as tmp:
            snap = Path(tmp) / "snap.md"
            snap.write_text(drifted, encoding="utf-8")
            with mock.patch.object(gen, "SNAPSHOT", snap), \
                    redirect_stderr(io.StringIO()) as err:
                rc = gen.main(["--check"])
        self.assertEqual(rc, 1)
        self.assertIn("out of date", err.getvalue())

    def test_missing_pack_fails_the_check(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(gen, "PACK", Path(tmp) / "absent.json"), \
                redirect_stderr(io.StringIO()):
            self.assertEqual(gen.main(["--check"]), 1)

    def test_snapshot_without_a_capture_date_is_refused(self):
        with self.assertRaises(ValueError):
            gen.build_pack("## Models\n")


class RowFilterTests(unittest.TestCase):
    def test_only_live_confirmed_standard_rows_enter_the_pack(self):
        text = _snapshot(
            "| Live model | 1.00 / 0.10 / 2.00 | live-confirmed | unconfirmed: `live` | — |",
            "| Stale model | 9.00 / 0.90 / 9.00 | unverified | unconfirmed | — |",
            "| Reranker | 0.20 per 1M tokens | live-confirmed | unconfirmed: `rr` | — |",
        )
        models = gen.build_pack(text)["models"]
        self.assertEqual([m["name"] for m in models], ["Live model"])
        self.assertEqual((models[0]["input"], models[0]["cached_input"],
                          models[0]["output"]), (1.0, 0.1, 2.0))

    def test_unconfirmed_path_is_not_routable(self):
        text = _snapshot(
            "| Live model | 1.00 / 0.10 / 2.00 | live-confirmed | unconfirmed: `live` | — |")
        self.assertIsNone(gen.build_pack(text)["models"][0]["path"])

    def test_confirmed_path_is_built_from_the_slug(self):
        text = _snapshot(
            "| Live model | 1.00 / 0.10 / 2.00 | live-confirmed | confirmed: `live-slug` | — |")
        self.assertEqual(gen.build_pack(text)["models"][0]["path"],
                         "accounts/fireworks/models/live-slug")


class LoaderTests(unittest.TestCase):
    def setUp(self):
        self.offers = fireworks_offers()
        self.by_name = {o.model: o for o in self.offers}

    def test_every_pack_row_is_loaded_as_a_fireworks_endpoint_price(self):
        pack = json.loads(FIREWORKS_PACK.read_text(encoding="utf-8"))
        self.assertEqual(len(self.offers), len(pack["models"]))
        for offer in self.offers:
            self.assertEqual(offer.price.provider_name, FIREWORKS_PROVIDER)

    def test_rates_are_converted_from_per_million_to_per_token(self):
        nemotron = self.by_name["Nemotron Lightning 3.5 30B A3B"]
        self.assertAlmostEqual(nemotron.price.prompt, 0.05 / 1_000_000)
        self.assertAlmostEqual(nemotron.price.input_cache_read, 0.01 / 1_000_000)
        self.assertAlmostEqual(nemotron.price.completion, 0.20 / 1_000_000)

    def test_only_the_confirmed_model_is_routable(self):
        routable = [o for o in self.offers if o.routable]
        self.assertEqual([o.model for o in routable],
                         ["Nemotron Lightning 3.5 30B A3B"])
        self.assertEqual(routable[0].path,
                         "accounts/fireworks/models/nemotron-lightning-3p5-30b-a3b")
        self.assertFalse(self.by_name["Ember-1"].routable)

    def test_missing_pack_raises_instead_of_reading_as_no_offers(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(HarnessError):
                fireworks_offers(Path(tmp) / "absent.json")

    def test_malformed_pack_raises_instead_of_reading_as_no_offers(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text('{"models": "not a list"}', encoding="utf-8")
            with self.assertRaises(HarnessError):
                fireworks_offers(bad)


class NoNetworkTests(unittest.TestCase):
    def test_loading_and_checking_open_no_socket(self):
        def refuse(*_a, **_k):
            raise AssertionError("EV-6 must not touch the network")

        with mock.patch.object(socket, "socket", side_effect=refuse), \
                mock.patch.object(socket, "create_connection", side_effect=refuse), \
                mock.patch.object(socket, "getaddrinfo", side_effect=refuse), \
                redirect_stderr(io.StringIO()):
            self.assertTrue(fireworks_offers())
            self.assertEqual(gen.main(["--check"]), 0)


if __name__ == "__main__":
    unittest.main()
