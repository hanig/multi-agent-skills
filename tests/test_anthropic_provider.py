"""Native Anthropic transport regressions; every provider call is offline."""

import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/hanig-review-gate/scripts"))
import committee as C
import resolve_models as RM
import review as R


class TestAnthropicProvider(unittest.TestCase):
    SEAT = {"name": "native", "provider": "anthropic", "model": "claude-test-5-5",
            "max_output_tokens": 8192}
    KEY = 'sk-ant-fixture-"quoted\\value'

    def setUp(self):
        env = mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": self.KEY,
                                          "HANIG_ROUTING_SNAPSHOTS": "off"})
        env.start()
        self.addCleanup(env.stop)
        # Any test that forgets its fake transport must fail before network I/O.
        transport = mock.patch.object(R.urllib.request, "urlopen",
                                      side_effect=AssertionError("network forbidden"))
        self.urlopen = transport.start()
        self.addCleanup(transport.stop)

    def response(self, **extra):
        data = {"content": [{"type": "thinking", "thinking": "private"},
                            {"type": "text", "text": "first"},
                            {"type": "text", "text": " second"}],
                "usage": {"input_tokens": 17, "output_tokens": 29,
                          "reasoning_tokens": 11},
                "stop_reason": "end_turn", "model": self.SEAT["model"],
                "id": "msg_fixture"}
        data.update(extra)
        return data

    def fake_reply(self, data):
        self.urlopen.side_effect = lambda *a, **k: io.BytesIO(json.dumps(data).encode())

    def assert_native_headers(self, request):
        headers = {name.lower(): value for name, value in request.header_items()}
        self.assertEqual(headers, {"x-api-key": self.KEY,
                                   "anthropic-version": "2023-06-01",
                                   "content-type": "application/json"})
        self.assertNotIn("authorization", headers)

    def test_request_headers_shape_and_response(self):
        self.fake_reply(self.response())
        result, error = R.PROVIDERS["anthropic"](self.SEAT, "the prompt", 10)
        self.assertIsNone(error)
        request = self.urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(request.get_method(), "POST")
        self.assert_native_headers(request)
        self.assertEqual(json.loads(request.data), {
            "model": self.SEAT["model"], "max_tokens": 8192, "system": R.SYSTEM,
            "messages": [{"role": "user", "content": "the prompt"}]})
        self.assertEqual(result, {"text": "first second", "in_tokens": 17,
                                 "out_tokens": 29, "reasoning_tokens": 11,
                                 "served_model": self.SEAT["model"],
                                 "status": "completed", "response_id": "msg_fixture"})

    def test_declared_effort_only_and_default_budget(self):
        for effort in (None, "low", "medium", "high", "xhigh", "max"):
            with self.subTest(effort=effort):
                seat = dict(self.SEAT, effort=effort)
                del seat["max_output_tokens"]
                self.fake_reply(self.response())
                result, error = R.call_anthropic(seat, "p", 10)
                self.assertIsNone(error)
                payload = json.loads(self.urlopen.call_args.args[0].data)
                self.assertEqual(payload["max_tokens"], R.DEFAULT_MAX_OUTPUT_TOKENS)
                if effort:
                    self.assertEqual(payload["thinking"], {"type": "adaptive"})
                    self.assertEqual(payload["output_config"], {"effort": effort})
                else:
                    self.assertNotIn("thinking", payload)
                    self.assertNotIn("output_config", payload)
                self.assertNotIn("reasoning", payload)

    def test_deadline_passes_through(self):
        with mock.patch.object(R, "_post", return_value=(self.response(), None)) as post:
            R.call_anthropic(self.SEAT, "p", 15, deadline=12345)
        self.assertEqual(post.call_args.kwargs["deadline"], 12345)
        self.assertEqual(post.call_args.args[3], 15)

    def test_missing_key_is_unavailable_at_all_entrypoints(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            expected = "ANTHROPIC_API_KEY not set"
            self.assertEqual(R.call_anthropic(self.SEAT, "p", 1), (None, expected))
            self.assertEqual(R.availability(self.SEAT), expected)
            self.assertEqual(R.probe_liveness(self.SEAT), expected)
            answer = R.run_one(self.SEAT, "p", 1)
            self.assertFalse(answer["ok"])
            self.assertEqual(answer["error"], expected)
            self.assertEqual(C.ask_member(self.SEAT, [], "p", 1), (None, expected, None))
            with self.assertRaisesRegex(RuntimeError, "^ANTHROPIC_API_KEY not set$"):
                RM.list_ids("anthropic", 1)
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(R.cmd_list([self.SEAT]), R.STATES["REVIEW_UNAVAILABLE"])
            self.assertIn(expected, out.getvalue())
        self.urlopen.assert_not_called()

    def test_stop_reasons_and_absent_usage_remain_unknown(self):
        for stop in ("max_tokens", "stop_sequence", "refusal", "pause_turn", None):
            with self.subTest(stop=stop):
                self.fake_reply(self.response(stop_reason=stop, usage={}))
                result, error = R.call_anthropic(self.SEAT, "p", 1)
                self.assertIsNone(error)
                self.assertEqual(result["status"], stop)
                for field in ("in_tokens", "out_tokens", "reasoning_tokens"):
                    self.assertIsNone(result[field])
        self.fake_reply(self.response(usage={"output_tokens_details": {"reasoning_tokens": 3}}))
        self.assertEqual(R.call_anthropic(self.SEAT, "p", 1)[0]["reasoning_tokens"], 3)
        self.fake_reply(self.response(usage={"output_tokens_details": {"thinking_tokens": 7}}))
        self.assertEqual(R.call_anthropic(self.SEAT, "p", 1)[0]["reasoning_tokens"], 7)

    def test_empty_budget_exhaustion_uses_shared_classifier(self):
        self.fake_reply(self.response(content=[{"type": "thinking", "thinking": "hidden"}],
                                      stop_reason="max_tokens"))
        result, error = R.call_anthropic(self.SEAT, "p", 1)
        self.assertIsNone(result)
        self.assertIn("whole output budget", error)
        self.assertIn("29 tokens", error)
        self.assertIn("11 of them reasoning", error)

    def assert_scrubbed(self, value):
        rendered = json.dumps(value)
        for form in (self.KEY, json.dumps(self.KEY)[1:-1], repr(self.KEY).strip("'\"")):
            self.assertNotIn(form, rendered)
        self.assertIn("ANTHROPIC_API_KEY redacted", rendered)

    def test_errors_scrub_headers_shapes_and_empty_metadata_before_rendering(self):
        malformed = [self.KEY, {"content": None, "echo": self.KEY},
                     self.response(content=[{"type": "text", "text": {"echo": self.KEY}}]),
                     self.response(usage={"output_tokens_details": self.KEY})]
        for data in malformed:
            with self.subTest(data=data):
                self.fake_reply(data)
                result, error = R.call_anthropic(self.SEAT, "p", 1)
                self.assertIsNone(result)
                self.assertNotIn(self.KEY, error)
        self.fake_reply(self.response(content=[], stop_reason=self.KEY))
        self.assert_scrubbed(R.call_anthropic(self.SEAT, "p", 1)[1])
        for thrown in (ValueError("Invalid header " + repr(self.KEY)),
                       urllib.error.HTTPError("https://api.anthropic.com/v1/messages",
                                              401, "Unauthorized", {},
                                              io.BytesIO(self.KEY.encode()))):
            self.urlopen.side_effect = thrown
            with mock.patch.object(R.time, "sleep"):
                self.assert_scrubbed(R.call_anthropic(self.SEAT, "p", 1)[1])
        with mock.patch.object(R, "_post", side_effect=ValueError(self.KEY)):
            self.assert_scrubbed(R.call_anthropic(self.SEAT, "p", 1)[1])

    def test_provider_error_is_scrubbed_in_gate_journal_and_committee_output(self):
        with mock.patch.object(R, "_post", return_value=(None, "error " + self.KEY)):
            result = R.run_one(self.SEAT, "p", 1)
            self.assert_scrubbed(result)
            with tempfile.TemporaryDirectory() as tmp:
                _, path = R.append_review_journal(
                    Path(tmp).resolve() / "journal.jsonl", "implementation", 1,
                    [self.SEAT["name"]], "REVIEW_UNAVAILABLE", [], results=[result])
                self.assert_scrubbed(json.loads(path.read_text()))
            session = {"members": {"native": {"history": []}}, "turns": []}
            replies = C.run_turn([self.SEAT], session, "p", 1, "plan")
            out = io.StringIO()
            with redirect_stdout(out):
                C.show_results(replies)
            self.assert_scrubbed(out.getvalue())
            self.assert_scrubbed(session)

    def test_committee_preserves_history_system_usage_and_completion(self):
        history = [{"role": "user", "content": "old question"},
                   {"role": "assistant", "content": "old answer"}]
        for system in (None, "synthesis instructions"):
            self.fake_reply(self.response())
            text, error, usage = C.ask_member(self.SEAT, history, "next", 10, system=system)
            self.assertEqual((text, error, usage),
                             ("first second", None, {"in_tokens": 17, "out_tokens": 29}))
            request = self.urlopen.call_args.args[0]
            self.assertEqual(request.full_url, "https://api.anthropic.com/v1/messages")
            self.assert_native_headers(request)
            payload = json.loads(request.data)
            self.assertEqual(payload["messages"], history + [{"role": "user", "content": "next"}])
            self.assertEqual(payload["system"], system or C.SYSTEM_PLANNER)
        self.fake_reply(self.response(stop_reason="max_tokens"))
        text, error, usage = C.ask_member(self.SEAT, history, "next", 10, require_complete=True)
        self.assertIsNone(text)
        self.assertIn("incomplete/truncated Anthropic", error)
        self.assertEqual(usage, {"in_tokens": 17, "out_tokens": 29})

    def test_resolver_listing_headers_pagination_and_error_redaction(self):
        pages = [{"data": [{"id": "claude-test-5-6"}], "has_more": True,
                  "last_id": "cursor/+"}, {"data": [{"id": "claude-test-5-7"}], "has_more": False}]
        self.urlopen.side_effect = [io.BytesIO(json.dumps(p).encode()) for p in pages]
        self.assertEqual(RM.list_ids("anthropic", 4), ["claude-test-5-6", "claude-test-5-7"])
        for call in self.urlopen.call_args_list:
            self.assert_native_headers(call.args[0])
            self.assertEqual(call.args[0].get_method(), "GET")
        self.assertEqual(self.urlopen.call_args_list[0].args[0].full_url,
                         "https://api.anthropic.com/v1/models")
        self.assertEqual(self.urlopen.call_args_list[1].args[0].full_url,
                         "https://api.anthropic.com/v1/models?after_id=cursor%2F%2B")
        self.urlopen.side_effect = ValueError("bad header " + repr(self.KEY))
        with self.assertRaises(RuntimeError) as raised:
            RM.list_ids("anthropic", 1)
        self.assert_scrubbed(str(raised.exception))

    def test_listing_refuses_repeated_or_missing_cursor(self):
        for cursor in (None, "same"):
            self.fake_reply({"data": [], "has_more": True, "last_id": cursor})
            with self.assertRaisesRegex(RuntimeError, "invalid page cursor"):
                RM.list_ids("anthropic", 1)

    def test_resolver_probe_uses_native_messages(self):
        verdict = {"verdict": "upheld", "findings": [], "claims": [
            {"claim_index": 1, "status": "supported", "reason": "prose"}]}
        self.fake_reply(self.response(content=[{"type": "text", "text": json.dumps(verdict)}]))
        record, error = RM.probe_reviewer(self.SEAT, self.SEAT["model"], 10)
        self.assertIsNone(error)
        self.assertEqual(record["provider"], "anthropic")
        self.assertEqual(record["outcome"], "completed")
        request = self.urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.anthropic.com/v1/messages")
        self.assert_native_headers(request)


if __name__ == "__main__":
    unittest.main()
