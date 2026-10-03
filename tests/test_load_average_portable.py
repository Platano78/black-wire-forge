"""Gate: the load average is read portably. os.getloadavg() exists on POSIX
only, so on Windows the process-lane poller used to die on it and every
process lane (Grid check, 3D turntable) stayed down. _load_average() must
return the real number where the platform has one and 0.0 where it has none
-- the lane's load meter then stays empty instead of the lane crashing.

Runs on ANY platform -- the Windows behaviour is simulated by deleting
os.getloadavg, never by being on Windows.

Run: python3 tests/test_load_average_portable.py
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
FAILED = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond:
        print("        -> %s" % (detail,))
        FAILED.append(name)


scratch = tempfile.mkdtemp(prefix="bwf_load_avg_")
cfg = os.path.join(scratch, "config.json")
with open(cfg, "w") as f:
    json.dump({"port": 1, "bind": "127.0.0.1",
               "lanes": [{"id": "cpu", "name": "This machine", "kind": "process", "caps": ["3d"]}]}, f)
os.environ["GENCENTER_CONFIG"] = cfg
os.environ["GENCENTER_DATA"] = os.path.join(scratch, "data")
spec = importlib.util.spec_from_file_location("srv_load_avg", os.path.join(ROOT, "server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)

print("a platform that has os.getloadavg")
# POSIX: the real one. Windows: none exists, so install a known one for the
# length of the check and put back whatever was there (nothing) afterwards.
_had = hasattr(os, "getloadavg")
_saved_here = getattr(os, "getloadavg", None)
try:
    if not _had:
        os.getloadavg = lambda: (0.5, 0.5, 0.5)
    value = srv._load_average()
    if not _had:
        check("returns the first number of the 1-minute average", value == 0.5, repr(value))
finally:
    if _had:
        os.getloadavg = _saved_here
    else:
        if hasattr(os, "getloadavg"):
            del os.getloadavg
check("returns a number", isinstance(value, float), repr(value))
check("returns a non-negative one", value >= 0, repr(value))

print("a platform with no os.getloadavg at all (Windows)")
saved = getattr(os, "getloadavg", None)
try:
    if hasattr(os, "getloadavg"):
        del os.getloadavg
    check("returns 0.0", srv._load_average() == 0.0, repr(srv._load_average()))
    check("the attribute really is gone", not hasattr(os, "getloadavg"))
finally:
    if saved is not None:
        os.getloadavg = saved
check("the attribute is back", getattr(os, "getloadavg", None) is saved)

print("a platform whose os.getloadavg raises")
def _boom():
    raise OSError("no load average here")

try:
    os.getloadavg = _boom
    check("returns 0.0", srv._load_average() == 0.0, repr(srv._load_average()))
finally:
    if saved is not None:
        os.getloadavg = saved
    elif hasattr(os, "getloadavg"):
        del os.getloadavg

print("polling a process lane on a platform with no os.getloadavg")
srv.shutil.which = lambda prog: None            # nothing installed, as on a bare box
raised = None
try:
    if hasattr(os, "getloadavg"):
        del os.getloadavg
    srv.poll_process_lane(srv.LANE_BY_ID["cpu"])
except Exception as exc:                        # noqa: BLE001 - the point of the gate
    raised = exc
finally:
    if saved is not None:
        os.getloadavg = saved
    elif hasattr(os, "getloadavg"):
        del os.getloadavg
check("poll_process_lane did not raise", raised is None, repr(raised))
lanes = {l["id"]: l for l in
         srv.Handler.lanes_payload(srv.Handler.__new__(srv.Handler))["lanes"]}
check("the lane's load is 0.0", lanes["cpu"].get("load") == 0.0, repr(lanes["cpu"].get("load")))

shutil.rmtree(scratch, ignore_errors=True)

if FAILED:
    print("FAILED: %d checks: %s" % (len(FAILED), ", ".join(FAILED)))
    sys.exit(1)
print("OK: all checks passed")
