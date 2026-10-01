"""Five rough spots the owner found in release gate 7 (2026-09-30), each proven here.

1  Setup step 2: a role covered by a DIFFERENT file than the recommended one must not read
   "<recommended file> . already installed"; it says the role is "covered by <file found>".
2  The room's "No <cap> machine is reachable right now" note goes away by itself when the lane
   comes up (the page opened while the lane's first check was still running), no reload.
3  Setup step 1: ticking "ComfyUI runs on this computer" while the ComfyUI address is another
   machine shows a hint (never blocks); a loopback / this-computer address shows none.
4  Pixel Art Make with an empty (or blank) prompt is refused like the Picture room refuses one
   ("Tell it what you want first."), with or without a shape image.
5  Setup never says "We run the one above." (hardware-agnostic): it says "We tested with ...".

Real server.py, real fake ComfyUI, real browser (Playwright). SKIPs without Playwright/Chromium.
Needs port 3998 free (the Setup server).

Run: python3 tests/test_gate7_rough.py
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import _ui_fixture  # noqa: E402,F401  (SKIPs cleanly without Playwright/Chromium)
import _setup_fixture as fx  # noqa: E402
from _setup_fixture import check  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

REPO = os.path.dirname(HERE)
RIG = json.load(open(os.path.join(HERE, "golden", "pools_rig.json")))
RECOMMENDED_UNET = "qwen_image_2.1_Q6_K.gguf"
OTHER_UNET = "qwen_image_2.1_nvfp4.safetensors"     # covers the same role, is not the recommended file
PICTURE_FILES = {"unet": OTHER_UNET, "clip": "text_encoders/qwen3vl_8b_int8_convrot.safetensors",
                 "vae": "vae/qwen_image_2.1_vae_bf16.safetensors"}


def jget(url, data=None, timeout=10):
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
        time.sleep(0.15)
    return None


def start_comfy(port, store, pools_file=None):
    os.makedirs(store, exist_ok=True)
    cmd = [sys.executable, os.path.join(HERE, "fixtures", "fake_comfy.py"), "--port", str(port), "--store", store]
    if pools_file:
        cmd += ["--pools", pools_file]
    fx.PROCS.append(subprocess.Popen(cmd, cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    wait(lambda: jget("http://127.0.0.1:%d/system_stats" % port)[0] == 200, 20)


def start_app(name, lanes, extra_timing=None):
    """server.py on a scratch config with these lanes. -> base url ending in '/'."""
    d = os.path.join(fx.SCRATCH, name)
    os.makedirs(os.path.join(d, "data"), exist_ok=True)
    port = fx.free_port()
    cfg = os.path.join(d, "config.json")
    timing = {"poll_seconds": 0.4, "job_poll_seconds": 0.6, "http_timeout": 4.0, "discover_seconds": 300.0}
    timing.update(extra_timing or {})
    json.dump({"title": name, "port": port, "bind": "127.0.0.1", "lanes": lanes, "timing": timing}, open(cfg, "w"))
    env = dict(os.environ, GENCENTER_CONFIG=cfg, GENCENTER_DATA=os.path.join(d, "data"))
    fx.PROCS.append(subprocess.Popen([sys.executable, os.path.join(REPO, "server.py")], cwd=REPO, env=env,
                                     stdout=open(os.path.join(d, "server.log"), "w"), stderr=subprocess.STDOUT))
    url = "http://127.0.0.1:%d/" % port
    wait(lambda: jget(url + "api/health")[0] == 200, 30)
    return url


def walk_to_step1_found(page, lane_port):
    page.goto(fx.BASE + "/")
    page.get_by_role("button", name="Yes, find it").click()
    page.wait_for_function("!/^Looking/.test(document.getElementById('c-status').textContent)", timeout=20000)
    page.get_by_label("Address", exact=True).fill("127.0.0.1")
    page.get_by_label("Port", exact=True).fill(str(lane_port))
    page.get_by_role("button", name="Check this address").click()
    page.wait_for_function("document.getElementById('c-gpu').textContent === 'fake gpu'", timeout=10000)


def setup_section():
    """Rough 1, 3, 5: a Setup server (port 3998) against a ComfyUI whose unet is not the recommended file."""
    pools = {pool: {sorted(RIG[pool])[0]: [[name]]} for pool, name in PICTURE_FILES.items()}
    store = os.path.join(fx.SCRATCH, "picture-lane")
    os.makedirs(store, exist_ok=True)
    pools_file = os.path.join(store, "pools.json")
    json.dump(pools, open(pools_file, "w"))
    comfy_port = fx.free_port()
    start_comfy(comfy_port, store, pools_file)
    proc, cfg, logp = fx.boot("rough")
    check("Setup server up", fx.wait_health(proc, want_setup=True) is not None, open(logp).read()[-400:])

    print("rough 1 (server): a role covered by another file says which file")
    code, found = fx.http("POST", "/api/setup/probe-comfy", {"host": "127.0.0.1", "port": comfy_port})
    code, body = fx.http("GET", "/api/setup/rooms?host=127.0.0.1&port=%d" % comfy_port)
    roles = {r["role"]: r for r in next(x for x in body["rooms"] if x["id"] == "picture")["roles"]}
    unet, clip = roles["qwen_unet"], roles["qwen_clip"]
    check("the Picture unet role is installed (by the nvfp4 file)", unet.get("installed") is True, unet.get("installed"))
    check("the role says which file covers it", unet.get("found") == OTHER_UNET, unet.get("found"))
    check("the role's recommended file is still the GGUF", unet["sources"][0]["file"] == RECOMMENDED_UNET,
          unet["sources"][0]["file"])
    check("a role covered by the recommended file names that file",
          clip.get("found") == PICTURE_FILES["clip"].split("/")[-1], clip.get("found"))

    print("rough 3 (server): the lane address is classed as this computer or not")
    check("the probe of 127.0.0.1 says this computer", (found.get("found") or {}).get("this_computer") is True, found)
    os.environ["GENCENTER_CONFIG"] = os.path.join(fx.SCRATCH, "no-such-config.json")
    os.environ["GENCENTER_DATA"] = os.path.join(fx.SCRATCH, "unit-data")
    import server  # noqa: E402  (no config: Setup mode, nothing starts on import)
    here = getattr(server, "host_is_this_computer", None)
    gname = socket.gethostname()
    check("server has host_is_this_computer()", here is not None)
    if here:
        for h in ("localhost", "127.0.0.1", "127.0.0.5", "::1", "[::1]", gname, gname.upper(), gname + ".local"):
            check("this computer: %s" % h, here(h) is True)
        check("this computer: the local address the page arrived on", here("192.0.2.77", "192.0.2.77") is True)
        for h in ("192.0.2.10", "studio-pc", "studio-pc.local", "10.0.0.5"):
            check("another machine: %s" % h, here(h) is False)
        # this computer's own LAN address, typed in while the page was reached on 127.0.0.1
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as u:
                u.connect(("192.0.2.1", 9))          # UDP connect sends nothing; it only picks the route's address
                lan = u.getsockname()[0]
        except OSError:
            lan = None
        if lan and not lan.startswith("127."):
            check("this computer: its own LAN address %s, typed in" % lan, here(lan, "127.0.0.1") is True)
        else:
            print("  SKIP  this computer's own LAN address (no route to find one)")

    print("rough 1, 3, 5 (browser): Setup at 1280 wide")
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        walk_to_step1_found(page, comfy_port)
        ticked = page.get_by_label("ComfyUI runs on this computer")
        hint = page.locator("#m-hint")
        check("the hint is not there before the box is ticked", not hint.is_visible())
        ticked.check()
        check("a loopback ComfyUI address: no hint", not hint.is_visible())
        ticked.uncheck()
        # another machine: the probe's answer is replaced (nothing else listens elsewhere)
        other = {"ok": True, "found": {"host": "studio-pc", "port": 8188, "gpu": "fake gpu", "vram_gb": 16,
                                       "version": "fake", "name": "Studio", "this_computer": False}}
        page.route("**/api/setup/probe-comfy", lambda r: r.fulfill(status=200, content_type="application/json",
                                                                   body=json.dumps(other)))
        page.get_by_label("Address", exact=True).fill("studio-pc")
        page.get_by_role("button", name="Check this address").click()
        page.wait_for_function("document.getElementById('c-where').textContent === 'studio-pc:8188'", timeout=10000)
        check("ticked off: no hint yet", not hint.is_visible())
        ticked.check()
        check("another machine + ticked: the hint shows", hint.is_visible())
        t = hint.text_content() if hint.count() else ""
        check("the hint names the host and says what happens to the files",
              "another machine (studio-pc)" in t and "go to this computer's folder" in t
              and "only sees them if the folder is shared" in t, t)
        check("the hint does not block Next", page.locator("#next").is_enabled())
        ticked.uncheck()
        check("unticked: the hint goes away", not hint.is_visible())
        page.unroute("**/api/setup/probe-comfy")
        page.get_by_label("Address", exact=True).fill("127.0.0.1")
        page.get_by_role("button", name="Check this address").click()
        page.wait_for_function("document.getElementById('c-where').textContent === '127.0.0.1:%d'" % comfy_port,
                               timeout=10000)
        ticked.check()
        check("back on the real address: no hint", not hint.is_visible())
        ticked.uncheck()
        def second_tested_file(route):      # the packs today have one role (the H3 speed pack) with two tested files
            j = route.fetch().json()
            for room in j["rooms"]:
                for role in room["roles"]:
                    if role["role"] == "qwen_unet":
                        role["sources"].append(dict(role["sources"][0], file="qwen_image_2.1_other.gguf"))
            route.fulfill(status=200, content_type="application/json", body=json.dumps(j))
        page.route("**/api/setup/rooms*", second_tested_file)
        page.get_by_role("button", name="Next").click()
        page.wait_for_function("/Checked against your ComfyUI/.test(document.getElementById('r-status').textContent)",
                               timeout=30000)
        card = page.locator("details.room[data-room=picture]")
        card.locator(":scope > summary").click()
        wraps = card.locator(":scope > .body > .file")
        rows = [wraps.nth(i).locator(":scope > .file").first.inner_text() for i in range(wraps.count())]
        unet_row = next((r for r in rows if RECOMMENDED_UNET in r), "")
        clip_row = next((r for r in rows if "qwen3vl_8b_int8_convrot" in r), "")
        check("rough 1: the unet row does not claim the GGUF is in ComfyUI",
              unet_row and "already installed" not in unet_row and "already in your ComfyUI" not in unet_row, unet_row)
        check("rough 1: ...it says the role is covered by the file found",
              "covered by " + OTHER_UNET in unet_row, unet_row)
        check("rough 1: a row whose recommended file IS there still says already installed",
              "already in your ComfyUI" in clip_row and "covered by" not in clip_row, clip_row)
        page.evaluate("document.querySelectorAll('details').forEach(d => d.open = true)")
        alts = page.locator("details.alt p.muted").all_text_contents()
        check("rough 5: alternatives that are tested by us read 'We tested with the one above.'",
              any("We tested with the one above." in a for a in alts), alts[:4])
        check("rough 5: nothing in Setup says 'we run'", "we run" not in page.inner_text("body").lower()
              and "we run" not in open(os.path.join(REPO, "setup.html"), encoding="utf-8").read().lower())
        check("no page errors in Setup", not errors, errors)
        browser.close()
    fx.stop(proc)


def pixel_section():
    """Rough 4: a real app with a fake lane that can make Pixel Art."""
    print("rough 4: an empty Pixel Art prompt is refused")
    lane_port = fx.free_port()
    start_comfy(lane_port, os.path.join(fx.SCRATCH, "pixel-lane"))
    app = start_app("pixel", [{"id": "t", "name": "Fake lane", "host": "127.0.0.1", "port": lane_port,
                               "caps": ["image"]}])
    check("(setup) the lane is up and discovered", wait(lambda: any(
        l["up"] and l["discovered"] for l in jget(app + "api/lanes")[1]["lanes"]), 40))
    eng = jget(app + "api/engines?lane=t")[1]
    pix = next(m for m in eng["image"]["modes"] if m["id"] == "pixelart")
    check("(setup) the lane can make pixelart", pix["available"], pix.get("missing"))

    defaults = {f["id"]: f["default"] for f in pix["fields"] if "default" in f}   # what the page sends with Make

    def gen(mode, prompt, **more):
        body = dict(defaults, lane="t", kind="image", mode=mode, prompt=prompt)
        return jget(app + "api/generate", dict(body, **more))
    sentence = "Tell it what you want first."
    jobs_before = len(jget(app + "api/jobs?limit=60")[1]["jobs"])
    picture = gen("t2i", "   ")
    check("(reference) the Picture room refuses a blank prompt", picture[0] == 400
          and picture[1]["error"] == sentence, picture)
    for label, prompt in (("empty", ""), ("blank", "  \n ")):
        code, r = gen("pixelart", prompt)
        check("Pixel Art, %s prompt: 400 with the Picture room's sentence" % label,
              code == 400 and r.get("ok") is False and r.get("error") == sentence, (code, r))
    code, r = gen("pixelart", "", shape_image="some_shape.png")
    check("Pixel Art, empty prompt with a shape image: refused too (the prompt says what fills the shape)",
          code == 400 and r.get("error") == sentence, (code, r))
    code, r = gen("pixelart", "", pixel_palette="#1a1c2c, #5d275d, #b13e53")
    check("Pixel Art, empty prompt with a palette: refused", code == 400 and r.get("error") == sentence, (code, r))
    check("nothing was queued by the refusals", len(jget(app + "api/jobs?limit=60")[1]["jobs"]) == jobs_before)
    code, r = gen("pixelart", "a small knight in blue armor")
    check("Pixel Art with a prompt is not refused for the prompt", "Tell it what" not in str(r.get("error", "")),
          (code, r))
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(app + "#room=pixelart", wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(800)
        page.locator("#inspector [data-field-id=prompt]").fill("")
        page.click("#makeBtn")
        ok = wait(lambda: sentence in (page.text_content("#inspectorMsg") or ""), 8)
        check("the Pixel Art room shows the sentence when Make is pressed with nothing written", ok,
              page.text_content("#inspectorMsg"))
        browser.close()


def stale_note_section():
    """Rough 2: the page opens while the lane's first check is still running."""
    print("rough 2: the 'No audio machine is reachable' note clears when the lane comes up")
    lane_port = fx.free_port()
    hang = socket.socket()                    # takes the connection, never answers: the first check is "running"
    hang.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    hang.bind(("127.0.0.1", lane_port))
    hang.listen(8)
    app = start_app("stale", [{"id": "snd", "name": "Sound lane", "host": "127.0.0.1", "port": lane_port,
                               "caps": ["audio"]}], {"http_timeout": 30.0})
    lane = jget(app + "api/lanes")[1]["lanes"][0]
    check("(setup) the lane has not finished its first check", lane["up"] is False and not lane.get("checked"), lane)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(app + "#room=music", wait_until="networkidle", timeout=30000)
        page.evaluate("window.__noReload = 1")
        note = page.locator("#roomPlainNote")
        check("the page opened while checking shows the no-machine note",
              note.is_visible() and "No audio machine is reachable right now" in note.text_content(),
              note.text_content())
        hang.close()
        start_comfy(lane_port, os.path.join(fx.SCRATCH, "stale-lane"))
        check("(setup) the server now reports the lane up and discovered", wait(
            lambda: (lambda x: x["up"] and x["discovered"])(jget(app + "api/lanes")[1]["lanes"][0]), 30))
        t0 = time.time()
        gone = wait(lambda: not note.is_visible(), 6)    # the page polls every 3 s: one poll, with margin
        check("the note is gone within one poll of the lane coming up, without a reload", gone,
              "still: %r after %.1fs" % (note.text_content(), time.time() - t0))
        check("(same page, never reloaded)", page.evaluate("window.__noReload") == 1)
        check("the room's own form is usable once the lane is up", page.locator("#roomForm").is_visible())
        check("no page errors in the room", not errors, errors)
        browser.close()


try:
    if not fx.port_free(fx.SETUP_PORT):
        check("port %d is free for the Setup server" % fx.SETUP_PORT, False, "something else is listening on it")
        fx.finish()
    for section in (setup_section, pixel_section, stale_note_section):
        try:
            section()
        except Exception as e:          # one section's crash must not hide the others' results
            import traceback
            traceback.print_exc()
            check("%s ran to the end" % section.__name__, False, repr(e))
finally:
    fx.finish()
