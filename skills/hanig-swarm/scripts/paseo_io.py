"""Reading JSON out of a CLI that prints prose before it.

paseo writes human notices ("Created workspace ...", "Tip: reuse with
--workspace {id}") to stdout BEFORE its JSON, so json.loads on the whole
stream fails, and every candidate brace has to be tried because the preamble
can contain one too. Starting at the first brace parsed "{id}", failed, and
left a launched agent unbound.

This existed TWICE, in swarm.py as _paseo_json/_balanced_from and in unit.py
as _json_object_in, whose docstring said "Shared shape with swarm.py's
_paseo_json". Two copies of one predicate is the shape of every recurring
defect here: the fix lands on one branch and its neighbour keeps the bug.
"""
import json


def first_json_object(text):
    """The first balanced JSON object in `text`, or None."""
    if not text:
        return None
    for i, ch in enumerate(text):
        if ch == "{":
            got = _balanced_from(text, i)
            if got is not None:
                return got
    return None


def _balanced_from(text, i):
    depth, instr, esc = 0, False, False
    for j, ch in enumerate(text[i:], i):
        if instr:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                instr = False
            continue
        if ch == '"':
            instr = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[i:j + 1])
                except ValueError:
                    return None
    return None
