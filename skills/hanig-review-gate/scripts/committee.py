#!/usr/bin/env python3
"""committee.py — a persistent two-member committee that plans, then reviews
its own plan's implementation.

Modelled on Shreshth's `paseo-committee`, which runs on Paseo agents that stay
alive across plan -> implement -> review. This repo has no agent daemon, so the
same shape is built on message history: each member is a conversation, not a
call.

Why this is not review.py. review.py is a stateless refutation gate: one HTTP
call per reviewer per round, no memory, no follow-ups. Three things follow, and
all three cost this repo real time:

  - A reviewer that never wrote a plan reviews against its own idea of
    correctness, which is unbounded. paseo-committee's Phase 3 asks a much
    smaller question: "review the changes against THE PLAN, flag drift and
    missing pieces". That question terminates.
  - A finding cannot be challenged. "Why does that happen? Symptom or cause?
    What did you consider and reject?" needs a second turn, and a stateless
    gate has none, so the first answer gets implemented.
  - Every round re-derives context, so rounds repeat ground instead of
    building on it.

Phases, per the reference skill:
  1. plan     both members, fresh, same problem prompt. Challenge, then
              synthesise. Convergence -> unified plan. Divergence -> the user.
  2. implement  you do it. The committee stays clean.
  3. review   the diff goes back to the SAME members, against their own plan.

Hard rule from the reference, carried verbatim: every prompt ends with the
no-edits suffix. These members analyse; they do not write.

Python 3.8+, stdlib only. Lives beside review.py and imports it: files WITHIN
one skill may import each other, since `install.sh --only NAME` installs the
skill whole. Only cross-skill imports are forbidden.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import review as R  # noqa: E402  (same skill, installed together)

NO_EDITS = ("\n\nThis is analysis only. Do NOT edit, create, or delete any "
            "files. Do NOT write code unless the prompt explicitly asks for a "
            "specification.")

SESSIONS = Path(os.environ.get("HANIG_COMMITTEE_DIR",
                               Path.home() / ".hanig-committee"))

STATES = {"COMMITTEE_OK": 0, "DIVERGED": 1, "NO_SESSION": 2, "UNAVAILABLE": 3}
USAGE_ERROR = 64


def die(msg):
    sys.exit(f"error: {msg}")


def session_path(name):
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in name)
    if not safe.strip("-"):
        die(f"session name {name!r} has no usable characters; use letters, "
            f"digits, - or _")
    return SESSIONS / f"{safe}.json"


def load_session(name):
    p = session_path(name)
    if not p.exists():
        sys.exit(f"error: no committee session {name!r} at {p}. Open one with "
                 f"`committee.py open {name} --problem-file FILE`.\n"
                 f"  existing: {', '.join(sorted(s.stem for s in SESSIONS.glob('*.json'))) or 'none'}")
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError) as e:
        sys.exit(f"error: session {name!r} is unreadable: {e}. Delete {p} and "
                 f"open a new one.")


def save_session(name, data):
    SESSIONS.mkdir(parents=True, exist_ok=True)
    p = session_path(name)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(p)


def pick_members(explicit):
    """Two members from contrasting providers. Contrast is the point, so this
    refuses a same-provider pair rather than quietly accepting it."""
    revs = [r for r in R.load_reviewers() if r.get("enabled", True)]
    if explicit:
        want = [n.strip() for spec in explicit for n in spec.split(",")
                if n.strip()]
        chosen = [r for r in revs if r["name"] in want]
        missing = sorted(set(want) - {r["name"] for r in chosen})
        if missing:
            die(f"no reviewer named {', '.join(missing)}. Available: "
                f"{', '.join(sorted(r['name'] for r in revs))}.")
    else:
        chosen = [r for r in revs if "plan" in (r.get("profiles") or [])]
    if len(chosen) != 2:
        die(f"a committee is exactly two members; got {len(chosen)} "
            f"({', '.join(r['name'] for r in chosen) or 'none'}). Name two "
            f"with --member, or fix the 'plan' profile in reviewers.json.")
    if len({r["provider"] for r in chosen}) != 2:
        die(f"both members use provider "
            f"{chosen[0]['provider']!r}, so they can share a failure mode. "
            f"Contrast is the point of a committee: name one from each "
            f"provider.")
    return chosen


def ask_member(member, history, prompt, timeout):
    """One turn. Returns (reply_text, error). History is the full prior
    exchange, so the member answers WITH its own earlier reasoning in view --
    the property a stateless gate cannot have."""
    msgs = list(history) + [{"role": "user", "content": prompt}]
    key_var = {"openai": "OPENAI_API_KEY",
               "openrouter": "OPENROUTER_API_KEY"}.get(member["provider"])
    if not key_var:
        return None, (f"provider {member['provider']!r} is not supported here; "
                      f"use openai or openrouter, or add a call path")
    key = os.environ.get(key_var)
    if not key:
        # Name the action. The keys live in ~/.zshrc, so a non-login shell has
        # them unset and the bare fact is not actionable -- which cost this
        # committee its first run.
        return None, (f"{key_var} not set in this environment. The keys are "
                      f"exported from ~/.zshrc, so run under a login shell: "
                      f"zsh -lic '...'")

    budget = member.get("max_output_tokens", R.DEFAULT_MAX_OUTPUT_TOKENS)
    if member["provider"] == "openai":
        payload = {"model": member["model"],
                   "input": [{"role": "system", "content": SYSTEM_PLANNER}]
                            + msgs,
                   "max_output_tokens": budget}
        if member.get("effort"):
            payload["reasoning"] = {"effort": member["effort"]}
        data, err = R._post("https://api.openai.com/v1/responses", payload,
                            {"Authorization": f"Bearer {key}",
                             "Content-Type": "application/json"}, timeout)
        if err:
            return None, err
        text = "".join(
            c.get("text", "")
            for o in (data.get("output") or [])
            if isinstance(o, dict) and o.get("type") == "message"
            for c in (o.get("content") or []) if isinstance(c, dict))
    else:
        payload = {"model": member["model"],
                   "messages": [{"role": "system", "content": SYSTEM_PLANNER}]
                               + msgs,
                   "max_tokens": budget}
        if member.get("effort"):
            payload["reasoning"] = {"effort": member["effort"]}
        data, err = R._post("https://openrouter.ai/api/v1/chat/completions",
                            payload,
                            {"Authorization": f"Bearer {key}",
                             "Content-Type": "application/json"}, timeout)
        if err:
            return None, err
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return None, R.redact(f"unexpected response shape: {str(data)[:200]}")
    if not (text or "").strip():
        return None, ("empty reply; the model may have spent its whole output "
                      "budget on reasoning. Raise max_output_tokens for "
                      f"{member['name']} in reviewers.json.")
    return text, None


SYSTEM_PLANNER = """You are one of two members of a technical committee, chosen \
from a different provider than the other member so that you do not share a \
failure mode.

Your job in the planning phase is ROOT CAUSE ANALYSIS and a plan, not approval \
and not a defect hunt. State your assumptions. Ask why three levels deep. Say \
explicitly whether the thing being described is a symptom or the problem, and \
whether the proposed direction patches the symptom or removes the problem. You \
may propose a completely different approach: stepping back is the point.

Say what you considered and REJECTED, and why. That is often the most useful \
part of your answer.

In a later phase you will be shown an implementation and asked to review it \
AGAINST THE PLAN YOU HELPED WRITE. Judge drift and missing pieces then, not \
your idea of an ideal solution.

Be concrete. Where you are uncertain, say so plainly and say what would settle \
it. Do not pad."""


def phase_prompt(problem):
    return (
        f"{problem}\n\n"
        "Do root cause analysis. State your assumptions. Ask why three levels "
        "deep. Are we patching a symptom or removing the problem? What would "
        "you consider and reject, and why?\n\n"
        "End your answer with a section headed 'PLAN' containing numbered "
        "steps and explicit acceptance criteria: what must be true for the "
        "implementation to be correct. Those criteria are what you will "
        "review against later, so make them checkable."
        + NO_EDITS)


def run_turn(members, session, prompt, timeout, label):
    """Both members, in parallel, same prompt. Waits for BOTH: the reference
    skill is explicit that you wait for both, not whichever finishes first."""
    import concurrent.futures
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        futs = {ex.submit(ask_member, m,
                          session["members"][m["name"]]["history"],
                          prompt, timeout): m for m in members}
        for fut in concurrent.futures.as_completed(futs):
            m = futs[fut]
            try:
                text, err = fut.result()
            except Exception as e:                     # noqa: BLE001
                text, err = None, f"{type(e).__name__}: {e}"
            results[m["name"]] = (text, err)
    for m in members:
        text, err = results[m["name"]]
        rec = session["members"][m["name"]]
        rec["history"].append({"role": "user", "content": prompt})
        if err:
            rec["history"].append({"role": "assistant",
                                   "content": f"[no reply: {err}]"})
            rec.setdefault("errors", []).append({"phase": label, "error": err})
        else:
            rec["history"].append({"role": "assistant", "content": text})
    session["turns"].append({"label": label, "prompt": prompt,
                             "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
    return results


def show_results(results, quiet=False):
    ok = 0
    for name, (text, err) in sorted(results.items()):
        print(f"\n{'=' * 70}\n{name}\n{'=' * 70}")
        if err:
            print(f"  [no reply] {err}")
        else:
            ok += 1
            print(text if not quiet else text[:2000])
    return ok


def cmd_open(args):
    members = pick_members(args.member)
    problem = Path(args.problem_file).read_text() if args.problem_file \
        else args.problem
    if not problem or not problem.strip():
        die("a committee needs a problem statement; pass --problem or "
            "--problem-file")
    p = session_path(args.name)
    if p.exists() and not args.force:
        die(f"session {args.name!r} already exists at {p}; use `ask` to "
            f"continue it, or --force to replace it")
    session = {"name": args.name,
               "opened_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
               "phase": "plan",
               "problem": problem,
               "members": {m["name"]: {"provider": m["provider"],
                                       "model": m["model"],
                                       "history": []} for m in members},
               "turns": []}
    names = ", ".join(f"{m['name']} ({m['provider']})" for m in members)
    print(f"committee {args.name!r}: {names}\nphase 1 (plan), waiting for "
          f"both members...\n")
    results = run_turn(members, session, phase_prompt(problem), args.timeout,
                       "plan")
    save_session(args.name, session)
    ok = show_results(results)
    print(f"\n{'-' * 70}")
    print(f"session saved: {session_path(args.name)}")
    print(f"{ok}/2 members answered.")
    if ok < 2:
        print("A committee needs both. Re-run the missing member with "
              "`committee.py ask` once its provider recovers.")
        return STATES["UNAVAILABLE"]
    print("\nNext: CHALLENGE them before accepting anything. e.g.\n"
          "  committee.py ask %s --prompt \"Why does <X> happen? Symptom or "
          "cause?\"\n"
          "  committee.py ask %s --prompt \"What did you consider and "
          "reject?\"\n"
          "Then synthesise. Convergence -> unified plan; real divergence -> "
          "ask the user." % (args.name, args.name))
    return STATES["COMMITTEE_OK"]


def cmd_ask(args):
    session = load_session(args.name)
    members = [r for r in R.load_reviewers()
               if r["name"] in session["members"]]
    if len(members) != len(session["members"]):
        die(f"reviewers.json no longer defines every member of this session "
            f"({', '.join(session['members'])}). Restore them, or open a new "
            f"session.")
    prompt = Path(args.prompt_file).read_text() if args.prompt_file \
        else args.prompt
    if not prompt or not prompt.strip():
        die("pass --prompt or --prompt-file")
    results = run_turn(members, session, prompt.rstrip() + NO_EDITS,
                       args.timeout, args.label or "ask")
    save_session(args.name, session)
    ok = show_results(results)
    return STATES["COMMITTEE_OK"] if ok == 2 else STATES["UNAVAILABLE"]


def cmd_review(args):
    """Phase 3: the diff goes back to the SAME members, against THEIR plan."""
    session = load_session(args.name)
    members = [r for r in R.load_reviewers() if r["name"] in session["members"]]
    # review.py exposes git_out(*args) -> str, not run(argv) -> (rc, out, err).
    # This called a function that has never existed, so phase 3 died with an
    # AttributeError every time it was invoked: the committee could plan and be
    # challenged, and could never review. Found by using it.
    if args.range:
        diff = R.git_out("diff", args.range)
    elif args.staged:
        diff = R.git_out("diff", "--staged")
    else:
        diff = R.git_out("diff")
    if not diff.strip():
        # git_out returns "" for a failed git as well as for an empty diff, so
        # the two cannot be distinguished here. Say both rather than asserting
        # the wrong one.
        die("no diff to review: either the range matched nothing or git "
            "failed. Check the range you passed, or stage the change.")
    body = diff[:args.max_chars]
    truncated = len(diff) > args.max_chars
    prompt = (
        "Implementation is done. Review the changes below AGAINST THE PLAN you "
        "helped write earlier in this conversation.\n\n"
        "Flag drift and missing pieces. Judge against the plan's acceptance "
        "criteria, not against an ideal solution. If the implementation "
        "revealed that the PLAN was wrong, say that explicitly rather than "
        "reporting it as an implementation defect.\n\n"
        "Also answer: does this change make any HONEST use fail that "
        "previously worked?\n\n"
        + ("NOTE: the diff was truncated; judge only what is shown.\n\n"
           if truncated else "")
        + f"```diff\n{body}\n```" + NO_EDITS)
    session["phase"] = "review"
    results = run_turn(members, session, prompt, args.timeout, "review")
    save_session(args.name, session)
    ok = show_results(results)
    return STATES["COMMITTEE_OK"] if ok == 2 else STATES["UNAVAILABLE"]


def cmd_show(args):
    session = load_session(args.name)
    print(f"committee {session['name']!r}  phase={session['phase']}  "
          f"opened={session['opened_at']}")
    for name, rec in session["members"].items():
        turns = len([h for h in rec["history"] if h["role"] == "assistant"])
        errs = len(rec.get("errors") or [])
        print(f"  {name:18} {rec['provider']:11} turns={turns} errors={errs}")
    print(f"\nturns: {', '.join(t['label'] for t in session['turns']) or 'none'}")
    if args.full:
        for name, rec in session["members"].items():
            print(f"\n{'=' * 70}\n{name}\n{'=' * 70}")
            for h in rec["history"]:
                who = "YOU" if h["role"] == "user" else name
                print(f"\n--- {who} ---\n{h['content']}")
    return STATES["COMMITTEE_OK"]


def main():
    ap = argparse.ArgumentParser(
        prog="committee.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    o = sub.add_parser("open", help="phase 1: two members plan, in parallel")
    o.add_argument("name")
    o.add_argument("--problem")
    o.add_argument("--problem-file")
    o.add_argument("--member", action="append", default=[],
                   help="override the plan panel; name two, repeatable or "
                        "comma-separated")
    o.add_argument("--timeout", type=int, default=1800)
    o.add_argument("--force", action="store_true")
    o.set_defaults(fn=cmd_open)

    a = sub.add_parser("ask", help="follow-up to both members, with context")
    a.add_argument("name")
    a.add_argument("--prompt")
    a.add_argument("--prompt-file")
    a.add_argument("--label")
    a.add_argument("--timeout", type=int, default=1800)
    a.set_defaults(fn=cmd_ask)

    r = sub.add_parser("review", help="phase 3: the diff, against their plan")
    r.add_argument("name")
    r.add_argument("--range")
    r.add_argument("--staged", action="store_true")
    r.add_argument("--max-chars", type=int, default=100_000)
    r.add_argument("--timeout", type=int, default=1800)
    r.set_defaults(fn=cmd_review)

    s = sub.add_parser("show", help="session state, or the full transcript")
    s.add_argument("name")
    s.add_argument("--full", action="store_true")
    s.set_defaults(fn=cmd_show)

    args = ap.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
