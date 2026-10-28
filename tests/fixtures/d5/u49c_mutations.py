"""Named entry/advisory reversions in memory; every restored test must pass."""
import ast
import io
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tests import test_d5_u49_entrypoints as tests


def replacement(module, name, old, new):
    source = Path(module.__file__).read_text()
    node = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == name)
    body = ast.get_source_segment(source, node)
    assert body.count(old) == 1, (name, old)
    namespace = {}
    exec(compile(body.replace(old, new), module.__file__, 'exec'), module.__dict__, namespace)
    return mock.patch.object(module, name, namespace[name])


CASES = {
    'entrypoint-unwired-cmd_run': ('TestRoutes.test_cmd_run', tests.S, 'cmd_run',
        "_d5_refuse('cmd_run')", 'pass'),
    'entrypoint-unwired-cmd_advance': ('TestRoutes.test_cmd_advance', tests.S, 'cmd_advance',
        "_d5_refuse('cmd_advance')", 'pass'),
    'entrypoint-unwired-swarm-main': ('TestRoutes.test_swarm_main', tests.S, 'main',
        '_d5_dispatch(args)', 'pass'),
    'entrypoint-unwired-merge-main': ('TestRoutes.test_merge_main', tests.M, 'main',
        'if D5.d5_active():', 'if False:'),
    'advisory-blocks': ('TestAdvisory.test_warning_truth_table_at_swarm_entries', tests.S,
        '_d5_bytecode_advisory', '        try:', "        raise SystemExit('advisory refusal')\n        try:"),
    'advisory-silent-when-active': ('TestAdvisory.test_warning_truth_table_at_swarm_entries', tests.S,
        '_d5_bytecode_advisory', 'if D5.d5_active() and not sys.dont_write_bytecode:', 'if False:'),
    'advisory-unwired-cmd_run': ('TestAdvisory.test_warning_truth_table_at_swarm_entries', tests.S,
        'cmd_run', '_d5_bytecode_advisory()', 'pass'),
    'advisory-unwired-cmd_advance': ('TestAdvisory.test_warning_truth_table_at_swarm_entries', tests.S,
        'cmd_advance', '_d5_bytecode_advisory()', 'pass'),
    'advisory-unwired-main': ('TestAdvisory.test_warning_truth_table_at_swarm_entries', tests.S,
        'main', '_d5_bytecode_advisory()', 'pass'),
    'advisory-stream-failure-blocks': ('TestAdvisory.test_read_only_and_diagnostic_failure_never_blocks',
        tests.S, '_d5_bytecode_advisory', '            pass', '            raise'),
}


def run_test(name):
    cls, method = name.split('.')
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(
        unittest.TestSuite([getattr(tests, cls)(method)]))
    print(stream.getvalue(), end='', flush=True)
    return result


def main():
    for name, (test, module, function, old, new) in CASES.items():
        print('MUTATION ' + name, flush=True)
        with replacement(module, function, old, new):
            result = run_test(test)
        if result.wasSuccessful():
            raise AssertionError('mutation survived: ' + name)
        if result.errors and name != 'advisory-stream-failure-blocks':
            raise AssertionError('mutation failed outside an assertion: ' + name)
        print('RESTORED ' + name, flush=True)
        if not run_test(test).wasSuccessful():
            raise AssertionError('restored regression failed: ' + name)
    print('All mutations killed; all restored regressions passed.')


if __name__ == '__main__':
    main()
