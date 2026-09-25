"""Exercise writer registration through unittest and the isolation consumer."""

import json
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


class TestJournalWriterRegistry(unittest.TestCase):
    def child(self, body):
        program = "\n".join((
            "import io, json, os, subprocess, sys, unittest",
            "from pathlib import Path",
            "from types import SimpleNamespace",
            "from unittest.mock import patch",
            "import tests.test_review as module",
            "from tests.journal_writers import journal_writer, registered_writer_names",
            "module.setUpModule()",
            "try:",
            textwrap.indent(textwrap.dedent(body), "    "),
            "finally:",
            "    module.tearDownModule()",
        ))
        result = subprocess.run(
            [sys.executable, "-c", program], cwd=REPO,
            capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_000_new_unmarked_writer_fails_before_persisting(self):
        report = self.child('''
            root = module._ensure_module_state_home()
            path = root / 'state' / module.review.JOURNAL_DIR / module.review.JOURNAL_NAME
            class NewWriter(unittest.TestCase):
                def test_new_writer(self):
                    module.review.append_review_journal(
                        path, 'implementation', 1, ['offline'], 'REVIEW_PASS',
                        [module.review.HONEST_RUN_CLAIM])
            result = unittest.TestResult()
            NewWriter('test_new_writer').run(result)
            print(json.dumps({'ok': result.wasSuccessful(),
                              'failures': [message for _, message in result.failures],
                              'errors': [message for _, message in result.errors],
                              'records': len(list(path.glob('*/record.jsonl')))}))
        ''')
        self.assertFalse(report['ok'],
                         'the new unmarked writer passed silently: %r' % report)
        self.assertEqual(report['errors'], [])
        self.assertEqual(len(report['failures']), 1)
        self.assertIn('unregistered journal writer', report['failures'][0])
        self.assertIn('NewWriter.test_new_writer', report['failures'][0])
        self.assertEqual(report['records'], 0)

    def test_record_wrapper_refuses_before_non_gating_production_handler(self):
        report = self.child('''
            called = []
            class NewWriter(unittest.TestCase):
                def test_new_writer(self):
                    module.review.record_review_round(None, [], 'REVIEW_PASS')
            with patch.object(module, '_ORIGINAL_RECORD_REVIEW_ROUND',
                              side_effect=lambda *a: called.append(a)):
                result = unittest.TestResult()
                NewWriter('test_new_writer').run(result)
            print(json.dumps({'ok': result.wasSuccessful(), 'called': called,
                              'failures': [message for _, message in result.failures],
                              'fixture_created': module._MODULE_STATE_HOME is not None}))
        ''')
        self.assertFalse(report['ok'])
        self.assertIn('unregistered journal writer', report['failures'][0])
        self.assertEqual(report['called'], [])
        self.assertFalse(report['fixture_created'])

    def test_writer_helpers_refuse_new_unmarked_tests_in_existing_classes(self):
        report = self.child('''
            results = []
            helpers = (
                (module.TestRangeDivergence, lambda self: self.range_cli('HEAD~1..HEAD')),
                (module.TestProtocolIsEnforcedNotRemembered, lambda self: self.cycle_cli()),
                (module.TestReviewJournal, lambda self: self.invoke()),
                (module.TestReviewJournal, lambda self: self.seed_record('new', {})),
            )
            for parent, helper in helpers:
                def test_new_writer(self, helper=helper):
                    helper(self)
                case_type = type('NewWriter', (parent,), {'test_new_writer': test_new_writer})
                result = unittest.TestResult()
                case_type('test_new_writer').run(result)
                results.append({'ok': result.wasSuccessful(),
                                'failures': [message for _, message in result.failures],
                                'errors': [message for _, message in result.errors]})
            print(json.dumps(results))
        ''')
        self.assertEqual(len(report), 4)
        for result in report:
            with self.subTest(result=result):
                self.assertFalse(result['ok'])
                self.assertEqual(result['errors'], [])
                self.assertEqual(len(result['failures']), 1)
                self.assertIn('unregistered journal writer', result['failures'][0])

    def test_registered_outer_test_cannot_authorize_unmarked_nested_case(self):
        report = self.child('''
            root = module._ensure_module_state_home()
            path = root / 'nested-journal'
            nested = unittest.TestResult()
            class Inner(unittest.TestCase):
                def test_unmarked(self):
                    module.review.append_review_journal(
                        path, 'implementation', 1, [], 'REVIEW_PASS', [])
            class Outer(unittest.TestCase):
                @journal_writer
                def test_marked(self):
                    Inner('test_unmarked').run(nested)
            outer = unittest.TestResult()
            Outer('test_marked').run(outer)
            print(json.dumps({'outer_ok': outer.wasSuccessful(),
                              'inner_ok': nested.wasSuccessful(),
                              'failures': [message for _, message in nested.failures],
                              'exists': path.exists()}))
        ''')
        self.assertTrue(report['outer_ok'])
        self.assertFalse(report['inner_ok'])
        self.assertIn('Inner.test_unmarked', report['failures'][0])
        self.assertFalse(report['exists'])

    def test_direct_unmarked_nested_method_cannot_use_outer_registration(self):
        report = self.child('''
            root = module._ensure_module_state_home()
            path = root / 'direct-nested-journal'
            class Inner(unittest.TestCase):
                def test_unmarked(self):
                    module.review.append_review_journal(
                        path, 'implementation', 1, [], 'REVIEW_PASS', [])
            class Outer(unittest.TestCase):
                @journal_writer
                def test_marked(self):
                    Inner('test_unmarked').test_unmarked()
            result = unittest.TestResult()
            Outer('test_marked').run(result)
            print(json.dumps({'ok': result.wasSuccessful(),
                              'failures': [message for _, message in result.failures],
                              'exists': path.exists()}))
        ''')
        self.assertFalse(report['ok'])
        self.assertEqual(len(report['failures']), 1)
        self.assertIn('Inner.test_unmarked', report['failures'][0])
        self.assertFalse(report['exists'])

    def test_unmarked_test_cannot_borrow_a_registered_case_helper(self):
        report = self.child('''
            class NewWriter(unittest.TestCase):
                def test_unmarked(self):
                    borrowed = module.TestReviewJournal(
                        'test_two_invocations_append_two_records_with_monotonic_timestamps')
                    borrowed.setUp()
                    try:
                        borrowed.invoke()
                    finally:
                        borrowed.doCleanups()
            result = unittest.TestResult()
            NewWriter('test_unmarked').run(result)
            print(json.dumps({'ok': result.wasSuccessful(),
                              'failures': [message for _, message in result.failures],
                              'errors': [message for _, message in result.errors]}))
        ''')
        self.assertFalse(report['ok'])
        self.assertEqual(report['errors'], [])
        self.assertEqual(len(report['failures']), 1)
        self.assertIn('NewWriter.test_unmarked', report['failures'][0])

    def test_borrowed_selected_method_does_not_authorize_its_unmarked_caller(self):
        report = self.child('''
            outcomes = []
            for marked in (False, True):
                seen = {}
                class Caller(unittest.TestCase):
                    def test_caller(self):
                        borrowed = module.TestReviewJournal(
                            'test_two_invocations_append_two_records_with_monotonic_timestamps')
                        borrowed.setUp()
                        try:
                            borrowed.test_two_invocations_append_two_records_with_monotonic_timestamps()
                            seen['records'] = len(borrowed.records())
                        finally:
                            borrowed.doCleanups()
                if marked:
                    Caller.test_caller = journal_writer(Caller.test_caller)
                result = unittest.TestResult()
                Caller('test_caller').run(result)
                outcomes.append({'marked': marked, 'ok': result.wasSuccessful(),
                                 'records': seen.get('records'),
                                 'failures': [message for _, message in result.failures],
                                 'errors': [message for _, message in result.errors]})
            print(json.dumps(outcomes))
        ''')
        self.assertFalse(report[0]['ok'], report[0])
        self.assertEqual(report[0]['errors'], [])
        self.assertEqual(len(report[0]['failures']), 1)
        self.assertIn('Caller.test_caller', report[0]['failures'][0])
        self.assertTrue(report[1]['ok'], report[1])
        self.assertEqual(report[1]['records'], 2)

    def test_case_object_without_active_test_cannot_authorize_a_helper(self):
        report = self.child('''
            borrowed = module.TestReviewJournal(
                'test_two_invocations_append_two_records_with_monotonic_timestamps')
            borrowed.setUp()
            try:
                refusal = None
                try:
                    borrowed.invoke()
                except AssertionError as error:
                    refusal = str(error)
                print(json.dumps({'refusal': refusal,
                                  'records': len(borrowed.records())}))
            finally:
                borrowed.doCleanups()
        ''')
        self.assertIsNotNone(report['refusal'], report)
        self.assertIn('without a registered test', report['refusal'])
        self.assertEqual(report['records'], 0)

    def test_static_and_class_methods_register_in_either_decorator_order(self):
        report = self.child('''
            from types import ModuleType
            paths = []
            def append():
                root = module._ensure_module_state_home()
                _, path = module.review.append_review_journal(
                    root / 'descriptor-journal', 'implementation', 1, [], 'REVIEW_PASS', [])
                paths.append(path)
            class Writers(unittest.TestCase):
                @journal_writer
                @staticmethod
                def test_static_outer():
                    append()
                @staticmethod
                @journal_writer
                def test_static_inner():
                    append()
                @journal_writer
                @classmethod
                def test_class_outer(cls):
                    append()
                @classmethod
                @journal_writer
                def test_class_inner(cls):
                    append()
            discovered = ModuleType('descriptor_fixture')
            discovered.Writers = Writers
            names = registered_writer_names(discovered)
            result = unittest.TestResult()
            unittest.defaultTestLoader.loadTestsFromModule(discovered).run(result)
            print(json.dumps({'ok': result.wasSuccessful(), 'names': names,
                              'count': result.testsRun,
                              'records': sum(path.is_file() for path in paths),
                              'failures': [message for _, message in result.failures],
                              'errors': [message for _, message in result.errors]}))
        ''')
        self.assertTrue(report['ok'], report)
        self.assertEqual(report['count'], 4)
        self.assertEqual(len(report['names']), 4)
        self.assertEqual(report['records'], 4)

    def test_guard_discovers_every_registered_writer_including_new_method(self):
        report = self.child('''
            executed = []
            @journal_writer
            def test_added_writer(self):
                root = module._ensure_module_state_home()
                _, path = module.review.append_review_journal(
                    root / 'added-journal', 'implementation', 1, [], 'REVIEW_PASS', [])
                self.assertTrue(path.is_file())
                executed.append(self.id())
            owner = module.TestReviewSuiteJournalIsolation
            owner.test_added_writer = test_added_writer
            names = registered_writer_names(module)
            commands = []
            def run(command, **kwargs):
                commands.append({'names': command[3:], 'timeout': kwargs['timeout']})
                return subprocess.CompletedProcess(command, 0, '', '')
            result = unittest.TestResult()
            with patch.object(module.subprocess, 'run', side_effect=run):
                owner('test_module_suite_leaves_the_user_journal_untouched').run(result)
            writer_result = unittest.TestResult()
            owner('test_added_writer').run(writer_result)
            print(json.dumps({'names': names, 'commands': commands,
                              'guard_ok': result.wasSuccessful(),
                              'guard_errors': [message for _, message in result.errors],
                              'writer_ok': writer_result.wasSuccessful(),
                              'executed': executed}))
        ''')
        self.assertTrue(report['guard_ok'], report['guard_errors'])
        self.assertTrue(report['writer_ok'])
        self.assertEqual(len(report['executed']), 1)
        self.assertIn(report['executed'][0], report['names'])
        self.assertGreater(len(report['names']), 3)
        self.assertEqual(len(report['commands']), 3)
        for command in report['commands']:
            self.assertEqual(command['names'], report['names'])
            self.assertEqual(command['timeout'], 600)

    def test_registration_survives_other_decorators_in_either_order(self):
        report = self.child('''
            import functools
            from types import ModuleType
            def wrapped(method):
                @functools.wraps(method)
                def call(self):
                    return method(self)
                return call
            class Writers(unittest.TestCase):
                @wrapped
                @journal_writer
                def test_inner(self):
                    self.append()
                @journal_writer
                @wrapped
                def test_outer(self):
                    self.append()
                def append(self):
                    root = module._ensure_module_state_home()
                    _, path = module.review.append_review_journal(
                        root / self._testMethodName, 'implementation', 1, [], 'REVIEW_PASS', [])
                    self.assertTrue(path.is_file())
            discovered = ModuleType('writer_fixture')
            discovered.Writers = Writers
            names = registered_writer_names(discovered)
            result = unittest.TestResult()
            unittest.defaultTestLoader.loadTestsFromModule(discovered).run(result)
            print(json.dumps({'ok': result.wasSuccessful(), 'names': names,
                              'count': result.testsRun,
                              'failures': [message for _, message in result.failures]}))
        ''')
        self.assertTrue(report['ok'], report['failures'])
        self.assertEqual(report['count'], 2)
        self.assertEqual(len(report['names']), 2)

    def test_guard_still_detects_operator_journal_byte_changes(self):
        report = self.child('''
            def corrupt(command, **kwargs):
                env = kwargs['env']
                if env.get('HOME') == str(module.REPO):
                    state = Path(env['TMPDIR']) / 'hanig-review-gate-state'
                else:
                    state = Path(env.get('XDG_STATE_HOME', str(Path(env['HOME']) / '.local' / 'state')))
                seed = next((state / module.review.JOURNAL_DIR).rglob('record.jsonl'))
                seed.write_bytes(b'changed operator bytes\\n')
                return subprocess.CompletedProcess(command, 0, '', '')
            result = unittest.TestResult()
            with patch.object(module.subprocess, 'run', side_effect=corrupt):
                module.TestReviewSuiteJournalIsolation(
                    'test_module_suite_leaves_the_user_journal_untouched').run(result)
            print(json.dumps({'ok': result.wasSuccessful(),
                              'failures': [message for _, message in result.failures],
                              'errors': [message for _, message in result.errors]}))
        ''')
        self.assertFalse(report['ok'])
        self.assertEqual(report['errors'], [])
        self.assertEqual(len(report['failures']), 3)
        for failure in report['failures']:
            self.assertIn('changed seeded journal state', failure)

    def test_opaque_decorator_requires_outermost_registration(self):
        report = self.child('''
            from types import ModuleType
            def opaque(method):
                def call(self):
                    return method(self)
                return call
            outcomes = []
            for marker_outermost in (False, True):
                path = module._ensure_module_state_home() / str(marker_outermost)
                def test_writer(self):
                    module.review.append_review_journal(
                        path, 'implementation', 1, [], 'REVIEW_PASS', [])
                if marker_outermost:
                    selected = journal_writer(opaque(test_writer))
                else:
                    selected = opaque(journal_writer(test_writer))
                writer = type('Writer', (unittest.TestCase,), {'test_writer': selected})
                discovered = ModuleType('opaque_fixture')
                discovered.Writer = writer
                result = unittest.TestResult()
                writer('test_writer').run(result)
                outcomes.append({'registered': list(registered_writer_names(discovered)),
                                 'ok': result.wasSuccessful(),
                                 'failures': [message for _, message in result.failures],
                                 'records': len(list(path.glob('*/record.jsonl')))})
            print(json.dumps(outcomes))
        ''')
        self.assertFalse(report[0]['ok'])
        self.assertEqual(report[0]['registered'], [])
        self.assertIn('unregistered journal writer', report[0]['failures'][0])
        self.assertEqual(report[0]['records'], 0)
        self.assertTrue(report[1]['ok'], report[1])
        self.assertEqual(len(report[1]['registered']), 1)
        self.assertEqual(report[1]['records'], 1)


    def test_direct_marked_method_without_runner_cannot_persist(self):
        report = self.child('''
            path = module._ensure_module_state_home() / 'direct-marked'
            class Writer(unittest.TestCase):
                @journal_writer
                def test_writer(self):
                    module.review.append_review_journal(
                        path, 'implementation', 1, [], 'REVIEW_PASS', [])
            refusal = None
            try:
                Writer('test_writer').test_writer()
            except AssertionError as error:
                refusal = str(error)
            print(json.dumps({'refusal': refusal,
                              'records': len(list(path.glob('*/record.jsonl')))}))
        ''')
        self.assertIsNotNone(report['refusal'], report)
        self.assertIn('without a registered test', report['refusal'])
        self.assertEqual(report['records'], 0)

    def test_renaming_unmarked_running_test_cannot_authorize_persistence(self):
        report = self.child('''
            outcomes = []
            for rename in (False, True):
                path = module._ensure_module_state_home() / ('rename-' + str(rename))
                class Writer(unittest.TestCase):
                    @journal_writer
                    def test_marked(self):
                        self.append()
                    def test_unmarked(self):
                        if rename:
                            self._testMethodName = 'test_marked'
                        self.append()
                    def append(self):
                        module.review.append_review_journal(
                            path, 'implementation', 1, [], 'REVIEW_PASS', [])
                result = unittest.TestResult()
                Writer('test_unmarked').run(result)
                outcomes.append({'rename': rename, 'ok': result.wasSuccessful(),
                                 'failures': [message for _, message in result.failures],
                                 'errors': [message for _, message in result.errors],
                                 'records': len(list(path.glob('*/record.jsonl')))})
            print(json.dumps(outcomes))
        ''')
        self.assertEqual(len(report), 2)
        for outcome in report:
            with self.subTest(rename=outcome['rename']):
                self.assertFalse(outcome['ok'], outcome)
                self.assertEqual(outcome['errors'], [])
                self.assertEqual(len(outcome['failures']), 1)
                self.assertIn('unregistered journal writer', outcome['failures'][0])
                self.assertEqual(outcome['records'], 0)

    def test_marked_running_code_keeps_authority_when_selected_name_changes(self):
        report = self.child('''
            outcomes = []
            for rename in (False, True):
                path = module._ensure_module_state_home() / ('marked-' + str(rename))
                class Writer(unittest.TestCase):
                    @journal_writer
                    def test_marked(self):
                        if rename:
                            self._testMethodName = 'test_unmarked'
                        module.review.append_review_journal(
                            path, 'implementation', 1, [], 'REVIEW_PASS', [])
                    def test_unmarked(self):
                        self.fail('the unmarked body must not execute')
                result = unittest.TestResult()
                Writer('test_marked').run(result)
                outcomes.append({'rename': rename, 'ok': result.wasSuccessful(),
                                 'failures': [message for _, message in result.failures],
                                 'errors': [message for _, message in result.errors],
                                 'records': len(list(path.glob('*/record.jsonl')))})
            print(json.dumps(outcomes))
        ''')
        for outcome in report:
            with self.subTest(rename=outcome['rename']):
                self.assertTrue(outcome['ok'], outcome)
                self.assertEqual(outcome['records'], 1)

    def test_runner_without_active_registered_code_cannot_persist_from_setup(self):
        report = self.child('''
            path = module._ensure_module_state_home() / 'setup-writer'
            class Writer(unittest.TestCase):
                def setUp(self):
                    module.review.append_review_journal(
                        path, 'implementation', 1, [], 'REVIEW_PASS', [])
                @journal_writer
                def test_marked(self):
                    pass
            result = unittest.TestResult()
            Writer('test_marked').run(result)
            print(json.dumps({'ok': result.wasSuccessful(),
                              'failures': [message for _, message in result.failures],
                              'errors': [message for _, message in result.errors],
                              'records': len(list(path.glob('*/record.jsonl')))}))
        ''')
        self.assertFalse(report['ok'], report)
        self.assertEqual(report['errors'], [])
        self.assertEqual(len(report['failures']), 1)
        self.assertIn('active registered test code', report['failures'][0])
        self.assertEqual(report['records'], 0)

    def test_renamed_direct_nested_case_cannot_borrow_outer_runner(self):
        report = self.child('''
            path = module._ensure_module_state_home() / 'renamed-nested'
            class Inner(unittest.TestCase):
                @journal_writer
                def test_marked(self):
                    pass
                def test_unmarked(case):
                    case._testMethodName = 'test_marked'
                    module.review.append_review_journal(
                        path, 'implementation', 1, [], 'REVIEW_PASS', [])
            class Outer(unittest.TestCase):
                @journal_writer
                def test_marked(self):
                    Inner('test_unmarked').test_unmarked()
            result = unittest.TestResult()
            Outer('test_marked').run(result)
            print(json.dumps({'ok': result.wasSuccessful(),
                              'failures': [message for _, message in result.failures],
                              'errors': [message for _, message in result.errors],
                              'records': len(list(path.glob('*/record.jsonl')))}))
        ''')
        self.assertFalse(report['ok'], report)
        self.assertEqual(report['errors'], [])
        self.assertEqual(len(report['failures']), 1)
        self.assertIn('unregistered journal writer', report['failures'][0])
        self.assertEqual(report['records'], 0)


    def test_marked_code_keeps_authority_after_receiver_local_rebinding(self):
        report = self.child('''
            outcomes = []
            for mode in ('delete', 'replace'):
                path = module._ensure_module_state_home() / ('receiver-' + mode)
                class Writer(unittest.TestCase):
                    @journal_writer
                    def test_writer(self):
                        if mode == 'delete':
                            del self
                        else:
                            self = object()
                        module.review.append_review_journal(
                            path, 'implementation', 1, [], 'REVIEW_PASS', [])
                result = unittest.TestResult()
                Writer('test_writer').run(result)
                outcomes.append({'mode': mode, 'ok': result.wasSuccessful(),
                                 'failures': [message for _, message in result.failures],
                                 'errors': [message for _, message in result.errors],
                                 'records': len(list(path.glob('*/record.jsonl')))})
            print(json.dumps(outcomes))
        ''')
        self.assertEqual(len(report), 2)
        for outcome in report:
            with self.subTest(mode=outcome['mode']):
                self.assertTrue(outcome['ok'], outcome)
                self.assertEqual(outcome['records'], 1)


if __name__ == '__main__':
    unittest.main()
