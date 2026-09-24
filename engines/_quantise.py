#!/usr/bin/env python3
"""Vendored VERBATIM from
the original script this was vendored from (internal, not included in this repo) (md5
c8331e7525ab0df72217c532220c9a60 at copy time) -- every function body below is
byte-identical to that source; only this paragraph, the leading "_" (which
keeps engines._discover() from mistaking this for a pack, see
engines/__init__.py) and the numpy/PIL import guard just below (portability
fix P1 -- neither is stdlib, and pixelart.py imports this module at server
startup, so an unguarded ImportError here used to take the whole app down
before it could bind) were added. tests/test_pixelart_post.py gates this with
a byte-comparison of quantise_and_dither's own output against the original
script on a fixed input, so any future drift between the two copies is caught,
not assumed away.

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
    for line in pathlib.Path(path).read_text().splitlines():
        line = line.split("#")[0].strip() if not line.strip().startswith("#") else ""
        if not line:
            continue
        h = line.lstrip("#").strip()
        cols.append([int(h[i:i + 2], 16) for i in (0, 2, 4)])
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
        im = Image.fromarray(rgb)
    q = im.quantize(colors=n_colors, method=QUANTIZE_MEDIANCUT, dither=DITHER_NONE)
    pal = q.getpalette()[: n_colors * 3]
    return np.array(pal, dtype=np.float32).reshape(-1, 3)


def _modal_downscale(arr, target):
    """Per-block most-common value. arr: (H,W,C) uint8, H==W divisible by target."""
    h, w = arr.shape[:2]
    n = h // target
    if n * target != h or w != h:
        raise ValueError(f"modal downscale needs a square canvas divisible by {target}; got {w}x{h}")
    c = arr.shape[2]
    blocks = arr.reshape(target, n, target, n, c).transpose(0, 2, 1, 3, 4).reshape(target, target, n * n, c)
    out = np.empty((target, target, c), dtype=arr.dtype)
    for y in range(target):
        for x in range(target):
            vals, counts = np.unique(blocks[y, x], axis=0, return_counts=True)
            out[y, x] = vals[int(np.argmax(counts))]
    return out


def nearest_palette_index(rgb, palette):
    # rgb: (H,W,3) float32, palette: (K,3) float32
    diff = rgb[:, :, None, :] - palette[None, None, :, :]
    dist = (diff ** 2).sum(axis=-1)
    return np.argmin(dist, axis=-1)


def quantise_and_dither(src_path, out_path, target_size=64, n_colors=8,
                         dither_strength=0.0, alpha_path=None, palette_mode="population",
                         downscale="nearest", palette_file=None):
    im = Image.open(src_path).convert("RGB")
    rgb = np.array(im, dtype=np.float32)

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

    if palette_file:
        palette = load_locked_palette(palette_file)
        n_colors = len(palette)
    else:
        palette = build_palette(np.array(im), n_colors, bg_mask, palette_mode)

    if dither_strength > 0:
        h, w, _ = rgb.shape
        tile = np.tile(BAYER4, (h // 4 + 1, w // 4 + 1))[:h, :w]
        # Ordered dither perturbs each channel by a fraction of the local
        # palette gap before nearest-neighbour mapping.
        step = 255.0 / max(1, n_colors - 1)
        rgb = rgb + tile[:, :, None] * step * dither_strength

    idx = nearest_palette_index(rgb, palette)
    quant = palette[idx].astype(np.uint8)
    out = Image.fromarray(quant, "RGB")

    if downscale == "modal":
        # 🔴 NEAREST on a 16x reduction is POINT SAMPLING, not pixel-art conversion:
        # inside a block that straddles hull and canopy it keeps whichever single
        # source pixel it lands on, so a small bright focal feature disappears at
        # random. Taking the block's MOST COMMON colour is deterministic, introduces
        # no colour that was not already in the palette, and cannot miss a feature
        # that dominates its own block.
        quant = _modal_downscale(quant, target_size)
        if alpha is not None:
            alpha = _modal_downscale(alpha[..., None], target_size)[..., 0]
        out = Image.fromarray(quant, "RGB")
        if alpha is not None:
            out.putalpha(Image.fromarray(alpha))
    elif alpha is not None:
        out = out.resize((target_size, target_size), Image.NEAREST)
        a_small = Image.fromarray(alpha).resize((target_size, target_size), Image.NEAREST)
        out.putalpha(a_small)
    else:
        out = out.resize((target_size, target_size), Image.NEAREST)

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
    p.add_argument("--downscale", choices=("nearest", "modal"), default="nearest",
                   help="nearest = point sample (loses small features); modal = per-block most common colour")
    p.add_argument("--palette-mode", choices=("population", "unique"), default="population",
                   help="population = median cut over pixels (large flat areas win slots); "
                        "unique = median cut over distinct colours (small highlights survive)")
    a = p.parse_args()
    quantise_and_dither(a.src, a.out, a.size, a.colors, a.dither, a.alpha, a.palette_mode, a.downscale, a.palette_file)
