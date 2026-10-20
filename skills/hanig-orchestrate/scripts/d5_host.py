"""Inactive adapter: measured Darwin 25.5.0 arm64 kinfo_proc ABI. No death verdict."""
import ctypes as C
import os
import struct
import sys
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
 command: str

def session_id():
 """Sole environment reader."""
 return os.environ.get("CLAUDE_CODE_SESSION_ID") or None

def _read(pid):
 if type(pid) is not int or not 0 < pid < 2 ** 31:
  return Unknown("invalid pid")
 if sys.platform != "darwin" or (os.uname().release, os.uname().machine) != ("25.5.0", "arm64"):
  return Unknown("unmeasured platform")
 libc=C.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
 sysctl=libc.sysctl
 sysctl.argtypes = [C.POINTER(C.c_int), C.c_uint,
     C.c_void_p, C.POINTER(C.c_size_t),
     C.c_void_p, C.c_size_t]
 sysctl.restype = C.c_int
 # CTL_KERN, KERN_PROC, KERN_PROC_PID.
 mib=(C.c_int * 4)(1, 14, 1, pid)
 data=C.create_string_buffer(648)
 size=C.c_size_t(len(data))
 if sysctl(mib, 4, data, C.byref(size), None, 0) != 0:
  return Unknown("sysctl errno {}".format(C.get_errno()))
 if size.value != 648:
  return Unknown("absent or incompatible kinfo_proc")
 sec, usec=struct.unpack_from("<qi", data.raw, 0)
 found_pid=struct.unpack_from("<i", data.raw, 40)[0]
 ppid=struct.unpack_from("<i", data.raw, 560)[0]
 command=data.raw[243:260].split(b"\0", 1)[0].decode("ascii")
 if found_pid != pid or sec <= 0 or not 0 <= usec < 1000000 or ppid < 0 or not command:
  return Unknown("invalid kernel identity")
 return ProcessRecord(pid, ppid, sec * 1000000 + usec, command)

def _inspect(pid):
 try:
  return _read(pid)
 except (OSError, ValueError, AttributeError, struct.error) as exc:
  return Unknown("inspection failed: {}".format(type(exc).__name__))

def process_identity(pid):
 """PID/start microseconds or Unknown; never death."""
 rec=_inspect(pid)
 if isinstance(rec, Unknown):
  return rec
 return ProcessIdentity(rec.pid, rec.start_us)

def ancestry(pid):
 """Caller through PID 1, rechecked; non-atomic."""
 chain=[]
 seen=set()
 while pid:
  if pid in seen or len(chain) >= 128:
   return Unknown("cyclic or excessive ancestry")
  rec=_inspect(pid)
  if isinstance(rec, Unknown):
   return rec
  seen.add(pid)
  chain.append(rec)
  if pid == 1:
   break
  pid=rec.ppid
 if not chain or chain[-1].pid != 1:
  return Unknown("ancestry did not reach init")
 if any(_inspect(rec.pid) != rec for rec in chain):
  return Unknown("ancestry changed during inspection")
 return tuple(chain)

def select_session_ancestor(chain):
 """Nearest claude ancestor; assumes trusted writers."""
 if isinstance(chain, Unknown) or not chain:
  return Unknown("missing ancestry")
 if sum(rec[3] == "claude" for rec in chain[1:]) > 1:
  return Unknown("multiple session hosts: unresolved lifecycle")
 seen=set()
 for index, rec in enumerate(chain):
  pid, ppid, start, command=rec
  if (type(pid) is not int or pid <= 0 or pid in seen
    or type(start) is not int or start <= 0):
   return Unknown("invalid ancestry identity")
  seen.add(pid)
  if index and (chain[index - 1][1] != pid or chain[index - 1][2] < start):
   return Unknown("disconnected ancestry")
  if index and command == "claude":
   return ProcessIdentity(pid, start)
 return Unknown("no supported session host")
