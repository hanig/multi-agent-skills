"""RECOVERY, NOT READY: expected-failing round-3 cleanup interruption regression."""
import inspect
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tests import test_d5_submission as t


def reproduce():
    case = t.TestSubmission()
    case.setUp()
    previous = sys.gettrace()
    try:
        pending = []
        observations = []
        case.boundary.on_start = lambda: pending.append('outstanding')
        def finish():
            observations.append((case.probe('drain'), case.probe('start')))
            pending.clear()
            return dict(t.OK)
        case.boundary.on_finish = finish
        lines, first = inspect.getsourcelines(t.submission.submit)
        target = first + next(i for i, line in enumerate(lines)
            if line.strip() == 'if began and state is not Initiation.NOT_SUBMITTED:')
        def interrupt(frame, event, arg):
            if (event == 'line' and frame.f_code is t.submission.submit.__code__
                    and frame.f_lineno == target):
                sys.settrace(None)
                raise KeyboardInterrupt('cleanup-entry')
            return interrupt
        try:
            sys.settrace(interrupt)
            case.submit()
        except KeyboardInterrupt as exc:
            print('interrupt:', str(exc))
        finally:
            sys.settrace(previous)
        calls = [call[0] for call in case.boundary.calls]
        print('calls:', calls)
        print('finish observations:', observations)
        print('pending:', pending)
        print('states:', [row['state'] for row in case.rows()])
        print('drain:', case.probe('drain'))
        assert calls == ['start', 'finish'] and not pending
        assert observations == [('blocked', 'available')]
    finally:
        sys.settrace(previous)
        case.doCleanups()


if __name__ == "__main__":
    reproduce()
