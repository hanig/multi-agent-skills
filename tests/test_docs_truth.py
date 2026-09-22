"""Guard the canonical suite floor and every declared test method."""
import ast
import collections
import importlib
import inspect
import re
import sys
import tempfile
import tokenize
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CANONICAL_DOCUMENT = ROOT / "CLAUDE.md"
LIVE_DOCUMENTS = (
    CANONICAL_DOCUMENT,
    ROOT / "MEMORY.md",
    ROOT / "README.md",
)
SUITE_MARKER = "<!-- docs-truth:suite-lower-bound -->"
FENCE_OPEN = re.compile(r" {0,3}(?P<fence>`{3,}|~{3,})")
ATX_HEADING = re.compile(r" {0,3}#{1,6}(?:[ \t]+|$)")
SETEXT_UNDERLINE = re.compile(r" {0,3}(?:=+|-+)[ \t]*$")
THEMATIC_BREAK = re.compile(
    r" {0,3}(?:(?:-[ \t]*){3,}|(?:\*[ \t]*){3,}|(?:_[ \t]*){3,})$")
TABLE_DELIMITER_CELL = re.compile(r"(?:-{3,}|:-+:?|-+:)")
NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d+)"
ADJECTIVE = r"(?:full|whole|entire|measured|historical|current)"
TOKEN_START = r"(?<![\w-])"
TOKEN_END = r"(?![\w-])"
COUNT_START = r"(?<![\w,-])"
SUITE_CLAIMS = tuple(
    re.compile(pattern, re.IGNORECASE | re.MULTILINE) for pattern in (
    rf"{TOKEN_START}{ADJECTIVE}\s+suite(?:\s+(?:size|count))?"
    rf"\s*(?::|=)?\s*\**\s*(?P<count>{NUMBER})\s+tests?{TOKEN_END}",
    rf"{TOKEN_START}built\s+and\s+green\s*:\s*\**\s*"
    rf"(?P<count>{NUMBER})\s+tests?{TOKEN_END}",
    rf"{COUNT_START}(?P<count>{NUMBER})\s+tests?\s*,\s*"
    rf"standard\s+library\s+only{TOKEN_END}",
    rf"{COUNT_START}(?P<count>{NUMBER})\s+tests?\s+"
    rf"(?:in|of)\s+the\s+{ADJECTIVE}\s+suite{TOKEN_END}",
    rf"{TOKEN_START}(?:the\s+)?suite\s+(?:currently\s+)?"
    rf"(?:contains|has)\s+\**\s*(?P<count>{NUMBER})\s+tests?{TOKEN_END}",
    # A copula was missing, so "The full suite total is 2,000 tests." matched
    # nothing -- luna. Only a colon or an equals sign was admitted, and a
    # copula is the ordinary way to write the sentence.
    rf"{TOKEN_START}{ADJECTIVE}\s+suite\s+"
    rf"(?:total|size|count)\s*(?:\s+(?:is|was|stands\s+at))?"
    rf"\s*(?::|=)?\s*\**\s*"
    rf"(?P<count>{NUMBER})\s+tests?{TOKEN_END}",
))
LOWER_BOUND_CLAIM = re.compile(
    rf"{TOKEN_START}(?:the\s+)?{ADJECTIVE}\s+suite"
    rf"(?:\s+(?:size|count))?\s*(?::|=|has|contains)?\s*"
    rf"at\s+least\s+(?P<count>{NUMBER})\s+(?:discoverable\s+)?"
    rf"tests?{TOKEN_END}",
    re.IGNORECASE | re.MULTILINE,
)
CANONICAL_LOWER_BOUND = re.compile(
    rf"Full suite: at least (?P<count>{NUMBER}) tests discoverable by "
    rf"unittest\. Run the command below for the exact current total\.",
    re.IGNORECASE,
)


def normalized_lines(text):
    return [line.rstrip(" \t") for line in text.splitlines()]


def valid_fence_match(line):
    fence = FENCE_OPEN.match(line)
    if (fence and fence.group("fence").startswith("`")
            and "`" in line[fence.end("fence") :]):
        return None
    return fence


def table_cells(line):
    """Return GFM table cells, or None when no unescaped pipe exists."""
    if len(line) - len(line.lstrip(" ")) > 3:
        return None
    content = line.lstrip(" ")
    cells = []
    cell = []
    saw_pipe = False
    backslashes = 0
    for character in content:
        if character == "|" and backslashes % 2 == 0:
            saw_pipe = True
            cells.append("".join(cell).strip())
            cell = []
        else:
            cell.append(character)
        if character == "\\":
            backslashes += 1
        else:
            backslashes = 0
    cells.append("".join(cell).strip())
    if not saw_pipe:
        return None
    if cells and not cells[0]:
        cells.pop(0)
    if cells and not cells[-1]:
        cells.pop()
    return cells or None


def opening_token_is_escaped(text, position):
    """Return whether a prose syntax opener is backslash-escaped."""
    backslashes = 0
    position -= 1
    while position >= 0 and text[position] == "\\":
        backslashes += 1
        position -= 1
    return backslashes % 2 == 1


def matching_code_delimiter(text, start, width):
    """Find an equal backtick run within one already-owned text block."""
    limit = len(text)
    scan = start
    while scan < limit:
        candidate = text.find("`", scan, limit)
        if candidate < 0:
            return None
        end = candidate
        while end < limit and text[end] == "`":
            end += 1
        if end - candidate == width:
            return candidate
        scan = end
    return None


def leading_spaces(line, cursor):
    end = cursor
    while end < len(line) and line[end] == " ":
        end += 1
    return end - cursor


def quote_marker_end(line, cursor):
    spaces = leading_spaces(line, cursor)
    marker = cursor + spaces
    if spaces > 3 or marker >= len(line) or line[marker] != ">":
        return None
    marker += 1
    if marker < len(line) and line[marker] == " ":
        marker += 1
    return marker


def list_marker_details(line, cursor):
    spaces = leading_spaces(line, cursor)
    start = cursor + spaces
    if spaces > 3 or start >= len(line):
        return None
    marker_end = start
    ordered_start = None
    if line[start] in "*+-":
        marker_end += 1
    elif line[start].isdigit():
        while marker_end < len(line) and line[marker_end].isdigit():
            marker_end += 1
        digits = line[start:marker_end]
        if (not digits or len(digits) > 9 or marker_end >= len(line)
                or line[marker_end] not in ".)"):
            return None
        ordered_start = int(digits)
        marker_end += 1
    else:
        return None
    if marker_end < len(line) and line[marker_end] != " ":
        return None
    space_end = marker_end
    while space_end < len(line) and line[space_end] == " ":
        space_end += 1
    spacing = space_end - marker_end
    if spacing == 0:
        content_column = marker_end + 1
    elif spacing <= 4:
        content_column = space_end
    else:
        content_column = marker_end + 1
    content = line[content_column:]
    return content_column, ordered_start, bool(content.strip())


def match_containers(line, stack):
    if not line.strip():
        return len(stack), len(line)
    cursor = 0
    for index, frame in enumerate(stack):
        if frame[0] == "quote":
            end = quote_marker_end(line, cursor)
            if end is None:
                return index, cursor
            cursor = end
        else:
            content_column = frame[1]
            if leading_spaces(line, cursor) < content_column - cursor:
                return index, cursor
            cursor = content_column
    return len(stack), cursor


def comment_start(line, cursor):
    spaces = leading_spaces(line, cursor)
    start = cursor + spaces
    if spaces <= 3 and line.startswith("<!--", start):
        return start
    return None


def table_delimiter(line):
    cells = table_cells(line)
    return bool(cells and all(
        TABLE_DELIMITER_CELL.fullmatch(cell) for cell in cells))


def paragraph_interrupts(line, cursor, ordered_requires_one=True):
    residual = line[cursor:]
    if (not residual.strip()
            or ATX_HEADING.match(residual)
            or THEMATIC_BREAK.fullmatch(residual)
            or valid_fence_match(residual)
            or quote_marker_end(line, cursor) is not None
            or comment_start(line, cursor) is not None):
        return True
    item = list_marker_details(line, cursor)
    if item is None:
        return False
    _, ordered_start, has_content = item
    return bool(has_content and (
        ordered_start is None
        or not ordered_requires_one
        or ordered_start == 1))


def table_interrupts(line, cursor):
    """Return whether a supported block start ends a GFM table."""
    return (paragraph_interrupts(line, cursor, ordered_requires_one=False)
            or list_marker_details(line, cursor) is not None)


def mask_inline_text(text):
    """Mask code spans and terminated inline comments in one owned block."""
    visible = list(text)
    index = 0
    while index < len(text):
        if (text.startswith("<!--", index)
                and not opening_token_is_escaped(text, index)):
            closing = text.find("-->", index + 4)
            if closing >= 0:
                end = closing + 3
                visible[index:end] = [
                    "\n" if char == "\n" else " "
                    for char in text[index:end]
                ]
                index = end
                continue
        if text[index] == "`" and not opening_token_is_escaped(text, index):
            delimiter_end = index
            while delimiter_end < len(text) and text[delimiter_end] == "`":
                delimiter_end += 1
            width = delimiter_end - index
            closing = matching_code_delimiter(text, delimiter_end, width)
            if closing is not None:
                end = closing + width
                visible[index:end] = [
                    "\n" if char == "\n" else " "
                    for char in text[index:end]
                ]
                index = end
                continue
            index = delimiter_end
            continue
        index += 1
    return "".join(visible)


def lex_document(text):
    """Return one ownership-ordered lexical view of supported Markdown."""
    lines = normalized_lines(text)
    structural = [line.expandtabs(4) for line in lines]
    records = []
    markers = []
    stack = []
    leaf = None
    paragraph_serial = 0
    comment_end = None

    def emit(index, kind, visible=False, group=None, output=None,
             residual=None):
        records.append({
            "line": index,
            "kind": kind,
            "visible": visible,
            "group": group,
            "output": lines[index] if output is None else output,
            "residual": residual,
            "stack": tuple(stack),
        })

    index = 0
    while index < len(lines):
        raw = lines[index]
        line = structural[index]

        if comment_end is not None:
            end_line, end_column = comment_end
            if index < end_line:
                emit(index, "COMMENT")
                index += 1
                continue
            masked = " " * min(end_column, len(raw)) + raw[end_column:]
            comment_end = None
            leaf = None
            if masked.strip():
                paragraph_serial += 1
                leaf = ("PARAGRAPH", paragraph_serial)
                emit(index, "PARAGRAPH", True, paragraph_serial,
                     output=masked, residual=masked)
            else:
                emit(index, "COMMENT")
            index += 1
            continue

        emitted = False
        while not emitted:
            matched, cursor = match_containers(line, stack)

            if leaf is not None and leaf[0] == "FENCE":
                if matched != len(stack):
                    stack = stack[:matched]
                    leaf = None
                    continue
                residual = line[cursor:]
                token, width = leaf[1], leaf[2]
                closing = re.fullmatch(
                    r" {0,3}" + re.escape(token)
                    + "{" + str(width) + r",}\s*", residual)
                emit(index, "FENCE")
                if closing:
                    leaf = None
                emitted = True
                continue

            if leaf is not None and leaf[0] == "INDENTED_CODE":
                if matched != len(stack):
                    stack = stack[:matched]
                    leaf = None
                    continue
                residual = line[cursor:]
                if not residual.strip() or leading_spaces(line, cursor) >= 4:
                    emit(index, "INDENTED_CODE")
                    emitted = True
                    continue
                leaf = None

            if matched != len(stack):
                if (leaf is not None and leaf[0] == "PARAGRAPH"
                        and line[cursor:].strip()
                        and not paragraph_interrupts(line, cursor)):
                    emit(index, "PARAGRAPH", True, leaf[1], residual=line[cursor:])
                    emitted = True
                    continue
                stack = stack[:matched]
                leaf = None
                continue

            residual = line[cursor:]

            if leaf is not None and leaf[0] == "TABLE":
                if (not residual.strip()
                        or leading_spaces(line, cursor) >= 4
                        or table_interrupts(line, cursor)):
                    leaf = None
                    continue
                emit(index, "TABLE_ROW", True,
                     ("table", index), residual=residual)
                emitted = True
                continue

            if not residual.strip():
                leaf = None
                emit(index, "BLANK")
                emitted = True
                continue

            if leaf is not None and leaf[0] == "PARAGRAPH":
                header = records[-1] if records else None
                header_cells = (
                    table_cells(header["residual"])
                    if header and header["group"] == leaf[1]
                    and header["kind"] == "PARAGRAPH" else None)
                delimiter_cells = table_cells(residual)
                if (header_cells is not None
                        and delimiter_cells is not None
                        and len(header_cells) == len(delimiter_cells)
                        and table_delimiter(residual)):
                    header["kind"] = "TABLE_ROW"
                    header["group"] = ("table", header["line"])
                    emit(index, "TABLE_ROW", True,
                         ("table", index), residual=residual)
                    leaf = ("TABLE",)
                    emitted = True
                    continue
                if SETEXT_UNDERLINE.fullmatch(residual):
                    emit(index, "SETEXT", True,
                         ("setext", index), residual=residual)
                    leaf = None
                    emitted = True
                    continue
                if not paragraph_interrupts(line, cursor):
                    emit(index, "PARAGRAPH", True, leaf[1], residual=residual)
                    emitted = True
                    continue
                leaf = None

            while True:
                residual = line[cursor:]
                quote_end = quote_marker_end(line, cursor)
                if quote_end is not None:
                    stack.append(("quote",))
                    cursor = quote_end
                    continue
                if THEMATIC_BREAK.fullmatch(residual):
                    break
                item = list_marker_details(line, cursor)
                if item is None:
                    break
                content_column, _, _ = item
                stack.append(("list", content_column))
                cursor = content_column

            residual = line[cursor:]
            if not residual.strip():
                emit(index, "CONTAINER")
                emitted = True
                continue
            if THEMATIC_BREAK.fullmatch(residual):
                emit(index, "THEMATIC")
                emitted = True
                continue
            if ATX_HEADING.match(residual):
                emit(index, "ATX", True, ("atx", index), residual=residual)
                emitted = True
                continue
            fence = valid_fence_match(residual)
            if fence:
                token = fence.group("fence")
                leaf = ("FENCE", token[0], len(token))
                emit(index, "FENCE")
                emitted = True
                continue

            opener = comment_start(line, cursor)
            if opener is not None:
                closing_line = index
                closing_column = raw.find("-->", opener + 4)
                while closing_column < 0 and closing_line + 1 < len(lines):
                    closing_line += 1
                    closing_column = lines[closing_line].find("-->")
                if closing_column >= 0:
                    if (not stack and raw.strip() == SUITE_MARKER
                            and closing_line == index):
                        markers.append(index + 1)
                    end_column = closing_column + 3
                    if closing_line == index:
                        masked = " " * end_column + raw[end_column:]
                        if masked.strip():
                            paragraph_serial += 1
                            leaf = ("PARAGRAPH", paragraph_serial)
                            emit(index, "PARAGRAPH", True,
                                 paragraph_serial, output=masked,
                                 residual=masked)
                        else:
                            emit(index, "COMMENT")
                    else:
                        comment_end = (closing_line, end_column)
                        leaf = ("COMMENT",)
                        emit(index, "COMMENT")
                    emitted = True
                    continue

            if leading_spaces(line, cursor) >= 4:
                leaf = ("INDENTED_CODE",)
                emit(index, "INDENTED_CODE")
                emitted = True
                continue
            paragraph_serial += 1
            leaf = ("PARAGRAPH", paragraph_serial)
            emit(index, "PARAGRAPH", True,
                 paragraph_serial, residual=residual)
            emitted = True
        index += 1

    visible_groups = {}
    for record in records:
        if record["visible"]:
            visible_groups.setdefault(record["group"], []).append(record)
    for group in visible_groups.values():
        joined = "\n".join(record["output"] for record in group)
        masked = mask_inline_text(joined).split("\n")
        for record, output in zip(group, masked):
            record["output"] = output

    by_line = {record["line"]: record for record in records}
    return markers, [
        (line_index + 1,
         by_line[line_index]["output"] if by_line[line_index]["visible"]
         else " " * len(lines[line_index]),
         not by_line[line_index]["visible"])
        for line_index in range(len(lines))
    ]


def claim_counts(text):
    return [count for _, count in claim_matches(text)]


def claim_matches(text):
    candidates = sorted(
        (match.start(), match.end(),
         int(match.group("count").replace(",", "")))
        for pattern in SUITE_CLAIMS
        for match in pattern.finditer(text)
    )
    merged = []
    for start, end, count in candidates:
        for index, (owned_start, owned_end, owned_count) in enumerate(merged):
            if count == owned_count and start < owned_end and owned_start < end:
                merged[index] = (
                    min(start, owned_start), max(end, owned_end), count)
                break
        else:
            merged.append((start, end, count))
    return [(start, count) for start, _, count in merged]


def lower_bound_matches(text):
    return [
        (match.start(), int(match.group("count").replace(",", "")))
        for match in LOWER_BOUND_CLAIM.finditer(text)
    ]


def prose_paragraphs(lexed_lines):
    paragraphs = []
    current = []
    for line_number, line, boundary in lexed_lines:
        if boundary:
            if current:
                paragraphs.append(current)
                current = []
        elif line.strip():
            current.append((line_number, line.strip()))
    if current:
        paragraphs.append(current)
    return paragraphs


def scan_counts(paragraphs, skip=None):
    """Every count-shaped claim in PARAGRAPHS, as (line number, count).

    No floor filter. The previous version skipped any count below the
    document's own documented floor, reasoning that a reader cannot
    mistake it for the suite total because the marked claim contradicts
    it -- which luna refused, correctly: `The full suite total is 1499
    tests.` next to a floor of 1500 is exactly the sentence a reader
    consults and is misled by, and suppressing it from a REPORT buys
    nothing. The filter existed to avoid false failures, and this has not
    failed on a competing count since it became a report.
    """
    found = []
    for paragraph in paragraphs:
        if skip is not None and paragraph is skip:
            continue
        visible = "\n".join(line for _, line in paragraph)
        for position, count in (claim_matches(visible)
                                + lower_bound_matches(visible)):
            line_offset = visible.count("\n", 0, position)
            found.append((paragraph[line_offset][0], count))
    return sorted(found)


def check_canonical_suite_floor(filename, text, discovered, problems=None):
    """Structural problems and competing counts for the canonical document.

    Returns the competing counts. Structural problems go into PROBLEMS
    when the caller supplies a list, and otherwise raise ONCE at the end,
    all of them together with the counts.

    luna and kimi-k2.7-code both refuted "every check reports rather than
    raises". They were right about the code and the important half is not
    the raise -- a structural break in the guard's own document should
    fail the suite -- it is that the raise happened EARLY, before the rest
    of the document had been read. One malformed marker hid every
    competing count behind it, which is the same "one problem conceals
    the others" shape the report was introduced to end. So nothing aborts
    the scan; the verdict comes after it.
    """
    raise_at_end = problems is None
    problems = [] if raise_at_end else problems
    markers, lexed_lines = lex_document(text)
    paragraphs = prose_paragraphs(lexed_lines)
    owner = None
    documented_floor = None

    if len(markers) != 1:
        problems.append(
            f"{filename}: expected one canonical suite-floor marker, "
            f"found {len(markers)}")
    else:
        marker_line = markers[0]
        following = [
            (line_number, line)
            for paragraph in paragraphs
            for line_number, line in paragraph
            if line_number > marker_line
        ]
        if not following:
            problems.append(f"{filename}: suite-floor marker has no claim")
        else:
            claim_line = following[0][0]
            owners = [
                paragraph for paragraph in paragraphs
                if any(line_number == claim_line
                       for line_number, _ in paragraph)
            ]
            if len(owners) != 1 or len(owners[0]) != 1:
                problems.append(
                    f"{filename}: canonical suite floor must be a standalone "
                    f"paragraph")
            else:
                owner = owners[0]
                canonical = CANONICAL_LOWER_BOUND.fullmatch(owner[0][1])
                if canonical is None:
                    problems.append(
                        f"{filename}: canonical marker must own the suite "
                        f"lower bound")
                    owner = None
                else:
                    documented_floor = int(
                        canonical.group("count").replace(",", ""))
                    if discovered < documented_floor:
                        problems.append(
                            f"{filename}: documented suite floor "
                            f"{documented_floor} exceeds {discovered} tests "
                            f"discovered by unittest")

    # A competing claim is one that could be MISTAKEN for the marked one.
    # The document's own floor is what decides that, and reusing it needs no
    # new annotation and no new judgement about English.
    #
    # Rejecting every count-shaped paragraph was the last place this guard
    # inferred scope from wording, and luna refuted it with honest content:
    # "The installer's current suite count: 12 tests." matches the pattern
    # and is not about this suite at all. A step-back committee split on the
    # remedy -- astra would have demoted the whole scan to a nonblocking
    # warning, accepting that "nothing guarantees rejection of an unmarked
    # stale total"; deepseek-v4-pro pointed out that the floor already
    # discriminates, since a genuine suite total cannot be below the number
    # this document asserts the suite exceeds. The second keeps the
    # capability, so it is what runs here.
    #
    # It tightens by itself: raise the floor and more numbers become
    # competing claims. What it gives up is an unmarked count BELOW the
    # floor going unremarked -- which by construction cannot be read as this
    # suite's total, because the marked claim directly contradicts it.
    unowned = scan_counts(paragraphs, skip=owner)
    # REPORTED, NOT RAISED. This is astra's plan from the committee split,
    # adopted after luna demonstrated both failure modes of the magnitude
    # rule I had preferred instead -- in one round:
    #
    #   "The installer was built and green: 2,000 tests."
    #       -> would fail the suite. Honest component prose, no stale claim.
    #   "The installer has 1,500 tests."
    #       -> would pass. An at-floor count whose wording no pattern matches.
    #
    # Over-fires on matched wording, under-fires on unmatched wording.
    # Neither magnitude nor phrasing separates a component count from a
    # suite count, which is what astra said when the committee split:
    # "Preserve the existing competing-claim scanner as a NONBLOCKING
    # warning, with line references. Keep exactly-one-marker and
    # marked-claim truth validation as hard requirements."
    #
    # And the cost, in astra's words, conceded rather than hidden: "This
    # deliberately relinquishes automatic rejection of UNMARKED competing
    # suite totals. It is not capability-preserving, and should not be
    # presented as such."
    #
    # What is still HARD here: exactly one marker, that marker owning a
    # standalone claim, and the claim's floor not exceeding the real count.
    # What is now SOFT: everything else in the document. The scan keeps its
    # line references, which is also what the lexer corpus below asserts
    # on -- removing the scan outright would have deleted the only
    # observable those 27 cases have, which is the shape of deletion this
    # repository has been burned by.
    unowned = [(line_number, count) for line_number, count in unowned]
    if raise_at_end and problems:
        raise AssertionError(
            "; ".join(problems)
            + (f" | competing counts also present: {unowned}"
               if unowned else ""))
    return unowned


# Two residual limits, stated because a claim about them has already been
# refuted once each.
#
# A component sentence whose count happens to be AT OR ABOVE the floor is
# still rejected -- luna's "The installer was built and green: 2,000 tests."
# astra predicted exactly this when the committee split: a magnitude rule
# does not establish attribution, it relocates the errors. The relocation
# is accepted because the surviving false positive is a sentence about two
# thousand of something in the one document that carries the live claim,
# which is both rare and genuinely confusable, while the alternative was to
# stop guarding unmarked totals at all.
#
# OUTSIDE the canonical document, an unmarked count is never scanned at any
# magnitude: only a MARKED claim is rejected there. That is rule 1 and it is
# deliberate, since a regex cannot tell what a count is attributed to. It
# means "a competing claim is caught" is true of CLAUDE.md and of nothing
# else, which is narrower than it sounds and is why it is written here.


def check_live_suite_claims(documents, discovered):
    """Check every live document and return one report for all of them.

    Returns `(filename, line number, count)` for every count-shaped claim
    a reader could take for the suite total, in every document. Raises
    once, at the end, if any document is structurally broken.

    Two things were wrong before. luna: this called
    `check_canonical_suite_floor` and threw the return value away, so the
    report reached nobody -- computed and dropped, which is worse than
    not computing it. And then, once the return value was kept, luna
    again: only the canonical document was ever SCANNED. An unmarked
    `The suite has 1600 tests.` in README.md or MEMORY.md produced no
    report at all, although those two documents are named in
    LIVE_DOCUMENTS and the module's whole subject is a count going stale
    in them. The canonical document is the only one with a floor to
    check; it is not the only one a reader believes.
    """
    problems = []
    reported = []
    for path, text in documents:
        if path.name == CANONICAL_DOCUMENT.name:
            reported.extend(
                (path.name, line_number, count)
                for line_number, count in check_canonical_suite_floor(
                    path.name, text, discovered, problems))
            continue
        markers, lexed_lines = lex_document(text)
        if markers:
            details = ", ".join(f"line {line_number}"
                                for line_number in markers)
            problems.append(
                f"{path.name}: marked live suite claim(s) must reference "
                f"{CANONICAL_DOCUMENT.name}: {details}")
        reported.extend(
            (path.name, line_number, count)
            for line_number, count in scan_counts(
                prose_paragraphs(lexed_lines)))
    if problems:
        raise AssertionError(
            "; ".join(problems)
            + (f" | counts also reported: {reported}" if reported else ""))
    return reported


def walk_suite(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from walk_suite(test)
        else:
            yield test


def unittest_discoverable_paths(start):
    """Mirror unittest discovery's rule that recursion enters packages only."""
    paths = []
    for path in start.rglob("test*.py"):
        for parent in path.parents:
            if parent == start:
                paths.append(path)
                break
            if not (parent / "__init__.py").is_file():
                break
    return sorted(paths)


def source_test_classes(path):
    """Return module/nested class test declarations and source ranges."""
    if isinstance(path, Path):
        with tokenize.open(path) as source_file:
            source = source_file.read()
    else:
        source = path.read_text()

    def class_declarations(body, prefix=()):
        declarations = []
        for node in body:
            if not isinstance(node, ast.ClassDef):
                continue
            qualified_name = ".".join(prefix + (node.name,))
            declarations.append((qualified_name, [
                member.name for member in node.body
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                and member.name.startswith("test")
            ], node.lineno, node.end_lineno))
            declarations.extend(
                class_declarations(node.body, prefix + (node.name,)))
        return declarations

    tree = ast.parse(source)
    declarations = class_declarations(tree.body)
    return declarations


def unreachable_test_classes(path):
    """Classes declaring test methods that the module scope cannot reach.

    luna: `source_test_classes` walks module bodies and nested class
    bodies only, so `if False:` around a TestCase -- or one declared
    inside a function -- produced NO expected declaration, and the sweep
    passed on a file whose test methods it had never heard of. That is
    this module's own failure mode: a declaration that leaves the
    collected set with nothing going red, the same shape as the `__main__`
    block that once hid 13 tests.

    Only classes that DECLARE a test method count. The repository has 23
    function-local helper classes (fake loaders, crash doubles) and every
    one of them declares none; reporting those would be noise that trains
    a reader to skip the report.
    """
    if isinstance(path, Path):
        with tokenize.open(path) as source_file:
            source = source_file.read()
    else:
        source = path.read_text()
    tree = ast.parse(source)

    reachable = set()

    def mark(body):
        for node in body:
            if isinstance(node, ast.ClassDef):
                reachable.add(id(node))
                mark(node.body)

    mark(tree.body)
    hidden = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or id(node) in reachable:
            continue
        methods = sorted(
            member.name for member in node.body
            if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
            and member.name.startswith("test"))
        if methods:
            hidden.append((node.name, methods, node.lineno))
    return sorted(hidden)


def source_test_methods(path):
    """Return the public name-only view used by focused regressions."""
    return [
        (name, methods)
        for name, methods, _, _ in source_test_classes(path)
    ]


def expected_test_methods(path, module, concrete=None):
    """Project available source names onto concrete TestCase consumers.

    This proves binding availability and class/name collection membership,
    not callable provenance or execution of the originally declared body.
    Decorators and later callable substitutions are intentionally admissible.
    """
    if concrete is None:
        concrete = {
            cls for _, cls in inspect.getmembers(module, inspect.isclass)
            if cls.__module__ == module.__name__
            and issubclass(cls, unittest.TestCase)
        }
    fresh_loader = unittest.TestLoader()
    collectible = {
        cls: set(fresh_loader.getTestCaseNames(cls))
        for cls in concrete
    }
    expected = set()
    duplicate_declarations = []
    unavailable_classes = []
    erased_declarations = []
    unconsumed_declarations = []
    declarations = source_test_classes(path)
    class_counts = collections.Counter(name for name, _, _, _ in declarations)
    duplicate_declarations.extend(
        f"{name} (class redefined)"
        for name, count in class_counts.items()
        if count != 1)
    for owner_name, methods, _, _ in declarations:
        if class_counts[owner_name] != 1:
            continue
        duplicate_declarations.extend(
            f"{owner_name}.{method}"
            for method, count in collections.Counter(methods).items()
            if count != 1)
        owner = module
        for component in owner_name.split("."):
            owner = getattr(owner, component, None)
            if owner is None:
                break
        runtime_name = (
            getattr(owner, "__qualname__", None)
            if "." in owner_name else getattr(owner, "__name__", None))
        if (not inspect.isclass(owner)
                or runtime_name != owner_name
                or owner.__module__ != module.__name__):
            unavailable_classes.append(owner_name)
            continue
        erased = {
            method for method in methods
            if not callable(getattr(owner, method, None))
        }
        erased_declarations.extend(
            f"{owner_name}.{method}" for method in erased)
        consumers = [cls for cls in concrete if owner in cls.__mro__]
        if not consumers:
            unconsumed_declarations.extend(
                f"{owner_name}.{method}" for method in set(methods))
            continue
        expected.update(
            (cls, method)
            for cls in consumers
            for method in methods
            if method not in erased
            if (cls is owner and issubclass(owner, unittest.TestCase))
            or method in collectible[cls])
    return (expected, sorted(duplicate_declarations),
            sorted(unavailable_classes), sorted(erased_declarations),
            sorted(unconsumed_declarations))


def assert_every_test_method_collected(paths, loader=None, module_loader=None):
    # A FRESH loader for the repository sweep, never the caller's.
    # kimi-k2.7-code: reusing the ambient loader meant `unittest discover
    # -s tests -k docs_truth` -- an ordinary invocation -- compared every
    # declared method against a collection that `-k` had filtered, and
    # reported the filtered-out methods as hidden. A guard that fails
    # because someone ran a subset is a false alarm, and it was documented
    # as a limit here before it was fixed, which is worse: the limit was
    # real and avoidable.
    module_loader = module_loader or importlib.import_module
    paths = list(paths)
    repository_discovery = paths and all(
        isinstance(path, Path) for path in paths)
    sweep_loader = unittest.TestLoader() if repository_discovery else (
        loader or unittest.defaultTestLoader)
    loader = loader or unittest.defaultTestLoader
    discovered_suite = (
        sweep_loader.discover(str(ROOT / "tests"))
        if repository_discovery else None)
    discovered_tests = (
        list(walk_suite(discovered_suite))
        if repository_discovery else [])
    failures = {}
    loaded = []
    for path in paths:
        if isinstance(path, Path):
            module_name = ".".join(
                path.relative_to(ROOT / "tests").with_suffix("").parts)
        else:
            module_name = f"tests.{path.stem}"
        try:
            module = (sys.modules[module_name] if repository_discovery
                      else module_loader(module_name))
            suite = (None if repository_discovery
                     else loader.loadTestsFromName(module_name))
        except Exception as exc:  # report collection failures, do not hide them
            failures[path.name] = {"could not load": str(exc)}
            continue
        loaded.append((path, module, suite))

    module_names = {module.__name__ for _, module, _ in loaded}
    concrete = {
        cls
        for _, module, _ in loaded
        for _, cls in inspect.getmembers(module, inspect.isclass)
        if cls.__module__ in module_names
        and issubclass(cls, unittest.TestCase)
    }
    collected = {
        (type(test), test._testMethodName)
        for test in (
            discovered_tests if repository_discovery
            else walk_suite(unittest.TestSuite(
                suite for _, _, suite in loaded)))
        if isinstance(test, unittest.TestCase)
    }
    for path, module, _ in loaded:
        try:
            (expected, duplicates, unavailable, erased,
             unconsumed) = expected_test_methods(path, module, concrete)
        except Exception as exc:
            failures[path.name] = {"could not inspect": str(exc)}
            continue
        problems = {}
        unreachable = unreachable_test_classes(path)
        if unreachable:
            problems["test methods declared outside module scope"] = [
                "%s.%s (line %d)" % (name, method, line)
                for name, methods, line in unreachable
                for method in methods]
        if duplicates:
            problems["duplicate declarations"] = duplicates
        if unavailable:
            problems["source classes unavailable at runtime"] = unavailable
        if erased:
            problems["source methods unavailable at runtime"] = erased
        if unconsumed:
            problems["test declarations have no TestCase consumer"] = unconsumed
        missing = sorted(
            f"{cls.__name__}.{method}"
            for cls, method in expected - collected)
        if missing:
            problems["declared but not collected"] = missing
        if problems:
            failures[path.name] = problems
    if failures:
        raise AssertionError(f"declared test methods were hidden: {failures}")


def competing_counts(filename, text, discovered):
    """The soft scan's report, for tests that used to expect a raise.

    The scan became non-blocking (astra's plan, after luna demonstrated
    both failure modes of the magnitude rule). The lexer corpus below still
    needs an observable, and this is it: the list of (line, count) the scan
    reports, which is exactly what it used to raise about.
    """
    return check_canonical_suite_floor(filename, text, discovered) or []


def baseline_counts(discovered):
    """What the live canonical document already reports, before a test
    adds anything.

    Every corpus case builds its document by appending a block to the real
    CLAUDE.md, so asserting an ABSOLUTE empty report makes the test depend
    on that file containing no count-shaped prose. glm-5.3 showed the
    consequence: appending the honest sentence "The installer was built
    and green: 2,000 tests." -- the very sentence another test in this
    file requires be accepted -- reddens the suite, which is the false
    failure this round retired. Measured: it fails the entry-point test
    and two corpus tests.

    So the cases assert a DELTA. The hiding property is "adding this block
    does not make a new claim visible", which is true whatever the
    document already says.
    """
    return competing_counts(CANONICAL_DOCUMENT.name,
                            CANONICAL_DOCUMENT.read_text(), discovered)


class TestDocsTruth(unittest.TestCase):

    def test_canonical_suite_floor_rejects_stale_and_competing_claims(self):
        discovered = unittest.TestLoader().discover(
            str(ROOT / "tests")).countTestCases()
        text = CANONICAL_DOCUMENT.read_text()
        documents = [(path, path.read_text()) for path in LIVE_DOCUMENTS]
        check_live_suite_claims(documents, discovered)

        # Ordinary test growth cannot invalidate a monotonic lower bound.
        check_live_suite_claims(documents, discovered + 100)

        stale = CANONICAL_LOWER_BOUND.sub(
            f"Full suite: at least {discovered + 1:,} tests discoverable by "
            "unittest. Run the command below for the exact current total.",
            text,
            count=1,
        )
        with self.assertRaisesRegex(AssertionError, "suite floor"):
            check_canonical_suite_floor(
                CANONICAL_DOCUMENT.name, stale, discovered)

        contradictions = (
            text + f"\nHistorical suite size: {discovered + 1} tests.\n",
            text + f"\n`<!--` Historical suite size: {discovered + 1} tests.\n",
            text + f"\n`<!-- \\` Historical suite size: "
            f"{discovered + 1} tests. -->\n",
            text + f"\n<!-- note ` --> Historical suite size: "
            f"{discovered + 1} tests. ` tail\n",
            text + f"\n`placeholder\n\nFull suite: "
            f"{discovered + 1} tests.`\n",
            text + f"\n<!-- unclosed\nFull suite: {discovered + 1} tests.\n",
            text + f"\n<!-- explanation\n--> Historical suite size: "
            f"{discovered + 1} tests.\n",
            text + f"\n<!--\n``` -->\nHistorical suite size: "
            f"{discovered + 1} tests.\n",
            text + f"\n`<!--\n`\nHistorical suite size: "
            f"{discovered + 1} tests.\n",
            text + f"\nx```unterminated\nHistorical suite size: "
            f"{discovered + 1} tests.\n``\n",
            text + f"\n\\<!-- Historical suite size: "
            f"{discovered + 1} tests. -->\n",
            text + f"\nRouting note: {discovered + 1} tests, "
            "standard library only.\n",
            text + f"\n`opening\n```text\nexample\n```\n"
            f"Historical suite size: {discovered + 1} tests.\n`\n",
            text + f"\nNote:\n{discovered + 1} tests, "
            "standard library only.\n",
            text + f"\nContext:\n    Current suite total: "
            f"{discovered + 1} tests.\n",
        )
        for variant in contradictions:
            with self.subTest(variant=variant[-90:]):
                # The scan reports rather than raises now; the lexer
                # property under test is unchanged -- the claim must be
                # SEEN -- so the assertion moves from the exception to the
                # report.
                self.assertTrue(
                    competing_counts(CANONICAL_DOCUMENT.name, variant,
                                     discovered),
                    "the lexer did not see the smuggled claim")

        visible_claim = f"Historical suite size: {discovered + 1} tests."
        after_multiline_comment = (
            text + "\n<!-- first line\nsecond line -->\n"
            + visible_claim + "\n")
        claim_line = after_multiline_comment.splitlines().index(
            visible_claim) + 1
        self.assertIn(
            (claim_line, discovered + 1),
            competing_counts(CANONICAL_DOCUMENT.name,
                             after_multiline_comment, discovered),
            "the report must name the line and the count")

        exact_claims = (
            f"Current suite: {discovered} tests.",
            f"Current suite total: {discovered} tests.",
            f"The suite currently contains {discovered} tests.",
            f"The suite has {discovered} tests.",
        )
        for target in LIVE_DOCUMENTS:
            for exact_claim in exact_claims:
                competing = [
                    (path, source + (
                        f"\n{SUITE_MARKER}\n{exact_claim}\n"
                        if path == target else ""))
                    for path, source in documents
                ]
                with self.subTest(
                        target=target.name, exact_claim=exact_claim):
                    with self.assertRaisesRegex(AssertionError, target.name):
                        check_live_suite_claims(competing, discovered)

    def test_unmarked_component_counts_are_not_live_suite_claims(self):
        discovered = unittest.TestLoader().discover(
            str(ROOT / "tests")).countTestCases()
        canonical = CANONICAL_DOCUMENT.read_text()
        honest_component_sentences = (
            (0, "The installer's current suite count: 12 tests."),
            (1, "The installer was built and green: 12 tests at release."),
            (2, "The scheduler has 0 tests, standard library only."),
            (3, "A skill with 3 tests in the full suite."),
            (4, "The installer's suite currently contains 12 tests."),
            (5, "The installer's current suite total: 12 tests."),
        )
        for pattern_index, sentence in honest_component_sentences:
            with self.subTest(pattern=pattern_index + 1, sentence=sentence):
                self.assertIsNotNone(
                    SUITE_CLAIMS[pattern_index].search(sentence))
                check_live_suite_claims(
                    ((CANONICAL_DOCUMENT, canonical),
                     (ROOT / "README.md", sentence)),
                    discovered,
                )

    def test_marked_stale_live_total_outside_canonical_is_rejected(self):
        discovered = unittest.TestLoader().discover(
            str(ROOT / "tests")).countTestCases()
        canonical = CANONICAL_DOCUMENT.read_text()
        stale = (
            SUITE_MARKER + "\n"
            + f"Python 3.8+, {discovered - 1} tests, standard library only."
        )
        with self.assertRaisesRegex(
                AssertionError, r"README\.md: marked live suite claim"):
            check_live_suite_claims(
                ((CANONICAL_DOCUMENT, canonical),
                 (ROOT / "README.md", stale)),
                discovered,
            )

    def test_unrecognized_docs_truth_comments_are_inert(self):
        discovered = unittest.TestLoader().discover(
            str(ROOT / "tests")).countTestCases()
        text = CANONICAL_DOCUMENT.read_text()
        unrelated = "<!-- docs-truth:future-note -->"
        inert_variants = (
            text + "\n" + unrelated + "\n",
            text.replace(SUITE_MARKER, SUITE_MARKER + "\n" + unrelated, 1),
            text.replace(
                "\n\n" + SUITE_MARKER,
                "\nContext before marker.\n" + SUITE_MARKER,
                1),
            text + f"\n<!-- ` Full suite: {discovered + 1} tests. -->\n",
            text + f"\n<!--\nHistorical suite size: "
            f"{discovered + 1} tests. -->\n",
            text + f"\n<!-- explanation\n```text\nexample\n```\n"
            f"Full suite: {discovered + 1} tests.\n-->\n",
            text.replace(
                SUITE_MARKER,
                "<!-- multi-line\nunknown comment -->\n" + SUITE_MARKER,
                1),
            text + f"\n`placeholder\nFull suite: {discovered + 1} tests.`\n",
            text + f"\n\n    Full suite: {discovered + 1} tests.\n",
            text + "\n\n    " + SUITE_MARKER + "\n",
            text + "\n```text\n" + SUITE_MARKER + "\n```\n",
            text + f"\n> ```text\n> Full suite: "
            f"{discovered + 1} tests.\n> ```\n",
            text + f"\n```text\n> Full suite: "
            f"{discovered + 1} tests.\n```\n",
            text + f"\n`<!--`\n\n~~~text\nFull suite: "
            f"{discovered + 1} tests.\n~~~\n",
        )
        for variant in inert_variants:
            with self.subTest(variant=variant[-90:]):
                check_canonical_suite_floor(
                    CANONICAL_DOCUMENT.name, variant, discovered)

    def test_indented_code_contexts_preserve_visible_prose(self):
        discovered = unittest.TestLoader().discover(
            str(ROOT / "tests")).countTestCases()
        text = CANONICAL_DOCUMENT.read_text()
        example = f"Full suite: {discovered + 1} tests."
        inert_blocks = {
            "ATX heading": f"# Examples\n    {example}",
            "tab-indented root code": f"\t{example}",
            "setext heading": f"Examples\n==\n    {example}",
            "thematic break": f"---\n    {example}",
            "blank block quote line": (
                f"> Example\n>\n>     {example}"
            ),
            "GFM table end": (
                "| Kind | Value |\n| --- | --- |\n| example | one |\n"
                f"    {example}"
            ),
            "fenced block end": f"```text\nexample\n```\n    {example}",
            "list item code after a blank": (
                f"- Example\n\n      {example}"
            ),
            "list item code after a heading": (
                f"- # Examples\n      {example}"
            ),
            "quoted fence with a literal nested marker": (
                f"> ```text\n> > {example}\n> ```"
            ),
            "nested list fence": (
                f"- outer\n    - ~~~text\n      {example}\n      ~~~"
            ),
            "list then quote fence": (
                f"- > ```text\n  > {example}\n  > ```"
            ),
            "root code after a closed list": (
                f"- Example\n\nRegular prose.\n\n    {example}"
            ),
            "single-dash setext heading": (
                f"Examples\n-\n    {example}"
            ),
            "thematic break closes a list": (
                f"- Example\n* * *\n    {example}"
            ),
            "ordered two does not interrupt a paragraph": (
                f"Context:\n2. item\n\n    {example}"
            ),
            "pipe-free table row before code": (
                "| Kind | Value |\n| --- | --- |\n| example | one |\n"
                f"note\n    {example}"
            ),
        }
        for name, block in inert_blocks.items():
            with self.subTest(name=name):
                # HIDING direction: the lexer must have hidden this, so
                # the report must be EMPTY. Asserting only that nothing
                # raised became vacuous when the scan stopped raising --
                # glm-5.3 showed mask_inline_text could be replaced with
                # `return text` and all 1,751 tests would stay green.
                self.assertEqual(
                    competing_counts(
                            CANONICAL_DOCUMENT.name,
                    text + "\n\n" + block + "\n",
                    discovered,),
                    baseline_counts(discovered),
                    "the lexer failed to hide this claim")

        visible_claim = f"Current suite total: {discovered + 1} tests."
        visible_blocks = {
            "heading text": f"# {visible_claim}",
            "standalone equals line": f"====\n    {visible_claim}",
            "list continuation": f"- Example\n    {visible_claim}",
            "blank-separated list continuation": (
                f"- Example\n\n    {visible_claim}"
            ),
            "tab-separated list continuation": (
                f"- Example\n\n\t{visible_claim}"
            ),
            "block quote continuation": f"> Example\n>     {visible_claim}",
            "block quote lazy continuation": (
                f"> Example\n    {visible_claim}"
            ),
            "table cell": (
                "| Kind | Value |\n| --- | --- |\n"
                f"| current | {visible_claim} |"
            ),
            "pipe-free table cell": (
                "| Kind | Value |\n| --- | --- |\n"
                f"{visible_claim}"
            ),
            "paragraph continuation": f"Example\n    {visible_claim}",
            "inline span crossing a heading": (
                f"`opening\n# {visible_claim}\n`"
            ),
        }
        for name, block in visible_blocks.items():
            with self.subTest(name=name):
                # The property is the lexer's: this prose is VISIBLE, not
                # code. The scan's report is how that is observed now.
                self.assertGreater(
                    len(competing_counts(CANONICAL_DOCUMENT.name,
                                         text + "\n\n" + block + "\n",
                                         discovered)),
                    len(baseline_counts(discovered)),
                    "visible prose was treated as code")

    def test_gfm_delimiter_minimums_preserve_inline_ownership(self):
        discovered = unittest.TestLoader().discover(
            str(ROOT / "tests")).countTestCases()
        text = CANONICAL_DOCUMENT.read_text()
        hidden_claim = f"Current suite total: {discovered + 1} tests."
        for delimiter in ("| - | -- |", "| -- | - |"):
            with self.subTest(delimiter=delimiter):
                # HIDING direction: the lexer must have hidden this, so
                # the report must be EMPTY. Asserting only that nothing
                # raised became vacuous when the scan stopped raising --
                # glm-5.3 showed mask_inline_text could be replaced with
                # `return text` and all 1,751 tests would stay green.
                self.assertEqual(
                    competing_counts(
                            CANONICAL_DOCUMENT.name,
                    text + "\n\n`opening | cell |\n" + delimiter + "\n"
                    + hidden_claim + "`\n",
                    discovered,),
                    baseline_counts(discovered),
                    "the lexer failed to hide this claim")

        for delimiter in ("| --- |", "| :- |", "| -: |", "| :-: |"):
            with self.subTest(valid=delimiter):
                self.assertTrue(table_delimiter(delimiter))
        for delimiter in ("| - |", "| -- |"):
            with self.subTest(invalid=delimiter):
                self.assertFalse(table_delimiter(delimiter))

    def test_empty_list_items_end_tables_without_splitting_inline_spans(self):
        discovered = unittest.TestLoader().discover(
            str(ROOT / "tests")).countTestCases()
        text = CANONICAL_DOCUMENT.read_text()
        hidden_claim = f"Current suite total: {discovered + 1} tests."
        for marker in ("-", "+", "*", "1.", "2)"):
            with self.subTest(marker=marker):
                block = (
                    "| Kind | Value |\n| --- | --- |\n"
                    + marker + "\n  `opening\n  " + hidden_claim + "`"
                )
                # HIDING direction: the lexer must have hidden this, so
                # the report must be EMPTY. Asserting only that nothing
                # raised became vacuous when the scan stopped raising --
                # glm-5.3 showed mask_inline_text could be replaced with
                # `return text` and all 1,751 tests would stay green.
                self.assertEqual(
                    competing_counts(
                            CANONICAL_DOCUMENT.name,
                    text + "\n\n" + block + "\n",
                    discovered,),
                    baseline_counts(discovered),
                    "the lexer failed to hide this claim")

    def test_comment_tails_keep_residual_for_paragraph_continuations(self):
        discovered = unittest.TestLoader().discover(
            str(ROOT / "tests")).countTestCases()
        text = CANONICAL_DOCUMENT.read_text()
        hidden_claim = f"Current suite total: {discovered + 1} tests."
        prefixes = (
            "<!-- first line\nsecond line --> `opening",
            "<!-- note --> `opening",
        )
        for prefix in prefixes:
            with self.subTest(prefix=prefix):
                # HIDING direction: the lexer must have hidden this, so
                # the report must be EMPTY. Asserting only that nothing
                # raised became vacuous when the scan stopped raising --
                # glm-5.3 showed mask_inline_text could be replaced with
                # `return text` and all 1,751 tests would stay green.
                self.assertEqual(
                    competing_counts(
                            CANONICAL_DOCUMENT.name,
                    text + "\n\n" + prefix + "\n" + hidden_claim + "`\n",
                    discovered,),
                    baseline_counts(discovered),
                    "the lexer failed to hide this claim")

    def test_a_component_count_below_the_floor_is_prose_not_a_claim(self):
        """luna's counterexample, reproduced and then fixed.

            $ echo "The installer's current suite count: 12 tests." >> CLAUDE.md
            AssertionError: CLAUDE.md: unmarked suite-count claim(s): line 100: 12

        A guard that reddens because somebody wrote an honest sentence,
        with no stale claim present, is a false failure and this repository
        refuses one. The remedy is the document's own floor rather than a
        new annotation: a count below the number the document asserts the
        suite exceeds cannot be read as this suite's total.
        
    DECLARED LIMIT, found while checking a reviewer's finding: running the
    suite with `-k` whose pattern selects THIS test makes it fail. It
    compares declared methods against collected ones, and `-k` suppresses
    collection of everything else, so the shortfall it reports is the
    filter rather than a hidden test. `-k` patterns that do not match this
    test simply do not run it. No honest full-suite invocation is affected;
    the documented `discover -s tests` is green, including this test, at
    1,748 tests.
    """
        discovered = unittest.TestLoader().discover(
            str(ROOT / "tests")).countTestCases()
        text = CANONICAL_DOCUMENT.read_text()
        for sentence in (
                "The installer's current suite count: 12 tests.",
                "The scheduler has 0 tests, standard library only.",
                "The installer ships 12 tests, standard library only.",
                "A skill with 3 tests in the full suite.",
        ):
            with self.subTest(sentence=sentence):
                check_canonical_suite_floor(
                    CANONICAL_DOCUMENT.name,
                    text + "\n" + sentence + "\n",
                    discovered)

    def test_the_floor_is_the_boundary_between_prose_and_a_competing_claim(self):
        """Exactly at the floor is a competing claim; one below is prose.

        The discrimination has to be the floor itself rather than a
        constant, so that raising the floor tightens the guard with no
        second number to maintain.
        """
        discovered = unittest.TestLoader().discover(
            str(ROOT / "tests")).countTestCases()
        text = CANONICAL_DOCUMENT.read_text()
        floor_match = CANONICAL_LOWER_BOUND.search(text)
        self.assertIsNotNone(floor_match, "the canonical floor must be findable")
        floor = int(floor_match.group("count").replace(",", ""))

        check_canonical_suite_floor(
            CANONICAL_DOCUMENT.name,
            text + f"\nThe suite has {floor - 1} tests.\n",
            discovered)

        self.assertTrue(
            competing_counts(CANONICAL_DOCUMENT.name,
                             text + f"\nThe suite has {floor} tests.\n",
                             discovered),
            "a count exactly at the floor must still be reported")

    def test_one_sentence_two_floors_opposite_outcomes(self):
        """The boundary IS the floor, shown on a single sentence.

        kimi-k2.7-code read the previous version as not demonstrating the
        moving boundary, because its two assertions used different document
        bodies. They tested the property in opposite directions, which is
        the same thing, but the reading was fair: this asserts both
        outcomes for ONE sentence under two floors, side by side.
        """
        discovered = unittest.TestLoader().discover(
            str(ROOT / "tests")).countTestCases()
        text = CANONICAL_DOCUMENT.read_text()
        sentence = "\nThe suite has 1550 tests.\n"

        raised = CANONICAL_LOWER_BOUND.sub(
            "Full suite: at least 1,600 tests discoverable by unittest. Run "
            "the command below for the exact current total.",
            text, count=1)
        self.assertNotEqual(raised, text, "the floor line must be substitutable")

        # floor 1,600: 1,550 is below it, so prose.
        check_canonical_suite_floor(
            CANONICAL_DOCUMENT.name, raised + sentence, max(discovered, 1600))

        # floor 1,500, same sentence: at or above it, so a competing claim.
        self.assertTrue(
            competing_counts(CANONICAL_DOCUMENT.name, text + sentence,
                             discovered),
            "an at-or-above-floor count must still be reported")

    def test_an_exact_count_is_judged_by_the_floor_like_any_other(self):
        """kimi-k2.7-code: the 27 competing-claim subtests all use a number
        above the floor, so they never showed what happens below it or at
        it. An exact count is not a special case -- the floor decides."""
        discovered = unittest.TestLoader().discover(
            str(ROOT / "tests")).countTestCases()
        text = CANONICAL_DOCUMENT.read_text()
        floor = int(CANONICAL_LOWER_BOUND.search(text)
                    .group("count").replace(",", ""))

        # below the floor: prose, even stated as an exact suite total
        check_canonical_suite_floor(
            CANONICAL_DOCUMENT.name,
            text + "\nThe full suite total is %d tests.\n" % (floor - 1),
            discovered)

        # at the floor, and above it: reported as competing, with the line
        for count in (floor, floor + 1, discovered):
            with self.subTest(count=count):
                reported = competing_counts(
                    CANONICAL_DOCUMENT.name,
                    text + "\nThe full suite total is %d tests.\n" % count,
                    max(discovered, count))
                self.assertTrue(reported)
                self.assertIn(count, [found for _line, found in reported])
                self.assertTrue(all(isinstance(line, int)
                                    for line, _found in reported),
                                "the report must name the line")

    def test_honest_component_prose_never_fails_the_suite(self):
        """luna's finding, as a regression rather than a disposition.

        The magnitude rule rejected "The installer was built and green:
        2,000 tests." -- honest prose, no stale claim present, suite red.
        A guard that reddens on an honest sentence is a false failure and
        this repository refuses one, so the scan reports instead of
        raising. These sentences must be accepted at every magnitude,
        including above the floor.
        """
        discovered = unittest.TestLoader().discover(
            str(ROOT / "tests")).countTestCases()
        text = CANONICAL_DOCUMENT.read_text()
        for sentence in (
                "The installer was built and green: 2,000 tests.",
                "The installer's current suite count: 12 tests.",
                "The scheduler has 0 tests, standard library only.",
                "A skill with 3 tests in the full suite.",
                "The full suite total is 9,999 tests.",
        ):
            with self.subTest(sentence=sentence):
                # Must not raise. It may be REPORTED, which is the point.
                check_canonical_suite_floor(
                    CANONICAL_DOCUMENT.name,
                    text + "\n" + sentence + "\n",
                    discovered)

    def test_the_hard_failures_are_still_hard(self):
        """What the retreat did NOT give up."""
        discovered = unittest.TestLoader().discover(
            str(ROOT / "tests")).countTestCases()
        text = CANONICAL_DOCUMENT.read_text()

        stale = CANONICAL_LOWER_BOUND.sub(
            f"Full suite: at least {discovered + 1:,} tests discoverable by "
            "unittest. Run the command below for the exact current total.",
            text, count=1)
        with self.assertRaisesRegex(AssertionError, "suite floor"):
            check_canonical_suite_floor(
                CANONICAL_DOCUMENT.name, stale, discovered)

        two_markers = text + "\n" + SUITE_MARKER + "\nFull suite: at least " \
            "1,500 tests discoverable by unittest. Run the command below " \
            "for the exact current total.\n"
        with self.assertRaisesRegex(AssertionError, "marker"):
            check_canonical_suite_floor(
                CANONICAL_DOCUMENT.name, two_markers, discovered)

    def test_a_count_below_the_documented_floor_is_still_reported(self):
        """luna: the scan suppressed exactly the sentence that misleads.

        The old rule skipped any count below the document's own floor,
        reasoning that the marked claim contradicts it so no reader can
        be fooled. But a reader consulting `The full suite total is 1499
        tests.` does not also read the floor two hundred lines away, and
        the scan is a REPORT -- there was never a false-failure cost to
        avoid.
        """
        discovered = unittest.TestLoader().discover(
            str(ROOT / "tests")).countTestCases()
        text = CANONICAL_DOCUMENT.read_text()
        floor = int(CANONICAL_LOWER_BOUND.search(text)
                    .group("count").replace(",", ""))
        self.assertLess(floor, discovered,
                        "this case needs a floor below the real count")
        low = floor - 1
        reported = competing_counts(
            CANONICAL_DOCUMENT.name,
            text + f"\nThe full suite total is {low} tests.\n",
            discovered)
        self.assertIn(low, [count for _line, count in reported],
                      "a stale total below the floor went unreported")

    def test_a_structural_problem_does_not_hide_the_counts(self):
        """luna and kimi-k2.7-code: the check raised on the first
        problem it met, so everything after it was never read.

        The raise itself is right -- a broken marker in the guard's own
        document should fail the suite. Raising EARLY is what made one
        problem conceal the others, which is the failure this scan was
        rewritten to end.
        """
        discovered = unittest.TestLoader().discover(
            str(ROOT / "tests")).countTestCases()
        text = CANONICAL_DOCUMENT.read_text()
        # Two markers (a structural problem) AND a competing count that
        # sits after them, so an early raise cannot have seen it.
        broken = (text + f"\n{SUITE_MARKER}\n"
                  + f"\nHistorical suite size: {discovered + 3} tests.\n")
        with self.assertRaises(AssertionError) as caught:
            check_canonical_suite_floor(
                CANONICAL_DOCUMENT.name, broken, discovered)
        message = str(caught.exception)
        self.assertIn("marker", message)
        self.assertIn(str(discovered + 3), message,
                      "the structural problem hid the competing count")

        # And with a caller-supplied accumulator, nothing raises at all.
        problems = []
        counts = check_canonical_suite_floor(
            CANONICAL_DOCUMENT.name, broken, discovered, problems)
        self.assertTrue(problems)
        self.assertIn(discovered + 3, [count for _line, count in counts])

    def test_a_test_method_declared_outside_module_scope_is_reported(self):
        """luna: `if False: class Hidden(TestCase): def test_lost` was
        invisible, so the sweep passed on a file whose test methods it
        had never heard of.

        This is the module's own subject -- a declaration leaving the
        collected set with nothing going red -- and it is the same shape
        as the `__main__` block that once hid 13 tests.
        """
        source = (
            "import unittest\n"
            "\n"
            "if False:\n"
            "    class Hidden(unittest.TestCase):\n"
            "        def test_lost(self):\n"
            "            pass\n"
            "\n"
            "def factory():\n"
            "    class AlsoHidden(unittest.TestCase):\n"
            "        def test_also_lost(self):\n"
            "            pass\n"
            "    return AlsoHidden\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test_hidden.py"
            path.write_text(source)
            hidden = unreachable_test_classes(path)
        names = {name: methods for name, methods, _line in hidden}
        self.assertEqual(
            names,
            {"Hidden": ["test_lost"], "AlsoHidden": ["test_also_lost"]})

        # A helper class with no test method is not noise in the report.
        # The repository has 23 of those and every one declares none.
        helper = (
            "def make():\n"
            "    class Crash(Exception):\n"
            "        def raise_it(self):\n"
            "            pass\n"
            "    return Crash\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test_helper.py"
            path.write_text(helper)
            self.assertEqual(unreachable_test_classes(path), [])

    def test_the_entry_point_hands_the_report_to_its_caller(self):
        """luna: the report was computed and thrown away.

        `check_live_suite_claims` called the canonical check and discarded
        its return value, so the soft report reached nobody -- which is
        worse than not computing it, because the scan then costs work and
        buys nothing.
        """
        discovered = unittest.TestLoader().discover(
            str(ROOT / "tests")).countTestCases()
        text = CANONICAL_DOCUMENT.read_text()
        others = [(path, path.read_text()) for path in LIVE_DOCUMENTS
                  if path.name != CANONICAL_DOCUMENT.name]

        clean = check_live_suite_claims(
            [(CANONICAL_DOCUMENT, text)] + others, discovered)
        self.assertEqual(clean, baseline_counts(discovered),
                         "the caller saw something the scan did not report")

        noisy = check_live_suite_claims(
            [(CANONICAL_DOCUMENT,
              text + f"\nHistorical suite size: {discovered + 1} tests.\n")]
            + others, discovered)
        self.assertGreater(
            len(noisy), len(clean),
            "the caller received no report for a competing count")
        self.assertIn(discovered + 1,
                      [count for _name, _line, count in noisy])

        # Every live document is scanned, not only the canonical one.
        # luna: an unmarked stale total in README.md or MEMORY.md was
        # never looked at, although both are in LIVE_DOCUMENTS and both
        # are documents a reader believes.
        for target in (path for path in LIVE_DOCUMENTS
                       if path.name != CANONICAL_DOCUMENT.name):
            with self.subTest(document=target.name):
                planted = [
                    (path, source + (
                        f"\nThe suite has {discovered + 7} tests.\n"
                        if path == target else ""))
                    for path, source in
                    [(CANONICAL_DOCUMENT, text)] + others
                ]
                report = check_live_suite_claims(planted, discovered)
                self.assertIn(
                    (target.name, discovered + 7),
                    [(name, count) for name, _line, count in report],
                    "%s was not scanned for an unmarked stale total"
                    % target.name)


if __name__ == "__main__":
    unittest.main()
