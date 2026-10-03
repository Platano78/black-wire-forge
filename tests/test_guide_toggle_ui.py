"""Browser gate for the guide on / off switch.

Off = the room behaves exactly as if no guide model were configured: the person
types straight into the fields and Make sends exactly what they typed. The
switch is shown only when the server HAS a guide model (the server's truth is
kept in GUIDE.helper_server), and the choice is this browser's, for every room.

Same setup as tests/test_guide_ui.py: its own server.py subprocesses (scratch
config + scratch data, random free ports), its own fake ComfyUI lane, and an
in-process fake OpenAI-compatible helper that records every request it is sent.

Run: python3 tests/test_guide_toggle_ui.py
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

# ---- fake helper: records every request, answers /models --------------
HELPER = {"reply": "ok", "finish": "stop", "requests": [], "delay": 0, "replies": []}

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
        time.sleep(HELPER["delay"])
        reply = HELPER["replies"].pop(0) if HELPER["replies"] else HELPER["reply"]
        self._send({"choices": [{"message": {"content": reply}, "finish_reason": HELPER["finish"]}]})
    def log_message(self, *a):
        pass

fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeHelper)
threading.Thread(target=fake.serve_forever, daemon=True).start()

PROCS = []
SCRATCH = tempfile.mkdtemp(prefix="bwf_guide_toggle_ui_")

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

def start_server(name, lane_port, with_helper):
    port = free_port()
    cfg = {"title": "guide toggle test", "port": port, "bind": "127.0.0.1",
           "lanes": [{"id": "t", "name": "Fake lane", "host": "127.0.0.1", "port": lane_port,
                      "caps": ["image", "video", "audio"],
                      "models": {"ace_unet": "ace.safetensors", "ace_clip1": "a.safetensors",
                                 "ace_clip2": "b.safetensors", "ace_vae": "v.safetensors",
                                 "music3_unet": "m.safetensors", "music3_clip": "mc.safetensors",
                                 "music3_vae": "mv.safetensors"}}],
           "timing": {"poll_seconds": 0.5, "job_poll_seconds": 1.0}}
    if with_helper:
        cfg["helper"] = {"url": "http://127.0.0.1:%d/v1" % fake.server_address[1], "model": "test-model",
                         "timeout_s": 10}
    cfg_path = os.path.join(SCRATCH, "config_%s.json" % name)
    with open(cfg_path, "w") as f:
        json.dump(cfg, f)
    env = dict(os.environ, GENCENTER_CONFIG=cfg_path, GENCENTER_DATA=os.path.join(SCRATCH, "data_" + name))
    logf = open(os.path.join(SCRATCH, "server_%s.log" % name), "w")
    PROCS.append(subprocess.Popen([sys.executable, os.path.join(REPO, "server.py")], cwd=REPO, env=env,
                                  stdout=logf, stderr=subprocess.STDOUT))
    url = "http://127.0.0.1:%d/" % port
    if not wait_true("server %s is up" % name, lambda: http_json(url + "api/health").get("ok"), 30):
        raise SystemExit("server %s did not come up" % name)
    return url

def read(rel):
    with open(os.path.join(REPO, rel), encoding="utf-8") as f:
        return f.read()

ROOM_GUIDES = {r["id"]: r["guide"] for r in json.loads(read("rooms.json"))}
GUIDE_META = {gid: json.loads(read("guides/%s/guide.json" % gid)) for gid in set(ROOM_GUIDES.values())}

def enter_music(page, url):
    page.goto("about:blank")   # a hash-only goto would not reload the page
    page.goto(url + "#room=music", wait_until="networkidle", timeout=30000)
    page.wait_for_function("n => document.querySelector('#guideName') && "
                           "document.querySelector('#guideName').textContent === n",
                           arg=GUIDE_META[ROOM_GUIDES["music"]]["name"], timeout=15000)
    page.wait_for_function("() => document.querySelector('#guideOnOff') && "
                           "!document.querySelector('#guideOnOff').hidden", timeout=15000)

def text_of(page, sel):
    el = page.query_selector(sel)
    return el.inner_text() if el else ""

def switch_state(page):
    return (page.inner_text("#guideOnOff"), page.get_attribute("#guideOnOff", "aria-pressed"),
            page.is_visible("#guideInput"))

def prompt_box_ok(page):
    """Visible, enabled, and never parked inside a closed <details>."""
    return page.evaluate("""() => {
        const b = document.querySelector('#promptBox');
        if(!b || !b.offsetParent || b.disabled || b.readOnly) return false;
        let d = b.closest('details');
        while(d){ if(!d.open) return false; d = d.parentElement && d.parentElement.closest('details'); }
        return true;
    }""")

GEN_BODIES = []
HELPER_CALLS = []
def record(url_suffix, bucket):
    def rec(req):
        if req.url.split("?")[0].endswith(url_suffix) and req.method == "POST":
            bucket.append(json.loads(req.post_data or "{}"))
    return rec

def make(page):
    """Press Make (answering the Make-time confirm if the page asks)."""
    page.click("#makeBtn")
    page.wait_for_timeout(500)
    if page.is_visible("#makeAnywayBtn"):
        page.click("#makeAnywayBtn")
    page.wait_for_timeout(800)

PROMPT = "a lighthouse at sunset"

try:
    lane = start_lane()
    url_brain = start_server("brain", lane, True)
    url_none = start_server("nobrain", lane, False)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()

        print("Music with a guide model: the switch is on, and says so")
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.route("**/api/generate", lambda route: (GEN_BODIES.append(json.loads(route.request.post_data or "{}")),
                                                      route.continue_())[1])
        page.on("request", record("/api/guide/chat", HELPER_CALLS))
        enter_music(page, url_brain)
        check("on: the switch is visible and reads on", switch_state(page) == ("Guide: on", "true", True), switch_state(page))
        check("on: it is a plain button", page.get_attribute("#guideOnOff", "type") == "button"
              and page.eval_on_selector("#guideOnOff", "e => e.classList.contains('btn')"))
        check("on: the guide's own box is there", page.is_visible("#guideInput")
              and page.get_attribute("#guideInput", "placeholder") == "Tell the Music room what you want")
        check("on: the prompt box is inside the guide's disclosure", not prompt_box_ok(page))

        print("click it off: the prompt box is the main field again")
        HELPER["requests"].clear()
        del HELPER_CALLS[:]
        page.click("#guideOnOff")
        page.wait_for_function("() => document.querySelector('#guideOnOff').textContent === 'Guide: off'", timeout=10000)
        check("off: the switch reads off", switch_state(page) == ("Guide: off", "false", False), switch_state(page))
        check("off: the prompt box is visible, enabled, and not in a closed disclosure", prompt_box_ok(page))
        note = text_of(page, "#guideOffNote")
        check("off: the one-line note says so, with a way back in the switch",
              page.is_visible("#guideOffNote") and "Type straight into the fields" in note, note)
        check("off: no 'add a helper to config.json' -- there IS one",
              not page.is_visible("#guideAddBrain") and not page.is_visible("#guideWriteNoBrain")
              and not page.is_visible("#guideReviseNoBrain"),
              (page.is_visible("#guideAddBrain"), page.is_visible("#guideWriteNoBrain"),
               page.is_visible("#guideReviseNoBrain")))
        check("off: nothing went to the helper on the click", HELPER["requests"] == [] and HELPER_CALLS == [],
              (HELPER["requests"], HELPER_CALLS))

        print("off: Make sends exactly what was typed")
        page.fill("#promptBox", PROMPT)
        del GEN_BODIES[:]
        make(page)
        body = GEN_BODIES[-1] if GEN_BODIES else {}
        check("off: /api/generate carried the prompt as typed", bool(GEN_BODIES) and body.get("prompt") == PROMPT, body)
        neg = body.get("negative")
        check("off: nothing was added to the negative prompt",
              not (isinstance(neg, str) and neg.strip()), repr(neg))
        check("off: the helper was never asked", HELPER["requests"] == [] and HELPER_CALLS == [],
              (HELPER["requests"], HELPER_CALLS))

        print("off: it is remembered, and comes back on")
        page.reload(wait_until="networkidle")
        enter_music(page, url_brain)
        check("reload: still off", switch_state(page) == ("Guide: off", "false", False), switch_state(page))
        check("reload: the prompt box is still the main field", prompt_box_ok(page))
        HELPER["requests"].clear()
        page.click("#guideOnOff")
        page.wait_for_function("() => document.querySelector('#guideOnOff').textContent === 'Guide: on'", timeout=10000)
        check("on again: the guide's box is back", switch_state(page) == ("Guide: on", "true", True), switch_state(page))
        check("on again: the prompt box is back under the guide", not prompt_box_ok(page))
        check("on again: nothing went to the helper", HELPER["requests"] == [])

        print("390 wide: the same switch, a finger-sized one")
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(400)
        page.locator("#guideOnOff").scroll_into_view_if_needed()
        check("390: the switch is visible", page.is_visible("#guideOnOff"))
        check("390: it is at least 44px tall",
              page.eval_on_selector("#guideOnOff", "e => e.getBoundingClientRect().height >= 44"),
              page.eval_on_selector("#guideOnOff", "e => e.getBoundingClientRect().height"))
        page.click("#guideOnOff")
        page.wait_for_function("() => document.querySelector('#guideOnOff').textContent === 'Guide: off'", timeout=10000)
        check("390: off, and the prompt box is the main field",
              switch_state(page) == ("Guide: off", "false", False) and prompt_box_ok(page), switch_state(page))
        check("390: the note still says so", "Type straight into the fields" in text_of(page, "#guideOffNote"))
        check("390: no horizontal scroll", page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth"))
        page.click("#guideOnOff")
        page.wait_for_function("() => document.querySelector('#guideOnOff').textContent === 'Guide: on'", timeout=10000)
        check("390: and on again", page.is_visible("#guideInput"))
        page.close()

        print("no guide model on the server: no switch, and the old note")
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(url_none + "#room=music", wait_until="networkidle", timeout=30000)
        page.wait_for_function("n => document.querySelector('#guideName') && "
                               "document.querySelector('#guideName').textContent === n",
                               arg=GUIDE_META[ROOM_GUIDES["music"]]["name"], timeout=15000)
        check("no helper: no switch", not page.is_visible("#guideOnOff"))
        check("no helper: the one-line note is unchanged",
              text_of(page, "#guideOffNote") == "The guide is off, so type straight into the fields.",
              text_of(page, "#guideOffNote"))
        check("no helper: how to add a helper is still there", page.is_visible("#guideAddBrain")
              and "config.json" in text_of(page, "#guideAddBrain"))
        page.close()

        print("a browser that will not store anything: the switch still works")
        errors = []
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.add_init_script("Storage.prototype.setItem = function(){ throw new Error('storage is full'); };")
        enter_music(page, url_brain)
        page.click("#guideOnOff")
        page.wait_for_function("() => document.querySelector('#guideOnOff').textContent === 'Guide: off'", timeout=10000)
        check("blocked storage: the switch still turns off, in memory",
              switch_state(page) == ("Guide: off", "false", False) and prompt_box_ok(page), switch_state(page))
        page.click("#guideOnOff")
        page.wait_for_function("() => document.querySelector('#guideOnOff').textContent === 'Guide: on'", timeout=10000)
        check("blocked storage: and back on", page.is_visible("#guideInput"))
        check("blocked storage: no page error", errors == [], errors)
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