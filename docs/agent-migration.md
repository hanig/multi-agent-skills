# Migrating a skill installation between host agents

The installer manages a maintained skill set for Claude Code, Codex, OpenCode,
and Pi.  Treat it as the source of truth for its own managed records; do not
copy skill directories between agent stores by hand.

## Before changing anything

Run the diagnostic command in read-only JSON mode and save its result.  It
identifies selected targets, resolved roots, managed skills and their
provenance, conflicts, and anything it cannot safely remove.  An absent host
is reported as absent; it is never silently interpreted as a different host.

```sh
./bin/doctor --json
./install.sh --dry-run --json
```

Use explicit target selectors for migrations.  The default target selection is
convenient for a first install, but a migration should declare both the source
and destination so the command's payload can be reviewed.  See the current
target and path matrix in `docs/agent-compatibility.md`.

## Copy, link, and shared roots

Copy mode is the portable default.  It installs a snapshot with managed
provenance, so it remains runnable after the checkout is moved or removed.
Link mode is for development only: an installed skill changes whenever its
checkout changes, and the checkout must remain available.

Two agent targets can resolve to the same root.  That is shared visibility, not
an instruction to duplicate or delete a directory.  Review the diagnostic
payload before proceeding, then let the installer retain a single managed
record.  A selective uninstall removes only the selected target's ownership;
it must leave a skill still visible through another target intact.

## Legacy Claude prefix installs

`--prefix` remains a legacy Claude-compatible spelling for an explicit skill
root.  It is intentionally narrower than agent selection: use it when you
must preserve a script or a pre-existing custom Claude layout, and use explicit
host selectors for new cross-agent installs.  Do not combine a legacy prefix
with a broad target request unless the diagnostic output confirms the resulting
root and overlap policy.

Existing ownership rules still apply.  Foreign same-name skills are not
overwritten or removed without the explicit takeover mechanism, and vendored
skills remain protected from a normal uninstall.  The provenance record, not a
directory name or symlink shape, decides whether the installer owns a skill.

## Upgrade, recovery, and removal

1. Run the install command in dry-run mode and inspect the target, path,
   provenance, permission, conflict, and action payloads.  A re-run over an
   owned skill is the public upgrade operation; its action is reported as
   `upgraded`, not as a fresh install.
2. Run the same selected install in copy mode for a durable snapshot, then run
   diagnostics again to confirm the recorded version and health.
3. If an interruption or simulated failure is reported, rerun diagnostics
   first.  Recovery must either remove its own incomplete staging data or
   restore the previous managed version; it must not remove foreign skills.
4. Use selective uninstall for one host.  Use the broad uninstall only after
   diagnostics show every selected target and shared-root consequence.

Migration itself is deliberately planning-only.  Name one destination and a
legacy source, then keep `--dry-run`; the command emits a `migration-plan`
payload and never deletes the legacy content:

```sh
./install.sh --agent codex --migrate-from "$HOME/.codex/skills" --dry-run --json
```

## Verifying a move

Run one deterministic bundled workflow from a project directory unrelated to
the source checkout.  For a copy install, repeat after making the source
checkout unavailable.  A successful installer invocation alone proves only
file deployment; record native discovery and one representative invocation on
the destination host before declaring the migration complete.

See `docs/cross-agent-acceptance.md` for the release evidence record and the
distinction between hermetic command coverage and live host-agent proof.
