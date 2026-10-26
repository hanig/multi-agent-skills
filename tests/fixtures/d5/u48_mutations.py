"""U4.8 bounded in-memory reversions; disk code and captured base stay intact."""
from contextlib import ExitStack
import io
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tests import test_d5_u48_state_compat as tests
from tests.fixtures.d5.u44_mutations import replacement


CASES = {
    'legacy-normalized-active': 'TestPreparedState.test_active_normalization_and_migration_refuse_even_with_readiness',
    'legacy-migrated-active': 'TestPreparedState.test_active_normalization_and_migration_refuse_even_with_readiness',
    'legacy-key-changed': 'TestPreparedState.test_legacy_operation_key_stable_across_epoch_and_d5_metadata',
    'historical-reinterpreted': 'TestHistoricalIntent.test_historical_intent_refuses_before_registration_or_forge_reads',
    'inactive-migration-changed': 'TestInactive.test_base_and_inactive_effects_with_inherited_sinfo',
    'ineligible-state-ready': 'TestPreparedState.test_ineligible_state_cannot_publish_readiness_or_enter_merge',
    'premature-activation': 'TestPreparedState.test_premature_activation_authority_launches_and_writes_nothing',
    'stale-m6-base': 'TestInactive.test_base_and_inactive_effects_with_inherited_sinfo',
}


def patches(name):
    if name == 'legacy-normalized-active':
        return [replacement(tests.S, 'advance', "    _d5_refuse('advance')", '    pass')]
    if name == 'legacy-migrated-active':
        return [replacement(tests.S, '_prepare_command_paths',
                            "        _d5_refuse('_prepare_command_paths')", '        pass')]
    if name == 'legacy-key-changed':
        return [replacement(tests.M, 'authority',
            'binding = {"unit": args.unit,', 'binding = {"state_epoch": epoch, "unit": args.unit,')]
    if name == 'historical-reinterpreted':
        return [replacement(tests.M, 'reconcile',
            '        if intent is not None:\n            _d5_journal_binding(intent, ready, binding["repo"])',
            '        pass')]
    if name == 'inactive-migration-changed':
        return [replacement(tests.S, '_prepare_command_paths',
            'CP.migrate_legacy_defaults(state, root, cwd=os.getcwd())', 'None')]
    if name == 'ineligible-state-ready':
        return [mock.patch.object(tests.R, 'prepared_state', return_value=None)]
    if name == 'premature-activation':
        return [replacement(tests.M, 'authority', 'if D5.d5_active():', 'if False:')]
    if name == 'stale-m6-base':
        return [mock.patch.object(tests, 'BASE', 'eb90c2c50081291c43e7177bcd3edfc2197fe61e')]
    raise AssertionError(name)


def run_test(name):
    cls, method = CASES[name].split('.')
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(
        unittest.TestSuite([getattr(tests, cls)(method)]))
    print(stream.getvalue(), end='', flush=True)
    return result


def main():
    for name in CASES:
        print('MUTATION ' + name, flush=True)
        with ExitStack() as stack:
            for patch in patches(name):
                stack.enter_context(patch)
            result = run_test(name)
        if result.wasSuccessful():
            raise AssertionError('mutation survived: ' + name)
        # A removed migration produces a missing destination (FileNotFound),
        # not merely a changed return value; other mutants hit assertions.
        if result.errors and name != 'inactive-migration-changed':
            raise AssertionError('mutation failed outside an assertion: ' + name)
        print('RESTORED ' + name, flush=True)
        if not run_test(name).wasSuccessful():
            raise AssertionError('restored regression failed: ' + name)
    print('All mutations killed; all restored regressions passed.')


if __name__ == '__main__':
    main()
