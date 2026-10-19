"""Synthetic fixtures for tests/test_pixelart_converter.py, drawn in code so the repo ships
no model output and every check can name exact pixels. Deterministic: same files every run.

    python3 tests/fixtures/pixel/make_fixtures.py <out_dir>

face.png    240x320 RGBA, scale 10 source px per output px at 24x32. A flat skin ellipse on a
            transparent background (RGB green where alpha is 0) with a soft alpha ramp at its edge.
            Two dark eyes and a dark mouth, each smaller than half an output cell and placed across
            a cell boundary, so neither point sampling nor the per-cell majority keeps them.
blocks.png  240x320 RGB, four flat quadrants aligned to the 24x32 grid, plus planted features
            exactly one output cell each: an off-colour dot, a bright highlight, a 3-cell group
            and a diagonal line of 3 cells (corner contact only).
neon.png    256x192 RGB, a dark street of brown/grey gradients and fixed noise, with one small
            pink and one small cyan sign (each about 0.4% of the picture).
edges.png   128x64 RGB, left: a flat square on a flat ground (hard edges); right: a smooth
            horizontal grey ramp.
"""
import os
import sys

import numpy as np
from PIL import Image

SKIN = (230, 180, 150)
BG_KEY = (0, 255, 0)
EYE = (20, 20, 30)
MOUTH = (120, 30, 40)
# face feature boxes in source pixels: (x0, x1, y0, y1), half-open
LEFT_EYE = (71, 79, 116, 124)
RIGHT_EYE = (161, 169, 116, 124)
MOUTH_BOX = (90, 150, 206, 214)

QUAD = [(60, 90, 160), (170, 140, 60), (70, 130, 80), (150, 70, 90)]  # TL, TR, BL, BR
DOT = (90, 75, 150)          # close in lightness to QUAD[0], different colour
HIGHLIGHT = (255, 255, 240)  # bright, in the dark BL quadrant
# output-cell coordinates (col, row) of the planted features at 24x32
DOT_CELL = (5, 6)
HIGHLIGHT_CELL = (6, 24)
GROUP3_CELLS = [(18, 6), (19, 6), (18, 7)]      # TR quadrant, 3 touching cells
DIAG3_CELLS = [(17, 22), (18, 23), (19, 24)]    # BR quadrant, corner contact only
GROUP_COL = (200, 120, 40)   # off-colour for the 3-cell groups, close in lightness to its quadrant

PINK = (255, 60, 200)
CYAN = (40, 230, 240)
PINK_BOX = (40, 56, 30, 42)    # x0, x1, y0, y1 in source px (16x12)
CYAN_BOX = (190, 206, 120, 132)

EDGE_BG = 95
EDGE_SHAPE = 160
SQUARE = (16, 48, 16, 48)      # x0, x1, y0, y1
RAMP_X = (64, 128)
RAMP_LO, RAMP_HI = 40, 220


def face():
    w, h = 240, 320
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float64)
    r = np.sqrt(((xx + 0.5 - 120) / 90) ** 2 + ((yy + 0.5 - 170) / 120) ** 2)
    alpha = np.clip((1.0 - r) / 0.18, 0.0, 1.0)
    a = np.round(alpha * 255).astype(np.uint8)
    rgb = np.empty((h, w, 3), np.uint8)
    rgb[:] = BG_KEY
    rgb[a > 0] = SKIN
    for (x0, x1, y0, y1), col in ((LEFT_EYE, EYE), (RIGHT_EYE, EYE), (MOUTH_BOX, MOUTH)):
        rgb[y0:y1, x0:x1] = col
    return Image.fromarray(np.dstack([rgb, a]), "RGBA")


def _cell(arr, col, row, colour, scale=10):
    arr[row * scale:(row + 1) * scale, col * scale:(col + 1) * scale] = colour


def blocks():
    w, h = 240, 320
    arr = np.empty((h, w, 3), np.uint8)
    arr[:h // 2, :w // 2] = QUAD[0]
    arr[:h // 2, w // 2:] = QUAD[1]
    arr[h // 2:, :w // 2] = QUAD[2]
    arr[h // 2:, w // 2:] = QUAD[3]
    _cell(arr, *DOT_CELL, DOT)
    _cell(arr, *HIGHLIGHT_CELL, HIGHLIGHT)
    for c in GROUP3_CELLS + DIAG3_CELLS:
        _cell(arr, *c, GROUP_COL)
    return Image.fromarray(arr, "RGB")


def neon():
    w, h = 256, 192
    rng = np.random.default_rng(20261018)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float64)
    base = np.empty((h, w, 3))
    # brown-to-grey street: lightness falls with height, warmth varies across
    t = yy / (h - 1)
    u = xx / (w - 1)
    base[..., 0] = 30 + 70 * t + 25 * u
    base[..., 1] = 25 + 55 * t + 10 * u
    base[..., 2] = 22 + 40 * t + 5 * (1 - u)
    base += rng.normal(0, 9, size=(h, w, 1)) + rng.normal(0, 4, size=(h, w, 3))
    arr = np.clip(np.round(base), 0, 255).astype(np.uint8)
    for (x0, x1, y0, y1), col in ((PINK_BOX, PINK), (CYAN_BOX, CYAN)):
        arr[y0:y1, x0:x1] = col
    return Image.fromarray(arr, "RGB")


def edges():
    w, h = 128, 64
    arr = np.full((h, w), EDGE_BG, np.float64)
    x0, x1, y0, y1 = SQUARE
    arr[y0:y1, x0:x1] = EDGE_SHAPE
    r0, r1 = RAMP_X
    arr[:, r0:r1] = RAMP_LO + (np.arange(r1 - r0) / (r1 - r0 - 1)) * (RAMP_HI - RAMP_LO)
    g = np.round(arr).astype(np.uint8)
    return Image.fromarray(np.dstack([g, g, g]), "RGB")


def make_all(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    paths = {}
    for name, fn in (("face", face), ("blocks", blocks), ("neon", neon), ("edges", edges)):
        p = os.path.join(out_dir, name + ".png")
        fn().save(p)
        paths[name] = p
    return paths


if __name__ == "__main__":
    for k, v in make_all(sys.argv[1] if len(sys.argv) > 1 else ".").items():
        print(k, v)
