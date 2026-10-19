"""The home view: what do you want to make?

Pins, in a real browser against a real server.py + a fake ComfyUI lane (nothing
reaches a machine: /api/generate and /api/upload are answered locally):
1. a visit with no link opens the home view: every task of GET /api/engines
   "tasks" as a card, in four columns by group; a task nothing fills, and a
   task no machine can run, are dimmed and say so;
2. a card opens its task (#task=<id>); "‹ Everything" returns; the old links
   (#task=, #room=, #workflows) still open what they open;
3. search ranks by what the person means ("animate my photo"), opens the top
   match on Enter, clears on Escape, and says so when nothing fits;
4. a dropped file (the picker, a drop on the strip, a drop anywhere) is
   classified, folds the tasks that cannot use it, and is carried into the
   task's first matching field through the app's own upload path; a task
   without such a field says so and keeps the file; a refused upload says why;
5. nothing runs off the edge at 390 wide.

Run: python3 tests/test_home_ui.py
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

sys.dont_write_bytecode = True
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))

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

def http_json(url, timeout=3):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())

def stop(proc):
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

try:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as _pw:
        _pw.chromium.launch().close()
except Exception as e:
    print("SKIP: playwright or its chromium is not installed (%s). pip install -r requirements-dev.txt "
          "&& python3 -m playwright install --with-deps chromium" % e)
    sys.exit(0)

tmp = tempfile.mkdtemp(prefix="bwf-home-")
fake_port = free_port()
fake = subprocess.Popen([sys.executable, os.path.join(HERE, "fixtures", "fake_comfy.py"), "--port", str(fake_port)],
                        cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
data_dir = os.path.join(tmp, "data")
shutil.copytree(os.path.join(HERE, "fixtures", "ws_packs"), os.path.join(data_dir, "packs"))
server_port = free_port()
cfg_path = os.path.join(tmp, "config.json")
with open(cfg_path, "w") as f:
    json.dump({"title": "Home view", "port": server_port, "bind": "127.0.0.1",
               "lanes": [{"id": "rig", "name": "Fake lane", "host": "127.0.0.1", "port": fake_port,
                          "caps": ["3d", "audio", "image", "video"]},
                         {"id": "here", "kind": "process", "name": "This box", "slots": 1, "caps": ["3d"]}],
               "timing": {"poll_seconds": 1.0, "job_poll_seconds": 1.0, "http_timeout": 4.0,
                          "free_settle_seconds": 2.0, "discover_seconds": 300.0}}, f)
URL = "http://127.0.0.1:%d/" % server_port
logf = open(os.path.join(tmp, "server.log"), "w")
server = subprocess.Popen([sys.executable, os.path.join(REPO, "server.py")], cwd=REPO,
                          env=dict(os.environ, GENCENTER_CONFIG=cfg_path, GENCENTER_DATA=data_dir),
                          stdout=logf, stderr=subprocess.STDOUT)

def up():
    d = http_json(URL + "api/engines?lane=rig")
    return any(m.get("available") for c, v in d.items() if isinstance(v, dict) and "modes" in v for m in v["modes"])

ok = False
for _ in range(120):
    try:
        if up():
            ok = True
            break
    except Exception:
        pass
    time.sleep(0.25)
if not ok:
    check("server is up and the fake lane offers a mode", False)
    stop(server); stop(fake)
    sys.exit(1)

GENERATED = []
JOBS = {"jobs": [], "log": [], "now": 0}
UPLOAD_NAMES = []        # what the next /api/upload answers

def fake_generate(route, request):
    body = json.loads(request.post_data or "{}")
    GENERATED.append(body)
    job = {"id": "ws-%d" % len(GENERATED), "lane": "rig", "lane_name": "Fake lane", "kind": body.get("kind"),
           "mode": body.get("mode"), "status": "queued", "step": 0, "total": 0, "created": time.time(),
           "outputs": [], "prompt": body.get("prompt", "")}
    route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True, "job": job}))

def fake_upload(route, request):
    n = (request.post_data_buffer or b"").count(b'name="file"')
    names = [{"name": "up-%d.png" % i} for i in range(max(1, n))]
    route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True, "files": names}))

def guard(page):
    page.route("**/api/generate", fake_generate)
    page.route("**/api/upload", fake_upload)
    page.route("**/api/view*", lambda r, q: r.fulfill(status=200, content_type="video/mp4", body=""))

def open_page(browser, hash_="", width=1440, height=900):
    page = browser.new_page(viewport={"width": width, "height": height})
    page.on("pageerror", lambda e: ERRORS.append("PAGEERROR: %s" % e))
    page.on("console", lambda m: ERRORS.append("CONSOLE %s: %s" % (m.type, m.text)) if m.type == "error" else None)
    guard(page)
    page.goto(URL + hash_, wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(1500)
    return page

def field_ids(page):
    return set(page.eval_on_selector_all("#inspector [data-field-id]", "els => els.map(e => e.dataset.fieldId)"))

def declared_ids(engines, cap, mode):
    m = next(x for x in engines[cap]["modes"] if x["id"] == mode)
    return {f["id"] for f in m["fields"]}


UPLOAD_FAIL = []         # when non-empty the next upload answers this refusal

UPLOADS_SEEN = []

def fake_upload2(route, request):
    UPLOADS_SEEN.append(1)
    if UPLOAD_FAIL:
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": False, "error": UPLOAD_FAIL.pop()}))
        return
    fake_upload(route, request)

def guard2(page):
    guard(page)
    page.route("**/api/workflows?*", lambda r, q: r.fulfill(status=200, content_type="application/json",
                                                           body=json.dumps({"ok": False, "error": "Not asked in this test."})))
    page.route("**/api/upload", fake_upload2)

def open2(browser, hash_="", width=1440, height=900):
    page = browser.new_page(viewport={"width": width, "height": height})
    page.on("pageerror", lambda e: ERRORS.append("PAGEERROR: %s" % e))
    page.on("console", lambda m: ERRORS.append("CONSOLE %s: %s %s" % (m.type, m.text, m.location.get("url"))) if m.type == "error" else None)
    guard2(page)
    page.goto(URL + hash_, wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(1500)
    return page

PNG = {"name": "photo.png", "mimeType": "image/png", "buffer": b"\x89PNG\r\n\x1a\n" + b"0" * 32}
WAV = {"name": "tune.wav", "mimeType": "audio/wav", "buffer": b"RIFF0000WAVE" + b"0" * 32}

def home_cards(page):
    return page.eval_on_selector_all("#homeCards .task-card", "els => els.map(e => e.dataset.taskId)")

def first_primary(engines, task, types):
    """The first primary field of the task's first way that takes one of `types`."""
    w = task["ways"][0]
    m = next(x for x in engines[w["cap"]]["modes"] if x["id"] == w["mode"])
    fs = sorted([f for f in m["fields"] if f.get("tier") == "primary" and f["type"] in types], key=lambda f: f.get("order", 0))
    return fs[0]["id"] if fs else None

def uploads(page):
    return page.evaluate("Object.fromEntries(Object.entries(STATE.uploads).map(([k, v]) => [k, v.length]))")

def drop_files(page, selector, files):
    """A synthetic drop of real File objects on `selector`."""
    page.evaluate("""([sel, files]) => {
      const dt = new DataTransfer();
      files.forEach(f => dt.items.add(new File([f.body], f.name, {type: f.type})));
      const t = document.querySelector(sel);
      ['dragover', 'drop'].forEach(n => t.dispatchEvent(new DragEvent(n, {bubbles: true, cancelable: true, dataTransfer: dt})));
    }""", [selector, files])

def over_edge(page, w):
    return page.evaluate("""(w) => Array.from(document.querySelectorAll('#home *')).filter(e => {
      const r = e.getBoundingClientRect(); if (!r.width && !r.height) return false;
      return r.left < -0.5 || r.right > w + 0.5; }).map(e => e.tagName + '.' + e.className + ' ' + (e.textContent || '').trim().slice(0, 30))""", w)

ERRORS = []
try:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        engines = http_json(URL + "api/engines?lane=rig")
        TASKS = engines["tasks"]
        by_id = {t["id"]: t for t in TASKS}

        print()
        print("no link: the home view, every task as a card")
        page = open2(browser)
        check("the page opens on home, not on a task", page.is_visible("#home") and not page.is_visible("#layout")
              and page.evaluate("document.body.classList.contains('home-open')"))
        check("... with no hash", page.evaluate("location.hash") == "", page.evaluate("location.hash"))
        check("the headline", page.inner_text("#homeHeading").strip() == "What do you want to make?")
        check("a search box and a drop strip", page.is_visible("#homeSearch") and "drop a picture, clip, song or 3D object here" in page.inner_text("#homeDrop")
              and page.is_visible("#homeSample") and "sample picture" in page.inner_text("#homeSample").lower())
        cols = page.eval_on_selector_all("#homeCards .home-col", "els => els.map(e => [e.dataset.group, Array.from(e.querySelectorAll('.task-card')).map(c => c.dataset.taskId)])")
        want = [[g, [t["id"] for t in TASKS if t["group"] == g]] for g in ("SOUND", "PICTURE", "MOTION", "OBJECT")]
        check("four columns Sound / Picture / Motion / Object holding exactly the server's tasks", cols == want, cols)
        card = page.query_selector('#homeCards [data-task-id="animate"]')
        check("a card is the verb (bold) over the line", card.query_selector("b").inner_text() == by_id["animate"]["verb"]
              and by_id["animate"]["line"] in card.inner_text())
        check("on home the bar is Everything (pressed) and the four groups: no group pressed, no task tabs",
              page.get_attribute("#homeBtn", "aria-pressed") == "true" and page.query_selector("#roomStrip .task-tab") is None
              and page.eval_on_selector_all("#roomStrip .group-tab", "e => e.map(b => b.getAttribute('aria-pressed'))") == ["false"] * 4)
        check("the task bar is still there with an Everything button, pressed",
              page.is_visible("#homeBtn") and page.get_attribute("#homeBtn", "aria-pressed") == "true"
              and page.get_attribute('#roomStrip [data-group="SOUND"]', "aria-pressed") == "false")
        page.close()

        print()
        print("dim rules")
        page = open2(browser)
        empty = [t["id"] for t in TASKS if not t["ways"] and not t.get("action")]
        check("(setup) the fake lane leaves at least one task nothing fills", bool(empty), empty)
        for tid in empty[:2]:
            c = page.query_selector('#homeCards [data-task-id="%s"]' % tid)
            check("%s: dimmed, says nothing installed makes it" % tid,
                  "task-card-dim" in c.get_attribute("class") and "Nothing installed makes this yet." in c.text_content(), c.text_content())
        c = page.query_selector('#homeCards [data-task-id="%s"]' % by_id["t2i"]["id"])
        check("a task a machine can run is not dimmed", "task-card-dim" not in c.get_attribute("class") and c.get_attribute("data-off") is None)
        txt = page.evaluate("""() => {
          const t = taskById('sfx');
          const saved = taskModes(t).map(x => [x.info, x.info.available]);
          const lanes = STATE.lanes.map(l => [l, l.up]);
          saved.forEach(([i]) => { i.available = false; });
          lanes.forEach(([l]) => { l.up = false; });
          renderHomeCards();
          const c = document.querySelector('#homeCards [data-task-id="sfx"]');
          const out = [c.className, c.textContent];
          saved.forEach(([i, v]) => { i.available = v; });
          lanes.forEach(([l, v]) => { l.up = v; });
          renderHomeCards();
          return out; }""")
        check("a task whose machines are asleep or unreachable is NOT dimmed", "task-card-dim" not in txt[0] and "machine" not in txt[1].lower(), txt)
        txt = page.evaluate("""() => {
          const t = taskById('sfx');
          const caps = taskCaps(t);
          const saved = STATE.lanes.map(l => [l, l.declared_caps]);
          saved.forEach(([l]) => { l.declared_caps = (l.declared_caps || []).filter(c => !caps.includes(c)); });
          renderHomeCards();
          const c = document.querySelector('#homeCards [data-task-id="sfx"]');
          const out = [c.className, c.textContent];
          saved.forEach(([l, v]) => { l.declared_caps = v; });
          renderHomeCards();
          return out; }""")
        check("a task whose cap no lane declares is dimmed: 'No machine in config.json can make this.'",
              "task-card-dim" in txt[0] and "No machine in config.json can make this." in txt[1], txt)
        page.close()

        print()
        print("a card opens its task; Everything returns; the old links still open what they did")
        page = open2(browser)
        page.click('#homeCards [data-task-id="animate"]')
        page.wait_for_timeout(800)
        check("a card opens #task=animate with the workspace", page.evaluate("location.hash") == "#task=animate"
              and page.is_visible("#layout") and not page.is_visible("#home") and page.evaluate("STATE.task") == "animate")
        check("the task tab is pressed and Everything is not", page.get_attribute('#roomStrip [data-task-id="animate"]', "aria-pressed") == "true"
              and page.get_attribute("#homeBtn", "aria-pressed") == "false")
        page.click("#homeBtn")
        page.wait_for_timeout(300)
        check("Everything returns to home", page.is_visible("#home") and not page.is_visible("#layout") and page.evaluate("location.hash") == "")
        check("... no tab or group looks current", page.eval_on_selector_all('#roomStrip [aria-pressed="true"]', "e => e.map(x => x.id)") == ["homeBtn"])
        page.click("#homeBtn")
        page.evaluate("location.hash = '#task=animate'")
        page.wait_for_timeout(600)
        check("after Everything, the same #task= link shows the workspace again",
              not page.is_visible("#home") and page.is_visible("#layout") and page.evaluate("STATE.task") == "animate")
        page.click("#homeBtn")
        page.wait_for_timeout(200)
        page.click('#roomStrip [data-group="PICTURE"]')
        page.wait_for_timeout(500)
        check("a group button leaves home for that group's task", not page.is_visible("#home") and by_id[page.evaluate("STATE.task")]["group"] == "PICTURE")
        page.evaluate("location.hash = ''")
        page.wait_for_timeout(500)
        check("clearing the link in the address bar shows home", page.is_visible("#home"))
        page.click("#workflowsBtn")
        page.wait_for_timeout(300)
        check("Workflows replaces home", page.is_visible("#workflowsView") and not page.is_visible("#home"))
        page.close()
        for h, test, label in (("#task=animate", "STATE.task == 'animate'", "#task= opens that task"),
                               ("#room=video", "STATE.task == 'video'", "#room= opens the room's first task"),
                               ("#workflows", "WF.open", "#workflows opens Workflows")):
            page = open2(browser, h)
            check(label + " and not home", page.evaluate(test) and not page.is_visible("#home"), page.evaluate("location.hash"))
            page.close()

        page = open2(browser, "#task=animate")
        page.evaluate("location.hash = '#workflows'")
        page.wait_for_timeout(300)
        page.click("#homeBtn")
        page.wait_for_timeout(200)
        page.evaluate("history.back()")
        page.wait_for_timeout(700)
        check("Back onto the link of the task still open shows its workspace, not home",
              page.evaluate("location.hash") == "#task=animate" and not page.is_visible("#home") and page.is_visible("#layout"),
              page.evaluate("location.hash"))
        page.close()

        print()
        print("search")
        page = open2(browser)
        def ask(q):
            page.fill("#homeSearch", q)
            page.wait_for_timeout(150)
            return home_cards(page)
        r = ask("animate my photo")
        check("'animate my photo' puts Animate a picture first", r[:1] == ["animate"], r[:4])
        check("... as one ranked list", page.text_content("#homeCards .home-results h2").strip() == "Best matches first"
              and page.query_selector("#homeCards .home-cols") is None)
        r = ask("make my photo talk")
        check("'make my photo talk' puts Make a face say a line first", r[:1] == ["talking"], r[:4])
        for q in ("4k", "bigger"):
            r = ask(q)
            check("'%s' puts Make a picture bigger first" % q, r[:1] == ["upscale"], r[:4])
        r = ask("make a video")
        check("'make a video' puts Make a video from words first (a token counts once per task)", r[:1] == ["video"], r[:4])
        r = ask("3d object")
        check("'3d object' puts Turn a picture into a 3D object first", r[:1] == ["mesh"], r[:4])
        r = ask("face")
        check("a word matches word starts only ('face' does not hit 'surfaces')", "textures" not in r and "talking" in r, r)
        r = ask("zzzz")
        check("nothing fits: the empty sentence", r == [] and page.inner_text("#homeZero").strip() == "Nothing here does that yet.")
        page.fill("#homeSearch", "animate my photo")
        page.press("#homeSearch", "Escape")
        page.wait_for_timeout(150)
        check("Escape clears the search and brings the columns back", page.input_value("#homeSearch") == ""
              and len(page.query_selector_all("#homeCards .home-col")) == 4)
        page.fill("#homeSearch", "animate my photo")
        page.press("#homeSearch", "Enter")
        page.wait_for_timeout(800)
        check("Enter opens the top match", page.evaluate("STATE.task") == "animate" and not page.is_visible("#home"),
              page.evaluate("STATE.task"))
        page.close()

        print()
        print("a dropped picture folds what cannot use it and carries into the task")
        page = open2(browser)
        page.set_input_files("#homeFile", PNG)
        page.wait_for_selector("#homeChip", timeout=10000)
        check("a chip names the file and its kind, with a clear button", page.text_content("#homeChip").strip().lower().startswith("photo.png · picture")
              and page.is_visible("#homeChipClear"), page.text_content("#homeChipBar"))
        shown = home_cards(page)
        accepts = [t["id"] for t in TASKS if "picture" in t["accepts"]]
        check("only tasks that take a picture stay up front", shown == accepts and "animate" in shown, shown)
        fold = page.inner_text("#homeFold")
        check("the rest fold under 'N things don't use a picture ▸'", fold.strip() == "%d things don't use a picture ▸" % (len(TASKS) - len(accepts)), fold)
        page.click("#homeFold")
        page.wait_for_timeout(150)
        check("opening the fold lists them", set(page.eval_on_selector_all("#homeFolded .task-card", "e => e.map(c => c.dataset.taskId)")) == set(by_id) - set(accepts))
        page.click("#homeFold")
        page.click('#homeCards [data-task-id="animate"]')
        field = first_primary(engines, by_id["animate"], ("image", "image_list"))
        if field:
            page.wait_for_function("f => (STATE.uploads[f] || []).length >= 1", arg=field, timeout=10000)
        page.wait_for_function("() => STATE.carry === null", timeout=10000)
        check("(setup) animate has a primary picture field", field is not None)
        check("the picture is in animate's first picture field (through /api/upload)", uploads(page).get(field) == 1, uploads(page))
        check("... and the carry is used up", page.evaluate("STATE.carry") is None and page.is_hidden("#carryNote"))
        page.click("#homeBtn")
        check("back home the chip is gone", page.query_selector("#homeChip") is None)
        page.close()

        print()
        print("a drop on the strip and a drop anywhere; the clear button; a refused file")
        page = open2(browser)
        drop_files(page, "#homeDrop", [{"name": "shot.jpg", "type": "image/jpeg", "body": "x"}])
        page.wait_for_timeout(200)
        check("a drop on the strip is taken", "shot.jpg" in page.text_content("#homeChip"))
        page.click("#homeChipClear")
        check("× clears the file and the full columns come back", page.query_selector("#homeChip") is None
              and len(page.query_selector_all("#homeCards .home-col")) == 4 and page.evaluate("STATE.carry") is None)
        drop_files(page, "#homeCards", [{"name": "clip.mp4", "type": "", "body": "x"}])
        page.wait_for_timeout(200)
        check("a drop anywhere on the view is taken; the kind comes from the extension", "clip.mp4 · clip" in page.text_content("#homeChip").lower(),
              page.text_content("#homeChipBar"))
        drop_files(page, "#homeCards", [{"name": "notes.txt", "type": "text/plain", "body": "x"}])
        page.wait_for_timeout(200)
        check("an unknown file gets one plain line", page.inner_text("#homeReject").strip() == "That file isn't a picture, clip, song or 3D object.")
        check("... and the earlier file is kept", "clip.mp4" in page.text_content("#homeChip"))
        drop_files(page, "#homeCards", [{"name": "thing.glb", "type": "", "body": "x"}])
        page.wait_for_timeout(200)
        check("a .glb is a 3D object", "thing.glb · 3d object" in page.text_content("#homeChip").lower(), page.text_content("#homeChipBar"))
        check("... and the line is gone", page.query_selector("#homeReject") is None)
        page.close()

        print()
        print("a sample picture; a song into the cover task")
        page = open2(browser)
        page.click("#homeSample")
        page.wait_for_function("() => !!(STATE.carry && STATE.carry.file)", timeout=10000)   # canvas.toBlob is async
        check("'Try a sample picture' holds a generated picture", page.evaluate("STATE.carry && STATE.carry.file.name") == "sample-picture.png"
              and page.evaluate("STATE.carry.file.type") == "image/png" and page.evaluate("STATE.carry.file.size") > 100)
        page.close()
        page = open2(browser)
        page.set_input_files("#homeFile", WAV)
        page.wait_for_selector("#homeChip", timeout=10000)
        shown = home_cards(page)
        check("a song brings the tasks that take a song up front", "cover" in shown and "animate" not in shown, shown)
        check("the music-video task counts as taking a song", "musicvideo" in shown, shown)
        page.click('#homeCards [data-task-id="cover"]')
        sf = first_primary(engines, by_id["cover"], ("audio",))
        if sf:
            page.wait_for_function("f => (STATE.uploads[f] || []).length >= 1", arg=sf, timeout=10000)
        check("(setup) cover has a primary song field", sf is not None)
        check("the song is in cover's first song field", page.evaluate("location.hash") == "#task=cover" and uploads(page).get(sf) == 1, uploads(page))
        page.close()

        print()
        print("a song into the music-video task goes to its dialog")
        page = open2(browser)
        page.set_input_files("#homeFile", WAV)
        n0 = len(UPLOADS_SEEN)
        page.click('#homeCards [data-task-id="musicvideo"]')
        page.wait_for_function("() => document.getElementById('forgeVideoDialog').open "
                               "&& (document.getElementById('forgeVideoSong').files[0] || {}).name", timeout=10000)
        check("the music-video dialog opens with the song in its file input",
              page.evaluate("document.getElementById('forgeVideoDialog').open")
              and page.evaluate("(document.getElementById('forgeVideoSong').files[0] || {}).name") == "tune.wav")
        check("... and the dialog's own upload ran", len(UPLOADS_SEEN) > n0, UPLOADS_SEEN)
        check("... the carry is used up", page.evaluate("STATE.carry") is None)
        page.close()

        print()
        print("a task that cannot use the file says so and keeps it; a refused upload says why")
        page = open2(browser)
        page.set_input_files("#homeFile", PNG)
        page.click("#homeFold")
        page.click('#homeFolded [data-task-id="song"]')
        page.wait_for_function("() => { const n = document.getElementById('carryNote'); return !!n && !n.hidden && n.textContent.trim(); }", timeout=10000)
        check("this one doesn't use your picture", page.inner_text("#carryNote").strip() == "This one doesn't use your picture.", page.inner_text("#carryNote"))
        check("... the file is kept for the next task", page.evaluate("STATE.carry && STATE.carry.file.name") == "photo.png")
        page.evaluate("selectTask('animate')")
        if field:
            page.wait_for_function("f => (STATE.uploads[f] || []).length >= 1", arg=field, timeout=10000)
        page.wait_for_function("() => document.getElementById('carryNote').hidden", timeout=10000)
        check("... and the next task takes it; the line is gone", uploads(page).get(field) == 1 and page.is_hidden("#carryNote"), uploads(page))
        page.close()
        page = open2(browser)
        page.set_input_files("#homeFile", PNG)
        page.click("#homeFold")
        page.click('#homeFolded [data-task-id="textures"]')
        page.wait_for_function("() => { const n = document.getElementById('carryNote'); return !!n && !n.hidden && n.textContent.trim(); }", timeout=10000)
        check("a task with nothing installed still shows the 'doesn't use your picture' line",
              page.is_visible("#carryNote") and page.text_content("#carryNote").strip() == "This one doesn't use your picture.",
              page.text_content("#carryNote"))
        page.close()
        page = open2(browser)
        UPLOAD_FAIL.append("That machine is full.")
        page.set_input_files("#homeFile", PNG)
        page.click('#homeCards [data-task-id="animate"]')
        page.wait_for_function("() => document.getElementById('inspectorMsg').textContent.includes('That machine is full.')", timeout=10000)
        check("a refused upload shows the server's sentence in the inspector message", "That machine is full." in page.inner_text("#inspectorMsg"), page.inner_text("#inspectorMsg"))
        check("... and nothing was attached", not uploads(page).get(field))
        page.close()

        print()
        print("the field's own picker still uploads (the shared path)")
        page = open2(browser, "#task=animate")
        page.set_input_files("#upload_%s" % field, PNG)
        page.wait_for_function("f => (STATE.uploads[f] || []).length >= 1", arg=field, timeout=10000)
        check("a picture picked in the form lands in STATE.uploads", uploads(page).get(field) == 1, uploads(page))
        page.close()

        print()
        print("nothing runs off the edge at 390 wide")
        for w, h in ((1440, 900), (390, 844)):
            page = open2(browser, "", w, h)
            states = [("plain", None), ("with a dropped picture", "pic"), ("searching", "q")]
            for label, how in states:
                if how == "pic":
                    page.set_input_files("#homeFile", PNG)
                    page.click("#homeFold")
                elif how == "q":
                    page.click("#homeChipClear")
                    page.fill("#homeSearch", "animate my photo")
                page.wait_for_timeout(200)
                check("%dx%d %s: no home element past the edge" % (w, h, label), not over_edge(page, w), over_edge(page, w)[:4])
                check("%dx%d %s: the page does not scroll sideways" % (w, h, label), page.evaluate("document.documentElement.scrollWidth") <= w)
            if w == 390:
                page.fill("#homeSearch", "")
                page.wait_for_timeout(150)
                xs = page.eval_on_selector_all("#homeCards .home-col", "els => els.map(e => Math.round(e.getBoundingClientRect().left))")
                check("390: the columns stack", len(set(xs)) == 1 and len(xs) == 4, xs)
            page.close()

        browser.close()
finally:
    stop(server)
    stop(fake)
    logf.close()
    shutil.rmtree(tmp, ignore_errors=True)

check("zero console/page errors", not ERRORS, str(ERRORS[:5]))
print()
print(("FAILED: %d" % len(FAILED)) if FAILED else "ALL PASS")
sys.exit(1 if FAILED else 0)
