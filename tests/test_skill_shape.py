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


def _local_markdown_targets(body):
    for match in LOCAL_MARKDOWN_LINK.finditer(body):
        target = match.group(1)
        path = target.split("#", 1)[0]
        if path and "://" not in path:
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
