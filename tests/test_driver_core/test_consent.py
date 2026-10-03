"""The consent law, and the two gates it sits behind.

Consent is the only thing standing between a decision model and a real
machine, so it is worth being precise about what it is. This module is the
specification: **a consent is a capability for one exact ``(action, params)``
pair.** It is reusable, which is what makes one confirmation for a run of
identical clicks sensible. It is not transferable, and there is no wildcard.

Each test below names the specific lie the old behaviour told, because a
consent rule that is merely strict is not the same as one that is right --
"strict" does not tell you which unsafe thing it closed.
"""
import os
import shutil
import tempfile
import unittest

from driver_core import osal
from driver_core.actions import DEFAULT_VOCABULARY
from driver_core.audit import MemoryAuditLog
from driver_core.budget import Budget
from driver_core.errors import ConsentError, ExecutorError, OsalError
from driver_core.executor import (
    NO_CONSENT_NEEDED, Consent, Executor, normalise_params,
)
from driver_core.executor_registry import (
    build_driver_registry, build_read_only_registry,
)


def _executor(*, allow_write=True, dry_run=False):
    return Executor(vocabulary=DEFAULT_VOCABULARY,
                    registry=build_driver_registry(allow_write=allow_write),
                    dry_run=dry_run, audit=MemoryAuditLog())


class WildcardTests(unittest.TestCase):
    """There is no wildcard grant, for any class."""

    def test_a_blanket_grant_authorises_nothing_that_has_consequences(self):
        """``Consent(True, "*")`` used to mean "every mutating action".

        It now means nothing at all beyond read-only. A person who typed
        "yes" without being shown an action or a path has not agreed to
        anything specific, and a rule that treats them as though they had is
        what turns a convenience flag into a standing deletion order.
        """
        consent = Consent(True, "*")
        self.assertFalse(consent.covers("click", "mutating", {"target": "OK"}))
        self.assertFalse(
            consent.covers("write_file", "mutating",
                           {"path": "/tmp/x", "content": ""}))
        self.assertFalse(consent.covers("delete_file", "irreversible",
                                        {"path": "/tmp/x"}))

    def test_a_blanket_grant_still_allows_observation(self):
        consent = Consent(True, "*")
        self.assertTrue(consent.covers("observe", "read_only", {}))

    def test_consent_for_one_action_does_not_transfer_to_another(self):
        """The exact case the class docstring promised all along.

        ``covers`` implemented the inline comment that contradicted its own
        docstring. This is the test that makes the docstring true.
        """
        consent = Consent(True, "delete_file", {"path": "/tmp/x"})
        self.assertFalse(consent.covers("click", "mutating", {"target": "OK"}))
        self.assertTrue(consent.covers("delete_file", "irreversible",
                                       {"path": "/tmp/x"}))

    def test_consent_for_one_path_does_not_transfer_to_another(self):
        consent = Consent(True, "delete_file", {"path": "/tmp/x"})
        self.assertFalse(consent.covers("delete_file", "irreversible",
                                        {"path": "/tmp/y"}))

    def test_a_wildcard_cannot_authorise_irreversible_even_when_params_match(self):
        """The second defect, in the same function.

        ``Consent(True, "*", params={"path": "x"})`` used to pass the
        irreversible branch outright. A standing grant could therefore
        authorise permanent deletion -- the one consequence the design
        exists to prevent -- and it did so through the code that claimed not
        to allow it.
        """
        consent = Consent(True, "*", {"path": "x"})
        self.assertFalse(consent.covers("delete_file", "irreversible",
                                        {"path": "x"}))
        self.assertFalse(consent.covers("delete_file", "irreversible",
                                        {"path": "anything-else"}))

    def test_a_refused_grant_also_refuses_when_the_caller_gave_no_consent(self):
        consent = Consent(False, "click", {"target": "OK"})
        self.assertFalse(consent.covers("click", "mutating", {"target": "OK"}))

    def test_an_empty_param_set_is_a_real_param_set(self):
        """``{}`` means "these exact no parameters", not "unspecified".

        An action whose params are ``()`` and an action whose params were
        never specified must not be conflated, or a grant captured for one
        silently covers the other.
        """
        consent = Consent(True, "no_action", {})
        self.assertTrue(consent.covers("no_action", "read_only", {}))


class ReuseTests(unittest.TestCase):
    """Reuse is what makes one confirmation for a run of clicks sensible."""

    def test_a_mutating_consent_is_reusable_for_the_same_pair(self):
        executor = _executor()
        osal.register_input_backend(osal.platform_name(),
                                    lambda kind, value=None, target=None:
                                    (True, "ok"))
        self.addCleanup(osal.disable_input_backend)
        consent = Consent(True, "click", {"target": "OK"})
        for _ in range(3):
            result = executor.execute("click", {"target": "OK"},
                                      consent=consent)
            self.assertTrue(result.ok)
        self.assertEqual(consent.spent, frozenset())

    def test_an_irreversible_consent_is_spent_on_use(self):
        """A standing grant cannot mean "delete this path, whenever next".

        Nobody saw the path when they said yes on the first run of a driver
        that is now running again unattended. So the capability is consumed
        by the consequence it authorised, and a second deletion needs a
        second person.
        """
        directory = tempfile.mkdtemp(prefix="driver-consent-")
        self.addCleanup(shutil.rmtree, directory, True)
        victim = os.path.join(directory, "a.txt")
        with open(victim, "w", encoding="utf-8") as handle:
            handle.write("x")

        executor = _executor()
        consent = Consent(True, "delete_file", {"path": victim})
        first = executor.execute("delete_file", {"path": victim},
                                 consent=consent)
        self.assertTrue(first.ok)
        self.assertEqual(consent.spent, frozenset({"delete_file"}))
        self.assertFalse(os.path.exists(victim))

        with open(victim, "w", encoding="utf-8") as handle:
            handle.write("y")
        with self.assertRaises(ConsentError) as ctx:
            executor.execute("delete_file", {"path": victim}, consent=consent)
        self.assertIn("re-confirmation", str(ctx.exception))
        # Refused *before* the file was touched.
        self.assertTrue(os.path.exists(victim))

    def test_a_failed_irreversible_action_does_not_spend_the_consent(self):
        """It refused before it committed, so nothing was authorised away."""
        victim = os.path.join(tempfile.gettempdir(), "driver-nonexistent-xyz")
        consent = Consent(True, "delete_file", {"path": victim})
        result = _executor().execute("delete_file", {"path": victim},
                                     consent=consent)
        self.assertFalse(result.ok)
        self.assertEqual(consent.spent, frozenset())

    def test_a_dry_run_does_not_spend_the_consent(self):
        """A rehearsal performed no consequence, so it consumed nothing."""
        directory = tempfile.mkdtemp(prefix="driver-consent-")
        self.addCleanup(shutil.rmtree, directory, True)
        victim = os.path.join(directory, "a.txt")
        with open(victim, "w", encoding="utf-8") as handle:
            handle.write("x")
        consent = Consent(True, "delete_file", {"path": victim})
        result = _executor(dry_run=True).execute("delete_file",
                                                 {"path": victim},
                                                 consent=consent)
        self.assertTrue(result.ok)
        self.assertTrue(result.dry_run)
        self.assertEqual(consent.spent, frozenset())
        self.assertTrue(os.path.exists(victim))


class ResolvedParamsTests(unittest.TestCase):
    """Params are compared in the resolved form, shown in the resolved form."""

    def test_a_path_is_resolved_before_anything_sees_it(self):
        action = DEFAULT_VOCABULARY.resolve("delete_file")
        resolved = normalise_params(action, {"path": "~"})
        self.assertEqual(resolved["path"], osal.resolve_path("~"))
        self.assertTrue(os.path.isabs(resolved["path"]))

    def test_consent_for_an_unresolved_path_does_not_match(self):
        """The load-bearing half.

        ``~/notes`` and ``/home/x/notes`` are different paths, and only one
        of them was shown to the person who said yes. Resolving at
        comparison time instead would silently widen every grant to the
        union of everything it could have meant.
        """
        executor = _executor()
        consent = Consent(True, "delete_file", {"path": "~"})
        action, resolved = executor.resolve("delete_file", {"path": "~"})
        self.assertNotEqual(consent.params, resolved["path"])
        self.assertFalse(consent.covers(action.name, action.action_class,
                                        resolved))

    def test_an_action_declaring_a_normaliser_nobody_implements_is_refused(self):
        from driver_core.actions import MUTATING, Action
        action = Action("x", MUTATING, "e", ("path",), "", "filesystem",
                        normalisers={"path": "no_such_normaliser"})
        with self.assertRaises(Exception) as ctx:
            normalise_params(action, {"path": "/tmp/x"})
        self.assertIn("not implemented", str(ctx.exception))

    def test_a_normaliser_declared_for_an_undeclared_param_is_refused(self):
        from driver_core.actions import MUTATING, Action
        with self.assertRaises(Exception) as ctx:
            Action("x", MUTATING, "e", ("path",), "", "filesystem",
                   normalisers={"content": "resolve_path"})
        self.assertIn("undeclared parameter", str(ctx.exception))

    def test_the_declaration_is_visible_in_the_vocabulary(self):
        """A declaration nobody can read is not a declaration."""
        action = DEFAULT_VOCABULARY.resolve("delete_file").to_dict()
        self.assertEqual(action["normalisers"], {"path": "resolve_path"})


class GateTests(unittest.TestCase):
    """Registration and consent are independent, and both are required."""

    def test_consent_alone_does_not_confer_a_capability(self):
        """A perfect grant against an unregistered executor still refuses.

        This is the more useful of the two failure directions: it means a
        build that cannot write cannot be talked into writing.
        """
        executor = _executor(allow_write=False)
        consent = Consent(True, "write_file",
                          {"path": osal.resolve_path("/tmp/x"), "content": ""})
        with self.assertRaises(ExecutorError) as ctx:
            executor.execute("write_file",
                             {"path": "/tmp/x", "content": ""},
                             consent=consent)
        self.assertIn("not registered", str(ctx.exception))

    def test_registration_alone_does_not_confer_permission(self):
        executor = _executor(allow_write=True)
        with self.assertRaises(ConsentError):
            executor.execute("write_file",
                             {"path": osal.resolve_path("/tmp/x"),
                              "content": ""},
                             consent=Consent(False))

    def test_the_default_driver_is_an_observer(self):
        """Building a Driver must not silently confer the power to delete."""
        registry = build_driver_registry()
        self.assertNotIn("delete_file", registry)
        self.assertNotIn("click", registry)
        self.assertIn("observe", registry)

    def test_the_read_only_registry_still_exists_unchanged(self):
        registry = build_read_only_registry()
        self.assertEqual(sorted(registry.names()),
                         ["no_action", "observe", "read_value"])

    def test_the_default_no_consent_grant_observes_only(self):
        self.assertTrue(NO_CONSENT_NEEDED.covers("observe", "read_only", {}))
        self.assertFalse(NO_CONSENT_NEEDED.covers("click", "mutating",
                                                   {"target": "OK"}))


class InputBackendTests(unittest.TestCase):
    """Priority 1's actual deliverable: dispatch, and honest refusal."""

    def test_shipped_state_is_no_backend_and_it_refuses_by_name(self):
        """driver-core contains no platform input code, by design.

        The consequence an operator has to live with is that an
        unconfigured driver clicks nothing -- and says so, rather than
        reporting a success that did not happen.
        """
        self.assertEqual(osal.input_backends(), {})
        ok, detail = osal.send_input("click", target="OK")
        self.assertFalse(ok)
        self.assertIn("no registered backend", detail)

    def test_a_registered_backend_is_used(self):
        seen = []

        def backend(kind, value=None, target=None):
            seen.append((kind, value, target))
            return True, "sent"

        osal.register_input_backend(osal.platform_name(), backend)
        self.addCleanup(osal.disable_input_backend)
        ok, _ = osal.send_input("click", target="OK")
        self.assertTrue(ok)
        self.assertEqual(seen, [("click", None, "OK")])

    def test_a_backend_can_be_withdrawn_at_runtime(self):
        """Observe the machine, never drive it -- as a policy, not an
        accident of never having configured it."""
        osal.register_input_backend(osal.platform_name(),
                                    lambda kind, value=None, target=None:
                                    (True, "ok"))
        self.addCleanup(osal.disable_input_backend)
        self.assertTrue(osal.send_input("click", target="OK")[0])
        osal.disable_input_backend()
        self.assertFalse(osal.send_input("click", target="OK")[0])

    def test_an_unknown_kind_is_refused_even_with_a_backend(self):
        osal.register_input_backend(osal.platform_name(),
                                    lambda kind, value=None, target=None:
                                    (True, "ok"))
        self.addCleanup(osal.disable_input_backend)
        ok, detail = osal.send_input("teleport", value="x")
        self.assertFalse(ok)
        self.assertIn("unknown input kind", detail)

    def test_a_raising_backend_is_a_refusal_not_a_success(self):
        def backend(kind, value=None, target=None):
            raise RuntimeError("accessibility permission denied")

        osal.register_input_backend(osal.platform_name(), backend)
        self.addCleanup(osal.disable_input_backend)
        ok, detail = osal.send_input("click", target="OK")
        self.assertFalse(ok)
        self.assertIn("accessibility permission denied", detail)

    def test_a_backend_returning_the_wrong_shape_is_refused(self):
        """A mis-shaped return must not be read as success."""
        osal.register_input_backend(osal.platform_name(),
                                    lambda kind, value=None, target=None: True)
        self.addCleanup(osal.disable_input_backend)
        ok, detail = osal.send_input("click", target="OK")
        self.assertFalse(ok)
        self.assertIn("(ok, detail) pair", detail)

    def test_a_backend_for_an_undeclared_platform_is_refused(self):
        with self.assertRaises(OsalError):
            osal.register_input_backend("plan9", lambda **kwargs: (True, ""))

    def test_the_four_actions_that_need_a_backend_all_refuse_without_one(self):
        """focus, scroll and submit are separate kinds, not disguised
        clicks -- a backend asked to click cannot know whether it is nudging
        a cursor or activating something that cannot be undone."""
        for kind, kwargs in (("focus", {"target": "w"}),
                             ("scroll", {"value": "down"}),
                             ("submit", {"target": "Delete"}),
                             ("click", {"target": "OK"})):
            ok, detail = osal.send_input(kind, **kwargs)
            self.assertFalse(ok, kind)
            self.assertIn("no registered backend", detail)


class FilesystemPolicyTests(unittest.TestCase):
    """The two filesystem actions driver-core performs for itself."""

    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="driver-osal-")
        self.addCleanup(shutil.rmtree, self.directory, True)

    def _path(self, name):
        return os.path.join(self.directory, name)

    def test_a_write_is_atomic_and_leaves_no_temporary_behind(self):
        target = self._path("a.txt")
        record = osal.atomic_write(target, "hello")
        self.assertEqual(record["path"], target)
        self.assertEqual(record["bytes"], 5)
        self.assertFalse(record["replaced"])
        with open(target, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "hello")
        self.assertEqual([n for n in os.listdir(self.directory) if ".tmp" in n],
                         [])

    def test_overwriting_backs_up_first(self):
        """A mutating action that could not be undone by hand would have to
        be declared IRREVERSIBLE. Backing up is what keeps this honest."""
        target = self._path("a.txt")
        osal.atomic_write(target, "first")
        record = osal.atomic_write(target, "second")
        self.assertTrue(record["replaced"])
        with open(record["backup"], encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "first")
        with open(target, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "second")

    def test_a_missing_directory_is_refused_not_created(self):
        """Creating a path nobody was shown is a side effect consent did
        not cover."""
        with self.assertRaises(OsalError) as ctx:
            osal.atomic_write(self._path("no/such/dir/a.txt"), "x")
        self.assertIn("will not create", str(ctx.exception))
        self.assertFalse(os.path.exists(self._path("no")))

    def test_writing_over_a_directory_is_refused(self):
        with self.assertRaises(OsalError):
            osal.atomic_write(self.directory, "x")

    def test_delete_removes_exactly_one_file(self):
        target = self._path("a.txt")
        osal.atomic_write(target, "x")
        record = osal.remove_file(target)
        self.assertTrue(record["deleted"])
        self.assertFalse(os.path.exists(target))

    def test_delete_refuses_a_missing_path_rather_than_reporting_success(self):
        with self.assertRaises(OsalError) as ctx:
            osal.remove_file(self._path("never-existed"))
        self.assertIn("nothing was deleted", str(ctx.exception))

    def test_delete_never_recurses_into_a_directory(self):
        with self.assertRaises(OsalError) as ctx:
            osal.remove_file(self.directory)
        self.assertIn("never recurses", str(ctx.exception))
        self.assertTrue(os.path.isdir(self.directory))

    def test_delete_refuses_a_symlink_rather_than_guessing_which_end(self):
        """Deleting the link and deleting its target are different acts."""
        target = self._path("real.txt")
        link = self._path("link.txt")
        osal.atomic_write(target, "x")
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable here")
        with self.assertRaises(OsalError) as ctx:
            osal.remove_file(link)
        self.assertIn("symbolic link", str(ctx.exception))
        self.assertTrue(os.path.exists(target))
        self.assertTrue(os.path.lexists(link))

    def test_an_empty_path_is_refused(self):
        for bad in ("", "   ", None, 7):
            with self.assertRaises(OsalError):
                osal.resolve_path(bad)


class ExecutorWiringTests(unittest.TestCase):
    """The handlers themselves, and what they refuse to invent."""

    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="driver-wire-")
        self.addCleanup(shutil.rmtree, self.directory, True)

    def _path(self, name):
        return os.path.join(self.directory, name)

    def test_write_file_actually_writes(self):
        target = self._path("a.txt")
        result = _executor().execute(
            "write_file", {"path": target, "content": "hello"},
            consent=Consent(True, "write_file",
                            {"path": target, "content": "hello"}))
        self.assertTrue(result.ok)
        with open(target, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "hello")

    def test_a_refused_input_backend_fails_the_execution_rather_than_the_call(self):
        """An executor that *returned* would be recorded as a successful
        action with an unhappy payload, and a caller branching on
        ``execution.ok`` would carry on."""
        result = _executor().execute(
            "click", {"target": "OK"},
            consent=Consent(True, "click", {"target": "OK"}))
        self.assertFalse(result.ok)
        self.assertIn("no registered backend", result.detail)

    def test_delete_file_is_performed_by_driver_core_itself(self):
        target = self._path("a.txt")
        osal.atomic_write(target, "x")
        result = _executor().execute(
            "delete_file", {"path": target},
            consent=Consent(True, "delete_file", {"path": target}))
        self.assertTrue(result.ok)
        self.assertFalse(os.path.exists(target))

    def test_the_audit_record_carries_the_resolved_parameters(self):
        """What ran, and what was consented to, must be the same thing."""
        audit = MemoryAuditLog()
        executor = Executor(vocabulary=DEFAULT_VOCABULARY,
                            registry=build_driver_registry(allow_write=True),
                            audit=audit)
        target = self._path("a.txt")
        executor.execute("write_file", {"path": target, "content": "x"},
                         consent=Consent(True, "write_file",
                                         {"path": target, "content": "x"}),
                         step_id="s1")
        record = [r for r in audit.read_all() if r["kind"] == "action"][0]
        self.assertEqual(record["consent"]["params"]["path"], target)
        self.assertEqual(record["step_id"], "s1")


class PipelineOkMeansActedTests(unittest.TestCase):
    """``ok`` must mean the action happened, not that the pipeline finished.

    Found by the live run, not by the suite: a real executor can *fail*
    without raising (a backend that refuses, a delete of a path that moved),
    and the step was reporting ``ok: true`` with ``execution.ok: false``
    inside it. A host branching on ``ok`` would have been told the click
    happened. A refusal is a refusal all the way up.
    """

    def _step(self, action, params, consent):
        from driver_core.config import load_settings
        from driver_core.driver import Driver
        from driver_core.ev import FakeJev, action_answer
        from driver_core.extractors import (
            ExtractorPool, StructuredExtractor,
        )
        from driver_core.perception import StructuredSource
        from driver_core.states import CLI_SCHEMA

        settings = load_settings(env={}, quorum=2, min_agreement=1.0,
                                confidence_threshold=0.7, run_ceiling_usd=1.0,
                                step_ceiling_usd=1.0, allow_write=True)
        state = {"exit_code": 0, "stdout": "", "stderr": ""}

        def reader(capture, schema):
            return dict(state)

        def source_reader(target):
            return dict(state)

        driver = Driver(
            settings=settings, budget=Budget(1.0, step_ceiling_usd=1.0),
            audit=MemoryAuditLog(),
            pool=ExtractorPool([StructuredExtractor(f"s{i}", reader)
                                for i in range(2)]),
            jev=FakeJev(action_answer(action, confidence=0.99)),
            sources=[StructuredSource("cli", source_reader)])
        return driver.step("t", schema=CLI_SCHEMA, params=params,
                           consent=consent)

    def test_a_failing_executor_makes_the_step_a_refusal(self):
        result = self._step("click", {"target": "OK"},
                            Consent(True, "click", {"target": "OK"}))
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "execution_refused")
        self.assertIn("no registered backend", result.detail)
        # The record still travels, so the failure is inspectable.
        self.assertIsNotNone(result.execution)
        self.assertFalse(result.execution.ok)

    def test_a_successful_executor_is_still_ok(self):
        directory = tempfile.mkdtemp(prefix="driver-ok-")
        self.addCleanup(shutil.rmtree, directory, True)
        target = os.path.join(directory, "a.txt")
        params = {"path": target, "content": "hi"}
        consent = Consent(True, "write_file", params=dict(params), by="test")
        result = self._step("write_file", params, consent)
        self.assertTrue(result.ok, result.detail)
        self.assertTrue(result.execution.ok)
        self.assertTrue(os.path.isfile(target))


if __name__ == "__main__":
    unittest.main()
