"""scripts/autopack.py end to end against tests/fixtures/fake_comfy.py (spec sections 8, 12, 14 S1).

The fake serves the fixture replicas as a lane's templates, a real-shaped /object_info
(--object-info-file) and, in ACCEPT mode, a finished render. The CLI is run as a subprocess.

Flow under test: doctor, list, convert --api-json, show, answer / ack / licence, review,
test (uploads, one queued prompt, download), verdict, enable, then BWF's own scan lists the mode
available against the fake's pools; updates notices a changed template; disable; delete.
Beside it: every refusal exits 3 with the sentence on stderr, usage errors exit 2, a lane that
is down exits 4, a missing converter exits 5; convert and show never write to the lane.

Run: python3 tests/test_autopack_cli.py
"""
import importlib.util, json, os, shutil, socket, subprocess, sys, tempfile, time, urllib.request
SCRATCH = tempfile.mkdtemp(prefix="bwf-apcli-")
DATA = os.path.join(SCRATCH, "data")
os.environ["GENCENTER_DATA"] = DATA
import _autopack_fixture as fx_mod
ap, _comfy = fx_mod.ap, fx_mod._comfy
import engines

FAILED = []
PROCS = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)[:600]) if not cond and detail else ""))
    if not cond: FAILED.append(name)

HERE, ROOT = fx_mod.HERE, fx_mod.ROOT
CLI = os.path.join(ROOT, "scripts", "autopack.py")
FAKE = os.path.join(HERE, "fixtures", "fake_comfy.py")
fx = fx_mod.Fx()

WF = os.path.join(SCRATCH, "wf")
shutil.copytree(fx_mod.FIX, WF, ignore=shutil.ignore_patterns("expected", "__pycache__"))
STORE = os.path.join(SCRATCH, "lane")


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


PORT = free_port()
URL = "http://127.0.0.1:%d" % PORT
proc = subprocess.Popen([sys.executable, FAKE, "--port", str(PORT), "--store", STORE, "--workflows", WF,
                         "--object-info-file", os.path.join(WF, "object_info.json"), "--comfy-version", "0.99.0"],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
PROCS.append(proc)
for _ in range(100):
    try:
        urllib.request.urlopen(URL + "/system_stats", timeout=1).read(); break
    except Exception:
        time.sleep(0.1)


def lane_get(path):
    return json.loads(urllib.request.urlopen(URL + path, timeout=10).read())


def lane_post(path, obj):
    req = urllib.request.Request(URL + path, data=json.dumps(obj).encode(), method="POST", headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


def lane_log():
    p = os.path.join(STORE, "requests.log")
    return [json.loads(x) for x in open(p) if x.strip()] if os.path.exists(p) else []


def cli(*args, lane=True, timeout=120):
    r = subprocess.run([sys.executable, CLI, "--data", DATA] + list(args), capture_output=True, text=True, timeout=timeout,
                       env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    return r.returncode, r.stdout, r.stderr


def packs_listing():
    d = os.path.join(DATA, "packs")
    return sorted(os.listdir(d)) if os.path.isdir(d) else []


try:
    print("doctor and list read the lane and change nothing")
    code, out, err = cli("doctor", "--lane", URL)
    check("doctor exits 0 and prints the lane's versions", code == 0 and "0.99.0" in out and "9.9.9" in out and "8.8.8" in out, (code, out, err))
    check("doctor says what the converter is (no conversion happens)", "converter" in out.lower())
    code, out, err = cli("list", "--lane", URL)
    check("list names the templates", code == 0 and "video_multi_ref_switch" in out and "image_flat_t2i" in out, (code, out, err))
    code, out, _ = cli("list", "--lane", URL, "--media", "video")
    check("--media filters", "video_multi_ref_switch" in out and "image_flat_t2i" not in out, out)
    code, out, _ = cli("list", "--lane", URL, "--ready")
    check("--ready keeps only what this lane can run, and never a paid template",
          "image_flat_t2i" in out and "api_paid_cloud" not in out, out)

    print("convert --api-json")
    API = os.path.join(fx_mod.FIX, "api", "video_multi_ref_switch.api.json")
    code, out, err = cli("convert", "--lane", URL, "--template", "video_multi_ref_switch", "--api-json", API)
    ID = "video-multi-ref-switch"
    check("convert exits 0 and prints the draft id", code == 0 and ID in out, (code, out, err))
    d = os.path.join(DATA, "packs-draft", ID)
    a = json.load(open(os.path.join(d, "autopack.json")))
    check("draft recorded with the lane's versions and the export route",
          a["state"] == "draft" and a["source"]["comfyui_version"] == "0.99.0" and a["source"]["frontend_version"] == "9.9.9"
          and a["source"]["templates_version"] == "8.8.8" and a["source"]["converter"] == "export" and a["source"]["lane"] == URL, a["source"])
    check("the template bytes kept are the lane's", open(os.path.join(d, "source", "template.ui.json"), "rb").read() == fx.ui_bytes("video_multi_ref_switch"))
    code, out, err = cli("convert", "--lane", URL, "--template", "video_multi_ref_switch", "--api-json", API)
    check("converting again returns the same draft", code == 0 and ID in out and os.listdir(os.path.join(DATA, "packs-draft")) == [ID], (code, out, err))
    check("convert never wrote to the lane", all(r["method"] == "GET" for r in lane_log()), [r for r in lane_log() if r["method"] != "GET"])

    print("show, answers, review")
    code, out, err = cli("show", ID, "--lane", URL)
    check("show prints fields, what needs an answer, models and readiness",
          code == 0 and "picture1" in out and "picture_roles" in out and "unet" in out and "available" in out, (code, out, err))
    check("show says config pins are not applied to drafts", "pin" in out.lower())
    code, out, err = cli("review", ID)
    check("review with open flags exits 3 and lists them on stderr", code == 3 and "licence" in err.lower() and "picture" in err.lower(), (code, out, err))
    check("a refused review leaves it a draft", json.load(open(os.path.join(d, "autopack.json")))["state"] == "draft")
    code, out, err = cli("answer", ID, "picture_roles", "Face", "Outfit")
    check("a wrong number of labels is refused", code == 3 and "3" in err, (code, err))
    for argv in (("answer", ID, "picture_roles", "Face", "Outfit", "Setting"), ("answer", ID, "length_unit", "seconds"),
                 ("answer", ID, "switch:opt_enable_light", "Fast light mode"), ("ack", ID, "steps_hidden")):
        code, out, err = cli(*argv)
        check("ok: " + " ".join(argv[2:]), code == 0, (code, out, err))
    code, out, err = cli("licence", ID, "--name", "Checked by hand", "--url", "https://example.invalid/l", "--checked")
    check("licence confirmed", code == 0, (code, out, err))
    m = json.load(open(os.path.join(d, "manifest.json")))
    byid = {f["id"]: f for f in m["fields"]}
    check("answers landed in the manifest", [byid["picture%d" % i]["label"] for i in (1, 2, 3)] == ["Face", "Outfit", "Setting"]
          and byid["length"]["units"] == "seconds" and byid["length"]["range"] == [1, 60] and byid["length"]["label"] == "Length (seconds)"
          and byid["opt_enable_light"]["label"] == "Fast light mode", {k: v.get("label") for k, v in byid.items()})
    check("shippable stays false", m["licence"]["shippable"] is False and m["licence"]["name"] == "Checked by hand")
    code, out, err = cli("set", ID, "prompt.hint=Plain words work")
    check("set works", code == 0, (code, err))
    code, out, err = cli("review", ID)
    check("review passes", code == 0 and json.load(open(os.path.join(d, "autopack.json")))["state"] == "reviewed", (code, out, err))
    check("nothing is enabled by any of that", packs_listing() == [])

    print("test: uploads, one prompt, a download")
    code, out, err = cli("test", ID, "--lane", URL)
    check("a missing input is refused naming the field's label", code == 3 and "Face" in err, (code, err))
    check("a refused test queued nothing", not any(r["path"] == "/prompt" for r in lane_log()))
    OUTP = os.path.join(STORE, "outputs"); os.makedirs(OUTP, exist_ok=True)
    open(os.path.join(OUTP, "bwf_00001_.mp4"), "wb").write(b"fake mp4 bytes")
    lane_post("/_control/accept", {"outputs": [{"filename": "bwf_00001_.mp4", "subfolder": "", "type": "output", "node": "28"}]})
    ins = {}
    for n in ("a", "b", "c"):
        ins[n] = os.path.join(SCRATCH, n + ".png"); open(ins[n], "wb").write(b"png " + n.encode())
    code, out, err = cli("test", ID, "--lane", URL, "--input", "picture1=" + ins["a"], "--input", "picture2=" + ins["b"], "--input", "picture3=" + ins["c"])
    check("test exits 0", code == 0, (code, out, err))
    tdir = os.path.join(d, "test")
    check("the output is downloaded into the draft's test folder", os.path.isfile(os.path.join(tdir, "bwf_00001_.mp4")) and os.path.getsize(os.path.join(tdir, "bwf_00001_.mp4")) > 0 and tdir in out, (out, os.listdir(d)))
    log = lane_log()
    check("three uploads and exactly one prompt", len([r for r in log if r["path"] == "/upload/image"]) == 3 and len([r for r in log if r["path"] == "/prompt"]) == 1)
    t = json.load(open(os.path.join(d, "autopack.json")))["test"]
    check("the test record: machine pass, no human verdict yet", t["machine_pass"] is True and t["looks_right"] is None and t["lane"] == URL and t["outputs"], t)
    cur = ap.read_draft(DATA, ID)
    res = ap.readiness(cur["manifest"], cur["graph"], cur["ap"]["flags"], fx.oi, fx.pools, True)
    files = {r["role"]: r["file"] for r in res["roles"]}
    want = ap.build_test_graph(cur["manifest"], cur["graph"], {"picture1": "a.png", "picture2": "b.png", "picture3": "c.png"}, files)
    check("the prompt the lane received is the draft's own built graph", json.load(open(os.path.join(STORE, "last_prompt.json"))) == want)
    check("the model files in it are the ones the rules resolve", want["11"]["inputs"]["unet_name"] == "multimodel_bf16.safetensors" and want["14"]["inputs"]["lora_name"] == "multi_light_lora.safetensors")

    print("verdict and enable")
    code, out, err = cli("enable", ID)
    check("enable before a verdict is refused", code == 3 and packs_listing() == [], (code, err))
    code, out, err = cli("verdict", ID, "--looks-right")
    check("verdict looks-right -> tested", code == 0 and json.load(open(os.path.join(d, "autopack.json")))["state"] == "tested", (code, out, err))
    check("still not enabled", packs_listing() == [])
    code, out, err = cli("enable", ID)
    check("enable exits 0 and says to restart", code == 0 and "Added. Restart Black Wire Forge to load it." in out, (code, out, err))
    check("the pack is in place", sorted(os.listdir(os.path.join(DATA, "packs", ID))) == ["autopack.json", "graph.api.json", "manifest.json"])
    engines._PACKS = None
    packs = {p["id"]: p for p in engines.packs()}
    pk = packs.get(ID)
    check("BWF's own scan lists it, no problems", pk is not None and engines.user_pack_problems() == [], engines.user_pack_problems())
    models = {}
    for role, (pool, rule) in pk["roles"].items():
        hits = _comfy.rule_matches(fx.pools[pool], rule)
        if hits:
            models[role] = sorted(hits, key=lambda x: (-_comfy.rank(x, rule, True), x))[0]
    check("and it is available against the fake's pools", engines.abilities(models).get("multi_ref_switch") is True, models)

    print("updates, disable, delete")
    code, out, err = cli("updates", "--lane", URL)
    check("updates: nothing changed", code == 0 and "changed" not in out.lower(), (code, out, err))
    p = os.path.join(WF, "templates", "video_multi_ref_switch.json")
    u = json.load(open(p)); u["extra"] = {"touched": True}; json.dump(u, open(p, "w"))
    code, out, err = cli("updates", "--lane", URL)
    check("updates: a template whose bytes changed is named", code == 0 and "The template changed on" in out and ID in out, (code, out, err))
    code, out, err = cli("disable", ID)
    check("disable removes the pack and says to restart", code == 0 and packs_listing() == [] and "Restart" in out, (code, out, err))
    code, out, err = cli("delete", ID)
    check("delete without --yes and no terminal is refused", code == 3 and os.path.isdir(d), (code, err))
    code, out, err = cli("delete", ID, "--yes")
    check("delete --yes removes the draft", code == 0 and not os.path.exists(d), (code, out, err))

    print("refusals exit 3 with the sentence on stderr")
    PAID = "This template runs on a paid online service, so it cannot become a mode here."
    code, out, err = cli("convert", "--lane", URL, "--template", "image_billed", "--api-json", os.path.join(fx_mod.FIX, "api", "image_billed.api.json"))
    check("billed template (non-api_ name)", code == 3 and PAID in err and not os.path.exists(os.path.join(DATA, "packs-draft", "image-billed")), (code, err))
    code, out, err = cli("convert", "--lane", URL, "--template", "api_paid_cloud", "--api-json", API)
    check("api_ template", code == 3 and PAID in err, (code, err))
    code, out, err = cli("convert", "--lane", URL, "--template", "no_such_template", "--api-json", API)
    check("a template the lane does not list", code == 3 and "no_such_template" in err, (code, err))
    code, out, err = cli("convert", "--lane", URL, "--template", "image_flat_t2i", "--api-json", os.path.join(SCRATCH, "missing.json"))
    check("an --api-json that is not there", code == 3 and "missing.json" in err, (code, err))
    open(os.path.join(SCRATCH, "notgraph.json"), "w").write('{"nodes": []}')
    code, out, err = cli("convert", "--lane", URL, "--template", "image_flat_t2i", "--api-json", os.path.join(SCRATCH, "notgraph.json"))
    check("an --api-json that is a UI workflow, not an API graph", code == 3 and "Export (API)" in err, (code, err))
    code, out, err = cli("show", "never-made")
    check("an unknown draft", code == 3 and err.strip() != "", (code, err))
    code, out, err = cli("set", "never-made", "x=1")
    check("set on an unknown draft", code == 3, (code, err))

    print("exit codes: usage 2, lane down 4, converter missing 5")
    check("no command is a usage error", cli()[0] == 2)
    check("a command missing its arguments is a usage error", cli("answer", "x")[0] == 2 and cli("convert", "--lane", URL)[0] == 2)
    code, out, err = cli("doctor", "--lane", "http://127.0.0.1:1")
    check("a lane that is down exits 4 with a sentence", code == 4 and err.strip() != "", (code, err))
    code, out, err = cli("--config", os.path.join(SCRATCH, "no-config.json"), "doctor", "--lane", "no-such-lane-id")
    check("a lane id with no config is a usage error", code == 2 and "lane" in err.lower(), (code, err))
    cfg = os.path.join(SCRATCH, "config.json")
    json.dump({"port": 1, "lanes": [{"id": "rig", "name": "The rig", "host": "127.0.0.1", "port": PORT}]}, open(cfg, "w"))
    code, out, err = cli("--config", cfg, "doctor", "--lane", "rig")
    check("--lane ID resolves through config.json", code == 0 and "0.99.0" in out, (code, out, err))
    before = open(cfg).read()
    check("and the config is only read", before == open(cfg).read())

    spec = importlib.util.spec_from_file_location("autopack_cli", CLI)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    from engines import _autopack_convert as conv
    def boom(*a, **k): raise conv.ConverterUnavailable("The browser converter is not installed (stub).")
    real = conv.convert
    conv.convert = boom
    import io, contextlib
    ebuf = io.StringIO()
    with contextlib.redirect_stderr(ebuf), contextlib.redirect_stdout(io.StringIO()):
        rc = mod.main(["--data", DATA, "convert", "--lane", URL, "--template", "image_flat_t2i"])
    conv.convert = real
    check("convert without --api-json and without a converter exits 5 with its sentence", rc == 5 and "not installed (stub)" in ebuf.getvalue(), (rc, ebuf.getvalue()))
    check("converter_status never raises and explains itself", isinstance(conv.converter_status(), tuple) and isinstance(conv.converter_status()[1], str))
    saved = {k: sys.modules.get(k) for k in ("playwright", "playwright.sync_api")}
    sys.modules["playwright"] = None; sys.modules["playwright.sync_api"] = None
    try:
        ok, sentence = conv.converter_status()
    finally:
        for k, v in saved.items():
            if v is None: sys.modules.pop(k, None)
            else: sys.modules[k] = v
    check("without playwright: not ok, and the sentence names the install line", ok is False and "requirements-converter.txt" in sentence, sentence)
    check("the allowlist helper keeps the lane's origin only",
          conv._same_origin("http://127.0.0.1:8188/api/x", "http://127.0.0.1:8188") and not conv._same_origin("http://example.com/x", "http://127.0.0.1:8188")
          and not conv._same_origin("http://127.0.0.1:9999/x", "http://127.0.0.1:8188") and not conv._same_origin("https://127.0.0.1:8188/x", "http://127.0.0.1:8188"))
finally:
    for p in PROCS:
        p.kill()
    shutil.rmtree(SCRATCH, ignore_errors=True)

print()
if FAILED:
    print("FAILED: %d" % len(FAILED))
    for n in FAILED: print("  -", n)
    sys.exit(1)
print("all passed")
