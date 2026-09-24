"""Acceptance gate for D1: the generic, field-driven dispatch path.

Before this slice, server.py routed on hardcoded (kind, mode) tuples --
"if kind == 'video': ... else: fl2va" would silently build an H3 graph for
ANY unrecognised video mode. This proves the fix at the level that actually
matters: a POST to /api/generate (server.Handler.api_generate, called
directly rather than over a real socket -- read_json/send_json/dispatch are
monkeypatched, everything else is the real module).

Two synthetic packs exercise the two cases D1 named: a brand-new cap, and a
new mode under an EXISTING cap that already has a legacy pack (video). A
RED/GREEN pair proves the specific historical bug: the OLD routing logic
(reproduced inline, never re-imported) sends video/zzz to fl2va; the real
code does not.

Run: python3 tests/test_generic_dispatch.py
"""
import importlib.util, os, sys, types
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + detail) if not cond and detail else ""))
    if not cond: FAILED.append(name)

import engines

import _scratch_config  # noqa: E402 -- must run before server.py's own exec_module below

spec = importlib.util.spec_from_file_location("srv_generic", os.path.join(ROOT, "server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)

# -- two synthetic packs: a brand-new cap, and a new mode under "video" -----
vector_pack = types.ModuleType("engines._vector_fake")
vector_pack.ENGINE = {
    "id": "zzz-vector", "cap": "vector",
    "roles": {"vec_unet": ("unet", {"all": ["vector"]})},
    "provides": {"make": ["vec_unet"]},
    "words": {"vec_unet": "the vector model"},
    "graphs": {"make": lambda p, m: {"1": {"class_type": "VectorMake",
                                            "inputs": {"prompt": p.get("prompt", ""), "unet": m["vec_unet"]}}}},
    "describe": lambda m: "Vector" if m.get("vec_unet") else "",
    "fields": {"make": [{"id": "prompt", "label": "Prompt", "type": "text",
                          "tier": "primary", "group": "Content", "order": 1}]},
    "licence": {"name": "TEST", "shippable": True, "attribution": "Vector Co"},
}
sys.modules["engines._vector_fake"] = vector_pack

video_zzz_pack = types.ModuleType("engines._video_zzz_fake")
video_zzz_pack.ENGINE = {
    "id": "zzz-video-zzz", "cap": "video",
    "roles": {"zzz_unet": ("unet", {"all": ["zzzvideo"]})},
    "provides": {"zzz": ["zzz_unet"]},
    "words": {"zzz_unet": "the zzz video model"},
    "graphs": {"zzz": lambda p, m: {"1": {"class_type": "ZzzVideoMake",
                                           "inputs": {"prompt": p.get("prompt", ""), "unet": m["zzz_unet"]}}}},
    "describe": lambda m: "",
    "fields": {"zzz": [{"id": "prompt", "label": "Prompt", "type": "text",
                         "tier": "primary", "group": "Content", "order": 1}]},
    "licence": {"name": "TEST", "shippable": True, "attribution": "Zzz Co"},
}
sys.modules["engines._video_zzz_fake"] = video_zzz_pack

# B1: a pack whose graph builder raises ValueError (LTX's shape: a business-
# rule check like "context_overlap must be smaller than context_length",
# not a missing role) -- the generic dispatch path must turn this into a
# plain 400, never let it escape as an unhandled exception.
def _boom_graph(p, m):
    if p.get("overlap", 0) >= p.get("window", 10):
        raise ValueError("overlap (%r) must be smaller than window (%r)"
                          % (p.get("overlap"), p.get("window")))
    return {"1": {"class_type": "BoomMake", "inputs": {"unet": m["boom_unet"]}}}


boom_pack = types.ModuleType("engines._boom_fake")
boom_pack.ENGINE = {
    "id": "zzz-boom", "cap": "vector",
    "roles": {"boom_unet": ("unet", {"all": ["boom"]})},
    "provides": {"boom": ["boom_unet"]},
    "words": {"boom_unet": "the boom model"},
    "graphs": {"boom": _boom_graph},
    "describe": lambda m: "",
    "fields": {"boom": [
        {"id": "window", "label": "Window", "type": "int", "default": 10,
         "tier": "primary", "group": "Content", "order": 1},
        {"id": "overlap", "label": "Overlap", "type": "int", "default": 0,
         "tier": "primary", "group": "Content", "order": 2},
    ]},
    "licence": {"name": "TEST", "shippable": True, "attribution": "Boom Co"},
}
sys.modules["engines._boom_fake"] = boom_pack

# E3: a pack whose graph builder raises ValueError if it ever sees a "pic"
# key in args at all -- proves an empty file-typed value ("" for an image,
# [] for an image_list) never reaches the graph builder as a key, the same
# way an absent field never does. "strength" (a "number" field) guards the
# unrelated B1/E1 coercion behaviour ("" on a number must still 400).
def _pic_graph(p, m):
    if "pic" in p:
        raise ValueError("got pic=%r" % (p["pic"],))
    return {"1": {"class_type": "PicMake", "inputs": {"unet": m["pic_unet"]}}}


pic_pack = types.ModuleType("engines._pic_fake")
pic_pack.ENGINE = {
    "id": "zzz-pic", "cap": "pic",
    "roles": {"pic_unet": ("unet", {"all": ["pic"]})},
    "provides": {"go": ["pic_unet"]},
    "words": {"pic_unet": "the pic model"},
    "graphs": {"go": _pic_graph},
    "describe": lambda m: "",
    "fields": {"go": [
        {"id": "prompt", "label": "Prompt", "type": "text",
         "tier": "primary", "group": "Content", "order": 1},
        {"id": "pic", "label": "Pic", "type": "image",
         "tier": "primary", "group": "Content", "order": 2},
        {"id": "pics", "label": "Pics", "type": "image_list",
         "tier": "primary", "group": "Content", "order": 3},
        {"id": "strength", "label": "Strength", "type": "number", "default": 1.0,
         "tier": "primary", "group": "Content", "order": 4},
    ]},
    "licence": {"name": "TEST", "shippable": True, "attribution": "Pic Co"},
}
sys.modules["engines._pic_fake"] = pic_pack

engines._PACKS = None
_real = engines.packs()
engines.packs.__globals__["_PACKS"] = sorted(
    _real + [vector_pack.ENGINE, video_zzz_pack.ENGINE, boom_pack.ENGINE, pic_pack.ENGINE], key=lambda e: e["id"])
srv.engines._PACKS = engines.packs.__globals__["_PACKS"]

print("D1 sanity: the synthetic modes are NOT legacy, and video/zzz shares a cap with a legacy pack")
check("vector/make is not legacy", engines.legacy_dispatch("vector", "make") is False)
check("video/zzz is not legacy", engines.legacy_dispatch("video", "zzz") is False)
check("video HAS a legacy pack (minimax-h3)", engines.has_legacy("video") is True)
check("vector has NO legacy pack", engines.has_legacy("vector") is False)

print()
print("RED: the OLD hardcoded routing (reproduced inline, not re-imported) misroutes video/zzz to fl2va")
def pre_fix_qmode(kind, mode):
    if kind == "image":
        return "edit" if mode == "edit" else "t2i"
    elif kind == "video":
        return "ref2v" if mode == "ref2v" else "fl2va"
    return mode
check("RED: the pre-fix routing sends video/zzz to fl2va, not zzz",
      pre_fix_qmode("video", "zzz") == "fl2va")

# -- a fake lane, discovered with exactly the synthetic packs' roles --------
LANE = {"id": "synth", "name": "Synth", "box": "b", "note": "", "host": "127.0.0.1", "port": 1,
        # E2: "audio" and "image" added for the yue2 reserved-key-collision
        # cases and the plain-t2i regression check below -- real packs
        # (audio/qwen_image), same synthetic lane.
        "caps": ["video", "vector", "audio", "image", "pic"]}
srv.LANE_BY_ID[LANE["id"]] = LANE
if LANE not in srv.LANES:
    srv.LANES.append(LANE)
with srv.STATE_LOCK:
    srv.LANE_STATE[LANE["id"]] = {"up": True}
with srv.DISCOVERY_LOCK:
    srv.DISCOVERY[LANE["id"]] = {"models": {"vec_unet": "vector_v1.safetensors", "zzz_unet": "zzzvideo_v1.safetensors",
                                            "boom_unet": "boom_v1.safetensors",
                                            "yue2_ckpt": "yue2_v1.safetensors",
                                            "qwen_unet": "qwen_v1.safetensors", "qwen_clip": "qwen_clip_v1.safetensors",
                                            "qwen_vae": "qwen_vae_v1.safetensors",
                                            "pic_unet": "pic_v1.safetensors"},
                                 "pools": {}, "checked": 1.0, "err": ""}

captured = []
_orig_dispatch = srv.dispatch
def fake_dispatch(lane, graph, kind, mode, meta):
    captured.append({"lane": lane["id"], "graph": graph, "kind": kind, "mode": mode, "meta": meta})
    return {"ok": True, "job": {"id": "fake"}, "notes": []}
srv.dispatch = fake_dispatch


def call_api_generate(body):
    obj = srv.Handler.__new__(srv.Handler)
    obj.read_json = lambda: body
    results = []
    obj.send_json = lambda payload, code=200: results.append((payload, code))
    srv.Handler.api_generate(obj)
    return results[-1] if results else (None, None)


print()
print("GREEN: video/zzz dispatches through the generic path to ITS OWN graph, never fl2va")
captured.clear()
payload, code = call_api_generate({"lane": "synth", "kind": "video", "mode": "zzz", "prompt": "a zzz test"})
check("video/zzz call succeeded", payload and payload.get("ok") is True, str(payload))
check("video/zzz dispatched with mode 'zzz', not 'fl2va'",
      captured and captured[-1]["mode"] == "zzz", str(captured))
check("video/zzz built ITS OWN graph (class_type ZzzVideoMake)",
      captured and captured[-1]["graph"].get("1", {}).get("class_type") == "ZzzVideoMake", str(captured))
check("video/zzz graph is NOT an H3 graph", "fl2va" not in str(captured[-1]["graph"]) if captured else False)

print()
print("GREEN: a brand-new cap (vector/make) dispatches through the generic path to its own graph")
captured.clear()
payload, code = call_api_generate({"lane": "synth", "kind": "vector", "mode": "make", "prompt": "a vector test"})
check("vector/make call succeeded", payload and payload.get("ok") is True, str(payload))
check("vector/make dispatched with mode 'make'",
      captured and captured[-1]["mode"] == "make", str(captured))
check("vector/make built ITS OWN graph (class_type VectorMake)",
      captured and captured[-1]["graph"].get("1", {}).get("class_type") == "VectorMake", str(captured))

print()
print("GREEN: an unknown (kind, mode) refuses cleanly, no fallback to another mode")
payload, code = call_api_generate({"lane": "synth", "kind": "vector", "mode": "no-such-mode"})
check("unknown mode refuses with 400", code == 400, str((payload, code)))
check("unknown mode error names both kind and mode",
      payload and "vector" in payload.get("error", "") and "no-such-mode" in payload.get("error", ""),
      str(payload))

print()
print("RED: a pack's own ValueError, reproduced with no except around it, escapes as a raw exception")
try:
    engines.graph_for("vector", "boom", {"window": 10, "overlap": 40}, {"boom_unet": "boom_v1.safetensors"})
    red_escaped = False
except ValueError:
    red_escaped = True
check("RED: graph_for alone raises ValueError uncaught (the shape B1 guards against)", red_escaped)

print()
print("GREEN: the generic dispatch path turns that same ValueError into a plain 400, not a server error")
captured.clear()
payload, code = call_api_generate({"lane": "synth", "kind": "vector", "mode": "boom", "window": 10, "overlap": 40})
check("boom call did not raise, and refused with 400", code == 400, str((payload, code)))
check("boom error carries the pack's own message",
      payload and "overlap" in payload.get("error", "") and "window" in payload.get("error", ""),
      str(payload))

print()
print("GREEN: a malformed field value (coercion ValueError, not the pack's own check) is ALSO a plain 400")
payload, code = call_api_generate({"lane": "synth", "kind": "vector", "mode": "boom", "window": "", "overlap": 0})
check("malformed int field refuses with 400, not a server error", code == 400, str((payload, code)))

print()
print("E2: yue2's own 'mode' field (reserved-key collision with the dispatch mode) reads from body.values")
captured.clear()
payload, code = call_api_generate({"lane": "synth", "kind": "audio", "mode": "yue2",
                                    "style": "test style", "values": {"mode": "melody"}})
check("yue2 with values.mode='melody' call succeeded", payload and payload.get("ok") is True, str(payload))
check("yue2 dispatched with the dispatch mode 'yue2', not the field's value",
      captured and captured[-1]["mode"] == "yue2", str(captured))
yue2_node = (captured[-1]["graph"].get("25") or {}).get("inputs", {}) if captured else {}
check("yue2's own graph node got the field's chosen mode 'melody'",
      yue2_node.get("mode") == "melody", repr(yue2_node))

captured.clear()
payload, code = call_api_generate({"lane": "synth", "kind": "audio", "mode": "yue2", "style": "test style"})
check("yue2 with no values at all still succeeds", payload and payload.get("ok") is True, str(payload))
yue2_node = (captured[-1]["graph"].get("25") or {}).get("inputs", {}) if captured else {}
check("yue2's own graph node falls back to the field's declared default 'full' (never the dispatch mode 'yue2')",
      yue2_node.get("mode") == "full", repr(yue2_node))

captured.clear()
payload, code = call_api_generate({"lane": "synth", "kind": "image", "mode": "t2i", "prompt": "a plain t2i request"})
check("a plain t2i request (no 'values' key at all) is unaffected by E2",
      payload and payload.get("ok") is True, str(payload))
check("t2i dispatched with mode 't2i'", captured and captured[-1]["mode"] == "t2i", str(captured))

print()
print("E3: an empty file-typed value (\"\" image / [] image_list) never reaches the graph builder as a key")
captured.clear()
payload, code = call_api_generate({"lane": "synth", "kind": "pic", "mode": "go",
                                    "values": {"prompt": "x", "pic": "", "pics": []}})
check("empty pic/pics call succeeded (no 'pic' key reached the builder)",
      payload and payload.get("ok") is True, str((payload, code)))

captured.clear()
payload, code = call_api_generate({"lane": "synth", "kind": "pic", "mode": "go",
                                    "values": {"prompt": "x", "pic": "a.png"}})
check("a real pic value DOES reach the builder (proven by its own raise carrying the value)",
      code == 400 and payload and "a.png" in payload.get("error", ""), str((payload, code)))

payload, code = call_api_generate({"lane": "synth", "kind": "pic", "mode": "go",
                                    "values": {"prompt": "x", "strength": ""}})
check("an empty NUMBER field still 400s with 'needs a number.' (E3 must not touch B1/E1)",
      code == 400 and payload and payload.get("error", "").endswith("needs a number."), str((payload, code)))

print()
print("Q21: sampler/cfg reach the legacy image graph via the generic field fallback")
captured.clear()
payload, code = call_api_generate({"lane": "synth", "kind": "image", "mode": "t2i",
                                    "prompt": "a q21 request", "sampler": "euler"})
check("sampler=euler call succeeded", payload and payload.get("ok") is True, str(payload))
ks = (captured[-1]["graph"].get("8") or {}).get("inputs", {}) if captured else {}
check("sampler=euler reached the graph's KSampler", ks.get("sampler_name") == "euler", repr(ks))

payload, code = call_api_generate({"lane": "synth", "kind": "image", "mode": "t2i",
                                    "prompt": "a q21 request", "sampler": "bogus"})
check("sampler=bogus refuses with 400", code == 400, str((payload, code)))
check("sampler=bogus names the field and 'must be one of'",
      payload and "Sampling method" in payload.get("error", "") and "must be one of" in payload.get("error", ""),
      str(payload))

captured.clear()
payload, code = call_api_generate({"lane": "synth", "kind": "image", "mode": "t2i",
                                    "prompt": "a q21 request"})
check("omitting cfg call succeeded", payload and payload.get("ok") is True, str(payload))
ks = (captured[-1]["graph"].get("8") or {}).get("inputs", {}) if captured else {}
check("omitting cfg uses the field's declared default (3)", ks.get("cfg") == 3.0, repr(ks))

srv.dispatch = _orig_dispatch

print()
print(("FAILED: %d" % len(FAILED)) if FAILED else "ALL PASS")
sys.exit(1 if FAILED else 0)
