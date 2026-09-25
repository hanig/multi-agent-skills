"""Registration and runtime checks for review tests that can write journals.

Registration is per test method, never per class or helper: adding a test to a
writer class does not silently grant it permission. The isolation guard asks
unittest for the module's discoverable cases and selects registered methods.
This is test plumbing, not a sandbox against arbitrary Python/file writes.
"""

import inspect
import unittest
import weakref


_WRITERS = weakref.WeakSet()


def journal_writer(method):
    """Declare a test method for inclusion in the journal isolation guard.

    Place this outermost when another decorator does not preserve __wrapped__.
    Transparent decorators and static/class descriptors support either order.
    """
    _WRITERS.add(getattr(method, "__func__", method))
    return method


def _registered_function(method):
    function = getattr(method, "__func__", method)
    return inspect.unwrap(function, stop=lambda candidate: candidate in _WRITERS)


def _test_method(case):
    # Selected-name lookup is for discovery only, never runtime authorization.
    return _registered_function(getattr(case, case._testMethodName))


def _frame_receiver(frame):
    """Read the bound first argument without assuming it is named self."""
    code = frame.f_code
    if code.co_argcount:
        return frame.f_locals.get(code.co_varnames[0])
    if code.co_flags & inspect.CO_VARARGS:
        arguments = frame.f_locals.get(code.co_varnames[code.co_kwonlyargcount], ())
        return arguments[0] if arguments else None
    return None


def _registered_code_is_active(function, frames):
    """Match the captured callable's code without trusting mutable body locals."""
    code = getattr(function, "__code__", None)
    return any(frame.f_code is code for frame in frames)


def require_registered_writer():
    """Authorize actual registered execution beneath every active unittest run.

    TestCase.run captures testMethod before invoking it. Read that captured
    callable and require its registered code below that run. Mutating the case's
    selected name or the body's receiver local cannot grant or remove permission.
    Directly called borrowed test methods remain constraints, never authority:
    an unmarked nested method is refused even beneath a registered outer run.
    """
    frame = inspect.currentframe()
    frames = []
    try:
        frame = frame.f_back
        while frame is not None:
            frames.append(frame)
            frame = frame.f_back
        runs = {index: _frame_receiver(frame)
                for index, frame in enumerate(frames)
                if frame.f_code is unittest.TestCase.run.__code__
                and isinstance(_frame_receiver(frame), unittest.TestCase)}
        if not runs:
            raise AssertionError("journal helper called without a registered test")
        running_cases = {id(case) for case in runs.values()}
        callers = []
        for index, frame in enumerate(frames):
            case = _frame_receiver(frame)
            if index in runs:
                # This is unittest's captured callable, not mutable case metadata.
                selected = frame.f_locals.get("testMethod")
                function = _registered_function(selected)
                if not _registered_code_is_active(function, frames[:index]):
                    raise AssertionError(
                        "journal helper called without active registered test code")
                callers.append((case, function))
            elif isinstance(case, unittest.TestCase) and id(case) not in running_cases:
                # A borrowed helper instance grants nothing. A directly called
                # test body on it must still be registered, including after a rename.
                names = unittest.defaultTestLoader.getTestCaseNames(type(case))
                if hasattr(type(case), "runTest"):
                    names.append("runTest")
                for name in names:
                    candidate = getattr(case, name)
                    leaf = inspect.unwrap(getattr(candidate, "__func__", candidate))
                    if frame.f_code is getattr(leaf, "__code__", None):
                        callers.append((case, _registered_function(candidate)))
        for caller, function in callers:
            if function not in _WRITERS:
                raise AssertionError(
                    "unregistered journal writer: %s.%s; mark the test with "
                    "@journal_writer so the isolation guard covers it" %
                    (type(caller).__qualname__, getattr(function, "__name__", "unknown")))
    finally:
        frames.clear()
        del frame


def registered_writer_names(module):
    """Return every discoverable registered test's ID, with no writer roster."""
    def cases(suite):
        for item in suite:
            if isinstance(item, unittest.TestSuite):
                yield from cases(item)
            else:
                yield item

    suite = unittest.defaultTestLoader.loadTestsFromModule(module)
    return tuple(case.id() for case in cases(suite)
                 if _test_method(case) in _WRITERS)
