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
MAX_QOS_ROWS = 500       # a QOS table is small; a runaway one is not the survey


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


# THREE STATES, and collapsing any two of them is the whole failure this block
# exists to prevent. Hours went into `QOSGrpCpuLimit` while a 736-CPU
# partition sat with 202 CPUs idle, because the survey reported how big every
# partition was and never reported who was ALLOWED to use it -- and "this
# partition restricts nobody" and "we could not find out who it restricts" are
# different facts. Written as an empty list, the second one reads as the
# first, which is the quiet lie a plan author cannot detect. So every limit
# below is one of:
#
#   {"state": "set",          "value": <the limit; it binds>}
#   {"state": "unrestricted", "value": None}   the cluster declares no limit
#   {"state": "unknown", "value": None, "why": ...}  assume NOTHING from this
LIMIT_SET, LIMIT_OPEN, LIMIT_UNKNOWN = "set", "unrestricted", "unknown"

# Slurm's several spellings of "nothing is limited here": ALL for the Allow*
# lists, UNLIMITED (or a bare 0) for the Max* numbers, N/A for an unset
# partition QOS. None of the three is evidence about any of the others, and
# none of them means the query failed.
_SLURM_OPEN = {"ALL", "UNLIMITED", "N/A", "NONE", "(NULL)", "0", ""}


def _limit(state, value=None, why=None):
    out = {"state": state, "value": value}
    if why:
        # Only on unknown, and always on unknown: a consumer that has to
        # decide whether to re-query needs to know WHICH command went missing.
        out["why"] = why
    return out


def _oneliner_fields(line):
    """Parse one record of `scontrol -o show ...` into a dict.

    `-o` is Slurm's own one-record-per-line format, asked for in preference to
    the human-readable block form for the reason this repo keeps relearning:
    scraping a layout somebody may reflow between releases is a bug waiting
    for an upgrade.

    A VALUE MAY ITSELF CONTAIN '=' (`TRES=cpu=736,mem=6000000M`), so each
    value runs to the next space and the left-to-right scan never mistakes the
    inner `cpu=736` for a key of its own -- checked with TRES sitting directly
    before the fields we actually read."""
    return {m.group(1): m.group(2) for m in re.finditer(r"(\w+)=(\S*)", line)}


def _parse_tres(text):
    """`cpu=512,mem=2000G,node=10` -> a dict.

    A bare count becomes an int so a consumer can compare it. Anything
    carrying a unit stays the string Slurm printed, because 2000G and 2000 are
    not the same number and this file must not pretend to know which was
    meant."""
    out = {}
    for item in (text or "").split(","):
        key, _, val = item.partition("=")
        key, val = key.strip(), val.strip()
        if key and val:
            out[key] = int(val) if val.isdigit() else val
    return out


def partition_records():
    """Every partition as `scontrol` describes it, keyed by name.

    Returns (records, why_unavailable). None is NOT {}: None means the query
    did not answer, {} means it answered and named no partition. That
    distinction is the reason this returns a pair at all."""
    if not shutil.which("scontrol"):
        return None, "scontrol is not on this PATH"
    rc, o, err = run(["scontrol", "-o", "show", "partition"])
    if rc != 0:
        return None, ("`scontrol -o show partition` failed: "
                      f"{err.strip()[:120] or rc}")
    if len(o) >= MAX_OUTPUT:
        # The read is capped mid-stream, so the last record is half a line and
        # `MaxMemPerCPU=51` would parse cleanly into a wrong number. Unknown
        # beats half-parsed; the partition list from sinfo is unaffected.
        return None, (f"scontrol output hit the {MAX_OUTPUT}-byte read cap, "
                      "so the last record is truncated; not parsed")
    recs = {}
    for line in o.splitlines():
        fields = _oneliner_fields(line)
        name = fields.get("PartitionName", "").strip()
        if name:
            recs[name] = fields
    return recs, None


def qos_grptres():
    """GrpTRES per QOS name, from `sacctmgr`.

    This is the number that produced `QOSGrpCpuLimit` on an idle partition:
    the ceiling is on the QOS, not on the partition, so nothing in `sinfo`
    can see it and a plan author sizing a job from partition CPUs has no idea
    it is there.

    `-n -P` is sacctmgr's parseable output. The column form pads AND TRUNCATES
    long names -- that is what the `%30` on the assoc query below is working
    around -- and a truncated QOS name joins to no partition at all, which
    would look exactly like a partition with no QOS.

    Returns (table, why_unavailable). None is NOT {}: {} means sacctmgr
    answered and no QOS exists."""
    if not shutil.which("sacctmgr"):
        return None, "sacctmgr is not on this PATH"
    rc, o, err = run(["sacctmgr", "-n", "-P", "show", "qos",
                      "format=Name,GrpTRES"])
    if rc != 0:
        return None, f"`sacctmgr show qos` failed: {err.strip()[:120] or rc}"
    if len(o) >= MAX_OUTPUT:
        return None, (f"sacctmgr output hit the {MAX_OUTPUT}-byte read cap, "
                      "so the last row is truncated; not parsed")
    table = {}
    for line in o.splitlines()[:MAX_QOS_ROWS]:
        name, _, tres = line.partition("|")
        if name.strip():
            table[name.strip()] = _parse_tres(tres)
    return table, None


def partition_limits(fields, why, qos_table, qos_why):
    """The limit fields for ONE partition, in the three-state shape.

    `fields` is that partition's `scontrol` record, or None when scontrol did
    not answer -- in which case every field is unknown and `why` says which
    command was missing. Nothing here ever returns an empty allowance."""
    if fields is None:
        reason = why or "scontrol did not report this partition"
        return {k: _limit(LIMIT_UNKNOWN, why=reason)
                for k in ("allow_accounts", "deny_accounts",
                          "max_mem_per_cpu_mb", "qos", "qos_grptres")}

    # BOTH SIDES of the account rule, because AllowAccounts=ALL on a
    # partition that DENIES your account is not the open partition it reads
    # as, and reporting only the allowance would recreate the original bug
    # with a second field.
    #
    # Slurm prints exactly ONE of the two: `AllowAccounts` when an allow-list
    # is set or neither is, and `DenyAccounts` instead when only a deny-list
    # is. So the absence of one key, WITH the other present, is an answer --
    # "nothing is denied here" -- and only the absence of both is ignorance.
    # Calling a missing DenyAccounts unknown printed `denied=UNKNOWN` beside
    # every partition on a cluster that denies nobody, which is the
    # cries-wolf direction that gets a field ignored.
    def _accounts(key, other):
        raw = fields.get(key)
        if raw is None:
            if fields.get(other) is not None:
                return _limit(LIMIT_OPEN)
            return _limit(LIMIT_UNKNOWN,
                          why="scontrol printed neither AllowAccounts nor "
                              "DenyAccounts for this partition")
        if raw.strip().upper() in _SLURM_OPEN:
            return _limit(LIMIT_OPEN)
        return _limit(LIMIT_SET,
                      [a for a in (x.strip() for x in raw.split(",")) if a])

    out = {"allow_accounts": _accounts("AllowAccounts", "DenyAccounts"),
           "deny_accounts": _accounts("DenyAccounts", "AllowAccounts")}

    raw = fields.get("MaxMemPerCPU")
    if raw is None:
        out["max_mem_per_cpu_mb"] = _limit(
            LIMIT_UNKNOWN, why="scontrol printed no MaxMemPerCPU")
    elif raw.strip().upper() in _SLURM_OPEN:
        out["max_mem_per_cpu_mb"] = _limit(LIMIT_OPEN)
    else:
        # Slurm prints plain megabytes here. A suffixed form is kept verbatim
        # rather than converted, because a wrong number is worse than a string
        # a human can read.
        mb = raw.strip()
        out["max_mem_per_cpu_mb"] = _limit(LIMIT_SET,
                                           int(mb) if mb.isdigit() else mb)

    raw = (fields.get("QOS") or "").strip()
    if not raw:
        out["qos"] = _limit(LIMIT_UNKNOWN,
                            why="scontrol printed no QOS for this partition")
        out["qos_grptres"] = _limit(LIMIT_UNKNOWN, why=out["qos"]["why"])
        return out
    if raw.upper() in _SLURM_OPEN:
        # No partition QOS attached, so no partition-QOS GrpTRES. See
        # qos_grptres_note: an association QOS can still cap you.
        out["qos"] = _limit(LIMIT_OPEN)
        out["qos_grptres"] = _limit(LIMIT_OPEN)
        return out
    out["qos"] = _limit(LIMIT_SET, raw)
    if qos_table is None:
        out["qos_grptres"] = _limit(LIMIT_UNKNOWN, why=qos_why or "not queried")
    elif raw not in qos_table:
        out["qos_grptres"] = _limit(
            LIMIT_UNKNOWN,
            why=f"the partition names QOS {raw!r}, which sacctmgr did not list")
    elif qos_table[raw]:
        out["qos_grptres"] = _limit(LIMIT_SET, qos_table[raw])
    else:
        out["qos_grptres"] = _limit(LIMIT_OPEN)
    return out


def _limits_line(part):
    """One line per partition for the human-readable output. UNKNOWN is
    printed as UNKNOWN rather than omitted: a field left off the line reads
    as a field with nothing in it."""
    bits = []
    for key, label in (("allow_accounts", "accounts"),
                       ("deny_accounts", "denied"),
                       ("max_mem_per_cpu_mb", "maxmem/cpu"),
                       ("qos_grptres", "grptres")):
        lim = part.get(key) or {}
        if lim.get("state") == LIMIT_UNKNOWN:
            bits.append(f"{label}=UNKNOWN")
        elif lim.get("state") != LIMIT_SET:
            continue
        elif isinstance(lim["value"], list):
            bits.append(f"{label}=" + ",".join(lim["value"][:6]))
        elif isinstance(lim["value"], dict):
            bits.append(f"{label}=" + ",".join(
                f"{k}={v}" for k, v in sorted(lim["value"].items())))
        elif key == "max_mem_per_cpu_mb" and isinstance(lim["value"], int):
            # The unit, spelled out. This is the number a memory request is
            # divided by to get the CPU count Slurm will actually charge.
            bits.append(f"{label}={lim['value']}M")
        else:
            bits.append(f"{label}={lim['value']}")
    return "  ".join(bits)


def scheduler():
    """Slurm facts a plan author would otherwise have to ask for."""
    if not shutil.which("sinfo"):
        return {"present": False}
    out = {"present": True}
    parts, seen = [], set()
    rc, o, _ = run(["sinfo", "-h", "-o", "%P|%a|%l|%D"])
    if rc == 0:
        for line in o.splitlines():
            f = line.split("|")
            if len(f) >= 4:
                name = f[0].strip().rstrip("*")
                parts.append({"partition": name,
                              "default": f[0].strip().endswith("*"),
                              "avail": f[1].strip(), "timelimit": f[2].strip(),
                              "nodes": f[3].strip()})
                seen.add(name)
    # WHO MAY USE IT, and what the allowance costs. Not one of these comes
    # from sinfo, and every one of them can refuse a job that fits the
    # partition's size -- which is how the survey came to report a 736-CPU
    # partition to somebody whose account was never allowed in it.
    recs, why = partition_records()
    qos_table, qos_why = qos_grptres()
    for name in sorted(recs or {}):
        if name in seen:
            continue
        # A partition scontrol knows and sinfo did not print (Hidden=YES is
        # the usual reason) is still a partition a plan may name.
        f = recs[name]
        parts.append({"partition": name,
                      "default": f.get("Default", "").upper() == "YES",
                      "avail": f.get("State", "").lower(),
                      "timelimit": f.get("MaxTime", ""),
                      "nodes": f.get("TotalNodes", "")})
        seen.add(name)
    for part in parts:
        part.update(partition_limits(
            (recs or {}).get(part["partition"]) if recs is not None else None,
            why, qos_table, qos_why))
    if rc == 0 or recs is not None:
        out["partitions"] = parts
    else:
        # ABSENT, not empty. A consumer that saw [] here would read "this
        # cluster has no partitions", which is never a true statement.
        out["partitions_unavailable"] = why or "sinfo did not answer"
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
    if qos_table is not None:
        # The whole table, not only the QOS the partitions name: a unit can
        # carry a --qos of its own, and validate will want to resolve that too.
        out["qos"] = {n: {"grptres": _limit(LIMIT_SET, t) if t
                          else _limit(LIMIT_OPEN)}
                      for n, t in sorted(qos_table.items())}
    elif qos_why:
        out["qos_unavailable"] = qos_why
    out["limits_note"] = (
        "allow_accounts, deny_accounts, max_mem_per_cpu_mb, qos and "
        "qos_grptres each carry state=set|unrestricted|unknown. UNKNOWN IS "
        "NOT UNRESTRICTED: it means the query did not answer, so assume "
        "nothing and say so rather than planning around it. A set "
        "max_mem_per_cpu_mb also fixes the CPU count, because Slurm charges "
        "ceil(mem_mb / max_mem_per_cpu_mb) CPUs and then refuses the job by "
        "naming CPUS, NOT MEMORY -- 700G at MaxMemPerCPU=5120 costs 140 CPUs, "
        "not the 32 that were asked for, and the error points away from the "
        "cause.")
    out["qos_grptres_note"] = (
        "the PARTITION QOS only. An account or association QOS can impose a "
        "GrpTRES this does not show, so an unrestricted qos_grptres is not a "
        "promise that nothing caps you.")
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
                        # A SYMLINK is neither walked nor counted. os.walk
                        # listed a symlink-to-directory under dirnames, so
                        # this rewrite began counting it as a FILE with no
                        # extension, inflating the count and inventing a
                        # "(none)" row. Following one also risks a cycle.
                        if entry.is_symlink():
                            continue
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

    # DO NOT OVERWRITE THESE. The skill is told to write PLAN.md and MEMORY.md,
    # and on the adopt path a repo may already have them. This repo's own
    # PLAN.md is a 66 KB design document; following the instruction literally
    # would have destroyed it, and the adopt section said steps 1 and 2 change
    # but "the rest does not", which endorsed exactly that.
    #
    # Reported as its own field rather than left inside `documents`, because a
    # list of forty paths is something you skim and a list of things you are
    # about to clobber is something you read.
    protected = []
    for name in ("PLAN.md", "MEMORY.md", "README.md", "plan.json",
                 "tickets.json", "findings.json"):
        f = root / name
        try:
            if f.is_file():
                protected.append({"path": name, "bytes": f.stat().st_size})
        except OSError:
            continue
    out["protected_docs"] = protected
    if protected:
        out["protected_docs_note"] = (
            "these already exist and were NOT written by this run. Write the "
            "plan to .swarm/ instead of overwriting them, and never replace a "
            "PLAN.md you did not create.")

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

    # 2 adds the per-partition allowance block (allow_accounts,
    # deny_accounts, max_mem_per_cpu_mb, qos, qos_grptres). Additive, but
    # versioned so a consumer can tell "this cluster restricts nothing"
    # from "this survey is older than the question".
    data = {"schema_version": 2, "machine": machine(),
            "scheduler": scheduler(), "repo": repo(args.repo)}
    # DEDUPE. Surveying a home directory printed the same filesystem twice,
    # on every host, because home and the repo resolve to the same path.
    seen, paths = set(), []
    for cand in (Path.home(), Path(args.repo).resolve()):
        if str(cand) not in seen:
            seen.add(str(cand))
            paths.append(cand)
    data["storage"] = storage(paths)
    # Scrub at the BOUNDARY, once, rather than at each field that might carry
    # a secret. Verified with a remote of the form
    # https://user:ghp_...@github.com/... , which the first version wrote out
    # verbatim into a file meant to be read, committed and pasted.
    data = scrub(data)

    if args.out:
        # The skill's very FIRST command is `--out .swarm/survey.json`, and
        # .swarm does not exist yet, so this raised FileNotFoundError before
        # anything else could happen.
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(data, indent=2, sort_keys=True))
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
        # The allowance, per partition. Printed because the machine-readable
        # field exists for validate and a human reading the terminal was the
        # one who lost the hours.
        for part in s.get("partitions", [])[:12]:
            line = _limits_line(part)
            if line:
                print(f"  limits    {part['partition']}: {line}")
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
