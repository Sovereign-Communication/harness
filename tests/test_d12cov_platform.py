"""D12 coverage pins: platform and edge branches the main suites never reach.

D12 requires >=95% of changed lines to be executed by the suite under
tracing. The branches pinned here are the ones a happy-path run cannot
touch: lock contention and lock-shape errors in the OSAL, queue-bus
refusals and watcher outcomes, MCP log-shape fallbacks, repo-scope
exclusions for links and vanished entries, and the no-tools route
override in the hourglass. Each test asserts the branch's real outcome
(exception, value, or recorded state), not merely that the line ran.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from harness import chat as chat_mod
from harness import osal
from harness.cli import main as cli_main
from harness.endpoint_pricing import _normalize_overrides, _parse_endpoints
from harness.errors import HarnessError, ToolCancelled
from harness.jev import JevEvaluationResult
from harness.lifecycle import LifecycleCoordinator, adapter_for
from harness.mcp import McpServer
from harness.mcp_log import log_tool_call, scrub
from harness.overlay import run_overlay
from harness.queue_bus import (
    BLOCKED, CLAIMED, QUEUED, RUNNING,
    QueueBus, QueueError, Watcher, run_cli as queue_run_cli,
)
from harness.repo_scope import audit_scope
from harness.request_intent import is_read_only_audit_request
from harness.restart_policy import RestartStateStore
from harness.task_lifecycle import TaskLaneLifecycleMixin
from harness.workspace_audit import _summary


class TempTree(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def path(self, *parts):
        return os.path.join(self.root, *parts)


# --------------------------------------------------------------- osal locks

class OsalFileLockEdgeTests(TempTree):

    def test_lock_path_that_is_not_a_regular_file_is_refused(self):
        fifo = self.path("lockfifo")
        os.mkfifo(fifo)
        with self.assertRaises(osal.OsalError) as caught:
            osal.acquire_file_lock(fifo)
        self.assertIn("must be a regular file", str(caught.exception))

    def test_a_held_lock_refuses_the_second_acquirer(self):
        target = self.path("held.lock")
        fd = osal.acquire_file_lock(target)
        try:
            with self.assertRaises(osal.OsalError) as caught:
                osal.acquire_file_lock(target)
            self.assertIn("already held or unavailable",
                          str(caught.exception))
        finally:
            osal.release_file_lock(fd)

    def test_a_zero_byte_write_cannot_initialize_the_lock(self):
        target = self.path("short.lock")
        with mock.patch.object(os, "write", return_value=0):
            with self.assertRaises(osal.OsalError) as caught:
                osal.acquire_file_lock(target)
        self.assertIn("cannot initialize lock file", str(caught.exception))

    def test_an_oserror_after_open_becomes_a_lock_open_refusal(self):
        target = self.path("ioer.lock")
        with mock.patch.object(os, "write",
                               side_effect=OSError("device gone")):
            with self.assertRaises(osal.OsalError) as caught:
                osal.acquire_file_lock(target)
        self.assertIn("cannot open lock file", str(caught.exception))

    def test_a_missing_parent_directory_is_a_lock_open_refusal(self):
        target = self.path("no", "such", "dir", "x.lock")
        with self.assertRaises(osal.OsalError) as caught:
            osal.acquire_file_lock(target)
        self.assertIn("cannot open lock file", str(caught.exception))

    def test_the_win32_locking_branch_uses_msvcrt_when_present(self):
        target = self.path("win.lock")
        calls = []
        fake_msvcrt = types.SimpleNamespace(
            LK_NBLCK=1,
            locking=lambda fd, mode, nbytes: calls.append((fd, mode, nbytes)))
        with mock.patch.object(sys, "platform", "win32"), \
                mock.patch.dict(sys.modules, {"msvcrt": fake_msvcrt}):
            fd = osal.acquire_file_lock(target)
        try:
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0], fd)
            self.assertEqual(calls[0][1], fake_msvcrt.LK_NBLCK)
            self.assertEqual(calls[0][2], 1)
        finally:
            osal.release_file_lock(fd)

    def test_the_win32_branch_without_msvcrt_reports_unavailable(self):
        target = self.path("win2.lock")
        with mock.patch.object(sys, "platform", "win32"), \
                mock.patch.dict(sys.modules, {"msvcrt": None}):
            with self.assertRaises(osal.OsalError) as caught:
                osal.acquire_file_lock(target)
        self.assertIn("already held or unavailable", str(caught.exception))


# --------------------------------------------------------------- queue bus

class QueueBusEdgeTests(TempTree):

    def setUp(self):
        super().setUp()
        self.bus = QueueBus(self.root)

    def _write_item(self, repo, item_id, data):
        folder = Path(self.bus._items_dir(repo))
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{item_id}.json").write_text(
            json.dumps(data), encoding="utf-8")

    def test_an_unreadable_item_file_is_refused_by_name(self):
        folder = Path(self.bus._items_dir("harness"))
        folder.mkdir(parents=True)
        (folder / "bad.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(QueueError) as caught:
            self.bus.item("harness", "bad")
        self.assertIn("is unreadable", str(caught.exception))

    def test_an_item_with_an_unknown_state_is_refused(self):
        self._write_item("harness", "odd", {
            "id": "odd", "repo": "harness", "summary": "s",
            "state": "teleported"})
        with self.assertRaises(QueueError) as caught:
            self.bus.item("harness", "odd")
        self.assertIn("unknown state 'teleported'", str(caught.exception))

    def test_items_for_a_repo_that_does_not_exist_is_empty(self):
        self.assertEqual(self.bus.items("ghost"), [])

    def test_items_skips_non_json_neighbours(self):
        self.bus.enqueue("harness", "item-1", "real work")
        Path(self.bus._items_dir("harness"), "notes.txt").write_text(
            "not an item", encoding="utf-8")
        found = self.bus.items("harness")
        self.assertEqual([item.id for item in found], ["item-1"])

    def test_history_without_an_events_file_is_empty(self):
        self.assertEqual(self.bus.history("ghost"), [])

    def test_history_skips_blank_lines(self):
        self.bus.enqueue("harness", "item-1", "real work")
        events = Path(self.bus._events_path("harness"))
        with open(events, "a", encoding="utf-8") as handle:
            handle.write("\n\n")
        history = self.bus.history("harness")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["to"], QUEUED)

    def test_resume_reopens_a_checkpointed_item(self):
        self.bus.enqueue("harness", "item-1", "real work")
        self.bus.claim("harness", "item-1", "watcher")
        self.bus.start("harness", "item-1", "watcher")
        self.bus.checkpoint("harness", "item-1", "watcher", {"step": 1})
        resumed = self.bus.resume("harness", "item-1", "watcher")
        self.assertEqual(resumed.state, RUNNING)

    def test_finish_refuses_an_unknown_outcome(self):
        self.bus.enqueue("harness", "item-1", "real work")
        with self.assertRaises(QueueError) as caught:
            self.bus.finish("harness", "item-1", "watcher", "vanished")
        self.assertIn("finish outcome must be one of", str(caught.exception))

    def test_an_illegal_move_is_refused_by_name(self):
        self.bus.enqueue("harness", "item-1", "real work")
        with self.assertRaises(QueueError) as caught:
            self.bus._transition("harness", "item-1", BLOCKED, "watcher",
                                 allowed_from=(QUEUED,))
        self.assertIn("is not a legal queue move", str(caught.exception))

    def test_a_claim_against_an_existing_owner_is_refused(self):
        self._write_item("harness", "owned", {
            "id": "owned", "repo": "harness", "summary": "s",
            "state": QUEUED, "owner": "first"})
        with self.assertRaises(QueueError) as caught:
            self.bus._transition("harness", "owned", CLAIMED, "second",
                                 allowed_from=(QUEUED,), clear_owner=True)
        self.assertIn("already claimed by 'first'", str(caught.exception))

    def test_a_held_repo_lock_makes_mutations_refuse_as_busy(self):
        repo_dir = Path(self.bus._repo_dir("harness"))
        repo_dir.mkdir(parents=True)
        fd = osal.acquire_file_lock(str(repo_dir / ".lock"))
        try:
            with self.assertRaises(QueueError) as caught:
                self.bus.enqueue("harness", "item-1", "real work")
            self.assertIn("is busy", str(caught.exception))
        finally:
            osal.release_file_lock(fd)


class WatcherEdgeTests(TempTree):

    def test_an_unauthorized_step_on_an_empty_queue_is_dormant(self):
        outcome = Watcher(QueueBus(self.root)).step()
        self.assertEqual(outcome.action, Watcher.DORMANT)
        self.assertEqual(outcome.detail, "queue is empty")

    def test_an_authorized_step_skips_empty_repos_and_claims(self):
        os.makedirs(self.path("aaa"))
        bus = QueueBus(self.root)
        bus.enqueue("bbb", "item-1", "real work")
        outcome = Watcher(bus, launcher=None).step(authorized=True)
        self.assertEqual(outcome.action, Watcher.CLAIMED)
        self.assertEqual(outcome.repo, "bbb")
        self.assertEqual(bus.item("bbb", "item-1").state, RUNNING)

    def test_a_refused_claim_reports_dormant_with_the_reason(self):
        class RefusingBus:
            def repos(self):
                return ["harness"]

            def items(self, repo, state=None):
                return [SimpleNamespace(id="item-1")]

            def claim(self, repo, item_id, owner):
                raise QueueError("held by another watcher")

        outcome = Watcher(RefusingBus()).step(authorized=True)
        self.assertEqual(outcome.action, Watcher.DORMANT)
        self.assertIn("claim refused: held by another watcher",
                      outcome.detail)


class QueueCliTests(TempTree):

    def _run(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = queue_run_cli(args)
        return code, out.getvalue(), err.getvalue()

    def test_status_for_one_repo_prints_its_counts(self):
        bus = QueueBus(self.root)
        bus.enqueue("harness", "item-1", "real work")
        code, out, _ = self._run(
            ["status", "--root", self.root, "--repo", "harness"])
        self.assertEqual(code, 0)
        self.assertIn("harness: 1 items (queued=1)", out)

    def test_status_json_prints_the_payload(self):
        bus = QueueBus(self.root)
        bus.enqueue("harness", "item-1", "real work")
        code, out, _ = self._run(["status", "--root", self.root, "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload[0]["repo"], "harness")
        self.assertEqual(payload[0]["total"], 1)

    def test_status_on_an_empty_root_says_so(self):
        code, out, _ = self._run(["status", "--root", self.root])
        self.assertEqual(code, 0)
        self.assertIn("empty (no repos)", out)

    def test_enqueue_with_an_object_payload_stores_it(self):
        code, out, _ = self._run([
            "enqueue", "--root", self.root, "--repo", "harness",
            "--id", "item-9", "--summary", "do it",
            "--payload", '{"k": 1}'])
        self.assertEqual(code, 0)
        self.assertIn("enqueued harness/item-9 as queued", out)
        item = QueueBus(self.root).item("harness", "item-9")
        self.assertEqual(item.payload, {"k": 1})

    def test_enqueue_with_a_non_object_payload_is_fatal(self):
        code, _, err = self._run([
            "enqueue", "--root", self.root, "--repo", "harness",
            "--id", "item-9", "--summary", "do it", "--payload", '["x"]'])
        self.assertEqual(code, 1)
        self.assertIn("[FATAL] queue:", err)
        self.assertIn("--payload must be a JSON object", err)

    def test_watch_without_authorization_holds_with_exit_3(self):
        bus = QueueBus(self.root)
        bus.enqueue("harness", "item-1", "real work")
        code, out, _ = self._run(
            ["watch", "--root", self.root, "--steps", "1"])
        self.assertEqual(code, 3)
        self.assertIn("supervised_hold", out)

    def test_the_top_level_cli_intercepts_the_queue_face(self):
        bus = QueueBus(self.root)
        bus.enqueue("harness", "item-1", "real work")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli_main(["queue", "status", "--root", self.root])
        self.assertEqual(code, 0)
        self.assertIn("harness: 1 items", out.getvalue())


# --------------------------------------------------------------- MCP edges

class _RecordingLedger:
    def __init__(self):
        self.events = []

    def append(self, *args, **kwargs):
        self.events.append((args, kwargs))


def _bare_server(**kwargs):
    kwargs.setdefault("transport", None)
    kwargs.setdefault("api_key", "sk-test")
    kwargs.setdefault("governor", None)
    kwargs.setdefault("ledger", _RecordingLedger())
    kwargs.setdefault("router", None)
    kwargs.setdefault("engine", None)
    return McpServer(**kwargs)


class McpLogShapeTests(TempTree):

    def setUp(self):
        super().setUp()
        self.log = Path(self.path("mcp.jsonl"))
        self.server = _bare_server(mcp_log_path=str(self.log))

    def _records(self):
        return [json.loads(line) for line
                in self.log.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    def test_a_non_dict_params_still_logs_with_no_tool_name(self):
        import time
        self.server._log_tool_call(
            {"params": "not-a-dict"}, {"result": {"isError": False}},
            time.monotonic())
        records = self._records()
        self.assertEqual(len(records), 1)
        self.assertIsNone(records[0]["tool"])
        self.assertTrue(records[0]["ok"])

    def test_an_error_response_logs_its_message(self):
        import time
        self.server._log_tool_call(
            {"params": {"name": "probe", "arguments": {}}},
            {"error": {"message": "backend refused"}}, time.monotonic())
        records = self._records()
        self.assertFalse(records[0]["ok"])
        self.assertEqual(records[0]["error"], "backend refused")

    def test_error_text_prefers_the_first_content_block(self):
        self.assertEqual(
            McpServer._error_text(
                {"content": [{"text": "boom"}], "errorKind": "other"}),
            "boom")

    def test_error_text_falls_back_to_the_error_kind(self):
        self.assertEqual(
            McpServer._error_text({"content": [], "errorKind": "denied"}),
            "denied")

    def test_error_text_falls_back_to_a_plain_refusal(self):
        self.assertEqual(McpServer._error_text({}), "tool failed")
        self.assertEqual(
            McpServer._error_text({"content": [{"nope": 1}]}),
            "tool failed")


class McpFlushRunTests(TempTree):

    def test_flush_run_needs_exactly_one_source(self):
        server = _bare_server()
        with self.assertRaises(HarnessError) as caught:
            server._invoke("flush_run", {"campaign": "a",
                                         "canary_suite": "b"})
        self.assertIn("exactly one of campaign or canary_suite",
                      str(caught.exception))
        with self.assertRaises(HarnessError):
            server._invoke("flush_run", {})

    def test_flush_run_without_write_authority_is_refused_and_ledgered(self):
        ledger = _RecordingLedger()
        server = _bare_server(ledger=ledger)
        with self.assertRaises(HarnessError) as caught:
            server._invoke("flush_run", {"canary_suite": "suite.json"})
        self.assertIn("allow_write", str(caught.exception))
        self.assertEqual(len(ledger.events), 1)
        self.assertEqual(ledger.events[0][0][0], "trust_gate")

    def test_flush_run_defaults_the_canary_ledger_next_to_the_suite(self):
        suite = Path(self.path("suite.json"))
        suite.write_text("{}", encoding="utf-8")
        server = _bare_server(allow_write=True,
                              allowed_roots=[self.root])
        seen = {}

        def fake_run(suite_path, ledger_path=None):
            seen["suite"] = suite_path
            seen["ledger"] = ledger_path
            return {"ran": True}

        with mock.patch("harness.mcp.run_canary_suite_file", fake_run):
            result = server._invoke("flush_run", {
                "canary_suite": str(suite), "root": self.root,
                "allow_write": True})
        self.assertEqual(result, {"ran": True})
        self.assertEqual(seen["suite"], str(suite))
        self.assertEqual(seen["ledger"], f"{suite}.canary.events.jsonl")


# --------------------------------------------------------------- repo scope

class RepoScopeEdgeTests(TempTree):

    def test_no_root_means_an_empty_scope(self):
        scope = audit_scope(None)
        self.assertIsNone(scope["root"])
        self.assertEqual(scope["included"], [])
        self.assertEqual(scope["excluded_count"], 0)

    def test_a_file_as_root_scans_to_an_empty_scope(self):
        target = Path(self.path("plain.txt"))
        target.write_text("x", encoding="utf-8")
        scope = audit_scope(target)
        self.assertEqual(scope["included"], [])
        self.assertEqual(scope["excluded_count"], 0)

    def test_a_directory_escaping_the_root_is_never_walked(self):
        Path(self.path("keep.py")).write_text("x = 1\n", encoding="utf-8")
        with mock.patch("harness.osal.is_within", return_value=False):
            scope = audit_scope(Path(self.root))
        self.assertEqual(scope["included"], [])
        self.assertEqual(scope["excluded_count"], 0)

    def test_a_symlinked_file_is_excluded_as_a_link(self):
        real = Path(self.path("real.py"))
        real.write_text("x = 1\n", encoding="utf-8")
        os.symlink(real, self.path("alias.py"))
        scope = audit_scope(Path(self.root))
        reasons = {entry["path"]: entry["reason"]
                   for entry in scope["excluded"]}
        self.assertEqual(reasons.get("alias.py"), "link_or_reparse")
        self.assertEqual(scope["excluded_count"], 1)
        self.assertEqual(scope["included"], ["real.py"])

    def test_a_skipped_suffix_is_excluded_by_name(self):
        Path(self.path("debug.log")).write_text("noise", encoding="utf-8")
        Path(self.path("keep.py")).write_text("x = 1\n", encoding="utf-8")
        scope = audit_scope(Path(self.root))
        reasons = {entry["path"]: entry["reason"]
                   for entry in scope["excluded"]}
        self.assertEqual(reasons.get("debug.log"), "skipped_suffix")
        self.assertEqual(scope["included"], ["keep.py"])

    def test_an_entry_that_vanishes_mid_scan_is_skipped(self):
        Path(self.path("keep.py")).write_text("x = 1\n", encoding="utf-8")
        real_scandir = os.scandir

        class BrokenEntry:
            name = "aaa-broken.py"

            def __init__(self, path):
                self.path = path

            def stat(self, follow_symlinks=True):
                raise OSError("vanished mid-scan")

            def is_symlink(self):
                return False

            def is_dir(self, follow_symlinks=True):
                return False

        class Scan:
            def __init__(self, entries):
                self._entries = entries

            def __enter__(self):
                return iter(self._entries)

            def __exit__(self, *exc):
                return False

        def fake_scandir(path):
            entries = list(real_scandir(path))
            broken = BrokenEntry(os.path.join(str(path), "aaa-broken.py"))
            return Scan([broken] + entries)

        with mock.patch.object(os, "scandir", fake_scandir):
            scope = audit_scope(Path(self.root))
        self.assertEqual(scope["included"], ["keep.py"])


# --------------------------------------------------------------- chat routes

class ChatMetaRouteTests(unittest.TestCase):

    def test_a_meta_model_dispatches_with_the_threaded_key(self):
        seen = {}

        def fake_meta(transport, key, model, messages, max_tokens,
                      governor=None):
            seen.update(key=key, model=model)
            return "meta-response"

        with mock.patch.object(chat_mod, "_chat_meta", fake_meta):
            result = chat_mod.chat(None, "api-key-1", "muse-spark-1.1",
                                   [], 16)
        self.assertEqual(result, "meta-response")
        self.assertEqual(seen, {"key": "api-key-1",
                                "model": "muse-spark-1.1"})

    def test_a_meta_model_without_any_key_refuses(self):
        with mock.patch.object(chat_mod, "resolve_meta_key",
                               return_value=""):
            with self.assertRaises(HarnessError) as caught:
                chat_mod.chat(None, None, "muse-spark-1.1", [], 16)
        self.assertIn("Meta key missing", str(caught.exception))

    def test_a_fireworks_route_threads_the_settings_key(self):
        from harness.fireworks import Route
        route = Route("fireworks", "accounts/fireworks/models/x",
                      "accounts/fireworks/models/x")
        seen = {}

        def fake_fireworks(transport, key, model, messages, max_tokens,
                           governor=None):
            seen.update(key=key, model=model)
            return "fw-response"

        settings = SimpleNamespace(fireworks_enabled=True,
                                   fireworks_key="fw-from-settings",
                                   meta_key=None)
        with mock.patch("harness.routing_table.route_for_model",
                        return_value=route), \
                mock.patch.object(chat_mod, "_chat_fireworks",
                                  fake_fireworks):
            result = chat_mod.chat_routed(
                None, "api-key-1", "accounts/fireworks/models/x", [], 16,
                settings=settings)
        self.assertEqual(result, "fw-response")
        self.assertEqual(seen["key"], "fw-from-settings")


# --------------------------------------------------------------- small sets

class EndpointPricingEdgeTests(unittest.TestCase):

    def test_none_overrides_normalize_to_no_steps(self):
        self.assertEqual(_normalize_overrides(None), ())

    def test_a_non_list_overrides_value_is_refused(self):
        with self.assertRaises(HarnessError) as caught:
            _normalize_overrides("not-a-list")
        self.assertIn("must be a list of price-step objects",
                      str(caught.exception))

    def test_a_feed_with_non_list_overrides_is_refused(self):
        payload = {"data": [{
            "name": "provider", "pricing": {
                "prompt": 1, "completion": 2, "overrides": "nope"}}]}
        with self.assertRaises(HarnessError) as caught:
            _parse_endpoints("model-x", payload)
        self.assertIn("non-list overrides field", str(caught.exception))


class McpLogEdgeTests(TempTree):

    def test_scrub_recurses_through_lists_and_tuples(self):
        cleaned = scrub(["plain", {"api_key": "shh"}, ("nested",)])
        self.assertEqual(cleaned[0], "plain")
        self.assertEqual(cleaned[1]["api_key"], "[REDACTED]")
        self.assertEqual(cleaned[2], ["nested"])

    def test_log_tool_call_without_a_path_is_a_no_op(self):
        self.assertIsNone(log_tool_call(
            "", tool="probe", args={}, ok=True, duration_ms=3))
        self.assertIsNone(log_tool_call(
            None, tool="probe", args={}, ok=True, duration_ms=3))

    def test_an_unwritable_log_path_is_swallowed(self):
        blocker = Path(self.path("blocker"))
        blocker.write_text("a file, not a directory", encoding="utf-8")
        target = blocker / "nested" / "mcp.jsonl"
        self.assertIsNone(log_tool_call(
            str(target), tool="probe", args={}, ok=True, duration_ms=3))
        self.assertFalse(target.exists())


class RestartStateStoreEdgeTests(TempTree):

    def test_unreadable_state_file_is_refused(self):
        target = Path(self.path("state.json"))
        target.write_text("{not json", encoding="utf-8")
        with self.assertRaises(HarnessError) as caught:
            RestartStateStore(str(target))
        self.assertIn("restart state is unreadable", str(caught.exception))

    def test_a_non_object_state_file_is_refused(self):
        target = Path(self.path("state.json"))
        target.write_text("[1, 2]", encoding="utf-8")
        with self.assertRaises(HarnessError) as caught:
            RestartStateStore(str(target))
        self.assertIn("restart state must be an object",
                      str(caught.exception))

    def test_a_failing_event_sink_does_not_lose_the_state(self):
        def explode(*args, **kwargs):
            raise RuntimeError("sink down")

        store = RestartStateStore(event=explode)
        state = {"event_key": "k1", "terminal": True, "request_id": "r-1",
                 "status": "failed", "assignment_hash": "h",
                 "reasons": ["boom"]}
        result = store._remember(state)
        self.assertFalse(result["event_emitted"])
        self.assertFalse(result["deduplicated"])
        self.assertEqual(store.last("r-1")["event_key"], "k1")


class OverlayEdgeTests(TempTree):

    def _tree(self, name):
        folder = Path(self.path(name))
        folder.mkdir(parents=True)
        return folder

    def test_a_missing_base_path_is_refused(self):
        overlay = self._tree("overlay")
        with self.assertRaises(HarnessError) as caught:
            run_overlay("", str(overlay), ["true"])
        self.assertIn("must be a directory path", str(caught.exception))

    def test_a_symlinked_directory_in_the_overlay_is_refused(self):
        base = self._tree("base")
        overlay = self._tree("overlay")
        real = self._tree("real")
        (real / "f.txt").write_text("x", encoding="utf-8")
        os.symlink(real, overlay / "linked")
        with self.assertRaises(HarnessError) as caught:
            run_overlay(str(base), str(overlay), ["true"])
        self.assertIn("is a symlink", str(caught.exception))

    def test_a_file_over_a_base_directory_is_refused(self):
        base = self._tree("base")
        (base / "thing").mkdir()
        overlay = self._tree("overlay")
        (overlay / "thing").write_text("file", encoding="utf-8")
        with self.assertRaises(HarnessError) as caught:
            run_overlay(str(base), str(overlay), ["true"])
        self.assertIn("is a directory in the base and a file in the "
                      "overlay", str(caught.exception))

    def test_a_cwd_that_names_a_file_is_refused(self):
        base = self._tree("base")
        (base / "afile").write_text("x", encoding="utf-8")
        overlay = self._tree("overlay")
        with self.assertRaises(HarnessError) as caught:
            run_overlay(str(base), str(overlay),
                        [sys.executable, "-c", "pass"], cwd="afile")
        self.assertIn("is not a directory in the composed tree",
                      str(caught.exception))


class SmallPredicateTests(unittest.TestCase):

    def test_an_empty_prompt_is_not_a_read_only_audit(self):
        self.assertFalse(is_read_only_audit_request(""))
        self.assertFalse(is_read_only_audit_request(None))

    def test_a_truncated_scope_summary_says_it_is_not_complete(self):
        scope = {"included_count": 3, "excluded_count": 1,
                 "truncated": True}
        estimate = {"input_tokens_with_buffer": 42,
                    "worst_case_cost_usd": 0.25}
        text = _summary(scope, estimate, "ok")
        self.assertIn("listing capped - NOT a complete workspace audit",
                      text)
        self.assertIn("3 files included, 1 excluded", text)


# ------------------------------------------------------- task lifecycle lane

class _WorkspaceAuditHandler(TaskLaneLifecycleMixin):
    pass


class WorkspaceAuditLifecycleEdgeTests(TempTree):

    def setUp(self):
        super().setUp()
        self.handler = _WorkspaceAuditHandler()
        self.handler.lifecycle = LifecycleCoordinator(
            history_dir=self.root)
        self.adapter = adapter_for(self.handler.lifecycle, "audit")

    def test_a_completed_saved_result_is_replayed_verbatim(self):
        state = self.adapter.begin("sess-a", "audit the workspace")
        saved = {"status": "approval_required", "response": "priced"}
        state["data"]["result"] = saved
        state["status"] = "complete"
        result = self.handler._handle_workspace_audit(
            "audit the workspace", "sess-a", lifecycle_state=state)
        self.assertEqual(result, saved)

    def test_cancellation_interrupts_the_state_and_raises(self):
        state = self.adapter.begin("sess-b", "audit the workspace")
        with self.assertRaises(ToolCancelled):
            self.handler._handle_workspace_audit(
                "audit the workspace", "sess-b",
                cancel_check=lambda: True, lifecycle_state=state)
        self.assertEqual(state["status"], "interrupted")


# ------------------------------------------------------- hourglass routing

def _noul(value):
    return {"type": "noul", "noul": value}


class _FakePolicy:
    def __init__(self, assessments):
        self.assessments = list(assessments)
        self.calls = []

    def evaluate_answer(self, prompt, answer, context, **kwargs):
        self.calls.append((prompt, answer, context, kwargs))
        sufficient, iteration, plan = self.assessments.pop(0)
        result = JevEvaluationResult(
            "pass" if sufficient >= 0.99 and not iteration else "fail",
            0.0, sufficient,
            {"answer_sufficient": _noul(sufficient),
             "iteration_required": _noul(1.0 if iteration else 0.0),
             "plan_required": _noul(1.0 if plan else 0.0)},
            ["test assessment"], model="jev-test")
        return result, {
            "capability": "answer", "pack_version": "answer-sufficiency-v1",
            "native": True, "answer_sufficient": sufficient,
            "iteration_required": iteration, "plan_required": plan,
            "cost": 0.001, "input_tokens": 10, "output_tokens": 1,
        }


class HourglassNoToolsRouteTests(TempTree):

    def test_a_no_tools_prompt_overrides_an_advisory_web_route(self):
        from harness.agent import AutonomousAgent
        from harness.config import load_settings

        agent = AutonomousAgent(settings=load_settings(),
                                root_dir=Path(self.root),
                                history_dir=Path(self.root))
        policy = _FakePolicy([(0.90, False, False)])
        with mock.patch("harness.hourglass_lifecycle.policy_for",
                        return_value=policy), \
                mock.patch("harness.hourglass_lifecycle.governor_for",
                           return_value=(None, mock.MagicMock())), \
                mock.patch("harness.hourglass_lifecycle.ledger_for",
                           return_value=mock.MagicMock()), \
                mock.patch.object(agent, "_hourglass_answer_once",
                                  return_value={"model": "m1",
                                                "answer": "plain answer",
                                                "cost": 0.001}), \
                mock.patch.object(
                    agent, "_gather_web_context",
                    side_effect=AssertionError(
                        "a no-tools run must not search")), \
                mock.patch("harness.hourglass_lifecycle.save_chat_turn"):
            result = agent.run_hourglass_request(
                "Explain how a bill becomes law, and do not use any tools",
                request_preflight={
                    "source": "jev_request_workflow",
                    "choice": "web-research", "native": True, "cost": 0.0,
                })
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["web_used"])


if __name__ == "__main__":
    unittest.main()
