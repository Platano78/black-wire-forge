"""False availability: a mode that BWF reports "available" while its graph names a model file the
lane does not hold (spike bug 1, 2026-10-16: 4 of 28 "available" packs).

A template loader that no pool covers (here LoadDA3Model) gets no `requires` role, so today's
loader cannot see its file at all. Nothing about that is fixed in the loader; the guard is
autopack's readiness, which must not call such a draft available, and `enable`, which must
not copy it into packs/.

  (a) characterisation, stays green: the spike-shaped manifest (no requires) loaded by today's
      loader IS available on a lane that lacks the file -- the hole the guard closes;
  (b) readiness is `blocked` with the file named (S1: unslotted loader; after S2 the geometry
      pool turns this into `needs_files`);
  (c) `enable` refuses it with exit 3 even when the draft's own state says tested.

Run: python3 tests/test_autopack_falseavail.py
"""
import json, os, shutil, socket, subprocess, sys, tempfile, time, urllib.request
SCRATCH = tempfile.mkdtemp(prefix="bwf-apfa-")
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
fx = fx_mod.Fx()
NAME = "utility_unslotted_da3"
d = fx.synth(NAME)
manifest, graph = d["manifest"], d["graph"]

try:
    print("(a) characterisation: today's loader calls the spike-shaped pack available")
    check("the draft has no requires role (the loader's only way to see a file)", manifest["requires"] == [])
    check("the graph names a file the lane does not list", graph["2"]["inputs"]["model_name"] == "depth_anything_3_large.safetensors"
          and "depth_anything_3_large.safetensors" not in str(fx.oi["LoadDA3Model"]["input"]["required"]["model_name"]))
    pdir = os.path.join(DATA, "packs", manifest["id"]); os.makedirs(pdir)
    json.dump(manifest, open(os.path.join(pdir, "manifest.json"), "w")); json.dump(graph, open(os.path.join(pdir, "graph.api.json"), "w"))
    engines._PACKS = None
    pk = next((p for p in engines.packs() if p["id"] == manifest["id"]), None)
    check("the pack loads", pk is not None, engines.user_pack_problems())
    check("and abilities() says its mode is available with NO model files at all", engines.abilities({}).get(manifest["mode"]) is True)
    shutil.rmtree(pdir)
    engines._PACKS = None

    print("(b) readiness refuses to call it available")
    rd = ap.readiness(manifest, graph, d["flags"], fx.oi, fx.pools, True)
    check("state is blocked, not available", rd["state"] == "blocked", rd)
    check("the sentence names the file and the loader", any("depth_anything_3_large.safetensors" in s and "LoadDA3Model" in s for s in rd["blocked"]), rd["blocked"])
    no_flag = [f for f in d["flags"] if f["kind"] != "block"]
    rd2 = ap.readiness(manifest, graph, no_flag, fx.oi, fx.pools, True)
    check("without the flag the built graph still fails the validator: blocked, with the file quoted",
          rd2["state"] == "blocked" and any("depth_anything_3_large.safetensors" in s for s in rd2["blocked"]), rd2)
    oi2 = json.loads(json.dumps(fx.oi))
    oi2["LoadDA3Model"]["input"]["required"]["model_name"] = ["COMBO", {"options": ["depth_anything_3_large.safetensors"]}]
    rd3 = ap.readiness(manifest, graph, no_flag, oi2, fx.pools, True)
    check("and when the lane does list the file, the validator is satisfied", rd3["state"] == "available", rd3)
    oi3 = json.loads(json.dumps(fx.oi)); del oi3["RunDA3"]
    rd4 = ap.readiness(manifest, graph, no_flag, oi3, fx.pools, True)
    check("a node the lane lacks is blocked in plain words", rd4["state"] == "blocked" and any("RunDA3" in s and "does not have" in s for s in rd4["blocked"]), rd4)
    pools_empty = dict(fx.pools); pools_empty["vae"] = []
    flat = fx.synth("image_flat_t2i")
    rd5 = ap.readiness(flat["manifest"], flat["graph"], flat["flags"], fx.oi, pools_empty, True)
    check("a missing role file is needs_files, with the directory and url from the template", rd5["state"] == "needs_files"
          and rd5["missing"][0]["template_file"] == "flat_vae.safetensors", rd5)
    srcs = ap.template_model_sources(fx.ui("image_flat_t2i"))
    rd6 = ap.readiness(flat["manifest"], flat["graph"], flat["flags"], fx.oi, pools_empty, True, sources=srcs)
    check("with the template's sources: directory and url filled in", rd6["missing"][0]["directory"] == "vae" and rd6["missing"][0]["url"] == "https://example.invalid/m/flat_vae.safetensors", rd6["missing"])
    rd7 = ap.readiness(flat["manifest"], flat["graph"], flat["flags"], fx.oi, fx.pools, True)
    check("a clean pack is available, and says which file each role gets", rd7["state"] == "available"
          and [r["file"] for r in rd7["roles"]] == ["flatmodel_bf16.safetensors", "flat_text_enc.safetensors", "flat_vae.safetensors"]
          and not any(r["substituted"] for r in rd7["roles"]), rd7)
    sub = fx.synth("image_subst_dit")
    rd8 = ap.readiness(sub["manifest"], sub["graph"], sub["flags"], fx.oi, fx.pools, True)
    check("a substituted file is reported as such", rd8["state"] == "available" and rd8["roles"][0]["substituted"] is True
          and rd8["roles"][0]["file"] == "zq_dit_int8_convrot.safetensors" and rd8["roles"][0]["template_file"] == "zq_dit_fp16.safetensors", rd8)

    print("(c) enable refuses it, exit 3, even if the draft says tested")
    def free_port():
        s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p
    PORT = free_port(); URL = "http://127.0.0.1:%d" % PORT
    PROCS.append(subprocess.Popen([sys.executable, os.path.join(HERE, "fixtures", "fake_comfy.py"), "--port", str(PORT), "--store", os.path.join(SCRATCH, "lane"),
                                   "--workflows", fx_mod.FIX, "--object-info-file", os.path.join(fx_mod.FIX, "object_info.json")],
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT))
    for _ in range(100):
        try:
            urllib.request.urlopen(URL + "/system_stats", timeout=1).read(); break
        except Exception:
            time.sleep(0.1)
    CLI = os.path.join(ROOT, "scripts", "autopack.py")
    def cli(*args):
        r = subprocess.run([sys.executable, CLI, "--data", DATA] + list(args), capture_output=True, text=True, timeout=120, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        return r.returncode, r.stdout, r.stderr
    code, out, err = cli("convert", "--lane", URL, "--template", NAME, "--api-json", os.path.join(fx_mod.FIX, "api", NAME + ".api.json"))
    check("the draft is written so the user can see why", code == 0 and os.path.isdir(os.path.join(DATA, "packs-draft", "utility-unslotted-da3")), (code, out, err))
    code, out, err = cli("show", "utility-unslotted-da3", "--lane", URL)
    check("show says it cannot be added yet", code == 0 and "can't be added yet" in out and "blocked" in out.lower(), (code, out, err))
    cli("licence", "utility-unslotted-da3", "--name", "x", "--checked"); cli("ack", "utility-unslotted-da3", "no_prompt")
    code, out, err = cli("review", "utility-unslotted-da3")
    check("review refuses it", code == 3 and "can't be added yet" in err, (code, err))
    cur = ap.read_draft(DATA, "utility-unslotted-da3")
    m_sha, g_sha = ap.hashes(cur["manifest"], cur["graph"])
    a = cur["ap"]
    a["state"] = "tested"
    a["reviewed"] = {"manifest_sha256": m_sha, "graph_sha256": g_sha, "at": 1}
    a["test"] = {"lane": URL, "job": "x", "prompt_id": "x", "inputs": {}, "outputs": ["o.png"], "machine_pass": True, "reasons": [],
                 "looks_right": True, "manifest_sha256": m_sha, "graph_sha256": g_sha, "at": 1}
    ap._write_ap(cur["dir"], a)
    code, out, err = cli("enable", "utility-unslotted-da3")
    check("enable exits 3 and says why", code == 3 and "depth_anything_3_large.safetensors" in err, (code, out, err))
    check("nothing reached packs/", not os.path.isdir(os.path.join(DATA, "packs")) or os.listdir(os.path.join(DATA, "packs")) == [], os.listdir(DATA))
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
