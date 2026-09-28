"""Authored code names no model: routing is data.

Which model reviews, breaks ties or writes code lives in
``skills/hanig-review-gate/reviewers.json``, ``skills/hanig-swarm/agents.json``
and ``models.json``. A model id inside a script is a routing decision the data
cannot change, which is how the committee tie-breaker stayed pinned to one
model after the roster moved on. This sweep reads every string literal in the
authored scripts, installer and hooks (comments and docstrings are prose, not
routing) and fails on any model id those three files declare.
"""

import ast
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCES = (sorted((ROOT / "skills").glob("hanig-*/scripts/*.py"))
           + sorted((ROOT / "lib").glob("*.py"))
           + sorted((ROOT / ".claude" / "hooks").glob("*.py")))


def declared_model_ids():
    ids = set()
    review = json.loads((ROOT / "skills/hanig-review-gate/reviewers.json")
                        .read_text(encoding="utf-8"))
    for reviewer in review["reviewers"]:
        ids.add(reviewer["model"])
        ids.add(reviewer["model"].rsplit("/", 1)[-1])
    agents = json.loads((ROOT / "skills/hanig-swarm/agents.json")
                        .read_text(encoding="utf-8"))
    for route in [agents["default"]["provider"], *agents["thinking_by_model"]]:
        ids.add(route)
        ids.add(route.split("/", 1)[1])
    registry = json.loads((ROOT / "models.json").read_text(encoding="utf-8"))
    for model in registry["models"]:
        ids.add(model["id"])
        ids.add(model["id"].split("/", 1)[1])
    return {value for value in ids if len(value) > 3}


def literal_hits(source, ids):
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docstrings.add(id(first.value))
    hits = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docstrings):
            hits.extend((node.lineno, model) for model in sorted(ids)
                        if model in node.value)
    return hits


class TestNoHardcodedModels(unittest.TestCase):
    def test_the_sweep_covers_the_code_that_routes(self):
        names = {path.name for path in SOURCES}
        for expected in ("swarm.py", "committee.py", "review.py",
                         "skill_installer.py", "tracker_sync_check.py"):
            self.assertIn(expected, names)
        self.assertIn("gpt-6-sol", declared_model_ids())

    def test_no_authored_string_literal_names_a_declared_model(self):
        ids = declared_model_ids()
        found = []
        for path in SOURCES:
            for line, model in literal_hits(path.read_text(encoding="utf-8"), ids):
                found.append("%s:%d names %s" % (path.relative_to(ROOT), line, model))
        self.assertEqual(found, [], "\n".join(found))

    def test_the_sweep_catches_a_literal_and_ignores_prose(self):
        ids = {"gpt-6-sol"}
        self.assertEqual(literal_hits('X = "codex/gpt-6-sol"\n', ids),
                         [(1, "gpt-6-sol")])
        self.assertEqual(literal_hits('f(model="gpt-6-sol")\n', ids),
                         [(1, "gpt-6-sol")])
        self.assertEqual(literal_hits(
            'def f():\n    """gpt-6-sol found this."""\n    # gpt-6-sol\n', ids),
            [])


if __name__ == "__main__":
    unittest.main()
