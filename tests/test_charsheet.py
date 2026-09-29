"""CHARS-1: the Characters room's engine mode ("charsheet", Qwen-Image 2.1 edit
encoder, one reference picture -> a 3:2 design sheet).

Pins the recipe the sheets were proven on and the two ways it fails silently:
the reference must reach TextEncodeQwenImage21 through the DOTTED autogrow key
"images.image_1" (a nested dict quietly renders prompt-only), and the sampler
must take EmptyLatentImage, not the encoder's own latent. Also: the sizes, and
that ModelAttentionBackend / QwenImage21Cache are wired in only when the lane
reports them.

Run: python3 tests/test_charsheet.py
"""
import json, os, sys
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail else ""))
    if not cond: FAILED.append(name)

import engines

MODELS = json.load(open(os.path.join(HERE, "golden", "models.json")))
FAST = dict(MODELS, cs_attn=True, cs_cache=True)
ARGS = {"reference": "hero.png", "name": "Rookie", "prompt": "1. THE SHEET ...", "seed": 7}


def by_class(g, cls):
    return [(nid, n) for nid, n in g.items() if n["class_type"] == cls]


print("the mode is registered")
check("image cap has a charsheet mode", "charsheet" in engines.modes_for("image"), engines.modes_for("image"))
check("it takes the generic field-driven path (not the legacy image one)",
      engines.legacy_dispatch("image", "charsheet") is False)
check("t2i and edit stay on the legacy path",
      engines.legacy_dispatch("image", "t2i") and engines.legacy_dispatch("image", "edit"))

print()
print("graph: the recipe")
g = engines.graph_for("image", "charsheet", ARGS, FAST)
enc = by_class(g, "TextEncodeQwenImage21")
check("one TextEncodeQwenImage21", len(enc) == 1, enc)
enc_id, enc_node = enc[0]
ins = enc_node["inputs"]
check("the reference arrives on the dotted autogrow key images.image_1", "images.image_1" in ins, sorted(ins))
check("...and there is no nested 'images' dict", "images" not in ins, sorted(ins))
load = g[ins["images.image_1"][0]]
check("that key is fed by a LoadImage of the reference", load["class_type"] == "LoadImage"
      and load["inputs"]["image"] == "hero.png", load)
check("the prompt is passed through untouched", ins["prompt"] == ARGS["prompt"])
check("negative prompt is empty, encoder resolution 1024", ins["negative_prompt"] == "" and ins["resolution"] == 1024, ins)
check("encoder clip is the qwen_image CLIP type",
      g[ins["clip"][0]]["inputs"]["type"] == "qwen_image", g[ins["clip"][0]])
(ks_id, ks), = by_class(g, "KSampler")
lat_id, lat = by_class(g, "EmptyLatentImage")[0]
check("KSampler takes the EmptyLatentImage", ks["inputs"]["latent_image"] == [lat_id, 0], ks["inputs"]["latent_image"])
check("KSampler does NOT take the encoder's own latent", ks["inputs"]["latent_image"][0] != enc_id)
check("KSampler recipe: 25 steps, cfg 1, euler, simple, denoise 1",
      (ks["inputs"]["steps"], ks["inputs"]["cfg"], ks["inputs"]["sampler_name"],
       ks["inputs"]["scheduler"], ks["inputs"]["denoise"]) == (25, 1, "euler", "simple", 1), ks["inputs"])
check("positive/negative come from the encoder", ks["inputs"]["positive"] == [enc_id, 0]
      and ks["inputs"]["negative"] == [enc_id, 1])
check("the seed is the request's", ks["inputs"]["seed"] == 7)
check("VAEDecode -> SaveImage", len(by_class(g, "VAEDecode")) == 1 and len(by_class(g, "SaveImage")) == 1)

print()
print("graph: sizes (3:2, multiples of 32, within 3% of the target area)")
from engines import qwen_image as qi
for label, mp, want in (("Quick (1 MP)", 1.0, (1216, 832)), ("Balanced (3.4 MP)", 3.4, (2272, 1504)),
                        ("Large (6 MP)", 6.0, (3008, 2016))):
    gg = engines.graph_for("image", "charsheet", dict(ARGS, size=label), FAST)
    l = by_class(gg, "EmptyLatentImage")[0][1]["inputs"]
    got = (l["width"], l["height"])
    check("%s -> %dx%d" % (label, want[0], want[1]), got == want, got)
    check("%s: multiples of 32" % label, got[0] % 32 == 0 and got[1] % 32 == 0)
    check("%s: within 3%% of %s MP" % (label, mp), abs(got[0] * got[1] / 1e6 - mp) / mp < 0.03, got)
    check("%s: 3:2 within a snap" % label, abs(got[0] / got[1] - 1.5) < 0.05, got)
l = by_class(g, "EmptyLatentImage")[0][1]["inputs"]
check("no size asked -> Balanced", (l["width"], l["height"]) == (2272, 1504), l)
size_field = next(f for f in engines.fields("image", "charsheet") if f["id"] == "size")
check("Balanced is the declared default", size_field["default"] == "Balanced (3.4 MP)", size_field)

print()
print("graph: optional speed nodes")
check("with the nodes: attention backend then cache, before the sampler",
      by_class(g, "ModelAttentionBackend") and by_class(g, "QwenImage21Cache"))
cache_id = by_class(g, "QwenImage21Cache")[0][0]
attn_id, attn = by_class(g, "ModelAttentionBackend")[0]
check("KSampler reads the cache node", ks["inputs"]["model"] == [cache_id, 0], ks["inputs"]["model"])
check("the cache reads the attention backend", g[cache_id]["inputs"]["model"] == [attn_id, 0])
check("attention backend is 'comfy kitchen attention'", attn["inputs"]["attention"] == "comfy kitchen attention")
plain = engines.graph_for("image", "charsheet", ARGS, MODELS)
check("without them: neither node is in the graph",
      not by_class(plain, "ModelAttentionBackend") and not by_class(plain, "QwenImage21Cache"))
(pks_id, pks), = by_class(plain, "KSampler")
unet_id = [i for i, n in plain.items() if n["class_type"] in ("UNETLoader", "UnetLoaderGGUF")][0]
check("without them: KSampler reads the UNet loader directly", pks["inputs"]["model"] == [unet_id, 0], pks["inputs"])
one = engines.graph_for("image", "charsheet", ARGS, dict(MODELS, cs_cache=True))
check("only the cache present: it reads the UNet, sampler reads it",
      not by_class(one, "ModelAttentionBackend") and by_class(one, "QwenImage21Cache")[0][1]["inputs"]["model"][0] == unet_id)


def refs_ok(graph):
    """Every [node_id, slot] input points at a node that exists."""
    for nid, n in graph.items():
        for k, v in n["inputs"].items():
            if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str) and v[0] not in graph:
                return "%s.%s -> %s" % (nid, k, v[0])
    return None
check("both graphs validate: every wire lands on a node", refs_ok(g) is None and refs_ok(plain) is None,
      (refs_ok(g), refs_ok(plain)))

print()
print("graph: no reference is refused in plain words")
try:
    engines.graph_for("image", "charsheet", {"prompt": "x", "seed": 1}, MODELS)
    check("no reference -> ValueError", False)
except ValueError as e:
    check("no reference -> a plain sentence", "picture" in str(e).lower(), str(e))

print()
print("optional node declarations")
check("the pack declares both speed nodes",
      engines.optional_nodes().get("cs_attn") == "ModelAttentionBackend"
      and engines.optional_nodes().get("cs_cache") == "QwenImage21Cache", engines.optional_nodes())
check("the sheet needs the same three models t2i does",
      engines.abilities(MODELS).get("charsheet") is True)

print()
print("%d failed" % len(FAILED))
sys.exit(1 if FAILED else 0)
