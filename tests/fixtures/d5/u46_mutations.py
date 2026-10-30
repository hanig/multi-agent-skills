"""Named U4.6 mutations run in isolated operator processes; no source edits."""
import ast
from contextlib import ExitStack
import io
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))


def replacement(module, name, old, new):
    source = Path(module.__file__).read_text()
    nodes = ast.parse(source).body
    owner = module
    if '.' in name:
        cls, name = name.split('.')
        nodes = next(n for n in nodes if isinstance(n, ast.ClassDef) and n.name == cls).body
        owner = getattr(module, cls)
    node = next(n for n in nodes if isinstance(n, ast.FunctionDef) and n.name == name)
    import textwrap
    source = textwrap.dedent(ast.get_source_segment(source, node))
    if source.count(old) != 1:
        raise AssertionError('mutation target not unique: ' + name)
    namespace = {}
    exec(compile(source.replace(old, new), module.__file__, 'exec'), module.__dict__, namespace)
    return mock.patch.object(owner, name, namespace[name])


def patch(name):
    import merge_unit as M
    import d5_submission as DS
    release = 'if H and S and (not A or N or (F and C)):'
    if name in ('cancel-after-dispatch-noeffect', 'premature-activation'):
        return replacement(M, 'cancel_before_request',
                           "_d5_refuse('cancel_before_request', Path(intent_path).parent)", 'pass')
    if name == 'timeout-completes':
        return replacement(M, '_MergeBoundary.finish', '        raise\n', '        return self.completion()\n')
    if name == 'reap-releases':
        return replacement(DS, 'submit', release,
            'if H and S and (not A or N or (F and C) or boundary.process.poll() is not None):')
    if name == 'unknown-released':
        return replacement(DS, 'submit', release, 'if H and S:')
    if name == 'normal-never-releases':
        return replacement(DS, 'submit', 'os.close(holder)', 'pass')
    raise AssertionError(name)


CASES = {
    'cancel-after-dispatch-noeffect': 'TestCancellation.test_cancel_after_dispatch_cannot_use_legacy_noeffect',
    'timeout-completes': 'TestCancellation.test_timeout_does_not_complete_or_release',
    'reap-releases': 'TestCancellation.test_external_exit_and_reap_do_not_release',
    'unknown-released': 'TestCancellation.test_unknown_queued_operation_does_not_release',
    'normal-never-releases': 'TestCancellation.test_normal_completion_releases_after_receipt_under_lease',
    'premature-activation': 'TestInactiveAndPremature.test_premature_activation_zero_effects',
    'stale-m6-base': 'TestInactiveAndPremature.test_inactive_base_capture_with_inherited_sinfo',
}


def main():
    from tests import test_d5_u46_cancellation as tests
    def run(name):
        cls, method = CASES[name].split('.')
        stream = io.StringIO()
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(
            unittest.TestSuite([getattr(tests, cls)(method)]))
        print(stream.getvalue(), end='', flush=True)
        return result
    for name in CASES:
        print('MUTATION ' + name, flush=True)
        with ExitStack() as stack:
            if name == 'stale-m6-base':
                stack.enter_context(mock.patch.object(tests, 'BASE', 'eb90c2c50081291c43e7177bcd3edfc2197fe61e'))
            elif name == 'premature-activation':
                stack.enter_context(patch(name))
            else:
                stack.enter_context(mock.patch.object(tests, 'MUTATION', name))
            result = run(name)
        if not result.failures or result.errors:
            raise AssertionError('mutation must fail assertions, not setup: ' + name)
        print('RESTORED ' + name, flush=True)
        if not run(name).wasSuccessful():
            raise AssertionError('restored regression failed: ' + name)
    print('All seven mutations killed; restored regressions passed.')


if __name__ == '__main__':
    main()
