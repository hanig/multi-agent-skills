"""Pinned U4 inventory evidence, not an effect-enforcement or call-graph proof."""
import ast
import copy
from functools import lru_cache
import hashlib
import io
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

BASE = '276e76af05a83846ea9c45ae7d030cf11a2e4e94'
ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'tests/d5_u4_effect_manifest.md'
ALIASES = dict(S='swarm', SS='swarm_state', SW='swarm_workspaces',
    SD='swarm_dispatch', SP='swarm_preflight', SO='swarm_outbox',
    SC='swarm_continue', SCL='swarm_code_launch', M='merge_unit',
    V='verify', RV='remote_verify', LS='linear_sync', LA='linear_api',
    DC='drain_contract')
CITATION = re.compile(r'(?<![\w/])([\w.]+):([\w.]+):([0-9]+(?:,[0-9]+)*)')
# Selected sinks plus predecessor regressions; not general Python data flow.
SINKS = set(('mkdtemp mkdir rmtree copyfile chmod symlink_to Popen killpg '
             'write write_text write_bytes unlink rmdir replace link fsync flock '
             'publish publish_cleanup publish_launch_witness _command _git '
             '_isolated_git runner remote_call _observe_execution run_pinned '
             'make_bundle output_evidence TemporaryFile TemporaryDirectory run').split())

# Construct tokens in sorted citation order, cross-checked with the frozen set.
# C.name is a call; D.name a function definition; other tokens are AST classes.
TOKENS = """
C.mkdir C.symlink_to*2 C.rmtree C._copy_tree*2 C.write_text C.read C.type C.ProcessIdentity C.time
C.evaluate If C.type C.drain C._Completion C._refusals C.drain C.complete C._classify C.revoke
C.claim_custody C.relinquish C.submit D.complete C.drain Assign C._grant_locked C.operation
C.frozenset If C._prior C.Popen*3 Return C.sibling_skill_root C.insert C.pin C._prior
C.AuthorityError C.dup C.start C._validate_locked C.prepare C._validate_locked C.start C.bind
C.finish C.complete If C.close C._raise_as_contract_error*2 C.request C.transport C.urlopen
C.read_comment C.query C.read_comment C.confirm_comment C.reconcile_issue Assign C.fail Continue If
C.record_drain_receipt C.mkdir C.open C.flock C.pages_of C.query C.read_drain_issue
C.TemporaryDirectory C.write_text*2 C.run C.durable_write*2 D.cancel_before_request
C.durable_write*4 C.unlink D.current_intent C.durable_write C.unlink C.durable_write C.unlink
C.durable_write C.acquire_lease C.release_lease C.run C.print_tracker_drain C.print_tracker_audit
D.print_tracker_audit C.run Return C.run*3 C.observed_target C.run C.observed_target C.durable_write
C.observed_target C.run*3 C.durable_write C.run C.save_state C.durable_write
C.run_merge_preconditions C.acquire_lease C.run C._fsync_append C.acknowledge C.release_lease
C.runner C.run C._isolated_git C.binding_lock C.publish C.mkdir C.flock D.cleanup C.flock C.len
C.publish_cleanup C._command C.publish_cleanup C.rmtree Assign C.all D.cleanup_slurm_locked
C.flock*2 C.len C.publish_cleanup C._command C.publish_cleanup*2 C.rmtree C.publish_cleanup C.unlink
C._command D.finish_empty_stage C.flock C.rmdir C.remote_call C.publish C._git*3 Assign*2 C.write
C.fsync C.link C.replace C.fsync C.unlink C.publish C.mkdir C.publish C.fsync C.call C.publish
C.run*2 D.run_remote C.binding_lock C.publish Assign*2 C.publish*2 C.TemporaryDirectory
C.make_bundle C.write_text C.copyfile C.TemporaryFile C.publish C.publish_launch_witness C.run
C.retrieve_records C.remote_call C.publish*2 Assign C.publish Assign C.remote_call C.publish*3
C.cleanup_completing_window C.retrieve_records C.publish Assign C.remote_call C.publish C._command
D.supervise C.write_text C._command C.write_text D.worker C.mkdir*2 C._git*5 C.write_bytes
C.run_pinned C.publish*4 C.SkillPathError C.resolve C.emit_intent C.migrate_legacy_defaults
C.save_state*2 C.Popen C.save_state*2 C._capture_code_launch C._write_launch_record C.save_state*2
C._isolation_submission C._clear_isolation_marker C.save_state C.write_json C.write_text C.run
C.strip C.open C.Popen C.write_json Return C._git C.save_state*2 C._create_code_worktree
C.save_state*3 C.allocate C.save_state C.remove C.populate C.run C.save_state*6 C.str C.save_state
C._archive_code_worktree*2 C._cleanup_codex_home C.save_state*4 C._recover_code_launch
C.save_state*2 C._bind C.save_state*3 C._recover_code_launch C.save_state*2 C._bind C.save_state*2
C._git C._submit C.save_state*2 C._bind C.save_state*2 C.renew_lease C.save_state*2 C._check*2
C.maybe_resume C.save_state C.admit_merge C._set_unit_state C.maybe_continue C.save_state*2
C._archive_code_worktree C.save_state*2 C._release_output_claims C.save_state C._allocate
C.save_state C._capture_artifact_basis C.save_state C._take_output_claims C._submit
C._release_output_claims C.save_state*2 C._note_claimed_attempt C._bind C.save_state*2
C.backfill_tracker_intents C.save_state C._emit_current_intent C.cmd_run C.continue_plan C.write*2
C._fsync_append C.acquire_lease C._refresh_code_terminal_watches C.save_state C.advance
C._start_code_terminal_watchers C.save_state C.release_lease C.acquire_lease C.get
C._has_bound_merge_evidence C.dict C.bump_lease_epoch C.save_state C._emit_current_intent
C.release_lease C._seed_reachability_problem C.run*2 C.save_state C.run C.save_state C._git C.write
C.replace C.unlink C._seed_reachability_problem*2 C.flock If C._append C.print Return C.run*2
C.unlink Return Assign D._check C.flock C._heal_jsonl_tail C.fsync C.mkdir C.touch C.flock
C._heal_jsonl_tail C.write C.fsync C._allocate_sequence C._fsync_append C._append_receipt_atomic
C.rmtree C.write_json C.rmtree C._git_push_destination C._git C.mkdir C.rename C.mkdir C.write_json
C._preserve C._save C.run C._save C._holds_the_path C.write_json C.remove C.run*3 C.mkdir
C.write_json C.event C._isolated_git C.rmtree C._isolated_git C.rmtree C._isolated_git C.rmtree*2
C.run*3 C.TemporaryFile C.run C.runner C.mkdtemp C.mkdir C.rmtree C._isolated_git C.rmtree
C._isolated_git C.rmtree*2 C.write C.rmtree C._isolated_git C.rmtree C.runner C.Popen C.killpg
C._candidate_checkout C.rmtree C._git C._isolated_git*3 C.mkdir C.mkdtemp C.write C._git
C.run_pinned C.rmtree C.mkdtemp C._git C.rmtree C.run_pinned C._git C.rmtree
D.run_merge_preconditions C.run_pinned C.output_evidence C.rmtree*2 D.run_pinned C.mkdtemp
C.copyfile C.chmod C.mkdir C.symlink_to C._observe_execution C.runner C.rmtree C._isolated_git*2
"""


def construct(node):
    if isinstance(node, ast.Call):
        f = node.func
        return 'C.' + (f.attr if isinstance(f, ast.Attribute) else
                       f.id if isinstance(f, ast.Name) else 'call')
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return 'D.' + node.name
    return type(node).__name__


@lru_cache(None)
def index(tree):
    functions = {}
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.setdefault(n.name, []).append(n)
    functions['module'] = [tree]
    spans = {}
    for owner, roots in functions.items():
        for root in roots:
            for n in ast.walk(root):
                if hasattr(n, 'lineno'):
                    spans.setdefault((owner, construct(n)), []).append((n.lineno, n.end_lineno))
    return functions, spans


def table_rows(text):
    return [s for s in text.splitlines() if re.match(r'^ {0,3}\|', s)]


def calls(tree):
    """Keep lexical owners, including nested functions."""
    def visit(node, owner='module'):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            owner = node.name
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else (
                node.func.id if isinstance(node.func, ast.Name) else '')
            yield owner, node.lineno, name
        for child in ast.iter_child_nodes(node):
            yield from visit(child, owner)
    return list(visit(tree))


class TestManifest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        found = subprocess.run(['git', '-C', str(ROOT), 'cat-file', '-e',
            BASE + '^{commit}'], capture_output=True, timeout=60)
        if found.returncode:
            raise AssertionError('Missing BASE %s; git fetch origin %s' % (BASE, BASE))
        raw = subprocess.run(['git', '-C', str(ROOT), 'archive', BASE, 'skills'],
            check=True, capture_output=True, timeout=60).stdout
        with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
            cls.base_files = {m.name: archive.extractfile(m).read()
                              for m in archive.getmembers() if m.isfile()}
        cls.trees = {Path(p).name: ast.parse(data) for p, data in cls.base_files.items()
                     if p.endswith('.py') and '/scripts/' in p}

    def citations(self, text):
        self.assertIn('Base **' + BASE + '**', text, 'unpinned manifest')
        result = set()
        for alias, owner, numbers in CITATION.findall(text):
            file = ALIASES.get(alias, alias)
            if not file.endswith('.py'):
                file += '.py'
            self.assertIn(file, self.trees, 'unknown source: ' + alias)
            tree = self.trees[file]
            nodes = index(tree)[0].get(owner.split('.')[-1], [])
            if owner == 'module':
                nodes = tree.body
            self.assertTrue(nodes, (alias, owner))
            for number in map(int, numbers.split(',')):
                self.assertTrue(any(n.lineno <= number <= n.end_lineno for n in nodes),
                                'stale citation: %s:%s:%s' % (alias, owner, number))
                result.add((file, owner, number))
        self.assertTrue(result)
        return result

    def coverage(self, text):
        rows = '\n'.join(table_rows(text))
        cited = self.citations('Base **' + BASE + '**\n' + rows)
        owners = {
            'S': set(('advance _submit cmd_run cmd_advance cmd_continue cmd_stop_unit '
                      'cmd_merge cmd_watch_code_terminal _start_code_terminal_watchers').split()),
            'M': set(('main current_intent abandon cancel_before_request reconcile '
                      'verify_integration print_tracker_drain').split())}
        sinks = SINKS | set(('save_state _bind _allocate _archive_code_worktree '
            'maybe_resume _capture_artifact_basis acquire_lease release_lease '
            '_fsync_append durable_write acknowledge').split())
        for alias in ('V', 'RV', 'S', 'M'):
            file = ALIASES[alias] + '.py'
            for owner, line, name in calls(self.trees[file]):
                if name in sinks and (alias not in owners or owner in owners[alias]):
                    self.assertIn((file, owner, line), cited,
                                  'unclassified sink: %s:%s:%s %s' % (alias, owner, line, name))
        for owner, name in (('confirm_comment', 'query'), ('reconcile_issue', 'query'),
                            ('record_drain_receipt', 'run'), ('project_lock', 'flock'),
                            ('drain_locked', 'record_drain_receipt')):
            nodes = [(o, n) for o, n, c in calls(self.trees['linear_sync.py'])
                     if o == owner and c == name]
            self.assertTrue(nodes)
            for o, n in nodes:
                self.assertIn(('linear_sync.py', o, n), cited)

    def semantics(self, sites, tokens=TOKENS):
        expected = []
        for word in tokens.split():
            name, _, count = word.partition('*')
            expected.extend([name] * int(count or 1))
        self.assertEqual(len(sites), len(expected), 'construct coverage drift')
        errors = []
        for (file, owner, line), name in zip(sorted(sites), expected):
            spans = index(self.trees[file])[1].get((owner.split('.')[-1], name), [])
            if not any(start <= line <= end for start, end in spans):
                errors.append('%s:%s:%s expects %s; BASE spans %s' % (
                    file, owner, line, name, spans))
        self.assertFalse(errors, '\n'.join(errors))

    def manifest(self, text):
        sites = self.citations(text)
        self.assertEqual(hashlib.sha256(repr(sorted(sites)).encode()).hexdigest(),
            'f3174e9e753a31e4d5171d04566f42cc5e11f0a93b7f810f095b24b586f53740', 'citation set drift')
        self.semantics(sites)
        self.coverage(text)

    def test_manifest_citations_and_coverage(self):
        self.manifest(MANIFEST.read_text())

    def test_citation_drift(self):
        text = MANIFEST.read_text()
        with self.assertRaisesRegex(AssertionError, 'citation set drift'):
            self.manifest(text.replace('S:_submit:1564,1569,1572', 'S:_submit:1564,1569,1573'))
        with self.assertRaisesRegex(AssertionError, 'unpinned'):
            self.citations(text.replace(BASE, 'HEAD'))
        with self.assertRaisesRegex(AssertionError, 'stale citation'):
            self.citations(text.replace('S:cmd_merge:4656', 'S:cmd_merge:999999'))
        # Valid line, wrong call: coverage must reject it too.
        with self.assertRaisesRegex(AssertionError, 'unclassified sink'):
            self.coverage(text.replace('V:_observe_execution:1016',
                                       'V:_observe_execution:1026'))

    def test_equivalent_citations(self):
        text = MANIFEST.read_text()
        for changed in (text + '\nS:cmd_run:3646\n', text.replace(
                'S:_submit:1564,1569,1572',
                'S:_submit:1564; S:_submit:1569; S:_submit:1572')):
            with self.subTest(changed=changed[-40:]):
                self.assertEqual(self.citations(text), self.citations(changed))
                self.manifest(changed)

    def test_coordinator_sink_omission(self):
        for file, owner in (('swarm.py', 'advance'), ('merge_unit.py', 'reconcile')):
            with self.subTest(file=file):
                self.trees = dict(type(self).trees)
                self.trees[file] = copy.deepcopy(self.trees[file])
                f = next(n for n in self.trees[file].body
                         if isinstance(n, ast.FunctionDef) and n.name == owner)
                extra = ast.parse('subprocess.Popen(["true"])').body[0]
                ast.increment_lineno(extra, f.end_lineno)
                f.end_lineno += 1
                f.body.append(extra)  # AST only; never launch the synthetic child.
                with self.assertRaisesRegex(AssertionError, 'unclassified sink'):
                    self.manifest(MANIFEST.read_text())
        self.trees = type(self).trees

    def test_cold_activation_import(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, data in self.base_files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            probe = root / 'probe.py'
            probe.write_text(PROBE)
            result = subprocess.run([sys.executable, '-I', '-S', '-B', str(probe), str(root)],
                cwd=str(root), env={'PATH': os.defpath, 'HOME': str(root),
                'PYTHONDONTWRITEBYTECODE': '1'}, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('inactive; no storage/process/lock effects', result.stdout)

    def test_indented_tables(self):
        text = MANIFEST.read_text()
        for spaces in range(1, 5):
            changed = '\n'.join(' ' * spaces + s if s.startswith('|') else s
                                for s in text.splitlines())
            if spaces < 4:
                self.manifest(changed)
            else:
                with self.assertRaises(AssertionError):
                    self.manifest(changed)

    def test_missing_base(self):
        with tempfile.TemporaryDirectory() as empty, patch(__name__ + '.ROOT', Path(empty)):
            result = unittest.TestResult()
            missing = type('MissingBase', (TestManifest,), {})
            unittest.TestSuite([missing('test_manifest_citations_and_coverage')]).run(result)
        self.assertEqual(result.testsRun, 0)
        self.assertEqual(len(result.errors), 1)
        message = result.errors[0][1]
        self.assertIn('git fetch origin ' + BASE, message)
        self.assertNotIn('CalledProcessError', message)

    def test_reviewed_rows(self):
        rows = table_rows(MANIFEST.read_text())
        self.assertTrue(any('M:verify_integration:775' in s for s in rows))
        self.assertEqual(sum('S:_submit:1564,1569,1572' in s for s in rows), 1)

    def test_inventory_regressions(self):
        rows = '\n'.join(table_rows(MANIFEST.read_text()))
        cited = self.citations('Base **' + BASE + '**\n' + rows)
        required = {
            'S.advance': '_bind renew_lease maybe_resume _capture_artifact_basis',
            'S.cmd_stop_unit': 'acquire_lease release_lease',
            'S.cmd_watch_code_terminal': 'run', 'S.cmd_merge': '_fsync_append',
            'M.verify_integration': 'acquire_lease release_lease'}
        for item, names in required.items():
            alias, owner = item.split('.')
            file = ALIASES[alias] + '.py'
            for call in names.split():
                nodes = [(o, n) for o, n, c in calls(self.trees[file])
                         if o == owner and c == call]
                self.assertTrue(nodes, item)
                for o, n in nodes:
                    self.assertIn((file, o, n), cited)


PROBE = '"""Cold pinned-source activation probe; ordinary import reads are permitted."""\nimport os\nfrom pathlib import Path\nimport sys\n\nroot = Path(sys.argv[1])\nsys.path.insert(0, str(root / \'skills/hanig-orchestrate/scripts\'))\nassert not any(n.startswith(\'d5_\') for n in sys.modules)\n\n\ndef audit(event, args):\n    if event == \'open\':\n        path, mode, flags = args\n        if flags & (os.O_CREAT | os.O_TRUNC | os.O_APPEND | os.O_WRONLY | os.O_RDWR):\n            raise AssertionError(\'activation write: \' + str(path))\n        if not isinstance(path, str) or not path.endswith((\'.py\', \'.pyc\')):\n            raise AssertionError(\'activation non-source read: \' + str(path))\n    if event.startswith((\'subprocess.\', \'socket.\')) or event in (\n            \'fcntl.flock\', \'fcntl.lockf\', \'os.mkdir\', \'os.rename\', \'os.remove\',\n            \'os.rmdir\', \'os.chmod\', \'os.link\', \'os.symlink\', \'os.system\'):\n        raise AssertionError(\'activation effect: \' + event)\n\n\ndef profile(frame, event, arg):\n    if event == \'call\' and frame.f_globals.get(\'__name__\') == \'d5_registry\':\n        if frame.f_code.co_name not in (\'<module>\', \'d5_active\'):\n            raise AssertionError(\'activation registry access: \' + frame.f_code.co_name)\n\n\nsys.addaudithook(audit)\nsys.setprofile(profile)\nimport d5_registry\nassert d5_registry.d5_active() is False\nsys.setprofile(None)\nprint(\'inactive; no storage/process/lock effects\')\n'

if __name__ == "__main__":
    unittest.main()
