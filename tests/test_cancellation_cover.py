"""Hermetic D12 coverage for the GUI-cancellation lane (#198).

Every test below executes added lines that the traced battery otherwise
never runs: cancellation propagation (``except ToolCancelled: raise``),
unknown-usage accounting, and the platform-specific osal boundaries. No
network, no operator config, no ambient credentials: transports are
fakes, children are fakes (except one real local ``sys.executable -c``
spawn that never touches the network).

NOTE (trace infra, not a test gap): ``harness/errors.py`` lines can never
appear in ``coverage_baseline.json``. The stdlib ``trace`` module caches
its ignore decision by module *basename*, and ``email/errors.py`` (pulled
in by ``importlib.metadata`` from ``harness/__init__``) always wins that
race, so ``harness/errors.py`` is systematically untraced. The
``TestErrorsCover`` tests below still pin the new exception API; they just
cannot move the D12 ratio.
"""
import sys
import unittest
from unittest import mock

from harness import osal
from harness.errors import HarnessError, ProviderUsageUnknown, ToolCancelled
from harness.events import current_task_id, task_context
from harness.executor import ConcurrentExecutor
from harness.dag import DAGNode, TaskDAG
from harness.spend import SpendGovernor
from tests._fake import FakeTransport


class TestErrorsCover(unittest.TestCase):
    """Pin the cancellation/unknown-usage exception API.

    (See module docstring: these lines are untraceable by the coverage
    ritual, so these tests guard behavior, not the D12 ratio.)
    """

    def test_tool_cancelled_defaults(self):
        exc = ToolCancelled()
        self.assertEqual(exc.known_cost, 0.0)
        self.assertFalse(exc.cost_accounted)
        self.assertFalse(exc.usage_unknown)

    def test_tool_cancelled_clamps_negative_cost(self):
        exc = ToolCancelled("stop", known_cost=-5, usage_unknown=True)
        self.assertEqual(exc.known_cost, 0.0)
        self.assertTrue(exc.usage_unknown)

    def test_tool_cancelled_add_known_cost(self):
        exc = ToolCancelled(known_cost=0.01)
        exc.add_known_cost(0.02)
        self.assertAlmostEqual(exc.known_cost, 0.03)
        exc.add_known_cost(None)
        exc.add_known_cost("bad")
        exc.add_known_cost([1])
        self.assertAlmostEqual(exc.known_cost, 0.03)

    def test_provider_usage_unknown_defaults(self):
        exc = ProviderUsageUnknown("lost")
        self.assertEqual(exc.known_cost, 0.0)
        self.assertEqual(exc.reserved_cost, 0.0)
        self.assertFalse(exc.cost_accounted)
        self.assertTrue(exc.usage_unknown)
        self.assertEqual(exc.kind, "provider_usage_unknown")

    def test_provider_usage_unknown_costs_and_add(self):
        exc = ProviderUsageUnknown("lost", known_cost=0.004,
                                   reserved_cost=0.02)
        self.assertAlmostEqual(exc.known_cost, 0.004)
        self.assertAlmostEqual(exc.reserved_cost, 0.02)
        exc.add_known_cost(0.001)
        self.assertAlmostEqual(exc.known_cost, 0.005)
        exc.add_known_cost(None)
        exc.add_known_cost("bad")
        self.assertAlmostEqual(exc.known_cost, 0.005)


class TestEventsCover(unittest.TestCase):
    def test_current_task_id_defaults_and_context(self):
        self.assertIsNone(current_task_id())
        with task_context("run-1"):
            self.assertEqual(current_task_id(), "run-1")
        self.assertIsNone(current_task_id())


class _FakeProc:
    """Scriptable stand-in for subprocess.Popen."""

    def __init__(self, polls, wait_results=None, kill_exc=None,
                 terminate_exc=None):
        self._polls = list(polls)
        self._waits = list(wait_results or [])
        self._kill_exc = kill_exc
        self._terminate_exc = terminate_exc
        self.returncode = None
        self.terminated = 0
        self.killed = 0

    def poll(self):
        if self._polls:
            self.returncode = self._polls.pop(0)
        return self.returncode

    def terminate(self):
        self.terminated += 1
        if self._terminate_exc is not None:
            raise self._terminate_exc

    def kill(self):
        self.killed += 1
        if self._kill_exc is not None:
            raise self._kill_exc

    def wait(self, timeout=None):
        if self._waits:
            result = self._waits.pop(0)
            if isinstance(result, BaseException):
                raise result
            self.returncode = result
            return result
        self.returncode = 0
        return 0


class TestOsalCover(unittest.TestCase):
    def test_spawn_rejects_str_argv(self):
        with self.assertRaisesRegex(osal.OsalError, "argv list"):
            osal.spawn_piped_process("not-a-list")

    def test_spawn_rejects_empty_argv(self):
        with self.assertRaisesRegex(osal.OsalError, "empty argv"):
            osal.spawn_piped_process([])

    def test_spawn_with_explicit_env(self):
        import os as _os
        child = osal.spawn_piped_process(
            [sys.executable, "-c", "pass"], env=dict(_os.environ))
        try:
            child.communicate(timeout=30)
            self.assertEqual(child.returncode, 0)
        finally:
            if child.poll() is None:
                osal.kill_and_reap(child)

    def test_terminate_already_reaped(self):
        self.assertEqual(osal.terminate_and_reap(_FakeProc([3])), 3)

    def test_terminate_graceful_wait(self):
        proc = _FakeProc([None], wait_results=[5])
        self.assertEqual(osal.terminate_and_reap(proc), 5)
        self.assertEqual(proc.terminated, 1)

    def test_terminate_race_after_terminate_error(self):
        proc = _FakeProc([None, 7], terminate_exc=OSError("gone"))
        self.assertEqual(osal.terminate_and_reap(proc), 7)

    def test_terminate_escalates_to_kill(self):
        import subprocess
        proc = _FakeProc([None, None, None],
                         wait_results=[subprocess.TimeoutExpired("x", 1), 0])
        self.assertEqual(osal.terminate_and_reap(proc), 0)
        self.assertEqual(proc.terminated, 1)
        self.assertEqual(proc.killed, 1)

    def test_terminate_kill_race(self):
        import subprocess
        proc = _FakeProc([None, 9],
                         wait_results=[subprocess.TimeoutExpired("x", 1)],
                         kill_exc=OSError("gone"))
        self.assertEqual(osal.terminate_and_reap(proc), 9)

    def test_terminate_unreapable(self):
        import subprocess
        proc = _FakeProc(
            [None, None, None],
            wait_results=[subprocess.TimeoutExpired("x", 1),
                          subprocess.TimeoutExpired("x", 1)])
        with self.assertRaisesRegex(osal.OsalError, "could not be reaped"):
            osal.terminate_and_reap(proc)

    def test_kill_already_reaped(self):
        self.assertEqual(osal.kill_and_reap(_FakeProc([4])), 4)

    def test_kill_waits(self):
        proc = _FakeProc([None], wait_results=[6])
        self.assertEqual(osal.kill_and_reap(proc), 6)
        self.assertEqual(proc.killed, 1)

    def test_kill_race(self):
        proc = _FakeProc([None, 8], kill_exc=OSError("gone"))
        self.assertEqual(osal.kill_and_reap(proc), 8)

    def test_kill_unreapable(self):
        import subprocess
        proc = _FakeProc([None],
                         wait_results=[subprocess.TimeoutExpired("x", 1)])
        with self.assertRaisesRegex(osal.OsalError, "could not be reaped"):
            osal.kill_and_reap(proc)


class TestExecutorCover(unittest.TestCase):
    def test_execute_files_propagates_cancellation(self):
        def worker(idx, path):
            raise ToolCancelled("stop")

        with self.assertRaises(ToolCancelled):
            ConcurrentExecutor(max_workers=2).execute_files(["a.py"], worker)

    def test_execute_dag_propagates_cancellation(self):
        dag = TaskDAG(nodes={
            "A": DAGNode(node_id="A", instruction="a",
                         target_files=("a.py",)),
            "B": DAGNode(node_id="B", instruction="b",
                         target_files=("b.py",)),
        })

        def worker(node, **kwargs):
            raise ToolCancelled("stop")

        with self.assertRaises(ToolCancelled):
            ConcurrentExecutor(max_workers=2).execute_dag(dag, worker)


class TestSpendCover(unittest.TestCase):
    def _gov(self):
        return SpendGovernor(FakeTransport(), "sk-test")

    def test_retain_unknown_nonpositive_is_noop(self):
        gov = self._gov()
        self.assertEqual(gov.retain_unknown(0, "x"), 0.0)
        self.assertEqual(gov.retain_unknown(None, "x"), 0.0)
        self.assertEqual(gov.snapshot().get("unknown_liability", 0.0), 0.0)

    def test_reclassify_unknown_token_raises(self):
        gov = self._gov()
        with self.assertRaisesRegex(HarnessError, "unknown reservation"):
            gov.reclassify_unknown(("nope", 1.0))


class TestWebCover(unittest.TestCase):
    def test_http_get_truncates_oversized_child_body(self):
        from harness import web

        big = b"y" * (web._MAX_BYTES + 25)

        def fake_request(method, url, **kwargs):
            return 200, big, None, url + "/final"

        with task_context("web-1", cancel_check=lambda: False):
            with mock.patch("harness._http.cancellable_request",
                             side_effect=fake_request):
                status, raw, final = web._http_get("http://127.0.0.1/x")
        self.assertEqual(status, 200)
        self.assertEqual(raw, big[:web._MAX_BYTES])
        self.assertTrue(final.endswith("/final"))

    def test_search_web_cancellation_emits_and_reraises(self):
        from harness import web

        with mock.patch.object(web, "_http_get",
                               side_effect=ToolCancelled("stop")):
            with self.assertRaises(ToolCancelled):
                web.search_web("latest news")

    def test_fetch_url_cancellation_emits_and_reraises(self):
        from harness import web

        with mock.patch.object(web, "_http_get",
                               side_effect=ToolCancelled("stop")):
            with self.assertRaises(ToolCancelled):
                web.fetch_url("https://127.0.0.1/x", {"127.0.0.1"})

    def test_gather_web_context_cancellation_propagates(self):
        from harness import web

        def fetch_boom(url, **kwargs):
            raise ToolCancelled("stop")

        with self.assertRaises(ToolCancelled):
            web.gather_web_context(
                "some prompt",
                allowed_hosts={"example.com"},
                find_urls_fn=lambda prompt: [],
                search_web_fn=lambda query: [{
                    "url": "https://example.com/x",
                    "title": "t", "snippet": "s"}],
                fetch_url_fn=fetch_boom)


class TestPanelCover(unittest.TestCase):
    def test_failed_slot_refills_from_pool(self):
        """The failure-refill submit path runs when a slot reports ok=False.

        ``_run_panel_slot`` never returns ``{"ok": False}`` on its own, so
        the refill contract is pinned by staging one failed future through
        the real wait loop.
        """
        import concurrent.futures as _futures
        from harness.panel import panel_judge
        from tests._fake import _gov, P1, P2, JUDGE, comp, m

        real_wait = _futures.wait
        calls = []

        def fake_future():
            fut = mock.Mock()
            fut.result.return_value = {"ok": False,
                                       "failure": {"model": P1,
                                                   "reason": "staged"}}
            return fut

        def staged_wait(futures, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                return ([fake_future()], set())
            return real_wait(futures, **kwargs)

        fake = FakeTransport(models=[m(P1), m(P2), m(JUDGE)],
                             posts=[comp("take one"), comp("take two"),
                                    comp("verdict: agree")])
        gov = _gov(fake)
        with mock.patch.object(_futures, "wait", side_effect=staged_wait):
            result = panel_judge(transport=fake, api_key="k", governor=gov,
                                 prompt="Q?", panel=[P1, P2], judge=JUDGE,
                                 max_panelists=1)
        self.assertEqual(len(calls), 2)
        self.assertTrue(result["panel_results"])
