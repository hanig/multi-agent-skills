"""Merge/recovery CLI with real coordinator/Git and forge on a replaced PATH."""

import ast
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "skills/hanig-swarm/scripts"
OPERATOR = SCRIPTS.parents[1] / "hanig-orchestrate/scripts/merge_unit.py"
sys.path.insert(0, str(SCRIPTS))
import swarm as S
import child_environment as CE


GH_STUB = r'''
import json, os, pathlib, sys
p = pathlib.Path(os.environ["FORGE_STATE"])
data = json.loads(p.read_text())
args = sys.argv[1:]
if os.environ.get("FORGE_EXPECT_TOKEN"):
    assert os.environ.get("GH_TOKEN") == os.environ["FORGE_EXPECT_TOKEN"]
with open(os.environ["FORGE_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
if args[:2] == ["pr", "view"]:
    if data["pr"]["state"] == "MERGED" and data.get("fail_view_once"):
        data["fail_view_once"] = False
        p.write_text(json.dumps(data))
        sys.exit(1)
    print(json.dumps(data["pr"]))
elif args[:2] == ["pr", "checks"]:
    print(json.dumps(data["checks"]))
    sys.exit(data.get("checks_exit", 0))
elif args[:2] == ["pr", "merge"]:
    intents = [json.loads(path.read_text()) for path in
               pathlib.Path(os.environ["COORDINATOR_STATE"]).glob("merge-unit-*.json")]
    intents = [intent for intent in intents if intent["phase"] == "merge_requested"]
    assert len(intents) == 1, "merge called without durable intent"
    intent = intents[0]
    assert intent["phase"] == "merge_requested"
    assert intent["preconditions"]["approver"] == "Operator"
    assert args[args.index("--match-head-commit") + 1] == intent["binding"]["head"]
    assert "--squash" in args
    data.setdefault("merge_operations", []).append(intent["operation_id"])
    p.write_text(json.dumps(data))
    if data.get("fail_merge"):
        sys.exit("transport failure before forge mutation")
    if not data.get("queued"):
        data["pr"]["state"] = "MERGED"
        data["pr"]["mergeCommit"] = {"oid": data["commit"]["sha"]}
        data["pr"]["baseRefOid"] = data["commit"]["sha"]
    p.write_text(json.dumps(data))
elif args[:1] == ["api"]:
    assert args[-1].endswith("/git/commits/" + data["commit"]["sha"])
    print(json.dumps(data["commit"]))
else:
    sys.exit("unexpected forge call: " + repr(args))
'''


class TestMergeUnit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name).resolve()
        self.operator = OPERATOR
        self.repo = self.directory / "repo"
        self.repo.mkdir()
        self.bin = self.directory / "bin"
        self.bin.mkdir()
        (self.bin / "git").symlink_to(shutil.which("git"))
        (self.bin / "python3").symlink_to(sys.executable)
        gh = self.bin / "gh"
        gh.write_text("#!" + sys.executable + "\n" + GH_STUB)
        gh.chmod(0o755)
        paseo = self.bin / "paseo"
        paseo.write_text("#!" + sys.executable + "\nraise SystemExit('unexpected worker call')\n")
        paseo.chmod(0o755)
        self.state_dir = self.directory / "state"
        self.state_dir.mkdir()
        self.root = self.directory / "runs"
        self.attempt = self.root / "u" / "a1"
        self.attempt.mkdir(parents=True)
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("GIT_", "SWARM_", "HANIG_"))}
        self.env.update({"PATH": str(self.bin), "HOME": str(self.directory / "home"),
                         "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                         "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
                         "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
                         "FORGE_STATE": str(self.directory / "forge.json"),
                         "FORGE_LOG": str(self.directory / "forge-calls.jsonl"),
                         "COORDINATOR_STATE": str(self.state_dir)})
        self.git("init", "-q")
        self.git("checkout", "-qb", "main")
        (self.repo / "change.txt").write_text("base\n")
        self.git("add", "change.txt")
        self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD")
        base_tree = self.git("rev-parse", "HEAD^{tree}")
        self.git("checkout", "-qb", "swarm-a1")
        (self.repo / "change.txt").write_text("produced\n")
        self.git("commit", "-qam", "produced")
        self.head = self.git("rev-parse", "HEAD")
        self.merged = self.git("commit-tree", "HEAD^{tree}", "-p", self.base, "-m", "squashed")
        self.remote = "https://github.com/example/project"
        self.unit = {"id": "u", "kind": "code", "repo": str(self.repo),
                     "target_branch": "main", "scope": ["change.txt"],
                     "prompt": "bounded work", "mode": "full-access", "outputs": ["evidence.md"]}
        self.plan = {"name": "merge-test", "units": [self.unit]}
        self.launch = {"schema_version": 5, "unit_id": "u", "attempt_id": "a1",
                       "launch_host": os.uname().nodename, "repo": str(self.repo),
                       "repository_remote": self.remote, "repository_remote_raw": self.remote,
                       "base_commit": self.base, "base_tree": base_tree,
                       "worktree_slug": "a1", "branch": "swarm-a1",
                       "target_branch": "main", "judgment_ref": "refs/heads/swarm-a1",
                       "captured_at": "test"}
        facts = dict(self.launch, schema_version=6, execution_workspace=str(self.repo),
                     workspace_identity={"realpath": str(self.repo)}, clean_at_launch=True)
        self.us = {"state": "READY_FOR_PR", "attempt_dir": str(self.attempt),
                   "gpu_hours": 0, "attempts": [str(self.attempt)], "job_id": "finished-agent",
                   "attempt_launch_intents": {"a1": self.launch},
                   "attempt_launch_facts": {"a1": facts},
                   "attempt_produced_heads": {"a1": self.head}}
        self.state = {"schema_version": 1, "units": {"u": self.us},
                      "root": str(self.root), "plan_digest": S.plan_digest(self.plan)}
        self.forge = {"pr": {"number": 7, "url": self.remote + "/pull/7", "state": "OPEN",
                             "headRefOid": self.head, "baseRefName": "main",
                             "baseRefOid": self.base, "mergeCommit": None},
                      "checks": [{"name": "test", "state": "SUCCESS"}],
                      "commit": {"sha": self.merged, "parents": [{"sha": self.base}]}}
        self.plan_path = self.directory / "plan.json"
        self.save()

    def git(self, *args):
        result = subprocess.run([str(self.bin / "git"), "-C", str(self.repo)] + list(args),
                                env=self.env, capture_output=True, text=True, check=True)
        return result.stdout.strip()

    def save(self):
        self.plan_path.write_text(json.dumps(self.plan))
        (self.state_dir / S.STATE_FILE).write_text(json.dumps(self.state))
        Path(self.env["FORGE_STATE"]).write_text(json.dumps(self.forge))

    def invoke(self, *extra, approver=True):
        command = [sys.executable, str(self.operator), str(self.plan_path),
                   "--state-dir", str(self.state_dir), "--unit", "u", "--pr", "7"]
        if approver:
            command += ["--approver", "Operator"]
        return subprocess.run(command + list(extra), cwd=str(self.repo), env=self.env,
                              capture_output=True, text=True, timeout=60)

    def calls(self, prefix=None):
        path = Path(self.env["FORGE_LOG"])
        rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [row for row in rows if prefix is None or row[:len(prefix)] == prefix]

    def receipts(self):
        return S.load_merge_receipts(self.state_dir)[0]

    def intent(self):
        paths = list(self.state_dir.glob("merge-unit-*.json"))
        self.assertEqual(len(paths), 1)
        return json.loads(paths[0].read_text())

    def assert_refused(self, result):
        self.assertEqual(self.calls(["pr", "merge"]), [], result.stdout + result.stderr)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.receipts(), [])

    def test_success_records_exact_anchor_and_advances_once(self):
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.calls(["pr", "merge"])), 1)
        receipt, = self.receipts()
        self.assertEqual(receipt["repo"], self.remote)
        self.assertEqual(receipt["head"], self.head)
        self.assertEqual(receipt["target_commit"], self.base)
        state = json.loads((self.state_dir / S.STATE_FILE).read_text())
        self.assertEqual(state["units"]["u"]["state"], "DONE", result.stdout)
        self.assertIn("--root " + str(self.root), result.stdout)
        self.assertEqual(self.intent()["phase"], "receipt_recorded")

    def test_tracker_close_is_printed_after_advance_and_acknowledged_separately(self):
        self.state_dir.rename(self.directory / "state with spaces")
        self.state_dir = self.directory / "state with spaces"
        self.env["COORDINATOR_STATE"] = str(self.state_dir)
        self.unit["tracker"] = "ARC-1"
        self.state["plan_digest"] = S.plan_digest(self.plan)
        self.save()
        old_keys = []
        for unit, attempt in (("u", "/runs/u/older"), ("other", str(self.attempt))):
            old_keys.append(S.emit_intent(
                self.state_dir, self.plan["name"], unit, "DONE",
                {"attempt_dir": attempt}, evidence={"receipt": {"state": "DONE"}}))
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        close, = [i for i in S.load_outbox_contract(self.state_dir)
                  if i["verb"] == "close" and i["unit"] == "u"
                  and i["attempt_dir"] == str(self.attempt)]
        for key in old_keys:
            self.assertNotIn(key, result.stdout)
        self.assertEqual(close["tracker"], "ARC-1")
        self.assertIn("key={} tracker='ARC-1'".format(close["key"]), result.stdout)
        self.assertLess(result.stdout.index("Merge receipt recorded and advance ran."),
                        result.stdout.index("Pending tracker close:"))
        self.assertEqual(S.acknowledgment_status(self.state_dir)[0], {})
        line, = [line for line in result.stdout.splitlines()
                 if "--record-receipt" in line]
        command = shlex.split(line)
        self.assertEqual(command, [sys.executable, str(SCRIPTS / "swarm.py"), "outbox",
                                  "--state-dir", str(self.state_dir), "--record-receipt",
                                  close["key"], "--ref", "ID"])
        # The displayed command is actually consumable after the session's
        # external action. This test supplies an attestation, not a tracker call.
        command[-1] = "ARC-1"
        ack = subprocess.run(command, cwd=str(self.repo), env=self.env,
                             capture_output=True, text=True)
        self.assertEqual(ack.returncode, 0, ack.stdout + ack.stderr)
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("Pending tracker close:", result.stdout)
        self.assertNotIn("--record-receipt", result.stdout)
        self.assertEqual(len(self.calls(["pr", "merge"])), 1)

    def test_tracker_close_without_declared_issue_is_explicit(self):
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        close, = [i for i in S.read_outbox(self.state_dir) if i["verb"] == "close"]
        self.assertNotIn("tracker", close)
        self.assertIn("key={} no tracker declared".format(close["key"]), result.stdout)
        self.assertIn("--record-receipt {} --ref ID".format(close["key"]), result.stdout)

    def test_forge_auth_survives_but_all_coordinator_children_are_contained(self):
        names = sorted(CE.DENIED_ENV_NAMES | {"SWARM_UNIT_TEST", "SWARM_DEP_TEST"})
        self.env.update({name: "synthetic-operator-secret" for name in names})
        self.env["FORGE_EXPECT_TOKEN"] = "synthetic-operator-secret"
        site = self.directory / "environment-probe"
        site.mkdir()
        log = self.directory / "coordinator-environments.jsonl"
        # Observe real scope-check, merge recording, and advance processes at
        # Python startup. The forge stub separately requires its fake token.
        (site / "sitecustomize.py").write_text(
            "import json, os, sys\n"
            "if len(sys.argv) > 1 and sys.argv[0].endswith('swarm.py'):\n"
            "    with open(%r, 'a') as log:\n"
            "        log.write(json.dumps([sys.argv[1], "
            "[n for n in %r if n in os.environ]]) + '\\n')\n" % (str(log), names))
        self.env["PYTHONPATH"] = str(site)
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(rows, [["scope-check", []], ["merge", []], ["advance", []]])
        self.assertEqual(len(self.calls(["pr", "merge"])), 1)

    def test_copied_operator_uses_its_installed_sibling_from_project_cwd(self):
        prefix = self.directory / "installed"
        operator = prefix / "hanig-orchestrate"
        swarm = prefix / "hanig-swarm"
        shutil.copytree(OPERATOR.parents[1], operator)
        shutil.copytree(SCRIPTS.parent, swarm)
        self.operator = operator / "scripts/merge_unit.py"
        self.env["HANIG_ORCHESTRATE_DIR"] = str(operator)
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(str(swarm / "scripts/swarm.py"), result.stdout)
        self.assertNotIn(str(SCRIPTS / "swarm.py"), result.stdout)

    def test_linked_operator_uses_explicit_dependency_parent(self):
        prefix = self.directory / "links"
        prefix.mkdir()
        operator = prefix / "hanig-orchestrate"
        operator.symlink_to(OPERATOR.parents[1], target_is_directory=True)
        dependencies = self.directory / "dependencies"
        swarm = dependencies / "hanig-swarm"
        shutil.copytree(SCRIPTS.parent, swarm)
        self.operator = operator / "scripts/merge_unit.py"
        self.env["HANIG_ORCHESTRATE_DIR"] = str(operator)
        self.env["HANIG_SKILL_DEP_ROOTS"] = str(dependencies)
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(str(swarm / "scripts/swarm.py"), result.stdout)

    def test_relative_dependency_parent_is_relative_to_loaded_skill(self):
        prefix = self.directory / "relative-install"
        operator = prefix / "hanig-orchestrate"
        dependencies = prefix / "deps"
        swarm = dependencies / "hanig-swarm"
        shutil.copytree(OPERATOR.parents[1], operator)
        shutil.copytree(SCRIPTS.parent, swarm)
        self.operator = operator / "scripts/merge_unit.py"
        self.env["HANIG_ORCHESTRATE_DIR"] = str(operator)
        self.env["HANIG_SKILL_DEP_ROOTS"] = "../deps"
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(str(swarm / "scripts/swarm.py"), result.stdout)

    def test_relative_dependency_parent_keeps_the_logical_link_parent(self):
        prefix = self.directory / "relative-link-install"
        prefix.mkdir()
        operator = prefix / "hanig-orchestrate"
        operator.symlink_to(OPERATOR.parents[1], target_is_directory=True)
        swarm = prefix / "deps/hanig-swarm"
        shutil.copytree(SCRIPTS.parent, swarm)
        self.operator = operator / "scripts/merge_unit.py"
        self.env["HANIG_ORCHESTRATE_DIR"] = str(operator)
        self.env["HANIG_SKILL_DEP_ROOTS"] = "../deps"
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(str(swarm / "scripts/swarm.py"), result.stdout)

    def test_missing_installed_sibling_refuses_before_forge_or_writes(self):
        operator = self.directory / "isolated" / "hanig-orchestrate"
        shutil.copytree(OPERATOR.parents[1], operator)
        self.operator = operator / "scripts/merge_unit.py"
        self.env["HANIG_ORCHESTRATE_DIR"] = str(operator)
        # A matching bundle in the project cwd is not a declared dependency.
        (self.repo / "hanig-swarm").symlink_to(SCRIPTS.parent, target_is_directory=True)
        before = {p.name: p.read_bytes() for p in self.state_dir.iterdir()}
        result = self.invoke()
        self.assert_refused(result)
        self.assertIn("missing declared installed dependency", result.stderr)
        self.assertEqual(self.calls(), [])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.state_dir.iterdir()})

    def test_head_mismatch_refuses_before_merge(self):
        self.forge["pr"]["headRefOid"] = "f" * 40
        self.save()
        self.assert_refused(self.invoke())

    def test_target_mismatch_refuses_before_merge(self):
        self.forge["pr"]["baseRefName"] = "elsewhere"
        self.save()
        self.assert_refused(self.invoke())

    def test_out_of_scope_refuses_before_merge(self):
        self.unit["scope"] = []
        self.state["plan_digest"] = S.plan_digest(self.plan)
        self.save()
        result = self.invoke()
        self.assert_refused(result)
        self.assertIn("scope-check exited 1", result.stderr)

    def test_unchecked_scope_refuses_before_merge(self):
        del self.unit["scope"]
        self.state["plan_digest"] = S.plan_digest(self.plan)
        self.save()
        result = self.invoke()
        self.assert_refused(result)
        self.assertIn("scope-check exited 2", result.stderr)

    def assert_scope_override(self, scope, expected_exit):
        self.unit["scope"] = scope
        if scope is None:
            del self.unit["scope"]
        self.state["plan_digest"] = S.plan_digest(self.plan)
        self.save()
        result = self.invoke("--allow-unchecked-scope", "Reviewed exception")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        observed = self.intent()["preconditions"]
        self.assertEqual(observed["scope_exit"], expected_exit)
        self.assertEqual(observed["allow_unchecked_scope"], "Reviewed exception")

    def test_scope_override_is_recorded_for_out_of_scope(self):
        self.assert_scope_override([], 1)

    def test_scope_override_is_recorded_for_unchecked(self):
        self.assert_scope_override(None, 2)

    def test_non_success_checks_refuse_before_merge(self):
        for status in ("FAILURE", "PENDING", "IN_PROGRESS", "QUEUED", "NEUTRAL", "SKIPPED", None):
            with self.subTest(status=status):
                self.forge["checks"].append({"name": "other", "state": status})
                self.save()
                self.assert_refused(self.invoke())
                self.forge["checks"].pop()

    def test_checks_command_failure_refuses_before_merge(self):
        self.forge["checks_exit"] = 8
        self.save()
        self.assert_refused(self.invoke())

    def test_empty_checks_refuse_before_merge(self):
        self.forge["checks"] = []
        self.save()
        self.assert_refused(self.invoke())

    def test_merged_reconciliation_ignores_later_ci_failure(self):
        self.forge["pr"].update(state="MERGED", mergeCommit={"oid": self.merged})
        self.forge["checks"] = [{"name": "test rerun", "state": "PENDING"}]
        self.forge["checks_exit"] = 8
        self.save()
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls(["pr", "merge"]), [])
        self.assertEqual(self.calls(["pr", "checks"]), [])
        self.assertEqual(len(self.receipts()), 1)

    def test_merged_reconciliation_does_not_reapply_scope_policy(self):
        self.forge["pr"].update(state="MERGED", mergeCommit={"oid": self.merged})
        self.unit["scope"] = []
        self.state["plan_digest"] = S.plan_digest(self.plan)
        self.save()
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn(" scope-check ", result.stdout)
        self.assertEqual(self.calls(["pr", "merge"]), [])

    def test_trailing_slash_remote_preserves_exact_receipt(self):
        self.launch["repository_remote"] += "/"
        self.us["attempt_launch_facts"]["a1"]["repository_remote"] += "/"
        self.save()
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.receipts()[0]["repo"], self.remote + "/")

    def test_http_forge_url_preserves_observed_pr_url(self):
        self.launch["repository_remote"] = "http://forge.example/example/project"
        self.us["attempt_launch_facts"]["a1"]["repository_remote"] = self.launch["repository_remote"]
        self.forge["pr"]["url"] = "http://forge.example/example/project/pull/7"
        self.save()
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.receipts()[0]["pr"], self.forge["pr"]["url"])
        self.assertEqual(self.receipts()[0]["repo"], self.launch["repository_remote"])

    def scope_startup_notice(self):
        # Exercise the real CLI with a Python startup diagnostic on stdout.
        # The operator, gh stub and advance keep their ordinary output.
        site = self.directory / "startup"
        site.mkdir()
        (site / "sitecustomize.py").write_text(
            "import sys\n"
            "if len(sys.argv) > 1 and sys.argv[0].endswith('swarm.py') "
            "and sys.argv[1] == 'scope-check':\n"
            "    print('scope startup diagnostic')\n")
        self.env["PYTHONPATH"] = str(site)

    def test_explicit_scope_exception_retains_non_json_output(self):
        self.scope_startup_notice()
        self.unit["scope"] = []
        self.state["plan_digest"] = S.plan_digest(self.plan)
        self.save()
        result = self.invoke("--allow-unchecked-scope", "Accept failed scope observation")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        observed = self.intent()["preconditions"]
        self.assertEqual(observed["scope_exit"], 1)
        self.assertIn("scope startup diagnostic", observed["scope"]["unparsed_stdout"])

    def test_scope_exception_retains_schema_invalid_json_verbatim(self):
        site = self.directory / "scope-exception-output"
        site.mkdir()
        (site / "sitecustomize.py").write_text(
            "import atexit, io, os, sys\n"
            "if len(sys.argv) > 1 and sys.argv[0].endswith('swarm.py') "
            "and sys.argv[1] == 'scope-check':\n"
            "    original = sys.stdout\n"
            "    sys.stdout = io.StringIO()\n"
            "    atexit.register(lambda: original.write(os.environ['SCOPE_OUTPUT']))\n"
            "    sys.stderr.write('scope diagnostic\\n')\n")
        self.env["PYTHONPATH"] = str(site)
        self.env["SCOPE_OUTPUT"] = ' [ "scope diagnostic" ] \n'
        self.unit["scope"] = []
        self.state["plan_digest"] = S.plan_digest(self.plan)
        self.save()
        result = self.invoke("--allow-unchecked-scope", "Accept failed observation")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        observed = self.intent()["preconditions"]
        self.assertEqual(observed["scope_exit"], 1)
        self.assertEqual(observed.get("scope_stdout"), self.env["SCOPE_OUTPUT"])
        self.assertEqual(observed.get("scope_stderr"), "scope diagnostic\n")

    def test_malformed_successful_scope_output_is_not_an_exception(self):
        self.scope_startup_notice()
        self.assert_refused(self.invoke("--allow-unchecked-scope", "Not applicable"))

    def test_schema_invalid_successful_scope_output_refuses(self):
        # Run the real scope-check to completion but corrupt its output
        # channel. This simulates a broken producer, not an authority grant.
        observed = subprocess.run(
            [sys.executable, str(SCRIPTS / "swarm.py"), "scope-check",
             str(self.plan_path), "--state-dir", str(self.state_dir),
             "--unit", "u", "--json"], env=self.env, cwd=self.repo,
            capture_output=True, text=True, check=True)
        valid = json.loads(observed.stdout)
        payloads = [None, [], {}, {"status": "in_scope"}]
        for key, value in (("status", "unchecked"), ("unit", "other"),
                           ("attempt", "older"), ("head", "f" * 40),
                           ("base", None), ("scope", "**"), ("scope", [None]),
                           ("out_of_scope", ["outside"]),
                           ("deletions_out_of_scope", ["removed"])):
            payloads.append(dict(valid, **{key: value}))
        site = self.directory / "scope-output-probe"
        site.mkdir()
        (site / "sitecustomize.py").write_text(
            "import atexit, io, os, sys\n"
            "if len(sys.argv) > 1 and sys.argv[0].endswith('swarm.py') "
            "and sys.argv[1] == 'scope-check':\n"
            "    original = sys.stdout\n"
            "    sys.stdout = io.StringIO()\n"
            "    atexit.register(lambda: original.write(os.environ['SCOPE_OUTPUT']))\n")
        self.env["PYTHONPATH"] = str(site)
        for payload in payloads:
            with self.subTest(payload=payload):
                # Keep each corrupted-result trial independent, including
                # when a mutation incorrectly merged an earlier trial.
                self.save()
                for path in self.state_dir.glob("merge-unit-*.json"):
                    path.unlink()
                for path in (self.state_dir / S.MERGE_RECEIPTS,
                             Path(self.env["FORGE_LOG"])):
                    if path.exists():
                        path.unlink()
                self.env["SCOPE_OUTPUT"] = json.dumps(payload)
                self.assert_refused(self.invoke("--allow-unchecked-scope", "Not applicable"))

    def test_forge_pr_url_cannot_change_repository(self):
        self.forge["pr"]["url"] = "https://other.example/example/project/pull/7"
        self.save()
        self.assert_refused(self.invoke())

    def test_explicit_remote_port_is_not_silently_discarded(self):
        self.launch["repository_remote"] = "https://forge.example:8443/example/project"
        self.save()
        self.assert_refused(self.invoke())
        self.assertEqual(self.calls(), [])

    def test_already_merged_reconciles_without_merge_call(self):
        self.forge["pr"].update(state="MERGED", mergeCommit={"oid": self.merged},
                                 baseRefOid="e" * 40)
        self.save()
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls(["pr", "merge"]), [])
        self.assertEqual(self.receipts()[0]["target_commit"], self.base)
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.receipts()), 1)

    def test_merged_at_wrong_head_refuses(self):
        self.forge["pr"].update(state="MERGED", headRefOid="f" * 40,
                                 mergeCommit={"oid": self.merged})
        self.save()
        self.assert_refused(self.invoke())

    def test_missing_or_blank_approver_refuses_without_forge_calls(self):
        for args in ([], ["--approver", "   "]):
            result = self.invoke(*args, approver=False)
            self.assert_refused(result)
            self.assertEqual(self.calls(), [])

    def test_dry_run_prints_commands_without_forge_or_state_writes(self):
        before = {p.name: p.read_bytes() for p in self.state_dir.iterdir()}
        result = self.invoke("--dry-run")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(self.calls(), [])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.state_dir.iterdir()})
        for expected in ("gh pr view", "scope-check", "gh pr checks", "gh pr merge",
                         "gh api", "--target-commit", "--root", "persist intent",
                         "<observed-pr-url>"):
            self.assertIn(expected, result.stdout)

    def test_missing_root_refuses_and_explicit_root_is_forwarded(self):
        del self.state["root"]
        self.save()
        self.assert_refused(self.invoke())
        self.assertEqual(self.calls(), [])
        result = self.invoke("--root", str(self.root))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("--root " + str(self.root), result.stdout)

    def test_conflicting_root_refuses_before_forge(self):
        self.assert_refused(self.invoke("--root", str(self.directory / "wrong")))
        self.assertEqual(self.calls(), [])

    def test_lost_merge_response_reconciles_stable_intent(self):
        self.forge["fail_view_once"] = True
        self.save()
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.calls(["pr", "merge"])), 1)
        self.assertEqual(self.receipts(), [])
        operation_id = self.intent()["operation_id"]
        preconditions = self.intent()["preconditions"]
        forge_path = Path(self.env["FORGE_STATE"])
        forge = json.loads(forge_path.read_text())
        forge["checks"] = [{"name": "rerun", "state": "PENDING"}]
        forge["checks_exit"] = 8
        forge_path.write_text(json.dumps(forge))
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.calls(["pr", "merge"])), 1)
        self.assertEqual(self.intent()["operation_id"], operation_id)
        self.assertEqual(self.intent()["preconditions"], preconditions)
        self.assertEqual(len(self.receipts()), 1)

    def test_unresolved_merge_request_is_never_resubmitted(self):
        self.forge["queued"] = True
        self.save()
        self.assertNotEqual(self.invoke().returncode, 0)
        self.assertNotEqual(self.invoke().returncode, 0)
        self.assertEqual(len(self.calls(["pr", "merge"])), 1)
        self.assertEqual(self.receipts(), [])

    def leave_transport_failure(self):
        self.forge["fail_merge"] = True
        self.save()
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("transport failure", result.stderr)
        self.assertEqual(len(self.calls(["pr", "merge"])), 1)
        self.assertEqual(self.receipts(), [])
        return self.intent()

    def abandon_intent(self, intent, *extra, approver=True):
        return self.invoke("--abandon-intent", intent["operation_id"],
                           "--reason", "Confirmed request did not reach forge",
                           *extra, approver=approver)

    def abandonment_records(self):
        return list(self.state_dir.glob("merge-abandonment-*.json"))

    def trace_coordinator_calls(self):
        site = self.directory / "coordinator-probe"
        site.mkdir()
        log = self.directory / "coordinator-calls.jsonl"
        (site / "sitecustomize.py").write_text(
            "import json, sys\n"
            "if len(sys.argv) > 1 and sys.argv[0].endswith('swarm.py'):\n"
            "    with open(%r, 'a') as log:\n"
            "        log.write(json.dumps(sys.argv[1]) + '\\n')\n" % str(log))
        self.env["PYTHONPATH"] = str(site)
        return log

    def assert_abandon_refused(self, result):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.calls(["pr", "merge"])), 1)
        self.assertEqual(self.receipts(), [])
        self.assertEqual(self.abandonment_records(), [])
        self.assertEqual(self.intent()["phase"], "merge_requested")

    def test_abandon_open_retains_record_and_permits_exactly_one_new_operation(self):
        coordinator_log = self.trace_coordinator_calls()
        intent = self.leave_transport_failure()
        result = self.abandon_intent(intent)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        record_path, = self.abandonment_records()
        record_bytes = record_path.read_bytes()
        record = json.loads(record_bytes)
        self.assertEqual(record["intent"], intent)
        self.assertEqual(record["operation_id"], intent["operation_id"])
        self.assertEqual(record["approver"], "Operator")
        self.assertEqual(record["reason"], "Confirmed request did not reach forge")
        self.assertEqual(record["observed_pr"], self.forge["pr"])
        self.assertIsNotNone(datetime.fromisoformat(record["observed_at"]).tzinfo)
        self.assertEqual(self.intent(), dict(intent, phase="resolved_by_abandonment",
                                             abandonment=record_path.name))
        self.assertEqual(len(self.calls(["pr", "merge"])), 1)
        self.assertEqual(len(self.calls(["pr", "checks"])), 1)
        self.assertEqual(self.receipts(), [])
        self.assertEqual([json.loads(line) for line in coordinator_log.read_text().splitlines()],
                         ["scope-check"])
        # A second transport failure consumes the one new operation. A further
        # ordinary call cannot silently reuse this abandonment to merge again.
        self.assertNotEqual(self.invoke().returncode, 0)
        self.assertNotEqual(self.invoke().returncode, 0)
        self.assertEqual(len(self.calls(["pr", "merge"])), 2)
        forge = json.loads(Path(self.env["FORGE_STATE"]).read_text())
        self.assertEqual(len(set(forge["merge_operations"])), 2)
        self.assertEqual(len(list(self.state_dir.glob("merge-unit-*.json"))), 2)
        self.assertEqual(record_path.read_bytes(), record_bytes)
        # Naming the old operation cannot resolve the new pending request.
        self.assertNotEqual(self.abandon_intent(intent).returncode, 0)
        self.assertEqual(len(self.calls(["pr", "merge"])), 2)

    def test_abandon_then_successful_merge_rechecks_and_records_new_operation(self):
        intent = self.leave_transport_failure()
        self.assertEqual(self.abandon_intent(intent).returncode, 0)
        forge_path = Path(self.env["FORGE_STATE"])
        forge = json.loads(forge_path.read_text())
        forge["fail_merge"] = False
        forge_path.write_text(json.dumps(forge))
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(" scope-check ", result.stdout)
        self.assertEqual(len(self.calls(["pr", "checks"])), 2)
        self.assertEqual(len(self.calls(["pr", "merge"])), 2)
        self.assertEqual(len(set(json.loads(forge_path.read_text())["merge_operations"])), 2)
        self.assertEqual(len(self.receipts()), 1)
        state = json.loads((self.state_dir / S.STATE_FILE).read_text())
        self.assertEqual(state["units"]["u"]["state"], "DONE")
        self.assertEqual(self.invoke().returncode, 0)
        self.assertEqual(len(self.calls(["pr", "merge"])), 2)

    def test_abandon_merged_at_judged_head_reconciles_instead(self):
        coordinator_log = self.trace_coordinator_calls()
        intent = self.leave_transport_failure()
        self.forge["pr"].update(state="MERGED", mergeCommit={"oid": self.merged})
        self.forge["checks"] = [{"name": "rerun", "state": "FAILURE"}]
        self.save()
        prior_calls = len(self.calls())
        result = self.abandon_intent(intent)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([call[:2] for call in self.calls()[prior_calls:]],
                         [["pr", "view"], ["api", "--hostname"]])
        self.assertEqual(self.abandonment_records(), [])
        self.assertEqual(self.intent()["phase"], "receipt_recorded")
        self.assertEqual(self.receipts()[0]["head"], self.head)
        self.assertEqual([json.loads(line) for line in coordinator_log.read_text().splitlines()],
                         ["scope-check", "merge", "advance"])
        state = json.loads((self.state_dir / S.STATE_FILE).read_text())
        self.assertEqual(state["units"]["u"]["state"], "DONE")

    def test_abandon_merged_elsewhere_refuses(self):
        intent = self.leave_transport_failure()
        self.forge["pr"].update(state="MERGED", headRefOid="f" * 40,
                                 mergeCommit={"oid": self.merged})
        self.save()
        result = self.abandon_intent(intent)
        self.assert_abandon_refused(result)
        self.assertIn("PR head does not equal coordinator-judged head", result.stderr)

    def test_abandon_closed_moved_head_or_retargeted_pr_refuses(self):
        intent = self.leave_transport_failure()
        original_pr = dict(self.forge["pr"])
        for changed in ({"state": "CLOSED"}, {"headRefOid": "f" * 40},
                        {"baseRefName": "elsewhere"}):
            with self.subTest(changed=changed):
                self.forge["pr"] = dict(original_pr, **changed)
                self.save()
                self.assert_abandon_refused(self.abandon_intent(intent))

    def test_abandon_missing_or_blank_approver_and_reason_refuses(self):
        intent = self.leave_transport_failure()
        calls = self.calls()
        for extra in ([], ["--reason", "valid"], ["--approver", "Operator"],
                      ["--approver", " ", "--reason", "valid"],
                      ["--approver", "Operator", "--reason", " "],
                      ["--approver", "Operator", "--reason", "line\nbreak"]):
            with self.subTest(extra=extra):
                self.assert_abandon_refused(self.invoke(
                    "--abandon-intent", intent["operation_id"], *extra, approver=False))
                self.assertEqual(self.calls(), calls)

    def test_abandon_missing_wrong_binding_or_resolved_operation_refuses(self):
        result = self.invoke("--abandon-intent", "a" * 64, "--reason", "checked")
        self.assert_refused(result)
        self.assertEqual(self.calls(), [])
        intent = self.leave_transport_failure()
        calls = self.calls()
        self.assert_abandon_refused(self.invoke(
            "--abandon-intent", "a" * 64, "--reason", "checked"))
        self.assert_abandon_refused(self.abandon_intent(intent, "--pr", "8"))
        self.assertEqual(self.calls(), calls)
        self.assertEqual(self.abandon_intent(intent).returncode, 0)
        before = {p.name: p.read_bytes() for p in self.state_dir.glob("merge-*.json")}
        result = self.abandon_intent(intent)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("current unresolved intent", result.stderr)
        self.assertEqual(before, {p.name: p.read_bytes()
                                 for p in self.state_dir.glob("merge-*.json")})

    def test_abandon_dry_run_does_not_call_forge_or_change_state(self):
        intent = self.leave_transport_failure()
        before = {p.name: p.read_bytes() for p in self.state_dir.iterdir()}
        calls = self.calls()
        result = self.abandon_intent(intent, "--dry-run")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertNotIn("gh pr merge", result.stdout)
        self.assertEqual(self.calls(), calls)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.state_dir.iterdir()})

    def interrupt_abandonment_write(self, sidecar=False):
        site = self.directory / "write-interruption"
        site.mkdir()
        (site / "sitecustomize.py").write_text(
            "import json, os, pathlib\n"
            "replace = os.replace\n"
            "def interrupted(src, dst):\n"
            "    name = pathlib.Path(dst).name\n"
            "    if %r:\n"
            "        if name.startswith('merge-abandonment-'):\n"
            "            raise OSError('injected abandonment publication failure')\n"
            "    elif name.startswith('merge-unit-'):\n"
            "        if json.loads(pathlib.Path(src).read_text()).get('phase') == "
            "'resolved_by_abandonment':\n"
            "            raise OSError('injected resolution marker failure')\n"
            "    return replace(src, dst)\n"
            "os.replace = interrupted\n" % sidecar)
        self.env["PYTHONPATH"] = str(site)

    def test_abandonment_commit_survives_interrupted_intent_marker(self):
        intent = self.leave_transport_failure()
        self.interrupt_abandonment_write()
        result = self.abandon_intent(intent)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("injected resolution marker failure", result.stderr)
        record_path, = self.abandonment_records()
        record_bytes = record_path.read_bytes()
        self.assertEqual(self.intent(), intent)
        del self.env["PYTHONPATH"]
        # The ordinary consumer repairs persisted state, then attempts only
        # one successor; the still-active transport stub leaves that uncertain.
        self.assertNotEqual(self.invoke().returncode, 0)
        retained = self.state_dir / ("merge-unit-" + intent["operation_id"] + ".json")
        self.assertEqual(json.loads(retained.read_text())["phase"], "resolved_by_abandonment")
        self.assertEqual(record_path.read_bytes(), record_bytes)
        self.assertEqual(len(self.calls(["pr", "merge"])), 2)
        self.assertNotEqual(self.invoke().returncode, 0)
        self.assertEqual(len(self.calls(["pr", "merge"])), 2)

    def test_failed_abandonment_publication_keeps_original_request_unresolved(self):
        intent = self.leave_transport_failure()
        self.interrupt_abandonment_write(sidecar=True)
        result = self.abandon_intent(intent)
        self.assert_abandon_refused(result)
        self.assertIn("injected abandonment publication failure", result.stderr)
        del self.env["PYTHONPATH"]
        self.assert_abandon_refused(self.invoke())

    def test_resolved_intent_without_its_abandonment_record_refuses(self):
        intent = self.leave_transport_failure()
        self.assertEqual(self.abandon_intent(intent).returncode, 0)
        record_path, = self.abandonment_records()
        record_path.unlink()
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no valid resolution record", result.stderr)
        self.assertEqual(len(self.calls(["pr", "merge"])), 1)

    def test_legacy_intent_metadata_does_not_block_merged_reconciliation(self):
        intent = self.leave_transport_failure()
        path = self.state_dir / ("merge-unit-" + intent["operation_id"] + ".json")
        for field, value in (("phase", None), ("phase", "submitted"),
                             ("phase", "absent"), ("operation_id", "absent"),
                             ("operation_id", "older-metadata")):
            with self.subTest(field=field, value=value):
                legacy = dict(intent, **{field: value})
                if value == "absent":
                    del legacy[field]
                path.write_text(json.dumps(legacy))
                self.forge["pr"].update(state="MERGED", mergeCommit={"oid": self.merged})
                self.save()
                receipt_path = self.state_dir / S.MERGE_RECEIPTS
                if receipt_path.exists():
                    receipt_path.unlink()
                result = self.invoke()
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(len(self.calls(["pr", "merge"])), 1)
                self.assertEqual(self.receipts()[0]["head"], self.head)
                self.assertEqual(self.intent()["phase"], "receipt_recorded")
                self.assertEqual(self.intent()["operation_id"], intent["operation_id"])
                self.assertEqual(self.abandonment_records(), [])

    def test_legacy_intent_metadata_does_not_allow_an_open_request_to_repeat(self):
        intent = self.leave_transport_failure()
        path = self.state_dir / ("merge-unit-" + intent["operation_id"] + ".json")
        for field in ("phase", "operation_id"):
            with self.subTest(field=field):
                legacy = dict(intent)
                del legacy[field]
                path.write_text(json.dumps(legacy))
                result = self.invoke()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("unresolved outcome", result.stderr)
                self.assertEqual(len(self.calls(["pr", "merge"])), 1)
                self.assertEqual(self.receipts(), [])

    def test_abandon_uses_binding_identity_instead_of_legacy_id_metadata(self):
        intent = self.leave_transport_failure()
        legacy = dict(intent)
        del legacy["operation_id"]
        path = self.state_dir / ("merge-unit-" + intent["operation_id"] + ".json")
        path.write_text(json.dumps(legacy))
        result = self.abandon_intent(intent)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        record_path, = self.abandonment_records()
        record = json.loads(record_path.read_text())
        self.assertEqual(record["operation_id"], intent["operation_id"])
        self.assertEqual(record["intent"], legacy)
        self.assertNotEqual(self.invoke().returncode, 0)  # second transport failure
        self.assertNotEqual(self.invoke().returncode, 0)  # never resubmitted
        forge = json.loads(Path(self.env["FORGE_STATE"]).read_text())
        self.assertEqual(len(set(forge["merge_operations"])), 2)
        self.assertEqual(len(self.calls(["pr", "merge"])), 2)

    def test_advance_failure_preserves_receipt_for_retry(self):
        self.state["halted"] = "test hold"
        self.save()
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(len(self.receipts()), 1)
        state_path = self.state_dir / S.STATE_FILE
        state = json.loads(state_path.read_text())
        state["halted"] = None
        state_path.write_text(json.dumps(state))
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.receipts()), 1)
        self.assertEqual(len(self.calls(["pr", "merge"])), 1)

    def test_bad_persisted_repo_receipt_is_corrected(self):
        self.forge["pr"].update(state="MERGED", mergeCommit={"oid": self.merged})
        self.save()
        receipt = {"unit": "u", "repo": "example/project", "pr": self.remote + "/pull/7",
                   "head": self.head, "target": "main", "target_commit": self.base,
                   "merged_as": self.merged, "method": "squash", "merged": True}
        (self.state_dir / S.MERGE_RECEIPTS).write_text(json.dumps(receipt) + "\n")
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.receipts()[-1]["repo"], self.remote)
        state = json.loads((self.state_dir / S.STATE_FILE).read_text())
        self.assertEqual(state["units"]["u"]["merge_receipt"]["repo"], self.remote)

    def test_worker_files_cannot_supply_missing_authority(self):
        (self.attempt / "receipt.json").write_text(json.dumps({"produced_head": self.head}))
        self.us["attempt_produced_heads"] = {"older": self.head}
        self.us["produced_head"] = self.head
        self.save()
        self.assert_refused(self.invoke("--allow-unchecked-scope", "exception"))
        self.assertEqual(self.calls(), [])

    def test_plan_drift_refuses_before_forge(self):
        self.unit["scope"] = ["**"]
        self.save()
        self.assert_refused(self.invoke())
        self.assertEqual(self.calls(), [])

    def test_coordinator_lock_refuses_before_forge(self):
        ok, _ = S.acquire_lease(self.state_dir)
        self.assertTrue(ok)
        try:
            self.assert_refused(self.invoke())
            self.assertEqual(self.calls(), [])
        finally:
            S.release_lease(self.state_dir)

    def test_state_in_worktree_refuses_without_creating_lock(self):
        self.state_dir = self.repo / "bad-state"
        self.state_dir.mkdir()
        self.save()
        self.assert_refused(self.invoke())
        self.assertFalse((self.state_dir / S.LOCK).exists())

    def test_coordinator_never_imports_network_operator(self):
        imports = []
        for node in ast.walk(ast.parse((SCRIPTS / "swarm.py").read_text())):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module)
        self.assertNotIn("merge_unit", imports)


if __name__ == "__main__":
    unittest.main()
