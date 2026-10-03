"""Hermetic tests for harness/provision.py (probe, plan, approve, execute).

Every command goes through a fake runner and a fake ``which``; the only real
filesystem effect is ``mkdir`` inside a temporary approved root, which is the
very behaviour the executor's builtin must prove is confined and skipped in a
dry run. No network, no key, no real package manager.
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import osal, provision as pv
from harness.config import load_settings
from harness.errors import HarnessError
from harness.jev import JevEvaluationResult
from harness.jev_packs import (provision_selection_question_pack,
                               provision_verification_question_pack)
from harness.jev_policy import JevPolicy
from harness.ledger import AutonomyLedger

LINUX = {"os": "Linux", "arch": "x86_64", "python_version": "3.12.1",
         "python_executable": "python", "free_disk_bytes": 50 * 10**9}
WINDOWS = dict(LINUX, os="Windows", python_executable="python.exe")


class FakeRunner:
    """Scripted ``osal.run`` double: records every call, never spawns."""

    def __init__(self, script=None, default=None):
        self.script = dict(script or {})
        self.default = default or osal.CommandResult(0, "ok\n", "")
        self.calls = []

    def __call__(self, argv, cwd=None, timeout=None, env=None, **kw):
        # The executor pins a bare name to a resolved path; record and match
        # on the program's bare name so a test reads the same either way.
        shown = [pv._exe_name(argv[0])] + list(argv[1:])
        self.calls.append({"argv": shown, "resolved": list(argv), "cwd": cwd,
                           "timeout": timeout, "env": env})
        for prefix, outcome in self.script.items():
            if tuple(shown[:len(prefix)]) == prefix:
                if isinstance(outcome, BaseException):
                    raise outcome
                return outcome
        return self.default

    def argvs(self):
        return [c["argv"] for c in self.calls]


FAKE_BIN = os.path.join(os.path.abspath(os.sep), "usr", "bin")


def fake_which(name):
    """Every bare name resolves into a PATH directory nobody is working in."""
    return os.path.join(FAKE_BIN, name)


def which_of(*names, paths=None):
    table = {n: (paths or {}).get(n, "/usr/bin/" + n) for n in names}
    return lambda name: table.get(name)


class TempRootCase(unittest.TestCase):
    def setUp(self):
        # realpath: a Windows temp dir can be an 8.3 short path (RUNNER~1),
        # which the path grammar refuses in step arguments.
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="provision_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = os.path.join(self.tmp, "approved")
        os.makedirs(self.root)
        self.policy = pv.ProvisionPolicy(approved_roots=(self.root,))
        self.ledger = AutonomyLedger(os.path.join(self.tmp, "ledger.jsonl"))

    def work(self, name="tool"):
        return os.path.join(self.root, name)

    def probe(self, facts=None, managers=("pip", "npm"), gpus=False):
        runner = FakeRunner({
            ("nvidia-smi",): osal.CommandResult(0, "NVIDIA RTX 4090, 24564 MiB\n"),
        })
        names = list(managers) + (["nvidia-smi"] if gpus else [])
        return pv.probe_host(self.root, runner=runner,
                             which=which_of(*names), facts=facts or LINUX)

    def events(self, name):
        return [e for e in self.ledger.entries() if e["event"] == name]


# --------------------------------------------------------------------------
# vocabulary parity
# --------------------------------------------------------------------------

class VocabularyTests(unittest.TestCase):
    def test_classes_match_driver_core_consent_classes(self):
        from driver_core import actions
        self.assertEqual(pv.READ, actions.READ_ONLY)
        self.assertEqual(pv.MUTATING, actions.MUTATING)
        self.assertEqual(pv.IRREVERSIBLE, actions.IRREVERSIBLE)
        self.assertEqual(pv.CLASSES, actions.ACTION_CLASSES)

    def test_error_is_a_harness_error_with_problems(self):
        err = pv.ProvisionError("bad", ["a", "b"])
        self.assertIsInstance(err, HarnessError)
        self.assertEqual(err.problems, ["a", "b"])
        self.assertEqual(pv.ProvisionError("x").problems, [])


# --------------------------------------------------------------------------
# probe
# --------------------------------------------------------------------------

class ProbeTests(TempRootCase):
    def test_probe_reads_facts_managers_and_gpu(self):
        runner = FakeRunner({
            ("pip", "--version"): osal.CommandResult(0, "pip 24.0 from x\n"),
            ("nvidia-smi",): osal.CommandResult(
                0, "NVIDIA GeForce RTX 4090,  24564 MiB\n\n"),
        })
        probe = pv.probe_host(self.root, runner=runner, facts=LINUX,
                              which=which_of("pip", "npm", "apt", "nvidia-smi"))
        self.assertEqual(probe.os_name, "Linux")
        self.assertEqual(probe.arch, "x86_64")
        self.assertEqual(probe.python_version, "3.12.1")
        self.assertEqual(probe.free_disk_bytes, 50 * 10**9)
        self.assertEqual(probe.usable_managers(), ("apt", "pip", "npm"))
        self.assertEqual(probe.manager("pip").version, "pip 24.0 from x")
        self.assertTrue(probe.gpu_present)
        self.assertEqual(probe.gpus, ("NVIDIA GeForce RTX 4090, 24564 MiB",))
        # every probe command is a fixed read-only query with a timeout
        for call in runner.calls:
            self.assertIsNotNone(call["timeout"])
            self.assertTrue(call["argv"][-1].startswith("--")
                            or call["argv"][-1] == "--format=csv,noheader")

    def test_manager_whose_version_fails_is_not_usable(self):
        runner = FakeRunner(default=osal.CommandResult(1, "", "stub"))
        probe = pv.probe_host(self.root, runner=runner, facts=WINDOWS,
                              which=which_of("winget"))
        self.assertEqual(probe.usable_managers(), ())
        self.assertIsNone(probe.manager("winget"))
        self.assertFalse(probe.package_managers[0].usable)
        self.assertIsNone(probe.package_managers[0].version)

    def test_pip_found_only_as_python_module(self):
        runner = FakeRunner({
            ("python", "-I", "-m", "pip"): osal.CommandResult(0, "pip 23.1\n")})
        probe = pv.probe_host(self.root, runner=runner, facts=LINUX,
                              which=which_of())
        pip = probe.manager("pip")
        self.assertEqual(pip.path, "python -m pip")
        self.assertEqual(runner.argvs(), [["python", "-I", "-m", "pip", "--version"]])

    def test_no_pip_at_all(self):
        runner = FakeRunner(default=osal.CommandResult(1, "", "no"))
        probe = pv.probe_host(self.root, runner=runner, facts=LINUX,
                              which=which_of())
        self.assertEqual(probe.package_managers, ())
        self.assertFalse(probe.gpu_present)

    def test_rocm_gpu_parse_and_failure(self):
        out = ("GPU[0] : Card series: Radeon RX 7900\nGPU[0] : Card model: 0x744c\n"
               "noise\n")
        runner = FakeRunner({("rocm-smi",): osal.CommandResult(0, out)})
        probe = pv.probe_host(self.root, runner=runner, facts=LINUX,
                              which=which_of("rocm-smi"))
        self.assertEqual(probe.gpus, ("Radeon RX 7900", "0x744c"))
        failing = FakeRunner(default=osal.CommandResult(2, "", "x"))
        probe = pv.probe_host(self.root, runner=failing, facts=LINUX,
                              which=which_of("rocm-smi", "nvidia-smi"))
        self.assertFalse(probe.gpu_present)

    def test_apple_silicon_counts_as_gpu(self):
        facts = dict(LINUX, os="Darwin", arch="arm64")
        probe = pv.probe_host(self.root, runner=FakeRunner(), facts=facts,
                              which=which_of())
        self.assertTrue(probe.gpu_present)
        self.assertIn("Apple silicon", probe.gpus[0])

    def test_defaults_come_from_the_real_platform(self):
        probe = pv.probe_host(os.path.join(self.root, "does", "not", "exist"),
                              runner=FakeRunner(default=osal.CommandResult(1)),
                              which=which_of())
        self.assertTrue(probe.os_name)
        self.assertTrue(probe.python_version)
        self.assertEqual(probe.python_executable, osal.python_exe())
        self.assertIsInstance(probe.free_disk_bytes, int)  # nearest ancestor

    def test_disk_probe_failure_and_filesystem_root_are_handled(self):
        with mock.patch.object(pv.shutil, "disk_usage", side_effect=OSError("x")):
            facts = pv._default_facts(self.root)
        self.assertIsNone(facts["free_disk_bytes"])
        with mock.patch.object(pv.os.path, "exists", return_value=False):
            top = pv._existing_ancestor(os.path.join(self.root, "a", "b"))
        self.assertEqual(top, os.path.dirname(top))  # climbed to the root

    def test_missing_disk_figure_is_none(self):
        probe = pv.probe_host(self.root, runner=FakeRunner(), which=which_of(),
                              facts=dict(LINUX, free_disk_bytes=None))
        self.assertIsNone(probe.free_disk_bytes)

    def test_dict_and_jev_facts_exclude_paths(self):
        probe = self.probe(gpus=True)
        data = probe.to_dict()
        self.assertEqual(data["os"], "Linux")
        self.assertTrue(data["gpu_present"])
        self.assertEqual(len(data["package_managers"]), 2)
        facts = probe.jev_facts()
        self.assertNotIn("python_executable", facts)
        self.assertNotIn("probed_path", facts)
        self.assertEqual(facts["package_managers"], ["pip", "npm"])

    def test_first_line_and_ancestor_helpers(self):
        self.assertIsNone(pv._first_line("\n  \n"))
        self.assertEqual(len(pv._first_line("x" * 500)), 120)
        self.assertEqual(
            os.path.normcase(pv._existing_ancestor(
                os.path.join(self.root, "a", "b"))),
            os.path.normcase(self.root))


# --------------------------------------------------------------------------
# step / plan types
# --------------------------------------------------------------------------

def step(**over):
    base = dict(id="s1", description="d", argv=("python", "--version"),
                classification=pv.READ, expected_effect="prints version")
    base.update(over)
    return pv.Step(**base)


class StepTypeTests(unittest.TestCase):
    def test_shell_string_argv_is_unrepresentable(self):
        with self.assertRaises(pv.ProvisionError) as ctx:
            step(argv="pip install requests && rm -rf /")
        self.assertIn("not a shell string", str(ctx.exception))
        with self.assertRaises(pv.ProvisionError):
            step(argv=b"pip")

    def test_argv_must_be_a_nonempty_list_of_nonempty_strings(self):
        for bad in ([], ["pip", 3], ["pip", ""], 42, None):
            with self.assertRaises(pv.ProvisionError, msg=repr(bad)):
                step(argv=bad)

    def test_argv_list_is_frozen_to_tuple(self):
        self.assertEqual(step(argv=["python", "--version"]).argv,
                         ("python", "--version"))

    def test_step_from_dict_is_strict(self):
        good = step().to_dict()
        self.assertEqual(pv.step_from_dict(good), step())
        for mutate in (
            lambda d: d.update(extra=1),
            lambda d: d.pop("argv"),
            lambda d: d.update(argv="pip install x"),
            lambda d: d.update(id=5),
            lambda d: d.update(rollback_hint=3),
            lambda d: d.update(requires_network="yes"),
            lambda d: d.update(cwd=3),
        ):
            data = dict(good)
            mutate(data)
            with self.assertRaises(pv.ProvisionError, msg=repr(data)):
                pv.step_from_dict(data)
        with self.assertRaises(pv.ProvisionError):
            pv.step_from_dict(["not", "a", "dict"])

    def test_step_digest_tracks_content(self):
        self.assertEqual(step().digest(), step().digest())
        self.assertNotEqual(step().digest(),
                            step(argv=("python", "-V")).digest())

    def test_plan_digest_ignores_provenance_but_not_content(self):
        a = pv.Plan("g", [step()], "/r", "inventory", "deterministic", True)
        b = pv.Plan("g", [step()], "/r", "other", "jev", False, ("note",))
        c = pv.Plan("g", [step(argv=("python", "-V"))], "/r", "inventory",
                    "deterministic", True)
        self.assertEqual(a.digest, b.digest)
        self.assertNotEqual(a.digest, c.digest)
        self.assertEqual(a.step("s1"), step())
        with self.assertRaises(pv.ProvisionError):
            a.step("nope")

    def test_plan_round_trip_and_rendering(self):
        plan = pv.Plan("goal", [step(), step(
            id="s2", argv=("mkdir", "/r/x"), classification=pv.MUTATING,
            rollback_hint="delete it", requires_network=True)],
            "/r", "inventory", "deterministic", True, ("be careful",))
        data = plan.to_dict()
        self.assertEqual(data["digest"], plan.digest)
        rebuilt = pv.plan_from_dict(data)
        self.assertEqual(rebuilt.digest, plan.digest)
        self.assertTrue(rebuilt.is_fallback)
        text = pv.render_plan(plan)
        self.assertIn("[mutating] [network] s2", text)
        self.assertIn("rollback: delete it", text)
        self.assertIn("note: be careful", text)
        self.assertIn("(deterministic, fallback)", text)

    def test_plan_from_dict_refuses_malformed_plans(self):
        for bad in ("x", {"steps": "no"}, {"steps": [], "goal": 1, "root": "r"},
                    {"steps": [{"id": "s"}], "goal": "g", "root": "r"}):
            with self.assertRaises(pv.ProvisionError, msg=repr(bad)):
                pv.plan_from_dict(bad)
        bare = pv.plan_from_dict({"steps": [], "goal": "g", "root": "r"})
        self.assertEqual((bare.recipe, bare.source), ("external", "external"))


# --------------------------------------------------------------------------
# the allowlist
# --------------------------------------------------------------------------

class AllowlistTests(TempRootCase):
    def admit(self, *argv):
        return pv.classify_argv(list(argv), self.policy)

    def refuse(self, *argv, needle=""):
        with self.assertRaises(pv.ProvisionError) as ctx:
            pv.classify_argv(list(argv), self.policy)
        self.assertIn(needle, str(ctx.exception), msg=repr(argv))

    def test_read_only_queries(self):
        for argv in (("python", "--version"), ("python3.12", "-V"),
                     ("python", "-I", "-m", "pip", "--version"),
                     ("python", "-I", "-m", "pip", "list"),
                     ("pip", "list", "--format=json"),
                     ("pip", "show", "requests"),
                     ("pip", "--version"),
                     ("npm", "--version"),
                     ("npm", "ls", "left-pad", "--prefix", self.root),
                     ("npm", "list", "--prefix", self.root),
                     ("winget", "--version"), ("brew", "--version"),
                     ("winget", "list", "--id", "Pkg.Id"),
                     ("brew", "list", "wget"),
                     ("apt", "list", "--installed", "git"),
                     ("choco", "list", "--local-only", "git")):
            adm = self.admit(*argv)
            self.assertEqual(adm.minimum_class, pv.READ, msg=argv)
            self.assertFalse(adm.requires_network, msg=argv)

    def test_mutating_rules(self):
        venv_py = os.path.join(self.work(), "venv", "bin", "python")
        cases = {
            ("mkdir", self.work()): False,
            ("python", "-I", "-m", "venv", self.work()): False,
            (venv_py, "-I", "-m", "pip", "install", "--isolated", "--only-binary=:all:",
             "requests==2.0", "rich[extras]>=1"): True,
            ("python", "-I", "-m", "pip", "install", "--isolated", "--only-binary=:all:",
             "--target", self.work(), "ruff"): True,
            ("npm", "install", "left-pad@1.3.0", "@scope/pkg", "--prefix",
             self.root, "--ignore-scripts"): True,
            (os.path.join(self.work(), "venv", "bin", "pip"), "install",
             "--isolated", "--only-binary=:all:", "-q", "x"): True,
        }
        for argv, network in cases.items():
            adm = self.admit(*argv)
            self.assertEqual(adm.minimum_class, pv.MUTATING, msg=argv)
            self.assertEqual(adm.requires_network, network, msg=argv)

    def test_system_install_is_irreversible_and_networked(self):
        for argv in (("winget", "install", "--id", "Pkg.Id", "--exact",
                      "--version", "1.2"),
                     ("choco", "install", "--version", "1.0", "-y", "git"),
                     ("scoop", "install", "extras/git"),
                     ("apt-get", "install", "-y", "git"),
                     ("brew", "install", "git")):
            adm = self.admit(*argv)
            self.assertEqual(adm.minimum_class, pv.IRREVERSIBLE, msg=argv)
            self.assertTrue(adm.requires_network, msg=argv)

    def test_system_install_can_be_forbidden_by_policy(self):
        policy = pv.ProvisionPolicy(approved_roots=(self.root,),
                                    allow_system_install=False)
        with self.assertRaises(pv.ProvisionError):
            pv.classify_argv(["brew", "install", "git"], policy)
        # queries stay fine
        pv.classify_argv(["brew", "list", "git"], policy)

    def test_never_allowed_executables_are_named(self):
        for exe, why in (("sudo", "privilege"), ("rm", "deletion"),
                         ("format", "formatting"), ("reg", "registry"),
                         ("powershell", "shell"), ("bash", "shell"),
                         ("cmd", "shell"), ("curl", "download"),
                         ("/usr/bin/sudo", "privilege"),
                         ("C:\\Windows\\System32\\reg.exe", "registry"),
                         ("RM.EXE", "deletion"), ("mkfs", "formatting")):
            self.refuse(exe, "-rf", "/", needle=why)

    def test_unlisted_executables_and_registry_paths(self):
        self.refuse("git", "clone", "x", needle="not on the provisioning allowlist")
        self.refuse("python", "--version", "HKLM\\Software\\X", needle="registry")
        self.refuse("python", "--version", "bad\narg", needle="shell syntax")

    def test_shell_string_and_malformed_argv(self):
        for bad in ("pip install x", b"pip", [], (), None, ["pip", 1], ["pip", ""]):
            with self.assertRaises(pv.ProvisionError, msg=repr(bad)):
                pv.classify_argv(bad, self.policy)

    def test_python_is_confined_to_venv_and_pip(self):
        self.refuse("python", "-c", "import os", needle="no -c")
        self.refuse("python", "script.py", needle="no -c")
        self.refuse("python", "-m", "http.server", needle="no -c")
        self.refuse("python", "-I", "-m", "venv", needle="exactly one directory")
        self.refuse("python", "-I", "-m", "venv", "--clear", self.work(),
                    needle="exactly one directory")
        self.refuse("python", "-I", "-m", "venv", os.path.join(self.tmp, "outside"),
                    needle="outside every approved root")

    def test_pip_refusals(self):
        root = self.work()
        self.refuse("pip", needle="needs a subcommand")
        self.refuse("pip", "uninstall", "x", needle="not allowed")
        self.refuse("pip", "list", "--user", needle="not allowed")
        self.refuse("pip", "show", "https://x/y.whl", needle="local file")
        self.refuse("pip", "install", "--isolated", "--only-binary=:all:", "requests",
                    needle="must target an approved root")
        self.refuse("python", "-I", "-m", "pip", "install", "--isolated", "--target", root, "x",
                    needle="--only-binary")
        self.refuse("python", "-I", "-m", "pip", "install", "--isolated", "--only-binary=:all:",
                    "--target", root, "git+https://example.invalid/x",
                    needle="plain name")
        self.refuse("python", "-I", "-m", "pip", "install", "--isolated", "--only-binary=:all:",
                    "--target", root, "./local", needle="plain name")
        self.refuse("python", "-I", "-m", "pip", "install", "--isolated", "--only-binary=:all:",
                    "--target", root, "--index-url", "http://evil", "x",
                    needle="not allowed")
        self.refuse("python", "-I", "-m", "pip", "install", "--isolated", "--only-binary=:all:",
                    "--target", root, "-r", "reqs.txt", needle="not allowed")
        self.refuse("python", "-I", "-m", "pip", "install", "--isolated", "--only-binary=:all:",
                    "--target", os.path.join(self.tmp, "out"), "x",
                    needle="outside every approved root")
        self.refuse("python", "-I", "-m", "pip", "install", "--isolated", "--only-binary=:all:",
                    "--target", needle="needs a value")
        self.refuse("python", "-I", "-m", "pip", "install", "--isolated", "--only-binary=:all:",
                    "--target", root, needle="at least one package")
        self.refuse("python", "-I", "-m", "pip", needle="may only run")  # no subcommand

    def test_source_builds_only_when_policy_allows(self):
        policy = pv.ProvisionPolicy(approved_roots=(self.root,),
                                    allow_source_builds=True)
        adm = pv.classify_argv(
            ["python", "-I", "-m", "pip", "install", "--isolated", "--target", self.work(), "x"],
            policy)
        self.assertEqual(adm.rule, "pip-install")

    def test_inline_flag_values(self):
        adm = self.admit("python", "-I", "-m", "pip", "install", "--isolated", "--only-binary=:all:",
                         "--target=" + self.work(), "x")
        self.assertEqual(adm.minimum_class, pv.MUTATING)

    def test_npm_refusals(self):
        self.refuse("npm", needle="needs a subcommand")
        self.refuse("npm", "publish", needle="not allowed")
        self.refuse("npm", "install", "-g", "x", needle="not allowed")
        self.refuse("npm", "install", "x", needle="--prefix")
        self.refuse("npm", "ls", needle="--prefix")
        self.refuse("npm", "install", "x", "--prefix", self.root,
                    needle="--ignore-scripts")
        self.refuse("npm", "install", "x", "--prefix", self.tmp,
                    "--ignore-scripts", needle="outside every approved root")
        self.refuse("npm", "install", "http://evil/x", "--prefix", self.root,
                    "--ignore-scripts", needle="plain name")
        self.refuse("npm", "ls", "../x", "--prefix", self.root,
                    needle="plain name")

    def test_system_manager_refusals(self):
        self.refuse("winget", needle="needs a subcommand")
        self.refuse("winget", "uninstall", "--id", "X", needle="not allowed")
        self.refuse("winget", "install", "Pkg.Id", needle="--id")
        self.refuse("winget", "install", "--id", "Pkg.Id", "extra", needle="--id")
        self.refuse("choco", "install", "-y", needle="at least one package")
        self.refuse("brew", "install", "https://x/y.rb", needle="local file")
        self.refuse("choco", "install", "--version", "1;2", "x", needle="not plain")
        self.refuse("brew", "list", "a/b/c", needle="plain name")
        self.refuse("apt", "install", "--allow-unauthenticated", "x",
                    needle="not allowed")

    def test_mkdir_refusals(self):
        self.refuse("mkdir", needle="exactly one directory")
        self.refuse("mkdir", "-p", self.work(), needle="exactly one directory")
        self.refuse("mkdir", self.work(), "b", needle="exactly one directory")
        self.refuse("mkdir", os.path.join(self.tmp, "outside"),
                    needle="outside every approved root")
        self.refuse("mkdir", os.path.join(self.root, "..", "escape"),
                    needle="outside every approved root")
        # an absolute-path "mkdir" is some other binary, not the builtin
        self.refuse(os.path.join(self.tmp, "mkdir"), self.work(),
                    needle="not a trusted executable")

    def test_no_approved_root_means_no_path_rule_passes(self):
        bare = pv.ProvisionPolicy()
        with self.assertRaises(pv.ProvisionError) as ctx:
            pv.classify_argv(["mkdir", self.work()], bare)
        self.assertIn("no approved root", str(ctx.exception))


class StepAndPlanValidationTests(TempRootCase):
    def good_mkdir(self, **over):
        base = dict(id="mk", argv=("mkdir", self.work()),
                    classification=pv.MUTATING, rollback_hint="delete it",
                    expected_effect="creates a dir")
        base.update(over)
        return step(**base)

    def test_valid_step_returns_admission(self):
        self.assertEqual(pv.validate_step(self.good_mkdir(), self.policy).rule,
                         "mkdir")

    def test_under_classification_is_rejected_over_is_fine(self):
        with self.assertRaises(pv.ProvisionError) as ctx:
            pv.validate_step(self.good_mkdir(classification=pv.READ), self.policy)
        self.assertIn("at least mutating", str(ctx.exception))
        pv.validate_step(self.good_mkdir(classification=pv.IRREVERSIBLE),
                         self.policy)

    def test_hidden_network_use_is_rejected(self):
        install = step(id="i", argv=("brew", "install", "git"),
                       classification=pv.IRREVERSIBLE, rollback_hint="x")
        with self.assertRaises(pv.ProvisionError) as ctx:
            pv.validate_step(install, self.policy)
        self.assertIn("requires_network must be true", str(ctx.exception))

    def test_mutating_step_needs_a_rollback_hint(self):
        with self.assertRaises(pv.ProvisionError) as ctx:
            pv.validate_step(self.good_mkdir(rollback_hint="  "), self.policy)
        self.assertIn("rollback_hint", str(ctx.exception))

    def test_malformed_fields_are_all_reported(self):
        bad = step(id="Bad Id!", description=" ", expected_effect="",
                   classification="whatever", argv=("rm", "-rf", "/"),
                   requires_network="yes", cwd=self.tmp)
        with self.assertRaises(pv.ProvisionError) as ctx:
            pv.validate_step(bad, self.policy)
        text = " ".join(ctx.exception.problems)
        for needle in ("lowercase", "description", "expected_effect",
                       "classification", "requires_network", "cwd",
                       "never allowed"):
            self.assertIn(needle, text)

    def test_cwd_is_never_selectable(self):
        with self.assertRaises(pv.ProvisionError) as ctx:
            pv.validate_step(step(cwd=self.root), self.policy)
        self.assertIn("cwd is not selectable", str(ctx.exception))

    def test_plan_defects_are_aggregated(self):
        dup = [step(id="a"), step(id="a"), step(id="b", argv=("rm", "x"))]
        plan = pv.Plan(" ", dup, self.root, "x", "x", True)
        with self.assertRaises(pv.ProvisionError) as ctx:
            pv.validate_plan(plan, self.policy)
        text = " ".join(ctx.exception.problems)
        self.assertIn("goal must be non-empty", text)
        self.assertIn("duplicate step id 'a'", text)
        self.assertIn("never allowed", text)
        empty = pv.Plan("g", [], self.root, "x", "x", True)
        with self.assertRaises(pv.ProvisionError):
            pv.validate_plan(empty, self.policy)

    def test_step_limit(self):
        policy = pv.ProvisionPolicy(approved_roots=(self.root,), max_steps=1)
        plan = pv.Plan("g", [step(id="a"), step(id="b")], self.root, "x", "x", True)
        with self.assertRaises(pv.ProvisionError) as ctx:
            pv.validate_plan(plan, policy)
        self.assertIn("exceeds the limit", str(ctx.exception))

    def test_valid_plan_returns_one_admission_per_step(self):
        plan = pv.Plan("g", [step(id="a"), self.good_mkdir()], self.root,
                       "x", "x", True)
        self.assertEqual(len(pv.validate_plan(plan, self.policy)), 2)

    def test_needs_approval_rule(self):
        self.assertFalse(pv.needs_approval(step()))
        self.assertTrue(pv.needs_approval(step(requires_network=True)))
        self.assertTrue(pv.needs_approval(step(classification=pv.MUTATING)))


# --------------------------------------------------------------------------
# approval gate
# --------------------------------------------------------------------------

def mutating_plan(root, n=2, irreversible_last=False):
    steps = []
    for index in range(n):
        steps.append(pv.Step(
            f"mk{index}", "make", ("mkdir", os.path.join(root, f"d{index}")),
            pv.MUTATING, "creates a directory", "delete it"))
    if irreversible_last:
        steps.append(pv.Step(
            "inst", "install", ("brew", "install", "git"), pv.IRREVERSIBLE,
            "changes the system", "brew uninstall git", requires_network=True))
    return pv.Plan("make dirs", steps, root, "x", "x", True)


class ApprovalGateTests(TempRootCase):
    def test_no_record_means_no_consent(self):
        plan = mutating_plan(self.root)
        gate = pv.ApprovalGate()
        self.assertIsNone(gate.resolve(plan, plan.steps[0]))
        self.assertIsNone(gate.resolve_or_ask(plan, plan.steps[0]))

    def test_plan_approval_covers_mutating_but_never_irreversible(self):
        plan = mutating_plan(self.root, irreversible_last=True)
        gate = pv.ApprovalGate()
        gate.approve_plan(plan, "alice", "looks right")
        self.assertEqual(gate.resolve(plan, plan.steps[0]).decision, pv.APPROVE)
        self.assertIsNone(gate.resolve(plan, plan.step("inst")))
        gate.approve_step(plan, "inst", "alice")
        self.assertEqual(gate.resolve(plan, plan.step("inst")).scope, "step")

    def test_decline_and_defer_are_first_class_records(self):
        plan = mutating_plan(self.root)
        gate = pv.ApprovalGate()
        gate.defer_plan(plan, "alice", "not today")
        self.assertEqual(gate.resolve(plan, plan.steps[0]).decision, pv.DEFER)
        gate.decline_plan(plan, "alice", "no")
        self.assertEqual(gate.resolve(plan, plan.steps[0]).decision, pv.DECLINE)
        # a later plan-level approve supersedes
        gate.approve_plan(plan, "alice")
        self.assertEqual(gate.resolve(plan, plan.steps[0]).decision, pv.APPROVE)
        gate.decline_step(plan, "mk1", "alice", "this one no")
        self.assertEqual(gate.resolve(plan, plan.steps[1]).decision, pv.DECLINE)
        gate.defer_step(plan, "mk1", "alice", "later")
        self.assertEqual(gate.resolve(plan, plan.steps[1]).decision, pv.DEFER)

    def test_plan_level_decline_beats_step_level_approve(self):
        plan = mutating_plan(self.root)
        gate = pv.ApprovalGate()
        gate.approve_step(plan, "mk0", "alice")
        gate.decline_plan(plan, "alice", "changed my mind")
        self.assertEqual(gate.resolve(plan, plan.steps[0]).decision, pv.DECLINE)

    def test_approval_is_bound_to_the_plan_digest(self):
        plan = mutating_plan(self.root)
        gate = pv.ApprovalGate()
        gate.approve_plan(plan, "alice")
        edited = pv.Plan(plan.goal, list(plan.steps) + [pv.Step(
            "extra", "x", ("mkdir", os.path.join(self.root, "x")), pv.MUTATING,
            "e", "r")], plan.root, "x", "x", True)
        self.assertNotEqual(plan.digest, edited.digest)
        self.assertIsNone(gate.resolve(edited, edited.steps[0]))

    def test_step_approval_is_bound_to_the_step_digest(self):
        plan = mutating_plan(self.root)
        gate = pv.ApprovalGate()
        gate.approve_step(plan, "mk0", "alice")
        tampered_step = pv.Step("mk0", "make", ("mkdir", os.path.join(self.root, "zz")),
                                pv.MUTATING, "creates a directory", "delete it")
        self.assertIsNone(gate.resolve(plan, tampered_step))

    def test_invalid_records_are_refused(self):
        plan = mutating_plan(self.root)
        gate = pv.ApprovalGate()
        with self.assertRaises(pv.ProvisionError):
            gate.record(plan, "maybe", approver="alice")
        with self.assertRaises(pv.ProvisionError):
            gate.record(plan, pv.APPROVE, approver="  ")
        with self.assertRaises(pv.ProvisionError):
            gate.record(plan, pv.APPROVE, approver=None)
        with self.assertRaises(pv.ProvisionError):
            gate.approve_step(plan, "no-such-step", "alice")
        self.assertEqual(gate.records, ())

    def test_flagged_plans_cannot_be_approved_whole(self):
        plan = pv.Plan("g", mutating_plan(self.root).steps, self.root, "x", "x",
                       True, review_required=True)
        gate = pv.ApprovalGate()
        with self.assertRaises(pv.ProvisionError):
            gate.approve_plan(plan, "alice")
        gate.approve_step(plan, "mk0", "alice")  # per-step still possible
        gate.decline_plan(plan, "alice")  # and declining is always possible

    def test_every_record_is_ledgered_with_its_own_timestamp_key(self):
        plan = mutating_plan(self.root)
        gate = pv.ApprovalGate(ledger=self.ledger, task_id="t1")
        gate.approve_plan(plan, "alice", "ok")
        gate.decline_step(plan, "mk1", "alice", "nope")
        entries = self.events("provision_approval")
        self.assertEqual([e["decision"] for e in entries], ["approve", "decline"])
        self.assertEqual(entries[1]["step_id"], "mk1")
        self.assertEqual(entries[0]["task_id"], "t1")
        self.assertIn("decided_at", entries[0])
        self.assertEqual(entries[0]["plan_digest"], plan.digest)
        self.assertEqual(self.ledger.verify(), (True, None))

    def test_ask_hook_outcomes(self):
        plan = mutating_plan(self.root)
        step0 = plan.steps[0]

        def gate_for(answer):
            return pv.ApprovalGate(ask=lambda p, s: answer, ask_approver="ui")

        record = gate_for((pv.APPROVE, "fine")).resolve_or_ask(plan, step0)
        self.assertEqual((record.decision, record.approver), (pv.APPROVE, "ui"))
        self.assertEqual(
            gate_for((pv.DECLINE, "no")).resolve_or_ask(plan, step0).decision,
            pv.DECLINE)
        self.assertIsNone(gate_for(None).resolve_or_ask(plan, step0))
        # malformed or unrecognised answers become a recorded DEFER
        for answer in ("approve", (1, 2, 3), ("yes please", "x"), (None, "why")):
            got = gate_for(answer).resolve_or_ask(plan, step0)
            self.assertEqual(got.decision, pv.DEFER, msg=repr(answer))

        def boom(p, s):
            raise RuntimeError("ui crashed")

        failed = pv.ApprovalGate(ask=boom).resolve_or_ask(plan, step0)
        self.assertEqual(failed.decision, pv.DEFER)
        self.assertIn("RuntimeError", failed.reason)

    def test_existing_record_short_circuits_ask(self):
        plan = mutating_plan(self.root)
        asked = []
        gate = pv.ApprovalGate(ask=lambda p, s: asked.append(s) or (pv.APPROVE, ""))
        gate.approve_plan(plan, "alice")
        gate.resolve_or_ask(plan, plan.steps[0])
        self.assertEqual(asked, [])


# --------------------------------------------------------------------------
# executor
# --------------------------------------------------------------------------

class ExecutorTests(TempRootCase):
    def run_plan(self, plan, gate, **kw):
        runner = kw.pop("runner", FakeRunner())
        kw.setdefault("which", fake_which)
        report = pv.execute_plan(plan, gate, policy=self.policy,
                                 ledger=self.ledger, task_id="t", runner=runner,
                                 **kw)
        return report, runner

    def test_dry_run_is_the_default_and_does_nothing(self):
        target = os.path.join(self.root, "d0")
        plan = mutating_plan(self.root)
        runner = FakeRunner()
        report = pv.execute_plan(plan, pv.ApprovalGate(), policy=self.policy,
                                 ledger=self.ledger, runner=runner)
        self.assertTrue(report.dry_run)
        self.assertEqual(report.outcome, pv.DRY_RUN)
        self.assertEqual(runner.calls, [])
        self.assertFalse(os.path.exists(target))
        self.assertTrue(all(r.status == pv.DRY_RUN for r in report.results))
        self.assertIn("approval: missing", report.results[0].note)
        self.assertEqual(report.rollback_hints, ())
        for entry in self.events("provision_step"):
            self.assertTrue(entry["dry_run"])

    def test_dry_run_reports_approval_state_without_asking(self):
        plan = mutating_plan(self.root, irreversible_last=True)
        asked = []
        gate = pv.ApprovalGate(ask=lambda p, s: asked.append(s))
        gate.approve_step(plan, "mk0", "a")
        gate.decline_step(plan, "mk1", "a")
        gate.defer_step(plan, "inst", "a")
        report = pv.execute_plan(plan, gate, policy=self.policy,
                                 runner=FakeRunner())
        notes = [r.note for r in report.results]
        self.assertIn("approval: approved", notes[0])
        self.assertIn("approval: declined", notes[1])
        self.assertIn("approval: deferred", notes[2])
        self.assertEqual(asked, [])  # a preview never prompts

    def test_dry_run_read_step_needs_no_approval(self):
        plan = pv.Plan("g", [step()], self.root, "x", "x", True)
        report = pv.execute_plan(plan, pv.ApprovalGate(), policy=self.policy,
                                 runner=FakeRunner())
        self.assertIn("approval: not required", report.results[0].note)

    def test_real_run_without_approval_executes_nothing(self):
        plan = mutating_plan(self.root)
        report, runner = self.run_plan(plan, pv.ApprovalGate(), dry_run=False)
        self.assertEqual(report.outcome, pv.AWAITING_APPROVAL)
        self.assertTrue(report.resumable)
        self.assertEqual([r.status for r in report.results],
                         [pv.AWAITING_APPROVAL, pv.SKIPPED])
        self.assertEqual(runner.calls, [])
        self.assertFalse(os.path.exists(os.path.join(self.root, "d0")))

    def test_approved_run_executes_in_order_and_ledgers_everything(self):
        plan = pv.Plan("g", [
            step(id="probe", argv=("python", "--version")),
            pv.Step("mk", "make", ("mkdir", self.work()), pv.MUTATING,
                    "creates", "delete it"),
            pv.Step("pipx", "install", ("python", "-I", "-m", "pip", "install", "--isolated",
                    "--only-binary=:all:", "--target", self.work(), "ruff"),
                    pv.MUTATING, "installs", "delete it", requires_network=True),
        ], self.root, "x", "x", True)
        gate = pv.ApprovalGate(ledger=self.ledger, task_id="t")
        gate.approve_plan(plan, "alice")
        report, runner = self.run_plan(plan, gate, dry_run=False)
        self.assertEqual(report.outcome, pv.COMPLETED)
        self.assertEqual(report.completed_ids(), ("probe", "mk", "pipx"))
        self.assertTrue(os.path.isdir(self.work()))  # builtin mkdir ran for real
        # mkdir is a builtin, never a subprocess
        self.assertEqual([a[0] for a in runner.argvs()], ["python", "python"])
        self.assertTrue(all(c["timeout"] == pv.DEFAULT_STEP_TIMEOUT_S
                            for c in runner.calls))
        self.assertEqual(report.rollback_hints, ())
        self.assertFalse(report.resumable)
        self.assertEqual(report.results[1].approver, "alice")
        # ledger: plan, approval, 3 steps, end -- one chain
        names = [e["event"] for e in self.ledger.entries()]
        self.assertEqual(names, [
            "provision_approval", "provision_plan",
            "provision_step_start", "provision_step",   # python --version
            "provision_step_start", "provision_step",   # mkdir (builtin)
            "provision_step_start", "provision_step",   # pip install
            "provision_end"])
        self.assertEqual(self.ledger.verify(), (True, None))
        end = self.events("provision_end")[0]
        self.assertEqual(end["outcome"], pv.COMPLETED)
        self.assertEqual(end["plan_digest"], plan.digest)
        self.assertEqual(report.to_dict()["outcome"], pv.COMPLETED)

    def test_ledger_chain_survives_reload(self):
        plan = mutating_plan(self.root, n=1)
        gate = pv.ApprovalGate(ledger=self.ledger)
        gate.approve_plan(plan, "a")
        self.run_plan(plan, gate, dry_run=False)
        reloaded = AutonomyLedger(self.ledger.path)
        self.assertEqual(reloaded.verify(), (True, None))
        self.assertEqual(len(reloaded.entries()), len(self.ledger.entries()))

    def test_failure_stops_skips_the_rest_and_reports_rollback(self):
        plan = pv.Plan("g", [
            pv.Step("mk", "make", ("mkdir", self.work()), pv.MUTATING,
                    "creates", "delete the dir"),
            pv.Step("inst", "install", ("python", "-I", "-m", "pip", "install", "--isolated",
                    "--only-binary=:all:", "--target", self.work(), "x"),
                    pv.MUTATING, "installs", "remove the target",
                    requires_network=True),
            step(id="verify", argv=("python", "--version")),
        ], self.root, "x", "x", True)
        gate = pv.ApprovalGate()
        gate.approve_plan(plan, "a")
        runner = FakeRunner({("python", "-I", "-m", "pip"):
                             osal.CommandResult(1, "", "no wheel")})
        report, _ = self.run_plan(plan, gate, dry_run=False, runner=runner)
        self.assertEqual(report.outcome, pv.FAILED)
        self.assertEqual([r.status for r in report.results],
                         [pv.COMPLETED, pv.FAILED, pv.SKIPPED])
        self.assertEqual(report.results[1].returncode, 1)
        self.assertEqual(report.results[1].note, "non-zero exit")
        self.assertEqual(len(runner.calls), 1)  # verify never ran
        self.assertEqual(report.rollback_hints,
                         (("inst", "remove the target"), ("mk", "delete the dir")))
        end = self.events("provision_end")[0]
        self.assertEqual(end["completed"], ["mk"])
        self.assertEqual(len(end["rollback_hints"]), 2)

    def test_timeout_and_runner_errors_are_failures(self):
        plan = pv.Plan("g", [step(id="slow")], self.root, "x", "x", True)
        for outcome, note in (
                (osal.CommandResult(124, "timed out"), "timed out"),
                (OSError("exec format"), "non-zero exit")):
            runner = FakeRunner({("python",): outcome})
            report, _ = self.run_plan(plan, pv.ApprovalGate(), dry_run=False,
                                      runner=runner)
            self.assertEqual(report.outcome, pv.FAILED)
            self.assertEqual(report.results[0].note, note)
        self.assertIn("runner error", report.results[0].stderr)

    def test_mkdir_failure_is_reported(self):
        blocker = self.work()
        with open(blocker, "w", encoding="utf-8") as handle:
            handle.write("a file where the directory should go")
        plan = pv.Plan("g", [pv.Step("mk", "make", ("mkdir", blocker),
                                     pv.MUTATING, "creates", "delete")],
                       self.root, "x", "x", True)
        gate = pv.ApprovalGate()
        gate.approve_plan(plan, "a")
        report, _ = self.run_plan(plan, gate, dry_run=False)
        self.assertEqual(report.outcome, pv.FAILED)
        self.assertIn("mkdir failed", report.results[0].stderr)

    def test_decline_stops_and_records_the_reason(self):
        plan = mutating_plan(self.root)
        gate = pv.ApprovalGate(ledger=self.ledger)
        gate.approve_step(plan, "mk0", "a")
        gate.decline_step(plan, "mk1", "a", "not that directory")
        report, runner = self.run_plan(plan, gate, dry_run=False)
        self.assertEqual(report.outcome, pv.DECLINED)
        self.assertFalse(report.resumable)
        self.assertEqual([r.status for r in report.results],
                         [pv.COMPLETED, pv.DECLINED])
        self.assertEqual(report.results[1].note, "not that directory")
        self.assertTrue(os.path.isdir(os.path.join(self.root, "d0")))
        self.assertFalse(os.path.exists(os.path.join(self.root, "d1")))
        # progress made before the decline still reports how to undo it
        self.assertEqual(report.rollback_hints, (("mk0", "delete it"),))
        steps = self.events("provision_step")
        self.assertEqual([e["status"] for e in steps],
                         [pv.COMPLETED, pv.DECLINED])

    def test_defer_is_resumable_and_resume_skips_completed_work(self):
        plan = mutating_plan(self.root)
        gate = pv.ApprovalGate()
        gate.approve_step(plan, "mk0", "a")
        gate.defer_step(plan, "mk1", "a", "ask me tomorrow")
        first, _ = self.run_plan(plan, gate, dry_run=False)
        self.assertEqual(first.outcome, pv.DEFERRED)
        self.assertTrue(first.resumable)
        gate.approve_step(plan, "mk1", "a", "tomorrow")
        second, runner = self.run_plan(plan, gate, dry_run=False,
                                       done=first.completed_ids())
        self.assertEqual(second.outcome, pv.COMPLETED)
        self.assertEqual([r.status for r in second.results],
                         [pv.ALREADY_DONE, pv.COMPLETED])
        self.assertTrue(os.path.isdir(os.path.join(self.root, "d1")))

    def test_interactive_ask_approves_during_the_run(self):
        plan = mutating_plan(self.root, n=1)
        gate = pv.ApprovalGate(ledger=self.ledger,
                               ask=lambda p, s: (pv.APPROVE, "yes"),
                               ask_approver="card")
        report, _ = self.run_plan(plan, gate, dry_run=False)
        self.assertEqual(report.outcome, pv.COMPLETED)
        self.assertEqual(report.results[0].approver, "card")
        self.assertEqual(len(self.events("provision_approval")), 1)

    def test_read_step_with_network_still_needs_approval(self):
        plan = pv.Plan("g", [step(id="net", requires_network=True)], self.root,
                       "x", "x", True)
        report, runner = self.run_plan(plan, pv.ApprovalGate(), dry_run=False)
        self.assertEqual(report.outcome, pv.AWAITING_APPROVAL)
        self.assertEqual(runner.calls, [])

    def test_read_only_step_runs_without_approval(self):
        plan = pv.Plan("g", [step()], self.root, "x", "x", True)
        report, runner = self.run_plan(plan, pv.ApprovalGate(), dry_run=False)
        self.assertEqual(report.outcome, pv.COMPLETED)
        self.assertEqual(len(runner.calls), 1)
        self.assertIsNone(report.results[0].approval)
        # a READ step that succeeded is not something to roll back
        self.assertEqual(report.rollback_hints, ())

    def test_irreversible_needs_its_own_approval_in_a_real_run(self):
        plan = mutating_plan(self.root, n=1, irreversible_last=True)
        gate = pv.ApprovalGate()
        gate.approve_plan(plan, "a")
        report, runner = self.run_plan(plan, gate, dry_run=False)
        self.assertEqual(report.outcome, pv.AWAITING_APPROVAL)
        self.assertEqual(report.results[-1].step_id, "inst")
        self.assertNotIn(["brew", "install", "git"], runner.argvs())
        gate.approve_step(plan, "inst", "a")
        report, runner = self.run_plan(plan, gate, dry_run=False,
                                       done=("mk0",))
        self.assertEqual(report.outcome, pv.COMPLETED)
        self.assertEqual(runner.argvs(), [["brew", "install", "git"]])

    def test_invalid_plan_is_refused_before_anything_is_ledgered(self):
        bad = pv.Plan("g", [step(argv=("sudo", "rm", "-rf", "/"))], self.root,
                      "x", "x", True)
        with self.assertRaises(pv.ProvisionError):
            pv.execute_plan(bad, pv.ApprovalGate(), policy=self.policy,
                            ledger=self.ledger, runner=FakeRunner(),
                            dry_run=False, which=fake_which)
        self.assertEqual(self.ledger.entries(), [])

    def test_output_is_truncated_in_the_ledger_but_hashed_whole(self):
        policy = pv.ProvisionPolicy(approved_roots=(self.root,),
                                    max_output_chars=10)
        plan = pv.Plan("g", [step()], self.root, "x", "x", True)
        runner = FakeRunner(default=osal.CommandResult(0, "x" * 100, ""))
        report = pv.execute_plan(plan, pv.ApprovalGate(), policy=policy,
                                 ledger=self.ledger, runner=runner,
                                 dry_run=False, which=fake_which)
        self.assertEqual(len(report.results[0].stdout), 100)
        logged = self.events("provision_step")[0]
        self.assertTrue(logged["stdout"].startswith("...[truncated]"))
        self.assertEqual(logged["output_sha256"], pv._sha256("x" * 100 + "\x00"))

    def test_a_dry_run_needs_no_ledger_but_a_real_run_refuses_without_one(self):
        plan = pv.Plan("g", [step()], self.root, "x", "x", True)
        preview = pv.execute_plan(plan, pv.ApprovalGate(), policy=self.policy,
                                  runner=FakeRunner())
        self.assertEqual(preview.outcome, pv.DRY_RUN)
        runner = FakeRunner()
        with self.assertRaises(pv.ProvisionError) as ctx:
            pv.execute_plan(plan, pv.ApprovalGate(), policy=self.policy,
                            runner=runner, dry_run=False, which=fake_which)
        self.assertIn("ledger", str(ctx.exception))
        self.assertEqual(runner.calls, [])

    def test_default_runner_is_the_osal_seam(self):
        # No runner passed: a bare '--version' of the running interpreter
        # proves the default routes through osal.run for real, read-only.
        plan = pv.Plan("g", [step(argv=(osal.python_exe(), "--version"))],
                       self.root, "x", "x", True)
        policy = pv.ProvisionPolicy(approved_roots=(self.root,),
                                    trusted_executables=(osal.python_exe(),))
        report = pv.execute_plan(plan, pv.ApprovalGate(), policy=policy,
                                 ledger=self.ledger, dry_run=False)
        self.assertEqual(report.outcome, pv.COMPLETED)
        self.assertIn("Python", report.results[0].stdout + report.results[0].stderr)


# --------------------------------------------------------------------------
# planner
# --------------------------------------------------------------------------

class FakeJev:
    """Stands in for JevPolicy.evaluate_provision; counts calls."""

    def __init__(self, choice=None, noul=None, fallback=False, raises=None):
        self.choice, self.noul = choice, noul or {}
        self.fallback, self.raises = fallback, raises
        self.calls = []

    def evaluate_provision(self, state, questions, **kw):
        self.calls.append((state, questions))
        if self.raises:
            raise self.raises
        answers = {}
        if "recipe" in questions:
            answers["recipe"] = {"choice": self.choice}
        for key, value in self.noul.items():
            answers[key] = {"noul": value}
        return JevEvaluationResult(
            "pass", 0.8, 1.0, answers, [], is_fallback=self.fallback,
            model="jev-test"), {}


class ExtractRequestsTests(unittest.TestCase):
    def test_pinned_python_and_node_tokens(self):
        got = pv.extract_requests("provision ruff==0.6.1 and left-pad@1.3.0")
        self.assertEqual(
            got, (pv.PackageRequest("ruff", "0.6.1", "python"),
                  pv.PackageRequest("left-pad", "1.3.0", "node")))

    def test_verb_form_with_ecosystem_hint(self):
        self.assertEqual(pv.extract_requests("install the ruff via pip"),
                         (pv.PackageRequest("ruff", None, "python"),))
        self.assertEqual(pv.extract_requests("set up prettier with npm"),
                         (pv.PackageRequest("prettier", None, "node"),))
        self.assertEqual(pv.extract_requests("install git with winget")[0].ecosystem,
                         "system")
        self.assertIsNone(pv.extract_requests("install git")[0].ecosystem)

    def test_ambiguous_hints_and_stopwords_yield_nothing_invented(self):
        both = pv.extract_requests("install mytool with pip or npm")
        self.assertEqual(both, (pv.PackageRequest("mytool", None, None),))
        self.assertEqual(pv.extract_requests("install the tool"), ())
        self.assertEqual(pv.extract_requests(""), ())
        self.assertEqual(pv.extract_requests(None), ())
        dup = pv.extract_requests("install ruff==1.0 ruff==1.0")
        self.assertEqual(len(dup), 1)

    def test_package_request_specs(self):
        pinned = pv.PackageRequest("x", "1.0")
        self.assertEqual((pinned.pip_spec(), pinned.npm_spec()), ("x==1.0", "x@1.0"))
        bare = pv.PackageRequest("x")
        self.assertEqual((bare.pip_spec(), bare.npm_spec()), ("x", "x"))


class PlannerTests(TempRootCase):
    def plan(self, goal="provision ruff==0.6.1", probe=None, **kw):
        return pv.plan_provision(goal, probe or self.probe(), policy=self.policy,
                                 **kw)

    def test_offline_python_plan_is_deterministic_reviewable_and_valid(self):
        plan = self.plan(probe=self.probe(managers=("pip",)))
        self.assertEqual(plan.recipe, pv.RECIPE_VENV_PIP)
        self.assertTrue(plan.is_fallback)
        self.assertEqual(plan.source, "deterministic")
        self.assertEqual([s.id for s in plan.steps],
                         ["mkdir-root", "create-venv", "pip-install",
                          "verify-1-ruff"])
        self.assertEqual([s.classification for s in plan.steps],
                         [pv.MUTATING, pv.MUTATING, pv.MUTATING, pv.MUTATING])
        self.assertTrue(plan.steps[2].requires_network)
        self.assertIn("ruff==0.6.1", plan.steps[2].argv)
        self.assertIn("--only-binary=:all:", plan.steps[2].argv)
        self.assertTrue(os.path.normcase(plan.root).startswith(
            os.path.normcase(self.root)))
        self.assertTrue(plan.steps[2].argv[0].endswith(os.path.join("bin", "python")))
        pv.validate_plan(plan, self.policy)  # idempotent re-validation
        self.assertEqual(plan.digest, self.plan(
            probe=self.probe(managers=("pip",))).digest)
        self.assertIn("only one recipe fits", plan.notes[0])

    def test_windows_venv_interpreter_path(self):
        plan = self.plan(probe=self.probe(facts=WINDOWS, managers=("pip",)))
        self.assertTrue(plan.steps[2].argv[0].endswith(
            os.path.join("Scripts", "python.exe")))

    def test_npm_plan(self):
        plan = self.plan("provision left-pad@1.3.0")
        self.assertEqual(plan.recipe, pv.RECIPE_NPM_PREFIX)
        install = plan.steps[1]
        self.assertIn("--ignore-scripts", install.argv)
        self.assertIn("left-pad@1.3.0", install.argv)
        self.assertEqual(plan.steps[2].classification, pv.READ)

    def test_system_plan_is_irreversible_per_package(self):
        probe = self.probe(managers=("apt",))
        plan = self.plan("install git with apt", probe=probe)
        self.assertEqual(plan.recipe, pv.RECIPE_SYSTEM_PM)
        self.assertEqual(plan.steps[0].classification, pv.IRREVERSIBLE)
        self.assertEqual(plan.steps[0].argv, ("apt", "install", "-y", "git"))
        self.assertEqual(plan.steps[1].argv, ("apt", "list", "--installed", "git"))
        self.assertEqual(plan.steps[1].classification, pv.READ)

    def test_system_plan_per_manager(self):
        cases = (
            (WINDOWS, ("winget",), ("winget", "install", "--id", "Git.Git",
                                    "--exact", "--version", "2.0")),
            (WINDOWS, ("choco",), ("choco", "install", "--version", "2.0", "-y",
                                   "git")),
            (WINDOWS, ("scoop",), ("scoop", "install", "git")),
            (dict(LINUX, os="Darwin"), ("brew",), ("brew", "install", "git")),
        )
        for facts, managers, expected in cases:
            probe = self.probe(facts=facts, managers=managers)
            name = "Git.Git" if managers == ("winget",) else "git"
            plan = pv.plan_provision(
                "install git with system", probe, policy=self.policy,
                packages=[pv.PackageRequest(name, "2.0", "system")])
            self.assertEqual(plan.steps[0].argv, expected, msg=managers)

    def test_inventory_when_nothing_installable_is_named(self):
        plan = self.plan("make my machine nicer")
        self.assertEqual(plan.recipe, pv.RECIPE_INVENTORY)
        self.assertTrue(all(s.classification == pv.READ for s in plan.steps))
        self.assertIn("no installable package", plan.notes[0])
        self.assertEqual([s.id for s in plan.steps],
                         ["python-version", "pip-version", "npm-version"])
        self.assertTrue(plan.is_fallback)

    def test_inventory_when_no_recipe_fits_this_host(self):
        policy = pv.ProvisionPolicy(approved_roots=(self.root,),
                                    allow_system_install=False)
        probe = self.probe(managers=())
        plan = pv.plan_provision("install git with apt", probe, policy=policy)
        self.assertEqual(plan.recipe, pv.RECIPE_INVENTORY)
        self.assertIn("no setup recipe fits", plan.notes[0])

    def test_no_approved_root_means_no_private_recipe(self):
        policy = pv.ProvisionPolicy(allow_system_install=False)
        plan = pv.plan_provision("provision ruff==1.0", self.probe(), policy=policy)
        self.assertEqual(plan.recipe, pv.RECIPE_INVENTORY)
        self.assertEqual(plan.root, "")

    def test_root_must_be_inside_an_approved_root(self):
        with self.assertRaises(pv.ProvisionError):
            self.plan(root=os.path.join(self.tmp, "elsewhere"))
        plan = self.plan(root=os.path.join(self.root, "custom"))
        self.assertTrue(plan.root.endswith("custom"))

    def test_empty_goal_is_refused(self):
        for goal in ("", "   ", None):
            with self.assertRaises(pv.ProvisionError):
                self.plan(goal)

    def test_ambiguous_ecosystem_lists_all_applicable_recipes(self):
        probe = self.probe(managers=("pip", "npm", "apt"))
        req = [pv.PackageRequest("tool")]
        self.assertEqual(pv.applicable_recipes(req, probe, self.policy),
                         [pv.RECIPE_VENV_PIP, pv.RECIPE_NPM_PREFIX,
                          pv.RECIPE_SYSTEM_PM])
        self.assertEqual(pv.applicable_recipes([], probe, self.policy), [])
        node = [pv.PackageRequest("tool", None, "node")]
        self.assertEqual(pv.applicable_recipes(node, probe, self.policy),
                         [pv.RECIPE_NPM_PREFIX])

    # ---- Jev: selection + advisory review ------------------------------

    def ambiguous_probe(self):
        return self.probe(managers=("pip", "npm", "apt"))

    def test_jev_choice_among_declared_recipes_is_used(self):
        jev = FakeJev(choice=pv.RECIPE_NPM_PREFIX,
                      noul={"satisfies_goal": 0.9, "exceeds_goal": 0.1})
        plan = self.plan("install mytool", probe=self.ambiguous_probe(), jev=jev,
                         task_id="t9")
        self.assertEqual(plan.recipe, pv.RECIPE_NPM_PREFIX)
        self.assertEqual(plan.source, "jev")
        self.assertFalse(plan.is_fallback)
        self.assertFalse(plan.review_required)
        self.assertEqual(plan.review["satisfies_goal"], 0.9)
        select_state, select_questions = jev.calls[0]
        self.assertEqual(set(select_questions["recipe"]["criteria"]),
                         {"venv-pip", "npm-prefix", "system-pm"})
        self.assertNotIn("python_executable", select_state["host"])
        self.assertEqual(len(jev.calls), 2)  # selection + review

    def test_jev_choice_outside_the_declared_set_is_refused(self):
        jev = FakeJev(choice="curl-pipe-bash")
        plan = self.plan("install mytool", probe=self.ambiguous_probe(), jev=jev)
        self.assertEqual(plan.recipe, pv.RECIPE_VENV_PIP)  # deterministic order
        self.assertTrue(plan.is_fallback)
        self.assertIn("not a declared recipe", " ".join(plan.notes))

    def test_jev_fallback_or_failure_degrades_honestly(self):
        for jev in (FakeJev(choice=pv.RECIPE_NPM_PREFIX, fallback=True),
                    FakeJev(raises=HarnessError("down"))):
            plan = self.plan("install mytool", probe=self.ambiguous_probe(), jev=jev)
            self.assertEqual(plan.recipe, pv.RECIPE_VENV_PIP)
            self.assertTrue(plan.is_fallback)
            self.assertEqual(plan.source, "deterministic")
            self.assertFalse(plan.review_required)
            self.assertTrue(plan.review["is_fallback"])

    def test_without_a_judgment_source_the_most_reversible_recipe_wins(self):
        plan = self.plan("install mytool", probe=self.ambiguous_probe())
        self.assertEqual(plan.recipe, pv.RECIPE_VENV_PIP)
        self.assertIn("no judgment source", plan.notes[0])
        self.assertTrue(plan.is_fallback)
        self.assertEqual(plan.review, {})

    def test_single_candidate_asks_selection_not_at_all_but_reviews(self):
        jev = FakeJev(noul={"satisfies_goal": 0.9, "exceeds_goal": 0.1})
        plan = self.plan(probe=self.probe(managers=("pip",)), jev=jev)
        self.assertEqual(len(jev.calls), 1)
        self.assertNotIn("recipe", jev.calls[0][1])
        self.assertEqual(plan.source, "jev-review")
        self.assertFalse(plan.is_fallback)

    def test_review_that_flags_overreach_forces_per_step_approval(self):
        for noul in ({"satisfies_goal": 0.9, "exceeds_goal": 0.8},
                     {"satisfies_goal": 0.2, "exceeds_goal": 0.1}):
            plan = self.plan(probe=self.probe(managers=("pip",)),
                             jev=FakeJev(noul=noul))
            self.assertTrue(plan.review_required, msg=noul)
            with self.assertRaises(pv.ProvisionError):
                pv.ApprovalGate().approve_plan(plan, "a")

    def test_review_with_unparseable_answers_is_a_fallback(self):
        plan = self.plan(probe=self.probe(managers=("pip",)),
                         jev=FakeJev(noul={"satisfies_goal": "high"}))
        self.assertTrue(plan.review["is_fallback"])
        self.assertFalse(plan.review_required)

    def test_review_failure_is_recorded_not_raised(self):
        plan = self.plan(probe=self.probe(managers=("pip",)),
                         jev=FakeJev(raises=HarnessError("down")))
        self.assertEqual(plan.review["reason"], "down")

    def test_inventory_plans_are_not_sent_for_review(self):
        jev = FakeJev()
        self.plan("nothing to do", jev=jev)
        self.assertEqual(jev.calls, [])

    def test_bad_candidates_raise_in_the_pack_builder(self):
        with self.assertRaises(ValueError):
            provision_selection_question_pack([("only-one", "x")])
        with self.assertRaises(ValueError):
            provision_selection_question_pack([("a", "x"), ("a", "y")])
        with self.assertRaises(ValueError):
            provision_selection_question_pack([("a", "x"), ("", "y")])
        with self.assertRaises(ValueError):
            provision_selection_question_pack(["ab", "cd"][0:1] + [3])
        pack = provision_selection_question_pack([("a", "x"), ("b", "y")])
        self.assertEqual(pack["recipe"]["type"], "choice")
        verification = provision_verification_question_pack()
        self.assertEqual(set(verification), {"satisfies_goal", "exceeds_goal"})


# --------------------------------------------------------------------------
# JevPolicy.evaluate_provision (the one owner's dispatch + ledger rule)
# --------------------------------------------------------------------------

class _Governor:
    max_cost = 1.0
    spent = 0.0

    def __init__(self):
        self.reserved = []
        self.reconciled = []

    def reserve(self, worst, label):
        self.reserved.append(label)
        return {"label": label}

    def reconcile(self, reservation, cost):
        self.reconciled.append(cost)

    def record_actual(self, cost, model):
        pass


class _Evaluator:
    api_key = "jev-key"
    model = "jev-test"

    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    def evaluate(self, state, questions=None):
        self.calls.append(state)
        if self.fail:
            raise HarnessError("transport down")
        return JevEvaluationResult(
            "pass", 0.8, 1.0, {"recipe": {"choice": "venv-pip"}}, [],
            is_fallback=False, model=self.model)


class EvaluateProvisionTests(TempRootCase):
    def policy_for(self, keyed=True, fail=False):
        settings = load_settings()
        settings.jev_api_key = "k" if keyed else None
        evaluator = _Evaluator(fail=fail)
        if not keyed:
            evaluator.api_key = None
        governor = _Governor()
        return JevPolicy(settings, ledger=self.ledger, evaluator=evaluator,
                         governor=governor), evaluator, governor

    questions = provision_selection_question_pack([("venv-pip", "a"), ("npm-prefix", "b")])

    def test_unkeyed_is_an_honest_fallback_with_one_ledger_event(self):
        policy, evaluator, _ = self.policy_for(keyed=False)
        result, structural = policy.evaluate_provision({"goal": "g"}, self.questions)
        self.assertTrue(result.is_fallback)
        self.assertEqual(result.answers, {})
        self.assertEqual(evaluator.calls, [])
        self.assertEqual(structural["site"], "provision_plan")
        evals = self.events("jev_eval")
        self.assertEqual(len(evals), 1)
        self.assertTrue(evals[0]["is_fallback"])

    def test_keyed_dispatches_once_and_settles_spend(self):
        policy, evaluator, governor = self.policy_for()
        result, structural = policy.evaluate_provision({"goal": "g"}, self.questions,
                                                       task_id="t")
        self.assertFalse(result.is_fallback)
        self.assertEqual(len(evaluator.calls), 1)
        self.assertEqual(governor.reserved, ["jev:provision_plan"])
        self.assertEqual(len(governor.reconciled), 1)
        self.assertEqual(len(self.events("jev_eval")), 1)
        self.assertEqual(structural["site"], "provision_plan")

    def test_keyed_failure_is_recorded_as_a_refusal(self):
        policy, _, governor = self.policy_for(fail=True)
        result, _ = policy.evaluate_provision({"goal": "g"}, self.questions)
        self.assertEqual(result.verdict, "fail")
        self.assertEqual(len(self.events("jev_refusal")), 1)
        self.assertEqual(governor.reconciled, [0.0])  # the reservation is released

    def test_a_failing_reservation_release_does_not_mask_the_refusal(self):
        policy, _, governor = self.policy_for(fail=True)

        def reconcile(reservation, cost):
            raise HarnessError("ledger of spend is down")

        governor.reconcile = reconcile
        result, _ = policy.evaluate_provision({"goal": "g"}, self.questions)
        self.assertEqual(result.verdict, "fail")
        self.assertEqual(len(self.events("jev_refusal")), 1)

    def test_end_to_end_planner_through_the_real_policy(self):
        policy, evaluator, _ = self.policy_for()
        plan = pv.plan_provision(
            "install mytool", self.probe(managers=("pip", "npm")),
            policy=self.policy, jev=policy)
        self.assertEqual(plan.recipe, pv.RECIPE_VENV_PIP)
        self.assertEqual(plan.source, "jev")
        # selection + review on the real policy: each ONE jev_eval
        self.assertEqual(len(self.events("jev_eval")), 2)
        self.assertEqual(self.ledger.verify(), (True, None))


# --------------------------------------------------------------------------
# the whole loop, hermetically
# --------------------------------------------------------------------------

class EndToEndTests(TempRootCase):
    def test_probe_plan_preview_approve_execute_audit(self):
        probe = self.probe(managers=("pip",))
        plan = pv.plan_provision("provision ruff==0.6.1", probe,
                                 policy=self.policy)
        runner = FakeRunner()
        gate = pv.ApprovalGate(ledger=self.ledger, task_id="e2e")

        preview = pv.execute_plan(plan, gate, policy=self.policy,
                                  ledger=self.ledger, task_id="e2e",
                                  runner=runner)
        self.assertEqual(preview.outcome, pv.DRY_RUN)
        self.assertEqual(runner.calls, [])
        self.assertFalse(os.path.exists(plan.root))

        refused, _ = (pv.execute_plan(plan, gate, policy=self.policy,
                                      ledger=self.ledger, task_id="e2e",
                                      runner=runner, dry_run=False,
                                      which=fake_which), None)
        self.assertEqual(refused.outcome, pv.AWAITING_APPROVAL)
        self.assertEqual(runner.calls, [])

        gate.approve_plan(plan, "operator", "reviewed the rendered plan")
        done = pv.execute_plan(plan, gate, policy=self.policy,
                               ledger=self.ledger, task_id="e2e", runner=runner,
                               dry_run=False, which=fake_which)
        self.assertEqual(done.outcome, pv.COMPLETED)
        self.assertTrue(os.path.isdir(plan.root))
        argvs = runner.argvs()
        self.assertEqual(len(argvs), 3)  # venv, pip install, pip show
        self.assertEqual(argvs[0][1:4], ["-I", "-m", "venv"])
        self.assertEqual(self.ledger.verify(), (True, None))
        kinds = {e["event"] for e in self.ledger.entries()}
        self.assertTrue(kinds <= set(pv.PROVISION_EVENTS))
        self.assertTrue(all(e["task_id"] == "e2e" for e in self.ledger.entries()))


if __name__ == "__main__":
    unittest.main()
