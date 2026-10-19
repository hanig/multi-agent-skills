"""Potential contributor metadata survives retries without gaining authority."""
import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/hanig-swarm/scripts"))
import swarm as S
import swarm_authors as SA
from tests.test_attempt_worktrees import paseo_resolvable


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
        self.assertEqual(S._dispatch_prompt(unit), S._dispatch_prompt(plain))
        self.assertEqual(S.declared_seed(unit), S.declared_seed(plain))
        self.assertEqual(S.declared_scope(unit), S.declared_scope(plain))


if __name__ == "__main__":
    unittest.main()
