"""Guard the canonical suite count and every declared test method."""
import ast
import collections
import importlib
import inspect
import re
import sys
import tokenize
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CANONICAL_DOCUMENT = ROOT / "CLAUDE.md"
SUITE_MARKER = "<!-- docs-truth:suite-count -->"
FENCE_OPEN = re.compile(r" {0,3}(?P<fence>`{3,}|~{3,})")
BLOCKQUOTE_PREFIX = re.compile(r"(?: {0,3}>[ \t]?)+")
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
))


def normalized_lines(text):
    return [line.rstrip(" \t") for line in text.splitlines()]


def block_line_details(line):
    quote = BLOCKQUOTE_PREFIX.match(line)
    quote_offset = quote.end() if quote else 0
    return line[quote_offset:], line[:quote_offset].count(">")


def valid_fence_match(line):
    fence = FENCE_OPEN.match(line)
    if (fence and fence.group("fence").startswith("`")
            and "`" in line[fence.end("fence") :]):
        return None
    return fence


def crosses_block_boundary(text, start, end):
    """Return whether an inline span would consume a later block opener."""
    for line in text[start:end].split("\n")[1:]:
        block_line, _ = block_line_details(line)
        if (valid_fence_match(block_line)
                or block_line.startswith(("    ", "\t"))):
            return True
    return False


def opening_token_is_escaped(text, position):
    """Return whether a prose syntax opener is backslash-escaped."""
    backslashes = 0
    position -= 1
    while position >= 0 and text[position] == "\\":
        backslashes += 1
        position -= 1
    return backslashes % 2 == 1


def matching_code_delimiter(text, start, width):
    """Find an equal backtick run before this paragraph ends."""
    paragraph_break = re.search(r"\n[ \t]*\n", text[start:])
    limit = (start + paragraph_break.start()
             if paragraph_break else len(text))
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


def lex_document(text):
    """Return one source-ordered lexical view of blocks and inline syntax."""
    lines = normalized_lines(text)
    source = "\n".join(lines)
    visible = list(source)
    boundaries = [not line.strip() for line in lines]
    comment_lines = set()
    markers = []
    open_fence = None
    index = 0
    while index < len(source):
        line_number = source.count("\n", 0, index) + 1
        line_start = index == 0 or source[index - 1] == "\n"
        if line_start:
            line_end = source.find("\n", index)
            if line_end < 0:
                line_end = len(source)
            line = source[index:line_end]
            block_line, quote_depth = block_line_details(line)
            if open_fence is not None:
                if open_fence[2] and quote_depth != open_fence[2]:
                    open_fence = None
                else:
                    boundaries[line_number - 1] = True
                    visible[index:line_end] = " " * (line_end - index)
                    closing_line = line if open_fence[2] == 0 else block_line
                    closing = re.fullmatch(
                        r" {0,3}" + re.escape(open_fence[0])
                        + "{" + str(open_fence[1]) + r",}\s*",
                        closing_line)
                    if closing:
                        open_fence = None
                    index = line_end
                    continue
            fence = valid_fence_match(block_line)
            if fence:
                token = fence.group("fence")
                open_fence = (token[0], len(token), quote_depth)
                boundaries[line_number - 1] = True
                visible[index:line_end] = " " * (line_end - index)
                index = line_end
                continue
            if block_line.startswith(("    ", "\t")):
                boundaries[line_number - 1] = True
                visible[index:line_end] = " " * (line_end - index)
                index = line_end
                continue

        if (source.startswith("<!--", index)
                and not opening_token_is_escaped(source, index)):
            closing = source.find("-->", index + 4)
            if closing < 0:
                # An unterminated opener is literal and owns no later prose.
                index += 1
                continue
            end = closing + 3
            comment = source[index:end]
            first_comment_line = source.count("\n", 0, index)
            last_comment_line = source.count("\n", 0, end)
            comment_lines.update(
                range(first_comment_line, last_comment_line + 1))
            if (comment == SUITE_MARKER
                    and "\n" not in comment
                    and re.fullmatch(
                        r" {0,3}" + re.escape(SUITE_MARKER) + r"[ \t]*",
                        lines[line_number - 1])):
                markers.append(line_number)
            visible[index:end] = [
                "\n" if char == "\n" else " "
                for char in source[index:end]
            ]
            index = end
            continue

        if (source[index] == "`"
                and not opening_token_is_escaped(source, index)):
            delimiter_end = index
            while delimiter_end < len(source) and source[delimiter_end] == "`":
                delimiter_end += 1
            width = delimiter_end - index
            closing = matching_code_delimiter(source, delimiter_end, width)
            if (closing is not None
                    and not crosses_block_boundary(
                        source, delimiter_end, closing)):
                end = closing + width
                visible[index:end] = [
                    "\n" if char == "\n" else " "
                    for char in source[index:end]
                ]
                index = end
                continue
            # Do not reinterpret a suffix of this unmatched run as a shorter
            # delimiter; the whole run is one literal token.
            index = delimiter_end
            continue
        index += 1

    visible_lines = "".join(visible).split("\n") if lines else []
    for line_index in comment_lines:
        if line_index < len(visible_lines) and not visible_lines[line_index].strip():
            boundaries[line_index] = True
    return markers, [
        (index + 1, visible_lines[index], boundaries[index])
        for index in range(len(lines))
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


def check_canonical_suite_count(filename, text, discovered):
    markers, lexed_lines = lex_document(text)
    if len(markers) != 1:
        raise AssertionError(
            f"{filename}: expected one canonical suite-count marker, "
            f"found {len(markers)}")

    marker_line = markers[0]
    paragraphs = prose_paragraphs(lexed_lines)
    following = [
        (line_number, line)
        for paragraph in paragraphs
        for line_number, line in paragraph
        if line_number > marker_line
    ]
    if not following:
        raise AssertionError(f"{filename}: suite-count marker has no claim")
    claim_line = following[0][0]
    owners = [
        paragraph for paragraph in paragraphs
        if any(line_number == claim_line for line_number, _ in paragraph)
    ]
    if len(owners) != 1 or len(owners[0]) != 1:
        raise AssertionError(
            f"{filename}: canonical suite count must be a standalone paragraph")

    canonical_visible_line = owners[0][0][1]
    canonical_counts = claim_counts(canonical_visible_line)
    if len(canonical_counts) != 1:
        raise AssertionError(
            f"{filename}: canonical marker must own exactly one suite count")
    if canonical_counts[0] != discovered:
        raise AssertionError(
            f"{filename}: documented suite count {canonical_counts[0]} does "
            f"not match {discovered} tests discovered by unittest")

    unowned = []
    for paragraph in paragraphs:
        if paragraph is owners[0]:
            continue
        visible = "\n".join(line for _, line in paragraph)
        for position, count in claim_matches(visible):
            line_offset = visible.count("\n", 0, position)
            unowned.append((paragraph[line_offset][0], count))
    if unowned:
        details = ", ".join(
            f"line {line_number}: {count}"
            for line_number, count in unowned)
        raise AssertionError(
            f"{filename}: unmarked suite-count claim(s): {details}")


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
    """Return top-level test declarations and their source ownership range."""
    declarations = []
    if isinstance(path, Path):
        with tokenize.open(path) as source_file:
            source = source_file.read()
    else:
        source = path.read_text()
    for node in ast.parse(source).body:
        if not isinstance(node, ast.ClassDef):
            continue
        declarations.append((node.name, [
            member.name for member in node.body
            if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
            and member.name.startswith("test")
        ], node.lineno, node.end_lineno))
    return declarations


def source_test_methods(path):
    """Return the public name-only view used by focused regressions."""
    return [
        (name, methods)
        for name, methods, _, _ in source_test_classes(path)
    ]


def runtime_method_location(owner, method):
    value = inspect.unwrap(getattr(owner, method))
    function = getattr(value, "__func__", value)
    code = getattr(function, "__code__", None)
    return ((code.co_filename, code.co_firstlineno)
            if code is not None else (None, None))


def source_filename_matches(path, runtime_filename):
    if runtime_filename is None:
        return False
    if isinstance(path, Path):
        return Path(runtime_filename).resolve() == path.resolve()
    return Path(runtime_filename).name == path.name


def expected_test_methods(path, module, concrete=None):
    """Project source declarations onto concrete unittest TestCase classes."""
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
    for owner_name, methods, first_line, last_line in declarations:
        if class_counts[owner_name] != 1:
            continue
        duplicate_declarations.extend(
            f"{owner_name}.{method}"
            for method, count in collections.Counter(methods).items()
            if count != 1)
        owner = getattr(module, owner_name, None)
        if (not inspect.isclass(owner)
                or owner.__name__ != owner_name
                or owner.__module__ != module.__name__):
            unavailable_classes.append(owner_name)
            continue
        erased = {
            method for method in methods
            if not callable(getattr(owner, method, None))
        }
        rebound = set()
        for method in methods:
            if method in erased:
                continue
            method_file, method_line = runtime_method_location(owner, method)
            if (not source_filename_matches(path, method_file)
                    or method_line is None
                    or not first_line <= method_line <= last_line):
                rebound.add(method)
        erased.update(rebound)
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
    loader = loader or unittest.defaultTestLoader
    module_loader = module_loader or importlib.import_module
    paths = list(paths)
    repository_discovery = paths and all(
        isinstance(path, Path) for path in paths)
    discovered_suite = (
        loader.discover(str(ROOT / "tests"))
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


class TestDocsTruth(unittest.TestCase):

    def test_canonical_suite_count_rejects_unmarked_contradictions(self):
        discovered = unittest.TestLoader().discover(
            str(ROOT / "tests")).countTestCases()
        text = CANONICAL_DOCUMENT.read_text()
        check_canonical_suite_count(CANONICAL_DOCUMENT.name, text, discovered)

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
        )
        for variant in contradictions:
            with self.subTest(variant=variant[-90:]):
                with self.assertRaisesRegex(AssertionError, "CLAUDE.md"):
                    check_canonical_suite_count(
                        CANONICAL_DOCUMENT.name, variant, discovered)

        visible_claim = f"Historical suite size: {discovered + 1} tests."
        after_multiline_comment = (
            text + "\n<!-- first line\nsecond line -->\n"
            + visible_claim + "\n")
        claim_line = after_multiline_comment.splitlines().index(
            visible_claim) + 1
        with self.assertRaisesRegex(
                AssertionError, rf"line {claim_line}: {discovered + 1}"):
            check_canonical_suite_count(
                CANONICAL_DOCUMENT.name, after_multiline_comment, discovered)

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
            text.replace(
                f"Full suite: {discovered} tests on 2026-09-19.",
                f"Full suite: {discovered} tests, standard library only.",
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
            text + f"\n    Full suite: {discovered + 1} tests.\n",
            text + "\n    " + SUITE_MARKER + "\n",
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
                check_canonical_suite_count(
                    CANONICAL_DOCUMENT.name, variant, discovered)


if __name__ == "__main__":
    unittest.main()
