"""Guard retired tracker instructions and the audit's current-status citations."""
import ast
from pathlib import Path
import re
import tempfile
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

# Match retired instructions across line wrapping, without banning unrelated
# MCP setup, capability absence, protocol field names or planned write denial.
RETIRED = re.compile("|".join((
    r"\bsession\s+(?:that\s+)?(?:has|with)\s+(?:the\s+)?(?:Linear\s+)?MCP\b",
    r"\b(?:through|via)\s+(?:(?:the|an?|authorized|existing|Linear)\s+)*"
    r"(?:MCP(?:\s+connector)?|connector)\b",
    r"\b(?:session\s+with\s+the|connector\s+session['’]s)\s+"
    r"(?:connector|report)\b",
    r"\bconnector\s+(?:drain\w*|filing|applies\s+it)\b",
    r"\b(?:existing\s+connector\s+path|manual\s+connector\s+filing)\b",
    r"\bUse\s+the\s+connector\s+available\s+to\b",
    r"\bLinear\s+MCP\s+write\s+path\s+stays\s+usable\b",
    r"\bsession\s+with\s+the\s+real\s+connector\s+apply\s+it\b",
)), re.IGNORECASE)


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
    problems = []
    for path in authored_documents(root):
        relative = path.relative_to(root).as_posix()
        body = path.read_text(encoding="utf-8")
        if relative == PROTOCOL_PATH:
            # Exempt these exact bytes, not the whole file or a mutable section.
            body = body.replace(PROTOCOL_PARAGRAPH,
                                "\n" * PROTOCOL_PARAGRAPH.count("\n"))
        for match in RETIRED.finditer(body):
            problems.append("{}:{}: {}".format(
                relative, body.count("\n", 0, match.start()) + 1, match[0]))
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
