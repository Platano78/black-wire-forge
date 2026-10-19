#!/usr/bin/env python3
"""Vendored VERBATIM from
the original script this was vendored from (internal, not included in this repo) (md5
c8331e7525ab0df72217c532220c9a60 at copy time) -- every function body below was
byte-identical to that source at copy time (the changes since are listed below); only this paragraph, the leading "_" (which
keeps engines._discover() from mistaking this for a pack, see
engines/__init__.py) and the numpy/PIL import guard just below (portability
fix P1 -- neither is stdlib, and pixelart.py imports this module at server
startup, so an unguarded ImportError here used to take the whole app down
before it could bind) were added. tests/test_pixelart.py gates this with
a byte-comparison of quantise_and_dither's own output against the original
script on a fixed input, so any future drift between the two copies is caught,
not assumed away.

Changed since the copy (PA-1): target_size may be (width, height); the render is
centre-cropped to that aspect first; _modal_downscale takes any integer target and an
optional opaque mask. For a square sprite from a square render the nearest path is
still byte-identical to the original (same gate).

Converter upgrade (2026-10-18, written clean-room from a behaviour description):
opt-in options, every default unchanged (tests/test_pixelart_converter.py compares the old
calls against a frozen copy): alpha_mode="hard" (each pixel fully opaque or clear) and
clean_specks / speck_max / speck_keep (stray-pixel cleanup), both applied after the downscale.
Also opt-in: downscale="detail" (keeps small features that stand out), palette_mode="balanced" (small saturated areas get palette entries), dither_gate=<spread> (no dither on hard edges; dither only between each pixel's two nearest colours).

Deterministic 8-colour quantise -> ordered (Bayer) dither -> 64x64 downscale.

Route ruled by an internal decision: quantise/dither is a deterministic transform, not a
model call, so it never drifts. Palette is locked via median-cut over the raw
SDXL output (excluding background) then every pixel maps to its nearest
palette entry, optionally with a 4x4 Bayer ordered dither before the final
nearest-neighbour downscale to the target sprite size.

ponytail: dither strength is a knob (0 = off) because the mined Slynyrd
guidance says NOT to dither hard metal vehicle hulls (see report) — the ruled
route keeps the step, this just lets a caller turn it down without deleting it.
"""
import pathlib
import re
import sys

try:
    import numpy as np
    from PIL import Image
    DEPS_OK, DEPS_ERROR = True, None
except ImportError as e:
    np, Image = None, None
    DEPS_OK, DEPS_ERROR = False, str(e)

if DEPS_OK:
    # portability fix: Image.Dither / Image.Quantize are new in Pillow 9.1 (the
    # pre-9.1 constants live straight on Image, e.g. Image.NONE / Image.MEDIANCUT).
    # Resolved once here so quantize() below works unchanged on Pillow 8.x-12.x.
    DITHER_NONE = Image.Dither.NONE if hasattr(Image, "Dither") else Image.NONE
    QUANTIZE_MEDIANCUT = Image.Quantize.MEDIANCUT if hasattr(Image, "Quantize") else Image.MEDIANCUT
else:
    DITHER_NONE = None
    QUANTIZE_MEDIANCUT = None

if DEPS_OK:
    BAYER4 = np.array([
        [0, 8, 2, 10],
        [12, 4, 14, 6],
        [3, 11, 1, 9],
        [15, 7, 13, 5],
    ], dtype=np.float32) / 16.0 - 0.5  # centered around 0
else:
    BAYER4 = None


def load_locked_palette(path):
    """Read an AUTHORED palette: one #rrggbb per line, blanks and # comments ignored.

    ⚖ This is what makes "off-palette %" a real measurement. A palette derived by
    median cut FROM the image being measured maps every pixel to its own nearest
    entry, so off-palette is 0.00% BY CONSTRUCTION for any input whatsoever — the
    gate cannot fail and proves nothing. That decision's cloud reference scored 0.0%
    against a LOCKED input palette, which is a different and much stronger claim.
    """
    cols = []
    for n, raw in enumerate(pathlib.Path(path).read_text().splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        # a colour is "#rrggbb" or "rrggbb", optionally followed by a "# note" / "; note";
        # any other line that starts with "#" or ";" is a comment
        m = re.match(r"^#?([0-9a-fA-F]{6})(?:\s+[#;].*)?$", line)
        if m:
            h = m.group(1)
            cols.append([int(h[i:i + 2], 16) for i in (0, 2, 4)])
        elif line[0] in "#;":
            continue
        else:
            raise ValueError(f"{path}: line {n} is not a colour (expected #rrggbb): {raw.strip()[:40]!r}")
    if not cols:
        raise ValueError(f"no colours parsed from {path}")
    return np.array(cols, dtype=np.float32)


def build_palette(rgb, n_colors, bg_mask=None, palette_mode="population"):
    """Median-cut palette via PIL's own quantizer (deterministic given fixed
    input pixels — no randomness in PIL's MEDIANCUT path).

    🔴 The background is EXCLUDED by rebuilding the image from subject pixels only,
    never by painting a sentinel colour over it. A sentinel is still a colour: the
    quantizer gives it a palette slot (so an 8-colour palette really holds 7 usable
    entries) and nearest-neighbour mapping then lands real subject pixels ON it.
    That shipped 2 pixels of pure (255,0,255) — the conventional transparency key —
    into a sprite that had already passed every gate.
    """
    if bg_mask is not None:
        subject = rgb[~bg_mask]
        if subject.size == 0:
            raise ValueError("background mask covers every pixel — check its polarity")
        if palette_mode == "balanced":
            return _balanced_palette(subject, n_colors)
        # A 1-D strip of just the subject pixels: same colours, same median-cut
        # result, zero background influence and no sentinel.
        if palette_mode == "unique":
            # Median cut allocates slots by PIXEL POPULATION, so a large flat hull
            # takes every slot and a small bright focal feature (a canopy, a thruster
            # glow) gets none — the sprite collapses to one dark hue and stops reading
            # at 64px. Quantising the DISTINCT COLOURS instead drops population weight
            # entirely, so a rare highlight competes on equal terms. Still deterministic.
            subject = np.unique(subject.reshape(-1, 3), axis=0)
        strip = subject.reshape(1, -1, 3).astype(np.uint8)
        im = Image.fromarray(strip)
    else:
        if palette_mode == "balanced":
            return _balanced_palette(rgb, n_colors)
        im = Image.fromarray(rgb)
    q = im.quantize(colors=n_colors, method=QUANTIZE_MEDIANCUT, dither=DITHER_NONE)
    pal = q.getpalette()[: n_colors * 3]
    return np.array(pal, dtype=np.float32).reshape(-1, 3)


BALANCE_PER_BIN = 256  # most samples one (lightness, hue) bin may contribute to the k-means
BALANCE_ITERS = 30     # Lloyd iterations at most; stops early when assignments settle


def _balanced_palette(pixels, n_colors):
    """B2 (palette_mode="balanced"). Median cut hands out slots by pixel population, so a
    dark street spends every slot on browns and a small neon sign gets none. Here each
    distinct colour is put in an OKLab bin (8 lightness steps x 12 hues, plus a grey bin),
    each bin contributes at most BALANCE_PER_BIN samples, and k-means in OKLab picks the
    centres. Each centre then snaps to a real subject colour, so every palette entry
    occurs in the picture. One rng(0) per call: same input, same palette."""
    px = np.asarray(pixels, dtype=np.uint8).reshape(-1, 3)
    cols, inv = np.unique(px, axis=0, return_inverse=True)
    inv = inv.reshape(-1)
    if len(cols) <= n_colors:
        order = np.argsort([_pack(c) for c in cols], kind="stable")
        return cols[order].astype(np.float32)

    lab = _oklab(cols)
    lbin = np.minimum((lab[:, 0] * 8).astype(np.int64), 7)
    hue = ((np.arctan2(lab[:, 2], lab[:, 1]) + np.pi) / (2 * np.pi) * 12).astype(np.int64) % 12
    hbin = np.where(np.hypot(lab[:, 1], lab[:, 2]) < 0.03, 12, hue)
    binid = lbin * 13 + hbin

    rng = np.random.default_rng(0)
    pix_bin = binid[inv]
    order = np.argsort(pix_bin, kind="stable")
    _, starts, sizes = np.unique(pix_bin[order], return_index=True, return_counts=True)
    kept = []
    for s, n in zip(starts, sizes):  # ascending bin id
        members = order[s:s + n]
        if n > BALANCE_PER_BIN:
            members = rng.choice(members, BALANCE_PER_BIN, replace=False)
        kept.append(members)
    sel = np.concatenate(kept)
    ci = inv[sel]          # distinct-colour index of each kept sample
    X = lab[ci]            # OKLab of each kept sample; duplicates are the weighting
    k = min(n_colors, len(X))

    # k-means++ seeding
    C = np.empty((k, 3))
    C[0] = X[rng.integers(len(X))]
    d2 = ((X - C[0]) ** 2).sum(1)
    for j in range(1, k):
        tot = d2.sum()
        C[j] = X[rng.choice(len(X), p=(d2 / tot) if tot > 0 else None)]
        d2 = np.minimum(d2, ((X - C[j]) ** 2).sum(1))

    # Lloyd iterations; an empty cluster is re-seeded at the sample farthest from its centre
    assign = None
    for _ in range(BALANCE_ITERS):
        dist = ((X[:, None, :] - C[None, :, :]) ** 2).sum(-1)
        new = dist.argmin(1)
        if assign is not None and np.array_equal(new, assign):
            break
        assign = new
        for j in range(k):
            m = assign == j
            if m.any():
                C[j] = X[m].mean(0)
            else:
                C[j] = X[dist[np.arange(len(X)), assign].argmax()]
    assign = ((X[:, None, :] - C[None, :, :]) ** 2).sum(-1).argmin(1)

    # snap each centre to the nearest real colour among its own samples
    chosen = []
    for j in range(k):
        cand = np.unique(ci[assign == j])
        if cand.size == 0:
            continue
        pick = int(cand[np.argmin(((lab[cand] - C[j]) ** 2).sum(1))])
        if pick not in chosen:
            chosen.append(pick)
    return cols[chosen].astype(np.float32)


def _box_edges(w, h, tw, th):
    """Source edges of each output cell: xe[x]:xe[x+1] and ye[y]:ye[y+1] (the last
    entry is the source size). Shared by _modal_downscale and the alpha steps, so
    they always agree on which source pixels make up a cell."""
    xe = [min(max(i * w // tw, 0), w - 1) for i in range(tw)] + [w]
    ye = [min(max(i * h // th, 0), h - 1) for i in range(th)] + [h]
    return xe, ye


def _modal_downscale(arr, target, opaque=None):
    """Per-box most-common value. arr: (H,W,C) uint8; target: an int (square) or
    (width, height) of any size -- it need not divide the source.

    Box edges are floor(i * src / target), so the boxes tile the source with every
    source pixel in exactly one box. `opaque` (H,W bool, optional): when given, a
    box votes only among its opaque pixels -- transparent ones never outvote opaque
    ones -- and falls back to all its pixels only when the whole box is transparent.
    Ties go to the smallest value (np.unique's sorted order), as before.
    """
    tw, th = (target, target) if isinstance(target, int) else (int(target[0]), int(target[1]))
    h, w, c = arr.shape
    # one integer per pixel so np.unique runs on a 1-D array, same order as axis=0
    key = np.zeros((h, w), dtype=np.int64)
    for k in range(c):
        key = (key << 8) | arr[..., k].astype(np.int64)
    xe, ye = _box_edges(w, h, tw, th)
    out = np.empty((th, tw, c), dtype=arr.dtype)
    for y in range(th):
        y0, y1 = ye[y], max(ye[y + 1], ye[y] + 1)
        for x in range(tw):
            x0, x1 = xe[x], max(xe[x + 1], xe[x] + 1)
            box = key[y0:y1, x0:x1]
            if opaque is not None:
                ob = opaque[y0:y1, x0:x1]
                if ob.any():
                    box = box[ob]
            vals, counts = np.unique(box, return_counts=True)
            v = int(vals[int(np.argmax(counts))])
            for k in range(c - 1, -1, -1):
                out[y, x, k] = v & 255
                v >>= 8
    return out


def nearest_palette_index(rgb, palette):
    # rgb: (H,W,3) float32, palette: (K,3) float32
    diff = rgb[:, :, None, :] - palette[None, None, :, :]
    dist = (diff ** 2).sum(axis=-1)
    return np.argmin(dist, axis=-1)


def _nearest_oklab_index(rgb, palette):
    """Nearest palette entry in OKLab (used by palette_mode="balanced"). Each distinct
    colour is looked up once, in chunks, so a large render never builds an N x K x 3 array."""
    h, w, _ = rgb.shape
    uniq, inv = np.unique(rgb.reshape(-1, 3), axis=0, return_inverse=True)
    inv = inv.reshape(-1)
    pl, ul = _oklab(palette), _oklab(uniq)
    best = np.empty(len(uniq), dtype=np.int64)
    for s in range(0, len(uniq), 4096):
        d = ((ul[s:s + 4096, None, :] - pl[None, :, :]) ** 2).sum(-1)
        best[s:s + 4096] = d.argmin(-1)
    return best[inv].reshape(h, w)


def _oklab(rgb):
    """sRGB 0..255, shape (...,3) -> OKLab (L, a, b), shape (...,3), float64.
    Björn Ottosson's published conversion; L runs 0..1."""
    c = np.asarray(rgb, dtype=np.float64) / 255.0
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = lin[..., 0], lin[..., 1], lin[..., 2]
    l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l_, m_, s_ = np.cbrt(l), np.cbrt(m), np.cbrt(s)
    L = 0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_
    A = 1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_
    B = 0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_
    return np.stack([L, A, B], axis=-1)


def _pack(rgb):
    return (int(rgb[0]) << 16) | (int(rgb[1]) << 8) | int(rgb[2])


def _unpack(v):
    return ((v >> 16) & 255, (v >> 8) & 255, v & 255)


def _hard_alpha(alpha, quant, small_rgb, tw, th, nearest):
    """B5: alpha becomes 0 or 255 only. A cell is opaque when at least half of its
    source box is opaque (mean alpha/255 >= 0.5). An opaque cell keeps its colour, so
    the background colour cannot bleed into an edge: for "nearest", the sampled pixel's
    colour is kept when that pixel is at least half opaque, otherwise the palette colour
    with the most alpha in the box wins. "modal" and "detail" vote among covered pixels only (alpha >= 128) in this mode.
    alpha: full-res uint8 (H,W); quant: full-res palette colours (H,W,3) uint8."""
    h, w = alpha.shape
    xe, ye = _box_edges(w, h, tw, th)
    if nearest:
        # Which source pixel PIL's NEAREST picked for each cell: resize an index image
        # with the same call, so the choice cannot drift from what PIL did to the RGB.
        picked = np.array(Image.fromarray(np.arange(h * w, dtype=np.int32).reshape(h, w))
                          .resize((tw, th), Image.NEAREST))
    out_rgb = small_rgb.copy()
    out_a = np.zeros((th, tw), dtype=np.uint8)
    for y in range(th):
        y0, y1 = ye[y], max(ye[y + 1], ye[y] + 1)
        for x in range(tw):
            x0, x1 = xe[x], max(xe[x + 1], xe[x] + 1)
            box_a = alpha[y0:y1, x0:x1]
            if (box_a.astype(np.float64) / 255.0).mean() < 0.5:
                continue
            out_a[y, x] = 255
            if nearest:
                sy, sx = divmod(int(picked[y, x]), w)
                if alpha[sy, sx] >= 128:
                    continue
                keys = np.array([_pack(p) for p in quant[y0:y1, x0:x1].reshape(-1, 3)], dtype=np.int64)
                vals, inv = np.unique(keys, return_inverse=True)
                sums = np.bincount(inv.ravel(), weights=box_a.ravel().astype(np.float64),
                                   minlength=len(vals))
                # most alpha wins; ties go to the smallest packed value
                best = int(vals[np.lexsort((vals, -sums))[0]])
                out_rgb[y, x] = _unpack(best)
    return out_rgb, out_a


def _clean_specks(small, small_alpha, speck_max, speck_keep):
    """B4: a group of same-colour pixels that touch (8-connected) and number at most
    speck_max is replaced by its most common neighbour colour, unless the two differ in
    OKLab lightness by more than speck_keep (then it is a highlight and stays). Only
    pixels with alpha >= 128 take part. One pass: every decision reads the image as it
    was before the pass."""
    th, tw, _ = small.shape
    part = np.ones((th, tw), dtype=bool) if small_alpha is None else (small_alpha >= 128)
    key = np.array([[_pack(small[y, x]) for x in range(tw)] for y in range(th)], dtype=np.int64)
    seen = np.zeros((th, tw), dtype=bool)
    changes = []
    for y in range(th):
        for x in range(tw):
            if not part[y, x] or seen[y, x]:
                continue
            k = key[y, x]
            seen[y, x] = True
            stack, grp = [(y, x)], []
            while stack:
                cy, cx = stack.pop()
                grp.append((cy, cx))
                for ny in range(max(cy - 1, 0), min(cy + 2, th)):
                    for nx in range(max(cx - 1, 0), min(cx + 2, tw)):
                        if part[ny, nx] and not seen[ny, nx] and key[ny, nx] == k:
                            seen[ny, nx] = True
                            stack.append((ny, nx))
            if len(grp) > speck_max:
                continue
            members = set(grp)
            nbrs = set()
            for cy, cx in grp:
                for ny in range(max(cy - 1, 0), min(cy + 2, th)):
                    for nx in range(max(cx - 1, 0), min(cx + 2, tw)):
                        if (ny, nx) not in members and part[ny, nx]:
                            nbrs.add((ny, nx))
            if not nbrs:
                continue
            counts = {}
            for ny, nx in nbrs:
                v = int(key[ny, nx])
                counts[v] = counts.get(v, 0) + 1
            rep = min(counts, key=lambda v: (-counts[v], v))
            lg = _oklab(np.array(_unpack(int(k)), dtype=np.float64))[0]
            lr = _oklab(np.array(_unpack(rep), dtype=np.float64))[0]
            if abs(lg - lr) > speck_keep:
                continue
            changes.append((grp, rep))
    out = small.copy()
    for grp, rep in changes:
        for cy, cx in grp:
            out[cy, cx] = _unpack(rep)
    return out


# downscale="detail" (B1): a cell's colour changes only when a real share of its samples
# stands out from the surround. Constants are the first tuning of the rule; the fixtures
# in tests/fixtures/pixel/ are what they were checked against.
DETAIL_SHARE = 0.25   # the standing-out samples must be at least this share of the cell
DETAIL_MARGIN = 0.05  # ...sit this much further from the surround than the rest do
DETAIL_GAP = 0.10     # ...and differ this much in OKLab lightness from the rest


def _detail_downscale(idx, palette, alpha, small_rgb, small_alpha, tw, th, min_alpha=8):
    """B1: start from the modal result. For each output cell, sample a 4x4 grid of
    full-res palette indices and split them by lightness into A (the side of the cell's
    mean that the modal colour sits on) and B (the other side). B replaces the cell's
    colour with its most common palette colour only when it is a real share of the cell,
    stands out from the surrounding modal colours more than A does, and differs from A
    in lightness. Every decision reads the modal result, so the order cannot matter.
    Output holds palette colours only; plain areas are unchanged."""
    h, w = idx.shape
    xe, ye = _box_edges(w, h, tw, th)
    S = 4
    Lpal = _oklab(palette)[:, 0]
    Lmod = _oklab(small_rgb)[..., 0]
    out = small_rgb.copy()
    for y in range(th):
        y0, y1 = ye[y], max(ye[y + 1], ye[y] + 1)
        for x in range(tw):
            x0, x1 = xe[x], max(xe[x + 1], xe[x] + 1)
            ks = []
            for j in range(S):
                sy = y0 + int((j + 0.5) * (y1 - y0) / S)
                for i in range(S):
                    sx = x0 + int((i + 0.5) * (x1 - x0) / S)
                    if alpha is not None and alpha[sy, sx] < min_alpha:
                        continue  # background is never a sample
                    ks.append(int(idx[sy, sx]))
            if not ks:
                continue
            Ls = Lpal[np.array(ks)]
            mean = float(Ls.mean())
            Lm = float(Lmod[y, x])
            # A: samples on the same side of the mean as the modal colour; B: the rest
            inA = (Ls > mean) == (Lm > mean)
            if not inA.any() or inA.all():
                continue  # no contrast to compare against
            nb = []
            for ny in range(max(y - 1, 0), min(y + 2, th)):
                for nx in range(max(x - 1, 0), min(x + 2, tw)):
                    if (ny, nx) == (y, x):
                        continue
                    if small_alpha is not None and small_alpha[ny, nx] < 128:
                        continue
                    nb.append(float(Lmod[ny, nx]))
            Lsur = float(np.mean(nb)) if nb else Lm
            mA = float(Ls[inA].mean())
            mB = float(Ls[~inA].mean())
            if not (len(ks) - inA.sum()) / len(ks) >= DETAIL_SHARE:
                continue
            if not abs(mB - Lsur) > abs(mA - Lsur) + DETAIL_MARGIN:
                continue
            if not abs(mB - mA) >= DETAIL_GAP:
                continue
            counts = {}
            for k in np.array(ks)[~inA]:
                v = _pack(palette[k].astype(np.uint8))
                counts[v] = counts.get(v, 0) + 1
            out[y, x] = _unpack(min(counts, key=lambda v: (-counts[v], v)))
    return out


DITHER_GATE_BLOCK = 1 << 21  # elements per row block of the gated dither's (rows, W, K) distances


def _gated_dither_index(rgb, palette, dither_strength, dither_gate, use_oklab):
    """B3 (dither_gate). Ordered dither only where the picture is smooth, and only between
    each pixel's two nearest palette colours. p1 is the nearest entry exactly as the plain
    mapping for this call picks it (same space, same distance, same first-index ties); p2 is
    the second nearest. t is how far the pixel sits from p1 toward p2. A pixel takes p2 when
    t * s > rank + 1/32, where rank is the 4x4 Bayer threshold (0..15/16) and
    s = dither_strength * fade. fade is 1 at zero local OKLab lightness spread (3x3, edges
    clamped), 0 at dither_gate and beyond; with dither_gate <= 0 it is 0 everywhere.
    Works in row blocks, so a large render never builds an H x W x K x 3 array."""
    h, w, _ = rgb.shape
    K = len(palette)
    if K == 1:
        return np.zeros((h, w), dtype=np.int64)

    # gate: spread of OKLab L over each 3x3 neighbourhood, from the source before any dither
    L = _oklab(rgb)[..., 0]
    # 3x3 max - min by shifting a padded copy (sliding_window_view would need numpy 1.20+)
    Lp = np.pad(L, 1, mode="edge")
    shifts = [Lp[dy:dy + h, dx:dx + w] for dy in range(3) for dx in range(3)]
    spread = np.maximum.reduce(shifts) - np.minimum.reduce(shifts)
    if dither_gate > 0:
        fade = np.clip(1.0 - spread / dither_gate, 0.0, 1.0)
    else:
        fade = np.zeros((h, w))
    s = dither_strength * fade
    thresh = np.tile(BAYER4, (h // 4 + 1, w // 4 + 1))[:h, :w].astype(np.float64) + 0.5 + 1.0 / 32

    # the mapping space the plain (undithered) mapping of this call uses
    P = _oklab(palette) if use_oklab else palette
    idx = np.empty((h, w), dtype=np.int64)
    rows = max(1, DITHER_GATE_BLOCK // (w * K))
    for y0 in range(0, h, rows):
        y1 = min(h, y0 + rows)
        X = _oklab(rgb[y0:y1]) if use_oklab else rgb[y0:y1]
        d = ((X[:, :, None, :] - P[None, None, :, :]) ** 2).sum(axis=-1)
        p1 = d.argmin(axis=-1)
        np.put_along_axis(d, p1[..., None], np.inf, axis=-1)
        p2 = d.argmin(axis=-1)
        A = P[p1].astype(np.float64)
        D = P[p2].astype(np.float64) - A
        den = (D ** 2).sum(axis=-1)
        num = ((X.astype(np.float64) - A) * D).sum(axis=-1)
        t = np.where(den > 0, np.clip(num / np.where(den > 0, den, 1.0), 0.0, 1.0), 0.0)
        idx[y0:y1] = np.where(t * s[y0:y1] > thresh[y0:y1], p2, p1)
    return idx


def quantise_and_dither(src_path, out_path, target_size=64, n_colors=8,
                         dither_strength=0.0, alpha_path=None, palette_mode="population",
                         downscale="nearest", palette_file=None,
                         alpha_mode="keep", clean_specks=False, speck_max=2, speck_keep=0.25,
                         dither_gate=None):
    # alpha_mode "hard" (B5) and clean_specks (B4) are opt-in; the defaults leave the output unchanged.
    if alpha_mode not in ("keep", "hard"):
        raise ValueError('alpha_mode must be "keep" or "hard", not %r' % (alpha_mode,))
    # target_size: an int (square sprite) or (width, height)
    tw, th = (target_size, target_size) if isinstance(target_size, int) else (int(target_size[0]), int(target_size[1]))
    im = Image.open(src_path).convert("RGB")

    alpha = None
    bg_mask = None
    if alpha_path:
        src = Image.open(alpha_path)
        # 🔴 .convert("L") on an RGBA image DISCARDS the alpha channel and returns
        # LUMINANCE. The lane's own background-removal step emits RGBA, so the
        # obvious call silently substitutes brightness for opacity: the background
        # is never excluded from the palette, and the sprite's alpha becomes "how
        # bright was this pixel". Take the real channel when there is one.
        if src.mode in ("RGBA", "LA") or "transparency" in src.info:
            a = np.array(src.convert("RGBA"))[..., 3]
        else:
            a = np.array(src.convert("L"))
        bg_mask = a < 8
        alpha = a

    # Centre-crop the render to the sprite's aspect BEFORE anything else, so the
    # palette, the dither and the downscale all see only the picture that is kept.
    # Same aspect (every square sprite from a square render) = no crop at all.
    W, H = im.size
    if W * th != H * tw:
        if W * th > H * tw:
            cw, ch = max(1, H * tw // th), H
        else:
            cw, ch = W, max(1, W * th // tw)
        x0, y0 = (W - cw) // 2, (H - ch) // 2
        im = im.crop((x0, y0, x0 + cw, y0 + ch))
        if alpha is not None:
            alpha = alpha[y0:y0 + ch, x0:x0 + cw]
            bg_mask = bg_mask[y0:y0 + ch, x0:x0 + cw]
    rgb = np.array(im, dtype=np.float32)

    if palette_file:
        palette = load_locked_palette(palette_file)
        n_colors = len(palette)
    else:
        palette = build_palette(np.array(im), n_colors, bg_mask, palette_mode)

    use_oklab = palette_mode == "balanced" and not palette_file
    if dither_strength > 0 and dither_gate is not None:
        # B3: replaces the perturb-then-map step below for this call
        idx = _gated_dither_index(rgb, palette, dither_strength, dither_gate, use_oklab)
    else:
        if dither_strength > 0:
            h, w, _ = rgb.shape
            tile = np.tile(BAYER4, (h // 4 + 1, w // 4 + 1))[:h, :w]
            # Ordered dither perturbs each channel by a fraction of the local
            # palette gap before nearest-neighbour mapping.
            step = 255.0 / max(1, n_colors - 1)
            rgb = rgb + tile[:, :, None] * step * dither_strength

        if use_oklab:
            idx = _nearest_oklab_index(rgb, palette)
        else:
            idx = nearest_palette_index(rgb, palette)
    quant = palette[idx].astype(np.uint8)
    out = Image.fromarray(quant, "RGB")

    if downscale in ("modal", "detail"):
        # detail (B1) starts from the modal result and only changes some cells' colour
        # 🔴 NEAREST on a 16x reduction is POINT SAMPLING, not pixel-art conversion:
        # inside a block that straddles hull and canopy it keeps whichever single
        # source pixel it lands on, so a small bright focal feature disappears at
        # random. Taking the block's MOST COMMON colour is deterministic, introduces
        # no colour that was not already in the palette, and cannot miss a feature
        # that dominates its own block.
        opaque = None if alpha is None else ~bg_mask
        # Hard alpha: an opaque cell's colour comes from covered pixels only (alpha >= 128),
        # so a wide half-clear background can never outvote the subject (review 2026-10-18).
        min_alpha = 128 if (alpha_mode == "hard" and alpha is not None) else 8
        vote = opaque if min_alpha == 8 else (alpha >= 128)
        small_rgb = _modal_downscale(quant, (tw, th), vote)
        small_alpha = None if alpha is None else _modal_downscale(alpha[..., None], (tw, th), opaque)[..., 0]
        if downscale == "detail":
            small_rgb = _detail_downscale(idx, palette, alpha, small_rgb, small_alpha, tw, th, min_alpha)
    else:
        small_rgb = np.array(out.resize((tw, th), Image.NEAREST))
        small_alpha = None if alpha is None else np.array(Image.fromarray(alpha).resize((tw, th), Image.NEAREST))

    if alpha_mode == "hard" and alpha is not None:
        small_rgb, small_alpha = _hard_alpha(alpha, quant, small_rgb, tw, th, nearest=downscale not in ("modal", "detail"))
    if clean_specks and speck_max >= 1:
        small_rgb = _clean_specks(small_rgb, small_alpha, speck_max, speck_keep)

    out = Image.fromarray(small_rgb)
    if small_alpha is not None:
        out.putalpha(Image.fromarray(small_alpha))
    out.save(out_path)
    used_colors = len(np.unique(idx))
    print(f"wrote {out_path} {out.size} palette={n_colors} used={used_colors} dither={dither_strength}")
    return out_path


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("src")
    p.add_argument("out")
    p.add_argument("--size", type=int, default=64)
    p.add_argument("--colors", type=int, default=8)
    p.add_argument("--dither", type=float, default=0.0, help="0=off, ~0.15=subtle")
    p.add_argument("--alpha", default=None, help="separate alpha/mask PNG, same res as src")
    p.add_argument("--palette-file", default=None,
                   help="AUTHORED palette, one #rrggbb per line. Makes off-palette%% a real gate "
                        "and reserves slots for small focal features median cut would never give one.")
    p.add_argument("--downscale", choices=("nearest", "modal", "detail"), default="nearest",
                   help="nearest = point sample (loses small features); modal = per-block most common colour; "
                        "detail = modal, but keeps small features that stand out")
    p.add_argument("--palette-mode", choices=("population", "unique", "balanced"), default="population",
                   help="population = median cut over pixels (large flat areas win slots); "
                        "unique = median cut over distinct colours (small highlights survive); "
                        "balanced = OKLab k-means with each hue/lightness bin capped (small saturated areas get slots)")
    p.add_argument("--alpha-mode", choices=("keep", "hard"), default="keep",
                   help="keep = soft alpha from the cutout; hard = every cell fully opaque or clear")
    p.add_argument("--clean-specks", action="store_true",
                   help="replace tiny same-colour groups with their neighbours' colour (highlights stay)")
    p.add_argument("--speck-max", type=int, default=2,
                   help="largest group (in pixels) that counts as a speck")
    p.add_argument("--speck-keep", type=float, default=0.25,
                   help="OKLab lightness gap above which a small group is kept as a highlight")
    p.add_argument("--dither-gate", type=float, default=None,
                   help="dither only where the picture is smooth: local lightness spread above this (OKLab L, try 0.1) gets no dither")
    a = p.parse_args()
    quantise_and_dither(a.src, a.out, a.size, a.colors, a.dither, a.alpha, a.palette_mode, a.downscale, a.palette_file,
                        alpha_mode=a.alpha_mode, clean_specks=a.clean_specks,
                        speck_max=a.speck_max, speck_keep=a.speck_keep, dither_gate=a.dither_gate)
