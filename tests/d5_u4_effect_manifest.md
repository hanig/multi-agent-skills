# U4.0 pinned inventory

Base **276e76af05a83846ea9c45ae7d030cf11a2e4e94** for every file:function:line.
Aliases: S=swarm.py, SS=swarm_state.py, SW=swarm_workspaces.py,
SD=swarm_dispatch.py, SP=swarm_preflight.py, SO=swarm_outbox.py,
SC=swarm_continue.py, SCL=swarm_code_launch.py, M=merge_unit.py.
Paths: skills/hanig-swarm/scripts except M/d5_* in hanig-orchestrate/scripts.
Tests: tests/test_NAME.py. IDs: row.line.

## Supported envelope and accepted limits

This prevents competing sessions from initiating operations. It does not prevent planning mistakes by the valid holder or replace existing crash reconciliation.

No H, enforcement or spawn-readiness claim. Row contracts: I=Start one named initiation, Finish interprets its result;
C=bind admitted operation, Finish records its factual result; L=acquire/release
existing exclusion; R=read/planning without admission. Owner after `/` is the proposed adapter in the command process. Split each
initiation/opaque I+C helper; synchronous local results allowed. Retain crash
ordering/L locks. C requires binding. No adapter is implemented here.

## Coordinator effects

S:cmd_run:3646 calls `advance`; S:cmd_advance:3669 is `return cmd_run(args)`.

| ID / pinned evidence | Class, owner; Start → Finish; split decision | Test route |
|---|---|---|
| paths S:_prepare_command_paths:3520 `CP.migrate_legacy_defaults` | I/path. coordinator_paths.py:migrate_legacy_defaults:249,250,262 copies/publishes. | coordinator_paths |
| lock S:cmd_run:3621,3656 `SS.acquire_lease/release_lease` | L/coordinator. | swarm_state |
| watcher S:cmd_run:3644,3645,3653,3654 refresh/start/save; S:_start_code_terminal_watchers:3381,3387,3401,3409,3416 `save_state`/`Popen` | I/watch + C/status. | arc725_terminal_watcher |
| state S:advance:2073,2115,2131,2169,2193,2215,2221,2228,2241,2263,2285,2295,2303,2310,2345,2350,2362,2375,2399,2416,2529,2696,2797,2803,2826,2861,2916,2939,3011,3024,3046,3049,3113 `SS.save_state` | I/state or bound C/state. SS:save_state:350 `U.write_json`. | swarm_state, swarm_evidence |
| cleanup S:advance:2092,2101,2798 `SW._archive_code_worktree`; :2112 `_cleanup_codex_home` | I/preservation + I/removal. SW:_preserve_and_archive_code_worktree:464,499 Git remove/Paseo archive; SW:_cleanup_codex_home:306 `CH.remove`. | arc682_recovery, swarm_workspaces |
| recover S:advance:2195,2265 `_recover_code_launch`; :2223,2296,2351 `_bind` | C/recovery if bound, else I/recovery. SW:_recover_code_launch:155 `paseo inspect`; SD:_bind:279 `unit.py bind`. | swarm_code_launch, swarm_dispatch |
| allocate S:advance:2912 `SD._allocate`; SD:_allocate:38 `U.run` | I/allocation. unit.py:cmd_allocate:927,957,960 mkdir/spec/event. | swarm_dispatch |
| dispatch S:advance:2338,2957 `_submit` | I/dispatch (each retry). | attempt_worktrees |
| check S:advance:2436,2438 `SE._check` | I/checker + C/receipt. swarm_evidence.py:_check:858 owns process protocol, not a pure read. | swarm_evidence, pre_dispatch_artifacts |
| claim S:advance:2829,2950,2966,3030 release/take/note output claims | L/claim + I/namespace. SP:_take_output_claims:378,407,408,420 mkdir/rename/write; SP:_drop_claims:432 and _release_output_claims:448 remove; _note_claimed_attempt:467 writes. | dispatch_claims |
| nudge S:advance:2694 `maybe_continue`; S:maybe_continue:1976,1977,1984 save/send/save | I/send + C/ack (resend is new I). | continuation |
| outbox S:advance:3060,3140 backfill/emit; S:cmd_stop_unit:3759 emit | I/outbox. SO:emit_intent:611,646 sequence/append; | swarm_outbox |
| anchor S:_submit:1440,1451 capture/write launch; :1487,1510,1541,1684,1713,1723,1734,1737,1804,1834,1856,1863,1879,1892,1896 `SS.save_state` | I/preparation + C/launch facts. | swarm_code_launch |
| isolation S:_submit:1530 `_clear_isolation_marker` | I/marker. SD:_clear_isolation_marker:206 unlink. | swarm_dispatch |
| slurm S:_submit:1564,1569,1572 `write_json`, `write_text`, `U.run` | I/script + I/sbatch + C/job ID. | swarm_dispatch; baseline run/advance |
| pipeline S:_submit:1594,1614,1626 `open`, `Popen`, `write_json` | I/log + I/engine + C/launch. | swarm_dispatch; baseline pipeline |
| code S:_submit:1716 `_create_code_worktree`; :1795,1809,1816 `CH.allocate/remove/populate`; :1826 `U.run(argv)` | I/worktree + I/home + I/Paseo + C/handoff. SCL:_create_code_worktree:256 `worktree add`; codex_home.py:allocate:116 mkdir, populate:151,155 symlink, remove:182 rmtree. | attempt_worktrees, codex_home |
| continue S:cmd_continue:3532 `SC.continue_plan`; SC:_plan_file:104 `flock` | L/plan inode. | continue_command |
| seed SC:_continuation:77,81 and S:cmd_validate:3565 `_seed_reachability_problem` | I/Git metadata. SP:_seed_reachability_problem:848,879 `fetch`, `update-ref -d`; split from publication; preview also fetches. | validate_seeds |
| publish SC:continue_plan:148 `_append`; SC:_append:121,126,130 write/replace/unlink | I/plan. | continue_command; baseline continue |
| stop S:cmd_stop_unit:3749,3758 bump/save; :3759 emit | I/decision + I/outbox. | abandon_supersede; baseline stop |

## Merge effects

| ID / pinned evidence | Class, owner; Start → Finish; split decision | Test route |
|---|---|---|
| mlock M:main:984,990 `S.acquire_lease/release_lease` | L/operator. | merge_unit contention |
| repair M:current_intent:431,432,435,436,475 write/unlink | I/intent repair. | merge_unit recovery |
| mabandon M:abandon:486,488 `durable_write` | I/sidecar + I/resolve. | baseline merge-abandon |
| intent M:reconcile:632 `durable_write` | I/intent. | baseline merge |
| cancel M:cancel_before_request:502,507,511,515,516 write/unlink | C/cancellation if bound. | baseline merge-cancel |
| request M:reconcile:647 `run(cmd["merge"])` | I/conditional squash + C/result. | baseline merge-ok/fail/pending |
| record M:reconcile:686,692,706,708 write/receipt child/save/write | C/merge recorder if bound. | baseline merge/record |
| next M:main:997 `run(advance)` | I/advance. | merge_unit |
| tracker M:main:1001,1002 drain/audit; M:print_tracker_drain:886 `subprocess.run` | I/drain (U6) + R/audit. | merge_unit tracker |
| verify M:verify_integration:724 `V.run_merge_preconditions`; :808,810 append/acknowledge | I/verifier + C/evidence. | merge_verification_routing |


**U4-V blocker:** inventory reachable verifier effects before dispatch. verify.py:run_merge_preconditions:797,803,827,837,861,862
creates trees, runs remote/local checks and deletes trees;
verify.py:_candidate_checkout:638,648 initiates `merge --no-commit` / `write-tree`;
verify.py:_observe_execution:1016,1045 starts/kills a process group.
remote_verify.py:run_remote:1539 transfers/starts remote work; remote internals have unpinned effects.
**U4-T:** bind tracker-drain per-issue effects to U6; never admit the opaque helper once.

## Absence and protocol fit

Absent: run/advance forge merge (S:advance:2612 reads `SE.admit_merge`, :2651 sets
DONE); remote tracker write (S:_emit_current_intent:3674 → SO:emit_intent:646
journal append); stop cancellation/replacement/dispatch (S:cmd_stop_unit:3744–3769
only records/reports; :3737,3741 requires existing DONE/bound successor);
continue dispatch (SC:continue_plan:148–150 append/print/return). Preview skips
append (:147), not seed fetch. S:_submit:1576,1633,1897 returns by kind; retries
use dispatch/nudge rows. M:reconcile:560 returns dry; :564,573,608,612,616,629,637,648,654
are observations, not forge mutations.

Activation d5_registry.py:d5_active:68 returns False. Import :55 resolves siblings,
:57 changes sys.path; skill_paths.py:loaded_skill_root:40,45 stats/resolves paths.
**U4-P blocker:** cold transitive-import purity needs proof; preloaded sentinels
cannot establish it. 
**U4-R4 errata blocker:** d5_completion.py:bind:73–77 only accepts ledger handles;
_Completion.complete:54 only calls ledger.complete (d5_effect_log.py:complete:313–327).
No binding to S:cmd_merge:4656's real
journal recorder exists. R2 d5_control.py:revoke:55 revokes store
without deactivation: ARC-3636-OD1 permits active completion after revocation.
M4 must order success → real revoke returned → recorder released, preserve the
existing journal and deny next initiation. D5 receipt alone is insufficient.

R6 d5_submission.py:submit:91,97 holds DRAIN; :103–113 START covers R5/prepare/Start;
:136 Finish is outside START; :147–148 owns the sole H/S/(not A or N or F+C)
release gate; :92–93 refuses ambient START. R5 d5_admission.py:_validate_locked:80–101
freshly checks mandate/store/identity/custody/scope/time. R3 d5_install.py:_installation:63
orders exclusive DRAIN then START. R9 delegates (d5_lease.py:grant:69–73;
d5_custody.py:claim:34/relinquish:41).

**U4-R8 blocker:** d5_process.py:DESIGNATED_BOOTSTRAPS:33 is empty;
protected_spawn:73,80 refuses undesignated/ambient START. worker_spawn:93 supports
argv-only fresh pipes without protected descriptors; S:_submit:1615–1621 needs
cwd/env/log/session options and detached lifetime. Prove routes without changing
scheduling; protected children require designated bootstraps (test_d5_acceptance.py:37).

Proposed order: coordinator flock → DRAIN → START → store STATE, then journal
flock in completion (swarm_journal.py:_fsync_append:60,75–80). Recording lease
is coordinator exclusion, not authority; M:main:990 releases before advance:997;
verification reacquires:735. U4.3 must prove
ordering/M5 with admissible authority; never recreate R6's release gate.

## Oracle and budgets

Oracle coverage = test SCENARIOS, with synthetic services. Mutations:
oracle-uses-new-helper, baseline-drop-subprocess, baseline-change-record-bytes.

Estimated bytes including tests:

| Unit | Basis / split | Budget |
|---|---|---:|
| U4.1 | R6 156/R4 77 lines; after P/R4/R8 | 22,000 |
| U4.2 | M:519–829 preparation/cancellation; verification separately | 24,000; U4.2v 26,000 |
| U4.3 | M:632–714,984–1002 request/record; tracker U4-T | 28,000 |
| U4.4 | S:1370–1898,2003–3142 split code/home, scheduler/engine, state/outbox/cleanup, watchers | U4.4a–d each 26,000 |
| U4.5 | S:3668–3669 shared route/direct-helper retries | 16,000 |
| U4.6 | SC:99–154/SP:835–880 fetch/publication | 24,000 |
| U4.7 | S:3678–3769 abandonment | 20,000 |
| U4.8 | S:3735–3748 supersession binding | 18,000 |
| U4.9 | R10/M1–M6, separate M4 process/lock case | 25,000; U4.9m4 22,000 |

U4-V/T/P/R4/R8 need separate contracts/budgets; no U3 edit authorized. Remeasure diffs.
