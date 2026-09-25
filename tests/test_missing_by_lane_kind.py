"""Gate: a lane's missing_<cap> names what THAT kind of lane needs. A process
lane with no Blender must say it needs Blender, never the TRELLIS2 GPU model
files a ComfyUI 3d lane needs -- and a ComfyUI lane never names Blender. No
browser, no network.

Run: python3 tests/test_missing_by_lane_kind.py
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


scratch = tempfile.mkdtemp(prefix="bwf_missing_kind_")
cfg = os.path.join(scratch, "config.json")
with open(cfg, "w") as f:
    json.dump({"port": 1, "bind": "127.0.0.1",
               "lanes": [{"id": "cpu", "name": "This machine", "kind": "process", "caps": ["3d"]},
                         {"id": "gpu", "name": "GPU box", "host": "127.0.0.1", "port": 1, "caps": ["3d"]}]}, f)
os.environ["GENCENTER_CONFIG"] = cfg
os.environ["GENCENTER_DATA"] = os.path.join(scratch, "data")
spec = importlib.util.spec_from_file_location("srv_missing_kind", os.path.join(ROOT, "server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)

import engines  # noqa: E402
turntable = next(p for p in engines.packs() if p["id"] == "turntable")
mesh = next(p for p in engines.packs() if p["cap"] == "3d" and p["id"] != "turntable")
BIN_WORDS = [turntable["words"][r] for r in turntable["bins"]]
MESH_WORDS = set(mesh["words"].values())

srv.shutil.which = lambda prog: None          # neither blender nor ffmpeg installed
srv.poll_process_lane(srv.LANE_BY_ID["cpu"])
lanes = {l["id"]: l for l in srv.Handler.lanes_payload(srv.Handler.__new__(srv.Handler))["lanes"]}

print("a process lane with no Blender")
cpu = lanes["cpu"]["missing_3d"]
check("missing_3d names the lane's own programs", cpu == BIN_WORDS, (cpu, BIN_WORDS))
check("missing_3d names no GPU model file", not MESH_WORDS & set(cpu), cpu)

print("a ComfyUI 3d lane with no model files")
gpu = lanes["gpu"]["missing_3d"]
check("missing_3d names the mesh pack's model files", gpu and set(gpu) <= MESH_WORDS, gpu)
check("missing_3d names no program", not set(BIN_WORDS) & set(gpu), gpu)

print("a process lane with everything installed")
srv.shutil.which = lambda prog: "/usr/bin/" + prog
srv.poll_process_lane(srv.LANE_BY_ID["cpu"])
lanes = {l["id"]: l for l in srv.Handler.lanes_payload(srv.Handler.__new__(srv.Handler))["lanes"]}
check("missing_3d is empty", lanes["cpu"]["missing_3d"] == [], lanes["cpu"]["missing_3d"])

if FAILED:
    print("FAILED: %d checks: %s" % (len(FAILED), ", ".join(FAILED)))
    sys.exit(1)
print("OK: all checks passed")
