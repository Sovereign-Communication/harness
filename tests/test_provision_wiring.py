"""Conditions for wiring provisioning to a surface (round-3 verifier findings).

Pins: code-running steps need approval, the executed program's stdin and
wall clock are bounded (``osal.run_tree``), approval-store failures never
lose a report, the apt grammar, probes under the same containment as steps,
an allowlisted environment, look-alike characters, leftovers, and ``done=``
backed by ledger evidence. Hermetic: fake runners except the few tests that
run the current interpreter for the process-level properties.
"""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import osal, provision as pv
from tests.test_provision import FakeRunner, fake_which, step
from tests.test_provision_hardening import FailingLedger, HardeningCase


class InRootProgramsNeedApprovalTests(HardeningCase):
    def inside(self, *parts):
        return os.path.join(self.work(), "venv", "bin", *parts)

    def test_any_run_of_an_in_root_program_is_at_least_mutating(self):
        py = self.inside("python")
        for argv in ((py, "-I", "-m", "pip", "show", "--isolated", "x"),
                     (py, "-I", "-m", "pip", "list"),
                     (py, "-I", "-m", "pip", "--version"),
                     (py, "--version"),
                     (self.inside("pip"), "list"),
                     (self.inside("pip"), "--version")):
            adm = self.admit(*argv)
            self.assertEqual(adm.minimum_class, pv.MUTATING, msg=argv)
            self.assertFalse(adm.requires_network, msg=argv)

    def test_truly_read_only_probes_stay_approval_free(self):
        trusted = pv.ProvisionPolicy(approved_roots=(self.root,),
                                     trusted_executables=(osal.python_exe(),))
        for argv, policy in ((("python", "--version"), self.policy),
                             (("python", "-I", "-m", "pip", "show", "--isolated", "x"),
                              self.policy),
                             (("pip", "--version"), self.policy),
                             ((osal.python_exe(), "--version"), trusted)):
            adm = self.admit(*argv, policy=policy)
            self.assertEqual(adm.minimum_class, pv.READ, msg=argv)
            self.assertFalse(pv.needs_approval(step(argv=argv)), msg=argv)

    def test_a_read_labelled_in_root_step_is_rejected_as_under_classified(self):
        bad = step(argv=(self.inside("pip"), "--version"))
        with self.assertRaises(pv.ProvisionError) as ctx:
            pv.validate_step(bad, self.policy)
        self.assertIn("at least mutating", str(ctx.exception))

    def test_a_planted_in_root_program_does_not_run_without_approval(self):
        planted = self.inside("pip.exe")
        plan = pv.Plan("g", [pv.Step(
            "verify", "d", (planted, "--version"), pv.MUTATING, "e", "nothing")],
            self.root, "x", "x", True)
        runner = FakeRunner()
        report = pv.execute_plan(plan, pv.ApprovalGate(), policy=self.policy,
                                 ledger=self.ledger, runner=runner,
                                 dry_run=False, which=fake_which)
        self.assertEqual(report.outcome, pv.AWAITING_APPROVAL)
        self.assertEqual(runner.calls, [])

    def test_the_planners_verify_steps_of_a_venv_need_approval(self):
        plan = pv.plan_provision("provision ruff==0.6.1",
                                 self.probe(managers=("pip",)),
                                 policy=self.policy)
        verify = plan.steps[-1]
        self.assertEqual(verify.classification, pv.MUTATING)
        self.assertTrue(pv.needs_approval(verify))


class RunTreeTests(unittest.TestCase):
    def test_stdin_is_the_null_device_not_the_harness_stdin(self):
        code = "import sys;print('STDIN:' + repr(sys.stdin.read()))"
        result = osal.run_tree([osal.python_exe(), "-c", code], timeout=30)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "STDIN:''")

    def test_a_grandchild_holding_the_pipes_cannot_stall_the_timeout(self):
        code = ("import subprocess,sys,time;"
                "subprocess.Popen([sys.executable,'-c','import time;time.sleep(25)']);"
                "time.sleep(60)")
        started = time.monotonic()
        result = osal.run_tree([osal.python_exe(), "-c", code], timeout=2)
        elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 124)
        self.assertIn("timed out", result.stdout)
        self.assertLess(elapsed, 15, "run_tree must return near its timeout")

    def test_exit_codes_output_cwd_and_env(self):
        here = os.path.realpath(os.path.dirname(os.path.abspath(__file__)))
        code = ("import os,sys;print(os.getcwd());print(os.environ.get('RT_X'));"
                "sys.stderr.write('err');sys.exit(3)")
        result = osal.run_tree([osal.python_exe(), "-c", code], cwd=here,
                               env=dict(os.environ, RT_X="seen"), timeout=30)
        self.assertEqual(result.returncode, 3)
        lines = result.stdout.split()
        self.assertEqual(os.path.realpath(lines[0]), here)
        self.assertEqual(lines[1], "seen")
        self.assertEqual(result.stderr, "err")

    def test_failures_map_to_the_same_codes_as_run(self):
        self.assertEqual(osal.run_tree(["definitely-not-a-program-xyz"]).returncode, 127)
        directory = os.path.dirname(os.path.abspath(__file__))
        self.assertIn(osal.run_tree([directory]).returncode, (126, 127))
        with self.assertRaises(osal.OsalError):
            osal.run_tree("python --version")
        with self.assertRaises(osal.OsalError):
            osal.run_tree([])

    def test_provision_uses_the_hard_timeout_runner_by_default(self):
        class Done:
            returncode, stdout, stderr = 0, "v1\n", ""

        calls = []

        def fake_tree(argv, cwd=None, timeout=None, env=None):
            calls.append((argv, cwd, timeout, env))
            return Done()

        with mock.patch.object(osal, "run_tree", fake_tree):
            pv.probe_host(os.getcwd(), which=lambda n: None, facts=dict(
                os="Linux", arch="x", python_version="3", python_executable="python",
                free_disk_bytes=1))
        self.assertTrue(calls)  # the pip fallback probe went through run_tree


class RunTreeBranchTests(unittest.TestCase):
    """The platform- and failure-specific branches, driven with doubles."""

    def test_posix_kill_takes_the_whole_process_group(self):
        process = mock.Mock(pid=4242)
        with mock.patch.object(osal, "IS_WINDOWS", False),                 mock.patch.object(os, "getpgid", create=True,
                                  return_value=77) as getpgid,                 mock.patch.object(os, "killpg", create=True) as killpg:
            osal._kill_tree(process)
        getpgid.assert_called_once_with(4242)
        self.assertEqual(killpg.call_args[0][0], 77)
        process.kill.assert_called_once()

    def test_a_group_that_is_already_gone_is_not_an_error(self):
        process = mock.Mock(pid=1)
        process.kill.side_effect = OSError("gone")
        with mock.patch.object(osal, "IS_WINDOWS", False),                 mock.patch.object(os, "killpg", create=True),                 mock.patch.object(os, "getpgid", create=True,
                                  side_effect=OSError("no such process")):
            osal._kill_tree(process)  # must not raise

    def test_windows_kill_uses_taskkill_on_the_tree_and_survives_its_absence(self):
        process = mock.Mock(pid=9)
        with mock.patch.object(osal, "IS_WINDOWS", True),                 mock.patch.object(osal.subprocess, "run") as run:
            osal._kill_tree(process)
        self.assertEqual(run.call_args[0][0][:3], ["taskkill", "/T", "/F"])
        with mock.patch.object(osal, "IS_WINDOWS", True),                 mock.patch.object(osal.subprocess, "run",
                                  side_effect=OSError("no taskkill")):
            osal._kill_tree(process)

    def test_launch_failures_are_exit_codes(self):
        for exc, code in ((PermissionError("x"), 126), (OSError("x"), 126)):
            with mock.patch.object(osal.subprocess, "Popen", side_effect=exc):
                self.assertEqual(osal.run_tree(["x"]).returncode, code)

    def test_pipes_a_stubborn_descendant_holds_are_abandoned(self):
        timeout = osal.subprocess.TimeoutExpired

        class Stubborn:
            pid = 5
            returncode = None

            def __init__(self):
                self.stdout = mock.Mock()
                self.stderr = mock.Mock()
                self.stderr.close.side_effect = OSError("already closed")

            def communicate(self, timeout=None):
                raise osal.subprocess.TimeoutExpired("x", timeout)

            def wait(self, timeout=None):
                raise osal.subprocess.TimeoutExpired("x", timeout)

            def kill(self):
                pass

        stubborn = Stubborn()
        with mock.patch.object(osal.subprocess, "Popen", return_value=stubborn),                 mock.patch.object(osal, "_kill_tree"),                 mock.patch.object(osal, "_DRAIN_SECONDS", 0.01),                 mock.patch.object(osal, "IS_WINDOWS", False):
            result = osal.run_tree(["x"], timeout=0.01)
        self.assertIsNotNone(timeout)
        self.assertEqual(result.returncode, 124)
        stubborn.stdout.close.assert_called_once()
        stubborn.stderr.close.assert_called_once()

    def test_windows_does_not_block_closing_inherited_pipes(self):
        class Stubborn:
            pid = 5
            returncode = None

            def __init__(self):
                self.stdout = mock.Mock()
                self.stderr = mock.Mock()

            def communicate(self, timeout=None):
                raise osal.subprocess.TimeoutExpired("x", timeout)

            def wait(self, timeout=None):
                raise osal.subprocess.TimeoutExpired("x", timeout)

            def kill(self):
                pass

        stubborn = Stubborn()
        with mock.patch.object(osal.subprocess, "Popen", return_value=stubborn),                 mock.patch.object(osal, "_kill_tree"),                 mock.patch.object(osal, "_DRAIN_SECONDS", 0.01),                 mock.patch.object(osal, "IS_WINDOWS", True):
            result = osal.run_tree(["x"], timeout=0.01)
        self.assertEqual(result.returncode, 124)
        stubborn.stdout.close.assert_not_called()
        stubborn.stderr.close.assert_not_called()

    def test_the_posix_launch_path_starts_a_new_session(self):
        captured = {}

        class Quick:
            returncode = 0

            def __init__(self, argv, **kwargs):
                captured.update(kwargs)

            def communicate(self, timeout=None):
                return b"out", b"err"

        with mock.patch.object(osal, "IS_WINDOWS", False),                 mock.patch.object(osal.subprocess, "Popen", Quick):
            result = osal.run_tree(["x"], cwd="c", env={"A": "1"})
        self.assertEqual((result.stdout, result.stderr), ("out", "err"))
        self.assertTrue(captured["start_new_session"])
        self.assertEqual((captured["cwd"], captured["env"]), ("c", {"A": "1"}))
        self.assertIs(captured["stdin"], osal.subprocess.DEVNULL)


class ApprovalStoreFailureTests(HardeningCase):
    def test_a_gate_ledger_failure_mid_run_keeps_what_ran(self):
        s1 = step(id="ver")
        s2 = pv.Step("mk", "d", ("mkdir", self.work("x")), pv.MUTATING, "e",
                     "undo x")
        s3 = pv.Step("mk2", "d", ("mkdir", self.work("y")), pv.MUTATING, "e",
                     "undo y")
        plan = pv.Plan("g", [s1, s2, s3], self.root, "r", "s", False)
        gate = pv.ApprovalGate(ask=lambda p, s: (pv.APPROVE, "ok"))
        gate.approve_step(plan, "mk", "tester")
        gate.ledger = FailingLedger(self.tmp, "provision_approval")
        report = pv.execute_plan(plan, gate, policy=self.policy,
                                 ledger=self.ledger, runner=FakeRunner(),
                                 dry_run=False, which=fake_which)
        self.assertTrue(report.audit_failed)
        self.assertEqual(report.outcome, pv.AUDIT_FAILED)
        self.assertEqual([r.status for r in report.results],
                         [pv.COMPLETED, pv.COMPLETED, pv.AUDIT_FAILED])
        self.assertTrue(os.path.isdir(self.work("x")))   # it did run
        self.assertFalse(os.path.exists(self.work("y")))  # the unrecorded one did not
        self.assertEqual(report.rollback_hints, (("mk", "undo x"),))

    def test_later_steps_after_an_approval_failure_are_skipped(self):
        plan = pv.Plan("g", [
            pv.Step("a", "d", ("mkdir", self.work("a")), pv.MUTATING, "e", "u"),
            pv.Step("b", "d", ("mkdir", self.work("b")), pv.MUTATING, "e", "u")],
            self.root, "r", "s", False)
        gate = pv.ApprovalGate(FailingLedger(self.tmp, "provision_approval"),
                               ask=lambda p, s: (pv.APPROVE, "ok"))
        report = pv.execute_plan(plan, gate, policy=self.policy,
                                 ledger=self.ledger, runner=FakeRunner(),
                                 dry_run=False, which=fake_which)
        self.assertEqual([r.status for r in report.results],
                         [pv.AUDIT_FAILED, pv.SKIPPED])
        self.assertFalse(os.path.exists(self.work("a")))


class AptGrammarTests(HardeningCase):
    def test_apt_ids_start_and_end_alphanumeric_with_no_pattern_characters(self):
        for spec in ("a.", "ca.", "lib.", "g..", "foo-", "foo+", "foo.+", "a*",
                     "a?", "a[b]", "^a", "a$", "a|b", "-a", "+a", ".a", "A"):
            self.refuse("apt", "install", "-y", spec)
            self.refuse("apt-get", "install", "-y", spec)
        for spec in ("git", "libstdc++6", "build-essential", "python3.12",
                     "a", "7zip"):
            self.admit("apt", "install", "-y", spec)

    def test_a_trailing_newline_is_not_swallowed_by_an_anchor(self):
        for pattern in pv._SYSTEM_ID_RES.values():
            self.assertIsNone(pattern.match("git\n"))
        self.assertIsNone(pv._STEP_ID_RE.match("abc\n"))
        self.assertIsNone(pv._VERSION_RE.match("1.0\n"))
        self.assertIsNone(pv._BARE_NAME_RE.match("pip\n"))

    def test_a_step_id_with_a_trailing_newline_is_refused(self):
        with self.assertRaises(pv.ProvisionError):
            pv.validate_step(step(id="abc\n"), self.policy)


class ProbeContainmentTests(HardeningCase):
    def test_probes_run_in_an_empty_private_directory_with_a_clean_env(self):
        seen = []

        def runner(argv, cwd=None, timeout=None, env=None):
            seen.append((cwd, os.path.isdir(cwd), os.listdir(cwd), env))
            return osal.CommandResult(0, "v1\n", "")

        dirty = {"PATH": "p", "SystemRoot": "r", "NODE_OPTIONS": "--require x",
                 "APT_CONFIG": "/evil", "HOMEBREW_API_DOMAIN": "http://evil",
                 "GIT_SSH_COMMAND": "evil", "HTTPS_PROXY": "http://e"}
        pv.probe_host(self.root, runner=runner, environ=dirty,
                      which=lambda n: os.path.join(os.path.abspath(os.sep), "bin", n)
                      if n in ("npm", "nvidia-smi") else None,
                      facts=dict(os="Linux", arch="x", python_version="3",
                                 python_executable="python", free_disk_bytes=1))
        self.assertTrue(seen)
        for cwd, was_dir, contents, env in seen:
            self.assertTrue(was_dir)
            self.assertEqual(contents, [])
            self.assertNotEqual(os.path.realpath(cwd), os.path.realpath(os.getcwd()))
            self.assertEqual((env["PATH"], env["SystemRoot"]), ("p", "r"))
            for key in ("NODE_OPTIONS", "APT_CONFIG", "HOMEBREW_API_DOMAIN",
                        "GIT_SSH_COMMAND", "HTTPS_PROXY"):
                self.assertNotIn(key, env)
        self.assertFalse(os.path.exists(seen[0][0]))  # removed afterwards


class ProbeCleanupTests(HardeningCase):
    def test_a_probe_directory_that_cannot_be_removed_does_not_fail_the_probe(self):
        with mock.patch.object(pv.os, "rmdir", side_effect=OSError("busy")):
            probe = pv.probe_host(self.root, runner=FakeRunner(),
                                  which=lambda n: None, facts=dict(
                                      os="Linux", arch="x", python_version="3",
                                      python_executable="python",
                                      free_disk_bytes=1))
        self.assertEqual(probe.os_name, "Linux")

    def test_the_path_helper_names_lookalikes_on_its_own(self):
        self.assertIn("look-alike",
                      pv._path_problem(os.path.join(self.root, "x①")))


class EnvironmentAllowlistTests(HardeningCase):
    def test_only_listed_names_survive(self):
        env = pv._scrubbed_env({
            "APT_CONFIG": "/evil/apt.conf", "HOMEBREW_BOTTLE_DOMAIN": "http://e",
            "GIT_SSH_COMMAND": "evil", "GIT_CONFIG_GLOBAL": "/evil",
            "ChocolateyInstall": "C:/evil", "SCOOP": "C:/evil",
            "JAVA_TOOL_OPTIONS": "-javaagent:x", "BASH_ENV": "/x",
            "TMPDIR": "/x", "NODE_OPTIONS": "--require x", "HTTP_PROXY": "x",
            "npm_config_registry": "http://e", "pip_index_url": "http://e",
            "PATH": "/bin", "Path": "ignored-dup", "PATHEXT": ".EXE",
            "SystemRoot": "C:/Windows", "TEMP": "/t", "TMP": "/t",
            "HOME": "/h", "USERPROFILE": "C:/u", "APPDATA": "a",
            "LOCALAPPDATA": "l", "ProgramData": "d", "ProgramFiles": "pf",
            "ProgramFiles(x86)": "pf86", "COMSPEC": "cmd", "LANG": "C",
            "LC_ALL": "C", "TERM": "dumb"})
        for key in ("APT_CONFIG", "HOMEBREW_BOTTLE_DOMAIN", "GIT_SSH_COMMAND",
                    "GIT_CONFIG_GLOBAL", "ChocolateyInstall", "SCOOP",
                    "JAVA_TOOL_OPTIONS", "BASH_ENV", "TMPDIR", "NODE_OPTIONS",
                    "HTTP_PROXY", "npm_config_registry", "pip_index_url"):
            self.assertNotIn(key, env, msg=key)
        for key in ("PATH", "PATHEXT", "SystemRoot", "TEMP", "TMP", "HOME",
                    "USERPROFILE", "APPDATA", "LOCALAPPDATA", "ProgramData",
                    "ProgramFiles", "ProgramFiles(x86)", "COMSPEC", "LANG",
                    "LC_ALL", "TERM"):
            self.assertIn(key, env, msg=key)
        self.assertEqual(env["NPM_CONFIG_IGNORE_SCRIPTS"], "true")
        self.assertEqual(env["PIP_CONFIG_FILE"], os.devnull)


class LookAlikeTests(HardeningCase):
    def test_fullwidth_and_compatibility_characters_are_refused(self):
        for char in ("\uff02", "\uff06", "\uff05", "\uff5c", "\uff1e", "\uff3e",
                     "\uff01", "\u2460", "\ufb01"):
            path = os.path.join(self.root, "x" + char + "y")
            self.refuse("mkdir", path)
            self.refuse("python", "-I", "-m", "venv", path)
            self.refuse("npm", "install", "x", "--prefix", path,
                        "--ignore-scripts")
            self.refuse("python", "--version", "a" + char)

    def test_plain_non_ascii_names_that_nfkc_keeps_are_allowed(self):
        self.assertEqual(
            self.admit("mkdir", os.path.join(self.root, "caf\u00e9")).rule, "mkdir")


class LeftoverAndStaleDirectoryTests(HardeningCase):
    def test_leftovers_are_listed_on_the_result_and_in_the_ledger(self):
        def runner(argv, cwd=None, timeout=None, env=None):
            for name in ("b.txt", "a.txt"):
                with open(os.path.join(cwd, name), "w", encoding="utf-8") as h:
                    h.write("x")
            return osal.CommandResult(0, "ok", "")

        report = pv.execute_plan(
            pv.Plan("g", [step()], self.root, "x", "x", True), pv.ApprovalGate(),
            policy=self.policy, ledger=self.ledger, runner=runner,
            dry_run=False, which=fake_which)
        self.assertEqual(report.results[0].leftovers, ("a.txt", "b.txt"))
        self.assertEqual(self.events("provision_step")[0]["leftovers"],
                         ["a.txt", "b.txt"])
        self.assertEqual(report.results[0].to_dict()["leftovers"],
                         ["a.txt", "b.txt"])

    def test_clean_runs_report_no_leftovers(self):
        report = pv.execute_plan(
            pv.Plan("g", [step()], self.root, "x", "x", True), pv.ApprovalGate(),
            policy=self.policy, ledger=self.ledger, runner=FakeRunner(),
            dry_run=False, which=fake_which)
        self.assertEqual(report.results[0].leftovers, ())

    def test_empty_stale_private_directories_are_cleaned_non_empty_kept(self):
        empty = os.path.join(self.root, ".provision-cwd-stale1")
        full = os.path.join(self.root, ".provision-cwd-stale2")
        keep = os.path.join(self.root, "not-ours")
        for d in (empty, full, keep):
            os.makedirs(d)
        with open(os.path.join(full, "evidence.txt"), "w", encoding="utf-8") as h:
            h.write("x")
        pv.execute_plan(pv.Plan("g", [step()], self.root, "x", "x", True),
                        pv.ApprovalGate(), policy=self.policy,
                        ledger=self.ledger, runner=FakeRunner(), dry_run=False,
                        which=fake_which)
        self.assertFalse(os.path.exists(empty))
        self.assertTrue(os.path.exists(full))
        self.assertTrue(os.path.exists(keep))
        pv._clean_stale_private_dirs(os.path.join(self.tmp, "missing"))  # no raise


class DoneEvidenceTests(HardeningCase):
    def plan(self):
        return pv.Plan("g", [pv.Step("mk", "d", ("mkdir", self.work("z")),
                                     pv.MUTATING, "e", "rb")],
                       self.root, "r", "s", False)

    def test_a_forged_done_list_is_refused_and_nothing_is_skipped(self):
        plan = self.plan()
        with self.assertRaises(pv.ProvisionError) as ctx:
            pv.execute_plan(plan, pv.ApprovalGate(), policy=self.policy,
                            ledger=self.ledger, runner=FakeRunner(),
                            dry_run=False, which=fake_which, done=["mk"])
        self.assertIn("no completed ledger entry", str(ctx.exception))
        self.assertFalse(os.path.exists(self.work("z")))

    def test_done_without_a_ledger_is_refused_even_for_a_preview(self):
        with self.assertRaises(pv.ProvisionError):
            pv.execute_plan(self.plan(), pv.ApprovalGate(), policy=self.policy,
                            runner=FakeRunner(), done=["mk"])

    def test_done_is_honoured_only_for_steps_proven_for_this_plan_digest(self):
        plan = self.plan()
        gate = pv.ApprovalGate()
        gate.approve_plan(plan, "me")
        pv.execute_plan(plan, gate, policy=self.policy, ledger=self.ledger,
                        runner=FakeRunner(), dry_run=False, which=fake_which)
        second = pv.execute_plan(plan, gate, policy=self.policy,
                                 ledger=self.ledger, runner=FakeRunner(),
                                 dry_run=False, which=fake_which, done=["mk"])
        self.assertEqual([r.status for r in second.results], [pv.ALREADY_DONE])
        # a different plan (different digest) cannot borrow that evidence
        other = pv.Plan("g", [pv.Step("mk", "d", ("mkdir", self.work("w")),
                                      pv.MUTATING, "e", "rb")],
                        self.root, "r", "s", False)
        with self.assertRaises(pv.ProvisionError):
            pv.execute_plan(other, gate, policy=self.policy, ledger=self.ledger,
                            runner=FakeRunner(), dry_run=False,
                            which=fake_which, done=["mk"])

    def test_a_dry_run_entry_is_not_evidence_of_completion(self):
        plan = self.plan()
        pv.execute_plan(plan, pv.ApprovalGate(), policy=self.policy,
                        ledger=self.ledger, runner=FakeRunner())
        with self.assertRaises(pv.ProvisionError):
            pv.execute_plan(plan, pv.ApprovalGate(), policy=self.policy,
                            ledger=self.ledger, runner=FakeRunner(), done=["mk"])


if __name__ == "__main__":
    unittest.main()
