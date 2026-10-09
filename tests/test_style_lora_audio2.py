"""LORA-2A acceptance gate: style-pack (LoRA) wiring for ACE-Step 1.5 (song),
MiniMax-Music3 (music) and YuE2 (yue2/cover). sfx is out of scope. No style
-> byte-identical to golden (pinned by tests/test_audio_golden.py).
Run: python3 tests/test_style_lora_audio2.py"""
import json, os, sys
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
G = os.path.join(HERE, "golden")

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail else ""))
    if not cond: FAILED.append(name)

import engines

def by_class(graph, cls):
    return [n for n in graph.values() if n.get("class_type") == cls]

MODELS = json.load(open(os.path.join(G, "audio_models.json")))
ARGS = json.load(open(os.path.join(G, "audio_args.json")))

print("song: style chains off node 104, feeds ModelSamplingAuraFlow (78)")
g = engines.graph_for("audio", "song", dict(ARGS["song"], style_1="cool_ace_step15.safetensors"), MODELS)
loras = by_class(g, "LoraLoaderModelOnly")
check("exactly one LoraLoaderModelOnly node", len(loras) == 1, loras)
if loras:
    check("reads node 104", loras[0]["inputs"]["model"] == ["104", 0])
check("ModelSamplingAuraFlow (78) reads the style chain", g["78"]["inputs"]["model"] == ["200", 0], g["78"])

print()
print("music: style chains off node 1, feeds KSampler (7) directly")
gm = engines.graph_for("audio", "music", dict(ARGS["music"], style_1="cool_music3.safetensors"), MODELS)
loras_m = by_class(gm, "LoraLoaderModelOnly")
check("exactly one LoraLoaderModelOnly node", len(loras_m) == 1, loras_m)
if loras_m:
    check("reads node 1", loras_m[0]["inputs"]["model"] == ["1", 0])
check("KSampler (7) reads the style chain", gm["7"]["inputs"]["model"] == ["200", 0], gm["7"])

print()
print("yue2: LoraLoader (model+clip) off node 15, feeds KSampler's model AND "
      "YuE2GenerateMusic/YuE2GenerateABC's clip -- LoraLoaderModelOnly alone leaves "
      "text_encoders.* unpatched (render proof)")
gy = engines.graph_for("audio", "yue2", dict(ARGS["yue2"], style_1="cool_yue2.safetensors"), MODELS)
check("no LoraLoaderModelOnly node (model-only would leave CLIP unpatched)", by_class(gy, "LoraLoaderModelOnly") == [])
loras_y = by_class(gy, "LoraLoader")
check("exactly one LoraLoader node", len(loras_y) == 1, loras_y)
if loras_y:
    check("reads node 15 for BOTH model and clip",
          loras_y[0]["inputs"]["model"] == ["15", 0] and loras_y[0]["inputs"]["clip"] == ["15", 1], loras_y[0])
    check("strength_model == strength_clip == the chosen strength",
          loras_y[0]["inputs"]["strength_model"] == 1.0 == loras_y[0]["inputs"]["strength_clip"])
check("KSampler (8) reads the style chain's MODEL output (slot 0)", gy["8"]["inputs"]["model"] == ["200", 0], gy["8"])
check("YuE2GenerateMusic (25) reads the style chain's CLIP output (slot 1)", gy["25"]["inputs"]["clip"] == ["200", 1], gy["25"])
check("YuE2GenerateABC (24, plan=True by default) reads the style chain's CLIP too", gy["24"]["inputs"]["clip"] == ["200", 1], gy["24"])

print()
print("cover: two LoraLoaders stack (model+clip both threaded), off its OWN node 47")
gc = engines.graph_for("audio", "cover",
                        dict(ARGS["cover"], style_1="cool_yue2.safetensors", style_2="second_yue2.safetensors"), MODELS)
check("no LoraLoaderModelOnly node", by_class(gc, "LoraLoaderModelOnly") == [])
check("two LoraLoader nodes", len(by_class(gc, "LoraLoader")) == 2, by_class(gc, "LoraLoader"))
n200, n201 = gc.get("200"), gc.get("201")
check("node 200 reads node 47 for model AND clip",
      n200 is not None and n200["inputs"]["model"] == ["47", 0] and n200["inputs"]["clip"] == ["47", 1], n200)
check("node 201 reads node 200's model AND clip outputs",
      n201 is not None and n201["inputs"]["model"] == ["200", 0] and n201["inputs"]["clip"] == ["200", 1], n201)
check("KSampler (51) reads the second style's MODEL output", gc["51"]["inputs"]["model"] == ["201", 0], gc["51"])
check("YuE2GenerateMusic (48) reads the second style's CLIP output", gc["48"]["inputs"]["clip"] == ["201", 1], gc["48"])

print()
print("no style chosen: no style node (LoraLoaderModelOnly OR LoraLoader) in any of the four "
      "(full byte-identity vs. the frozen source is pinned separately by test_audio_golden.py)")
for mode in ("song", "music", "yue2", "cover"):
    got = engines.graph_for("audio", mode, dict(ARGS[mode]), MODELS)
    check("%s: no style node" % mode,
          by_class(got, "LoraLoaderModelOnly") == [] and by_class(got, "LoraLoader") == [])
check("song ModelSamplingAuraFlow reads the bare loader",
      engines.graph_for("audio", "song", dict(ARGS["song"]), MODELS)["78"]["inputs"]["model"] == ["104", 0])
check("music KSampler reads the bare loader",
      engines.graph_for("audio", "music", dict(ARGS["music"]), MODELS)["7"]["inputs"]["model"] == ["1", 0])
plain_yue2 = engines.graph_for("audio", "yue2", dict(ARGS["yue2"]), MODELS)
check("yue2 KSampler reads the bare loader's model", plain_yue2["8"]["inputs"]["model"] == ["15", 0])
check("yue2 YuE2GenerateMusic reads the bare loader's clip", plain_yue2["25"]["inputs"]["clip"] == ["15", 1])
plain_cover = engines.graph_for("audio", "cover", dict(ARGS["cover"]), MODELS)
check("cover KSampler reads the bare loader's model", plain_cover["51"]["inputs"]["model"] == ["47", 0])
check("cover YuE2GenerateMusic reads the bare loader's clip", plain_cover["48"]["inputs"]["clip"] == ["47", 1])

print()
print("field declarations offer the Style group on song/music/yue2/cover, NOT sfx")
for mode in ("song", "music", "yue2", "cover"):
    ids = {f["id"] for f in engines.fields("audio", mode) if f.get("group") == "Style"}
    check("audio/%s declares the 4 style fields" % mode,
          ids == {"style_1", "style_1_strength", "style_2", "style_2_strength"}, ids)
check("audio/sfx declares NO style fields (out of scope)",
      not any(f.get("group") == "Style" for f in engines.fields("audio", "sfx")))

print()
print("strength 0 is a real value (LoRA off), not 'unset' -- only absent means 1.0")
for mode, style in (("song", "cool_ace_step15.safetensors"), ("music", "cool_music3.safetensors")):
    g0 = engines.graph_for("audio", mode, dict(ARGS[mode], style_1=style, style_1_strength=0.0), MODELS)
    l0 = by_class(g0, "LoraLoaderModelOnly")
    check("%s: strength 0.0 reaches the node as 0.0" % mode, len(l0) == 1 and l0[0]["inputs"]["strength_model"] == 0.0, l0)
    gd = engines.graph_for("audio", mode, dict(ARGS[mode], style_1=style), MODELS)
    check("%s: absent strength defaults to 1.0" % mode, by_class(gd, "LoraLoaderModelOnly")[0]["inputs"]["strength_model"] == 1.0)
for mode in ("yue2", "cover"):
    g0 = engines.graph_for("audio", mode, dict(ARGS[mode], style_1="cool_yue2.safetensors", style_1_strength=0.0), MODELS)
    l0 = by_class(g0, "LoraLoader")
    check("%s: model and clip strength 0.0" % mode, len(l0) == 1
          and l0[0]["inputs"]["strength_model"] == 0.0 and l0[0]["inputs"]["strength_clip"] == 0.0, l0)
    gd = engines.graph_for("audio", mode, dict(ARGS[mode], style_1="cool_yue2.safetensors"), MODELS)
    check("%s: absent strength defaults to 1.0" % mode, by_class(gd, "LoraLoader")[0]["inputs"]["strength_model"] == 1.0)

print()
print(("FAILED: %d" % len(FAILED)) if FAILED else "ALL PASS")
sys.exit(1 if FAILED else 0)
