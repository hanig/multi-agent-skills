# U4.0a pinned effect manifest

Base **276e76af05a83846ea9c45ae7d030cf11a2e4e94** for every file:function:line.
Aliases: S=swarm.py, SS=swarm_state.py, SW=swarm_workspaces.py,
SD=swarm_dispatch.py, SP=swarm_preflight.py, SO=swarm_outbox.py,
SC=swarm_continue.py, SCL=swarm_code_launch.py, M=merge_unit.py, V=verify.py, RV=remote_verify.py,
LS=linear_sync.py, LA=linear_api.py, DC=drain_contract.py.
Paths: skills/hanig-swarm/scripts except M/d5_*/skill_paths.py in
hanig-orchestrate/scripts and LS/LA/DC in hanig-project/scripts.
Tests: tests/test_NAME.py; IV=integration_verification, RVT=remote_verification,
MVT=merge_verification_routing, SDT=swarm_dispatch, SST=swarm_state,
AW=attempt_worktrees, WT=arc725_terminal_watcher; baseline routes belong to U4.0b. Tests are future D5 routes. IDs = row/citation/line; comma lists
expand to individual sites and each loop/retry. 

## Supported envelope and accepted limits

This prevents competing sessions from initiating operations. It does not prevent planning mistakes by the valid holder or replace existing crash reconciliation.

No H/enforcement/readiness claim. Each expanded site uses:
I = Start initiates the named operation once; Finish observes return/ack before
the next I. C = bind admitted operation; Finish records only its factual result.
L = retain original acquire/release extent. R = observe/plan without admission.
Owner follows `/`; split each I/opaque I+C, never a multi-site row.
Synchronous returns allowed; retain crash order.
All C is conditional on the R4 binding prerequisite below; no adapters exist yet.

## Coordinator effects

S:cmd_run:3646 calls `advance`; S:cmd_advance:3669 is `return cmd_run(args)`.

| Evidence / ID | Contract | Test |
|---|---|---|
| paths S:_prepare_command_paths:3520 `CP.migrate_legacy_defaults` | I/path. coordinator_paths.py:migrate_legacy_defaults:249,250,262 copies/publishes. | coordinator_paths |
| lock S:cmd_stop_unit:3706,3769; M:verify_integration:735,815; S:cmd_run:3621,3656 | L/coordinator. | SST |
| watcher S:cmd_run:3644,3645,3653,3654 refresh/start/save; S:_start_code_terminal_watchers:3381,3387,3401,3409,3416 | I/watch + C/status. | WT |
| state S:advance:2073,2115,2131,2169,2193,2215,2221,2228,2241,2263,2285,2295,2303,2310,2345,2350,2362,2375,2399,2416,2529,2696,2797,2803,2826,2861,2916,2939,3011,3024,3046,3049,3113 | I/state or bound C/state. SS:save_state:350 `U.write_json`. | swarm_state, swarm_evidence |
| cleanup S:advance:2092,2101,2798 `SW._archive_code_worktree`; S:advance:2112 `_cleanup_codex_home` | I/preserve/remove. SW:_preserve_and_archive_code_worktree:464,499 Git remove/Paseo archive; SW:_cleanup_codex_home:306 `CH.remove`. | arc682_recovery, swarm_workspaces |
| recover S:advance:2195,2265 `_recover_code_launch`; S:advance:2223,2296,2351 `_bind` | C/recovery (binding required). SW:_recover_code_launch:155 `paseo inspect`; SD:_bind:279 `unit.py bind`. | swarm_code_launch, swarm_dispatch |
| allocate S:advance:2912 `SD._allocate`; SD:_allocate:38 `U.run` | I/allocation. unit.py:cmd_allocate:924,955,958 mkdir/spec/event. | SDT |
| dispatch S:advance:2338,2957 `_submit` | I/dispatch (each retry). | AW |
| check S:advance:2436,2438 `SE._check` | I/checker; C/receipt. swarm_evidence.py:_check:858 owns checker process. | swarm_evidence, pre_dispatch_artifacts |
| claim S:advance:2829,2950,2966,3030 release/take/note output claims | L/claim + I/namespace. SP:_take_output_claims:378,406,407,421 mkdir/rename/write; SP:_drop_claims:432 and SP:_release_output_claims:448 remove; SP:_note_claimed_attempt:463 writes. | dispatch_claims |
| nudge S:advance:2503 `SPRS.maybe_resume`; swarm_provider_resume.py:maybe_resume:108,116,134,144 snapshot/save/send/save; S:advance:2694 `maybe_continue`; S:maybe_continue:1976,1977,1984 save/send/save | I/send + C/ack (resend is new I). | continuation |
| outbox S:advance:3060,3140 backfill/emit; S:cmd_stop_unit:3759 emit | I/outbox. SO:emit_intent:611,646 sequence/append; | swarm_outbox |
| anchor S:_submit:1440,1451 capture/write launch; S:_submit:1487,1510,1541,1684,1713,1723,1734,1737,1804,1834,1856,1863,1879,1892,1896 | I/preparation; C/launch. | swarm_code_launch |
| isolation S:_submit:1527 `SD._isolation_submission` renders only (SD:_isolation_submission:184 returns); S:_submit:1530 `_clear_isolation_marker` | R/render + I/marker. SD:_clear_isolation_marker:206 unlink. | SDT |
| slurm S:_submit:1564,1569,1572 `write_json`, `write_text`, `U.run` | I/script/sbatch; C/job ID. | swarm_dispatch |
| pipeline S:_submit:1594,1614,1626 `open`, `Popen`, `write_json` | I/log/engine; C/launch. | swarm_dispatch |
| code S:_submit:1716 `_create_code_worktree`; S:_submit:1795,1809,1816 `CH.allocate/remove/populate`; S:_submit:1826 `U.run(argv)` | I/worktree/home/Paseo; C/handoff. SCL:_create_code_worktree:256 `worktree add`; codex_home.py:allocate:116 mkdir, codex_home.py:populate:151,155 symlink, codex_home.py:remove:182 rmtree. | attempt_worktrees, codex_home |
| continue S:cmd_continue:3532 `SC.continue_plan`; SC:_plan_file:104 `flock` | L/plan inode. | continue_command |
| seed SC:_continuation:77,81 and S:cmd_validate:3565 `_seed_reachability_problem` | I/Git metadata. SP:_seed_reachability_problem:848,877 `fetch`, `update-ref -d`; split from publication; preview also fetches. | validate_seeds |
| publish SC:continue_plan:148 `_append`; SC:_append:121,126,130 write/replace/unlink | I/plan. | continue_command |
| stop S:cmd_stop_unit:3749,3758 bump/save; S:cmd_stop_unit:3759 emit | I/decision/outbox. | abandon_supersede |

| bind S:advance:2223,2296,2351,3032 `SD._bind` | C/dispatch; record launch ID; split. | SDT |
| lease check S:advance:2387 `SS.renew_lease`; SS:renew_lease:243 `return _holds_the_path` | R/coordinator; inspect inode, no write. | SST |
| basis S:advance:2937 `SE._capture_artifact_basis`; swarm_evidence.py:_capture_artifact_basis:539 populates dict | I/state; digest, save at S:advance:2939. | pre_dispatch_artifacts |
| watch child S:cmd_watch_code_terminal:3453,3477 `paseo wait`, `advance-code-terminal` | R/wait; I/advance child owns effects; split. | WT |
| observations S:_submit:1665; S:advance:2322; M:verify_integration:775 | R/caller; show-ref/PR view → observation. | MVT |
| journal S:cmd_merge:4656 `SJ._fsync_append` | C/merge child; M requests, child journals; retain lease/lifetime in Finish. | baseline record |

## Merge effects

| Evidence / ID | Contract | Test |
|---|---|---|
| mlock M:main:984,990 | L/operator. | merge_unit contention |
| repair M:current_intent:431,432,435,436,475 write/unlink | I/intent repair. | merge_unit recovery |
| mabandon M:abandon:486,488 `durable_write` | I/sidecar/resolve. | baseline merge-abandon |
| intent M:reconcile:632 `durable_write` | I/intent. | baseline merge |
| cancel M:cancel_before_request:502,507,511,515,516 write/unlink | C/cancellation if bound. | baseline merge-cancel |
| request M:reconcile:647 `run(cmd["merge"])` | I/squash; C/result. | baseline merge-ok/fail/pending |
| record M:reconcile:686,692,706,708 write/receipt child/save/write | C/merge recorder if bound. | baseline merge/record |
| next M:main:997 `run(advance)` | I/advance. | merge_unit |
| tracker M:main:1001,1002 drain/audit; M:print_tracker_drain:886 `subprocess.run` | I/drain (U6) + R/audit. | merge_unit tracker |
| verify M:verify_integration:724 `V.run_merge_preconditions`; M:verify_integration:808,810 append/acknowledge | I/verifier + C/evidence. | MVT |


## Verifier and remote verifier expansion

Owners: v local verifier, rv connected operator, rw worker/supervisor, rc cleanup.
Transport and child operations are distinct; no nested R6 under START. Each
transaction includes its fsync/replace/finally-unlink. Wait/timeout kill ends the
started child; cleanup retries and later claims are new I. Preparation and
recovery dispositions are I; received factual evidence is C only after binding.

| Evidence / ID | Contract | Test |
|---|---|---|
| V:_git:98 | R/v Git transport; mutating callers own admission | verify |
| V:_isolated_git:133 | R/v isolated Git transport; mutating callers own admission | IV |
| V:_isolated_checkout:139,142,144,149,152,154,158,166,171,173,175,179 | I/v temp/mkdir/init/alternates/checkout; R object lookup; I failure removal | IV |
| V:read_policy:225 | R/v Git show → policy | verify |
| V:_digest_base_blob:336,375,388,402,403 | R/v ls-tree/cat-file → digest; I temporary capture, Finish closes | verify |
| V:corpus_evidence:442 | R/v Git diff → paths | verify |
| V:run_in_checkout:495,497,500,504,507,508 | I/v temp/worktree add, verifier, worktree remove/rmtree; split | verify |
| V:integration_basis:527,540,553 | R/v cat-file/merge-base → identity | IV |
| V:target_before_merge:590,599 | R/v cat-file/rev-list → parent | IV |
| V:_candidate_checkout:636,643,648,651,654,657,662 | I/v merge-no-commit/write-tree/removals; R rev-parse; split | IV |
| V:candidate_merge_basis:674 | I/v removal → basis | IV |
| V:run_in_candidate_merge:698,705 | I/v verifier then removal; split | IV |
| V:run_merge_preconditions:836,856,861,862 | I/v each claim and removals; C output evidence; split | MVT |
| V:output_evidence:959,961,965 | C/v mkdir/temp/write → bound diagnostic paths/digests | MVT |
| V:_observe_execution:1016,1043 | I/v Popen → communicate; C timeout kill/reap of same group | verify |
| V:run_pinned:1215,1218,1222,1253,1256,1262,1266,1273 | I/v temp/copy/chmod/bin/links, launch, removal; split | verify |
| RV:resolve_executables:105 | R/rv --version → bounded response | RVT |
| RV:__call__:126 | R/rv Git transport; caller owns mutation | RVT |
| RV:_command:175 | I/rw transport, caller owns one command → status | RVT |
| RV:_git:182 | I/rw Git transport, caller owns mutation | RVT |
| RV:make_bundle:189,193,194 | I/rv commit-tree/update-ref/bundle; split | RVT |
| RV:publish:215,217,220,225,228,233 | C/rv (I preparation); write/fsync/link-or-replace/fsync/unlink → durable bytes | RVT |
| RV:publish_launch_witness:248,249,254 | I/rv mkdir/publish/fsync → prelaunch witness | RVT |
| RV:binding_lock:350,353 | L/rv mkdir/open/flock → close on exit | RVT |
| RV:acknowledge:494 | C/rv bound launch/digest → published ledger | RVT |
| RV:worker:545,559,563,564,565,567,568,574,575,582,584,589,590 | I/rw once-marker/tree/init/fetch-bundle/checkout/add/write-tree/file/verifier; C receipt/result/final; split | RVT |
| RV:scheduler_state:598 | R/rc sacct → exact terminal row | RVT |
| RV:supervise:635,638,647 | I/rw script/sbatch; C job-id; R polling; direct calls worker | RVT |
| RV:publish_cleanup:723 | C/rc audit (I cancellation intent) → durable evidence | RVT |
| RV:discover_slurm_jobs:750 | R/rc sacct/squeue → identities | RVT |
| RV:cleanup_slurm_locked:850,860,927,929,936,966,973,984,1002 | L/rc external then stage; I scancel/removal/lock unlink; C snapshots; split | RVT |
| RV:cleanup:1049,1088,1090,1097,1114 | L/rc stage; I scancel/removal; C snapshots; split, including legacy job path | RVT |
| RV:finish_empty_stage:1149,1174 | L/rc external; I rmdir after digest ack; split from first R call | RVT |
| RV:remote_call:1278 | I/rv cleanup SSH or R frame/probe → response; child owns mutation | RVT |
| RV:read_remote_record:1322 | R/rv string replace (not filesystem); frame → bytes | RVT |
| RV:run_remote:1431,1482,1486,1522,1524,1525,1527,1528,1533,1534,1539,1563,1572,1580,1587,1603,1623,1638,1645,1649,1669,1679,1697,1727 | I/rv transfer/recovery/launch/cleanup/retirement; C evidence/acks; split (below) | RVT |
| RV:receive:1506 | C/rv ingest/publish → durable evidence | RVT |
| RV:frame_call:1551 | R/rv frame SSH → bytes, no verifier rerun | RVT |
| RV:main:1747 | C/rw supervision marker; cleanup separate | RVT |

Inline I/rw stage mkdir/copy/exec: RV:module:674 BOOTSTRAP. I/rc cleanup,
empty-stage and retirement (external flock/digest/history+lock unlink):
RV:run_remote:1597,1610,1682. Split from SSH; route RVT.
No R8 designation. RV:run_remote:1439 retrieves, never reruns; RV:worker:545 rejects duplicate workers. Cleanup allows
three extra passes (RV:run_remote:1650; RV:module:37) and four cancellation
attempts (RV:cleanup_slurm_locked:922; RV:cleanup:1083); every retry is new I.
RV:cleanup_slurm:825,831 polling is R. Fresh launch: RV:run_remote:1454. Keep policy.

## Tracker drain expansion (handoff to U6)

| Evidence / ID | Contract | Test |
|---|---|---|
| tlock LS:project_lock:823,824,826 | L/drain process; mkdir/open/flock → close on exit; retain outside per-issue boundaries. | linear_drain |
| comment LS:drain_locked:1009; LS:confirm_comment:761 | I/U6 comment; one deterministic-ID create → exact readback at LS:confirm_comment:769; split every intent. | linear_drain |
| state LS:drain_locked:1030; LS:reconcile_issue:927,935 | R/U6 workflow-state query at 927; I/U6 issueUpdate at 935 → readback at LS:reconcile_issue:938; separate from comment. | linear_drain |
| acknowledgment LS:drain_locked:1065; LS:record_drain_receipt:895,897,898,899 | C/U6 receipt owner; temp/intent/observation + reconcile child → child exit and temp cleanup; bind to confirmed mutation. | linear_drain |
| receipt DC:record_reconciliation:115,124; SO:record_receipt:1016 | C/reconcile child; record receipt then reconciliation journal → durable acknowledgment; keep ordering. | drain_contract |
| receipt sink SO:_append_receipt_atomic:912,914,917,923,952,954 | L/receipt inode + C/receipt append/fsync → locked duplicate check and durable record; retain. | outbox |
| transport LA:request:127; LA:transport:101 | I/U6 mutation or R/query according to caller; one HTTP request → response; expose submission/wait, no opaque retry. | linear_drain |

LS:confirm_comment:760,769 reads before/after ambiguous create.
LS:drain_locked:1038,1051,1057,1061 withholds unresolved/superseded/dry/unconfirmed
acks. LA:request:127 and LA:query:148 make one request, no HTTP retries. Later
drains re-admit mutations. M:print_tracker_audit:897 only reads. **U4-T blocker:**
U6 must split comment/state and bind child ack; admitting the drain once fails.

## Absence evidence and protocol fit

Absent: forge merge in run/advance (S:advance:2612 admits evidence, S:advance:2651
sets DONE); remote tracker write there (S:_emit_current_intent:3674 → SO:emit_intent:646
local journal); stop cancellation/replacement/dispatch (S:cmd_stop_unit:3744,3749,3758,3759,3769
records disposition; S:cmd_stop_unit:3737,3741 requires DONE/bound successor);
continue dispatch (SC:continue_plan:148,149,150 append/print/return). Preview
skips append at SC:continue_plan:147, still fetches seeds. S:_submit:1576,1633,1897
returns by kind. M:reconcile:560 returns dry; M:reconcile:564,573,608,612,616,629,637,648,654
observes forge state, unlike request at M:reconcile:647. Dry merge can still
construct a candidate through V:candidate_merge_basis:670,674; R is not zero I/O.

**P:** d5_registry.py:d5_active:68 returns False. Cold import is NOT zero I/O:
d5_registry.py:module:55,57 resolves siblings/changes sys.path;
skill_paths.py:loaded_skill_root:40,45 stats/resolves. Cold probe rejects storage/process/network/lock effects and registry access;
source discovery is allowed. Missing siblings can refuse before evaluation. **Blocker P:** separate U3 errata
(12,000 bytes with tests) exposing the SAME predicate without discovery, or an
owner-approved M6 argument accepting these new reads/refusals. No alternate flag.

**R4:** d5_completion.py:bind:73,76,77 accepts ledger handles only;
d5_completion.py:complete:52,53,54 calls ledger.complete under DRAIN
(d5_effect_log.py:complete:313). No recorder callback. R6 binds after Start at
d5_submission.py:submit:124, runs Finish at d5_submission.py:submit:136 then
complete at d5_submission.py:submit:144. Finish retains DRAIN but cannot bind S:cmd_merge:4656 through R4 before Start
or transfer capability to its child. **Blocker R4:** separate U3 errata/contract amendment
(24,000) settling actual journal binding/ownership; inactive keeps old recorder.
R2 d5_control.py:revoke:55 does not deactivate. M4: success → real revoke returned
→ recorder released; D5 active, old journal persisted, next initiation denied.
D5 receipt alone is insufficient. d5_completion.py:uncertain:63 retains ambiguity;
recorder failure keeps existing reconciliation.

R6 d5_submission.py:submit:91,97 holds DRAIN; d5_submission.py:submit:103,105,106,110,113
START/admit/prepare/recheck/Start; Finish outside START. Sole release gate
H/S/(not A or N or F+C) at d5_submission.py:submit:147,148; ambient START refused
at d5_submission.py:submit:92,93. R5 d5_admission.py:_validate_locked:80,86,87,90,98,101
freshly checks registration/mandate/store/identity/scope/time. R3
d5_install.py:_installation:63 orders DRAIN then START. R9 delegates:
d5_lease.py:grant:69,72,73; d5_custody.py:claim:33; d5_custody.py:relinquish:40.

**R8:** d5_process.py:worker_spawn:93,95 accepts argv, fresh pipes, no protected
fds; caller drains/reaps. d5_process.py:module:33 has an empty allowlist;
d5_process.py:protected_spawn:73,80,86 refuses undesignated/ambient START;
d5_effect_child.py:bootstrap:50 owns child admission. Thus protected_spawn cannot
be Boundary.start. R10 leaves production designations to U4–U7.
S:_submit:1572 sbatch is argv-compatible subject to handoff proof;
S:_submit:1614 pipeline and S:_start_code_terminal_watchers:3401 need missing
cwd/env/log/session options; S:_submit:1826 Paseo and S:maybe_continue:1977 send
need preserved env/timeouts. **Blockers:** R8a launcher/options/async lifetime
(24,000); R8b designated bootstrap outside START, one owner, fd/recording lifetime
(28,000); U4.2r remote authority transport. Synthetic success is not production M2.

Lock order: coordinator flock → DRAIN → START → STATE; Finish retains DRAIN then
journal flock (swarm_journal.py:_fsync_append:60,75,80). Lease is not authority.
M:main:990 releases before M:main:997 advance. M:verify_integration:724 runs
unlocked then reacquires at M:verify_integration:735. RV:run_remote:1425 holds
binding lock across waits; RV:acknowledge:486 takes it under coordinator lock:
never add the reverse wait. U6 project → receipt lock has no coordinator
callback. M5 must admit authority before testing old-lock contention.

## Source-informed budgets

Budgets include code + tests + reserve (bytes). Suffixes are separate units;
remeasure each delta. Prerequisites block dispatch; no U3 edit authorized here.

| Unit | Source family / split | Code+tests+reserve (1k=1,000 bytes) |
|---|---|---:|
| U4.1 | R6 submit/R4 above; thin adapters after P/R4/R8 | 6k + 14k + 4k = 24k |
| U4.2a | M:current_intent:395 / M:cancel_before_request:493; preparation | 7k + 14k + 3k = 24k |
| U4.2v | V:run_merge_preconditions:768 / V:run_pinned:1202; local verifier | 8k + 15k + 4k = 27k |
| U4.2r | RV:run_remote:1413 / RV:worker:539 / RV:supervise:624; remote launch prerequisite | 8k + 16k + 4k = 28k |
| U4.2c | RV:cleanup_slurm_locked:839 / RV:cleanup:1012 / RV:finish_empty_stage:1138; cleanup/retry | 7k + 16k + 4k = 27k |
| U4.3 | M:reconcile:632,647,692 / S:cmd_merge:4656; request/record, tracker U6 | 7k + 17k + 4k = 28k |
| U4.4a | S:_submit:1716,1795,1826; code/home/worktree | 7k + 15k + 4k = 26k |
| U4.4b | S:_submit:1572,1614; scheduler/pipeline | 6k + 16k + 4k = 26k |
| U4.4c | S:advance:2092,2912,2937,2950,3060; state/allocation/claims/outbox/cleanup | 8k + 16k + 4k = 28k |
| U4.4d | S:_start_code_terminal_watchers:3401 / S:advance:2503,2694; watcher/resume | 6k + 16k + 4k = 26k |
| U4.5 | S:cmd_advance:3669 / S:cmd_watch_code_terminal:3477; shared/direct routes | 3k + 10k + 3k = 16k |
| U4.6 | SC:continue_plan:148 / SP:_seed_reachability_problem:848,877; preview/fetch/publish | 6k + 14k + 4k = 24k |
| U4.7 | S:cmd_stop_unit:3749,3758,3759; abandonment/outbox | 4k + 12k + 4k = 20k |
| U4.8 | S:cmd_stop_unit:3737,3741; supersession binding | 3k + 11k + 4k = 18k |
| U4.9a | d5_submission.py:submit:113,136; cross-command M1/M2/M3/M6, U4.0b oracle | 2k + 20k + 3k = 25k |
| U4.9m4 | M:reconcile:692 / S:cmd_merge:4656 / d5_control.py:revoke:55; M4/M5 barriers | 2k + 17k + 3k = 22k |

Retain U4.0b oracle/old contention tests. Guard proves pinned membership and
enumerated AST sinks, not general call-flow/semantics. Missing base object fails:
`git fetch origin 276e76af05a83846ea9c45ae7d030cf11a2e4e94`, rerun; no fallback.
