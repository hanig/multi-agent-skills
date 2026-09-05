# ARC-274 independent installer review

Reviewed `origin/main...2414d8a80e23ea8ae4e5638c65696b7a86f9f570` on
2026-09-05. This was a review-only pass over `lib/skill_installer.py`,
`lib/skill_lifecycle.py`, discovery/diagnostics, and their cross-agent
interfaces. No implementation changes, real user skill stores, credentials,
agent sessions, paid models, pushes, PRs, or merges were used.

## Result

**Do not merge.** Two reproducible P1 destructive path defects and five P2
correctness/safety defects remain. All reproductions used `TemporaryDirectory`
roots and explicit disposable `HOME`/`PATH` values.

## Findings

### P1 — `--only` can escape an absent uninstall root and delete an owned payload elsewhere

**Lines:** `lib/skill_installer.py:133`, `lib/skill_installer.py:550-560`,
`lib/skill_installer.py:576-591`, `lib/skill_installer.py:430-435`, and
`lib/skill_lifecycle.py:33-35,565-603`.

`--only` values are not required to be a single skill-name component. When the
selected root does not exist, line 559 joins each raw value to the root. A value
such as `../victim` or an absolute path therefore escapes the selected root.
Lifecycle `_absolute()` normalizes the traversal and `uninstall()` removes the
escaped destination when it has valid repository provenance. The installer then
raises `StopIteration` in `_action()` because the normalized result parent no
longer equals any planned root, so deletion occurs before the user receives a
structured result.

Disposable reproduction:

1. Install a lifecycle-owned fixture at `$BASE/victim`.
2. Leave `$BASE/missing-root` absent.
3. Run:

   ```sh
   HOME="$BASE/home" PATH=/usr/bin:/bin ./install.sh \
     --prefix "$BASE/missing-root" --uninstall --only ../victim --json
   ```

Observed: exit 1 with `StopIteration` at `skill_installer.py:432`, while
`$BASE/victim` no longer exists.

**Impact:** a destructive command scoped to one prefix can delete a valid
managed skill outside that prefix. This violates the CLI's explicit target
boundary and returns no deletion receipt.

**Proposed fix:** validate every `--only` argument at parse time as a non-empty
single component (`Path(name).name == name`, excluding `.`/`..`, separators,
absolute paths, and NUL). Before calling lifecycle, prove every candidate's
canonical parent is the selected canonical root. Make `_action()` robust to a
normalized path mismatch, but do not treat that reporting guard as the safety
fix. Add live and dry-run regression tests for `../x`, absolute paths, absent
roots, symlinked roots, and zero writes/deletes on rejection.

### P1 — a symlinked `--prefix` bypasses source containment and link mode destroys the source

**Lines:** `lib/skill_installer.py:381-384,405-426` and
`lib/skill_lifecycle.py:33-35,378-395,410-467` (especially line 441).

The installer compares lexical absolute paths with `commonpath()`, while
lifecycle deliberately does not resolve the final symlink. A prefix symlink
whose target is the source skill's parent therefore passes the guard. With an
authored skill, `--force --mode link` classifies the source itself as a foreign
destination, moves it to a backup, creates a link at the source path pointing to
that same source path, and deletes the backup.

Disposable reproduction shape:

```text
$BASE/repo/skills/hanig-demo/SKILL.md     # source
$BASE/prefix -> $BASE/repo/skills         # requested --prefix
./install.sh --prefix $BASE/prefix --mode link --force --only hanig-demo
```

The bounded API-level reproduction constructed the same public plan. Initial
preflight returned `upgrade`; install returned `upgraded`; afterward
`hanig-demo` was a self-referential symlink and its original `SKILL.md` was
inaccessible. Everything was under a temporary tree.

**Impact:** an explicitly supported prefix alias can destroy checkout source
content. Lexical aliases also derive different advisory-lock names, so they can
bypass the intended per-destination serialization.

**Proposed fix:** enforce the containment invariant in lifecycle as well as the
CLI. Resolve existing parents/canonical targets before staging, reject canonical
source/destination equality and destinations inside a source regardless of
`--force`, and use one canonical destination identity for duplicate detection
and locking. Add copy/link tests for direct, parent-symlink, final-symlink, and
nonexistent-final-component aliases.

### P2 — explicit foreign replacement imports untrusted consumer ownership

**Lines:** `lib/skill_lifecycle.py:319-348,351-361,378-391`.

Preflight correctly labels a same-repository marker with a mismatched recorded
destination as foreign and requires `allow_foreign_replace`. However,
`_record_for()` unions the old consumers whenever only the repository string
matches. It does not require `_owned_by()` to have succeeded.

Disposable reproduction: a foreign destination contained a schema-2 marker
with `repo=multi-agent-skills`, a different `destination=...`, and
`consumers=pi`. Replacing it with a target whose sole selected consumer was
`claude` returned `upgraded`, but the new valid record contained
`('claude', 'pi')`. Uninstalling for Claude then returned `retained-shared` and
left the payload for the never-selected phantom Pi consumer.

**Impact:** takeover of a foreign/malformed same-repository record can strand
payloads and corrupt selective ownership. Similar stale records can carry
arbitrary consumer registrations into a new install.

**Proposed fix:** carry an explicit `previous_owned` decision from preflight and
merge consumers only when exact prior ownership, including destination and link
identity, was proven. A foreign takeover must create provenance solely from the
new target. Test mismatched destination, replaced link identity, stale sidecar,
and foreign mode cases.

### P2 — link-to-copy upgrades orphan a sidecar and block a later link install

**Lines:** `lib/skill_lifecycle.py:200-209,335-339,423-451,552-603`.

`_publish()` backs up/removes a sidecar only when the *new* target mode is
`link`. An owned `link -> copy` upgrade publishes a directory marker but leaves
the old link sidecar. Uninstall sees the directory-local marker first, removes
the copy, and never removes that sidecar. A later fresh link preflight sees an
absent destination plus the stale sidecar and blocks.

Observed sequence in a disposable root:

```text
link install       -> installed
link -> copy       -> upgraded
sidecar after copy -> exists
copy uninstall     -> removed
sidecar afterward  -> exists
fresh link         -> blocked: link provenance sidecar exists without this repository's ownership record
```

**Impact:** a supported mode transition leaves installer-owned metadata that
permanently prevents reinstall until a user manually edits the store.

**Proposed fix:** make sidecar transition handling depend on both previous and
new mode. Back up a proven predecessor link sidecar for any upgrade, restore it
on failed publication, and remove it after a successful copy publication. Add
`link -> copy -> uninstall -> link` and injected rollback tests.

### P2 — target flag order changes placement, and the installer drops a known duplicate-visibility conflict

**Lines:** `skills/hanig-project/scripts/agent_discovery.py:472-533` and
`lib/skill_installer.py:241-273,523-536,638-644`.

Selection only lets a previously selected destination cover a later agent.
Consequently, explicit `codex,opencode` produces one `.agents/skills` write,
while `opencode,codex` produces both OpenCode's native root and `.agents/skills`.
Discovery reports the latter as `competing_visibility`, because OpenCode sees
both copies. `run()` passes the selection to `build_discovery_plan()`, which
discards that field, and the installer JSON returns success with an empty
`conflicts` list and no diagnostic.

Disposable end-to-end dry run for `--agent opencode --agent codex --dry-run
--json` returned code 0 and actions under both roots, with `conflicts=[]` and
only the generic dry-run diagnostic. Reversing the two flags returned one root.

**Impact:** semantically identical agent sets have order-dependent filesystem
topology, and the user is not shown the loader collision already detected by the
authoritative planner. The two copies can later drift or be upgraded/uninstalled
independently.

**Proposed fix:** make destination selection deterministic for an agent set
(preserving requested display order separately), and propagate
`competing_visibility` into the installer document. Require an explicit
acknowledgment before writing a known duplicate, or otherwise define and test a
deterministic precedence policy.

### P2 — frontmatter validation accepts definitions known to be malformed

**Lines:** `lib/skill_installer.py:330-378`.

`validate_payload()` only checks that the file starts with `---\n` and that any
line starts with `name:`. In disposable fixtures it accepted all of:

- frontmatter with no closing `---` delimiter;
- `name:` with an empty value; and
- directory `alpha` with `name: beta`.

Those sources can then be staged and installed even though the README promises
frontmatter validation. Diagnostics intentionally reports only file presence,
and native loading remains unverified, so no later local gate proves the
installed definition is loadable or has the intended identity.

**Impact:** a malformed repository payload can be published successfully and
silently fail or collide under agent loaders.

**Proposed fix:** parse exactly bounded YAML frontmatter, require a mapping with
a non-empty string `name`, require the closing delimiter, and require
`name == path.name` (plus any cross-agent required fields such as a non-empty
description). Test duplicate keys, invalid YAML, empty/scalar documents,
unterminated delimiters, and name mismatch.

### P2 — diagnostics can report provenance valid when lifecycle rejects the same bytes

**Lines:** `skills/hanig-project/scripts/agent_diagnostics.py:43-60,77-119`
versus `lib/skill_lifecycle.py:154-197`.

Diagnostics decodes metadata with UTF-8 replacement, while lifecycle uses
strict UTF-8 and returns no ownership on `UnicodeError`. A temporary schema-2
copy marker with otherwise valid fields and one trailing `0xff` byte produced:

```text
diagnostic ownership       = owned
diagnostic provenance      = valid
lifecycle read_provenance  = None
```

**Impact:** doctor/survey automation can claim a payload is validly owned even
though upgrade and uninstall treat it as foreign. This breaks the documented
single provenance contract and can misdirect recovery.

**Proposed fix:** share the parser where packaging permits, or mirror its strict
UTF-8 and field validation exactly. Add cross-module corpus tests asserting the
same ownership verdict for malformed encodings, duplicate fields, invalid
consumer values, invalid modes/origins, and incomplete records.

## Review dimensions

| Dimension | Rating | Notes |
| --- | --- | --- |
| Security / destructive safety | **Fail** | Two P1 target-boundary/source-destruction repros. |
| Correctness / concurrency / rollback | **Fail** | Stale transition metadata, foreign consumer inheritance, and alias lock identity. |
| Cross-agent provenance / discovery | **Fail** | Known duplicate visibility is order-dependent and dropped; diagnostics disagrees with lifecycle. |
| Performance | No P1/P2 found | Probes and reads are bounded; no material hot-path issue found in scope. |
| Maintainability / test coverage | **Needs work** | Safety decisions are duplicated across installer, lifecycle, and diagnostics without shared conformance tests. |

Positive observations: publication is staged, copy payloads are revalidated,
link ownership binds to link identity, ordinary same-path concurrent changes are
fingerprinted under advisory locks, foreign destinations are blocked by default,
selective uninstall retains proven shared consumers, discovery probes have
bounded output/time, and diagnostics explicitly keeps native loading unverified.
These controls are worthwhile but do not cover the reproduced paths above.

## Validation and gaps

I ran seven bounded disposable reproductions: uninstall traversal, prefix
symlink/source alias, foreign consumer inheritance, link/copy transition,
selection-order/visibility behavior, malformed frontmatter, and diagnostic
encoding disagreement. I also inspected the focused existing tests and current
acceptance record. Per assignment, I did **not** run the full suite; the parent
coordinator owns regression.

No native agent invocation was performed. The repository's own native release
record remains unverified for every listed agent/OS combination (macOS Claude
and Codex have version-probe evidence only; OpenCode/Pi and all Linux rows lack
even that complete evidence). That missing evidence is a release gap, not a
pass, and none of the hermetic reproductions changes it.

## Implementation follow-up (2026-09-05)

The assigned implementation follow-up resolves the two P1s and the lifecycle,
frontmatter, and public duplicate-visibility P2s in focused commits:

| Review finding | Resolution | Evidence |
| --- | --- | --- |
| `--only` uninstall path escape | Resolved in `9123802` | Path-like, absolute, dot-prefixed, separator, empty, and NUL values are rejected before discovery or mutation; live/dry-run copy/link regressions preserve the outside owned fixture. |
| Symlink-prefix source destruction | Resolved in `9123802` | CLI and lifecycle canonicalize source and destination-parent identities, reject overlapping payloads before staging, deduplicate aliases, and never follow a foreign final link during replacement. |
| Foreign takeover imports consumers | Resolved in `c8c3650` | Preflight records exact prior ownership; consumer union occurs only for proven ownership, and a mismatched same-repository record no longer strands a phantom consumer. |
| Link-to-copy sidecar orphan | Resolved in `c8c3650` | Successful transition removes the prior sidecar; injected failure restores and rebinds the old link record; link→copy→uninstall→link succeeds. |
| Order-dependent/dropped duplicate visibility | Resolved across compatibility commit `07c1074`, `c8c3650`, and the independent-verification follow-up | Discovery uses fixed adapter precedence while preserving presentation order; public JSON and diagnostics retain `competing_visibility`. Expected mixed-host exposure proceeds with identical snapshots and a prominent precedence warning, while existing foreign/org collisions still fail closed. Combined public order coverage is in `b94bef5`. |
| Malformed frontmatter accepted | Resolved in `c8c3650` plus the independent-verification follow-up | A 64-KiB-bounded stdlib parser requires delimited unique top-level fields, exact non-empty portable identity/description, and rejects malformed flow/mapping/quote syntax, YAML non-string scalars, and reserved indicators; every shipped bundle passes. |
| Diagnostics/lifecycle encoding disagreement | Resolved across lifecycle commit `c8c3650` and diagnostics follow-up `266cf2f` | Both sides reject invalid UTF-8, duplicate/malformed fields, and incomplete or inconsistent schema-2 records while retaining empty consumers and normalizing consumer order. |

Focused evidence after combining `07c1074` locally:

- 68 installer/lifecycle/cross-agent/capability tests passed in 27.064 seconds;
- 98 discovery/diagnostics/installer/lifecycle/cross-agent tests passed in
  38.513 seconds; and
- 30 older installer/portable compatibility tests initially exposed only a
  logical-versus-canonical prefix display mismatch. The targeted regression
  passed after retaining canonical mutation identity but printing the caller's
  logical spelling in the `doctor --prefix` suggestion.

The parent coordinator still owns a full regression on the integrated tree and
native-loader release evidence. The unchanged `2414d8a` baseline full run was
reported separately as 1,544 tests with two pre-existing failures; that result
is neither evidence for these fixes nor a merge pass.
