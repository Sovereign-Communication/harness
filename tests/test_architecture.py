"""Architecture guard: the import-direction rule from CONTRIBUTING.md.

The structure is layered -- interfaces (cli, mcp) import engines, engines
import shared lanes, lanes import primitives. Data flows one way. This test
keeps it that way without anyone having to re-read the docs:

1. No product module may import an interface (cli/mcp). Interfaces are the
   top of the dependency graph; anything reaching up is a violation.
2. No module may re-export another module's names. A facade indirection (the
   old harness/core.py shim) doubles every dependency edge and hides the real
   owner; import from the owning module at the use site instead. Mechanically:
   a module-level import that the module's own code never references is a
   re-export, not a use.
"""
import ast
import os
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(HERE, "harness")
INTERFACES = {"cli", "mcp"}


def _module_names():
    for fn in sorted(os.listdir(PKG)):
        if fn.endswith(".py") and fn != "__pycache__":
            yield os.path.splitext(fn)[0], os.path.join(PKG, fn)


def _intra_package_imports(tree):
    """Yield the top-level module name of every intra-package import."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level >= 1 and node.module:
            yield node.module.split(".")[0]
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == "harness" and len(parts) > 1:
                    yield parts[1]


def _dunder_all_names(tree):
    for node in tree.body:
        if (isinstance(node, ast.Assign) and
                any(getattr(t, "id", None) == "__all__" for t in node.targets)):
            if isinstance(node.value, (ast.List, ast.Tuple)):
                return {e.value for e in node.value.elts
                        if isinstance(e, ast.Constant)}
    return set()


def _reexported_names(tree):
    """Module-level intra-package imports that the module's own code never
    references: by definition a re-export shim, not a use."""
    bound = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.level >= 1 and node.module:
            for alias in node.names:
                if alias.name != "*":
                    bound.append(alias.asname or alias.name)
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    used |= _dunder_all_names(tree)
    return [n for n in bound if n not in used]


class ResultVocabularyTests(unittest.TestCase):
    """The result shapes CLI/MCP consume are owned by harness/results.py --
    apply.py must import them, never redefine them (one def site per shape)."""

    VOCABULARY = {"_round_entry", "_terminal_result", "_defer_result", "_http_error"}

    def test_result_vocabulary_has_one_def_site(self):
        offenders = []
        for name, path in _module_names():
            if name == "results":
                continue
            with open(path, encoding="utf-8") as source:
                tree = ast.parse(source.read(), path)
            for node in tree.body:
                if isinstance(node, ast.FunctionDef) and node.name in self.VOCABULARY:
                    offenders.append(f"{name}.py defines {node.name}")
        self.assertEqual(offenders, [],
                         "the result vocabulary has exactly one def site "
                         "(harness/results.py): " + ", ".join(offenders))


class InterfaceHygieneTests(unittest.TestCase):
    """Interfaces import their policy at module level. Function-level imports
    inside main() are the documented lazy-entry seam; anywhere else they hide
    dependency edges and make layering unreadable."""

    def test_no_deferred_policy_imports_outside_main(self):
        offenders = []
        for name, path in _module_names():
            if name not in INTERFACES:
                continue
            with open(path, encoding="utf-8") as source:
                tree = ast.parse(source.read(), path)
            for node in ast.walk(tree):
                if not (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and node.name != "main"):
                    continue
                for sub in ast.walk(node):
                    if (isinstance(sub, ast.ImportFrom) and sub.level >= 1
                            and sub.module and sub.module != name):
                        offenders.append(f"{name}.{node.name} imports {sub.module}")
        self.assertEqual(offenders, [],
                         "interface functions import policy at module level, "
                         "not lazily: " + ", ".join(offenders))


class ImportDirectionTests(unittest.TestCase):
    def test_no_module_imports_an_interface(self):
        offenders = []
        for name, path in _module_names():
            if name in INTERFACES:
                continue
            with open(path, encoding="utf-8") as source:
                tree = ast.parse(source.read(), path)
            for target in _intra_package_imports(tree):
                if target in INTERFACES:
                    offenders.append(f"{name}.py -> {target}")
        self.assertEqual(offenders, [],
                         "interface modules must not be imported by the layers "
                         "beneath them: " + ", ".join(offenders))

    def test_no_reexport_facades(self):
        offenders = []
        for name, path in _module_names():
            if name in INTERFACES:
                continue
            with open(path, encoding="utf-8") as source:
                tree = ast.parse(source.read(), path)
            for dead in _reexported_names(tree):
                offenders.append(f"{name}.py re-exports {dead}")
        self.assertEqual(offenders, [],
                         "no module may re-export another module's names "
                         "(import from the owner at the use site): "
                         + ", ".join(offenders))

    def test_engine_construction_is_session_only(self):
        """Engine/router construction has ONE owner (harness/session.py).
        The history that motivates this guard: an engine built without its
        api_key (round-1 dogfood) and MCP's copy missing the use_free
        threading -- both were construction-site drift. Interfaces receive
        engines from session builders; they must not construct them."""
        offenders = []
        for name, path in _module_names():
            if name == "session":
                continue
            with open(path, encoding="utf-8") as source:
                tree = ast.parse(source.read(), path)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    fn = node.func
                    target = (fn.attr if isinstance(fn, ast.Attribute)
                              else fn.id if isinstance(fn, ast.Name) else None)
                    if target in ("ApplyEngine", "Router"):
                        offenders.append(f"{name}.py constructs {target}")
        self.assertEqual(offenders, [],
                         "ApplyEngine/Router construction belongs to "
                         "harness/session.py only: " + ", ".join(offenders))


class SchemaModulePurityTests(unittest.TestCase):
    """mcp_schemas.py is pure data: the MCP tool contract dicts and nothing
    else. Its docstring states the keep-it-data-only rule; this mechanizes it
    so the rule holds for as long as the guard runs."""

    def test_mcp_schemas_is_data_only(self):
        with open(os.path.join(PKG, "mcp_schemas.py"), encoding="utf-8") as src:
            tree = ast.parse(src.read(), "mcp_schemas.py")
        offenders = [type(node).__name__ for node in tree.body
                     if isinstance(node, (ast.Import, ast.ImportFrom,
                                          ast.FunctionDef, ast.AsyncFunctionDef,
                                          ast.ClassDef))]
        self.assertEqual(offenders, [],
                         "mcp_schemas.py must stay data-only (no imports, "
                         "no defs): " + ", ".join(offenders))


if __name__ == "__main__":
    unittest.main()
