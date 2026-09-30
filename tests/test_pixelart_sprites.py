"""Acceptance gate for PA-1: any sprite size, a clean pixel grid, a palette you choose.

All on synthetic PNGs (no render): the post step (`engines.pixelart._post_quantise`)
and the vendored downscale, plus the API's 400 for a bad palette, plus a golden that
pins today's square requests to what they produced before PA-1.

  (1) 1024x1024 -> 24x32 / 48x24 / 24x24, both grid modes: exact size, only palette
      colours (no anti-aliasing), the crop keeps the middle.
  (2) modal 1024 -> 24 no longer raises; a 3x3-block checkerboard keeps the majority
      colour of every box.
  (3) pixel_palette: 4 colours in -> only those 4 out; a bad entry -> 400 with a
      sentence; empty == no field at all, byte for byte.
  (4) transparent pixels take no palette slot.
  (5) golden: no pixel_height / palette / grid -> the same sprite and the same graph
      as before (tests/golden/pixelart_*); PA_BASE_TREE=<pre-PA-1 checkout> also
      compares bytes against that tree live.

Run: python3 tests/test_pixelart_sprites.py
"""
import io, json, os, subprocess, sys
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
G = os.path.join(HERE, "golden")

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail else ""))
    if not cond: FAILED.append(name)

try:
    import numpy as np
    from PIL import Image
except ImportError as e:
    print("SKIP: %s (requirements.txt)" % e); sys.exit(0)

import engines
import engines.pixelart as pa
from engines import _quantise


def png(arr):
    b = io.BytesIO(); Image.fromarray(arr).save(b, "PNG"); return b.getvalue()

def decode(b):
    return np.array(Image.open(io.BytesIO(b)).convert("RGBA"))

def post(arr, **args):
    return pa._post_quantise(png(arr), "render.png", args)

GRID_NEAREST, GRID_MODAL = "Sharp (nearest)", "Cleanest (most common colour)"
RED1, RED2, GREEN, BLUE, GREY = (200, 30, 30), (230, 140, 20), (40, 170, 70), (30, 60, 220), (90, 90, 90)
PAL = [RED1, RED2, GREEN, BLUE]
PAL_TXT = ", ".join("#%02X%02X%02X" % c for c in PAL)

def crop_render():
    """1024x1024, opaque. Background GREEN; RED2 bands top/bottom (y<256, y>=768),
    RED1 bands left/right (x<128, x>=896) painted over them; a BLUE marker at the centre."""
    a = np.zeros((1024, 1024, 4), np.uint8); a[..., 3] = 255
    a[..., :3] = GREEN
    a[:256, :, :3] = RED2; a[768:, :, :3] = RED2
    a[:, :128, :3] = RED1; a[:, 896:, :3] = RED1
    a[432:592, 432:592, :3] = BLUE
    return a

def colours(arr, alpha_min=1):
    flat = arr.reshape(-1, 4); flat = flat[flat[:, 3] >= alpha_min]
    return {tuple(int(v) for v in c) for c in np.unique(flat[:, :3], axis=0)}

print("(1) sprite sizes: exact size, palette colours only, centre-crop keeps the middle")
R = crop_render()
for (w, h) in ((24, 32), (48, 24), (24, 24)):
    for grid in (GRID_NEAREST, GRID_MODAL):
        tag = "%dx%d %s" % (w, h, "modal" if grid == GRID_MODAL else "nearest")
        try:
            data, name = post(R, pixel_size=w, pixel_height=h, pixel_palette=PAL_TXT, pixel_grid=grid)
            out = decode(data)
        except Exception as e:
            check(tag + ": renders", False, repr(e)); continue
        check(tag + ": exact size", out.shape[:2] == (h, w) and name == "sprite_%dx%d.png" % (w, h), (out.shape, name))
        check(tag + ": only palette colours (no anti-aliasing)", colours(out) <= set(PAL), colours(out) - set(PAL))
        check(tag + ": centred marker survives", tuple(out[h // 2, w // 2, :3]) == BLUE, tuple(out[h // 2, w // 2, :3]))
        has = colours(out)
        if (w, h) == (24, 32):
            check(tag + ": the left/right bands are cropped away, top/bottom stay", RED1 not in has and RED2 in has, has)
        if (w, h) == (48, 24):
            check(tag + ": the top/bottom bands are cropped away, left/right stay", RED2 not in has and RED1 in has, has)
        if (w, h) == (24, 24):
            check(tag + ": square from a square render is not cropped", RED1 in has and RED2 in has, has)
# auto palette (no chosen colours) also holds the colour count and stays in its own palette
data, _ = post(R, pixel_size=24, pixel_height=32, pixel_colors=3)
out = decode(data)
check("24x32 auto palette: exact size and at most 3 colours", out.shape[:2] == (32, 24) and len(colours(out)) <= 3, (out.shape, colours(out)))

print()
print("(2) modal downscale: any integer target, majority colour per box")
chk = np.zeros((1024, 1024, 3), np.uint8)
yy, xx = np.mgrid[0:1024, 0:1024]
cb = ((xx // 3 + yy // 3) % 2).astype(bool)
chk[cb] = (250, 250, 250); chk[~cb] = (5, 5, 5)
try:
    got = _quantise._modal_downscale(chk, 24)
    check("modal 1024 -> 24 does not raise", got.shape == (24, 24, 3), got.shape)
except Exception as e:
    got = None
    check("modal 1024 -> 24 does not raise", False, repr(e))
def ref_modal(arr, tw, th):
    H, W = arr.shape[:2]
    out = np.zeros((th, tw, arr.shape[2]), arr.dtype)
    for j in range(th):
        for i in range(tw):
            box = arr[j * H // th:(j + 1) * H // th, i * W // tw:(i + 1) * W // tw].reshape(-1, arr.shape[2])
            vals, cnt = np.unique(box, axis=0, return_counts=True)
            out[j, i] = vals[np.argmax(cnt)]
    return out
if got is not None:
    check("every 24x24 box holds the majority colour of its floor-mapped source box", np.array_equal(got, ref_modal(chk, 24, 24)))
    n_dark = int((got[..., 0] == 5).sum()); n_light = int((got[..., 0] == 250).sum())
    check("the checkerboard gives both colours and nothing else", n_dark + n_light == 576 and n_dark > 0 and n_light > 0, (n_dark, n_light))
try:
    g2 = _quantise._modal_downscale(chk, (24, 32))
    check("modal to a non-square 24x32 matches the brute-force box vote", np.array_equal(g2, ref_modal(chk, 24, 32)))
except Exception as e:
    check("modal to a non-square 24x32 matches the brute-force box vote", False, repr(e))
# one clear majority: 10 of 16 columns A, 6 B, in a 16x16 source -> 1x1 target is A
blk = np.zeros((16, 16, 3), np.uint8); blk[:, :10] = (10, 10, 10); blk[:, 10:] = (200, 200, 200)
try:
    check("a 60/40 box takes the 60", tuple(_quantise._modal_downscale(blk, 1)[0, 0]) == (10, 10, 10))
except Exception as e:
    check("a 60/40 box takes the 60", False, repr(e))
# every source pixel is counted exactly once (boxes tile the source)
cnt = np.zeros((1024, 1), np.int64)
edges = [i * 1024 // 24 for i in range(25)]
check("floor box edges tile the source exactly once", edges[0] == 0 and edges[-1] == 1024 and all(b > a for a, b in zip(edges, edges[1:])))
# transparent never outvotes opaque
src = np.zeros((4, 4, 3), np.uint8); src[:] = (1, 1, 1)
src[0, 0] = (9, 9, 9)
opq = np.zeros((4, 4), bool); opq[0, 0] = True
try:
    check("one opaque pixel beats fifteen transparent ones", tuple(_quantise._modal_downscale(src, 1, opq)[0, 0]) == (9, 9, 9))
    check("a wholly transparent box still answers", tuple(_quantise._modal_downscale(src, 1, np.zeros((4, 4), bool))[0, 0]) == (1, 1, 1))
except Exception as e:
    check("transparent never outvotes opaque", False, repr(e))
# and through the post step, with a cut-out render
cut = np.zeros((1024, 1024, 4), np.uint8); cut[..., :3] = GREEN
cut[:, :, 3] = 0; cut[0:512, 0:512, 3] = 255; cut[0:512, 0:512, :3] = BLUE
cut[512:, 512:, :3] = RED1     # invisible (alpha 0) colour must not leak
try:
    data, _ = post(cut, pixel_size=24, pixel_grid=GRID_MODAL, pixel_colors=2)
    o = decode(data)
    check("modal post: transparent stays transparent, opaque stays opaque", o[0, 0, 3] == 255 and o[23, 23, 3] == 0, (o[0, 0], o[23, 23]))
except Exception as e:
    check("modal post: transparent stays transparent, opaque stays opaque", False, repr(e))

print()
print("(3) pixel_palette")
rng = np.random.RandomState(7)
noise = np.zeros((256, 256, 4), np.uint8); noise[..., :3] = rng.randint(0, 256, (256, 256, 3)); noise[..., 3] = 255
data, _ = post(noise, pixel_size=32, pixel_palette=PAL_TXT)
check("a 4-colour list: the sprite uses only those colours", colours(decode(data)) <= set(PAL) and len(colours(decode(data))) >= 2, colours(decode(data)))
data, _ = post(noise, pixel_size=32, pixel_palette="1a1c2c 5d275d\nb13e53;ffcd75", pixel_colors=2)
check("spaces, new lines, no # and upper/lower case are all fine; Colours is ignored",
      colours(decode(data)) <= {(0x1a, 0x1c, 0x2c), (0x5d, 0x27, 0x5d), (0xb1, 0x3e, 0x53), (0xff, 0xcd, 0x75)})
for bad, why in (("#1a1c2c, #zzz", "not a hex colour"), ("#1a1c2c", "one colour"), ("#12345, #ffffff", "five digits"),
                 (", ".join("#%06x" % i for i in range(257)), "257 colours")):
    try:
        pa.parse_palette(bad); ok, msg = False, ""
    except ValueError as e:
        ok, msg = True, str(e)
    except AttributeError as e:
        ok, msg = False, repr(e)
    check("bad palette (%s): ValueError sentence" % why, ok and msg.endswith(".") and "Traceback" not in msg, msg)
try:
    check("empty palette parses to []", pa.parse_palette("") == [] and pa.parse_palette("  ") == [] and pa.parse_palette(None) == [])
    check("2 and 256 colours are accepted", len(pa.parse_palette("#000000,#ffffff")) == 2 and len(pa.parse_palette(", ".join("#%06x" % i for i in range(256)))) == 256)
except AttributeError as e:
    check("parse_palette exists", False, repr(e))

print()
print("(3b) load_locked_palette: the documented #rrggbb format, comments, bad lines")
import tempfile
def load(text):
    with tempfile.TemporaryDirectory() as td:
        f = os.path.join(td, "p.txt"); open(f, "w").write(text)
        return [tuple(int(v) for v in c) for c in _quantise.load_locked_palette(f)]
def loads(text):
    try:
        return load(text)
    except Exception as e:
        return e
want = [(0x1a, 0x1c, 0x2c), (0x5d, 0x27, 0x5d)]
check("a file of #rrggbb lines loads", loads("#1a1c2c\n#5D275D\n") == want, loads("#1a1c2c\n#5D275D\n"))
mixed = "# my palette\n\n; also a comment\n#1a1c2c\n  5d275d  \n#note: dark\n"
check("comments, blanks and both forms mixed load", loads(mixed) == want, loads(mixed))
check("a bare-hex file (the old behaviour) is unchanged", loads("1a1c2c\n5d275d\n") == want)
check("a trailing note after a colour is still allowed", loads("1a1c2c # dark\n#5d275d ; purple\n") == want)
e = loads("#1a1c2c\n# fine\nnot-a-colour\n")
check("a bad line raises and names its line number", isinstance(e, ValueError) and "line 3" in str(e), repr(e))
check("a # line that is not #+6 hex digits is a comment", loads("#1a1c2c\n#12345\n") == [want[0]])
e = loads("# only comments\n")
check("a file with no colours still raises", isinstance(e, ValueError) and "no colours" in str(e), repr(e))

# through the API: a bad palette is a 400 with a sentence, before any render
import _scratch_config  # noqa: E402,F401
import importlib.util
spec = importlib.util.spec_from_file_location("srv_pa1", os.path.join(ROOT, "server.py"))
srv = importlib.util.module_from_spec(spec); spec.loader.exec_module(srv)
LANE = {"id": "pa1", "name": "PA1", "box": "b", "note": "", "host": "127.0.0.1", "port": 1, "caps": ["image"]}
srv.LANE_BY_ID[LANE["id"]] = LANE
if LANE not in srv.LANES: srv.LANES.append(LANE)
with srv.STATE_LOCK: srv.LANE_STATE[LANE["id"]] = {"up": True}
with srv.DISCOVERY_LOCK:
    srv.DISCOVERY[LANE["id"]] = {"models": {"qwen_unet": "qwen_image_2.1.gguf", "qwen_clip": "qwen3vl_8b.safetensors",
                                            "qwen_vae": "qwen_image_2.1_vae.safetensors", "birefnet_model": "birefnet.safetensors"},
                                 "pools": {}, "checked": 1.0, "err": ""}
sent = []
srv.dispatch = lambda lane, graph, kind, mode, meta: (sent.append(meta) or {"ok": True, "job": {"id": "x"}, "notes": []})
DEFAULTS = {"width": 1024, "height": 1024, "resolution": 1024, "steps": 20, "cfg": 2.5}
def api(values):
    values = dict(DEFAULTS, **values)
    body = {"lane": "pa1", "kind": "image", "mode": "pixelart", "confirm": True,
            "prompt": "Pixel art sprite, a knight", "values": values}
    obj = srv.Handler.__new__(srv.Handler); res = []
    obj.read_json = lambda: body
    obj.send_json = lambda payload, code=200: res.append((payload, code))
    srv.Handler.api_generate(obj)
    return res[-1]
pl, code = api({"pixel_palette": "#1a1c2c, nope"})
check("API: a bad palette entry is 400 with a sentence", code == 400 and "nope" in (pl.get("error") or "") and pl["error"].endswith("."), (code, pl))
check("API: nothing was dispatched for it", not sent, sent)
pl, code = api({"pixel_palette": PAL_TXT, "pixel_size": 24, "pixel_height": 32})
check("API: a good palette and 24x32 is accepted", code == 200 and pl.get("ok") and sent and sent[-1]["args"].get("pixel_height") == 32, (code, pl))
pl, code = api({"pixel_height": 5})
check("API: a silly height is 400 with a sentence", code == 400 and "height" in (pl.get("error") or "").lower(), (code, pl))
pl, code = api({"pixel_grid": "whatever"})
check("API: an unknown grid choice is 400", code == 400, (code, pl))

print()
print("(4) transparent pixels take no palette slot")
sp = np.zeros((64, 64, 4), np.uint8); sp[..., :3] = (255, 0, 255)      # a loud colour under alpha 0
sp[8:24, 8:24] = (*RED1, 255); sp[8:24, 40:56] = (*GREEN, 255); sp[40:56, 24:40] = (*BLUE, 255)
for grid in (GRID_NEAREST, GRID_MODAL):
    data, _ = post(sp, pixel_size=64, pixel_colors=3, pixel_grid=grid)
    o = decode(data)
    check("3 opaque colours + colours=3 -> exactly those 3 (%s)" % ("modal" if grid == GRID_MODAL else "nearest"),
          colours(o, 8) == {RED1, GREEN, BLUE}, colours(o, 8))

print()
print("(5) golden: a request with none of the new fields is what it was before")
def golden_render():
    r = np.random.RandomState(11)
    a = np.zeros((1024, 1024, 4), np.uint8)
    yy, xx = np.mgrid[0:1024, 0:1024]
    body = ((xx - 512) ** 2 / 380 ** 2 + (yy - 540) ** 2 / 440 ** 2) < 1
    a[body, :3] = np.stack([(xx[body] // 4) % 256, (yy[body] // 3) % 256, (xx[body] + yy[body]) // 8 % 256], 1)
    a[body, :3] = (a[body, :3] // 40) * 40 + r.randint(0, 6, (int(body.sum()), 3)).astype(np.uint8)
    a[300:420, 400:640, :3] = (220, 40, 40); a[body, 3] = 255
    return a
GR = golden_render()
CASES = {"default": {}, "custom": {"pixel_size": 32, "pixel_colors": 6, "pixel_dither": 0.12}}
WRITE = os.environ.get("PA_WRITE_GOLDEN")
for name, args in CASES.items():
    data, fname = post(GR, **args)
    path = os.path.join(G, "pixelart_sprite_%s.png" % name)
    if WRITE:
        open(path, "wb").write(data); print("  wrote", path)
    want = open(path, "rb").read()
    check("%s: same sprite pixels as the golden" % name, np.array_equal(decode(data), decode(want)))
    check("%s: same file name (sprite_NxN)" % name, fname == "sprite_%dx%d.png" % ((args.get("pixel_size", 64),) * 2), fname)
    data2, _ = post(GR, **dict(args, pixel_height=0, pixel_palette="", pixel_grid=GRID_NEAREST))
    check("%s: the new fields at their defaults change nothing, byte for byte" % name, data2 == data)
models = json.load(open(os.path.join(G, "models.json")))
gargs = {"prompt": "a dagger interceptor jet", "seed": 1, "steps": 20, "cfg": 2.5,
         "width": 1024, "height": 1024, "resolution": 1024, "negative": ""}
graph = {"free": engines.graph_for("image", "pixelart", gargs, models),
         "held": engines.graph_for("image", "pixelart", dict(gargs, shape_image="mask.png"), models)}
gpath = os.path.join(G, "pixelart_graph.json")
if WRITE:
    json.dump(graph, open(gpath, "w"), indent=1, sort_keys=True); print("  wrote", gpath)
check("the graphs (free and held) are the same as before", json.loads(json.dumps(graph)) == json.load(open(gpath)))
check("new pixel fields in the request do not touch the graph",
      engines.graph_for("image", "pixelart", dict(gargs, pixel_height=32, pixel_palette=PAL_TXT, pixel_grid=GRID_MODAL), models) == graph["free"])
base = os.environ.get("PA_BASE_TREE")
if base and os.path.isdir(base):
    # the quantiser prints a "wrote ..." line to stdout, so the old tree hands its
    # sprites back through files, not through stdout
    import tempfile
    code = ("import sys, io; sys.path.insert(0, %r); sys.dont_write_bytecode = True\n"
            "import numpy as np; from PIL import Image\n"
            "import engines.pixelart as p\n"
            "a = np.array(Image.open(sys.argv[1])); b = io.BytesIO(); Image.fromarray(a).save(b, 'PNG')\n"
            "for i, args in enumerate((%r, %r)):\n"
            "    d, n = p._post_quantise(b.getvalue(), 'r.png', args)\n"
            "    open(sys.argv[2] + str(i), 'wb').write(d)\n") % (base, CASES["default"], CASES["custom"])
    with tempfile.TemporaryDirectory() as td:
        Image.fromarray(GR).save(os.path.join(td, "render.png"))
        r = subprocess.run([sys.executable, "-c", code, os.path.join(td, "render.png"), os.path.join(td, "out")], capture_output=True)
        ok = r.returncode == 0
        for i, a in enumerate(CASES.values()):
            ok = ok and open(os.path.join(td, "out%d" % i), "rb").read() == post(GR, **a)[0]
    check("live vs the pre-PA-1 tree at PA_BASE_TREE: byte-identical sprites", ok, r.stderr[-300:].decode())
else:
    print("  SKIP  live comparison (set PA_BASE_TREE to a pre-PA-1 checkout)")

print()
print(("FAILED: %d" % len(FAILED)) if FAILED else "ALL PASS")
sys.exit(1 if FAILED else 0)
