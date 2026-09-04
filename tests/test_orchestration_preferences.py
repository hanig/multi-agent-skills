#!/usr/bin/env python3
"""The shipped reference `orchestration-preferences.json`.

This repo ships eight skills that each refuse to choose a provider until they
have read `~/.paseo/orchestration-preferences.json`, and shipped no example of
that file. `examples/orchestration-preferences.json` is that example. No
Python in this repo reads it -- its only consumer is a human or an agent
copying it to `~/.paseo/` -- so nothing else would notice if it rotted.

A malformed reference file is worse than none: it is copied into place and
then every Paseo skill resolves a role to a key that is not there. So these
tests hold it to the contract in `skills/paseo/SKILL.md`, and they read that
contract out of the skill rather than restating it, so a vendored-skill update
that renames a category fails here instead of silently disagreeing.

Python 3.8+, stdlib only.
"""
import json
import os
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "examples" / "orchestration-preferences.json"
PASEO_SKILL = ROOT / "skills" / "paseo" / "SKILL.md"
MODELS = ROOT / "models.json"
LIVE_NAME = "orchestration-preferences.json"
LIVE_PATH = "~/.paseo/" + LIVE_NAME


def skill_categories():
    """The categories the paseo skill declares, from the skill itself."""
    text = PASEO_SKILL.read_text()
    line = re.search(r"^Categories: (.+)$", text, re.M)
    assert line, "skills/paseo/SKILL.md no longer declares a Categories line"
    return set(re.findall(r"`([a-z]+)`", line.group(1)))


def attested_provider_strings():
    """Provider strings this repo can vouch for, from the two places that
    carry real ones: the examples in the paseo skill's Models section, and the
    ids in models.json (which are `<Provider>/<Model>`, per the docstring on
    `match_observed` in bin/bus).

    Deliberately narrow. The point of the check that uses this is to catch a
    plausible-looking invented string, which a loose harvest of every
    slash-containing token in the skill would wave through.
    """
    text = PASEO_SKILL.read_text()
    section = re.search(r"^## Models$(.+?)^## ", text, re.M | re.S)
    assert section, "skills/paseo/SKILL.md no longer has a Models section"
    strings = set(re.findall(r"`([a-z]+/[a-z0-9.\-]+)`", section.group(1)))
    strings |= {m["id"] for m in json.loads(MODELS.read_text())["models"]}
    return strings


class TestItParses(unittest.TestCase):

    def test_the_template_exists(self):
        self.assertTrue(TEMPLATE.is_file(),
                        "the skills require a file this repo does not ship")

    def test_it_is_json(self):
        json.loads(TEMPLATE.read_text())


class TestTheContract(unittest.TestCase):
    """`providers` and `preferences`, exactly as skills/paseo/SKILL.md
    describes them. A skill resolves a role by indexing `providers` with a
    category name; a missing key is a KeyError at dispatch time, and an extra
    one is a routing decision nothing will ever read."""

    def setUp(self):
        self.cfg = json.loads(TEMPLATE.read_text())

    def test_the_categories_are_exactly_the_skills(self):
        self.assertEqual(set(self.cfg["providers"]), skill_categories())

    def test_the_skill_still_declares_the_five_we_wrote_this_against(self):
        """Pins the other end. If a vendored update adds a sixth category,
        the assertion above would go on passing only by accident."""
        self.assertEqual(skill_categories(),
                         {"impl", "ui", "research", "planning", "audit"})

    def test_every_category_routes_somewhere(self):
        for category, provider in self.cfg["providers"].items():
            self.assertIsInstance(provider, str, category)
            self.assertTrue(provider.strip(), category)

    def test_no_provider_string_is_invented(self):
        """A plausible-looking wrong provider string is worse than a marked
        gap: it dispatches, or fails deep inside the daemon, on a machine the
        author of this file could not test against."""
        attested = attested_provider_strings()
        for category, provider in self.cfg["providers"].items():
            self.assertIn(provider, attested,
                          "%s routes to a string no skill or models.json "
                          "entry attests" % category)

    def test_preferences_is_a_list_of_prompt_ready_strings(self):
        prefs = self.cfg["preferences"]
        self.assertIsInstance(prefs, list)
        self.assertTrue(prefs)
        for pref in prefs:
            self.assertIsInstance(pref, str)
            self.assertTrue(pref.strip())


class TestTheRoutingDecisionItEncodes(unittest.TestCase):
    """docs/plan-field-reports.md, 'Model routing, set 2026-09-01'."""

    def setUp(self):
        self.providers = json.loads(TEMPLATE.read_text())["providers"]

    def test_the_author_does_not_review_itself(self):
        """Verbatim from the decision: sol coordinates and integrates, and
        does NOT review, because an author reviewing itself is what the roster
        exists to prevent. The example in skills/paseo/SKILL.md routes `audit`
        to the same provider as `impl`; copying that example into place would
        reinstate exactly the thing the decision removed."""
        self.assertNotEqual(self.providers["audit"], self.providers["impl"])

    def test_the_committee_seats_are_not_the_author_either(self):
        """paseo-committee picks its members from the planning and research
        categories, and its Phase 3 sends those members the implementation
        diff to review. So those two seats are review seats in disguise, and
        the no-self-review rule reaches them as well."""
        self.assertNotEqual(self.providers["planning"], self.providers["impl"])
        self.assertNotEqual(self.providers["research"], self.providers["impl"])

    def test_every_category_records_why(self):
        """Three of the five are judgement calls. The file has to say which,
        rather than presenting a guess as settled."""
        cfg = json.loads(TEMPLATE.read_text())
        self.assertEqual(set(cfg["_judgement_calls"]), set(self.providers))


class TestItIsObviouslyNotTheLiveFile(unittest.TestCase):

    def test_it_says_so_and_says_where_the_live_file_is(self):
        cfg = json.loads(TEMPLATE.read_text())
        self.assertIn(LIVE_PATH, cfg["_comment"])

    def test_the_repo_ships_no_second_copy(self):
        """One template, in one place, under examples/. A copy at the repo
        root or inside a skill directory reads as live configuration, and the
        two would drift."""
        found = []
        for base, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
            if LIVE_NAME in files:
                found.append(str(Path(base, LIVE_NAME).relative_to(ROOT)))
        self.assertEqual(found, ["examples/" + LIVE_NAME])

    def test_the_readme_tells_you_to_copy_it(self):
        """Shipping the file without the host-side step is the same failure
        as not shipping it: nobody knows it has to be somewhere else."""
        readme = (ROOT / "README.md").read_text()
        self.assertIn("examples/" + LIVE_NAME, readme)
        self.assertIn(LIVE_PATH, readme)


if __name__ == "__main__":
    unittest.main()
