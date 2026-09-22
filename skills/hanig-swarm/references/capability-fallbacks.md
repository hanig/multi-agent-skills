# Capability fallbacks

Loading the skill proves only loader compatibility. Check each capability when <!-- declaration: placement.reference-elaboration -->
it is needed and report the exact missing command, service, account, or path. <!-- declaration: placement.reference-elaboration -->

| capability | if unavailable | bounded fallback |
|---|---|---|
| shell/filesystem | no usable shell or denied path | report the blocked command/path and request host-approved access; never fabricate output | <!-- declaration: capability.shell-filesystem -->
| Python 3 and Git | helper or worktree evidence cannot run | install or expose host-approved programs, preserve the repository cwd, then rerun | <!-- declaration: capability.python-git -->
| Slurm scheduler | submission/accounting cannot be queried | leave scheduler work blocked and retain the declared plan; never infer a run result | <!-- declaration: capability.slurm -->
| Paseo and agent bus | binary, daemon, state, or bus is absent | do bounded local work without delegation, or ask an operator to configure the optional service; never create look-alike paths or a daemon | <!-- declaration: capability.paseo-bus -->
| reviewer providers | configuration or coordinator credentials are absent | retain evidence and mark the change unreviewed; never borrow credentials or substitute worker self-confidence | <!-- declaration: capability.review -->
| tracker connector | no authorized connector/account | retain the idempotent outbox intent as pending synchronization for an authorized session |

Python and Git are baseline prerequisites for workflows that name them. Paseo,
the bus, reviewer access, and tracker connectors are optional capabilities with
different fallbacks. None is installed or configured by skill installation.
Provider authentication used to start a worker is not authority to dispatch a
different backend or call another external service.
