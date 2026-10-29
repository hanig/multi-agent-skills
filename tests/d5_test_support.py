"""Explicit inactive fixtures and child delivery; importing never changes activation.

Use inactive() or for_case(self). Production-wiring tests retain active defaults.
"""
from contextlib import ExitStack, contextmanager
import os
from functools import wraps
from pathlib import Path
import sys
import tempfile
from unittest import mock


def child_bootstrap(source):
    """Explicit inactive premise for a test-owned Python child runner."""
    return (
        'import importlib.util, sys\n'
        'spec = importlib.util.spec_from_file_location("d5_activation", ' + repr(str(Path(source).resolve())) + ')\n'
        'module = importlib.util.module_from_spec(spec)\n'
        'spec.loader.exec_module(module)\n'
        'module.d5_active = lambda: False\n'
        'sys.modules["d5_activation"] = module\n')


@contextmanager
def inactive(owner=None, environ=None):
    with ExitStack() as stack:
        scripts = Path(__file__).resolve().parents[1] / 'skills/hanig-swarm/scripts'
        stack.enter_context(mock.patch.object(sys, 'path', [str(scripts)] + sys.path))
        import d5_activation
        stack.enter_context(mock.patch.object(d5_activation, 'd5_active', return_value=False))
        if owner is not None and owner is not d5_activation:
            stack.enter_context(mock.patch.object(owner, 'd5_active', return_value=False))
        directory = stack.enter_context(tempfile.TemporaryDirectory(prefix='d5-inactive-test-'))
        # Carry the premise into children; their explicit mocks take precedence.
        source = Path(d5_activation.__file__).resolve()
        environment = dict(os.environ if environ is None else environ)
        bootstrap = child_bootstrap(source)
        # Preserve an explicit child environment's existing fault hook after the pin.
        if environ is not None:
            for entry in environment.get('PYTHONPATH', '').split(os.pathsep):
                hook = Path(entry) / 'sitecustomize.py'
                if entry and hook.is_file():
                    filename = repr(str(hook.resolve()))
                    bootstrap += ('with open(' + filename + ', "rb") as fixture_hook:\n'
                                  '    exec(compile(fixture_hook.read(), ' + filename + ', "exec"))\n')
                    break
        Path(directory, 'sitecustomize.py').write_text(bootstrap)
        environment['PYTHONPATH'] = directory + os.pathsep + environment.get('PYTHONPATH', '')
        stack.enter_context(mock.patch.dict(os.environ, {'PYTHONPATH': environment['PYTHONPATH']}))
        yield environment


def for_case(case):
    context = inactive()
    context.__enter__()
    case.addCleanup(context.__exit__, None, None, None)


def inactive_class(cls):
    """Pin only this legacy class, including setUp, tearDown and children."""
    original = cls.setUp

    @wraps(original)
    def setup(self):
        for_case(self)
        original(self)
    cls.setUp = setup
    return cls


def inactive_classes(*classes):
    """Opt in only the explicitly named legacy fixture classes."""
    for cls in classes:
        inactive_class(cls)
