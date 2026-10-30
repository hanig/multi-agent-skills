<!-- docs-truth:status -->
Status: live
Date: 2026-10-22
Title: D5 integration preparation
Purpose: Record inactive integration, registration, lease, custody and host contracts.

**D5 activates per state directory at the owner's `d5_install` publish,
not at merge.** U8 makes activation installation-derived. A directory without
installation witnesses keeps its legacy behavior. READY and BROKEN installations
are active; BROKEN refuses with `d5_install` repair guidance. Merge launch remains closed. Existing confirmations are
not leases. See [the cutover runbook](d5-cutover-runbook.md) for the measured
tracker gaps and [the mandate](orchestrator-mandate.md) for bounded authority.
A same-UID operator invoking `unit.py` or `remote_verify.py` directly without
`--state-dir` is not D5-contained. Coordinator-mediated calls supply that path;
remote staging checks it on the originating host and carries it as audit data.

The preparation measurements below remain historical evidence, not a claim
of complete enforcement.

 [Manifest](../tests/fixtures/d5/integration-manifest.json)
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
refuses instead of waiting to replace it. The public grant API requires a later
confirmation timestamp for each replacement, including unchanged bounds;
blank confirmation fields refuse. Exact nonblank receipt bytes are retained.
Revocation requires registration and uses the store's terminal transition.

`register`, `grant` and `revoke` now reach their prepared checks through the
shared active predicate. Explicit inactive fixtures still exercise their
"D5 is not active" refusal. Read-only `status` works while inactive and creates
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

Cryptographic owner authentication and a management CLI are not delivered.
Confirmation contents remain trusted caller evidence, not authenticated owner
identity. Activation does not supply those missing capabilities.
