# U4.0: pinned effect inventory and baseline

Base: **276e76af05a83846ea9c45ae7d030cf11a2e4e94**. Every citation below is
`file:function:line` at this commit. File aliases expand literally: S=swarm.py,
SS=swarm_state.py, SW=swarm_workspaces.py, SD=swarm_dispatch.py,
SP=swarm_preflight.py, SO=swarm_outbox.py, SC=swarm_continue.py,
SCL=swarm_code_launch.py, M=merge_unit.py. All are under
`skills/hanig-swarm/scripts/` except M and d5_*.py, under
`skills/hanig-orchestrate/scripts/`. Test basenames mean `tests/test_NAME.py`.
Comma-separated lines identify separate callsites, with stable ID `row.line`.
Quoted source fragments identify the operation behind each claim.

## Supported envelope and accepted limits

This prevents competing sessions from initiating operations. It does not prevent planning mistakes by the valid holder or replace existing crash reconciliation.

U4.0 is evidence only: listed surfaces/reachable helpers, no H, enforcement or
production-spawn readiness claim. I=initiation; C=completion candidate ONLY if
bound to its admitted operation; L=legacy coordination; R=read/planning.
Each row names the proposed adapter owner and Start → Finish. All I rows must
be split from subsequent initiations; comma-separated calls require individual
review, not entry-only admission. C rows are blocked where binding is unresolved.
Existing local multiwrite crash transactions must retain their ordering.

## Coordinator effects

S:cmd_run:3646 calls `advance`; S:cmd_advance:3669 is `return cmd_run(args)`.
Every pass/submit row is reachable from both; direct `advance` also matters.

| ID / pinned evidence | Class, owner; Start → Finish; split decision | Test route |
|---|---|---|
| paths S:_prepare_command_paths:3520 `CP.migrate_legacy_defaults` | I, path owner; copy legacy roots → marker; separate from dispatch. coordinator_paths.py:migrate_legacy_defaults:253,254,262 copies/publishes. | coordinator_paths |
| lock S:cmd_run:3621,3656 `SS.acquire_lease/release_lease` | L, coordinator; flock/epoch → release; retain. SS:acquire_lease:120,179 `flock`, `write_json`; SS:bump_lease_epoch:197 epoch write. | swarm_state |
| watcher S:cmd_run:3644,3645,3653,3654 refresh/start/save; S:_start_code_terminal_watchers:3381,3387,3401,3409,3416 `save_state`/`Popen` | I/C, watcher owner; intent/log/spawn → status; split spawn from writes. | arc725_terminal_watcher |
| state S:advance:2073,2115,2131,2169,2193,2215,2221,2228,2241,2263,2285,2295,2303,2310,2345,2350,2362,2375,2399,2416,2529,2696,2797,2803,2826,2861,2916,2939,3011,3024,3046,3049,3113 `SS.save_state` | I or bound C, state owner; persist transition → ack; split from later initiation. Includes clock/DONE repair, ratification, deadline, recovery, judgment, budget, receipt repair. Sink SS:save_state:350 `U.write_json`. | swarm_state, swarm_evidence |
| cleanup S:advance:2092,2101,2798 `SW._archive_code_worktree`; :2112 `_cleanup_codex_home` | I, cleanup owner; preserve/restore-check → remove resource; split. SW:_preserve_and_archive_code_worktree:464,499 Git remove/Paseo archive; SW:_cleanup_codex_home:306 `CH.remove`. | arc682_recovery, swarm_workspaces |
| recover S:advance:2195,2265 `_recover_code_launch`; :2223,2296,2351 `_bind` | C if bound else I, recovery owner; observe orphan → persist launch/bind. SW:_recover_code_launch:155 `paseo inspect`; SD:_bind:279 `unit.py bind`. | swarm_code_launch, swarm_dispatch |
| allocate S:advance:2912 `SD._allocate`; SD:_allocate:38 `U.run` | I, allocation owner; allocate fresh root/spec/event → path (unit.py:cmd_allocate:927,957,960); separate from launch. | swarm_dispatch |
| dispatch S:advance:2338,2957 `_submit` | I, dispatch owner; one recovered/new launch → handoff; every retry separately admitted. | attempt_worktrees |
| check S:advance:2436,2438 `SE._check` | I/C, checker owner; launch checker → receipt/digest; split from transition. swarm_evidence.py:_check:858 owns process protocol, not a pure read. | swarm_evidence, pre_dispatch_artifacts |
| claim S:advance:2829,2950,2966,3030 release/take/note output claims | L/I, claim owner; one namespace operation → result; split each destination. SP:_take_output_claims:378,407,408,420 mkdir/rename/write; SP:_drop_claims:432 and _release_output_claims:448 remove; _note_claimed_attempt:467 writes. | dispatch_claims |
| nudge S:advance:2694 `maybe_continue`; S:maybe_continue:1976,1977,1984 save/send/save | I/C, continuation owner; one `paseo send` → ack; split. Pending resend is new initiation; :1969–1975 explicitly admits duplicate delivery. | continuation |
| outbox S:advance:3060,3140 backfill/emit; S:cmd_stop_unit:3759 emit | I, outbox owner; local obligation → durable ack. SO:emit_intent:613,646 sequence/append; SO:backfill_tracker_intents:688,691 rewrite. Split from remote drain. | swarm_outbox |
| anchor S:_submit:1440,1451 capture/write launch; :1487,1510,1541,1684,1713,1723,1734,1737,1804,1834,1856,1863,1879,1892,1896 `SS.save_state` | I/C, preparation owner; pin one fact → persist; split. SCL:_write_code_launch_record:309 and _write_launch_record:586 `fh.write`. | swarm_code_launch |
| isolation S:_submit:1530 `_clear_isolation_marker` | I, preparation owner; unlink marker → result (SD:_clear_isolation_marker:206); separate. | swarm_dispatch |
| slurm S:_submit:1564,1569,1572 `write_json`, `write_text`, `U.run` | I/C, scheduler owner; script/spec then one sbatch attempt → job ID; split run's start/wait and preparation. | swarm_dispatch; baseline run/advance |
| pipeline S:_submit:1594,1614,1626 `open`, `Popen`, `write_json` | I/C, engine owner; log/detached launch → launch record; split; R8/lifetime blocker below. | swarm_dispatch; baseline pipeline |
| code S:_submit:1716 `_create_code_worktree`; :1795,1809,1816 `CH.allocate/remove/populate`; :1826 `U.run(argv)` | I/C, worktree/home/worker owners; prepare each resource separately, launch Paseo → handoff. SCL:_create_code_worktree:256 `worktree add`; codex_home.py:allocate:116 mkdir, populate:152,156 symlink, remove:182 rmtree. | attempt_worktrees, codex_home |
| continue S:cmd_continue:3532 `SC.continue_plan`; SC:_plan_file:114 `flock` | L, plan owner; lock current inode → close; retain retry on replaced inode. | continue_command |
| seed SC:_continuation:80,84 and S:cmd_validate:3565 `_seed_reachability_problem` | I, Git metadata owner; fetch → validate/delete temporary ref. SP:_seed_reachability_problem:848,879 `fetch`, `update-ref -d`; split from publication; preview also fetches. | validate_seeds |
| publish SC:continue_plan:159 `_append`; SC:_append:134,139,143 write/replace/unlink | I, plan owner; atomic publication → result; separate; no downstream dispatch. | continue_command; baseline continue |
| stop S:cmd_stop_unit:3749,3758 bump/save; :3759 emit | I, decision owner; terminal disposition → separate outbox obligation. :3723–3729 replay repairs outbox without new decision. | abandon_supersede; baseline stop |

## Merge effects

| ID / pinned evidence | Class, owner; Start → Finish; split decision | Test route |
|---|---|---|
| mlock M:main:984,990 `S.acquire_lease/release_lease` | L, operator; existing flock → release before advance/verifier; retain. Dry :981 skips acquisition. | merge_unit contention |
| repair M:current_intent:431,432,435,436,475 write/unlink | I, intent owner; cancellation/rollback/abandonment repair → remove pending marker; split records/deletion. Dry :523 passes `repair=False`. | merge_unit recovery |
| mabandon M:abandon:486,488 `durable_write` | I, intent owner; sidecar commit point → resolved intent; split while preserving crash order. | baseline merge-abandon |
| intent M:reconcile:632 `durable_write` | I, preparation owner; request intent → durable ack; separate from request. | baseline merge |
| cancel M:cancel_before_request:502,507,511,515,516 write/unlink | C candidate, preparation owner; pending/cancel-or-rollback/commit → remove pending; binding required, no remote cancellation claim. | baseline merge-cancel |
| request M:reconcile:647 `run(cmd["merge"])` | I, merge owner; one conditional squash → observe outcome; split synchronous run (:96 subprocess.run). | baseline merge-ok/fail/pending |
| record M:reconcile:686,692,706,708 write/receipt child/save/write | C candidate, merge owner; record actual merge → reread journal (:693–695); R4 blocker. S:cmd_merge:4656 `_fsync_append` is the real recorder. | baseline merge/record |
| next M:main:997 `run(advance)` | I, coordinator; new pass → CLI result; after flock release. | merge_unit |
| tracker M:main:1001,1002 drain/audit; M:print_tracker_drain:886 `subprocess.run` | I, tracker owner; each remote mutation → ack; split opaque drain via U6. Audit :930 launches/writes local report (R w.r.t. tracker only). | merge_unit tracker |
| verify M:verify_integration:724 `V.run_merge_preconditions`; :808,810 append/acknowledge | I/C, verifier owner; each verifier → evidence after reacquiring flock (:735); split preparation, spawn, cleanup and recording. | merge_verification_routing |

**Coverage blocker U4-V:** reachable verifier effects need a separately scoped
inventory before dispatch. verify.py:run_merge_preconditions:797,803,827,837,861,863
creates trees, runs remote/local checks and deletes trees;
verify.py:_candidate_checkout:638,648 initiates `merge --no-commit` / `write-tree`;
verify.py:_observe_execution:1016,1045 starts/kills a process group.
remote_verify.py:run_remote:1539 transfers/starts remote work; its remote worker,
supervisor and cleanup have additional submission points not pinned here.
**U4-T** similarly binds per-issue effects hidden behind tracker drain to U6.
These are not absent classes or permission to admit opaque helpers once.

## Absent classes and feasibility

Run/advance initiate no forge merge: S:advance:2612 `SE.admit_merge` reads
receipts; :2651 sets DONE. No remote tracker write: S:_emit_current_intent:3674
only calls SO.emit_intent, ending at journal append (:646). The coordinator's
network-import guard is test_merge_unit.py:test_coordinator_never_imports_network_operator:1332.
Stop has no cancellation/dispatch/replacement: S:cmd_stop_unit:3744–3769 writes
state/outbox and reports retention; successor is already DONE and bound
(:3737,3741). Continue returns after append/print (SC:continue_plan:159–161),
preview skips append (:158), but fetch is present. `_submit` returns per kind
(S:1576,1633,1897); retries and nudges are the separate rows above.
M:reconcile:560 ends dry-run; forge observations :564,573,608,612,616,629,637,648,654
are R, not requests. Successful merge recording is C, not fresh holder-gated I.

Activation: d5_registry.py:d5_active:68 is `return False`. Import :55 resolves
siblings and :57 changes sys.path; skill_paths.py:loaded_skill_root:40,45
stats/resolves installation paths, not authority state. **U4-P prerequisite:**
prove cold transitive import purity before choosing the active-wrapper lookup.
The present oracle preloads D5 sentinels and cannot establish that cold lookup
property. Do not substitute lookup/registration for the activation predicate.

**U4-R4 errata prerequisite:** the real recorder cannot currently be bound through
R4. d5_completion.py:bind:73–77 accepts ledger Attempt/Receipt only;
_Completion.complete:54 calls only `ledger.complete`; d5_effect_log.py:complete:313–327
updates its ledger. R6's capability remains internal (d5_submission.py:submit:126,145);
Boundary.start receives Operation, Finish no capability. A D5 receipt alone
cannot satisfy M4. Separate errata must establish operation-bound recording and
cross-process ownership, without U4 changing U3. Inactive recording stays on S:cmd_merge:4656.

Revocation does not deactivate D5: d5_control.py:revoke:56 calls store.revoke
under START, without changing d5_active. ARC-3636-OD1 therefore permits completion
after revocation while still active. M4 needs real R2/R4 barriers:
success observed → revoke returned → recorder released, existing journal persists,
next initiation denied. No unprotected postlude or duplicate R6 release gate.

R6 d5_submission.py:submit:91,97 retains shared DRAIN; :103–113 takes START,
fresh R5 checks/prepare/Start; :136 finishes outside START; :147–148 releases only
`H and S and (not A or N or (F and C))`. :92–93 refuses ambient START.
R5 d5_admission.py:_validate_locked:80–101 checks mandate/store/identity/custody/scope/time.
R3 d5_install.py:_installation:63 takes exclusive DRAIN then START. R9 delegates:
d5_lease.py:grant:69–73, d5_custody.py:claim:34 and relinquish:41.

R8 d5_process.py:DESIGNATED_BOOTSTRAPS:33 is empty; protected_spawn:73 refuses
unlisted bootstraps, :80 refuses START. worker_spawn:93 supports argv-only fresh
pipes, no protected descriptors. **U4-R8 prerequisite:** prove production adapter
routes; pipeline S:_submit:1615–1621 needs cwd/env/log/session options absent from
worker_spawn, and detached lifetime must preserve scheduling. Protected coordinator
children need separately scoped designated bootstraps (test_d5_acceptance.py:37).
No synthetic Boundary proves these production routes eligible.

Proposed lock order: coordinator flock → R6 DRAIN → START → store STATE;
release START before remote waits; journal flock inside completion.
swarm_journal.py:_fsync_append:60,75–80 holds flock through heal/write/fsync.
Merge's recording lease is the existing coordinator flock, not D5 authority;
M:main:990 releases before :997 advance; verification reacquires (:735).
R2 has no coordinator acquisition, allowing revoke while Finish waits.
U4.3 must prove this order and M5 with otherwise admissible authority.

## Oracle and subsequent budgets

The oracle archives BASE skills independently, materializes both sides at the
same paths, runs cold with identical fixed clocks/IDs/environment, and compares
results, stdout/stderr, ordered subprocess argv/kwargs and persisted bytes.
Import fencing and base hashes prevent new helpers supplying oracle expectations.
Named mutations: oracle-uses-new-helper, baseline-drop-subprocess,
baseline-change-record-bytes. External services and some preparation/evidence
readers are synthetic; coverage is the test's SCENARIOS, not every input.
No production helper is introduced. Existing exclusion tests remain unchanged.

Estimated complete bytes including tests, grounded in the cited source spans:

| Unit | Basis / required split | Budget |
|---|---|---:|
| U4.1 | R6 156 lines / R4 77; after P/R4/R8 prerequisites | 22,000 |
| U4.2 | M:519–829 too large together: preparation/cancellation; verifier separately | 24,000; U4.2v 26,000 |
| U4.3 | M:632–714,984–1002 request/record; tracker separately U4-T | 28,000 |
| U4.4 | S:1370–1898,2003–3142: split code/home, scheduler/engine, state/outbox/cleanup, watchers | U4.4a–d each 26,000 |
| U4.5 | S:3668–3669 shared route; direct helper/retry coverage | 16,000 |
| U4.6 | SC:106–166 plus SP:835–880 fetch/publication | 24,000 |
| U4.7 | S:3678–3769 terminal decisions/outbox, abandonment | 20,000 |
| U4.8 | S:3735–3748 successor binding/supersession | 18,000 |
| U4.9 | R10 composition, cross-surface M1–M6; M4 process/lock case separately | 25,000; U4.9m4 22,000 |

Prerequisites U4-V/T/P/R4/R8 need their own contracts/budgets before dispatch.
Unresolved designs have no defensible implementation estimate yet. No U3 edit
is authorized here. These estimates must be remeasured against actual diffs.
