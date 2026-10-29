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
    # Search in the child, after removing our own entry; nested pins unwind too.
    bootstrap = child_bootstrap(source) + (
      'import os\n'
      'sys.path.remove(os.path.dirname(__file__))\n'
      'pin = sys.modules.pop("sitecustomize")\n'
      'try: importlib.import_module("sitecustomize")\n'
      'except ModuleNotFoundError as error:\n'
      '    if error.name != "sitecustomize": raise\n'
      '    sys.modules["sitecustomize"] = pin\n')
    Path(directory, 'sitecustomize.py').write_text(bootstrap)
    prior = environment.get('PYTHONPATH')
    environment['PYTHONPATH'] = directory + (os.pathsep + prior if prior else '')
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
