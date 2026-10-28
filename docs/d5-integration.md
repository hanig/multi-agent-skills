<!-- docs-truth:status -->
Status: live
Date: 2026-10-28
Title: D5 integration and narrowed activation closure
Purpose: Bind the merged U4.9 guarantees to tests, explicit non-claims and activation prerequisites.

**D5 remains inactive.** U4.9a (PR #310) and U4.9c (PR #313) landed;
U4.9b did not land (ARC-4171-OD2/OD3). Its admission, option injection,
review-gate activation copy and route-inventory drift guard remain preserved
at `recovery/swarm-d105d6d1b345fb7f`, not part of the shipped guarantee.

The following table is the complete U4.9 guarantee list. Its exact claims,
mechanism names and test IDs must agree with `u49_guarantees` in the
[manifest](../tests/fixtures/d5/integration-manifest.json).
`tests.test_d5_u49_activation_closure` checks that agreement, resolves the
named definitions and runs the cited behavior tests. This establishes the
named coverage, not a proof of arbitrary call-flow closure. Active scenarios
use synthetic activation; merge-child tests explicitly force admission.
No production activation is performed by these tests.

## U4.9 guarantees

| ID | Guarantee | Mechanisms | Tests |
|---|---|---|---|
| merge-child-containment | The designated merge child uses shell=False and close_fds=True, receives no authority descriptors, and filters the declared D5/session/provider/tracker credential names while retaining the declared forge credentials. Arbitrary commands and undesignated extra switches refuse. | `merge_unit.py:_merge_child_env`<br>`merge_unit.py:_MergeBoundary.start` | `tests.test_d5_containment.TestMergeChild.test_actual_child_environment_and_no_descriptors_or_provider_tracker`<br>`tests.test_d5_containment.TestMergeChild.test_arbitrary_executable_or_extra_switch_refuses_before_launch` |
| direct-root-refusals | With D5 active, unit.py allocate/bind/check and remote_verify.py worker/supervise refuse before effects, with readiness absent or installed. | `unit.py:main`<br>`remote_verify.py:main` | `tests.test_d5_containment.TestDirectRoots.test_direct_clis_both_readiness_states_before_effects` |
| merge-admission-switch | merge_launch_admitted() is False until owner activation at U8; active merge entries refuse before effects even with readiness installed. | `merge_unit.py:merge_launch_admitted`<br>`merge_unit.py:_d5_merge_launch_entry`<br>`merge_unit.py:_active_entry_lease` | `tests.test_d5_containment.TestMergeAdmission.test_production_merge_launch_is_not_admitted`<br>`tests.test_d5_containment.TestMergeAdmission.test_active_entries_refuse_before_any_effect_without_forced_admission` |
| unavailable-audit | With D5 active, the post-merge tracker audit prints UNAVAILABLE without launching or importing linear_sync and without audit writes or transport. | `merge_unit.py:print_tracker_audit` | `tests.test_d5_containment.TestMergeAudit.test_active_audit_unavailable_without_dependencies_or_effects` |
| entry-point-routing | swarm.py cmd_run, cmd_advance and main use shared d5_activation routing; merge_unit.py main retains its admission refusal. The named inactive captures remain equal to their recorded baselines. | `swarm.py:cmd_run`<br>`swarm.py:cmd_advance`<br>`swarm.py:main`<br>`merge_unit.py:main`<br>`d5_activation.py:d5_active` | `tests.test_d5_u49_entrypoints.TestRoutes.test_cmd_run`<br>`tests.test_d5_u49_entrypoints.TestRoutes.test_cmd_advance`<br>`tests.test_d5_u49_entrypoints.TestRoutes.test_swarm_main`<br>`tests.test_d5_u49_entrypoints.TestRoutes.test_merge_main`<br>`tests.test_d5_u49_entrypoints.TestRoutes.test_manifest_routes_and_shared_activation_owner`<br>`tests.test_d5_u49_entrypoints.TestInactive.test_m6_bound_to_base`<br>`tests.test_d5_containment.TestDirectRoots.test_inactive_matches_recorded_merge_base` |
| bytecode-advisory | The swarm entries warn when D5 is active and sys.dont_write_bytecode is false. The advisory neither changes that flag nor probes the filesystem, and diagnostic failures never block schema. | `swarm.py:_d5_bytecode_advisory` | `tests.test_d5_u49_entrypoints.TestAdvisory.test_warning_truth_table_at_swarm_entries`<br>`tests.test_d5_u49_entrypoints.TestAdvisory.test_read_only_and_diagnostic_failure_never_blocks`<br>`tests.test_d5_u49_entrypoints.TestAdvisory.test_missing_stderr_preserves_schema_stdout`<br>`tests.test_d5_u49_entrypoints.TestAdvisory.test_unexpected_stderr_errors_never_gate_schema` |

## Explicit non-claims

- No process-level isolation or same-UID isolation (OD1b). Child argument,
  environment and descriptor containment is a narrower claim.
- No bytecode suppression or stale-cache behaviour guarantee. The advisory
  reads an interpreter flag; it does not inspect caches or enforce host
  temporary-directory readiness. U8 may enforce or waive that precondition.
- No repository-wide quiet or OD4 closure; effects outside the named entries
  are not covered by this closure test.
- No non-canonical guard coverage.
- No `linear_sync` mutator direct-root refusals (ARC-3766-OD1a).
- No route-inventory drift guard (ARC-4171-OD2). The owner checks the inventory
  by hand at the U8 activation review. The guarantee-table consistency check
  does not inventory newly added production routes.
- No review or committee admission or option injection under D5. U4.9b did
  not land; U8 must decide these before activation. The review-gate activation
  copy is likewise unlanded. Existing parser parity is not admission coverage.

The [stale activation bytecode entry](deferred-edge-cases.md#stale-activation-bytecode-honoured-while-the-activation-source-is-inactive)
records a deferral against preserved, unmerged U4.9b work, not shipped admission.

## Carried items

- OD5's caller-held lease precondition: U5 (lease lifecycle).
- Authorized `gh` executable binding and integrity: U7. The existing command
  shape check does not establish executable identity.
- Changing `merge_launch_admitted()` from False: U8, owner only, after the
  activation review and its prerequisite decisions. This unit grants no activation.

## Earlier integration preparation

The reference below describes earlier inactive preparation contracts and their
limits. It does not extend the U4.9 activation guarantees above.

**D5 is inactive; preparation only.** [Manifest](../tests/fixtures/d5/integration-manifest.json)
classes cite handlers: reads=read_only, files/state=local_mutation,
dispatch/merge/tracker/spend=outward (strongest mode). Unresolved verifier
internals block dependents.

| Check | Claim | Non-claim |
|---|---|---|
| Parity | Real parsers; both directions/all depths; continue --write | Not every flag |
| Classes | Closed set; source rationale; key classes pinned | No exhaustive helper-effect proof |
| Boundaries | Module-level def/async def exists | nothing checks that it is called or reachable. |
| Ancestry | Pid/ppid/start only; leaf first to topmost reachable; three-record and captured fixtures | Not atomic |
| Hops | parent.start_time <= child.start_time; equality allowed; inversion suggests PID reuse | Nothing beyond a break is trusted |
| Identity | (pid, kernel microsecond start time); reuse differs | EPERM/failure unknown, never dead |
| Native ID | session_id alone reads CLAUDE_CODE_SESSION_ID; required, no fallback; every verify_anchor rechecks ID and identity | No custody established |
| Anchor | CLAUDE_PID: strict ASCII PID, strict ancestor, same real UID; identity from ancestry. Liveness: same start alive, changed start or ESRCH dead, otherwise unknown | Trusted same-UID environment can be forged; zombies count as alive; ancestry is not atomic |
| Inactivity | Scope/merge refusal | No import scanner |
| Mandate | U8 baseline below | No Markdown parser |

Cut applied by the orchestrator on 2026-10-20 (DG7): host recognition and
`select_session_ancestor` move to their first consumer, U2. Inputs are the
round-4 empty-argv[0] counterexample (argv[1] `claude` misread as argv0), the
`KERN_PROCARGS2` NUL-padding ambiguity, and the probe showing `p_comm` is the
versioned binary name `2.1.300`. U0 contains no host-name rule or argument
reader. [Fixtures](../tests/fixtures/d5/host-chains.json) retain the two real
captures and their provenance with the command column removed.

Committee **d5-u0-stepback**, 2026-10-20, NARROW_SCOPE/CONVERGED (DG4-DG6):
U0-M3 moves to U8; U0-M5 uses scope/merge refusal. U8 mandate baseline blob
`9491614c6ae2d7ca570b89c4ccb451f4c81f272f` at base
`136269424f1426f34e9afa0b227483e294a65d35`.

Locks: coordinator -> journal; merge releases before advance reacquires;
drain: project -> receipt; continuation: plan; review releases round before
spend, journals use exclusive directories; committee: none. Verification:
release coordinator, binding across SSH, reacquire; cleanup: sibling -> stage.
No TTL. Jobs/agents/engines/watchers outlive dispatch; requests may finish
after disconnect.
Worker: swarm_code_protocol._code_completion_protocol -> loaded review.py,
complete range/author exclusion. Only review is a worker outward CLI; no D5 permit.

Outbox: key/project/unit/verb/seq, closing_evidence/kind/unit_state/why/at,
job_id/attempt_dir/evidence, optional tracker. Envelope: schema_version,
project/unit/attempt/idempotency_key/requested_operation/evidence_digest/
required_connector_capability. No D5 grant/destination scope.
DG8 (2026-10-20) places authority at
`coordinator_paths._external_base(worktrees) / "hanig-authority" / "v1"`.
The Git common directory holds neither authority nor a pointer to it.
`d5_registry.py` resolves the swarm sibling through `skill_paths`.
Registration keys are the opened Git common directory's `(st_dev, st_ino)`,
exact `plan_id` matching `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`, and the opened
external state directory's `(st_dev, st_ino)`. Paths are locators only; plan
IDs are never path components. The registry rejects conflicting plan/state
bindings, re-verifies state identity on lookup, and permits two plans in one
repository. Aliases of one filesystem object share a registration; clones
and recreated inodes do not (inode reuse is an accepted limit).

`registry.json` is atomically published under permanent `registry.lock`;
records live in `plans/<random-registration-id>/`. `lookup` reads a snapshot
without creating a lock. `d5_lease.py` provides `grant`, `revoke` and `status`
through public registry, record and store APIs; it has no CLI or production
caller. Grant validates before writing, requires a registration, assigns a
fresh grant ID and session cap 1, digests exact mandate bytes, and stores
owner response and displayed bounds without summarizing or trimming them.
Custody and invalidation start null. Creation and replacement use the store's
locking, consent rules and predecessor archive. Two initial grants that both
observe no record select `store.create`; one refuses. Grant mutations also
hold a permanent `grant.lock` with a nonblocking exclusive lock, so a caller
seeing a newly published record while its writer still holds the store lock
refuses instead of waiting to replace it. Sequential replacement stays allowed.
Revocation requires registration and uses the store's terminal transition.

`register`, `grant` and `revoke` refuse with "D5 is not active" until U8 changes
`d5_registry.d5_active()`. Read-only `status` works while inactive and creates
nothing: no external base, authority root, registry, record directory or lock.
It reads bytes, decodes and evaluates the record without the store's locking
`read` or invalidation-persisting `check`. The returned activation state,
registration, record and record state are diagnostic, never authorization;
a reported mandate change does not persist invalidation.

Supported envelope: local filesystem with trusted same-UID coordinator
writers, Git 2.36 or later and the standard repository layout. Placement
containment and the Git/layout gates belong to U1b-1c (ARC-2794); tamper checks
belong to U1b-1b (ARC-2793), both before activation. No claim of path-string or
cross-clone equivalence, persistence across inode recreation, protection from
malicious same-UID replacement, or TOCTOU against outside-lock writers.
`d5_custody.py` adds inert `claim`, `relinquish` and diagnostic `status` APIs,
with no CLI or production caller. Claim and relinquish require activation,
registration and a verified native session anchor; an Unknown anchor refuses
with its reason and has no fallback. Under the permanent record lock, claim
evaluates the grant and durably invalidates a changed mandate before refusing.
Only a valid grant can be claimed. Initial custody has generation 1; the exact
same session ID and process incarnation continue without a write. A different
session or anchor requires the stored holder's death and increments generation
without a new grant or owner confirmation. Liveness is three-valued: alive,
dead or unknown; exceptions and unexpected results count as unknown. A zombie
anchor counts as alive, and persistent anchors may delay rollover.

Relinquishment retains the holder and its first relinquishment timestamp,
disables that holder's further claims, and permits no live handoff. Grant
replacement carries custody unchanged, including relinquishment; caller-supplied
custody that differs refuses. There is no TTL, heartbeat or time-based takeover.
Custody status reads bytes and decodes without locking, evaluation or writes,
even while inactive. Its activation state, registration, custody, holder
liveness and caller identity match are diagnostic, never authorization. A match
can remain true for a relinquished holder or an invalidated or expired grant.

Cryptographic owner authentication, enforcement, a CLI and activation remain
later units. D5 stays inactive.
