"""Opt-in transport; no production callers or effect completion.

Supported envelope and accepted limits
Single owner; file-backed capture, caller budgets disk, bounded reads.
Wait: local status or TimeoutExpired. Cancel polls the leader first; signals
only if running. If that poll sees exit, or exit was recorded before,
surviving group members are not signalled. Cancel before wait cannot guarantee
descendant shutdown; callers needing it must own an appropriate lifetime protocol.
Local exit does not imply descendant termination. Escapes/post-exit writes excluded.
Logs always append. Failed launch leaves a new file empty and existing bytes intact;
handles close but files are never unlinked. Only regular logs, no final symlinks/fd aliases.
stdin: bytes/DEVNULL/borrowed PIPE. close owns stdin/captures, not the private fd;
R8b must authorize that fd and retain parent custody.
"""
from dataclasses import dataclass
import os
import signal
import stat
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
	env: object = None  # None inherits; mappings replace.
	stdin: object = subprocess.DEVNULL
	stdout: object = CAPTURE
	stderr: object = CAPTURE
	new_session: bool = False
class Handle:
	def __init__(self, process, captures, new_session):
		self._p = process
		self._files = captures
		self._session = new_session
		self._closed = False
	@property
	def pid(self): return self._p.pid
	@property
	def stdin(self): return self._p.stdin
	def close_stdin(self):
		if self.stdin is not None and not self.stdin.closed:
			self.stdin.close()
	def poll(self): return self._p.poll()
	def wait(self, timeout=None): return self._p.wait(timeout=timeout)
	def read(self, stream, *, offset=0, limit=65536):
		if type(offset) is not int or offset < 0:
			raise ValueError("invalid offset")
		if type(limit) is not int or not 0 < limit <= MAX_READ:
			raise ValueError("invalid read limit")
		return os.pread(self._capture(stream).fileno(), limit, offset)
	def size(self, stream): return os.fstat(self._capture(stream).fileno()).st_size
	def _capture(self, stream):
		if self._closed:
			raise ValueError("transport is closed")
		if type(stream) is not str or stream not in self._files:
			raise ValueError("stream was not captured")
		return self._files[stream]
	def cancel(self):
		if self.poll() is None:
			try:
				if self._session:
					os.killpg(self.pid, signal.SIGKILL)
				else:
					self._p.kill()
			except ProcessLookupError:
				pass
	def close(self):
		if not self._closed:
			self.cancel()
			self.wait()
			self.close_stdin()
			for file in self._files.values():
				file.close()
			self._closed = True
	def __enter__(self): return self
	def __exit__(self, *exc): self.close()
def _log(path, mode):
	directory = os.open(os.path.dirname(path) or '.', os.O_DIRECTORY |
						getattr(os, 'O_SEARCH', getattr(os, 'O_PATH', os.O_RDONLY)))
	try:
		here = os.fstat(directory)
		try:
			magic = os.stat('/dev/fd')
		except FileNotFoundError:
			magic = None
		if magic and (here.st_dev, here.st_ino) == (magic.st_dev, magic.st_ino):
			raise ValueError('logs cannot export descriptors')
		flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_APPEND
		fd = os.open(os.path.basename(path), flags, 0o666, dir_fd=directory)
		try:
			if not stat.S_ISREG(os.fstat(fd).st_mode):
				raise ValueError('logs require regular files')
			return os.fdopen(fd, mode)
		except BaseException:
			os.close(fd)
			raise
	finally:
		os.close(directory)
def launch(argv, *, options=None):
	return _launch(argv, options=options)
def _snapshot_options(argv, options):
	"""Only boundary reading caller options; return exact primitive leaves."""
	options = Options() if options is None else options
	if type(options) is not Options:
		raise TypeError("expected exact Options")
	def path(value):
		value = os.fspath(value)
		if type(value) not in (str, bytes):
			raise ValueError("path requires exact str/bytes")
		return value
	if isinstance(argv, (str, bytes)):
		raise TypeError("argv requires a sequence")
	argv = tuple(path(arg) for arg in argv)
	if not argv:
		raise ValueError("invalid argv")
	cwd, env, stdin, stdout, stderr, session = (
		options.cwd, options.env, options.stdin, options.stdout,
		options.stderr, options.new_session)
	if type(session) is not bool:
		raise ValueError("new_session must be bool")
	if not (type(stdin) is bytes or type(stdin) is int and
			stdin in (subprocess.DEVNULL, subprocess.PIPE)):
		raise ValueError("invalid stdin option")
	outputs = []
	for name, value in (("stdout", stdout), ("stderr", stderr)):
		if type(value) is Log:
			mode, target = value.mode, value.path
			if type(mode) is not str or mode != "ab":
				raise ValueError("logs require ab mode")
			value = (path(target), mode)
		elif not (value is CAPTURE or type(value) is int and
				(value == subprocess.DEVNULL or
				name == "stderr" and value == subprocess.STDOUT)):
			raise ValueError("invalid output option")
		outputs.append((name, value))
	cwd = None if cwd is None else path(cwd)
	if env is not None:
		if type(env) is not dict:
			raise ValueError("environment requires exact dict")
		env = env.copy()
		if any(type(k) is not str or type(v) is not str for k, v in env.items()):
			raise ValueError("environment requires exact str keys/values")
	return argv, cwd, env, stdin, outputs, session
def _launch(argv, *, options=None, _protected_fd=None):
	argv, cwd, env, stdin, outputs, session = _snapshot_options(argv, options)
	if _protected_fd is not None and (type(_protected_fd) is not int or _protected_fd <= 2):
		raise ValueError("private fd must be above 2")
	owned, captures = [], {}
	proc = None
	try:
		streams = {}
		for name, value in outputs:
			if value is CAPTURE:
				value = tempfile.TemporaryFile("w+b")
				captures[name] = value
				owned.append(value)
			elif type(value) is tuple:
				value = _log(*value)
				owned.append(value)
			streams[name] = value
		if isinstance(stdin, bytes):
			source = tempfile.TemporaryFile("w+b")
			owned.append(source)
			source.write(stdin)
			source.seek(0)
			stdin = source
		proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=stdin,
			**streams, start_new_session=session, close_fds=True,
			pass_fds=() if _protected_fd is None else (_protected_fd,), bufsize=0)
		result = Handle(proc, captures, session)
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
