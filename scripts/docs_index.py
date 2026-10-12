#!/usr/bin/env python3
"""Generate a documentation index from exactly six physical header lines.

The header is the status marker, Status, Date, Title, Purpose, then a blank
line, in that order. Title and Purpose are literal nonempty single-line text.
Only these six lines are decoded; the rest of each document is opaque.
Successors are relative .md paths to indexed documents within docs/.

Out of contract: link validity (ARC-1787), whether a status or purpose is
factually current, and documents in docs/probes/ and docs/research/.
"""
import argparse
import datetime
import re
import sys
from pathlib import Path, PureWindowsPath
from urllib.parse import quote, urlsplit


MARKER = '<!-- docs-truth:status -->'
DATE = re.compile(r'Date: ([0-9]{4}-[0-9]{2}-[0-9]{2})\Z')
INDEX_TITLE = 'Documentation index'
INDEX_INTRO = 'Browse the maintained references, historical records, and superseded plans in this repository.'
EXCLUDED = ('probes', 'research')


class DocsError(ValueError):
    pass


def refuse(path, line, reason):
    raise DocsError('{}:{}: {}'.format(path, line, reason))


def indexed_files(root):
    return sorted(p for p in (root / 'docs').rglob('*')
                  if p.is_file() and p.suffix.lower() == '.md'
                  and p.relative_to(root / 'docs').parts[0] not in EXCLUDED)


def metadata(path):
    # Binary readline avoids decoding any body bytes through text buffering.
    lines = []
    with path.open('rb') as source:
        for number in range(1, 7):
            raw = source.readline()
            if not raw:
                refuse(path, number, 'missing header line')
            if raw.endswith(b'\n'):
                raw = raw[:-1]
                if raw.endswith(b'\r'):
                    raw = raw[:-1]
            try:
                line = raw.decode('utf-8')
            except UnicodeDecodeError:
                refuse(path, number, 'header must be UTF-8')
            if '\r' in line:
                refuse(path, number, 'unexpected carriage return in header')
            lines.append(line)
    if lines[0] != MARKER:
        refuse(path, 1, 'expected exact status marker')
    successor = None
    if lines[1] in ('Status: live', 'Status: historical'):
        status = lines[1][8:]
    elif lines[1].startswith('Status: superseded by '):
        status = lines[1][8:]
        successor = lines[1][22:]
    else:
        refuse(path, 2, 'expected Status: live, historical, or superseded by <path>')
    date = DATE.fullmatch(lines[2])
    if not date:
        refuse(path, 3, 'expected Date: YYYY-MM-DD')
    try:
        datetime.date.fromisoformat(date[1])
    except ValueError:
        refuse(path, 3, 'invalid calendar date')
    values = []
    for number, prefix in ((4, 'Title: '), (5, 'Purpose: ')):
        line = lines[number - 1]
        if not line.startswith(prefix) or not line[len(prefix):].strip():
            refuse(path, number, 'expected ' + prefix + '<nonempty text>')
        values.append(line[len(prefix):])
    if lines[5] != '':
        refuse(path, 6, 'expected blank line')
    return dict(path=path, title=values[0], status=status, date=date[1],
                purpose=values[1], successor=successor)


def validate_successor(record, files, root):
    successor = record['successor']
    if successor is None:
        return
    path = record['path']
    if not successor or successor != successor.strip():
        refuse(path, 2, 'successor must be nonempty and unpadded')
    # URLs, fragments and absolute paths are not document-relative paths.
    try:
        parsed = urlsplit(successor)
    except ValueError:
        refuse(path, 2, 'successor must not be a malformed URL')
    if (parsed.scheme or parsed.netloc or '#' in successor or '?' in successor
            or Path(successor).is_absolute() or PureWindowsPath(successor).drive
            or successor.startswith('\\') or not successor.endswith('.md')):
        refuse(path, 2, 'successor must be a relative .md path without URL or fragment')
    docs = root / 'docs'
    # Resolve .. lexically first, refusing a traversal outside docs even if it
    # subsequently re-enters. Internal spaces remain exactly as supplied.
    parts = list(path.parent.relative_to(docs).parts)
    for part in Path(successor).parts:
        if part == '..':
            if not parts:
                refuse(path, 2, 'successor escapes docs directory')
            parts.pop()
        else:
            parts.append(part)
    target = path.parent / successor
    try:
        resolved = target.resolve()
        relative = resolved.relative_to(docs.resolve())
    except (ValueError, RuntimeError, OSError):
        refuse(path, 2, 'successor escapes docs directory or cannot be resolved')
    if (not parts or parts[0] in EXCLUDED or not relative.parts
            or relative.parts[0] in EXCLUDED
            or resolved not in {p.resolve() for p in files}):
        refuse(path, 2, 'successor must name an existing indexed document')
    if resolved == path.resolve():
        refuse(path, 2, 'successor must not reference itself')


def collect(root):
    files = indexed_files(root)
    records = [metadata(p) for p in files]
    for record in records:
        validate_successor(record, files, root)
    return records


def cell(text):
    # Encode table/inline syntax, without interpreting any source Markdown.
    return ''.join('&#{};'.format(ord(char)) if char in '&<>\\|[]*_`~!'
                   else char for char in text)


def render(records, root):
    records = [r for r in records if r['path'] != root / 'docs/README.md']
    date = max((r['date'] for r in records), default='2026-10-11')
    lines = [MARKER, 'Status: live', 'Date: ' + date,
             'Title: ' + INDEX_TITLE, 'Purpose: ' + INDEX_INTRO, '',
             '# ' + INDEX_TITLE, '', INDEX_INTRO, '',
             '<!-- Generated by scripts/docs_index.py; do not edit by hand. -->', '',
             'Regenerate with `python3 scripts/docs_index.py write` from the repository root.', '',
             'Every indexed document starts with six physical lines: the exact',
             '`<!-- docs-truth:status -->` marker, `Status: live`, `Status: historical`,',
             'or `Status: superseded by <path>` on line 2, `Date: YYYY-MM-DD`,',
             '`Title: <text>`, `Purpose: <text>`, then a blank line.',
             'Title and Purpose are literal nonempty single-line text; the body is opaque.',
             'Successors resolve from the source document to an existing indexed `.md`',
             'document within `docs/`; internal spaces are allowed.',
             'Dates record status classification, not renewed measurement or authorization.',
             '`live` means a maintained reference or a plan with remaining work;',
             '`historical` preserves completed work, observations, or rejected proposals.',
             'Research and probe documents are excluded from metadata and this index.',
             'Link validity (ARC-1787) and factual currency of status or purpose are not checked.', '',
             '| Document | Status | Date | Purpose |', '| --- | --- | --- | --- |']
    for record in records:
        name = record['path'].relative_to(root / 'docs').as_posix()
        lines.append('| [{}](<{}>) | {} | {} | {} |'.format(
            cell(record['title']), quote(name, safe='/'), cell(record['status']),
            record['date'], cell(record['purpose'])))
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('write',))
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        records = collect(root)
        expected = render(records, root)
        (root / 'docs/README.md').write_text(expected, encoding='utf-8')
    except (DocsError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print('Documentation index generated from six-line headers.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
