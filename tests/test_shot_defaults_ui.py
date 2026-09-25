"""Browser gate (Playwright) for shot shape/length defaults and the Make-time
shape check in the Cutting Room: a new picture shot starts at the film's
shape, a new sound shot at the cut's length, and a picture whose shape does
not fit the shot is flagged in #makeConfirm before anything is sent.

The feature is built by someone else; most checks here are expected to FAIL
until it lands. The suite runs to the end no matter what (short waits,
helpers that return ""/None, clicks in try/except) and reports
`FAILED: N` at the end.

Run: flock /models/scratch/bwf-personas/browser.lock timeout 600 \
         python3 tests/test_shot_defaults_ui.py
"""
import shutil
import sys

if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
    print("SKIP: ffmpeg/ffprobe are not on PATH (needed to measure a take's length).")
    sys.exit(0)

import json
import os
import subprocess
import time
import urllib.error
import urllib.request

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _ui_fixture as ui  # noqa: E402
from _ui_fixture import check  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

SEQ = "s_0000eeee"
NOW = time.time()
DATA = os.path.join(ui.SCRATCH, "data")
TAKE_DIR = os.path.join(DATA, "seq", SEQ, "takes")

# ---------------------------------------------------------------- seed data
os.makedirs(os.path.join(ui.SCRATCH, "lane", "outputs"))
with open(os.path.join(ui.SCRATCH, "lane", "outputs", "square.png"), "wb") as f:
    f.write(ui.gradient_png(64, 64))

SQ = os.path.join(ui.SCRATCH, "sq.png")
WIDE = os.path.join(ui.SCRATCH, "wide.png")
with open(SQ, "wb") as f:
    f.write(ui.gradient_png(64, 64))
with open(WIDE, "wb") as f:
    f.write(ui.gradient_png(160, 90))

os.makedirs(TAKE_DIR)
subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=24:duration=6",
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-shortest",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                os.path.join(TAKE_DIR, "vid1.mp4")],
               check=True, capture_output=True)
with open(os.path.join(TAKE_DIR, "sq1.png"), "wb") as f:
    f.write(ui.gradient_png(64, 64))

os.makedirs(os.path.join(DATA, "sequences"))
P1 = {"id": "p1", "lane": "picture", "beat_id": None, "cap": "image", "mode": "t2i",
      "recipe": None, "quality": None, "values": {}, "refs": "auto",
      "takes": [{"job_id": "sq1", "made": NOW, "beat_rev": None,
                 "inputs": {"refs": [], "cables": {}}, "file": "takes/sq1.png"}],
      "pick": "sq1", "trim": None, "title": None}
V1 = {"id": "v1", "lane": "video", "beat_id": None, "cap": "video", "mode": "ltx",
      "recipe": None, "quality": None, "values": {"prompt": "a plane"}, "refs": "auto",
      "takes": [{"job_id": "vid1", "made": NOW, "beat_rev": None,
                 "inputs": {"refs": [], "cables": {}}, "file": "takes/vid1.mp4"}],
      "pick": "vid1", "trim": None, "title": None}
with open(os.path.join(DATA, "sequences", SEQ + ".json"), "w") as f:
    json.dump({"id": SEQ, "schema": 1, "rev": 1, "title": "Defaults test",
               "mode": "sequence", "created": NOW, "updated": NOW,
               "canvas": {"width": 1024, "height": 576},
               "refs": [], "beats": [], "slots": [P1, V1], "cables": [], "cuts": []}, f)

JOBS = [
    {"id": "sq1", "lane": "t", "lane_name": "Fake lane", "kind": "image", "mode": "t2i",
     "status": "done", "prompt": "square", "seed": 1, "created": NOW,
     "outputs": [{"filename": "square.png", "subfolder": "", "type": "output", "media": "image"}]},
    {"id": "vid1", "lane": "t", "lane_name": "Fake lane", "kind": "video", "mode": "ltx",
     "status": "done", "prompt": "a plane", "seed": 2, "created": NOW,
     "outputs": [{"filename": "vid1.mp4", "subfolder": "", "type": "output", "media": "video"}]},
]
URL = ui.start(JOBS)

# ---------------------------------------------------------------- helpers
REQS = []

def generates():
    return sum(1 for m, u in REQS if m == "POST" and u.endswith("/api/sequence/generate"))

def api(path):
    try:
        with urllib.request.urlopen(URL + path, timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None

def post(path, body):
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode() or "null")
        except Exception:
            return None
    except Exception:
        return None

def seq():
    return api("api/sequence?id=" + SEQ)

def slot_of(sid):
    s = seq()
    if not isinstance(s, dict) or not sid:
        return None
    for sl in s.get("slots") or []:
        if sl.get("id") == sid:
            return sl
    return None

PAGES = []   # Playwright's sync API only delivers events (requests) inside its own calls


def wait_for(fn, secs):
    deadline = time.time() + secs
    ok = False
    while time.time() < deadline:
        try:
            ok = bool(fn())
        except Exception:
            ok = False
        if ok:
            return True
        if PAGES:
            PAGES[0].wait_for_timeout(200)
        else:
            time.sleep(0.2)
    return False

def selected(page, lane):
    try:
        el = page.query_selector('#tlTrack%s [aria-pressed="true"][data-slot-id]' % lane)
        return el.get_attribute("data-slot-id") if el else None
    except Exception:
        return None

def engines():
    return api("api/engines?lane=t") or {}

def mode_fields(cap, mode):
    if not mode:
        return []
    for m in (engines().get(cap) or {}).get("modes") or []:
        if m.get("id") == mode:
            return m.get("fields") or []
    return []

def page_value(page, fid):
    try:
        el = page.query_selector('#inspector [data-field-id="%s"]' % fid)
        return el.input_value() if el else ""
    except Exception:
        return ""

def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

def confirm_text(page):
    """#makeConfirm's visible text, or "" when it is hidden/absent."""
    try:
        if page.is_visible("#makeConfirm"):
            return page.inner_text("#makeConfirm")
    except Exception:
        pass
    return ""

def thumb_ok(page, fid, secs):
    def ok():
        try:
            n = page.eval_on_selector_all(
                "#thumbs_%s img" % fid, "els => els.filter(e => e.naturalWidth > 0).length")
            return bool(n and n > 0)
        except Exception:
            return False
    return wait_for(ok, secs)

def upload_first_image_field(page, file, secs=8):
    """Set `file` on the inspector's first image file input; return (fid, thumb shown)."""
    try:
        el = page.query_selector('#inspector input[type=file][accept="image/*"]')
        if not el:
            return None, False
        fid = (el.get_attribute("id") or "").replace("upload_", "", 1)
        if not fid:
            return None, False
        page.set_input_files("#upload_" + fid, file)
        return fid, thumb_ok(page, fid, secs)
    except Exception:
        return None, False

# ---------------------------------------------------------------- the drive
def sized_picture_mode_ready():
    """Discovery has run: some picture mode with a width field is available on the lane."""
    try:
        modes = api("api/engines?lane=t")["image"]["modes"]
    except Exception:
        return False
    return any(m.get("available") and any(f.get("id") == "width" for f in m.get("fields") or []) for m in modes)


def drive(page):
    check("(setup) the lane offers a picture mode with a size", wait_for(sized_picture_mode_ready, 30))
    page.goto(URL + "#room=cutting&seq=" + SEQ, wait_until="networkidle")
    try:
        page.wait_for_selector('[data-slot-id="v1"]', timeout=15000)
    except Exception:
        pass

    def measured():
        sl = slot_of("v1") or {}
        te = sl.get("trim_effective")
        return bool(te) and abs(float(te.get("len", -1)) - 6.0) < 0.3
    check("(setup) the server measured the 6 s take", wait_for(measured, 10),
          (slot_of("v1") or {}).get("trim_effective"))

    # -- 1: a new picture shot starts at the film's shape --------------------
    try:
        page.click('#tlTrackPicture [data-add-lane="picture"]')
    except Exception:
        pass
    NEWP = None
    def newp():
        nonlocal NEWP
        sel = selected(page, "Picture")
        if sel and page.query_selector('#inspector [data-field-id="width"]'):
            NEWP = sel
            return True
        return False
    wait_for(newp, 10)
    w = num(page_value(page, "width"))
    h = num(page_value(page, "height"))
    ratio_ok = bool(w and h) and abs((int(w) / int(h)) / (1024.0 / 576.0) - 1) < 0.02
    check("a picture shot starts at the film's shape", ratio_ok, "width=%r height=%r" % (w, h))
    sv = (slot_of(NEWP) or {}).get("values") or {}
    w2, h2 = num(sv.get("width")), num(sv.get("height"))
    check("…saved on the shot", bool(w2 and h2)
          and abs((int(w2) / int(h2)) / (1024.0 / 576.0) - 1) < 0.02, sv)
    try:
        check("…and says so", "film" in page.inner_text("#inspectorMsg"),
              page.inner_text("#inspectorMsg"))
    except Exception:
        check("…and says so", False, "no #inspectorMsg")
    ui.shot(page, "defaults-1")

    # -- 2: a new sound shot starts at the cut's length ----------------------
    try:
        page.click('#tlTrackSound [data-add-lane="sound"]')
    except Exception:
        pass
    NEWS = None
    def news():
        nonlocal NEWS
        sel = selected(page, "Sound")
        if sel and slot_of(sel) is not None:
            NEWS = sel
            return True
        return False
    wait_for(news, 10)
    sslot = slot_of(NEWS) or {}
    smode = sslot.get("mode")
    fsecs = [f for f in mode_fields("audio", smode) if f.get("units") == "seconds"]
    F = next((f for f in fsecs if f.get("tier") == "primary"), fsecs[0] if fsecs else None)
    expected = (min(max(6, F["range"][0]), F["range"][1])
                if F is not None and isinstance(F.get("range"), list) and len(F["range"]) == 2 else 6)
    if F is not None:
        v = num(page_value(page, F["id"]))
        check("a sound shot starts at the cut's length", v is not None and v == expected,
              "%s=%r" % (F["id"], v))
        check("…saved on the shot", (sslot.get("values") or {}).get(F["id"]) == expected,
              sslot.get("values"))
        try:
            check("…and says so", "length of the cut" in page.inner_text("#inspectorMsg"),
                  page.inner_text("#inspectorMsg"))
        except Exception:
            check("…and says so", False, "no #inspectorMsg")
    else:
        check("a sound shot starts at the cut's length", False,
              "no seconds field in mode %r" % smode)
        check("…saved on the shot", False, "no seconds field in mode %r" % smode)
        check("…and says so", False, "no seconds field in mode %r" % smode)

    # -- 3: a square picture in a 16:9 shot is flagged before Make -----------
    try:
        page.click('#tlTrackVideo [data-add-lane="video"]')
    except Exception:
        pass
    NEWV = None
    def newv():
        nonlocal NEWV
        sel = selected(page, "Video")
        if sel and sel != "v1" and slot_of(sel) is not None:
            NEWV = sel
            return True
        return False
    wait_for(newv, 10)
    FID, shown = upload_first_image_field(page, SQ)
    check("(setup) the uploaded square shows its thumbnail",
          FID is not None and shown, "fid=%r" % FID)
    if FID:
        n0 = generates()
        try:
            page.click("#makeBtn")
        except Exception:
            pass
        page.wait_for_timeout(1500)
        txt = confirm_text(page)
        check("a square picture in a 16:9 shot is flagged before Make",
              bool(txt) and "square" in txt and "16:9" in txt, txt or "(#makeConfirm hidden)")
        af = next((f for f in mode_fields("video", (slot_of(NEWV) or {}).get("mode"))
                   if f.get("id") == FID), None)
        if af and af.get("aspect_warning"):
            check("…in the pack's own words", af["aspect_warning"] in txt, txt)
        check("nothing is sent before you answer", generates() == n0,
              "sent=%d expected=%d" % (generates(), n0))
        try:
            page.click("#makeAnywayBtn")
        except Exception:
            pass
        check("Make anyway sends it", wait_for(lambda: generates() > n0, 8),
              "sent=%d expected>%d" % (generates(), n0))
    else:
        check("a square picture in a 16:9 shot is flagged before Make", False, "no image field")
        check("nothing is sent before you answer", False, "no image field")
        check("Make anyway sends it", False, "no image field")
    ui.shot(page, "defaults-3")

    # -- 4: a 16:9 picture is not flagged ------------------------------------
    try:
        page.click('.tl-slot[data-slot-id="v1"]')
    except Exception:
        pass
    page.wait_for_timeout(1000)
    try:
        page.click('#tlTrackVideo [data-add-lane="video"]')
    except Exception:
        pass
    NEWV2 = None
    def newv2():
        nonlocal NEWV2
        sel = selected(page, "Video")
        if sel and sel != "v1" and sel != NEWV and slot_of(sel) is not None:
            NEWV2 = sel
            return True
        return False
    wait_for(newv2, 10)
    FID2, shown2 = upload_first_image_field(page, WIDE)
    if FID2:
        n1 = generates()
        try:
            page.click("#makeBtn")
        except Exception:
            pass
        sent = wait_for(lambda: generates() > n1, 8)
        check("a 16:9 picture is not flagged",
              sent and ("shape" not in confirm_text(page)),
              "sent=%r confirm=%r" % (sent, confirm_text(page)))
    else:
        check("a 16:9 picture is not flagged", False, "no image field on the second video shot")

    # -- 5: a cabled square picture says it will be cropped ------------------
    V3 = None
    r = None
    b = seq()
    if isinstance(b, dict):
        r = post("api/sequence/op", {"id": SEQ, "rev": b["rev"], "op": "add_slot",
                                     "lane": "video", "cap": "video", "mode": "ltx",
                                     "values": {"prompt": "cabled"}})
        if isinstance(r, dict) and isinstance(r.get("slots"), list):
            before = {s.get("id") for s in (b.get("slots") or [])}
            V3 = next((s.get("id") for s in r["slots"] if s.get("id") not in before), None)
    if V3 and FID and isinstance(r, dict):
        post("api/sequence/op", {"id": SEQ, "rev": r.get("rev"), "op": "patch",
                                 "from": "p1", "to": V3, "field": FID})
    def cable_in():
        s = seq()
        return bool(s) and any(c.get("to") == V3 for c in (s or {}).get("cables") or [])
    check("(setup) the cable is in", bool(V3) and wait_for(cable_in, 5),
          (seq() or {}).get("cables") if V3 else "no new slot (r=%r)" % (r,))
    if V3:
        page.reload(wait_until="networkidle")
        try:
            page.wait_for_selector('[data-slot-id="%s"]' % V3, timeout=15000)
        except Exception:
            pass
        try:
            page.click('[data-slot-id="%s"]' % V3)
        except Exception:
            pass
        page.wait_for_timeout(1500)   # the p1 tile's img must load
        n2 = generates()
        try:
            page.click("#makeBtn")
        except Exception:
            pass
        page.wait_for_timeout(1500)
        txt = confirm_text(page)
        check("a cabled square picture says it will be cropped",
              bool(txt) and "cropped" in txt, txt or "(#makeConfirm hidden)")
        check("…and nothing is sent yet", generates() == n2,
              "sent=%d expected=%d" % (generates(), n2))
    else:
        check("a cabled square picture says it will be cropped", False, "no cabled shot")
        check("…and nothing is sent yet", False, "no cabled shot")
    ui.shot(page, "defaults-5")

try:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        PAGES.append(page)
        page.on("request", lambda r: REQS.append((r.method, r.url)))
        try:
            drive(page)
        except Exception as e:
            check("the suite ran without raising", False, repr(e))
        page.close()
        browser.close()
finally:
    ui.finish()
