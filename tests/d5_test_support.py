"""Explicit inactive fixtures; importing this module changes no activation state.

Children use an explicit bootstrap after normal interpreter startup. A script
bootstrap lives beside its entry point and removes itself before running it.
"""
import os
from functools import wraps
import sys
import tempfile
import subprocess


def child_bootstrap(source):
    code = globals().get('_embedded_source')
    if code is None:
        with open(__file__, encoding='utf-8') as stream:
            code = stream.read()
    return ('# d5-test-bootstrap\n_d5_namespace = {"__name__": "d5_test_pin", "_embedded_source": ' + repr(code) + '}\n'
            'exec(compile(_d5_namespace["_embedded_source"], "<d5-test-pin>", "exec"), _d5_namespace)\n'
            '_d5_pin = _d5_namespace["inactive"](source=' + repr(os.fspath(source)) + ')\n'
            '_d5_pin.__enter__()\n')


class inactive:
    """Pin module lookups, refuse uncovered bound imports, restore identities."""
    def __init__(self, owner=None, environ=None, source=None):
        self.owner, self.environ, self.source = owner, environ, source
        self.targets, self.files = [], []

    def __call__(self, function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with inactive(self.owner, self.environ, self.source):
                return function(*args, **kwargs)
        return wrapped

    def _audit(self, original):
        for name, module in tuple(sys.modules.items()):
            if module is None:
                continue
            for attribute, value in tuple(vars(module).items()):
                if value is original and (module, attribute) not in self.covered:
                    raise AssertionError('uncovered D5 reference: ' + name + '.' + attribute)

    def _install(self, target):
        previous, previous_class = target.d5_active, type(target)
        raw = vars(target)['d5_active']
        off = lambda: False
        class Pinned(previous_class):
            @property
            def d5_active(self):
                return off

            @d5_active.setter
            def d5_active(self, value):
                self.__dict__['d5_active'] = value
        self.targets.append((target, previous_class, raw, previous, off))
        target.__class__ = Pinned
        target.d5_active = off
        if target.d5_active is not off or target.d5_active() is not False:
            raise AssertionError('D5 inactive pin did not take effect')

    def __enter__(self):
        module = sys.modules.get('d5_activation')
        if module is None:
            if self.source is None:
                self.source = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                           'skills/hanig-swarm/scripts/d5_activation.py')
            module = type(sys)('d5_activation')
            module.__file__ = self.source
            with open(self.source, 'rb') as stream:
                exec(compile(stream.read(), self.source, 'exec'), vars(module))
            sys.modules['d5_activation'] = module
        if self.source is not None and os.path.realpath(module.__file__) != os.path.realpath(self.source):
            raise AssertionError('D5 activation source mismatch')
        self.source = module.__file__
        self.covered = [(module, 'd5_active')]
        if self.owner is not None and self.owner is not module:
            self.covered.append((self.owner, 'd5_active'))
        original = module.d5_active
        self._audit(original)
        self.popen = subprocess.Popen
        try:
            for target, attribute in self.covered:
                self._install(target)
            self._audit(original)
            if any(target.d5_active() is not False for target, attribute in self.covered):
                raise AssertionError('D5 inactive pin did not take effect')
            pin = self
            class Child(self.popen):
                def __init__(self, args, *positional, **kwargs):
                    super().__init__(pin._command(args, kwargs), *positional, **kwargs)
            subprocess.Popen = Child
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise
        return dict(os.environ if self.environ is None else self.environ)

    def _command(self, args, options):
        if options.get('shell') or not isinstance(args, (list, tuple)) or not args:
            return args
        if not os.path.basename(os.fspath(args[0])).startswith('python'):
            return args
        command = list(args)
        index = 1
        while index < len(command) and command[index] in ('-B', '-u', '-I', '-S', '-s', '-E'):
            index += 1
        if index >= len(command):
            return args
        if command[index] == '-c' and index + 1 < len(command):
            command[index + 1] = child_bootstrap(self.source) + command[index + 1]
            return command
        script = os.path.abspath(os.path.join(options.get('cwd') or os.curdir,
                                             os.fspath(command[index])))
        if not os.path.isfile(script):
            return args
        source = os.path.join(os.path.dirname(script), 'd5_activation.py')
        if not os.path.isfile(source):
            source = self.source
        # Preserve suffix-based entry selection in existing startup fixtures.
        fd, wrapper = tempfile.mkstemp(prefix='.d5-test-', suffix='-' + os.path.basename(script),
                                       dir=os.path.dirname(script))
        self.files.append(wrapper)
        with os.fdopen(fd, 'w') as stream:
            stream.write('import os, sys\nos.unlink(__file__)\n' + child_bootstrap(source) +
                         'sys.argv[0] = ' + repr(os.fspath(command[index])) + '\n'
                         '__file__ = os.path.abspath(sys.argv[0])\n'
                         'with open(__file__, "rb") as _d5_stream:\n'
                         '    _d5_program = compile(_d5_stream.read(), __file__, "exec")\n'
                         'exec(_d5_program, globals())\n')
        command[index] = wrapper
        return command

    def __exit__(self, kind, value, traceback):
        subprocess.Popen = self.popen
        for target, previous_class, raw, previous, off in reversed(self.targets):
            target.__class__ = previous_class
            vars(target)['d5_active'] = raw
            for module in tuple(sys.modules.values()):
                if module is not None:
                    for name, bound in tuple(vars(module).items()):
                        if bound is off:
                            setattr(module, name, previous)
        self.targets.clear()
        for path in self.files:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        self.files.clear()


def for_case(case):
    context = inactive()
    context.__enter__()
    case.addCleanup(context.__exit__, None, None, None)
    return context


def inactive_class(cls):
    original = cls.setUp
    @wraps(original)
    def setup(self):
        for_case(self)
        original(self)
    cls.setUp = setup
    return cls


def inactive_classes(*classes):
    for cls in classes:
        inactive_class(cls)
