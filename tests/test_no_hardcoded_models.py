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


PYTHON_SUFFIXES = {".py", ".pyw"}


def is_python(path):
    """A Python source by suffix, or an extensionless file with a python shebang."""
    if path.suffix in PYTHON_SUFFIXES:
        return True
    if path.suffix or not path.is_file():
        return False
    with path.open("rb") as handle:
        first = handle.readline()
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
    routes = [reviewer["model"] for reviewer in review["reviewers"]]
    agents = json.loads((ROOT / "skills/hanig-swarm/agents.json")
                        .read_text(encoding="utf-8"))
    routes += [agents["default"]["provider"], *agents["thinking_by_model"]]
    registry = json.loads((ROOT / "models.json").read_text(encoding="utf-8"))
    routes += [model["id"] for model in registry["models"]]
    for route in routes:
        # The whole route and every suffix after a slash: openrouter's
        # "moonshotai/kimi-k2.7-code" and its bare "kimi-k2.7-code".
        segments = route.split("/")
        ids.update("/".join(segments[i:]) for i in range(len(segments)))
    # A bare segment such as "opus" is an English word; only keep a bare
    # segment when it is an identifier (it carries a digit).
    return {value for value in ids
            if len(value) > 3 and ("/" in value or any(c.isdigit() for c in value))}


# Characters that belong to a model id. A match must not run into one on
# either side, except that a "/" may precede it (a provider prefix), so
# "codex/gpt-6-sol" hits gpt-6-sol while "docs/claude/opus-usage.md" and
# "claude/opus.md" do not hit claude/opus.
ID_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")


def names_model(text, model):
    start = text.find(model)
    while start != -1:
        end = start + len(model)
        before = text[start - 1] if start else ""
        after = text[end] if end < len(text) else ""
        if before not in ID_CHARS and after not in ID_CHARS | {"/"}:
            return True
        start = text.find(model, start + 1)
    return False


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
                        if names_model(node.value, model))
    return hits


class TestNoHardcodedModels(unittest.TestCase):
    def test_the_sweep_covers_the_code_that_routes(self):
        names = {path.name for path in SOURCES}
        for expected in ("swarm.py", "committee.py", "review.py",
                         "skill_installer.py", "tracker_sync_check.py",
                         "integration_tests.py", "changed_tests_stable.py"):
            self.assertIn(expected, names)
        declared = declared_model_ids()
        self.assertIn("gpt-6-sol", declared)
        self.assertIn("claude/opus", declared)
        # Deliberate: a bare suffix with no digit is an English word, so
        # "opus" (from claude/opus) is not an id on its own.
        self.assertNotIn("opus", declared)
        for vendored in VENDORED:
            self.assertTrue((ROOT / vendored).is_file(), vendored)

    def test_new_tracked_scripts_are_swept_and_untracked_state_is_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            swept = [root / "scripts" / "route.py", root / "bin" / "route",
                     root / "bin" / "tools" / "bus",
                     root / "skills" / "hanig-x" / "scripts" / "a.py",
                     root / ".github" / "route.py", root / ".claude" / "h.py",
                     root / "scripts" / "launch.pyw", root / "bin" / "long"]
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
                if path.name == "long":  # "python" past the first 200 bytes
                    body = "#!/usr/bin/env " + "-S " * 80 + "python3\n"
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
        for value in ("gpt-6-sol", "codex/gpt-6-sol", "--model gpt-6-sol",
                      "openrouter/x/gpt-6-sol", "'gpt-6-sol'"):
            with self.subTest(value=value):
                self.assertTrue(names_model(value, "gpt-6-sol"))
        for value in ("docs/claude/opus-usage.md", "claude/opus.md",
                      "claude/opus/notes", "xclaude/opus", "claude/opuses"):
            with self.subTest(value=value):
                self.assertFalse(names_model(value, "claude/opus"))
        self.assertEqual(literal_hits('P = "docs/claude/opus-usage.md"\n',
                                      {"claude/opus"}), [])


if __name__ == "__main__":
    unittest.main()
