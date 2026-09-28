"""LORA-2E #4: the Video room's "Browse styles" drawer shows BOTH its real
style families (LTX-2.5, MiniMax-H3), and the Talking Head / Music rooms
show their own real family too -- against server.py's REAL engines/*.py
packs (engines.style_catalogs()), never the BWF_TEST_STYLE_FAMILIES seam
tests/test_lora_drawer_families_ui.py uses. That seam substitutes a fixed
family list; THIS suite proves the same tab-building code against
model-discovery output real packs actually declare.

The only fixture here is the ComfyUI /object_info wire (a deliberately
minimal, self-contained lane -- never tests/golden/pools_rig.json or
tests/fixtures/pools_extra.json, which other UI suites share and this must
not perturb): one clean, non-excluded filename per role so each family's
OWN match rule (LORA-2A's "none": ["turbo", "distill", ...]) actually
passes -- ltx_transformer's role_rules PREFERS a distilled quant, which the
LTX-2.5 style family's own match rule then EXCLUDES (a real product
distinction: a distilled checkpoint can't use LoRAs trained against the
full model), so a single clean candidate is what makes the family show up
here at all.

Run: python3 tests/test_video_room_family_tabs_ui.py
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from _picture_server import FAILED, check, free_port  # noqa: E402

try:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as _pw:
        _pw.chromium.launch().close()
except Exception as e:
    print("SKIP: playwright or its chromium is not installed (%s). pip install -r requirements-dev.txt "
          "&& python3 -m playwright install --with-deps chromium" % e)
    sys.exit(0)

# One clean candidate per role -- each family's OWN match rule (see module
# docstring) needs a filename with no excluded word in it.
UNET_FILES = [
    "ltx25/ltx-2.5-video-transformer-bf16.safetensors",           # ltx_transformer (role_rules: all=["ltx"])
    "minimax_h3/minimax_h3_fl2va_unet_bf16.safetensors",          # h3_unet_fl2va (all=["minimax_h3"], none=["ref2v"])
    "minimax_music3/minimax_music3_dit_bf16.safetensors",         # music3_unet (all=["minimax_music3"])
]
NODES = {"UNETLoader": {"unet_name": [UNET_FILES]}}
SYSTEM_STATS = {"system": {"comfyui_version": "fake", "ram_free": 1},
                "devices": [{"name": "fake gpu", "vram_total": 1 << 36,
                             "vram_free": 1 << 36, "torch_vram_total": 0}]}
QUEUE = {"queue_running": [], "queue_pending": []}


class _Lane(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/system_stats":
            self._send(200, SYSTEM_STATS)
        elif path == "/queue":
            self._send(200, QUEUE)
        elif path.startswith("/object_info/"):
            node = path[len("/object_info/"):].strip("/")
            if node in NODES:
                self._send(200, {node: {"input": {"required": NODES[node]}}})
            else:
                self._send(404, {"error": "no such node"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        self._send(500, {"error": "fake lane never renders"})   # unused by this suite

    def log_message(self, *a):
        pass


SCRATCH = tempfile.mkdtemp(prefix="bwf_video_tabs_ui_")
PROCS = []


def wait_up(url, timeout=20):
    for _ in range(int(timeout * 10)):
        try:
            return urllib.request.urlopen(url, timeout=1)
        except Exception:
            time.sleep(0.1)
    raise SystemExit("did not come up: " + url)


lane = ThreadingHTTPServer(("127.0.0.1", 0), _Lane)
threading.Thread(target=lane.serve_forever, daemon=True).start()
lane_port = lane.server_address[1]
port = free_port()
data = os.path.join(SCRATCH, "data")
os.makedirs(data, exist_ok=True)
with open(os.path.join(data, "jobs.json"), "w") as f:
    json.dump({}, f)
cfg = os.path.join(SCRATCH, "config.json")
with open(cfg, "w") as f:
    json.dump({"title": "video tabs ui", "port": port, "bind": "127.0.0.1",
               "lanes": [{"id": "t", "name": "Real-pack lane", "host": "127.0.0.1", "port": lane_port,
                          "caps": ["video", "audio"]}],
               "timing": {"poll_seconds": 0.5, "job_poll_seconds": 1.0}}, f)
env = dict(os.environ, GENCENTER_CONFIG=cfg, GENCENTER_DATA=data)
PROCS.append(subprocess.Popen([sys.executable, os.path.join(ROOT, "server.py")], cwd=ROOT, env=env,
                              stdout=open(os.path.join(SCRATCH, "server.log"), "w"), stderr=subprocess.STDOUT))
url = "http://127.0.0.1:%d/" % port
wait_up(url + "api/health")


def finish():
    if sys.exc_info()[0] is not None:
        import traceback
        traceback.print_exc()
        FAILED.append("the suite crashed")
    for p in PROCS:
        p.terminate()
    lane.shutdown()
    print("\nFAILED: %d" % len(FAILED) + (" checks: " + ", ".join(FAILED) if FAILED else ""))
    sys.exit(1 if FAILED else 0)


try:
    from playwright.sync_api import sync_playwright as _spw
    with _spw() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(url + "#room=video", wait_until="networkidle")
        page.wait_for_function("() => STATE.lanes.some(l => l.id === 't' && l.up)", timeout=15000)
        page.wait_for_function("() => !document.querySelector('#browseStylesBtn').hidden", timeout=15000)
        # UX-2 #1: the engine chip's popover can be sitting open (it opens
        # on its own while the picked engine still reads as unavailable,
        # e.g. this suite's minimal one-candidate-per-role fixture during
        # the first discovery pass) and now overlays the panel instead of
        # pushing it down the way the old <details> summary did.
        page.evaluate("() => { if(typeof setEngineChipOpen === 'function') setEngineChipOpen(false); }")

        page.click("#browseStylesBtn")
        page.wait_for_function("() => document.querySelector('#lorasDialog').open", timeout=5000)
        page.wait_for_function("() => document.querySelectorAll('#lorasFamilyTabs button').length >= 1",
                                timeout=15000)
        labels = page.eval_on_selector_all("#lorasFamilyTabs button", "es => es.map(e => e.textContent)")
        check("Video room: BOTH real families show as tabs (LTX-2.5, MiniMax-H3), no BWF_TEST_STYLE_FAMILIES seam",
              "LTX-2.5" in labels and "MiniMax-H3" in labels, labels)
        page.close()

        # Talking Head: same cap (video), so the SAME real families are what
        # this room's drawer shows too -- there is no per-mode narrowing in
        # the catalog endpoint, only per-cap (server.py api_catalog_loras).
        page2 = browser.new_page(viewport={"width": 1280, "height": 800})
        page2.goto(url + "#room=talking", wait_until="networkidle")
        page2.wait_for_function("() => STATE.lanes.some(l => l.id === 't' && l.up)", timeout=15000)
        page2.wait_for_function("() => !document.querySelector('#browseStylesBtn').hidden", timeout=15000)
        page2.click("#browseStylesBtn")
        page2.wait_for_function("() => document.querySelector('#lorasDialog').open", timeout=5000)
        page2.wait_for_function("() => document.querySelectorAll('#lorasFamilyTabs button').length >= 1",
                                 timeout=15000)
        labels2 = page2.eval_on_selector_all("#lorasFamilyTabs button", "es => es.map(e => e.textContent)")
        check("Talking Head room: its real family (LTX-2.5) is present among the shown tabs",
              "LTX-2.5" in labels2, labels2)
        page2.close()

        # Music room: cap=audio -- its real family (MiniMax-Music3) shows,
        # from the same real engines.style_catalogs() path.
        page3 = browser.new_page(viewport={"width": 1280, "height": 800})
        page3.goto(url + "#room=music", wait_until="networkidle")
        page3.wait_for_function("() => STATE.lanes.some(l => l.id === 't' && l.up)", timeout=15000)
        page3.wait_for_function("() => !document.querySelector('#browseStylesBtn').hidden", timeout=15000)
        page3.click("#browseStylesBtn")
        page3.wait_for_function("() => document.querySelector('#lorasDialog').open", timeout=5000)
        page3.wait_for_function(
            "() => document.querySelectorAll('#lorasFamilyTabs button').length >= 1"
            + " || document.querySelector('#lorasBody .credit-row')", timeout=15000)
        body3 = page3.inner_text("#lorasBody")
        tablabels3 = page3.eval_on_selector_all(
            "#lorasFamilyTabs button", "es => es.map(e => e.textContent).join(',')")
        check("Music room: its real family (MiniMax-Music3) is present -- either as a labelled tab, "
              "or as the single family the drawer defaulted to with no tabs to show",
              "MiniMax-Music3" in body3 or "MiniMax-Music3" in tablabels3, body3)
        page3.close()

        browser.close()
finally:
    finish()
