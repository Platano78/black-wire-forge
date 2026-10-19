"""Add a ComfyUI template as a data pack ("Add as a mode"): the part that needs no network.

A template is converted to ComfyUI's API format by the lane's own page (engines/_autopack_convert.py)
or by the user's "Export (API)"; this module turns that graph into a DRAFT data pack
(manifest + graph, the shape engines/_userpacks.py reads), says what it could not work out
as typed flags, checks the draft against a lane, and keeps it through draft -> reviewed ->
tested -> enabled. Lane reads are done by the caller and passed in as plain data
(an /object_info, the lane's pools); nothing here opens a socket.

  validator    validate_api_graph / prune_to_outputs     (spec section 3)
  synthesis    synthesize                                (section 4)
  readiness    readiness / build_test_graph              (sections 6, 8)
  store        create_draft, read_draft, edit, answer, ack, set_licence, review,
               record_test, verdict, enable, disable, delete   (section 7)

Every refusal is a Refused whose .sentence is plain English, shown verbatim.
Nothing here names a model or a vendor; nothing enables a pack except enable().
"""

import copy
import hashlib
import json
import os
import re
import shutil
import tempfile
import time

from . import _comfy
from . import _userpacks as up

MODEL_EXTS = (".safetensors", ".gguf", ".ckpt", ".pt", ".pth", ".bin", ".sft", ".onnx", ".pkl")
# Inputs that take a file the user uploads at run time: never checked against a dropdown.
UPLOAD_PICKERS = {("LoadImage", "image"), ("LoadImageMask", "image"), ("LoadVideo", "file"), ("LoadAudio", "audio")}

PRIM = {"PrimitiveString", "PrimitiveStringMultiline", "PrimitiveInt", "PrimitiveFloat", "PrimitiveBoolean"}
PREVIEW = {"PreviewAny", "PreviewImage", "ImageCompare", "MaskPreview", "Preview3DAdvanced", "PreviewAudio"}
TEXT_POS = ["text", "prompt", "positive_prompt", "text_g", "caption", "instruction", "user_prompt"]
TEXT_NEG = ["negative_prompt", "neg_prompt", "negative"]
UI_ONLY_NODES = {"MarkdownNote", "Note", "Reroute", "PrimitiveNode"}
RESERVED = up.RESERVED_IDS

# (loader class, input) -> BWF pool. Folder-based pools, so one pool serves every dropdown
# that lists the same folder (the three LTX loaders share the clip / checkpoint lists).
POOL_OF = {}
for _c, _i in [("UNETLoader", "unet_name"), ("UnetLoaderGGUF", "unet_name")]:
    POOL_OF[(_c, _i)] = "unet"
for _c, _i in [("CLIPLoader", "clip_name"), ("DualCLIPLoader", "clip_name1"), ("DualCLIPLoader", "clip_name2"),
               ("TripleCLIPLoader", "clip_name1"), ("TripleCLIPLoader", "clip_name2"), ("TripleCLIPLoader", "clip_name3"),
               ("QuadrupleCLIPLoader", "clip_name1"), ("QuadrupleCLIPLoader", "clip_name2"),
               ("QuadrupleCLIPLoader", "clip_name3"), ("QuadrupleCLIPLoader", "clip_name4"),
               ("LTXAVTextEncoderLoader", "text_encoder")]:
    POOL_OF[(_c, _i)] = "clip"
POOL_OF[("VAELoader", "vae_name")] = "vae"
POOL_OF[("LoraLoaderModelOnly", "lora_name")] = "lora"
POOL_OF[("LoraLoader", "lora_name")] = "lora"
for _c, _i in [("CheckpointLoaderSimple", "ckpt_name"), ("ImageOnlyCheckpointLoader", "ckpt_name"),
               ("LTXAVTextEncoderLoader", "ckpt_name"), ("LTXVAudioVAELoader", "ckpt_name")]:
    POOL_OF[(_c, _i)] = "checkpoint"
POOL_OF[("AudioEncoderLoader", "audio_encoder_name")] = "audio_encoder"
POOL_OF[("LoadBackgroundRemovalModel", "bg_removal_name")] = "bg_removal"
POOL_OF[("UpscaleModelLoader", "model_name")] = "upscale_model"
POOL_OF[("CLIPVisionLoader", "clip_name")] = "clip_vision"
POOL_OF[("LatentUpscaleModelLoader", "model_name")] = "latent_upscaler"

# Tokens that say HOW a file is stored, not WHICH model it is.
DTYPE = {"fp16", "bf16", "fp32", "fp8", "int8", "int4", "nvfp4", "w4a8", "awq", "convrot", "e4m3fn", "e5m2", "scaled",
         "comfyui", "safetensors", "pruned", "gguf", "sft", "q8", "q6", "q5", "q4", "float16", "float32", "ema"}

PAID = "This template runs on a paid online service, so it cannot become a mode here."
NO_FILE = "This template makes no file BWF could keep (it only previews)."
_PICTURE_MENTION = re.compile(r"(?i)<?(picture|image|ref_image)[ _]?(\d+)>?")


class Refused(Exception):
    """A refusal. `.sentence` is plain English and is shown as it stands."""

    def __init__(self, sentence):
        super().__init__(sentence)
        self.sentence = sentence


# ----------------------------------------------------------------------------- helpers

def is_link(v):
    return isinstance(v, list) and len(v) == 2 and isinstance(v[0], str)


def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def sha(obj):
    return hashlib.sha256(canon(obj).encode("utf-8")).hexdigest()


def hashes(manifest, graph):
    return sha(manifest), sha(graph)


def slug(s, sep):
    return re.sub(r"[^a-z0-9]+", sep, (s or "").lower()).strip(sep)


def _base(path):
    return str(path).replace("\\", "/").rsplit("/", 1)[-1]


def _numeric_id(k):
    return [int(x) if x.isdigit() else 0 for x in k.split(":")]


def _specs(oi, cls):
    inp = (oi.get(cls) or {}).get("input") or {}
    return {**(inp.get("required") or {}), **(inp.get("optional") or {}), **(inp.get("hidden") or {})}, inp.get("required") or {}


def _enum_options(spec):
    """The options of a dropdown spec in either shape, or None when the spec is not a dropdown."""
    if not isinstance(spec, list) or not spec:
        return None
    if isinstance(spec[0], list):
        return spec[0]
    if spec[0] == "COMBO" and len(spec) > 1 and isinstance(spec[1], dict):
        o = spec[1].get("options")
        return o if isinstance(o, list) and not spec[1].get("remote") else None
    if isinstance(spec[0], str) and len(spec) >= 2 and all(isinstance(x, str) for x in spec):
        return list(spec)
    return None


def _dynamic(spec):
    return isinstance(spec, list) and bool(spec) and isinstance(spec[0], str) and spec[0].startswith("COMFY_")


def _autogrow_names(spec):
    if not (isinstance(spec, list) and len(spec) > 1 and spec[0] == "COMFY_AUTOGROW_V3" and isinstance(spec[1], dict)):
        return None
    t = spec[1].get("template")
    if not isinstance(t, dict):
        return None
    if "names" in t:
        return list(t["names"])
    if "prefix" in t:
        return ["%s%d" % (t["prefix"], i) for i in range(int(t.get("max", 0)))]
    return None


def _combo_child(specs, inputs, name):
    """Spec of a dotted child ("format.quality") of a dynamic combo, looked up in the option
    its parent chose; (spec or None, parent_is_dynamic_combo)."""
    parent, sep, rest = name.partition(".")
    p = specs.get(parent) if sep else None
    if not (p and isinstance(p, list) and p[0] == "COMFY_DYNAMICCOMBO_V3" and len(p) > 1 and isinstance(p[1], dict)):
        return None, False
    for opt in p[1].get("options") or []:
        if isinstance(opt, dict) and opt.get("key") == inputs.get(parent):
            sub = {**((opt.get("inputs") or {}).get("required") or {}), **((opt.get("inputs") or {}).get("optional") or {})}
            return sub.get(rest), True
    return None, True


# ----------------------------------------------------------------------------- validator (section 3)

def prune_to_outputs(graph, oi):
    """Only the nodes an output node needs (what the backend executes). -> (graph, has_output)."""
    outs = [k for k, n in graph.items() if (oi.get(n.get("class_type")) or {}).get("output_node")]
    if not outs:
        return graph, False
    keep, stack = set(), list(outs)
    while stack:
        k = stack.pop()
        if k in keep or k not in graph:
            continue
        keep.add(k)
        for v in (graph[k].get("inputs") or {}).values():
            if is_link(v):
                stack.append(v[0])
    return {k: graph[k] for k in graph if k in keep}, True


def validate_api_graph(graph, oi):
    """Check an API-format graph against one lane's /object_info, on the part of it an output
    needs. -> {unknown_classes, billed, missing_files [{node,class,input,value}], errors, warnings}.

    A model file outside a dropdown (either shape: the old [[...]] and the V3 ["COMBO", {...}])
    is `missing_files`; any other value outside one, a missing required input, a broken link or
    a bad literal is an `errors` sentence. A literal INT written as a string is only a warning
    (the backend coerces it)."""
    res = {"unknown_classes": [], "billed": [], "missing_files": [], "errors": [], "warnings": []}
    g, has_out = prune_to_outputs(graph, oi)
    if not has_out:
        res["errors"].append("The graph has no output node.")
    out_count = {k: len((oi.get(n.get("class_type")) or {}).get("output") or []) for k, n in g.items() if n.get("class_type") in oi}
    for nid, node in g.items():
        cls = node.get("class_type")
        if cls not in oi:
            if cls not in res["unknown_classes"]:
                res["unknown_classes"].append(cls)
            continue
        if oi[cls].get("api_node") and cls not in res["billed"]:
            res["billed"].append(cls)
        specs, required = _specs(oi, cls)
        inputs = node.get("inputs") or {}
        where = "Node %s (%s)" % (nid, cls)
        for name in inputs:
            if name in specs:
                continue
            field, sep, slot = name.partition(".")
            if sep and field in specs:
                names = _autogrow_names(specs[field])
                if names is not None and slot in names:
                    continue
                child, dyn = _combo_child(specs, inputs, name)
                if child is not None:
                    continue
            res["warnings"].append("%s: unknown input '%s'" % (where, name))
        for req, spec in required.items():
            if req in inputs or any(k.startswith(req + ".") for k in inputs) or _dynamic(spec):
                continue
            res["errors"].append("%s: missing required input '%s'" % (where, req))
        for name, v in inputs.items():
            if is_link(v):
                if v[0] not in g:
                    res["errors"].append("%s: link [%r, %s] references non-existent node '%s'" % (where, v[0], v[1], v[0]))
                elif v[0] in out_count and (not isinstance(v[1], int) or v[1] < 0 or v[1] >= out_count[v[0]]):
                    res["errors"].append("%s: link [%r, %s] slot %s out of range (node %s has %d outputs)"
                                         % (where, v[0], v[1], v[1], v[0], out_count[v[0]]))
                continue
            spec = specs.get(name)
            if spec is None:
                spec, _dyn = _combo_child(specs, inputs, name)
            if spec is None or (cls, name) in UPLOAD_PICKERS or (cls == "CustomCombo" and name == "choice"):
                continue
            opts = _enum_options(spec)
            if opts is not None:
                if v not in opts:
                    if isinstance(v, str) and v.lower().endswith(MODEL_EXTS):
                        res["missing_files"].append({"node": nid, "class": cls, "input": name, "value": v})
                    else:
                        res["errors"].append("%s: input '%s' value %r is not one of its %d options" % (where, name, v, len(opts)))
                continue
            if isinstance(spec, list) and spec and spec[0] == "COMFY_DYNAMICCOMBO_V3" and len(spec) > 1 and isinstance(spec[1], dict):
                keys = [o.get("key") for o in spec[1].get("options") or [] if isinstance(o, dict)]
                if v not in keys:
                    res["errors"].append("%s: input '%s' value %r is not one of its options" % (where, name, v))
                continue
            _check_literal(res, where, name, v, spec)
    return res


def _check_literal(res, where, name, v, spec):
    if not (isinstance(spec, list) and spec and isinstance(spec[0], str)):
        return
    t, o = spec[0], (spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {})
    if t in ("INT", "FLOAT"):
        if isinstance(v, str) and re.fullmatch(r"-?\d+(\.\d+)?", v.strip() or "x"):
            res["warnings"].append("%s: %s '%s' is written as the string %r (the backend converts it)" % (where, t, name, v))
            return
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            res["errors"].append("%s: %s '%s' has the value %r, which is not a number" % (where, t, name, v))
        elif t == "INT" and isinstance(v, float) and v != int(v):
            res["errors"].append("%s: INT '%s' has the fractional value %r" % (where, name, v))
        elif "min" in o and v < o["min"]:
            res["errors"].append("%s: '%s' is %r, below its minimum %r" % (where, name, v, o["min"]))
        elif "max" in o and v > o["max"]:
            res["errors"].append("%s: '%s' is %r, above its maximum %r" % (where, name, v, o["max"]))
    elif t == "BOOLEAN" and not isinstance(v, bool):
        res["errors"].append("%s: BOOLEAN '%s' has the value %r" % (where, name, v))
    elif t == "STRING" and not isinstance(v, str):
        res["errors"].append("%s: STRING '%s' has the value %r" % (where, name, v))


# ----------------------------------------------------------------------------- template readers

def template_promoted(ui):
    """The widgets a template's author put on a subgraph's face (properties.proxyWidgets), as
    ("<instance id>:<inner id>", widget name), in the template's own order."""
    out = []
    if not isinstance(ui, dict):
        return out
    subs = {s.get("id") for s in ((ui.get("definitions") or {}).get("subgraphs") or []) if isinstance(s, dict)}
    for nd in ui.get("nodes") or []:
        if isinstance(nd, dict) and nd.get("type") in subs:
            for item in (nd.get("properties") or {}).get("proxyWidgets") or []:
                if isinstance(item, (list, tuple)) and len(item) == 2:
                    out.append(("%s:%s" % (nd.get("id"), item[0]), item[1]))
    return out


def template_model_sources(ui):
    """basename -> {directory, url} from the template's own `properties.models` (top level and subgraphs)."""
    out = {}
    if not isinstance(ui, dict):
        return out
    graphs = [ui] + [s for s in ((ui.get("definitions") or {}).get("subgraphs") or []) if isinstance(s, dict)]
    for gr in graphs:
        for nd in gr.get("nodes") or []:
            props = nd.get("properties") if isinstance(nd, dict) and isinstance(nd.get("properties"), dict) else {}
            for m in props.get("models") or []:
                if isinstance(m, dict) and isinstance(m.get("name"), str) and m["name"]:
                    out.setdefault(_base(m["name"]), {"directory": m.get("directory") if isinstance(m.get("directory"), str) else None,
                                                      "url": m.get("url") if isinstance(m.get("url"), str) else None})
    return out


def template_node_types(ui):
    """The node classes a template uses (a subgraph's own id and UI-only nodes are not classes)."""
    types = set()
    if not isinstance(ui, dict):
        return types
    subs = [s for s in ((ui.get("definitions") or {}).get("subgraphs") or []) if isinstance(s, dict)]
    sub_ids = {s.get("id") for s in subs}
    for gr in [ui] + subs:
        for nd in gr.get("nodes") or []:
            t = nd.get("type") if isinstance(nd, dict) else None
            if isinstance(t, str) and t and t not in sub_ids and t not in UI_ONLY_NODES:
                types.add(t)
    return types


def index_entries(index):
    """A lane's /templates/index.json -> name -> {name,title,description,models,local,category,type}."""
    out = {}
    for cat in index if isinstance(index, list) else []:
        if not isinstance(cat, dict):
            continue
        for t in cat.get("templates") if isinstance(cat.get("templates"), list) else []:
            name = t.get("name") if isinstance(t, dict) else None
            if isinstance(name, str) and name and name not in out:
                out[name] = {"name": name, "title": str(t.get("title") or name)[:200],
                             "description": str(t.get("description") or "")[:1000],
                             "models": [str(x)[:100] for x in (t.get("models") or []) if isinstance(x, str)][:12],
                             "local": t.get("openSource") is not False,
                             "category": str(cat.get("title") or "")[:80], "type": str(cat.get("type") or "")}
    return out


# ----------------------------------------------------------------------------- taken

def taken_for(data_dir, builtin=(), exclude=None):
    """What a new mode must not collide with, in the shape _userpacks.load_user_packs builds:
    the ids, (cap, mode) pairs, ability and capability names, model roles of `builtin` (the
    engines that exist now), minus the engine whose id is `exclude`; plus, when data_dir is
    given, the ids and modes of the drafts there (minus the draft `exclude`)."""
    packs = [p for p in builtin if p.get("id") != exclude]
    taken = {"ids": {p.get("id") for p in packs},
             "modes": {(p.get("cap"), m) for p in packs for m in (p.get("graphs") or {})},
             "abilities": {a for p in packs for a in (p.get("provides") or {})},
             "caps": {p.get("cap") for p in packs},
             "roles": {r for p in packs for r in (p.get("roles") or {})}}
    root = os.path.join(data_dir, "packs-draft") if data_dir else None
    if root and os.path.isdir(root):
        for name in sorted(os.listdir(root)):
            if name.startswith(".") or name == exclude:
                continue
            try:
                with open(os.path.join(root, name, "manifest.json"), encoding="utf-8") as f:
                    m = json.load(f)
                taken["ids"].add(name)
                if isinstance(m.get("mode"), str):
                    taken["modes"].add((m.get("cap"), m["mode"]))
                    taken["abilities"].add(m["mode"])
            except (OSError, ValueError):
                continue
    return taken


def _unique(base, used, sep):
    if base not in used:
        return base
    n = 2
    while "%s%s%d" % (base, sep, n) in used:
        n += 1
    return "%s%s%d" % (base, sep, n)


def _flag(fid, kind, text, **data):
    return {"id": fid, "kind": kind, "text": text, "data": data}


# ----------------------------------------------------------------------------- synthesis (section 4)

class _Synth:
    def __init__(self, g, oi, promoted, pools, small_card, lane_name):
        self.g, self.oi, self.promoted, self.pools = g, oi, list(promoted), pools
        self.small_card, self.lane_name = small_card, lane_name
        self.used = set()
        self.fields, self.requires, self.flags = [], [], []
        self.candidates = {}

    # -- graph reading
    def spec(self, nid, inp):
        specs, _ = _specs(self.oi, self.g[nid]["class_type"])
        if inp in specs:
            return specs[inp]
        child, _dyn = _combo_child(specs, self.g[nid]["inputs"], inp)
        return child

    def out_type(self, nid, slot):
        o = (self.oi.get(self.g[nid]["class_type"]) or {}).get("output") or []
        try:
            return o[slot] if isinstance(o[slot], str) else "COMBO"
        except (IndexError, TypeError):
            return None

    def title(self, nid):
        t = (self.g[nid].get("_meta") or {}).get("title", "")
        d = (self.oi.get(self.g[nid]["class_type"]) or {}).get("display_name", "")
        return t if t and t != d else ""

    def lit(self, nid, inp):
        return self.g[nid]["inputs"].get(inp)

    def terminals(self, nid, inp, depth=0):
        """Follow a possibly-linked input back to the literal that supplies it.
        -> ([(node, input)], derived, [switch terminals])"""
        v = self.g[nid]["inputs"].get(inp)
        if not is_link(v):
            return [(nid, inp)], False, []
        if depth > 12 or v[0] not in self.g:
            return [], True, []
        src = v[0]
        sc = self.g[src]["class_type"]
        if sc in PRIM:
            return [(src, "value")], False, []
        if sc == "ComfySwitchNode":
            out, der, sw = [], False, []
            if is_link(self.g[src]["inputs"].get("switch")):
                st, _, _ = self.terminals(src, "switch", depth + 1)
                sw += st
            for b in ("on_true", "on_false"):
                if b in self.g[src]["inputs"]:
                    t, d, w = self.terminals(src, b, depth + 1)
                    out += t
                    der |= d
                    sw += w
            return out, der, sw
        if sc == "PreviewAny" and "source" in self.g[src]["inputs"]:
            return self.terminals(src, "source", depth + 1)
        if sc == "TextGenerate" and "prompt" in self.g[src]["inputs"]:
            return self.terminals(src, "prompt", depth + 1)
        if sc == "StringConcatenate":
            out = []
            for k in ("string_a", "string_b"):
                t, _, _ = self.terminals(src, k, depth + 1)
                # the system-prompt idiom ("You are ...") is the template's scaffolding, not user input
                out += [x for x in t if not str(self.lit(*x)).lstrip().startswith("You are")]
            return out, True, []
        if sc == "ComfyMathExpression":
            out, sw = [], []
            for k in self.g[src]["inputs"]:
                if k.startswith("values."):
                    t, _, w = self.terminals(src, k, depth + 1)
                    out += t
                    sw += w
            return out, True, sw
        return [], True, []

    # -- fields
    def add_field(self, f, binds):
        binds = [b for b in binds if (b["node"], b["input"]) not in self.used]
        if not binds or f["id"] in {x["id"] for x in self.fields} or f["id"] in RESERVED:
            return None
        for b in binds:
            self.used.add((b["node"], b["input"]))
        f["bind"] = binds
        self.fields.append(f)
        return f

    def text_walk(self, roots, negative):
        sinks, seen, stack = [], set(), [r[0] for r in roots]
        while stack:
            nid = stack.pop()
            if nid in seen or nid not in self.g:
                continue
            seen.add(nid)
            ins = self.g[nid]["inputs"]
            names = [n for n in (TEXT_NEG if negative else TEXT_POS) if n in ins and (self.spec(nid, n) or [""])[0] == "STRING"]
            if negative is False:
                names = [n for n in names if n not in TEXT_NEG]
            if names:
                sinks.append((nid, names[0]))
            for k, v in ins.items():
                if k == "negative" and negative is not True and "positive" in ins:
                    continue       # keep polarity at nodes that have both
                if k == "positive" and negative is True and "negative" in ins:
                    continue
                if is_link(v) and v[0] in self.g and self.out_type(v[0], v[1]) == "CONDITIONING":
                    stack.append(v[0])
        return sinks

    def text_sources(self, sinks):
        ts = []
        for nid, inp in sinks:
            t, _, _ = self.terminals(nid, inp)
            ts += t
        ts = [t for t in dict.fromkeys(ts) if isinstance(self.lit(*t), str)]
        if any(str(self.lit(*t)).strip() for t in ts):
            ts = [t for t in ts if str(self.lit(*t)).strip()]       # empty = a negative placeholder
        return ts

    def build_prompt(self):
        g = self.g
        pos, neg = [], []
        for nid, n in g.items():
            for k, v in n["inputs"].items():
                if not is_link(v):
                    continue
                if k == "positive" or k.startswith("cond") or k == "conditioning":
                    pos.append(v)
                elif k == "negative":
                    neg.append(v)
        psinks = self.text_walk(pos, None)
        if not psinks:       # fall back to the first literal multiline STRING that is not a system prompt
            for nid, n in g.items():
                for k in n["inputs"]:
                    sp = self.spec(nid, k)
                    if (sp and sp[0] == "STRING" and isinstance(sp[1] if len(sp) > 1 else {}, dict) and (sp[1] or {}).get("multiline")
                            and (k in TEXT_POS or n["class_type"] == "PrimitiveStringMultiline")
                            and "system" not in k and k not in TEXT_NEG and isinstance(n["inputs"][k], str) and n["inputs"][k].strip()):
                        psinks.append((nid, k))
            psinks = psinks[:1]
        ts = self.text_sources(psinks)
        prompt_default = None
        if ts:
            groups = {}
            for t in ts:
                groups.setdefault(self.lit(*t), []).append(t)
            cands = [{"node": v[0][0], "input": v[0][1], "title": self.title(v[0][0]),
                      "excerpt": (txt.strip()[:80] + ("..." if len(txt.strip()) > 80 else "")),
                      "also": [[n, i] for n, i in v[1:]]} for txt, v in groups.items()]
            first = cands[0]
            binds = [{"node": first["node"], "input": first["input"]}] + [{"node": n, "input": i} for n, i in first["also"]]
            f = self.add_field({"id": "prompt", "label": "Prompt", "type": "textarea", "default": self.lit(first["node"], first["input"]),
                                "tier": "primary", "group": "Content"}, binds)
            if f:
                prompt_default = f["default"]
                if len(cands) > 1:
                    self.candidates["prompt"] = cands
                    self.flags.append(_flag("prompt_source", "answer", "This template has %d different texts that could be the prompt. Pick the one a user should write." % len(cands),
                                            candidates=len(cands)))
        if prompt_default is None:
            self.flags.append(_flag("no_prompt", "ack", "No prompt was found. That is right for a template that works on a picture without words "
                                                        "(an upscaler, a depth map); check that this is one."))
        nts = [t for t in self.text_sources([s for s in self.text_walk(neg, True) if s not in psinks]) if str(self.lit(*t)).strip()]
        if nts:
            self.add_field({"id": "negative", "label": "Negative prompt", "type": "textarea", "default": self.lit(*nts[0]),
                            "tier": "advanced", "group": "Content"}, [{"node": n, "input": i} for n, i in nts if self.lit(n, i) == self.lit(*nts[0])])
        return prompt_default

    def build_media(self, prompt_default):
        g = self.g
        mentions = []
        for m in _PICTURE_MENTION.finditer(prompt_default or ""):
            if m.group(0) not in mentions:
                mentions.append(m.group(0))
        for cls, inp, ftype, one_id, one_label, word in (("LoadImage", "image", "image", "picture", "Picture", "Picture"),
                                                         ("LoadVideo", "file", "video_list", "clip", "Source clip", "Clip"),
                                                         ("LoadAudio", "audio", "audio", "audio", "Audio", "Audio")):
            nodes = sorted([k for k, n in g.items() if n["class_type"] == cls and not is_link(n["inputs"].get(inp))], key=_numeric_id)
            if not nodes:
                continue
            ids = []
            for j, k in enumerate(nodes):
                t = self.title(k)
                if len(nodes) == 1:
                    fid, label, hint = one_id, t or one_label, None
                else:
                    fid, label = "%s%d" % (one_id, j + 1), "%s %d (%s)" % (word, j + 1, t or "untitled")
                    hint = "Template node %s (%s)." % (k, t or "untitled")
                    if cls == "LoadImage" and mentions:
                        hint += " The prompt mentions: %s." % ", ".join(mentions)
                f = {"id": fid, "label": label, "type": ftype, "tier": "primary", "group": "Content", "required": True}
                if ftype == "video_list":
                    f["max"] = 1
                if hint:
                    f["hint"] = hint
                bind = {"node": k, "input": inp}
                if ftype == "video_list":
                    bind["index"] = 0
                if self.add_field(f, [bind]):
                    ids.append(fid)
            if len(nodes) > 1:
                what = {"LoadImage": "picture", "LoadVideo": "clip", "LoadAudio": "audio"}[cls]
                self.flags.append(_flag("%s_roles" % what, "answer", "This template takes %d %ss. Say what each one is, in this order." % (len(nodes), what),
                                        fields=ids, nodes=nodes))

    def build_size(self):
        g, done, derived = self.g, False, False
        for nid, n in g.items():
            ins = n["inputs"]
            if "width" in ins and "height" in ins and re.search(r"Latent|ToVideo|Empty|Resolution", n["class_type"]) and n["class_type"] != "ResolutionSelector":
                tw, dw, _ = self.terminals(nid, "width")
                th, dh, _ = self.terminals(nid, "height")
                if dw or dh or not tw or not th:
                    src = g[ins["width"][0]] if is_link(ins["width"]) and ins["width"][0] in g else None
                    if src and src["class_type"] == "ResolutionSelector":
                        rid = ins["width"][0]
                        ar = self.spec(rid, "aspect_ratio")
                        opts = _enum_options(ar)
                        if opts and self.add_field({"id": "aspect", "label": "Aspect ratio", "type": "select", "options": opts,
                                                    "default": g[rid]["inputs"]["aspect_ratio"], "tier": "primary", "group": "Size"},
                                                   [{"node": rid, "input": "aspect_ratio"}]):
                            self.add_field({"id": "megapixels", "label": "Megapixels", "type": "number", "units": "MP",
                                            "default": g[rid]["inputs"]["megapixels"], "range": [0.1, 4.0], "tier": "primary", "group": "Size"},
                                           [{"node": rid, "input": "megapixels"}])
                            done = True
                    else:
                        derived = True
                    continue
                for fid, label, t in (("width", "Width", tw), ("height", "Height", th)):
                    v = self.lit(*t[0])
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        self.add_field({"id": fid, "label": label, "type": "int", "units": "px", "default": int(v), "range": [64, 4096],
                                        "tier": "primary", "group": "Size"}, [{"node": a, "input": b} for a, b in t])
                        done = True
                if done:
                    break
        if derived and not done:
            self.flags.append(_flag("size_derived", "ack", "The size comes from the input picture or from a calculation, so this mode has no size fields."))

    def build_length(self):
        g = self.g
        for nid, n in g.items():
            for inp in ("length", "frames", "num_frames", "video_frames"):
                if inp in n["inputs"] and re.search(r"ToVideo|Video|Latent", n["class_type"]):
                    t, der, _ = self.terminals(nid, inp)
                    t = [x for x in t if isinstance(self.lit(*x), (int, float)) and not isinstance(self.lit(*x), bool)]
                    if not t:
                        continue
                    v = self.lit(*t[0])
                    f = {"id": "length", "label": "Length" if der else "Length (frames)", "type": "number" if isinstance(v, float) else "int",
                         "units": "value" if der else "frames", "default": v, "range": [1, 2000], "tier": "primary", "group": "Length"}
                    if self.add_field(f, [{"node": a, "input": b} for a, b in t[:1]]):
                        if der:
                            self.flags.append(_flag("length_unit", "answer", "The length is worked out through a calculation. Say whether the number is "
                                                                              "frames, seconds or something else.", field="length", template_value=v))
                        return

    def build_sampling(self):
        g = self.g
        num = lambda x: isinstance(x, (int, float)) and not isinstance(x, bool)
        steps = [(k, "steps") for k, n in g.items() if num(n["inputs"].get("steps"))]
        if steps and len({g[k]["inputs"]["steps"] for k, _ in steps}) == 1:
            self.add_field({"id": "steps", "label": "Steps", "type": "int", "units": "steps", "default": int(g[steps[0][0]]["inputs"]["steps"]),
                            "range": [1, 200], "tier": "advanced", "group": "Sampling"}, [{"node": k, "input": i} for k, i in steps])
        elif steps or any(is_link(n["inputs"].get("steps")) for n in g.values()):
            self.flags.append(_flag("steps_hidden", "ack", "Steps are chosen by a switch or differ between stages, so this mode has no steps field."))
        cfgs = [(k, i) for k, n in g.items() for i in ("cfg", "guidance") if num(n["inputs"].get(i))]
        if cfgs and len({g[k]["inputs"][i] for k, i in cfgs}) == 1:
            k0, i0 = cfgs[0]
            self.add_field({"id": "cfg", "label": "Guidance (CFG)", "type": "number", "units": "cfg", "default": float(g[k0]["inputs"][i0]),
                            "range": [0, 30], "tier": "advanced", "group": "Sampling"}, [{"node": k, "input": i} for k, i in cfgs])
        elif len(cfgs) > 1:
            self.flags.append(_flag("cfg_hidden", "ack", "Guidance differs between stages, so this mode has no guidance field."))
        seeds = []
        for k, n in g.items():
            for i in n["inputs"]:
                if i in ("seed", "noise_seed") or i.endswith(".seed"):
                    t, _, _ = self.terminals(k, i)
                    seeds += [x for x in t if isinstance(self.lit(*x), int) and not isinstance(self.lit(*x), bool)]
        seeds = list(dict.fromkeys(seeds))
        if seeds:
            self.add_field({"id": "seed", "label": "Seed", "type": "int", "units": "seed", "default": int(self.lit(*seeds[0])) % 2147483647,
                            "range": [0, 2147483647], "tier": "advanced", "group": "Sampling"}, [{"node": a, "input": b} for a, b in seeds])

    def build_switches(self):
        g = self.g
        for k, n in g.items():
            if n["class_type"] == "PrimitiveBoolean" and any(is_link(m["inputs"].get("switch")) and m["inputs"]["switch"][0] == k
                                                              for m in g.values() if m["class_type"] == "ComfySwitchNode"):
                fid = "opt_" + (slug(self.title(k), "_")[:20].strip("_") or slug(k, "_")[:10])
                label = self.title(k) or "Option (node %s)" % k
                if self.add_field({"id": fid, "label": label, "type": "checkbox", "default": bool(n["inputs"]["value"]), "tier": "advanced",
                                   "group": "Options", "hint": "Template switch node %s; the template does not say what it does." % k},
                                  [{"node": k, "input": "value"}]):
                    self.flags.append(_flag("switch:" + fid, "answer", "A switch in the template (%s) changes how it runs, and the template does not "
                                                                       "say what it does. Give it a label, or hide it." % label, field=fid, node=k))

    def build_promoted(self):
        g, added = self.g, []
        for nid, inp in self.promoted:
            if nid not in g or inp not in g[nid]["inputs"] or (nid, inp) in self.used:
                continue
            v, sp = g[nid]["inputs"][inp], self.spec(nid, inp)
            if is_link(v) or not sp or isinstance(v, (dict, list)):
                continue
            t = sp[0] if isinstance(sp[0], str) else "COMBO"
            o = sp[1] if len(sp) > 1 and isinstance(sp[1], dict) else {}
            fid = re.sub(r"\W+", "_", slug(self.title(nid), "_")[:24] if inp == "value" and self.title(nid) else inp).strip("_")[:30] or "opt"
            fid = ("p_" + fid) if not fid[0].isalpha() else fid
            while fid in {x["id"] for x in self.fields} or fid in RESERVED:
                fid += "_"
            label = ("%s: %s" % (self.title(nid), inp)) if self.title(nid) else inp.replace("_", " ")
            base = {"id": fid, "label": label, "tier": "advanced", "group": "Template options"}
            num = isinstance(v, (int, float)) and not isinstance(v, bool)
            if t in ("INT", "FLOAT") and num:
                lo, hi = max(o.get("min", -1e9), -1e9), min(o.get("max", 1e9), 1e9)
                f = dict(base, type="int" if t == "INT" else "number", units=inp[:20], default=int(v) if t == "INT" else v, range=[lo, hi])
            elif t == "BOOLEAN" and isinstance(v, bool):
                f = dict(base, type="checkbox", default=v)
            elif _enum_options(sp) is not None:
                opts = _enum_options(sp)
                if not opts or any(isinstance(x, (list, dict)) for x in opts) or (isinstance(v, str) and v.lower().endswith(MODEL_EXTS)) or v not in opts:
                    continue
                f = dict(base, type="select", options=opts, default=v)
            elif t == "STRING" and isinstance(v, str):
                f = dict(base, type="textarea" if o.get("multiline") else "text", default=v)
            else:
                continue
            if self.add_field(f, [{"node": nid, "input": inp}]):
                added.append(fid)
        if added:
            self.flags.append(_flag("promoted_unreviewed", "ack", "The template's author put these settings on its face: %s. Their ranges come from "
                                                                  "the node, not from a person; check them." % ", ".join(added), fields=added))

    # -- requires
    @staticmethod
    def toks(fname):
        stem = _base(fname).lower()
        for e in MODEL_EXTS:
            if stem.endswith(e):
                stem = stem[:-len(e)]
        return [t for t in re.split(r"[^a-z0-9.]+", stem) if t]

    def pool_names(self, pool):
        return list(self.pools.get(pool) or [])

    def rule_for(self, fname, pool, own):
        toks = self.toks(fname)
        keep = [t for t in toks if t not in DTYPE]
        rule = {"all": keep or toks}
        low = _base(fname).lower()
        # Sibling variants: other files of this pool (the lane's, and this template's own) that
        # the 'all' rule would also accept. Exclude each with a token the original lacks.
        sibs = {x for x in (set(own.get(pool, ())) | set(self.pool_names(pool))) if self.toks(x) != toks and all(t in _base(x).lower() for t in rule["all"])}
        none = []
        for o in sorted(sibs):
            ol = _base(o).lower()
            if any(t in ol for t in none):
                continue
            cand = [t for t in self.toks(o) if t not in DTYPE and t not in low and t not in none]
            if cand:
                t = max(cand, key=len)
                if len(t) <= 3 and "_" + t + "_" in ol:
                    t = "_" + t + "_"
                none.append(t)
        if none:
            rule["none"] = none
        # the exact file first, so the template's own file always ranks first when the lane has it
        stem = low
        for e in MODEL_EXTS:
            if stem.endswith(e):
                stem = stem[:-len(e)]
                break
        rule["prefer"] = [stem] + [t for t in toks if t in DTYPE and t not in ("safetensors", "comfyui")]
        return rule

    def build_requires(self):
        own = {}
        for n in self.g.values():
            for inp, v in n["inputs"].items():
                if isinstance(v, str) and v.lower().endswith(MODEL_EXTS) and (n["class_type"], inp) in POOL_OF:
                    own.setdefault(POOL_OF[(n["class_type"], inp)], set()).add(v)
        counts = {}
        for nid in sorted(self.g, key=_numeric_id):
            n = self.g[nid]
            for inp, v in n["inputs"].items():
                if not isinstance(v, str) or not v.lower().endswith(MODEL_EXTS) or (n["class_type"], inp) in UPLOAD_PICKERS:
                    continue
                sp = self.spec(nid, inp)
                if not sp:
                    continue
                pool = POOL_OF.get((n["class_type"], inp))
                if not pool:
                    if _enum_options(sp) is not None:
                        self.flags.append(_flag("unslotted_loader:%s.%s" % (nid, inp), "block",
                                                "BWF cannot check whether this machine has %s (loaded by %s), so this template can't be added yet." % (v, n["class_type"]),
                                                node=nid, input=inp, file=v, loader=n["class_type"]))
                    continue
                if (nid, inp) in self.used:
                    continue
                counts[pool] = counts.get(pool, 0) + 1
                role = pool.replace("_", "") + ("" if counts[pool] == 1 else str(counts[pool]))
                self.requires.append({"role": role, "pool": pool, "node": nid, "input": inp, "match": self.rule_for(v, pool, own),
                                      "words": "the %s file %s" % (pool.replace("_", " "), _base(v))})
                self.used.add((nid, inp))

    def substitution_flags(self):
        for r in self.requires:
            hits = _comfy.rule_matches(self.pool_names(r["pool"]), r["match"])
            if not hits:
                continue
            best = sorted(hits, key=lambda x: (-_comfy.rank(x, r["match"], self.small_card), x))[0]
            orig = self.g[r["node"]]["inputs"][r["input"]]
            if _base(best) != _base(orig):
                self.flags.append(_flag("model_substitution:" + r["role"], "ack",
                                        "The template names %s, but %s will be used instead (%s)." % (_base(orig), _base(best), self.lane_name or "this ComfyUI"),
                                        role=r["role"], template_file=orig, file=best))


def _output_kinds(g):
    kinds = set()
    for n in g.values():
        c = n["class_type"]
        if c == "SaveVideo":
            kinds.add("video")
        elif c.startswith("SaveImage") or c == "SaveAnimatedWEBP":
            kinds.add("image")
        elif c.startswith("SaveAudio"):
            kinds.add("audio")
        elif c in ("SaveGLB", "Save3DAdvanced"):
            kinds.add("3d")
    return kinds


def synthesize(name, template_ui, api_graph, oi, meta, lane_pools, taken, lane_name="", small_card=True):
    """A converted template -> a draft: {"manifest", "graph", "flags", "candidates", "log"}.

    Raises Refused (and writes nothing) for a template that runs on a paid service or makes no
    file. Everything the code is unsure of becomes a flag: `block` (cannot be added), `answer`
    (a person must say) or `ack` (a person must look). Nodes are never added, removed or
    rewired, except dropping previews nothing uses and swapping a lone preview for a save."""
    meta = meta or {}
    if name.startswith("api_") or meta.get("local") is False:
        raise Refused(PAID)
    if validate_api_graph(api_graph, oi)["billed"]:
        raise Refused(PAID)

    used_ids = set(taken.get("ids") or ())
    pid = _unique(slug(name, "-")[:36].strip("-") or "template", used_ids, "-")
    used_modes = {m for _c, m in (taken.get("modes") or ())} | set(taken.get("abilities") or ()) | set(taken.get("caps") or ())

    g0 = api_graph
    g, _has = prune_to_outputs(g0, oi)
    consumed = {v[0] for n in g.values() for v in n["inputs"].values() if is_link(v)}
    g = {k: n for k, n in g.items() if not (n["class_type"] in PREVIEW and k not in consumed)}
    g, _has = prune_to_outputs(g, oi)
    flags, swapped = [], False
    if not any(n["class_type"].startswith("Save") for n in g.values()):
        for k, n in g0.items():
            if n["class_type"] == "PreviewImage" and "images" in n["inputs"]:
                g[k] = {"inputs": {"images": n["inputs"]["images"], "filename_prefix": "bwf/" + pid},
                        "class_type": "SaveImage", "_meta": {"title": "Save Image"}}
                swapped = True
        if swapped:         # bring back the upstream nodes the preview drop pruned away
            need, stack = set(), [k for k, n in g.items() if n["class_type"] == "SaveImage"]
            while stack:
                k = stack.pop()
                if k in need:
                    continue
                need.add(k)
                for v in (g.get(k) or g0[k])["inputs"].values():
                    if is_link(v) and v[0] in g0:
                        stack.append(v[0])
            g = {k: (g.get(k) or g0[k]) for k in g0 if k in need}
    kinds = _output_kinds(g)
    cap = next((k for k in ("video", "audio", "3d", "image") if k in kinds), None)
    if not cap:
        raise Refused(NO_FILE)
    if swapped:
        flags.append(_flag("preview_swapped", "ack", "The template only previews its picture, so a save step was put in its place."))
    if len(kinds) > 1:
        ks = sorted(kinds, key=lambda k: ("video", "audio", "3d", "image").index(k))
        flags.append(_flag("two_output_kinds", "ack", "This template makes both %s and %s; the mode is filed under %s." % (" and ".join(ks[:-1]), ks[-1], cap), kinds=ks, cap=cap))

    mode = re.sub(r"^(image|video|audio|utility|3d|basic|template|templates|llm)[_-]", "", name)
    mode = slug(mode, "_")[:20].strip("_") or "t"
    if not mode[0].isalpha():
        mode = "t" + mode
    mode = _unique(mode, used_modes, "_")

    S = _Synth(g, oi, template_promoted(template_ui), lane_pools, small_card, lane_name)
    S.flags = flags
    S.build_requires()
    prompt_default = S.build_prompt()
    S.build_media(prompt_default)
    S.build_size()
    S.build_length()
    S.build_sampling()
    S.build_switches()
    S.build_promoted()
    S.substitution_flags()
    for i, f in enumerate(S.fields, 1):
        f["order"] = i

    title = (meta.get("title") or name)[:90] or name
    models = meta.get("models") or []
    manifest = {"schema": 1, "id": pid, "cap": cap, "mode": mode, "label": title, "graph": "graph.api.json", "fields": S.fields,
                "requires": S.requires,
                "licence": {"name": "Unknown (from the ComfyUI template %s)" % name, "shippable": False,
                            "attribution": "Made from the ComfyUI template %s on %s; models: %s" % (name, lane_name or "a ComfyUI lane", ", ".join(models) if models else "not listed")}}
    first = re.split(r"(?<=[.!?])\s", (meta.get("description") or "").strip(), maxsplit=1)[0].strip()[:200]
    if first:
        manifest["note"] = first
    S.flags.append(_flag("licence", "answer", "The template does not say what licence its models have. Check the licences of the models listed in the attribution, then confirm."))
    return {"manifest": manifest, "graph": g, "flags": S.flags, "candidates": S.candidates, "log": {}}


# ----------------------------------------------------------------------------- loading a draft the way BWF would

def _engine_list(engine_list):
    if engine_list is not None:
        return engine_list
    from . import packs
    return packs()


def _try_load(manifest, graph, engine_list=None, exclude="__same__"):
    """BWF's own loader on a manifest + graph held in memory -> the ENGINE dict, or PackError."""
    pid = manifest.get("id")
    if not isinstance(pid, str) or not up._ID_RE.match(pid) or "__" in pid:
        raise up.PackError("id must be lower-case letters, digits, '-' or '_' (got %r)" % (pid,))
    work = tempfile.mkdtemp(prefix="bwf-autopack-")
    try:
        folder = os.path.join(work, pid)
        os.makedirs(folder)
        rel = manifest.get("graph") if isinstance(manifest.get("graph"), str) and re.fullmatch(r"[\w.\-]+", manifest.get("graph") or "") else "graph.api.json"
        with open(os.path.join(folder, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f)
        with open(os.path.join(folder, rel), "w", encoding="utf-8") as f:
            json.dump(graph, f)
        taken = taken_for(None, _engine_list(engine_list), exclude=pid if exclude == "__same__" else exclude)
        return up._load_one(pid, os.path.realpath(folder), taken)
    finally:
        shutil.rmtree(work, ignore_errors=True)


_PLACEHOLDER = {"image": "bwf-check.png", "audio": "bwf-check.wav", "model": "bwf-check.glb"}


def _placeholder_args(manifest):
    args = {}
    for f in manifest.get("fields") or []:
        t = f.get("type")
        if t in _PLACEHOLDER:
            args[f["id"]] = _PLACEHOLDER[t]
        elif t == "image_list":
            args[f["id"]] = [_PLACEHOLDER["image"]] * int(f.get("max", 1))
        elif t == "video_list":
            args[f["id"]] = ["bwf-check.mp4"] * int(f.get("max", 1))
    return args


def build_test_graph(manifest, graph, args, files, engine_list=None):
    """The graph BWF's own builder makes for this draft: `args` are field values (the user's
    uploads by name; anything not given keeps the field's default) and `files` maps each
    role to the model file chosen for it. Raises Refused in plain words when it cannot."""
    try:
        eng = _try_load(manifest, graph, engine_list)
    except up.PackError as e:
        raise Refused(str(e))
    mode = next(iter(eng["graphs"]))
    models = {"u__%s__%s" % (manifest["id"].replace("-", "_"), role): f for role, f in files.items()}
    try:
        return eng["graphs"][mode](dict(args), models)
    except KeyError as e:
        label = next((f["label"] for f in manifest.get("fields", []) if f["id"] == e.args[0]), e.args[0])
        raise Refused("%s is needed for this." % label)
    except ValueError as e:
        raise Refused(str(e))


# ----------------------------------------------------------------------------- readiness (section 6)

def readiness(manifest, graph, flags, oi, pools, small_card, sources=None, engine_list=None):
    """Whether a lane can run this draft, in the order that matters:
    block flag -> classes the lane lacks -> model files -> the graph BWF would really build,
    validated. -> {state: available|needs_files|blocked, roles, missing, blocked}."""
    out = {"state": "available", "roles": [], "missing": [], "blocked": []}
    blocks = [f["text"] for f in flags or [] if f.get("kind") == "block"]
    if blocks:
        out.update(state="blocked", blocked=blocks)
        return out
    absent = sorted({n.get("class_type") for n in graph.values() if n.get("class_type") not in oi})
    if absent:
        out.update(state="blocked", blocked=["This needs nodes this ComfyUI does not have: %s." % ", ".join(str(a) for a in absent)])
        return out
    files = {}
    for r in manifest.get("requires") or []:
        hits = _comfy.rule_matches(pools.get(r["pool"], []), r["match"])
        orig = graph[r["node"]]["inputs"][r["input"]]
        best = sorted(hits, key=lambda x: (-_comfy.rank(x, r["match"], small_card), x))[0] if hits else None
        why = ("the only file that fits" if len(hits) == 1 else "ranked first of %d files that fit" % len(hits)) if hits else "no file on this lane fits"
        out["roles"].append({"role": r["role"], "words": r["words"], "template_file": orig, "file": best, "why": why,
                             "substituted": bool(best) and _base(best) != _base(orig)})
        if best:
            files[r["role"]] = best
        elif not r.get("optional"):
            src = (sources or {}).get(_base(orig)) or {}
            out["missing"].append({"words": r["words"], "template_file": orig, "directory": src.get("directory"), "url": src.get("url")})
    if out["missing"]:
        out["state"] = "needs_files"
        return out
    try:
        built = build_test_graph(manifest, graph, _placeholder_args(manifest), files, engine_list)
    except Refused as e:
        out.update(state="blocked", blocked=[e.sentence])
        return out
    v = validate_api_graph(built, oi)
    lines = ["%s (%s): %s names %s, which this ComfyUI does not list." % (m["node"], m["class"], m["input"], m["value"]) for m in v["missing_files"]]
    lines += list(v["errors"]) + ["The graph uses %s, which this ComfyUI does not have." % c for c in v["unknown_classes"]]
    if lines:
        out.update(state="blocked", blocked=lines)
    return out


def test_verdict(entry, cap, sizes=None):
    """The machine half of a test render: did the lane finish without error and make a file
    of the pack's kind? `entry` is the lane's /history entry; `sizes` maps downloaded file
    name -> bytes (the CLI knows them). -> {machine_pass, reasons}"""
    reasons = []
    status = entry.get("status") or {}
    if status.get("status_str") == "error":
        reasons.append("The lane reported an error while running it.")
    elif status.get("completed") is False:
        reasons.append("The lane did not finish it.")
    outs = _comfy.collect_outputs(entry)
    if not outs:
        reasons.append("The lane finished without making any file.")
    elif not any(o["media"] == cap for o in outs):
        reasons.append("It made %s, not %s." % (", ".join(sorted({o["media"] for o in outs})), cap))
    if sizes is not None:
        for o in outs:
            if o["media"] == cap and sizes.get(o["filename"]) == 0:
                reasons.append("The file %s is empty." % o["filename"])
    return {"machine_pass": not reasons, "reasons": reasons}


# ----------------------------------------------------------------------------- the store (section 7)

def drafts_root(data_dir):
    return os.path.join(data_dir, "packs-draft")


def draft_dir(data_dir, did):
    if not isinstance(did, str) or not up._ID_RE.match(did):
        raise Refused("There is no draft called %r." % (did,))
    root = os.path.realpath(drafts_root(data_dir))
    path = os.path.realpath(os.path.join(root, did))
    if os.path.commonpath([root, path]) != root or path == root:
        raise Refused("There is no draft called %r." % did)
    return path


def _read(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _dump(path, obj, **kw):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, **kw)


def _write_ap(ddir, ap):
    ap["updated"] = int(time.time())
    tmp = os.path.join(ddir, ".autopack.json.tmp")
    _dump(tmp, ap, indent=1, ensure_ascii=False)
    _replace(tmp, os.path.join(ddir, "autopack.json"))


def _replace(src, dst):
    import fsutil
    fsutil.replace(src, dst)


def _write_manifest(ddir, manifest):
    _dump(os.path.join(ddir, "manifest.json"), manifest, indent=1, ensure_ascii=False)


def find_draft(data_dir, lane, template):
    root = drafts_root(data_dir)
    if not os.path.isdir(root):
        return None
    for name in sorted(os.listdir(root)):
        try:
            a = _read(os.path.join(root, name, "autopack.json"))
        except (OSError, ValueError):
            continue
        if a.get("source", {}).get("lane") == lane and a.get("source", {}).get("template") == template:
            return name
    return None


def create_draft(data_dir, draft, lane, lane_name, template, title, category, versions, template_bytes, converted, converter, now=None):
    """Write a synthesized draft under packs-draft/<id>/ -> (id, created). A draft for the same
    (lane, template) already there is returned as it is."""
    existing = find_draft(data_dir, lane, template)
    if existing:
        return existing, False
    m = draft["manifest"]
    did = m["id"]
    final = os.path.join(drafts_root(data_dir), did)
    if os.path.exists(final):
        raise Refused("There is already a draft called %s." % did)
    now = int(now if now is not None else time.time())
    stage = os.path.join(drafts_root(data_dir), ".tmp-" + did)
    shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(os.path.join(stage, "source"))
    _write_manifest(stage, m)
    _dump(os.path.join(stage, "graph.api.json"), draft["graph"], ensure_ascii=False)
    with open(os.path.join(stage, "source", "template.ui.json"), "wb") as f:
        f.write(template_bytes)
    _dump(os.path.join(stage, "source", "converted.api.json"), converted)
    _dump(os.path.join(stage, "source", "synth.manifest.json"), m, indent=1, ensure_ascii=False)
    ap = {"autopack": 1, "id": did, "state": "draft", "created": now, "updated": now,
          "source": {"lane": lane, "lane_name": lane_name, "template": template, "title": title, "category": category,
                     "comfyui_version": versions.get("comfyui_version"), "frontend_version": versions.get("frontend_version"),
                     "templates_version": versions.get("templates_version"),
                     "template_sha256": hashlib.sha256(template_bytes).hexdigest(), "converted_sha256": sha(converted), "converter": converter},
          "flags": [dict(f, resolved=False, answer=None) for f in draft["flags"]],
          "candidates": draft.get("candidates") or {},
          "licence": {"confirmed": False, "name": None, "url": None, "at": None},
          "reviewed": None, "test": None, "enabled": None}
    _write_ap(stage, ap)
    _replace(stage, final)
    return did, True


def read_draft(data_dir, did):
    """-> {id, dir, ap, manifest, graph}. A reviewed or tested pack whose manifest or graph no
    longer hashes to what was reviewed is a draft again (an edit, however it was made)."""
    ddir = draft_dir(data_dir, did)
    try:
        ap = _read(os.path.join(ddir, "autopack.json"))
        manifest = _read(os.path.join(ddir, "manifest.json"))
        graph = _read(os.path.join(ddir, manifest.get("graph") if isinstance(manifest.get("graph"), str) else "graph.api.json"))
    except (OSError, ValueError):
        raise Refused("There is no draft called %s." % did)
    if ap.get("state") in ("reviewed", "tested"):
        rev = ap.get("reviewed") or {}
        if (rev.get("manifest_sha256"), rev.get("graph_sha256")) != hashes(manifest, graph):
            ap["state"], ap["reviewed"] = "draft", None
            _write_ap(ddir, ap)
    return {"id": did, "dir": ddir, "ap": ap, "manifest": manifest, "graph": graph}


def list_drafts(data_dir):
    root = drafts_root(data_dir)
    out = []
    for name in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        if name.startswith("."):
            continue
        try:
            out.append(read_draft(data_dir, name))
        except Refused:
            continue
    return out


def _rooms():
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "rooms.json")
    try:
        return {r.get("id") for r in _read(p) if isinstance(r, dict)}
    except (OSError, ValueError):
        return set()


def _parse_value(f, raw):
    t = f.get("type")
    low = raw.strip().lower()
    if t in ("text", "textarea", "select"):
        return raw
    if t == "int":
        try:
            return int(raw)
        except ValueError:
            raise Refused("%s needs a whole number (got %r)." % (f["label"], raw))
    if t == "number":
        try:
            return float(raw) if not re.fullmatch(r"-?\d+", raw.strip()) else int(raw)
        except ValueError:
            raise Refused("%s needs a number (got %r)." % (f["label"], raw))
    if t == "checkbox":
        if low in ("true", "yes", "on", "1"):
            return True
        if low in ("false", "no", "off", "0"):
            return False
        raise Refused("%s needs true or false (got %r)." % (f["label"], raw))
    raise Refused("%s takes a file from the user; it has no default." % f["label"])


def _parse_range(f, raw):
    s = raw.strip().strip("[]")
    parts = [p.strip() for p in s.split(",")]
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        nums = []
    if len(nums) != 2:
        raise Refused("A range is written low,high (for example 1,60).")
    if f.get("type") == "int":
        nums = [int(x) if x == int(x) else x for x in nums]
    else:
        nums = [int(x) if x == int(x) and "." not in p else x for x, p in zip(nums, parts)]
    return nums


def _check_one(f, graph, manifest):
    try:
        up._check_field(f, graph, manifest["fields"].index(f) if f in manifest["fields"] else 0)
    except up.PackError as e:
        raise Refused(str(e))


def edit(data_dir, did, assignments, rooms=None, engine_list=None):
    """`set`: KEY=VALUE edits to manifest.json (section 7.4). Validated by the loader's own field
    rules; any edit sends a reviewed pack back to draft (by hash, on the next read)."""
    r = read_draft(data_dir, did)
    manifest = copy.deepcopy(r["manifest"])
    graph = r["graph"]
    synth = _read(os.path.join(r["dir"], "source", "synth.manifest.json"))
    for item in assignments:
        if "=" not in item:
            raise Refused("Write each edit as KEY=VALUE (got %r)." % item)
        key, raw = item.split("=", 1)
        if "." not in key:
            if key == "label":
                if not raw.strip():
                    raise Refused("A mode needs a label.")
                manifest["label"] = raw
            elif key == "note":
                if raw.strip():
                    manifest["note"] = raw
                else:
                    manifest.pop("note", None)
            elif key == "room":
                if raw.strip():
                    if raw not in (rooms if rooms is not None else _rooms()):
                        raise Refused("%r is not a room in rooms.json." % raw)
                    manifest["room"] = raw
                else:
                    manifest.pop("room", None)
            elif key == "mode":
                if not up._CAP_RE.match(raw):
                    raise Refused("A mode is a short lower-case word (got %r)." % raw)
                t = taken_for(data_dir, _engine_list(engine_list), exclude=did)
                if raw in t["abilities"] or raw in t["caps"] or (manifest["cap"], raw) in t["modes"]:
                    raise Refused("The mode %r is already used by another mode or capability." % raw)
                manifest["mode"] = raw
            else:
                raise Refused("Unknown setting %r. Use label, note, room, mode, or FIELD.label/hint/default/units/range/tier/order/hidden." % key)
            continue
        fid, attr = key.split(".", 1)
        fields = manifest["fields"]
        f = next((x for x in fields if x["id"] == fid), None)
        if attr == "hidden":
            low = raw.strip().lower()
            if low in ("true", "yes", "1"):
                if f is None:
                    raise Refused("There is no field called %s." % fid)
                if f.get("required"):
                    raise Refused("%s is needed for this mode to run, so it cannot be hidden." % f["label"])
                manifest["fields"] = [x for x in fields if x["id"] != fid]
            elif low in ("false", "no", "0"):
                if f is None:
                    orig = next((x for x in synth["fields"] if x["id"] == fid), None)
                    if orig is None:
                        raise Refused("There is no field called %s." % fid)
                    keep = {x["id"] for x in fields} | {fid}
                    manifest["fields"] = [copy.deepcopy(x) if x["id"] == fid else next(y for y in fields if y["id"] == x["id"]) for x in synth["fields"] if x["id"] in keep]
                    manifest["fields"] += [x for x in fields if x["id"] not in {y["id"] for y in synth["fields"]}]
            else:
                raise Refused("hidden is true or false (got %r)." % raw)
            continue
        if f is None:
            raise Refused("There is no field called %s." % fid)
        if attr in ("label", "hint", "units", "tier"):
            if attr in ("label", "units") and not raw.strip():
                raise Refused("A field's %s cannot be empty." % attr)
            f[attr] = raw
        elif attr == "default":
            f["default"] = _parse_value(f, raw)
        elif attr == "range":
            f["range"] = _parse_range(f, raw)
        elif attr == "order":
            try:
                f["order"] = int(raw)
            except ValueError:
                raise Refused("order is a whole number (got %r)." % raw)
        else:
            raise Refused("A field has label, hint, default, units, range, tier, order or hidden (got %r)." % attr)
        _check_one(f, graph, manifest)
    seen = set()
    for f in manifest["fields"]:
        for b in f["bind"]:
            if (b["node"], b["input"]) in seen:
                raise Refused("%s.%s is bound by more than one field." % (b["node"], b["input"]))
            seen.add((b["node"], b["input"]))
    _write_manifest(r["dir"], manifest)


def _flag_of(r, flag):
    f = next((x for x in r["ap"]["flags"] if x["id"] == flag), None)
    if f is None:
        raise Refused("This draft has no flag called %s." % flag)
    return f


def answer(data_dir, did, flag, values):
    """`answer`: give the answer a flag asks for (section 7.4). `values` are the words after the flag."""
    r = read_draft(data_dir, did)
    f = _flag_of(r, flag)
    if f["kind"] != "answer":
        raise Refused("%s is not a question; %s." % (flag, "check it with ack" if f["kind"] == "ack" else "it blocks this template for good"))
    if flag == "licence":
        raise Refused("Confirm the licence with: licence %s --name TEXT --checked" % did)
    manifest = copy.deepcopy(r["manifest"])
    fields = {x["id"]: x for x in manifest["fields"]}
    values = [v for v in values]
    if not values or any(not str(v).strip() for v in values):
        raise Refused("That needs an answer.")
    if flag in ("picture_roles", "clip_roles", "audio_roles"):
        ids = f["data"]["fields"]
        if len(values) != len(ids):
            raise Refused("This template has %d slots, so give %d labels (got %d)." % (len(ids), len(ids), len(values)))
        for fid, label in zip(ids, values):
            if fid in fields:
                fields[fid]["label"] = label
    elif flag == "prompt_source":
        cands = r["ap"].get("candidates", {}).get("prompt") or []
        try:
            k = int(values[0])
        except ValueError:
            k = 0
        if not 1 <= k <= len(cands):
            raise Refused("Pick a number from 1 to %d." % len(cands))
        c = cands[k - 1]
        binds = [{"node": c["node"], "input": c["input"]}] + [{"node": n, "input": i} for n, i in c.get("also") or []]
        mine = {(b["node"], b["input"]) for b in binds}
        for x in manifest["fields"]:
            if x["id"] != "prompt" and any((b["node"], b["input"]) in mine for b in x["bind"]):
                raise Refused("That text is already used by the %s field." % x["label"])
        if "prompt" not in fields:
            raise Refused("The prompt field is hidden; show it first.")
        fields["prompt"]["bind"] = binds
        fields["prompt"]["default"] = r["graph"][c["node"]]["inputs"][c["input"]]
    elif flag == "length_unit":
        unit = values[0].strip()
        if "length" not in fields:
            raise Refused("The length field is hidden; show it first.")
        lf = fields["length"]
        lf["units"], lf["label"] = unit, "Length (%s)" % unit
        if unit == "frames":
            lf["range"] = [1, 2000]
        elif unit == "seconds":
            lf["range"] = [1, 60]
    elif flag.startswith("switch:"):
        fid = f["data"]["field"]
        if values[0].strip().lower() == "hide" and len(values) == 1:
            manifest["fields"] = [x for x in manifest["fields"] if x["id"] != fid]
        elif fid in fields:
            fields[fid]["label"] = " ".join(values)
    for x in manifest["fields"]:
        try:
            up._check_field(x, r["graph"], 0)
        except up.PackError as e:
            raise Refused(str(e))
    _write_manifest(r["dir"], manifest)
    f["resolved"], f["answer"] = True, list(values)
    _write_ap(r["dir"], r["ap"])


def ack(data_dir, did, flags):
    r = read_draft(data_dir, did)
    todo = []
    for flag in flags:
        f = _flag_of(r, flag)
        if f["kind"] == "block":
            raise Refused("%s cannot be acknowledged: this template can't be added yet." % flag)
        if f["kind"] == "answer":
            raise Refused("%s needs an answer, not a tick: use answer %s ..." % (flag, did))
        todo.append(f)
    for f in todo:
        f["resolved"] = True
    _write_ap(r["dir"], r["ap"])


def set_licence(data_dir, did, name, url, checked):
    if not checked:
        raise Refused("Confirm that you checked the licences of the models: pass --checked.")
    if not name or not str(name).strip():
        raise Refused("Say what the licence is (--name). \"Unknown\" is an answer.")
    r = read_draft(data_dir, did)
    manifest = copy.deepcopy(r["manifest"])
    manifest["licence"]["name"] = name
    manifest["licence"]["shippable"] = False
    if url:
        manifest["licence"]["url"] = url
    else:
        manifest["licence"].pop("url", None)
    _write_manifest(r["dir"], manifest)
    r["ap"]["licence"] = {"confirmed": True, "name": name, "url": url or None, "at": int(time.time())}
    for f in r["ap"]["flags"]:
        if f["id"] == "licence":
            f["resolved"], f["answer"] = True, [name]
    _write_ap(r["dir"], r["ap"])


def review(data_dir, did, oi, engine_list=None):
    """draft -> reviewed if every precondition holds (section 7.3). -> the unmet ones as
    sentences; an empty list means the pack is now reviewed."""
    r = read_draft(data_dir, did)
    if r["ap"]["state"] == "enabled":
        raise Refused("It is already added. Disable it first.")
    unmet = []
    for f in r["ap"]["flags"]:
        if f["kind"] == "block":
            unmet.append(f["text"])
    for f in r["ap"]["flags"]:
        if f["kind"] == "answer" and f["id"] != "licence" and not f["resolved"]:
            unmet.append("Needs your answer (%s): %s" % (f["id"], f["text"]))
        elif f["kind"] == "ack" and not f["resolved"]:
            unmet.append("Check this (%s): %s" % (f["id"], f["text"]))
    if not r["ap"]["licence"].get("confirmed"):
        unmet.append("The licence is not confirmed. Check the models' licences, then run: licence %s --name TEXT --checked" % did)
    if (r["manifest"].get("licence") or {}).get("shippable") is True:
        unmet.append("Packs made from templates stay unshippable.")
    try:
        _try_load(r["manifest"], r["graph"], engine_list)
    except up.PackError as e:
        unmet.append("BWF's loader would not accept it: %s" % e)
    v = validate_api_graph(r["graph"], oi)
    unmet += ["The graph is not valid for this ComfyUI: %s" % e for e in v["errors"]]
    if unmet:
        return unmet
    mh, gh = hashes(r["manifest"], r["graph"])
    r["ap"]["state"] = "reviewed"
    r["ap"]["reviewed"] = {"manifest_sha256": mh, "graph_sha256": gh, "at": int(time.time())}
    _write_ap(r["dir"], r["ap"])
    return []


def record_test(data_dir, did, record):
    r = read_draft(data_dir, did)
    if r["ap"]["state"] not in ("reviewed", "tested"):
        raise Refused("Review it before testing it." if r["ap"]["state"] == "draft" else "It is already added.")
    mh, gh = hashes(r["manifest"], r["graph"])
    r["ap"]["test"] = dict(record, looks_right=None, manifest_sha256=mh, graph_sha256=gh, at=int(time.time()))
    _write_ap(r["dir"], r["ap"])


def _test_current(r):
    t = r["ap"].get("test")
    if not t:
        raise Refused("Run a test first.")
    if (t["manifest_sha256"], t["graph_sha256"]) != hashes(r["manifest"], r["graph"]):
        raise Refused("The pack changed after that test. Test it again.")
    return t


def verdict(data_dir, did, looks_right):
    r = read_draft(data_dir, did)
    t = r["ap"].get("test")
    if not t:
        raise Refused("Run a test first.")
    if r["ap"]["state"] not in ("reviewed", "tested"):
        raise Refused("Review it again first: it changed since the test." if r["ap"]["state"] == "draft" else "It is already added.")
    _test_current(r)
    if looks_right:
        if not t.get("machine_pass"):
            raise Refused("The test failed: %s" % " ".join(t.get("reasons") or ["it made no usable file."]))
        t["looks_right"], r["ap"]["state"] = True, "tested"
    else:
        t["looks_right"], r["ap"]["state"] = False, "reviewed"
    _write_ap(r["dir"], r["ap"])
    return r["ap"]["state"]


def enable(data_dir, did, engine_list, ready):
    """tested -> enabled: copy manifest, graph and autopack.json into packs/<id>/ through a
    hidden staging folder. `ready` is readiness() against the tested lane, made just now.
    Takes effect after a BWF restart. -> the pack folder."""
    r = read_draft(data_dir, did)
    st = r["ap"]["state"]
    if st == "enabled":
        raise Refused("It is already added.")
    if st != "tested":
        raise Refused("Only a tested pack can be added (this one is %s)." % st)
    t = _test_current(r)
    if t.get("looks_right") is not True or not t.get("machine_pass"):
        raise Refused("Say that the test looks right first (verdict --looks-right).")
    if ready["state"] != "available":
        if ready["state"] == "needs_files":
            raise Refused("This ComfyUI lacks: %s." % "; ".join("%s (%s)" % (m["words"], m["template_file"]) for m in ready["missing"]))
        raise Refused(" ".join(ready["blocked"]))
    packs = os.path.join(data_dir, "packs")
    final = os.path.join(packs, did)
    if os.path.lexists(final):
        raise Refused("There is already a pack called %s in packs/, so nothing was added." % did)
    try:
        _try_load(r["manifest"], r["graph"], engine_list, exclude=None)
    except up.PackError as e:
        raise Refused("BWF's loader would not accept it now: %s" % e)
    now = int(time.time())
    stage = os.path.join(packs, ".tmp-" + did)
    shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(stage)
    try:
        ap = copy.deepcopy(r["ap"])
        ap["state"], ap["enabled"] = "enabled", {"at": now, "pack_dir": final}
        _write_manifest(stage, r["manifest"])
        _dump(os.path.join(stage, r["manifest"].get("graph") or "graph.api.json"), r["graph"], ensure_ascii=False)
        _dump(os.path.join(stage, "autopack.json"), ap, indent=1, ensure_ascii=False)
        _replace(stage, final)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    r["ap"]["state"], r["ap"]["enabled"] = "enabled", {"at": now, "pack_dir": final}
    _write_ap(r["dir"], r["ap"])
    return final


def disable(data_dir, did):
    r = read_draft(data_dir, did)
    if r["ap"]["state"] != "enabled":
        raise Refused("It is not added.")
    final = os.path.join(data_dir, "packs", did)
    try:
        theirs = _read(os.path.join(final, "autopack.json"))
    except (OSError, ValueError):
        theirs = None
    if not isinstance(theirs, dict) or theirs.get("id") != did:
        raise Refused("packs/%s is not a pack this tool made, so it was left alone." % did)
    shutil.rmtree(final)
    r["ap"]["state"], r["ap"]["enabled"] = "tested", None
    _write_ap(r["dir"], r["ap"])


def delete(data_dir, did):
    r = read_draft(data_dir, did)
    if r["ap"]["state"] == "enabled":
        raise Refused("Disable it first.")
    shutil.rmtree(r["dir"])
