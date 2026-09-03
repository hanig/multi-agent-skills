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
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "hanig-swarm" / "scripts"

# Imported, NOT restated. A second copy of this list is the same duplication
# that produced the defects in the first place.
sys.path.insert(0, str(SCRIPTS))
import worktree as _W  # noqa: E402
AUTHORITY_KEYS = set(_W.AUTHORITY_KEYS)

# Functions permitted to name an authority key, each with the reason. A
# function absent from here may not touch one at all.
ALLOWED = {
    ("swarm.py", "_write_launch_record"):
        "writes the record from the coordinator's own git observation",
    ("swarm.py", "_code_launch_record_payload"):
        "renders deterministic audit bytes from coordinator-state facts",
    ("swarm.py", "_complete_code_launch"):
        "combines the trusted launch intent with Paseo's returned cwd",
    ("swarm.py", "_code_launch_intent_problem"):
        "validates the coordinator-state intent, including that its generated "
        "source branch differs from its plan-declared pull-request target",
    ("swarm.py", "_code_completion_protocol"):
        "renders repository, branch and base from the coordinator-state "
        "launch intent into instructions; it never reads the audit record",
    ("swarm.py", "_code_protocol_problem"):
        "checks an assembled prompt against the coordinator-state launch "
        "intent (or validate_plan's fixed canary intent)",
    ("swarm.py", "_register_code_workspace"):
        "records cleanup metadata from the coordinator-state intent",
    ("swarm.py", "_registered_attempt_workspace"):
        "matches Paseo's registry against the coordinator-state intent",
    ("swarm.py", "_recover_code_launch"):
        "binds recovered Paseo state to coordinator-state workspace identity",
    ("swarm.py", "_archive_code_worktree"):
        "reads coordinator-state workspace metadata only for cleanup",
    ("swarm.py", "_execution_workspace"):
        "reads the declared workspace from the PLAN, not from a record",
    ("swarm.py", "validate_plan"):
        "validates the plan's own execution_workspace field before any run",
    ("coordinator_paths.py", "operated_worktrees"):
        "reads execution_workspace off the PLAN's units to build the path "
        "containment set; no record is involved",
    ("worktree.py", "judge_detail"):
        "reads the launch snapshot transported from coordinator state",
    ("worktree.py", "launch_facts_problem"):
        "validates the coordinator-state snapshot schema and identity",
    ("worktree.py", "workspace_identity_problem"):
        "checks the live directory against coordinator-state identity",
    ("worktree.py", "validate_pinned_head"):
        "checks immutable objects named by coordinator state",
    ("swarm.py", "_submit"):
        "uses the coordinator-created snapshot for dispatch",
    ("swarm.py", "_allocate"):
        "passes repository identity from the trusted plan into allocation",
    ("swarm.py", "admit_merge"):
        "compares the merge attestation's repository to trusted state",
    ("swarm.py", "advance"):
        "uses repository identity from the coordinator-state snapshot",
    ("swarm.py", "trusted_base"):
        "reads the base only from the coordinator-state snapshot",
    ("swarm.py", "cmd_verify"):
        "uses repository and base only from the coordinator-state snapshot",
}

# The UNSEALED reader. This is the sharper invariant: naming a field is not
# the defect, taking it from a record nobody checked is. Every call site is
# listed with what makes it safe.
# Functions permitted to call record_claim, i.e. to look at what an unsealed
# record CLAIMS for an authority field. Each must compare it against the plan
# or coordinator state and refuse on disagreement; none may act on it.
CLAIM_ALLOWED = {}

RAW_READER = "read_launch_record"
RAW_ALLOWED = {
    ("swarm.py", "advance"):
        "reads it only to cite a preflight audit receipt, never for judging",
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


def _named_calls(path, name):
    """(function, line) for every call to `name`."""
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
            got = (f.attr if isinstance(f, ast.Attribute)
                   else getattr(f, "id", None))
            if got == name:
                out.append((stack[-1] if stack else "<module>", node.lineno))
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
            "through coordinator state; if this site is genuinely audit-only "
            "or fail-closed, add it to RAW_ALLOWED with the "
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
        self.assertIn("launch_facts_problem", called)
        self.assertNotIn("read_sealed_launch_record", called)
        self.assertNotIn(RAW_READER, called)

    def test_record_claim_is_called_only_where_declared(self):
        offenders = []
        for path in sorted(SCRIPTS.glob("*.py")):
            for func, line in _named_calls(path, "record_claim"):
                if (path.name, func) not in CLAIM_ALLOWED:
                    offenders.append(f"{path.name}:{line} {func}()")
        self.assertEqual(
            offenders, [],
            "record_claim returns what an unsealed record CLAIMS. A caller "
            "that does not cross-check it against authority and refuse on "
            "disagreement must not use it:\n  " + "\n  ".join(offenders))

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

        claim_seen = set()
        for path in sorted(SCRIPTS.glob("*.py")):
            for func, _line in _named_calls(path, "record_claim"):
                claim_seen.add((path.name, func))
        dead_claim = sorted(set(CLAIM_ALLOWED) - claim_seen)
        self.assertEqual(dead_claim, [],
                         f"CLAIM_ALLOWED entries with no call site: "
                         f"{dead_claim}")


class TestTrustedSnapshotMakesTheRecordAuditOnly(unittest.TestCase):

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

    def test_a_real_transition_is_judged_with_trusted_facts(self):
        err, anchor = self.S._write_launch_record(str(self.att), self.spec)
        self.assertIsNone(err)
        self._commit("b.txt")
        produced, _head, why = self.W.judge_detail(
            self.U.run, str(self.att), self.spec, anchor["facts"])
        self.assertTrue(produced, why)

    def test_rewriting_the_audit_record_changes_no_judging_input(self):
        err, anchor = self.S._write_launch_record(str(self.att), self.spec)
        self.assertIsNone(err)
        self._commit("b.txt")
        # The agent rewrites its own anchor to an unrelated base.
        path = self.W.launch_record_path(str(self.att))
        rec = json.loads(path.read_text())
        rec["base_commit"] = "0" * 40
        path.write_text(json.dumps(rec))
        produced, _head, why = self.W.judge_detail(
            self.U.run, str(self.att), self.spec, anchor["facts"])
        self.assertTrue(produced, why)

    def test_no_trusted_snapshot_means_no_judgment(self):
        err, _anchor = self.S._write_launch_record(str(self.att), self.spec)
        self.assertIsNone(err)
        self._commit("b.txt")
        produced, _head, why = self.W.judge_detail(
            self.U.run, str(self.att), self.spec, None)
        self.assertFalse(produced)
        self.assertIn("no launch snapshot", why)


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
        import hashlib, os, subprocess
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")
        unit = {"id": "u1", "kind": "code", "repo": str(self.repo),
                "target_branch": "main", "mode": "full-access",
                "prompt": "work"}
        state = {"units": {}}
        real, launched = self.S.U.run, []

        def spy(argv, **kwargs):
            if argv and argv[0] == "paseo":
                launched.append(argv)
                workspace = self.tmp / "managed" / "att1"
                subprocess.run(
                    ["git", "-C", str(self.repo), "worktree", "add", "-q",
                     "-b", argv[argv.index("--new-branch") + 1],
                     str(workspace), argv[argv.index("--base") + 1]],
                    check=True, env=env, capture_output=True, text=True)
                return 0, json.dumps({
                    "agentId": "11111111-2222-3333-4444-555555555555",
                    "cwd": str(workspace)}), ""
            return real(argv, **kwargs)

        self.S.U.run = spy
        try:
            job, err = self.S._submit(
                unit, str(self.att), False, state, str(self.tmp / "state"))
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

    def test_check_passes_launch_facts_to_the_judging_process(self):
        seen = {}
        real = self.S.U.run

        def spy(argv, **kwargs):
            seen["argv"] = argv
            return 0, "DONE", ""

        self.S.U.run = spy
        try:
            self.S._check(str(self.att), {"attempt_id": "att1"})
        finally:
            self.S.U.run = real
        self.assertIn("--launch-facts", seen["argv"])
        payload = seen["argv"][seen["argv"].index("--launch-facts") + 1]
        self.assertEqual(json.loads(payload), {"attempt_id": "att1"})


class TestTheRecordItselfRefuses(unittest.TestCase):
    """The reviewer's hole in the static tests, closed at runtime.

    _key_uses and _raw_reader_calls match string literals and direct reader
    names, so a computed key or an aliased reader walks past both. Detection
    at one chokepoint is weaker than making the thing unrepresentable, so the
    object refuses. The static tests remain as the fast guard; this is the
    control that does not depend on how the access was spelled.
    """

    def setUp(self):
        sys.path.insert(0, str(SCRIPTS))
        import worktree as W
        self.W = W
        self.rec = W.EvidenceRecord({
            "base_commit": "a" * 40, "base_tree": "b" * 40,
            "execution_workspace": "/checkout", "dirty_paths": [],
            "repo": "/checkout", "preflight": {"status": "passed"}})

    def test_a_literal_authority_key_is_refused(self):
        for key in sorted(self.W.AUTHORITY_KEYS):
            with self.assertRaises(self.W.AuthorityFromEvidence):
                self.rec.get(key)
            with self.assertRaises(self.W.AuthorityFromEvidence):
                self.rec[key]

    def test_a_COMPUTED_authority_key_is_refused_too(self):
        # The exact bypass the static scan cannot see.
        key = "base_" + "commit"
        with self.assertRaises(self.W.AuthorityFromEvidence):
            self.rec.get(key)
        for key in [k for k in ("base_commit",)]:
            with self.assertRaises(self.W.AuthorityFromEvidence):
                self.rec[key]

    def test_an_ALIASED_reader_still_returns_a_refusing_record(self):
        # The other bypass: rename the reader, defeat the name match.
        import tempfile, shutil, json as _json
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        att = tmp / "runs" / "u" / "a1"
        att.mkdir(parents=True)
        self.W.launch_record_path(str(att)).write_text(
            _json.dumps({"base_commit": "c" * 40, "repo": "/x"}))
        aliased = self.W.read_launch_record          # no longer the name
        rec, err = aliased(str(att))
        self.assertIsNone(err)
        with self.assertRaises(self.W.AuthorityFromEvidence):
            rec.get("base_commit")

    def test_the_other_spellings_are_refused_too(self):
        for call in (lambda: self.rec.pop("base_commit"),
                     lambda: self.rec.setdefault("base_tree", "x")):
            with self.assertRaises(self.W.AuthorityFromEvidence):
                call()
        # Iterating values hands the fields out without naming them.
        self.assertNotIn("base_commit", dict(self.rec.items()))
        self.assertNotIn("a" * 40, self.rec.values())
        # Keys stay visible: a caller may still see what the record holds.
        self.assertIn("base_commit", set(self.rec.keys()))

    def test_the_declared_escape_hatch_is_the_only_way_through(self):
        # Stated plainly rather than claimed away: dict.get reaches the field,
        # and must, because record_claim is built on it. The control is that
        # the route is named, allowlisted and tested -- not that it is sealed.
        self.assertEqual(dict.get(self.rec, "base_commit"), "a" * 40)

    def test_non_authority_fields_are_untouched(self):
        # A guard that refuses everything would just move the damage.
        self.assertEqual(self.rec["preflight"]["status"], "passed")
        self.assertIsNone(self.rec.get("absent"))
        self.assertEqual(self.rec.get("absent", "d"), "d")

    def test_record_claim_is_the_declared_way_through(self):
        self.assertEqual(
            self.W.record_claim(self.rec, "execution_workspace"), "/checkout")
        self.assertIsNone(self.W.record_claim(None, "base_commit"))

    def test_the_sealed_reader_returns_a_plain_usable_record(self):
        # Sealed means checked, so judging must be able to read the fields it
        # judges on. A refusing type there would break the legitimate path.
        import tempfile, shutil, hashlib as _h, json as _json
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        att = tmp / "runs" / "u" / "a1"
        att.mkdir(parents=True)
        payload = _json.dumps({"base_commit": "d" * 40})
        self.W.launch_record_path(str(att)).write_text(payload)
        seal = _h.sha256(payload.encode()).hexdigest()
        rec, err = self.W.read_sealed_launch_record(str(att), seal)
        self.assertIsNone(err)
        self.assertEqual(rec.get("base_commit"), "d" * 40)


class TestReceiptProvenanceIsActuallyRecorded(unittest.TestCase):
    """That the coordinator records causing a receipt, not merely that the
    report checks it.

    Written for the second time this change was made: a mutation removing the
    coordinator's recording broke nothing, because the report's own fixtures
    write the digest themselves. Verifying a mechanism with fixtures that
    supply its input proves the consumer, never the producer.
    """

    def test_advance_records_the_digest_of_the_receipt_its_check_wrote(self):
        import importlib.util, json as _json, hashlib as _h
        import shutil, tempfile
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        spec = importlib.util.spec_from_file_location(
            "sw_prov", SCRIPTS / "swarm.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)

        st = tmp / "st"
        st.mkdir(parents=True)
        att = tmp / "rn" / "h" / "attempt-1"
        att.mkdir(parents=True)
        (st / "swarm-state.json").write_text(_json.dumps(
            {"schema_version": 1, "halted": None, "units": {
                "h": {"state": "SUBMITTED", "job_id": "2868624",
                      "attempt_dir": str(att), "attempts": [str(att)],
                      "gpu_hours": 0}}}))
        plan = {"name": "p", "units": [
            {"id": "h", "kind": "slurm", "runtime": "none", "command": "true",
             "outputs": ["o"], "write_scopes": ["h/"]}]}

        payload = _json.dumps({"task_id": "h", "attempt_id": "attempt-1",
                               "state": "DONE"})

        def check(unit_dir, seal=None):
            # What the real check does: write the receipt, then report the
            # digest of the receipt it wrote.
            (Path(unit_dir) / "receipt.json").write_text(payload)
            result = {"produced_head": None,
                      "receipt_sha256":
                          _h.sha256(payload.encode()).hexdigest()}
            return 0, "DONE", "", ("SWARM_CHECK_RESULT " +
                                    _json.dumps(result, sort_keys=True,
                                                separators=(",", ":")))

        m._check = check
        ok, why = m.acquire_lease(str(st))
        self.assertTrue(ok, why)
        self.addCleanup(m.release_lease, str(st))
        m.advance(plan, m.load_state(str(st)), str(st), str(tmp / "rn"),
                  False, max_new=0)

        state = m.load_state(str(st))
        seals = ((state.get("units") or {}).get("h") or {}).get(
            "attempt_receipt_seals") or {}
        self.assertEqual(
            seals.get("attempt-1"), _h.sha256(payload.encode()).hexdigest(),
            "the coordinator ran the check that wrote this receipt and did "
            "not record causing it, so the report cannot tell it apart from "
            "one the agent wrote")


class TestOnlyAReceiptTheCheckReportedWritingIsAttested(unittest.TestCase):
    """The coordinator cannot tell whose receipt it is by looking.

    It used to digest whatever `receipt.json` was on disk once the check
    returned. An agent that pre-writes a DONE receipt, and a check that then
    fails to replace it, produced an attested agent claim. Only the writer
    knows what it wrote, so the check reports the digest and the coordinator
    records nothing else.
    """

    def _run(self, check_impl, payload_on_disk):
        import importlib.util, json as _json
        import shutil, tempfile
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        spec = importlib.util.spec_from_file_location(
            "sw_stale", SCRIPTS / "swarm.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        st, att = tmp / "st", tmp / "rn" / "h" / "attempt-1"
        st.mkdir(parents=True)
        att.mkdir(parents=True)
        (att / "receipt.json").write_text(payload_on_disk)
        (st / "swarm-state.json").write_text(_json.dumps(
            {"schema_version": 1, "halted": None, "units": {
                "h": {"state": "SUBMITTED", "job_id": "2868624",
                      "attempt_dir": str(att), "attempts": [str(att)],
                      "gpu_hours": 0}}}))
        plan = {"name": "p", "units": [
            {"id": "h", "kind": "slurm", "runtime": "none", "command": "true",
             "outputs": ["o"], "write_scopes": ["h/"]}]}
        m._check = check_impl
        ok, why = m.acquire_lease(str(st))
        self.assertTrue(ok, why)
        self.addCleanup(m.release_lease, str(st))
        m.advance(plan, m.load_state(str(st)), str(st), str(tmp / "rn"),
                  False, max_new=0)
        state = m.load_state(str(st))
        return ((state.get("units") or {}).get("h") or {}).get(
            "attempt_receipt_seals") or {}

    def test_a_check_that_wrote_nothing_attests_nothing(self):
        # The agent's pre-written claim is still on disk. The check reports no
        # digest because it wrote no receipt.
        forged = json.dumps({"task_id": "h", "state": "DONE"})
        seals = self._run(
            lambda d, facts=None: (1, "check crashed", ""), forged)
        self.assertEqual(seals, {}, "attested a receipt the check did not "
                                    "write")

    def test_a_malformed_digest_line_attests_nothing(self):
        forged = json.dumps({"task_id": "h", "state": "DONE"})
        seals = self._run(
            lambda d, facts=None: (
                0, "DONE", "", "SWARM_CHECK_RESULT not-json"),
            forged)
        self.assertEqual(seals, {})

    def test_a_digest_line_the_agent_cannot_reach_is_what_counts(self):
        # The reported digest is of the file the check wrote, so a later edit
        # by the agent no longer matches and the report will say so.
        import hashlib as _h
        good = json.dumps({"task_id": "h", "attempt_id": "attempt-1",
                           "state": "FAILED"})

        def check(unit_dir, seal=None):
            (Path(unit_dir) / "receipt.json").write_text(good)
            result = {"produced_head": None,
                      "receipt_sha256":
                          _h.sha256(good.encode()).hexdigest()}
            return 0, "FAILED", "", ("SWARM_CHECK_RESULT " +
                                      json.dumps(result, sort_keys=True,
                                                 separators=(",", ":")))

        seals = self._run(check, json.dumps({"state": "DONE"}))
        self.assertEqual(seals.get("attempt-1"),
                         _h.sha256(good.encode()).hexdigest())


# The receipt is the SECOND record with this problem, and it took four rounds
# plus my own sweep to see it. report.py was taught to check attestation and
# the six readers in swarm.py were not; each opened receipt.json and decided
# its own trust. One of them supplied `produced_head`, the commit a merge
# attestation is checked against, from a file the agent can write -- and then
# stored it as coordinator state. Another handed the same value to the
# verifier's checkout.
RECEIPT_READER = "attested_receipt"
RECEIPT_ALLOWED = {
    ("swarm.py", "attested_receipt"):
        "IS the accessor: reads the bytes and checks them against the digest "
        "the coordinator recorded for a check it caused",
    ("unit.py", "cmd_check"):
        "WRITES the receipt and reports the digest of what it wrote",
    ("unit.py", "<module>"):
        "defines the RECEIPT filename constant",
}


class TestNothingReadsAReceiptWithoutAttestation(unittest.TestCase):

    def test_no_module_opens_a_receipt_outside_the_accessor(self):
        offenders = []
        for path in sorted(SCRIPTS.glob("*.py")):
            tree = ast.parse(path.read_text())
            stack = []

            class V(ast.NodeVisitor):
                def visit_FunctionDef(self, node):
                    stack.append(node.name)
                    self.generic_visit(node)
                    stack.pop()

                visit_AsyncFunctionDef = visit_FunctionDef

                def visit_Constant(self, node):
                    if node.value != "receipt.json":
                        return
                    func = stack[-1] if stack else "<module>"
                    if (path.name, func) not in RECEIPT_ALLOWED:
                        offenders.append(f"{path.name}:{node.lineno} "
                                         f"{func}() names receipt.json")

            V().visit(tree)
        self.assertEqual(
            offenders, [],
            "A receipt read for a decision must go through "
            + RECEIPT_READER + ", which checks it against the digest the "
            "coordinator recorded for a check it caused:\n  "
            + "\n  ".join(offenders))

    def test_the_receipt_constant_is_not_a_way_around_the_literal(self):
        # U.RECEIPT is the same string wearing a name.
        offenders = []
        for path in sorted(SCRIPTS.glob("*.py")):
            tree = ast.parse(path.read_text())
            stack = []

            class V(ast.NodeVisitor):
                def visit_FunctionDef(self, node):
                    stack.append(node.name)
                    self.generic_visit(node)
                    stack.pop()

                visit_AsyncFunctionDef = visit_FunctionDef

                def visit_Attribute(self, node):
                    if node.attr != "RECEIPT":
                        return self.generic_visit(node)
                    func = stack[-1] if stack else "<module>"
                    if (path.name, func) not in RECEIPT_ALLOWED:
                        offenders.append(f"{path.name}:{node.lineno} "
                                         f"{func}() uses U.RECEIPT")
                    self.generic_visit(node)

                def visit_Name(self, node):
                    # The BARE name, inside unit.py, which the attribute form
                    # never sees. My own scan had exactly the literal-only
                    # blind spot a reviewer named on the previous guard.
                    if node.id != "RECEIPT":
                        return
                    func = stack[-1] if stack else "<module>"
                    if (path.name, func) not in RECEIPT_ALLOWED:
                        offenders.append(f"{path.name}:{node.lineno} "
                                         f"{func}() uses RECEIPT")

            V().visit(tree)
        self.assertEqual(offenders, [], "\n  ".join(offenders))

    def test_the_allowlist_describes_the_code(self):
        seen = set()
        for path in sorted(SCRIPTS.glob("*.py")):
            tree = ast.parse(path.read_text())
            stack = []

            class V(ast.NodeVisitor):
                def visit_FunctionDef(self, node):
                    stack.append(node.name)
                    self.generic_visit(node)
                    stack.pop()

                visit_AsyncFunctionDef = visit_FunctionDef

                def _seen(self):
                    # "<module>" when outside a function, matching how the
                    # offender scan names it. Requiring `stack` here meant the
                    # module-level constant never counted, so its allowlist
                    # entry looked dead.
                    seen.add((path.name, stack[-1] if stack else "<module>"))

                def visit_Constant(self, node):
                    if node.value == "receipt.json":
                        self._seen()

                def visit_Attribute(self, node):
                    if node.attr == "RECEIPT":
                        self._seen()
                    self.generic_visit(node)

                def visit_Name(self, node):
                    if node.id == "RECEIPT":
                        self._seen()

            V().visit(tree)
        dead = sorted(set(RECEIPT_ALLOWED) - seen)
        self.assertEqual(dead, [], f"entries with no call site: {dead}")


class TestTheDigestDescribesWhatWasWritten(unittest.TestCase):
    """Three fixes that a mutation pass showed were unguarded.

    Each was fixed and none was tested, which is the third time in this change
    that a mechanism's producer went unverified while its consumer was
    covered. Mutation is the only thing that has reliably caught it.
    """

    def setUp(self):
        import shutil, tempfile
        sys.path.insert(0, str(SCRIPTS))
        import unit as U, worktree as W
        self.U, self.W = U, W
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_write_json_digests_the_bytes_it_wrote_not_a_re_read(self):
        # The race: anything can replace the file between a write and a read
        # of the same path. A digest of bytes we are holding has no gap.
        import hashlib
        path = self.tmp / "receipt.json"
        got = []
        self.assertIsNone(self.U.write_json(path, {"state": "DONE"},
                                            digest_out=got))
        self.assertEqual(len(got), 1)
        original = path.read_bytes()
        self.assertEqual(got[0], hashlib.sha256(original).hexdigest())

        # The agent replaces it. The reported digest still describes what the
        # writer wrote, so the swap is detectable rather than blessed.
        path.write_text('{"state": "DONE", "forged": true}')
        self.assertNotEqual(
            got[0], hashlib.sha256(path.read_bytes()).hexdigest())

    def test_write_json_never_reads_the_path_back_to_digest_it(self):
        # The behavioural test above cannot catch this: a re-read of a file
        # nobody touched agrees with the bytes written, so the race is
        # invisible unless it is actually raced. What IS checkable is that no
        # read of the path exists to race with.
        tree = ast.parse((SCRIPTS / "unit.py").read_text())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "write_json")
        src = ast.unparse(fn)
        self.assertNotIn("read_bytes", src,
                         "write_json reads the path back; anything can "
                         "replace the file in that gap")
        self.assertNotIn("read_text", src)
        digest_calls = [n for n in ast.walk(fn)
                        if isinstance(n, ast.Call)
                        and getattr(n.func, "id", None) == "_digest_bytes"]
        self.assertEqual(len(digest_calls), 1)
        self.assertIn("payload", ast.unparse(digest_calls[0]),
                      "the digest must be taken from the bytes this call "
                      "wrote, which it is holding")

    def test_produced_head_can_judge_only_when_given_trusted_facts(self):
        # Dropping the seal here made this return None for every attempt,
        # leaving the agent-writable receipt as the only working source for
        # the commit a merge attestation is pinned to.
        import subprocess, os as _os
        env = dict(_os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")
        repo = self.tmp / "r"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True,
                       env=env, capture_output=True)
        (repo / "a.txt").write_text("one\n")
        for args in (["add", "-A"], ["commit", "-qm", "base"]):
            subprocess.run(["git", "-C", str(repo)] + args, check=True,
                           env=env, capture_output=True)
        att = self.tmp / "runs" / "u" / "a1"
        att.mkdir(parents=True)
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "sw_ph", SCRIPTS / "swarm.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        unit = {"id": "u", "kind": "code", "repo": str(repo)}
        err, anchored = m._write_launch_record(str(att), unit)
        self.assertIsNone(err)
        (repo / "b.txt").write_text("work\n")
        for args in (["add", "-A"], ["commit", "-qm", "work"]):
            subprocess.run(["git", "-C", str(repo)] + args, check=True,
                           env=env, capture_output=True)
        head = self.W.produced_head(self.U.run, str(att), unit,
                                    anchored["facts"])
        self.assertTrue(head, "produced_head cannot judge, so the only "
                              "remaining source is the agent's receipt")
        self.assertIsNone(
            self.W.produced_head(self.U.run, str(att), unit, None),
            "judging without trusted facts must refuse, not guess")


class TestAWithdrawnSealDoesNotLingerAsAuthority(unittest.TestCase):

    def test_a_later_check_that_wrote_nothing_withdraws_the_earlier_seal(self):
        # Otherwise a favourable receipt from an earlier check keeps
        # validating: restore that file and it still matches the stale seal,
        # outranking the coordinator's newer verdict.
        import importlib.util, json as _json, hashlib as _h
        import shutil, tempfile
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        sys.path.insert(0, str(SCRIPTS))
        spec = importlib.util.spec_from_file_location(
            "sw_wd", SCRIPTS / "swarm.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        att = tmp / "runs" / "h" / "attempt-1"
        att.mkdir(parents=True)
        good = _json.dumps({"task_id": "h", "attempt_id": "attempt-1",
                            "state": "DONE"})
        (att / "receipt.json").write_text(good)
        state = {"units": {"h": {"attempt_receipt_seals": {
            "attempt-1": _h.sha256(good.encode()).hexdigest()}}}}
        # It is admissible now.
        self.assertIsNotNone(m.attested_receipt(state, "h", str(att))[0])

        # A later check writes nothing and so reports no digest.
        m._record_receipt_provenance(state, "h", str(att), None)
        seals = state["units"]["h"].get("attempt_receipt_seals") or {}
        self.assertNotIn("attempt-1", seals,
                         "the coordinator still vouches for a receipt it no "
                         "longer caused")
        rec, why = m.attested_receipt(state, "h", str(att))
        self.assertIsNone(rec)
        self.assertIn("claim by", why)


if __name__ == "__main__":
    unittest.main()
