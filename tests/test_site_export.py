"""Hermetic gate tests for SITE-1: the Proof Bench site exporter.

The exporter is the boundary between a private evidence ledger and the
public site. These tests pin the fail-closed contract: no consent, no
export; broken chain, no export; credential-shaped content anywhere in the
output, no export. They also pin the v1/v2 escalation-event tolerance that
keeps this slice coalescing-safe with the Jev-directed escalation work.
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness.errors import HarnessError
from harness.ledger import AutonomyLedger
from harness.site_export import (
    BUNDLE_SCHEMA,
    CONSENT_SCHEMA,
    MAX_BUNDLE_BYTES,
    build_runs,
    export_bundle,
    load_consent,
    load_pricing,
    verify_chain,
    write_bundle,
)


def _write_consent(dirpath, **overrides):
    consent = {
        "schema": CONSENT_SCHEMA,
        "accepted_at": "2026-09-21T12:00:00Z",
        "surface": "site_optin_page",
    }
    consent.update(overrides)
    path = os.path.join(dirpath, "consent.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(consent, f)
    return path


def _write_pricing(dirpath):
    path = os.path.join(dirpath, "pricing.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "test/free-model:free": {"input_per_mtok": 0.0, "output_per_mtok": 0.0},
            "test/paid-flash": {"input_per_mtok": 0.10, "output_per_mtok": 0.40},
        }, f)
    return path


class SiteExportTestBase(unittest.TestCase):
    """Shared: a real (hash-chained) ledger built through the ONE owner."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="site_export_test_")
        self.ledger_path = os.path.join(self.tmp, "ledger.jsonl")
        self.ledger = AutonomyLedger(self.ledger_path)

    def tearDown(self):
        for name in os.listdir(self.tmp):
            try:
                os.unlink(os.path.join(self.tmp, name))
            except OSError:
                pass
        try:
            os.rmdir(self.tmp)
        except OSError:
            pass


class ConsentTests(SiteExportTestBase):
    def test_missing_file_refused(self):
        with self.assertRaises(HarnessError):
            load_consent(os.path.join(self.tmp, "absent.json"))

    def test_wrong_schema_refused(self):
        path = _write_consent(self.tmp, schema="site-consent-v0")
        with self.assertRaises(HarnessError):
            load_consent(path)

    def test_missing_timestamp_refused(self):
        path = _write_consent(self.tmp, accepted_at="")
        with self.assertRaises(HarnessError):
            load_consent(path)

    def test_valid_consent_loads(self):
        path = _write_consent(self.tmp)
        consent = load_consent(path)
        self.assertEqual(consent["schema"], CONSENT_SCHEMA)

    def test_bom_tolerant(self):
        path = os.path.join(self.tmp, "consent_bom.json")
        with open(path, "w", encoding="utf-8-sig") as f:
            json.dump({"schema": CONSENT_SCHEMA,
                       "accepted_at": "2026-09-21T12:00:00Z",
                       "surface": "x"}, f)
        self.assertEqual(load_consent(path)["surface"], "x")


class PricingTests(SiteExportTestBase):
    def test_absent_file_refused(self):
        with self.assertRaises(HarnessError):
            load_pricing(os.path.join(self.tmp, "absent.json"))

    def test_no_usable_rows_refused(self):
        path = os.path.join(self.tmp, "p.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"model/x": {"input_per_mtok": "nope"}}, f)
        with self.assertRaises(HarnessError):
            load_pricing(path)

    def test_rows_normalized(self):
        path = _write_pricing(self.tmp)
        pricing = load_pricing(path)
        self.assertEqual(pricing["test/paid-flash"]["output_per_mtok"], 0.40)


class ChainTests(SiteExportTestBase):
    def test_clean_chain_verifies(self):
        self.ledger.append("dispatch_start", task_id="t1", model="m")
        chain = verify_chain(self.ledger_path)
        self.assertTrue(chain["verified_claim"])
        self.assertEqual(chain["entries"], 1)

    def test_tampered_entry_refused(self):
        """A rewritten tail fails prev_hash linkage: entry[0]'s stored hash
        is chained into entry[1]'s prev_hash, so a naive re-hash of one
        entry does NOT restore validity (that is the chain's whole point)."""
        self.ledger.append("dispatch_start", task_id="t1", model="m")
        self.ledger.append("dispatch_start", task_id="t2", model="m")
        with open(self.ledger_path, encoding="utf-8") as f:
            lines = f.readlines()
        entry = json.loads(lines[0])
        entry["model"] = "tampered"
        body = {k: v for k, v in entry.items() if k != "hash"}
        import hashlib
        entry["hash"] = hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":"))
            .encode("utf-8")).hexdigest()
        lines[0] = json.dumps(entry) + "\n"
        with open(self.ledger_path, "w", encoding="utf-8") as f:
            f.writelines(lines)
        with self.assertRaises(HarnessError):
            verify_chain(self.ledger_path)

    def test_empty_ledger_refused(self):
        # A ledger file with zero entries has nothing to export.
        AutonomyLedger(self.ledger_path)  # creates an empty file
        with self.assertRaises(HarnessError):
            verify_chain(self.ledger_path)


def _populate_task_ledger(ledger, *, v2_escalation=False):
    """One full run: dispatch -> verify fails -> verify passes -> complete.

    With ``v2_escalation`` the escalate event carries the additive fields
    the Jev-directed escalation work is expected to add.
    """
    ledger.append("dispatch_start", task_id="task/a", model="test/free-model:free")
    ledger.append("model_result", task_id="task/a", model="test/free-model:free",
                  cost=0.0002, status="ok")
    ledger.append("verify_round", task_id="task/a", round=1,
                  passed=False, model="test/free-model:free", readiness="confident")
    ledger.append("verify_round", task_id="task/a", round=2,
                  passed=True, model="test/free-model:free", readiness="confident")
    esc_fields = {"from_model": "test/free-model:free", "to_model": "test/paid-flash"}
    if v2_escalation:
        esc_fields.update({
            "directed_by": "jev", "jev_confidence": 0.41, "target_rung": 1,
            "condensed_context_chars": 1840,
        })
    ledger.append("escalate", task_id="task/a", **esc_fields)
    ledger.append("model_result", task_id="task/a", model="test/paid-flash",
                  cost=0.0031, status="ok",
                  event_note="escalation_rung_1")
    ledger.append("verify_round", task_id="task/a", round=3,
                  passed=True, model="test/paid-flash", readiness="confident")
    ledger.append("complete", task_id="task/a", model="test/paid-flash",
                  rounds=3, status="ok")
    ledger.append("jev_eval", task_id="task/a", site="apply",
                  model="jev-latest", verdict="pass", supported=0.6,
                  confidence=0.41, input_tokens=200, output_tokens=0,
                  cost=0.0000084, is_fallback=False)
    ledger.append("spend_check", task_id="task/a", spent=0.0034)
    ledger.append("consent_accept", task_id="task/a")  # NOT in the allowlist


class BuildRunsTests(SiteExportTestBase):
    def test_full_run_shape(self):
        _populate_task_ledger(self.ledger, v2_escalation=True)
        runs = build_runs(self.ledger.entries())
        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertEqual(run["outcome"], "pass")
        self.assertTrue(run["gated"])
        self.assertEqual(run["rounds"], 3)
        self.assertAlmostEqual(run["cost"], 0.0033, places=9)
        self.assertEqual(run["jev_evals"]["count"], 1)
        self.assertAlmostEqual(run["jev_evals"]["cost"], 0.0000084, places=12)
        # The run dispatched on a free model but the escalation walk reached
        # the paid rung: deepest_tier_reached records how far the hourglass
        # actually ran (that is the run-depth story the site tells).
        self.assertEqual(run["entry_tier"], "T0")            # entered on free
        self.assertEqual(run["tier"], "T2")                 # completed on paid rung
        self.assertEqual(run["deepest_tier_reached"], "T2")  # hourglass ran this far
        esc = run["escalation"]
        self.assertEqual(esc["directed_by"], "jev")
        self.assertAlmostEqual(esc["jev_confidence"], 0.41, places=6)
        self.assertEqual(esc["target_rung"], 1)
        self.assertEqual(esc["condensed_context_chars"], 1840)
        warrant = esc["warrant"]
        self.assertEqual(warrant["lower_rung_rounds"], 3)
        self.assertEqual(warrant["lower_rung_verify_failures"], 1)

    def test_v1_events_get_honest_defaults(self):
        """v1 ledgers (no additive fields) must render, not crash."""
        _populate_task_ledger(self.ledger, v2_escalation=False)
        runs = build_runs(self.ledger.entries())
        esc = runs[0]["escalation"]
        self.assertEqual(esc["directed_by"], "verify_lane")
        self.assertIsNone(esc["jev_confidence"])
        self.assertIsNone(esc["target_rung"])
        self.assertIsNone(esc["condensed_context_chars"])

    def test_ungated_run_flagged(self):
        self.ledger.append("dispatch_start", task_id="task/ng", model="m")
        self.ledger.append("complete", task_id="task/ng", model="m",
                           rounds=1, status="ok", note="no verification gate")
        runs = build_runs(self.ledger.entries())
        self.assertFalse(runs[0]["gated"])
        self.assertEqual(runs[0]["outcome"], "pass")

    def test_abort_and_defer_outcomes(self):
        self.ledger.append("dispatch_start", task_id="task/ab", model="m")
        self.ledger.append("abort", task_id="task/ab", reason="verify rounds exhausted")
        self.ledger.append("dispatch_start", task_id="task/df", model="m")
        self.ledger.append("defer_midtask", task_id="task/df", category="readiness")
        runs = {r["run_id"]: r for r in build_runs(self.ledger.entries())}
        by_outcome = {}
        for run in runs.values():
            by_outcome.setdefault(run["outcome"], 0)
            by_outcome[run["outcome"]] += 1
        self.assertEqual(by_outcome.get("aborted"), 1)
        self.assertEqual(by_outcome.get("deferred"), 1)

    def test_no_free_text_in_output(self):
        """The sanitizer's core promise: no free-text field is ever copied."""
        _populate_task_ledger(self.ledger, v2_escalation=True)
        self.ledger.append("verify_round", task_id="task/a", round=4,
                           passed=False, model="x",
                           # hostile-looking fields that must NOT propagate:
                           output="C:\\Users\\secret\\project\\main.py: NameError",
                           error="stack trace with C:\\Users\\secret paths",
                           caller="mcp:peer/9.9")
        runs = build_runs(self.ledger.entries())
        blob = json.dumps(runs)
        for forbidden in ("secret", "NameError", "stack trace", "mcp:peer",
                          "task/a", "C:\\\\Users"):
            self.assertNotIn(forbidden, blob,
                             f"leaked free-text/identity: {forbidden!r}")
        # Task identity is only ever a truncated hash.
        import hashlib
        expected = hashlib.sha256(b"task:task/a").hexdigest()[:16]
        self.assertEqual(runs[0]["task_ref"], expected)


class ExportTests(SiteExportTestBase):
    def test_refuses_without_consent(self):
        _populate_task_ledger(self.ledger)
        with self.assertRaises(HarnessError):
            export_bundle(self.ledger_path,
                          os.path.join(self.tmp, "absent_consent.json"))

    def test_full_export_shape(self):
        _populate_task_ledger(self.ledger, v2_escalation=True)
        consent = _write_consent(self.tmp)
        bundle = export_bundle(self.ledger_path, consent,
                               pricing_path=_write_pricing(self.tmp),
                               harness_version="0.3.3")
        self.assertEqual(bundle["schema"], BUNDLE_SCHEMA)
        self.assertEqual(len(bundle["bundle_id"]), 16)
        self.assertEqual(bundle["totals"]["runs"], 1)
        self.assertEqual(bundle["totals"]["gated_runs"], 1)
        self.assertEqual(bundle["totals"]["escalations"], 1)
        self.assertEqual(bundle["chain"]["entries"], len(self.ledger.entries()))
        self.assertIn("test/paid-flash", bundle["pricing_snapshot"])
        # Bundle identity is stable under re-export (same ledger + consent
        # + pricing => same core => same dedupe key).
        pricing = _write_pricing(self.tmp)
        again = export_bundle(self.ledger_path, consent, pricing_path=pricing,
                              harness_version="0.3.3")
        self.assertEqual(bundle["bundle_id"], again["bundle_id"])
        # ...and a different consent record CHANGES identity: the consent
        # block is bound into the canonical hash, so a tampered/swapped
        # consent can never ride a previously-seen bundle_id past the
        # site's dedupe check.
        consent2 = _write_consent(self.tmp, accepted_at="2026-09-22T00:00:00Z")
        third = export_bundle(self.ledger_path, consent2, pricing_path=pricing,
                              harness_version="0.3.3")
        self.assertNotEqual(bundle["bundle_id"], third["bundle_id"])

    def test_bundle_id_changes_when_core_changes(self):
        _populate_task_ledger(self.ledger)
        consent = _write_consent(self.tmp)
        first = export_bundle(self.ledger_path, consent)
        self.ledger.append("dispatch_start", task_id="task/b", model="m")
        second = export_bundle(self.ledger_path, consent)
        self.assertNotEqual(first["bundle_id"], second["bundle_id"])

    def test_no_exportable_events_refused(self):
        consent = _write_consent(self.tmp)
        self.ledger.append("consent_accept", task_id="x")  # allowlist excludes it
        with self.assertRaises(HarnessError):
            export_bundle(self.ledger_path, consent)


class WriteTests(SiteExportTestBase):
    def _bundle(self):
        _populate_task_ledger(self.ledger, v2_escalation=True)
        consent = _write_consent(self.tmp)
        return export_bundle(self.ledger_path, consent,
                             pricing_path=_write_pricing(self.tmp))

    def test_write_and_secret_scan_clean(self):
        out = os.path.join(self.tmp, "bundle.json")
        size = write_bundle(self._bundle(), out)
        with open(out, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["schema"], BUNDLE_SCHEMA)
        self.assertGreater(size, 0)

    def test_secret_scan_refuses(self):
        bundle = self._bundle()
        bundle["capabilities"].append({
            "model": "sk-or-v1-0123456789abcdef0123",
            "observed_success": None, "samples": 0, "gate_wasted": 0,
            "calls": 0})
        out = os.path.join(self.tmp, "poisoned.json")
        with self.assertRaises(HarnessError):
            write_bundle(bundle, out)
        self.assertFalse(os.path.exists(out))
        self.assertFalse(os.path.exists(out + ".tmp"))

    def test_truncation_marks_bundle(self):
        """Over-cap bundles drop oldest runs and record the truncation."""
        bundle = self._bundle()
        padding = {"model": "pad", "observed_success": None, "samples": 0,
                   "gate_wasted": 0, "calls": 0,
                   "note": "x" * (MAX_BUNDLE_BYTES // 2)}
        bundle["capabilities"] = [padding]
        bundle["runs"] = [dict(r) for r in bundle["runs"]] + [
            {"run_id": f"old{i:04d}", "task_ref": f"t{i}", "lane": "task",
             "ts": "2026-01-01T00:00:00Z", "primary_model": "m", "tier": "T0",
             "rounds": 1, "tokens_in": None, "tokens_out": None, "cost": 0.0,
             "outcome": "pass", "gated": False, "deepest_tier_reached": None,
             "jev_evals": {"count": 0, "cost": 0.0, "fallback": 0}}
            for i in range(2000)]
        bundle["totals"] = {"runs": len(bundle["runs"]), "gated_runs": 0,
                            "completed": 0, "escalations": 0, "total_cost": 0.0}
        out = os.path.join(self.tmp, "big.json")
        write_bundle(bundle, out)
        with open(out, encoding="utf-8") as f:
            data = json.load(f)
        self.assertTrue(data["truncated"])
        self.assertLess(len(data["runs"]), 2000)
        self.assertEqual(len(data["runs"]), data["totals"]["runs"])


if __name__ == "__main__":
    unittest.main()
