# Agent compatibility and capability contract

This repository distributes one Markdown skill format to several hosts. That
is **loader compatibility**, not a promise that every host has the same tools,
accounts, background-worker backend, or permission policy. This document
records the audit at commit `8ac8992`; it is the support boundary for
user-level macOS and Linux installations.

## Use the host's conventions, then the project context

Install or expose a skill through the current host's normal skill-discovery
mechanism. The existing installer has its own documented placement contract;
it does not provision other hosts, services, or executable helpers. Do not
copy a host configuration, credentials, or permission/model defaults merely
because a skill's Markdown can be loaded elsewhere.

Before acting, read the project instructions that the active host surfaces and
the applicable repository instructions reachable from the worktree. The
practical adapters are intentionally small:

| Host | Skill and instruction adapter | Approval/tool adapter |
|---|---|---|
| Claude | Use Claude's discovered `SKILL.md` and project instructions, including `CLAUDE.md` where it is in scope. | Use only tools actually exposed in this session; obtain its normal approval before an external mutation. |
| Codex | Use Codex's discovered `SKILL.md` and `AGENTS.md`/repository instructions. | Use the session's available file, shell, and connector tools; do not translate another host's tool name into a presumed Codex tool. |
| OpenCode | Use its configured skill sources and discovered `AGENTS.md` instructions. | Respect the active agent's configured permission rules; an unavailable tool is unavailable, not an invitation to weaken a rule. |
| Pi | Use Pi's configured skills and its loaded `AGENTS.md` or `CLAUDE.md` project context. | Use Pi's actual tools and approval flow, and keep an unavailable service as pending work. |

`MEMORY.md` is ordinary project state, not Claude-only state. Any host may
read it, and `hanig-portable-handoff` may refresh only its factual marked block
when its local prerequisites are available. A handoff records paths,
identities, decisions, blockers, and verifier receipts; it never transfers
credentials or silently turns an absent service into a result.

## Capability contract

Ask about a capability at the time it is needed. Presence of a skill, a binary
in `PATH`, or a provider name in configuration is not evidence that an account,
connector, or authority is available.

| Capability | Required for | If unavailable | Honest fallback / setup boundary |
|---|---|---|---|
| Shell and filesystem access | Scripted checks and local artifacts | The host has no usable shell or access is denied | Report the blocked command/path and request the host-approved access; do not fabricate its output. |
| Python 3 and Git | Authored helper scripts and worktree/commit evidence | Either command is absent or unusable in the current worktree | Install or expose the host-approved program, then rerun. Preserve the repository cwd and use a disposable home/config root in tests. |
| Linear connector and authorized account | Reading or mutating Linear | No connector, account, or mutation authority in this session | Keep the ticket intent/outbox artifact reviewable, report **pending synchronization**, and let a session with the real connector apply it. |
| Paseo and agent bus | The optional delegation, fleet, and notification workflows | Binary, daemon, configured state, or local bus is absent | Do the bounded local work without delegation, or ask an operator to install/configure the optional service. Do not create look-alike paths or a daemon. |
| Reviewer providers and coordinator-held credentials | `hanig-review-gate` automated multi-model review | Reviewer configuration or credentials are unavailable | Mark the change **unreviewed** and retain the local evidence; do not borrow/copy credentials or call a paid provider from a worker. |

Python and Git are baseline local prerequisites for their named workflows.
Paseo/agent-bus, reviewer access, and Linear are optional capabilities with
different fallbacks; none is installed or configured by skill installation.
The coordinator/worker boundary remains in force: a worker receives neither
the coordinator's connector authority nor its denied credential environment,
and an agent's provider authentication is not a grant to use another model or
external service.

### Linear operations and outbox proof

Use the connector available to the **current session** and its real operation
names. Before any external tracker mutation, obey the host's approval policy
and the skill's explicit approval gate. Record the connector's actual result
or returned reference only after the operation succeeds; never invent a tool
name, receipt, ticket reference, or read-back.

If Linear is absent, create no substitute tracker state. Retain the existing
draft or outbox intent with its evidence, identify the intended operation, and
report it as pending synchronization. A later authorized session may apply the
intent and record its actual receipt. A ticket is not synchronized merely
because an agent said it was, and a code unit is not closed merely because a
worker settled.

### Delegation is not host support

Claude, Codex, OpenCode, and Pi can load the common Markdown shape subject to
their own discovery rules. That does not mean every one is implemented as a
Paseo worker provider. In particular, OpenCode may coordinate an already
available worker backend, but this repository does **not** add an OpenCode
dispatch implementation. Do not infer a worker backend, credential route, or
approval mode from a host installation.

## Bundle audit matrix

Every bundle below has a directory `SKILL.md` with YAML frontmatter containing
`name` and `description`; its name matches the directory's lowercase
hyphenated identifier. This is the portable loader subset. “All four” means
the Markdown is suitable for Claude, Codex, OpenCode, and Pi discovery after
the host-specific placement/configuration above. It does not widen runtime
support.

| Bundle | Origin | Loader/frontmatter | Host-specific names, paths, siblings, optional services | Support classification |
|---|---|---|---|---|
| `hanig-project` | authored | portable / all four | Python/Git helpers; sibling `hanig-swarm`; tracker connector and approval gate; optional scheduler/Paseo | capability-limited; Linear uses the outbox contract |
| `hanig-swarm` | authored | portable / all four | Python/Git, Slurm, optional Paseo; sibling authored scripts; coordinator-only credentials | capability-limited; Slurm/dispatch behavior is not a host-worker promise |
| `hanig-verified-workflow` | authored | portable / all four | Python helper, Git evidence, `sbatch`/`sacct`, project-local Nextflow/Snakemake | capability-limited; usable locally only where its workflow programs exist |
| `hanig-review-gate` | authored | portable / all four | Python review helpers, reviewer configuration and coordinator credentials | capability-limited; unavailable reviewers yield unreviewed, never a self-pass |
| `hanig-portable-handoff` | authored | portable / all four | Python/Git helper, `MEMORY.md`, local receipt paths | supported when Python/Git and paths are available; never moves secrets |
| `agent-bus` | vendored verbatim | portable / all four | fixed upstream `~/.agent-bus` layout, local bus helper, optional Paseo | dependency-limited; upstream path is not created by this installer |
| `paseo` | vendored verbatim | portable / all four | Paseo daemon/CLI, `~/.paseo` preferences, agent-bus | dependency-limited; no daemon or provider is provisioned |
| `paseo-advisor` | vendored verbatim | portable / all four | sibling `paseo`, provider preferences, Paseo service | dependency-limited |
| `paseo-committee` | vendored verbatim | portable / all four | sibling `paseo`, two available providers, Paseo service | dependency-limited |
| `paseo-handoff` | vendored verbatim | portable / all four | sibling `paseo`, provider preferences, optional worktree service | dependency-limited |
| `paseo-loop` | vendored verbatim | portable / all four | sibling `paseo`, provider preferences, optional agent-bus | dependency-limited |
| `pi-fleet` | vendored verbatim | portable / all four | Pi-oriented wording, Paseo, agent-bus, provider preferences | dependency-limited; its fleet instructions are Pi-specific, not an OpenCode backend |
| `start-a-sprint` | vendored verbatim | portable / all four | Paseo/agent-bus, configured providers, Git worktrees, Linear references | dependency-limited; host availability is not worker-provider support |

Vendored bundles remain traceable to author Shreshth's
`multi-agent-skills-main.zip` snapshot dated 2026-08-25 and extracted on
2026-09-03 (recorded in commit `19c5171`). Local compatibility text lives here
rather than rewriting them. The only tracked local patch in that vendored
surface is the separately documented `bin/bus` model-registry correction; it
is not a claim that the eight upstream skill documents are locally modified.
