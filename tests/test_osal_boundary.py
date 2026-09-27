"""PLAT-osal-module: the OS boundary is enforced, not merely documented.

``harness/osal.py`` exists so platform behaviour has one owner. A docstring
is not enforcement, though: without a check, the third ``subprocess.run`` in
a helper is a week away and the divergence comes back. This test is that
check -- it walks the package with :mod:`ast` (not a regex, so a mention in
a comment or a docstring is not a violation and an aliased import is not a
smuggling route) and fails on OS contact outside the owner.

The banned set is deliberately small and exactly the four primitives the
platform-unification slice found diverging: a process launcher, a platform
probe, a second platform probe, and the browser hand-off. Adding a fifth
primitive to this list is a design decision, not a drive-by edit -- and
``harness/osal.py`` itself is exempt, since being the boundary is its job.
"""
import ast
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE = os.path.join(REPO_ROOT, "harness")

OWNER = "osal.py"

# (node description, predicate) -- each returns a human-readable reason.
BANNED = (
    ("subprocess import",
     lambda n: isinstance(n, (ast.Import, ast.ImportFrom))
     and any(a.name.split(".")[0] == "subprocess"
             for a in getattr(n, "names", []))),
    ("webbrowser import",
     lambda n: isinstance(n, (ast.Import, ast.ImportFrom))
     and any(a.name.split(".")[0] == "webbrowser"
             for a in getattr(n, "names", []))),
    ("os.name platform probe",
     lambda n: isinstance(n, ast.Attribute) and n.attr == "name"
     and isinstance(n.value, ast.Name) and n.value.id == "os"),
    ("sys.platform platform probe",
     lambda n: isinstance(n, ast.Attribute) and n.attr == "platform"
     and isinstance(n.value, ast.Name) and n.value.id == "sys"),
)


def _package_modules():
    for dirpath, dirnames, filenames in os.walk(PACKAGE):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in sorted(filenames):
            if name.endswith(".py"):
                yield os.path.join(dirpath, name)


class OsalBoundaryTest(unittest.TestCase):
    def test_os_contact_lives_only_in_osal(self):
        violations = []
        for path in _package_modules():
            rel = os.path.relpath(path, PACKAGE).replace("\\", "/")
            if rel == OWNER:
                continue
            with open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), filename=path)
            for node in ast.walk(tree):
                for label, is_banned in BANNED:
                    if is_banned(node):
                        violations.append(
                            f"harness/{rel}:{getattr(node, 'lineno', '?')}: {label} "
                            f"(belongs in harness/{OWNER})")
        self.assertEqual(
            violations, [],
            "OS contact outside the owner -- route it through harness/osal.py:\n  "
            + "\n  ".join(violations))

    def test_owner_actually_owns_the_banned_primitives(self):
        """The rule is only meaningful if osal is the module that uses them."""
        with open(os.path.join(PACKAGE, OWNER), encoding="utf-8") as handle:
            source = handle.read()
        for banned in ("import subprocess", "import webbrowser", "os.name",
                       "sys.executable"):
            self.assertIn(banned, source,
                          f"harness/{OWNER} must be where {banned!r} lives")

    def test_boundary_scan_covers_this_file_set(self):
        """Sanity: the scan found the package, not an empty directory."""
        rels = [os.path.relpath(p, PACKAGE) for p in _package_modules()]
        self.assertIn("cli.py", rels)
        self.assertIn(OWNER, rels)
        self.assertGreater(len(rels), 20)


class OsalSurfaceTest(unittest.TestCase):
    """The owner's public surface: what callers may rely on staying put."""

    def test_platform_flags_are_consistent(self):
        from harness import osal
        self.assertNotEqual(osal.IS_WINDOWS, osal.IS_POSIX)
        self.assertEqual(osal.IS_WINDOWS, sys.platform.startswith("win"))
        # A Windows-only hole: SO_REUSEADDR lets a second process take over a
        # bound loopback port there, and POSIX needs it to rebind after
        # TIME_WAIT. The answer lives in osal so the server cannot drift.
        self.assertEqual(osal.HARDEN_REUSE, osal.IS_WINDOWS)

    def test_documented_helpers_exist(self):
        from harness import osal
        for name in ("run", "run_bounded", "which", "read_text", "write_text",
                     "detect_newline", "atomic_write_text", "norm_path", "same_path",
                     "is_within", "display_path", "normalize_roots", "keyfile_mode",
                     "keyfile_is_insecure", "open_url", "python_exe"):
            self.assertTrue(callable(getattr(osal, name, None)),
                            f"harness.osal must keep {name}()")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
