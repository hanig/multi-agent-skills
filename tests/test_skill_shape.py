"""Size, local-reference, and generated declaration shape of authored skills.

The local-reference contract is deliberately file-level: relative inline
Markdown links only, with fragments and queries refused rather than interpreted
as renderer-specific navigation. Behavior declarations come from structured data;
reference prose only needs mechanical modal-to-id ties, not English inference.
"""

from pathlib import Path
import importlib.util
import json
import re
import shutil
import string
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "skills"
DEFAULT_BODY_LINE_BUDGET = 500
EXTERNAL_MARKDOWN_LINK = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")
MARKDOWN_ESCAPABLE = frozenset(
    r"!\"#$%&'()*+,-./:;<=>?@[\]^_`{|}~\\"
)
MARKDOWN_DELIMITER_WHITESPACE_TEXT = " \t\n\r\f\v"
MARKDOWN_DELIMITER_WHITESPACE = frozenset(MARKDOWN_DELIMITER_WHITESPACE_TEXT)

DECLARATION_SCRIPT = SKILLS / "hanig-swarm" / "scripts" / "declaration_registry.py"
DECLARATION_SPEC = importlib.util.spec_from_file_location(
    "hanig_swarm_declaration_registry", DECLARATION_SCRIPT
)
DECLARATION_REGISTRY = importlib.util.module_from_spec(DECLARATION_SPEC)
DECLARATION_SPEC.loader.exec_module(DECLARATION_REGISTRY)


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
                target = body[begin:index].strip(
                    MARKDOWN_DELIMITER_WHITESPACE_TEXT
                )
                return _markdown_unescape(target), index + 1, False
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


class TestAuthoredSkillShape(unittest.TestCase):
    def test_every_authored_skill_body_is_within_its_line_budget(self):
        docs = _authored_skill_docs()
        self.assertTrue(docs, "the authored-skill sweep matched no files")
        for doc in docs:
            body_lines = len(_body(doc).splitlines())
            with self.subTest(skill=doc.parent.name):
                self.assertLessEqual(
                    body_lines, DEFAULT_BODY_LINE_BUDGET,
                    f"{doc}: authored body has {body_lines} lines; "
                    f"budget is {DEFAULT_BODY_LINE_BUDGET}",
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

    def test_angle_bracket_external_link_with_surrounding_whitespace_is_not_local(self):
        with tempfile.TemporaryDirectory() as raw:
            skill = Path(raw) / "hanig-example"
            skill.mkdir()
            doc = skill / "SKILL.md"
            doc.write_text(
                "---\nname: example\n---\n[x](< https://example.com >)\n",
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
                ["details.md", "details.md", "a(b).md", "a(b).md"],
            )
            self.assertEqual(_local_reference_problems(doc), [])

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

    def test_swarm_declaration_block_matches_the_canonical_registry(self):
        skill = SKILLS / "hanig-swarm"
        self.assertEqual(DECLARATION_REGISTRY.body_diff(skill), "")

    def test_swarm_reference_modals_are_tied_to_registered_declarations(self):
        skill = SKILLS / "hanig-swarm"
        self.assertEqual(DECLARATION_REGISTRY.reference_problems(skill), [])

    def test_project_declaration_block_matches_the_canonical_registry(self):
        skill = SKILLS / "hanig-project"
        self.assertEqual(DECLARATION_REGISTRY.body_diff(skill), "")

    def test_project_reference_modals_are_tied_to_registered_declarations(self):
        skill = SKILLS / "hanig-project"
        self.assertEqual(DECLARATION_REGISTRY.reference_problems(skill), [])

    def test_project_keeps_partition_routing_as_owner_judgment(self):
        declarations = {
            item["id"]: item["normative_text"]
            for item in DECLARATION_REGISTRY.load_registry(
                SKILLS / "hanig-project"
            )
        }
        self.assertIn(
            "ask the owner whether CPU-only work may run",
            declarations["cluster.account-allowance"],
        )

    def test_project_keeps_active_host_policy_in_the_registry(self):
        declarations = {
            item["id"]: item["normative_text"]
            for item in DECLARATION_REGISTRY.load_registry(
                SKILLS / "hanig-project"
            )
        }
        self.assertIn(
            "Follow the active host's discovered project instructions",
            declarations["capability.host-policy"],
        )

    def test_project_registry_keeps_interview_speech_acts(self):
        declarations = {
            item["id"]: item["normative_text"]
            for item in DECLARATION_REGISTRY.load_registry(
                SKILLS / "hanig-project"
            )
        }
        required = {
            "repository.destination": "State the adopted remote and branch",
            "survey.partition-state": "report an unknown state",
            "cluster.account-allowance": "named denial is not a question",
            "cluster.memory-charging": "recommend shrinking per-job memory",
            "plan.docs-protection": "tell the owner",
            "closure.by-kind": "merge observations are attested",
            "code.configuration": "confirm the selected provider's exact mode spelling",
            "capability.tracker": "report the pending synchronization",
            "findings.bound": "reason for each",
            "closure.evidence": "report an issue closed without it as an integrity violation",
            "judgment.by-kind": "coordinator-pinned pre-dispatch artifact basis",
        }
        for declaration_id, speech_act in required.items():
            with self.subTest(declaration=declaration_id):
                self.assertIn(speech_act, declarations[declaration_id])
        self.assertIn(
            "coordinator does not validate the mode",
            declarations["code.configuration"],
        )
        self.assertNotIn(
            "dispatch must refuse unsupported provider-mode pairs",
            declarations["code.configuration"],
        )

    def test_an_unregistered_reference_imperative_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            skill = Path(raw) / "hanig-swarm"
            shutil.copytree(SKILLS / "hanig-swarm", skill)
            reference = skill / "references" / "field-evidence.md"
            reference.write_text(
                "# Fixture\nA new dispatch rule must stay hidden here.\n",
                encoding="utf-8",
            )
            self.assertEqual(
                DECLARATION_REGISTRY.reference_problems(skill),
                ["references/field-evidence.md:2: "
                 "imperative modal lacks a declaration marker"],
            )

    def test_ambiguous_reference_structures_are_refused_not_parsed(self):
        with tempfile.TemporaryDirectory() as raw:
            skill = Path(raw) / "hanig-swarm"
            shutil.copytree(SKILLS / "hanig-swarm", skill)
            reference = skill / "references" / "field-evidence.md"
            cases = {
                "    indented prose": "indentation of four or more spaces",
                "\ttabbed prose": "tab outside a fenced code block",
                "<!-- comment\twith a tab -->":
                    "tab outside a fenced code block",
                "A tied rule must remain. "
                "<!-- declaration:\tscheduler.queued-job -->":
                    "tab outside a fenced code block",
                "> quote": "blockquote",
                "  - nested item": "nested list",
                "- ```text": "noncanonical fence",
                "  ```text": "noncanonical fence",
                "<div>raw HTML</div>": "raw HTML",
                "Prose <span>inline raw HTML</span>.": "raw HTML",
                "<div": "raw HTML",
                "<!doctype html>": "raw HTML",
                '<a title="1<2">': "raw HTML",
                "---": "thematic break or Setext heading",
                "  -": "thematic break or Setext heading",
                "  --": "thematic break or Setext heading",
                "  ===": "thematic break or Setext heading",
                "  [label]: target": "link definition",
                "-bad list marker": "list marker must use one space",
                "<!-- unterminated comment": "malformed or mixed HTML comment",
                "- - -": "ambiguous or empty prose content",
            }
            for line, message in cases.items():
                with self.subTest(line=line):
                    reference.write_text(line + "\n", encoding="utf-8")
                    self.assertEqual(
                        DECLARATION_REGISTRY.reference_problems(skill),
                        ["references/field-evidence.md:1: "
                        "unsupported reference syntax: " + message],
                    )

    def test_reference_prose_start_grammar_is_total_over_printable_ascii(self):
        prefixes = ("", "- ", "1. ", "  ", "# ")
        characters = string.printable.replace("\n", "").replace("\r", "")
        characters = characters.replace("\v", "").replace("\f", "")
        for prefix in prefixes:
            for character in characters:
                line = prefix + character + "x"
                expected = character not in DECLARATION_REGISTRY.PROSE_START
                with self.subTest(prefix=prefix, character=repr(character)):
                    self.assertEqual(
                        DECLARATION_REGISTRY._outside_fence_syntax_problem(line)
                        is not None,
                        expected,
                    )

    def test_structures_at_prose_content_position_are_refused(self):
        cases = (
            "- - nested",
            "+ + nested",
            "1. - nested",
            "- 1. nested",
            "1. 2. nested",
            "- > quote",
            "-     code",
            "- # heading",
            "- [label]: target",
            "+ - - -",
            "1. * * *",
        )
        for line in cases:
            with self.subTest(line=line):
                self.assertIsNotNone(
                    DECLARATION_REGISTRY._outside_fence_syntax_problem(line)
                )

    def test_numbers_are_prose_but_ordered_markers_at_content_are_refused(self):
        refused = ("1.", "- 1. nested", "1. 2) nested", "  3. nested")
        accepted = ("3 items", "- 3 items", "# 1. Overview", "1234567890. x")
        for line in refused:
            with self.subTest(line=line):
                self.assertIsNotNone(
                    DECLARATION_REGISTRY._outside_fence_syntax_problem(line)
                )
        for line in accepted:
            with self.subTest(line=line):
                self.assertIsNone(
                    DECLARATION_REGISTRY._outside_fence_syntax_problem(line)
                )

    def test_reference_name_grammar_is_total_over_printable_ascii(self):
        safe_middle = frozenset(string.ascii_lowercase + string.digits + "-")
        for character in string.printable:
            reference = "references/a{}b.md".format(character)
            with self.subTest(character=repr(character)):
                self.assertEqual(
                    bool(DECLARATION_REGISTRY.REFERENCE_PATTERN.fullmatch(
                        reference
                    )),
                    character in safe_middle,
                )

    def test_an_unserializable_reference_name_is_refused_at_the_registry(self):
        with tempfile.TemporaryDirectory() as raw:
            skill = Path(raw) / "hanig-swarm"
            shutil.copytree(SKILLS / "hanig-swarm", skill)
            unsafe = skill / "references" / "foo bar.md"
            unsafe.write_text("Measured note.\n", encoding="utf-8")
            registry_path = skill / "declarations.json"
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            registry["declarations"][0]["references"] = [
                "references/foo bar.md"
            ]
            registry_path.write_text(json.dumps(registry), encoding="utf-8")
            with self.assertRaisesRegex(
                    DECLARATION_REGISTRY.RegistryError,
                    "has an invalid reference: references/foo bar.md"):
                DECLARATION_REGISTRY.load_registry(skill)

    def test_reference_existence_is_byte_exact_on_case_folding_filesystems(self):
        with tempfile.TemporaryDirectory() as raw:
            skill = Path(raw) / "hanig-swarm"
            shutil.copytree(SKILLS / "hanig-swarm", skill)
            lower = skill / "references" / "limits.md"
            lower.rename(skill / "references" / "Limits.md")
            with self.assertRaisesRegex(
                    DECLARATION_REGISTRY.RegistryError,
                    "reference is absent: references/limits.md"):
                DECLARATION_REGISTRY.load_registry(skill)

    def test_registry_reference_symlink_to_identical_external_file_is_refused(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            skill = root / "hanig-swarm"
            shutil.copytree(SKILLS / "hanig-swarm", skill)
            reference = skill / "references" / "limits.md"
            outside = root / "outside-limits.md"
            outside.write_bytes(reference.read_bytes())
            reference.unlink()
            reference.symlink_to(outside)
            with self.assertRaisesRegex(
                    DECLARATION_REGISTRY.RegistryError,
                    "reference is absent: references/limits.md"):
                DECLARATION_REGISTRY.load_registry(skill)

    def test_registry_reference_names_roundtrip_through_generated_links(self):
        declarations = DECLARATION_REGISTRY.load_registry(
            SKILLS / "hanig-swarm"
        )
        expected = [
            reference
            for declaration in declarations
            for reference in declaration["references"]
        ]
        self.assertEqual(
            list(_inline_markdown_targets(
                DECLARATION_REGISTRY.render_block(declarations)
            )),
            expected,
        )

    def test_canonical_fences_and_comments_are_explicit_exclusions(self):
        with tempfile.TemporaryDirectory() as raw:
            skill = Path(raw) / "hanig-swarm"
            shutil.copytree(SKILLS / "hanig-swarm", skill)
            reference = skill / "references" / "capability-fallbacks.md"
            reference.write_text(
                "# Fixture\n"
                "```text\n\tThis must remain example text.\n"
                "<div>Example raw HTML must remain literal.</div>\n"
                "<!-- declaration: not.authority.in.code -->\n````\n"
                "<!-- Maintainers must remember this example. -->\n"
                "A real rule must be tied. "
                "<!-- declaration: capability.shell-filesystem -->\n",
                encoding="utf-8",
            )
            self.assertEqual(DECLARATION_REGISTRY.reference_problems(skill), [])

    def test_noncanonical_closer_cannot_swallow_following_prose(self):
        with tempfile.TemporaryDirectory() as raw:
            skill = Path(raw) / "hanig-swarm"
            shutil.copytree(SKILLS / "hanig-swarm", skill)
            reference = skill / "references" / "field-evidence.md"
            reference.write_text(
                "```text\nThis must remain example text.\n  ```\n"
                "This rule must still be checked.\n",
                encoding="utf-8",
            )
            self.assertEqual(
                DECLARATION_REGISTRY.reference_problems(skill),
                ["references/field-evidence.md:3: unsupported reference syntax: "
                 "fence closer must start at column zero",
                 "references/field-evidence.md:4: "
                 "imperative modal lacks a declaration marker"],
            )

    def test_unclosed_fence_is_refused(self):
        with tempfile.TemporaryDirectory() as raw:
            skill = Path(raw) / "hanig-swarm"
            shutil.copytree(SKILLS / "hanig-swarm", skill)
            reference = skill / "references" / "field-evidence.md"
            reference.write_text(
                "# Fixture\n```text\nThis must remain example text.\n",
                encoding="utf-8",
            )
            self.assertEqual(
                DECLARATION_REGISTRY.reference_problems(skill),
                ["references/field-evidence.md:2: unsupported reference syntax: "
                 "unclosed fenced code block"],
            )

    def test_declaration_markers_are_validated_even_without_a_modal(self):
        with tempfile.TemporaryDirectory() as raw:
            skill = Path(raw) / "hanig-swarm"
            shutil.copytree(SKILLS / "hanig-swarm", skill)
            reference = skill / "references" / "field-evidence.md"
            reference.write_text(
                "Measured note. <!-- declaration: unknown.id -->\n"
                "Another note. <!-- declaration: capability.shell-filesystem -->\n",
                encoding="utf-8",
            )
            self.assertEqual(
                DECLARATION_REGISTRY.reference_problems(skill),
                ["references/field-evidence.md:1: unknown declaration id unknown.id",
                 "references/field-evidence.md:2: declaration "
                 "capability.shell-filesystem does not point to this reference"],
            )

    def test_declaration_marker_on_an_adjacent_line_has_no_authority(self):
        with tempfile.TemporaryDirectory() as raw:
            skill = Path(raw) / "hanig-swarm"
            shutil.copytree(SKILLS / "hanig-swarm", skill)
            reference = skill / "references" / "field-evidence.md"
            reference.write_text(
                "A rule must be tied on this line.\n"
                "<!-- declaration: scheduler.queued-job -->\n",
                encoding="utf-8",
            )
            self.assertEqual(
                DECLARATION_REGISTRY.reference_problems(skill),
                ["references/field-evidence.md:1: imperative modal lacks a "
                 "declaration marker",
                 "references/field-evidence.md:2: unsupported reference syntax: "
                 "declaration marker must follow prose on the same line"],
            )


GENERATED_BEGIN = "<!-- BEGIN GENERATED DECLARATIONS"
GENERATED_END = "<!-- END GENERATED DECLARATIONS -->"
FRONTMATTER = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)

# How many of the registry's own interview topics may appear in one
# authored sentence before it is a second copy of the list. The honest
# tree's maximum is ONE; the three paraphrases three reviewers wrote to
# defeat the previous rule score six and seven. Three is far from both.
SECOND_COPY_TOPICS = 3

# What may sit between two items of a LIST: punctuation, one
# conjunction, an article. Anything else and they are two mentions in a
# sentence, not two entries in a list.
LIST_SEPARATOR = re.compile(r"^[\s,;:]*(?:and|or|plus|then)?[\s,;:]*(?:the\s+)?$")


class TestDeclarationsDoNotSilentlyLeave(unittest.TestCase):
    """A statement can leave the decision surface and nothing notices.

    The registry has two enforcement checks and neither sees a deletion:
    body-generation compares the body to the registry, and reference-drift
    polices references. If a declaration is removed from the registry, both
    agree perfectly with each other about a surface that no longer carries
    the rule.

    That is not hypothetical. This branch's own restructuring deleted the
    reporting-cadence interview -- a statement that changes what the agent
    ASKS and WRITES, which is the registry's own definition of
    behaviour-deciding -- and it survived in no declaration, no reference
    and no body text. Three reviewers found it independently; no test did.

    The first version of this pinned seven ids and called itself "a floor,
    not a schema". luna and glm-5.3 both refused that: sixty of the
    sixty-seven declarations could still be deleted with every check green,
    including `closure.evidence`, the rule that refuses a close without
    evidence. A floor that omits most of the building is not a floor. So
    the whole set is pinned.

    This is a SNAPSHOT, not a schema. Adding a declaration fails this list
    too, which is the point: the list is the place a reader looks to see
    what the skill decides, and changing that set should be a deliberate
    edit with a reason in the commit, not a side effect of regenerating a
    body.
    """

    DECLARED = {
        "hanig-project": (
            "placement.behavior-deciding",
            "placement.reference-elaboration",
            "placement.reference-dialect",
            "capability.host-policy",
            "capability.tracker",
            "capability.install-boundary",
            "paths.skill-directory",
            "workflow.order",
            "survey.read-before-ask",
            "survey.incomplete-walk",
            "survey.partition-state",
            "adoption.context",
            "repository.destination",
            "repository.source-data",
            "repository.creation-approval",
            "interview.judgment-only",
            "interview.retry-boundary",
            "interview.dispatch-complete",
            "interview.reporting-cadence",
            "plan.inputs",
            "plan.scheduler-route",
            "plan.promotion",
            "code.configuration",
            "code.target-branch",
            "runtime.contract",
            "retry.contract",
            "cluster.memory-flag",
            "cluster.account-allowance",
            "cluster.memory-charging",
            "cluster.qos-scope",
            "findings.interview",
            "unit.retry-size",
            "judgment.by-kind",
            "slurm.command-boundary",
            "pipeline.command-boundary",
            "code.prompt-boundary",
            "outputs.attempt-relative",
            "code.default-agent",
            "slurm.array-outputs",
            "plan.required-fields",
            "code.write-scopes",
            "code.worktree-isolation",
            "plan.docs-protection",
            "plan.human-document",
            "plan.validate",
            "tracker.team",
            "tracker.credential-boundary",
            "tracker.approval",
            "tracker.autopilot",
            "tracker.apply",
            "tracker.edges",
            "tracker.readback-shape",
            "tracker.attestation",
            "tracker.check",
            "dispatch.sequence",
            "drain.authority",
            "closure.evidence",
            "closure.by-kind",
            "drain.block-intent",
            "outbox.receipt",
            "outbox.idempotency",
            "report.required",
            "report.evidence-source",
            "report.contents",
            "findings.contract",
            "findings.bound",
            "adoption.remaining-work",
        ),
    }

    @staticmethod
    def second_copies(named_sentences, topics):
        """Sentences that LIST enough of TOPICS to be a copy of the list.

        Counting topics was not enough. kimi-k2.7-code: "The budget for
        protected destinations determines retry exposure and reporting
        cadence" carries four and is honest prose, so a count alone
        fails an honest run -- the direction this test must never err
        in, because it reddens correct work.

        What separates a list from prose is what sits BETWEEN the
        items. In a list it is punctuation and at most a conjunction;
        in prose it is other words. So the topics must be adjacent:
        three or more in a row with nothing but separators between
        them.
        """
        found = []
        for name, sentences in named_sentences:
            for sentence in sentences:
                lowered = sentence.lower()
                hits = sorted((lowered.index(t), t)
                              for t in topics if t in lowered)
                run, best, listed = 1, 1, []
                for (start, topic), (nxt, _) in zip(hits, hits[1:]):
                    between = lowered[start + len(topic):nxt]
                    if LIST_SEPARATOR.match(between):
                        run += 1
                    else:
                        run = 1
                    best = max(best, run)
                if best >= SECOND_COPY_TOPICS:
                    listed = [t for _p, t in hits]
                    found.append((name, ", ".join(listed), sentence))
        return found

    @staticmethod
    def interview_topics(enumeration):
        """The topics `interview.judgment-only` itself names.

        Read out of the declaration rather than written down here, so
        this test has no second copy of the list either -- which would
        be the same defect it exists to catch.
        """
        listing = enumeration.split("At least:", 1)[1].split(". ", 1)[0]
        topics = []
        for piece in re.split(r",|\band\b", listing):
            topic = re.sub(r"^the ", "", piece.strip().strip(".").lower())
            if len(topic) > 3:
                topics.append(topic)
        return topics

    def authored_surfaces(self):
        """Every hanig-project surface a human wrote, generated block cut."""
        skill = SKILLS / "hanig-project"
        return [skill / "SKILL.md"] + sorted(
            (skill / "references").glob("*.md"))

    @staticmethod
    def sentences(surface):
        """Sentences of the AUTHORED text: no frontmatter, no generated
        block.

        The frontmatter is metadata rather than guidance -- its
        `description` is a when-to-use list and legitimately reads as one
        -- and the generated block is derived from the registry, so
        holding it to a rule about second copies would fail the first one.
        """
        text = surface.read_text()
        if GENERATED_BEGIN in text and GENERATED_END in text:
            head = text[:text.index(GENERATED_BEGIN)]
            tail = text[text.index(GENERATED_END) + len(GENERATED_END):]
            text = head + "\n" + tail
        # The frontmatter is SCANNED now. It was stripped because an
        # earlier rule keyed on the word "ask", and the description's
        # when-to-use list tripped it. The rule keys on the registry's
        # topics now, which the description does not contain, so the
        # exemption bought nothing and left a surface a second copy
        # could hide in -- luna.
        return re.split(r"(?<=[.!?])\s+", " ".join(text.split()))

    def test_the_declared_set_is_exactly_what_is_pinned(self):
        for skill, ids in self.DECLARED.items():
            registry = SKILLS / skill / "declarations.json"
            with open(registry) as handle:
                declared = [entry["id"]
                            for entry in json.load(handle)["declarations"]]
            with self.subTest(skill=skill):
                self.assertEqual(
                    sorted(declared), sorted(ids),
                    "%s's declaration set changed. Neither registry check "
                    "can see a declaration leave, so this snapshot is the "
                    "only thing that can: update it in the same commit and "
                    "say in the message what was added or dropped and why."
                    % skill)
                self.assertEqual(
                    len(declared), len(set(declared)),
                    "%s declares the same id twice" % skill)

    def test_required_declaration_ids_are_present(self):
        for skill, ids in self.DECLARED.items():
            registry = SKILLS / skill / "declarations.json"
            with open(registry) as handle:
                declared = {entry["id"]
                            for entry in json.load(handle)["declarations"]}
            for required in ids:
                with self.subTest(skill=skill, declaration=required):
                    self.assertIn(
                        required, declared,
                        "%s left %s's registry. If it was replaced, name the "
                        "replacement here; if it was dropped, say why in the "
                        "commit, because the two registry checks cannot see "
                        "a deletion." % (required, skill))

    def test_each_required_declaration_reaches_the_generated_body(self):
        """Present in the registry is not present in the body."""
        for skill, ids in self.DECLARED.items():
            text = (SKILLS / skill / "SKILL.md").read_text()
            for required in ids:
                with self.subTest(skill=skill, declaration=required):
                    self.assertIn("`%s`:" % required, text)

    def test_the_dialect_declaration_matches_what_the_checker_does(self):
        """The declaration says what is refused; this checks each one.

        kimi-k2.7-code read "raw HTML ... are refused" as covering the
        `<!-- declaration: id -->` markers the reference files are full
        of, and posed a dilemma: either the checker flags every
        reference file, or it silently exempts HTML and the declaration
        is a false promise. Neither holds -- the sentence's first clause
        permits valid same-line comments and its second refuses OTHER
        raw HTML.

        Then, the round after: only the HTML half was exercised, while
        the declaration names five more forms. "An invariant written in
        prose is not an invariant" and a declaration is prose, so every
        form it promises to refuse is planted here and every one must be
        caught. Planted in a COPY, because the first version edited the
        tracked source file and restored it, which glm-5.3 noted races
        with a concurrent run and leaves the file mutated if one is
        killed.
        """
        skill = SKILLS / "hanig-project"
        self.assertEqual(DECLARATION_REGISTRY.reference_problems(skill), [],
                         "the markers the files already use must be accepted")

        refused = (
            ("raw HTML", "<div>raw html</div>"),
            ("tab", "\tA tab-indented behaviour-deciding line."),
            ("indentation of four or more spaces",
             "    A four-space indented line."),
            ("blockquote", "> A quoted behaviour-deciding line."),
            ("prose indentation must be two spaces",
             " A one-space indented line."),
            ("nested list", "  - a nested list item"),
        )
        for expected, planted in refused:
            with self.subTest(form=expected):
                with tempfile.TemporaryDirectory() as tmp:
                    copy = Path(tmp) / "hanig-project"
                    shutil.copytree(skill, copy)
                    reference = copy / "references" / "unit-contract.md"
                    reference.write_text(
                        reference.read_text() + "\n" + planted + "\n")
                    problems = DECLARATION_REGISTRY.reference_problems(copy)
                self.assertTrue(
                    any(expected in problem for problem in problems),
                    "placement.reference-dialect promises %r is refused; "
                    "the checker said %r" % (expected, problems))

        # And the copy itself is clean before anything is planted, so a
        # subTest failure above means the planted line, not the copy.
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "hanig-project"
            shutil.copytree(skill, copy)
            self.assertEqual(DECLARATION_REGISTRY.reference_problems(copy), [])

    def test_declarations_do_not_contradict_each_other(self):
        """Two declarations I wrote disagreed, and nothing noticed.

        luna: `interview.judgment-only` enumerated the interview's
        categories as if exhaustive, and the enumeration omitted the
        reporting cadence that `interview.reporting-cadence` separately
        requires. An agent following the first skips the second.

        There is no general contradiction checker and this does not build
        one. It pins the specific pairing: an enumeration of what to ask
        must name every topic another declaration makes mandatory, or say
        it is not exhaustive.
        """
        with open(SKILLS / "hanig-project" / "declarations.json") as handle:
            declared = {entry["id"]: entry["normative_text"]
                        for entry in json.load(handle)["declarations"]}

        enumeration = declared["interview.judgment-only"]
        self.assertIn(
            "cadence", enumeration.lower(),
            "the interview enumeration omits the reporting cadence that "
            "interview.reporting-cadence separately requires")
        self.assertIn(
            "not an exhaustive", enumeration.lower(),
            "an enumeration that reads as exhaustive must say it is not, "
            "or the next mandatory topic silently falls outside it")

        # ONE COPY -- detected by the registry's own topics, not by any
        # pattern I write.
        #
        # Three rounds of this test looked for English and three rounds
        # of reviewers wrote English that missed it. First the literal
        # phrases "done criteria" and "protected destinations" (luna
        # paraphrased them). Then any sentence that both asks and lists
        # five items -- luna, kimi-k2.7-code and glm-5.3 independently
        # produced "Question the owner, one at a time, about ...", which
        # has no "ask", and a semicolon list, which has no commas. Each
        # round I widened the pattern and the next round walked past it,
        # which is the same losing move recorded in the tracker hook's
        # header for the same reason.
        #
        # So the needles come from `interview.judgment-only` itself. A
        # second copy of the list is a sentence carrying several of the
        # topics the list names, whatever verb introduces it and whatever
        # punctuation separates them. It cannot be paraphrased around
        # without changing the topic words -- at which point it is a
        # different list saying different things, which no test can
        # police and the registry does not claim to. And it tightens by
        # itself: add a topic to the declaration and the needle set grows
        # with it.
        topics = self.interview_topics(enumeration)
        self.assertGreaterEqual(len(topics), 5,
                                "the topic list did not parse: %r" % (topics,))

        # The needles must FOLLOW the declaration, not merely equal it
        # today. luna, kimi-k2.7-code and glm-5.3 all made the same
        # point in one round: my mutation replaced the parser with a
        # one-item list, which trips the length check, but replacing it
        # with a hardcoded copy of the same seven topics passes
        # everything -- and a hardcoded copy is the second copy this
        # whole test exists to forbid. So the declaration is perturbed
        # and the output has to move with it.
        moved = enumeration.replace("budget", "spending ceiling")
        self.assertNotEqual(moved, enumeration, "the perturbation missed")
        perturbed = self.interview_topics(moved)
        self.assertIn("spending ceiling", perturbed,
                      "the needles are not read from the declaration")
        self.assertNotIn("budget", perturbed)
        self.assertEqual(len(perturbed), len(topics))

        named = [(surface.name, self.sentences(surface))
                 for surface in self.authored_surfaces()]
        self.assertEqual(
            self.second_copies(named, topics), [],
            "the interview topic list has a second copy outside the "
            "generated declaration block. The topics have one home, "
            "interview.judgment-only; a second copy is how the reporting "
            "cadence left one surface while surviving in another.")

        # The rule must also FIRE. Disabling the threshold left the suite
        # green until this case existed, which is the shape this whole
        # branch is about: a check whose only evidence is that it has not
        # complained. Every string here is a paraphrase a reviewer wrote
        # to walk past an earlier version of this test.
        # The FRONTMATTER is part of the surface. luna: it was
        # stripped, so a second copy could sit in the description and
        # never be looked at. Asserted directly, because a mutation
        # restoring the strip is invisible while no real frontmatter
        # carries topics.
        skill_md = SKILLS / "hanig-project" / "SKILL.md"
        scanned = " ".join(self.sentences(skill_md))
        self.assertIn(
            "Start a swarm project", scanned,
            "the frontmatter is not being scanned, so a second copy "
            "could hide there")
        planted = list(self.sentences(skill_md)) + [
            "Ask about done criteria, the scientific claim, discardable "
            "work, budget, protected destinations, retry exposure, and "
            "reporting cadence."]
        self.assertTrue(
            self.second_copies([("SKILL.md", planted)], topics),
            "a second copy in the scanned text was not detected")

        # Honest prose carrying the topics in unrelated grammatical
        # roles must NOT be flagged. kimi-k2.7-code wrote the first of
        # these to refute the honest-run claim, and it did.
        for label, honest in (
                ("topics in unrelated roles",
                 "The budget for protected destinations determines retry "
                 "exposure and reporting cadence."),
                ("two mentions in one sentence",
                 "A unit with a budget must declare retry exposure, and "
                 "its protected destinations are surveyed rather than "
                 "asked about."),
        ):
            with self.subTest(honest=label):
                self.assertEqual(
                    self.second_copies([("planted.md", [honest])], topics),
                    [],
                    "honest prose was read as a second copy of the list")

        for label, paraphrase in (
                ("no ask verb",
                 "Question the owner, one at a time, about done criteria, "
                 "the scientific claim, discardable work, budget, protected "
                 "destinations, retry exposure, and reporting cadence."),
                ("semicolons instead of commas",
                 "Interview coverage: completion criteria; scientific claim; "
                 "discardable work; budget; protected destinations; retry "
                 "exposure; reporting cadence."),
                ("a different verb",
                 "The interview must cover: done criteria, the scientific "
                 "claim, discardable work, budget, protected destinations, "
                 "retry exposure, and reporting cadence."),
        ):
            with self.subTest(paraphrase=label):
                self.assertTrue(
                    self.second_copies([("planted.md", [paraphrase])], topics),
                    "a second copy phrased as %r was not detected" % label)

    def test_every_declaration_elaboration_mentions_its_subject(self):
        """glm-5.3: the cadence declaration pointed at a reference that
        contained no cadence content at all, so following the link for
        guidance found a category list that omitted it.

        A narrow check, not a general one: a reference named as a
        declaration's elaboration must carry at least one line tied to
        that declaration's id.
        """
        skill = SKILLS / "hanig-project"
        with open(skill / "declarations.json") as handle:
            entries = json.load(handle)["declarations"]
        for entry in entries:
            for relative in entry.get("references", ()):
                path = skill / relative
                with self.subTest(declaration=entry["id"], ref=relative):
                    self.assertIn(
                        "<!-- declaration: %s -->" % entry["id"],
                        path.read_text(),
                        "%s names %s as its elaboration, but that file ties "
                        "no line to it" % (entry["id"], relative))

    def test_normative_text_is_grammatical_where_it_was_not(self):
        """kimi-k2.7-code found two noun-adjunct ambiguities.

        "judgment inspection cannot settle" parses as a compound noun
        rather than "judgment THAT inspection cannot settle", and "grants
        a worker coordinator authority" as "a worker-coordinator role"
        rather than "a worker the coordinator's authority". A normative
        sentence that can be parsed two ways states two rules.
        """
        with open(SKILLS / "hanig-project" / "declarations.json") as handle:
            declared = {entry["id"]: entry["normative_text"]
                        for entry in json.load(handle)["declarations"]}
        self.assertNotIn("judgment inspection cannot",
                         declared["interview.judgment-only"])
        self.assertNotIn("a worker coordinator authority",
                         declared["capability.install-boundary"])
        self.assertIn("coordinator's authority",
                      declared["capability.install-boundary"])


if __name__ == "__main__":
    unittest.main()
