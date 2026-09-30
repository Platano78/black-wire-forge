"""Acceptance gate for the four rough spots from the release gate's gate 7 (RS1-RS4).

RS1  first load while discovery is still running: the engine menu stays closed,
     engines read "checking..." (never "needs ..."), and after discovery answers a
     missing model shows "needs ..." only when the user opens the menu.
RS2  the Cutting Room's "Kind of shot": a new video shot can be turned into a mode
     that can sing along; locked (with a sentence) once the shot has takes;
     update_slot {mode} prunes values the new mode does not declare.
RS3  song_seconds in the derived sequence once the song take is picked/adopted, and
     the "Sings" line becomes a warning sentence when a shot sings past the end.
RS4  a failed process job shows the last lines its program printed under the error.

Real page, real browser (Playwright), scratch config + data, random ports; the
song is a real 10 s WAV, so RS3 runs the real ffprobe. SKIPs without Playwright.

Run: python3 tests/test_rough_spots_ui.py
"""
import atexit, json, os, shutil, socket, subprocess, sys, tempfile, time, urllib.error, urllib.request, wave
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
import engines
import runner

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail != "" else ""))
    if not cond:
        FAILED.append(name)

try:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as _pw:
        _pw.chromium.launch().close()
except Exception as e:
    print("SKIP: playwright or its chromium is not installed (%s). pip install -r requirements-dev.txt "
          "&& python3 -m playwright install --with-deps chromium" % e)
    sys.exit(0)
if not shutil.which("ffprobe"):
    print("SKIP: ffprobe is not installed, so a song's length cannot be probed")
    sys.exit(0)


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p


def http(url, data=None, timeout=10):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                                 headers={"Content-Type": "application/json"} if data is not None else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "null")


def wait(fn, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = fn()
            if v:
                return v
        except Exception:
            pass
        time.sleep(0.2)
    return None


PROCS = []
SCRATCH = tempfile.mkdtemp(prefix="bwf_rs_")
atexit.register(lambda: ([p.terminate() for p in PROCS], shutil.rmtree(SCRATCH, ignore_errors=True)))


def start_server(name, lanes_cfg, lane_port, lane_args=(), jobs=None, extra_cfg=None):
    """A fake ComfyUI lane on `lane_port` (already asked for by lanes_cfg) and a server.py
    subprocess on a scratch config. -> (url, lane store dir)."""
    store = os.path.join(SCRATCH, name, "lane")
    os.makedirs(os.path.join(store, "outputs"), exist_ok=True)
    PROCS.append(subprocess.Popen([sys.executable, os.path.join(HERE, "fixtures", "fake_comfy.py"), "--port",
                                   str(lane_port), "--store", store] + list(lane_args), cwd=REPO,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    wait(lambda: http("http://127.0.0.1:%d/system_stats" % lane_port)[0] == 200)
    data = os.path.join(SCRATCH, name, "data")
    os.makedirs(data, exist_ok=True)
    if jobs:
        json.dump(jobs, open(os.path.join(data, "jobs.json"), "w"))
    port = free_port()
    cfg = os.path.join(SCRATCH, name, "config.json")
    json.dump(dict({"title": name, "port": port, "bind": "127.0.0.1", "lanes": lanes_cfg,
                    "timing": {"poll_seconds": 0.4, "job_poll_seconds": 0.6, "http_timeout": 4.0,
                               "free_settle_seconds": 1.0, "discover_seconds": 300.0}}, **(extra_cfg or {})),
              open(cfg, "w"))
    env = dict(os.environ, GENCENTER_CONFIG=cfg, GENCENTER_DATA=data)
    PROCS.append(subprocess.Popen([sys.executable, os.path.join(REPO, "server.py")], cwd=REPO, env=env,
                                  stdout=open(os.path.join(SCRATCH, name, "server.log"), "w"), stderr=subprocess.STDOUT))
    url = "http://127.0.0.1:%d/" % port
    wait(lambda: http(url + "api/health")[0] == 200, 30)
    return url, store


def text(page, sel):
    el = page.query_selector(sel)
    return el.text_content().strip() if el and el.is_visible() else None


def act(fn):
    """An action whose absence is what a check right after it reports: never a crash."""
    try:
        fn()
    except Exception as e:
        print("  (action failed: %s)" % str(e).splitlines()[0][:90])


def cls(page, sel):
    try:
        return page.get_attribute(sel, "class", timeout=2000) or ""
    except Exception:
        return None


def val(page, sel):
    try:
        return page.input_value(sel, timeout=2000)
    except Exception:
        return None


def disabled(page, sel):
    try:
        return page.is_disabled(sel, timeout=2000)
    except Exception:
        return None


errors = []
with sync_playwright() as pw:
    browser = pw.chromium.launch()

    # ------------------------------------------------------------------ RS1
    print("RS1: discovery still running -> menu closed, 'checking...', no 'needs'")
    gate = os.path.join(SCRATCH, "rs1.gate")
    lp = free_port()
    url1, _ = start_server("rs1", [{"id": "t", "name": "Fake lane", "host": "127.0.0.1", "port": lp,
                                     "caps": ["image", "video", "audio"]}], lp,
                           lane_args=["--object-info-gate", gate])
    check("(setup) the lane is up but discovery has not answered",
          wait(lambda: (lambda d: d["lanes"][0]["up"] and not d["lanes"][0]["discovered"])(http(url1 + "api/lanes")[1]), 20))
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(url1 + "#room=video", wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(1500)
    check("(setup) discovery is still running while the page is looked at",
          not http(url1 + "api/lanes")[1]["lanes"][0]["discovered"])
    check("the engine menu is closed on load", not page.is_visible("#enginePicker"))
    chip = page.text_content("#enginePickerWrap") or ""
    check("the chip reads 'checking…'", "checking…" in chip, chip)
    body = page.inner_text("body")
    check("no 'needs' text anywhere on the page", "needs" not in body.lower(), [l for l in body.splitlines() if "needs" in l.lower()][:3])
    page.click("#engineChipBtn")
    page.wait_for_timeout(300)
    rows = page.eval_on_selector_all("#enginePicker .engine-row", "els => els.map(e => e.innerText)")
    check("opening it by click shows 'checking…' on the engines, still no 'needs'",
          bool(rows) and all("checking…" in r and "needs" not in r.lower() for r in rows), rows)
    page.click("#engineChipBtn")   # close it again
    open(gate, "w").close()        # discovery may now answer
    check("(setup) discovery answered", wait(lambda: http(url1 + "api/lanes")[1]["lanes"][0]["discovered"], 30))
    page.wait_for_function("() => !document.querySelector('#enginePickerWrap').innerText.includes('checking')", timeout=15000)
    check("after it answered the menu is still closed (it opens only on a click)", not page.is_visible("#enginePicker"))
    eng = http(url1 + "api/engines?lane=t")[1]
    lacking = next((r["id"] for r in eng["rooms"] if len(r["modes"]) >= 2 and any(
        not next(m for m in eng[x["cap"]]["modes"] if m["id"] == x["mode"])["available"] for x in r["modes"])), None)
    check("(setup) some room has an engine whose model this lane lacks", lacking is not None)
    page.goto(url1 + "#room=" + lacking, wait_until="networkidle", timeout=30000)
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(800)
    check("(after discovery) a fresh load still leaves the menu closed", not page.is_visible("#enginePicker"))
    page.click("#engineChipBtn")
    page.wait_for_timeout(300)
    rows = page.eval_on_selector_all("#enginePicker .engine-row", "els => els.map(e => e.innerText)")
    check("opened by the user, a missing model reads 'needs …'", any("needs " in r for r in rows), rows)
    check("...and no row says 'checking…' any more", not any("checking" in r for r in rows), rows)
    page.close()

    # ------------------------------------------------------------- RS2 / RS3 / RS4
    lp = free_port()
    cmd = [sys.executable, "-c",
           "import sys; print('working'); print('rs4-boom: cannot open /work/model.blend', file=sys.stderr); sys.exit(3)"]
    ok, tail, err = runner.run_steps([{"argv": cmd, "timeout_s": 30}], SCRATCH)
    check("(setup) runner.run_steps fails and keeps the program's last lines", (not ok) and "exit code 3" in err
          and any("rs4-boom" in x for x in tail), (ok, tail, err))
    FAILED_JOB = {"id": "rs4job00001", "lane": "cpu", "lane_name": "This machine", "kind": "3d", "mode": "turntable",
                  "status": "error", "prompt": "", "args": {}, "created": time.time(), "error": err,
                  "outputs": [], "notes": ["  ", "noise <b>1</b>", "noise 2"] + tail}
    url, store = start_server("main", [{"id": "t", "name": "Fake lane", "host": "127.0.0.1", "port": lp,
                                         "caps": ["image", "video", "audio"]},
                                        {"id": "cpu", "name": "This machine", "kind": "process", "caps": ["3d"]}],
                              lp, jobs=[FAILED_JOB])
    check("(setup) the lane is up and discovered", wait(lambda: any(
        l["id"] == "t" and l["up"] and l["discovered"] for l in http(url + "api/lanes")[1]["lanes"]), 40))

    def op(sid, name, **kw):
        rev = http(url + "api/sequence?id=" + sid)[1]["rev"]
        return http(url + "api/sequence/op", dict(kw, id=sid, rev=rev, op=name))

    def new_seq(title):
        return http(url + "api/sequence", {"title": title, "mode": "sequence"})[1]["id"]

    def slot_of(sid, slot_id):
        return next(s for s in http(url + "api/sequence?id=" + sid)[1]["slots"] if s["id"] == slot_id)

    SING = {}
    for pack in engines.packs():
        if pack["cap"] == "video":
            SING.update(pack.get("sing_along") or {})
    modes = engines.modes_for("video")
    FIRST = modes[0]
    CONT = next(m for m, s in SING.items() if s.get("carried_frames"))
    check("(setup) a new video shot's default mode cannot sing, another mode can", FIRST not in SING and CONT in SING, (FIRST, CONT))

    # a 10 s song on the lane
    with wave.open(os.path.join(store, "outputs", "song.wav"), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000); w.writeframes(b"\0\0" * 8000 * 10)
    http("http://127.0.0.1:%d/_control/accept" % lp, {"outputs": [{"filename": "song.wav", "subfolder": "", "type": "output"}]})

    def make_song(sid):
        code, b = op(sid, "add_slot", lane="sound", cap="audio", mode="sfx", values={"prompt": "a drum loop", "seconds": 6})
        s1 = b["added_slot_id"]
        code, b = http(url + "api/sequence/generate", {"id": sid, "slot_id": s1})
        assert code == 200 and b.get("ok"), b
        return s1, b["job"]["id"]

    def add_video(sid, mode, length=124):
        values = {f["id"]: f["default"] for f in engines.fields("video", mode) if f.get("default") is not None}
        values.update(prompt="he sings", length=length)
        code, b = op(sid, "add_slot", lane="video", cap="video", mode=mode, values=values)
        assert code == 200, b
        return b["added_slot_id"]

    # -- RS3 API: pick_take, then adopt_take
    print("RS3: song_seconds appears in the derived sequence once the song is picked / adopted")
    sid = new_seq("sung")
    s1, song_job = make_song(sid)
    wait(lambda: http(url + "api/jobs?limit=60")[1] and any(
        j["id"] == song_job and j["status"] == "done" for j in http(url + "api/jobs?limit=60")[1]["jobs"]), 20)
    check("(setup) the song render finished", wait(lambda: op(sid, "pick_take", slot_id=s1, job_id=song_job)[0] == 200, 20))
    got = wait(lambda: http(url + "api/sequence?id=" + sid)[1].get("song_seconds"), 20)
    check("pick_take on the song slot -> song_seconds == 10.0", got is not None and abs(got - 10.0) < 0.05, got)
    sid2 = new_seq("adopted")
    code, b = op(sid2, "add_slot", lane="sound", cap="audio", mode="sfx", values={"prompt": "another"})
    a_slot = b["added_slot_id"]
    check("(before) a sequence with no song has no song_seconds", "song_seconds" not in http(url + "api/sequence?id=" + sid2)[1])
    code, b = op(sid2, "adopt_take", slot_id=a_slot, job_id=song_job)
    check("(setup) adopt_take took the finished song", code == 200, b)
    got = wait(lambda: http(url + "api/sequence?id=" + sid2)[1].get("song_seconds"), 20)
    check("adopt_take on the song slot -> song_seconds == 10.0", got is not None and abs(got - 10.0) < 0.05, got)

    # -- RS2 API: update_slot {mode} prunes values, resets recipe / quality
    print("RS2: update_slot {mode}")
    sid3 = new_seq("kinds")
    a = FIRST
    b_mode = None
    for cand in modes:
        if cand == a:
            continue
        gone = {f["id"] for f in engines.fields("video", a)} - {f["id"] for f in engines.fields("video", cand)}
        if gone:
            b_mode, dropped = cand, sorted(gone)[0]
            break
    check("(setup) two video modes where the first declares a field the second does not", b_mode is not None, (a, modes))
    vslot = add_video(sid3, a)
    recipe = (engines.presets("video", a) or [{}])[0].get("id")
    quality = (engines.quality("video", a) or [{}])[0].get("id")
    code, r = op(sid3, "update_slot", slot_id=vslot, values={dropped: (engines.fields("video", a) and next(
        f for f in engines.fields("video", a) if f["id"] == dropped).get("default", 1)) or 1},
        **({"recipe": recipe} if recipe else {}), **({"quality": quality} if quality else {}))
    check("(setup) the shot carries a value only its first mode declares (and a recipe / quality)", code == 200, r)
    code, r = op(sid3, "update_slot", slot_id=vslot, mode=b_mode)
    sl = slot_of(sid3, vslot)
    check("update_slot {mode} switches the mode", code == 200 and sl["mode"] == b_mode, (code, r))
    check("...and prunes the value the new mode does not declare", dropped not in sl["values"], sl["values"])
    check("...and keeps values both declare (the prompt)", sl["values"].get("prompt") == "he sings", sl["values"])
    check("...and resets recipe and quality", sl["recipe"] is None and sl["quality"] is None, (sl["recipe"], sl["quality"]))

    # -- RS2 + RS3 in the page
    print("RS2/RS3 in the Cutting Room")
    sid4 = sid   # has the song
    v_new = add_video(sid4, FIRST, length=124)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(url + "#room=cutting&seq=" + sid4, wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(800)
    act(lambda: page.click('.tl-slot[data-slot-id="%s"]' % v_new, timeout=3000))
    check("a new video shot shows 'Kind of shot'", wait(lambda: page.is_visible("#kindSelect"), 8)
          and "Kind of shot" in (text(page, "#slotKind") or ""), text(page, "#slotKind"))
    check("...on the first mode, enabled", val(page, "#kindSelect") == FIRST and disabled(page, "#kindSelect") is False)
    opts = page.eval_on_selector_all("#kindSelect option", "els => els.map(e => [e.value, e.textContent])")
    words = engines.mode_words("video")
    check("its options are the kind's modes, in the engines' own words",
          [o[0] for o in opts] == modes and all(o[1] == words.get(o[0], o[0]) for o in opts), opts)
    check("(before) no Sing along on the mode that cannot sing", not page.is_visible("#singToggle"))
    if page.is_visible("#kindSelect"):
        page.select_option("#kindSelect", CONT)
    check("changing it sends update_slot: the shot is now that mode",
          wait(lambda: slot_of(sid4, v_new)["mode"] == CONT, 8))
    check("the Sing along toggle appears", wait(lambda: page.is_visible("#singToggle"), 8))
    check("the select shows the new kind", val(page, "#kindSelect") == CONT)
    # RS3 in the page: this shot is the head; Starts at 0 -> 0:00.0 - 0:05.2 of a 10 s song
    if not page.is_visible("#singToggle"):   # the base has no way to reach it from here; go through the API
        op(sid4, "update_slot", slot_id=v_new, mode=CONT)
        page.reload(wait_until="networkidle"); page.wait_for_timeout(800)
        page.click('.tl-slot[data-slot-id="%s"]' % v_new)
        wait(lambda: page.is_visible("#singToggle"), 8)
    act(lambda: page.click("#singToggle", timeout=3000))
    got = wait(lambda: (text(page, "#singLine") or "").startswith("Sings 0:00.0 – 0:05.2 of ") and text(page, "#singLine"), 8)
    check("within the song: the plain 'Sings … of <song>' line", bool(got) and "past the end" not in got, text(page, "#singLine"))
    check("...and not in the warning style", cls(page, "#singLine") is not None and "warn" not in cls(page, "#singLine"))
    act(lambda: (page.fill("#singStart", "6", timeout=3000), page.dispatch_event("#singStart", "change")))
    want = "Sings 0:06.0 – 0:11.2, past the end of the song (0:10.0). Start it earlier or make it shorter."
    got = wait(lambda: text(page, "#singLine") == want and text(page, "#singLine"), 8)
    check('past the end: "%s"' % want, got == want, text(page, "#singLine"))
    check("...in the warning style", "warn" in (cls(page, "#singLine") or ""))
    act(lambda: (page.fill("#singStart", "4", timeout=3000), page.dispatch_event("#singStart", "change")))
    got = wait(lambda: (text(page, "#singLine") or "").startswith("Sings 0:04.0 – 0:09.2 of "), 8)
    check("moved earlier, it is the plain line again", bool(got), text(page, "#singLine"))
    # a shot with a take: locked
    code, b = http(url + "api/sequence/generate", {"id": sid4, "slot_id": v_new})
    if not (code == 200 and b.get("ok")):
        print("  (a video take could not be started on the fake lane: %s %s)" % (code, b))
    http("http://127.0.0.1:%d/_control/accept" % lp, {"outputs": [{"filename": "song.wav", "subfolder": "", "type": "output"}]})
    check("(setup) the shot now has a take", wait(lambda: slot_of(sid4, v_new).get("takes"), 20), b)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(url + "#room=cutting&seq=" + sid4, wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(800)
    page.click('.tl-slot[data-slot-id="%s"]' % v_new)
    check("a shot with a take shows 'Kind of shot' disabled", wait(lambda: page.is_visible("#kindSelect"), 8)
          and disabled(page, "#kindSelect") is True)
    check("...with the sentence 'Make a new shot to change its kind'",
          "Make a new shot to change its kind" in (text(page, "#kindLockNote") or ""), text(page, "#slotKind"))
    check("the Sing along toggle is still there for the shot's own mode", page.is_visible("#singToggle"))
    page.close()

    # -- RS4
    print("RS4: a failed process job shows what its program printed")
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(url + "#room=3d", wait_until="networkidle", timeout=30000)
    page.wait_for_selector('#binBody tr[data-job="rs4job00001"]', timeout=15000)
    page.click('#binBody tr[data-job="rs4job00001"]')
    page.wait_for_timeout(1500)
    tail_text = text(page, "#jobTail") or ""
    check("the error sentence is still shown", "stopped with exit code 3" in page.inner_text("#monitor"), page.inner_text("#monitor"))
    check("the program's own line is shown under it", "rs4-boom: cannot open /work/model.blend" in tail_text, tail_text)
    check("only the last 3 non-empty lines (the older noise is not)", "noise 2" in tail_text and "noise <b>1</b>" not in tail_text
          and len([l for l in tail_text.splitlines() if l.strip()]) == 3, tail_text)
    fam = page.eval_on_selector("#jobTail", "e => getComputedStyle(e).fontFamily").lower() if tail_text else ""
    check("it is monospace", "mono" in fam or "menlo" in fam, fam)
    check("its HTML is escaped, never interpreted (noise <b>1</b> literal)",
          bool(tail_text) and page.eval_on_selector("#jobTail", "e => e.querySelector('b') === null"))
    page.close()

    check("no page errors", not errors, errors)
    browser.close()

print()
print("ALL PASS" if not FAILED else "FAILED: %d -- %s" % (len(FAILED), FAILED))
sys.exit(1 if FAILED else 0)
