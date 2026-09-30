# Plan: adopt point releases per family at project start

Owner request, 2026-09-30: stop hand-maintaining model names; when a project
starts, use the newest model in each family. Owner decision: point releases are
adopted automatically; a new generation waits for the owner. The immediate
GPT-6.1 Sol swap ships separately and does not depend on this.

This is the second draft. Seven plan-review rounds on the first found an edge in
each new matching rule; a step-back consult with Sol (2026-09-30) recommended
removing rules rather than adding them. This draft does that.

## Constraints

- A reviewer on an unmeasured output budget silently shrank quorum (ARC-689), so
  every model a seat uses needs a measured budget record for that exact id.
- A Paseo agent requested as `claude-opus-5-5` came up as `claude-opus-5` with no
  error (2026-09-28), so a request is not evidence of what ran.
- Authority lives in merged configuration or coordinator state, never in a file
  an agent writes. `swarm.py` stays network-free and imports no review code.

## Design

### A family is a literal prefix and suffix around a version

```json
"family": {"listing": "openai", "vendor": "", "prefix": "gpt-",
           "separator": ".", "suffix": "-sol", "major": 6}
```

An id splits at its last `/` into a vendor path and a name; an id with no `/`
has vendor path `""` and is all name. A listed id is in the family when its
vendor path equals `vendor` exactly and its name is exactly `prefix`, then a
version, then `suffix`. A version is one run of digits (`N`) or two runs
joined by the family's `separator` (`N` + separator + `M`): `.` for
`gpt-6.1-sol`, `-` for `claude-opus-5-5`. Versions compare as integer pairs,
`M` defaulting to 0, so `6.10` is above `6.2`. Nothing else is parsed: `-pro`,
`:batch`, dated snapshots and previews are simply not in the family. This parser
is used for catalog listings only.

`major` is the owner's ceiling, and it lives in merged `reviewers.json` or
`agents.json`, so only a reviewed change raises it. The shipped `model` must be
in its family and carry that major; a test checks both.

### Selection

From the listing, keep ids in the family whose version has major equal to
`major` and is greater than the shipped pin's version. Choose the greatest. A
listed id with a greater major is reported `NEW_GENERATION` and never chosen;
adopting it means raising `major` in a pull request. A seat with no candidate
keeps its pin.

`listing` is `openai` (`GET /v1/models`), `openrouter` (`GET /api/v1/models`),
or `paseo:<provider>` (`paseo provider models <provider>`).

### A candidate is adopted only after its probe passes

- API seats: one request through the gate's own call path at the seat's effort
  and `max_output_tokens`, prompt `Reply with exactly OK.`. Pass means status
  completed, non-empty text, positive output tokens, and a reported `model`
  exactly equal to the requested id. The typed response fields become the
  candidate's `_max_output_tokens_accepted` record. That record proves budget
  ACCEPTANCE at the seat's configured cap, the same standard every shipped
  record meets and says it meets; it does not prove the model can fill that
  budget. Capacity is caught where it matters: a reviewer whose answer is
  truncated or empty is already classified `REVIEW_INCOMPLETE` by ARC-689 and
  gives no judgment, so an under-delivering resolved model fails a review
  loudly and never counts toward a pass.
- Paseo seats: one `paseo run` at the seat's thinking id, then `paseo inspect`.
  Pass means inspected Provider, Model and Thinking exactly equal what was
  requested. The canary agent is archived afterwards. A Paseo seat is a coding
  agent, not an API reviewer: it has no `max_output_tokens` and no budget
  record, so the output-budget rule does not apply to it. Families are only
  declared for API reviewer seats in `reviewers.json` and the code-agent
  default in `agents.json`; a reviewer seat is never routed through Paseo.

Anything else, including a reported id that differs in any way, keeps the pin
and is reported `PROBE_FAILED` with the reason. Exact equality is what the
providers return today: gpt-6.1-sol, anthropic/claude-sonnet-5.5 and a Paseo
claude canary all reported exactly the requested id. A provider that starts
reporting snapshots will fail probes and keep pins until that is addressed; that
fails toward the measured choice.

### One snapshot per project

The resolver writes a single snapshot to
`<state home>/hanig-review-gate/routing/<project key>.json`, never inside the
project. The project key is a digest of the resolved `--project` directory
itself, not its Git top-level, so two projects in one repository never share a
snapshot. A reader looks for a snapshot keyed to its resolved working
directory, then to each parent up to the Git top-level (or the filesystem root
outside Git), and uses the nearest one. The snapshot is bound to that key and to the SHA-256
of the installed `reviewers.json` and `agents.json` bytes it resolved against,
and holds, per changed seat, the chosen model and its probe record. It is
written atomically.

Readers (`review.py`, `committee.py`, and `swarm.py` by a plain file read) use
the snapshot only when its project key and config hashes match what they have
loaded, and then only its per-seat model and probe record, and only for a seat
where that model is in the seat's family, carries the config's `major`, is
above the pin, and is named by the probe record's typed `model` field;
otherwise they use the pins. Any change to the installed config, including reinstalling a new
release, invalidates the snapshot until the resolver runs again. Every report
and `--list` names each seat's model as `pinned` or `resolved`.

Declared limit, as for coordinator state: the state home is writable by the
owner's UID, which agents launched by the owner share. The snapshot cannot raise
a family's major, because the ceiling is read from the installed config, but it
does not authenticate who wrote it.

### When it runs

`hanig-project` step 1 adds one command after the survey:
`python3 "$HANIG_REVIEW_GATE_DIR/scripts/resolve_models.py" --project .`. It
prints one line per seat (`RESOLVED`, `PINNED`, `NEW_GENERATION`,
`PROBE_FAILED`) and exits 0 whenever every seat has a usable model, which
includes keeping a pin. It can also be run alone.

### Author exclusion is unchanged, and applied to the model in effect

The exact-id rule (ARC-755) stays as it is: it compares the author
declaration's model id after the first slash with the seat's model, so
`codex/gpt-6.1-astra` is compared as `gpt-6.1-astra`. It is applied to each
seat's effective model, so a seat resolved to `gpt-6.1-astra` is excluded for
an author declared as `codex/gpt-6.1-astra`. It does not exclude a different version of
the same family (an author on `gpt-6-astra` against a seat on `gpt-6.1-astra`);
that needs author provenance recorded by the coordinator and is a follow-up, not
part of this change.

## Acceptance criteria

1. The family parser accepts exactly vendor, prefix, version, suffix; a table
   of listed ids (`-pro`, `:batch`, snapshots, other vendors, bare words) is
   rejected; a slash-free id has an empty vendor; `-` and `.` separators both
   parse; `6.10` ranks above `6.2`; each shipped seat's pin is in its family and
   carries its major.
2. Selection adopts the greatest same-major point release above the pin,
   reports a greater major as `NEW_GENERATION` without choosing it, and never
   chooses anything at or below the pin.
3. A probe passes only on completed status, non-empty text, positive output
   tokens, and an exactly equal reported model (API) or exactly equal inspected
   Provider, Model and Thinking (Paseo); each failure mode keeps the pin.
4. The snapshot is written only under the state home and atomically; an
   in-project path is refused; two project directories in one repository get
   different snapshots, and a reader in a subdirectory finds its nearest one.
5. Readers apply the snapshot only when project key and both config hashes
   match, and apply a seat entry only when it passes the family, major, pin and
   probe-record checks; a one-byte config change makes them fall back to the
   pins.
6. `swarm.py` reads the snapshot without importing review code or using the
   network, and a unit's own provider, model or thinking still wins.
7. Author exclusion uses the effective model.
8. No model id appears in code; families live in JSON.
9. Each guard is checked by mutation.

## Out of scope

- Raising a family's major automatically.
- What a provider alias serves over time; a resolved seat names a listed id, as a
  pin does today.
- Author exclusion across versions of one family (follow-up).
