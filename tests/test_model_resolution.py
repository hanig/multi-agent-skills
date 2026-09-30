"""Per-project point-release resolution (docs/plan-model-resolution.md).

Every case runs against a temporary state home, so nothing here reads or
writes the real ~/.local/state.
"""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "skills" / "hanig-review-gate"
sys.path.insert(0, str(GATE / "scripts"))
sys.path.insert(0, str(ROOT / "skills" / "hanig-swarm" / "scripts"))

import model_family as MF  # noqa: E402
import resolve_models as RM  # noqa: E402
import review as R  # noqa: E402

SOL = {"listing": "openai", "vendor": "", "prefix": "gpt-", "separator": ".",
       "suffix": "-sol", "major": 6}


def fixture_roster():
    """The shipped roster with both Sol seats pinned to gpt-6-sol.

    Reader and resolver tests need a pin with a known newer point release,
    and must not depend on which release main happens to ship.
    """
    roster = []
    for seat in shipped()[0]:
        seat = dict(seat)
        if seat["name"] in ("sol", "sol-tiebreak"):
            seat["model"] = "gpt-6-sol"
            seat["_max_output_tokens_accepted"] = dict(
                seat["_max_output_tokens_accepted"], model="gpt-6-sol")
        roster.append(seat)
    return roster


def shipped():
    reviewers = json.loads((GATE / "reviewers.json").read_text(encoding="utf-8"))
    agents = json.loads((ROOT / "skills/hanig-swarm/agents.json")
                        .read_text(encoding="utf-8"))
    return reviewers["reviewers"], agents


class StateHome(unittest.TestCase):
    """A private state home and project directory per test."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.state = base / "state"
        self.project = base / "project"
        self.project.mkdir()
        env = mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("HANIG_ROUTING_SNAPSHOTS", None)
        self.digests = MF.config_digests(R.CONFIG, R.AGENTS_CONFIG)

    def snapshot(self, **reviewers):
        return {"schema": MF.SCHEMA, "project": str(self.project),
                "project_key": MF.project_key(self.project),
                "config_sha256": self.digests, "resolved_at": "t",
                "reviewers": reviewers, "agent_default": None}


class TestFamilyParser(unittest.TestCase):
    def test_membership_is_exactly_vendor_prefix_version_suffix(self):
        accepted = {"gpt-6-sol": (6, 0), "gpt-6.1-sol": (6, 1),
                    "gpt-6.10-sol": (6, 10), "gpt-7-sol": (7, 0)}
        for model, version in accepted.items():
            with self.subTest(model=model):
                self.assertEqual(MF.version_of(model, SOL), version)
        for model in ("gpt-6-sol-pro", "gpt-6-sol:batch", "gpt-6.1-sol-2026-09-30",
                      "openai/gpt-6-sol", "gpt-6.1.2-sol", "gpt--sol", "gpt-x-sol",
                      "gpt-6-solar", "sol", "", None, "gpt-6-luna",
                      "/gpt-6.1-sol", "gpt-6.1-sol/", "x//gpt-6.1-sol",
                      "gpt-" + "9" * 5000 + "-sol"):
            with self.subTest(model=model):
                self.assertIsNone(MF.version_of(model, SOL))

    def test_vendor_paths_and_hyphen_separator(self):
        kimi = dict(SOL, listing="openrouter", vendor="moonshotai",
                    prefix="kimi-k", suffix="-code", major=2)
        self.assertEqual(MF.version_of("moonshotai/kimi-k2.7-code", kimi), (2, 7))
        self.assertIsNone(MF.version_of("kimi-k2.7-code", kimi))
        self.assertIsNone(MF.version_of("othercorp/kimi-k2.7-code", kimi))
        self.assertIsNone(MF.version_of("x/moonshotai/kimi-k2.7-code", kimi))
        opus = dict(SOL, prefix="claude-opus-", separator="-", suffix="", major=5)
        self.assertEqual(MF.version_of("claude-opus-5-5", opus), (5, 5))
        self.assertIsNone(MF.version_of("claude-opus-5.5", opus))
        self.assertEqual(MF.split_id("gpt-6-sol"), ("", "gpt-6-sol"))

    def test_versions_compare_as_integers(self):
        chosen, _newer = MF.select(["gpt-6.2-sol", "gpt-6.10-sol", "gpt-6.9-sol"],
                                   SOL, "gpt-6.1-sol")
        self.assertEqual(chosen, "gpt-6.10-sol")

    def test_invalid_family_declarations_are_refused(self):
        for bad in (None, dict(SOL, listing="anthropic"), dict(SOL, listing="paseo:"),
                    dict(SOL, prefix=""), dict(SOL, separator="_"),
                    dict(SOL, major="6"), dict(SOL, major=True), dict(SOL, major=-1)):
            with self.subTest(family=bad):
                with self.assertRaises(MF.FamilyError):
                    MF.check_family(bad)

    def test_every_shipped_pin_is_in_its_family_at_its_major(self):
        reviewers, agents = shipped()
        declared = [(r["name"], r["model"], r["family"])
                    for r in reviewers if r.get("family")]
        default = agents["default"]
        declared.append(("agent-default", default["provider"].split("/", 1)[1],
                         default["family"]))
        self.assertGreaterEqual(len(declared), 8)
        for name, model, family in declared:
            with self.subTest(seat=name):
                MF.check_family(family)
                version = MF.version_of(model, family)
                self.assertIsNotNone(version, model)
                self.assertEqual(version[0], family["major"])


class TestSelection(unittest.TestCase):
    def test_greatest_same_major_point_release_above_the_pin(self):
        listed = ["gpt-6-sol", "gpt-6.1-sol", "gpt-6.2-sol", "gpt-6.2-sol-pro",
                  "gpt-6.3-sol:batch", "gpt-7-sol", "gpt-7.1-sol", "gpt-5.9-sol"]
        chosen, newer = MF.select(listed, SOL, "gpt-6-sol")
        self.assertEqual(chosen, "gpt-6.2-sol")
        self.assertEqual(newer, ["gpt-7-sol", "gpt-7.1-sol"])

    def test_nothing_at_or_below_the_pin_is_chosen(self):
        self.assertEqual(MF.select(["gpt-6-sol", "gpt-6.1-sol"], SOL, "gpt-6.1-sol"),
                         (None, []))
        self.assertEqual(MF.select([], SOL, "gpt-6-sol"), (None, []))

    def test_a_pin_outside_its_family_is_a_configuration_error(self):
        with self.assertRaises(MF.FamilyError):
            MF.select(["gpt-6.1-sol"], SOL, "gpt-6-luna")


class TestProbes(unittest.TestCase):
    SEAT = {"name": "sol", "provider": "openai", "model": "gpt-6-sol",
            "effort": "xhigh", "max_output_tokens": 128000, "family": SOL}
    VERDICT = json.dumps({"verdict": "upheld", "findings": [], "claims": [
        {"claim_index": 1, "status": "supported", "reason": "prose only"}]})
    GOOD = {"text": VERDICT, "in_tokens": 11, "out_tokens": 5, "reasoning_tokens": 0,
            "served_model": "gpt-6.1-sol", "status": "completed", "response_id": "r"}

    def probe(self, response, error=None):
        call = mock.Mock(return_value=(response, error))
        with mock.patch.dict(R.PROVIDERS, {"openai": call}):
            result = RM.probe_reviewer(self.SEAT, "gpt-6.1-sol", 1)
        return result, call

    def test_a_passing_probe_is_a_typed_budget_record_for_the_exact_id(self):
        (record, why), call = self.probe(dict(self.GOOD))
        self.assertIsNone(why)
        requested = call.call_args.args[0]
        self.assertEqual((requested["model"], requested["effort"],
                          requested["max_output_tokens"]),
                         ("gpt-6.1-sol", "xhigh", 128000))
        for key, value in {"model": "gpt-6.1-sol", "provider": "openai",
                           "max_output_tokens": 128000, "effort": "xhigh",
                           "outcome": "completed", "output_tokens": 5}.items():
            self.assertEqual(record[key], value, key)

    def test_every_failure_mode_keeps_the_pin(self):
        cases = {"error": (None, "HTTP 500"),
                 "incomplete": (dict(self.GOOD, status="incomplete"), None),
                 "empty": (dict(self.GOOD, text="  "), None),
                 "not a verdict": (dict(self.GOOD, text="OK"), None),
                 "no tokens": (dict(self.GOOD, out_tokens=0), None),
                 "bool tokens": (dict(self.GOOD, out_tokens=True), None),
                 "other model": (dict(self.GOOD, served_model="gpt-6-sol"), None),
                 "snapshot": (dict(self.GOOD, served_model="gpt-6.1-sol-2026-09-30"),
                              None),
                 "no model": (dict(self.GOOD, served_model=None), None)}
        for label, (response, error) in cases.items():
            with self.subTest(case=label):
                (record, why), _call = self.probe(response, error)
                self.assertIsNone(record)
                self.assertTrue(why)

    def paseo(self, inspected, archive_fails=False):
        calls = []

        def fake(argv, timeout, cwd=None):
            calls.append(argv[0])
            if argv[0] == "run":
                return {"agentId": "a1", "status": "completed"}
            if argv[0] == "inspect":
                return inspected
            if argv[0] == "ls":
                return []
            if archive_fails:
                raise RuntimeError("paseo archive exited 1: daemon gone")
            return {"status": "archived"}
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"XDG_STATE_HOME": tmp}), \
                mock.patch.object(RM, "_paseo", side_effect=fake):
            result = RM.probe_agent("codex", "gpt-6.1-astra", "high", 5)
        return result, calls

    def test_paseo_canary_must_inspect_as_exactly_what_was_requested(self):
        good = {"Provider": "codex", "Model": "gpt-6.1-astra", "Thinking": "high"}
        (probe, why), calls = self.paseo(good)
        self.assertIsNone(why)
        self.assertEqual(probe["Model"], "gpt-6.1-astra")
        self.assertEqual(calls, ["run", "inspect", "ls", "archive"])
        for key, value in (("Model", "gpt-6-astra"), ("Thinking", "auto"),
                           ("Provider", "claude")):
            with self.subTest(mismatch=key):
                (probe, why), calls = self.paseo(dict(good, **{key: value}))
                self.assertIsNone(probe)
                self.assertIn("inspected", why)
                self.assertEqual(calls, ["run", "inspect", "ls", "archive"])

    def test_an_archive_failure_survives_an_inspect_failure(self):
        def fake(argv, timeout, cwd=None):
            if argv[0] == "run":
                return {"agentId": "a1"}
            raise RuntimeError("paseo %s exited 1" % argv[0])
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"XDG_STATE_HOME": tmp}), \
                mock.patch.object(RM, "_paseo", side_effect=fake), \
                redirect_stderr(io.StringIO()):
            probe, why = RM.probe_agent("codex", "gpt-6.1-astra", "high", 5)
        self.assertIsNone(probe)
        self.assertIn("inspect exited", why)
        self.assertIn("left unarchived", why)

    def test_an_archive_reply_counts_only_when_it_says_archived(self):
        with mock.patch.object(RM, "_paseo", return_value={"status": "failed"}), \
                redirect_stderr(io.StringIO()) as err:
            self.assertIn("'failed'", RM._archive("a1"))
        self.assertIn("not archived", err.getvalue())
        with mock.patch.object(RM, "_paseo", return_value={"status": "archived"}):
            self.assertIsNone(RM._archive("a1"))

    def test_a_title_match_with_an_unusable_id_is_reported(self):
        def fake(argv, timeout, cwd=None):
            if argv[0] == "ls":
                return [{"id": 7, "name": "resolve-canary-x"}]
            return {"status": "archived"}
        with mock.patch.object(RM, "_paseo", side_effect=fake), \
                redirect_stderr(io.StringIO()) as err:
            reason = RM._archive_orphans(None, "resolve-canary-x", ".")
        self.assertIn("unusable id", reason)
        self.assertIn("unusable id", err.getvalue())

    def test_a_failed_lookup_is_printed(self):
        def fake(argv, timeout, cwd=None):
            if argv[0] == "ls":
                raise RuntimeError("daemon gone")
            return {"status": "archived"}
        with mock.patch.object(RM, "_paseo", side_effect=fake), \
                redirect_stderr(io.StringIO()) as err:
            reason = RM._archive_orphans("a1", "resolve-canary-x", ".")
        self.assertIn("could not look up", reason)
        self.assertIn("could not look up", err.getvalue())

    def test_a_failed_archive_is_reported_not_swallowed(self):
        good = {"Provider": "codex", "Model": "gpt-6.1-astra", "Thinking": "high"}
        with redirect_stderr(io.StringIO()) as err:
            (probe, why), calls = self.paseo(good, archive_fails=True)
        self.assertIn("not archived", err.getvalue())
        self.assertIn("daemon gone", probe["archive_error"])
        with redirect_stderr(io.StringIO()):
            (probe, why), _calls = self.paseo(dict(good, Model="x"), archive_fails=True)
        self.assertIn("left unarchived", why)

    def test_a_malformed_paseo_reply_fails_the_probe_and_never_raises(self):
        replies = {"run is a list": {"run": [], "inspect": {}},
                   "run has no id": {"run": {"agentId": 7}, "inspect": {}},
                   "inspect is a list": {"run": {"agentId": "a1"}, "inspect": []},
                   "inspect is a string": {"run": {"agentId": "a1"}, "inspect": "x"}}
        for label, reply in replies.items():
            def fake(argv, timeout, cwd=None, reply=reply):
                return reply.get(argv[0], {"status": "archived"})
            with self.subTest(case=label), tempfile.TemporaryDirectory() as tmp, \
                    mock.patch.dict(os.environ, {"XDG_STATE_HOME": tmp}), \
                    mock.patch.object(RM, "_paseo", side_effect=fake):
                probe, why = RM.probe_agent("codex", "gpt-6.1-astra", "high", 5)
            self.assertIsNone(probe)
            self.assertTrue(why)

    def test_a_run_that_fails_after_creating_an_agent_still_archives_it(self):
        archived = []

        def fake(argv, timeout, cwd=None):
            if argv[0] == "run":
                raise RM.PaseoError("paseo run exited 1: wait timeout",
                                    {"agentId": "orphan"})
            if argv[0] == "ls":
                return []
            archived.append(argv[1])
            return {"status": "archived"}
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"XDG_STATE_HOME": tmp}), \
                mock.patch.object(RM, "_paseo", side_effect=fake):
            probe, why = RM.probe_agent("codex", "gpt-6.1-astra", "high", 5)
        self.assertIsNone(probe)
        self.assertEqual(archived, ["orphan"])
        self.assertIn("wait timeout", why)

    def test_a_run_reporting_no_id_is_still_archived_by_title(self):
        seen = {"archived": []}

        def fake(argv, timeout, cwd=None):
            if argv[0] == "run":
                seen["title"] = argv[argv.index("--title") + 1]
                return {"agentId": 7}
            if argv[0] == "ls":
                return [{"id": "made", "name": seen["title"]}]
            seen["archived"].append(argv[1])
            return {"status": "archived"}
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"XDG_STATE_HOME": tmp}), \
                mock.patch.object(RM, "_paseo", side_effect=fake):
            probe, why = RM.probe_agent("codex", "gpt-6.1-astra", "high", 5)
        self.assertIsNone(probe)
        self.assertIn("no agentId", why)
        self.assertEqual(seen["archived"], ["made"])

    def test_a_killed_run_is_archived_by_its_unique_title(self):
        seen = {}

        def fake(argv, timeout, cwd=None):
            if argv[0] == "run":
                seen["title"] = argv[argv.index("--title") + 1]
                raise subprocess.TimeoutExpired(["paseo", "run"], timeout)
            if argv[0] == "ls":
                return [{"id": "other", "name": "resolve-canary-elsewhere"},
                        {"id": "mine", "name": seen["title"]}]
            seen.setdefault("archived", []).append(argv[1])
            return {"status": "archived"}
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"XDG_STATE_HOME": tmp}), \
                mock.patch.object(RM, "_paseo", side_effect=fake):
            probe, why = RM.probe_agent("codex", "gpt-6.1-astra", "high", 5)
        self.assertIsNone(probe)
        self.assertTrue(seen["title"].startswith("resolve-canary-"))
        self.assertEqual(seen["archived"], ["mine"])

    def test_a_nonzero_paseo_exit_keeps_the_json_it_printed(self):
        done = subprocess.CompletedProcess([], 1, stdout='banner\n{"agentId": "a9"}',
                                           stderr="boom")
        with mock.patch.object(RM.subprocess, "run", return_value=done):
            with self.assertRaises(RM.PaseoError) as raised:
                RM._paseo(["run"], 5)
        self.assertEqual(raised.exception.payload, {"agentId": "a9"})

    def test_a_malformed_listing_raises_the_handled_error(self):
        for listing, reply in (("openrouter", {"data": [{"name": "x"}]}),
                               ("paseo:codex", [{"id": "gpt-6-astra"}, {"model": "y"}]),
                               ("paseo:codex", {"id": "gpt-6-astra"}),
                               ("paseo:codex", "gpt-6-astra"),
                               ("openrouter", []), ("openrouter", {"data": {"id": 1}})):
            with self.subTest(listing=listing, reply=reply):
                with mock.patch.object(RM, "_paseo", return_value=reply), \
                        mock.patch.object(RM, "_get_json", return_value=reply):
                    with self.assertRaises(RuntimeError):
                        RM.list_ids(listing, 5)

    def test_paseo_output_is_parsed_after_its_banner(self):
        done = subprocess.CompletedProcess(
            [], 0, stdout='Created workspace w - x\nTip\n{"agentId": "a1"}\n', stderr="")
        with mock.patch.object(RM.subprocess, "run", return_value=done):
            self.assertEqual(RM._paseo(["run"], 5), {"agentId": "a1"})


class TestSnapshotStorage(StateHome):
    def test_snapshot_lives_under_the_state_home_and_is_written_atomically(self):
        path = MF.write_snapshot(self.project, self.snapshot())
        self.assertEqual(path.parent, self.state / "hanig-review-gate" / "routing")
        self.assertEqual(json.loads(path.read_text())["project_key"],
                         MF.project_key(self.project))
        self.assertEqual([p.name for p in path.parent.iterdir()], [path.name])

    def test_a_state_home_inside_the_project_is_refused(self):
        with mock.patch.dict(os.environ,
                             {"XDG_STATE_HOME": str(self.project / "state")}):
            with self.assertRaises(OSError):
                MF.write_snapshot(self.project, self.snapshot())
            self.assertFalse((self.project / "state").exists())

    def test_projects_in_one_repository_do_not_share_and_subdirs_find_theirs(self):
        (self.project / ".git").mkdir()
        a, b = self.project / "a", self.project / "b"
        (a / "deep").mkdir(parents=True)
        b.mkdir()
        self.assertNotEqual(MF.snapshot_path(a), MF.snapshot_path(b))
        MF.write_snapshot(a, dict(self.snapshot(), project=str(a),
                                  project_key=MF.project_key(a)))
        path, _data, project = MF.find_snapshot(a / "deep")
        self.assertEqual((path, Path(os.path.realpath(project))),
                         (MF.snapshot_path(a), Path(os.path.realpath(a))))
        self.assertEqual(MF.find_snapshot(b)[0], None)


class TestReaders(StateHome):
    ENTRY = {"model": "gpt-6.1-sol",
             "record": {"model": "gpt-6.1-sol", "provider": "openai",
                        "max_output_tokens": 128000, "effort": "xhigh",
                        "outcome": "completed", "output_tokens": 5}}

    def roster(self):
        return fixture_roster()

    def apply(self, snapshot, roster=None):
        MF.write_snapshot(self.project, snapshot)
        return MF.apply_to_reviewers(roster or self.roster(), self.digests,
                                     start=self.project)

    def test_a_matching_snapshot_moves_only_its_valid_seats(self):
        out, notes = self.apply(self.snapshot(**{"sol": self.ENTRY}))
        seats = {r["name"]: r for r in out}
        self.assertEqual(seats["sol"]["model"], "gpt-6.1-sol")
        self.assertEqual(seats["sol"]["_resolved_from"], "gpt-6-sol")
        self.assertEqual(seats["sol"]["_max_output_tokens_accepted"]["model"],
                         "gpt-6.1-sol")
        self.assertEqual(seats["sol-tiebreak"]["model"], "gpt-6-sol")
        self.assertEqual(notes, [])

    def test_any_config_byte_change_falls_back_to_the_pins(self):
        MF.write_snapshot(self.project, self.snapshot(**{"sol": self.ENTRY}))
        changed = dict(self.digests, **{"reviewers.json": "0" * 64})
        out, notes = MF.apply_to_reviewers(self.roster(), changed, start=self.project)
        self.assertEqual({r["name"]: r["model"] for r in out}["sol"], "gpt-6-sol")
        self.assertIn("does not match", notes[0])

    def test_entries_failing_the_live_checks_keep_the_pin(self):
        bad = {"older": dict(self.ENTRY, model="gpt-5.9-sol"),
               "same as pin": dict(self.ENTRY, model="gpt-6-sol"),
               "new major": dict(self.ENTRY, model="gpt-7-sol"),
               "other family": dict(self.ENTRY, model="gpt-6.1-luna"),
               "record names another": dict(self.ENTRY, record=dict(
                   self.ENTRY["record"], model="gpt-6-sol")),
               "record effort": dict(self.ENTRY, record=dict(
                   self.ENTRY["record"], effort="high")),
               "record cap": dict(self.ENTRY, record=dict(
                   self.ENTRY["record"], max_output_tokens=64000)),
               "record provider": dict(self.ENTRY, record=dict(
                   self.ENTRY["record"], provider="openrouter")),
               "no tokens": dict(self.ENTRY, record=dict(
                   self.ENTRY["record"], output_tokens=0)),
               "no record": {"model": "gpt-6.1-sol"}}
        for label, entry in bad.items():
            if label in ("older", "new major") or entry["model"] != self.ENTRY["model"]:
                entry = dict(entry, record=dict(entry.get("record") or {},
                                                model=entry["model"]))
            with self.subTest(case=label):
                out, notes = self.apply(self.snapshot(**{"sol": entry}))
                self.assertEqual({r["name"]: r["model"] for r in out}["sol"],
                                 "gpt-6-sol")
                self.assertTrue(any("sol" in note for note in notes))

    def test_the_gate_never_breaks_on_resolution_and_falls_back_to_pins(self):
        broken = Path(self.tmp.name) / "agents-dir"
        broken.mkdir()
        with mock.patch.object(R, "AGENTS_CONFIG", broken), \
                mock.patch.object(R, "load_reviewers", return_value=self.roster()), \
                redirect_stderr(io.StringIO()) as err:
            effective = R.load_effective_reviewers(start=self.project)
        self.assertEqual([r["model"] for r in effective],
                         [r["model"] for r in self.roster()])
        self.assertIn("using pins", err.getvalue())
        with mock.patch.dict(os.environ, {"HANIG_ROUTING_SNAPSHOTS": "off"}), \
                mock.patch.object(R, "AGENTS_CONFIG", broken), \
                mock.patch.object(R, "load_reviewers", return_value=self.roster()), \
                redirect_stderr(io.StringIO()) as err:
            R.load_effective_reviewers(start=self.project)
        self.assertEqual(err.getvalue(), "")

    def test_an_unreadable_snapshot_is_reported_and_ignored(self):
        path = MF.snapshot_path(self.project)
        path.parent.mkdir(parents=True)
        path.write_text("{not json")
        with redirect_stderr(io.StringIO()) as err:
            self.assertEqual(MF.find_snapshot(self.project), (None, None, None))
        self.assertIn("unreadable, ignored", err.getvalue())

    def test_the_off_switch_ignores_snapshots(self):
        MF.write_snapshot(self.project, self.snapshot(**{"sol": self.ENTRY}))
        with mock.patch.dict(os.environ, {"HANIG_ROUTING_SNAPSHOTS": "off"}):
            out, _notes = MF.apply_to_reviewers(self.roster(), self.digests,
                                                start=self.project)
        self.assertEqual({r["name"]: r["model"] for r in out}["sol"], "gpt-6-sol")

    def test_author_exclusion_sees_the_effective_model(self):
        MF.write_snapshot(self.project, self.snapshot(**{"sol": self.ENTRY}))
        with mock.patch.object(R, "load_reviewers", return_value=self.roster()), \
                redirect_stderr(io.StringIO()) as err:
            effective = R.load_effective_reviewers(start=self.project)
        self.assertIn("sol resolved gpt-6-sol -> gpt-6.1-sol", err.getvalue())
        models = R.author_model_ids(["codex/gpt-6.1-sol"], effective)
        _kept, excluded = R.exclude_authors(effective, models)
        self.assertEqual({r["name"] for r in excluded}, {"sol"})

    def test_a_resolved_seat_that_fails_its_review_falls_back_to_its_pin(self):
        seat = {"name": "sol", "provider": "openai", "model": "gpt-6.1-sol",
                "effort": "xhigh", "_resolved_from": "gpt-6-sol",
                "_max_output_tokens_accepted": {"model": "gpt-6.1-sol"},
                "_pinned_accepted": {"model": "gpt-6-sol"}}
        verdict = json.dumps({"verdict": "upheld", "findings": [], "claims": []})
        asked = []

        def provider(rev, prompt, timeout, deadline=None):
            asked.append((rev["model"], rev["_max_output_tokens_accepted"]["model"],
                          "_resolved_from" in rev))
            if rev["model"] == "gpt-6.1-sol":
                return None, "no content: finish_reason='length'"
            return {"text": verdict, "in_tokens": 1, "out_tokens": 2}, None
        with mock.patch.dict(R.PROVIDERS, {"openai": provider}), \
                redirect_stderr(io.StringIO()) as err:
            result = R.run_one(seat, "prompt", 5)
        self.assertTrue(result["ok"])
        self.assertEqual(result["model"], "gpt-6-sol")
        self.assertEqual(result["fell_back_from"], "gpt-6.1-sol")
        self.assertIn("no content", result["resolved_error"])
        self.assertEqual(asked, [("gpt-6.1-sol", "gpt-6.1-sol", True),
                                 ("gpt-6-sol", "gpt-6-sol", False)])
        self.assertIn("fell back", err.getvalue())
        with mock.patch.dict(R.PROVIDERS, {"openai": provider}):
            pinned_only = R.run_one(dict(seat, model="gpt-6.1-sol",
                                         _resolved_from=None), "prompt", 5)
        self.assertFalse(pinned_only["ok"])

    def test_an_author_on_the_pin_keeps_the_seat_but_loses_the_fallback(self):
        seat = {"name": "sol", "provider": "openai", "model": "gpt-6.1-sol",
                "_resolved_from": "gpt-6-sol",
                "_max_output_tokens_accepted": {"model": "gpt-6.1-sol"},
                "_pinned_accepted": {"model": "gpt-6-sol"}}
        kept, excluded = R.exclude_authors([seat], R.author_model_ids(
            ["codex/gpt-6-sol"], [seat]))
        self.assertEqual(excluded, [])
        self.assertTrue(kept[0]["_no_pin_fallback"])
        asked = []

        def provider(rev, prompt, timeout, deadline=None):
            asked.append(rev["model"])
            return None, "HTTP 500"
        with mock.patch.dict(R.PROVIDERS, {"openai": provider}):
            result = R.run_one(kept[0], "prompt", 5)
        self.assertEqual(asked, ["gpt-6.1-sol"])
        self.assertFalse(result["ok"])
        _kept, excluded = R.exclude_authors([seat], R.author_model_ids(
            ["codex/gpt-6.1-sol"], [seat]))
        self.assertEqual([r["name"] for r in excluded], ["sol"])

    def test_a_usable_refutation_is_never_retried_on_the_pin(self):
        seat = {"name": "sol", "provider": "openai", "model": "gpt-6.1-sol",
                "_resolved_from": "gpt-6-sol",
                "_max_output_tokens_accepted": {"model": "gpt-6.1-sol"},
                "_pinned_accepted": {"model": "gpt-6-sol"}}
        refuted = json.dumps({"verdict": "refuted", "findings": [], "claims": []})
        asked = []

        def provider(rev, prompt, timeout, deadline=None):
            asked.append(rev["model"])
            return {"text": refuted, "in_tokens": 1, "out_tokens": 2}, None
        with mock.patch.dict(R.PROVIDERS, {"openai": provider}):
            result = R.run_one(seat, "prompt", 5)
        self.assertTrue(result["ok"])
        self.assertEqual(result["verdict"], "refuted")
        self.assertEqual(asked, ["gpt-6.1-sol"])

    def test_the_committee_keeps_the_pins_even_with_a_snapshot(self):
        import committee
        MF.write_snapshot(self.project, self.snapshot(**{"sol-tiebreak": dict(
            self.ENTRY, record=dict(self.ENTRY["record"]))}))
        with mock.patch.object(committee.R, "load_reviewers",
                               return_value=self.roster()), \
                mock.patch.object(os, "getcwd", return_value=str(self.project)):
            seat, error = committee.tiebreaker()
        self.assertIsNone(error)
        self.assertEqual(seat["model"], "gpt-6-sol")
        self.assertNotIn("_resolved_from", seat)

    def test_swarm_adopts_a_valid_agent_default_without_network_code(self):
        import swarm as S
        agents = shipped()[1]
        MF.write_snapshot(self.project, dict(self.snapshot(), agent_default={
            "provider": "codex/gpt-6.1-astra",
            "probe": {"Provider": "codex", "Model": "gpt-6.1-astra",
                      "Thinking": agents["thinking_by_model"]["codex/gpt-6-astra"]}}))
        plan = self.project / ".swarm" / "plan.json"
        plan.parent.mkdir()
        pinned, table = S.DEFAULT_AGENT_PROVIDER, dict(S.THINKING_BY_MODEL)
        self.addCleanup(setattr, S, "DEFAULT_AGENT_PROVIDER", pinned)
        self.addCleanup(setattr, S, "THINKING_BY_MODEL", table)
        with redirect_stderr(io.StringIO()):
            self.assertEqual(S.apply_agent_resolution(plan), "codex/gpt-6.1-astra")
        self.assertEqual(S.default_thinking_for({}), table[pinned])
        self.assertEqual(S.default_thinking_for({"provider": "codex/other"}),
                         S.DEFAULT_AGENT_THINKING)
        check = subprocess.run(
            [sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); "
             "import swarm; swarm.apply_agent_resolution(sys.argv[2]); "
             "assert 'review' not in sys.modules and 'committee' not in sys.modules"
             " and 'resolve_models' not in sys.modules",
             str(ROOT / "skills/hanig-swarm/scripts"), str(plan)],
            capture_output=True, text=True, timeout=60,
            env=dict(os.environ, XDG_STATE_HOME=str(self.state)))
        self.assertEqual(check.returncode, 0, check.stderr)

    def test_one_thinking_rule_for_resolver_reader_and_swarm(self):
        import swarm as S
        default = {"provider": "codex/gpt-6-astra", "thinking": "medium"}
        table = {"codex/gpt-6-astra": "high", "codex/gpt-6.1-astra": "xhigh"}
        self.assertEqual(MF.agent_thinking(default, table, "codex/gpt-6.1-astra"), "xhigh")
        self.assertEqual(MF.agent_thinking(default, table, "codex/gpt-6.2-astra"), "high")
        self.assertEqual(MF.agent_thinking(default, {}, "codex/gpt-6.2-astra"), "medium")
        family = shipped()[1]["default"]["family"]
        default = dict(default, family=family)
        entry = lambda thinking: {"provider": "codex/gpt-6.1-astra", "probe": {
            "Provider": "codex", "Model": "gpt-6.1-astra", "Thinking": thinking}}
        self.assertIsNone(MF.agent_default_override(default, table, entry("high")))
        self.assertEqual(MF.agent_default_override(default, table, entry("xhigh")),
                         "codex/gpt-6.1-astra")
        agents = json.loads(json.dumps(shipped()[1]))
        agents["thinking_by_model"]["codex/gpt-6.1-astra"] = "xhigh"
        fake = Path(self.tmp.name) / "agents.json"
        fake.write_text(json.dumps(agents))
        digests = MF.config_digests(R.CONFIG, fake)
        MF.write_snapshot(self.project, dict(self.snapshot(), config_sha256=digests,
                                             agent_default=entry("xhigh")))
        table_before = dict(S.THINKING_BY_MODEL)
        self.addCleanup(setattr, S, "THINKING_BY_MODEL", table_before)
        self.addCleanup(setattr, S, "DEFAULT_AGENT_PROVIDER", S.PINNED_AGENT_PROVIDER)
        with mock.patch.object(S, "AGENTS_FILE", fake), \
                mock.patch.object(S, "THINKING_BY_MODEL", dict(table_before)), \
                redirect_stderr(io.StringIO()):
            self.assertEqual(S.apply_agent_resolution(self.project / "plan.json"),
                             "codex/gpt-6.1-astra")
            self.assertEqual(S.default_thinking_for({}), "xhigh")

    def test_swarm_resolution_never_outlives_its_plan(self):
        import swarm as S
        agents = shipped()[1]
        MF.write_snapshot(self.project, dict(self.snapshot(), agent_default={
            "provider": "codex/gpt-6.1-astra",
            "probe": {"Provider": "codex", "Model": "gpt-6.1-astra",
                      "Thinking": agents["thinking_by_model"]["codex/gpt-6-astra"]}}))
        other = Path(self.tmp.name) / "other"
        other.mkdir()
        self.addCleanup(setattr, S, "DEFAULT_AGENT_PROVIDER", S.PINNED_AGENT_PROVIDER)
        with redirect_stderr(io.StringIO()):
            self.assertEqual(S.apply_agent_resolution(self.project / "plan.json"),
                             "codex/gpt-6.1-astra")
            self.assertIn("codex/gpt-6.1-astra", S.THINKING_BY_MODEL)
            self.assertEqual(S.apply_agent_resolution(other / "plan.json"),
                             S.PINNED_AGENT_PROVIDER)
        self.assertEqual(S.THINKING_BY_MODEL, S.PINNED_THINKING_BY_MODEL)

    def test_swarm_never_breaks_on_resolution(self):
        import swarm as S
        self.addCleanup(setattr, S, "DEFAULT_AGENT_PROVIDER", S.PINNED_AGENT_PROVIDER)
        with mock.patch.object(S, "_model_family", side_effect=OSError("unreadable")), \
                redirect_stderr(io.StringIO()) as err:
            self.assertEqual(S.apply_agent_resolution(self.project / "plan.json"),
                             S.PINNED_AGENT_PROVIDER)
        self.assertIn("using the pin", err.getvalue())

    def test_swarm_keeps_the_pin_for_a_mismatched_agent_probe(self):
        import swarm as S
        MF.write_snapshot(self.project, dict(self.snapshot(), agent_default={
            "provider": "codex/gpt-6.1-astra",
            "probe": {"Provider": "codex", "Model": "gpt-6-astra", "Thinking": "high"}}))
        pinned = S.DEFAULT_AGENT_PROVIDER
        self.addCleanup(setattr, S, "DEFAULT_AGENT_PROVIDER", pinned)
        self.assertEqual(S.apply_agent_resolution(self.project / "plan.json"), pinned)


class TestResolveEndToEnd(StateHome):
    LISTINGS = {"openai": ["gpt-6-sol", "gpt-6.1-sol", "gpt-6.1-sol-pro", "gpt-7-sol",
                           "gpt-6-luna", "gpt-6-astra"],
                "openrouter": ["anthropic/claude-sonnet-5.5", "moonshotai/kimi-k2.7-code",
                               "deepseek/deepseek-v4-pro"],
                "paseo:codex": ["gpt-6-astra", "gpt-6-sol"]}

    def run_resolver(self, *flags, probe=None):
        def record(seat, candidate, timeout):
            return ({"model": candidate, "provider": seat["provider"],
                     "max_output_tokens": seat.get("max_output_tokens"),
                     "effort": seat.get("effort"), "outcome": "completed",
                     "output_tokens": 3}, None)
        argv = ["resolve_models.py", "--project", str(self.project), "--json"] + list(flags)
        out = io.StringIO()
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(RM.R, "load_reviewers", return_value=fixture_roster()), \
                mock.patch.object(RM, "list_ids", side_effect=lambda l, t: self.LISTINGS[l]), \
                mock.patch.object(RM, "probe_reviewer", side_effect=probe or record) as p, \
                mock.patch.object(RM, "probe_agent") as agent, \
                redirect_stdout(out):
            with self.assertRaises(SystemExit) as stopped:
                RM.main()
        self.assertEqual(stopped.exception.code, 0)
        self.assertFalse(agent.called, "no newer paseo astra is listed")
        return json.loads(out.getvalue()), p

    def test_resolves_point_releases_and_reports_new_generations(self):
        report, probe = self.run_resolver()
        status = {(l["seat"], l["status"]) for l in report["seats"]}
        self.assertIn(("sol", "RESOLVED"), status)
        self.assertIn(("sol-tiebreak", "RESOLVED"), status)
        self.assertIn(("sol", "NEW_GENERATION"), status)
        self.assertIn(("luna", "PINNED"), status)
        self.assertEqual({c.args[1] for c in probe.call_args_list}, {"gpt-6.1-sol"})
        snapshot = json.loads(Path(report["snapshot"]).read_text())
        self.assertEqual(set(snapshot["reviewers"]), {"sol", "sol-tiebreak"})
        self.assertEqual(snapshot["config_sha256"], self.digests)

    def test_a_malformed_listing_keeps_the_pin(self):
        argv = ["resolve_models.py", "--project", str(self.project), "--json"]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(RM, "_get_json", return_value=[]), \
                mock.patch.object(RM, "_paseo", return_value={"not": "a list"}), \
                mock.patch.dict(os.environ, {"OPENAI_API_KEY": "k"}), \
                redirect_stdout(io.StringIO()) as out:
            with self.assertRaises(SystemExit) as stopped:
                RM.main()
        self.assertEqual(stopped.exception.code, 0)
        statuses = {l["status"] for l in json.loads(out.getvalue())["seats"]}
        self.assertEqual(statuses, {"LISTING_FAILED"})

    def test_any_failure_inside_one_seat_keeps_its_pin_and_spares_the_others(self):
        def probe(seat, candidate, timeout):
            if seat["name"] == "sol":
                raise AttributeError("'list' object has no attribute 'get'")
            return ({"model": candidate, "provider": seat["provider"],
                     "max_output_tokens": seat.get("max_output_tokens"),
                     "effort": seat.get("effort"), "outcome": "completed",
                     "output_tokens": 3}, None)
        report, _probe = self.run_resolver(probe=probe)
        status = {(l["seat"], l["status"]) for l in report["seats"]}
        self.assertIn(("sol", "SEAT_FAILED"), status)
        self.assertIn(("sol-tiebreak", "RESOLVED"), status)
        failed = [l for l in report["seats"] if l["status"] == "SEAT_FAILED"]
        self.assertIn("AttributeError", failed[0]["detail"])
        self.assertEqual(set(json.loads(Path(report["snapshot"]).read_text())
                             ["reviewers"]), {"sol-tiebreak"})

    def test_a_failed_probe_writes_no_entry(self):
        report, _probe = self.run_resolver(probe=lambda s, c, t: (None, "HTTP 500"))
        self.assertIn(("sol", "PROBE_FAILED"),
                      {(l["seat"], l["status"]) for l in report["seats"]})
        self.assertEqual(json.loads(Path(report["snapshot"]).read_text())["reviewers"],
                         {})

    def test_dry_run_probes_and_writes_nothing(self):
        report, probe = self.run_resolver("--dry-run")
        self.assertIsNone(report["snapshot"])
        self.assertFalse(probe.called)
        self.assertFalse((self.state / "hanig-review-gate").exists())

    def exit_code(self, **patches):
        argv = ["resolve_models.py", "--project", str(self.project)]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(RM, "list_ids", side_effect=AssertionError("listed")), \
                mock.patch.object(RM, "probe_reviewer", side_effect=AssertionError("probed")), \
                redirect_stderr(io.StringIO()) as err, redirect_stdout(io.StringIO()):
            with mock.patch.multiple(RM.R, **patches):
                with self.assertRaises(SystemExit) as stopped:
                    RM.main()
        return stopped.exception.code, err.getvalue()

    def test_any_configuration_error_stops_before_any_seat_starts(self):
        roster = [dict(r) for r in shipped()[0]]
        last = next(r for r in reversed(roster) if r.get("family"))
        last["family"] = dict(last["family"], separator="_")
        code, err = self.exit_code(load_reviewers=mock.Mock(return_value=roster))
        self.assertEqual(code, 4)
        self.assertIn("separator", err)
        roster = [dict(r) for r in shipped()[0]]
        last = next(r for r in reversed(roster) if r.get("family"))
        last["family"] = dict(last["family"], major=last["family"]["major"] + 1)
        code, err = self.exit_code(load_reviewers=mock.Mock(return_value=roster))
        self.assertEqual(code, 4)
        self.assertIn("not in its family", err)

    def test_a_reviewer_family_must_list_from_its_own_provider(self):
        roster = [dict(r) for r in shipped()[0]]
        seat = next(r for r in roster if r["name"] == "sol")
        seat["family"] = dict(seat["family"], listing="openrouter")
        code, err = self.exit_code(load_reviewers=mock.Mock(return_value=roster))
        self.assertEqual(code, 4)
        self.assertIn("must equal its provider", err)

    def test_malformed_agents_json_is_a_configuration_error(self):
        bad = Path(self.tmp.name) / "agents.json"
        for body in ("{", "[]", "null", '{"default": 3}', json.dumps(
                {"default": {"provider": "codex", "family": shipped()[1]["default"]["family"]},
                 "thinking_by_model": {}})):
            with self.subTest(body=body):
                bad.write_text(body)
                code, err = self.exit_code(AGENTS_CONFIG=bad)
                self.assertEqual(code, 4, err)

    def test_agents_json_rules_match_swarm_exactly(self):
        import swarm as S
        good = shipped()[1]
        cases = [good, [], {"default": 3, "thinking_by_model": {}},
                 dict(good, thinking_by_model={}),
                 dict(good, thinking_by_model={"codex/m": 7}),
                 dict(good, thinking_by_model={"codex": "high"}),
                 dict(good, thinking_by_model={"codex/m ": "high"}),
                 dict(good, default=dict(good["default"], provider="codex")),
                 dict(good, default=dict(good["default"], provider="codex/m/")),
                 dict(good, default=dict(good["default"], thinking=" high ")),
                 dict(good, default=dict(good["default"], thinking="")),
                 dict(good, default={"provider": "codex/m", "thinking": "high"})]
        path = Path(self.tmp.name) / "agents.json"
        for body in cases:
            with self.subTest(body=body):
                path.write_text(json.dumps(body))
                try:
                    S.load_agent_routing(path)
                    swarm_ok = True
                except SystemExit:
                    swarm_ok = False
                try:
                    RM.validate_config([], body)
                    resolver_ok = True
                except MF.FamilyError:
                    resolver_ok = False
                self.assertEqual(resolver_ok, swarm_ok)

    def test_the_state_location_is_checked_before_any_canary_exists(self):
        inside = self.project / ".state"
        argv = ["resolve_models.py", "--project", str(self.project)]
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(inside)}), \
                mock.patch.object(sys, "argv", argv), \
                mock.patch.object(RM, "list_ids", side_effect=AssertionError("listed")), \
                redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit) as stopped:
                RM.main()
        self.assertEqual(stopped.exception.code, 4)
        self.assertIn("inside project", err.getvalue())
        self.assertFalse(inside.exists())

    def test_the_agent_default_is_probed_at_its_effective_thinking(self):
        listings = dict(self.LISTINGS, **{"paseo:codex": ["gpt-6-astra", "gpt-6.1-astra"]})
        agents = shipped()[1]
        want = agents["thinking_by_model"]["codex/gpt-6-astra"]
        argv = ["resolve_models.py", "--project", str(self.project), "--json",
                "--only", "agent-default"]
        probe = mock.Mock(return_value=({"Provider": "codex", "Model": "gpt-6.1-astra",
                                         "Thinking": want}, None))
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(RM, "list_ids", side_effect=lambda l, t: listings[l]), \
                mock.patch.object(RM, "probe_agent", probe), \
                redirect_stdout(io.StringIO()) as out:
            with self.assertRaises(SystemExit):
                RM.main()
        self.assertEqual(probe.call_args.args[:3], ("codex", "gpt-6.1-astra", want))
        snapshot = json.loads(Path(json.loads(out.getvalue())["snapshot"]).read_text())
        self.assertEqual(snapshot["agent_default"]["provider"], "codex/gpt-6.1-astra")

    def test_a_failed_write_removes_the_older_snapshot(self):
        self.run_resolver()
        path = MF.snapshot_path(self.project)
        self.assertTrue(path.exists())
        argv = ["resolve_models.py", "--project", str(self.project)]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(RM.R, "load_reviewers", return_value=fixture_roster()), \
                mock.patch.object(RM, "list_ids", side_effect=lambda l, t: self.LISTINGS[l]), \
                mock.patch.object(RM, "probe_reviewer", return_value=(None, "down")), \
                mock.patch.object(RM.MF, "write_snapshot", side_effect=OSError("disk full")), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as stopped:
                RM.main()
        self.assertEqual(stopped.exception.code, 4)
        self.assertFalse(path.exists())

    def test_a_partial_run_keeps_the_other_seats(self):
        self.run_resolver()
        report, _probe = self.run_resolver("--only", "sol")
        snapshot = json.loads(Path(report["snapshot"]).read_text())
        self.assertEqual(set(snapshot["reviewers"]), {"sol", "sol-tiebreak"})


if __name__ == "__main__":
    unittest.main()
