"""Cold D5 activation and the existing registry patch point."""
import ast
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/hanig-swarm/scripts"
SOURCE = SCRIPTS / "d5_activation.py"
sys.path.insert(0, str(ROOT / "skills/hanig-orchestrate/scripts"))
import d5_registry as registry
import d5_activation as activation


# Setup happens before the observation window, in an isolated interpreter.
# An audit hook covers source/cache reads and rejects other audited effects.
# The import wrapper also catches imports of already-cached modules.
COLD_PROBE = r'''
import builtins
import importlib.util
import os
import sys

directory = sys.argv[1]
source = os.path.join(directory, "d5_activation.py")
cache = importlib.util.cache_from_source(source)
sys.path[:] = [directory]
before_modules = dict(sys.modules)
before_path = list(sys.path)
opened = []
observing = True
real_import = builtins.__import__

def checked_import(name, globals=None, locals=None, fromlist=(), level=0):
    if globals and globals.get("__name__") == "d5_activation":
        raise AssertionError("activation imported " + name)
    return real_import(name, globals, locals, fromlist, level)

def audit(event, args):
    if not observing:
        return
    if event == "open":
        path, mode, flags = args
        assert path in (source, cache), ("unexpected open", path)
        assert not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC
                            | os.O_APPEND), ("write", path)
        opened.append(path)
    else:
        assert event in {"import", "os.listdir", "compile", "exec", "marshal.loads"}, event

builtins.__import__ = checked_import
sys.addaudithook(audit)
try:
    import d5_activation
    after_modules = dict(sys.modules)
    after_path = list(sys.path)
    assert d5_activation.d5_active() is True, "activation is not True"
finally:
    observing = False
    builtins.__import__ = real_import

assert set(after_modules) - set(before_modules) == {"d5_activation"}
assert all(after_modules.get(name) is module for name, module in before_modules.items())
assert after_path == before_path, "sys.path changed"
assert sys.path == before_path, "predicate changed sys.path"
assert sys.modules == after_modules, "predicate changed sys.modules"
assert opened, "probe did not observe the source/cache loader"
print("cold-import: OK")
'''


def cold_probe(directory):
    return subprocess.run([sys.executable, "-I", "-S", "-B", "-c", COLD_PROBE,
                           str(directory)], capture_output=True, text=True, timeout=30)


class TestD5Activation(unittest.TestCase):
    def test_cold_import(self):
        result = cold_probe(SCRIPTS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "cold-import: OK\n")

    def test_cold_import_without_siblings(self):
        with tempfile.TemporaryDirectory() as temporary:
            (Path(temporary) / SOURCE.name).write_bytes(SOURCE.read_bytes())
            result = cold_probe(temporary)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_only_a_function_with_no_imports_or_definition_effects(self):
        tree = ast.parse(SOURCE.read_text())
        self.assertEqual(len(tree.body), 1)
        function = tree.body[0]
        self.assertIsInstance(function, ast.FunctionDef)
        self.assertEqual(function.name, "d5_active")
        self.assertEqual(function.decorator_list, [])
        self.assertEqual(ast.dump(function.args), ast.dump(ast.arguments(
            posonlyargs=[], args=[], vararg=None, kwonlyargs=[], kw_defaults=[],
            kwarg=None, defaults=[])))
        self.assertIsNone(function.returns)
        self.assertEqual(getattr(function, "type_params", []), [])
        self.assertEqual(ast.get_docstring(function),
                         "U8 owns activation. There is no runtime override.")
        self.assertFalse(any(isinstance(n, (ast.Import, ast.ImportFrom))
                             for n in ast.walk(tree)))

    def test_production_active(self):
        self.assertIs(activation.d5_active(), True)
        self.assertIs(registry.d5_active(), True)

    def test_activation_patch_reaches_registry(self):
        for value in (True, False):
            with self.subTest(value=value), \
                 mock.patch.object(activation, "d5_active", return_value=value) as predicate:
                self.assertIs(registry.d5_active(), value)
                predicate.assert_called_once_with()

    def test_registry_patch_remains_local_and_reaches_consumer(self):
        with mock.patch.object(registry, "d5_active", return_value=False) as predicate:
            with self.assertRaisesRegex(registry.AuthorityError, "D5 is not active"):
                registry._active()
            predicate.assert_called_once_with()
            self.assertIs(activation.d5_active(), True)
        registry._active()

    def test_import_mutations_fail_cold_probe(self):
        for statement in ("import d5_registry", "import sys", "from os import path"):
            with self.subTest(statement=statement), tempfile.TemporaryDirectory() as temporary:
                (Path(temporary) / SOURCE.name).write_text(
                    statement + "\n" + SOURCE.read_text())
                result = cold_probe(temporary)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("activation imported", result.stderr)


if __name__ == "__main__":
    unittest.main()
