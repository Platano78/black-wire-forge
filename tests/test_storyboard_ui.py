"""Browser acceptance gate for C3.5 -- the storyboard SCRIPT lane in the Cutting Room.
(the internal sequence/storyboard design spec Sections 3, 7, 8 row C3.5.)

SELECTOR CONTRACT the builder is told to honour (none of these DOM hooks exist on HEAD;
this is my own ruling where the spec names no element -- see the delivered report §7):

  [data-script-lane]          the whole SCRIPT lane column. Present in the DOM ONLY when
                               the open sequence's mode === "storyboard" (absent, not just
                               hidden, in "sequence" mode -- same contract test_sequence_ui.py
                               already enforces for other seq-only elements).
  [data-beat-id="<id>"]       one beat's row inside [data-script-lane], one per paragraph.
  [data-beat-kind="<kind>"]   the SAME row also carries this attribute (scene/film/picture/
                               sound/note), so a scene beat is selectable as
                               [data-beat-kind="scene"] and can be asserted "visibly marked
                               as a slugline" via that attribute/class, not just its text.
  [data-beat-text]            inside a beat row: the editable paragraph control (textarea or
                               contenteditable) whose committed value drives update_beat.
  [data-stale-reason]         inside a slot's tile or inspector: a plain VISIBLE text node
                               listing that slot's stale reasons (e.g. "script changed") --
                               no hover/title-attribute required to read it.
  [data-copy-beat="<beat_id>"] a button shown on a stale beat-linked slot; fires
                               copy_beat_to_prompt for that beat.
  #seqNewMode                 a <select> (values "sequence"|"storyboard") added next to the
                               existing #seqNewTitle/#seqNewBtn in the "New sequence" row.
  #seqModeSelect               a <select> (values "sequence"|"storyboard") in the open
                               sequence's own toolbar; changing it fires set_mode.

Drives the real page in a real browser against its own fake ComfyUI lane
(tests/fixtures/fake_comfy.py) in ACCEPT mode, and its own server.py subprocess (scratch
config + scratch data dir, both on random free ports). Nothing reaches a real machine.

DO NOT EDIT: tests/test_cut.py, tests/test_cut_ui.py, tests/fixtures/fake_comfy.py.

Run: python3 tests/test_storyboard_ui.py
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

PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753"
    "de0000000c4944415478da6360606060000000050001a5f645400000000049454e44ae426082")

# --- our own fake lane, ACCEPT mode armed for a single picture output ------
fake_port = free_port()
fake_store = tempfile.mkdtemp(prefix="bwf-sb-ui-fake-")
os.makedirs(os.path.join(fake_store, "outputs"), exist_ok=True)
with open(os.path.join(fake_store, "outputs", "shot.png"), "wb") as f:
    f.write(PNG_1X1)
fake = subprocess.Popen(
    [sys.executable, os.path.join(HERE, "fixtures", "fake_comfy.py"),
     "--port", str(fake_port), "--store", fake_store],
    cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
if not wait_true("fake ComfyUI lane answers /system_stats",
                 lambda: http_json("http://127.0.0.1:%d/system_stats" % fake_port), 15):
    stop(fake)
    sys.exit(1)
http_json("http://127.0.0.1:%d/_control/accept" % fake_port,
          data=json.dumps({"outputs": [{"filename": "shot.png"}]}).encode())

# --- our own server.py: scratch config, scratch data dir, random port ------
tmp = tempfile.mkdtemp(prefix="bwf-sb-ui-")
cfg_path = os.path.join(tmp, "config.json")
server_port = free_port()
with open(cfg_path, "w") as f:
    json.dump({
        "title": "Storyboard UI Test",
        "port": server_port,
        "bind": "127.0.0.1",
        "lanes": [{"id": LANE_ID, "name": "Fake lane", "host": "127.0.0.1", "port": fake_port,
                   "caps": ["image", "video", "audio"]}],
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

def lane_has_video_mode():
    d = http_json(URL + "/api/engines?lane=" + LANE_ID)
    return any(m.get("available") for m in d.get("video", {}).get("modes", []))

if not wait_true("server is up and the fake lane offers at least one video mode",
                 lane_has_video_mode, 30):
    stop(server); stop(fake); logf.close()
    with open(os.path.join(tmp, "server.log")) as f:
        print("  -- server.py said: %s" % f.read()[-800:])
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1)

def tab(page, rid):
    return '#roomStrip [data-room-id="%s"]' % rid

# A short, real slice of the same TILL DELETE material test_storyboard.py's import
# fixture uses -- 4 beats (1 scene, 3 film) is enough to exercise the lane without a
# 30s browser run over all 11. Kept consistent in provenance with the server test.
SCRIPT_SNIPPET = (
    "INT. LUNAR APARTMENT - NIGHT.\n\n"
    "[reference generation] The target video pushes slowly into <Subject 1> as <Subject 2> "
    "sets two porcelain cups down on the steel table and offers tea to someone off screen. "
    "One continuous shot, one speaker.\n\n"
    "[reference generation] The target video holds a close-up on <Subject 2>, seated in "
    "<Subject 1>, as she refuses to look at someone off screen and tells them to stop using "
    "her dead husband's voice. One continuous shot, one speaker.\n\n"
    "[reference generation] The target video shows <Subject 3> kneeling beside <Subject 2> "
    "in <Subject 1> and asking her to confirm his own deletion. One continuous shot, two "
    "speakers, ending on her one-word answer."
)
FIRST_FILM_TEXT = ("[reference generation] The target video pushes slowly into <Subject 1> as "
                    "<Subject 2> sets two porcelain cups down on the steel table and offers tea "
                    "to someone off screen. One continuous shot, one speaker.")
EDITED_FILM_TEXT = FIRST_FILM_TEXT + " (edited)"

try:
    print("driving the real page: new STORYBOARD sequence -> script lane -> beat edit -> stale -> copy -> mode switch")
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
        check("in the Cutting Room: sequence picker visible on entry", page.is_visible("#seqPicker"))

        print("new sequence in STORYBOARD mode")
        check("a mode control exists on the New sequence row (#seqNewMode)", page.is_visible("#seqNewMode"))
        page.fill("#seqNewTitle", "Storyboard UI Test Seq")
        if page.is_visible("#seqNewMode"):
            page.select_option("#seqNewMode", "storyboard")
        page.click("#seqNewBtn")
        page.wait_for_timeout(400)
        h = page.evaluate("location.hash")
        check("URL carries #room=cutting&seq=<id>", "room=cutting" in h and "seq=s_" in h, h)
        seq_id = h.split("seq=")[1].split("&")[0]
        created = http_json(URL + "api/sequence?id=" + seq_id)
        check("the sequence was actually created in storyboard mode (server truth, not just the request sent)",
              created.get("mode") == "storyboard", created.get("mode"))

        print("SCRIPT lane present in storyboard mode; asserting we are truly in the Cutting Room")
        check("we are in the real Cutting Room (roomForm/timeline machinery reachable, not some other screen)",
              page.is_visible("#seqPicker") or page.is_visible("#tlTrackPicture"))
        check("[data-script-lane] is present in the DOM in storyboard mode",
              page.query_selector("[data-script-lane]") is not None)

        print("import the script snippet")
        check("a script import control exists (#scriptImportText/#scriptImportBtn)",
              page.is_visible("#scriptImportText") and page.is_visible("#scriptImportBtn"))
        if page.is_visible("#scriptImportText"):
            page.fill("#scriptImportText", SCRIPT_SNIPPET)
            page.click("#scriptImportBtn")
            page.wait_for_timeout(500)

        beat_rows = wait_true("4 beat rows appear in the script lane after import",
                              lambda: page.eval_on_selector_all("[data-script-lane] [data-beat-id]", "els => els.length") == 4,
                              8)
        scene_rows = page.query_selector_all('[data-script-lane] [data-beat-kind="scene"]')
        check("exactly one beat is marked data-beat-kind=\"scene\" (the slugline)", len(scene_rows) == 1, len(scene_rows))
        film_rows = page.query_selector_all('[data-script-lane] [data-beat-kind="film"]')
        check("exactly three beats are marked data-beat-kind=\"film\"", len(film_rows) == 3, len(film_rows))
        check("the scene beat is visibly marked as a slugline (its row differs from a film row, not same class)",
              bool(scene_rows) and bool(film_rows)
              and scene_rows[0].evaluate("el => el.className") != film_rows[0].evaluate("el => el.className"))

        first_film_row = page.query_selector('[data-script-lane] [data-beat-kind="film"]')
        first_beat_id = first_film_row.get_attribute("data-beat-id") if first_film_row else None
        check("a film beat row exposes its own data-beat-id", bool(first_beat_id), first_beat_id)

        print("that beat's auto-created slot pre-filled its prompt from the beat text")
        if first_beat_id:
            page.click('[data-script-lane] [data-beat-id="%s"]' % first_beat_id)
            page.wait_for_timeout(300)
            has_prompt = page.is_visible("#promptBox")
            check("selecting the beat's slot opens the recipe inspector with a prompt box",
                  has_prompt)
            if has_prompt:
                check("the prompt box is pre-filled with the beat's own text",
                      page.input_value("#promptBox") == FIRST_FILM_TEXT, page.input_value("#promptBox"))

        print("make + pick a take on that slot, so staleness has something to attach to")
        if first_beat_id and page.is_visible("#makeBtn"):
            page.click("#makeBtn")
            # Since the motion writers (P3c), a bracket in an LTX prompt is a Make-time problem:
            # this beat opens with the other video engine's "[reference generation]" prefix, so
            # Make asks first. The storyboard flow goes on with "Make anyway".
            page.wait_for_selector("#makeConfirm:not([hidden])", timeout=10000)
            check("Make asks first about the bracketed prefix in an LTX prompt",
                  "[reference generation]" in page.inner_text("#makeConfirmList"), page.inner_text("#makeConfirm"))
            page.click("#makeAnywayBtn")
            slot_sel = '#tlTrackVideo [data-slot-id]'
            wait_true("the linked video slot leaves 'empty' after Make",
                      lambda: "tl-slot-empty" not in (page.eval_on_selector(slot_sel, "el => el.className") or ""),
                      6)
            wait_true("slot reaches 'unpicked' (a take exists)",
                      lambda: "tl-slot-unpicked" in (page.eval_on_selector(slot_sel, "el => el.className") or ""), 8)
            page.click('#takesRow [data-pick]')
            page.wait_for_timeout(400)
            check("slot is 'ready' after Pick",
                  "tl-slot-ready" in (page.eval_on_selector(slot_sel, "el => el.className") or ""))
        else:
            check("make + pick a take on the beat's slot (skipped -- no beat id or no Make button)", False)

        print("edit the beat's own text in the script lane")
        if first_beat_id:
            text_el = page.query_selector('[data-script-lane] [data-beat-id="%s"] [data-beat-text]' % first_beat_id)
            check("the beat row exposes an editable [data-beat-text] control", text_el is not None)
            if text_el is not None:
                tag = page.eval_on_selector('[data-script-lane] [data-beat-id="%s"] [data-beat-text]' % first_beat_id,
                                             "el => el.tagName.toLowerCase()")
                if tag in ("textarea", "input"):
                    page.fill('[data-script-lane] [data-beat-id="%s"] [data-beat-text]' % first_beat_id, EDITED_FILM_TEXT)
                    page.keyboard.press("Tab")
                else:
                    page.evaluate(
                        """(sel, txt) => { const el = document.querySelector(sel);
                             el.innerText = txt; el.dispatchEvent(new Event('blur', {bubbles: true})); }""",
                        '[data-script-lane] [data-beat-id="%s"] [data-beat-text]' % first_beat_id, EDITED_FILM_TEXT)
                page.wait_for_timeout(500)

        print("staleness ('script changed') becomes visible WITHOUT hovering, within one poll")
        stale_visible = wait_true(
            'a [data-stale-reason] element containing "script changed" is visible with no hover',
            lambda: any("script changed" in (el.evaluate("e => e.textContent") or "")
                        for el in page.query_selector_all("[data-stale-reason]")
                        if el.evaluate("e => getComputedStyle(e).visibility !== 'hidden' "
                                       "&& getComputedStyle(e).display !== 'none'")),
            6)

        print("'Copy beat into prompt' is offered on the now-stale slot")
        copy_btn = page.query_selector('[data-copy-beat="%s"]' % first_beat_id) if first_beat_id else None
        check("[data-copy-beat] control exists for the stale beat", copy_btn is not None)
        if copy_btn is not None and copy_btn.is_visible():
            copy_btn.click()
            page.wait_for_timeout(400)
            if page.is_visible("#promptBox"):
                check("clicking Copy beat into prompt overwrites the prompt with the CURRENT (edited) beat text",
                      page.input_value("#promptBox") == EDITED_FILM_TEXT, page.input_value("#promptBox"))
        else:
            check("clicking Copy beat into prompt updates the prompt (skipped -- control not visible)", False)

        print("switching mode to 'sequence' removes the script lane from the DOM entirely")
        check("#seqModeSelect exists once a sequence is open", page.is_visible("#seqModeSelect"))
        if page.is_visible("#seqModeSelect"):
            page.select_option("#seqModeSelect", "sequence")
            page.wait_for_timeout(500)
            check("[data-script-lane] is ABSENT from the DOM in sequence mode (not merely hidden)",
                  page.query_selector("[data-script-lane]") is None)
            mode_after = http_json(URL + "api/sequence?id=" + seq_id).get("mode")
            check("the server's own record of mode is now 'sequence'", mode_after == "sequence", mode_after)
        else:
            check("[data-script-lane] absent after switching to sequence mode (skipped -- no mode control)", False)

        print("zero console/page errors across the whole run")
        # The Make-time ask above is a 409 needs_confirm answer, which the browser logs as a failed
        # resource (sometimes after the click returns): exactly one is expected, not a page error.
        conflicts = [e for e in errors if "status of 409 (Conflict)" in e]
        check("exactly one 409 was logged (the Make-time ask)", len(conflicts) == 1, errors[:10])
        check("no console or page errors were seen", len([e for e in errors if e not in conflicts]) == 0, errors[:10])

finally:
    stop(server)
    stop(fake)
    logf.close()
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.rmtree(fake_store, ignore_errors=True)

print("\n%s" % ("ALL PASS" if not FAILED else "FAILED: %d -- %s" % (len(FAILED), FAILED)))
sys.exit(1 if FAILED else 0)
