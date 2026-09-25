"""Browser gate for P3e: "Write this shot" on a storyboard's Cutting Room shots. An LTX shot and an
H3 shot each run their OWN mode's writer (room = that mode's room) with the beat as the topic and
the beats either side in context.neighbours; the Film Room Guide speaks; "Use these" fills that
shot's own prompt; a preview never follows the user to another shot. Real page, its own server.py,
fake lane and scripted helper. Screenshots (3440x1440) go to $SHOTS_DIR when it is set.
Run: python3 tests/test_shot_writer_ui.py
"""
import json, os, socket, subprocess, sys, tempfile, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__)); REPO = os.path.dirname(HERE); sys.path.insert(0, REPO)
from engines import ltx, minimax_h3  # noqa: E402
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
def api(path, body=None):
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=10).read())
REPLIES, ASKED, BODIES = [], [], []
class Brain(BaseHTTPRequestHandler):
    def _send(self, obj):
        b = json.dumps(obj).encode(); self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self): self._send({"data": [{"id": "test-model", "meta": {"n_ctx": 32768}}]})
    def do_POST(self):
        ASKED.append(json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))["messages"])
        self._send({"choices": [{"message": {"content": REPLIES.pop(0) if REPLIES else "ok"}, "finish_reason": "stop"}]})
    def log_message(self, *a): pass
brain = ThreadingHTTPServer(("127.0.0.1", 0), Brain); threading.Thread(target=brain.serve_forever, daemon=True).start()
S = tempfile.mkdtemp(prefix="bwf_shot_ui_"); lane_port, port = free_port(), free_port(); URL = "http://127.0.0.1:%d/" % port
PROCS = [subprocess.Popen([sys.executable, os.path.join(HERE, "fixtures", "fake_comfy.py"), "--port", str(lane_port),
                           "--store", os.path.join(S, "lane")], cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)]
json.dump({"port": port, "bind": "127.0.0.1", "title": "shots", "timing": {"poll_seconds": 0.5, "job_poll_seconds": 1.0},
           "lanes": [{"id": "t", "name": "Fake lane", "host": "127.0.0.1", "port": lane_port, "caps": ["video"]}],
           "helper": {"url": "http://127.0.0.1:%d/v1" % brain.server_address[1], "model": "test-model", "timeout_s": 10,
                      "vision": False}}, open(os.path.join(S, "config.json"), "w"))
PROCS.append(subprocess.Popen([sys.executable, os.path.join(REPO, "server.py")], cwd=REPO, stdout=subprocess.DEVNULL,
                              stderr=subprocess.STDOUT, env=dict(os.environ, GENCENTER_CONFIG=os.path.join(S, "config.json"),
                                                                 GENCENTER_DATA=os.path.join(S, "data"))))
PRE = "[reference generation] "
SLUG, B1, B2 = ("INT. LUNAR APARTMENT - NIGHT.", PRE + "The target video pushes into <Subject 1> as <Subject 2> pours tea.",
                PRE + "The target video holds a close-up on <Subject 2>, who will not look up.")
LTX_OUT = "A woman pours tea at a steel table in a dim lunar apartment. The camera pushes in slowly."
H3_OUT = PRE + "Live-action, cinematic, the woman from the reference picture keeps her eyes on her cup."
SHOTS = os.environ.get("SHOTS_DIR")
try:
    end = time.time() + 30
    while time.time() < end:
        try: urllib.request.urlopen(URL + "api/health", timeout=2); break
        except Exception: time.sleep(0.2)
    seq = api("api/sequence", {"title": "Shots", "mode": "storyboard"})
    seq = api("api/sequence/op", {"id": seq["id"], "rev": seq["rev"], "op": "import_script",
                                  "text": "\n\n".join([SLUG, B1, B2, "She finally answers."])})
    film = [b for b in seq["beats"] if b["kind"] == "film"]
    seq = api("api/sequence/op", {"id": seq["id"], "rev": seq["rev"], "op": "update_slot",
                                  "slot_id": film[1]["slot_id"], "mode": "ref2v"})
    with sync_playwright() as pw:
        page = pw.chromium.launch().new_page(viewport={"width": 3440, "height": 1440})
        errors = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("request", lambda r: BODIES.append(json.loads(r.post_data or "{}")) if r.url.endswith("/api/guide/skill") else None)
        page.goto(URL + "#room=cutting&seq=" + seq["id"], wait_until="networkidle")
        print("an LTX shot: its own writer, a question, then the preview")
        page.click('[data-script-lane] [data-beat-id="%s"]' % film[0]["id"])
        page.wait_for_selector("#slotWriteBtn", state="visible", timeout=15000)
        check("the Cutting Room offers Write this shot, not Help me write this", page.is_hidden("#helperWriteBtn"))
        REPLIES[:] = ["QUESTION: How should the camera move?\nOPTIONS: Push in slowly | Static shot", "LENGTH: NONE\nPROMPT: " + LTX_OUT]
        page.click("#slotWriteBtn")
        page.wait_for_selector('#guideSkillOptions [data-option="Push in slowly"]', timeout=15000)
        b = BODIES[-1]
        check("the request is the LTX mode, from its own room, with the beat as the topic",
              (b.get("room"), b.get("mode"), b.get("topic")) == ("video", "ltx", B1), b)
        check("the beats either side go as context", (b.get("context") or {}).get("neighbours") == {"before": SLUG, "after": B2}, b)
        check("the brain got LTX's own writer", bool(ASKED) and ASKED[-1][0]["content"] == ltx.ENGINE["writers"]["ltx"]["prompt"])
        check("the Film Room Guide speaks", "film room guide" in page.inner_text("#guideSkill").lower(), page.inner_text("#guideSkill"))
        page.click('#guideSkillOptions [data-option="Push in slowly"]')
        page.wait_for_selector("#guideSkillUse", timeout=15000)
        check("the answer carries the same shot's context", (BODIES[-1].get("context") or {}).get("neighbours", {}).get("after") == B2)
        check("the preview shows the LTX prompt", LTX_OUT in page.inner_text("#guideSkill"))
        if SHOTS: page.screenshot(path=os.path.join(SHOTS, "write-this-shot-ltx.png"))
        page.click("#guideSkillUse")
        check("Use these fills the shot's prompt", page.input_value("#promptBox") == LTX_OUT, page.input_value("#promptBox"))
        page.wait_for_timeout(1500)
        saved = next(s for s in api("api/sequence?id=" + seq["id"])["slots"] if s["id"] == film[0]["slot_id"])
        check("...and the shot saves it", saved["values"].get("prompt") == LTX_OUT, saved["values"].get("prompt"))
        print("an H3 shot: H3's own writer, its own task prefix kept")
        page.click('[data-script-lane] [data-beat-id="%s"]' % film[1]["id"])
        page.wait_for_selector("#slotWriteBtn", state="visible", timeout=15000)
        REPLIES[:] = ["LENGTH: NONE\nPROMPT: " + H3_OUT]
        page.click("#slotWriteBtn")
        page.wait_for_selector("#guideSkillUse", timeout=15000)
        n = (BODIES[-1].get("context") or {}).get("neighbours") or {}
        check("the request is the H3 reference mode, neighbours both sides", BODIES[-1].get("mode") == "ref2v"
              and (n.get("before"), n.get("after")) == (B1, "She finally answers."), BODIES[-1])
        check("the topic is THIS shot's beat", BODIES[-1].get("topic") == B2, BODIES[-1].get("topic"))
        check("the previous shot's actual prompt goes too", n.get("previous") == LTX_OUT, n)
        check("a keep line names the first beat's subjects and props", B1 in (n.get("keep") or "")
              and "subjects and props" in (n.get("keep") or ""), n)
        check("the brain got H3's own writer", ASKED[-1][0]["content"] == minimax_h3.ENGINE["writers"]["ref2v"]["prompt"])
        check("the preview keeps H3's prefix and raises no problem",
              H3_OUT in page.inner_text("#guideSkill") and page.query_selector("#guideSkillProblems") is None)
        if SHOTS: page.screenshot(path=os.path.join(SHOTS, "write-this-shot-h3.png"))
        page.click('[data-script-lane] [data-beat-id="%s"]' % film[0]["id"])
        page.wait_for_timeout(500)
        check("the H3 preview does not follow the user to the LTX shot", page.is_hidden("#guideSkill"))
        check("no console or page errors", errors == [], errors[:5])
finally:
    for p in PROCS: p.terminate()
print("\n%s" % ("ALL PASS" if not FAILED else "FAILED: %d -- %s" % (len(FAILED), FAILED)))
sys.exit(1 if FAILED else 0)