# Unit contract details

The generated declarations in `../SKILL.md` are authoritative. This file keeps
the longer failure explanations and examples. <!-- declaration: placement.reference-elaboration -->

## Kind boundaries

For `slurm`, the command is the work. Nesting `sbatch --wrap` submits an inner
job with no attempt binding while the outer job can exit successfully before
the real work runs. The command must never invoke `sbatch` or `srun`; scheduler <!-- declaration: slurm.command-boundary -->
flags belong in the unit's `sbatch` list. <!-- declaration: slurm.command-boundary -->

For code, `prompt` becomes the last positional argument to the agent runner.
A coordinator dispatches that runner through `paseo run`. <!-- declaration: code.prompt-boundary -->
A provider flag placed there is a sentence for the agent to read, not runner
configuration. Provider, mode, model, thinking, and environment must remain <!-- declaration: code.prompt-boundary -->
unit fields. <!-- declaration: code.prompt-boundary -->

The default agent is `codex/gpt-6-astra` at `thinking: high`. <!-- declaration: code.default-agent -->
The owner chose this default on 2026-09-24 after verification on a live agent. <!-- declaration: code.default-agent -->
An explicit mode <!-- declaration: code.configuration -->
is still required because provider vocabularies differ and default permissions <!-- declaration: code.configuration -->
can stop unattended work at its first write. The owner chooses unattended <!-- declaration: code.configuration -->
autonomy or a deliberate permission stall; the planner confirms the selected <!-- declaration: code.configuration -->
provider's spelling before recording and passing it verbatim. No positive list <!-- declaration: code.configuration -->
is portable or exhaustive. `bypass` is Claude's spelling and Codex rejects it, <!-- declaration: code.configuration -->
so the planner must ask rather than silently select a bypass. <!-- declaration: code.configuration -->

## Output location

The done predicate searches the attempt's exclusive write root. An absolute
output elsewhere can be produced correctly and remain structurally invisible.
Outputs must be relative to the run directory; use `SWARM_UNIT_DIR` when a tool <!-- declaration: outputs.attempt-relative -->
needs an absolute spelling and promotion when the bytes belong in a shared <!-- declaration: outputs.attempt-relative -->
location. <!-- declaration: outputs.attempt-relative -->

## Array failure

A Slurm array gives every task one unit attempt directory. The first task to
finish can replace the artifacts record while other tasks are incomplete, and
a dry run does not expose the fan-out. A unit must not combine `--array` with <!-- declaration: slurm.array-outputs -->
declared outputs; use one unit per shard or a separate merge unit over
per-task paths. <!-- declaration: slurm.array-outputs -->

## Code isolation and closure

`write_scopes` lets validation reject overlaps among concurrently runnable
units. It does not confine a process, so overlapping units must be ordered or <!-- declaration: code.write-scopes -->
given disjoint scopes. <!-- declaration: code.write-scopes -->

The coordinator instead gives each code attempt a verified worktree at the
anchored base. That isolates paths and permits concurrent attempts to share a
pull-request target, but it cannot isolate Unix principals. <!-- declaration: code.worktree-isolation -->

The coordinator creates the source branch. The unit must declare its repository <!-- declaration: code.target-branch -->
and `target_branch`, the destination into which its pull request merges; legacy
`branch` cannot substitute. <!-- declaration: code.target-branch -->
