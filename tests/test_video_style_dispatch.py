"""The H3 video modes (fl2va / ref2v / continue) are legacy_dispatch, so
_generate_video builds its args by hand. A declared Style (LoRA) pool_select
field, and the strength number that depends on it, must still reach the
graph through the SERVER dispatch path -- not only through engines.graph_for
(which tests/test_style_lora_h3.py covers). Same harness shape as
tests/test_pool_select.py: a real Handler.api_generate, dispatch() captured.

Run: python3 tests/test_video_style_dispatch.py
"""
import importlib.util, json, os, sys
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail else ""))
    if not cond: FAILED.append(name)

import _scratch_config  # noqa: E402 -- must run before server.py's own exec_module below

spec = importlib.util.spec_from_file_location("srv_vstyle", os.path.join(ROOT, "server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)

MODELS = json.load(open(os.path.join(HERE, "golden", "models.json")))
LANE = {"id": "v", "name": "Video lane", "box": "b", "note": "", "host": "127.0.0.1", "port": 1,
        "gpu": "127.0.0.1", "gpu_label": "b", "caps": ["video"]}
srv.LANE_BY_ID[LANE["id"]] = LANE
if LANE not in srv.LANES:
    srv.LANES.append(LANE)
with srv.STATE_LOCK:
    srv.LANE_STATE[LANE["id"]] = {"up": True}
LTX_MODELS = json.load(open(os.path.join(HERE, "golden", "ltx_models.json")))
STYLE = "cool_minimax_h3.safetensors"
LTX_STYLE = "cool_ltx25.safetensors"
POOL = [STYLE, LTX_STYLE, "h3_turbo_8step.safetensors", "unrelated.safetensors"]
with srv.DISCOVERY_LOCK:
    srv.DISCOVERY[LANE["id"]] = {"models": dict(MODELS, **LTX_MODELS), "pools": {"lora": POOL}, "checked": 1.0, "err": ""}

captured = []
srv.dispatch = lambda lane, graph, kind, mode, meta: (
    captured.append({"graph": graph, "meta": meta}) or {"ok": True, "job": {"id": "fake"}, "notes": []})


def call(body):
    o = srv.Handler.__new__(srv.Handler)
    o.read_json = lambda: body
    results = []
    o.send_json = lambda payload, code=200: results.append((payload, code))
    srv.Handler.api_generate(o)
    return results[-1] if results else (None, None)


def req(mode, values=None, **extra):
    b = {"lane": "v", "kind": "video", "mode": mode, "confirm": True, "steps": 25, "turbo_lora": False,
         "prompt": "a slow dolly shot across a quiet harbour at dawn, soft light", "seed": 7}
    if mode == "ref2v":
        b["ref_images"] = ["ref.png"]
    else:
        b["first_frame"] = "first.png"
    b.update(extra)
    if values is not None:
        b["values"] = values
    return b


def loras(g):
    return [n for n in g.values() if n.get("class_type") == "LoraLoaderModelOnly"]


for mode, sched, guider in (("ref2v", "167", "168"), ("fl2va", "9", "16"), ("continue", "9", "16")):
    print()
    print("%s: a Style from the lane's pool, with its strength, reaches the submitted graph" % mode)
    captured.clear()
    payload, code = call(req(mode, {"style_1": STYLE, "style_1_strength": 0.8}))
    check("%s call succeeded" % mode, bool(payload) and payload.get("ok") is True, str((payload, code)))
    g = captured[-1]["graph"] if captured else {}
    ls = loras(g)
    check("%s: one LoraLoaderModelOnly with the chosen file and strength 0.8" % mode,
          len(ls) == 1 and ls[0]["inputs"]["lora_name"] == STYLE and ls[0]["inputs"]["strength_model"] == 0.8, ls)
    check("%s: scheduler and guider read the style chain" % mode,
          bool(g) and g[sched]["inputs"]["model"] == ["230", 0] and g[guider]["inputs"]["model"] == ["230", 0])

print()
print("strength 0 through the server path is LoRA-off, not the 1.0 default")
for mode in ("ref2v", "fl2va", "continue"):
    captured.clear()
    payload, code = call(req(mode, {"style_1": STYLE, "style_1_strength": 0}))
    ls = loras(captured[-1]["graph"]) if captured else []
    check("%s: strength 0 -> strength_model == 0.0" % mode,
          len(ls) == 1 and ls[0]["inputs"]["strength_model"] == 0.0, (payload, ls))

print()
print("a strength alone (no style chosen) adds nothing")
captured.clear()
payload, code = call(req("ref2v", {"style_1": "", "style_1_strength": 0.8}))
check("no LoRA node", payload and payload.get("ok") is True and not loras(captured[-1]["graph"]))

print()
print("a Style the lane's pool does not offer is refused with the lane-name sentence")
for mode in ("ref2v", "fl2va", "continue"):
    captured.clear()
    payload, code = call(req(mode, {"style_1": "not_a_real_pack.safetensors"}))
    check("%s: 400" % mode, code == 400, str((payload, code)))
    check("%s: 'Style is not available on Video lane.'" % mode,
          payload and payload.get("error") == "Style is not available on Video lane.", payload)
    check("%s: dispatch never ran" % mode, not captured)

print()
print("no style chosen: graph identical to the same request with no style keys at all")
for mode in ("ref2v", "fl2va", "continue"):
    captured.clear()
    call(req(mode))
    base = captured[-1]["graph"]
    captured.clear()
    call(req(mode, {"style_1": "", "style_1_strength": 1.0, "style_2": ""}))
    check("%s: byte-identical" % mode,
          json.dumps(captured[-1]["graph"], sort_keys=True) == json.dumps(base, sort_keys=True))
    check("%s: no LoRA node" % mode, not loras(base))

print()
print("LTX (not legacy_dispatch -> generic path): a Style reaches the graph, a bogus one is refused")
captured.clear()
payload, code = call({"lane": "v", "kind": "video", "mode": "ltx", "confirm": True, "steps": 20,
                      "prompt": "a slow dolly shot across a quiet harbour at dawn, soft light", "seed": 7,
                      "values": {"style_1": LTX_STYLE, "style_1_strength": 0.8}})
check("ltx call succeeded", bool(payload) and payload.get("ok") is True, str((payload, code)))
ls = loras(captured[-1]["graph"]) if captured else []
check("ltx: one LoRA node with the chosen file and strength 0.8",
      len(ls) == 1 and ls[0]["inputs"]["lora_name"] == LTX_STYLE and ls[0]["inputs"]["strength_model"] == 0.8, ls)
captured.clear()
payload, code = call({"lane": "v", "kind": "video", "mode": "ltx", "confirm": True, "steps": 20,
                      "prompt": "a slow dolly shot across a quiet harbour at dawn, soft light", "seed": 7,
                      "values": {"style_1": LTX_STYLE, "style_1_strength": 0}})
ls = loras(captured[-1]["graph"]) if captured else []
check("ltx: strength 0 -> strength_model == 0.0", len(ls) == 1 and ls[0]["inputs"]["strength_model"] == 0.0, (payload, ls))
captured.clear()
payload, code = call({"lane": "v", "kind": "video", "mode": "ltx", "confirm": True, "steps": 20,
                      "prompt": "a slow dolly shot across a quiet harbour at dawn, soft light", "seed": 7,
                      "values": {"style_1": "not_a_real_pack.safetensors"}})
check("ltx: bogus style refused 400, dispatch never ran", code == 400 and not captured, str((payload, code)))

print()
print("the image path is unchanged: a Style on a Qwen image lane still renders")
IL = {"id": "q", "name": "Qwen lane", "box": "b", "note": "", "host": "127.0.0.1", "port": 1,
      "gpu": "127.0.0.1", "gpu_label": "b", "caps": ["image"]}
srv.LANE_BY_ID["q"] = IL
if IL not in srv.LANES:
    srv.LANES.append(IL)
with srv.STATE_LOCK:
    srv.LANE_STATE["q"] = {"up": True}
with srv.DISCOVERY_LOCK:
    srv.DISCOVERY["q"] = {"models": {"qwen_unet": "qwen_image_2.1_q6.gguf", "qwen_clip": "qwen3vl_8b.safetensors",
                                      "qwen_vae": "qwen_image_2.1_vae.safetensors"},
                          "pools": {"lora": ["cool_style_qwen_image.safetensors"]}, "checked": 1.0, "err": ""}
captured.clear()
payload, code = call({"lane": "q", "kind": "image", "mode": "t2i", "confirm": True,
                      "prompt": "a wide painted mountain landscape at dawn, soft light, calm mood",
                      "values": {"style_1": "cool_style_qwen_image.safetensors", "style_1_strength": 0.6}})
ls = loras(captured[-1]["graph"]) if captured else []
check("image: one LoRA node, strength 0.6", len(ls) == 1 and ls[0]["inputs"]["strength_model"] == 0.6, (payload, ls))

print()
print(("FAILED: %d" % len(FAILED)) if FAILED else "ALL PASS")
sys.exit(1 if FAILED else 0)
