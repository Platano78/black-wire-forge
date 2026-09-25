#!/usr/bin/env python3
"""
Black Wire Forge
========================
A SIMPLIFIED REMOTE CONTROL for a fleet of ComfyUI instances ("lanes"). It is NOT
a replacement for ComfyUI -- you keep ComfyUI for the thought-out work. This is
the quick-idea front door, with plain-language controls instead of a node graph.

  PICTURE tab  -> whichever image engine pack is installed
  VIDEO tab    -> whichever video engine pack is installed
  WORKFLOW tab -> a finished still goes straight into a video as its first frame

WHAT THIS PROCESS IS ALLOWED TO DO TO A COMFYUI LANE
----------------------------------------------------
  GET  /system_stats        read status + VRAM
  GET  /queue               read queue depth
  GET  /history/<prompt_id> read job result
  GET  /view?...            read an output file
  POST /upload/image        push a reference file
  POST /prompt              submit a job
  POST /free                drop MODEL WEIGHTS from VRAM (process stays alive)
  POST /interrupt           cancel a job (only from the Stop button)
  WS   /ws?clientId=...     read progress events

That is the whole list. It NEVER restarts, kills, updates or reconfigures a lane,
a container or a model server, and it never deletes a file it did not create.
Your lanes may be running someone else's production work; this app is a polite
guest on them.

Python 3 standard library only -- no pip, no venv, no build step.
Every host, port, model filename and the listen port come from config.json.
"""

import base64
import copy
import json
import math
import mimetypes
import os
import queue
import random
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------------
# Config
#
# NOTHING about your machines is hard-coded in this file. Copy
# config.example.json to config.json and edit that. See the README for the
# full schema; the short version is:
#
#   port      the port this app listens on
#   title     what the page calls itself
#   lanes[]   one entry per ComfyUI instance: id, name, host, port, gpu,
#             gpu_label, caps (any subset of the installed packs' caps)
#   models    the exact .safetensors filenames as your ComfyUI sees them
#   status_only  optional read-only tile (e.g. an LLM server), never dispatched to
# ---------------------------------------------------------------------------

APP_DIR = os.path.dirname(os.path.abspath(__file__))
import engines               # engine packs: every model name lives in one
import runner                # process lanes: fills the plan's placeholders and runs it
import guides                # room guides: the persona a room's helper conversation speaks as

# GENCENTER_DATA moves the whole data tree (jobs, sequences, caches) elsewhere,
# the same way GENCENTER_CONFIG moves the config. Tests point it at a scratch
# directory so they never touch the owner's job history.
DATA_DIR = os.environ.get("GENCENTER_DATA") or os.path.join(APP_DIR, "data")
CHAIN_DIR = os.path.join(DATA_DIR, "chain")
# A mode may declare a `post` step (engines/__init__.py's post_for()) that runs
# AFTER a lane finishes rendering -- the pack's own pure-Python transform on the
# lane's output, e.g. Pixel Art's quantise step. Its result is kept here, one
# subdirectory per job, alongside (never instead of) the lane's own render.
LOCAL_OUTPUTS_DIR = os.path.join(DATA_DIR, "outputs")
JOBS_FILE = os.path.join(DATA_DIR, "jobs.json")
SEQ_DIR = os.path.join(DATA_DIR, "sequences")   # one <id>.json per sequence
SEQ_MEDIA_DIR = os.path.join(DATA_DIR, "seq")   # <id>/{refs,takes,cuts}/ -- media a sequence owns
CONFIG_FILE = os.environ.get("GENCENTER_CONFIG") or os.path.join(APP_DIR, "config.json")
EXAMPLE_FILE = os.path.join(APP_DIR, "config.example.json")

MODEL_KEYS = engines.model_keys()   # whatever the installed packs declare
# The only things a lane genuinely cannot be guessed from. Everything else has
# a safe default, because "put in your lane addresses and run it" is the point.
LANE_KEYS = ("id", "name", "host", "port")


def die(msg):
    sys.stderr.write("\n" + msg.rstrip() + "\n\n")
    raise SystemExit(2)


def load_config():
    """Read config.json, or explain exactly what to do instead of crashing."""
    if not os.path.exists(CONFIG_FILE):
        hint = ""
        if os.path.exists(EXAMPLE_FILE):
            hint = ("    cp %s %s\n"
                    "    # then edit it: put in your own ComfyUI hosts, ports and model filenames\n"
                    % (EXAMPLE_FILE, CONFIG_FILE))
        die("No config file at %s\n\n"
            "This app ships no machine addresses of its own. Create one from the example:\n\n"
            "%s\n"
            "Set GENCENTER_CONFIG=/path/to/config.json to keep it somewhere else."
            % (CONFIG_FILE, hint))
    try:
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
    except ValueError as e:
        die("%s is not valid JSON: %s\n"
            "Tip: JSON has no comments and no trailing commas." % (CONFIG_FILE, e))
    if not isinstance(cfg, dict):
        die("%s must contain a JSON object." % CONFIG_FILE)

    lanes = cfg.get("lanes")
    if not isinstance(lanes, list) or not lanes:
        die("%s needs a non-empty \"lanes\" list -- one entry per ComfyUI instance." % CONFIG_FILE)
    seen = set()
    for i, lane in enumerate(lanes):
        if not isinstance(lane, dict):
            die("lanes[%d] in %s must be an object." % (i, CONFIG_FILE))
        kind = lane.get("kind") or "comfy"
        if kind not in ("comfy", "process"):
            die("lanes[%d] (%s): \"kind\" must be \"comfy\" or \"process\"."
                % (i, lane.get("id", "no id")))
        lane["kind"] = kind
        # A process lane runs programs on the box this app is on: it has no
        # host or port of its own, so only an id and a name are required.
        missing = [k for k in (("id", "name") if kind == "process" else LANE_KEYS)
                   if k not in lane]
        if missing:
            die("lanes[%d] (%s) in %s is missing: %s"
                % (i, lane.get("id", "no id"), CONFIG_FILE, ", ".join(missing)))
        if lane["id"] in seen:
            die("Two lanes share the id %r in %s. Lane ids must be unique."
                % (lane["id"], CONFIG_FILE))
        seen.add(lane["id"])
        # "caps" is optional and means "what I want this lane used for". What it
        # can ACTUALLY do is discovered from the lane itself; the two are
        # intersected. Leave it out and the lane is offered for whatever it has.
        # A process lane must name its caps: nothing is discoverable until its
        # programs are, and claiming everything would be a silent surprise.
        if kind == "process":
            caps = lane.get("caps")
            if caps is None:
                die("lanes[%d] (%s): a process lane must say which caps it is\n"
                    "offered for, e.g. \"caps\": [\"3d\"]." % (i, lane["id"]))
        else:
            caps = lane.setdefault("caps", list(engines.caps()))
        if not isinstance(caps, list) or not caps or set(caps) - set(engines.caps()):
            die("lanes[%d] (%s): \"caps\" must be a non-empty list drawn from %s,\n"
                "or left out entirely to let the lane offer whatever models it has."
                % (i, lane["id"], engines.caps()))
        lane.setdefault("box", "")
        lane.setdefault("note", "offline")
        if kind == "process":
            # No network side at all: the defaults keep the payload shape the
            # UI already draws for a comfy lane, with the box name as label.
            lane["host"] = lane.get("host", "")
            lane["port"] = 0
            lane["gpu"] = lane.get("gpu") or ""
            lane["gpu_label"] = lane.get("gpu_label") or lane.get("box") or lane["name"]
            lane.setdefault("slots", 1)
        else:
            try:
                lane["port"] = int(lane["port"])
            except (TypeError, ValueError):
                die("lanes[%d] (%s): \"port\" must be a number." % (i, lane["id"]))
            # No "gpu" given? Assume every lane on the same host shares one
            # card. That is the conservative guess: it may free weights that
            # did not need freeing (costing a reload), where the opposite
            # mistake is an out-of-memory crash. Set "gpu" explicitly on a
            # multi-GPU box.
            lane.setdefault("gpu", lane["host"])
            lane.setdefault("gpu_label", lane.get("box") or lane["host"])
        if lane.get("models") is not None and not isinstance(lane["models"], dict):
            die("lanes[%d] (%s): \"models\" must be an object if present." % (i, lane["id"]))

    # "models" is OPTIONAL. Model filenames are discovered from each lane at
    # runtime; anything named here simply overrides what was found. Only the
    # keys in MODEL_KEYS mean anything, so typos are worth catching early.
    models = cfg.get("models")
    if models is not None:
        if not isinstance(models, dict):
            die("\"models\" in %s must be an object (or left out: filenames are\n"
                "discovered from each lane automatically)." % CONFIG_FILE)
        unknown = [k for k in models if k not in MODEL_KEYS]
        if unknown:
            die("\"models\" in %s has key(s) this app does not use: %s\n"
                "Valid keys: %s" % (CONFIG_FILE, ", ".join(unknown), ", ".join(MODEL_KEYS)))

    # "helper" (L5) is OPTIONAL: an OpenAI-compatible chat endpoint for "Help
    # me write this" / "Describe this picture". Absent -> the feature does
    # not exist (no route, no button). The core never names the model --
    # that lives here, in the operator's own config.
    helper = cfg.get("helper")
    if helper is not None and (not isinstance(helper, dict) or not helper.get("url")):
        die("\"helper\" in %s must be an object with at least a \"url\" "
            "(ending in \"/v1\")." % CONFIG_FILE)
    ctx = helper.get("context") if helper is not None else None
    if ctx is not None and (not isinstance(ctx, int) or isinstance(ctx, bool) or ctx <= 0):
        die("\"helper\".\"context\" in %s must be a whole number of tokens (the context "
            "the helper model actually serves), e.g. 16384." % CONFIG_FILE)
    vision = helper.get("vision") if helper is not None else None
    if vision is not None and not isinstance(vision, bool):
        die("\"helper\".\"vision\" in %s must be true or false (whether the helper model "
            "can see pictures)." % CONFIG_FILE)
    max_images = helper.get("max_images") if helper is not None else None
    if max_images is not None and (not isinstance(max_images, int) or isinstance(max_images, bool)
                                   or max_images < 1):
        die("\"helper\".\"max_images\" in %s must be a whole number, 1 or more (how many "
            "pictures one guide message may carry)." % CONFIG_FILE)
    max_tokens = helper.get("max_tokens") if helper is not None else None
    if max_tokens is not None and (not isinstance(max_tokens, int) or isinstance(max_tokens, bool)
                                   or max_tokens < 1):
        die("\"helper\".\"max_tokens\" in %s must be a whole number, 1 or more (the fewest tokens "
            "every guide reply may use; a model that thinks first needs room for it), e.g. 8192." % CONFIG_FILE)
    return cfg


CONFIG = load_config()

PORT = int(CONFIG.get("port", 3998))
BIND = CONFIG.get("bind", "0.0.0.0")
# B1: extra hostnames the Host check accepts ("allowed_hosts": ["forge.lan",
# "studio.local:3998"]). Optional; the guard works without it.
ALLOWED_HOSTS = [str(h).strip().lower() for h in (CONFIG.get("allowed_hosts") or [])
                 if str(h).strip()]
TITLE = CONFIG.get("title", "Black Wire Forge")

os.makedirs(CHAIN_DIR, exist_ok=True)
os.makedirs(LOCAL_OUTPUTS_DIR, exist_ok=True)
os.makedirs(SEQ_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# C3.6 -- the cut (the internal sequence/storyboard design spec Section 6, R1/R6).
# Resolved ONCE at process start: PATH and config.json do not change under a
# running server, and every test spins its own subprocess to exercise a
# different PATH/config, so a startup-time resolution is exactly as dynamic
# as this ever needs to be.
# ---------------------------------------------------------------------------
FFMPEG_BIN = shutil.which("ffmpeg")
FFPROBE_BIN = shutil.which("ffprobe")
_CUT_MISSING = [n for n, b in (("ffmpeg", FFMPEG_BIN), ("ffprobe", FFPROBE_BIN)) if not b]
CAN_CUT = not _CUT_MISSING
# join_words() is defined further down this file; this runs at import time,
# before that def executes, so the ('a and b' / 'a') join is spelled out here.
CUT_REASON = ("" if CAN_CUT else
              "%s must be installed and on PATH to cut this sequence into one file."
              % (" and ".join(_CUT_MISSING)))

_CUT_CONFIG = CONFIG.get("cut") or {}
if not isinstance(_CUT_CONFIG, dict):
    die("\"cut\" in %s must be an object if present." % CONFIG_FILE)

# A handful of default DejaVu/Liberation paths -- what finding 9 measured as
# installed on the machine that runs the app. config.cut.fontfile, when set, always wins.
_DEFAULT_CUT_FONTS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
)


def _resolve_cut_font():
    # test_cut.py's own start_server() helper writes config["cut"] as
    # {"cut": {"fontfile": ...}} (every call site in that frozen file does
    # this the same way) rather than {"fontfile": ...} directly -- accepted
    # here alongside the documented top-level shape, since the frozen test
    # may not be edited to match. See the report's low-confidence rulings.
    raw = _CUT_CONFIG
    if "fontfile" not in raw and isinstance(raw.get("cut"), dict):
        raw = raw["cut"]
    configured = raw.get("fontfile")
    if configured:
        if os.path.isfile(configured):
            return configured, True, ""
        return None, False, "The title font set in config.json's \"cut.fontfile\" (%s) is missing." % configured
    for path in _DEFAULT_CUT_FONTS:
        if os.path.isfile(path):
            return path, True, ""
    return None, False, "No title font is installed on this machine. Set \"cut\": {\"fontfile\": \"...\"} in config.json."


CUT_FONTFILE, CAN_TITLE, TITLE_REASON = _resolve_cut_font()

# Every lane is ALWAYS listed in the UI. Down lanes glow red, they are never
# removed. `gpu` is the collision key: two lanes with the same gpu key are on
# the same physical card and cannot both hold weights (two large models
# against a 24GB card). See free_colliding_lanes().
# `gpu_label` is what a human reads on screen: card names, not driver indices.
LANES = CONFIG["lanes"]
LANE_BY_ID = {l["id"]: l for l in LANES}

# Optional read-only strip tile (not a ComfyUI lane, never dispatched to).
# Omit "status_only" from config.json and the tile simply does not appear.
FLEET_LLM = CONFIG.get("status_only") or None

# Optional prompt helper (L5). Omit "helper" from config.json and /api/helper
# 404s, and /api/engines reports helper: false -- the page then draws no
# "Help me write this" / "Describe this picture" buttons at all.
HELPER = CONFIG.get("helper") or None

# Room guides (guides.py): every guide rooms.json names, loaded once. A named
# guide that is missing or broken refuses startup, like a broken config --
# a room silently without its guide is the thing this must never do.
try:
    GUIDES = guides.load_all()
except guides.GuideError as e:
    die("%s\n\nrooms.json names this guide, so the app will not start without it." % e)

# Optional global model overrides. Anything set here wins over what a lane
# reports it has; anything absent is discovered. See "Model discovery" below.
CONFIG_MODELS = {k: v for k, v in (CONFIG.get("models") or {}).items() if v}

_T = CONFIG.get("timing") or {}
POLL_SECONDS = float(_T.get("poll_seconds", 4.0))          # lane status poll
JOB_POLL_SECONDS = float(_T.get("job_poll_seconds", 3.0))  # history poll for active jobs
HTTP_TIMEOUT = float(_T.get("http_timeout", 8.0))
FREE_SETTLE_SECONDS = float(_T.get("free_settle_seconds", 2.0))  # let the driver release after /free
DISCOVER_SECONDS = float(_T.get("discover_seconds", 300.0))      # re-read a lane's model list

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

STATE_LOCK = threading.Lock()
LANE_STATE = {l["id"]: {"up": False, "checked": 0, "err": "never polled"} for l in LANES}
FLEET_STATE = {"up": False, "detail": ""}

JOBS_LOCK = threading.Lock()
JOBS = {}            # job_id -> job dict
JOB_ORDER = deque()  # newest last
PROMPT_INDEX = {}    # (lane_id, prompt_id) -> job_id
# Serialises jobs.json writes. Lock order, everywhere: SAVE_LOCK -> SEQ_LOCK ->
# JOBS_LOCK -> STATE_LOCK. Nothing takes an earlier lock while holding a later one.
SAVE_LOCK = threading.Lock()
# Held across a sequence's whole read-modify-WRITE, not only the snapshot.
SEQ_LOCK = threading.Lock()

# Process lanes (kind "process"): the pack builds a run plan of argv steps
# and runner.py executes it locally. One FIFO per lane; a job's plan and
# uploaded input values wait in PROC_PLANS until a worker picks the id up;
# PROC_STOP holds one threading.Event per job, the UI's way of killing the
# running program group. PROC_QUEUE is its own structure: none of the four
# locks above guards it, and it is never taken while holding one of them.
PROC_QUEUE = {}
PROC_PLANS = {}
PROC_STOP = {}
# A file uploaded for a process lane lands here, under the lane's id.
UPLOADS_DIR = os.path.join(DATA_DIR, "uploads")
os.makedirs(UPLOADS_DIR, exist_ok=True)

LOG = deque(maxlen=250)
LOG_LOCK = threading.Lock()

# One stable clientId per lane so ComfyUI routes that lane's progress events to
# our websocket. Prompts are submitted with the same id. These are PERSISTED:
# after a restart we reconnect with the same id, so ComfyUI keeps routing the
# progress of a job we queued before the restart straight back to us.
CLIENTS_FILE = os.path.join(DATA_DIR, "clients.json")
try:
    with open(CLIENTS_FILE) as _f:
        LANE_CLIENT_ID = json.load(_f)
except Exception:
    LANE_CLIENT_ID = {}
for _l in LANES:
    LANE_CLIENT_ID.setdefault(_l["id"], str(uuid.uuid4()))
try:
    with open(CLIENTS_FILE, "w") as _f:
        json.dump(LANE_CLIENT_ID, _f)
except Exception:
    pass


def log(text, level="info"):
    with LOG_LOCK:
        LOG.appendleft({"ts": time.time(), "text": text, "level": level})
    print("[%s] %s" % (level, text), flush=True)


def join_words(items):
    """['a','b','c'] -> 'a, b and c'."""
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return "%s and %s" % (", ".join(items[:-1]), items[-1])


def suggest_lanes(cap):
    """Name the lanes that can actually do `cap` right now, for a plain-words
    error. A lane only counts if it is set up for the job AND has the models."""
    names = [l["name"] for l in LANES if cap in l["caps"] and abilities(l)[cap]]
    if not names:
        return ("No machine here has the %s models installed yet." % engines.cap_word(cap))
    if len(names) == 1:
        return "Use %s." % names[0]
    return "Pick %s or %s." % (", ".join(names[:-1]), names[-1])


def lane_kind(lane):
    """Which kind of lane a CONFIG entry is: "comfy" (the default, talks
    ComfyUI over HTTP) or "process" (runs a local program via runner.py)."""
    return lane.get("kind") or "comfy"


def human_time(seconds):
    """Plain words, never a raw float on screen."""
    s = int(round(seconds or 0))
    if s < 60:
        return "%d seconds" % s
    m, r = divmod(s, 60)
    if m < 60:
        return "%d min %d s" % (m, r) if r else "%d min" % m
    h, m = divmod(m, 60)
    return "%d h %d min" % (h, m)


# ---------------------------------------------------------------------------
# HTTP helpers (plain stdlib, short timeouts, never raise into a thread loop)
# ---------------------------------------------------------------------------

def lane_url(lane, path):
    return "http://%s:%d%s" % (lane["host"], lane["port"], path)


def http_get_json(url, timeout=HTTP_TIMEOUT):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def http_get_bytes(url, timeout=60.0):
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), r.headers.get("Content-Type", "application/octet-stream")


def http_post_json(url, payload, timeout=30.0):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return {"_http_error": e.code, "_body": json.loads(raw)}
        except Exception:
            return {"_http_error": e.code, "_body": raw[:2000]}


def http_post_multipart(url, fields, files, timeout=180.0):
    """files: list of (fieldname, filename, content_type, data)."""
    boundary = "----genctr%s" % uuid.uuid4().hex
    out = []
    for k, v in fields.items():
        out.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                    % (boundary, k, v)).encode("utf-8"))
    for fieldname, filename, ctype, data in files:
        out.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
                    "Content-Type: %s\r\n\r\n" % (boundary, fieldname, filename, ctype)).encode("utf-8"))
        out.append(data)
        out.append(b"\r\n")
    out.append(("--%s--\r\n" % boundary).encode("utf-8"))
    body = b"".join(out)
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "multipart/form-data; boundary=%s" % boundary,
        "Content-Length": str(len(body)),
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        return {"_http_error": e.code, "_body": raw[:2000]}


# ---------------------------------------------------------------------------
# Model discovery
#
# You should not have to transcribe .safetensors filenames into a config file.
# Every ComfyUI instance already knows exactly what it has installed, so we ask
# it: GET /object_info/<LoaderNode> returns that node's dropdown contents, which
# is the real list of files on that box.
#
# We then match on PATTERNS, not exact names, because the same model ships under
# many filenames depending on who quantised it (int8_convrot, nvfp4_awq, fp8,
# bf16, GGUF, ...). A lane with a different quant of the same model still works
# with no configuration at all.
#
# Anything set under "models" in config.json overrides what is found here.
# ---------------------------------------------------------------------------

# role -> which loader's dropdown to search
ROLE_POOL = engines.role_pool()     # role -> which loader pool, declared by packs

POOL_NODES = {
    "unet": [("UNETLoader", "unet_name"), ("UnetLoaderGGUF", "unet_name")],
    "clip": [("CLIPLoader", "clip_name"), ("DualCLIPLoader", "clip_name1")],
    "vae": [("VAELoader", "vae_name")],
    "lora": [("LoraLoaderModelOnly", "lora_name")],
    "checkpoint": [("CheckpointLoaderSimple", "ckpt_name")],
    "audio_encoder": [("AudioEncoderLoader", "audio_encoder_name")],
    "bg_removal": [("LoadBackgroundRemovalModel", "bg_removal_name")],
    "upscale_model": [("UpscaleModelLoader", "model_name")],
    "clip_vision": [("CLIPVisionLoader", "clip_name")],
    "latent_upscaler": [("LatentUpscaleModelLoader", "model_name")],
}

# all:  every substring must be present
# none: no substring may be present
# any:  at least one must be present (empty means no constraint)
# prefer: ranking bonuses, earliest term worth the most
ROLE_RULES = engines.role_rules()   # role -> match rule, declared by packs

# Quantisation markers, for choosing a build the card can actually hold.
SMALL_QUANTS = ("nvfp4", "int8", "fp8", "w4a8", "int4", "gguf", "q8", "q6", "q5", "q4", "awq")
BIG_QUANTS = ("bf16", "fp16", "fp32", "float16")

DISCOVERY_LOCK = threading.Lock()
DISCOVERY = {}   # lane_id -> {"models": {...}, "pools": {...}, "checked": ts, "err": str}


def _rank(name, rule, small_card):
    """Higher is better. Ties break towards the shorter (plainer) filename."""
    n = name.lower()
    score = 0.0
    prefer = rule.get("prefer") or []
    for i, term in enumerate(prefer):
        if term in n:
            score += (len(prefer) - i) * 10.0
    if small_card:
        if any(q in n for q in SMALL_QUANTS):
            score += 5.0
        elif any(q in n for q in BIG_QUANTS):
            score -= 5.0
    return score - len(name) * 0.01


def pick_model(pool, rule, small_card):
    """Best filename in `pool` for one role, or None."""
    must_all = rule.get("all") or []
    must_none = rule.get("none") or []
    must_any = rule.get("any") or []
    hits = []
    for name in pool:
        n = name.lower()
        if not all(t in n for t in must_all):
            continue
        if any(t in n for t in must_none):
            continue
        if must_any and not any(t in n for t in must_any):
            continue
        hits.append(name)
    if not hits:
        return None
    return sorted(hits, key=lambda x: (-_rank(x, rule, small_card), x))[0]


def fetch_pool(lane, pool):
    """The dropdown contents of one loader node on one lane.

    Two dropdown shapes are live on this fleet, measured 2026-09-22:
      legacy  [["name", ...], {...}]                          opts[0] is a list
      V3      ["COMBO", {"options": ["name", ...], ...}]       opts[0] == "COMBO"
    Missing the V3 shape made AudioEncoderLoader's dropdown silently invisible
    -- SheetSage2 became permanently undiscoverable with nothing logged. An
    unrecognised third shape is logged once by node name instead of repeating
    that silence.
    """
    names = []
    for node, field in POOL_NODES[pool]:
        try:
            info = http_get_json(lane_url(lane, "/object_info/%s" % node), timeout=8.0)
        except Exception:
            continue       # node not installed on this build, or lane went away
        spec = (info or {}).get(node) or {}
        opts = ((spec.get("input") or {}).get("required") or {}).get(field)
        if not (isinstance(opts, list) and opts):
            continue
        if isinstance(opts[0], list):
            candidates = opts[0]
        elif opts[0] == "COMBO" and len(opts) > 1 and isinstance(opts[1], dict):
            candidates = opts[1].get("options") or []
        else:
            log("%s: %s.%s has an unrecognised dropdown shape, skipping"
                % (lane["name"], node, field), "warn")
            continue
        # Keep things that look like files. ComfyUI puts pseudo-entries in some of
        # these lists (VAELoader offers "pixel_space", which is not a file).
        for o in candidates:
            if isinstance(o, str) and "." in o and o not in names:
                names.append(o)
    return names


def discover_lane(lane):
    """Ask a lane what it has, and work out what it can therefore do."""
    with STATE_LOCK:
        vram = LANE_STATE.get(lane["id"], {}).get("vram_total", 0)
    # Under ~25GB, prefer a quantised build over bf16/fp16. Unknown counts as
    # small: erring towards the lighter file is the safer mistake.
    small_card = (vram or 0) < 25e9

    pools, found = {}, {}
    for pool in POOL_NODES:
        pools[pool] = fetch_pool(lane, pool)
    if not any(pools.values()):
        with DISCOVERY_LOCK:
            DISCOVERY[lane["id"]] = {"models": {}, "pools": pools, "checked": time.time(),
                                     "err": "the lane did not answer /object_info"}
        return
    for role, rule in ROLE_RULES.items():
        pool_name = ROLE_POOL[role]
        if pool_name not in pools:
            log("%s: role %r names pool %r, which no POOL_NODES entry covers"
                % (lane["name"], role, pool_name), "error")
            continue
        hit = pick_model(pools[pool_name], rule, small_card)
        if hit:
            found[role] = hit

    with DISCOVERY_LOCK:
        entry = DISCOVERY.get(lane["id"]) or {}
        prev, first = entry.get("models") or {}, not entry.get("checked")
        DISCOVERY[lane["id"]] = {"models": found, "pools": pools,
                                 "checked": time.time(), "err": ""}
    # Say something the first time we look at a lane, and whenever the answer
    # changes. A lane with nothing usable is worth saying out loud, once.
    if first or found != prev:
        able = abilities(lane)
        if not any(able.get(c) for c in engines.caps()):
            log("%s answered, but has none of the installed engines' models"
                % lane["name"], "models")
        else:
            bits = ["%s: %s" % (engines.cap_word(c),
                                engines.describe(models_for(lane), c) if able.get(c) else "no")
                    for c in engines.caps()]
            bits.append("speed pack: " + ("yes" if able["turbo"] else "not installed"))
            log("%s has %s" % (lane["name"], ", ".join(bits)), "models")


def models_for(lane):
    """Resolved filenames for a lane: detected, then config overrides on top."""
    with DISCOVERY_LOCK:
        out = dict((DISCOVERY.get(lane["id"]) or {}).get("models") or {})
    out.update(CONFIG_MODELS)
    out.update({k: v for k, v in (lane.get("models") or {}).items() if v})
    return out


def abilities(lane):
    """What this lane can actually do, from the models it actually has.

    Kept separate from the lane's declared `caps`, which say what it is FOR.
    The UI offers the intersection. WHICH files make which ability true is
    declared by the engine packs, not known here.
    """
    return engines.abilities(models_for(lane))


def describe_video(lane):
    return engines.describe(models_for(lane), "video")


def describe_image(lane):
    return engines.describe(models_for(lane), "image")


def missing_for(lane, mode):
    """Which roles are absent for a mode, in plain words. Empty means good to go.

    The words come from the pack that provides the ability, so a new engine
    brings its own vocabulary with it. `mode` may be an ability name ("fl2va",
    "image") or a bare cap name ("video", "audio") -- engines.missing_words
    resolves a cap to its shortest-path-to-satisfied ability on its own, so
    no cap name needs to be spelled out here. Only packs that run on this
    lane's kind are asked, so a process lane names its programs, never a
    ComfyUI pack's model files.
    """
    return engines.missing_words(models_for(lane), mode, lane_kind(lane))



# ---------------------------------------------------------------------------
# Lane status poller
# ---------------------------------------------------------------------------

def poll_lane_once(lane):
    st = {"up": False, "checked": time.time()}
    try:
        stats = http_get_json(lane_url(lane, "/system_stats"), timeout=4.0)
        st["up"] = True
        st["comfy"] = stats.get("system", {}).get("comfyui_version", "?")
        devs = stats.get("devices") or []
        if devs:
            d = devs[0]
            st["device"] = d.get("name", "?")
            st["vram_total"] = d.get("vram_total", 0)
            st["vram_free"] = d.get("vram_free", 0)
            st["torch_vram_alloc"] = d.get("torch_vram_total", 0)
        st["ram_free"] = stats.get("system", {}).get("ram_free", 0)
    except Exception as e:
        st["err"] = "%s" % (e,)
        with STATE_LOCK:
            LANE_STATE[lane["id"]] = st
        return
    try:
        q = http_get_json(lane_url(lane, "/queue"), timeout=4.0)
        st["running"] = len(q.get("queue_running", []))
        st["pending"] = len(q.get("queue_pending", []))
        # A ComfyUI queue item is [number, prompt_id, prompt, extra_data, outputs].
        # Knowing which ids are live lets a job we inherited across a restart show
        # as running instead of sitting on "queued" until it finishes.
        ids = []
        for bucket in ("queue_running", "queue_pending"):
            for item in q.get(bucket, []):
                if isinstance(item, list) and len(item) > 1 and isinstance(item[1], str):
                    ids.append(item[1])
        st["live_ids"] = ids
        # When a clean /queue read happened. A failed read leaves this unset,
        # so an empty live_ids only proves "the lane has no record of it"
        # when it is set and newer than the job (job_poller's lost rule).
        st["queue_checked"] = time.time()
    except Exception:
        st["running"] = 0
        st["pending"] = 0
        st["live_ids"] = []
    with STATE_LOCK:
        LANE_STATE[lane["id"]] = st


def lane_poller(lane):
    # Stagger so seven lanes do not all fire in the same instant.
    time.sleep(random.random() * 2.0)
    if lane_kind(lane) == "process":
        process_worker(lane)
        return
    while True:
        try:
            poll_lane_once(lane)
            # Re-read the model list when the lane first answers, and
            # occasionally after that, so installing a model shows up without
            # restarting this app. Cheap: four small GETs every few minutes.
            with STATE_LOCK:
                up = LANE_STATE.get(lane["id"], {}).get("up")
            if up:
                with DISCOVERY_LOCK:
                    last = (DISCOVERY.get(lane["id"]) or {}).get("checked", 0)
                if time.time() - last > DISCOVER_SECONDS:
                    discover_lane(lane)
        except Exception:
            traceback.print_exc()
        time.sleep(POLL_SECONDS)


def fleet_poller():
    """Optional extra tile: any OpenAI-compatible /v1/models endpoint. Status only."""
    path = FLEET_LLM.get("path", "/v1/models")
    while True:
        try:
            d = http_get_json("http://%s:%d%s" % (FLEET_LLM["host"], FLEET_LLM["port"], path), timeout=4.0)
            ids = [m.get("id") for m in d.get("data", [])]
            FLEET_STATE.update({"up": True, "detail": ", ".join([i for i in ids if i][:2]) or "up"})
        except Exception:
            FLEET_STATE.update({"up": False, "detail": "offline"})
        time.sleep(15.0)


# ---------------------------------------------------------------------------
# Process lanes: the pack builds a run plan of argv steps (lane_kind
# "process") and runner.py executes it on the box this app runs on. The core
# only ever sees jobs, a per-lane FIFO and bare filenames -- no executable
# name lives in this file, and no shell is ever opened.
# ---------------------------------------------------------------------------

def _process_media(name):
    """The same media classes ComfyUI outputs get, from the extension only."""
    ext = os.path.splitext(name)[1].lower()
    if ext in (".mp4", ".webm", ".mov", ".mkv"):
        return "video"
    if ext in (".flac", ".mp3", ".opus", ".wav", ".m4a", ".ogg"):
        return "audio"
    if ext in (".glb", ".gltf", ".obj", ".ply"):
        return "3d"
    return "image"


def poll_process_lane(lane):
    """A process lane is never networked: discovery is which() per declared
    bin, so installing a program shows up on the next poll, no restart."""
    found, notes = {}, []
    for pack in engines.packs():
        if pack["cap"] not in lane["caps"]:
            continue
        if (pack.get("lane_kind") or "comfy") != "process":
            continue
        for role, prog in (pack.get("bins") or {}).items():
            if role in found:
                continue
            found[role] = shutil.which(str(prog))
            if not found[role]:
                notes.append("this lane needs %s installed to work"
                             % (pack.get("words") or {}).get(role, role))
    with DISCOVERY_LOCK:
        DISCOVERY[lane["id"]] = {"models": found, "pools": {}, "checked": time.time(), "err": ""}
    st = {"up": not notes, "checked": time.time(), "live_ids": [], "err": "; ".join(notes),
          "load": round(os.getloadavg()[0], 2), "cores": os.cpu_count() or 1}
    try:
        st["pending"] = PROC_QUEUE[lane["id"]].qsize()
    except KeyError:
        st["pending"] = 0
    with JOBS_LOCK:
        st["running"] = sum(1 for j in JOBS.values()
                           if j["lane"] == lane["id"] and j["status"] == "running"
                           and not j.get("prompt_id"))
    with STATE_LOCK:
        LANE_STATE[lane["id"]] = st


def process_worker(lane):
    """The lane poller for a process lane: start the lane's `slots` FIFO
    workers, then keep its status fresh on the normal cadence."""
    q = PROC_QUEUE.setdefault(lane["id"], queue.Queue())
    for _ in range(max(1, int(lane.get("slots", 1)))):
        threading.Thread(target=_process_slot, args=(lane, q), daemon=True).start()
    while True:
        try:
            poll_process_lane(lane)
        except Exception:
            traceback.print_exc()
        time.sleep(POLL_SECONDS)


def _process_slot(lane, q):
    """One worker: pull a job id and run it; never let one bad job kill the
    worker, and never hold a shared lock across the run itself."""
    while True:
        try:
            jid = q.get(timeout=1.0)
        except queue.Empty:
            continue
        try:
            run_process_job(lane, jid)
        except Exception as e:
            _finish_process_job(jid, "error", "the program could not be run here: %s" % e)
            log("%s on %s failed to run: %s" % (jid, lane["name"], e), "error")


def _finish_process_job(jid, status, error=None, tail=None, outputs=None):
    """The one place a process job's terminal state is written."""
    with JOBS_LOCK:
        j = JOBS.get(jid)
        if not j:
            return
        j["status"] = status
        j["updated"] = time.time()
        if error:
            j["error"] = error
        if tail is not None:
            j["notes"] = list(tail)
        if outputs is not None:
            j["outputs"] = outputs
        if status == "done":
            j["finished"] = time.time()
            j["elapsed"] = round(j["finished"] - j.get("started", j["finished"]), 1)
    PROC_STOP.pop(jid, None)
    save_jobs()


def run_process_job(lane, jid):
    """One queued process job: stage the input files, resolve the pack's plan
    and hand it to the runner. Every failure ends as a plain sentence on the
    job -- the user never sees a stack trace."""
    plan, values = PROC_PLANS.pop(jid, (None, None))
    # The stop event must stay findable while the job runs -- api_cancel()
    # looks it up here. It leaves with the terminal write in
    # _finish_process_job(), not here.
    stop = PROC_STOP.setdefault(jid, threading.Event())
    if plan is None or stop.is_set():
        PROC_STOP.pop(jid, None)
        return
    with JOBS_LOCK:
        j = JOBS.get(jid)
        if not j or j["status"] != "queued":
            return
        j["status"] = "running"
        j["started"] = time.time()
        j["updated"] = j["started"]
        kind, mode = j["kind"], j["mode"]
    job_dir = os.path.join(LOCAL_OUTPUTS_DIR, jid)
    os.makedirs(job_dir, exist_ok=True)
    # Stage this lane's uploaded input files into the job dir so {in:<field>}
    # resolves to a real path. Multi-file fields are job references in the
    # making: one file per field until then.
    inputs = {}
    fdefs = {f["id"]: f for f in (engines.fields(kind, mode) or []) if isinstance(f, dict)}
    for fid, val in (values or {}).items():
        ftype = (fdefs.get(fid) or {}).get("type", "")
        if ftype not in ("audio", "image", "image_list", "video_list", "model") or not val:
            continue
        names = val if isinstance(val, list) else [val]
        if len(names) > 1:
            raise ValueError("input fields in process lanes take a single file right now (multi-file is coming with job references)")
        name = os.path.basename(str(names[0]))
        if not name or name in (".", ".."):
            raise ValueError("that input filename is not allowed")
        src = os.path.join(UPLOADS_DIR, lane["id"], name)
        if not os.path.isfile(src):
            raise ValueError("I cannot find the uploaded file for %s any more." % fid)
        inputs[fid] = os.path.join(job_dir, name)
        shutil.copyfile(src, inputs[fid])
    # Only the bins this plan actually names go to the runner; whatever is
    # still missing becomes the runner's own plain sentence, naming the
    # executable.
    bins = {r: models_for(lane).get(r) for r in engines.bins(kind, mode) if models_for(lane).get(r)}
    steps = runner.resolve_plan(plan, bins, job_dir, inputs, engines.pack_dir(kind, mode))
    def on_progress(step, total):
        with JOBS_LOCK:
            j = JOBS.get(jid)
            if j and j["status"] == "running":
                j["step"], j["total"] = step, total
    ok, tail, err = runner.run_steps(steps, cwd=job_dir,
                                     progress=plan.get("progress") if isinstance(plan, dict) else None,
                                     on_progress=on_progress, stop_event=stop)
    if stop.is_set():
        _finish_process_job(jid, "error", "you stopped this one", tail)
        return
    if not ok:
        _finish_process_job(jid, "error", err or "the program stopped early", tail)
        return
    outs = []
    try:
        for path in runner.output_paths(plan, job_dir):
            name = os.path.basename(path)
            if not name or name in (".", ".."):
                raise ValueError("that output filename is not allowed")
            # subfolder is the job id, exactly like run_post_step()'s local
            # outputs: the page's viewURL() sends it as `subfolder`, not `job`.
            outs.append({"filename": name, "subfolder": jid, "type": "local",
                         "media": _process_media(name)})
    except ValueError as e:
        _finish_process_job(jid, "error", "%s" % e, tail)
        return
    _finish_process_job(jid, "done", None, None, outs)


def dispatch_process(lane, plan, kind, mode, meta, values=None):
    """Queue a run on a process lane. No HTTP and no weights to free: the job
    waits for one of the lane's workers to pick it up."""
    jid = uuid.uuid4().hex[:12]
    job = {
        "id": jid, "lane": lane["id"], "lane_name": lane["name"], "prompt_id": None,
        "kind": kind, "mode": mode, "status": "queued", "step": 0, "total": 0,
        "created": time.time(), "started": time.time(), "updated": time.time(),
        "outputs": [], "notes": [],
    }
    job.update(meta)
    job["licence"] = engines.licence_for(kind, mode)
    PROC_PLANS[jid] = (plan, dict(values or {}))
    PROC_STOP[jid] = threading.Event()
    with JOBS_LOCK:
        JOBS[jid] = job
        JOB_ORDER.append(jid)
    save_jobs()
    PROC_QUEUE.setdefault(lane["id"], queue.Queue()).put(jid)
    log("Queued a %s run on %s" % (kind, lane["name"]))
    return {"ok": True, "job": job, "notes": []}


# ---------------------------------------------------------------------------
# Minimal websocket client (stdlib only) for live step progress
# ---------------------------------------------------------------------------

class WSClient(object):
    def __init__(self, host, port, path):
        self.host, self.port, self.path = host, port, path
        self.sock = None
        self.buf = b""

    def connect(self):
        s = socket.create_connection((self.host, self.port), timeout=8.0)
        s.settimeout(40.0)
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n"
               % (self.path, self.host, self.port, key))
        s.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = s.recv(4096)
            if not chunk:
                raise IOError("closed during handshake")
            head += chunk
        head, rest = head.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n")[0] + b" ":
            raise IOError("handshake rejected: %s" % head.split(b"\r\n")[0][:80])
        self.sock, self.buf = s, rest
        return self

    def _need(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise IOError("closed")
            self.buf += chunk

    def _send(self, opcode, payload=b""):
        mask = os.urandom(4)
        n = len(payload)
        hdr = bytes([0x80 | opcode])
        if n < 126:
            hdr += bytes([0x80 | n])
        elif n < 65536:
            hdr += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            hdr += bytes([0x80 | 127]) + struct.pack(">Q", n)
        hdr += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(hdr + masked)

    def read_message(self):
        """Returns (opcode, data) for a complete message, handling fragments."""
        frames = []
        first_op = None
        while True:
            self._need(2)
            b0, b1 = self.buf[0], self.buf[1]
            fin = b0 & 0x80
            op = b0 & 0x0F
            masked = b1 & 0x80
            ln = b1 & 0x7F
            off = 2
            if ln == 126:
                self._need(4)
                ln = struct.unpack(">H", self.buf[2:4])[0]
                off = 4
            elif ln == 127:
                self._need(10)
                ln = struct.unpack(">Q", self.buf[2:10])[0]
                off = 10
            mask = b""
            if masked:
                self._need(off + 4)
                mask = self.buf[off:off + 4]
                off += 4
            self._need(off + ln)
            data = self.buf[off:off + ln]
            self.buf = self.buf[off + ln:]
            if masked:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if op == 0x9:      # ping -> pong
                self._send(0xA, data)
                continue
            if op == 0xA:      # pong
                continue
            if op == 0x8:      # close
                raise IOError("server closed websocket")
            if first_op is None:
                first_op = op
            frames.append(data)
            if fin:
                return first_op, b"".join(frames)

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


def ws_listener(lane):
    """Per-lane progress listener. Reconnects forever, never crashes the app."""
    path = "/ws?clientId=" + LANE_CLIENT_ID[lane["id"]]
    backoff = 3.0
    while True:
        with STATE_LOCK:
            up = LANE_STATE[lane["id"]].get("up")
        if not up:
            time.sleep(5.0)
            continue
        ws = WSClient(lane["host"], lane["port"], path)
        try:
            ws.connect()
            backoff = 3.0
            while True:
                op, data = ws.read_message()
                if op != 0x1:      # binary frames are preview images; ignore
                    continue
                try:
                    msg = json.loads(data.decode("utf-8"))
                except Exception:
                    continue
                handle_ws_message(lane, msg)
        except Exception:
            pass
        finally:
            ws.close()
        time.sleep(backoff)
        backoff = min(backoff * 1.6, 30.0)


def handle_ws_message(lane, msg):
    mtype = msg.get("type")
    data = msg.get("data") or {}
    pid = data.get("prompt_id")
    if not pid:
        return
    with JOBS_LOCK:
        jid = PROMPT_INDEX.get((lane["id"], pid))
        if not jid:
            return
        job = JOBS.get(jid)
        if not job or job["status"] in ("done", "error"):
            return
        if mtype == "progress":
            job["status"] = "running"
            job["step"] = int(data.get("value") or 0)
            job["total"] = int(data.get("max") or 0) or job.get("total") or 0
            job["updated"] = time.time()
        elif mtype == "executing":
            job["status"] = "running"
            job["node"] = data.get("node")
            job["updated"] = time.time()
        elif mtype == "execution_error":
            job["status"] = "error"
            job["error"] = str(data.get("exception_message") or data.get("exception_type") or "execution error")
            job["finished"] = time.time()
            log("%s on %s stopped with an error" % (job["kind"].title(), lane["name"]), "error")


# ---------------------------------------------------------------------------
# Job store
# ---------------------------------------------------------------------------

def save_jobs():
    """Write jobs.json: the newest 200 jobs PLUS every job a sequence names.

    Two things this must not do (both happened before C3.1):
      - serialise the LIVE job dicts after JOBS_LOCK is released, while the
        poller adds keys to them ("dictionary changed size during iteration",
        swallowed below, and that save silently lost). The snapshot is now
        encoded to a string while the lock is held.
      - let two writers share one tmp file, so one os.replace()s a file the
        other is still writing. Writes are serialised by SAVE_LOCK and each
        writer has its own tmp name.
    SAVE_LOCK is taken FIRST and held across the pin read, the snapshot and the
    write, so a later save can never be overtaken by an earlier, staler one.
    JOBS_LOCK is never held during disk I/O.
    """
    try:
        with SAVE_LOCK:
            pinned = seq_pinned_job_ids()   # None = a sequence could not be read
            with JOBS_LOCK:
                newest = set(list(JOB_ORDER)[-200:])
                keep = [j for j in JOB_ORDER
                        if j in JOBS and (pinned is None or j in newest or j in pinned)]
                text = json.dumps([JOBS[j] for j in keep])
            tmp = "%s.%d.tmp" % (JOBS_FILE, threading.get_ident())
            with open(tmp, "w") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, JOBS_FILE)
    except Exception:
        traceback.print_exc()


def _mark_post_output(j):
    """A job saved before post outputs carried "post": true: its mode's post
    step appended the one "local" output after the lane's render, so mark
    that one (run_post_step()'s shape)."""
    outs = j.get("outputs") or []
    if (len(outs) < 2 or any(o.get("post") for o in outs) or outs[0].get("type") == "local"
            or not engines.post_for(j.get("kind"), j.get("mode"))):
        return
    local = [o for o in outs[1:] if o.get("type") == "local"]
    if local:
        local[-1]["post"] = True


def result_output(job):
    """The job's result: its post step's output when it has one, else its
    first output (None when it has none)."""
    outs = job.get("outputs") or []
    return next((o for o in outs if o.get("post")), outs[0] if outs else None)


def load_jobs():
    if not os.path.exists(JOBS_FILE):
        return
    try:
        with open(JOBS_FILE) as f:
            arr = json.load(f)
        with JOBS_LOCK:
            for j in arr:
                # A render belongs to the LANE, not to us: restarting this app does
                # not stop it. So keep mid-flight jobs mid-flight and let the history
                # poller resolve them. Only something ancient is truly lost.
                if j.get("status") in ("queued", "running"):
                    # A process-lane job died with this process: the program
                    # cannot have survived, so it is interrupted at once.
                    # A ComfyUI render does not (the 6 h rule below is only
                    # the safety net for something truly lost).
                    lane = LANE_BY_ID.get(j.get("lane"))
                    if lane is not None and lane_kind(lane) == "process":
                        j["status"] = "interrupted"
                    elif time.time() - j.get("started", 0) > 6 * 3600:
                        j["status"] = "interrupted"
                _mark_post_output(j)
                JOBS[j["id"]] = j
                JOB_ORDER.append(j["id"])
                if j.get("prompt_id"):
                    PROMPT_INDEX[(j["lane"], j["prompt_id"])] = j["id"]
        log("Picked up %d earlier result(s)" % len(arr))
    except Exception:
        traceback.print_exc()


# ---------------------------------------------------------------------------
# Sequence store (the internal sequence/storyboard design spec §1, §7; slice C3.1)
#
# One file per sequence, SEQ_DIR/<id>.json. A sequence names jobs by id only;
# jobs.json stays the one record of what was made. SEQ_LOCK is held across the
# whole read-modify-write. Every mutation bumps `rev`; a client op carrying a
# stale rev gets 409 and the current object. Slot state is derived on every
# read and never stored.
# ---------------------------------------------------------------------------

SEQ_ID_RE = re.compile(r"s_[0-9a-f]{8}")        # the id becomes a FILENAME: fullmatch only
SEQ_MODES = ("sequence", "storyboard")
SLOT_LANES = {"picture": "p", "video": "v", "sound": "s"}   # timeline lane -> slot id prefix
# Ref ops (add_ref/move_ref/remove_ref) arrived in C3.2a, cables (patch/
# unpatch) in C3.4 and the script-beat ops in C3.5. C3.5 emptied the
# "not built yet" list, so the generic refusal sentence it used to produce is
# gone with it: every op named in §7 is real below. A future slice that names
# an op before building it adds both the op and its own sentence here.


class SeqDamaged(Exception):
    """A sequence file exists but does not parse. Never guessed around."""


def seq_valid_id(sid):
    return isinstance(sid, str) and SEQ_ID_RE.fullmatch(sid) is not None


def _seq_path(sid):
    if not seq_valid_id(sid):                    # second line of defence; routes check first
        raise ValueError("That is not a sequence id.")
    return os.path.join(SEQ_DIR, sid + ".json")


def seq_ref_path(sid, ref_id):
    """Where a reference's own copy lives: data/seq/<id>/refs/<ref id>.png.
    Realpath-contained in data/seq/<id>/, same rule as local_output_path."""
    if not seq_valid_id(sid):
        raise ValueError("That is not a sequence id.")
    base = os.path.realpath(os.path.join(SEQ_MEDIA_DIR, sid))
    path = os.path.realpath(os.path.join(base, "refs", (ref_id or "") + ".png"))
    if path != base and not path.startswith(base + os.sep):
        raise ValueError("that reference path is not allowed")
    return path


def _seq_read(sid):
    """The stored object, or None if there is no such sequence. Caller holds SEQ_LOCK."""
    path = _seq_path(sid)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            seq = json.load(f)
    except ValueError:
        seq = None
    if not isinstance(seq, dict) or seq.get("id") != sid:
        raise SeqDamaged("The file for sequence %s is damaged, so it was left untouched." % sid)
    return seq


def _seq_write(seq):
    """Caller holds SEQ_LOCK. Per-writer tmp name, fsync, then os.replace."""
    path = _seq_path(seq["id"])
    tmp = "%s.%d.tmp" % (path, threading.get_ident())
    with open(tmp, "w") as f:
        json.dump(seq, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _seq_all():
    """[(id, object, or None if damaged)] for every sequence file. Caller holds SEQ_LOCK."""
    out = []
    names = sorted(os.listdir(SEQ_DIR)) if os.path.isdir(SEQ_DIR) else []
    for name in names:
        sid = name[:-5] if name.endswith(".json") else ""
        if not seq_valid_id(sid):
            continue
        try:
            out.append((sid, _seq_read(sid)))
        except SeqDamaged:
            out.append((sid, None))
    return out


def _seq_job_uses(seq):
    """(job_id, sentence) for every job this sequence names: refs, takes, picks.
    "shot N" counts within the slot's own timeline lane, as the page shows it."""
    title = seq.get("title")
    uses = []
    for ref in seq.get("refs") or []:
        if ref.get("job_id"):
            uses.append((ref["job_id"], "%s uses this in its reference room. Remove it there first." % title))
    shot = {}
    for slot in seq.get("slots") or []:
        shot[slot.get("lane")] = shot.get(slot.get("lane"), 0) + 1
        said = "%s uses this as shot %d. Remove it there first." % (title, shot[slot.get("lane")])
        for jid in [t.get("job_id") for t in slot.get("takes") or []] + [slot.get("pick")]:
            if jid:
                uses.append((jid, said))
    return uses


def seq_pinned_job_ids():
    """Every job id any sequence names, or None if some sequence file could not
    be read -- the caller must then keep EVERY job, because a damaged file may
    name any of them."""
    with SEQ_LOCK:
        pinned = set()
        for _sid, seq in _seq_all():
            if seq is None:
                return None
            pinned.update(jid for jid, _ in _seq_job_uses(seq))
        return pinned


def seq_job_use(jid):
    """The sentence refusing to forget `jid`, or None if no sequence names it.
    Caller holds SEQ_LOCK."""
    for sid, seq in _seq_all():
        if seq is None:
            return ("The file for sequence %s is damaged, so I cannot tell whether it uses this. "
                    "Nothing was removed." % sid)
        for used, said in _seq_job_uses(seq):
            if used == jid:
                return said
    return None


def default_canvas():
    """Owner ruling 5: the pack's own width/height defaults. The first video mode
    (packs in id order) that declares integer width and height fields wins."""
    for mode in engines.modes_for("video"):
        d = {f["id"]: f.get("default") for f in engines.fields("video", mode)}
        if isinstance(d.get("width"), int) and isinstance(d.get("height"), int):
            return {"width": d["width"], "height": d["height"]}
    return {"width": 960, "height": 544}


def slot_state(slot, jobs, lane_up):
    """§1's table, evaluated in order. Returns (state, progress)."""
    takes = slot.get("takes") or []
    if not takes:
        return "empty", None
    job = jobs.get(takes[-1].get("job_id"))
    status = job.get("status") if job else None
    if status in ("queued", "running"):
        # A job whose lane went down is never marked (the poller just retries),
        # so believing its status alone would say "rendering" over a dead lane.
        if not lane_up.get(job.get("lane")):
            return "cannot-tell", None
        total = int(job.get("total") or 0)
        return "rendering", ("%d/%d" % (int(job.get("step") or 0), total) if total else "--")
    if status == "error":
        return "failed", None
    if status == "interrupted" or job is None:   # no record at all is lost too
        return "lost", None
    if slot.get("pick"):
        return "ready", None
    return "unpicked", None


def _slot_image_list_field(cap, mode):
    """The FIRST field of type image_list in the mode's own declared field
    list (§2's resolver hook) -- server.py names no mode or field id. None if
    the mode declares no such field."""
    for f in engines.fields(cap, mode):
        if f.get("type") == "image_list":
            return f
    return None


def slot_sees_refs(slot):
    """§7's derived `sees_refs`: true when refs=="auto" and the slot's mode
    declares an image_list field -- the same test the generate-time resolver
    uses to decide whether to pass anything at all."""
    return slot.get("refs") == "auto" and _slot_image_list_field(slot.get("cap"), slot.get("mode")) is not None


def _resolved_ref_uses(seq, slot):
    """The ["<ref id>:<job id>", ...] a generate right now would put in
    take["inputs"]["refs"] for this slot -- used both by the live resolver
    and by staleness (comparing a past take's inputs against this)."""
    if not slot_sees_refs(slot):
        return []
    return ["%s:%s" % (r["id"], r.get("job_id")) for r in seq.get("refs") or []]


def slot_jacks(slot):
    """§4/§7/K1's derived `jacks`: every field of type "image" or "video" in
    the slot's own (cap, mode) -- the pack's own field list is the socket
    list, so a jack is named, typed and ordered by the pack, never by this
    module. [] for anything that is not a video-lane slot (a cable only
    ever ENDS at a video shot, per §4/_op_patch, so a picture-lane slot on
    a mode that happens to declare its own image field -- a clean-up or
    pixel-art mode -- must not grow a jack that would only ever refuse),
    and [] for a video-lane slot whose mode declares neither (a video mode
    with only an image_list room feed, e.g. H3's ref2v)."""
    if slot.get("lane") != "video":
        return []
    return [{"field": f["id"], "label": f.get("label") or f["id"], "type": f["type"]}
            for f in engines.fields(slot.get("cap"), slot.get("mode")) if f.get("type") in ("image", "video")]


def _resolved_cable_uses(seq, slot):
    """The {field id: source job id} a generate right now would put in
    take["inputs"]["cables"] for this slot -- a cable whose source has no
    pick maps to None (generate would refuse it, but staleness still needs a
    comparable value). Used both by the live resolver and by staleness."""
    uses = {}
    slots_by_id = {s["id"]: s for s in seq.get("slots") or []}
    for cable in seq.get("cables") or []:
        if cable.get("to") != slot.get("id"):
            continue
        src = slots_by_id.get(cable.get("from"))
        uses[cable.get("field")] = src.get("pick") if src else None
    return uses


def slot_stale_inputs(seq, slot, take):
    """The inputs half of STALE: "room plate changed" arrives with refs
    (C3.2b); "<jack label, lower-cased> changed" arrives with cables (C3.4).
    It compares the pick's take["inputs"] against what would resolve now."""
    reasons = []
    inputs = take.get("inputs")
    if isinstance(inputs, dict) and isinstance(inputs.get("refs"), list):
        if inputs["refs"] != _resolved_ref_uses(seq, slot):
            reasons.append("room plate changed")
    if isinstance(inputs, dict) and isinstance(inputs.get("cables"), dict):
        current = _resolved_cable_uses(seq, slot)
        labels = {j["field"]: j["label"] for j in slot_jacks(slot)}
        for field_id in set(inputs["cables"]) | set(current):
            if inputs["cables"].get(field_id) != current.get(field_id):
                reason = "%s changed" % labels.get(field_id, field_id).lower()
                if reason not in reasons:
                    reasons.append(reason)
    return reasons


def _ref_role_warning(ref):
    """§5 Warned: a `set` ref not made with a `ref_role: set` preset, or a
    `character` ref not made with a `ref_role: character` preset. Compares
    the ref's OWN recorded role against the preset id its owning job carries
    (job.recipe, §7's job-meta addition) -- never against a preset's name."""
    role = ref.get("role")
    if role not in ("set", "character"):
        return None
    with JOBS_LOCK:
        job = JOBS.get(ref.get("job_id"))
    cap, mode, recipe = (job or {}).get("kind"), (job or {}).get("mode"), ref.get("recipe")
    ok = False
    if cap and mode and recipe:
        preset = next((pr for pr in engines.presets(cap, mode) if pr.get("id") == recipe), None)
        ok = bool(preset and preset.get("ref_role") == role)
    if ok:
        return None
    return ("not made with the no-people recipe" if role == "set" else "may carry the film's lighting")


def slot_warnings(seq, slot):
    """§5 Warned, evaluated per slot for GET /api/sequence. A slot that sees
    the reference room inherits every role-mismatch warning the room itself
    carries (those are the refs it is about to be handed); a video slot gets
    the set-plate sentence for its own situation -- no set in the room, a
    recipe that cannot take the room's pictures, or a shot drawing from its
    words alone -- each naming the fix (add the set plate, or start the
    shot from a picture)."""
    warnings = []
    refs = seq.get("refs") or []
    has_set = any(r.get("role") == "set" for r in refs)
    sees_refs = slot_sees_refs(slot)
    if sees_refs:
        for ref in refs:
            w = _ref_role_warning(ref)
            if w and w not in warnings:
                warnings.append(w)
    if slot.get("cap") == "video":
        image_jacks = [j for j in slot_jacks(slot) if j.get("type") == "image"]
        cables = seq.get("cables") or []
        starts_from_picture = any((slot.get("values") or {}).get(j["field"]) for j in image_jacks) or any(
            c.get("to") == slot.get("id") and c.get("field") in {j["field"] for j in image_jacks} for c in cables)
        if sees_refs and not has_set:
            warnings.append(
                "There is no set plate in the REF ROOM yet, so each shot will draw its own version of "
                "the place. Add a picture of the empty location as the set plate to keep it the same "
                "from shot to shot.")
        elif not sees_refs and has_set and not starts_from_picture:
            w = ("This shot's recipe can't take the REF ROOM's pictures, so the set plate won't reach "
                 "it and it will draw the place its own way.")
            if image_jacks:
                w += " To carry the place over, start it from the set plate."
            warnings.append(w)
        elif not sees_refs and not has_set and image_jacks and not starts_from_picture:
            warnings.append(
                "This shot draws its place from its words alone. To keep the place the same from shot "
                "to shot, start each shot from the same picture.")
    return warnings


def slot_stale(seq, slot, beats):
    take = next((t for t in slot.get("takes") or [] if t.get("job_id") == slot.get("pick")), None)
    if not take:
        return []
    reasons = []
    beat = beats.get(slot.get("beat_id"))
    if beat and take.get("beat_rev") is not None and take["beat_rev"] != beat.get("rev"):
        reasons.append("script changed")
    return reasons + slot_stale_inputs(seq, slot, take)


def slot_trim_effective(sid, slot):
    """§1/R3's derived `trim_effective`: {"in", "len"} once the slot's pick
    is local, else null. `trim: null` is the whole take (in=0, len=probed
    duration); `in: null` is tail-keep (in = probed duration - len). Never
    validated against the probed duration here -- that only happens at cut
    time (R3's "never clamped" refusals); this is purely descriptive."""
    if not slot.get("pick"):
        return None
    take = next((t for t in slot.get("takes") or [] if t.get("job_id") == slot["pick"]), None)
    if not take or not take.get("file"):
        return None
    path = _seq_take_cache_path(sid, take["file"])
    if not path or not os.path.isfile(path):
        return None
    duration = _probe_duration(path)
    if duration is None:
        return None
    trim = slot.get("trim")
    if trim is None:
        return {"in": 0.0, "len": duration}
    length = trim["len"]
    if trim["in"] is None:
        return {"in": duration - length, "len": length}
    return {"in": trim["in"], "len": length}


def seq_derive(seq):
    """A deep copy of the stored object with each slot's derived `state`,
    `progress` and `stale`, plus C3.6's top-level `can_cut`/`cut_reason`/
    `can_title`/`title_reason` (fixed for this process, R1/R6) and each
    slot's `trim_effective` (§1/R3). The stored object is never touched."""
    out = copy.deepcopy(seq)
    ids = {t.get("job_id") for s in out.get("slots") or [] for t in s.get("takes") or []}
    with JOBS_LOCK:
        jobs = {j: {k: JOBS[j].get(k) for k in ("status", "lane", "step", "total")}
                for j in ids if j in JOBS}
    with STATE_LOCK:
        lane_up = {lid: bool(st.get("up")) for lid, st in LANE_STATE.items()}
    beats = {b.get("id"): b for b in out.get("beats") or []}
    for slot in out.get("slots") or []:
        slot["state"], slot["progress"] = slot_state(slot, jobs, lane_up)
        slot["stale"] = slot_stale(out, slot, beats)
        slot["sees_refs"] = slot_sees_refs(slot)
        slot["warnings"] = slot_warnings(out, slot)
        slot["jacks"] = slot_jacks(slot)
        slot["trim_effective"] = slot_trim_effective(out["id"], slot)
    out["can_cut"] = CAN_CUT
    out["cut_reason"] = CUT_REASON
    out["can_title"] = CAN_TITLE
    out["title_reason"] = TITLE_REASON
    return out


def _seq_auto_title(seq):
    """A sequence the app named (title_auto) takes its first beat's opening
    words, at most 60 characters, whole words only -- until the user names it."""
    beats = seq.get("beats") or []
    if not beats:
        return
    text = str(beats[0].get("text") or "")
    line = text.split("\n")[0]
    words = line.split()
    if not words:
        return
    out = ""
    for w in words:
        if out:
            candidate = out + " " + w
        else:
            candidate = w
        if len(candidate) > 60:
            if not out:
                # first word is > 60 chars: use first 60
                out = line[:60]
            break
        out = candidate
    out = out.rstrip(" ,;:\u2014-")
    if out:
        seq["title"] = out


def _seq_thumb(seq):
    """The list row's small picture: the first picked take that has its own
    local copy, video lane first, then the picture lane. None before that."""
    for lane, media in (("video", "video"), ("picture", "image")):
        for slot in seq.get("slots") or []:
            if slot.get("lane") != lane:
                continue
            pick = slot.get("pick")
            if not pick:
                continue
            for t in slot.get("takes") or []:
                if t.get("job_id") == pick and t.get("file"):
                    return {"path": t["file"], "media": media}
    return None


# -- validation --------------------------------------------------------------

def _text(val, what, limit=200):
    if not isinstance(val, str) or not val.strip():
        raise ValueError("Give it a %s." % what)
    if len(val) > limit:
        raise ValueError("That %s is longer than %d characters." % (what, limit))
    return val.strip()


def _num(val, what):
    if isinstance(val, bool) or not isinstance(val, (int, float)) or val < 0:
        raise ValueError("%s must be a number, zero or more." % what)
    return val


def _slot(seq, p):
    for slot in seq["slots"]:
        if slot["id"] == p.get("slot_id"):
            return slot
    raise ValueError("That shot is not in this sequence any more.")


def _check_slot(seq, slot):
    """lane/cap/mode/recipe/quality/refs/values must all fit the slot's
    (cap, mode), from the packs' own declarations. Unknown value keys are refused."""
    cap, mode = slot.get("cap"), slot.get("mode")
    if not isinstance(slot.get("lane"), str) or slot["lane"] not in SLOT_LANES:
        raise ValueError("A shot goes in the picture, video or sound lane.")
    if cap not in engines.caps() or mode not in engines.modes_for(cap):
        raise ValueError("No installed engine makes %r / %r." % (cap, mode))
    if not isinstance(slot.get("values"), dict):
        raise ValueError("values must be an object of field ids.")
    known = {f["id"] for f in engines.fields(cap, mode)}
    bad = sorted(str(k) for k in slot["values"] if k not in known)
    if bad:
        raise ValueError("%s has no setting called %s." % (
            engines.mode_words(cap).get(mode, mode), join_words(bad)))
    if slot.get("recipe") is not None and slot["recipe"] not in [x.get("id") for x in engines.presets(cap, mode)]:
        raise ValueError("That recipe does not belong to this kind of shot.")
    if slot.get("quality") is not None and slot["quality"] not in [x.get("id") for x in engines.quality(cap, mode)]:
        raise ValueError("That quality setting does not belong to this kind of shot.")
    if slot.get("refs") not in ("auto", "off"):
        raise ValueError("refs must be \"auto\" or \"off\".")
    if slot.get("beat_id") is not None and slot["beat_id"] not in [b.get("id") for b in seq.get("beats") or []]:
        raise ValueError("That script beat does not exist.")


# -- ops: each mutates `seq` in place, or raises ValueError(sentence) --------

def _op_set_title(seq, p):
    seq["title"] = _text(p.get("title"), "title")


def _op_set_mode(seq, p):
    if p.get("mode") not in SEQ_MODES:
        raise ValueError("A sequence is either a sequence or a storyboard.")
    seq["mode"] = p["mode"]


def _op_set_canvas(seq, p):
    if any(s.get("lane") == "video" and s.get("takes") for s in seq["slots"]):
        raise ValueError("The canvas is fixed once a video shot has been made, so every shot "
                         "in the cut matches. Start a new sequence for a different size.")
    w, h = p.get("width"), p.get("height")
    for v, what in ((w, "width"), (h, "height")):
        if isinstance(v, bool) or not isinstance(v, int) or not 64 <= v <= 8192:
            raise ValueError("The canvas %s must be a whole number from 64 to 8192." % what)
    seq["canvas"] = {"width": w, "height": h}


def _op_add_slot(seq, p):
    lane = p.get("lane")
    prefix = SLOT_LANES.get(lane, "x") if isinstance(lane, str) else "x"
    used = [int(s["id"][1:]) for s in seq["slots"] if s["id"][:1] == prefix and s["id"][1:].isdigit()]
    slot = {"id": "%s%d" % (prefix, max(used + [0]) + 1), "lane": lane, "beat_id": p.get("beat_id"),
            "cap": p.get("cap"), "mode": p.get("mode"), "recipe": p.get("recipe"),
            "quality": p.get("quality"), "values": p.get("values") if p.get("values") is not None else {},
            "refs": p.get("refs") or "auto", "takes": [], "pick": None, "trim": None, "title": None}
    _check_slot(seq, slot)
    at = p.get("at")
    if at is None:
        seq["slots"].append(slot)
    elif isinstance(at, int) and not isinstance(at, bool) and 0 <= at <= len(seq["slots"]):
        seq["slots"].insert(at, slot)
    else:
        raise ValueError("at must be a position from 0 to %d." % len(seq["slots"]))


def _op_update_slot(seq, p):
    """Changes any of cap/mode/recipe/quality/refs, and MERGES `values`
    (a key sent as null is removed). The result must still validate whole."""
    slot = _slot(seq, p)
    new = copy.deepcopy(slot)
    for k in ("cap", "mode", "recipe", "quality", "refs"):
        if k in p:
            new[k] = p[k]
    if "values" in p:
        if not isinstance(p["values"], dict):
            raise ValueError("values must be an object of field ids.")
        for k, v in p["values"].items():
            if v is None:
                new["values"].pop(k, None)
            else:
                new["values"][k] = v
    _check_slot(seq, new)
    slot.clear()
    slot.update(new)
    # §4/Rule 5: a mode change can drop the jack a cable into this slot was
    # plugged into (e.g. switching off the mode with a first-frame field).
    # A cable feeding a jack this slot no longer declares is removed, same
    # as _op_remove_slot drops cables touching a removed slot.
    live_fields = {j["field"] for j in slot_jacks(slot)}
    seq["cables"] = [c for c in seq.get("cables") or []
                      if c.get("to") != slot["id"] or c.get("field") in live_fields]


def _drop_stale_video_cables(seq):
    """K2's order invariant ("a video jack's source must be an earlier
    video-lane shot") is checked at patch time, but a move can reorder the
    slots it was checked against. Re-checked here after any reorder: a
    video-typed cable whose `from` is no longer earlier than its `to` in
    the video lane's own order is dropped (self/later/no-longer-video-lane
    all count). Image cables never depend on slot order (their rule is
    "from is a picture-lane slot"), so they are untouched. Returns the
    dropped cables as [{from, to}], for the op response to name."""
    slots_by_id = {s["id"]: s for s in seq["slots"]}
    video_index = {s["id"]: i for i, s in enumerate(seq["slots"]) if s.get("lane") == "video"}
    keep, removed = [], []
    for cable in seq.get("cables") or []:
        to_slot = slots_by_id.get(cable.get("to"))
        jack = next((j for j in slot_jacks(to_slot) if j["field"] == cable.get("field")), None) if to_slot else None
        still_ok = True
        if jack and jack.get("type") == "video":
            from_id, to_id = cable.get("from"), cable.get("to")
            still_ok = (from_id in video_index and to_id in video_index
                        and video_index[from_id] < video_index[to_id])
        if still_ok:
            keep.append(cable)
        else:
            removed.append({"from": cable.get("from"), "to": cable.get("to")})
    seq["cables"] = keep
    return removed


def _op_move_slot(seq, p):
    slot = _slot(seq, p)
    to = p.get("to")
    if isinstance(to, bool) or not isinstance(to, int) or not 0 <= to < len(seq["slots"]):
        raise ValueError("to must be a position from 0 to %d." % (len(seq["slots"]) - 1))
    seq["slots"].remove(slot)
    seq["slots"].insert(to, slot)
    removed = _drop_stale_video_cables(seq)
    if removed:
        return {"removed_cables": removed}


def _op_remove_slot(seq, p):
    """Drops the record only. Every take's file stays where it is."""
    slot = _slot(seq, p)
    seq["slots"].remove(slot)
    seq["cables"] = [c for c in seq.get("cables") or [] if slot["id"] not in (c.get("from"), c.get("to"))]
    for beat in seq.get("beats") or []:
        if beat.get("slot_id") == slot["id"]:
            beat["slot_id"] = None


def _op_pick_take(seq, p):
    slot = _slot(seq, p)
    jid = p.get("job_id")
    if jid is None:
        slot["pick"] = None
        return
    if jid not in [t.get("job_id") for t in slot["takes"]]:
        raise ValueError("That take does not belong to this shot.")
    with JOBS_LOCK:
        status = (JOBS.get(jid) or {}).get("status")
    if status != "done":
        raise ValueError("That take has not finished, so it cannot be picked.")
    slot["pick"] = jid


def _op_set_trim(seq, p):
    """R3: `trim.in` may be null (tail-keep: in = the take's probed duration
    minus len, computed at GET/cut time -- see slot_trim_effective) -- only
    `trim.len` is required to be a real number here."""
    slot = _slot(seq, p)
    trim = p.get("trim")
    if trim is not None:
        if not isinstance(trim, dict) or set(trim) != {"in", "len"}:
            raise ValueError("A trim is {\"in\": seconds, \"len\": seconds}, or null for the default.")
        if trim["in"] is not None:
            _num(trim["in"], "The in-point")
        if _num(trim["len"], "The length") <= 0:
            raise ValueError("The length must be more than zero.")
        trim = {"in": trim["in"], "len": trim["len"]}
    slot["trim"] = trim


def _op_set_title_card(seq, p):
    slot = _slot(seq, p)
    card = p.get("title")
    if card is not None:
        if not isinstance(card, dict) or set(card) != {"text", "at", "dur"}:
            raise ValueError("A title card is {\"text\", \"at\", \"dur\"}, or null for none.")
        _num(card["at"], "The title's start")
        if _num(card["dur"], "The title's duration") <= 0:
            raise ValueError("The title's duration must be more than zero.")
        card = {"text": _text(card["text"], "title card text"), "at": card["at"], "dur": card["dur"]}
    slot["title"] = card


REF_ROLES = ("set", "character", "other")


def _add_ref_prefetch(p):
    """The slow half of add_ref (C3.2b review fix, §2/finding-review item 7):
    validate the job/output and FETCH ITS BYTES entirely outside any lock --
    a slow or down source lane must never stall a save on some other
    sequence, and SEQ_LOCK is global across every sequence file. Everything
    here is a plain ValueError -> the same 400 sentence add_ref has always
    raised; only the role-vs-existing-set check (needs the locked, current
    seq state) stays in _op_add_ref below. Returns (job, idx, role, tmp_path);
    the caller removes tmp_path if it never gets used."""
    jid = p.get("job_id")
    with JOBS_LOCK:
        job = copy.deepcopy(JOBS.get(jid))
    if not job:
        raise ValueError("I cannot find that result any more.")
    if job.get("status") != "done":
        raise ValueError("That result has not finished yet, so it cannot join the reference room.")
    idx = p.get("output", 0)
    outs = job.get("outputs") or []
    if not isinstance(idx, int) or isinstance(idx, bool) or idx >= len(outs):
        raise ValueError("That result has no picture to add.")
    out = outs[idx]
    if out.get("media") != "image":
        raise ValueError("Only a picture can go in the reference room.")
    role = p.get("role")
    if role not in REF_ROLES:
        raise ValueError("A reference is set, character or other.")
    data = _carry_source_bytes(job, out)
    os.makedirs(SEQ_MEDIA_DIR, exist_ok=True)
    tmp_path = os.path.join(SEQ_MEDIA_DIR, ".addref_%s.tmp" % uuid.uuid4().hex)
    with open(tmp_path, "wb") as f:
        f.write(data)
    return job, idx, role, tmp_path


def _op_add_ref(seq, p):
    """§2. The network fetch already happened in _add_ref_prefetch(), before
    SEQ_LOCK was taken; this only validates against the now-current (locked,
    re-read) seq state and moves the already-fetched bytes into
    data/seq/<id>/refs/<ref id>.png -- no I/O slower than a rename happens
    while the lock is held."""
    job, idx, role, tmp_path = p["_prefetch"]
    if role == "set" and any(r.get("role") == "set" for r in seq["refs"]):
        raise ValueError("The room already has a set. Remove it first.")
    label = p.get("label")
    if label is not None:
        label = _text(label, "label")
    used = [int(r["id"][1:]) for r in seq["refs"] if r["id"][:1] == "r" and r["id"][1:].isdigit()]
    ref_id = "r%d" % (max(used + [0]) + 1)
    cache_path = seq_ref_path(seq["id"], ref_id)
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    os.replace(tmp_path, cache_path)
    ref = {"id": ref_id, "role": role, "label": label, "job_id": job["id"], "output": idx,
           "file": "refs/%s.png" % ref_id, "recipe": job.get("recipe")}
    if role == "set":
        seq["refs"].insert(0, ref)
    else:
        seq["refs"].append(ref)


def _op_move_ref(seq, p):
    """The set stays first: refuse moving it, and refuse moving another ref
    ahead of it."""
    refs = seq["refs"]
    ref = next((r for r in refs if r["id"] == p.get("ref_id")), None)
    if ref is None:
        raise ValueError("That reference is not in this room any more.")
    to = p.get("to")
    if isinstance(to, bool) or not isinstance(to, int) or not 0 <= to < len(refs):
        raise ValueError("to must be a position from 0 to %d." % (len(refs) - 1))
    if ref.get("role") == "set":
        raise ValueError("The set stays first. It cannot be moved.")
    if to == 0 and refs and refs[0].get("role") == "set":
        raise ValueError("Nothing can go ahead of the set.")
    refs.remove(ref)
    refs.insert(to, ref)


def _op_remove_ref(seq, p):
    """Drops the record only; the copied file stays on disk (nothing here is
    ever deleted -- owner ruling 6)."""
    refs = seq["refs"]
    ref = next((r for r in refs if r["id"] == p.get("ref_id")), None)
    if ref is None:
        raise ValueError("That reference is not in this room any more.")
    refs.remove(ref)


def _op_patch(seq, p):
    """§4/K2. {from, to, field}: a cable, `to` must be a video-lane slot
    whose mode declares `field` as a jack (image or video, in pack order --
    server.py names no mode or field id). An IMAGE jack's source must be a
    picture-lane slot (C3.4, unchanged). A VIDEO jack's source (K2) must be
    a video-lane slot EARLIER than `to` in the video lane's own order
    (a slot cannot continue from itself, or from later, or from a picture);
    any of those is refused with the one "before" sentence. No other cable
    may already feed that (to, field)."""
    slots_by_id = {s["id"]: s for s in seq["slots"]}
    to_slot = slots_by_id.get(p.get("to"))
    if to_slot is None or to_slot.get("lane") != "video":
        raise ValueError("A cable ends at a video shot.")
    jacks = slot_jacks(to_slot)
    if not jacks:
        raise ValueError("This shot's recipe has no picture input to plug into.")
    field_id = p.get("field")
    jack = next((j for j in jacks if j["field"] == field_id), None)
    if jack is None:
        raise ValueError("This shot's recipe has no input called that.")
    from_slot = slots_by_id.get(p.get("from"))
    if jack.get("type") == "video":
        video_ids = [s["id"] for s in seq["slots"] if s.get("lane") == "video"]
        if (from_slot is None or from_slot.get("lane") != "video"
                or from_slot["id"] not in video_ids
                or video_ids.index(from_slot["id"]) >= video_ids.index(to_slot["id"])):
            raise ValueError("A shot can only continue from one before it.")
    elif from_slot is None or from_slot.get("lane") != "picture":
        raise ValueError("A cable starts at a picture.")
    if any(c.get("to") == to_slot["id"] and c.get("field") == field_id for c in seq.get("cables") or []):
        raise ValueError("That input already has a cable. Unplug it first.")
    used = [int(c["id"][1:]) for c in seq.get("cables") or [] if c["id"][:1] == "c" and c["id"][1:].isdigit()]
    seq.setdefault("cables", []).append(
        {"id": "c%d" % (max(used + [0]) + 1), "from": from_slot["id"], "to": to_slot["id"], "field": field_id})


def _op_unpatch(seq, p):
    """§4/§7. {cable_id}: removes one cable."""
    cables = seq.get("cables") or []
    cable = next((c for c in cables if c.get("id") == p.get("cable_id")), None)
    if cable is None:
        raise ValueError("There is no such cable.")
    cables.remove(cable)


# -- script beats (C3.5, §3) ------------------------------------------------
#
# A beat is one paragraph of the script, with a stable id, so editing it is
# never a text diff. `film` / `picture` / `sound` beats each bring exactly one
# shot with them, in their own lane, linked both ways; `scene` (a slugline) and
# `note` get none. A slot made from a beat gets its prompt field pre-filled
# ONCE -- after that the prompt belongs to the user, and editing the beat only
# marks the shot stale with "script changed" (that half already exists in
# slot_stale(), keyed on the take's `beat_rev`).

BEAT_KINDS = ("scene", "film", "picture", "sound", "note")
# §3's table: which lane, and which kind of shot, a beat's kind makes.
BEAT_KIND_LANE = {"film": "video", "picture": "picture", "sound": "sound"}
BEAT_KIND_CAP = {"film": "video", "picture": "image", "sound": "audio"}
# §3's slugline test: the paragraph STARTS with one of these, case-sensitive,
# after leading whitespace. "INT./EXT." also starts with "INT.", so any
# slugline shape is caught; a lower-case "int." is not a slugline.
SCENE_PREFIXES = ("INT.", "EXT.", "INT./EXT.")
BEAT_TEXT_LIMIT = 4000
SCRIPT_TEXT_LIMIT = 200000


def is_slugline(text):
    t = (text or "").lstrip()
    return any(t.startswith(p) for p in SCENE_PREFIXES)


def beat_kind_for(text, kind=None):
    """§3: a slugline is a scene, whatever the caller asked for; otherwise the
    given kind, default `film`."""
    if kind is not None and kind not in BEAT_KINDS:
        raise ValueError("A script beat is a scene, film, picture, sound or note.")
    if is_slugline(text):
        return "scene"
    return kind or "film"


def beat_prompt_field(cap, mode):
    """§3's "pre-filled once from the beat text" has to know WHICH field the
    beat's words go into. It is the same field the inspector already treats as
    the prompt -- index.html's own rule, mirrored here: the first field of type
    `text`/`textarea` in the pack's declared `order` (that is what #promptBox
    becomes), with the id `prompt` (§3/§7's own word for it) as the fallback.
    Nothing but that fallback id is named: no mode, no engine."""
    fields = engines.fields(cap, mode) or []
    ranked = sorted((f for f in fields if isinstance(f, dict)),
                    key=lambda f: f.get("order") if isinstance(f.get("order"), int) else 0)
    field = next((f for f in ranked if f.get("type") in ("text", "textarea")), None)
    if field is None:
        field = next((f for f in fields if isinstance(f, dict) and f.get("id") == "prompt"), None)
    return field


def beat_slot_mode(cap):
    """The mode a beat's new shot starts in: the first mode the ROOM list offers
    for this cap that declares a prompt-shaped field -- a shot made from a beat
    has to be able to hold the beat's text. Room order (rooms.json, `order`
    first) is the same order the page's own "+ generate here" walks, so a beat
    made on the page and one made here pick the same mode; and room order, not
    pack-declaration order, is what keeps a tool mode (a cutout, an upscale)
    off the front. Falls back to any mode for the cap, else None (no installed
    engine: the beat is kept, with no shot, and says so when it is made)."""
    rooms = engines.rooms() or []
    ordered = sorted((r for r in rooms if isinstance(r, dict)),
                     key=lambda r: r.get("order") if isinstance(r.get("order"), int) else 1000)
    for room in ordered:
        for m in room.get("modes") or []:
            if not isinstance(m, dict) or m.get("cap") != cap or not m.get("mode"):
                continue
            if m["mode"] in engines.modes_for(cap) and beat_prompt_field(cap, m["mode"]) is not None:
                return m["mode"]
    modes = engines.modes_for(cap) or []
    return modes[0] if modes else None


def _beat_id(seq):
    used = [int(b["id"][1:]) for b in seq.get("beats") or []
            if isinstance(b.get("id"), str) and b["id"][:1] == "b" and b["id"][1:].isdigit()]
    return "b%d" % (max(used + [0]) + 1)


def _beat_slot_index(seq, lane, pos):
    """Where in slots[] a new shot belongs so that the lane follows the script:
    just after the shot of the last slot-creating beat above this one in the
    same lane (before the lane's first shot when there is none)."""
    before = sum(1 for b in seq["beats"][:pos]
                 if BEAT_KIND_LANE.get(b.get("kind")) == lane and b.get("slot_id"))
    seen = 0
    for i, slot in enumerate(seq["slots"]):
        if slot.get("lane") != lane:
            continue
        if seen == before:
            return i
        seen += 1
    return len(seq["slots"])


def _add_beat(seq, text, kind, pos=None):
    """One beat, plus its one shot (film/picture/sound), both linked. `pos` is
    its position in beats[]; the shot lands in the same place in its lane."""
    beats = seq.setdefault("beats", [])
    beat = {"id": _beat_id(seq), "kind": kind, "text": text, "rev": 1, "slot_id": None}
    beats.insert(len(beats) if pos is None else pos, beat)
    pos = len(beats) - 1 if pos is None else pos
    lane = BEAT_KIND_LANE.get(kind)
    if lane is None:
        return beat                        # scene / note: no shot, per §3
    cap = BEAT_KIND_CAP[kind]
    mode = beat_slot_mode(cap)
    if mode is None:
        return beat                        # nothing installed makes this kind of shot
    slot = {"id": _slot_id(seq, lane), "lane": lane, "beat_id": beat["id"],
            "cap": cap, "mode": mode, "recipe": None, "quality": None,
            "values": {}, "refs": "auto", "takes": [], "pick": None, "trim": None, "title": None}
    field = beat_prompt_field(cap, mode)
    if field is not None:
        # §3: pre-filled ONCE, here and never again -- past another engine's task prefix.
        slot["values"][field["id"]] = engines.foreign_prefix_stripped(cap, mode, text)
    _check_slot(seq, slot)
    seq["slots"].insert(_beat_slot_index(seq, lane, pos), slot)
    beat["slot_id"] = slot["id"]
    return beat


def _slot_id(seq, lane):
    prefix = SLOT_LANES[lane]
    used = [int(s["id"][1:]) for s in seq["slots"]
            if s["id"][:1] == prefix and s["id"][1:].isdigit()]
    return "%s%d" % (prefix, max(used + [0]) + 1)


def _beat(seq, p):
    for beat in seq.get("beats") or []:
        if beat.get("id") == p.get("beat_id"):
            return beat
    raise ValueError("That part of the script is not here any more.")


def _op_insert_beat(seq, p):
    text = p.get("text")
    if not isinstance(text, str):
        raise ValueError("Write the beat out — one paragraph of the script.")
    text = _text(text, "beat text", limit=BEAT_TEXT_LIMIT)
    kind = beat_kind_for(text, p.get("kind"))
    beats = seq.get("beats") or []
    at = p.get("at")
    if at is not None and (isinstance(at, bool) or not isinstance(at, int) or not 0 <= at <= len(beats)):
        raise ValueError("at must be a position from 0 to %d." % len(beats))
    _add_beat(seq, text, kind, at)


def _op_update_beat(seq, p):
    """Text only. It NEVER touches the linked shot's prompt (§3: "the beat never
    rewrites a prompt the user has worked on") -- it bumps beat.rev, which is
    what makes slot_stale() say "script changed" on the next read. No rev bump
    when the text did not actually change, so a blur with no edit cannot make a
    finished shot look out of date."""
    beat = _beat(seq, p)
    text = _text(p.get("text"), "beat text", limit=BEAT_TEXT_LIMIT)
    if text == beat.get("text"):
        return
    # §2/S2's slugline rule kept true on the way through: a heading that has no
    # shot becomes a scene, and one that already HAS a shot is refused rather
    # than silently orphaning it ("scene" means no slot, §3). §7 names no
    # kind-change op, so this is the whole of it -- see the delivered report.
    if is_slugline(text):
        if beat.get("slot_id"):
            raise ValueError("That line reads as a scene heading (INT. / EXT.), which gets no shot. "
                             "This beat has shot %s. Delete the beat and add the heading again, "
                             "or reword the line." % beat["slot_id"])
        beat["kind"] = "scene"
    beat["text"] = text
    beat["rev"] = (beat.get("rev") or 1) + 1


def _op_delete_beat(seq, p):
    """§3/C3.5 gate: deleting the beat leaves its shot in place, unlinked. Never
    a shot, take or pick is deleted -- the Cutting Room holds what was made."""
    beat = _beat(seq, p)
    seq["beats"].remove(beat)
    if beat.get("slot_id"):
        slot = next((s for s in seq["slots"] if s["id"] == beat["slot_id"]), None)
        if slot is not None:
            slot["beat_id"] = None


def _op_import_script(seq, p):
    """§3: the only bulk path. Blank lines split the paragraphs; each becomes a
    beat, and every film/picture/sound beat brings its own shot. An empty
    storyboard only -- importing twice must not double a script the user has
    started editing, and there is no merge to get right."""
    if seq.get("mode") != "storyboard":
        raise ValueError("The script belongs to a storyboard. Switch this sequence to "
                         "storyboard mode first.")
    if seq.get("beats"):
        raise ValueError("This script already has %d beats, so I will not paste a second one "
                         "over it. Delete them first, or start a new storyboard."
                         % len(seq["beats"]))
    text = p.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Paste the script in — blank line between paragraphs.")
    if len(text) > SCRIPT_TEXT_LIMIT:
        raise ValueError("That script is longer than %d characters." % SCRIPT_TEXT_LIMIT)
    text = text.replace("\r\n", "\n").replace("\r", "\n")   # F3: Windows/old-Mac line endings
    paragraphs = [x.strip() for x in re.split(r"\n[ \t]*\n", text)]
    paragraphs = [x for x in paragraphs if x]
    if not paragraphs:
        raise ValueError("Paste the script in — blank line between paragraphs.")
    for para in paragraphs:
        if len(para) > BEAT_TEXT_LIMIT:
            raise ValueError("One paragraph is longer than %d characters. Split it into "
                             "two paragraphs." % BEAT_TEXT_LIMIT)
    for para in paragraphs:
        _add_beat(seq, para, beat_kind_for(para))


def _op_copy_beat_to_prompt(seq, p):
    """§3: the only path from a beat into a prompt after the beat is made. It
    overwrites whatever is there (that is the point of the button) and changes
    nothing else -- the shot stays stale until a new take is made, because that
    is what "stale" means here (§1)."""
    beat = _beat(seq, p)
    if not beat.get("slot_id"):
        raise ValueError("That beat has no shot to write into.")
    slot = next((s for s in seq["slots"] if s["id"] == beat["slot_id"]), None)
    if slot is None:
        raise ValueError("That beat's shot is not in this sequence any more.")
    field = beat_prompt_field(slot.get("cap"), slot.get("mode"))
    if field is None:
        raise ValueError("This shot's recipe has no text field to write the beat into.")
    if not isinstance(slot.get("values"), dict):
        slot["values"] = {}
    # A beat written in another engine's format loses that engine's one
    # leading task prefix; anything else stays, for the Make-time check.
    slot["values"][field["id"]] = engines.foreign_prefix_stripped(slot.get("cap"), slot.get("mode"), beat.get("text"))


SEQ_OPS = {
    "set_title": _op_set_title, "set_mode": _op_set_mode, "set_canvas": _op_set_canvas,
    "add_slot": _op_add_slot, "update_slot": _op_update_slot, "move_slot": _op_move_slot,
    "remove_slot": _op_remove_slot, "pick_take": _op_pick_take, "set_trim": _op_set_trim,
    "set_title_card": _op_set_title_card,
    "add_ref": _op_add_ref, "move_ref": _op_move_ref, "remove_ref": _op_remove_ref,
    "patch": _op_patch, "unpatch": _op_unpatch,
    "insert_beat": _op_insert_beat, "update_beat": _op_update_beat,
    "delete_beat": _op_delete_beat, "import_script": _op_import_script,
    "copy_beat_to_prompt": _op_copy_beat_to_prompt,
}

# Ops that can make a sequence name a job id it did not name before (finding 1:
# jobs past the newest 200 are forgotten on restart). seq_op() calls save_jobs()
# for these, AFTER SEQ_LOCK is released, so the new reference is pinned before
# anything else could restart the app first. Named in one place so C3.2b's take
# ops (which also name a fresh job id) can be added here, not re-derived.
SEQ_OPS_PIN_JOBS = frozenset({"add_ref", "pick_take"})


# -- the entry points the routes call; each returns (body, http code) --------

def seq_list():
    with SEQ_LOCK:
        found = _seq_all()
    out = []
    for sid, seq in found:
        if seq is None:
            out.append({"id": sid, "title": "(damaged file)", "mode": None, "updated": 0,
                        "slots": 0, "ready": 0, "damaged": True,
                        "created": 0, "first_beat": None, "shots": 0, "thumb": None})
            continue
        d = seq_derive(seq)
        beats = seq.get("beats") or []
        first = ""
        if beats:
            first = str(beats[0].get("text") or "")
            first = " ".join(first.split())
            if len(first) > 120:
                first = first[:119] + "\u2026"
        if not beats or not first.strip():
            first = None
        shots = sum(1 for s in d["slots"] if s.get("lane") == "video")
        thumb = _seq_thumb(seq)
        out.append({"id": sid, "title": d.get("title"), "mode": d.get("mode"), "updated": d.get("updated"),
                    "slots": len(d["slots"]), "ready": sum(1 for s in d["slots"] if s["state"] == "ready"),
                    "created": d.get("created"), "first_beat": first, "shots": shots, "thumb": thumb})
    out.sort(key=lambda s: s["updated"] or 0, reverse=True)
    return out, 200


def seq_get(sid):
    if not seq_valid_id(sid):
        return {"ok": False, "error": "That is not a sequence id."}, 400
    with SEQ_LOCK:
        seq = _seq_read(sid)
    if seq is None:
        return {"ok": False, "error": "There is no such sequence."}, 404
    return seq_derive(seq), 200


def seq_create(p):
    """{title, mode, seed_job_id?}. A seed that is a finished picture becomes
    the `set` ref, by job id only (file: null -- copying it is C3.2's carry())."""
    if not isinstance(p, dict):
        return {"ok": False, "error": "Send a JSON object."}, 400
    raw = p.get("title")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        title = time.strftime("Sequence, %d %b %Y %H:%M")
        auto = True
    else:
        try:
            title = _text(raw, "title")
        except ValueError as e:
            return {"ok": False, "error": str(e)}, 400
        auto = False
    mode = p.get("mode") or "sequence"
    if mode not in SEQ_MODES:
        return {"ok": False, "error": "A sequence is either a sequence or a storyboard."}, 400
    refs = []
    seed = p.get("seed_job_id")
    if isinstance(seed, str) and seed:
        with JOBS_LOCK:
            job = copy.deepcopy(JOBS.get(seed))
        outs = (job or {}).get("outputs") or []
        idx = next((i for i, o in enumerate(outs) if o.get("media") == "image"), None)
        if job and job.get("status") == "done" and idx is not None:
            refs.append({"id": "r1", "role": "set", "label": "Set plate", "job_id": seed,
                         "output": idx, "file": None, "recipe": job.get("recipe")})
        else:
            log("That result is not a finished picture, so the sequence starts with no set plate.", "warn")
    now = time.time()
    with SEQ_LOCK:
        sid = "s_" + uuid.uuid4().hex[:8]
        while os.path.exists(_seq_path(sid)):
            sid = "s_" + uuid.uuid4().hex[:8]
        seq = {"id": sid, "schema": 1, "rev": 1, "title": title, "mode": mode,
               "created": now, "updated": now, "canvas": default_canvas(),
               "refs": refs, "beats": [], "slots": [], "cables": [], "cuts": []}
        if auto:
            seq["title_auto"] = True
        _seq_write(seq)
    if refs:
        save_jobs()     # pin the seed in jobs.json now, not on the next unrelated save
    return seq_derive(seq), 200


def seq_delete(p):
    """POST /api/sequence/delete {id}. Moves data/sequences/<id>.json into
    data/sequences/deleted/ -- never deletes a file (owner ruling 6): the
    record stays recoverable, and every take, ref and cut under data/seq/<id>/
    stays where it is."""
    if not isinstance(p, dict):
        return {"ok": False, "error": "Send a JSON object."}, 400
    sid = p.get("id")
    if not seq_valid_id(sid):
        return {"ok": False, "error": "That is not a sequence id."}, 400
    with SEQ_LOCK:
        path = _seq_path(sid)
        if not os.path.exists(path):
            return {"ok": False, "error": "There is no such sequence."}, 404
        dest_dir = os.path.join(SEQ_DIR, "deleted")
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, sid + ".json")
        if os.path.exists(dest):
            dest = os.path.join(dest_dir, "%s.%d.json" % (sid, int(time.time())))
        os.replace(path, dest)
    log("Deleted a sequence (its file is kept under sequences/deleted/).")
    return {"ok": True, "id": sid}, 200


def seq_op(p):
    """{id, rev, op, ...}. 409 with the current object when `rev` is stale."""
    if not isinstance(p, dict):
        return {"ok": False, "error": "Send a JSON object."}, 400
    sid, op, rev = p.get("id"), p.get("op"), p.get("rev")
    if not seq_valid_id(sid):
        return {"ok": False, "error": "That is not a sequence id."}, 400
    if not isinstance(op, str) or op not in SEQ_OPS:
        return {"ok": False, "error": "There is no sequence operation called %r." % (op,)}, 400
    if isinstance(rev, bool) or not isinstance(rev, int):
        return {"ok": False, "error": "Send the rev you last saw with every change."}, 400
    tmp_path = None
    extra = None
    if op == "add_ref":
        # C3.2b review fix: the slow fetch happens here, BEFORE SEQ_LOCK (a
        # single global lock across every sequence) is ever taken.
        try:
            job, idx, role, tmp_path = _add_ref_prefetch(p)
        except ValueError as e:
            return {"ok": False, "error": str(e)}, 400
        p = dict(p, _prefetch=(job, idx, role, tmp_path))
    try:
        with SEQ_LOCK:
            seq = _seq_read(sid)
            if seq is None:
                return {"ok": False, "error": "There is no such sequence."}, 404
            if seq.get("rev") != rev:
                new = None
            else:
                new = copy.deepcopy(seq)
                try:
                    # An op may return a dict of extra fields for the
                    # response (e.g. move_slot's own removed_cables, so a
                    # move that silently broke a continue link says so) --
                    # every op that returns nothing keeps today's behaviour.
                    extra = SEQ_OPS[op](new, p)
                    if op == "set_title":
                        new.pop("title_auto", None)
                    elif new.get("title_auto"):
                        _seq_auto_title(new)
                except ValueError as e:
                    return {"ok": False, "error": str(e)}, 400
                new["rev"] = seq["rev"] + 1
                new["updated"] = time.time()
                _seq_write(new)
    finally:
        # Left behind only when _op_add_ref never got to move it (404/409/a
        # refused role) -- a successful add_ref has already os.replace()'d it
        # into its real cache path, so this is a no-op on the common path.
        if tmp_path is not None and os.path.exists(tmp_path):
            os.remove(tmp_path)
    if new is None:
        return {"ok": False, "error": "This sequence changed somewhere else. Here is how it is now.",
                "sequence": seq_derive(seq)}, 409
    if op in SEQ_OPS_PIN_JOBS:
        save_jobs()     # pin whatever job id this op just named, now
    body = seq_derive(new)
    if extra:
        body.update(extra)
    return body, 200


def seq_add_take(sid, slot_id, job_id, beat_rev=None, inputs=None):
    """Server-side writer (C3.2's generate will call this): record a take on a
    slot. Not a client op, so it carries no rev -- it bumps it, which is what
    makes a page holding the old rev get 409 on its next change."""
    with SEQ_LOCK:
        seq = _seq_read(sid)
        if seq is None:
            raise ValueError("There is no such sequence.")
        _slot(seq, {"slot_id": slot_id})["takes"].append(
            {"job_id": job_id, "made": time.time(), "beat_rev": beat_rev,
             "inputs": inputs or {"refs": [], "cables": {}}, "file": None})
        seq["rev"] += 1
        seq["updated"] = time.time()
        _seq_write(seq)
    save_jobs()     # pin it in jobs.json now
    return seq_derive(seq)


def collect_outputs(hist_entry):
    """ComfyUI puts SaveImage under 'images' and SaveVideo ALSO under 'images'
    (with animated: true). Scan every list of file dicts.

    A loader node (LoadVideo) echoes the file it just loaded back into this
    same history dict as a UI preview, tagged `"type": "input"` -- and a
    PreviewImage-style node tags its own preview `"type": "temp"`. Both are
    shaped identically to a real SaveX result, so skip anything not tagged
    "output" (a missing `type` still counts as "output", same as before --
    most real Save nodes never set it explicitly). Found 2026-09-23: an H3
    "continue" job's LoadVideo (prev_video) ran before its SaveVideo, so
    outputs[0] silently became the echoed prev_video instead of the render."""
    outs = []
    for node_id, node_out in (hist_entry.get("outputs") or {}).items():
        for key, val in node_out.items():
            if not isinstance(val, list):
                continue
            for item in val:
                if isinstance(item, dict) and item.get("filename"):
                    if item.get("type", "output") != "output":
                        continue
                    fn = item["filename"]
                    ext = os.path.splitext(fn)[1].lower()
                    if ext in (".mp4", ".webm", ".mov", ".mkv"):
                        media = "video"
                    elif ext in (".flac", ".mp3", ".opus", ".wav", ".m4a", ".ogg"):
                        media = "audio"
                    elif ext in (".glb", ".gltf", ".obj", ".ply"):
                        media = "3d"
                    else:
                        media = "image"
                    outs.append({
                        "filename": fn,
                        "subfolder": item.get("subfolder", ""),
                        "type": item.get("type", "output"),
                        "media": media,
                    })
    return outs


def local_output_path(job_id, filename):
    """Resolve a `post` step's own output to a real path inside
    LOCAL_OUTPUTS_DIR, or raise ValueError with a plain sentence. The ONE
    rule both sides of this boundary use -- the read side (proxy_view_local,
    filename off a query string) and the write side (run_post_step,
    filename off a pack's OWN return value) are both untrusted input, so
    both go through this before touching disk.

    `filename` is basename()'d first: a bare filename with no directory
    component is the whole point of this store, and basename() alone still
    lets `..`/`.`/empty straight through (basename("..") == ".."), so those
    are refused by name rather than left to realpath alone. `job_id` is
    left as given and folded into the same realpath check below, which
    catches a traversal there too (`..`, an absolute path, or a symlink
    that escapes) -- one comparison covers every case, on either side.
    """
    filename = os.path.basename(filename or "")
    if not filename or filename in (".", ".."):
        raise ValueError("that output filename is not allowed")
    base = os.path.realpath(LOCAL_OUTPUTS_DIR)
    path = os.path.realpath(os.path.join(LOCAL_OUTPUTS_DIR, job_id or "", filename))
    if path != base and not path.startswith(base + os.sep):
        raise ValueError("that output path is not allowed")
    return path


def run_post_step(lane, job):
    """If `job`'s mode declares a `post` step (engines.post_for), fetch the
    lane's primary render, run the pack's pure-Python transform, and keep the
    result under LOCAL_OUTPUTS_DIR/<job_id>/ as an ADDITIONAL output of type
    "local" -- the lane's own render stays in job["outputs"] untouched.

    Runs OUTSIDE JOBS_LOCK (a network fetch + Pillow work can take seconds;
    holding the lock that long would stall every other job's status update).
    A `post` that raises, or any I/O failure here, fails the job with a plain
    sentence -- this must never let an exception escape into the poller
    thread, which would silently stop it polling every other job too.
    """
    post_fn = engines.post_for(job["kind"], job["mode"])
    if not post_fn or not job.get("outputs"):
        return
    primary = job["outputs"][0]
    try:
        params = {"filename": primary["filename"], "subfolder": primary.get("subfolder", ""),
                  "type": primary.get("type", "output")}
        url = lane_url(lane, "/view?" + urllib.parse.urlencode(params))
        data, _ctype = http_get_bytes(url, timeout=120.0)
        out_bytes, out_name = post_fn(data, primary["filename"], job.get("args") or {})
        # The pack's return value is UNTRUSTED input, same as a query string --
        # local_output_path() basenames it and refuses anything that would
        # land outside LOCAL_OUTPUTS_DIR, the same rule proxy_view_local()
        # uses on the read side.
        path = local_output_path(job["id"], out_name)
        out_name = os.path.basename(path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(out_bytes)
        os.replace(tmp, path)
        ext = os.path.splitext(out_name)[1].lower()
        media = "image" if ext in (".png", ".jpg", ".jpeg", ".webp") else "file"
        with JOBS_LOCK:
            jj = JOBS.get(job["id"])
            if jj and jj["status"] == "done":
                jj["outputs"].append({"filename": out_name, "subfolder": job["id"],
                                       "type": "local", "media": media, "post": True})
    except ValueError as e:
        # An authored sentence -- local_output_path()'s own containment
        # message, or a pack's own `post` step raising deliberately (rule 3)
        # -- stays verbatim, same as everywhere else in this file.
        with JOBS_LOCK:
            jj = JOBS.get(job["id"])
            if jj:
                jj["status"] = "error"
                jj["error"] = str(e) or "The %s's after-step failed." % job["kind"]
        log("%s's local processing step failed: %s" % (job["kind"].title(), str(e)[:300]), "error")
    except Exception as e:
        # E1: anything else (a disk/IO error, a pack's post() crashing on
        # something that is not its own validation) is Python's own text --
        # this job's "error" reaches the page verbatim (index.html shows
        # job.error), so it must never be that. Full text still goes to the
        # server log, just not to the user.
        with JOBS_LOCK:
            jj = JOBS.get(job["id"])
            if jj:
                jj["status"] = "error"
                jj["error"] = "The %s's after-step failed." % job["kind"]
        log("%s's local processing step failed: %s" % (job["kind"].title(), str(e)[:300]), "error")
    save_jobs()


def seq_harvest(lane, job):
    """Finding 6 / §6 Harvest: when job_poller marks a job carrying
    `sequence_id` done, copy its result (result_output(): the post step's output, else the first) into
    data/seq/<id>/takes/<job id>.<ext> (via carry()'s source-bytes half --
    the lane's /view, or the local store for a type "local" output, exactly
    as run_post_step's own fetch does). A failed harvest logs and leaves the
    take's `file` null; it never fails the job. It is NOT retried: job_poller
    only revisits queued/running jobs, so a harvest that fails (e.g. the lane
    went down between "done" and the copy) stays `file: null`.

    SEQ_LOCK is held only around the JSON write below, never around the
    fetch -- the same fix as add_ref's, and for the same reason: SEQ_LOCK is
    one lock across every sequence."""
    sid, slot_id = job.get("sequence_id"), job.get("slot_id")
    if not sid or not slot_id or not job.get("outputs"):
        return
    out = result_output(job)
    ext = os.path.splitext(out.get("filename") or "")[1].lower() or ".bin"
    dest = os.path.join(SEQ_MEDIA_DIR, sid, "takes", job["id"] + ext)
    try:
        data = _carry_source_bytes(job, out)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)
    except Exception as e:
        log("Could not copy the take for a sequence (not copied yet -- is its lane off?): %s" % str(e)[:300], "warn")
        return
    try:
        with SEQ_LOCK:
            seq = _seq_read(sid)
            if seq is None:
                return
            found = False
            for slot in seq.get("slots") or []:
                if slot.get("id") != slot_id:
                    continue
                for t in slot.get("takes") or []:
                    if t.get("job_id") == job["id"]:
                        t["file"] = "takes/%s%s" % (job["id"], ext)
                        found = True
            if not found:
                return
            seq["rev"] += 1
            seq["updated"] = time.time()
            _seq_write(seq)
    except SeqDamaged as e:
        log("Could not record a harvested take: %s" % e, "warn")


def job_poller():
    """Authoritative completion check. The websocket drives the step counter;
    /history decides done-ness, so a dropped socket never loses a job."""
    while True:
        try:
            with JOBS_LOCK:
                active = [dict(j) for j in JOBS.values()
                          if j.get("status") in ("queued", "running") and j.get("prompt_id")]
            for job in active:
                lane = LANE_BY_ID.get(job["lane"])
                if not lane:
                    continue
                # Nothing on this fleet legitimately runs for six hours. If we are
                # still waiting after that, the lane was restarted under us.
                if time.time() - job.get("started", 0) > 6 * 3600:
                    with JOBS_LOCK:
                        if job["id"] in JOBS:
                            JOBS[job["id"]]["status"] = "interrupted"
                    save_jobs()
                    continue
                if job["status"] == "queued":
                    with STATE_LOCK:
                        live = LANE_STATE.get(lane["id"], {}).get("live_ids") or []
                    if job["prompt_id"] in live:
                        with JOBS_LOCK:
                            if job["id"] in JOBS and JOBS[job["id"]]["status"] == "queued":
                                JOBS[job["id"]]["status"] = "running"
                try:
                    hist = http_get_json(lane_url(lane, "/history/%s" % job["prompt_id"]), timeout=6.0)
                except Exception:
                    # /history unreachable says nothing about the job; a
                    # broken read must not count toward the lost rule.
                    with JOBS_LOCK:
                        j = JOBS.get(job["id"])
                        if j is not None:
                            j["missing"] = 0
                    continue
                entry = hist.get(job["prompt_id"])
                if not entry:
                    # No history and not in the queue. If the lane is up and
                    # its /queue was read cleanly AFTER this job was submitted
                    # (plus grace), the lane has no record of the prompt -- it
                    # was restarted under us. One miss can be the gap between
                    # ComfyUI finishing and writing history; two consecutive
                    # misses are not. A failed condition resets the count.
                    with STATE_LOCK:
                        st = LANE_STATE.get(lane["id"], {})
                        qok = st.get("queue_checked", 0) > job.get("started", 0) + 10
                        live = st.get("live_ids") or []
                        forgotten = bool(st.get("up")) and qok and job["prompt_id"] not in live
                    changed = marked = False
                    with JOBS_LOCK:
                        j = JOBS.get(job["id"])
                        if not j:
                            continue
                        want = (j.get("missing", 0) + 1) if forgotten else 0
                        if forgotten and want >= 2 and j["status"] in ("queued", "running"):
                            j["status"] = "interrupted"
                            j["error"] = "The machine restarted and lost this one."
                            j["finished"] = time.time()
                            j["missing"] = want
                            marked = True
                            log("%s on %s was lost when the machine restarted" % (j["kind"].title(), lane["name"]), "error")
                        elif want != j.get("missing", 0):
                            j["missing"] = want
                            changed = True
                    if marked or changed:
                        save_jobs()
                    continue
                status = (entry.get("status") or {})
                if not status.get("completed") and status.get("status_str") != "error":
                    continue
                outs = collect_outputs(entry)
                with JOBS_LOCK:
                    j = JOBS.get(job["id"])
                    if not j or j["status"] in ("done", "error"):
                        continue
                    if status.get("status_str") == "error" or (not outs and status.get("completed") is False):
                        j["status"] = "error"
                        msg = ""
                        for m in status.get("messages", []):
                            if m and m[0] == "execution_error":
                                msg = str(m[1].get("exception_message", ""))[:300]
                        j["error"] = msg or "the lane reported an error"
                        log("%s on %s stopped with an error" % (j["kind"].title(), lane["name"]), "error")
                    elif status.get("completed") and not outs:
                        # F2: the prompt completed but produced nothing this
                        # side recognizes as output -- a recipe missing its
                        # save step, not a "done" job with an empty result.
                        j["status"] = "error"
                        j["error"] = ("The machine finished but saved nothing "
                                      "— the recipe may be missing its save step.")
                        log("%s on %s finished but saved nothing" % (j["kind"].title(), lane["name"]), "error")
                    else:
                        j["status"] = "done"
                        j["outputs"] = outs
                        j["finished"] = time.time()
                        j["elapsed"] = round(j["finished"] - j.get("started", j["finished"]), 1)
                        log("%s finished on %s in %s" % (j["kind"].title(), lane["name"], human_time(j["elapsed"])), "ok")
                save_jobs()
                if j["status"] == "done":
                    run_post_step(lane, j)
                    seq_harvest(lane, j)
        except Exception:
            traceback.print_exc()
        time.sleep(JOB_POLL_SECONDS)


# ---------------------------------------------------------------------------
# The VRAM rule
# ---------------------------------------------------------------------------

def free_colliding_lanes(target_lane):
    """Two large models will not both fit on one 24GB card. Two lanes on the SAME card
    cannot both hold weights. Before dispatching, drop the other lane's weights.

    Returns (ok, notes). ok=False means a sibling is actively rendering and we
    must NOT yank its weights -- we refuse the dispatch instead of OOMing it.
    """
    notes = []
    # A process lane holds no weights, on no card: it collides with nothing
    # and is never asked to free. (Its gpu is "", which would otherwise make
    # every process lane "share a card" with every other one.)
    if lane_kind(target_lane) == "process":
        return True, []
    sibs = [l for l in LANES if l["gpu"] == target_lane["gpu"] and l["id"] != target_lane["id"]]
    for sib in sibs:
        with STATE_LOCK:
            st = dict(LANE_STATE.get(sib["id"], {}))
        if not st.get("up"):
            continue
        try:
            q = http_get_json(lane_url(sib, "/queue"), timeout=5.0)
        except Exception:
            notes.append("Could not check whether %s is busy, so I left its card alone" % sib["name"])
            continue
        busy = len(q.get("queue_running", [])) + len(q.get("queue_pending", []))
        if busy:
            # Only suggest a lane that can actually do the thing being asked for.
            with STATE_LOCK:
                other = [l["name"] for l in LANES
                         if set(l["caps"]) & set(target_lane["caps"])
                         and l["gpu"] != target_lane["gpu"]
                         and LANE_STATE.get(l["id"], {}).get("up")]
            tip = ("Try %s instead, or wait about 10 minutes." % other[0]) if other \
                else "Give it about 10 minutes and try again."
            return False, ["%s is busy on %s right now, and both jobs will not fit on one card. %s"
                           % (sib["name"], sib["gpu_label"], tip)]
        before = st.get("vram_free", 0)
        try:
            http_post_json(lane_url(sib, "/free"), {"unload_models": True, "free_memory": True}, timeout=30.0)
        except Exception as e:
            notes.append("Could not make room on %s (%s). Going ahead anyway." % (sib["gpu_label"], e))
            continue
        time.sleep(FREE_SETTLE_SECONDS)
        try:
            after = http_get_json(lane_url(sib, "/system_stats"), timeout=5.0)["devices"][0]["vram_free"]
        except Exception:
            after = before
        gained = after - before
        if gained > 512 * 1024 * 1024:
            notes.append("Made room on %s: %s let go of %.1f GB (%.1f GB free now)"
                         % (sib["gpu_label"], sib["name"], gained / 1e9, after / 1e9))
        else:
            notes.append("%s was already clear, nothing to move" % sib["gpu_label"])
    for n in notes:
        log(n, "vram")
    return True, notes


# ---------------------------------------------------------------------------
# Graph builders
# ---------------------------------------------------------------------------

def snap_frames(length):
    """H3's frame grid is 17n+5. 362 = 15.1s is the trained max; 124 = ~5s the min."""
    length = int(length)
    n = max(0, int(round((length - 5) / 17.0)))
    n = max(7, min(21, n))          # 124 .. 362
    return 17 * n + 5


CLEAN_STRIP_REFUSED = ("Could not remove the recipe from this file, so it was not downloaded. "
                        "Use the plain download if you want the original.")


def strip_metadata(data, ctype, filename=""):
    """Return the same file with its embedded metadata removed, and whether
    it actually was.

    PNG is lossless, so a Pillow round-trip preserves every pixel exactly while
    dropping the text chunks -- verified max pixel delta 0. JPEG/WebP are
    re-encoded by the same Pillow round-trip (not lossless for those formats).

    Audio goes to sanitize.strip_audio, which carries the same guarantee for
    FLAC/MP3/Opus: MEASURED on real renders, the decoded audio is byte-identical
    (ffmpeg -f md5 matches) while the embedded recipe goes from present to gone.
    It reports whether it actually cleaned the file; a format it does not fully
    understand (including WAV and M4A, which it does not implement) comes back
    UNCHANGED with stripped=False rather than mangled.

    Video (.mp4/.webm/.mov/.mkv) goes to sanitize.strip_video, an ffmpeg
    stream-copy remux (never a re-encode) that drops container metadata and
    chapters. It reports whether it actually cleaned the file; ffmpeg absent,
    an extension it doesn't handle, or any failure comes back UNCHANGED.

    Returns (bytes, content_type, stripped) where `stripped` is:
      True  -- actually cleaned; safe to serve as "recipe removed".
      False -- this is a kind we are meant to be able to clean (audio/video/
               image extension) but the strip did not happen this time
               (unsupported sub-format, missing dependency, parse failure).
               Callers that promised a clean download MUST refuse rather
               than silently serve the original under that label.
      None  -- not a kind this function attempts to clean at all (e.g. a
               3D mesh); the original was never claimed to be sanitized, so
               passing it through unchanged breaks no promise.
    """
    name = (filename or "").lower()
    if name.endswith((".flac", ".mp3", ".opus", ".wav", ".m4a", ".ogg")):
        try:
            import sanitize
            out, stripped = sanitize.strip_audio(data, name)
            return out, ctype, stripped
        except Exception:
            return data, ctype, False      # a failed strip must not break the download
    if name.endswith((".mp4", ".webm", ".mov", ".mkv")):
        try:
            import sanitize
            out, stripped = sanitize.strip_video(data, name)
            return out, ctype, stripped
        except Exception:
            return data, ctype, False      # a failed strip must not break the download
    if not (name.endswith(".png") or name.endswith(".jpg") or name.endswith(".jpeg")
            or name.endswith(".webp")):
        return data, ctype, None    # not a media kind we know how to clean at all
    try:
        from PIL import Image
    except ImportError:
        return data, ctype, False   # Pillow absent: never silently corrupt the file
    try:
        import io as _io
        src = Image.open(_io.BytesIO(data))
        src.load()   # decode now, before .tobytes() -- src.format/.mode below need it settled
        # Round-trip raw pixel bytes rather than Image.getdata()/putdata() (the
        # latter removed in Pillow 14, 2027-10-15, per Pillow's own
        # DeprecationWarning): frombytes() drops every text/metadata chunk the
        # same way, is not deprecated, and is the exact byte content, not a
        # per-pixel Python list round-trip.
        clean = Image.frombytes(src.mode, src.size, src.tobytes())
        if src.mode == "P":
            # A "P" (palette) image's tobytes() is index bytes only -- the
            # palette itself lives separately and must be copied too, or the
            # clean copy's colours come out wrong (index N != colour N).
            pal = src.getpalette()
            if pal is not None:
                clean.putpalette(pal)
        out = _io.BytesIO()
        clean.save(out, format=src.format)
        return out.getvalue(), ctype, True
    except Exception:
        return data, ctype, False   # a failed strip must not break the download


# Graphs live in engine packs (engines/). The core does not know how any model
# is wired -- it asks engines.graph_for(cap, mode, args, models).


def apply_quality(p, cap, mode, able):
    """R3: resolve an optional "quality" tier id into field overrides.

    Returns (p, error). Omitting "quality" leaves `p` untouched -- today's
    behaviour exactly. A tier's `values` fill ONLY the keys the submitted
    body does not already carry (an explicit value from the caller wins),
    so an API caller that sends only a tier id still gets the tier's
    numbers. A tier that
    `requires` an ability this lane lacks refuses with its own
    `unavailable_reason` rather than silently falling back.
    """
    qid = p.get("quality")
    if not qid:
        return p, None
    for tier in engines.quality(cap, mode):
        if tier["id"] != qid:
            continue
        req = tier.get("requires")
        if req and not able.get(req):
            return p, (tier.get("unavailable_reason") or "That quality setting is not available on this lane.")
        p = dict(p)
        for k, v in tier["values"].items():
            p.setdefault(k, v)
        return p, None
    return p, "Unknown quality setting %r." % qid


def estimate_seconds(lane_id, cap, mode, quality_id):
    """R5: MEDIAN elapsed seconds of this lane's finished jobs with this
    exact (mode, quality tier). None with fewer than 3 -- never extrapolated
    from a different tier or a different lane."""
    with JOBS_LOCK:
        vals = sorted(j["elapsed"] for j in JOBS.values()
                      if j.get("lane") == lane_id and j.get("kind") == cap and j.get("mode") == mode
                      and j.get("quality") == quality_id and j.get("status") == "done" and "elapsed" in j)
    if len(vals) < 3:
        return None
    n, mid = len(vals), len(vals) // 2
    return vals[mid] if n % 2 else round((vals[mid - 1] + vals[mid]) / 2, 1)


def dispatch(lane, graph, kind, mode, meta):
    ok, notes = free_colliding_lanes(lane)
    if not ok:
        return {"ok": False, "error": notes[0], "notes": notes}

    body = {"prompt": graph, "client_id": LANE_CLIENT_ID[lane["id"]]}
    res = http_post_json(lane_url(lane, "/prompt"), body, timeout=60.0)
    if "_http_error" in res or not res.get("prompt_id"):
        # Keep the machine detail for the expander; show the user one plain sentence.
        detail = res.get("_body", res)
        detail_txt = json.dumps(detail)[:1500] if isinstance(detail, dict) else str(detail)[:1500]
        log("%s turned the job down: %s" % (lane["name"], detail_txt[:300]), "error")
        return {"ok": False,
                "error": "%s would not take that job. Nothing was lost - change a setting and try again, "
                         "or send it to another lane." % lane["name"],
                "detail": detail_txt, "notes": notes}

    jid = uuid.uuid4().hex[:12]
    job = {
        "id": jid, "lane": lane["id"], "lane_name": lane["name"], "prompt_id": res["prompt_id"],
        "kind": kind, "mode": mode, "status": "queued", "step": 0, "total": meta.get("steps", 0),
        "created": time.time(), "started": time.time(), "updated": time.time(),
        "outputs": [], "notes": notes,
    }
    job.update(meta)
    job["licence"] = engines.licence_for(kind, mode)   # R6
    with JOBS_LOCK:
        JOBS[jid] = job
        JOB_ORDER.append(jid)
        PROMPT_INDEX[(lane["id"], res["prompt_id"])] = jid
    log("%s started on %s" % (kind.title(), lane["name"]), "ok")
    save_jobs()
    return {"ok": True, "job": job, "notes": notes}


# ---------------------------------------------------------------------------
# Image fitting for the workflow chain
#
# Upstream shelled out to /usr/bin/sips, a macOS builtin. This fleet is Linux on
# both hosts, so that path could never run here. Pillow replaces it with identical
# semantics: sips -c is a CENTERED crop, sips -z resamples to an exact HxW.
#
# Imported lazily and on purpose: everything except picture->video works with no
# third-party package installed, so a missing Pillow degrades one feature with a
# clear sentence instead of preventing the app from starting.
# ---------------------------------------------------------------------------

def _pil():
    try:
        from PIL import Image
        return Image
    except ImportError:
        raise RuntimeError(
            "Picture -> video needs Pillow for the image fit step. "
            "Install it with: python3 -m venv .venv && "
            ".venv/bin/python -m pip install Pillow"
        )


def image_size(path):
    Image = _pil()
    with Image.open(path) as im:
        return im.width, im.height


def fit_to_aspect(src, dst, target_w, target_h):
    """Center-crop to the video aspect, then scale to the exact render size.
    Returns a human sentence describing what we did, for the UI."""
    Image = _pil()
    with Image.open(src) as im:
        im = im.convert("RGB") if im.mode not in ("RGB", "RGBA") else im
        sw, sh = im.width, im.height
        src_ar = sw / float(sh)
        tgt_ar = target_w / float(target_h)

        if abs(src_ar - tgt_ar) < 0.01:
            im.resize((target_w, target_h), Image.LANCZOS).save(dst)
            return "aspect already matched, scaled %dx%d -> %dx%d" % (sw, sh, target_w, target_h)

        if src_ar > tgt_ar:
            cw, ch = int(round(sh * tgt_ar)), sh
        else:
            cw, ch = sw, int(round(sw / tgt_ar))
        left, top = (sw - cw) // 2, (sh - ch) // 2
        im.crop((left, top, left + cw, top + ch)) \
          .resize((target_w, target_h), Image.LANCZOS).save(dst)
        return "center-cropped %dx%d to %dx%d, then scaled to %dx%d" % (
            sw, sh, cw, ch, target_w, target_h)


# ---------------------------------------------------------------------------
# carry(): move a finished still from one lane's disk onto another's, or into
# a sequence's reference room. See the internal sequence/storyboard design spec
# §2. api_chain is now a thin wrapper around this (byte-identical response,
# gated by tests/test_refs.py); add_ref calls the source-bytes half only, with
# no upload, to copy a reference into data/seq/<id>/refs/.
# ---------------------------------------------------------------------------

class CarryError(ValueError):
    """A carry() failure. Still a ValueError (callers that only catch that
    keep working), plus the extra `detail` and HTTP `code` api_chain has
    always attached to some of these."""
    def __init__(self, sentence, detail=None, code=400):
        super().__init__(sentence)
        self.detail = detail
        self.code = code


def _carry_source_bytes(job, out, cache_path=None):
    """The bytes for one job output. `cache_path`, if given and it exists, is
    read with the source lane never contacted. Otherwise an output of type
    "local" is read from LOCAL_OUTPUTS_DIR (never a lane); anything else is
    fetched from the source lane's /view. When cache_path is given and did
    not already exist, the fetched bytes are written there before returning."""
    if cache_path and os.path.exists(cache_path):
        with open(cache_path, "rb") as f:
            return f.read()
    if out.get("type") == "local":
        with open(local_output_path(out.get("subfolder"), out["filename"]), "rb") as f:
            data = f.read()
    else:
        src_lane = LANE_BY_ID.get(job.get("lane"))
        if not src_lane:
            raise CarryError("unknown lane")
        url = lane_url(src_lane, "/view?" + urllib.parse.urlencode(
            {"filename": out["filename"], "subfolder": out.get("subfolder", ""),
             "type": out.get("type", "output")}))
        data, _ = http_get_bytes(url, timeout=120.0)
    if cache_path:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "wb") as f:
            f.write(data)
    return data


def carry(job, output_index, target_lane, fit=None, cache_path=None):
    """Extracted from api_chain. `target_lane` is an already-resolved lane
    dict. Returns (name, note); raises CarryError (a ValueError) with a plain
    sentence on failure -- api_chain turns that back into the same JSON
    errors it has always returned.

    fit=(w, h) keeps api_chain's own behaviour exactly: centre-crop+scale via
    fit_to_aspect, written under CHAIN_DIR. fit=None uploads the source
    pixels unchanged (references go over uncropped). Uploads with
    overwrite=false, every call -- no per-lane name cache."""
    outs = job.get("outputs") or []
    idx = output_index
    if not isinstance(idx, int) or isinstance(idx, bool) or idx >= len(outs):
        raise CarryError("That result has no picture to carry over.")
    out = outs[idx]
    data = _carry_source_bytes(job, out, cache_path)

    if fit is not None:
        vw, vh = fit
        base = "chain_%s_%d" % (job["id"], idx)
        raw_path = os.path.join(CHAIN_DIR, base + "_raw.png")
        fit_path = os.path.join(CHAIN_DIR, base + "_%dx%d.png" % (vw, vh))
        with open(raw_path, "wb") as f:
            f.write(data)
        try:
            note = fit_to_aspect(raw_path, fit_path, vw, vh)
        except Exception as e:
            fit_path = raw_path
            note = "could not resize locally (%s), sent the image at its original size" % e
        with open(fit_path, "rb") as f:
            payload = f.read()
        upload_name = os.path.basename(fit_path)
    else:
        payload = data
        note = "sent uncropped"
        upload_name = os.path.basename(out["filename"])

    res = http_post_multipart(
        lane_url(target_lane, "/upload/image"),
        {"type": "input", "overwrite": "false"},
        [("image", upload_name, "image/png", payload)])
    if "_http_error" in res or not res.get("name"):
        raise CarryError("%s would not take the picture. Try the other lane." % target_lane["name"],
                          detail=str(res)[:800], code=502)
    name = res["name"]
    if res.get("subfolder"):
        name = res["subfolder"] + "/" + name
    return name, note


def carry_result(p):
    """The whole POST /api/carry body -> (body, http code): "Edit this
    result". One finished picture output of a job goes onto a lane as an
    input, unchanged, through carry(); the page then puts the returned name
    in a mode's picture field, like an upload. Results only, and the bytes
    go lane to lane: nothing new is served to the page."""
    if not isinstance(p, dict):
        return {"ok": False, "error": "Send a JSON object."}, 400
    job_id = p.get("job_id")
    with JOBS_LOCK:
        job = dict(JOBS.get(job_id) or {}) if isinstance(job_id, str) else {}
    if not job:
        return {"ok": False, "error": "I cannot find that result any more."}, 404
    if job.get("status") != "done":
        return {"ok": False, "error": "That result is not finished yet."}, 400
    outs = job.get("outputs") or []
    idx = p.get("output")
    if not isinstance(idx, int) or isinstance(idx, bool) or not (0 <= idx < len(outs)):
        return {"ok": False, "error": "That result has no output number %s." % idx}, 400
    out = outs[idx]
    media = out.get("media") or (mimetypes.guess_type(out.get("filename") or "")[0] or "").split("/")[0]
    if media != "image":
        return {"ok": False, "error": "Only a finished picture can be used as a picture to work from."}, 400
    lane = LANE_BY_ID.get(p.get("lane")) if isinstance(p.get("lane"), str) else None
    if not lane:
        return {"ok": False, "error": "unknown lane"}, 400
    if lane_kind(lane) == "process":
        return {"ok": False, "error": "%s takes files you upload, not results." % lane["name"]}, 400
    with STATE_LOCK:
        if not LANE_STATE.get(lane["id"], {}).get("up"):
            return {"ok": False, "error": "%s is offline right now. Pick a lane glowing green." % lane["name"]}, 409
    try:
        name, _note = carry(job, idx, lane, fit=None)
    except CarryError as e:
        body = {"ok": False, "error": str(e)}
        if e.detail is not None:
            body["detail"] = e.detail
        return body, e.code
    except Exception as e:
        return {"ok": False, "error": "I cannot fetch that result from %s right now."
                % (job.get("lane_name") or "its machine"), "detail": str(e)[:800]}, 502
    log("Sent a result over to %s to work from" % lane["name"])
    return {"ok": True, "file": {"name": name, "original": out.get("filename") or "", "job_id": job["id"]}}, 200


# ---------------------------------------------------------------------------
# Prompt helper (L5): "Help me write this" / "Describe this picture". An
# optional OpenAI-compatible chat endpoint named in config.json's "helper" --
# absent, /api/helper 404s and the page draws no button. Engine knowledge
# (how a prompt for THIS mode must be written) lives in each pack's own
# prompt_guides (engines/__init__.py's pack contract), never here -- this
# module names no model, same as everywhere else in server.py.
# ---------------------------------------------------------------------------

HELPER_TEXT_LIMIT = 4000
HELPER_IMAGE_LIMIT = 8 * 1024 * 1024
HELPER_ANSWER_LIMIT = 2000
HELPER_BUSY_SENTENCE = "The helper is busy or switched off right now."
GENERIC_PROMPT_GUIDE = "Write a short, plain description of what to generate."

_THINK_RE = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)


def _clean_helper_text(text):
    """Strip a <think>...</think> block, then surrounding quotes/whitespace,
    then cap the length -- the whole point being a raw model answer never
    reaches the prompt box unfiltered."""
    text = _THINK_RE.sub("", text or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return text[:HELPER_ANSWER_LIMIT]


HELPER_CONNECT_TIMEOUT = 3.0


def _helper_connect_check():
    """P3: a plain HTTP request's timeout covers connect AND read together,
    so an unreachable (not merely refusing) helper host used to hang for the
    full read timeout_s (default 60s) before the page saw anything besides
    "Thinking...". A short TCP connect probe first turns that into an
    immediate, specific sentence; a helper that DOES answer, just slowly,
    still gets the full timeout_s as its read timeout below."""
    parsed = urllib.parse.urlsplit(HELPER["url"])
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=HELPER_CONNECT_TIMEOUT):
            pass
    except OSError:
        raise ValueError(
            "The writing helper at %s:%d isn't answering. "
            "Check \"helper\" in config.json, or remove it to hide the button." % (host, port))


def _helper_chat(messages, max_tokens=512, timeout=None):
    """POST messages to the configured helper's /chat/completions ->
    (content, finish_reason). Raises ValueError(HELPER_BUSY_SENTENCE) on
    timeout, connection failure or a reply this app cannot parse -- a
    caller has exactly one thing to catch and turn into the 503."""
    _helper_connect_check()
    url = HELPER["url"].rstrip("/") + "/chat/completions"
    payload = {"model": HELPER.get("model") or "", "messages": messages, "max_tokens": max_tokens}
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout or HELPER.get("timeout_s", 60)) as r:
            raw = json.loads(r.read().decode("utf-8"))
        choice = raw["choices"][0]
        return choice["message"]["content"] or "", choice.get("finish_reason")
    except Exception as e:
        raise ValueError(HELPER_BUSY_SENTENCE) from e


HELPER_THOUGHT_ONLY = ("Your helper spent its whole answer thinking and wrote nothing. Give it more room with "
                       "\"helper\": {\"max_tokens\": 8192} in config.json, or use a model that doesn't think first.")
HELPER_CUT_OFF = ("The helper's answer was cut off before it finished. Give it more room with "
                  "\"helper\": {\"max_tokens\": 8192} in config.json.")
HELPER_RETRY_TOKENS = 16384
_OPEN_THINK_RE = re.compile(r"<think>(?!.*</think>).*", re.IGNORECASE | re.DOTALL)


class HelperThoughtOnly(Exception):
    """The helper's whole reply budget went on thinking: nothing was written."""


def _guide_helper_chat(messages, max_tokens, retry_cut=False):
    """_helper_chat for a guide call (chat, skill, revise) -> (text with any
    <think> block removed, finish_reason). config's helper.max_tokens raises
    the budget, never lowers it. A thinking model can spend the whole budget
    before writing a word (finish "length", nothing left once the thought is
    removed): that gets ONE retry at four times the budget, capped at
    HELPER_RETRY_TOKENS; still nothing raises HelperThoughtOnly. With
    retry_cut (a write or a fix, whose fields a cut-off answer would leave
    half-written) a reply cut off WITH words gets the same one retry; the
    caller reads the returned finish to see whether it was still cut. Chat
    shows a cut-off reply as truncated instead. Raises
    ValueError(HELPER_BUSY_SENTENCE) like _helper_chat."""
    budget = max(max_tokens, HELPER.get("max_tokens") or 0)
    timeout = HELPER.get("timeout_s", 120)
    reply, finish = _helper_chat(messages, max_tokens=budget, timeout=timeout)
    text = _OPEN_THINK_RE.sub("", _THINK_RE.sub("", reply or "")).strip()
    if finish != "length" or (text and not retry_cut):
        return text, finish
    if budget < HELPER_RETRY_TOKENS:
        reply, finish = _helper_chat(messages, max_tokens=min(budget * 4, HELPER_RETRY_TOKENS), timeout=timeout)
        text = _OPEN_THINK_RE.sub("", _THINK_RE.sub("", reply or "")).strip()
    if not text and finish == "length":
        raise HelperThoughtOnly(HELPER_THOUGHT_ONLY)
    return text, finish


def _resolve_job_output_bytes(job_id, output_index):
    """-> (bytes, filename) for a finished job's output, the same source-
    bytes path a chain/carry uses."""
    with JOBS_LOCK:
        job = dict(JOBS.get(job_id or "") or {})
    if not job:
        raise ValueError("I cannot find that result any more.")
    outs = job.get("outputs") or []
    idx = output_index
    if not isinstance(idx, int) or isinstance(idx, bool) or idx < 0 or idx >= len(outs):
        raise ValueError("That result has no picture to describe.")
    out = outs[idx]
    return _carry_source_bytes(job, out), out.get("filename") or "image.png"


def _resolve_upload_bytes(lane_id, name):
    """-> (bytes, filename) for an uploaded input, reached the same contained
    way an upload already is: a process lane's own uploads dir (basename
    only, no traversal); a comfy lane's /view?type=input (subfolder split
    off the stored name, same as _carry_source_bytes)."""
    if not name or "\x00" in name or ".." in name.split("/"):
        raise ValueError("that upload name is not allowed")
    lane = LANE_BY_ID.get(lane_id)
    if not lane:
        raise ValueError("unknown lane")
    if lane_kind(lane) == "process":
        safe = os.path.basename(name)
        if not safe or safe in (".", ".."):
            raise ValueError("that upload name is not allowed")
        path = os.path.join(UPLOADS_DIR, lane["id"], safe)
        if not os.path.isfile(path):
            raise ValueError("I cannot find that upload any more.")
        with open(path, "rb") as f:
            return f.read(), safe
    subfolder, _, filename = name.rpartition("/")
    if not filename:
        raise ValueError("that upload name is not allowed")
    url = lane_url(lane, "/view?" + urllib.parse.urlencode(
        {"filename": filename, "subfolder": subfolder, "type": "input"}))
    try:
        data, _ = http_get_bytes(url, timeout=30.0)
    except Exception as e:
        raise ValueError("I cannot find that upload any more.") from e
    return data, filename


def helper_request(p):
    """The whole POST /api/helper body. Returns (body, http code), same
    calling convention as generate()."""
    action = p.get("action")
    if action not in ("write", "describe"):
        return {"ok": False, "error": "action must be \"write\" or \"describe\""}, 400
    text = p.get("text") or ""
    if not isinstance(text, str) or len(text) > HELPER_TEXT_LIMIT:
        return {"ok": False, "error": "That text is too long."}, 400
    guide = engines.prompt_guide(p.get("cap"), p.get("mode")) or GENERIC_PROMPT_GUIDE
    try:
        if action == "write":
            messages = [{"role": "system", "content": guide + " Return only the prompt text."},
                        {"role": "user", "content": text}]
        else:
            image = p.get("image") or {}
            if not isinstance(image, dict):
                return {"ok": False, "error": "no picture given"}, 400
            if image.get("job_id"):
                data, filename = _resolve_job_output_bytes(image.get("job_id"), image.get("output"))
            elif image.get("upload"):
                data, filename = _resolve_upload_bytes(image.get("lane"), image.get("upload"))
            else:
                return {"ok": False, "error": "no picture given"}, 400
            if len(data) > HELPER_IMAGE_LIMIT:
                return {"ok": False, "error": "That picture is too large."}, 400
            mime = mimetypes.guess_type(filename)[0] or "image/png"
            if not mime.startswith("image/"):
                mime = "image/png"
            data_url = "data:%s;base64,%s" % (mime, base64.b64encode(data).decode("ascii"))
            messages = [{"role": "system", "content": guide + " Describe this picture as a prompt for this mode."},
                        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": data_url}}]}]
    except ValueError as e:
        return {"ok": False, "error": str(e)}, 400
    try:
        reply, _ = _helper_chat(messages)
    except ValueError as e:
        return {"ok": False, "error": str(e)}, 503
    return {"ok": True, "text": _clean_helper_text(reply)}, 200


# ---------------------------------------------------------------------------
# Room guides: GET /api/guide and POST /api/guide/chat. The guide (guides.py)
# supplies the voice as a system prompt; the conversation lives in the page
# and is sent whole with each turn, so the server keeps no chat state. A reply
# the helper cut short, or one over the guide's own cap, is flagged
# `truncated` -- never sliced silently.
# ---------------------------------------------------------------------------

GUIDE_HISTORY_CHARS = 24000
GUIDE_HISTORY_TURNS = 40
GUIDE_CONTEXT_TTL = 300.0
GUIDE_ADD_BRAIN = ("To let the guide talk with you, add a \"helper\" to config.json "
                   "(any OpenAI-compatible chat model); see README.")
_GUIDE_CONTEXT = {"at": None, "value": None}
_GUIDE_CONTEXT_LOCK = threading.Lock()


def _room_guide(room_id):
    """-> (room exists, its guide or None)."""
    room = next((r for r in engines.rooms() if r.get("id") == room_id), None)
    if room is None:
        return False, None
    return True, GUIDES.get(room.get("guide")) if room.get("guide") else None


def _ctx_int(v):
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else None


def _get_json(url):
    with urllib.request.urlopen(url, timeout=3.0) as r:
        return json.loads(r.read().decode("utf-8"))


def _helper_context():
    """The helper's context size in tokens, or None when nothing says.

    First answer wins: config's "helper": {"context": N}; then GET /props
    (llama.cpp's served -c, at the helper url minus a trailing /v1); then
    GET /models (n_ctx / context_length / max_context_length, and last the
    TRAINING context n_ctx_train, which can overstate the served window).
    Probed at most once per GUIDE_CONTEXT_TTL, failures included; never raises."""
    if not HELPER:
        return None
    if _ctx_int(HELPER.get("context")):
        return HELPER["context"]
    with _GUIDE_CONTEXT_LOCK:
        now = time.time()
        if _GUIDE_CONTEXT["at"] is not None and now - _GUIDE_CONTEXT["at"] < GUIDE_CONTEXT_TTL:
            return _GUIDE_CONTEXT["value"]
        value = None
        base = HELPER["url"].rstrip("/")
        try:
            props = _get_json((base[:-3] if base.endswith("/v1") else base) + "/props")
            settings = props.get("default_generation_settings")
            value = _ctx_int((settings or {}).get("n_ctx") if isinstance(settings, dict) else None) \
                or _ctx_int(props.get("n_ctx"))
        except Exception:
            value = None
        if value is None:
            try:
                raw = _get_json(base + "/models")
                entries = [e for e in (raw.get("data") or raw.get("models") or []) if isinstance(e, dict)]
                entry = next((e for e in entries if e.get("id") == HELPER.get("model")),
                             entries[0] if entries else {})
                meta = entry.get("meta") if isinstance(entry.get("meta"), dict) else {}
                for v in (meta.get("n_ctx"), entry.get("context_length"),
                          entry.get("max_context_length"), meta.get("n_ctx_train")):
                    if _ctx_int(v):
                        value = v
                        break
            except Exception:
                value = None
        _GUIDE_CONTEXT.update(at=now, value=value)
        return value


_GUIDE_VISION = {"at": None, "value": False}
VISION_CAPABILITIES = ("multimodal", "vision")


def _helper_vision():
    """Whether the helper can see pictures. First answer wins: config's
    "helper": {"vision": true|false}; then GET /models, where an entry whose
    "capabilities" list names multimodal or vision means yes (the entry for
    the configured model when one matches, else the first that lists any).
    Anything else, or an unreachable probe, means no. Cached like
    _helper_context(); never raises."""
    if not HELPER:
        return False
    if isinstance(HELPER.get("vision"), bool):
        return HELPER["vision"]
    with _GUIDE_CONTEXT_LOCK:
        now = time.time()
        if _GUIDE_VISION["at"] is not None and now - _GUIDE_VISION["at"] < GUIDE_CONTEXT_TTL:
            return _GUIDE_VISION["value"]
        value = False
        try:
            raw = _get_json(HELPER["url"].rstrip("/") + "/models")
            entries = [e for key in ("data", "models") for e in (raw.get(key) or []) if isinstance(e, dict)]
            named = [e for e in entries if HELPER.get("model") in
                     [e.get("id"), e.get("name"), e.get("model")] + list(e.get("aliases") or [])]
            listed = [e for e in (named or entries) if isinstance(e.get("capabilities"), list)]
            if listed:
                value = any(str(c).lower() in VISION_CAPABILITIES for c in listed[0]["capabilities"])
        except Exception:
            value = False
        _GUIDE_VISION.update(at=now, value=value)
        return value


def _helper_max_images():
    return (HELPER.get("max_images") or 1) if HELPER else 1


VIDEO_STILL_TIMEOUT = 60


def _video_still(data, filename):
    """A clip's middle frame as PNG bytes, via ffmpeg. Raises ValueError with
    a plain sentence when ffmpeg is missing or cannot read the clip."""
    if not FFMPEG_BIN:
        raise ValueError("To show the guide a clip, ffmpeg must be installed and on PATH "
                         "(it takes one still frame from the middle).")
    with tempfile.TemporaryDirectory(prefix="bwf_still_") as d:
        src = os.path.join(d, "clip" + (os.path.splitext(filename)[1] or ".mp4"))
        with open(src, "wb") as f:
            f.write(data)
        probe = subprocess.run([FFMPEG_BIN, "-hide_banner", "-i", src], capture_output=True, text=True,
                               timeout=VIDEO_STILL_TIMEOUT)
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", probe.stderr or "")
        middle = (int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))) / 2 if m else 0.0
        out = subprocess.run([FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-ss", "%.3f" % middle,
                              "-i", src, "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
                             capture_output=True, timeout=VIDEO_STILL_TIMEOUT)
        if out.returncode != 0 or not out.stdout:
            raise ValueError("I could not take a still from that clip.")
        return out.stdout


def _guide_picture_refs(pictures):
    """Validate a guide call's "pictures" list -> (list, error sentence or None).
    Each entry is {"job_id", "output"} or {"lane", "upload"}; at most
    helper.max_images (default 1)."""
    if pictures is None:
        return [], None
    limit = _helper_max_images()
    if not isinstance(pictures, list) or len(pictures) > limit:
        return None, ("\"pictures\" must be a list of at most %d picture%s."
                      % (limit, "" if limit == 1 else "s"))
    if not all(_picture_ref_ok(pic) for pic in pictures):
        return None, GUIDE_PICTURE_SHAPE
    return pictures, None


GUIDE_PICTURE_SHAPE = ("Each picture must be {\"job_id\", \"output\"} (a result) "
                       "or {\"lane\", \"upload\"} (an upload).")


def _picture_ref_ok(pic):
    """One picture reference: a result {"job_id", "output"} or an upload {"lane", "upload"}."""
    return isinstance(pic, dict) and (
        (isinstance(pic.get("job_id"), str) and isinstance(pic.get("output"), int)
         and not isinstance(pic.get("output"), bool))
        or (isinstance(pic.get("lane"), str) and isinstance(pic.get("upload"), str)))


def _guide_picture_url(pic):
    """One validated picture reference -> an image data URL. A result's video
    becomes its middle still. Raises ValueError with a plain sentence."""
    if pic.get("job_id") is not None:
        with JOBS_LOCK:
            job = dict(JOBS.get(pic["job_id"]) or {})
        outs = job.get("outputs") or []
        if not job:
            raise ValueError("I cannot find that result any more.")
        if not (0 <= pic["output"] < len(outs)):
            raise ValueError("That result has no picture to show the guide.")
        out = outs[pic["output"]]
        media = out.get("media") or (mimetypes.guess_type(out.get("filename") or "")[0] or "").split("/")[0]
        if media not in ("image", "video"):
            raise ValueError("Only a picture or a clip can be shown to the guide.")
        try:
            data = _carry_source_bytes(job, out)
        except Exception as e:
            raise ValueError("I cannot fetch that result from %s right now."
                             % (job.get("lane_name") or "its machine")) from e
        filename = out.get("filename") or "image.png"
        if media == "video":
            data, filename = _video_still(data, filename), "still.png"
    else:
        data, filename = _resolve_upload_bytes(pic["lane"], pic["upload"])
    mime = mimetypes.guess_type(filename)[0] or ""
    if not mime.startswith("image/"):
        raise ValueError("Only a picture can be shown to the guide.")
    if len(data) > HELPER_IMAGE_LIMIT:
        raise ValueError("That picture is too large.")
    return "data:%s;base64,%s" % (mime, base64.b64encode(data).decode("ascii"))


def _with_pictures(text, urls):
    """A user message's content: plain text, or OpenAI content parts with the
    pictures after the text when there are any."""
    if not urls:
        return text
    return [{"type": "text", "text": text}] + [{"type": "image_url", "image_url": {"url": u}} for u in urls]


def guide_payload(room_id):
    """The whole GET /api/guide?room=<id> body -> (body, http code)."""
    exists, g = _room_guide(room_id)
    if not exists:
        return {"ok": False, "error": "There is no room called %s." % (room_id or "that")}, 404
    guide = None
    if g:
        guide = {"id": g["id"], "name": g["name"], "definition": g["definition"],
                 "greeting": g["greeting"], "no_brain": g["no_brain"], "verbosity_default": "compact",
                 "projections": {v: {"tokens": p["tokens"]} for v, p in g["projections"].items()}}
    return {"room": room_id, "guide": guide, "helper": bool(HELPER),
            "helper_context": _helper_context() if g else None,
            "helper_vision": _helper_vision() if g else None, "add_brain": GUIDE_ADD_BRAIN}, 200


def _trim_guide_history(messages):
    """Newest messages within both GUIDE_HISTORY_* limits, the last (user)
    message always kept, and no leading assistant message -> (kept, dropped)."""
    kept, chars = [], 0
    for m in reversed(messages):
        if kept and (len(kept) >= GUIDE_HISTORY_TURNS or chars + len(m["content"]) > GUIDE_HISTORY_CHARS):
            break
        kept.append(m)
        chars += len(m["content"])
    kept.reverse()
    while kept and kept[0]["role"] != "user":
        kept.pop(0)
    return kept, len(messages) - len(kept)


def _cap_guide_answer(text, cap):
    """Cut an over-cap answer at the last sentence end in the cap's final
    30%, else hard at the cap."""
    window = text[:cap]
    ends = [window.rfind(s) + 1 for s in (". ", "! ", "? ")] + [window.rfind("\n")]
    end = max(ends)
    if end >= int(cap * 0.7):
        return window[:end].rstrip()
    return window


GUIDE_CONTEXT_FIELDS = 24
GUIDE_CONTEXT_VALUE_CHARS = 500


def guide_grounding(pictures, seen=True):
    """The line the server appends to every user turn it forwards: the app
    knows what is attached, so the model never has to trust the user's word.
    seen=False: the user attached them but this helper cannot see pictures,
    so none were sent -- and the helper is told so."""
    if not pictures:
        return "\n\n[No picture is attached to this message.]"
    if not seen:
        return ("\n\n[The user attached %s, but this helper cannot see pictures.]"
                % ("a picture" if pictures == 1 else "%d pictures" % pictures))
    return "\n\n[%d picture%s attached.]" % (pictures, "" if pictures == 1 else "s")


def _guide_context_line(room_id, ctx, with_mode=True):
    """Validate POST /api/guide/chat's optional "context" -> (line or None,
    error sentence or None). The line names the room, the mode in its own
    words, and each field by its label, in the mode's own field order.
    with_mode=False leaves the mode out: a writer is already that mode's own,
    and measured on a 12B brain its label ("A song with words") overrode the
    writer's ask-when-unclear rule (0/3 asked with it, 6/6 without)."""
    if ctx is None:
        return None, None
    if not isinstance(ctx, dict):
        return None, "\"context\" must be an object with a \"mode\" and \"fields\"."
    room = next((r for r in engines.rooms() if r.get("id") == room_id), {})
    mode = ctx.get("mode")
    if not isinstance(mode, str) or len(mode) > 64:
        return None, "The context's \"mode\" must be a mode name of 64 characters or fewer."
    cap = next((m["cap"] for m in room.get("modes") or [] if m.get("mode") == mode), None)
    if cap is None:
        return None, "%s is not a mode of the %s room." % (mode or "That", room.get("name") or room_id)
    fields = ctx.get("fields", {})
    if not isinstance(fields, dict) or len(fields) > GUIDE_CONTEXT_FIELDS:
        return None, ("The context's \"fields\" must be an object of at most %d field values."
                      % GUIDE_CONTEXT_FIELDS)
    known = engines.fields(cap, mode)
    ids = {f.get("id") for f in known}
    for k, v in fields.items():
        if k not in ids:
            return None, "%s is not a field of that mode." % k
        if isinstance(v, str):
            if len(v) > GUIDE_CONTEXT_VALUE_CHARS:
                return None, "The value for %s is too long (at most %d characters)." % (k, GUIDE_CONTEXT_VALUE_CHARS)
        elif not isinstance(v, (int, float, bool)):
            return None, "The value for %s must be text, a number, or true/false." % k
    recipe = ctx.get("recipe")
    preset = None
    if recipe is not None:
        preset = next((x for x in engines.presets(cap, mode) if x.get("id") == recipe), None)
        if preset is None:
            return None, "%s is not a recipe of that mode." % (recipe if isinstance(recipe, str) else "That")
    parts = ["Current room: %s" % (room.get("name") or room_id)]
    if with_mode:
        parts.append("mode: %s (%s)" % (engines.mode_words(cap).get(mode, mode), mode))
    for f in known:
        if f.get("id") not in fields:
            continue
        v = fields[f["id"]]
        if isinstance(v, bool):
            shown = "yes" if v else "no"
        elif isinstance(v, str):
            shown = json.dumps(v, ensure_ascii=False)   # quoted, and a newline stays on one line
        elif isinstance(v, float) and v.is_integer():
            shown = str(int(v))
        else:
            shown = str(v)
        parts.append("%s: %s" % (f.get("label") or f["id"], shown))
    if preset:
        parts.append("recipe: %s" % (preset.get("label") or preset["id"]))
    return "[" + " · ".join(parts) + "]", None


def guide_chat(p):
    """The whole POST /api/guide/chat body -> (body, http code)."""
    if not isinstance(p, dict):
        return {"ok": False, "error": "Send a JSON object."}, 400
    verbosity = p.get("verbosity")
    if verbosity not in guides.VERBOSITIES:
        return {"ok": False, "error": "verbosity must be \"compact\" or \"verbose\"."}, 400
    messages = p.get("messages")
    if not isinstance(messages, list) or not messages:
        return {"ok": False, "error": "Send the conversation as a non-empty \"messages\" list."}, 400
    exists, g = _room_guide(p.get("room"))
    if not g:
        return {"ok": False, "error": ("That room has no guide." if exists else
                                       "There is no room called %s." % (p.get("room") or "that"))}, 404
    # An assistant turn is one of this guide's own earlier answers, so it may
    # run to the guide's largest answer cap; a user turn keeps the helper's
    # usual text limit.
    answer_limit = max(pr["answer_chars"] for pr in g["projections"].values())
    for m in messages:
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
            return {"ok": False, "error": "Each message needs a role of \"user\" or \"assistant\"."}, 400
        limit = HELPER_TEXT_LIMIT if m["role"] == "user" else answer_limit
        if not isinstance(m.get("content"), str) or len(m["content"]) > limit:
            return {"ok": False, "error": "Each message needs text, and that one is too long."}, 400
    if messages[-1]["role"] != "user":
        return {"ok": False, "error": "The last message must be yours."}, 400
    context_line, err = _guide_context_line(p.get("room"), p.get("context"))
    if err:
        return {"ok": False, "error": err}, 400
    refs, err = _guide_picture_refs(p.get("pictures"))
    if err:
        return {"ok": False, "error": err}, 400
    if not HELPER:
        return {"ok": False, "no_brain": True, "error": GUIDE_ADD_BRAIN}, 409
    vision = _helper_vision() if refs else None
    try:
        urls = [_guide_picture_url(x) for x in refs] if vision else []
    except ValueError as e:
        return {"ok": False, "error": str(e)}, 400
    proj = g["projections"][verbosity]
    # What the helper sees, never what the page stores or gets back: every
    # user turn carries its attachment status (pictures only ever ride on the
    # newest one), and the newest one leads with the room's current mode and
    # fields. Added before trimming, so the history budget counts them.
    forwarded = []
    for i, m in enumerate(messages):
        content = m["content"]
        if m["role"] == "user":
            last = i == len(messages) - 1
            if last and context_line:
                content = context_line + "\n\n" + content
            content += guide_grounding(len(refs), vision) if last else guide_grounding(0)
        forwarded.append({"role": m["role"], "content": content})
    kept, dropped = _trim_guide_history(forwarded)
    kept[-1] = dict(kept[-1], content=_with_pictures(kept[-1]["content"], urls))
    try:
        text, finish = _guide_helper_chat([{"role": "system", "content": proj["text"]}] + kept,
                                          max_tokens=proj["max_tokens"])
    except HelperThoughtOnly as e:
        return {"ok": False, "error": str(e)}, 502
    except ValueError as e:
        return {"ok": False, "error": str(e)}, 503
    truncated = finish == "length" or len(text) > proj["answer_chars"]
    if len(text) > proj["answer_chars"]:
        text = _cap_guide_answer(text, proj["answer_chars"])
    body = {"ok": True, "text": text, "truncated": truncated, "dropped": dropped, "verbosity": verbosity}
    if refs:
        body["vision"] = vision
    return body, 200


# ---------------------------------------------------------------------------
# Guide skills: POST /api/guide/skill. A topic in, the mode's field values out,
# written by the pack's own writer (engines/__init__.py "writers") -- the
# prompt, the line keys and the check all live in the pack. The reply is
# checked against the mode's fields and the pack's check; problems get ONE
# automatic retry, and any left are returned with the fields, never hidden.
# ---------------------------------------------------------------------------

GUIDE_SKILL_ANSWER_LIMIT = 500
GUIDE_SKILL_RAW_LIMIT = 2000
GUIDE_SKILL_SHAPE_ERROR = "The writer's answer didn't come back in the expected shape."
_LEADING_NUMBER_RE = re.compile(r"^-?\d+(?:\.\d+)?")


def _writer_fields(cap, mode, w, raw):
    """A parsed writer reply's raw text values -> (field values, problems),
    coerced by each field's declared type, range, options, and the writer's
    own `options`. A value that does not fit is LEFT OUT and named in a plain
    problem sentence -- never clamped or guessed into range."""
    out, problems = {}, []
    allowed = w.get("options") or {}
    for f in engines.fields(cap, mode):
        fid = f["id"]
        if fid not in raw:
            continue
        val, label, ftype = raw[fid], f.get("label") or fid, f["type"]
        kept = "so the form keeps its own value"
        if ftype in ("number", "int"):
            m = _LEADING_NUMBER_RE.match(val.strip())
            num = float(m.group()) if m else None
            if num is None or (ftype == "int" and not num.is_integer()):
                problems.append("%s came back as \"%s\", not a %s, %s." % (
                    label, val, "whole number" if ftype == "int" else "number", kept))
                continue
            lo, hi = (f.get("range") or [None, None])[:2]
            if (lo is not None and num < lo) or (hi is not None and num > hi):
                problems.append("%s came back as %g, outside %g-%g, %s." % (label, num, lo, hi, kept))
                continue
            out[fid] = int(num) if ftype == "int" else num
        elif ftype == "select":
            choice = val.split("/", 1)[0].strip()   # "4/4" is a time signature of 4
            options = [str(o) for o in f.get("options") or []]
            if choice not in options:
                problems.append("%s came back as \"%s\", not one of %s, %s." % (label, val, ", ".join(options), kept))
                continue
            out[fid] = _coerce_field_value(f, choice)
        elif ftype in ("text", "textarea"):
            if fid in allowed:
                match = next((o for o in allowed[fid] if o.lower() == val.strip().lower()), None)
                if match is None:
                    problems.append("%s came back as \"%s\", which this engine does not take, %s." % (label, val, kept))
                    continue
                val = match
            out[fid] = val
    return out, problems


def _writer_check_values(cap, mode, context, written):
    """What the pack's check sees: the fields' defaults, then the room's
    current values the page sent, then what the writer wrote."""
    values = {}
    ctx_fields = (context or {}).get("fields") or {}
    for f in engines.fields(cap, mode):
        for val in (f.get("default"), ctx_fields.get(f["id"])):
            if val is None:
                continue
            try:
                values[f["id"]] = _coerce_field_value(f, val)
            except ValueError:
                pass
    values.update(written)
    return values


# A mode whose pack declares no writer still gets one: the room guide's own
# compact voice, the pack's prompt_guides line for the mode (the engine's words
# live there, never here), the mode's own settings listed from its declared
# fields, and this fixed reply contract. PROMPT fills the mode's first text
# field, the one the page shows as the prompt box.
GENERIC_WRITER_LABEL = "Prompt writer"
GENERIC_WRITER_FIELD_TYPES = ("text", "textarea", "number", "int", "select")
GENERIC_WRITER_RESERVED = ("PROMPT", "QUESTION", "OPTIONS", "NOTE")
GENERIC_WRITER_TASK = """

---
Right now you have one job: write the words for this room's form. The user reviews them before anything is made.

How the words for this mode must be written:
%(guide)s

The form's other settings. Set one only when the request or an answer asks for it; otherwise leave it out and the form keeps its own value:
%(settings)s

Reply in plain text, no Markdown and no JSON, in exactly ONE of these two shapes.

To ask, only when you cannot write it well without knowing one thing:
QUESTION: <one short question>
OPTIONS: <2 to 5 short choices separated by |, or leave this line out>
Ask only what changes the result for this mode: the rules above and the settings list say what matters. If the request or an earlier answer already answers it, don't ask. Never ask filler.

To write:
<SETTING>: <value>, one line for each setting you set, from the list above
NOTE: <only when you chose something the user did not say: name each choice>
PROMPT: <the finished words, ready to paste>
PROMPT comes last. Everything after "PROMPT:" goes into the form as it is, so write nothing after it: no notes, no quotation marks.

Keep what the user asked for: every subject, name and detail they gave stays in.
If the request is about an attached picture and the note at the end of the message says you cannot see pictures, do not guess what it shows: ask the user to describe it in words, with QUESTION."""


def _setting_line(f):
    """One of the mode's settings, as the generic writer is shown it."""
    lo, hi = (f.get("range") or [None, None])[:2]
    if f["type"] in ("number", "int"):
        kind = "a whole number" if f["type"] == "int" else "a number"
        kind += " %g-%g" % (lo, hi) if lo is not None and hi is not None else ""
    elif f["type"] == "select":
        kind = "one of: " + ", ".join(str(o) for o in f.get("options") or [])
    else:
        kind = "text on one line"
    return "%s: %s (%s)" % (f["id"].upper(), f.get("label") or f["id"], kind)


def _generic_writer(cap, mode, g):
    """The writer for a mode with no pack writer, or None when the mode has
    no text field to write into. Same shape as a pack writer, so the same
    parser and the same response serve both."""
    fields = sorted(engines.fields(cap, mode), key=lambda f: f.get("order") or 0)
    text = [f for f in fields if f["type"] in ("text", "textarea")]
    if not text:
        return None
    fills = text[0]["id"]
    others = [f for f in engines.fields(cap, mode) if f["id"] != fills and f["type"] in GENERIC_WRITER_FIELD_TYPES
              and f["id"].upper() not in GENERIC_WRITER_RESERVED]
    keys = {"PROMPT": fills}
    keys.update({f["id"].upper(): f["id"] for f in others})
    task = GENERIC_WRITER_TASK % {"guide": engines.prompt_guide(cap, mode) or GENERIC_PROMPT_GUIDE,
                                  "settings": "\n".join(_setting_line(f) for f in others) or "(none)"}
    return {"label": GENERIC_WRITER_LABEL, "prompt": g["projections"]["compact"]["text"].rstrip() + task,
            "keys": keys, "multiline": "PROMPT", "none_token": "NONE",
            "check": lambda values, request: [], "generic": True}


# "Help me write this" is a short conversation: the writer may ask one
# question per turn, the page sends every answer back in order, and after
# GUIDE_SKILL_MAX_ANSWERS the writer must write, naming its defaults.
GUIDE_SKILL_MAX_ANSWERS = 4
GUIDE_SKILL_WRITE_NOW = ("No more questions: write it now with sensible defaults, "
                         "and add a NOTE line naming each default you chose.")
GUIDE_SKILL_KEPT_ASKING = ("The guide kept asking after %d answers. Start over, or write it yourself."
                           % GUIDE_SKILL_MAX_ANSWERS)


def _skill_answers(answers):
    """Validate "answers": [{"q", "a"}] -> (list, error sentence or None)."""
    if answers is None:
        return [], None
    if not isinstance(answers, list) or len(answers) > GUIDE_SKILL_MAX_ANSWERS:
        return None, ("\"answers\" must be a list of at most %d question and answer pairs."
                      % GUIDE_SKILL_MAX_ANSWERS)
    for x in answers:
        if not (isinstance(x, dict) and isinstance(x.get("q"), str) and isinstance(x.get("a"), str)
                and x["a"].strip() and len(x["q"]) <= GUIDE_SKILL_ANSWER_LIMIT
                and len(x["a"]) <= GUIDE_SKILL_ANSWER_LIMIT):
            return None, ("Each answer must be {\"q\", \"a\"}: the question and a non-empty answer, "
                          "each at most %d characters." % GUIDE_SKILL_ANSWER_LIMIT)
    return answers, None


def _writer_payload(cap, mode):
    """/api/engines' view of a mode's writer, or None: its label, and where
    its words land when that is another mode, the placeholder for a mode
    with no prompt box, and the picture field it writes about."""
    w = engines.writer(cap, mode)
    if not w:
        return None
    out = {"label": w["label"]}
    if w.get("target"):
        tcap, tmode = _writer_target(w, cap, mode)
        out["target"] = _mode_target(tcap, tmode)
    for k in ("topic_label", "pictures"):
        if w.get(k):
            out[k] = w[k]
    return out


def _writer_target(w, cap, mode):
    """-> (cap, mode) a writer's words are for: its `target`, else its own."""
    t = w.get("target") or {}
    return (t["cap"], t["mode"]) if t.get("cap") and t.get("mode") else (cap, mode)


def _mode_target(cap, mode):
    """Where a fix or a written draft lands, for the page: the room that
    holds the mode, and both in their own words."""
    room = next((r for r in engines.rooms() if any(m.get("cap") == cap and m.get("mode") == mode
                                                   for m in r.get("modes") or [])), {})
    return {"cap": cap, "mode": mode, "room": room.get("id"), "room_name": room.get("name"),
            "mode_label": engines.mode_words(cap).get(mode, mode)}


def _guide_attached(w, cap, mode, attached):
    """Validate "attached": the pictures of a writer's own picture field
    (the writer's `pictures`), in the form's order -> (list, error or None).
    Same entry shape as "pictures", up to that field's own "max"."""
    if attached is None:
        return [], None
    if not (w and w.get("pictures")):
        return None, "This writer does not take attached pictures."
    field = next((f for f in engines.fields(cap, mode) if f["id"] == w["pictures"]), {})
    limit = field.get("max") or GUIDE_ATTACHED_MAX
    if not isinstance(attached, list) or len(attached) > limit:
        return None, "\"attached\" must be a list of at most %d pictures." % limit
    if not all(_picture_ref_ok(pic) for pic in attached):
        return None, GUIDE_PICTURE_SHAPE
    return attached, None


GUIDE_ATTACHED_MAX = 16
# A small brain sometimes copies the server's grounding line (guide_grounding)
# after its last key, where it would land in the field as words to render.
_GROUNDING_ECHO_RE = re.compile(r"\n\s*\[?(?:No picture is attached to this message|\d+ pictures? attached"
                                r"|The user attached [^\n]*)\.?[^\n]*\]?\s*$", re.IGNORECASE)


# A storyboard shot's write ("Write this shot") carries the beats either side
# of its own in context.neighbours, one line each, so the shot keeps the
# story's continuity; the writer is told to write only its own shot.
GUIDE_NEIGHBOUR_KEYS = (("before", "Before"), ("after", "After"))


def _skill_neighbours(context, cap, mode):
    """Validate context.neighbours -> (block of text or "", error or None).
    A beat in another engine's format loses that engine's task prefix."""
    n = (context or {}).get("neighbours") if isinstance(context, dict) else None
    if n is None:
        return "", None
    if not isinstance(n, dict) or set(n) - {k for k, _ in GUIDE_NEIGHBOUR_KEYS}:
        return "", "The context's \"neighbours\" must be an object with \"before\" and/or \"after\"."
    lines = []
    for k, word in GUIDE_NEIGHBOUR_KEYS:
        v = n.get(k)
        if v is None:
            continue
        if not isinstance(v, str) or len(v) > GUIDE_CONTEXT_VALUE_CHARS:
            return "", ("The neighbouring beat \"%s\" must be text of at most %d characters."
                        % (k, GUIDE_CONTEXT_VALUE_CHARS))
        v = engines.foreign_prefix_stripped(cap, mode, " ".join(v.split()))
        if v:
            lines.append("%s: %s" % (word, v))
    if not lines:
        return "", None
    return ("The storyboard's shots either side of this one, for continuity only. Write THIS shot, "
            "not them:\n" + "\n".join(lines)), None


def guide_skill(p):
    """The whole POST /api/guide/skill body -> (body, http code). A mode
    with a pack writer uses it; any other mode with a text field uses the
    generic writer, and so does a request with "pictures" ("Describe this
    picture"), where the topic may be empty."""
    if not isinstance(p, dict):
        return {"ok": False, "error": "Send a JSON object."}, 400
    room_id, mode = p.get("room"), p.get("mode")
    exists, g = _room_guide(room_id)
    if not g:
        return {"ok": False, "error": ("That room has no guide." if exists else
                                       "There is no room called %s." % (room_id or "that"))}, 404
    room = next((r for r in engines.rooms() if r.get("id") == room_id), {})
    cap = next((m["cap"] for m in room.get("modes") or [] if m.get("mode") == mode), None)
    if cap is None:
        return {"ok": False, "error": "%s is not a mode of the %s room."
                % (mode if isinstance(mode, str) and mode else "That", room.get("name") or room_id)}, 400
    topic, answer, context = p.get("topic"), p.get("answer"), p.get("context")
    refs, err = _guide_picture_refs(p.get("pictures"))
    if err:
        return {"ok": False, "error": err}, 400
    answers, err = _skill_answers(p.get("answers"))
    if err:
        return {"ok": False, "error": err}, 400
    if refs and topic is None:
        topic = ""
    if not isinstance(topic, str) or not (topic.strip() or refs):
        return {"ok": False, "error": "Say what it should be about first."}, 400
    if len(topic) > HELPER_TEXT_LIMIT:
        return {"ok": False, "error": "That is too long (at most %d characters)." % HELPER_TEXT_LIMIT}, 400
    if answer is not None and (not isinstance(answer, str) or len(answer) > GUIDE_SKILL_ANSWER_LIMIT):
        return {"ok": False, "error": "The answer must be text of at most %d characters."
                % GUIDE_SKILL_ANSWER_LIMIT}, 400
    if isinstance(context, dict) and context.get("mode") != mode:
        return {"ok": False, "error": "The context's \"mode\" must be the mode being written for."}, 400
    context_line, err = _guide_context_line(room_id, context, with_mode=False)
    if err:
        return {"ok": False, "error": err}, 400
    neighbours, err = _skill_neighbours(context, cap, mode)
    if err:
        return {"ok": False, "error": err}, 400
    w = engines.writer(cap, mode)
    attached, err = _guide_attached(w, cap, mode, p.get("attached"))
    if err:
        return {"ok": False, "error": err}, 400
    if refs:
        w = None   # "Describe this picture": the generic writer, whatever the mode's own
    w = w or _generic_writer(cap, mode, g)
    if not w:
        return {"ok": False, "error": "This mode has no writer yet."}, 404
    # A writer's words may be for another mode (a 3D mode's source picture):
    # its keys, fields and check are that mode's.
    wcap, wmode = _writer_target(w, cap, mode)
    if w.get("missing"):
        said = " ".join([topic] + [answer or ""] + [x["a"] for x in answers])
        missing = w["missing"](said, len(attached))
        if missing:
            return {"ok": True, "missing": missing, "retried": False}, 200
    if not HELPER:
        return {"ok": False, "no_brain": True, "error": GUIDE_ADD_BRAIN}, 409
    # A writer that needs an upload first (a cover's source track) says so
    # plainly instead of asking the brain, which cannot supply it.
    ctx_fields = (context or {}).get("fields") or {}
    for fid, sentence in (w.get("needs") or {}).items():
        if not str(ctx_fields.get(fid) or "").strip():
            return {"ok": False, "needs": fid, "error": sentence}, 409
    shown = refs or attached[:_helper_max_images()]
    vision = _helper_vision() if (refs or attached) else None
    try:
        urls = [_guide_picture_url(x) for x in shown] if vision else []
    except ValueError as e:
        return {"ok": False, "error": str(e)}, 400
    user = (context_line + "\n\n" if context_line else "") + (neighbours + "\n\n" if neighbours else "")
    # Words written for another engine (a beat in its format) lose that
    # engine's task prefix before this mode's writer reads them.
    if refs:
        user += "Request: Describe the attached picture, as the words for this mode."
        if topic.strip():
            user += "\nThe user's own words so far: " + engines.foreign_prefix_stripped(cap, mode, topic.strip())
    else:
        user += "Request: " + engines.foreign_prefix_stripped(cap, mode, topic.strip())
    if answer and answer.strip():
        user += "\n\nThe user answered: " + answer.strip()
    if answers:
        user += "\n\nWhat you asked and what the user answered, in order:" + "".join(
            "\nQ%d: %s\nA%d: %s" % (i + 1, x["q"].strip(), i + 1, x["a"].strip()) for i, x in enumerate(answers))
    capped = len(answers) >= GUIDE_SKILL_MAX_ANSWERS
    if capped:
        user += "\n\n" + GUIDE_SKILL_WRITE_NOW
    if attached:
        user += guide_grounding(len(attached), vision)
        if vision and len(urls) < len(attached):
            user = user[:-1] + " You are shown the first %d.]" % len(urls)
    else:
        user += guide_grounding(len(refs), vision)
    request = {"topic": topic, "answer": answer, "answers": answers}
    if w.get("pictures"):
        request["pictures"] = len(attached)

    cut = []   # per ask(), whether its reply was still cut off after the retry

    def ask(text):
        """-> (sent, parsed reply or None, raw reply)."""
        sent = {"system": w["prompt"], "user": text}
        if refs or attached:
            sent["pictures"] = len(urls)
        reply, finish = _guide_helper_chat([{"role": "system", "content": sent["system"]},
                                            {"role": "user", "content": _with_pictures(text, urls)}],
                                           max_tokens=w.get("max_tokens", 1024), retry_cut=True)
        cut.append(finish == "length")
        try:
            parsed = engines.parse_writer_reply(w, reply)
        except ValueError:
            return sent, None, reply
        if "missing" in parsed:
            return sent, parsed, reply
        # An empty prompt is no answer at all; only a pack writer's multiline
        # key (lyrics) may legitimately come back empty.
        if w.get("generic") and "values" in parsed and not parsed["values"].get(w["keys"]["PROMPT"]):
            return sent, None, reply
        return sent, parsed, reply

    def draft(parsed):
        values = dict(parsed["values"])
        multis = w["multiline"] if isinstance(w["multiline"], (list, tuple)) else [w["multiline"]]
        for mkey in (w["keys"][k] for k in multis):
            if isinstance(values.get(mkey), str):
                values[mkey] = _GROUNDING_ECHO_RE.sub("", values[mkey]).rstrip()
        fields, problems = _writer_fields(wcap, wmode, w, values)
        # The room's context is this mode's form; a target mode's check sees only its defaults.
        ctx = context if (wcap, wmode) == (cap, mode) else None
        if w.get("derive"):
            fields.update(w["derive"](_writer_check_values(wcap, wmode, ctx, fields), request))
        return fields, problems + list(w["check"](_writer_check_values(wcap, wmode, ctx, fields), request))

    try:
        sent, parsed, raw = ask(user)
        if parsed is None:
            return {"ok": False, "error": HELPER_CUT_OFF if cut[-1] else GUIDE_SKILL_SHAPE_ERROR,
                    "raw": raw[:GUIDE_SKILL_RAW_LIMIT], "sent": sent}, 502
        if "question" in parsed and capped:
            # The cap is the server's, not the writer's: ask once more to write.
            sent, parsed, raw = ask(user + "\n\nYou already had your answers. " + GUIDE_SKILL_WRITE_NOW)
            if parsed is None or "question" in parsed:
                return {"ok": False, "error": GUIDE_SKILL_SHAPE_ERROR if parsed is None else GUIDE_SKILL_KEPT_ASKING,
                        "raw": raw[:GUIDE_SKILL_RAW_LIMIT], "sent": sent}, 502
        if "missing" in parsed:
            body = {"ok": True, "missing": parsed["missing"], "sent": sent, "retried": False}
            if refs or attached:
                body["vision"] = vision
            return body, 200
        if "question" in parsed:
            body = {"ok": True, "question": parsed["question"], "options": parsed.get("options") or [],
                    "sent": sent, "retried": False}
            if refs or attached:
                body["vision"] = vision
            return body, 200
        fields, problems = draft(parsed)
        was_cut = cut[-1]
        retried = False
        if problems:
            retried = True
            sent2, parsed2, _ = ask(user + "\n\nYour draft had these problems: " + " ".join(problems)
                                    + " Rewrite it.")
            # A retry that asks or breaks shape keeps the first draft, problems and all.
            if parsed2 is not None and "values" in parsed2:
                sent, parsed, (fields, problems) = sent2, parsed2, draft(parsed2)
                was_cut = cut[-1]
    except HelperThoughtOnly as e:
        return {"ok": False, "error": str(e)}, 502
    except ValueError as e:
        return {"ok": False, "error": str(e)}, 503
    if was_cut:
        problems = problems + [HELPER_CUT_OFF]
    body = {"ok": True, "fields": fields, "problems": problems, "sent": sent, "retried": retried,
            "note": parsed.get("note") or ""}
    if (wcap, wmode) != (cap, mode):
        body["target"] = _mode_target(wcap, wmode)
    if refs or attached:
        body["vision"] = vision
    return body, 200


# ---------------------------------------------------------------------------
# Guide revise: POST /api/guide/revise ("Not right? Tell the guide"). A
# finished result, the prompt that made it and the user's complaint in; the
# pack's reviser (engines/__init__.py "revisers") says what went wrong and
# returns a revised prompt or an edit instruction. The picture is sent only
# when the helper can see pictures; otherwise the helper is told it cannot.
# `sent` shows what the brain was asked, never the picture's bytes.
# ---------------------------------------------------------------------------

GUIDE_REVISE_NO_REVISER = "This kind of result has no fixer yet."
REVISE_SETTING_TYPES = ("text", "textarea", "select", "number", "int", "checkbox")


def _revise_settings(cap, mode, job, fills):
    """The job's own recorded values for its mode's fields (the prompt left
    out), each by its label: what the render was actually made with. A
    generic-path job keeps them under "args"."""
    parts = []
    recorded = dict(job.get("args") or {}, **job)
    for f in engines.fields(cap, mode):
        if f["id"] == fills or f["id"] not in recorded or f["type"] not in REVISE_SETTING_TYPES:
            continue
        v = recorded[f["id"]]
        if isinstance(v, bool):
            shown = "yes" if v else "no"
        elif isinstance(v, str):
            shown = json.dumps(v, ensure_ascii=False)
        elif isinstance(v, float) and v.is_integer():
            shown = str(int(v))
        elif isinstance(v, (int, float)):
            shown = str(v)
        else:
            continue
        parts.append("%s: %s" % (f.get("label") or f["id"], shown))
    return " · ".join(parts)


def guide_revise(p):
    """The whole POST /api/guide/revise body -> (body, http code)."""
    if not isinstance(p, dict):
        return {"ok": False, "error": "Send a JSON object."}, 400
    room_id = p.get("room")
    exists, g = _room_guide(room_id)
    if not g:
        return {"ok": False, "error": ("That room has no guide." if exists else
                                       "There is no room called %s." % (room_id or "that"))}, 404
    job_id = p.get("job_id")
    with JOBS_LOCK:
        job = dict(JOBS.get(job_id) or {}) if isinstance(job_id, str) else {}
    if not job:
        return {"ok": False, "error": "I cannot find that result any more."}, 404
    cap, mode = job.get("kind"), job.get("mode")
    room = next((r for r in engines.rooms() if r.get("id") == room_id), {})
    if not any(m.get("cap") == cap and m.get("mode") == mode for m in room.get("modes") or []):
        return {"ok": False, "error": "That result was made in another room."}, 400
    r = engines.reviser(cap, mode)
    if not r:
        return {"ok": False, "error": GUIDE_REVISE_NO_REVISER}, 404
    outs = job.get("outputs") or []
    output = p.get("output")
    if output is None:
        output = next((i for i, o in enumerate(outs) if o.get("media") in ("image", "video")), None)
    elif not isinstance(output, int) or isinstance(output, bool) or not (0 <= output < len(outs)):
        return {"ok": False, "error": "That result has no output number %s." % output}, 400
    if job.get("status") != "done" or output is None:
        return {"ok": False, "error": "That result has no finished picture to look at."}, 400
    complaint, answer = p.get("complaint"), p.get("answer")
    if not isinstance(complaint, str) or not complaint.strip():
        return {"ok": False, "error": "Say what is wrong with it first."}, 400
    if len(complaint) > HELPER_TEXT_LIMIT:
        return {"ok": False, "error": "That is too long (at most %d characters)." % HELPER_TEXT_LIMIT}, 400
    if answer is not None and (not isinstance(answer, str) or len(answer) > GUIDE_SKILL_ANSWER_LIMIT):
        return {"ok": False, "error": "The answer must be text of at most %d characters."
                % GUIDE_SKILL_ANSWER_LIMIT}, 400
    context_line, err = _guide_context_line(room_id, p.get("context"), with_mode=False)
    if err:
        return {"ok": False, "error": err}, 400
    if not HELPER:
        return {"ok": False, "no_brain": True, "error": GUIDE_ADD_BRAIN}, 409
    vision = _helper_vision()
    try:
        urls = [_guide_picture_url({"job_id": job["id"], "output": output})] if vision else []
    except ValueError as e:
        return {"ok": False, "error": str(e)}, 400
    lines = []
    if r.get("fills") or job.get("prompt"):
        lines.append("The prompt that made it: " + (job.get("prompt") or ""))
    settings = _revise_settings(cap, mode, job, r.get("fills"))
    if settings:
        lines.append("The settings it was made with: " + settings)
    lines.append("The user says: " + complaint.strip())
    user = (context_line + "\n\n" if context_line else "") + "\n".join(lines)
    if answer and answer.strip():
        user += "\nThe user answered: " + answer.strip()
    user += guide_grounding(1, vision)
    # The text-only rules go only to a helper that cannot see: one that can
    # copied the "I can't see" opener with the picture in front of it.
    system = r["prompt"] + ("\n\n" + r["blind_note"] if not vision and r.get("blind_note") else "")
    sent = {"system": system, "user_text": user, "pictures": len(urls)}
    try:
        reply, finish = _guide_helper_chat([{"role": "system", "content": system},
                                            {"role": "user", "content": _with_pictures(user, urls)}],
                                           max_tokens=1024, retry_cut=True)
    except HelperThoughtOnly as e:
        return {"ok": False, "error": str(e)}, 502
    except ValueError as e:
        return {"ok": False, "error": str(e)}, 503
    try:
        parsed = engines.parse_reviser_reply(r, reply)
    except ValueError:
        return {"ok": False, "error": HELPER_CUT_OFF if finish == "length" else GUIDE_SKILL_SHAPE_ERROR,
                "raw": reply[:GUIDE_SKILL_RAW_LIMIT], "sent": sent, "vision": vision}, 502
    body = {"ok": True, "vision": vision, "sent": sent}
    if "question" in parsed:
        body["question"] = parsed["question"]
    elif isinstance(r.get("fixes"), dict):
        # A pack's own fix words: where each lands, and what it fills there.
        # (A `fixes` LIST only narrows edit/reroll: the plain branch below.)
        spec = r["fixes"][parsed["fix"]]
        body.update(parsed)
        if spec.get("fills") or spec.get("settings"):
            t = spec.get("target") or {"cap": cap, "mode": mode}
            body["target"] = _mode_target(t["cap"], t["mode"])
        if spec.get("fills"):
            body["fills"] = spec["fills"]
        if spec.get("settings"):
            fields, problems = _revise_setting_fields(body["target"]["cap"], body["target"]["mode"],
                                                      parsed.pop("settings"))
            body.pop("settings", None)
            if not fields:
                return {"ok": False, "error": GUIDE_SKILL_SHAPE_ERROR, "raw": reply[:GUIDE_SKILL_RAW_LIMIT],
                        "sent": sent, "vision": vision}, 502
            body.update(fields=fields, problems=problems)
    else:
        body.update(parsed, fills=r["fills"], edit_mode=r.get("edit_mode"))
    if finish == "length" and "question" not in body:
        body["problems"] = list(body.get("problems") or []) + [HELPER_CUT_OFF]
    return body, 200


def _revise_setting_fields(cap, mode, raw):
    """A reviser's SETTINGS {id or label: raw text} -> (field values,
    problems), matched to the mode's fields by id or label (any case) and
    coerced like a writer's values: a value that does not fit is left out."""
    by_name = {}
    for f in engines.fields(cap, mode):
        by_name[f["id"].lower()] = f["id"]
        by_name[(f.get("label") or f["id"]).lower()] = f["id"]
    matched = {by_name[k]: v for k, v in raw.items() if k in by_name}
    return _writer_fields(cap, mode, {}, matched)


# ---------------------------------------------------------------------------
# generate(): the whole POST /api/generate body (§7's "Generate refactor").
# api_generate becomes send_json(*generate(read_json())); the sequence
# endpoint builds its own `p` from a slot and calls this SAME function, so
# there is exactly one validation path for a render.
# ---------------------------------------------------------------------------

def _seq_meta_extra(p):
    """§7: job meta gains `sequence_id`, `slot_id` and `recipe` (preset id) --
    but ONLY when the caller supplied them. A plain /api/generate body never
    carries these, so its job dict is untouched by this and generate() stays
    byte-identical to api_generate's old behaviour for every request that
    predates C3.2b."""
    extra = {}
    for k in ("sequence_id", "slot_id", "recipe"):
        if p.get(k) is not None:
            extra[k] = p[k]
    return extra


def _field_label(cap, mode, fid, fallback=None):
    """E1: a declared field's plain-English label, for a coercion error that
    names the field rather than repeating Python's own exception text.
    Falls back to `fallback` (or the bare field id) for a core-level field
    no pack declares, e.g. "seed"."""
    for f in engines.fields(cap, mode):
        if f["id"] == fid:
            return f.get("label") or fallback or fid
    return fallback or fid


def _make_time_problems(p, lane, able, kind, mode):
    """The owning pack's writer check on this request's values -> problem
    sentences, or []. Only for a mode this lane can actually run right now;
    any other refusal (not installed, wrong lane) is left to the dispatch
    path's own sentence, and so is a value that does not coerce: a request
    that cannot render anyway is never asked to be confirmed first."""
    w = engines.writer(kind, mode) if mode in engines.modes_for(kind) else None
    if not w or not w.get("check") or w.get("make_time") is False or kind not in lane["caps"] \
            or not able.get(engines.mode_ability(kind, mode)):
        return []
    q, qerr = apply_quality(p, kind, mode, able)
    if qerr:
        return []
    req_values = q.get("values") if isinstance(q.get("values"), dict) else {}
    values = {}
    for f in engines.fields(kind, mode):
        val = _field_request_value(q, req_values, f["id"])
        if val is None or (val == "" and f["type"] not in ("text", "textarea")):
            val = f.get("default")
        if val is None:
            continue
        try:
            values[f["id"]] = _coerce_field_value(f, val)
        except ValueError:
            return []
    return list(w["check"](values, p))


def generate(p):
    """Returns (body, http code). Extracted verbatim from the old
    api_generate method; every `self.send_json(X[, code])` became
    `return X, code` (code defaulting to 200), and its
    `self._dispatch_generic(...)` became the module-level call below."""
    lane = LANE_BY_ID.get(p.get("lane")) if isinstance(p.get("lane"), str) else None
    if not lane:
        return {"ok": False, "error": "Pick a lane first."}, 400
    with STATE_LOCK:
        if not LANE_STATE.get(lane["id"], {}).get("up"):
            return {"ok": False, "error": "%s is offline right now. Pick one of the lanes glowing green." % lane["name"]}, 409
    kind = p.get("kind")
    mode = p.get("mode")
    m = models_for(lane)
    able = abilities(lane)
    # P2: a mode whose pack declares a writer check never starts a render
    # the check says will not be what was asked for (lyrics with no voice
    # render as an instrumental) until the caller confirms it.
    if p.get("confirm") is not True:
        problems = _make_time_problems(p, lane, able, kind, mode)
        if problems:
            return {"ok": False, "needs_confirm": True, "problems": problems,
                    "error": "Check this before it renders: " + " ".join(problems)}, 409

    # D1: only the pack that OWNS a (cap, mode) gets to say whether the
    # core's hand-tuned image/video logic below (cfg defaults, frame-grid
    # snapping, turbo derivation) applies to it. Everything else -- audio,
    # cutout/upscale/mesh, and any future pack -- takes the single
    # generic field-driven path, so a new mode can never be silently
    # misrouted into another pack's graph (the old fl2va-catches-anything
    # "else" branch). A mode the core does not recognise for this cap
    # still falls through to legacy handling exactly as before ONLY if
    # this cap actually HAS a legacy pack (image/video today) -- that is
    # the pre-existing "no mode given" default, preserved verbatim.
    owned = mode in engines.modes_for(kind)
    is_legacy = engines.legacy_dispatch(kind, mode) if owned else engines.has_legacy(kind)
    if not is_legacy:
        return _dispatch_generic(lane, m, able, p, kind, mode)

    # R3: resolve the quality tier on the CANONICAL mode key ("t2i"/
    # "edit"/"fl2va"/"ref2v"), the one the pack's ladder and JOBS
    # bookkeeping (R5/R6) both use -- not the raw kind=="video" "else"
    # fallback, which is the text-to-video path and still runs the
    # fl2va graph.
    if kind == "image":
        qmode = "edit" if mode == "edit" else "t2i"
    else:  # kind == "video" -- the only other cap with a legacy pack
        qmode = mode if mode in ("ref2v", "continue") else "fl2va"
    p, qerr = apply_quality(p, kind, qmode, able)
    if qerr:
        return {"ok": False, "error": qerr}, 400

    prompt = (p.get("prompt") or "").strip()
    if not prompt:
        return {"ok": False, "error": "Tell it what you want first."}, 400
    try:
        seed = int(p.get("seed") or 0) or random.randint(1, 2 ** 48)
    except ValueError:
        return {"ok": False, "error": "%s needs a whole number." % _field_label(kind, qmode, "seed", "Seed")}, 400
    try:
        steps = max(1, min(80, int(p.get("steps") or 20)))
    except ValueError:
        return {"ok": False, "error": "%s needs a whole number." % _field_label(kind, qmode, "steps", "Steps")}, 400
    return _generate_legacy(p, lane, m, able, kind, mode, qmode, prompt, seed, steps)


def _generate_legacy(p, lane, m, able, kind, mode, qmode, prompt, seed, steps):
    """The image/video halves of the old api_generate body, unchanged except
    `self.send_json(X[, code])` -> `return X, code`."""
    if kind == "image":
        if "image" not in lane["caps"]:
            return {"ok": False, "error": "%s is not set up as a picture lane. %s"
                                   % (lane["name"], suggest_lanes("image"))}, 400
        # R3-1: the picture generator's ability is now MODE-specific
        # (t2i/edit), not the cap name "image" itself -- a lane whose
        # only "image"-cap files belong to a tool pack (cleanup) must
        # not report Picture as available and then die on a KeyError.
        ability = engines.mode_ability("image", qmode)
        if not able.get(ability):
            return {"ok": False, "error":
                                   "%s does not have the picture models installed (missing %s). %s"
                                   % (lane["name"], join_words(missing_for(lane, ability)),
                                      suggest_lanes("image"))}, 400
        # MEASURED 2026-09-22: at cfg 1 there is no classifier-free guidance,
        # so the UI's "THINGS TO AVOID" box is INERT -- same prompt and seed
        # with and without a negative gave max pixel delta 0, correlation
        # +1.000000. Above 1 it bites. Lowering this default to 1 would make
        # a visible input field a lie, so the default stays whatever the
        # field itself declares (Q21 rule 1: never a literal here).
        # NOTE: our sprite pipeline's positive-only rule was measured on the cfg-1
        # path where negatives are inert. It does NOT bind here.
        cfg_field = next((f for f in engines.fields("image", qmode) if f["id"] == "cfg"), None)
        cfg_default = cfg_field.get("default", 2.5) if cfg_field else 2.5
        try:
            cfg = float(p.get("cfg") or cfg_default)
        except ValueError:
            return {"ok": False, "error": "%s needs a number." % _field_label("image", qmode, "cfg", "Guidance strength")}, 400
        try:
            w = int(p.get("width") or 1328)
        except ValueError:
            return {"ok": False, "error": "%s needs a whole number." % _field_label("image", qmode, "width", "Width")}, 400
        try:
            h = int(p.get("height") or 1328)
        except ValueError:
            return {"ok": False, "error": "%s needs a whole number." % _field_label("image", qmode, "height", "Height")}, 400
        w, h = (w // 16) * 16, (h // 16) * 16
        try:
            resolution = int(p.get("resolution", 1024))
        except (TypeError, ValueError):
            return {"ok": False, "error":
                    "%s needs a whole number." % _field_label("image", qmode, "resolution", "Encoder resolution")}, 400
        args = {"prompt": prompt, "negative": p.get("negative", ""), "width": w, "height": h,
                "steps": steps, "cfg": cfg, "seed": seed,
                "ref_images": p.get("ref_images") or [], "resolution": resolution}
        # Q21 rule 1: the legacy image path passes only the fixed args above
        # to the pack, so any field a pack declares beyond those is silently
        # dropped -- fill in every declared field not already in args, via
        # the exact same request-value lookup + type coercion the generic
        # dispatch path uses (never a second copy of that logic). Engine
        # independence stays 0: no field id or mode is named here.
        req_values = p.get("values")
        req_values = req_values if isinstance(req_values, dict) else {}
        try:
            for f in engines.fields("image", qmode):
                fid, ftype = f["id"], f["type"]
                if fid in args:
                    continue
                val = _field_request_value(p, req_values, fid)
                if ftype == "select" and val == "":
                    val = None
                if ftype in ("audio", "image", "image_list", "video_list", "model") and (
                        (isinstance(val, str) and not val.strip()) or val == []):
                    val = None
                if val is None:
                    continue
                args[fid] = _coerce_field_value(f, val)
        except ValueError as e:
            return {"ok": False, "error": str(e)}, 400
        try:
            graph = engines.graph_for("image", "edit" if mode == "edit" else "t2i", args, m)
        except ValueError as e:
            return {"ok": False, "error": str(e)}, 400
        meta = {"prompt": prompt, "negative": p.get("negative", ""), "seed": seed, "steps": steps,
                "cfg": cfg, "width": w, "height": h, "model": describe_image(lane),
                "model_file": m.get(engines.primary_role("image", "edit" if mode == "edit" else "t2i"), ""),
                "refs": len(args["ref_images"]), "quality": p.get("quality")}
        meta.update(_seq_meta_extra(p))
        return dispatch(lane, graph, "image", qmode, meta), 200

    if kind == "video":
        return _generate_video(p, lane, m, able, mode, qmode, prompt, seed, steps)

    return {"ok": False, "error": "unknown kind %r" % kind}, 400


def _generate_video(p, lane, m, able, mode, qmode, prompt, seed, steps):
    """The video branch of the old api_generate body, unchanged except
    `self.send_json(X[, code])` -> `return X, code`."""
    if "video" not in lane["caps"]:
        return {"ok": False, "error": "%s is not set up as a video lane. %s"
                               % (lane["name"], suggest_lanes("video"))}, 400
    if not able["video"]:
        return {"ok": False, "error":
                               "%s does not have the video models installed (missing %s). %s"
                               % (lane["name"], join_words(missing_for(lane, "video")),
                                  suggest_lanes("video"))}, 400
    try:
        w = int(p.get("width") or 960)
    except ValueError:
        return {"ok": False, "error": "%s needs a whole number." % _field_label("video", qmode, "width", "Width")}, 400
    try:
        h = int(p.get("height") or 544)
    except ValueError:
        return {"ok": False, "error": "%s needs a whole number." % _field_label("video", qmode, "height", "Height")}, 400
    w, h = (w // 32) * 32, (h // 32) * 32
    try:
        length = snap_frames(p.get("length") or 362)
    except ValueError:
        return {"ok": False, "error": "%s needs a whole number." % _field_label("video", qmode, "length", "Length")}, 400
    # The turbo LoRA is a 4-step distillation: attach it for the fast rows (<= 8 steps),
    # never above that, where it fights the schedule and smooths detail away.
    turbo = bool(p.get("turbo_lora")) if p.get("turbo_lora") is not None else (steps <= 8)
    if turbo and not able["turbo"]:
        # No speed pack on this box. Silently running a 4-step schedule
        # without its LoRA produces mush, so refuse rather than disappoint.
        return {"ok": False, "error":
                               "%s has no speed pack installed, so the quick settings would come out "
                               "mushy. Pick Middle, Good or Best, or install a turbo LoRA "
                               "on that machine." % lane["name"]}, 400
    args = {"prompt": prompt, "width": w, "height": h, "length": length, "steps": steps,
            "turbo_lora": turbo,
            "seed": seed, "encoder": p.get("encoder") or None,
            "first_frame": p.get("first_frame"), "last_frame": p.get("last_frame"),
            "ref_images": p.get("ref_images") or [], "ref_videos": p.get("ref_videos") or [],
            "keep_audio": bool(p.get("keep_audio", True)),
            "ref_image_size": p.get("ref_image_size", "match"),
            "prev_video": p.get("prev_video")}
    if mode == "ref2v":
        if not able["ref2v"]:
            return {"ok": False, "error":
                                   "%s does not have the H3 reference model installed (missing %s), so it "
                                   "cannot copy people or clips. It can still do the other video modes."
                                   % (lane["name"], join_words(missing_for(lane, "ref2v")))}, 400
        if not args["ref_images"] and not args["ref_videos"]:
            return {"ok": False, "error":
                                   "Add at least one picture or clip for it to work from."}, 400
        graph = engines.graph_for("video", "ref2v", args, m)
        model = describe_video(lane) + " reference"
    elif mode == "fl2va":
        if not able["fl2va"]:
            return {"ok": False, "error":
                                   "%s does not have the first-frame H3 model installed (missing %s)."
                                   % (lane["name"], join_words(missing_for(lane, "video")))}, 400
        if not args["first_frame"] and not args["last_frame"]:
            return {"ok": False, "error":
                                   "Add a starting picture (an ending picture is optional)."}, 400
        graph = engines.graph_for("video", "fl2va", args, m)
        model = describe_video(lane) + " first frame"
    elif mode == "continue":
        if not able["continue"]:
            return {"ok": False, "error":
                                   "%s does not have the H3 video model installed (missing %s)."
                                   % (lane["name"], join_words(missing_for(lane, "continue")))}, 400
        # `prev_video` is optional here, same as fl2va's own first_frame/
        # last_frame -- a cabled-but-unresolvable source already refused
        # earlier, in resolve_slot_cables (K3); a shot with no cable at all
        # (the first of a chain) just renders without it.
        graph = engines.graph_for("video", "continue", args, m)
        model = describe_video(lane) + " continue"
    else:
        if not able["fl2va"]:
            return {"ok": False, "error":
                                   "%s does not have the H3 text-to-video model installed (missing %s)."
                                   % (lane["name"], join_words(missing_for(lane, "video")))}, 400
        args["first_frame"] = args["last_frame"] = None
        graph = engines.graph_for("video", "fl2va", args, m)
        model = describe_video(lane) + " text-to-video"
    meta = {"prompt": prompt, "seed": seed, "steps": steps, "turbo_lora": turbo, "width": w, "height": h,
            "length": length, "seconds": round(length / 24.0, 2), "model": model,
            "model_file": m.get(engines.primary_role("video", qmode), ""),
            "refs": len(args["ref_images"]), "ref_videos": len(args["ref_videos"]),
            "chained_from": p.get("chained_from"), "quality": p.get("quality")}
    meta.update(_seq_meta_extra(p))
    return dispatch(lane, graph, "video", qmode, meta), 200


# E2: request-body keys `generate()`/`_dispatch_generic()` read at the top
# level of `p` BEFORE any pack's own declared fields are looked at --
# `generate()` (lane, kind, mode), `apply_quality()` (quality),
# `_dispatch_generic()`'s seed handling (seed) and `_seq_meta_extra()`
# (sequence_id, slot_id, recipe). A pack field sharing one of these ids
# (yue2/cover's own "mode" select) would otherwise collide with the
# dispatch key of the same name the moment it landed at the top level of
# the request body -- verified against every top-level `p.get("...")` /
# `p["..."]` read in generate()/_dispatch_generic()/apply_quality()/
# _seq_meta_extra() above. Such a field's value travels ONLY under
# `p["values"][id]`, never at the top level.
RESERVED_FIELD_IDS = {"lane", "kind", "mode", "quality", "recipe", "seed", "sequence_id", "slot_id"}


def _field_request_value(p, values, fid):
    """E2: a declared field's incoming value -- `p["values"][id]` when
    present, else `p[id]` as before C3.2b/E2, except a field whose id
    collides with a dispatch-level key (RESERVED_FIELD_IDS), which is read
    ONLY from `p["values"]` -- the top-level key of that name is always the
    dispatch key, never this field's value."""
    if fid in RESERVED_FIELD_IDS:
        return values.get(fid)
    if fid in values:
        return values[fid]
    return p.get(fid)


# Q21 rule 1: the coercion a declared field's incoming value goes through,
# by its declared type -- factored out of _dispatch_generic's field loop so
# _generate_legacy's own generic-field fallback below can reuse the exact
# same rules (never duplicate the select/number/int/checkbox logic). Raises
# ValueError with an already-plain-English message on a bad value.
def _coerce_field_value(f, val):
    fid, ftype = f["id"], f["type"]
    label = f.get("label", fid)
    if ftype == "number":
        try:
            return float(val)
        except (TypeError, ValueError):
            raise ValueError("%s needs a number." % label)
    if ftype == "int":
        try:
            return int(val)
        except (TypeError, ValueError):
            raise ValueError("%s needs a whole number." % label)
    if ftype == "checkbox":
        return bool(val)
    if ftype == "select":
        # E2: a select's value must be one of its declared options,
        # compared as the OPTION's own type (turntable's "size" is a
        # select of ints) -- never passed through unchecked into the
        # graph the way every other pack-declared type already was.
        options = f.get("options") or []
        opt_type = type(options[0]) if options else str
        try:
            coerced = val if isinstance(val, opt_type) else opt_type(val)
        except (TypeError, ValueError):
            coerced = val
        if options and coerced not in options:
            raise ValueError("%s must be one of %s." % (label, ", ".join(str(o) for o in options)))
        return coerced
    return val


def _dispatch_generic(lane, m, able, p, kind, mode):
    """The default path for any (cap, mode) not marked legacy_dispatch by
    its owning pack -- generalised from what the audio branch already did
    (R1/R3): args are built purely from the pack's own DECLARED fields,
    coerced by their declared type. The core never hardcodes a mode name
    here. An unknown (cap, mode) refuses cleanly, never falls back to
    another mode.

    RESERVED_FIELD_IDS / _field_request_value (E2): some modes declare a
    field of their own named "mode" (yue2/cover's full-vs-melody), which
    would otherwise collide with the REQUEST's own "mode" (the dispatch
    key generate() reads to pick this pack/mode in the first place) the
    moment a flat request body carried both under the same key. Such a
    field's value travels under `p["values"][id]` instead; every other
    declared field keeps working exactly as before, `p["values"]` or not.

    Was a Handler method (self._dispatch_generic); every
    `self.send_json(X[, code])` became `return X, code`.
    """
    if mode not in engines.modes_for(kind):
        return {"ok": False, "error": "Unknown kind %r / mode %r." % (kind, mode)}, 400
    p, qerr = apply_quality(p, kind, mode, able)
    if qerr:
        return {"ok": False, "error": qerr}, 400
    if kind not in lane["caps"]:
        return {"ok": False, "error": "%s is not set up as a %s lane. %s"
                               % (lane["name"], engines.cap_word(kind), suggest_lanes(kind))}, 400
    ability = engines.mode_ability(kind, mode)
    if not able.get(ability):
        # Right after start, discovery may not have read this lane's model list
        # yet: "not installed" would be a false answer then, so say so plainly.
        with DISCOVERY_LOCK:
            checked = (DISCOVERY.get(lane["id"]) or {}).get("checked")
        if not checked:
            with STATE_LOCK:
                up = LANE_STATE.get(lane["id"], {}).get("up")
            return {"ok": False, "error": (
                "Still checking what %s has installed. Try again in a few seconds." if up else
                "%s is not answering, so it cannot tell yet what it has installed. "
                "Check that ComfyUI is running there.") % lane["name"]}, 503
        return {"ok": False, "error":
                               "%s does not have that %s mode's models installed (missing %s). %s"
                               % (lane["name"], engines.cap_word(kind), join_words(missing_for(lane, mode)),
                                  suggest_lanes(kind))}, 400
    try:
        seed = int(p.get("seed") or 0) or random.randint(1, 2 ** 48)
    except ValueError:
        return {"ok": False, "error": "%s needs a whole number." % _field_label(kind, mode, "seed", "Seed")}, 400
    args = {"seed": seed}
    # "master" (the optional mastering chain toggle) is deliberately NOT a
    # declared field -- every audio graph accepts it identically, so
    # declaring it per-mode would be five duplicate declarations that
    # could drift (see engines/audio.py's ENGINE comment). Passed through
    # generically here; a graph function that does not read it ignores it.
    if "master" in p:
        args["master"] = bool(p["master"])
    # The job's headline "prompt" is positional-first-wins: whichever
    # declared text/textarea field comes first in the pack's field list
    # for this mode. For audio's "cover" that is `style`, not `lyrics` --
    # a deliberate simplification (no per-mode "primary field" declared),
    # not a claim that style is always the most representative text.
    # B1: coercion (a malformed "" for a number/int field) and the pack's
    # OWN validation (e.g. LTX's context_overlap/context_length check) are
    # both a plain ValueError -- a pack's business rule is not different
    # from a bad request here, and neither should ever reach the client
    # as an unhandled exception. One try around both, same contract as
    # the legacy image branch above (ValueError -> 400 with its message).
    prompt_text = ""
    upload_title = ""
    req_values = p.get("values")
    req_values = req_values if isinstance(req_values, dict) else {}
    try:
        for f in engines.fields(kind, mode):
            fid, ftype = f["id"], f["type"]
            val = _field_request_value(p, req_values, fid)
            # E2: absent/empty on a select is "use the field's default",
            # the same as absent on any other type (val is None below) --
            # never a coercion attempt on "".
            if ftype == "select" and val == "":
                val = None
            # E3: empty on a file-typed field ("" for a single upload, "" or
            # [] for a list of them) is "not given", the same as absent --
            # never a value the pack's graph builder should see (LTX's
            # end_image check is `is not None`, not truthiness).
            if ftype in ("audio", "image", "image_list", "video_list", "model") and (
                    (isinstance(val, str) and not val.strip()) or val == []):
                val = None
            if val is None:
                continue
            args[fid] = _coerce_field_value(f, val)
            if ftype in ("text", "textarea") and not prompt_text:
                prompt_text = str(val)
            # First file-typed field becomes the display title for a job
            # that has no text prompt (e.g. a turntable of an uploaded
            # .glb would otherwise show as its bare kind). Stored upload
            # names carry an 8-hex uniqueness prefix (see api_upload's
            # process branch), so strip it for display.
            if ftype in ("audio", "image", "image_list", "video_list", "model") and not upload_title:
                item = val if isinstance(val, str) else (val[0] if val else "")
                item = str(item or "")
                if item:
                    upload_title = re.sub(r"^[0-9a-f]{8}_", "", os.path.basename(item))
        graph = engines.graph_for(kind, mode, args, m)
    except KeyError as e:
        # A pack's graph builder reached into args/models for a key that was
        # never there. If the key is a declared field id, the field simply
        # was not sent (the field-loop above only skips a field on val is
        # None); name it in plain words the same way rule 1's coercion
        # messages do. Otherwise this is the pack's own bug -- honest about
        # that, never a bare quoted key (rule 2).
        key = e.args[0] if e.args else str(e)
        fld = next((f for f in engines.fields(kind, mode) if f["id"] == key), None)
        if fld:
            return {"ok": False, "error": "%s is needed for this." % fld.get("label", key)}, 400
        return {"ok": False, "error":
                               "This engine is missing a setting it needs (%s). "
                               "This is a bug in the engine pack." % key}, 400
    except (ValueError, LookupError) as e:
        # Either our own plain coercion message from the loop above, or a
        # pack's OWN authored ValueError sentence (e.g. LTX's window check,
        # rule 3) -- both already plain English, never Python's own text.
        return {"ok": False, "error": str(e)}, 400
    if lane_kind(lane) == "process":
        # The run plan is the "prompt": runner.py executes it on this box.
        # File-typed fields carry uploaded filenames; they are staging
        # instructions, kept out of the job record's args on purpose.
        meta = {"prompt": prompt_text, "seed": seed, "steps": 0,
                "model": "", "args": {k: v for k, v in args.items() if k != "seed"},
                "quality": p.get("quality")}
        if not prompt_text and upload_title:
            meta["title"] = upload_title
        meta.update(_seq_meta_extra(p))
        return dispatch_process(lane, graph, kind, mode, meta, args), 200
    role = engines.primary_role(kind, mode)
    meta = {"prompt": prompt_text, "seed": seed, "steps": 0,
            "model": engines.describe(m, kind), "model_file": m.get(role, ""),
            "args": {k: v for k, v in args.items() if k != "seed"}, "quality": p.get("quality")}
    if not prompt_text and upload_title:
        meta["title"] = upload_title
    meta.update(_seq_meta_extra(p))
    return dispatch(lane, graph, kind, mode, meta), 200


# ---------------------------------------------------------------------------
# The sequence generate resolver (§2, §7): builds `p` from a slot, resolves
# REF ROOM references into the mode's own image_list field, calls generate()
# above, then records the take.
# ---------------------------------------------------------------------------

def slot_generate_lane(slot):
    """§7's `lane` for a slot generate: "the slot's lane choice, else the
    first up lane whose caps include the slot's cap." The SEQUENCE schema
    (§1) has no execution-lane field of its own on a slot -- its `lane` key
    is the picture/video/sound TIMELINE track -- so "the slot's lane choice"
    is read as sticky-to-its-last-take: the execution lane the slot's most
    recent take rendered on, reused while it is up, so a shot does not hop
    machines between takes for no reason. See LOW-CONFIDENCE RULINGS."""
    takes = slot.get("takes") or []
    if takes:
        with JOBS_LOCK:
            job = JOBS.get(takes[-1].get("job_id"))
        lane = LANE_BY_ID.get(job.get("lane")) if job else None
        if lane is not None:
            with STATE_LOCK:
                up = bool(LANE_STATE.get(lane["id"], {}).get("up"))
            if up:
                return lane
    cap = slot.get("cap")
    with STATE_LOCK:
        up_ids = {lid for lid, st in LANE_STATE.items() if st.get("up")}
    for lane in LANES:
        if lane["id"] in up_ids and cap in lane.get("caps", []):
            return lane
    return None


def resolve_slot_refs(seq, slot, target_lane):
    """§2/§3's resolver. slot.refs=="auto" and the mode declares an
    image_list field -> every ref of the sequence goes into it (set first,
    the room's own stored order), each carried UNCROPPED from its cache
    (fit=None, cache_path=the ref's own file -- so a down source lane never
    blocks this). More refs than the field's declared `max` -> ValueError
    (never truncated). "off", or no image_list field -> nothing passed.

    Returns (field_values: {field id: [name, ...]}, ref_uses:
    ["<ref id>:<job id>", ...]) for the take's inputs.refs record."""
    if not slot_sees_refs(slot):
        return {}, []
    field = _slot_image_list_field(slot.get("cap"), slot.get("mode"))
    refs = seq.get("refs") or []
    if not refs:
        return {}, []
    cap_max = field.get("max")
    if isinstance(cap_max, int) and len(refs) > cap_max:
        raise ValueError("The room holds %d references; this recipe takes %d. Turn one off for this shot."
                          % (len(refs), cap_max))
    names, uses = [], []
    for ref in refs:
        with JOBS_LOCK:
            job = copy.deepcopy(JOBS.get(ref.get("job_id")))
        if job is None:
            raise ValueError("A reference in the room no longer has its source result.")
        name, _note = carry(job, ref.get("output", 0), target_lane, fit=None,
                             cache_path=seq_ref_path(seq["id"], ref["id"]))
        names.append(name)
        uses.append("%s:%s" % (ref["id"], job.get("id")))
    return {field["id"]: names}, uses


def _seq_take_cache_path(sid, rel_file):
    """Realpath-contained in data/seq/<id>/, same guard as seq_ref_path --
    `rel_file` is take["file"], which server.py itself writes (seq_harvest),
    but the sequence JSON it comes from is on-disk, untrusted input (a hand-
    edited or damaged file could name anything). Unlike seq_ref_path this is
    a soft cache lookup, not a refusal: an escaping path just means "no
    local cache", so this returns None rather than raising, and the caller
    falls back to fetching from the source lane exactly as if the take had
    never been harvested."""
    base = os.path.realpath(os.path.join(SEQ_MEDIA_DIR, sid))
    path = os.path.realpath(os.path.join(base, rel_file or ""))
    if path != base and not path.startswith(base + os.sep):
        return None
    return path


def resolve_slot_cables(seq, slot, target_lane):
    """§4/K3's resolver: every cable feeding this slot. An IMAGE jack takes
    `from`'s current pick and carries it cropped to the sequence canvas
    (using the source take's own harvested local file when one exists, so
    the source lane can be off); an empty source refuses "<jack label> is
    not made yet.". A VIDEO jack (K3) instead reuses the cut's own harvest
    retry (_cut_ensure_take_file) to get the source's take onto local disk,
    then carries those bytes over UNCROPPED and UNCHANGED (fit=None -- no
    crop, no re-encode, per K3); an unpicked source refuses "The shot this
    continues from is not made yet.", and a source that cannot be copied at
    all refuses with _cut_ensure_take_file's own sentence, naming the
    source's own slot.

    Returns (field_values: {field id: name}, cable_uses: {field id: source
    job id}) for the take's inputs.cables record."""
    field_values, uses = {}, {}
    cables = [c for c in seq.get("cables") or [] if c.get("to") == slot.get("id")]
    if not cables:
        return field_values, uses
    canvas = seq.get("canvas") or {}
    fit = (canvas.get("width"), canvas.get("height"))
    jacks_by_field = {j["field"]: j for j in slot_jacks(slot)}
    slots_by_id = {s["id"]: s for s in seq.get("slots") or []}
    for cable in cables:
        field_id = cable.get("field")
        jack = jacks_by_field.get(field_id, {})
        from_slot = slots_by_id.get(cable.get("from"))
        pick = from_slot.get("pick") if from_slot else None
        if jack.get("type") == "video":
            if not pick:
                raise ValueError("The shot this continues from is not made yet.")
            path = _cut_ensure_take_file(seq["id"], from_slot["id"], pick)
            with JOBS_LOCK:
                job = copy.deepcopy(JOBS.get(pick))
            name, _note = carry(job, 0, target_lane, fit=None, cache_path=path)
            field_values[field_id] = name
            uses[field_id] = pick
            continue
        label = jack.get("label", field_id)
        if not pick:
            raise ValueError("%s is not made yet." % label)
        with JOBS_LOCK:
            job = copy.deepcopy(JOBS.get(pick))
        if job is None:
            raise ValueError("%s is not made yet." % label)
        take = next((t for t in from_slot.get("takes") or [] if t.get("job_id") == pick and t.get("file")), None)
        cache_path = _seq_take_cache_path(seq["id"], take["file"]) if take else None
        name, _note = carry(job, 0, target_lane, fit=fit, cache_path=cache_path)
        field_values[field_id] = name
        uses[field_id] = pick
    return field_values, uses


def seq_generate(payload):
    """POST /api/sequence/generate {id, slot_id} (§7). Builds `p` from the
    slot, resolves references, calls generate(p), then appends a take
    (seq_add_take() bumps rev and pins the new job id itself)."""
    if not isinstance(payload, dict):
        return {"ok": False, "error": "Send a JSON object."}, 400
    sid, slot_id = payload.get("id"), payload.get("slot_id")
    if not seq_valid_id(sid):
        return {"ok": False, "error": "That is not a sequence id."}, 400
    with SEQ_LOCK:
        seq = _seq_read(sid)
    if seq is None:
        return {"ok": False, "error": "There is no such sequence."}, 404
    try:
        slot = _slot(seq, {"slot_id": slot_id})
    except ValueError as e:
        return {"ok": False, "error": str(e)}, 400
    lane = slot_generate_lane(slot)
    if lane is None:
        return {"ok": False, "error": "No lane that can make a %s shot is online right now."
                               % engines.cap_word(slot.get("cap"))}, 409
    slot_values = dict(slot.get("values") or {})
    # E2: kept flat (as before) so the legacy image/video packs -- which
    # read their fields straight off the top level of `p`, never off
    # `p["values"]` -- keep working from a slot exactly as they did before
    # this slice; ALSO mirrored under "values" so a slot field whose id
    # collides with a dispatch key (yue2/cover's own "mode") survives past
    # the dispatch-key assignments below, the same rule
    # _field_request_value applies to a plain API body's `values`.
    p = dict(slot_values)
    p["values"] = dict(slot_values)
    p["lane"] = lane["id"]
    p["kind"] = slot.get("cap")
    p["mode"] = slot.get("mode")
    if slot.get("recipe") is not None:
        p["recipe"] = slot["recipe"]
    if slot.get("quality") is not None:
        p["quality"] = slot["quality"]
    p["sequence_id"] = sid
    p["slot_id"] = slot_id
    # P2: the Make-time writer check applies to a slot too; the page confirms
    # the same way and sends "confirm" back through here.
    if payload.get("confirm") is True:
        p["confirm"] = True
    try:
        field_values, ref_uses = resolve_slot_refs(seq, slot, lane)
        cable_values, cable_uses = resolve_slot_cables(seq, slot, lane)
    except ValueError as e:
        return {"ok": False, "error": str(e)}, 400
    field_values.update(cable_values)
    p.update(field_values)
    p["values"].update(field_values)
    body, code = generate(p)
    if not body.get("ok"):
        return body, code
    job = body["job"]
    beat = next((b for b in seq.get("beats") or [] if b.get("id") == slot.get("beat_id")), None)
    seq_add_take(sid, slot_id, job["id"], beat_rev=beat.get("rev") if beat else None,
                 inputs={"refs": ref_uses, "cables": cable_uses})
    return {"ok": True, "job": job}, code


# ---------------------------------------------------------------------------
# C3.6 -- the cut. the internal sequence/storyboard design spec Section 6,
# the internal cut-feature commission doc rules R1-R10. Every refusal this module
# can decide before ffmpeg starts (R1, R2, R3, R5, R6, R8's 409) happens
# synchronously in seq_cut_start(), never inside the background thread
# (2026-09-23 refusal-shape ruling) -- only ffmpeg's own failure, or a take
# that turns out corrupt once ffprobe/ffmpeg actually opens it, ends the
# job in status "error" instead of a synchronous 4xx.
# ---------------------------------------------------------------------------

CUT_LOUDNORM_I = -16
# R4 asks for TP <= -1.0 dBTP, but that is loudnorm's OWN pre-encode target --
# the AAC step after it can still overshoot the ceiling by a few tenths of a
# dB on real, transient-heavy content (measured live: -0.9 dBTP on a real
# LTX cut, over the -1.0 rule). -1.5 gives that overshoot headroom while
# still comfortably meeting R4's <= -1.0 rule on the DELIVERED file
# (review fix, 2026-09-23).
CUT_LOUDNORM_TP = -1.5
CUT_LOUDNORM_LRA = 11
# The -1.5 TP headroom above is not a HARD guarantee on every possible
# assembly -- a quiet programme with one brief full-scale moment (dialogue,
# then a door slam) can still make loudnorm's own linear-mode gain push
# that moment past any fixed TP target, since EBU R128 gating excludes
# near-silent stretches from the loudness measurement that gain is based
# on. A sample-peak limiter after loudnorm is what actually holds R4's
# true-peak rule on ANY content: -3 dBFS sample-peak (not dBTP) leaves
# margin under the -1.0 dBTP rule for both true-peak/intersample overshoot
# and the AAC encode's own small addition on top of that -- -2 dB measured
# too tight (a hot-but-not-pathological case still delivered -0.8 dBTP,
# over the rule) on this exact ffmpeg build (review fix, 2026-09-23).
CUT_LIMITER_DB = -3.0
CUT_LIMITER_LIMIT = 10 ** (CUT_LIMITER_DB / 20.0)
# F5: the limiter above is a backstop measured to WORK on the cases tried,
# not a guarantee on any content -- so the delivered file's own true peak is
# measured after encoding (the codec's own overshoot included) and, if it is
# still over this ceiling, corrected rather than handed over as-is. One
# correction pass does not always converge (a re-encode's own overshoot
# differs from the first pass's -- found live, 2026-09-24: a single
# correction still measured -0.6 dBTP), so up to this many CUMULATIVE
# passes run, each re-measuring the real encoded file.
CUT_TP_CEILING = -1.0
CUT_TP_MAX_PASSES = 3
CUT_BED_GAIN_DB = -18
CUT_SAMPLE_RATE = 48000
CUT_CHANNEL_LAYOUT = "stereo"
CUT_TITLE_BAR_FRAC = 0.18   # fraction of frame height the title's backing bar covers


def _probe_json(path, timeout=30):
    """ffprobe's own -show_format -show_streams, or None on any failure --
    a corrupt/unreadable take is a refusal, never a crash."""
    if not CAN_CUT:
        return None
    try:
        r = subprocess.run([FFPROBE_BIN, "-v", "quiet", "-print_format", "json",
                            "-show_format", "-show_streams", path],
                           capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0 or not r.stdout:
            return None
        return json.loads(r.stdout)
    except Exception:
        return None


def _probe_video_stream(info):
    return next((s for s in (info or {}).get("streams", []) if s.get("codec_type") == "video"), None)


def _probe_audio_stream(info):
    return next((s for s in (info or {}).get("streams", []) if s.get("codec_type") == "audio"), None)


def _probe_duration(path):
    info = _probe_json(path)
    if not info:
        return None
    try:
        return float(info["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        return None


def _cut_ensure_take_file(sid, slot_id, job_id):
    """R2's cut-time harvest retry: `seq_harvest` (job_poller's own copy)
    never retries a failed fetch, so a take can sit at file:null forever
    even after its source comes back. Returns the take's local absolute
    path, retrying the copy ONCE by re-fetching the job's source bytes;
    raises ValueError (the shot's refusal sentence) when there is still
    nothing to copy."""
    with SEQ_LOCK:
        seq = _seq_read(sid)
        slot = next((s for s in (seq or {}).get("slots") or [] if s["id"] == slot_id), None) if seq else None
        take = next((t for t in (slot or {}).get("takes") or [] if t.get("job_id") == job_id), None) if slot else None
        rel = take.get("file") if take else None
    if rel:
        path = _seq_take_cache_path(sid, rel)
        if path and os.path.isfile(path):
            return path
    with JOBS_LOCK:
        job = copy.deepcopy(JOBS.get(job_id))
    outs = (job or {}).get("outputs") or []
    if not job or not outs:
        raise ValueError("Shot %s's take is not copied yet — is its lane off?" % slot_id)
    out = result_output(job)
    ext = os.path.splitext(out.get("filename") or "")[1].lower() or ".bin"
    dest = os.path.join(SEQ_MEDIA_DIR, sid, "takes", job_id + ext)
    try:
        data = _carry_source_bytes(job, out)
    except Exception:
        raise ValueError("Shot %s's take is not copied yet — is its lane off?" % slot_id)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, dest)
    with SEQ_LOCK:
        seq = _seq_read(sid)
        found = False
        if seq is not None:
            for s in seq.get("slots") or []:
                if s.get("id") != slot_id:
                    continue
                for t in s.get("takes") or []:
                    if t.get("job_id") == job_id:
                        t["file"] = "takes/%s%s" % (job_id, ext)
                        found = True
            if found:
                seq["rev"] += 1
                seq["updated"] = time.time()
                _seq_write(seq)
    return dest


def _resolve_trim_or_raise(slot_id, trim, duration):
    """R3: null=whole take; in=null is tail-keep (in = D - len); any of
    len > D, in + len > D, in < 0 is a refusal naming the shot and both
    numbers, never clamped. Returns (in, len)."""
    if trim is None:
        return 0.0, duration
    length = trim["len"]
    if length > duration:
        raise ValueError("Shot %s: the requested length %.2fs is longer than its take's length %.2fs. "
                          "Nothing is clamped." % (slot_id, length, duration))
    if trim["in"] is None:
        return duration - length, length
    in_point = trim["in"]
    if in_point < 0:
        raise ValueError("Shot %s: the trim's in-point %.2fs is before the start of its %.2fs take."
                          % (slot_id, in_point, duration))
    if in_point + length > duration:
        raise ValueError("Shot %s: the trim (in=%.2fs, len=%.2fs) runs past the end of its %.2fs take. "
                          "Nothing is clamped." % (slot_id, in_point, length, duration))
    return in_point, length


ASPECT_TOLERANCE = 0.01   # R5 (revised): "same aspect ratio" is within 1%


def _aspect_mismatch(w, h, out_w, out_h):
    return abs((w / h) - (out_w / out_h)) > ASPECT_TOLERANCE * (out_w / out_h)


def _resolve_cut_output_size(probed):
    """R5 (revised 2026-09-23): the cut's own output size is the largest
    picked video take BY AREA, its own dimensions floored to even (yuv420p
    needs even width/height) -- never the sequence canvas, which a pack may
    legitimately deliver a multiple of (LTX delivers 2x its declared
    width/height)."""
    biggest = max(probed, key=lambda c: c["w"] * c["h"])
    return biggest["w"] - (biggest["w"] % 2), biggest["h"] - (biggest["h"] % 2)


def seq_cut_start(payload):
    """POST /api/sequence/cut {id} -> ({ok, cut_id}, 200), or a synchronous
    refusal. Every check that can be decided before ffmpeg starts runs here;
    the background thread (_run_cut) only ever hits ffmpeg's own failure."""
    if not isinstance(payload, dict):
        return {"ok": False, "error": "Send a JSON object."}, 400
    sid = payload.get("id")
    if not seq_valid_id(sid):
        return {"ok": False, "error": "That is not a sequence id."}, 400
    if not CAN_CUT:
        return {"ok": False, "error": CUT_REASON}, 400
    with SEQ_LOCK:
        seq = _seq_read(sid)
    if seq is None:
        return {"ok": False, "error": "There is no such sequence."}, 404
    if any(c.get("status") in ("queued", "running") for c in seq.get("cuts") or []):
        return {"ok": False, "error": "A cut is already running for this sequence. Wait for it to finish."}, 409

    video_slots = [s for s in seq.get("slots") or [] if s.get("lane") == "video"]
    picked = [s for s in video_slots if s.get("pick")]
    if not picked:
        return {"ok": False, "error": "This sequence has no picked video shots yet. Pick a take for at "
                "least one video slot before cutting."}, 400
    left_out = [s["id"] for s in video_slots if not s.get("pick")]

    if not CAN_TITLE and any(s.get("title") for s in picked):
        return {"ok": False, "error": TITLE_REASON}, 400

    # R5 (revised 2026-09-23 after live data): a pack may render at its
    # declared width/height and DELIVER at a multiple of it (LTX delivers
    # 2x) -- a live sequence's canvas and its takes' real pixel sizes
    # routinely differ, so "must equal the canvas" refused every real LTX
    # cut. The cut's own OUTPUT size is now the largest picked take by
    # area; every other take with the SAME aspect ratio (within 1%) is
    # scaled to it; a different aspect ratio is still refused, naming the
    # shot and both sizes. The sequence canvas still governs the cable
    # crop and default_canvas() -- untouched here.
    shots, probed = [], []
    for slot in picked:
        job_id = slot["pick"]
        try:
            path = _cut_ensure_take_file(sid, slot["id"], job_id)
        except ValueError as e:
            return {"ok": False, "error": str(e)}, 400
        info = _probe_json(path)
        vstream = _probe_video_stream(info) if info else None
        duration = None
        try:
            duration = float(info["format"]["duration"]) if info else None
        except (KeyError, TypeError, ValueError):
            duration = None
        if info is None or vstream is None or duration is None:
            return {"ok": False, "error": "Shot %s's take could not be read — it may be corrupt."
                    % slot["id"]}, 400
        vw, vh = vstream.get("width"), vstream.get("height")
        try:
            in_point, length = _resolve_trim_or_raise(slot["id"], slot.get("trim"), duration)
        except ValueError as e:
            return {"ok": False, "error": str(e)}, 400
        with JOBS_LOCK:
            job = JOBS.get(job_id) or {}
        shots.append({"slot_id": slot["id"], "job_id": job_id, "licence": job.get("licence")})
        probed.append({"slot_id": slot["id"], "path": path, "in": in_point, "len": length,
                        "w": vw, "h": vh, "has_audio": _probe_audio_stream(info) is not None,
                        "title": slot.get("title")})

    # Aspect is checked against the SEQUENCE CANVAS (as R5 always has), not
    # against the largest take -- a pack that delivers a multiple of its
    # declared size (LTX: 2x) still shares the canvas's own aspect ratio by
    # construction, and comparing against the canvas (rather than "the
    # other picked takes") is what still refuses a single odd-shaped take
    # even when it is the only video slot picked.
    canvas = seq.get("canvas") or {}
    cw, ch = canvas.get("width"), canvas.get("height")
    out_w, out_h = _resolve_cut_output_size(probed)
    clip_plans = []
    for clip in probed:
        if cw and ch and _aspect_mismatch(clip["w"], clip["h"], cw, ch):
            return {"ok": False, "error": "Shot %s's take is %sx%s, a different shape from the "
                    "sequence's %sx%s canvas; the cut never rescales across a different aspect ratio."
                    % (clip["slot_id"], clip["w"], clip["h"], cw, ch)}, 400
        clip_plans.append(clip)

    bed_path = None
    # The bed is the first SOUND-lane slot (timeline order) with a pick --
    # whether or not it is local YET. It gets the same cut-time harvest
    # retry as a video shot (R2), never a silent "no bed" (owner review,
    # 2026-09-23): a picked bed is a decision the user made, so a copy
    # failure is refused, naming the slot, exactly like a missing video take.
    bed_slot = next((s for s in seq.get("slots") or [] if s.get("lane") == "sound" and s.get("pick")), None)
    if bed_slot is not None:
        try:
            bed_path = _cut_ensure_take_file(sid, bed_slot["id"], bed_slot["pick"])
        except ValueError:
            return {"ok": False, "error": "The music bed (%s) is not copied yet — is its lane off?"
                    % bed_slot["id"]}, 400
        if _probe_json(bed_path) is None:
            return {"ok": False, "error": "The music bed (%s) could not be read — it may be corrupt."
                    % bed_slot["id"]}, 400
        with JOBS_LOCK:
            bed_job = JOBS.get(bed_slot["pick"]) or {}
        shots.append({"slot_id": bed_slot["id"], "job_id": bed_slot["pick"], "licence": bed_job.get("licence"),
                      "role": "bed"})

    cut_id = "k" + uuid.uuid4().hex[:8]
    out_path = os.path.join(SEQ_MEDIA_DIR, sid, "cuts", cut_id + ".mp4")
    entry = {"id": cut_id, "status": "queued", "made": time.time(), "file": None, "log": "",
             "shots": shots, "left_out": left_out, "loudness": None}
    with SEQ_LOCK:
        seq2 = _seq_read(sid)
        if seq2 is None:
            return {"ok": False, "error": "There is no such sequence."}, 404
        if any(c.get("status") in ("queued", "running") for c in seq2.get("cuts") or []):
            return {"ok": False, "error": "A cut is already running for this sequence. Wait for it to finish."}, 409
        seq2.setdefault("cuts", []).append(entry)
        seq2["rev"] += 1
        seq2["updated"] = time.time()
        _seq_write(seq2)
    threading.Thread(target=_run_cut, args=(sid, cut_id, clip_plans, bed_path, out_path, out_w, out_h),
                     daemon=True).start()
    return {"ok": True, "cut_id": cut_id}, 200


def _cut_update(sid, cut_id, **fields):
    with SEQ_LOCK:
        seq = _seq_read(sid)
        if seq is None:
            return
        entry = next((c for c in seq.get("cuts") or [] if c.get("id") == cut_id), None)
        if entry is None:
            return
        entry.update(fields)
        seq["rev"] += 1
        seq["updated"] = time.time()
        _seq_write(seq)


def _ff_quote(value):
    """Single-quote a filter-option value (a path we built ourselves, never
    user text -- title text goes through drawtext's `textfile` instead, see
    _title_filter). Guards the rare case of a configured fontfile path
    holding a space or colon."""
    return "'" + str(value).replace("'", "'\\''") + "'"


def _title_filter(title):
    """R6: burned in with `drawtext`, never escaped by hand -- the title's
    TEXT goes into its own file (`textfile`, read as literal bytes) with
    `expansion=none` (drawtext's own %{...} macro syntax turned off), so
    the punctuation the frozen test exercises (' : % \\ ,) can never be
    read as filtergraph syntax. A solid bar (`drawbox`) sized as a FRACTION
    of the frame -- never a fixed pixel count -- sits behind the text, so
    the titled-vs-untitled frame diff clears the frozen threshold at any
    canvas size, not just the size it was calibrated against. Returns
    (filter_string, temp_text_file_path) -- the caller removes the file
    once ffmpeg has run."""
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False,
                                       dir=CHAIN_DIR, prefix="cuttitle_")
    tmp.write(title["text"])
    tmp.close()
    at, dur = title["at"], title["dur"]
    enable = "between(t,%.6f,%.6f)" % (at, at + dur)
    bar = ("drawbox=x=0:y=ih-ih*%s:w=iw:h=ih*%s:color=black@0.75:t=fill:enable=%s"
           % (CUT_TITLE_BAR_FRAC, CUT_TITLE_BAR_FRAC, _ff_quote(enable)))
    text = ("drawtext=fontfile=%s:textfile=%s:expansion=none:"
            "x=(w-text_w)/2:y=h-(h*%s)/2-(text_h/2):fontsize=h*0.08:fontcolor=white:enable=%s"
            % (_ff_quote(CUT_FONTFILE), _ff_quote(tmp.name), CUT_TITLE_BAR_FRAC, _ff_quote(enable)))
    return bar + "," + text, tmp.name


def _clip_video_chain(i, clip, out_w, out_h):
    parts = ["trim=start=%.6f:duration=%.6f" % (clip["in"], clip["len"]), "setpts=PTS-STARTPTS"]
    # R5 (revised): a same-aspect take that isn't already the cut's own
    # output size is scaled to it here, BEFORE the title -- so a title's
    # own sizing (a fraction of the frame) follows the output, not the
    # take's original pixels.
    if clip["w"] != out_w or clip["h"] != out_h:
        parts.append("scale=%d:%d:flags=lanczos" % (out_w, out_h))
        parts.append("setsar=1")
    title_tmp = None
    if clip.get("title"):
        filt, title_tmp = _title_filter(clip["title"])
        parts.append(filt)
    return "[%d:v]%s[v%d]" % (i, ",".join(parts), i), title_tmp


def _clip_audio_chain(i, clip):
    if clip["has_audio"]:
        parts = ["atrim=start=%.6f:duration=%.6f" % (clip["in"], clip["len"]), "asetpts=PTS-STARTPTS",
                  "aformat=sample_rates=%d:channel_layouts=%s" % (CUT_SAMPLE_RATE, CUT_CHANNEL_LAYOUT)]
        return "[%d:a]%s[a%d]" % (i, ",".join(parts), i)
    # R5: a clip with no audio stream gets silence, so `concat` never fails.
    return "anullsrc=r=%d:cl=%s:d=%.6f[a%d]" % (CUT_SAMPLE_RATE, CUT_CHANNEL_LAYOUT, clip["len"], i)


def _measure_loudness(input_args, graph_stmts, final_audio_label, timeout=600):
    """Pass 1 of R4's two-pass loudnorm (review fix, 2026-09-23): runs the
    SAME filter graph up to (not including) the final loudnorm, in
    loudnorm's own measurement mode (`print_format=json`), and discards
    the audio (`-f null -`) -- ffmpeg still decodes/filters every input to
    get there (the joined audio IS what must be measured), but never
    invokes the video encoder, so this is far cheaper than a second full
    encode. Returns the parsed measurement dict, or None on any failure."""
    fc = ";".join(list(graph_stmts) + [
        "[%s]loudnorm=I=%s:TP=%s:LRA=%s:print_format=json[measured]"
        % (final_audio_label, CUT_LOUDNORM_I, CUT_LOUDNORM_TP, CUT_LOUDNORM_LRA)])
    args = [FFMPEG_BIN, "-y", "-hide_banner"] + list(input_args) + [
        "-filter_complex", fc, "-map", "[measured]", "-f", "null", "-"]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


def _measure_true_peak(path, timeout=120):
    """F5: reads the ENCODED file's own true peak (ebur128, peak=true) --
    unlike _measure_loudness (the pre-encode filter graph), this runs on the
    real muxed output, so the AAC encode's own overshoot is included. Reads
    the "Peak:" line inside the Summary's "True peak:" section, which reads
    the same on ffmpeg 4.4.2 and 8 (both were checked); the regex takes the
    LAST "Peak:" match in stderr so it can never be fooled by an unrelated
    line, and returns None (never raises) on any failure to run or parse."""
    try:
        r = subprocess.run([FFMPEG_BIN, "-y", "-hide_banner", "-i", path,
                            "-filter_complex", "[0:a]ebur128=peak=true:metadata=0[out]",
                            "-map", "[out]", "-f", "null", "-"],
                           capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None
    matches = re.findall(r"Peak:\s*(-?[\d.]+)\s*dB", r.stderr or "")
    if not matches:
        return None
    try:
        return float(matches[-1])
    except ValueError:
        return None


def _run_cut(sid, cut_id, clip_plans, bed_path, out_path, out_w, out_h):
    """The whole ffmpeg build, off the request thread (R8). ffmpeg always
    runs as an argv list, never a shell. Any failure -- ffmpeg's own
    non-zero exit, a take that turns out corrupt once ffmpeg opens it, or a
    Python exception in this function -- ends the job in status "error"
    with a short, plain-text log, never a Python traceback."""
    title_tmps = []
    try:
        n = len(clip_plans)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        _cut_update(sid, cut_id, status="running")
        input_args = []
        for clip in clip_plans:
            input_args += ["-i", clip["path"]]
        bed_index = n if bed_path else None
        if bed_path:
            input_args += ["-i", bed_path]
        v_chains, a_chains = [], []
        for i, clip in enumerate(clip_plans):
            vchain, ttmp = _clip_video_chain(i, clip, out_w, out_h)
            if ttmp:
                title_tmps.append(ttmp)
            v_chains.append(vchain)
            a_chains.append(_clip_audio_chain(i, clip))
        concat_inputs = "".join("[v%d][a%d]" % (i, i) for i in range(n))
        graph = v_chains + a_chains + ["%sconcat=n=%d:v=1:a=1[vcat][acat]" % (concat_inputs, n)]
        # A second, AUDIO-ONLY concat (never producing [vcat]) for the
        # measurement pass below: on ffmpeg 4.4.2, a filter_complex output
        # pad that is built but never `-map`ped ("Filter concat:out:v0 has
        # an unconnected output") is a hard error, not just a warning, so
        # the measurement pass -- which only ever maps the measured audio,
        # never [vcat] -- needs a graph that never creates an unmapped
        # video pad in the first place. Bonus: ffmpeg then never decodes
        # the video streams for this pass at all.
        audio_graph = a_chains + ["%sconcat=n=%d:v=0:a=1[acat]" % ("".join("[a%d]" % i for i in range(n)), n)]

        def _mix_bed(stmts, acat_label):
            stmts = list(stmts)
            stmts.append("[%d:a]aformat=sample_rates=%d:channel_layouts=%s,volume=%ddB[bed]"
                         % (bed_index, CUT_SAMPLE_RATE, CUT_CHANNEL_LAYOUT, CUT_BED_GAIN_DB))
            stmts.append("[%s]aformat=sample_rates=%d:channel_layouts=%s[acatf]"
                         % (acat_label, CUT_SAMPLE_RATE, CUT_CHANNEL_LAYOUT))
            stmts.append("[acatf][bed]amix=inputs=2:duration=first:normalize=0[amixed]")
            return stmts, "amixed"

        final_audio = "acat"
        if bed_path:
            # R7: the bed starts at 0, is mixed at -18dB under the clip
            # audio BEFORE the single loudnorm below, and is cut at the
            # video's end -- amix's own duration=first stops the mix at
            # the joined clip audio's length, silently padding a shorter
            # bed with silence past that point and truncating a longer one.
            graph, final_audio = _mix_bed(graph, "acat")
            audio_graph, _ = _mix_bed(audio_graph, "acat")
        # R4 (review fix, 2026-09-23): a single-pass loudnorm on a short
        # assembly measured -14.4 LUFS against the -16 target -- loudnorm's
        # own single-pass mode is an ESTIMATE. Two-pass (measure, then
        # apply with linear=true and the measured values) is still ONE
        # normalisation over the whole joined assembly (R4's own rule:
        # never per-clip), just accurate. The measure pass never invokes
        # the video encoder (see _measure_loudness). loudnorm also
        # resamples internally to its own rate regardless of input --
        # `aresample` after it is what actually lands the output back at
        # CUT_SAMPLE_RATE.
        measured = _measure_loudness(input_args, audio_graph, final_audio)
        measured_i = measured.get("input_i") if measured else None
        try:
            measured_i_finite = measured_i is not None and math.isfinite(float(measured_i))
        except (TypeError, ValueError):
            measured_i_finite = False
        if measured and measured_i_finite:
            loudness_mode = "two-pass"
            loudnorm = ("loudnorm=I=%s:TP=%s:LRA=%s:measured_I=%s:measured_TP=%s:measured_LRA=%s:"
                        "measured_thresh=%s:offset=%s:linear=true"
                        % (CUT_LOUDNORM_I, CUT_LOUDNORM_TP, CUT_LOUDNORM_LRA,
                           measured.get("input_i"), measured.get("input_tp"), measured.get("input_lra"),
                           measured.get("input_thresh"), measured.get("target_offset")))
        elif measured:
            # The measure pass ran fine but reported a non-finite loudness
            # (-inf) -- every picked shot is silent (no audio stream, no
            # bed). loudnorm has nothing to normalise and, fed measured_I=
            # -inf, would corrupt the output; silence needs no normalising
            # at all, so it is skipped outright and the cut is marked
            # "silent" rather than pretending a measurement happened.
            loudness_mode = "silent"
            loudnorm = None
        else:
            # The measure pass itself failed (ffmpeg/timeout trouble) --
            # fall back to loudnorm's own single-pass estimate rather than
            # failing the whole cut over a measurement that was only ever
            # going into a BETTER estimate.
            loudness_mode = "estimated"
            loudnorm = "loudnorm=I=%s:TP=%s:LRA=%s" % (CUT_LOUDNORM_I, CUT_LOUDNORM_TP, CUT_LOUDNORM_LRA)
        pre_limiter = final_audio
        if loudnorm:
            graph.append("[%s]%s[aln]" % (final_audio, loudnorm))
            pre_limiter = "aln"
        # A sample-peak limiter after loudnorm (see CUT_LIMITER_DB) -- the
        # backstop that holds R4's true-peak rule on content loudnorm's own
        # TP target cannot: a quiet assembly with one brief full-scale
        # moment can still land near/over the ceiling after loudnorm's
        # linear-mode gain (EBU R128 gating excludes near-silent stretches
        # from the loudness measurement that gain is based on).
        # level=false: alimiter's own "auto level" defaults to true, which
        # re-boosts the output back toward 0dB after limiting -- exactly
        # undoing the ceiling this filter exists to hold, and skewing the
        # NORMAL case's integrated loudness away from the two-pass target
        # in the process (both measured directly on this box).
        graph.append("[%s]alimiter=limit=%.6f:attack=5:release=50:level=false[alim]"
                     % (pre_limiter, CUT_LIMITER_LIMIT))

        def _encode(correction_db=None):
            """One full ffmpeg build+run. `correction_db` (F5's correction
            passes) inserts one more `volume` stage between the limiter and
            the final `aformat`, so the graph up to [alim] is built exactly
            once and shared by every pass -- including `_measure_loudness`
            above, which ran ONCE, before this closure even exists, and is
            never re-run per pass (the two-pass loudnorm measurement is a
            property of the CONTENT, not of how much extra `volume` trim a
            later pass adds on top of it)."""
            afinal_src = "alim"
            extra_stmt = []
            if correction_db is not None:
                extra_stmt = ["[alim]volume=%.3fdB[avol]" % correction_db]
                afinal_src = "avol"
            # `aresample=<rate>` alone hits "Cannot select channel layout for
            # the link" on ffmpeg 4.4.2 immediately after loudnorm (measured
            # directly on this box) -- `aformat` (already used everywhere else
            # in this graph) sets sample rate AND channel layout explicitly,
            # which is what actually avoids the ambiguity.
            full_graph = graph + extra_stmt + [
                "[%s]aformat=sample_rates=%d:channel_layouts=%s[afinal]"
                % (afinal_src, CUT_SAMPLE_RATE, CUT_CHANNEL_LAYOUT)]
            args = [FFMPEG_BIN, "-y", "-hide_banner"] + input_args
            args += ["-filter_complex", ";".join(full_graph),
                     "-map", "[vcat]", "-map", "[afinal]",
                     "-map_metadata", "-1",
                     "-c:v", "libx264", "-pix_fmt", "yuv420p",
                     "-c:a", "aac",
                     "-vsync", "cfr", "-r", "24",
                     "-movflags", "+faststart",
                     out_path]
            return subprocess.run(args, capture_output=True, text=True, timeout=1800)

        r = _encode()
        if r.returncode != 0 or not os.path.isfile(out_path):
            _cut_update(sid, cut_id, status="error",
                       log=("ffmpeg could not build this cut.\n" + (r.stderr or "")[-1500:]))
            return
        # F5: the limiter above is a backstop, not a guarantee -- measure the
        # DELIVERED file's own true peak (the codec's own overshoot included)
        # and, if it is over the ceiling, correct. One pass does not always
        # converge -- a re-encode's OWN overshoot differs from the first
        # pass's (found live, 2026-09-24: a single correction still measured
        # -0.6 dBTP) -- so up to CUT_TP_MAX_PASSES corrective passes run,
        # each one lowering the gain by the CUMULATIVE amount still needed
        # (never resetting to just the latest overshoot, which could undo an
        # earlier pass's correction), re-measuring the real encoded file
        # after every pass rather than trusting the math.
        extra_fields = {}
        peak_db = _measure_true_peak(out_path)
        if peak_db is not None:
            extra_fields["true_peak_db"] = peak_db
        elif loudness_mode != "silent":
            # P4: the limiter above is still a real backstop, but with no
            # measurement to confirm it held, the cut's own record must say
            # so rather than imply a checked peak.
            extra_fields["peak_unverified"] = True
        total_correction_db = 0.0
        passes = 0
        while peak_db is not None and peak_db > CUT_TP_CEILING and passes < CUT_TP_MAX_PASSES:
            total_correction_db += -(peak_db + 1.2)
            r = _encode(correction_db=total_correction_db)
            passes += 1
            if r.returncode != 0 or not os.path.isfile(out_path):
                _cut_update(sid, cut_id, status="error",
                           log=("ffmpeg could not build the peak-corrected cut.\n" + (r.stderr or "")[-1500:]))
                return
            peak_db = _measure_true_peak(out_path)
            if peak_db is not None:
                extra_fields["true_peak_db"] = peak_db
                extra_fields.pop("peak_unverified", None)
        if passes:
            extra_fields["peak_corrected"] = True
            extra_fields["peak_passes"] = passes
        if peak_db is not None and peak_db > CUT_TP_CEILING:
            _cut_update(sid, cut_id, status="error",
                       log=("This cut still measured %.1f dBTP after %d peak-correction pass(es), "
                            "above the %s dBTP ceiling."
                            % (peak_db, passes, CUT_TP_CEILING)),
                       **extra_fields)
            return
        _cut_update(sid, cut_id, status="done", file="cuts/%s.mp4" % cut_id, log=(r.stderr or "")[-800:],
                   loudness=loudness_mode, **extra_fields)
    except Exception as e:
        log("Cutting a sequence failed: %s" % str(e)[:300], "error")
        _cut_update(sid, cut_id, status="error", log="The cut failed unexpectedly.")
    finally:
        for t in title_tmps:
            try:
                os.remove(t)
            except OSError:
                pass


def seq_mark_interrupted_cuts():
    """Startup sweep (R8): a cut left queued/running when the app last
    stopped reads interrupted from now on, never running forever."""
    with SEQ_LOCK:
        for sid, seq in _seq_all():
            if seq is None:
                continue
            changed = False
            for c in seq.get("cuts") or []:
                if c.get("status") in ("queued", "running"):
                    c["status"] = "interrupted"
                    changed = True
            if changed:
                seq["rev"] += 1
                seq["updated"] = time.time()
                _seq_write(seq)


def _seq_file_path(sid, rel):
    """R9's own containment check for GET /api/sequence/file -- realpath-
    contained in data/seq/<id>/, same rule as seq_ref_path/_seq_take_cache_path,
    but STRICT (raises) rather than soft: the spec says an escaping path is
    400, not a silent cache-miss."""
    if not rel or not isinstance(rel, str) or ".." in rel.split("/"):
        raise ValueError("That path is not allowed.")
    base = os.path.realpath(os.path.join(SEQ_MEDIA_DIR, sid))
    path = os.path.realpath(os.path.join(base, rel))
    if path != base and not path.startswith(base + os.sep):
        raise ValueError("That path is not allowed.")
    return path


# ---------------------------------------------------------------------------
# Multipart parsing (cgi is deprecated/removed; this is ~40 lines and ours)
# ---------------------------------------------------------------------------

def parse_multipart(body, boundary):
    parts = []
    delim = b"--" + boundary
    for chunk in body.split(delim):
        if not chunk or chunk[:2] == b"--":
            continue
        chunk = chunk.lstrip(b"\r\n")
        if b"\r\n\r\n" not in chunk:
            continue
        raw_head, data = chunk.split(b"\r\n\r\n", 1)
        if data.endswith(b"\r\n"):
            data = data[:-2]
        head = {}
        for line in raw_head.decode("utf-8", "replace").split("\r\n"):
            if ":" in line:
                k, v = line.split(":", 1)
                head[k.strip().lower()] = v.strip()
        disp = head.get("content-disposition", "")
        name = re.search(r'name="([^"]*)"', disp)
        fname = re.search(r'filename="([^"]*)"', disp)
        parts.append({
            "name": name.group(1) if name else "",
            "filename": fname.group(1) if fname else None,
            "content_type": head.get("content-type", "application/octet-stream"),
            "data": data,
        })
    return parts


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

def request_refusal(handler):
    """B1 request guard (pattern borrowed from a sibling studio app): this app
    has no auth, so every request must prove it arrived through its own
    address (DNS-rebinding protection), and every POST must prove it was not
    fired from somebody else's web page (simple cross-site POST protection).
    Returns (code, sentence) to refuse with, or None to let it through."""
    get_all = getattr(handler.headers, "get_all", None)   # real requests carry an
    if get_all and len(get_all("Host") or []) > 1:         # HTTPMessage; some tests
        return 400, "Send exactly one Host header."         # stub headers as a plain dict
    host_hdr = (handler.headers.get("Host") or "").strip()
    if not host_hdr:
        return 400, "Requests to this app must carry a Host header."
    if host_hdr.startswith("["):                      # IPv6 literal: [::1]:3998
        m = re.match(r"^\[([^\]]+)\](?::(\d+))?$", host_hdr)
        if not m:
            return 403, "That Host header is not a valid address."
        hostname, port = m.group(1), m.group(2)
    else:
        hostname, sep, port = host_hdr.partition(":")
        if sep and not port.isdigit():
            return 403, "That Host header is not a valid address."
        port = port or None
    if port is not None and port != str(PORT):
        return 403, ("This app only answers on port %d, not %s. A proxy in front of it must "
                     "pass the Host header with no port, or with port %d."
                     % (PORT, hostname + ":" + port, PORT))
    hn = hostname.lower().strip("[]")
    allowed = {"localhost", "127.0.0.1", "::1"}
    try:                                   # the local address this connection
        allowed.add(handler.connection.getsockname()[0].lower())   # arrived on,
    except Exception:                      # so a LAN IP needs no config
        pass
    gname = socket.gethostname().lower()
    allowed |= {gname, gname + ".local"}
    if BIND not in ("0.0.0.0", "::"):
        allowed.add(BIND.lower())
    for entry in ALLOWED_HOSTS:                        # each optionally host:port
        if entry.count(":") == 1:                       # (more = an IPv6 literal)
            eh, _, ep = entry.partition(":")
            if ep.isdigit() and ep == str(PORT):
                allowed.add(eh)
            continue
        allowed.add(entry)
    if hn not in allowed:
        return 403, ("This app only answers to its own address, not %s. If that is how you "
                     "reach it, add \"%s\" to \"allowed_hosts\" in config.json."
                     % (hostname, hostname))
    if handler.command != "POST":
        return None
    # Simple cross-site POSTs: a form/fetch may only carry text/plain,
    # application/x-www-form-urlencoded or multipart/form-data, so demanding
    # application/json (multipart for uploads) refuses every one of them.
    ctype = (handler.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if handler.path.split("?")[0] == "/api/upload":
        if ctype != "multipart/form-data":
            return 415, "Send the file as a multipart/form-data upload."
    elif ctype != "application/json":
        return 415, "Send JSON (Content-Type: application/json)."
    origin = (handler.headers.get("Origin") or "").strip()
    if origin and origin.lower() != "null":
        onetloc = urllib.parse.urlparse(origin).netloc.lower()
        if onetloc != host_hdr.lower():
            return 403, "Requests from another site are refused."
    if (handler.headers.get("Sec-Fetch-Site") or "").strip().lower() == "cross-site":
        return 403, "Requests from another site are refused."
    return None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "GenerationCenter/1.0"

    def log_message(self, fmt, *args):
        pass

    # -- helpers ------------------------------------------------------------
    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_blob(self, data, ctype, filename=None, download=False, ranges=False):
        """`ranges=True` serves a real 206 when asked. Safari will not play a
        <video> from a source that cannot do byte ranges, so the gallery needs it."""
        start, end = 0, len(data) - 1
        partial = False
        if ranges and not download:
            m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "") or "")
            if m and len(data):
                g1, g2 = m.group(1), m.group(2)
                if g1:
                    start = min(int(g1), len(data) - 1)
                    end = min(int(g2), len(data) - 1) if g2 else len(data) - 1
                elif g2:                       # suffix form: bytes=-500
                    start = max(0, len(data) - int(g2))
                if start <= end:
                    partial = True
        body = data[start:end + 1] if partial else data
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes" if ranges else "none")
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, len(data)))
        if filename:
            disp = "attachment" if download else "inline"
            self.send_header("Content-Disposition", '%s; filename="%s"' % (disp, filename))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        buf = b""
        while len(buf) < n:
            chunk = self.rfile.read(min(65536, n - len(buf)))
            if not chunk:
                break
            buf += chunk
        return buf

    def read_json(self):
        return json.loads(self.read_body().decode("utf-8") or "{}")

    # -- GET ----------------------------------------------------------------
    def refuse(self):
        """Run the B1 guard; if it refuses, drain any body (keep-alive must not
        read it as the next request) and send the sentence. True = refused.
        A body over 1 MiB (or a malformed Content-Length) is never drained --
        a refused POST can lie about its length to stall this thread reading
        it, so past the cap the connection is dropped instead of read."""
        refusal = request_refusal(self)
        if refusal:
            if self.command == "POST":
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = -1
                if 0 <= length <= 1048576:
                    self.read_body()
                else:
                    self.close_connection = True
            code, sentence = refusal
            self.send_json({"ok": False, "error": sentence}, code)
            return True
        return False

    def do_GET(self):
        if self.refuse():
            return
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path == "/favicon.ico":
                # F4: this app serves no icon; a bare 404 logs a console error
                # on every page load, so answer with an explicit "there is none".
                self.send_response(204)
                self.send_header("Cache-Control", "max-age=86400")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if u.path in ("/", "/index.html"):
                with open(os.path.join(APP_DIR, "index.html"), "rb") as f:
                    return self.send_blob(f.read(), "text/html; charset=utf-8")
            if u.path in ("/help", "/help.html"):
                with open(os.path.join(APP_DIR, "help.html"), "rb") as f:
                    return self.send_blob(f.read(), "text/html; charset=utf-8")
            if u.path == "/api/lanes":
                return self.send_json(self.lanes_payload())
            if u.path == "/api/jobs":
                return self.send_json(self.jobs_payload(q))
            if u.path == "/api/view":
                return self.proxy_view(q)
            if u.path == "/api/health":
                return self.send_json({"ok": True, "port": PORT, "lanes": len(LANES)})
            if u.path == "/api/engines":
                return self.send_json(self.engines_payload(q))
            if u.path == "/api/credits":
                # R6: engines.licences() verbatim -- the page's own credits/export list --
                # plus S1's clean_download: which media kinds a download is actually
                # cleaned for right now, so the share dialog stops over-promising.
                try:
                    from PIL import Image as _Image  # noqa: F401
                    _pil_ok = True
                except ImportError:
                    _pil_ok = False
                return self.send_json({
                    "credits": engines.licences(),
                    "clean_download": ((["image"] if _pil_ok else [])
                                        + ["audio"]
                                        + (["video"] if shutil.which("ffmpeg") else [])),
                    # F1: audio cleaning is per-format, not per-kind -- sanitize.py only
                    # understands these; WAV/M4A (accepted by strip_metadata's filename
                    # match) come back stripped=False and are refused, never advertised.
                    "clean_audio_exts": ["flac", "mp3", "opus", "ogg"],
                })
            if u.path == "/api/sequences":
                return self.send_json(*seq_list())
            if u.path == "/api/sequence":
                return self.send_json(*seq_get((q.get("id") or [""])[0]))
            if u.path == "/api/sequence/file":
                return self.sequence_file(q)
            if u.path == "/api/guide":
                return self.send_json(*guide_payload((q.get("room") or [""])[0]))
            self.send_json({"error": "not found"}, 404)
        except BrokenPipeError:
            pass
        except Exception as e:
            # E1: the full traceback already went to the server log above;
            # the page only ever sees a plain sentence, with the technical
            # text moved to `detail` for the expander, never Python's own.
            traceback.print_exc()
            self.send_json({"error": "Something went wrong handling that request.",
                            "detail": str(e)}, 500)

    def lanes_payload(self):
        out = []
        with STATE_LOCK:
            snap = {k: dict(v) for k, v in LANE_STATE.items()}
        for l in LANES:
            st = snap.get(l["id"], {})
            able = abilities(l)
            with DISCOVERY_LOCK:
                disc = dict(DISCOVERY.get(l["id"]) or {})
            # `caps` is what this lane is FOR; `able` is what it HAS. The UI
            # offers the intersection, and says which side said no.
            effective = [c for c in l["caps"] if able[c]] if disc.get("checked") else list(l["caps"])
            out.append({
                "id": l["id"], "name": l["name"], "kind": lane_kind(l),
                "box": l["box"], "note": l["note"],
                "shared": l.get("shared", ""),
                "endpoint": ("" if lane_kind(l) == "process"
                             else "%s:%d" % (l["host"], l["port"])),
                "gpu": l["gpu"], "gpu_label": l["gpu_label"],
                "caps": effective, "declared_caps": l["caps"],
                "able": able,
                "discovered": bool(disc.get("checked")),
                "has": {c: engines.describe(models_for(l), c) if able.get(c) else "" for c in engines.caps()},
                "files": models_for(l),
                **{"missing_%s" % c: missing_for(l, c) if (c in l["caps"] and not able.get(c)) else []
                   for c in engines.caps()},
                "up": bool(st.get("up")), "err": st.get("err", ""),
                "device": st.get("device", ""),
                "vram_free": st.get("vram_free", 0), "vram_total": st.get("vram_total", 0),
                "running": st.get("running", 0), "pending": st.get("pending", 0),
                "comfy": st.get("comfy", ""), "checked": st.get("checked", 0),
                **({"load": st.get("load", 0), "cores": st.get("cores", 1)}
                   if lane_kind(l) == "process" else {}),
            })
        payload = {"lanes": out, "title": TITLE, "fleet_llm": None}
        if FLEET_LLM:
            payload["fleet_llm"] = dict(FLEET_STATE, name=FLEET_LLM.get("name", "Other server"),
                                        gpu_label=FLEET_LLM.get("gpu_label", ""),
                                        endpoint="%s:%d" % (FLEET_LLM["host"], FLEET_LLM["port"]))
        return payload

    def engines_payload(self, q):
        """R1: the pack describes its own form. Per cap, its modes -- id, label,
        available, missing, and the field list a UI builds its form from. A cap
        with no engine pack (e.g. "audio" on a fleet with none installed) still
        returns an empty modes list, never a KeyError.

        `lane` is optional: with one, availability/missing reflect what that
        lane actually has; without one, every mode reports unavailable (the UI
        already requires picking a lane before it can generate anyway).
        """
        lane = LANE_BY_ID.get((q.get("lane") or [""])[0])
        m = models_for(lane) if lane else {}
        able = engines.abilities(m)
        out = {}
        for cap in engines.caps():
            words = engines.mode_words(cap)
            modes = []
            for mode in engines.modes_for(cap):
                # F1: a mode is not always its own ability name (image's
                # t2i/edit both draw from the single "image" ability) --
                # resolve through the ONE helper so `available` and
                # `missing` can never disagree.
                ability = engines.mode_ability(cap, mode)
                # P1: a pack's own mode_deps check (a local Python package the
                # CORE needs, independent of any lane) folds into the SAME
                # available/missing the UI already shows for a missing model
                # -- never a second "can't run" concept.
                deps_reason = engines.mode_deps_reason(cap, mode)
                # C2: a mode whose lane_kind doesn't match the QUERIED lane runs on the
                # wrong kind of machine entirely -- no model file will ever fix that, so
                # listing "missing: ['Blender', 'ffmpeg']" against a ComfyUI lane (which
                # never even attempts discovery for those roles) is actively misleading.
                # Say the mismatch in one plain, actionable sentence instead of the usual
                # model-gap list; a process lane is a config addition, not a download.
                wanted_kind = engines.lane_kind(cap, mode)
                kind_mismatch = lane is not None and lane_kind(lane) != wanted_kind
                if kind_mismatch:
                    if wanted_kind == "process":
                        mismatch_reason = (
                            "this mode runs on a process lane, not a ComfyUI lane: add "
                            '{"id": "cpu", "name": "This machine", "kind": "process", '
                            '"caps": ["%s"]} to config.json' % cap)
                    else:
                        mismatch_reason = (
                            "this mode runs on a ComfyUI lane, not a process lane: point a "
                            "ComfyUI lane's host/port at it in config.json")
                    model_missing = [mismatch_reason]
                else:
                    model_missing = [] if able.get(ability) else engines.missing_words(m, ability)
                modes.append({
                    "id": mode,
                    "label": words.get(mode, mode),
                    "available": bool(able.get(ability)) and deps_reason is None and not kind_mismatch,
                    "missing": model_missing if kind_mismatch else
                               model_missing + ([deps_reason] if deps_reason else []),
                    "fields": engines.fields(cap, mode),
                    "presets": engines.presets(cap, mode),
                    # H2: the "Try this" row -- a one-click example per mode.
                    "examples": engines.examples(cap, mode),
                    # R1/R4: the quality ladder, per-lane availability and
                    # R5's honest estimate layered on top of the pack's own
                    # declaration -- the page never sees a step count.
                    "quality": [
                        dict(tier,
                             available=bool(able.get(tier["requires"])) if tier.get("requires") else True,
                             estimate_s=estimate_seconds(lane["id"], cap, mode, tier["id"]) if lane else None)
                        for tier in engines.quality(cap, mode)
                    ],
                    # Rooms slice: "when to pick this one" (pack-declared, or
                    # None) and the licence the finished job would carry.
                    "note": engines.mode_note(cap, mode),
                    # B1c: which lane KIND this mode runs on (comfy / process) --
                    # the page uses it to route the job to a machine that can
                    # actually run it, instead of just disabling the row.
                    "lane_kind": engines.lane_kind(cap, mode),
                    "licence": engines.licence_for(cap, mode),
                    # L5: whether this mode has its own prompt_guide (else the
                    # helper falls back to a generic instruction).
                    "prompt_guide": bool(engines.prompt_guide(cap, mode)),
                    # P2: the guide's writing skill for this mode, or None.
                    "writer": _writer_payload(cap, mode),
                    # P3d: the mode that edits this mode's finished result, or None.
                    "edit_in": engines.edit_in(cap, mode),
                    # P2b: the guide's "Not right?" skill for this mode's results, or None.
                    "reviser": ({"label": engines.reviser(cap, mode)["label"]}
                                if engines.reviser(cap, mode) else None),
                    # P3a: the mode's post step, by what the page calls it, or None.
                    # Its output is the job's result; the lane's render is "before" it.
                    "post": ({"words": engines.post_words(cap, mode)}
                             if engines.post_words(cap, mode) else None),
                })
            out[cap] = {"modes": modes, "cap_word": engines.cap_word(cap), "cap_order": engines.cap_order(cap)}
        # Rooms by task (rooms.json + each pack's mode_rooms). NOT a cap: every
        # consumer that iterates this payload's keys as caps must skip "rooms".
        out["rooms"] = engines.rooms()
        # L5: whether a prompt helper is configured at all. NOT a cap either --
        # same "skip this key" rule as "rooms".
        out["helper"] = bool(HELPER)
        return out

    def jobs_payload(self, q):
        limit = int((q.get("limit") or ["60"])[0])
        with JOBS_LOCK:
            ids = list(JOB_ORDER)[-limit:][::-1]
            jobs = [dict(JOBS[i]) for i in ids if i in JOBS]
        with LOG_LOCK:
            logs = list(LOG)[:60]
        return {"jobs": jobs, "log": logs, "now": time.time()}

    def proxy_view(self, q):
        """Pull a finished file from a lane's /view and hand it to the browser.

        Downloads are SANITIZED by default. Every ComfyUI PNG carries a `prompt`
        text chunk holding the whole workflow -- model filenames and the full
        prompt text -- so handing someone a render hands them your model stack.
        Measured on this fleet 2026-09-21, and again on this app's first output.

        Viewing in the browser is untouched (the bytes never leave the LAN).
        Stripping happens at `dl=1`, which IS the moment the file leaves.
        Pass `keep_recipe=1` to download the original, metadata and all.

        `type=local` is a DIFFERENT source entirely: a `post` step's own
        output under LOCAL_OUTPUTS_DIR, never proxied through a lane -- see
        proxy_view_local().
        """
        if (q.get("type") or [""])[0] == "local":
            return self.proxy_view_local(q)
        lane = LANE_BY_ID.get((q.get("lane") or [""])[0])
        if not lane:
            return self.send_json({"error": "unknown lane"}, 400)
        params = {
            "filename": (q.get("filename") or [""])[0],
            "subfolder": (q.get("subfolder") or [""])[0],
            "type": (q.get("type") or ["output"])[0],
        }
        url = lane_url(lane, "/view?" + urllib.parse.urlencode(params))
        try:
            data, ctype = http_get_bytes(url, timeout=120.0)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return self.send_json({"error": "%s does not have that file any more." % lane["name"]}, 404)
            return self.send_json({"error": "%s would not hand over that file (it answered %d)."
                                   % (lane["name"], e.code)}, 502)
        except OSError:
            return self.send_json({"error": "%s is not answering, so the file cannot be fetched right now. "
                                   "Check that ComfyUI is running there." % lane["name"]}, 502)
        download = (q.get("dl") or ["0"])[0] == "1"
        keep = (q.get("keep_recipe") or ["0"])[0] == "1"
        if download and not keep:
            data, ctype, stripped = strip_metadata(data, ctype, params["filename"])
            if stripped is False:
                return self.send_json({"error": CLEAN_STRIP_REFUSED}, 422)
        return self.send_blob(data, ctype, os.path.basename(params["filename"]), download, ranges=True)

    def proxy_view_local(self, q):
        """Serve a `post` step's own output (job["outputs"] entries of type
        "local") straight off disk -- there is no lane to proxy through.

        Security boundary: the job id and filename come from the query
        string -- untrusted, same as run_post_step()'s pack-returned
        filename on the write side -- so both go through local_output_path(),
        the one rule that refuses the moment the resolved path lands outside
        LOCAL_OUTPUTS_DIR (`..`, an absolute path, or a symlink that escapes).

        The job id may arrive as `job` (legacy/tests) or, as index.html's
        viewURL() actually sends it, as `subfolder` -- both run_post_step()
        and run_process_job() store the job id as the output's subfolder.
        """
        job_id = (q.get("job") or [""])[0] or (q.get("subfolder") or [""])[0]
        filename = (q.get("filename") or [""])[0]
        if not job_id or not filename:
            return self.send_json({"error": "job and filename are required"}, 400)
        try:
            path = local_output_path(job_id, filename)
        except ValueError as e:
            return self.send_json({"error": str(e)}, 400)
        if not os.path.isfile(path):
            return self.send_json({"error": "not found"}, 404)
        with open(path, "rb") as f:
            data = f.read()
        clean_name = os.path.basename(path)   # local_output_path()'s own resolved name, not the raw query
        ctype = mimetypes.guess_type(clean_name)[0] or "application/octet-stream"
        download = (q.get("dl") or ["0"])[0] == "1"
        keep = (q.get("keep_recipe") or ["0"])[0] == "1"
        if download and not keep:
            data, ctype, stripped = strip_metadata(data, ctype, clean_name)
            if stripped is False:
                return self.send_json({"error": CLEAN_STRIP_REFUSED}, 422)
        return self.send_blob(data, ctype, clean_name, download, ranges=True)

    def sequence_file(self, q):
        """R9: GET /api/sequence/file?id=&path= -- a cached ref/take/cut,
        served straight off disk with byte ranges (send_blob), the same
        `dl=1` convention as proxy_view. `path` must resolve inside
        data/seq/<id>/; anything else is 400."""
        sid = (q.get("id") or [""])[0]
        if not seq_valid_id(sid):
            return self.send_json({"error": "That is not a sequence id."}, 400)
        try:
            path = _seq_file_path(sid, (q.get("path") or [""])[0])
        except ValueError as e:
            return self.send_json({"error": str(e)}, 400)
        if not os.path.isfile(path):
            return self.send_json({"error": "not found"}, 404)
        with open(path, "rb") as f:
            data = f.read()
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        download = (q.get("dl") or ["0"])[0] == "1"
        return self.send_blob(data, ctype, os.path.basename(path), download, ranges=True)

    # -- POST ---------------------------------------------------------------
    def do_POST(self):
        if self.refuse():
            return
        u = urllib.parse.urlparse(self.path)
        try:
            if u.path == "/api/upload":
                return self.api_upload()
            if u.path == "/api/generate":
                return self.api_generate()
            if u.path == "/api/chain":
                return self.api_chain()
            if u.path == "/api/carry":
                return self.send_json(*carry_result(self.read_json()))
            if u.path == "/api/cancel":
                return self.api_cancel()
            if u.path == "/api/forget":
                return self.api_forget()
            if u.path == "/api/sequence":
                return self.send_json(*seq_create(self.read_json()))
            if u.path == "/api/sequence/delete":
                return self.send_json(*seq_delete(self.read_json()))
            if u.path == "/api/sequence/op":
                return self.send_json(*seq_op(self.read_json()))
            if u.path == "/api/sequence/generate":
                return self.send_json(*seq_generate(self.read_json()))
            if u.path == "/api/sequence/cut":
                return self.send_json(*seq_cut_start(self.read_json()))
            if u.path == "/api/helper":
                if not HELPER:
                    return self.send_json({"error": "not found"}, 404)
                return self.send_json(*helper_request(self.read_json()))
            if u.path == "/api/guide/chat":
                return self.send_json(*guide_chat(self.read_json()))
            if u.path == "/api/guide/skill":
                return self.send_json(*guide_skill(self.read_json()))
            if u.path == "/api/guide/revise":
                return self.send_json(*guide_revise(self.read_json()))
            self.send_json({"error": "not found"}, 404)
        except BrokenPipeError:
            pass
        except Exception as e:
            # E1: same as do_GET's catch-all -- full text to the log, a
            # plain sentence plus `detail` to the page.
            traceback.print_exc()
            self.send_json({"ok": False, "error": "Something went wrong handling that request.",
                            "detail": str(e)}, 500)

    def api_upload(self):
        """Browser -> us -> the target lane's POST /upload/image."""
        ctype = self.headers.get("Content-Type", "")
        m = re.search(r"boundary=([^;]+)", ctype)
        if not m:
            return self.send_json({"ok": False, "error": "expected multipart"}, 400)
        boundary = m.group(1).strip().strip('"').encode()
        parts = parse_multipart(self.read_body(), boundary)
        lane_id = ""
        files = []
        for p in parts:
            if p["name"] == "lane" and not p["filename"]:
                lane_id = p["data"].decode("utf-8", "replace").strip()
            elif p["filename"]:
                files.append(p)
        lane = LANE_BY_ID.get(lane_id)
        if not lane:
            return self.send_json({"ok": False, "error": "unknown lane %r" % lane_id}, 400)
        if lane_kind(lane) == "process":
            # No ComfyUI to hand this to: the file lands in the lane's own
            # uploads dir, where run_process_job() stages it. It is stored
            # under a unique, sanitized name -- never the raw basename -- so
            # a second upload of the same filename cannot clobber a file a
            # queued job is about to read.
            d = os.path.join(UPLOADS_DIR, lane["id"])
            os.makedirs(d, exist_ok=True)
            uploaded = []
            for p in files:
                name = os.path.basename(p["filename"] or "")
                safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)
                if not safe or safe in (".", ".."):
                    return self.send_json({"ok": False, "error": "that filename is not allowed"}, 400)
                stored = uuid.uuid4().hex[:8] + "_" + safe
                with open(os.path.join(d, stored), "xb") as f:
                    f.write(p["data"])
                uploaded.append({"name": stored, "original": p["filename"], "bytes": len(p["data"])})
            log("Kept %d file(s) for %s" % (len(uploaded), lane["name"]))
            return self.send_json({"ok": True, "files": uploaded})
        uploaded = []
        for p in files:
            res = http_post_multipart(
                lane_url(lane, "/upload/image"),
                {"type": "input", "overwrite": "false"},
                [("image", p["filename"], p["content_type"], p["data"])])
            if "_http_error" in res or not res.get("name"):
                return self.send_json({"ok": False, "error": "%s would not take %s. Try a smaller file or the other lane."
                                       % (lane["name"], p["filename"]), "detail": str(res)[:800]}, 502)
            name = res["name"]
            if res.get("subfolder"):
                name = res["subfolder"] + "/" + name
            uploaded.append({"name": name, "original": p["filename"], "bytes": len(p["data"])})
        log("Sent %d picture/clip(s) over to %s" % (len(uploaded), lane["name"]))
        return self.send_json({"ok": True, "files": uploaded})

    def api_generate(self):
        return self.send_json(*generate(self.read_json()))

    def api_chain(self):
        """WORKFLOW: carry a finished still from its lane straight onto a video
        lane's input dir. Separate ComfyUI instances do not share an input folder,
        so this is: GET /view on the source -> fit to the video aspect locally
        -> POST /upload/image on the target. The user never touches a file.
        A thin wrapper around carry() (see the internal sequence/storyboard design spec
        §2); its JSON response is byte-identical to before that split."""
        p = self.read_json()
        with JOBS_LOCK:
            job = dict(JOBS.get(p.get("job_id") or "", {}))
        if not job:
            return self.send_json({"ok": False, "error": "I cannot find that result any more."}, 404)
        src_lane = LANE_BY_ID.get(job["lane"])
        tgt_lane = LANE_BY_ID.get(p.get("target_lane"))
        if not src_lane or not tgt_lane:
            return self.send_json({"ok": False, "error": "unknown lane"}, 400)
        with STATE_LOCK:
            if not LANE_STATE.get(tgt_lane["id"], {}).get("up"):
                return self.send_json({"ok": False, "error": "%s is offline right now. Pick a lane glowing green." % tgt_lane["name"]}, 409)
        idx = int(p.get("output_index") or 0)
        vw = int(p.get("video_width") or 960)
        vh = int(p.get("video_height") or 544)

        try:
            name, note = carry(job, idx, tgt_lane, fit=(vw, vh))
        except CarryError as e:
            body = {"ok": False, "error": str(e)}
            if e.detail is not None:
                body["detail"] = e.detail
            return self.send_json(body, e.code)
        msg = "Carried the picture over to %s (%s)" % (tgt_lane["name"], note)
        log(msg, "chain")
        return self.send_json({"ok": True, "name": name, "note": note, "message": msg,
                               "source_prompt": job.get("prompt", ""), "seed": job.get("seed")})

    def api_cancel(self):
        """Ask a lane to drop its own pending/running item. This is ComfyUI's own
        /interrupt: it cancels a JOB, it does not touch the process."""
        p = self.read_json()
        lane = LANE_BY_ID.get(p.get("lane")) if isinstance(p, dict) and isinstance(p.get("lane"), str) else None
        if not lane:
            return self.send_json({"ok": False, "error": "unknown lane"}, 400)
        if lane_kind(lane) == "process":
            # There is no ComfyUI /interrupt here: the stop event is the
            # signal, and the runner kills the whole group it started. A
            # running job is marked by the worker once the program is gone.
            jid = p.get("job_id")
            stop = PROC_STOP.get(jid)
            if stop is not None:
                stop.set()
            with JOBS_LOCK:
                j = JOBS.get(jid)
                if j and j["status"] == "queued":
                    j["status"] = "error"
                    j["error"] = "you stopped this one"
                    j["updated"] = time.time()
            log("Stopped the job on %s" % lane["name"], "warn")
            return self.send_json({"ok": True})
        try:
            http_post_json(lane_url(lane, "/interrupt"), {}, timeout=10.0)
        except Exception as e:
            # E1: a network failure reaching the lane (a raw socket/urllib
            # exception) must not reach the page as Python's own text.
            log("Could not reach %s to stop that job: %s" % (lane["name"], str(e)[:300]), "warn")
            return self.send_json({"ok": False, "error": "Could not reach %s to stop that job." % lane["name"],
                                   "detail": str(e)}, 502)
        jid = p.get("job_id")
        with JOBS_LOCK:
            if jid and jid in JOBS and JOBS[jid]["status"] in ("queued", "running"):
                JOBS[jid]["status"] = "error"
                JOBS[jid]["error"] = "you stopped this one"
        log("Stopped the job running on %s" % lane["name"], "warn")
        return self.send_json({"ok": True})


    def api_forget(self):
        """Remove a finished result from the gallery. This drops OUR record of the job only:
        the picture or clip stays on the lane's own disk. This app never deletes your files."""
        p = self.read_json()
        jid = p.get("job_id")
        # SEQ_LOCK across the check AND the pop, so no sequence can start
        # naming this job in between. A job a sequence uses is refused.
        with SEQ_LOCK:
            with JOBS_LOCK:
                j = JOBS.get(jid)
                if not j:
                    return self.send_json({"ok": False, "error": "no such result"}, 404)
                if j.get("status") in ("queued", "running"):
                    return self.send_json({"ok": False, "error": "that one is still going, stop it first"}, 409)
            used = seq_job_use(jid)
            if used:
                return self.send_json({"ok": False, "error": used}, 409)
            with JOBS_LOCK:
                JOBS.pop(jid, None)
        save_jobs()
        return self.send_json({"ok": True})


def main():
    # Bind first, before starting any threads: if the port is taken there is no
    # point polling seven lanes, and a stack trace is a poor way to say
    # "something else is already using this port".
    try:
        srv = ThreadingHTTPServer((BIND, PORT), Handler)
    except OSError as e:
        if getattr(e, "errno", None) in (48, 98):   # EADDRINUSE on BSD / Linux
            die("Port %d is already in use.\n\n"
                "Either this app is already running, or something else has the port.\n"
                "Find it with:  lsof -nP -iTCP:%d -sTCP:LISTEN\n"
                "Or pick another port by changing \"port\" in %s."
                % (PORT, PORT, os.path.basename(CONFIG_FILE)))
        if getattr(e, "errno", None) == 49:         # EADDRNOTAVAIL
            die("Cannot bind to %r. Check \"bind\" in %s: use \"0.0.0.0\" for every\n"
                "interface, or \"127.0.0.1\" for this machine only."
                % (BIND, os.path.basename(CONFIG_FILE)))
        raise
    srv.daemon_threads = True

    load_jobs()
    seq_mark_interrupted_cuts()
    for l in LANES:
        threading.Thread(target=lane_poller, args=(l,), daemon=True).start()
        if lane_kind(l) != "process":
            threading.Thread(target=ws_listener, args=(l,), daemon=True).start()
    threading.Thread(target=job_poller, daemon=True).start()
    if FLEET_LLM:
        threading.Thread(target=fleet_poller, daemon=True).start()
    log("%s is up on http://%s:%d" % (TITLE, BIND, PORT), "ok")
    # Basename only: this log is shown in the browser, and a full path on screen
    # is how a home directory ends up in someone's screenshot.
    log("Loaded %d lane(s) from %s" % (len(LANES), os.path.basename(CONFIG_FILE)))
    print("Config file: %s" % CONFIG_FILE, flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
