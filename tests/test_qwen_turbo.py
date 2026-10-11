"""Qwen-Image-2.1-Turbo: the picture, edit and character-sheet graphs run the Turbo
build on its own saved 8-step sigma schedule, and every other graph is untouched.

What this pins, and why:
- Turbo is found by "turbo" in the chosen unet's filename (engines.qwen_image.is_turbo).
- A Turbo graph samples with the sigma chain (RandomNoise, BasicGuider, KSamplerSelect,
  ManualSigmas, SamplerCustomAdvanced on nodes 50-54), never a KSampler: the same weights
  under a plain KSampler euler/simple 8-step run came out visibly darker and seamed worse
  (measured 2026-10-10). No guidance nodes (APG/FreSca), no ModelSamplingAuraFlow.
- Style LoRAs and the charsheet's speed wrappers still sit on the model path, before the guider.
- A graph for a non-Turbo model is byte-identical to its golden (the sampler is the same
  KSampler node "8" it always was).
- Discovery never picks a turbo file (measured 2026-10-10: it drops fine background detail):
  both unet roles take the base model's int8 file, else the GGUF. The turbo graph code is kept
  and tested here, reachable only by a hand-set model name.

Run: python3 tests/test_qwen_turbo.py
"""
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

try:
    from engines import qwen_image
    HAVE_TURBO = hasattr(qwen_image, "is_turbo")
except Exception:       # keep the file runnable on a tree that has none of this
    HAVE_TURBO = False

G = os.path.join(HERE, "golden")
MODELS = json.load(open(os.path.join(G, "models.json")))
ARGS = json.load(open(os.path.join(G, "args.json")))["img"]
TURBO_FILE = "qwen_image_2.1_turbo_int8_convrot.safetensors"
BASE_FILE = "qwen_image_2.1_Q6_K.gguf"
TM = dict(MODELS, qwen_unet=TURBO_FILE)
SIGMAS = "1.0, 0.978453, 0.95418, 0.926626, 0.89508, 0.845148, 0.704534, 0.414568, 0.0"
SEED = ARGS["seed"]


def classes(g):
    return [n["class_type"] for n in g.values()]


def nodes(g, cls):
    return [(nid, n) for nid, n in g.items() if n["class_type"] == cls]


def no_ksampler_family(g):
    return not any(c in ("KSampler", "APG", "FreSca", "ModelSamplingAuraFlow") for c in classes(g))


print("1. t2i on Turbo: the sigma chain, no KSampler, no guidance, even when Balanced is asked for")
args = dict(ARGS, guidance_style="Balanced", steps=40, cfg=3.0, sampler="seeds_2", scheduler="sgm_uniform")
g = engines.graph_for("image", "t2i", args, TM)
check("no KSampler / APG / FreSca / ModelSamplingAuraFlow", no_ksampler_family(g), sorted(classes(g)))
check("node 1 is a UNETLoader of the Turbo file",
      g["1"] == {"class_type": "UNETLoader", "inputs": {"unet_name": TURBO_FILE, "weight_dtype": "default"}}, g.get("1"))
check("50 RandomNoise carries the seed",
      g.get("50") == {"class_type": "RandomNoise", "inputs": {"noise_seed": SEED}}, g.get("50"))
check("51 BasicGuider: the model wire is node 1, conditioning is the positive (4, 0)",
      g.get("51") == {"class_type": "BasicGuider", "inputs": {"model": ["1", 0], "conditioning": ["4", 0]}}, g.get("51"))
check("52 KSamplerSelect euler (the steps/sampler/scheduler fields are not read)",
      g.get("52") == {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}}, g.get("52"))
check("53 ManualSigmas is the saved schedule, exactly",
      g.get("53") == {"class_type": "ManualSigmas", "inputs": {"sigmas": SIGMAS}}, g.get("53"))
check("54 SamplerCustomAdvanced is wired to 50-53 and the EmptyLatentImage (6, 0)",
      g.get("54") == {"class_type": "SamplerCustomAdvanced", "inputs": {
          "noise": ["50", 0], "guider": ["51", 0], "sampler": ["52", 0], "sigmas": ["53", 0],
          "latent_image": ["6", 0]}}, g.get("54"))
check("9 VAEDecode reads 54", g["9"]["inputs"]["samples"] == ["54", 0], g["9"])
check("the same params without Balanced give the same sampling nodes",
      {k: v for k, v in engines.graph_for("image", "t2i", dict(ARGS, steps=7, cfg=1.0), TM).items()
       if k in ("50", "51", "52", "53", "54", "9")}
      == {k: v for k, v in g.items() if k in ("50", "51", "52", "53", "54", "9")})

print()
print("2. edit on Turbo: no shift node, the encoder's own latent, the references still wired")
eg = engines.graph_for("image", "edit", dict(ARGS, ref_images=["a.png", "b.png"]), TM)
check("no KSampler / APG / FreSca / ModelSamplingAuraFlow", no_ksampler_family(eg), sorted(classes(eg)))
check("54 latent_image is the encoder's (4, 2)", eg["54"]["inputs"]["latent_image"] == ["4", 2], eg.get("54"))
check("51 guider reads node 1 and the positive (4, 0)",
      eg["51"]["inputs"] == {"model": ["1", 0], "conditioning": ["4", 0]}, eg.get("51"))
check("53 sigmas exact", eg["53"]["inputs"]["sigmas"] == SIGMAS)
check("references are on images.image_1 / images.image_2 via LoadImage 101 / 102",
      eg["4"]["inputs"].get("images.image_1") == ["101", 0] and eg["4"]["inputs"].get("images.image_2") == ["102", 0]
      and eg["101"]["inputs"]["image"] == "a.png" and eg["102"]["inputs"]["image"] == "b.png",
      {k: v for k, v in eg["4"]["inputs"].items() if k.startswith("images")})
check("9 reads 54", eg["9"]["inputs"]["samples"] == ["54", 0])

print()
print("3. character sheet on Turbo: the speed wrappers feed the guider")
CS = {"reference": "hero.png", "name": "Rookie", "prompt": "SHEET", "seed": 7}
cg = engines.graph_for("image", "charsheet", CS, dict(TM, cs_attn=True, cs_cache=True))
check("no KSampler family", no_ksampler_family(cg), sorted(classes(cg)))
check("with cs_attn and cs_cache the guider's model is the cache node's output (12, 0)",
      cg["51"]["inputs"]["model"] == ["12", 0]
      and cg["12"]["class_type"] == "QwenImage21Cache" and cg["12"]["inputs"]["model"] == ["11", 0]
      and cg["11"]["class_type"] == "ModelAttentionBackend" and cg["11"]["inputs"]["model"] == ["1", 0], cg.get("51"))
check("charsheet latent is its EmptyLatentImage (6, 0), noise seed is the seed",
      cg["54"]["inputs"]["latent_image"] == ["6", 0] and cg["50"]["inputs"]["noise_seed"] == 7)
check("the reference still reaches the encoder on images.image_1",
      cg["4"]["inputs"].get("images.image_1") == ["5", 0] and cg["5"]["inputs"]["image"] == "hero.png")
cn = engines.graph_for("image", "charsheet", CS, dict(TM, cs_attn=False, cs_cache=False))
check("without them the guider's model is (1, 0)", cn["51"]["inputs"]["model"] == ["1", 0], cn.get("51"))
check("...and neither wrapper is in the graph", not nodes(cn, "ModelAttentionBackend") and not nodes(cn, "QwenImage21Cache"))

print()
print("4. style LoRAs on Turbo t2i: the guider reads the LoRA chain's output")
sg = engines.graph_for("image", "t2i", dict(ARGS, style_1="a_qwen_image.safetensors", style_1_strength=0.7), TM)
check("one style: guider model is (30, 0), and 30 reads node 1",
      sg["51"]["inputs"]["model"] == ["30", 0] and sg["30"]["inputs"]["model"] == ["1", 0], sg.get("51"))
sg2 = engines.graph_for("image", "t2i", dict(ARGS, style_1="a_qwen_image.safetensors",
                                              style_2="b_qwen_image.safetensors", style_2_strength=0.5), TM)
check("two styles: guider model is (31, 0), and 31 reads 30",
      sg2["51"]["inputs"]["model"] == ["31", 0] and sg2["31"]["inputs"]["model"] == ["30", 0], sg2.get("51"))
check("no APG/FreSca with a style on Turbo", no_ksampler_family(sg), sorted(classes(sg)))

print()
print("5. a non-Turbo graph is byte-identical to its golden, and has none of the Turbo nodes")
want_t2i = json.load(open(os.path.join(G, "qwen_t2i.json")))
want_edit = json.load(open(os.path.join(G, "qwen_edit.json")))
bt = engines.graph_for("image", "t2i", ARGS, MODELS)
be = engines.graph_for("image", "edit", ARGS, MODELS)
check("t2i == tests/golden/qwen_t2i.json", json.dumps(bt, sort_keys=True) == json.dumps(want_t2i, sort_keys=True))
check("edit == tests/golden/qwen_edit.json", json.dumps(be, sort_keys=True) == json.dumps(want_edit, sort_keys=True))
check("t2i keeps its KSampler on node 8", bt["8"]["class_type"] == "KSampler" and bt["9"]["inputs"]["samples"] == ["8", 0])
check("edit keeps ModelSamplingAuraFlow (shift 3.1) on node 7 feeding node 8",
      be["7"]["class_type"] == "ModelSamplingAuraFlow" and be["7"]["inputs"]["shift"] == 3.1
      and be["8"]["inputs"]["model"] == ["7", 0])
check("neither has a node 50-54",
      not any(k in bt or k in be for k in ("50", "51", "52", "53", "54")))
bc = engines.graph_for("image", "charsheet", CS, dict(MODELS, cs_attn=True, cs_cache=True))
check("charsheet keeps its 25-step euler/simple KSampler",
      bc["8"]["class_type"] == "KSampler" and bc["8"]["inputs"]["steps"] == 25
      and bc["8"]["inputs"]["sampler_name"] == "euler" and bc["8"]["inputs"]["scheduler"] == "simple"
      and bc["8"]["inputs"]["model"] == ["12", 0])

print()
print("6. discovery: the base model on int8, GGUF as fallback, turbo files never picked")
spec = importlib.util.spec_from_file_location("srv_turbo", os.path.join(ROOT, "server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)
INT8_BASE = "qwen_image_2.1_int8_convrot.safetensors"
TURBO_GGUF = "qwen_image_2.1_turbo_Q6_K.gguf"
RULES = {r: srv.ROLE_RULES[r] for r in ("qwen_unet", "pixel_unet")}
check("pixel_unet is a role of its own", "pixel_unet" in srv.ROLE_RULES and "qwen_unet" in srv.ROLE_RULES,
      sorted(r for r in srv.ROLE_RULES if "unet" in r))
RIG_POOL = [BASE_FILE, TURBO_GGUF, TURBO_FILE, INT8_BASE]
for small in (True, False):
    for role in RULES:
        got = srv.pick_model(RIG_POOL, RULES[role], small)
        check("the rig's pool (base GGUF, turbo GGUF, turbo int8, base int8), small_card=%s: %s -> the base int8" % (small, role),
              got == INT8_BASE, got)
        got = srv.pick_model([BASE_FILE, TURBO_GGUF, TURBO_FILE], RULES[role], small)
        check("no base int8 in the pool, small_card=%s: %s -> the base GGUF" % (small, role), got == BASE_FILE, got)
        got = srv.pick_model([TURBO_GGUF, TURBO_FILE], RULES[role], small)
        check("only turbo files, small_card=%s: %s finds nothing" % (small, role), got is None, got)
MB_POOL = ["qwen_image_2.1_bf16.safetensors", "qwen_image_2.1_turbo_bf16.safetensors"]
for role in RULES:
    got = srv.pick_model(MB_POOL, RULES[role], False)
    check("a big-card pool (base bf16, turbo bf16), big card: %s -> the base bf16" % role,
          got == "qwen_image_2.1_bf16.safetensors", got)
check("Pixel Art is unavailable with only a turbo file under pixel_unet",
      engines.abilities(dict(MODELS, pixel_unet="", birefnet_model="m")).get("pixelart") is False
      or not engines.abilities(dict(MODELS, pixel_unet="", birefnet_model="m")).get("pixelart"))
check("Pixel Art is available with a base file",
      engines.abilities(dict(MODELS, pixel_unet=BASE_FILE, birefnet_model="m")).get("pixelart") is True)

print()
print("   Pixel Art with a hand-set turbo qwen_unet still builds the base model's graph")
PM = dict(MODELS, qwen_unet=TURBO_FILE, pixel_unet=BASE_FILE, birefnet_model="model.safetensors")
pg = engines.graph_for("image", "pixelart", dict(ARGS, prompt="a slime"), PM)
check("unet is the base GGUF", pg["1"]["inputs"]["unet_name"] == BASE_FILE, pg.get("1"))
check("it samples with a KSampler and the Balanced APG/FreSca pair, no sigma chain",
      "KSampler" in classes(pg) and "APG" in classes(pg) and "FreSca" in classes(pg)
      and not any(k in pg for k in ("50", "51", "52", "53", "54")), sorted(classes(pg)))
pe = engines.graph_for("image", "pixelart", dict(ARGS, prompt="a slime", shape_image="shape.png"), PM)
check("with a shape image (the edit path) it is the base model on a KSampler too",
      pe["1"]["inputs"]["unet_name"] == BASE_FILE and "KSampler" in classes(pe) and "SamplerCustomAdvanced" not in classes(pe))

print()
print("7. the lane's description and the fields' hints")
check("_describe on a Turbo name says Turbo",
      "Turbo" in engines.describe(TM, "image"), engines.describe(TM, "image"))
check("_describe keeps the quant words", "int8" in engines.describe(TM, "image").lower(), engines.describe(TM, "image"))
check("_describe on the base name does not say Turbo", "Turbo" not in engines.describe(MODELS, "image"),
      engines.describe(MODELS, "image"))
hints = {(m_, f["id"]): f.get("hint") for m_ in ("t2i", "edit") for f in engines.fields("image", m_)}
check("no t2i/edit field hint mentions Turbo (the app never selects it)",
      not [k for k, v in hints.items() if "turbo" in (v or "").lower()], [k for k, v in hints.items() if "turbo" in (v or "").lower()])
notes = {(m_, p["id"]): p.get("note") for m_ in ("t2i", "charsheet") for p in engines.presets("image", m_)}
check("no t2i/charsheet preset note mentions Turbo", not [k for k, v in notes.items() if "turbo" in (v or "").lower()])
check("the int8 base name builds the plain KSampler graph (is_turbo is False)",
      not qwen_image.is_turbo({"qwen_unet": INT8_BASE})
      and "KSampler" in classes(engines.graph_for("image", "t2i", ARGS, dict(MODELS, qwen_unet=INT8_BASE))))

print("\nFAILED: %d" % len(FAILED) + (" checks: " + ", ".join(FAILED) if FAILED else ""))
sys.exit(1 if FAILED else 0)
