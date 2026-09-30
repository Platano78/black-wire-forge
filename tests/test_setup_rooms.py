"""W2 "What do you want to make first?" over HTTP, against a real server.py in Setup mode and
tests/fixtures/fake_comfy.py serving EXACTLY the Picture room's files (--pools):

(a) GET /api/setup/rooms lists rooms.json's rooms in order, each with its modes, the files its roles
    need (from the packs' sources), nodes, licences and total_bytes;
(b) against the found ComfyUI: Picture installed, Pixel Art not (only its background-removal file
    is missing: one file, its exact size), Music not; with no ComfyUI, or one no probe found, every
    `installed` is null and nothing is contacted;
(c) total_bytes counts a file once per room: Picture and Pixel Art share the three picture roles,
    three rooms share the background-removal file, the Video room's modes share files;
(d) /object_info is read once per Setup session; a ComfyUI that trickles or floods /object_info
    is given up on within the bound (null, never a hang);
(e) once a config exists, the route 404s.

Run: python3 tests/test_setup_rooms.py
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import _setup_fixture as fx  # noqa: E402
from _setup_fixture import check  # noqa: E402
import engines  # noqa: E402

RIG = json.load(open(os.path.join(HERE, "golden", "pools_rig.json")))
STOP = threading.Event()


def main_source(role):
    lst = next(p["sources"][role] for p in engines.packs() if role in (p.get("sources") or {}))
    return next(s for s in lst if s["run_by_us"])


# The Picture room's three files, named as `hf download --local-dir` leaves them (MODELS.md's rows).
PICTURE_FILES = {"unet": "qwen_image_2.1_Q6_K.gguf", "clip": "text_encoders/qwen3vl_8b_int8_convrot.safetensors",
                 "vae": "vae/qwen_image_2.1_vae_bf16.safetensors"}


def comfy_with(files, store):
    """A fake ComfyUI whose pools hold exactly these files ({pool: name}). -> its port."""
    pools = {pool: {sorted(RIG[pool])[0]: [[name]]} for pool, name in files.items()}
    os.makedirs(store, exist_ok=True)
    path = os.path.join(store, "pools.json")
    json.dump(pools, open(path, "w"))
    port = fx.free_port()
    fx.PROCS.append(subprocess.Popen([sys.executable, os.path.join(HERE, "fixtures", "fake_comfy.py"), "--port",
                                      str(port), "--store", store, "--pools", path], cwd=os.path.dirname(HERE),
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    for _ in range(100):
        try:
            import urllib.request
            urllib.request.urlopen("http://127.0.0.1:%d/system_stats" % port, timeout=1)
            break
        except Exception:
            time.sleep(0.1)
    return port


class Stingy(BaseHTTPRequestHandler):
    """A ComfyUI whose /system_stats is fine but whose /object_info trickles (or, FLOOD, is 65 MiB)."""
    FLOOD = False

    def do_GET(self):
        if self.path == "/system_stats":
            body = json.dumps({"system": {"comfyui_version": "x"}, "devices": []}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        big = 65 * 1024 * 1024
        self.send_header("Content-Length", str(big + 2 if self.FLOOD else 100 * 1024 * 1024))
        self.end_headers()
        try:
            if self.FLOOD:
                self.wfile.write(b"{" + b" " * big + b"}")
                return
            self.wfile.write(b"{")
            while not STOP.wait(0.2):
                self.wfile.write(b" ")
                self.wfile.flush()
        except OSError:
            pass

    def log_message(self, *a):
        pass


def serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1]


def object_info_hits(store):
    try:
        return sum(1 for line in open(os.path.join(store, "requests.log"))
                   if json.loads(line)["path"].startswith("/object_info"))
    except OSError:
        return 0


def rooms(query=""):
    code, body = fx.http("GET", "/api/setup/rooms" + query)
    return code, body, {r["id"]: r for r in body.get("rooms", [])} if isinstance(body, dict) else {}


try:
    if not fx.port_free(fx.SETUP_PORT):
        check("port %d is free for the Setup server" % fx.SETUP_PORT, False, "something else is listening on it")
        fx.finish()
    picture_roles = ["qwen_unet", "qwen_clip", "qwen_vae"]
    store = os.path.join(fx.SCRATCH, "picture-lane")
    port = comfy_with(PICTURE_FILES, store)
    proc, cfg, logp = fx.boot("rooms")
    check("Setup server up", fx.wait_health(proc, want_setup=True) is not None, open(logp).read()[-400:])

    print("(a) the rooms, in rooms.json's order, with what each needs")
    code, body, by = rooms()
    check("GET /api/setup/rooms -> 200 ok", code == 200 and isinstance(body, dict) and body.get("ok") is True,
          (code, str(body)[:300]))
    if code != 200:
        fx.finish()
    order = [r["id"] for r in json.load(open(os.path.join(os.path.dirname(HERE), "rooms.json")))]
    check("rooms come in rooms.json's order", [r["id"] for r in body["rooms"]][:len(order)] == order,
          [r["id"] for r in body["rooms"]])
    pic = by["picture"]
    check("a room carries id, name, group, blurb, modes, roles, nodes, licences, total_bytes",
          {"id", "name", "group", "blurb", "modes", "roles", "nodes", "licences", "total_bytes"} <= set(pic)
          and pic["group"] == "PICTURE" and pic["blurb"], sorted(pic))
    check("modes carry mode + label", pic["modes"] and all(set(m) == {"mode", "label"} and m["label"]
                                                           for m in pic["modes"]), pic["modes"])
    check("the Picture room needs the three picture roles, each with its recommended file first",
          [r["role"] for r in pic["roles"]] == picture_roles
          and all(r["sources"][0] == main_source(r["role"]) for r in pic["roles"]), pic["roles"])
    check("licences carry name, url, shippable, summary",
          pic["licences"] and all(set(x) == {"name", "url", "shippable", "summary"} for x in pic["licences"]),
          pic["licences"])
    every = [x for r in body["rooms"] for x in r["licences"]]
    check("every licence the rooms API returns has a non-empty summary",
          every and all(isinstance(x["summary"], str) and x["summary"].strip() for x in every),
          [x for x in every if not (x.get("summary") or "").strip()])
    md = open(os.path.join(os.path.dirname(HERE), "docs", "MODELS.md"), encoding="utf-8").read()
    md_links = {u for line in md.splitlines() if line.startswith("Licence:")
                for u in re.findall(r"\]\((https?://[^)]+)\)", line)}
    api_links = {x["url"] for x in every if x["url"]}
    check("the rooms API carries exactly the licence links MODELS.md's Licence: lines give",
          api_links == md_links and len(md_links) >= 4, (sorted(api_links), sorted(md_links)))
    vid = by["video"]
    alt = next((r for r in vid["roles"] if r.get("any_of")), None)
    check("an any-of role lists the one we run first and the 'not run by us' one after it",
          alt is not None and alt["sources"][0]["run_by_us"] is True and alt["sources"][-1]["run_by_us"] is False,
          alt)
    check("custom-node packages come as name + url", vid["nodes"] and all(set(n) == {"name", "url"}
                                                                          for n in vid["nodes"]), vid["nodes"])
    three = by["3d"]
    check("the 3D room names the programs its process mode needs", three["programs"] == ["Blender", "ffmpeg"],
          three["programs"])

    print("(b) no ComfyUI -> installed is null, and nothing is contacted")
    check("no address: every installed is null", body.get("comfy") is False
          and all(r["installed"] is None for x in body["rooms"] for r in x["roles"])
          and all(x["installed"] is None for x in body["rooms"]))
    code, body2, _ = rooms("?host=127.0.0.1&port=%d" % port)
    check("an address no probe found is not read: still null", code == 200 and body2.get("comfy") is False
          and object_info_hits(store) == 0, (body2.get("comfy"), object_info_hits(store)))

    print("(b) against the ComfyUI step 1 found (exactly the Picture room's files)")
    code, found = fx.http("POST", "/api/setup/probe-comfy", {"host": "127.0.0.1", "port": port})
    check("step 1 finds the fake ComfyUI", code == 200 and found.get("ok"), found)
    code, body, by = rooms("?host=127.0.0.1&port=%d" % port)
    check("checked against it", body.get("comfy") is True, body.get("comfy"))
    check("Picture: installed, nothing to get", by["picture"]["installed"] is True
          and all(r["installed"] is True for r in by["picture"]["roles"])
          and by["picture"]["needs"] == {"files": 0, "bytes": 0}, by["picture"])
    pix = by["pixelart"]
    bg = main_source("birefnet_model")
    check("Pixel Art: not installed, only its background-removal file missing (1 file, its size)",
          pix["installed"] is False and [r["role"] for r in pix["roles"] if not r["installed"]] == ["birefnet_model"]
          and pix["needs"] == {"files": 1, "bytes": bg["size"]}, (pix["installed"], pix["needs"]))
    check("Music: not installed", by["music"]["installed"] is False, by["music"]["installed"])
    check("a room with no files to check (Cutting Room) stays null", by["cutting"]["installed"] is None
          and by["cutting"]["roles"] == [], by["cutting"])

    print("(c) total_bytes counts each file once")
    qwen = sum(main_source(r)["size"] for r in picture_roles)
    check("Picture = the three picture files (15,902,886,512 bytes)", by["picture"]["total_bytes"] == qwen == 15902886512,
          by["picture"]["total_bytes"])
    check("Pixel Art = those three + background removal, counted once (16,347,360,108)",
          pix["total_bytes"] == qwen + bg["size"] == 16347360108, pix["total_bytes"])
    shared = [rid for rid, r in by.items() if any(x["role"] == "birefnet_model" for x in r["roles"])]
    check("three rooms share the background-removal file", sorted(shared) == ["3d", "cleanup", "pixelart"], shared)
    check("Clean-up = background removal + upscaler (511,514,585)",
          by["cleanup"]["total_bytes"] == 444473596 + 67040989, by["cleanup"]["total_bytes"])
    video_main = {}
    for m in by["video"]["modes"]:
        cap = next(c for c in engines.caps() if m["mode"] in engines.modes_for(c))
        for e in engines.needs(cap, m["mode"])["roles"]:
            group = e if isinstance(e, list) else [e]
            s = next(s for r in group for s in engines.needs(cap, m["mode"])["sources"][r] if s["run_by_us"])
            video_main[(s["repo"], s["file"])] = s["size"]
    check("Video: five modes, each shared file once (%d files)" % len(video_main),
          by["video"]["total_bytes"] == sum(video_main.values()) and len(video_main) == 10
          and by["video"]["total_bytes"] == 14831573088 + 15372971786 + 1472223346 + 364866540 + 995778752
          + 2 * 11564180576 + 15687142551 + 5207808496 + 605254808, (by["video"]["total_bytes"], len(video_main)))

    print("(d) /object_info read once; a bad one is given up on within the bound")
    rooms("?host=127.0.0.1&port=%d" % port)
    check("two rooms reads, one /object_info fetch", object_info_hits(store) == 1, object_info_hits(store))
    for label, handler, limit in (("trickling", Stingy, 25), ("65 MiB", type("Flood", (Stingy,), {"FLOOD": True}), 15)):
        bad = serve(handler)
        code, f = fx.http("POST", "/api/setup/probe-comfy", {"host": "127.0.0.1", "port": bad})
        t0 = time.monotonic()
        try:
            code, b, _ = rooms("?host=127.0.0.1&port=%d" % bad)
        except Exception as e:
            code, b = None, repr(e)
        secs = time.monotonic() - t0
        check("a %s /object_info: plain null answer within %d s" % (label, limit),
              f.get("ok") and code == 200 and b.get("comfy") is False and secs < limit, (round(secs, 1), code))
    STOP.set()
    fx.stop(proc)

    print("(e) once configured, the route is gone")
    text = json.dumps({"lanes": [{"id": "c", "name": "C", "host": "127.0.0.1", "port": port}]})
    proc2, cfg2, log2 = fx.boot("rooms-configured", text)
    check("configured server up", fx.wait_health(proc2, want_setup=False) is not None, open(log2).read()[-400:])
    code, _ = fx.http("GET", "/api/setup/rooms?host=127.0.0.1&port=%d" % port)
    check("GET /api/setup/rooms -> 404 once a config exists (it answered 200 in Setup mode above)", code == 404, code)
    fx.stop(proc2)
finally:
    STOP.set()
    fx.finish()
