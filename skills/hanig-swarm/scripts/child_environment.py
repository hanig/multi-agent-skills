"""Environment containment shared by every coordinator spawn path.

This is deliberately a finite, exact-name denylist. It stops credentials the
coordinator is known to hold from reaching a child without trying to classify
the site-specific and unbounded environment needed by scientific jobs.

DECLARED LIMIT: this is not a general secret filter. A credential held under a
name not enumerated below, or embedded in another variable's value, passes
through untouched. That is accepted because the Paseo daemon independently
supplies provider credentials to agents regardless, and because two rounds of
name/value pattern matching broke legitimate runtime configuration in ways an
enumerator cannot bound.
"""

import os


# Concrete authority known to be present in coordinator environments or under
# conventional, unambiguous credential names across deployments. Keep this
# bounded and exact: additions require an exact credential name, not a suffix,
# prefix or value-shape guess.
DENIED_ENV_NAMES = frozenset({
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "SENTRY_DSN_NXTRAY",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "HUGGINGFACE_TOKEN",
    # Exact AWS credential names cannot match AWS_REGION, AWS_PROFILE or other
    # runtime configuration. That specificity is the policy boundary.
    "AWS_SECRET_ACCESS_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    # Access to the live SSH credential agent is authority, even though the
    # variable contains a socket path rather than the credential itself.
    "SSH_AUTH_SOCK",
    # sbatch would otherwise reacquire the user's login environment after this
    # module filtered it, defeating containment from the far side.
    "SBATCH_GET_USER_ENV",
})


def _constructed_name(name):
    return name.startswith(("SWARM_UNIT_", "SWARM_DEP_"))


def child_env(extra=None):
    """Return the environment a coordinator child may receive.

    Values pass through unchanged. Only exact names in ``DENIED_ENV_NAMES``
    are removed. Ambient and constructed variables remain different
    authorities: ``SWARM_UNIT_*`` and ``SWARM_DEP_*`` are never inherited from
    ``os.environ``. Callers pass the exact unit/dependency map they constructed
    through ``extra``; no other per-call widening is accepted.

    This contains known process-environment inheritance only. It cannot
    isolate same-uid filesystem configuration, unenumerated or embedded
    credentials, or credentials independently inherited by a Paseo daemon.
    """
    out = {name: value for name, value in os.environ.items()
           if not _constructed_name(name) and name not in DENIED_ENV_NAMES}
    if extra:
        for name, value in extra.items():
            name = str(name)
            if not _constructed_name(name):
                raise ValueError(
                    f"refusing non-constructed child environment {name}")
            out[name] = str(value)
    return out
