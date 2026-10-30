from contextlib import nullcontext
"""Live retained protection and kernel release at exit, at two interrupt sites."""
import inspect
import json
import subprocess
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tests import test_d5_submission as t
import swarm_state as swarm_state


def child(data):
    case = t.TestSubmission()
    case.args, directory, case.request, scope, external = data
    case.directory = Path(directory)
    assert swarm_state.acquire_lease(Path(case.args[2]), bump_epoch=False) == (True, None)
    case.request["anchor"] = t.ANCHOR
    case.operation = t.submission.Operation("A", "fixture", scope.encode(), b"effect")
    case.boundary = t.DeterministicBoundary()
    lines, first = inspect.getsourcelines(t.submission.submit)
    entry = ("outcome = boundary.finish()" if "--finish-entry" in sys.argv else
             "if S and A and state is not Initiation.NOT_SUBMITTED:")
    target = first + next(i for i, line in enumerate(lines) if line.strip() == entry)
    def interrupt(frame, event, arg):
        if event == "line" and frame.f_code is t.submission.submit.__code__ and frame.f_lineno == target:
            sys.settrace(None)
            raise KeyboardInterrupt("cleanup-entry")
        return interrupt
    with t.mock.patch.object(t.registry.CP, "_external_base", return_value=Path(external)), \
            nullcontext(), \
            t.mock.patch.object(t.host, "session_anchor", return_value=("native", t.ANCHOR)), \
            t.mock.patch.object(t.admission.time, "time", return_value=150):
        try:
            sys.settrace(interrupt)
            case.submit()
        except KeyboardInterrupt as exc:
            assert str(exc) == "cleanup-entry"
        else:
            raise AssertionError("interrupt did not fire")
        finally:
            sys.settrace(None)
        assert [c[0] for c in case.boundary.calls] == ["start"]
        assert case.rows()[0]["outcome"] is None
        assert case.probe("drain") == "blocked"


def reproduce():
    case = t.TestSubmission()
    case.setUp()
    try:
        data = [list(map(str, case.args)), str(case.directory),
                {k: v for k, v in case.request.items() if k != "anchor"},
                case.operation.scope.decode(), str(case.root / "external")]
        swarm_state.release_lease(case.state_dir)
        result = subprocess.run([sys.executable, __file__, json.dumps(data)] + sys.argv[1:], timeout=30)
        assert result.returncode == 0
        assert case.probe("drain") == "available"
        row, = t.ledger.unresolved(*case.args)
        state = "unknown" if "--finish-entry" in sys.argv else "submitted"
        assert row["state"] == state and row["outcome"] is None
        assert row["reconciliation_required"] is (state != "submitted")
    finally:
        case.doCleanups()


if __name__ == "__main__":
    child(json.loads(sys.argv[1])) if len(sys.argv) > 1 and sys.argv[1] != "--finish-entry" else reproduce()
