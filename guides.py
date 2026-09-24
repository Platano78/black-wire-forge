"""Room guides: the persona each room's helper conversation speaks as.

A room in rooms.json may name a guide (``"guide": "<id>"``). The guide lives
in ``guides/<id>/``: a ``guide.json`` (loader metadata) naming the persona
file, two system-prompt projections (compact, verbose), a knowledge file,
per-projection reply caps, a greeting, and the plain guidance shown when no
helper is configured.

Every guide rooms.json names is loaded once, at startup. A named guide that
is missing or broken is a startup refusal (GuideError, one plain sentence
naming the file), never a room that quietly has no guide.

Stdlib only. Nothing here names a model.
"""
import json
import os
import re

APP_DIR = os.path.dirname(os.path.abspath(__file__))
GUIDES_DIR = os.path.join(APP_DIR, "guides")
ROOMS_PATH = os.path.join(APP_DIR, "rooms.json")
VERBOSITIES = ("compact", "verbose")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class GuideError(ValueError):
    """A guide rooms.json names cannot be loaded. str() is the sentence."""


def _rel(path):
    return os.path.relpath(path, APP_DIR)


def _read_text(path):
    if not os.path.isfile(path):
        raise GuideError("%s is missing." % _rel(path))
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if not text.strip():
        raise GuideError("%s is empty." % _rel(path))
    return text


def _read_json(path):
    try:
        return json.loads(_read_text(path))
    except ValueError as e:
        if isinstance(e, GuideError):
            raise
        raise GuideError("%s is not valid JSON: %s" % (_rel(path), e))


def _positive_int(v):
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def load_guide(gid, guides_dir=GUIDES_DIR):
    """-> the loaded guide dict for guides/<gid>/, or raise GuideError."""
    if not isinstance(gid, str) or not _ID_RE.match(gid):
        raise GuideError("rooms.json names a guide %r; a guide id is lower-case letters, "
                         "digits, - and _ only." % (gid,))
    base = os.path.join(guides_dir, gid)
    meta_path = os.path.join(base, "guide.json")
    meta = _read_json(meta_path)
    where = _rel(meta_path)
    if not isinstance(meta, dict):
        raise GuideError("%s must contain a JSON object." % where)
    if meta.get("id") != gid:
        raise GuideError("%s says its id is %r, but it lives in guides/%s/." % (where, meta.get("id"), gid))
    for key in ("name", "greeting"):
        if not isinstance(meta.get(key), str) or not meta[key].strip():
            raise GuideError("%s needs a non-empty \"%s\"." % (where, key))
    no_brain = meta.get("no_brain")
    if (not isinstance(no_brain, list) or not no_brain
            or not all(isinstance(s, str) and s.strip() for s in no_brain)):
        raise GuideError("%s needs \"no_brain\": a non-empty list of plain lines." % where)

    def named_file(name, label):
        if not isinstance(name, str) or not name or os.path.basename(name) != name:
            raise GuideError("%s: \"%s\" must be a file name inside guides/%s/." % (where, label, gid))
        return os.path.join(base, name)

    persona = _read_json(named_file(meta.get("persona"), "persona"))
    definition = persona.get("definition") if isinstance(persona, dict) else None
    if not isinstance(definition, str) or not definition.strip():
        raise GuideError("%s needs a non-empty \"definition\"." % _rel(named_file(meta["persona"], "persona")))
    _read_text(named_file(meta.get("knowledge"), "knowledge"))

    projections = meta.get("projections")
    caps = meta.get("caps")
    if not isinstance(projections, dict) or not isinstance(caps, dict):
        raise GuideError("%s needs \"projections\" and \"caps\" objects." % where)
    out_proj = {}
    for v in VERBOSITIES:
        text = _read_text(named_file(projections.get(v), "projections." + v))
        cap = caps.get(v)
        if (not isinstance(cap, dict) or not _positive_int(cap.get("max_tokens"))
                or not _positive_int(cap.get("answer_chars"))):
            raise GuideError("%s: caps.%s needs positive whole numbers for \"max_tokens\" and "
                             "\"answer_chars\"." % (where, v))
        out_proj[v] = {"text": text, "tokens": (len(text) + 3) // 4,   # chars/4, rounded up
                       "max_tokens": cap["max_tokens"], "answer_chars": cap["answer_chars"]}
    return {"id": gid, "name": meta["name"].strip(), "definition": definition.strip(),
            "greeting": meta["greeting"].strip(), "no_brain": [s.strip() for s in no_brain],
            "projections": out_proj}


def load_all(rooms_path=ROOMS_PATH, guides_dir=GUIDES_DIR):
    """-> {guide id: guide} for every guide rooms.json names. A rooms.json
    that cannot be read names no guides (engines.rooms() already warns about
    it and falls back); a guide it DOES name must load, or GuideError."""
    try:
        with open(rooms_path) as f:
            rooms = json.load(f)
    except (OSError, ValueError):
        return {}
    out = {}
    for room in rooms if isinstance(rooms, list) else []:
        gid = room.get("guide") if isinstance(room, dict) else None
        if gid is not None and gid not in out:
            out[gid] = load_guide(gid, guides_dir)
    return out
