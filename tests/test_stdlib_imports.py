"""Static dependencies of authored scripts are stdlib or repo-local.

This sweep checks every explicit ``import`` and ``from ... import`` statement
in the declared source roots. Dynamic loading and runtime module origin are
deliberately outside its finite source-declaration contract; a runtime that
needs those guarantees must establish them in its canary or preflight.
"""

import ast
import sys
import sysconfig
import tempfile
import tokenize
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]



def stdlib_module_roots():
    """Top-level standard-library module names, on 3.9 as well as 3.10+.

    ``sys.stdlib_module_names`` is 3.10+, and release validation pins 3.9, so
    reading it unguarded raised AttributeError on both CI runners while passing
    on a 3.12 developer host. The sweep then certified nothing exactly where it
    was meant to run.

    The fallback enumerates the interpreter's own stdlib directory rather than
    carrying a hand-written list, because a list rots and a rotted list reads as
    a third-party import.
    """
    names = set(sys.builtin_module_names)
    modern = getattr(sys, "stdlib_module_names", None)
    if modern is not None:
        return names | set(modern)
    for key in ("stdlib", "platstdlib"):
        directory = sysconfig.get_paths().get(key)
        if not directory:
            continue
        base = Path(directory)
        if not base.is_dir():
            continue
        for entry in base.iterdir():
            if entry.suffix == ".py":
                names.add(entry.stem)
            elif entry.is_dir() and (entry / "__init__.py").exists():
                names.add(entry.name)
        dynload = base / "lib-dynload"
        if dynload.is_dir():
            for entry in dynload.iterdir():
                names.add(entry.name.split(".", 1)[0])
    return names


def authored_python_files():
    skill_scripts = ROOT.glob("skills/hanig-*/scripts/**/*.py")
    library_files = (ROOT / "lib").rglob("*.py")
    return sorted([*skill_scripts, *library_files])


def parse_python(path):
    with tokenize.open(path) as source:
        return ast.parse(source.read(), filename=str(path))


def repo_local_import_roots(files):
    roots = set()
    for path in files:
        relative = path.relative_to(ROOT)
        if relative.parts[0] == "lib":
            roots.add("lib")
            module_parts = relative.parts[1:]
        else:
            scripts_index = relative.parts.index("scripts")
            module_parts = relative.parts[scripts_index + 1:]
        if not module_parts:
            continue
        first = module_parts[0]
        roots.add(Path(first).stem if first.endswith(".py") else first)
    return roots


def declared_imports(tree):
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom) and not node.level:
            found.append((node.lineno, node.module or ""))
    return found


def outside_declared_imports(tree, allowed):
    return [(line, name) for line, name in declared_imports(tree)
            if name.split(".", 1)[0] not in allowed]


class TestAuthoredScriptsUseOnlyTheStandardLibrary(unittest.TestCase):

    def test_nested_repo_local_package_roots_are_allowed(self):
        future_files = [
            ROOT / "lib" / "pkg" / "submodule.py",
            ROOT / "skills" / "hanig-demo" / "scripts" / "helpers"
            / "formatting.py",
        ]
        roots = repo_local_import_roots(future_files)
        self.assertEqual(roots, {"lib", "pkg", "helpers"})

    def test_the_contract_covers_import_statements_not_dynamic_loading(self):
        tree = ast.parse(
            "import os.path as os_path\n"
            "import os, requests.adapters\n"
            "from pathlib import Path\n"
            "from urllib.request import urlopen\n"
            "from . import sibling\n"
            "from ..pkg import helper\n"
            "import importlib.util\n"
            "importlib.import_module('dynamic_dependency')\n"
            "__import__('another_dynamic_dependency')\n")
        names = [name for _line, name in declared_imports(tree)]
        for expected in ("os.path", "os", "requests.adapters", "pathlib",
                         "urllib.request", "importlib.util"):
            self.assertIn(expected, names)
        self.assertNotIn("dynamic_dependency", names)
        self.assertNotIn("another_dynamic_dependency", names)
        self.assertEqual(
            outside_declared_imports(
                tree, {"os", "pathlib", "urllib", "importlib"}),
            [(2, "requests.adapters")])

    def test_python_source_encoding_declarations_are_honored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "encoded.py"
            path.write_bytes(
                b"# coding: latin-1\n# caf\xe9\nimport third_party\n")
            tree = parse_python(path)
        self.assertEqual(
            outside_declared_imports(tree, set()),
            [(3, "third_party")])

    def test_every_import_is_stdlib_or_repo_local(self):
        files = authored_python_files()
        self.assertTrue(files, "the authored-script sweep matched no files")

        local_modules = repo_local_import_roots(files)
        allowed = stdlib_module_roots() | local_modules
        outside = []
        for path in files:
            tree = parse_python(path)
            for line, name in outside_declared_imports(tree, allowed):
                outside.append(
                    "%s:%d imports %s" %
                    (path.relative_to(ROOT), line, name))

        self.assertEqual(outside, [], "\n".join(outside))
