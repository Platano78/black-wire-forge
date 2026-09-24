"""Frozen browser acceptance gate for C3.4b's page floor
(the internal continue-feature commission doc; G1/G3/G4 -- G2/G5 are the
orchestrator's own live-gate concern, not this file's).

SELECTOR CONTRACT the builder must honour (this file is frozen and may not
be edited to match whatever the builder actually shipped). All of these
already exist in index.html for C3.4's picture->video jacks; the contract
here is that a "video"-typed jack is rendered through the SAME generic code
paths, not a bespoke one:

  .tl-jack[data-jack-slot][data-jack-field]
                       One button per entry in a video slot's own `jacks`
                       (server-derived, see tests/test_continue.py K1) --
                       this already fires for ANY jack, image or video, with
                       no per-type special-casing in the renderer. Carries
                       `data-jack-cable="<id>"` once plugged, absent
                       otherwise. Clicking an unplugged one arms
                       STATE.cablePending = {to, field}.
  .tl-slot[data-slot-id]
                       A shot tile (any lane). While a cable is pending,
                       clicking one that is a legal SOURCE for the pending
                       jack's type completes the patch (existing behaviour
                       for a picture-lane tile + an image jack; extended to
                       a video-lane tile + a video jack).
  #tlCableMsg          Visible text for a patch/unpatch refusal (existing
                       `msgCable()`; class "err" on failure).
  #inspectorMsg        Visible text for a generate refusal (existing
                       `handleSlotMake()`; class "err" on failure).
  .tl-stale span (inside .tl-slot .tl-stale)
                       A stale shot's FIRST reason, as real text content
                       (existing `slotTileHTML()`), not a title= tooltip --
                       this test reads innerText/textContent, never a
                       title attribute.
  #makeBtn             Generate the selected slot (existing).

Drives the real page in a real browser (Playwright) against its own fake
ComfyUI lane and its own server.py subprocess (scratch config + scratch
data dir, random free ports). Nothing reaches the live ~/black-wire-forge
app or its own port.

Run: python3 tests/test_continue_ui.py
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
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
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p

def http_json(url, timeout=5, data=None, method=None):
    req = urllib.request.Request(url, data=data, method=method,
                                  headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())

def http_post_json(url, payload, timeout=10):
    return http_json(url, timeout=timeout, data=json.dumps(payload).encode(), method="POST")

def http_status(url, data=None, method=None, timeout=10):
    """Like http_json, but returns (code, body) and never raises on a 4xx --
    for calls whose own refusal (the body/status) is part of what a check
    inspects, not just an unexpected-failure signal."""
    hdrs = {"Content-Type": "application/json"} if data is not None else {}
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "null")
        except Exception:
            return e.code, None

def wait_true(desc, fn, timeout, interval=0.15):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = fn()
            if last:
                return True, last
        except Exception as e:
            last = e
        time.sleep(interval)
    check(desc, False, "still false after %.0fs (last=%r)" % (timeout, last))
    return False, last

def stop(proc):
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill(); proc.wait()

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

PROCS = []


def start_fake_lane(scratch, name):
    store = os.path.join(scratch, "fake_%s" % name)
    os.makedirs(os.path.join(store, "outputs"), exist_ok=True)
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "fixtures", "fake_comfy.py"), "--port", str(port), "--store", store],
        cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    PROCS.append(proc)
    ok, _ = wait_true("fake lane %s answers /system_stats" % name,
                       lambda: http_json("http://127.0.0.1:%d/system_stats" % port), 15)
    if not ok:
        raise SystemExit("fake lane %s did not come up" % name)
    return proc, port, store


def control_accept(port, outputs):
    http_json("http://127.0.0.1:%d/_control/accept" % port,
              data=json.dumps({"outputs": outputs}).encode(), method="POST")


def start_server(scratch, lane_port, name="main"):
    data_dir = os.path.join(scratch, "data_%s" % name)
    cfg_path = os.path.join(scratch, "config_%s.json" % name)
    port = free_port()
    cfg = {"title": "C3.4b UI test", "port": port, "bind": "127.0.0.1",
           "lanes": [{"id": LANE_ID, "name": "Fake lane", "host": "127.0.0.1", "port": lane_port,
                      "caps": ["image", "video"]}],
           "timing": {"poll_seconds": 0.4, "job_poll_seconds": 0.6, "http_timeout": 4.0,
                      "free_settle_seconds": 1.0, "discover_seconds": 300.0}}
    with open(cfg_path, "w") as f:
        json.dump(cfg, f)
    logf = open(os.path.join(scratch, "server_%s.log" % name), "w")
    env = dict(os.environ, GENCENTER_CONFIG=cfg_path, GENCENTER_DATA=data_dir)
    proc = subprocess.Popen([sys.executable, os.path.join(REPO, "server.py")], cwd=REPO, env=env,
                             stdout=logf, stderr=subprocess.STDOUT)
    PROCS.append(proc)
    url = "http://127.0.0.1:%d/" % port

    def lane_up():
        d = http_json(url + "api/lanes")
        lanes = d.get("lanes") if isinstance(d, dict) else d
        l = next((x for x in (lanes or []) if x.get("id") == LANE_ID), None)
        return bool(l and l.get("up") and l.get("discovered"))  # discovered: generate before discovery is refused (503)

    ok, _ = wait_true("server %s is up and the fake lane is up" % name, lane_up, 30)
    if not ok:
        with open(os.path.join(scratch, "server_%s.log" % name)) as f:
            print("  -- server %s log tail: %s" % (name, f.read()[-1200:]))
    return proc, url, data_dir


# ---------------------------------------------------------------------------
# Discovery (never hardcoded): the first video-lane mode that declares a
# field of type "video", read straight off the live /api/engines payload --
# the same thing the page itself reads.
# ---------------------------------------------------------------------------

def discover_continue_mode(url):
    eng = http_json(url + "api/engines?lane=" + LANE_ID)
    for m in (eng.get("video") or {}).get("modes") or []:
        vf = [f for f in (m.get("fields") or []) if f.get("type") == "video"]
        if vf:
            return m["id"], vf[0]
    return None, None


def default_values_for(fields):
    field_ids = {f["id"] for f in fields}
    values = {f["id"]: f["default"] for f in fields if f.get("default") is not None}
    for text_field in ("prompt", "line"):
        if text_field in field_ids:
            values[text_field] = "a scene"
            break
    return values


def add_video_slot(url, sid, rev, mode, fields):
    code, body = http_status(url + "api/sequence/op",
                              data={"id": sid, "rev": rev, "op": "add_slot", "lane": "video", "cap": "video",
                                    "mode": mode, "values": default_values_for(fields)}, method="POST")
    if code != 200:
        raise RuntimeError("add_slot(video) failed: %r" % (body,))
    return body


def seq_get(url, sid):
    return http_json(url + "api/sequence?id=" + sid)


def seq_op(url, sid, rev, op, **kw):
    return http_status(url + "api/sequence/op", data=dict(kw, id=sid, rev=rev, op=op), method="POST")


def patch_cable(url, sid, rev, from_id, to_id, field_id):
    code, body = seq_op(url, sid, rev, "patch", **{"from": from_id, "to": to_id, "field": field_id})
    if code != 200:
        raise RuntimeError("patch failed: %r" % (body,))
    return body


def generate_and_harvest(url, sid, slot_id, lane_port, fname, fake_store, timeout=20):
    # The fake lane's /view 404s for a filename with no real bytes under its
    # own outputs/ dir -- the server's harvester (and later the cable's own
    # _cut_ensure_take_file re-copy) both actually fetch over HTTP, so a
    # canned filename with nothing behind it harvests as a silent 404.
    with open(os.path.join(fake_store, "outputs", fname), "wb") as f:
        f.write(os.urandom(256))
    control_accept(lane_port, [{"filename": fname, "subfolder": "", "type": "output"}])
    code, body = http_status(url + "api/sequence/generate", timeout=timeout,
                              data={"id": sid, "slot_id": slot_id}, method="POST")
    if code != 200 or not body.get("ok"):
        raise RuntimeError("generate failed: %r" % (body,))
    job_id = body["job"]["id"]
    control_accept(lane_port, None)

    def harvested():
        seq = seq_get(url, sid)
        slot = next(s for s in seq["slots"] if s["id"] == slot_id)
        take = next((t for t in slot["takes"] if t["job_id"] == job_id), None)
        return take if take and take.get("file") else None

    wait_true("(setup) take %s harvested" % job_id, harvested, timeout)
    return job_id


def pick(url, sid, rev, slot_id, job_id):
    code, body = seq_op(url, sid, rev, "pick_take", slot_id=slot_id, job_id=job_id)
    if code != 200:
        raise RuntimeError("pick_take failed: %r" % (body,))
    return body


SCRATCH = tempfile.mkdtemp(prefix="bwf-continue-ui-")
print("scratch dir: %s (real data/ and the live app are never touched)" % SCRATCH)

import atexit
def _cleanup():
    for p in PROCS:
        stop(p)
    shutil.rmtree(SCRATCH, ignore_errors=True)
atexit.register(_cleanup)

fake_proc, fake_port, fake_store = start_fake_lane(SCRATCH, "t")
srv_proc, URL, DATA_DIR = start_server(SCRATCH, fake_port, "main")

CONTINUE_MODE, VIDEO_FIELD = discover_continue_mode(URL)
check("sanity: /api/engines lists a video mode with a field of type \"video\" "
      "(K5's continue mode) -- without this NOTHING below can be exercised",
      CONTINUE_MODE is not None, CONTINUE_MODE)

if CONTINUE_MODE is not None:
    VIDEO_FIELD_ID = VIDEO_FIELD["id"]
    eng = http_json(URL + "api/engines?lane=" + LANE_ID)
    CONTINUE_FIELDS = next(m["fields"] for m in eng["video"]["modes"] if m["id"] == CONTINUE_MODE)
    print("     continue mode: %r  video field: %r" % (CONTINUE_MODE, VIDEO_FIELD_ID))

    seq1 = http_post_json(URL + "api/sequence", {"title": "link+refusal", "mode": "sequence"})
    sid1, rev1 = seq1["id"], seq1["rev"]
    b = add_video_slot(URL, sid1, rev1, CONTINUE_MODE, CONTINUE_FIELDS); rev1 = b["rev"]
    v1a = b["slots"][0]["id"]
    b = add_video_slot(URL, sid1, rev1, CONTINUE_MODE, CONTINUE_FIELDS); rev1 = b["rev"]
    v1b = b["slots"][1]["id"]

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))

            print("part 1: jack click -> earlier-tile click creates the continue link; "
                  "generate refusal shows the server's sentence")
            page.goto(URL + "#room=cutting&seq=%s" % sid1, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(1000)
            check("really landed in the Cutting Room on this sequence",
                  "seq=%s" % sid1 in page.evaluate("location.hash"), page.evaluate("location.hash"))

            jack_sel = '.tl-jack[data-jack-slot="%s"][data-jack-field="%s"]' % (v1b, VIDEO_FIELD_ID)
            jack = page.query_selector(jack_sel)
            check("[data-jack-slot][data-jack-field] exists on the later shot for the video field "
                  "(the same generic jack renderer C3.4 uses for image jacks)", jack is not None, jack_sel)

            if jack is not None:
                page.click(jack_sel)
                page.wait_for_timeout(200)
                tile_sel = '.tl-slot[data-slot-id="%s"]' % v1a
                page.click(tile_sel)
                page.wait_for_timeout(400)
                cabled = page.query_selector(jack_sel + "[data-jack-cable]")
                check("clicking the jack then the EARLIER video shot's tile creates the link "
                      "(the jack now carries data-jack-cable)", cabled is not None, jack_sel)
                api_after = seq_get(URL, sid1)
                cable = next((c for c in api_after.get("cables") or []
                              if c.get("from") == v1a and c.get("to") == v1b and c.get("field") == VIDEO_FIELD_ID), None)
                check("the server's own record of the sequence agrees a cable was created",
                      cable is not None, api_after.get("cables"))

            page.click('.tl-slot[data-slot-id="%s"]' % v1b)
            page.wait_for_timeout(300)
            check("selecting the later shot opens the inspector (Make appears)", page.is_visible("#makeBtn"))
            if page.is_visible("#makeBtn"):
                page.click("#makeBtn")
                seen, _ = wait_true(
                    "#inspectorMsg shows a refusal after Make (source not picked)",
                    lambda: "not made yet" in (page.eval_on_selector(
                        "#inspectorMsg", "el => el.textContent || ''") or ""), 8)
                if seen:
                    text = page.eval_on_selector("#inspectorMsg", "el => el.textContent || ''").strip()
                    check('#inspectorMsg\'s visible text is exactly the server\'s sentence '
                          '"The shot this continues from is not made yet."',
                          text == "The shot this continues from is not made yet.", text)
            browser.close()
    except Exception as e:
        check("part 1 ran without raising", False, repr(e))

    print("part 2: after re-picking the source, the target shows 'previous shot changed' "
          "as visible text, without hover")
    seq2 = http_post_json(URL + "api/sequence", {"title": "stale display", "mode": "sequence"})
    sid2, rev2 = seq2["id"], seq2["rev"]
    b = add_video_slot(URL, sid2, rev2, CONTINUE_MODE, CONTINUE_FIELDS); rev2 = b["rev"]
    v2src = b["slots"][0]["id"]
    b = add_video_slot(URL, sid2, rev2, CONTINUE_MODE, CONTINUE_FIELDS); rev2 = b["rev"]
    v2dst = b["slots"][1]["id"]

    try:
        b = patch_cable(URL, sid2, rev2, v2src, v2dst, VIDEO_FIELD_ID); rev2 = b["rev"]

        job_old = generate_and_harvest(URL, sid2, v2src, fake_port, "c34b_src_old.mp4", fake_store)
        rev2 = seq_get(URL, sid2)["rev"]
        b = pick(URL, sid2, rev2, v2src, job_old); rev2 = b["rev"]

        job_dst = generate_and_harvest(URL, sid2, v2dst, fake_port, "c34b_dst.mp4", fake_store)
        rev2 = seq_get(URL, sid2)["rev"]
        b = pick(URL, sid2, rev2, v2dst, job_dst); rev2 = b["rev"]

        job_new = generate_and_harvest(URL, sid2, v2src, fake_port, "c34b_src_new.mp4", fake_store)
        rev2 = seq_get(URL, sid2)["rev"]
        b = pick(URL, sid2, rev2, v2src, job_new); rev2 = b["rev"]

        api_seq2 = seq_get(URL, sid2)
        dst_slot_api = next(s for s in api_seq2["slots"] if s["id"] == v2dst)
        check("(setup) the server's own record already shows the target stale",
              bool(dst_slot_api.get("stale")), dst_slot_api.get("stale"))

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(URL + "#room=cutting&seq=%s" % sid2, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(1200)

            tile_sel = '.tl-slot[data-slot-id="%s"] .tl-stale span' % v2dst
            seen, _ = wait_true("the target tile shows a .tl-stale span", lambda: page.query_selector(tile_sel), 8)
            if seen:
                visible = page.eval_on_selector(
                    tile_sel,
                    "el => { const s = getComputedStyle(el); return s.display !== 'none' "
                    "&& s.visibility !== 'hidden' && el.offsetParent !== null; }")
                check(".tl-stale span is actually rendered (not display:none/visibility:hidden, "
                      "not merely a title= tooltip)", visible, visible)
                text = page.eval_on_selector(tile_sel, "el => el.textContent || ''").strip()
                check("'previous shot changed' is visible as real text content on the target's tile",
                      text == "previous shot changed", text)
            browser.close()
    except Exception as e:
        check("part 2 ran without raising", False, repr(e))
else:
    for rule_id in ("G1: a visible control to continue from the shot before",
                     "G3 (part 1): the jack+tile click creates the link and the server agrees",
                     "G3 (part 1): a generate refusal shows the server's sentence as visible text",
                     "G4 (part 2): 'previous shot changed' is visible on the target's tile without hover"):
        check("%s (cannot exercise -- no continue mode discovered on /api/engines)" % rule_id, False)


print()
print("ALL PASS" if not FAILED else "FAILED: %d -- %s" % (len(FAILED), FAILED))
sys.exit(1 if FAILED else 0)
