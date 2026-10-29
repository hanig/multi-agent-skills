"""Named CLI consumer probe with disposable state and intercepted effects."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import importlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest import mock
import urllib.request


def capture(source, tool, command, active=False, installed=False):
    source = Path(source).resolve()
    sys.dont_write_bytecode = True
    for name in ('hanig-swarm', 'hanig-orchestrate', 'hanig-project'):
        sys.path.insert(0, str(source / 'skills' / name / 'scripts'))
    sys.path.insert(0, str(source))
    from tests.test_d5_u4_0b1_issuance import no_effects
    from tests.test_d5_registry import TestD5Registry
    from tests.fixtures.d5.ready import ready
    import d5_activation
    import unit
    import remote_verify
    import merge_unit
    import linear_api

    with ExitStack() as stack:
        tmp = Path(stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        # Replace PATH: an inherited scheduler must never enter the fixture.
        stack.enter_context(mock.patch.dict(os.environ, {'PATH': ''}, clear=True))
        old = Path.cwd()
        os.chdir(tmp)
        stack.callback(os.chdir, old)
        # None observes production wiring without an activation mock.
        if active is not None:
            stack.enter_context(mock.patch.object(d5_activation, 'd5_active', return_value=active))
        stack.enter_context(mock.patch.object(unit, 'now_iso', return_value='fixture-time'))
        state, runs = tmp / 'state', tmp / 'runs'
        state.mkdir()
        runs.mkdir()
        if installed:
            # Registry setup needs Git only before observation begins.
            with mock.patch.dict(os.environ, {'PATH': os.defpath}):
                case = TestD5Registry()
                case.setUp()
                stack.callback(case.doCleanups)
                with mock.patch.object(d5_activation, 'd5_active', return_value=True):
                    ready(case, case.repo, 'p', case.state)
            state = case.state
        (state / 'swarm-state.json').write_text('{"units":{}}')
        (runs / 'unit.json').write_text(json.dumps(dict(kind='pipeline', task_id='u',
            attempt_id='a', declared_outputs=['result'], created_at_epoch=0, job_id=None)))
        (runs / 'request.json').write_text(json.dumps(dict(verification_host={'executor': 'direct'},
            basis={}, timeout=1, checks=[])))
        binding = tmp / '.hanig' / 'linear-binding.json'
        binding.parent.mkdir()
        binding.write_text(json.dumps(dict(schema_version=1, repository='https://example.invalid/r',
            workspace={'id': 'workspace', 'name': 'Workspace'}, team={'id': 'team', 'key': 'ARC'},
            project={'id': 'project', 'name': 'Project'})))
        stack.enter_context(mock.patch.object(linear_api, 'load_key', return_value='synthetic'))
        entries = {
            'unit': {
                'allocate': ['allocate', '--root', str(runs), '--task', 'u', '--kind', 'pipeline', '--output', 'out'],
                'bind': ['bind', str(runs), '--job-id', '7'],
                'check': ['check', str(runs), '--json'],
            },
            'remote_verify': {name: ['--' + name, str(runs)] for name in ('worker', 'supervise')},
            'merge_unit': {'audit': []},
        }
        stack.enter_context(mock.patch.object(sys, 'argv', [tool + '.py'] + entries[tool][command]))
        transports = []
        def transport(*args, **kwargs):
            transports.append('wire')
            raise AssertionError('unexpected transport')
        stack.enter_context(mock.patch.object(urllib.request, 'urlopen', transport))
        def snapshot():
            return {str(p): p.read_bytes() for root in (state, runs)
                    for p in root.rglob('*') if p.is_file()}
        before = snapshot()
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output), no_effects() as (launches, writes):
            try:
                if tool == 'merge_unit':
                    result = merge_unit.print_tracker_audit(SimpleNamespace(
                        plan=str(tmp / 'plan.json'), state_dir=str(state)))
                else:
                    result = importlib.import_module(tool).main()
            except BaseException as exc:
                result = [type(exc).__name__, str(exc)]
        captured = dict(outcome=result, output=output.getvalue(), launches=launches, writes=writes,
                        transports=transports, protected_unchanged=before == snapshot())
        return json.loads(json.dumps(captured).replace(str(tmp), '<fixture>').replace(str(source), '<checkout>'))


if __name__ == '__main__':
    print(json.dumps(capture(*json.loads(sys.argv[1])), sort_keys=True))
