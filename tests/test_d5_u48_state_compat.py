"""U4.8: prepared state, unchanged legacy keys, and no active migration."""
from contextlib import nullcontext
import argparse
from contextlib import ExitStack
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from tests import test_merge_unit as legacy
from tests import test_d5_u44_result_consumption as consumption
from tests.test_d5_u45_retry_recovery import inactive_script_capture
from tests.test_d5_u4_0b1_issuance import no_effects
from tests.test_d5_u4_1_active_surface import SurfaceFixture
from tests.test_d5_u4_2_request_authority import tree_bytes
from tests.fixtures.d5.ready import ready
import d5_activation as activation
import d5_readiness as R
import merge_unit as M
import swarm as S

BASE = 'd48e6324aae6ace2a296152b4d6365a509d51aea'
FIXTURE = Path(__file__).parent / 'fixtures/d5/u48-inactive-base.json'


def migration_capture():
    """Real copy-only migration through S; no inherited executables available."""
    with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
        directory = Path(temp).resolve()
        tools = directory / 'tools'
        tools.mkdir()
        stack.enter_context(mock.patch.dict(os.environ, {'PATH': str(tools)}))
        stack.enter_context(nullcontext())
        stack.enter_context(mock.patch.object(S.CP, '_external_base', return_value=directory / 'external'))
        previous = os.getcwd()
        os.chdir(directory)
        stack.callback(os.chdir, previous)
        source = directory / '.swarm/state'
        source.mkdir(parents=True)
        runs = directory / '.swarm/runs/u/a1'
        runs.mkdir(parents=True)
        (runs / 'out').write_text('retained artifact\n')
        snapshot = dict(schema_version=1, units={'u': dict(state='DONE', attempt_dir=str(runs))})
        (source / S.STATE_FILE).write_text(json.dumps(snapshot))
        args = argparse.Namespace(state_dir=None, root=None)
        state_dir, root = S._prepare_command_paths(args, need_root=True)
        marker = json.loads((state_dir / 'legacy-migration.json').read_text())
        marker['copied_at'] = '<time>'
        captured = dict(marker=marker, state=json.loads((state_dir / S.STATE_FILE).read_text()),
            source=json.loads((source / S.STATE_FILE).read_text()),
            output=(root / 'u/a1/out').read_text(), source_output=(runs / 'out').read_text(),
            args=vars(args))
        return json.loads(json.dumps(captured).replace(str(state_dir.parent), '<project>')
                          .replace(str(directory), '<fixture>'))


def surface_capture():
    values = {}
    for tool, names in (('swarm', ('advance', '_prepare_command_paths', 'main')),
                        ('merge', ('current_intent', 'reconcile', 'main'))):
        for name in names:
            with tempfile.TemporaryDirectory() as temp:
                values[tool + '.' + name] = SurfaceFixture(temp).observe(tool, name, False)
    return values


def installation_capture(module=R):
    """Eligible inactive promotion: real registry, lease, writes and traversal."""
    c = legacy.TestMergeUnit()
    c.setUp()
    try:
        with ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, c.env))
            stack.enter_context(nullcontext())
            stack.enter_context(mock.patch.object(R.registry.CP, '_external_base',
                                                 return_value=c.directory / 'external'))
            entry = ready(c, c.repo, c.plan['name'], c.state_dir, promote=False)
            before = tree_bytes(c.state_dir)
            writes, launches = [], []
            write, popen = module.store.write_bytes, subprocess.Popen

            def writing(path, raw):
                writes.append([path.name, json.loads(raw)])
                return write(path, raw)

            def launching(argv, *args, **kwargs):
                launches.append(argv)
                return popen(argv, *args, **kwargs)

            with mock.patch.object(module.store, 'write_bytes', writing), \
                    mock.patch.object(subprocess, 'Popen', launching):
                value = module.promote(c.repo, c.plan['name'], c.state_dir)
                module.successor_gate(entry)
            after = tree_bytes(c.state_dir)
            result = dict(writes=writes, launches=launches,
                changed=sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k)))
            encoded = json.dumps(result, sort_keys=True)
            for key in ('registration_id', 'generation', 'repo_key', 'state_key', 'record_key'):
                encoded = encoded.replace(json.dumps(value[key]), json.dumps('<' + key + '>'))
            return json.loads(encoded.replace(value['record_dir'], '<record>')
                              .replace(str(c.directory), '<fixture>'))
    finally:
        c.doCleanups()


class TestInactive(unittest.TestCase):
    def test_base_and_inactive_effects_with_inherited_sinfo(self):
        data = json.loads(FIXTURE.read_text())
        # The consumer is pinned to the coordinator-assigned base, not a
        # predecessor unit's capture. No Git reads supply expected behavior.
        self.assertEqual(data['base'], BASE)
        with tempfile.TemporaryDirectory() as temp:
            fake = Path(temp) / 'sinfo'
            fake.write_text('#!/bin/sh\nexit 99\n')
            fake.chmod(0o755)
            with mock.patch.dict(os.environ, {'PATH': temp}):
                self.assertEqual(surface_capture(), data['surface'])
                self.assertEqual(migration_capture(), data['migration'])

    def test_inactive_operator_authority_and_reconciliation(self):
        data = json.loads(FIXTURE.read_text())
        self.assertEqual(data['base'], BASE)
        self.assertEqual(inactive_script_capture(), data['operator'])

    def test_inactive_eligible_installer_effects(self):
        data = json.loads(FIXTURE.read_text())
        self.assertEqual(data['base'], BASE)
        self.assertEqual(installation_capture(), data['installation'])


class TestPreparedState(unittest.TestCase):
    def setUp(self):
        self.c = legacy.TestMergeUnit()
        self.c.prepare_active_fixture()
        self.addCleanup(self.c.doCleanups)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.dict(os.environ, self.c.env))
        self.stack.enter_context(mock.patch.object(R.registry.CP, '_external_base',
                                                 return_value=self.c.directory / 'external'))
        self.entry = ready(self, self.c.repo, self.c.plan['name'], self.c.state_dir)
        self.stack.enter_context(nullcontext())
        self.stack.enter_context(mock.patch.object(M, 'merge_launch_admitted', return_value=True))
        self.args = argparse.Namespace(state_dir=str(self.c.state_dir), root=str(self.c.root),
            unit='u', pr=7, verify_integration=False, abandon_intent=None,
            retrieve_remote_evidence=False, remote_recovery_attestation=None)

    def put(self, snapshot):
        (self.c.state_dir / S.STATE_FILE).write_text(json.dumps(snapshot))

    def refused_without_effects(self, call, message):
        before = tree_bytes(self.c.state_dir)
        with no_effects() as effects, self.assertRaisesRegex((ValueError, SystemExit), message):
            call()
        self.assertEqual(effects, ([], []))
        self.assertEqual(tree_bytes(self.c.state_dir), before)

    def test_ineligible_state_cannot_publish_readiness_or_enter_merge(self):
        variants = [dict(self.c.state, schema_version=0), dict(self.c.state, epoch=1)]
        for phase in ('RUNNING', 'SUBMITTED', 'ALLOCATED', 'unknown'):
            bad = copy.deepcopy(self.c.state)
            bad['units']['u']['state'] = phase
            variants.append(bad)
        for field, value in (('job_id', 'dry-123'), ('state', None)):
            bad = copy.deepcopy(self.c.state)
            bad['units']['u'][field] = value
            variants.append(bad)
        for bad in variants:
            with self.subTest(snapshot=bad):
                self.put(bad)
                for call in (lambda: R.promote(self.c.repo, self.c.plan['name'], self.c.state_dir),
                             lambda: M.authority(self.args, self.c.plan),
                             lambda: M._d5_merge_entry(self.args)):
                    self.refused_without_effects(call, 'prepared state')

    def test_preflight_declares_and_reads_eligible_states_without_rewriting(self):
        self.assertEqual(R.PREPARED_UNIT_STATES, (None, 'READY_FOR_PR', 'DONE', 'ABANDONED', 'SUPERSEDED'))
        for phase in R.PREPARED_UNIT_STATES:
            snapshot = copy.deepcopy(self.c.state)
            snapshot['units']['u']['state'] = phase
            if phase is None:
                snapshot['units']['u'].update(attempt_dir=None, job_id=None)
            self.put(snapshot)
            before = tree_bytes(self.c.state_dir)
            with no_effects() as effects:
                self.assertEqual(R.prepared_state(self.c.state_dir), snapshot)
            self.assertEqual(effects, ([], []))
            self.assertEqual(tree_bytes(self.c.state_dir), before)
        (self.c.state_dir / S.STATE_FILE).unlink()
        with no_effects():
            self.assertIsNone(R.prepared_state(self.c.state_dir))

    def test_legacy_operation_key_stable_across_epoch_and_d5_metadata(self):
        first = M.authority(self.args, self.c.plan)[2]
        expected = hashlib.sha256(json.dumps(first, sort_keys=True).encode()).hexdigest()
        (self.c.state_dir / S.STATE_EPOCH_FILE).write_text('{"epoch": 42}')
        snapshot = copy.deepcopy(self.c.state)
        snapshot['d5'] = {'installation': R.require(self.c.state_dir), 'admitted': True}
        self.put(snapshot)
        second = M.authority(self.args, self.c.plan)[2]
        self.assertEqual(first, second)
        self.assertEqual(set(second), {'unit', 'attempt', 'head', 'repo', 'target', 'pr'})
        operation, _, _ = M.current_intent(self.c.state_dir, second, str(self.c.root), repair=False)
        self.assertEqual(operation, expected)

    def test_active_normalization_and_migration_refuse_even_with_readiness(self):
        for phase, job in (('DONE', 'finished-agent'), ('RUNNING', 'dry-123')):
            snapshot = copy.deepcopy(self.c.state)
            snapshot['units']['u'].update(state=phase, job_id=job)
            before = copy.deepcopy(snapshot)
            self.refused_without_effects(lambda: S.advance(self.c.plan, snapshot,
                self.c.state_dir, self.c.root, False), 'D5 active')
            self.assertEqual(snapshot, before)
        self.args.state_dir = None
        self.args.root = None
        self.refused_without_effects(lambda: S._prepare_command_paths(self.args,
            self.c.plan, need_root=True), 'D5 active')

    def test_premature_activation_authority_launches_and_writes_nothing(self):
        (self.c.state_dir / R.MARKER).unlink()
        for call in (lambda: M.authority(self.args, self.c.plan),
                     lambda: M._d5_merge_entry(self.args)):
            self.refused_without_effects(call, 'readiness')
        for tool, names in (('swarm', ('advance', '_prepare_command_paths', 'main')),
                            ('merge', ('current_intent', 'reconcile', 'main'))):
            for name in names:
                with tempfile.TemporaryDirectory() as temp:
                    result = SurfaceFixture(temp).observe(tool, name, True)
                    self.assertEqual(result['launches'], [])
                    self.assertEqual(result['writes'], [])
                    self.assertEqual(result['files_changed'], [])


class TestHistoricalIntent(unittest.TestCase):
    def setUp(self):
        self.operator = consumption.OperatorCase()
        self.operator.setUp()
        self.addCleanup(self.operator.doCleanups)
        self.f = self.operator.f

    def invoke(self):
        return self.operator.invoke()

    def test_historical_intent_refuses_before_registration_or_forge_reads(self):
        historical = copy.deepcopy(self.f.intent)
        for metadata in (None, {}, {'state': 'Recorded', 'binding': {'installation':
                consumption.provider.readiness.require(self.f.c.state_dir)}}):
            row = dict(historical, phase='receipt_recorded')
            if metadata is not None:
                row['d5'] = metadata
            self.f.path.write_text(json.dumps(row))
            before = tree_bytes(self.f.c.state_dir)
            with no_effects() as effects:
                code, output = self.invoke()
            self.assertEqual(code, 1, output)
            self.assertEqual(effects, ([], []))
            self.assertEqual(tree_bytes(self.f.c.state_dir), before)
            # Historical audit traversal remains usable by the installer.
            with no_effects():
                R.successor_gate(self.f.c.entry)


if __name__ == '__main__':
    unittest.main()
