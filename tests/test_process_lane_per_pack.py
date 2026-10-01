"""Gate: a process lane is UP when at least one of its packs has every program
it needs, and down only when none can run. A lane declaring ["3d","producer"]
on a box with ffmpeg, Python 3 and a producer Python but no Blender is up:
Grid check and Mix are available, the turntable mode is unavailable and names
Blender, and the lane's notes still say Blender is needed. Nothing satisfied
is down as before; everything satisfied is up with no notes. No browser, no
network.

Run: python3 tests/test_process_lane_per_pack.py
"""
import importlib.util
import json
import os
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


scratch = tempfile.mkdtemp(prefix="bwf_lane_per_pack_")
cfg = os.path.join(scratch, "config.json")
with open(cfg, "w") as f:
    json.dump({"port": 1, "bind": "127.0.0.1",
               "lanes": [{"id": "cpu", "name": "This machine", "kind": "process",
                          "caps": ["3d", "producer"]}]}, f)
os.environ["GENCENTER_CONFIG"] = cfg
os.environ["GENCENTER_DATA"] = os.path.join(scratch, "data")
spec = importlib.util.spec_from_file_location("srv_lane_per_pack", os.path.join(ROOT, "server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)

import engines  # noqa: E402
turntable = next(p for p in engines.packs() if p["id"] == "turntable")
producer = next(p for p in engines.packs() if p["id"] == "producer")
LANE = srv.LANE_BY_ID["cpu"]
BLENDER = turntable["bins"]["blender"]


def poll(present):
    """Poll the lane with only the named programs 'installed'."""
    srv.shutil.which = lambda prog: ("/usr/bin/" + str(prog)) if str(prog) in present else None
    srv.poll_process_lane(LANE)
    with srv.STATE_LOCK:
        st = dict(srv.LANE_STATE["cpu"])
    h = srv.Handler.__new__(srv.Handler)
    modes = {(cap, m["id"]): m for cap, v in h.engines_payload({"lane": ["cpu"]}).items()
             if cap not in ("rooms", "helper", "helper_guide_on") for m in v["modes"]}
    return st, modes


print("producer programs present, Blender missing")
st, modes = poll({producer["bins"]["producer_python"], producer["bins"]["ffmpeg"],
                 producer["bins"]["python3"]})
check("lane is up", st["up"] is True, st)
check("lane notes name Blender", "Blender" in st["err"], st["err"])
check("grid mode is available", modes[("producer", "grid")]["available"], modes[("producer", "grid")])
check("mix mode is available", modes[("producer", "mix")]["available"], modes[("producer", "mix")])
tt = modes[("3d", "turntable")]
check("turntable mode is unavailable", tt["available"] is False, tt)
check("turntable's missing list names Blender", "Blender" in tt["missing"], tt["missing"])
check("turntable's missing list does not name ffmpeg", "ffmpeg" not in tt["missing"], tt["missing"])

print("every program missing")
st, modes = poll(set())
check("lane is down", st["up"] is False, st)
check("lane notes name Blender", "Blender" in st["err"], st["err"])
check("grid mode is unavailable", not modes[("producer", "grid")]["available"])

print("every program present")
st, modes = poll({BLENDER, "ffmpeg", producer["bins"]["producer_python"],
                 producer["bins"]["python3"]})
check("lane is up", st["up"] is True, st)
check("lane has no notes", st["err"] == "", st["err"])
check("turntable available", modes[("3d", "turntable")]["available"])
check("grid available", modes[("producer", "grid")]["available"])
check("mix available", modes[("producer", "mix")]["available"])

if FAILED:
    print("FAILED: %d checks: %s" % (len(FAILED), ", ".join(FAILED)))
    sys.exit(1)
print("OK: all checks passed")
