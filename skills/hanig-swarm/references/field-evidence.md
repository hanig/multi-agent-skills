# Field evidence and operational asides

This is measured context behind the decision rules in `../SKILL.md`; it is not
a second source of behavior.

## Moving a queued job

To move a job that is already queued, update it in place:

```bash
scontrol update JobId=187196 Partition=cpu
```

The job ID and attempt binding survive. Cancel-and-redispatch creates an
unbound job ID and changes `sbatch`, hence the plan digest, so the next advance
requires `--accept-plan-change`. `survey.py` reports `allow_accounts`, <!-- declaration: scheduler.queued-job -->
`qos_grptres`, and `max_mem_per_cpu_mb` to prevent the mismatch at planning
time.

## Same-node exclusion trials

Eight concurrent advances across three trials on each of lambda, chimera, and
andromeda produced one dispatcher every time. Every trial was same-node; that
is exactly the topology a local `flock` already guarantees and does not certify <!-- declaration: unattended.lock -->
cross-node recovery.

Measured 2026-09-03:

| host | `$HOME` | filesystem |
|---|---|---|
| lambda | `/home/hani` | nfs |
| chimera | `/home/hani` | nfs4 |
| andromeda | `/mnt/weka/home/hgoodarzi` | wekafs |

`XDG_STATE_HOME` was unset on all three, so default state landed under
`~/.local/state`; lambda and chimera therefore used NFS. Andromeda `/tmp` was
overlayfs and node-local/ephemeral; lambda and chimera `/tmp` were ext2/ext3.

## Orphan reconciliation trial

After a live job's ID was erased from coordinator state, `reconcile_orphan`
found scheduler job 187880 by its `swarm-{attempt}` name and did not resubmit.

## Shared Git stash field failure

Three agents in three linked worktrees each ran `git stash -u` and `pop` in one
window. Each pop consumed another agent's entry because the repository has one
shared stash ref. Dangling commits recovered the content. The completion
protocol now bans stash and names non-shared substitutes.

## First real DAG

On lambda, 2026-08-28, a real three-unit `A -> {B, C}` DAG submitted A alone,
held B and C, released both in one advance after A reached DONE, and finished
with all outputs in exclusive attempt roots. `status` exited 0.

## Per-cluster memory and Python measurements

| | lambda | andromeda | chimera |
|---|---|---|---|
| default memory | `DefMemPerNode = UNLIMITED` | `DefMemPerCPU = 4096` | `DefMemPerCPU = 4096` |
| `--mem` required | **yes** | no | no | <!-- declaration: cluster.plan-specific -->
| SelectTypeParameters | `CR_CORE_MEMORY,CR_ONE_TASK_PER_CORE` | `CR_CPU_MEMORY,CR_PACK_NODES` | `CR_CORE_MEMORY` |
| python3 | 3.12.3 | 3.10.12 | 3.10.12 |

Lambda without `--mem` fails with “Requested node configuration is not
available” even though `sbatch --test-only` accepts the flags. Python 3.10.12 <!-- declaration: cluster.plan-specific, python.host-floor -->
on andromeda and chimera sets the host floor.

Partition names do not port across the three clusters: <!-- declaration: cluster.plan-specific -->

- lambda: `labinloop model_dev preemptible`
- andromeda: `all h100-reserved preemptible standard`
- chimera: `gpu gpu_batch cpu gpu_high_mem`

Other paid-for observations: use `chimera-login` because `chimera` has a
`RemoteCommand`; `--test-only` start estimates were pessimistic enough to <!-- declaration: cluster.access -->
predict 22:08 for a job that began and ended within one second; a lifted Slurm
module needs callees, imports, and constants, with <!-- declaration: drift.lifted-module -->
`tests/test_swarm.py::TestTheLiftIsClosed` guarding the last category.

## Credential measurement

A live probe observed a key reaching an agent while absent from the short-lived
Paseo client's environment. This is why coordinator child filtering is not
described as worker credential isolation. Exact-name and value-shape matching
also broke legitimate runtime configuration; `HF_TOKEN` under a second name is
the standing example.
