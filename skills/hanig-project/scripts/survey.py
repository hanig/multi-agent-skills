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

# survey is read-only unless --out was explicitly requested; importing the
# additive agent diagnostics must not create bytecode beside copied scripts.
sys.dont_write_bytecode = True
import agent_diagnostics

MAX_TREE_ENTRIES = 4000
MAX_DEPTH = 6
MAX_DIRS = 20_000        # a fan-out can leave hundreds of thousands of empty dirs
MAX_ENTRIES_PER_DIR = 5_000   # one directory must not be materialised whole
WALK_SECONDS = 20        # a survey that hangs is worse than a partial one
# The HARD bound (see _walk), and the grace is deliberate: the walk gets 10s
# past its own deadline to NOTICE that deadline and report a coherent partial
# result, so only a walk that cannot come back at all is killed outright.
WALK_KILL_SECONDS = WALK_SECONDS + 10
REAP_SECONDS = 2         # a child parked in a syscall may never die; do not wait on it
WALK_TALLY_DIRS = 100    # how often the child reports what it may not survive
WALK_EXTS_INLINE = 40    # a small histogram rides every progress line; a big one does not
WALK_TAIL_BYTES = 256_000     # only the END of the child's progress is read
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
                # Kill, then wait with a BOUND. A child parked in an
                # uninterruptible syscall -- a dead automount, a stale NFS
                # handle -- does not die until that syscall returns, and a
                # bare proc.wait() here would hand the hang straight back to
                # the survey. Same defect as the walk's, one function up.
                proc.kill()
                try:
                    proc.wait(timeout=REAP_SECONDS)
                except subprocess.TimeoutExpired:
                    return 127, "", (f"timed out after {timeout}s and did "
                                     f"not die when it was killed")
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
        # The one cluster fact that actually changes how a plan is written,
        # and now the one `swarm.py validate` refuses a memory-less unit on.
        # ABSENT, not False, when scontrol printed no DefMemPerNode: `or ""`
        # turned "the config did not say" into "no flag needed", which is the
        # unknown-reads-as-fine direction, and a consumer cannot tell the two
        # apart once it is written down as a bool.
        dm = out.get("config", {}).get("DefMemPerNode")
        if dm is not None:
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


# ---------------------------------------------------------------------------
# THE WALK, and the only bound on it that actually holds.
#
# WALK_SECONDS used to be the whole story: `deadline = time.time() +
# WALK_SECONDS`, checked in the `while stack` loop. That bounds a walk that
# is doing TOO MUCH. It does not bound a walk that is doing NOTHING. When
# os.scandir blocks inside opendir() the loop never reaches its own check, so
# the walk never returns and neither does the survey. Measured here against
# $HOME: still running at 45s, and two orphaned instances wedged 59 and 47
# minutes on 0.07s of CPU, both parked under ~/Library/CloudStorage. The
# existing guards (MAX_DIRS, MAX_ENTRIES_PER_DIR, the explicit scandir stack)
# are correct and are kept -- they were written against fan-out, and this
# failure is LATENCY, not volume.
#
# It matters more than a slow tool. Step 1 of hanig-project is to survey the
# host, and lambda/andromeda/chimera keep $HOME and the project trees on
# network filesystems, where a stale NFS handle or a dead automount blocks
# opendir() far longer than a sync daemon does. The first thing anyone runs
# hangs, with no output and no error.
#
# So the walk runs in a CHILD PROCESS killed on a real deadline. `run` above
# already bounds every cluster command that way, so the pattern is this
# file's own. SIGALRM was the cheaper option and was rejected: a thread
# parked in an uninterruptible syscall on a hard NFS mount does not run the
# handler until the syscall returns, which is exactly the case being defended
# against, and it would also cut the walk at an arbitrary point. A child
# needs no cooperation from the stuck code at all -- worst case it cannot even
# be reaped, and the survey still returns on time and says so. Pruning
# (CloudStorage is in `skip` below) is a complement, not the bound: it stops
# us paying the deadline for a directory already known to be hostile, and it
# can only ever name the hostile places somebody already found.
#
# How a walk ENDED is a fact about the walk, not about the tree:
#
#   complete  -- it saw the whole tree within its bounds
#   truncated -- a declared cap stopped it: THERE WAS TOO MUCH
#   stuck     -- a syscall did not return and the child was killed: SOMETHING
#                DID NOT ANSWER, and how much is missing cannot be known
#   unknown   -- the child produced no verdict at all
#
# The last three all mean file_count, size_mb and extensions are FLOORS. That
# `truncated` and `stuck` stay APART is the same rule the LIMIT_* states exist
# for: "there was too much" is a fact about the tree that a plan author can
# act on, and "a filesystem did not answer" is a warning about the host.
# Reporting the second as the first is the quiet lie this repo keeps banning.
WALK_OK, WALK_TRUNCATED = "complete", "truncated"
WALK_STUCK, WALK_UNKNOWN = "stuck", "unknown"


def _tally_walk(root, deadline, report=None):
    """Count files by extension under `root`. Returns (counts, state, why).

    The fan-out guards are unchanged from the loop this was lifted out of: an
    entry cap, a per-directory cap, a depth cap, a directory cap, and a
    wall-clock deadline checked between entries.

    That deadline is COOPERATIVE and cannot be anything else here -- this loop
    only reaches its own check between entries. Bounding a syscall that never
    returns is _walk's job, not this function's.

    `report`, when given, is called with one small JSON-able record per
    directory ENTERED and one per WALK_TALLY_DIRS directories counted, so a
    caller that has to KILL this walk still learns where it stopped and what
    it had counted by the time it did.
    """
    exts, files, total_bytes = {}, 0, 0
    state, why = WALK_OK, None
    skip = {".git", "node_modules", "__pycache__", ".venv", "venv",
            ".mypy_cache", ".cache", "site-packages", ".conda", ".micromamba",
            "miniconda3", "miniforge3", ".local", ".rustup", ".cargo",
            # A sync daemon's mount point, not a source tree: opendir() on it
            # blocked for tens of minutes on this laptop. Pruning it is an
            # optimisation, NOT the bound -- see the essay above.
            "CloudStorage"}
    # An explicit scandir STACK, not os.walk. os.walk builds the complete
    # dirnames and filenames lists for a directory before handing them over,
    # so a root with a million immediate children was materialised in full
    # before any guard could look at it -- two reviewers found that, and it is
    # the same class as the sorted(iterdir())[:60] that looked like a bound
    # and was not. Here every entry is counted as it is seen.
    stack, dirs_seen = [(str(root), 0)], 0

    def counts():
        return {"files": files, "bytes": total_bytes, "dirs": dirs_seen,
                "exts": exts}

    def emit(kind, **extra):
        """Report progress the walk may not survive to report again.

        The RUNNING COUNTS ride every record, including the per-directory
        one. Without that, a walk killed on its first hostile directory
        reported zero files for a tree it had already half-counted, which
        reads as an empty repo -- the exact confusion this whole change is
        about. The histogram rides along too while it is small, which in a
        real tree it always is; a tree with hundreds of distinct extensions
        would make a per-directory line expensive, so there it travels on the
        periodic tally instead and the survey says the histogram may lag."""
        if not report:
            return
        rec = dict(extra, t=kind, files=files, bytes=total_bytes,
                   dirs=dirs_seen)
        if kind != "enter" or len(exts) <= WALK_EXTS_INLINE:
            rec["exts"] = exts
        report(rec)

    while stack and state == WALK_OK:
        if time.time() > deadline:
            state = WALK_TRUNCATED
            why = (f"the walk reached its {WALK_SECONDS}s deadline after "
                   f"{dirs_seen} directories")
            break
        if dirs_seen > MAX_DIRS:
            state = WALK_TRUNCATED
            why = f"more than MAX_DIRS ({MAX_DIRS}) directories"
            break
        current, depth = stack.pop()
        dirs_seen += 1
        # BEFORE the scandir, deliberately. If this is the call that never
        # returns, this line is the only record of which directory it was.
        emit("enter", p=str(current))
        try:
            with os.scandir(current) as it:
                per_dir = 0
                for entry in it:
                    per_dir += 1
                    if per_dir > MAX_ENTRIES_PER_DIR:
                        state = WALK_TRUNCATED
                        why = (f"a directory held more than "
                               f"MAX_ENTRIES_PER_DIR ({MAX_ENTRIES_PER_DIR}) "
                               f"entries")
                        break
                    if files > MAX_TREE_ENTRIES:
                        state = WALK_TRUNCATED
                        why = f"more than MAX_TREE_ENTRIES ({MAX_TREE_ENTRIES}) files"
                        break
                    if time.time() > deadline:
                        state = WALK_TRUNCATED
                        why = (f"the walk reached its {WALK_SECONDS}s deadline "
                               f"after {dirs_seen} directories")
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
        if dirs_seen % WALK_TALLY_DIRS == 0:
            emit("tally")
    return counts(), state, why


def _walk_child(root):
    """--walk-only: walk, and stream progress a kill cannot take back.

    os.write on fd 1, one JSON line at a time, not print(): an unflushed
    buffer is exactly what a SIGKILLed child loses, and the whole point of
    this child is that it may be killed."""
    def report(rec):
        try:
            os.write(1, (json.dumps(rec) + "\n").encode())
        except (OSError, TypeError, ValueError):
            pass          # a survey is not worth failing over a progress line

    counts, state, why = _tally_walk(root, time.time() + WALK_SECONDS, report)
    report(dict(counts, t="done", state=state, why=why))
    return 0


def _walk_verdict(state, why, counts, bound="child", stuck_at=None):
    out = {"state": state, "bound": bound, "seconds": WALK_SECONDS,
           "kill_seconds": WALK_KILL_SECONDS,
           "dirs_seen": counts.get("dirs", 0)}
    if state != WALK_OK:
        # Always a reason, never a bare flag. A consumer deciding whether to
        # re-run needs to know whether it hit a cap or hit a wall.
        out["why"] = why or "no reason was recorded"
        out["note"] = ("file_count, size_mb and extensions are FLOORS: the "
                       "walk stopped before it ran out of tree, and on a "
                       "tree with many distinct extensions the histogram can "
                       "lag the file count by up to WALK_TALLY_DIRS "
                       "directories")
    if stuck_at:
        out["stuck_at"] = stuck_at
    if bound != "child":
        out["bound_note"] = ("no child could be launched, so only the "
                             "cooperative deadline applied: a syscall that "
                             "never returns could still outlast it")
    return out


def _tail(fh, limit):
    """The END of the child's progress log. The latest record is the one that
    matters, and a killed child leaves an arbitrarily long log behind it."""
    try:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - limit))
        return fh.read(limit + 4096).decode("utf-8", "replace")
    except OSError:
        return ""


def _walk(root):
    """Count the tree under a bound that holds even when a syscall does not.

    Returns (counts, walk): what was seen, and how the walk ENDED."""
    counts = {"files": 0, "bytes": 0, "dirs": 0, "exts": {}}
    self_path = os.path.abspath(__file__) if globals().get("__file__") else ""
    if not (self_path and sys.executable and os.path.isfile(self_path)):
        counts, state, why = _tally_walk(root, time.time() + WALK_SECONDS)
        return counts, _walk_verdict(state, why, counts, bound="cooperative")

    env = dict(os.environ)
    env.update(_CHILD_ENV)
    argv = [sys.executable, self_path, "--walk-only", str(root)]
    started, killed, rc = time.time(), True, None
    try:
        with tempfile.TemporaryFile() as fout, tempfile.TemporaryFile() as ferr:
            try:
                proc = subprocess.Popen(argv, stdout=fout, stderr=ferr,
                                        stdin=subprocess.DEVNULL, env=env)
            except OSError:
                counts, state, why = _tally_walk(root,
                                                 time.time() + WALK_SECONDS)
                return counts, _walk_verdict(state, why, counts,
                                             bound="cooperative")
            try:
                rc = proc.wait(timeout=WALK_KILL_SECONDS)
                killed = False
            except subprocess.TimeoutExpired:
                # Kill, then do NOT wait without a bound. A process parked in
                # an uninterruptible syscall does not die until that syscall
                # returns, and a bare proc.wait() here would hand the hang
                # straight back to the survey it exists to protect.
                proc.kill()
                try:
                    proc.wait(timeout=REAP_SECONDS)
                except subprocess.TimeoutExpired:
                    pass
            tail, err = _tail(fout, WALK_TAIL_BYTES), _tail(ferr, 2000)
    except OSError:
        return counts, _walk_verdict(WALK_UNKNOWN,
                                     "the walk child could not be run",
                                     counts)

    done, tally, enter = None, None, None
    for line in tail.splitlines():
        line = line.strip()
        # A tail read starts mid-line, and a kill can cut one in half.
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        kind = rec.get("t")
        if kind == "done":
            done = rec
        elif kind == "tally":
            tally = rec
        elif kind == "enter":
            enter = rec
    # Every counter here only ever goes up, so the newest value is the
    # largest one and the records can be merged without trusting their order
    # -- which matters, because a kill decides which of them is last.
    ext_at = -1
    for rec in (tally, enter, done):
        if not rec:
            continue
        for key in ("files", "bytes", "dirs"):
            counts[key] = max(counts[key], rec.get(key) or 0)
        if rec.get("exts") is not None and (rec.get("files") or 0) >= ext_at:
            counts["exts"], ext_at = rec["exts"], rec.get("files") or 0

    if killed:
        where = (enter or {}).get("p") or str(root)
        return counts, _walk_verdict(
            WALK_STUCK,
            f"a directory did not answer: os.scandir was still inside "
            f"{where} when the walk was killed at {WALK_KILL_SECONDS}s "
            f"(ran {round(time.time() - started, 1)}s). how much of the tree "
            f"is missing cannot be known from here",
            counts, stuck_at=where)
    if done:
        return counts, _walk_verdict(done.get("state") or WALK_UNKNOWN,
                                     done.get("why"), counts)
    why = f"the walk child exited rc={rc} without reporting a verdict"
    if err.strip():
        why += f": {err.strip().splitlines()[-1][:200]}"
    return counts, _walk_verdict(WALK_UNKNOWN, why, counts)


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

    # Language mix and size. HARD-BOUNDED four ways now, because the first
    # version was pointed at a cluster home directory and never returned:
    # an entry cap, a per-directory cap and a depth cap against a tree that
    # is too big, and a child process killed on a real deadline against a
    # tree that does not answer. A survey that hangs is worse than one that
    # says "I only looked at the first N" -- and worse again than one that
    # says WHY it stopped.
    counts, walk = _walk(root)
    out["file_count"] = counts["files"]
    # counted_truncated_at is the OLD flag and stays one: it answers "is this
    # count a total?" and nothing else, so it is truthy for a STUCK walk too
    # -- a floor rendering as a total is the failure it exists to stop. WHICH
    # kind of cut-short it was lives in out["walk"], where the two facts stay
    # unconflated.
    out["counted_truncated_at"] = (MAX_TREE_ENTRIES
                                   if walk["state"] != WALK_OK else None)
    out["walk"] = walk
    out["size_mb"] = round(counts["bytes"] / 1e6, 1)
    out["extensions"] = dict(sorted(counts["exts"].items(),
                                    key=lambda kv: -kv[1])[:15])

    # Documents that answer "what is this and what was decided".
    docs = []
    # _walk's child is the hard boundary for a filesystem that does not
    # answer.  Re-enumerating it with pathlib.glob would silently reintroduce
    # an unbounded scandir after that child was killed, so a partial survey
    # deliberately omits this convenience inventory.
    if walk["state"] == WALK_OK:
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
    else:
        out["documents_note"] = "not enumerated: the bounded repository walk did not finish"
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
    ap.add_argument("--walk-only", metavar="DIR", default=None,
                    help="internal: walk DIR and stream JSON progress on "
                         "stdout. This is the child the survey itself runs "
                         "and kills at WALK_KILL_SECONDS, so that a "
                         "directory whose opendir() never returns cannot "
                         "hang the survey.")
    args = ap.parse_args()

    # Before anything else: this mode must do nothing but the walk, since its
    # parent may kill it at any moment.
    if args.walk_only:
        return _walk_child(args.walk_only)

    # 2 adds the per-partition allowance block (allow_accounts,
    # deny_accounts, max_mem_per_cpu_mb, qos, qos_grptres). 3 adds
    # repo.walk, which says how the tree walk ENDED. Additive, but versioned
    # so a consumer can tell "this cluster restricts nothing" from "this
    # survey is older than the question", and "the walk finished" from "this
    # survey predates anyone asking".
    data = {"schema_version": 4, "machine": machine(),
            "scheduler": scheduler(), "repo": repo(args.repo),
            # Additive: consumers of the pre-existing machine/scheduler/repo
            # keys keep working while automation gets a stable readiness
            # report.  agent_diagnostics never starts an agent session.
            "agent_diagnostics": agent_diagnostics.diagnostics()}
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
    for name, agent in data["agent_diagnostics"]["agents"].items():
        print(f"  agent     {name}: present={agent['agent_present']['state']}, "
              f"installed={agent['installation']['state']}, "
              f"discovery={agent['discovery']['state']}, "
              f"workflow={agent['workflow']['state']}")
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
        # Say when the count is a floor rather than a total, and say WHICH
        # kind of floor. Presenting a capped walk as the whole tree would be a
        # quiet lie in the one file the interview is meant to trust; and "I
        # ran out of room" and "something did not answer" are different facts
        # that a reader acts on differently, so they get different words.
        walk = r.get("walk") or {}
        state = walk.get("state", WALK_OK)
        cap = "" if state == WALK_OK else (
            " (capped)" if state == WALK_TRUNCATED else " (CUT SHORT)")
        print(f"            {r['file_count']} files{cap}, {r['size_mb']} MB, "
              f"top: {', '.join(list(r['extensions'])[:6])}")
        if state != WALK_OK:
            print(f"            walk {state.upper()}: {walk.get('why')}")
        if r.get("documents"):
            print(f"            docs: "
                  f"{', '.join(d['path'] for d in r['documents'][:8])}")
        if r.get("existing_swarm_state"):
            print(f"            EXISTING swarm state: "
                  f"{', '.join(r['existing_swarm_state'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
