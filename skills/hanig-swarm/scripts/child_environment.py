"""Environment containment shared by every coordinator spawn path."""

import os


SLURM_INPUT_ENV_NAMES = frozenset({
    # SchedMD's documented sbatch "INPUT ENVIRONMENT VARIABLES". Keep this
    # exact and audited: these are site/user submission defaults, so dropping
    # one can silently change account, cluster or resources; accepting an
    # SBATCH_* prefix would turn this boundary back into a wildcard.
    "SBATCH_ACCOUNT", "SBATCH_ACCTG_FREQ", "SBATCH_ARRAY_INX",
    "SBATCH_BATCH", "SBATCH_CLUSTERS", "SBATCH_CONSTRAINT",
    "SBATCH_CONTAINER", "SBATCH_CONTAINER_ID", "SBATCH_CONTAINER_TYPE",
    "SBATCH_CORE_SPEC", "SBATCH_CPUS_PER_GPU", "SBATCH_DEBUG",
    "SBATCH_DELAY_BOOT", "SBATCH_DISTRIBUTION", "SBATCH_ERROR",
    "SBATCH_EXCLUSIVE", "SBATCH_EXPORT", "SBATCH_GET_USER_ENV",
    "SBATCH_GPU_BIND", "SBATCH_GPU_FREQ", "SBATCH_GPUS",
    "SBATCH_GPUS_PER_NODE", "SBATCH_GPUS_PER_TASK", "SBATCH_GRES",
    "SBATCH_GRES_FLAGS", "SBATCH_HINT", "SBATCH_IGNORE_PBS", "SBATCH_INPUT",
    "SBATCH_JOB_NAME", "SBATCH_MEM_BIND", "SBATCH_MEM_PER_CPU",
    "SBATCH_MEM_PER_GPU", "SBATCH_MEM_PER_NODE", "SBATCH_NETWORK",
    "SBATCH_NO_KILL", "SBATCH_NO_REQUEUE", "SBATCH_OPEN_MODE",
    "SBATCH_OUTPUT", "SBATCH_OVERCOMMIT", "SBATCH_PARTITION", "SBATCH_POWER",
    "SBATCH_PROFILE", "SBATCH_QOS", "SBATCH_REQ_SWITCH", "SBATCH_REQUEUE",
    "SBATCH_RESERVATION", "SBATCH_SEGMENT_SIZE", "SBATCH_SIGNAL",
    "SBATCH_SPREAD_JOB", "SBATCH_THREAD_SPEC", "SBATCH_THREADS_PER_CORE",
    "SBATCH_TIMELIMIT", "SBATCH_TRES_BIND", "SBATCH_TRES_PER_TASK",
    "SBATCH_USE_MIN_NODES", "SBATCH_WAIT", "SBATCH_WAIT_ALL_NODES",
    "SBATCH_WAIT4SWITCH", "SBATCH_WCKEY", "SLURM_CLUSTERS", "SLURM_CONF",
    "SLURM_DEBUG_FLAGS", "SLURM_EXIT_ERROR", "SLURM_HINT",
    "SLURM_STEP_KILLED_MSG_NODE_ID", "SLURM_UMASK",
    # Used by Slurm client libraries at sites including this project's; not
    # an sbatch option, but retained from the previous execution environment.
    "SLURM_CLUSTER_NAME",
})


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
}) | SLURM_INPUT_ENV_NAMES
_AMBIENT_PREFIXES = ("LC_",)


def _constructed_name(name):
    return (name in ("SWARM_UNIT_ID", "SWARM_UNIT_DIR")
            or name.startswith("SWARM_DEP_"))


def child_env(extra=None):
    """The only ambient environment a coordinator child may receive.

    Children get execution context, never coordinator authority: tool/config
    locations, locale and temporary-directory settings, selected Git/Python
    behaviour, and enumerated Slurm submission defaults. Credential, token,
    auth, cloud and agent-provider variables are absent unless somebody
    weakens this allowlist explicitly.

    Ambient and constructed variables are different authorities.
    SWARM_UNIT_ID, SWARM_UNIT_DIR and SWARM_DEP_* are NEVER matched out of
    os.environ: callers pass the exact unit/dependency map they just built
    through ``extra``. Only those explicit values may use the SWARM names.

    HOME is required for Git and Paseo configuration. This contains process
    environment inheritance; it cannot isolate same-uid filesystem config or
    credentials, nor credentials independently inherited by a Paseo daemon.
    """
    out = {name: value for name, value in os.environ.items()
           if name in _NAMES or name.startswith(_AMBIENT_PREFIXES)}
    if extra:
        for name, value in extra.items():
            name = str(name)
            if name not in _NAMES and not _constructed_name(name):
                raise ValueError(f"refusing disallowed child environment {name}")
            out[name] = str(value)
    return out
