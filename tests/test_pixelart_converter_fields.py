"""Phase 5 gate: the Pixel Art room's five opt-in converter boxes (Keep small details,
Keep accent colours, Clean specks, Hard edges, Keep edges crisp) exist, default off,
and reach the vendored quantise call only when ticked.

Run: python3 tests/test_pixelart_converter_fields.py
"""
import io, os, sys
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail else ""))
    if not cond: FAILED.append(name)

import engines
from engines import pixelart

NEW = [("pixel_detail", "Keep small details", "downscale", "detail"),
       ("pixel_accents", "Keep accent colours", "palette_mode", "balanced"),
       ("pixel_specks", "Clean specks", "clean_specks", True),
       ("pixel_hard_edges", "Hard edges", "alpha_mode", "hard"),
       ("pixel_dither_edges", "Keep edges crisp", "dither_gate", 0.1)]

print("(a) the five fields")
fields = engines.fields("image", "pixelart")
sprite = sorted((f for f in fields if f.get("group") == "Sprite"), key=lambda f: f["order"])
check("the Sprite group lists the six old fields first, then the five new ones in order",
      [f["id"] for f in sprite] == ["pixel_size", "pixel_height", "pixel_colors", "pixel_palette",
                                    "pixel_grid", "pixel_dither"] + [n[0] for n in NEW],
      [f["id"] for f in sprite])
byid = {f["id"]: f for f in fields}
for fid, label, _, _ in NEW:
    f = byid.get(fid, {})
    check(fid + " is a Sprite checkbox, advanced, default False, labelled " + repr(label),
          f.get("label") == label and f.get("type") == "checkbox" and f.get("default") is False
          and f.get("group") == "Sprite" and f.get("tier") == "advanced" and f.get("hint"), f)
check("DITHER_EDGE_GATE is 0.1", pixelart.DITHER_EDGE_GATE == 0.1)

print()
print("(b) what _post_quantise hands to quantise_and_dither")
# the converter is replaced by a recorder, so the bytes need not be a real picture and this
# section runs without numpy/Pillow (the bare release-gate container has neither)
def png():
    return b"stand-in render bytes"

calls = []
def recorder(src, out, **kw):
    calls.append(kw)
    with open(out, "wb") as f:
        f.write(b"stand-in sprite bytes")
real = pixelart._quantise.quantise_and_dither
pixelart._quantise.quantise_and_dither = recorder
try:
    base = {"pixel_size": 16, "pixel_colors": 8, "pixel_dither": 0.0}
    OLD = {"target_size", "n_colors", "dither_strength", "alpha_path", "downscale"}
    calls.clear(); pixelart._post_quantise(png(), "r.png", dict(base))
    check("all off: only the five existing keywords are passed", set(calls[0]) == OLD, sorted(calls[0]))
    check("all off: downscale is nearest", calls[0]["downscale"] == "nearest")
    calls.clear(); pixelart._post_quantise(png(), "r.png", dict(base, **{n[0]: False for n in NEW}))
    check("all explicitly False: same call as all off", set(calls[0]) == OLD, sorted(calls[0]))
    calls.clear(); pixelart._post_quantise(png(), "r.png", dict(base, pixel_grid=pixelart.GRID_MODAL))
    check("Cleanest grid still maps to modal", calls[0]["downscale"] == "modal")
    for fid, _, kw, val in NEW:
        calls.clear(); pixelart._post_quantise(png(), "r.png", dict(base, **{fid: True}))
        extra = set(calls[0]) - OLD
        if kw == "downscale":
            check(fid + " on: downscale is 'detail', nothing else added", calls[0]["downscale"] == "detail" and not extra, calls[0])
        else:
            check(fid + " on: adds only %s=%r" % (kw, val), extra == {kw} and calls[0][kw] == val
                  and calls[0]["downscale"] == "nearest", calls[0])
    calls.clear(); pixelart._post_quantise(png(), "r.png", dict(base, pixel_grid=pixelart.GRID_MODAL, pixel_detail=True))
    check("Keep small details overrides the Pixel grid choice", calls[0]["downscale"] == "detail")
finally:
    pixelart._quantise.quantise_and_dither = real

print()
print("(c) end to end, every box on")
import importlib.util
if not (importlib.util.find_spec("PIL") and importlib.util.find_spec("numpy")):
    print("  SKIP  this check needs Pillow and numpy (requirements.txt), not installed here")
else:
    from PIL import Image
    import numpy as np
    src = Image.effect_mandelbrot((32, 32), (-3, -2.5, 2, 2.5), 20).convert("RGBA")
    src.putalpha(Image.linear_gradient("L").resize((32, 32)))   # alpha 0..255: soft edges to harden
    buf = io.BytesIO(); src.save(buf, "PNG")
    data, name = pixelart._post_quantise(
        buf.getvalue(), "r.png", dict(base, pixel_size=16, pixel_detail=True, pixel_accents=True,
                                      pixel_specks=True, pixel_hard_edges=True, pixel_dither_edges=True,
                                      pixel_dither=0.2))
    im = Image.open(io.BytesIO(data))
    alphas = {int(a) for a in np.asarray(im.convert("RGBA"))[:, :, 3].ravel()}
    check("the result is a 16x16 PNG", im.format == "PNG" and im.size == (16, 16) and name == "sprite_16x16.png", (im.format, im.size))
    check("alpha holds only 0 and 255", alphas <= {0, 255} and len(alphas) == 2, sorted(alphas))

print()
print("(d) the API's engine listing reports the five as False")
import _scratch_config  # noqa: F401
spec = importlib.util.spec_from_file_location("srv_pxfields", os.path.join(ROOT, "server.py"))
srv = importlib.util.module_from_spec(spec); spec.loader.exec_module(srv)
payload = srv.Handler.engines_payload(srv.Handler.__new__(srv.Handler), {})
image = payload.get("image", {})
mode = next((m for m in image.get("modes", []) if m.get("id") == "pixelart"), {})
listed = {f["id"]: f for f in mode.get("fields", [])}
for fid, *_ in NEW:
    check(fid + " is listed with default False", fid in listed and listed[fid].get("default") is False, listed.get(fid))

print()
if FAILED:
    print("FAILED: %d" % len(FAILED)); sys.exit(1)
print("ALL PASS")
