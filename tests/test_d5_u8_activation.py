"""U8 cutover policy and real default wiring; external services are never used."""
from contextlib import nullcontext
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.test_d5_u4_1_active_surface import SurfaceFixture, S_ENTRIES, M_ENTRIES
from tests.test_d5_u4_0b1_issuance import no_effects
from tests import test_d5_lease as lease_fixture
from tests.fixtures.d5.ready import ready, ready_installation
import d5_activation as activation
import d5_authority as authority
import d5_authority_store as store
import d5_lease as lease
import d5_registry as registry
import merge_unit


ROOT = Path(__file__).resolve().parents[1]
MANDATE = ROOT / 'docs/orchestrator-mandate.md'
BASELINE = ROOT / 'tests/fixtures/d5/u8-mandate-baseline.json'


def section(text, heading):
 lines = text.splitlines()
 candidates = [i for i, line in enumerate(lines)
    if line.strip() == '## ' + heading]
 assert len(candidates) == 1, 'ambiguous heading: ' + heading
 start = candidates[0]
 assert lines[start] == '## ' + heading, 'noncanonical heading'
 stop = next((i for i in range(start + 1, len(lines))
    if re.match(r'^##\s', lines[i])), len(lines))
 return lines[start + 1:stop]


def preserve(text):
 baseline = json.loads(BASELINE.read_text())['sections']
 for heading, expected in baseline.items():
  actual = iter(section(text, heading))
  for line in expected:
   assert any(candidate == line for candidate in actual), (heading, line)


class TestU8Policy(unittest.TestCase):
 def test_M3_preserves_grants_bounds_stops_and_judgment(self):
  text = MANDATE.read_text()
  preserve(text)
  lines = [line for line in section(text, 'Adjudication policy') if line.strip()]
  self.assertEqual(hashlib.sha256('\n'.join(lines).encode()).hexdigest(),
      json.loads(BASELINE.read_text())['adjudication_sha256'])

 def test_M3_delete_old_bound_and_duplicate_heading_mutations(self):
  text = MANDATE.read_text()
  baseline = json.loads(BASELINE.read_text())['sections']
  for heading, lines in baseline.items():
   for line in lines:
    with self.subTest(heading=heading, deleted=line), self.assertRaises(AssertionError):
     preserve(text.replace(line, '', 1))
   with self.subTest(heading=heading), self.assertRaises(AssertionError):
    preserve(text + '\n## ' + heading + '  \n')

 def test_M4_normative_owner_fixed_mechanisms(self):
  text = MANDATE.read_text()
  for clause in ('native `CLAUDE_CODE_SESSION_ID`',
     "ancestor's PID and kernel start identity",
     'only after verified predecessor death',
     'There is no\nsupervisor wrapper',
     'issue time < expiry <= issue time + 24 hours',
     'The session cap is exactly one',
     'D5-OD1 means no new outward-action admissions',
     'Already-submitted effects may finish',
     'restoring old\nbytes does not resurrect it',
     'Only the OWNER merges',
     'P1 is a hard precondition',
     'before running linear_sync drain'):
   with self.subTest(clause=clause):
    self.assertIn(clause, text)


class TestU8Wiring(unittest.TestCase):
 def test_M6_positive_submission_uses_production_activation(self):
  from tests import test_d5_submission as t
  fixture = t.TestSubmission()
  self.addCleanup(fixture.doCleanups)
  fixture.setUp()
  # Real activation; synthetic clock, identity and submission.
  with nullcontext():
   self.assertIs(registry.d5_active(fixture.state_dir), True)
   result = fixture.submit()
  self.assertEqual(result.outcome, t.OK)
  self.assertEqual([call[0] for call in fixture.boundary.calls], ['start', 'finish'])

 def test_M1_M6_default_production_guards_refuse_before_effects(self):
  self.assertIs(merge_unit.merge_launch_admitted(), False)
  for tool, names in (('swarm', S_ENTRIES), ('operator', M_ENTRIES)):
   for name in names:
    with self.subTest(tool=tool, entry=name), tempfile.TemporaryDirectory() as directory:
     fixture = SurfaceFixture(directory)
     ready_installation(directory, state_dir=fixture.state_dir)
     self.assertIs(activation.d5_active(fixture.state_dir), True)
     self.assertIs(registry.d5_active(fixture.state_dir), True)
     with ExitStack() as stack:
      call = fixture.case(stack, tool, name)
      out = stack.enter_context(redirect_stdout(io.StringIO()))
      err = stack.enter_context(redirect_stderr(io.StringIO()))
      with no_effects() as effects:
       try:
        result = call()
       except (Exception, SystemExit) as exc:
        result = str(exc)
      self.assertEqual(effects, ([], []))
      self.assertIn('D5 active:', str(result) + out.getvalue() + err.getvalue())

 def test_M6_direct_roots_use_production_activation(self):
  from tests.test_d5_containment import cli_probe
  for tool, commands in {'unit': ('allocate', 'bind', 'check'),
       'remote_verify': ('worker', 'supervise')}.items():
   for command in commands:
    with self.subTest(tool=tool, command=command):
     result = cli_probe(tool, command, None)
     self.assertIn('D5 active:', str(result['outcome']))
     self.assertEqual(result['outcome'][0], 'SystemExit')
     for channel in ('launches', 'writes', 'transports'):
      self.assertEqual(result[channel], [], result)
     self.assertTrue(result['protected_unchanged'])

 def test_M6_outbox_helpers_and_merge_receipt_use_production_activation(self):
  from tests import test_d5_u6_tracker_reject as t
  import swarm_evidence
  for route in t.ENTRIES:
   with self.subTest(route=route), tempfile.TemporaryDirectory() as tmp:
    case = t.OutboxFixture(tmp)
    ready_installation(tmp, state_dir=case.state_dir)
    before = case.tree()
    with no_effects() as effects:
     with self.assertRaisesRegex(t.SO.OutboxError, 'D5 active:'):
      case.call(route)
     self.assertEqual(effects, ([], []))
    self.assertEqual(case.tree(), before)
  with tempfile.TemporaryDirectory() as tmp:
   state_dir = ready_installation(tmp)['state_dir']
   with no_effects() as effects, self.assertRaisesRegex(t.SO.OutboxError, 'D5 active:'):
    swarm_evidence.admit_merge(state_dir, 'u', 'a' * 40)
   self.assertEqual(effects, ([], []))











 def test_M5_replacement_needs_fresh_confirmation(self):
  fixture = lease_fixture.TestD5Lease()
  self.addCleanup(fixture.doCleanups)
  fixture.setUp()
  entry = ready(fixture, fixture.repo, 'p', fixture.state)
  first = fixture.grant()
  before = fixture.tree()
  for changes in ({}, {'expires_at': 201},
      {'owner_response': 'copied receipt with changed words'}):
   with self.subTest(changes=changes), self.assertRaisesRegex(
     authority.AuthorityError, 'fresh confirmation'):
    fixture.grant(**changes)
   self.assertEqual(fixture.tree(), before)
  second = fixture.grant(confirmed_at=102, expires_at=201,
       owner_response='Fresh confirmation\n')
  self.assertNotEqual(first['grant_id'], second['grant_id'])
  self.assertEqual(store.read(Path(entry['record_dir'])), second)
  self.assertEqual(second['confirmation']['owner_response'], 'Fresh confirmation\n')

 def test_M5_no_grant_from_conversational_receipt(self):
  fixture = lease_fixture.TestD5Lease()
  self.addCleanup(fixture.doCleanups)
  fixture.setUp()
  entry = ready(fixture, fixture.repo, 'p', fixture.state)
  directory = Path(entry['record_dir'])
  # Required fields reach the real API/schema before installation.
  for missing in ('expires_at', 'owner_response', 'displayed_bounds'):
   args = dict(fixture.args)
   del args[missing]
   with self.subTest(missing=missing), self.assertRaises(TypeError):
    lease.grant(fixture.repo, 'p', fixture.state, **args)
   self.assertFalse((directory / store.RECORD_NAME).exists())
  for changes in ({'owner_response': ''}, {'displayed_bounds': ''}, {'owner_response': ' \t\n'},
      {'displayed_bounds': ' \t\n'},
      {'expires_at': 100 + 86401}):
   with self.subTest(changes=changes), self.assertRaises(authority.AuthorityError):
    fixture.grant(**changes)
   self.assertFalse((directory / store.RECORD_NAME).exists())
  record = fixture.grant(expires_at=100 + 86400)
  self.assertEqual(store.read(directory), record)
  self.assertEqual(record['session_cap'], 1)
  self.assertEqual(record['confirmation']['owner_response'], fixture.args['owner_response'])
  self.assertEqual(authority.evaluate(record, 150, b'old mandate')[0], 'mandate_changed')


if __name__ == '__main__':
 unittest.main()
