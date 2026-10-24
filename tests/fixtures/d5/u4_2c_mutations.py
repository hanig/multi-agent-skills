"""Opt-in U4.2-c mutations: green/red/green, exact-byte restoration, no commits."""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "skills/hanig-orchestrate/scripts/d5_submission.py"
MODULE = "tests.test_d5_u4_2c_completion_provider.TestCompletionProvider."
MUTATIONS = [
    ("journal-ledger-fallback", "outcome = capability.complete(outcome)",
     "outcome = completion.bind(attempt).complete(outcome)",
     "test_journal_dispatch_preparation_binding_and_completion"),
    ("provider-from-result", "outcome = boundary.finish()",
     'outcome = boundary.finish()\n                    provider = completion._PROVIDERS[outcome.get("provider", "journal")]',
     "test_provider_is_fixed_before_result_and_never_from_result"),
    ("exit-is-completion", "outcome = capability.complete(outcome)",
     "outcome = outcome  # mutant: Finish return substitutes for durable completion",
     "test_missing_malformed_or_unbound_readback_retains_holder"),
    ("uncertainty-to-ledger", "provider.bind(attempt).uncertain()",
     "completion.bind(attempt).uncertain()",
     "test_uncertainty_uses_same_provider_and_never_ledger"),
    ("release-expression-changed", "H and S and (not A or N or (F and C)):",
     "H and S and (not A or N or F):",
     "test_release_expression_unchanged"),
    ("missing-completion-validation",
     "outcome = capability.complete(outcome)\n                        provider.validate_outcome(outcome)",
     "outcome = capability.complete(outcome)",
     "test_malformed_capability_return_does_not_establish_C"),
]


def main():
    original = SOURCE.read_bytes()
    try:
        for name, old, new, test in MUTATIONS:
            if original.decode().count(old) != 1:
                raise AssertionError("mutation anchor not unique: " + name)
            for phase, raw, succeeds in (
                    ("before", original, True),
                    ("mutated", original.decode().replace(old, new).encode(), False),
                    ("restored", original, True)):
                SOURCE.write_bytes(raw)
                print(name, phase, flush=True)
                result = subprocess.run([sys.executable, "-B", "-m", "unittest", MODULE + test],
                    cwd=str(ROOT), capture_output=True, text=True, timeout=60)
                print(result.stdout + result.stderr, end="", flush=True)
                if (result.returncode == 0) != succeeds:
                    raise AssertionError("mutation not detected: " + name + " " + phase)
    finally:
        SOURCE.write_bytes(original)
    print("All U4.2-c mutations detected; source restored.")


if __name__ == "__main__":
    main()
