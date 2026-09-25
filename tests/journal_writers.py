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


def _test_method(case):
    method = getattr(case, case._testMethodName)
    method = getattr(method, "__func__", method)
    # Preserve declarations across ordinary functools.wraps/mock.patch layers,
    # regardless of which decorator is outermost.
    return inspect.unwrap(method, stop=lambda function: function in _WRITERS)


def require_registered_writer():
    """Refuse an unmarked running test, even when it borrows a marked helper.

    Every active selected-test/run frame must be registered: a registered
    borrowed test method cannot grant permission to an unmarked outer caller.
    Helper-instance locals alone are not active tests and grant no permission.
    A cached isolation fixture is not permission to write from an unmarked test.
    """
    frame = inspect.currentframe()
    callers = []
    try:
        frame = frame.f_back
        while frame is not None:
            case = frame.f_locals.get("self")
            if isinstance(case, unittest.TestCase):
                selected = getattr(case, case._testMethodName)
                test_codes = (getattr(selected, "__code__", None),
                              getattr(inspect.unwrap(selected), "__code__", None))
                if (any(frame.f_code is code for code in test_codes)
                        or frame.f_code is unittest.TestCase.run.__code__):
                    callers.append(case)
            frame = frame.f_back
        if not callers:
            raise AssertionError("journal helper called without a registered test")
        for caller in callers:
            if _test_method(caller) not in _WRITERS:
                raise AssertionError(
                    "unregistered journal writer: %s; mark the test with "
                    "@journal_writer so the isolation guard covers it" % caller.id())
    finally:
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
