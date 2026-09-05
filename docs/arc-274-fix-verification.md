# ARC-274 fix verification

Verified on 2026-09-05 in the disposable Orca worktree rooted at
`arc-274-compatibility-fixes`. No real agent home, user skill store, credential,
agent session, paid model, global installation, push, pull request, or merge was
used.

## Commit ledger

Consumed implementation commits are listed separately from verification-owned
commits:

- discovery/diagnostic implementation owned by this worktree: `07c1074`;
- P1 boundary implementation supplied as `9123802`, cherry-picked here as
  `c531bb8`;
- lifecycle/installer P2 implementation supplied as `c8c3650`, cherry-picked
  here as `b059f0d`;
- narrow diagnostic schema alignment: `266cf2f`;
- verification corpus/report: recorded in the final worker receipt after this
  document and its tests are committed.

## Provenance parser contract and parity

Schema 2 requires `schema`, `repo`, `origin`, `source_version`, compatibility
`version`, absolute `destination`, `consumers`, `mode`, `installed_at`,
`link_target`, and `link_identity`. UTF-8 is strict; nonempty malformed lines,
empty field names, duplicate keys, missing generated fields, mismatched source
versions, unknown origins, invalid modes, relative destinations, malformed
consumer components, and inconsistent copy/link fields are rejected.

The cross-module corpus in `tests/test_agent_diagnostics.py` applies those bytes
to both the copied-skill diagnostic and lifecycle. It also separates structural
parsing from ownership: a structurally valid record with a wrong destination,
link target, or link-object identity remains unowned. Historical schema-2 copy
records with `consumers=` remain valid, while unsorted duplicate consumers are
accepted and normalize to a stable set. Valid link records require an absolute
target and a sidecar identity matching the current link object.

Focused parity gate:

```text
python3 -m unittest tests.test_agent_diagnostics -v
Ran 22 tests ... OK
```

This gate uses only temporary roots. It includes invalid UTF-8, duplicate and
malformed fields, every generated required field other than the schema
discriminator itself, invalid origin/mode, empty and reserved consumer tokens,
copy/link mode identity, wrong link target, and replaced link identity. A marker
without `schema=2` is intentionally handled as historical legacy metadata rather
than mislabeled as a malformed schema-2 record.

## Independent P1 reproduction results

The original absent-root traversal shape was rerun through the public installer
test: an owned `$BASE/victim` was prepared, the selected prefix was absent, and
live plus dry-run uninstall requests used `--only ../victim` in copy and link
modes. Each request was rejected during option parsing, the absent prefix stayed
absent, and the victim bytes were unchanged. The source-alias reproduction was
rerun in both modes with `$BASE/prefix -> $BASE/repo/skills`; source/destination
overlap was rejected before preflight or staging, and the original `SKILL.md`
remained a regular readable file.

The canonical-identity review also checked direct versus parent-symlink paths,
duplicate preflight destinations, and replacement of a final foreign symlink.
Parents resolve to one lock/destination identity while the final symlink itself
is replaced rather than followed.

```text
python3 -m unittest -v \
  tests.test_multi_agent_installer.TestPublicCli.test_only_uninstall_never_escapes_an_absent_prefix \
  tests.test_multi_agent_installer.TestPublicCli.test_prefix_alias_into_source_is_rejected_before_copy_or_link \
  tests.test_install_lifecycle.LifecycleTest.test_source_destination_alias_overlap_is_rejected_before_writes \
  tests.test_install_lifecycle.LifecycleTest.test_destination_parent_aliases_have_one_identity \
  tests.test_install_lifecycle.LifecycleTest.test_foreign_final_symlink_target_is_not_followed_on_replacement
Ran 5 tests ... OK
```

No remaining P1 boundary bypass was found in the reviewed fix. Validation covers
direct paths, existing parent aliases, absent final components, and final
symlinks; lifecycle repeats the overlap invariant so a caller bypassing the CLI
does not regain the destructive path.

## Lifecycle P2 reproduction results

The foreign-takeover reproduction now carries an explicit `previous_owned`
decision from preflight. A same-repository record with the wrong destination no
longer contributes its unproven Pi consumer to a new Claude record. The
link-to-copy sequence now moves and removes the predecessor sidecar, and the
injected publish failure restores both the old link and sidecar, rebinds the
link-object identity, and leaves no sidecar backup.

```text
python3 -m unittest -v \
  tests.test_install_lifecycle.LifecycleTest.test_schema_two_parser_is_strict_but_normalizes_consumer_order \
  tests.test_install_lifecycle.LifecycleTest.test_foreign_replacement_does_not_import_unproven_consumers \
  tests.test_install_lifecycle.LifecycleTest.test_link_to_copy_transition_removes_sidecar_and_allows_fresh_link \
  tests.test_install_lifecycle.LifecycleTest.test_failed_link_to_copy_transition_restores_link_and_sidecar \
  tests.test_install_lifecycle.LifecycleTest.test_source_destination_alias_overlap_is_rejected_before_writes \
  tests.test_install_lifecycle.LifecycleTest.test_destination_parent_aliases_have_one_identity \
  tests.test_multi_agent_installer.TestSelectionBeforeWrites.test_frontmatter_rejects_malformed_or_wrong_identity \
  tests.test_multi_agent_installer.TestPublicCli.test_default_dry_run_selects_all_verified_agents_without_writes \
  tests.test_multi_agent_installer.TestPublicCli.test_duplicate_visibility_blocks_live_writes_without_acknowledgment
Ran 9 tests ... OK
```

## Pending findings at this checkpoint

Two installer-level findings were sent to the coordinator and assigned back to
the implementation owner before this gate can pass:

1. The bounded frontmatter subset currently accepts YAML implicit non-string
   scalars. A disposable directory named `true` with `name: true` and
   `description: null` was accepted even though standard YAML yields a boolean
   and null rather than the promised nonempty strings; reserved invalid plain
   scalar starts have the same class of problem.
2. The consumed installer currently blocks an ordinary all-verified automatic
   plan solely because the expected Claude-root plus `.agents` topology is both
   visible to OpenCode. The result correctly publishes `competing_visibility`,
   but requiring `--allow-duplicate-visibility` conflicts with the confirmed
   default all-detected contract, which treats this deterministic unavoidable
   exposure as warning-only.

Until follow-up commits resolve both findings and the focused tests are rerun,
this document records a **pending / do-not-merge** gate rather than a pass.
