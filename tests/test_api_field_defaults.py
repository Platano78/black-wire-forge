"""v1.4.4: an API caller that leaves a declared field out gets the field's own
declared default, exactly as the page does -- not the graph builder's last-resort
fallback.

Before: POST /api/generate for a bare t2i (no guidance_style / sampler /
scheduler) ran the pre-2026-10-03 Balanced recipe (APG + FreSca, seeds_2 /
sgm_uniform), because `_fill_declared_fields` only filled a declared field from
the REQUEST and `qwen_t2i_graph` falls back to Balanced for callers that build
params without those fields (pixel art and the goldens, which stay as they are).
Owner ruling 2026-10-03: Default = raw cfg 1 / Plain / euler / simple, for every
caller. This drives the real dispatch (server.Handler.api_generate, called
directly, dispatch() replaced by a capture) and reads the graph that was built.

Run: python3 tests/test_api_field_defaults.py
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

import engines
import _scratch_config  # noqa: E402,F401 -- must run before server.py's exec_module
spec = importlib.util.spec_from_file_location("srv_field_defaults", os.path.join(ROOT, "server.py"))
srv = importlib.util.module_from_spec(spec); spec.loader.exec_module(srv)

LANE = {"id": "fd", "name": "FD", "box": "b", "note": "", "host": "127.0.0.1", "port": 1, "caps": ["image"]}
srv.LANE_BY_ID[LANE["id"]] = LANE
if LANE not in srv.LANES: srv.LANES.append(LANE)
with srv.STATE_LOCK: srv.LANE_STATE[LANE["id"]] = {"up": True}
with srv.DISCOVERY_LOCK:
    srv.DISCOVERY[LANE["id"]] = {"models": {"qwen_unet": "qwen_image_2.1_int8.safetensors",
                                            "pixel_unet": "qwen_image_2.1_int8.safetensors",
                                            "qwen_clip": "qwen3vl_8b.safetensors",
                                            "qwen_vae": "qwen_image_2.1_vae.safetensors",
                                            "birefnet_model": "birefnet.safetensors"},
                                 "pools": {}, "checked": 1.0, "err": ""}
captured = []
srv.dispatch = lambda lane, graph, kind, mode, meta: (
    captured.append({"graph": graph, "mode": mode, "meta": meta}) or {"ok": True, "job": {"id": "x"}, "notes": []})


def api(body):
    captured.clear()
    obj = srv.Handler.__new__(srv.Handler); res = []
    obj.read_json = lambda: body
    obj.send_json = lambda payload, code=200: res.append((payload, code))
    srv.Handler.api_generate(obj)
    return res[-1], (captured[-1]["graph"] if captured else None)


def classes(g): return {n.get("class_type") for n in g.values()}
def ksampler(g): return next((n["inputs"] for n in g.values() if n.get("class_type") == "KSampler"), {})

BARE = {"lane": "fd", "kind": "image", "mode": "t2i", "prompt": "a lighthouse", "width": 1328,
        "height": 1328, "steps": 40, "cfg": 1.0, "seed": 777}

print("a bare t2i call (no guidance_style / sampler / scheduler) gets the page's Default")
(pl, code), g = api(dict(BARE))
check("the call succeeded", code == 200 and pl.get("ok") and g is not None, (code, pl))
cl = classes(g or {})
check("no APG node", "APG" not in cl, sorted(cl))
check("no FreSca node", "FreSca" not in cl, sorted(cl))
ks = ksampler(g or {})
check("sampler euler", ks.get("sampler_name") == "euler", repr(ks))
check("scheduler simple", ks.get("scheduler") == "simple", repr(ks))

print()
print("a call that spells guidance_style Balanced still gets the Balanced recipe (opt-in stays)")
(pl, code), g = api(dict(BARE, guidance_style="Balanced"))
check("the call succeeded", code == 200 and pl.get("ok") and g is not None, (code, pl))
cl = classes(g or {})
check("APG node present", "APG" in cl, sorted(cl))
check("FreSca node present", "FreSca" in cl, sorted(cl))
ks = ksampler(g or {})
check("Balanced with nothing else spelled uses the declared sampler/scheduler (euler/simple)",
      (ks.get("sampler_name"), ks.get("scheduler")) == ("euler", "simple"), repr(ks))
(pl, code), g = api(dict(BARE, guidance_style="Balanced", sampler="seeds_2", scheduler="sgm_uniform"))
ks = ksampler(g or {})
check("an explicit seeds_2 / sgm_uniform is honoured", (ks.get("sampler_name"), ks.get("scheduler")) == ("seeds_2", "sgm_uniform"), repr(ks))
(pl, code), g = api(dict(BARE, sampler="seeds_2"))
check("an explicit sampler alone is honoured (and the rest default)",
      ksampler(g or {}).get("sampler_name") == "seeds_2" and "APG" not in classes(g or {}), repr(ksampler(g or {})))
(pl, code), g = api(dict(BARE, values={"guidance_style": "Balanced"}))
check("the same through body.values", "APG" in classes(g or {}), sorted(classes(g or {})))

print()
print("edit mode (declares the same fields) also takes its declared defaults")
(pl, code), g = api({"lane": "fd", "kind": "image", "mode": "edit", "prompt": "make it night", "seed": 5,
                     "ref_images": ["a.png"]})
check("the edit call built a graph", g is not None, (code, pl))
check("edit: no APG / FreSca", not ({"APG", "FreSca"} & classes(g or {})), sorted(classes(g or {})))

print()
print("which fields get filled: scalars with a default; never pool_select / files; enabled_when honoured")
args = {"prompt": "x", "steps": 40, "cfg": 1.0}
err = srv._fill_declared_fields({"prompt": "x"}, LANE, "image", "t2i", args)
check("no error", err is None, repr(err))
check("guidance_style filled with its declared default", args.get("guidance_style") == "Plain", repr(args))
check("sampler / scheduler filled", (args.get("sampler"), args.get("scheduler")) == ("euler", "simple"), repr(args))
a2 = {}
e2 = srv._fill_declared_fields({"sampler": "seeds_2"}, LANE, "image", "t2i", a2)
check("a request value is never overwritten by a default", e2 is None and a2.get("sampler") == "seeds_2", repr((e2, a2)))
check("pool_select style_1 is not filled with its '' default", "style_1" not in args, repr(args))
check("style_1_strength (enabled_when style_1 truthy) is not filled", "style_1_strength" not in args, repr(args))
check("APG numbers (enabled_when guidance_style == Balanced) are not filled under Plain",
      not any(k.startswith(("apg_", "fresca_")) for k in args), repr(args))
a3 = {}
srv._fill_declared_fields({"guidance_style": "Balanced"}, LANE, "image", "t2i", a3)
check("under Balanced the APG / FreSca numbers take their declared defaults",
      a3.get("apg_eta") == 1.0 and a3.get("fresca_scale_high") == 2.0 and a3.get("fresca_freq_cutoff") == 8, repr(a3))
a4 = {}
srv._fill_declared_fields({}, LANE, "video", "fl2va", a4, ("pool_select",))
check("a typed fill (video's pool_select pass) fills no defaults", a4 == {}, repr(a4))

print()
print("pixel art does not pass through this fill and still builds its Balanced graph")
(pl, code), g = api({"lane": "fd", "kind": "image", "mode": "pixelart", "confirm": True,
                     "prompt": "Pixel art sprite, a knight", "seed": 3,
                     "values": {"width": 1024, "height": 1024, "resolution": 1024, "steps": 20, "cfg": 2.5}})
check("the pixel art call built a graph", code == 200 and g is not None, (code, pl))
cl = classes(g or {})
check("pixel art keeps APG + FreSca (Balanced)", {"APG", "FreSca"} <= cl, sorted(cl))
ks = ksampler(g or {})
check("pixel art keeps seeds_2 / sgm_uniform", (ks.get("sampler_name"), ks.get("scheduler")) == ("seeds_2", "sgm_uniform"), repr(ks))

print()
if FAILED:
    print("FAILED: %d" % len(FAILED)); sys.exit(1)
print("ALL PASS")
