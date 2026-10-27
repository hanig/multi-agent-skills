"""Inactive gate CLI captures with synthetic sessions and intercepted effects."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import importlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest import mock


def capture(source, tool, command):
    source = Path(source).resolve()
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(source))
    sys.path.insert(0, str(source / 'skills/hanig-review-gate/scripts'))
    from tests.test_d5_u4_0b1_issuance import no_effects
    from tests.watchdog_guard import guarded_main
    import review
    import committee
    with ExitStack() as stack:
        root = Path(stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        stack.enter_context(mock.patch.dict(os.environ, {
            'PATH': '', 'XDG_STATE_HOME': str(root / 'state'),
            'HANIG_COMMITTEE_DIR': str(root / 'sessions'),
            'HANIG_ROUTING_SNAPSHOTS': 'off', 'OPENAI_API_KEY': 'synthetic',
            'ANTHROPIC_API_KEY': 'synthetic', 'OPENROUTER_API_KEY': 'synthetic',
        }, clear=True))
        stack.enter_context(mock.patch.object(committee, 'SESSIONS', root / 'sessions'))
        stack.enter_context(mock.patch.object(review.time, 'sleep', return_value=None))
        subject = root / 'body.txt'
        subject.write_text('Fixture body')
        committee.save_session('fixture', dict(name='fixture', phase='plan',
            opened_at='fixture', problem='fixture', turns=[], members={}))
        entries = {
            'review': {
                'size': ['--size', '--file', str(subject)],
                'list': ['--list'],
                'ledger': ['--open-findings', '--head', 'a' * 40],
                'option': ['--size', '--range=--output=synthetic'],
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
        stack.enter_context(mock.patch.object(sys, 'argv', [tool + '.py'] + entries[tool][command]))
        transports = []
        def transport(*args, **kwargs):
            transports.append('provider')
            raise AssertionError('unexpected provider')
        stack.enter_context(mock.patch.object(review.urllib.request, 'urlopen', transport))
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output), no_effects() as (launches, writes):
            try:
                result = guarded_main(importlib.import_module(tool).main)
            except BaseException as exc:
                result = [type(exc).__name__, str(exc)]
        captured = dict(outcome=result, output=output.getvalue(), launches=launches,
                        writes=writes, transports=transports)
        return json.loads(json.dumps(captured).replace(str(root), '<fixture>')
                          .replace(str(source), '<checkout>'))


if __name__ == '__main__':
    print(json.dumps(capture(*json.loads(sys.argv[1])), sort_keys=True))
