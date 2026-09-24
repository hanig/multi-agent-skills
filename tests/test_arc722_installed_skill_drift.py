"""ARC-722: installed skill claims are dispatch diagnostics, never authority."""
import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "hanig-swarm" / "scripts"))
import swarm as S
import worktree as W
from tests.test_attempt_worktrees import ENV, FakePaseo, code_unit, git, repo_at


class TestInstalledSkillDrift(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.enter_patch(mock.patch.dict(os.environ, {"HOME": str(self.home)}))
        self.repo = repo_at(self.tmp / "repo")
        remote = self.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)],
                       check=True, env=ENV)
        git(self.repo, "remote", "add", "origin", str(remote))
        git(self.repo, "branch", "-M", "main")
        git(self.repo, "push", "-qu", "origin", "main")
        self.base = git(self.repo, "rev-parse", "HEAD")
        self.unit = code_unit(self.repo)
        self.state = {"units": {}}
        self.state_dir = self.tmp / "state"
        self.fake = FakePaseo(self, self.tmp / "managed", S.U.run)
        self.enter_patch(mock.patch.object(S.U, "run", self.fake))

    def enter_patch(self, patch):
        value = patch.start()
        self.addCleanup(patch.stop)
        return value

    def install(self, store, name, version):
        skill = self.home / store / "skills" / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("fixture skill\n")
        marker = skill / ".installed-by-multi-agent-skills"
        marker.write_text(f"repo=multi-agent-skills\nsource_version={version}\n")
        return marker

    def dispatch(self, name="attempt", dispatch_source=None):
        attempt = self.tmp / "runs" / "code" / name
        attempt.mkdir(parents=True)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            agent, error = S._submit(self.unit, str(attempt), False,
                                     self.state, str(self.state_dir),
                                     dispatch_source=dispatch_source)
        self.assertIsNone(error)
        self.assertEqual(agent, "agent-" + name)
        facts = self.state["units"]["code"]["attempt_launch_facts"][name]
        return attempt, facts, stderr.getvalue()

    def advance_repo_base(self):
        old = self.base
        (self.repo / "next.txt").write_text("next base\n")
        git(self.repo, "add", "next.txt")
        git(self.repo, "commit", "-qm", "next base")
        git(self.repo, "push", "-q", "origin", "main")
        self.base = git(self.repo, "rev-parse", "HEAD")
        return old

    def test_uncached_dispatch_records_each_store_and_warns_on_drift(self):
        stale = "f" * 40
        marker = self.install(".agents", "hanig-review-gate", stale)
        matching = self.install(".claude", "hanig-review-gate", self.base)
        self.install(".agents", "paseo", stale)
        attempt, facts, warning = self.dispatch()

        self.assertIn("WARNING: unit 'code': installed skill drift", warning)
        self.assertIn(str(marker.parent), warning)
        self.assertIn(stale, warning)
        self.assertIn(self.base, warning)
        self.assertNotIn(str(matching.parent), warning)
        self.assertEqual(warning.count("installed skill drift"), 1)
        versions = facts["installed_skills"]["skills"]
        self.assertEqual([(row["path"], row["source_version"]) for row in versions],
                         [(str(marker.parent), stale),
                          (str(matching.parent), self.base)])
        self.assertEqual(facts["installed_skills"]["errors"], [])
        persisted = S.load_state(self.state_dir)
        intent = persisted["units"]["code"]["attempt_launch_intents"][attempt.name]
        saved = persisted["units"]["code"]["attempt_launch_facts"][attempt.name]
        audit = json.loads(W.launch_record_path(attempt).read_text())
        for record in (intent, saved, audit):
            self.assertEqual(record["installed_skills"], facts["installed_skills"])

    def test_cached_source_dispatch_records_snapshot_and_warns(self):
        stale = "f" * 40
        marker = self.install(".agents", "hanig-swarm", stale)
        target, error = S._resolve_dispatch_target(self.unit)
        self.assertIsNone(error)
        source, error = S._dispatch_source_identity(self.unit, target)
        self.assertIsNone(error)
        with mock.patch.object(S, "_resolve_dispatch_target",
                               side_effect=AssertionError("must use cached source")):
            attempt, facts, warning = self.dispatch(dispatch_source=source)
        self.assertIn("installed skill drift", warning)
        self.assertIn(str(marker.parent), warning)
        self.assertIn(stale, warning)
        self.assertIn(self.base, warning)
        saved = S.load_state(self.state_dir)["units"]["code"]
        self.assertEqual(saved["attempt_launch_facts"][attempt.name]
                         ["installed_skills"], facts["installed_skills"])

    def test_redispatch_warns_from_persisted_snapshot_without_resampling(self):
        stale = "f" * 40
        marker = self.install(".agents", "hanig-swarm", stale)
        for cached in (False, True):
            with self.subTest(cached=cached):
                name = "redispatch-" + str(cached)
                attempt = self.tmp / "runs" / "code" / name
                error, anchor = S._capture_code_launch(str(attempt), self.unit)
                self.assertIsNone(error)
                self.state = {"units": {"code": {
                    "attempt_launch_intents": {name: anchor["intent"]}}}}
                S.save_state(self.state_dir, self.state)
                self.state = S.load_state(self.state_dir)
                marker.write_text("source_version=" + self.base + "\n")
                source = None
                if cached:
                    target, error = S._resolve_dispatch_target(self.unit)
                    self.assertIsNone(error)
                    source, error = S._dispatch_source_identity(self.unit, target)
                    self.assertIsNone(error)
                with mock.patch.object(S, "_installed_skill_snapshot",
                                       side_effect=AssertionError("must not resample")):
                    _attempt, facts, warning = self.dispatch(name, source)
                self.assertIn("installed skill drift", warning)
                self.assertIn(stale, warning)
                self.assertEqual(facts["installed_skills"],
                                 anchor["intent"]["installed_skills"])
                marker.write_text("source_version=" + stale + "\n")

    def test_warning_names_scanned_stores_and_excludes_custom_stores(self):
        self.install(".agents", "hanig-swarm", "f" * 40)
        self.install("custom", "hanig-custom", "e" * 40)
        project_skill = self.repo / ".agents" / "skills" / "hanig-project"
        project_skill.mkdir(parents=True)
        (project_skill / "SKILL.md").write_text("project fixture\n")
        (project_skill / ".installed-by-multi-agent-skills").write_text(
            "source_version=" + "d" * 40 + "\n")
        _attempt, facts, warning = self.dispatch()
        self.assertEqual([entry["skill"] for entry in facts["installed_skills"]["skills"]],
                         ["hanig-swarm"])
        self.assertIn("Only ~/.agents/skills and ~/.claude/skills are scanned", warning)
        self.assertIn("project/custom stores are not scanned", warning)

    def test_equal_versions_dispatch_without_warning(self):
        for store in (".agents", ".claude"):
            self.install(store, "hanig-swarm", self.base)
        _attempt, facts, warning = self.dispatch()
        self.assertEqual(warning, "")
        self.assertEqual(len(facts["installed_skills"]["skills"]), 2)

    def test_installer_abbreviation_is_resolved_without_changing_raw_claim(self):
        short = git(self.repo, "rev-parse", "--short", "HEAD")
        self.install(".agents", "hanig-swarm", short)
        _attempt, facts, warning = self.dispatch()
        self.assertEqual(warning, "")
        entry = facts["installed_skills"]["skills"][0]
        self.assertEqual(entry["source_version"], short)
        self.assertEqual(entry["resolved_commit"], self.base)

    def test_unresolved_abbreviation_is_not_treated_as_equal(self):
        short = self.base[:7]
        self.install(".agents", "hanig-swarm", short)
        real_git = S._git

        def ambiguous(repo, *args, **kwargs):
            if args == ("rev-parse", "--disambiguate=" + short):
                return 128, "", "ambiguous revision"
            return real_git(repo, *args, **kwargs)

        with mock.patch.object(S, "_git", ambiguous):
            _attempt, facts, warning = self.dispatch()
        self.assertIn(short, warning)
        self.assertIn(self.base, warning)
        self.assertNotIn("resolved_commit", facts["installed_skills"]["skills"][0])

    def test_uppercase_object_names_resolve_without_rewriting_marker_claims(self):
        if self.base.upper() == self.base:
            self.skipTest("fixture commit has no hex letters to uppercase")
        short = git(self.repo, "rev-parse", "--short", "HEAD")
        # Ensure the abbreviation exercises upper-case letters, even when
        # the ordinary short name happens to contain only decimal digits.
        length = max(len(short), next((i + 1 for i, c in enumerate(self.base)
                                      if c in "abcdef"), len(self.base)))
        versions = (self.base[:length].upper(), self.base.upper())
        for name, version in zip(("short", "full"), versions):
            self.install(".agents", "hanig-" + name, version)
        _attempt, facts, warning = self.dispatch()
        self.assertEqual(warning, "")
        entries = facts["installed_skills"]["skills"]
        self.assertEqual({entry["source_version"] for entry in entries},
                         set(versions))
        for entry in entries:
            self.assertEqual(entry["resolved_commit"], self.base)

    def test_broken_or_closed_warning_stream_does_not_abort_dispatch(self):
        self.install(".agents", "hanig-swarm", "f" * 40)
        self.install(".agents", "hanig-unknown", self.base).unlink()
        broken = mock.Mock()
        broken.write.side_effect = BrokenPipeError("fixture broken stderr")
        closed = io.StringIO()
        closed.close()
        for index, stream in enumerate((broken, closed)):
            with self.subTest(index=index):
                attempt = self.tmp / "runs" / "code" / ("stderr-" + str(index))
                attempt.mkdir(parents=True)
                with contextlib.redirect_stderr(stream):
                    agent, error = S._submit(
                        self.unit, str(attempt), False,
                        self.state, str(self.state_dir))
                self.assertIsNone(error)
                self.assertEqual(agent, "agent-" + attempt.name)
                facts = self.state["units"]["code"]["attempt_launch_facts"][attempt.name]
                self.assertEqual(facts["installed_skills"]["skills"][0]["source_version"],
                                 "f" * 40)

    def test_dirty_marker_is_not_normalized_to_the_base(self):
        version = git(self.repo, "rev-parse", "--short", "HEAD") + "-dirty"
        self.install(".agents", "hanig-swarm", version)
        _attempt, facts, warning = self.dispatch()
        self.assertIn(version, warning)
        self.assertIn(self.base, warning)
        self.assertEqual(facts["installed_skills"]["skills"][0]["source_version"],
                         version)

    def test_hash_named_refs_cannot_hide_installed_version_drift(self):
        old = self.advance_repo_base()
        short = git(self.repo, "rev-parse", "--short", old)
        self.install(".agents", "hanig-swarm", short)
        for kind in ("branch", "tag"):
            with self.subTest(kind=kind):
                if kind == "branch":
                    git(self.repo, "branch", short, self.base)
                else:
                    git(self.repo, "tag", "-a", short, "-m", "shadow", self.base)
                _attempt, facts, warning = self.dispatch("stale-" + kind)
                self.assertIn(short, warning)
                self.assertIn(self.base, warning)
                entry = facts["installed_skills"]["skills"][0]
                self.assertEqual(entry["source_version"], short)
                self.assertNotIn("resolved_commit", entry)

    def test_hash_named_refs_cannot_create_false_drift(self):
        old = self.advance_repo_base()
        short = git(self.repo, "rev-parse", "--short", "HEAD")
        self.install(".agents", "hanig-swarm", short)
        for kind in ("branch", "tag"):
            with self.subTest(kind=kind):
                if kind == "branch":
                    git(self.repo, "branch", short, old)
                else:
                    git(self.repo, "tag", "-a", short, "-m", "shadow", old)
                _attempt, facts, warning = self.dispatch("matching-" + kind)
                self.assertEqual(warning, "")
                self.assertEqual(facts["installed_skills"]["skills"][0]["resolved_commit"],
                                 self.base)

    def test_ambiguous_commit_prefix_does_not_establish_equality(self):
        prefix = self.base[:4]
        tree = git(self.repo, "rev-parse", "HEAD^{tree}")
        # Create a real descendant commit sharing the four-hex prefix.
        # Hash locally before writing the one matching fixture object.
        for index in range(2000000):
            payload = (f"tree {tree}\nparent {self.base}\n"
                       "author t <t@x> 1700000000 +0000\n"
                       "committer t <t@x> 1700000000 +0000\n\n"
                       f"collision {index}\n").encode()
            digest = hashlib.sha1(b"commit " + str(len(payload)).encode()
                                  + b"\0" + payload).hexdigest()
            if digest.startswith(prefix):
                break
        else:
            self.fail("bounded fixture collision search exhausted")
        written = subprocess.run(
            ["git", "-C", str(self.repo), "hash-object", "-w", "-t", "commit", "--stdin"],
            input=payload, stdout=subprocess.PIPE, check=True, env=ENV)
        self.assertEqual(written.stdout.decode().strip(), digest)
        self.assertNotEqual(digest, self.base)
        candidates = git(self.repo, "rev-parse", "--disambiguate=" + prefix).splitlines()
        self.assertIn(self.base, candidates)
        self.assertIn(digest, candidates)
        self.install(".agents", "hanig-swarm", prefix)
        _attempt, facts, warning = self.dispatch()
        self.assertIn(prefix, warning)
        self.assertIn(self.base, warning)
        self.assertNotIn("resolved_commit", facts["installed_skills"]["skills"][0])

    def test_incomplete_object_lookup_stays_advisory_and_is_not_a_commit(self):
        short = git(self.repo, "rev-parse", "--short", "HEAD")
        self.install(".agents", "hanig-swarm", short)
        real_git = S._git
        results = [(0, "", ""), (0, "malformed", ""),
                   (0, self.base + "\n" + "f" * 40, ""),
                   (1, self.base, "lookup failed"), (0, "f" * 40, "")]
        for index, result in enumerate(results):
            with self.subTest(result=result):
                def incomplete(repo, *args, **kwargs):
                    if args == ("rev-parse", "--disambiguate=" + short):
                        return result
                    return real_git(repo, *args, **kwargs)

                with mock.patch.object(S, "_git", incomplete):
                    _attempt, facts, warning = self.dispatch("lookup-" + str(index))
                self.assertIn(short, warning)
                self.assertIn(self.base, warning)
                entry = facts["installed_skills"]["skills"][0]
                self.assertEqual(entry["source_version"], short)
                self.assertNotIn("resolved_commit", entry)

    def test_bad_missing_and_nonregular_markers_remain_advisory(self):
        for name, payload in (("invalid", b"\xff"), ("empty", b"source_version=\n"),
                              ("duplicate", b"source_version=a\nsource_version=b\n"),
                              ("large", b"x" * 65537), ("missing", None),
                              ("fifo", None)):
            marker = self.install(".agents", "hanig-" + name, self.base)
            if payload is None:
                marker.unlink()
                if name == "fifo":
                    os.mkfifo(marker)
            else:
                marker.write_bytes(payload)
        _attempt, facts, warning = self.dispatch()
        self.assertEqual(warning.count("installed skill audit incomplete"), 6)
        entries = facts["installed_skills"]["skills"]
        self.assertEqual(len(entries), 6)
        for entry in entries:
            self.assertIsNone(entry["source_version"])
            self.assertTrue(entry["error"])
            self.assertIn(repr(entry["marker"]), warning)
            self.assertIn(repr(entry["error"]), warning)

    def test_unreadable_root_is_recorded_and_does_not_refuse_dispatch(self):
        self.install(".claude", "hanig-swarm", self.base)
        unreadable = self.home / ".agents" / "skills"
        real_iterdir = Path.iterdir

        def denied(path):
            if path == unreadable:
                raise PermissionError("fixture denial")
            return real_iterdir(path)

        with mock.patch.object(Path, "iterdir", denied):
            _attempt, facts, warning = self.dispatch()
        self.assertIn("installed skill audit incomplete", warning)
        self.assertIn(str(unreadable), warning)
        self.assertIn("fixture denial", warning)
        self.assertEqual(facts["installed_skills"]["errors"],
                         [{"path": str(unreadable), "error": "fixture denial"}])
        self.assertEqual(len(facts["installed_skills"]["skills"]), 1)

    def test_absent_stores_are_quiet_but_dangling_store_links_warn(self):
        _attempt, facts, warning = self.dispatch("absent")
        self.assertEqual(warning, "")
        self.assertEqual(facts["installed_skills"], {"skills": [], "errors": []})
        for store in (".agents", ".claude"):
            with self.subTest(store=store):
                root = self.home / store / "skills"
                root.parent.mkdir(parents=True)
                root.symlink_to(self.tmp / "missing-store", target_is_directory=True)
                _attempt, facts, warning = self.dispatch("dangling-" + store[1:])
                self.assertIn("installed skill audit incomplete", warning)
                self.assertIn(str(root), warning)
                errors = facts["installed_skills"]["errors"]
                self.assertEqual(len(errors), 1)
                self.assertEqual(errors[0]["path"], str(root))
                self.assertIn("No such file or directory", errors[0]["error"])
                root.unlink()

    def test_link_sidecar_takes_precedence_over_linked_copy_marker(self):
        sys.path.insert(0, str(ROOT / "lib"))
        self.addCleanup(sys.path.remove, str(ROOT / "lib"))
        import skill_lifecycle as lifecycle
        source = self.tmp / "link-source"
        source.mkdir()
        (source / "SKILL.md").write_text("linked fixture\n")
        source_marker = source / ".installed-by-multi-agent-skills"
        source_marker.write_text("source_version=" + "f" * 40 + "\n")
        destination = self.home / ".agents" / "skills" / "hanig-swarm"
        target = lifecycle.LifecycleTarget(
            "hanig-swarm", source, destination, "authored", mode="link",
            consumers=("codex",), source_version=self.base)
        results = lifecycle.install((target,))
        self.assertEqual(results[0].status, "installed")
        marker = lifecycle.provenance_path(destination)
        before = marker.read_bytes()
        _attempt, facts, warning = self.dispatch()
        self.assertEqual(warning, "")
        entry = facts["installed_skills"]["skills"][0]
        self.assertEqual(entry["source_version"], self.base)
        self.assertEqual(entry["marker"], str(marker))
        self.assertEqual(marker.read_bytes(), before)
        self.assertEqual(source_marker.read_text(),
                         "source_version=" + "f" * 40 + "\n")
        self.assertTrue(destination.is_symlink())

    def test_link_install_in_relocated_symlinked_store_has_visible_advisory(self):
        sys.path.insert(0, str(ROOT / "lib"))
        self.addCleanup(sys.path.remove, str(ROOT / "lib"))
        import skill_lifecycle as lifecycle
        source = self.tmp / "link-source"
        source.mkdir()
        (source / "SKILL.md").write_text("linked fixture\n")
        store = self.home / ".agents" / "skills"
        destination = store / "hanig-swarm"
        target = lifecycle.LifecycleTarget(
            "hanig-swarm", source, destination, "authored", mode="link",
            consumers=("codex",), source_version=self.base)
        self.assertEqual(lifecycle.install((target,))[0].status, "installed")
        marker = lifecycle.provenance_path(destination)
        original = marker.read_bytes()
        moved_store = self.tmp / "moved-skills"
        store.rename(moved_store)
        store.symlink_to(moved_store, target_is_directory=True)

        attempt, facts, warning = self.dispatch()

        entry = facts["installed_skills"]["skills"][0]
        self.assertIsNone(entry["source_version"])
        self.assertIn("FileNotFoundError", entry["error"])
        self.assertIn("installed skill audit incomplete", warning)
        self.assertIn(repr(entry["marker"]), warning)
        self.assertIn(repr(entry["error"]), warning)
        self.assertIn("symlinked skill stores may hide link-install sidecars", warning)
        self.assertEqual(marker.read_bytes(), original)
        persisted = S.load_state(self.state_dir)["units"]["code"]
        self.assertEqual(persisted["attempt_launch_facts"][attempt.name]
                         ["installed_skills"], facts["installed_skills"])

    def test_snapshot_is_durable_before_paseo_and_survives_recovery(self):
        stale = "f" * 40
        marker = self.install(".agents", "hanig-swarm", stale)
        observed = []

        def launch(argv, **kwargs):
            if argv[:2] == ["paseo", "run"]:
                saved = S.load_state(self.state_dir)
                observed.append(saved["units"]["code"]["attempt_launch_intents"]
                                ["attempt"]["installed_skills"])
                marker.write_text("source_version=" + self.base + "\n")
            return self.fake(argv, **kwargs)

        with mock.patch.object(S.U, "run", launch):
            attempt, facts, warning = self.dispatch()
        self.assertEqual(observed, [facts["installed_skills"]])
        self.assertIn(stale, warning)
        audit_path = W.launch_record_path(attempt)
        original = audit_path.read_bytes()
        self.state = S.load_state(self.state_dir)
        del self.state["units"]["code"]["attempt_launch_facts"]
        with mock.patch.object(S, "_installed_skill_snapshot",
                               side_effect=AssertionError("must not resample")):
            error = S._complete_code_launch(
                self.state, self.unit, str(attempt), facts["execution_workspace"],
                recovery=True)
        self.assertIsNone(error)
        recovered = self.state["units"]["code"]["attempt_launch_facts"]["attempt"]
        self.assertEqual(recovered["installed_skills"], observed[0])
        self.assertEqual(audit_path.read_bytes(), original)

    def test_legacy_intent_keeps_absence_and_existing_audit_bytes(self):
        attempt, facts, _warning = self.dispatch()
        us = self.state["units"]["code"]
        del us["attempt_launch_intents"][attempt.name]["installed_skills"]
        legacy = dict(facts)
        del legacy["installed_skills"]
        payload = S._code_launch_record_payload(legacy)
        W.launch_record_path(attempt).write_bytes(payload)
        us["attempt_record_seals"][attempt.name] = hashlib.sha256(payload).hexdigest()
        del us["attempt_launch_facts"]
        self.install(".agents", "hanig-swarm", "f" * 40)
        error = S._complete_code_launch(
            self.state, self.unit, str(attempt), facts["execution_workspace"],
            recovery=True)
        self.assertIsNone(error)
        self.assertNotIn("installed_skills", us["attempt_launch_facts"][attempt.name])
        self.assertEqual(W.launch_record_path(attempt).read_bytes(), payload)

    def test_skill_claims_do_not_control_production_judgment(self):
        self.install(".agents", "hanig-swarm", "f" * 40)
        attempt, facts, _warning = self.dispatch()
        workspace = Path(facts["execution_workspace"])
        (workspace / "result.txt").write_text("produced\n")
        git(workspace, "add", "result.txt")
        git(workspace, "commit", "-qm", "work")
        git(workspace, "push", "origin", "HEAD:" + facts["judgment_ref"])
        for inventory in (None, {"skills": "nonsense"},
                          {"skills": [{"source_version": "unknown"}]}):
            with self.subTest(inventory=inventory):
                facts["installed_skills"] = inventory
                audit = json.loads(W.launch_record_path(attempt).read_text())
                audit["installed_skills"] = inventory
                W.launch_record_path(attempt).write_text(json.dumps(audit))
                produced, head, why = W.judge_detail(
                    S.U.run, str(attempt), self.unit, facts)
                self.assertTrue(produced, why)
                self.assertEqual(head, git(workspace, "rev-parse", "HEAD"))


if __name__ == "__main__":
    unittest.main()
