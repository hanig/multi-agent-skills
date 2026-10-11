#!/usr/bin/env python3
"""Generate the documentation index and validate local Markdown destinations.

No network requests are made. URI schemes, protocol-relative URLs and absolute
host paths are outside the relative-link contract. Markdown code and comments
are not links. Local Markdown fragments use GitHub heading slugs or HTML ids.
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
    title = re.search(r'^# ([^\n]+)', text, re.M)
    if not title:
        raise DocsError('{}: missing document title'.format(path))
    # The first prose paragraph after the title supplies the purpose. Skip
    # headings, comments and block quotes, which often carry old review notes.
    paragraphs = re.split(r'\n\s*\n', text[title.end():])
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


def visible_markdown(text):
    """Mask code blocks/spans and comments, preserving source offsets."""
    text = ''.join(block_lines(text))
    pattern = re.compile(r'<!--.*?(?:-->|\Z)|(?<!\\)(`+)(?!`)(.*?)(?<!`)\1(?!`)', re.S)
    return pattern.sub(lambda m: blank(m[0]), text)


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


def label(value):
    return ' '.join(value.split()).casefold()


def markdown_links(text):
    """Yield (offset, destination) for inline, image and reference links."""
    visible = visible_markdown(text)
    definitions = {}
    definition_spans = []
    for match in re.finditer(r'^ {0,3}\[([^\]\n]+)\]:[ \t]*(?:\n[ \t]*)?', visible, re.M):
        dest = destination(visible, match.end())
        if dest:
            definitions.setdefault(label(match[1]), dest[0])
            definition_spans.append((match.start(), dest[1]))
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
                tail = visible[dest[1]:]
                # Optional quoted/parenthesized title, then closing paren.
                closing = re.match(r'\s*(?:"[^"\n]*"|\x27[^\x27\n]*\x27|\([^()\n]*\))?\s*\)', tail)
                if closing:
                    yield pos, dest[0]
        else:
            reference = visible[pos + 1:end]
            if after < len(visible) and visible[after] == '[':
                ref_end = close_bracket(visible, after)
                if ref_end is not None:
                    reference = visible[after + 1:ref_end] or reference
            if label(reference) in definitions:
                yield pos, definitions[label(reference)]
        # Scan labels too: a linked image has its own destination.
        pos += 1


def plain(value):
    value = re.sub(r'!?\[([^\]]+)\]\([^)]*\)', r'\1', value)
    value = re.sub(r'<[^>]*>', '', value)
    return html.unescape(value.replace('`', '').replace('*', ''))


def anchors(text):
    # Keep inline code content for headings; strip only block code/comments.
    headings = []
    fence = None
    lines = re.sub(r'<!--.*?-->', '', text, flags=re.S).splitlines()
    visible = []
    for line in lines:
        token = re.match(r' {0,3}(`{3,}|~{3,})', line)
        if token:
            if fence is None:
                fence = (token[1][0], len(token[1]))
            elif re.fullmatch(r' {0,3}' + re.escape(fence[0]) + '{' + str(fence[1]) + r',}\s*', line):
                fence = None
            visible.append('')
            continue
        visible.append('' if fence or line.startswith(('    ', '\t')) else line)
    for i, line in enumerate(visible):
        heading = re.match(r' {0,3}#{1,6}\s+(.+?)(?:\s+#+)?\s*$', line)
        if heading:
            headings.append(heading[1])
        elif i and re.fullmatch(r' {0,3}(?:=+|-+)\s*', line) and visible[i - 1].strip():
            headings.append(visible[i - 1].strip())
    result = set()
    for heading in headings:
        slug = re.sub(r'[^\w\- ]', '', plain(heading).lower()).replace(' ', '-')
        candidate = slug
        count = 0
        while candidate in result:
            count += 1
            candidate = '{}-{}'.format(slug, count)
        result.add(candidate)
    result.update(m[2] for m in re.finditer(r'\b(?:id|name)\s*=\s*(["\x27])(.*?)\1', '\n'.join(visible)))
    return result


def check_links(root):
    errors = []
    cache = {}
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
            elif parsed.fragment and dest.is_file() and dest.suffix.lower() == '.md':
                if dest not in cache:
                    cache[dest] = anchors(dest.read_text(encoding='utf-8'))
                if unquote(parsed.fragment) not in cache[dest]:
                    problem = 'missing anchor'
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
             'file/directory destinations and Markdown heading or HTML-id fragments.', '',
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
