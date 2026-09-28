"""LORA-1 Build A acceptance gate: the "pool_select" field type end to end --
its live options come from the LANE's own discovered "lora" pool (never a
hard-coded list), and a value outside that pool is refused before it ever
reaches the render graph. Same harness shape as tests/test_generic_dispatch.py
(a real srv.Handler, dispatch() monkeypatched to capture the graph).

Run: python3 tests/test_pool_select.py
"""
import importlib.util, os, sys
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail else ""))
    if not cond: FAILED.append(name)

import _scratch_config  # noqa: E402 -- must run before server.py's own exec_module below

spec = importlib.util.spec_from_file_location("srv_pool", os.path.join(ROOT, "server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)

LANE = {"id": "q", "name": "Qwen lane", "box": "b", "note": "", "host": "127.0.0.1", "port": 1,
        "gpu": "127.0.0.1", "gpu_label": "b", "caps": ["image"]}
srv.LANE_BY_ID[LANE["id"]] = LANE
if LANE not in srv.LANES:
    srv.LANES.append(LANE)
with srv.STATE_LOCK:
    srv.LANE_STATE[LANE["id"]] = {"up": True}
LORA_POOL = ["cool_style_qwen_image.safetensors", "another_qwen-image_pack.safetensors",
             "h3_turbo_8step.safetensors", "unrelated.safetensors"]
with srv.DISCOVERY_LOCK:
    srv.DISCOVERY[LANE["id"]] = {
        "models": {"qwen_unet": "qwen_image_2.1_q6.gguf", "qwen_clip": "qwen3vl_8b.safetensors",
                   "qwen_vae": "qwen_image_2.1_vae.safetensors"},
        "pools": {"lora": LORA_POOL}, "checked": 1.0, "err": ""}

print("pool_select_options: only lora-pool files matching the field's own rule")
field = {"pool": "lora", "match": {"any": ["qwen_image", "qwen-image"]}}
got = srv.pool_select_options(LANE, field)
check("both qwen_image-family files are offered", set(got) == {"cool_style_qwen_image.safetensors",
                                                                "another_qwen-image_pack.safetensors"}, got)
check("the turbo LoRA and the unrelated file are NOT offered", "h3_turbo_8step.safetensors" not in got
      and "unrelated.safetensors" not in got)

print()
print("engines_payload: a pool_select field's options come from THIS lane, per request")
obj = srv.Handler.__new__(srv.Handler)
payload = srv.Handler.engines_payload(obj, {"lane": [LANE["id"]]})
fields = next(m for m in payload["image"]["modes"] if m["id"] == "t2i")["fields"]
style1 = next(f for f in fields if f["id"] == "style_1")
check("style_1's options are [\"\"] plus the two matching files",
      set(style1["options"]) == {"", "cool_style_qwen_image.safetensors", "another_qwen-image_pack.safetensors"},
      style1["options"])
no_lane_payload = srv.Handler.engines_payload(obj, {})
style1_no_lane = next(f for f in
                      next(m for m in no_lane_payload["image"]["modes"] if m["id"] == "t2i")["fields"]
                      if f["id"] == "style_1")
check("with no lane, the static declaration is returned unchanged (no 'options' key added)",
      "options" not in style1_no_lane, style1_no_lane)

print()
print("lanes_payload exposes whether THIS lane opted in to downloads")
def q_lane_payload():
    return next(l for l in srv.Handler.lanes_payload(obj)["lanes"] if l["id"] == "q")
check("no 'downloads' on the lane -> False", q_lane_payload()["downloads"] is False)
LANE["downloads"] = {"loras_dir": "/tmp/does-not-need-to-exist-for-this-check"}
check("'downloads' present with a loras_dir -> True", q_lane_payload()["downloads"] is True)
del LANE["downloads"]

captured = []
srv.dispatch = lambda lane, graph, kind, mode, meta: (
    captured.append({"graph": graph, "meta": meta}) or {"ok": True, "job": {"id": "fake"}, "notes": []})


def call_api_generate(body):
    o = srv.Handler.__new__(srv.Handler)
    o.read_json = lambda: body
    results = []
    o.send_json = lambda payload, code=200: results.append((payload, code))
    srv.Handler.api_generate(o)
    return results[-1] if results else (None, None)


print()
print("api_generate: a style from the lane's own pool renders with a LoraLoaderModelOnly node")
payload, code = call_api_generate({"lane": "q", "kind": "image", "mode": "t2i", "confirm": True,
                                   "prompt": "a wide painted mountain landscape at dawn, soft light, calm mood",
                                   "values": {"style_1": "cool_style_qwen_image.safetensors",
                                              "style_1_strength": 0.6}})
check("the call succeeded", payload and payload.get("ok") is True, str((payload, code)))
graph = captured[-1]["graph"] if captured else {}
lora_nodes = [n for n in graph.values() if n.get("class_type") == "LoraLoaderModelOnly"]
check("exactly one LoraLoaderModelOnly node, carrying the chosen strength",
      len(lora_nodes) == 1 and lora_nodes[0]["inputs"]["strength_model"] == 0.6, lora_nodes)

print()
print("api_generate: a style NOT in the lane's pool is refused before it reaches the graph")
captured.clear()
payload, code = call_api_generate({"lane": "q", "kind": "image", "mode": "t2i", "confirm": True,
                                   "prompt": "a wide painted mountain landscape at dawn, soft light, calm mood",
                                   "values": {"style_1": "not_a_real_pack.safetensors"}})
check("refused with 400", code == 400, str((payload, code)))
check("the error names the field", payload and "Style" in payload.get("error", ""), payload)
check("dispatch() never ran (no graph was built)", not captured)

print()
print(("FAILED: %d" % len(FAILED)) if FAILED else "ALL PASS")
sys.exit(1 if FAILED else 0)
