"""Environment containment shared by every coordinator spawn path."""

import os


_NAMES = frozenset({
    # Locate programs and the user's non-secret Git/Python/Paseo configuration.
    "PATH", "HOME", "SHELL", "USER", "LOGNAME",
    "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME",
    "XDG_STATE_HOME", "XDG_RUNTIME_DIR", "PASEO_HOST",
    # Temporary files, locale, terminal behaviour and time rendering.
    "TMPDIR", "TMP", "TEMP", "LANG", "LANGUAGE", "TERM", "COLORTERM",
    "NO_COLOR", "TZ",
    # Git behaviour needed for local checkout inspection and commits. Broad
    # GIT_* is intentionally forbidden: it includes credential/helper knobs.
    "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_GLOBAL",
    "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_OPTIONAL_LOCKS", "GIT_TERMINAL_PROMPT", "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE", "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_DATE",
    # Python execution settings, including an activated environment.
    "PYTHONPATH", "PYTHONHOME", "PYTHONUTF8", "PYTHONIOENCODING",
    "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE", "PYTHONNOUSERSITE",
    "PYTHONUSERBASE", "PYTHONWARNINGS", "PYTHONHASHSEED", "VIRTUAL_ENV",
    "CONDA_PREFIX", "CONDA_DEFAULT_ENV",
    # Slurm client discovery. Submission credentials are not environment.
    "SLURM_CONF", "SLURM_CLUSTER_NAME",
    # Per-attempt identity. Dependency locations use the prefix below.
    "SWARM_UNIT_ID", "SWARM_UNIT_DIR",
})
_PREFIXES = ("LC_", "SWARM_DEP_")


def child_env(extra=None):
    """The only ambient environment a coordinator child may receive.

    Children get execution context, never coordinator authority: tool/config
    locations, locale and temporary-directory settings, selected Git/Python
    behaviour, and the SWARM_* map needed by units. Credential, token, auth,
    cloud and agent-provider variables are absent unless somebody weakens this
    allowlist explicitly. Callers may add values only under an allowed name.

    HOME is required for Git and Paseo configuration. This contains process
    environment inheritance; it cannot isolate same-uid filesystem config or
    credentials, nor credentials independently inherited by a Paseo daemon.
    """
    source = dict(os.environ)
    if extra:
        for name, value in extra.items():
            name = str(name)
            if name not in _NAMES and not name.startswith(_PREFIXES):
                raise ValueError(f"refusing disallowed child environment {name}")
            source[name] = str(value)
    return {name: value for name, value in source.items()
            if name in _NAMES or name.startswith(_PREFIXES)}
