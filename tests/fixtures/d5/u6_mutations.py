"""Run the U6 named mutations in memory; never edit the checkout or use a tracker."""
import inspect
import io
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tests import test_d5_u6_tracker_reject as T


def check(label, owner, name, transform, test):
    source = inspect.getsource(getattr(owner, name))
    mutated = transform(source)
    if source == mutated:
        raise AssertionError("mutation did not change " + name)
    # Same globals as the consumer, with only this function replaced.
    local = {}
    exec(compile(mutated, "<u6-mutation:" + label + ">", "exec"), owner.__dict__, local)
    output = io.StringIO()
    with mock.patch.object(owner, name, local[name]):
        result = unittest.TextTestRunner(stream=output).run(
            unittest.defaultTestLoader.loadTestsFromName(test, T))
    print(label + ": " + ("KILLED" if not result.wasSuccessful() else "SURVIVED"))
    print(output.getvalue(), end="")
    if result.wasSuccessful() or result.testsRun != 1:
        raise AssertionError("mutation must fail its one regression: " + label)


def main():
    for name in T.ENTRIES:
        def remove(source):
            lines = source.splitlines(keepends=True)
            index = next(i for i, line in enumerate(lines) if "if D5.d5_active():" in line)
            del lines[index:index + 2]
            return "".join(lines)
        check("revert-" + name, T.SO, name, remove,
              "TestTrackerRefusal.test_premature_activation_helpers_have_zero_effects")
    check("outbox-enqueue-active", T.S, "advance", lambda source: source.replace(
        "    _d5_refuse('advance')", "    SO.emit_intent(state_dir, 'fixture', 'u', 'SUBMITTED', {})\n"
        "    _d5_refuse('advance')"),
        "TestTrackerRefusal.test_row75_containing_entries_refuse_before_helpers")
    check("backfill-reachable", T.S, "advance", lambda source: source.replace(
        "    _d5_refuse('advance')", "    SO.backfill_tracker_intents(state_dir, 'fixture', {})\n"
        "    _d5_refuse('advance')"),
        "TestTrackerRefusal.test_row75_containing_entries_refuse_before_helpers")
    check("audit-mutates", T.M, "print_tracker_audit", lambda source: source.replace(
        "script, 'audit', *inputs", "script, 'drain', *inputs"),
        "TestAuditObservation.test_active_audit_invokes_only_observations_at_real_transport")


if __name__ == "__main__":
    main()
