"""TaoMate 4-step fast draft (fast_kind "taomate"), fl2va only.

(a) graph: LoraLoaderModelOnly(taomate) -> MiniMaxH3SigmaShift(12, 3), scheduler beta, sampler er_sde,
    the request's steps, no turbo LoRA; no fast_kind (or "turbo") is exactly today's turbo graph.
(b) ability: true only with the LoRA file AND the MiniMaxH3SigmaShift node on the lane.
(c) generate(): fl2va + taomate renders it; missing ability, continue, ref2v and an unknown kind are
    refused with a sentence; without turbo_lora fast_kind is inert.
(d) the preset is offered only where the ability is met; the quality ladder is untouched
    (tests/test_api_engines.py pins its steps and tiers, so no ladder row).

In-process (server.py loaded, dispatch stubbed): no ComfyUI, no network.
Run: python3 tests/test_taomate_draft.py"""
import importlib.util
import json
import os
import sys

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail != "" else ""))
    if not cond:
        FAILED.append(name)

import _scratch_config  # noqa: E402,F401 -- before server.py's own import-time config read
import engines  # noqa: E402

G = os.path.join(HERE, "golden")
MODELS = json.load(open(os.path.join(G, "models.json")))
ARGS = json.load(open(os.path.join(G, "args.json")))["vid"]
TAO = "minimax_h3/minimax_h3_taomate_fl2va_3step_avg_rank_19_bf16.safetensors"
MODELS_TAO = dict(MODELS, h3_taomate_lora=TAO)

def by_class(g, cls):
    return [n for n in g.values() if n.get("class_type") == cls]

print("graph: taomate chain")
req = dict(ARGS, steps=4, turbo_lora=True, fast_kind="taomate")
g = engines.graph_for("video", "fl2va", req, MODELS_TAO)
loras = by_class(g, "LoraLoaderModelOnly")
check("one LoRA loader, the taomate file at 1.0",
      len(loras) == 1 and loras[0]["inputs"]["lora_name"] == TAO and loras[0]["inputs"]["strength_model"] == 1.0, loras)
check("no turbo LoRA in the graph", all(n["inputs"]["lora_name"] != MODELS["h3_turbo_lora"] for n in loras))
sig = by_class(g, "MiniMaxH3SigmaShift")
check("one sigma shift, 12 / 3, reading the LoRA",
      len(sig) == 1 and sig[0]["inputs"] == {"model": ["7", 0], "shift_video": 12.0, "shift_audio": 3.0}, sig)
check("guider and scheduler read the sigma shift", g["16"]["inputs"]["model"] == ["8", 0]
      and g["9"]["inputs"]["model"] == ["8", 0])
check("scheduler beta, 4 steps", g["9"]["inputs"]["scheduler"] == "beta" and g["9"]["inputs"]["steps"] == 4, g["9"])
check("sampler er_sde", g["17"]["inputs"]["sampler_name"] == "er_sde", g["17"])
check("no Sage node anywhere", not any("sage" in n["class_type"].lower() for n in g.values()))

print()
print("graph: no fast_kind / turbo is today's turbo graph")
base = engines.graph_for("video", "fl2va", dict(ARGS, steps=8, turbo_lora=True), MODELS_TAO)
for fk in ("turbo", None):
    r = dict(ARGS, steps=8, turbo_lora=True)
    if fk:
        r["fast_kind"] = fk
    check("fast_kind=%r identical to absent" % fk, engines.graph_for("video", "fl2va", r, MODELS_TAO) == base)
check("turbo graph keeps res_multistep/simple and the turbo LoRA",
      base["17"]["inputs"]["sampler_name"] == "res_multistep" and base["9"]["inputs"]["scheduler"] == "simple"
      and base["7"]["inputs"]["lora_name"] == MODELS["h3_turbo_lora"] and not by_class(base, "MiniMaxH3SigmaShift"))
check("taomate without turbo_lora builds the plain graph",
      engines.graph_for("video", "fl2va", dict(ARGS, fast_kind="taomate"), MODELS_TAO)
      == engines.graph_for("video", "fl2va", dict(ARGS), MODELS_TAO))
check("continue ignores fast_kind at graph level (the server refuses it)",
      engines.graph_for("video", "continue", dict(ARGS, turbo_lora=True, fast_kind="taomate"), MODELS_TAO)
      == engines.graph_for("video", "continue", dict(ARGS, turbo_lora=True), MODELS_TAO))

print()
print("role detection")
check("the taomate file resolves the taomate role", engines.role_rules()["h3_taomate_lora"]["any"] == ["taomate"])
_spec = importlib.util.spec_from_file_location("srv_taomate", os.path.join(ROOT, "server.py"))
_s = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_s)   # the scratch config is set above
turbo_pool = ["minimax_h3/minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors", TAO]
check("turbo role does not pick the taomate file",
      _s.pick_model(turbo_pool, engines.role_rules()["h3_turbo_lora"], False) == turbo_pool[0])
check("taomate role picks only the taomate file",
      _s.pick_model(turbo_pool, engines.role_rules()["h3_taomate_lora"], False) == TAO)
check("style packs leave the taomate file out",
      all("taomate" in f["match"]["none"] for f in engines.fields("video", "fl2va") if f["id"] == "style_1"))

print()
print("generate(): ability and refusals")
LANE = "t"
def lane_with(models, node):
    srv = _s
    srv.LANE_BY_ID[LANE]["models"] = dict(models)
    srv.DISCOVERY[LANE] = {"models": {}, "pools": {}, "nodes": {"h3_sigma_shift": node}, "checked": 1.0, "err": ""}
    with srv.STATE_LOCK:
        srv.LANE_STATE[LANE] = {"up": True, "checked": 1.0, "err": ""}
    return srv.LANE_BY_ID[LANE]

CAPTURED = []
_s.dispatch = lambda lane, graph, kind, mode, meta: (CAPTURED.append((graph, meta)) or {"ok": True, "job": {"id": "x"}})

def gen(mode, **kw):
    body = {"lane": LANE, "kind": "video", "mode": mode, "prompt": "p", "seed": 1, "length": 124, "confirm": True, "first_frame": "a.png"}
    body.update(kw)
    CAPTURED.clear()
    return _s.generate(body)

# generate() needs a lane that exists in the scratch config
_s.LANE_BY_ID[LANE] = {"id": LANE, "name": "Test lane", "host": "127.0.0.1", "port": 1,
                       "caps": ["image", "video"], "models": {}}
FULL = dict(MODELS, h3_clip_int8="h3_clip_int8.safetensors")

lane_with(dict(FULL, h3_taomate_lora=TAO), True)
check("ability true with file and node", _s.abilities(_s.LANE_BY_ID[LANE])["taomate"] is True)
res, code = gen("fl2va", steps=4, turbo_lora=True, fast_kind="taomate")
check("fl2va taomate renders", code == 200 and res.get("ok"), res)
gr, meta = CAPTURED[0] if CAPTURED else ({}, {})
check("submitted graph has the taomate chain",
      gr.get("7", {}).get("inputs", {}).get("lora_name") == TAO and gr.get("8", {}).get("class_type") == "MiniMaxH3SigmaShift"
      and gr["9"]["inputs"]["scheduler"] == "beta" and gr["17"]["inputs"]["sampler_name"] == "er_sde"
      and gr["9"]["inputs"]["steps"] == 4 and MODELS["h3_turbo_lora"] not in json.dumps(gr))
res, code = gen("fl2va", steps=8, turbo_lora=True)
check("no fast_kind: today's turbo graph through generate()",
      code == 200 and CAPTURED[0][0]["7"]["inputs"]["lora_name"] == MODELS["h3_turbo_lora"]
      and "8" not in CAPTURED[0][0] and CAPTURED[0][0]["17"]["inputs"]["sampler_name"] == "res_multistep")
res, code = gen("fl2va", steps=20, turbo_lora=False, fast_kind="taomate")
check("taomate with fast mode off is inert", code == 200 and "7" not in CAPTURED[0][0], res)

for mode, extra in (("continue", {}), ("ref2v", {"ref_images": ["a.png"]})):
    res, code = gen(mode, steps=4, turbo_lora=True, fast_kind="taomate", **extra)
    check("%s + taomate refused with a sentence" % mode,
          code == 400 and not res["ok"] and "fast draft" in res["error"].lower() and not CAPTURED, (code, res))
res, code = gen("fl2va", steps=4, turbo_lora=True, fast_kind="bogus")
check("unknown fast_kind refused with a sentence", code == 400 and "turbo, taomate" in res["error"] and not CAPTURED, (code, res))

# a lane with the taomate pack and node but NO turbo LoRA
NO_TURBO = {k: v for k, v in FULL.items() if k != "h3_turbo_lora"}
lane_with(dict(NO_TURBO, h3_taomate_lora=TAO), True)
check("no-turbo lane: taomate ability true, turbo false",
      _s.abilities(_s.LANE_BY_ID[LANE])["taomate"] is True and _s.abilities(_s.LANE_BY_ID[LANE])["turbo"] is False)
res, code = gen("fl2va", steps=4, turbo_lora=True, fast_kind="taomate")
check("no-turbo lane renders the taomate graph", code == 200 and CAPTURED
      and CAPTURED[0][0].get("8", {}).get("class_type") == "MiniMaxH3SigmaShift"
      and CAPTURED[0][0]["7"]["inputs"]["lora_name"] == TAO, (code, res))
res, code = gen("fl2va", steps=8, turbo_lora=True)
check("no-turbo lane refuses plain turbo with the speed-pack sentence",
      code == 400 and "no speed pack installed" in res["error"] and not CAPTURED, (code, res))

lane_with(dict(FULL, h3_taomate_lora=TAO), False)
check("ability false without the node", _s.abilities(_s.LANE_BY_ID[LANE])["taomate"] is False)
res, code = gen("fl2va", steps=4, turbo_lora=True, fast_kind="taomate")
check("refused without the node", code == 400 and "fast-draft pack" in res["error"] and not CAPTURED, (code, res))
res, code = gen("fl2va", steps=8, turbo_lora=True)
check("turbo still renders on that lane", code == 200)

lane_with(FULL, True)
check("ability false without the file", _s.abilities(_s.LANE_BY_ID[LANE])["taomate"] is False)
res, code = gen("fl2va", steps=4, turbo_lora=True, fast_kind="taomate")
check("refused without the file", code == 400 and "fast-draft pack" in res["error"] and not CAPTURED, (code, res))

print()
print("listing: preset only where met; ladder untouched")
def listing(models, node):
    lane_with(models, node)
    out = _s.Handler.engines_payload(None, {"lane": [LANE]})
    mode = next(m for m in out["video"]["modes"] if m["id"] == "fl2va")
    return mode
met = listing(dict(FULL, h3_taomate_lora=TAO), True)
check("preset offered, before turbo-8step",
      [p["id"] for p in met["presets"]][:3] == ["default-length", "taomate-4step", "turbo-8step"], [p["id"] for p in met["presets"]])
check("ladder has no fast-draft row", [q["id"] for q in met["quality"]]
      == ["fast", "fast-plus", "fast-best", "middle", "good", "best"])
check("ladder default stays Best", [q["id"] for q in met["quality"] if q.get("default")] == ["best"])
for label, models, node in (("no node", dict(FULL, h3_taomate_lora=TAO), False), ("no file", FULL, True)):
    lst = listing(models, node)
    check("%s: preset hidden" % label, "taomate-4step" not in [p["id"] for p in lst["presets"]]
          and "turbo-8step" in [p["id"] for p in lst["presets"]])
fk = next(f for f in met["fields"] if f["id"] == "fast_kind")
check("fast_kind is an advanced select, no default (first option, turbo, shows)", fk["type"] == "select" and fk["tier"] == "advanced"
      and "default" not in fk and fk["options"] == ["turbo", "taomate"])
check("continue and ref2v declare no fast_kind field",
      all("fast_kind" not in [f["id"] for f in engines.fields("video", m)] for m in ("continue", "ref2v")))

print()
print("FAILED: %d" % len(FAILED) if FAILED else "ALL PASS")
sys.exit(1 if FAILED else 0)
