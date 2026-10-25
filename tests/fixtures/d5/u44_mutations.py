"""Run U4.4 regression tests against bounded in-memory mutations, then restore."""
import ast
from contextlib import ExitStack
import io
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tests import test_d5_u44_result_consumption as tests


def replacement(module, name, old, new):
    tree = ast.parse(Path(module.__file__).read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    source = ast.get_source_segment(Path(module.__file__).read_text(), node)
    if source.count(old) != 1:
        raise AssertionError('mutation target not unique: ' + name)
    namespace = {}
    exec(compile(source.replace(old, new), module.__file__, 'exec'), module.__dict__, namespace)
    return mock.patch.object(module, name, namespace[name])


def patches(name):
    if name == 'intent-label-completes':
        return [replacement(tests.M, 'reconcile', 'if intent is not None:\n',
            'if intent is not None and intent.get("phase") in ("merged", "receipt_recorded"):\n'
            '            return None\n        if intent is not None:\n')]
    if name == 'legacy-receipt-completes':
        return [replacement(tests.SE, 'admit_merge',
            'if D5.d5_active():', 'if False:')]
    if name == 'report-before-readback':
        return [replacement(tests.M, '_d5_result_receipt',
            'J._readback(handle, expected)', 'return expected')]
    if name == 'typed-receipt':
        return [replacement(tests.M, '_d5_result_receipt',
            'J.encoded(r) == J.encoded(expected)', 'r == expected')]
    if name == 'historical-grant':
        # Remove both the provider fence and the consumer fence to demonstrate
        # that even an old successful receipt must not acquire D5 authority.
        return [replacement(tests.J, 'recover',
            '_require(type(intent.get("d5")) is dict, "legacy intent cannot authorize active recovery")',
            'if "d5" not in intent:\n        return outcome'),
            replacement(tests.M, '_d5_result_receipt',
            'if current != operation_id or not isinstance((intent or {}).get("d5"), dict):',
            'if "d5" not in intent:\n        return {}\n'
            '    if current != operation_id or not isinstance((intent or {}).get("d5"), dict):')]
    if name == 'stale-m6-base':
        return [mock.patch.object(tests, 'BASE', '1b61800ff263b4a10978afdbbdfc64b117195914')]
    raise AssertionError(name)


CASES = {
    'intent-label-completes': 'OperatorCase.test_recorded_intent_needs_matching_receipt_on_every_recovery',
    'legacy-receipt-completes': 'OperatorCase.test_direct_consumption_refuses_even_ready_valid_receipt',
    'report-before-readback': 'OperatorCase.test_result_without_durable_readback_never_reports_success',
    'historical-grant': 'OperatorCase.test_historical_intent_and_receipt_never_gain_active_authority',
    'stale-m6-base': 'TestInactiveAndPremature.test_inactive_capture_on_recorded_base_with_inherited_sinfo',
    'typed-receipt': 'OperatorCase.test_readback_rejects_boolean_integer_aliases',
}


def run_test(name):
    cls, method = CASES[name].split('.')
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(
        unittest.TestSuite([getattr(tests, cls)(method)]))
    print(stream.getvalue(), end='')
    return result


def main():
    for name in CASES:
        print('MUTATION ' + name, flush=True)
        with ExitStack() as stack:
            for patch in patches(name):
                stack.enter_context(patch)
            result = run_test(name)
        if not result.failures or result.errors:
            raise AssertionError('mutation must fail assertions, not setup: ' + name)
        print('RESTORED ' + name, flush=True)
        if not run_test(name).wasSuccessful():
            raise AssertionError('restored regression failed: ' + name)
    print('All mutations killed; all restored regressions passed.')


if __name__ == '__main__':
    main()
