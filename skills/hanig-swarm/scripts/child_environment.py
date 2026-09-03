"""Environment containment shared by every coordinator spawn path.

The policy is deliberately a denylist. Scientific runtime environments are
site-specific and unbounded: Slurm, modules, Conda, MPI, CUDA, proxies and
library paths all add variables that the coordinator cannot enumerate. A
missed runtime variable silently breaks a job, sometimes unsafely (for
example, losing ``CUDA_VISIBLE_DEVICES``). A missed credential pattern leaks
one name that can be identified and added here. Given the accepted limit that
the Paseo daemon can independently provide credentials to an agent, the
latter is the cheaper failure mode.
"""

import os


# Spell out credentials known to be present in coordinator environments even
# where a suffix rule also covers them. The redundancy documents concrete
# authority held by this process rather than leaving it implicit in a pattern.
DENIED_ENV_NAMES = frozenset({
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    # Present in this coordinator environment and contains URL userinfo.
    "SENTRY_DSN_NXTRAY",
    # These variables grant access to credential agents or credential caches.
    "SSH_AUTH_SOCK",
    "GPG_AGENT_INFO",
    "KRB5CCNAME",
    "X509_USER_PROXY",
    # sbatch would otherwise reacquire the user's login environment after this
    # module filtered it, defeating containment from the far side.
    "SBATCH_GET_USER_ENV",
})

DENIED_ENV_PREFIXES = (
    # AWS uses both credential-shaped and innocuous names, but allowing any
    # AWS_* name makes a future credential spelling a leak-by-default.
    "AWS_",
)

DENIED_ENV_SUFFIXES = (
    "_API_KEY",
    "_TOKEN",
    "_SECRET",
    "_PASSWORD",
    "_CREDENTIALS",
    "_ACCESS_KEY",
    "_PRIVATE_KEY",
    "_SECRET_KEY",
    # DSNs routinely embed passwords or ingestion credentials.
    "_DSN",
)


def _constructed_name(name):
    return name.startswith(("SWARM_UNIT_", "SWARM_DEP_"))


def _denied_name(name):
    name = name.upper()
    return (name in DENIED_ENV_NAMES
            or name.startswith(DENIED_ENV_PREFIXES)
            or name.endswith(DENIED_ENV_SUFFIXES))


def child_env(extra=None):
    """Return the environment a coordinator child may receive.

    Runtime context passes through except for credential-shaped names and the
    Slurm switch that would re-import the login environment. Ambient and
    constructed variables remain different authorities: ``SWARM_UNIT_*`` and
    ``SWARM_DEP_*`` are never inherited from ``os.environ``. Callers pass the
    exact unit/dependency map they constructed through ``extra``; no other
    per-call widening is accepted. A constructed name that resembles a
    credential is still safe here: its value is a coordinator-built unit or
    attempt path, not a value read from the ambient environment.

    This contains process environment inheritance only. It cannot isolate
    same-uid filesystem configuration or credentials, nor credentials
    independently inherited by a Paseo daemon.
    """
    out = {name: value for name, value in os.environ.items()
           if not _constructed_name(name) and not _denied_name(name)}
    if extra:
        for name, value in extra.items():
            name = str(name)
            if not _constructed_name(name):
                raise ValueError(
                    f"refusing non-constructed child environment {name}")
            out[name] = str(value)
    return out
