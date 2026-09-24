"""C4 -- server.py's strip_metadata() copied pixels via Image.getdata() /
Image.putdata(), a per-pixel Python list round-trip through an API Pillow's
own DeprecationWarning says is removed in Pillow 14 (2027-10-15). Proves the
replacement (Image.frombytes(src.mode, src.size, src.tobytes()), with the
palette copied separately for "P" mode) is byte-identical for RGB, RGBA and
P-mode PNGs, strips the metadata it always did, and raises no getdata
DeprecationWarning doing it.

No HTTP server: strip_metadata() is a plain function, called directly.

Run:  python3 tests/test_strip_metadata_pixels.py
Exit 0 = all good. No third-party imports beyond Pillow (SKIPs without it).
"""
import importlib.util
import io
import json
import os
import sys
import tempfile
import warnings

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FAILED = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond:
        print("        -> %s" % (detail,))
        FAILED.append(name)


try:
    from PIL import Image
    from PIL.PngImagePlugin import PngInfo
except ImportError:
    print("  SKIP  Pillow is not installed -- this test needs it")
    sys.exit(0)

SCRATCH = tempfile.mkdtemp(prefix="bwf_c4_")
CONFIG = os.path.join(SCRATCH, "config.json")
with open(CONFIG, "w") as f:
    json.dump({
        "port": 1, "bind": "127.0.0.1",
        "timing": {"poll_seconds": 300, "job_poll_seconds": 300},
        "lanes": [{"id": "t", "name": "t", "host": "127.0.0.1", "port": 1, "caps": ["image"]}],
    }, f)
os.environ["GENCENTER_CONFIG"] = CONFIG
os.environ["GENCENTER_DATA"] = os.path.join(SCRATCH, "data")

spec = importlib.util.spec_from_file_location("srv_c4", os.path.join(ROOT, "server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)


def make_png(mode):
    if mode == "P":
        im = Image.new("P", (5, 4))
        pal = []
        for i in range(256):
            pal += [i, (i * 3) % 256, (i * 7) % 256]
        im.putpalette(pal)
        im.putdata([0, 10, 20, 30, 40] * 4)
    elif mode == "RGBA":
        im = Image.new("RGBA", (5, 4))
        im.putdata([(r % 256, (r * 2) % 256, (r * 5) % 256, (r * 7) % 256) for r in range(20)])
    else:
        im = Image.new("RGB", (5, 4))
        im.putdata([(r % 256, (r * 2) % 256, (r * 5) % 256) for r in range(20)])
    buf = io.BytesIO()
    info = PngInfo()
    info.add_text("prompt", '{"1": {"class_type": "Secret"}}')
    im.save(buf, format="PNG", pnginfo=info)
    return im, buf.getvalue()


for mode in ("RGB", "RGBA", "P"):
    src_im, raw = make_png(mode)
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always", DeprecationWarning)
        out, ctype, stripped = srv.strip_metadata(raw, "image/png", "test.png")
        getdata_warned = any("getdata" in str(x.message) for x in w)
    check("%s: strip_metadata reports stripped=True" % mode, stripped is True, stripped)
    check("%s: no getdata/putdata DeprecationWarning" % mode, not getdata_warned)
    clean_im = Image.open(io.BytesIO(out))
    clean_im.load()
    check("%s: mode is preserved" % mode, clean_im.mode == src_im.mode,
          (clean_im.mode, src_im.mode))
    check("%s: pixels are byte-identical" % mode, clean_im.tobytes() == src_im.tobytes())
    if mode == "P":
        check("%s: palette is preserved" % mode, clean_im.getpalette() == src_im.getpalette())
    check("%s: the workflow/prompt text chunk is gone" % mode, b"Secret" not in out)

print()
if FAILED:
    print("FAILED: %d check(s): %s" % (len(FAILED), ", ".join(FAILED)))
    sys.exit(1)
print("All strip_metadata pixel-identity checks passed.")
