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
# Only complete retired instruction sentences deleted from the recorded base
# belong in this catalog. Each entry records its source and deleted line text.
CATALOG_PATH = ROOT / "tests" / "fixtures" / "retired_tracker_instructions.json"


def normalize(text):
    """Normalize these presentations (not arbitrary Markdown):

    Markdown links reduced to their link text; emphasis and code-span markers
    removed; list and blockquote markers removed; table cells joined with
    single spaces; soft wraps joined.
    """
    text = re.sub(r"\[([^\[\]]+)\]\([^\n]*?\)", r"\1", text)
    text = re.sub(r"(?m)^[ \t]*(?:(?:[-+*]|[0-9]+[.)])\s+|>\s*)+", "", text)
    text = re.sub(r"(`+)(.*?)\1", r"\2", text, flags=re.DOTALL)
    text = re.sub(r"\*+|(?<!\w)_+|_+(?!\w)", "", text)
    text = text.replace("|", " ")
    return " ".join(text.split())


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
    retired = [normalize(entry["text"]) for entry in catalog()]
    problems = []
    for path in authored_documents(root):
        relative = path.relative_to(root).as_posix()
        body = path.read_text(encoding="utf-8")
        texts = (string_values(json.loads(body)) if path.suffix == ".json"
                 else [body])
        for text in texts:
            normalized = normalize(text)
            for sentence in retired:
                if sentence in normalized:
                    problems.append(relative + ": " + sentence)
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

    def test_every_catalog_entry_is_detected_across_scope_and_presentation(self):
        entries = catalog()
        self.assertTrue(entries)
        self.assertEqual(len({entry["text"] for entry in entries}), len(entries))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for entry in entries:
                self.assertTrue(entry["source"])
                self.assertTrue(entry["deleted_lines"])
                self.assertTrue(entry["text"].endswith("."))
                wrapped = textwrap.fill(entry["text"], width=40,
                                        break_long_words=False, break_on_hyphens=False)
                for name in ("README.md", "CLAUDE.md", "docs/new.md",
                             "skills/hanig-project/SKILL.md",
                             "skills/hanig-project/declarations.json",
                             "skills/hanig-swarm/SKILL.md",
                             "skills/hanig-swarm/declarations.json",
                             "skills/hanig-orchestrate/references/new.md",
                             "docs/tracker-outbox.md"):
                    for text in (entry["text"], entry["text"].replace("`", ""),
                                 "`" + entry["text"].replace("`", "") + "`",
                                 wrapped, "- " + wrapped,
                                 "[" + entry["text"] + "](/migration)",
                                 "> " + wrapped.replace("\n", "\n> "),
                                 "| Retired | " + entry["text"] + " |",
                                 entry["text"].replace(" ", " | ", 1),
                                 "**" + entry["text"] + "**",
                                 "_" + entry["text"] + "_"):
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
