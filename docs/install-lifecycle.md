# Skill installation lifecycle API

`lib.skill_lifecycle` is the mutation layer for the multi-store installer. It does not discover `$HOME`, infer loader precedence, or select skills. The CLI must resolve every destination, selected name, and consumer before calling it.

## Caller contract

Build one `LifecycleTarget` per selected payload:

```python
LifecycleTarget(
    name="hanig-swarm",
    source=checkout / "skills" / "hanig-swarm",
    destination=home / ".claude" / "skills" / "hanig-swarm",
    origin="authored",  # or "vendored"
    consumers=("claude",), mode="copy", source_version="8ac8992",
)
```

Call `preflight(targets)` for dry-run output, then `install(targets)` to make changes. A target is blocked when its source is invalid or its destination is present without a matching `repo=multi-agent-skills` record, unless the caller explicitly sets `allow_foreign_replace`. The module validates `SKILL.md`; the installer should retain its richer frontmatter/script validation before it constructs targets.

Each copied payload contains `.installed-by-multi-agent-skills`; a link has a deterministic adjacent sidecar under `.multi-agent-skills-provenance`. Schema 2 records `repo`, `origin`, `source_version`/compatibility `version`, absolute `destination`, comma-separated `consumers`, `mode`, and timestamp. Legacy markers remain readable but have `origin=unknown`; they are never silently treated as authored and are not destructively uninstalled.

## Failure and concurrency semantics

`install()` first stages and validates every selected target on its destination filesystem. It moves an existing destination to a unique backup only after its replacement is staged; a failed publish restores that backup, or reports its recoverable path if restoration also fails. Stage/backup names include UUIDs; an advisory `flock` serializes lifecycle writers for each destination on macOS and Linux.

This is intentionally **not a multi-root atomic transaction**. After staging, each root is published independently and the returned `InstallResult` list is the accurate partial-result record. A caller must print those individual statuses and may safely retry blocked/failed destinations; it must not report an all-or-nothing upgrade.

## Uninstall and prune integration

Pass only candidate paths to `uninstall()`. It checks both repository identity and recorded destination before deletion. With `consumers=(...)`, it detaches only those consumers and retains a shared payload while other recorded consumers remain. The CLI should say that this preserves the payload but cannot guarantee what an independent loader chooses to make visible; precedence stays loader-specific.

Vendored records require `include_vendored=True`; unknown legacy origins are retained. Foreign directories, a foreign/dangling symlink, and unrecorded metadata are blocked rather than inferred as ownership. Missing source checkouts do not prevent uninstall because the record is at the destination.

## Migration planning

`legacy_roots(home)` names the supported user-level candidates: `~/.claude/skills`, `~/.codex/skills`, and `~/.config/codex/skills`. `plan_migration()` is dry-run only: the caller supplies legacy roots, `selected_names`, and a destination resolver, then receives `ready`, `already-present`, or `invalid-source` items. It never deletes a legacy root or same-name payload. When the CLI later executes a `ready` item it must copy, validate, and verify the new destination through `install()` before offering any separate, explicit removal of the old copy.
