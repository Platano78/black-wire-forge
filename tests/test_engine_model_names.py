"""Gate: /api/engines names the model each mode runs (FB-1). `model` is the
pack's own words for the mode's primary role with one leading "the " dropped;
`model_file` is the basename of the file discovery resolved for that role on the
QUERIED lane, else null. No browser, no network.

Run: python3 tests/test_engine_model_names.py
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


scratch = tempfile.mkdtemp(prefix="bwf_model_names_")
cfg = os.path.join(scratch, "config.json")
with open(cfg, "w") as f:
    json.dump({"port": 1, "bind": "127.0.0.1",
               "lanes": [{"id": "gpu", "name": "GPU box", "host": "127.0.0.1", "port": 1,
                          "caps": ["audio", "image", "video"]},
                         {"id": "cpu", "name": "This machine", "kind": "process", "caps": ["3d"]}]}, f)
os.environ["GENCENTER_CONFIG"] = cfg
os.environ["GENCENTER_DATA"] = os.path.join(scratch, "data")
spec = importlib.util.spec_from_file_location("srv_model_names", os.path.join(ROOT, "server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)
import engines  # noqa: E402


def payload(lane=None):
    h = srv.Handler.__new__(srv.Handler)
    return h.engines_payload({"lane": [lane]} if lane else {})


caps = [c for c in engines.caps()]

print("every mode with a primary role and a word gets a model name")
none = payload()
seen = 0
for cap in caps:
    for m in none[cap]["modes"]:
        role = engines.primary_role(cap, m["id"])
        words = next((p.get("words") or {} for p in engines.packs()
                      if p["cap"] == cap and m["id"] in p["graphs"]), {})
        if role and words.get(role):
            seen += 1
            check("%s/%s: model is set" % (cap, m["id"]), bool(m["model"]), m["model"])
            check("%s/%s: no leading 'the '" % (cap, m["id"]), not (m["model"] or "").lower().startswith("the "), m["model"])
            check("%s/%s: no lane, no file" % (cap, m["id"]), m["model_file"] is None, m["model_file"])
check("at least one mode was checked", seen > 0, seen)
audio_song = next(m for m in none["audio"]["modes"] if m["id"] == "song")
check("the pack's own words are reused (one 'the ' dropped)",
      audio_song["model"] == next(p for p in engines.packs() if p["cap"] == "audio" and "song" in p["graphs"])
      ["words"][engines.primary_role("audio", "song")][4:], audio_song["model"])

print("a lane with the file present reports its basename; without, null")
role = engines.primary_role("audio", "song")
with srv.DISCOVERY_LOCK:
    srv.DISCOVERY["gpu"] = {"checked": True, "models": {role: "sub/dir/my_weights.safetensors"}}
song = next(m for m in payload("gpu")["audio"]["modes"] if m["id"] == "song")
check("model_file is the basename only", song["model_file"] == "my_weights.safetensors", song["model_file"])
other = [m for m in payload("gpu")["audio"]["modes"]
         if engines.primary_role("audio", m["id"]) != role]
check("a mode whose role was not found has model_file null", other and all(m["model_file"] is None for m in other),
      [(m["id"], m["model_file"]) for m in other])
with srv.DISCOVERY_LOCK:
    srv.DISCOVERY["gpu"] = {"checked": True, "models": {}}
song = next(m for m in payload("gpu")["audio"]["modes"] if m["id"] == "song")
check("file absent -> model_file null, model still named", song["model_file"] is None and bool(song["model"]), song)

print("a process lane never reports a file")
pl = payload("cpu")
check("3d modes on a process lane: model_file null",
      all(m["model_file"] is None for m in pl["3d"]["modes"]), pl["3d"]["modes"])

print("\nFAILED: %d" % len(FAILED) + (": " + ", ".join(FAILED) if FAILED else ""))
sys.exit(1 if FAILED else 0)
