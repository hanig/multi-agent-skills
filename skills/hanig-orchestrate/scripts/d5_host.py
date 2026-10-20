"""Inactive Darwin 25.5.0 arm64 adapter; identity=(pid, kernel start_us).

Records contain only pid, ppid and kernel start time. session_id alone reads
CLAUDE_CODE_SESSION_ID, the other D5-OD4 custody half. No custody is established.
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
 return ProcessRecord(pid, ppid, sec*1000000+usec)

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
 """Pid/ppid/start-only records, leaf first to topmost reachable; not atomic.

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
