"""Browser gate for "Write from a picture" in the Sound rooms.

With the guide ON: the Music room (a mode whose writer fills `lyrics`) shows
#helperPictureBtn; the Picture room does not. Choosing a file in
#helperPictureFile uploads it (/api/upload, the page's existing shape) and
sends exactly one POST /api/guide/skill whose JSON carries the music mode and
pictures == [{lane, upload}].

Same setup as tests/test_guide_toggle_ui.py: its own server.py subprocess
(scratch config + scratch data, a random free port), its own fake ComfyUI
lane, and an in-process fake OpenAI-compatible helper.

Run: python3 tests/test_guide_picture_lyrics_ui.py
"""
import json
import os
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
HERE = os.path.dirname(os.path.abspath(__file__))

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail else ""))
    if not cond:
        FAILED.append(name)

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p

def http_json(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())

def wait_true(desc, fn, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if fn():
                return True
        except Exception:
            pass
        time.sleep(0.15)
    check(desc, False, "still false after %.0fs" % timeout)
    return False

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIP: playwright is not installed. pip install -r requirements-dev.txt "
          "&& python3 -m playwright install --with-deps chromium")
    sys.exit(0)

try:
    with sync_playwright() as _pw_probe:
        _pw_probe.chromium.launch().close()
except Exception as _pw_err:
    print("SKIP: playwright's chromium browser is not installed (%s). "
          "Run: python3 -m playwright install --with-deps chromium" % _pw_err)
    sys.exit(0)

# ---- fake helper: records every request, answers /models -------------------
HELPER = {"reply": "ok", "requests": []}

class FakeHelper(BaseHTTPRequestHandler):
    def _send(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        self._send({"data": [{"id": "test-model", "meta": {"n_ctx": 8192}}]})
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        HELPER["requests"].append(json.loads(self.rfile.read(n) or b"{}"))
        self._send({"choices": [{"message": {"content": HELPER["reply"]}, "finish_reason": "stop"}]})
    def log_message(self, *a):
        pass

fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeHelper)
threading.Thread(target=fake.serve_forever, daemon=True).start()

PROCS = []
SCRATCH = tempfile.mkdtemp(prefix="bwf_guide_picture_lyrics_ui_")

def start_lane():
    store = os.path.join(SCRATCH, "fake_lane")
    os.makedirs(os.path.join(store, "outputs"), exist_ok=True)
    port = free_port()
    PROCS.append(subprocess.Popen(
        [sys.executable, os.path.join(HERE, "fixtures", "fake_comfy.py"), "--port", str(port), "--store", store],
        cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    if not wait_true("fake lane answers", lambda: http_json("http://127.0.0.1:%d/system_stats" % port), 15):
        raise SystemExit("fake lane did not come up")
    return port

def start_server(lane_port):
    port = free_port()
    cfg = {"title": "guide picture lyrics test", "port": port, "bind": "127.0.0.1",
           "lanes": [{"id": "t", "name": "Fake lane", "host": "127.0.0.1", "port": lane_port,
                      "caps": ["image", "video", "audio"],
                      "models": {"ace_unet": "ace.safetensors", "ace_clip1": "a.safetensors",
                                 "ace_clip2": "b.safetensors", "ace_vae": "v.safetensors",
                                 "music3_unet": "m.safetensors", "music3_clip": "mc.safetensors",
                                 "music3_vae": "mv.safetensors"}}],
           "timing": {"poll_seconds": 0.5, "job_poll_seconds": 1.0},
           "helper": {"url": "http://127.0.0.1:%d/v1" % fake.server_address[1], "model": "test-model",
                      "timeout_s": 10, "guide_default": "on"}}
    cfg_path = os.path.join(SCRATCH, "config.json")
    with open(cfg_path, "w") as f:
        json.dump(cfg, f)
    env = dict(os.environ, GENCENTER_CONFIG=cfg_path, GENCENTER_DATA=os.path.join(SCRATCH, "data"))
    logf = open(os.path.join(SCRATCH, "server.log"), "w")
    PROCS.append(subprocess.Popen([sys.executable, os.path.join(REPO, "server.py")], cwd=REPO, env=env,
                                  stdout=logf, stderr=subprocess.STDOUT))
    url = "http://127.0.0.1:%d/" % port
    if not wait_true("server is up", lambda: http_json(url + "api/health").get("ok"), 30):
        raise SystemExit("server did not come up")
    return url

def read(rel):
    with open(os.path.join(REPO, rel), encoding="utf-8") as f:
        return f.read()

ROOM_GUIDES = {r["id"]: r["guide"] for r in json.loads(read("rooms.json"))}
GUIDE_META = {gid: json.loads(read("guides/%s/guide.json" % gid)) for gid in set(ROOM_GUIDES.values())}

def enter_room(page, url, room):
    page.goto("about:blank")   # a hash-only goto would not reload the page
    page.goto(url + "#room=" + room, wait_until="networkidle", timeout=30000)
    page.wait_for_function("n => document.querySelector('#guideName') && "
                           "document.querySelector('#guideName').textContent === n",
                           arg=GUIDE_META[ROOM_GUIDES[room]]["name"], timeout=15000)
    page.wait_for_function("() => document.querySelector('#guideOnOff') && "
                           "!document.querySelector('#guideOnOff').hidden", timeout=15000)

PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
                    "0000000c4944415408d763f8cfc000000301010018dd8db00000000049454e44ae426082")
PIC_PATH = os.path.join(SCRATCH, "mood.png")
with open(PIC_PATH, "wb") as f:
    f.write(PNG)

SKILL_CALLS = []
def record(req):
    if req.url.split("?")[0].endswith("/api/guide/skill") and req.method == "POST":
        SKILL_CALLS.append(json.loads(req.post_data or "{}"))

try:
    lane = start_lane()
    url = start_server(lane)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.on("request", record)
        HELPER["reply"] = ("TAGS: warm female vocals, folk, singing\nBPM: NONE\nKEY: NONE\nDURATION: 30\n"
                           "TIMESIG: NONE\nLANGUAGE: NONE\nLYRICS:\n[Verse]\nline one\nline two\n")

        print("guide ON in the Music room: the button is there, the Picture room's writer has no Lyrics")
        enter_room(page, url, "music")
        check("music, guide on: the switch reads on",
              page.inner_text("#guideOnOff") == "Guide: on", page.inner_text("#guideOnOff"))
        check("music: #helperPictureBtn is visible", page.is_visible("#helperPictureBtn"))
        check("music: the button says what it does",
              page.inner_text("#helperPictureBtn") == "Write from a picture",
              page.inner_text("#helperPictureBtn"))
        enter_room(page, url, "picture")
        check("picture room: #helperPictureBtn is not there", not page.is_visible("#helperPictureBtn"))

        print("choosing a picture: it uploads, and ONE guide/skill request carries it")
        enter_room(page, url, "music")
        page.wait_for_function("() => document.querySelector('#helperPictureBtn') && "
                               "!document.querySelector('#helperPictureBtn').hidden", timeout=15000)
        music_mode = page.evaluate("() => STATE.mode")
        del SKILL_CALLS[:]
        page.set_input_files("#helperPictureFile", PIC_PATH)
        # The event only arrives while a Playwright call runs, so poll with
        # page.wait_for_timeout, not a plain sleep.
        deadline = time.time() + 20
        while time.time() < deadline and not SKILL_CALLS:
            page.wait_for_timeout(200)
        ok = bool(SKILL_CALLS)
        body = SKILL_CALLS[0] if SKILL_CALLS else {}
        check("the request is for the music mode", ok and len(SKILL_CALLS) == 1
              and body.get("room") == "music" and body.get("mode") == music_mode, (len(SKILL_CALLS), body))
        pics = body.get("pictures") or []
        check("the request carries the uploaded picture as {lane, upload}",
              len(pics) == 1 and set(pics[0]) == {"lane", "upload"} and pics[0]["lane"] == "t"
              and str(pics[0]["upload"]).endswith(".png"), pics)
        page.close()
        browser.close()
finally:
    for p in PROCS:
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()

if FAILED:
    print("FAILED: %d checks: %s" % (len(FAILED), ", ".join(FAILED)))
    sys.exit(1)
print("OK: all checks passed")
