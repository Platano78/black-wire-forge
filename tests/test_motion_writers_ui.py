"""Browser gate for P3c: the Talking Head write conversation (a tone question
with options -> the preview -> Use these fills line, look and length) and
"Not right? Tell the guide" on a finished ltx clip. Real page, real browser,
its own server.py, fake lane and scripted helper. Run: python3 tests/test_motion_writers_ui.py
"""
import json, os, socket, subprocess, sys, tempfile, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__)); REPO = os.path.dirname(HERE)
FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else "  %s" % (detail,)))
    if not cond: FAILED.append(name)
try:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as _p: _p.chromium.launch().close()
except Exception as e:
    print("SKIP: playwright or its chromium is not installed (%s)" % e); sys.exit(0)
def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p
def up(url, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        try: return urllib.request.urlopen(url, timeout=2).status == 200
        except Exception: time.sleep(0.2)
    return False
REPLIES, BODIES = [], []
class Brain(BaseHTTPRequestHandler):
    def _send(self, obj):
        b = json.dumps(obj).encode(); self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self): self._send({"data": [{"id": "test-model", "meta": {"n_ctx": 8192}}]})
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self._send({"choices": [{"message": {"content": REPLIES.pop(0) if REPLIES else "ok"}, "finish_reason": "stop"}]})
    def log_message(self, *a): pass
brain = ThreadingHTTPServer(("127.0.0.1", 0), Brain); threading.Thread(target=brain.serve_forever, daemon=True).start()
S = tempfile.mkdtemp(prefix="bwf_motion_ui_"); PROCS = []
lane_port, port = free_port(), free_port()
PROCS.append(subprocess.Popen([sys.executable, os.path.join(HERE, "fixtures", "fake_comfy.py"), "--port", str(lane_port),
                               "--store", os.path.join(S, "lane")], cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
JOB = {"id": "clipjob1", "lane": "t", "lane_name": "Fake lane", "kind": "video", "mode": "ltx", "status": "done",
       "prompt": "Slow push toward the lighthouse lamp.", "created": time.time(), "started": time.time(), "updated": time.time(),
       "notes": [], "outputs": [{"filename": "LTX_00005_.mp4", "subfolder": "", "type": "output", "media": "video"}]}
os.makedirs(os.path.join(S, "data")); json.dump([JOB], open(os.path.join(S, "data", "jobs.json"), "w"))
cfg = {"port": port, "bind": "127.0.0.1", "title": "motion", "timing": {"poll_seconds": 0.5, "job_poll_seconds": 1.0},
       "lanes": [{"id": "t", "name": "Fake lane", "host": "127.0.0.1", "port": lane_port, "caps": ["video"],
                  "models": {"ltx_transformer": "ltx.gguf", "ltx_clip": "ltx_te.safetensors", "ltx_vae_video": "ltx_vae.safetensors",
                             "ltx_vae_audio": "ltx_audio_vae.safetensors", "ltx_upscaler": "ltx_spatial.safetensors"}}],
       "helper": {"url": "http://127.0.0.1:%d/v1" % brain.server_address[1], "model": "test-model", "timeout_s": 10, "vision": False}}
json.dump(cfg, open(os.path.join(S, "config.json"), "w"))
PROCS.append(subprocess.Popen([sys.executable, os.path.join(REPO, "server.py")], cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
                              env=dict(os.environ, GENCENTER_CONFIG=os.path.join(S, "config.json"), GENCENTER_DATA=os.path.join(S, "data"))))
URL = "http://127.0.0.1:%d/" % port
LINE = "Happy Monday, everyone! Let's crush those goals and make this week incredible!"   # live trial reply, 12 words
try:
    if not up(URL + "api/health"): raise SystemExit("server did not come up")
    with sync_playwright() as pw:
        page = pw.chromium.launch().new_page(viewport={"width": 1280, "height": 800})
        page.on("request", lambda r: BODIES.append(json.loads(r.post_data or "{}")) if r.url.endswith("/api/guide/skill") else None)
        print("Talking Head: the write conversation")
        page.goto(URL + "#room=talking", wait_until="networkidle")
        page.wait_for_selector("#helperWriteBtn", state="visible", timeout=15000)
        REPLIES[:] = ["QUESTION: What tone should it have?\nOPTIONS: Excited | Grumpy | Professional",
                      "LENGTH: NONE\nLOOK: bright morning light, close-up\nNOTE: excited tone\nLINE: " + LINE]
        page.fill("#promptBox", "say something about Mondays")
        page.click("#helperWriteBtn")
        page.wait_for_selector('#guideSkillOptions [data-option="Excited"]', timeout=15000)
        check("the tone question offers its options as chips", page.eval_on_selector_all(
            "#guideSkillOptions [data-option]", "els => els.map(e => e.dataset.option)") == ["Excited", "Grumpy", "Professional"])
        page.click('#guideSkillOptions [data-option="Excited"]')
        page.wait_for_selector("#guideSkillUse", timeout=15000)
        preview = page.inner_text("#guideSkill")
        check("the preview shows the line, the shot note and the length the app worked out",
              LINE in preview and "bright morning light, close-up" in preview and "153" in preview, preview)
        check("the answer went back with its question", BODIES[-1].get("answers") == [{"q": "What tone should it have?", "a": "Excited"}], BODIES[-1:])
        page.click("#guideSkillUse")
        val = lambda f: page.input_value('#inspector [data-field-id="%s"]' % f)
        check("Use these fills line, look and length", (val("line"), val("look"), val("length")) ==
              (LINE, "bright morning light, close-up", "153"), (val("line"), val("look"), val("length")))
        print("Video: Not right? on a finished ltx clip")
        page.goto("about:blank"); page.goto(URL + "#room=video", wait_until="networkidle")
        page.wait_for_selector('#binBody tr[data-job="clipjob1"]', timeout=15000)
        page.click('#binBody tr[data-job="clipjob1"]')
        page.wait_for_selector("#monitorActions", timeout=15000)
        check("the button is on the finished clip", page.is_visible("#notRightBtn"))
        page.click("#notRightBtn")
        page.wait_for_selector("#guideChip:not([hidden])", timeout=15000)
        check("the chip says it sends a still from the middle", "a still from the middle" in page.inner_text("#guideChip"))
        page.close()
finally:
    for p in PROCS: p.terminate()
print("FAILED: %d" % len(FAILED)); sys.exit(1 if FAILED else 0)
