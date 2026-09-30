"""WB: the Workflows tab's server side, against tests/fixtures/fake_comfy.py --workflows
(small fixtures shaped like a real ComfyUI 0.37.0's template index, template JSON, model
folders and userdata), with server.py as a subprocess. No browser.

(1) GET /api/workflows lists a lane's templates and saved workflows; an unknown or
    non-ComfyUI lane is a 400; a lane that is down answers with a sentence.
(2) GET /api/workflows/ready: ready / needs_models / needs_nodes / needs_version come out
    right; a subgraph's id and UI-only nodes are never "missing nodes"; at most 24 names;
    a name the lane did not list is a 400; /object_info is read once.
    REV-1: a model file named only in a node's widget values must be one of the lane's own
    /object_info dropdown options for that node class, or it is a missing model (directory
    null, the node class named); a ".pt"-ending string on a node with no dropdown is ignored.
(3) POST /api/workflows/open saves a template into the fake's userdata under its
    sanitised title, then " (2)"; a name not in the lane's listing is a 400; the fake
    records no other write.
(4) GET /api/workflows/thumb passes a webp (sent as application/octet-stream, as ComfyUI 0.37 does); a text/html answer, a 3 MiB image or a
    template without an image thumbnail is a 404.

Run: python3 tests/test_workflows_api.py
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
FIX = os.path.join(HERE, "fixtures", "workflows")
FAILED = []
PROCS = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond:
        print("        -> %s" % (detail,))
        FAILED.append(name)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw, code, ctype = r.read(), r.status, r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        raw, code, ctype = e.read(), e.code, e.headers.get("Content-Type", "")
    if ctype.startswith("application/json"):
        return code, json.loads(raw or b"{}"), ctype
    return code, raw, ctype


def lane_log():
    with open(os.path.join(STORE, "requests.log")) as f:
        return [json.loads(x) for x in f if x.strip()]


def ready(kind, names):
    return call("GET", "/api/workflows/ready?lane=wb&kind=%s&names=%s"
                % (kind, ",".join(urllib.parse.quote(n, safe="") for n in names)))


SCRATCH = tempfile.mkdtemp(prefix="bwf_wb_api_")
STORE = os.path.join(SCRATCH, "lane")
MINE = os.path.join(STORE, "userdata", "workflows")
os.makedirs(MINE)
with open(os.path.join(FIX, "templates", "wf_cloud_api.json")) as f:
    ok_graph = f.read()
with open(os.path.join(MINE, "My flow, v2.json"), "w") as f:
    f.write(ok_graph)
with open(os.path.join(MINE, "Broken.json"), "w") as f:
    json.dump({"nodes": [{"id": 1, "type": "WfNotInstalled"}, {"id": 2, "type": "SaveImage"}]}, f)
# REV-1: hand-saved workflows name their files only in widget values. PRESENT is one of the
# fake's own UNETLoader dropdown options (tests/golden/pools_rig.json), read, not typed.
with open(os.path.join(HERE, "golden", "pools_rig.json")) as f:
    PRESENT = json.load(f)["unet"]["UNETLoader.unet_name"][0][0]


def widget_flow(unet):
    return {"nodes": [{"id": 1, "type": "UNETLoader", "widgets_values": [unet, "default"]},
                      {"id": 2, "type": "CLIPTextEncode", "widgets_values": ["a portrait, film grain.pt"]},
                      {"id": 3, "type": "SaveImage", "widgets_values": ["ComfyUI"]}]}


with open(os.path.join(MINE, "Widget missing.json"), "w") as f:
    json.dump(widget_flow("wf_absent_unet.safetensors"), f)
with open(os.path.join(MINE, "Widget present.json"), "w") as f:
    json.dump(widget_flow(PRESENT), f)
# REV-2, each exactly as the rig's real "Endless" workflow has it: (a) frontend-only helper
# nodes that never appear in /object_info; (b) a VAELoader whose widget loads a remapped
# file the lane lists while properties.models still names the author's (absent) original;
# (c) a text widget holding a file-name PATTERN on a node class that has a dropdown.
with open(os.path.join(MINE, "Frontend helpers.json"), "w") as f:
    json.dump({"nodes": [{"id": i + 1, "type": t, "properties": {"Node name for S&R": t, "aux_id": "x/y"}}
                         for i, t in enumerate(["Fast Groups Bypasser (rgthree)", "Label (rgthree)", "GetNode",
                                                "SetNode", "easy bookmark", "SaveImage"])]}, f)
with open(os.path.join(MINE, "Remapped vae.json"), "w") as f:
    json.dump({"nodes": [{"id": 1, "type": "VAELoader", "properties": {"models": [
        {"name": "minimax_h3_video_vae_fp16.safetensors", "directory": "vae",
         "url": "https://example.com/minimax_h3_video_vae_fp16.safetensors"}]},
        "widgets_values": ["minimax_h3/vae/minimax_h3_video_vae_int8_convrot.safetensors"]}]}, f)
with open(os.path.join(MINE, "Stitcher pattern.json"), "w") as f:
    json.dump({"nodes": [{"id": 1, "type": "H3MotionContextClipStitcher -noEmbryo",
                          "widgets_values": ["clip_*.safetensors", "overlap"]}]}, f)
SEEDED = ["Broken.json", "Frontend helpers.json", "My flow, v2.json", "Remapped vae.json", "Stitcher pattern.json",
          "Widget missing.json", "Widget present.json"]

try:
    lane_port, port = free_port(), free_port()
    PROCS.append(subprocess.Popen(
        [sys.executable, os.path.join(HERE, "fixtures", "fake_comfy.py"), "--port", str(lane_port),
         "--store", STORE, "--workflows", FIX, "--comfy-version", "0.37.0"],
        cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    cfg = os.path.join(SCRATCH, "config.json")
    with open(cfg, "w") as f:
        json.dump({"port": port, "bind": "127.0.0.1", "timing": {"poll_seconds": 0.5},
                   "lanes": [{"id": "wb", "name": "Box A", "host": "127.0.0.1", "port": lane_port,
                              "caps": ["image"]},
                             {"id": "off", "name": "Box B", "host": "127.0.0.1", "port": free_port(),
                              "caps": ["image"]},
                             {"id": "cpu", "name": "This machine", "kind": "process", "caps": ["3d"]}]}, f)
    env = dict(os.environ, GENCENTER_CONFIG=cfg, GENCENTER_DATA=os.path.join(SCRATCH, "data"))
    PROCS.append(subprocess.Popen([sys.executable, os.path.join(REPO, "server.py")], cwd=REPO, env=env,
                                  stdout=open(os.path.join(SCRATCH, "server.log"), "w"),
                                  stderr=subprocess.STDOUT))
    BASE = "http://127.0.0.1:%d" % port
    for _ in range(200):
        try:
            _, lanes, _ = call("GET", "/api/lanes")
            st = {l["id"]: l for l in lanes["lanes"]}
            if st["wb"]["up"] and st["off"]["checked"]:
                break
        except Exception:
            pass
        time.sleep(0.1)
    else:
        raise SystemExit("server or fake lane did not come up")

    print("(1) the list")
    code, d, _ = call("GET", "/api/workflows?lane=wb")
    names = sorted(t["name"] for t in d.get("templates", []))
    check("200 with every template the lane lists", code == 200 and names == sorted(
        ["wf_ready_image", "wf_cloud_api", "wf_needs_model_video", "wf_needs_node_audio", "wf_xss", "wf_newer",
         "wf_thumb_html", "wf_thumb_big"]), (code, names))
    by = {t["name"]: t for t in d.get("templates", [])}
    check("the saved workflows, with size and modified",
          sorted(m["file"] for m in d.get("mine", [])) == SEEDED
          and all(m["size"] and m["modified"] for m in d["mine"]), d.get("mine"))
    check("the lane's version", d.get("version") == "0.37.0", d.get("version"))
    check("openSource:false is not local; the rest are",
          by["wf_cloud_api"]["local"] is False and by["wf_ready_image"]["local"] is True, by.get("wf_cloud_api"))
    check("media type from the category; an unknown one is other",
          by["wf_needs_model_video"]["type"] == "video" and by["wf_newer"]["type"] == "other"
          and by["wf_xss"]["type"] == "3d", [(n, t["type"]) for n, t in by.items()])
    check("fields the page needs",
          by["wf_ready_image"]["size"] == 2147483648 and by["wf_ready_image"]["tutorial"] == "https://example.com/tutorial"
          and by["wf_needs_node_audio"]["packs"] == ["comfyui-wf-fixture-pack"]
          and by["wf_ready_image"]["thumb"] is True and by["wf_needs_node_audio"]["thumb"] is False
          and by["wf_ready_image"]["category"] == "Image", by.get("wf_ready_image"))
    check("nodes_known is a bool", isinstance(d.get("nodes_known"), bool), d.get("nodes_known"))
    code, d, _ = call("GET", "/api/workflows?lane=nope")
    check("unknown lane -> 400 with a sentence", code == 400 and d.get("error"), (code, d))
    code, d, _ = call("GET", "/api/workflows?lane=cpu")
    check("a process lane -> 400", code == 400, (code, d))
    code, d, _ = call("GET", "/api/workflows?lane=off")
    check("a lane that is down -> a sentence naming it", code == 502 and d.get("ok") is False
          and d.get("error", "").startswith("Box B is not answering"), (code, d))

    print("(2) readiness")
    code, d, _ = ready("template", list(by))
    r = d.get("ready", {})
    check("200 for every name", code == 200 and sorted(r) == sorted(by), (code, d))
    check("the subgraph template is ready (its id, a MarkdownNote and a Reroute are not missing nodes)",
          r["wf_ready_image"]["state"] == "ready" and r["wf_ready_image"]["missing_nodes"] == [],
          r.get("wf_ready_image"))
    check("the missing model file -> needs_models, with folder, file and source",
          r["wf_needs_model_video"]["state"] == "needs_models" and r["wf_needs_model_video"]["missing_models"] == [
              {"directory": "diffusion_models", "name": "wf_missing_video.safetensors",
               "url": "https://example.com/wf_missing_video.safetensors"}], r.get("wf_needs_model_video"))
    check("the missing node class -> needs_nodes, with its pack ids",
          r["wf_needs_node_audio"]["state"] == "needs_nodes"
          and r["wf_needs_node_audio"]["missing_nodes"] == ["WfFixturePackNode"]
          and r["wf_needs_node_audio"]["packs"] == ["comfyui-wf-fixture-pack"], r.get("wf_needs_node_audio"))
    check("a newer minimum ComfyUI -> needs_version",
          r["wf_newer"]["state"] == "needs_version" and r["wf_newer"]["min_version"] == "99.0.0", r.get("wf_newer"))
    code, d, _ = ready("mine", ["My flow, v2.json", "Broken.json"])
    r = d.get("ready", {})
    check("saved workflows too (a comma in a file name survives)",
          code == 200 and r.get("My flow, v2.json", {}).get("state") == "ready"
          and r.get("Broken.json", {}).get("missing_nodes") == ["WfNotInstalled"], (code, d))
    code, d, _ = ready("mine", ["Widget missing.json", "Widget present.json"])
    r = d.get("ready", {})
    miss, have = r.get("Widget missing.json", {}), r.get("Widget present.json", {})
    check("REV-1: a widget file not in the lane's UNETLoader list -> needs_models naming it and the node",
          code == 200 and miss.get("state") == "needs_models" and miss.get("missing_models") == [
              {"directory": None, "name": "wf_absent_unet.safetensors", "node": "UNETLoader", "url": None}],
          (code, miss))
    check("REV-1: the same workflow with a file the list has -> ready", have.get("state") == "ready"
          and have.get("missing_models") == [], have)
    check("REV-1: a prompt ending in .pt on a node with no dropdown is never a missing model",
          not any(m["name"].endswith(".pt") for m in miss.get("missing_models", []) + have.get("missing_models", [])),
          (miss, have))
    code, d, _ = ready("mine", ["Frontend helpers.json", "Remapped vae.json", "Stitcher pattern.json",
                                "Widget missing.json"])
    r = d.get("ready", {})
    check("REV-2a: frontend-only helper nodes (rgthree, GetNode/SetNode, easy bookmark) are not missing nodes",
          code == 200 and r.get("Frontend helpers.json", {}).get("state") == "ready",
          r.get("Frontend helpers.json"))
    check("REV-2b: a node's widget (the file it loads) wins over a differently named properties.models hint",
          r.get("Remapped vae.json", {}).get("state") == "ready", r.get("Remapped vae.json"))
    check("REV-2c: a file-name pattern in a text widget is not a model file",
          r.get("Stitcher pattern.json", {}).get("state") == "ready", r.get("Stitcher pattern.json"))
    check("REV-2: REV-1's missing widget file still reads needs_models",
          r.get("Widget missing.json", {}).get("state") == "needs_models", r.get("Widget missing.json"))
    code, d, _ = ready("template", ["wf_xss"] * 25)
    check("25 names -> 400", code == 400 and "24" in d.get("error", ""), (code, d))
    code, d, _ = ready("template", ["wf_xss"] * 24)
    check("24 names -> 200", code == 200, (code, d))
    code, d, _ = ready("template", ["../../object_info"])
    check("a name the lane did not list -> 400", code == 400, (code, d))
    code, d, _ = ready("mine", ["wf_xss"])
    check("a template name asked about as a saved workflow -> 400", code == 400, (code, d))
    code, d, _ = call("GET", "/api/workflows/ready?lane=wb&kind=other&names=wf_xss")
    check("an unknown kind -> 400", code == 400, (code, d))
    n_info = sum(1 for e in lane_log() if e["path"] == "/object_info")
    check("/object_info was read once for all of that", n_info == 1, n_info)

    print("(3) open")
    before = len(lane_log())
    code, d, _ = call("POST", "/api/workflows/open", {"lane": "wb", "kind": "template", "name": "wf_ready_image"})
    first = "Ready Text to Image Base.json"
    check("saved under the sanitised title", code == 200 and d.get("ok") and d.get("saved") == first
          and d.get("url") == "http://127.0.0.1:%d/" % lane_port, (code, d))
    with open(os.path.join(FIX, "templates", "wf_ready_image.json"), "rb") as f:
        want = f.read()
    saved = os.path.join(MINE, first)
    check("the fake's userdata holds the template's own bytes",
          os.path.isfile(saved) and open(saved, "rb").read() == want, os.listdir(MINE))
    code, d, _ = call("POST", "/api/workflows/open", {"lane": "wb", "kind": "template", "name": "wf_ready_image"})
    check("a second open -> \" (2)\"", code == 200 and d.get("saved") == "Ready Text to Image Base (2).json",
          (code, d))
    code, d, _ = call("POST", "/api/workflows/open", {"lane": "wb", "kind": "template", "name": "../../evil"})
    check("a name not in the lane's listing -> 400", code == 400, (code, d))
    code, d, _ = call("POST", "/api/workflows/open", {"lane": "wb", "kind": "mine", "name": "../Broken.json"})
    check("a saved-workflow name not in the listing -> 400", code == 400, (code, d))
    code, d, _ = call("POST", "/api/workflows/open", {"lane": "wb", "kind": "mine", "name": "Broken.json"})
    check("a saved workflow just answers the address", code == 200 and d.get("ok")
          and d.get("url") == "http://127.0.0.1:%d/" % lane_port and "saved" not in d, (code, d))
    code, d, _ = call("POST", "/api/workflows/open", {"lane": "nope", "kind": "template", "name": "wf_xss"})
    check("an unknown lane -> 400", code == 400, (code, d))
    code, d, _ = call("POST", "/api/workflows/open", {"lane": "off", "kind": "template", "name": "wf_xss"})
    check("a lane that is down -> a sentence", code == 502 and d.get("error", "").startswith("Box B"), (code, d))
    writes = [e for e in lane_log()[before:] if e["method"] != "GET"]
    check("the fake recorded exactly those two writes, nothing else",
          [(e["path"], e.get("filename")) for e in writes] == [
              ("/api/userdata/workflows%2FReady%20Text%20to%20Image%20Base.json", first),
              ("/api/userdata/workflows%2FReady%20Text%20to%20Image%20Base%20%282%29.json",
               "Ready Text to Image Base (2).json")], writes)
    all_writes = [e for e in lane_log() if e["method"] != "GET"]
    check("and no other write in the whole run", all_writes == writes, all_writes)
    check("the userdata folder holds only the two seeded files plus the two copies",
          sorted(os.listdir(MINE)) == sorted(SEEDED + [first, "Ready Text to Image Base (2).json"]),
          os.listdir(MINE))
    code, d, _ = call("GET", "/api/workflows?lane=wb")
    check("the list shows the new copies straight away",
          first in [m["file"] for m in d.get("mine", [])], d.get("mine"))

    print("(4) thumbnails")
    code, body, ctype = call("GET", "/api/workflows/thumb?lane=wb&name=wf_ready_image")
    check("a webp passes, typed from its bytes (the lane says application/octet-stream, as ComfyUI does)", code == 200 and ctype == "image/webp" and body[:4] == b"RIFF", (code, ctype))
    code, body, ctype = call("GET", "/api/workflows/thumb?lane=wb&name=wf_thumb_html")
    check("a text/html answer -> 404", code == 404, (code, ctype))
    code, body, ctype = call("GET", "/api/workflows/thumb?lane=wb&name=wf_thumb_big")
    check("a 3 MiB image -> 404", code == 404, (code, ctype))
    code, body, ctype = call("GET", "/api/workflows/thumb?lane=wb&name=wf_needs_node_audio")
    check("a template with no image thumbnail -> 404 (never fetched)", code == 404, (code, ctype))
    code, body, ctype = call("GET", "/api/workflows/thumb?lane=wb&name=..%2F..%2Fsystem_stats")
    check("a name the lane did not list -> 404", code == 404, (code, ctype))
    fetched = [e["path"] for e in lane_log() if e["path"].startswith("/templates/") and e["path"].endswith(("webp", "mp3"))]
    check("only the listed image thumbnails were asked for", sorted(fetched) == sorted(
        ["/templates/wf_ready_image-1.webp", "/templates/wf_thumb_html-1.webp", "/templates/wf_thumb_big-1.webp"]),
        fetched)
finally:
    if sys.exc_info()[0] is not None:
        import traceback
        traceback.print_exc()
        FAILED.append("the suite crashed")
    for p in PROCS:
        p.terminate()
    print("\nFAILED: %d" % len(FAILED) + (" checks: " + ", ".join(FAILED) if FAILED else ""))
    sys.exit(1 if FAILED else 0)
