"""Authored code names no model: routing is data.

Which model reviews, breaks ties or writes code lives in
``skills/hanig-review-gate/reviewers.json``, ``skills/hanig-swarm/agents.json``
and ``models.json``. A model id inside a script is a routing decision the data
cannot change, which is how the committee tie-breaker stayed pinned to one
model after the roster moved on. This sweep reads every non-docstring string
literal in every tracked Python file outside tests, docs and vendored code
(comments and docstrings are prose, not routing) and fails on any model id those three files declare.
"""

import ast
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
# The sweep's scope is what the repository TRACKS, because tracked is what
# authored means here: virtualenvs, caches and other tool state are never
# committed, so no skip list for them exists to go stale. Earlier versions
# walked the filesystem and skipped named directories, and three gate rounds
# in a row found a directory the list got wrong.
#
# Excluded from the tracked set: tests, docs and examples (prose and
# fixtures that legitimately name models) and upstream code this repo
# carries verbatim and must not edit (CLAUDE.md): the non-hanig skill
# bundles, which the hanig- prefix classifies, and the vendored fleet tools
# in bin/.
VENDORED = {"bin/bus", "bin/agent-manager", "bin/agent-view"}
SKIPPED_TOP = {"tests", "docs", "examples"}


def is_python(path):
    if path.suffix == ".py":
        return True
    if path.suffix or not path.is_file():
        return False
    with path.open("rb") as handle:
        first = handle.readline(200)
    return first.startswith(b"#!") and b"python" in first


def tracked_files(root):
    """Paths git tracks under root. Raises when git cannot answer."""
    listing = subprocess.run(["git", "-C", str(root), "ls-files", "-z"],
                             capture_output=True, check=True, timeout=60)
    return [root / name for name in
            listing.stdout.decode("utf-8", "surrogateescape").split("\0") if name]


def authored_python(root=ROOT):
    """Every tracked Python file outside tests, docs and vendored code."""
    found = []
    for path in sorted(tracked_files(root)):
        rel = path.relative_to(root)
        if not path.is_file() or rel.parts[0] in SKIPPED_TOP:
            continue
        if rel.parts[0] == "skills" and not rel.parts[1].startswith("hanig-"):
            continue
        if rel.as_posix() in VENDORED:
            continue
        if is_python(path):
            found.append(path)
    return found


SOURCES = authored_python()


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
    # A bare segment such as "opus" is an English word; only keep a bare
    # segment when it is an identifier (it carries a digit).
    return {value for value in ids
            if len(value) > 3 and ("/" in value or any(c.isdigit() for c in value))}


def literal_hits(source, ids):
    """Model ids in string constants the code USES.

    A string that is a whole statement (a docstring, wherever it sits, or a
    bare string used as a comment) does nothing at runtime and routes
    nothing, so it is prose. Every other string constant is a value.
    """
    tree = ast.parse(source)
    prose = {id(node.value) for node in ast.walk(tree)
             if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)}
    hits = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in prose):
            hits.extend((node.lineno, model) for model in sorted(ids)
                        if model in node.value)
    return hits


class TestNoHardcodedModels(unittest.TestCase):
    def test_the_sweep_covers_the_code_that_routes(self):
        names = {path.name for path in SOURCES}
        for expected in ("swarm.py", "committee.py", "review.py",
                         "skill_installer.py", "tracker_sync_check.py",
                         "integration_tests.py", "changed_tests_stable.py"):
            self.assertIn(expected, names)
        self.assertIn("gpt-6-sol", declared_model_ids())
        for vendored in VENDORED:
            self.assertTrue((ROOT / vendored).is_file(), vendored)

    def test_new_tracked_scripts_are_swept_and_untracked_state_is_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            swept = [root / "scripts" / "route.py", root / "bin" / "route",
                     root / "bin" / "tools" / "bus",
                     root / "skills" / "hanig-x" / "scripts" / "a.py",
                     root / ".github" / "route.py", root / ".claude" / "h.py"]
            excluded = [root / "tests" / "t.py", root / "bin" / "bus",
                        root / "skills" / "paseo" / "scripts" / "v.py",
                        root / "bin" / "notes"]
            untracked = [root / ".venv" / "lib" / "x.py",
                         root / "lib" / "site-packages" / "z.py",
                         root / "fresh.py"]
            for path in swept + excluded + untracked:
                path.parent.mkdir(parents=True, exist_ok=True)
                body = "no shebang\n" if path.name == "notes" else (
                    "#!/usr/bin/env python3\nMODEL = 'x'\n")
                path.write_text(body, encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "--"]
                           + [str(p.relative_to(root)) for p in swept + excluded],
                           check=True)
            self.assertEqual(authored_python(root), sorted(swept))

    def test_a_directory_git_cannot_read_fails_loudly(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(subprocess.CalledProcessError):
                authored_python(Path(tmp) / "absent")

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
        self.assertEqual(literal_hits(
            'from __future__ import annotations\n"""gpt-6-sol notes."""\n', ids),
            [])
        self.assertEqual(literal_hits('X = ("a", "gpt-6-sol")\n"""prose"""\n', ids),
                         [(1, "gpt-6-sol")])


if __name__ == "__main__":
    unittest.main()
