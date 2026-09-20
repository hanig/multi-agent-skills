"""Size, local-reference, and declared-limit shape of authored skills."""

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
LOCAL_MARKDOWN_LINK = re.compile(r"\[[^]]*\]\(([^)]+)\)")
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


def _body(path):
    lines = path.read_text(encoding="utf-8").splitlines()
    boundaries = [index for index, line in enumerate(lines) if line == "---"]
    if len(boundaries) < 2 or boundaries[0] != 0:
        raise AssertionError(f"{path}: missing YAML frontmatter boundaries")
    return "\n".join(lines[boundaries[1] + 1:])


def _authored_skill_docs():
    return sorted(SKILLS.glob("hanig-*/SKILL.md"))


def _local_markdown_targets(body):
    for match in LOCAL_MARKDOWN_LINK.finditer(body):
        target = match.group(1).split("#", 1)[0]
        if target and "://" not in target and not target.startswith("#"):
            yield target


def _local_reference_problems(doc):
    skill = doc.parent.resolve()
    problems = []
    for target in _local_markdown_targets(_body(doc)):
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

    def test_a_missing_or_escaping_body_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            skill = Path(raw) / "hanig-example"
            skill.mkdir()
            doc = skill / "SKILL.md"
            doc.write_text(
                "---\nname: example\n---\n"
                "[missing](references/missing.md) [escape](../outside.md)\n",
                encoding="utf-8",
            )
            self.assertEqual(
                _local_reference_problems(doc),
                ["linked file is absent: references/missing.md",
                 "link escapes skill: ../outside.md"],
            )

    def test_every_declared_swarm_limit_remains_in_the_body_with_a_pointer(self):
        doc = SKILLS / "hanig-swarm" / "SKILL.md"
        limit_lines = [line for line in _body(doc).splitlines()
                       if line.startswith("- **LIMIT: ")]
        labels = [line.split("**", 2)[1] for line in limit_lines]
        self.assertEqual(set(labels), set(SWARM_LIMIT_LABELS))
        self.assertEqual(len(labels), len(SWARM_LIMIT_LABELS))
        for label, line in zip(labels, limit_lines):
            with self.subTest(limit=label):
                self.assertIn("](references/limits.md#", line)


if __name__ == "__main__":
    unittest.main()
