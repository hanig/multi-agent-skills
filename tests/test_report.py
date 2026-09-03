"""The end-of-run report.

A report assembled from a narrative account of the run would be exactly the
self-assertion the rest of this repo refuses. These tests pin that it is built
from evidence on disk, and that it does not quietly upgrade a weak claim into
a strong one.
"""
import hashlib
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
             tickets=None, attested=True):
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
    # Receipts first, then state, so state can carry the digest the
    # coordinator records for a check it caused. Without that the receipt is
    # unattested and correctly does NOT outrank state, which is a different
    # scenario from the one most of these tests are about.
    for uid, rc in (receipts or {}).items():
        d = os.path.join(tmp, ".swarm", "runs", uid, "att1")
        os.makedirs(d, exist_ok=True)
        rc.setdefault("task_id", uid)
        path = os.path.join(d, "receipt.json")
        with open(path, "w") as fh:
            json.dump(rc, fh)
        if attested:
            with open(path, "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()
            state["units"].setdefault(uid, {})[
                "attempt_receipt_seals"] = {"att1": digest}
    with open(os.path.join(tmp, ".swarm", "state", "swarm-state.json"),
              "w") as fh:
        json.dump(state, fh)
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


class TestAcknowledgmentComesFromTheReceiptJournal(unittest.TestCase):
    """swarm.py stopped writing `applied`; this file went on reading it, so
    every run reported its tracker updates as pending forever. That is what
    happens when a field is removed and its consumers are not traced."""

    TICKETS = {"issues": [{"identifier": "ARC-1", "unit": "a", "title": "t"}]}

    def _receipts(self, t, lines):
        d = os.path.join(t, ".swarm", "state")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, R.RECEIPTS), "w") as fh:
            for line in lines:
                fh.write(line + "\n")

    def test_an_intent_with_no_receipt_is_unacknowledged(self):
        with tempfile.TemporaryDirectory() as t:
            _project(t, outbox=[{"key": "k1", "unit": "a", "verb": "close"}],
                     tickets=self.TICKETS)
            body = R.render(R.collect(t))
            self.assertIn("unacknowledged", body)
            self.assertIn("does NOT mean they were never", body)

    def test_an_attested_intent_is_not_reported_unacknowledged(self):
        with tempfile.TemporaryDirectory() as t:
            _project(t, outbox=[{"key": "k1", "unit": "a", "verb": "close"}],
                     tickets=self.TICKETS)
            self._receipts(t, [json.dumps({"key": "k1", "ref": "ARC-1",
                                           "attested": True})])
            body = R.render(R.collect(t))
            self.assertNotIn("1 tracker intent(s) are unacknowledged", body)

    def test_conflicting_refs_are_surfaced(self):
        with tempfile.TemporaryDirectory() as t:
            _project(t, outbox=[{"key": "k1", "unit": "a", "verb": "close"}],
                     tickets=self.TICKETS)
            self._receipts(t, [
                json.dumps({"key": "k1", "ref": "ARC-1", "attested": True}),
                json.dumps({"key": "k1", "ref": "ARC-9", "attested": True})])
            self.assertIn("conflicting tracker refs", R.render(R.collect(t)))

    def test_a_corrupt_journal_shows_no_status_at_all(self):
        with tempfile.TemporaryDirectory() as t:
            _project(t, outbox=[{"key": "k1", "unit": "a", "verb": "close"}],
                     tickets=self.TICKETS)
            self._receipts(t, ["NOT JSON"])
            body = R.render(R.collect(t))
            self.assertIn("cannot be read in full", body)

    def test_attested_is_never_presented_as_verified(self):
        with tempfile.TemporaryDirectory() as t:
            _project(t, outbox=[{"key": "k1", "unit": "a", "verb": "close"}],
                     tickets=self.TICKETS)
            body = R.render(R.collect(t))
            self.assertIn("the drainer's word, not proof", body)


class TestEvidenceOutranksCoordinatorState(unittest.TestCase):
    """The worst defect in the change: `su.get("state") or rc.get("state")`
    let stored state beat the receipt that judged the attempt."""

    def test_a_failed_receipt_beats_a_done_state(self):
        with tempfile.TemporaryDirectory() as t:
            _project(t, receipts={"a": {"state": "FAILED", "outputs": {}}})
            rows = R.unit_rows(R.collect(t))
            self.assertEqual(rows[0]["state"], "FAILED")
            # The receipt declares FAILED and delivers none of the declared
            # outputs, so the disagreement is now surfaced ahead of the
            # failure itself: previously it was invisible because mismatch
            # was only computed for DONE units.
            self.assertEqual(R.verdict(rows)[0], "EVIDENCE MISMATCH")

    def test_a_failed_unit_whose_receipt_agrees_reports_failed(self):
        with tempfile.TemporaryDirectory() as t:
            _project(t, units=[{"id": "a", "kind": "slurm", "outputs": [],
                                "needs": []}],
                     receipts={"a": {"state": "FAILED", "outputs": {}}})
            self.assertEqual(R.verdict(R.unit_rows(R.collect(t)))[0], "FAILED")

    def test_the_disagreement_is_shown_not_hidden(self):
        with tempfile.TemporaryDirectory() as t:
            _project(t, receipts={"a": {"state": "FAILED", "outputs": {}}})
            body = R.render(R.collect(t))
            self.assertIn("coordinator state says DONE", body)
            self.assertIn("receipt says FAILED", body)

    def test_state_is_used_when_there_is_no_receipt_yet(self):
        with tempfile.TemporaryDirectory() as t:
            _project(t)
            self.assertEqual(R.unit_rows(R.collect(t))[0]["state"], "DONE")


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



class TestPublishedReportsCannotCarryAnExploit(unittest.TestCase):
    """These reports get published as artifacts, and tickets.json is data
    this program did not write."""

    def _with_url(self, t, url):
        _project(t, tickets={"issues": [
            {"identifier": "ARC-1", "unit": "a", "title": "t", "url": url}]},
            outbox=[{"key": "k1", "unit": "a", "verb": "close"}])
        return R.render(R.collect(t))

    def test_a_javascript_url_never_becomes_a_link(self):
        with tempfile.TemporaryDirectory() as t:
            body = self._with_url(t, "javascript:alert(document.domain)")
            self.assertNotIn("href=\"javascript", body.lower())
            self.assertIn("ARC-1", body)          # still shown, as text

    def test_obfuscated_schemes_are_also_refused(self):
        for bad in ("JaVaScRiPt:alert(1)", "  javascript:alert(1)",
                    "java\tscript:alert(1)", "data:text/html,<script>x</script>",
                    "vbscript:msgbox(1)", "\njavascript:alert(1)"):
            self.assertIsNone(R._safe_url(bad), bad)

    def test_ordinary_links_still_work(self):
        for good in ("https://linear.app/x/issue/ARC-1",
                     "http://example.org/a?b=c"):
            self.assertEqual(R._safe_url(good), good)

    def test_a_receipt_without_a_ref_is_not_an_attestation(self):
        with tempfile.TemporaryDirectory() as t:
            _project(t, outbox=[{"key": "k1", "unit": "a", "verb": "close"}],
                     tickets={"issues": [{"identifier": "ARC-1", "unit": "a",
                                          "title": "t"}]})
            d = os.path.join(t, ".swarm", "state")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, R.RECEIPTS), "w") as fh:
                fh.write(json.dumps({"key": "k1", "attested": True}) + "\n")
            body = R.render(R.collect(t))
            self.assertIn("cannot be read in full", body)

    def test_corruption_suppresses_the_counts_entirely(self):
        """Saying 'N attested' and 'no status can be trusted' in one report
        is worse than saying neither."""
        with tempfile.TemporaryDirectory() as t:
            _project(t, outbox=[{"key": "k1", "unit": "a", "verb": "close"}],
                     tickets={"issues": [{"identifier": "ARC-1", "unit": "a",
                                          "title": "t"}]})
            d = os.path.join(t, ".swarm", "state")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, R.RECEIPTS), "w") as fh:
                fh.write(json.dumps({"key": "k1", "ref": "ARC-1",
                                     "attested": True}) + "\n")
                fh.write("NOT JSON\n")
            unack, attested, conflicts, fatal = R.outbox_summary(R.collect(t))
            self.assertTrue(fatal)
            self.assertEqual((unack, attested, conflicts), ([], [], []),
                             "a count derived from a journal we just said "
                             "cannot be read is not a safer number")
            body = R.render(R.collect(t))
            self.assertNotIn("are unacknowledged", body)


class TestUnknownStatesAreNotProgress(unittest.TestCase):
    """A USAGE_ERROR unit used to make a finished run read IN FLIGHT
    forever: a stall dressed as progress."""

    def _state(self, t, state):
        p = os.path.join(t, ".swarm", "state", "swarm-state.json")
        s = json.load(open(p))
        s["units"]["a"]["state"] = state
        json.dump(s, open(p, "w"))

    def test_usage_error_is_a_failure_not_a_live_unit(self):
        with tempfile.TemporaryDirectory() as t:
            _project(t)
            self._state(t, "USAGE_ERROR")
            self.assertEqual(R.verdict(R.unit_rows(R.collect(t)))[0], "FAILED")

    def test_an_unrecognised_state_is_reported_as_unknown(self):
        with tempfile.TemporaryDirectory() as t:
            _project(t)
            self._state(t, "WAT")
            v, why = R.verdict(R.unit_rows(R.collect(t)))
            self.assertEqual(v, "UNKNOWN")
            self.assertIn("WAT", why)

    def test_preempted_is_still_live(self):
        with tempfile.TemporaryDirectory() as t:
            _project(t)
            self._state(t, "PREEMPTED")
            self.assertEqual(R.verdict(R.unit_rows(R.collect(t)))[0],
                             "IN FLIGHT")

    def test_a_corrupt_journal_shows_no_number_at_all(self):
        with tempfile.TemporaryDirectory() as t:
            _project(t, outbox=[{"key": "k1", "unit": "a", "verb": "close"}])
            d = os.path.join(t, ".swarm", "state")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, R.RECEIPTS), "w") as fh:
                fh.write("NOT JSON\n")
            body = R.render(R.collect(t))
            # The attested counter specifically, not every zero on the page:
            # "0 declared outputs" is a real count and belongs there.
            i = body.index("tracker updates attested")
            card = body[max(0, i - 200):i]
            self.assertIn("unknown", card)
            self.assertNotIn(">0<", card.replace(" ", ""))

    def test_no_html_entity_is_double_escaped(self):
        """`e("&mdash;")` renders the literal text "&mdash;" on the page."""
        with tempfile.TemporaryDirectory() as t:
            _project(t)
            self.assertNotIn("&amp;mdash;", R.render(R.collect(t)))


class TestEvidenceIntegrity(unittest.TestCase):
    """A receipt that cannot be read used to be skipped, leaving the stored
    DONE state as the only thing claiming the unit finished."""

    def _break_receipt(self, t, uid="a"):
        d = os.path.join(t, ".swarm", "runs", uid, "att1")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "receipt.json"), "w") as fh:
            fh.write("{ NOT JSON")

    def test_an_unreadable_receipt_blocks_a_verdict(self):
        with tempfile.TemporaryDirectory() as t:
            _project(t)
            self._break_receipt(t)
            v, why = R.verdict(R.unit_rows(R.collect(t)))
            self.assertEqual(v, "NO VERDICT")
            self.assertIn("self-report", why)

    def test_the_unreadable_receipt_is_not_silently_dropped(self):
        with tempfile.TemporaryDirectory() as t:
            _project(t)
            self._break_receipt(t)
            ev = R.collect(t)["evidence"]
            self.assertEqual(ev["a"]["status"], "unreadable")

    def test_delivering_something_other_than_declared_is_a_mismatch(self):
        with tempfile.TemporaryDirectory() as t:
            _project(t, receipts={"a": {"state": "DONE", "outputs": {
                "wrong.dat": {"sha256": "x" * 64, "size": 1,
                              "method": "content-digest"}}}})
            v, why = R.verdict(R.unit_rows(R.collect(t)))
            self.assertEqual(v, "EVIDENCE MISMATCH")

    def test_a_missing_declared_output_is_a_mismatch(self):
        with tempfile.TemporaryDirectory() as t:
            _project(t, receipts={"a": {"state": "DONE", "outputs": {}}})
            self.assertEqual(R.verdict(R.unit_rows(R.collect(t)))[0],
                             "EVIDENCE MISMATCH")

    def test_delivering_exactly_what_was_declared_is_complete(self):
        with tempfile.TemporaryDirectory() as t:
            _project(t, receipts={"a": {"state": "DONE", "outputs": {
                "out.txt": {"sha256": "y" * 64, "size": 2,
                            "method": "content-digest"}}}})
            self.assertEqual(R.verdict(R.unit_rows(R.collect(t)))[0],
                             "COMPLETE")

    def test_no_receipt_is_not_a_mismatch_but_is_not_complete_either(self):
        """"Has not reported yet" is not "delivered the wrong thing". It is
        also not "finished": a stored DONE with no receipt means nothing here
        judged the artifacts."""
        with tempfile.TemporaryDirectory() as t:
            _project(t)
            rows = R.unit_rows(R.collect(t))
            self.assertEqual(rows[0]["missing_outputs"], [])
            v, why = R.verdict(rows)
            self.assertEqual(v, "NO VERDICT")
            self.assertIn("no receipt", why)


class TestTheCommitteePlan(unittest.TestCase):
    """The four findings the step-back committee scoped as ONE invariant:
    COMPLETE must require evidence, and every way of not having evidence must
    prevent it."""

    def test_a_stored_done_with_no_receipt_is_not_complete(self):
        with tempfile.TemporaryDirectory() as t:
            _project(t)
            v, why = R.verdict(R.unit_rows(R.collect(t)))
            self.assertEqual(v, "NO VERDICT")
            self.assertIn("self-report", why)

    def test_an_untraversable_attempt_directory_is_not_silently_lost(self):
        """glob returned nothing for both 'absent' and 'cannot look'."""
        import stat
        with tempfile.TemporaryDirectory() as t:
            _project(t)
            d = os.path.join(t, ".swarm", "runs", "a", "att1")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "receipt.json"), "w") as fh:
                json.dump({"state": "DONE", "outputs": {}}, fh)
            os.chmod(os.path.join(t, ".swarm", "runs", "a"), 0)
            try:
                ev = R.collect(t)["evidence"]
                self.assertEqual(ev["a"]["status"], "unavailable")
                self.assertEqual(R.verdict(R.unit_rows(R.collect(t)))[0],
                                 "NO VERDICT")
            finally:
                os.chmod(os.path.join(t, ".swarm", "runs", "a"),
                         stat.S_IRWXU)

    def test_extra_outputs_count_even_when_none_were_declared(self):
        with tempfile.TemporaryDirectory() as t:
            _project(t, units=[{"id": "a", "kind": "slurm", "outputs": [],
                                "needs": []}],
                     receipts={"a": {"state": "DONE", "outputs": {
                         "surprise.txt": {"sha256": "c" * 64, "size": 1,
                                          "method": "content-digest"}}}})
            rows = R.unit_rows(R.collect(t))
            self.assertEqual(rows[0]["undeclared_outputs"], ["surprise.txt"])
            self.assertEqual(R.verdict(rows)[0], "EVIDENCE MISMATCH")

    def test_a_mismatch_on_a_failed_unit_is_visible(self):
        with tempfile.TemporaryDirectory() as t:
            _project(t, receipts={"a": {"state": "FAILED", "outputs": {
                "unexpected.dat": {"sha256": "d" * 64, "size": 1,
                                   "method": "content-digest"}}}})
            rows = R.unit_rows(R.collect(t))
            self.assertEqual(R.verdict(rows)[0], "EVIDENCE MISMATCH")
            self.assertEqual(rows[0]["state"], "FAILED",
                             "the stored failure must not be rewritten")

    def test_a_matching_receipt_still_reaches_complete(self):
        with tempfile.TemporaryDirectory() as t:
            _project(t, receipts={"a": {"state": "DONE", "outputs": {
                "out.txt": {"sha256": "e" * 64, "size": 3,
                            "method": "content-digest"}}}})
            self.assertEqual(R.verdict(R.unit_rows(R.collect(t)))[0],
                             "COMPLETE")

def _row(state, **kw):
    r = {"id": kw.get("id", "u"), "state": state, "has_receipt": True,
         "missing_outputs": [], "undeclared_outputs": [],
         "evidence": {"status": "ok"}, "preflight_refusals": [],
         "receipt_attestation": R.ATTESTED}
    r.update(kw)
    return r


class TestCoordinatorStatesAreAllClassified(unittest.TestCase):
    """Every state `advance` can store must land in exactly one bucket.

    An unclassified state makes the whole run report UNKNOWN, which reads as
    a confused report rather than as the thing that actually happened.
    """

    def test_failed_evidence_is_a_failure_not_an_unknown(self):
        got, why = R.verdict([_row("FAILED_EVIDENCE")])
        self.assertEqual(got, "FAILED", why)

    def test_preflight_refused_is_live_because_it_redispatches(self):
        # advance does not skip PREFLIGHT_REFUSED, so the unit is retried once
        # the workspace is clean. Calling it FAILED would be wrong.
        got, why = R.verdict([_row("PREFLIGHT_REFUSED")])
        self.assertEqual(got, "IN FLIGHT", why)

    def test_no_coordinator_state_is_left_unclassified(self):
        swarm = (ROOT / "skills" / "hanig-swarm" / "scripts" /
                 "swarm.py").read_text()
        import re
        stored = set(re.findall(r'us\["state"\] = "([A-Z_]+)"', swarm))
        known = set(R.TERMINAL_OK) | set(R.TERMINAL_BAD) | set(R.LIVE)
        missing = sorted(stored - known)
        self.assertEqual(missing, [], "swarm.py stores %s, which the report "
                         "does not classify" % missing)


class TestBlockedBeforeLaunchIsVisible(unittest.TestCase):

    def test_a_refused_unit_says_which_workspace_is_dirty(self):
        with tempfile.TemporaryDirectory() as t:
            _project(t)
            sp = os.path.join(t, ".swarm", "state", "swarm-state.json")
            st = json.load(open(sp))
            st["units"]["a"].update({
                "state": "PREFLIGHT_REFUSED", "attempt_dir": None,
                "preflight_refusals": [{"workspace": "/checkout",
                                        "dirty_path_count": 2,
                                        "receipt": None}]})
            json.dump(st, open(sp, "w"))
            data = R.collect(t)
            html = R.render(data)
            self.assertIn("Blocked before launch", html)
            self.assertIn("/checkout", html)
            self.assertIn("2 dirty path(s)", html)


class TestFilesNothingDeclaredAreVisible(unittest.TestCase):
    """18 bytes of debris named `phase0b/--reflink=auto` rode a DONE unit out
    of a real run because nothing looked. The receipt names it now, and a
    receipt nobody reads is the same silence one file further along."""

    DEBRIS = "phase0b/--reflink=auto"

    def _done(self, stray):
        return {"a": {"state": "DONE", "outputs": {
            "out.txt": {"sha256": "f" * 64, "size": 1,
                        "method": "content-digest"}},
            "basis": {"stray_untracked": stray}}}

    def test_the_stray_path_is_rendered(self):
        with tempfile.TemporaryDirectory() as t:
            _project(t, receipts=self._done(
                {"workspace": "/checkout", "paths": [self.DEBRIS],
                 "count": 1}))
            html = R.render(R.collect(t))
            self.assertIn("Files nothing declared", html)
            self.assertIn("/checkout", html)
            self.assertIn("--reflink=auto", html)

    def test_a_truncated_list_says_how_many_it_did_not_show(self):
        with tempfile.TemporaryDirectory() as t:
            _project(t, receipts=self._done(
                {"workspace": "/checkout", "paths": ["junk/f000"],
                 "count": 51}))
            html = R.render(R.collect(t))
            self.assertIn("51 untracked path(s)", html)
            self.assertIn("and 50 more", html)

    def test_debris_does_not_move_the_verdict(self):
        """Audit-only, and the report is where that is easiest to break:
        authority lives in coordinator state, so a stray file must not turn a
        genuinely complete run into a failure."""
        with tempfile.TemporaryDirectory() as t:
            _project(t, receipts=self._done(
                {"workspace": "/checkout", "paths": [self.DEBRIS],
                 "count": 1}))
            rows = R.unit_rows(R.collect(t))
            self.assertEqual(rows[0]["stray_untracked"]["count"], 1)
            self.assertEqual(R.verdict(rows)[0], "COMPLETE")

    def test_a_clean_workspace_writes_no_section(self):
        with tempfile.TemporaryDirectory() as t:
            _project(t, receipts=self._done(
                {"workspace": "/checkout", "paths": [], "count": 0}))
            self.assertNotIn("Files nothing declared", R.render(R.collect(t)))

    def test_a_receipt_that_did_not_look_writes_no_section(self):
        """`null` is "we did not look", which is not "we looked and it was
        clean". Neither gets a section, but reading one as the other would."""
        with tempfile.TemporaryDirectory() as t:
            _project(t, receipts=self._done(None))
            rows = R.unit_rows(R.collect(t))
            self.assertIsNone(rows[0]["stray_untracked"])
            self.assertNotIn("Files nothing declared", R.render(R.collect(t)))

    def test_a_receipt_predating_the_field_still_renders(self):
        with tempfile.TemporaryDirectory() as t:
            _project(t, receipts={"a": {"state": "DONE", "outputs": {
                "out.txt": {"sha256": "f" * 64, "size": 1,
                            "method": "content-digest"}}}})
            rows = R.unit_rows(R.collect(t))
            self.assertIsNone(rows[0]["stray_untracked"])
            self.assertEqual(R.verdict(rows)[0], "COMPLETE")


class TestAnUnattributedReceiptDoesNotOutrankState(unittest.TestCase):
    """The other half of "the receipt wins".

    An agent can write receipt.json into its own attempt directory. Letting
    any receipt outrank coordinator state therefore handed the party being
    judged priority over the party judging. A receipt counts when the
    coordinator recorded causing the check that wrote it.
    """

    def test_an_attested_receipt_still_wins(self):
        with tempfile.TemporaryDirectory() as t:
            _project(t, receipts={"a": {"state": "FAILED"}}, attested=True)
            rows = R.unit_rows(R.collect(t))
            self.assertEqual(rows[0]["state"], "FAILED")
            self.assertTrue(rows[0]["receipt_attested"])

    def test_an_unattested_receipt_does_not_win(self):
        with tempfile.TemporaryDirectory() as t:
            # Coordinator state says DONE; an unattributed receipt claims
            # otherwise. Previously the receipt decided.
            _project(t, receipts={"a": {"state": "FAILED"}}, attested=False)
            rows = R.unit_rows(R.collect(t))
            self.assertEqual(rows[0]["state"], "DONE")
            self.assertFalse(rows[0]["receipt_attested"])

    def test_a_forged_done_cannot_promote_a_failed_unit(self):
        # The direction that matters: the agent claims success.
        with tempfile.TemporaryDirectory() as t:
            _project(t, receipts={"a": {"state": "DONE"}}, attested=False)
            sp = os.path.join(t, ".swarm", "state", "swarm-state.json")
            st = json.load(open(sp))
            st["units"]["a"]["state"] = "FAILED"
            json.dump(st, open(sp, "w"))
            data = R.collect(t)
            rows = R.unit_rows(data)
            self.assertEqual(rows[0]["state"], "FAILED")
            html = R.render(data)
            self.assertIn("Receipts the coordinator cannot vouch for", html)

    def test_an_unattributable_receipt_yields_no_verdict(self):
        # Not FAILED and not COMPLETE: the report genuinely does not know
        # whether the artifacts were judged. Saying so is the honest answer,
        # and it is what this document exists for.
        with tempfile.TemporaryDirectory() as t:
            _project(t, receipts={"a": {"state": "DONE"}}, attested=False)
            got, why = R.verdict(R.unit_rows(R.collect(t)))
            self.assertEqual(got, "NO VERDICT", why)
            self.assertIn("attributes", why)

    def test_an_attested_run_still_reaches_a_real_verdict(self):
        # The other direction: attestation must not make every run NO VERDICT.
        with tempfile.TemporaryDirectory() as t:
            _project(t, receipts={"a": {"state": "DONE", "outputs": {
                "out.txt": {"sha256": "d" * 64, "bytes": 3}}}},
                     attested=True)
            got, why = R.verdict(R.unit_rows(R.collect(t)))
            self.assertEqual(got, "COMPLETE", why)

    def test_a_contradicted_receipt_says_it_was_replaced(self):
        with tempfile.TemporaryDirectory() as t:
            _project(t, receipts={"a": {"state": "FAILED"}}, attested=True)
            path = os.path.join(t, ".swarm", "runs", "a", "att1",
                                "receipt.json")
            rc = json.load(open(path))
            rc["state"] = "DONE"
            json.dump(rc, open(path, "w"))
            data = R.collect(t)
            rows = R.unit_rows(data)
            self.assertEqual(rows[0]["receipt_attestation"], R.CONTRADICTED)
            html = R.render(data)
            self.assertIn("replaced", html)
            # A contradicted receipt is not an unknown: the coordinator did
            # check, and its state stands, so the run still gets a verdict.
            got, _why = R.verdict(rows)
            self.assertNotEqual(got, "NO VERDICT")

    def test_a_receipt_edited_after_the_check_is_not_attested(self):
        with tempfile.TemporaryDirectory() as t:
            _project(t, receipts={"a": {"state": "FAILED"}}, attested=True)
            path = os.path.join(t, ".swarm", "runs", "a", "att1",
                                "receipt.json")
            rc = json.load(open(path))
            rc["state"] = "DONE"
            json.dump(rc, open(path, "w"))
            rows = R.unit_rows(R.collect(t))
            self.assertFalse(rows[0]["receipt_attested"])
            self.assertEqual(rows[0]["state"], "DONE",
                             "coordinator state is what stands here")


if __name__ == "__main__":
    unittest.main()
