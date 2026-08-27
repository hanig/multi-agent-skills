# hanig/multi-agent-skills — Design Plan

Merged from an independent Claude proposal and a Codex proposal (GPT-5.x,
`codex exec`, read-only) briefed on the same context. Points of disagreement are
flagged rather than smoothed over.

---

## 1. The conclusion both proposals reached independently

**Do not port the fleet.** The valuable idea in the reference repo is not
multi-agent orchestration — it is the **artifact contract**: a declared,
independently verifiable statement of what "done" means, written *before*
execution and checked by something other than the thing that did the work.

For this workload, Slurm arrays, Nextflow, and independent experiments already
provide parallelism. Agent fleets help with separable code changes, audits, and
literature synthesis — not long GPU jobs. Keep agents mostly serial until
evidence says otherwise.

The repo's real job: **the one portable home for personal skills**, working
identically on a MacBook and on a login node with no MCP, no browser, no egress,
and no pip install rights.

### Why this matters here specifically

The same fallacy the reference repo attacks — "lifecycle state is not
completion" — appears throughout computational biology:

| Reported as success | Actually |
|---|---|
| `sacct` says `COMPLETED` | Python caught an exception and exited 0 |
| Nextflow exit 0 | a process emitted a zero-row table |
| Training "finished" | hit the wall-clock limit, never converged |
| Figure regenerated | built from a stale intermediate |
| Job reran identically | an input silently changed underneath it |

---

## 2. Contract model (shared substrate)

One small CLI — working name `hskill` — Python **standard library only**.
Every skill uses it; none reimplements contract logic in prose.

```text
.agent-runs/<run-id>/
  contract.json      # written BEFORE execution, immutable
  attempts.jsonl     # append-only: each attempt, incl. requeue/preemption
  events.jsonl       # append-only raw evidence
  outputs.json       # observed outputs + identities
  verification.json  # verifier-derived receipt — never written by the job itself
```

`contract.json` records, before anything runs: intended command and cwd; git
commit plus a hash of any dirty diff; environment/container identity; input
identities; declared outputs; mechanical validators; scientific success
predicates; retry/preemption policy; schema and tool version.

### Verification states — never collapse to a boolean

```
RUNNING · TECHNICALLY_COMPLETE · SCIENTIFIC_PASS · SCIENTIFIC_FAIL
PREEMPTED · FAILED · INCOMPLETE_EVIDENCE · AWAITING_REVIEW
```

`TECHNICALLY_COMPLETE` vs `SCIENTIFIC_PASS` is the distinction that carries the
whole design. The process ended cleanly ≠ the result is admissible.

### Input identity — a ladder, not a hash

Hashing every input is not viable at Tahoe-100M / scBaseCount scale. Declare
which rung was used; weaker rungs are recorded as explicitly weak evidence:

1. Content digest
2. Digest of an immutable dataset manifest
3. Versioned object URI + generation/version ID
4. Size + mtime — **explicitly weak**

*(Codex's correction. The initial Claude draft said "sha256 everything," which
is wrong at this scale.)*

### Retrospective contracts

A contract written after a run may audit it, but is labeled `RETROSPECTIVE` and
never earns the assurance of criteria declared beforehand.

---

## 3. Skills to build, in order

All personal skills are prefixed `hanig-` so they can never collide with an Arc
org skill name.

### 1. `hanig-verified-workflow` — build first

Launch, monitor, resume, and verify Slurm / Nextflow / Snakemake work against
predeclared output contracts.

**Triggers:** run, submit, monitor, resume, retry, or determine completion of a
batch job or pipeline. *Not* for cluster setup, resource advice, or pipeline
code edits that won't be executed.

**Deterministic:** initialize and validate the contract; capture git state,
command, environment, inputs, expected targets; wrap execution to record
timestamps, exit status, signals, attempt identity; record Slurm job/array IDs;
query `sacct`/`squeue`; treat requeue and state3 SPOT preemption as new attempts
under one logical run; parse Nextflow trace; inspect Snakemake targets; run
declared validators; emit the receipt.

**Instructs only:** which site profile, partition, account, storage tier, or
container — those belong to the Arc cluster skills or the project.

**`SCIENTIFIC_PASS` requires all of:** scheduler terminal success; wrapper exit
0; every required Nextflow process `COMPLETED`/`CACHED` or every declared
Snakemake target present; outputs pass declared format, schema, count, and
domain validators; output identities recorded; verifier can connect outputs back
to declared code, inputs, environment, and command.

A green Slurm state alone is insufficient. Files merely existing is insufficient.

### 2. `hanig-verified-training`

Run and evaluate training/fine-tuning with explicit convergence, checkpoint, and
data-provenance criteria. Directly relevant to Mach-1, CodonFM, STATE, Evo 2.

**Triggers:** launching, resuming, evaluating, or selecting a checkpoint from
pretraining, fine-tuning, or sweeps; and any question of whether a model
"converged."

**Deterministic:** record code, config, seed, dataset and split identity,
environment, accelerator count, precision, checkpoint policy; normalize metrics
to append-only JSONL; verify steps monotonic and metrics finite; detect missing
intervals, NaNs, divergence, truncated checkpoints; run a checkpoint-load smoke
test; evaluate prespecified convergence predicates; record *why* a checkpoint was
selected, not just its filename.

No mandatory W&B/TensorBoard dependency — adapters may read them when present,
but the portable contract takes normalized JSONL plus shell validators.

**The key separation:** hitting max allocation is `BUDGET_EXHAUSTED`, **not**
convergence. Convergence requires a predicate declared before the run, e.g.
*val loss improves <0.2% over 5 evals AND no monitored safety metric regresses
beyond threshold AND minimum step count reached*. Picking the numerically best
checkpoint after seeing results is legitimate but must be labeled post hoc.

### 3. `hanig-portable-handoff`

Capture and restore durable research state across machines, clusters, and
sessions. Attacks stated pain point #1 (context switching) head-on.

**Deterministic:** capture repo URL, commit, branch, dirty-diff hash, changed
paths; linked run IDs and Slurm job IDs; output/checkpoint locations *without
copying large data*; current contract states and unresolved failures; verify
referenced files and receipts exist. On resume: compare this machine's code and
input identities against the handoff and **report mismatches instead of silently
continuing**. Redact credential-shaped environment names; never dump the
environment.

**Also absorbs the standing MEMORY.md rule.** CLAUDE.md already mandates a
per-project `MEMORY.md` and warns that a stale one is worse than none — today
that depends on the model remembering. Generate the factual sections from real
state (git log since last update, changed files, open run contracts and their
current verdicts, unresolved TODOs) and let the model write only the judgment
sections: decisions, blockers, recommendations.

*(Claude kept this; Codex's version scoped handoff to explicit pause/resume only
and would have dropped the standing-rule automation.)*

### 4. `hanig-reproducible-result`

Regenerate and verify figures, tables, benchmark results, and exports with
input-to-output provenance.

**Triggers:** regenerate, update, validate, or compare a figure, table,
supplementary artifact, or model export. *Not* visual design or narrative —
those are `figure-composer` and `paper-narrative` in the org store.

**Deterministic:** record generating command, source commit, dirty-diff hash,
environment, inputs; execute the declared build; check outputs exist and parse;
validate dimensions, schemas, row counts, required panels/columns, finite values;
record output digests; optional numeric comparison against a reference with
declared tolerances; optional double-render to detect nondeterminism.

**Three levels, not a boolean:**

1. `GENERATED` — command succeeded, outputs exist
2. `VALIDATED` — passes all declared structural/numeric checks, has provenance
3. `REVIEWED` — a named person explicitly accepted the scientific result

A regenerated figure without review is `VALIDATED`, never `REVIEWED`. The receipt
must not claim semantic correctness because a PDF opens.

### 5. Migrate the four stranded Box skills — cheap, do it early

`grant-writing`, `lab-update`, `literature-digest`, `manuscript-drafting`
currently live in `CLAUDE/.skills/skills/`. Box CloudStorage paths exist on no
server. Move them into the repo unchanged, auditing each for hardcoded Box paths.
Low effort, immediate portability win.

### Deferred: `hanig-bounded-delegation`

Only after the above are in regular use, **and only after a paired benchmark**:
single strong agent vs. coordinator + bounded workers, measuring wall time, human
interventions, defects caught in review, cost, and merge conflicts. Keep the
machinery only if it materially improves wall time without lowering review
quality. This is the experiment the reference repo never ran.

If built: require disjoint declared scopes; give each worker a base commit,
worktree, and artifact contract; verify branch/base/cwd/clean/test predicates
before accepting. No daemon, no message bus, no advisor or committee personas,
no model registry. Git plus process state plus receipts is sufficient.

---

## 4. Installer

**Copy by default, not symlink.** *(Codex's position; the Claude draft argued
symlink and was wrong.)* Symlinking into a live checkout breaks when the checkout
sits under Box, when a branch switch silently mutates every installed skill, when
the checkout is mid-update, and when HPC mount paths differ. `--mode link` exists
for skill development only; production installs on Mac and HPC are immutable
copied snapshots.

`install.sh` — Bash 3.2 and git 2.23 compatible:

1. `umask 077`
2. Accept a local checkout **or a release archive** as source
3. Derive a version from the commit or packaged manifest
4. Refuse a dirty source unless an explicit dev flag is given
5. Validate: declared files and checksums; skill names and frontmatter; no
   unfinished scaffolding; shell syntax; Python syntax; bundled self-tests;
   destination collisions
6. Stage under a private temp dir **on the destination filesystem**
7. Install only listed skills, into `~/.claude/skills/hanig-*`
8. Install the shared CLI into `~/.local/bin`
9. Replace only directories carrying this repo's ownership marker
10. Roll back if any replacement fails
11. Write an install receipt: source revision, tree digest, profile, timestamp,
    installed paths
12. Run an offline `doctor`
13. **Print** required PATH changes; never edit shell rc files

Never delete or overwrite an unknown skill.

Forbidden for portability: `git init -b`, `git switch`, sparse checkout, GNU
`readlink`, GNU `sed`. Bootstrap with `git clone --depth 1`, or `git init` +
`fetch` + `checkout`.

**Mac vs login node** — identical core. Mac may report available MCP/desktop
integrations but no contract may depend on them. Login node assumes no MCP,
browser, or egress; stdlib plus already-present commands only; probes Slurm /
Nextflow / Snakemake as optional capabilities; starts no daemon; no network
unless `--update` is passed explicitly.

**Install once per shared home**, not once per login node.

**Updating** — two paths, one script. Connected: `install.sh --update`
(fast-forward-only fetch, validate, install). Restricted HPC: build a release
archive on the Mac, transfer it, `install.sh --source <archive>`. Transactional
and explicit; never auto-update at startup. `doctor` compares the install receipt
against on-disk digests and reports drift.

---

## 5. Layering with the Arc org store

Ownership is semantic, not loader-precedence-based — precedence may differ across
clients, so correctness must never depend on two same-named skills resolving in a
particular order.

1. **Project-local** owns targets, datasets, figures, tests, scientific thresholds
2. **Arc org skills** own Arc infrastructure, services, model-specific knowledge,
   storage policy, institutional workflow
3. **Personal `hanig-*`** owns cross-environment execution semantics, provenance,
   handoff, and the personal standard for what "complete" means
4. **General model behavior** fills the rest

Rules: prefix everything `hanig-`; never name a personal skill `chimera`,
`state-designer`, `evo2`, `figure-composer`, or any other Arc name; installer
aborts on collision; personal skills may say "use the available Arc cluster skill
to select resources" but must still function when it's absent; don't copy Arc
docs into `references/`; project contracts override personal defaults for
scientific criteria; personal tooling may strengthen verification but must never
silently reinterpret a project's declared threshold.

---

## 6. What not to build

From the reference repo: the fleet daemon; the message bus, cursor protocol,
callback system, inbox compaction; the hand-maintained model ranking; provider/
model/reasoning enforcement tied to today's CLI behavior; advisor, committee, and
orchestration personas; the 254-line sprint process encoded as prose.

Generally: a replacement for Slurm/Nextflow/Snakemake; personal copies of Arc's
cluster, model, literature, manuscript, comms, or design skills; MCP-backed
Slack/Gmail/Notion/Asana skills (they fail on HPC and duplicate the Mac); generic
Python/R/PyTorch/scanpy tutorials; automatic git commits as universal proof of
completion; automatic background updating; a universal environment lockfile
spanning macOS + Apptainer + Docker + conda + every cluster; any claim that
arbitrary scientific correctness can be verified mechanically.

Also avoid a generic "do research" skill — triggers too broadly, eats context,
adds little over base model capability.

---

## 6b. Deployment targets — chimera, lambda, andromeda

Decided 2026-08-25. **state3 (GCP) is out of scope** despite an existing skill;
revisit only if it re-enters weekly use.

| | chimera | lambda | andromeda |
|---|---|---|---|
| Host | `8.19.55.99` (`arc-slurm`) | `192.222.48.103` | `ac-gefion-login` |
| Owner | Arc on-prem | Lambda AI | Gefion / DCAI (Copenhagen) |
| Access | SSH | SSH | **Tailscale SSH** — must be on the Andromeda tailnet |
| Scheduler | Slurm | Slurm | **Slurm via SUNK (Slurm on Kubernetes)** |
| GPUs | H100 | H100 | 256 × H100 80GB HBM3 (32 × DGX H100 × 8) |
| Interconnect | — | — | 8 × ConnectX-7 @ 400 Gbps NDR IB per node |
| Storage | `/home` (0.5 TB soft), `/large_storage` (100 TB), `/scratch` (not backed up), `/common_datasets`, `/processed_datasets`; Weka | `/data`, `/checkpoints`, `/cold-storage` (rclone) | WEKA at `/mnt/weka` (**~14–15 TB total, shared**); Cloudflare R2 at `/mnt/r2-cold-storage-pvc` |
| Home dir | `/home/$USER` | documented | **NOT DOCUMENTED — open question** |
| Node naming | — | — | `ac-gefion-h100-reserved-031-NNN` |
| Existing skill | org store `chimera`; plugin `chimera-hpc` | plugin `lambda-hpc` | **none** |
| Known risk | login node **heavily restricted** — Slurm/file ops/editors/git/conda only, "do NOT run heavy processes" | — | SUNK pod lifecycle; tiny shared FS |

Source for andromeda: `andromedacapacity.notion.site/Arc-256xH100-gef-cph-01`
(read 2026-08-25). Everything below marked *not documented* is genuinely absent
from that page, not merely unread.

### Four consequences for the build

1. **SUNK is the single most important finding.** Andromeda runs Slurm *on
   Kubernetes*, which breaks two assumptions the workflow verifier was built on:
   - **`$HOME` may not persist.** If the login node is a pod, a
     `~/.claude/skills/` install can vanish on restart. Andromeda likely needs
     the install to live on `/mnt/weka/<user>/` with `CLAUDE_CONFIG_DIR` pointed
     at it — a **different install target from the other two clusters**.
   - **`sacct` may not exist.** SUNK deployments frequently run without
     `slurmdbd`, meaning no job accounting history. `hanig-verified-workflow`
     was designed to query `sacct` for terminal state. It must therefore treat
     scheduler history as *optional evidence* and fall back to wrapper exit
     status plus artifact predicates. This is a day-one design constraint, not
     a later port. (`probe.sh` now detects k8s and tests `sacct` directly.)
2. **Andromeda's shared filesystem is small relative to its compute.** ~15 TB
   RWX serving 256 H100s. Checkpoints from a large training run will fill it
   fast — which is exactly why the R2 cold tier exists. `hanig-verified-training`
   should treat "checkpoint retained" and "checkpoint on the hot tier" as
   different facts, and record which tier a selected checkpoint lives on.
3. **Chimera's login-node policy is a real deployment risk, not a footnote.**
   A Node-based agent process plausibly reads as a "heavy process." Options, in
   preference order: (a) confirm with Arc HPC that an interactive `claude`
   session on the login node is acceptable; (b) run the agent inside an
   `sh_dev` allocation instead of on the login node; (c) run the agent on the
   Mac and let it drive chimera over SSH, keeping only the contract files
   cluster-side. Option (c) needs no permission and may be the right default
   for chimera specifically.
4. **Three separate homes, three installs, and now three different install
   targets.** No shared filesystem spans them; andromeda may not have a durable
   home at all. The release-archive path (`install.sh --source <archive>`) is
   the primary mechanism, not the fallback — and `install.sh` needs an explicit
   `--prefix` so andromeda can install to `/mnt/weka` instead of `$HOME`.

Andromeda also documents required NCCL settings for multi-node IB jobs
(`NCCL_IB_HCA=mlx5_0,mlx5_3,mlx5_4,mlx5_5,mlx5_6,mlx5_9,mlx5_10,mlx5_11`,
`NCCL_SOCKET_IFNAME=eth0`). These belong in a job contract's recorded
environment, since a run that silently lost IB and fell back to TCP is a
performance failure the receipt should be able to explain.

## 6c. Probe results — measured 2026-08-25

`probe.sh` run on all three. Full output in `probes/{chimera-login,lambda,
andromeda}.{txt,json}`. **No blockers on any host.**

| | chimera | lambda | andromeda |
|---|---|---|---|
| host | `chimera-login` | `vci-steady-state-login-001` | `ac-gefion-login-0` |
| user | `hani` | `hani` | **`hgoodarzi`** |
| OS | Ubuntu 22.04.5 | Ubuntu 24.04.2 | Ubuntu 22.04.5 |
| `$HOME` | `/home/hani` (nfs4) | `/home/hani` (nfs) | `/mnt/weka/home/hgoodarzi` (**wekafs**) |
| python3 | 3.10.12 | 3.12.3 | 3.10.12 |
| git | 2.34.1 | 2.43.0 | 2.34.1 |
| claude | **2.1.241** | **2.1.227** | **2.1.227** |
| org skills | **0** | **0** | **0** |
| personal skills | 3 | 0 | 0 |
| Slurm | 25.11.0 | 25.05.0 | 24.11.5 |
| `sacct` | **works** | **works** | **works** |
| accounts | `ctc,goodarzilab` | `hani` | `root` |
| k8s | no | no | `kubectl` present |
| symlinks | yes | yes | yes |
| egress | github/anthropic/pypi OK | same | same |

### Findings that change the plan

1. **Claude Code is already installed on all three** (2.1.227–2.1.241), along
   with node 24–26 — in conda/micromamba prefixes loaded by `.bashrc`. A
   non-interactive `ssh host 'command -v node'` reports ABSENT, which is a
   *probe artifact, not a fact*. `probe.sh` now re-checks through `bash -lic`
   and judges on whether `claude --version` works, since claude resolves its own
   runtime. **Nothing needs installing to run agents on these clusters.**
2. **Open question 1 is settled: org skills reach none of the three.**
   `~/.claude-science` is absent everywhere; all three report 0 org skills. The
   `hanig-*` skills must be fully self-sufficient on-cluster, and there is no
   collision risk with org names there — only on the Mac.
3. **`sacct` works on all three, including andromeda.** The SUNK concern about a
   missing `slurmdbd` did not materialize. The verifier may use scheduler
   history as primary evidence — but keep the artifact-predicate fallback,
   since it costs nothing and covers the exit-0-with-no-output case that
   `sacct` cannot see.
4. **Andromeda's `$HOME` is on WEKA** (`/mnt/weka/home/hgoodarzi`, `wekafs`), so
   it persists across pod restarts. The predicted need for a separate
   `--prefix` install target does **not** apply. One install path works on all
   three. `--prefix` is still worth having, but is no longer on the critical path.
5. **Usernames differ** — `hani` on chimera and lambda, **`hgoodarzi`** on
   andromeda. Nothing may hardcode `$USER` or assume a consistent home path.
6. **git ≥ 2.34 on all clusters.** The git 2.23 constraint is **Mac-only**.
   Release archives are built on the Mac, so the constraint still binds there,
   but cluster-side scripts need no 2.23 workarounds.
7. **Chimera already has 3 personal skills**: `arc-reactor-setup`, `arc-scrna`,
   and `arc-scrna.bak.2026-05-16`. That `.bak` directory is sitting in the
   skills folder and will be parsed as a skill — worth cleaning up. Check for
   name collisions before installing there.
8. **No container runtime on any login node** (apptainer/singularity/docker all
   absent), and **no nextflow or snakemake anywhere**. Andromeda has
   `/usr/bin/enroot` and `srun` shows extensive container flags — so containers
   are a *compute-node* concern, via enroot/pyxis on andromeda. If real
   workloads use Nextflow or Snakemake, they run from a conda env, not a system
   install; `hanig-verified-workflow` must locate them per-project, not assume
   a global binary.

### Storage pressure — worth acting on independently of this project

| mount | host | free / total | used |
|---|---|---|---|
| `/mnt/weka` | andromeda | 3.0 T / 80 T | **97%** |
| `/data` | lambda | 13 T / 160 T | 93% |
| `/checkpoints` | lambda | 7.5 T / 69 T | 90% |
| `/processed_datasets` | chimera | 27 T / 273 T | 91% |
| `/scratch` | chimera | 24 T / 164 T | 86% |
| `/cold-storage` | lambda | 1.0 P / 1.0 P | **0%** |
| `/mnt/r2-cold-storage-pvc` | andromeda | 1.0 P / 1.0 P | **0%** |

Two things stand out. **Andromeda's WEKA is 97% full with 3 TB free serving 256
H100s** — a large run can plausibly fail on write, and the Notion page's stated
capacity (14 TiB / 15 TB) is wrong in both directions: the tier is 80 T. And
**both petabyte cold tiers are completely unused** while the hot tiers sit at
90–97%. That is a migration that wants doing regardless of this repo.

This directly motivates a `hanig-verified-training` requirement: record **which
storage tier** a selected checkpoint lives on, and treat "checkpoint retained"
and "checkpoint on the hot tier" as different facts.

### `probe.sh` — settle this empirically

`probe.sh` in this folder is a dependency-free, read-only POSIX-sh probe that
answers open questions 1, 3, and 5 in a single pass per host. It reports:
identity and OS; toolchain with the specific portability gates (`git init -b`,
node ≥ 18, GNU vs BSD `sed`/`readlink`); whether `claude` is installed and
**whether the Arc org skill store is present**; scheduler flavor, partitions,
accounts, and whether `sacct` is actually queryable; workflow engines;
container runtime; real storage capacity per candidate mount; network egress to
github / api.anthropic.com / pypi; home filesystem type, whether it is networked
(one install per cluster vs per node), and whether symlinks work at all.

It ends with a deployment verdict naming any hard blockers.

```bash
for h in chimera lambda andromeda; do
  scp probe.sh $h:~/ && ssh $h 'sh ~/probe.sh' > probe-$h.txt
done
```

Add `--json` for a machine-readable form suitable for diffing the three.
Verified working on macOS (BSD userland, git 2.23); written to POSIX sh with no
gawk-only constructs so it survives an old Linux login node.

## 7. Open questions — resolve before writing code

1. **Do Arc org skills reach HPC login nodes?** They're served from
   `~/.claude-science/orgs/<uuid>/skills/` on the Mac. If they reach login nodes,
   no personal cluster-reference skill is needed and `hanig-*` stays purely about
   execution semantics. If they don't, a thin cross-cluster router is needed.
   **Unverified — highest-value thing to check first.** One `claude` session on
   chimera answers it.
2. ~~Which clusters are in weekly use?~~ **ANSWERED 2026-08-25: chimera, lambda,
   andromeda.** state3 and the UCSF hosts are out of scope. See §6b.
3. **Is andromeda Slurm?** Chimera and lambda both are; andromeda/gefion is
   unconfirmed. If not, `hanig-verified-workflow` needs a scheduler adapter seam
   from the start. `probe.sh` answers this.
4. **Public or private repo?** Cluster hostnames, IPs, storage layouts, partition
   names, and account codes will end up in contracts and references. If public,
   that content needs a private overlay or must be scrubbed at the boundary.
5. **Where does the local checkout live?** Not under Box — that's the failure
   mode the copy-based installer exists to avoid.

---

## 8. Where the two proposals disagreed

| Question | Claude | Codex | Resolution |
|---|---|---|---|
| Install mode | symlink | copy snapshots | **Codex** — Box/branch-switch/mount hazards are real |
| Input identity | hash everything | 4-rung ladder | **Codex** — hashing 500M cells is not viable |
| Skill count | 3 + migrations | 4 + deferred delegation | **Codex** — training deserves its own contract |
| Completion states | 5 exit codes | 8 named states | **Codex** — `TECHNICALLY_COMPLETE` ≠ `SCIENTIFIC_PASS` |
| Cluster-context skill | build one | org store owns it | **Open** — gated on question 1 |
| MEMORY.md automation | build it | not proposed | **Claude** — standing rule + pain point #1 |
| Box skill migration | migrate | not proposed | **Claude** — Codex wasn't told they existed |
| Fleet machinery | don't port | don't port | **Agreed** |
