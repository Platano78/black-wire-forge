"""Browser gate for PA-1: the Pixel Art room shows Sprite width/height, Colours,
Palette (hex colours) and Pixel grid, and "Take colours from a picture..." fills the
palette field from a local PNG (read with a canvas, nothing uploaded), the most common
colours first, as many as the Colours value, fully transparent pixels ignored.

Run: python3 tests/test_pixelart_palette_ui.py
"""
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _ui_fixture as ui  # noqa: E402
from _ui_fixture import check  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402
from PIL import Image  # noqa: E402

URL = ui.start([])
# 100x100: five colours of known, distinct counts, plus a sixth under alpha 0 that must be ignored
KNOWN = [((26, 28, 44), 3000), ((93, 39, 93), 2500), ((177, 62, 83), 1800), ((255, 205, 117), 1200), ((56, 183, 100), 600)]
im = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
px = im.load()
cells = [(x, y) for y in range(100) for x in range(100)]
i = 0
for col, n in KNOWN:
    for _ in range(n):
        px[cells[i]] = col + (255,); i += 1
for j in range(i, 10000):
    px[cells[j]] = (255, 0, 255, 0)       # loud colour, fully transparent: ignored
PNG = os.path.join(ui.SCRATCH, "known.png")
im.save(PNG)
hexof = lambda c: "#%02x%02x%02x" % c

try:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 1100})
        page.goto(URL + "#room=pixelart", wait_until="networkidle")
        page.wait_for_function("() => STATE.lanes.some(l => l.id === 't' && l.up)", timeout=15000)
        page.reload(wait_until="networkidle")
        page.wait_for_selector("#inspector [data-field-id=pixel_size]", state="attached", timeout=15000)
        page.evaluate("() => document.querySelectorAll('#inspector details').forEach(d => d.open = true)")
        labels = page.evaluate("() => Array.from(document.querySelectorAll('#inspector label.field-label')).map(l => l.textContent.trim())")
        for want in ("Sprite width", "Sprite height", "Colours", "Palette (hex colours)", "Pixel grid"):
            check("the room shows the field: " + want, any(l.startswith(want) for l in labels), labels)
        check("Sprite height defaults to 0 (as tall as wide)",
              page.evaluate("() => document.querySelector('#inspector [data-field-id=pixel_height]').value") == "0")
        check("Pixel grid offers Sharp (nearest) and Cleanest (most common colour)",
              page.evaluate("() => Array.from(document.querySelector('#inspector [data-field-id=pixel_grid]').options).map(o => o.value)")
              == ["Sharp (nearest)", "Cleanest (most common colour)"])
        hints = page.inner_text("#inspector")
        check("the height hint says the picture is cropped from the middle", "cropped to this shape from the middle" in hints, hints[:300])
        check("the palette hint says Colours is ignored when it is filled in", "Colours setting is ignored" in hints)
        check("the helper button is there", page.query_selector("#inspector button[data-colours-from=pixel_palette]") is not None)
        check("the palette field starts empty", page.evaluate("() => document.getElementById('f_pixel_palette').value") == "")

        uploads = []
        page.on("request", lambda r: uploads.append(r.url) if "/api/upload" in r.url or r.method == "POST" else None)
        page.fill("#f_pixel_colors", "4")
        with page.expect_file_chooser(timeout=5000) as fc:
            page.click("#inspector button[data-colours-from=pixel_palette]")
        fc.value.set_files(PNG)     # the visible button opens the file picker
        page.wait_for_function("() => document.getElementById('f_pixel_palette').value !== ''", timeout=10000)
        got = page.evaluate("() => document.getElementById('f_pixel_palette').value")
        want = ", ".join(hexof(c) for c, _ in KNOWN[:4])
        check("the field holds the 4 most common colours, most common first", got == want, got)
        check("the transparent colour was ignored", "#ff00ff" not in got)
        check("nothing was uploaded", not uploads, uploads)
        check("it says what it took", "Took 4 of 5" in page.inner_text("#inspector"), page.inner_text("#inspector")[-200:])
        check("the form value carries it (currentValues)", page.evaluate("() => currentValues().pixel_palette") == want)
        page.fill("#f_pixel_colors", "9")
        page.set_input_files("#inspector input[data-colours-file=pixel_palette]", PNG)
        page.wait_for_function("() => document.getElementById('f_pixel_palette').value.split(',').length === 5", timeout=10000)
        check("asking for more colours than the picture has gives all of them",
              page.evaluate("() => document.getElementById('f_pixel_palette').value") == ", ".join(hexof(c) for c, _ in KNOWN))
        ui.shot(page, "pixel-palette")
        browser.close()
finally:
    ui.finish()
