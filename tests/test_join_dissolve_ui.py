"""Acceptance gate for E2 "join: dissolve", gate 7 (R5): the Cutting Room's
shot form shows the pack's join checkbox ("Blend into the previous shot") on a
continue shot like any field, visible and TICKED for a shot that never set it
(the sequence's seq_defaults -- the field's own default is off); an autosave
of another field writes it as ticked, and unticking it is saved on the shot.
The Video room's form shows it UNticked by default (its clips stay trimmed).
The cut notes line carries the cut's join sentence.

Selector: #f_<the pack's join field id> (the page's own field-control id).

Real page, real browser (Playwright), its own fake ComfyUI lane and server.py
subprocess (scratch config + data, random ports). SKIPs without Playwright.

Run: python3 tests/test_join_dissolve_ui.py
"""
import atexit, json, os, shutil, socket, subprocess, sys, tempfile, time, urllib.error, urllib.request
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
import engines

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
SCRATCH = tempfile.mkdtemp(prefix="bwf_join_ui_")
atexit.register(lambda: ([p.terminate() for p in PROCS], shutil.rmtree(SCRATCH, ignore_errors=True)))
STORE = os.path.join(SCRATCH, "store")
os.makedirs(os.path.join(STORE, "outputs"))
LANE_PORT, PORT = free_port(), free_port()
PROCS.append(subprocess.Popen([sys.executable, os.path.join(HERE, "fixtures", "fake_comfy.py"), "--port", str(LANE_PORT),
                               "--store", STORE], cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
wait(lambda: http("http://127.0.0.1:%d/system_stats" % LANE_PORT)[0] == 200)
CFG = os.path.join(SCRATCH, "config.json")
json.dump({"title": "e2 ui", "port": PORT, "bind": "127.0.0.1",
           "lanes": [{"id": "t", "name": "Fake lane", "host": "127.0.0.1", "port": LANE_PORT,
                      "caps": ["image", "video", "audio"]}],
           "timing": {"poll_seconds": 0.4, "job_poll_seconds": 0.6, "http_timeout": 4.0,
                      "free_settle_seconds": 1.0, "discover_seconds": 300.0}}, open(CFG, "w"))
env = dict(os.environ, GENCENTER_CONFIG=CFG, GENCENTER_DATA=os.path.join(SCRATCH, "data"))
PROCS.append(subprocess.Popen([sys.executable, os.path.join(REPO, "server.py")], cwd=REPO, env=env,
                              stdout=open(os.path.join(SCRATCH, "server.log"), "w"), stderr=subprocess.STDOUT))
URL = "http://127.0.0.1:%d/" % PORT


def lane_up():
    code, d = http(URL + "api/lanes")
    lanes = d.get("lanes") if isinstance(d, dict) else d
    lane = next((x for x in lanes or [] if x.get("id") == "t"), None)
    return bool(lane and lane.get("up") and lane.get("discovered"))


if not wait(lane_up, 40):
    raise SystemExit("server/lane did not come up")

JOINS = {}
for pack in engines.packs():
    if pack["cap"] == "video":
        JOINS.update(pack.get("joins") or {})
MODE = next(iter(JOINS), None)
FIELD = JOINS[MODE]["field"]
FDECL = next(f for f in engines.fields("video", MODE) if f["id"] == FIELD)
JACK = next((f["id"] for f in engines.fields("video", MODE) if f.get("type") == "video"), None)
OTHER = next((m for m in engines.modes_for("video") if m not in JOINS), None)


def op(sid, name, **kw):
    rev = http(URL + "api/sequence?id=" + sid)[1]["rev"]
    return http(URL + "api/sequence/op", dict(kw, id=sid, rev=rev, op=name))


def add_video(sid, mode, drop=()):
    values = {f["id"]: f["default"] for f in engines.fields("video", mode) if f.get("default") is not None}
    values.update(prompt="it carries on", length=124)
    for k in drop:
        values.pop(k, None)
    code, b = op(sid, "add_slot", lane="video", cap="video", mode=mode, values=values)
    assert code == 200, b
    return b["added_slot_id"]


sid = http(URL + "api/sequence", {"title": "joins", "mode": "sequence"})[1]["id"]
v1 = add_video(sid, MODE, drop=(FIELD,))   # never set, as the page's own "+" makes a shot (values {})
v2 = add_video(sid, MODE, drop=(FIELD,))
v3 = add_video(sid, OTHER) if OTHER else None
op(sid, "patch", **{"from": v1, "to": v2, "field": JACK})
SEL = "#f_" + FIELD


def slot_values(v):
    return next(s for s in http(URL + "api/sequence?id=" + sid)[1]["slots"] if s["id"] == v).get("values") or {}


with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(URL + "#room=cutting&seq=" + sid, wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(800)
    for v, how in ((v2, "a cabled continue shot that never set the field"), (v1, "a chain-head continue shot that never set it")):
        page.click('.tl-slot[data-slot-id="%s"]' % v)
        check("(setup) %s: the form opened" % how, wait(lambda: page.is_visible("#makeBtn"), 8))
        check("%s: the '%s' checkbox is visible and ticked (the Cutting Room's default)" % (how, FDECL["label"]),
              wait(lambda: page.is_visible(SEL), 8) and page.is_checked(SEL))
        if os.environ.get("BWF_E2_SHOTS"):
            page.query_selector(SEL).scroll_into_view_if_needed()
            page.screenshot(path=os.path.join(os.environ["BWF_E2_SHOTS"], "join_%s.png" % v))
        label = page.query_selector('label[for="%s"]' % SEL[1:])
        check("%s: its label reads '%s'" % (how, FDECL["label"]),
              label is not None and label.is_visible() and label.text_content().strip() == FDECL["label"])
    # v1's form is open: editing another field autosaves the form, the box included.
    page.fill("#inspector [data-field-id=\"prompt\"]", "it carries on, faster")
    check("an autosave of another field writes the shown value: %s true, not the field's own default" % FIELD,
          wait(lambda: slot_values(v1).get("prompt") == "it carries on, faster", 8) and slot_values(v1).get(FIELD) is True,
          slot_values(v1))
    page.click('.tl-slot[data-slot-id="%s"]' % v2)
    page.wait_for_timeout(500)
    page.uncheck(SEL)
    page.wait_for_timeout(300)
    check("unticking it is saved on the shot (%s: false)" % FIELD,
          wait(lambda: slot_values(v2).get(FIELD) is False, 8), slot_values(v2))
    page.click('.tl-slot[data-slot-id="%s"]' % v1)
    page.wait_for_timeout(500)
    page.click('.tl-slot[data-slot-id="%s"]' % v2)
    check("...and it stays unticked when the shot is opened again", wait(lambda: page.is_visible(SEL), 8)
          and not page.is_checked(SEL))
    if v3:
        page.click('.tl-slot[data-slot-id="%s"]' % v3)
        page.wait_for_timeout(700)
        check("a shot whose mode declares no join has no such checkbox", not page.is_visible(SEL))
    print("the Video room: the same field, unticked by default")
    vr = browser.new_page(viewport={"width": 1440, "height": 900})
    vr.on("pageerror", lambda e: errors.append(str(e)))
    vr.goto(URL + "#room=video", wait_until="networkidle", timeout=30000)
    vr.wait_for_timeout(800)
    vr.evaluate("m => { const r = document.querySelector('input[name=engine][data-mode=\"' + m + '\"]'); "
                "if (r) { r.checked = true; r.dispatchEvent(new Event('change', {bubbles: true})); r.click(); } }", MODE)
    check("the Video room's %s form shows the checkbox, unticked" % MODE,
          wait(lambda: vr.is_visible(SEL), 8) and not vr.is_checked(SEL))
    if os.environ.get("BWF_E2_SHOTS"):
        vr.query_selector(SEL).scroll_into_view_if_needed()
        vr.screenshot(path=os.path.join(os.environ["BWF_E2_SHOTS"], "join_video_room.png"))
    note = page.evaluate("() => cutNotesText({status: 'done', loudness: null, "
                         "join_note: '1 shot join blended across the shared frames.'})")
    check("the cut notes line carries the cut's join sentence", "1 shot join blended" in (note or ""), note)
    check("no page errors", not errors, errors)
    browser.close()

print()
print("ALL PASS" if not FAILED else "FAILED: %d -- %s" % (len(FAILED), FAILED))
sys.exit(1 if FAILED else 0)
