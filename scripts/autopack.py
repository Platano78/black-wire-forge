#!/usr/bin/env python3
"""Make a Black Wire Forge mode from a ComfyUI template, from the terminal.

    python3 scripts/autopack.py [--data DIR] [--config FILE] COMMAND

  doctor   --lane ID|URL                  converter status, lane versions; converts nothing
  list     --lane ID|URL [--media M] [--ready]
  convert  --lane ID|URL --template NAME [--api-json FILE] [--id ID]
  show     ID [--lane ID|URL]             fields, flags, models + readiness, licence, state
  set      ID KEY=VALUE...                label/hint/default/units/range/tier/order/hidden of a field; label/note/room/mode
  answer   ID FLAG VALUE...               give the answer a flag asks for
  ack      ID FLAG...                     tick the checks a flag asks for
  licence  ID --name TEXT [--url URL] --checked
  review   ID                             draft -> reviewed, if nothing is open
  test     ID --lane ID|URL [--input FIELD=PATH ...] [--timeout S]     ONE render on the lane
  verdict  ID (--looks-right | --wrong)   your look at the test render
  enable   ID  |  disable ID  |  delete ID [--yes]
  updates  [--lane ID|URL]                templates that changed on their lane since a draft was made

Drafts live in DATA/packs-draft/<id>/; `enable` copies three files into DATA/packs/<id>/ and
BWF loads it at its next start. Nothing is ever enabled except by `enable`.

Exit codes: 0 ok; 2 usage; 3 refused (the sentence is on stderr); 4 lane unreachable;
5 converter unavailable. --data defaults to GENCENTER_DATA, else <repo>/data. --lane ID is read
from config.json (never written); --lane URL is used as given. This script never imports server.py.
"""

import argparse
import hashlib
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import engines                                   # noqa: E402
from engines import _autopack as ap              # noqa: E402
from engines import _autopack_convert as conv    # noqa: E402
from engines import _comfy                       # noqa: E402
from engines import _userpacks as up             # noqa: E402

RESTART_ADDED = "Added. Restart Black Wire Forge to load it."
RESTART_REMOVED = "Removed. Restart Black Wire Forge to unload it."
PIN_NOTE = 'Config pins ("models" in config.json) are not applied to drafts.'


class Usage(Exception):
    pass


class LaneError(Exception):
    """The lane did not answer."""


class LaneHTTP(Exception):
    def __init__(self, code, body):
        super().__init__("HTTP %s" % code)
        self.code, self.body = code, body


def out(s=""):
    print(s)


# ----------------------------------------------------------------------------- the lane

class Lane:
    def __init__(self, url, name=None):
        self.url = url.rstrip("/")
        self.name = name or self.url

    def _call(self, req, timeout):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            raise LaneHTTP(e.code, e.read())
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise LaneError("%s is not answering (%s)." % (self.name, getattr(e, "reason", e)))

    def get_bytes(self, path, timeout=60):
        return self._call(urllib.request.Request(self.url + path), timeout)

    def get_json(self, path, timeout=60):
        try:
            return json.loads(self.get_bytes(path, timeout))
        except ValueError:
            raise LaneError("%s answered %s with something that is not JSON." % (self.name, path))

    def post_json(self, path, obj, timeout=60):
        req = urllib.request.Request(self.url + path, data=json.dumps(obj).encode(), method="POST", headers={"Content-Type": "application/json"})
        return json.loads(self._call(req, timeout) or b"{}")

    def upload(self, filepath, timeout=300):
        """POST /upload/image (any media type) -> the name the lane gives the file."""
        boundary = "----bwf" + uuid.uuid4().hex
        with open(filepath, "rb") as f:
            data = f.read()
        fname = os.path.basename(filepath)
        ctype = mimetypes.guess_type(fname)[0] or "application/octet-stream"
        body = b""
        for k, v in (("type", "input"), ("overwrite", "true")):
            body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (boundary, k, v)).encode()
        body += ("--%s\r\nContent-Disposition: form-data; name=\"image\"; filename=\"%s\"\r\nContent-Type: %s\r\n\r\n"
                 % (boundary, fname.replace('"', "_"), ctype)).encode() + data + ("\r\n--%s--\r\n" % boundary).encode()
        req = urllib.request.Request(self.url + "/upload/image", data=body, method="POST",
                                     headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
        res = json.loads(self._call(req, timeout))
        name = res.get("name")
        if not name:
            raise ap.Refused("%s did not accept the upload of %s." % (self.name, fname))
        return (res["subfolder"] + "/" + name) if res.get("subfolder") else name

    def stats(self):
        s = self.get_json("/system_stats", 15)
        system = s.get("system") or {}
        pk = {p.get("name"): p.get("installed") for p in system.get("comfy_package_versions") or [] if isinstance(p, dict)}
        dev = (s.get("devices") or [{}])[0]
        return {"comfyui_version": system.get("comfyui_version"), "frontend_version": pk.get("comfyui-frontend-package"),
                "templates_version": pk.get("comfyui-workflow-templates"), "vram_total": dev.get("vram_total")}

    def object_info(self):
        return self.get_json("/object_info", 180)

    def index(self):
        return self.get_json("/templates/index.json", 60)

    def template_bytes(self, name):
        try:
            return self.get_bytes("/templates/%s.json" % urllib.parse.quote(name, safe=""), 60)
        except LaneHTTP as e:
            raise ap.Refused("%s has no template file called %s." % (self.name, name) if e.code == 404 else "%s refused to give %s (HTTP %s)." % (self.name, name, e.code))


def load_config(path):
    path = path or os.environ.get("GENCENTER_CONFIG") or os.path.join(ROOT, "config.json")
    try:
        with open(path, encoding="utf-8-sig") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}


def resolve_lane(value, config_path):
    if not value:
        raise Usage("Say which ComfyUI with --lane (an id from config.json, or its URL).")
    if value.startswith(("http://", "https://")):
        return Lane(value)
    cfg = load_config(config_path)
    for lane in cfg.get("lanes") or []:
        if isinstance(lane, dict) and lane.get("id") == value:
            if lane.get("kind", "comfy") != "comfy":
                raise ap.Refused("The lane %s is not a ComfyUI lane." % value)
            return Lane("http://%s:%s" % (lane.get("host", "127.0.0.1"), lane.get("port", 8188)), lane.get("name") or value)
    raise Usage("There is no lane called %r in %s. Pass the lane's URL instead (for example http://127.0.0.1:8188)."
                % (value, config_path or os.environ.get("GENCENTER_CONFIG") or os.path.join(ROOT, "config.json")))


# ----------------------------------------------------------------------------- printing

def table(headers, rows):
    rows = [[str(c) for c in r] for r in rows]
    widths = [min(60, max(len(h), *(len(r[i]) for r in rows))) if rows else len(h) for i, h in enumerate(headers)]
    lines = ["  ".join(h.ljust(w) for h, w in zip(headers, widths)).rstrip()]
    for r in rows:
        lines.append("  ".join((c if len(c) <= w else c[:w - 1] + "~").ljust(w) for c, w in zip(r, widths)).rstrip())
    return "\n".join(lines)


def lane_facts(lane):
    """What readiness needs from a lane: its object_info, pools, small-card flag."""
    stats = lane.stats()
    oi = lane.object_info()
    pools = {p: _comfy.pool_names_from_object_info(oi, p) for p in _comfy.POOL_NODES}
    return stats, oi, pools, _comfy.small_card_for(stats.get("vram_total"))


def draft_sources(r):
    p = os.path.join(r["dir"], "source", "template.ui.json")
    try:
        with open(p, encoding="utf-8") as f:
            return ap.template_model_sources(json.load(f))
    except (OSError, ValueError):
        return {}


def readiness_for(r, lane):
    stats, oi, pools, small = lane_facts(lane)
    return ap.readiness(r["manifest"], r["graph"], r["ap"]["flags"], oi, pools, small, sources=draft_sources(r)), oi


def describe_readiness(rd):
    if rd["state"] == "available":
        return "Readiness: available (this ComfyUI has everything it needs)."
    if rd["state"] == "needs_files":
        lines = ["Readiness: needs files on this ComfyUI:"]
        for m in rd["missing"]:
            lines.append("  - %s (%s)%s%s" % (m["words"], m["template_file"], ", folder %s" % m["directory"] if m.get("directory") else "",
                                              ", from %s" % m["url"] if m.get("url") else ""))
        return "\n".join(lines)
    return "\n".join(["Readiness: blocked, can't be added yet:"] + ["  - %s" % s for s in rd["blocked"]])


# ----------------------------------------------------------------------------- commands

def cmd_doctor(a):
    lane = resolve_lane(a.lane, a.config)
    st = lane.stats()
    entries = ap.index_entries(lane.index())
    ok, sentence = conv.converter_status(sandbox=_sandbox(a.config))
    out("Lane: %s (%s)" % (lane.name, lane.url))
    out("ComfyUI:   %s" % (st["comfyui_version"] or "not reported"))
    out("Frontend:  %s" % (st["frontend_version"] or "not reported"))
    out("Templates: %s (%d listed, %d that run locally)" % (st["templates_version"] or "not reported", len(entries), sum(1 for e in entries.values() if e["local"])))
    out("Converter: %s - %s" % ("ready" if ok else "unavailable", sentence))
    out("Nothing was converted or changed.")
    return 0


def _ready_set(lane, entries):
    oi = lane.object_info()
    known = set(oi)
    folders, listing = None, {}

    def folder_files(d):
        nonlocal folders
        if folders is None:
            try:
                folders = set(lane.get_json("/models", 30))
            except (LaneHTTP, LaneError):
                folders = set()
        if d not in folders:
            return None
        if d not in listing:
            try:
                listing[d] = {ap._base(f) for f in lane.get_json("/models/%s" % urllib.parse.quote(d, safe=""), 30) if isinstance(f, str)}
            except (LaneHTTP, LaneError):
                listing[d] = set()
        return listing[d]

    ok = set()
    for name, e in entries.items():
        if not e["local"] or name.startswith("api_"):
            continue
        try:
            ui = json.loads(lane.template_bytes(name))
        except (ap.Refused, ValueError, LaneError):
            continue
        if ap.template_node_types(ui) - known:
            continue
        good = True
        for base, src in ap.template_model_sources(ui).items():
            if src.get("directory"):
                files = folder_files(src["directory"])
                if files is None or base not in files:
                    good = False
                    break
        if good:
            ok.add(name)
    return ok


def cmd_list(a):
    lane = resolve_lane(a.lane, a.config)
    entries = ap.index_entries(lane.index())
    rows = [e for e in entries.values() if not a.media or e["type"] == a.media]
    if a.ready:
        ready = _ready_set(lane, {e["name"]: e for e in rows})
        rows = [e for e in rows if e["name"] in ready]
    out(table(["template", "kind", "local", "title"], [(e["name"], e["type"], "yes" if e["local"] else "paid", e["title"]) for e in rows]))
    out("%d template(s)." % len(rows))
    return 0


def _sandbox(config_path):
    pb = load_config(config_path).get("pack_builder")
    return not (isinstance(pb, dict) and pb.get("browser_sandbox") is False)


def cmd_convert(a):
    lane = resolve_lane(a.lane, a.config)
    st = lane.stats()
    entries = ap.index_entries(lane.index())
    entry = entries.get(a.template)
    if entry is None:
        raise ap.Refused("%s does not list a template called %s." % (lane.name, a.template))
    if a.template.startswith("api_") or not entry["local"]:
        raise ap.Refused(ap.PAID)
    existing = ap.find_draft(a.data, a.lane, a.template)
    if existing:
        out("There is already a draft for this template: %s" % existing)
        return 0
    ui_bytes = lane.template_bytes(a.template)
    try:
        ui = json.loads(ui_bytes)
    except ValueError:
        raise ap.Refused("%s gave a template file that is not JSON." % lane.name)
    if a.api_json:
        try:
            with open(a.api_json, encoding="utf-8-sig") as f:
                converted = json.load(f)
        except (OSError, ValueError) as e:
            raise ap.Refused("Cannot read %s as JSON (%s)." % (a.api_json, e))
        if not (isinstance(converted, dict) and converted and all(isinstance(n, dict) and isinstance(n.get("class_type"), str) and isinstance(n.get("inputs"), dict) for n in converted.values())):
            raise ap.Refused("%s is not an API-format graph. In ComfyUI use Workflow > Export (API), not Save." % a.api_json)
        converter, frontend, blocked = "export", None, []
    else:
        res = conv.convert(lane.url, ui, sandbox=_sandbox(a.config))
        converted, frontend, converter, blocked = res["prompt"], res.get("frontend_version"), "browser", res.get("blocked") or []
    oi = lane.object_info()
    pools = {p: _comfy.pool_names_from_object_info(oi, p) for p in _comfy.POOL_NODES}
    taken = ap.taken_for(a.data, engines.packs())
    draft = ap.synthesize(a.template, ui, converted, oi, entry, pools, taken, lane_name=lane.name, small_card=_comfy.small_card_for(st.get("vram_total")))
    if a.id:
        if not up._ID_RE.match(a.id) or "__" in a.id:
            raise ap.Refused("An id is lower-case letters, digits, '-' or '_' (got %r)." % a.id)
        if a.id in taken["ids"]:
            raise ap.Refused("The id %s is already used." % a.id)
        old = draft["manifest"]["id"]
        draft["manifest"]["id"] = a.id
        for n in draft["graph"].values():
            if n.get("inputs", {}).get("filename_prefix") == "bwf/" + old:
                n["inputs"]["filename_prefix"] = "bwf/" + a.id
    versions = {"comfyui_version": st["comfyui_version"], "frontend_version": st["frontend_version"] or frontend, "templates_version": st["templates_version"]}
    did, created = ap.create_draft(a.data, draft, lane=a.lane, lane_name=lane.name, template=a.template, title=entry["title"], category=entry["category"],
                                   versions=versions, template_bytes=ui_bytes, converted=converted, converter=converter)
    m = draft["manifest"]
    out("Made draft %s (mode %s, %s)." % (did, m["mode"], m["cap"]))
    out("%d thing(s) to look at: %s" % (len(draft["flags"]), ", ".join(f["id"] for f in draft["flags"])))
    out("Next: python3 scripts/autopack.py show %s --lane %s" % (did, a.lane))
    if blocked:
        out("(%d page request(s) outside the lane were refused during conversion.)" % len(blocked))
    return 0


def cmd_show(a):
    r = ap.read_draft(a.data, a.id)
    m, a_ = r["manifest"], r["ap"]
    src = a_["source"]
    out("Draft %s   state: %s" % (r["id"], a_["state"]))
    out("Template %s on %s (ComfyUI %s, frontend %s, templates %s)" % (src["template"], src["lane_name"] or src["lane"], src.get("comfyui_version"), src.get("frontend_version"), src.get("templates_version")))
    out("Mode %s (%s)   label: %s" % (m["mode"], m["cap"], m["label"]))
    lic = a_["licence"]
    out("Licence: %s. %s Stays private (not shippable)." % (m["licence"]["name"], "Confirmed." if lic.get("confirmed") else "NOT confirmed yet."))
    out()
    out("Fields")
    out(table(["id", "label", "kind", "default", "where it goes"],
              [(f["id"], f["label"], f["type"] + ("*" if f.get("required") else ""), ("" if f.get("default") is None else str(f["default"]).replace("\n", " ")[:40]),
                ", ".join("%s.%s" % (r["graph"][b["node"]]["class_type"], b["input"]) + (" [%s]" % b["node"]) for b in f["bind"]))
               for f in m["fields"]]))
    out("(* = needed for every run)")
    qs = [f for f in a_["flags"] if f["kind"] == "answer" and f["id"] != "licence"]
    out()
    out("Needs your answer" + ("" if qs else ": nothing"))
    for f in qs:
        out("  [%s] %s%s" % (f["id"], f["text"], "   (answered: %s)" % " ".join(f["answer"]) if f["resolved"] else ""))
        if f["id"] == "prompt_source":
            for i, c in enumerate(a_.get("candidates", {}).get("prompt") or [], 1):
                out("      %d. %s - %s" % (i, c["title"] or "node %s" % c["node"], c["excerpt"]))
        if f["id"].endswith("_roles"):
            for fid in f["data"].get("fields", []):
                fld = next((x for x in m["fields"] if x["id"] == fid), None)
                if fld:
                    out("      %s: %s%s" % (fid, fld["label"], "  (%s)" % fld["hint"] if fld.get("hint") else ""))
    cs = [f for f in a_["flags"] if f["kind"] == "ack"]
    out()
    out("Check these" + ("" if cs else ": nothing"))
    for f in cs:
        out("  [%s] %s %s" % (f["id"], "(checked)" if f["resolved"] else "(open)", f["text"]))
    bs = [f for f in a_["flags"] if f["kind"] == "block"]
    if bs:
        out()
        out("Blocked")
        for f in bs:
            out("  %s" % f["text"])
    out()
    out("Models")
    lane_value = a.lane or src["lane"]
    rd = None
    try:
        lane = resolve_lane(lane_value, a.config)
        rd, _oi = readiness_for(r, lane)
    except Usage as e:
        out("  (lane not checked: %s)" % e)
    except LaneError as e:
        out("  (lane not checked: %s)" % e)
    if rd is not None:
        out(table(["role", "file it will use here", "why", "note"],
                  [(x["role"], x["file"] or "-", x["why"], "different file than the template (%s)" % x["template_file"] if x["substituted"] else "") for x in rd["roles"]]) if rd["roles"] else "  none to check")
        out()
        out(describe_readiness(rd))
    else:
        out(table(["role", "template file"], [(q["role"], r["graph"][q["node"]]["inputs"][q["input"]]) for q in m["requires"]]) if m["requires"] else "  none to check")
    out(PIN_NOTE)
    return 0


def cmd_set(a):
    ap.edit(a.data, a.id, a.assignments)
    out("Saved. (A reviewed pack goes back to draft; review it again.)")
    return 0


def cmd_answer(a):
    ap.answer(a.data, a.id, a.flag, a.values)
    out("Answered %s." % a.flag)
    return 0


def cmd_ack(a):
    ap.ack(a.data, a.id, a.flags)
    out("Checked: %s." % ", ".join(a.flags))
    return 0


def cmd_licence(a):
    ap.set_licence(a.data, a.id, a.name, a.url, a.checked)
    out("Licence recorded: %s. The mode stays private (not shippable)." % a.name)
    return 0


def _source_lane(r, override, config):
    return resolve_lane(override or r["ap"]["source"]["lane"], config)


def cmd_review(a):
    r = ap.read_draft(a.data, a.id)
    lane = _source_lane(r, a.lane, a.config)
    oi = lane.object_info()
    unmet = ap.review(a.data, a.id, oi, engines.packs())
    if unmet:
        print("Not ready to review yet:", file=sys.stderr)
        for s in unmet:
            print("  - " + s, file=sys.stderr)
        return 3
    out("Reviewed. Next: python3 scripts/autopack.py test %s --lane %s --input FIELD=PATH" % (a.id, r["ap"]["source"]["lane"]))
    return 0


def cmd_test(a):
    lane = resolve_lane(a.lane, a.config)
    r = ap.read_draft(a.data, a.id)
    if r["ap"]["state"] not in ("reviewed", "tested"):
        raise ap.Refused("Review it before testing it." if r["ap"]["state"] == "draft" else "It is already added.")
    m = r["manifest"]
    inputs = {}
    for item in a.input or []:
        if "=" not in item:
            raise Usage("Write each input as FIELD=PATH (got %r)." % item)
        fid, path = item.split("=", 1)
        if fid not in {f["id"] for f in m["fields"]}:
            raise ap.Refused("There is no field called %s." % fid)
        inputs.setdefault(fid, []).append(path)
    file_fields = [f for f in m["fields"] if f["type"] in up.FILE_TYPES]
    for f in file_fields:
        if f.get("required") and f["id"] not in inputs:
            raise ap.Refused("%s is needed for the test: pass --input %s=PATH." % (f["label"], f["id"]))
    for fid, paths in inputs.items():
        if fid not in {f["id"] for f in file_fields}:
            raise ap.Refused("%s does not take a file." % fid)
        for p in paths:
            if not os.path.isfile(p):
                raise ap.Refused("There is no file at %s." % p)
    rd, oi = readiness_for(r, lane)
    if rd["state"] != "available":
        raise ap.Refused("%s cannot run it yet. %s" % (lane.name, describe_readiness(rd)))
    names, args = {}, {}
    for fid, paths in inputs.items():
        f = next(x for x in file_fields if x["id"] == fid)
        up_names = [lane.upload(p) for p in paths]
        names[fid] = up_names
        args[fid] = up_names if f["type"] in up.LIST_TYPES else up_names[0]
    files = {x["role"]: x["file"] for x in rd["roles"] if x["file"]}
    graph = ap.build_test_graph(m, r["graph"], args, files)
    v = ap.validate_api_graph(graph, oi)
    if v["errors"] or v["missing_files"] or v["unknown_classes"]:
        raise ap.Refused("The graph is not valid for this ComfyUI: " + "; ".join(v["errors"] + ["%s names %s" % (x["input"], x["value"]) for x in v["missing_files"]]))
    try:
        res = lane.post_json("/prompt", {"prompt": graph, "client_id": "bwf-pack-" + uuid.uuid4().hex}, 120)
    except LaneHTTP as e:
        try:
            body = json.loads(e.body)
            msg = (body.get("error") or {}).get("message") if isinstance(body.get("error"), dict) else body.get("error")
            nodes = body.get("node_errors") or {}
        except ValueError:
            msg, nodes = None, {}
        raise ap.Refused("%s refused the prompt%s%s" % (lane.name, ": %s" % msg if msg else " (HTTP %s)" % e.code, " (%d node(s) with errors)" % len(nodes) if nodes else ""))
    pid = res.get("prompt_id")
    if not pid:
        raise ap.Refused("%s accepted the prompt but gave no job id." % lane.name)
    out("Queued on %s: %s. Waiting (up to %ds)." % (lane.name, pid, a.timeout))
    deadline, entry = time.time() + a.timeout, None
    while True:
        hist = lane.get_json("/history/%s" % urllib.parse.quote(pid, safe=""), 30)
        if isinstance(hist, dict) and hist.get(pid):
            entry = hist[pid]
            break
        if time.time() > deadline:
            break
        time.sleep(2)
    tdir = os.path.join(r["dir"], "test")
    saved, sizes = [], {}
    if entry is None:
        verdict = {"machine_pass": False, "reasons": ["The lane did not finish within %d seconds (job %s may still be running)." % (a.timeout, pid)]}
    else:
        os.makedirs(tdir, exist_ok=True)
        for o in _comfy.collect_outputs(entry):
            q = urllib.parse.urlencode({"filename": o["filename"], "subfolder": o["subfolder"], "type": o["type"]})
            try:
                data = lane.get_bytes("/view?" + q, 600)
            except LaneHTTP:
                sizes[o["filename"]] = 0
                continue
            dest = os.path.join(tdir, os.path.basename(o["filename"]))
            with open(dest, "wb") as f:
                f.write(data)
            saved.append(dest)
            sizes[o["filename"]] = len(data)
        verdict = ap.test_verdict(entry, m["cap"], sizes)
    ap.record_test(a.data, a.id, {"lane": a.lane, "job": pid, "prompt_id": pid, "inputs": names, "outputs": [os.path.basename(s) for s in saved],
                                  "machine_pass": verdict["machine_pass"], "reasons": verdict["reasons"]})
    for s in saved:
        out("Saved: %s" % s)
    if not verdict["machine_pass"]:
        print("Machine check failed:", file=sys.stderr)
        for s in verdict["reasons"]:
            print("  - " + s, file=sys.stderr)
        return 3
    out("Machine check passed. Look at the file, then: python3 scripts/autopack.py verdict %s --looks-right   (or --wrong)" % a.id)
    return 0


def cmd_verdict(a):
    state = ap.verdict(a.data, a.id, bool(a.looks_right))
    out("Recorded. State: %s." % state)
    return 0


def cmd_enable(a):
    r = ap.read_draft(a.data, a.id)
    t = r["ap"].get("test") or {}
    lane = resolve_lane(a.lane or t.get("lane") or r["ap"]["source"]["lane"], a.config)
    rd, _oi = readiness_for(r, lane)
    ap.enable(a.data, a.id, engines.packs(), rd)
    out(RESTART_ADDED)
    return 0


def cmd_disable(a):
    ap.disable(a.data, a.id)
    out(RESTART_REMOVED)
    return 0


def cmd_delete(a):
    r = ap.read_draft(a.data, a.id)
    if r["ap"]["state"] == "enabled":
        raise ap.Refused("Disable it first.")
    if not a.yes:
        if not sys.stdin.isatty():
            raise ap.Refused("Pass --yes to delete a draft without a terminal.")
        if input("Delete draft %s? [y/N] " % a.id).strip().lower() != "y":
            out("Left as it was.")
            return 0
    ap.delete(a.data, a.id)
    out("Deleted draft %s." % a.id)
    return 0


def cmd_updates(a):
    drafts = ap.list_drafts(a.data)
    if not drafts:
        out("No drafts.")
        return 0
    down, lanes, changed = False, {}, 0
    for r in drafts:
        src = r["ap"]["source"]
        key = a.lane or src["lane"]
        if key not in lanes:
            try:
                lane = resolve_lane(key, a.config)
                lanes[key] = (lane, ap.index_entries(lane.index()), lane.stats())
            except (Usage, LaneError) as e:
                lanes[key] = None
                print("%s: not checked (%s)" % (key, e), file=sys.stderr)
                down = True
        got = lanes[key]
        if got is None:
            continue
        lane, entries, st = got
        name = lane.name if a.lane else (src.get("lane_name") or lane.name)
        if src["template"] not in entries:
            out("%s: %s no longer lists this template." % (r["id"], name))
            changed += 1
            continue
        try:
            now = hashlib.sha256(lane.template_bytes(src["template"])).hexdigest()
        except ap.Refused:
            out("%s: %s no longer lists this template." % (r["id"], name))
            changed += 1
            continue
        if now != src.get("template_sha256"):
            out("%s: The template changed on %s since this was made." % (r["id"], name))
            changed += 1
        if st.get("frontend_version") and st["frontend_version"] != src.get("frontend_version"):
            out("%s: info: %s now runs frontend %s (this was made with %s)." % (r["id"], name, st["frontend_version"], src.get("frontend_version")))
    if not changed:
        out("All templates match what the drafts were made from.")
    return 4 if down else 0


# ----------------------------------------------------------------------------- main

def build_parser():
    p = argparse.ArgumentParser(prog="autopack.py", description="Make a Black Wire Forge mode from a ComfyUI template.")
    p.add_argument("--data", default=None, help="data folder (default: GENCENTER_DATA, else <repo>/data)")
    p.add_argument("--config", default=None, help="config.json to read lanes from (default: GENCENTER_CONFIG, else <repo>/config.json)")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(fn=fn)
        return sp

    sp = add("doctor", cmd_doctor, "converter status and the lane's versions")
    sp.add_argument("--lane", required=True)
    sp = add("list", cmd_list, "the lane's templates")
    sp.add_argument("--lane", required=True)
    sp.add_argument("--media")
    sp.add_argument("--ready", action="store_true", help="only templates this lane has the nodes and model files for")
    sp = add("convert", cmd_convert, "make a draft from a template")
    sp.add_argument("--lane", required=True)
    sp.add_argument("--template", required=True)
    sp.add_argument("--api-json")
    sp.add_argument("--id")
    sp = add("show", cmd_show, "a draft's fields, flags, models and readiness")
    sp.add_argument("id")
    sp.add_argument("--lane")
    sp = add("set", cmd_set, "edit a draft")
    sp.add_argument("id")
    sp.add_argument("assignments", nargs="+", metavar="KEY=VALUE")
    sp = add("answer", cmd_answer, "answer a question a flag asks")
    sp.add_argument("id")
    sp.add_argument("flag")
    sp.add_argument("values", nargs="+")
    sp = add("ack", cmd_ack, "tick a check")
    sp.add_argument("id")
    sp.add_argument("flags", nargs="+")
    sp = add("licence", cmd_licence, "confirm the licence")
    sp.add_argument("id")
    sp.add_argument("--name", required=True)
    sp.add_argument("--url")
    sp.add_argument("--checked", action="store_true")
    sp = add("review", cmd_review, "draft -> reviewed")
    sp.add_argument("id")
    sp.add_argument("--lane")
    sp = add("test", cmd_test, "one test render on the lane")
    sp.add_argument("id")
    sp.add_argument("--lane", required=True)
    sp.add_argument("--input", action="append", metavar="FIELD=PATH")
    sp.add_argument("--timeout", type=int, default=1800)
    sp = add("verdict", cmd_verdict, "record whether the test looks right")
    sp.add_argument("id")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--looks-right", action="store_true")
    g.add_argument("--wrong", action="store_true")
    sp = add("enable", cmd_enable, "copy a tested draft into packs/")
    sp.add_argument("id")
    sp.add_argument("--lane")
    sp = add("disable", cmd_disable, "remove an added pack")
    sp.add_argument("id")
    sp = add("delete", cmd_delete, "delete a draft")
    sp.add_argument("id")
    sp.add_argument("--yes", action="store_true")
    sp = add("updates", cmd_updates, "templates that changed since a draft was made")
    sp.add_argument("--lane")
    return p


def main(argv=None):
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 2
    args.data = os.path.abspath(args.data or os.environ.get("GENCENTER_DATA") or os.path.join(ROOT, "data"))
    os.environ["GENCENTER_DATA"] = args.data         # engines reads its packs from here
    try:
        return args.fn(args)
    except Usage as e:
        print("usage: %s" % e, file=sys.stderr)
        return 2
    except ap.Refused as e:
        print(e.sentence, file=sys.stderr)
        return 3
    except LaneError as e:
        print(str(e), file=sys.stderr)
        return 4
    except LaneHTTP as e:
        print("The lane answered with an error (HTTP %s)." % e.code, file=sys.stderr)
        return 4
    except conv.ConverterUnavailable as e:
        print(e.sentence, file=sys.stderr)
        return 5


if __name__ == "__main__":
    sys.exit(main())
