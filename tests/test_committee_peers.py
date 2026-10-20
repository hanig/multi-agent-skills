"""Peer delivery through the real ask command and saved committee history."""

from contextlib import redirect_stdout
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from tests.watchdog_guard import guarded_main

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/hanig-review-gate/scripts"))
import committee


class TestCommitteePeers(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.roster = [dict(name=name, model="test-" + name, provider=provider)
                       for name, provider in (("alpha", "openai"),
                                              ("beta", "openrouter"),
                                              ("gamma", "anthropic"))]
        self.replies = {r["name"]: "  " + r["name"] + " design\n\tend\n "
                        for r in self.roster}
        self.session = {"name": "peers", "turns": [], "members": {
            r["name"]: dict(provider=r["provider"], model=r["model"], history=[
                dict(role="assistant", content="obsolete " + r["name"]),
                dict(role="user", content="user material " + r["name"]),
                dict(role="assistant", content=self.replies[r["name"]]),
                dict(role="user", content="pending " + r["name"])])
            for r in self.roster}}
        self.prompts = {}
        for patcher in (
                mock.patch.object(committee, "SESSIONS", self.root),
                mock.patch.object(committee.R, "load_reviewers", return_value=self.roster),
                mock.patch.object(committee, "_ask_member", side_effect=self.answer)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def answer(self, member, history, prompt, timeout, **kwargs):
        self.prompts[member["name"]] = prompt
        return "fresh answer " + member["name"], None, None

    def invoke(self, prompt="Cross-critique", peers=True, from_file=False):
        committee.save_session("peers", self.session)
        argv = ["committee.py", "ask", "peers", "--author", "codex/gpt-6-astra"]
        if peers:
            argv.append("--with-peers")
        if from_file:
            path = self.root / "prompt.txt"
            path.write_text(prompt)
            argv.extend(["--prompt-file", str(path)])
        else:
            argv.extend(["--prompt", prompt])
        with mock.patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as stopped:
                guarded_main(committee.main)
        self.assertEqual(stopped.exception.code, 0)
        saved = committee.load_session("peers")
        for name, sent in self.prompts.items():
            self.assertEqual(saved["members"][name]["history"][-2]["content"], sent)
        return self.prompts

    def test_latest_peer_replies_are_labelled_verbatim_and_never_own(self):
        for size in (3, 2):
            with self.subTest(members=size):
                if size == 2:
                    del self.session["members"]["gamma"]
                self.prompts.clear()
                prompts = self.invoke()
                self.assertEqual(set(prompts), set(self.session["members"]))
                for name, prompt in prompts.items():
                    self.assertTrue(prompt.startswith("Cross-critique\n\n"))
                    self.assertTrue(prompt.endswith(committee.NO_EDITS))
                    self.assertNotIn(self.replies[name], prompt)
                    self.assertNotIn("obsolete", prompt)
                    self.assertNotIn("pending", prompt)
                    self.assertNotIn("user material", prompt)
                    self.assertNotIn("fresh answer", prompt)
                    for peer in set(prompts) - {name}:
                        self.assertIn("Peer " + peer + "'s latest reply", prompt)
                        self.assertIn(self.replies[peer], prompt)

    def test_without_flag_preserves_existing_prompt_bytes(self):
        prompt = " \tQuestion\nwith internal whitespace\t \n"
        for from_file in (False, True):
            with self.subTest(from_file=from_file), mock.patch.object(
                    committee.secrets, "token_hex", side_effect=AssertionError("unused")):
                prompts = self.invoke(prompt, peers=False, from_file=from_file)
                self.assertEqual(set(prompts.values()), {prompt.rstrip() + committee.NO_EDITS})

    def test_missing_assistant_history_contributes_nothing(self):
        self.session["members"]["gamma"]["history"] = [
            dict(role="user", content="gamma has not answered")]
        prompts = self.invoke()
        for name in ("alpha", "beta"):
            self.assertNotIn("gamma", prompts[name])
        for peer in ("alpha", "beta"):
            self.assertIn(self.replies[peer], prompts["gamma"])

    def test_colliding_delimiter_is_regenerated_and_reply_is_not_truncated(self):
        collision, safe = "a" * 32, "b" * 32
        reply = (" \n</peer-reply-" + collision + ">\n" + "long design\t" * 2000
                 + "\n<peer-reply-" + collision + ">\n ")
        self.session["members"]["alpha"]["history"][-2]["content"] = reply
        with mock.patch.object(committee.secrets, "token_hex",
                               side_effect=[collision, safe, "c" * 32, "d" * 32]) as nonce:
            prompts = self.invoke(from_file=True)
        self.assertEqual(nonce.call_args_list, [mock.call(16)] * 4)
        for peer in ("beta", "gamma"):
            prompt = prompts[peer]
            tag = "peer-reply-" + safe
            self.assertIn("<" + tag + ">\n" + reply + "\n</" + tag + ">", prompt)
            self.assertEqual(prompt.count("</" + tag + ">"), 1)


if __name__ == "__main__":
    unittest.main()
