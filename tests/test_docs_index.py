"""Exercise documentation status metadata and index generation."""
import contextlib
import importlib.util
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/docs_index.py'
SPEC = importlib.util.spec_from_file_location('docs_index', SCRIPT)
docs_index = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(docs_index)


def document(status='live', date='2026-10-11', title='Example', body='A useful purpose.'):
    return '{}\nStatus: {}\nDate: {}\n\n# {}\n\n{}\n'.format(
        docs_index.MARKER, status, date, title, body)


class TestRepositoryDocs(unittest.TestCase):
    def test_every_document_has_valid_metadata(self):
        records = docs_index.collect(ROOT)
        self.assertTrue(records)
        self.assertEqual(len(docs_index.indexed_files(ROOT)), len(records))

    def test_generated_index_matches_committed_file(self):
        expected = docs_index.render(docs_index.collect(ROOT), ROOT)
        self.assertEqual(expected, (ROOT / 'docs/README.md').read_text(encoding='utf-8'))


class TestDocsCommand(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'docs').mkdir()
        self.doc = self.root / 'docs/example.md'
        self.doc.write_text(document(), encoding='utf-8')
        self.assertEqual(0, self.run_command('write')[0])

    def run_command(self, command):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = docs_index.main([command, '--root', str(self.root)])
        return code, output.getvalue()

    def test_missing_status_is_refused_not_repaired_by_write(self):
        self.doc.write_text(document().replace('Status: live\n', ''), encoding='utf-8')
        code, output = self.run_command('write')
        self.assertEqual(1, code)
        self.assertIn('metadata', output)
        self.assertIn('example.md', output)

    def test_invalid_metadata_and_successors(self):
        for status, date in [('LIVE', '2026-10-11'), ('draft', '2026-10-11'),
                             ('live', '2026-02-30'), ('live', '2026-1-1'),
                             ('superseded by missing.md', '2026-10-11'),
                             ('superseded by example.md', '2026-10-11')]:
            with self.subTest(status=status, date=date):
                self.doc.write_text(document(status, date), encoding='utf-8')
                self.assertEqual(1, self.run_command('write')[0])

    def test_missing_title_and_purpose_fail(self):
        for text in [document().replace('# Example', 'Example'),
                     document(body='')]:
            self.doc.write_text(text, encoding='utf-8')
            self.assertEqual(1, self.run_command('write')[0])

    def test_nested_docs_and_only_top_level_exclusions(self):
        for relative in ['nested/new.md', 'nested/research/new.MD']:
            path = self.root / 'docs' / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# Missing metadata\n', encoding='utf-8')
            self.assertEqual(1, self.run_command('write')[0])
            path.write_text(document(), encoding='utf-8')
        for excluded in ['research', 'probes']:
            path = self.root / 'docs' / excluded / 'new.md'
            path.parent.mkdir()
            path.write_text('# No metadata required\n', encoding='utf-8')
        self.assertEqual(0, self.run_command('write')[0])

    def test_changes_to_each_source_field_make_index_stale(self):
        for changed in [document(status='historical'), document(date='2026-10-10'),
                        document(title='New title'), document(body='New purpose.')]:
            with self.subTest(changed=changed):
                self.doc.write_text(changed, encoding='utf-8')
                expected = docs_index.render(docs_index.collect(self.root), self.root)
                self.assertNotEqual(expected, (self.root / 'docs/README.md').read_text(encoding='utf-8'))
                self.assertEqual(0, self.run_command('write')[0])
                self.assertEqual(expected, (self.root / 'docs/README.md').read_text(encoding='utf-8'))
                self.doc.write_text(document(), encoding='utf-8')
                self.assertEqual(0, self.run_command('write')[0])

    def test_regeneration_is_byte_stable_and_can_recreate_deleted_index(self):
        index = self.root / 'docs/README.md'
        before = index.read_bytes()
        index.unlink()
        self.assertEqual(0, self.run_command('write')[0])
        self.assertEqual(before, index.read_bytes())
        self.assertEqual(0, self.run_command('write')[0])
        self.assertEqual(before, index.read_bytes())

    def test_review_markdown_title_forms(self):
        for title in ['   # Example', 'Example\n=======']:
            with self.subTest(title=title):
                self.doc.write_text(document().replace('# Example', title), encoding='utf-8')
                self.assertEqual(0, self.run_command('write')[0])
                self.assertEqual('Example', docs_index.metadata(self.doc)['title'])

    def test_cli_runs_from_an_unrelated_directory(self):
        result = subprocess.run([sys.executable, str(SCRIPT), 'write', '--root', str(self.root)],
                                cwd=str(self.root.parent), capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == '__main__':
    unittest.main()
