"""Regression tests for the independent verifier's confirmed provisioning bypasses.

Each class below pins one finding against ``harness/provision.py``: cmd.exe
syntax and batch shims, option-parser value smuggling, untrusted argv[0],
local-artifact installs, relative-path confusion, root and source validation,
plan identity, audit requirements, case folding, caps, environment scrubbing,
and judgment-failure fallbacks. Hermetic like ``test_provision.py``: a fake
runner and fake ``which``; the only real filesystem effect is a scratch dir.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import osal, provision as pv
from tests.test_provision import (LINUX, FakeJev, FakeRunner, TempRootCase,
                                  fake_which, mutating_plan, step, which_of)


class HardeningCase(TempRootCase):
    def refuse(self, *argv, needle="", policy=None):
        with self.assertRaises(pv.ProvisionError, msg=repr(argv)) as ctx:
            pv.classify_argv(list(argv), policy or self.policy)
        self.assertIn(needle, str(ctx.exception), msg=repr(argv))

    def admit(self, *argv, policy=None):
        return pv.classify_argv(list(argv), policy or self.policy)

    def pip_install(self, *specs):
        return ["python", "-m", "pip", "install", "--only-binary=:all:",
                "--target", self.work(), *specs]


class CmdSyntaxAndShimTests(HardeningCase):
    def test_cmd_metacharacters_are_refused_in_every_path_and_arg(self):
        for name in ("a&whoami", "a|hostname", "a^b", "a%PATH%b", 'a"b',
                     "a!b", "a<b", "a>b", "a\nb", "a\rb"):
            prefix = os.path.join(self.root, name)
            self.refuse("npm", "install", "left-pad", "--prefix", prefix,
                        "--ignore-scripts", needle="shell syntax")
            self.refuse("mkdir", prefix, needle="shell syntax")
            self.refuse("python", "-m", "venv", prefix, needle="shell syntax")
            self.refuse("python", "-m", "pip", "install", "--only-binary=:all:",
                        "--target", prefix, "x", needle="shell syntax")
            if name not in ("a<b", "a>b"):  # these are valid requirement specs
                self.refuse("python", "--version", name, needle="shell syntax")

    def test_requirement_operators_survive_but_nothing_else_does(self):
        for spec in ("rich>=1", "rich<3", "rich!=2.0", "rich[x]~=1.2"):
            self.admit(*self.pip_install(spec))
        for spec in ("rich>=1&calc", "rich>=1|x", "rich>%PATH%", 'rich">1'):
            self.refuse(*self.pip_install(spec), needle="shell syntax")

    def test_batch_shims_are_never_admitted(self):
        for exe in ("npm.cmd", "NPM.CMD", "x.bat",
                    os.path.join(self.root, "npm.cmd")):
            self.refuse(exe, "--version", needle="batch shim")

    def test_executor_refuses_a_bare_name_that_resolves_to_a_shim(self):
        # A scratch shim that would echo (and, unquoted, execute) its args.
        shim = os.path.join(self.tmp, "npm.cmd")
        with open(shim, "w", encoding="utf-8") as handle:
            handle.write("@echo off\r\necho SHIM-ARGS: %*\r\n")
        plan = pv.Plan("g", [step(id="n", argv=("npm", "--version"))],
                       self.root, "x", "x", True)
        runner = FakeRunner()
        report = pv.execute_plan(plan, pv.ApprovalGate(), policy=self.policy,
                                 ledger=self.ledger, runner=runner,
                                 dry_run=False, which=lambda name: shim)
        self.assertEqual(runner.calls, [])
        self.assertEqual(report.outcome, pv.FAILED)
        self.assertIn("batch shim", report.results[0].stderr)
        self.assertTrue(self.events("provision_step_start")[0]["refused"])

    def test_planner_skips_npm_when_it_is_only_a_cmd_shim(self):
        probe = pv.probe_host(
            self.root, runner=FakeRunner(), facts=LINUX,
            which=which_of("npm", paths={"npm": "C:\\node\\npm.CMD"}))
        self.assertTrue(probe.manager("npm").usable)
        self.assertEqual(pv.applicable_recipes(
            [pv.PackageRequest("x", None, "node")], probe, self.policy), [])


class FlagSmugglingTests(HardeningCase):
    def test_npm_cannot_be_talked_out_of_ignore_scripts(self):
        pre = ["npm", "install", "x"]
        for tail in (["--prefix", self.root, "--ignore-scripts", "false"],
                     ["--prefix", self.root, "--ignore-scripts", "0"],
                     ["--prefix", self.root, "--ignore-scripts=false"],
                     ["--prefix", self.root, "--ignore-scripts",
                      "--no-ignore-scripts"]):
            self.refuse(*pre, *tail)
        self.refuse("npm", "install", "false", "--prefix", self.root,
                    "--ignore-scripts", needle="boolean word")
        self.refuse("npm", "install", "--prefix", self.root, "--ignore-scripts",
                    "x", needle="must come before the flags")
        self.refuse("npm", "install", "x", "--ignore-scripts", "--prefix",
                    self.root, "y", needle="must come before the flags")

    def test_boolean_words_are_never_packages(self):
        for word in ("true", "TRUE", "yes", "no", "off", "on", "1", "0",
                     "null", "undefined"):
            self.refuse("pip", "show", word, needle="boolean word")
            self.refuse("brew", "install", word, needle="boolean word")


class ExecutableTests(HardeningCase):
    def test_argv0_path_must_be_trusted_or_in_a_root(self):
        outside = os.path.join(self.tmp, "planted", "python.exe")
        for exe in (outside, os.path.join(self.tmp, "winget.exe")):
            self.refuse(exe, "--version", needle="not a trusted executable")
        self.refuse("." + os.sep + "python.exe", "--version", needle="absolute")
        inside = os.path.join(self.work(), "venv", "bin", "python")
        self.assertEqual(self.admit(inside, "--version").minimum_class, pv.READ)
        trusted = pv.ProvisionPolicy(approved_roots=(self.root,),
                                     trusted_executables=(outside,))
        self.assertEqual(self.admit(outside, "--version", policy=trusted)
                         .minimum_class, pv.READ)

    def test_policy_for_probe_trusts_exactly_the_probed_interpreter(self):
        probe = pv.probe_host(
            self.root, runner=FakeRunner(), which=which_of(),
            facts=dict(LINUX, python_executable=osal.python_exe()))
        policy = pv.policy_for_probe(probe, (self.root,))
        self.assertEqual(policy.trusted_executables, (osal.python_exe(),))
        self.admit(osal.python_exe(), "--version", policy=policy)
        bare = pv.probe_host(self.root, runner=FakeRunner(), which=which_of(),
                             facts=LINUX)
        self.assertEqual(
            pv.policy_for_probe(bare, (self.root,)).trusted_executables, ())
        extra = pv.policy_for_probe(
            bare, (self.root,),
            trusted_executables=(osal.python_exe(),))
        self.assertEqual(len(extra.trusted_executables), 1)

    def test_executor_refuses_a_name_resolving_into_the_working_directory(self):
        plan = pv.Plan("g", [step(id="p", argv=("python", "--version"),
                                  cwd=self.root)], self.root, "x", "x", True)
        for planted_dir in (self.root, os.getcwd()):
            runner = FakeRunner()
            report = pv.execute_plan(
                plan, pv.ApprovalGate(), policy=self.policy,
                ledger=self.ledger, runner=runner, dry_run=False,
                which=lambda name, d=planted_dir: os.path.join(d, name + ".exe"))
            self.assertEqual(runner.calls, [], msg=planted_dir)
            self.assertIn("planted executable", report.results[0].stderr)

    def test_executor_fails_a_step_whose_program_is_not_on_path(self):
        plan = pv.Plan("g", [step()], self.root, "x", "x", True)
        runner = FakeRunner()
        report = pv.execute_plan(plan, pv.ApprovalGate(), policy=self.policy,
                                 ledger=self.ledger, runner=runner,
                                 dry_run=False, which=lambda name: None)
        self.assertEqual(report.outcome, pv.FAILED)
        self.assertEqual(report.results[0].returncode, 127)
        self.assertIn("not found on PATH", report.results[0].stderr)

    def test_executor_runs_the_resolved_path_not_the_bare_name(self):
        plan = pv.Plan("g", [step()], self.root, "x", "x", True)
        runner = FakeRunner()
        pv.execute_plan(plan, pv.ApprovalGate(), policy=self.policy,
                        ledger=self.ledger, runner=runner, dry_run=False,
                        which=fake_which)
        self.assertEqual(runner.calls[0]["resolved"][0], "/usr/bin/python")
        self.assertEqual(self.events("provision_step_start")[0]["resolved"][0],
                         "/usr/bin/python")


class LocalArtifactTests(HardeningCase):
    NAMES = ("evil-1.0-py3-none-any.whl", "evil.zip", "evil.tar", "evil.tar.gz",
             "evil.tgz", "evil.deb", "evil.nupkg", "evil.json", "evil.rb",
             "EVIL.WHL")

    def test_local_artifact_specs_are_refused_by_every_manager(self):
        for name in self.NAMES:
            self.refuse("pip", "show", name, needle="local file")
            self.refuse(*self.pip_install(name), needle="local file")
            self.refuse("npm", "install", name, "--prefix", self.root,
                        "--ignore-scripts", needle="local file")
            for manager in ("brew", "apt", "scoop", "choco"):
                self.refuse(manager, "install", name, needle="local file")
            self.refuse("winget", "install", "--id", name, needle="local file")
            self.refuse("brew", "list", name, needle="local file")

    def test_extract_requests_never_returns_a_local_file(self):
        for goal in ("install evil-1.0-py3-none-any.whl with pip",
                     "install evil.tgz", "provision evil==1.0.whl",
                     "install x@1.0.tgz", "install " + "a" * 500):
            self.assertEqual(pv.extract_requests(goal), (), msg=goal)

    def test_a_local_wheel_goal_yields_an_inventory_not_an_install(self):
        plan = pv.plan_provision("install evil-1.0-py3-none-any.whl with pip",
                                 self.probe(), policy=self.policy)
        self.assertEqual(plan.recipe, pv.RECIPE_INVENTORY)
        self.assertTrue(all(s.classification == pv.READ for s in plan.steps))
        with self.assertRaises(pv.ProvisionError):
            pv.plan_provision("x", self.probe(), policy=self.policy,
                              packages=[pv.PackageRequest("evil.whl")])


class PathAndRootTests(HardeningCase):
    def test_relative_paths_are_refused_everywhere(self):
        rel = os.path.join("..", "..", "x")
        for argv in (("npm", "install", "x", "--prefix", rel, "--ignore-scripts"),
                     ("npm", "ls", "--prefix", rel),
                     ("python", "-m", "pip", "install", "--only-binary=:all:",
                      "--target", rel, "x"),
                     ("python", "-m", "venv", rel), ("mkdir", rel),
                     ("mkdir", "tool")):
            self.refuse(*argv, needle="absolute")
        with self.assertRaises(pv.ProvisionError) as ctx:
            pv.validate_step(step(cwd=os.path.join("..", "x")), self.policy)
        self.assertIn("absolute", str(ctx.exception))

    def test_unc_and_device_paths_are_refused(self):
        for path in ("\\\\server\\share\\x", "\\\\?\\C:\\x", "//server/share/x"):
            self.refuse("mkdir", path, needle="UNC")

    def test_winget_source_is_a_closed_vocabulary(self):
        self.admit("winget", "install", "--id", "a.b", "--source", "winget")
        self.admit("winget", "list", "--id", "a.b", "--source", "msstore")
        for evil in ("--evil", "http://e/x", "custom"):
            self.refuse("winget", "install", "--id", "a.b", "--source", evil,
                        needle="--source")
            self.refuse("winget", "list", "--id", "a.b", "--source", evil,
                        needle="--source")

    def test_validate_plan_checks_the_plan_root(self):
        outside = pv.Plan("g", [step()], os.path.join(self.tmp, "elsewhere"),
                          "x", "x", True)
        with self.assertRaises(pv.ProvisionError) as ctx:
            pv.validate_plan(outside, self.policy)
        self.assertIn("plan root", str(ctx.exception))
        rootless = pv.Plan("g", [pv.Step(
            "m", "d", ("mkdir", self.work()), pv.MUTATING, "e", "r")],
            "", "x", "x", True)
        with self.assertRaises(pv.ProvisionError) as ctx:
            pv.validate_plan(rootless, self.policy)
        self.assertIn("must name its root", str(ctx.exception))
        pv.validate_plan(pv.Plan("g", [step()], "", "x", "x", True), self.policy)

    def test_policy_rejects_unusable_roots_and_executables(self):
        filesystem_root = os.path.abspath(os.sep)
        for bad in ("", "   ", "relative/dir", filesystem_root,
                    "\\\\server\\share", "//server/share", None, 5):
            with self.assertRaises(pv.ProvisionError, msg=repr(bad)):
                pv.ProvisionPolicy(approved_roots=(bad,))
        for bad in ("python", "", "\\\\server\\share\\python.exe"):
            with self.assertRaises(pv.ProvisionError, msg=repr(bad)):
                pv.ProvisionPolicy(trusted_executables=(bad,))
        if osal.IS_WINDOWS:
            with self.assertRaises(pv.ProvisionError):
                pv.ProvisionPolicy(approved_roots=("C:relative",))

    def test_kelvin_sign_is_not_folded_into_k(self):
        kelvin = chr(0x212A)
        self.assertEqual(osal._ascii_fold("ABC" + kelvin), "abc" + kelvin)
        a = os.path.join(self.tmp, "kroot")
        b = os.path.join(self.tmp, kelvin + "root")
        self.assertFalse(osal.is_within(os.path.join(b, "x"), a))
        self.assertFalse(osal.same_path(a, b))


    def test_case_sensitive_volumes_keep_their_own_normalization(self):
        with mock.patch.object(osal, "_fs_case_insensitive", return_value=False):
            self.assertEqual(osal.norm_path(self.root),
                             os.path.normcase(os.path.realpath(self.root)))
            self.assertNotEqual(osal.norm_path(self.root.upper()),
                                osal.norm_path(self.root) + "?")

    def test_a_nul_in_a_path_is_refused(self):
        self.assertIn("NUL", pv._path_problem(self.root + "\0x"))


class PlanIdentityTests(HardeningCase):
    def test_review_flag_changes_the_digest_and_survives_a_round_trip(self):
        steps = mutating_plan(self.root).steps
        plain = pv.Plan("g", steps, self.root, "x", "x", True)
        flagged = pv.Plan("g", steps, self.root, "x", "x", True,
                          review_required=True)
        self.assertNotEqual(plain.digest, flagged.digest)
        rebuilt = pv.plan_from_dict(flagged.to_dict())
        self.assertTrue(rebuilt.review_required)
        self.assertEqual(rebuilt.digest, flagged.digest)
        with self.assertRaises(pv.ProvisionError):
            pv.ApprovalGate().approve_plan(rebuilt, "a")
        gate = pv.ApprovalGate()
        gate.approve_plan(plain, "a")
        self.assertIsNone(gate.resolve(flagged, flagged.steps[0]))
        self.assertFalse(pv.plan_from_dict(
            dict(flagged.to_dict(), review_required="yes")).review_required)
        self.assertEqual(pv.plan_from_dict(
            dict(flagged.to_dict(), review={"a": 1})).review, {"a": 1})

    def test_gate_records_cannot_be_edited_by_callers(self):
        gate = pv.ApprovalGate()
        gate.approve_plan(mutating_plan(self.root), "a")
        self.assertIsInstance(gate.records, tuple)
        with self.assertRaises(AttributeError):
            gate.records.append(None)
        with self.assertRaises(AttributeError):
            gate.records = []
        self.assertEqual(len(gate.records), 1)


class AuditAndExecutionTests(HardeningCase):
    def test_dry_run_must_be_exactly_a_boolean(self):
        plan = pv.Plan("g", [step()], self.root, "x", "x", True)
        for bad in (None, 0, 1, "", "no", [], "false"):
            runner = FakeRunner()
            with self.assertRaises(pv.ProvisionError, msg=repr(bad)):
                pv.execute_plan(plan, pv.ApprovalGate(), policy=self.policy,
                                ledger=self.ledger, runner=runner, dry_run=bad,
                                which=fake_which)
            self.assertEqual(runner.calls, [])
        self.assertEqual(self.ledger.entries(), [])

    def test_a_start_entry_is_chained_before_each_step_runs(self):
        seen = []

        def runner(argv, cwd=None, timeout=None, env=None):
            seen.append([e["event"] for e in self.ledger.entries()])
            return osal.CommandResult(0, "ok", "")

        plan = pv.Plan("g", [step(id="a"), step(id="b")], self.root, "x", "x",
                       True)
        pv.execute_plan(plan, pv.ApprovalGate(), policy=self.policy,
                        ledger=self.ledger, runner=runner, dry_run=False,
                        which=fake_which)
        self.assertEqual(seen[0], ["provision_plan", "provision_step_start"])
        self.assertEqual(seen[1][-1], "provision_step_start")
        self.assertEqual(seen[1].count("provision_step"), 1)
        self.assertEqual(self.ledger.verify(), (True, None))

    def test_a_ledger_failure_before_a_step_stops_it_from_running(self):
        class Failing:
            def append(self, event, **fields):
                if event == "provision_step_start":
                    raise OSError("disk full")

        runner = FakeRunner()
        plan = pv.Plan("g", [step()], self.root, "x", "x", True)
        with self.assertRaises(OSError):
            pv.execute_plan(plan, pv.ApprovalGate(), policy=self.policy,
                            ledger=Failing(), runner=runner, dry_run=False,
                            which=fake_which)
        self.assertEqual(runner.calls, [])

    def test_mkdir_is_rechecked_when_it_runs(self):
        plan = pv.Plan("g", [pv.Step("m", "d", ("mkdir", self.work()),
                                     pv.MUTATING, "e", "r")],
                       self.root, "x", "x", True)

        def deny(path, policy, what):
            raise pv.ProvisionError("moved out of the root")

        with mock.patch.object(pv, "_require_in_roots", deny):
            result = pv._builtin_mkdir(plan.steps[0], self.policy)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(os.path.exists(self.work()))

    def test_a_runner_that_raises_anything_is_a_failed_step(self):
        plan = pv.Plan("g", [step()], self.root, "x", "x", True)
        report = pv.execute_plan(
            plan, pv.ApprovalGate(), policy=self.policy, ledger=self.ledger,
            runner=FakeRunner({("python",): RuntimeError("boom")}),
            dry_run=False, which=fake_which)
        self.assertEqual(report.outcome, pv.FAILED)
        self.assertIn("boom", report.results[0].stderr)

    def test_executed_steps_get_a_scrubbed_environment(self):
        dirty = {"PATH": "/usr/bin", "HOME": "/h",
                 "PIP_INDEX_URL": "http://evil",
                 "pip_extra_index_url": "http://evil",
                 "NPM_CONFIG_REGISTRY": "x", "npm_config_ignore_scripts": "false",
                 "PYTHONPATH": "/evil", "PYTHONSTARTUP": "/evil.py"}
        plan = pv.Plan("g", [step()], self.root, "x", "x", True)
        runner = FakeRunner()
        pv.execute_plan(plan, pv.ApprovalGate(), policy=self.policy,
                        ledger=self.ledger, runner=runner, dry_run=False,
                        which=fake_which, environ=dirty)
        env = runner.calls[0]["env"]
        self.assertEqual((env["PATH"], env["HOME"]), ("/usr/bin", "/h"))
        for key in ("PIP_INDEX_URL", "pip_extra_index_url",
                    "NPM_CONFIG_REGISTRY", "npm_config_ignore_scripts",
                    "PYTHONPATH", "PYTHONSTARTUP"):
            self.assertNotIn(key, env)
        self.assertEqual(env["PIP_CONFIG_FILE"], os.devnull)
        self.assertEqual(env["NPM_CONFIG_USERCONFIG"], os.devnull)
        self.assertEqual(env["NPM_CONFIG_IGNORE_SCRIPTS"], "true")
        self.assertEqual(env["PYTHONNOUSERSITE"], "1")
        self.assertTrue(pv._scrubbed_env())  # defaults to the real environment


class CapTests(HardeningCase):
    def test_argv_and_spec_counts_are_capped(self):
        self.refuse(*self.pip_install("a" * 2000), needle="limit")
        self.refuse("python", "--version", *(["x"] * 100), needle="limit")
        self.refuse(*self.pip_install(*(["a"] * 40)), needle="exceeds the limit")
        self.admit(*self.pip_install(*(["a"] * 20)))
        tight = pv.ProvisionPolicy(approved_roots=(self.root,), max_specs=2,
                                   max_argv=10, max_arg_chars=50)
        self.refuse("pip", "show", "a", "b", "c", needle="exceeds the limit",
                    policy=tight)
        self.refuse("mkdir", os.path.join(self.root, "x" * 60), needle="limit",
                    policy=tight)
        with self.assertRaises(pv.ProvisionError):
            pv.plan_provision("g", self.probe(), policy=tight,
                              packages=[pv.PackageRequest(n) for n in "abc"])
        with self.assertRaises(pv.ProvisionError):
            pv.plan_provision("x" * (pv.MAX_GOAL_CHARS + 1), self.probe(),
                              policy=self.policy)


class JudgmentFailureTests(HardeningCase):
    def test_any_judgment_error_degrades_to_the_deterministic_plan(self):
        probe = self.probe(managers=("pip", "npm", "apt"))
        for error in (RuntimeError("boom"), KeyError("k"), ValueError("v")):
            plan = pv.plan_provision("install mytool", probe, policy=self.policy,
                                     jev=FakeJev(raises=error))
            self.assertEqual(plan.recipe, pv.RECIPE_VENV_PIP)
            self.assertTrue(plan.is_fallback)
            self.assertTrue(plan.review["is_fallback"])


if __name__ == "__main__":
    unittest.main()
