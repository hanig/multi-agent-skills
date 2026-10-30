"""M6 first-effect prefixes from fixed inputs, independent of host paths.

The real entry bodies run until the first intercepted preparation operation.
This measures entry routing, not successful dispatch or merge execution.
"""
from contextlib import nullcontext
import argparse
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import sys
import tempfile
from unittest import mock


MERGE_ARGV = ['fixture-plan.json', '--state-dir', 'fixture-state', '--unit', 'u',
              '--pr', '1', '--approver', 'Operator']


class CaptureStop(RuntimeError):
    pass


def inactive_captures(swarm, merge):
    captures = {}
    for entry in ('swarm.cmd_run', 'swarm.cmd_advance', 'swarm.main', 'merge_unit.main'):
        effects, out, err = [], io.StringIO(), io.StringIO()

        def stop(operation):
            def call(value):
                effects.append([operation, value])
                raise CaptureStop(operation)
            return call

        with ExitStack() as stack:
            state_dir = stack.enter_context(tempfile.TemporaryDirectory())
            stack.enter_context(mock.patch.object(swarm.SR, 'apply_agent_resolution',
                side_effect=stop('apply_agent_resolution')))
            stack.enter_context(mock.patch.object(merge, 'read_object', side_effect=stop('read_object')))
            stack.enter_context(mock.patch.object(sys, 'argv', ['swarm.py', 'run', 'fixture-plan.json', '--state-dir', state_dir]))
            stack.enter_context(redirect_stdout(out))
            stack.enter_context(redirect_stderr(err))
            try:
                if entry == 'merge_unit.main':
                    value = merge.main(MERGE_ARGV)
                elif entry == 'swarm.main':
                    value = swarm.main()
                else:
                    value = getattr(swarm, entry.split('.')[1])(argparse.Namespace(plan='fixture-plan.json', state_dir=state_dir))
                outcome = ['return', value]
            except (CaptureStop, SystemExit) as exc:
                outcome = [type(exc).__name__, str(exc)]
        captures[entry] = dict(effects=effects, outcome=outcome, stdout=out.getvalue(), stderr=err.getvalue())
    return captures
