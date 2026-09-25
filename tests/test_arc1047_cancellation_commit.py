"""Cancellation crash recovery through the CLI, real Git and a stub-only PATH."""

import json
from pathlib import Path
import unittest

from tests import test_arc683_merge_precondition as preconditions


class TestCancellationCommit(unittest.TestCase):
    def setUp(self):
        self.precondition = preconditions.TestMergePrecondition()
        self.precondition.setUp()
        self.addCleanup(self.precondition.doCleanups)
        self.f = self.precondition.f

    def interrupt(self, stage):
        """Inject a process exit or storage error at a real publication boundary."""
        site = Path(self.f.env['PYTHONPATH']) / 'sitecustomize.py'
        with site.open('a') as handle:
            handle.write('''
if sys.argv[0].endswith('merge_unit.py'):
    arc_replace, arc_sync, arc_unlink = os.replace, os.fsync, pathlib.Path.unlink
    arc_stage = %r
    arc_published = False
    arc_cancel_synced = False
    arc_commit_synced = False
    def arc_replace_probe(src, dst):
        global arc_published
        if pathlib.Path(dst).suffix == '.cancellation-committed':
            assert arc_cancel_synced, 'commit published before cancellation directory fsync'
            if arc_stage == 'before-commit':
                os._exit(92)
            if arc_stage == 'commit-rename-failure':
                raise OSError('injected commit rename failure')
            result = arc_replace(src, dst)
            arc_published = True
            return result
        return arc_replace(src, dst)
    def arc_sync_probe(fd):
        global arc_cancel_synced, arc_commit_synced
        if arc_published and stat.S_ISDIR(os.fstat(fd).st_mode):
            if arc_stage == 'commit-directory-failure':
                raise OSError('injected commit directory fsync failure')
        result = arc_sync(fd)
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            state = pathlib.Path(os.environ['COORDINATOR_STATE'])
            if any(json.loads(p.read_text()).get('phase') == 'cancelled_before_request'
                   for p in state.glob('merge-unit-*.json')):
                arc_cancel_synced = True
            if arc_published:
                arc_commit_synced = True
        return result
    def arc_unlink_probe(path, *args, **kwargs):
        if path.suffix == '.cancellation-pending' and arc_stage == 'after-commit':
            assert arc_commit_synced, 'pending removed before commit directory fsync'
            os._exit(91)
        return arc_unlink(path, *args, **kwargs)
    os.replace, os.fsync, pathlib.Path.unlink = arc_replace_probe, arc_sync_probe, arc_unlink_probe
''' % stage)

    def leave_committed_crash(self):
        target, _ = self.precondition.move_after_publication()
        self.interrupt('after-commit')
        result = self.f.invoke()
        self.f.assert_refused(result)
        self.assertEqual(result.returncode, 91, result.stdout + result.stderr)
        cancelled = self.f.intent()
        self.assertEqual(cancelled['phase'], 'cancelled_before_request')
        pending, = self.f.state_dir.glob('*.cancellation-pending')
        del self.f.env['PYTHONPATH']
        return target, cancelled, pending

    def test_crash_after_commit_before_pending_removal_allows_fresh_verification(self):
        target, cancelled, pending = self.leave_committed_crash()
        result = self.f.invoke('--verify-integration')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('unresolved', result.stderr)
        self.assertEqual(self.f.intent(), cancelled)
        self.assertFalse(pending.exists())
        marker = pending.with_suffix('.cancellation-committed')
        self.assertEqual(json.loads(marker.read_text()), cancelled)
        self.assertEqual(preconditions.S.load_verifications(self.f.state_dir)[0][-1]
                         ['target_commit'], target)
        self.assertEqual(self.f.calls(['pr', 'merge']), [])

    def test_failed_directory_fsync_and_rollback_still_refuses(self):
        self.precondition.move_after_publication(fail_resolution='rollback')
        result = self.f.invoke()
        self.f.assert_refused(result)
        self.assertIn('injected cancellation rollback failure', result.stderr)
        self.assertEqual(self.f.intent()['phase'], 'cancelled_before_request')
        pending, = self.f.state_dir.glob('*.cancellation-pending')
        original = json.loads(pending.read_text())
        self.assertFalse(pending.with_suffix('.cancellation-committed').exists())
        del self.f.env['PYTHONPATH']
        for extra in ((), ('--verify-integration',)):
            result = self.f.invoke(*extra)
            self.f.assert_refused(result)
            self.assertIn('unresolved', result.stderr)
            self.assertEqual(self.f.intent(), original)

    def test_recovered_cancellation_rechecks_target_and_merges_exactly_once(self):
        target, cancelled, pending = self.leave_committed_crash()
        stale = self.f.invoke()
        self.f.assert_refused(stale)
        self.assertIn('target moved', stale.stderr)
        self.assertNotIn('unresolved outcome', stale.stderr)
        self.assertFalse(pending.exists())
        verified = self.f.invoke('--verify-integration')
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        for _ in range(2):
            result = self.f.invoke()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.f.calls(['pr', 'merge'])), 1)
        intents = [json.loads(p.read_text()) for p in self.f.state_dir.glob('merge-unit-*.json')]
        self.assertEqual(len(intents), 2)
        self.assertIn(cancelled, intents)
        successor, = [i for i in intents if i['phase'] == 'receipt_recorded']
        self.assertNotEqual(successor['operation_id'], cancelled['operation_id'])
        self.assertEqual(successor['target_before_request'], target)

    def assert_uncommitted_refused(self, stage, exit_code):
        self.precondition.move_after_publication()
        self.interrupt(stage)
        result = self.f.invoke()
        self.f.assert_refused(result)
        self.assertEqual(result.returncode, exit_code, result.stdout + result.stderr)
        self.assertEqual(self.f.intent()['phase'], 'cancelled_before_request')
        pending, = self.f.state_dir.glob('*.cancellation-pending')
        original = json.loads(pending.read_text())
        self.assertFalse(pending.with_suffix('.cancellation-committed').exists())
        del self.f.env['PYTHONPATH']
        result = self.f.invoke('--verify-integration')
        self.f.assert_refused(result)
        self.assertIn('unresolved', result.stderr)
        self.assertEqual(self.f.intent(), original)
        self.assertFalse(pending.exists())

    def test_crash_before_commit_remains_unresolved(self):
        self.assert_uncommitted_refused('before-commit', 92)

    def test_commit_rename_failure_remains_unresolved(self):
        self.assert_uncommitted_refused('commit-rename-failure', 1)

    def test_visible_commit_after_its_fsync_failure_witnesses_durable_cancellation(self):
        self.precondition.move_after_publication()
        self.interrupt('commit-directory-failure')
        result = self.f.invoke()
        self.f.assert_refused(result)
        self.assertIn('injected commit directory fsync failure', result.stderr)
        cancelled = self.f.intent()
        pending, = self.f.state_dir.glob('*.cancellation-pending')
        self.assertEqual(json.loads(pending.with_suffix('.cancellation-committed').read_text()),
                         cancelled)
        del self.f.env['PYTHONPATH']
        result = self.f.invoke('--verify-integration')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.f.intent(), cancelled)
        self.assertFalse(pending.exists())
        self.assertEqual(self.f.calls(['pr', 'merge']), [])

    def test_committed_pending_dry_run_is_read_only(self):
        self.leave_committed_crash()
        before = {p.name: p.read_bytes() for p in self.f.state_dir.iterdir()}
        calls = self.f.calls()
        result = self.f.invoke('--dry-run', '--verify-integration')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.f.state_dir.iterdir()})
        self.assertEqual(self.f.calls(), calls)

    def test_mismatched_commit_cannot_resolve_pending_record(self):
        _, cancelled, pending = self.leave_committed_crash()
        marker = pending.with_suffix('.cancellation-committed')
        cases = [dict(cancelled, operation_id='f' * 64),
                 dict(cancelled, root=cancelled['root'] + '-other'),
                 dict(cancelled, phase='merge_requested'),
                 dict(cancelled, binding=dict(cancelled['binding'], pr=8)),
                 dict(cancelled, cancellation=dict(cancelled['cancellation'], reason='other'))]
        for invalid in cases:
            with self.subTest(invalid=invalid):
                marker.write_text(json.dumps(invalid))
                result = self.f.invoke('--verify-integration')
                self.f.assert_refused(result)
                self.assertIn('cancellation commit does not match', result.stderr)
                self.assertTrue(pending.exists())
                self.assertEqual(self.f.intent(), cancelled)

    def test_committed_marker_cannot_resolve_queued_successor(self):
        self.leave_committed_crash()
        verified = self.f.invoke('--verify-integration')
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        forge_path = Path(self.f.env['FORGE_STATE'])
        forge = json.loads(forge_path.read_text())
        forge['queued'] = True
        forge_path.write_text(json.dumps(forge))
        queued = self.f.invoke()
        self.assertNotEqual(queued.returncode, 0, queued.stdout + queued.stderr)
        self.assertIn('merge not observed', queued.stderr)
        result = self.f.invoke()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('unresolved outcome', result.stderr)
        self.assertEqual(len(self.f.calls(['pr', 'merge'])), 1)
        self.assertEqual(self.f.receipts(), [])


if __name__ == '__main__':
    unittest.main()
