"""Installation-derived activation, broken-state corruption and real CLI guards."""
import ast
from contextlib import ExitStack, redirect_stdout
import hashlib
import io
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
from tests.fixtures.d5.ready import ready_installation, missing_installation, classification_only


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

    def test_classifier_never_uses_ambient_cwd(self):
        with mock.patch.object(os, 'getcwd', side_effect=AssertionError('ambient cwd')):
            self.assertFalse(activation.d5_active(self.empty))
            with self.assertRaisesRegex(ValueError, 'absolute state_dir'):
                activation.d5_active('relative-state')

    def test_default_lookup_and_schema_preserve_posix_path_bytes(self):
        import swarm
        paths = swarm.CP
        outputs, locators = [], []
        # This host refuses non-UTF8 directory creation. Model the filesystem
        # discovery result, retaining the real hashing and schema entrypoint.
        for raw in (b'/project-\xff', b'/project-\xfe', b'/project-ascii'):
            context = Path(os.fsdecode(raw))
            with self.subTest(raw=raw), \
                    mock.patch.object(paths.os, 'getcwd', return_value=str(context)), \
                    mock.patch.object(paths, '_resolved', side_effect=Path), \
                    mock.patch.object(paths, '_git', return_value=None), \
                    mock.patch.object(paths, '_external_base', return_value=self.root), \
                    mock.patch.object(sys, 'argv', ['swarm.py', 'schema', '--json']):
                early = swarm._d5_state(None)
                normal = paths.default_paths()[0]
                self.assertEqual(early, normal)
                self.assertTrue(early.parent.name.endswith(hashlib.sha256(raw).hexdigest()[:12]))
                locators.append(early)
                output = io.StringIO()
                with redirect_stdout(output), self.assertRaises(SystemExit) as stopped:
                    swarm.main()
                self.assertEqual(stopped.exception.code, 0)
                self.assertTrue(json.loads(output.getvalue())['fields'])
                outputs.append(output.getvalue())
        self.assertEqual(len(set(locators)), 3)
        self.assertEqual(len(set(outputs)), 1)

    def test_inactive_guard_rejects_cached_child_thread_io(self):
        from threading import Thread
        target = self.root / 'unrelated-thread-output'
        target.write_text('before')
        cached_open = open
        errors = []

        def writer():
            try:
                with cached_open(target, 'w') as stream:
                    stream.write('after')
            except AssertionError as exc:
                errors.append(exc)

        worker = Thread(target=writer)
        try:
            with classification_only(self.empty):
                self.assertFalse(activation.d5_active(self.empty))
                worker.start()
                worker.join(10)
        except AssertionError as exc:
            errors.append(exc)
        finally:
            if worker.ident is not None:
                worker.join(10)
                self.assertFalse(worker.is_alive(), 'child IO watchdog')
        self.assertEqual(target.read_text(), 'before')
        self.assertTrue(errors, 'the guard must reject child-thread filesystem IO')

    def test_inactive_guard_observes_only_its_calling_thread(self):
        import threading
        enter, done = threading.Event(), threading.Event()
        target = self.root / 'existing-thread-output'
        errors = []
        def unrelated():
            try:
                if not enter.wait(10):
                    raise AssertionError('guard entry watchdog')
                target.write_text('unrelated')
                target.stat()
            except BaseException as exc:
                errors.append(exc)
            finally:
                done.set()
        worker = threading.Thread(target=unrelated)
        worker.start()
        try:
            with classification_only(self.empty):
                self.assertFalse(activation.d5_active(self.empty))
                enter.set()
                self.assertTrue(done.wait(10), 'other thread watchdog')
        finally:
            enter.set()
            worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(target.read_text(), 'unrelated')

    def test_inactive_guard_rejects_low_level_thread_start(self):
        import _thread
        import threading
        target = self.root / 'low-level-thread-output'
        target.write_text('before')
        completed = threading.Event()
        cached_open = open
        def writer():
            try:
                with cached_open(target, 'w') as stream:
                    stream.write('after')
            finally:
                completed.set()
        launched = False
        try:
            with self.assertRaisesRegex(AssertionError, 'inactive thread start'):
                with classification_only(self.empty):
                    _thread.start_new_thread(writer, ())
                    launched = True
        finally:
            if launched:
                self.assertTrue(completed.wait(10), 'thread watchdog')
        self.assertEqual(target.read_text(), 'before')

    def test_inactive_guard_rejects_filesystem_operation_siblings(self):
        source = self.root / 'source'
        source.write_bytes(b'preserved')
        fd = os.open(str(source), os.O_RDWR)
        self.addCleanup(os.close, fd)
        operations = [
            ('listdir', lambda: os.listdir(self.root)),
            ('scandir', lambda: list(os.scandir(self.root))),
            ('access', lambda: os.access(source, os.R_OK)),
            ('getcwd', lambda: os.getcwd()),
            ('symlink', lambda: os.symlink('target', self.root / 'link')),
            ('link', lambda: os.link(source, self.root / 'hardlink')),
            ('truncate', lambda: os.truncate(source, 0)),
            ('ftruncate', lambda: os.ftruncate(fd, 0)),
            ('read', lambda: os.read(fd, 1)),
            ('write', lambda: os.write(fd, b'changed')),
            ('fstat', lambda: os.fstat(fd)),
            ('fchmod', lambda: os.fchmod(fd, 0o600)),
            ('path-stat', lambda: source.stat()),
            ('path-lstat', lambda: source.lstat()),
            ('path-iterdir', lambda: list(self.root.iterdir())),
            ('path-glob', lambda: list(self.root.glob('*'))),
            ('path-rglob', lambda: list(self.root.rglob('*'))),
            ('path-symlink', lambda: (self.root / 'path-link').symlink_to(source)),
            ('path-chmod', lambda: source.chmod(0o600)),
            ('path-read', lambda: source.read_bytes()),
        ]
        for name, operation in operations:
            with self.subTest(operation=name), classification_only(self.empty):
                self.assertFalse(activation.d5_active(self.empty))
                with self.assertRaises(AssertionError):
                    operation()
        self.assertEqual(source.read_bytes(), b'preserved')

    def test_git_metadata_preserves_filesystem_path_bytes(self):
        import swarm
        paths = swarm.CP
        self.assertFalse(hasattr(paths, '_pre_git_layout'))
        self.assertFalse(hasattr(paths, 'pre_git_default_state'))
        for witness in ('gitfile', 'commondir', 'backlink'):
            with self.subTest(witness=witness), tempfile.TemporaryDirectory(dir=self.root) as tmp:
                root = Path(tmp).resolve()
                main, linked = root / 'main', root / 'linked'
                common = main / '.git'
                admin = common / 'worktrees' / 'linked'
                admin.mkdir(parents=True)
                linked.mkdir()
                (linked / '.git').write_bytes(b'gitdir: ' + os.fsencode(admin) + b'\n')
                (admin / 'commondir').write_bytes(b'../..\n')
                (admin / 'gitdir').write_bytes(os.fsencode(linked / '.git') + b'\n')
                raw = os.fsencode(root) + b'/metadata-\xff'
                if witness == 'gitfile':
                    (linked / '.git').write_bytes(b'gitdir: ' + raw + b'\n')
                elif witness == 'commondir':
                    (admin / 'commondir').write_bytes(raw + b'\n')
                else:
                    (admin / 'gitdir').write_bytes(raw + b'/.git\n')
                # Model POSIX path existence on hosts that reject byte 0xff.
                # Metadata reads and decoding, lookup and the CLI remain real.
                original_dir, original_file = Path.is_dir, Path.is_file
                def exists(method):
                    def check(path):
                        if '\udcff' in str(path):
                            return False
                        return method(path)
                    return check
                previous = Path.cwd()
                try:
                    os.chdir(linked)
                    with mock.patch.object(Path, 'is_dir', exists(original_dir)), \
                            mock.patch.object(Path, 'is_file', exists(original_file)), \
                            mock.patch.dict(os.environ, {'XDG_STATE_HOME': str(root / 'xdg')}), \
                            mock.patch.object(sys, 'argv', ['swarm.py', 'schema', '--json']):
                        self.assertEqual(swarm._d5_state(None), paths.resolve_paths()[0])
                        output = io.StringIO()
                        with redirect_stdout(output), self.assertRaises(SystemExit) as stopped:
                            swarm.main()
                        self.assertEqual(stopped.exception.code, 0)
                        self.assertTrue(json.loads(output.getvalue())['fields'])
                finally:
                    os.chdir(previous)

    def test_linked_worktree_fixtures_accept_configured_default_branch(self):
        config = self.root / 'git-config'
        config.write_text('[init]\n\tdefaultBranch = linked\n')
        names = [
            'tests.test_d5_activation.TestD5Activation.test_relative_git_backlinks_match_operated_state_before_effects',
            'tests.test_d5_activation.TestD5Activation.test_implicit_lookup_matches_operated_state',
            'tests.test_d5_routing.TestD5Routing.test_claim_from_linked_checkout_uses_registered_checkout',
            'tests.test_d5_admission.TestAdmission.test_registered_source_from_linked_caller',
            'tests.test_d5_control.TestD5Control.test_registered_checkout_is_source',
        ]
        result = subprocess.run([sys.executable, '-B', '-m', 'unittest', '-v'] + names,
            cwd=Path(__file__).resolve().parents[1],
            env=dict(os.environ, GIT_CONFIG_GLOBAL=str(config), GIT_CONFIG_NOSYSTEM='1'),
            capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_relative_git_backlinks_match_operated_state_before_effects(self):
        import itertools
        import swarm
        root = self.root / 'relative-layout'
        main, linked, home = root / 'main', root / 'linked', root / 'home'
        home.mkdir(parents=True)
        def git(*args):
            return subprocess.run(['git'] + list(map(str, args)), check=True,
                                  capture_output=True, timeout=30)
        git('init', main)
        git('-C', main, 'symbolic-ref', 'HEAD', 'refs/heads/fixture-base')
        git('-C', main, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
            'commit', '--allow-empty', '-m', 'fixture')
        git('-C', main, 'worktree', 'add', '-b', 'linked', linked)
        admin = main / '.git' / 'worktrees' / 'linked'
        pointers = ((linked / '.git', admin, 'gitdir: '),
                    (admin / 'commondir', main / '.git', ''),
                    (admin / 'gitdir', linked / '.git', ''))
        previous = Path.cwd()
        try:
            with mock.patch.object(Path, 'home', return_value=home), \
                    mock.patch.dict(os.environ, {'XDG_STATE_HOME': str(linked / 'xdg')}):
                for modes in itertools.product((False, True), repeat=3):
                    for (pointer, target, prefix), relative in zip(pointers, modes):
                        value = os.path.relpath(target, pointer.parent) if relative else str(target)
                        pointer.write_text(prefix + value + '\n')
                    for cwd in (main, linked):
                        with self.subTest(relative=modes, cwd=cwd.name):
                            os.chdir(cwd)
                            normal = swarm.CP.resolve_paths()[0]
                            self.assertEqual(swarm._d5_state(None), normal)
                            ready_installation(root, state_dir=normal)
                            with self.assertRaisesRegex(SystemExit, 'D5 active:'):
                                swarm._d5_refuse('run', None, plan={})
        finally:
            os.chdir(previous)

    def test_inactive_guard_rejects_cached_filesystem_calls(self):
        import builtins
        from io import FileIO
        cached_open, cached_io_open, cached_os_open = builtins.open, io.open, os.open
        cached_stat, cached_lstat, cached_listdir = os.stat, os.lstat, os.listdir
        cached_write = os.write
        source = self.root / 'cached-source'
        source.write_bytes(b'preserved')
        previous_profile = sys.getprofile()
        with source.open('r+b') as handle:
            fd = handle.fileno()
            operations = (
                ('FileIO', lambda: FileIO(source, 'wb')),
                ('builtin-open', lambda: cached_open(source, 'wb')),
                ('io-open', lambda: cached_io_open(source, 'wb')),
                ('os-open', lambda: cached_os_open(source, os.O_WRONLY | os.O_TRUNC)),
                ('stat', lambda: cached_stat(source)),
                ('lstat', lambda: cached_lstat(source)),
                ('listdir', lambda: cached_listdir(self.root)),
                ('descriptor-write', lambda: cached_write(fd, b'changed')),
                ('handle-write', lambda: handle.write(b'changed')),
            )
            for name, operation in operations:
                with self.subTest(operation=name), classification_only(self.empty):
                    self.assertFalse(activation.d5_active(self.empty))
                    with self.assertRaises(AssertionError):
                        value = operation()
                        if hasattr(value, 'close'):
                            value.close()
                self.assertIs(sys.getprofile(), previous_profile)
        self.assertEqual(source.read_bytes(), b'preserved')
        source.write_bytes(b'guard finished')
        self.assertEqual(source.read_bytes(), b'guard finished')
        state = self.publish()
        with readiness.installer_lease(state):
            readiness.withdraw(state)
        with classification_only(state):
            self.assertTrue(activation.d5_active(state))

    def test_inactive_guard_allows_memory_io_and_descriptor_queries(self):
        source = self.root / 'metadata-query'
        source.write_bytes(b'preserved')
        with source.open('r+b') as handle:
            descriptor = handle.fileno()
            with classification_only(self.empty):
                text, binary = io.StringIO(), io.BytesIO()
                text.write('memory')
                binary.write(b'memory')
                self.assertEqual(text.getvalue(), 'memory')
                self.assertEqual(binary.getvalue(), b'memory')
                self.assertEqual(handle.fileno(), descriptor)
                self.assertTrue(handle.readable())
                self.assertTrue(handle.writable())
                with self.assertRaises(AssertionError):
                    handle.write(b'changed')
        self.assertEqual(source.read_bytes(), b'preserved')

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
        import swarm_outbox as outbox
        import swarm_evidence as evidence
        for corruption in ('deleted', 'truncated', 'schema', 'binding', 'symlink', 'mid-publish', 'sentinel-removed'):
            with self.subTest(corruption=corruption):
                state = self.publish()
                marker = state / readiness.MARKER
                if corruption in ('deleted', 'mid-publish'):
                    marker.unlink()
                elif corruption == 'sentinel-removed':
                    (state / readiness.SENTINEL).unlink()
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
                # Final-round counterexample: every broken refusal names repair.
                calls = [
                    (outbox._write_sequence, ({},)),
                    (outbox._allocate_sequence, ([],)),
                    (outbox.set_sequence_floor, (1, 'owner', 'probe')),
                    (outbox.emit_intent, ('p', 'u', 'DONE', {})),
                    (outbox.backfill_tracker_intents, ('p', {})),
                    (outbox._append_receipt_atomic, ({},)),
                    (outbox.record_receipt, ('key', 'ref')),
                    (evidence.admit_merge, ('u', '1' * 40)),
                ]
                before = {p.name: p.read_bytes() for p in state.iterdir() if p.is_file()}
                for operation, arguments in calls:
                    with self.subTest(operation=operation.__name__):
                        try:
                            with self.assertRaises(outbox.OutboxError) as refused:
                                operation(state, *arguments)
                            self.assertIn(readiness.installation_state.REPAIR_GUIDANCE, str(refused.exception))
                        finally:
                            self.assertEqual(
                                {p.name: p.read_bytes() for p in state.iterdir() if p.is_file()},
                                before)
                # Real publication repairs every corruption, including symlinks.
                ready_installation(self.root / 'installed', self.entry)

    def test_refusal_route_sweep_uses_one_repair_constructor(self):
        """Finite source sweep; completeness beyond these routes is review-enforced."""
        import d5_installation as classifier
        self.assertIn('d5_install', classifier.REPAIR_GUIDANCE)
        self.assertIn('promote', classifier.REPAIR_GUIDANCE)
        inventory = {}
        for bundle in ('hanig-swarm', 'hanig-orchestrate'):
            for path in sorted((ROOT / 'skills' / bundle / 'scripts').glob('*.py')):
                tree = ast.parse(path.read_text())
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call) or not node.args:
                        continue
                    name = getattr(node.func, 'id', getattr(node.func, 'attr', ''))
                    if name not in ('OutboxError', 'Refusal', 'SystemExit', 'ValueError', 'exit'):
                        continue
                    value = node.args[0]
                    value = value.body if isinstance(value, ast.IfExp) else value
                    if not any(isinstance(n, ast.Constant) and isinstance(n.value, str)
                               and 'D5 active:' in n.value for n in ast.walk(value)):
                        continue
                    with self.subTest(path=path.name, line=node.lineno):
                        self.assertIsInstance(value, ast.Call)
                        self.assertIsInstance(value.func, ast.Attribute)
                        self.assertEqual(value.func.attr, 'refusal_message')
                        self.assertIsInstance(value.func.value, ast.Name)
                        self.assertEqual(value.func.value.id, 'DI')
                    inventory[path.name] = inventory.get(path.name, 0) + 1
        self.assertEqual(inventory, {'swarm_outbox.py': 7, 'swarm_evidence.py': 1,
            'swarm.py': 1, 'unit.py': 1, 'remote_verify.py': 2, 'merge_unit.py': 7})

    def test_broken_sibling_routes_carry_shared_repair_form(self):
        import argparse
        import swarm
        import merge_unit
        import remote_verify
        state = self.publish()
        with readiness.installer_lease(state):
            readiness.withdraw(state)
        calls = [
            lambda: swarm._d5_refuse('run', state),
            lambda: merge_unit._d5_refuse('fixture', state),
            lambda: merge_unit._d5_merge_launch_entry(state),
            lambda: merge_unit._d5_merge_entry(argparse.Namespace(state_dir=state)),
            lambda: remote_verify.run_remote(None, None, None, None, None, None, None, state, None, None),
            lambda: readiness.refuse_broken(state),
            lambda: readiness.require(state),
        ]
        for index, call in enumerate(calls):
            with self.subTest(route=index), readiness.installer_lease(state), mock.patch.object(subprocess, 'run',
                    side_effect=AssertionError('process before broken refusal')):
                with self.assertRaises((ValueError, SystemExit)) as refused:
                    call()
                self.assertIn(readiness.installation_state.REPAIR_GUIDANCE, str(refused.exception))
        for script, args in ((SCRIPTS / 'unit.py', ['allocate', '--root', str(self.root / 'runs'),
                '--task', 'fixture', '--kind', 'pipeline', '--output', 'out']),
                (SCRIPTS / 'remote_verify.py', ['--worker', str(self.root / 'missing')])):
            result = subprocess.run([sys.executable, '-B', str(script)] + args + ['--state-dir', str(state)],
                                    capture_output=True, text=True, timeout=30)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(readiness.installation_state.REPAIR_GUIDANCE, result.stdout + result.stderr)

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
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == activation.d5_active.__name__)
        calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
        self.assertEqual(len(calls), 1)
        self.assertEqual(ast.dump(calls[0]), ast.dump(ast.parse('d5_activation.' + activation.d5_active.__name__ + '(state_dir)').body[0].value))

    def test_fresh_process_agreement(self):
        state = self.publish()
        code = ('from __future__ import annotations\n'
                'import sys\nsys.path.insert(0, ' + repr(str(SCRIPTS)) + ')\n'
                'import d5_activation\n'
                'print(getattr(d5_activation, ' + repr(activation.d5_active.__name__) + ')(sys.argv[1]))\n')
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
        """OD-U8R-11 / implicit-lookup-differs: one operated-path resolver."""
        import swarm
        import coordinator_paths as paths
        old = Path.cwd()
        self.addCleanup(os.chdir, old)
        repo, other = self.root / 'repo', self.root / 'other'
        for directory in (repo, other):
            subprocess.run(['git', 'init', str(directory)], check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
            subprocess.run(['git', '-C', str(directory), 'symbolic-ref', 'HEAD',
                            'refs/heads/fixture-base'], check=True, capture_output=True, timeout=30)
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
                        os.chdir(cwd)
                        with mock.patch.object(paths, 'resolve_paths', wraps=paths.resolve_paths) as resolve:
                            classified = swarm._d5_state(None, plan=plan, extra_repos=extra)
                        resolve.assert_called_once_with(plan=plan, extra_repos=extra)
                        self.assertEqual(classified, operated)
        # Both preferred bases inside operated checkouts force the temp fallback.
        with mock.patch.dict(os.environ, {'XDG_STATE_HOME': str(repo)}), \
                mock.patch.object(Path, 'home', return_value=other):
            plan = {'units': [{'repo': str(other)}]}
            os.chdir(repo)
            self.assertEqual(swarm._d5_state(None, plan=plan),
                             paths.resolve_paths(plan=plan, cwd=repo)[0])

    def test_effectful_git_requires_prior_classification(self):
        import argparse
        import swarm
        repo, other = self.root / 'repo', self.root / 'other'
        for directory in (repo, other):
            subprocess.run(['git', 'init', str(directory)], check=True,
                           capture_output=True, timeout=30)
            subprocess.run(['git', '-C', str(directory), 'symbolic-ref', 'HEAD',
                            'refs/heads/fixture-base'], check=True, capture_output=True, timeout=30)
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
            # Only these existing resolver queries are allowed before classification.
            read_only = {('rev-parse', '--show-toplevel'),
                         ('worktree', 'list', '--porcelain')}
            import d5_installation
            run = subprocess.run
            for installed in (readiness.READY, readiness.BROKEN):
                if installed == readiness.BROKEN:
                    with readiness.installer_lease(state):
                        readiness.withdraw(state)
                self.assertEqual(readiness.state(state), installed)
                args = argparse.Namespace(plan=str(plan_path), state_dir=None,
                                          fn=swarm.cmd_run, command='run')
                calls = (lambda: swarm._d5_dispatch(args), lambda: swarm.cmd_run(args),
                         lambda: swarm._prepare_command_paths(args, plan=plan),
                         lambda: swarm.cmd_validate(argparse.Namespace(
                             plan=str(plan_path), state_dir=None, check_seeds=True)))
                for call, classification_count in zip(calls, (1, 2, 1, 1)):
                    events = []
                    previous = sys.getprofile()
                    def profile(frame, event, result):
                        if event == 'return' and frame.f_code is d5_installation.state.__code__:
                            events.append(('classified', frame.f_locals['state_dir'], result))
                        if previous is not None:
                            previous(frame, event, result)
                    def git_spy(argv, *positional, **keywords):
                        if Path(argv[0]).name == 'git':
                            arguments = tuple(argv[3:]) if argv[1] == '-C' else tuple(argv[1:])
                            events.append(('git', arguments))
                            self.assertIn(arguments, read_only,
                                          'effectful Git before D5 refusal: ' + repr(events))
                        return run(argv, *positional, **keywords)
                    with self.subTest(state=installed, call=call), \
                            mock.patch.object(subprocess, 'run', side_effect=git_spy):
                        try:
                            sys.setprofile(profile)
                            with self.assertRaisesRegex(SystemExit, 'D5 active:'):
                                call()
                        finally:
                            sys.setprofile(previous)
                    classified = [event for event in events if event[0] == 'classified']
                    self.assertEqual(classified, [('classified', state, installed)] * classification_count)
                    self.assertTrue(any(event[0] == 'git' for event in events))
                    self.assertEqual(events[-1], classified[0])

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
