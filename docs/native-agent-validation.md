# Native agent validation

- Date: 2026-09-05
- Integration under test: `2414d8a80e23ea8ae4e5638c65696b7a86f9f570`
- Representative skill: `hanig-portable-handoff`
- Verdict: **macOS native discovery passed; cross-host/invocation evidence remains incomplete**

## Result

On the available macOS host, the combined installer made exactly two confined
physical writes for the four requested agents. Claude Code 2.1.261 and Codex
0.153.4 came from the host; official pinned OpenCode 1.18.29 and Pi 0.73.1
packages came from a disposable npm prefix. All four gated native loaders found
the installed skill, and the safe native harness exited zero with no observed
failures. The installed Python helper also started from a separate cwd, but
that is only payload execution and is not native agent invocation.

The full macOS/Linux release evidence remains incomplete. No Linux target was
authorized and no authenticated LLM-driven skill execution was run. The
coordinator explicitly directed this worker not to assume the broken local
Docker context was usable and not to use SSH or remote infrastructure.

## Evidence classes

- **Native discovery** means the installed host loader parsed/listed the skill.
- **Command resolution** means a native skill command was accepted far enough
  to reach an intentionally unauthenticated model boundary.
- **Standalone script execution** means a copied helper ran directly under
  Python; it proves payload integrity, not agent behavior.
- **LLM-driven invocation** means an authenticated model selected or executed
  the skill. No such observation was made in this credentialless validation.

Keeping these classes separate follows the repository test strategy: fast
installer/lifecycle tests cover deterministic filesystem behavior, native
loader checks cover host integration, and neither substitutes for a real
model-driven invocation.

## Host inventory

| Property | Observation |
|---|---|
| OS | macOS 26.6.2, build 25G83; Darwin 25.6.0 |
| Process architecture | `x86_64` process on Apple M1 Max |
| Python | 3.9.6 |
| Node | v26.7.0 |
| Claude Code | 2.1.261 at `/Users/hani/.local/bin/claude` |
| Codex CLI | 0.153.4 at `/Users/hani/miniforge3/bin/codex` |
| Host OpenCode | 1.15.12 at `/Users/hani/miniforge3/bin/opencode` |
| Host Pi | not in `PATH` |
| Disposable OpenCode | official npm `opencode-ai@1.18.29` |
| Disposable Pi | official npm `@mariozechner/pi-coding-agent@0.73.1` |

The OpenCode executable is an x86_64 build. Every invocation warned that the
CPU lacks AVX support and recommended a baseline Bun build. Its isolated
`--version` probe took 2.013-2.539 seconds in repeated observations, exceeding
the installer's fixed 2.0-second discovery deadline; consequently the combined
installer honestly reported OpenCode as `undetermined` even when explicitly
selected.

## macOS/Linux matrix

| Agent | Gated version | macOS native discovery | macOS representative invocation | Linux evidence | Minimal missing requirement |
|---|---:|---|---|---|---|
| Claude Code | 2.1.261 | **Pass.** Native debug trace loaded one user skill from isolated `CLAUDE_CONFIG_DIR`; zero tokens and $0. | **Not passed.** Direct `/hanig-portable-handoff` resolved to the API boundary, then the invalid test key returned 401 with zero tokens and $0. | Not observed. | For actual invocation, a coordinator-owned test account/model and explicit spend authorization. For Linux, a disposable local Linux host exposing 2.1.261 in `PATH`. |
| Codex | 0.153.4 | **Pass.** App-server `skills/list` returned the installed skill as `scope=user`, `enabled=true`, with no loader errors. | Not run; `skills/list` is discovery, not a model turn. | Not observed. | A coordinator-owned authenticated test model for invocation; a disposable local Linux host exposing 0.153.4 for Linux discovery. |
| OpenCode | 1.18.29 | **Pass.** Official pinned 1.18.29 `opencode debug skill --pure` found the exact installed `.agents` copy. Host 1.15.12 was recorded separately and is not the gate evidence. | Not run; `debug skill` is discovery, not a provider/model turn. | Not observed. | A disposable local Linux host exposing 1.18.29 (baseline build if needed); configured test provider/model only if actual invocation is required. |
| Pi | 0.73.1 | **Pass.** Official pinned 0.73.1 `DefaultResourceLoader.reload()`/`getSkills()` returned the exact installed `.agents` copy with no diagnostics. | Not run; native SDK discovery starts no model. | Not observed. | A disposable local Linux host with the importable 0.73.1 package; a configured test model only if `/skill:hanig-portable-handoff` execution is required. |

For every Linux row, the exact bounded rerun requirement is a coordinator-
approved **local disposable Linux** environment with checkout commit
`2414d8a80e23ea8ae4e5638c65696b7a86f9f570`, Python 3, Git, and the versioned
agent binaries on `PATH`. Official pinned npm packages may be placed in a
temporary prefix as below, then the harness runs unchanged. No credential
variables, external container, SSH target, global install, or user skill store
is authorized by this report.

## Pinned disposable package provenance

Registry metadata came from `https://registry.npmjs.org/` with
`NPM_CONFIG_USERCONFIG=/dev/null`, a scratch-only npm cache, audit/funding
disabled, and no auth variables. The exact releases were:

| Package | Version | Registry integrity | Tarball |
|---|---:|---|---|
| `opencode-ai` | 1.18.29 | `sha512-syIDVwlrYTgTOXzZe9SkInJWethbq6l3SNC762UeXyO0a9V0wGfd+U4yACvppwNBnhIsl0j2QPYYCyLpNaSomg==` | `https://registry.npmjs.org/opencode-ai/-/opencode-ai-1.18.29.tgz` |
| `@mariozechner/pi-coding-agent` | 0.73.1 | `sha512-gXQh3SaZmWTfVMc4Ao5+LGbVeKvzyO7tolok0nLsZgq9nGjZx/EEU3NM8C+qUnB4Nvs2rswG5qOVgLzQkq0fHQ==` | `https://registry.npmjs.org/@mariozechner/pi-coding-agent/-/pi-coding-agent-0.73.1.tgz` |

The disposable install command was equivalent to:

```sh
HOME="$PACKAGE_SCRATCH/home" \
NPM_CONFIG_USERCONFIG=/dev/null \
NPM_CONFIG_CACHE="$PACKAGE_SCRATCH/npm-cache" \
NPM_CONFIG_REGISTRY=https://registry.npmjs.org/ \
NPM_CONFIG_AUDIT=false NPM_CONFIG_FUND=false \
npm install --prefix "$PACKAGE_SCRATCH/prefix" \
  --no-save --package-lock=false \
  opencode-ai@1.18.29 @mariozechner/pi-coding-agent@0.73.1

PATH="$PACKAGE_SCRATCH/prefix/node_modules/.bin:$PATH" \
python3 tests/native_agent_validation.py
```

Installed manifests repeated the exact package names and versions. Both
`opencode --version` and `pi --version` returned the pinned versions. The
outer package prefix and the harness's inner agent homes were separate
temporary directories and both were deleted on exit; nothing was installed
globally.

## Isolation and installer observation

The harness uses Python `TemporaryDirectory` as the canonical scratch root and
sets all of the following below it:

```text
HOME
XDG_CONFIG_HOME
CODEX_HOME
CLAUDE_CONFIG_DIR
PI_CODING_AGENT_DIR
TMPDIR
CLAUDE_CODE_TMPDIR
```

Child environments are allowlisted rather than inherited, so personal API
keys, OAuth tokens, SSH agents, and other credential variables are absent. The
Claude discovery subprocess receives only a known-invalid test key. The
harness preserves the repository cwd and runs native loaders from a separate
empty workspace directory. The temporary tree is deleted when the harness
exits.

The exact installer command was:

```sh
./install.sh \
  --agent claude --agent codex --agent opencode --agent pi \
  --only hanig-portable-handoff --json
```

It returned zero, no conflicts, and these two actions:

1. `CLAUDE_CONFIG_DIR/skills/hanig-portable-handoff`, registered for Claude.
2. `HOME/.agents/skills/hanig-portable-handoff`, registered for Codex,
   OpenCode, and Pi.

Both roots resolved below scratch. This confirms the combined plan's intended
de-duplication: the shared `.agents` copy covers Codex, OpenCode, and Pi rather
than publishing three identical copies.

## Native observations

### Claude Code 2.1.261

Authoritative local help says `CLAUDE_CONFIG_DIR` relocates the config root and
that skills can be invoked by `/skill-name`. The public documentation agrees:

- <https://code.claude.com/docs/en/claude-directory>
- <https://code.claude.com/docs/en/env-vars>
- <https://code.claude.com/docs/en/slash-commands>

The reusable discovery check runs a local command, not the representative skill
body:

```sh
ANTHROPIC_API_KEY=<deliberately-invalid-test-key> \
CLAUDE_CONFIG_DIR="$SCRATCH/claude" \
claude --no-session-persistence --permission-prompts none \
  --setting-sources user --strict-mcp-config \
  --mcp-config '{"mcpServers":{}}' \
  --debug skills --debug-file "$SCRATCH/tmp/claude-skills.log" \
  --output-format json --print /help
```

The invalid key takes precedence over OAuth/keychain authentication. Native
debug output named the isolated user root, reported `Loaded 1 unique skills`
with `user: 1`, and reported `getSkills returning: 1 skill dir commands`.
The JSON result recorded zero input/output/cache tokens and `$0` cost.

A bounded direct resolution probe replaced `/help` with
`/hanig-portable-handoff`. The native loader again reported one user skill and
advanced to an API request; the deliberately invalid key received HTTP 401.
The result recorded zero tokens and `$0`. This is command-resolution evidence,
not successful LLM-driven skill execution. As a negative control under the
same normal-loader configuration,
`/definitely-not-a-native-validation-skill` returned `Unknown command` locally,
made no model request, and also recorded zero tokens and `$0`.

`--bare` is not a valid discovery substitute on this build. Although its help
says skills still resolve, the observed debug trace said `[reduced mode]
Skipping skill dir discovery`, returned zero skill-directory commands, and
reported `Unknown command: /hanig-portable-handoff`. The report does not turn
that contradiction into a pass.

### Codex CLI 0.153.4

Local `codex app-server generate-json-schema --experimental` and
`generate-ts --experimental` emitted the authoritative installed protocol. It
contains `skills/list`, whose parameters accept `cwds` and `forceReload`, and a
response containing skill metadata and loader errors. The upstream app-server
documentation is at:

- <https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md>

The exact protocol sequence was:

```text
codex app-server --listen stdio://
-> initialize(clientInfo, experimentalApi=true, requestAttestation=false)
<- initialize result: codexHome=$SCRATCH/codex, platformOs=macos
-> initialized
-> skills/list(cwds=[$SCRATCH/workspace], forceReload=true)
```

The response contained one `hanig-portable-handoff` entry at the installed
`HOME/.agents/skills/.../SKILL.md` path with `scope: user`, `enabled: true`, and
`errors: []`. App-server analytics default to disabled and this operation did
not require login or a model.

### OpenCode 1.18.29 (pinned disposable release)

OpenCode exposes a native list-only surface in local help:

```sh
HOME="$SCRATCH/home" XDG_CONFIG_HOME="$SCRATCH/xdg" \
OPENCODE_PURE=1 OPENCODE_DISABLE_PROJECT_CONFIG=1 \
OPENCODE_DISABLE_DEFAULT_PLUGINS=1 \
opencode debug skill --pure
```

It returned JSON containing `hanig-portable-handoff` at the exact installed
`HOME/.agents/skills/.../SKILL.md` path. Database migration, logs, locks,
cache, and config files stayed inside scratch. Its package manifest and
`--version` both reported 1.18.29. The official discovery locations include
the shared global `.agents` root:

- <https://opencode.ai/docs/skills>

The same check also passed on host-installed 1.15.12, but that earlier result
is only compatibility context. It is not the version-gated evidence above.

### Pi 0.73.1 (pinned disposable release)

Pi was absent from the host `PATH`, so the official 0.73.1 npm release was
placed in the disposable package prefix. The version-pinned upstream
documentation states that Pi loads `~/.agents/skills/`, scans directories
containing `SKILL.md`, and registers skills as `/skill:name`:

- <https://github.com/badlogic/pi-mono/blob/v0.73.1/packages/coding-agent/docs/skills.md>

The harness located the installed package entry point from the `pi` executable,
verified the package manifest version, then ran the native SDK loader with
extensions, prompts, themes, and context files disabled. Under the disposable
`HOME`/`PI_CODING_AGENT_DIR`, `DefaultResourceLoader.reload()` followed by
`getSkills()` returned exactly one `hanig-portable-handoff` at the installed
shared path and no diagnostics. This needed no provider credential or model. A
`/skill:name` execution still needs a configured model and remains a separate
gate.

## Standalone payload execution

After installation, this command ran from `$SCRATCH/workspace` and returned
zero:

```sh
python3 "$SCRATCH/home/.agents/skills/hanig-portable-handoff/scripts/handoff.py" --help
```

Its output listed `capture`, `resume`, and `memory`. This proves that the copied
helper starts independently of the checkout. It does **not** prove that any
agent loaded, chose, or invoked the skill.

## Reusable harness

Run:

```sh
python3 tests/native_agent_validation.py
```

The harness emits a JSON record and uses these exit codes:

- `0`: all four version and native-discovery checks passed on that host; this
  still does not prove an LLM-driven invocation.
- `1`: a check that could run contradicted expectations or failed.
- `2`: bounded checks ran, but required evidence was unavailable/incomplete.

The 2026-09-05 host-only macOS run exited `2` with no observed check failures:
host OpenCode was the wrong version and Pi was absent. With the official pinned
OpenCode/Pi package prefix first in `PATH`, the same harness exited `0`; all
four version gates, all four native discovery checks, and standalone payload
execution passed with no observed failures. `actual_llm_driven_invocation`
remained `not_run` in both records.

The harness is suitable for a credentialless GitHub Actions Linux job without
code changes: provision the same exact agent versions into job-local paths,
put them first in `PATH`, and run it. That is a future route, not evidence from
this macOS run.

A deterministic local mock provider could test per-agent prompt/tool plumbing
without paid credentials, but it would require separate protocol adapters (for
example an Anthropic-compatible endpoint for Claude and local-provider routes
for Codex/OpenCode/Pi). Such a mock would prove only that a scripted synthetic
response receives skill context and issues a tool call; it would not prove
ordinary LLM skill selection. Building that cross-agent provider harness was
out of scope and would not close a real-model invocation gate.

## Release blockers

1. Run the harness on the coordinator-approved local disposable Linux host for
   all four gated versions.
2. If the release gate requires actual invocation, use explicitly authorized
   test accounts/models and record model/provider, prompt, loaded skill path,
   response, tokens, and cost. No worker-owned or personal credential may be
   reused for this.

The macOS native-discovery gate is complete. Until the remaining observations
exist, this is not a cross-host release or merge pass.
