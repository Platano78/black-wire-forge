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
print("yue2: style chains off node 15 (CheckpointLoaderSimple), feeds KSampler (8)")
gy = engines.graph_for("audio", "yue2", dict(ARGS["yue2"], style_1="cool_yue2.safetensors"), MODELS)
loras_y = by_class(gy, "LoraLoaderModelOnly")
check("exactly one LoraLoaderModelOnly node", len(loras_y) == 1, loras_y)
if loras_y:
    check("reads node 15", loras_y[0]["inputs"]["model"] == ["15", 0])
check("KSampler (8) reads the style chain", gy["8"]["inputs"]["model"] == ["200", 0], gy["8"])

print()
print("cover: two styles stack, off its OWN node 47, feeding KSampler (51)")
gc = engines.graph_for("audio", "cover",
                        dict(ARGS["cover"], style_1="cool_yue2.safetensors", style_2="second_yue2.safetensors"), MODELS)
n200, n201 = gc.get("200"), gc.get("201")
check("node 200 reads node 47", n200 is not None and n200["inputs"]["model"] == ["47", 0], n200)
check("node 201 reads node 200's output", n201 is not None and n201["inputs"]["model"] == ["200", 0], n201)
check("KSampler (51) reads the second style's output", gc["51"]["inputs"]["model"] == ["201", 0], gc["51"])

print()
print("no style chosen: no LoraLoaderModelOnly node in any of the four "
      "(full byte-identity vs. the frozen source is pinned separately by test_audio_golden.py)")
for mode, model_node in (("song", None), ("music", None), ("yue2", None), ("cover", None)):
    got = engines.graph_for("audio", mode, dict(ARGS[mode]), MODELS)
    check("%s: no style node" % mode, by_class(got, "LoraLoaderModelOnly") == [])
check("song ModelSamplingAuraFlow reads the bare loader",
      engines.graph_for("audio", "song", dict(ARGS["song"]), MODELS)["78"]["inputs"]["model"] == ["104", 0])
check("music KSampler reads the bare loader",
      engines.graph_for("audio", "music", dict(ARGS["music"]), MODELS)["7"]["inputs"]["model"] == ["1", 0])
check("yue2 KSampler reads the bare loader",
      engines.graph_for("audio", "yue2", dict(ARGS["yue2"]), MODELS)["8"]["inputs"]["model"] == ["15", 0])
check("cover KSampler reads the bare loader",
      engines.graph_for("audio", "cover", dict(ARGS["cover"]), MODELS)["51"]["inputs"]["model"] == ["47", 0])

print()
print("field declarations offer the Style group on song/music/yue2/cover, NOT sfx")
for mode in ("song", "music", "yue2", "cover"):
    ids = {f["id"] for f in engines.fields("audio", mode) if f.get("group") == "Style"}
    check("audio/%s declares the 4 style fields" % mode,
          ids == {"style_1", "style_1_strength", "style_2", "style_2_strength"}, ids)
check("audio/sfx declares NO style fields (out of scope)",
      not any(f.get("group") == "Style" for f in engines.fields("audio", "sfx")))

print()
print(("FAILED: %d" % len(FAILED)) if FAILED else "ALL PASS")
sys.exit(1 if FAILED else 0)
