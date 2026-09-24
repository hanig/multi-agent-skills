#!/usr/bin/env python3
"""Offline tests for committee.py session token-usage evidence."""

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
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


class TestCommitteeTiebreak(unittest.TestCase):
    POSITIONS = {
        "luna": "Keep the stable order.\nEvidence: input IDs are already sorted.\n",
        "kimi-k2.7-code": "Reverse the order.\nEvidence: newest ID should appear first.\n",
    }

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        patcher = mock.patch.object(committee, "SESSIONS", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.mandate = REPO / "docs/orchestrator-mandate.md"
        self.session = {
            "name": "split", "phase": "plan", "opened_at": "test",
            "problem": "Which existing ordering should we retain?",
            "turns": [{"label": "ask", "prompt": "State your final position."}],
            "members": {
                name: {"provider": "openai" if name == "luna" else "openrouter",
                       "model": name, "history": [
                           {"role": "assistant", "content": position}]}
                for name, position in self.POSITIONS.items()
            },
        }
        committee.save_session("split", self.session)
        self.calls = []
        self.ruling = {"status": "RULING", "adopts": "luna",
                       "ruling": "RULING: Adopt luna's stable ordering.",
                       "evidence": "The supplied IDs are already sorted."}
        self.synthesis = {"status": "DIVERGED", "reason": "The orders disagree."}
        self.tie_error = None
        self.tie_status = "completed"
        self.tie_text = None

    @staticmethod
    def response(value, status="completed"):
        return {"status": status,
                "output": [{"type": "message", "content": [{
                    "text": value if isinstance(value, str) else json.dumps(value)}]}],
                "usage": {"input_tokens": 12, "output_tokens": 34}}

    def provider(self, url, payload, headers, timeout):
        self.calls.append(payload)
        if payload["model"] == "gpt-6-astra":
            if self.tie_error:
                return None, self.tie_error
            value = self.ruling if self.tie_text is None else self.tie_text
            return self.response(value, self.tie_status), None
        return self.response(self.synthesis), None

    def invoke(self, command="synthesize", *extra, author="gpt-5.6-sol"):
        argv = [str(SCRIPT), command, "split"]
        if command not in ("show",):
            argv += ["--mandate-file", str(self.mandate)]
            if author is not None:
                argv += ["--author", author]
        argv.extend(extra)
        stdout = io.StringIO()
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test-key",
                                             "OPENROUTER_API_KEY": "test-key"}), \
                mock.patch.object(committee.R, "_post", side_effect=self.provider), \
                redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as stopped:
                committee.main()
        return stopped.exception.code, stdout.getvalue(), committee.load_session("split")

    def test_divergence_routes_to_astra_xhigh_and_persists_ruling(self):
        code, output, saved = self.invoke()
        self.assertEqual(code, 0, output)
        self.assertEqual([p["model"] for p in self.calls],
                         ["gpt-5.6-luna", "gpt-6-astra"])
        request = self.calls[-1]
        self.assertEqual(request["reasoning"], {"effort": "xhigh"})
        self.assertEqual(request["max_output_tokens"], 128000)
        prompt = request["input"][-1]["content"]
        packet = json.loads(prompt.split("\n", 1)[1][:-len(committee.NO_EDITS)])
        self.assertEqual(packet["positions"], self.POSITIONS)
        self.assertEqual(packet["question"], self.session["problem"])
        self.assertEqual(packet["latest_prompt"], "State your final position.")
        self.assertEqual(packet["mandate"], self.mandate.read_text())
        self.assertIn("not a fresh plan", prompt)
        self.assertTrue(prompt.endswith(committee.NO_EDITS))
        self.assertIn("authority boundary", request["input"][0]["content"])
        ruling = saved["resolution"]
        self.assertEqual(ruling["status"], "RULING")
        self.assertEqual(ruling["ruling"], self.ruling["ruling"])
        self.assertEqual(ruling["adopts"], "luna")
        self.assertEqual(ruling["evidence"], self.ruling["evidence"])
        self.assertEqual(ruling["reviewer"]["name"], "astra-xhigh")
        self.assertEqual(ruling["usage"]["out_tokens"], 34)
        self.assertEqual([d["status"] for d in saved["decisions"]],
                         ["DIVERGED", "RULING"])
        self.assertIn(self.ruling["ruling"], self.invoke("show")[1])

    def test_direct_tiebreak_uses_persisted_author_on_later_invocation(self):
        self.assertEqual(self.invoke("tiebreak")[0], 0)
        self.assertEqual(self.invoke("tiebreak", author=None)[0], 0)
        self.assertEqual(len(self.calls), 2)

    def test_tiebreaker_unavailable_routes_owner_with_reason(self):
        self.tie_error = "HTTP 503: provider unavailable"
        code, output, saved = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn(self.tie_error, output)
        self.assertEqual(saved["resolution"]["status"], "OWNER")
        self.assertEqual(saved["resolution"]["reason"], self.tie_error)

    def test_astra_author_refuses_without_a_tiebreak_call(self):
        for author in ("gpt-6-astra", "openai/gpt-6-astra", "astra", "astra-xhigh"):
            with self.subTest(author=author):
                committee.save_session("split", self.session)
                self.calls.clear()
                code, output, saved = self.invoke("tiebreak", author=author)
                self.assertEqual(code, 1)
                self.assertIn("cannot judge itself", output)
                self.assertEqual(saved["resolution"]["status"], "OWNER")
                self.assertEqual(self.calls, [])

    def test_synthesis_divergence_respects_astra_author(self):
        code, output, _saved = self.invoke(author="gpt-6-astra")
        self.assertEqual(code, 1)
        self.assertIn("cannot judge itself", output)
        self.assertEqual(len(self.calls), 1)

    def test_other_author_containing_astra_name_is_not_refused(self):
        code, output, _saved = self.invoke(
            "tiebreak", author="my-gpt-6-astra-helper")
        self.assertEqual(code, 0, output)
        self.assertEqual(len(self.calls), 1)

    def test_open_accepts_author_and_prints_followup_instructions(self):
        argv = [str(SCRIPT), "open", "fresh", "--problem", "Which order?",
                "--member", "luna,kimi-k2.7-code", "--author", "gpt-5.6-sol"]
        output = io.StringIO()
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(committee, "ask_member",
                                  return_value=("Final position", None, None)), \
                redirect_stdout(output):
            with self.assertRaises(SystemExit) as stopped:
                committee.main()
        self.assertEqual(stopped.exception.code, 0, output.getvalue())
        self.assertEqual(committee.load_session("fresh")["author"], "gpt-5.6-sol")
        self.assertIn("committee.py synthesize fresh", output.getvalue())

    def test_open_padded_astra_author_then_synthesize_refuses(self):
        argv = [str(SCRIPT), "open", "split", "--force", "--problem", "Order?",
                "--member", "luna,kimi-k2.7-code", "--author", " gpt-6-astra "]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(committee, "ask_member",
                                  return_value=("Final position", None, None)), \
                redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as stopped:
                committee.main()
        self.assertEqual(stopped.exception.code, 0)
        self.assertEqual(committee.load_session("split")["author"], "gpt-6-astra")
        code, _output, saved = self.invoke(author=None)
        self.assertEqual(code, 1)
        self.assertIn("cannot judge itself", saved["resolution"]["reason"])
        self.assertEqual([p["model"] for p in self.calls], ["gpt-5.6-luna"])

    def test_legacy_padded_author_is_repaired_and_refused_on_both_paths(self):
        for author in ("gpt-6-astra", "openai/gpt-6-astra", "ASTRA", "astra-xhigh"):
            for command in ("synthesize", "tiebreak"):
                with self.subTest(author=author, command=command):
                    self.calls.clear()
                    self.session["author"] = " \t" + author + "\n"
                    committee.save_session("split", self.session)
                    code, _output, saved = self.invoke(command, author=None)
                    self.assertEqual(code, 1)
                    self.assertEqual(saved["author"], author)
                    self.assertEqual(saved["resolution"]["inputs"]["author"], author)
                    self.assertIn("cannot judge itself", saved["resolution"]["reason"])
                    self.assertNotIn("gpt-6-astra", [p["model"] for p in self.calls])

    def test_missing_or_conflicting_author_routes_owner(self):
        self.assertEqual(self.invoke("tiebreak", author=None)[0], 1)
        self.assertEqual(self.calls, [])
        self.invoke("tiebreak", author="gpt-6-astra")
        code, output, saved = self.invoke("tiebreak")
        self.assertEqual(code, 1)
        self.assertIn("conflicts", output)
        self.assertEqual(saved["author"], "gpt-6-astra")
        self.assertEqual(self.calls, [])

    def test_known_stop_and_ask_is_persisted_and_never_calls_provider(self):
        code, output, _saved = self.invoke(
            "tiebreak", "--stop-and-ask", "Changing the owner's goal")
        self.assertEqual(code, 1)
        self.assertIn("Changing the owner's goal", output)
        self.assertEqual(self.invoke()[0], 1)
        self.assertEqual(self.calls, [])

    def assert_veto_survives_author_error(self, prior, declared, error):
        reason = "Changing the owner's goal"
        for command in ("synthesize", "tiebreak"):
            for retry in ("synthesize", "tiebreak"):
                with self.subTest(command=command, retry=retry):
                    self.calls.clear()
                    committee.save_session("split", dict(self.session, author=prior))
                    code, output, refused = self.invoke(
                        command, "--stop-and-ask", reason, author=declared)
                    self.assertEqual(code, 1, output)
                    self.assertIn(error, refused["resolution"]["reason"])
                    self.assertEqual(refused["resolution"]["status"], "OWNER")
                    self.assertEqual(self.calls, [])

                    # invoke reloads the saved session; no flag is repeated.
                    code, output, retried = self.invoke(
                        retry, author=None if prior else "gpt-5.6-sol")
                    self.assertEqual(code, 1, output)
                    self.assertEqual(refused.get("stop_and_ask"), reason)
                    self.assertEqual(retried.get("stop_and_ask"), reason)
                    self.assertEqual(retried["resolution"]["status"], "OWNER")
                    self.assertIn(reason, retried["resolution"]["reason"])
                    self.assertEqual(self.calls, [])
                    self.assertEqual([d["status"] for d in retried["decisions"]],
                                     ["OWNER", "OWNER"])

    def test_stop_and_ask_survives_unknown_author_retry(self):
        self.assert_veto_survives_author_error(None, None, "author unknown")

    def test_stop_and_ask_survives_conflicting_author_retry(self):
        self.assert_veto_survives_author_error(
            "gpt-5.6-sol", "luna", "author conflicts")

    def test_model_identified_stop_and_ask_routes_owner(self):
        for command in ("synthesize", "tiebreak"):
            with self.subTest(command=command):
                decision = {"status": "OWNER", "reason": "The goal would change."}
                self.synthesis = self.ruling = decision
                code, output, saved = self.invoke(command)
                self.assertEqual(code, 1)
                self.assertIn(decision["reason"], output)
                self.assertEqual(saved["resolution"]["status"], "OWNER")

    def test_missing_mandate_refuses_without_provider(self):
        self.mandate = self.root / "absent.md"
        self.assertEqual(self.invoke("tiebreak")[0], 1)
        self.assertEqual(self.calls, [])

    def test_incomplete_reply_even_with_valid_json_routes_owner(self):
        self.tie_status = "incomplete"
        code, output, saved = self.invoke("tiebreak")
        self.assertEqual(code, 1)
        self.assertIn("truncated", output)
        self.assertEqual(saved["resolution"]["usage"]["out_tokens"], 34)

    def test_empty_and_invalid_rulings_route_owner(self):
        for value in ("", "   ", "{", "[]", '{}',
                      json.dumps(dict(self.ruling, adopts="outsider")),
                      json.dumps(dict(self.ruling, evidence="")),
                      json.dumps(dict(self.ruling, ruling="new plan"))):
            with self.subTest(value=value):
                self.tie_text = value
                self.assertEqual(self.invoke("tiebreak")[0], 1)

    def test_failure_replaces_old_persisted_success(self):
        self.assertEqual(self.invoke("tiebreak")[0], 0)
        self.tie_error = "offline"
        code, _output, saved = self.invoke("tiebreak")
        self.assertEqual(code, 1)
        self.assertEqual(saved["resolution"]["status"], "OWNER")
        self.assertNotIn("ruling", saved["resolution"])
        self.assertEqual(saved["decisions"][0]["status"], "RULING")

    def test_new_member_turn_invalidates_current_resolution(self):
        self.invoke("tiebreak")
        session = committee.load_session("split")
        members = committee.pick_members(["luna,kimi-k2.7-code"])
        with mock.patch.object(committee, "ask_member",
                               return_value=("new position", None, None)):
            committee.run_turn(members, session, "new question", 1, "ask")
        committee.save_session("split", session)
        saved = committee.load_session("split")
        self.assertNotIn("resolution", saved)
        self.assertEqual(saved["decisions"][0]["status"], "RULING")

    def test_failed_member_turn_keeps_prior_decision_and_records_errors(self):
        self.invoke("tiebreak")
        old = committee.load_session("split")["resolution"]
        argv = [str(SCRIPT), "ask", "split", "--prompt", "Clarify the evidence"]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(committee, "ask_member",
                                  return_value=(None, "offline", None)), \
                redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as stopped:
                committee.main()
        self.assertEqual(stopped.exception.code, committee.STATES["UNAVAILABLE"])
        saved = committee.load_session("split")
        self.assertNotIn("resolution", saved)
        self.assertIn(old, saved["decisions"])
        self.assertEqual(saved["turns"][-1]["label"], "ask")
        for member in saved["members"].values():
            self.assertEqual(member["errors"][-1]["error"], "offline")

    def test_interrupted_member_turn_leaves_disk_unchanged(self):
        self.invoke("tiebreak")
        before = committee.session_path("split").read_bytes()
        argv = [str(SCRIPT), "ask", "split", "--prompt", "Clarify the evidence"]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(committee, "ask_member", side_effect=KeyboardInterrupt), \
                redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                committee.main()
        self.assertEqual(committee.session_path("split").read_bytes(), before)

    def test_convergence_preserves_unified_plan_without_tiebreak(self):
        self.synthesis = {"status": "CONVERGED", "plan": "Retain stable order."}
        code, _output, saved = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(saved["resolution"]["plan"], self.synthesis["plan"])
        self.assertEqual(len(self.calls), 1)

    def test_empty_synthesis_plan_routes_owner(self):
        for value in ("", "   "):
            with self.subTest(plan=value):
                self.synthesis = {"status": "CONVERGED", "plan": value}
                code, _output, saved = self.invoke()
                self.assertEqual(code, 1)
                self.assertEqual(saved["resolution"]["status"], "OWNER")

    def test_placeholder_plan_routes_owner(self):
        for value in ("TBD", " tBd "):
            with self.subTest(plan=value):
                self.synthesis = {"status": "CONVERGED", "plan": value}
                code, _output, saved = self.invoke()
                self.assertEqual(code, 1)
                self.assertIn("placeholder plan", saved["resolution"]["reason"])

    def test_disabled_tiebreak_entry_routes_owner(self):
        reviewers = committee.R.load_reviewers()
        for reviewer in reviewers:
            if reviewer["name"] == "astra-xhigh":
                reviewer["enabled"] = False
        with mock.patch.object(committee.R, "load_reviewers", return_value=reviewers):
            code, output, _saved = self.invoke("tiebreak")
        self.assertEqual(code, 1)
        self.assertIn("unavailable", output)
        self.assertEqual(self.calls, [])

    def test_missing_final_member_reply_routes_owner(self):
        self.session["members"]["luna"]["history"][-1]["content"] = "[no reply: offline]"
        committee.save_session("split", self.session)
        code, output, _saved = self.invoke("tiebreak")
        self.assertEqual(code, 1)
        self.assertIn("no usable final position from luna", output)
        self.assertEqual(self.calls, [])

    def test_tiebreak_profile_is_separate_from_every_gate_and_committee(self):
        reviewers = committee.R.load_reviewers()
        entry = next(r for r in reviewers if r["name"] == "astra-xhigh")
        self.assertEqual(entry["profiles"], ["tiebreak"])
        self.assertEqual(entry["effort"], "xhigh")
        for profile in ("plan", "fast", "standard", "deep", "committee"):
            self.assertNotIn("astra-xhigh", [r["name"] for r in reviewers
                                            if profile in r.get("profiles", [])])
        self.assertNotIn("astra-xhigh", [m["name"] for m in committee.pick_members([])])

    def test_swarm_import_graph_excludes_committee_and_review(self):
        swarm_scripts = REPO / "skills/hanig-swarm/scripts"
        result = subprocess.run(
            [sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); "
             "import swarm; assert 'committee' not in sys.modules; "
             "assert 'review' not in sys.modules", str(swarm_scripts)],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
