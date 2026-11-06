"""The per-repo queue bus and laptop watcher (vision driver, item 4).

These pins cover the contract the lane relies on: the documented state machine
and nothing else, ownership that means something, a finished item that can
never be replayed, an auditable event per move, a watcher that is dormant on an
empty queue, and supervised launches that refuse to start without authorization.
"""
import json
import os
import tempfile
import unittest

from harness.errors import HarnessError
from harness.queue_bus import (
    BLOCKED, CHECKPOINTED, CLAIMED, DONE, PARKED, QUEUED, RUNNING,
    QueueBus, QueueError, Watcher, WatchOutcome,
)


class QueueBusCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bus = QueueBus(self.tmp.name)

    def queued(self, item_id="item-1", repo="harness"):
        return self.bus.enqueue(repo, item_id, "drive one launch",
                                payload={"goal": "open notepad"})


class StateMachineTests(QueueBusCase):

    def test_enqueue_lands_in_queued_with_one_event(self):
        item = self.queued()
        self.assertEqual(item.state, QUEUED)
        self.assertEqual(item.attempts, 0)
        events = self.bus.history("harness")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["from"], None)
        self.assertEqual(events[0]["to"], QUEUED)
        self.assertEqual(events[0]["actor"], "lane")

    def test_a_second_enqueue_does_not_overwrite_history(self):
        self.queued()
        with self.assertRaises(QueueError):
            self.queued()
        self.assertEqual(len(self.bus.history("harness")), 1)

    def test_the_documented_chain_runs_in_order(self):
        self.queued()
        self.bus.claim("harness", "item-1", "watcher")
        self.bus.start("harness", "item-1", "watcher")
        self.bus.checkpoint("harness", "item-1", "watcher",
                            {"step": 3, "note": "halfway"})
        final = self.bus.finish("harness", "item-1", "watcher", DONE,
                                reason="verified")
        self.assertEqual(final.state, DONE)
        trail = [(event["from"], event["to"])
                 for event in self.bus.history("harness")]
        self.assertEqual(trail, [
            (None, QUEUED), (QUEUED, CLAIMED), (CLAIMED, RUNNING),
            (RUNNING, CHECKPOINTED), (CHECKPOINTED, DONE),
        ])
        self.assertEqual(final.attempts, 1)

    def test_skipping_a_stage_is_refused_and_changes_nothing(self):
        self.queued()
        with self.assertRaises(QueueError) as ctx:
            self.bus.finish("harness", "item-1", "lane", DONE)
        self.assertIn("queued", str(ctx.exception))
        self.assertEqual(self.bus.item("harness", "item-1").state, QUEUED)
        self.assertEqual(len(self.bus.history("harness")), 1)

    def test_done_is_final_and_never_replayed(self):
        self.queued()
        self.bus.claim("harness", "item-1", "watcher")
        self.bus.start("harness", "item-1", "watcher")
        self.bus.checkpoint("harness", "item-1", "watcher", {"step": 1})
        self.bus.finish("harness", "item-1", "watcher", DONE)
        for attempt in (
                lambda: self.bus.claim("harness", "item-1", "watcher"),
                lambda: self.bus.requeue("harness", "item-1", "operator"),
                lambda: self.bus.finish("harness", "item-1", "watcher", DONE)):
            with self.assertRaises(QueueError):
                attempt()
        self.assertEqual(self.bus.item("harness", "item-1").state, DONE)

    def test_only_the_checkpoint_closes_as_done(self):
        """A pass that wrote no resume point has no evidence of progress."""
        self.queued()
        self.bus.claim("harness", "item-1", "watcher")
        self.bus.start("harness", "item-1", "watcher")
        with self.assertRaises(QueueError):
            self.bus.finish("harness", "item-1", "watcher", DONE)
        # ...but a failure is still recorded from the running state.
        blocked = self.bus.finish("harness", "item-1", "watcher", BLOCKED,
                                  reason="launch failed: not found")
        self.assertEqual(blocked.state, BLOCKED)
        self.assertIn("not found", blocked.reason)

    def test_ownership_binds_every_move_after_the_claim(self):
        self.queued()
        self.bus.claim("harness", "item-1", "watcher")
        with self.assertRaises(QueueError) as ctx:
            self.bus.start("harness", "item-1", "someone-else")
        self.assertIn("watcher", str(ctx.exception))
        with self.assertRaises(QueueError):
            self.bus.claim("harness", "item-1", "someone-else")
        self.assertEqual(self.bus.item("harness", "item-1").state, CLAIMED)

    def test_an_anonymous_move_is_refused(self):
        self.queued()
        with self.assertRaises(QueueError):
            self.bus.claim("harness", "item-1", "  ")

    def test_a_hold_returns_to_the_queue_only_by_operator_requeue(self):
        self.queued()
        self.bus.claim("harness", "item-1", "watcher")
        self.bus.finish("harness", "item-1", "watcher", PARKED,
                        reason="owner deferred")
        back = self.bus.requeue("harness", "item-1", "operator",
                                reason="owner resumed")
        self.assertEqual(back.state, QUEUED)
        self.assertEqual(back.owner, "")
        self.assertEqual(back.attempts, 1)

    def test_a_checkpoint_payload_is_written_before_the_state_change(self):
        self.queued()
        self.bus.claim("harness", "item-1", "watcher")
        self.bus.start("harness", "item-1", "watcher")
        self.bus.checkpoint("harness", "item-1", "watcher", {"step": 2})
        path = os.path.join(self.tmp.name, "harness", "checkpoints",
                            "item-1.json")
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), {"step": 2})

    def test_an_empty_checkpoint_is_refused(self):
        self.queued()
        self.bus.claim("harness", "item-1", "watcher")
        self.bus.start("harness", "item-1", "watcher")
        with self.assertRaises(QueueError):
            self.bus.checkpoint("harness", "item-1", "watcher", {})
        self.assertEqual(self.bus.item("harness", "item-1").state, RUNNING)

    def test_repos_and_snapshots_separate_by_repo(self):
        self.queued("item-1", "harness")
        self.queued("item-2", "harness")
        self.queued("item-3", "other-repo")
        self.bus.claim("other-repo", "item-3", "watcher")
        snapshot = self.bus.snapshot("harness")
        self.assertEqual(snapshot["states"][QUEUED], 2)
        self.assertEqual(snapshot["states"][CLAIMED], 0)
        self.assertEqual(self.bus.repos(), ["harness", "other-repo"])

    def test_unknown_item_and_repo_are_named_refusals(self):
        with self.assertRaises(QueueError):
            self.bus.item("harness", "nope")
        with self.assertRaises(QueueError):
            self.bus.enqueue("../escape", "item-1", "nope")
        with self.assertRaises(QueueError):
            self.bus.enqueue("harness", "..", "nope")

    def test_empty_summary_is_not_a_unit_of_work(self):
        with self.assertRaises(QueueError):
            self.bus.enqueue("harness", "item-1", "   ")

    def test_a_corrupt_event_line_fails_closed(self):
        self.queued()
        path = os.path.join(self.tmp.name, "harness", "events.jsonl")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("{not json\n")
        with self.assertRaises(QueueError):
            self.bus.history("harness")


class WatcherTests(QueueBusCase):

    def test_dormant_on_an_empty_queue_without_writing_events(self):
        empty = QueueBus(os.path.join(self.tmp.name, "nowhere"))
        outcome = Watcher(empty).step(authorized=True)
        self.assertEqual(outcome.action, Watcher.DORMANT)
        self.assertEqual(empty.repos(), [])

    def test_dormant_run_pauses_between_empty_passes(self):
        empty = QueueBus(os.path.join(self.tmp.name, "nowhere"))
        sleeps = []
        Watcher(empty, interval=7).run(steps=3, authorized=True,
                                       sleep=sleeps.append)
        self.assertEqual(sleeps, [7, 7, 7])

    def test_a_supervised_watcher_claims_nothing(self):
        self.queued()
        outcome = Watcher(self.bus).step(authorized=False)
        self.assertEqual(outcome.action, Watcher.SUPERVISED_HOLD)
        self.assertEqual(outcome.item_id, "item-1")
        self.assertEqual(self.bus.item("harness", "item-1").state, QUEUED)
        self.assertEqual(len(self.bus.history("harness")), 1)

    def test_an_authorized_watcher_claims_and_starts_one_item(self):
        self.queued()
        seen = []

        def launcher(item):
            seen.append((item.state, item.id))
            return "launched notepad"

        outcome = Watcher(self.bus, launcher).step(authorized=True)
        self.assertEqual(outcome.action, WatchOutcome("claimed").action)
        self.assertEqual(outcome.item_id, "item-1")
        self.assertEqual(seen, [(RUNNING, "item-1")])
        self.assertEqual(self.bus.item("harness", "item-1").state, RUNNING)

    def test_the_watcher_never_writes_done(self):
        self.queued()
        Watcher(self.bus, lambda item: "launched").step(authorized=True)
        self.assertEqual(self.bus.item("harness", "item-1").state, RUNNING)

    def test_a_failed_launch_blocks_with_its_reason(self):
        self.queued()
        def launcher(item):
            raise RuntimeError("app missing")
        outcome = Watcher(self.bus, launcher).step(authorized=True)
        self.assertIn("app missing", outcome.detail)
        item = self.bus.item("harness", "item-1")
        self.assertEqual(item.state, BLOCKED)
        self.assertIn("app missing", item.reason)

    def test_a_step_takes_one_item_and_leaves_the_rest_queued(self):
        self.queued("item-1")
        self.queued("item-2")
        Watcher(self.bus, lambda item: "launched").step(authorized=True)
        self.assertEqual(self.bus.item("harness", "item-1").state, RUNNING)
        self.assertEqual(self.bus.item("harness", "item-2").state, QUEUED)

    def test_the_watcher_needs_an_explicit_step_count(self):
        with self.assertRaises(HarnessError):
            Watcher(self.bus).run(steps=0, authorized=True)


if __name__ == "__main__":  # pragma: no cover - manual runs
    unittest.main()
