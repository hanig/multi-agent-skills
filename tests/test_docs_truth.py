"""Check declared doc claims and four reserved suite-count prose forms.

This is deliberately not a natural-language or CommonMark classifier.  The
reserved forms apply to top-level rendered prose; other wording and Markdown
contexts are outside the guard rather than guessed from proximity.
"""
import collections
import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INVENTORY = {
    "README.md": {"suite-count": "count", "review-profiles": "table"},
    "MEMORY.md": {"suite-count": "count", "review-rosters": "table"},
    "CLAUDE.md": {"suite-count": "count", "disabled-reviewers": "scalar"},
}
MARKER = re.compile(r"<!-- docs-truth:(?P<id>[a-z0-9][a-z0-9-]*) -->")
FENCE_OPEN = re.compile(r" {0,3}(?P<fence>`{3,}|~{3,})")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
HTML_COMMENT = re.compile(r"<!--.*?-->[ \t]*")
NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d+)"
ADJECTIVE = r"(?:full|whole|entire|measured|historical|current)"
TOKEN_START = r"(?<![\w-])"
TOKEN_END = r"(?![\w-])"
COUNT_START = r"(?<![\w,-])"
CLAIM_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    rf"{TOKEN_START}{ADJECTIVE}\s+suite(?:\s+(?:size|count))?"
    rf"\s*(?::|=)?\s*\**\s*(?P<count>{NUMBER})\s+tests?{TOKEN_END}",
    rf"{TOKEN_START}built\s+and\s+green\s*:\s*\**\s*"
    rf"(?P<count>{NUMBER})\s+tests?{TOKEN_END}",
    rf"^(?P<count>{NUMBER})\s+tests?\s*,\s*"
    rf"standard\s+library\s+only{TOKEN_END}",
    rf"{COUNT_START}(?P<count>{NUMBER})\s+tests?\s+"
    rf"(?:in|of)\s+the\s+{ADJECTIVE}\s+suite{TOKEN_END}",
))


def normalized_lines(text):
    return [line.rstrip(" \t") for line in text.splitlines()]


def has_unescaped_pipe(text):
    escaped = False
    code_delimiter = None
    index = 0
    while index < len(text):
        char = text[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\" and code_delimiter is None:
            escaped = True
            index += 1
        elif char == "`":
            end = index
            while end < len(text) and text[end] == "`":
                end += 1
            run = end - index
            if code_delimiter is None:
                code_delimiter = run
            elif run == code_delimiter:
                code_delimiter = None
            index = end
        elif char == "|" and code_delimiter is None:
            return True
        else:
            index += 1
    return False


def marker_positions(filename, text):
    positions = []
    open_fence = None
    for index, line in enumerate(normalized_lines(text)):
        fence = FENCE_OPEN.match(line)
        if open_fence is not None:
            closing = re.fullmatch(
                r" {0,3}" + re.escape(open_fence[0])
                + "{" + str(open_fence[1]) + r",}\s*", line)
            if closing:
                open_fence = None
            continue
        if fence:
            token = fence.group("fence")
            open_fence = (token[0], len(token))
            continue
        match = MARKER.fullmatch(line)
        if match and match.group("id") in INVENTORY[filename]:
            positions.append((match.group("id"), index))
    return positions


def is_inert_docs_truth_comment(filename, line):
    return bool(
        HTML_COMMENT.fullmatch(line.strip())
        and not strip_inert_docs_truth_comments(filename, line))


def strip_inert_docs_truth_comments(filename, line):
    def replacement(match):
        comment = match.group(0).strip()
        marker = MARKER.fullmatch(comment)
        if ("docs-truth:" in comment
                and (not marker
                     or marker.group("id") not in INVENTORY[filename])):
            return ""
        return match.group(0)

    return HTML_COMMENT.sub(replacement, line).rstrip(" \t")


def marker_body_index(filename, all_lines, marker_index):
    body_index = marker_index + 1
    while (body_index < len(all_lines)
           and is_inert_docs_truth_comment(filename, all_lines[body_index])):
        body_index += 1
    return body_index


def registered_units(filename, text):
    all_lines = normalized_lines(text)
    positions = marker_positions(filename, text)
    counts = collections.Counter(identifier for identifier, _ in positions)
    expected = INVENTORY[filename]
    if set(counts) != set(expected):
        raise AssertionError(
            f"{filename}: docs-truth marker inventory disagrees; "
            f"expected {sorted(expected)}, found {sorted(counts)}")
    duplicates = sorted(identifier for identifier, count in counts.items()
                        if count != 1)
    if duplicates:
        raise AssertionError(
            f"{filename}: duplicate docs-truth marker(s): "
            f"{', '.join(duplicates)}")

    units = {}
    for identifier, index in positions:
        body_index = marker_body_index(filename, all_lines, index)
        if body_index >= len(all_lines) or not all_lines[body_index]:
            raise AssertionError(
                f"{filename}: orphaned docs-truth marker {identifier!r}")
        if MARKER.fullmatch(all_lines[body_index]):
            raise AssertionError(
                f"{filename}: stacked docs-truth marker {identifier!r}")
        if expected[identifier] in {"scalar", "count"}:
            body_line = strip_inert_docs_truth_comments(
                filename, all_lines[body_index])
            if (body_line.lstrip().startswith(
                    ("#", "```", "~~~", "<!--"))
                    or has_unescaped_pipe(body_line)):
                raise AssertionError(
                    f"{filename}: scalar marker {identifier!r} owns markup")
            if (expected[identifier] == "count"
                    and ("`" in body_line
                         or re.match(
                             r"\s*(?:>|[-+*]\s|\d+[.)]\s)", body_line))):
                raise AssertionError(
                    f"{filename}: suite-count marker owns non-plain prose")
            units[identifier] = [body_line]
            continue
        body = []
        for line in all_lines[body_index:]:
            if not line:
                break
            if is_inert_docs_truth_comment(filename, line):
                continue
            body.append(strip_inert_docs_truth_comments(filename, line))
        if not body:
            raise AssertionError(
                f"{filename}: marker {identifier!r} does not own a table")
        units[identifier] = body
    return units


def prose_lines(text):
    open_fence = None
    in_comment = False
    for line_number, source_line in enumerate(normalized_lines(text), 1):
        fence = FENCE_OPEN.match(source_line)
        if open_fence is not None:
            closing = re.fullmatch(
                r" {0,3}" + re.escape(open_fence[0])
                + "{" + str(open_fence[1]) + r",}\s*", source_line)
            if closing:
                open_fence = None
            yield line_number, ""
            continue
        if fence:
            token = fence.group("fence")
            open_fence = (token[0], len(token))
            yield line_number, ""
            continue

        visible = []
        index = 0
        while index < len(source_line):
            if in_comment:
                end = source_line.find("-->", index)
                if end < 0:
                    index = len(source_line)
                else:
                    in_comment = False
                    index = end + 3
            else:
                start = source_line.find("<!--", index)
                if start < 0:
                    visible.append(source_line[index:])
                    index = len(source_line)
                else:
                    visible.append(source_line[index:start])
                    in_comment = True
                    index = start + 4

        yield line_number, "".join(visible)


def strip_code_spans(text):
    def escaped(position):
        backslashes = 0
        position -= 1
        while position >= 0 and text[position] == "\\":
            backslashes += 1
            position -= 1
        return backslashes % 2 == 1

    visible = []
    index = 0
    while index < len(text):
        if text[index] != "`":
            visible.append(text[index])
            index += 1
            continue
        if escaped(index):
            visible.append(text[index])
            index += 1
            continue
        end = index
        while end < len(text) and text[end] == "`":
            end += 1
        delimiter = text[index:end]
        closing = None
        scan = end
        while scan < len(text):
            candidate = text.find("`", scan)
            if candidate < 0:
                break
            if escaped(candidate):
                scan = candidate + 1
                continue
            candidate_end = candidate
            while (candidate_end < len(text)
                   and text[candidate_end] == "`"):
                candidate_end += 1
            if candidate_end - candidate == len(delimiter):
                closing = candidate
                break
            scan = candidate_end
        if closing is None:
            visible.append(delimiter)
            index = end
        else:
            index = closing + len(delimiter)
    return "".join(visible)


def claim_counts(text):
    return [
        int(match.group("count").replace(",", ""))
        for pattern in CLAIM_PATTERNS
        for match in pattern.finditer(text)
    ]


def unowned_claims(filename, text):
    all_lines = normalized_lines(text)
    suite_markers = [
        index for identifier, index in marker_positions(filename, text)
        if identifier == "suite-count"
    ]
    owned_lines = {
        marker_body_index(filename, all_lines, index) + 1
        for index in suite_markers
    }
    paragraphs = []
    paragraph_lines = []
    for line_number, line in prose_lines(text):
        if not line.strip():
            if paragraph_lines:
                paragraphs.append(paragraph_lines)
                paragraph_lines = []
            continue
        paragraph_lines.append((line_number, line.strip()))
    if paragraph_lines:
        paragraphs.append(paragraph_lines)

    owned_paragraphs = [
        paragraph for paragraph in paragraphs
        if any(line_number in owned_lines for line_number, _ in paragraph)
    ]
    if (len(owned_paragraphs) != len(owned_lines)
            or any(len(paragraph) != 1 for paragraph in owned_paragraphs)):
        raise AssertionError(
            f"{filename}: suite-count marker must own a standalone paragraph")
    return [
        (paragraph[0][0], count)
        for paragraph in paragraphs
        if not any(line_number in owned_lines for line_number, _ in paragraph)
        for count in claim_counts(strip_code_spans(
            " ".join(line for _, line in paragraph)))
    ]


def check_suite_counts(filename, text, units, discovered):
    claims = claim_counts(units["suite-count"][0])
    if not claims:
        raise AssertionError(
            f"{filename}: suite-count marker owns no suite-count claim")
    stale = [count for count in claims if count != discovered]
    if stale:
        raise AssertionError(
            f"{filename}: documented suite count does not match the "
            f"{discovered} tests discovered by unittest "
            f"(marked values: {stale})")
    escaped = unowned_claims(filename, text)
    if escaped:
        details = ", ".join(
            f"line {line}: {count}" for line, count in escaped)
        raise AssertionError(
            f"{filename}: unmarked suite-count claim(s): {details}")


def markdown_cells(line):
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        raise AssertionError("not a complete Markdown table row")
    cells = []
    cell = []
    escaped = False
    code_delimiter = None
    content = stripped[1:-1]
    index = 0
    while index < len(content):
        char = content[index]
        if escaped:
            cell.extend(("\\", char))
            escaped = False
            index += 1
        elif char == "\\" and code_delimiter is None:
            escaped = True
            index += 1
        elif char == "`":
            end = index
            while end < len(content) and content[end] == "`":
                end += 1
            token = content[index:end]
            run = len(token)
            if code_delimiter is None:
                code_delimiter = run
            elif run == code_delimiter:
                code_delimiter = None
            cell.append(token)
            index = end
        elif char == "|" and code_delimiter is None:
            cells.append("".join(cell).strip())
            cell = []
            index += 1
        else:
            cell.append(char)
            index += 1
    if escaped or code_delimiter is not None:
        raise AssertionError("malformed Markdown table row")
    cells.append("".join(cell).strip())
    return cells


def table_rows(filename, unit, expected_header):
    try:
        rows = [markdown_cells(line) for line in unit]
    except AssertionError as exc:
        raise AssertionError(f"{filename}: {exc}") from exc
    if not rows or rows[0] != expected_header:
        raise AssertionError(f"{filename}: marked table has the wrong header")
    if len(rows) < 3 or len(rows[1]) != len(expected_header):
        raise AssertionError(f"{filename}: marked table has no valid separator")
    if not all(re.fullmatch(r":?-{3,}:?", cell) for cell in rows[1]):
        raise AssertionError(f"{filename}: marked table has an invalid separator")
    if any(len(row) != len(expected_header) for row in rows[2:]):
        raise AssertionError(f"{filename}: marked table has a malformed row")
    return rows[2:]


def roster(filename, label, cell):
    raw_names = re.split(r"\s*,\s*|\s+and\s+", cell.strip())
    names = []
    for raw in raw_names:
        quoted = raw.startswith("`") and raw.endswith("`")
        if raw.startswith("`") != raw.endswith("`"):
            raise AssertionError(f"{filename}: malformed {label} reviewer {raw!r}")
        name = raw[1:-1] if quoted else raw
        if not NAME.fullmatch(name):
            raise AssertionError(f"{filename}: malformed {label} reviewer {raw!r}")
        names.append(name)
    if len(names) != len(set(names)):
        raise AssertionError(f"{filename}: duplicate reviewer in {label}")
    return set(names)


def roster_rows(filename, rows, labels, membership_column):
    parsed = {}
    documented = [row[0].strip("`") for row in rows]
    if len(documented) != len(set(documented)):
        raise AssertionError(f"{filename}: duplicate reviewer claim row")
    for label in labels:
        matches = [row for row in rows if row[0].strip("`") == label]
        if len(matches) != 1:
            raise AssertionError(
                f"{filename}: expected one {label!r} reviewer claim, "
                f"found {len(matches)}")
        parsed[label] = roster(filename, label, matches[0][membership_column])
    if set(documented) != set(labels):
        raise AssertionError(
            f"{filename}: documented reviewer profiles disagree with config")
    return parsed


def disabled_roster(filename, unit):
    match = re.fullmatch(r"Disabled reviewers: (?P<members>none|.+)\.", unit[0])
    if not match:
        raise AssertionError(f"{filename}: malformed disabled-reviewer claim")
    if match.group("members") == "none":
        return set()
    return roster(filename, "disabled", match.group("members"))


class TestDocsTruth(unittest.TestCase):

    def test_registered_claims_match_live_sources(self):
        discovered = unittest.TestLoader().discover(
            str(ROOT / "tests")).countTestCases()
        documents = {name: (ROOT / name).read_text() for name in INVENTORY}
        units = {
            name: registered_units(name, text)
            for name, text in documents.items()
        }
        for filename in INVENTORY:
            with self.subTest(document=filename, claim="suite-count"):
                check_suite_counts(
                    filename, documents[filename], units[filename], discovered)

        config = json.loads((
            ROOT / "skills" / "hanig-review-gate" / "reviewers.json"
        ).read_text())
        reviewers = config["reviewers"]
        gate_profiles = set(config["_profiles"]) - {"committee"}
        expected_profiles = {
            profile: {
                reviewer["name"] for reviewer in reviewers
                if reviewer.get("enabled", True)
                and profile in (reviewer.get("profiles") or [])
            }
            for profile in gate_profiles
        }
        readme_rows = table_rows(
            "README.md", units["README.md"]["review-profiles"],
            ["profile", "membership", "use"])
        self.assertEqual(
            roster_rows("README.md", readme_rows, expected_profiles, 1),
            expected_profiles)

        enabled_gate = {
            reviewer["name"] for reviewer in reviewers
            if reviewer.get("enabled", True)
            and gate_profiles.intersection(reviewer.get("profiles") or [])
        }
        enabled_committee = {
            reviewer["name"] for reviewer in reviewers
            if reviewer.get("enabled", True)
            and "committee" in (reviewer.get("profiles") or [])
        }
        memory_rows = table_rows(
            "MEMORY.md", units["MEMORY.md"]["review-rosters"],
            ["Piece", "File", "State"])
        memory_rosters = roster_rows(
            "MEMORY.md",
            [row for row in memory_rows
             if row[0] in {"Review gate", "Committee"}],
            {"Review gate", "Committee"}, 2)
        self.assertEqual(
            memory_rosters,
            {"Review gate": enabled_gate, "Committee": enabled_committee})

        disabled = {
            reviewer["name"] for reviewer in reviewers
            if not reviewer.get("enabled", True)
        }
        self.assertEqual(
            disabled_roster(
                "CLAUDE.md", units["CLAUDE.md"]["disabled-reviewers"]),
            disabled)

        # The live document, never this source file, supplies every marker.
        for filename, text in documents.items():
            if filename not in INVENTORY:
                continue
            marker = f"<!-- docs-truth:{next(iter(INVENTORY[filename]))} -->"
            with self.subTest(document=filename, mutation="missing marker"):
                with self.assertRaisesRegex(AssertionError, filename):
                    registered_units(filename, text.replace(marker, "", 1))
            with self.subTest(document=filename, mutation="duplicate marker"):
                duplicate = text.replace(marker, marker + "\n" + marker, 1)
                with self.assertRaisesRegex(AssertionError, filename):
                    registered_units(filename, duplicate)
            with self.subTest(document=filename, mutation="CRLF and whitespace"):
                variant = text.replace(marker, marker + " \t", 1)
                self.assertEqual(
                    set(registered_units(filename, variant.replace("\n", "\r\n"))),
                    set(INVENTORY[filename]))

        with self.subTest(mutation="unmarked stale suite count"):
            variant = (
                documents["README.md"]
                + "\nHistorical suite size: 1 tests.\n")
            with self.assertRaisesRegex(AssertionError, "README.md"):
                check_suite_counts(
                    "README.md", variant,
                    registered_units("README.md", variant), discovered)
        with self.subTest(mutation="unrecognized marker is inert"):
            marker = "<!-- docs-truth:review-profiles -->"
            variant = documents["README.md"].replace(
                marker, marker + "\n<!-- docs-truth:future-note -->", 1)
            self.assertEqual(
                registered_units("README.md", variant),
                units["README.md"])
            check_suite_counts(
                "README.md", variant,
                registered_units("README.md", variant), discovered)
            inline_variant = documents["README.md"].replace(
                "| `plan` |",
                "| `plan` <!-- docs-truth:future-note --> |", 1)
            self.assertEqual(
                registered_units("README.md", inline_variant),
                units["README.md"])
        with self.subTest(mutation="fenced suite history is inert"):
            variant = (
                documents["README.md"]
                + "\n```text\nFull suite: 1 tests\n```\n")
            check_suite_counts(
                "README.md", variant,
                registered_units("README.md", variant), discovered)
        with self.subTest(mutation="local count after bare suite is inert"):
            variant = (
                documents["README.md"]
                + "\nThe suite stopped after 1 tests in one module.\n")
            check_suite_counts(
                "README.md", variant,
                registered_units("README.md", variant), discovered)
        with self.subTest(mutation="hyphenated non-reserved prose is inert"):
            for prose in (
                    "The non-full suite: 1 tests.",
                    "The near-full suite count: 1 tests.",
                    "A semi-current suite: 1 tests.",
                    "A re-built and green: 1 tests.",
                    "This discusses post-50 tests in the full suite."):
                variant = documents["README.md"] + "\n" + prose + "\n"
                check_suite_counts(
                    "README.md", variant,
                    registered_units("README.md", variant), discovered)
        with self.subTest(mutation="unmatched backtick stays prose"):
            for prose in (
                    "The `flag changed; Historical suite size: 1 tests.",
                    "``example``` Historical suite size: 1 tests. `",
                    r"Use \`Full suite: 1 tests\` as the label."):
                variant = documents["README.md"] + "\n" + prose + "\n"
                with self.assertRaisesRegex(AssertionError, "README.md"):
                    check_suite_counts(
                        "README.md", variant,
                        registered_units("README.md", variant), discovered)
                self.assertEqual(claim_counts(strip_code_spans(prose)), [1])
        with self.subTest(mutation="closed code spans are inert"):
            for prose in (
                    "`Full suite: 1 tests.`",
                    "The example ``Full suite:\n1 tests.`` is code.",
                    "``example``` Historical suite size: 1 tests. ``",
                    r"Use \\`Full suite: 1 tests\\` as the label."):
                variant = documents["README.md"] + "\n" + prose + "\n"
                check_suite_counts(
                    "README.md", variant,
                    registered_units("README.md", variant), discovered)
        with self.subTest(mutation="owned count paragraph cannot soft-wrap"):
            claim = f"Full suite: {discovered} tests"
            variant = documents["README.md"].replace(
                claim + ", standard library only; no network or cluster required.\n\n",
                claim + ", standard library only; no network or cluster required.\n"
                "50 tests, standard library only.\n\n", 1)
            with self.assertRaisesRegex(
                    AssertionError, "standalone paragraph"):
                check_suite_counts(
                    "README.md", variant,
                    registered_units("README.md", variant), discovered)
        with self.subTest(mutation="marked second stale count"):
            claim = f"Full suite: {discovered} tests"
            variant = documents["README.md"].replace(
                claim, claim + ". Historical suite size: 1 tests", 1)
            with self.assertRaisesRegex(AssertionError, "marked values"):
                check_suite_counts(
                    "README.md", variant,
                    registered_units("README.md", variant), discovered)
        with self.subTest(mutation="singular unmarked count"):
            variant = documents["README.md"] + "\nFull suite: 1 test.\n"
            with self.assertRaisesRegex(AssertionError, "README.md"):
                check_suite_counts(
                    "README.md", variant,
                    registered_units("README.md", variant), discovered)
        with self.subTest(mutation="count before unmarked suite"):
            variant = (
                documents["README.md"]
                + "\nThere were 1 tests in the full suite.\n")
            with self.assertRaisesRegex(AssertionError, "README.md"):
                check_suite_counts(
                    "README.md", variant,
                    registered_units("README.md", variant), discovered)
        with self.subTest(mutation="wrapped unmarked suite count"):
            variant = documents["README.md"] + "\nFull suite:\n1 tests.\n"
            with self.assertRaisesRegex(AssertionError, "README.md"):
                check_suite_counts(
                    "README.md", variant,
                    registered_units("README.md", variant), discovered)
        with self.subTest(mutation="later module count is inert"):
            mixed_count_claim = (
                f"Full suite: {discovered} tests, "
                "and module X has 50 tests.")
            variant = (
                documents["README.md"]
                + "\n" + mixed_count_claim + "\n")
            with self.assertRaisesRegex(AssertionError, "README.md"):
                check_suite_counts(
                    "README.md", variant,
                    registered_units("README.md", variant), discovered)
            self.assertEqual(claim_counts(mixed_count_claim), [discovered])
            mixed_legacy_claim = (
                f"Full suite: {discovered} tests, and module X has "
                "50 tests, standard library only.")
            self.assertEqual(
                claim_counts(mixed_legacy_claim), [discovered])
        with self.subTest(mutation="escaped table pipe"):
            self.assertEqual(
                markdown_cells(r"| key | prose with \| a pipe |"),
                ["key", r"prose with \| a pipe"])
        with self.subTest(mutation="multi-backtick table pipe"):
            self.assertEqual(
                markdown_cells("| key | ``prose | pipe`` |"),
                ["key", "``prose | pipe``"])
        with self.subTest(mutation="unpiped continuation row"):
            marker = "<!-- docs-truth:review-profiles -->"
            table_text = documents["README.md"].replace(
                "\n\n**Two contrasting models", "\nrogue | members | prose\n\n"
                "**Two contrasting models", 1)
            self.assertIn(marker, table_text)
            with self.assertRaisesRegex(AssertionError, "README.md"):
                bad_units = registered_units("README.md", table_text)
                table_rows(
                    "README.md", bad_units["review-profiles"],
                    ["profile", "membership", "use"])
        with self.subTest(mutation="all reviewers enabled"):
            self.assertEqual(
                disabled_roster("CLAUDE.md", ["Disabled reviewers: none."]),
                set())
        with self.subTest(mutation="dotted reviewer"):
            self.assertEqual(
                roster("README.md", "plan", "a.and.b and `luna`"),
                {"a.and.b", "luna"})


if __name__ == "__main__":
    unittest.main()
