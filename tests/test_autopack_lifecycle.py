"""engines/_autopack.py draft store and state machine (spec section 7): draft -> reviewed ->
tested -> enabled, and back.

Contract under test:
  * every transition in 7.3 and every refusal beside it, with its sentence;
  * an edit after review (detected by hash, even a hand edit of manifest.json) returns the
    pack to draft; a test only counts while the hashes it recorded still match;
  * enable copies exactly three files into packs/<id>/ through a hidden staging folder;
    disable and delete touch only what autopack made; a hand pack (no autopack.json) is never
    touched; delete is refused while enabled; `shippable: true` is refused;
  * NOTHING ever enables as a side effect: the packs folder stays empty after every command
    except the explicit enable.

Run: python3 tests/test_autopack_lifecycle.py
"""
import copy, json, os, shutil, sys, tempfile
DATA = tempfile.mkdtemp(prefix="bwf-apl-")
os.environ["GENCENTER_DATA"] = DATA
import _autopack_fixture as fx_mod
ap = fx_mod.ap
import engines

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail else ""))
    if not cond: FAILED.append(name)

fx = fx_mod.Fx()


def packs_listing():
    d = os.path.join(DATA, "packs")
    return sorted(os.listdir(d)) if os.path.isdir(d) else []


def taken(exclude=None):
    """for synthesis: the live engines and the drafts that exist"""
    return ap.taken_for(DATA, engines.packs(), exclude=exclude)


def live():
    """for review / enable: the engines BWF has loaded now"""
    return engines.packs()


def refused(fn, *a, **k):
    try:
        fn(*a, **k)
    except ap.Refused as e:
        return e.sentence
    return None


def new(name, lane="rig"):
    d = fx.synth(name, taken=taken())
    return ap.create_draft(DATA, d, **fx_mod.provenance(fx, name, lane))


def state(did):
    return ap.read_draft(DATA, did)["ap"]["state"]


def confirm_licence(did):
    ap.set_licence(DATA, did, "Test licence", None, True)


def ready(did):
    r = ap.read_draft(DATA, did)
    return ap.readiness(r["manifest"], r["graph"], r["ap"]["flags"], fx.oi, fx.pools, True)


print("convert: (none) -> draft")
did, created = new("image_flat_t2i")
r = ap.read_draft(DATA, did)
check("a new draft", created and did == "image-flat-t2i" and r["ap"]["state"] == "draft")
check("draft folder layout", sorted(os.listdir(r["dir"])) == ["autopack.json", "graph.api.json", "manifest.json", "source"]
      and sorted(os.listdir(os.path.join(r["dir"], "source"))) == ["converted.api.json", "synth.manifest.json", "template.ui.json"])
a = r["ap"]
check("provenance recorded", a["autopack"] == 1 and a["source"]["lane"] == "rig" and a["source"]["template"] == "image_flat_t2i"
      and a["source"]["comfyui_version"] == "0.0.1" and a["source"]["converter"] == "export"
      and len(a["source"]["template_sha256"]) == 64 and len(a["source"]["converted_sha256"]) == 64, a["source"])
check("the template bytes are kept as fetched", open(os.path.join(r["dir"], "source", "template.ui.json"), "rb").read() == fx.ui_bytes("image_flat_t2i"))
check("licence starts unconfirmed", a["licence"]["confirmed"] is False and a["reviewed"] is None and a["test"] is None and a["enabled"] is None)
check("the loader-facing pack folder is untouched", packs_listing() == [])
did2, created2 = new("image_flat_t2i")
check("the same (lane, template) returns the existing draft", did2 == did and created2 is False and os.listdir(os.path.join(DATA, "packs-draft")) == [did])
did3, created3 = new("image_flat_t2i", lane="other")
check("another lane makes another draft (-2)", created3 and did3 == "image-flat-t2i-2", did3)
ap.delete(DATA, did3)
check("delete removes a draft", not os.path.exists(os.path.join(DATA, "packs-draft", did3)))

print("review: draft -> reviewed")
unmet = ap.review(DATA, did, fx.oi, live())
check("the unconfirmed licence stops it, in a sentence", unmet and any("licen" in s.lower() for s in unmet) and state(did) == "draft", unmet)
check("the open answer flag is named", any("licence" in s.lower() for s in unmet))
confirm_licence(did)
check("licence recorded as unknown, never shippable", ap.read_draft(DATA, did)["manifest"]["licence"]["shippable"] is False
      and ap.read_draft(DATA, did)["ap"]["licence"]["confirmed"] is True and ap.read_draft(DATA, did)["ap"]["licence"]["name"] == "Test licence")
check("review passes", ap.review(DATA, did, fx.oi, live()) == [] and state(did) == "reviewed")
r = ap.read_draft(DATA, did)
msha, gsha = ap.hashes(r["manifest"], r["graph"])
check("review records the hashes", r["ap"]["reviewed"]["manifest_sha256"] == msha and r["ap"]["reviewed"]["graph_sha256"] == gsha)
check("still nothing enabled", packs_listing() == [])

print("edits after review drop it back to draft")
ap.edit(DATA, did, ["prompt.label=Describe it"])
check("a set command returns it to draft", state(did) == "draft")
check("the edit is in manifest.json", ap.read_draft(DATA, did)["manifest"]["fields"][0]["label"] == "Describe it")
ap.review(DATA, did, fx.oi, live())
check("re-review", state(did) == "reviewed")
mp = os.path.join(DATA, "packs-draft", did, "manifest.json")
m = json.load(open(mp)); m["label"] = "Hand edited"; json.dump(m, open(mp, "w"))
check("a hand edit of manifest.json is caught by hash on the next read", state(did) == "draft")
m["label"] = "Flat: Text to Image"; json.dump(m, open(mp, "w"))
ap.edit(DATA, did, ["prompt.label=Prompt"])
ap.review(DATA, did, fx.oi, live())
gp = os.path.join(DATA, "packs-draft", did, "graph.api.json")
g = json.load(open(gp)); g["9"]["inputs"]["filename_prefix"] = "changed"; json.dump(g, open(gp, "w"))
check("a graph edit too", state(did) == "draft")
g["9"]["inputs"]["filename_prefix"] = "flat"; json.dump(g, open(gp, "w"))
ap.review(DATA, did, fx.oi, live())
check("back to reviewed once the review is redone", state(did) == "reviewed")

print("edit validation")
check("an unknown field is refused", "no field" in (refused(ap.edit, DATA, did, ["nope.label=x"]) or ""))
check("a bad tier is refused", refused(ap.edit, DATA, did, ["prompt.tier=huge"]) is not None)
check("a range that is not low,high is refused", refused(ap.edit, DATA, did, ["steps.range=5"]) is not None)
ap.edit(DATA, did, ["steps.range=2,60", "steps.default=30", "steps.units=iterations", "steps.hint=Fewer is faster", "steps.tier=primary", "steps.order=9"])
st = next(f for f in ap.read_draft(DATA, did)["manifest"]["fields"] if f["id"] == "steps")
check("label/hint/default/units/range/tier/order are editable", (st["range"], st["default"], st["units"], st["hint"], st["tier"], st["order"]) == ([2, 60], 30, "iterations", "Fewer is faster", "primary", 9), st)
check("a default outside the field's kind is refused", refused(ap.edit, DATA, did, ["steps.default=abc"]) is not None)
ap.edit(DATA, did, ["cfg.hidden=true"])
check("hiding removes the field (the graph keeps its value)", "cfg" not in [f["id"] for f in ap.read_draft(DATA, did)["manifest"]["fields"]]
      and ap.read_draft(DATA, did)["graph"]["7"]["inputs"]["cfg"] == 4.5)
ap.edit(DATA, did, ["cfg.hidden=false"])
check("un-hiding restores it from the synthesized manifest", "cfg" in [f["id"] for f in ap.read_draft(DATA, did)["manifest"]["fields"]])
ap.edit(DATA, did, ["label=A new label", "note=One line.", "mode=flat_two"])
m = ap.read_draft(DATA, did)["manifest"]
check("label, note and mode are editable", (m["label"], m["note"], m["mode"]) == ("A new label", "One line.", "flat_two"))
check("mode is checked against the loader's rule", refused(ap.edit, DATA, did, ["mode=3d"]) is not None and refused(ap.edit, DATA, did, ["mode=Bad Mode"]) is not None)
check("room must be a rooms.json id", refused(ap.edit, DATA, did, ["room=no-such-room"], ) is not None)
ap.edit(DATA, did, ["mode=flat_t2i"])
ap.review(DATA, did, fx.oi, live())

print("shippable true is refused at review")
m = json.load(open(mp)); m["licence"]["shippable"] = True; json.dump(m, open(mp, "w"))
unmet = ap.review(DATA, did, fx.oi, live())
check("the sentence is exact", "Packs made from templates stay unshippable." in unmet and state(did) == "draft", unmet)
m["licence"]["shippable"] = False; json.dump(m, open(mp, "w"))

print("the loader must accept the draft against the engines that exist now")
ab = next(iter(next(p for p in engines.packs() if p.get("provides"))["provides"]))
m = json.load(open(mp)); m["mode"] = ab; json.dump(m, open(mp, "w"))
unmet = ap.review(DATA, did, fx.oi, live())
check("a clashing mode is an unmet precondition", unmet and any("clash" in s or "already" in s for s in unmet) and state(did) == "draft", unmet)
m["mode"] = "flat_t2i"; json.dump(m, open(mp, "w"))
g = json.load(open(gp)); g["7"]["inputs"]["cfg"] = 900.0; json.dump(g, open(gp, "w"))
unmet = ap.review(DATA, did, fx.oi, live())
check("a graph the validator rejects is an unmet precondition", unmet and any("cfg" in s for s in unmet), unmet)
g["7"]["inputs"]["cfg"] = 4.5; json.dump(g, open(gp, "w"))
check("a clean re-review", ap.review(DATA, did, fx.oi, live()) == [] and state(did) == "reviewed")

print("answers, acks and block flags (sub_edit and da3)")
sid, _ = new("image_sub_edit")
confirm_licence(sid)
unmet = ap.review(DATA, sid, fx.oi, live())
check("an unacked flag stops review", unmet and any("promoted" in s.lower() or "check" in s.lower() for s in unmet), unmet)
check("an answer flag cannot be acked", refused(ap.ack, DATA, sid, ["licence"]) is not None)
check("an unknown flag is refused", refused(ap.ack, DATA, sid, ["nope"]) is not None)
ap.ack(DATA, sid, ["promoted_unreviewed"])
check("acked, review passes", ap.review(DATA, sid, fx.oi, live()) == [] and state(sid) == "reviewed")
bid, _ = new("utility_unslotted_da3")
confirm_licence(bid); ap.ack(DATA, bid, ["no_prompt"])
unmet = ap.review(DATA, bid, fx.oi, live())
check("a block flag stops review for good", unmet and any("can't be added yet" in s for s in unmet) and state(bid) == "draft", unmet)
check("a block flag cannot be acked", refused(ap.ack, DATA, bid, ["unslotted_loader:2.model_name"]) is not None)
check("the blocked draft cannot be tested or enabled", refused(ap.enable, DATA, bid, live(), ready(bid)) is not None)
check("nothing enabled yet", packs_listing() == [])

print("test records and the verdict: reviewed -> tested")
bad_entry = {"status": {"status_str": "error", "completed": False}, "outputs": {}}
v = ap.test_verdict(bad_entry, "image")
check("machine pass fails with reasons for an errored job with no output", v["machine_pass"] is False and len(v["reasons"]) >= 2, v)
good_entry = {"status": {"status_str": "success", "completed": True}, "outputs": {"9": {"images": [{"filename": "o.png", "subfolder": "bwf", "type": "output"}]}}}
v = ap.test_verdict(good_entry, "image", sizes={"o.png": 1234})
check("a finished job with an output of the pack's kind passes", v == {"machine_pass": True, "reasons": []}, v)
v = ap.test_verdict(good_entry, "video")
check("an output of another kind than the cap is a reason", v["machine_pass"] is False and any("video" in s for s in v["reasons"]), v)
v = ap.test_verdict(good_entry, "image", sizes={"o.png": 0})
check("an empty download is a reason", v["machine_pass"] is False, v)
v = ap.test_verdict({"status": {"status_str": "success", "completed": True}, "outputs": {"3": {"images": [{"filename": "p.png", "type": "temp"}]}}}, "image")
check("a preview (temp) is not an output", v["machine_pass"] is False, v)
v = ap.test_verdict({"status": {"status_str": "success", "completed": True}, "outputs": {"3": {"glb": [{"filename": "m.glb", "type": "output"}]}}}, "3d")
check("3d covers .glb", v["machine_pass"] is True, v)

check("verdict before any test is refused", refused(ap.verdict, DATA, did, True) is not None)
ap.record_test(DATA, did, {"lane": "rig", "job": "j", "prompt_id": "p1", "inputs": {}, "outputs": ["o.png"], "machine_pass": False, "reasons": ["The lane made no file."]})
t = ap.read_draft(DATA, did)["ap"]["test"]
cur = ap.read_draft(DATA, did)
check("the test record carries the current hashes and no verdict yet",
      t["looks_right"] is None and (t["manifest_sha256"], t["graph_sha256"]) == ap.hashes(cur["manifest"], cur["graph"]))
check("a machine fail cannot become tested", refused(ap.verdict, DATA, did, True) is not None and state(did) == "reviewed")
check("'wrong' is recorded and the pack stays reviewed", ap.verdict(DATA, did, False) == "reviewed" and ap.read_draft(DATA, did)["ap"]["test"]["looks_right"] is False)
ap.record_test(DATA, did, {"lane": "rig", "job": "j", "prompt_id": "p2", "inputs": {}, "outputs": ["o.png"], "machine_pass": True, "reasons": []})
ap.edit(DATA, did, ["prompt.hint=changed after the test"])
check("an edit after the test drops the pack to draft, and the stale test cannot pass", state(did) == "draft" and refused(ap.verdict, DATA, did, True) is not None)
ap.edit(DATA, did, ["prompt.hint=Short is fine"])
ap.review(DATA, did, fx.oi, live())
ap.record_test(DATA, did, {"lane": "rig", "job": "j", "prompt_id": "p3", "inputs": {}, "outputs": ["o.png"], "machine_pass": True, "reasons": []})
check("looks right moves it to tested", ap.verdict(DATA, did, True) == "tested" and state(did) == "tested")
check("still nothing enabled", packs_listing() == [])

print("enable: tested -> enabled")
cur = ap.read_draft(DATA, did)
short = ap.readiness(cur["manifest"], cur["graph"], cur["ap"]["flags"], fx.oi, dict(fx.pools, unet=[]), True)
check("enable on a lane that lacks a file is refused, naming it", short["state"] == "needs_files" and "flatmodel_bf16" in (refused(ap.enable, DATA, did, live(), short) or ""))
rd = ready(did)
check("readiness on the tested lane is available", rd["state"] == "available", rd)
hand = os.path.join(DATA, "packs", "image-flat-t2i"); os.makedirs(hand)
open(os.path.join(hand, "manifest.json"), "w").write("{}")
check("a packs/<id> that already exists is never overwritten", refused(ap.enable, DATA, did, live(), rd) is not None and os.listdir(hand) == ["manifest.json"])
shutil.rmtree(hand)
ghost = {"id": "image-flat-t2i", "cap": "image", "provides": {}, "graphs": {}, "roles": {}}
check("an id taken by a live engine is refused", refused(ap.enable, DATA, did, live() + [ghost], rd) is not None)
ghost2 = {"id": "someone-else", "cap": "image", "provides": {"flat_t2i": []}, "graphs": {"flat_t2i": None}, "roles": {}}
check("a mode taken by a live engine is refused", refused(ap.enable, DATA, did, live() + [ghost2], rd) is not None)
check("a failed enable leaves no staging folder", packs_listing() == [])
pack_dir = ap.enable(DATA, did, live(), rd)
check("enable writes exactly three files", sorted(os.listdir(pack_dir)) == ["autopack.json", "graph.api.json", "manifest.json"] and packs_listing() == ["image-flat-t2i"], os.listdir(pack_dir))
check("state is enabled and recorded", state(did) == "enabled" and ap.read_draft(DATA, did)["ap"]["enabled"]["pack_dir"] == pack_dir)
check("the copy is the draft", json.load(open(os.path.join(pack_dir, "manifest.json"))) == ap.read_draft(DATA, did)["manifest"]
      and json.load(open(os.path.join(pack_dir, "autopack.json")))["id"] == did and json.load(open(os.path.join(pack_dir, "autopack.json")))["state"] == "enabled")
engines._PACKS = None
check("BWF's own scan now loads it (the restart's effect)", any(p["id"] == "image-flat-t2i" and p.get("user_pack") for p in engines.packs()) and engines.user_pack_problems() == [], engines.user_pack_problems())
check("enable twice is refused", refused(ap.enable, DATA, did, live(), rd) is not None)
check("delete is refused while enabled", refused(ap.delete, DATA, did) == "Disable it first.")
check("an enabled pack still reads as enabled (hash demotion is for reviewed/tested only)", state(did) == "enabled")

print("disable: enabled -> tested; never a hand pack")
ap.disable(DATA, did)
check("disable removes only the pack folder and returns to tested", packs_listing() == [] and state(did) == "tested"
      and os.path.isdir(os.path.join(DATA, "packs-draft", did)))
os.makedirs(os.path.join(DATA, "packs", did)); open(os.path.join(DATA, "packs", did, "manifest.json"), "w").write("{}")
r = ap.read_draft(DATA, did); r["ap"]["state"] = "enabled"; ap._write_ap(r["dir"], r["ap"])
check("a hand pack (no autopack.json) is refused and untouched", refused(ap.disable, DATA, did) is not None and os.listdir(os.path.join(DATA, "packs", did)) == ["manifest.json"])
json.dump({"autopack": 1, "id": "someone-else", "state": "enabled"}, open(os.path.join(DATA, "packs", did, "autopack.json"), "w"))
check("a pack whose autopack.json names another id is refused", refused(ap.disable, DATA, did) is not None)
shutil.rmtree(os.path.join(DATA, "packs", did))
r = ap.read_draft(DATA, did); r["ap"]["state"] = "tested"; ap._write_ap(r["dir"], r["ap"])
check("enable and disable never touch another draft's pack", packs_listing() == [])

print("delete")
check("delete refuses a bad id (no path escape)", refused(ap.delete, DATA, "../packs") is not None and refused(ap.read_draft, DATA, "../x") is not None)
ap.delete(DATA, did)
check("delete removes the draft folder", not os.path.exists(os.path.join(DATA, "packs-draft", did)))
check("an unknown draft is a sentence, not a traceback", refused(ap.read_draft, DATA, "never-made") is not None)

print("nothing enabled itself, at any point")
check("the packs folder is empty at the end", packs_listing() == [])

print()
if FAILED:
    print("FAILED: %d" % len(FAILED))
    for n in FAILED: print("  -", n)
    sys.exit(1)
print("all passed")
