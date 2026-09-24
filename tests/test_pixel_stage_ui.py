"""Browser gate for P3a: a job whose mode has a `post` step shows the post
step's output (the sprite) as its result -- stage, history thumb, Share --
scaled up with square pixels (image-rendering: pixelated, a whole-number
scale), and a "Before the pixel step" toggle shows the lane's render from
before it. A plain picture (no post step) never gets the pixel treatment.

The job is seeded into jobs.json WITHOUT the "post" mark, the shape jobs
had before it existed, so load_jobs()'s backfill is exercised too. One fake
lane (a stub HTTP server) answers /system_stats and serves the render on
/view; nothing renders.

SHOT_DIR=<dir> also saves screenshots (3440x1440, 1280x800, the toggle).
SHOT_RAW / SHOT_SPRITE=<png> use real images for them instead of the
synthetic ones.

Run: python3 tests/test_pixel_stage_ui.py
"""
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.dont_write_bytecode = True
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAILED = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail else ""))
    if not cond:
        FAILED.append(name)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


try:
    from playwright.sync_api import sync_playwright
    from PIL import Image
except ImportError as e:
    print("SKIP: %s. pip install -r requirements-dev.txt -r requirements.txt "
          "&& python3 -m playwright install --with-deps chromium" % e)
    sys.exit(0)
try:
    with sync_playwright() as _pw_probe:
        _pw_probe.chromium.launch().close()
except Exception as _pw_err:
    print("SKIP: playwright's chromium browser is not installed (%s)." % _pw_err)
    sys.exit(0)


def png_bytes(path, size, pattern):
    if path:
        with open(path, "rb") as f:
            return f.read()
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    for x in range(size):
        for y in range(size):
            if pattern(x, y):
                im.putpixel((x, y), (60, 160, 90, 255))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


RAW = png_bytes(os.environ.get("SHOT_RAW"), 256, lambda x, y: (x - 128) ** 2 + (y - 128) ** 2 < 90 ** 2)
SPRITE = png_bytes(os.environ.get("SHOT_SPRITE"), 64, lambda x, y: (x // 8 + y // 8) % 2 == 0)


class FakeLane(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/view"):
            body, ctype = RAW, "image/png"
        else:
            body, ctype = b"{}", "application/json"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


lane_srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeLane)
threading.Thread(target=lane_srv.serve_forever, daemon=True).start()

tmp = tempfile.mkdtemp(prefix="bwf-pixel-stage-")
data_dir = os.path.join(tmp, "data")
os.makedirs(os.path.join(data_dir, "outputs", "pxjob"))
with open(os.path.join(data_dir, "outputs", "pxjob", "sprite_64x64.png"), "wb") as f:
    f.write(SPRITE)
now = time.time()
with open(os.path.join(data_dir, "jobs.json"), "w") as f:
    json.dump([
        {"id": "picjob", "lane": "fake", "lane_name": "Fake lane", "kind": "image", "mode": "t2i",
         "status": "done", "prompt": "a plain picture", "created": now - 60, "started": now - 60,
         "finished": now - 60, "elapsed": 1.0,
         "outputs": [{"filename": "PIC_00001_.png", "subfolder": "blackwire", "type": "output", "media": "image"}]},
        {"id": "pxjob", "lane": "fake", "lane_name": "Fake lane", "kind": "image", "mode": "pixelart",
         "status": "done", "prompt": "Pixel art sprite, retro video game style, a frog", "created": now,
         "started": now, "finished": now, "elapsed": 1.0,
         "outputs": [{"filename": "PIXELART_00001_.png", "subfolder": "blackwire", "type": "output", "media": "image"},
                     {"filename": "sprite_64x64.png", "subfolder": "pxjob", "type": "local", "media": "image"}]},
    ], f)
cfg_path = os.path.join(tmp, "config.json")
port = free_port()
with open(cfg_path, "w") as f:
    json.dump({"title": "Pixel Stage Test", "port": port, "bind": "127.0.0.1",
               "lanes": [{"id": "fake", "name": "Fake lane", "host": "127.0.0.1",
                          "port": lane_srv.server_address[1], "caps": ["image"]}],
               "timing": {"poll_seconds": 0.5, "job_poll_seconds": 1.0, "http_timeout": 1.0,
                          "discover_seconds": 300.0}}, f)
URL = "http://127.0.0.1:%d/" % port
logf = open(os.path.join(tmp, "server.log"), "w")
server = subprocess.Popen([sys.executable, os.path.join(REPO, "server.py")], cwd=REPO,
                          env=dict(os.environ, GENCENTER_CONFIG=cfg_path, GENCENTER_DATA=data_dir),
                          stdout=logf, stderr=subprocess.STDOUT)
deadline = time.time() + 20
while time.time() < deadline:
    try:
        urllib.request.urlopen(URL + "api/health", timeout=1)
        break
    except Exception:
        time.sleep(0.2)

SHOTS = os.environ.get("SHOT_DIR")
STAGE_IMG = "#monitorStage img"


def stage(page):
    return page.eval_on_selector(STAGE_IMG, """el => ({src: el.getAttribute('src'), px: el.classList.contains('px'),
        rendering: getComputedStyle(el).imageRendering, nw: el.naturalWidth, w: el.getBoundingClientRect().width,
        h: el.getBoundingClientRect().height})""")


try:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for vw, vh in ((1280, 800), (3440, 1440)):
            print("viewport %dx%d" % (vw, vh))
            page = browser.new_page(viewport={"width": vw, "height": vh})
            page.goto(URL + "#room=pixelart", wait_until="networkidle", timeout=30000)
            page.wait_for_selector('#binBody tr[data-job="pxjob"]', timeout=10000)
            page.click('#binBody tr[data-job="pxjob"]')
            page.wait_for_function("() => { const i = document.querySelector('#monitorStage img.px');"
                                   " return i && i.complete && i.naturalWidth > 0 && i.style.width; }", timeout=10000)
            s = stage(page)
            check("stage shows the post step's output (type=local)", "type=local" in s["src"], s)
            check("stage image is marked px and renders pixelated", s["px"] and s["rendering"] == "pixelated", s)
            k = s["w"] / s["nw"]
            check("stage scales the 64px sprite up by a whole number > 1",
                  s["nw"] == 64 and k > 1 and abs(k - round(k)) < 1e-6 and s["w"] == s["h"], s)
            thumb = page.eval_on_selector('#binBody tr[data-job="pxjob"] .bin-thumb img',
                                          "el => [el.getAttribute('src'), getComputedStyle(el).imageRendering]")
            check("history thumb is the sprite, pixelated", "type=local" in thumb[0] and thumb[1] == "pixelated", thumb)
            if SHOTS:
                page.screenshot(path=os.path.join(SHOTS, "pixel-stage-%dx%d.png" % (vw, vh)))
            btn = page.eval_on_selector("#beforeBtn", "el => [el.textContent, el.getAttribute('aria-pressed')]")
            check("the toggle reads 'Before the pixel step', not pressed", btn == ["Before the pixel step", "false"], btn)
            page.click("#beforeBtn")
            page.wait_for_function("() => { const i = document.querySelector('#monitorStage img');"
                                   " return i && i.getAttribute('src').includes('type=output'); }", timeout=5000)
            s = stage(page)
            check("pressed: the stage shows the lane's render, without px", "type=output" in s["src"] and not s["px"], s)
            check("pressed: aria-pressed is true",
                  page.eval_on_selector("#beforeBtn", "el => el.getAttribute('aria-pressed')") == "true")
            if SHOTS and vw == 1280:
                page.wait_for_timeout(500)
                page.screenshot(path=os.path.join(SHOTS, "pixel-stage-before-%dx%d.png" % (vw, vh)))
            page.click("#beforeBtn")
            page.wait_for_selector("#monitorStage img.px", timeout=5000)
            check("pressed again: back to the sprite", "type=local" in stage(page)["src"])
            page.click("#shareBtn")
            page.wait_for_selector("#shareKeepLink, #shareDownloadLink", timeout=5000)
            href = page.eval_on_selector("#shareKeepLink, #shareDownloadLink", "el => el.getAttribute('href')")
            check("Share downloads the sprite", "type=local" in href and "sprite_64x64.png" in href, href)
            page.keyboard.press("Escape")
            page.goto(URL + "#room=picture", wait_until="networkidle", timeout=30000)
            page.wait_for_selector('#binBody tr[data-job="picjob"]', timeout=10000)
            page.click('#binBody tr[data-job="picjob"]')
            page.wait_for_selector(STAGE_IMG, timeout=10000)
            s = stage(page)
            check("a plain picture (no post step) is not px and scales smoothly",
                  not s["px"] and s["rendering"] != "pixelated", s)
            check("a plain picture has no before toggle", page.query_selector("#beforeBtn") is None)
            page.close()
        browser.close()
finally:
    server.terminate()
    try:
        server.wait(timeout=5)
    except subprocess.TimeoutExpired:
        server.kill()
    logf.close()
    lane_srv.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)

print()
print("FAILED: %d" % len(FAILED))
for n in FAILED:
    print("  - " + n)
sys.exit(1 if FAILED else 0)