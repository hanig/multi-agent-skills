"""Size, local-reference, and declaration shape of authored skills.

The local-reference contract is deliberately file-level: relative inline
Markdown links only, with fragments and queries refused rather than interpreted
as renderer-specific navigation. Limit records occupy one fence-free body
section, so example text cannot satisfy their inventory.
"""

from pathlib import Path
import re
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "skills"
DEFAULT_BODY_LINE_BUDGET = 500
BODY_LINE_BUDGETS = {
    # A separate planned unit owns this already-measured split.
    "hanig-project": 600,
}
EXTERNAL_MARKDOWN_LINK = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")
MARKDOWN_ESCAPABLE = frozenset(
    r"!\"#$%&'()*+,-./:;<=>?@[\]^_`{|}~\\"
)
MARKDOWN_DELIMITER_WHITESPACE = frozenset(" \t\n\r\f\v")
SWARM_LIMIT_LABELS = (
    "LIMIT: runtime canary scope.",
    "LIMIT: trusted-writer isolation.",
    "LIMIT: container isolation scope.",
    "LIMIT: pre-dispatch artifact basis.",
    "LIMIT: same-UID authority access.",
    "LIMIT: process-tree quiescence.",
    "LIMIT: remote-ref durability.",
    "LIMIT: verifier corpus boundary.",
    "LIMIT: integration verification topology.",
    "LIMIT: write scopes.",
    "LIMIT: worktree inode identity.",
    "LIMIT: child credentials.",
    "LIMIT: worktree adoption.",
    "LIMIT: Paseo workspace ID.",
    "LIMIT: pipeline interior.",
    "LIMIT: convergence plateau.",
    "LIMIT: coordinator lock topology.",
    "LIMIT: output-claim registry.",
    "LIMIT: base-branch comparison.",
)
SWARM_BEHAVIOR_CLAUSES = (
    "Report missing Python, Git, scheduler, Paseo, or bus capabilities",
    "Never infer OpenCode as a supported worker backend",
    "A unit may not be its own canary",
    "The container backend and image are declarations; never infer them",
    "Unsupported or ambiguous isolation profiles are refused",
    "Never restate a done predicate without its basis clause",
    "delete the redundant attribution check",
    "Slurm ownership logic is lifted from `contract.py`; do not edit",
    "Widening worker credential access requires a deliberately designed proxy",
    "agent credentials required for those actions are not a defect to re-file",
    "must rename that claim or migrate its receipt producer",
    "Branch-local test receipts can never satisfy `integration-tests`",
    "The coordinator never rewrites `remote.origin.fetch`",
    "local-only repository is refused before launch",
    "The base is a commit ID, never a ref",
    "Paseo `--cwd` names only the trusted source repository",
    "A misspelled criterion key is refused during validation",
    "never the writable attempt directory",
    "never a Paseo schedule",
    "Do not add a heartbeat, TTL, or lock stealing",
    "Choose lock topology per host and record it",
    "Do not replace the Paseo registry checks with a lock",
    "take identity from the launch intent",
    "Do not use `sbatch --test-only` start estimates to decide whether to wait",
    "Test on Python 3.10, not the newest host",
    "tests/test_record_is_not_authority.py",
)


def _body(path):
    lines = path.read_text(encoding="utf-8").splitlines()
    boundaries = [index for index, line in enumerate(lines) if line == "---"]
    if len(boundaries) < 2 or boundaries[0] != 0:
        raise AssertionError(f"{path}: missing YAML frontmatter boundaries")
    return "\n".join(lines[boundaries[1] + 1:])


def _authored_skill_docs():
    return sorted(SKILLS.glob("hanig-*/SKILL.md"))


def _is_escaped(text, index):
    backslashes = 0
    index -= 1
    while index >= 0 and text[index] == "\\":
        backslashes += 1
        index -= 1
    return bool(backslashes % 2)


def _markdown_unescape(value):
    out = []
    index = 0
    while index < len(value):
        if (value[index] == "\\" and index + 1 < len(value)
                and value[index + 1] in MARKDOWN_ESCAPABLE):
            index += 1
        out.append(value[index])
        index += 1
    return "".join(out)


def _label_end(body, start):
    depth = 1
    index = start + 1
    while index < len(body):
        if body[index] == "\\" and index + 1 < len(body):
            index += 2
            continue
        if body[index] == "[":
            depth += 1
        elif body[index] == "]":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _destination(body, start):
    index = start
    while (index < len(body)
           and body[index] in MARKDOWN_DELIMITER_WHITESPACE):
        index += 1
    if index >= len(body):
        return "", index, False
    if index > start and body[index] in ("\"", "'", "("):
        return "", index, False

    if body[index] == "<":
        begin = index + 1
        index = begin
        while index < len(body):
            if body[index] == "\n":
                return _markdown_unescape(body[begin:index]), index, False
            if body[index] == ">" and not _is_escaped(body, index):
                return _markdown_unescape(body[begin:index]), index + 1, False
            index += 1
        return _markdown_unescape(body[begin:index]), index, False

    begin = index
    depth = 0
    while index < len(body):
        char = body[index]
        if (char == "\\" and index + 1 < len(body)
                and body[index + 1] in MARKDOWN_ESCAPABLE):
            index += 2
            continue
        if char in MARKDOWN_DELIMITER_WHITESPACE and depth == 0:
            break
        if char == "(":
            depth += 1
        elif char == ")":
            if depth == 0:
                return _markdown_unescape(body[begin:index]), index + 1, True
            depth -= 1
        index += 1
    return _markdown_unescape(body[begin:index]), index, False


def _quoted_end(body, start, quote):
    index = start + 1
    recovery = None
    while index < len(body):
        if body[index] == "\\" and index + 1 < len(body):
            index += 2
            continue
        if body[index] == quote:
            return index + 1, True
        if recovery is None and body[index] == "[" and not _is_escaped(body, index):
            recovery = index
        elif recovery is None and body[index] == "\n":
            recovery = index + 1
        index += 1
    return (recovery if recovery is not None else index), False


def _parenthesized_title_end(body, start):
    depth = 1
    index = start + 1
    recovery = None
    while index < len(body):
        if body[index] == "\\" and index + 1 < len(body):
            index += 2
            continue
        if body[index] == "(":
            depth += 1
        elif body[index] == ")":
            depth -= 1
            if depth == 0:
                return index + 1, True
        if recovery is None and body[index] == "[" and not _is_escaped(body, index):
            recovery = index
        elif recovery is None and body[index] == "\n":
            recovery = index + 1
        index += 1
    return (recovery if recovery is not None else index), False


def _outer_link_end(body, start):
    index = start
    crossed_line = False
    while (index < len(body)
           and body[index] in MARKDOWN_DELIMITER_WHITESPACE):
        crossed_line = crossed_line or body[index] == "\n"
        index += 1
    if index < len(body) and body[index] in ("\"", "'"):
        index, closed = _quoted_end(body, index, body[index])
        if not closed:
            return index
    elif index < len(body) and body[index] == "(":
        index, closed = _parenthesized_title_end(body, index)
        if not closed:
            return index
    elif crossed_line and (index >= len(body) or body[index] != ")"):
        return index

    depth = 0
    quote = None
    while index < len(body):
        char = body[index]
        if char == "\\" and index + 1 < len(body):
            index += 2
            continue
        if quote:
            if char == quote:
                quote = None
        elif char in ("\"", "'"):
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            if depth == 0:
                return index + 1
            depth -= 1
        elif char == "\n" and index + 1 < len(body) and body[index + 1] == "\n":
            return index + 2
        index += 1
    return index


def _inline_markdown_targets(body):
    index = 0
    while index < len(body):
        if body[index] != "[" or _is_escaped(body, index):
            index += 1
            continue
        close = _label_end(body, index)
        if close is None or close + 1 >= len(body) or body[close + 1] != "(":
            index += 1
            continue
        target, tail, already_closed = _destination(body, close + 2)
        yield target
        index = tail if already_closed else _outer_link_end(body, tail)
        if index <= close + 1:
            index = close + 2


def _local_markdown_targets(body):
    for target in _inline_markdown_targets(body):
        if target and not EXTERNAL_MARKDOWN_LINK.match(target):
            yield target


def _local_reference_problems(doc):
    skill = doc.parent.resolve()
    problems = []
    for target in _local_markdown_targets(_body(doc)):
        if "#" in target or "?" in target:
            problems.append(f"local link must name a whole file: {target}")
            continue
        resolved = (skill / target).resolve()
        if not resolved.is_relative_to(skill):
            problems.append(f"link escapes skill: {target}")
        elif not resolved.is_file():
            problems.append(f"linked file is absent: {target}")
    return problems


def _swarm_limit_section(body):
    lines = body.splitlines()
    start = lines.index("## Declared limits: meet every one before relying on the system")
    end = next((index for index in range(start + 1, len(lines))
                if lines[index].startswith("## ")), len(lines))
    return lines[start + 1:end]


class TestAuthoredSkillShape(unittest.TestCase):
    def test_every_authored_skill_body_is_within_its_line_budget(self):
        docs = _authored_skill_docs()
        self.assertTrue(docs, "the authored-skill sweep matched no files")
        for doc in docs:
            body_lines = len(_body(doc).splitlines())
            budget = BODY_LINE_BUDGETS.get(doc.parent.name,
                                           DEFAULT_BODY_LINE_BUDGET)
            with self.subTest(skill=doc.parent.name):
                self.assertLessEqual(
                    body_lines, budget,
                    f"{doc}: authored body has {body_lines} lines; budget is {budget}",
                )

    def test_body_markdown_paths_exist_inside_the_skill(self):
        for doc in _authored_skill_docs():
            with self.subTest(skill=doc.parent.name):
                self.assertEqual(_local_reference_problems(doc), [])

    def test_a_missing_escaping_or_fragmented_body_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            skill = Path(raw) / "hanig-example"
            skill.mkdir()
            references = skill / "references"
            references.mkdir()
            (references / "details.md").write_text("# Present heading\n",
                                                    encoding="utf-8")
            doc = skill / "SKILL.md"
            doc.write_text(
                "---\nname: example\n---\n"
                "[missing](references/missing.md) [escape](../outside.md) "
                "[fragment](references/details.md#present-heading)\n",
                encoding="utf-8",
            )
            self.assertEqual(
                _local_reference_problems(doc),
                ["linked file is absent: references/missing.md",
                 "link escapes skill: ../outside.md",
                 "local link must name a whole file: "
                 "references/details.md#present-heading"],
            )

    def test_a_same_file_fragment_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            skill = Path(raw) / "hanig-example"
            skill.mkdir()
            doc = skill / "SKILL.md"
            doc.write_text(
                "---\nname: example\n---\n[x](#some-heading)\n",
                encoding="utf-8",
            )
            self.assertEqual(
                _local_reference_problems(doc),
                ["local link must name a whole file: #some-heading"],
            )

    def test_a_missing_local_path_with_an_external_url_in_its_query_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            skill = Path(raw) / "hanig-example"
            skill.mkdir()
            doc = skill / "SKILL.md"
            doc.write_text(
                "---\nname: example\n---\n"
                "[x](references/nonexistent-file.md?r=https://example.com)\n",
                encoding="utf-8",
            )
            self.assertEqual(
                _local_reference_problems(doc),
                ["local link must name a whole file: "
                 "references/nonexistent-file.md?r=https://example.com"],
            )

    def test_scheme_prefixed_external_links_are_not_local_references(self):
        with tempfile.TemporaryDirectory() as raw:
            skill = Path(raw) / "hanig-example"
            skill.mkdir()
            doc = skill / "SKILL.md"
            doc.write_text(
                "---\nname: example\n---\n"
                "[web](https://example.com/docs?q=one#heading)\n"
                "[ftp](ftp://example.com/readme.md)\n"
                "[file](file:///tmp/readme.md)\n"
                "[git](git+ssh://example.com/repository.git)\n"
                "[malformed](https://[::1)\n"
                "[spaced]( https://example.com/docs )\n"
                "[angled](<https://example.com/docs>)\n",
                encoding="utf-8",
            )
            self.assertEqual(_local_reference_problems(doc), [])

    def test_empty_destinations_are_not_local_references(self):
        with tempfile.TemporaryDirectory() as raw:
            skill = Path(raw) / "hanig-example"
            skill.mkdir()
            doc = skill / "SKILL.md"
            doc.write_text(
                "---\nname: example\n---\n"
                "[bare]()\n"
                "[angle](<>)\n"
                "[quoted]( \"title\")\n"
                "[parenthesized]( (title) )\n",
                encoding="utf-8",
            )
            self.assertEqual(list(_local_markdown_targets(_body(doc))), [])
            self.assertEqual(_local_reference_problems(doc), [])

    def test_unicode_filename_space_is_not_a_markdown_delimiter(self):
        with tempfile.TemporaryDirectory() as raw:
            skill = Path(raw) / "hanig-example"
            skill.mkdir()
            filename = "existing\u00a0file.md"
            (skill / filename).write_text("details\n", encoding="utf-8")
            doc = skill / "SKILL.md"
            doc.write_text(
                f"---\nname: example\n---\n[present]({filename})\n",
                encoding="utf-8",
            )
            self.assertEqual(
                list(_local_markdown_targets(_body(doc))),
                [filename],
            )
            self.assertEqual(_local_reference_problems(doc), [])

    def test_a_markdown_title_is_not_part_of_the_local_path(self):
        with tempfile.TemporaryDirectory() as raw:
            skill = Path(raw) / "hanig-example"
            skill.mkdir()
            (skill / "details.md").write_text("details\n", encoding="utf-8")
            doc = skill / "SKILL.md"
            doc.write_text(
                "---\nname: example\n---\n"
                "[present](details.md \"see [ghost](ghost.md) at "
                "https://example.com\")\n"
                "[missing](missing.md 'documentation')\n",
                encoding="utf-8",
            )
            self.assertEqual(
                _local_reference_problems(doc),
                ["linked file is absent: missing.md"],
            )

    def test_a_multiline_markdown_title_is_not_scanned_as_another_link(self):
        with tempfile.TemporaryDirectory() as raw:
            skill = Path(raw) / "hanig-example"
            skill.mkdir()
            (skill / "details.md").write_text("details\n", encoding="utf-8")
            doc = skill / "SKILL.md"
            doc.write_text(
                "---\nname: example\n---\n"
                "[present](details.md\n\"[ghost](ghost.md)\")\n",
                encoding="utf-8",
            )
            self.assertEqual(
                list(_local_markdown_targets(_body(doc))),
                ["details.md"],
            )
            self.assertEqual(_local_reference_problems(doc), [])

    def test_a_malformed_query_or_escaped_title_cannot_hide_a_local_path(self):
        with tempfile.TemporaryDirectory() as raw:
            skill = Path(raw) / "hanig-example"
            skill.mkdir()
            doc = skill / "SKILL.md"
            doc.write_text(
                "---\nname: example\n---\n"
                "[query](missing.md?q=hello world)\n"
                "[title](missing.md \"see \\\"https://example.com\\\"\")\n",
                encoding="utf-8",
            )
            self.assertEqual(
                _local_reference_problems(doc),
                ["local link must name a whole file: missing.md?q=hello",
                 "linked file is absent: missing.md"],
            )

    def test_scanner_preserves_destination_syntax_and_outer_boundaries(self):
        with tempfile.TemporaryDirectory() as raw:
            skill = Path(raw) / "hanig-example"
            skill.mkdir()
            (skill / "details.md").write_text("details\n", encoding="utf-8")
            (skill / "a(b).md").write_text("balanced\n", encoding="utf-8")
            doc = skill / "SKILL.md"
            doc.write_text(
                "---\nname: example\n---\n"
                "[outer](details.md \"[ghost](ghost.md)\")\n"
                "[angle](<details.md >)\n"
                "[balanced](a(b).md)\n"
                "[escaped](a\\(b\\).md)\n",
                encoding="utf-8",
            )
            body = _body(doc)
            self.assertEqual(
                list(_inline_markdown_targets(body)),
                ["details.md", "details.md ", "a(b).md", "a(b).md"],
            )
            self.assertEqual(
                _local_reference_problems(doc),
                ["linked file is absent: details.md "],
            )

    def test_malformed_link_recovery_does_not_hide_later_links(self):
        with tempfile.TemporaryDirectory() as raw:
            skill = Path(raw) / "hanig-example"
            skill.mkdir()
            (skill / "existing.md").write_text("existing\n", encoding="utf-8")
            (skill / "details.md").write_text("details\n", encoding="utf-8")
            doc = skill / "SKILL.md"
            doc.write_text(
                "---\nname: example\n---\n"
                "[valid](existing.md)\n"
                "[broken](missing.md\n"
                "[later](absent.md)\n"
                "[literal-backslash](details.md\\ \"note\")\n"
                "[unclosed-quote](quote-missing.md \"title\n"
                "[after-quote](quote-hidden.md)\n"
                "[unclosed-paren](paren-missing.md (title\n"
                "[after-paren](paren-hidden.md)\n",
                encoding="utf-8",
            )
            self.assertEqual(
                list(_inline_markdown_targets(_body(doc))),
                ["existing.md", "missing.md", "absent.md", "details.md\\",
                 "quote-missing.md", "quote-hidden.md", "paren-missing.md",
                 "paren-hidden.md"],
            )
            self.assertEqual(
                _local_reference_problems(doc),
                ["linked file is absent: missing.md",
                 "linked file is absent: absent.md",
                 "linked file is absent: details.md\\",
                 "linked file is absent: quote-missing.md",
                 "linked file is absent: quote-hidden.md",
                 "linked file is absent: paren-missing.md",
                 "linked file is absent: paren-hidden.md"],
            )

    def test_every_declared_swarm_limit_remains_in_the_body_with_a_pointer(self):
        doc = SKILLS / "hanig-swarm" / "SKILL.md"
        section = _swarm_limit_section(_body(doc))
        self.assertFalse(any(line.lstrip().startswith(("```", "~~~"))
                             for line in section),
                         "declared limits must remain prose, not examples")
        limit_lines = [line for line in section
                       if line.startswith("- **LIMIT: ")]
        labels = [line.split("**", 2)[1] for line in limit_lines]
        self.assertEqual(set(labels), set(SWARM_LIMIT_LABELS))
        self.assertEqual(len(labels), len(SWARM_LIMIT_LABELS))
        for label, line in zip(labels, limit_lines):
            with self.subTest(limit=label):
                self.assertIn("](references/limits.md)", line)

    def test_baseline_swarm_behavior_decisions_remain_in_the_body(self):
        doc = SKILLS / "hanig-swarm" / "SKILL.md"
        body = " ".join(_body(doc).split())
        for clause in SWARM_BEHAVIOR_CLAUSES:
            with self.subTest(clause=clause):
                self.assertIn(clause, body)


if __name__ == "__main__":
    unittest.main()
