"""The tier chain: CLI -> MCP -> DOM -> pixels, proven rather than asserted.

The tests here are deliberately adversarial about the ordering. A test that
registers a screen source and checks the capture came from a CLI source
proves very little: it proves the loop tried the CLI first on *that* run, not
that a screen capture was unreachable. So the source that would violate the
ordering raises if it is ever called, and the assertions are about
reachability rather than about which name came back.

That distinction is the whole claim. "Pixels are last resort" is a statement
about cost and reliability that an operator has to be able to rely on, and a
statement like that should be enforced by a type or a filter rather than by
the shape of a ``for`` loop.
"""
import os
import shutil
import tempfile
import unittest

from driver_core import osal
from driver_core.config import load_settings
from driver_core.driver import Driver
from driver_core.errors import PerceptionUnavailable
from driver_core.ev import FakeJev, action_answer
from driver_core.extractors import (
    ExtractorPool, VisionExtractor,
)
from driver_core.perception import (
    CLI, DOM, GUI, MCP, SOURCE_ORDER, STRUCTURED_CLASSES, TARGET_CLASSES,
    Capture, CliSource, McpSource, ScreenSource, StructuredSource,
    Target, as_target, read_document, select_capture,
)

SCREEN_STATE = {"window_title": "report - editor", "foreground_app": "editor",
                "error_dialog_present": False}
CLI_STATE = {"exit_code": 0, "stdout": "", "stderr": ""}


class ExplodingScreen(ScreenSource):
    """A screen source that fails the test if it is ever consulted.

    Not a counting fake -- a *refusing* one. A counter would still let the
    call happen and then report it, which is exactly the thing the ordering
    is supposed to make impossible.
    """

    def capture(self, target=None):
        raise AssertionError(
            "the vision tier was reached for a structured target class")


def _source(name, payload, **kwargs):
    return StructuredSource(name, lambda target: dict(payload), **kwargs)


class TargetClassTests(unittest.TestCase):

    def test_the_four_declared_classes_are_exactly_the_four_tiers(self):
        self.assertEqual(TARGET_CLASSES, ("cli", "mcp", "dom", "gui"))
        self.assertEqual(set(STRUCTURED_CLASSES) | {"gui"},
                         set(TARGET_CLASSES))

    def test_three_of_four_are_declared_as_not_needing_pixels(self):
        self.assertEqual(len(STRUCTURED_CLASSES), 3)
        self.assertNotIn(GUI, STRUCTURED_CLASSES)

    def test_an_unknown_class_is_refused_by_name(self):
        with self.assertRaises(PerceptionUnavailable) as ctx:
            Target("app", "telepathy")
        self.assertIn("unknown target class", str(ctx.exception))

    def test_a_bare_string_is_accepted_and_is_explicitly_weaker(self):
        """Compatibility, stated plainly rather than implied."""
        target = as_target("my-app")
        self.assertIsInstance(target, Target)
        self.assertFalse(target.declared)
        self.assertFalse(target.structured)

    def test_a_declared_structured_class_reports_structured(self):
        self.assertTrue(Target("x", DOM).structured)
        self.assertFalse(Target("x", GUI).structured)


class ReachabilityTests(unittest.TestCase):
    """The ordering as an unreachable-code property."""

    def test_a_dom_target_can_never_reach_the_screen_source(self):
        """The core guarantee, with a source that refuses to be called."""
        source = _source(DOM, {"window_title": "x"})
        capture = select_capture(Target("app", DOM), [source, ExplodingScreen()])
        self.assertEqual(capture.source, DOM)

    def test_the_same_holds_for_cli_and_mcp(self):
        for cls in (CLI, MCP):
            with self.subTest(cls=cls):
                source = _source(cls, {"exit_code": 0})
                capture = select_capture(
                    Target("app", cls), [source, ExplodingScreen()])
                self.assertEqual(capture.source, cls)

    def test_a_dom_target_reports_the_screen_tier_as_excluded_not_declined(self):
        """The refusal distinguishes 'wrong class declared' from 'nothing
        to see', because the remedies are different."""
        with self.assertRaises(PerceptionUnavailable) as ctx:
            select_capture(Target("app", DOM),
                           [ExplodingScreen()])
        message = str(ctx.exception)
        self.assertIn("was therefore not reached", message)
        self.assertIn("declared 'dom'", message)

    def test_the_screen_tier_is_reachable_for_a_gui_target(self):
        """Otherwise the guarantee would be achieved by simply removing it."""
        source = ExplodingScreen()
        source.capture = lambda target=None: Capture(  # noqa: E731
            GUI, target, "pixels", fingerprint="abc")
        capture = select_capture(Target("app", GUI), [source])
        self.assertEqual(capture.source, GUI)
        self.assertEqual(capture.payload, "pixels")

    def test_an_undeclared_target_still_falls_back_to_pixels(self):
        """The compatibility path is unchanged: bare string, pixels last."""
        screen = ExplodingScreen()
        screen.capture = lambda target=None: Capture(  # noqa: E731
            GUI, target, "pixels", fingerprint="abc")
        capture = select_capture("app", [_source(DOM, None),
                                         _source(CLI, None), screen])
        self.assertEqual(capture.source, GUI)

    def test_a_source_declaring_an_unknown_class_is_refused(self):
        with self.assertRaises(PerceptionUnavailable) as ctx:
            _source(DOM, {}, serves=("telepathy",))
        self.assertIn("unknown target class", str(ctx.exception))

    def test_a_structured_source_serves_only_its_own_class_by_default(self):
        source = _source(DOM, {"a": 1})
        self.assertTrue(source.can_serve(Target("x", DOM)))
        self.assertFalse(source.can_serve(Target("x", CLI)))


class ReaderContractTests(unittest.TestCase):
    """A reader is handed the ref, never a Target object.

    Regression test for an integration break found by running PR #1's live
    run against this branch: a reader that passes its argument into argv or
    ``open()`` suddenly received a ``Target``, and failed with a TypeError
    from inside the caller's own code. The class is bookkeeping for the
    chain; a reader wants the thing being observed.
    """

    def test_a_reader_receives_a_plain_string(self):
        seen = []
        source = StructuredSource(CLI, lambda t: seen.append(t) or {"a": 1})
        select_capture(Target("my-app", CLI), [source])
        self.assertEqual(seen, ["my-app"])
        self.assertIsInstance(seen[0], str)

    def test_a_reader_can_use_the_ref_as_a_path_or_argument(self):
        directory = tempfile.mkdtemp(prefix="driver-reader-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "target.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("hello")
        source = StructuredSource(CLI, lambda ref: {"path": ref})
        capture = select_capture(Target(path, CLI), [source])
        self.assertEqual(capture.payload["path"], path)


class FallThroughTests(unittest.TestCase):
    """Order within the structured tiers, and what happens when one declines."""

    def test_tiers_are_tried_in_the_declared_order(self):
        """The first two decline, so all three are exercised before the
        third answers -- which is the only way to observe the order."""
        seen = []

        def recorder(name, answer):
            def reader(target):
                seen.append(name)
                return answer
            return reader

        sources = [
            StructuredSource(DOM, recorder(DOM, {"window_title": "x"})),
            StructuredSource(MCP, recorder(MCP, None)),
            StructuredSource(CLI, recorder(CLI, None)),
        ]
        capture = select_capture("app", sources)
        self.assertEqual(seen, [CLI, MCP, DOM])
        self.assertEqual(capture.source, DOM)

    def test_prefer_reorders_within_the_structured_set_only(self):
        seen = []

        def recorder(name, answer):
            def reader(target):
                seen.append(name)
                return answer
            return reader

        screen = ExplodingScreen()
        screen.capture = lambda target=None: Capture(  # noqa: E731
            GUI, target, "pixels")
        sources = [
            StructuredSource(CLI, recorder(CLI, None)),
            StructuredSource(DOM, recorder(DOM, {"window_title": "x"})),
            screen,
        ]
        capture = select_capture("app", sources, prefer=(DOM,))
        self.assertEqual(seen, [DOM])
        self.assertEqual(capture.source, DOM)

    def test_prefer_cannot_promote_pixels_past_a_structured_source(self):
        """The ordering's one non-negotiable, tested directly."""
        screen = ExplodingScreen()
        screen.capture = lambda target=None: Capture(  # noqa: E731
            GUI, target, "pixels")
        sources = [_source(CLI, {"exit_code": 0}), screen]
        capture = select_capture("app", sources, prefer=(GUI,))
        self.assertEqual(capture.source, CLI)

    def test_a_failing_reader_declines_and_the_refusal_names_the_reason(self):
        """A reader that raises must not stop the chain, and must not be
        swallowed either -- the operator needs to know which tier broke."""

        def broken(target):
            raise RuntimeError("the reader exploded")

        with self.assertRaises(PerceptionUnavailable) as ctx:
            select_capture("app", [StructuredSource(CLI, broken)])
        self.assertIn("reader failed: the reader exploded", str(ctx.exception))


class PoolSelectionTests(unittest.TestCase):
    """The extraction side of the same guarantee."""

    def _pool(self, cls, extractors):
        return ExtractorPool(extractors, serves=(cls,))

    def test_a_declared_pool_serves_only_its_class(self):
        pool = self._pool(CLI, [])
        self.assertFalse(pool.refuses_class(CLI))
        self.assertTrue(pool.refuses_class(DOM))
        self.assertTrue(pool.refuses_class(GUI))

    def test_an_undeclared_pool_still_serves_everything(self):
        pool = ExtractorPool([])
        self.assertFalse(pool.refuses_class(GUI))
        self.assertFalse(pool.refuses_class(CLI))

    def test_for_target_returns_none_for_a_refused_class(self):
        pool = self._pool(GUI, [])
        self.assertIsNone(pool.for_target(Target("x", DOM)))
        self.assertIsNotNone(pool.for_target(Target("x", GUI)))

    def test_a_vision_pool_is_not_a_candidate_for_a_dom_target(self):
        """No call, no billing, no opinion nobody asked for."""
        pool = self._pool(GUI, [])
        self.assertIsNone(pool.for_target(Target("app", DOM)))

    def test_the_vision_extractor_declares_it_serves_only_gui(self):
        from driver_core.audit import MemoryAuditLog
        from driver_core.budget import Budget
        extractor = VisionExtractor(
            "slot", load_settings(env={"DRIVER_JEV_API_KEY": "k"}),
            budget=Budget(1.0, step_ceiling_usd=1.0), audit=MemoryAuditLog())
        self.assertEqual(extractor.serves, (GUI,))
        self.assertFalse(extractor.deterministic)


class DriverPoolSelectionTests(unittest.TestCase):
    """The driver picks the pool by class, so the rule holds end to end."""

    def _driver(self, pools, sources):
        settings = load_settings(env={}, quorum=2, min_agreement=1.0,
                                confidence_threshold=0.7, run_ceiling_usd=1.0,
                                step_ceiling_usd=1.0, dry_run=True)
        from driver_core.audit import MemoryAuditLog
        from driver_core.budget import Budget
        return Driver(settings=settings, budget=Budget(1.0, step_ceiling_usd=1.0),
                      audit=MemoryAuditLog(), pool=ExtractorPool([]),
                      pools=pools, sources=sources,
                      jev=FakeJev(action_answer("observe", confidence=0.99)))

    def test_a_dom_target_uses_the_dom_pool_not_the_vision_pool(self):
        calls = []

        class Recorder(ExtractorPool):
            def run(self, capture, schema):
                calls.append(self.serves)
                return []

        pools = {DOM: Recorder([], serves=(DOM,)),
                 GUI: Recorder([], serves=(GUI,))}
        driver = self._driver(pools, [_source(DOM, {"window_title": "x"})])
        driver.step(Target("app", DOM))
        self.assertEqual(calls, [(DOM,)])

    def test_an_undeclared_target_falls_back_to_the_default_pool(self):
        calls = []

        class Recorder(ExtractorPool):
            def run(self, capture, schema):
                calls.append("default")
                return []

        driver = self._driver({}, [_source(CLI, {"exit_code": 0})])
        driver.pool = Recorder([])
        driver.step("app")
        self.assertEqual(calls, ["default"])


class OsalInputTests(unittest.TestCase):
    """``input_text`` really works now.

    Both of these are regression tests for a parameter that had never been
    exercised: it raised ``ValueError`` on every platform because the call
    passed ``stdin=PIPE`` *and* ``input=`` to :func:`subprocess.run`, which
    rejects both together. The live tier run found it the moment an MCP
    client needed to talk to a server.
    """

    ECHO = ("import sys; data = sys.stdin.read(); "
            "sys.stdout.write('got:' + data)")

    def test_a_child_really_receives_stdin(self):
        result = osal.run(["python", "-c", self.ECHO], input_text="hello")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.stdout, "got:hello")

    def test_a_child_that_ignores_stdin_still_works(self):
        result = osal.run(["python", "-c", "print('no stdin needed')"])
        self.assertTrue(result.ok)
        self.assertIn("no stdin needed", result.stdout)

    def test_a_non_string_input_is_refused_by_type(self):
        with self.assertRaises(TypeError) as ctx:
            osal.run(["python", "-c", "pass"], input_text=b"bytes")
        self.assertIn("must be str or None", str(ctx.exception))

    def test_the_scheme_restriction_holds(self):
        """A URL-shaped hole must not become a disk-read primitive."""
        for url in ("file:///etc/passwd", "ftp://example.invalid/x"):
            with self.assertRaises(Exception) as ctx:
                osal.http_get(url)
            self.assertIn("refusing to fetch", str(ctx.exception))

    def test_a_url_with_no_host_is_refused(self):
        with self.assertRaises(Exception) as ctx:
            osal.http_get("http:///no-host")
        self.assertIn("no host", str(ctx.exception))


class McpParsingTests(unittest.TestCase):
    """The JSON-RPC reader, against the shapes real servers emit."""

    def test_the_last_well_formed_response_wins(self):
        from driver_core.perception import _last_reply
        stream = ("server ready on stdio\n"
                  '{"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n'
                  '{"jsonrpc":"2.0","id":2,"result":{"ok":false}}\n')
        self.assertEqual(_last_reply(stream)["id"], 2)

    def test_a_banner_only_stream_yields_no_reply(self):
        from driver_core.perception import _last_reply
        self.assertIsNone(_last_reply("starting up\nlistening\n"))

    def test_structured_content_is_preferred_over_text(self):
        from driver_core.perception import _tool_payload
        payload = _tool_payload({"content": [{"type": "text", "text": "x"}],
                                 "structuredContent": {"a": 1}})
        self.assertEqual(payload, {"a": 1})

    def test_a_text_only_result_is_refused_rather_than_parsed(self):
        from driver_core.perception import _tool_payload
        self.assertIsNone(_tool_payload({"content": [
            {"type": "text", "text": "exit_code: 0"}]}))
        self.assertIsNone(_tool_payload({}))
        self.assertIsNone(_tool_payload("not a mapping"))


class DocumentReadingTests(unittest.TestCase):
    """The DOM tier's parser, exercised without a network."""

    HTML = """
    <html><head><title>  Payroll Report </title>
    <style>.error { color: red }</style></head>
    <body><h1>Payroll Report</h1><p>Total &amp; deductions</p>
    <script>var error = "Save failed";</script></body></html>
    """

    def test_title_and_visible_text_are_extracted(self):
        payload = read_document(self.HTML)
        self.assertEqual(payload["window_title"], "Payroll Report")
        self.assertIn("deductions", payload["visible_text"])

    def test_script_and_style_bodies_are_dropped(self):
        """They contain the words a schema asks for most often, and leaving
        them in would make this tier disagree with the pixel tier."""
        payload = read_document(self.HTML)
        self.assertNotIn("Save failed", payload["visible_text"])
        self.assertNotIn("color: red", payload["visible_text"])

    def test_an_empty_document_yields_nothing_rather_than_inventing(self):
        self.assertEqual(read_document(""), {})

    def test_entities_are_decoded(self):
        self.assertIn("&", read_document("<p>a &amp; b</p>")["visible_text"])


class SourceConstructionTests(unittest.TestCase):
    """Refusals at construction, where a mistake is still free to correct."""

    def test_a_string_command_is_refused_rather_than_shelled(self):
        for factory in (CliSource, ):
            with self.assertRaises(PerceptionUnavailable) as ctx:
                factory("ls -la")
            self.assertIn("argv list", str(ctx.exception))

    def test_an_empty_command_is_refused(self):
        with self.assertRaises(PerceptionUnavailable):
            CliSource([])

    def test_mcp_requires_a_command(self):
        with self.assertRaises(PerceptionUnavailable):
            McpSource("not-a-list", "tool")

    def test_an_unknown_source_name_is_refused(self):
        with self.assertRaises(PerceptionUnavailable):
            StructuredSource("telepathy", lambda t: None)

    def test_source_order_declares_pixels_last(self):
        self.assertEqual(SOURCE_ORDER[-1], "screen")


class ScreenNeverLoggedTests(unittest.TestCase):
    """A capture must stay loggable, including its target class."""

    def test_a_target_object_never_reaches_the_audit_payload(self):
        """json.dumps would raise on it, at the worst possible moment."""
        capture = Capture(CLI, Target("app", CLI), {"exit_code": 0},
                          fingerprint="abc")
        summary = capture.summary()
        self.assertEqual(summary["target"], "app")
        self.assertIsInstance(summary["target"], str)

    def test_the_summary_carries_no_payload(self):
        capture = Capture(DOM, Target("app", DOM),
                          {"window_title": "Confidential Payroll"})
        self.assertNotIn("Confidential", str(capture.summary()))

    def test_the_fingerprint_is_stable_and_short(self):
        first = Capture(CLI, "a", {"x": 1}, fingerprint=osabp())
        second = Capture(CLI, "b", {"x": 1}, fingerprint=osabp())
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(len(first.fingerprint), 16)

    def test_the_capture_carries_the_class_so_the_pool_can_be_chosen(self):
        capture = Capture(DOM, Target("app", DOM), {"window_title": "x"})
        self.assertEqual(capture.target_class, DOM)
        self.assertIsNone(Capture(DOM, "app", {}).target_class)


def osabp():
    from driver_core.perception import fingerprint
    return fingerprint({"x": 1})


if __name__ == "__main__":
    unittest.main()
