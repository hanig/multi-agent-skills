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
import subprocess
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS = os.path.join(REPO_ROOT, ".claude", "settings.json")
HOOK = os.path.join(REPO_ROOT, ".claude", "hooks", "tracker-sync-check.sh")


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
        if "tracker-sync" in hook.get("command", "")
    ]

# Commands that change a PR or an issue. Detection is deliberately loose, so
# these are representative spellings rather than an exhaustive grammar.
OUTWARD = [
    "git push origin HEAD",
    "gh pr create --base main --head topic",
    "gh pr merge 41 --squash",
    "gh pr close 27",
    "gh issue comment ARC-689 --body x",
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
    def test_hook_is_executable(self):
        self.assertTrue(os.path.isfile(HOOK), HOOK)
        self.assertTrue(os.access(HOOK, os.X_OK), HOOK + " is not executable")

    def test_settings_wire_the_hook_as_a_post_tool_use_hook(self):
        with open(SETTINGS) as handle:
            settings = json.load(handle)
        entries = (settings.get("hooks") or {}).get("PostToolUse") or []
        commands = [
            hook.get("command", "")
            for entry in entries
            for hook in (entry.get("hooks") or [])
        ]
        matching = [c for c in commands if "tracker-sync-check.sh" in c]
        self.assertTrue(
            matching,
            "the hook is not wired as a PostToolUse hook, so it never runs",
        )
        for command in matching:
            # An unquoted path breaks on a project directory containing a
            # space; the hook then silently never runs.
            self.assertIn('"', command, "hook path must be quoted: " + command)

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
        for command in ["ls -la", "python3 -m unittest discover -s tests"]:
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


if __name__ == "__main__":
    unittest.main()
