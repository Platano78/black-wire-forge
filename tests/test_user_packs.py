"""Data packs (user workflow packs): DATA_DIR/packs/<id>/manifest.json + graph.api.json
become ordinary ENGINE dicts through engines/_userpacks.py.

Contract under test:
  * a valid pack appears in modes_for / fields / abilities / graph_for, with
    bound values and resolved model file names written into the graph;
  * every kind of bad pack is SKIPPED with one problem line and never raises;
  * no packs folder (or an empty one) leaves the scan byte-identical;
  * the loader reads only DATA_DIR/packs and stays inside each pack folder.

Run: python3 tests/test_user_packs.py   (scripts/run-tests.sh puts scratch on disk)
"""
import copy, json, os, re, shutil, sys, tempfile
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail else ""))
    if not cond: FAILED.append(name)

WORK = tempfile.mkdtemp(prefix="bwf-userpacks-")
os.environ["GENCENTER_DATA"] = WORK
import engines
import engines._userpacks as up


def scan(data=WORK):
    """Rescan with GENCENTER_DATA=data; returns (packs, problems)."""
    os.environ["GENCENTER_DATA"] = data
    engines._PACKS = None
    return engines.packs(), engines.user_pack_problems()


# -- a tiny synthetic graph and manifest --------------------------------------
GRAPH = {
    "1": {"class_type": "ModelLoader", "inputs": {"model_name": "placeholder.safetensors"}},
    "2": {"class_type": "TextBox", "inputs": {"text": "hello", "clip": ["1", 0]}},
    "3": {"class_type": "Sampler", "inputs": {"steps": 10, "seed": 1, "strength": 1.0, "sampler": "a", "on": False}},
    "4": {"class_type": "PictureIn", "inputs": {"image": "stale.png"}},
    "5": {"class_type": "ClipIn", "inputs": {"file": "stale.mp4", "second": "stale2.mp4"}},
    "6": {"class_type": "LoraBox", "inputs": {"model": ["1", 0], "lora_name": "placeholder_lora.safetensors"}},
}
MANIFEST = {
    "schema": 1, "id": "demo", "cap": "video", "mode": "demomode",
    "label": "Demo mode", "note": "Pick this to test data packs.", "graph": "graph.api.json",
    "fields": [
        {"id": "prompt", "label": "Prompt", "type": "textarea", "default": "a default", "tier": "primary",
         "group": "Content", "order": 1, "bind": [{"node": "2", "input": "text"}]},
        {"id": "clip", "label": "Clip", "type": "video_list", "max": 2, "required": True, "tier": "primary",
         "group": "Content", "order": 2,
         "bind": [{"node": "5", "input": "file", "index": 0}, {"node": "5", "input": "second", "index": 1}]},
        {"id": "pic", "label": "Picture", "type": "image", "tier": "primary", "group": "Content", "order": 3,
         "bind": [{"node": "4", "input": "image"}]},
        {"id": "steps", "label": "Steps", "type": "int", "default": 20, "units": "steps", "range": [1, 100],
         "ui_range": [4, 50], "tier": "advanced", "group": "Quality", "order": 1,
         "bind": [{"node": "3", "input": "steps"}]},
        {"id": "strength", "label": "Strength", "type": "number", "default": 0.75, "units": "x",
         "bind": [{"node": "3", "input": "strength"}]},
        {"id": "seed", "label": "Seed", "type": "int", "units": "seed", "bind": [{"node": "3", "input": "seed"}]},
        {"id": "sampler", "label": "Sampler", "type": "select", "options": ["a", "b"], "default": "a",
         "bind": [{"node": "3", "input": "sampler"}]},
        {"id": "extra", "label": "Extra", "type": "checkbox", "default": False,
         "enabled_when": {"field": "sampler", "equals": "b"}, "disabled_reason": "Only with sampler b.",
         "bind": [{"node": "3", "input": "on"}]},
    ],
    "requires": [
        {"role": "unet", "pool": "unet", "node": "1", "input": "model_name",
         "match": {"all": ["demo", "base"], "prefer": ["int8"]}, "words": "the demo model"},
        {"role": "lora", "pool": "lora", "node": "6", "input": "lora_name",
         "match": {"all": ["demo_lora"]}, "words": "the demo adapter"},
    ],
    "licence": {"name": "Demo Licence", "shippable": False, "attribution": "Demo Co", "url": "https://example.invalid/l"},
}
U_UNET, U_LORA = "u__demo__unet", "u__demo__lora"
MODELS = {U_UNET: "sub/demo_base_int8.safetensors", U_LORA: "sub/demo_lora_v1.safetensors"}


def write_pack(data, name, manifest=None, graph=None, raw_manifest=None, raw_graph=None):
    d = os.path.join(data, "packs", name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "manifest.json"), "w") as f:
        f.write(raw_manifest if raw_manifest is not None else json.dumps(manifest if manifest is not None else MANIFEST))
    with open(os.path.join(d, "graph.api.json"), "w") as f:
        f.write(raw_graph if raw_graph is not None else json.dumps(graph if graph is not None else GRAPH))
    return d


def fresh():
    d = tempfile.mkdtemp(prefix="case-", dir=WORK)
    return d


def mut(**kw):
    m = copy.deepcopy(MANIFEST)
    m.update(kw)
    return m


def problem_for(name, problems):
    return next((p for p in problems if p.startswith(name + ":")), None)


# -- 0. baseline: no packs folder at all ---------------------------------------
print("no packs folder")
empty = fresh()
base_packs, base_problems = scan(empty)
BASE_IDS = [p["id"] for p in base_packs]
check("scan works with no packs folder", len(BASE_IDS) > 0)
check("no problems with no packs folder", base_problems == [])
check("no user pack in the baseline", not any(p.get("user_pack") for p in base_packs))
os.makedirs(os.path.join(empty, "packs"))
p2, pr2 = scan(empty)
check("empty packs folder = identical pack list", [p["id"] for p in p2] == BASE_IDS and pr2 == [])
check("same pack objects survive (not rebuilt)", all(a.keys() == b.keys() for a, b in zip(base_packs, p2)))
BUILTIN = {p["id"]: p for p in base_packs}
BASE_ABILITIES = engines.abilities({})
BASE_MODES = {c: engines.modes_for(c) for c in engines.caps()}
BASE_ROLES = dict(engines.role_pool())

# -- 1. a valid pack -----------------------------------------------------------
print("a valid pack")
good = fresh()
write_pack(good, "demo")
packs, problems = scan(good)
check("valid pack loads with no problems", problems == [], problems)
check("pack is in packs()", "demo" in [p["id"] for p in packs])
check("packs stay sorted by id", [p["id"] for p in packs] == sorted(p["id"] for p in packs))
check("mode appears in modes_for(video)", "demomode" in engines.modes_for("video"))
check("built-in modes are untouched", all(m in engines.modes_for("video") for m in BASE_MODES["video"]))
fl = engines.fields("video", "demomode")
check("fields come back in order", [f["id"] for f in fl][:3] == ["prompt", "clip", "pic"])
check("bind / required are not shown to the page", all("bind" not in f and "required" not in f for f in fl))
check("descriptor keys survive", fl[3]["ui_range"] == [4, 50] and fl[3]["units"] == "steps" and fl[3]["group"] == "Quality")
check("enabled_when survives", fl[7]["enabled_when"] == {"field": "sampler", "equals": "b"})
check("legacy_dispatch is false (generic path)", engines.legacy_dispatch("video", "demomode") is False)
check("role names are namespaced", U_UNET in engines.role_pool() and engines.role_pool()[U_UNET] == "unet")
check("role rule is lower-cased data", engines.role_rules()[U_UNET] == {"all": ["demo", "base"], "prefer": ["int8"]})
ab = engines.abilities(MODELS)
check("ability true when every required model resolved", ab["demomode"] is True)
check("cap is true through the pack", ab["video"] is True)
ab2 = engines.abilities({U_UNET: MODELS[U_UNET]})
check("ability false when one required model is missing", ab2["demomode"] is False)
check("missing words name the absent file", engines.missing_words({U_UNET: MODELS[U_UNET]}, "demomode") == ["the demo adapter"])
check("both missing -> both named", engines.missing_words({}, "demomode") == ["the demo model", "the demo adapter"])
check("mode_ability is the mode", engines.mode_ability("video", "demomode") == "demomode")
check("built-in abilities unchanged for an empty lane",
      {k: v for k, v in engines.abilities({}).items() if k in BASE_ABILITIES} == BASE_ABILITIES)
check("mode label", engines.mode_words("video").get("demomode") == "Demo mode")
check("mode note", engines.mode_note("video", "demomode") == "Pick this to test data packs.")
check("licence_for", engines.licence_for("video", "demomode") ==
      {"name": "Demo Licence", "shippable": False, "attribution": "Demo Co"})
check("licences() lists it", any(l["engine"] == "demo" and l["name"] == "Demo Licence" for l in engines.licences()))
check("lane headline is not hijacked by the data pack",
      engines.describe(MODELS, "video") != "Demo mode" and "Demo mode" not in engines.describe({}, "video"))
check("describe_mode names the mode on its own model", engines.describe_mode(MODELS, "video", "demomode") == "Demo mode")
check("primary role is the first requires", engines.primary_role("video", "demomode") == U_UNET)

# graph_for with bound values
args = {"prompt": "hello there", "clip": ["c1.mp4", "c2.mp4"], "pic": "p.png", "steps": 8, "strength": 0.5,
        "seed": 99, "sampler": "b", "extra": True}
g = engines.graph_for("video", "demomode", args, MODELS)
check("text bound", g["2"]["inputs"]["text"] == "hello there")
check("link inputs untouched", g["2"]["inputs"]["clip"] == ["1", 0] and g["6"]["inputs"]["model"] == ["1", 0])
check("list index 0 and 1", g["5"]["inputs"] == {"file": "c1.mp4", "second": "c2.mp4"})
check("image bound", g["4"]["inputs"]["image"] == "p.png")
check("int / number / seed / select / checkbox bound",
      (g["3"]["inputs"]["steps"], g["3"]["inputs"]["strength"], g["3"]["inputs"]["seed"],
       g["3"]["inputs"]["sampler"], g["3"]["inputs"]["on"]) == (8, 0.5, 99, "b", True))
check("requires file names written into the graph",
      g["1"]["inputs"]["model_name"] == MODELS[U_UNET] and g["6"]["inputs"]["lora_name"] == MODELS[U_LORA])
check("graph_for does not mutate the template", engines.graph_for("video", "demomode", {"clip": ["z.mp4"]}, MODELS)["4"]["inputs"]["image"] == "stale.png")
g2 = engines.graph_for("video", "demomode", {"clip": ["only.mp4"]}, MODELS)
check("absent field -> its declared default", g2["2"]["inputs"]["text"] == "a default" and g2["3"]["inputs"]["steps"] == 20
      and g2["3"]["inputs"]["strength"] == 0.75)
check("absent field with no default -> graph keeps its own value", g2["3"]["inputs"]["seed"] == 1)
check("list index past the end is skipped", g2["5"]["inputs"] == {"file": "only.mp4", "second": "stale2.mp4"})
try:
    engines.graph_for("video", "demomode", {}, MODELS)
    check("required field missing -> KeyError(field id)", False)
except KeyError as e:
    check("required field missing -> KeyError(field id)", e.args[0] == "clip")
try:
    engines.graph_for("video", "demomode", {"clip": ["c.mp4"]}, {U_UNET: MODELS[U_UNET]})
    check("unresolved model at build -> ValueError naming it", False)
except ValueError as e:
    check("unresolved model at build -> ValueError naming it", "the demo adapter" in str(e))

# every public accessor is safe on a data-pack mode
acc = [lambda: engines.presets("video", "demomode"), lambda: engines.quality("video", "demomode"),
       lambda: engines.examples("video", "demomode"), lambda: engines.writer("video", "demomode"),
       lambda: engines.reviser("video", "demomode"), lambda: engines.post_for("video", "demomode"),
       lambda: engines.needs("video", "demomode"), lambda: engines.mode_room("video", "demomode"),
       lambda: engines.prompt_guide("video", "demomode"), lambda: engines.mode_deps_reason("video", "demomode"),
       lambda: engines.lane_kind("video", "demomode"), lambda: engines.bins("video", "demomode"),
       lambda: engines.pack_dir("video", "demomode"), lambda: engines.edit_in("video", "demomode"),
       lambda: engines.mode_model_words("video", "demomode"), lambda: engines.rooms(),
       lambda: engines.caps(), lambda: engines.cap_word("video"), lambda: engines.cap_order("video"),
       lambda: engines.stage_class_types(), lambda: engines.optional_nodes(), lambda: engines.style_catalogs()]
errs = []
for fn in acc:
    try:
        fn()
    except Exception as e:                      # noqa: BLE001
        errs.append(repr(e))
check("public accessors do not raise on a data-pack mode", not errs, errs)

# a room, a second valid pack in a NEW capability, two packs sorted
good2 = fresh()
write_pack(good2, "demo")
m_other = mut(id="aaa-other", cap="gizmo", mode="spin", room="clips")
write_pack(good2, "aaa-other", manifest=m_other)
pk, pr = scan(good2)
check("two valid packs, no problems", pr == [], pr)
check("new capability appears", "gizmo" in engines.caps() and engines.modes_for("gizmo") == ["spin"])
check("room from the manifest", engines.mode_room("gizmo", "spin") == "clips")
check("a new cap falls back to its own name as the word", engines.cap_word("gizmo") == "gizmo")

# -- 2. bad packs: skipped with a problem line, never raised --------------------
print("bad packs are skipped, never raised")
def expect_skip(label, name, why_re, manifest=None, graph=None, raw_manifest=None, raw_graph=None, extra=None):
    d = fresh()
    write_pack(d, "demo", manifest=mut(mode="companion"))   # a good pack beside it must keep loading
    # the bad one gets its own folder name unless the case sets `name`
    bad_name = name
    write_pack(d, bad_name, manifest=manifest, graph=graph, raw_manifest=raw_manifest, raw_graph=raw_graph)
    if extra:
        extra(os.path.join(d, "packs", bad_name), d)
    try:
        pk, pr = scan(d)
    except Exception as e:                  # noqa: BLE001
        check(label + " (scan raised)", False, repr(e))
        return
    ids = [p["id"] for p in pk]
    line = problem_for(bad_name, pr)
    check(label + ": skipped", not any(p["id"] == bad_name and p.get("user_pack") for p in pk), ids)
    check(label + ": problem line", line is not None and re.search(why_re, line, re.I) is not None, pr)
    check(label + ": the good pack still loads", "demo" in ids)
    check(label + ": built-ins intact", all(i in ids for i in BASE_IDS))

expect_skip("invalid JSON manifest", "bad1", r"not valid JSON", raw_manifest="{ nope")
expect_skip("invalid JSON graph", "bad2", r"not valid JSON", manifest=mut(id="bad2"), raw_graph="[1,")
expect_skip("manifest not an object", "bad3", r"JSON object", raw_manifest="[1,2]")
expect_skip("deeply nested JSON", "bad4", r"not valid JSON", raw_manifest="[" * 200000)
expect_skip("unknown schema version", "bad5", r"schema", manifest=mut(id="bad5", schema=2))
expect_skip("missing schema", "bad5b", r"schema", manifest={k: v for k, v in mut(id="bad5b").items() if k != "schema"})
expect_skip("unknown manifest key", "bad6", r"unknown manifest key", manifest=mut(id="bad6", code="x"))
expect_skip("id differs from folder", "bad7", r"folder name", manifest=mut(id="other"))
expect_skip("id with bad characters", "Bad8", r"id must be", manifest=mut(id="Bad8"))

def with_field(i, **kw):
    m = mut(id="badf")
    m["fields"] = copy.deepcopy(m["fields"])
    m["fields"][i].update(kw)
    return m
def drop_key(i, key):
    m = with_field(i)
    m["fields"][i].pop(key)
    return m

expect_skip("bind to a node not in the graph", "badf", r"node '99'.*not in the graph",
            manifest=with_field(0, bind=[{"node": "99", "input": "text"}]))
expect_skip("bind to an input the node lacks", "badf", r"input 'nope'.*does not have",
            manifest=with_field(0, bind=[{"node": "2", "input": "nope"}]))
expect_skip("bind to class_type", "badf", r"input 'class_type'.*does not have",
            manifest=with_field(0, bind=[{"node": "2", "input": "class_type"}]))
expect_skip("bind over a wired link", "badf", r"wired to another node",
            manifest=with_field(0, bind=[{"node": "2", "input": "clip"}]))
expect_skip("non-string node id in a bind", "badf", r"string node and input",
            manifest=with_field(0, bind=[{"node": 2, "input": "text"}]))
expect_skip("non-string input in a bind", "badf", r"string node and input",
            manifest=with_field(0, bind=[{"node": "2", "input": ["text"]}]))
expect_skip("field without a bind", "badf", r"needs a bind", manifest=drop_key(0, "bind"))
expect_skip("bind is not a list", "badf", r"needs a bind", manifest=with_field(0, bind={"node": "2", "input": "text"}))
expect_skip("unknown key in a bind", "badf", r"bind\[0\] must be", manifest=with_field(0, bind=[{"node": "2", "input": "text", "x": 1}]))
expect_skip("index on a non-list field", "badf", r"index belongs", manifest=with_field(0, bind=[{"node": "2", "input": "text", "index": 0}]))
expect_skip("two fields bind the same input", "badf", r"bound by more than one field",
            manifest=with_field(4, bind=[{"node": "3", "input": "steps"}]))
expect_skip("unknown field type", "badf", r"unknown type", manifest=with_field(0, type="spreadsheet"))
expect_skip("unknown field key", "badf", r"unknown key", manifest=with_field(0, evil="x"))
expect_skip("reserved field id", "badf", r"reserved", manifest=with_field(0, id="quality"))
expect_skip("field id not an identifier", "badf", r"identifier", manifest=with_field(0, id="a b"))
expect_skip("number field without units", "badf", r"needs units", manifest=drop_key(3, "units"))
expect_skip("default of the wrong type (int)", "badf", r"default 'twenty'", manifest=with_field(3, default="twenty"))
expect_skip("default of the wrong type (text)", "badf", r"default 5", manifest=with_field(0, default=5))
expect_skip("default of the wrong type (checkbox)", "badf", r"default 'yes'", manifest=with_field(7, default="yes"))
expect_skip("select default not in options", "badf", r"default 'z'", manifest=with_field(6, default="z"))
expect_skip("select without options", "badf", r"options must be", manifest=drop_key(6, "options"))
expect_skip("ui_range outside range", "badf", r"ui_range must lie inside", manifest=with_field(3, ui_range=[0, 200]))
expect_skip("range malformed", "badf", r"range must be", manifest=with_field(3, range=[5, 1]))
expect_skip("max on a non-list field", "badf", r"max belongs", manifest=with_field(0, max=2))
expect_skip("enabled_when names a missing field", "badf", r"enabled_when", manifest=with_field(7, enabled_when={"field": "ghost", "equals": 1}))
def bad_pool():
    m = with_field(6, type="pool_select", pool="nope", match={"all": ["x"]})
    m["fields"][6].pop("options")
    m["fields"][6].pop("default")
    return m
expect_skip("pool_select with a pool outside the enum", "badf", r"pool 'nope' is not one of", manifest=bad_pool())

def dup_ids():
    m = mut(id="badf")
    m["fields"] = copy.deepcopy(m["fields"])
    m["fields"][1]["id"] = "prompt"
    return m
expect_skip("duplicate field ids", "badf", r"duplicate field id", manifest=dup_ids())

def with_req(i, **kw):
    m = mut(id="badr")
    m["requires"] = copy.deepcopy(m["requires"])
    m["requires"][i].update(kw)
    return m
expect_skip("requires.pool outside the fixed enum", "badr", r"pool 'bogus' is not one of", manifest=with_req(0, pool="bogus"))
expect_skip("requires node not in graph", "badr", r"node '42'", manifest=with_req(0, node="42"))
expect_skip("requires input the node lacks", "badr", r"input 'zzz'", manifest=with_req(0, input="zzz"))
expect_skip("requires a non-string node", "badr", r"string node and input", manifest=with_req(0, node=1))
expect_skip("requires match matches everything", "badr", r"non-empty 'all' or 'any'", manifest=with_req(0, match={"prefer": ["x"]}))
expect_skip("requires match with unknown key", "badr", r"only all/any/none/prefer", manifest=with_req(0, match={"all": ["x"], "regex": ["x"]}))
expect_skip("requires match entries not strings", "badr", r"list of non-empty strings", manifest=with_req(0, match={"all": [1]}))
expect_skip("requires role twice", "badr", r"twice", manifest=with_req(1, role="unet"))
expect_skip("requires role with bad characters", "badr", r"short lower-case identifier", manifest=with_req(0, role="A b"))
expect_skip("requires missing words", "badr", r"words", manifest=with_req(0, words=""))
expect_skip("requires writes an input a field already binds", "badr", r"both a field and a requires",
            manifest=with_req(0, node="3", input="steps"))
expect_skip("licence missing name", "badl", r"licence", manifest=mut(id="badl", licence={"shippable": False}))
expect_skip("licence shippable not a bool", "badl", r"shippable", manifest=mut(id="badl", licence={"name": "x", "shippable": "no"}))

# collisions with built-ins
first_builtin = base_packs[0]
bi_cap, bi_mode = next((p["cap"], m) for p in base_packs for m in p["graphs"])
bi_ability = next(a for p in base_packs for a in p["provides"] if a not in p["graphs"] and a != p["cap"]) \
    if any(a not in p["graphs"] and a != p["cap"] for p in base_packs for a in p["provides"]) else None
expect_skip("id equal to a built-in id", first_builtin["id"], r"already used", manifest=mut(id=first_builtin["id"]))
expect_skip("(cap, mode) equal to a built-in mode", "badc", r"already provided",
            manifest=mut(id="badc", cap=bi_cap, mode=bi_mode))
expect_skip("mode equal to a capability name", "badc", r"clashes", manifest=mut(id="badc", mode="video"))
if bi_ability:
    expect_skip("mode equal to a built-in ability name", "badc", r"clashes", manifest=mut(id="badc", mode=bi_ability))
# two data packs claiming the same (cap, mode): first (by folder name) wins
dd = fresh()
write_pack(dd, "demo")
write_pack(dd, "demo2", manifest=mut(id="demo2"))
pk, pr = scan(dd)
check("second data pack with the same (cap, mode) is skipped", problem_for("demo2", pr) is not None and "demo2" not in [p["id"] for p in pk]
      and "demo" in [p["id"] for p in pk], pr)

# graph file problems
m_nog = mut(id="nog", graph="gone.json")
expect_skip("missing graph file", "nog", r"missing", manifest=m_nog)
expect_skip("graph is not an API graph", "nog2", r"non-empty object|class_type", manifest=mut(id="nog2"), raw_graph='{"nodes": []}')
expect_skip("graph node without inputs", "nog3", r"class_type string and an inputs object", manifest=mut(id="nog3"),
            graph={"1": {"class_type": "X"}})
expect_skip("graph path not a string", "nog4", r"non-empty string", manifest=mut(id="nog4", graph=7))
expect_skip("empty graph path", "nog4b", r"non-empty string", manifest=mut(id="nog4b", graph=""))

# path safety
def outside_file(content=None):
    out = os.path.join(WORK, "outside-%d.json" % len(os.listdir(WORK)))
    with open(out, "w") as f:
        json.dump(content if content is not None else GRAPH, f)
    return out

out1 = outside_file()
expect_skip("graph path with ..", "esc1", r"leaves the pack folder",
            manifest=mut(id="esc1", graph="../" * 20 + out1.lstrip("/")))
expect_skip("graph absolute path", "esc2", r"relative to the pack folder", manifest=mut(id="esc2", graph=out1))
def link_graph(pack_dir, data_dir):
    os.remove(os.path.join(pack_dir, "graph.api.json"))
    os.symlink(out1, os.path.join(pack_dir, "graph.api.json"))
expect_skip("graph symlinked outside the folder", "esc3", r"leaves the pack folder", manifest=mut(id="esc3"), extra=link_graph)
def link_manifest(pack_dir, data_dir):
    outm = outside_file(mut(id="esc4"))
    os.remove(os.path.join(pack_dir, "manifest.json"))
    os.symlink(outm, os.path.join(pack_dir, "manifest.json"))
expect_skip("manifest symlinked outside the folder", "esc4", r"leaves the pack folder", manifest=mut(id="esc4"), extra=link_manifest)
def link_folder(pack_dir, data_dir):
    real = os.path.join(data_dir, "elsewhere")
    shutil.move(pack_dir, real)
    os.symlink(real, pack_dir)
expect_skip("pack folder symlinked outside packs/", "esc5", r"outside the packs folder", manifest=mut(id="esc5"), extra=link_folder)
def link_inside(pack_dir, data_dir):
    shutil.copy(os.path.join(pack_dir, "graph.api.json"), os.path.join(pack_dir, "real.json"))
    os.remove(os.path.join(pack_dir, "graph.api.json"))
    os.symlink(os.path.join(pack_dir, "real.json"), os.path.join(pack_dir, "graph.api.json"))
d = fresh(); write_pack(d, "inl", manifest=mut(id="inl")); link_inside(os.path.join(d, "packs", "inl"), d)
pk, pr = scan(d)
check("a symlink that stays inside the folder is fine", "inl" in [p["id"] for p in pk] and pr == [], pr)

# size caps (2 MB each)
cap_bytes = 2 * 1024 * 1024
big = '{"1": {"class_type": "X", "inputs": {"a": "%s"}}}' % ("a" * (cap_bytes + 10))
expect_skip("graph over the 2 MB cap", "big1", r"byte limit", manifest=mut(id="big1"), raw_graph=big)
bigm = json.dumps(mut(id="big2", comment="b" * (cap_bytes + 10)))
expect_skip("manifest over the 2 MB cap", "big2", r"byte limit", raw_manifest=bigm)
under = json.dumps(mut(id="ok2", comment="c" * (cap_bytes // 2)))
d = fresh(); write_pack(d, "ok2", raw_manifest=under)
pk, pr = scan(d)
check("a manifest under the cap loads", "ok2" in [p["id"] for p in pk], pr)

# adversarial-pass defects: non-finite numbers, deep nesting, newline names, BOM
print("hardening")
expect_skip("NaN default", "nan1", r"not valid JSON", raw_manifest=json.dumps(with_field(4, default=float("nan"))).replace('"id": "badf"', '"id": "nan1"'))
expect_skip("Infinity range", "inf1", r"not valid JSON", raw_manifest=json.dumps(with_field(3, range=[float("-inf"), float("inf")])).replace('"id": "badf"', '"id": "inf1"'))
expect_skip("Infinity token in the graph", "inf2", r"not valid JSON", manifest=mut(id="inf2"),
            raw_graph=json.dumps(GRAPH).replace('"strength": 1.0', '"strength": Infinity'))
check("a finite number check rejects nan/inf directly", not up._num(float("nan")) and not up._num(float("inf")) and up._num(1.5))
deep = "[" * 900 + "]" * 900
g_deep = copy.deepcopy(GRAPH); g_deep["2"]["inputs"]["deep"] = json.loads(deep)
expect_skip("graph nested 900 deep", "deep1", r"nested deeper", manifest=mut(id="deep1"), raw_graph=json.dumps(g_deep))
expect_skip("manifest nested 900 deep", "deep2", r"nested deeper", raw_manifest=json.dumps(dict(mut(id="deep2"), comment=json.loads(deep))))
g_ok = copy.deepcopy(GRAPH); g_ok["2"]["inputs"]["deep"] = json.loads("[" * 20 + "]" * 20)
d = fresh(); write_pack(d, "deepok", manifest=mut(id="deepok"), graph=g_ok)
pk, pr = scan(d)
check("nesting within the limit is fine", "deepok" in [p["id"] for p in pk], pr)
expect_skip("mode with a trailing newline", "nl1", r"mode must be", manifest=mut(id="nl1", mode="newmode\n"))
expect_skip("cap with a trailing newline", "nl3", r"cap must be", manifest=mut(id="nl3", cap="video\n"))
expect_skip("id and folder with a trailing newline", "nl2\n", r"id must be", manifest=mut(id="nl2\n"))
expect_skip("field id with a trailing newline", "badf", r"identifier", manifest=with_field(0, id="prompt\n"))
expect_skip("role with a trailing newline", "badr", r"short lower-case identifier", manifest=with_req(0, role="unet\n"))
d = fresh()
pd = os.path.join(d, "packs", "bom1"); os.makedirs(pd)
open(os.path.join(pd, "manifest.json"), "wb").write(b"\xef\xbb\xbf" + json.dumps(mut(id="bom1")).encode())
open(os.path.join(pd, "graph.api.json"), "wb").write(b"\xef\xbb\xbf" + json.dumps(GRAPH).encode())
pk, pr = scan(d)
check("a UTF-8 BOM in manifest and graph is accepted", "bom1" in [p["id"] for p in pk] and pr == [], pr)
# a bound value can only be data: never a list (a link) or an object
scan(good)
for bad_args, what in (({"clip": ["c.mp4"], "pic": ["3", 0]}, "a list in a picture field"),
                       ({"clip": ["c.mp4"], "prompt": {"a": 1}}, "an object in a text field"),
                       ({"clip": ["c.mp4"], "steps": ["3", 0]}, "a list in a number field"),
                       ({"clip": [["3", 0]]}, "a link as a list item"),
                       ({"clip": ["c.mp4"], "strength": float("nan")}, "NaN in a number field")):
    try:
        engines.graph_for("video", "demomode", bad_args, MODELS)
        check("%s is refused" % what, False)
    except ValueError:
        check("%s is refused" % what, True)
check("plain scalars still pass", engines.graph_for("video", "demomode", {"clip": ["c.mp4"], "extra": True, "steps": 3, "strength": 0.5}, MODELS)["3"]["inputs"]["on"] is True)

# presets: the page's recipe row needs a non-empty list to refresh its summary
print("presets")
scan(good)
DEFAULT_PRESET = [{"id": "default", "label": "Default", "note": "As set up in this pack.", "values": {}}]
check("a pack with no presets gets one 'Default' preset", engines.presets("video", "demomode") == DEFAULT_PRESET, engines.presets("video", "demomode"))
check("quality and examples stay empty", engines.quality("video", "demomode") == [] and engines.examples("video", "demomode") == [])
m_pre = mut(id="pre1", mode="premode", presets=[
    {"id": "quick", "label": "Quick", "note": "Fewer steps.", "values": {"steps": 8, "sampler": "b", "extra": True}},
    {"id": "plain", "label": "Plain", "values": {}}])
d = fresh(); write_pack(d, "pre1", manifest=m_pre)
pk, pr = scan(d)
check("declared presets are used as written (note defaults to empty)", pr == [] and engines.presets("video", "premode") ==
      [{"id": "quick", "label": "Quick", "note": "Fewer steps.", "values": {"steps": 8, "sampler": "b", "extra": True}},
       {"id": "plain", "label": "Plain", "note": "", "values": {}}], (pr, engines.presets("video", "premode")))
d = fresh(); write_pack(d, "pre2", manifest=mut(id="pre2", mode="premode2", presets=[]))
pk, pr = scan(d)
check("an empty presets list means 'none declared' (default synthesized)", engines.presets("video", "premode2") == DEFAULT_PRESET, pr)
def with_presets(pid, presets):
    return mut(id=pid, presets=presets)
P = lambda **kw: dict({"id": "a", "label": "A", "values": {}}, **kw)
expect_skip("presets not a list", "pbad", r"presets must be a list", manifest=with_presets("pbad", {"id": "a"}))
expect_skip("preset not an object", "pbad", r"presets\[0\]", manifest=with_presets("pbad", ["x"]))
expect_skip("preset with an unknown key", "pbad", r"unknown key", manifest=with_presets("pbad", [P(evil=1)]))
expect_skip("preset id repeated", "pbad", r"twice", manifest=with_presets("pbad", [P(), P()]))
expect_skip("preset without a label", "pbad", r"label", manifest=with_presets("pbad", [P(label="")]))
expect_skip("preset values name a missing field", "pbad", r"no field .ghost.", manifest=with_presets("pbad", [P(values={"ghost": 1})]))
expect_skip("preset value of the wrong type", "pbad", r"does not fit", manifest=with_presets("pbad", [P(values={"steps": "many"})]))
expect_skip("preset value outside a select's options", "pbad", r"does not fit", manifest=with_presets("pbad", [P(values={"sampler": "zzz"})]))
expect_skip("preset value on an upload field", "pbad", r"upload", manifest=with_presets("pbad", [P(values={"pic": "x.png"})]))
expect_skip("preset id not an identifier", "pbad", r"identifier", manifest=with_presets("pbad", [P(id="a b")]))
# a room named in the manifest: joining an existing room avoids a one-mode room named like its only mode
d = fresh(); write_pack(d, "rm1", manifest=mut(id="rm1", mode="roommode", room="video"))
pk, pr = scan(d)
vr = next(r for r in engines.rooms() if r["id"] == "video")
check("a manifest room that exists adds the mode to it (no duplicate one-mode room)",
      {"cap": "video", "mode": "roommode"} in vr["modes"] and not any(r["id"] == "video-roommode" for r in engines.rooms()), pr)

# folders that are not packs
d = fresh()
os.makedirs(os.path.join(d, "packs", "empty-folder"))
os.makedirs(os.path.join(d, "packs", ".hidden"))
os.makedirs(os.path.join(d, "packs", "_draft"))
with open(os.path.join(d, "packs", "README.txt"), "w") as f:
    f.write("notes")
pk, pr = scan(d)
check("folder without a manifest is a problem, hidden/_ folders and files are ignored",
      problem_for("empty-folder", pr) is not None and len(pr) == 1, pr)
check("the scan still returns every built-in", [p["id"] for p in pk] == BASE_IDS)

# a loader bug can never kill the scan
real_load = up.load_user_packs
def boom(*a, **k):
    raise RuntimeError("loader exploded")
up.load_user_packs = boom
try:
    pk, pr = scan(good)
    check("an exception inside the loader is contained", [p["id"] for p in pk] == BASE_IDS and any("exploded" in p for p in pr), pr)
finally:
    up.load_user_packs = real_load

# -- 3. trust boundary: only DATA_DIR/packs, only at scan time ----------------------
print("trust boundary")
src = open(os.path.join(ROOT, "engines", "_userpacks.py")).read() + open(os.path.join(ROOT, "engines", "__init__.py")).read()
check("loader takes the data dir from GENCENTER_DATA (server.py's own rule)", "GENCENTER_DATA" in open(os.path.join(ROOT, "engines", "__init__.py")).read())
check("no network, eval or exec in the loader",
      not re.search(r"\b(eval|exec|subprocess|urllib|socket|__import__|requests)\b", open(os.path.join(ROOT, "engines", "_userpacks.py")).read().split('"""', 2)[2]))
server_src = open(os.path.join(ROOT, "server.py")).read()
check("server.py has no reload / upload hook for packs", "user_pack" not in server_src and "_userpacks" not in server_src)
scan(empty)
write_pack(empty, "late", manifest=mut(id="late"))
check("a pack added after the scan is not picked up until the next start",
      "late" not in [p["id"] for p in engines.packs()])

# the fixed pool list tracks server.py's POOL_NODES
m = re.search(r"POOL_NODES = \{(.*?)\n\}", server_src, re.S)
server_pools = set(re.findall(r'^\s*"(\w+)":\s*\[', m.group(1), re.M)) if m else set()
check("POOLS equals server.py POOL_NODES", server_pools and server_pools == set(up.POOLS), (sorted(server_pools), sorted(up.POOLS)))
check("RESERVED_IDS + seed equals server.py RESERVED_FIELD_IDS",
      set(re.search(r"RESERVED_FIELD_IDS = \{(.*?)\}", server_src).group(1).replace('"', "").replace(" ", "").split(",")) ==
      set(up.RESERVED_IDS) | {"seed"})

# back to the baseline: nothing leaks between scans
pk, pr = scan(fresh())
check("an empty data dir after all of this restores the baseline",
      [p["id"] for p in pk] == BASE_IDS and {c: engines.modes_for(c) for c in engines.caps()} == BASE_MODES
      and dict(engines.role_pool()) == BASE_ROLES)

# -- 4. through the real generic dispatch (server.Handler.api_generate) ---------------
print("generic dispatch end to end")
import importlib.util
import _scratch_config  # noqa: E402,F401 -- points GENCENTER_CONFIG at a scratch config before server.py loads
_spec = importlib.util.spec_from_file_location("srv_userpacks", os.path.join(ROOT, "server.py"))
srv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(srv)
scan(good)                                   # server.py shares this engines module; rescan with the demo pack
LANE = {"id": "up", "name": "Up", "box": "b", "note": "", "host": "127.0.0.1", "port": 1, "caps": ["video"]}
srv.LANE_BY_ID[LANE["id"]] = LANE
if LANE not in srv.LANES:
    srv.LANES.append(LANE)
with srv.STATE_LOCK:
    srv.LANE_STATE[LANE["id"]] = {"up": True}
def set_models(models):
    with srv.DISCOVERY_LOCK:
        srv.DISCOVERY[LANE["id"]] = {"models": models, "pools": {}, "checked": 1.0, "err": ""}
captured = []
def fake_dispatch(lane, graph, kind, mode, meta):
    captured.append({"graph": graph, "kind": kind, "mode": mode, "meta": meta})
    return {"ok": True, "job": {"id": "fake"}, "notes": []}
srv.dispatch = fake_dispatch
def call(body):
    obj = srv.Handler.__new__(srv.Handler)
    obj.read_json = lambda: body
    res = []
    obj.send_json = lambda payload, code=200: res.append((payload, code))
    srv.Handler.api_generate(obj)
    return res[-1]

set_models(dict(MODELS))
captured.clear()
payload, code = call({"lane": "up", "kind": "video", "mode": "demomode", "prompt": "ignored top-level",
                      "clip": ["u1.mp4"], "pic": "u.png", "steps": "12", "seed": 77, "strength": "0.5"})
check("data-pack mode dispatches (200)", code == 200 and payload.get("ok") is True, (payload, code))
gr = captured[-1]["graph"] if captured else {}
check("dispatch uses the pack's own mode", captured and captured[-1]["mode"] == "demomode")
check("clip, picture and the coerced ints/floats reach the graph",
      gr.get("5", {}).get("inputs", {}).get("file") == "u1.mp4" and gr["4"]["inputs"]["image"] == "u.png"
      and gr["3"]["inputs"]["steps"] == 12 and gr["3"]["inputs"]["strength"] == 0.5, gr)
check("the request's seed reaches the bound seed input", gr["3"]["inputs"]["seed"] == 77)
check("resolved model files reach the graph", gr["1"]["inputs"]["model_name"] == MODELS[U_UNET]
      and gr["6"]["inputs"]["lora_name"] == MODELS[U_LORA])
check("a seed not in the request is generated, not the template's 1",
      call({"lane": "up", "kind": "video", "mode": "demomode", "clip": ["u1.mp4"]})[1] == 200
      and captured[-1]["graph"]["3"]["inputs"]["seed"] not in (1, None))
payload, code = call({"lane": "up", "kind": "video", "mode": "demomode"})
check("a required field left out is a plain 400 naming it", code == 400 and "Clip is needed" in payload.get("error", ""), (payload, code))
payload, code = call({"lane": "up", "kind": "video", "mode": "demomode", "clip": ["u1.mp4"], "steps": ""})
check("a bad number is a plain 400", code == 400 and "Steps needs a whole number" in payload.get("error", ""), (payload, code))
payload, code = call({"lane": "up", "kind": "video", "mode": "demomode", "clip": ["u1.mp4"], "sampler": "zzz"})
check("a select outside its options is a plain 400", code == 400 and "Sampler must be one of" in payload.get("error", ""), (payload, code))
payload, code = call({"lane": "up", "kind": "video", "mode": "demomode", "clip": ["u1.mp4"], "pic": ["3", 0]})
check("a link smuggled through a picture field is a plain 400", code == 400 and "Picture" in payload.get("error", ""), (payload, code))
set_models({U_UNET: MODELS[U_UNET]})
payload, code = call({"lane": "up", "kind": "video", "mode": "demomode", "clip": ["u1.mp4"]})
check("a lane missing a required model refuses and names it",
      code == 400 and "the demo adapter" in payload.get("error", ""), (payload, code))

shutil.rmtree(WORK, ignore_errors=True)
print()
if FAILED:
    print("FAILED: %d" % len(FAILED))
    for n in FAILED:
        print("  -", n)
    sys.exit(1)
print("all passed")
