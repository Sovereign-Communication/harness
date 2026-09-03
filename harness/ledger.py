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
from datetime import datetime, timezone


def _canon(entry):
    return json.dumps(entry, sort_keys=True, separators=(",", ":"))


class AutonomyLedger:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._tail = []
        self._seq = 0
        self._prev_hash = None
        self._load()

    def _load(self):
        if not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                self._tail.append(entry)
                self._seq = entry["seq"]
                self._prev_hash = entry["hash"]

    def append(self, event, task_id=None, **fields):
        entry = {
            "seq": self._seq + 1,
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": event,
            "task_id": task_id,
            "prev_hash": self._prev_hash,
        }
        entry.update(fields)
        body = dict(entry)
        body["hash"] = hashlib.sha256(_canon(body).encode("utf-8")).hexdigest()
        with self._lock:
            self._tail.append(body)
            self._seq = body["seq"]
            self._prev_hash = body["hash"]
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(_canon(body) + "\n")
        return body

    def entries(self):
        return list(self._tail)

    def tail(self, n=20):
        return self._tail[-n:]

    def verify(self):
        """Recompute the hash chain. Returns (ok, first_bad_seq_or_None)."""
        prev = None
        for e in self._tail:
            body = {k: v for k, v in e.items() if k != "hash"}
            if body.get("prev_hash") != prev:
                return False, e["seq"]
            calc = hashlib.sha256(_canon(body).encode("utf-8")).hexdigest()
            if calc != e["hash"]:
                return False, e["seq"]
            prev = e["hash"]
        return True, None

    def participation_report(self):
        events = self._tail
        counts = dict.fromkeys([
            "offer", "consent_accept", "consent_decline", "consent_defer",
            "consent_redirect", "consent_renew_accept", "consent_renew_defer",
            "dispatch_start", "verify_round", "defer_midtask", "complete",
            "abort", "escalate",
        ], 0)
        offers_required = 0
        model_stats = {}
        rounds_per_task = {}

        def ms(model):
            return model_stats.setdefault(model or "?", {})

        for e in events:
            ev = e["event"]
            if ev in counts:
                counts[ev] += 1
            if ev == "offer":
                if e.get("required"):
                    offers_required += 1
                m_ = ms(e.get("model"))
                m_["offers"] = m_.get("offers", 0) + 1
            elif ev == "consent_accept":
                m_ = ms(e.get("model")); m_["accepts"] = m_.get("accepts", 0) + 1
            elif ev == "consent_decline":
                m_ = ms(e.get("model")); m_["declines"] = m_.get("declines", 0) + 1
            elif ev == "consent_defer":
                m_ = ms(e.get("model")); m_["defers"] = m_.get("defers", 0) + 1
            elif ev == "consent_redirect":
                m_ = ms(e.get("model")); m_["redirects"] = m_.get("redirects", 0) + 1
            elif ev == "complete":
                m_ = ms(e.get("model")); m_["completions"] = m_.get("completions", 0) + 1
            elif ev == "verify_round":
                rounds_per_task.setdefault(e.get("task_id"), []).append(e.get("round"))

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
        return report