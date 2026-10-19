"""Gate: a "checkbox" field's value means what it says. The server used bool(val), and
bool("false") is True, so a JSON "false" (or "0", "no", "off") sent to /api/generate ticked
the box. Every checkbox value from a request goes through server._coerce_field_value, so
this pins that function.

Run: python3 tests/test_checkbox_values.py
"""
import importlib.util
import os
import sys

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FAILED = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + repr(detail)) if not cond and detail != "" else ""))
    if not cond:
        FAILED.append(name)


import _scratch_config  # noqa: E402,F401 -- before server.py's own exec_module
spec = importlib.util.spec_from_file_location("srv_checkbox", os.path.join(ROOT, "server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)
import engines  # noqa: E402

box = next(f for f in engines.fields("image", "pixelart") if f["id"] == "pixel_hard_edges")


def coerce(val):
    try:
        return "ok", srv._coerce_field_value(box, val)
    except ValueError as e:
        return "refused", str(e)


print("real booleans and their usual spellings")
for val, want in ((True, True), (False, False), (None, False), (1, True), (0, False),
                  ("true", True), ("True", True), (" TRUE ", True), ("1", True), ("yes", True), ("on", True),
                  ("false", False), ("False", False), ("0", False), ("no", False), ("off", False), ("", False)):
    got = coerce(val)
    check("%r -> %s" % (val, want), got == ("ok", want), got)

print()
print("anything else is refused with a sentence, never quietly ticked")
for val in ("maybe", "2", 2, -1, 0.5, [], {}):
    kind, msg = coerce(val)
    check("%r is refused" % (val,), kind == "refused", (kind, msg))
    if kind == "refused":
        check("%r: the sentence names the field" % (val,), "Hard edges" in msg and msg.endswith("."), msg)

print()
print("through /api/generate: the job gets what the request said")
LANE = {"id": "cb1", "name": "CB1", "box": "b", "note": "", "host": "127.0.0.1", "port": 1, "caps": ["image"]}
srv.LANE_BY_ID[LANE["id"]] = LANE
if LANE not in srv.LANES:
    srv.LANES.append(LANE)
with srv.STATE_LOCK:
    srv.LANE_STATE[LANE["id"]] = {"up": True}
with srv.DISCOVERY_LOCK:
    srv.DISCOVERY[LANE["id"]] = {"models": {"qwen_unet": "qwen_image_2.1.gguf", "pixel_unet": "qwen_image_2.1.gguf",
                                            "qwen_clip": "qwen3vl_8b.safetensors", "qwen_vae": "qwen_image_2.1_vae.safetensors",
                                            "birefnet_model": "birefnet.safetensors"},
                                 "pools": {}, "checked": 1.0, "err": ""}
sent = []
srv.dispatch = lambda lane, graph, kind, mode, meta: (sent.append(meta) or {"ok": True, "job": {"id": "x"}, "notes": []})


def api(values):
    body = {"lane": "cb1", "kind": "image", "mode": "pixelart", "confirm": True,
            "prompt": "Pixel art sprite, a knight",
            "values": dict({"width": 1024, "height": 1024, "resolution": 1024, "steps": 20, "cfg": 2.5}, **values)}
    obj = srv.Handler.__new__(srv.Handler)
    res = []
    obj.read_json = lambda: body
    obj.send_json = lambda payload, code=200: res.append((payload, code))
    srv.Handler.api_generate(obj)
    return res[-1]


for val, want in (("false", False), ("true", True), (False, False), (True, True)):
    sent.clear()
    pl, code = api({"pixel_hard_edges": val})
    got = sent[-1]["args"].get("pixel_hard_edges") if sent else None
    check("pixel_hard_edges=%r reaches the job as %s" % (val, want), code == 200 and got is want, (code, pl, got))
sent.clear()
pl, code = api({"pixel_hard_edges": "maybe"})
check("pixel_hard_edges='maybe' is a 400 with the sentence, nothing dispatched",
      code == 400 and "needs true or false" in (pl.get("error") or "") and not sent, (code, pl))

print()
print(("FAILED: %d" % len(FAILED)) if FAILED else "ALL PASS")
sys.exit(1 if FAILED else 0)
