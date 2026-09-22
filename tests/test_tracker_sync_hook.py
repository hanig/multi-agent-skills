"""The tracker-sync PostToolUse hook, tested through its delivery contract.

This hook exists because prose did not hold the tracker obligation. It then
shipped a defect that made it a no-op: it printed its reminder to stdout and
exited 0, which the harness shows only in transcript mode, so nothing it ever
produced reached the model. The logic was right and the guard was hollow.

So these tests do not read the script. They run the command `settings.json`
configures, through a shell, the way the harness runs it -- JSON on stdin --
and assert on what a consumer receives: a single JSON
object carrying `hookSpecificOutput.additionalContext`, which is the only
shape that is injected into the model's context. A reminder printed as plain
text passes every test that inspects the script's logic and fails these.
"""

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS = os.path.join(REPO_ROOT, ".claude", "settings.json")

# Two of these cases reach INTO the hook: the clamp on the probe timeout
# and the last-resort guard around main(). Everything else runs the wired
# command through a shell, which is what this file is for -- but a guard
# that only fires when the hook itself is broken cannot be provoked from
# outside, and leaving it untested is what kimi-k2.7-code and glm-5.3
# both refused. A fresh module per test, so a patched main() never leaks.
HOOK_SPEC = importlib.util.spec_from_file_location(
    "tracker_sync_check_under_test",
    os.path.join(REPO_ROOT, ".claude", "hooks", "tracker_sync_check.py"))


def wired_commands():
    """The PostToolUse commands settings.json actually configures.

    The tests below run THESE rather than naming an implementation file, so
    the contract stays pinned to what the harness executes. A step-back
    committee (astra, deepseek-v4-pro) asked for exactly this: the hook is a
    shell script today and the same committee recommended porting it to
    Python, and a test that hardcodes `bash <path>` would have to be edited
    to let that port pass -- which is a test the change under review gets to
    rewrite.
    """
    with open(SETTINGS) as handle:
        settings = json.load(handle)
    entries = (settings.get("hooks") or {}).get("PostToolUse") or []
    return [
        hook.get("command", "")
        for entry in entries
        for hook in (entry.get("hooks") or [])
        if "tracker" in hook.get("command", "") and "sync" in hook.get("command", "")
    ]

# Commands that change a PR or an issue. Detection is deliberately loose, so
# these are representative spellings rather than an exhaustive grammar.
OUTWARD = [
    "git push origin HEAD",
    "gh pr create --base main --head topic",
    "gh pr merge 41 --squash",
    "gh pr close 27",
    "gh pr edit 41 --title 'new title'",
    "gh pr ready 41",
    "gh pr reopen 27",
    "gh pr comment 41 --body x",
    "gh issue comment ARC-689 --body x",
    "gh issue create --title x",
    "gh issue close ARC-689",
]

# Read-only: these change nothing, and a reminder on them is the noise that
# makes the real ones stop being read.
INWARD = [
    "ls -la",
    "python3 -m unittest discover -s tests",
    "gh pr view 41",
    "gh pr list --state open",
    "gh pr diff 41",
    "gh pr checks 41",
    "gh issue list",
    "gh issue view ARC-689",
    "gh issue status",
]


def run_hook(command, env_overrides=None, timeout=60):
    """Invoke the hook exactly as the harness does and return (rc, out, err)."""
    env = dict(os.environ)
    # Never let a test read the operator's real coordinator state.
    env["HANIG_TRACKER_REPO"] = os.path.join(REPO_ROOT, "no-such-repo-for-tests")
    env["HANIG_TRACKER_STATE_DIR"] = os.path.join(REPO_ROOT, "no-such-state-for-tests")
    for key, value in (env_overrides or {}).items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    payload = json.dumps({
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
    })
    # Exactly how the harness invokes it: the configured command string,
    # through a shell, with CLAUDE_PROJECT_DIR pointing at the project.
    commands = wired_commands()
    if len(commands) != 1:
        raise AssertionError(
            "expected exactly one wired tracker-sync command, got %r" % (commands,))
    env["CLAUDE_PROJECT_DIR"] = REPO_ROOT
    proc = subprocess.run(
        ["/bin/sh", "-c", commands[0]],
        input=payload.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout.decode("utf-8"), proc.stderr.decode("utf-8")


def injected_context(stdout):
    """The text the model actually receives, or None if nothing reaches it.

    Returns None rather than raising for plain-text output, because that is
    the exact regression being guarded: output that a human sees in the
    transcript and the model never does.
    """
    text = stdout.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    specific = parsed.get("hookSpecificOutput")
    if not isinstance(specific, dict):
        return None
    if specific.get("hookEventName") != "PostToolUse":
        return None
    context = specific.get("additionalContext")
    if not isinstance(context, str) or not context.strip():
        return None
    return context


class TrackerSyncHookDelivery(unittest.TestCase):
    def test_settings_wire_the_hook_as_a_post_tool_use_hook(self):
        commands = wired_commands()
        self.assertTrue(
            commands,
            "the hook is not wired as a PostToolUse hook, so it never runs",
        )
        for command in commands:
            # The harness runs this through a shell, so an unquoted path
            # breaks on a project directory containing a space and the hook
            # then silently never runs. That shipped once.
            self.assertIn(
                '"$CLAUDE_PROJECT_DIR', command,
                "the hook path must be quoted against a directory with a "
                "space in it: " + command)

    def test_the_wired_command_lives_in_this_repository(self):
        """luna: a settings file is mutable, so "run whatever is wired" is
        only as strong as where that thing is allowed to live.

        The command must name a path under this repository's .claude/hooks,
        so repointing the tests at a stand-in is a visible change to a
        reviewed file rather than a quiet redirection.
        """
        for command in wired_commands():
            with self.subTest(command=command):
                self.assertIn('"$CLAUDE_PROJECT_DIR/.claude/hooks/', command)
                self.assertNotIn("..", command)

    def test_the_wired_command_points_at_a_file_that_exists(self):
        for command in wired_commands():
            with self.subTest(command=command):
                rc, out, _ = run_hook("git push origin HEAD")
                self.assertEqual(
                    rc, 0,
                    "the wired command did not run: " + command)

    def test_every_outward_command_reaches_the_model(self):
        for command in OUTWARD:
            with self.subTest(command=command):
                rc, out, err = run_hook(command)
                self.assertEqual(rc, 0, "hook must never block: " + err)
                context = injected_context(out)
                self.assertIsNotNone(
                    context,
                    "nothing reached the model for %r; stdout was %r. Plain "
                    "text on stdout is transcript-only." % (command, out),
                )
                self.assertIn("Linear", context)

    def test_unrelated_commands_stay_silent(self):
        for command in INWARD:
            with self.subTest(command=command):
                rc, out, _ = run_hook(command)
                self.assertEqual(rc, 0)
                self.assertEqual(out.strip(), "", "spurious reminder for " + command)

    def test_unset_home_still_reaches_the_model(self):
        """Regression: `set -u` plus a bare $HOME killed the hook outright.

        With HOME and HANIG_TRACKER_REPO both unset the script died at the
        assignment with "HOME: unbound variable", before any output, so a
        matched outward action produced no reminder at all.
        """
        rc, out, err = run_hook(
            "git push origin HEAD",
            {"HOME": None, "HANIG_TRACKER_REPO": None, "HANIG_TRACKER_STATE_DIR": None},
        )
        self.assertNotIn("unbound variable", err)
        self.assertEqual(rc, 0)
        context = injected_context(out)
        self.assertIsNotNone(context, "silent when HOME is unset; stdout %r" % out)
        self.assertIn("Unknown is not zero", context)

    def test_an_unreadable_outbox_reports_unknown_rather_than_nothing(self):
        """Fail loud, not quiet: the conditions that make the outbox
        unreadable are the ones during which the tracker is most likely
        adrift, so a silent exit is the worst available answer."""
        rc, out, _ = run_hook(
            "gh pr merge 1",
            {"HANIG_TRACKER_REPO": "/no/such/repo", "HANIG_TRACKER_STATE_DIR": None},
        )
        self.assertEqual(rc, 0)
        context = injected_context(out)
        self.assertIsNotNone(context, "silent on a missing repo; stdout %r" % out)
        self.assertIn("Unknown is not zero", context)

    def test_the_reminder_names_the_command_it_saw(self):
        _, out, _ = run_hook("gh pr merge 41 --squash")
        context = injected_context(out)
        self.assertIsNotNone(context)
        self.assertIn("gh pr merge", context)
        # Detection is loose, so the reminder must not assert the command ran.
        self.assertIn("may not have run", context)


def _process_is_alive(pid):
    """True only if the pid names a RUNNING process, not a zombie.

    glm-5.3: os.kill(pid, 0) succeeds against a killed-but-unreaped
    process, so under a non-reaping PID 1 -- `docker run` without --init,
    where the test process itself adopts the dead grandchild -- the hook
    correctly kills the descendant and the assertion fails anyway.
    """
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        state = subprocess.run(["ps", "-o", "state=", "-p", str(pid)],
                               stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL,
                               timeout=10).stdout.decode().strip()
    except (OSError, subprocess.SubprocessError):
        return True     # cannot tell; treat as alive rather than pass falsely
    if not state:
        return False
    return not state.startswith("Z")



def fake_repo(script_body, root):
    """A repository whose only content is a stand-in outbox probe.

    The hook builds the probe's argv from the repository path, so a fixture
    repository is how the probe's behaviour gets controlled without a real
    coordinator, a real outbox, or the operator's own state.
    """
    scripts = os.path.join(root, "skills", "hanig-swarm", "scripts")
    os.makedirs(scripts, exist_ok=True)
    with open(os.path.join(scripts, "swarm.py"), "w") as handle:
        handle.write(textwrap.dedent(script_body))
    return root


PROBE_OK = """
    import json
    print(json.dumps({"intents": [
        {"ack_status": "unacknowledged",
         "envelope": {"requested_operation": "close"}},
        {"ack_status": "unacknowledged",
         "envelope": {"requested_operation": "block"}},
        {"ack_status": "unacknowledged",
         "envelope": {"requested_operation": "block"}},
        # The coordinator's wire value is "attested_confirmed";
        # "acknowledged" is a Python compatibility alias in swarm.py and
        # is never emitted. The strict vocabulary caught this fixture
        # inventing a value, which is the second time today strictness
        # found fabricated data in my own test material.
        {"ack_status": "attested_confirmed",
         "envelope": {"requested_operation": "close"}},
    ]}))
"""

PROBE_EMPTY = """
    import json
    print(json.dumps({"intents": []}))
"""

# Prints a perfectly valid EMPTY outbox and then fails. Without the exit-code
# check the hook reports "total 0", which reads as "nothing pending" when what
# actually happened is that the probe broke. An earlier mutation that only
# exited nonzero could not detect that: with no output at all the JSON parse
# fails anyway, so the exit-code check looked load-bearing when it was not.
PROBE_EXITS_NONZERO = """
    import json, sys
    print(json.dumps({"intents": []}))
    sys.exit(3)
"""

PROBE_NOT_JSON = """
    print("Traceback (most recent call last):")
"""

# Valid JSON the reader does not recognise. luna found that each of these
# produced "total 0" -- a schema mismatch reading as an empty outbox, which
# is the same defect as a failed probe reading as one.
# Output that DECODES or PARSES only because the reader was permissive.
# luna: decode(..., "replace") turns a corrupt byte into U+FFFD and lets the
# rest parse, and json.loads accepts NaN by default, so a malformed payload
# still reported an empty outbox.
PROBE_MALFORMED = {
    "invalid utf-8": (
        "    import sys\n"
        "    sys.stdout.buffer.write(b'{\"intents\":[],\"d\":\"\\xff\"}')"),
    "NaN in the payload": '    print(\'{"intents": [], "d": NaN}\')',
    "Infinity in the payload": '    print(\'{"intents": [], "d": Infinity}\')',
}

PROBE_WRONG_SHAPE = {
    "intents is a string": '    print(\'{"intents": "not-a-list"}\')',
    "no intents key": "    print('{}')",
    "a bare number": "    print('7')",
    "intents holds non-objects": '    print(\'{"intents": [1, 2, 3]}\')',
    # Each of these passed the check one field earlier and reported
    # "total 0" -- a shape the reader does not understand read as an
    # outbox with nothing in it, three times, one field further in each
    # round. luna found all three.
    "a known status but no envelope":
        '    print(\'{"intents": [{"ack_status": "attested"}]}\')',
    "an envelope with no operation":
        '    print(\'{"intents": [{"ack_status": "attested",'
        ' "envelope": {}}]}\')',
    "a numeric requested_operation":
        '    print(\'{"intents": [{"ack_status": "attested",'
        ' "envelope": {"requested_operation": 1}}]}\')',
    "a blank requested_operation":
        '    print(\'{"intents": [{"ack_status": "attested",'
        ' "envelope": {"requested_operation": "  "}}]}\')',
}

# Prints a well-formed EMPTY outbox, forks a child that inherits stdout
# and outlives it, then exits ZERO. luna: the reader saw the direct
# child gone with bytes already in hand and returned them, so the probe
# reported "total 0" with a live descendant still holding the write end
# -- the answer was a prefix and was read as the whole of it.
PROBE_EXITS_LEAVING_A_WRITER = """
    import os, sys, time
    print('{"intents": []}', flush=True)
    if os.fork() == 0:
        time.sleep(30)
        os._exit(0)
    os._exit(0)
"""

# Ignores SIGTERM and spawns a child that outlives it and also ignores
# SIGTERM. This is the shape the shell version leaked: it reaped by killing
# the subshell, leaving the python grandchild alive holding the output pipe.
PROBE_HANGS_WITH_CHILD = """
    import signal, subprocess, sys, time
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    subprocess.Popen([sys.executable, "-c",
        "import os, signal, sys, time\\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\\n"
        "open(sys.argv[1], 'w').write(str(os.getpid()))\\n"
        "time.sleep(600)", %r])
    time.sleep(600)
"""


class TrackerSyncHookOutboxReporting(unittest.TestCase):
    """What the hook says about the outbox it read, or failed to read.

    Delivery is covered above. These cover the separate contract: that the
    hook distinguishes a successfully-read EMPTY outbox from every way of
    FAILING to read one. Conflating those is how a probe failure becomes a
    reassuring "nothing pending", which is the worst answer available here.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tracker-sync-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_against(self, script_body, env=None, command="git push origin HEAD",
                    timeout=60):
        repo = fake_repo(script_body, os.path.join(self.tmp, "repo"))
        overrides = {"HANIG_TRACKER_REPO": repo,
                     "HANIG_TRACKER_STATE_DIR": os.path.join(self.tmp, "state")}
        overrides.update(env or {})
        rc, out, err = run_hook(command, overrides, timeout=timeout)
        return rc, injected_context(out), out, err

    def test_a_real_outbox_is_reported_by_verb(self):
        """The counts reach the model, not merely the fact that a probe ran."""
        rc, context, out, _ = self.run_against(PROBE_OK)
        self.assertEqual(rc, 0)
        self.assertIsNotNone(context, out)
        self.assertIn("close=1", context)
        self.assertIn("block=2", context)
        self.assertIn("total 3", context)
        self.assertNotIn("Unknown is not zero", context)

    def test_an_empty_outbox_is_reported_as_empty_not_as_unknown(self):
        rc, context, out, _ = self.run_against(PROBE_EMPTY)
        self.assertEqual(rc, 0)
        self.assertIsNotNone(context, out)
        self.assertIn("total 0", context)
        self.assertNotIn("Unknown is not zero", context)

    def test_a_failing_probe_is_unknown_and_never_an_empty_outbox(self):
        rc, context, out, _ = self.run_against(PROBE_EXITS_NONZERO)
        self.assertEqual(rc, 0)
        self.assertIsNotNone(context, out)
        self.assertIn("Unknown is not zero", context)
        self.assertNotIn("total 0", context)

    def test_probe_output_that_is_not_json_is_unknown(self):
        rc, context, out, _ = self.run_against(PROBE_NOT_JSON)
        self.assertEqual(rc, 0)
        self.assertIsNotNone(context, out)
        self.assertIn("Unknown is not zero", context)

    def test_a_relative_repository_path_still_finds_the_probe(self):
        """luna: cwd=repo plus a relative script path resolves it twice.

        With the process working directory at /work and
        HANIG_TRACKER_REPO=repo, the interpreter looked for
        /work/repo/repo/skills/... and reported an unknown outbox while the
        real state sat there unread -- a silent downgrade to "unknown" that
        looks exactly like a genuine failure to read.
        """
        fake_repo(PROBE_OK, os.path.join(self.tmp, "repo"))
        env = dict(os.environ)
        env["HANIG_TRACKER_REPO"] = "repo"          # relative, on purpose
        env["HANIG_TRACKER_STATE_DIR"] = os.path.join(self.tmp, "state")
        env["CLAUDE_PROJECT_DIR"] = REPO_ROOT
        commands = wired_commands()
        payload = json.dumps({"hook_event_name": "PostToolUse",
                              "tool_name": "Bash",
                              "tool_input": {"command": "git push origin HEAD"}})
        proc = subprocess.run(
            ["/bin/sh", "-c", commands[0]], input=payload.encode("utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, cwd=self.tmp, timeout=60)
        context = injected_context(proc.stdout.decode("utf-8"))
        self.assertIsNotNone(context, proc.stdout)
        self.assertIn("total 3", context)
        self.assertNotIn("Unknown is not zero", context)

    def test_json_this_hook_cannot_read_is_unknown_not_an_empty_outbox(self):
        """The most reassuring sentence this hook can say is "total 0", so
        it must be the hardest one to reach by accident."""
        for label, body in PROBE_WRONG_SHAPE.items():
            with self.subTest(shape=label):
                rc, context, out, _ = self.run_against(body)
                self.assertEqual(rc, 0)
                self.assertIsNotNone(context, out)
                self.assertIn("Unknown is not zero", context)
                self.assertNotIn("total 0", context)

    def test_a_descendant_holding_stdout_makes_the_answer_incomplete(self):
        """luna: the probe exited zero with a live writer behind it.

        `_read_bounded` returned as soon as the DIRECT child was gone and
        nothing was readable, so the bytes written before the fork were
        treated as the whole answer -- and those bytes were a well-formed
        empty outbox, which is the one sentence this hook must never
        reach by accident. No EOF ever arrived, and no EOF means the
        output is a prefix.
        """
        rc, context, out, _ = self.run_against(
            PROBE_EXITS_LEAVING_A_WRITER, timeout=90)
        self.assertEqual(rc, 0)
        self.assertIsNotNone(context, out)
        self.assertIn("Unknown is not zero", context)
        self.assertNotIn("total 0", context)
        self.assertIn("incomplete", context)

    def test_malformed_output_is_unknown_even_when_it_parses(self):
        """A permissive reader is how malformed output became "total 0"."""
        for label, body in PROBE_MALFORMED.items():
            with self.subTest(payload=label):
                rc, context, out, _ = self.run_against(body)
                self.assertEqual(rc, 0)
                self.assertIsNotNone(context, out)
                self.assertIn("Unknown is not zero", context)
                self.assertNotIn("total 0", context)

    def test_detection_is_linear_in_the_command(self):
        """glm-5.3: four nested-greedy patterns with re.S turned an honest
        bulk script into a multi-minute stall on the synchronous per-tool
        path, or a hook killed with nothing emitted.

        The input is deliberately honest: several hundred read-only
        `gh pr view` lines, no mutating verb anywhere. It must stay silent
        AND stay fast.
        """
        haystack = "\n".join("gh pr view %d" % n for n in range(600))
        started = time.time()
        rc, out, _ = run_hook(haystack, timeout=30)
        elapsed = time.time() - started
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "",
                         "a read-only bulk script must not fire the hook")
        self.assertLess(elapsed, 5.0,
                        "detection took %.1fs on a 600-line honest command; "
                        "this runs on every Bash call" % elapsed)

    def test_a_mutating_verb_on_another_line_is_not_one_command(self):
        """With re.S a `gh` on line 1 and a `merge` on line 400 matched as
        though they were the same command. They are not."""
        rc, out, _ = run_hook("gh pr view 1\necho merge\n")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "", "matched across separate commands")

    def test_a_zombie_is_not_a_living_descendant(self):
        """The liveness helper itself, tested against a real zombie.

        glm-5.3's scenario needs a non-reaping PID 1, which this host does
        not have -- launchd reaps, so os.kill already reports the dead
        grandchild as gone and mutating the helper away does NOT fail the
        descendant test here. That is an honest gap in the mutation, so
        the helper is verified directly instead: fork a child, let it exit,
        do not wait for it, and confirm the two answers disagree.
        """
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        try:
            deadline = time.time() + 5
            while time.time() < deadline:
                try:
                    os.kill(pid, 0)
                except OSError:
                    self.skipTest("this platform reaped before we looked")
                    return
                if not _process_is_alive(pid):
                    break
                time.sleep(0.05)
            else:
                self.fail("a zombie was still reported alive after 5s")
            # os.kill still says yes; that is exactly the trap.
            os.kill(pid, 0)
        finally:
            try:
                os.waitpid(pid, 0)
            except OSError:
                pass

    def test_a_hanging_probe_is_bounded_and_leaves_no_descendants(self):
        """The defect class a step-back committee predicted would come next.

        Both members, asked what this file ships eighth, independently said
        incomplete timeout cleanup: the hook exceeding its deadline or
        leaving processes behind. So the probe here never finishes, ignores
        SIGTERM, and spawns a child that does the same.
        """
        marker = os.path.join(self.tmp, "descendant-pid")
        started = time.time()
        rc, context, out, _ = self.run_against(
            PROBE_HANGS_WITH_CHILD % (marker,),
            env={"HANIG_TRACKER_PROBE_TIMEOUT_S": "2"}, timeout=40)
        elapsed = time.time() - started
        self.assertEqual(rc, 0)
        self.assertIsNotNone(context, out)
        self.assertIn("Unknown is not zero", context)
        self.assertLess(
            elapsed, 30,
            "the hook is on a synchronous per-tool path and took %.1fs "
            "against a probe that never finishes" % elapsed)

        pid = None
        deadline = time.time() + 5
        while time.time() < deadline and pid is None:
            try:
                with open(marker) as handle:
                    pid = int(handle.read().strip())
            except (IOError, OSError, ValueError):
                time.sleep(0.1)
        self.assertIsNotNone(
            pid, "the fixture never recorded its grandchild pid, so this "
                 "test would pass without testing anything")
        alive = True
        for _ in range(50):
            if not _process_is_alive(pid):
                alive = False
                break
            time.sleep(0.1)
        if alive:
            try:
                os.kill(pid, 9)
            except OSError:
                pass
        self.assertFalse(
            alive,
            "the probe's grandchild (pid %s) outlived the hook; reaping only "
            "the direct child leaves it alive holding the output pipe" % pid)


class TrackerSyncHookInputContract(unittest.TestCase):
    """One hostile case per untrusted input, enumerated rather than found.

    Four review rounds found four defects here and the gate refused a fifth,
    saying the rounds had stopped converging. They were one defect wearing
    four hats: the hook produced a confident answer from an input it had not
    validated at the boundary. A relative repository locator resolved twice,
    a JSON shape never checked, bytes decoded permissively, a command
    scanned by a pattern whose cost was never bounded.

    So this class is organised by INPUT rather than by defect. If a sixth
    defect is found here, the first question is which input it came in
    through and whether that input is listed.
    """

    INPUTS = ("the harness event", "the command text",
              "the repository locator", "the probe's exit status",
              "the probe's bytes")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tracker-sync-contract-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def invoke(self, payload_bytes, env_overrides=None, timeout=60):
        env = dict(os.environ)
        env["HANIG_TRACKER_REPO"] = os.path.join(self.tmp, "repo")
        env["HANIG_TRACKER_STATE_DIR"] = os.path.join(self.tmp, "state")
        env["CLAUDE_PROJECT_DIR"] = REPO_ROOT
        for key, value in (env_overrides or {}).items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        commands = wired_commands()
        proc = subprocess.run(
            ["/bin/sh", "-c", commands[0]], input=payload_bytes,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, timeout=timeout)
        return proc.returncode, proc.stdout.decode("utf-8")

    # 1. the harness event -------------------------------------------------

    def test_an_unreadable_event_emits_nothing(self):
        """An event that cannot be read is not evidence of an outward
        action, so the answer is silence rather than a reported unknown."""
        for label, payload in (
                ("empty stdin", b""),
                ("not json", b"{ this is not json"),
                ("truncated json", b'{"tool_input": {"command"'),
                ("no tool_input", b'{"hook_event_name": "PostToolUse"}'),
                ("command is not a string", b'{"tool_input": {"command": 7}}'),
                ("invalid utf-8", b'{"tool_input": {"command": "\xff"}}'),
        ):
            with self.subTest(event=label):
                rc, out = self.invoke(payload)
                self.assertEqual(rc, 0, "the hook must never block")
                self.assertEqual(out.strip(), "",
                                 "emitted on an unreadable event: %r" % out)

    # 2. the command text --------------------------------------------------

    def test_hostile_command_text_stays_bounded_and_correct(self):
        """luna, kimi-k2.7-code and glm-5.3 all noted this class named the
        command text and then tested ordinary commands in it."""
        cases = {
            # glm-5.3: the word "issue" in an argument routed the whole
            # line into the issue branch and the merge emitted NOTHING.
            "merge whose subject mentions an issue": (
                'gh pr merge 41 --squash --subject "fixes issue #3"',
                "gh pr merge"),
            "merge with an issue in a comment": (
                "gh pr merge 41 # fixes issue 3", "gh pr merge"),
            "review whose body mentions an issue": (
                'gh pr review 41 --approve --body "closes issue #3"',
                "gh pr review"),
            # kimi-k2.7-code: a pipe made an argument look like a subcommand.
            "read-only piped into grep merge": (
                "gh pr view 41 | grep merge", ""),
            "read-only piped into grep close": (
                "gh pr view 41 | grep close", ""),
            # The other direction, which is what makes the pipe split
            # load-bearing rather than decorative: only the FIRST gh in a
            # segment is examined, so without splitting on `|` the second
            # command here is never looked at and a real merge is missed.
            "read-only piped into a real merge": (
                "gh pr view 41 | gh pr merge 41", "gh pr merge"),
            "mutating first, read-only second": (
                "gh pr merge 41 | tee /tmp/log", "gh pr merge"),
            # luna: a mutating subcommand that was simply absent.
            "update-branch": ("gh pr update-branch 41", "gh pr update-branch"),
            # luna and kimi-k2.7-code, round 3: the parse was still an
            # approximation, so a global option's value became the noun, a
            # comment fired, `&` was not a separator, and a read-only git
            # subcommand with `push` in an argument fired.
            "a global --repo before the subcommand": (
                "gh --repo acme/project pr merge 41", "gh pr merge"),
            "the -R spelling": ("gh -R acme/project pr merge 41",
                                "gh pr merge"),
            "a single ampersand separator": (
                "gh pr view 1 & gh pr merge 2", "gh pr merge"),
            "a merge in a comment": ("# gh pr merge 41\ngh pr view 41", ""),
            "push as a grep argument": ("git log --grep push", ""),
            "push in a config key": ("git config push.default simple", ""),
            "a merge inside echo": ('echo "gh pr merge"', ""),
            "a leading environment assignment": (
                "GIT_SSH_COMMAND=ssh git push origin HEAD", "git push"),
            "an absolute program path": ("/usr/bin/git push origin HEAD",
                                         "git push"),
            # Round 3: operators and redirections shlex emits that the
            # splitter and the redirection pattern did not know, and a
            # heredoc whose delimiter cannot be read.
            "the stderr pipe operator": (
                "gh pr view 41 |& gh pr merge 41", "gh pr merge"),
            # Round 1 of the rewrite: glm-5.3 found `(` in the program-word
            # slot, and kimi-k2.7-code found `VAR+=value` there. Both are
            # covered by the backstop as well as by their own token, which
            # is the point -- the backstop is what stops the next one.
            "a subshell": ("(gh pr merge 41)", "gh pr merge"),
            "a subshell around a push": (
                "(git push origin HEAD)", "git push"),
            "a brace group": ("{ gh pr merge 41; }", "gh pr merge"),
            "a descriptor duplication": (
                "<&3 gh pr merge 41", "gh pr merge"),
            "a clobbering redirect": (
                ">|out git push origin HEAD", "git push"),
            "a read-write redirect": (
                "<>rw git push origin HEAD", "git push"),
            "an appending assignment": (
                "PATH+=/usr/local/bin git push origin HEAD", "git push"),
            # Round 2. glm-5.3 found the first of these as a DETECTION
            # REGRESSION against the shell hook this file replaces: the
            # old `*"git push"*` substring match caught it and the port
            # did not, because a reserved word is plain letters and so
            # passes the program-word backstop.
            "a for loop": (
                "for r in origin upstream; do git push $r main; done",
                "git push"),
            "an if guard": (
                "if gh pr merge 41 --auto; then echo ok; fi",
                "gh pr merge"),
            "an until loop": (
                "until gh pr merge 41; do sleep 5; done", "gh pr merge"),
            "a negated while": (
                "while ! git push origin HEAD; do sleep 1; done",
                "git push"),
            "the time keyword": ("time git push origin HEAD", "git push"),
            # A reserved word as an ARGUMENT is still an argument.
            "do as an argument": ("echo do git push", ""),
            # kimi-k2.7-code: a redirection between the program word and
            # its subcommand was collected as the subcommand.
            "a redirection before the subcommand": (
                "git 2>/dev/null push origin HEAD", "git push"),
            "a global option and a redirection": (
                "gh -R o/r 2>/dev/null pr merge 41", "gh pr merge"),
            # The descriptor form and the BARE form take different
            # branches, and only the descriptor one had a case, so the
            # bare branch's mutation survived.
            "a bare redirection before the subcommand": (
                "git >/tmp/log push origin HEAD", "git push"),
            "a bare append before the subcommand": (
                "gh >>/tmp/log pr merge 41", "gh pr merge"),
            # Round 3. The SECOND regression class against the shell
            # hook this file replaces: a wrapper word in the program
            # slot. luna found `command`, glm-5.3 found the rest, and
            # the old `*"git push"*` substring match caught every one.
            "the command builtin": (
                "command git push origin HEAD", "git push"),
            "timeout with an operand": (
                "timeout 600 git push origin HEAD", "git push"),
            "sudo": ("sudo git push origin HEAD", "git push"),
            "sudo with an option and a value": (
                "sudo -u bob git push origin HEAD", "git push"),
            "env with an assignment": (
                "env FOO=1 git push origin HEAD", "git push"),
            "nohup": ("nohup gh pr merge 41", "gh pr merge"),
            "nice with an option": (
                "nice -n 5 git push origin HEAD", "git push"),
            # kimi-k2.7-code: short options cluster.
            "a clustered shell option": (
                "bash -ce 'git push origin HEAD'", "git push"),
            "another clustered shell option": (
                "sh -xc 'gh pr merge 41'", "gh pr merge"),
            "an equals-attached shell option": (
                'bash --command="git push origin HEAD" x', "git push"),
            # glm-5.3, out of scope but noise: `[` is the test builtin
            # and a legitimate program name, so a benign conditional
            # must not read as an unparseable command.
            "the test builtin in a conditional": (
                'if [ -n "$GH_TOKEN" ]; then gh pr merge 41; fi',
                "gh pr merge"),
            # A wrapper word that is not wrapping an outward command.
            "sudo running something harmless": ("sudo ls -la", ""),
            # Round 2. eval JOINS its arguments and runs the result, so
            # these are one command and not four -- glm-5.3 noted the
            # wrapper branch already joins and that join-then-run is
            # eval's own semantics.
            "eval with unquoted arguments": (
                "eval git push origin HEAD", "git push"),
            # A regex cannot match balanced parentheses, so a nested
            # substitution was skipped entirely, and backticks were
            # never looked for.
            "a nested command substitution": (
                'echo "$(git push origin $(printf x))"', "git push"),
            "a backtick substitution": (
                'echo "`git push origin HEAD`"', "git push"),
            "a substitution running something harmless": (
                'echo "$(printf x)"', ""),
            # astra, step-back, after PR 44 merged: a heredoc fed to a
            # SHELL is the script that shell runs, and discarding it as
            # inert was the third detection regression against the hook
            # this replaces. The consumer is the discriminator.
            "a shell-fed heredoc is a script": (
                "bash <<'EOF'\ngit push origin HEAD\nEOF", "git push"),
            "an unquoted shell heredoc": (
                "bash <<EOF\ngh pr merge 41\nEOF", "gh pr merge"),
            "a wrapper around a shell heredoc": (
                "sudo bash <<'EOF'\ngit push origin HEAD\nEOF", "git push"),
            # ...and a heredoc fed to something that does NOT run it
            # stays data, which is the half that must not regress.
            "a cat heredoc is still data": (
                "cat <<'EOF'\ngit push origin HEAD\nEOF", ""),
            "a heredoc written to a file is still data": (
                "cat > x.sh <<'EOF'\ngit push\nEOF", ""),
            "a shell heredoc doing something harmless": (
                "bash <<'EOF'\necho hello\nEOF", ""),
            # Round 2 on the heredoc consumer. luna and kimi-k2.7-code
            # found six shapes where the token that decided was not the
            # program: a wrapper's option, its value, its operand, and
            # the heredoc delimiter or redirection target when the
            # redirection came first. Every one was a silent miss.
            "a wrapper option before the shell": (
                "env -i bash <<'EOF'\ngit push origin HEAD\nEOF",
                "git push"),
            "a wrapper option with a value before the shell": (
                "sudo -u bob bash <<'EOF'\ngit push origin HEAD\nEOF",
                "git push"),
            "a wrapper operand before the shell": (
                "timeout 600 bash <<'EOF'\ngit push origin HEAD\nEOF",
                "git push"),
            "the redirection before the command": (
                "<<'EOF' bash\ngit push origin HEAD\nEOF", "git push"),
            "an output redirection before the command": (
                "> /tmp/o bash <<'EOF'\ngit push origin HEAD\nEOF",
                "git push"),
            # ...and the same shapes must not start flagging data.
            "a wrapper option before a non-shell": (
                "sudo -u bob cat <<'EOF'\ngit push origin HEAD\nEOF", ""),
            "the redirection before a non-shell": (
                "<<'EOF' cat\ngit push origin HEAD\nEOF", ""),
            # A shell NAME as an argument to something that is not a
            # shell. Nothing distinguished "reached a command word
            # before any wrapper" from "still scanning past one" until
            # this case: every other shape has no shell token after
            # the program, so deleting that stop changed no answer and
            # the mutation survived. Here it flips data into a false
            # outward action.
            "a shell name as an argument to a non-shell": (
                "echo bash <<'EOF'\ngit push origin HEAD\nEOF", ""),
            # luna: a shell given its script with -c reads the heredoc
            # as stdin and ignores it, so calling that outward is a
            # false positive -- the direction that reddens honest work.
            "a shell whose script came from -c": (
                "bash -c ':' <<'EOF'\ngit push origin HEAD\nEOF", ""),
            # Round 2. These execute through ordinary shell grammar; the
            # consumer is the simple command carrying the heredoc, and only
            # its last stdin heredoc can become the shell's script.
            "a delimiter named like a shell option": (
                "bash <<'-c'\ngit push origin HEAD\n-c", "git push"),
            "a shell later in a pipeline": (
                "cat | bash <<'EOF'\ngit push origin HEAD\nEOF", "git push"),
            "a shell after an earlier compound command": (
                "true; bash <<'EOF'\ngit push origin HEAD\nEOF", "git push"),
            "a shell-looking argument to a non-shell wrapper command": (
                "env echo bash <<'EOF'\ngit push origin HEAD\nEOF", ""),
            "only the last of two heredocs is shell stdin": (
                "bash <<'FIRST' <<'LAST'\ngit push origin HEAD\nFIRST\n"
                "echo ok\nLAST", ""),
            "a later stdin redirection replaces the heredoc": (
                "bash <<'EOF' < /dev/null\ngit push origin HEAD\nEOF", ""),
            "a later fd-zero redirection replaces the heredoc": (
                "bash <<'EOF' 0< /dev/null\ngit push origin HEAD\nEOF", ""),
            "a later read-write redirection replaces the heredoc": (
                "bash <<'EOF' <> /dev/null\ngit push origin HEAD\nEOF", ""),
            "a later here-string replaces the heredoc": (
                "bash <<'EOF' <<< ':'\ngit push origin HEAD\nEOF", ""),
            "an earlier stdin redirection is replaced by the heredoc": (
                "bash < /dev/null <<'EOF'\ngit push origin HEAD\nEOF",
                "git push"),
            "an fd-one input redirection does not replace shell stdin": (
                "bash <<'EOF' 1< /dev/null\ngit push origin HEAD\nEOF",
                "git push"),
            "a separated number is an argument not a descriptor": (
                "bash -s <<'EOF' 1 < /dev/null\ngit push origin HEAD\nEOF",
                ""),
            "duplicating stdin onto itself preserves the heredoc": (
                "bash <<'EOF' <&0\ngit push origin HEAD\nEOF", "git push"),
            "an explicit fd-zero self-duplication preserves the heredoc": (
                "bash <<'EOF' 0<&0\ngit push origin HEAD\nEOF", "git push"),
            "a non-stdin heredoc is data rather than a shell script": (
                "bash 3<<'EOF'\ngit push origin HEAD\nEOF", ""),
            "a non-stdin heredoc duplicated onto stdin is a script": (
                "bash 3<<'EOF' <&3\ngit push origin HEAD\nEOF", "git push"),
            "moving stdin away makes its heredoc data": (
                "bash <<'EOF' 3<&0-\ngit push origin HEAD\nEOF", ""),
            "moving a heredoc onto stdin makes it a script": (
                "bash 3<<'EOF' <&3-\ngit push origin HEAD\nEOF", "git push"),
            "closing stdin makes its heredoc data": (
                "bash <<'EOF' <&-\ngit push origin HEAD\nEOF", ""),
            "a compact consumed dup operand is not a new descriptor": (
                "bash 3<<'EOF' <&3</dev/null\ngit push origin HEAD\nEOF", ""),
            "a spaced consumed dup operand is not a new descriptor": (
                "bash 3<<'EOF' <& 3</dev/null\ngit push origin HEAD\nEOF", ""),
            "a moved-away heredoc restored to stdin is a script": (
                "bash <<'EOF' 3<&0- 0<&3\ngit push origin HEAD\nEOF",
                "git push"),
            "a self-move preserves the stdin heredoc": (
                "bash <<'EOF' 0<&0-\ngit push origin HEAD\nEOF", "git push"),
            "a heredoc on cat after a shell pipeline command": (
                "bash | cat <<'EOF'\ngit push origin HEAD\nEOF", ""),
            "a shell running something harmless too": (
                "bash -c 'echo hi'", ""),
            # Round 1 of the restart. All three reviewers found the same
            # COMPOSITION: a wrapper handing the command to a shell. Two
            # mechanisms that each worked alone and not together.
            "a wrapper around a shell": (
                "sudo bash -c 'git push origin HEAD'", "git push"),
            "timeout around a shell": (
                'timeout 600 bash -c "gh pr merge 41"', "gh pr merge"),
            "nohup around a shell": (
                "nohup sh -c 'git push origin HEAD'", "git push"),
            "a wrapper with options around a shell": (
                "sudo -u deploy bash -c 'git push origin HEAD'", "git push"),
            # luna, three more, each a regression against the old hook.
            "a reserved word with its own option": (
                "time -p git push origin HEAD", "git push"),
            "an attached -c payload": (
                "bash -c'git push origin HEAD'", "git push"),
            "eval of a command string": (
                'eval "git push origin HEAD"', "git push"),
            "a command substitution": (
                'echo "$(git push origin HEAD)"', "git push"),
            # luna: the command delegated to another shell as a string.
            "bash -c with a push": (
                "bash -c 'git push origin HEAD'", "git push"),
            "sh -c with a merge": ("sh -c 'gh pr merge 41'", "gh pr merge"),
            "an absolute shell path": (
                '/bin/bash -c "gh issue close 5"', "gh issue close"),
            "a nested shell": (
                'bash -c \'sh -c "git push"\'', "git push"),
            "a shell running something harmless": (
                "bash -c 'echo hello'", ""),
            "a leading combined redirection": (
                "&>/dev/null git push origin HEAD", "git push"),
            "a leading descriptor merge": (
                "2>&1 git push origin HEAD", "git push"),
            "a spaced dash-heredoc is still a heredoc": (
                "cat <<- EOF\nbody\nEOF\ngit push origin HEAD", "git push"),
            # Round 2's findings: the heredoc stripper ran a regex over
            # raw text before shlex, so a quoted `<<EOF` and a `<<` in a
            # comment each swallowed the real command after them; and
            # shlex splits `2>` so a bare descriptor became the program.
            "a quoted heredoc marker is not a heredoc": (
                "printf '%s' '<<EOF'\ngit push origin HEAD", "git push"),
            "a heredoc mentioned in a comment": (
                "# usage: cat << EOF\ngit push origin HEAD", "git push"),
            "a numbered descriptor redirection": (
                "2>/dev/null git push origin HEAD", "git push"),
            "a trailing descriptor redirection": (
                "git push origin HEAD 2> /dev/null", "git push"),
            "a dash heredoc still hides its body": (
                "cat <<-EOF\ngit push\nEOF", ""),
            # A step-back committee's acceptance criteria, verbatim.
            # deepseek-v4-pro supplied thirteen after four hand-rolled
            # shapes; these are the ones not already above.
            "AC4 heredoc body is not a command": (
                "cat > x.sh <<'EOF'\ngit push\nEOF", ""),
            "AC5 a real push after a heredoc": (
                "cat > x.sh <<'EOF'\ngit push\nEOF\ngit push origin HEAD",
                "git push"),
            "AC3 a merge inside a quoted argument": (
                'echo "one; gh pr merge"', ""),
            "AC2 a leading redirection": (
                "> /tmp/merge.log gh pr merge 41", "gh pr merge"),
            "AC1 a quoted multi-word assignment value": (
                'GIT_SSH_COMMAND="ssh -i /some path/key" git push origin HEAD',
                "git push"),
            "AC7 pr lock": ("gh pr lock 41", "gh pr lock"),
            "AC7 pr unlock": ("gh pr unlock 41", "gh pr unlock"),
            "AC8 a subcommand in a variable": ("gh pr $merge 41", ""),
            "commands on separate lines": (
                "git add -A\ngit push origin HEAD", "git push"),
            # position, not presence
            "git behind a -C flag": ("git -C /tmp/x push origin HEAD",
                                     "git push"),
            "the word push as an argument": ("echo push origin", ""),
            # honest bulk script: must stay silent AND stay fast
            "600 read-only lines": ("\n".join("gh pr view %d" % n
                                              for n in range(600)), ""),
            # the words exist but on separate commands
            "verb on another line": ("gh pr view 1\necho merge\n", ""),
            # read-only issue subcommands
            "gh issue list": ("gh issue list", ""),
            "gh issue view": ("gh issue view ARC-689", ""),
            # a compound where the mutating verb IS the same command
            "compound with a push": ("make build && git push origin HEAD",
                                     "git push"),
            "backslash continuation": ("gh pr \\\n  merge 41", "gh pr merge"),
            # degenerate shapes
            "empty command": ("", ""),
            "only whitespace": ("   \n\t ", ""),
            "no word characters": ("!!! ??? ***", ""),
        }
        for label, (command, expected) in cases.items():
            with self.subTest(command=label):
                started = time.time()
                payload = json.dumps(
                    {"tool_input": {"command": command}}).encode()
                rc, out = self.invoke(payload, timeout=30)
                elapsed = time.time() - started
                self.assertEqual(rc, 0)
                self.assertLess(elapsed, 10.0,
                                "%s took %.1fs" % (label, elapsed))
                if expected:
                    context = injected_context(out)
                    self.assertIsNotNone(context, out)
                    self.assertIn(expected, context)
                else:
                    self.assertEqual(out.strip(), "",
                                     "%s should be silent: %r" % (label, out))

        # The narrowing, stated rather than assumed. An unknown answer is
        # only worth giving about text that could be an outward action at
        # all, and "x x x ..." past the size bound could not be. glm-5.3's
        # point about habituation is the whole reason: a reminder that
        # fires on obvious non-commands is a reminder nobody reads.
        for label, command in (
                ("oversized with nothing outward in it", "x " * 40000),
                ("unlexable with nothing outward in it", 'echo "oops'),
        ):
            with self.subTest(command=label):
                payload = json.dumps(
                    {"tool_input": {"command": command}}).encode()
                rc, out = self.invoke(payload, timeout=30)
                self.assertEqual(rc, 0)
                self.assertEqual(out.strip(), "",
                                 "%s produced a reminder" % label)

    def test_a_parse_out_of_its_depth_reminds_rather_than_guesses(self):
        """Five shapes of this function each hid a real command by
        treating "I could not read this" as "there is nothing here".

        A heredoc whose delimiter never arrives is the sharp case: a
        quoted `'<<'` is indistinguishable from the operator once shlex
        has removed the quotes (luna), and a `<<` with nothing usable
        after it made the old code skip every following line (glm-5.3).
        Both now say so instead of going quiet.
        """
        fake_repo(PROBE_EMPTY, os.path.join(self.tmp, "repo"))
        for label, command in (
                ("a quoted << argument", "printf '%s' '<<' DONE\n"
                                         "git push origin HEAD"),
                ("a bare << with nothing after", "cat <<\n"
                                                 "git push origin HEAD"),
                ("<< followed only by a dash", "cat << -\n"
                                               "git push origin HEAD"),
                ("a heredoc whose delimiter never arrives",
                 "cat <<'EOF'\ngit push origin HEAD"),
                # The blank line is the whole point. An earlier fix
                # returned "" as the delimiter, and a blank line in the
                # body then CLOSED the heredoc, so the rest was read as
                # commands and a quiet one meant silence -- the reminder
                # for an unreadable parse went missing.
                ("a degenerate heredoc followed by a blank line",
                 "cat <<\n\ngit status\n\ngh pr merge 41"),
        ):
            with self.subTest(command=label):
                payload = json.dumps(
                    {"tool_input": {"command": command}}).encode()
                rc, out = self.invoke(payload, timeout=30)
                self.assertEqual(rc, 0)
                context = injected_context(out)
                self.assertIsNotNone(context, "%s emitted nothing" % label)
                self.assertIn("could not be parsed", context)

    def test_a_well_formed_heredoc_still_hides_its_body(self):
        """The out-of-depth rule must not swallow the working case."""
        fake_repo(PROBE_EMPTY, os.path.join(self.tmp, "repo"))
        for label, command in (
                ("a merge in the body", "cat <<'EOF'\ngh pr merge 41\nEOF"),
                ("a blank line in the body",
                 "cat <<'EOF'\n\ngh pr merge 41\nEOF"),
        ):
            with self.subTest(command=label):
                payload = json.dumps(
                    {"tool_input": {"command": command}}).encode()
                rc, out = self.invoke(payload, timeout=30)
                self.assertEqual(rc, 0)
                self.assertEqual(out.strip(), "",
                                 "%s fired on heredoc body text" % label)

    def test_a_program_word_that_is_not_a_command_is_not_a_parse(self):
        """The backstop, and the reason this round is not a sixth patch.

        glm-5.3: `(gh pr merge 41)` lexes cleanly, and `(` then sat in the
        program-word slot, matched no program, and the hook went silent on
        a real merge. Three rounds running I added the spelling a reviewer
        named and the next reviewer brought the one beside it.

        So this asserts the SHAPE rule rather than the spellings: a word
        that cannot be a command name means the parser did not find a
        command, whatever put it there. The token itself need not be
        known.
        """
        fake_repo(PROBE_EMPTY, os.path.join(self.tmp, "repo"))
        for label, command in (
                ("an unresolved variable", "GIT_DIR=x $CMD push origin HEAD"),
                ("a glob in the program slot", "gh* pr merge 41"),
                ("a bracket glob", "[gh]h pr merge 41"),
                ("a tilde-brace expansion", "~/{gh,x} pr merge 41"),
        ):
            with self.subTest(command=label):
                payload = json.dumps(
                    {"tool_input": {"command": command}}).encode()
                rc, out = self.invoke(payload, timeout=30)
                self.assertEqual(rc, 0)
                context = injected_context(out)
                self.assertIsNotNone(context, "%s emitted nothing" % label)
                self.assertIn(
                    "could not be parsed", context,
                    "%s was answered with a verdict, not an unknown" % label)

    def test_the_probe_timeout_override_is_clamped(self):
        """glm-5.3: nothing supplied an out-of-range value, so reverting
        the clamp left the suite green.

        The clamp is what keeps a stray value from disabling the bound or
        turning this hook into a long stall on a synchronous per-tool
        path, which is a property worth a test rather than a comment.
        """
        hook = importlib.util.module_from_spec(HOOK_SPEC)
        HOOK_SPEC.loader.exec_module(hook)
        original = os.environ.get("HANIG_TRACKER_PROBE_TIMEOUT_S")
        try:
            for value, expected in (("99999", 120.0), ("-5", 1.0),
                                    ("0", 1.0), ("", 20.0),
                                    ("not a number", 20.0), ("45", 45.0),
                                    # kimi-k2.7-code: float() accepts
                                    # these and min/max propagate them,
                                    # so the deadline was switched off
                                    # by a value the clamp let through.
                                    ("NaN", 20.0), ("nan", 20.0),
                                    ("inf", 20.0), ("-inf", 20.0),
                                    ("Infinity", 20.0)):
                with self.subTest(value=value):
                    os.environ["HANIG_TRACKER_PROBE_TIMEOUT_S"] = value
                    self.assertEqual(hook.probe_timeout_s(), expected)
        finally:
            if original is None:
                os.environ.pop("HANIG_TRACKER_PROBE_TIMEOUT_S", None)
            else:
                os.environ["HANIG_TRACKER_PROBE_TIMEOUT_S"] = original

    def test_a_reminder_that_cannot_reach_stdout_goes_to_stderr(self):
        """kimi-k2.7-code: the guard swallowed its own emit failure.

        `main()` calls `emit()`, stdout is closed, `emit()` raises, the
        guard tries to emit the failure message, `emit()` raises again,
        and the inner handler returned 0 with no output -- a hook whose
        headline claim is that it is COMMITTED to emitting, delivering
        nothing in exactly the case that claim names.

        stderr with a nonzero status is the other path the harness
        reads, and it is taken only here.
        """
        hook = importlib.util.module_from_spec(HOOK_SPEC)
        HOOK_SPEC.loader.exec_module(hook)

        class Closed(io.StringIO):
            def write(self, _text):
                raise ValueError("I/O operation on closed file")

        hook.main = lambda: (_ for _ in ()).throw(RuntimeError("broken"))
        errors = io.StringIO()
        stdout, stderr = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = Closed(), errors
        try:
            status = hook.run()
        finally:
            sys.stdout, sys.stderr = stdout, stderr
        self.assertEqual(status, 2,
                         "a hook that cannot write stdout exited quietly")
        self.assertIn("Unknown is not zero", errors.getvalue())

    def test_the_last_resort_guard_emits_when_the_hook_itself_is_wrong(self):
        """kimi-k2.7-code and glm-5.3: the guard was exercised by nothing.

        "Committed to emitting, including when this file itself is wrong"
        is the module's own headline claim, and it rested on a line that
        reverting would not have failed anything.
        """
        hook = importlib.util.module_from_spec(HOOK_SPEC)
        HOOK_SPEC.loader.exec_module(hook)
        class NotAnException(BaseException):
            """A BaseException that is not an Exception, and not one
            unittest treats specially. KeyboardInterrupt here aborted the
            whole runner instead of failing one test, which is a worse
            signal than the defect it was catching."""

        for failure in (RuntimeError("main is broken"),
                        MemoryError(),
                        NotAnException("the interpreter is unwinding")):
            with self.subTest(failure=type(failure).__name__):
                hook.main = lambda _f=failure: (_ for _ in ()).throw(_f)
                captured = io.StringIO()
                stdout, sys.stdout = sys.stdout, captured
                try:
                    status = hook.run()
                finally:
                    sys.stdout = stdout
                self.assertEqual(status, 0)
                payload = json.loads(captured.getvalue())
                self.assertIn(
                    "Unknown is not zero",
                    payload["hookSpecificOutput"]["additionalContext"])

    def test_unparseable_and_oversized_commands_fail_towards_a_reminder(self):
        """The stated asymmetry, applied where the parser gives up.

        Proper lexing costs more than bad splitting -- 0.0296s against
        0.0022s on 600 lines -- and a hook on a synchronous per-tool path
        should not spend half a second on a pathological command. Past the
        size bound, and for text shlex cannot lex at all, the text is not
        parsed and the reminder is emitted: "a spurious reminder costs one
        line of context, a missed one costs the tracker sync this hook
        exists to guarantee."
        """
        fake_repo(PROBE_EMPTY, os.path.join(self.tmp, "repo"))
        for label, command in (
                ("oversized", "git log " * 20000),
                ("unbalanced quote", 'git commit -m "oops'),
        ):
            with self.subTest(command=label):
                payload = json.dumps(
                    {"tool_input": {"command": command}}).encode()
                started = time.time()
                rc, out = self.invoke(payload, timeout=30)
                elapsed = time.time() - started
                self.assertEqual(rc, 0)
                context = injected_context(out)
                self.assertIsNotNone(
                    context, "%s emitted nothing: %r" % (label, out))
                self.assertIn("outward action", context)
                self.assertLess(elapsed, 10.0,
                                "%s took %.1fs" % (label, elapsed))

    # 3. the repository locator -------------------------------------------

    def test_a_repository_without_the_probe_is_a_reported_unknown(self):
        """The locator can point somewhere real that cannot answer."""
        empty = os.path.join(self.tmp, "repo")
        os.makedirs(empty, exist_ok=True)
        payload = json.dumps(
            {"tool_input": {"command": "git push origin HEAD"}}).encode()
        rc, out = self.invoke(payload)
        self.assertEqual(rc, 0)
        context = injected_context(out)
        self.assertIsNotNone(context, out)
        self.assertIn("Unknown is not zero", context)
        self.assertNotIn("total 0", context)

    def test_a_file_where_a_repository_was_is_a_reported_unknown(self):
        afile = os.path.join(self.tmp, "repo-is-a-file")
        with open(afile, "w") as handle:
            handle.write("not a repository\n")
        payload = json.dumps(
            {"tool_input": {"command": "gh pr merge 1"}}).encode()
        rc, out = self.invoke(payload, {"HANIG_TRACKER_REPO": afile})
        self.assertEqual(rc, 0)
        context = injected_context(out)
        self.assertIsNotNone(context, out)
        self.assertIn("Unknown is not zero", context)

    # 5. the probe's bytes --------------------------------------------------

    def test_a_probe_that_writes_without_end_is_bounded_by_size(self):
        """luna: the bound covered time and not size, so a probe writing
        gigabytes exhausted memory before the reminder could be emitted."""
        body = """
            import sys
            block = b"x" * 65536
            while True:
                sys.stdout.buffer.write(block)
        """
        fake_repo(body, os.path.join(self.tmp, "repo"))
        payload = json.dumps(
            {"tool_input": {"command": "git push origin HEAD"}}).encode()
        started = time.time()
        rc, out = self.invoke(payload, {"HANIG_TRACKER_PROBE_TIMEOUT_S": "60"},
                              timeout=90)
        elapsed = time.time() - started
        self.assertEqual(rc, 0)
        context = injected_context(out)
        self.assertIsNotNone(context, out)
        self.assertIn("Unknown is not zero", context)
        self.assertLess(elapsed, 60,
                        "the size cap must end this before the deadline "
                        "does; took %.1fs" % elapsed)

    # the invariant that cuts across all five ------------------------------

    def test_total_zero_requires_every_input_to_have_been_validated(self):
        """The most reassuring sentence must be the hardest to reach.

        Each of these breaks exactly one link in the chain that ends in
        "total 0", and each must report unknown instead.
        """
        breakages = {
            "probe will not start": None,          # no repo at all
            "probe exits nonzero": PROBE_EXITS_NONZERO,
            "probe prints non-json": PROBE_NOT_JSON,
            "probe prints the wrong shape": PROBE_WRONG_SHAPE["no intents key"],
            "probe prints invalid utf-8": PROBE_MALFORMED["invalid utf-8"],
            "probe prints NaN": PROBE_MALFORMED["NaN in the payload"],
            "intent has no ack_status": '    print(\'{"intents": [{}]}\')',
            "intent is not an object": '    print(\'{"intents": ["x"]}\')',
            "ack_status the reader does not know":
                '    print(\'{"intents": [{"ack_status": "pending"}]}\')',
            "a known status but no envelope":
                '    print(\'{"intents": [{"ack_status": "attested"}]}\')',
            "an envelope with no operation":
                '    print(\'{"intents": [{"ack_status": "attested",'
                ' "envelope": {}}]}\')',
        }
        payload = json.dumps(
            {"tool_input": {"command": "git push origin HEAD"}}).encode()
        for label, body in breakages.items():
            with self.subTest(broken=label):
                repo = os.path.join(self.tmp, "repo")
                shutil.rmtree(repo, ignore_errors=True)
                if body is not None:
                    fake_repo(body, repo)
                rc, out = self.invoke(payload)
                self.assertEqual(rc, 0)
                context = injected_context(out)
                self.assertIsNotNone(context, out)
                self.assertNotIn(
                    "total 0", context,
                    "%s still produced an empty-outbox report" % label)
                self.assertIn("Unknown is not zero", context)

        # ...and the intact chain does produce a count, so the assertions
        # above are not passing because nothing ever reports one.
        repo = os.path.join(self.tmp, "repo")
        shutil.rmtree(repo, ignore_errors=True)
        fake_repo(PROBE_EMPTY, repo)
        rc, out = self.invoke(payload)
        context = injected_context(out)
        self.assertIsNotNone(context, out)
        self.assertIn("total 0", context)


if __name__ == "__main__":
    unittest.main()
