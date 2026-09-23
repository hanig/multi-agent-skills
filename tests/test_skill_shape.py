"""Size, local-reference, and generated declaration shape of authored skills.

The local-reference contract is deliberately file-level: relative inline
Markdown links only, with fragments and queries refused rather than interpreted
as renderer-specific navigation. Swarm declarations come from structured data;
reference prose only needs mechanical modal-to-id ties, not English inference.
"""

from pathlib import Path
import ast
import importlib.util
import json
import os
import re
import shutil
import string
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "skills"
DEFAULT_BODY_LINE_BUDGET = 500
BODY_LINE_BUDGETS = {
    # A separate planned unit owns this already-measured split: ARC-612,
    # which applies the declaration registry that landed for hanig-swarm and
    # RETIRES this entry. Until it lands the number is a holding position.
    #
    # Raised 600 -> 618 on 2026-09-21 because PR #39 added the reporting
    # cadence interview to this body and turned `main` red: the gate passed
    # the change and no suite was run before merging, so nothing caught it.
    # The alternative was cutting behaviour-deciding prose by hand to fit,
    # which is the failure this budget's own unit exists to stop -- kimi, on
    # the committee that designed the registry: "The criterion cannot be
    # sacrificed to the budget. If the full registry does not fit, the budget
    # or the criterion is wrong, not the inclusion of behaviour rules."
    #
    # 618 is the body's EXACT measured length, so any addition trips this:
    # one added line measures 619 and fails, removing it passes.
    # A first attempt set 615 against a 612-line body and described three
    # lines of slack as "so the next addition trips it again"; luna refuted
    # that in one line -- a one-line addition yields 613 and passes. A margin
    # is not a tripwire. There is no margin here.
    "hanig-project": 618,
}
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

ORCHESTRATE_DECLARATIONS = (
    "placement.behavior-deciding",
    "placement.reference-elaboration",
    "placement.reference-dialect",
    "capability.host-policy",
    "capability.dependencies",
    "paths.skill-directory",
    "authority.source",
    "authority.confirmation",
    "authority.narrow-mode",
    "authority.revocation",
    "role.supervision",
    "delegation.whole-loop",
    "delegation.prompt",
    "delegation.configuration",
    "delegation.continuation",
    "retry.boundary",
    "evidence.checkable",
    "evidence.authority",
    "review.panel-source",
    "review.author-exclusion",
    "review.claims",
    "review.honesty",
    "review.cost",
    "review.effort",
    "review.rounds",
    "adjudication.matrix",
    "adjudication.concurrence",
    "adjudication.record",
    "adjudication.nonoverridable",
    "adjudication.honesty",
    "watch.facts",
    "watch.source",
    "watch.proof",
    "loop.quiescence",
    "loop.advance",
    "loop.yield",
    "preservation.before-cleanup",
    "dispatch.mechanics",
    "merge.requirements",
    "tracker.authority",
    "tracker.reconcile",
    "report.three-parts",
    "handoff.contents",
    "handoff.transfer",
    "takeover.verify",
    "limit.session-liveness",
)


def _orchestrate_completeness_problems(skill):
    actual = tuple(item["id"] for item in
                   DECLARATION_REGISTRY.load_registry(skill))
    problems = [
        "missing declaration: " + declaration_id
        for declaration_id in ORCHESTRATE_DECLARATIONS
        if declaration_id not in actual
    ]
    problems.extend(
        "unexpected declaration: " + declaration_id
        for declaration_id in actual
        if declaration_id not in ORCHESTRATE_DECLARATIONS
    )
    if not problems and actual != ORCHESTRATE_DECLARATIONS:
        problems.append("declaration order differs")
    return problems


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

    def test_orchestrate_declaration_block_matches_the_canonical_registry(self):
        skill = SKILLS / "hanig-orchestrate"
        self.assertEqual(DECLARATION_REGISTRY.body_diff(skill), "")
        self.assertEqual(_orchestrate_completeness_problems(skill), [])

    def test_orchestrate_reference_modals_are_tied_to_registered_declarations(self):
        skill = SKILLS / "hanig-orchestrate"
        self.assertEqual(DECLARATION_REGISTRY.reference_problems(skill), [])

    def test_orchestrate_reads_authority_from_the_current_mandate(self):
        skill = SKILLS / "hanig-orchestrate"
        mandate = (ROOT / "docs" / "orchestrator-mandate.md").read_text(
            encoding="utf-8")
        declarations = {
            item["id"]: item["normative_text"]
            for item in DECLARATION_REGISTRY.load_registry(skill)
        }
        authority_sections = (
            "Granted, without asking",
            "Bounded by",
            "Always stop and ask",
        )
        for heading in authority_sections:
            with self.subTest(heading=heading):
                self.assertIn("## " + heading, mandate)
                self.assertIn(heading, declarations["authority.confirmation"])
        for copied_id in (
                "authority.grant", "authority.bounds", "authority.stop"):
            self.assertNotIn(
                copied_id, declarations,
                "the operating skill copied authority that belongs only in "
                "the current mandate")
        self.assertIn("current mandate", declarations["authority.confirmation"])
        self.assertIn("current mandate", declarations["authority.narrow-mode"])

        bundle = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (skill / "SKILL.md", *sorted(
                (skill / "references").glob("*.md")))
        )
        for stale_copy in (
                "The seven granted powers",
                "The six bounds retain",
                "The five stop conditions"):
            self.assertNotIn(stale_copy, bundle)

    def test_orchestrate_carries_dispatch_tracker_and_report_order(self):
        declarations = {
            item["id"]: item["normative_text"]
            for item in DECLARATION_REGISTRY.load_registry(
                SKILLS / "hanig-orchestrate")
        }
        tracker = declarations["tracker.reconcile"]
        for required in (
                "each dispatch as a tracker event", "after each dispatch",
                "move to in progress with its unit", "connector is available",
                "pending synchronization without blocking unrelated dispatch",
                "stopped unshipped attempt", "work was preserved"):
            self.assertIn(required, tracker)
        report = declarations["report.three-parts"]
        self.assertIn("three ordered parts", report)
        self.assertIn("step-three dispatches", report)
        self.assertIn("before writing the report", report)
        self.assertIn("pending synchronization", report)

    def test_orchestrate_installs_and_doctor_calls_it_authored(self):
        names = (
            "hanig-orchestrate",
            "hanig-project",
            "hanig-swarm",
            "hanig-review-gate",
            "hanig-portable-handoff",
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            prefix = root / "skills"
            command = [str(ROOT / "install.sh"), "--prefix", str(prefix)]
            for name in names:
                command.extend(("--only", name))
            installed = subprocess.run(
                command,
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
            )
            self.assertEqual(installed.returncode, 0, installed.stdout)
            self.assertTrue((prefix / "hanig-orchestrate" / "SKILL.md").is_file())

            doctor = subprocess.run(
                ["sh", str(ROOT / "bin" / "doctor"), "--prefix", str(prefix)],
                cwd=str(ROOT),
                env={**dict(os.environ), "HOME": str(root / "home")},
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
            )
            self.assertEqual(doctor.returncode, 0, doctor.stdout)
            authored = [line for line in doctor.stdout.splitlines()
                        if "hanig-orchestrate" in line]
            self.assertEqual(len(authored), 1, doctor.stdout)
            self.assertIn("ours (version", authored[0])

    def test_orchestrate_review_facts_match_the_named_sources(self):
        reference = (SKILLS / "hanig-orchestrate" / "references" /
                     "delegation-evidence.md").read_text(encoding="utf-8")
        config = json.loads(
            (SKILLS / "hanig-review-gate" / "reviewers.json").read_text(
                encoding="utf-8"))
        reviewers = config["reviewers"]
        for profile in ("plan", "fast", "standard", "deep", "committee"):
            names = [reviewer["name"] for reviewer in reviewers
                     if reviewer.get("enabled", True)
                     and profile in (reviewer.get("profiles") or [])]
            published = re.search(
                r"^`{}`: (.+)\.$".format(re.escape(profile)),
                reference,
                re.MULTILINE,
            )
            self.assertIsNotNone(published, profile)
            self.assertCountEqual(
                [name.strip() for name in published.group(1).split(",")],
                names,
            )

        for reviewer in reviewers:
            if not reviewer.get("enabled", True) or not reviewer.get("_cost"):
                continue
            cost = reviewer["_cost"]
            self.assertIn(
                "{} input ${} and output ${}".format(
                    reviewer["name"], cost["in"], cost["out"]),
                reference,
            )
        astra = next(item for item in reviewers if item["name"] == "astra")
        self.assertNotIn("_cost", astra)
        self.assertIn("Astra has no `_cost` record", reference)

        effort_note = config["_effort_null_is_deliberate"]
        for measured in (
                "3 of 3 samples",
                "at effort=low in 2 of 3",
                "4581-character review for $0.016 in 3 of 3",
                "high cost 83% more"):
            self.assertIn(measured, effort_note)
        for published in (
                "3 of 3 high samples",
                "2 of 3 low samples",
                "4581-character review costing $0.016 in 3 of 3",
                "high cost 83 percent more"):
            self.assertIn(published, reference)

    def test_orchestrate_describes_advance_as_the_one_pass_it_is(self):
        source = (SKILLS / "hanig-swarm" / "scripts" / "swarm.py").read_text(
            encoding="utf-8")
        functions = {node.name: node for node in ast.parse(source).body
                     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        body = functions["cmd_advance"].body
        self.assertEqual(len(body), 1)
        self.assertIsInstance(body[0], ast.Return)
        self.assertIsInstance(body[0].value, ast.Call)
        self.assertEqual(body[0].value.func.id, "cmd_run")

        cmd_run = functions["cmd_run"]
        guarded = [node for node in cmd_run.body if isinstance(node, ast.Try)]
        self.assertEqual(len(guarded), 1)
        try_index = cmd_run.body.index(guarded[0])
        acquire_calls = [node for statement in cmd_run.body[:try_index]
                         for node in ast.walk(statement)
                         if isinstance(node, ast.Call)
                         and isinstance(node.func, ast.Name)
                         and node.func.id == "acquire_lease"]
        self.assertEqual(len(acquire_calls), 1)
        advance_calls = [node for node in ast.walk(guarded[0])
                         if isinstance(node, ast.Call)
                         and isinstance(node.func, ast.Name)
                         and node.func.id == "advance"]
        self.assertEqual(len(advance_calls), 1)
        release_calls = [node for node in ast.walk(
            ast.Module(body=guarded[0].finalbody, type_ignores=[]))
                         if isinstance(node, ast.Call)
                         and isinstance(node.func, ast.Name)
                         and node.func.id == "release_lease"]
        self.assertEqual(len(release_calls), 1)
        exiting_prints = [node for node in ast.walk(cmd_run)
                          if isinstance(node, ast.Call)
                          and isinstance(node.func, ast.Name)
                          and node.func.id == "print"
                          and "coordinator exiting" in ast.unparse(node)]
        self.assertEqual(len(exiting_prints), 1)
        loop_reference = (SKILLS / "hanig-orchestrate" / "references" /
                          "operating-loop.md").read_text(encoding="utf-8")
        self.assertIn("calls `advance` once under the state lock", loop_reference)
        self.assertIn("prints that the coordinator is exiting", loop_reference)

    def test_moving_an_orchestrate_rule_to_a_reference_fails_completeness(self):
        with tempfile.TemporaryDirectory() as raw:
            skill = Path(raw) / "hanig-orchestrate"
            shutil.copytree(SKILLS / "hanig-orchestrate", skill)
            registry = skill / "declarations.json"
            data = json.loads(registry.read_text(encoding="utf-8"))
            data["declarations"] = [
                item for item in data["declarations"]
                if item["id"] != "delegation.prompt"
            ]
            registry.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            reference = skill / "references" / "delegation-evidence.md"
            reference.write_text(
                reference.read_text(encoding="utf-8").replace(
                    "declaration: delegation.prompt",
                    "declaration: placement.reference-elaboration") +
                "\nA delegated prompt must still carry the complete rule here. "
                "<!-- declaration: placement.reference-elaboration -->\n",
                encoding="utf-8",
            )
            DECLARATION_REGISTRY.write_body(skill)
            self.assertEqual(DECLARATION_REGISTRY.body_diff(skill), "")
            self.assertEqual(DECLARATION_REGISTRY.reference_problems(skill), [])
            self.assertEqual(
                _orchestrate_completeness_problems(skill),
                ["missing declaration: delegation.prompt"],
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


if __name__ == "__main__":
    unittest.main()
