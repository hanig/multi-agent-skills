#!/usr/bin/env python3
"""Offline tests for committee.py session token-usage evidence."""

import importlib.util
import io
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SCRIPT = (REPO / "skills" / "hanig-review-gate" / "scripts" /
          "committee.py")

spec = importlib.util.spec_from_file_location("committee", SCRIPT)
committee = importlib.util.module_from_spec(spec)
spec.loader.exec_module(committee)


class TestCommitteeTokenUsage(unittest.TestCase):
    def run_stub(self, member, response):
        session = {
            "members": {
                member["name"]: {
                    "provider": member["provider"],
                    "model": member["model"],
                    "history": [],
                },
            },
            "turns": [],
        }
        key_var = {
            "openai": "OPENAI_API_KEY",
            "openrouter": "OPENROUTER_API_KEY",
        }[member["provider"]]
        with mock.patch.dict(os.environ, {key_var: "test-key"}), \
                mock.patch.object(committee.R, "_post",
                                  return_value=(response, None)):
            results = committee.run_turn(
                [member], session, "test prompt", 1, "plan")
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            committee.show_results(results)
        return stdout.getvalue(), session

    def test_openrouter_known_output_usage_is_reported_and_persisted(self):
        member = {"name": "stub-deepseek", "provider": "openrouter",
                  "model": "stub/model"}
        response = {
            "choices": [{"message": {"content": "stub answer"}}],
            "usage": {"prompt_tokens": 314, "completion_tokens": 2718},
        }

        output, session = self.run_stub(member, response)

        self.assertIn("  token usage: 314in/2718out", output.splitlines())
        self.assertEqual(
            session["members"]["stub-deepseek"]["usage"],
            [{"phase": "plan", "in_tokens": 314, "out_tokens": 2718}],
        )

    def test_response_without_usage_reports_unknown_instead_of_zero(self):
        member = {"name": "stub-luna", "provider": "openai",
                  "model": "stub-model"}
        response = {
            "output": [{"type": "message",
                        "content": [{"text": "stub answer"}]}],
        }

        output, session = self.run_stub(member, response)

        self.assertIn("  token usage: unknown", output.splitlines())
        self.assertNotIn("0in/0out", output)
        self.assertEqual(
            session["members"]["stub-luna"]["usage"],
            [{"phase": "plan", "in_tokens": None, "out_tokens": None}],
        )

    def test_openai_known_usage_uses_its_provider_field_names(self):
        member = {"name": "stub-luna", "provider": "openai",
                  "model": "stub-model"}
        response = {
            "output": [{"type": "message",
                        "content": [{"text": "stub answer"}]}],
            "usage": {"input_tokens": 123, "output_tokens": 456},
        }

        output, session = self.run_stub(member, response)

        self.assertIn("  token usage: 123in/456out", output.splitlines())
        self.assertEqual(
            session["members"]["stub-luna"]["usage"][0]["out_tokens"],
            456,
        )

    def test_empty_reply_still_reports_provider_usage(self):
        member = {"name": "stub-deepseek", "provider": "openrouter",
                  "model": "stub/model"}
        response = {
            "choices": [{"message": {"content": ""}}],
            "usage": {"prompt_tokens": 900, "completion_tokens": 128000},
        }

        output, _session = self.run_stub(member, response)

        self.assertIn("[no reply] empty reply", output)
        self.assertIn("  token usage: 900in/128000out", output.splitlines())


if __name__ == "__main__":
    unittest.main(verbosity=2)
