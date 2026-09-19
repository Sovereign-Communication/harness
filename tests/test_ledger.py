import json
import os
import tempfile
import unittest

from harness.ledger import AutonomyLedger


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "ledger.jsonl")
        self.ledger = AutonomyLedger(self.path)

    def tearDown(self):
        self.dir.cleanup()

    def test_chain_builds_and_verifies(self):
        for i in range(5):
            self.ledger.append("offer", task_id=f"t{i}", model="m1", required=True)
        self.assertEqual(len(self.ledger.entries()), 5)
        ok, bad = self.ledger.verify()
        self.assertTrue(ok)
        self.assertIsNone(bad)

    def test_tamper_detected(self):
        self.ledger.append("offer", task_id="t1", model="m1")
        self.ledger.append("consent_accept", task_id="t1", model="m1", reason="sure")
        # Tamper with the on-disk record: rewrite entry 2's reason.
        with open(self.path, encoding="utf-8") as stream:
            lines = stream.read().splitlines()
        entry = json.loads(lines[1])
        entry["reason"] = "FORGED"
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("\n".join(json.dumps(e, sort_keys=True) for e in
                              [json.loads(lines[0]), entry]) + "\n")
        reloaded = AutonomyLedger(self.path)
        ok, bad = reloaded.verify()
        self.assertFalse(ok)
        self.assertEqual(bad, 2)

    def _other_writer_append(self, event, task_id="other"):
        """Simulate a second harness process appending to the same file from
        its own (identical) chain state -- the append lock serializes the
        write but not the chain state of the two writers."""
        import hashlib
        from harness.ledger import _canon
        tail = self.ledger.entries()
        entry = {
            "seq": (tail[-1]["seq"] if tail else 0) + 1,
            "ts": "2026-09-06T19:14:06+00:00",
            "event": event,
            "task_id": task_id,
            "prev_hash": tail[-1]["hash"] if tail else None,
        }
        entry["hash"] = hashlib.sha256(_canon(entry).encode("utf-8")).hexdigest()
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(_canon(entry) + "\n")

    def test_append_rebases_after_concurrent_write(self):
        """Regression (live): two processes that loaded the ledger together
        both appended from the same prev_hash and forked the chain with
        duplicate seqs. append() must re-read the file under the lock and
        continue the OTHER writer's chain."""
        self.ledger.append("dispatch_start", task_id="t1", model="m1")
        self.ledger.append("model_result", task_id="t1", model="m1")
        self._other_writer_append("dispatch_start")
        self.ledger.append("complete", task_id="t1", model="m1")
        seqs = [e["seq"] for e in self.ledger.entries()]
        self.assertEqual(seqs, [1, 2, 3, 4])
        reloaded = AutonomyLedger(self.path)
        ok, bad = reloaded.verify()
        self.assertTrue(ok)
        self.assertIsNone(bad)

    def test_repair_truncates_forked_tail(self):
        """A forked/duplicate tail (crash or append race) is duplicate
        evidence: repair must truncate to the longest valid prefix and leave
        a chain that verifies."""
        for i in range(3):
            self.ledger.append("offer", task_id=f"t{i}", model="m1")
        # Fork: a stale second writer appends from seq 2's state.
        tail = self.ledger.entries()
        import hashlib
        from harness.ledger import _canon
        entry = {"seq": 4, "ts": "2026-09-06T19:14:06+00:00",
                 "event": "abort", "task_id": "fork",
                 "prev_hash": tail[1]["hash"]}
        entry["hash"] = hashlib.sha256(_canon(entry).encode("utf-8")).hexdigest()
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(_canon(entry) + "\n")
        reloaded = AutonomyLedger(self.path)
        ok, bad = reloaded.verify()
        self.assertFalse(ok)
        kept, dropped = reloaded.repair()
        self.assertEqual(kept, 3)
        self.assertGreaterEqual(dropped, 1)
        healed = AutonomyLedger(self.path)
        ok, bad = healed.verify()
        self.assertTrue(ok)
        self.assertIsNone(bad)

    def test_reload_resumes_chain(self):
        self.ledger.append("offer", task_id="t1", model="m1")
        self.ledger.append("consent_accept", task_id="t1", model="m1", reason="ok")
        again = AutonomyLedger(self.path)
        self.assertEqual(again._seq, 2)
        ok, bad = again.verify()
        self.assertTrue(ok)
        # appending on the reloaded handle stays chained
        again.append("complete", task_id="t1", model="m1", status="ok")
        ok, bad = AutonomyLedger(self.path).verify()
        self.assertTrue(ok)

    def test_participation_report_step_up_is_not_a_denial(self):
        # The trust step-up's own event is an escalation, not a refusal:
        # neither the host gate counts nor the per-model denial count may
        # treat it as a strike (that poisoned every rescue rung).
        led = self.ledger
        led.append("trust_gate", task_id="t1", model="rung",
                   reason="preview-band", severity="soft")
        led.append("trust_gate", task_id="t1", model="rung",
                   reason="primary stepped up: primary model trust denied",
                   severity="soft")
        r = led.participation_report()
        self.assertEqual(r["trust_gates"], 1)
        self.assertEqual(r["per_model"]["rung"]["trust_denials"], 1)

    def test_participation_report_refuse_band_soft_denies_are_not_evidence(self):
        # The deadlock breaker: a soft denial recorded AT the refuse band
        # is the lockout speaking (denied nodes complete nothing, so the
        # gate would otherwise feed itself strikes forever). Hostile
        # attempts and soft denies above the band stay evidence.
        led = self.ledger
        led.append("trust_gate", task_id="t1", model="m",
                   reason="refused", severity="soft", combined=-11)
        led.append("trust_gate", task_id="t1", model="m",
                   reason="over ceiling", severity="soft", combined=-2)
        led.append("trust_gate", task_id="t1", model="m",
                   reason="retarget", severity="hostile", combined=-11)
        r = led.participation_report()
        self.assertEqual(r["trust_gates"], 2)
        self.assertEqual(r["trust_hostile"], 1)
        # Per-model attribution skips the refuse-band soft deny too
        # (the over-ceiling soft deny and the hostile attempt still count).
        self.assertEqual(r["per_model"]["m"]["trust_denials"], 2)
        self.assertEqual(r["per_model"]["m"]["trust_hostile"], 1)

    def test_participation_report_counts(self):
        led = self.ledger
        led.append("offer", task_id="t1", model="judge", required=True)
        led.append("consent_accept", task_id="t1", model="judge", reason="ok")
        led.append("dispatch_start", task_id="t1", model="coder")
        led.append("verify_round", task_id="t1", round=1, passed=True, model="coder")
        led.append("complete", task_id="t1", model="coder", rounds=1, status="ok")
        led.append("offer", task_id="t2", model="judge", required=True)
        led.append("consent_decline", task_id="t2", model="judge", reason="no")
        led.append("offer", task_id="t3", model="judge", required=False)
        led.append("consent_defer", task_id="t3", model="judge", reason="later")
        r = led.participation_report()
        self.assertEqual(r["offers"], 3)
        self.assertEqual(r["accepts"], 1)
        self.assertEqual(r["declines"], 1)
        self.assertEqual(r["defers"], 1)
        self.assertEqual(r["completions"], 1)
        self.assertEqual(r["accept_rate"], round(1 / 3, 4))
        self.assertEqual(r["completion_rate"], 1.0)
        self.assertEqual(r["consent_required_offers"], 2)
        self.assertEqual(r["mean_verify_rounds"], 1.0)
        self.assertFalse(r["consent_looks_degenerate"])

    def test_calibration_tracks_readiness_vs_verify(self):
        """Join HARNESS_READY: confident verdicts with the same-round verify outcome."""
        led = self.ledger
        # coder_a: confident twice, both passed -> well-calibrated
        led.append("readiness", task_id="t1", model="coder_a", round=1, decision="confident")
        led.append("verify_round", task_id="t1", round=1, passed=True, model="coder_a",
                 readiness="confident")
        led.append("readiness", task_id="t2", model="coder_a", round=1, decision="confident")
        led.append("verify_round", task_id="t2", round=1, passed=True, model="coder_a",
                 readiness="confident")
        # coder_b: confident twice, both failed -> overconfident
        led.append("readiness", task_id="t3", model="coder_b", round=1, decision="confident")
        led.append("verify_round", task_id="t3", round=1, passed=False, model="coder_b",
                 readiness="confident")
        led.append("readiness", task_id="t4", model="coder_b", round=1, decision="confident")
        led.append("verify_round", task_id="t4", round=1, passed=False, model="coder_b",
                 readiness="confident")
        # coder_c: deferred every time -> no verify join, but defer counted
        led.append("readiness", task_id="t5", model="coder_c", round=1, decision="defer")
        r = led.participation_report()
        cal = r["calibration"]
        self.assertEqual(cal["coder_a"]["confidence_precision"], 1.0)
        self.assertEqual(cal["coder_b"]["confidence_precision"], 0.0)
        self.assertEqual(cal["coder_c"]["defer"], 1)
        self.assertIsNone(cal["coder_c"]["confidence_precision"])
        self.assertEqual(r["confidence_precision"], round(2 / 4, 3))
        self.assertIn("coder_b", r["underconfident_or_overconfident"])
        self.assertNotIn("coder_a", r["underconfident_or_overconfident"])

    def test_calibration_ignores_unmatched_readiness(self):
        """A confident verdict with no same-round verify outcome does not count as pass."""
        led = self.ledger
        led.append("readiness", task_id="t1", model="coder", round=1, decision="confident")
        # verify_round for a different round -> no join
        led.append("verify_round", task_id="t1", round=2, passed=False, model="coder",
                 readiness="confident")
        r = led.participation_report()
        cal = r["calibration"]["coder"]
        self.assertEqual(cal["confident"], 1)
        self.assertEqual(cal["confident_verified"], 0)
        self.assertIsNone(cal["confidence_precision"])

    def test_success_rate_and_model_result_samples(self):
        """success_rate aggregates verify outcomes; samples counts model_result events."""
        led = self.ledger
        led.append("verify_round", task_id="t1", round=1, passed=True, model="coder")
        led.append("verify_round", task_id="t1", round=2, passed=True, model="coder")
        led.append("verify_round", task_id="t2", round=1, passed=False, model="coder")
        led.append("verify_round", task_id="t3", round=1, passed=True, model="coder")
        led.append("model_result", task_id="t1", model="coder", task_type="code",
                 json_expected=False, json_ok=None, status="ok")
        led.append("model_result", task_id="t2", model="coder", task_type="code",
                 json_expected=False, json_ok=None, status="ok")
        led.append("model_result", task_id="t3", model="coder", task_type="code",
                 json_expected=False, json_ok=None, status="ok")
        r = led.participation_report()
        cal = r["calibration"]["coder"]
        self.assertEqual(cal["success_rate"], round(3 / 4, 3))
        self.assertEqual(cal["samples"], 3)

    def test_degenerate_consent_flagged(self):
        led = self.ledger
        for i in range(10):
            led.append("offer", task_id=f"t{i}", model="judge", required=True)
            led.append("consent_accept", task_id=f"t{i}", model="judge", reason="ok")
        r = led.participation_report()
        self.assertEqual(r["accept_rate"], 1.0)
        self.assertTrue(r["consent_looks_degenerate"])
        self.assertIn("theater", r["degenerate_note"])

    def test_gate_wasted_runs_counts_rounds_exhausted_aborts(self):
        """Gate-waste evidence (the v0.3.1 dogfood finding): the ledger
        counts rounds-exhausted aborts per LEADING model -- the same
        predicate the dogfood curator turns into its headline claim --
        so pool ordering can demote demonstrated gate-wasters."""
        led = self.ledger
        led.append("abort", task_id="t1", model="acme/waster:free",
                   reason="verify rounds exhausted", rotations=3)
        led.append("abort", task_id="t2", model="acme/waster:free",
                   reason="verify rounds exhausted", rotations=3)
        led.append("abort", task_id="t3", model="acme/waster:free",
                   reason="verify rounds exhausted", rotations=3)
        # Same event, different reason -- not gate waste.
        led.append("abort", task_id="t4", model="acme/waster:free",
                   reason="operator cancelled")
        # Same event, no model -- unattributable.
        led.append("abort", task_id="t5", reason="verify rounds exhausted")
        r = led.participation_report()
        self.assertEqual(r["calibration"]["acme/waster:free"]["gate_wasted_runs"], 3)
        self.assertNotIn("gate_wasted_runs", r["calibration"].get("acme/other:free", {}))


class LedgerCallerTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "ledger.jsonl")

    def tearDown(self):
        self.dir.cleanup()

    def test_caller_tagged_when_configured_untagged_by_default(self):
        led = AutonomyLedger(self.path)
        led.append("offer", task_id="t1", model="m")
        self.assertNotIn("caller", led.entries()[-1])
        led.caller = "mcp:probe/1.0"
        led.append("offer", task_id="t2", model="m")
        self.assertEqual(led.entries()[-1]["caller"], "mcp:probe/1.0")
        # An explicit caller on the event wins over the session default.
        led.append("offer", task_id="t3", model="m", caller="cli")
        self.assertEqual(led.entries()[-1]["caller"], "cli")
        ok, bad = AutonomyLedger(self.path).verify()
        self.assertTrue(ok)
        self.assertIsNone(bad)

    def test_participation_report_breaks_out_per_caller(self):
        led = AutonomyLedger(self.path)
        led.append("complete", task_id="t1", model="m", status="ok")
        led.caller = "mcp:probe/1.0"
        led.append("complete", task_id="t2", model="m", status="ok")
        led.append("trust_gate", task_id="t3", model="m",
                   reason="x", severity="hostile")
        r = led.participation_report()
        self.assertEqual(r["completions"], 2)
        self.assertEqual(r["per_caller"]["mcp:probe/1.0"]["completions"], 1)
        self.assertEqual(r["per_caller"]["mcp:probe/1.0"]["trust_hostile"], 1)
        self.assertNotIn("untagged", r["per_caller"])


class LedgerCorruptionTests(unittest.TestCase):
    def test_torn_trailing_line_quarantined_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ledger.jsonl")
            led = AutonomyLedger(path)
            led.append("offer", task_id="t1", model="m")
            with open(path, "a", encoding="utf-8") as f:
                f.write("{not json\n")
            reloaded = AutonomyLedger(path)
            # Load must not crash; the intact prefix stays usable.
            self.assertGreaterEqual(reloaded.quarantined, 1)
            self.assertTrue(reloaded.chain_broken)
            # A new append still works on the intact prefix.
            reloaded.append("complete", task_id="t1", model="m")
            n = reloaded._valid_prefix_len()
            self.assertEqual(n, len(reloaded.entries()))
            # verify fails closed until the torn line is repaired away.
            ok, _bad = reloaded.verify()
            self.assertFalse(ok)
            kept, dropped = reloaded.repair()
            self.assertGreaterEqual(dropped, 0)
            ok2, _bad2 = AutonomyLedger(path).verify()
            # After repair the on-disk chain is clean again.
            self.assertTrue(ok2)

    def test_shape_broken_line_quarantined(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "led.jsonl")
            with open(path, "w", encoding="utf-8") as f:
                f.write('"just a string"\n[1, 2]\n')
            led = AutonomyLedger(path)
            self.assertEqual(len(led.entries()), 0)
            self.assertEqual(led.quarantined, 2)


class LedgerSegmentAnchorTests(unittest.TestCase):
    """Rotation must leave a chained boundary record: a pruned prefix
    reports as an explicit cut, never as a complete genesis chain."""

    def _tiny_bytes(self, monkeypatch_keep=3):
        import harness.ledger as ledger_mod
        old_max, old_keep = (ledger_mod.LEDGER_MAX_BYTES,
                             ledger_mod.LEDGER_KEEP_ROTATIONS)
        ledger_mod.LEDGER_MAX_BYTES = 200
        ledger_mod.LEDGER_KEEP_ROTATIONS = monkeypatch_keep
        self.addCleanup(setattr, ledger_mod, "LEDGER_MAX_BYTES", old_max)
        self.addCleanup(setattr, ledger_mod, "LEDGER_KEEP_ROTATIONS", old_keep)

    def _anchor_of(self, path):
        import json
        with open(path, encoding="utf-8") as f:
            return json.loads(f.readline())

    def test_rotation_writes_chained_segment_anchor(self):
        import hashlib
        from harness.ledger import _canon
        self._tiny_bytes()
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "led.jsonl")
            led = AutonomyLedger(path)
            led.append("offer", task_id="t1", model="m")
            led.append("offer", task_id="t2", model="m")
            rotated = [p for p in led._rotated_paths()]
            self.assertEqual(len(rotated), 1)
            anchor = self._anchor_of(path)
            self.assertEqual(anchor["event"], "segment")
            self.assertEqual(anchor["seq"], 3)
            # The anchor chains onto the rotated tip, tip fields name it.
            tip = led.entries()[1]
            self.assertEqual(anchor["prev_hash"], tip["hash"])
            self.assertEqual(anchor["tip_seq"], 2)
            self.assertEqual(anchor["tip_hash"], tip["hash"])
            body = {k: v for k, v in anchor.items() if k != "hash"}
            self.assertEqual(
                hashlib.sha256(_canon(body).encode("utf-8")).hexdigest(),
                anchor["hash"])
            # A fresh loader sees one continuous chain through the cut.
            fresh = AutonomyLedger(path)
            ok, bad = fresh.verify()
            self.assertTrue(ok)
            self.assertIsNone(bad)

    def test_chain_status_reports_segments_and_cut(self):
        self._tiny_bytes()
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "led.jsonl")
            led = AutonomyLedger(path)
            led.append("offer", task_id="t1", model="m")
            led.append("offer", task_id="t2", model="m")
            st = AutonomyLedger(path).chain_status()
            self.assertTrue(st["ok"])
            self.assertTrue(st["segmented"])
            self.assertEqual(len(st["segments"]), 2)
            self.assertEqual(st["first_retained_seq"], 1)
            self.assertFalse(st["pruned"])
            self.assertTrue(st["segments"][-1]["opens_with_segment_event"])

    def test_pruned_prefix_reported_not_silent(self):
        self._tiny_bytes(monkeypatch_keep=1)
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "led.jsonl")
            led = AutonomyLedger(path)
            for i in range(6):
                led.append("offer", task_id=f"t{i}", model="m")
            fresh = AutonomyLedger(path)
            # Suffix rule preserved: retained links verify ...
            ok, bad = fresh.verify()
            self.assertTrue(ok)
            self.assertIsNone(bad)
            # ... but the cut is explicit, not a silent genesis.
            st = fresh.chain_status()
            self.assertTrue(st["segmented"])
            self.assertTrue(st["pruned"])
            self.assertGreater(st["first_retained_seq"], 1)

    def test_repair_on_healthy_pruned_ledger_is_noop(self):
        """A healthy pruned suffix needs no healing: repair must leave the
        segment files alone (collapsing them would destroy the boundary
        anchors that tell pruning from tampering)."""
        self._tiny_bytes(monkeypatch_keep=1)
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "led.jsonl")
            led = AutonomyLedger(path)
            for i in range(6):
                led.append("offer", task_id=f"t{i}", model="m")
            fresh = AutonomyLedger(path)
            self.assertGreater(fresh.entries()[0]["seq"], 1)
            import hashlib
            before = {}
            for p in [path] + fresh._rotated_paths():
                with open(p, "rb") as f:
                    before[p] = hashlib.sha256(f.read()).hexdigest()
            kept, dropped = fresh.repair()
            self.assertEqual(dropped, 0)
            for p, digest in before.items():
                with open(p, "rb") as f:
                    self.assertEqual(hashlib.sha256(f.read()).hexdigest(),
                                     digest)
            healed = AutonomyLedger(path)
            ok, bad = healed.verify()
            self.assertTrue(ok)
            self.assertIsNone(bad)
            st = healed.chain_status()
            self.assertTrue(st["segmented"])
            self.assertTrue(st["pruned"])
            self.assertGreater(st["first_retained_seq"], 1)

    def test_repair_preserves_healthy_segments_byte_identical(self):
        """Repair truncates only the cut file: older retained segments must
        survive byte-identical (they hold anchored evidence the repair must
        not rewrite away)."""
        import hashlib
        self._tiny_bytes()
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "led.jsonl")
            led = AutonomyLedger(path)
            led.append("offer", task_id="t1", model="m")
            led.append("offer", task_id="t2", model="m")
            rotated = [p for p in led._rotated_paths()]
            self.assertEqual(len(rotated), 1)
            with open(rotated[0], "rb") as f:
                before = f.read()
            before_hash = hashlib.sha256(before).hexdigest()
            # Fork the ACTIVE tail only (stale-writer duplicate).
            tail = led.entries()
            from harness.ledger import _canon
            fork = {"seq": tail[-1]["seq"] + 1, "ts": "2026-09-06T19:14:06+00:00",
                    "event": "abort", "task_id": "fork",
                    "prev_hash": tail[-2]["hash"]}
            fork["hash"] = hashlib.sha256(
                _canon(fork).encode("utf-8")).hexdigest()
            with open(path, "a", encoding="utf-8") as f:
                f.write(_canon(fork) + "\n")
            reloaded = AutonomyLedger(path)
            ok, _ = reloaded.verify()
            self.assertFalse(ok)
            kept, dropped = reloaded.repair()
            self.assertGreaterEqual(dropped, 1)
            with open(rotated[0], "rb") as f:
                self.assertEqual(hashlib.sha256(f.read()).hexdigest(),
                                 before_hash)
            healed = AutonomyLedger(path)
            ok, bad = healed.verify()
            self.assertTrue(ok)
            self.assertIsNone(bad)

    def test_repair_truncates_tampered_active_on_pruned_load(self):
        """Tamper in the active tail of a pruned load: repair truncates just
        that file, keeps older segments byte-identical, and the healed chain
        verifies as an explicit suffix (flag follows content)."""
        import hashlib
        from harness.ledger import _canon
        self._tiny_bytes(monkeypatch_keep=1)
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "led.jsonl")
            led = AutonomyLedger(path)
            for i in range(6):
                led.append("offer", task_id=f"t{i}", model="m")
            fresh = AutonomyLedger(path)
            self.assertGreater(fresh.entries()[0]["seq"], 1)
            rotated = list(fresh._rotated_paths())
            self.assertTrue(rotated)
            with open(rotated[0], "rb") as f:
                before = hashlib.sha256(f.read()).hexdigest()
            tail = fresh.entries()
            fork = {"seq": tail[-1]["seq"] + 1,
                    "ts": "2026-09-06T19:14:06+00:00",
                    "event": "abort", "task_id": "fork",
                    "prev_hash": tail[-2]["hash"]}
            fork["hash"] = hashlib.sha256(
                _canon(fork).encode("utf-8")).hexdigest()
            with open(path, "a", encoding="utf-8") as f:
                f.write(_canon(fork) + "\n")
            reloaded = AutonomyLedger(path)
            self.assertFalse(reloaded.verify()[0])
            kept, dropped = reloaded.repair()
            self.assertGreaterEqual(dropped, 1)
            with open(rotated[0], "rb") as f:
                self.assertEqual(hashlib.sha256(f.read()).hexdigest(), before)
            healed = AutonomyLedger(path)
            ok, bad = healed.verify()
            self.assertTrue(ok)
            self.assertIsNone(bad)
            self.assertTrue(healed.chain_status()["pruned"])

    def test_tamper_after_rotation_still_detected(self):
        import json
        self._tiny_bytes()
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "led.jsonl")
            led = AutonomyLedger(path)
            led.append("offer", task_id="t1", model="m")
            led.append("offer", task_id="t2", model="m")
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()
            entry = json.loads(lines[0])
            entry["reason"] = "FORGED"
            with open(path, "w", encoding="utf-8") as f:
                f.write(json.dumps(entry, sort_keys=True) + "\n")
            ok, bad = AutonomyLedger(path).verify()
            self.assertFalse(ok)


class LedgerLockTests(unittest.TestCase):
    def test_contended_lock_fails_closed_fast_not_forever(self):
        """Regression: the POSIX acquire used blocking flock, so a wedged
        peer hung appends forever and the retry budget was dead code. With
        LOCK_NB the budget actually runs and fails closed with 'busy'."""
        import time
        import harness.ledger as ledger_mod
        from harness.errors import HarnessError
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "led.jsonl")
            led = AutonomyLedger(path)
            lock_path = path + ".lock"
            with open(lock_path, "a+b") as holder:
                try:
                    import msvcrt
                    holder.seek(0)
                    msvcrt.locking(holder.fileno(), msvcrt.LK_NBLCK, 1)
                    hold = True
                except ImportError:
                    import fcntl
                    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    hold = True
                self.assertTrue(hold)
                old_attempts = ledger_mod._LOCK_ATTEMPTS
                ledger_mod._LOCK_ATTEMPTS = 3
                try:
                    start = time.time()
                    with self.assertRaisesRegex(HarnessError, "busy"):
                        led.append("offer", task_id="t1", model="m")
                    self.assertLess(time.time() - start, 5,
                                    "bounded budget must fail fast, not hang")
                finally:
                    ledger_mod._LOCK_ATTEMPTS = old_attempts


if __name__ == "__main__":
    unittest.main()


class LedgerOwnershipTests(unittest.TestCase):
    """S6/DESIGN mirror pin: the analytics are OWNED by ledger_analytics,
    not merely re-exported -- a moved definition must fail this pin (the
    D1/D2/D8 lesson: audit extractors follow the owner)."""

    def test_analytics_owner_and_inheritance(self):
        import harness.ledger_analytics as la
        self.assertTrue(hasattr(la, "LedgerAnalytics"))
        self.assertIs(AutonomyLedger.participation_report,
                      la.LedgerAnalytics.participation_report)
        self.assertTrue(issubclass(AutonomyLedger, la.LedgerAnalytics))

    def test_lifecycle_owner(self):
        self.assertEqual(AutonomyLedger.append.__module__, "harness.ledger")
        self.assertEqual(AutonomyLedger.verify.__module__, "harness.ledger")
        self.assertEqual(AutonomyLedger.repair.__module__, "harness.ledger")
