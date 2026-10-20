<!-- docs-truth:status -->
Status: live
Date: 2026-10-20
Title: D5 integration preparation
Purpose: Record inactive integration contracts.

**D5 is not active.**
The [manifest](../tests/fixtures/d5/integration-manifest.json) pins parsers and
source functions; unresolved evidence blocks dependents.

Locks: coordinator → journal; merge releases before advance reacquires;
drain then takes project → receipt. Continuation locks the plan. Review releases
round locking before spend; journals use exclusive directories. Committee has
no session lock. Verification releases coordinator, takes binding across SSH,
then reacquires to publish. Remote cleanup: sibling → stage. No lease TTL.

Jobs/agents/engines/watchers outlive dispatch; requests can finish after disconnect.
Watchers advance/resume; merge advances/drains; SSH submits/cancels.

Worker path: `swarm_code_protocol._code_completion_protocol` → loaded review.py,
complete range, author exclusion. Only review is a worker outward CLI; no D5
permit.

Outbox fields: key/project/unit/verb/seq, closing_evidence/kind/unit_state/why/at,
job_id/attempt_dir/evidence, optional tracker. Envelope: schema_version,
project/unit/attempt/idempotency_key/requested_operation/evidence_digest/
required_connector_capability. D5 grant/destination scope is absent.

Recommend a separate Git-common-directory authority namespace.
Independent installs ship identical `skill_paths.py` copies with an equality
guard: the packaging precedent. U1 decides.
