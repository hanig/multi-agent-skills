"""Shared repair wording for installation and active-profile refusals."""

REPAIR_GUIDANCE = "repair with d5_install promote"


def refusal_message(message):
    """One repair form for every installation and active-profile refusal."""
    return message if message.endswith(REPAIR_GUIDANCE) else message + "; " + REPAIR_GUIDANCE
