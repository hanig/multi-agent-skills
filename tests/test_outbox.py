"""The tracker outbox.

The coordinator cannot reach a tracker and must never depend on one. These
tests pin the three properties that make that separation safe rather than
merely convenient.
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SWARM = ROOT / "skills" / "hanig-swarm" / "scripts" / "swarm.py"
sys.path.insert(0, str(SWARM.parent))
import swarm as S  # noqa: E402

PLAN = {"name": "rna-bench", "units": [
    {"id": "prep", "kind": "slurm", "command": "true", "outputs": ["o"],
     "write_scopes": ["r/p/"]},
    {"id": "train", "kind": "slurm", "command": "true", "outputs": ["o"],
     "needs": ["prep"], "write_scopes": ["r/t/"]}]}


def run(tmp, *argv):
    (tmp / "plan.json").write_text(json.dumps(PLAN))
    return subprocess.run(
        [sys.executable, str(SWARM), *argv, str(tmp / "plan.json"),
         "--dry-run", "--state-dir", str(tmp / "st")],
        capture_output=True, text=True, cwd=tmp)


class TestIdempotency(unittest.TestCase):
    def test_the_same_transition_never_yields_two_intents(self):
        """The point of the key. A drain that retries after a partial failure
        must not open a second issue for one unit."""
        a = S.outbox_key("p", "train", "DONE", "/x/attempt-1")
        b = S.outbox_key("p", "train", "DONE", "/x/attempt-1")
        self.assertEqual(a, b)

    def test_a_new_attempt_is_a_new_intent(self):
        """A preempted unit that reruns is genuinely new work, and the tracker
        should hear about it. Keying on state alone would swallow it."""
        a = S.outbox_key("p", "train", "DONE", "/x/attempt-1")
        b = S.outbox_key("p", "train", "DONE", "/x/attempt-2")
        self.assertNotEqual(a, b)

    def test_distinct_projects_do_not_collide(self):
        self.assertNotEqual(S.outbox_key("a", "u", "DONE", None),
                            S.outbox_key("b", "u", "DONE", None))

    def test_advancing_twice_appends_once(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            run(tmp, "run")
            run(tmp, "advance")
            run(tmp, "advance")
            keys = [i["key"] for i in S.read_outbox(tmp / "st")]
            self.assertEqual(len(keys), len(set(keys)),
                             "an intent was appended twice")


class TestNothingClosesOnASelfReport(unittest.TestCase):
    def test_a_close_intent_carries_the_verdict(self):
        """A unit reporting success is exactly what this family refuses to
        believe. A close must carry the receipt that a predicate produced."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            st = Path(d); st.mkdir(exist_ok=True)
            S.emit_intent(st, "p", "u", "DONE", {"attempt_dir": None},
                          evidence={"receipt": {"verdict": "DONE"}})
            i = S.read_outbox(st)[0]
            self.assertEqual(i["verb"], "close")
            self.assertIsNotNone(i["evidence"])

    def test_only_declared_states_produce_intents(self):
        """Enumerate the good set. An unrecognised state emits nothing rather
        than guessing at a tracker mutation."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            st = Path(d)
            self.assertIsNone(
                S.emit_intent(st, "p", "u", "RUNNING", {"attempt_dir": None}))
            self.assertEqual(S.read_outbox(st), [])

    def test_every_declared_state_maps_to_a_real_unit_state(self):
        """Closure by exclusion: a state in TRACKER_EVENTS that swarm.py can
        never produce is dead code that will silently stop firing."""
        produced = set(S.NAME.values()) | {
            "SUBMITTED", "HELD", "PREEMPTED", "FAILED_EVIDENCE"}
        unreachable = set(S.TRACKER_EVENTS) - produced
        self.assertEqual(unreachable, set(),
                         f"TRACKER_EVENTS names states nothing produces: "
                         f"{unreachable}")


class TestTheTrackerIsNeverAuthoritative(unittest.TestCase):
    def test_swarm_has_no_network_code(self):
        """Structural. The coordinator runs on a shared login node; it holds no
        tracker token because it makes no calls. Checked by AST, not by grep,
        so a string in a docstring cannot trip or satisfy it."""
        import ast
        tree = ast.parse(SWARM.read_text())
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods.update(a.name.split(".")[0] for a in n.names)
            elif isinstance(n, ast.ImportFrom) and n.module:
                mods.add(n.module.split(".")[0])
        banned = mods & {"urllib", "http", "requests", "socket", "httpx"}
        self.assertEqual(banned, set(),
                         f"the coordinator imports network code: {banned}")

    def test_an_unwritable_outbox_does_not_stop_the_swarm(self):
        """A full or read-only filesystem must degrade to a warning. Losing a
        ticket update is an inconvenience; stalling the DAG is an outage."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            st = Path(d) / "nope" / "deeper"   # parent does not exist
            self.assertIsNone(
                S.emit_intent(st, "p", "u", "DONE", {"attempt_dir": None}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
