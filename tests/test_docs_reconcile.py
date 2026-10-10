"""Guard retired tracker instructions and the audit's current-status citations."""
import ast
import json
from pathlib import Path
import re
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
AUDIT = "docs/audit-protocol-enforcement.md"
PROTOCOL_PATH = "docs/tracker-outbox.md"
PROTOCOL_PARAGRAPH = """MCP core is stateless as of the 2026-07-28 specification and tasks are an
extension. A drain must not infer an initialization sequence or task model from
the word “MCP.” A2A 1.0.0 also has asynchronous task lifecycle states;
`TASK_STATE_COMPLETED` means that a remote task lifecycle settled, not that the
requested tracker mutation is present. Both protocols are connector details
outside the coordinator."""

# This is a finite regression catalog, not a classifier for future prose.
# Source entries come from the recorded base; regression entries retain every
# positive fixture and reproduced writer sentence from review. Whole textual
# units distinguish an instruction from a longer read-only or negated sentence.
# New paraphrases require inspection in the manual MCP/connector sweep.
CATALOG_PATH = ROOT / "tests" / "fixtures" / "retired_tracker_instructions.json"


def text_units(text):
    """Compare complete sentences within paragraphs/cells, ignoring soft wraps.

    Strip common presentation wrappers only. Semicolons stay inside sentences;
    no verb lists, read/write inference, negation rules or substring matching.
    """
    text = re.sub(r"<!-- declaration: [a-z0-9.-]+ -->", "", text, flags=re.DOTALL)
    text = re.sub(r"(?m)^[ \t]*```[^\n]*$", "", text)
    text = re.sub(r"(?m)^[ \t]*(?:[-+*] +|[0-9]+[.)] +)", "\n\n", text)
    text = re.sub(r"(?m)^[ \t]*(?:#{1,6} +|> *)", "", text)
    text = text.replace("`", "").replace("**", "")
    for paragraph in re.split(r"\n[ \t]*\n", text):
        for cell in paragraph.split("|"):
            for sentence in re.split(r"(?<=[.!?])\s+", cell.strip()):
                unit = " ".join(sentence.split()).strip(" *")
                if unit:
                    yield unit


def string_values(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from string_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from string_values(item)


def catalog():
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def authored_documents(root):
    """Include root instructions, all docs and authored skills, never tests/vendors."""
    paths = set(root.glob("*.md")) | set((root / "docs").rglob("*.md"))
    for skill in (root / "skills").glob("hanig-*"):
        paths.update(skill.rglob("*.md"))
        registry = skill / "declarations.json"
        if registry.exists():
            paths.add(registry)
    return sorted(paths)


def retired_wording(root):
    retired = {unit for entry in catalog() for unit in text_units(entry["text"])}
    problems = []
    for path in authored_documents(root):
        relative = path.relative_to(root).as_posix()
        body = path.read_text(encoding="utf-8")
        if relative == PROTOCOL_PATH:
            # Exempt these exact bytes, not the whole file or a mutable section.
            body = body.replace(PROTOCOL_PARAGRAPH, "")
        texts = (string_values(json.loads(body)) if path.suffix == ".json"
                 else [body])
        for text in texts:
            for unit in text_units(text):
                if unit in retired:
                    problems.append(relative + ": " + unit)
    return problems


def status_section(body):
    match = re.search(r"\A## Status as of \d{4}-\d{2}-\d{2}\n(.*?)(?=\n## )",
                      body, re.DOTALL)
    if not match:
        raise ValueError("missing dated status section at the top")
    return match[1]


def citation_problems(root, section):
    """Resolve files and qualified Python symbols without importing test modules."""
    problems = []
    citations = re.findall(r"`([^`\s]+\.(?:py|md|json|yml)(?:::[^`\s]+)?)`",
                           section)
    for citation in citations:
        filename, separator, symbol = citation.partition("::")
        path = root / filename
        if not path.is_file():
            problems.append("missing citation file: " + citation)
            continue
        if not separator:
            continue
        if path.suffix != ".py":
            problems.append("non-Python symbol citation: " + citation)
            continue
        nodes = ast.parse(path.read_text(encoding="utf-8")).body
        for part in symbol.split("."):
            found = next((node for node in nodes if
                          isinstance(node, (ast.ClassDef, ast.FunctionDef,
                                            ast.AsyncFunctionDef))
                          and node.name == part), None)
            assigned = any(isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == part
                for target in node.targets) for node in nodes)
            if found is None and not (assigned and part == symbol.split(".")[-1]):
                problems.append("missing citation symbol: " + citation)
                break
            nodes = found.body if found else []
    return problems


class TestDocumentationReconciliation(unittest.TestCase):
    def test_no_retired_tracker_write_wording_in_authored_documents(self):
        self.assertEqual(retired_wording(ROOT), [])

    def test_only_the_original_protocol_paragraph_is_exempt(self):
        self.assertEqual((ROOT / PROTOCOL_PATH).read_text(
            encoding="utf-8").count(PROTOCOL_PARAGRAPH), 1)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / PROTOCOL_PATH
            path.parent.mkdir()
            path.write_text(PROTOCOL_PARAGRAPH +
                            "\n\nA session with MCP drains them.\n",
                            encoding="utf-8")
            self.assertEqual(len(retired_wording(root)), 1)

    def test_scanner_covers_added_scope_and_wrapped_retired_sentences(self):
        samples = {
            "README.md": "A session that has MCP drains them.",
            "CLAUDE.md": "A session drains through the existing connector path.",
            "docs/new.md": "Use the connector available to the current session.",
            "skills/hanig-project/SKILL.md": "In the session with the\nconnector: file it.",
            "skills/hanig-project/declarations.json": '{"text": "File through the Linear MCP connector."}',
            "skills/hanig-swarm/SKILL.md": "Drain through the authorized connector.",
            "skills/hanig-swarm/declarations.json": '{"text": "A session with MCP drains them."}',
            "skills/hanig-orchestrate/references/new.md": "The connector applies it when available.",
            PROTOCOL_PATH: "Existing connector draining remains the write path.",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, text in samples.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            problems = retired_wording(root)
            self.assertEqual({p.split(":", 1)[0] for p in problems}, set(samples))
            vendor = root / "skills/paseo/SKILL.md"
            vendor.parent.mkdir(parents=True)
            vendor.write_text("A session with MCP drains them.", encoding="utf-8")
            self.assertEqual(retired_wording(root), problems)

    def test_read_only_connector_instructions_are_admissible(self):
        samples = (
            "Use the connector available to the current session to read an issue's status; do not mutate it.",
            "Read an issue's status through the authorized connector.",
            "Query tracker dependencies through the Linear MCP connector.",
            "In the session with the connector, inspect the issue's status.",
            "The MCP connector files are configuration artifacts.",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for text in samples:
                with self.subTest(text=text):
                    (root / "README.md").write_text(text, encoding="utf-8")
                    self.assertEqual(retired_wording(root), [])

    def test_connector_as_writer_and_read_before_write_are_rejected(self):
        writes = (
            "The MCP connector files approved drafts.",
            "The connector creates Linear issues.",
            "The connector drains pending outbox intents.",
            "The MCP connector applies tracker intents.",
            "Read the draft and file it through the Linear MCP connector.",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for write in writes:
                for prefix in ("", "Read the draft first. ", "Query tracker status; "):
                    with self.subTest(write=write, prefix=prefix):
                        (root / "README.md").write_text(prefix + write, encoding="utf-8")
                        self.assertTrue(retired_wording(root))

    def test_second_round_reproductions_have_the_required_verdicts(self):
        examples = (
            ("Read the issue status through the connector, then close the issue through it.", True),
            ("Read the ticket and close it through the Linear MCP connector.", True),
            ("A session with the connector is not required for tracker fallback.", False),
            ("A session with the connector is unavailable.", False),
            ("Do not use the connector available to the current session to create Linear issues; use linear_sync.py instead.", False),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for text, rejected in examples:
                with self.subTest(text=text):
                    (root / "README.md").write_text(text, encoding="utf-8")
                    self.assertEqual(bool(retired_wording(root)), rejected)

    def test_every_catalog_entry_is_detected_across_scope_and_presentation(self):
        entries = catalog()
        self.assertTrue(entries)
        self.assertEqual(len({entry["text"] for entry in entries}), len(entries))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for entry in entries:
                self.assertTrue(entry["source"])
                self.assertEqual(list(text_units(entry["text"])), [entry["text"]])
                wrapped = textwrap.fill(entry["text"], width=40,
                                        break_long_words=False, break_on_hyphens=False)
                for name in ("README.md", "CLAUDE.md", "docs/new.md",
                             "skills/hanig-project/SKILL.md",
                             "skills/hanig-project/declarations.json",
                             "skills/hanig-swarm/SKILL.md",
                             "skills/hanig-swarm/declarations.json"):
                    for text in (entry["text"], wrapped, "- " + wrapped,
                                 "| " + entry["text"] + " |"):
                        with self.subTest(source=entry["source"], name=name, text=text):
                            path = root / name
                            path.parent.mkdir(parents=True, exist_ok=True)
                            payload = json.dumps({"instruction": text}) if path.suffix == ".json" else text
                            path.write_text(payload, encoding="utf-8")
                            try:
                                self.assertEqual({p.split(":", 1)[0] for p in retired_wording(root)}, {name})
                            finally:
                                # Each trial must find its own injected instruction.
                                path.unlink()

    def test_audit_status_covers_each_finding_and_citations_resolve(self):
        body = (ROOT / AUDIT).read_text(encoding="utf-8")
        section = status_section(body)
        original = re.findall(r"^### (\d+)\. ", body, re.MULTILINE)
        rows = re.findall(r"^\| (\d+)\. (.*?) \| (.*?) \| (.*?) \|$",
                          section, re.MULTILINE)
        self.assertEqual(original, [str(i) for i in range(1, 13)])
        self.assertEqual([row[0] for row in rows], original)
        for number, _title, status, evidence in rows:
            with self.subTest(finding=number):
                self.assertRegex(status, r"^(?:Open|Closed)\b")
                self.assertRegex(evidence, r"`(?:tests|skills)/[^`]+\.py::[^`]+`")
        self.assertEqual(citation_problems(ROOT, section), [])

    def test_citation_guard_rejects_missing_file_class_and_method(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "test_sample.py").write_text(
                "class Sample:\n    def test_present(self):\n        pass\n",
                encoding="utf-8")
            for citation in ("missing.py::Sample.test_present",
                             "test_sample.py::Missing.test_present",
                             "test_sample.py::Sample.test_missing"):
                with self.subTest(citation=citation):
                    self.assertEqual(len(citation_problems(root, "`" + citation + "`")), 1)
            self.assertEqual(citation_problems(
                root, "`test_sample.py::Sample.test_present`"), [])

    def test_memory_points_to_live_guidance_without_the_stale_narrative(self):
        body = (ROOT / "MEMORY.md").read_text(encoding="utf-8")
        narrative, facts = body.split("<!-- handoff:facts:begin", 1)
        self.assertIn("[CLAUDE.md](CLAUDE.md)", narrative)
        self.assertIn("[docs/plan-field-reports.md](docs/plan-field-reports.md)", narrative)
        for retired in ("## What this is", "## Status as of 2026-09-02",
                        "**Next, in order**", "## The lesson worth carrying"):
            self.assertNotIn(retired, narrative)
        self.assertIn("<!-- handoff:facts:end -->", facts)


if __name__ == "__main__":
    unittest.main()
