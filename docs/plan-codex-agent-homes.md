# Plan: a Codex home per dispatched agent

Status: passed plan review (round 2, Kimi and Sol) on 2026-10-06.

Owner decisions, 2026-10-06: every Codex agent the swarm dispatches gets its
own `CODEX_HOME`, created by the dispatch code (not described in skill
prose), and those homes authenticate with the OpenAI API key.

## Why

Every Codex process sharing one home shares its SQLite databases
(`state_5`, `logs_2`, `memories_1`, `queue_1`, `thread_history_1`,
`goals_1`) and the `thread-writer-locks` directory. Another session reported
that with more than 30 vitrine agents up, dispatches failed on the shared
lock five times in a row, and that ARC-1384 dispatched on the first try with
its own home. That is one success, observed while lock holders were also
being cut from 31 to 18. The mechanism holds either way: a home per agent
has no shared database and no shared lock. Measured 2026-10-06 on this
host: a fresh `CODEX_HOME` creates all of those databases inside itself.

## Credentials

Measured 2026-10-06 on this host:
- `~/.codex/auth.json` is a ChatGPT login whose refresh token is invalid
  (`invalid_refresh_token` when a fresh home tried to use it), so copying or
  symlinking it would hand agents a dead credential. The working ChatGPT
  login evidently lives elsewhere (Codex's keychain store keys entries by a
  hash of the `CODEX_HOME` path, so a new home cannot reach it). That last
  point is an inference; the credential store was not inspected.
- Codex's source (`codex-rs/login/src/auth/storage.rs`): the default store is
  `file`; file saves write `auth.json` in place (truncate and write, mode
  0600), so a symlinked file survives a save.

So agent homes use an API-key login, which never refreshes, and the
source file is read-only: Codex in API-key mode never writes `auth.json`, so
read-only costs nothing, and a stray save through an agent's symlink (for
example an accidental `codex login` inside an agent) fails loudly instead of
truncating the shared credential. An agent running as the same user could
still change the file's mode or contents deliberately; that is the
repository's declared trusted-writer boundary, not something this closes.
Dispatch re-validates the source before every launch, so a source that was
changed or loosened refuses the next dispatch instead of propagating. a source home
whose `auth.json` has `auth_mode` `apikey`, by default `~/.codex-api`,
overridable with `HANIG_SWARM_CODEX_AUTH_HOME`. The key is never copied:
each agent home symlinks the source's `auth.json`. This bills API dollars
rather than plan quota, as the owner chose.

## What dispatch does

For a unit whose provider is `codex/...`, before `paseo run`:

1. Validate the source: `<source>/auth.json` exists, is a regular file owned
   by the current user, is read-only (mode 0400: no write bit for anyone,
   no group or other access), and parses with `auth_mode == "apikey"`. Only `auth_mode` is read; no other field is read
   into memory longer than the parse, logged, or printed. Any failure
   refuses the launch with a one-line reason naming the path and the fix
   (`chmod 600 <source>/auth.json 2>/dev/null; printenv OPENAI_API_KEY |
   CODEX_HOME=<source> codex login --with-api-key && chmod 400
   <source>/auth.json`, which first makes an existing read-only file
   writable so the login can replace it), never the key; the unit stays unlaunched, as for other
   dispatch refusals.
2. Create the agent home with `mkdir(exist_ok=False)`, mode 0700, at
   `<state dir>/codex-homes/<attempt id>`, under the coordinator state root,
   outside every Git worktree (`coordinator_paths` rules) and outside the
   attempt's declared write root.
3. Inside it, symlink `auth.json` to the source's `auth.json`, and symlink
   `config.toml` and `skills/` to the operator's `~/.codex` equivalents when
   they exist (absent ones are skipped, not created).
4. Add `--env CODEX_HOME=<agent home>` to the `paseo run` argv, and record
   the home's path in coordinator state for that attempt (state is the
   authority for cleanup; the agent cannot redirect it).

Other providers are unchanged. Agents no longer share Codex memories or
thread history; each unit's full specification is in its prompt, so they do
not need them.

## Cleanup

The home holds a link to the credential and the agent's databases. It is
removed when the attempt's agent is archived or the attempt reaches a
terminal state and its work is preserved (the existing preserve-then-clean
order in `_archive_code_worktree`, extended to non-code Codex units at their
terminal transition). Removal deletes the symlinks themselves and never
follows them, so the source `auth.json`, `config.toml` and `skills/` survive;
it refuses a path that is not exactly the recorded home under
`<state dir>/codex-homes/`. A failed removal is recorded and retried on the
next advance; it never blocks unrelated work.

## Hosts

Each host that dispatches Codex agents needs the source home once:
`printenv OPENAI_API_KEY | CODEX_HOME=~/.codex-api codex login
--with-api-key` (key on stdin, never argv), then `chmod 400
~/.codex-api/auth.json`. `bin/doctor` reports whether the
source exists and is in API-key mode.

## Acceptance criteria

1. Two Codex units dispatched in one advance get distinct homes, each
   passed as `--env CODEX_HOME=...`, each with `auth.json` a symlink to the
   source; non-Codex units get none.
2. A missing source, a non-apikey source, or a source that is writable or
   group- or world-accessible refuses the launch; a write attempted through
   an agent home's symlink fails against the read-only source and leaves it
   byte-identical (tested by writing through the link); before `paseo run`, with the fix in the
   message and no key in any output.
3. Homes are created exclusively under `<state dir>/codex-homes/`, never
   inside a worktree or the attempt write root; a pre-existing path refuses.
4. Archive or terminal cleanup removes the home, leaves the source files
   and their targets intact, refuses an unrecorded or out-of-root path, and
   retries a failed removal without blocking other units.
5. The key never appears in argv, logs, state or any file the coordinator
   writes; `OPENAI_API_KEY` stays stripped from children.
6. Live check, owner-authorized when run: one trivial Codex unit dispatched
   through Paseo on this host starts, writes its databases inside its own
   home, finishes, and its home is removed on cleanup.

## Not in this PR

`bin/bus launch-worker` (vendored with one pinned patch), migrating running
agents, and ChatGPT-login homes.
