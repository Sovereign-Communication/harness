"""PLAT-osal-module: hermetic tests for every primitive in the OS owner.

Each test here is the hermetic double for a claim the 2026-09-26 scout made
on a live machine: a shell string breaks forward-slash paths on Windows,
``shlex`` eats backslashes, case-sensitive path comparison fails on
case-insensitive filesystems, CRLF text writes fork the evidence bytes, and
key-file permission bits do not exist on Windows. Where a claim is
platform-specific, the test asserts the *documented* answer on the running
platform rather than pretending otherwise.
"""
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

from harness import osal


class TempTree(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = self.dir.name

    def path(self, *parts):
        return os.path.join(self.root, *parts)


class OsalRunTest(TempTree):
    def test_argv_runs_and_captures_output(self):
        result = osal.run([sys.executable, "-c", "print('hello')"])
        self.assertEqual(result.returncode, 0)
        self.assertIn("hello", result.combined)

    def test_stderr_is_part_of_the_record(self):
        result = osal.run([sys.executable, "-c",
                           "import sys; sys.stderr.write('warn'); sys.exit(3)"])
        self.assertEqual(result.returncode, 3)
        self.assertIn("warn", result.combined)

    def test_a_command_string_is_refused(self):
        """The whole point: gates are data. A string is a shell waiting."""
        with self.assertRaises(osal.OsalError):
            osal.run("echo hi")
        with self.assertRaises(osal.OsalError):
            osal.run([])

    def test_metacharacters_are_inert_without_a_shell(self):
        result = osal.run([sys.executable, "-c", "print('safe')"])
        self.assertEqual(result.returncode, 0)
        self.assertNotIn(";", result.stdout)
        # A "gate" that tried to chain would have to be a real argv element;
        # the runner never re-parses anything it is handed.
        chained = osal.run([sys.executable, "-c",
                            "import sys; print(sys.argv[1])", "a && rm -rf /"])
        self.assertIn("a && rm -rf /", chained.stdout)

    def test_missing_executable_is_127(self):
        result = osal.run(["harness-definitely-not-a-real-binary-xyz"])
        self.assertEqual(result.returncode, 127)

    def test_timeout_is_124_not_a_hang(self):
        result = osal.run([sys.executable, "-c", "import time; time.sleep(30)"],
                          timeout=1)
        self.assertEqual(result.returncode, 124)

    def test_cwd_is_honored(self):
        result = osal.run([sys.executable, "-c", "import os; print(os.getcwd())"],
                          cwd=self.root)
        self.assertEqual(osal.norm_path(result.stdout.strip()),
                         osal.norm_path(self.root))

    def test_undecodable_output_does_not_raise(self):
        result = osal.run([sys.executable, "-c",
                           "import sys; sys.stdout.buffer.write(b'\\xff\\xfe')"])
        self.assertEqual(result.returncode, 0)


class OsalTextIoTest(TempTree):
    def test_write_text_is_lf_on_every_platform(self):
        p = self.path("evidence.jsonl")
        osal.write_text(p, "a\nb\n")
        with open(p, "rb") as handle:
            raw = handle.read()
        self.assertEqual(raw, b"a\nb\n")
        self.assertNotIn(b"\r\n", raw)

    def test_read_text_does_not_translate(self):
        p = self.path("crlf.txt")
        with open(p, "wb") as handle:
            handle.write(b"one\r\ntwo\r\n")
        self.assertEqual(osal.read_text(p), "one\r\ntwo\r\n")

    def test_read_text_is_utf8_regardless_of_locale(self):
        p = self.path("utf8.txt")
        osal.write_text(p, "café ✓\n")
        self.assertEqual(osal.read_text(p), "café ✓\n")

    def test_detect_newline(self):
        crlf = self.path("crlf2.txt")
        lf = self.path("lf2.txt")
        bare = self.path("bare.txt")
        for path, data in ((crlf, b"x\r\ny"), (lf, b"x\ny"), (bare, b"xy")):
            with open(path, "wb") as handle:
                handle.write(data)
        self.assertEqual(osal.detect_newline(crlf), "\r\n")
        self.assertEqual(osal.detect_newline(lf), "\n")
        self.assertIsNone(osal.detect_newline(bare))
        self.assertIsNone(osal.detect_newline(self.path("missing.txt")))

    def test_atomic_write_replaces_and_leaves_no_temp(self):
        p = self.path("target.py")
        osal.atomic_write_text(p, "x = 1\n")
        osal.atomic_write_text(p, "x = 2\n")
        self.assertEqual(osal.read_text(p), "x = 2\n")
        self.assertEqual([n for n in os.listdir(self.root) if n.startswith(".harness-")],
                         [])

    def test_atomic_write_preserves_the_targets_newline_style(self):
        p = self.path("crlf3.py")
        with open(p, "wb") as handle:
            handle.write(b"a\r\nb\r\n")
        osal.atomic_write_text(p, "a\nb\nc\n")
        with open(p, "rb") as handle:
            self.assertEqual(handle.read(), b"a\r\nb\r\nc\r\n")

    def test_atomic_write_newline_none_writes_exact_bytes(self):
        p = self.path("snapshot.bin")
        osal.atomic_write_text(p, "a\r\nb", newline=None)
        with open(p, "rb") as handle:
            self.assertEqual(handle.read(), b"a\r\nb")

    @unittest.skipIf(osal.IS_WINDOWS, "POSIX symlink semantics")
    def test_atomic_write_refuses_a_symlink(self):
        real = self.path("real.txt")
        link = self.path("link.txt")
        osal.atomic_write_text(real, "safe\n")
        try:
            os.symlink(real, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable here")
        with self.assertRaises(osal.AtomicWriteError):
            osal.atomic_write_text(link, "clobbered\n")
        self.assertEqual(osal.read_text(real), "safe\n")

    @unittest.skipIf(osal.IS_WINDOWS, "POSIX mode bits")
    def test_atomic_write_preserves_permission_mode(self):
        p = self.path("gate.sh")
        osal.atomic_write_text(p, "#!/bin/sh\nexit 0\n")
        os.chmod(p, 0o755)
        osal.atomic_write_text(p, "#!/bin/sh\nexit 1\n")
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o755)

    @unittest.skipIf(osal.IS_WINDOWS, "POSIX mode bits")
    def test_new_file_stays_private(self):
        p = self.path("fresh.txt")
        osal.atomic_write_text(p, "x\n")
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode) & 0o077, 0)


class OsalBoundedRunTest(TempTree):
    def test_small_output_comes_back_as_bytes(self):
        out = osal.run_bounded([sys.executable, "-c",
                                "import sys; sys.stdout.buffer.write(b'abc')"],
                               max_bytes=1024)
        self.assertEqual(out, b"abc")

    def test_oversized_output_is_refused_not_truncated(self):
        code = "import sys; sys.stdout.buffer.write(b'x' * 500000)"
        self.assertIsNone(osal.run_bounded([sys.executable, "-c", code],
                                           max_bytes=1024, timeout=30))

    def test_failure_and_timeout_are_none(self):
        self.assertIsNone(osal.run_bounded([sys.executable, "-c",
                                            "import sys; sys.exit(1)"], 1024))
        self.assertIsNone(osal.run_bounded(
            [sys.executable, "-c", "import time; time.sleep(30)"], 1024, timeout=1))
        self.assertIsNone(osal.run_bounded(["harness-definitely-not-real-xyz"], 16))

    def test_refuses_a_command_string_and_a_negative_bound(self):
        with self.assertRaises(osal.OsalError):
            osal.run_bounded("git log", 16)
        self.assertIsNone(osal.run_bounded([sys.executable, "-c", "pass"], -1))

    def test_cwd_is_honored(self):
        out = osal.run_bounded([sys.executable, "-c",
                                "import os,sys; sys.stdout.write(os.getcwd())"],
                               max_bytes=4096, cwd=self.root)
        self.assertEqual(osal.norm_path(out.decode("utf-8")),
                         osal.norm_path(self.root))


    def test_the_reader_never_exceeds_the_bound_plus_one(self):
        """The cap is enforced by the READER, not by the child's good graces:
        a pipe can hand over more than the bound before we can stop it, so the
        drain asks for at most max_bytes+1 and kills the child on overflow."""
        import io

        class CountingBytesIO(io.BytesIO):
            def __init__(self, value):
                super().__init__(value)
                self.bytes_read = 0

            def read1(self, size=-1):
                value = super().read(size)
                self.bytes_read += len(value)
                return value

        class FakeProcess:
            def __init__(self):
                self.stdout = CountingBytesIO(b"0123456789")
                self.killed = False

            def wait(self, timeout=None):
                return 0

            def kill(self):
                self.killed = True

        process = FakeProcess()
        with mock.patch("harness.osal.subprocess.Popen", return_value=process):
            self.assertIsNone(osal.run_bounded(["git", "log"], max_bytes=4))
        self.assertTrue(process.killed)
        self.assertEqual(process.stdout.bytes_read, 5,
                         "read exactly max_bytes+1 before declaring overflow")

    def test_a_hung_child_is_killed_and_its_pipe_closed(self):
        import io
        import threading

        class BlockingPipe:
            def __init__(self):
                self.started = threading.Event()
                self.closed_event = threading.Event()
                self.closed = False

            def read1(self, _size):
                self.started.set()
                self.closed_event.wait()
                return b""

            def close(self):
                self.closed = True
                self.closed_event.set()

        class StuckProcess:
            def __init__(self):
                self.stdout = BlockingPipe()
                self.killed = False
                self.reader_started = False

            def wait(self, timeout=None):
                if timeout is not None:
                    # Generous: the drain thread is a Python thread, so under a
                    # traced (20x slower) battery a 2s handshake is a flake,
                    # not a contract.
                    self.reader_started = self.stdout.started.wait(30)
                    return 0
                return -9

            def kill(self):
                self.killed = True

        process = StuckProcess()
        with mock.patch("harness.osal.subprocess.Popen", return_value=process):
            self.assertIsNone(osal.run_bounded(["git", "log"], max_bytes=8,
                                               timeout=5))
        self.assertTrue(process.killed)
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.reader_started)
        del io

    def test_a_child_with_no_stdout_pipe_is_not_leaked(self):
        class MissingStdout:
            stdout = None

            def __init__(self):
                self.killed = False
                self.waited = False

            def wait(self):
                self.waited = True
                return 0

            def kill(self):
                self.killed = True

        process = MissingStdout()
        with mock.patch("harness.osal.subprocess.Popen", return_value=process):
            self.assertIsNone(osal.run_bounded(["git", "log"], max_bytes=8))
        self.assertTrue(process.killed)
        self.assertTrue(process.waited)

    def test_a_timed_out_child_is_killed(self):
        import io
        import subprocess as _subprocess

        class TimeoutProcess:
            def __init__(self):
                self.stdout = io.BytesIO(b"")   # a real pipe: the drain runs
                self.killed = False
                self.wait_calls = 0

            def wait(self, timeout=None):
                self.wait_calls += 1
                if timeout is not None:
                    raise _subprocess.TimeoutExpired("git", timeout)
                return -9  # the post-kill reap

            def kill(self):
                self.killed = True

        process = TimeoutProcess()
        with mock.patch("harness.osal.subprocess.Popen", return_value=process):
            self.assertIsNone(osal.run_bounded(["git", "log"], max_bytes=8,
                                               timeout=1))
        self.assertTrue(process.killed)
        self.assertEqual(process.wait_calls, 2,
                         "the wait that times out, plus the reap after kill")


class OsalPathTest(TempTree):
    def test_display_path_is_separator_neutral(self):
        self.assertEqual(osal.display_path("harness/jev.py"), "harness/jev.py")
        self.assertEqual(osal.display_path("harness\\jev.py"), "harness/jev.py")
        self.assertEqual(osal.display_path("C:\\repo\\harness"), "C:/repo/harness")
        self.assertEqual(osal.display_path("C:/repo//harness/"), "C:/repo/harness")
        self.assertIsNone(osal.display_path(None))

    def test_display_path_keeps_the_root_separator(self):
        self.assertEqual(osal.display_path("C:/"), "C:/")
        self.assertEqual(osal.display_path("/"), "/")

    def test_norm_path_absolves_and_resolves(self):
        nested = self.path("a", "..", "b")
        os.makedirs(self.path("a"))
        self.assertEqual(osal.norm_path(nested), osal.norm_path(self.path("b")))

    def test_norm_path_case_folds_only_where_the_filesystem_does(self):
        path = self.path("CaseSensitive.py")
        open(path, "w").close()
        folded = osal.norm_path(path) == osal.norm_path(path.upper())
        # The filesystem is the authority -- not os.name, not sys.platform:
        # default macOS volumes fold case even though posixpath.normcase
        # does not, and a case-sensitive volume on any OS does not.
        self.assertEqual(folded, os.path.exists(path.upper()))

    def test_same_path_and_is_within(self):
        inside = self.path("pkg", "mod.py")
        os.makedirs(os.path.dirname(inside))
        open(inside, "w").close()
        sibling = self.path("pkg-other", "mod.py")
        os.makedirs(os.path.dirname(sibling))
        open(sibling, "w").close()
        self.assertTrue(osal.same_path(inside, os.path.join(self.root, "pkg", ".", "mod.py")))
        self.assertTrue(osal.is_within(inside, self.root))
        self.assertTrue(osal.is_within(self.root, self.root))
        # The prefix trap: "pkg-other" is not inside "pkg".
        self.assertFalse(osal.is_within(sibling, os.path.join(self.root, "pkg")))

    def test_normalize_roots_drops_nothing_and_normalizes(self):
        roots = osal.normalize_roots([self.path("pkg"), self.path("other")])
        self.assertEqual(roots, [osal.norm_path(self.path("pkg")),
                                 osal.norm_path(self.path("other"))])
        self.assertEqual(osal.normalize_roots(None), [])

    def test_is_within_follows_a_symlinked_parent(self):
        if osal.IS_WINDOWS:
            self.skipTest("symlink creation needs privileges on Windows")
        outside = tempfile.mkdtemp(prefix="osal_outside_")
        self.addCleanup(lambda: __import__("shutil").rmtree(outside, ignore_errors=True))
        inside = self.path("real")
        os.makedirs(inside)
        link = self.path("link")
        os.symlink(outside, link)
        self.assertFalse(osal.is_within(os.path.join(link, "x.txt"), self.root))


class OsalKeyfileTest(TempTree):
    @unittest.skipIf(osal.IS_WINDOWS, "POSIX mode bits")
    def test_permissive_keyfile_is_flagged(self):
        p = self.path("jev.env")
        with open(p, "w") as handle:
            handle.write("OPENROUTER_API_KEY=sk-test\n")
        os.chmod(p, 0o644)
        self.assertEqual(osal.keyfile_mode(p), 0o644)
        self.assertTrue(osal.keyfile_is_insecure(p))
        os.chmod(p, 0o600)
        self.assertFalse(osal.keyfile_is_insecure(p))

    def test_windows_answers_not_modelled_rather_than_insecure(self):
        p = self.path("jev2.env")
        with open(p, "w") as handle:
            handle.write("OPENROUTER_API_KEY=sk-test\n")
        with mock.patch.object(osal, "IS_WINDOWS", True):
            self.assertIsNone(osal.keyfile_mode(p))
            self.assertFalse(osal.keyfile_is_insecure(p))

    def test_unreadable_keyfile_is_not_a_false_alarm(self):
        self.assertFalse(osal.keyfile_is_insecure(self.path("nope.env")))

    def test_config_warns_through_osal(self):
        """The caller delegates; there is no second permission policy."""
        from harness import config
        p = self.path("key3.env")
        with open(p, "w") as handle:
            handle.write("OPENROUTER_API_KEY=sk-test\n")
        with mock.patch.object(osal, "IS_WINDOWS", True):
            with mock.patch("harness.output.eprint") as warned:
                config._warn_insecure_keyfile(p)
        self.assertEqual(warned.call_count, 0)


class OsalUiTest(unittest.TestCase):
    def test_open_url_delegates_to_webbrowser(self):
        opened = []
        with mock.patch("webbrowser.open", lambda url: opened.append(url) or True):
            self.assertTrue(osal.open_url("http://127.0.0.1:8765/#tok"))
        self.assertEqual(opened, ["http://127.0.0.1:8765/#tok"])

    def test_open_url_reports_failure_instead_of_raising(self):
        def boom(url):
            raise OSError("no browser here")
        with mock.patch("webbrowser.open", boom):
            self.assertFalse(osal.open_url("http://127.0.0.1:8765/"))

    def test_python_exe_is_this_interpreter(self):
        self.assertEqual(osal.python_exe(), sys.executable)

    def test_which_finds_the_running_interpreter(self):
        self.assertIsNotNone(osal.which(os.path.basename(sys.executable))
                             or osal.which("python"))
        self.assertIsNone(osal.which("harness-definitely-not-a-real-binary-xyz"))


class OsalErrorsTest(unittest.TestCase):
    def test_error_types_are_oserrors_so_existing_handlers_still_work(self):
        """filesafety re-exports AtomicWriteError; its callers catch OSError."""
        self.assertTrue(issubclass(osal.AtomicWriteError, OSError))
        self.assertTrue(issubclass(osal.OsalError, OSError))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
