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

import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS = os.path.join(REPO_ROOT, ".claude", "settings.json")


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
        {"ack_status": "acknowledged",
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
            try:
                os.kill(pid, 0)
            except OSError:
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


if __name__ == "__main__":
    unittest.main()
