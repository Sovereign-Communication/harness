"""Append-only, hash-chained JSONL ledger of consent & participation events.

This is how the harness *proves* autonomy rather than asserting it: every
offer -> decision -> dispatch -> completion/deferral is recorded, each entry
hash-chained to the previous so the record is tamper-evident. The
participation report surfaces aggregate autonomy metrics, including a
degenerate-consent flag for when a consent gate has become theater (near-100%
acceptance is a warning, not a success).
"""
import hashlib
import json
import os
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from .errors import HarnessError
from .output import eprint


def _canon(entry):
    return json.dumps(entry, sort_keys=True, separators=(",", ":"))


LEDGER_MAX_BYTES = 10 * 1024 * 1024  # rotate the JSONL at 10 MB
LEDGER_KEEP_ROTATIONS = 3
# Contended-append retry budget: an append holds the lock for well under a
# millisecond, so 100 x 20ms only ever trips if a peer process is wedged.
_LOCK_ATTEMPTS = 100
_LOCK_RETRY_SECONDS = 0.02


class AutonomyLedger:
    def __init__(self, path, caller=None):
        self.path = path
        # Session authorship for the evidence loop (e.g. "cli",
        # "mcp:peer/1.0"): stamped onto every appended event unless the
        # call site names one explicitly. Unset (None) leaves history
        # untagged, exactly as before.
        self.caller = caller
        self._lock = threading.Lock()
        self._tail = []
        self._seq = 0
        self._prev_hash = None
        self._segmented = False
        # Count of corrupt/torn lines skipped at load (audit #8b: must exist
        # as a real attribute on every instance, clean load included).
        self.quarantined = 0
        # Soft integrity: True when load recomputed a hash/prev/seq break.
        self.chain_broken = False
        self.first_bad_seq = None
        self._load()

    def _rotated_paths(self):
        """Return retained ledger segments oldest-first, excluding the active
        file. Segment names are deliberately opaque to callers; the active
        file is always the final segment."""
        directory = os.path.dirname(self.path) or "."
        prefix = os.path.basename(self.path) + "."
        try:
            names = sorted(fn for fn in os.listdir(directory) if fn.startswith(prefix)
                           and not fn.endswith(".lock") and not fn.endswith(".repair.tmp"))
        except OSError:
            names = []
        return [os.path.join(directory, fn) for fn in names]

    def _ledger_paths(self):
        return self._rotated_paths() + ([self.path] if os.path.exists(self.path) else [])

    def _rotate_if_needed(self):
        """Rotate the JSONL when it outgrows LEDGER_MAX_BYTES.

        Rotation is a segment operation, not a chain reset: verification and
        rebasing read retained segments oldest-first. A unique suffix avoids a
        same-second rotation overwriting evidence. Retention is explicit and
        bounded; operators needing a complete archive must copy segments out.
        """
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return
        if size < LEDGER_MAX_BYTES:
            return
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%f")
        rotated = f"{self.path}.{stamp}"
        try:
            os.replace(self.path, rotated)
        except OSError:
            return
        rotations = self._rotated_paths()
        for old in rotations[:-LEDGER_KEEP_ROTATIONS]:
            try:
                os.unlink(old)
            except OSError:
                pass
        self._segmented = bool(self._rotated_paths())
        # Anchor the cut: the new active file opens with a chained event
        # naming the rotated file and its tip, so a fresh loader can tell
        # pruning from prefix tampering. Failure degrades to today's
        # behavior (an unmarked boundary), never to a lost append: the
        # rotation already happened, the evidence is safe either way.
        try:
            self._append_segment_anchor(os.path.basename(rotated))
        except OSError as e:
            eprint(f"[ledger] segment anchor not written: {e}")

    @contextmanager
    def _file_lock(self):
        """Cross-process append lock with bounded retry.

        Playtest finding: two harness processes running in parallel crashed
        each other with 'ledger is busy' on the first contended append -- the
        acquisition was try-once. Appends are sub-millisecond, so a short
        retry budget absorbs real contention without ever silently skipping
        an append (fail closed after the budget, never before)."""
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        lf = None
        try:
            lf = open(self.path + '.lock', 'a+b')
        except OSError:
            lf = None  # lock file unwritable: degrade to thread-lock-only
        if lf is not None:
            locked = False
            try:
                for _ in range(_LOCK_ATTEMPTS):
                    try:
                        try:
                            import msvcrt
                            lf.seek(0)
                            msvcrt.locking(lf.fileno(), msvcrt.LK_NBLCK, 1)
                        except ImportError:
                            import fcntl
                            # LOCK_NB: without it this blocks forever on
                            # POSIX and the retry budget above is dead code
                            # (Windows already uses LK_NBLCK).
                            fcntl.flock(lf.fileno(),
                                        fcntl.LOCK_EX | fcntl.LOCK_NB)
                        locked = True
                        break
                    except OSError:
                        time.sleep(_LOCK_RETRY_SECONDS)
            finally:
                if not locked:
                    lf.close()
            if not locked:
                raise HarnessError(
                    f'ledger {self.path} is busy: another harness process '
                    f'held the lock for over {_LOCK_ATTEMPTS * _LOCK_RETRY_SECONDS:.0f}s')
        try:
            yield
        finally:
            if lf is not None:
                try:
                    try:
                        import msvcrt
                        lf.seek(0)
                        msvcrt.locking(lf.fileno(), msvcrt.LK_UNLCK, 1)
                    except ImportError:
                        import fcntl
                        fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
                lf.close()

    def _persist(self, line):
        """Append one canonical line (caller holds the file lock)."""
        with open(self.path, 'a', encoding='utf-8') as f:
            f.write(line + chr(10))
            f.flush()
            os.fsync(f.fileno())  # torn-line resistance: never lose the tail

    def _append_segment_anchor(self, rotated_name):
        """Open the fresh active file with a chained boundary record.

        Caller must hold both locks (only _rotate_if_needed calls this,
        itself called from append under both locks). The anchor chains
        normally -- seq tip+1, prev_hash tip -- so rotation never forks
        the chain; it additionally names the moved file and its tip.
        """
        tip_seq, tip_hash = self._seq, self._prev_hash
        entry = {
            "seq": tip_seq + 1,
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": "segment",
            "task_id": None,
            "prev_hash": tip_hash,
            "rotated": rotated_name,
            "tip_seq": tip_seq,
            "tip_hash": tip_hash,
        }
        body = dict(entry)
        body["hash"] = hashlib.sha256(_canon(body).encode("utf-8")).hexdigest()
        self._tail.append(body)
        self._seq = body["seq"]
        self._prev_hash = body["hash"]
        self._persist(_canon(body))

    def _load(self):
        paths = self._ledger_paths()
        self._segmented = len(paths) > 1 or bool(self._rotated_paths())
        self.chain_broken = False
        expected_prev = None
        for source_path in paths:
            with open(source_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        if (not isinstance(entry, dict) or "seq" not in entry
                                or "hash" not in entry):
                            raise ValueError("entry missing seq/hash")
                        if not isinstance(entry["seq"], int) or entry["seq"] < 0:
                            raise ValueError("seq must be a non-negative int")
                        body = {k: v for k, v in entry.items() if k != "hash"}
                        recomputed = hashlib.sha256(
                            _canon(body).encode("utf-8")).hexdigest()
                        if recomputed != entry["hash"]:
                            raise ValueError("hash mismatch")
                        if (expected_prev is not None
                                and entry.get("prev_hash") != expected_prev
                                and entry.get("event") != "segment"):
                            raise ValueError("prev_hash linkage broken")
                    except (ValueError, TypeError):
                        # Quarantine the damage, keep the intact prefix, and
                        # flag the break so verify never silently passes.
                        self.quarantined = getattr(self, "quarantined", 0) + 1
                        self.chain_broken = True
                        if self.first_bad_seq is None:
                            try:
                                self.first_bad_seq = entry.get("seq")
                            except Exception:
                                self.first_bad_seq = -1
                        eprint(f"[ledger] corrupt line quarantined in {source_path}; "
                               "run `harness ledger verify` for status.")
                        continue
                    self._tail.append(entry)
                    self._seq = entry["seq"]
                    self._prev_hash = entry["hash"]
                    expected_prev = entry["hash"]

    def _rebase_under_lock(self):
        """Re-read the file under the append lock and advance this instance
        past any events other processes wrote since our load.

        The append lock serializes *writes*, not *chain state*: without this
        rebase, two harness processes that loaded the ledger together both
        append from the same prev_hash and fork the chain at the second
        line (observed live as duplicate seq 812-814 from a parallel bench
        run). Cost: one small file read per append; appends are already
        fsync-bound.
        """
        last_seq = self._tail[-1]["seq"] if self._tail else 0
        last_hash = self._tail[-1]["hash"] if self._tail else None
        # Read every retained segment under the append lock. This is required
        # after rotation: the active file alone is only the newest segment.
        for source_path in self._ledger_paths():
            try:
                with open(source_path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                            if (not isinstance(entry, dict) or "seq" not in entry
                                    or "hash" not in entry):
                                raise ValueError("entry missing seq/hash")
                        except (ValueError, TypeError):
                            continue  # torn line: quarantine policy applies at next load
                        if entry["seq"] > last_seq:
                            self._tail.append(entry)
                            last_seq = entry["seq"]
                            last_hash = entry["hash"]
            except OSError:
                continue
        self._seq = last_seq
        self._prev_hash = last_hash

    def append(self, event, task_id=None, **fields):
        entry = {
            "seq": self._seq + 1,
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": event,
            "task_id": task_id,
            "prev_hash": self._prev_hash,
        }
        if self.caller is not None:
            entry.setdefault("caller", self.caller)
        entry.update(fields)
        body = dict(entry)
        body["hash"] = hashlib.sha256(_canon(body).encode("utf-8")).hexdigest()
        with self._lock:
            with self._file_lock():
                # Rebase AFTER the file lock is held: rebasing before it
                # raced another process's append between the read and the
                # lock, which is exactly the fork the rebase exists to
                # prevent.
                self._rebase_under_lock()
                body.pop("hash", None)  # recompute over the rebased seq/prev_hash only
                body["seq"] = self._seq + 1
                body["prev_hash"] = self._prev_hash
                body["hash"] = hashlib.sha256(_canon(body).encode("utf-8")).hexdigest()
                self._tail.append(body)
                self._seq = body["seq"]
                self._prev_hash = body["hash"]
                # Persist first. Rotating before this write would move the
                # old file away and leave the new active segment containing a
                # line whose predecessor is not loaded by a fresh instance.
                self._persist(_canon(body))
                self._rotate_if_needed()
        return body

    def entries(self):
        return list(self._tail)

    def tail(self, n=20):
        return self._tail[-n:]

    def _valid_prefix_len(self):
        """Length of the longest valid hash-chain prefix. ONE walk, consumed
        by both verify() and repair() so the two can never disagree about
        what 'valid' means."""
        prev = None
        for i, e in enumerate(self._tail):
            body = {k: v for k, v in e.items() if k != "hash"}
            # If old segments were pruned, the first retained entry has an
            # unverifiable predecessor. Accept that one external anchor, but
            # validate every link and hash after it. An unsegmented ledger still
            # requires the normal genesis prev_hash=None check.
            if i == 0 and self._segmented and body.get("prev_hash") is not None:
                prev = body.get("prev_hash")
            elif body.get("prev_hash") != prev:
                return i
            calc = hashlib.sha256(_canon(body).encode("utf-8")).hexdigest()
            if calc != e["hash"]:
                return i
            prev = e["hash"]
        return len(self._tail)

    def verify(self):
        """Recompute the hash chain. Returns (ok, first_bad_seq_or_None).

        A load that quarantined torn/tampered lines fails closed: the memory
        prefix may be intact, but the on-disk evidence does not check out.
        """
        n = self._valid_prefix_len()
        if n != len(self._tail):
            return False, self._tail[n]["seq"]
        if getattr(self, "chain_broken", False):
            return False, getattr(self, "first_bad_seq", None)
        return True, None

    def _segment_bounds(self):
        """Per-file evidence inventory, oldest segment first.

        Re-reads the retained segment files (cheap: only verify/status
        paths call this) so the report reflects disk, not memory. Blank
        and corrupt lines are skipped exactly like _load skips them.
        """
        bounds = []
        for source_path in self._ledger_paths():
            first_seq = last_seq = first_prev = tip_hash = None
            opens_with_segment = False
            seen_valid = False
            count = 0
            try:
                with open(source_path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                            if (not isinstance(entry, dict)
                                    or "seq" not in entry
                                    or "hash" not in entry):
                                raise ValueError("entry missing seq/hash")
                        except (ValueError, TypeError):
                            continue
                        if not seen_valid:
                            seen_valid = True
                            first_seq = entry["seq"]
                            first_prev = entry.get("prev_hash")
                            opens_with_segment = entry.get("event") == "segment"
                        last_seq = entry["seq"]
                        tip_hash = entry["hash"]
                        count += 1
            except OSError:
                continue
            bounds.append({"path": os.path.basename(source_path),
                           "entries": count, "first_seq": first_seq,
                           "last_seq": last_seq, "first_prev": first_prev,
                           "tip_hash": tip_hash,
                           "opens_with_segment_event": opens_with_segment})
        return bounds

    def chain_status(self):
        """Honest chain verdict: validity PLUS retention shape.

        verify() answers "do the retained links check out"; this answers
        the operator's real question -- "am I looking at a complete
        genesis chain or a valid suffix of a pruned one". A pruned prefix
        reports pruned=True with its cut seq; it never masquerades as
        genesis, and tampering still fails ok=False either way.
        """
        ok, bad = self.verify()
        bounds = self._segment_bounds()
        first_retained = self._tail[0]["seq"] if self._tail else None
        # Continuity from genesis: the first nonempty segment must open
        # with prev_hash None, and every later segment must open exactly
        # on the previous segment's tip (the anchor events make this
        # checkable). Anything else is a pruned -- or deleted -- prefix,
        # reported explicitly instead of masquerading as genesis.
        complete, prev_tip, started = True, None, False
        for b in bounds:
            if not b["entries"]:
                continue
            if not started:
                if b["first_prev"] is not None:
                    complete = False
                started = True
            elif b["first_prev"] != prev_tip:
                complete = False
            prev_tip = b["tip_hash"]
        pruned = started and not complete
        return {"ok": ok, "first_bad_seq": bad,
                "segmented": len(bounds) > 1, "segments": bounds,
                "first_retained_seq": first_retained, "pruned": pruned,
                "entries": len(self._tail),
                "quarantined": getattr(self, "quarantined", 0),
                "chain_broken_on_load": bool(getattr(self, "chain_broken", False))}

    def _rewrite_file(self, source_path, entries):
        """Atomically replace one segment file with the given valid entries
        (tmp + fsync + replace, so a crash never leaves half a segment)."""
        tmp = self.path + ".repair.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(_canon(e) + chr(10))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, source_path)

    def repair(self):
        """Truncate the invalid tail, preserving healthy segments as files.

        Repair is destructive to the invalid tail only: the segment file
        holding the first bad entry is rewritten to its valid prefix (or
        unlinked when nothing in it survives), every newer segment file is
        unlinked, and every older segment file is left byte-identical --
        collapsing healthy segments would destroy the boundary anchors
        that tell pruning from tampering. A healthy ledger is a no-op
        (callers should still archive the directory first: repair unlinks).
        Returns (kept, dropped).
        """
        with self._lock:
            with self._file_lock():
                kept = self._tail[:self._valid_prefix_len()]
                dropped = len(self._tail) - len(kept)
                # Load-time quarantine can remove damage from memory without
                # touching disk; treat those lines as repair-dropped too.
                quarantined = int(getattr(self, "quarantined", 0) or 0)
                if not dropped and not quarantined:
                    return len(kept), 0
                # Unsegmented heal: rewrite the single active file to the kept
                # prefix (this also removes quarantined torn tails).
                if kept and quarantined and not dropped and not self._segmented:
                    self._rewrite_file(self.path, kept)
                    self._tail = kept
                    self._seq = kept[-1]["seq"]
                    self._prev_hash = kept[-1]["hash"]
                    self.chain_broken = False
                    self.quarantined = 0
                    self.first_bad_seq = None
                    return len(kept), quarantined
                # Segmented: keep the existing per-file cut mapping so older
                # segment files stay byte-identical. Fall through even when
                # memory dropped==0 (quarantine already removed the damage
                # from the tail); the walk drops the unmatched disk line.
                dropped = dropped or quarantined
                if not kept:
                    # Nothing survives: reset to an empty active file and
                    # drop every rotated segment (today's behavior).
                    self._rewrite_file(self.path, [])
                    for segment in self._rotated_paths():
                        try:
                            os.unlink(segment)
                        except OSError:
                            pass
                    self._tail, self._seq, self._prev_hash = [], 0, None
                    self._segmented = False
                    self.chain_broken = False
                    self.quarantined = 0
                    return 0, dropped or quarantined
                # Map the kept prefix back onto segment files, oldest
                # first. Files before the cut are byte-identical (never
                # rewritten); the cut file is truncated to its kept
                # prefix; newer files are unlinked. Torn lines are
                # matched exactly like _load matches them, so quarantine
                # skips never shift the cut.
                remaining = list(kept)
                cut_done = False
                for source_path in self._ledger_paths():
                    if cut_done:
                        try:
                            os.unlink(source_path)
                        except OSError:
                            pass
                        continue
                    mine = []
                    try:
                        with open(source_path, encoding="utf-8") as f:
                            lines = f.read().splitlines()
                    except OSError:
                        continue
                    cut_here = False
                    for line in lines:
                        if not remaining:
                            cut_here = True
                            break
                        text = line.strip()
                        if not text:
                            continue
                        try:
                            entry = json.loads(text)
                            if (not isinstance(entry, dict)
                                    or "seq" not in entry
                                    or "hash" not in entry):
                                raise ValueError("entry missing seq/hash")
                        except (ValueError, TypeError):
                            continue
                        if entry["seq"] == remaining[0]["seq"] and \
                                entry["hash"] == remaining[0]["hash"]:
                            mine.append(remaining.pop(0))
                        else:
                            cut_here = True
                            break
                    if cut_here:
                        if mine:
                            self._rewrite_file(source_path, mine)
                        else:
                            try:
                                os.unlink(source_path)
                            except OSError:
                                pass
                        cut_done = True
                    # else: file fully kept -- untouched, byte-identical.
                self._tail = kept
                self._seq = kept[-1]["seq"] if kept else 0
                self._prev_hash = kept[-1]["hash"] if kept else None
                # A repaired chain starting mid-history is still a
                # suffix, not genesis: recompute the flag from content,
                # or verify() would call the next load tampered.
                self._segmented = bool(kept) and \
                    kept[0].get("prev_hash") is not None
                self.chain_broken = False
                self.quarantined = 0
                self.first_bad_seq = None
        return len(kept), dropped or quarantined

    def participation_report(self):
        events = self._tail
        counts = dict.fromkeys([
            "offer", "consent_accept", "consent_decline", "consent_defer",
            "consent_redirect", "consent_renew_accept", "consent_renew_defer",
            "dispatch_start", "verify_round", "defer_midtask", "complete",
            "abort", "escalate",
        ], 0)
        trust_gates = 0
        trust_hostile = 0
        offers_required = 0
        model_stats = {}
        per_caller = {}
        rounds_per_task = {}
        billable_events = {
            "model_result", "consent_accept", "consent_decline", "consent_defer",
            "consent_redirect", "consent_renew_accept", "consent_renew_defer",
        }
        tracked_cost = 0.0
        cost_event_count = 0

        def add_cost(event):
            nonlocal tracked_cost, cost_event_count
            if event.get("event") not in billable_events:
                return
            raw = event.get("billable_cost", event.get("cost", 0.0))
            try:
                amount = float(raw or 0.0)
            except (TypeError, ValueError):
                return
            if amount >= 0:
                tracked_cost += amount
                cost_event_count += 1

        def ms(model):
            return model_stats.setdefault(model or "?", {})

        for e in events:
            add_cost(e)
            ev = e["event"]
            if ev in counts:
                counts[ev] += 1
            if ev == "trust_gate":
                trust_gates += 1
                if e.get("severity") == "hostile":
                    trust_hostile += 1
            if ev == "offer":
                if e.get("required"):
                    offers_required += 1
                m_ = ms(e.get("model"))
                m_["offers"] = m_.get("offers", 0) + 1
            elif ev == "consent_accept":
                m_ = ms(e.get("model"))
                m_["accepts"] = m_.get("accepts", 0) + 1
            elif ev == "consent_decline":
                m_ = ms(e.get("model"))
                m_["declines"] = m_.get("declines", 0) + 1
            elif ev == "consent_defer":
                m_ = ms(e.get("model"))
                m_["defers"] = m_.get("defers", 0) + 1
            elif ev == "consent_redirect":
                m_ = ms(e.get("model"))
                m_["redirects"] = m_.get("redirects", 0) + 1
            elif ev == "consent_rotate":
                # Consent-unusable evidence: this model emitted an HTTP 200
                # answer the consent parser could not use (empty, reasoning-
                # only, unparseable). Tier faults (HTTP 429/401) are recoverable
                # rotation, not a model fault -- excluded, same rationale as the
                # apply lane's unusable_outputs.
                if not str(e.get("reason") or "").startswith("HTTP"):
                    stats = model_stats.setdefault(e.get("model"), {})
                    stats["consent_unusable"] = stats.get("consent_unusable", 0) + 1
            elif ev == "complete":
                m_ = ms(e.get("model"))
                m_["completions"] = m_.get("completions", 0) + 1
            elif ev == "trust_gate":
                # Safety-denial evidence, attributed when the denial names
                # a model (dispatch/mutation gates do; bare MCP boundary
                # refusals may not -- those still count host-globally).
                m_ = ms(e.get("model"))
                m_["trust_denials"] = m_.get("trust_denials", 0) + 1
                if e.get("severity") == "hostile":
                    m_["trust_hostile"] = m_.get("trust_hostile", 0) + 1
            elif ev == "verify_round":
                rounds_per_task.setdefault(e.get("task_id"), []).append(e.get("round"))
            caller = e.get("caller")
            if caller:
                cs = per_caller.setdefault(
                    caller, {"completions": 0, "trust_gates": 0,
                             "trust_hostile": 0})
                if ev == "complete":
                    cs["completions"] += 1
                elif ev == "trust_gate":
                    cs["trust_gates"] += 1
                    if e.get("severity") == "hostile":
                        cs["trust_hostile"] += 1

        offers = counts["offer"]
        accepts = counts["consent_accept"]

        def rate(a, b):
            return round(a / b, 4) if b else None

        report = {
            "offers": offers,
            "accepts": accepts,
            "declines": counts["consent_decline"],
            "defers": counts["consent_defer"],
            "redirects": counts["consent_redirect"],
            "renew_accepts": counts["consent_renew_accept"],
            "renew_defers": counts["consent_renew_defer"],
            "dispatch_starts": counts["dispatch_start"],
            "completions": counts["complete"],
            "aborts": counts["abort"],
            "deferred_midtask": counts["defer_midtask"],
            "escalations": counts["escalate"],
            "accept_rate": rate(accepts, offers),
            "decline_rate": rate(counts["consent_decline"], offers),
            "defer_rate": rate(counts["consent_defer"], offers),
            "redirect_rate": rate(counts["consent_redirect"], offers),
            "completion_rate": rate(counts["complete"], counts["dispatch_start"]),
            "consent_required_offers": offers_required,
            "per_model": model_stats,
            "trust_gates": trust_gates,
            "trust_hostile": trust_hostile,
            "per_caller": per_caller,
            "tracked_cost": round(tracked_cost, 9),
            "billable_event_count": cost_event_count,
            "consent_looks_degenerate": False,
        }
        if rounds_per_task:
            report["mean_verify_rounds"] = round(
                sum(len(v) for v in rounds_per_task.values()) / len(rounds_per_task), 2)
        if offers_required and report["accept_rate"] is not None and report["accept_rate"] >= 0.98:
            report["consent_looks_degenerate"] = True
            report["degenerate_note"] = (
                "near-100% acceptance: consent may be theater. Make decline/defer "
                "psychologically available in the probe prompt, or lower coercion framing.")

        # ---- confidence calibration (readiness verdict vs verify outcome) ----
        # For each model, join its self-declared HARNESS_READY: confident attempts
        # with the verify outcome of that same (task, round). confidence_precision
        # = confident-and-passed / (confident-and-passed + confident-and-failed).
        # High = well-calibrated (only says confident when it can do the work);
        # low = overconfident.
        confident_readiness = defaultdict(set)   # model -> {(task_id, round)}
        defer_count = defaultdict(int)            # model -> readiness defers
        missing_count = defaultdict(int)          # model -> no READY marker
        verify_hits = defaultdict(lambda: {"pass": 0, "fail": 0})
        for e in events:
            ev = e["event"]
            if ev == "readiness":
                m_ = e.get("model")
                dec = e.get("decision")
                if dec == "defer":
                    defer_count[m_] += 1
                elif dec == "missing":
                    missing_count[m_] += 1
                else:
                    confident_readiness[m_].add((e.get("task_id"), e.get("round")))
            elif ev == "verify_round" and e.get("readiness") == "confident":
                m_ = e.get("model")
                if (e.get("task_id"), e.get("round")) in confident_readiness[m_]:
                    if e.get("passed"):
                        verify_hits[m_]["pass"] += 1
                    else:
                        verify_hits[m_]["fail"] += 1

        # ---- observed success rate (verify gate outcomes, model_result joins) ----
        # success_rate = passes / (passes + fails) over every task a model ran,
        # using the verify_round outcome records (the same ground truth bench uses).
        task_outcome = defaultdict(lambda: {"pass": 0, "fail": 0})  # model -> code verify counts
        structured_outcome = defaultdict(lambda: {"pass": 0, "fail": 0})
        json_events = defaultdict(int)  # model -> JSON-expected model_result count
        model_events = defaultdict(int)  # model -> all model_result sample count
        for e in events:
            ev = e["event"]
            m_ = e.get("model")
            if ev == "verify_round" and m_ is not None and "passed" in e:
                task_outcome[m_]["pass" if e.get("passed") else "fail"] += 1
            elif ev == "model_result" and m_ is not None:
                model_events[m_] += 1
                if e.get("json_expected"):
                    json_events[m_] += 1
                # Unusable-output evidence (reasoning-only demotion): the
                # apply engine's protocol-condition failure -- HTTP 200 but
                # no usable content (the reasoning-only fallback). Per-model,
                # unlike a 429, which is recoverable rotation.
                if e.get("status") == "error" and \
                        "no usable content" in str(e.get("reason") or ""):
                    stats = model_stats.setdefault(m_, {})
                    stats["unusable_outputs"] = stats.get("unusable_outputs", 0) + 1
                # The live known-answer capability probe records `correct`; use
                # it as structured-task ground truth rather than pretending a
                # JSON-shaped but incorrect answer was a success.
                if e.get("task_type") == "structured" and "correct" in e:
                    structured_outcome[m_]["pass" if e.get("correct") else "fail"] += 1

        calibration = {}
        all_pass = all_fail = 0
        all_models = set(list(confident_readiness) + list(verify_hits) +
                         list(defer_count) + list(task_outcome) + list(model_events) +
                         list(model_stats))
        for m_ in all_models:
            passes = verify_hits[m_]["pass"]
            fails = verify_hits[m_]["fail"]
            denom = passes + fails
            all_pass += passes
            all_fail += fails
            to = task_outcome[m_]
            t_denom = to["pass"] + to["fail"]
            calibration[m_] = {
                "confident": len(confident_readiness[m_]),
                "defer": defer_count[m_],
                "missing": missing_count[m_],
                "confident_verified": denom,
                "confident_passed": passes,
                "confident_failed": fails,
                "confidence_precision": round(passes / denom, 3) if denom else None,
                "success_pass": to["pass"],
                "success_fail": to["fail"],
                "success_rate": round(to["pass"] / t_denom, 3) if t_denom else None,
                "structured_pass": structured_outcome[m_]["pass"],
                "structured_fail": structured_outcome[m_]["fail"],
                "structured_success_rate": (
                    round(structured_outcome[m_]["pass"] /
                          (structured_outcome[m_]["pass"] + structured_outcome[m_]["fail"]), 3)
                    if structured_outcome[m_]["pass"] + structured_outcome[m_]["fail"] else None),
                "structured_samples": (structured_outcome[m_]["pass"] +
                                        structured_outcome[m_]["fail"]),
                "json_samples": json_events[m_],
                "samples": model_events[m_],
                "unusable_outputs": model_stats.get(m_, {}).get("unusable_outputs", 0),
                "consent_unusable": model_stats.get(m_, {}).get("consent_unusable", 0),
                "trust_denials": model_stats.get(m_, {}).get("trust_denials", 0),
                "trust_hostile": model_stats.get(m_, {}).get("trust_hostile", 0),
            }
        report["calibration"] = calibration
        denom = all_pass + all_fail
        report["confidence_precision"] = round(all_pass / denom, 3) if denom else None
        report["underconfident_or_overconfident"] = sorted(
            m_ for m_, c in calibration.items()
            if c["confidence_precision"] is not None and c["confidence_precision"] < 0.6)
        return report
