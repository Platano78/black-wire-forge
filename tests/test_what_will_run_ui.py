"""Browser gate for "What will run" (slice 1, read-only): on a ComfyUI mode the
panel sits under the prompt and the room's guide, before Recipe, and lists one row per part
by default -- the part's words, the exact file its loader will be given (the
same file /api/engines resolves), AUTO PICK, and "N others fit" one click away;
expanded, every other file that fits with its reason and every file left out on
purpose, struck through with the pack's reason. The licence stamp says
non-commercial for the picture model and names the video model's territory. A
process-lane mode shows no panel. At 390 px no row overflows and the page does
not scroll sideways.

BWF_WWR_SHOTS=<dir> also saves the screenshots (1440 and 390, picture and video).

Run: python3 tests/test_what_will_run_ui.py
"""
import json
import os
import subprocess
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _ui_fixture as ui  # noqa: E402
from _ui_fixture import check  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

SHOTS = os.environ.get("BWF_WWR_SHOTS")
TURBO = "qwen_image_2.1_turbo_int8.safetensors"
INT8 = "qwen_image_2.1_int8_convrot.safetensors"

# The live rig's pools, plus the int8 picture build, a bf16 one and a Turbo build.
pools = json.load(open(os.path.join(ui.HERE, "golden", "pools_rig.json")))
unet = pools["unet"]["UNETLoader.unet_name"]
unet[0] = unet[0] + [INT8, "qwen_image_2.1_bf16.safetensors", TURBO]
pools_path = os.path.join(ui.SCRATCH, "pools.json")
json.dump(pools, open(pools_path, "w"))
store = os.path.join(ui.SCRATCH, "lane")
os.makedirs(store, exist_ok=True)
lane_port, port = ui.free_port(), ui.free_port()
ui.PROCS.append(subprocess.Popen([sys.executable, os.path.join(ui.HERE, "fixtures", "fake_comfy.py"), "--port",
                                  str(lane_port), "--store", store, "--pools", pools_path], cwd=ui.REPO,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
ui.wait_up("http://127.0.0.1:%d/system_stats" % lane_port)
data = os.path.join(ui.SCRATCH, "data")
os.makedirs(data, exist_ok=True)
cfg = os.path.join(ui.SCRATCH, "config.json")
json.dump({"title": "wwr ui", "port": port, "bind": "127.0.0.1",
           "lanes": [{"id": "t", "name": "Fake lane", "host": "127.0.0.1", "port": lane_port,
                      "caps": ["image", "video", "audio"]},
                     {"id": "cpu", "name": "This machine", "kind": "process", "caps": ["3d"]}],
           "timing": {"poll_seconds": 0.5, "job_poll_seconds": 1.0}}, open(cfg, "w"))
# Stand-in programs for the process lane, so the 3D room's process mode is there whatever this
# machine has installed (nothing here ever runs them: the check only opens the room).
stub_bin = os.path.join(ui.SCRATCH, "bin")
os.makedirs(stub_bin, exist_ok=True)
for prog in ("blender", "ffmpeg"):
    with open(os.path.join(stub_bin, prog), "w") as f:
        f.write("#!/bin/sh\nexit 0\n")
    os.chmod(os.path.join(stub_bin, prog), 0o755)
ui.PROCS.append(subprocess.Popen([sys.executable, os.path.join(ui.REPO, "server.py")], cwd=ui.REPO,
                                 env=dict(os.environ, GENCENTER_CONFIG=cfg, GENCENTER_DATA=data,
                                          PATH=stub_bin + os.pathsep + os.environ.get("PATH", "")),
                                 stdout=open(os.path.join(ui.SCRATCH, "server.log"), "w"), stderr=subprocess.STDOUT))
URL = "http://127.0.0.1:%d/" % port
ui.wait_up(URL + "api/health")


def api(path):
    import urllib.request
    with urllib.request.urlopen(URL + path, timeout=10) as r:
        return json.loads(r.read().decode())


def wait_discovered():
    import time
    for _ in range(100):
        if any(l["id"] == "t" and l["discovered"] for l in api("api/lanes")["lanes"]):
            return
        time.sleep(0.2)


def shot(page, name):
    if SHOTS:
        os.makedirs(SHOTS, exist_ok=True)
        page.screenshot(path=os.path.join(SHOTS, name + ".png"), full_page=True)


OVERFLOW_JS = """() => {
  const box = document.querySelector('#whatWillRun');
  const wide = Array.from(box.querySelectorAll('*')).filter(e => e.scrollWidth > e.clientWidth + 1
      && getComputedStyle(e).overflowX !== 'visible' || e.getBoundingClientRect().right > box.getBoundingClientRect().right + 1);
  return {wide: wide.map(e => e.className + ':' + e.textContent.slice(0, 40)),
          page: document.documentElement.scrollWidth - window.innerWidth};
}"""


# The panel must not push a primary input down: every visible input the form shows above
# Recipe (the prompt, the guide, the mode's file inputs) comes before it, and Recipe is next.
PLACED_JS = """() => {
  const box = document.querySelector('#whatWillRun'), recipe = document.querySelector('#recipeDetails');
  const shown = e => e && e.offsetParent !== null;
  const inputs = Array.from(document.querySelectorAll('#roomForm input, #roomForm textarea, #roomForm select'))
    .filter(e => shown(e) && !box.contains(e)
            && (e.compareDocumentPosition(recipe) & Node.DOCUMENT_POSITION_FOLLOWING));
  const late = inputs.filter(e => box.compareDocumentPosition(e) & Node.DOCUMENT_POSITION_FOLLOWING);
  return {ok: !!inputs.length && !late.length && box.nextElementSibling === recipe,
          inputs: inputs.length, late: late.map(e => e.id || e.name || e.type)};
}"""


def open_room(page, room, mode=None):
    if mode:
        page.add_init_script("localStorage.setItem('bwf.room.engine.%s', JSON.stringify(%s))"
                             % (room, json.dumps(mode)))
    page.goto(URL + "#room=" + room, wait_until="networkidle", timeout=30000)
    page.wait_for_selector("#whatWillRun:not([hidden]) .wwr-part", timeout=15000)


try:
    wait_discovered()
    eng = api("api/engines?lane=t")
    t2i = next(m for m in eng["image"]["modes"] if m["id"] == "t2i")
    fl2va = next(m for m in eng["video"]["modes"] if m["id"] == "fl2va")
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for width in (1440, 390):
            page = browser.new_page(viewport={"width": width, "height": 1000})
            open_room(page, "picture")
            print("Picture at %d px" % width)
            rows = page.locator("#whatWillRun .wwr-part")
            check("%d: one row per part by default (%d)" % (width, len(t2i["parts"])),
                  rows.count() == len(t2i["parts"]) == 3, rows.count())
            files = page.locator("#whatWillRun .wwr-part > .wwr-fn").all_text_contents()
            check("%d: each row names the exact file /api/engines says will load" % width,
                  files == [p["file"] for p in t2i["parts"]] and files[0] == INT8, files)
            check("%d: the picture model row is tagged AUTO PICK and says how many others fit" % width,
                  "auto pick" in rows.nth(0).inner_text().lower() and "others fit" in rows.nth(0).inner_text(),
                  rows.nth(0).inner_text())
            placed = page.evaluate(PLACED_JS)
            check("%d: picture: the panel comes after every input above Recipe, directly before Recipe" % width,
                  placed["ok"], placed)
            check("%d: the stamp says non-commercial (structured field, not the name)" % width,
                  "non-commercial" in page.locator("#whatWillRun .wwr-foot").inner_text().lower(),
                  page.locator("#whatWillRun .wwr-foot").inner_text())
            shot(page, "picture-%d" % width)
            page.locator("#whatWillRun .wwr-other").first.click()
            page.wait_for_selector("#whatWillRun .wwr-cands")
            text = page.locator("#whatWillRun .wwr-cands").inner_text()
            check("%d: expanded, every other file that fits is listed with its reason" % width,
                  all(c["file"] in text for c in t2i["parts"][0]["candidates"]) and "Ranked 2nd" in text, text)
            refused = page.locator("#whatWillRun .wwr-cand.refused .wwr-fn")
            check("%d: the Turbo file is struck through with the term and the pack's reason" % width,
                  refused.all_text_contents() == [TURBO]
                  and page.evaluate("e => getComputedStyle(e).textDecorationLine", refused.first.element_handle())
                  == "line-through" and '"turbo"' in text and "background detail" in text, text)
            shot(page, "picture-%d-open" % width)
            o = page.evaluate(OVERFLOW_JS)
            check("%d: no row overflows the panel, no sideways scroll" % width, not o["wide"] and o["page"] <= 0, o)
            page.close()

            page = browser.new_page(viewport={"width": width, "height": 1000})
            open_room(page, "video", {"cap": "video", "mode": "fl2va"})
            print("Video (fl2va) at %d px" % width)
            placed = page.evaluate(PLACED_JS)
            check("%d: video: the panel comes after the prompt and the starting-picture input, directly before Recipe"
                  % width, placed["ok"] and placed["inputs"] >= 2, placed)
            files = page.locator("#whatWillRun .wwr-part > .wwr-fn").all_text_contents()
            check("%d: every part of the video mode, the text encoder the graph reads first included" % width,
                  files == [p["file"] for p in fl2va["parts"] if p["file"]] and len(files) == 5, files)
            check("%d: the speed pack row says which quality settings load it" % width,
                  "Loaded only at" in page.locator("#whatWillRun").inner_text())
            foot = page.locator("#whatWillRun .wwr-foot").inner_text()
            check("%d: the stamp names the territory" % width, "south korea" in foot.lower(), foot)
            shot(page, "video-%d" % width)
            o = page.evaluate(OVERFLOW_JS)
            check("%d: video panel: no row overflows, no sideways scroll" % width, not o["wide"] and o["page"] <= 0, o)
            page.close()

        # The 3D room's process mode, picked explicitly; its programs are the stubs above.
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.add_init_script("localStorage.setItem('bwf.room.engine.3d', JSON.stringify(%s))"
                             % json.dumps({"cap": "3d", "mode": "turntable"}))
        page.goto(URL + "#room=3d", wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(1500)
        info = page.evaluate("() => [STATE.cap, STATE.mode, (currentMode() || {}).lane_kind]")
        check("a process-lane mode shows no panel",
              info[2] == "process" and page.locator("#whatWillRun").is_hidden(), info)
        browser.close()
finally:
    ui.finish()
