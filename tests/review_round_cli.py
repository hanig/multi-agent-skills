"""Subprocess CLI harness: real parser, ledger and journal; offline providers.

Only the roster and provider transport are substituted. Calls leave evidence
outside the ledger so a refusal must precede both transport and journal writes.
"""

import importlib.util
import json
import os
from pathlib import Path
import sys


def main():
    script = sys.argv.pop(1)
    sys.argv[0] = script
    spec = importlib.util.spec_from_file_location("cli_review", script)
    review = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(review)
    review.load_effective_reviewers = lambda: [
        {"name": provider, "provider": provider, "model": "fixture",
         "profiles": ["fast", "standard", "deep"], "enabled": True}
        for provider in ("openai", "openrouter")]

    def transport(_url, payload, *_args, **_kwargs):
        with Path(os.environ["ROUND_CLI_CALLS"]).open("a") as stream:
            stream.write("provider\n")
        answer = json.dumps({"verdict": "upheld", "findings": [], "claims": [
            {"claim_index": 0, "status": "supported",
             "claim": review.HONEST_RUN_CLAIM, "why": "The offline candidate fixture preserves its declared output."}],
            "notes": "Offline test fixture."})
        if "input" in payload:
            return {"output": [{"type": "message", "content": [
                {"type": "output_text", "text": answer}]}]}, None
        return {"choices": [{"message": {"content": answer},
                             "finish_reason": "stop"}]}, None

    def unexpected_network(*_args, **_kwargs):
        raise AssertionError("unexpected real network access")

    review._post = transport
    review.urllib.request.urlopen = unexpected_network
    if os.environ.get("ROUND_CLI_LOCK_READY"):
        original_flock = review.fcntl.flock

        def observed_flock(*args):
            try:
                return original_flock(*args)
            except BlockingIOError:
                Path(os.environ["ROUND_CLI_LOCK_READY"]).write_text("contended")
                raise

        review.fcntl.flock = observed_flock
    review.main()


if __name__ == "__main__":
    main()
