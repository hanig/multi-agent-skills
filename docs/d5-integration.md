<!-- docs-truth:status -->
Status: live
Date: 2026-10-20
Title: D5 integration preparation
Purpose: Record inactive source and host contracts for bounded lease work.

**D5 is not active.** No production caller, mandate or declaration changes.
The [manifest](../tests/fixtures/d5/integration-manifest.json) covers parsers,
source functions, boundaries and lifetimes using named columns and unique source basenames.
Parser/AST checks cannot discover every effect. Unresolved verifier internals
block dependent enforcement until inspected; they receive no exemption.

Lock order: coordinator then journal. Merge releases before advance reacquires;
drain follows under project then receipt locks. Continuation locks the plan.
Review releases its round lock before spending; journal directories use O_EXCL.
Committee has no session lock. Verification releases coordinator, takes binding
across SSH, then reacquires coordinator to publish. Remote cleanup: sibling then
stage lock. `lease.json` is diagnostic; no TTL.

Jobs/agents/engines/watchers survive dispatch; remote requests can finish after
disconnect. Watchers can advance/resume; merge can advance/drain; SSH can submit
and cancel jobs. Each retry is another start, not covered by helper admission.

Worker path: `swarm_code_protocol._code_completion_protocol` → loaded review.py,
complete range, author exclusion. Only review is expected as a worker outward
CLI; no D5 delegation exists. `child_environment` does not isolate daemon keys.
Current narrow mode remains read/survey/plan/gate/report before confirmation.

Outbox: key/project/unit/verb/seq, closing_evidence/kind/unit_state/why/at,
job_id/attempt_dir/evidence, optional tracker. Envelope: schema_version,
project/unit/attempt/idempotency_key/requested_operation/evidence_digest/
required_connector_capability. No D5 grant or canonical destination scope.

Recommend the plan's separate Git-common-directory authority namespace.
Independent installs need a packaging choice: byte-identical shipped
`skill_paths.py` copies with an equality guard are the precedent. U1 decides.

Measured Darwin 25.5.0 arm64 SDK ABI: size 648; start offset 0 (8-byte seconds,
4-byte microseconds); PID 40, parent 560, command 243/17 bytes. The library reads
sysctl, returns Unknown on absence/failure/other platforms, never death.
Synthetic Python → shell → claude → shared shell → multiplexer chains select
only claude. Rechecking is not atomic or spoof resistant; U2 must revalidate.
