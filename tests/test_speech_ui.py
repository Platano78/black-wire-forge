"""Browser gate for Speak it: a sound field offers a typed line when the
server has a speech source, the spoken wav lands in the same file slot a picked
recording does, and with no speech source the page is exactly as it was (no
Speak it row at all).

Same setup as tests/test_speech_source.py: its own server.py subprocesses
(scratch config + scratch data, random free ports), its own fake ComfyUI lane,
and an in-process fake OpenAI-style speech endpoint that records every request
and answers with a real 1 s wav.

Run: python3 tests/test_speech_ui.py
"""
import json
import math
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import wave as _wave

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

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
    # Finding #21: absence of an OPTIONAL dev dependency is a SKIP, not a
    # failure -- a clean clone with no playwright must not read as broken.
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


# ---- fake speech source: records every request, answers with a 1 s wav ----
def make_wav():
    """Bytes of a 1 s 16 kHz mono 16-bit WAV sine wave."""
    import tempfile as _tf
    path = _tf.NamedTemporaryFile(suffix=".wav", delete=False).name
    with _wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"".join(struct.pack("<h", int(32767 * 0.3 * math.sin(2 * math.pi * 440 * i / 16000)))
                                for i in range(16000)))
    with open(path, "rb") as f:
        data = f.read()
    os.unlink(path)
    return data

WAV = make_wav()
SAID = []

class FakeSpeech:
    """The in-process OpenAI-style /audio/speech the server is pointed at."""
    def handle(self, req):
        if req.command != "POST":
            req.send_response(404)
            req.end_headers()
            return
        n = int(req.headers.get("Content-Length") or 0)
        SAID.append(json.loads(req.rfile.read(n) or b"{}"))
        req.send_response(200)
        req.send_header("Content-Type", "audio/wav")
        req.send_header("Content-Length", str(len(WAV)))
        req.end_headers()
        req.wfile.write(WAV)

class FakeSpeechServer(threading.Thread):
    def __init__(self, handler):
        super().__init__(daemon=True)
        self.handler = handler
        self.server = None
        self.port = None

    def run(self):
        from http.server import HTTPServer, BaseHTTPRequestHandler
        class Adaptor(BaseHTTPRequestHandler):
            speech = None
            def do_POST(self):
                self.speech.handle(self)
            def log_message(self, fmt, *args):
                pass
        Adaptor.speech = self.handler
        self.server = HTTPServer(("127.0.0.1", 0), Adaptor)
        self.port = self.server.server_address[1]
        self.server.serve_forever()

    def stop(self):
        if self.server is not None:
            self.server.shutdown()

SCRATCH = tempfile.mkdtemp(prefix="bwf_speech_ui_")
PROCS = []
SPEECH_THREAD = FakeSpeechServer(FakeSpeech())
SPEECH_THREAD.start()
while SPEECH_THREAD.port is None:
    time.sleep(0.05)

def start_lane():
    store = os.path.join(SCRATCH, "fake_lane")
    os.makedirs(os.path.join(store, "outputs"), exist_ok=True)
    port = free_port()
    PROCS.append(subprocess.Popen(
        [sys.executable, os.path.join(HERE, "fixtures", "fake_comfy.py"), "--port", str(port), "--store", store],
        cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    if not wait_true("fake lane answers /system_stats",
                     lambda: http_json("http://127.0.0.1:%d/system_stats" % port), 15):
        raise SystemExit("fake lane did not come up")
    return port

def start_server(name, lane_port, with_speech):
    port = free_port()
    cfg = {"title": "speech UI test", "port": port, "bind": "127.0.0.1",
           "lanes": [{"id": "t", "name": "Fake lane", "host": "127.0.0.1", "port": lane_port,
                      "caps": ["image", "video", "audio"],
                      "models": {"ltx_transformer": "ltx.gguf", "ltx_clip": "ltx_te.safetensors",
                                 "ltx_vae_video": "ltx_vae.safetensors", "ltx_vae_audio": "ltx_audio.vae.safetensors",
                                 "ltx_upscaler": "ltx_up.safetensors"}}],
           "timing": {"poll_seconds": 0.5, "job_poll_seconds": 1.0, "http_timeout": 5.0}}
    if with_speech:
        cfg["speech"] = {"url": "http://127.0.0.1:%d" % SPEECH_THREAD.port, "model": "tts-1",
                         "voice": "alloy", "timeout": 10}
    cfg_path = os.path.join(SCRATCH, "config_%s.json" % name)
    with open(cfg_path, "w") as f:
        json.dump(cfg, f)
    env = dict(os.environ, GENCENTER_CONFIG=cfg_path, GENCENTER_DATA=os.path.join(SCRATCH, "data_" + name))
    logf = open(os.path.join(SCRATCH, "server_%s.log" % name), "w")
    PROCS.append(subprocess.Popen([sys.executable, os.path.join(REPO, "server.py")], cwd=REPO, env=env,
                                  stdout=logf, stderr=subprocess.STDOUT))
    url = "http://127.0.0.1:%d/" % port
    if not wait_true("server %s is up" % name, lambda: http_json(url + "api/health").get("ok"), 30):
        with open(os.path.join(SCRATCH, "server_%s.log" % name)) as f:
            print("  -- server %s log tail: %s" % (name, f.read()[-1200:]))
        raise SystemExit("server %s did not come up" % name)
    return url

def open_talking(page, url):
    """The Talking Head room, its sound field open and the recipe drawer expanded."""
    page.goto("about:blank")   # a hash-only goto would not reload the page
    page.goto(url + "#room=talking", wait_until="networkidle", timeout=30000)
    page.wait_for_function("() => !!STATE.activeLane", timeout=15000)
    if page.query_selector("#upload_audio_slice") is None:
        page.check('#enginePicker input[data-cap="video"][data-mode="ltx"]')
        page.wait_for_selector("#upload_audio_slice", timeout=15000)
    page.evaluate("() => { document.querySelector('#recipeDetails').open = true; }")
    page.wait_for_selector("#upload_audio_slice", timeout=15000)
    page.wait_for_timeout(300)

def msg(page):
    el = page.query_selector("#inspectorMsg")
    return el.text_content() if el else ""

try:
    lane = start_lane()
    url_on = start_server("on", lane, True)
    url_off = start_server("off", lane, False)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()

        print("a speech source: the sound field offers Speak it")
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        open_talking(page, url_on)
        check("the page knows the server can speak", page.evaluate("() => STATE.speechOn") is True)
        check("the Speak it box and button are there",
              page.is_visible("#speak_audio_slice") and page.is_visible("#speakBtn_audio_slice"))
        btn = page.query_selector("#speakBtn_audio_slice")
        check("the button says Speak it",
              btn is not None and btn.inner_text().strip() == "Speak it")

        print("nothing typed: the page asks for the line and says nothing to the speech source")
        del SAID[:]
        page.click("#speakBtn_audio_slice")
        page.wait_for_timeout(600)
        check("empty text: the page asks for the line first",
              msg(page) == "Type the line to speak first.", msg(page))
        check("empty text: the speech source was not asked", SAID == [], SAID)

        print("a typed line: it is spoken, and the wav lands in the field")
        page.fill("#speak_audio_slice", "hello there")
        page.click("#speakBtn_audio_slice")
        page.wait_for_function("() => document.querySelector('#inspectorMsg').textContent.startsWith('Spoken:')",
                               timeout=15000)
        check("the speech source was asked once, with the line as typed",
              len(SAID) == 1 and SAID[0].get("input") == "hello there", SAID)
        check("the page reports the length it got back",
              msg(page).startswith("Spoken: 1.0 s."), msg(page))
        check("the field now holds the spoken file, the way an uploaded one shows",
              page.evaluate("() => (STATE.uploads.audio_slice || []).length === 1")
              and page.inner_text('[data-ledger-row="audio_slice"] .ref-tag') == "1 loaded"
              and page.query_selector("#thumbs_audio_slice .t") is not None,
              page.evaluate("() => STATE.uploads.audio_slice"))
        check("the spoken file has the name the lane stored",
              page.evaluate("() => STATE.uploads.audio_slice[0].name || ''") == "speech.wav",
              page.evaluate("() => STATE.uploads.audio_slice[0].name"))
        check("the Speak it button is usable again", not page.eval_on_selector("#speakBtn_audio_slice", "e => e.disabled"))
        check("no page JS errors", errors == [], errors)
        page.close()

        print("no speech source: the page is exactly as it was")
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        off_errors = []
        page.on("pageerror", lambda e: off_errors.append(str(e)))
        open_talking(page, url_off)
        check("the page knows it cannot speak", page.evaluate("() => STATE.speechOn") is False)
        check("no Speak it row anywhere in the room",
              page.evaluate("() => document.querySelectorAll('[id^=\"speak_\"]').length") == 0,
              page.evaluate("() => document.querySelectorAll('[id^=\"speak_\"]').length"))
        check("the sound field's own picker is untouched", page.is_visible("#upload_audio_slice"))
        check("no page JS errors", off_errors == [], off_errors)
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
    SPEECH_THREAD.stop()

print()
print("ALL PASS" if not FAILED else "FAILED: %d -- %s" % (len(FAILED), FAILED))
sys.exit(1 if FAILED else 0)