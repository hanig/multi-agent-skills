"""ARC-690: exercise offline tracker/plan reconciliation through its CLI."""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "hanig-project" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import tickets as T


class TestTicketsReconcile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.plan = self.root / "plan.json"
        self.readback = self.root / "issues.json"
        self.plan.write_text(json.dumps({"name": "p", "units": [
            {"id": "A", "outputs": ["a.txt"]},
            {"id": "B", "outputs": ["b.txt"]}]}))

    def issue(self, unit, state="Todo", identifier=None):
        return {"identifier": identifier or "ARC-" + unit,
                "title": "Work for " + str(unit), "state": state, "unit": unit}

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "tickets.py"), "reconcile",
             str(self.plan), *args], capture_output=True, text=True,
            cwd=self.root, timeout=15)

    def read(self, issues, as_json=True):
        self.readback.write_text(json.dumps(issues))
        args = ["--tracker-issues", str(self.readback)]
        if as_json:
            args.append("--json")
        result = self.run_cli(*args)
        self.assertEqual(result.stderr, "", result.stderr)
        return result

    def test_a_c_readback_names_c_and_missing_b_and_exits_nonzero(self):
        result = self.read([self.issue("A"), self.issue("C")])
        report = json.loads(result.stdout)
        self.assertEqual(report["unplanned_issues"], [self.issue("C")])
        self.assertEqual(report["units_without_issues"], ["B"])
        self.assertEqual(result.returncode, 3, result.stdout)
        self.assertIs(report["in_sync"], False)
        self.assertEqual(report["evidence"], "ATTESTED")
        self.assertIn("not verified", report["basis"])

    def test_no_readback_is_unknown_not_zero_orphans(self):
        result = self.run_cli("--json")
        report = json.loads(result.stdout)
        self.assertEqual(result.returncode, 4, result.stdout)
        self.assertEqual(report["tracker_state"], "unknown")
        self.assertIsNone(report["unplanned_issues"])
        self.assertIsNone(report["units_without_issues"])
        self.assertIsNone(report["in_sync"])
        human = self.run_cli()
        self.assertEqual(human.returncode, 4)
        self.assertIn("Tracker side UNKNOWN", human.stdout)
        self.assertNotIn("no plan unit: 0", human.stdout)
        self.assertNotIn("read-back: 0", human.stdout)

    def test_each_orphan_direction_independently_gates(self):
        for issues, unplanned, missing in (
                ([self.issue("A"), self.issue("B"), self.issue("C")], ["C"], []),
                ([self.issue("A")], [], ["B"])):
            with self.subTest(issues=issues):
                result = self.read(issues)
                report = json.loads(result.stdout)
                self.assertEqual(result.returncode, 3)
                self.assertEqual([r["unit"] for r in report["unplanned_issues"]],
                                 unplanned)
                self.assertEqual(report["units_without_issues"], missing)

    def test_plain_text_names_both_sides_and_labels_the_attestation(self):
        result = self.read([self.issue("A"), self.issue("C")], as_json=False)
        self.assertEqual(result.returncode, 3)
        self.assertIn("ARC-C: Work for C", result.stdout)
        self.assertIn("\n  B\n", result.stdout)
        self.assertIn("ATTESTED", result.stdout)
        self.assertIn("not verified", result.stdout)
        self.assertIn("freshness are not checked", result.stdout)

    def test_agreement_is_success_with_existing_draft_unit_mapping(self):
        draft = T.draft(json.loads(self.plan.read_text()))
        issues = [dict(issue, identifier="ARC-" + issue["unit"], state="Todo")
                  for issue in draft["issues"]]
        result = self.read(issues)
        report = json.loads(result.stdout)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(report["unplanned_issues"], [])
        self.assertEqual(report["units_without_issues"], [])
        self.assertIs(report["in_sync"], True)

    def test_empty_readback_is_known_and_names_all_missing_units(self):
        result = self.read([])
        report = json.loads(result.stdout)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(report["tracker_state"], "read")
        self.assertEqual(report["unplanned_issues"], [])
        self.assertEqual(report["units_without_issues"], ["A", "B"])
        self.plan.write_text(json.dumps({"name": "p", "units": []}))
        self.assertEqual(self.read([]).returncode, 0)

    def test_terminal_states_are_not_orphans_and_still_cover_a_unit(self):
        states = ("Done", " completed ", "CANCELLED", "canceled",
                  {"name": "Shipped", "type": "completed"},
                  {"name": "Won't do", "type": "canceled"}, {"name": "Done"})
        for state in states:
            with self.subTest(state=state):
                result = self.read([self.issue("A", state), self.issue("B"),
                                    self.issue("C", state)])
                report = json.loads(result.stdout)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertEqual(report["unplanned_issues"], [])
                self.assertEqual(report["units_without_issues"], [])

    def test_nonterminal_and_unrecognized_states_remain_open(self):
        for state in ("Backlog", "Todo", "In Progress", "In Review", "Blocked",
                      "Custom waiting state", {"name": "Done", "type": "started"}):
            with self.subTest(state=state):
                result = self.read([self.issue("A"), self.issue("B"),
                                    self.issue("C", state)])
                self.assertEqual(result.returncode, 3, result.stdout)
                self.assertEqual(json.loads(result.stdout)["unplanned_issues"],
                                 [self.issue("C", state)])

    def test_unmapped_manual_issues_are_named_without_guessing_from_title(self):
        for mapped_field in ({}, {"unit": None}):
            with self.subTest(mapped_field=mapped_field):
                manual = dict(identifier="ARC-123", title="A: looks like a unit",
                              state="Todo", **mapped_field)
                result = self.read([manual, self.issue("B")])
                report = json.loads(result.stdout)
                self.assertEqual(result.returncode, 3)
                self.assertEqual(report["unplanned_issues"][0]["identifier"], "ARC-123")
                self.assertEqual(report["units_without_issues"], ["A"])

    def assert_unknown_error(self, result):
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        report = json.loads(result.stdout)
        self.assertEqual(report["tracker_state"], "unknown")
        self.assertIsNone(report["unplanned_issues"])
        self.assertIsNone(report["units_without_issues"])
        self.assertIsNone(report["in_sync"])
        self.assertTrue(report["reason"])

    def test_bad_readback_cannot_become_empty_or_a_partial_comparison(self):
        for issues in (None, {}, "", [None], [self.issue("A"), {}],
                       [self.issue("A"), self.issue("A")]):
            with self.subTest(issues=issues):
                self.assert_unknown_error(self.read(issues))
        for field, values in (("identifier", [None, "", 2]),
                              ("title", [None, " ", []]),
                              ("unit", ["", 3, []]),
                              ("state", [None, "", {}, {"type": None}, 3])):
            for value in values:
                with self.subTest(field=field, value=value):
                    row = dict(self.issue("A"), **{field: value})
                    self.assert_unknown_error(self.read([row]))

    def test_missing_and_unreadable_files_fail_closed_in_json(self):
        for raw in (None, "{ broken", "null", "{}"):
            with self.subTest(raw=raw):
                if raw is not None:
                    self.readback.write_text(raw)
                result = self.run_cli("--tracker-issues", str(self.readback), "--json")
                self.assert_unknown_error(result)
                self.assertIn(str(self.readback), json.loads(result.stdout)["reason"])
        self.plan.unlink()
        self.assert_unknown_error(self.run_cli("--json"))

    def test_invalid_plan_shapes_do_not_erase_units(self):
        for plan in (None, [], {}, {"units": "AB"}, {"units": None},
                     {"units": [None]}, {"units": [{"id": []}]},
                     {"units": [{"id": "A"}, {"id": "A"}]}):
            with self.subTest(plan=plan):
                self.plan.write_text(json.dumps(plan))
                self.assert_unknown_error(self.read([]))

    def test_deeply_nested_json_is_invalid_for_plan_and_readback(self):
        for target in (self.plan, self.readback):
            with self.subTest(target=target.name):
                self.plan.write_text('{"units": []}')
                self.readback.write_text('[]')
                target.write_text('[' * 2000 + '0' + ']' * 2000)
                result = self.run_cli("--tracker-issues", str(self.readback), "--json")
                self.assert_unknown_error(result)
                self.assertIn(str(target), json.loads(result.stdout)["reason"])

    def test_other_decoder_exceptions_report_invalid_input_even_without_message(self):
        self.readback.write_text('[]')
        args = SimpleNamespace(plan=str(self.plan),
                               tracker_issues=str(self.readback), json=True)
        for error in (MemoryError(), RuntimeError("decoder failed")):
            for target in (self.plan, self.readback):
                with self.subTest(error=type(error).__name__, target=target.name):
                    effects = [error] if target == self.plan else [{"units": []}, error]
                    output = io.StringIO()
                    with mock.patch.object(T.json, "loads", side_effect=effects):
                        with contextlib.redirect_stdout(output):
                            code = T.cmd_reconcile(args)
                    self.assert_unknown_error(subprocess.CompletedProcess(
                        [], code, output.getvalue(), ""))

    def test_deep_report_fields_fail_closed_without_partial_output_or_writes(self):
        deep = '[' * 2000 + '0' + ']' * 2000
        for target in (self.plan, self.readback):
            with self.subTest(target=target.name):
                self.plan.write_text('{"name": "p", "units": []}')
                self.readback.write_text('[]')
                if target == self.plan:
                    target.write_text('{"name":' + deep + ',"units":[]}')
                else:
                    target.write_text(
                        '[{"identifier":"ARC-C","title":"C","unit":null,'
                        '"state":{"type":"Todo","metadata":' + deep + '}}]')
                before = {p.name: p.read_bytes() for p in self.root.iterdir()}
                result = self.run_cli("--tracker-issues", str(self.readback), "--json")
                self.assert_unknown_error(result)
                self.assertIn(str(target), json.loads(result.stdout)["reason"])
                self.assertEqual({p.name: p.read_bytes() for p in self.root.iterdir()},
                                 before)

    def assert_legacy_readers_refuse_without_writes(self, raw):
        bad = self.root / "invalid.json"
        bad.write_text(raw)
        for args, message in (
                (["draft", str(bad)], "no readable plan"),
                (["draft", str(self.plan), "--brief", str(bad)], "no readable brief"),
                (["draft", str(self.plan), "--out", str(bad)], "cannot be read"),
                (["draft", str(self.plan), "--tracker-edges", str(bad)],
                 "could not be read"),
                (["check", str(bad), str(self.plan)], "no readable plan"),
                (["check", str(self.plan), str(bad)], "no readable draft"),
                (["approve", str(bad), "--approver", "test"], "no readable draft")):
            with self.subTest(args=args):
                before = {p.name: p.read_bytes() for p in self.root.iterdir()}
                result = subprocess.run(
                    [sys.executable, str(SCRIPTS / "tickets.py"), *args],
                    capture_output=True, text=True, cwd=self.root, timeout=15)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(message, result.stdout + result.stderr)
                self.assertIn(str(bad), result.stdout + result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual({p.name: p.read_bytes() for p in self.root.iterdir()},
                                 before)

    def test_legacy_readers_handle_nested_json_without_overwriting_inputs(self):
        self.assert_legacy_readers_refuse_without_writes('[' * 2000 + '0' + ']' * 2000)

    def test_legacy_readers_reject_decoded_nonobjects_without_overwriting_inputs(self):
        for value in ([], [0], None, False, True, 0, "", "text"):
            with self.subTest(value=value):
                self.assert_legacy_readers_refuse_without_writes(json.dumps(value))

    def test_legacy_readers_reject_invalid_json_without_overwriting_inputs(self):
        self.assert_legacy_readers_refuse_without_writes('{ broken')

    def test_reconcile_names_invalid_input_files(self):
        for target in (self.plan, self.readback):
            for raw in ('null', 'false', '"text"', '0', '{ broken'):
                with self.subTest(target=target.name, raw=raw):
                    self.plan.write_text('{"units": []}')
                    self.readback.write_text('[]')
                    target.write_text(raw)
                    result = self.run_cli("--tracker-issues", str(self.readback), "--json")
                    self.assert_unknown_error(result)
                    self.assertIn(str(target), json.loads(result.stdout)["reason"])

    def test_valid_legacy_objects_keep_ids_and_approval_across_redrafts(self):
        local = self.root / "tickets.json"
        brief = self.root / "brief.json"
        edges = self.root / "edges.json"
        brief.write_text('{"name": "Project title"}')
        edges.write_text(json.dumps({"read_at": "2026-09-24T00:00:00Z",
                                     "edges": {"A": [], "B": []}}))

        def run(*args):
            result = subprocess.run(
                [sys.executable, str(SCRIPTS / "tickets.py"), *args],
                capture_output=True, text=True, cwd=self.root, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        run("draft", str(self.plan), "--brief", str(brief))
        draft = json.loads(local.read_text())
        self.assertEqual(draft["project"]["name"], "Project title")
        draft["project"]["linear_id"] = "project-id"
        for issue in draft["issues"]:
            issue["linear_id"] = "issue-" + issue["unit"]
            issue["identifier"] = "ARC-" + issue["unit"]
        local.write_text(json.dumps(draft))
        run("approve", str(local), "--approver", "test")
        approved = json.loads(local.read_text())
        for _ in range(2):
            run("draft", str(self.plan), "--brief", str(brief),
                "--tracker-edges", str(edges))
            run("check", str(self.plan), str(local))
            current = json.loads(local.read_text())
            self.assertEqual(current["project"], approved["project"])
            self.assertEqual(current["approval"], approved["approval"])
            self.assertEqual([i["linear_id"] for i in current["issues"]],
                             [i["linear_id"] for i in approved["issues"]])
            self.assertEqual([i["identifier"] for i in current["issues"]],
                             [i["identifier"] for i in approved["issues"]])

    def test_rerun_reads_changed_files_and_never_reuses_a_persisted_verdict(self):
        self.assertEqual(self.read([self.issue("A"), self.issue("B")]).returncode, 0)
        self.assertEqual(self.read([self.issue("A"), self.issue("C")]).returncode, 3)
        self.plan.write_text(json.dumps({"name": "p", "units": [{"id": "A"}, {"id": "C"}]}))
        result = self.run_cli("--tracker-issues", str(self.readback), "--json")
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_reconciliation_does_not_write_or_trust_the_local_draft(self):
        local = self.root / "tickets.json"
        local.write_text(json.dumps(T.draft(json.loads(self.plan.read_text()))))
        self.readback.write_text(json.dumps([self.issue("A"), self.issue("C")]))
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        self.assertEqual(self.run_cli("--tracker-issues", str(self.readback)).returncode, 3)
        self.assertEqual(self.run_cli("--json").returncode, 4)
        self.assertEqual({p.name: p.read_bytes() for p in self.root.iterdir()}, before)

    def test_help_documents_exit_codes_and_state_policy(self):
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0)
        text = " ".join(result.stdout.split())
        for phrase in ("Exit 0:", "2: invalid input", "3: either orphan set",
                       "4: no read-back", "done/completed/cancelled/canceled",
                       "draft issues[].unit", "ATTESTED"):
            self.assertIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
