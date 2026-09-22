#!/usr/bin/env python3
"""PostToolUse hook: after an outward action, report unsynced tracker state.

Written because prose did not work. The obligation was documented in
hanig-project SKILL.md step 6 and then skipped twice within the hour by the
session that wrote it. The harness runs this; the model cannot forget it.

What it guarantees, exactly: it detects supported spellings of an outward
command and delivers context about pending or unavailable tracker state. It
does not prove exhaustive detection, it does not make the tracker update
happen, and a reminder the model can read and ignore is a softer thing than
an enforcement. An earlier header here claimed the harness made forgetting
impossible; that was false in two separate ways at once.

## Why this is Python and not the shell script it replaces

The shell version shipped eight defects in two days and seven were found by
review, not by its author:

  1. `eval` on $HANIG_TRACKER_REPO. `'$(rm -rf "$HOME")'` would have run.
  2. The same value interpolated into an ssh remote command string, so
     `"'; touch /tmp/canary; #"` escaped the quoting. This was (1)'s sibling,
     left behind when (1) was fixed.
  3. A state directory derived from the local home and sent to a host where
     that path does not exist, so the probe reported "could not read".
  4. The reminder printed to stdout at exit 0, which the harness shows only
     in transcript mode -- so nothing it produced ever reached the model.
  5. Detection too precise, missing real spellings.
  6. The hook path unquoted in settings.json, so a project directory with a
     space in it silently never ran the hook.
  7. `set -u` plus a bare $HOME: with HOME unset the script died before any
     output, with three silent `exit 0` paths as its siblings.
  8. The probe was reaped by killing the subshell, which leaves the python
     grandchild alive holding the output pipe.

(1), (2), (6), (7) and (8) are substrate defects, not logic defects. A
step-back committee (astra, deepseek-v4-pro) was convened after (7) and both
members independently recommended this port, as a bounded migration with the
behaviour held fixed by tests/test_tracker_sync_hook.py -- which executes
whatever command settings.json wires, so it did not have to be repointed to
let this change pass.

Two rules this file must keep:

  * Configuration is data, never code. No shell strings are assembled here;
    the probe is argv, and nothing derived from the environment is executed.
  * Once a command matches, this hook is COMMITTED to emitting. Every
    failure below degrades to a reported unknown and never to silence,
    because the conditions that would silence it are exactly the ones during
    which the tracker is most likely adrift.
"""

import collections
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile

# Detection is deliberately SENSITIVE, not precise, because the costs are not
# symmetric. A spurious reminder costs one line of context. A missed one costs
# the tracker sync this hook exists to guarantee.
# `gh pr` subcommands that CHANGE a pull request. kimi-k2.7-code found the
# first gap here -- `gh pr edit` retitles a PR and was not matched -- so the
# list is the mutating verbs rather than the three that happened to come to
# mind. Read-only subcommands (view, list, diff, checks, status) are
# deliberately absent: they change nothing, so a reminder on them is noise,
# and habituation is the way a reminder stops being read.
PR_MUTATING = frozenset(
    "merge close create edit ready reopen comment review".split())

# Detection is a LINEAR token scan, not a regex.
#
# It was `\bgh\b.*\bpr\b.*\bmerge\b` with re.S, four of them, run on
# every Bash command. glm-5.3 showed what that costs: `.` spans newlines,
# so on an honest multi-kilobyte command containing a few hundred read-only
# `gh pr view` lines each pattern pairs every gh with every later pr and
# rescans the tail, and the synchronous PostToolUse path stalls for tens of
# seconds to minutes -- or the harness kills the hook and it emits nothing,
# which is the one outcome this module promises never to produce. The
# deleted shell version matched the same input instantly with `case` globs,
# so the port had regressed it.
#
# Scanning per line also fixes a correctness bug the regex had: with re.S a
# `gh` on line 1 and a `merge` on line 400 matched as though they were one
# command.
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9-]*")


def _logical_lines(command):
    """Split into commands, joining backslash continuations."""
    joined = command.replace("\\\n", " ")
    return joined.replace(";", "\n").replace("&&", "\n").splitlines()


def matched_label(command):
    """The outward action this command looks like, or None.

    Deliberately SENSITIVE rather than precise: a spurious reminder costs
    one line of context, a missed one costs the tracker sync this exists
    to guarantee.
    """
    for line in _logical_lines(command):
        words = set(w.lower() for w in _WORD.findall(line))
        if "git" in words and "push" in words:
            return "git push"
        if "gh" not in words:
            continue
        if "issue" in words:
            return "gh issue"
        if "pr" in words:
            verbs = words & PR_MUTATING
            if verbs:
                return "gh pr " + sorted(verbs)[0]
    return None


REAP_GRACE_S = 2


def probe_timeout_s():
    """How long the outbox probe may take, default 20 seconds.

    Overridable only so tests can exercise the timeout path without taking
    20 seconds each; clamped so a stray value cannot disable the bound or
    turn this hook into a long stall on a synchronous per-tool path.
    """
    raw = os.environ.get("HANIG_TRACKER_PROBE_TIMEOUT_S") or ""
    try:
        return max(1.0, min(120.0, float(raw)))
    except ValueError:
        return 20.0


def emit(message):
    """Deliver to the MODEL, not to the transcript.

    `hookSpecificOutput.additionalContext` on stdout at exit 0 is the only
    shape the harness injects into the model's context. Plain text at exit 0
    is transcript-only, which is how this hook spent its first day as a
    no-op.
    """
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": message,
        }
    }))
    sys.stdout.write("\n")
    sys.stdout.flush()


def state_dir_for(repo):
    """The coordinator's state directory for a project, by its own rule."""
    resolved = os.path.realpath(repo)
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:12]
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", os.path.basename(resolved)).strip("-.")
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "hanig-swarm", "projects",
                        "%s-%s" % (slug or "project", digest), "state")


def _reject_constant(name):
    """Refuse the JSON extensions the outbox never writes."""
    raise ValueError("unexpected JSON constant %s" % name)


def _shape_of(data):
    """A short, safe description of JSON this hook cannot read."""
    if isinstance(data, dict):
        keys = ", ".join(sorted(str(k) for k in list(data)[:5])) or "no keys"
        return "an object with %s" % keys
    return "a %s" % type(data).__name__


def read_outbox(repo, state):
    """Run the coordinator's own outbox reader, bounded, and reap its tree.

    The probe is started in its own session so the timeout path can kill the
    whole process group. Killing only the direct child leaves the python
    grandchild alive holding the output pipe, which is the defect class a
    step-back committee predicted would come next.

    **Declared limit, not a closed one.** A descendant that calls setsid for
    itself leaves the group and survives the kill -- luna established that,
    and POSIX offers no way to reap it from here. The bound this keeps is on
    the HOOK, not on the process tree: the reap itself is time-limited, so a
    surviving descendant delays nothing and the reminder is still delivered.
    That is the same shape as the exclusivity convention elsewhere in this
    repository, where "live descendant processes are declared limits, not
    closed ones".
    """
    # Resolve ONCE. Passing a relative script path alongside cwd=repo makes
    # the interpreter resolve it a second time against that cwd, so
    # HANIG_TRACKER_REPO=repo looked for repo/repo/skills/... and reported an
    # unknown outbox while the real state sat there unread.
    root = os.path.abspath(repo)
    argv = [sys.executable or "python3",
            os.path.join(root, "skills", "hanig-swarm", "scripts", "swarm.py"),
            "outbox", "--state-dir", state, "--json"]
    try:
        proc = subprocess.Popen(
            argv, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            start_new_session=True)
    except OSError as exc:
        return None, "The outbox probe could not start (%s)." % exc.strerror
    try:
        out, _ = proc.communicate(timeout=probe_timeout_s())
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            pass
        try:
            # Bounded even here: a child in uninterruptible I/O survives
            # SIGKILL, and this hook must still answer rather than hang.
            proc.communicate(timeout=REAP_GRACE_S)
        except subprocess.TimeoutExpired:
            pass
        return None, ("The outbox probe did not finish within %gs."
                      % probe_timeout_s())
    if proc.returncode != 0:
        return None, ("The outbox probe exited %s." % proc.returncode)
    try:
        text = out.decode("utf-8")
    except UnicodeDecodeError:
        # "replace" would turn a corrupt byte into U+FFFD and let the rest
        # parse, so malformed output could still report an empty outbox.
        return None, "The outbox probe returned output that is not UTF-8."
    try:
        # json.loads accepts NaN, Infinity and -Infinity by default, which
        # the outbox never emits; accepting them means accepting a payload
        # no json.dumps on the other side produced.
        data = json.loads(text, parse_constant=_reject_constant)
    except ValueError as exc:
        return None, ("The outbox probe returned output that is not JSON "
                      "(%s)." % str(exc).split("\n")[0][:120])
    # Validate the SHAPE before believing the count. luna: a probe that
    # exits 0 and prints {"intents": "not-a-list"} or {} produced "total 0"
    # with no warning -- a schema the reader does not recognise reading as
    # an empty outbox is the same defect as a failed probe reading as one,
    # and "nothing pending" is the most reassuring thing this hook can say.
    if isinstance(data, list):
        intents = data
    elif isinstance(data, dict) and isinstance(data.get("intents"), list):
        intents = data["intents"]
    else:
        return None, ("The outbox probe returned JSON this hook does not "
                      "recognise (%s)." % _shape_of(data))
    if not all(isinstance(i, dict) for i in intents):
        return None, ("The outbox probe returned %d intent(s), not all of "
                      "which are objects." % len(intents))
    pending = [i for i in intents
               if i.get("ack_status") == "unacknowledged"]
    verbs = collections.Counter(
        (i.get("envelope") or {}).get("requested_operation") for i in pending)
    # close and block are both terminal CANDIDATES. Which blocks are terminal
    # is the coordinator state machine's call, not this hook's, so report the
    # verbs rather than collapsing them into one number this cannot derive.
    # An earlier version printed "pending TERMINAL intents: 0" while five
    # terminal candidates sat unacknowledged.
    listed = ", ".join("%s=%d" % (k, v) for k, v in sorted(
        verbs.items(), key=lambda kv: str(kv[0]))) or "none"
    return ("unacknowledged by verb: %s | close=%d block=%d are terminal "
            "candidates | total %d"
            % (listed, verbs.get("close", 0), verbs.get("block", 0),
               len(pending))), None


def main():
    try:
        payload = json.load(sys.stdin)
        command = ((payload.get("tool_input") or {}).get("command") or "")
    except Exception:
        return 0
    if not isinstance(command, str):
        return 0
    label = matched_label(command)
    if label is None:
        return 0

    prefix = ("TRACKER SYNC CHECK: this tool call looks like `%s` (matched "
              "loosely; it may not have run). " % label)
    unknown_suffix = (" Unknown is not zero -- check Linear before assuming "
                      "it is current.")

    repo = os.environ.get("HANIG_TRACKER_REPO") or ""
    if not repo:
        home = os.environ.get("HOME") or ""
        if not home:
            emit(prefix + "Neither HANIG_TRACKER_REPO nor HOME is set, so the "
                          "repository could not be located." + unknown_suffix)
            return 0
        repo = os.path.join(home, "multi-agent-skills")
    if not os.path.isdir(repo):
        emit(prefix + "There is no repository at %s." % repo + unknown_suffix)
        return 0

    state = os.environ.get("HANIG_TRACKER_STATE_DIR") or ""
    if not state:
        try:
            state = state_dir_for(repo)
        except Exception:
            state = ""
    if not state:
        emit(prefix + "Could not derive the coordinator state directory for "
                      "%s." % repo + unknown_suffix)
        return 0

    report, problem = read_outbox(repo, state)
    if report is None:
        emit(prefix + "%s Could not read the outbox at %s."
             % (problem, state) + unknown_suffix)
        return 0
    emit(prefix + report + ". If it did run, reflect it in Linear now; the "
         "issue comment is part of the action, not a follow-up. Draining is "
         "reconciliation, never replay -- an intent contradicted by current "
         "tracker state is escalated, not applied.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Committed to emitting, including when this file itself is wrong.
        try:
            emit("TRACKER SYNC CHECK: this hook failed while checking tracker "
                 "state. Unknown is not zero -- check Linear before assuming "
                 "it is current.")
        except Exception:
            pass
        sys.exit(0)
