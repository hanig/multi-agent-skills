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
AW=attempt_worktrees, WT=arc725_terminal_watcher; baseline routes belong to U4.0b. Tests are future D5 routes. IDs = citation/line; comma lists
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

S:cmd_run:3646 calls advance; S:cmd_advance:3669 delegates.

| Evidence / ID | Contract | Test |
|---|---|---|
| S:_prepare_command_paths:3520 | I/path. coordinator_paths.py:migrate_legacy_defaults:249,250,262 copies/publishes. | coordinator_paths |
| S:cmd_stop_unit:3706,3769; M:verify_integration:735,815; S:cmd_run:3621,3656 | L/coordinator. | SST |
| S:cmd_run:3644,3645,3653,3654 refresh/start/save; S:_start_code_terminal_watchers:3381,3387,3401,3409,3416 | I/watch + C/status. | WT |
| S:advance:2073,2115,2131,2169,2193,2215,2221,2228,2241,2263,2285,2295,2303,2310,2345,2350,2362,2375,2399,2416,2529,2696,2797,2803,2826,2861,2916,2939,3011,3024,3046,3049,3113 | I+C/state; split initiating decisions from bound completion facts at EACH save. SS:save_state:350 `U.write_json`. | swarm_state, swarm_evidence |
| S:advance:2092,2101,2798; S:advance:2112 | I/preserve/remove. SW:_preserve_and_archive_code_worktree:464,499 Git remove/Paseo archive; SW:_cleanup_codex_home:306 `CH.remove`. | arc682_recovery, swarm_workspaces |
| S:advance:2195,2265; S:advance:2223,2296,2351 | C/recovery (binding required). SW:_recover_code_launch:155 `paseo inspect`; SD:_bind:279 `unit.py bind`. | swarm_code_launch, swarm_dispatch |
| S:advance:2912; SD:_allocate:38 | I/allocation. unit.py:cmd_allocate:924,955,958 mkdir/spec/event. | SDT |
| S:advance:2338,2957 | I/dispatch (each retry). | AW |
| S:advance:2436,2438 | I/checker; C/receipt. swarm_evidence.py:_check:858 owns checker process. | swarm_evidence, pre_dispatch_artifacts |
| S:advance:2829,2950,2966,3030 release/take/note output claims | L/claim + I/namespace. SP:_take_output_claims:378,406,407,421 mkdir/rename/write; SP:_drop_claims:432 and SP:_release_output_claims:448 remove; SP:_note_claimed_attempt:463 writes. | dispatch_claims |
| S:advance:2503; swarm_provider_resume.py:maybe_resume:108,116,134,144 snapshot/save/send/save; S:advance:2694; S:maybe_continue:1976,1977,1984 save/send/save | I/send + C/ack (resend is new I). | continuation |
| S:advance:3060,3140 backfill/emit; S:cmd_stop_unit:3759 emit | I/outbox. SO:emit_intent:611,646 sequence/append; | swarm_outbox |
| S:_submit:1440,1451 capture/write launch; S:_submit:1487,1510,1541,1684,1713,1723,1734,1737,1804,1834,1856,1863,1879,1892,1896 | I/preparation; C/launch. | swarm_code_launch |
| S:_submit:1527 renders only (SD:_isolation_submission:184 returns); S:_submit:1530 | R/render + I/marker. SD:_clear_isolation_marker:206 unlink. | SDT |
| S:_submit:1564,1569,1572 | I/script/sbatch; C/job ID. | swarm_dispatch |
| S:_submit:1594,1614,1626 | I/log/engine; C/launch. | swarm_dispatch |
| S:_submit:1716; S:_submit:1795,1809,1816; S:_submit:1826 | I/worktree/home/Paseo; C/handoff. SCL:_create_code_worktree:256 `worktree add`; codex_home.py:allocate:116 mkdir, codex_home.py:populate:151,155 symlink, codex_home.py:remove:182 rmtree. | attempt_worktrees, codex_home |
| S:cmd_continue:3532; SC:_plan_file:104 | L/plan inode. | continue_command |
| SC:_continuation:77,81 and S:cmd_validate:3565 | I/Git metadata. SP:_seed_reachability_problem:848,877 `fetch`, `update-ref -d`; split from publication; preview also fetches. | validate_seeds |
| SC:continue_plan:148; SC:_append:121,126,130 write/replace/unlink | I/plan. | continue_command |
| S:cmd_stop_unit:3749,3758 bump/save; S:cmd_stop_unit:3759 emit | I/decision/outbox. | abandon_supersede |
| S:advance:2223,2296,2351,3032 | C/dispatch; record launch ID; split. | SDT |
| S:advance:2387; SS:renew_lease:243 | R/coordinator; inspect inode, no write. | SST |
| S:advance:2937; swarm_evidence.py:_capture_artifact_basis:539 populates dict | I/state; digest, save at S:advance:2939. | pre_dispatch_artifacts |
| S:cmd_watch_code_terminal:3453,3477 | R/wait; I/advance child owns effects; split. | WT |
| S:_submit:1665; S:advance:2322; M:verify_integration:775 | R/caller; show-ref/PR view → observation. | MVT |
| S:cmd_merge:4635,4654 | R/recorder; stderr diagnostics, no admission. | merge_unit |
| S:cmd_merge:4656 | C/merge child; M requests, child journals; retain lease/lifetime in Finish. | baseline record |

## Merge effects

| Evidence / ID | Contract | Test |
|---|---|---|
| M:main:984,990 | L/operator. | merge_unit contention |
| M:current_intent:431,432,435,436,475 write/unlink | I/intent repair. | merge_unit recovery |
| M:abandon:486,488 | I/sidecar/resolve. | baseline merge-abandon |
| M:reconcile:632 | I/intent. | baseline merge |
| M:cancel_before_request:502,507,511,515,516 write/unlink | C/cancellation if bound. | baseline merge-cancel |
| M:reconcile:564,573,608,612,616,629,637,648,654 | R/operator; forge observations → state. | merge_unit |
| M:reconcile:647 | I/squash; C/result. | baseline merge-ok/fail/pending |
| M:reconcile:686,692,706,708 write/receipt child/save/write | C/merge recorder if bound. | baseline merge/record |
| M:main:997 | I/advance. | merge_unit |
| M:main:1001,1002 drain/audit; M:print_tracker_drain:886 | I/drain (U6) + R/audit. | merge_unit tracker |
| M:verify_integration:724; M:verify_integration:808,810 append/acknowledge | I/verifier + C/evidence. | MVT |


## Verifier and remote verifier expansion

Owners: v local verifier, rv connected operator, rw worker/supervisor, rc cleanup.
Transport and child operations are distinct; no nested R6 under START. Transactions include fsync/replace/unlink; timeout kill ends that child.
Retries/later claims/preparation/recovery are I; bound factual evidence is C.

| Evidence / ID | Contract | Test |
|---|---|---|
| V:_git:98 | R/v Git; caller owns admission | verify |
| V:_isolated_git:133 | R/v isolated Git; caller owns admission | IV |
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
| RV:__call__:126 | R/rv Git; caller owns mutation | RVT |
| RV:_command:175 | I/rw command → status | RVT |
| RV:_git:182 | I/rw Git; caller owns mutation | RVT |
| RV:make_bundle:189,193,194 | I/rv commit-tree/update-ref/bundle; split | RVT |
| RV:publish:215,217,220,225,228,233 | C/rv publish (I preparation); atomic durable write | RVT |
| RV:publish_launch_witness:248,249,254 | I/rv durable prelaunch witness | RVT |
| RV:binding_lock:350,353 | L/rv flock → close | RVT |
| RV:acknowledge:494 | C/rv launch/digest ack | RVT |
| RV:worker:545,559,563,564,565,567,568,574,575,582,584,589,590 | I/rw once-marker/tree/init/fetch-bundle/checkout/add/write-tree/file/verifier; C receipt/result/final; split | RVT |
| RV:scheduler_state:598 | R/rc sacct → terminal row | RVT |
| RV:supervise:635,638,647 | I/rw script/sbatch; C job-id; R polling; direct calls worker | RVT |
| RV:publish_cleanup:723 | C/rc audit; I cancellation intent | RVT |
| RV:discover_slurm_jobs:750 | R/rc sacct/squeue → identities | RVT |
| RV:cleanup_slurm_locked:850,860,927,929,936,966,973,984,1002 | L/rc external then stage; I scancel/removal/lock unlink; C snapshots; split | RVT |
| RV:cleanup:1049,1088,1090,1097,1114 | L/rc stage; I scancel/removal; C snapshots; split, including legacy job path | RVT |
| RV:finish_empty_stage:1149,1174 | L/rc external; I rmdir after digest ack; first call R | RVT |
| RV:remote_call:1278 | I/rv cleanup SSH; R frame/probe; child owns mutation | RVT |
| RV:read_remote_record:1322 | R/rv string replacement/frame reads | RVT |
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
| LS:project_lock:823,824,826 | L/drain process; mkdir/open/flock → close on exit; retain outside per-issue boundaries. | linear_drain |
| LS:drain_locked:1009; LS:confirm_comment:761 | I/U6 comment create → readback at LS:confirm_comment:769; split every intent. | linear_drain |
| LS:drain_locked:1030; LS:reconcile_issue:927,935 | R/U6 query 927; I/U6 state 935 → readback at LS:reconcile_issue:938; separate from comment. | linear_drain |
| LS:drain_locked:1065; LS:record_drain_receipt:895,897,898,899 | C/U6 confirmed mutation; temp intent/observation/reconcile child → exit/cleanup. | linear_drain |
| DC:record_reconciliation:115,124; SO:record_receipt:1016 | C/reconcile child; receipt THEN reconciliation → durable ack. | drain_contract |
| SO:_append_receipt_atomic:912,914,917,923,952,954 | L/receipt inode; C/append/fsync after duplicate check. | outbox |
| LA:request:127; LA:transport:101 | I/U6 mutation or R/query; HTTP → response; split submit/wait, no retry. | linear_drain |

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
returns by kind. M:reconcile:560 returns dry; forge observations are R (table), unlike M:reconcile:647. Dry merge can still
construct a candidate through V:candidate_merge_basis:670,674; R is not zero I/O.

**P:** d5_registry.py:d5_active:68 is False, but import resolves siblings/changes
sys.path (d5_registry.py:module:55,57; skill_paths.py:loaded_skill_root:40,45).
Cold probe permits source discovery, rejects storage/process/network/lock effects
and registry access. Missing siblings refuse. **Blocker P:** U3 errata (12,000,
tests included) for the SAME predicate without discovery, or approved M6 amendment;
no alternate flag.

**R4:** d5_completion.py:bind:73,76,77 accepts ledger handles; complete only calls
ledger under DRAIN (d5_completion.py:complete:52,53,54; d5_effect_log.py:complete:313).
No callback. R6 bind/Finish/complete: d5_submission.py:submit:124,136,144, AFTER
Start. **Blocker R4:** U3 errata/amendment (24,000 with tests) binding the real
S:cmd_merge:4656 journal before Start and settling child ownership. Inactive
keeps old recorder. M4: success → revoke returned → recorder released; D5 active,
journal persists, next initiation denied. d5_control.py:revoke:55 does not
deactivate; D5 receipt alone is insufficient. d5_completion.py:uncertain:63 and
recorder failure retain existing reconciliation.

R6 d5_submission.py:submit:91,97 holds DRAIN; d5_submission.py:submit:103,105,106,110,113
START/admit/prepare/recheck/Start; Finish outside START. Sole release gate
H/S/(not A or N or F+C) at d5_submission.py:submit:147,148; ambient START refused
at d5_submission.py:submit:92,93. R5 d5_admission.py:_validate_locked:80,86,87,90,98,101
freshly checks registration/mandate/store/identity/scope/time. R3
d5_install.py:_installation:63 orders DRAIN then START. R9 delegates:
d5_lease.py:grant:69,72,73; d5_custody.py:claim:33; d5_custody.py:relinquish:40.

**R8:** d5_process.py:worker_spawn:93,95: argv/pipes, no protected fds; drain/reap.
Allowlist empty (d5_process.py:module:33); protected_spawn refuses undesignated
and ambient START (d5_process.py:protected_spawn:73,80,86). Child owns admission
(d5_effect_child.py:bootstrap:50), so no protected_spawn inside Boundary.start.
R10 leaves designations to U4–U7. S:_submit:1572 sbatch is argv-compatible,
handoff unproved; S:_submit:1614 and S:_start_code_terminal_watchers:3401 lack
cwd/env/log/session support; S:_submit:1826 and S:maybe_continue:1977 need env/timeouts.
**Blockers:** R8a options/async launcher (24,000); R8b bootstrap outside START,
one owner/fd/recording lifetime (28,000); U4.2r remote authority. Synthetic != M2.

Locks: coordinator → DRAIN → START → STATE; Finish retains DRAIN then journal
flock (swarm_journal.py:_fsync_append:60,75,80). Lease != authority. M:main:990
releases before M:main:997; M:verify_integration:724 runs unlocked, reacquires
at M:verify_integration:735. RV:run_remote:1425 holds binding lock across waits;
RV:acknowledge:486 takes it under coordinator: forbid reverse wait. U6 project →
receipt lock, no coordinator callback. M5 requires otherwise valid authority.

## Source-informed budgets

Separate units; totals include tests/reserve. Remeasure. Prerequisites block
dispatch; no U3 edits here.

| Unit | Source family / split | Budget (×1,000 bytes, tests included) |
|---|---|---:|
| U4.1 | R6 submit/R4 above; thin adapters after P/R4/R8 | 24 |
| U4.2a | M:current_intent:395 / M:cancel_before_request:493; preparation | 24 |
| U4.2v | V:run_merge_preconditions:768 / V:run_pinned:1202; local verifier | 27 |
| U4.2r | RV:run_remote:1413 / RV:worker:539 / RV:supervise:624; remote launch prerequisite | 28 |
| U4.2c | RV:cleanup_slurm_locked:839 / RV:cleanup:1012 / RV:finish_empty_stage:1138; cleanup/retry | 27 |
| U4.3 | M:reconcile:632,647,692 / S:cmd_merge:4656; request/record, tracker U6 | 28 |
| U4.4a | S:_submit:1716,1795,1826; code/home/worktree | 26 |
| U4.4b | S:_submit:1572,1614; scheduler/pipeline | 26 |
| U4.4c | S:advance:2092,2912,2937,2950,3060; state/allocation/claims/outbox/cleanup | 28 |
| U4.4d | S:_start_code_terminal_watchers:3401 / S:advance:2503,2694; watcher/resume | 26 |
| U4.5 | S:cmd_advance:3669 / S:cmd_watch_code_terminal:3477; shared/direct routes | 16 |
| U4.6 | SC:continue_plan:148 / SP:_seed_reachability_problem:848,877; preview/fetch/publish | 24 |
| U4.7 | S:cmd_stop_unit:3749,3758,3759; abandonment/outbox | 20 |
| U4.8 | S:cmd_stop_unit:3737,3741; supersession binding | 18 |
| U4.9a | d5_submission.py:submit:113,136; cross-command M1/M2/M3/M6, U4.0b oracle | 25 |
| U4.9m4 | M:reconcile:692 / S:cmd_merge:4656 / d5_control.py:revoke:55; M4/M5 barriers | 22 |

Retain U4.0b oracle/locks. Guard pins citation sites/AST sinks, not semantics.
Missing base object fails:
`git fetch origin 276e76af05a83846ea9c45ae7d030cf11a2e4e94`, rerun; no fallback.
