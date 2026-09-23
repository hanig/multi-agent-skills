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
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "examples" / "orchestration-preferences.json"
PASEO_SKILL = ROOT / "skills" / "paseo" / "SKILL.md"
MODELS = ROOT / "models.json"
BUS = ROOT / "bin" / "bus"
PLAN = ROOT / "docs" / "plan-field-reports.md"
README = ROOT / "README.md"
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


def preferences_section():
    """The 'Orchestration preferences' section of the paseo skill, alone.

    Scoped on purpose: `thinkingOptionId` is a real key elsewhere in that
    skill (on `create_agent`/`update_agent` `settings`), and the claim under
    test is only that the PREFERENCES FILE has no slot for it."""
    text = PASEO_SKILL.read_text()
    section = re.search(r"^## Orchestration preferences$(.+?)^## ", text,
                        re.M | re.S)
    assert section, "skills/paseo/SKILL.md no longer has that section"
    return section.group(1)


def bus_help(*argv):
    """`bin/bus ... --help`, with the bus root redirected at a throwaway dir.

    Importing or running bin/bus mkdirs its root at module scope, so it never
    gets to touch the real ~/.agent-bus from a test run."""
    with tempfile.TemporaryDirectory() as home:
        out = subprocess.run(
            [sys.executable, str(BUS), *argv, "--help"],
            capture_output=True, text=True, timeout=60,
            env=dict(os.environ, AGENT_BUS_HOME=home),
        )
    return out.stdout + out.stderr


def bus_call(body):
    """Load bin/bus in a CHILD interpreter and run `body` against it.

    A child rather than an import: bin/bus calls `os.umask(0o077)` and mkdirs
    at module scope, and a umask change leaks into every other test in the
    process. `body` prints one JSON object; this returns it."""
    script = (
        "import importlib.machinery, importlib.util, argparse, json\n"
        "loader = importlib.machinery.SourceFileLoader('busmod', %r)\n"
        "spec = importlib.util.spec_from_loader('busmod', loader)\n"
        "bus = importlib.util.module_from_spec(spec)\n"
        "loader.exec_module(bus)\n" % str(BUS)
    ) + body
    with tempfile.TemporaryDirectory() as home:
        out = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=60,
            env=dict(os.environ, AGENT_BUS_HOME=home),
        )
    assert out.returncode == 0, "bin/bus probe failed: " + (out.stderr or "")[-2000:]
    return json.loads(out.stdout)


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


class TestEffortIsNotConfigurableHere(unittest.TestCase):
    """ARC-263. `providers` is a provider string per category and effort is a
    separate axis (`paseo run --thinking <id>` / `settings.thinkingOptionId`),
    so this file cannot set effort and must not read as though it does.

    These pin the negative half of the claim. If a vendored update grows an
    effort slot, the disclaimers in the template, `README.md` and
    `docs/plan-field-reports.md` become wrong, and this is where that surfaces
    -- the same reason the category names are read out of the skill rather
    than restated here."""

    def test_the_skill_declares_only_providers_and_preferences(self):
        example = re.search(r"```json\n(.+?)```", preferences_section(), re.S)
        self.assertTrue(example, "the section no longer shows a JSON example")
        self.assertEqual(set(json.loads(example.group(1))),
                         {"providers", "preferences"})

    def test_the_section_names_no_effort_key(self):
        self.assertNotIn("thinking", preferences_section().lower())

    def test_the_template_says_it_cannot_set_effort(self):
        cfg = json.loads(TEMPLATE.read_text())
        self.assertIn("CANNOT set it", " ".join(cfg["preferences"]))

    def test_the_template_names_the_consequence_not_just_the_gap(self):
        """A gap stated in the abstract gets read as harmless. The specific
        consequence -- claude/opus dispatched from `ui`/`planning` runs at
        `auto`, below the intended `high` -- is the part that stops someone
        trusting the file to control effort."""
        cfg = json.loads(TEMPLATE.read_text())
        said = (" ".join(cfg["preferences"]) + " " +
                " ".join(cfg["_unmapped"].values())).lower()
        for fragment in ("auto", "claude/opus", "below"):
            self.assertIn(fragment, said)


class TestEffortIsVerifiableAtLaunch(unittest.TestCase):
    """The other half: what cannot be configured can still be asserted.
    `bin/bus` compares `paseo inspect`'s `Thinking` against an expectation, so
    the docs point at verification instead of at a schema change. A
    verification path that stops existing, or stops being named, is the
    ARC-249 defect class -- so both ends are pinned."""

    def test_the_launch_contract_compares_the_thinking_id(self):
        got = bus_call(
            "want = argparse.Namespace(expect_cwd=None, expect_branch=None,\n"
            "                          expect_model=None, expect_provider=None,\n"
            "                          expect_thinking='high')\n"
            "print(json.dumps({\n"
            "    'matched': bus.launch_contract_issues({'Cwd': '', 'Thinking': 'high'}, want),\n"
            "    'mismatched': bus.launch_contract_issues({'Cwd': '', 'Thinking': 'auto'}, want),\n"
            "}))\n"
        )
        self.assertEqual(got["matched"], [])
        self.assertEqual(len(got["mismatched"]), 1)
        self.assertIn("thinking=auto", got["mismatched"][0])
        self.assertIn("expected high", got["mismatched"][0])

    def test_an_uninspectable_thinking_id_fails_closed(self):
        """`paseo inspect` not reporting `Thinking` must read as unmet, not as
        satisfied. Measured 2026-09-04: it does report it, next to `Provider`
        and `Model`. If a Paseo upgrade drops the field, this is the check
        that has to notice rather than start passing everything."""
        got = bus_call(
            "want = argparse.Namespace(expect_cwd=None, expect_branch=None,\n"
            "                          expect_model=None, expect_provider=None,\n"
            "                          expect_thinking='high')\n"
            "print(json.dumps({'absent': bus.launch_contract_issues({'Cwd': ''}, want)}))\n"
        )
        self.assertEqual(len(got["absent"]), 1)
        self.assertIn("(missing)", got["absent"][0])

    def test_await_still_takes_expect_thinking(self):
        self.assertIn("--expect-thinking", bus_help("await"))

    def test_launch_worker_will_not_launch_without_an_effort(self):
        """Not merely accepted: required. A worker launched through it cannot
        silently take the provider default."""
        usage = " ".join(bus_help("launch-worker").split())
        self.assertIn("--thinking THINKING", usage)
        self.assertNotIn("[--thinking", usage)

    def test_the_docs_name_the_mechanism(self):
        """ARC-249 was four sites routing work through a `bus await` nothing
        called. A verification path nobody is told about is the same defect,
        so the three files that discuss effort have to point at it."""
        for path in (TEMPLATE, PLAN, README):
            self.assertIn("--expect-thinking", path.read_text(), str(path))


class TestOurFilesNameRealBusCommands(unittest.TestCase):

    def test_no_owned_file_invents_a_bus_subcommand(self):
        """This repo shipped `bus launch --expect-provider/--expect-model` in
        two places. There is no `bus launch`; the flags live on `await`, and
        the launcher is `launch-worker`. A reader typing it gets an argparse
        error, having been told the check does not exist."""
        choices = re.search(r"\{(register[a-z,\-]+)\}", bus_help())
        self.assertTrue(choices, "cannot read bus's subcommand list")
        real = set(choices.group(1).split(","))
        self.assertIn("launch-worker", real)
        for path in (TEMPLATE, PLAN, README):
            for named in re.findall(r"`(?:[\w./~-]*/)?bus ([a-z][a-z-]*)",
                                    path.read_text()):
                self.assertIn(named, real,
                              "%s names `bus %s`, which does not exist"
                              % (path.name, named))


if __name__ == "__main__":
    unittest.main()
