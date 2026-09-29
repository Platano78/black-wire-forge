"""Acceptance gate for E1 "Sing along", gate 5 (R5): the Cutting Room's shot
form shows the "Sing along" toggle (only on a shot that can sing, only while
the sequence has a song), the chain head's "Starts at", and a read-only line
on every singing shot: "Sings 0:23.0 – 0:28.2 of <song title>".

Selectors: #slotSing (the block), #singToggle (the checkbox), #singStart
(the chain head's seconds), #singLine (the read-only line).

Real page, real browser (Playwright), its own fake ComfyUI lane and server.py
subprocess (scratch config + data, random ports). SKIPs without Playwright.

Run: python3 tests/test_sing_along_ui.py
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
SCRATCH = tempfile.mkdtemp(prefix="bwf_sing_ui_")
atexit.register(lambda: ([p.terminate() for p in PROCS], shutil.rmtree(SCRATCH, ignore_errors=True)))
STORE = os.path.join(SCRATCH, "store")
os.makedirs(os.path.join(STORE, "outputs"))
LANE_PORT, PORT = free_port(), free_port()
PROCS.append(subprocess.Popen([sys.executable, os.path.join(HERE, "fixtures", "fake_comfy.py"), "--port", str(LANE_PORT),
                               "--store", STORE], cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
wait(lambda: http("http://127.0.0.1:%d/system_stats" % LANE_PORT)[0] == 200)
CFG = os.path.join(SCRATCH, "config.json")
json.dump({"title": "e1 ui", "port": PORT, "bind": "127.0.0.1",
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

SING = {}
for pack in engines.packs():
    if pack["cap"] == "video":
        SING.update(pack.get("sing_along") or {})
CONT = next((m for m, s in SING.items() if s.get("carried_frames")), None)
JACK = next((f["id"] for f in engines.fields("video", CONT) if f.get("type") == "video"), None)
OTHER = next((m for m in engines.modes_for("video") if m not in SING), None)


def op(sid, name, **kw):
    rev = http(URL + "api/sequence?id=" + sid)[1]["rev"]
    return http(URL + "api/sequence/op", dict(kw, id=sid, rev=rev, op=name))


def add_video(sid, mode, length=124):
    values = {f["id"]: f["default"] for f in engines.fields("video", mode) if f.get("default") is not None}
    values.update(prompt="he sings", length=length)
    code, b = op(sid, "add_slot", lane="video", cap="video", mode=mode, values=values)
    assert code == 200, b
    return b["added_slot_id"]


def new_seq(title):
    return http(URL + "api/sequence", {"title": title, "mode": "sequence"})[1]["id"]


# A sequence WITHOUT a song, and one WITH (a sound take made on the fake lane, then picked).
bare = new_seq("no song")
bare_v = add_video(bare, CONT)
sid = new_seq("sung")
v1 = add_video(sid, CONT)
v2 = add_video(sid, CONT)
v3 = add_video(sid, OTHER) if OTHER else None
op(sid, "patch", **{"from": v1, "to": v2, "field": JACK})
code, b = op(sid, "add_slot", lane="sound", cap="audio", mode="sfx", values={"prompt": "a drum loop", "seconds": 6})
s1 = b["added_slot_id"]
with open(os.path.join(STORE, "outputs", "song.flac"), "wb") as f:
    f.write(b"fLaC" + os.urandom(1024))
http("http://127.0.0.1:%d/_control/accept" % LANE_PORT, {"outputs": [{"filename": "song.flac", "subfolder": "", "type": "output"}]})
code, b = http(URL + "api/sequence/generate", {"id": sid, "slot_id": s1})
check("(setup) the song renders on the fake lane", code == 200 and b.get("ok"), b)
song_job = b["job"]["id"]
wait(lambda: op(sid, "pick_take", slot_id=s1, job_id=song_job)[0] == 200, 20)
check("(setup) the song is picked", http(URL + "api/sequence?id=" + sid)[1].get("song", {}).get("job_id") == song_job)


def text(page, sel):
    el = page.query_selector(sel)
    return el.text_content().strip() if el and el.is_visible() else None


with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    print("a sequence without a song offers no Sing along")
    page.goto(URL + "#room=cutting&seq=" + bare, wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(800)
    page.click('.tl-slot[data-slot-id="%s"]' % bare_v)
    page.wait_for_timeout(600)
    check("(setup) the shot's form opened", page.is_visible("#makeBtn"))
    check("no #singToggle on a shot when the sequence has no song", not page.is_visible("#singToggle"))

    print("the sung sequence: toggle, Starts at, and the Sings line")
    page = browser.new_page(viewport={"width": 1440, "height": 900})   # a hash-only goto keeps the open sequence
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(URL + "#room=cutting&seq=" + sid, wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(800)
    page.click('.tl-slot[data-slot-id="%s"]' % v1)
    check("the chain head shows a visible 'Sing along' toggle, off by default",
          wait(lambda: page.is_visible("#singToggle"), 8) and not page.is_checked("#singToggle")
          and "Sing along" in (text(page, "#slotSing") or ""), text(page, "#slotSing"))
    check("no Starts at / Sings line while it is off",
          not page.is_visible("#singStart") and not page.is_visible("#singLine"))
    page.click("#singToggle")
    check("ticking it turns Sing along on at the server",
          wait(lambda: (next(s for s in http(URL + "api/sequence?id=" + sid)[1]["slots"] if s["id"] == v1)
                        .get("sing") or {}).get("on"), 8))
    check("the chain head now shows 'Starts at (seconds)'",
          wait(lambda: page.is_visible("#singStart"), 8) and "Starts at (seconds)" in (text(page, "#slotSing") or ""))
    page.fill("#singStart", "23")
    page.dispatch_event("#singStart", "change")
    want = "Sings 0:23.0 – 0:28.2 of a drum loop"
    got = wait(lambda: text(page, "#singLine") == want and text(page, "#singLine"), 8)
    check('the head reads "%s"' % want, got == want, text(page, "#singLine"))

    page.click('.tl-slot[data-slot-id="%s"]' % v2)
    check("the continued shot shows the toggle, off", wait(lambda: page.is_visible("#singToggle")
                                                           and not page.is_checked("#singToggle"), 8))
    page.click("#singToggle")
    want2 = "Sings 0:28.2 – 0:32.4 of a drum loop"
    got2 = wait(lambda: text(page, "#singLine") == want2 and text(page, "#singLine"), 8)
    check('the continued shot reads "%s" (heard from 28.17 = 27.25 + 22/24 -- the audible span, computed, never typed)' % want2,
          got2 == want2, text(page, "#singLine"))
    check("a chained shot has no Starts at of its own", not page.is_visible("#singStart"))

    page.click('.tl-slot[data-slot-id="%s"]' % v1)
    page.wait_for_timeout(500)
    page.fill("#singStart", "10")
    page.dispatch_event("#singStart", "change")
    page.click('.tl-slot[data-slot-id="%s"]' % v2)
    want3 = "Sings 0:15.2 – 0:19.4 of a drum loop"
    got3 = wait(lambda: text(page, "#singLine") == want3 and text(page, "#singLine"), 8)
    check("moving the head's start moves the chained shot's line with it (%s)" % want3, got3 == want3,
          text(page, "#singLine"))
    if v3:
        page.click('.tl-slot[data-slot-id="%s"]' % v3)
        page.wait_for_timeout(700)
        check("a shot whose mode cannot sing offers no toggle", not page.is_visible("#singToggle"))
    print("F5: 'Use one I already made' on a sound shot")
    adopt_sid = new_seq("adopt")
    code, b = op(adopt_sid, "add_slot", lane="sound", cap="audio", mode="sfx", values={"prompt": "another"})
    a_slot = b["added_slot_id"]
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(URL + "#room=cutting&seq=" + adopt_sid, wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(800)
    page.click('.tl-slot[data-slot-id="%s"]' % a_slot)
    check("the control is visible on a sound shot with nothing made",
          wait(lambda: page.is_visible("#adoptSelect"), 8) and "Use one I already made" in (text(page, "#slotAdopt") or ""),
          text(page, "#slotAdopt"))
    opts = page.eval_on_selector_all("#adoptSelect option", "els => els.map(e => e.value)")
    check("it lists the finished sound job (and no video job)", song_job in opts, opts)
    page.select_option("#adoptSelect", song_job)
    picked = wait(lambda: next(x for x in http(URL + "api/sequence?id=" + adopt_sid)[1]["slots"]
                               if x["id"] == a_slot).get("pick") == song_job, 8)
    check("choosing it makes it the shot's pick", picked)
    check("...and it is no longer offered on that shot",
          wait(lambda: not page.is_visible("#adoptSelect"), 8))

    check("no page errors", not errors, errors)
    browser.close()

print()
print("ALL PASS" if not FAILED else "FAILED: %d -- %s" % (len(FAILED), FAILED))
sys.exit(1 if FAILED else 0)
