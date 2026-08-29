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
import time
from pathlib import Path

MAX_TREE_ENTRIES = 4000
MAX_DEPTH = 6
MAX_DIRS = 20_000        # a fan-out can leave hundreds of thousands of empty dirs
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


def redact(text):
    """Strip credentials from a string bound for the survey file.

    NOT a guarantee. It removes URL userinfo, common query-string secret
    parameters, and a list of known token families. A secret that matches none
    of those -- a passphrase in a commit subject, a bespoke internal token --
    passes through, and no pattern list can fix that. Treat the survey as
    sensitive, not as sanitised."""
    if not isinstance(text, str):
        return text
    out = _URL_CREDS.sub(lambda m: f"{m.group('scheme')}<redacted>@", text)
    out = _URL_QUERY_SECRET.sub(r"\1<redacted>", out)
    return _TOKENISH.sub("<redacted>", out)


def scrub(obj):
    """Redact recursively. Applied to the WHOLE survey on the way out, so a
    field added later is covered without anyone remembering to think about it.
    Fail closed: a new key is scrubbed by default, not exempt by default."""
    if isinstance(obj, dict):
        # KEYS TOO. A file named `x.sk-aaaaaaaaaaaaaaaa` becomes a key in the
        # extension map, and scrubbing only values let it through.
        return {scrub(k): scrub(v) for k, v in obj.items()}
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
    try:
        proc = subprocess.Popen(
            argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, env=env)
    except OSError:
        return 127, "", "could not run"
    try:
        # errors="replace": a commit message in ISO-8859-1 must not crash a
        # survey with a UnicodeDecodeError traceback.
        out = proc.stdout.read(MAX_OUTPUT).decode("utf-8", "replace")
        err = proc.stderr.read(2000).decode("utf-8", "replace")
        proc.stdout.close()
        proc.stderr.close()
        rc = proc.wait(timeout=timeout)
    except (subprocess.SubprocessError, OSError, ValueError):
        proc.kill()
        proc.wait()
        return 127, "", "timed out or could not be read"
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
        rc, o, _ = run(["git", "status", "--porcelain"], cwd=str(root))
        if rc == 0:
            out["dirty_files"] = len([l for l in o.splitlines() if l.strip()])

    # Language mix and size. HARD-BOUNDED in three ways, because the first
    # version was pointed at a cluster home directory and never returned:
    # an entry cap, a depth cap, and a wall-clock deadline. A survey that
    # hangs is worse than one that says "I only looked at the first N".
    exts, files, total_bytes, truncated = {}, 0, 0, False
    skip = {".git", "node_modules", "__pycache__", ".venv", "venv",
            ".mypy_cache", ".cache", "site-packages", ".conda", ".micromamba",
            "miniconda3", "miniforge3", ".local", ".rustup", ".cargo"}
    deadline = time.time() + WALK_SECONDS
    base_depth = len(root.parts)
    dirs_seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        # CHECKED PER DIRECTORY. Both guards used to sit inside the filename
        # loop, so 300,000 EMPTY directories -- exactly what a failed fan-out
        # leaves behind -- were walked without ever consulting either, and the
        # partial result was then reported as a complete count. Two reviewers
        # found this independently.
        dirs_seen += 1
        if dirs_seen > MAX_DIRS or time.time() > deadline:
            truncated = True
            break
        if len(Path(dirpath).parts) - base_depth >= MAX_DEPTH:
            dirnames[:] = []
        dirnames[:] = [d for d in dirnames
                       if d not in skip and not d.startswith(".")]
        for fn in filenames:
            files += 1
            if files > MAX_TREE_ENTRIES or time.time() > deadline:
                truncated = True
                break
            ext = Path(fn).suffix.lower() or "(none)"
            exts[ext] = exts.get(ext, 0) + 1
            try:
                total_bytes += (Path(dirpath) / fn).stat().st_size
            except OSError:
                pass
        if truncated:
            break
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
                  f"{r.get('branch','?')}, {r.get('commits','?')} commits, "
                  f"{r.get('dirty_files',0)} dirty")
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
