def d5_active(state_dir):
    """Installation-derived activation; broken installations remain contained."""
    import d5_installation
    if state_dir is None:
        raise ValueError("D5 state_dir is required")
    return d5_installation.state(state_dir) != d5_installation.NOT_INSTALLED


def refusal_message(message):
    """Expose the shared repair message through the coordinator's D5 facade."""
    import d5_installation
    return d5_installation.refusal_message(message)
