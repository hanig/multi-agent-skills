"""Installation-derived activation, broken-state corruption and real CLI guards."""
import ast
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'skills/hanig-swarm/scripts'
sys.path.insert(0, str(ROOT / 'skills/hanig-orchestrate/scripts'))
import d5_registry as registry
import d5_activation as activation
import d5_readiness as readiness
from tests.fixtures.d5.ready import ready_installation


class TestD5Activation(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.empty = self.root / 'empty'
        self.empty.mkdir()

    def publish(self):
        self.entry = ready_installation(self.root / 'installed')
        return Path(self.entry['state_dir'])

    def test_activation_follows_installation(self):
        self.assertFalse(activation.d5_active(self.empty))
        state = self.publish()
        self.assertTrue(activation.d5_active(state))
        self.assertEqual(readiness.state(state), readiness.READY)
        self.assertEqual(readiness.state(self.empty), readiness.NOT_INSTALLED)

    def test_readiness_absent_is_inactive_and_legacy_proceeds(self):
        p = subprocess.run([sys.executable, '-B', str(SCRIPTS / 'unit.py'),
             'allocate', '--state-dir', str(self.empty), '--root', str(self.root / 'runs'),
             '--task', 'legacy', '--kind', 'pipeline', '--output', 'out'],
             capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(Path(p.stdout.strip()).is_dir())

    def test_constant_activation_and_state_dir_isolation(self):
        state = self.publish()
        for directory, expected in [(self.empty, False), (state, True)] * 3:
            self.assertIs(activation.d5_active(directory), expected)
            self.assertIs(registry.d5_active(directory), expected)

    def test_signature_requires_state_dir(self):
        for fn in (activation.d5_active, registry.d5_active):
            with self.assertRaises(TypeError):
                fn()
            with self.assertRaises(ValueError):
                fn(None)

    def test_no_cache(self):
        state = self.root / 'installed' / 'state'
        self.assertFalse(activation.d5_active(state))
        self.publish()
        self.assertEqual(readiness.state(state), readiness.READY)
        with readiness.installer_lease(state):
            readiness.withdraw(state)
        self.assertTrue(activation.d5_active(state))
        self.assertEqual(readiness.state(state), readiness.BROKEN)
        ready_installation(self.root / 'installed', self.entry)
        self.assertEqual(readiness.state(state), readiness.READY)

    def test_broken_is_active_and_refuses(self):
        for corruption in ('deleted', 'truncated', 'schema', 'binding', 'symlink', 'mid-publish'):
            with self.subTest(corruption=corruption):
                state = self.publish()
                marker = state / readiness.MARKER
                if corruption in ('deleted', 'mid-publish'):
                    marker.unlink()
                elif corruption == 'truncated':
                    marker.write_text('{')
                elif corruption == 'symlink':
                    marker.unlink()
                    marker.symlink_to(Path(self.entry['record_dir']) / readiness.INSTALLATION)
                else:
                    value = json.loads(marker.read_text())
                    value['schema_version' if corruption == 'schema' else 'generation'] = 2
                    marker.write_text(json.dumps(value))
                self.assertTrue(activation.d5_active(state))
                self.assertEqual(readiness.state(state), readiness.BROKEN)
                with readiness.installer_lease(state), mock.patch.object(registry, '_git') as git:
                    with self.assertRaisesRegex(registry.AuthorityError, 'd5_install'):
                        registry.register(self.root, 'p', state)
                    git.assert_not_called()
                # Real publication repairs every corruption, including symlinks.
                ready_installation(self.root / 'installed', self.entry)

    def test_registry_requires_readiness(self):
        state = self.publish()
        (state / readiness.MARKER).unlink()
        with readiness.installer_lease(state), mock.patch.object(registry, '_git') as git:
            with self.assertRaisesRegex(registry.AuthorityError, 'd5_install'):
                registry.register(self.root, 'p', state)
            git.assert_not_called()

    def test_publish_interruption_order(self):
        state = self.publish()
        real_publish = readiness.publish
        calls = []
        def interrupted(path, value):
            real_publish(path, value)
            calls.append(path.name)
            self.assertEqual(readiness.state(state), readiness.BROKEN)
            raise InterruptedError('synthetic interruption after first publication')
        with readiness.installer_lease(state), mock.patch.object(readiness, 'publish', interrupted):
            with self.assertRaises(InterruptedError):
                readiness.publish_installation(self.entry)
        self.assertEqual(calls, [readiness.SENTINEL])
        self.assertTrue(activation.d5_active(state))

    def test_single_implementation(self):
        source = Path(activation.__file__).read_text()
        tree = ast.parse(source)
        self.assertEqual(len(tree.body), 1)
        self.assertIsInstance(tree.body[0], ast.FunctionDef)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, ('environ', 'getenv', 'argv', 'getcwd'))
        tree = ast.parse(Path(registry.__file__).read_text())
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'd5_active')
        calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
        self.assertEqual(len(calls), 1)
        self.assertEqual(ast.dump(calls[0]), ast.dump(ast.parse('d5_activation.d5_active(state_dir)').body[0].value))

    def test_fresh_process_agreement(self):
        state = self.publish()
        code = ('from __future__ import annotations\n'
                'import sys\nsys.path.insert(0, ' + repr(str(SCRIPTS)) + ')\n'
                'import d5_activation\n'
                'print(d5_activation.d5_active(sys.argv[1]))\n')
        script = self.root / 'probe.py'
        script.write_text(code)
        linked = self.root / 'linked.py'
        linked.symlink_to(script)
        executable = self.root / 'renamed-runner'
        executable.symlink_to(sys.executable)
        entries = [[sys.executable, '-B', '-m', 'probe'],
                   [sys.executable, '-B', '-c', code],
                   [sys.executable, '-B', str(linked)],
                   [str(executable), '-B', str(script)]]
        nested = ('import subprocess,sys\nsubprocess.run(' + repr(entries[1]) +
                  '+[sys.argv[1]], check=True)\n')
        entries.append([sys.executable, '-B', '-c', nested])
        for directory in (state, self.empty):
            for command in entries:
                with self.subTest(directory=directory, command=command[:3]):
                    p = subprocess.run(command + [str(directory)], cwd=self.root,
                         capture_output=True, text=True, timeout=30)
                    self.assertEqual(p.returncode, 0, p.stderr)
                    self.assertEqual(p.stdout.strip(), str(activation.d5_active(directory)))

    def test_no_tracked_readiness(self):
        names = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).split(b'\0')
        forbidden = {readiness.MARKER, readiness.INSTALLATION, readiness.SENTINEL}
        self.assertFalse([n for n in names if os.fsdecode(n).split('/')[-1] in forbidden])


if __name__ == '__main__':
    unittest.main()
