"""Structural regressions for the second independent verification round.

Where ``test_provision_hardening.py`` pins individual bypasses, this file
pins the structure that replaced the blocklists: every step runs in a fresh
empty private directory, python is always isolated, every step is
re-validated immediately before it runs, approvals and the audit trail fail
closed, each package manager has a positive grammar, and nothing a human
reads can carry control or bidi characters. Hermetic: fake runner, fake
``which``, scratch directories only.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import osal, provision as pv
from harness.ledger import AutonomyLedger
from tests.test_provision import (FAKE_BIN, FakeRunner, TempRootCase, fake_which,
                                  mutating_plan, step, which_of)
from tests.test_provision_hardening import FailingLedger, HardeningCase


def make_dir_link(testcase, link, target):
    """A directory junction (Windows, no privilege) or symlink, else skip."""
    try:
        import _winapi
        _winapi.CreateJunction(target, link)
        return
    except (ImportError, AttributeError):
        pass
    except OSError as exc:
        testcase.skipTest(f"symlink/junction unavailable on this platform: {exc}")
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        testcase.skipTest(f"symlink privilege or platform support missing: {exc}")


class PrivateWorkingDirectoryTests(HardeningCase):
    def run_one(self, plan, **kw):
        runner = kw.pop("runner", FakeRunner())
        return pv.execute_plan(plan, pv.ApprovalGate(), policy=self.policy,
                               ledger=self.ledger, runner=runner,
                               dry_run=False, which=fake_which, **kw), runner

    def test_each_step_runs_in_a_fresh_empty_private_directory_in_the_root(self):
        seen = []

        def runner(argv, cwd=None, timeout=None, env=None):
            seen.append((cwd, os.path.isdir(cwd), os.listdir(cwd)))
            return osal.CommandResult(0, "ok", "")

        plan = pv.Plan("g", [step(id="a"), step(id="b")], self.root, "x", "x", True)
        self.run_one(plan, runner=runner)
        self.assertEqual(len(seen), 2)
        self.assertNotEqual(seen[0][0], seen[1][0])  # fresh per step
        for cwd, was_dir, contents in seen:
            self.assertTrue(was_dir)
            self.assertEqual(contents, [])  # empty at the moment it ran
            self.assertTrue(osal.is_within(cwd, self.root))
            self.assertFalse(osal.same_path(cwd, os.getcwd()))
            self.assertFalse(os.path.exists(cwd))  # removed afterwards
        self.assertEqual(os.listdir(self.root), [])

    def test_a_step_that_leaves_files_is_reported_and_its_directory_kept(self):
        def runner(argv, cwd=None, timeout=None, env=None):
            with open(os.path.join(cwd, "dropped.txt"), "w", encoding="utf-8") as h:
                h.write("x")
            return osal.CommandResult(0, "ok", "")

        report, _ = self.run_one(pv.Plan("g", [step()], self.root, "x", "x", True),
                                 runner=runner)
        self.assertIn("left files", report.results[0].note)

    def test_a_private_directory_that_is_not_empty_is_refused(self):
        dirty = os.path.join(self.root, "dirty")
        os.makedirs(dirty)
        with open(os.path.join(dirty, "evil-1.0.tar.lz"), "w", encoding="utf-8") as h:
            h.write("x")
        with mock.patch.object(pv.tempfile, "mkdtemp", return_value=dirty):
            report, runner = self.run_one(
                pv.Plan("g", [step()], self.root, "x", "x", True))
        self.assertEqual(runner.calls, [])
        self.assertEqual(report.outcome, pv.FAILED)
        self.assertIn("not empty", report.results[0].stderr)

    def test_the_process_directory_is_never_a_steps_directory(self):
        marker = os.path.join(self.tmp, "cwd-with-archive")
        os.makedirs(marker)
        before = os.getcwd()
        self.addCleanup(os.chdir, before)
        os.chdir(marker)
        _, runner = self.run_one(pv.Plan("g", [step()], self.root, "x", "x", True))
        self.assertNotEqual(os.path.normcase(runner.calls[0]["cwd"]),
                            os.path.normcase(marker))

    def test_a_real_run_needs_an_existing_approved_root(self):
        ghost = os.path.join(self.tmp, "ghost-root")
        policy = pv.ProvisionPolicy(approved_roots=(ghost,))
        plan = pv.Plan("g", [step()], ghost, "x", "x", True)
        with self.assertRaises(pv.ProvisionError) as ctx:
            pv.execute_plan(plan, pv.ApprovalGate(), policy=policy,
                            ledger=self.ledger, runner=FakeRunner(),
                            dry_run=False, which=fake_which)
        self.assertIn("does not exist", str(ctx.exception))
        with self.assertRaises(pv.ProvisionError) as ctx:
            pv.execute_plan(pv.Plan("g", [step()], "", "x", "x", True),
                            pv.ApprovalGate(), policy=pv.ProvisionPolicy(),
                            ledger=self.ledger, runner=FakeRunner(),
                            dry_run=False, which=fake_which)
        self.assertIn("approved root", str(ctx.exception))

    def test_the_private_directory_lives_in_the_root_that_holds_the_plan(self):
        other = os.path.join(self.tmp, "other-root")
        os.makedirs(other)
        policy = pv.ProvisionPolicy(approved_roots=(other, self.root))
        runner = FakeRunner()
        pv.execute_plan(pv.Plan("g", [step()], self.work(), "x", "x", True),
                        pv.ApprovalGate(), policy=policy, ledger=self.ledger,
                        runner=runner, dry_run=False, which=fake_which)
        self.assertTrue(osal.is_within(runner.calls[0]["cwd"], self.root))


class ArchiveFamilyTests(HardeningCase):
    ARCHIVES = ("evil-1.0.tbz", "evil-1.0.txz", "evil-1.0.tar.lz",
                "evil-1.0.tar.lzma", "evil-1.0.bz2", "evil-1.0.xz", "evil.egg",
                "evil.7z", "evil.tlz", "evil.tbz2", "evil.lzma", "evil.tar.Z",
                "evil.rar", "evil.zip", "evil.whl", "evil.msi", "evil.exe")
    MANIFESTS = ("evil.git", "evil.nuspec", "evil.config", "evil.yml",
                 "evil.yaml", "evil.psd1", "evil.json", "evil.rb")

    def test_every_archive_extension_is_refused_in_every_manager(self):
        for name in self.ARCHIVES:
            self.refuse("pip", "show", name, needle="local file")
            self.refuse(*self.pip_install(name), needle="local file")
            self.refuse(*self.pip_install(name + "==1.0"), needle="local file")
            self.refuse(*self.pip_install("evil==1.0." + name.split(".", 1)[-1]),
                        needle="local file")
            self.refuse("npm", "install", name, "--prefix", self.root,
                        "--ignore-scripts", needle="local file")
            for manager in ("choco", "scoop", "apt", "brew"):
                self.refuse(manager, "install", name, needle="local file")

    def test_manifest_and_repo_names_are_refused_by_system_managers(self):
        for name in self.MANIFESTS:
            for manager in ("choco", "scoop", "apt", "brew"):
                self.refuse(manager, "install", name, needle="local file")
            self.refuse("brew", "list", name, needle="local file")

    def test_names_that_merely_contain_dots_are_fine(self):
        for spec in ("zope.interface", "ruamel.yaml==0.18", "ruamel.yaml",
                     "a.b-c"):
            self.admit(*self.pip_install(spec))
        self.admit("npm", "install", "chart.js", "--prefix", self.root,
                   "--ignore-scripts")
        self.admit("winget", "install", "--id", "Git.Git")  # a real winget id
        self.admit("winget", "install", "--id", "Microsoft.VisualStudioCode")

    def test_goal_derived_archive_names_never_become_a_plan(self):
        for goal in ("pip install evil-1.0.tbz", "install evil-1.0.txz with pip",
                     "python venv pinned evil==1.0.tbz", "install evil.egg"):
            plan = pv.plan_provision(goal, self.probe(), policy=self.policy)
            self.assertEqual(plan.recipe, pv.RECIPE_INVENTORY, msg=goal)


class IsolationTests(HardeningCase):
    def test_python_code_always_runs_isolated(self):
        self.refuse("python", "-m", "pip", "install", "--isolated",
                    "--only-binary=:all:", "--target", self.work(), "x",
                    needle="may only run")
        self.refuse("python", "-m", "venv", self.work(), needle="may only run")
        self.refuse("python", "-I", "-m", "http.server", needle="may only run")
        self.refuse("python", "-I", "-c", "1", needle="may only run")
        self.admit("python", "-I", "-m", "venv", self.work())
        self.assertEqual(self.admit(*self.pip_install("x")).rule, "pip-install")

    def test_pip_install_must_be_isolated_from_config(self):
        self.refuse("python", "-I", "-m", "pip", "install",
                    "--only-binary=:all:", "--target", self.work(), "x",
                    needle="--isolated")
        self.refuse("pip", "install", "--only-binary=:all:", "--target",
                    self.work(), "x", needle="--isolated")

    def test_the_planner_emits_isolated_invocations(self):
        plan = pv.plan_provision("provision ruff==0.6.1",
                                 self.probe(managers=("pip",)),
                                 policy=self.policy)
        venv, install, show = plan.steps[1].argv, plan.steps[2].argv, plan.steps[3].argv
        self.assertEqual(venv[1:4], ("-I", "-m", "venv"))
        self.assertEqual(install[1:5], ("-I", "-m", "pip", "install"))
        self.assertIn("--isolated", install)
        self.assertEqual(show[1:4], ("-I", "-m", "pip"))

    def test_version_queries_take_exactly_one_argument(self):
        smuggled = (("pip", "--version", "install", "--index-url", "http://e", "x"),
                    ("python", "-I", "-m", "pip", "--version", "install", "x"),
                    ("npm", "--version", "install", "evil"),
                    ("winget", "--version", "install", "--id", "Evil.X"),
                    ("choco", "--version", "install", "evil", "-y"),
                    ("apt", "--version", "install", "-o", "APT::Update::Pre-Invoke::=x"),
                    ("brew", "--version", "install", "evil"),
                    ("python", "--version", "--help"))
        for argv in smuggled:
            self.refuse(*argv, needle="")
        for manager in ("pip", "npm", "winget", "choco", "apt", "brew", "scoop"):
            self.assertEqual(self.admit(manager, "--version").minimum_class,
                             pv.READ)

    def test_pip_list_and_show_arguments_are_validated(self):
        self.refuse("pip", "list", "requests", needle="no packages")
        self.refuse("pip", "list", "--user", needle="not allowed")
        self.refuse("pip", "show", needle="at least one package")
        self.refuse("pip", "show", "--user", "x", needle="not allowed")


class RevalidationTests(HardeningCase):
    def test_every_step_is_revalidated_immediately_before_it_runs(self):
        plan = pv.Plan("g", [step(id="a"), step(id="b")], self.root, "x", "x", True)
        real = pv.validate_step
        calls = []

        def counting(st, policy):
            calls.append(st.id)
            return real(st, policy)

        with mock.patch.object(pv, "validate_step", counting):
            pv.execute_plan(plan, pv.ApprovalGate(), policy=self.policy,
                            ledger=self.ledger, runner=FakeRunner(),
                            dry_run=False, which=fake_which)
        # once per step in validate_plan, once more right before each ran
        self.assertEqual(calls, ["a", "b", "a", "b"])

    def test_a_directory_swapped_for_a_link_after_the_plan_is_refused(self):
        target = os.path.join(self.root, "t")
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        steps = [step(id="first"),
                 pv.Step("inst", "d", ("python", "-I", "-m", "pip", "install",
                                       "--isolated", "--only-binary=:all:",
                                       "--target", target, "requests"),
                         pv.MUTATING, "installs", "delete", True)]
        plan = pv.Plan("g", steps, self.root, "x", "x", True)
        gate = pv.ApprovalGate()
        gate.approve_plan(plan, "me")
        calls = []

        def runner(argv, cwd=None, timeout=None, env=None):
            calls.append(list(argv))
            if len(calls) == 1:  # the swap happens between the two steps
                make_dir_link(self, target, outside)
            return osal.CommandResult(0, "ok", "")

        report = pv.execute_plan(plan, gate, policy=self.policy,
                                 ledger=self.ledger, runner=runner,
                                 dry_run=False, which=fake_which)
        self.assertEqual(len(calls), 1)  # the pip install never ran
        self.assertEqual(report.outcome, pv.FAILED)
        self.assertIn("outside every approved root", report.results[1].stderr)

    def test_a_venv_interpreter_symlink_is_allowed_a_swapped_directory_is_not(self):
        venv_bin = os.path.join(self.work(), "venv", "bin")
        os.makedirs(venv_bin)
        link = os.path.join(venv_bin, "python")
        try:
            os.symlink(osal.python_exe(), link)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink privilege or platform support missing: {exc}")
        adm = self.admit(link, "-I", "-m", "pip", "install", "--isolated",
                         "--only-binary=:all:", "x")
        self.assertEqual(adm.leaf_paths, (link,))
        outside = os.path.join(self.tmp, "outside-bin")
        os.makedirs(outside)
        swapped = os.path.join(self.root, "swapped")
        make_dir_link(self, swapped, outside)
        self.refuse(os.path.join(swapped, "python"), "--version",
                    needle="not a trusted executable")


class LedgerAndApprovalTests(HardeningCase):
    def test_no_ledger_entry_means_no_approval(self):
        plan = mutating_plan(self.root)
        gate = pv.ApprovalGate(FailingLedger(self.tmp, "provision_approval"))
        with self.assertRaises(OSError):
            gate.approve_plan(plan, "me")
        self.assertEqual(gate.records, ())
        self.assertIsNone(gate.resolve(plan, plan.steps[0]))

    def test_a_real_run_needs_the_real_chained_ledger(self):
        class Null:
            def append(self, *a, **k):
                return None

            def verify(self):
                return True, None

        plan = pv.Plan("g", [step()], self.root, "x", "x", True)
        for stand_in in (Null(), None, [], object()):
            runner = FakeRunner()
            with self.assertRaises(pv.ProvisionError, msg=repr(stand_in)):
                pv.execute_plan(plan, pv.ApprovalGate(), policy=self.policy,
                                ledger=stand_in, runner=runner, dry_run=False,
                                which=fake_which)
            self.assertEqual(runner.calls, [])

    def test_a_ledger_whose_chain_does_not_verify_is_refused(self):
        self.ledger.append("probe", note="first")
        self.ledger.append("probe", note="second")
        with open(self.ledger.path, encoding="utf-8") as handle:
            text = handle.read()
        with open(self.ledger.path, "w", encoding="utf-8", newline="") as handle:
            handle.write(text.replace("first", "forged", 1))
        broken = AutonomyLedger(self.ledger.path)
        self.assertFalse(broken.verify()[0])
        runner = FakeRunner()
        with self.assertRaises(pv.ProvisionError) as ctx:
            pv.execute_plan(pv.Plan("g", [step()], self.root, "x", "x", True),
                            pv.ApprovalGate(), policy=self.policy, ledger=broken,
                            runner=runner, dry_run=False, which=fake_which)
        self.assertIn("hash chain", str(ctx.exception))
        self.assertEqual(runner.calls, [])

    def test_a_ledger_that_fails_after_a_step_ran_still_returns_the_result(self):
        plan = pv.Plan("g", [
            pv.Step("mk", "make", ("mkdir", self.work()), pv.MUTATING,
                    "creates", "delete the dir"),
            step(id="after")], self.root, "x", "x", True)
        gate = pv.ApprovalGate()
        gate.approve_plan(plan, "me")
        runner = FakeRunner()
        report = pv.execute_plan(
            plan, gate, policy=self.policy,
            ledger=FailingLedger(self.tmp, "provision_step"), runner=runner,
            dry_run=False, which=fake_which)
        self.assertTrue(os.path.isdir(self.work()))  # the step did run
        self.assertTrue(report.audit_failed)
        self.assertEqual(report.outcome, pv.AUDIT_FAILED)
        self.assertEqual(report.results[0].status, pv.COMPLETED)
        self.assertEqual(report.results[1].status, pv.SKIPPED)
        self.assertEqual(runner.calls, [])  # nothing further ran
        self.assertEqual(report.rollback_hints,
                         (("mk", "delete the dir"),))
        self.assertTrue(report.to_dict()["audit_failed"])

    def test_a_ledger_that_cannot_write_the_end_entry_marks_the_audit_failed(self):
        report = pv.execute_plan(
            pv.Plan("g", [step()], self.root, "x", "x", True),
            pv.ApprovalGate(), policy=self.policy,
            ledger=FailingLedger(self.tmp, "provision_end"),
            runner=FakeRunner(), dry_run=False, which=fake_which)
        self.assertTrue(report.audit_failed)
        self.assertEqual(report.outcome, pv.AUDIT_FAILED)
        self.assertEqual(report.results[0].status, pv.COMPLETED)


class PackageGrammarTests(HardeningCase):
    def test_apt_removal_and_pinning_syntax_is_refused(self):
        for spec in ("foo-", "foo+", "foo=1.0", "foo/stable", "Foo", "-foo",
                     "foo bar"):
            self.refuse("apt", "install", "-y", spec)
            self.refuse("apt-get", "install", "-y", spec)
        self.admit("apt", "install", "-y", "libstdc++6")
        self.admit("apt-get", "install", "-y", "build-essential")

    def test_choco_refuses_config_and_package_files(self):
        for spec in ("packages.config", "foo.nuspec", "foo.nupkg", "./foo",
                     "c:\\x\\foo", "http://x/foo"):
            self.refuse("choco", "install", "-y", spec)
        self.assertEqual(self.admit("choco", "install", "-y", "git").rule,
                         "choco-install")
        self.refuse("choco", "install", "--version", "latest", "git",
                    needle="not plain")

    def test_scoop_refuses_manifests_urls_and_paths(self):
        for spec in ("foo.yml", "foo.json", "https://x/y.json", "a/b/c",
                     "../foo", "c:\\foo", "a//b"):
            self.refuse("scoop", "install", spec)
        self.admit("scoop", "install", "git")
        self.admit("scoop", "install", "extras/git")

    def test_brew_refuses_taps_and_formula_files(self):
        for spec in ("user/repo/formula", "a/b", "foo.rb", "Foo"):
            self.refuse("brew", "install", spec)
        self.admit("brew", "install", "wget")
        self.admit("brew", "install", "openssl@3")

    def test_winget_ids_are_strict(self):
        for spec in ("a b", "a/b", "../x", "x;y"):
            self.refuse("winget", "install", "--id", spec)
        self.refuse("winget", "install", "--id", "a.b", "--version", "1;2",
                    needle="not plain")
        self.admit("winget", "install", "--id", "Notepad++.Notepad++")


class RepeatedFlagAndRepoTests(HardeningCase):
    def test_a_flag_given_twice_is_refused_not_last_one_wins(self):
        evil = os.path.join(os.path.abspath(os.sep), "evil")
        self.refuse("npm", "install", "foo", "--prefix", evil, "--prefix",
                    self.root, "--ignore-scripts", needle="given twice")
        self.refuse("python", "-I", "-m", "pip", "install", "--isolated",
                    "--only-binary=:all:", "--target", self.work(), "--target",
                    self.work("b"), "x", needle="given twice")

    def test_repository_names_are_refused_by_pip_and_npm(self):
        for spec in ("foo.git", "foo@x.git"):
            self.refuse("npm", "install", spec, "--prefix", self.root,
                        "--ignore-scripts", needle="local file")
        self.refuse(*self.pip_install("foo.git"), needle="local file")


class UnsafeTextTests(HardeningCase):
    BAD = ("a\nb", "a\rb", "a\x1bb", "a\x07b", "a\tb", "a\u202eb", "a\u200bb",
           "a\u2028b", "a\u0085b", "a\ufeffb", "a\U000e0001b")

    def test_control_and_invisible_characters_are_rejected_everywhere(self):
        good = dict(id="s", description="d", argv=("python", "--version"),
                    classification=pv.READ, expected_effect="e")
        for bad in self.BAD:
            for field in ("description", "expected_effect"):
                with self.assertRaises(pv.ProvisionError, msg=(field, bad)):
                    pv.validate_step(pv.Step(**dict(good, **{field: bad})),
                                     self.policy)
            with self.assertRaises(pv.ProvisionError, msg=("rollback", bad)):
                pv.validate_step(pv.Step(
                    "m", "d", ("mkdir", self.work()), pv.MUTATING, "e", bad),
                    self.policy)
            with self.assertRaises(pv.ProvisionError, msg=("goal", bad)):
                pv.validate_plan(pv.Plan(bad, [step()], self.root, "x", "x", True),
                                 self.policy)
            self.refuse("python", "--version", bad)

    def test_the_planner_refuses_such_goals(self):
        for bad in self.BAD:
            with self.assertRaises(pv.ProvisionError, msg=bad):
                pv.plan_provision("install mytool" + bad, self.probe(),
                                  policy=self.policy)

    def test_rendering_escapes_everything_and_shows_argv_verbatim(self):
        esc = chr(27)
        forged = pv.Step(
            "a", "innocent\n2. [read_only] fake-step: harmless\n   run: x",
            ("mkdir", os.path.join(self.root, "q" + esc + "[2K\u202ex")),
            pv.MUTATING, "creates" + esc + "[1A" + esc + "[2K", "undo\r\nDigest: 0")
        plan = pv.Plan("goal\rDigest: 000", [forged], self.root, "re\ncipe",
                       "s\u202ee", True, ("note\nnote2",))
        text = pv.render_plan(plan)
        # 5 header lines + 4 for the one step + 1 note: nothing forged a line
        self.assertEqual(len(text.splitlines()), 10, msg=repr(text))
        for char in (esc, "\r", "\u202e", "\u200b"):
            self.assertNotIn(char, text)
        self.assertIn("\\x1b", text)
        self.assertIn("\\u202e", text)
        self.assertIn("   argv: [", text)
        self.assertIn("Review required: no", text)
        with self.assertRaises(pv.ProvisionError):
            pv.validate_plan(plan, self.policy)


class EnvironmentTests(HardeningCase):
    def test_the_environment_is_scrubbed_of_loader_proxy_and_tool_config(self):
        dirty = {"PATH": "p", "HOME": "h", "NODE_OPTIONS": "--require x.js",
                 "LD_PRELOAD": "x.so", "LD_LIBRARY_PATH": "/x",
                 "DYLD_INSERT_LIBRARIES": "x", "HTTPS_PROXY": "http://e",
                 "http_proxy": "http://e", "ALL_PROXY": "http://e",
                 "SSL_CERT_FILE": "x", "REQUESTS_CA_BUNDLE": "x",
                 "NODE_EXTRA_CA_CERTS": "x", "UV_INDEX_URL": "x",
                 "npm_config_registry": "http://e", "Pip_Index_Url": "http://e",
                 "NPM_TOKEN": "t", "PYTHONPATH": "x"}
        env = pv._scrubbed_env(dirty)
        self.assertEqual((env["PATH"], env["HOME"]), ("p", "h"))
        for key in dirty:
            if key not in ("PATH", "HOME"):
                self.assertNotIn(key, env, msg=key)
        self.assertEqual(env["NPM_CONFIG_USERCONFIG"], os.devnull)
        self.assertEqual(env["NPM_CONFIG_GLOBALCONFIG"], os.devnull)

    def test_the_pip_config_comment_is_honest(self):
        doc = pv._scrubbed_env.__doc__
        self.assertIn("--isolated", doc)
        self.assertIn(".npmrc", doc)


class PlanBindingTests(HardeningCase):
    def test_every_step_path_is_bound_to_the_plan_root(self):
        a, b = self.work("a"), self.work("b")
        plan = pv.Plan("g", [
            pv.Step("m1", "d", ("mkdir", a), pv.MUTATING, "e", "r"),
            pv.Step("m2", "d", ("mkdir", b), pv.MUTATING, "e", "r")],
            a, "x", "x", True)
        with self.assertRaises(pv.ProvisionError) as ctx:
            pv.validate_plan(plan, self.policy)
        self.assertEqual(len(ctx.exception.problems), 1)
        self.assertIn("outside the plan root", ctx.exception.problems[0])
        pv.validate_plan(pv.Plan("g", plan.steps, self.root, "x", "x", True),
                         self.policy)

    def test_npm_pip_and_venv_paths_are_bound_too(self):
        a, b = self.work("a"), self.work("b")
        venv_py = os.path.join(b, "venv", "bin", "python")
        for argv in (
                ("npm", "install", "x", "--prefix", b, "--ignore-scripts"),
                ("python", "-I", "-m", "venv", b),
                ("python", "-I", "-m", "pip", "install", "--isolated",
                 "--only-binary=:all:", "--target", b, "x"),
                (venv_py, "-I", "-m", "pip", "install", "--isolated",
                 "--only-binary=:all:", "x")):
            net = argv[3] != "venv"
            plan = pv.Plan("g", [pv.Step("s", "d", argv, pv.MUTATING, "e", "r",
                                         net)], a, "x", "x", True)
            with self.assertRaises(pv.ProvisionError, msg=argv) as ctx:
                pv.validate_plan(plan, self.policy)
            self.assertIn("outside the plan root", str(ctx.exception))


class PathGrammarTests(HardeningCase):
    def mkdir_problem(self, path):
        with self.assertRaises(pv.ProvisionError, msg=repr(path)):
            pv.classify_argv(["mkdir", path], self.policy)

    def test_hostile_path_forms_are_refused(self):
        r, b = self.root, os.sep
        forms = (r + b + "x.", r + b + "x ", r + b + "x::$DATA", r + b + "CON",
                 r + b + "NUL", r + b + "aux.txt", r + b + "COM1",
                 r + b + "LPT9.log", r + "::$DATA", r + b + "x:stream",
                 r + b + "x\u200b", r + b + "\u202ex", r + b + "\x1b[2Kx",
                 r + b + "\x07", r + b + "\tx", r + "\uff3cx", r + "\uff0fx",
                 r + "\u2215x", r + b + "PROGRA~1", r + b + "ab~12.txt")
        for path in forms:
            self.mkdir_problem(path)

    def test_extended_unc_and_relative_forms_are_refused(self):
        r = self.root
        for path in ("\\\\.\\" + r, "\\\\?\\" + r + "\\x", "//?/" + r.replace(
                "\\", "/") + "/x", "C:x", "\\Users\\x", "x"):
            self.mkdir_problem(path)

    def test_equivalent_spellings_of_the_root_are_still_in_the_root(self):
        r = self.root
        for path in (r + os.sep + "x", r.replace("\\", "/") + "/x",
                     r + os.sep + "a" + os.sep + ".." + os.sep + "x"):
            self.assertEqual(self.admit("mkdir", path).rule, "mkdir")
        self.mkdir_problem(r + os.sep + "x" + os.sep + ".." + os.sep + ".."
                           + os.sep + "outside")
        self.mkdir_problem(r + "x" + os.sep + "y")  # sibling sharing a prefix

    def test_trusted_executable_spellings(self):
        trusted = os.path.join(self.tmp, "Trusted", "python.exe")
        policy = pv.ProvisionPolicy(approved_roots=(self.root,),
                                    trusted_executables=(trusted,))
        self.assertEqual(
            self.admit(trusted, "--version", policy=policy).rule, "python-version")
        for bad in (trusted + ":evil", trusted + ".", trusted + " ",
                    os.path.join(self.tmp, "Trusted", "pythonw.exe"),
                    os.path.join(self.tmp, "Trusted", "python.bat")):
            with self.assertRaises(pv.ProvisionError, msg=bad):
                pv.classify_argv([bad, "--version"], policy)

    def test_root_path_forms_for_executables(self):
        r, b = self.root, os.sep
        for exe in (r + b + "pythonw.exe", r + b + "python3..",
                    r + b + "pip.cmd.", r + b + "python.bat ",
                    r + b + "mkdir.exe"):
            with self.assertRaises(pv.ProvisionError, msg=exe):
                pv.classify_argv([exe, "--version"], self.policy)
        self.admit(r + b + "PY.EXE", "--version")  # a launcher inside the root

    def test_bare_executable_names_are_plain_ascii(self):
        kelvin = "m" + chr(0x212A) + "dir"
        for exe in ("mkdir.cmd", kelvin, "pip ", "python\u00a0", " python",
                    "python.exe:x", " ", "python.", "pip\u0000", "p\u0131p"):
            with self.assertRaises(pv.ProvisionError, msg=repr(exe)):
                pv.classify_argv([exe, os.path.join(self.root, "x")]
                                 if "mkdir" in exe.lower() or exe == kelvin
                                 else [exe, "--version"], self.policy)
        self.assertEqual(self.admit("PYTHON.EXE", "--version").rule,
                         "python-version")
        self.assertEqual(self.admit("Python3.12", "-V").rule, "python-version")

    def test_policy_roots_may_be_short_names_but_step_paths_may_not(self):
        short_root = os.path.join(self.tmp, "RUNNER~1")
        pv.ProvisionPolicy(approved_roots=(short_root,))  # fine for a root
        self.assertIn("8.3", pv._path_problem(short_root))


class HelperEdgeTests(HardeningCase):
    def test_helpers_reject_what_the_classifier_would_have_caught_first(self):
        self.assertEqual(pv._unsafe_chars(5), [])
        self.assertEqual(pv._unsafe_chars("a​b"), ["", "​"])
        self.assertIn("control or invisible",
                      pv._path_problem(os.path.join(self.root, "x")))
        with self.assertRaises(pv.ProvisionError):
            pv._check_executable("pıp", self.policy)  # dotless i


class ProbeIgnoresPlantedProgramsTests(TempRootCase):
    def test_a_program_found_in_the_working_tree_counts_as_absent(self):
        plant = os.path.join(self.tmp, "plant")
        os.makedirs(os.path.join(plant, "sub"))
        before = os.getcwd()
        self.addCleanup(os.chdir, before)
        os.chdir(plant)
        runner = FakeRunner(default=osal.CommandResult(1, "", "no"))
        found = {"winget": os.path.join(plant, "winget.exe"),
                 "choco": os.path.join(plant, "sub", "choco.exe"),
                 "npm": "npm.cmd",  # a relative hit is the working directory
                 "brew": os.path.join(FAKE_BIN, "brew")}
        probe = pv.probe_host(self.root, runner=runner, facts=dict(
            os="Linux", arch="x86_64", python_version="3.12",
            python_executable="python", free_disk_bytes=1),
            which=lambda name: found.get(name))
        self.assertEqual([m.name for m in probe.package_managers], ["brew"])
        ran = [call["resolved"][0] for call in runner.calls]
        self.assertEqual(ran[0], os.path.join(FAKE_BIN, "brew"))  # absolute path
        self.assertNotIn(os.path.join(plant, "winget.exe"), ran)
        self.assertNotIn("npm.cmd", ran)

    def test_probe_runs_the_absolute_path_not_the_bare_name(self):
        runner = FakeRunner()
        pv.probe_host(self.root, runner=runner, which=which_of("pip"),
                      facts=dict(os="Linux", arch="x", python_version="3",
                                 python_executable="python",
                                 free_disk_bytes=1))
        self.assertTrue(os.path.isabs(runner.calls[0]["resolved"][0]))


class ChurnGuardsTests(TempRootCase):
    def test_a_filesystem_root_is_never_a_working_directory_exclusion(self):
        top = os.path.abspath(os.sep)
        with mock.patch.object(pv.os, "getcwd", return_value=top):
            self.assertFalse(pv._in_working_directory(
                os.path.join(top, "usr", "bin", "python")))
            self.assertTrue(pv._in_working_directory(
                os.path.join(top, "python.exe")))

    def test_dry_run_still_creates_no_private_directory(self):
        plan = pv.Plan("g", [step()], self.root, "x", "x", True)
        pv.execute_plan(plan, pv.ApprovalGate(), policy=self.policy,
                        runner=FakeRunner())
        self.assertEqual(os.listdir(self.root), [])


if __name__ == "__main__":
    unittest.main()
