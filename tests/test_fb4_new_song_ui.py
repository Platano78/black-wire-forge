"""Browser gate for FB-4: "New song" (New picture, ...) -- a clean slate in one click.
In every room with a guide and a form (not the Cutting Room) a visible button in the
guide's header row, compact or expanded, aborts any guide call, clears the room's
conversation (server-recorded), resets the mode's fields to their defaults, drops the
uploads, closes What the guide filled in, and keeps the engine, the lane and History.
One line, "Started fresh. Undo", restores all of it for 10 s or until the person types
or sends anything.

Set BWF_FB4_SHOTS=<dir> to save the Music room after the click, with the Undo line, at 1280 wide.

Run: python3 tests/test_fb4_new_song_ui.py
"""
import json
import os
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _ui_fixture as ui  # noqa: E402
import _picture_server as ps  # noqa: E402
from _ui_fixture import check  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

SHOTS = os.environ.get("BWF_FB4_SHOTS")
REPO = ui.REPO

helper = ThreadingHTTPServer(("127.0.0.1", 0), ps.FakeHelper)
threading.Thread(target=helper.serve_forever, daemon=True).start()
store = os.path.join(ui.SCRATCH, "lane")
os.makedirs(os.path.join(store, "outputs"), exist_ok=True)
with open(os.path.join(store, "outputs", "fix.png"), "wb") as f:
    f.write(ui.gradient_png(160, 120))
lane_port = ui.free_port()
ui.PROCS.append(subprocess.Popen([sys.executable, os.path.join(ui.HERE, "fixtures", "fake_comfy.py"), "--port",
                                  str(lane_port), "--store", store], cwd=REPO,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
ui.wait_up("http://127.0.0.1:%d/system_stats" % lane_port)

JOBS = [{"id": "songjob1", "lane": "t", "lane_name": "Fake lane", "kind": "audio", "mode": "song", "status": "done",
         "prompt": "an earlier country song", "seed": 3, "created": time.time() - 60,
         "outputs": [{"filename": "fix.png", "subfolder": "", "type": "output", "media": "image"}]},
        {"id": "picjob1", "lane": "t", "lane_name": "Fake lane", "kind": "image", "mode": "t2i", "status": "done",
         "prompt": "an earlier picture", "seed": 7, "created": time.time() - 30,
         "outputs": [{"filename": "fix.png", "subfolder": "", "type": "output", "media": "image"}]}]


def start_server(name):
    port = ui.free_port()
    data = os.path.join(ui.SCRATCH, "data_" + name)
    os.makedirs(data, exist_ok=True)
    with open(os.path.join(data, "jobs.json"), "w") as f:
        json.dump(JOBS, f)
    cfg = {"title": "fb4 ui", "port": port, "bind": "127.0.0.1",
           "lanes": [{"id": "t", "name": "Fake lane", "host": "127.0.0.1", "port": lane_port,
                      "caps": ["image", "video", "audio"],
                      "models": {"ace_unet": "ace.safetensors", "ace_clip1": "a.safetensors",
                                 "ace_clip2": "b.safetensors", "ace_vae": "v.safetensors"}}],
           "helper": {"url": "http://127.0.0.1:%d/v1" % helper.server_address[1], "model": "test-model",
                      "timeout_s": 10, "vision": False, "guide_default": "on"},
           "timing": {"poll_seconds": 0.5, "job_poll_seconds": 1.0}}
    path = os.path.join(ui.SCRATCH, "config_%s.json" % name)
    with open(path, "w") as f:
        json.dump(cfg, f)
    env = dict(os.environ, GENCENTER_CONFIG=path, GENCENTER_DATA=data)
    ui.PROCS.append(subprocess.Popen([sys.executable, os.path.join(REPO, "server.py")], cwd=REPO, env=env,
                                     stdout=open(os.path.join(ui.SCRATCH, "server_%s.log" % name), "w"),
                                     stderr=subprocess.STDOUT))
    url = "http://127.0.0.1:%d/" % port
    ui.wait_up(url + "api/health")
    return url


URL = start_server("brain")
SONG = ("TAGS: country, slow, pedal steel, sad male vocals\nBPM: 76\nKEY: NONE\nDURATION: 150\nTIMESIG: NONE\n"
        "LANGUAGE: NONE\nLYRICS:\n[Verse]\nthe porch light is on\n\n[Chorus]\nbut nobody's home")
NEW = "#guideNewBtn"
UNDO = "#guideNewUndo"
PNG = {"name": "ref.png", "mimeType": "image/png", "buffer": ps.PNG}


def enter(page, room_id, mode=None):
    page.goto("about:blank")
    page.goto(URL + "#room=" + room_id, wait_until="networkidle", timeout=30000)
    page.wait_for_function("() => typeof GUIDE !== 'undefined' && GUIDE && GUIDE.guide", timeout=15000)
    if mode and page.evaluate("STATE.mode") != mode:
        page.evaluate("() => setEngineChipOpen(true)")
        page.check('#enginePicker input[data-mode="%s"]' % mode)
    page.wait_for_timeout(600)


def values(page):
    return page.evaluate("() => Object.fromEntries(Array.from(document.querySelectorAll('#inspector [data-field-id]'))"
                         ".map(e => [e.dataset.fieldId, e.type === 'checkbox' ? e.checked : e.value]))")


def msgs(page, who):
    return page.eval_on_selector_all("#guideLog .guide-msg.from-%s .guide-text" % who, "els => els.map(e => e.textContent)")


def settle(page):
    page.wait_for_timeout(300)
    page.wait_for_function("() => document.querySelector('#guideThinking').hidden", timeout=15000)
    page.wait_for_timeout(400)


def vis(page, sel):
    return page.is_visible(sel)


OPEN = "() => !!(document.querySelector('#guideFilled') || {}).open"

try:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        print("1: Music, compact view: New song is visible; one click is a clean slate")
        enter(page, "music", "song")
        check("compact view", page.evaluate("document.querySelector('#guidePanel').dataset.expanded") == "false")
        check("New song is visible in the compact view", vis(page, NEW) and page.inner_text(NEW) == "New song",
              page.inner_text(NEW) if page.query_selector(NEW) else None)
        check("the Undo line is not there yet", not vis(page, UNDO))
        defaults = values(page)
        engine = page.evaluate("[STATE.cap, STATE.mode, STATE.activeLane]")
        ps.HELPER_STATE["replies"][:] = [SONG]
        page.fill("#guideInput", "a sad country song")
        page.press("#guideInput", "Enter")
        settle(page)
        page.fill("#f_bpm", "72")
        drafted = values(page)
        check("setup: the draft filled the fields and one hand edit stuck",
              drafted["bpm"] == 72 or drafted["bpm"] == "72", drafted)
        check("setup: the conversation has turns and What the guide filled in is open",
              len(msgs(page, "user")) >= 1 and page.evaluate(OPEN))
        edited_before = page.evaluate("Array.from(STATE.editedFields).sort()")
        check("setup: the hand edit is marked", "bpm" in edited_before, edited_before)
        page.click(NEW)
        page.wait_for_selector(UNDO + ":not([hidden])", timeout=5000)
        check("click: the conversation is empty", msgs(page, "user") == [] and
              page.evaluate("guideHistLoad(guideHistKey()).length") == 0, msgs(page, "user"))
        now = values(page)
        check("click: every field is back at its default", now == defaults,
              {k: (defaults.get(k), v) for k, v in now.items() if defaults.get(k) != v})
        check("click: the edited-field marks are cleared", page.evaluate("STATE.editedFields.size") == 0)
        check("click: What the guide filled in is closed", not page.evaluate(OPEN))
        check("click: the engine and lane stay", page.evaluate("[STATE.cap, STATE.mode, STATE.activeLane]") == engine)
        check("click: focus is in the guide's box", page.evaluate("document.activeElement && document.activeElement.id")
              == "guideInput")
        check("click: the Undo line reads Started fresh. Undo",
              page.inner_text(UNDO).replace("\n", " ").strip() == "Started fresh. Undo", page.inner_text(UNDO))
        check("click: Undo is a button", page.evaluate("() => document.querySelector('#guideNewUndo button').textContent")
              == "Undo")
        check("click: the server cleared the history too", page.evaluate(
            "async () => (await getJSON('/api/guide/history?key=' + encodeURIComponent(guideHistKey()))).history.length") == 0)
        if SHOTS:
            os.makedirs(SHOTS, exist_ok=True)
            page.screenshot(path=os.path.join(SHOTS, "music-new-song-undo-1280.png"))

        print("2: Undo within 10 s restores the conversation, the values and the edit")
        page.click("#guideNewUndoBtn")
        page.wait_for_timeout(700)
        check("undo: the Undo line is gone", not vis(page, UNDO))
        check("undo: the conversation is back", "a sad country song" in msgs(page, "user"), msgs(page, "user"))
        check("undo: the values are back (bpm edit included)", values(page) == drafted,
              {k: (drafted.get(k), v) for k, v in values(page).items() if drafted.get(k) != v})
        check("undo: the edited mark is back", page.evaluate("Array.from(STATE.editedFields).sort()") == edited_before)
        check("undo: What the guide filled in is open again", page.evaluate(OPEN))
        page.wait_for_timeout(900)   # the debounced push has reached the server, at its new generation
        check("undo: the server has the conversation again", page.evaluate(
            "async () => (await getJSON('/api/guide/history?key=' + encodeURIComponent(guideHistKey()))).history.length") >= 2)
        page.reload(wait_until="networkidle")
        page.wait_for_function("() => typeof GUIDE !== 'undefined' && GUIDE && GUIDE.guide", timeout=15000)
        page.wait_for_timeout(600)
        check("undo: and a reload keeps it", "a sad country song" in msgs(page, "user"))

        print("3: typing after the click removes the Undo")
        page.click(NEW)
        page.wait_for_selector(UNDO + ":not([hidden])", timeout=5000)
        page.type("#guideInput", "x")
        check("typing: the Undo line is gone", not vis(page, UNDO))
        page.click(NEW)
        page.wait_for_selector(UNDO + ":not([hidden])", timeout=5000)
        page.evaluate("() => { document.querySelector('#guideFilled').open = true; }")
        page.click("#promptBox")
        page.keyboard.type("y")
        check("typing in a field: the Undo line is gone", not vis(page, UNDO))
        page.click(NEW)
        page.wait_for_selector(UNDO + ":not([hidden])", timeout=5000)
        page.wait_for_timeout(10600)
        check("after 10 s the Undo line goes by itself", not vis(page, UNDO))
        check("New song shows in the expanded view too", (page.click("#guideExpandBtn") or True) and vis(page, NEW))

        print("5: History still lists the earlier job after the click")
        page.wait_for_selector('#binBody tr[data-job="songjob1"]', timeout=15000)
        check("history: the earlier song is listed before", True)
        page.click(NEW)
        page.wait_for_selector(UNDO + ":not([hidden])", timeout=5000)
        check("history: the earlier song is still listed after", page.query_selector('#binBody tr[data-job="songjob1"]') is not None)
        check("history: and the job is still on the server", page.evaluate(
            "async () => ((await getJSON('/api/jobs')).jobs || (await getJSON('/api/jobs')) || []).some(j => j.id === 'songjob1')"))

        print("4: Picture room: New picture clears an uploaded picture; the Cutting Room shows no such button")
        enter(page, "picture", "edit")
        check("picture: the button says New picture", vis(page, NEW) and page.inner_text(NEW) == "New picture",
              page.inner_text(NEW) if page.query_selector(NEW) else None)
        fid = page.evaluate("() => { const f = fieldsFor(currentMode()).find(f => f.type === 'image' || f.type === 'image_list');"
                            " return f && f.id; }")
        page.set_input_files("#upload_" + fid, PNG)
        page.wait_for_function("f => (STATE.uploads[f] || []).length === 1", arg=fid, timeout=15000)
        page.click(NEW)
        page.wait_for_selector(UNDO + ":not([hidden])", timeout=5000)
        check("picture: the uploaded picture is gone", page.evaluate("f => (STATE.uploads[f] || []).length", fid) == 0
              and page.eval_on_selector_all("#thumbs_" + fid + " .t", "e => e.length") == 0)
        check("picture: the mode stays", page.evaluate("STATE.mode") == "edit")
        page.click("#guideNewUndoBtn")
        page.wait_for_timeout(500)
        check("picture: Undo puts the picture back", page.evaluate("f => (STATE.uploads[f] || []).length", fid) == 1
              and page.eval_on_selector_all("#thumbs_" + fid + " .t", "e => e.length") == 1)
        for rid, word in (("cover", "New song"), ("sfx", "New sound"), ("characters", "New sheet"),
                          ("video", "New video"), ("3d", "New model"), ("pixelart", "New picture")):
            enter(page, rid)
            check("%s: %s" % (rid, word), vis(page, NEW) and page.inner_text(NEW) == word,
                  page.inner_text(NEW) if page.query_selector(NEW) else None)
        enter(page, "cutting")
        check("cutting: no New button", not vis(page, NEW))
        check("no page errors", errors == [], errors)
        page.close()
        browser.close()
finally:
    ui.finish()
