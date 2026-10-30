"""D5 fixture construction and coordinator command inventories (OD6/OD7)."""
import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = ('d5_' + 'test_support', 'd5_' + 'child_bootstrap')
ARTIFACTS = ('d5-readiness.json', 'd5-installed.json', 'installation.json')
# Only corruption tests may overwrite a real publisher's artifacts.
CORRUPTION = {
    ('test_d5_activation.py', 'test_broken_is_active_and_refuses'),
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
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            activation_modules.update(alias.asname or alias.name for alias in node.names
                                      if alias.name == 'd5_activation')
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
    # Track installation path expressions and their aliases through assignments.
    tainted = set()
    def key(node):
        return ast.dump(node, include_attributes=False)
    def artifact(node):
        return any((isinstance(n, ast.Constant) and isinstance(n.value, str) and
                    any(name in n.value for name in ARTIFACTS)) or
                   (isinstance(n, ast.Attribute) and n.attr in ('MARKER', 'INSTALLATION', 'SENTINEL')) or
                   key(n) in tainted for n in ast.walk(node))
    for _ in range(5):
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and artifact(node.value):
                for target in node.targets:
                    # Store contexts differ from later Load contexts.
                    if isinstance(target, ast.Name):
                        tainted.add(key(ast.Name(id=target.id, ctx=ast.Load())))
                    elif isinstance(target, ast.Attribute):
                        tainted.add(key(ast.Attribute(value=target.value, attr=target.attr, ctx=ast.Load())))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(attribute(n) or isinstance(n, ast.Name) and n.id == 'd5_active'
                   for target in targets for n in ast.walk(target)):
                problems.append('activation assignment')
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, 'id', '')
            values = [n.value for arg in node.args for n in ast.walk(arg)
                      if isinstance(n, ast.Constant) and isinstance(n.value, str)]
            if name in ('object', 'patch', 'setattr', 'setitem', 'patches') and (
                    any('d5_active' in v or 'd5_activation.' in v for v in values) or
                    any(isinstance(a, ast.Name) and a.id in activation_modules for a in node.args)):
                problems.append('activation patch')
            if name in ('write_text', 'write_bytes', 'open') and isinstance(node.func, ast.Attribute) and artifact(node.func.value):
                writes = name != 'open' or any(any(c in v for c in 'wax+') for v in values)
                if writes and (filename, function(node)) not in CORRUPTION:
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
                    sites.extend(command_sites(node.value))
                except SyntaxError:
                    pass  # Shell wrapper; its Python argv expression is scanned above.
    return sites


class TestActivationGuards(unittest.TestCase):
    def test_no_activation_pins_bootstraps_or_handwritten_installations(self):
        for name in FORBIDDEN:
            self.assertFalse((ROOT / 'tests' / (name + '.py')).exists())
        for path in sorted((ROOT / 'tests').rglob('*.py')):
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertEqual(fixture_problems(path.read_text(), path.name), [])

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
