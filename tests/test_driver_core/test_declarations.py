"""Declaration guards, the OS boundary, and the perception seam.

The OS boundary test is the one that keeps a platform difference from being
rediscovered by a user on the platform nobody tested. It is a build-failing
scan, not a convention, and it is deliberately written as an AST walk rather
than a grep so that ``import subprocess as sp`` and
``os.name`` behind an alias are both caught.

:class:`ModuleSeamTests` is the same kind of guard for a different kind of
accident. A capture chain living in one file with three adapters and an HTML
parser can drift back together at any time, and every one of those merges is
invisible until a change to tier order needs reading a document scraper to be
made safely. The seam is cheap to lose and cheap to state, so it is stated
here as an import graph and checked.
"""
import ast
import importlib
import inspect
import os
import pathlib
import re
import unittest

import driver_core
from driver_core import osal
from driver_core.actions import (
    DEFAULT_VOCABULARY, READ_ONLY, Vocabulary, check_batch,
)
from driver_core.audit import KIND_REFUSAL, MemoryAuditLog
from driver_core.driver import Driver
from driver_core.errors import SchemaError, VocabularyError
from driver_core.executor import Executor
from driver_core.perception import StructuredSource, fingerprint
from driver_core.schema import Field, Schema, validate_state
from driver_core.states import SCREEN_SCHEMA

PACKAGE = os.path.dirname(os.path.abspath(driver_core.__file__))

#: Modules permitted to touch the OS directly. Everything else is scanned.
BOUNDARY_OWNER = "osal.py"

BANNED = (
    ("subprocess import",
     lambda n: isinstance(n, (ast.Import, ast.ImportFrom))
     and any(a.name.split(".")[0] == "subprocess"
             for a in getattr(n, "names", []))),
    ("os.name platform probe",
     lambda n: isinstance(n, ast.Attribute) and n.attr == "name"
     and isinstance(n.value, ast.Name) and n.value.id == "os"),
    ("sys.platform platform probe",
     lambda n: isinstance(n, ast.Attribute) and n.attr == "platform"
     and isinstance(n.value, ast.Name) and n.value.id == "sys"),
)


def _package_modules():
    for name in sorted(os.listdir(PACKAGE)):
        if name.endswith(".py"):
            yield name, os.path.join(PACKAGE, name)


def _sibling_imports(name):
    """The sibling modules ``name`` imports, by module name.

    Relative imports only. A sibling reached through an absolute name is still
    a sibling, but the package has no such import today and asserting on the
    relative form keeps the guard readable.
    """
    path = os.path.join(PACKAGE, name)
    tree = ast.parse(pathlib.Path(path).read_text(encoding="utf-8"), path)
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            found.add((node.module or "").split(".")[0])
    return found


class OsBoundaryTests(unittest.TestCase):

    def test_os_contact_lives_only_in_osal(self):
        findings = []
        for name, path in _package_modules():
            if name == BOUNDARY_OWNER:
                continue
            source = pathlib.Path(path).read_text(encoding="utf-8")
            tree = ast.parse(source, path)
            for node in ast.walk(tree):
                for label, predicate in BANNED:
                    if predicate(node):
                        findings.append(f"{name}:{node.lineno} {label}")
        self.assertEqual(findings, [],
                         "OS contact escaped the boundary: " + "; ".join(findings))

    def test_the_owner_actually_owns_what_the_scan_bans(self):
        """If the owner stopped using subprocess the ban would pass
        vacuously, and the guard would be protecting nothing."""
        source = pathlib.Path(PACKAGE, BOUNDARY_OWNER).read_text(encoding="utf-8")
        self.assertIn("import subprocess", source)

    def test_the_scan_covers_the_whole_package(self):
        modules = dict(_package_modules())
        self.assertIn(BOUNDARY_OWNER, modules)
        self.assertGreaterEqual(len(modules), 10)

    def test_run_refuses_a_bare_string_so_a_shell_is_impossible(self):
        with self.assertRaises(TypeError) as ctx:
            osal.run("ls -la")
        self.assertIn("argv list", str(ctx.exception))

    def test_a_missing_binary_is_a_named_condition_not_an_exception(self):
        result = osal.run(["definitely-not-a-real-binary-xyz"])
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "not_found")

    def test_a_non_zero_exit_is_an_answer_not_a_failure(self):
        result = osal.run(["python", "-c", "import sys; sys.exit(3)"])
        self.assertEqual(result.returncode, 3)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "")

    def test_a_command_that_actually_runs_returns_its_output(self):
        result = osal.run(["python", "-c", "print('hello')"])
        self.assertTrue(result.ok)
        self.assertIn("hello", result.stdout)

    def test_synthetic_input_is_declared_unsupported_rather_than_faked(self):
        ok, detail = osal.send_input("click", target="OK")
        self.assertFalse(ok)
        self.assertIn("no registered backend", detail)

    def test_an_unknown_input_kind_is_refused(self):
        ok, detail = osal.send_input("teleport", value="x")
        self.assertFalse(ok)
        self.assertIn("unknown input kind", detail)


class SchemaDeclarationTests(unittest.TestCase):

    def test_a_schema_with_no_required_field_is_refused(self):
        """A schema nothing must satisfy would let an empty state pass."""
        with self.assertRaises(SchemaError):
            Schema("s", "1", [Field("a", "string", presence="optional")])

    def test_duplicate_fields_are_refused(self):
        with self.assertRaises(SchemaError):
            Schema("s", "1", [Field("a", "string"), Field("a", "string")])

    def test_an_enum_without_values_is_refused(self):
        with self.assertRaises(SchemaError):
            Field("a", "enum")

    def test_provenance_must_match_the_field_type(self):
        with self.assertRaises(SchemaError):
            Field("a", "string", provenance="numeric")
        with self.assertRaises(SchemaError):
            Field("a", "boolean", provenance="cased")

    def test_an_undeclared_field_makes_a_state_invalid(self):
        with self.assertRaises(SchemaError) as ctx:
            validate_state({"window_title": "x", "surprise": 1}, SCREEN_SCHEMA)
        self.assertIn("undeclared field", str(ctx.exception))

    def test_a_missing_required_field_is_invalid(self):
        with self.assertRaises(SchemaError):
            validate_state({"window_title": "x"}, SCREEN_SCHEMA)

    def test_an_optional_field_with_a_bad_value_is_invalid_not_absent(self):
        """Treated as absent, a hallucinated value becomes invisible instead
        of rejected."""
        with self.assertRaises(SchemaError):
            validate_state({"window_title": "x", "foreground_app": "app",
                            "error_dialog_present": False,
                            "dialog_kind": "not_a_kind"}, SCREEN_SCHEMA)

    def test_normalisation_is_applied_not_stored_raw(self):
        state = validate_state({"window_title": "  Report  ", "foreground_app": "e",
                                "error_dialog_present": "YES"},
                               SCREEN_SCHEMA)
        self.assertEqual(state["window_title"], "report")
        self.assertIs(state["error_dialog_present"], True)


class VocabularyTests(unittest.TestCase):

    def test_an_undeclared_action_raises_rather_than_defaulting(self):
        with self.assertRaises(VocabularyError) as ctx:
            DEFAULT_VOCABULARY.resolve("rm_rf")
        self.assertIn("not in the declared vocabulary", str(ctx.exception))

    def test_a_near_miss_does_not_resolve(self):
        for near in ("Observe", "observe ", "observes", "OBSERVE"):
            with self.assertRaises(VocabularyError):
                DEFAULT_VOCABULARY.resolve(near)

    def test_every_action_declares_an_executor_and_params(self):
        for name in DEFAULT_VOCABULARY.names():
            action = DEFAULT_VOCABULARY.resolve(name)
            self.assertTrue(action.executor)
            self.assertTrue(action.target)

    def test_irreversible_actions_require_exact_consent_scope(self):
        self.assertTrue(
            DEFAULT_VOCABULARY.resolve("delete_file").requires_human_confirmation)
        self.assertFalse(
            DEFAULT_VOCABULARY.resolve("click").requires_human_confirmation)

    def test_undeclared_parameters_are_refused(self):
        action = DEFAULT_VOCABULARY.resolve("read_value")
        with self.assertRaises(VocabularyError):
            action.check_params({"field": "x", "sneaky": 1})

    def test_missing_parameters_are_refused(self):
        action = DEFAULT_VOCABULARY.resolve("read_value")
        with self.assertRaises(VocabularyError):
            action.check_params({})

    def test_a_batch_may_not_smuggle_several_irreversible_actions(self):
        with self.assertRaises(VocabularyError):
            check_batch(["delete_file", "submit_irreversible"],
                        DEFAULT_VOCABULARY)

    def test_a_duplicate_action_name_is_refused(self):
        from driver_core.actions import Action
        dup = Action("x", READ_ONLY, "e", (), "", "any")
        with self.assertRaises(VocabularyError):
            Vocabulary("v", "1", [dup, dup])


class PerceptionTests(unittest.TestCase):

    def test_a_fingerprint_does_not_leak_the_payload(self):
        payload = {"window_title": "Confidential Payroll 2026"}
        digest = fingerprint(payload)
        self.assertNotIn("Payroll", digest)
        self.assertEqual(len(digest), 16)

    def test_an_undeclared_source_name_is_refused(self):
        with self.assertRaises(Exception) as ctx:
            StructuredSource("telepathy", lambda t: None)
        self.assertIn("unknown structured source", str(ctx.exception))

    def test_a_declined_source_produces_a_named_capture_not_a_crash(self):
        source = StructuredSource("cli", lambda target: None)
        capture = source.capture("t")
        self.assertFalse(capture.ok)
        self.assertIn("nothing", capture.detail)


class ModuleSeamTests(unittest.TestCase):

    #: The perception tier's four owners and, for each, the siblings it is
    #: forbidden to import. ``perception`` is the façade: it may reach all
    #: four, and none of the four may reach it, which keeps the cycle shut.
    FORBIDDEN = {
        "parsing.py": {"observation", "adapters", "chain", "perception"},
        "observation.py": {"adapters", "chain", "parsing", "perception"},
        "adapters.py": {"chain", "perception"},
        "chain.py": {"adapters", "parsing", "perception"},
    }

    def test_the_seam_modules_exist_under_the_names_the_table_names(self):
        modules = dict(_package_modules())
        missing = (set(self.FORBIDDEN) | {"perception.py"}) - set(modules)
        self.assertEqual(sorted(missing), [],
                         "the perception tier's modules moved; update the table")

    def test_no_module_reaches_across_a_forbidden_edge(self):
        """The whole seam, in one walk: no adapter may name the order, no
        policy may name an adapter, no vocabulary may name either, and nothing
        beneath the façade may name the façade."""
        findings = []
        for name, forbidden in self.FORBIDDEN.items():
            reached = _sibling_imports(name) & forbidden
            if reached:
                findings.append(f"{name} imports {sorted(reached)}")
        self.assertEqual(findings, [], "; ".join(findings))

    def test_a_parser_imports_nothing_at_all(self):
        """Stronger than the table above, which only bans today's siblings:
        a wire-format reader should stay answerable from the standard library
        alone, and a sixth module is not a licence to reach into it."""
        self.assertEqual(_sibling_imports("parsing.py"), set())

    def test_the_os_boundary_still_covers_every_split_module(self):
        """The scan globs the directory, so the split could in principle have
        left a file outside its reach. This pins that it did not."""
        scanned = dict(_package_modules())
        for name in ("adapters.py", "chain.py", "observation.py", "parsing.py"):
            self.assertIn(name, scanned)


class _Unchained:
    """Something that acts and records nothing, the way a rogue executor does."""

    audit = None


class _WrongChain:
    """Something that acts and records to a chain of its own."""

    def __init__(self):
        self.audit = MemoryAuditLog()


class AuditIsRequiredTests(unittest.TestCase):
    """Nothing that acts on the machine can be built without a chain.

    ``Driver`` and ``Executor`` both used to default ``audit=None``, which
    read as "auditing is optional" on the two objects that actually run
    actions. It was not reachable in production -- ``Driver`` substituted a
    real log -- which is exactly why it survived: unreachable is not the
    same as unrepresentable, and a default argument is representable.

    So the argument is required, ``None`` is refused, and this checks the
    signature itself rather than a list of the classes that have it, so a
    sixth one is covered without anyone remembering to add it here. The
    seam beside that check -- an executor handed to a driver rather than
    built by one -- enforces the same rule, because that is where an
    unrecorded action would actually arrive.
    """

    def _constructors_taking_an_audit(self):
        """Every class in the package whose constructor accepts ``audit``."""
        found = []
        root = pathlib.Path(driver_core.__file__).parent
        for path in sorted(root.glob("*.py")):
            module = importlib.import_module(
                f"driver_core.{path.stem}" if path.stem != "__init__"
                else "driver_core")
            for _, cls in inspect.getmembers(module, inspect.isclass):
                if cls.__module__ != module.__name__:
                    continue
                init = cls.__dict__.get("__init__")
                if init is None:
                    continue
                if "audit" in inspect.signature(init).parameters:
                    found.append(cls)
        return found

    def test_no_constructor_defaults_the_audit_log(self):
        defaulted = [cls.__name__ for cls in self._constructors_taking_an_audit()
                     if inspect.signature(cls.__dict__["__init__"])
                     .parameters["audit"].default is not inspect.Parameter.empty]
        self.assertEqual(
            defaulted, [],
            f"a default of {defaulted} puts an unrecorded action back within "
            f"reach; the argument has to be required")

    def test_the_four_that_take_one_all_do(self):
        """Pins that the scan found the real collaborators and not an empty
        set, which would pass the test above for the wrong reason."""
        self.assertEqual(
            sorted(cls.__name__ for cls in self._constructors_taking_an_audit()),
            ["Driver", "Executor", "JevClient", "VisionExtractor"])

    def test_an_executor_cannot_be_built_without_a_chain(self):
        with self.assertRaises(TypeError):
            Executor(vocabulary=DEFAULT_VOCABULARY, registry={})

    def test_a_driver_cannot_be_built_without_a_chain(self):
        with self.assertRaises(TypeError):
            Driver()

    def test_none_is_refused_by_name(self):
        """Passing ``audit=None`` explicitly is the same mistake with more
        steps, and it must not be a way through."""
        for owner, call in (
            ("An Executor", lambda: Executor(
                vocabulary=DEFAULT_VOCABULARY, registry={}, audit=None)),
            ("A Driver", lambda: Driver(audit=None)),
        ):
            with self.subTest(owner=owner):
                with self.assertRaises(TypeError) as ctx:
                    call()
                message = str(ctx.exception)
                self.assertIn(owner, message)
                # The message has to name both ways out, or a caller
                # reaching this learns only that something is wrong.
                self.assertIn("AuditLog(path)", message)
                self.assertIn("MemoryAuditLog()", message)

    def test_there_is_no_null_log_to_sneak_past_it_with(self):
        """The refusal is only honest because the alternative still records.

        A caller that wants no *disk* is a real case; a caller that wants no
        *record* is not, and ``MemoryAuditLog`` must not quietly have become
        the second one.
        """
        log = MemoryAuditLog()
        log.append(KIND_REFUSAL, step_id="s", reason="r", detail="")
        self.assertEqual([r["kind"] for r in log.read_all()], ["refusal"])
        self.assertTrue(log.verify().ok)

    def test_an_executor_brought_to_a_driver_must_share_its_chain(self):
        """The seam, which is where an unrecorded action actually arrives.

        ``Driver(executor=...)`` never went through the constructor check, so
        a caller could hand over something recording to nothing, or to a
        second chain whose records nothing links to the step that led to
        them. Both are refused now, by name, with the way out named.
        """
        for label, executor in (
            ("no chain at all", _Unchained()),
            ("a different chain", _WrongChain()),
            ("not an executor at all", object()),
        ):
            with self.subTest(executor=label):
                with self.assertRaises(TypeError) as ctx:
                    Driver(audit=MemoryAuditLog(), executor=executor)
                self.assertIn("chain", str(ctx.exception))

    def test_the_executor_a_driver_builds_shares_its_chain(self):
        driver = Driver(audit=MemoryAuditLog())
        self.assertIs(driver.executor.audit, driver.audit)

    def test_one_function_owns_the_pairing_and_the_entry_points_use_it(self):
        """``Driver`` alone cannot decide where the chain goes, and three
        call sites each working it out is three copies of one rule.

        Checked on the source rather than by calling the entry points,
        because the failure is a duplicate someone adds later, not a wrong
        answer today.
        """
        root = pathlib.Path(driver_core.__file__).parent
        offenders = []
        for path in sorted(root.glob("*.py")):
            if path.name in ("driver.py", "audit.py"):
                continue
            text = path.read_text(encoding="utf-8")
            if "Driver(" in text and "default_audit_path" in text:
                offenders.append(path.name)
            if re.search(r"Driver\([^)]*audit\s*=", text, re.S):
                offenders.append(path.name)
        self.assertEqual(
            sorted(set(offenders)), [],
            "a module builds a Driver and names its own chain; call "
            "driver_from_settings instead")


if __name__ == "__main__":
    unittest.main()
