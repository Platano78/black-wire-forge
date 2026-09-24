"""Browser gate for the room guide panel (P1: the Film Room Guide in the
Cutting Room).

Drives the real page in a real browser (Playwright) against its own
server.py subprocesses (scratch config + scratch data, random free ports),
its own fake ComfyUI lane, and an in-process fake OpenAI-compatible helper
that records every chat request. One server has the helper configured, one
has none (the no-brain state).

Set BWF_GUIDE_SHOTS=<dir> to also save screenshots (1280x800 and 390 wide,
brain and no-brain) into that directory.

Run: python3 tests/test_guide_ui.py
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
SHOTS = os.environ.get("BWF_GUIDE_SHOTS")

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

# ---- fake helper: records every chat body, answers /models --------------
HELPER = {"reply": "ok", "finish": "stop", "requests": []}

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
        self._send({"choices": [{"message": {"content": HELPER["reply"]}, "finish_reason": HELPER["finish"]}]})
    def log_message(self, *a):
        pass

fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeHelper)
threading.Thread(target=fake.serve_forever, daemon=True).start()

PROCS = []
SCRATCH = tempfile.mkdtemp(prefix="bwf_guide_ui_")

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
    cfg = {"title": "guide UI test", "port": port, "bind": "127.0.0.1",
           "lanes": [{"id": "t", "name": "Fake lane", "host": "127.0.0.1", "port": lane_port,
                      "caps": ["image", "video", "audio"]}],
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

COMPACT = read("guides/film/system-prompt.txt")
VERBOSE = read("guides/film/system-prompt-verbose.txt")
META = json.loads(read("guides/film/guide.json"))

def enter_cutting(page, url):
    page.goto(url + "#room=cutting", wait_until="networkidle", timeout=30000)
    page.wait_for_function("() => document.querySelector('#guideName') && "
                           "document.querySelector('#guideName').textContent.includes('Film Room Guide')",
                           timeout=15000)

def guide_msgs(page):
    return page.eval_on_selector_all("#guideLog .guide-msg .guide-text", "els => els.map(e => e.textContent)")

def send(page, text, expect_count):
    page.fill("#guideInput", text)
    page.press("#guideInput", "Enter")
    page.wait_for_function("n => document.querySelectorAll('#guideLog .guide-msg').length >= n", arg=expect_count,
                           timeout=15000)
    page.wait_for_function("() => document.querySelector('#guideThinking').hidden", timeout=15000)

def no_hscroll(page):
    return page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")

def shot(page, name):
    if SHOTS:
        os.makedirs(SHOTS, exist_ok=True)
        page.screenshot(path=os.path.join(SHOTS, name + ".png"), full_page=False)

try:
    lane = start_lane()
    url_brain = start_server("brain", lane, True)
    url_none = start_server("nobrain", lane, False)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()

        # ---------------- no brain ----------------
        print("no brain: guidance + how to add a brain, no input box")
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        enter_cutting(page, url_none)
        check("no brain: panel visible", page.is_visible("#guidePanel"))
        check("no brain: definition shown", "working film editor" in page.inner_text("#guideDefinition"))
        check("no brain: every guidance line shown",
              page.eval_on_selector_all("#guideNoBrainList li", "els => els.map(e => e.textContent)") == META["no_brain"])
        check("no brain: add_brain sentence shown", "config.json" in page.inner_text("#guideAddBrain"))
        check("no brain: no input box", not page.is_visible("#guideInput") and not page.is_visible("#guideSendBtn"))
        shot(page, "nobrain-1280x800")
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(300)
        check("no brain: no horizontal scroll at 390", no_hscroll(page))
        page.locator("#guidePanel").scroll_into_view_if_needed()
        shot(page, "nobrain-390")
        page.close()

        # ---------------- brain ----------------
        print("brain: summary visible on entry, compact by default")
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        enter_cutting(page, url_brain)
        box = page.eval_on_selector("#guidePanel > summary", "e => { const r = e.getBoundingClientRect(); "
                                    "return [r.top, r.bottom, window.innerHeight]; }")
        check("brain: guide heading within the viewport at 1280x800", box[1] <= box[2] and box[0] >= 0, box)
        check("brain: greeting shown first", guide_msgs(page)[:1] == [META["greeting"]], guide_msgs(page))
        check("brain: compact selected by default",
              page.get_attribute('#guideVerbosity [data-verbosity="compact"]', "aria-pressed") == "true")

        print("brain: a reply renders as text, never as HTML")
        HELPER.update(reply='<img src=x onerror="window.__xss=1">Decide the ending first.', finish="stop")
        HELPER["requests"].clear()
        send(page, "a lighthouse keeper's last night", 3)
        page.wait_for_timeout(300)
        check("reply: rendered", guide_msgs(page)[-1] == HELPER["reply"], guide_msgs(page)[-1:])
        check("reply: no img element created", page.eval_on_selector_all("#guideLog img", "els => els.length") == 0)
        check("reply: no script ran", page.evaluate("() => window.__xss === undefined"))
        req = HELPER["requests"][-1] if HELPER["requests"] else {}
        check("compact: system prompt is the compact file", (req.get("messages") or [{}])[0].get("content") == COMPACT)

        print("brain: the second turn carries the history")
        HELPER.update(reply="Two characters, one room.")
        send(page, "who is in it?", 5)
        req = HELPER["requests"][-1]
        check("history: second request carries both turns in order", req["messages"][1:] == [
            {"role": "user", "content": "a lighthouse keeper's last night"},
            {"role": "assistant", "content": '<img src=x onerror="window.__xss=1">Decide the ending first.'},
            {"role": "user", "content": "who is in it?"}], req["messages"][1:])

        print("brain: verbose switches the system prompt and warns, never flips back")
        page.click('#guideVerbosity [data-verbosity="verbose"]')
        check("verbose: selected", page.get_attribute('#guideVerbosity [data-verbosity="verbose"]', "aria-pressed") == "true")
        check("verbose: small-context warning shown", page.is_visible("#guideCtxNote")
              and "8,192" in page.inner_text("#guideCtxNote"), page.inner_text("#guideCtxNote"))
        HELPER.update(reply="Beat one: the lamp. Beat two: the", finish="length")
        send(page, "give me the full 30s plan", 7)
        check("verbose: system prompt is the verbose file", HELPER["requests"][-1]["messages"][0]["content"] == VERBOSE)
        check("verbose: still selected after sending",
              page.get_attribute('#guideVerbosity [data-verbosity="verbose"]', "aria-pressed") == "true")
        hints = page.eval_on_selector_all("#guideLog .guide-hint", "els => els.map(e => e.textContent)")
        check("truncated: hint shown under the reply", any("cut off" in h for h in hints), hints)
        shot(page, "brain-1280x800")

        print("brain: reload keeps the history and the toggle")
        before = guide_msgs(page)
        page.reload(wait_until="networkidle")
        page.wait_for_function("() => document.querySelectorAll('#guideLog .guide-msg').length >= 7", timeout=15000)
        check("reload: history kept", guide_msgs(page) == before, guide_msgs(page))
        check("reload: verbose kept", page.get_attribute('#guideVerbosity [data-verbosity="verbose"]', "aria-pressed") == "true")

        print("brain: each sequence has its own conversation")
        page.fill("#seqNewTitle", "guide seq")
        page.click("#seqNewBtn")
        page.wait_for_function("() => location.hash.includes('seq=')", timeout=15000)
        page.wait_for_timeout(400)
        check("sequence: a new sequence starts with only the greeting", guide_msgs(page) == [META["greeting"]],
              guide_msgs(page))
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(300)
        check("brain: no horizontal scroll at 390", no_hscroll(page))
        page.locator("#guidePanel").scroll_into_view_if_needed()
        shot(page, "brain-390")
        page.set_viewport_size({"width": 1280, "height": 800})
        page.click("#seqCloseBtn")
        page.wait_for_function("n => document.querySelectorAll('#guideLog .guide-msg').length >= n",
                               arg=len(before), timeout=15000)
        check("sequence: closing it brings the room's own conversation back", guide_msgs(page) == before,
              guide_msgs(page))

        print("brain: clear empties the conversation, and it stays empty")
        page.click("#guideClearBtn")
        check("clear: only the greeting left", guide_msgs(page) == [META["greeting"]])
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(800)
        check("clear: still empty after reload", guide_msgs(page) == [META["greeting"]], guide_msgs(page))
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
