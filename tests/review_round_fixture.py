"""Independent reservation fixtures for tests of other review subsystems.

Each invocation has its own change and ledger. Fresh-panel tests receive an
exhausted predecessor. The real reservation still runs; none of the panel,
transport, verdict or journal assertions is bypassed. Cross-invocation and Git
identity behavior is exercised separately by test_review_rounds.
"""

import tempfile
from pathlib import Path
from unittest.mock import patch


def isolated_reservation(module, original, args):
    with tempfile.TemporaryDirectory(prefix="review-round-fixture-") as root, \
            patch.object(module, "round_ledger_root", return_value=Path(root)), \
            patch.object(module, "change_identity", return_value="a" * 64):
        if args.fresh_cycle_from:
            module.reserve_round("a" * 64, module.MAX_ROUNDS,
                                 args.fresh_cycle_from)
            if args.round > 1:
                module.reserve_round("a" * 64, 1, args.selected_profile,
                                     args.fresh_cycle_from)
        return original(args)
