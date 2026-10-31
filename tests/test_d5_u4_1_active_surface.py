"""U4.1 entry refusal, first-effect capture, aliases and installed packages."""
from contextlib import nullcontext
import argparse
import ast
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.test_d5_u4_0b1_issuance import no_effects

ROOT = Path(__file__).resolve().parents[1]
SWARM = ROOT / "skills/hanig-swarm/scripts"
OPERATOR = ROOT / "skills/hanig-orchestrate/scripts"
sys.path.insert(0, str(SWARM))
sys.path.insert(0, str(OPERATOR))
import swarm as S
import merge_unit as M
import d5_activation as activation
from d5_readiness import MARKER
from tests.fixtures.d5.ready import ready_installation

FIXTURE = ROOT / "tests/fixtures/d5/u41-inactive-effects.json"
MANIFEST = ROOT / "tests/fixtures/d5/integration-manifest.json"
S_ENTRIES = ("_submit", "maybe_continue", "advance", "_start_code_terminal_watchers",
             "cmd_watch_code_terminal", "_prepare_command_paths", "cmd_continue",
             "cmd_validate", "cmd_run", "cmd_advance", "cmd_stop_unit", "promote",
             "cmd_promote", "_integration_verification_fence", "cmd_verify", "cmd_merge",
             "cmd_seq_floor", "cmd_outbox", "main")
M_ENTRIES = ("run", "current_intent", "abandon", "reconcile", "verify_integration",
             "print_tracker_drain", "main")


def normalize_capture(captured, paths=()):
    """Parse command records before mapping paths; retain other effect data.

    Both the untouched embedded base and live captures use this representation.
    Only '+ ' records emitted by shlex.join are interpreted as commands.
    """
    captured = json.loads(json.dumps(captured, default=str))
    lines = iter(captured["output"].splitlines(keepends=True))
    records = []
    for line in lines:
        if not line.startswith("+ "):
            records.append(line)
            continue
        while True:
            ending = "\n" if line.endswith("\n") else ""
            body = line[2:-1] if ending else line[2:]
            try:
                tokens = shlex.split(body)
                break
            except ValueError as exc:
                # shlex.join can quote a token containing a literal newline.
                following = next(lines, None) if str(exc) == "No closing quotation" else None
                if following is None:
                    raise ValueError("malformed M6 command record") from exc
                line += following
        if not tokens or not tokens[0]:
            raise ValueError("malformed M6 command record: empty command")
        records.append({"argv": tokens, "ending": ending})
    captured["output"] = records
    mappings = dict(paths)
    pattern = (re.compile("|".join(re.escape(p) for p in
               sorted(mappings, key=len, reverse=True))) if mappings else None)

    def normalize(value):
        if isinstance(value, str):
            return pattern.sub(lambda m: mappings[m.group()], value) if pattern else value
        if isinstance(value, list):
            return [normalize(v) for v in value]
        if isinstance(value, dict):
            return {normalize(k): normalize(v) for k, v in value.items()}
        return value
    return normalize(captured)


class SurfaceFixture:
    """Synthetic local authority; never talks to a forge, tracker or worker.

    Stop at the first attempted external effect. The committed base captures
    pin that observable prefix and outcome for *each* changed legacy entry.
    In active mode the same fixtures must refuse before reaching that prefix.
    Full legacy success/recovery coverage remains in test_swarm/test_merge_unit.
    """
    def __init__(self, directory, swarm=S, operator=M):
        self.directory, self.s, self.m = Path(directory), swarm, operator
        # No executable is needed before these captures' first trapped effect.
        self.tools = self.directory / "tools"
        self.tools.mkdir()
        self.state_dir = self.directory / "state"
        self.state_dir.mkdir()
        self.attempt = self.directory / "runs/u/a1"
        self.attempt.mkdir(parents=True)
        self.plan_path = self.directory / "plan.json"
        self.unit = {"id": "u", "kind": "slurm", "command": "true", "outputs": ["out"], "runtime": "none"}
        self.plan = {"name": "fixture", "units": [self.unit]}
        self.plan_path.write_text(json.dumps(self.plan))
        self.us = {"state": "RUNNING", "attempt_dir": str(self.attempt), "job_id": "agent"}
        self.state = {"units": {"u": self.us}, "root": str(self.directory / "runs")}
        self.binding = {"unit": "u", "attempt": "a1", "head": "a" * 40,
                        "repo": "https://github.com/example/fixture", "target": "main"}
        self.args = argparse.Namespace(
            plan=str(self.plan_path), state_dir=str(self.state_dir),
            root=str(self.directory / "runs"), unit="u", attempt=str(self.attempt),
            agent="agent", dry_run=False, max_new_dispatches=0,
            approver="Operator", approve=True, reason="test", command="run", json=True,
            check_seeds=True, survey=None, claim="tests-pass", verifier="fixture",
            path="verifier.py", head="a" * 40, target="main", merged_as="b" * 40,
            repo=self.binding["repo"], pr=7, method="squash", floor=1,
            record_receipt=None, accept_weak=False, new_id="next", write=True,
            prompt_file="prompt.txt", head_ref=None, arg=[], timeout=30, all=True,
            verify_integration=False, verification_timeout=30, abandon_intent=None,
            retrieve_remote_evidence=False, remote_recovery_attestation=None,
            allow_unchecked_scope=None)
        self.margv = [self.args.plan, "--state-dir", self.args.state_dir,
                      "--unit", "u", "--pr", "7", "--approver", "Operator"]

    def tree(self):
        return {str(p.relative_to(self.directory)): p.read_bytes().hex()
                for p in self.directory.rglob("*") if p.is_file()}

    def case(self, stack, tool, name):
        s, m, a = self.s, self.m, self.args
        patch = lambda owner, key, value: stack.enter_context(
            mock.patch.object(owner, key, return_value=value))
        # Read-only inputs are supplied locally; effect primitives stay real
        # until no_effects instruments them. No entry or guard is mocked.
        patch(s.SR, "apply_agent_resolution", None)
        patch(os, "urandom", b"0" * 16)
        patch(s.CP, "resolve_paths", (self.state_dir, self.directory / "runs", None))
        patch(s.CP, "project_context", self.directory)
        patch(s.SS, "load_state", self.state)
        patch(s.SE, "_receipt_reason", s.U.REASON_NO_OUTPUTS)
        patch(s.U, "slurm_record_policy", (False, None))
        if tool == "swarm":
            calls = {
                "_submit": lambda: s._submit(self.unit, self.attempt, False, {}, self.state_dir),
                "maybe_continue": lambda: s.maybe_continue(self.state_dir, "u",
                    {"kind": "code", "continuation": {"max": 1}}, self.us, [], self.state),
                "advance": lambda: s.advance({"units": []}, {"units": {}}, self.state_dir,
                                             self.directory / "runs", False),
                "_start_code_terminal_watchers": lambda: s._start_code_terminal_watchers(
                    {"units": [{"id": "u", "kind": "code"}]}, self.state, a, []),
                "_prepare_command_paths": lambda: s._prepare_command_paths(a, self.plan),
                "_integration_verification_fence": lambda: s._integration_verification_fence(
                    self.state_dir, "u", {"unit": "u"}),
                "promote": lambda: s.promote(self.plan, self.state, self.state_dir,
                                             "u", "Operator", True),
            }
            if name == "_prepare_command_paths":
                a.state_dir = None  # exercise implicit migration, not a no-op
                legacy = self.directory / ".swarm/state"
                legacy.mkdir(parents=True)
                (legacy / "swarm-state.json").write_text('{"units":{}}')
            if name in ("promote", "cmd_promote"):
                self.unit["promote_to"] = str(self.directory / "shared")
                self.us["state"] = "DONE"
                patch(s.SE, "attested_receipt", ({"state": "DONE", "outputs": {"out": {}}}, None))
                patch(s, "_redigest", (True, "unchanged"))
                self.plan_path.write_text(json.dumps(self.plan))
            if name == "cmd_continue":
                self.unit.update(kind="code", repo=str(self.directory))
                intent = dict(self.binding, repo=str(self.directory), base_commit="b" * 40,
                              branch="seed", repository_remote="origin", repository_remote_raw="origin")
                self.us["attempt_launch_intents"] = {"a1": intent}
                self.plan_path.write_text(json.dumps(self.plan))
                (self.state_dir / s.SS.STATE_FILE).write_text(json.dumps(self.state))
                patch(s.SCL, "_code_launch_intent_problem", None)
                patch(s.SP, "_resolve_dispatch_target", (dict(intent), None))
            if name == "cmd_verify":
                patch(s.SS, "trusted_launch_facts", {"repo": str(self.directory), "base_commit": "b" * 40})
                patch(s.SE, "trusted_produced_head", "a" * 40)
                patch(s.W, "validate_pinned_head", None)
            if name == "main":
                stack.enter_context(mock.patch.object(sys, "argv", ["swarm.py", "run", a.plan,
                    "--state-dir", a.state_dir, "--root", a.root]))
                return s.main
            if name == "cmd_validate":
                self.unit["seed"] = {"ref": "refs/heads/seed", "head": "a" * 40, "base": "b" * 40}
                patch(s, "validate_plan", {"units": 1, "with_deps": 0})
                patch(s.SP, "_resolve_dispatch_target", ({}, None))
                patch(s.SP, "_dispatch_source_identity", ({"repo": str(self.directory),
                    "repository_remote_raw": "origin", "repository_remote": "origin"}, None))
                return lambda: s.cmd_validate(a, self.plan)
            return calls.get(name, lambda: getattr(s, name)(a))
        # A real pending cancellation record drives the legacy repair writer.
        if name == "current_intent":
            import hashlib
            operation = hashlib.sha256(json.dumps(self.binding, sort_keys=True).encode()).hexdigest()
            p = self.state_dir / ("merge-unit-" + operation + ".cancellation-pending")
            p.write_text(json.dumps({"binding": self.binding, "root": a.root,
                                    "operation_id": operation, "phase": "merge_requested"}))
        if name in ("reconcile", "main"):
            patch(m, "authority", (self.state_dir, a.root, self.binding, "github.com",
                                   "example/fixture", {}, str(self.directory)))
        if name == "print_tracker_drain":
            binding = self.directory / ".hanig/linear-binding.json"
            binding.parent.mkdir()
            binding.write_text("{}")
            project = ROOT / "skills/hanig-project"
            sys.path.insert(0, str(project / "scripts"))
            import linear_api
            patch(linear_api, "load_key", "synthetic-fixture-key")
        calls = {
            "run": lambda: m.run(["gh", "pr", "merge", "7", "--squash"], state_dir=self.state_dir),
            "current_intent": lambda: m.current_intent(self.state_dir, self.binding, a.root),
            "abandon": lambda: m.abandon(a, self.state_dir / "intent.json", {}, {}, "op"),
            "reconcile": lambda: m.reconcile(a, self.plan),
            "verify_integration": lambda: m.verify_integration(a, str(self.directory), {
                "binding": self.binding, "target": "b" * 40, "execution_policy": None}),
            "print_tracker_drain": lambda: m.print_tracker_drain(a),
            "main": lambda: m.main(self.margv),
        }
        return calls[name]

    def observe(self, tool, name, active, broken=False):
        if active:
            ready_installation(self.directory, state_dir=self.state_dir)
            if broken:
                (self.state_dir / MARKER).unlink()
        with ExitStack() as stack:
            # Replace PATH for every entry; inherited schedulers change even
            # the inactive first-effect prefix through executable discovery.
            stack.enter_context(mock.patch.dict(os.environ, {"PATH": str(self.tools)}))
            call = self.case(stack, tool, name)
            before = self.tree()
            output = io.StringIO()
            stack.enter_context(redirect_stdout(output))
            stack.enter_context(redirect_stderr(output))
            stack.enter_context(nullcontext())
            commands, tracker_intents = [], []
            emit = self.s.SO.emit_intent
            def track(*args, **kwargs):
                tracker_intents.append([args, kwargs])
                return emit(*args, **kwargs)
            stack.enter_context(mock.patch.object(self.s.SO, "emit_intent", track))
            with no_effects() as (launches, writes):
                # NamedTemporaryFile enters io.open first in 3.12, os.open in
                # 3.9. Capture its creation request before that implementation
                # detail; all lower-level write/launch tripwires remain armed.
                def temporary(*args, **kwargs):
                    writes.append("NamedTemporaryFile")
                    raise AssertionError("unexpected NamedTemporaryFile")
                # Retain the exact requested argv as well as the primitive name.
                def launching(original):
                    def capture(command, *args, **kwargs):
                        commands.append(command)
                        return original(command, *args, **kwargs)
                    return capture
                with ExitStack() as launch_stack:
                    launch_stack.enter_context(mock.patch.object(tempfile, "NamedTemporaryFile", temporary))
                    for primitive in ("Popen", "run"):
                        launch_stack.enter_context(mock.patch.object(subprocess, primitive,
                            launching(getattr(subprocess, primitive))))
                    try:
                        result = call()
                        outcome = ["return", result]
                    except (Exception, SystemExit) as exc:
                        outcome = [type(exc).__name__, str(exc)]
            after = self.tree()
        captured = {"launches": launches, "commands": commands, "writes": writes,
                    "tracker_intents": tracker_intents, "outcome": outcome,
                    "files_changed": sorted(k for k in before.keys() | after.keys()
                                            if before.get(k) != after.get(k)),
                    "output": output.getvalue()}
        return normalize_capture(captured, (
            (str(ROOT), "<checkout>"), (str(self.directory), "<fixture>"),
            (str(Path(sys.executable).resolve()), "<python>"),
            (sys.executable, "<python>")))


class TestActiveSurface(unittest.TestCase):
    def capture(self, tool, name, active):
        with tempfile.TemporaryDirectory() as tmp:
            return SurfaceFixture(tmp).observe(tool, name, active)

    def test_inactive_effects_match_recorded_base(self):
        data = json.loads(FIXTURE.read_text())
        self.assertEqual(data["base"], "351e8d468f3ed9d36a8ca357450be0fc3239d6b4")
        expected_keys = {tool + "." + name for tool, names in
                         (("swarm", S_ENTRIES), ("operator", M_ENTRIES)) for name in names}
        self.assertEqual(set(data["captures"]), expected_keys)
        for key, expected in data["captures"].items():
            with self.subTest(entry=key):
                tool, name = key.split(".")
                self.assertEqual(self.capture(tool, name, False), normalize_capture(expected))

    def test_m6_inherited_scheduler_path_matches_recorded_base(self):
        captures = json.loads(FIXTURE.read_text())["captures"]
        with tempfile.TemporaryDirectory() as inherited:
            for name in ("sinfo", "squeue", "sbatch"):
                executable = Path(inherited) / name
                executable.write_text("#!/bin/sh\nexit 97\n")
                executable.chmod(0o755)
            inherited_path = inherited + os.pathsep + os.environ.get("PATH", "")
            with mock.patch.dict(os.environ, {"PATH": inherited_path}):
                self.assertEqual(shutil.which("sinfo"), str(Path(inherited) / "sinfo"))
                for key, expected in captures.items():
                    with self.subTest(entry=key):
                        tool, name = key.split(".")
                        self.assertEqual(self.capture(tool, name, False), normalize_capture(expected))
                        self.assertEqual(os.environ["PATH"], inherited_path)

    def test_inactive_capture_normalizes_interpreter_before_parent_paths(self):
        expected = normalize_capture(json.loads(FIXTURE.read_text())["captures"]["operator.reconcile"])
        for location in ("checkout", "fixture"):
            with self.subTest(location=location), tempfile.TemporaryDirectory() as tmp:
                fixture = SurfaceFixture(tmp)
                parent = ROOT if location == "checkout" else fixture.directory
                # Captures intercept launches; no interpreter is installed or run.
                executable = str(parent / ".u41-review-venv/bin/python")
                with mock.patch.object(sys, "executable", executable):
                    self.assertEqual(fixture.observe("operator", "reconcile", False), expected)

    def test_inactive_capture_normalizes_quoted_interpreter_path(self):
        expected = normalize_capture(json.loads(FIXTURE.read_text())["captures"]["operator.reconcile"])
        with tempfile.TemporaryDirectory() as tmp:
            fixture = SurfaceFixture(tmp)
            executable = str(ROOT / ".u41 review venv/bin/python")
            with mock.patch.object(sys, "executable", executable):
                observed = fixture.observe("operator", "reconcile", False)
        self.assertEqual(observed["commands"], expected["commands"])
        self.assertEqual(observed, expected)

    def test_inactive_capture_normalizes_quoted_checkout_and_local_venv(self):
        expected = normalize_capture(json.loads(FIXTURE.read_text())["captures"]["operator.reconcile"])
        for leaf in ("checkout space", "checkout's space", 'checkout "quote"\\path\nnext'):
            for local_venv in (False, True):
                with self.subTest(leaf=leaf, local_venv=local_venv), \
                        tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
                    checkout = Path(tmp) / leaf
                    directory = checkout / "capture space"
                    directory.mkdir(parents=True)
                    fixture = SurfaceFixture(directory)
                    # Exercise the real command builder under relocated paths.
                    # Launches are trapped; no checkout or interpreter is installed.
                    stack.enter_context(mock.patch(__name__ + ".ROOT", checkout))
                    stack.enter_context(mock.patch.object(M, "SWARM",
                        str(checkout / "skills/hanig-swarm/scripts/swarm.py")))
                    if local_venv:
                        stack.enter_context(mock.patch.object(sys, "executable",
                            str(checkout / ".venv space/bin/python")))
                    self.assertEqual(fixture.observe("operator", "reconcile", False), expected)

    def test_m6_rejects_malformed_command_records(self):
        for record in ("+ 'unterminated\n", "+ echo \\", "+ \n", "+ ''\n"):
            with self.subTest(record=record), tempfile.TemporaryDirectory() as tmp:
                fixture = SurfaceFixture(tmp)
                with mock.patch.object(fixture, "case", return_value=lambda: print(record, end="")):
                    with self.assertRaisesRegex(ValueError, "malformed M6 command record"):
                        fixture.observe("operator", "run", False)

    def test_m6_normalizer_retains_noncommand_effects_and_token_boundaries(self):
        captured = json.loads(FIXTURE.read_text())["captures"]["operator.run"]
        captured["output"] = "before 'quoted'\n+ tool 'two words' ''\nafter  spaced\n"
        expected = dict(captured, output=["before 'quoted'\n",
            {"argv": ["tool", "two words", ""], "ending": "\n"}, "after  spaced\n"])
        self.assertEqual(normalize_capture(captured), expected)
        for field, value in (("writes", ["extra"]), ("launches", []),
                ("tracker_intents", ["extra"]), ("outcome", ["return", 0]),
                ("files_changed", ["extra"]), ("commands", [["tool", "two", "words"]]),
                ("output", "before 'quoted'\n+ tool two words ''\nafter  spaced\n")):
            with self.subTest(field=field):
                self.assertNotEqual(normalize_capture(dict(captured, **{field: value})), expected)

    def test_premature_activation_direct_entries_before_first_effect(self):
        for tool, names in (("swarm", S_ENTRIES), ("operator", M_ENTRIES)):
            for name in names:
                with self.subTest(tool=tool, entry=name):
                    observed = self.capture(tool, name, True)
                    self.assertEqual(observed["launches"], [])
                    self.assertEqual(observed["writes"], [])
                    self.assertEqual(observed["files_changed"], [])
                    self.assertIn("D5 active:", str(observed["outcome"]) + str(observed["output"]))
                    if tool == "operator" and name == "main":
                        self.assertEqual(observed["outcome"], ["return", 1])

    def test_cli_aliases_and_default_refusal(self):
        # Real parsers supply required values; no parser error counts as refusal.
        from tests.test_d5_integration import parser_for
        manifest = json.loads(MANIFEST.read_text())
        parser = parser_for(manifest["tools"]["swarm.py"])
        subs = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        state_dir = ready_installation(temporary.name)["state_dir"]
        for name, child in subs.choices.items():
            if name in ("schema", "status", "scope-check", "validate"):
                continue
            argv = [name]
            for action in child._actions:
                if not action.required and action.option_strings:
                    continue
                value = (list(action.choices)[0] if action.choices else
                         "1" if action.type is int else "fixture")
                argv.extend(([action.option_strings[0]] if action.option_strings else []) + [value])
            if "--state-dir" in child._option_string_actions:
                argv += ["--state-dir", state_dir]
            # In particular both aliases reach their real shared handler.
            with self.subTest(command=name), mock.patch.object(sys, "argv", ["swarm.py"] + argv), \
                    nullcontext(), no_effects() as effects:
                with self.assertRaisesRegex(SystemExit, "D5 active:"):
                    S.main()
                self.assertEqual(effects, ([], []))
        for name in ("new-command", "status", "advance-code-terminal"):
            args = argparse.Namespace(state_dir=state_dir, command=name, fn=lambda _: self.fail("unguarded dispatch"))
            with mock.patch.object(argparse.ArgumentParser, "parse_args", return_value=args), \
                    nullcontext(), no_effects() as effects:
                with self.assertRaisesRegex(SystemExit, "D5 active:"):
                    S.main()
                self.assertEqual(effects, ([], []))

    def test_operator_modes_refuse_before_preparation(self):
        modes = ([], ["--dry-run"], ["--verify-integration"],
                 ["--verify-integration", "--retrieve-remote-evidence"],
                 ["--verify-integration", "--retrieve-remote-evidence",
                  "--remote-recovery-attestation", "fixture"],
                 ["--abandon-intent", "op", "--reason", "test"])
        with tempfile.TemporaryDirectory() as tmp:
            fixture = SurfaceFixture(tmp)
            ready_installation(tmp, state_dir=fixture.state_dir)
            for mode in modes:
                with self.subTest(mode=mode), nullcontext(), \
                        redirect_stderr(io.StringIO()) as err, no_effects() as effects:
                    self.assertEqual(M.main(fixture.margv + mode), 1)
                    self.assertIn("D5 active:", err.getvalue())
                    self.assertEqual(effects, ([], []))

    def test_active_observation_dispatch_is_retained(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = ready_installation(tmp)["state_dir"]
            for fn in (S.cmd_schema, S.cmd_status, S.cmd_scope_check, S.cmd_validate):
                S._d5_dispatch(argparse.Namespace(fn=fn, check_seeds=False, state_dir=state_dir))
            with self.assertRaisesRegex(SystemExit, "D5 active:"):
                S._d5_dispatch(argparse.Namespace(fn=S.cmd_validate, check_seeds=True, state_dir=state_dir))

    def test_named_route_ownership_and_callers(self):
        profile = json.loads(MANIFEST.read_text())["u41_active_surface"]
        self.assertEqual(profile["owners"], {str(n): {"owner": "U4.1", "verification": "U4.7"}
                                           for n in range(71, 75)})
        self.assertEqual(set(profile["swarm_entries"]), set(S_ENTRIES))
        self.assertEqual(set(profile["operator_entries"]), set(M_ENTRIES))
        tree = ast.parse(Path(S.__file__).read_text())
        callers = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)
                   and any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                           and c.func.id == "_integration_verification_fence" for c in ast.walk(n))}
        self.assertEqual(callers, {"cmd_verify"})

    def test_swarm_imports_no_orchestrate_d5_helper(self):
        assert_swarm_imports(Path(S.__file__).read_text())

    def assert_refused(self, observed):
        self.assertEqual(observed["launches"], [])
        self.assertEqual(observed["writes"], [])
        self.assertIn("D5 active:", str(observed["outcome"]) + str(observed["output"]))

    def test_mutation_route_not_refused(self):
        # Independent barriers can be redundant. Remove a leaf barrier to expose
        # its real legacy body; the same zero-effect assertion must detect it.
        for module, tool, names in ((S, "swarm", ("_submit", "maybe_continue", "advance",
                "_start_code_terminal_watchers", "cmd_watch_code_terminal", "_prepare_command_paths",
                "cmd_continue", "cmd_validate", "promote", "_integration_verification_fence")),
                (M, "operator", ("run", "current_intent", "abandon", "verify_integration",
                                  "print_tracker_drain"))):
            for name in names:
                with self.subTest(entry=name), mock.patch.object(
                        module, name, mutated_function(module, name, remove=True)):
                    with self.assertRaises(AssertionError):
                        self.assert_refused(self.capture(tool, name, True))

    def test_mutation_refuse_after_effect(self):
        for module, name, tool in ((S, "promote", "swarm"), (S, "cmd_continue", "swarm"),
                                   (S, "cmd_verify", "swarm"), (M, "reconcile", "operator")):
            with self.subTest(entry=name), mock.patch.object(
                    module, name, mutated_function(module, name, late=True)):
                with self.assertRaises(AssertionError):
                    self.assert_refused(self.capture(tool, name, True))

    def test_mutation_swarm_imports_d5_helper(self):
        for statement in ("import d5_submission", "from d5_registry import lookup"):
            with self.subTest(statement=statement), self.assertRaises(AssertionError):
                assert_swarm_imports(statement + "\n" + Path(S.__file__).read_text())

    def test_mutation_alias_bypass(self):
        with mock.patch.object(S, "_d5_dispatch", lambda args: None), \
                mock.patch.object(argparse.ArgumentParser, "parse_args", return_value=argparse.Namespace(
                    fn=lambda args: 0, command="new-alias")), \
                nullcontext():
            with self.assertRaises(AssertionError):
                with self.assertRaisesRegex(SystemExit, "D5 active:"):
                    S.main()

    def test_installed_swarm_and_operator(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "installed"
            destination.mkdir()
            installed_state = ready_installation(tmp)["state_dir"]
            # Real copy installation of swarm, then of the operator's declared closure.
            for skill in ("hanig-swarm", "hanig-orchestrate"):
                names = (skill,) if skill == "hanig-swarm" else (
                    "hanig-orchestrate", "hanig-project", "hanig-review-gate", "hanig-portable-handoff")
                for name in names:
                    shutil.copytree(ROOT / "skills" / name, destination / name,
                                    ignore=shutil.ignore_patterns("__pycache__"))
                probe = r'''
import builtins, pathlib, sys
root = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root / 'hanig-swarm/scripts'))
real_import = builtins.__import__
def checked(name, *args, **kwargs):
    if name.startswith('d5_') and name not in ('d5_activation', 'd5_installation'):
        raise AssertionError('unexpected orchestrate helper: ' + name)
    return real_import(name, *args, **kwargs)
builtins.__import__ = checked
import d5_activation
import swarm
try:
    from types import SimpleNamespace
    swarm.cmd_continue(SimpleNamespace(state_dir=sys.argv[3]))
except SystemExit as exc:
    assert 'D5 active:' in str(exc)
else:
    raise AssertionError('entry not refused')
assert not any(n.startswith('d5_') and n not in ('d5_activation', 'd5_installation') for n in sys.modules)
if sys.argv[2] == 'operator':
    sys.path.insert(0, str(root / 'hanig-orchestrate/scripts'))
    import merge_unit
    assert merge_unit.SWARM == str(root / 'hanig-swarm/scripts/swarm.py')
    assert merge_unit.main(['unused', '--state-dir', sys.argv[3], '--unit', 'u',
                            '--pr', '1', '--approver', 'Operator']) == 1
print('installed refusal: OK')
'''
                env = {k: v for k, v in os.environ.items() if not k.startswith("HANIG_")}
                result = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", probe,
                                         str(destination), "swarm" if skill == "hanig-swarm" else "operator", installed_state],
                                        cwd=tmp, env=env, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("installed refusal: OK", result.stdout)


def assert_swarm_imports(source):
    for node in ast.walk(ast.parse(source)):
        names = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                 else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
        for name in names:
            assert not (name.startswith("d5_") and name != "d5_activation"), name


def mutated_function(module, name, remove=False, late=False):
    tree = ast.parse(Path(module.__file__).read_text())
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    class Mutation(ast.NodeTransformer):
        def visit_Expr(self, node):
            if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name) \
                    and node.value.func.id in ("_d5_refuse", "_d5_merge_entry"):
                if remove:
                    return ast.Pass()
                if late:
                    return [ast.parse("open('forbidden-u41-write', 'w')").body[0], node]
            return self.generic_visit(node)
    function = Mutation().visit(function)
    namespace = {}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])),
                 module.__file__, "exec"), module.__dict__, namespace)
    return namespace[name]


if __name__ == "__main__":
    unittest.main()
