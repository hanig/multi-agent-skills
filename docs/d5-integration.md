<!-- docs-truth:status -->
Status: live
Date: 2026-10-20
Title: D5 integration preparation
Purpose: Record inactive source and host contracts for bounded lease work.

**D5 is not active.** No production caller, mandate or declaration changes.
The [manifest](../tests/fixtures/d5/integration-manifest.json) checks parsers and
source functions; it cannot discover every effect.
Unresolved verifier internals block dependent work.

Locks: coordinator → journal; merge releases before advance reacquires;
drain then takes project → receipt. Continuation locks the plan. Review releases
round locking before spend; journals use exclusive directories. Committee has
no session lock. Verification releases coordinator, takes binding across SSH,
then reacquires to publish. Remote cleanup: sibling → stage. No lease TTL.

Jobs/agents/engines/watchers survive dispatch; remote requests may finish after
disconnect. Watchers advance/resume, merge advances/drains, SSH submits/cancels.
Each retry is a fresh start; helper launch is not blanket permission.

Worker path: `swarm_code_protocol._code_completion_protocol` → loaded review.py,
complete range, authors excluded. Only review is a worker outward CLI; no D5
permit exists. `child_environment` does not isolate daemon keys. Narrow mode
remains read/survey/plan/gate/report before confirmation.

Outbox fields: key/project/unit/verb/seq, closing_evidence/kind/unit_state/why/at,
job_id/attempt_dir/evidence, optional tracker. Envelope: schema_version,
project/unit/attempt/idempotency_key/requested_operation/evidence_digest/
required_connector_capability. D5 grant/destination scope is absent.

Recommend the plan's separate Git-common-directory authority namespace.
Independent installs ship identical `skill_paths.py` copies with an equality
guard: the packaging precedent. U1 decides.
