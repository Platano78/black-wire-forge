"""LORA-2A acceptance gate: style-pack (LoRA) wiring for MiniMax-H3
(fl2va/continue/ref2v). No style -> byte-identical to golden (pinned by
tests/test_packs_golden.py). Run: python3 tests/test_style_lora_h3.py"""
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

MODELS = json.load(open(os.path.join(G, "models.json")))
ARGS = json.load(open(os.path.join(G, "args.json")))["vid"]

print("fl2va: no turbo -- style chains straight off the UNet loader (6)")
g = engines.graph_for("video", "fl2va", dict(ARGS, style_1="cool_minimax_h3.safetensors", style_1_strength=0.6), MODELS)
loras = by_class(g, "LoraLoaderModelOnly")
check("exactly one LoraLoaderModelOnly node", len(loras) == 1, loras)
if loras:
    check("reads node 6", loras[0]["inputs"]["model"] == ["6", 0], loras[0])
    check("lora_name/strength carried", loras[0]["inputs"]["lora_name"] == "cool_minimax_h3.safetensors"
          and loras[0]["inputs"]["strength_model"] == 0.6)
check("BasicGuider (16) reads the style chain", g["16"]["inputs"]["model"] == ["230", 0], g["16"])
check("BasicScheduler (9) reads the style chain too", g["9"]["inputs"]["model"] == ["230", 0], g["9"])

print()
print("fl2va: turbo ON -- style chains AFTER the turbo LoRA (7), not before it")
gt = engines.graph_for("video", "fl2va", dict(ARGS, turbo_lora=True, style_1="cool_minimax_h3.safetensors"), MODELS)
check("style node reads node 7", gt.get("230", {}).get("inputs", {}).get("model") == ["7", 0], gt.get("230"))

print()
print("continue: two styles stack, in order")
gc = engines.graph_for("video", "continue",
                        dict(ARGS, style_1="a_minimax_h3.safetensors", style_2="b_minimax_h3.safetensors"), MODELS)
n230, n231 = gc.get("230"), gc.get("231")
check("node 230 reads node 6", n230 is not None and n230["inputs"]["model"] == ["6", 0], n230)
check("node 231 reads node 230's output", n231 is not None and n231["inputs"]["model"] == ["230", 0], n231)
check("BasicGuider reads the second style's output", gc["16"]["inputs"]["model"] == ["231", 0])

print()
print("ref2v: style chains off node 159, feeds scheduler (167) + guider (168)")
gr = engines.graph_for("video", "ref2v", dict(ARGS, style_1="cool_minimax_h3.safetensors"), MODELS)
loras_r = by_class(gr, "LoraLoaderModelOnly")
check("exactly one LoraLoaderModelOnly node", len(loras_r) == 1, loras_r)
if loras_r:
    check("reads node 159", loras_r[0]["inputs"]["model"] == ["159", 0])
check("BasicScheduler (167) reads the style chain", gr["167"]["inputs"]["model"] == ["230", 0])
check("BasicGuider (168) reads the style chain", gr["168"]["inputs"]["model"] == ["230", 0])

print()
print("no style chosen: fl2va/ref2v stay byte-identical to golden")
want_fl2va = json.load(open(os.path.join(G, "h3_fl2va.json")))
got_fl2va = engines.graph_for("video", "fl2va", dict(ARGS), MODELS)
check("fl2va unchanged", json.loads(json.dumps(got_fl2va, sort_keys=True)) == json.loads(json.dumps(want_fl2va, sort_keys=True)))
want_ref2va = json.load(open(os.path.join(G, "h3_ref2va.json")))
got_ref2va = engines.graph_for("video", "ref2v", dict(ARGS), MODELS)
check("ref2v unchanged", json.loads(json.dumps(got_ref2va, sort_keys=True)) == json.loads(json.dumps(want_ref2va, sort_keys=True)))

print()
print("field declarations offer the Style group on all three modes")
for mode in ("fl2va", "ref2v", "continue"):
    ids = {f["id"] for f in engines.fields("video", mode) if f.get("group") == "Style"}
    check("video/%s declares the 4 style fields" % mode,
          ids == {"style_1", "style_1_strength", "style_2", "style_2_strength"}, ids)

print()
print(("FAILED: %d" % len(FAILED)) if FAILED else "ALL PASS")
sys.exit(1 if FAILED else 0)
