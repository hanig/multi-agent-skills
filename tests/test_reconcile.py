"""The read-only operator exercised through a forge stub on a replaced PATH."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/hanig-orchestrate/scripts"
sys.path.insert(0, str(SCRIPTS))
import reconcile as R


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.state = self.root / "state"
        self.state.mkdir()
        self.remote = "https://github.com/example/project"
        self.write_state()
        self.forge = self.root / "forge.json"
        self.calls = self.root / "calls.jsonl"
        self.env = dict(os.environ, PATH=str(self.bin), FORGE=str(self.forge), CALLS=str(self.calls),
                        PYTHONDONTWRITEBYTECODE="1")
        (self.bin / "gh").write_text("#!" + sys.executable + "\n" + '''import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ['CALLS'], 'a') as output:
    output.write(json.dumps(args) + '\\n')
if args[:2] == ['repo', 'view']:
    assert args[3:] == ['--json', 'url'], args
    print(os.environ.get('GH_REPO_RAW', json.dumps({'url': os.environ.get(
        'GH_RESOLVED_URL', 'https://github.com/example/project')})))
    sys.exit(int(os.environ.get('GH_REPO_EXIT', '0')))
assert args[:2] == ['api', 'graphql'], args
assert 'states:MERGED' in args[args.index('-f') + 1], args
pages = json.loads(Path(os.environ['FORGE']).read_text())
if isinstance(pages, dict) and 'raw' in pages:
    print(pages['raw'])
    sys.exit(0)
if isinstance(pages, dict) and 'exit' in pages:
    print('forge unavailable', file=sys.stderr)
    sys.exit(pages['exit'])
cursor = next((arg[7:] for arg in args if arg.startswith('cursor=')), '0')
print(json.dumps(pages[int(cursor)]))
''')
        (self.bin / "gh").chmod(0o755)
        (self.bin / "git").write_text("#!" + sys.executable + "\nimport sys\nassert sys.argv[1:] == ['remote', 'get-url', 'origin']\nprint('" + self.remote + "')\n")
        (self.bin / "git").chmod(0o755)
        self.set_pages([])

    def write_state(self, remote=None, directory=None):
        directory = directory or self.state
        (directory / R.S.STATE_FILE).write_text(json.dumps({"units": {"u": {
            "attempt_launch_facts": {"a1": {"repository_remote": remote or self.remote}}}}}))

    def pr(self, number=7, merged="2026-09-25T12:00:00Z", updated=None):
        return {"number": number, "url": self.remote + "/pull/" + str(number),
                "state": "MERGED", "baseRefName": "main", "headRefOid": "a" * 40,
                "mergeCommit": {"oid": "b" * 40}, "mergedAt": merged, "updatedAt": updated or merged}

    def set_pages(self, *pages):
        self.forge.write_text(json.dumps([{"data": {"repository": {"pullRequests": {
            "nodes": page, "pageInfo": {"hasNextPage": index < len(pages) - 1,
                                        "endCursor": str(index + 1)}}}}}
            for index, page in enumerate(pages)]))

    def record(self, directory=None, **changes):
        record = {"schema_version": 1, "operation_id": "test", "phase": "receipt_recorded",
                  "binding": {"repo": self.remote, "target": "main", "pr": 7, "head": "a" * 40},
                  "merged_as": "b" * 40}
        record.update(changes)
        ((directory or self.state) / "merge-unit-test.json").write_text(json.dumps(record))

    def run_cli(self, *extra, json_output=True):
        args = [sys.executable, str(SCRIPTS / "reconcile.py"), "--state-dir", str(self.state), "--limit", "100"]
        if json_output:
            args.append("--json")
        proc = subprocess.run(args + list(extra), env=self.env, cwd=str(self.root),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
        return proc.returncode, json.loads(proc.stdout) if json_output else proc.stdout

    def kinds(self, report):
        return [f["kind"] for f in report["findings"]]

    def test_unmediated_merge(self):
        self.set_pages([self.pr()])
        code, report = self.run_cli()
        self.assertEqual(code, 1, report)
        self.assertIn("UNMEDIATED MERGE", self.kinds(report))
        self.assertEqual(report["findings"][0]["pr"], 7)

    def test_unreadable_state_never_clean(self):
        shutil.rmtree(self.state)
        code, output = self.run_cli(json_output=False)
        self.assertEqual(code, 2, output)
        self.assertNotIn("CLEAN", output)
        self.assertIn("UNREADABLE SOURCE", output)

    def test_mismatched_source_never_clean(self):
        self.write_state("https://github.com/other/repository")
        code, report = self.run_cli()
        self.assertEqual(code, 1, report)
        self.assertIn("MISMATCHED SOURCE", self.kinds(report))

    def test_matching_record_and_state_are_unchanged(self):
        self.set_pages([self.pr()])
        self.record()
        def snapshot():
            return {str(p.relative_to(self.state)): p.read_bytes() for p in self.state.rglob('*') if p.is_file()}
        before = snapshot()
        code, report = self.run_cli()
        self.assertEqual(code, 0, report)
        self.assertEqual(report["status"], "CLEAN")
        self.assertEqual(before, snapshot())

    def test_obligation_and_receipt_use_coordinator_readers(self):
        key = R.S.emit_intent(self.state, "project", "u", "SUBMITTED", {"attempt_dir": "/attempt/a1"}, kind="code")
        self.assertIsNotNone(key)
        before = {p.name: p.read_bytes() for p in self.state.iterdir()}
        code, report = self.run_cli()
        self.assertEqual(code, 1, report)
        self.assertEqual(self.kinds(report), ["UNACKNOWLEDGED OBLIGATION"])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.state.iterdir()})
        R.S.record_receipt(self.state, key, "test-reference")
        code, report = self.run_cli()
        self.assertEqual(code, 0, report)

    def test_record_must_match_repo_target_pr_head_and_merge(self):
        self.set_pages([self.pr()])
        for field, value in (("repo", "https://github.com/other/repo"), ("target", "other"),
                             ("pr", 8), ("head", "c" * 40)):
            self.record()
            path = self.state / "merge-unit-test.json"
            data = json.loads(path.read_text())
            data["binding"][field] = value
            path.write_text(json.dumps(data))
            with self.subTest(field=field):
                code, report = self.run_cli()
                self.assertEqual(code, 1, report)
                self.assertIn("UNMEDIATED MERGE", self.kinds(report))
        self.record(merged_as="c" * 40)
        self.assertEqual(self.run_cli()[0], 1)

    def test_pending_request_accounts_for_lost_response_but_cancellation_does_not(self):
        self.set_pages([self.pr()])
        self.record(phase="merge_requested", merged_as=None)
        self.assertEqual(self.run_cli()[0], 0)
        for phase in ("cancelled_before_request", "resolved_by_abandonment"):
            self.record(phase=phase)
            self.assertEqual(self.run_cli()[0], 1)

    def test_other_state_can_supply_record_but_mismatched_state_cannot(self):
        self.set_pages([self.pr()])
        other = self.root / "second"
        other.mkdir()
        self.write_state(directory=other)
        self.record(directory=other)
        self.assertEqual(self.run_cli("--state-dir", str(other))[0], 0)
        self.write_state("https://github.com/other/repo", directory=other)
        code, report = self.run_cli("--state-dir", str(other))
        self.assertEqual(code, 1)
        self.assertIn("UNMEDIATED MERGE", self.kinds(report))

    def test_foreign_source_obligations_are_not_attributed_to_requested_repo(self):
        self.write_state("https://github.com/other/repository")
        key = R.S.emit_intent(self.state, "foreign", "u", "SUBMITTED",
                             {"attempt_dir": "/attempt/a1"}, kind="code")
        self.assertIsNotNone(key)
        code, report = self.run_cli()
        self.assertEqual(code, 1, report)
        self.assertEqual(self.kinds(report), ["MISMATCHED SOURCE"])
        # Reading every source still matters: a foreign corrupt journal is
        # unreadable, rather than silently skipped after identity mismatch.
        (self.state / R.S.RECEIPTS).write_text("{}\n")
        self.assertEqual(self.run_cli()[0], 2)

    def test_historical_pr_head_is_distinct_from_current_branch_target(self):
        # Mirrors the observed GitHub field distinction, e.g. cpython PR 154042.
        # No network: the forge stub supplies a later live branch target.
        pr = self.pr()
        pr["headRef"] = {"target": {"oid": "c" * 40}}
        self.set_pages([pr])
        self.record()
        self.assertEqual(self.run_cli()[0], 0)

    def test_bad_state_outbox_receipts_and_merge_journal_are_unreadable(self):
        for name, content in ((R.S.STATE_FILE, '{}'), (R.S.OUTBOX, '{bad}\n'),
                              (R.S.RECEIPTS, '{}\n'), ('merge-unit-bad.json', '{}')):
            with self.subTest(name=name):
                path = self.state / name
                path.write_text(content)
                code, report = self.run_cli()
                self.assertEqual(code, 2, report)
                self.assertNotEqual(report["status"], "CLEAN")
                path.unlink()
                self.write_state()
        (self.state / R.S.OUTBOX).mkdir()
        self.assertEqual(self.run_cli()[0], 2)

    def test_deep_json_in_every_reader_is_unreadable(self):
        nested = "[" * 10000 + "0" + "]" * 10000
        for name in (R.S.STATE_FILE, R.S.OUTBOX, R.S.RECEIPTS, "merge-unit-deep.json"):
            with self.subTest(name=name):
                path = self.state / name
                path.write_text(nested)
                code, report = self.run_cli()
                self.assertEqual(code, 2, report)
                self.assertEqual(report["status"], "UNREADABLE")
                path.unlink()
                self.write_state()
        self.forge.write_text(json.dumps({"raw": '{"data":' + nested + '}'}))
        code, report = self.run_cli()
        self.assertEqual(code, 2, report)
        self.assertEqual(report["errors"][0]["source"], "forge")
        self.assertIn("recursion", report["errors"][0]["detail"])

    def test_nonstandard_constants_at_owned_json_boundaries_are_unreadable(self):
        for constant in (float("nan"), float("inf"), float("-inf")):
            for source in ("state", "merge", "forge", "repository"):
                with self.subTest(constant=constant, source=source):
                    self.write_state()
                    self.set_pages([])
                    extra = []
                    path = None
                    if source in ("state", "merge"):
                        if source == "merge":
                            self.record()
                        path = self.state / (R.S.STATE_FILE if source == "state" else "merge-unit-test.json")
                        data = json.loads(path.read_text())
                        data["ignored"] = constant
                        path.write_text(json.dumps(data))
                    elif source == "forge":
                        data = json.loads(self.forge.read_text())[0]
                        data["ignored"] = constant
                        self.forge.write_text(json.dumps({"raw": json.dumps(data)}))
                    else:
                        self.env["GH_REPO_RAW"] = json.dumps({"url": self.remote, "ignored": constant})
                        extra = ["--repo", "example/project"]
                    try:
                        code, report = self.run_cli(*extra)
                        self.assertEqual(code, 2, report)
                        self.assertEqual(report["status"], "UNREADABLE")
                    finally:
                        self.env.pop("GH_REPO_RAW", None)
                        if source == "merge":
                            path.unlink()

    def test_json_strings_and_large_numbers_remain_readable(self):
        for raw in ('"NaN"', '"Infinity"', "1e999", "-1e999"):
            with self.subTest(raw=raw):
                self.write_state()
                path = self.state / R.S.STATE_FILE
                path.write_text(path.read_text()[:-1] + ', "ignored": ' + raw + '}')
                code, report = self.run_cli()
                self.assertEqual(code, 0, report)

    def test_special_files_in_every_state_reader_are_unreadable(self):
        for name in (R.S.STATE_FILE, R.S.OUTBOX, R.S.RECEIPTS, "merge-unit-fifo.json"):
            with self.subTest(name=name):
                path = self.state / name
                if path.exists():
                    path.unlink()
                os.mkfifo(str(path))
                code, report = self.run_cli()
                self.assertEqual(code, 2, report)
                self.assertEqual(report["status"], "UNREADABLE")
                path.unlink()
                self.write_state()

    def test_forge_failure_never_clean(self):
        for payload in ({"exit": 1}, [{"errors": ["unavailable"]}], [{"data": {"repository": None}}]):
            self.forge.write_text(json.dumps(payload))
            code, report = self.run_cli()
            self.assertEqual(code, 2, report)
            self.assertNotEqual(report["status"], "CLEAN")

    def test_since_inclusive_and_limit_sorted_by_merge_not_update(self):
        old = self.pr(6, "2025-01-01T00:00:00Z", "2026-09-26T00:00:00Z")
        self.set_pages([old], [self.pr()])
        code, report = self.run_cli("--limit", "1")
        self.assertEqual(code, 1, report)
        self.assertEqual(report["findings"][0]["pr"], 7)
        self.assertEqual(report["merged_prs_checked"], 1)
        code, report = self.run_cli("--since", "2026-09-25T12:00:00Z")
        self.assertEqual(report["merged_prs_checked"], 1)
        code, report = self.run_cli("--since", "2026-09-25T12:00:01Z")
        self.assertEqual(code, 0, report)

    def test_origin_and_equivalent_ssh_identity(self):
        self.write_state("git@github.com:example/project.git")
        self.assertEqual(self.run_cli()[0], 0)
        self.assertEqual(self.run_cli("--repo", "example/project")[0], 0)

    def test_shorthand_uses_gh_resolved_host_without_changing_source(self):
        self.remote = "https://github.example.invalid/example/project"
        self.env["GH_RESOLVED_URL"] = self.remote
        self.write_state()
        self.record()
        self.set_pages([self.pr()])
        before = {p.name: p.read_bytes() for p in self.state.iterdir()}
        code, report = self.run_cli("--repo", "example/project")
        self.assertEqual(code, 0, report)
        self.assertEqual(report["status"], "CLEAN")
        self.assertEqual(report["merged_prs_checked"], 1)
        self.assertEqual(report["repository"], "github.example.invalid/example/project")
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.state.iterdir()})
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual(calls[0], ["repo", "view", "example/project", "--json", "url"])
        self.assertEqual(calls[1][calls[1].index("--hostname") + 1], "github.example.invalid")

    def test_shorthand_resolution_failure_is_unreadable(self):
        for raw in ("garbage", "[]", "{}", '{"url": 1}', '{"url": "example/project"}',
                    "[" * 10000 + "0" + "]" * 10000):
            with self.subTest(raw=raw[:40]):
                self.env["GH_REPO_RAW"] = raw
                code, report = self.run_cli("--repo", "example/project")
                self.assertEqual(code, 2, report)
                self.assertEqual(report["status"], "UNREADABLE")
        self.env.pop("GH_REPO_RAW")
        self.env["GH_REPO_EXIT"] = "1"
        self.assertEqual(self.run_cli("--repo", "example/project")[0], 2)

    def test_unknown_anchor_and_invalid_timestamp_are_errors(self):
        self.assertEqual(self.run_cli("--since", "yesterday")[0], 2)
        self.assertEqual(self.run_cli("--since", "2026-01-01")[0], 2)
        (self.state / R.S.STATE_FILE).write_text('{"units": {}}')
        self.assertEqual(self.run_cli()[0], 2)


if __name__ == "__main__":
    unittest.main()
