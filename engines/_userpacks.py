"""Data packs: user workflow packs loaded from DATA_DIR/packs/<id>/.

A data pack is a folder holding ``manifest.json`` plus one ComfyUI API-format
graph (``graph.api.json`` by convention). ``load_user_packs`` turns each valid
one into an ordinary ENGINE dict, so it takes the same field-driven page, the
generic dispatch path, the ability / "X is missing" sentences and the licence
stamp as a pack written in Python. Nothing here knows a model or vendor name.

Trust rules (user content may never take the app down):
  * every pack is validated; an invalid one is SKIPPED with one problem line
    ("<folder>: <reason>"), never raised;
  * a manifest holds data only: bindings are {node, input} pairs against the
    graph, values are substituted as data, there is no code and no eval;
  * the manifest and graph are read only from inside the pack's own folder
    (realpath must stay under it, so `..`, absolute paths and symlinks out are
    refused) and only up to MAX_BYTES each;
  * packs are read at scan time from DATA_DIR/packs only -- there is no upload
    endpoint and no runtime reload.

Manifest (schema 1), all keys below, unknown keys are refused:

  schema   1
  id       str, equals the folder name; [a-z0-9][a-z0-9_-]{0,39}
  cap      str, lane capability the mode belongs to ("video", ...)
  mode     str, the mode's id (unique against every built-in mode, ability
           and capability name)
  label    str, the mode's name on the page
  note     optional str, one line saying when to pick this mode
  room     optional str, a room id from rooms.json
  graph    str, relative path of the API-format graph inside the folder
  fields   list of field descriptors (the engine contract's own keys) plus
           ``bind``: [{node, input, index?}]. ``index`` picks an item of an
           image_list / video_list value (default 0). ``required: true``
           makes a missing value an error instead of keeping the graph's own.
  requires list of {role, pool, node, input, match, words}: a model file the
           mode needs. ``match`` is {all, any, none, prefer} (lists of
           lower-case name fragments, like a built-in pack's role rule); the
           resolved file name is written into graph[node].inputs[input].
           The mode is available only when every role resolves on the lane.
  presets  optional list of {id, label, note?, values{field id: value}}: named starting
           values for the mode's recipe row. None declared gives one 'Default' (the page
           only refreshes its recipe line for a non-empty list).
  licence  {name, shippable (default false), attribution, url?, summary?}
  comment  optional str, ignored
"""

import copy
import json
import logging
import math
import os
import re

SCHEMA = 1
MAX_BYTES = 2 * 1024 * 1024          # manifest and graph, each
MAX_DEPTH = 32                       # JSON nesting, manifest and graph

# The loader pools server.py can read from a lane (server.py POOL_NODES keys).
# tests/test_user_packs.py checks this list against server.py so they cannot drift.
POOLS = frozenset((
    "unet", "clip", "vae", "lora", "checkpoint", "audio_encoder",
    "bg_removal", "upscale_model", "clip_vision", "latent_upscaler"))

FIELD_TYPES = frozenset((
    "text", "textarea", "select", "pool_select", "checkbox", "number", "int",
    "audio", "image", "image_list", "video_list", "model"))
FILE_TYPES = frozenset(("audio", "image", "image_list", "video_list", "model"))
LIST_TYPES = frozenset(("image_list", "video_list"))
# Dispatch-level request keys (server.py RESERVED_FIELD_IDS). "seed" is the
# one a pack may declare: dispatch always passes args["seed"].
RESERVED_IDS = frozenset(("lane", "kind", "mode", "quality", "recipe", "sequence_id", "slot_id"))

TOP_KEYS = frozenset(("schema", "id", "cap", "mode", "label", "note", "room", "graph",
                      "fields", "requires", "licence", "presets", "comment"))
FIELD_KEYS = frozenset(("id", "label", "type", "default", "hint", "tier", "group", "order",
                        "units", "range", "ui_range", "options", "max", "enabled_when",
                        "disabled_reason", "aspect_warning", "pool", "match", "bind", "required"))
MANIFEST_ONLY = ("bind", "required")        # not part of the descriptor the page sees
REQUIRES_KEYS = frozenset(("role", "pool", "node", "input", "match", "words"))
LICENCE_KEYS = frozenset(("name", "shippable", "attribution", "url", "summary"))
MATCH_KEYS = frozenset(("all", "any", "none", "prefer"))

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}\Z")
_CAP_RE = re.compile(r"^[a-z][a-z0-9_]{0,23}\Z")
_ROLE_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,39}\Z")
_FIELD_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,39}\Z")

_log = logging.getLogger(__name__)


class PackError(Exception):
    """A reason one pack is skipped. The text is the problem line."""


def packs_dir(data_dir):
    return os.path.join(data_dir, "packs")


def _str(v):
    return isinstance(v, str) and bool(v.strip())


def _num(v):
    """A real, finite number (NaN / Infinity would break the page's JSON parse)."""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _no_constant(token):
    raise ValueError("%s is not valid JSON" % token)


def _too_deep(obj):
    """True when nesting exceeds MAX_DEPTH. Iterative: a deep file must not recurse."""
    stack = [(obj, 1)]
    while stack:
        cur, depth = stack.pop()
        if isinstance(cur, (dict, list)):
            if depth > MAX_DEPTH:
                return True
            for child in (cur.values() if isinstance(cur, dict) else cur):
                if isinstance(child, (dict, list)):
                    stack.append((child, depth + 1))
    return False


def _read_json(root, rel, what):
    """Parse a JSON file that must live inside `root` (realpath check, so a
    `..`, an absolute path or a symlink pointing out is refused)."""
    if not _str(rel):
        raise PackError("%s path must be a non-empty string" % what)
    if os.path.isabs(rel) or "\x00" in rel:
        raise PackError("%s path %r must be relative to the pack folder" % (what, rel))
    path = os.path.realpath(os.path.join(root, rel))
    if os.path.commonpath([root, path]) != root:
        raise PackError("%s path %r leaves the pack folder" % (what, rel))
    if not os.path.isfile(path):
        raise PackError("%s file %r is missing" % (what, rel))
    size = os.path.getsize(path)
    if size > MAX_BYTES:
        raise PackError("%s file %r is %d bytes, over the %d byte limit" % (what, rel, size, MAX_BYTES))
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise PackError("%s file %r is over the %d byte limit" % (what, rel, MAX_BYTES))
        data = json.loads(raw.decode("utf-8-sig"), parse_constant=_no_constant)
        if _too_deep(data):
            raise PackError("%s file %r is nested deeper than %d levels" % (what, rel, MAX_DEPTH))
        return data
    except PackError:
        raise
    except (OSError, ValueError, RecursionError) as e:       # JSONDecodeError, bad UTF-8, too deep
        raise PackError("%s file %r is not valid JSON (%s)" % (what, rel, e))


def _check_graph(graph):
    if not isinstance(graph, dict) or not graph:
        raise PackError("graph must be a non-empty object of node id -> node")
    for nid, node in graph.items():
        if not isinstance(node, dict) or not _str(node.get("class_type")) \
                or not isinstance(node.get("inputs"), dict):
            raise PackError("graph node %r needs a class_type string and an inputs object "
                            "(is this an API-format graph?)" % (nid,))


def _is_link(v):
    return isinstance(v, list) and len(v) == 2 and isinstance(v[0], str) and isinstance(v[1], int)


def _check_target(graph, node, inp, what):
    """node/input must name an existing, non-link graph input."""
    if not isinstance(node, str) or not isinstance(inp, str) or not node or not inp:
        raise PackError("%s needs string node and input (got %r, %r)" % (what, node, inp))
    if node not in graph:
        raise PackError("%s names node %r, which is not in the graph" % (what, node))
    inputs = graph[node]["inputs"]
    if inp not in inputs:
        raise PackError("%s names input %r, which node %r does not have" % (what, inp, node))
    if _is_link(inputs[inp]):
        raise PackError("%s targets %s.%s, which is wired to another node, not a value" % (what, node, inp))


def _check_rule(rule, what):
    if not isinstance(rule, dict) or not rule or set(rule) - MATCH_KEYS:
        raise PackError("%s must be an object with only %s" % (what, "/".join(sorted(MATCH_KEYS))))
    out = {}
    for k, v in rule.items():
        if not isinstance(v, list) or not all(_str(t) for t in v):
            raise PackError("%s.%s must be a list of non-empty strings" % (what, k))
        out[k] = [t.strip().lower() for t in v]
    if not (out.get("all") or out.get("any")):
        raise PackError("%s needs a non-empty 'all' or 'any' (it would match every file)" % what)
    return out


def _fits(f, d):
    """Whether `d` is a value the field's type can hold (used for defaults and preset values)."""
    return {"text": isinstance(d, str), "textarea": isinstance(d, str),
            "checkbox": isinstance(d, bool), "number": _num(d),
            "int": isinstance(d, int) and not isinstance(d, bool),
            "select": d in (f.get("options") or []) and not isinstance(d, (list, dict)),
            "pool_select": isinstance(d, str)}.get(f["type"], False)


def _check_presets(raw, fields):
    """Manifest presets -> the list the page's recipe row reads. None declared
    (absent or empty) gives one 'Default': the page refreshes its recipe summary
    only for a non-empty list, so an empty one would keep the previous room's."""
    if raw is None or raw == []:
        return [{"id": "default", "label": "Default", "note": "As set up in this pack.", "values": {}}]
    if not isinstance(raw, list):
        raise PackError("presets must be a list")
    by_id = {f["id"]: f for f in fields}
    out, seen = [], set()
    for i, p in enumerate(raw):
        if not isinstance(p, dict):
            raise PackError("presets[%d] must be an object" % i)
        if set(p) - {"id", "label", "note", "values"}:
            raise PackError("presets[%d] has unknown key(s) %s" % (i, ", ".join(sorted(set(p) - {"id", "label", "note", "values"}))))
        pid = p.get("id")
        if not isinstance(pid, str) or not _FIELD_ID_RE.match(pid):
            raise PackError("presets[%d].id must be a short identifier" % i)
        if pid in seen:
            raise PackError("preset id %r listed twice" % pid)
        seen.add(pid)
        if not _str(p.get("label")):
            raise PackError("preset %r needs a label" % pid)
        if "note" in p and not isinstance(p["note"], str):
            raise PackError("preset %r: note must be a string" % pid)
        values = p.get("values", {})
        if not isinstance(values, dict):
            raise PackError("preset %r: values must be an object" % pid)
        for k, v in values.items():
            f = by_id.get(k)
            if f is None:
                raise PackError("preset %r values name no field %r" % (pid, k))
            if f["type"] in FILE_TYPES:
                raise PackError("preset %r cannot set %r: an upload field" % (pid, k))
            if not _fits(f, v):
                raise PackError("preset %r: value %r does not fit field %r" % (pid, v, k))
        out.append({"id": pid, "label": p["label"], "note": p.get("note", ""), "values": copy.deepcopy(values)})
    return out


def _check_field(f, graph, idx):
    if not isinstance(f, dict):
        raise PackError("fields[%d] must be an object" % idx)
    bad = set(f) - FIELD_KEYS
    if bad:
        raise PackError("fields[%d] has unknown key(s) %s" % (idx, ", ".join(sorted(bad))))
    fid = f.get("id")
    if not isinstance(fid, str) or not _FIELD_ID_RE.match(fid):
        raise PackError("fields[%d].id must be a short identifier" % idx)
    if fid in RESERVED_IDS:
        raise PackError("field id %r is reserved by the request format" % fid)
    ftype = f.get("type")
    if ftype not in FIELD_TYPES:
        raise PackError("field %r has unknown type %r" % (fid, ftype))
    if not _str(f.get("label")):
        raise PackError("field %r needs a label" % fid)
    for k in ("hint", "group", "units", "disabled_reason", "aspect_warning"):
        if k in f and not isinstance(f[k], str):
            raise PackError("field %r: %s must be a string" % (fid, k))
    if "tier" in f and f["tier"] not in ("primary", "advanced"):
        raise PackError("field %r: tier must be primary or advanced" % fid)
    if "order" in f and (not isinstance(f["order"], int) or isinstance(f["order"], bool)):
        raise PackError("field %r: order must be an integer" % fid)
    if "required" in f and not isinstance(f["required"], bool):
        raise PackError("field %r: required must be true or false" % fid)
    if ftype in ("number", "int") and not _str(f.get("units")):
        raise PackError("field %r: a %s field needs units" % (fid, ftype))
    if ftype == "select":
        opts = f.get("options")
        if not isinstance(opts, list) or not opts or any(isinstance(o, (list, dict)) for o in opts):
            raise PackError("field %r: options must be a non-empty list of values" % fid)
    elif "options" in f:
        raise PackError("field %r: options belong to select fields only (a pool_select reads the lane)" % fid)
    if ftype == "pool_select":
        if f.get("pool") not in POOLS:
            raise PackError("field %r: pool %r is not one of %s" % (fid, f.get("pool"), ", ".join(sorted(POOLS))))
        _check_rule(f.get("match"), "field %r match" % fid)
    elif "pool" in f or "match" in f:
        raise PackError("field %r: pool/match belong to pool_select fields only" % fid)
    if ftype in LIST_TYPES:
        if "max" in f and (not isinstance(f["max"], int) or isinstance(f["max"], bool) or f["max"] < 1):
            raise PackError("field %r: max must be a whole number of at least 1" % fid)
    elif "max" in f:
        raise PackError("field %r: max belongs to image_list / video_list fields only" % fid)
    for k in ("range", "ui_range"):
        if k in f:
            r = f[k]
            if ftype not in ("number", "int") or not (isinstance(r, list) and len(r) == 2
                                                       and all(_num(x) for x in r) and r[0] <= r[1]):
                raise PackError("field %r: %s must be [low, high] on a number/int field" % (fid, k))
    if "ui_range" in f and "range" in f and not (f["range"][0] <= f["ui_range"][0]
                                                 and f["ui_range"][1] <= f["range"][1]):
        raise PackError("field %r: ui_range must lie inside range" % fid)
    if "default" in f and not _fits(f, f["default"]):
        raise PackError("field %r: default %r does not fit a %s field" % (fid, f["default"], ftype))
    binds = f.get("bind")
    if not isinstance(binds, list) or not binds:
        raise PackError("field %r needs a bind list (a field that changes nothing is a typo)" % fid)
    seen = set()
    for j, b in enumerate(binds):
        if not isinstance(b, dict) or set(b) - {"node", "input", "index"}:
            raise PackError("field %r bind[%d] must be {node, input[, index]}" % (fid, j))
        _check_target(graph, b.get("node"), b.get("input"), "field %r bind[%d]" % (fid, j))
        if "index" in b:
            if ftype not in LIST_TYPES:
                raise PackError("field %r bind[%d]: index belongs to image_list / video_list fields" % (fid, j))
            if not isinstance(b["index"], int) or isinstance(b["index"], bool) or b["index"] < 0:
                raise PackError("field %r bind[%d]: index must be a whole number, 0 or more" % (fid, j))
        key = (b["node"], b["input"])
        if key in seen:
            raise PackError("field %r binds %s.%s twice" % (fid, *key))
        seen.add(key)
    return fid


def _check_enabled_when(f, ids):
    ew = f.get("enabled_when")
    if ew is None:
        return
    if not isinstance(ew, dict) or set(ew) - {"field", "equals", "not_equals", "truthy"} \
            or ew.get("field") not in ids:
        raise PackError("field %r: enabled_when must name another field of this mode "
                        "(field + equals / not_equals / truthy)" % f["id"])


def _check_licence(lic):
    if not isinstance(lic, dict) or set(lic) - LICENCE_KEYS or not _str(lic.get("name")):
        raise PackError("licence must be an object with a name (and only %s)" % "/".join(sorted(LICENCE_KEYS)))
    for k in ("attribution", "url", "summary"):
        if k in lic and not isinstance(lic[k], str):
            raise PackError("licence.%s must be a string" % k)
    if "shippable" in lic and not isinstance(lic["shippable"], bool):
        raise PackError("licence.shippable must be true or false")
    return {"name": lic["name"], "shippable": lic.get("shippable", False),
            "attribution": lic.get("attribution", ""), **{k: lic[k] for k in ("url", "summary") if k in lic}}


def _plain(v):
    """A value that can only be data: never a list (a link to another node) or an object."""
    return isinstance(v, (str, bool, int)) or (isinstance(v, float) and math.isfinite(v))


def _builder(graph, fields, requires, roles_by_req):
    """The mode's graph builder: copy the graph, write bound field values and
    the resolved model file names. Pure data substitution."""
    def build(args, models):
        g = copy.deepcopy(graph)
        for f in fields:
            fid = f["id"]
            if fid in args:
                val = args[fid]
            elif "default" in f:
                val = f["default"]
            elif f.get("required"):
                raise KeyError(fid)           # _dispatch_generic words this as "<label> is needed for this."
            else:
                continue
            if val is None:
                if f.get("required"):
                    raise KeyError(fid)
                continue
            for b in f["bind"]:
                v = val
                if f["type"] in LIST_TYPES:
                    items = val if isinstance(val, (list, tuple)) else [val]
                    if not all(isinstance(x, str) for x in items):
                        raise ValueError("%s needs file names." % f["label"])
                    i = b.get("index", 0)
                    if i >= len(items):
                        if f.get("required") and i == 0:
                            raise KeyError(fid)
                        continue
                    v = items[i]
                if not _plain(v):
                    raise ValueError("%s needs a single plain value." % f["label"])
                g[b["node"]]["inputs"][b["input"]] = v
        for req, role in zip(requires, roles_by_req):
            name = models.get(role)
            if not name:
                raise ValueError("%s is not installed on this machine." % req["words"])
            g[req["node"]]["inputs"][req["input"]] = name
        return g
    return build


def _load_one(folder, root, taken):
    """Validate one pack folder -> an ENGINE dict, or raise PackError."""
    manifest = _read_json(root, "manifest.json", "manifest")
    if not isinstance(manifest, dict):
        raise PackError("manifest must be a JSON object")
    if manifest.get("schema") != SCHEMA:
        raise PackError("unknown schema version %r (this app reads schema %d)" % (manifest.get("schema"), SCHEMA))
    bad = set(manifest) - TOP_KEYS
    if bad:
        raise PackError("unknown manifest key(s) %s" % ", ".join(sorted(bad)))
    pid, cap, mode = manifest.get("id"), manifest.get("cap"), manifest.get("mode")
    if not isinstance(pid, str) or not _ID_RE.match(pid) or "__" in pid:
        raise PackError("id must be lower-case letters, digits, '-' or '_' (got %r)" % (pid,))
    if pid != folder:
        raise PackError("id %r must equal the folder name %r" % (pid, folder))
    if pid in taken["ids"]:
        raise PackError("id %r is already used by another pack" % pid)
    if not isinstance(cap, str) or not _CAP_RE.match(cap):
        raise PackError("cap must be a short lower-case word (got %r)" % (cap,))
    if not isinstance(mode, str) or not _CAP_RE.match(mode):
        raise PackError("mode must be a short lower-case word (got %r)" % (mode,))
    if (cap, mode) in taken["modes"]:
        raise PackError("mode %r under cap %r is already provided by another pack" % (mode, cap))
    if mode in taken["abilities"] or mode in taken["caps"]:
        raise PackError("mode %r clashes with an existing ability or capability name" % mode)
    if not _str(manifest.get("label")):
        raise PackError("label must be a non-empty string")
    for k in ("note", "room", "comment"):
        if k in manifest and not _str(manifest[k]):
            raise PackError("%s must be a non-empty string when present" % k)

    graph = _read_json(root, manifest.get("graph"), "graph")
    _check_graph(graph)

    raw_fields = manifest.get("fields", [])
    if not isinstance(raw_fields, list):
        raise PackError("fields must be a list")
    fields, ids, used = [], set(), set()
    for i, f in enumerate(raw_fields):
        fid = _check_field(f, graph, i)
        if fid in ids:
            raise PackError("duplicate field id %r" % fid)
        ids.add(fid)
        for b in f["bind"]:
            key = (b["node"], b["input"])
            if key in used:
                raise PackError("%s.%s is bound by more than one field" % key)
            used.add(key)
        fields.append(f)
    for f in fields:
        _check_enabled_when(f, ids)

    raw_req = manifest.get("requires", [])
    if not isinstance(raw_req, list):
        raise PackError("requires must be a list")
    requires, roles, words, role_seen = [], {}, {}, set()
    internal = []
    for i, r in enumerate(raw_req):
        if not isinstance(r, dict) or set(r) != REQUIRES_KEYS:
            raise PackError("requires[%d] must have exactly %s" % (i, "/".join(sorted(REQUIRES_KEYS))))
        if not isinstance(r["role"], str) or not _ROLE_RE.match(r["role"]) or "__" in r["role"]:
            raise PackError("requires[%d].role must be a short lower-case identifier" % i)
        if r["role"] in role_seen:
            raise PackError("requires lists role %r twice" % r["role"])
        role_seen.add(r["role"])
        if r["pool"] not in POOLS:
            raise PackError("requires[%d].pool %r is not one of %s" % (i, r["pool"], ", ".join(sorted(POOLS))))
        if not _str(r["words"]):
            raise PackError("requires[%d].words must be a plain-English name for the file" % i)
        _check_target(graph, r["node"], r["input"], "requires[%d]" % i)
        key = (r["node"], r["input"])
        if key in used:
            raise PackError("%s.%s is written by both a field and a requires entry (or twice)" % key)
        used.add(key)
        rule = _check_rule(r["match"], "requires[%d].match" % i)
        role = "u__%s__%s" % (pid.replace("-", "_"), r["role"])
        if role in taken["roles"]:
            raise PackError("model role %r collides with an existing role" % role)
        roles[role] = (r["pool"], rule)
        words[role] = r["words"]
        internal.append(role)
        requires.append(r)

    presets = _check_presets(manifest.get("presets"), fields)
    licence = _check_licence(manifest.get("licence", {"name": "Not stated", "shippable": False}))

    desc = {}
    clean = []
    for f in fields:
        d = {k: copy.deepcopy(v) for k, v in f.items() if k not in MANIFEST_ONLY}
        clean.append(d)
    label = manifest["label"]
    primary = internal[0] if internal else None

    def describe(models):
        # The lane headline asks with every model; describe_mode() asks with the
        # primary role alone. Only the second is ours to answer.
        have = [k for k, v in (models or {}).items() if v]
        return label if primary and have == [primary] else ""

    build = _builder(graph, fields, requires, internal)
    engine = {
        "id": pid, "cap": cap,
        "roles": roles,
        "provides": {mode: list(internal)},
        "words": words,
        "graphs": {mode: build},
        "describe": describe,
        "fields": {mode: clean},
        "mode_words": {mode: label},
        "presets": {mode: presets},
        "licence": licence,
        "user_pack": True,
    }
    if "note" in manifest:
        engine["mode_notes"] = {mode: manifest["note"]}
    if "room" in manifest:
        engine["mode_rooms"] = {mode: manifest["room"]}
    taken["ids"].add(pid)
    taken["modes"].add((cap, mode))
    taken["abilities"].add(mode)
    taken["roles"].update(roles)
    return engine


def load_user_packs(data_dir, builtin=()):
    """Scan ``data_dir/packs`` -> (engines, problems).

    ``builtin`` is the list of already-discovered ENGINE dicts; a data pack may
    not reuse their ids, (cap, mode) pairs, ability names, capability names or
    model role names. Never raises: a broken pack lands in `problems` as one
    "<folder>: <reason>" line (also logged) and the rest still load.
    """
    engines, problems = [], []
    taken = {"ids": {p.get("id") for p in builtin},
             "modes": {(p.get("cap"), m) for p in builtin for m in (p.get("graphs") or {})},
             "abilities": {a for p in builtin for a in (p.get("provides") or {})},
             "caps": {p.get("cap") for p in builtin},
             "roles": {r for p in builtin for r in (p.get("roles") or {})}}

    def skip(name, why):
        line = "%s: %s" % (name, why)
        problems.append(line)
        _log.warning("data pack skipped -- %s", line)

    try:
        base = os.path.realpath(packs_dir(data_dir))
        if not os.path.isdir(base):
            return engines, problems
        names = sorted(os.listdir(base))
    except OSError as e:
        skip("packs", "cannot read the packs folder (%s)" % e)
        return engines, problems
    for name in names:
        if name.startswith((".", "_")):
            continue
        path = os.path.join(base, name)
        if not os.path.isdir(path):
            continue
        root = os.path.realpath(path)
        try:
            if os.path.commonpath([base, root]) != base or root == base:
                raise PackError("folder resolves outside the packs folder")
            engines.append(_load_one(name, root, taken))
        except PackError as e:
            skip(name, e)
        except Exception as e:          # user content must never take the app down
            skip(name, "unexpected %s: %s" % (type(e).__name__, e))
    return engines, problems
