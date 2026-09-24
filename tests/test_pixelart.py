"""Acceptance gate for the pixelart engine pack: graph shape (both shape_image
branches), pack contract fields, and the vendored quantise transform's byte
parity with the original internal sprite script.

Run: python3 tests/test_pixelart.py
"""
import json, os, subprocess, sys, tempfile
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
G = os.path.join(HERE, "golden")

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + detail) if not cond and detail else ""))
    if not cond: FAILED.append(name)

import engines

models = json.load(open(os.path.join(G, "models.json")))
args = {"prompt": "a dagger interceptor jet", "seed": 1, "steps": 20, "cfg": 2.5,
        "width": 1024, "height": 1024, "resolution": 1024, "negative": ""}

print("free branch (no shape_image): a plain t2i graph plus the cutout chain")
g_free = engines.graph_for("image", "pixelart", args, models)
check("no leftover SaveImage node '10' from qwen_t2i_graph", "10" not in g_free)
check("ends in a cutout chain feeding ITS OWN SaveImage",
      g_free["24"]["class_type"] == "SaveImage" and g_free["24"]["inputs"]["images"] == ["23", 0])
check("background removal is wired from the render, not a second load",
      g_free["21"]["inputs"]["image"] == ["9", 0])
check("filename_prefix is blackwire/PIXELART, not owui/pixelart",
      g_free["24"]["inputs"]["filename_prefix"] == "blackwire/PIXELART")

print()
print("held branch (shape_image given): the edit graph, wrapping the user's prompt")
args_held = dict(args, shape_image="dagger_mask_1024.png", prompt="a red dragon")
g_held = engines.graph_for("image", "pixelart", args_held, models)
check("no leftover SaveImage node '10' from qwen_edit_graph", "10" not in g_held)
check("the shape image is wired in as the edit reference",
      g_held.get("101", {}).get("inputs", {}).get("image") == "dagger_mask_1024.png")
instruction = g_held["4"]["inputs"]["prompt"]
check("the instruction wraps the user's own prompt",
      "a red dragon" in instruction and "silhouette" in instruction.lower())
check("held branch also ends in its own cutout chain",
      g_held["24"]["inputs"]["filename_prefix"] == "blackwire/PIXELART")

print()
print("pack contract: pixelart does not shadow qwen-image's cap identity")
check("cap_word('image') is still qwen-image's 'picture'", engines.cap_word("image") == "picture")
check("cap_order('image') is still qwen-image's declared 1", engines.cap_order("image") == 1)
check("pixelart's own describe() never names an engine",
      next(p for p in engines.packs() if p["id"] == "pixelart")["describe"]({}) == "")
pixelart_pack = next(p for p in engines.packs() if p["id"] == "pixelart")
check("pixelart provides mode 'pixelart' requiring the picture models AND background removal",
      set(pixelart_pack["provides"]["pixelart"]) == {"qwen_unet", "qwen_clip", "qwen_vae", "birefnet_model"})
qual = engines.quality("image", "pixelart")
check("exactly one default quality tier", sum(1 for t in qual if t.get("default")) == 1, str(qual))
check("at least one preset", len(engines.presets("image", "pixelart")) >= 1)
post_fn = engines.post_for("image", "pixelart")
check("pixelart declares a post step, t2i/edit do not",
      post_fn is not None and engines.post_for("image", "t2i") is None)

print()
print("the vendored quantise transform reproduces the original script byte-for-byte")
proof_dir = os.path.join(os.path.expanduser("~"), ".cache", "bwf-pixel-proof",
                          "scripts", "pixel-lane-proof")
orig_script = os.path.join(os.path.expanduser("~"), ".cache", "bwf-pixel-proof",
                            "scripts", "quantise.py")
src = os.path.join(proof_dir, "raw_sdxl_output.png")
if not (os.path.isfile(src) and os.path.isfile(orig_script)):
    print("  SKIP  fixed input or original script not found on this machine")
else:
    with open(src, "rb") as f:
        input_bytes = f.read()
    import engines.pixelart as pixelart_mod
    pack_bytes, pack_name = pixelart_mod._post_quantise(input_bytes, "raw_sdxl_output.png", {})
    with tempfile.TemporaryDirectory() as td:
        out_path = os.path.join(td, "orig_sprite.png")
        subprocess.run([sys.executable, orig_script, src, out_path, "--alpha", src],
                        check=True, capture_output=True)
        with open(out_path, "rb") as f:
            orig_bytes = f.read()
    check("pack's post output is byte-identical to the original script's output",
          pack_bytes == orig_bytes, "pack=%d bytes orig=%d bytes" % (len(pack_bytes), len(orig_bytes)))

print()
print(("FAILED: %d" % len(FAILED)) if FAILED else "ALL PASS")
sys.exit(1 if FAILED else 0)
