"""Exact D5 fixture syntax rules; indirect pins are review-enforced.

No taint, alias, path provenance or dataflow analysis. Artifact literals are
allowed only in the named corruption/fixture files below. Even read-only
literals and unrelated attributes matching the exact spelling are refused.
"""
import ast
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = ('d5_' + 'test_support', 'd5_' + 'child_bootstrap')
ACTIVE_NAME = 'd5_' + 'active'
ARTIFACTS = ('d5-' + 'readiness.json', 'd5-' + 'installed.json', 'installation' + '.json')
ARTIFACT_FILES = {
    'tests/test_d5_activation.py',
    'tests/test_d5_u45_retry_recovery.py',
    'tests/test_d5_u4_0b2_submit.py',
    'tests/test_d5_u4_0_readiness.py',
    'tests/test_d5_u4_0b1_issuance.py',
    'tests/fixtures/d5/ready.py',
    'tests/fixtures/d5/u49a_probe.py',
}


def fixture_problems(source, filename):
    """Check literal/import/store syntax only; indirect pins are review-enforced."""
    problems = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ['unparseable Python fixture']
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [alias.name for alias in node.names]
            if isinstance(node, ast.ImportFrom):
                names.append(node.module or '')
            if any(set(name.split('.')) & set(FORBIDDEN) for name in names):
                problems.append('obsolete activation helper import')
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if ACTIVE_NAME in node.value:
                problems.append('activation literal')
            if node.value in ARTIFACTS and filename not in ARTIFACT_FILES:
                problems.append('installation literal outside named files')
        if (isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store)
                and node.attr == ACTIVE_NAME):
            problems.append('activation attribute store')
    return sorted(set(problems))


def tree_problems(root):
    problems = []
    for name in FORBIDDEN:
        path = root / 'tests' / (name + '.py')
        if path.exists() or path.is_symlink():
            problems.append((str(path.relative_to(root)), 'obsolete helper file'))
    for path in sorted((root / 'tests').rglob('*.py')):
        relative = path.relative_to(root).as_posix()
        problems.extend((relative, reason) for reason in fixture_problems(path.read_text(), relative))
    return problems


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
        self.assertEqual(tree_problems(ROOT), [])

    def test_each_planted_rule_trips_the_repository_guard(self):
        fixtures = [(name + '.py', '') for name in FORBIDDEN]
        for name in FORBIDDEN:
            fixtures.extend(('test_probe.py', source) for source in (
                'import tests.' + name, 'from tests import ' + name,
                'from tests.' + name + ' import anything'))
        fixtures.extend(('test_probe.py', source) for source in (
            'message = ' + repr('prefix.' + ACTIVE_NAME + '.suffix'),
            'thing.' + ACTIVE_NAME + ' = False',
            'thing.' + ACTIVE_NAME + ': bool = False',
            'thing.' + ACTIVE_NAME + ' += 1',
            "Path('sitecustomize.py').write_text(" + repr('import d5_activation\nd5_activation.' + ACTIVE_NAME + ' = lambda: False\n') + ')'))
        fixtures.extend(('test_probe.py', 'path = ' + repr(name)) for name in ARTIFACTS)
        for filename, source in fixtures:
            with self.subTest(filename=filename, source=source), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / 'tests').mkdir()
                (root / 'tests' / filename).write_text(source)
                with mock.patch.object(sys.modules[__name__], 'ROOT', root):
                    with self.assertRaises(AssertionError):
                        self.test_no_activation_pins_bootstraps_or_handwritten_installations()

    def test_artifact_allowlist_is_exact_and_does_not_allow_pins(self):
        for filename in ARTIFACT_FILES:
            for name in ARTIFACTS:
                self.assertEqual(fixture_problems('path = ' + repr(name), filename), [])
                self.assertTrue(fixture_problems('path = ' + repr(name), 'other/' + filename))
            self.assertTrue(fixture_problems('x = ' + repr(ACTIVE_NAME), filename))
            self.assertTrue(fixture_problems('x.' + ACTIVE_NAME + ' = 1', filename))

    def test_checker_accepts_renamed_observations_and_read_only_queries(self):
        for source in (
            'observation.installation_active = registry.' + ACTIVE_NAME + '(state)',
            'results[registry.' + ACTIVE_NAME + '(state)] = 1',
            'results[registry.' + ACTIVE_NAME + '(state)]: int = 1',
            'results[registry.' + ACTIVE_NAME + '(state)] += 1',
            "path = root / readiness.MARKER\npath.parent.joinpath('output').write_text('ok')",
            "Path('sitecustomize.py').write_text('import ssl\\n')"):
            self.assertEqual(fixture_problems(source, 'tests/test_probe.py'), [])

    def test_matching_unrelated_attributes_and_read_only_literals_still_refuse(self):
        self.assertTrue(fixture_problems('observation.' + ACTIVE_NAME + ' = True', 'tests/test_probe.py'))
        for name in ARTIFACTS:
            self.assertTrue(fixture_problems('Path(' + repr(name) + ').read_text()', 'tests/test_probe.py'))

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
