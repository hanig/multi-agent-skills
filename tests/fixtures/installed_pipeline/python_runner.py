"""Run an installed CLI with isolated imports and record actual module paths.

The private python3 shim re-enters this runner for coordinator Python children.
This is a hermetic regression fixture, not an OS sandbox for hostile code.
"""
import importlib.util
import json
import os
from pathlib import Path
import sys


def main():
    store = Path(os.environ["PIPELINE_STORE"]).resolve()
    forbidden = [Path(p).resolve() for p in
                 json.loads(os.environ["PIPELINE_FORBIDDEN"])]

    def audit(event, args):
        if event.startswith("socket."):
            raise RuntimeError("network is forbidden in installed-pipeline tests")
        if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(args[0])).resolve()
            if any(path == root or root in path.parents for root in forbidden):
                raise RuntimeError("source checkout is unavailable: " + str(path))

    sys.addaudithook(audit)
    # Exercise the read guard, rather than merely asserting that it exists.
    for root in forbidden:
        try:
            open(root / "CLAUDE.md", "rb")
        except RuntimeError:
            pass
        else:
            raise AssertionError("source read guard did not fire")

    script = Path(sys.argv[1]).resolve()
    if store not in script.parents:
        raise AssertionError("CLI is outside the installed store: " + str(script))
    sys.argv = sys.argv[1:]
    sys.executable = os.environ["PIPELINE_PYTHON"]
    sys.path.insert(0, str(script.parent))
    spec = importlib.util.spec_from_file_location("__main__", script)
    module = importlib.util.module_from_spec(spec)
    sys.modules["__main__"] = module
    try:
        spec.loader.exec_module(module)
    finally:
        paths = {name: str(Path(m.__file__).resolve())
                 for name, m in list(sys.modules.items())
                 if getattr(m, "__file__", None)
                 and not m.__file__.startswith("<")}
        trace = Path(os.environ["PIPELINE_TRACES"]) / (str(os.getpid()) + ".json")
        trace.write_text(json.dumps({"argv": sys.argv, "modules": paths,
                                     "source_guard_exercised": True}))


if __name__ == "__main__":
    main()
