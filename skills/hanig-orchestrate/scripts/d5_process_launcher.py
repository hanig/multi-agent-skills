"""R8a asynchronous transport, with no production callers.

Supported envelope and accepted limits
-------------------------------------
Only required option families. Timeout proves neither descendant nor remote
termination. Cancellation kills the live local child, or its group with
new_session=True, while the leader is unreaped. Escaped descendants and writes
after leader exit are outside that policy. Local exit is not effect completion.

Capture is file-backed from creation; disk capacity is the caller's budget.
read() is bounded and preserves the writer's offset. No existing descriptors,
inherited stdio, shell text or callbacks are options. Logs are opened by path
in explicit modes. stdin accepts bytes, DEVNULL or a pipe borrowed by the caller;
close_stdin()/close() owns closing it. Captures live until close().
The private seam borrows one fd, never closes it or admits an effect. R8b must
check designation/START and retain parent custody. No protected route is added.
"""
from dataclasses import dataclass
import os
import signal
import subprocess
import tempfile


CAPTURE = object()
MAX_READ = 1024 * 1024


@dataclass(frozen=True)
class Log:
    path: object
    mode: str = "ab"


@dataclass(frozen=True)
class Options:
    cwd: object = None
    env: object = None  # None inherits; a mapping, including {}, replaces.
    stdin: object = subprocess.DEVNULL
    stdout: object = CAPTURE
    stderr: object = CAPTURE
    new_session: bool = False


class Handle:
    """Single-owner transport. Use a context or explicitly close after use."""

    def __init__(self, process, captures, new_session):
        self._process = process
        self._captures = captures
        self._new_session = new_session
        self._closed = False

    @property
    def pid(self):
        return self._process.pid

    @property
    def stdin(self):
        return self._process.stdin

    def close_stdin(self):
        if self.stdin is not None and not self.stdin.closed:
            self.stdin.close()

    def poll(self):
        """None means local running/unknown; an integer is local exit only."""
        return self._process.poll()

    def wait(self, timeout=None):
        """Reap the local child; TimeoutExpired leaves it alive and owned."""
        return self._process.wait(timeout=timeout)

    def read(self, stream, *, offset=0, limit=65536):
        """Read at most MAX_READ bytes; empty while running need not mean EOF."""
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be a nonnegative integer")
        if type(limit) is not int or not 0 < limit <= MAX_READ:
            raise ValueError("limit must be in 1..MAX_READ")
        return os.pread(self._capture(stream).fileno(), limit, offset)

    def size(self, stream):
        return os.fstat(self._capture(stream).fileno()).st_size

    def _capture(self, stream):
        if self._closed:
            raise ValueError("transport is closed")
        if stream not in self._captures:
            raise ValueError("stream was not captured")
        return self._captures[stream]

    def cancel(self):
        """SIGKILL the live child/group; wait() still must reap the child."""
        if self.poll() is None:
            try:
                if self._new_session:
                    os.killpg(self.pid, signal.SIGKILL)
                else:
                    self._process.kill()
            except ProcessLookupError:
                pass

    def close(self):
        """Cancel/reap if necessary, then close only owned transport resources.

        This never closes the protected handoff reference or releases DRAIN.
        If reaping fails, resources remain owned for an explicit retry.
        """
        if not self._closed:
            self.cancel()
            self.wait()
            self.close_stdin()
            for file in self._captures.values():
                file.close()
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def launch(argv, *, options=None):
    """Worker launch: argv only, no inherited protection or arbitrary fds."""
    return _launch(argv, options=options)


def _launch(argv, *, options=None, _protected_fd=None):
    """Private R8b seam: borrow one already-authorized, caller-retained fd."""
    options = Options() if options is None else options
    if not isinstance(options, Options):
        raise TypeError("options must be Options")
    if isinstance(argv, (str, bytes)):
        raise TypeError("argv must be a sequence, not shell text")
    argv = tuple(argv)
    if not argv or any(not isinstance(arg, (str, bytes, os.PathLike)) for arg in argv):
        raise ValueError("argv must contain executable and arguments")
    if type(options.new_session) is not bool:
        raise ValueError("new_session must be bool")
    # Validate everything before opening a log. No existing fd is an option:
    # even redirecting one to fd 0/1/2 would inherit its lock description.
    if not (isinstance(options.stdin, bytes) or
            type(options.stdin) is int and options.stdin in
            (subprocess.DEVNULL, subprocess.PIPE)):
        raise ValueError("stdin requires bytes, PIPE or DEVNULL")
    for name, value in (("stdout", options.stdout), ("stderr", options.stderr)):
        if isinstance(value, Log):
            if value.mode not in ("ab", "wb", "xb"):
                raise ValueError("log mode requires ab, wb or xb")
            os.fspath(value.path)
        elif not (value is CAPTURE or type(value) is int and
                  (value == subprocess.DEVNULL or
                   name == "stderr" and value == subprocess.STDOUT)):
            raise ValueError("output requires CAPTURE, Log, DEVNULL or stderr STDOUT")
    cwd = None if options.cwd is None else os.fspath(options.cwd)
    env = None if options.env is None else dict(options.env)
    if env is not None and any(not isinstance(k, str) or not isinstance(v, str)
                               for k, v in env.items()):
        raise ValueError("environment requires string keys and values")
    if _protected_fd is not None and (type(_protected_fd) is not int or _protected_fd <= 2):
        raise ValueError("private handoff requires a descriptor above 2")
    owned, captures = [], {}
    proc = None
    try:
        streams = {}
        for name, value in (("stdout", options.stdout), ("stderr", options.stderr)):
            if value is CAPTURE:
                value = tempfile.TemporaryFile("w+b")
                captures[name] = value
                owned.append(value)
            elif isinstance(value, Log):
                value = open(value.path, value.mode)
                owned.append(value)
            streams[name] = value
        stdin = options.stdin
        if isinstance(stdin, bytes):
            source = tempfile.TemporaryFile("w+b")
            owned.append(source)
            source.write(stdin)
            source.seek(0)
            stdin = source
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=stdin,
            **streams, start_new_session=options.new_session, close_fds=True,
            pass_fds=() if _protected_fd is None else (_protected_fd,), bufsize=0)
        result = Handle(proc, captures, options.new_session)
        for file in owned:
            if file not in captures.values():
                file.close()
        return result
    except BaseException:
        if proc is not None:
            proc.kill()
            proc.wait()
            if proc.stdin is not None:
                proc.stdin.close()
        for file in owned:
            file.close()
        raise
