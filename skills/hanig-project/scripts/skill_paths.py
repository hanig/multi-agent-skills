#!/usr/bin/env python3
"""Resolve installed hanig skill directories without relying on the shell cwd.

The caller supplies the directory containing the loaded ``SKILL.md``.  That is
the one fact a skill loader knows, whereas its installation prefix and the
process cwd are both arbitrary.  A cross-skill dependency is a sibling of that
loaded directory; it is never looked up in an agent-specific global store.
"""
import argparse
import os
import sys
from pathlib import Path


class SkillPathError(ValueError):
    """A loaded skill or one of its declared installed siblings is invalid."""


def _logical_skill_root(directory):
    """Keep the loader's spelling so a link store keeps its sibling parent."""
    root = Path(directory).expanduser()
    if root.name == "SKILL.md":
        root = root.parent
    # ``absolute`` normalizes a relative spelling without following a link.
    # Resolving here would turn ``prefix/hanig-project`` into the checkout in
    # --mode link and make us look for its siblings there, even when the
    # installed sibling lives at ``prefix/hanig-swarm``.
    return root.absolute()


def loaded_skill_root(directory, expected_name):
    """Return the physical installed directory after validating its identity."""
    root = _logical_skill_root(directory)
    if root.name != expected_name:
        raise SkillPathError(
            f"loaded skill directory is {root.name!r}, expected {expected_name!r}: {root}")
    skill_md = root / "SKILL.md"
    if not skill_md.is_file():
        raise SkillPathError(
            f"loaded {expected_name!r} skill directory is {root}; missing {skill_md}")
    try:
        return root.resolve(strict=True)
    except OSError as exc:
        raise SkillPathError(
            f"cannot resolve loaded {expected_name!r} skill directory {directory!r}: {exc}")


def _configured_roots(explicit_roots):
    """Return only caller-declared additional parents; never glob a home tree."""
    configured = os.environ.get("HANIG_SKILL_DEP_ROOTS", "")
    roots = list(explicit_roots or [])
    roots.extend(p for p in configured.split(os.pathsep) if p)
    return [Path(p).expanduser().absolute() for p in roots]


def sibling_skill_root(directory, loaded_name, sibling_name, explicit_roots=()):
    """Resolve an installed sibling or name the missing declared dependency."""
    logical_root = _logical_skill_root(directory)
    # Validate the loaded endpoint, but deliberately derive siblings from the
    # logical parent supplied by the loader (see _logical_skill_root).
    loaded_skill_root(logical_root, loaded_name)
    parents = [logical_root.parent] + _configured_roots(explicit_roots)
    tried = []
    for parent in parents:
        if parent in tried:
            continue
        tried.append(parent)
        try:
            return loaded_skill_root(parent / sibling_name, sibling_name)
        except SkillPathError:
            pass
    roots = ", ".join(str(p) for p in tried)
    raise SkillPathError(
        f"missing declared installed dependency {sibling_name!r}; searched "
        f"only these explicit skill parents: {roots}. Install it beside "
        f"{logical_root} (including with --only), or set HANIG_SKILL_DEP_ROOTS "
        f"or pass --root for its known parent, then retry.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("self", "sibling"):
        p = sub.add_parser(name)
        p.add_argument("loaded_dir", help="directory containing the loaded SKILL.md")
        p.add_argument("loaded_name", help="expected name of that loaded skill")
        if name == "sibling":
            p.add_argument("sibling_name", help="required sibling skill name")
            p.add_argument("--root", action="append", default=[],
                           help="additional known skill parent; repeatable")
    args = parser.parse_args(argv)
    try:
        if args.command == "self":
            path = loaded_skill_root(args.loaded_dir, args.loaded_name)
        else:
            path = sibling_skill_root(args.loaded_dir, args.loaded_name,
                                      args.sibling_name, args.root)
    except SkillPathError as exc:
        parser.error(str(exc))
    print(path)


if __name__ == "__main__":
    main()
