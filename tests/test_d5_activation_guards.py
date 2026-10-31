"""D5 fixture construction and coordinator command inventories (OD6/OD7)."""
import ast
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = ('d5_' + 'test_support', 'd5_' + 'child_bootstrap')
ARTIFACTS = ('d5-readiness.json', 'd5-installed.json', 'installation.json')
# Only corruption tests may overwrite a real publisher's artifacts.
CORRUPTION = {
    ('test_d5_activation.py', 'test_broken_is_active_and_refuses'),
    ('test_d5_u45_retry_recovery.py', 'test_recovery_requires_live_lease_and_does_not_borrow_or_reorder'),
    ('test_d5_u4_0b2_submit.py', 'test_submit_validates_marker_installation_and_registration_binding'),
    ('test_d5_u4_0_readiness.py', 'test_reader_refuses_missing_malformed_and_mismatched_without_processes'),
    ('test_d5_u4_0b1_issuance.py', 'test_claim_without_readiness_and_invalid_bindings'),
}


def fixture_problems(source, filename):
    """Inspect Python, including executable snippets, without executing fixtures."""
    problems = []
    if any(word in source for word in FORBIDDEN):
        problems.append('obsolete activation helper reference')
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return problems
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    activation_modules = {'d5_activation'}
    readers = {'readiness', 'installation_state', 'classifier'}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            activation_modules.update(alias.asname or alias.name for alias in node.names
                                      if alias.name == 'd5_activation')
            readers.update(alias.asname or alias.name for alias in node.names
                           if alias.name in ('d5_readiness', 'd5_installation'))
    def attribute(node):
        return isinstance(node, ast.Attribute) and (
            node.attr == 'd5_active' or
            isinstance(node.value, ast.Name) and node.value.id in activation_modules)
    def function(node):
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return node.name
        return None
    # Record path provenance at each use, in statement order. Rebinding a local
    # clears that local; existing aliases and possible branch values survive.
    snapshots, attributes = {}, {}
    def enclosing_class(node):
        while node in parents:
            node = parents[node]
            if isinstance(node, ast.ClassDef):
                return node.name
        return None
    def key(node):
        if isinstance(node, ast.Name):
            node = ast.Name(id=node.id, ctx=ast.Load())
        elif isinstance(node, ast.Attribute):
            node = ast.Attribute(value=node.value, attr=node.attr, ctx=ast.Load())
        return ast.dump(node, include_attributes=False)
    def artifact(node, env=None):
        if env is None:
            env = snapshots.get(id(node), {})
        if isinstance(node, (ast.Name, ast.Attribute)) and key(node) in env:
            return env[key(node)]
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return any(node.value == name or node.value.endswith('/' + name) for name in ARTIFACTS)
        if (isinstance(node, ast.Attribute) and node.attr in ('MARKER', 'INSTALLATION', 'SENTINEL')
                and isinstance(node.value, ast.Name) and node.value.id in readers):
            return True
        return any(artifact(child, env) for child in ast.iter_child_nodes(node))
    def remember(node, env):
        for child in ast.walk(node):
            snapshots[id(child)] = dict(env)
    def bind(target, value, env):
        if isinstance(target, (ast.Name, ast.Attribute)):
            env[key(target)] = value
        elif isinstance(target, (ast.Tuple, ast.List)):
            for child in target.elts:
                bind(child, value, env)
    def joined(*branches):
        return {name: any(branch.get(name, False) for branch in branches)
                for name in set().union(*(set(branch) for branch in branches))}
    assigned_attributes = {}
    for assignment in ast.walk(tree):
        if isinstance(assignment, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            owner = assignment
            while owner in parents and not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                owner = parents[owner]
            targets = assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
            assigned_attributes.setdefault(owner, []).extend(
                child for target in targets for child in ast.walk(target)
                if isinstance(child, ast.Attribute))
    def block(statements, incoming):
        env = dict(incoming)
        for node in statements:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                local = dict(env)
                local.update(attributes.get(enclosing_class(node), {}))
                after = block(node.body, local)
                for target in assigned_attributes.get(node, ()):
                    cls = enclosing_class(target)
                    if cls:
                        values = collected.setdefault(cls, {})
                        values[key(target)] = values.get(key(target), False) or after.get(key(target), False)
            elif isinstance(node, ast.ClassDef):
                block(node.body, env)
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                if node.value is not None:
                    remember(node.value, env)
                    value = artifact(node.value, env)
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for target in targets:
                        bind(target, value or (isinstance(node, ast.AugAssign) and artifact(target, env)), env)
            elif isinstance(node, ast.If):
                remember(node.test, env)
                env = joined(block(node.body, env), block(node.orelse, env))
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    remember(item.context_expr, env)
                    if item.optional_vars is not None:
                        bind(item.optional_vars, artifact(item.context_expr, env), env)
                env = block(node.body, env)
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
                expression = node.test if isinstance(node, ast.While) else node.iter
                remember(expression, env)
                body = dict(env)
                if not isinstance(node, ast.While):
                    bind(node.target, artifact(node.iter, env), body)
                while True:
                    after = joined(env, block(node.body, body))
                    expanded = joined(body, after)
                    if expanded == body:
                        break
                    body = expanded
                env = block(node.orelse, after)
            elif isinstance(node, ast.Try):
                body = block(node.body, env)
                handlers = [block(handler.body, joined(env, body)) for handler in node.handlers]
                env = block(node.finalbody, joined(block(node.orelse, body), *handlers))
            else:
                remember(node, env)
        return env
    # Fixture attributes are shared across setUp and test methods. Iterate to
    # a finite attribute fixed point; locals remain independent per method.
    while True:
        previous = {name: dict(values) for name, values in attributes.items()}
        snapshots.clear()
        collected = {}
        block(tree.body, {})
        attributes = collected
        if attributes == previous:
            break
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(attribute(n) or isinstance(n, ast.Name) and n.id == 'd5_active'
                   for target in targets for n in ast.walk(target)):
                problems.append('activation assignment')
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, 'id', '')
            # Inspect the patch target and attribute, not replacement values
            # or diagnostics nested under side_effect/new/return_value.
            count = 1 if name == 'patch' else 2
            arguments = list(node.args[:count]) + [k.value for k in node.keywords
                        if k.arg in ('target', 'attribute', 'name')]
            values = [n.value for arg in arguments for n in ast.walk(arg)
                      if isinstance(n, ast.Constant) and isinstance(n.value, str)]
            if name in ('object', 'patch', 'setattr', 'setitem', 'patches') and (
                    any(v.split('.')[-1] == 'd5_active' or 'd5_activation' in v.split('.') for v in values) or
                    any(isinstance(n, ast.Name) and n.id in activation_modules
                        for a in arguments for n in ast.walk(a))):
                problems.append('activation patch')
            target, writes = None, False
            if name in ('write_text', 'write_bytes', 'write', 'writelines') and isinstance(node.func, ast.Attribute):
                target, writes = node.func.value, True
            elif name == 'open':
                module_open = isinstance(node.func, ast.Name) or (
                    isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in ('os', 'io', 'builtins'))
                target = node.args[0] if module_open and node.args else getattr(node.func, 'value', None)
                offset = 1 if module_open else 0
                mode = node.args[offset] if len(node.args) > offset else None
                mode = next((k.value for k in node.keywords if k.arg in ('mode', 'flags')), mode)
                if mode is not None:
                    writes = (isinstance(mode, ast.Constant) and isinstance(mode.value, str)
                              and any(c in mode.value for c in 'wax+')) or (
                        isinstance(mode, ast.Constant) and type(mode.value) is int and bool(mode.value &
                        (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))) or any(
                        isinstance(n, ast.Attribute) and n.attr in ('O_WRONLY', 'O_RDWR', 'O_CREAT', 'O_TRUNC', 'O_APPEND')
                        for n in ast.walk(mode))
            if writes and target is not None and artifact(target):
                if (filename, function(node)) not in CORRUPTION:
                    problems.append('hand-written installation outside corruption test: ' + str(function(node)))
        # Embedded child Python and startup-hook payloads are also fixture code.
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and '\n' in node.value:
            if any(token in node.value for token in ('d5_active', 'd5_activation', 'd5_readiness')):
                problems.extend(fixture_problems(node.value, filename))
    return sorted(set(problems))


def command_sites(source):
    """Find argv list literals containing the actual entry path, including stages."""
    sites = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.List):
            strings = [n.value for n in ast.walk(node) if isinstance(n, ast.Constant)
                       and isinstance(n.value, str)]
            targets = set(strings) & {'unit.py', 'remote_verify.py'}
            if targets:
                sites.append((node.lineno, targets, '--state-dir' in strings))
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and '\n' in node.value:
            if 'remote_verify.py' in node.value:
                try:
                    sites.extend(command_sites(node.value.replace("%r", "()")))
                except SyntaxError:
                    pass  # Shell wrapper; its Python argv expression is scanned above.
    return sites


class TestActivationGuards(unittest.TestCase):
    def test_ready_fixture_imports_and_publishes_in_a_fresh_interpreter(self):
        source = """from tests.fixtures.d5.ready import ready_installation
import tempfile
import d5_installation
with tempfile.TemporaryDirectory() as tmp:
    installed = ready_installation(tmp)
    assert d5_installation.state(installed['state_dir']) == d5_installation.READY
"""
        result = subprocess.run([sys.executable, '-B', '-c', source], cwd=str(ROOT),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_no_activation_pins_bootstraps_or_handwritten_installations(self):
        for name in FORBIDDEN:
            self.assertFalse((ROOT / 'tests' / (name + '.py')).exists())
        for path in sorted((ROOT / 'tests').rglob('*')):
            if not path.is_file() or '__pycache__' in path.parts:
                continue
            try:
                source = path.read_text()
            except UnicodeError:
                continue
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertEqual(fixture_problems(source, path.name), [])

    def test_checker_detects_activation_assignment_and_startup_hook_pin(self):
        target = 'd5_' + 'active'
        for source in ('registry.' + target + ' = lambda: False',
                       'mock.patch.object(registry, ' + repr(target) + ', return_value=False)',
                       "Path('sitecustomize.py').write_text(" + repr('import d5_activation\nd5_activation.' + target + ' = lambda: False\n') + ')'):
            with self.subTest(source=source):
                self.assertTrue(fixture_problems(source, 'synthetic.py'))
        self.assertEqual(fixture_problems("Path('sitecustomize.py').write_text('import ssl\\n')", 'synthetic.py'), [])

    def test_checker_detects_handwritten_marker(self):
        self.assertTrue(fixture_problems("p = root / 'd5-readiness.json'\np.write_text('{}')", 'synthetic.py'))

    def test_checker_tracks_installation_paths_across_fixture_methods(self):
        source = "class Fixture:\n def setUp(self):\n  self.marker = root / 'd5-readiness.json'\n def test_bad(self):\n  self.marker.write_text('{}')\n"
        self.assertTrue(fixture_problems(source, 'synthetic.py'))

    def test_checker_catches_builtin_and_descriptor_writers(self):
        prefix = "p = root / 'd5-readiness.json'\n"
        for call in ("open(p, 'w')", "open(p, mode='wb')", "p.open(mode='w')",
                     "os.open(p, os.O_WRONLY | os.O_CREAT)", "os.open(p, 1)", "h = open(p, 'r+')\nh.write('{}')"):
            with self.subTest(call=call):
                self.assertTrue(fixture_problems(prefix + call, 'synthetic.py'))
        for call in ("open(p)", "open(p, mode='rb')", "p.open()", "os.open(p, os.O_RDONLY)", "os.open(p, 0)"):
            with self.subTest(read=call):
                self.assertEqual(fixture_problems(prefix + call, 'synthetic.py'), [])

    def test_checker_catches_keyword_activation_patch(self):
        source = "mock.patch.object(registry, attribute='d5_" + "active', new=lambda: False)"
        self.assertTrue(fixture_problems(source, 'synthetic.py'))

    def test_checker_distinguishes_patch_targets_from_diagnostics(self):
        target = 'd5_' + 'active'
        diagnostic = repr(target + ' must refuse before Git')
        for source in (
                "mock.patch.object(registry, '_git', side_effect=AssertionError(" + diagnostic + '))',
                "mock.patch('registry._git', side_effect=AssertionError(" + diagnostic + '))',
                "monkeypatch.setattr(registry, '_git', lambda: " + diagnostic + ')'):
            with self.subTest(source=source):
                self.assertEqual(fixture_problems(source, 'synthetic.py'), [])
        for source in (
                'mock.patch.object(registry, ' + repr(target) + ', return_value=False)',
                'mock.patch(' + repr('registry.' + target) + ', return_value=False)',
                'monkeypatch.setattr(registry, ' + repr(target) + ', lambda: False)',
                'mock.patch.object(d5_activation, "anything", return_value=False)'):
            with self.subTest(source=source):
                self.assertIn('activation patch', fixture_problems(source, 'synthetic.py'))

    def test_checker_forgets_reassigned_paths_without_forgetting_aliases(self):
        prefix = "path = root / 'd5-readiness.json'\npath.read_text()\n"
        for source in (
                prefix + "path = root / 'unrelated.json'\npath.write_text('{}')\n",
                prefix + "path = root / 'unrelated.json'\ncopy = path\ncopy.write_text('{}')\n",
                "class Rebound:\n def setUp(self):\n  self.path = root / 'd5-readiness.json'\n"
                "  self.path.read_text()\n  self.path = root / 'unrelated.json'\n"
                " def test_safe(self):\n  self.path.write_text('{}')\n",
                "class Fixture:\n def setUp(self):\n  self.path = root / 'd5-readiness.json'\n"
                " def test_safe(self):\n  self.path.read_text()\n  self.path = root / 'unrelated.json'\n  self.path.write_text('{}')\n"):
            with self.subTest(safe=source):
                self.assertEqual(fixture_problems(source, 'synthetic.py'), [])
        for source in (
                prefix + "path.write_text('{}')\npath = root / 'unrelated.json'\n",
                prefix + "copy = path\npath = root / 'unrelated.json'\ncopy.write_text('{}')\n",
                prefix + "if flag:\n path = root / 'unrelated.json'\npath.write_text('{}')\n",
                "path = root / 'unrelated.json'\nif flag:\n path = root / 'd5-readiness.json'\npath.write_text('{}')\n"):
            with self.subTest(unsafe=source):
                self.assertTrue(fixture_problems(source, 'synthetic.py'))

    def test_checker_accepts_real_publisher_read_then_unrelated_write(self):
        source = '''import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from tests.fixtures.d5.ready import ready_installation
class TestReaderWriter(unittest.TestCase):
 def test_read_then_write(self):
  with TemporaryDirectory() as tmp:
   entry = ready_installation(tmp)
   path = Path(entry['state_dir']) / 'd5-readiness.json'
   self.assertTrue(path.read_text())
   path = Path(tmp) / 'unrelated-output.json'
   path.write_text('{}')
   self.assertEqual(path.read_text(), '{}')
'''
        namespace = {'__name__': 'test_reader_writer'}
        exec(source, namespace)
        result = unittest.TestResult()
        namespace['TestReaderWriter']('test_read_then_write').run(result)
        self.assertTrue(result.wasSuccessful(), (result.errors, result.failures))
        self.assertEqual(fixture_problems(source, 'test_reader_writer.py'), [])

    def test_coordinator_passes_state_dir(self):
        inventory = {}
        for skill in ('hanig-swarm', 'hanig-orchestrate'):
            for path in sorted((ROOT / 'skills' / skill / 'scripts').glob('*.py')):
                sites = command_sites(path.read_text())
                if sites:
                    inventory[path.name] = len(sites)
                for line, targets, supplied in sites:
                    with self.subTest(file=path.name, line=line, targets=targets):
                        self.assertTrue(supplied, 'coordinator entry omits --state-dir')
        self.assertEqual(inventory, {'swarm_dispatch.py': 2, 'swarm_evidence.py': 1, 'remote_verify.py': 2})


if __name__ == '__main__':
    unittest.main()
