"""The end-of-run report.

A report assembled from a narrative account of the run would be exactly the
self-assertion the rest of this repo refuses. These tests pin that it is built
from evidence on disk, and that it does not quietly upgrade a weak claim into
a strong one.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "hanig-project" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import report as R  # noqa: E402


def _project(tmp, units=None, receipts=None, findings=None, outbox=None,
             tickets=None):
    units = units if units is not None else [
        {"id": "a", "kind": "slurm", "outputs": ["out.txt"], "needs": []}]
    os.makedirs(os.path.join(tmp, ".swarm", "state"), exist_ok=True)
    with open(os.path.join(tmp, "plan.json"), "w") as fh:
        json.dump({"project": "p", "units": units}, fh)
    state = {"plan_digest": "abc123", "units": {}}
    for u in units:
        state["units"][u["id"]] = {
            "state": "DONE", "job_id": "1", "attempts": ["d"],
            "attempt_dir": "d"}
    with open(os.path.join(tmp, ".swarm", "state", "swarm-state.json"),
              "w") as fh:
        json.dump(state, fh)
    for uid, rc in (receipts or {}).items():
        d = os.path.join(tmp, ".swarm", "runs", uid, "att1")
        os.makedirs(d, exist_ok=True)
        rc.setdefault("task_id", uid)
        with open(os.path.join(d, "receipt.json"), "w") as fh:
            json.dump(rc, fh)
    if findings is not None:
        with open(os.path.join(tmp, "findings.json"), "w") as fh:
            json.dump(findings, fh)
    if outbox is not None:
        with open(os.path.join(tmp, ".swarm", "state", "outbox.jsonl"),
                  "w") as fh:
            for i in outbox:
                fh.write(json.dumps(i) + "\n")
    if tickets is not None:
        with open(os.path.join(tmp, "tickets.json"), "w") as fh:
            json.dump(tickets, fh)
    return tmp


class TestBuiltFromEvidence(unittest.TestCase):

    def test_digests_come_from_the_receipt(self):
        with tempfile.TemporaryDirectory() as t:
            _project(t, receipts={"a": {"outputs": {"out.txt": {
                "sha256": "deadbeef" * 8, "size": 12, "method":
                "content-digest"}}, "state": "DONE"}})
            body = R.render(R.collect(t))
            self.assertIn("deadbeef", body)

    def test_a_missing_receipt_is_an_absence_not_a_crash(self):
        with tempfile.TemporaryDirectory() as t:
            _project(t)                       # no receipts at all
            body = R.render(R.collect(t))
            self.assertIn("No outputs have been digested yet", body)

    def test_verdict_is_failed_when_any_unit_failed(self):
        with tempfile.TemporaryDirectory() as t:
            _project(t)
            p = os.path.join(t, ".swarm", "state", "swarm-state.json")
            s = json.load(open(p))
            s["units"]["a"]["state"] = "FAILED"
            json.dump(s, open(p, "w"))
            self.assertEqual(R.verdict(R.unit_rows(R.collect(t)))[0], "FAILED")

    def test_unclosed_units_are_not_reported_as_complete(self):
        with tempfile.TemporaryDirectory() as t:
            _project(t, units=[
                {"id": "a", "kind": "slurm", "outputs": ["o"]},
                {"id": "b", "kind": "slurm", "outputs": ["o"]}])
            p = os.path.join(t, ".swarm", "state", "swarm-state.json")
            s = json.load(open(p))
            s["units"]["b"]["state"] = "RUNNING"
            json.dump(s, open(p, "w"))
            v, why = R.verdict(R.unit_rows(R.collect(t)))
            self.assertEqual(v, "IN FLIGHT")
            self.assertIn("1 of 2", why)


class TestWeakEvidenceIsNotUpgraded(unittest.TestCase):
    """The receipt's `basis` block records what it does NOT prove. A report
    that drops it turns a hedged claim into an unhedged one."""

    BASIS = {"os_enforced_isolation": False,
             "attribution_by_observation": False,
             "note": "not isolated from other processes as the same user"}

    def test_basis_admissions_are_rendered(self):
        with tempfile.TemporaryDirectory() as t:
            _project(t, receipts={"a": {"state": "DONE", "basis": self.BASIS,
                                        "outputs": {}}})
            body = R.render(R.collect(t))
            self.assertIn("does not establish", body)
            self.assertIn("isolation is by convention", body)
            self.assertIn("not isolated from other processes", body)

    def test_gaps_are_read_from_basis_not_hardcoded(self):
        # A receipt claiming enforced isolation must produce NO gap line.
        with tempfile.TemporaryDirectory() as t:
            _project(t, receipts={"a": {"state": "DONE", "outputs": {},
                                        "basis": {
                                            "os_enforced_isolation": True,
                                            "attribution_by_observation":
                                                True}}})
            self.assertEqual(R.evidence_gaps(R.unit_rows(R.collect(t))), [])


class TestFindingsAreLabelledAsClaims(unittest.TestCase):

    def test_project_findings_are_marked_as_the_projects_own(self):
        with tempfile.TemporaryDirectory() as t:
            _project(t, findings={"findings": [
                {"title": "T1", "detail": "D1"}]})
            body = R.render(R.collect(t))
            self.assertIn("T1", body)
            self.assertIn("Findings reported by this project", body)
            self.assertIn("does not verify them", body)

    def test_no_findings_section_when_the_project_supplied_none(self):
        with tempfile.TemporaryDirectory() as t:
            _project(t)
            self.assertNotIn("Findings reported by this project",
                             R.render(R.collect(t)))


class TestPendingIntentsAreSurfaced(unittest.TestCase):

    def test_a_pending_outbox_is_called_out(self):
        with tempfile.TemporaryDirectory() as t:
            _project(t, outbox=[{"applied": False, "unit": "a",
                                 "verb": "close"}],
                     tickets={"issues": [{"identifier": "ARC-1", "unit": "a",
                                          "title": "t"}]})
            body = R.render(R.collect(t))
            self.assertIn("still read as pending", body)

    def test_applied_intents_do_not_raise_the_warning(self):
        with tempfile.TemporaryDirectory() as t:
            _project(t, outbox=[{"applied": True, "unit": "a",
                                 "verb": "close"}],
                     tickets={"issues": [{"identifier": "ARC-1", "unit": "a",
                                          "title": "t"}]})
            self.assertNotIn("still read as pending", R.render(R.collect(t)))


class TestOutputIsSafeAndSelfContained(unittest.TestCase):

    def test_project_supplied_text_is_escaped(self):
        with tempfile.TemporaryDirectory() as t:
            _project(t, findings={"findings": [
                {"title": "<script>alert(1)</script>", "detail": "x"}]})
            body = R.render(R.collect(t))
            self.assertNotIn("<script>alert(1)</script>", body)
            self.assertIn("&lt;script&gt;", body)

    def test_fragment_has_no_document_scaffolding(self):
        # The artifact host supplies doctype/head/body; emitting our own
        # would nest a document inside a document.
        with tempfile.TemporaryDirectory() as t:
            _project(t)
            frag = R.wrap_fragment(R.render(R.collect(t)), "T")
            for tag in ("<!doctype", "<html", "<head>", "<body"):
                self.assertNotIn(tag, frag.lower())

    def test_standalone_is_a_whole_document(self):
        with tempfile.TemporaryDirectory() as t:
            _project(t)
            doc = R.wrap_standalone(R.render(R.collect(t)), "T")
            self.assertTrue(doc.lower().startswith("<!doctype html>"))

    def test_every_colour_token_is_defined_on_bare_root(self):
        """A token defined only inside a media query renders one theme's text
        on the other theme's ground."""
        import re
        css = R.CSS
        bare = css[css.index(":root{"):css.index("@media")]
        used = set(re.findall(r"var\((--[a-z0-9-]+)\)", css))
        declared = set(re.findall(r"(--[a-z0-9-]+)\s*:", bare))
        self.assertEqual(used - declared, set(),
                         "tokens used but not defined on bare :root")


if __name__ == "__main__":
    unittest.main()
