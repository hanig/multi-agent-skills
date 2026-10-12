"""Exercise literal documentation headers and deterministic index generation."""
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


def document(status='live', date='2026-10-11', title='Example',
             purpose='A useful purpose.', body='# Example\n\nLater prose.'):
    return '{}\nStatus: {}\nDate: {}\nTitle: {}\nPurpose: {}\n\n{}\n'.format(
        docs_index.MARKER, status, date, title, purpose, body)


class TestRepositoryDocs(unittest.TestCase):
    def test_every_document_has_valid_metadata(self):
        records = docs_index.collect(ROOT)
        self.assertTrue(records)
        self.assertEqual(docs_index.indexed_files(ROOT), [r['path'] for r in records])

    def test_generated_index_matches_committed_file(self):
        expected = docs_index.render(docs_index.collect(ROOT), ROOT).encode('utf-8')
        self.assertEqual(expected, (ROOT / 'docs/README.md').read_bytes())


class TestDocsCommand(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / 'docs').mkdir()
        self.doc = self.root / 'docs/example.md'
        self.index = self.root / 'docs/README.md'
        self.doc.write_text(document(), encoding='utf-8')
        self.assertEqual(0, self.run_command()[0])

    def run_command(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = docs_index.main(['write', '--root', str(self.root)])
        return code, output.getvalue()

    def assert_refused(self, content, line, path=None):
        path = path or self.doc
        path.write_bytes(content.encode('utf-8') if isinstance(content, str) else content)
        before = self.index.read_bytes()
        stat = self.index.stat()
        code, output = self.run_command()
        self.assertEqual(1, code, output)
        self.assertTrue(output.startswith('{}:{}: '.format(path, line)), output)
        self.assertEqual(before, self.index.read_bytes())
        self.assertEqual((stat.st_mtime_ns, stat.st_ino),
                         (self.index.stat().st_mtime_ns, self.index.stat().st_ino))
        return output

    def test_missing_status_is_refused_not_repaired_by_write(self):
        self.assert_refused(document().replace('Status: live\n', ''), 2)

    def test_missing_duplicate_and_reordered_header_lines(self):
        lines = document().splitlines(keepends=True)
        for index in range(6):
            with self.subTest(missing=index + 1):
                self.assert_refused(''.join(lines[:index] + lines[index + 1:]), index + 1)
            with self.subTest(duplicate=index + 1):
                # A seventh line is body, not a duplicate header. Duplicate
                # each required field *within* the six-line header instead.
                if index < 5:
                    self.assert_refused(''.join(lines[:index] + [lines[index]] + lines[index:]), index + 2)
            with self.subTest(reordered=index + 1):
                swapped = lines[:]
                other = (index + 1) % 6
                swapped[index], swapped[other] = swapped[other], swapped[index]
                self.assert_refused(''.join(swapped), min(index, other) + 1)
        for present in range(6):
            with self.subTest(truncated_after=present):
                self.assert_refused(''.join(lines[:present]), present + 1)

    def test_malformed_header_lines(self):
        cases = {
            1: [' <!-- docs-truth:status -->', '<!-- docs-truth:status --> ',
                '<!-- docs-truth:historical -->', '\ufeff' + docs_index.MARKER],
            2: ['Status: LIVE', 'Status: draft', 'Status: live ', 'Status:live',
                ' Status: live', 'Status: superseded by'],
            3: ['Date: 2026-1-01', 'Date: 2026-01-1', 'Date: 2026-02-30',
                'Date: 2025-02-29', 'Date: 0000-01-01', 'Date: 2026-13-01',
                'Date: ２０２６-01-01', 'Date: 2026-10-11 ', 'Date:2026-10-11'],
            4: ['Title:', 'Title: ', 'Title: \t', 'title: Example', '# Example'],
            5: ['Purpose:', 'Purpose: ', 'Purpose: \t', 'purpose: Example', 'Paragraph.'],
            6: [' ', '\t', '# Heading', 'Purpose: duplicate', docs_index.MARKER],
        }
        for line, replacements in cases.items():
            for replacement in replacements:
                with self.subTest(line=line, replacement=replacement):
                    lines = document().splitlines(keepends=True)
                    lines[line - 1] = replacement + '\n'
                    self.assert_refused(''.join(lines), line)
        self.assert_refused(document().encode().replace(b'Title: Example', b'Title: \xff'), 4)
        self.assert_refused(document().replace('Title: Example', 'Title: one\rtwo'), 4)

    def test_missing_title_and_purpose_fail(self):
        self.assert_refused(document(title=''), 4)
        self.assert_refused(document(purpose=''), 5)

    def test_later_invalid_document_and_invalid_index_prevent_all_writes(self):
        late = self.root / 'docs/zzz.md'
        self.assert_refused(document(date='bad'), 3, late)
        late.unlink()
        self.assert_refused(document(title=''), 4, self.index)

    def test_body_is_opaque_to_title_purpose_and_every_rendered_value(self):
        before = self.index.read_bytes()
        bodies = [
            '', '# Fake title\n\nA different first paragraph.',
            '# Fake title\n\n<!-- first comment paragraph\n\nhidden purpose -->\n\nLater prose.',
            '# Fake title\n\n```text\nfirst code line\n\nsecond code line\n```\n\nLater prose.',
            '#\n\nAn empty heading and later prose.',
            '   # Indented heading\n\nLater prose.', 'Setext heading\n====\n\nLater prose.',
            'Status: historical\nDate: bad\nTitle: duplicate\nPurpose: duplicate\n\n',
            '[broken](missing.md)\n\n<!-- unclosed\n\n`unclosed',
        ]
        for body in bodies:
            with self.subTest(body=body):
                self.doc.write_text(document(body=body), encoding='utf-8')
                record = docs_index.metadata(self.doc)
                self.assertEqual('Example', record['title'])
                self.assertEqual('A useful purpose.', record['purpose'])
                self.assertEqual(0, self.run_command()[0])
                self.assertEqual(before, self.index.read_bytes())
        # Even non-UTF-8 bytes after the header are never decoded.
        self.doc.write_bytes(document(body='').encode() + b'\xff\xfe\x00\n')
        self.assertEqual(0, self.run_command()[0])
        self.assertEqual(before, self.index.read_bytes())

    def test_literal_fields_are_preserved_and_escaped_only_in_table(self):
        literal = '  **bold** _under_ `code` [link](url) <tag> &amp; | \\ ~ !  '
        self.doc.write_text(document(title=literal, purpose=literal), encoding='utf-8')
        record = docs_index.metadata(self.doc)
        self.assertEqual(literal, record['title'])
        self.assertEqual(literal, record['purpose'])
        self.assertEqual(0, self.run_command()[0])
        escaped = ('  &#42;&#42;bold&#42;&#42; &#95;under&#95; &#96;code&#96; '
                   '&#91;link&#93;(url) &#60;tag&#62; &#38;amp; &#124; &#92; &#126; &#33;  ')
        row = '| [{}](<example.md>) | live | 2026-10-11 | {} |'.format(escaped, escaped)
        self.assertIn(row, self.index.read_text())

    def test_successor_with_internal_spaces_and_relative_parent_succeeds(self):
        successor = self.root / 'docs/new plan.md'
        successor.write_text(document(title='New plan'), encoding='utf-8')
        self.doc.write_text(document(status='superseded by new plan.md'), encoding='utf-8')
        self.assertEqual(0, self.run_command()[0])
        self.assertEqual('new plan.md', docs_index.metadata(self.doc)['successor'])
        self.assertIn('superseded by new plan.md', self.index.read_text())
        self.assertIn('[New plan](<new%20plan.md>)', self.index.read_text())
        nested = self.root / 'docs/nested/old.md'
        nested.parent.mkdir()
        nested.write_text(document(status='superseded by ../new plan.md'), encoding='utf-8')
        self.assertEqual(0, self.run_command()[0])

    def test_uppercase_successor_suffix_is_refused_with_exact_diagnostic(self):
        successor = self.root / 'docs/new.MD'
        successor.write_text(document(title='Uppercase extension'), encoding='utf-8')
        output = self.assert_refused(document(status='superseded by new.MD'), 2)
        self.assertEqual(
            '{}:2: successor must be a relative .md path without URL or fragment\n'.format(self.doc),
            output)

    def test_each_refused_successor_path_class(self):
        for relative in ['new.md', 'probes/new.md', 'research/new.md', 'directory.md/file.md']:
            p = self.root / 'docs' / relative
            p.parent.mkdir(exist_ok=True)
            p.write_text(document(), encoding='utf-8')
        outside = self.root / 'outside.md'
        outside.write_text(document(), encoding='utf-8')
        cases = [
            '', ' ', ' new.md', 'new.md ', '\tnew.md', 'new.md\t',
            'https://example.org/new.md', 'file:new.md', '//example.org/new.md',
            'https://[broken/new.md', 'new.md#fragment', 'new.md#', '#new.md',
            'new.md?query.md', str(outside), '/new.md', 'C:/new.md', '\\new.md',
            '../outside.md', '../docs/new.md', 'sub/../../outside.md',
            'example.md', './example.md', 'sub/../example.md',
            'probes/new.md', 'research/new.md', 'sub/../research/new.md',
            'missing.md', 'new.txt', 'directory.md',
        ]
        for successor in cases:
            with self.subTest(successor=successor):
                self.assert_refused(document(status='superseded by ' + successor), 2)

    def test_successor_symlinks_cannot_escape_exclusions_or_reference_self(self):
        targets = [self.doc, self.root / 'outside.md', self.root / 'docs/research/hidden.md']
        targets[1].write_text(document(), encoding='utf-8')
        targets[2].parent.mkdir()
        targets[2].write_text(document(), encoding='utf-8')
        alias = self.root / 'docs/z-alias.md'
        for target in targets:
            with self.subTest(target=target):
                alias.symlink_to(target)
                try:
                    self.assert_refused(document(status='superseded by z-alias.md'), 2)
                finally:
                    alias.unlink()

    def test_successor_resolves_parent_after_symlink_before_deciding(self):
        outside = self.root / 'outside'
        (outside / 'child').mkdir(parents=True)
        (outside / 'new.md').write_text(document(), encoding='utf-8')
        (self.root / 'docs/new.md').write_text(document(), encoding='utf-8')
        (self.root / 'docs/jump').symlink_to(outside / 'child')
        self.assert_refused(document(status='superseded by jump/../new.md'), 2)

    def test_nested_docs_and_only_top_level_exclusions(self):
        for relative in ['nested/new.md', 'nested/research/new.MD']:
            path = self.root / 'docs' / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            self.assert_refused(document().replace(docs_index.MARKER, 'Wrong marker'), 1, path)
            path.write_text(document(), encoding='utf-8')
        for excluded in ['research', 'probes']:
            path = self.root / 'docs' / excluded / 'new.md'
            path.parent.mkdir()
            path.write_text('# No metadata required\n', encoding='utf-8')
        self.assertEqual(0, self.run_command()[0])
        paths = [r['path'] for r in docs_index.collect(self.root)]
        self.assertIn(self.root / 'docs/nested/research/new.MD', paths)
        self.assertNotIn(self.root / 'docs/research/new.md', paths)

    def test_changes_to_each_source_field_make_index_stale(self):
        successor = self.root / 'docs/new.md'
        successor.write_text(document(), encoding='utf-8')
        self.assertEqual(0, self.run_command()[0])
        for changed in [document(status='historical'), document(status='superseded by new.md'),
                        document(date='2026-10-10'), document(title='New title'),
                        document(purpose='New purpose.')]:
            with self.subTest(changed=changed):
                self.doc.write_text(changed, encoding='utf-8')
                expected = docs_index.render(docs_index.collect(self.root), self.root).encode('utf-8')
                self.assertNotEqual(expected, self.index.read_bytes())
                self.assertEqual(0, self.run_command()[0])
                self.assertEqual(expected, self.index.read_bytes())
                self.doc.write_text(document(), encoding='utf-8')
                self.assertEqual(0, self.run_command()[0])

    def test_regeneration_is_byte_stable_and_can_recreate_deleted_index(self):
        before = self.index.read_bytes()
        self.index.unlink()
        for _ in range(3):
            self.assertEqual(0, self.run_command()[0])
            self.assertEqual(before, self.index.read_bytes())
        record = docs_index.metadata(self.index)
        self.assertEqual('Documentation index', record['title'])
        self.assertEqual('live', record['status'])
        self.assertNotIn('](<README.md>)', self.index.read_text())

    def test_crlf_headers_and_leap_day_succeed(self):
        self.doc.write_bytes(document(date='2024-02-29').replace('\n', '\r\n').encode('utf-8'))
        self.assertEqual(0, self.run_command()[0])
        self.assertEqual('2024-02-29', docs_index.metadata(self.doc)['date'])

    def test_cli_runs_from_an_unrelated_directory(self):
        result = subprocess.run([sys.executable, str(SCRIPT), 'write', '--root', str(self.root)],
                                cwd=str(self.root.parent), capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == '__main__':
    unittest.main()
