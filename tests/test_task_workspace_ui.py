"""The task workspace: the page opens on what a person wants to make, not on a
room of engines.

Pins, in a real browser against a real server.py + a fake ComfyUI lane (nothing
reaches a machine: /api/generate and /api/upload are answered locally):
1. the bar: four group buttons, then the current group's task tabs, all read
   from GET /api/engines "tasks" -- never a fixed list in the page;
2. ways: a task with 2+ ways shows them as a radio list that switches the mode
   (the form's fields change); the remembered way comes back on the next entry;
3. Draft/Final: a way with a final shows a two-part switch; FINAL runs the
   data pack's keeper (tests/fixtures/ws_packs), by the number of attached
   pictures for a keeper per count, and a Make there sends the keeper's mode;
4. routing: #task=<id> survives a reload, an old #room=<id> opens that room's
   first task, #task=<id>&try=<example> picks the task owning the example;
5. history scope is per task ("This task (n)"); a done job from this page
   ticks the trail; the monitor names what is still to fill in;
6. a task nothing fills is dimmed and says so; the music-video task carries
   its button; nothing in the bar overflows at 1440 and 390 wide.

Run: python3 tests/test_task_workspace_ui.py
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

tmp = tempfile.mkdtemp(prefix="bwf-taskws-")
fake_port = free_port()
fake = subprocess.Popen([sys.executable, os.path.join(HERE, "fixtures", "fake_comfy.py"), "--port", str(fake_port)],
                        cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
data_dir = os.path.join(tmp, "data")
shutil.copytree(os.path.join(HERE, "fixtures", "ws_packs"), os.path.join(data_dir, "packs"))
server_port = free_port()
cfg_path = os.path.join(tmp, "config.json")
with open(cfg_path, "w") as f:
    json.dump({"title": "Task workspace", "port": server_port, "bind": "127.0.0.1",
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

def until(page, fn, what="condition", timeout=10000):
    """Wait on the state a check asserts, not on a fixed pause (a slow host needs longer)."""
    waited = 0
    while not fn():
        if waited >= timeout:
            raise AssertionError("timed out waiting for " + what)
        page.wait_for_timeout(50)
        waited += 50

def txt(page, sel):
    return page.evaluate("s => { const e = document.querySelector(s); return e ? e.textContent.trim() : ''; }", sel)

def shown(page, sel):
    return page.evaluate("s => { const e = document.querySelector(s); return !!e && e.offsetParent !== null; }", sel)

def field_ids(page):
    return set(page.eval_on_selector_all("#inspector [data-field-id]", "els => els.map(e => e.dataset.fieldId)"))

def declared_ids(engines, cap, mode):
    m = next(x for x in engines[cap]["modes"] if x["id"] == mode)
    return {f["id"] for f in m["fields"]}

ERRORS = []
try:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        engines = http_json(URL + "api/engines?lane=rig")
        TASKS = engines["tasks"]
        by_id = {t["id"]: t for t in TASKS}

        print()
        print("the bar: groups and tabs come from the task list")
        page = open_page(browser)
        groups = page.eval_on_selector_all("#roomStrip .group-tab", "els => els.map(e => [e.dataset.group, e.textContent.trim()])")
        check("four group buttons, Sound / Picture / Motion / Object",
              groups == [["SOUND", "Sound"], ["PICTURE", "Picture"], ["MOTION", "Motion"], ["OBJECT", "Object"]], groups)
        check("no link opens the home view, and its first card opens the first task",
              page.is_visible("#home") and page.evaluate("location.hash") == "", page.evaluate("location.hash"))
        page.click("#homeCards .task-card")
        page.wait_for_timeout(500)
        check("... which is the first task in order", page.evaluate("location.hash") == "#task=" + TASKS[0]["id"],
              page.evaluate("location.hash"))
        for g in ("SOUND", "PICTURE", "MOTION", "OBJECT"):
            page.click('#roomStrip [data-group="%s"]' % g)
            page.wait_for_timeout(250)
            want = [t["id"] for t in TASKS if t["group"] == g]
            tabs = page.eval_on_selector_all("#roomStrip .task-tab", "els => els.map(e => [e.dataset.taskId, e.textContent.trim(), e.title])")
            check("%s: tabs are exactly that group's tasks, labelled by `short`" % g,
                  [t[0] for t in tabs] == want and all(t[1].startswith(by_id[t[0]]["short"]) for t in tabs), tabs)
            check("%s: the group button is pressed and the page is on one of its tasks" % g,
                  page.get_attribute('#roomStrip [data-group="%s"]' % g, "aria-pressed") == "true"
                  and by_id[page.evaluate("STATE.task")]["group"] == g)
        data_tabs = page.evaluate("fetch('/api/engines?lane=rig').then(r => r.json()).then(d => d.tasks.filter(t => t.id.startsWith('ws')).length)")
        check("fixture data packs add no tab of their own (they join built-in tasks)", data_tabs == 0, data_tabs)
        page.close()

        print()
        print("a way radio switches the mode; the form's fields change")
        page = open_page(browser, "#task=video")
        ways = page.eval_on_selector_all("#ways input[name=way]", "els => els.map(e => [e.dataset.cap, e.dataset.mode])")
        check("the video task lists its ways as radios", [w[1] for w in ways] == ["ltx", "ltx_loop"], ways)
        check("the first way is checked on a fresh browser", page.evaluate("STATE.mode") == "ltx"
              and page.eval_on_selector('#ways input:checked', "e => e.dataset.mode") == "ltx")
        before = field_ids(page)
        check("the form shows the first way's declared fields", before >= {"prompt"} and before <= declared_ids(engines, "video", "ltx") | {"promptBox"},
              sorted(before))
        page.check('#ways input[data-mode="ltx_loop"]')
        page.wait_for_timeout(400)
        after = field_ids(page)
        check("picking the second way switches STATE.mode", page.evaluate("STATE.mode") == "ltx_loop")
        check("... and the form's fields are the second way's", after != before and after <= declared_ids(engines, "video", "ltx_loop") | {"promptBox"},
              (sorted(before), sorted(after)))
        stored = json.loads(page.evaluate("localStorage.getItem('bwf.task.way.video')"))
        check("the pick is remembered as {cap, mode, stage} per task", stored == {"cap": "video", "mode": "ltx_loop", "stage": "draft"}, stored)
        check("no per-room engine is written any more",
              page.evaluate("Object.keys(localStorage).filter(k => k.startsWith('bwf.room.engine.')).length") == 0)
        page.click('#roomStrip [data-group="PICTURE"]')
        page.wait_for_timeout(250)
        page.click('#roomStrip [data-group="MOTION"]')
        page.wait_for_timeout(250)
        page.click('#roomStrip [data-task-id="animate"]')
        page.wait_for_timeout(250)
        page.click('#roomStrip [data-task-id="video"]')
        page.wait_for_timeout(400)
        check("coming back to the task restores the way last picked", page.evaluate("STATE.mode") == "ltx_loop"
              and page.eval_on_selector('#ways input:checked', "e => e.dataset.mode") == "ltx_loop")
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(1500)
        check("... and so does a reload", page.evaluate("STATE.mode") == "ltx_loop")
        check("the single-way task has no ways list", (page.click('#roomStrip [data-task-id="animate"]') or True)
              and not page.is_visible("#ways"))
        page.close()

        print()
        print("routing: #task= survives a reload; #room= opens the room's first task")
        page = open_page(browser, "#task=animate")
        check("#task=animate opens animate", page.evaluate("STATE.task") == "animate"
              and page.get_attribute('#roomStrip [data-task-id="animate"]', "aria-pressed") == "true")
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(1500)
        check("#task=animate survives a reload", page.evaluate("location.hash") == "#task=animate"
              and page.evaluate("STATE.task") == "animate")
        page.goto(URL + "#room=video", wait_until="networkidle")
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(1500)
        check("#room=video lands on the video task (the room's first), not a data pack's mode",
              page.evaluate("STATE.task") == "video" and page.evaluate("STATE.mode") == "ltx", [page.evaluate("STATE.task"), page.evaluate("STATE.mode")])
        check("... and rewrites the link to #task=video", page.evaluate("location.hash") == "#task=video", page.evaluate("location.hash"))
        ex = next(e for m in engines["video"]["modes"] if m["id"] == "fl2va" for e in (m.get("examples") or []))
        page.goto(URL + "#room=video&try=" + ex["id"], wait_until="networkidle")
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(1500)
        check("#room=video&try=<example> opens the task whose way owns that example",
              page.evaluate("STATE.task") == "animate" and page.evaluate("STATE.mode") == "fl2va",
              [page.evaluate("STATE.task"), page.evaluate("STATE.mode")])
        # An example owned by a task of ANOTHER room: the room follows the task, so the guide does too.
        page.goto(URL + "#room=music&try=" + ex["id"], wait_until="networkidle")
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(1500)
        want_room = page.evaluate("taskRoom(taskById('animate')).id")
        check("#room=music&try=<an animate example> puts STATE.room on the animate task's room, not music",
              page.evaluate("STATE.task") == "animate" and page.evaluate("STATE.room") == want_room != "music"
              and page.evaluate("location.hash").startswith("#task=animate"),
              [page.evaluate("STATE.task"), page.evaluate("STATE.room"), page.evaluate("location.hash")])
        page.goto("about:blank")
        page.goto(URL + "#task=song", wait_until="networkidle")
        page.evaluate("ex => selectRoom('music', {tryId: ex})", ex["id"])
        page.wait_for_timeout(800)
        check("the same through selectRoom in a live page", page.evaluate("STATE.room") == want_room
              and page.evaluate("STATE.task") == "animate", [page.evaluate("STATE.task"), page.evaluate("STATE.room")])
        page.close()

        print()
        print("Draft / Final: the switch runs the keeper")
        page = open_page(browser, "#task=animate")
        check("a way with a final shows the switch, Draft pressed", page.is_visible("#stageSwitch")
              and page.get_attribute('#stageSwitch [data-stage="draft"]', "aria-pressed") == "true"
              and page.get_attribute('#stageSwitch [data-stage="final"]', "aria-pressed") == "false")
        check("the draft line", "A quick look first." in page.inner_text("#stageHint"), page.inner_text("#stageHint"))
        check("the Make button says it makes a draft", page.text_content("#makeBtn").strip() == "Make a draft")
        draft_fields = field_ids(page)
        page.fill("#promptBox", "a lighthouse")
        page.click('#stageSwitch [data-stage="final"]')
        page.wait_for_timeout(500)
        check("FINAL switches to the data pack's keeper", page.evaluate("STATE.mode") == "wsanimkeep", page.evaluate("STATE.mode"))
        check("... pressed, with the keeper line", page.get_attribute('#stageSwitch [data-stage="final"]', "aria-pressed") == "true"
              and "The keeper" in page.inner_text("#stageHint"))
        check("... the form is the keeper's own", "keep_note" in field_ids(page) and "keep_note" not in draft_fields)
        check("... the prompt typed on the draft is still there", page.input_value("#promptBox") == "a lighthouse")
        check("... Make is plain Make", page.text_content("#makeBtn").strip() == "Make")
        before = len(GENERATED)
        page.click("#makeBtn")
        until(page, lambda: len(GENERATED) > before, "the keeper's /api/generate")
        check("a Make in Final sends the keeper's mode", len(GENERATED) > before and GENERATED[-1].get("mode") == "wsanimkeep"
              and GENERATED[-1].get("kind") == "video", GENERATED[-1:])
        stored = json.loads(page.evaluate("localStorage.getItem('bwf.task.way.animate')"))
        check("the stage saved after FINAL is final", stored.get("stage") == "final" and stored.get("mode") == "fl2va", stored)
        page.click('#stageSwitch [data-stage="draft"]')
        page.wait_for_timeout(500)
        check("DRAFT goes back to the way's own mode", page.evaluate("STATE.mode") == "fl2va"
              and field_ids(page) == draft_fields)
        stored = json.loads(page.evaluate("localStorage.getItem('bwf.task.way.animate')"))
        check("the stage is remembered with the way", stored.get("stage") == "draft" and stored.get("mode") == "fl2va", stored)
        page.close()

        print()
        print("a Draft opens on the first available quality tier; a pick is remembered per task")
        page = open_page(browser, "#task=animate")
        live = page.evaluate("fetch('/api/engines?lane=rig').then(r => r.json())")   # tier availability once discovery has answered
        tiers = next(m for m in live["video"]["modes"] if m["id"] == "fl2va")["quality"]
        first = next(t for t in tiers if t.get("available") is not False)
        other = next((t for t in tiers if t["id"] != first["id"] and t.get("available") is not False
                      and t.get("values") != first.get("values")), None)
        def pressed():
            return page.eval_on_selector_all('#qualityLadder [aria-pressed="true"]', "els => els.map(e => e.dataset.quality)")
        check("the draft opens on the first available tier (%s), not the mode's default" % first["id"],
              pressed() == [first["id"]], pressed())
        for k, v in (first.get("values") or {}).items():
            el = page.query_selector('#inspector [data-field-id="%s"]' % k)
            if el is not None and el.get_attribute("type") != "checkbox":
                check("... and its %s (%r) is in the field" % (k, v), page.input_value('#inspector [data-field-id="%s"]' % k) == str(v),
                      page.input_value('#inspector [data-field-id="%s"]' % k))
        if other is None:
            check("a second tier exists to pick", False)
        else:
            page.click('#qualityLadder [data-quality="%s"]' % other["id"])
            page.wait_for_timeout(200)
            page.reload(wait_until="networkidle")
            page.wait_for_timeout(1500)
            check("a tier picked in the task (%s) is remembered across a reload" % other["id"], pressed() == [other["id"]], pressed())
            page.click('#stageSwitch [data-stage="final"]')
            page.wait_for_timeout(500)
            page.click('#stageSwitch [data-stage="draft"]')
            page.wait_for_timeout(500)
            check("... and after Final and back to Draft", pressed() == [other["id"]], pressed())
        page.close()

        print()
        print("a remembered Final that this lane cannot run opens on the draft")
        live = http_json(URL + "api/engines?lane=rig")
        kinfo = next(m for m in live["video"]["modes"] if m["id"] == "wsloopkeep")
        check("(setup) the fixture keeper is unavailable on the fake lane", kinfo["available"] is False, kinfo.get("available"))
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        guard(page)
        page.add_init_script("localStorage.setItem('bwf.task.way.animate', JSON.stringify({cap: 'video', mode: 'ltx', stage: 'final'}))")
        page.goto(URL + "#task=video", wait_until="networkidle")
        page.wait_for_timeout(1500)
        check("the unavailable keeper is not restored: the task opens on its draft", page.evaluate("STATE.mode") == "ltx"
              and page.get_attribute('#stageSwitch [data-stage="draft"]', "aria-pressed") == "true", page.evaluate("STATE.mode"))
        page.close()

        print()
        print("Draft / Final: a keeper per number of pictures follows the attached pictures")
        page = open_page(browser, "#task=people")
        field = "ref_images"
        def attach(n):
            page.set_input_files("#upload_%s" % field,
                                 [{"name": "p%d.png" % i, "mimeType": "image/png", "buffer": b"x"} for i in range(n)])
            page.wait_for_function("([f, n]) => (STATE.uploads[f] || []).length === n", arg=[field, n], timeout=10000)
        check("people has a draft/final switch", page.is_visible("#stageSwitch"))
        page.click('#stageSwitch [data-stage="final"]')
        page.wait_for_timeout(500)
        check("no pictures yet: the lowest declared keeper (1 picture)", page.evaluate("STATE.mode") == "wspeople1", page.evaluate("STATE.mode"))
        attach(3)
        check("3 pictures attached while in Final: the keeper for 3", page.evaluate("STATE.mode") == "wspeople3", page.evaluate("STATE.mode"))
        check("... and the pictures came along", page.evaluate("(STATE.uploads['ref_images'] || []).length") == 3)
        page.click('#stageSwitch [data-stage="draft"]')
        page.wait_for_timeout(500)
        check("Draft is the people mode again", page.evaluate("STATE.mode") == "ref2v")
        check("... with its 3 pictures", page.evaluate("(STATE.uploads['ref_images'] || []).length") == 3)
        attach(2)
        page.click('#stageSwitch [data-stage="final"]')
        page.wait_for_timeout(500)
        check("2 pictures then FINAL: the keeper for 2", page.evaluate("STATE.mode") == "wspeople2", page.evaluate("STATE.mode"))
        page.click('#stageSwitch [data-stage="draft"]')
        page.wait_for_timeout(400)
        attach(7)
        page.click('#stageSwitch [data-stage="final"]')
        page.wait_for_timeout(500)
        check("7 pictures: clamped to the nearest lower keeper (3)", page.evaluate("STATE.mode") == "wspeople3", page.evaluate("STATE.mode"))
        page.close()

        print()
        print("History is per task; the monitor names what is left to fill in; the trail ticks what this page made")
        page = open_page(browser, "#task=animate")
        now = time.time()
        fixture = {"jobs": [
            {"id": "fx-anim", "lane": "rig", "lane_name": "Fixture", "kind": "video", "mode": "fl2va", "status": "running",
             "step": 1, "total": 10, "created": now, "outputs": [], "prompt": "fixture animate"},
            {"id": "fx-song", "lane": "rig", "lane_name": "Fixture", "kind": "audio", "mode": "song", "status": "queued",
             "step": 0, "total": 0, "created": now - 5, "outputs": [], "prompt": "fixture song"},
        ], "log": [], "now": now}
        page.route("**/api/jobs*", lambda r, q: r.fulfill(status=200, content_type="application/json", body=json.dumps(fixture)))
        page.wait_for_timeout(2600)
        toggle = page.inner_text("#historyToggle").upper()
        check("the toggle reads This task (1) / Everything (2)", "THIS TASK (1)" in toggle and "EVERYTHING (2)" in toggle, toggle)
        check("This task lists only the animate job",
              page.eval_on_selector_all("#binBody tr", "e => e.map(r => r.dataset.job)") == ["fx-anim"])
        page.click('#historyToggle [data-hist-scope="all"]')
        page.wait_for_timeout(200)
        page.click('#binBody tr[data-job="fx-song"]')
        page.wait_for_timeout(500)
        check("a row from another task opens that task", page.evaluate("STATE.task") == "song"
              and page.evaluate("location.hash") == "#task=song", page.evaluate("location.hash"))
        page.unroute("**/api/jobs*")
        fixture["jobs"].append({"id": "fx-loop", "lane": "rig", "lane_name": "Fixture", "kind": "video", "mode": "ltx_loop",
                                "status": "queued", "step": 0, "total": 0, "created": now - 9, "outputs": [], "prompt": "fixture loop"})
        page.evaluate("localStorage.setItem('bwf.task.way.video', JSON.stringify({cap: 'video', mode: 'ltx', stage: 'draft'}))")
        page.route("**/api/jobs*", lambda r, q: r.fulfill(status=200, content_type="application/json", body=json.dumps(fixture)))
        page.click('#roomStrip [data-group="PICTURE"]')
        page.wait_for_timeout(300)
        page.click('#roomStrip [data-group="MOTION"]')
        page.click('#roomStrip [data-task-id="animate"]')
        page.wait_for_timeout(2600)
        page.click('#historyToggle [data-hist-scope="all"]')
        page.click('#binBody tr[data-job="fx-loop"]')
        page.wait_for_timeout(600)
        check("a History row opens its task on the job's own way, not the remembered one",
              page.evaluate("STATE.task") == "video" and page.evaluate("STATE.mode") == "ltx_loop",
              [page.evaluate("STATE.task"), page.evaluate("STATE.mode")])
        page.unroute("**/api/jobs*")
        page.close()

        page = open_page(browser, "#task=t2i")
        mon = page.inner_text("#monitor")
        check("the monitor says what the task is", by_id["t2i"]["line"] in mon, mon)
        check("... and names the field still empty", "Fill in Prompt, then press Make." in mon, mon)
        check("... no 'Which one?' list any more", "Which one?" not in mon and page.query_selector("#monitor .guide-list") is None)
        page.fill("#promptBox", "a red kite")
        page.wait_for_timeout(200)
        check("... and turns to Ready once it is filled", "Ready. Press Make." in page.inner_text("#monitor"), page.inner_text("#monitor"))
        check("no music-video button on a sound-less task", page.query_selector("#forgeVideoFromFileBtn") is None)
        page.close()

        page = open_page(browser, "#task=animate")
        check("the draft pair reads 'Make a draft' in the sentence (an optional picture is not asked for)",
              "Fill in Prompt, then press Make a draft." in page.inner_text("#monitor"), page.inner_text("#monitor"))
        segs = page.eval_on_selector_all("#taskTrail [data-seg]", "els => els.map(e => e.textContent.trim())")
        check("the trail before: picture, task, Draft, Final -- nothing ticked",
              segs == ["Picture", "Animate", "Draft", "Final"], segs)
        page.set_input_files("#upload_first_frame", {"name": "a.png", "mimeType": "image/png", "buffer": b"x"})
        page.wait_for_function("() => (STATE.uploads['first_frame'] || []).length === 1", timeout=10000)
        check("a picture attached ticks the first segment", "✓" in page.inner_text('#taskTrail [data-seg="input"]'),
              page.inner_text("#taskTrail"))
        page.fill("#promptBox", "waves")
        n_gen = len(GENERATED)
        page.click("#makeBtn")
        until(page, lambda: len(GENERATED) > n_gen, "/api/generate")
        job = {"id": "ws-%d" % len(GENERATED), "lane": "rig", "lane_name": "Fake lane", "kind": "video", "mode": "fl2va",
               "status": "done", "step": 1, "total": 1, "created": time.time(), "prompt": "waves",
               "outputs": [{"filename": "x.mp4", "subfolder": "", "type": "output", "media": "video"}]}
        page.route("**/api/jobs*", lambda r, q: r.fulfill(status=200, content_type="application/json",
                                                         body=json.dumps({"jobs": [job], "log": [], "now": time.time()})))
        page.wait_for_function("() => (document.querySelector('#taskTrail [data-seg=\"draft\"]') || {textContent: ''}).textContent.includes('✓')", timeout=15000)
        check("a done job of the draft mode from this page ticks Draft",
              "✓" in page.inner_text('#taskTrail [data-seg="draft"]') and "✓" not in page.inner_text('#taskTrail [data-seg="final"]'),
              page.inner_text("#taskTrail"))
        page.unroute("**/api/jobs*")
        page.close()

        print()
        print("a task nothing fills is dimmed and says so; the music video carries its button")
        page = open_page(browser, "#task=textures")
        cls = page.get_attribute('#roomStrip [data-task-id="textures"]', "class")
        check("textures tab is dimmed with the plain title", "task-tab-dim" in cls
              and page.get_attribute('#roomStrip [data-task-id="textures"]', "title") == "Nothing installed makes this yet", cls)
        check("... the workspace says so and offers no form", "Nothing installed makes this yet" in page.inner_text("#roomPlainNote")
              and not page.is_visible("#makeBtn"))
        page.click('#roomStrip [data-group="MOTION"]')
        page.wait_for_timeout(250)
        page.click('#roomStrip [data-task-id="musicvideo"]')
        page.wait_for_timeout(400)
        check("the music-video task shows its button on the monitor", page.is_visible("#forgeVideoFromFileBtn")
              and page.inner_text("#forgeVideoFromFileBtn") == "Make a music video")
        check("... and is not dimmed", "task-tab-dim" not in page.get_attribute('#roomStrip [data-task-id="musicvideo"]', "class"))
        page.click('#roomStrip [data-group="SOUND"]')
        page.wait_for_timeout(300)
        check("a sound task keeps its song-file way in, in its own words",
              page.inner_text("#forgeVideoFromFileBtn") == "Make a music video from a song file")
        page.click('#roomStrip [data-group="PICTURE"]')
        page.wait_for_timeout(300)
        check("a picture task carries no such button", page.query_selector("#forgeVideoFromFileBtn") is None)
        page.close()

        print()
        print("the Cutting Room and Workflows bars are destinations: groups only, no task tabs")
        for label, btn in (("Cutting Room", "#roomStrip .room-cutting"), ("Workflows", "#workflowsBtn")):
            page = open_page(browser, "#task=musicvideo")      # a task open first, so its group's tabs are drawn
            page.route("**/api/workflows*", lambda r, q: r.fulfill(status=200, content_type="application/json",
                                                                 body=json.dumps({"ok": True, "workflows": []})))   # this fake lane lists none
            page.click('#roomStrip [data-group="SOUND"]')
            page.wait_for_timeout(250)
            page.click(btn)
            page.wait_for_timeout(500)
            check("%s: no task tab in the bar" % label, page.query_selector("#roomStrip .task-tab") is None,
                  page.eval_on_selector_all("#roomStrip .task-tab", "e => e.map(x => x.textContent)"))
            check("%s: four group buttons, none pressed" % label,
                  page.eval_on_selector_all("#roomStrip .group-tab", "e => e.map(b => b.getAttribute('aria-pressed'))") == ["false"] * 4)
            check("%s: its own button is the only one pressed" % label,
                  page.eval_on_selector_all('#roomStrip [aria-pressed="true"]', "e => e.map(x => x.className + '|' + x.id)").__len__() == 1
                  and page.get_attribute(btn, "aria-pressed") == "true")
            check("%s: Everything is there and not pressed" % label,
                  page.get_attribute("#homeBtn", "aria-pressed") == "false" and "Everything" in page.inner_text("#homeBtn"))
            page.close()

        print()
        print("a long licence stamp wraps inside the screen at 390 wide")
        page = open_page(browser, "#task=animate", 390, 844)
        page.set_input_files("#upload_first_frame", {"name": "a.png", "mimeType": "image/png", "buffer": b"x"})
        page.wait_for_timeout(500)
        page.fill("#promptBox", "waves")
        page.click("#makeBtn")
        page.wait_for_timeout(600)
        job = {"id": "ws-%d" % len(GENERATED), "lane": "rig", "lane_name": "Fake lane", "kind": "video", "mode": "fl2va",
               "status": "done", "step": 1, "total": 1, "created": time.time(), "prompt": "waves",
               "licence": {"name": "LTX-2.X Community License Agreement", "shippable": False, "url": "https://example.com/licence"},
               "outputs": [{"filename": "x.mp4", "subfolder": "", "type": "output", "media": "video"}]}
        page.route("**/api/jobs*", lambda r, q: r.fulfill(status=200, content_type="application/json",
                                                         body=json.dumps({"jobs": [job], "log": [], "now": time.time()})))
        page.wait_for_function("() => Array.from(document.querySelectorAll('.licence-stamp')).some(e => e.offsetParent !== null && e.textContent.includes('LTX-2.X'))", timeout=15000)
        stamps = page.evaluate("""() => Array.from(document.querySelectorAll('.licence-stamp')).filter(e => e.offsetParent !== null).map(e => {
          const r = e.getBoundingClientRect(); return [e.textContent.trim().slice(0, 40), Math.round(r.left), Math.round(r.right)]; })""")
        check("a long licence stamp is on screen", len(stamps) > 0 and any("LTX-2.X" in x[0] for x in stamps), stamps)
        check("... and no stamp reaches past the 390 px viewport", all(x[1] >= 0 and x[2] <= 390 for x in stamps), stamps)
        page.close()

        print()
        print("a shot that continues another runs in the Cutting Room, not here")
        page = open_page(browser, "#task=continue")
        check("continue: the task is on screen", page.evaluate("STATE.task") == "continue")
        check("continue: the Cutting Room note is visible without opening anything",
              shown(page, "#cuttingOnlyNote") and "runs in the Cutting Room" in txt(page, "#cuttingOnlyNote"),
              txt(page, "#cuttingOnlyNote"))
        check("continue: the note has an Open the Cutting Room button",
              shown(page, "#openCuttingBtn") and "Open the Cutting Room" in txt(page, "#openCuttingBtn"))
        check("continue: Make is disabled, with the reason as its title",
              page.evaluate("document.getElementById('makeBtn').disabled") and "Cutting Room" in (page.evaluate("document.getElementById('makeBtn').title") or ""))
        check("continue: the ready sentence says it runs in the Cutting Room",
              txt(page, "#taskReady").strip() == "This one runs in the Cutting Room.", txt(page, "#taskReady"))
        page.evaluate("(document.getElementById('openCuttingBtn') || {click(){}}).click()")
        page.wait_for_timeout(800)
        check("continue: the button opens the Cutting Room", "room=cutting" in page.evaluate("location.hash"),
              page.evaluate("location.hash"))
        check("continue: inside the Cutting Room the note is gone", not shown(page, "#cuttingOnlyNote"))
        page.close()
        page = open_page(browser, "#task=animate")
        check("animate (control): Make is enabled", not page.evaluate("document.getElementById('makeBtn').disabled"))
        check("animate (control): no Cutting Room note", not shown(page, "#cuttingOnlyNote"))
        check("animate (control): the ready sentence asks for fields",
              "Cutting Room" not in txt(page, "#taskReady"), txt(page, "#taskReady"))
        page.close()

        print()
        print("nothing in the bar overflows")
        for w, h in ((1440, 900), (390, 844)):
            page = open_page(browser, "", w, h)
            for g in ("SOUND", "PICTURE", "MOTION", "OBJECT"):
                page.click('#roomStrip [data-group="%s"]' % g)
                page.wait_for_timeout(200)
                bad = page.evaluate("""(w) => Array.from(document.querySelectorAll('#roomStrip button')).filter(e => {
                  const r = e.getBoundingClientRect(); return r.left < 0 || r.right > w + 0.5; }).map(e => e.textContent.trim())""", w)
                check("%dx%d %s: every bar button sits inside the viewport" % (w, h, g), not bad, bad)
                check("%dx%d %s: the page does not scroll sideways" % (w, h, g),
                      page.evaluate("document.documentElement.scrollWidth") <= w)
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
