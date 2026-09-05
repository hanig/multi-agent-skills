# The three clusters: rules, partitions, and what breaks

Arc runs three Slurm clusters that this tooling targets: **chimera**,
**lambda** and **andromeda**. A project is bound to exactly one of them, and
plans are NOT portable between them: most partition names exist on only one
cluster, and the two that do repeat mean different things (see §5).

Read the provenance before trusting a cell. In every partition table, the
**name, time limit and node count** are measured by `bin/probe.sh` on
2026-08-25, raw output in [docs/probes/](probes/). The **"Use it for"** column
is guidance, not measurement: it comes from the Arc org plugins and from use,
and the probe says nothing about it. Anything else attributed to "Arc plugin"
comes from the `chimera-hpc` / `lambda-hpc` org plugins, which describe policy
intent and are in places out of date with the live scheduler. Where the two
disagree, **`sinfo` wins** and the disagreement is recorded below rather than
silently resolved. §7 lists what is unverified by anything.

`state3` (the GCP Slurm cluster in `arc-state3`) is a fourth cluster with its
own org plugin. It is not a swarm target and is not covered here.

---

## 1. At a glance

| | chimera | lambda | andromeda |
|---|---|---|---|
| ssh host | `chimera-login` | `vci-steady-state-login-001` | `ac-gefion-login-0` |
| login IP | `8.19.55.99` (VPN) | `192.222.48.103` (no VPN) | via Tailscale |
| user | `hani` | `hani` | `hgoodarzi` |
| `$HOME` | `/home/hani` (nfs4) | `/home/hani` (nfs) | `/mnt/weka/home/hgoodarzi` (wekafs) |
| OS | Ubuntu 22.04.5 | Ubuntu 24.04.2 | Ubuntu 22.04.5 |
| python3 | 3.10.12 | 3.12.3 | 3.10.12 |
| git | 2.34.1 | 2.43.0 | 2.34.1 |
| Slurm | 25.11.0 | 25.05.0 | 24.11.5 (SUNK, on Kubernetes) |
| accounts (`sacctmgr`) | `ctc`, `goodarzilab` | `hani` | `root` (suspect; see §7) |
| default partition | `cpu` | `standard` | `all` |
| `--mem` required | no | **yes** | no |
| default memory | `DefMemPerCPU=4096` | `DefMemPerNode=UNLIMITED` | `DefMemPerCPU=4096` |
| GPUs (not probed) | 4x H100 80GB per node [plugin] | 8x H100 80GB per node [plugin] | H100, count unknown [inferred] |
| org skill store | absent | absent | absent |

---

## 2. chimera

Login is `chimera-login`, **not** `chimera`. The `chimera` ssh entry carries
`RemoteCommand sh_dev` and rejects a passed command, so every scripted use
takes `chimera-login`. Requires the Arc VPN.

### Partitions

Names, time limits and node counts measured 2026-08-25. The last column is
guidance, not measurement.

| Partition | Timelimit | Nodes | Use it for |
|---|---|---|---|
| `cpu` (default) | 5 days | 10 | Interactive CPU work only. **QOS `cpu_interact` caps you at 2 running jobs.** |
| `cpu_batch` | 14 days | 10 | **Any CPU fan-out.** 20 concurrent, 200 submitted. |
| `cpu_high_mem` | 14 days | 9 | Single high-memory CPU job (~1.15 TB/node). |
| `cpu_batch_high_mem` | 14 days | 9 | High-memory CPU fan-out. |
| `cpu_preemptible` | 14 days | 29 | Free CPU scavenging; checkpoint or lose it. |
| `gpu` | 1 day | 10 | Short GPU work, 1 node, up to 4 GPUs. |
| `gpu_batch` | 14 days | 10 | Long or multi-node GPU jobs. |
| `gpu_high_mem` | 14 days | 5 | GPU job needing ~640 GB RAM, 1 node. |
| `gpu_batch_high_mem` | 14 days | 5 | Same, multi-node or longer queue. |
| `gpu_cpu` | 3 days | 2 | Only for labs holding whole-node reservations. |
| `preemptible` | 14 days | 25 | Free GPU scavenging. 10-second kill grace. |
| `quick_preemptible` | 2 hours | 25 | Short scavenger bursts. Max 4 running / 8 submitted. |
| `goodarzilab_gpu_priority` | 14 days | 4 | **Ours.** Paid 24x7 by the lab. |
| `ctc_gpu_priority` | 14 days | 6 | Computational Tech Center. |
| `ctc_cpu_priority` | 14 days | 8 | Computational Tech Center. |
| `ctc_agent_priority` | infinite | 2 | Computational Tech Center. |
| `vci_gpu_priority` | 14 days | 2 | Other team. |
| `evo_gpu_priority` | 14 days | 2 | Other team. |
| `cell_reason_gpu_priority` | 14 days | 2 | Other team. |
| `hsu_gpu_priority` | 14 days | 4 | Other team. |

The `gpu_20gb` and `gpu_40gb` MIG partitions no longer exist; a Slurm upgrade
broke them. Any plan naming them is stale.

### Rules

**Never fan out on `cpu`.** Partition `cpu` maps to QOS `cpu_interact` with
`MaxJobsPU=2`, a cap on *running* jobs. A DAG that submits 8 CPU units to `cpu`
gets 2 running and 6 pending, and the 6 drain two at a time as the earlier ones
finish. Nothing is lost, but the fan-out has silently become a serial queue of
depth 2, so wall-clock is 4x what the plan assumed and the pending jobs look
like a scheduler stall. Fan-out belongs on `cpu_batch` (20 concurrent, 200
submitted).

**Priority partitions are paid for around the clock**, whether or not anything
is running. Their nodes are removed from the general `gpu`/`cpu` pools and
added to `preemptible`, which is why `preemptible` (25 nodes) is wider than
`gpu` (10). Using `goodarzilab_gpu_priority` costs nothing extra; leaving it
idle costs the same as using it.

**Preemption grace on chimera is 10 seconds.** An sbatch job is requeued where
possible; an interactive `sh_gpu` session is not, and simply dies. Interactive
work on `preemptible` is a bad trade.

**A GPU request implies 8 CPU cores and 80 GB of host RAM per GPU** (Arc
plugin, not re-measured) unless overridden with `--cpus-per-gpu` /
`--mem-per-gpu`. That 80 GB is system memory and has nothing to do with the
H100's 80GB of VRAM; the two numbers coinciding is a coincidence, and a job
needing more host RAM than that must ask for it or land on `gpu_high_mem`.

**Never set `CUDA_VISIBLE_DEVICES`.** Slurm sets it from `--gres`/`--gpus`, and
an explicit assignment overrides it wrongly.

**A `GPU`-prefixed hostname means nothing.** `GPU724A` is a naming convention,
not a statement that your job holds a GPU. Confirm with `nvidia-smi` or the
`gres/gpu` entry in `scontrol show job <id>`.

### Storage

Free and total measured 2026-08-25; the Note column is plugin-sourced.

| Path | Free / total | Note |
|---|---|---|
| `/home/hani` | 753G of 932G | Arc quota: 0.5 TB soft, 1 TB hard |
| `/large_storage` | 532T of 2.3P | Group space; 100 TB soft, 120 TB hard |
| `/scratch` | 24T of 164T | 86% used |
| `/common_datasets` | 13T of 55T | |
| `/processed_datasets` | 27T of 273T | 91% used |

Weka tiers a file to object store after two months without access. First read
after that is slow, not absent.

Billing (Arc plugin, unverified against an invoice): H100 at $1.23/GPU-hour
standard, $1.29 high-mem, **charged on allocation and not on utilization**. An
idle GPU held by a hung job bills identically to a saturated one. Charge with
`-A ctc` or `-A goodarzilab`.

Support goes to the DST HPC Service Desk, not Slack.

---

## 3. lambda

No VPN needed. Same credentials and SSH keys as chimera.

### Partitions

Names, time limits and node counts measured 2026-08-25. The last column is
guidance, not measurement.

| Partition | Timelimit | Nodes | Use it for |
|---|---|---|---|
| `standard` (default) | 7 days | 3 | General work up to a week. Not preemptible. |
| `pretrain` | 14 days | 6 | Pre-training runs. |
| `posttrain` | 14 days | 8 | Post-training and fine-tuning. |
| `model_dev` | 14 days | 2 | Model development. |
| `data_dev` | 14 days | 1 | Data work. |
| `labinloop` | 14 days | 2 | Lab-in-the-loop workloads. |
| `cpu_interactive` | infinite | 3 | CPU-only interactive sessions. |
| `interactive` | 1 day | 3 | **Was DRAIN at probe time.** Check before planning onto it. |
| `preemptible` | 14 days | 22 | Scavenging across the whole cluster. |
| `preemptible_low` | 14 days | 22 | Lowest priority; preempted even by `preemptible`. |

Both preemptible partitions hold 22 nodes, which the Arc plugin says is the
whole cluster. The probe measures the count, not that it is the total, so treat
"cluster-wide" as plugin-sourced.

### This disagrees with the Arc `lambda-hpc` plugin, and the plugin is stale

The plugin documents four partitions: `standard` (6 nodes, `017-022`),
`large_batch` (16 nodes, `001-016`), `preemptible`, `preemptible_low`. The live
scheduler has **no `large_batch` at all**, `standard` at **3 nodes**, and six
role partitions the plugin never mentions. The cluster was re-partitioned by
workload role after that plugin was written.

Consequence: a plan copied out of the plugin naming `large_batch` is refused on
lambda. Read `sinfo`, not the plugin. (The plugin already warns that the Notion
wiki is behind the plugin; the plugin is now behind the cluster.)

### Rules

**`--mem` is required on lambda and nowhere else.** `DefMemPerNode=UNLIMITED`
means a job with no `--mem` gets no memory allocation and fails with Slurm's
least helpful message, `Requested node configuration is not available`. Worse,
`sbatch --test-only` accepts the identical flags, so the dry run passes and the
real submission does not. Every lambda unit carries `--mem`.

**Preemption grace is 60 seconds** with `PreemptMode=REQUEUE` on every
partition, so a `SIGTERM` handler that checkpoints to `/data` genuinely works
here, unlike chimera's 10 seconds. Pass `--requeue` explicitly.

**`/tmp` is not writable.** Stage to `$HOME`.

**Per-GPU defaults** (Arc plugin, not re-measured): `DefCpuPerGPU=6`,
`DefMemPerGPU=151183`. That is a Slurm configuration value in MiB, learned
from the plugin rather than measured here, and it is quoted exactly rather than
converted: 147.6 GiB is 151,142 MiB, so a rounded `--mem-per-gpu=147.6G` is about
41 MiB short of it. Do not submit either number on this document's authority:
read the live value with `scontrol show config` on the login node, because a
plugin figure that is already wrong about lambda's partitions can be wrong
about its memory defaults too. This sits oddly beside
`DefMemPerNode=UNLIMITED`, and the interaction has not been verified. Declare
`--mem` and stop depending on the answer.

### Storage

Free and total measured 2026-08-25; the Note column is plugin-sourced.

| Path | Free / total | Note |
|---|---|---|
| `/home/hani` | 6.9T of 19T | |
| `/data` | 13T of 160T | **93% used** |
| `/checkpoints` | 7.5T of 69T | 90% used, and **periodically wiped** |
| `/cold-storage` | 1.0P of 1.0P | Cloudflare R2, `rclone` pre-configured |

Compute nodes also carry a local `/scratch` SSD, according to the Arc plugin.
The probe ran on the login node and never saw it, so it is absent from the
table above and its capacity here is unknown.

`/checkpoints` being wiped on a schedule makes it wrong for anything a job
needs on resume. Checkpoint to `/data`.

Support goes to Lambda Labs, not Arc DST.

---

## 4. andromeda

Reached over Tailscale; bring the tunnel up first. Runs **SUNK**, Slurm on
Kubernetes, which changes two assumptions worth stating: `$HOME` persistence
across a restart is not guaranteed the way it is on a bare-metal login node,
and node identity is less stable. Install to the shared filesystem.

### Partitions

Names, time limits and node counts measured 2026-08-25. The last column is
guidance, not measurement.

| Partition | Timelimit | Nodes | Use it for |
|---|---|---|---|
| `all` (default) | infinite | 32 | Everything not covered below. |
| `h100-reserved` | infinite | 29 | Reserved H100 capacity. Where GPU training goes. |
| `large_batch` | infinite | 32 | Wide batch fan-out. |
| `preemptible` | infinite | 32 | Scavenging. |
| `standard` | infinite | 3 | Small pool. |

**Every partition reports `infinite` as its time limit.** Nothing stops a
runaway job on the scheduler side, so walltime discipline lives entirely in the
plan. Declare `--time` on every andromeda unit.

Note the name collisions: `standard` exists here (3 nodes) and on lambda (3
nodes), and `preemptible` exists on all three clusters. Same name, different
node pool, different meaning. `large_batch` exists here and NOT on lambda,
whatever the Arc lambda plugin says. A matching partition name is not evidence
that a plan is portable.

### Storage

Free and total measured 2026-08-25; the Note column is plugin-sourced.

| Path | Free / total | Note |
|---|---|---|
| `/mnt/weka/home/hgoodarzi` | 2.9T of 80T | **97% used** |
| `/mnt/weka` | 2.9T of 80T | Same filesystem |
| `/mnt/r2-cold-storage-pvc` | 1.0P of 1.0P | R2 cold storage |

The Weka home at 97% is the live operational risk on this cluster. Check free
space before dispatching anything that writes checkpoints, and keep
`budget.gpu_hours` low until it is cleared.

---

## 5. Rules that hold across all three

**A plan belongs to one cluster.** Of the 35 partitions across the three, only
two names repeat: `preemptible` (all three, different node pools) and
`standard` (lambda and andromeda). Every other name exists on exactly one
cluster, so a unit's `sbatch` list does not travel, and the two that do repeat
travel by luck rather than by meaning. The coordinator enforces
this: `validate` and `run` query `sinfo`, refuse any plan naming a partition
this cluster lacks, and name what it does offer. Verified on chimera against a
plan carrying lambda's `labinloop`, refused with zero attempt directories
created. If the partition list comes back UNKNOWN (no `sinfo`, or a scheduler
that did not answer), nothing is refused, deliberately: a validator that blocks
honest work on its first flaky day is as bad as one that passes a broken plan.

**The Python floor is 3.10**, set by chimera and andromeda at 3.10.12. Testing
only against lambda's 3.12.3 will ship syntax those two reject.

**`node`, `npm` and `claude` exist only inside a login shell.** They live
behind conda/micromamba prefixes loaded by `.bashrc`, so
`ssh host 'command -v node'` reports ABSENT and is wrong. Probe with
`bash -lic`.

**No org skill store reaches any cluster.** `~/.claude-science` is absent on
all three. Skills installed there must be self-sufficient.

**No workflow engine or container runtime is installed on any login node.**
`nextflow`, `snakemake`, `apptainer`, `singularity` and `docker` all measured
ABSENT on all three. A `pipeline` unit brings its own or does not run.

**Usernames and home paths differ.** `hani` on chimera and lambda, `hgoodarzi`
on andromeda, and andromeda's home is under `/mnt/weka`. Never hardcode either.

**Git is 2.34+ on the clusters and 2.23.0 on the Mac.** Cluster versions were
probed 2026-08-25 (2.34.1, 2.43.0, 2.34.1); the Mac was measured 2026-09-03.
In anything that runs both places, avoid `git init -b`, which 2.23 does not
have at all, `git switch`, which it carries only as an experiment, and GNU-only
`sed` and `readlink` flags.

**`sacct` defaults to today** and needs `-S` to see anything older. A
`CANCELLED` job can carry exit code `0:0`, so the exit code alone does not
establish success. Slurm reuses job ids, so a job id alone does not identify a
run.

**Slurm's numbers are binary, and a bare number is MiB.** `--mem=700G` is
716,800 MiB, `--mem-per-gpu=148000` allocates 148,000 MiB, and `MaxMemPerCPU`
and `DefMemPerGPU` are both MiB. Read the default off the cluster with
`scontrol show config` and pass that value back exactly rather than a rounded
one. The lambda figure quoted in §3, 151,183 MiB, shows what rounding costs
(`148000` is 3,183 MiB less, about 3.1 GiB), but it came from the Arc plugin
and has not been re-measured, so treat it as an illustration and not as a
number to submit unchecked.

**`DenyAccounts` is printed INSTEAD of `AllowAccounts`, not beside it.** A
partition that denies your account looks wide open if you only read the
allowance, because the allowance field is simply absent. Read both. An unknown
allowance is not an unrestricted one.

**`MaxMemPerCPU` turns a memory request into a CPU count, and then Slurm
refuses the job by naming CPUs.** Slurm's units are binary, so `--mem=700G` is
716,800 MiB; at `MaxMemPerCPU=5120` that costs 716800 / 5120 = 140 CPUs, not
the 32 in your `--cpus-per-task`. Read the suffix carefully, because the same
number in decimal units gives a different answer (ceil(700,000 / 5120) = 137) and
only one of them is what Slurm charged you. The refusal points at the CPU count
and says nothing about memory, so the error names everything except the cause.
Size the job against the CPU count the cluster will charge.

**A QOS `GrpTRES` cap is invisible to `sinfo`.** Hours went into a
`QOSGrpCpuLimit` beside a 736-CPU partition sitting with 202 CPUs idle, because
the limit lives on the QOS and nothing in `sinfo` can see it. There are two
independent routes to the same error: the partition's QOS, and the association
QOS attached to your account. Resolving the first does not rule out the second.

**`sbatch --test-only` start-time estimates are pessimistic and useless.** It
predicted 22:08 for a job that started and finished within one second. Do not
use it to decide whether to wait. On lambda it also accepts flags that the real
submission rejects (see §3).

---

## 6. Choosing a partition

| What you are running | chimera | lambda | andromeda |
|---|---|---|---|
| Interactive shell, CPU | `cpu` (2-job cap) | `cpu_interactive` | `all` |
| Interactive shell, GPU | `gpu` | `standard` | `all` |
| CPU fan-out, many units | `cpu_batch` | `standard` | `large_batch` |
| High-memory CPU | `cpu_high_mem` | `standard` with `--mem` | `all` with `--mem` |
| GPU job under a day | `gpu` | `standard` | `h100-reserved` |
| GPU training, multi-day | `gpu_batch` | `pretrain` / `posttrain` | `h100-reserved` |
| Lab-priority GPU | `goodarzilab_gpu_priority` | n/a | `h100-reserved` |
| Checkpointable scavenging | `preemptible` | `preemptible` | `preemptible` |
| Throwaway burst | `quick_preemptible` | `preemptible_low` | `preemptible` |

Confirm before trusting the row, because partition sets change (lambda proves
it):

```bash
sinfo -o "%20P %10l %6D %10T"
scontrol show partition <name>
```

---

## 7. Known unknowns

These are not documented anywhere I can verify, and every one of them can
refuse a job at submission time with a message that does not name the cause.

The tooling to answer them landed on 2026-09-03 as B4 and B9 (ARC-231,
ARC-232): `survey.py` now reports `allow_accounts`, `deny_accounts`, `qos`,
`qos_grptres` and `max_mem_per_cpu_mb` per partition, parsed from
`scontrol -o show partition` and `sacctmgr -n -P show qos`. **The tool exists;
the answers for these three clusters have not been captured.** Running it on
each login node and folding the output into this file is the next step.

- **`AllowAccounts` per partition.** Whether `goodarzilab_gpu_priority`
  requires `-A goodarzilab`, and whether the other teams' priority partitions
  are merely conventionally off-limits or actually enforced.
- **`GrpTRES` and `MaxMemPerCPU` per partition**, on all three.
- **andromeda's real account.** The probe reported `root`, which is almost
  certainly an artifact of how SUNK exposes `sacctmgr` rather than an account
  to pass to `-A`. Passing it blindly is not safe.
- **lambda's role partitions.** Whether `pretrain`, `posttrain`, `model_dev`,
  `data_dev` and `labinloop` carry account or QOS restrictions, and who owns
  them.
- **lambda's `interactive` partition**, DRAIN at probe time. Transient or
  retired is unknown.

One command per cluster answers most of it, and reports the fields by the
names the rest of the tooling uses:

```bash
python3 skills/hanig-project/scripts/survey.py --json
```

---

## Provenance

| Claim class | Source | Date |
|---|---|---|
| Host identity; toolchain versions; skill stores; scheduler partitions and accounts; workflow engines; container runtimes; storage; home filesystem | `bin/probe.sh`, output in [docs/probes/](probes/) | 2026-08-25 |
| The `--mem` requirement, `DefMemPerNode`, `DefMemPerCPU`, `SelectTypeParameters`, `--test-only` behaviour | Live `sbatch`/`scontrol` on all three. **`DefMemPerGPU` was NOT among them**; see the row below | 2026-08-28 |
| chimera QOS caps, billing rates, Weka tiering, quotas | Arc `chimera-hpc` org plugin | not re-verified |
| lambda per-GPU defaults (`DefCpuPerGPU`, `DefMemPerGPU=151183`), R2 cold storage, `/scratch`, support routing | Arc `lambda-hpc` org plugin | contradicted on partitions; see §3. Read `DefMemPerGPU` live before relying on it |
| `DenyAccounts` printed instead of `AllowAccounts`; `MaxMemPerCPU` arithmetic; QOS `GrpTRES` invisibility | Paid for in ARC-231 / ARC-232; see commits `77e0e33`, `818d00a` | 2026-09-03 |
| Everything in §7 | Nothing. Unverified for these three clusters. | |

Partition sets are mutable. Re-run `bin/probe.sh` before trusting any table
here that is more than a few weeks old, and update this file from its output
rather than from memory.
