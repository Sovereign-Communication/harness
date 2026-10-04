"""Dynamic model rotation: the ranker is wired, and it obeys the rulings.

`rank_models_dynamically` shipped with the Dynamic Allocation Engine and had
no production caller. These tests pin the wiring and, more importantly, the
two constraints that make it safe to ship:

* the declared head of a pool is never displaced (DF-LING-2: in rotation,
  never default; and the cheap-first discipline generally), and
* a pool nobody has observed yet is served exactly as declared.

No network, no key, no ledger.
"""
import unittest

from harness.dynamic_allocation import rank_models_dynamically
from harness.router import Router


PANEL = ["a:free", "b:free", "c:free"]


def _router(**kw):
    return Router(panel=list(PANEL), judge="j:free", apply_model="a:free",
                  apply_pool=list(PANEL), **kw)


class UnobservedPoolsTests(unittest.TestCase):
    def test_a_pool_with_no_observations_is_served_as_declared(self):
        r = _router()
        self.assertEqual(r.apply_pool, PANEL)
        self.assertEqual(r.panel_pool, PANEL)

    def test_an_observation_outside_the_pool_does_not_reorder_it(self):
        # Health on some other model must not silently re-rank this pool.
        r = _router()
        r.note_model_result("zzz:free", ok=False, latency_ms=5000)
        self.assertEqual(r.apply_pool, PANEL)

    def test_a_single_model_pool_is_never_reordered(self):
        r = Router(panel=["only:free"], judge="j:free", apply_model="only:free")
        r.note_model_result("only:free", ok=False)
        self.assertEqual(r.apply_pool, ["only:free"])

    def test_setting_a_pool_reads_back_as_set(self):
        r = _router()
        r.apply_pool = ["x:free", "y:free"]
        self.assertEqual(r.apply_pool, ["x:free", "y:free"])


class HealthRegistryTests(unittest.TestCase):
    def test_a_failure_is_counted(self):
        r = _router()
        self.assertEqual(r.note_model_result("b:free", ok=False)["errors"], 1)

    def test_success_walks_the_error_count_back_down(self):
        r = _router()
        r.note_model_result("b:free", ok=False)
        r.note_model_result("b:free", ok=False)
        self.assertEqual(r.note_model_result("b:free", ok=True)["errors"], 1)

    def test_the_error_count_never_goes_negative(self):
        r = _router()
        r.note_model_result("b:free", ok=True)
        self.assertEqual(r.model_health("b:free")["errors"], 0)

    def test_an_unrecorded_model_has_no_health(self):
        self.assertIsNone(_router().model_health("b:free"))

    def test_a_blank_model_is_ignored(self):
        r = _router()
        self.assertIsNone(r.note_model_result("", ok=False))
        self.assertEqual(r.model_health(), {})

    def test_health_is_copied_out_not_shared(self):
        r = _router()
        r.note_model_result("b:free", ok=False)
        entry = r.model_health("b:free")
        entry["errors"] = 99
        self.assertEqual(r.model_health("b:free")["errors"], 1)


class RotationOrderTests(unittest.TestCase):
    def test_a_failing_model_sinks_behind_a_healthy_one(self):
        r = _router()
        r.note_model_result("b:free", ok=False)
        self.assertEqual(r.apply_pool, ["a:free", "c:free", "b:free"])

    def test_the_declared_head_is_never_displaced(self):
        # Even when the head itself has failed, it stays first: it is the
        # operator's declared primary, and DF-LING-2 keeps a rotation member
        # out of the default slot.
        r = _router()
        r.note_model_result("a:free", ok=False)
        r.note_model_result("b:free", ok=False)
        self.assertEqual(r.apply_pool[0], "a:free")

    def test_a_recovered_model_is_no_longer_penalised_for_errors(self):
        r = _router()
        r.note_model_result("b:free", ok=False)
        self.assertEqual(r.apply_pool[-1], "b:free")
        r.note_model_result("b:free", ok=True)
        self.assertEqual(r.apply_pool[0], "a:free")

    def test_every_pool_is_ordered_not_just_apply(self):
        r = Router(panel=list(PANEL), judge="j:free", apply_model="a:free",
                   apply_pool=list(PANEL), escalation_pool=list(PANEL),
                   specialist_pool=list(PANEL))
        for pool in (r.panel_pool, r.apply_pool, r.escalation_pool,
                     r.specialist_pool):
            pool[1] and r.note_model_result(pool[1], ok=False)
        self.assertEqual(r.escalation_pool, ["a:free", "c:free", "b:free"])
        self.assertEqual(r.specialist_pool, ["a:free", "c:free", "b:free"])

    def test_ordering_keeps_the_same_members(self):
        r = _router()
        r.note_model_result("b:free", ok=False)
        self.assertEqual(sorted(r.apply_pool), sorted(PANEL))


class RankerIsTheOneInUseTests(unittest.TestCase):
    def test_rotation_order_matches_the_ranker_on_the_tail(self):
        # The wiring must delegate, not reimplement, the policy.
        r = _router()
        health = {"b:free": {"errors": 2, "latency_ms": 900}}
        r._model_health = dict(health)
        expected = rank_models_dynamically(
            PANEL[1:], health_status=health, prefer_paid=not r.use_free)
        self.assertEqual(r.apply_pool, [PANEL[0]] + expected)


if __name__ == "__main__":
    unittest.main()
