"""Browser gate for the "Make a music video from this song" button and dialog.

Covers: the monitor button's show/hide rules (finished song only), the dialog
(photo upload via /api/upload, style line, Start enabled only after the
upload), the ONE POST to /api/forge/music-video with lane / song_job / photo
/ style and nothing else, a 400 answer's sentence shown in the status line,
an ok answer's plan line, the progress view (bar, Stop POSTing to
/api/forge/stop, the Cutting Room link at done), the load-time notice bar
when a run is already running, and a 404-answering server (the route is
missing) leaving the page unbroken. NOTHING REACHES A MACHINE: every forge
route is intercepted with page.route.

Run: python3 tests/test_forge_video_ui.py
"""
import json
import os
import sys
import tempfile
import time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _ui_fixture as ui  # noqa: E402
from _ui_fixture import check, FAILED  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: F401

NOW = time.time()
SONG_JOB = {"id": "song1", "lane": "t", "lane_name": "Fake lane", "kind": "audio", "mode": "song",
            "status": "done", "prompt": "a slow neon-carnival song", "seed": 7, "created": NOW,
            "outputs": [{"filename": "fix.wav", "subfolder": "", "type": "output", "media": "audio"}]}
RUNNING_JOB = {"id": "song2", "lane": "t", "lane_name": "Fake lane", "kind": "audio", "mode": "song",
               "status": "running", "prompt": "still cooking", "seed": 8, "created": NOW + 1,
               "outputs": []}
PICTURE_JOB = {"id": "pic1", "lane": "t", "lane_name": "Fake lane", "kind": "image", "mode": "t2i",
               "status": "done", "prompt": "a golden dragon", "seed": 42, "created": NOW + 2,
               "outputs": [{"filename": "fix.png", "subfolder": "", "type": "output", "media": "image"}]}

URL = ui.start([PICTURE_JOB, RUNNING_JOB, SONG_JOB])

TMP = tempfile.mkdtemp(prefix="bwf_forge_video_ui_")
PHOTO = os.path.join(TMP, "photo.png")
with open(PHOTO, "wb") as f:
    f.write(ui.gradient_png(16, 16))

PLAN_LINE = "38 shots of about 4 seconds each, roughly 41 minutes in all (9 shots first, then the rest)."


def open_music_room(page):
    page.goto(URL + "#room=music", wait_until="networkidle", timeout=30000)
    page.wait_for_selector("#makeBtn", timeout=15000)
    page.click('#historyToggle button[data-hist-scope="all"]')
    page.wait_for_selector('tr[data-job="song1"]', timeout=15000)


try:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 800})

        # ------------------------------------------------------------------
        # 1. The button's show/hide rules; the load-time check stays quiet
        #    when the server has no /api/forge routes at all (this fixture
        #    server does not -- the real server is older than this task).
        # ------------------------------------------------------------------
        open_music_room(page)
        check("Notice bar hidden when there is no running run",
              page.evaluate("() => $('#forgeVideoNotice').hidden"))

        page.click('tr[data-job="pic1"]')   # finished picture -> jumps to the Picture room
        page.wait_for_selector('#monitor [id="shareBtn"]', timeout=15000)
        check("Button hidden for a picture job",
              not page.evaluate("() => !!$('#forgeVideoBtn')"))

        page.click('#historyToggle button[data-hist-scope="all"]')
        page.click('tr[data-job="song2"]')  # unfinished song -> back to the Music room
        page.wait_for_selector('tr[data-job="song1"]', timeout=15000)
        page.wait_for_timeout(400)
        check("Button hidden for an unfinished song",
              not page.evaluate("() => !!$('#forgeVideoBtn')"))

        page.click('tr[data-job="song1"]')
        page.wait_for_selector('#forgeVideoBtn', timeout=15000)
        check("Button visible for a finished song", page.is_visible("#forgeVideoBtn"))
        check("Button says it in plain words",
              page.inner_text("#forgeVideoBtn") == "Make a music video from this song")

        # ------------------------------------------------------------------
        # 2. The dialog opens; Start is disabled until the photo is uploaded.
        # ------------------------------------------------------------------
        page.click("#forgeVideoBtn")
        check("Clicking it opens the dialog", page.evaluate("() => $('#forgeVideoDialog').open"))
        check("Dialog carries the form",
              page.evaluate("() => !!$('#forgeVideoPhoto') && !!$('#forgeVideoStyle') && !!$('#forgeVideoStart')"))
        check("Start is disabled before a photo",
              page.evaluate("() => $('#forgeVideoStart').disabled"))

        uploads = []

        def route_upload(route):
            uploads.append(1)
            route.fulfill(status=200, headers={"Content-Type": "application/json"},
                          json={"ok": True, "files": [{"name": "photo.png", "original": "photo.png"}]})

        page.route("**/api/upload*", route_upload)
        page.set_input_files("#forgeVideoPhoto", PHOTO)
        page.wait_for_function("() => !$('#forgeVideoStart').disabled", timeout=10000)
        check("One upload request, the page's existing /api/upload helper", len(uploads) == 1)

        # ------------------------------------------------------------------
        # 3. A 400 answer shows its own sentence; the dialog stays open in
        #    the form view, saying it in the status line.
        # ------------------------------------------------------------------
        page.fill("#forgeVideoStyle", "neon carnival at night")
        bad_posts = []

        def route_bad(route):
            bad_posts.append(json.loads(route.request.post_data))
            route.fulfill(status=400, headers={"Content-Type": "application/json"},
                          json={"ok": False, "error": "Add a photo of who is in the video."})

        page.route("**/api/forge/music-video*", route_bad)
        page.click("#forgeVideoStart")
        page.wait_for_function("() => { const m = $('#forgeVideoMsg'); return m && m.textContent.trim(); }",
                               timeout=10000)
        check("The 400's sentence is shown in the status line",
              page.evaluate("() => $('#forgeVideoMsg').textContent") == "Add a photo of who is in the video.")
        check("The dialog stays open in the form view",
              page.evaluate("() => $('#forgeVideoDialog').open && !$('#forgeVideoBar')"))

        # ------------------------------------------------------------------
        # 4. An ok answer: ONE POST with lane / song_job / photo / style and
        #    nothing else; the plan line shows; polling updates the bar.
        # ------------------------------------------------------------------
        start_posts = []

        def route_start_ok(route):
            start_posts.append(json.loads(route.request.post_data))
            route.fulfill(status=200, headers={"Content-Type": "application/json"},
                          json={"ok": True, "run_id": "run1", "plan_line": PLAN_LINE,
                                "shots": 38, "sequence": "s_v1"})

        page.unroute("**/api/forge/music-video*", route_bad)
        page.route("**/api/forge/music-video*", route_start_ok)

        polls = []

        def route_poll(route):
            polls.append(1)
            running = {"id": "run1", "kind": "music-video", "status": "running", "stage": "stills",
                       "message": "Making the pictures", "done": 3, "total": 10,
                       "plan_line": PLAN_LINE, "sequence": "s_v1"}
            done = dict(running, status="done", stage="done", message="Done", done=10,
                        total=10, cut_id="c1", file="cut.mp4")
            route.fulfill(status=200, headers={"Content-Type": "application/json"},
                          json=running if len(polls) < 3 else done)

        page.route("**/api/forge/run*", route_poll)

        page.click("#forgeVideoStart")
        page.wait_for_selector("#forgeVideoBar", timeout=10000)
        check("Start sent ONE POST to /api/forge/music-video", len(start_posts) == 1)
        if start_posts:
            body = start_posts[0]
            check("POST lane is the active lane", body.get("lane") == "t")
            check("POST song_job is the selected finished song", body.get("song_job") == "song1")
            check("POST photo is the uploaded lane file name", body.get("photo") == "photo.png")
            check("POST style is the typed line", body.get("style") == "neon carnival at night")
            check("POST carries nothing else", set(body) == {"lane", "song_job", "photo", "style"})
        check("The plan line is shown", PLAN_LINE in page.inner_text("#forgeVideoBody"))
        page.wait_for_function("() => { const b = $('#forgeVideoBar'); return b && b.value === 3 && b.max === 10; }",
                               timeout=10000)
        check("Polling updates the progress bar to 3 of 10",
              page.evaluate("() => [$('#forgeVideoBar').value, $('#forgeVideoBar').max]") == [3, 10])

        # ------------------------------------------------------------------
        # 5. Done: the plain ready message and the Cutting Room link, which
        #    opens the Cutting Room with the run's sequence selected.
        # ------------------------------------------------------------------
        page.wait_for_function("() => !!$('#forgeVideoCutLink')", timeout=20000)
        check("Done shows the plain ready message",
              "Your video is ready." in page.inner_text("#forgeVideoBody"))
        check("Done stops offering Stop", not page.evaluate("() => !!$('#forgeVideoStop')"))
        page.click("#forgeVideoCutLink")
        page.wait_for_function("() => location.hash.indexOf('seq=s_v1') >= 0", timeout=15000)
        check("The link lands in the Cutting Room with the run's sequence",
              page.evaluate("() => location.hash").startswith("#room=cutting"))
        check("The dialog closed behind it",
              not page.evaluate("() => $('#forgeVideoDialog').open"))

        # ------------------------------------------------------------------
        # 6. Stop: a fresh run (the dialog reopens in the form view, the
        #    photo is still there) posts {id} to /api/forge/stop, and the
        #    record's stopped message shows.
        # ------------------------------------------------------------------
        page.goto(URL + "#room=music", wait_until="networkidle", timeout=30000)
        page.wait_for_selector("#makeBtn", timeout=15000)
        page.click('#historyToggle button[data-hist-scope="all"]')
        page.click('tr[data-job="song1"]')
        page.wait_for_selector("#forgeVideoBtn", timeout=15000)
        page.click("#forgeVideoBtn")
        page.wait_for_function("() => !$('#forgeVideoStart').disabled", timeout=10000)
        check("Reopened dialog keeps the uploaded photo (Start enabled)",
              page.evaluate("() => !$('#forgeVideoStart').disabled"))

        stop_posts = []

        def route_start2(route):
            route.fulfill(status=200, headers={"Content-Type": "application/json"},
                          json={"ok": True, "run_id": "run2", "plan_line": PLAN_LINE,
                                "shots": 38, "sequence": "s_v2"})

        def route_stop(route):
            stop_posts.append(json.loads(route.request.post_data))
            route.fulfill(status=200, headers={"Content-Type": "application/json"}, json={"ok": True})

        poll2 = []

        def route_poll2(route):
            # running for the first two ticks (so the Stop button gets a
            # moment to be real), stopped from the third on.
            poll2.append(1)
            running = {"id": "run2", "kind": "music-video", "status": "running", "stage": "stills",
                       "message": "Making the pictures", "done": 1, "total": 10,
                       "plan_line": PLAN_LINE, "sequence": "s_v2"}
            body = running if len(poll2) <= 2 else dict(running, status="stopped", stage="shots",
                                                        message="Stopped before the cut was finished.", done=2)
            route.fulfill(status=200, headers={"Content-Type": "application/json"}, json=body)

        page.unroute("**/api/forge/music-video*", route_start_ok)
        page.unroute("**/api/forge/run*", route_poll)
        page.route("**/api/forge/music-video*", route_start2)
        page.route("**/api/forge/run*", route_poll2)
        page.route("**/api/forge/stop*", route_stop)

        page.click("#forgeVideoStart")
        page.wait_for_selector("#forgeVideoStop", timeout=10000)
        page.click("#forgeVideoStop")
        page.wait_for_timeout(800)
        check("Stop posts the run id to /api/forge/stop",
              len(stop_posts) == 1 and stop_posts[0] == {"id": "run2"})
        page.wait_for_function(
            "() => $('#forgeVideoBody').textContent.indexOf('Stopped before the cut was finished.') >= 0",
            timeout=15000)
        check("The record's stopped message shows",
              "Stopped before the cut was finished." in page.inner_text("#forgeVideoBody"))
        check("Stop is gone once the run has stopped", not page.evaluate("() => !!$('#forgeVideoStop')"))
        page.close()

        # ------------------------------------------------------------------
        # 7. Load with a run already running: the notice bar, and "Show"
        #    opens the dialog straight in the progress view.
        # ------------------------------------------------------------------
        page2 = browser.new_page(viewport={"width": 1280, "height": 800})
        running7 = {"id": "run7", "kind": "music-video", "status": "running", "stage": "shots",
                    "message": "Animating the shots", "done": 4, "total": 10,
                    "plan_line": PLAN_LINE, "sequence": "s_v7"}

        def route_latest(route):
            # the server's real shape: the bare record, with or without an id (forge_run_get)
            route.fulfill(status=200, headers={"Content-Type": "application/json"}, json=running7)

        page2.route("**/api/forge/run*", route_latest)
        page2.goto(URL + "#room=music", wait_until="networkidle", timeout=30000)
        page2.wait_for_selector("#makeBtn", timeout=15000)
        page2.wait_for_function("() => !$('#forgeVideoNotice').hidden", timeout=15000)
        check("Notice bar says it in plain words",
              "A music video is being made..." in page2.inner_text("#forgeVideoNotice"))
        page2.click("#forgeVideoNoticeShow")
        check("Show opens the dialog in the progress view",
              page2.evaluate("() => $('#forgeVideoDialog').open && !!$('#forgeVideoBar')"))
        check("The notice bar is gone after Show",
              page2.evaluate("() => $('#forgeVideoNotice').hidden"))
        page2.close()

        # ------------------------------------------------------------------
        # 8. An OLDER server: every /api/forge route answers 404. The page
        #    must not break and must not throw from its own code.
        # ------------------------------------------------------------------
        page3 = browser.new_page(viewport={"width": 1280, "height": 800})
        errors = []
        page3_errors = []
        page3.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page3.on("pageerror", lambda e: page3_errors.append(str(e)))

        def route_404(route):
            route.fulfill(status=404, headers={"Content-Type": "application/json"},
                          json={"error": "no such route"})

        page3.route("**/api/forge*", route_404)
        page3.goto(URL + "#room=music", wait_until="networkidle", timeout=30000)
        page3.wait_for_selector("#makeBtn", timeout=15000)
        page3.wait_for_timeout(800)
        check("The 404-answering server leaves the notice bar hidden",
              page3.evaluate("() => $('#forgeVideoNotice').hidden"))
        check("The page still renders its monitor", page3.evaluate("() => !!$('#monitor')"))
        check("no uncaught error from the page's own code", page3_errors == [], page3_errors[:5])
        # Chromium logs a bare "Failed to load resource: 404" for ANY 404 fetch --
        # that is the network stack talking, not the page's code. So the honest
        # check is: nothing OTHER than that expected 404 line is in the log.
        check("the only console error is the expected 404 resource line",
              len(errors) == 1 and "404" in errors[0], errors[:5])
        page3.close()

        browser.close()
        print("All forge video UI checks complete")

except Exception as e:
    print("Exception: %s" % e)
    import traceback
    traceback.print_exc()
    FAILED.append(str(e))
finally:
    ui.finish()
