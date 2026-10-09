"""LORA-2A acceptance gate: style-pack (LoRA) wiring for LTX-2.5
(ltx/ltx_loop/talking). No style -> byte-identical to golden (pinned by
tests/test_ltx_golden.py). Run: python3 tests/test_style_lora_ltx.py"""
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

MODELS = json.load(open(os.path.join(G, "ltx_models.json")))
ARGS = json.load(open(os.path.join(G, "ltx_args.json")))

print("ltx (two_stage default True): style chains ONCE off node 384, feeds BOTH stage guiders")
g = engines.graph_for("video", "ltx", dict(ARGS["t2v"], style_1="cool_ltx25.safetensors", style_1_strength=0.8), MODELS)
loras = by_class(g, "LoraLoaderModelOnly")
check("exactly one LoraLoaderModelOnly node (chained once, not per stage)", len(loras) == 1, loras)
if loras:
    check("reads node 384", loras[0]["inputs"]["model"] == ["384", 0])
check("stage 1 guider (388) reads the style chain", g["388"]["inputs"]["model"] == ["396", 0], g["388"])
check("stage 2 guider (391) reads the SAME style chain", g["391"]["inputs"]["model"] == ["396", 0], g["391"])

print()
print("ltx: two styles stack, in order")
g2 = engines.graph_for("video", "ltx", dict(ARGS["t2v"], style_1="a_ltx25.safetensors", style_2="b_ltx25.safetensors"), MODELS)
n396, n397 = g2.get("396"), g2.get("397")
check("node 396 reads node 384", n396 is not None and n396["inputs"]["model"] == ["384", 0], n396)
check("node 397 reads node 396's output", n397 is not None and n397["inputs"]["model"] == ["396", 0], n397)
check("both stage guiders read the second style's output",
      g2["388"]["inputs"]["model"] == ["397", 0] and g2["391"]["inputs"]["model"] == ["397", 0])

print()
print("ltx windowed: LTXVContextWindows wraps the STYLE chain, not the bare loader")
gw = engines.graph_for("video", "ltx", dict(ARGS["t2v"], style_1="cool_ltx25.safetensors", context_length=49, context_overlap=8), MODELS)
check("LTXVContextWindows (394) wraps the style chain", gw["394"]["inputs"]["model"] == ["396", 0], gw["394"])
check("stage guider now reads the context-window output", gw["388"]["inputs"]["model"] == ["394", 0])

print()
print("ltx_loop: style chains off node 384, feeds BOTH the CFGGuider and the LoopingSampler")
gl = engines.graph_for("video", "ltx_loop", dict(ARGS["loop"], style_1="cool_ltx25.safetensors"), MODELS)
check("exactly one LoraLoaderModelOnly node", len(by_class(gl, "LoraLoaderModelOnly")) == 1)
check("CFGGuider (390) reads the style chain", gl["390"]["inputs"]["model"] == ["396", 0], gl["390"])
check("LTXVLoopingSampler (500) reads the style chain too", gl["500"]["inputs"]["model"] == ["396", 0], gl["500"])

print()
print("no style chosen: no LoraLoaderModelOnly node, guiders read the bare loader "
      "(full byte-identity vs. the frozen source is pinned separately by test_ltx_golden.py)")
got_t2v = engines.graph_for("video", "ltx", dict(ARGS["t2v"]), MODELS)
check("no style node", by_class(got_t2v, "LoraLoaderModelOnly") == [])
check("stage 1 guider reads the bare loader", got_t2v["388"]["inputs"]["model"] == ["384", 0])
check("stage 2 guider reads the bare loader", got_t2v["391"]["inputs"]["model"] == ["384", 0])
got_loop = engines.graph_for("video", "ltx_loop", dict(ARGS["loop"]), MODELS)
check("no style node (ltx_loop)", by_class(got_loop, "LoraLoaderModelOnly") == [])
check("CFGGuider reads the bare loader", got_loop["390"]["inputs"]["model"] == ["384", 0])
check("LTXVLoopingSampler reads the bare loader", got_loop["500"]["inputs"]["model"] == ["384", 0])

print()
print("field declarations offer the Style group on all three modes")
for mode in ("ltx", "ltx_loop", "talking"):
    ids = {f["id"] for f in engines.fields("video", mode) if f.get("group") == "Style"}
    check("video/%s declares the 4 style fields" % mode,
          ids == {"style_1", "style_1_strength", "style_2", "style_2_strength"}, ids)

print()
print("strength 0 is a real value (LoRA off), not 'unset' -- only absent means 1.0")
g0 = engines.graph_for("video", "ltx", dict(ARGS["t2v"], style_1="cool_ltx25.safetensors", style_1_strength=0.0), MODELS)
l0 = by_class(g0, "LoraLoaderModelOnly")
check("ltx: strength 0.0 reaches the node as 0.0", len(l0) == 1 and l0[0]["inputs"]["strength_model"] == 0.0, l0)
gd = engines.graph_for("video", "ltx", dict(ARGS["t2v"], style_1="cool_ltx25.safetensors"), MODELS)
check("ltx: absent strength defaults to 1.0", by_class(gd, "LoraLoaderModelOnly")[0]["inputs"]["strength_model"] == 1.0)

print()
print(("FAILED: %d" % len(FAILED)) if FAILED else "ALL PASS")
sys.exit(1 if FAILED else 0)
