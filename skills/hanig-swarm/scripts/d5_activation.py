def d5_active(state_dir):
    """Installation-derived activation; broken installations remain contained."""
    import os.path
    import d5_installation
    if state_dir is None:
        raise ValueError("D5 state_dir is required")
    return d5_installation.state(os.path.realpath(state_dir)) != d5_installation.NOT_INSTALLED
