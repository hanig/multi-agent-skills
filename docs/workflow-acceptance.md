# Installed workflow acceptance

Run `python3 -m unittest tests.test_installed_pipeline -v` for the hermetic
workflow seam: the repository installer copies `hanig-project` and its declared
`hanig-swarm` dependency into a disposable store under a temporary HOME. The
staging source is removed before validate → run → advance → report executes
from a separate project directory through `HANIG_SWARM_DIR` and
`HANIG_PROJECT_DIR`.

The fixture replaces PATH with a closed shell worker, shell, Python interpreter,
and sleep for a bounded startup handshake. Isolated Python processes have no
PYTHONPATH or site configuration; the fixture refuses source-checkout reads and socket operations,
and records actual CLI and imported module `__file__` paths under the store,
including the coordinator's predicate subprocess and the report's sibling
imports. This is regression isolation, not an OS security boundary.

The trusted test grades coordinator status and the generated report. An honest
worker writes both declared outputs in its fresh attempt root and reaches DONE;
a hollow worker exits zero without outputs and must not reach DONE. Both print
the same success claim, which the grader ignores. A mutation in a second
temporary installed store makes the pipeline predicate ignore missing outputs:
the hollow unit reaches DONE and the unchanged hollow grader must reject it.
The report's additional evidence-mismatch headline can still catch the mutant;
the mutation specifically tests rejection of the false unit DONE.
Regressions also exercise the mutation after harmless predicate refactoring and
hold a worker until the first report to ensure transient INCOMPLETE liveness
observations are not mistaken for a recorded zero exit.

This covers installed skill composition and the local pipeline boundary. It
does not certify real agents, model quality, scheduler execution, tracker
synchronization, code-unit PR closure, or hostile same-user writers. No model,
network service, real Paseo daemon, scheduler, or real user store participates.
[Cross-agent acceptance](cross-agent-acceptance.md) records native discovery,
authenticated invocation, and directed handoffs separately.
[`tests/native_agent_validation.py`](../tests/native_agent_validation.py) is
the explicitly invoked credentialless loader harness; it does not grade this
workflow or replace the live invocation evidence.
