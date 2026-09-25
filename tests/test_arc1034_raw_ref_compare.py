"""ARC-1034: rendered diagnostics must never select a ref or an audit file."""
import copy
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.test_attempt_authority import RepoCase, U, W, git


class TestRawRefComparison(unittest.TestCase):
    def facts(self, schema, branch="topic\u200bname"):
        prefix = "refs/remotes/origin/" if schema == 3 else "refs/heads/"
        return {
            "schema_version": schema, "unit_id": "u1", "attempt_id": "att1",
            "repo": "/repo", "execution_workspace": "/workspace",
            "workspace_identity": {"realpath": "/workspace"},
            "base_commit": "a" * 40, "base_tree": "b" * 40,
            "clean_at_launch": True, "branch": branch,
            "judgment_ref": prefix + branch,
            "repository_remote": "/origin", "repository_remote_raw": "/origin",
        }

    def test_exact_raw_match_admits_and_different_ref_refuses(self):
        for schema in (3, 4, 5, 6):
            for branch in ("topic\u200bname", "topic\u0085", "topic\u00a0"):
                with self.subTest(schema=schema, branch=ascii(branch)):
                    facts = self.facts(schema, branch)
                    self.assertNotEqual(
                        branch, W.render_for_record(branch, len(branch),
                                                    collapse=False))
                    before = copy.deepcopy(facts)
                    self.assertIsNone(W.launch_facts_problem(facts))
                    self.assertEqual(facts, before)
                    facts["judgment_ref"] += "-different"
                    problem = W.launch_facts_problem(facts)
                    self.assertIsNotNone(problem)
                    self.assertTrue(problem.isprintable())

    def test_rendered_spelling_cannot_impersonate_the_raw_ref(self):
        for schema in (3, 4, 5, 6):
            with self.subTest(schema=schema):
                facts = self.facts(schema)
                ref = facts["judgment_ref"]
                facts["judgment_ref"] = W.render_for_record(
                    ref, len(ref), collapse=False)
                self.assertIsNotNone(W.launch_facts_problem(facts))

    def test_different_raw_refs_with_identical_diagnostics_refuse(self):
        facts = self.facts(4)
        ref = facts["judgment_ref"]
        other = ref.replace("\u200b", "\u200c")
        self.assertEqual(W.render_for_record(ref, len(ref), collapse=False),
                         W.render_for_record(other, len(other), collapse=False))
        facts["judgment_ref"] = other
        self.assertIsNotNone(W.launch_facts_problem(facts))

    def test_schema_three_derivation_keeps_raw_branch(self):
        facts = self.facts(3)
        self.assertEqual(W.effective_remote_ref(facts),
                         "refs/heads/" + facts["branch"])

    def test_launch_record_readers_select_exact_persisted_filename(self):
        with tempfile.TemporaryDirectory() as root:
            for suffix in ("\u200b", "\u200c"):
                attempt = Path(root) / ("att" + suffix)
                raw = json.dumps({"attempt": attempt.name}).encode()
                path = attempt.parent / ("launch-" + attempt.name + ".json")
                path.write_bytes(raw)
                self.assertEqual(W.launch_record_path(attempt), path)
                record, problem = W.read_launch_record(attempt)
                self.assertIsNone(problem)
                self.assertEqual(record["attempt"], attempt.name)
                record, problem = W.read_sealed_launch_record(
                    attempt, hashlib.sha256(raw).hexdigest())
                self.assertIsNone(problem)
                self.assertEqual(record["attempt"], attempt.name)


class TestRawRefGitJudgment(RepoCase):
    def remote_facts(self, branch, schema=6, attempt_id="att1"):
        attempt = self.tmp / "runs" / attempt_id
        facts = dict(self.facts(attempt), schema_version=schema,
                     branch=branch, repository_remote=str(self.remote),
                     repository_remote_raw=str(self.remote))
        facts["judgment_ref"] = (
            "refs/remotes/origin/" if schema == 3 else "refs/heads/") + branch
        return attempt, facts

    def test_real_git_fetch_and_receipt_keep_persisted_raw_refs(self):
        for schema in (3, 4, 5, 6):
            for suffix in ("\u200b", "\u0085", "\u00a0"):
                with self.subTest(schema=schema, suffix=ascii(suffix)):
                    branch = "topic-" + str(schema) + suffix + "-name"
                    git(self.repo, "checkout", "-qb", branch, self.base)
                    head = self.commit("produced.txt")
                    git(self.repo, "push", "-q", "origin",
                        head + ":refs/heads/" + branch)
                    attempt, facts = self.remote_facts(
                        branch, schema, "att-" + str(schema) + suffix)
                    snapshot = self.tmp / "launch-facts.json"
                    snapshot.write_text(json.dumps(facts))
                    persisted_bytes = snapshot.read_bytes()
                    restored = json.loads(persisted_bytes)
                    selected_ref = "refs/heads/" + branch
                    spec = {"kind": "code", "repo": str(self.repo),
                            "task_id": "u1",
                            "judgment_ref": "old-rendered-observation"}
                    produced, detail = W.judge_and_capture(
                        U.run, attempt, spec, restored)
                    self.assertTrue(produced, detail)
                    self.assertEqual(spec["produced_head"], head)
                    self.assertEqual(spec["judgment_ref"], selected_ref)
                    self.assertEqual(spec["launch_judgment_ref"],
                                     facts["judgment_ref"])
                    self.assertEqual(git(
                        self.repo, "rev-parse",
                        "refs/hanig-swarm/judgments/" + attempt.name), head)
                    self.assertEqual(snapshot.read_bytes(), persisted_bytes)

    def test_untrimmed_runner_preserves_trailing_unicode_in_remote_answer(self):
        # unit.run currently strips these characters before this module sees
        # them. An untrimmed runner isolates worktree.py's own boundary.
        for suffix in ("\u0085", "\u00a0"):
            with self.subTest(suffix=ascii(suffix)):
                branch = "trailing" + suffix
                git(self.repo, "checkout", "-qb", branch, self.base)
                head = self.commit("produced.txt")
                git(self.repo, "push", "-q", "origin",
                    head + ":refs/heads/" + branch)
                attempt, facts = self.remote_facts(branch)

                def runner(argv, timeout=60):
                    result = subprocess.run(
                        argv, capture_output=True, text=True, timeout=timeout,
                        env=U.child_env())
                    return result.returncode, result.stdout, result.stderr

                produced, judged_head, detail = W.judge_detail(
                    runner, attempt, {"repo": str(self.repo)}, facts)
                self.assertTrue(produced, detail)
                self.assertEqual(judged_head, head)

    def test_refspec_validation_refuses_invalid_source_and_destination(self):
        for field in ("source", "destination"):
            for invalid in ("bad:name", "bad*name", "bad?name", "bad[name",
                            "bad..name", "bad\nname", "bad\0name"):
                with self.subTest(field=field, invalid=ascii(invalid)):
                    attempt, facts = self.remote_facts(
                        invalid if field == "source" else "topic",
                        attempt_id=invalid if field == "destination" else "att1")
                    calls = []

                    def runner(argv, **kwargs):
                        calls.append(argv)
                        if "ls-remote" in argv or "fetch" in argv:
                            self.fail("invalid ref reached a remote command")
                        return U.run(argv, **kwargs)

                    judgment = {}
                    produced, head, detail = W.judge_detail(
                        runner, attempt, {"repo": str(self.repo)}, facts,
                        judgment)
                    self.assertFalse(produced)
                    self.assertIsNone(head)
                    self.assertEqual(judgment["production_state"],
                                     "remote-ref-unreadable")
                    self.assertTrue(detail.isprintable())
                    if "\0" not in invalid:
                        self.assertTrue(any("check-ref-format" in a
                                            for a in calls))

    def test_fetch_destination_is_not_truncated_for_a_long_attempt_id(self):
        attempt, facts = self.remote_facts("topic", attempt_id="a" * 5000)
        seen = []

        def runner(argv, **kwargs):
            if "ls-remote" in argv:
                return 0, "c" * 40 + "\t" + facts["judgment_ref"] + "\n", ""
            if "fetch" in argv:
                seen.append(argv[-1])
                return 1, "", "stopped after capturing the raw refspec"
            return U.run(argv, **kwargs)

        produced, head, _detail = W.judge_detail(
            runner, attempt, {"repo": str(self.repo)}, facts)
        self.assertFalse(produced)
        self.assertIsNone(head)
        self.assertEqual(seen, ["+refs/heads/topic:refs/hanig-swarm/judgments/"
                                + attempt.name])

    def test_different_remote_answer_never_fetches(self):
        attempt, facts = self.remote_facts("topic\u200bname")
        ref = facts["judgment_ref"]

        def runner(argv, **kwargs):
            if "ls-remote" in argv:
                return 0, "c" * 40 + "\t" + ref.replace("\u200b", "\u200c") + "\n", ""
            if "fetch" in argv:
                self.fail("a different remote answer reached fetch")
            return U.run(argv, **kwargs)

        produced, head, detail = W.judge_detail(
            runner, attempt, {"repo": str(self.repo)}, facts)
        self.assertFalse(produced)
        self.assertIsNone(head)
        self.assertIn("invalid exact-ref answer", detail)


if __name__ == "__main__":
    unittest.main()
