"""Browser gate for FB-1: (A) the engine picker names each mode's model (and the
file the lane resolved) and the chip's title says the same; (B) the double-Make
guard covers a Cutting Room slot Make, and "Make another" on it is a NORMAL slot
Make that never sends confirm:true.

Run: python3 tests/test_fb1_ui.py
"""
import json
import os
import sys
import time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _ui_fixture as ui  # noqa: E402
from _ui_fixture import check  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

SEQ = "s_0000fb01"
NOW = time.time()
DATA = os.path.join(ui.SCRATCH, "data")
os.makedirs(os.path.join(DATA, "sequences"))
P1 = {"id": "p1", "lane": "picture", "beat_id": None, "cap": "image", "mode": "t2i",
      "recipe": None, "quality": None, "values": {"prompt": "a lighthouse"}, "refs": "auto",
      "takes": [], "pick": None, "trim": None, "title": None}
with open(os.path.join(DATA, "sequences", SEQ + ".json"), "w") as f:
    json.dump({"id": SEQ, "schema": 1, "rev": 1, "title": "FB-1", "mode": "sequence",
               "created": NOW, "updated": NOW, "canvas": {"width": 1024, "height": 576},
               "refs": [], "beats": [], "slots": [P1], "cables": [], "cuts": []}, f)
URL = ui.start([])

GEN = []   # bodies POSTed to /api/sequence/generate
JOB = {"job": None}


def fake_generate(route, request):
    GEN.append(json.loads(request.post_data or "{}"))
    job = {"id": "job-fb1-%d" % len(GEN), "lane": "t", "lane_name": "Fake lane", "kind": "image",
           "mode": "t2i", "status": "running", "step": 0, "total": 0, "created": time.time(),
           "outputs": [], "progress": {"state": "loading", "stage": 0, "stages": 1,
                                       "percent": 0.0, "elapsed": 0.0}}
    JOB["job"] = job
    route.fulfill(status=200, content_type="application/json",
                  body=json.dumps({"ok": True, "job": job}))


def fake_jobs(route, request):
    jobs = [JOB["job"]] if JOB["job"] else []
    route.fulfill(status=200, content_type="application/json",
                  body=json.dumps({"jobs": jobs, "log": [], "now": time.time()}))


def api(path):
    import urllib.request
    with urllib.request.urlopen(URL + path, timeout=10) as r:
        return json.loads(r.read().decode())


try:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append("PAGEERROR: %s" % e))
        page.route("**/api/sequence/generate", fake_generate)
        page.route("**/api/jobs*", fake_jobs)

        print("A: the picker rows name the model")
        page.goto(URL + "#room=music", wait_until="networkidle", timeout=30000)
        page.wait_for_selector("#engineChipBtn", state="visible", timeout=10000)
        page.wait_for_function(
            "() => { const r = document.querySelector('#enginePicker input[data-mode=\"song\"]'); "
            "return r && !r.disabled; }", timeout=20000)
        modes = {m["id"]: m for m in api("api/engines?lane=t")["audio"]["modes"]}
        rows = page.eval_on_selector_all(
            "#enginePicker .engine-row",
            "els => els.map(e => [e.querySelector('input').dataset.mode, "
            "(e.querySelector('.erow-model') || {}).textContent || null])")
        check("the picker has a row per music mode", len(rows) >= 2, rows)
        check("at least one row carries a model line", any(t for _, t in rows), rows)
        for mode, text in rows:
            m = modes[mode]
            want = (m["model"] + (" · " + m["model_file"] if m.get("model_file") else "")) if m.get("model") else None
            check("row %s shows its model line" % mode, text == want, (text, want))
        song = modes["song"]
        check("song has a model name and a resolved file on the fake lane",
              bool(song.get("model")) and bool(song.get("model_file")) and "/" not in song["model_file"], song)
        cur_mode = page.evaluate("STATE.mode")
        cm = modes[cur_mode]
        want_title = (cm.get("model") or "") + (" · " + cm["model_file"] if cm.get("model_file") else "")
        check("the chip's title is the current mode's model line",
              page.get_attribute("#engineChipBtn", "title") == want_title,
              (page.get_attribute("#engineChipBtn", "title"), want_title))

        print("B: the slot Make double-press guard")
        page.goto(URL + "#room=cutting&seq=" + SEQ, wait_until="networkidle", timeout=30000)
        page.wait_for_selector('[data-slot-id="p1"]', timeout=15000)
        page.click('[data-slot-id="p1"]')
        page.wait_for_selector("#makeBtn", state="visible", timeout=10000)
        page.wait_for_function("() => STATE.activeLane && !document.querySelector('#makeBtn').disabled", timeout=15000)
        page.click("#makeBtn")
        page.wait_for_function("() => STATE.jobs.some(j => j.id === 'job-fb1-1')", timeout=10000)
        check("first slot Make posts once, no dupe notice", len(GEN) == 1 and page.is_hidden("#makeDupeNotice"), GEN)
        page.wait_for_function("() => !document.querySelector('#makeBtn').disabled", timeout=10000)
        page.click("#makeBtn")
        page.wait_for_timeout(600)
        check("second identical slot Make shows the dupe notice", page.is_visible("#makeDupeNotice"))
        check("...and did not post", len(GEN) == 1, GEN)
        page.click("#makeDupeAnywayBtn")
        try:
            page.wait_for_function("() => true", timeout=100)
            deadline = time.time() + 8
            while len(GEN) < 2 and time.time() < deadline:
                page.wait_for_timeout(200)
        except Exception:
            pass
        check("Make another posts a second slot Make", len(GEN) == 2, GEN)
        check("...without confirm:true", len(GEN) == 2 and "confirm" not in GEN[1], GEN)
        check("...for the same slot", len(GEN) == 2 and GEN[1].get("slot_id") == "p1", GEN)

        print("B2: an edit made inside the 600 ms autosave window is not a duplicate")
        page.wait_for_function("() => !document.querySelector('#makeBtn').disabled", timeout=10000)
        page.wait_for_timeout(1500)   # let any pending autosave settle; the job stays running
        n0 = len(GEN)
        edited = page.evaluate("""() => {
          const el = document.querySelector('#inspector [data-field-id="prompt"]')
                  || document.querySelector('#inspector textarea, #inspector input[type=text]');
          if(!el) return false;
          el.value = el.value + ' at dusk';
          el.dispatchEvent(new Event('input', {bubbles: true}));
          document.querySelector('#makeBtn').click();   // same tick: well under 600 ms
          return true;
        }""")
        check("the field was edited", edited)
        deadline = time.time() + 8
        while len(GEN) <= n0 and time.time() < deadline:
            page.wait_for_timeout(200)
        check("an edited shot Make posts", len(GEN) == n0 + 1, GEN)
        check("...and shows no dupe notice", page.is_hidden("#makeDupeNotice"))
        check("no page errors", not errors, errors)
        browser.close()
finally:
    ui.finish()
