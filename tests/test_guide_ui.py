"""Browser gate for the room guide panel: the Film Room Guide in the Cutting
Room (P1), every other room's guide, and the room context a generator room
sends with each turn (P1b/P1c); the song writer skill and the Make-time confirm (P2);
"Not right? Tell the guide" on a finished picture (P2b); one voice (P2c): "Help
me write this" / "Describe this picture" by the prompt box go to the room's
guide and answer in its panel, which sits at the top of the form.

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

def start_server(name, lane_port, with_helper, vision=None, jobs=None):
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
        if vision is not None:
            cfg["helper"]["vision"] = vision
    if jobs:
        os.makedirs(os.path.join(SCRATCH, "data_" + name), exist_ok=True)
        with open(os.path.join(SCRATCH, "data_" + name, "jobs.json"), "w") as f:
            json.dump(jobs, f)
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

def gradient_png(w, h):
    """A small RGB PNG (a colour gradient), so a result has a real picture to show."""
    import struct, zlib
    rows = b"".join(b"\x00" + bytes(v for x in range(w) for v in (x * 255 // w, y * 255 // h, 160)) for y in range(h))
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))

FIX_PROMPT = "a battle between Godzilla and MechaKing Ghidorah"
FIX_JOB = {"id": "fixjob1", "lane": "t", "lane_name": "Fake lane", "kind": "image", "mode": "t2i", "status": "done",
           "prompt": FIX_PROMPT, "negative": "", "cfg": 3.0, "width": 1328, "height": 1328, "steps": 20, "seed": 7,
           "created": time.time(), "started": time.time(), "updated": time.time(), "notes": [],
           "outputs": [{"filename": "fix.png", "subfolder": "", "type": "output", "media": "image"}]}

def show_fix_job(page, url):
    enter_room(page, url, "picture")
    page.wait_for_selector('#binBody tr[data-job="fixjob1"]', timeout=15000)
    page.click('#binBody tr[data-job="fixjob1"]')
    page.wait_for_selector("#monitorActions", timeout=15000)

def pick(page, mode):
    """Pick an engine: the picker is one summary line until opened (P2c)."""
    page.evaluate("() => { document.querySelector('#enginePickerDetails').open = true; }")
    page.check('#enginePicker input[data-mode="%s"]' % mode)

def shot(page, name):
    if SHOTS:
        os.makedirs(SHOTS, exist_ok=True)
        page.screenshot(path=os.path.join(SHOTS, name + ".png"), full_page=False)

try:
    lane = start_lane()
    with open(os.path.join(SCRATCH, "fake_lane", "outputs", "fix.png"), "wb") as f:
        f.write(gradient_png(96, 96))
    url_brain = start_server("brain", lane, True, vision=True, jobs=[FIX_JOB])
    url_none = start_server("nobrain", lane, False, jobs=[FIX_JOB])
    url_blind = start_server("blind", lane, True, vision=False, jobs=[FIX_JOB])
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
        box = page.eval_on_selector("#guideHeader", "e => { const r = e.getBoundingClientRect(); "
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
        check("compact: Clear conversation waits in the expanded panel", not page.is_visible("#guideClearBtn"))
        page.click("#guideExpandBtn")   # compact shows only the latest message; Clear is with the whole conversation
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
        print("Music, song: Help me write this -> the guide's song writer skill")
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.on("request", lambda req: (SKILL_BODIES.append(json.loads(req.post_data or "{}")) if req.url.endswith("/api/guide/skill") and req.method == "POST" else None, GEN_BODIES.append(json.loads(req.post_data or "{}")) if req.url.endswith("/api/generate") and req.method == "POST" else None))
        enter_room(page, url_brain, "music")
        check("skill: Help me write this shows for a mode with a writer", page.is_visible("#helperWriteBtn"))
        check("skill: the guide panel has no Write button of its own", page.query_selector("#guideWriteBtn") is None)
        check("skill: the button says it goes to the guide", text_of(page, "#helperCue") == "→ " + GUIDE_META["sound"]["name"],
              text_of(page, "#helperCue"))
        page.fill("#promptBox", "a song about the sea")
        shot(page, "skill-write-button-1280x800")
        HELPER["reply"] = GOOD
        page.click("#helperWriteBtn")
        page.wait_for_selector("#guideSkillUse", timeout=15000)
        check("skill: the reply is in the guide panel, in the guide's voice", page.eval_on_selector(
            "#guideSkill", "e => !!e.closest('#guidePanel')") and text_of(page, "#guideSkill .guide-who").lower() == GUIDE_META["sound"]["name"].lower()
              and "Here is what I wrote." in text_of(page, "#guideSkill"))
        check("skill: the preview names the fields", "Style / genre" in text_of(page, "#guideSkill") and "warm pop, clear female vocals" in text_of(page, "#guideSkill"))
        check("skill: lyrics in a monospaced block", page.eval_on_selector("#guideSkill pre", "e => getComputedStyle(e).fontFamily").lower().find("mono") >= 0)
        check("skill: the request carried the room context", bool(SKILL_BODIES) and SKILL_BODIES[-1].get("mode") == "song" and SKILL_BODIES[-1].get("topic") == "a song about the sea" and (SKILL_BODIES[-1].get("context") or {}).get("mode") == "song", SKILL_BODIES[-1:])
        check("skill: no 'what the brain was asked' disclosure -- the user sees only the guide's voice",
              page.query_selector("#guideSkillSent") is None)
        shot(page, "skill-preview-1280x800")
        page.click("#guideSkillUse")
        check("skill: Use these fills the style", page.input_value("#promptBox") == "warm pop, clear female vocals", page.input_value("#promptBox"))
        check("skill: Use these fills the lyrics", page.input_value("#f_lyrics").startswith("[Intro]\nhello sea"), page.input_value("#f_lyrics"))
        check("skill: the preview closes", not page.is_visible("#guideSkill"))
        print("skill: the question path")
        HELPER["reply"] = "QUESTION: Sung, or instrumental?"
        page.fill("#promptBox", "music for my video about the sea")
        page.click("#helperWriteBtn")
        page.wait_for_selector("#guideSkillRestart", timeout=15000)
        check("skill: the question is shown", "Sung, or instrumental?" in text_of(page, "#guideSkill"))
        shot(page, "skill-question-1280x800")
        HELPER["reply"] = GOOD
        check("skill: the panel's input is the answer box, the bubble has none", page.query_selector("#guideSkillAnswer") is None
              and page.get_attribute("#guideInput", "placeholder").startswith("Your answer"), page.get_attribute("#guideInput", "placeholder"))
        page.fill("#guideInput", "Sung")
        page.press("#guideInput", "Enter")
        page.wait_for_selector("#guideSkillUse", timeout=15000)
        check("skill: the answer goes back, with its question, and the same topic", SKILL_BODIES[-1].get("answers") == [{"q": "Sung, or instrumental?", "a": "Sung"}] and SKILL_BODIES[-1].get("topic") == "music for my video about the sea", SKILL_BODIES[-1])
        page.click("#guideSkillDismiss")
        check("skill: Dismiss closes it", not page.is_visible("#guideSkill"))
        print("skill: problems are shown plainly")
        HELPER["reply"] = BAD
        page.fill("#promptBox", "a song about the sea")
        page.click("#helperWriteBtn")
        page.wait_for_selector("#guideSkillProblems", timeout=15000)
        check("skill: both problems listed", page.eval_on_selector_all("#guideSkillProblems li", "els => els.length") == 2)
        shot(page, "skill-problems-1280x800")
        print("skill: 390 wide")
        HELPER["reply"] = GOOD
        page.fill("#promptBox", "a song about the sea")
        page.click("#helperWriteBtn")
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
        print("skill: a mode with no pack writer still writes through the guide (the generic writer)")
        pick(page, "music")
        page.wait_for_timeout(600)
        check("skill: Help me write this is there for it too", page.is_visible("#helperWriteBtn"), page.evaluate("STATE.mode"))
        HELPER["reply"] = "PROMPT: calm ambient pads, soft piano, no drums"
        del SKILL_BODIES[:]
        page.fill("#promptBox", "background music for a rainy cafe")
        page.click("#helperWriteBtn")
        page.wait_for_selector("#guideSkillUse", timeout=15000)
        check("skill: it went to /api/guide/skill for that mode", SKILL_BODIES and SKILL_BODIES[-1].get("mode") == "music"
              and SKILL_BODIES[-1].get("topic") == "background music for a rainy cafe", SKILL_BODIES[-1:])
        page.click("#guideSkillUse")
        check("skill: Use these fills its prompt box", page.input_value("#promptBox") == "calm ambient pads, soft piano, no drums",
              page.input_value("#promptBox"))
        page.close()
        print("no brain: no Write button, one line on how to add a helper")
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        enter_room(page, url_none, "music")
        check("no brain: no Help me write this", not page.is_visible("#helperWriteBtn") and page.query_selector("#guideWriteBtn") is None)
        check("no brain: the add-a-helper line for writing", text_of(page, "#guideWriteNoBrain") == "Add a helper to have the guide write the words for you.")
        page.close()

        # ---------------- P2b: "Not right? Tell the guide" ----------------
        REVISE_BODIES = []
        FIXED = "A battle between exactly two giant monsters: a grey kaiju and a golden three-headed mechanical dragon."
        REROLL = ("QUESTION:\nDIAGNOSIS: The model does not know \"MechaKing Ghidorah\" by name, so it drew an extra monster.\n"
                  "FIX: reroll\nPROMPT: " + FIXED + "\nNOTE: Pinned the count to exactly two.\nTWEAK: A night sky would add contrast.")
        EDIT = ("QUESTION:\nDIAGNOSIS: Only the dragon's colour is off.\nFIX: edit\nPROMPT: Make the dragon gold and keep "
                "everything else.\nNOTE: Only one thing changes, so an edit keeps the rest.\nTWEAK:")
        def record_revise(req):
            if req.url.endswith("/api/guide/revise") and req.method == "POST":
                REVISE_BODIES.append(json.loads(req.post_data or "{}"))
        print("Picture: Not right? Tell the guide (a helper that can see)")
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.on("request", record_revise)
        page.on("request", record_chat)
        show_fix_job(page, url_brain)
        check("revise: the button is on the finished t2i result", page.is_visible("#notRightBtn")
              and text_of(page, "#notRightBtn") == "Not right? Tell the guide")
        page.locator("#monitorActions").scroll_into_view_if_needed()
        shot(page, "revise-button-1280x800")
        page.click("#notRightBtn")
        page.wait_for_selector("#guideChip:not([hidden])", timeout=15000)
        check("revise: the chip shows the result's thumbnail", page.eval_on_selector(
            "#guideChip img", "e => e.getAttribute('src')").find("filename=fix.png") >= 0)
        check("revise: the chip says attached, with a remove button", "attached" in text_of(page, "#guideChip")
              and page.is_visible("#guideChipRemove"))
        check("revise: no can't-see note for a helper that can see", page.query_selector("#guideChipNote") is None)
        check("revise: the input is focused and asks what's wrong",
              page.evaluate("document.activeElement && document.activeElement.id") == "guideInput"
              and page.get_attribute("#guideInput", "placeholder") == "What's wrong with it?")
        shot(page, "revise-chip-1280x800")
        print("revise: the reroll path")
        HELPER.update(reply=REROLL, finish="stop")
        HELPER["requests"].clear()
        del CHAT_BODIES[:]
        page.fill("#guideInput", "I asked for Godzilla fighting MechaKing Ghidorah and got two Godzillas and some other monster")
        page.press("#guideInput", "Enter")
        page.wait_for_selector("#guideReviseUse", timeout=15000)
        body = REVISE_BODIES[-1] if REVISE_BODIES else {}
        check("revise: Send went to /api/guide/revise with the job and the complaint", body.get("job_id") == "fixjob1"
              and body.get("output") == 0 and body.get("room") == "picture"
              and body.get("complaint", "").startswith("I asked for Godzilla"), body)
        check("revise: nothing went to the chat", CHAT_BODIES == [], CHAT_BODIES[-1:])
        last = HELPER["requests"][-1]["messages"][-1]["content"] if HELPER["requests"] else ""
        check("revise: the helper got the picture on the user message", isinstance(last, list)
              and [p["type"] for p in last] == ["text", "image_url"], type(last))
        check("revise: the diagnosis is shown", "does not know" in text_of(page, "#guideReviseDiagnosis"))
        check("revise: the revised prompt is monospaced", text_of(page, "#guideRevisePrompt") == FIXED and page.eval_on_selector(
            "#guideRevisePrompt", "e => getComputedStyle(e).fontFamily").lower().find("mono") >= 0)
        check("revise: the note and the tweak are shown", "Pinned the count" in text_of(page, "#guideSkill")
              and "night sky" in text_of(page, "#guideSkill"))
        check("revise: no 'what the brain was asked' disclosure -- the user sees only the guide's voice",
              page.query_selector("#guideSkillSent") is None)
        page.locator("#guideSkill").scroll_into_view_if_needed()
        shot(page, "revise-reroll-1280x800")
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(300)
        check("revise: no horizontal scroll at 390 with the reply open", no_hscroll(page))
        page.locator("#guideSkill").scroll_into_view_if_needed()
        shot(page, "revise-reply-390")
        page.set_viewport_size({"width": 1280, "height": 800})
        page.fill("#promptBox", "something else")
        page.click("#guideReviseUse")
        check("revise: Use this prompt fills the prompt", page.input_value("#promptBox") == FIXED, page.input_value("#promptBox"))
        check("revise: and stays in t2i", page.evaluate("STATE.mode") == "t2i")
        check("revise: the reply and the chip close", not page.is_visible("#guideSkill") and not page.is_visible("#guideChip"))
        check("revise: the input goes back to asking the guide",
              page.get_attribute("#guideInput", "placeholder") == "Ask the guide. Enter sends, Shift+Enter for a new line.")
        print("revise: the question path")
        page.click("#notRightBtn")
        page.wait_for_selector("#guideChip:not([hidden])", timeout=15000)
        HELPER["reply"] = "QUESTION: What looks wrong to you: the monsters, the city, or the light?"
        page.fill("#guideInput", "it's just not right")
        page.press("#guideInput", "Enter")
        page.wait_for_selector("#guideReviseAnswer", timeout=15000)
        check("revise: the question is shown", "the monsters, the city" in text_of(page, "#guideSkill"))
        HELPER["reply"] = REROLL
        page.fill("#guideReviseAnswer", "the second monster")
        page.click("#guideReviseAnswerBtn")
        page.wait_for_selector("#guideReviseUse", timeout=15000)
        check("revise: the answer goes back with the same complaint", REVISE_BODIES[-1].get("answer") == "the second monster"
              and REVISE_BODIES[-1].get("complaint") == "it's just not right", REVISE_BODIES[-1])
        page.click("#guideReviseDismiss")
        print("revise: the edit path")
        HELPER["reply"] = EDIT
        page.fill("#guideInput", "the dragon should be gold")
        page.press("#guideInput", "Enter")
        page.wait_for_selector("#guideReviseEdit", timeout=15000)
        check("revise: the edit instruction is shown", "edit instruction" in text_of(page, "#guideSkill").lower()
              and text_of(page, "#guideRevisePrompt") == "Make the dragon gold and keep everything else.")
        page.locator("#guideSkill").scroll_into_view_if_needed()
        shot(page, "revise-edit-1280x800")
        page.click("#guideReviseEdit")
        page.wait_for_function("() => STATE.mode === 'edit'", timeout=15000)
        page.wait_for_timeout(300)
        check("revise: Try it in Edit switches to edit and fills its prompt",
              page.input_value("#promptBox") == "Make the dragon gold and keep everything else.", page.input_value("#promptBox"))
        # P3d: the result itself becomes the edit's first picture (tests/test_picture_ui.py).
        page.wait_for_function("() => (STATE.uploads.ref_images || []).length === 1", timeout=15000)
        check("revise: and the result is the edit's picture 1",
              "picture 1 under Pictures to work from" in text_of(page, "#inspectorMsg"), text_of(page, "#inspectorMsg"))
        print("revise: the remove button detaches it, and Send talks to the guide again")
        pick(page, "t2i")
        page.wait_for_timeout(600)
        page.click("#notRightBtn")
        page.wait_for_selector("#guideChip:not([hidden])", timeout=15000)
        page.click("#guideChipRemove")
        check("revise: the chip is gone", not page.is_visible("#guideChip"))
        n_revise = len(REVISE_BODIES)
        HELPER.update(reply="Ask me anything.")
        send(page, "hello", 2)
        check("revise: Send went to the chat, not the fixer", len(REVISE_BODIES) == n_revise and CHAT_BODIES
              and "pictures" not in CHAT_BODIES[-1], CHAT_BODIES[-1:])
        print("revise: a reply renders as text, never as HTML")
        page.click("#notRightBtn")
        page.wait_for_selector("#guideChip:not([hidden])", timeout=15000)
        HELPER["reply"] = REROLL.replace("DIAGNOSIS: ", 'DIAGNOSIS: <img src=x onerror="window.__xss2=1">')
        page.fill("#guideInput", "wrong monster")
        page.press("#guideInput", "Enter")
        page.wait_for_selector("#guideReviseUse", timeout=15000)
        check("revise: no img element created in the reply", page.eval_on_selector_all("#guideSkill img", "els => els.length") == 0
              and page.evaluate("() => window.__xss2 === undefined"))
        page.close()

        print("revise: a helper that cannot see pictures")
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        show_fix_job(page, url_blind)
        page.click("#notRightBtn")
        page.wait_for_selector("#guideChip:not([hidden])", timeout=15000)
        check("revise: the chip says the helper can't see pictures",
              text_of(page, "#guideChipNote") == "this helper can't see pictures: describe what's wrong in words")
        HELPER["requests"].clear()
        HELPER["reply"] = "QUESTION: I can't see the picture, so tell me: what looks wrong?"
        page.fill("#guideInput", "is anything wrong?")
        page.press("#guideInput", "Enter")
        page.wait_for_selector("#guideReviseAnswer", timeout=15000)
        last = HELPER["requests"][-1]["messages"][-1]["content"] if HELPER["requests"] else None
        check("revise: the call still went, with no picture and the truth",
              isinstance(last, str) and last.endswith("[The user attached a picture, but this helper cannot see pictures.]"), last)
        check("revise: the reply says it only has the words", "only has your words" in text_of(page, "#guideSkill"))
        page.locator("#guideChip").scroll_into_view_if_needed()
        shot(page, "revise-vision-false-1280x800")
        page.close()

        print("revise: no brain: the button still opens the guide, with how to add one")
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        show_fix_job(page, url_none)
        check("revise: no brain: the button is there", page.is_visible("#notRightBtn"))
        page.click("#notRightBtn")
        page.wait_for_selector("#guideReviseNoBrain:not([hidden])", timeout=15000)
        check("revise: no brain: the panel is open with the guidance", page.eval_on_selector("#guidePanel", "e => e.dataset.expanded === 'true'")
              and page.is_visible("#guideNoBrainList") and "config.json" in text_of(page, "#guideAddBrain"))
        check("revise: no brain: the add-a-helper line for fixing",
              text_of(page, "#guideReviseNoBrain") == "Add a helper to have the guide look at this result and fix its prompt.")
        check("revise: no brain: no chip, no input box", not page.is_visible("#guideChip") and not page.is_visible("#guideInput"))
        page.close()

        # ---------------- P2c: one voice ----------------
        IN_VIEW = ("sel => { const e = document.querySelector(sel); if(!e || !e.offsetParent) return false;"
                   " const r = e.getBoundingClientRect(), c = document.querySelector('#inspector').getBoundingClientRect();"
                   " return r.top >= Math.max(0, c.top) && r.bottom <= Math.min(innerHeight, c.bottom); }")
        # The owner's order: the prompt box, Help me write this, the guide right under it, then the rest.
        ORDER = ("() => { const top = s => { const e = document.querySelector(s); return e && e.offsetParent ? e.getBoundingClientRect().top : null; };"
                 " const g = document.querySelector('#guidePanel'), f = document.querySelector('#makeBtn');"
                 " const below = !!(g.compareDocumentPosition(f) & Node.DOCUMENT_POSITION_FOLLOWING)"
                 "   && !!(g.compareDocumentPosition(document.querySelector('#recipeDetails')) & Node.DOCUMENT_POSITION_FOLLOWING);"
                 " const right = g.previousElementSibling && g.previousElementSibling.id === 'promptWrap';"
                 " if(top('#promptBox') === null) return below && right;"
                 # the button is hidden with no helper: the guide takes its place
                 " const btn = top('#helperWriteBtn');"
                 " return below && right && (btn === null ? top('#promptBox') < top('#guideHeader')"
                 "   : top('#promptBox') < btn && btn < top('#guideHeader')); }")
        sys.path.insert(0, REPO)
        import engines
        PICTURE_COMPACT = read("guides/picture/" + GUIDE_META["picture"]["projections"]["compact"])
        ALL_URLS = []
        # Every fetch the page makes, from its own side too (not only the network).
        SPY = ("(() => { const f = window.fetch; window.__fetched = [];"
               " window.fetch = function(u){ window.__fetched.push(String(u && u.url || u)); return f.apply(this, arguments); }; })()")
        PAGE_FETCHES = []
        def voice_page(width=1280, height=800):
            p = browser.new_page(viewport={"width": width, "height": height})
            p.add_init_script(SPY)
            p.on("request", lambda req: ALL_URLS.append(req.url))
            p.on("request", lambda req: SKILL_BODIES.append(json.loads(req.post_data or "{}"))
                 if req.url.endswith("/api/guide/skill") and req.method == "POST" else None)
            return p
        def keep_fetches(p):
            PAGE_FETCHES.extend(p.evaluate("() => window.__fetched || []"))
        def last_user(): return HELPER["requests"][-1]["messages"][-1]["content"] if HELPER["requests"] else None

        print("P2c Picture, t2i (its pack writer since P3d): Help me write this -> the Picture Guide")
        page = voice_page()
        enter_room(page, url_brain, "picture")
        check("one voice: t2i has its pack writer (P3d)",
              page.evaluate("() => currentMode().writer.label") == engines.writer("image", "t2i")["label"])
        check("one voice: Help me write this is by the prompt box, cued to the guide", page.is_visible("#helperWriteBtn")
              and text_of(page, "#helperCue") == "→ " + GUIDE_META["picture"]["name"])
        page.fill("#promptBox", "a lighthouse")
        del SKILL_BODIES[:]
        HELPER["requests"].clear()
        HELPER.update(reply="PROMPT: A lighthouse at dusk on a rocky point, warm light in the lamp room.", finish="stop", delay=1.5)
        page.click("#helperWriteBtn")
        page.wait_for_selector("#helperSpinner:not([hidden])", timeout=5000)
        writing = GUIDE_META["picture"]["name"] + " is writing…"
        check("one voice: while it writes, the guide says so by name", text_of(page, "#helperSpinner") == writing
              and text_of(page, "#guideThinking") == writing, (text_of(page, "#helperSpinner"), text_of(page, "#guideThinking")))
        page.wait_for_selector("#guideSkillUse", timeout=15000)
        HELPER["delay"] = 0
        body = SKILL_BODIES[-1] if SKILL_BODIES else {}
        check("t2i: it went to /api/guide/skill with the prompt box's words", body.get("room") == "picture"
              and body.get("mode") == "t2i" and body.get("topic") == "a lighthouse"
              and (body.get("context") or {}).get("mode") == "t2i" and "pictures" not in body, body)
        system = HELPER["requests"][-1]["messages"][0]["content"] if HELPER["requests"] else ""
        check("t2i: the brain got t2i's own writer prompt (P3d; the generic writer: tests/test_generic_writer.py)",
              system == engines.writer("image", "t2i")["prompt"])
        check("t2i: the reply is a guide message in the guide panel", page.eval_on_selector(
            "#guideSkill", "e => !!e.closest('#guidePanel')") and text_of(page, "#guideSkill .guide-who").lower() == GUIDE_META["picture"]["name"].lower())
        check("t2i: and it is in view", page.evaluate(
            "() => { const r = document.querySelector('#guideSkillUse').getBoundingClientRect(); return r.top >= 0 && r.bottom <= innerHeight; }"))
        shot(page, "p2c-picture-help-1280x800")
        page.click("#guideSkillUse")
        check("t2i: Use these fills the prompt box",
              page.input_value("#promptBox") == "A lighthouse at dusk on a rocky point, warm light in the lamp room.",
              page.input_value("#promptBox"))
        print("P2c: a reply with no prompt comes back as the server's sentence, verbatim")
        HELPER["reply"] = "Sure! Here you go."
        page.click("#helperWriteBtn")
        page.wait_for_selector("#guideError:not([hidden])", timeout=15000)
        check("one voice: the server's error sentence, unchanged",
              text_of(page, "#guideError") == "The writer's answer didn't come back in the expected shape.", text_of(page, "#guideError"))
        keep_fetches(page)
        page.close()

        print("P2c Picture: Describe this picture -> the guide, with the picture")
        page = voice_page()
        show_fix_job(page, url_brain)
        page.wait_for_selector("#helperDescribeBtn:not([hidden])", timeout=15000)
        HELPER.update(reply="PROMPT: A soft colour gradient from green to violet.")
        HELPER["requests"].clear()
        page.click("#helperDescribeBtn")
        page.wait_for_selector("#guideSkillUse", timeout=15000)
        body = SKILL_BODIES[-1] if SKILL_BODIES else {}
        check("describe: it went to /api/guide/skill with the result as the picture",
              body.get("pictures") == [{"job_id": "fixjob1", "output": 0}] and body.get("mode") == "t2i", body)
        content = last_user()
        check("describe: the helper that can see got the picture", isinstance(content, list)
              and [x["type"] for x in content] == ["text", "image_url"], type(content))
        page.click("#guideSkillUse")
        check("describe: Use these fills the prompt box", page.input_value("#promptBox") == "A soft colour gradient from green to violet.",
              page.input_value("#promptBox"))
        print("P2c Picture: Not right? opens and focuses this same panel")
        page.click("#notRightBtn")
        page.wait_for_selector("#guideChip:not([hidden])", timeout=15000)
        check("not right: the same panel, input focused and in view",
              page.evaluate("document.activeElement && document.activeElement.id") == "guideInput"
              and page.evaluate("() => { const r = document.querySelector('#guideInput').getBoundingClientRect(); "
                                "return r.top >= 0 && r.bottom <= innerHeight; }"))
        shot(page, "p2c-picture-not-right-1280x800")
        keep_fetches(page)
        page.close()

        print("P2c Picture: Describe this picture with a helper that cannot see")
        page = voice_page()
        show_fix_job(page, url_blind)
        page.wait_for_selector("#helperDescribeBtn:not([hidden])", timeout=15000)
        HELPER.update(reply="QUESTION: I can't see the picture, so tell me what it shows.")
        HELPER["requests"].clear()
        page.click("#helperDescribeBtn")
        page.wait_for_selector("#guideSkillRestart", timeout=15000)
        content = last_user()
        check("blind describe: text only, with the truth", isinstance(content, str)
              and content.endswith("[The user attached a picture, but this helper cannot see pictures.]"), content)
        check("blind describe: the reply says it only has the words", "only has your words" in text_of(page, "#guideSkill"))
        keep_fetches(page)
        page.close()

        print("P2c: the expand state is remembered")
        page = voice_page()
        enter_room(page, url_brain, "music")
        expanded = lambda: page.eval_on_selector("#guidePanel", "e => e.dataset.expanded")
        check("expand: compact by default, only the latest message shown", expanded() == "false"
              and page.eval_on_selector_all("#guideLog .guide-msg", "els => els.filter(e => e.offsetParent).length") == 1)
        page.click("#guideExpandBtn")
        check("expand: expanded", expanded() == "true" and page.get_attribute("#guideExpandBtn", "aria-expanded") == "true")
        check("expand: the conversation is capped at 40% of the viewport, scrolling inside",
              page.eval_on_selector("#guideLog", "e => getComputedStyle(e).maxHeight === (innerHeight * 0.4) + 'px' "
                                    "&& getComputedStyle(e).overflowY === 'auto'"))
        page.reload(wait_until="networkidle")
        page.wait_for_function("() => document.querySelector('#guideName').textContent.length > 8", timeout=15000)
        check("expand: still expanded after a reload", expanded() == "true")
        page.click("#guideExpandBtn")
        page.reload(wait_until="networkidle")
        page.wait_for_function("() => document.querySelector('#guideName').textContent.length > 8", timeout=15000)
        check("expand: and compact again after collapsing and reloading", expanded() == "false")
        keep_fetches(page)
        page.close()

        # ---------------- P2c amendment: the write conversation ----------------
        def wait_question(page, text):
            page.wait_for_function("t => { const b = document.querySelector('#guideSkill'); return b && !b.hidden "
                                   "&& b.textContent.includes(t) && document.querySelector('#guideSkillRestart'); }",
                                   arg=text, timeout=15000)
        def user_msg(i): return HELPER["requests"][i]["messages"][-1]["content"]
        print("P2c write conversation (Picture t2i): a question with options, then another, then the fields")
        page = voice_page()
        enter_room(page, url_brain, "picture")
        del SKILL_BODIES[:]
        HELPER["requests"].clear()
        HELPER["replies"] = ["QUESTION: How many kites?\nOPTIONS: One | Two | A whole sky of them",
                             "QUESTION: Where are they flying?",
                             "WIDTH: 1024\nHEIGHT: 99999\nPROMPT: two red kites over a grey sea",
                             "WIDTH: 1024\nHEIGHT: 99999\nPROMPT: two red kites over a grey sea"]
        page.fill("#promptBox", "kites")
        page.click("#helperWriteBtn")
        wait_question(page, "How many kites?")
        chips = page.eval_on_selector_all("#guideSkillOptions .skill-chip", "els => els.map(e => e.textContent)")
        check("write: the options are chips, and there is still a box for any answer",
              chips == ["One", "Two", "A whole sky of them"] and page.query_selector("#guideSkillAnswer") is None
              and page.get_attribute("#guideInput", "placeholder") == "Your answer… (or pick one above)", chips)
        check("write: the question and its chips are in view as it arrives", page.evaluate(IN_VIEW, "#guideSkill .guide-text")
              and page.evaluate(IN_VIEW, "#guideSkillOptions"))
        check("write: the guide sits right under Help me write this, the prompt above", page.evaluate(ORDER))
        check("write: Start over is offered", page.is_visible("#guideSkillRestart"))
        shot(page, "p2c-write-question-1280x800")
        page.click('#guideSkillOptions [data-option="Two"]')
        wait_question(page, "Where are they flying?")
        shot(page, "p2c-write-after-chip-1280x800")
        check("write: clicking a chip sends that answer, with its question",
              SKILL_BODIES[-1].get("answers") == [{"q": "How many kites?", "a": "Two"}]
              and SKILL_BODIES[-1].get("topic") == "kites", SKILL_BODIES[-1:])
        check("write: the conversation shows in the guide's log", "How many kites?" in guide_msgs(page) and "Two" in guide_msgs(page)
              and "kites" in guide_msgs(page), guide_msgs(page))
        print("write: a reload picks the write up where it was")
        page.reload(wait_until="networkidle")
        wait_question(page, "Where are they flying?")
        check("write: after a reload the waiting question is back, once", guide_msgs(page).count("Where are they flying?") == 0
              and "Where are they flying?" in text_of(page, "#guideSkill"), guide_msgs(page))
        page.fill("#guideInput", "over a grey sea")
        page.press("#guideInput", "Enter")
        page.wait_for_selector("#guideSkillUse", timeout=15000)
        check("write: the answers go back in order, rebuilt after the reload",
              SKILL_BODIES[-1].get("answers") == [{"q": "How many kites?", "a": "Two"},
                                                  {"q": "Where are they flying?", "a": "over a grey sea"}], SKILL_BODIES[-1:])
        check("write: the brain saw both Q/A pairs in order",
              "Q1: How many kites?\nA1: Two\nQ2: Where are they flying?\nA2: over a grey sea" in user_msg(2), user_msg(2))
        check("write: a bad setting is named, not used", "Height" in text_of(page, "#guideSkillProblems")
              and "99999" in text_of(page, "#guideSkillProblems"), text_of(page, "#guideSkill"))
        shot(page, "p2c-write-preview-1280x800")
        page.evaluate("() => { document.querySelector('#inspector').scrollTop = 1e6; window.scrollTo(0, 1e6); }")
        page.click("#guideSkillUse")
        page.wait_for_timeout(300)
        check("write: Use these brings the prompt box back into view", page.evaluate(IN_VIEW, "#promptBox"))
        check("write: Use these fills the prompt and the setting it wrote", page.input_value("#promptBox")
              == "two red kites over a grey sea" and page.input_value('#inspector [data-field-id="width"]') == "1024",
              (page.input_value("#promptBox"), page.input_value('#inspector [data-field-id="width"]')))
        print("write: after 4 answers the guide writes, and names its defaults")
        del SKILL_BODIES[:]
        HELPER["requests"].clear()
        HELPER["replies"] = ["QUESTION: Q one?\nOPTIONS: yes | no", "QUESTION: Q two?", "QUESTION: Q three?", "QUESTION: Q four?",
                             "NOTE: I chose daylight and a square frame.\nPROMPT: a kite on a beach"]
        page.fill("#promptBox", "a kite")
        page.click("#helperWriteBtn")
        for q, a in (("Q one?", None), ("Q two?", "b"), ("Q three?", "c"), ("Q four?", "d")):
            wait_question(page, q)
            if a is None:
                page.click('#guideSkillOptions [data-option="yes"]')
            elif q == "Q three?":
                page.fill("#guideInput", a)   # the panel's own input answers a waiting question too
                page.press("#guideInput", "Enter")
            else:
                page.fill("#guideInput", a)
                page.click("#guideSendBtn")
        page.wait_for_selector("#guideSkillUse", timeout=15000)
        check("write: the 5th call carried 4 answers and the write-now line", len(SKILL_BODIES) == 5
              and len(SKILL_BODIES[-1].get("answers") or []) == 4 and "No more questions: write it now" in user_msg(4),
              [len(b.get("answers") or []) for b in SKILL_BODIES])
        check("write: the typed answer in the panel went to the write, not the chat",
              SKILL_BODIES[3].get("answers", [{}])[-1].get("a") == "c", SKILL_BODIES[3:4])
        check("write: the preview shows the NOTE", text_of(page, "#guideSkillNote") == "I chose daylight and a square frame.")
        shot(page, "p2c-write-capped-note-1280x800")
        print("write: Start over clears the write conversation")
        HELPER["replies"] = ["QUESTION: Which beach?"]
        page.fill("#promptBox", "a kite at the beach")
        page.click("#helperWriteBtn")
        wait_question(page, "Which beach?")
        page.click("#guideSkillRestart")
        check("write: Start over closes it and takes its turns out of the log", not page.is_visible("#guideSkill")
              and "a kite at the beach" not in guide_msgs(page))
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(800)
        check("write: and it stays gone after a reload", not page.is_visible("#guideSkill")
              and "Which beach?" not in text_of(page, "#guidePanel"))
        keep_fetches(page)
        page.close()

        print("every room: the right guide, the placement floors, screenshots on entry at 1280x800 (brain)")
        page = voice_page()
        for room_id, gid in ROOM_GUIDES.items():
            enter_room(page, url_brain, room_id)
            page.wait_for_timeout(1500)   # the lane poll settles any room note first
            check("%s: shows %s" % (room_id, GUIDE_META[gid]["name"]), page.is_visible("#guidePanel")
                  and page.inner_text("#guideName") == GUIDE_META[gid]["name"], page.inner_text("#guideName"))
            check("%s: the guide header is in view on entry" % room_id, page.evaluate(IN_VIEW, "#guideHeader"))
            if page.is_visible("#promptWrap"):
                check("%s: the prompt box and Help me write this are in view on entry" % room_id,
                      page.evaluate(IN_VIEW, "#promptBox") and page.evaluate(IN_VIEW, "#helperWriteBtn"))
            if room_id != "cutting" and page.is_visible("#makeBtn"):   # a room with nothing installed has no form
                check("%s: prompt box, Help me write this, the guide right under it, then the rest of the form" % room_id,
                      page.evaluate(ORDER))
            shot(page, "brain-%s-1280x800" % room_id)
        keep_fetches(page)
        page.close()
        for label, url in (("brain", url_brain), ("nobrain", url_none)):
            page = voice_page(390, 844)
            for room_id in ("music", "3d"):
                enter_room(page, url, room_id)
                page.wait_for_timeout(600)
                check("%s %s: no horizontal scroll at 390" % (label, room_id), no_hscroll(page))
                check("%s %s: the same order at 390" % (label, room_id), page.evaluate(ORDER))
                if label == "nobrain":
                    check("nobrain %s: the top slot has the guidance and the add-a-helper line, no Help me write this" % room_id,
                          page.is_visible("#guideNoBrain") and page.is_visible("#guideAddBrain")
                          and not page.is_visible("#helperWriteBtn"))
                page.locator("#guidePanel").scroll_into_view_if_needed()
                shot(page, "%s-%s-390" % (label, room_id))
            keep_fetches(page)
            page.close()
        print("P2c: the page never calls /api/helper")
        check("no /api/helper request on the network from any P2c page", ALL_URLS
              and not any(u.split("?")[0].endswith("/api/helper") for u in ALL_URLS), [u for u in ALL_URLS if "helper" in u])
        check("no fetch of /api/helper from the page itself", PAGE_FETCHES
              and not any(u.split("?")[0].endswith("/api/helper") for u in PAGE_FETCHES))
        check("index.html names no /api/helper call at all", "/api/helper" not in read("index.html"))
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
