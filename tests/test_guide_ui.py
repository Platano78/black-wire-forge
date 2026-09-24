"""Browser gate for the room guide panel: the Film Room Guide in the Cutting
Room (P1), every other room's guide, and the room context a generator room
sends with each turn (P1b/P1c); the song writer skill ("Write it for me") and the
Make-time confirm (P2).

Drives the real page in a real browser (Playwright) against its own
server.py subprocesses (scratch config + scratch data, random free ports),
its own fake ComfyUI lane, and an in-process fake OpenAI-compatible helper
that records every chat request. One server has the helper configured, one
has none (the no-brain state).

Set BWF_GUIDE_SHOTS=<dir> to also save screenshots (the Cutting Room at
1280x800 and 390 wide, brain and no-brain; every room's panel at 1280x800;
Music and 3D at 390 wide, brain and no-brain) into that directory.

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
                      "caps": ["image", "video", "audio"],
                      # the song and background-music roles, so both modes can run
                      # (the Make-time check only applies to a mode the lane can run)
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

COMPACT = read("guides/film/system-prompt.txt")
VERBOSE = read("guides/film/system-prompt-verbose.txt")
META = json.loads(read("guides/film/guide.json"))

def enter_cutting(page, url):
    page.goto(url + "#room=cutting", wait_until="networkidle", timeout=30000)
    page.wait_for_function("() => document.querySelector('#guideName') && "
                           "document.querySelector('#guideName').textContent.includes('Film Room Guide')",
                           timeout=15000)

ROOM_GUIDES = {r["id"]: r["guide"] for r in json.loads(read("rooms.json"))}
GUIDE_META = {gid: json.loads(read("guides/%s/guide.json" % gid)) for gid in set(ROOM_GUIDES.values())}

def size_note(gid):
    t = {v: (len(read("guides/%s/%s" % (gid, GUIDE_META[gid]["projections"][v]))) + 3) // 4
         for v in ("compact", "verbose")}
    return "Compact needs a model with about {:,} tokens of context; verbose about {:,}.".format(
        t["compact"] + 4096, t["verbose"] + 4096)

CHAT_BODIES = []   # every POST /api/guide/chat body the page sent
def record_chat(req):
    if req.url.endswith("/api/guide/chat") and req.method == "POST":
        CHAT_BODIES.append(json.loads(req.post_data or "{}"))

def enter_room(page, url, room_id):
    page.goto("about:blank")   # a hash-only goto would not reload the page
    page.goto(url + "#room=" + room_id, wait_until="networkidle", timeout=30000)
    page.wait_for_function("n => document.querySelector('#guideName') && "
                           "document.querySelector('#guideName').textContent === n",
                           arg=GUIDE_META[ROOM_GUIDES[room_id]]["name"], timeout=15000)

NO_PICTURE = "\n\n[No picture is attached to this message.]"

def text_of(page, sel):
    """The element's text, or "" when it does not exist (so a RED run fails a check, not the run)."""
    el = page.query_selector(sel)
    return el.inner_text() if el else ""

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
        page.on("request", record_chat)
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
        check("history: second request carries both turns in order, each user turn grounded", req["messages"][1:] == [
            {"role": "user", "content": "a lighthouse keeper's last night" + NO_PICTURE},
            {"role": "assistant", "content": '<img src=x onerror="window.__xss=1">Decide the ending first.'},
            {"role": "user", "content": "who is in it?" + NO_PICTURE}], req["messages"][1:])

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
        check("cutting: the page sent no context with any turn", CHAT_BODIES and
              not any("context" in b for b in CHAT_BODIES), CHAT_BODIES[-1:])
        check("size note: shown under the toggle", text_of(page, "#guideSizeNote") == size_note("film"),
              text_of(page, "#guideSizeNote"))
        page.close()

        print("Music: the Sound Room Guide, with the room's mode and primary fields sent as context")
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.on("request", record_chat)
        enter_room(page, url_brain, "music")
        check("music: the panel is visible", page.is_visible("#guidePanel"))
        check("music: greeting shown first", guide_msgs(page)[:1] == [GUIDE_META["sound"]["greeting"]], guide_msgs(page))
        check("music: size note", text_of(page, "#guideSizeNote") == size_note("sound"), text_of(page, "#guideSizeNote"))
        mode = page.evaluate("STATE.mode")
        check("music: A song with words is the selected mode", mode == "song", mode)
        page.fill("#promptBox", "warm pop, clear female vocals, singing")
        page.fill("#f_duration", "150")
        del CHAT_BODIES[:]
        HELPER.update(reply="Sung, then.", finish="stop")
        send(page, "a song about the last ferry home", 3)
        body = CHAT_BODIES[-1] if CHAT_BODIES else {}
        ctx = body.get("context") or {}
        check("music: the turn carries the selected mode", ctx.get("mode") == "song", body)
        check("music: and the primary field values, numbers as numbers",
              (ctx.get("fields") or {}).get("tags") == "warm pop, clear female vocals, singing"
              and (ctx.get("fields") or {}).get("duration") == 150, ctx)
        sent = HELPER["requests"][-1]["messages"][-1]["content"] if HELPER["requests"] else ""
        check("music: the helper sees the context line, with real labels",
              sent.startswith('[Current room: Music · mode: A song with words (song) · Style / genre: '
                              '"warm pop, clear female vocals, singing"') and "Duration (seconds): 150" in sent, sent)
        check("music: the log shows the user's own words, not the context line",
              guide_msgs(page)[1] == "a song about the last ferry home", guide_msgs(page))
        page.close()

        GOOD = "TAGS: warm pop, clear female vocals\nBPM: NONE\nKEY: NONE\nDURATION: 150\nTIMESIG: NONE\nLANGUAGE: NONE\nLYRICS:\n[Intro]\nhello sea\n\n[Verse]\na\n\n[Chorus]\nb\n\n[Verse]\nc\n\n[Chorus]\nd\n\n[Outro]\ne"
        BAD = "TAGS: warm pop\nDURATION: 150\nLYRICS:\n[Verse]\na\n\n[Chorus]\nb"
        SKILL_BODIES = []
        GEN_BODIES = []
        # ---------------- P2: the song writer skill + the Make-time confirm ----------------
        print("Music, song: Write it for me (the song writer skill)")
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.on("request", lambda req: (SKILL_BODIES.append(json.loads(req.post_data or "{}")) if req.url.endswith("/api/guide/skill") and req.method == "POST" else None, GEN_BODIES.append(json.loads(req.post_data or "{}")) if req.url.endswith("/api/generate") and req.method == "POST" else None))
        enter_room(page, url_brain, "music")
        check("skill: the Write button shows for a mode with a writer", page.is_visible("#guideWriteBtn"))
        check("skill: the old helper button is hidden for song", not page.is_visible("#helperWriteBtn"))
        page.fill("#guideInput", "a song about the sea")
        shot(page, "skill-write-button-1280x800")
        HELPER["reply"] = GOOD
        page.click("#guideWriteBtn")
        page.wait_for_selector("#guideSkillUse", timeout=15000)
        check("skill: the preview names the fields", "Style / genre" in text_of(page, "#guideSkill") and "warm pop, clear female vocals" in text_of(page, "#guideSkill"))
        check("skill: lyrics in a monospaced block", page.eval_on_selector("#guideSkill pre", "e => getComputedStyle(e).fontFamily").lower().find("mono") >= 0)
        check("skill: the request carried the room context", bool(SKILL_BODIES) and SKILL_BODIES[-1].get("mode") == "song" and SKILL_BODIES[-1].get("topic") == "a song about the sea" and (SKILL_BODIES[-1].get("context") or {}).get("mode") == "song", SKILL_BODIES[-1:])
        check("skill: what the brain was asked is there, collapsed", page.is_visible("#guideSkillSent") and page.eval_on_selector("#guideSkillSent", "e => !e.open"))
        page.click("#guideSkillSent summary")
        check("skill: the sent text is shown verbatim", "Request: a song about the sea" in text_of(page, "#guideSkillSent"))
        page.click("#guideSkillSent summary")
        shot(page, "skill-preview-1280x800")
        page.click("#guideSkillUse")
        check("skill: Use these fills the style", page.input_value("#promptBox") == "warm pop, clear female vocals", page.input_value("#promptBox"))
        check("skill: Use these fills the lyrics", page.input_value("#f_lyrics").startswith("[Intro]\nhello sea"), page.input_value("#f_lyrics"))
        check("skill: the preview closes", not page.is_visible("#guideSkill"))
        print("skill: the question path")
        HELPER["reply"] = "QUESTION: Sung, or instrumental?"
        page.fill("#guideInput", "music for my video about the sea")
        page.click("#guideWriteBtn")
        page.wait_for_selector("#guideSkillAnswer", timeout=15000)
        check("skill: the question is shown", "Sung, or instrumental?" in text_of(page, "#guideSkill"))
        shot(page, "skill-question-1280x800")
        HELPER["reply"] = GOOD
        page.fill("#guideSkillAnswer", "Sung")
        page.click("#guideSkillAnswerBtn")
        page.wait_for_selector("#guideSkillUse", timeout=15000)
        check("skill: the answer goes back with the same topic", SKILL_BODIES[-1].get("answer") == "Sung" and SKILL_BODIES[-1].get("topic") == "music for my video about the sea", SKILL_BODIES[-1])
        page.click("#guideSkillDismiss")
        check("skill: Dismiss closes it", not page.is_visible("#guideSkill"))
        print("skill: problems are shown plainly")
        HELPER["reply"] = BAD
        page.fill("#guideInput", "a song about the sea")
        page.click("#guideWriteBtn")
        page.wait_for_selector("#guideSkillProblems", timeout=15000)
        check("skill: both problems listed", page.eval_on_selector_all("#guideSkillProblems li", "els => els.length") == 2)
        shot(page, "skill-problems-1280x800")
        print("skill: 390 wide")
        HELPER["reply"] = GOOD
        page.fill("#guideInput", "a song about the sea")
        page.click("#guideWriteBtn")
        page.wait_for_selector("#guideSkillUse", timeout=15000)
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(300)
        check("skill: no horizontal scroll at 390 with the preview open", no_hscroll(page))
        page.locator("#guideSkill").scroll_into_view_if_needed()
        shot(page, "skill-preview-390")
        page.set_viewport_size({"width": 1280, "height": 800})
        page.click("#guideSkillDismiss")
        print("Make-time confirm: lyrics with no voice")
        page.fill("#promptBox", "warm acoustic pop, gentle drums")
        page.fill("#f_lyrics", "[Verse]\nSunlight on the water\n[Chorus]\nHold on")
        del GEN_BODIES[:]
        page.click("#makeBtn")
        page.wait_for_selector("#makeConfirm:not([hidden])", timeout=15000)
        check("confirm: the problems are shown", page.eval_on_selector_all("#makeConfirmList li", "els => els.length") == 2 and "voice" in text_of(page, "#makeConfirmList"))
        check("confirm: the first request had no confirm", len(GEN_BODIES) == 1 and "confirm" not in GEN_BODIES[0], GEN_BODIES)
        page.locator("#makeConfirm").scroll_into_view_if_needed()
        shot(page, "confirm-1280x800")
        page.click("#makeAnywayBtn")
        page.wait_for_timeout(800)
        check("confirm: Make anyway resends with confirm", len(GEN_BODIES) == 2 and GEN_BODIES[1].get("confirm") is True, GEN_BODIES)
        check("confirm: the box closes", not page.is_visible("#makeConfirm"))
        page.click("#makeBtn")
        page.wait_for_selector("#makeConfirm:not([hidden])", timeout=15000)
        page.click("#makeFixBtn")
        check("confirm: Fix it goes to the guide", page.evaluate("document.activeElement && document.activeElement.id") == "guideInput" and not page.is_visible("#makeConfirm"))
        print("skill: a mode with no writer keeps the old helper button")
        page.check('#enginePicker input[data-mode="music"]')
        page.wait_for_timeout(600)
        check("skill: no Write button for a mode without a writer", not page.is_visible("#guideWriteBtn"), page.evaluate("STATE.mode"))
        check("skill: the old helper button is back for it", page.is_visible("#helperWriteBtn"))
        page.close()
        print("no brain: no Write button, one line on how to add a helper")
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        enter_room(page, url_none, "music")
        check("no brain: no Write button", not page.is_visible("#guideWriteBtn"))
        check("no brain: the add-a-helper line for writing", text_of(page, "#guideWriteNoBrain") == "Add a helper to have the guide write the words for you.")
        page.close()

        print("every room: the right guide, screenshots at 1280x800 (brain)")
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        for room_id, gid in ROOM_GUIDES.items():
            enter_room(page, url_brain, room_id)
            check("%s: shows %s" % (room_id, GUIDE_META[gid]["name"]), page.is_visible("#guidePanel")
                  and page.inner_text("#guideName") == GUIDE_META[gid]["name"], page.inner_text("#guideName"))
            page.locator("#guidePanel").scroll_into_view_if_needed()
            shot(page, "brain-%s-1280x800" % room_id)
        page.close()
        for label, url in (("brain", url_brain), ("nobrain", url_none)):
            page = browser.new_page(viewport={"width": 390, "height": 844})
            for room_id in ("music", "3d"):
                enter_room(page, url, room_id)
                check("%s %s: no horizontal scroll at 390" % (label, room_id), no_hscroll(page))
                page.locator("#guidePanel").scroll_into_view_if_needed()
                shot(page, "%s-%s-390" % (label, room_id))
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
