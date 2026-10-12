"""Gate for "What will run" (slice 1) and the pin / licence fixes under it. No
browser, no network: server.py in process, discovery fed a fixture pool.

1. ONE ranking: pick_model(pool, rule, s) == role_candidates(pool, rule, s)[0]
   for every pack rule, both card sizes, over a pool holding turbo, GGUF, int8
   and bf16 builds -- discovery, Setup and the panel cannot disagree.
2. /api/engines t2i lists every picture part in `parts`, the exact file each
   will load, its ranked candidates with reasons, and the Turbo build under
   `refused` with the term and the pack's own reason.
3. config.json pins: a pin naming a file the lane's pool lacks makes the mode
   unavailable with a config.json sentence (also refused by generate); a pin
   in the pool that does not fit the rule runs, shown as CONFIG with a note;
   before discovery has answered, no pin is judged.
4. Licence stamps carry structured fields: Qwen-Image, YuE2-3B and
   SheetSage2 non-commercial, MiniMax-H3 its territory.

Run: python3 tests/test_what_will_run.py
"""
import importlib.util
import json
import os
import sys

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FAILED = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail else ""))
    if not cond:
        FAILED.append(name)


import _scratch_config  # noqa: E402,F401 -- before server.py's own exec_module

spec = importlib.util.spec_from_file_location("srv_wwr", os.path.join(ROOT, "server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)
import engines  # noqa: E402

INT8 = "qwen_image_2.1_int8_convrot.safetensors"
GGUF = "qwen_image_2.1_Q6_K.gguf"
BF16 = "qwen_image_2.1_bf16.safetensors"
TURBO = "qwen_image_2.1_turbo_int8.safetensors"
RIG = json.load(open(os.path.join(HERE, "golden", "pools_rig.json")))
RIG["unet"]["UNETLoader.unet_name"][0] = RIG["unet"]["UNETLoader.unet_name"][0] + [INT8, BF16, TURBO]


def fake_http_get_json(url, timeout=8.0):
    node = url.rsplit("/", 1)[-1]
    for entries in RIG.values():
        for key, opts in entries.items():
            if key.split(".")[0] == node:
                return {node: {"input": {"required": {key.split(".", 1)[1]: opts}}}}
    return {}


srv.http_get_json = fake_http_get_json


def lane(lid, **extra):
    l = dict({"id": lid, "name": "Lane " + lid, "box": "", "note": "", "host": "127.0.0.1", "port": 1,
              "gpu": "", "gpu_label": "", "caps": ["image", "video", "audio"]}, **extra)
    srv.LANE_BY_ID[lid] = l
    srv.LANES.append(l)
    with srv.STATE_LOCK:
        srv.LANE_STATE[lid] = {"up": True, "vram_total": 16 * 2 ** 30}
    return l


def payload(lid):
    return srv.Handler.engines_payload(srv.Handler.__new__(srv.Handler), {"lane": [lid]})


def mode(p, cap, m):
    return next(x for x in p[cap]["modes"] if x["id"] == m)


print("1. one ranking: pick_model is role_candidates' first entry, for every pack rule")
pools = {p: srv.fetch_pool({"id": "x", "name": "x", "host": "h", "port": 1}, p) for p in srv.POOL_NODES}
checked = 0
for role, rule in sorted(srv.ROLE_RULES.items()):
    pool = pools[srv.ROLE_POOL[role]] + [INT8, GGUF, BF16, TURBO]
    for small in (True, False):
        ranked = srv.role_candidates(pool, rule, small)
        got = srv.pick_model(pool, rule, small)
        if not (got == (ranked[0]["file"] if ranked else None)):
            check("%s small=%s: pick_model == role_candidates[0]" % (role, small), False, (got, ranked[:1]))
        checked += 1
check("pick_model == role_candidates()[0]['file'] for all %d (role, card) pairs" % checked,
      checked == 2 * len(srv.ROLE_RULES) and not FAILED)
ranked = srv.role_candidates([BF16, GGUF, INT8, TURBO], srv.ROLE_RULES["qwen_unet"], True)
check("the ranked list is in rank order, the refused Turbo build absent",
      [c["file"] for c in ranked] == [INT8, GGUF, BF16], [c["file"] for c in ranked])
check("each candidate says why, in the rule's own terms",
      ranked[0]["why"].startswith("Ranked first") and '"int8" (wanted most)' in ranked[0]["why"]
      and ranked[1]["quant"] == "GGUF", [c["why"] for c in ranked])
refused = srv.role_refused([BF16, GGUF, INT8, TURBO], srv.ROLE_RULES["qwen_unet"])
check("role_refused names the Turbo build, its term and the pack's reason",
      [(x["file"], x["term"]) for x in refused] == [(TURBO, "turbo")]
      and "background detail" in (refused[0]["why"] or ""), refused)

print("\n2. /api/engines: every part of t2i, the file it loads, and what was left out")
rig = lane("rig")
srv.discover_lane(rig)
t2i = mode(payload("rig"), "image", "t2i")
parts = t2i["parts"]
check("all three picture roles are parts", [p["roles"] for p in parts] == [["qwen_unet"], ["qwen_clip"], ["qwen_vae"]],
      [p["roles"] for p in parts])
m = srv.models_for(rig)
check("each part's file is exactly the file the graph will be given",
      all(p["file"] == m[p["roles"][0]] for p in parts) and parts[0]["file"] == INT8, [p["file"] for p in parts])
check("the picture model's candidates are ranked, the auto pick flagged",
      [c["file"] for c in parts[0]["candidates"]][:3] == [INT8, GGUF, BF16] and parts[0]["candidates"][0]["auto"]
      and parts[0]["source"] == "auto", parts[0]["candidates"])
check("the Turbo file is under refused with term 'turbo' and the pack's reason",
      [(x["file"], x["term"]) for x in parts[0]["refused"]] == [(TURBO, "turbo")]
      and "background detail" in parts[0]["refused"][0]["why"], parts[0]["refused"])
check("no pin: pinned is null", all(p["pinned"] is None for p in parts))
fl2va = mode(payload("rig"), "video", "fl2va")
enc = next(p for p in fl2va["parts"] if "h3_clip_int8" in p["roles"])
check("the any-of text encoder is ONE part, in the order the graph reads it",
      enc["roles"] == ["h3_clip_int8", "h3_clip_nvfp4"] and enc["file"] == m["h3_clip_int8"], enc["roles"])
speed = next(p for p in fl2va["parts"] if p["roles"] == ["h3_turbo_lora"])
check("the speed pack is a part only some quality tiers load",
      speed["tiers"] == [t["id"] for t in engines.quality("video", "fl2va") if t.get("requires")], speed["tiers"])
check("a process-lane mode has no parts", mode(payload("rig"), "3d", "turntable")["parts"] == [])

print("\n3. config.json pins")
gone = lane("gone", models={"qwen_vae": "not_here_vae.safetensors"})
srv.discover_lane(gone)
t2i = mode(payload("gone"), "image", "t2i")
want = "config.json names not_here_vae.safetensors, which Lane gone does not have."
check("a lane pin naming a file the pool lacks makes t2i unavailable",
      t2i["available"] is False, t2i["available"])
check("... with the config.json sentence in missing", any(x.startswith(want) for x in t2i["missing"]), t2i["missing"])
vae = next(p for p in t2i["parts"] if p["roles"] == ["qwen_vae"])
check("... and the part says so, its file empty (no silent fallback to the discovered one)",
      vae["file"] is None and vae["problem"].startswith(want) and vae["pinned"]["in_pool"] is False
      and vae["pinned"]["level"] == "lane", vae)
body, code = srv.generate({"lane": "gone", "kind": "image", "mode": "t2i", "prompt": "a lighthouse"})
check("generate refuses it with the same sentence", code == 400 and want in body.get("error", ""), (code, body))

odd = lane("odd", models={"qwen_vae": "qwen_image_vae.safetensors", "qwen_clip": "wan22/umt5_xxl_fp8_e4m3fn_scaled.safetensors"})
srv.discover_lane(odd)
t2i = mode(payload("odd"), "image", "t2i")
clip = next(p for p in t2i["parts"] if p["roles"] == ["qwen_clip"])
check("a pin in the pool that does not fit the rule still runs (the escape hatch)",
      t2i["available"] is True and clip["file"] == "wan22/umt5_xxl_fp8_e4m3fn_scaled.safetensors"
      and clip["source"] == "config", (t2i["available"], clip["file"], clip["source"]))
check("... shown with fits_rule false and a note", clip["pinned"]["fits_rule"] is False
      and clip["pinned"]["in_pool"] is True and "does not fit" in clip["why"], clip)
vae = next(p for p in t2i["parts"] if p["roles"] == ["qwen_vae"])
check("a fitting pin is CONFIG, the rule's own pick still visible as auto",
      vae["source"] == "config" and vae["why"] == "Set in config.json for Lane odd."
      and any(c["auto"] and c["file"] != vae["file"] for c in vae["candidates"]), vae)

anyof = lane("anyof", models={"h3_clip_int8": "nope/absent_int8.safetensors"})
srv.discover_lane(anyof)
fl2va = mode(payload("anyof"), "video", "fl2va")
want = "config.json names nope/absent_int8.safetensors, which Lane anyof does not have."
check("an absent pin on one member of an any-of part makes the mode unavailable (no stand-in from the other)",
      fl2va["available"] is False and any(x.startswith(want) for x in fl2va["missing"]),
      (fl2va["available"], fl2va["missing"]))
enc = next(p for p in fl2va["parts"] if "h3_clip_int8" in p["roles"])
check("... and that part names no file to run", enc["file"] is None and enc["problem"].startswith(want), enc)
body, code = srv.generate({"lane": "anyof", "kind": "video", "mode": "fl2va", "prompt": "a", "confirm": True})
check("... and generate refuses it", code >= 400 and want in body.get("error", ""), (code, body))
check("... and the lane listing does not offer video from it alone",
      "fl2va" not in [k for k, v in srv.abilities(anyof).items() if v], srv.abilities(anyof).get("fl2va"))

srv.CONFIG_MODELS["qwen_unet"] = "nowhere_unet.gguf"
t2i = mode(payload("rig"), "image", "t2i")
check("a top-level pin the lane lacks counts too",
      t2i["available"] is False and any("nowhere_unet.gguf" in x for x in t2i["missing"]), t2i["missing"])
early = lane("early", models={"qwen_vae": "not_here_vae.safetensors"})
check("before discovery has answered, no pin is judged",
      srv.pin_absent(early) == {} and srv.models_for(early).get("qwen_vae") == "not_here_vae.safetensors")
del srv.CONFIG_MODELS["qwen_unet"]

print("\n3a. a refusal never suggests the lane that refused, nor one that cannot run the mode")
saved_lanes = list(srv.LANES)
srv.LANES[:] = [srv.LANE_BY_ID["gone"]]   # can still clean up pictures, cannot make one (absent pin)
body, code = srv.generate({"lane": "gone", "kind": "image", "mode": "t2i", "prompt": "a lighthouse"})
check("one lane, refusing t2i: the error does not say 'Use Lane gone.'",
      code == 400 and "Use Lane gone." not in body.get("error", "")
      and "No machine here has the picture models installed yet." in body.get("error", ""), body)
srv.LANES[:] = [srv.LANE_BY_ID["gone"], srv.LANE_BY_ID["rig"]]
body, code = srv.generate({"lane": "gone", "kind": "image", "mode": "t2i", "prompt": "a lighthouse"})
check("two lanes: the one that can make the picture is suggested, the refusing one is not",
      code == 400 and body.get("error", "").endswith("Use Lane rig."), body)
srv.LANES[:] = saved_lanes

print("\n3b. Setup layers the pins of the configured lane at the probed address")
pools_rig = {p: srv.pool_names(p, lambda node: fake_http_get_json("/object_info/" + node), "x") for p in srv.POOL_NODES}
srv._setup_pools = lambda h, p: pools_rig
srv.SETUP_COMFY_SEEN.add(("127.0.0.9", 7))
srv.SETUP_VRAM[("127.0.0.9", 7)] = 16 * 2 ** 30
lane("setuplane", host="127.0.0.9", port=7, models={"qwen_vae": "qwen_image_vae.safetensors"})
lane("elsewhere", host="127.0.0.8", port=7, models={"qwen_clip": "wan22/umt5_xxl_fp8_e4m3fn_scaled.safetensors"})
rooms_out = srv.setup_rooms({"host": ["127.0.0.9"], "port": ["7"]})[0]["rooms"]
pic = {r["role"]: r for r in next(x for x in rooms_out if x["id"] == "picture")["roles"]}
check("Setup names the lane's own pinned file for that lane's address",
      pic["qwen_vae"]["found"] == "qwen_image_vae.safetensors", pic["qwen_vae"]["found"])
check("... and not another lane's pin", pic["qwen_clip"]["found"] == "qwen3vl_8b_int8_convrot.safetensors",
      pic["qwen_clip"]["found"])
srv.LANE_BY_ID["setuplane"]["models"] = {"qwen_vae": "nope_vae.safetensors"}
rooms_out = srv.setup_rooms({"host": ["127.0.0.9"], "port": ["7"]})[0]["rooms"]
pic = {r["role"]: r for r in next(x for x in rooms_out if x["id"] == "picture")["roles"]}
check("a pinned file that address lacks is not installed in Setup either",
      pic["qwen_vae"]["installed"] is False and pic["qwen_vae"]["found"] is None, pic["qwen_vae"])

print("\n4. licence stamps from structured fields")
p = payload("rig")
qwen = mode(p, "image", "t2i")["licence"]
check("Qwen-Image: noncommercial true (its name says neither NC nor non-commercial)",
      qwen.get("noncommercial") is True and "nc" not in qwen["name"].lower().split(), qwen)
check("Pixel Art (same model): noncommercial", mode(p, "image", "pixelart")["licence"].get("noncommercial") is True)
for m_id in ("yue2", "cover"):
    check("YuE2 %s: noncommercial" % m_id, mode(p, "audio", m_id)["licence"].get("noncommercial") is True)
cover = mode(p, "audio", "cover")
sheet = next(x for x in cover["parts"] if x["roles"] == ["yue2_audio_encoder"])
check("SheetSage2's own part stamp: noncommercial", (sheet["licence"] or {}).get("noncommercial") is True, sheet["licence"])
h3 = mode(p, "video", "fl2va")["licence"]
check("MiniMax-H3: territory names where it does not reach",
      "South Korea" in (h3.get("territory") or "") and not h3.get("noncommercial"), h3)
check("a permissive licence carries neither", not {"noncommercial", "territory"} & set(mode(p, "image", "upscale")["licence"]))
check("the job record gets the same fields (licence_for)", engines.licence_for("image", "t2i").get("noncommercial") is True)

print("\nFAILED: %d" % len(FAILED) + (": " + ", ".join(FAILED) if FAILED else ""))
sys.exit(1 if FAILED else 0)
