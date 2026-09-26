# Installed workflow acceptance

Run `python3 -m unittest tests.test_installed_pipeline -v` for the hermetic
workflow seam: the repository installer copies `hanig-project` and its declared
`hanig-swarm` dependency into a disposable store under a temporary HOME. The
staging source is removed before validate → run → advance → report executes
from a separate project directory through `HANIG_SWARM_DIR` and
`HANIG_PROJECT_DIR`.
If the ambient temporary parent is inside the checkout, the fixture chooses
an external writable system temporary parent; a subprocess regression runs the
honest control with a checkout-local `TMPDIR` to exercise this placement.

The fixture replaces PATH with a closed shell worker, shell, Python interpreter,
and sleep for a bounded startup handshake. Isolated Python processes have no
PYTHONPATH or site configuration; the fixture refuses source-checkout reads and
socket operations,
and records actual CLI and imported module `__file__` paths under the store,
including the coordinator's predicate subprocess and the report's sibling
imports. This is regression isolation, not an OS security boundary.

The trusted test grades coordinator status and the generated report. An honest
worker writes both declared outputs in its fresh attempt root and reaches DONE;
a hollow worker exits zero without outputs and must not reach DONE. Both print
the same success claim, which the grader ignores. A mutation in a second
temporary installed store makes the pipeline predicate ignore missing outputs:
the hollow unit reaches DONE and the unchanged hollow grader must reject it,
while the report must still list both outputs as missing.
The report's additional evidence-mismatch headline can still catch the mutant;
the mutation specifically tests rejection of the false unit DONE.
Regressions also exercise the mutation after harmless predicate refactoring and
hold a worker until the first report to ensure transient INCOMPLETE liveness
observations are not mistaken for a recorded zero exit.
The fixture hides `/proc` reads as well as omitting `ps`, so unavailable process
identity is exercised consistently on macOS and Linux. The read guard is
triggered through `open` on every invocation, including on hosts without `/proc`.

The mutation disables the unique direct missing-output refusal in
`_pipeline_state`: a formal-parameter truth test with a note append and an
INCOMPLETE return before the DONE path. Only that condition becomes false;
the rest of the executable AST and the pristine install are checked unchanged.
Call formatting, keyword arguments, and consistent local/parameter renames are
exercised. Renaming the function or changing, extracting, inverting, or duplicating
the decision requires updating this finite mutation adapter; unknown or ambiguous
targets refuse before writing. This does not claim invariance under arbitrary
future predicate redesign.

This covers installed skill composition and the local pipeline boundary. It
does not certify real agents, model quality, scheduler execution, tracker
synchronization, code-unit PR closure, or hostile same-user writers. No model,
network service, real Paseo daemon, scheduler, or real user store participates.
[Cross-agent acceptance](cross-agent-acceptance.md) records native discovery,
authenticated invocation, and directed handoffs separately.
[`tests/native_agent_validation.py`](../tests/native_agent_validation.py) is
the explicitly invoked credentialless loader harness; it does not grade this
workflow or replace the live invocation evidence.
