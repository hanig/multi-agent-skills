"""Inactive Darwin 25.5.0 arm64 adapter; identity=(pid, kernel start_us).

Host: KERN_PROCARGS2 argv[0] basename exactly claude, never p_comm. Unreadable
args identify no host. session_id alone reads CLAUDE_CODE_SESSION_ID, the other
D5-OD4 custody half, not selection input.
"""
import ctypes as C
import os, struct, sys
from typing import NamedTuple

class Unknown(NamedTuple):
 reason: str

class ProcessIdentity(NamedTuple):
 pid: int
 start_us: int

class ProcessRecord(NamedTuple):
 pid: int
 ppid: int
 start_us: int
 command: str  # argv[0], empty if unreadable

def session_id():
 """CLAUDE_CODE_SESSION_ID or None; sole environment reader."""
 return os.environ.get("CLAUDE_CODE_SESSION_ID") or None

def _sysctl(mib, capacity=None):
 libc=C.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
 call=libc.sysctl
 call.argtypes=[C.POINTER(C.c_int), C.c_uint, C.c_void_p,
     C.POINTER(C.c_size_t), C.c_void_p, C.c_size_t]
 call.restype=C.c_int
 name=(C.c_int*len(mib))(*mib)
 size=C.c_size_t(capacity or 0)
 if capacity is None:
  if call(name, len(mib), None, C.byref(size), None, 0):
   raise OSError(C.get_errno(), "sysctl size")
 if not 0<size.value<=1024*1024:
  raise ValueError("sysctl size")
 data=C.create_string_buffer(size.value)
 if call(name, len(mib), data, C.byref(size), None, 0):
  raise OSError(C.get_errno(), "sysctl read")
 if size.value>len(data):
  raise ValueError("sysctl overflow")
 return data.raw[:size.value]

def _argv0(pid):
 try:
  raw=_sysctl((1, 49, pid))  # CTL_KERN, KERN_PROCARGS2, pid
  if struct.unpack_from("<i", raw)[0]<=0:
   return ""
  # argc, NUL-terminated exec path, NUL padding, then argv[0].
  start=raw.index(b"\0", 4)+1
  while start<len(raw) and raw[start]==0:
   start += 1
  end=raw.index(b"\0", start)
  return raw[start:end].decode("utf-8", "surrogateescape")
 except (OSError, ValueError, struct.error):
  return ""

def _read(pid):
 if type(pid) is not int or not 0<pid<2 ** 31:
  return Unknown("invalid pid")
 if sys.platform!="darwin" or (os.uname().release, os.uname().machine)!=("25.5.0", "arm64"):
  return Unknown("unmeasured platform")
 raw=_sysctl((1, 14, 1, pid), 648)  # KERN_PROC_PID
 if len(raw)!=648:
  return Unknown("missing/wrong kinfo_proc")
 sec, usec=struct.unpack_from("<qi", raw, 0)
 found_pid=struct.unpack_from("<i", raw, 40)[0]
 ppid=struct.unpack_from("<i", raw, 560)[0]
 if found_pid!=pid or sec<=0 or not 0<=usec<1000000 or ppid<0:
  return Unknown("invalid kernel identity")
 return ProcessRecord(pid, ppid, sec*1000000+usec, _argv0(pid))

def _inspect(pid):
 try:
  return _read(pid)
 except (OSError, ValueError, AttributeError, struct.error) as exc:
  return Unknown("inspection failed: {}".format(type(exc).__name__))

def process_identity(pid):
 """PID/kernel microseconds; EPERM, absence, failed inspection: unknown, never dead."""
 rec=_inspect(pid)
 return rec if isinstance(rec, Unknown) else ProcessIdentity(rec.pid, rec.start_us)

def ancestry(pid):
 """Leaf first to topmost reachable ancestor; not atomic.

 Parent.start_us <= child.start_us; ties allowed. Stop before unreadable,
 cyclic or inverted hops. Inversion suggests PID reuse; trust nothing beyond
 a break. Failed leaf/changed reread: Unknown.
 """
 chain=[]
 seen=set()
 while pid and pid not in seen:
  rec=_inspect(pid)
  if isinstance(rec, Unknown):
   if not chain:
    return rec
   break
  if chain and rec.start_us>chain[-1].start_us:
   break
  seen.add(pid)
  chain.append(rec)
  pid=rec.ppid
 if not chain or any(_inspect(rec.pid)!=rec for rec in chain):
  return Unknown("missing or changed ancestry")
 return tuple(chain)

def select_session_ancestor(chain):
 """Outermost host in the unbroken leaf-first prefix, or None.

 Host: argv[0] basename exactly claude. Parent start <= child start (ties
 allowed); inversion suggests PID reuse. Trust nothing beyond broken links,
 repeated PIDs or inversions. A session started from inside
 another session's tool call binds to the outer session's process.
 """
 if isinstance(chain, Unknown) or not chain:
  return None
 selected=None
 seen=set()
 previous=None
 for pid, ppid, start, command in chain:
  if (type(pid) is not int or pid<=0 or pid in seen
    or type(start) is not int or start<=0):
   break
  if previous and (previous.ppid!=pid or start>previous.start_us):
   break
  seen.add(pid)
  if os.path.basename(command)=="claude":
   selected=ProcessIdentity(pid, start)
  previous=ProcessRecord(pid, ppid, start, command)
 return selected
