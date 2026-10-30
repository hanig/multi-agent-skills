"""Synthetic submitted effect child; R3's installation-side M5 consumer."""
from contextlib import nullcontext
import os
from pathlib import Path
import subprocess
import sys
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "skills/hanig-orchestrate/scripts"))
import d5_effect_log as ledger
import d5_protocol_locks as protocol


def main():
    role, directory = sys.argv[1:3]
    with protocol.pin(directory) as record:
        if role == "ancestor":
            with record.drain() as fd:
                child = subprocess.Popen([sys.executable, __file__, "effect", directory, str(fd)],
                                         close_fds=True, pass_fds=(fd,))
                return child.wait()
        inherited = int(sys.argv[3])
        os.set_inheritable(inherited, False)
        with record.drain(inherited_fd=inherited):
            os.close(inherited)
            with nullcontext(), \
                    mock.patch.object(ledger.registry, "lookup", return_value={
                        "registration_id": "a" * 32, "record_dir": directory}):
                attempt = ledger.prepare("fixture", "p", "fixture", record_id="a" * 32,
                    custody=dict(generation=1, session_id="fixture", anchor_pid=42, anchor_start_us=101),
                    kind="synthetic", scope=dict(plans=["p"], repositories=["r"], actions=["a"], trackers=[]),
                    correlation={"request_id": "M5"})
                receipt = ledger._classify(attempt, "submitted")
                print("submitted", flush=True)
                if sys.stdin.readline() != "release\n":
                    return 1
                ledger.complete(receipt, {"status": "succeeded", "detail": "synthetic response"})
        print("completed and released", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
