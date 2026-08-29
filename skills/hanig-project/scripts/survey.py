#!/usr/bin/env python3
"""survey.py: everything about this machine and this repo that can be LOOKED UP.

The point is subtraction. `grill-with-docs` says "if a question can be answered
by exploring the codebase, explore the codebase instead", and the swarm plan
makes that acceptance criterion 7(b): a question answerable by inspection is
never asked. This script is what makes that enforceable rather than aspirational
-- it produces the answers, so the interview can be about judgment only.

Standard library only, no network, safe on a login node. Read-only: it does not
write anything outside the file it is told to write.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MAX_TREE_ENTRIES = 4000
MAX_DEPTH = 6
MAX_DIRS = 20_000        # a fan-out can leave hundreds of thousands of empty dirs
MAX_ENTRIES_PER_DIR = 5_000   # one directory must not be materialised whole
WALK_SECONDS = 20        # a survey that hangs is worse than a partial one
RUN_TIMEOUT = 30
MAX_OUTPUT = 200_000     # a child cannot inflate the survey without limit


# Things that must never reach the survey file. It is read into a session,
# often committed, and sometimes pasted. A git remote can carry a token in its
# userinfo (https://user:TOKEN@host/...), which is exactly how a private
# credential ends up in a repo, and this script would have written it verbatim.
_URL_CREDS = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)"
                        r"(?P<user>[^/@\s]+)@")
# A credential also travels in a QUERY STRING (?private_token=...), which the
# userinfo pattern alone did not touch. A reviewer found that.
_URL_QUERY_SECRET = re.compile(
    r"([?&](?:private_token|access_token|token|api_key|apikey|password|"
    r'auth|key|secret)=)[^&\s"\']+', re.I)
# Token shapes. Deliberately a LIST OF FAMILIES rather than a claim of
# completeness: reviewers pointed out glpat- and github_pat_ were missing, and
# the honest lesson is that a closed list can never be complete. It catches
# the common shapes; it is not a guarantee, and the docstring says so.
_TOKENISH = re.compile(
    r"(gh[pousr]_[A-Za-z0-9]{16,}"             # GitHub classic
    r"|github_pat_[A-Za-z0-9_]{20,}"           # GitHub fine-grained
    r"|glpat-[A-Za-z0-9_-]{16,}"               # GitLab
    r"|gls-[A-Za-z0-9_-]{16,}"                 # GitLab shared runner
    r"|sk-[A-Za-z0-9_-]{16,}"                  # OpenAI-style
    r"|sk-ant-[A-Za-z0-9_-]{16,}"              # Anthropic
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"           # Slack
    r"|AKIA[0-9A-Z]{12,}"                      # AWS key id
    r"|ASIA[0-9A-Z]{12,}"                      # AWS session
    r"|lin_api_[A-Za-z0-9]{16,}"               # Linear
    r"|hf_[A-Za-z0-9]{16,}"                    # HuggingFace
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----)")   # a key pasted anywhere


# Prefixes that also occur in ordinary human names, and so cannot be redacted
# on sight without destroying legitimate data.
_AMBIGUOUS_PREFIXES = ("hf_", "sk-learn", "sk-", "github_pat_")


def _looks_random(body):
    """Does this look like a generated secret rather than a human name?

    Three reviewers found the previous version destroying legitimate data:
    a file called `hf_my_model_weights_1234567890123456` matched the `hf_`
    family and vanished into <redacted>, and a commit subject mentioning
    `github_pat_2024_roadmap` lost the reference. Both were the cries-wolf
    direction, and a scrubber that mangles ordinary filenames gets turned off.

    Real tokens are random base62: mixed case or dense digits, and almost no
    underscores. Human identifiers are lowercase words joined by underscores."""
    if body.count("_") >= 2:
        return False                       # my_model_weights, 2024_roadmap
    # MIXED CASE is the discriminator. Real tokens from these families are
    # base62 and essentially always contain both cases; human names are
    # lowercase. An earlier version accepted "digits present" as evidence and
    # so redacted `hf_bertbaseuncased2024run`, a perfectly ordinary model
    # directory -- found by checking which of a reviewer's examples actually
    # matched the pattern, three of which did not.
    return (any(c.isupper() for c in body)
            and any(c.islower() for c in body))


def redact(text):
    """Strip credentials from a string bound for the survey file.

    NOT a guarantee. It removes URL userinfo, common query-string secret
    parameters, and a list of known token families whose bodies also look
    generated. A secret that matches none of those -- a passphrase in a commit
    subject, a bespoke internal token -- passes through, and no pattern list
    can fix that. Treat the survey as sensitive, not as sanitised."""
    if not isinstance(text, str):
        return text
    out = _URL_CREDS.sub(lambda m: f"{m.group('scheme')}<redacted>@", text)
    out = _URL_QUERY_SECRET.sub(r"\1<redacted>", out)

    def _maybe(m):
        whole = m.group(0)
        # TWO CLASSES OF PREFIX, and conflating them was the bug in both
        # directions. Nobody names a file `ghp_...` or `glpat-...`, so those
        # are redacted unconditionally -- gating them on a randomness
        # heuristic let ghp_AAAABBBBCCCCDDDDEEEE through. But `hf_`, `sk-` and
        # `github_pat_` DO collide with ordinary names
        # (hf_my_model_weights, sk-learn-notes.md, github_pat_2024_roadmap),
        # so only those consult the heuristic.
        if whole.startswith(_AMBIGUOUS_PREFIXES):
            body = re.split(r"[_-]", whole, maxsplit=1)[-1]
            return "<redacted>" if _looks_random(body) else whole
        return "<redacted>"

    return _TOKENISH.sub(_maybe, out)


def scrub(obj):
    """Redact recursively. Applied to the WHOLE survey on the way out, so a
    field added later is covered without anyone remembering to think about it.
    Fail closed: a new key is scrubbed by default, not exempt by default."""
    if isinstance(obj, dict):
        # KEYS TOO. A file named `x.sk-aaaaaaaaaaaaaaaa` becomes a key in the
        # extension map, and scrubbing only values let it through.
        #
        # But two DISTINCT keys must not collapse onto one: `a.sk-aaa...` and
        # `a.sk-bbb...` both redact to `a.<redacted>`, and the second would
        # silently overwrite the first, losing a row from the survey. A
        # reviewer found that; collisions are disambiguated instead.
        out = {}
        for k, v in obj.items():
            key = scrub(k)
            if key in out and key != k:
                n = 2
                while f"{key}#{n}" in out:
                    n += 1
                key = f"{key}#{n}"
            out[key] = scrub(v)
        return out
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    return redact(obj)


# Environment for every child. This runs inside a directory somebody else
# controls, and the failure to avoid is a survey that HANGS rather than one
# that returns nothing: git will happily block forever asking for credentials
# or waiting on a pager, and a blocked survey blocks the whole interview.
_CHILD_ENV = {
    "GIT_TERMINAL_PROMPT": "0",     # never ask for credentials
    "GIT_PAGER": "cat",             # never start a pager
    "GIT_OPTIONAL_LOCKS": "0",      # do not take a lock in someone's repo
    "GCM_INTERACTIVE": "never",
    "SLURM_TIME_FORMAT": "standard",
}


# A repo's OWN .git/config can make git execute arbitrary commands:
# core.fsmonitor and core.hooksPath run on `git status`, core.pager and
# core.sshCommand and the filter/diff drivers on other operations. Surveying a
# repo you did not write therefore runs its author's code, which a reviewer
# pointed out and which matters precisely because this tool exists to study
# repos you did not write. These -c flags neutralise the known vectors.
#
# This is mitigation, NOT a sandbox. git has a large surface and only a
# container can make that claim; what is bounded here is the set of hooks git
# consults during the read-only commands below.
_GIT_SAFETY = ["-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null",
               "-c", "core.pager=cat", "-c", "core.sshCommand=false",
               "-c", "protocol.ext.allow=never", "-c", "uploadpack.allowFilter=false"]


def run(argv, cwd=None, timeout=RUN_TIMEOUT):
    """Never shell=True: argv is a list, so a path containing metacharacters
    is data. stdin is closed, because a child that reads stdin would otherwise
    block until the timeout and turn a survey into a stall.

    Output is read through a BOUNDED pipe rather than captured whole. A repo
    with a 500MB commit subject would otherwise be buffered entirely into this
    process before being truncated, on a login node shared by everyone."""
    if argv and argv[0] == "git":
        argv = [argv[0]] + _GIT_SAFETY + list(argv[1:])
    env = dict(os.environ)
    env.update(_CHILD_ENV)
    # Redirect to TEMP FILES rather than pipes. Reading a pipe blocks, so the
    # earlier version reached stdout.read() before wait(timeout=...) and the
    # timeout never applied; worse, a child that filled the stderr pipe while
    # we were blocked on stdout deadlocked outright. Files never block, the
    # timeout is enforced where it is written, and only the first MAX_OUTPUT
    # bytes are ever read into this process.
    try:
        with tempfile.TemporaryFile() as fout, tempfile.TemporaryFile() as ferr:
            try:
                proc = subprocess.Popen(argv, cwd=cwd, stdout=fout,
                                        stderr=ferr, stdin=subprocess.DEVNULL,
                                        env=env)
            except OSError:
                return 127, "", "could not run"
            try:
                rc = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                return 127, "", f"timed out after {timeout}s"
            fout.seek(0)
            ferr.seek(0)
            # errors="replace": a commit message in ISO-8859-1 must not crash
            # a survey with a UnicodeDecodeError traceback.
            out = fout.read(MAX_OUTPUT).decode("utf-8", "replace")
            err = ferr.read(2000).decode("utf-8", "replace")
    except OSError:
        return 127, "", "could not run"
    return rc, out, err


def machine():
    """Which box is this, and what can it run."""
    u = os.uname()
    out = {
        "hostname": u.nodename, "system": u.sysname, "release": u.release,
        "user": os.environ.get("USER"),
        "home": str(Path.home()),
        "python": sys.version.split()[0],
        "cpus": os.cpu_count(),
    }
    tools = ("sbatch", "squeue", "sacct", "sinfo", "nextflow", "snakemake",
             "paseo", "claude", "git", "docker", "apptainer", "singularity",
             "conda", "micromamba", "uv")
    # A non-interactive PATH is not the PATH a person gets. `claude` lives in
    # ~/.local/bin on the clusters, which ssh-without-a-login-shell does not
    # include, so `which` alone reported it missing on a host where it is
    # installed -- and would have sent someone to install what they have.
    extra = [Path.home() / ".local/bin", Path.home() / "bin",
             Path("/usr/local/bin"), Path("/opt/homebrew/bin")]
    for tool in tools:
        found = shutil.which(tool)
        if not found:
            for d in extra:
                cand = d / tool
                if cand.is_file() and os.access(cand, os.X_OK):
                    found = f"{cand} (not on this PATH)"
                    break
        if found:
            out.setdefault("tools", {})[tool] = found
    out.setdefault("tools", {})
    return out


def scheduler():
    """Slurm facts a plan author would otherwise have to ask for."""
    if not shutil.which("sinfo"):
        return {"present": False}
    out = {"present": True}
    rc, o, _ = run(["sinfo", "-h", "-o", "%P|%a|%l|%D"])
    if rc == 0:
        parts = []
        for line in o.splitlines():
            f = line.split("|")
            if len(f) >= 4:
                parts.append({"partition": f[0].strip().rstrip("*"),
                              "default": f[0].strip().endswith("*"),
                              "avail": f[1].strip(), "timelimit": f[2].strip(),
                              "nodes": f[3].strip()})
        out["partitions"] = parts
    rc, o, _ = run(["scontrol", "show", "config"])
    if rc == 0:
        for key in ("DefMemPerCPU", "DefMemPerNode", "MaxArraySize",
                    "SchedulerType"):
            m = re.search(rf"^{key}\s*=\s*(.+)$", o, re.M)
            if m:
                out.setdefault("config", {})[key] = m.group(1).strip()
        # The one cluster fact that actually changes how a plan is written.
        dm = (out.get("config", {}).get("DefMemPerNode") or "")
        out["mem_flag_required"] = ("UNLIMITED" in dm.upper())
    rc, o, _ = run(["sacctmgr", "-n", "show", "assoc",
                    f"user={os.environ.get('USER','')}", "format=Account%30"])
    if rc == 0:
        accts = sorted({a.strip() for a in o.splitlines() if a.strip()})
        if accts:
            out["accounts"] = accts
    return out


def storage(paths):
    out = []
    for p in paths:
        try:
            st = os.statvfs(p)
        except OSError:
            continue
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        out.append({"path": str(p), "total_gb": round(total / 1e9, 1),
                    "free_gb": round(free / 1e9, 1),
                    "used_pct": round(100 * (1 - free / total)) if total else None})
    return out


def repo(root):
    """What is already here. For a half-finished project this is most of the
    answer, and asking a human to recite it would be rude and slower."""
    root = Path(root).resolve()
    out = {"root": str(root), "exists": root.is_dir()}
    if not out["exists"]:
        return out

    rc, o, _ = run(["git", "rev-parse", "--is-inside-work-tree"], cwd=str(root))
    out["git"] = (rc == 0 and o.strip() == "true")
    if out["git"]:
        for key, argv in (
                ("head", ["git", "rev-parse", "--short", "HEAD"]),
                ("branch", ["git", "branch", "--show-current"]),
                ("remote", ["git", "remote", "get-url", "origin"]),
                ("commits", ["git", "rev-list", "--count", "HEAD"])):
            rc, o, _ = run(argv, cwd=str(root))
            if rc == 0:
                out[key] = o.strip()
        rc, o, _ = run(["git", "log", "-12", "--format=%h %ad %s",
                        "--date=short"], cwd=str(root))
        if rc == 0:
            out["recent_commits"] = o.strip().splitlines()
        # `git status` and `git diff` are NOT run here, and that is the whole
        # point of this comment. Both inspect working-tree CONTENT, so both
        # execute a filter driver the repo configures for itself via
        # .gitattributes -- measured, with a positive control, firing through
        # this script. A dirty-file count is a nice-to-have; arbitrary code
        # execution from a repo you are merely reading is not a price worth
        # paying for it. Every command above touches metadata only and was
        # measured NOT to fire a filter.
        out["dirty_files"] = None
        out["dirty_files_note"] = ("not collected: `git status` executes "
                                   "repo-configured filter drivers")

    # Language mix and size. HARD-BOUNDED in three ways, because the first
    # version was pointed at a cluster home directory and never returned:
    # an entry cap, a depth cap, and a wall-clock deadline. A survey that
    # hangs is worse than one that says "I only looked at the first N".
    exts, files, total_bytes, truncated = {}, 0, 0, False
    skip = {".git", "node_modules", "__pycache__", ".venv", "venv",
            ".mypy_cache", ".cache", "site-packages", ".conda", ".micromamba",
            "miniconda3", "miniforge3", ".local", ".rustup", ".cargo"}
    deadline = time.time() + WALK_SECONDS
    # An explicit scandir STACK, not os.walk. os.walk builds the complete
    # dirnames and filenames lists for a directory before handing them over,
    # so a root with a million immediate children was materialised in full
    # before any guard could look at it -- two reviewers found that, and it is
    # the same class as the sorted(iterdir())[:60] that looked like a bound
    # and was not. Here every entry is counted as it is seen.
    stack, dirs_seen = [(root, 0)], 0
    while stack and not truncated:
        if time.time() > deadline or dirs_seen > MAX_DIRS:
            truncated = True
            break
        current, depth = stack.pop()
        dirs_seen += 1
        try:
            with os.scandir(current) as it:
                per_dir = 0
                for entry in it:
                    per_dir += 1
                    if (per_dir > MAX_ENTRIES_PER_DIR
                            or files > MAX_TREE_ENTRIES
                            or time.time() > deadline):
                        truncated = True
                        break
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        continue
                    if is_dir:
                        if (entry.name not in skip
                                and not entry.name.startswith(".")
                                and depth + 1 < MAX_DEPTH):
                            stack.append((entry.path, depth + 1))
                        continue
                    files += 1
                    ext = Path(entry.name).suffix.lower() or "(none)"
                    exts[ext] = exts.get(ext, 0) + 1
                    try:
                        total_bytes += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            continue
    out["file_count"] = files
    out["counted_truncated_at"] = MAX_TREE_ENTRIES if truncated else None
    out["size_mb"] = round(total_bytes / 1e6, 1)
    out["extensions"] = dict(sorted(exts.items(), key=lambda kv: -kv[1])[:15])

    # Documents that answer "what is this and what was decided".
    docs = []
    for pat in ("README*", "CONTEXT.md", "CLAUDE.md", "MEMORY.md", "PLAN*.md",
                "docs/adr/*.md", "docs/*.md", "*.cff", "environment.y*ml",
                "pyproject.toml", "requirements*.txt", "Snakefile",
                "nextflow.config", "main.nf", "Makefile", "*.sbatch"):
        for f in sorted(root.glob(pat))[:12]:
            if f.is_file():
                try:
                    size = f.stat().st_size
                except OSError:
                    continue
                docs.append({"path": str(f.relative_to(root)), "bytes": size})
    out["documents"] = docs[:40]

    # An existing swarm project here? A recursive ** glob is what hung the
    # first version on a cluster home, so look only where state actually
    # lives rather than everywhere it could.
    for rel in (".swarm/state", "state", ".state"):
        cand = root / rel / "swarm-state.json"
        if cand.is_file():
            out.setdefault("existing_swarm_state", []).append(rel)
    # scandir + break, not sorted(iterdir())[:60]: sorting materialises every
    # child first, so a root with a million entries was enumerated in full
    # despite the slice that looks like a bound.
    try:
        with os.scandir(root) as it:
            for i, child in enumerate(it):
                if i >= 200:
                    break
                try:
                    if child.is_dir() and (Path(child.path) /
                                           ".swarm/state/swarm-state.json"
                                           ).is_file():
                        out.setdefault("existing_swarm_state", []).append(
                            f"{child.name}/.swarm/state")
                except OSError:
                    continue
    except OSError:
        pass
    return out


def main():
    ap = argparse.ArgumentParser(
        prog="survey.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=".",
                    help="the project directory to inspect (default: cwd)")
    ap.add_argument("--out", default=None,
                    help="write JSON here instead of stdout")
    ap.add_argument("--json", action="store_true",
                    help="JSON even when writing to stdout")
    args = ap.parse_args()

    data = {"schema_version": 1, "machine": machine(),
            "scheduler": scheduler(), "repo": repo(args.repo)}
    data["storage"] = storage([Path.home(), Path(args.repo).resolve()])
    # Scrub at the BOUNDARY, once, rather than at each field that might carry
    # a secret. Verified with a remote of the form
    # https://user:ghp_...@github.com/... , which the first version wrote out
    # verbatim into a file meant to be read, committed and pasted.
    data = scrub(data)

    if args.out:
        Path(args.out).write_text(json.dumps(data, indent=2, sort_keys=True))
        print(f"wrote {args.out}")
        return 0
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
        return 0

    m, s, r = data["machine"], data["scheduler"], data["repo"]
    print(f"  host      {m['hostname']}  ({m['system']}, python {m['python']}, "
          f"{m['cpus']} cpus)")
    print(f"  tools     {', '.join(sorted(m['tools'])) or 'none found'}")
    if s.get("present"):
        parts = [p["partition"] + ("*" if p["default"] else "")
                 for p in s.get("partitions", [])]
        print(f"  slurm     {len(parts)} partitions: {', '.join(parts[:10])}"
              f"{' ...' if len(parts) > 10 else ''}")
        if s.get("mem_flag_required"):
            print("            --mem is REQUIRED on this cluster")
        if s.get("accounts"):
            print(f"  accounts  {', '.join(s['accounts'])}")
    else:
        print("  slurm     not present")
    for st in data["storage"]:
        print(f"  disk      {st['path']}  {st['free_gb']}G free of "
              f"{st['total_gb']}G ({st['used_pct']}% used)")
    if r.get("exists"):
        print(f"  repo      {r['root']}")
        if r.get("git"):
            print(f"            git {r.get('head','?')} on "
                  f"{r.get('branch','?')}, {r.get('commits','?')} commits")
        # Say when the count is a floor rather than a total. Presenting a
        # capped walk as the whole tree would be a quiet lie in the one file
        # the interview is meant to trust.
        cap = " (capped)" if r.get("counted_truncated_at") else ""
        print(f"            {r['file_count']} files{cap}, {r['size_mb']} MB, "
              f"top: {', '.join(list(r['extensions'])[:6])}")
        if r.get("documents"):
            print(f"            docs: "
                  f"{', '.join(d['path'] for d in r['documents'][:8])}")
        if r.get("existing_swarm_state"):
            print(f"            EXISTING swarm state: "
                  f"{', '.join(r['existing_swarm_state'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
