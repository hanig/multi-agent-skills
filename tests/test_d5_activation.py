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
from tests.fixtures.d5.ready import ready_installation, missing_installation


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
        with mock.patch.object(registry, 'lookup', side_effect=AssertionError('lookup before readiness')):
            with self.assertRaisesRegex(registry.AuthorityError, 'caller-held coordinator lease'):
                registry.register(self.root, 'p', state)
        (state / readiness.MARKER).unlink()
        with readiness.installer_lease(state), mock.patch.object(registry, '_git') as git:
            with self.assertRaisesRegex(registry.AuthorityError, 'd5_install'):
                registry.register(self.root, 'p', state)
            git.assert_not_called()

    def test_corruption_fixture_preserves_only_its_own_metadata(self):
        state = self.publish()
        before = state.stat().st_mtime_ns
        with missing_installation(self.entry):
            self.assertFalse(activation.d5_active(state))
        self.assertEqual(state.stat().st_mtime_ns, before)
        with missing_installation(self.entry):
            (state / 'consumer-write').write_bytes(b'visible')
            os.utime(state, ns=(1, 1))
        self.assertNotEqual(state.stat().st_mtime_ns, before)
        self.assertEqual((state / 'consumer-write').read_bytes(), b'visible')

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

    def test_implicit_lookup_matches_operated_state(self):
        """PD2 / implicit-lookup-differs: all supported default path sources."""
        import coordinator_paths as paths
        repo, other = self.root / 'repo', self.root / 'other'
        for directory in (repo, other):
            subprocess.run(['git', 'init', str(directory)], check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        subprocess.run(['git', '-C', str(other), '-c', 'user.name=Test', '-c',
                        'user.email=test@example.invalid', 'commit', '--allow-empty', '-m', 'base'],
                       check=True, capture_output=True, timeout=30)
        linked = self.root / 'linked'
        subprocess.run(['git', '-C', str(other), 'worktree', 'add', '-b', 'linked', str(linked)],
                       check=True, capture_output=True, timeout=30)
        nested = repo / 'nested'
        nested.mkdir()
        for cwd in (repo, nested, linked, self.empty):
            for xdg in ('', 'relative', str(self.root / 'xdg'), str(repo), str(other), str(linked)):
                for source in ('repo', 'execution_workspace', 'workspace_policy', 'extra_repos'):
                    unit = ({source: str(other / 'future')} if source != 'workspace_policy'
                            else {source: {'path': str(other / 'future')}})
                    plan = {'units': [unit]} if source != 'extra_repos' else None
                    extra = [other] if source == 'extra_repos' else ()
                    with self.subTest(cwd=cwd.name, xdg=xdg, source=source), \
                            mock.patch.dict(os.environ, {'XDG_STATE_HOME': xdg}), \
                            mock.patch.object(Path, 'home', return_value=self.root / 'home'):
                        operated = paths.resolve_paths(plan=plan, cwd=cwd, extra_repos=extra)[0]
                        with mock.patch.object(paths, '_git', side_effect=AssertionError('early Git')):
                            early = paths.pre_git_default_state(cwd, plan=plan, extra_repos=extra)
                        self.assertEqual(early, operated)
        # Both preferred bases inside operated checkouts force the temp fallback.
        with mock.patch.dict(os.environ, {'XDG_STATE_HOME': str(repo)}), \
                mock.patch.object(Path, 'home', return_value=other):
            plan = {'units': [{'repo': str(other)}]}
            self.assertEqual(paths.pre_git_default_state(repo, plan=plan),
                             paths.resolve_paths(plan=plan, cwd=repo)[0])

    def test_implicit_callers_classify_operated_plan_before_git(self):
        import argparse
        import swarm
        repo, other = self.root / 'repo', self.root / 'other'
        for directory in (repo, other):
            subprocess.run(['git', 'init', str(directory)], check=True,
                           capture_output=True, timeout=30)
        plan = {'name': 'p', 'units': [{'repo': str(other)}]}
        plan_path = self.root / 'plan.json'
        plan_path.write_text(json.dumps(plan))
        old = Path.cwd()
        os.chdir(repo)
        self.addCleanup(os.chdir, old)
        with mock.patch.dict(os.environ, {'XDG_STATE_HOME': str(other)}), \
                mock.patch.object(Path, 'home', return_value=self.root / 'home'):
            state = swarm.CP.resolve_paths(plan=plan)[0]
            ready_installation(self.root, state_dir=state)
            args = argparse.Namespace(plan=str(plan_path), state_dir=None, fn=swarm.cmd_run, command='run')
            for call in (lambda: swarm._d5_dispatch(args), lambda: swarm.cmd_run(args),
                         lambda: swarm._prepare_command_paths(args, plan=plan)):
                with self.subTest(call=call), mock.patch.object(subprocess, 'run',
                        side_effect=AssertionError('Git before D5 refusal')):
                    with self.assertRaisesRegex(SystemExit, 'D5 active:'):
                        call()

    def test_expanded_explicit_state_matches_operated_state(self):
        import swarm
        state = self.publish()
        with mock.patch.dict(os.environ, {'HOME': str(state.parent)}), \
                mock.patch.object(subprocess, 'run', side_effect=AssertionError('Git before refusal')):
            self.assertEqual(swarm._d5_state('~/state'), state)
            with self.assertRaisesRegex(SystemExit, 'D5 active:'):
                swarm._d5_refuse('explicit', '~/state')

    def test_no_tracked_readiness(self):
        names = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).split(b'\0')
        forbidden = {readiness.MARKER, readiness.INSTALLATION, readiness.SENTINEL}
        self.assertFalse([n for n in names if os.fsdecode(n).split('/')[-1] in forbidden])


if __name__ == '__main__':
    unittest.main()
