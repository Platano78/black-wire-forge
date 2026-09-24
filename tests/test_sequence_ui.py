"""Browser acceptance gate for C3.3 -- the sequence UI in the Cutting Room.
(the internal sequence/storyboard design spec, section 8 row C3.3.)

Drives the real page in a real browser against its own fake ComfyUI lane
(tests/fixtures/fake_comfy.py) in ACCEPT mode, and its own server.py
subprocess (scratch config + scratch data dir, both on random free ports).
Nothing reaches a real machine, and no check may be skipped.

Run: python3 tests/test_sequence_ui.py
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
LANE_ID = "t"

URL = None
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

def http_json(url, timeout=3, data=None, method=None):
    req = urllib.request.Request(url, data=data, method=method,
                                  headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
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
except ImportError:
    # Finding #21: absence of an OPTIONAL dev dependency is a SKIP, not a
    # failure -- a clean clone with no playwright must not read as broken.
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

# A 1x1 PNG, reused two ways below: (a) the fake lane's canned render output,
# and (b) a real local file this test can hand to an <input type=file> when
# the Cutting Room's own "first available mode in room order" (§2 -- the
# page picks it, never this test) turns out to be an upload-only tool
# (e.g. cutout) rather than a prompt mode.
PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753"
    "de0000000c4944415478da6360606060000000050001a5f645400000000049454e44ae426082")

# --- our own fake lane, ACCEPT mode armed for a single picture output ------
fake_port = free_port()
fake_store = tempfile.mkdtemp(prefix="bwf-seq-ui-fake-")
os.makedirs(os.path.join(fake_store, "outputs"), exist_ok=True)
with open(os.path.join(fake_store, "outputs", "shot.png"), "wb") as f:
    f.write(PNG_1X1)   # <img> loads without a broken-image console error
fake = subprocess.Popen(
    [sys.executable, os.path.join(HERE, "fixtures", "fake_comfy.py"),
     "--port", str(fake_port), "--store", fake_store],
    cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
fake_procs = [fake]  # every fake this file ever starts, so exit can stop them all
if not wait_true("fake ComfyUI lane answers /system_stats",
                 lambda: http_json("http://127.0.0.1:%d/system_stats" % fake_port), 15):
    stop(fake)
    sys.exit(1)
http_json("http://127.0.0.1:%d/_control/accept" % fake_port,
          data=json.dumps({"outputs": [{"filename": "shot.png"}]}).encode())

# --- our own server.py: scratch config, scratch data dir, random port ------
tmp = tempfile.mkdtemp(prefix="bwf-seq-ui-")
local_png = os.path.join(tmp, "upload.png")
with open(local_png, "wb") as f:
    f.write(PNG_1X1)
cfg_path = os.path.join(tmp, "config.json")
server_port = free_port()
with open(cfg_path, "w") as f:
    json.dump({
        "title": "Sequence UI Test",
        "port": server_port,
        "bind": "127.0.0.1",
        "lanes": [{"id": LANE_ID, "name": "Fake lane", "host": "127.0.0.1", "port": fake_port,
                   "caps": ["image", "video", "audio"]}],
        # job_poll_seconds slow enough that a running job stays visibly
        # "rendering" for a moment after Make, fast enough the test doesn't
        # stall waiting for it to settle.
        "timing": {"poll_seconds": 0.4, "job_poll_seconds": 1.0,
                   "http_timeout": 4.0, "free_settle_seconds": 1.0, "discover_seconds": 300.0},
    }, f)
URL = "http://127.0.0.1:%d/" % server_port
logf = open(os.path.join(tmp, "server.log"), "w")
server = subprocess.Popen(
    [sys.executable, os.path.join(REPO, "server.py")],
    cwd=REPO,
    env=dict(os.environ, GENCENTER_CONFIG=cfg_path, GENCENTER_DATA=os.path.join(tmp, "data")),
    stdout=logf, stderr=subprocess.STDOUT)

def lane_has_audio_mode():
    d = http_json(URL + "/api/engines?lane=" + LANE_ID)
    return any(m.get("available") for m in d.get("audio", {}).get("modes", []))

if not wait_true("server is up and the fake lane offers at least one audio mode",
                 lane_has_audio_mode, 30):
    stop(server); stop(fake); logf.close()
    with open(os.path.join(tmp, "server.log")) as f:
        print("  -- server.py said: %s" % f.read()[-800:])
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1)

def tab(page, rid):
    return '#roomStrip [data-room-id="%s"]' % rid

try:
    print("driving the real page: new sequence -> add a slot -> Make -> pick -> ref room -> 409 -> reload")
    errors = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("pageerror", lambda e: errors.append("PAGEERROR: %s" % e))
        page.on("console", lambda m: errors.append("CONSOLE %s: %s" % (m.type, m.text)) if m.type == "error" else None)
        page.goto(URL, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(1000)

        page.click(tab(page, "cutting"))
        page.wait_for_timeout(300)
        check("sequence picker visible on entry", page.is_visible("#seqPicker"))
        check("no Make before any sequence is open", not page.is_visible("#makeBtn"))

        print("new sequence")
        page.fill("#seqNewTitle", "Till Delete")
        page.click("#seqNewBtn")
        page.wait_for_timeout(400)
        h = page.evaluate("location.hash")
        check("URL carries #room=cutting&seq=<id>", "room=cutting" in h and "seq=s_" in h, h)
        check("open note names the sequence", "Till Delete" in page.inner_text("#seqPicker"))

        # PICTURE lane (bug fix, orchestrator review 2026-09-23): the page
        # must pick this slot's mode by ROOM order (STATE.rooms, rooms.json's
        # own "order"), not by the cap's raw pack-declaration order -- the
        # Picture room (order 4) lists t2i before the Clean-up room (order 6)
        # ever lists cutout, so on a fixture where every image mode is
        # available (this one) the PICTURE lane must land on a PROMPT mode
        # (t2i), never the upload-only "cutout" a naive pack-order walk would
        # have picked. Assert this against the live rooms payload, never a
        # hardcoded "t2i".
        eng = http_json(URL + "api/engines?lane=" + LANE_ID)
        rooms = eng.get("rooms") or []
        expected_mode = None
        for room in sorted(rooms, key=lambda r: r.get("order", 1000)):
            for x in room.get("modes") or []:
                if x.get("cap") != "image":
                    continue
                m = next((mm for mm in eng["image"]["modes"] if mm["id"] == x["mode"]), None)
                if m and m.get("available"):
                    expected_mode = m["id"]
                    break
            if expected_mode:
                break
        check("the fixture has a room-ordered available image mode to expect", expected_mode is not None, rooms)

        print("add a picture slot")
        before_had_make = page.is_visible("#makeBtn")
        page.click('#tlTrackPicture [data-add-lane="picture"]')
        page.wait_for_timeout(300)
        check("adding a slot did not already show Make", not before_had_make)
        check("roomForm (recipe inspector) opens for the new slot", page.is_visible("#roomForm"))
        check("Make appears once a slot is selected", page.is_visible("#makeBtn"))
        slot_sel = '#tlTrackPicture [data-slot-id]'
        check("exactly one picture slot tile", page.eval_on_selector_all(slot_sel, "els => els.length") == 1)
        cls0 = page.eval_on_selector(slot_sel, "el => el.className")
        check("new slot starts empty", "tl-slot-empty" in cls0, cls0)

        seq_id_early = h.split("seq=")[1].split("&")[0]
        created = http_json(URL + "api/sequence?id=" + seq_id_early)
        actual_mode = created["slots"][-1]["mode"]
        check("'+ generate here' on PICTURE picked the first ROOM-ordered available image mode (%r), not a raw pack-order one"
              % expected_mode, actual_mode == expected_mode, actual_mode)

        print("fill this slot's own input, then Make")
        # The page picked this slot's mode itself (the first available image
        # mode in ROOM order) -- this test follows whatever that turns out to
        # be, a prompt box or an upload-only tool, rather than assuming t2i.
        has_prompt = page.is_visible("#promptWrap")
        if has_prompt:
            page.fill("#promptBox", "a red kite over the sea")
        else:
            upload_input = page.query_selector('#ledgerProminentBody input[type=file]')
            check("an upload input exists for a promptless mode", upload_input is not None)
            if upload_input:
                upload_input.set_input_files(local_png)
                page.wait_for_timeout(500)
        page.click("#makeBtn")
        # RED-run marker: on HEAD (before this slice) #makeBtn/roomForm never
        # appear in the Cutting Room at all, so the click above is a no-op and
        # the slot never leaves "empty" -- this is what fails first.
        seen = wait_true(
            "the slot leaves 'empty' after Make (ghost/cannot-tell/unpicked/ready)",
            lambda: page.eval_on_selector(slot_sel, "el => el.className").find("tl-slot-empty") == -1,
            6)
        cls1 = page.eval_on_selector(slot_sel, "el => el.className") if seen else cls0
        check("post-Make state is a real derived state, never re-invented",
              any(s in cls1 for s in ("tl-slot-ghost", "tl-slot-cannot-tell", "tl-slot-unpicked", "tl-slot-ready")),
              cls1)

        print("wait for the fake history to complete -> a take, unpicked")
        wait_true("slot reaches 'unpicked' (a take exists, nothing picked yet)",
                  lambda: "tl-slot-unpicked" in page.eval_on_selector(slot_sel, "el => el.className"), 8)
        check("takes strip shows one take",
              page.eval_on_selector_all("#takesRow .take-item", "els => els.length") >= 1)

        print("pick the take")
        page.click('#takesRow [data-pick]')
        page.wait_for_timeout(400)
        cls2 = page.eval_on_selector(slot_sel, "el => el.className")
        check("slot is 'ready' after Pick", "tl-slot-ready" in cls2, cls2)

        print("the 2s poll must not rebuild DOM the user is touching (U1)")
        # The job/timeline/reference renders run on a 2s timer. Rebuilding
        # nodes via innerHTML on every tick destroys whatever the user is
        # mid-way with: a slot button replaced between hover and click, a
        # playing <video> restarted, a <select> snapped back to its default.
        # Nodes whose content did not change must be the SAME nodes five
        # polls later; a value the user just changed must survive.
        marked = page.evaluate("""() => {
          const tile = document.querySelector('#tlTrackPicture [data-slot-id]');
          const img = document.querySelector('#monitorStage img');
          if (tile) tile.setAttribute('data-test-mark', 'u1');
          if (img) img.setAttribute('data-test-mark', 'u1');
          return {tile: !!tile, img: !!img, sel: !!document.querySelector('#addRefRole')};
        }""")
        check("identity check has a slot tile, a monitor image, and the select on screen",
              marked["tile"] and marked["img"] and marked["sel"])
        page.select_option("#addRefRole", "set")
        page.wait_for_timeout(5000)   # > two 2s poll cycles
        identity = page.evaluate("""() => {
          const tile = document.querySelector('#tlTrackPicture [data-slot-id]');
          const img = document.querySelector('#monitorStage img');
          const sel = document.querySelector('#addRefRole');
          return {tile: !!(tile && tile.dataset.testMark === 'u1'),
                  img: !!(img && img.dataset.testMark === 'u1'),
                  sel: sel ? sel.value : null};
        }""")
        check("slot tile is the same DOM node after 5s of 2s polls (not rebuilt)",
              identity["tile"])
        check("monitor image is the same DOM node after 5s (no mid-interaction restart)",
              identity["img"])
        check("a value the user just chose in a <select> survives the polls",
              identity["sel"] == "set")

        print("add the ready take to the reference room as the set")
        check("'Add to the reference room' offered for a finished picture", page.is_visible("#addRefBtn"))
        page.select_option("#addRefRole", "set")
        page.click("#addRefBtn")
        page.wait_for_timeout(300)
        ref_text = page.inner_text("#refTrack")
        check("REF ROOM shows the new reference with its role", "set" in ref_text.lower(), ref_text)

        print("a second set is refused with the server's own sentence")
        page.click("#addRefBtn")
        page.wait_for_timeout(300)
        seq_msg = page.inner_text("#seqMsg")
        check("the server's refusal sentence is shown", "already has a set" in seq_msg, seq_msg)

        print("a forced 409: bump the sequence's rev behind the page's back, then edit")
        seq_id = h.split("seq=")[1].split("&")[0]
        cur = http_json(URL + "api/sequence?id=" + seq_id)
        bumped = http_json(URL + "api/sequence/op",
                            data=json.dumps({"id": seq_id, "rev": cur["rev"], "op": "set_title",
                                              "title": "Till Delete (bumped)"}).encode())
        check("the rev really moved behind the page's back", bumped["rev"] == cur["rev"] + 1, bumped.get("rev"))
        stale_text = "a kite with a torn tail"
        prompt_field_id = page.eval_on_selector("#promptBox", "el => el.dataset.fieldId")
        prompt_box = page.query_selector("#promptBox")
        prompt_box.fill(stale_text)
        page.wait_for_timeout(400)   # inside the 600ms autosave debounce
        wait_true("409 message shown after the debounced save collides",
                  lambda: "changed elsewhere" in page.inner_text("#seqMsg"), 3)
        check("the user's just-typed text was never silently lost",
              page.input_value("#promptBox") == stale_text, page.input_value("#promptBox"))
        page.wait_for_timeout(600)
        saved = http_json(URL + "api/sequence?id=" + seq_id)
        saved_slot = saved["slots"][0] if saved.get("slots") else None
        check("the retried save actually landed after the refresh",
              saved_slot is not None and saved_slot.get("values", {}).get(prompt_field_id) == stale_text,
              saved_slot)

        print("reload with #room=cutting&seq=<id> -> the same sequence reopens")
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(1200)
        check("reload keeps the room and the sequence",
              page.evaluate("location.hash").startswith("#room=cutting&seq=" + seq_id))
        check("reload shows the same title", "Till Delete" in page.inner_text("#seqPicker"))

        print("the SCRIPT lane never appears in sequence mode")
        tl = page.inner_text("#timelinePanel")
        check("no SCRIPT lane in the DOM", "SCRIPT" not in tl.upper(), tl)
        check("no beat-bearing element exists", page.query_selector("[data-beat-id]") is None)

        print("Use again loads the job the monitor shows, not the one it showed before")
        # Two FINISHED jobs in one room render IDENTICAL monitor action
        # buttons (Share / Use again / Forget), so picking the other one from
        # the bin is a no-write render: with the handlers attached only on a
        # real write, the previously shown job's handlers survive, and
        # "Use again" loads the WRONG job's args into the form. The monitor
        # auto-follows the makes, so it currently shows job B; picking job A
        # must re-target the handlers to A. (This probe could not be a Forget
        # click: its arm-then-confirm LABEL CHANGES, which is a real write,
        # so the re-bind happens there and masks the bug.) The jobs come
        # from the Music room, not the Cutting Room: every job a sequence
        # generated is named by it as a take, and the server refuses to
        # touch a named job, so a Cutting-Room Forget could never prove a
        # removal, and different prompts are what make the args observable.
        page.click(tab(page, "music"))
        page.wait_for_timeout(400)
        check("the Music room offers Make", page.is_visible("#makeBtn"))
        check("the Music room's mode takes a prompt (so Use-again is observable)",
              page.is_visible("#promptWrap"))

        def music_jobs(since):
            jobs = http_json(URL + "api/jobs?limit=60")["jobs"]
            return [j for j in jobs if j.get("lane") == LANE_ID
                    and j.get("kind") == "audio"
                    and j.get("created", 0) >= since]

        PROMPT_A = "alpha lullaby for a paper moon"
        PROMPT_B = "bravo waltz for a tin moon"

        def make_music_job(prompt):
            mark = time.time()
            page.fill("#promptBox", prompt)
            page.click("#makeBtn")
            if not wait_true("a music job finished after Make",
                             lambda: [j for j in music_jobs(mark) if j["status"] == "done"],
                             15):
                return None
            return [j for j in music_jobs(mark) if j["status"] == "done"][0]

        job_a = make_music_job(PROMPT_A)
        job_b = make_music_job(PROMPT_B)
        if job_a and job_b:
            # The monitor's buttons depend on the PAGE'S view of each job's
            # status: select from the bin only once the page (not just the
            # server) knows both jobs are done, or a running-job render in
            # between would rewrite the actions and mask the whole point.
            def page_knows_both_done():
                return page.evaluate("""(ids) => ids.every(id => {
                    const x = (STATE.jobs || []).find(j => j.id === id);
                    return x && x.status === 'done';
                })""", [job_a["id"], job_b["id"]])
            wait_true("the page knows both music jobs are done", page_knows_both_done, 10)
            page.wait_for_selector('#binBody tr[data-job="%s"]' % job_a["id"], timeout=10000)
            page.wait_for_selector('#binBody tr[data-job="%s"]' % job_b["id"], timeout=10000)
            # The monitor has been AUTO-FOLLOWING the makes, so it currently
            # shows job B and the handlers are B's. Picking job A from the
            # bin must re-target them: both finished jobs render IDENTICAL
            # action buttons (Share / Use again / Forget), so in the buggy
            # build the render is a no-write and the handlers stay B's.
            page.click('#binBody tr[data-job="%s"]' % job_a["id"])
            page.wait_for_timeout(300)
            check("the selected finished job is on the monitor with its action buttons",
                  page.is_visible("#shareBtn") and page.is_visible("#useAgainBtn"))
            # The box still holds B's prompt from the Make above: wipe it, so
            # whatever Use-again loads next is its own doing.
            page.fill("#promptBox", "")
            page.click("#useAgainBtn")
            loaded = None
            deadline = time.time() + 10
            while time.time() < deadline:
                v = page.input_value("#promptBox")
                if PROMPT_A in v or PROMPT_B in v:
                    loaded = v
                    break
                time.sleep(0.15)
            check("'Use again' loaded the SELECTED job's (A's) prompt, not the previously shown job's (B's) prompt",
                  loaded is not None and PROMPT_A in loaded, loaded)

        # Back in the Cutting Room on the sequence for the lane-down block:
        # put the cutting hash back, then reload -- the page's own proven
        # path to "same room, same sequence reopened" (proven above).
        page.evaluate("location.hash = " + json.dumps(h))
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(1000)

        print("lane goes down mid-render: CANNOT TELL, not 'Still working'")
        # The page already knows which machines answer (/api/lanes -- the
        # same data the header chip and the machine modules use). A
        # queued/running job on a lane that stopped answering is NOT
        # "Still working": it may be LOST. So the monitor drops the step
        # chip, the Stop button, and the claim in favour of CANNOT TELL --
        # and the bin row stops pretending it knows the step count.
        page.click('#tlTrackPicture [data-slot-id]')
        wait_true("reselecting the slot shows Make again", lambda: page.is_visible("#makeBtn"), 5)
        # The console ledger ends where the dead lane begins: from the kill
        # on, the page honestly tries to load thumbnails from a machine that
        # no longer answers, and a failed resource load is the page working,
        # not a bug. Everything before this mark must stay clean.
        lane_down_errmark = len(errors)
        ACCEPT_PAYLOAD = json.dumps({"outputs": [{"filename": "shot.png"}]}).encode()

        def new_running_job(since):
            jobs = http_json(URL + "api/jobs?limit=60")["jobs"]
            return [j for j in jobs if j.get("lane") == LANE_ID
                    and j.get("created", 0) >= since
                    and j["status"] in ("queued", "running")]

        def fake_lane_down():
            lanes = http_json(URL + "api/lanes")["lanes"]
            return all(not l["up"] for l in lanes if l["id"] == LANE_ID)

        def restart_fake():
            global fake
            # stop() the CURRENT fake before binding a new one to the same
            # port: skip this and, whenever an earlier attempt looped back
            # here without ever killing the old fake (the "no running job
            # yet" retry path below), the new process's bind() loses the
            # port race, exits immediately, and `fake` is left pointing at
            # that dead process while the ORIGINAL one keeps answering --
            # stale reference: later stop(fake) calls become no-ops on the
            # real listener, so it never goes down and never gets reaped.
            stop(fake)
            fake = subprocess.Popen(
                [sys.executable, os.path.join(HERE, "fixtures", "fake_comfy.py"),
                 "--port", str(fake_port), "--store", fake_store],
                cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            fake_procs.append(fake)
            wait_true("the fake lane is back up (retry)",
                      lambda: http_json("http://127.0.0.1:%d/system_stats" % fake_port), 15)
            http_json("http://127.0.0.1:%d/_control/accept" % fake_port, data=ACCEPT_PAYLOAD)

        # The kill has to land AFTER the job left the client (a job the lane
        # never took is a refusal, not a lost render) but BEFORE the server's
        # 1s job poll sees the fake lane's pre-set /history answer. If the
        # poll wins the race the job finishes instead of being lost: bring
        # the lane back and try again. Retries here are quiet -- a formal
        # check only fires at the end of the block, on the final state.
        def quiet(fn, timeout):
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    if fn():
                        return True
                except Exception:
                    pass
                time.sleep(0.15)
            return False

        def make_diag():
            return page.evaluate("""() => ({
              seqMsg: ((document.querySelector('#seqMsg')||{}).textContent) || '',
              inspMsg: ((document.querySelector('#inspectorMsg')||{}).textContent) || '',
              makeDisabled: document.querySelector('#makeBtn')
                ? document.querySelector('#makeBtn').disabled : null})""")

        def new_job_any(since):
            jobs = http_json(URL + "api/jobs?limit=60")["jobs"]
            return [j for j in jobs if j.get("lane") == LANE_ID
                    and j.get("created", 0) >= since]

        down_job = None
        diag = None
        for attempt in range(1, 4):
            if attempt > 1:
                restart_fake()
            attempt_t = time.time()
            # One click, no re-clicks: a Make that produced NO job at all
            # is a real defect (a stale rev, a 409 the page should have
            # surfaced), so that attempt fails on its own check below with
            # the page's own state as the detail. The OUTER attempt loop
            # still retries for the one legitimate "no RUNNING job"
            # outcome: the server's poll finishing the job before the fake
            # lane was killed.
            page.click("#makeBtn")
            if not quiet(lambda: bool(new_running_job(attempt_t)), 5):
                if not new_job_any(attempt_t):
                    diag = make_diag()
                    check("lane-down attempt %d: the Make produced a job" % attempt,
                          False, repr(diag))
                continue
            if not new_running_job(attempt_t):
                continue
            stop(fake)
            wait_true("the server marks the fake lane down", fake_lane_down, 15)
            running = new_running_job(attempt_t)
            if running:
                down_job = running[0]
                break
        check("a job was in flight when the lane died", down_job is not None,
              repr(diag) if diag else "")
        if down_job:
            wait_true("the monitor says CANNOT TELL for the in-flight job",
                      lambda: "CANNOT TELL" in page.inner_text("#monitor"), 15)
            check("monitor: no 'Still working' for a job whose machine stopped answering",
                  "Still working" not in page.inner_text("#monitor"))
            check("monitor: no Stop button for a machine that stopped answering",
                  page.query_selector("#stopBtn") is None)

            def room_of(j):
                caps = http_json(URL + "api/engines?lane=" + LANE_ID)
                for room in caps.get("rooms") or []:
                    for x in room.get("modes") or []:
                        if x.get("cap") == j.get("kind") and x.get("mode") == j.get("mode"):
                            return room["id"]
                return None
            rid = room_of(down_job)
            if rid:
                page.wait_for_selector(tab(page, rid), state="visible", timeout=5000)
                page.click(tab(page, rid))
                wait_true("the bin shows the in-flight job's row",
                          lambda: page.is_visible('#binBody tr[data-job="%s"]' % down_job["id"]), 10)
                check("bin row: CANNOT TELL, not 'step N / M', for the dead lane's job",
                      "CANNOT TELL" in page.inner_text('#binBody tr[data-job="%s"]' % down_job["id"]))

        # This test deliberately provokes two real, in-spec HTTP error
        # responses (a 400 refusing a second set, a 409 from the forced-rev
        # test) -- the browser itself logs a "resource failed to load"
        # console entry for ANY non-2xx fetch response, independent of the
        # page's own (correct) JSON-body error handling. Those two expected
        # entries are not a page defect; anything else is. The ledger ends
        # at lane_down_errmark, where the last block killed the lane on
        # purpose: from there on, failed loads against the dead machine are
        # expected honesty, not defects.
        time.sleep(1.0)   # let in-flight console events settle first
        pre_dead_lane = errors[:lane_down_errmark]
        real_errors = [e for e in pre_dead_lane if "status of 409" not in e and "status of 400" not in e]
        check("zero console errors (other than the two errors this test itself provokes)",
              not real_errors, "\n".join(real_errors[:20]))

        browser.close()
finally:
    stop(server)
    for p in fake_procs:
        stop(p)
    logf.close()
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.rmtree(fake_store, ignore_errors=True)

print()
print("FAILED: %d" % len(FAILED))
if FAILED:
    for n in FAILED:
        print("  - " + n)
sys.exit(1 if FAILED else 0)
