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
import re
from urllib.parse import urlsplit, urlunsplit


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
    # AWS runtime configuration (region, profile, endpoints) passes. These
    # names either contain credentials or point directly to their source.
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
    "AWS_SHARED_CREDENTIALS_FILE",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    # These variables grant access to credential agents or credential caches.
    "SSH_AUTH_SOCK",
    "GPG_AGENT_INFO",
    "KRB5CCNAME",
    "X509_USER_PROXY",
    # sbatch would otherwise reacquire the user's login environment after this
    # module filtered it, defeating containment from the far side.
    "SBATCH_GET_USER_ENV",
})

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

# DECLARED LIMIT: these are deliberately lexical, documented credential
# formats rather than entropy guesses. Arbitrary JSON, base64 and bespoke
# values are opaque here. Trying to infer secrets from those formats would
# create false positives that silently break jobs, so additions to this list
# require a stable credential syntax. Boundaries avoid matching the common
# shapes inside words.
CREDENTIAL_VALUE_PATTERNS = tuple(map(re.compile, (
    r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]+",
    r"(?<![A-Za-z0-9])gh[po]_[A-Za-z0-9_]+",
    r"(?<![A-Z0-9])AKIA[A-Z0-9]{8,}",
    r"(?<![A-Za-z0-9])xox[baprs]-[A-Za-z0-9-]+",
)))
REDACTED_CREDENTIAL = "REDACTED_CREDENTIAL"


def _constructed_name(name):
    return name.startswith(("SWARM_UNIT_", "SWARM_DEP_"))


def _denied_name(name):
    name = name.upper()
    return (name in DENIED_ENV_NAMES
            or name.endswith(DENIED_ENV_SUFFIXES))


def _redact_url_userinfo(value):
    """Remove URL userinfo while preserving the rest of a usable URL."""
    try:
        parsed = urlsplit(value)
        has_userinfo = (parsed.username is not None
                        or parsed.password is not None)
    except (UnicodeError, ValueError):
        return value
    if not parsed.netloc or not has_userinfo:
        return value
    # Split on the final @ so an encoded or literal @ in userinfo cannot leave
    # a credential fragment behind. Keep host spelling, IPv6 brackets and port.
    netloc = parsed.netloc.rsplit("@", 1)[-1]
    return urlunsplit(parsed._replace(netloc=netloc))


def _sanitized_value(value):
    value = _redact_url_userinfo(value)
    for pattern in CREDENTIAL_VALUE_PATTERNS:
        value = pattern.sub(REDACTED_CREDENTIAL, value)
    return value


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
    independently inherited by a Paseo daemon. Value inspection is bounded to
    URL userinfo and known provider-key syntax. Arbitrary secrets inside JSON,
    base64 or bespoke formats cannot be detected without false positives that
    break unrelated jobs; entropy heuristics and general structured-value
    parsing deliberately do not belong at this boundary. The Paseo daemon's
    independent credential supply already makes this boundary porous.
    """
    out = {name: _sanitized_value(value)
           for name, value in os.environ.items()
           if not _constructed_name(name) and not _denied_name(name)}
    if extra:
        for name, value in extra.items():
            name = str(name)
            if not _constructed_name(name):
                raise ValueError(
                    f"refusing non-constructed child environment {name}")
            out[name] = str(value)
    return out
