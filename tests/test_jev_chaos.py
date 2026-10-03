"""Bounded deterministic chaos over every Jev site (fixed seeds, < 20 s).

Concurrent mixed traffic against a hostile transport (transport errors,
HTTP errors, 401/422, billed-but-unparseable answers, garbage, random token
bills), a tiny churning cache and a tight ceiling. The invariants that must
hold whatever the interleaving:

* nothing is left reserved (``outstanding == 0``);
* the governor's spend equals the ledger's billed cost to the cent;
* no request is ever sent without a live reservation;
* no dispatch-state (site / reserver / guard / thread token) leaks;
* no exception escapes a site; every site survives an overrun;
* zero-cost settlements are never booked as overruns.
"""
import collections
import random
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from harness.jev import (ACTIVE_GUARD, ACTIVE_RESERVER, ACTIVE_SITE, JevCache,
                         JevEvaluator, jev_cost)
from harness.jev_policy import policy_for
from tests import test_jev_core_hardening as T
from tests.test_jev_issue_sort import sample_pack as issue_pack
from tests.test_jev_log_judgment import sample_log_pack
from tests.test_jev_repo_judgment import STATE, repo_pack

EVIDENCE = {dim: {"score": 10.0, "checks_satisfied": 12, "checks_count": 12,
                  "checks": []} for dim in ("A", "R", "SM", "SD")}


def sites(v):
    """Every public ``evaluate_*`` site, parameterised so repeats can differ."""
    return {
        "diff": lambda p: p.evaluate_diff(T.DIFF, "set x to {}".format(v), "x.py"),
        "triage": lambda p: p.evaluate_triage("fix loop {}".format(v), ["a.py"]),
        "route": lambda p: p.evaluate_route("fix loop {}".format(v), ["a.py"]),
        "plan": lambda p: p.evaluate_plan("fix loop {}".format(v), ["a.py"]),
        "file_triage": lambda p: p.evaluate_file_triage(
            "fix {}".format(v), ["a.py", "b.py"]),
        "claims": lambda p: p.evaluate_claim_support(
            ["sky {}".format(v)], "ctx", enabled=True),
        "decision": lambda p: p.evaluate_decision("act", "end{}".format(v), "ctx"),
        "answer": lambda p: p.evaluate_answer("q{}".format(v), "a", "c"),
        "escalation": lambda p: p.evaluate_escalation_decision(
            "fail ctx {}".format(v)),
        "completion": lambda p: p.evaluate_completion_nouls(
            "goal{}".format(v), "state"),
        "scope": lambda p: p.evaluate_scope(
            {"goal": "g{}".format(v), "in_scope": ["a"], "state": "s"}),
        "model_route": lambda p: p.evaluate_model_route(
            "do thing {}".format(v), T.PACK),
        "issue_sort": lambda p: p.evaluate_issue_sort(
            {"issue": "auth token login broken {}".format(v)}, issue_pack()),
        "log_item": lambda p: p.evaluate_log_item(
            {"item": "dial failed on swarm {}".format(v)}, sample_log_pack()),
        "repo_summary": lambda p: p.evaluate_repo_summary(STATE, repo_pack()),
        "audit_dims": lambda p: p.evaluate_audit_dimensions(EVIDENCE),
        "hourglass": lambda p: p.evaluate_hourglass_stage(
            "context_intake", {"request": "audit {}".format(v)}),
    }


class _Case(T._Base):
    def runTest(self):  # pragma: no cover - only used as a helper instance
        pass


def run_chaos(seed, n_tasks=240, workers=6, ceiling_mult=60):
    rnd = random.Random(seed)
    case = _Case()
    case.setUp()
    gov = case.governor(max_cost=jev_cost(1024) * ceiling_mult)
    lock = threading.Lock()
    unreserved, leaks = [], []
    zero_overruns = [0]
    real_overrun = gov.record_overrun

    def record_overrun(cost, label):
        if not cost:
            zero_overruns[0] += 1
        return real_overrun(cost, label)
    gov.record_overrun = record_overrun
    box = {}

    class Transport:
        def post(self, url, key, payload, timeout=45):
            handle = getattr(box["policy"]._tl, "token", None)
            if handle is None or not getattr(handle, "reserved", False):
                with lock:
                    unreserved.append(sorted(payload["questions"])[:2])
            with lock:
                roll = rnd.random()
                tokens = rnd.choice([10, 100, 900, 1500, 4000])
                nap = rnd.random() * 0.002
            time.sleep(nap)
            usage = {"input_tokens": tokens, "output_tokens": 2}
            if roll < 0.15:
                raise OSError("boom")
            if roll < 0.22:
                return 500, {"error": "x"}
            if roll < 0.25:
                return 401, {"error": "x"}
            if roll < 0.27:
                return 422, {"error": "x"}
            if roll < 0.35:
                return 200, {"model": "jev-test", "usage": usage,
                             "answers": {"zz": 1}}       # billed, unparseable
            if roll < 0.37:
                return 200, "garbage"
            return 200, {"model": "jev-test", "usage": usage,
                         "answers": T.answers_for(payload["questions"])}

    transport = Transport()
    cache = JevCache(max_entries=3, ttl=0.02)
    evaluator = JevEvaluator(api_key="k", transport=transport, cache=cache)
    policy = policy_for(case.keyed_settings(), transport=transport,
                        governor=gov, ledger=case.ledger, evaluator=evaluator,
                        breaker_threshold=10 ** 6)
    box["policy"] = policy
    escapes = collections.Counter()
    names = list(sites(0))

    def task(i):
        r = random.Random(seed * 1000 + i)
        v = r.randrange(6)
        name = r.choice(names)
        fan = r.random() < 0.15
        try:
            if fan:
                jobs = [(n, (lambda n=n: sites(v)[n](policy)))
                        for n in r.sample(names, 3)]
                policy.fan_out(jobs, max_workers=3)
            else:
                sites(v)[name](policy)
        except BaseException as exc:  # noqa: BLE001 - the point of the test
            with lock:
                escapes[("FAN" if fan else name, type(exc).__name__)] += 1
        if r.random() < 0.1:
            cache.clear()
        if (ACTIVE_RESERVER.get() is not None or ACTIVE_GUARD.get() is not None
                or ACTIVE_SITE.get() != ""
                or getattr(policy._tl, "token", None) is not None):
            with lock:
                leaks.append(name)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(task, range(n_tasks)))
    policy.flush_fallbacks()
    rows = [e for e in case.ledger.entries() if e["event"] == "jev_eval"]
    billed = sum(float(e.get("cost") or 0.0) for e in rows
                 if not e.get("cache_hit") and not e.get("flush"))
    case.doCleanups()
    return {"outstanding": gov.outstanding, "spent": gov.spent, "billed": billed,
            "unreserved": len(unreserved), "leaks": len(leaks),
            "escapes": dict(escapes), "zero_overruns": zero_overruns[0],
            "rows": len(rows)}


class ChaosTests(unittest.TestCase):
    def check(self, seed, mult):
        out = run_chaos(seed, ceiling_mult=mult)
        self.assertAlmostEqual(out["outstanding"], 0.0, places=12, msg=out)
        self.assertAlmostEqual(out["spent"], out["billed"], places=9, msg=out)
        self.assertEqual(out["unreserved"], 0, out)
        self.assertEqual(out["leaks"], 0, out)
        self.assertEqual(out["zero_overruns"], 0, out)
        self.assertFalse(out["escapes"], out)
        self.assertGreater(out["rows"], 0)

    def test_roomy_budget(self):
        started = time.monotonic()
        for seed in (1, 2):
            self.check(seed, 400)
        self.assertLess(time.monotonic() - started, 20)

    def test_tight_budget_forces_overruns_and_refusals(self):
        started = time.monotonic()
        for seed in (3, 4):
            self.check(seed, 12)
        self.assertLess(time.monotonic() - started, 20)


if __name__ == "__main__":
    unittest.main()
