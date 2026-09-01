"""The launch record is EVIDENCE. The plan and coordinator state are AUTHORITY.

Three review rounds each found the same defect in a different place: some
caller reached for a value wherever it was handiest, and the launch record is
the handiest place and the wrong one. It lives on a filesystem the agent's
Unix user can write, one level above the attempt directory whose path is
handed to that agent as SWARM_UNIT_DIR.

    round 1  base_commit         re-anchoring read the base back out
    round 2  preflight.status    advance decided retry charging from it
    round 3  execution_workspace dispatch passed it to `paseo --cwd`
    round 3  dirty_paths         a stale refusal message was built from it

`trusted_base`'s docstring said "A test asserts nothing else reads
base_commit out of a launch record." No such test existed. That is why the
rounds did not converge: the invariant was prose, so each violation had to be
found by a human reading code, one at a time, and the most consequential one
was never found at all -- `judge_detail` decided whether an attempt PRODUCED
anything using a base and tree it read from the record.

This is that test. It is a chokepoint, not a review: a new read site fails
here until someone adds it deliberately, with a reason.
"""
import ast
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "hanig-swarm" / "scripts"

# Fields whose value decides something. Reading one of these out of the record
# and then acting on it is the defect; writing them there as evidence is fine.
AUTHORITY_KEYS = {"base_commit", "base_tree", "execution_workspace",
                  "dirty_paths"}

# Functions permitted to name an authority key, each with the reason. A
# function absent from here may not touch one at all.
ALLOWED = {
    ("swarm.py", "_write_launch_record"):
        "writes the record from the coordinator's own git observation",
    ("swarm.py", "_execution_workspace"):
        "reads the declared workspace from the PLAN, not from a record",
    ("swarm.py", "validate_plan"):
        "validates the plan's own execution_workspace field before any run",
    ("swarm.py", "_submit"):
        "cross-checks the record's claim AGAINST the plan and refuses on "
        "disagreement, which is the one safe way to read it",
    ("swarm.py", "cmd_verify"):
        "cross-checks the record's base against trusted_base, refuses on "
        "disagreement, and refuses outright when state holds no base",
    ("coordinator_paths.py", "operated_worktrees"):
        "reads execution_workspace off the PLAN's units to build the path "
        "containment set; no record is involved",
    ("worktree.py", "judge_detail"):
        "reads only what read_sealed_launch_record returned, and the seal "
        "test below is what keeps that true",
}

# The UNSEALED reader. This is the sharper invariant: naming a field is not
# the defect, taking it from a record nobody checked is. Every call site is
# listed with what makes it safe.
RAW_READER = "read_launch_record"
RAW_ALLOWED = {
    ("swarm.py", "_submit"):
        "cross-check against the plan; the plan decides and a mismatch "
        "refuses",
    ("swarm.py", "advance"):
        "reads it for the receipt citation and for reporting, and explicitly "
        "does NOT copy the base into state",
    ("swarm.py", "cmd_verify"):
        "cross-check against trusted_base, with no fallback",
    ("worktree.py", "refused_launch"):
        "fail-CLOSED: a record saying 'refused' can only cause a refusal, "
        "never permission",
}


def _key_uses(path):
    """(function, key, line) for every key used as an index or .get() arg.

    Prose is ignored by construction: a docstring is an expression statement,
    never a subscript or a call argument.
    """
    tree = ast.parse(path.read_text())
    out = []
    stack = []

    class V(ast.NodeVisitor):
        def visit_FunctionDef(self, node):
            stack.append(node.name)
            self.generic_visit(node)
            stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def _note(self, value, lineno):
            if isinstance(value, ast.Constant) and value.value in AUTHORITY_KEYS:
                out.append((stack[-1] if stack else "<module>",
                            value.value, lineno))

        def visit_Subscript(self, node):
            self._note(node.slice, node.lineno)
            self.generic_visit(node)

        def visit_Call(self, node):
            if (isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("get", "setdefault", "pop")
                    and node.args):
                self._note(node.args[0], node.lineno)
            self.generic_visit(node)

    V().visit(tree)
    return out


def _raw_reader_calls(path):
    """(function, line) for every call to the unsealed reader."""
    tree = ast.parse(path.read_text())
    out = []
    stack = []

    class V(ast.NodeVisitor):
        def visit_FunctionDef(self, node):
            stack.append(node.name)
            self.generic_visit(node)
            stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node):
            f = node.func
            name = (f.attr if isinstance(f, ast.Attribute)
                    else getattr(f, "id", None))
            if name == RAW_READER:
                out.append((stack[-1] if stack else "<module>", node.lineno))
            self.generic_visit(node)

    V().visit(tree)
    return out


class TestNoOneReadsAuthorityOutOfTheRecord(unittest.TestCase):

    def test_every_use_of_an_authority_key_is_declared(self):
        offenders = []
        for path in sorted(SCRIPTS.glob("*.py")):
            for func, key, line in _key_uses(path):
                if (path.name, func) not in ALLOWED:
                    offenders.append(f"{path.name}:{line} {func}() uses "
                                     f"{key!r}")
        self.assertEqual(
            offenders, [],
            "These sites name a field whose value decides something. Take it "
            "from the plan or from coordinator state, or add the function to "
            "ALLOWED with the reason it is safe:\n  " + "\n  ".join(offenders))

    def test_the_unsealed_reader_is_called_only_where_declared(self):
        offenders = []
        for path in sorted(SCRIPTS.glob("*.py")):
            for func, line in _raw_reader_calls(path):
                if (path.name, func) not in RAW_ALLOWED:
                    offenders.append(f"{path.name}:{line} {func}()")
        self.assertEqual(
            offenders, [],
            "These call the UNSEALED launch-record reader. Judging must go "
            "through read_sealed_launch_record; if this site genuinely only "
            "cross-checks or fails closed, add it to RAW_ALLOWED with the "
            "reason:\n  " + "\n  ".join(offenders))

    def test_judging_does_not_use_the_unsealed_reader(self):
        # The specific regression: judge_detail decided production from a
        # record it read itself. Named directly so a future edit that
        # reintroduces it fails here and not only in the sweep above.
        import ast as _ast
        src = (SCRIPTS / "worktree.py").read_text()
        tree = _ast.parse(src)
        fn = next(n for n in _ast.walk(tree)
                  if isinstance(n, _ast.FunctionDef)
                  and n.name == "judge_detail")
        called = {n.func.id for n in _ast.walk(fn)
                  if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Name)}
        self.assertIn("read_sealed_launch_record", called)
        self.assertNotIn(RAW_READER, called)

    def test_the_allowlist_has_no_dead_entries(self):
        # An allowlist that outlives its call sites stops describing the code
        # and starts excusing it.
        seen = set()
        for path in sorted(SCRIPTS.glob("*.py")):
            for func, _key, _line in _key_uses(path):
                seen.add((path.name, func))
        dead = sorted(set(ALLOWED) - seen)
        self.assertEqual(dead, [], f"ALLOWED entries with no call site: {dead}")

        raw_seen = set()
        for path in sorted(SCRIPTS.glob("*.py")):
            for func, _line in _raw_reader_calls(path):
                raw_seen.add((path.name, func))
        dead_raw = sorted(set(RAW_ALLOWED) - raw_seen)
        self.assertEqual(dead_raw, [],
                         f"RAW_ALLOWED entries with no call site: {dead_raw}")


class TestTheSealStopsARewrittenRecord(unittest.TestCase):
    """The behavioural half. The tests above are structural; this one shows
    the mechanism actually refuses the attack it was built for."""

    def setUp(self):
        import shutil, subprocess, sys, tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        sys.path.insert(0, str(SCRIPTS))
        import swarm as S, unit as U, worktree as W
        self.S, self.U, self.W = S, U, W
        env = dict(__import__("os").environ, GIT_AUTHOR_NAME="t",
                   GIT_AUTHOR_EMAIL="t@x", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@x")
        self.repo = self.tmp / "r"
        self.repo.mkdir()
        for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "base"]):
            if args[0] == "add":
                (self.repo / "a.txt").write_text("one\n")
            subprocess.run(["git", "-C", str(self.repo)] + args, check=True,
                           env=env, capture_output=True)
        self.env = env
        self.att = self.tmp / "runs" / "u1" / "att1"
        self.att.mkdir(parents=True)
        self.spec = {"id": "u1", "kind": "code", "repo": str(self.repo)}

    def _commit(self, name):
        import subprocess
        (self.repo / name).write_text("work\n")
        for args in (["add", "-A"], ["commit", "-qm", name]):
            subprocess.run(["git", "-C", str(self.repo)] + args, check=True,
                           env=self.env, capture_output=True)

    def test_a_real_transition_is_judged_with_the_seal(self):
        err, anchor = self.S._write_launch_record(str(self.att), self.spec)
        self.assertIsNone(err)
        self._commit("b.txt")
        produced, _head, why = self.W.judge_detail(
            self.U.run, str(self.att), self.spec, anchor["seal"])
        self.assertTrue(produced, why)

    def test_a_rewritten_base_is_refused_not_believed(self):
        err, anchor = self.S._write_launch_record(str(self.att), self.spec)
        self.assertIsNone(err)
        self._commit("b.txt")
        # The agent rewrites its own anchor to an unrelated base.
        path = self.W.launch_record_path(str(self.att))
        rec = json.loads(path.read_text())
        rec["base_commit"] = "0" * 40
        path.write_text(json.dumps(rec))
        produced, _head, why = self.W.judge_detail(
            self.U.run, str(self.att), self.spec, anchor["seal"])
        self.assertFalse(produced)
        self.assertIn("no longer matches the digest", why)

    def test_no_seal_means_no_judgment(self):
        err, _anchor = self.S._write_launch_record(str(self.att), self.spec)
        self.assertIsNone(err)
        self._commit("b.txt")
        produced, _head, why = self.W.judge_detail(
            self.U.run, str(self.att), self.spec, None)
        self.assertFalse(produced)
        self.assertIn("no record seal", why)


class TestTheSealActuallyTravels(unittest.TestCase):
    """The loop, end to end.

    Written because a mutation that stopped the coordinator storing the seal
    broke NOTHING: every test above proved the mechanism refuses correctly
    when handed a seal, and none proved a seal is ever handed over. That is
    the same shape as the defects this whole change is about, one layer up.
    """

    def setUp(self):
        import shutil, subprocess, sys, tempfile, os as _os
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        sys.path.insert(0, str(SCRIPTS))
        import swarm as S, worktree as W
        self.S, self.W = S, W
        env = dict(_os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")
        self.repo = self.tmp / "r"
        self.repo.mkdir()
        subprocess.run(["git", "-C", str(self.repo), "init", "-q"], check=True,
                       env=env, capture_output=True)
        (self.repo / "a.txt").write_text("one\n")
        for args in (["add", "-A"], ["commit", "-qm", "base"]):
            subprocess.run(["git", "-C", str(self.repo)] + args, check=True,
                           env=env, capture_output=True)
        self.att = self.tmp / "runs" / "u1" / "att1"
        self.att.mkdir(parents=True)

    def test_dispatch_stores_the_seal_of_the_record_on_disk(self):
        import hashlib
        unit = {"id": "u1", "kind": "code", "repo": str(self.repo),
                "branch": "b", "mode": "full-access", "prompt": "work"}
        state = {"units": {}}
        real, launched = self.S.U.run, []

        def spy(argv, **kwargs):
            if argv and argv[0] == "paseo":
                launched.append(argv)
                return 0, '{"agentId":"11111111-2222-3333-4444-555555555555"}', ""
            return real(argv, **kwargs)

        self.S.U.run = spy
        try:
            job, err = self.S._submit(unit, str(self.att), False, state)
        finally:
            self.S.U.run = real
        self.assertIsNone(err)
        self.assertTrue(job)

        stored = self.S.trusted_record_seal(state, "u1", str(self.att))
        self.assertTrue(stored, "the coordinator stored no seal, so nothing "
                                "downstream can judge this attempt")
        on_disk = hashlib.sha256(
            self.W.launch_record_path(str(self.att)).read_bytes()).hexdigest()
        self.assertEqual(stored, on_disk)

    def test_check_passes_the_seal_to_the_judging_process(self):
        seen = {}
        real = self.S.U.run

        def spy(argv, **kwargs):
            seen["argv"] = argv
            return 0, "DONE", ""

        self.S.U.run = spy
        try:
            self.S._check(str(self.att), "deadbeef" * 8)
        finally:
            self.S.U.run = real
        self.assertIn("--record-seal", seen["argv"])
        self.assertEqual(seen["argv"][seen["argv"].index("--record-seal") + 1],
                         "deadbeef" * 8)


if __name__ == "__main__":
    unittest.main()
