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

## What kept recurring, and the contract that replaces it

Four review rounds found four defects and the gate then refused a fifth,
correctly: "more rounds have not converged -- they have been finding
defects in the previous round's fixes."

They were one defect. A relative repository locator resolved twice; a JSON
shape never checked; bytes decoded permissively enough that a corrupt one
survived; a command scanned by a pattern whose cost was never bounded.
Every round, this hook took an UNTRUSTED INPUT and produced a confident
answer from it without validating it at the boundary. The fixes were
correct and the pattern was the problem.

So the inputs are enumerated, and each has a stated contract. There are
five, and `TrackerSyncHookInputContract` in the test file has a hostile
case for each:

  1. **The harness event on stdin.** May be absent, truncated, not JSON, or
     JSON without a command. Contract: if the event cannot be read, nothing
     is emitted -- an unreadable event is not evidence that an outward
     action occurred.
  2. **The command text.** Untrusted and unbounded. Contract: classified in
     time linear in its length, per logical line, and never across lines.
  3. **The repository locator.** May be empty, relative, absent, a file, or
     a directory that is not a repository. Contract: resolved exactly once,
     and anything unusable is a reported unknown.
  4. **The probe's exit status.** Contract: nonzero is a reported unknown,
     even when the probe also printed a well-formed empty outbox.
  5. **The probe's bytes.** Contract: decoded strictly, parsed without
     JSON's non-standard constants, and shape-checked before any count is
     believed.

Two invariants cut across all five. Once a command matches, this hook is
COMMITTED to emitting -- every failure degrades to a reported unknown and
never to silence, because the conditions that silence it are the ones
during which the tracker is most likely adrift. And "total 0" is the most
reassuring sentence available here, so it must be the hardest to reach by
accident: it requires a probe that started, exited zero, produced strict
UTF-8, parsed as standard JSON, and matched the outbox shape.

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
import select
import shlex
import signal
import subprocess
import sys
import tempfile
import time

# Detection is deliberately SENSITIVE, not precise, because the costs are not
# symmetric. A spurious reminder costs one line of context. A missed one costs
# the tracker sync this hook exists to guarantee.
# `gh pr` subcommands that CHANGE a pull request. kimi-k2.7-code found the
# first gap here -- `gh pr edit` retitles a PR and was not matched -- so the
# list is the mutating verbs rather than the three that happened to come to
# mind. Read-only subcommands (view, list, diff, checks, status) are
# deliberately absent: they change nothing, so a reminder on them is noise,
# and habituation is the way a reminder stops being read.
# The mutating subcommands of `gh pr` and `gh issue`. Read-only ones -- view,
# list, diff, checks, status -- are deliberately absent: they change nothing,
# so a reminder on them is noise, and habituation is how a reminder stops
# being read.
# The coordinator's own wire vocabulary, from swarm.py. luna: requiring
# merely a STRING let {"ack_status": "pending"} through, the pending filter
# matched nothing, and the hook reported "total 0" -- a status the reader
# does not know counted as an outbox with nothing in it. If the coordinator
# ever adds a value, this hook must report unknown until it is added here,
# which is the safe direction.
ACK_STATUSES = frozenset(
    ["unacknowledged", "attested", "attested_confirmed", "conflict"])

PR_MUTATING = frozenset(
    "merge close create edit ready reopen comment review "
    "update-branch lock unlock"
    .split())
ISSUE_MUTATING = frozenset(
    "create close reopen edit comment delete transfer pin unpin lock "
    "unlock develop".split())

# Detection LEXES with shlex and then parses a documented subset. That is
# the fifth shape, chosen by a step-back committee (astra, deepseek-v4-pro)
# after four hand-rolled shapes each failed the same way: every one of them
# approximated shell word splitting instead of using it.
#
#   1. `\bgh\b.*\bpr\b.*\bmerge\b` with re.S -- quadratic (8.05s for one
#      of four patterns on 600 honest lines, measured) and matched across
#      lines.
#   2. A per-line word-set intersection -- position-blind. A pipe made an
#      argument look like a subcommand, and the word `issue` inside
#      `--subject "fixes issue #3"` made a real `gh pr merge` emit nothing.
#   3. Search for `gh` anywhere, take the next two non-flag tokens -- missed
#      `gh --repo acme/x pr merge`, fired from a comment, fired on
#      `git log --grep push`, and `&` was not a separator.
#   4. A positional read over `segment.split()` -- five MAJOR findings,
#      every one a consequence of `split()` not being shell word splitting:
#      a leading redirection became the command word, a quoted assignment
#      value was split apart, and quoted text and heredoc bodies became
#      their own commands and produced reminders for actions never run.
#
# deepseek-v4-pro's diagnosis of why there were five: "Five iterations are
# evidence that the GUARANTEE is ill-posed, not the heuristic. Detecting
# 'obvious literal outward action' is well-posed. Detecting 'any outward
# action in arbitrary shell from text alone' is not."
#
# So the claim is narrowed to match what is achievable, and it is stated
# here rather than implied:
#
#   THIS HOOK DETECTS A DOCUMENTED SYNTACTIC SUBSET. Inside that subset it
#   is highly sensitive. Outside it, it fails closed and says nothing.
#
# IN the subset: a literal command word, optionally behind `VAR=value`
# assignments, redirections, an absolute path, and value-taking global
# options; separated by `;`, newline, `&&`, `||`, `|`, `&`; with quoting
# and heredoc bodies respected.
#
# OUT of the subset, and not claimed: a subcommand or command word supplied
# through a variable, an alias, `eval`, command substitution or backticks.
# Those are excluded by shlex rather than by a check of ours: it does not
# expand, so `gh pr $merge 41` lexes `$merge` and no unexpanded token ever
# equals an allowlisted verb. A `_literal()` guard was written for this and
# DELETED after its mutation passed -- measured across `$merge`, `${VERB}`,
# backticks, `$(...)`, `$GH` as the program and `$NOUN` as the noun, all six
# already silent without it. Code that survives its own mutation is the
# appearance of protection. If the lexer is ever replaced with one that
# expands, this is the thing to reinstate;
# anything whose execution depends on control flow; and a `#` inside a
# quoted string that shlex does not treat as a comment. A stronger
# guarantee than this needs a different mechanism -- observing effect, or
# capturing intent at the tool layer -- which is ARC-698, not more
# aggressive text parsing.
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_REDIRECTION = re.compile(r"^\d*(?:>>|>|<<-|<<|<)")

_OPERATORS = frozenset([";", "&&", "||", "|", "&", "\n"])

# Global options that take a SEPARATE value, so the value is not a noun.
_GH_VALUE_OPTIONS = frozenset(["--repo", "-R", "--hostname"])
_GIT_VALUE_OPTIONS = frozenset(
    ["-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path",
     "--config-env"])


def strip_heredocs(command):
    """Remove heredoc BODIES before lexing.

    shlex does not know heredocs, so `cat > x.sh <<'EOF' / git push / EOF`
    would offer `git push` as a command. It is a file being written, not a
    push being run -- glm-5.3 found the hook reminding about a push that
    never happened.
    """
    lines = command.splitlines()
    out, index = [], 0
    while index < len(lines):
        line = lines[index]
        out.append(line)
        match = re.search(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1", line)
        index += 1
        if not match:
            continue
        delimiter = match.group(2)
        while index < len(lines) and lines[index].strip() != delimiter:
            index += 1
        if index < len(lines):
            index += 1        # drop the delimiter line too
    return "\n".join(out)


_NEWLINE = "\n"


def _lex(command):
    """Shell words and operators, quoting respected, newlines preserved.

    Lexed LINE BY LINE with an explicit separator between lines, because
    shlex treats a newline as ordinary whitespace: lexing the whole text at
    once made `cat > x.sh <<EOF ... EOF` followed by a real `git push` into
    one command beginning with `cat`, so the push was missed. Backslash
    continuations are joined first, so a command split across lines stays
    one command.

    A quoted string containing a literal newline is therefore lexed as two
    lines. That is a declared limit, and the direction it fails in is
    towards a spurious reminder rather than a missed one.

    Returns None for text shlex cannot lex, which is not something to guess
    about.
    """
    joined = strip_heredocs(command).replace("\\\n", " ")
    tokens = []
    for line in joined.splitlines():
        if not line.strip():
            continue
        lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        try:
            tokens.extend(list(lexer))
        except ValueError:
            return None
        tokens.append(_NEWLINE)
    return tokens


def _simple_commands(tokens):
    """Split a token stream into separately-executed simple commands."""
    current, out = [], []
    for token in tokens:
        if token in _OPERATORS:
            out.append(current)
            current = []
        else:
            current.append(token)
    out.append(current)
    return [command for command in out if command]


def _command_word_and_arguments(tokens):
    """Strip assignments and redirections; return (program, arguments)."""
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if _ASSIGNMENT.match(token):
            index += 1
            continue
        if _REDIRECTION.match(token):
            index += 1
            if index < len(tokens):
                index += 1      # the redirection target
            continue
        break
    if index >= len(tokens):
        return None, []
    return tokens[index], tokens[index + 1:]


def _subcommands(arguments, value_options, depth):
    """The first `depth` positional arguments, global options skipped."""
    out, index = [], 0
    while index < len(arguments) and len(out) < depth:
        word = arguments[index]
        if word.startswith("-"):
            if word in value_options and "=" not in word:
                index += 1
            index += 1
            continue
        out.append(word)
        index += 1
    return out


# Lexing properly costs more than splitting badly: 0.0296s against 0.0022s
# on 600 lines, 0.4911s on 158,889 bytes. Linear, and fine for a real
# command, but a hook on a synchronous per-tool path should not spend half a
# second on a pathological one. Past this size the text is not parsed at
# all and the reminder is emitted unconditionally, which is the direction
# the module's stated asymmetry points: "a spurious reminder costs one line
# of context, a missed one costs the tracker sync this hook exists to
# guarantee."
_COMMAND_LEX_LIMIT = 64 * 1024


def matched_label(command):
    """The outward action this command looks like, or None."""
    if len(command) > _COMMAND_LEX_LIMIT:
        return "an outward action (command too large to parse)"
    tokens = _lex(command)
    if tokens is None:
        # Unlexable text -- an unbalanced quote. Guessing is what the four
        # earlier shapes did; erring towards a reminder is what the stated
        # asymmetry asks for.
        return "an outward action (command could not be parsed)"
    for simple in _simple_commands(tokens):
        program, arguments = _command_word_and_arguments(simple)
        if not program:
            continue
        program = program.rsplit("/", 1)[-1]
        if program == "gh":
            path = _subcommands(arguments, _GH_VALUE_OPTIONS, 2)
            if len(path) == 2:
                noun, verb = path
                if noun == "pr" and verb in PR_MUTATING:
                    return "gh pr " + verb
                if noun == "issue" and verb in ISSUE_MUTATING:
                    return "gh issue " + verb
        elif program == "git":
            path = _subcommands(arguments, _GIT_VALUE_OPTIONS, 1)
            if path and path[0] == "push":
                return "git push"
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


# An outbox is kilobytes. communicate() buffers whatever it is given, so a
# probe that writes gigabytes exhausts memory before this hook can emit the
# reminder it promises -- luna found that the bound covered time and not
# size. 8 MiB is far above any real outbox and far below anything that
# hurts.
_OUTPUT_LIMIT = 8 * 1024 * 1024


def _reap(proc):
    """Kill the probe's process group, close the pipe, and wait, bounded.

    The pipe is closed BEFORE waiting. luna: communicate() buffers whatever
    is still arriving, so a detached descendant writing to the inherited
    stdout could push the process past the size cap during cleanup -- the
    cap applied to the read and not to the reap. Closing the read end means
    a surviving writer gets EPIPE instead of this process getting the
    bytes.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except OSError:
        pass
    try:
        if proc.stdout is not None:
            proc.stdout.close()
    except OSError:
        pass
    try:
        proc.wait(timeout=REAP_GRACE_S)
    except subprocess.TimeoutExpired:
        pass


def _read_bounded(proc):
    """Read the probe's stdout under BOTH a deadline and a size cap.

    Returns (bytes, overflowed). ``None`` for the bytes means the deadline
    passed with the probe still running, which the caller reports as a
    timeout. Reading incrementally is what makes the size cap possible:
    communicate() has already allocated everything by the time it returns.
    """
    deadline = time.monotonic() + probe_timeout_s()
    chunks, total = [], 0
    stream = proc.stdout
    fd = stream.fileno()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, False
        try:
            ready, _, _ = select.select([fd], [], [], min(remaining, 0.25))
        except (OSError, ValueError):
            return b"".join(chunks), False
        if not ready:
            if proc.poll() is not None:
                return b"".join(chunks), False
            continue
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            return b"".join(chunks), False
        if not chunk:
            return b"".join(chunks), False
        total += len(chunk)
        if total > _OUTPUT_LIMIT:
            return b"".join(chunks), True
        chunks.append(chunk)


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
    out, overflow = _read_bounded(proc)
    if overflow:
        _reap(proc)
        return None, ("The outbox probe produced more than %d bytes, which "
                      "no outbox does." % _OUTPUT_LIMIT)
    if out is None:
        _reap(proc)
        return None, ("The outbox probe did not finish within %gs."
                      % probe_timeout_s())
    try:
        proc.wait(timeout=REAP_GRACE_S)
    except subprocess.TimeoutExpired:
        # stdout closed but the process lingers: the answer is already
        # read, so reap it and judge on what it wrote.
        _reap(proc)
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
    # Each intent must be recognisable, not merely an object. luna:
    # {"intents":[{}]} passed a list-of-dicts check, contributed nothing to
    # the pending filter, and reported "total 0" -- a payload the reader
    # does not understand counted as an outbox with nothing in it.
    unreadable = [i for i in intents
                  if not isinstance(i, dict)
                  or i.get("ack_status") not in ACK_STATUSES]
    if unreadable:
        return None, ("The outbox probe returned %d intent(s), %d of which "
                      "carry an ack_status this hook does not know."
                      % (len(intents), len(unreadable)))
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
