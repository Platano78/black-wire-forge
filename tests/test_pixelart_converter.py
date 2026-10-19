"""Acceptance gate for the Pixel Art converter upgrade (2026-10-18):
five opt-in behaviours in engines/_quantise.py, and every old call unchanged.

Groups (run all, or name some):  python3 tests/test_pixelart_converter.py [defaults B5 B4 B1 B2 B3]
  defaults  every pre-upgrade argument combination gives pixel-identical output to the frozen
            reference copy (tests/fixtures/pixel/quantise_reference.py)
  B5        alpha_mode="hard": alpha is 0 or 255, by coverage, no background colour in edges
  B4        clean_specks=True: tiny same-colour groups take their neighbours' colour, highlights stay
  B1        downscale="detail": small dark features (eyes, a mouth line) survive the downscale
  B2        palette_mode="balanced": small saturated areas get palette entries
  B3        dither_gate=<spread>: no dither on hard edges, dither on smooth ramps

The fixtures are drawn in code (tests/fixtures/pixel/make_fixtures.py); see its docstring for
what each one holds and where.
"""
import importlib.util, inspect, io, os, sys, tempfile, contextlib
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIX = os.path.join(HERE, "fixtures", "pixel")
sys.path.insert(0, ROOT)
sys.path.insert(0, FIX)

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + detail) if not cond and detail else ""))
    if not cond: FAILED.append(name)

if not (importlib.util.find_spec("PIL") and importlib.util.find_spec("numpy")):
    print("  SKIP  this suite needs Pillow and numpy (requirements.txt), not installed here")
    sys.exit(0)

import numpy as np
from PIL import Image
import engines._quantise as q
import make_fixtures as mf

_spec = importlib.util.spec_from_file_location("quantise_reference", os.path.join(FIX, "quantise_reference.py"))
ref = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ref)

GROUPS = ["defaults", "B5", "B4", "B1", "B2", "B3"]
want = sys.argv[1:] or GROUPS
bad = [g for g in want if g not in GROUPS]
if bad:
    print("unknown group(s): %s (choose from %s)" % (", ".join(bad), " ".join(GROUPS)))
    sys.exit(2)

TD = tempfile.mkdtemp(prefix="bwf-pxconv-")
F = mf.make_all(TD)
_n = [0]


def run(mod, src, *args, **kw):
    """Run mod.quantise_and_dither into a fresh file; -> uint8 array (H,W,C) plus mode."""
    _n[0] += 1
    out = os.path.join(TD, "out%d.png" % _n[0])
    with contextlib.redirect_stdout(io.StringIO()):
        mod.quantise_and_dither(src, out, *args, **kw)
    im = Image.open(out)
    im.load()
    return np.array(im), im.mode


def luma(a):
    a = a.astype(np.float64)
    return 0.299 * a[..., 0] + 0.587 * a[..., 1] + 0.114 * a[..., 2]


def colours(a):
    return {tuple(int(v) for v in c) for c in np.unique(a[..., :3].reshape(-1, 3), axis=0)}


def palette_file(name, cols):
    p = os.path.join(TD, name)
    with open(p, "w") as f:
        f.write("".join("#%02x%02x%02x\n" % tuple(c) for c in cols))
    return p


def sig_default(name):
    p = inspect.signature(q.quantise_and_dither).parameters.get(name)
    return "<missing>" if p is None else p.default


def guarded(group, fn):
    if group not in want:
        return
    print()
    print(group + ": " + fn.__doc__.strip())
    try:
        fn()
    except Exception as e:  # a missing option on the old code is a FAIL, not a crash
        check(group + " ran without raising", False, "%s: %s" % (type(e).__name__, e))


# face fixture geometry at 24x32 (10 source px per output px)
FACE_SIZE = (24, 32)
EYE_CELLS = [(7, (11, 12)), (16, (11, 12))]          # (col, candidate rows)
MOUTH_COLS, MOUTH_ROWS = range(9, 15), (20, 21)
CHEEK = (16, 12)                                     # (row, col), plain skin


def face_coverage():
    a = np.array(Image.open(F["face"]))[..., 3].astype(np.float64) / 255.0
    return a.reshape(32, 10, 24, 10).mean(axis=(1, 3))


def g_defaults():
    """old calls give output pixel-identical to the frozen reference"""
    grey8 = palette_file("grey8.txt", [(v, v, v) for v in (0, 36, 73, 109, 146, 182, 219, 255)])
    cases = [
        ("face", (), {}),
        ("face", (), {"alpha_path": F["face"]}),
        ("face", (), {"target_size": FACE_SIZE, "alpha_path": F["face"], "downscale": "modal"}),
        ("face", (), {"target_size": FACE_SIZE, "alpha_path": F["face"]}),
        ("face", (), {"target_size": (24, 24), "alpha_path": F["face"], "dither_strength": 0.3}),
        ("face", (32, 6, 0.15, F["face"], "unique", "modal"), {}),           # positional, CLI order
        ("blocks", (), {"target_size": FACE_SIZE}),
        ("blocks", (), {"target_size": FACE_SIZE, "downscale": "modal", "dither_strength": 0.5}),
        ("neon", (), {"target_size": (64, 48), "n_colors": 16}),
        ("neon", (), {"target_size": (64, 48), "n_colors": 16, "palette_mode": "unique", "downscale": "modal"}),
        ("neon", (), {"target_size": 48, "n_colors": 32, "dither_strength": 0.2}),
        ("edges", (), {"target_size": (128, 64), "palette_file": grey8, "dither_strength": 1.0}),
        ("edges", (), {"target_size": (64, 32), "palette_file": grey8, "downscale": "modal"}),
    ]
    for name, args, kw in cases:
        new, nm = run(q, F[name], *args, **kw)
        old, om = run(ref, F[name], *args, **kw)
        label = "%s %s %s" % (name, list(args) if args else "", kw)
        check("unchanged: " + label.replace(TD + os.sep, ""),
              nm == om and new.shape == old.shape and np.array_equal(new, old),
              "mode %s vs %s, shape %s vs %s" % (nm, om, new.shape, old.shape))
    check("old defaults kept (downscale nearest, palette population)",
          sig_default("downscale") == "nearest" and sig_default("palette_mode") == "population")


def option_defaults(*pairs):
    for k, v in pairs:
        check("new option %s defaults to %r" % (k, v), sig_default(k) == v, "got %r" % (sig_default(k),))


def g_b5():
    """hard alpha"""
    option_defaults(("alpha_mode", "keep"))
    cov = face_coverage()
    soft, _ = run(q, F["face"], target_size=FACE_SIZE, alpha_path=F["face"])
    check("fixture is meaningful: the default output has soft alpha (>2 levels)",
          len(np.unique(soft[..., 3])) > 2)
    for ds in ("nearest", "modal"):
        a, mode = run(q, F["face"], target_size=FACE_SIZE, alpha_path=F["face"], downscale=ds, alpha_mode="hard")
        al = a[..., 3] if a.ndim == 3 and a.shape[2] == 4 else None
        check("%s: output keeps an alpha channel" % ds, al is not None, "mode %s" % mode)
        if al is None:
            continue
        check("%s: every alpha is 0 or 255" % ds, set(np.unique(al).tolist()) <= {0, 255},
              str(sorted(set(np.unique(al).tolist()))[:12]))
        wrong_on = int(((cov >= 0.6) & (al != 255)).sum())
        wrong_off = int(((cov <= 0.4) & (al != 0)).sum())
        check("%s: cells at least 60%% covered are opaque, at most 40%% transparent" % ds,
              wrong_on == 0 and wrong_off == 0, "%d should be opaque, %d should be clear" % (wrong_on, wrong_off))
        op = al == 255
        rgb = a[..., :3].astype(int)
        greenish = int((op & (rgb[..., 1] > rgb[..., 0] + 20)).sum())
        check("%s: no opaque pixel takes the background's colour" % ds, greenish == 0, "%d green-tinted" % greenish)
    # review 2026-10-18: a mostly half-clear background (alpha 120, not covered) must not outvote
    # the covered pixels for an opaque cell's colour, whatever the downscale
    rgb = np.zeros((4, 4, 3), np.uint8); rgb[:] = (0, 255, 0)
    al = np.full((4, 4), 120, np.uint8)
    for i in range(7):
        rgb[i // 4, i % 4] = (255, 0, 0); al[i // 4, i % 4] = 255
    bleed = os.path.join(TD, "bleed.png")
    Image.fromarray(np.dstack([rgb, al]), "RGBA").save(bleed)
    rg = palette_file("rg.txt", [(255, 0, 0), (0, 255, 0)])
    for ds in ("nearest", "modal", "detail"):
        b, _ = run(q, bleed, target_size=1, palette_file=rg, alpha_path=bleed, alpha_mode="hard", downscale=ds)
        check("%s: an opaque cell takes its colour from covered pixels only" % ds,
              b[0, 0].tolist() == [255, 0, 0, 255], str(b[0, 0].tolist()))
    plain, _ = run(q, F["blocks"], target_size=FACE_SIZE)
    hard, _ = run(q, F["blocks"], target_size=FACE_SIZE, alpha_mode="hard")
    check("no alpha given: hard alpha changes nothing", np.array_equal(plain, hard))


def g_b4():
    """speck cleanup"""
    option_defaults(("clean_specks", False), ("speck_max", 2), ("speck_keep", 0.25))
    base, _ = run(q, F["blocks"], target_size=FACE_SIZE)
    a, _ = run(q, F["blocks"], target_size=FACE_SIZE, clean_specks=True)
    px = lambda arr, cell: tuple(int(v) for v in arr[cell[1], cell[0], :3])
    check("fixture is meaningful: the dot is there without cleanup", px(base, mf.DOT_CELL) == mf.DOT)
    check("a 1-px off-colour dot takes its neighbours' colour", px(a, mf.DOT_CELL) == mf.QUAD[0],
          "got %s" % (px(a, mf.DOT_CELL),))
    check("a 1-px bright highlight survives", px(a, mf.HIGHLIGHT_CELL) == mf.HIGHLIGHT)
    check("a 3-pixel group survives (bigger than speck_max)",
          all(px(a, c) == mf.GROUP_COL for c in mf.GROUP3_CELLS))
    check("3 pixels touching only at corners count as one group and survive",
          all(px(a, c) == mf.GROUP_COL for c in mf.DIAG3_CELLS))
    diff = int((base != a).any(axis=-1).sum())
    check("nothing else changes", diff == 1, "%d pixels changed" % diff)
    big, _ = run(q, F["blocks"], target_size=FACE_SIZE, clean_specks=True, speck_max=3)
    check("speck_max=3 also takes the 3-pixel groups",
          all(px(big, c) != mf.GROUP_COL for c in mf.GROUP3_CELLS + mf.DIAG3_CELLS))
    keep_all, _ = run(q, F["blocks"], target_size=FACE_SIZE, clean_specks=True, speck_keep=1.0)
    check("speck_keep=1.0 (the whole lightness range) lets nothing count as a highlight",
          px(keep_all, mf.HIGHLIGHT_CELL) == mf.QUAD[2], "got %s" % (px(keep_all, mf.HIGHLIGHT_CELL),))
    for ds in ("nearest", "modal"):
        s, _ = run(q, F["face"], target_size=FACE_SIZE, alpha_path=F["face"], downscale=ds)
        c, _ = run(q, F["face"], target_size=FACE_SIZE, alpha_path=F["face"], downscale=ds, clean_specks=True)
        clear = s[..., 3] == 0
        check("%s: transparent pixels are never changed" % ds,
              np.array_equal(s[..., 3], c[..., 3]) and np.array_equal(s[clear], c[clear]))


def _features_kept(a, cheek_l):
    L = luma(a)
    eyes = [min(L[r, col] for r in rows) < cheek_l - 60 for col, rows in EYE_CELLS]
    mouth = [min(L[r, col] for r in MOUTH_ROWS) < cheek_l - 60 for col in MOUTH_COLS]
    return eyes, mouth


def g_b1():
    """detail-keeping downscale"""
    modal, _ = run(q, F["face"], target_size=FACE_SIZE, alpha_path=F["face"], downscale="modal")
    cheek_l = luma(modal)[CHEEK]
    e0, m0 = _features_kept(modal, cheek_l)
    check("fixture is meaningful: modal loses the eyes and the mouth", not any(e0) and not any(m0))
    a, _ = run(q, F["face"], target_size=FACE_SIZE, alpha_path=F["face"], downscale="detail")
    e, m = _features_kept(a, cheek_l)
    check("both eyes survive as pixels darker than the skin", all(e), str(e))
    check("the mouth survives across its whole width", all(m), str(m))
    cov = face_coverage()
    interior = cov >= 0.999
    for col, rows in EYE_CELLS:
        for r in rows:
            interior[r, col] = False
    for col in MOUTH_COLS:
        for r in MOUTH_ROWS:
            interior[r, col] = False
    same = np.array_equal(a[interior], modal[interior])
    check("plain skin away from the features matches modal", same,
          "%d cells differ" % int((a[interior] != modal[interior]).any(axis=-1).sum()))
    b_modal, _ = run(q, F["blocks"], target_size=FACE_SIZE, downscale="modal")
    b_detail, _ = run(q, F["blocks"], target_size=FACE_SIZE, downscale="detail")
    check("flat blocks come out identical to modal", np.array_equal(b_modal, b_detail))
    pal = [mf.SKIN, mf.EYE, mf.MOUTH, (255, 255, 255), (90, 60, 50)]
    locked, _ = run(q, F["face"], target_size=FACE_SIZE, alpha_path=F["face"], downscale="detail",
                    palette_file=palette_file("face5.txt", pal))
    opaque = locked[..., 3] > 0
    used = {tuple(int(v) for v in c) for c in locked[opaque][:, :3]}
    check("output holds palette colours only", used <= set(pal), str(sorted(used - set(pal))[:5]))
    again, _ = run(q, F["face"], target_size=FACE_SIZE, alpha_path=F["face"], downscale="detail")
    check("deterministic", np.array_equal(a, again))
    h, _ = run(q, F["face"], target_size=FACE_SIZE, alpha_path=F["face"], downscale="detail", alpha_mode="hard")
    check("detail + hard alpha: every alpha is 0 or 255", set(np.unique(h[..., 3]).tolist()) <= {0, 255})


def _has(cols, pred):
    return any(pred(*c) for c in cols)

PINKISH = lambda r, g, b: r > 150 and g < 110 and b > 110
CYANISH = lambda r, g, b: g > 150 and b > 150 and r < 120


def g_b2():
    """balanced palette"""
    for ds in ("nearest", "modal"):
        old, _ = run(q, F["neon"], target_size=(64, 48), n_colors=16, downscale=ds)
        check("%s: fixture is meaningful: population mode has no pink and no cyan" % ds,
              not _has(colours(old), PINKISH) and not _has(colours(old), CYANISH))
        a, _ = run(q, F["neon"], target_size=(64, 48), n_colors=16, downscale=ds, palette_mode="balanced")
        check("%s: balanced keeps a pink entry" % ds, _has(colours(a), PINKISH))
        check("%s: balanced keeps a cyan entry" % ds, _has(colours(a), CYANISH))
    a1, _ = run(q, F["neon"], target_size=(64, 48), n_colors=16, palette_mode="balanced")
    a2, _ = run(q, F["neon"], target_size=(64, 48), n_colors=16, palette_mode="balanced")
    check("deterministic", np.array_equal(a1, a2))
    src_cols = colours(np.array(Image.open(F["blocks"])))
    few, _ = run(q, F["blocks"], target_size=FACE_SIZE, n_colors=16, palette_mode="balanced")
    check("more colours asked than the picture has: only the picture's own colours",
          colours(few) <= src_cols, str(sorted(colours(few) - src_cols)[:5]))
    f, _ = run(q, F["face"], target_size=FACE_SIZE, alpha_path=F["face"], palette_mode="balanced")
    op = f[..., 3] > 0
    rgb = f[..., :3].astype(int)
    check("the background never gets a palette entry",
          int((op & (rgb[..., 1] > rgb[..., 0] + 20)).sum()) == 0)


def g_b3():
    """edge-gated dither"""
    option_defaults(("dither_gate", None))
    greys = [(v, v, v) for v in (0, 36, 73, 109, 146, 182, 219, 255)]
    pf = palette_file("grey8b.txt", greys)
    src = np.array(Image.open(F["edges"]))[..., 0].astype(int)
    kw = {"target_size": (128, 64), "palette_file": pf}
    flat, _ = run(q, F["edges"], **kw)
    plain, _ = run(q, F["edges"], dither_strength=1.0, **kw)
    gated, _ = run(q, F["edges"], dither_strength=1.0, dither_gate=0.1, **kw)
    h, w = src.shape
    band = np.zeros((h, w), bool)
    for y in range(1, h - 1):
        for x in range(1, 60):
            win = src[y - 1:y + 2, x - 1:x + 2]
            band[y, x] = (win == mf.EDGE_BG).any() and (win == mf.EDGE_SHAPE).any()
    check("fixture is meaningful: plain dither changes pixels on the hard edge",
          bool((plain[band] != flat[band]).any()))
    nb = int((gated[band] != flat[band]).any(axis=-1).sum())
    check("no dither within 1 px of the hard edge", nb == 0, "%d of %d band pixels dithered" % (nb, int(band.sum())))
    ramp = np.zeros((h, w), bool)
    ramp[1:h - 1, mf.RAMP_X[0] + 4:mf.RAMP_X[1] - 4] = True
    share = float((gated[ramp] != flat[ramp]).any(axis=-1).mean())
    check("the smooth ramp still gets dither (>=10% of its pixels)", share >= 0.10, "%.1f%%" % (share * 100))
    pv = np.array([g[0] for g in greys])
    two = np.argsort(np.abs(src[..., None] - pv[None, None, :]), axis=-1)[..., :2]
    ok = (pv[two[..., 0]] == gated[..., 0]) | (pv[two[..., 1]] == gated[..., 0])
    check("every ramp pixel is one of its two nearest palette colours", bool(ok[ramp].all()),
          "%d off" % int((~ok[ramp]).sum()))
    off, _ = run(q, F["edges"], dither_strength=0.0, dither_gate=0.1, **kw)
    check("dither strength 0: the gate changes nothing", np.array_equal(off, flat))
    g2, _ = run(q, F["edges"], dither_strength=1.0, dither_gate=0.1, **kw)
    check("deterministic", np.array_equal(gated, g2))


guarded("defaults", g_defaults)
guarded("B5", g_b5)
guarded("B4", g_b4)
guarded("B1", g_b1)
guarded("B2", g_b2)
guarded("B3", g_b3)

import shutil
shutil.rmtree(TD, ignore_errors=True)
print()
print(("FAILED: %d" % len(FAILED)) if FAILED else "ALL PASS")
sys.exit(1 if FAILED else 0)
