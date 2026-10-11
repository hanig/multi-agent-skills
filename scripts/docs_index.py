#!/usr/bin/env python3
"""Generate the documentation index and check relative Markdown destinations.

In docs/, README.md and CLAUDE.md, check that relative link destinations exist.
Recognized forms are exactly inline links [text](relative/path) and reference
definitions [label]: relative/path, including inside block quotes (leading >
markers are stripped before matching). Destinations may have a trailing
#fragment and an optional title; fragments are not checked. Existing directory
destinations are accepted, as in the repository's probes/ links.

Fenced code blocks and HTML comments are masked before matching. Inline code
spans use a paragraph-bounded regex that cannot cross a blank line. Comments
are matched only outside code spans and must end with -->. Absolute URLs,
mailto, bare URLs and absolute host paths are ignored. No network requests or
heading/HTML-id anchor validation are performed; this is not a full Markdown
renderer.
"""
import argparse
import datetime
import html
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit


MARKER = '<!-- docs-truth:status -->'
STATUS = re.compile(r'Status: (live|historical|superseded by ([^\s]+\.md))\Z')
DATE = re.compile(r'Date: (\d{4}-\d{2}-\d{2})\Z')
INDEX_INTRO = 'Browse the maintained references, historical records, and superseded plans in this repository.'


class DocsError(ValueError):
    pass


def markdown_files(root):
    return sorted(p for p in (root / 'docs').rglob('*')
                  if p.is_file() and p.suffix.lower() == '.md')


def indexed_files(root):
    return [p for p in markdown_files(root)
            if p.relative_to(root / 'docs').parts[0] not in ('probes', 'research')]


def metadata(path):
    text = path.read_text(encoding='utf-8')
    lines = text.splitlines()
    if len(lines) < 3 or lines[0] != MARKER:
        raise DocsError('{}: missing first-line status marker'.format(path))
    status = STATUS.fullmatch(lines[1])
    date = DATE.fullmatch(lines[2])
    if not status or not date:
        raise DocsError('{}: invalid status/date metadata'.format(path))
    try:
        datetime.date.fromisoformat(date[1])
    except ValueError:
        raise DocsError('{}: invalid ISO date'.format(path))
    title = next((heading for heading in headings(text) if heading[0] == 1), None)
    if not title:
        raise DocsError('{}: missing document title'.format(path))
    # The first prose paragraph after the title supplies the purpose. Skip
    # headings, comments and block quotes, which often carry old review notes.
    paragraphs = re.split(r'\n\s*\n', text[title[2]:])
    purpose = next((' '.join(p.split()) for p in paragraphs if p.strip()
                    and not p.lstrip().startswith(('#', '>', '<!--', '```', '~~~', '- ', '|'))), '')
    if not purpose:
        raise DocsError('{}: missing purpose paragraph'.format(path))
    successor = status[2]
    if successor:
        target = path.parent / successor
        if (urlsplit(successor).scheme or successor.startswith('/')
                or not target.is_file() or target.resolve() == path.resolve()):
            raise DocsError('{}: invalid successor {}'.format(path, successor))
    return dict(path=path, title=title[1], status=status[1], date=date[1],
                purpose=purpose, successor=successor)


def collect(root):
    return [metadata(p) for p in indexed_files(root)]


def blank(value):
    return ''.join('\n' if c == '\n' else ' ' for c in value)


def block_lines(text):
    """Yield source lines with block code masked; keep list continuations."""
    fence = None
    indents = []
    paragraph = False
    for line in text.splitlines(keepends=True):
        content = re.sub(r'^(?: {0,3}> ?)+', '', line).expandtabs(4)
        spaces = len(content) - len(content.lstrip(' '))
        if content.strip():
            while indents and spaces < indents[-1]:
                indents.pop()
        indent = indents[-1] if indents else 0
        residual = content[indent:]
        item = re.match(r' {0,3}(?:[-+*]|[0-9]{1,9}[.)]) +', residual)
        if item and not fence:
            indent += item.end()
            indents.append(indent)
            residual = content[indent:]
        token = re.match(r' {0,3}(`{3,}|~{3,})(.*)', residual)
        if fence:
            yield blank(line)
            if re.fullmatch(r' {0,3}' + re.escape(fence[0]) + '{' + str(fence[1]) + r',}\s*', residual):
                fence = None
            paragraph = False
        elif token and not (token[1][0] == '`' and '`' in token[2]):
            fence = (token[1][0], len(token[1]))
            yield blank(line)
            paragraph = False
        elif residual.startswith('    ') and not paragraph:
            yield blank(line)
        else:
            yield line
            paragraph = bool(residual.strip()) and not re.match(r' {0,3}#', residual)


def mask_code_spans(text):
    """Mask inline code without pairing backticks across a blank line."""
    pattern = re.compile(r'(?<!\\)(`+)(?!`)(.*?)(?<!`)\1(?!`)', re.S)
    # Code spans belong to a paragraph, not the whole document.
    return ''.join(pattern.sub(lambda m: blank(m[0]), part)
                   for part in re.split(r'(\n[ \t]*\n)', text))


def mask_comments(text):
    """Mask closed comments found outside inline code; retain other bytes."""
    visible = mask_code_spans(text)
    for match in reversed(list(re.finditer(r'<!--.*?-->', visible, re.S))):
        text = text[:match.start()] + blank(text[match.start():match.end()]) + text[match.end():]
    return text


def visible_markdown(text):
    """Mask code blocks/spans and closed comments, preserving source offsets."""
    return mask_code_spans(mask_comments(''.join(block_lines(text))))


def escaped(text, pos):
    n = 0
    while pos > 0 and text[pos - 1] == '\\':
        n += 1
        pos -= 1
    return n % 2 == 1


def close_bracket(text, start):
    depth = 1
    for pos in range(start + 1, len(text)):
        if escaped(text, pos):
            continue
        if text[pos] == '[':
            depth += 1
        elif text[pos] == ']':
            depth -= 1
            if not depth:
                return pos
    return None


def destination(text, start):
    """Read an angle or balanced bare Markdown destination, before its title."""
    start += len(text[start:]) - len(text[start:].lstrip())
    if start >= len(text):
        return None
    if text[start] == '<':
        end = start + 1
        while end < len(text):
            if text[end] == '>' and not escaped(text, end):
                return text[start + 1:end], end + 1
            if text[end] == '\n':
                return None
            end += 1
        return None
    depth = 0
    end = start
    while end < len(text):
        char = text[end]
        if not escaped(text, end):
            if char == '(':
                depth += 1
            elif char == ')':
                if not depth:
                    break
                depth -= 1
            elif char.isspace():
                break
        end += 1
    if depth:
        return None
    return text[start:end], end


def title_end(text, start):
    """Consume an optional Markdown title, including escaped delimiters."""
    pos = start
    while pos < len(text) and text[pos] in ' \t\r\n':
        pos += 1
    if pos == start or pos == len(text) or text[pos] not in '\"\'(':
        return pos
    end_char = ')' if text[pos] == '(' else text[pos]
    pos += 1
    while pos < len(text):
        if text[pos] == end_char and not escaped(text, pos):
            return pos + 1
        if text[pos:pos + 2] == '\n\n':
            return None
        pos += 1
    return None


def markdown_links(text):
    """Yield (offset, destination) for inline links and reference definitions."""
    visible = visible_markdown(text)
    # Strip quote markers to spaces so diagnostic source offsets stay intact.
    # block_lines already masked indented code before this normalization.
    visible = re.sub(r'^(?: {0,3}> ?)+', lambda m: blank(m[0]), visible, flags=re.M)
    definition_spans = []
    for match in re.finditer(r'^[ \t]*\[([^\]\n]+)\]:[ \t]*(?:\n[ \t]*)?', visible, re.M):
        dest = destination(visible, match.end())
        if dest and not match[1].startswith('^'):
            # A definition owns its complete line, optionally followed by a
            # title. Prose after its destination makes it ordinary text.
            end = dest[1]
            line_end = visible.find('\n', end)
            if line_end < 0:
                line_end = len(visible)
            tail = visible[end:line_end]
            next_token = visible[end:].lstrip(' \t\r\n')[:1]
            has_title = bool(next_token and next_token in '\"\'(')
            if tail.strip() or has_title:
                end = title_end(visible, end)
                if end is None:
                    continue
                line_end = visible.find('\n', end)
                if line_end < 0:
                    line_end = len(visible)
                if visible[end:line_end].strip():
                    continue
            definition_spans.append((match.start(), line_end))
            yield match.start(), dest[0]
    for start, end in reversed(definition_spans):
        visible = visible[:start] + blank(visible[start:end]) + visible[end:]
    pos = 0
    while pos < len(visible):
        if visible[pos] != '[' or escaped(visible, pos):
            pos += 1
            continue
        end = close_bracket(visible, pos)
        if end is None:
            pos += 1
            continue
        after = end + 1
        if after < len(visible) and visible[after] == '(':
            dest = destination(visible, after + 1)
            if dest:
                closing = title_end(visible, dest[1])
                if closing is not None:
                    while closing < len(visible) and visible[closing] in ' \t\r\n':
                        closing += 1
                if closing is not None and closing < len(visible) and visible[closing] == ')':
                    yield pos, dest[0]
                    # A title may contain link-shaped prose. Keep the label
                    # available for linked images, but mask the destination.
                    visible = visible[:after] + blank(visible[after:closing + 1]) + visible[closing + 1:]
        # Scan labels too: a linked image has its own destination.
        pos += 1


def plain(value):
    # Protect literal code before removing underscore emphasis. Intraword
    # underscores (including identifiers) are ordinary text in Markdown.
    code = []

    def save_code(match):
        code.append(match[2])
        return '\x00{}\x00'.format(len(code) - 1)

    value = re.sub(r'(`+)(.*?)\1', save_code, value)
    value = re.sub(r'(?<![\w\\])(_{1,3})(?=\S)(.*?\S)\1(?!\w)', r'\2', value)
    value = re.sub(r'!?\[([^\]]+)\]\([^)]*\)', r'\1', value)
    value = re.sub(r'<[^>]*>', '', value).replace('*', '')
    value = re.sub(r'\x00(\d+)\x00', lambda m: code[int(m[1])], value)
    return html.unescape(value)


def headings(text):
    """Read ATX/Setext titles for index metadata only, never link anchors."""
    visible = ''.join(block_lines(text))
    visible = mask_comments(visible)
    previous = ''
    offset = 0
    for line in visible.splitlines(keepends=True):
        offset += len(line)
        heading = re.match(r' {0,3}(#{1,6})(?:[ \t]+(.+?))?(?:[ \t]+#+)?[ \t]*$', line.rstrip('\r\n'))
        if heading:
            yield len(heading[1]), heading[2] or '', offset
        elif previous.strip() and re.fullmatch(r' {0,3}(=+|-+)[ \t]*\n?', line):
            yield 1 if line.lstrip().startswith('=') else 2, previous.strip(), offset
        previous = line


def check_links(root):
    errors = []
    for path in markdown_files(root) + [root / 'README.md', root / 'CLAUDE.md']:
        text = path.read_text(encoding='utf-8')
        for offset, raw in markdown_links(text):
            target = html.unescape(re.sub(r'\\([!"#$%&\x27()*+,\-./:;<=>?@\[\]\\^_`{|}~])', r'\1', raw))
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc or target.startswith('/'):
                continue
            dest = path.parent / unquote(parsed.path) if parsed.path else path
            problem = None
            if not dest.exists():
                problem = 'missing destination'
            if problem:
                errors.append('{}:{}: {}: {}'.format(path.relative_to(root), text.count('\n', 0, offset) + 1, problem, raw))
    return errors


def cell(text):
    # Escape Markdown metacharacters so row text cannot create new links.
    return html.escape(plain(text), quote=False).replace('\\', '&#92;').replace('|', '&#124;').replace('[', '&#91;').replace(']', '&#93;')


def render(records, root):
    records = [r for r in records if r['path'] != root / 'docs/README.md']
    date = max((r['date'] for r in records), default='2026-10-11')
    lines = [MARKER, 'Status: live', 'Date: ' + date, '', '# Documentation index', '', INDEX_INTRO, '',
             '<!-- Generated by scripts/docs_index.py; do not edit by hand. -->', '',
             'Regenerate with `python3 scripts/docs_index.py write`; validate with',
             '`python3 scripts/docs_index.py check` from the repository root.', '',
             'Dates record status classification, not renewed measurement or authorization.',
             '`live` means a maintained reference or a plan with remaining work;',
             '`historical` preserves completed work, observations, or rejected proposals.',
             'Superseded entries name their replacement relative to the original document.',
             'Research and probe documents are excluded from metadata and this index,',
             'but their relative Markdown links are checked too. The checker ignores',
             'code and comments, external URLs, and absolute host paths; it checks',
             'file/directory destinations; fragments are not checked.', '',
             '| Document | Status | Date | Purpose |', '| --- | --- | --- | --- |']
    for record in records:
        name = record['path'].relative_to(root / 'docs').as_posix()
        # Angle destinations permit spaces and parentheses in file names.
        lines.append('| [{}](<{}>) | {} | {} | {} |'.format(
            cell(record['title']), name, cell(record['status']), record['date'], cell(record['purpose'])))
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('write', 'check'))
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        records = collect(root)
        index = root / 'docs/README.md'
        expected = render(records, root)
        if args.command == 'write':
            index.write_text(expected, encoding='utf-8')
        elif not index.is_file() or index.read_text(encoding='utf-8') != expected:
            raise DocsError('docs/README.md is stale; run python3 scripts/docs_index.py write')
        errors = check_links(root)
        if errors:
            raise DocsError('\n'.join(errors))
    except (DocsError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print('Documentation metadata, index and relative links checked.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
