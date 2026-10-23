"""Pinned U4 inventory evidence, not an effect-enforcement or call-graph proof."""
import ast
import copy
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
            nodes = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef,
                     ast.AsyncFunctionDef)) and n.name == owner.split('.')[-1]]
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
        rows = '\n'.join(line for line in text.splitlines() if line.startswith('|'))
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

    def manifest(self, text):
        sites = self.citations(text)
        self.assertEqual(hashlib.sha256(repr(sorted(sites)).encode()).hexdigest(),
            '9de1e65e32aa3c8189fbeb301afcc34d0dd3b01cd554b6235491f4c35561c40e', 'citation set drift')
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
            probe = ROOT / 'tests/fixtures/d5/u4_activation_probe.py'
            result = subprocess.run([sys.executable, '-I', '-S', '-B', str(probe), str(root)],
                cwd=str(root), env={'PATH': os.defpath, 'HOME': str(root),
                'PYTHONDONTWRITEBYTECODE': '1'}, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('inactive; no storage/process/lock effects', result.stdout)

    def test_inventory_regressions(self):
        rows = '\n'.join(x for x in MANIFEST.read_text().splitlines() if x.startswith('|'))
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


if __name__ == "__main__":
    unittest.main()
