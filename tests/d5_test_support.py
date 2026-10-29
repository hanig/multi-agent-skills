"""Explicit pre-cutover premise for legacy tests, including their Python children.

Opt in with inactive() around a test, or for_case(self) in a legacy fixture.
Importing this module never changes activation. Children inherit the pin only
while the opted-in context is live. Production wiring tests do not opt in.
"""
from contextlib import ExitStack, contextmanager
import os
from pathlib import Path
import tempfile
from unittest import mock


@contextmanager
def inactive():
    import d5_activation
    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(d5_activation, 'd5_active', return_value=False))
        directory = stack.enter_context(tempfile.TemporaryDirectory(prefix='d5-inactive-test-'))
        # Test-owned sitecustomize carries the same premise into legacy CLI
        # fixtures. Explicit active mocks in D5 children still take precedence.
        source = Path(d5_activation.__file__).resolve()
        Path(directory, 'sitecustomize.py').write_text(
            'import importlib.util, sys\n'
            'spec = importlib.util.spec_from_file_location("d5_activation", ' + repr(str(source)) + ')\n'
            'module = importlib.util.module_from_spec(spec)\n'
            'spec.loader.exec_module(module)\n'
            'module.d5_active = lambda: False\n'
            'sys.modules["d5_activation"] = module\n')
        path = directory + os.pathsep + os.environ.get('PYTHONPATH', '')
        stack.enter_context(mock.patch.dict(os.environ, {'PYTHONPATH': path}))
        yield


def for_case(case):
    context = inactive()
    context.__enter__()
    case.addCleanup(context.__exit__, None, None, None)
