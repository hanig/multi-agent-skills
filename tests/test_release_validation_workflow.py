"""Policy checks for the release-validation GitHub Actions workflow."""

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release-validation.yml"
EXPECTED_PYTHONS = ["3.9", "3.10", "3.12"]


def yaml_atom(value):
    value = value.strip()
    try:
        return ast.literal_eval(value)
    except (SyntaxError, ValueError):
        return value


def mapping_block(text, key, indent):
    lines = text.splitlines()
    marker = f"{' ' * indent}{key}:"
    matches = [index for index, line in enumerate(lines) if line == marker]
    if len(matches) != 1:
        raise AssertionError(
            f"expected one {key!r} mapping at indent {indent}, found {len(matches)}"
        )
    start = matches[0] + 1
    body = []
    for line in lines[start:]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        body.append(line)
    return "\n".join(body)


def yaml_scalar(text, key, indent):
    marker = f"{' ' * indent}{key}:"
    values = [
        line[len(marker):].strip()
        for line in text.splitlines()
        if line.startswith(marker) and line[:len(marker)] == marker
    ]
    if len(values) != 1 or not values[0]:
        raise AssertionError(
            f"expected one scalar {key!r} at indent {indent}, found {values!r}"
        )
    value = values[0]
    return yaml_atom(value)


def yaml_sequence(text, key, indent):
    lines = text.splitlines()
    marker = f"{' ' * indent}{key}:"
    matches = [
        (index, line[len(marker):].strip())
        for index, line in enumerate(lines)
        if line.startswith(marker) and line[:len(marker)] == marker
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"expected one sequence {key!r} at indent {indent}, found {len(matches)}"
        )
    index, inline = matches[0]
    if inline:
        if not inline.startswith("[") or not inline.endswith("]"):
            raise AssertionError(f"{key!r} is not a sequence")
        contents = inline[1:-1].strip()
        return [] if not contents else [yaml_atom(item) for item in contents.split(",")]

    item_prefix = f"{' ' * (indent + 2)}- "
    values = []
    for line in lines[index + 1:]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        if not line.strip():
            continue
        if not line.startswith(item_prefix):
            raise AssertionError(f"unsupported entry in {key!r} sequence: {line!r}")
        value = line[len(item_prefix):].strip()
        values.append(yaml_atom(value))
    return values


class TestReleaseValidationWorkflow(unittest.TestCase):
    def setUp(self):
        self.workflow = WORKFLOW.read_text(encoding="utf-8")

    def assert_trigger_and_python_matrices(self, workflow):
        event_block = mapping_block(workflow, "on", 0)
        push_block = mapping_block(event_block, "push", 2)
        self.assertEqual(yaml_sequence(push_block, "branches", 4), ["main"])

        jobs_block = mapping_block(workflow, "jobs", 0)
        for job_name in ("regression", "native-discovery"):
            job_block = mapping_block(jobs_block, job_name, 2)
            strategy_block = mapping_block(job_block, "strategy", 4)
            matrix_block = mapping_block(strategy_block, "matrix", 6)
            self.assertEqual(
                yaml_sequence(matrix_block, "python-version", 8),
                EXPECTED_PYTHONS,
            )
            self.assertEqual(
                yaml_scalar(job_block, "python-version", 10),
                "${{ matrix.python-version }}",
            )

    def test_main_push_and_supported_python_versions_are_covered(self):
        self.assert_trigger_and_python_matrices(self.workflow)

    def test_policy_accepts_equivalent_block_style_sequences(self):
        block_style = self.workflow.replace(
            "    branches: [main]",
            "    branches:\n      - main",
        ).replace(
            "        python-version: ['3.9', '3.10', '3.12']",
            "        python-version:\n"
            "          - '3.9'\n"
            "          - '3.10'\n"
            "          - '3.12'",
        )
        self.assert_trigger_and_python_matrices(block_style)

    def test_python_versions_must_be_quoted_strings(self):
        quoted = "python-version: ['3.9', '3.10', '3.12']"
        unquoted = "python-version: ['3.9', 3.10, '3.12']"
        self.assertEqual(self.workflow.count(quoted), 2)

        first = self.workflow.index(quoted)
        second = self.workflow.index(quoted, first + len(quoted))
        for start in (first, second):
            with self.subTest(matrix_offset=start):
                mutated = (
                    self.workflow[:start]
                    + unquoted
                    + self.workflow[start + len(quoted):]
                )
                with self.assertRaises(AssertionError):
                    self.assert_trigger_and_python_matrices(mutated)

    def test_an_unrelated_job_cannot_substitute_for_a_release_job_matrix(self):
        missing_regression_matrix = self.workflow.replace(
            "        python-version: ['3.9', '3.10', '3.12']\n",
            "",
            1,
        )
        missing_regression_matrix += """
  unrelated:
    strategy:
      matrix:
        python-version: ['3.9', '3.10', '3.12']
    steps:
      - uses: actions/setup-python@example
        with:
          python-version: ${{ matrix.python-version }}
"""
        with self.assertRaises(AssertionError):
            self.assert_trigger_and_python_matrices(missing_regression_matrix)

    def test_matrix_artifact_names_are_unique_per_python_version(self):
        jobs_block = mapping_block(self.workflow, "jobs", 0)
        suffix = "${{ matrix.os }}-python-${{ matrix.python-version }}"
        for job_name in ("regression", "native-discovery"):
            job_block = mapping_block(jobs_block, job_name, 2)
            self.assertEqual(
                yaml_scalar(job_block, "name", 10),
                f"{job_name}-{suffix}",
            )


if __name__ == "__main__":
    unittest.main()
