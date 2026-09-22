# Survey and interview evidence

The decision surface is `../SKILL.md`. This reference records why the survey
and interview declarations exist and gives examples without becoming a second
source of behavior. <!-- declaration: placement.reference-elaboration -->

<!--
  A NOTE ON THIS FILE'S MARKERS, because they have cost two review rounds.

  A declaration tie is a same-line `<!-- declaration: id -->` comment, and
  on a wrapped sentence it sits at the end of whichever line holds the
  modal -- often past column 80, visually detached from the words it
  licenses. Two consecutive review rounds produced a MAJOR finding from
  reading such a line without its marker: one on `cannot interrupt
  os.scandir` here, one on the paragraph-initial imperatives in
  tracker-sync.md. Both were tied; both read as untied.

  The convention is hanig-swarm's and is not changed here. If you are
  checking whether a modal is tied, read to end of line, and run
  `declaration_registry.py check-references` rather than scanning.
-->

## Repository walk states

The survey tree walk runs in a child process with a real deadline. A check in
the walking loop cannot interrupt `os.scandir` while `opendir()` is blocked on <!-- declaration: placement.reference-elaboration -->
a stale NFS handle or dead automount.

`repo.walk` therefore records `complete`, `truncated`, `stuck`, or `unknown`.
Truncation says a declared cap stopped a responsive tree; stuck says a
directory stopped answering and records `stuck_at`. Counts from either case
must be presented as floors rather than totals. <!-- declaration: survey.incomplete-walk -->

## Partition facts

The survey records `allow_accounts`, `deny_accounts`, partition `qos`,
`qos_grptres`, and `max_mem_per_cpu_mb`. Each permission or limit field carries <!-- declaration: survey.partition-state -->
`set`, `unrestricted`, or `unknown`; unknown means the query did not answer, so
it must be reported rather than planned around as unrestricted. <!-- declaration: survey.partition-state -->

Two observed failures motivated these fields. A 736-CPU partition had 202 idle
CPUs while an unseen `QOSGrpCpuLimit` blocked the selected account. Separately,
700 GB under `MaxMemPerCPU=5120` charged 140 CPUs rather than the requested 32.

When the per-CPU memory limit is set, the charged count is <!-- declaration: cluster.memory-charging -->
`ceil(mem_mb / max_mem_per_cpu_mb)`. The interview must quote that charged CPU <!-- declaration: cluster.memory-charging -->
count, recommend shrinking per-job memory until that charged count fits the
account, and ask whether the resulting footprint fits both budget and queue. <!-- declaration: cluster.memory-charging -->

Slurm can print `DenyAccounts` instead of `AllowAccounts`. Reading only the <!-- declaration: cluster.account-allowance -->
allow side makes an expressly denied partition look open. After reading both, <!-- declaration: cluster.account-allowance -->
ask the owner whether CPU-only work may use an exclusively allowed idle <!-- declaration: cluster.account-allowance -->
partition and recommend yes when the shared queue is capped. A named denial is <!-- declaration: cluster.account-allowance -->
not a question: report the closed route and move on. Report an unknown allow or <!-- declaration: cluster.account-allowance -->
deny state instead of asking around it or making a recommendation. <!-- declaration: cluster.account-allowance -->

The surveyed `qos_grptres` describes partition QoS only. Account or association <!-- declaration: cluster.qos-scope -->
QoS can carry another `GrpTRES` that the survey does not see, so unrestricted <!-- declaration: cluster.qos-scope -->
at the partition level must not be presented as a promise that submission will <!-- declaration: cluster.qos-scope -->
run. <!-- declaration: cluster.qos-scope -->

Validation refuses a partition the surveyed cluster does not have and refuses <!-- declaration: plan.scheduler-route -->
known allow or deny mismatches. It cannot validate account-name existence and <!-- declaration: plan.scheduler-route -->
stays silent when account data is unknown, so report that unknown state; a typo <!-- declaration: plan.scheduler-route -->
can survive validation and fail only at submission. <!-- declaration: plan.scheduler-route -->

## Repository destination examples

The survey already reports repository, remote, and branch state. A repository
with a remote is adopted without another question, and the adopted remote and
branch are stated in one line. A repository without a
remote gets one push-destination question with local work recommended. With no
repository, ask whether source must outlive the attempt and recommend nowhere. <!-- declaration: repository.destination -->

The deciding distinction is source versus data. A manifest, checkpoint, or TSV
is compute data even when code produced it. Swarm output must not be committed; <!-- declaration: repository.source-data -->
shared publication uses promotion and a named approver. <!-- declaration: repository.source-data -->

## Dispatch-complete interview

The interview is complete when the plan can run, not when questions run out.
One prior plan was validated and filed as five issues before anyone requested
the corpus subpath and glob that its command needed. <!-- declaration: interview.dispatch-complete -->

Run the schema before ending the interview and settle every required input, <!-- declaration: interview.dispatch-complete -->
partition, account, promotion destination and approver, code provider and mode,
target branch, runtime verification, and retry exposure. <!-- declaration: interview.dispatch-complete -->

Every question must concern judgment that inspection cannot settle and carry a <!-- declaration: interview.judgment-only -->
recommended answer. The categories are done criteria, scientific claim,
discardable work, budget, protected destinations, and retry exposure. <!-- declaration: interview.judgment-only -->

For a half-finished repository, the initial draft must be based on its project <!-- declaration: adoption.context -->
documents, architecture decisions, recent commits, and outputs already on
disk. <!-- declaration: adoption.context -->

Before filing that draft, completed work must be excluded and an artifact-free <!-- declaration: adoption.remaining-work -->
TODO must not be promoted into a unit merely because it exists in prose. <!-- declaration: adoption.remaining-work -->
