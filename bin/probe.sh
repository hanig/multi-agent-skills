#!/bin/sh
# probe.sh — read-only environment discovery for a Claude Code / skills deployment.
#
# Answers, in one pass on one host:
#   - can Claude Code even run here (node, egress, auth reachability)?
#   - do Arc org skills reach this machine, or only the Mac?
#   - what scheduler, storage, and container runtime are actually present?
#   - what portability constraints apply (python/git/sed/readlink flavor)?
#
# Safe by construction: reads only, writes only inside a temp dir it removes,
# never prints environment variable VALUES, never touches credentials.
#
# Usage:
#   sh probe.sh              human-readable report
#   sh probe.sh --json       machine-readable, for diffing across clusters
#
# Deploy:
#   scp probe.sh chimera:~/ && ssh chimera 'sh ~/probe.sh' > probe-chimera.txt

set -u

JSON=0
[ "${1:-}" = "--json" ] && JSON=1

TMPDIR_PROBE=$(mktemp -d 2>/dev/null || mktemp -d -t probe)
trap 'rm -rf "$TMPDIR_PROBE"' EXIT INT TERM

# --- helpers ----------------------------------------------------------------

# Collected key=value pairs for the JSON emitter.
KV="$TMPDIR_PROBE/kv"
: > "$KV"

rec() { # rec <key> <value>
  printf '%s\t%s\n' "$1" "$2" >> "$KV"
}

say() {
  [ "$JSON" -eq 1 ] && return 0
  printf '%s\n' "$*"
}

hdr() {
  [ "$JSON" -eq 1 ] && return 0
  printf '\n=== %s ===\n' "$1"
}

have() { command -v "$1" >/dev/null 2>&1; }

# First line of a command's output, or a fallback string.
firstline() {
  out=$("$@" 2>/dev/null | head -n 1) || out=""
  [ -n "$out" ] || out="-"
  printf '%s' "$out"
}

# Report a tool's presence and version in one line.
tool() { # tool <label> <cmd> [version-args...]
  label="$1"; cmd="$2"; shift 2
  if have "$cmd"; then
    v=$(firstline "$cmd" "$@")
    say "  $label: $v"
    rec "tool.$label" "$v"
  else
    say "  $label: ABSENT"
    rec "tool.$label" "absent"
  fi
}

# Directory existence + entry count, without listing contents.
dirstat() { # dirstat <label> <path>
  label="$1"; path="$2"
  if [ -d "$path" ]; then
    n=$(ls -1 "$path" 2>/dev/null | wc -l | tr -d ' ')
    say "  $label: present ($n entries) — $path"
    rec "dir.$label" "present:$n"
  else
    say "  $label: absent — $path"
    rec "dir.$label" "absent"
  fi
}

# --- 1. identity ------------------------------------------------------------

hdr "IDENTITY"
HOST=$(hostname 2>/dev/null || echo unknown)
say "  hostname: $HOST";                 rec host "$HOST"
say "  user: ${USER:-${LOGNAME:-unknown}}"
rec user "${USER:-${LOGNAME:-unknown}}"
say "  uname: $(uname -srm 2>/dev/null)"; rec uname "$(uname -srm 2>/dev/null)"

if [ -r /etc/os-release ]; then
  OSNAME=$(. /etc/os-release 2>/dev/null; printf '%s' "${PRETTY_NAME:-unknown}")
else
  OSNAME=$(uname -s)
fi
say "  os: $OSNAME";                      rec os "$OSNAME"
say "  shell: ${SHELL:-unknown}";         rec shell "${SHELL:-unknown}"
say "  home: $HOME";                      rec home "$HOME"

# --- 2. toolchain -----------------------------------------------------------

hdr "TOOLCHAIN (portability constraints)"
tool python3 python3 --version
tool git     git     --version
tool node    node    --version
tool npm     npm     --version
tool curl    curl    --version
tool rsync   rsync   --version
tool tar     tar     --version

# git >= 2.28 gates `git init -b`; older forces init+fetch+checkout.
if have git; then
  GV=$(git --version 2>/dev/null | awk '{print $3}')
  GMAJ=$(printf '%s' "$GV" | cut -d. -f1)
  GMIN=$(printf '%s' "$GV" | cut -d. -f2)
  if [ "${GMAJ:-0}" -gt 2 ] 2>/dev/null || { [ "${GMAJ:-0}" -eq 2 ] 2>/dev/null && [ "${GMIN:-0}" -ge 28 ] 2>/dev/null; }; then
    say "  git init -b supported: yes";   rec git.init_b yes
  else
    say "  git init -b supported: NO (use init+fetch+checkout)"; rec git.init_b no
  fi
fi

# Node >= 18 is Claude Code's floor.
if have node; then
  NV=$(node --version 2>/dev/null | tr -d 'v' | cut -d. -f1)
  if [ "${NV:-0}" -ge 18 ] 2>/dev/null; then
    say "  node >= 18: yes";              rec node.ok yes
  else
    say "  node >= 18: NO (Claude Code needs >= 18)"; rec node.ok no
  fi
fi

# GNU vs BSD userland changes sed/readlink/date flags in install scripts.
if sed --version >/dev/null 2>&1; then
  say "  sed flavor: GNU";                rec sed.flavor gnu
else
  say "  sed flavor: BSD/other (no --version)"; rec sed.flavor bsd
fi
if readlink -f / >/dev/null 2>&1; then
  say "  readlink -f: supported";         rec readlink.f yes
else
  say "  readlink -f: NOT supported";     rec readlink.f no
fi

# --- 3. claude + skills (open question 1) -----------------------------------

hdr "CLAUDE CODE + SKILL STORES"

# Non-interactive SSH does not source .bashrc, so conda/micromamba/nvm prefixes
# are missing from PATH. Tools installed there look ABSENT unless we re-check
# through a login shell. Getting this wrong reports a false "cannot run here".
# -i (interactive) is required: conda/micromamba/nvm init lives in .bashrc,
# which a non-interactive login shell (-lc) still skips. Job-control warnings
# on a non-tty are filtered out.
login_which() { # login_which <cmd> -> path, or empty
  bash -lic "command -v $1" 2>/dev/null | grep -v '^bash:' | head -n 1
}
login_ver() { # login_ver <cmd> <flag> -> version string, or empty
  bash -lic "$1 $2" 2>/dev/null | grep -v '^bash:' | head -n 1
}

for c in node npm claude; do
  p=$(command -v "$c" 2>/dev/null)
  if [ -n "$p" ]; then
    say "  $c: $p (on default PATH)"
    rec "login.$c" "path:$p"
  else
    lp=$(login_which "$c")
    if [ -n "$lp" ]; then
      lv=$(login_ver "$c" --version)
      say "  $c: $lp [login shell only] ${lv:+— $lv}"
      rec "login.$c" "loginshell:$lp"
    else
      say "  $c: ABSENT (not on default PATH or login shell)"
      rec "login.$c" absent
    fi
  fi
done

dirstat "~/.claude"             "$HOME/.claude"
dirstat "~/.claude/skills"      "$HOME/.claude/skills"
dirstat "~/.claude/plugins"     "$HOME/.claude/plugins"
dirstat "~/.claude-science"     "$HOME/.claude-science"
dirstat "~/.agents/skills"      "$HOME/.agents/skills"

# THE question: are org-managed skills visible here, or Mac-only?
ORG_SKILLS=0
for d in "$HOME"/.claude-science/orgs/*/skills; do
  [ -d "$d" ] || continue
  n=$(ls -1 "$d" 2>/dev/null | wc -l | tr -d ' ')
  ORG_SKILLS=$((ORG_SKILLS + n))
  say "  org skill store: $n skills at $d"
done
if [ "$ORG_SKILLS" -eq 0 ]; then
  say "  >>> ORG SKILLS NOT PRESENT on this host."
  say "  >>> Personal hanig-* skills must be self-sufficient here."
else
  say "  >>> org skills present: $ORG_SKILLS"
fi
rec org.skills "$ORG_SKILLS"

PERSONAL=0
[ -d "$HOME/.claude/skills" ] && PERSONAL=$(ls -1 "$HOME/.claude/skills" 2>/dev/null | wc -l | tr -d ' ')
rec personal.skills "$PERSONAL"

# --- 4. scheduler -----------------------------------------------------------

hdr "SCHEDULER"
SCHED="none"
if have sinfo || have sbatch; then
  SCHED="slurm"
  say "  flavor: Slurm"
  tool sbatch sbatch --version
  if have sinfo; then
    say "  partitions (name/avail/timelimit/nodes):"
    [ "$JSON" -eq 1 ] || sinfo -h -o '    %P %a %l %D' 2>/dev/null | head -20
    PARTS=$(sinfo -h -o '%P' 2>/dev/null | tr -d '*' | sort -u | tr '\n' ',' | sed 's/,$//')
    rec slurm.partitions "${PARTS:--}"
  fi
  if have sacctmgr; then
    ACCTS=$(sacctmgr -nP show assoc user="${USER:-x}" format=Account 2>/dev/null \
            | sort -u | tr '\n' ',' | sed 's/,$//')
    say "  accounts: ${ACCTS:--}"
    rec slurm.accounts "${ACCTS:--}"
  fi
  # sacct is what a job-contract verifier depends on; confirm it actually works.
  if have sacct && sacct -n -X --starttime now-7days -o JobID >/dev/null 2>&1; then
    say "  sacct queryable: yes"; rec sacct.ok yes
  else
    say "  sacct queryable: NO (verifier must fall back to exit codes + artifacts)"
    rec sacct.ok no
  fi
elif have qstat || have qsub; then
  SCHED="sge"
  say "  flavor: SGE/UGE (NOT Slurm — needs a separate adapter)"
  tool qstat qstat -help
else
  say "  flavor: none detected (interactive host?)"
fi
rec scheduler "$SCHED"

# Slurm-on-Kubernetes (SUNK, as on andromeda/gefion) changes two assumptions
# that the workflow verifier depends on: whether $HOME survives a pod restart,
# and whether slurmdbd/sacct history exists at all.
K8S="no"
[ -n "${KUBERNETES_SERVICE_HOST:-}" ] && K8S="yes"
[ -d /var/run/secrets/kubernetes.io ] && K8S="yes"
have kubectl && K8S="${K8S}+kubectl"
if [ "$K8S" != "no" ]; then
  say "  >>> Kubernetes-backed host detected ($K8S) — likely SUNK."
  say "  >>> Verify \$HOME persistence across restarts; prefer shared-FS install."
fi
rec k8s "$K8S"

# --- 5. workflow engines ----------------------------------------------------

hdr "WORKFLOW ENGINES"
tool nextflow  nextflow  -version
tool snakemake snakemake --version

# --- 6. containers ----------------------------------------------------------

hdr "CONTAINERS"
tool apptainer  apptainer  --version
tool singularity singularity --version
tool docker     docker     --version

# --- 7. storage -------------------------------------------------------------

hdr "STORAGE"
# Candidates across chimera / lambda / andromeda; only present ones are shown.
for p in "$HOME" /large_storage /scratch /data /checkpoints /cold-storage \
         /common_datasets /processed_datasets /mnt/gcs /work /project /projects \
         /mnt/weka /mnt/r2-cold-storage-pvc; do
  [ -d "$p" ] || continue
  line=$(df -h "$p" 2>/dev/null | tail -n 1)
  if [ -n "$line" ]; then
    # Fields 1-5 are Filesystem/Size/Used/Avail/Capacity on both GNU and BSD df
    # -h. Counting from the right breaks: "Mounted on" contains a space, and
    # macOS adds iused/ifree/%iused columns that Linux does not have.
    fs=$(printf '%s' "$line" | awk '{print $1}')
    size=$(printf '%s' "$line" | awk '{print $2}')
    avail=$(printf '%s' "$line" | awk '{print $4}')
    use=$(printf '%s' "$line" | awk '{print $5}')
    say "  $p: $avail free of $size ($use used) [$fs]"
    rec "storage.$p" "$avail/$size/$use"
  else
    say "  $p: present (df unavailable)"
    rec "storage.$p" "present"
  fi
done

if have quota && [ "$JSON" -eq 0 ]; then
  say "  quota (user):"
  quota -s 2>/dev/null | head -10
fi
if have lfs && [ "$JSON" -eq 0 ]; then
  say "  lustre quota:"; lfs quota -h "$HOME" 2>/dev/null | head -5
fi

# --- 8. egress --------------------------------------------------------------
# Both install (git clone) and Claude Code itself (API) need these.

hdr "NETWORK EGRESS"
probe_host() { # probe_host <label> <url>
  if ! have curl; then
    say "  $1: UNKNOWN (no curl)"; rec "egress.$1" unknown; return
  fi
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 -I "$2" 2>/dev/null)
  if [ -n "$code" ] && [ "$code" != "000" ]; then
    say "  $1: reachable (HTTP $code)"; rec "egress.$1" "ok:$code"
  else
    say "  $1: BLOCKED / unreachable"; rec "egress.$1" blocked
  fi
}
probe_host github    https://github.com
probe_host anthropic https://api.anthropic.com
probe_host pypi      https://pypi.org

if [ -n "${HTTPS_PROXY:-}${https_proxy:-}" ]; then
  say "  proxy: configured (value not printed)"; rec proxy set
else
  say "  proxy: none"; rec proxy none
fi

# --- 9. shared-home check ---------------------------------------------------
# Determines whether one install covers all login nodes of this cluster.

hdr "HOME FILESYSTEM"
HOMEFS=$(df -P "$HOME" 2>/dev/null | tail -n 1 | awk '{print $1}')
# df -T is GNU-only; on BSD/macOS fall back to parsing mount(8).
HOMETYPE=$(df -PT "$HOME" 2>/dev/null | tail -n 1 | awk '{print $2}')
if [ -z "${HOMETYPE:-}" ] || [ "$HOMETYPE" = "$HOMEFS" ]; then
  # Portable: mount lines read "<dev> on <mnt> (<type>, opts...)" on BSD and
  # "<dev> on <mnt> type <type> (opts)" on Linux. Avoid gawk-only match(,,arr).
  HOMETYPE=$(mount 2>/dev/null | awk -v d="$HOMEFS" '$1==d {
    for (i=1; i<=NF; i++) if ($i=="type" && i<NF) { print $(i+1); exit }
    n=split($0, a, "("); if (n>1) { split(a[2], b, ","); print b[1] }
    exit }')
  [ -n "${HOMETYPE:-}" ] || HOMETYPE="unknown"
fi
say "  device: ${HOMEFS:-unknown}";        rec homefs "${HOMEFS:-unknown}"
say "  type: ${HOMETYPE:-unknown}";        rec homefstype "${HOMETYPE:-unknown}"
case "${HOMETYPE:-}" in
  nfs*|lustre*|gpfs*|wekafs*|beegfs*|ceph*)
    say "  >>> networked home: one install likely covers all login nodes." ;;
  *)
    say "  >>> local-looking home: may need a per-node install. Verify." ;;
esac

# Symlink support decides install mode (copy vs link).
if ln -s /tmp "$TMPDIR_PROBE/lntest" 2>/dev/null; then
  say "  symlinks: supported";             rec symlink yes
  rm -f "$TMPDIR_PROBE/lntest"
else
  say "  symlinks: NOT supported (copy-mode install required)"; rec symlink no
fi

# --- 10. verdict ------------------------------------------------------------

hdr "DEPLOYMENT VERDICT"
BLOCKERS=""
# `claude` is the thing that must run; it ships its own runtime resolution, so
# a working `claude --version` settles it whether or not `node` is on PATH.
grep -q '^login.claude	absent' "$KV" 2>/dev/null && BLOCKERS="$BLOCKERS no-claude"
have git  || BLOCKERS="$BLOCKERS no-git"
have python3 || BLOCKERS="$BLOCKERS no-python3"
grep -q '^egress.anthropic	blocked' "$KV" 2>/dev/null && BLOCKERS="$BLOCKERS no-api-egress"
grep -q '^egress.github	blocked'    "$KV" 2>/dev/null && BLOCKERS="$BLOCKERS no-github-egress"

if [ -n "$BLOCKERS" ]; then
  say "  BLOCKERS:$BLOCKERS"
  say "  Claude Code cannot run unattended here without addressing these."
else
  say "  No blockers detected — Claude Code and copy-mode skill install viable."
fi
rec blockers "${BLOCKERS:-none}"

if [ "$ORG_SKILLS" -eq 0 ]; then
  say "  Personal skills must be SELF-SUFFICIENT (no Arc org store here)."
else
  say "  Org store present — personal skills may defer site facts to it."
fi

# --- JSON emitter -----------------------------------------------------------

if [ "$JSON" -eq 1 ]; then
  printf '{\n'
  first=1
  while IFS='	' read -r k v; do
    [ -n "$k" ] || continue
    [ $first -eq 1 ] || printf ',\n'
    first=0
    # Escape backslash and double-quote for valid JSON string values.
    ev=$(printf '%s' "$v" | sed 's/\\/\\\\/g; s/"/\\"/g')
    printf '  "%s": "%s"' "$k" "$ev"
  done < "$KV"
  printf '\n}\n'
fi
