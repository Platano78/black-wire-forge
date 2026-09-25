"""Browser smoke gate: the page renders the form the engine packs declare.

Every other gate drives the HTTP API directly, which is exactly why the
original C1 defect (the AUDIO tab silently rendering zero fields, because a
numeric field default blew up a string-only `esc()` mid-render) was
invisible to all of them -- the API path never touches the DOM. This test
drives the real page in a real browser and checks what actually renders,
against the PUBLIC CONTRACT: GET /api/engines (its caps AND its "rooms").

Rooms slice (the internal rooms-by-task design doc): the page is driven through its
room strip and engine picker. Per room, per engine, the primary/advanced
exactness checks still hold; plus Picture opens on text-to-picture, #room=
survives a reload, the Cutting Room shows its shell, a room's WORKING dot and
the Everything -> other room jump, a cutout Make carries NO prompt key, and
Music's duration field shows (and Make sends) the number that really renders.

NOTHING REACHES A MACHINE: /api/generate and /api/upload are intercepted and
fulfilled locally (page.route), /api/view is refused, and the history test
serves a fixture /api/jobs -- running jobs only, no outputs to fetch.

Self-contained: it spawns its own fake ComfyUI lane
(tests/fixtures/fake_comfy.py, serving real dropdown contents captured from
the rig in tests/golden/pools_rig.json) and its own `server.py` subprocess
(scratch config + scratch data dir, both on random free ports). Nothing
external is needed, and no check may be skipped: "cannot run" fails, because a
skipped gate is how the cutout prompt bug shipped. A mode the golden pools
do not cover must come back UNAVAILABLE, and that unavailability is exactly
what gets checked.

Run: python3 tests/test_ui_smoke.py
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

sys.dont_write_bytecode = True  # see test_audio_golden.py's comment on stale .pyc

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
LANE_ID = "rig"

URL = None    # this test's own server, on a random free port (set below)
FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + detail) if not cond and detail else ""))
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

def wait_true(desc, fn, timeout):
    """Poll fn() until truthy; register a failed check, then return False."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if fn():
                return True
        except Exception:
            pass
        time.sleep(0.25)
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
    # (Previously this was deliberately a FAILURE; that read as "the project
    # is broken" on a stranger's clean clone with no dev deps installed.)
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

# --- our own fake lane: real dropdown contents, zero real hardware ----------
fake_port = free_port()
fake = subprocess.Popen(
    [sys.executable, os.path.join(HERE, "fixtures", "fake_comfy.py"),
     "--port", str(fake_port)],
    cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
if not wait_true("fake ComfyUI lane answers /system_stats",
                 lambda: http_json("http://127.0.0.1:%d/system_stats" % fake_port), 15):
    stop(fake)
    sys.exit(1)

# --- L5: a fake OpenAI-compatible helper endpoint, in-process -------------
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
HELPER_REQUESTS = []
class FakeHelperHandler(BaseHTTPRequestHandler):
    KITE = ("a red kite over the sea: a bright realistic photograph of one red diamond-shaped kite flying high over a "
            "calm grey sea on a windy afternoon. The kite sits in the upper left of the frame, its long white tail rippling behind it, "
            "while pale waves roll toward a wide sandy beach below. Soft overcast light fills the scene evenly. The "
            "composition is open and airy, with a calm, free mood.")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        HELPER_REQUESTS.append(json.loads(self.rfile.read(n) or b"{}"))
        # The guide's writer reply shape (P2c: "Help me write this" goes to the room's guide)
        # A draft that passes the t2i writer's own check (P3d: 40+ words, positive only), so it is not retried.
        resp = json.dumps({"choices": [{"message": {"content": "PROMPT: " + self.KITE}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)
    def log_message(self, *a): pass
fake_helper = ThreadingHTTPServer(("127.0.0.1", 0), FakeHelperHandler)
helper_port = fake_helper.server_address[1]
threading.Thread(target=fake_helper.serve_forever, daemon=True).start()

# --- our own server.py: scratch config, scratch data dir, random port -------
tmp = tempfile.mkdtemp(prefix="bwf-smoke-")
cfg_path = os.path.join(tmp, "config.json")
server_port = free_port()
with open(cfg_path, "w") as f:
    json.dump({
        "title": "Smoke Test",
        "port": server_port,
        "bind": "127.0.0.1",
        "lanes": [{"id": LANE_ID, "name": "Fake lane",
                   "host": "127.0.0.1", "port": fake_port,
                   "caps": ["3d", "audio", "image", "video"]},
                  # The box this test server runs on: a process lane. It
                  # has no network side, and whether it can offer a mode
                  # depends on what is really installed here -- the checks
                  # below branch on the server's own answer, not a guess.
                  {"id": "here", "kind": "process", "name": "This box",
                   "slots": 1, "caps": ["3d"]}],
        "timing": {"poll_seconds": 1.0, "job_poll_seconds": 1.0,
                   "http_timeout": 4.0, "free_settle_seconds": 2.0,
                   "discover_seconds": 300.0},
        "helper": {"url": "http://127.0.0.1:%d/v1" % helper_port, "model": "test", "timeout_s": 5},
    }, f)
URL = "http://127.0.0.1:%d/" % server_port
logf = open(os.path.join(tmp, "server.log"), "w")
server = subprocess.Popen(
    [sys.executable, os.path.join(REPO, "server.py")],
    cwd=REPO,
    env=dict(os.environ, GENCENTER_CONFIG=cfg_path,
             GENCENTER_DATA=os.path.join(tmp, "data")),
    stdout=logf, stderr=subprocess.STDOUT)

def lane_has_a_mode():
    d = http_json(URL + "/api/engines?lane=" + LANE_ID)
    return any(m.get("available") for cap in d if cap not in ("rooms", "helper")
               for m in d[cap].get("modes", []))

if not wait_true("server is up and the fake lane offers at least one mode",
                 lane_has_a_mode, 30):
    stop(server)
    stop(fake)
    logf.close()
    with open(os.path.join(tmp, "server.log")) as f:
        print("  -- server.py said: %s" % " | ".join(
            l.strip() for l in f.read().splitlines() if l.strip())[-400:])
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1)

GENERATED = []
def fake_generate(route, request):
    GENERATED.append(json.loads(request.post_data or "{}"))
    route.fulfill(status=200, content_type="application/json",
                  body=json.dumps({"ok": False, "error": "intercepted by test_ui_smoke"}))

def guard(page):
    page.route("**/api/generate", fake_generate)
    page.route("**/api/upload", lambda r, q: r.fulfill(status=200, content_type="application/json",
                                                       body='{"ok": false, "error": "intercepted"}'))
    page.route("**/api/view*", lambda r, q: r.fulfill(status=404, body=""))

def tab(page, rid):
    return '#roomStrip [data-room-id="%s"]' % rid

# The engine picker is a one-line summary until opened (P2c); open it before
# touching its radios.
OPEN_PICKER = "() => { const d = document.querySelector('#enginePickerDetails'); if(d) d.open = true; }"

def pick_mode(page, mode):
    """Check the room's engine radio for `mode`; False when the lane cannot
    offer it (radio disabled) -- the calling block then FAILS, because on
    this test's fake lane every mode is supposed to be offerable."""
    page.evaluate(OPEN_PICKER)
    r = page.query_selector('#enginePicker input[data-mode="%s"]' % mode)
    if not r or r.is_disabled():
        return False
    r.check()
    page.wait_for_timeout(150)
    return True

try:
    print("driving the real page against the public contract (GET /api/engines), rooms first")
    errors = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("pageerror", lambda e: errors.append("PAGEERROR: %s" % e))
        page.on("console", lambda m: errors.append("CONSOLE %s: %s" % (m.type, m.text)) if m.type == "error" else None)
        guard(page)
        page.goto(URL, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(2000)  # let the page's own first-load polling settle

        lane = page.evaluate("STATE.activeLane")
        engines_resp = page.evaluate("l => fetch('/api/engines' + (l ? '?lane=' + l : '')).then(r => r.json())", lane)
        rooms = engines_resp.get("rooms") or []
        caps = [k for k in engines_resp if k not in ("rooms", "helper")]   # sit beside the caps, not one
        check("server declares at least one cap", bool(caps), str(list(engines_resp)))
        check("server declares rooms", bool(rooms), str(list(engines_resp)))
        mode_of = {(c, m["id"]): m for c in caps for m in engines_resp[c]["modes"]}
        # What the PROCESS lane (the box itself) can actually do, straight
        # from the server: the page must show that truth, so the process
        # checks below branch on it instead of assuming Blender is missing.
        proc_engines = page.evaluate("fetch('/api/engines?lane=here').then(r => r.json())")
        proc_on_box = {(c, m["id"]): m.get("available") is not False
                       for c in proc_engines if c not in ("rooms", "helper")
                       for m in proc_engines[c].get("modes", [])}
        check("default room is the first in order", page.evaluate("location.hash") == "#room=" + rooms[0]["id"],
              page.evaluate("location.hash"))

        for room in rooms:
            rid = room["id"]
            btn = page.query_selector(tab(page, rid))
            if not btn:
                check("room %r has a tab" % rid, False)
                continue
            btn.click()
            page.wait_for_timeout(200)
            check("room %r: tab latched" % rid, btn.get_attribute("aria-pressed") == "true")
            check("room %r: heading names it" % rid, page.inner_text("#roomHeading").strip() == room["name"],
                  page.inner_text("#roomHeading"))
            if room.get("kind") == "cutting":
                continue  # its own block below
            modes = room.get("modes") or []
            if not modes:
                check("room %r (no modes): tab dimmed with the plain tooltip" % rid,
                      "room-tab-dim" in (btn.get_attribute("class") or "")
                      and btn.get_attribute("title") == "Nothing installed makes this yet", btn.get_attribute("title"))
                check("room %r (no modes): inspector says so plainly" % rid,
                      "Nothing installed makes this yet" in page.inner_text("#inspector"))
                check("room %r (no modes): no form, no Make" % rid, not page.is_visible("#makeBtn"))
                continue
            page.evaluate(OPEN_PICKER)
            check("room %r: engine picker present iff 2+ modes" % rid,
                  page.is_visible("#enginePicker") == (len(modes) >= 2))
            for mm in modes:
                m = mode_of.get((mm["cap"], mm["mode"]))
                if m is None:
                    check("room %r: mode %r is in the per-cap payload" % (rid, mm), False)
                    continue
                mk = m.get("lane_kind") or "comfy"
                if len(modes) >= 2:
                    radio = page.query_selector('#enginePicker input[data-cap="%s"][data-mode="%s"]' % (mm["cap"], mm["mode"]))
                    if not radio:
                        check("room %r: engine row for %s/%s" % (rid, mm["cap"], mm["mode"]), False)
                        continue
                    if radio.is_disabled():
                        # A disabled row is only excused when its KIND has
                        # no machine at all -- a comfy mode unavailable on
                        # this fake COMFY lane is a FAIL, as before B2.
                        check("room %r: unavailable %s is only excused when its kind has no machine"
                              % (rid, mm["mode"]),
                              mk != "comfy",
                              "a comfy mode came back unavailable on the fake lane")
                        check("room %r: unavailable %s disabled, with its missing words" % (rid, mm["mode"]),
                              m["available"] is False)
                        # The disabled row itself says what is missing (the
                        # 'needs …' line); its form is not rendered, so there
                        # is nothing to count for it.
                        row_text = page.eval_on_selector(
                            '#enginePicker input[data-cap="%s"][data-mode="%s"]' % (mm["cap"], mm["mode"]),
                            "el => el.closest('label').innerText")
                        check("%s/%s unavailable on this lane: its row names what is missing" % (mm["cap"], mm["mode"]),
                              "needs" in (row_text or ""), repr(row_text))
                        continue
                    radio.check()
                    page.wait_for_timeout(400)
                    if mk != "comfy":
                        # B1c: picking a process mode moves the active lane
                        # to a process machine; the row then says what THAT
                        # machine can or cannot do, not what the comfy
                        # lane is missing.
                        check("process pick %s/%s: the active lane is now the process lane"
                              % (mm["cap"], mm["mode"]),
                              page.evaluate("STATE.activeLane") == "here",
                              page.evaluate("STATE.activeLane"))
                        check("process pick %s/%s: the box's machine module is the pressed one"
                              % (mm["cap"], mm["mode"]),
                              page.evaluate("() => Array.prototype.some.call(" +
                                            "document.querySelectorAll('#machineModules .machine')," +
                                            "e => e.getAttribute('aria-pressed') === 'true' && e.innerText.indexOf('This box') >= 0)"))
                        # With the box ACTIVE, a comfy room is not "not for
                        # this" -- its modes run on the comfy lane -- and a
                        # READY process lane can never be "asleep".
                        if page.query_selector(tab(page, "music")) is None:
                            check("Music room tab exists (its dot is checked)", False)
                        else:
                            check("This box active: Music tab carries no not-for-this square",
                                  page.eval_on_selector(tab(page, "music") + " .dot",
                                                        "el => el.hidden || !el.classList.contains('dot-not-for-this')"))
                        check("This box active and up: inspector says nothing is asleep",
                              "asleep right now" not in page.inner_text("#inspector"))
                        box_mod = page.evaluate("() => { const e = Array.prototype.find.call(" +
                                                "document.querySelectorAll('#machineModules .machine')," +
                                                "x => x.innerText.indexOf('This box') >= 0);" +
                                                " return e ? {vram: /vram/i.test(e.innerText)," +
                                                " ready: /READY/.test(e.innerText)} : null; }")
                        # READY only when the box really has its programs: a process
                        # lane whose bins are missing reports up=false, and its tile
                        # must not claim READY (help.html's own definition of READY).
                        box_can = bool(proc_on_box.get((mm["cap"], mm["mode"])))
                        check("process machine module: no VRAM wording, READY exactly when its programs are installed"
                              " (installed here: %s)" % box_can,
                              bool(box_mod) and not box_mod["vram"] and box_mod["ready"] == box_can,
                              repr(box_mod))
                        row_text = page.eval_on_selector(
                            '#enginePicker input[data-cap="%s"][data-mode="%s"]' % (mm["cap"], mm["mode"]),
                            "el => el.closest('label').innerText")
                        if proc_on_box.get((mm["cap"], mm["mode"])):
                            # The box has every program the mode needs: the
                            # form renders its declared primary fields.
                            fields = m.get("fields") or []
                            want_primary = len([f for f in fields if f.get("tier") == "primary"])
                            got_primary = page.eval_on_selector_all(
                                '#inspector [data-field-tier="primary"]', "els => els.length")
                            check("room=%r %s/%s on the box: primary field count matches declared (%d)"
                                  % (rid, mm["cap"], mm["mode"], want_primary),
                                  got_primary == want_primary, "page rendered %d" % got_primary)
                            examples = m.get("examples") or []
                            check("room=%r %s/%s on the box: has at least one example"
                                  % (rid, mm["cap"], mm["mode"]), bool(examples))
                            if examples:
                                ex = examples[0]
                                btn = page.query_selector('#tryThisRow [data-try="%s"]' % ex["id"])
                                check("room=%r %s/%s on the box: Try this button for its first example exists"
                                      % (rid, mm["cap"], mm["mode"]), btn is not None)
                                if btn:
                                    before = len(GENERATED)
                                    btn.click()
                                    page.wait_for_timeout(200)
                                    if ex.get("recipe"):
                                        got = page.input_value("#recipeSelect")
                                        check("%s/%s on the box Try this: recipe select shows %r"
                                              % (mm["cap"], mm["mode"], ex["recipe"]), got == ex["recipe"], repr(got))
                                    if ex.get("quality"):
                                        pressed = page.get_attribute(
                                            '#qualityLadder [data-quality="%s"]' % ex["quality"], "aria-pressed")
                                        check("%s/%s on the box Try this: quality tier %r selected"
                                              % (mm["cap"], mm["mode"], ex["quality"]), pressed == "true", repr(pressed))
                                    check("%s/%s on the box Try this: no /api/generate request was made"
                                          % (mm["cap"], mm["mode"]), len(GENERATED) == before)
                                    if ex.get("needs"):
                                        row_text = page.inner_text("#tryThisRow")
                                        check("%s/%s on the box Try this: 'needs' shows 'Needs a %s first'"
                                              % (mm["cap"], mm["mode"], ex["needs"]),
                                              ("Needs a %s first" % ex["needs"]) in row_text, repr(row_text))
                        else:
                            check("process mode %s/%s: the row names what the box is missing"
                                  % (mm["cap"], mm["mode"]),
                                  "needs" in (row_text or ""), repr(row_text))
                        # The mode's model field is a 3D file input whether
                        # or not the box can render yet (the form renders
                        # the declared fields either way).
                        model_accept = page.evaluate(
                            "() => { const i = document.getElementById('upload_model');" +
                            " return i && i.type === 'file' ? i.accept : null; }")
                        check("turntable model field: a file input with a .glb accept",
                              model_accept is not None and "glb" in model_accept,
                              repr(model_accept))
                        # Back to a comfy mode: the lane follows the pick.
                        back = None
                        for x in modes:
                            m2 = mode_of.get((x["cap"], x["mode"]))
                            if m2 and (m2.get("lane_kind") or "comfy") == "comfy":
                                back = x
                                break
                        if back is None:
                            check("a comfy mode exists in the room to click back to", False)
                        else:
                            page.query_selector(
                                '#enginePicker input[data-cap="%s"][data-mode="%s"]'
                                % (back["cap"], back["mode"])).check()
                            page.wait_for_timeout(400)
                            check("back to comfy %s: the active lane is the fake lane again" % back["mode"],
                                  page.evaluate("STATE.activeLane") == LANE_ID,
                                  page.evaluate("STATE.activeLane"))
                        continue
                fields = m.get("fields") or []
                want_primary = len([f for f in fields if f.get("tier") == "primary"])
                want_advanced = len([f for f in fields if f.get("tier") != "primary"])
                got_primary = page.eval_on_selector_all('#inspector [data-field-tier="primary"]', "els => els.length")
                summary = page.eval_on_selector("#advancedSummary", "el => el.textContent")
                check("room=%r %s/%s: primary field count matches declared (%d)" % (rid, mm["cap"], mm["mode"], want_primary),
                      got_primary == want_primary, "page rendered %d" % got_primary)
                check("room=%r %s/%s: 'Everything else (N)' states declared advanced count (%d)"
                      % (rid, mm["cap"], mm["mode"], want_advanced),
                      summary is not None and ("(%d)" % want_advanced) in summary, "summary text: %r" % summary)
                # v1.0.1: counting data-field-tier="primary" passed while Talking Head's
                # face upload sat inside the COLLAPSED recipe drawer. A primary file
                # input must be visible on arrival, with nothing opened first.
                for f in fields:
                    if f.get("tier") == "primary" and f.get("type") in ("image", "audio", "image_list", "video_list", "model"):
                        vis = page.is_visible("#upload_%s" % f["id"])
                        check("room=%r %s/%s: primary file input %r is visible without opening anything"
                              % (rid, mm["cap"], mm["mode"], f["id"]), vis, "hidden (inside a closed drawer?)")

                # H2: every mode's first Try this button -- click it, check
                # recipe/quality/values landed, and that Make was NEVER sent.
                examples = m.get("examples") or []
                check("room=%r %s/%s: has at least one example" % (rid, mm["cap"], mm["mode"]), bool(examples))
                if examples:
                    ex = examples[0]
                    btn = page.query_selector('#tryThisRow [data-try="%s"]' % ex["id"])
                    check("room=%r %s/%s: Try this button for its first example exists"
                          % (rid, mm["cap"], mm["mode"]), btn is not None)
                    if btn:
                        before = len(GENERATED)
                        btn.click()
                        page.wait_for_timeout(200)
                        if ex.get("recipe"):
                            got = page.input_value("#recipeSelect")
                            check("%s/%s Try this: recipe select shows %r" % (mm["cap"], mm["mode"], ex["recipe"]),
                                  got == ex["recipe"], repr(got))
                        if ex.get("quality"):
                            pressed = page.get_attribute(
                                '#qualityLadder [data-quality="%s"]' % ex["quality"], "aria-pressed")
                            check("%s/%s Try this: quality tier %r selected" % (mm["cap"], mm["mode"], ex["quality"]),
                                  pressed == "true", repr(pressed))
                        for k, v in (ex.get("values") or {}).items():
                            el = page.query_selector('#inspector [data-field-id="%s"]' % k)
                            if el is None:
                                check("%s/%s Try this: field %r exists to carry its value" % (mm["cap"], mm["mode"], k), False)
                                continue
                            if el.get_attribute("type") == "checkbox":
                                got = page.eval_on_selector('#inspector [data-field-id="%s"]' % k, "el => el.checked")
                                check("%s/%s Try this: %r == %r" % (mm["cap"], mm["mode"], k, v), got == bool(v), repr(got))
                            else:
                                got = page.input_value('#inspector [data-field-id="%s"]' % k)
                                check("%s/%s Try this: %r == %r" % (mm["cap"], mm["mode"], k, v),
                                      got == str(v), repr(got))
                        check("%s/%s Try this: no /api/generate request was made" % (mm["cap"], mm["mode"]),
                              len(GENERATED) == before)
                        if ex.get("needs"):
                            row_text = page.inner_text("#tryThisRow")
                            check("%s/%s Try this: 'needs' shows 'Needs a %s first'"
                                  % (mm["cap"], mm["mode"], ex["needs"]),
                                  ("Needs a %s first" % ex["needs"]) in row_text, repr(row_text))

        print()
        print("the 'runs on' routing line is muted, not the error colour")
        colors = page.evaluate("""() => {
          const real = sel => { const e = document.querySelector(sel);
            return e ? getComputedStyle(e).color : null; };
          const styled = cls => { const e = document.createElement('div');
            e.className = cls; document.body.appendChild(e);
            const v = getComputedStyle(e).color; e.remove(); return v; };
          return {runs: real('.engine-row .erow-runs') || styled('erow-runs'),
                  needs: real('.engine-row .erow-missing') || styled('erow-missing')};
        }""")
        check("the 'runs on' line is not the 'needs' (error) colour",
              colors["runs"] != colors["needs"], repr(colors))

        print()
        print("#room=<id>&try=<example id> applies the example on load, never presses Make")
        song = mode_of.get(("audio", "song")) or {}
        song_examples = song.get("examples") or []
        if not song_examples:
            check("audio/song has an example to test the URL with", False)
        else:
            ex = song_examples[0]
            before = len(GENERATED)
            page.goto(URL + "#room=music&try=" + ex["id"], wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(1500)
            checked = page.eval_on_selector('#enginePicker input:checked', "el => el && el.dataset.mode")
            check("URL try=: selects the example's own mode (song)", checked == "song", repr(checked))
            if ex.get("recipe"):
                got = page.input_value("#recipeSelect")
                check("URL try=: recipe select shows %r" % ex["recipe"], got == ex["recipe"], repr(got))
            for k, v in (ex.get("values") or {}).items():
                got = page.input_value('#inspector [data-field-id="%s"]' % k)
                check("URL try=: field %r == %r" % (k, v), got == str(v), repr(got))
            check("URL try=: no /api/generate request was made", len(GENERATED) == before)

        print()
        print("Picture opens on text-to-picture, not on a tool")
        page.click(tab(page, "picture"))
        page.wait_for_timeout(200)
        checked = page.eval_on_selector('#enginePicker input:checked', "el => el && el.dataset.mode")
        check("Picture's selected engine is t2i", checked == "t2i", repr(checked))

        print()
        print("the Cutting Room shows its shell -- a sequence picker, and an empty live timeline")
        page.click(tab(page, "cutting"))
        page.wait_for_timeout(300)
        tl = page.inner_text("#timelinePanel") if page.is_visible("#timelinePanel") else ""
        check("timeline visible with PICTURE / VIDEO / SOUND / locked REF ROOM",
              all(w in tl for w in ("PICTURE", "VIDEO", "SOUND", "[LOCKED] REF ROOM")), repr(tl))
        check("one '+ generate here' control per lane with no sequence open", tl.count("+ generate here") == 3, repr(tl))
        check("the fixed REF ROOM law line", "can't see faces or framing" in tl, repr(tl))
        check("the honest banner (no sequence open)",
              "Open a sequence, or start a new one" in page.inner_text("#monitor"), page.inner_text("#monitor"))
        check("sequence picker visible", page.is_visible("#seqPicker"))
        check("no Make in the Cutting Room with no slot selected", not page.is_visible("#makeBtn"))
        page.click(tab(page, "music"))
        page.wait_for_timeout(200)
        check("leaving it hides the timeline", not page.is_visible("#timelinePanel"))

        print()
        print("#room= survives a reload")
        page.click(tab(page, "video"))
        page.wait_for_timeout(200)
        check("URL carries #room=video", page.evaluate("location.hash") == "#room=video", page.evaluate("location.hash"))
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(1500)
        check("reload reopens Video", page.get_attribute(tab(page, "video"), "aria-pressed") == "true"
              and page.inner_text("#roomHeading").strip() == "Video", page.inner_text("#roomHeading"))

        print()
        print("Music: the recipe's number reaches its field, and the Make body carries what the page shows")
        page.click(tab(page, "cutting"))   # leave and re-enter, so this is a fresh entry
        page.wait_for_timeout(150)
        page.click(tab(page, "music"))
        page.wait_for_timeout(300)
        song = mode_of.get(("audio", "song")) or {}
        label = page.inner_text("#recipeSummaryLabel").strip()
        preset = next((p for p in song.get("presets") or [] if p["label"] == label), None)
        want = preset and (preset.get("values") or {}).get("duration")
        shown = page.input_value('#inspector [data-field-id="duration"]')
        check("selected recipe %r declares a duration" % label, want is not None, repr(preset))
        check("duration field == the recipe's value (%r)" % want, want is not None and float(shown) == float(want),
              "field shows %r" % shown)
        before = len(GENERATED)
        page.click("#makeBtn")
        page.wait_for_timeout(400)
        body = GENERATED[-1] if len(GENERATED) > before else {}
        check("Make body carries that duration", body.get("duration") == want, repr(body))
        # The user's own typing is the latest action: it renders and ships.
        page.fill('#inspector [data-field-id="duration"]', "97")
        before = len(GENERATED)
        page.click("#makeBtn")
        page.wait_for_timeout(400)
        body = GENERATED[-1] if len(GENERATED) > before else {}
        check("typed 97: Make body carries duration 97 with quality still set",
              body.get("duration") == 97 and bool(body.get("quality")), repr(body))
        # Then the tier: picking "Long take" writes ITS duration over the typed
        # value, the field stays editable, and the Make body carries it.
        tiers = {t["id"]: t for t in song.get("quality") or []}
        long_d = (tiers.get("long") or {}).get("values", {}).get("duration")
        page.click('#qualityLadder [data-quality="long"]')
        page.wait_for_timeout(200)
        shown = page.input_value('#inspector [data-field-id="duration"]')
        check("Long take: the field shows the tier's duration (%r)" % long_d,
              long_d is not None and float(shown) == float(long_d), "field shows %r" % shown)
        before = len(GENERATED)
        page.click("#makeBtn")
        page.wait_for_timeout(400)
        body = GENERATED[-1] if len(GENERATED) > before else {}
        check("Long take: Make body carries the tier's duration", body.get("duration") == long_d
              and body.get("quality") == "long", repr(body))
        check("no field is locked by a quality tier",
              page.eval_on_selector_all('#inspector [data-tier-locked]', 'e=>e.length') == 0)
        page.click('#qualityLadder [data-quality="standard"]')
        page.wait_for_timeout(150)

        print()
        print("a cutout Make carries no prompt key (the stale-promptBox bug)")
        page.click(tab(page, "picture"))
        page.wait_for_timeout(200)
        if not pick_mode(page, "t2i"):
            check("t2i unavailable on this lane: no prompt to go stale", False)
        else:
            page.fill("#promptBox", "a red kite over the sea")
            page.click(tab(page, "cleanup"))
            page.wait_for_timeout(200)
            if not pick_mode(page, "cutout"):
                check("cutout unavailable on this lane: no cutout Make to test", False)
            else:
                before = len(GENERATED)
                page.click("#makeBtn")
                page.wait_for_timeout(500)
                body = GENERATED[-1] if len(GENERATED) > before else None
                check("Make was intercepted (nothing sent to a machine)", body is not None)
                check("cutout body is kind=image mode=cutout", bool(body) and body.get("mode") == "cutout"
                      and body.get("kind") == "image", repr(body))
                check("cutout body has NO prompt key", bool(body) and "prompt" not in body, repr(body))
            if pick_mode(page, "upscale"):
                check("upscale renders exactly its one primary field",
                      page.eval_on_selector_all('#inspector [data-field-tier="primary"]', "els => els.length") == 1)
            else:
                check("upscale unavailable on this lane: no form to count", False)

        print()
        print("E2: yue2/cover's own 'mode' field must not collide with the request's dispatch mode")
        page.click(tab(page, "music"))
        page.wait_for_timeout(200)
        if not pick_mode(page, "yue2"):
            check("yue2 unavailable on this lane: no mode-field collision to test", False)
        else:
            page.select_option('#inspector [data-field-id="mode"]', "melody")
            before = len(GENERATED)
            page.click("#makeBtn")
            page.wait_for_timeout(400)
            body = GENERATED[-1] if len(GENERATED) > before else None
            check("yue2 Make was intercepted (nothing sent to a machine)", body is not None)
            check("yue2 body's top-level mode is the ENGINE mode 'yue2', not the field's value",
                  bool(body) and body.get("mode") == "yue2", repr(body))
            check("yue2 body's values.mode carries the chosen field value 'melody'",
                  bool(body) and (body.get("values") or {}).get("mode") == "melody", repr(body))
        page.click(tab(page, "cover"))
        page.wait_for_timeout(200)
        # The Cover room declares exactly one mode (itself), so the engine
        # picker -- which only renders for a room with 2+ modes -- stays
        # hidden and the room tab alone selects cap=audio/mode=cover;
        # pick_mode's radio would never exist here.
        if not page.query_selector('#inspector [data-field-id="mode"]'):
            check("cover unavailable on this lane: no mode-field collision to test", False)
        else:
            page.select_option('#inspector [data-field-id="mode"]', "full")
            before = len(GENERATED)
            page.click("#makeBtn")
            page.wait_for_timeout(400)
            body = GENERATED[-1] if len(GENERATED) > before else None
            check("cover Make was intercepted (nothing sent to a machine)", body is not None)
            check("cover body's top-level mode is the ENGINE mode 'cover', not the field's value",
                  bool(body) and body.get("mode") == "cover", repr(body))
            check("cover body's values.mode carries the chosen field value 'full'",
                  bool(body) and (body.get("values") or {}).get("mode") == "full", repr(body))

        print()
        print("history by room: WORKING dot, This room / Everything, and the jump to another room")
        now = time.time()
        fixture = {"jobs": [
            {"id": "fx-song", "lane": lane, "lane_name": "Fixture", "kind": "audio", "mode": "song",
             "status": "running", "step": 3, "total": 10, "created": now, "outputs": [], "prompt": "fixture song"},
            {"id": "fx-cut", "lane": lane, "lane_name": "Fixture", "kind": "image", "mode": "cutout",
             "status": "queued", "step": 0, "total": 0, "created": now - 5, "outputs": [], "prompt": ""},
        ], "log": [], "now": now}
        page.route("**/api/jobs*", lambda r, q: r.fulfill(status=200, content_type="application/json",
                                                         body=json.dumps(fixture)))
        page.wait_for_timeout(2600)  # one poll
        page.click(tab(page, "music"))
        page.wait_for_timeout(200)
        check("Music shows the WORKING dot", page.eval_on_selector(tab(page, "music") + " .dot",
              "el => !el.hidden && el.classList.contains('dot-working')"))
        # No WORKING dot on a room with no live job. (The dimmed "not for this
        # lane" square may sit there when the lane lacks the mode -- that is an
        # availability marker, not work in progress.)
        check("Sound FX shows no working dot", page.eval_on_selector(tab(page, "sfx") + " .dot",
              "el => el.hidden || !el.classList.contains('dot-working')"))
        toggle = page.inner_text("#historyToggle")
        check("toggle counts: This room (1) / Everything (2)",
              "THIS ROOM (1)" in toggle.upper() and "EVERYTHING (2)" in toggle.upper(), repr(toggle))
        check("This room lists only the song", page.eval_on_selector_all("#binBody tr", "e => e.map(r => r.dataset.job)")
              == ["fx-song"])
        page.click('#historyToggle [data-hist-scope="all"]')
        page.wait_for_timeout(150)
        check("Everything lists both", len(page.query_selector_all("#binBody tr")) == 2)
        page.click('#binBody tr[data-job="fx-cut"]')
        page.wait_for_timeout(300)
        check("picking the cutout job jumps to Clean-up", page.evaluate("location.hash") == "#room=cleanup"
              and page.get_attribute(tab(page, "cleanup"), "aria-pressed") == "true", page.evaluate("location.hash"))
        page.unroute("**/api/jobs*")

        print()
        print("Help button on the main toolbar")
        check("Help button present, links to /help", page.get_attribute("#helpBtn", "href") == "/help")

        print()
        print("Stop / Forget: monitor actions for a running vs a finished fixture job")
        fixture3 = {"jobs": fixture["jobs"] + [
            {"id": "fx-done", "lane": lane, "lane_name": "Fixture", "kind": "audio", "mode": "song",
             "status": "done", "step": 10, "total": 10, "created": now - 20,
             "outputs": [{"filename": "x.wav", "subfolder": "", "type": "output", "media": "audio"}],
             "prompt": "fixture done song"},
        ], "log": [], "now": now}
        page.route("**/api/jobs*", lambda r, q: r.fulfill(status=200, content_type="application/json",
                                                          body=json.dumps(fixture3)))
        page.wait_for_timeout(2600)
        page.click(tab(page, "music"))
        page.wait_for_timeout(200)
        page.click('#binBody tr[data-job="fx-song"]')
        page.wait_for_timeout(200)
        check("Stop shows for a running job", page.is_visible("#stopBtn"))
        check("Forget hidden for a running job", not page.is_visible("#forgetBtn"))
        # fx-done carries a real output, so selecting it makes the monitor
        # render an <audio src="/api/view...">: guard() answers /api/view
        # with a 404 on purpose (nothing may reach a real machine), which
        # is a real console error for a real <audio> tag -- answer it with
        # a harmless 200 for this one selection instead.
        def fake_view(route, request):
            route.fulfill(status=200, content_type="audio/wav", body="")
        page.route("**/api/view*", fake_view)
        page.click('#binBody tr[data-job="fx-done"]')
        page.wait_for_timeout(200)
        check("Forget shows for a done job", page.is_visible("#forgetBtn"))
        check("Stop hidden for a done job", not page.is_visible("#stopBtn"))
        # Left routed (not unrouted) for the rest of this page's life: the
        # Forget check below deliberately waits 2.5s (past a real
        # setInterval(pollJobs, 2000) tick, proving the armed label survives
        # a poll re-render) with fx-done still STATE.selectedJobId -- and
        # every one of those re-renders rebuilds the <audio src="/api/view...">
        # tag, refetching it. Unrouted, that tick would hit guard()'s
        # blanket 404 on purpose and fail "zero console/page errors" for a
        # reason that has nothing to do with the Forget flow under test; no
        # later check depends on /api/view actually refusing again.

        forget_calls = []
        def fake_forget(route, request):
            forget_calls.append(json.loads(request.post_data or "{}"))
            route.fulfill(status=200, content_type="application/json", body='{"ok": true}')
        page.route("**/api/forget", fake_forget)
        # The armed "click again to confirm" state lives in STATE.forgetArmed
        # and renderMonitor honours it -- so it must survive the page's own
        # 2s job poll (setInterval(pollJobs, 2000)), which calls
        # renderMonitor() unconditionally whenever a job is selected, same
        # fixture route as above (fixture3, unchanged jobs -- exercising the
        # real poll, not stubbing it away). Wait past a full poll tick before
        # checking, not just past the click.
        page.click("#forgetBtn")
        page.wait_for_timeout(2500)   # longer than one 2s poll tick
        check("armed label survives a job poll re-render, not just the click",
              len(forget_calls) == 0 and "again" in page.inner_text("#forgetBtn").lower())
        page.click("#forgetBtn")
        page.wait_for_timeout(200)
        check("second click calls /api/forget", len(forget_calls) == 1, repr(forget_calls))
        page.unroute("**/api/forget")
        page.unroute("**/api/jobs*")

        print()
        print("/help: 200, its title, its Help button target, live sections, zero console errors")
        with urllib.request.urlopen(URL + "help", timeout=5) as hr:
            check("GET /help returns 200", hr.status == 200)
            help_body = hr.read().decode("utf-8")
        check("/help contains 'How Black Wire Forge works'", "How Black Wire Forge works" in help_body)

        help_errors = []
        help_page = browser.new_page(viewport={"width": 1440, "height": 900})
        help_page.on("pageerror", lambda e: help_errors.append("PAGEERROR: %s" % e))
        help_page.on("console", lambda m: help_errors.append("CONSOLE %s: %s" % (m.type, m.text))
                     if m.type == "error" else None)
        help_page.goto(URL + "help", wait_until="networkidle", timeout=30000)
        help_page.wait_for_timeout(1500)
        live_text = help_page.inner_text("#roomsLive")
        room_names = [rm["name"] for rm in rooms]
        check("live rooms section lists at least one real room name",
              any(n in live_text for n in room_names), repr(live_text[:300]))
        check("live rooms section does not say 'Start the app'",
              "Start the app" not in live_text, repr(live_text[:300]))
        check("zero console/page errors on /help", not help_errors, str(help_errors[:5]))
        help_page.close()

        print()
        print("help.html: an asleep lane reads 'asleep', not a false 'needs the model' claim")
        with urllib.request.urlopen(URL + "api/lanes", timeout=5) as lr:
            asleep_lanes = json.loads(lr.read())
        for l in asleep_lanes["lanes"]:
            l["up"] = False
            l["err"] = ""
        with urllib.request.urlopen(URL + "api/engines?lane=" + LANE_ID, timeout=5) as er:
            asleep_engines = json.loads(er.read())
        check("fixture: the rig lane declares the audio cap",
              "audio" in (next(l for l in asleep_lanes["lanes"] if l["id"] == LANE_ID).get("caps") or []))
        for m in asleep_engines.get("audio", {}).get("modes", []):
            if m["id"] == "song":
                m["available"] = False
                m["missing"] = []  # asleep, not actually missing anything
        asleep_page = browser.new_page(viewport={"width": 1440, "height": 900})
        asleep_page.route("**/api/lanes", lambda r, q: r.fulfill(
            status=200, content_type="application/json", body=json.dumps(asleep_lanes)))
        asleep_page.route("**/api/engines*", lambda r, q: r.fulfill(
            status=200, content_type="application/json", body=json.dumps(asleep_engines)))
        asleep_page.goto(URL + "help", wait_until="networkidle", timeout=30000)
        asleep_page.wait_for_timeout(1500)
        live_text2 = asleep_page.inner_text("#roomsLive")
        check("asleep lane: says asleep",
              "asleep" in live_text2, repr(live_text2[:500]))
        check("asleep lane: does NOT show a false '(needs ...)' missing-model claim",
              "(needs" not in live_text2.lower(), repr(live_text2[:500]))
        asleep_page.close()

        print()
        print("L5 (P2c): Help me write this goes to the room's guide -- its answer, Use these, nothing sent to /api/generate")
        helper_page = browser.new_page(viewport={"width": 1440, "height": 900})
        guard(helper_page)
        helper_page.goto(URL, wait_until="networkidle", timeout=30000)
        helper_page.wait_for_timeout(1500)
        helper_page.click(tab(helper_page, "picture"))
        helper_page.wait_for_timeout(200)
        check("L5: Help me write this is visible with a helper configured",
              helper_page.is_visible("#helperWriteBtn"))
        before = len(HELPER_REQUESTS)
        helper_page.fill("#promptBox", "a kite")
        helper_page.click("#helperWriteBtn")
        helper_page.wait_for_selector("#guideSkillUse", timeout=15000)
        check("L5: the fake helper received exactly one request", len(HELPER_REQUESTS) == before + 1)
        check("L5: the guide panel shows the guide's answer",
              "a red kite over the sea" in helper_page.inner_text("#guidePanel #guideSkill"),
              helper_page.inner_text("#guideSkill"))
        helper_screenshot = os.path.join(tempfile.gettempdir(), "bwf_l5_screenshot-picture-helper.png")
        helper_page.screenshot(path=helper_screenshot)
        print("  (screenshot saved to %s)" % helper_screenshot)
        genbefore = len(GENERATED)
        helper_page.click("#guideSkillUse")
        helper_page.wait_for_timeout(200)
        check("L5: Use these replaces the prompt",
              helper_page.input_value("#promptBox") == FakeHelperHandler.KITE,
              helper_page.input_value("#promptBox"))
        check("L5: nothing was ever sent to /api/generate", len(GENERATED) == genbefore)
        helper_page.close()

        print()
        print("L5: no helper configured -> no buttons at all")
        tmp2 = tempfile.mkdtemp(prefix="bwf-smoke-nohelper-")
        cfg2_path = os.path.join(tmp2, "config.json")
        server2_port = free_port()
        cfg = json.load(open(cfg_path))
        del cfg["helper"]
        cfg["port"] = server2_port
        json.dump(cfg, open(cfg2_path, "w"))
        logf2 = open(os.path.join(tmp2, "server.log"), "w")
        server2 = subprocess.Popen(
            [sys.executable, os.path.join(REPO, "server.py")], cwd=REPO,
            env=dict(os.environ, GENCENTER_CONFIG=cfg2_path,
                     GENCENTER_DATA=os.path.join(tmp2, "data")),
            stdout=logf2, stderr=subprocess.STDOUT)
        url2 = "http://127.0.0.1:%d/" % server2_port
        try:
            if wait_true("no-helper server is up",
                        lambda: http_json(url2 + "api/health"), 20):
                nohelper_page = browser.new_page(viewport={"width": 1440, "height": 900})
                guard(nohelper_page)
                nohelper_page.goto(url2, wait_until="networkidle", timeout=30000)
                nohelper_page.wait_for_timeout(1500)
                nohelper_page.click(tab(nohelper_page, "picture"))
                nohelper_page.wait_for_timeout(200)
                check("L5: no helper configured -> Help me write this is not shown",
                      not nohelper_page.is_visible("#helperWriteBtn"))
                check("L5: no helper configured -> Describe this picture is not shown",
                      not nohelper_page.is_visible("#helperDescribeBtn"))
                nohelper_page.close()
        finally:
            stop(server2)
            logf2.close()
            shutil.rmtree(tmp2, ignore_errors=True)

        print()
        print("N1: the room strip never pushes the page sideways; Cutting Room stays reachable")
        for w, h in [(1440, 900), (1024, 768), (768, 1024), (390, 844)]:
            vp_page = browser.new_page(viewport={"width": w, "height": h})
            guard(vp_page)
            vp_page.goto(URL, wait_until="networkidle", timeout=30000)
            vp_page.wait_for_timeout(1000)
            scroll_width = vp_page.evaluate("document.documentElement.scrollWidth")
            check("N1 %dx%d: no page-level horizontal scroll" % (w, h),
                  scroll_width <= w, "scrollWidth=%d innerWidth=%d" % (scroll_width, w))
            cutting_box = vp_page.eval_on_selector(
                ".room-cutting", "el => { const r = el.getBoundingClientRect(); return {left: r.left, right: r.right}; }")
            check("N1 %dx%d: Cutting Room button fully inside [0, innerWidth]" % (w, h),
                  cutting_box is not None and cutting_box["left"] >= 0 and cutting_box["right"] <= w,
                  str(cutting_box))
            vp_page.close()

        print()
        print("N3: the sequence picker's New sequence button stays inside the inspector, not off the right edge")
        for w, h in [(1440, 900), (1024, 768)]:
            n3_page = browser.new_page(viewport={"width": w, "height": h})
            guard(n3_page)
            n3_page.goto(URL, wait_until="networkidle", timeout=30000)
            n3_page.click(".room-cutting")
            n3_page.wait_for_selector("#seqNewBtn")
            n3_page.wait_for_timeout(300)
            btn_box = n3_page.eval_on_selector(
                "#seqNewBtn", "el => { const r = el.getBoundingClientRect(); return {left: r.left, right: r.right, width: r.width}; }")
            check("N3 %dx%d: New sequence button fully inside the viewport, not clipped" % (w, h),
                  btn_box is not None and btn_box["width"] > 0 and btn_box["left"] >= 0 and btn_box["right"] <= w,
                  str(btn_box))
            n3_page.close()

        print()
        print("N2: machine chip buttons survive polls; a chip's meter still updates in place")
        n2_page = browser.new_page(viewport={"width": 1440, "height": 900})
        guard(n2_page)
        n2_page.goto(URL, wait_until="networkidle", timeout=30000)
        n2_page.wait_for_timeout(1500)
        chip_count = n2_page.eval_on_selector_all("#machineModules .machine", "els => els.length")
        if chip_count < 1:
            check("N2: at least one machine chip exists", False)
        else:
            # Mark the first chip's node so its identity, not just its
            # content, can be checked after the page has polled several
            # times -- a rebuilt button would lose this property.
            first_handle = n2_page.evaluate_handle(
                "document.querySelector('#machineModules .machine')")
            n2_page.evaluate("el => { el.__keep = 1; }", first_handle)
            before_on = n2_page.eval_on_selector(
                "#machineModules .machine .vram", "el => el.querySelectorAll('i.on').length")
            n2_page.wait_for_timeout(7000)  # > 2 lane polls (JS polls every 3000ms)
            same_node = n2_page.evaluate(
                "el => el.__keep === 1 && el.isConnected && "
                "document.querySelector('#machineModules .machine') === el",
                first_handle)
            check("N2: the first machine chip button is the same node after 7s of polling",
                  same_node, repr(same_node))
            # The meter must still be able to change while the button node
            # stays put: move the fake lane's reported vram_free.
            req = urllib.request.Request(
                "http://127.0.0.1:%d/_control/vram" % fake_port,
                data=json.dumps({"vram_free": 1073741824}).encode(),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5).read()
            n2_page.wait_for_timeout(7000)  # server's own lane poll (1s) + > 2 JS polls
            after_on = n2_page.eval_on_selector(
                "#machineModules .machine .vram", "el => el.querySelectorAll('i.on').length")
            still_same_node = n2_page.evaluate(
                "el => el.__keep === 1 && el.isConnected && "
                "document.querySelector('#machineModules .machine') === el",
                first_handle)
            check("N2: the meter's lit-bar count changed after vram_free moved",
                  after_on != before_on, "before=%r after=%r" % (before_on, after_on))
            check("N2: the button is still the same node after its meter changed",
                  still_same_node, repr(still_same_node))
            # Reset so this doesn't leak into any test that runs after it.
            req = urllib.request.Request(
                "http://127.0.0.1:%d/_control/vram" % fake_port,
                data=json.dumps({"vram_free": 17179869184}).encode(),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5).read()
        n2_page.close()

        browser.close()

finally:
    stop(server)
    stop(fake)
    fake_helper.shutdown()
    logf.close()
    shutil.rmtree(tmp, ignore_errors=True)
check("zero console/page errors", not errors, str(errors[:5]))

print()
print(("FAILED: %d" % len(FAILED)) if FAILED else "ALL PASS")
sys.exit(1 if FAILED else 0)
