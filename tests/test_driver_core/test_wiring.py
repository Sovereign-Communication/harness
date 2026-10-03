"""Declared sources become live ones, and nothing else does.

The perception tiers existed and were provable but unreachable: a default
``Driver()`` carried no sources, so ``driver-core step`` answered ``Tried:
none`` and every request ended in ``no_capture``. These tests pin the wiring
that closes that, and pin the two properties that make it safe to expose a
capability from configuration at all:

* **a source is off unless it was named** -- no tier is inferred from
  another setting's presence, and the vision tier has its own switch;
* **a declared command is tokenised, never shelled**.

Both matter because the alternative is a configuration string becoming code,
which is the one thing :mod:`driver_core.osal` exists to prevent.
"""
import os
import sys
import unittest
from unittest import mock

from driver_core.audit import MemoryAuditLog
from driver_core.config import ConfigError, load_settings
from driver_core.driver import Driver
from driver_core.errors import PerceptionUnavailable, SchemaError
from driver_core.extractors import ExtractorPool
from driver_core.perception import CLI, DOM, GUI, MCP, StructuredSource, Target
from driver_core.schema import validate_state
from driver_core.server import Service
from driver_core.states import (
    CLI_SCHEMA, DOM_SCHEMA, SCREEN_SCHEMA, WIRE_TARGETS, resolve_wire_target,
)
from driver_core.wiring import configured_pools, configured_sources, parse_argv


def _settings(**kwargs):
    return load_settings(env={}, **kwargs)


def _driver(**kwargs):
    kwargs.setdefault("settings", _settings())
    kwargs.setdefault("audit", MemoryAuditLog())
    return Driver(**kwargs)


class DefaultsTests(unittest.TestCase):
    """Nothing configured means nothing observed."""

    def test_a_default_driver_has_no_sources_and_no_vision(self):
        driver = _driver()
        self.assertEqual(driver.sources, [])
        self.assertEqual(configured_sources(driver.settings), [])
        self.assertIsNone(driver.screen)
        # The structured pools exist and are free; they hold no vision slot,
        # and with no configured source the class refuses anyway.
        self.assertEqual(sorted(driver.pools), [CLI, DOM, MCP])
        self.assertNotIn(GUI, driver.pools)

    def test_explicitly_empty_still_means_empty(self):
        """``()`` is a deliberate choice and must survive configuration."""
        driver = _driver(settings=_settings(cli_command="echo hi"),
                         sources=(), pools={}, screen=None)
        self.assertEqual(driver.sources, [])

    def test_configured_sources_become_live_sources(self):
        driver = _driver(settings=_settings(cli_command="echo hi"))
        self.assertEqual([s.name for s in driver.sources], ["cli"])

    def test_the_vision_tier_is_off_unless_its_own_switch_is_set(self):
        driver = _driver(settings=_settings(cli_command="echo hi"))
        self.assertIsNone(driver.screen)
        self.assertNotIn(GUI, driver.pools)
        self.assertNotIn("screen", [s.name for s in driver.sources])

    def test_a_declared_screen_source_is_consulted_once_not_twice(self):
        """The screen source is in the one list, and ``screen`` reads it back.

        ``sources`` and ``screen`` used to be two handles onto the same set,
        reconciled by a method that had to be called from everywhere. Now
        there is one list and ``screen`` is derived from it, so a step cannot
        capture the same pixels twice and ``/health`` cannot report the tier
        twice. The regression test is the count.
        """
        driver = _driver(settings=_settings(screen_enabled=True))
        self.assertEqual([s.name for s in driver.sources], ["screen"])
        self.assertIs(driver.screen, driver.sources[0])
        self.assertEqual(Service(driver=driver, token="t").health()["sources"],
                         ["screen"])

    def test_an_explicit_screen_source_joins_the_one_list(self):
        """A caller registering its own screen source still gets it, once."""
        screen = StructuredSource("screen", lambda ref: {"window_title": "x"},
                                  serves=("gui",))
        driver = _driver(settings=_settings(cli_command="echo hi"),
                         sources=[StructuredSource("cli", lambda ref: None)],
                         screen=screen)
        self.assertEqual([s.name for s in driver.sources], ["cli", "screen"])
        self.assertIs(driver.screen, screen)
        # Naming the same object in both places registers it once, not twice.
        both = _driver(sources=[screen], screen=screen)
        self.assertEqual(both.sources, [screen])


class TierIsolationTests(unittest.TestCase):
    """One switch, one tier. Never inferred from another setting."""

    def test_each_setting_enables_exactly_its_own_tier(self):
        cases = {
            "cli": _settings(cli_command="echo hi"),
            "mcp": _settings(mcp_command="srv --x", mcp_tool="status"),
            "dom": _settings(dom_url="https://example.invalid/r"),
            "screen": _settings(screen_enabled=True),
        }
        for expected, settings in cases.items():
            with self.subTest(tier=expected):
                self.assertEqual([s.name for s in configured_sources(settings)],
                                 [expected])

    def test_an_mcp_command_without_a_tool_enables_nothing(self):
        """Half a declaration is not a declaration."""
        self.assertEqual(configured_sources(_settings(mcp_command="srv --x")),
                         [])

    def test_configured_sources_follow_the_declared_tier_order(self):
        settings = _settings(screen_enabled=True, dom_url="https://e.invalid/r",
                             cli_command="echo hi")
        self.assertEqual([s.name for s in configured_sources(settings)],
                         ["cli", "dom", "screen"])


class PoolWiringTests(unittest.TestCase):

    def _pools(self, **kwargs):
        return configured_pools(_settings(**kwargs), budget=None, audit=None)
    def test_structured_classes_get_free_pools(self):
        pools = self._pools()
        self.assertEqual(sorted(pools), [CLI, DOM, MCP])
        for cls in (CLI, MCP, DOM):
            self.assertEqual(pools[cls].serves, (cls,))

    def test_the_vision_pool_appears_only_when_enabled(self):
        self.assertNotIn(GUI, self._pools())
        self.assertIn(GUI, self._pools(screen_enabled=True))

    def test_a_pool_is_sized_by_the_quorum_it_has_to_satisfy(self):
        """A pool with fewer slots than the quorum can never meet it.

        A declared driver that refused every step with
        ``insufficient_agreement`` would be a tier chain that exists and is
        still unreachable, so the pool is sized from the setting rather than
        from a constant that happens to match the default.
        """
        for quorum in (1, 2, 3):
            with self.subTest(quorum=quorum):
                structured = self._pools(quorum=quorum)
                self.assertEqual(structured[CLI].size, quorum)
                vision = self._pools(quorum=quorum, screen_enabled=True)
                self.assertEqual(vision[GUI].size, quorum)

    def test_caller_supplied_pools_are_the_only_pools(self):
        """One owner per collaborator: passing ``pools`` replaces, not merges.

        The declared pools used to sit underneath whatever the caller passed
        unless all three constructor arguments were supplied, so the same
        ``pools={}`` meant "the declared ones" or "none" depending on the
        other arguments. Replace is the only version of this that can be
        stated in one sentence.
        """
        only_dom = {DOM: ExtractorPool([], serves=(DOM,))}
        driver = _driver(settings=_settings(screen_enabled=True), pools=only_dom)
        self.assertEqual(sorted(driver.pools), [DOM])
        self.assertEqual(_driver(pools={}).pools, {})


class CommandParsingTests(unittest.TestCase):
    """Tokenisation, never a shell.

    One rule: **a backslash is never an escape character. There are no
    escapes.** Quotes group; everything between them is literal.
    """

    def test_a_simple_command_splits_into_argv(self):
        self.assertEqual(parse_argv("python -c print(1)"),
                         ["python", "-c", "print(1)"])

    def test_quoting_is_respected(self):
        self.assertEqual(parse_argv('tool --name "two words"'),
                         ["tool", "--name", "two words"])

    def test_an_empty_command_is_refused(self):
        for bad in ("", "   ", None):
            with self.assertRaises(ConfigError):
                parse_argv(bad)

    def test_an_unbalanced_quote_is_refused_by_name(self):
        with self.assertRaises(ConfigError) as ctx:
            parse_argv('tool --name "unclosed')
        self.assertIn("tokenised", str(ctx.exception))

    def test_no_expansion_happens(self):
        """The whole point: ``$VAR`` and globs stay literal text."""
        self.assertEqual(parse_argv("tool $HOME *.txt"),
                         ["tool", "$HOME", "*.txt"])

    def test_no_shell_metacharacter_becomes_a_pipe(self):
        argv = parse_argv("tool ; rm -rf /")
        self.assertEqual(argv, ["tool", ";", "rm", "-rf", "/"])
        self.assertNotIn("|", argv)

    # -- the Windows path, which is why the rule changed -----------------

    def test_an_unquoted_windows_path_keeps_its_backslashes(self):
        """The defect that changed this function.

        ``shlex`` in POSIX mode reads ``\\`` as an escape outside quotes, so
        this resolved to ``C:Python314python.exe`` -- a path nobody typed.
        The ``cli`` and ``mcp`` tiers were therefore unusable with an
        absolute interpreter path on the platform this project tests on most.

        Asserted as a literal so it is checked on every cell, not only where
        such a path happens to exist.
        """
        self.assertEqual(
            parse_argv("C:\\Python314\\python.exe server.py"),
            ["C:\\Python314\\python.exe", "server.py"])

    def test_a_windows_path_argument_keeps_its_backslashes(self):
        self.assertEqual(parse_argv("tool --path C:\\data\\out.txt --flag"),
                         ["tool", "--path", "C:\\data\\out.txt", "--flag"])

    def test_this_interpreter_round_trips_through_the_tokeniser(self):
        """The real case, in the form an operator actually writes it.

        On Windows ``sys.executable`` contains backslashes, so this fails on
        the old tokenizer and passes on the new one -- which is why the
        ``windows-latest`` cells are the ones that matter for it. Elsewhere
        it is a plain no-op that keeps the suite honest on all platforms.
        """
        self.assertEqual(parse_argv(sys.executable), [sys.executable])
        self.assertEqual(parse_argv(sys.executable + " -V")[0], sys.executable)

    def test_a_quoted_windows_path_still_works_exactly_as_before(self):
        """The one form that already worked, and must not change."""
        self.assertEqual(
            parse_argv('"C:\\Program Files\\Python\\python.exe" -m srv'),
            ["C:\\Program Files\\Python\\python.exe", "-m", "srv"])

    def test_a_quoted_path_ending_in_a_separator_keeps_it(self):
        """``"C:\\Users\\me\\"`` -- a path ending in a backslash.

        The old tokenizer treated ``\\"`` inside double quotes as an escaped
        quote, so it swallowed the closing quote and lost the separator. Any
        rule that keeps ``\\"`` as an escape reproduces exactly this bug,
        which is why there are no escapes at all.
        """
        self.assertEqual(parse_argv('"C:\\Users\\me\\"'), ["C:\\Users\\me\\"])

    def test_no_escape_sequence_is_processed_anywhere(self):
        """A backslash is a backslash, before or inside quotes.

        Before this, ``\\P`` became ``P`` and ``\\s`` became ``s``, which is
        how a path turned into a different string without anybody asking.
        """
        self.assertEqual(parse_argv('tool "a\\Pb\\sc"'),
                         ["tool", "a\\Pb\\sc"])
        self.assertEqual(parse_argv("tool a\\Pb\\sc"),
                         ["tool", "a\\Pb\\sc"])

    def test_single_quotes_group_too(self):
        self.assertEqual(parse_argv("tool 'two words'"),
                         ["tool", "two words"])

    def test_an_empty_quoted_argument_is_preserved(self):
        self.assertEqual(parse_argv('tool "" --flag'), ["tool", "", "--flag"])

    def test_an_unterminated_quote_names_the_fix_rather_than_guessing(self):
        """Closing it would be a guess, and a wrong guess here is a source
        that looks configured and then fails at run time.

        The realistic cause is a path quoted at one end only, which is what
        somebody types when they quote the half of the path they think is
        ambiguous.
        """
        with self.assertRaises(ConfigError) as ctx:
            parse_argv('"C:\\Program Files\\py\\python.exe -m srv')
        message = str(ctx.exception)
        self.assertIn("unterminated", message)
        self.assertIn("quote", message)
        # It has to say what to do, not merely that it could not do it.
        self.assertIn("space", message)
        self.assertIn("C:\\path with spaces", message)

    def test_an_unquoted_space_splits_and_is_not_refused(self):
        """A deliberate decision, pinned so it cannot drift by accident.

        ``C:\\Program Files\\py\\python.exe`` is two tokens under a rule where
        whitespace separates and quotes group. Refusing it would mean
        guessing at the operator's intent; splitting it is what the rule says.
        What makes that survivable is that the resulting ``not found`` names
        the split argv -- see LaunchDiagnosticTests.
        """
        self.assertEqual(parse_argv("C:\\Program Files\\py\\python.exe"),
                         ["C:\\Program", "Files\\py\\python.exe"])

    def test_a_single_quote_is_refused_the_same_way(self):
        with self.assertRaises(ConfigError) as ctx:
            parse_argv("tool 'oops")
        self.assertIn("unterminated", str(ctx.exception))


class LaunchDiagnosticTests(unittest.TestCase):
    """What a host is told when a declared command will not start.

    The message is the only clue a host gets: the source is configured, the
    step runs, and the failure arrives as a string. So it has to contain
    enough to act on.
    """

    def cli_detail(self, declared):
        source = configured_sources(
            load_settings(env={"DRIVER_CLI_COMMAND": declared}))[0]
        return source.capture(Target("my-app")).detail

    def mcp_detail(self, declared):
        source = configured_sources(load_settings(
            env={"DRIVER_MCP_COMMAND": declared,
                 "DRIVER_MCP_TOOL": "t"}))[0]
        return source.capture(Target("my-app")).detail

    def test_the_message_shows_the_argv_that_was_actually_attempted(self):
        detail = self.cli_detail("definitely-not-a-real-binary-xyz --flag")
        self.assertIn("not found", detail)
        self.assertIn("-- attempted", detail)
        self.assertIn("definitely-not-a-real-binary-xyz", detail)

    def test_a_split_path_is_visible_in_the_message(self):
        """An unquoted path containing a space is split, and the operator
        wrote one path. Echoing the argv is what turns that into a
        diagnosis: ``C:\\Program`` on its own is the tell.

        The message embeds a ``repr``, so backslashes appear doubled; the
        assertions read it with that escaping undone.
        """
        detail = self.cli_detail("C:\\Program Files\\Py\\python.exe")
        flat = detail.replace("\\\\", "\\")
        self.assertIn("C:\\Program", flat)
        self.assertIn("Files\\Py\\python.exe", flat)
        self.assertIn("space", detail)

    def test_the_bare_binary_case_does_not_mention_quoting(self):
        """A missing plain command is not a quoting problem, and telling the
        operator to look at their quotes sends them after the wrong thing."""
        detail = self.cli_detail("definitely-not-a-real-binary-xyz")
        self.assertNotIn("space", detail)

    def test_the_mcp_tier_gets_the_same_diagnostic(self):
        detail = self.mcp_detail("C:\\Program Files\\Py\\mcp.exe")
        self.assertIn("mcp server did not run", detail)
        self.assertIn("-- attempted", detail)
        self.assertIn("space", detail)

    def test_a_long_command_is_abbreviated_rather_than_dumped(self):
        """The detail reaches the audit chain, so an operator who pastes a
        long command must not turn one refusal into a wall of text."""
        declared = ("definitely-not-a-real-binary-xyz "
                    + " ".join(f"a{i}" for i in range(20)))
        detail = self.cli_detail(declared)
        self.assertIn("more'", detail)
        self.assertNotIn("a19", detail)

    def test_a_timeout_is_not_dressed_up_as_a_missing_binary(self):
        source = configured_sources(
            load_settings(env={"DRIVER_CLI_COMMAND": "python"}))[0]
        detail = source.capture(Target("x")).detail
        # python exists, so this one ran; the assertion is that a *successful*
        # launch produces no diagnostic at all.
        self.assertEqual(detail, "")


class DomSchemaTests(unittest.TestCase):
    """The DOM tier needs a schema that declares what a document can supply."""

    def test_a_dom_target_cannot_satisfy_the_screen_schema(self):
        """The reason DOM_SCHEMA exists, pinned so it is not undone."""
        with self.assertRaises(SchemaError):
            validate_state({"window_title": "Report", "visible_text": "x"},
                           SCREEN_SCHEMA)

    def test_a_document_does_satisfy_the_dom_schema(self):
        state = validate_state({"window_title": "Report", "visible_text": "x"},
                               DOM_SCHEMA)
        self.assertEqual(state["window_title"], "report")

    def test_a_document_with_no_title_is_a_shortfall_not_a_guess(self):
        with self.assertRaises(SchemaError) as ctx:
            validate_state({"visible_text": "x"}, DOM_SCHEMA)
        self.assertIn("missing required field", str(ctx.exception))

    def test_the_wire_table_binds_every_class_to_a_schema(self):
        """One table, so a class and its schema cannot drift apart.

        The old ``SCHEMAS_BY_TARGET`` answered ``"dom"`` with the screen
        schema while the wire table said nothing at all, which is how a
        request ended up able to reach pixels by default. This is the pin.
        """
        classes = {cls for cls, _ in WIRE_TARGETS.values()}
        self.assertEqual(classes, {CLI, MCP, DOM, GUI})
        self.assertEqual(WIRE_TARGETS["dom"], (DOM, DOM_SCHEMA))
        self.assertEqual(WIRE_TARGETS["cli"], (CLI, CLI_SCHEMA))
        self.assertEqual(WIRE_TARGETS["mcp"], (MCP, CLI_SCHEMA))
        self.assertEqual(WIRE_TARGETS["screen"], WIRE_TARGETS["gui"])

    def test_the_wire_table_is_resolved_by_one_rule_for_both_surfaces(self):
        """One implementation, so the CLI and the service cannot disagree.

        The service turns a refusal into a 400 and the CLI prints it; neither
        re-derives the rule, so "must this be declared?" is decided once.
        """
        for name, expected in WIRE_TARGETS.items():
            with self.subTest(name=name):
                self.assertEqual(resolve_wire_target(name), expected)
        self.assertEqual(resolve_wire_target("  CLI "), (CLI, CLI_SCHEMA))

    def test_the_rule_refuses_what_it_cannot_bind(self):
        for bad in (None, "", "   ", "banana", "teleport", 17):
            with self.subTest(bad=bad):
                with self.assertRaises(PerceptionUnavailable):
                    resolve_wire_target(bad)
        with self.assertRaises(PerceptionUnavailable) as ctx:
            resolve_wire_target(None)
        self.assertIn("schema is required", str(ctx.exception))
        with self.assertRaises(PerceptionUnavailable) as ctx:
            resolve_wire_target("banana")
        self.assertIn("unknown schema", str(ctx.exception))


class RefusalQualityTests(unittest.TestCase):
    """A refusal has to say what to do about it."""

    def test_the_refusal_names_the_configured_sources(self):
        driver = _driver(settings=_settings(cli_command="echo hi"))
        with self.assertRaises(PerceptionUnavailable) as ctx:
            driver._capture(Target("x", DOM), ())
        message = str(ctx.exception)
        self.assertIn("Configured sources", message)
        self.assertIn("cli", message)

    def test_the_refusal_says_when_nothing_is_configured(self):
        with self.assertRaises(PerceptionUnavailable) as ctx:
            _driver()._capture(Target("x", CLI), ())
        self.assertIn("none", str(ctx.exception))

    def test_a_driver_that_cannot_be_built_says_so_on_the_cli(self):
        """A bad declaration is reported, not raised as a traceback.

        ``Driver`` construction happens before the command runs, so without
        this the one fault an operator can most easily introduce -- a command
        with an unbalanced quote -- would escape the CLI's error handling
        entirely.
        """
        import contextlib
        import io
        from driver_core import cli

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ,
                             {"DRIVER_CLI_COMMAND": 'tool --name "unclosed'}):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(["health"])
        self.assertEqual(code, 2)
        self.assertIn("could not be tokenised", out.getvalue() + err.getvalue())


if __name__ == "__main__":
    unittest.main()
