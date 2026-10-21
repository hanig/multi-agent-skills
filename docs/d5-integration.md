<!-- docs-truth:status -->
Status: live
Date: 2026-10-20
Title: D5 integration preparation
Purpose: Record inactive integration, registration and host contracts.

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
| Native ID | session_id alone reads CLAUDE_CODE_SESSION_ID (D5-OD4 custody half) | No custody established |
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
DG8 (2026-10-20) places authority outside every operated worktree at
`coordinator_paths._external_base(worktrees) / "hanig-authority" / "v1"`.
The Git common directory holds neither authority nor a pointer to it.
`d5_registry.py` resolves the swarm sibling through `skill_paths`; it has no
CLI or production caller. Registration keys are the opened Git common
directory's `(st_dev, st_ino)`, exact `plan_id` matching
`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`, and the opened external state directory's
`(st_dev, st_ino)`. Paths are locators only; plan IDs are never path components.
The registry rejects conflicting plan/state bindings, re-verifies state
identity on lookup, and permits two plans in one repository. Aliases of one
filesystem object share a registration; clones and recreated inodes do not.

`registry.json` is atomically published under permanent `registry.lock`;
record directories live in `plans/<random-registration-id>/`.
`register` refuses with "D5 is not active" until U8 changes `d5_active()`.
Read-only `lookup` creates nothing, even with no authority root. The
`grant`, `revoke` and `status` management API is deferred to U1b-2.
Supported: local filesystem, trusted same-UID coordinator writers, Git 2.31+.
No claim of path-string or cross-clone equivalence, persistence across inode
recreation, protection from malicious same-UID replacement, or TOCTOU against
outside-lock writers. Custody, enforcement and activation remain later units.
