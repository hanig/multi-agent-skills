"""Direct-CLI effect probe. All credentials, state and transports are synthetic."""
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
import importlib
import importlib.machinery
import io
import json
import os
from pathlib import Path
import runpy
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


@contextmanager
def gate_writes(root):
    """Permit real committee persistence only under its fixed fixture root."""
    running, writes = [True], []
    def audit(event, args):
        if not running[0]:
            return
        paths = []
        if event == 'open' and args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
            paths = [args[0]]
        elif event in ('os.mkdir', 'os.remove', 'os.rmdir', 'os.chmod'):
            paths = [args[0]]
        elif event in ('os.rename', 'os.link', 'os.symlink'):
            paths = list(args[:2])
        elif event.startswith(('subprocess.', 'os.exec', 'os.spawn', 'os.posix_spawn', 'socket.')) or event in ('os.system', 'os.fork'):
            raise AssertionError('unexpected effect: ' + event)
        for path in paths:
            target = Path(path).resolve()
            if target != root and root not in target.parents:
                raise AssertionError('write outside gate root: ' + str(target))
            writes.append(str(target))
    sys.addaudithook(audit)
    try:
        yield [], writes
    finally:
        running[0] = False


def capture(source, tool, command, active=False, installed=False, allow_gate=False):
    source = Path(source).resolve()
    sys.dont_write_bytecode = True
    for name in ('hanig-swarm', 'hanig-orchestrate', 'hanig-project', 'hanig-review-gate'):
        sys.path.insert(0, str(source / 'skills' / name / 'scripts'))
    sys.path.insert(0, str(source))
    from tests.test_d5_u4_0b1_issuance import no_effects
    from tests.test_d5_registry import TestD5Registry
    from tests.fixtures.d5.ready import ready
    from tests.watchdog_guard import guarded_main
    import d5_activation
    import linear_sync
    import linear_api
    import review
    import committee
    import unit
    import remote_verify
    import pwd

    with ExitStack() as stack:
        tmp = Path(stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        if tool == 'merge_unit':
            from tests.test_d5_u4_1_active_surface import SurfaceFixture
            return SurfaceFixture(tmp).observe('operator', command, active)
        tools = tmp / 'tools'
        tools.mkdir()
        (tools / 'git').symlink_to(shutil.which('git'))
        home, state, runs, ops = [tmp / name for name in ('home', 'state', 'runs', 'ops')]
        for path in (home, state, runs, ops):
            path.mkdir()
        stack.enter_context(mock.patch.dict(os.environ, {
            'PATH': str(tools), 'HOME': str(home), 'HANIG_LINEAR_OPS_DIR': str(ops),
            'LINEAR_API_KEY': 'synthetic', 'OPENAI_API_KEY': 'synthetic',
            'ANTHROPIC_API_KEY': 'synthetic', 'OPENROUTER_API_KEY': 'synthetic',
        }, clear=True))
        stack.enter_context(mock.patch.object(pwd, 'getpwuid', return_value=SimpleNamespace(pw_dir=str(home))))
        old = Path.cwd()
        os.chdir(tmp)
        stack.callback(os.chdir, old)
        stack.enter_context(mock.patch.object(d5_activation, 'd5_active', return_value=active))
        stack.enter_context(mock.patch('time.sleep', return_value=None))
        stack.enter_context(mock.patch.object(unit, 'now_iso', return_value='fixture-time'))
        stack.enter_context(mock.patch.object(linear_sync, 'utc', return_value='2026-01-01T00:00:00+00:00'))
        loader = importlib.machinery.SourceFileLoader.exec_module

        def loaded(self, module):
            loader(self, module)
            if module.__name__ in ('_linear_d5_activation', '_gate_d5_activation'):
                module.d5_active = lambda: active
        stack.enter_context(mock.patch.object(importlib.machinery.SourceFileLoader, 'exec_module', loaded))
        if installed:
            case = TestD5Registry()
            case.setUp()
            stack.callback(case.doCleanups)
            with mock.patch.object(d5_activation, 'd5_active', return_value=True):
                ready(case, case.repo, 'p', case.state)
            state = case.state
        (state / 'swarm-state.json').write_text('{"units":{}}')
        (state / 'outbox.jsonl').write_text('{"pending":true}\n')
        (ops / 'pending.json').write_text('{"pending":true}')
        (runs / 'unit.json').write_text(json.dumps(dict(kind='pipeline', task_id='u',
            attempt_id='a', declared_outputs=['result'], created_at_epoch=0, job_id=None)))
        (runs / 'request.json').write_text(json.dumps(dict(verification_host={'executor': 'direct'},
            basis={}, timeout=1, checks=[])))
        binding = tmp / 'binding.json'
        binding.write_text(json.dumps(dict(schema_version=1, repository='https://example.invalid/r',
            workspace={'id': 'workspace', 'name': 'Workspace'}, team={'id': 'team', 'key': 'ARC'},
            project={'id': 'project', 'name': 'Project'})))
        body = tmp / 'body.md'
        body.write_text('Fixture body')
        committee.SESSIONS = home / '.hanig-committee'
        members = [r for r in review.load_reviewers() if r.get('enabled', True)][:2]
        committee.save_session('fixture', dict(name='fixture', phase='plan', opened_at='fixture',
            problem='fixture', turns=[], members={r['name']: dict(provider=r['provider'],
            model=r['model'], history=[]) for r in members}))
        entries = {
            'unit': {
                'allocate': ['allocate', '--root', str(runs), '--task', 'u', '--kind', 'pipeline', '--output', 'out'],
                'bind': ['bind', str(runs), '--job-id', '7'],
                'check': ['check', str(runs), '--json'],
            },
            'remote_verify': {name: ['--' + name, str(runs)] for name in ('worker', 'supervise')},
            'linear_sync': {
                'bind': ['bind', '--project', 'project', '--repository', 'https://example.invalid/r'],
                'drain': ['drain', '--binding', str(binding), '--state-dir', str(state)],
                'drain-dry': ['drain', '--binding', str(binding), '--state-dir', str(state), '--dry-run'],
                'file': ['file', '--draft', str(binding)],
                'replay': ['replay', 'fixture', '--draft', str(binding)],
                'issue-new': ['issue', 'new', '--binding', str(binding), '--title', 'Fixture',
                              '--body-file', str(body), '--independent', 'fixture', '--approver', 'Fixture'],
                'issue-edit': ['issue', 'edit', 'ARC-1', '--binding', str(binding),
                               '--title', 'Fixture', '--approver', 'Fixture'],
                'issue-replay': ['issue', 'replay', 'fixture', '--binding', str(binding)],
                'audit': ['audit', '--binding', str(binding)],
                'audit-state': ['audit', '--binding', str(binding), '--out', str(state / 'other.json')],
                'audit-ops': ['audit', '--binding', str(binding), '--out', str(ops / 'other.json')],
                'section': ['section', '--audit', str(binding)],
            },
            'review': {
                'size': ['--size', '--file', str(body)],
                'list': ['--list'],
                'ledger': ['--open-findings', '--head', 'a' * 40],
            },
            'committee': {
                'open': ['open', 'new', '--problem', 'fixture'],
                'ask': ['ask', 'fixture', '--prompt', 'fixture'],
                'review': ['review', 'fixture'],
                'synthesize': ['synthesize', 'fixture'],
                'tiebreak': ['tiebreak', 'fixture'],
                'show': ['show', 'fixture'],
            },
        }
        argv = [tool + '.py'] + entries[tool][command]
        stack.enter_context(mock.patch.object(sys, 'argv', argv))
        if tool == 'linear_sync':
            # Keep the real Client, intercept the single actual wire transport.
            def transport(*args, **kwargs):
                effects.append('tracker')
                raise AssertionError('tracker')
            stack.enter_context(mock.patch.object(linear_api, 'transport', transport))
        effects = []
        def provider(*args, **kwargs):
            effects.append('provider')
            if allow_gate:
                request = args[0]
                assert request.get_method() == 'POST'
                assert request.full_url in ('https://api.openai.com/v1/responses',
                    'https://api.anthropic.com/v1/messages', 'https://openrouter.ai/api/v1/chat/completions')
                return io.BytesIO(json.dumps(dict(status='completed', usage={}, stop_reason='end_turn',
                    output=[dict(type='message', content=[dict(text='fixture')])],
                    choices=[dict(finish_reason='stop', message=dict(content='fixture'))],
                    content=[dict(type='text', text='fixture')])).encode())
            raise AssertionError('provider')
        stack.enter_context(mock.patch.object(review.urllib.request, 'urlopen', provider))
        before = {str(p): p.read_bytes() for root in (state, runs, ops)
                  for p in root.rglob('*') if p.is_file()}
        output = io.StringIO()
        commands = []
        if allow_gate and command == 'review':
            stack.enter_context(mock.patch.object(review, 'git_out', return_value='diff --git a/f b/f\n+fixture'))
        boundary = gate_writes(committee.SESSIONS.resolve()) if allow_gate else no_effects()
        with redirect_stdout(output), redirect_stderr(output), boundary as (launches, writes):
            def launching(original):
                def call(argv, *args, **kwargs):
                    commands.append(argv)
                    return original(argv, *args, **kwargs)
                return call
            with ExitStack() as calls:
                for name in ('Popen', 'run'):
                    calls.enter_context(mock.patch('subprocess.' + name, launching(getattr(__import__('subprocess'), name))))
                try:
                    result = guarded_main(importlib.import_module(tool).main)
                except BaseException as exc:
                    result = [type(exc).__name__, str(exc)]
        after = {str(p): p.read_bytes() for root in (state, runs, ops)
                 for p in root.rglob('*') if p.is_file()}
        captured = dict(outcome=result, output=output.getvalue(), launches=launches, commands=commands, writes=writes,
                        transports=effects, protected_unchanged=before == after)
        return json.loads(json.dumps(captured).replace(str(tmp), '<fixture>').replace(str(source), '<checkout>'))


if __name__ == '__main__':
    print(json.dumps(capture(*json.loads(sys.argv[1])), sort_keys=True))
