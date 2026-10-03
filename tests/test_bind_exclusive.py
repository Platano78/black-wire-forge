"""Acceptance gate for the listening socket: server.py must bind exclusively
so a SECOND process cannot silently share the port, must say the Windows way
to find a busy port, and must recognise the Windows socket errnos
(WSAEADDRINUSE 10048 / WSAEADDRNOTAVAIL 10049) as well as the POSIX ones.

Runs on ANY platform -- the Windows behaviour is asserted from the source and
from the class flag, never by being on Windows. The only network use is
127.0.0.1 on an ephemeral port found by binding port 0.

Run: python3 tests/test_bind_exclusive.py
"""
import importlib.util
import json
import os
import re
import socket
import sys
import tempfile

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond else ""))
    if not cond: FAILED.append(name)

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p

# server.py reads its config at import time: give it a scratch one, like
# tests/test_runner_windows_safe.py does.
SCRATCH = tempfile.mkdtemp(prefix="bwf_bind_")
CONFIG = os.path.join(SCRATCH, "config.json")
json.dump({"port": free_port(), "bind": "127.0.0.1", "title": "bind",
           "timing": {"poll_seconds": 30, "job_poll_seconds": 30},
           "lanes": [{"id": "t", "name": "T", "host": "127.0.0.1", "port": 1, "caps": ["image"]}]},
          open(CONFIG, "w"))
os.environ["GENCENTER_CONFIG"] = CONFIG
os.environ["GENCENTER_DATA"] = os.path.join(SCRATCH, "data")

spec = importlib.util.spec_from_file_location("srv_bind", os.path.join(ROOT, "server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)

# 1. Reuse is allowed everywhere except Windows.
check("allow_reuse_address is True on POSIX, False on Windows",
      srv._Server.allow_reuse_address == (os.name != "nt"),
      repr(srv._Server.allow_reuse_address))

# 2. A second bind on the same port always fails: on POSIX because the first
#    socket is listening, on Windows because the exclusive flag was set.
class _Noop(srv.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

port = free_port()
first = None
second_errno = None
try:
    first = srv._Server(("127.0.0.1", port), _Noop)
    try:
        srv._Server(("127.0.0.1", port), _Noop)
    except OSError as e:
        second_errno = getattr(e, "errno", None)
finally:
    if first is not None:
        first.server_close()

IN_USE = (48, 98, 10048)
check("a second server on the same port raises OSError", second_errno is not None,
      "a second _Server bound %d without complaining" % port)
check("the refused bind reports EADDRINUSE (POSIX 48/98 or Windows 10048)",
      second_errno in IN_USE, repr(second_errno))

# 3. The errno tuples and the "find it with" line, read from the source so the
#    assertions hold without needing a live bind failure.
SRC = open(os.path.join(ROOT, "server.py")).read()

inuse = re.search(r'in \(48, 98, 10048\)', SRC)
notavail = re.search(r'in \(49, 10049\)', SRC)
check("the in-use errno tuple contains WSAEADDRINUSE (10048)", bool(inuse))
check("the not-available errno tuple contains WSAEADDRNOTAVAIL (10049)", bool(notavail))
check("the POSIX find-it line is still there, character for character",
      "Find it with:  %s\\n" in SRC and 'lsof -nP -iTCP:%d -sTCP:LISTEN' in SRC)
check("the Windows find-it line uses netstat/findstr",
      'netstat -ano | findstr :%d' in SRC and 'os.name == "nt"' in SRC)


# Behavioural: two REAL servers on one port. The second must refuse, and the
# message must carry the actual port number in the way to find the owner.
import subprocess, time, urllib.request
PORT2 = free_port()
CFG2 = os.path.join(SCRATCH, "config2.json")
json.dump({"port": PORT2, "bind": "127.0.0.1", "title": "bind2",
           "timing": {"poll_seconds": 30, "job_poll_seconds": 30},
           "lanes": [{"id": "t", "name": "T", "host": "127.0.0.1", "port": 1, "caps": ["image"]}]},
          open(CFG2, "w"))
env2 = dict(os.environ, GENCENTER_CONFIG=CFG2, GENCENTER_DATA=os.path.join(SCRATCH, "data2"))
first = subprocess.Popen([sys.executable, os.path.join(ROOT, "server.py")], cwd=ROOT, env=env2,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    up = False
    for _ in range(60):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/api/health" % PORT2, timeout=1).read(); up = True; break
        except Exception:
            time.sleep(0.25)
    check("a first real server came up", up)
    second = subprocess.run([sys.executable, os.path.join(ROOT, "server.py")], cwd=ROOT, env=env2,
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    out2 = (second.stdout or "") + (second.stderr or "")
    check("the second server refuses (non-zero exit)", second.returncode != 0, second.returncode)
    check("its message says the port is in use", "Port %d is already in use" % PORT2 in out2, out2[-300:])
    want = ("netstat -ano | findstr :%d" if os.name == "nt" else "lsof -nP -iTCP:%d -sTCP:LISTEN") % PORT2
    check("its find-it line carries the real port number", want in out2, out2[-300:])
finally:
    first.kill(); first.wait()

print("FAILED: %d" % len(FAILED))
sys.exit(1 if FAILED else 0)