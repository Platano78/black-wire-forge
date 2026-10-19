"""Browser check for the beat-snap controls in the Cutting Room.

Drives the real page in Playwright against the real server.py (scratch config and data, random ports) and a fake lane.
Everything the page shows is the server's own derived GET: the master is imported through /api/sequence/master and the
song map set through the op with that file's sha256, so the bars stepper and the read-out exercise real windows.
The analyze job states (running / error) are a stubbed GET (no grid lane exists in this harness); the real server's
refusal for "no lane can find the beats" is checked unstubbed.

Covered: the Find the beats button in every state, the analyze POST body, the Snap to beats enable rule and what ticking
stores, snap_report.warnings / snap_error verbatim, the Bars stepper (server value, read-out text, minus disabled at 1),
"Ends at song end" on the last shot only, end_at, the length field locked, "Follows the song" (LTX lipsync, H3 sing),
the greyed Starts at, the reflow sentence, and that none of the new ids exist with audio-led off.

The harness block is copied from tests/test_audio_led_ui.py (a frozen file that cannot be imported). Run:
python3 tests/test_bar_snap_ui.py
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

def start_fake_lane(scratch, name, output_clip_path=None):
    store = os.path.join(scratch, "fake_%s" % name)
    os.makedirs(os.path.join(store, "outputs"), exist_ok=True)
    if output_clip_path:
        # Always served under "ui_ready.mp4" -- the one filename
        # build_sequence_with_a_picked_shot() arms /_control/accept with.
        shutil.copy(output_clip_path, os.path.join(store, "outputs", "ui_ready.mp4"))
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "fixtures", "fake_comfy.py"), "--port", str(port), "--store", store],
        cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    PROCS.append(proc)
    ok = wait_true("fake lane %s answers /system_stats" % name,
                    lambda: http_json("http://127.0.0.1:%d/system_stats" % port), 15)
    if not ok:
        raise SystemExit("fake lane %s did not come up" % name)
    return proc, port, store


def start_server(scratch, lane_port, name, path_value, cut_config=None):
    data_dir = os.path.join(scratch, "data_%s" % name)
    cfg_path = os.path.join(scratch, "config_%s.json" % name)
    port = free_port()
    cfg = {"title": "C3.6 UI test", "port": port, "bind": "127.0.0.1",
           "lanes": [{"id": LANE_ID, "name": "Fake lane", "host": "127.0.0.1", "port": lane_port,
                      "caps": ["image", "video", "audio"]}],
           "timing": {"poll_seconds": 0.4, "job_poll_seconds": 1.0, "http_timeout": 4.0,
                      "free_settle_seconds": 1.0, "discover_seconds": 300.0}}
    if cut_config is not None:
        cfg["cut"] = cut_config
    with open(cfg_path, "w") as f:
        json.dump(cfg, f)
    logf = open(os.path.join(scratch, "server_%s.log" % name), "w")
    env = dict(os.environ, GENCENTER_CONFIG=cfg_path, GENCENTER_DATA=data_dir)
    if path_value is not None:
        env["PATH"] = path_value
    proc = subprocess.Popen([sys.executable, os.path.join(REPO, "server.py")], cwd=REPO, env=env,
                             stdout=logf, stderr=subprocess.STDOUT)
    PROCS.append(proc)
    url = "http://127.0.0.1:%d/" % port

    def lane_up():
        d = http_json(url + "api/lanes")
        lanes = d.get("lanes") if isinstance(d, dict) else d
        l = next((x for x in (lanes or []) if x.get("id") == LANE_ID), None)
        return bool(l and l.get("up") and l.get("discovered"))  # discovered: generate before discovery is refused (503)

    ok = wait_true("server %s is up and the fake lane is up" % name, lane_up, 30)
    if not ok:
        with open(os.path.join(scratch, "server_%s.log" % name)) as f:
            print("  -- server %s log tail: %s" % (name, f.read()[-1200:]))
    return proc, url, data_dir


def http_post_json(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                  headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def explain(e, name):
    """An HTTPError's own body and URL, plus the server's log tail -- so an
    intermittent setup failure says what the server refused and why."""
    body = ""
    if hasattr(e, "read"):
        try:
            body = e.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
    tail = ""
    try:
        with open(os.path.join(SCRATCH, "server_%s.log" % name)) as f:
            tail = f.read()[-800:]
    except OSError:
        pass
    return "%r url=%s body=%s\n  -- server %s log tail:\n%s" % (e, getattr(e, "url", ""), body, name, tail)



import hashlib
import math
import re
import wave

sys.path.insert(0, REPO)
import engines

SHOT_PATH = os.environ.get("BAR_SNAP_UI_SHOT")


def multipart_post(url, fields, filename, data):
    boundary = "----bwfsnap%d" % int(time.time() * 1000)
    parts = []
    for k, v in fields.items():
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (boundary, k, v)).encode())
    parts.append(("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
                  "Content-Type: audio/wav\r\n\r\n" % (boundary, filename)).encode() + data + b"\r\n")
    parts.append(("--%s--\r\n" % boundary).encode())
    req = urllib.request.Request(url, data=b"".join(parts), method="POST",
                                  headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def wav_file(path, seconds, sr=8000):
    n = int(sr * seconds)
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(b"\x00\x00" * n)
    with open(path, "rb") as f:
        return f.read()


def song_map(data, seconds, downbeats, per_bar=4):
    beats = []
    for a, b in zip(downbeats, downbeats[1:]):
        beats += [a + (b - a) * k / per_bar for k in range(per_bar)]
    beats.append(downbeats[-1])
    return {"schema": "north-star/song_map/v1", "name": "x",
            "source": {"path": "/somewhere/else/song.wav", "sha256": hashlib.sha256(data).hexdigest(), "duration_s": seconds},
            "beats": beats, "downbeats": list(downbeats), "bars": [], "meter": per_bar,
            "tempo": {"median_bpm": 96.77}, "sections": [], "lyrics": {"lines": []}}


def sing_time(sec):
    """index.html singTime(): Math.round(sec*10)/10 then m:ss.s (round half up)."""
    t = math.floor(sec * 10 + 0.5) / 10
    m = int(t // 60)
    return "%d:%s%.1f" % (m, "0" if t - m * 60 < 10 else "", t - m * 60)


def ids_present(page, ids):
    return [i for i in ids if page.query_selector("#" + i) is not None]


SCRATCH = tempfile.mkdtemp(prefix="bwf-snapui-")
if not shutil.which("ffmpeg"):
    print("SKIP: ffmpeg not on PATH -- the server's cut and master import need it")
    shutil.rmtree(SCRATCH, ignore_errors=True)
    sys.exit(0)

SECONDS = 40.0
BAR_S = 2.4
DOWN = [round(i * BAR_S, 4) for i in range(int(SECONDS / BAR_S) + 1) if i * BAR_S < SECONDS - 0.01]
MASTER_1 = wav_file(os.path.join(SCRATCH, "m1.wav"), SECONDS)
MASTER_2 = wav_file(os.path.join(SCRATCH, "m2.wav"), SECONDS - 3.0)
NEW_IDS = ["findBeatsBtn", "beatsLine", "snapBox", "snapWarnings", "snapError", "slotBars", "barsMinus", "barsValue",
           "barsPlus", "barsReadout", "toEndBox", "followsToggle", "lengthHint"]
OVERRIDE = {"job": None, "reflow": None, "span": None}


def api(url, path, payload=None):
    if payload is None:
        return http_json(url + path)
    return http_post_json(url + path, payload)


def op(url, sid, name, **kw):
    rev = http_json(url + "api/sequence?id=" + sid)["rev"]
    return http_post_json(url + "api/sequence/op", dict(kw, id=sid, rev=rev, op=name))


def new_seq(url, title, led=True, shots=True):
    sid = api(url, "api/sequence", {"title": title, "mode": "sequence"})["id"]
    op(url, sid, "set_canvas", width=192, height=96)
    if led:
        op(url, sid, "set_audio_led", on=True)
        d = http_json(url + "api/sequence?id=" + sid)
        multipart_post(url + "api/sequence/master", {"id": sid, "rev": d["rev"]}, "master.wav", MASTER_1)
    if shots:
        h3 = {f["id"]: f["default"] for f in engines.fields("video", "fl2va") if f.get("default") is not None}
        h3.update(prompt="a shot", length=116, width=192, height=96, first_frame="start.png")
        for mode, values in (("ltx", {"prompt": "a shot", "length": 116}), ("fl2va", h3), ("ltx", {"prompt": "last", "length": 116})):
            op(url, sid, "add_slot", lane="video", cap="video", mode=mode, values=values)
    return sid


try:
    fake_proc, fake_port, fake_store = start_fake_lane(SCRATCH, "snap")
    srv_proc, url, data_dir = start_server(SCRATCH, fake_port, "snap", None)
    sid = new_seq(url, "snap ui")
    plain = new_seq(url, "classic", led=False)
    vids = [s["id"] for s in http_json(url + "api/sequence?id=" + sid)["slots"]]
    V1, V2, V3 = vids

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        errors, posts, ops = [], [], []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("request", lambda r: ops.append(json.loads(r.post_data))
                if r.method == "POST" and r.url.endswith("/api/sequence/op") and r.post_data else None)

        def patch(route):
            resp = route.fetch()
            try:
                data = resp.json()
            except Exception:
                route.fulfill(response=resp)
                return
            if isinstance(data, dict) and "slots" in data:
                if OVERRIDE["job"] is not None:
                    data["song_map_job"] = OVERRIDE["job"]
                if OVERRIDE["span"] is not None:
                    for sl in data["slots"]:
                        if sl["id"] == OVERRIDE["span"][0]:
                            sl["sing_span"] = OVERRIDE["span"][1]
                if OVERRIDE["reflow"] is not None and route.request.method == "POST":
                    data["reflow_stale_count"] = OVERRIDE["reflow"]
            route.fulfill(status=resp.status, headers={"content-type": "application/json"}, body=json.dumps(data))
        page.route(re.compile(r"/api/sequence(\?[^/]*|/op)$"), patch)

        def settle(ms=600):
            page.wait_for_timeout(ms)

        def until(js, timeout=9000):
            try:
                page.wait_for_function(js, timeout=timeout)
                return True
            except Exception:
                return False

        def text(sel):
            return page.eval_on_selector(sel, "el => el.innerText").strip()

        def stored_slot(slot_id):
            return next(s for s in http_json(url + "api/sequence?id=" + sid)["slots"] if s["id"] == slot_id)

        page.goto(url + "#room=cutting&seq=%s" % sid, wait_until="load", timeout=30000)
        settle(1500)

        # ---- 1. Find the beats: none -------------------------------------------------------------------------------
        check("audio-led on: the Find the beats row is in the cut bar", page.query_selector("#findBeatsBtn") is not None)
        check("no map yet: the button reads 'Find the beats' and is enabled",
              text("#findBeatsBtn") == "Find the beats" and not page.is_disabled("#findBeatsBtn"), text("#findBeatsBtn"))
        check("no map yet: there is no beats line", page.query_selector("#beatsLine") is None)
        check("no map yet: Snap to beats is disabled and unticked",
              page.is_disabled("#snapBox") and not page.is_checked("#snapBox"))
        page.click('#tlTrackVideo [data-slot-id="%s"]' % V1)
        settle(900)
        check("not snapped: no Bars block, no follows toggle; the classic lip-sync toggle is still there",
              not ids_present(page, ["barsMinus", "barsValue", "barsPlus", "barsReadout", "toEndBox", "followsToggle", "lengthHint"])
              and page.query_selector("#lipsyncToggle") is not None)
        check("not snapped: the length field is enabled", not page.is_disabled('[data-field-id="length"]'))

        # the real server's own refusal (no process lane that can find beats exists in this harness)
        try:
            api(url, "api/sequence/analyze", {"id": sid})
            server_says = None
        except urllib.error.HTTPError as e:
            server_says = json.loads(e.read()).get("error")
        page.click("#findBeatsBtn")
        settle(1200)
        check("a refused Find the beats shows the server's sentence", bool(server_says) and text("#seqMsg") == server_says,
              (text("#seqMsg"), server_says))
        check("and the button is usable again", not page.is_disabled("#findBeatsBtn"))

        # ---- 2. job states (stubbed GET) -----------------------------------------------------------------------------
        OVERRIDE["job"] = {"job_id": "gridfake1", "status": "running", "lane": "cpu"}
        check("a running job: the button turns to 'Finding the beats…' and is disabled (the poll shows it)",
              until("() => { const b = document.querySelector('#findBeatsBtn'); return b && b.disabled && b.innerText.trim() === 'Finding the beats…'; }"))
        OVERRIDE["job"] = {"job_id": "gridfake1", "status": "error", "error": "beat_this is not installed on that lane"}
        check("an errored job: the server's sentence is a line and the button is enabled again",
              until("() => { const l = document.querySelector('#beatsLine'); return l && l.innerText.trim() === 'beat_this is not installed on that lane'; }")
              and not page.is_disabled("#findBeatsBtn") and text("#findBeatsBtn") == "Find the beats", text("#findBeatsBtn"))
        OVERRIDE["job"] = None

        # ---- 3. a fresh map ------------------------------------------------------------------------------------------
        got = op(url, sid, "set_song_map", song_map=song_map(MASTER_1, SECONDS, DOWN))
        check("(setup) the song map is stored and fresh", got.get("song_map") and not got.get("song_map_stale"))
        want_line = "%.1f BPM, %d bars, 4/4" % (got["song_map"]["bpm_median"], len(got["song_map"]["downbeats"]) - 1)
        check("a fresh map: one line '<bpm> BPM, <bars> bars, 4/4' (%s)" % want_line,
              until("() => { const l = document.querySelector('#beatsLine'); return l && l.innerText.trim() === %s; }" % json.dumps(want_line)),
              page.query_selector("#beatsLine") and text("#beatsLine"))
        check("and the button reads 'Find again'", text("#findBeatsBtn") == "Find again", text("#findBeatsBtn"))
        check("Snap to beats is enabled and unticked", not page.is_disabled("#snapBox") and not page.is_checked("#snapBox"))

        page.route("**/api/sequence/analyze", lambda route: (posts.append(route.request.post_data),
                   route.fulfill(status=200, headers={"content-type": "application/json"}, body='{"ok": true, "job_id": "gridfake2"}')))
        page.click("#findBeatsBtn")
        settle(1200)
        check("Find again POSTs /api/sequence/analyze with the sequence id",
              len(posts) == 1 and json.loads(posts[0]) == {"id": sid}, posts)
        check("and, with no job in the stored sequence, the button is back to enabled", not page.is_disabled("#findBeatsBtn"))
        page.unroute("**/api/sequence/analyze")

        # ---- 4. Snap to beats ----------------------------------------------------------------------------------------
        page.click("#snapBox")
        settle(1500)
        stored = http_json(url + "api/sequence?id=" + sid)
        check("ticking Snap to beats stores bar_snap", stored.get("bar_snap") is True, stored.get("bar_snap"))
        check("and the box stays ticked", page.is_checked("#snapBox"))
        check("snapped with room for every bar: no warnings line", page.query_selector("#snapWarnings") is None
              and not (stored.get("snap_report") or {}).get("warnings"), stored.get("snap_report"))

        # ---- 5. the shot inspector -----------------------------------------------------------------------------------
        page.click('#tlTrackVideo [data-slot-id="%s"]' % V1)
        settle(1000)
        check("snapped: a video shot shows the Bars block", page.query_selector("#barsValue") is not None and page.is_visible("#slotBars"))
        s1 = stored_slot(V1)
        check("the Bars number is the server's window.bars (%s)" % s1["window"]["bars"],
              text("#barsValue") == str(s1["window"]["bars"]), text("#barsValue"))
        w = s1["window"]
        want_read = "%s–%s · renders %d frames · keeps the %s" % (sing_time(w["start"]), sing_time(w["f1"] / 24),
                                                                  w["render_frames"], "end" if w["keep"] == "tail" else "start")
        check("the read-out is the derived window, exactly (%s)" % want_read, text("#barsReadout") == want_read, text("#barsReadout"))
        check("the length field is disabled, with the hint naming the frames",
              page.is_disabled('[data-field-id="length"]')
              and text("#lengthHint") == "set by the beat grid: %d frames" % w["render_frames"], text("#lengthHint"))
        check("a first shot: no 'Ends at song end' box", page.query_selector("#toEndBox") is None)

        n0 = w["bars"]
        ops.clear()
        page.click("#barsPlus")
        settle(1200)
        sent = [o for o in ops if o.get("op") == "set_shot_bars"]
        check("+ sends set_shot_bars {slot_id, bars: n+1}", len(sent) == 1 and sent[0].get("bars") == n0 + 1
              and sent[0].get("slot_id") == V1, sent)
        s1 = stored_slot(V1)
        check("the server now has slot.bars = n+1", s1.get("bars") == n0 + 1, s1.get("bars"))
        w = s1["window"]
        want_read = "%s–%s · renders %d frames · keeps the %s" % (sing_time(w["start"]), sing_time(w["f1"] / 24),
                                                                  w["render_frames"], "end" if w["keep"] == "tail" else "start")
        check("and the page shows the new number and the new read-out", text("#barsValue") == str(n0 + 1)
              and text("#barsReadout") == want_read, (text("#barsValue"), text("#barsReadout")))
        check("the hint follows the new window", text("#lengthHint") == "set by the beat grid: %d frames" % w["render_frames"])

        OVERRIDE["reflow"] = 2
        page.click("#barsMinus")
        settle(1200)
        check("a bar change that the server says staled 2 made shots says so",
              text("#seqMsg") == "2 made shots now need making again.", text("#seqMsg"))
        OVERRIDE["reflow"] = 1
        page.click("#barsPlus")
        settle(1200)
        check("and for 1, singular", text("#seqMsg") == "1 made shot now needs making again.", text("#seqMsg"))
        OVERRIDE["reflow"] = None

        while stored_slot(V1)["window"]["bars"] > 1:
            page.click("#barsMinus")
            settle(900)
        check("at 1 bar the minus button is disabled", page.is_disabled("#barsMinus") and text("#barsValue") == "1",
              text("#barsValue"))

        # end_at: set by hand
        op(url, sid, "set_shot_bars", slot_id=V1, end_at=5.0)
        check("a shot with end_at says 'Ends at 0:05.0 (set by hand)' (the poll shows it)",
              until("() => { const n = document.querySelector('#endAtNote'); return n && n.innerText.trim() === 'Ends at 0:05.0 (set by hand)'; }"))
        page.click("#barsPlus")
        settle(1200)
        s1 = stored_slot(V1)
        check("the stepper replaces end_at with bars", s1.get("bars") == 1 and "end_at" not in s1, {k: s1.get(k) for k in ("bars", "end_at")})
        check("and the note is gone", page.query_selector("#endAtNote") is None)

        # the last shot
        page.click('#tlTrackVideo [data-slot-id="%s"]' % V3)
        settle(1000)
        w3 = stored_slot(V3)["window"]
        check("the last shot has 'Ends at song end', ticked (it defaults to the end of the song)",
              page.query_selector("#toEndBox") is not None and page.is_checked("#toEndBox") and w3["edge"] == "to_end",
              w3.get("edge"))
        page.click('#tlTrackVideo [data-slot-id="%s"]' % V2)
        settle(900)
        check("a middle shot has no 'Ends at song end' box", page.query_selector("#toEndBox") is None)
        page.click('#tlTrackVideo [data-slot-id="%s"]' % V3)
        settle(900)
        ops.clear()
        page.uncheck("#toEndBox")
        settle(1200)
        sent = [o for o in ops if o.get("op") == "set_shot_bars"]
        s3 = stored_slot(V3)
        check("unticking sends bars (1 when none was set) and the server stores it",
              len(sent) == 1 and sent[0].get("bars") == 1 and s3.get("bars") == 1 and not s3.get("to_end"), (sent, s3.get("bars")))
        stored = http_json(url + "api/sequence?id=" + sid)
        warnings = (stored.get("snap_report") or {}).get("warnings") or []
        check("the server now warns that bars of the song come after the last shot", len(warnings) >= 1, stored.get("snap_report"))
        shown = page.eval_on_selector_all("#snapWarnings .hint", "els => els.map(e => e.innerText.trim())") if page.query_selector("#snapWarnings") else []
        check("every snap_report.warnings sentence is on the cut bar, word for word", shown == warnings and bool(shown), (shown, warnings))
        if SHOT_PATH:     # the cut bar (warning showing) over the shot inspector's snap block, as one picture
            page.locator("#cutBar").screenshot(path=SHOT_PATH + ".bar.png")
            page.locator("#slotSnap").screenshot(path=SHOT_PATH + ".snap.png")
            try:
                from PIL import Image
                top, bot = Image.open(SHOT_PATH + ".bar.png"), Image.open(SHOT_PATH + ".snap.png")
                img = Image.new("RGB", (max(top.width, bot.width), top.height + bot.height + 8), (0, 0, 0))
                img.paste(top, (0, 0)); img.paste(bot, (0, top.height + 8))
                img.save(SHOT_PATH)
            except ImportError:
                shutil.copy(SHOT_PATH + ".bar.png", SHOT_PATH)
        page.check("#toEndBox")
        settle(1200)
        s3 = stored_slot(V3)
        check("ticking it again sends to_end and the server stores it", s3.get("to_end") is True, s3)
        stored = http_json(url + "api/sequence?id=" + sid)
        check("and the warning goes away", page.query_selector("#snapWarnings") is None
              and not (stored.get("snap_report") or {}).get("warnings"))

        # ---- 6. Follows the song -------------------------------------------------------------------------------------
        page.click('#tlTrackVideo [data-slot-id="%s"]' % V1)
        settle(1000)
        check("snapped LTX shot: one 'Follows the song' toggle, ticked (lip-sync defaults on); the classic toggle is gone",
              page.is_checked("#followsToggle") and page.query_selector("#lipsyncToggle") is None
              and page.query_selector("#singToggle") is None)
        ops.clear()
        page.uncheck("#followsToggle")
        settle(1200)
        sent = [o for o in ops if o.get("op") == "set_lipsync"]
        check("unticking an LTX shot's toggle sends set_lipsync on:false", len(sent) == 1 and sent[0].get("on") is False
              and sent[0].get("slot_id") == V1, sent)
        check("the server stores lipsync false and the toggle stays unticked",
              stored_slot(V1).get("lipsync") is False and not page.is_checked("#followsToggle"))
        page.click('#tlTrackVideo [data-slot-id="%s"]' % V2)
        settle(1000)
        check("snapped H3 shot: one toggle, unticked (a shot never sings until it is told to)",
              page.query_selector("#followsToggle") is not None and not page.is_checked("#followsToggle")
              and page.query_selector("#singToggle") is None)
        ops.clear()
        page.check("#followsToggle")
        settle(1200)
        sent = [o for o in ops if o.get("op") == "set_sing"]
        check("ticking it sends set_sing on:true", len(sent) == 1 and sent[0].get("on") is True and sent[0].get("slot_id") == V2, sent)
        s2 = stored_slot(V2)
        check("the server says the shot follows the song", s2.get("follows_song") is True and (s2.get("sing") or {}).get("on") is True)
        check("and the toggle stays ticked", page.is_checked("#followsToggle"))
        check("the real GET has no Starts at row for this shot (its sing_span has no head)", page.query_selector("#singStart") is None)
        OVERRIDE["span"] = (V2, {"head": True, "start": 3.5, "heard_start": 3.5, "end": 8.0, "fps": 24})   # stubbed: the harness has no song slot
        check("'Starts at' is shown disabled with 'set by the beat grid', its value kept",
              until("() => { const e = document.querySelector('#singStart'); return e && e.disabled && e.value === '3.5'; }")
              and text("#singStartHint") == "set by the beat grid" and page.query_selector("#singLine") is None)
        OVERRIDE["span"] = None
        until("() => !document.querySelector('#singStart')")

        # ---- 7. a new master makes the map stale ---------------------------------------------------------------------
        d = http_json(url + "api/sequence?id=" + sid)
        multipart_post(url + "api/sequence/master", {"id": sid, "rev": d["rev"]}, "other.wav", MASTER_2)
        check("a new master: the line says 'stale: find again'",
              until("() => { const l = document.querySelector('#beatsLine'); return l && l.innerText.trim() === 'stale: find again'; }"),
              page.query_selector("#beatsLine") and text("#beatsLine"))
        check("Snap to beats is disabled while the map is stale", page.is_disabled("#snapBox"))
        check("the button is enabled ('Find again')", not page.is_disabled("#findBeatsBtn") and text("#findBeatsBtn") == "Find again")
        stored = http_json(url + "api/sequence?id=" + sid)
        check("the server's snap_error sentence is on the cut bar, verbatim",
              bool(stored.get("snap_error")) and text("#snapError") == stored["snap_error"], (stored.get("snap_error"), page.query_selector("#snapError")))

        # ---- 8. audio-led off: none of it ----------------------------------------------------------------------------
        page.click("#audioLedBox")
        settle(1500)
        check("audio-led off (bar_snap still stored): none of the new ids exist", not ids_present(page, NEW_IDS),
              ids_present(page, NEW_IDS))
        check("and the shot's length field is enabled again", not page.is_disabled('[data-field-id="length"]'))
        page.goto(url + "#room=cutting&seq=%s" % plain, wait_until="load", timeout=30000)
        page.reload(wait_until="load")
        settle(1500)
        page.click('#tlTrackVideo [data-slot-id]')
        settle(900)
        check("a classic sequence (never audio-led): none of the new ids exist", not ids_present(page, NEW_IDS), ids_present(page, NEW_IDS))
        check("no page JS errors", not errors, errors)
        browser.close()
except Exception as e:
    import traceback
    check("the beat-snap UI test ran without raising", False, traceback.format_exc() + "\n" + explain(e, "snap"))
finally:
    for p in PROCS:
        stop(p)
    shutil.rmtree(SCRATCH, ignore_errors=True)

print()
print("ALL PASS" if not FAILED else "FAILED: %d -- %s" % (len(FAILED), FAILED))
sys.exit(1 if FAILED else 0)
