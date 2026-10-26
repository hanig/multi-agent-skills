"""Limited tracked-entry drift detection, not repository-wide caller closure."""
import ast
import json
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'tests/fixtures/d5/integration-manifest.json'


def tracked(root):
    result = subprocess.run(['git', 'ls-files', '-z'], cwd=str(root),
        capture_output=True, check=True, timeout=30)
    return result.stdout.decode('utf-8').rstrip('\0').split('\0')


def candidates(root, paths):
    found = set()
    for name in paths:
        path = Path(root) / name
        if path.lstat().st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
            found.add(name)  # Includes symlinks via lstat, including dangling links.
        if path.suffix == '.py':
            try:
                tree = ast.parse(path.read_bytes(), filename=name)
            except (SyntaxError, ValueError) as exc:
                raise AssertionError('unparseable tracked Python: ' + name) from exc
            for node in ast.walk(tree):
                if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
                    continue
                test = node.test
                terms = [test.left] + test.comparators
                for left, op, right in zip(terms, test.ops, terms[1:]):
                    pair = (left, right)
                    if (isinstance(op, ast.Eq)
                            and any(isinstance(n, ast.Name) and n.id == '__name__' for n in pair)
                            and any(isinstance(n, ast.Constant) and n.value == '__main__' for n in pair)):
                        found.add(name)
    return found


def check_inventory(root, paths, manifest):
    admitted = {row['path'] for row in manifest['entrypoints']}
    denied = set(manifest['denied_entrypoints'])
    if admitted & denied:
        raise AssertionError('entry both admitted and denied')
    missing = candidates(root, paths) - admitted - denied
    if missing:
        raise AssertionError('unrecorded tracked entry: ' + ', '.join(sorted(missing)))


class TestEntrypointInventory(unittest.TestCase):
    def test_comparison_operator_belongs_to_the_main_name_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'import_only.py'
            manifest = {'entrypoints': [], 'denied_entrypoints': []}
            for expression in ('"__main__" != __name__ == "tests.fixtures.d5.import_only"',
                               '__name__ != "__main__" == "__main__"'):
                path.write_text('if ' + expression + ':\n    IMPORTED = True\n')
                path.chmod(0o644)
                check_inventory(tmp, [path.name], manifest)
            for expression in ('__name__ == "__main__"', '"__main__" == __name__',
                               '__name__ == "__main__" == "__main__"'):
                path.write_text('if ' + expression + ':\n    pass\n')
                with self.assertRaisesRegex(AssertionError, path.name):
                    check_inventory(tmp, [path.name], manifest)

    def test_tracked_entrypoints_recorded(self):
        manifest = json.loads(MANIFEST.read_text())['u47_containment']
        check_inventory(ROOT, tracked(ROOT), manifest)
        self.assertEqual(manifest['launchers'], [])

    def test_drift_mutations_fail_closed(self):
        for mutation in ('unrecorded-main', 'unrecorded-exec', 'unrecorded-symlink',
                         'syntax-error', 'deleted-record'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'new.py'
                path.write_text('if __name__ == "__main__":\n    pass\n')
                manifest = {'entrypoints': [], 'denied_entrypoints': []}
                if mutation == 'unrecorded-exec':
                    path.write_text('pass\n')
                    path.chmod(0o755)
                elif mutation == 'unrecorded-symlink':
                    path.unlink()
                    path = Path(tmp) / 'link'
                    path.symlink_to('absent')
                elif mutation == 'syntax-error':
                    path.write_text('if invalid syntax\n')
                    manifest['denied_entrypoints'] = [path.name]
                elif mutation == 'deleted-record':
                    manifest['denied_entrypoints'] = [path.name]
                    check_inventory(tmp, [path.name], manifest)
                    manifest['denied_entrypoints'].clear()
                with self.assertRaisesRegex(AssertionError, path.name):
                    check_inventory(tmp, [path.name], manifest)


if __name__ == '__main__':
    unittest.main()
