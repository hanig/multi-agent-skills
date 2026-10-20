<!-- docs-truth:status -->
Status: live
Date: 2026-10-20
Title: D5 integration preparation
Purpose: Record inactive integration and host contracts.

**D5 is inactive; preparation only.** [Manifest](../tests/fixtures/d5/integration-manifest.json)
classes cite handlers: reads=read_only, files/state=local_mutation,
dispatch/merge/tracker/spend=outward (strongest mode). Unresolved verifier
internals block dependents.

| Check | Claim | Non-claim |
|---|---|---|
| Parity | Real parsers; both directions/all depths; continue --write | Not every flag |
| Classes | Closed set; source rationale; key classes pinned | No exhaustive helper-effect proof |
| Boundaries | Module-level def/async def exists | nothing checks that it is called or reachable. |
| Ancestry | Leaf first to topmost reachable; three-record fixture | Not atomic |
| Hops | parent.start_time <= child.start_time; equality allowed; inversion suggests PID reuse | Nothing beyond a break is trusted |
| Selection | Outermost host within unbroken leaf-first prefix, else None | a session started from inside another session's tool call binds to the outer session's process. |
| Identity | (pid, kernel microsecond start time); reuse differs | EPERM/failure unknown, never dead |
| Native ID | session_id alone reads CLAUDE_CODE_SESSION_ID (D5-OD4 custody half) | Not selection input or custody |
| Inactivity | Scope/merge refusal | No import scanner |
| Mandate | U8 baseline below | No Markdown parser |

Host: argv[0] basename exactly `claude`, from
sysctl KERN_PROCARGS2 `(CTL_KERN,49,pid)`: argc, exec path, argv. Unreadable
arguments identify no host. Never p_comm: the 2026-10-20 probe found `2.1.300`
versus argv[0]/exec basename `claude` ([kernel source](https://raw.githubusercontent.com/apple-oss-distributions/xnu/main/bsd/kern/kern_sysctl.c)).
[Fixtures](../tests/fixtures/d5/host-chains.json) retain capture provenance;
nested cases derived (live auto-approved tools forbidden); no identifying paths.

Committee **d5-u0-stepback**, 2026-10-20, NARROW_SCOPE/CONVERGED (DG4-DG6):
U0-M3 moves to U8; U0-M5 uses scope/merge refusal. U8 mandate baseline blob
`9491614c6ae2d7ca570b89c4ccb451f4c81f272f` at base
`084001f14c7ada705b9dc1a8ba1a4789087b2d2a`.

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
Recommend Git-common-directory authority; equal shipped skill_paths.py copies
are the independent-install precedent. U1 decides.
