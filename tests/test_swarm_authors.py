"""Potential contributor metadata survives retries without gaining authority."""
import copy
import shlex
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/hanig-swarm/scripts"))
import swarm as S
import swarm_authors as SA
from tests.test_attempt_worktrees import paseo_resolvable
from tests.watchdog_guard import guarded_main


def record(unit="ancestor", attempt="a0", identity="provider/model"):
    return dict(unit=unit, attempt=attempt, identity=identity)


class AuthorHistoryTests(unittest.TestCase):
    def test_resolution_matches_protocol(self):
        for intent, expected in (
            ({"provider": "provider/embedded"}, "provider/embedded"),
            ({"provider": "provider/embedded", "model": "override"}, "provider/override"),
            ({"provider": "provider", "model": "vendor/nested"}, "provider/vendor/nested"),
            ({"provider": "provider"}, None),
            ({}, None), ({"provider": 7, "model": "model"}, None),
            ({"provider": "/model"}, None),
            ({"provider": "provider/"}, None),
            ({"provider": "provider/embedded", "model": []}, "provider/embedded"),
            ({"provider": "provider/embedded", "model": ["bad"]}, None),
        ):
            with self.subTest(intent=intent):
                self.assertEqual(SA.intent_identity(intent), expected)

    def test_order_dedup_legacy_and_excluded_attempt(self):
        carried = record()
        unknown = record(identity=None)
        unit = {"id": "work", "author_history": [carried, unknown, carried]}
        intents = {"a2": {"schema_version": 6, "provider": "two/model",
                          "author_history": [unknown, record("other")]},
                   "a1": {"schema_version": 5, "provider": "one/model",
                          "author_history": [record("ignored-legacy")]},
                   "a3": {"schema_version": 6, "provider": "current/model"}}
        expected = [carried, unknown, record("work", "a1", "one/model"),
                    record("other"), record("work", "a2", "two/model")]
        before = copy.deepcopy((unit, intents))
        for mapping in (intents, dict(reversed(list(intents.items())))):
            actual = SA.collect_author_history(
                unit, {"attempt_launch_intents": mapping}, "a3")
            self.assertEqual(actual, expected)
            actual[0]["identity"] = "changed/copy"
            self.assertEqual((unit, intents), before)
        for schema in range(1, 9):
            intent = {"schema_version": schema, "author_history": [carried]}
            actual = SA.collect_author_history(
                {"id": "work"}, {"attempt_launch_intents": {"a1": intent}})
            self.assertEqual(actual, ([carried] if schema >= 6 else []) +
                             [record("work", "a1", None)])

    def unit(self, history):
        return {"id": "work", "kind": "code", "repo": "/unused",
                "target_branch": "main", "mode": "full-access", "prompt": "work",
                "outputs": ["evidence.md"], "author_history": history}

    def test_validate_refuses_every_malformed_shape(self):
        invalid = ["provider/model", {}, None, 1, True, ["provider/model"],
                   [None], [1], [[]], [dict(record(), extra="bad")]]
        for key in ("unit", "attempt", "identity"):
            missing = record()
            del missing[key]
            invalid.append([missing])
        for key in ("unit", "attempt"):
            invalid.extend([[dict(record(), **{key: value})]
                            for value in (None, "", 1, True, [], {})])
        invalid.extend([[record(identity=value)] for value in
                        ("", "provider", "provider/", "/model", 1, True, [], {})])
        for value in invalid:
            with self.subTest(history=value), self.assertRaisesRegex(
                    S.PlanError, "unit 'work': author_history"):
                S.validate_plan({"units": [self.unit(value)]})
        with self.assertRaisesRegex(S.PlanError, "author_history.*code"):
            S.validate_plan({"units": [dict(self.unit([]), kind="pipeline")]})

    def test_valid_metadata_is_cosmetic_and_does_not_rewrite_plan(self):
        unit = self.unit([record(), record(identity=None), record(identity="p/vendor/m")])
        before = copy.deepcopy(unit)
        with paseo_resolvable():
            S.validate_plan({"units": [unit]})
        self.assertEqual(unit, before)
        plain = dict(unit)
        del plain["author_history"]
        self.assertEqual(S.plan_digest({"units": [unit]}),
                         S.plan_digest({"units": [plain]}))
        intent = {"repo": "/unused", "branch": "swarm-a1", "base_commit": "a" * 40,
                  "target_branch": "main"}
        old_instruction = (
            "The coordinator-recorded provider/model for this unit is "
            "UNKNOWN. Report the missing identity to the coordinator before "
            "review; do not invent an author or omit --author. "
            "The recorded lineage is incomplete: unit 'UNKNOWN', attempt 'UNKNOWN'. "
            "Report these unknown identities to the coordinator before review; "
            "do not guess.")
        new_instruction = (
            "The coordinator-recorded provider/model for this unit is "
            "UNKNOWN. Report the missing identity to the coordinator before "
            "review; do not invent an author or omit --author. "
            "Pass --author provider/model --author p/vendor/m on every review.py "
            "or committee.py run so the gate excludes all known contributing models. "
            "The recorded lineage is incomplete: unit 'UNKNOWN', attempt 'UNKNOWN'; "
            "unit 'ancestor', attempt 'a0'. Report these unknown identities to the "
            "coordinator before review; do not guess.")
        plain_prompt = S._dispatch_prompt(plain, intent)
        self.assertIn(old_instruction, plain_prompt)
        self.assertEqual(S._dispatch_prompt(unit, dict(intent, author_history=unit["author_history"])),
                         plain_prompt.replace(old_instruction, new_instruction))
        self.assertEqual(S.declared_seed(unit), S.declared_seed(plain))
        self.assertEqual(S.declared_scope(unit), S.declared_scope(plain))


class AuthorInstructionTests(unittest.TestCase):
    def intent(self, **extra):
        return dict(repo="/unused", branch="swarm-a3", base_commit="a" * 40,
                    target_branch="main", unit_id="work", attempt_id="a3", **extra)

    def flags(self, intent):
        prompt = S._code_completion_protocol(intent)
        if "Pass --author " not in prompt:
            return []
        return shlex.split(prompt.split("Pass ", 1)[1].split(" on every ", 1)[0])

    def test_distinct_authors_current_first_in_history_order(self):
        history = [record(identity="p/second"), record(identity="p/override"),
                   record(identity="q/vendor/third"), record(identity="p/second")]
        for schema in range(1, 8):
            intent = self.intent(schema_version=schema, provider="p/embedded",
                                 model="override", author_history=history)
            before = copy.deepcopy(intent)
            self.assertEqual(self.flags(intent), ["--author", "p/override",
                "--author", "p/second", "--author", "q/vendor/third"])
            self.assertEqual(intent, before)

    def test_unknown_current_keeps_inherited_flags_and_names_current_attempt(self):
        intent = self.intent(author_history=[record(identity="p/ancestor")])
        self.assertEqual(self.flags(intent), ["--author", "p/ancestor"])
        prompt = S._code_completion_protocol(intent)
        self.assertIn("provider/model for this unit is UNKNOWN", prompt)
        self.assertIn("The recorded lineage is incomplete: unit 'work', attempt 'a3'.", prompt)
        self.assertIn("Report these unknown identities to the coordinator before review; "
                      "do not guess.", prompt)
        self.assertNotIn("excludes this unit's own model", prompt)

    def test_unknown_history_is_named_even_with_known_current(self):
        intent = self.intent(provider="p/current", author_history=[
            record(identity=None), record("another", "b2", None)])
        self.assertEqual(self.flags(intent), ["--author", "p/current"])
        prompt = S._code_completion_protocol(intent)
        self.assertIn("The recorded lineage is incomplete: unit 'ancestor', attempt 'a0'; "
                      "unit 'another', attempt 'b2'.", prompt)
        self.assertIn("Report these unknown identities to the coordinator before review; "
                      "do not guess.", prompt)

    def test_nulls_never_render_as_flags(self):
        for provider in (None, "p/current"):
            intent = self.intent(provider=provider, author_history=[record(identity=None)])
            self.assertEqual(self.flags(intent),
                             ["--author", provider] if provider else [])
            prompt = S._code_completion_protocol(intent)
            self.assertNotIn("--author None", prompt)
            self.assertNotIn("--author UNKNOWN", prompt)

    def test_singleton_sentence_is_exact(self):
        expected = ("Pass --author p/current on every review.py or committee.py run "
                    "so the gate excludes this unit's own model.")
        for extra in ({}, {"author_history": []},
                      {"author_history": [record(identity="p/current")]}):
            self.assertEqual(SA.author_instruction(self.intent(provider="p/current", **extra)),
                             expected)

    def test_each_identity_is_shell_quoted(self):
        identities = ["p/model'with space", "q/model;$(false)"]
        intent = self.intent(provider=identities[0],
                             author_history=[record(identity=identities[1])])
        self.assertEqual(self.flags(intent), ["--author", identities[0],
                                             "--author", identities[1]])
        instruction = SA.author_instruction(intent)
        for identity in identities:
            self.assertIn("--author " + shlex.quote(identity), instruction)

    def test_synthetic_validation_never_borrows_plan_history(self):
        unit = AuthorHistoryTests().unit([record(identity="p/ancestor")])
        unit.update(provider="p/current")
        with paseo_resolvable(), mock.patch.object(
                S.SCP, "_dispatch_prompt", wraps=S.SCP._dispatch_prompt) as build:
            S.validate_plan({"units": [unit]})
        self.assertTrue(build.call_args_list)
        for call in build.call_args_list:
            plan_unit, intent = call.args
            self.assertEqual(plan_unit["author_history"], unit["author_history"])
            self.assertIsNone(SA.intent_identity(intent))
            prompt = S._dispatch_prompt(plan_unit, intent)
            self.assertIn("provider/model for this unit is UNKNOWN", prompt)
            self.assertNotIn("Pass --author", prompt)

    def test_rendered_flags_reach_real_parsers_and_committee_binding(self):
        # Stop committee.main at its command handler: parse the real CLI,
        # with no provider calls, review worker or journal writes.
        with mock.patch.object(sys, "path", [str(ROOT / "skills/hanig-review-gate/scripts")] + sys.path):
            import review
            import committee
        authors = ["p/current", "q/vendor/prior", "r/old"]
        intent = self.intent(provider=authors[0], author_history=[
            record(identity=identity) for identity in authors[1:]])
        flags = self.flags(intent)
        self.assertEqual(review.build_parser().parse_args(flags).author, authors)
        session = {}
        for command, handler in (("open", "cmd_open"), ("ask", "cmd_ask")):
            with mock.patch.object(sys, "argv", ["committee.py", command, "fixture"] + flags), \
                    mock.patch.object(committee, handler, return_value=0) as run:
                with self.assertRaises(SystemExit) as stopped:
                    guarded_main(committee.main)
            self.assertEqual(stopped.exception.code, 0)
            parsed = run.call_args.args[0].author
            self.assertEqual(parsed, authors)
            self.assertIsNone(committee.bind_authors(session, parsed))
            self.assertEqual(session["author"], authors)
        self.assertIsNotNone(committee.bind_authors(session, authors[:1]))


if __name__ == "__main__":
    unittest.main()
