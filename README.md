# multi-agent-skills

Portable Claude Code skills and local agent-coordination tools, installable
across laptops and HPC login nodes with one script.

Private. Contains cluster hostnames, partitions, account names, and storage
layouts.

## The idea

A scheduler reporting `COMPLETED` is not evidence that work was produced.
Neither is a zero exit code, a "done" line in a log, or an agent's own claim of
success. Python that catches an exception and returns 0, a pipeline stage that
emits a header-only table, a training run that hit its wall-clock limit — all
look like success to every tool that reports on them.

So every skill here declares an **artifact contract**: a verifiable statement of
what "done" means, written *before* execution and checked afterwards by
something that did not do the work.

Applied to schedulers and workflows rather than to git commits, which is where
the idea came from.

## Layout

```
install.sh                  copy-based installer (--mode link for development)
bin/doctor                  what is installed, from where, and does it still run
bin/probe.sh                read-only environment probe for a new host
skills/                     the skills themselves
tests/                      stdlib unittest; no network, no cluster required
docs/PLAN.md                full design, and where two proposals disagreed
docs/probes/                measured environment data per cluster
```

## Install

```bash
git clone git@github.com:hanig/multi-agent-skills.git
cd multi-agent-skills
./install.sh              # copies into ~/.claude/skills/
./bin/doctor              # verify
```

Options: `--prefix DIR`, `--mode copy|link`, `--only NAME`, `--dry-run`,
`--force`, `--uninstall`.

Copy is the default deliberately. A symlink into a live checkout breaks when the
checkout sits under a synced folder, when a branch switch silently mutates every
installed skill, or when the checkout is mid-update. Use `--mode link` only when
developing a skill.

The installer refuses to replace a directory it did not install, validates
frontmatter and script syntax before touching the destination, and aborts on a
name collision with an org-managed skill.

## Skills

| Skill | Purpose |
|---|---|
| `hanig-swarm` | Coordinate a swarm of agents to build projects autonomously on Slurm. `unit.py` allocates an exclusive per-attempt write root and judges done; `swarm.py` validates a DAG, dispatches, detaches and advances; `converge.py` answers whether a training run converged or merely stopped. |
| `hanig-verified-workflow` | Declare and verify what "done" means for a Slurm job. Distinguishes `SCIENTIFIC_PASS` from `TECHNICALLY_COMPLETE`. Its Slurm state machine is the ancestor of `unit.py`'s. |
| `hanig-review-gate` | Adversarial multi-model review. A panel for CLAIMS, not a gate on every commit — see `PROTOCOL.md`. |
| `hanig-portable-handoff` | Capture and resume run state across machines. |

**Deleted 2026-08-28, recoverable from history:** `hanig-verified-training` and
`hanig-reproducible-result`, plus the conformance and symmetry suites.

A committee concluded that isolation replaces attribution — an exclusive
per-attempt write root makes a cheap predicate conclusive, so the machinery built
to *prove* which process wrote a file was answering a question that no longer
needed asking. `traincontract.py`'s convergence evaluator was the one capability
with no replacement anywhere, including in Shreshth's repo, so it was ported to
`hanig-swarm/converge.py` BEFORE the deletion. `result.py` went because figures
and tables are not a swarm unit kind. The two suites expired with their subject:
a cross-tool conformance lock needs several tools, and a symmetry lock needs a
twin.

All personal skills are prefixed `hanig-` so they can never collide with an
Arc org-managed skill name.

## Deployment targets

Measured 2026-08-25 with `bin/probe.sh` — full output in `docs/probes/`.

| | chimera | lambda | andromeda |
|---|---|---|---|
| host | `chimera-login` | `vci-steady-state-login-001` | `ac-gefion-login-0` |
| user | `hani` | `hani` | `hgoodarzi` |
| `$HOME` | `/home/hani` (nfs4) | `/home/hani` (nfs) | `/mnt/weka/home/hgoodarzi` (wekafs) |
| python3 | 3.10.12 | 3.12.3 | 3.10.12 |
| Slurm | 25.11.0 | 25.05.0 | 24.11.5 (SUNK) |
| `sacct` | works | works | works |
| org skills | none | none | none |

Notes that bite:

- **Org skills reach none of the clusters.** Skills here must be self-sufficient.
- **`claude` and `node` live behind conda/micromamba prefixes loaded by
  `.bashrc`.** A non-interactive `ssh host 'command -v node'` reports ABSENT and
  is wrong. Check with `bash -lic`.
- **Usernames differ across clusters.** Never hardcode `$USER` or a home path.
- **git is ≥2.34 on the clusters but 2.23 on the Mac** — avoid `git init -b`,
  `git switch`, and GNU-only `sed`/`readlink` flags.
- Use `chimera-login`, not `chimera` — the latter carries
  `RemoteCommand sh_dev` and rejects a passed command. Lambda's `/tmp` is not
  writable; stage to `$HOME`. Andromeda needs Tailscale up.

## Tests

```bash
python3 tests/test_contract.py
```

Standard library only, no network, no cluster. Passing on macOS 3.10.16 and on
all three clusters (3.10.12, 3.12.3, 3.10.12).

## Coordination: Paseo, and the fusion

These verifiers adjudicate evidence: they answer "was the work actually done",
and refuse a self-assertion. They do not dispatch work.

Shreshth's `multi-agent-skills` (extracted to `~/paseo-multi-agent-skills`) is
the other half: agent coordination on the Paseo daemon -- who does the work, in
what isolation, notified how. Its skills are installed alongside these.

**As of 2026-08-28 the two are being fused deliberately.** An earlier note in
MEMORY.md called "do not port the coordination machinery" a settled premise; it
was not settled, it was my own narrowing of the brief, and it was overturned.
The reason it gave (do not rebuild a fleet and message bus) expired when Paseo
was installed: there is nothing left to rebuild.

The fusion's point: his agents coordinate but cannot prove anything -- an agent
reporting "done" is a bare claim, which is the self-assertion these tools exist
to refuse. Ours prove but cannot dispatch. Neither repo has the combination.
