"""Two dispatch-time refusals, from two field reports.

ARC-230 (plan item B6). Three ad-hoc sub-plans of one project, three state
directories, one run root. One unit was dispatched twice, from two of them,
into identical output paths. Every coordinator was right about everything it
could see: the lease excludes two CONTROLLERS over one state directory and
says nothing about two state directories over one output namespace.

ARC-243. Three agents in three worktrees of one repository each ran
`git stash -u` inside one window. The stash stack is a single ref in the
shared common Git directory, so each `pop` took another's entry.

Both are checked HERE rather than in test_swarm.py because both are refusals
taken before anything is created, and both need a fake scheduler on PATH.
"""
import ast
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "hanig-swarm" / "scripts"
SWARM = SCRIPTS / "swarm.py"
sys.path.insert(0, str(SCRIPTS))
import swarm as S  # noqa: E402

ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
           GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          env=ENV, capture_output=True, text=True).stdout


def _script(directory, name, body):
    f = Path(directory) / name
    f.write_text("#!/bin/sh\n" + body)
    f.chmod(0o755)
    return f


@contextlib.contextmanager
def fake_bin(**scripts):
    """PATH containing exactly these tools.

    The same seam test_swarm.py::_fake_scheduler and test_plan_shape.py use:
    a real Slurm installation outside this directory must not satisfy a tool
    deliberately omitted from a fixture, and swarm.py must know nothing about
    the substitution -- a bypass flag would be a flag somebody sets in anger
    on a real cluster.
    """
    d = tempfile.mkdtemp(prefix="claims-fakebin-")
    old = os.environ.get("PATH", "")
    try:
        for name, body in scripts.items():
            _script(d, name, body)
        os.environ["PATH"] = d
        yield d
    finally:
        os.environ["PATH"] = old
        shutil.rmtree(d, ignore_errors=True)


SBATCH = 'echo "778899"\n'
SQUEUE_LISTS = 'echo "778899"\n'          # a live job for any job name
SQUEUE_SILENT = 'exit 0\n'                # answered, and knows of no job
SQUEUE_BROKEN = 'echo "down" >&2\nexit 1\n'


def slurm_unit(uid="A", outputs=("out.txt",), **over):
    u = {"id": uid, "kind": "slurm", "command": "true", "runtime": "none",
         "outputs": list(outputs)}
    u.update(over)
    return u


def plan_of(*units):
    return {"project": "p", "units": [dict(u) for u in units]}


class ClaimCase(unittest.TestCase):
    """One run root, two coordinators, each with its own state directory."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="claims-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "runs"
        self.root.mkdir()
        self.shared = self.tmp / "canonical"
        self.shared.mkdir()

    def state_dir(self, name):
        d = self.tmp / name
        d.mkdir(exist_ok=True)
        # advance() halts the moment `renew_lease` says False, so a test that
        # never took the lease would be measuring the lease, not the claim.
        ok, why = S.acquire_lease(str(d))
        self.assertTrue(ok, why)
        self.addCleanup(S.release_lease, str(d))
        return d

    def advance(self, plan, state_dir, state=None, dry_run=False):
        state = S.load_state(str(state_dir)) if state is None else state
        report, dispatched, halted = S.advance(
            plan, state, str(state_dir), str(self.root), dry_run)
        return state, report, dispatched

    def unit_state(self, state, uid):
        return state["units"][uid]


class TestOneLiveClaimPerOutputDestination(ClaimCase):

    def test_a_unit_id_live_in_another_state_directory_is_refused(self):
        """The reported failure, reproduced: the same unit id dispatched from
        two plans into one output namespace."""
        first = self.state_dir("state-1")
        second = self.state_dir("state-2")
        with fake_bin(sbatch=SBATCH, squeue=SQUEUE_LISTS):
            s1, _r1, dispatched = self.advance(plan_of(slurm_unit()), first)
            self.assertEqual(dispatched, 1)
            self.assertEqual(self.unit_state(s1, "A")["state"], "SUBMITTED")

            s2, report, dispatched2 = self.advance(
                plan_of(slurm_unit()), second)

        self.assertEqual(dispatched2, 0,
                         "a second state directory dispatched a unit already "
                         "live in the first")
        us = self.unit_state(s2, "A")
        self.assertEqual(us["state"], "PREFLIGHT_REFUSED")
        self.assertIsNone(us["attempt_dir"])
        text = "\n".join(report)
        self.assertIn("output namespace", text)
        self.assertIn(str(first.resolve()), text,
                      "the refusal must name the state directory that holds "
                      "the claim, or a human cannot act on it")
        self.assertIn("squeue lists job 778899", text,
                      "the squeue half of B6 is the error message: say WHY "
                      "the other attempt is believed live")

    def test_two_units_with_different_ids_and_one_promotion_path(self):
        """What the squeue form structurally misses. Two ids, so no job is
        bound to the other's id and no name matches; they collide because
        they publish the same file into the same shared tree."""
        first = self.state_dir("state-1")
        second = self.state_dir("state-2")
        a = slurm_unit("align", outputs=["model.pt"],
                       promote_to=str(self.shared))
        b = slurm_unit("count", outputs=["model.pt"],
                       promote_to=str(self.shared))
        with fake_bin(sbatch=SBATCH, squeue=SQUEUE_LISTS):
            _s1, _r1, d1 = self.advance(plan_of(a), first)
            self.assertEqual(d1, 1)
            s2, report, d2 = self.advance(plan_of(b), second)

        self.assertEqual(d2, 0, "two units publishing one path both ran")
        self.assertEqual(self.unit_state(s2, "count")["state"],
                         "PREFLIGHT_REFUSED")
        text = "\n".join(report)
        self.assertIn(str((self.shared / "model.pt").resolve()), text)
        self.assertIn("'align'", text,
                      "name the other unit; 'something holds it' is not "
                      "actionable")

    def test_two_units_whose_outputs_escape_into_one_directory(self):
        """The same collision without promote_to: a declared output that
        climbs out of its own namespace lands where another's can."""
        first = self.state_dir("state-1")
        second = self.state_dir("state-2")
        with fake_bin(sbatch=SBATCH, squeue=SQUEUE_LISTS):
            _s1, _r1, d1 = self.advance(
                plan_of(slurm_unit("one", outputs=["../shared/x.tsv"])), first)
            self.assertEqual(d1, 1)
            _s2, report, d2 = self.advance(
                plan_of(slurm_unit("two", outputs=["../shared/x.tsv"])),
                second)
        self.assertEqual(d2, 0)
        self.assertIn(str((self.root / "shared" / "x.tsv").resolve()),
                      "\n".join(report))

    def test_two_units_sharing_a_relative_output_name_are_not_refused(self):
        """The false refusal this must not become. `metrics.jsonl` is the
        commonest output name in the repo and it is relative to each unit's
        OWN exclusive write root, so two units declaring it collide nowhere.
        A lock on the bare declared string would refuse almost every plan."""
        first = self.state_dir("state-1")
        second = self.state_dir("state-2")
        with fake_bin(sbatch=SBATCH, squeue=SQUEUE_LISTS):
            _s1, _r1, d1 = self.advance(
                plan_of(slurm_unit("one", outputs=["metrics.jsonl"])), first)
            _s2, report, d2 = self.advance(
                plan_of(slurm_unit("two", outputs=["metrics.jsonl"])), second)
        self.assertEqual((d1, d2), (1, 1), "\n".join(report))


class TestUnknownIsNotFree(ClaimCase):
    """`survey.py`'s three-state rule, applied where it decides a dispatch.
    "The scheduler does not list it" and "the scheduler could not be asked"
    are different facts, and written the same way the second reads as the
    first."""

    def _hold_a_claim(self, state_dir):
        with fake_bin(sbatch=SBATCH, squeue=SQUEUE_LISTS):
            _s, _r, dispatched = self.advance(plan_of(slurm_unit()),
                                              state_dir)
        self.assertEqual(dispatched, 1)

    def test_an_absent_squeue_does_not_read_as_free(self):
        first = self.state_dir("state-1")
        second = self.state_dir("state-2")
        self._hold_a_claim(first)
        with fake_bin(sbatch=SBATCH):          # squeue deliberately missing
            s2, report, dispatched = self.advance(
                plan_of(slurm_unit()), second)
        self.assertEqual(dispatched, 0,
                         "a missing squeue was read as proof the other "
                         "attempt had finished")
        self.assertEqual(self.unit_state(s2, "A")["state"],
                         "PREFLIGHT_REFUSED")
        text = "\n".join(report)
        self.assertIn("squeue is not on PATH", text)
        self.assertIn("UNKNOWN IS NOT FREE", text)

    def test_an_absent_squeue_stays_absent_with_real_slurm_on_the_host(self):
        host_squeue = shutil.which("squeue")
        if host_squeue is None:
            self.skipTest("this host has no real Slurm binary")

        first = self.state_dir("state-1")
        second = self.state_dir("state-2")
        self._hold_a_claim(first)
        with fake_bin(sbatch=SBATCH):          # squeue deliberately missing
            self.assertIsNone(shutil.which("squeue"))
            s2, report, dispatched = self.advance(
                plan_of(slurm_unit()), second)

        self.assertEqual(dispatched, 0, "\n".join(report))
        self.assertEqual(self.unit_state(s2, "A")["state"],
                         "PREFLIGHT_REFUSED")
        text = "\n".join(report)
        self.assertIn("squeue is not on PATH", text)
        self.assertIn("UNKNOWN IS NOT FREE", text)

    def test_a_failing_squeue_does_not_read_as_free(self):
        first = self.state_dir("state-1")
        second = self.state_dir("state-2")
        self._hold_a_claim(first)
        with fake_bin(sbatch=SBATCH, squeue=SQUEUE_BROKEN):
            _s2, report, dispatched = self.advance(
                plan_of(slurm_unit()), second)
        self.assertEqual(dispatched, 0)
        text = "\n".join(report)
        self.assertIn("failed", text)
        self.assertIn("UNKNOWN IS NOT FREE", text)

    def test_the_refusal_names_the_claim_directory_to_remove(self):
        """A refusal whose only remedy is unknown is a wedge. The claim has
        no TTL by design, so the escape hatch has to be written down where it
        is read."""
        first = self.state_dir("state-1")
        second = self.state_dir("state-2")
        self._hold_a_claim(first)
        with fake_bin(sbatch=SBATCH):
            _s2, report, _d = self.advance(plan_of(slurm_unit()), second)
        text = "\n".join(report)
        self.assertIn("rm -r ", text)
        claim = S._claim_dir(self.root, self.root.resolve() / "A")
        self.assertIn(str(claim), text)
        self.assertTrue(claim.is_dir())

    def test_squeue_answering_that_it_knows_no_such_job_releases_it(self):
        """The other half, without which the mechanism is a one-way ratchet.
        A registry that ANSWERED and does not list the attempt is positive
        evidence, and the dead claim is taken over rather than obeyed."""
        first = self.state_dir("state-1")
        second = self.state_dir("state-2")
        self._hold_a_claim(first)
        with fake_bin(sbatch=SBATCH, squeue=SQUEUE_SILENT):
            s2, report, dispatched = self.advance(
                plan_of(slurm_unit()), second)
        self.assertEqual(dispatched, 1, "\n".join(report))
        self.assertEqual(self.unit_state(s2, "A")["state"], "SUBMITTED")
        claim, err = S.U.read_json(
            S._claim_dir(self.root, self.root.resolve() / "A") / S.CLAIM_FILE)
        self.assertIsNone(err)
        self.assertEqual(claim["state_dir"], str(second.resolve()))

    def test_an_unreadable_claim_refuses_rather_than_being_assumed_empty(self):
        """A coordinator that died between the mkdir and the write leaves a
        claim naming nobody. It cannot be adjudicated, so it is not free."""
        state_dir = self.state_dir("state-1")
        claim = S._claim_dir(self.root, self.root.resolve() / "A")
        claim.mkdir(parents=True)
        with fake_bin(sbatch=SBATCH, squeue=SQUEUE_SILENT):
            s, report, dispatched = self.advance(
                plan_of(slurm_unit()), state_dir)
        self.assertEqual(dispatched, 0, "\n".join(report))
        self.assertEqual(self.unit_state(s, "A")["state"], "PREFLIGHT_REFUSED")
        self.assertIn("unreadable", "\n".join(report))


class TestAClaimDoesNotWedgeItsOwnProject(ClaimCase):

    def test_a_coordinator_reclaims_its_own_claim_after_a_crash(self):
        """No TTL, and none needed for the common case. Our own state file is
        authority for our own units, and the lease already serialises it, so
        a coordinator that died holding a claim retakes it on restart -- with
        no timeout and no assertion about anybody else's coordinator."""
        state_dir = self.state_dir("state-1")
        with fake_bin(sbatch=SBATCH, squeue=SQUEUE_LISTS):
            state, _r, d1 = self.advance(plan_of(slurm_unit()), state_dir)
            self.assertEqual(d1, 1)
            # Simulate the crash: the claim survives on disk, the coordinator
            # forgets the attempt entirely.
            state["units"]["A"] = {"state": None, "attempt_dir": None,
                                   "attempts": [], "gpu_hours": 0.0}
            S.save_state(str(state_dir), state)
            state2, report, d2 = self.advance(plan_of(slurm_unit()), state_dir)
        self.assertEqual(d2, 1, "a coordinator was wedged by its own claim:\n"
                                + "\n".join(report))
        self.assertEqual(self.unit_state(state2, "A")["state"], "SUBMITTED")

    def test_a_claim_is_released_once_the_unit_is_no_longer_live(self):
        state_dir = self.state_dir("state-1")
        claim = S._claim_dir(self.root, self.root.resolve() / "A")
        with fake_bin(sbatch=SBATCH, squeue=SQUEUE_LISTS):
            state, _r, _d = self.advance(plan_of(slurm_unit()), state_dir)
            self.assertTrue(claim.is_dir())
            state["units"]["A"]["state"] = "DONE"
            S.save_state(str(state_dir), state)
            self.advance(plan_of(slurm_unit()), state_dir, state=state)
        self.assertFalse(claim.exists(),
                         "a terminal unit kept its claim, so nothing else "
                         "could ever write that namespace")

    def test_a_refused_dispatch_leaves_no_claim_behind(self):
        """A partial claim would block the very unit that was not allowed to
        start. Two destinations, and the refusal falls on the second."""
        first = self.state_dir("state-1")
        second = self.state_dir("state-2")
        a = slurm_unit("align", outputs=["model.pt"],
                       promote_to=str(self.shared))
        b = slurm_unit("count", outputs=["model.pt"],
                       promote_to=str(self.shared))
        with fake_bin(sbatch=SBATCH, squeue=SQUEUE_LISTS):
            self.advance(plan_of(a), first)
            _s2, _report, d2 = self.advance(plan_of(b), second)
        self.assertEqual(d2, 0)
        self.assertFalse(S._claim_dir(self.root,
                                      self.root.resolve() / "count").exists(),
                         "the namespace claimed before the refusal was kept")

    def test_a_dry_run_takes_no_claim(self):
        state_dir = self.state_dir("state-1")
        with fake_bin(sbatch=SBATCH, squeue=SQUEUE_LISTS):
            self.advance(plan_of(slurm_unit()), state_dir, dry_run=True)
        self.assertFalse((self.root / S.OUTPUT_CLAIMS_DIRNAME).exists(),
                         "a dry run creates no writer, so a claim of its own "
                         "in a shared registry could only refuse honest work")


class TestTheClaimVocabularyIsExplicit(unittest.TestCase):

    def test_the_three_states_are_never_collapsed(self):
        self.assertEqual(
            sorted({S.CLAIM_LIVE, S.CLAIM_FREE, S.CLAIM_UNKNOWN}),
            ["free", "live", "unknown"])

    def test_a_kind_with_no_registry_to_ask_is_unknown_not_free(self):
        verdict, why = S._claim_liveness(
            {"attempt": "abc", "kind": "pipeline", "job": "engine-4242"})
        self.assertEqual(verdict, S.CLAIM_UNKNOWN)
        self.assertIn("liveness", why)

    def test_a_claim_naming_no_attempt_is_unknown(self):
        verdict, _why = S._claim_liveness({"kind": "slurm"})
        self.assertEqual(verdict, S.CLAIM_UNKNOWN)

    def test_a_dry_run_claim_is_free(self):
        verdict, _why = S._claim_liveness(
            {"attempt": "abc", "kind": "slurm", "job": "dry-abc123"})
        self.assertEqual(verdict, S.CLAIM_FREE)


# --- ARC-243 -------------------------------------------------------------

INTENT = {"repo": "/src/project", "branch": "swarm-a1",
          "target_branch": "main",
          "repository_remote": "ssh://git@example.invalid/p.git",
          "base_commit": "0" * 40}

SUBSTITUTES = ("git show 0000000000000000000000000000000000000000:",
               "git diff > /tmp/wip.patch",
               "add a separate worktree at")


class TestTheProtocolForbidsGitStash(unittest.TestCase):
    """C12 shipped with three review findings, all against "can an agent
    actually FOLLOW this", and no test asserts that it can. What IS
    mechanically checkable is that the prohibition never travels without its
    substitute -- a ban with no alternative is a ban that gets worked
    around, and that is the whole reason ARC-243 is a protocol defect rather
    than three mistakes."""

    def protocol(self, **over):
        intent = dict(INTENT)
        intent.update(over)
        return S._code_completion_protocol(intent)

    def test_the_prohibition_is_there_at_all(self):
        self.assertIn("NEVER run `git stash`", self.protocol())

    def test_every_line_that_forbids_it_also_names_the_substitute(self):
        for remote in (INTENT["repository_remote"], None):
            text = self.protocol(repository_remote=remote)
            banning = [l for l in text.splitlines() if "git stash" in l]
            self.assertTrue(banning, "the prohibition disappeared")
            for line in banning:
                for substitute in SUBSTITUTES:
                    self.assertIn(
                        substitute, line,
                        "the prohibition and %r must arrive in the same "
                        "breath; a reader who stops at the ban has nothing "
                        "to do instead" % substitute)

    def test_it_says_why_the_stack_is_shared(self):
        text = self.protocol()
        self.assertIn("SINGLE ref", text)
        self.assertIn("shared common Git directory", text)

    def test_it_requires_porcelain_before_committing(self):
        text = self.protocol()
        line = [l for l in text.splitlines() if "Before every commit" in l]
        self.assertEqual(len(line), 1, text)
        self.assertIn("git status --porcelain", line[0])
        self.assertIn("STOP AND REPORT", line[0])
        self.assertIn("another agent's files", line[0])

    def test_a_protocol_that_lost_a_substitute_is_refused(self):
        """The check is on the ASSEMBLED prompt, so an edit that keeps the
        ban and drops the alternative cannot ship. Patched at the builder
        because a plan cannot reach this text: the coordinator owns it."""
        for substitute in SUBSTITUTES:
            stripped = self.protocol().replace(substitute, "")
            with mock.patch.object(S, "_code_completion_protocol",
                                   lambda _i, t=stripped: t):
                problem = S._code_protocol_problem("task\n\n" + stripped,
                                                   INTENT)
            self.assertIsNotNone(
                problem, "dropping %r left an unclosable prompt acceptable"
                         % substitute)
            self.assertIn("substitute", problem)

    def test_a_protocol_that_lost_the_porcelain_rule_is_refused(self):
        stripped = self.protocol().replace("git status --porcelain", "")
        with mock.patch.object(S, "_code_completion_protocol",
                               lambda _i, t=stripped: t):
            problem = S._code_protocol_problem("task\n\n" + stripped, INTENT)
        self.assertIn("foreign path check", problem or "")

    def test_the_real_dispatched_prompt_satisfies_its_own_check(self):
        unit = {"id": "c1", "kind": "code", "prompt": "work",
                "repo": "/src/project", "target_branch": "main",
                "outputs": ["o"]}
        prompt = S._dispatch_prompt(unit, INTENT)
        self.assertIsNone(S._code_protocol_problem(prompt, INTENT))


class TestTheProtocolReservesMergingForTheOrchestrator(unittest.TestCase):
    RULES = {
        "worker stop instruction": "After opening the pull request, STOP.",
        "worker merge prohibition": "NEVER merge, approve, or enable auto-merge.",
        "merge authority": "Merge decisions belong to the orchestrator.",
        "closure interpretation": (
            "The merged-PR closure criterion describes how the orchestrator's "
            "merge is judged, not an instruction to the worker."),
    }

    def test_generated_protocol_reserves_merging_for_the_orchestrator(self):
        for remote in (INTENT["repository_remote"], None):
            for ref in (None, "refs/remotes/origin/swarm-a1",
                        "refs/heads/swarm-a1"):
                with self.subTest(remote=remote, judgment_ref=ref):
                    intent = dict(INTENT, repository_remote=remote,
                                  judgment_ref=ref)
                    protocol = S._code_completion_protocol(intent)
                    for sentence in self.RULES.values():
                        self.assertIn(sentence, protocol)
                    prompt = S._dispatch_prompt(
                        {"kind": "code", "prompt": "work"}, intent)
                    self.assertIsNone(S._code_protocol_problem(prompt, intent))

    def test_closure_still_requires_a_pr_merged_into_the_recorded_target(self):
        for target in ("main", "release/next"):
            with self.subTest(target=target):
                protocol = S._code_completion_protocol(
                    dict(INTENT, target_branch=target))
                self.assertIn(
                    "only a pull request merged into %r in that recorded "
                    "repository closes this code unit." % target, protocol)

    def test_a_protocol_missing_any_merge_boundary_instruction_is_refused(self):
        for name, sentence in self.RULES.items():
            with self.subTest(instruction=name):
                protocol = S._code_completion_protocol(INTENT)
                stripped = protocol.replace(sentence, "")
                self.assertNotEqual(protocol, stripped)
                # Patch the builder too: suffix equality alone must not let
                # a future builder omission pass the structural check.
                with mock.patch.object(S, "_code_completion_protocol",
                                       return_value=stripped):
                    prompt = S._dispatch_prompt(
                        {"kind": "code", "prompt": "work"}, INTENT)
                    problem = S._code_protocol_problem(prompt, INTENT)
                self.assertIn(name, problem or "")


# --- ARC-679 -------------------------------------------------------------

GATE_CLAUSE = "Before opening a pull request, run this repository's review gate."
PUSH_CLAUSE = "Push after every meaningful edit."


class TestTheProtocolRequiresReviewAndPush(unittest.TestCase):
    def test_author_comes_from_coordinator_intent(self):
        cases = (
            ({"provider": "codex", "model": "gpt-6-astra"},
             "codex/gpt-6-astra"),
            ({"provider": "codex/gpt-6-astra"}, "codex/gpt-6-astra"),
            ({"provider": "codex/gpt-6-astra", "model": "gpt-5.6-luna"},
             "codex/gpt-5.6-luna"),
            ({"provider": "openrouter", "model": "moonshotai/kimi-k2.7-code"},
             "openrouter/moonshotai/kimi-k2.7-code"),
        )
        for routing, author in cases:
            with self.subTest(routing=routing):
                intent = dict(INTENT, **routing)
                unit = dict(routing, kind="code", prompt="work")
                prompt = S._dispatch_prompt(unit, intent)
                self.assertIn("--author " + author, prompt)
                self.assertIsNone(S._code_protocol_problem(prompt, intent))

    def test_missing_identity_is_explicit_and_never_filled_from_a_default(self):
        for routing in ({}, {"provider": "codex"},
                        {"model": "gpt-6-astra"},
                        {"provider": None, "model": "gpt-6-astra"}):
            with self.subTest(routing=routing):
                intent = dict(INTENT, **routing)
                prompt = S._dispatch_prompt({"kind": "code"}, intent)
                self.assertIn("provider/model for this unit is UNKNOWN", prompt)
                self.assertIn("do not invent an author or omit --author", prompt)
                self.assertNotIn("Pass --author", prompt)
                self.assertIsNone(S._code_protocol_problem(prompt, intent))

    def test_review_covers_committed_delta_and_preserves_a_failed_review(self):
        protocol = S._code_completion_protocol(INTENT)
        for sentence in (
                GATE_CLAUSE, "hanig-review-gate's review.py",
                "--range " + INTENT["base_commit"] + "..HEAD",
                "COMPLETE delta from the recorded base",
                "Commit all intended edits first and leave the worktree clean",
                "Only exit 0 (REVIEW_PASS) permits opening a pull request",
                "Reproduce each confirmed finding before acting on it.",
                "If a reproduced failure remains, do NOT open a pull request",
                "RECOVERY, NOT READY:", "refs/heads/recovery/swarm-a1",
                "without moving the anchored attempt ref",
                "REVIEW_PASS means the panel failed to refute the claims, not proof."):
            self.assertIn(sentence, protocol)

    def test_push_requires_changed_content(self):
        protocol = S._code_completion_protocol(INTENT)
        self.assertIn(PUSH_CLAUSE, protocol)
        self.assertIn("Commit and push the actual changed content", protocol)
        self.assertIn("An empty or marker commit preserves no implementation", protocol)
        self.assertIn("a tree identical to the recorded base is refused", protocol)

    def test_plan_validation_refuses_either_missing_clause(self):
        unit = {"id": "code", "kind": "code", "repo": "/src/project",
                "target_branch": "main", "prompt": "work",
                "mode": "full-access", "outputs": ["o"]}
        real_protocol = S._code_completion_protocol
        for name, clause in (("review gate", GATE_CLAUSE),
                             ("push discipline", PUSH_CLAUSE)):
            with self.subTest(clause=name):
                def missing(intent):
                    protocol = real_protocol(intent)
                    self.assertIn(clause, protocol)
                    return protocol.replace(clause, "")
                with fake_bin(paseo="exit 99\n"), mock.patch.object(
                        S, "_code_completion_protocol", side_effect=missing):
                    with self.assertRaisesRegex(S.PlanError, name):
                        S.validate_plan(plan_of(unit))


class TestReviewAndPushBeforeAgentCreation(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="protocol-dispatch-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "repo"
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True,
                       env=ENV)
        (self.repo / "tracked.txt").write_text("base\n")
        git(self.repo, "add", "tracked.txt")
        git(self.repo, "commit", "-qm", "base")
        self.remote = self.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)],
                       check=True, env=ENV)
        git(self.repo, "remote", "add", "origin", str(self.remote))
        git(self.repo, "branch", "-M", "main")
        git(self.repo, "push", "-qu", "origin", "main")
        self.attempt = self.tmp / "runs" / "code" / "a1"
        self.attempt.mkdir(parents=True)
        self.unit = {"id": "code", "kind": "code", "repo": str(self.repo),
                     "target_branch": "main", "prompt": "work",
                     "mode": "full-access", "outputs": ["o"]}
        self.launches = []
        real_run = S.U.run

        def run(argv, **kwargs):
            if argv[:2] == ["paseo", "run"]:
                self.launches.append(argv)
                return 1, "", "test stopped at agent creation"
            return real_run(argv, **kwargs)

        patch = mock.patch.object(S.U, "run", side_effect=run)
        patch.start()
        self.addCleanup(patch.stop)

    def submit(self):
        return S._submit(self.unit, str(self.attempt), False,
                         {"units": {}}, str(self.tmp / "state"))

    def assert_missing_clause_refuses(self, clause, name):
        real_protocol = S._code_completion_protocol

        def missing(intent):
            protocol = real_protocol(intent)
            self.assertIn(clause, protocol)
            return protocol.replace(clause, "")

        # Patch the builder too, so suffix equality cannot mask a missing
        # required-set check. The real _submit must refuse before U.run.
        with mock.patch.object(S, "_code_completion_protocol", side_effect=missing):
            job, error = self.submit()
        self.assertEqual(self.launches, [], "missing clause reached agent creation")
        self.assertIsNone(job)
        self.assertIn("missing " + name, error or "")

    def test_missing_gate_clause_refuses_before_agent_creation(self):
        self.assert_missing_clause_refuses(GATE_CLAUSE, "review gate")

    def test_missing_push_clause_refuses_before_agent_creation(self):
        self.assert_missing_clause_refuses(PUSH_CLAUSE, "push discipline")

    def test_intact_protocol_reaches_agent_creation(self):
        _job, error = self.submit()
        self.assertEqual(len(self.launches), 1, error)
        prompt = self.launches[0][-1]
        self.assertIn(GATE_CLAUSE, prompt)
        self.assertIn(PUSH_CLAUSE, prompt)
        # This is a NEW launch: its default is recorded before agent creation.
        self.assertIn("--author " + S.DEFAULT_AGENT_PROVIDER, prompt)


class TestRecoveryCommandsPreserveContent(unittest.TestCase):
    """Execute the recovery block delivered to workers, against a real remote."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="protocol-recovery-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "repo"
        self.remote = self.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True,
                       env=ENV)
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)],
                       check=True, env=ENV)
        git(self.remote, "config", "core.logAllRefUpdates", "true")
        (self.repo / "tracked.txt").write_text("base\n")
        git(self.repo, "add", "tracked.txt")
        git(self.repo, "commit", "-qm", "base")
        git(self.repo, "branch", "-M", "swarm-a1")
        git(self.repo, "remote", "add", "origin", str(self.remote))
        self.base = git(self.repo, "rev-parse", "HEAD").strip()
        self.anchored_ref = "refs/heads/swarm-a1"
        git(self.repo, "push", "-q", "origin",
            self.base + ":" + self.anchored_ref)
        self.recovery_ref = "refs/heads/recovery/swarm-a1"
        self.message = ("RECOVERY, NOT READY: not reviewed; must not be merged; "
                        "open reproduced finding: sample failure")
        self.protocol = S._code_completion_protocol(dict(
            INTENT, repo=str(self.repo), repository_remote=str(self.remote),
            base_commit=self.base, judgment_ref=self.anchored_ref))

    def run_recovery(self):
        # Nothing restates the shell commands: mutations to the delivered
        # block are executed by the consumer, /bin/sh, in this real repo.
        block = self.protocol.split("```sh\n", 1)[1].split("\n```", 1)[0]
        return subprocess.run(
            ["sh", "-c", block], cwd=self.repo, capture_output=True, text=True,
            env=dict(ENV, SWARM_RECOVERY_MESSAGE=self.message))

    def make_work(self, committed):
        (self.repo / "tracked.txt").write_text("changed\n")
        (self.repo / "new.txt").write_text("new content\n")
        # Stage exactly the work the caller inspected and owns, per protocol.
        git(self.repo, "add", "tracked.txt", "new.txt")
        if committed:
            git(self.repo, "commit", "-qm", "implementation")

    def check_recovered(self, committed):
        self.make_work(committed)
        content_head = git(self.repo, "rev-parse", "HEAD").strip()
        result = self.run_recovery()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        head = git(self.repo, "rev-parse", "HEAD").strip()
        self.assertEqual(git(self.remote, "rev-parse", self.recovery_ref).strip(), head)
        self.assertEqual(git(self.remote, "rev-parse", self.anchored_ref).strip(),
                         self.base, "recovery moved the attempt's anchored ref")
        self.assertEqual(git(self.remote, "show", head + ":tracked.txt"), "changed\n")
        self.assertEqual(git(self.remote, "show", head + ":new.txt"), "new content\n")
        self.assertEqual(git(self.remote, "log", "-1", "--format=%s", head).strip(),
                         self.message)
        pushed = git(self.remote, "reflog", "show", "--format=%H",
                     self.recovery_ref).splitlines()
        self.assertEqual(len(pushed), 2 if committed else 1)
        if committed:
            self.assertEqual(pushed[-1], content_head,
                             "the first push must preserve the existing content")
            self.assertEqual(git(self.remote, "rev-parse", head + "^1").strip(),
                             content_head)
            self.assertEqual(git(self.remote, "rev-parse", head + "^{tree}"),
                             git(self.remote, "rev-parse", content_head + "^{tree}"))

    def test_dirty_tree_is_committed_and_pushed(self):
        self.check_recovered(committed=False)

    def test_clean_tree_pushes_content_before_empty_recovery_label(self):
        self.check_recovered(committed=True)

    def test_empty_label_alone_is_not_preservation(self):
        git(self.repo, "commit", "--allow-empty", "-qm", self.message)
        before = git(self.repo, "rev-parse", "HEAD")
        result = self.run_recovery()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires real content", result.stderr)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), before)
        self.assertEqual(git(self.remote, "for-each-ref", self.recovery_ref), "")

    def test_failed_content_push_does_not_create_a_label(self):
        self.make_work(committed=True)
        before = git(self.repo, "rev-parse", "HEAD")
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        result = self.run_recovery()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), before)
        self.assertEqual(git(self.remote, "for-each-ref", self.recovery_ref), "")


class TestTheCoordinatorRunsNoStashOfItsOwn(unittest.TestCase):
    """The refusal below must not fire on a stash the coordinator itself
    parked. It cannot, because the coordinator parks none -- which is a claim
    worth checking rather than asserting, since it is the premise."""

    def test_the_only_stash_subcommand_the_coordinator_runs_is_list(self):
        for path in sorted(SCRIPTS.glob("*.py")):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                literals = [a.value for a in node.args
                            if isinstance(a, ast.Constant)
                            and isinstance(a.value, str)]
                flat = []
                for a in node.args:
                    if isinstance(a, (ast.List, ast.Tuple)):
                        flat += [e.value for e in a.elts
                                 if isinstance(e, ast.Constant)
                                 and isinstance(e.value, str)]
                words = literals + flat
                if "stash" not in words:
                    continue
                self.assertIn(
                    "list", words,
                    "%s:%d runs a stash subcommand other than `list`. The "
                    "dispatch refusal assumes the coordinator parks nothing; "
                    "a coordinator that stashes would refuse its own work."
                    % (path.name, node.lineno))


class TestANonEmptyStashStackRefusesACodeDispatch(unittest.TestCase):
    """C10 refuses a dirty tree at second zero. This is the same shape for
    the one piece of Git state that CROSSES the per-attempt worktree
    boundary: the dirty index of the source checkout cannot reach a worktree
    made from an object id, and the stash stack can, because it is one ref in
    the shared common Git directory."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="stash-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True,
                       env=ENV)
        (self.repo / "tracked.txt").write_text("base\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "base")
        self.remote = self.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)],
                       check=True, env=ENV)
        git(self.repo, "remote", "add", "origin", str(self.remote))
        git(self.repo, "branch", "-M", "main")
        git(self.repo, "push", "-qu", "origin", "main")
        self.attempt = self.tmp / "runs" / "code" / "a1"
        self.attempt.mkdir(parents=True)

    def unit(self):
        return {"id": "code", "kind": "code", "repo": str(self.repo),
                "target_branch": "main", "prompt": "work",
                "mode": "full-access", "outputs": ["o"]}

    def park_something(self):
        (self.repo / "tracked.txt").write_text("edited\n")
        git(self.repo, "stash", "push", "-q", "-m", "somebody-elses-work")

    def test_a_clean_stack_dispatches(self):
        err, anchored = S._capture_code_launch(str(self.attempt), self.unit())
        self.assertIsNone(err)
        self.assertTrue(anchored["base"])

    def test_a_repository_without_origin_refuses_before_agent_creation(self):
        git(self.repo, "remote", "remove", "origin")
        err, anchored = S._capture_code_launch(str(self.attempt), self.unit())
        self.assertIsNone(anchored)
        self.assertIn("has no readable single origin push destination", err)
        self.assertIn("must push", err)

    def test_a_parked_entry_refuses_the_launch(self):
        self.park_something()
        err, anchored = S._capture_code_launch(str(self.attempt), self.unit())
        self.assertIsNone(anchored)
        self.assertIsInstance(err, S.PreflightRefusal)
        self.assertEqual(err.reason, "shared-stash-stack")
        self.assertEqual(err.workspace, str(self.repo.resolve()))
        self.assertIn("somebody-elses-work", err)
        self.assertIn("SINGLE ref", err)
        self.assertIn("git stash list", err,
                      "say how to clear it; a refusal with no remedy is a "
                      "wedge")

    def test_the_refusal_is_carried_out_of_submit(self):
        """PreflightRefusal is the signal advance classifies on, so a stash
        refusal costs no retry -- the same treatment a dirty tree gets."""
        self.park_something()
        job, err = S._submit(self.unit(), str(self.attempt), False,
                             {"units": {}}, str(self.tmp / "state"))
        self.assertIsNone(job)
        self.assertIsInstance(err, S.PreflightRefusal)

    def test_a_redispatch_of_the_same_attempt_re_asks(self):
        """Retry and recovery must not be the way past a launch check. The
        anchored base is never recaptured; only the CURRENT condition is
        re-asked, which is exactly what _write_launch_record does on its own
        already-anchored path."""
        err, anchored = S._capture_code_launch(str(self.attempt), self.unit())
        self.assertIsNone(err)
        state = {"units": {"code": {"attempt_launch_intents": {
            "a1": anchored["intent"]}}}}
        self.park_something()
        job, err = S._submit(self.unit(), str(self.attempt), False, state,
                             str(self.tmp / "state"))
        self.assertIsNone(job)
        self.assertIsInstance(err, S.PreflightRefusal)

    def test_an_unreadable_stack_is_not_read_as_empty(self):
        with mock.patch.object(S, "_git", lambda repo, *a, **k: (
                (1, "", "boom") if a[:1] == ("stash",) else (0, "x", ""))):
            problem = S._stash_preflight("code", str(self.repo))
        self.assertIn("cannot be shown to be empty", problem)


class TestTheRunReportSaysWhichPreflightRefused(unittest.TestCase):
    """A zero dirty count rendered as "uncommitted changes" sent a reader to
    clean a workspace that was already clean."""

    def test_every_refusal_carries_a_reason(self):
        self.assertEqual(S.PreflightRefusal("x").reason, "dirty-worktree")
        self.assertEqual(
            S._dirty_refusal("u", "/w", [{"status": "M", "path": "a"}]).reason,
            "dirty-worktree")
        self.assertEqual(S._stash_refusal("u", "/w", ["stash@{0}: wip"]).reason,
                         "shared-stash-stack")

    def _rendered(self, refusal):
        sys.path.insert(0, str(ROOT / "skills" / "hanig-project" / "scripts"))
        import report as R  # noqa: E402
        tmp = tempfile.mkdtemp(prefix="claims-report-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        state_dir = Path(tmp) / ".swarm" / "state"
        state_dir.mkdir(parents=True)
        (Path(tmp) / "plan.json").write_text(json.dumps(
            {"project": "p",
             "units": [{"id": "a", "kind": "slurm", "outputs": ["out.txt"],
                        "needs": []}]}))
        (state_dir / "swarm-state.json").write_text(json.dumps(
            {"plan_digest": "abc", "units": {"a": {
                "state": "PREFLIGHT_REFUSED", "attempt_dir": None,
                "attempts": [], "preflight_refusals": [refusal]}}}))
        return R.render(R.collect(tmp))

    def test_the_html_report_renders_each_reason_distinctly(self):
        base = {"workspace": "/checkout", "dirty_path_count": 0,
                "receipt": None}
        stash = self._rendered(dict(base, reason="shared-stash-stack"))
        claim = self._rendered(dict(base, reason="output-claim-held"))
        dirty = self._rendered(dict(base, dirty_path_count=2,
                                    reason="dirty-worktree"))
        self.assertIn("git stash", stash)
        self.assertNotIn("uncommitted changes", stash)
        self.assertIn("another state directory", claim)
        self.assertIn("2 dirty path(s)", dirty)

    def test_an_old_refusal_with_no_reason_still_renders(self):
        """State written before this field existed must not disappear from
        the report; the default is the refusal that used to be the only one."""
        html = self._rendered({"workspace": "/checkout",
                               "dirty_path_count": 3, "receipt": None})
        self.assertIn("3 dirty path(s)", html)


if __name__ == "__main__":
    unittest.main()
