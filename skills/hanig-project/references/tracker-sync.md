# Tracker synchronization details

The generated declarations in `../SKILL.md` decide behavior. This reference
explains the coordinator and connector boundary. <!-- declaration: placement.reference-elaboration -->

## Capability boundary

The coordinator drafts tracker work without network access. If the active
session has no real connector, it must preserve the reviewed draft or outbox <!-- declaration: capability.tracker -->
intent, report the pending synchronization to the owner, and must not invent a <!-- declaration: capability.tracker -->
remote result. <!-- declaration: capability.tracker -->
Tracker credentials and remote operations stay in the authorized connector
session, never on the shared login-node coordinator. <!-- declaration: tracker.credential-boundary -->

## Approval and creation

The owner sees the project, every issue title, and the count before one named
approval. No project or issue may be created while approval remains required. <!-- declaration: tracker.approval -->

After approval, create the project before its issues and persist each returned
id and identifier. Later drafts must update the objects keyed by unit id rather <!-- declaration: tracker.apply -->
than duplicate them. <!-- declaration: tracker.apply -->

## BlockedBy reconciliation

The connector's blockedBy relation is append-only unless `removeBlockedBy` is <!-- declaration: tracker.edges -->
called. Omitting an old edge from a new add list does not remove it. <!-- declaration: tracker.edges -->

Apply every declared addition and removal, then read every issue's blockedBy
relations back into the next draft. <!-- declaration: tracker.edges -->

Each key represents the blocked issue and may use a unit id, tracker identifier, <!-- declaration: tracker.readback-shape -->
or UUID. An issue observed with no blockers must appear with an empty list; <!-- declaration: tracker.readback-shape -->
omitting it is indistinguishable from not looking. Without a read-back,
`remove_blocked_by` must remain `null`, and after filing `check` treats that <!-- declaration: tracker.readback-shape -->
unknown state as drift. <!-- declaration: tracker.readback-shape -->

The loop is apply, read, re-draft, and check. The read-back is attested because
the coordinator receives the connector session's report rather than making the
network read itself. Synchronization must not be claimed without its read time <!-- declaration: tracker.attestation -->
and source. <!-- declaration: tracker.attestation -->

## Outbox acknowledgments

Unit state remains authoritative when tracker access fails. A tracker outage
must never mutate swarm state. <!-- declaration: drain.authority -->

Apply each pending intent through the connector before marking it applied. <!-- declaration: outbox.receipt -->
Record a receipt only after the connector confirms the tracker write. Missing <!-- declaration: outbox.receipt -->
receipt state means `unacknowledged`, not that no filing occurred; repeat drains
are safe because intents carry idempotency keys. <!-- declaration: outbox.receipt -->
