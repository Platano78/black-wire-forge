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
import ipaddress
import json
import math
import mimetypes
import os
import queue
import random
import re
import shutil
import socket
import stat
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
import webbrowser
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
import forge_run
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
GUIDE_HIST_DIR = os.path.join(DATA_DIR, "guide_history")   # one <key>.json per guide conversation
CONFIG_FILE = os.environ.get("GENCENTER_CONFIG") or os.path.join(APP_DIR, "config.json")

MODEL_KEYS = engines.model_keys()   # whatever the installed packs declare
# The only things a lane genuinely cannot be guessed from. Everything else has
# a safe default, because "put in your lane addresses and run it" is the point.
LANE_KEYS = ("id", "name", "host", "port")


def die(msg):
    sys.stderr.write("\n" + msg.rstrip() + "\n\n")
    raise SystemExit(2)


class ConfigError(Exception):
    """A config that load_config() refuses, with the sentence to say why."""


def load_config():
    """Read config.json, or explain exactly what to do instead of crashing.
    Only called when the file exists: a missing one is Setup mode (below)."""
    try:
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
    except ValueError as e:
        die("%s is not valid JSON: %s\n"
            "Tip: JSON has no comments and no trailing commas." % (CONFIG_FILE, e))
    try:
        return validate_config(cfg)
    except ConfigError as e:
        die(str(e))


def validate_config(cfg):
    """Every rule a config must meet -> cfg (defaults filled in), or raises
    ConfigError. Shared by load_config() and Setup's writer, so Setup can
    never write a file the next start would refuse."""
    if not isinstance(cfg, dict):
        raise ConfigError("%s must contain a JSON object." % CONFIG_FILE)

    lanes = cfg.get("lanes")
    if not isinstance(lanes, list) or not lanes:
        raise ConfigError("%s needs a non-empty \"lanes\" list -- one entry per ComfyUI instance." % CONFIG_FILE)
    seen = set()
    for i, lane in enumerate(lanes):
        if not isinstance(lane, dict):
            raise ConfigError("lanes[%d] in %s must be an object." % (i, CONFIG_FILE))
        kind = lane.get("kind") or "comfy"
        if kind not in ("comfy", "process"):
            raise ConfigError("lanes[%d] (%s): \"kind\" must be \"comfy\" or \"process\"."
                % (i, lane.get("id", "no id")))
        lane["kind"] = kind
        # A process lane runs programs on the box this app is on: it has no
        # host or port of its own, so only an id and a name are required.
        missing = [k for k in (("id", "name") if kind == "process" else LANE_KEYS)
                   if k not in lane]
        if missing:
            raise ConfigError("lanes[%d] (%s) in %s is missing: %s"
                % (i, lane.get("id", "no id"), CONFIG_FILE, ", ".join(missing)))
        if lane["id"] in seen:
            raise ConfigError("Two lanes share the id %r in %s. Lane ids must be unique."
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
                raise ConfigError("lanes[%d] (%s): a process lane must say which caps it is\n"
                    "offered for, e.g. \"caps\": [\"3d\"]." % (i, lane["id"]))
        else:
            caps = lane.setdefault("caps", list(engines.caps()))
        if not isinstance(caps, list) or not caps or set(caps) - set(engines.caps()):
            raise ConfigError("lanes[%d] (%s): \"caps\" must be a non-empty list drawn from %s,\n"
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
                raise ConfigError("lanes[%d] (%s): \"port\" must be a number." % (i, lane["id"]))
            # No "gpu" given? Assume every lane on the same host shares one
            # card. That is the conservative guess: it may free weights that
            # did not need freeing (costing a reload), where the opposite
            # mistake is an out-of-memory crash. Set "gpu" explicitly on a
            # multi-GPU box.
            lane.setdefault("gpu", lane["host"])
            lane.setdefault("gpu_label", lane.get("box") or lane["host"])
        if lane.get("models") is not None and not isinstance(lane["models"], dict):
            raise ConfigError("lanes[%d] (%s): \"models\" must be an object if present." % (i, lane["id"]))
        # LORA-1 Build C: downloads are OPT-IN PER LANE (owner ruling
        # 2026-09-28) -- absent by default, so a fresh config downloads
        # nothing. Present, it must at least name where a LoRA file lands.
        dl = lane.get("downloads")
        if dl is not None:
            if not isinstance(dl, dict) or not dl.get("loras_dir"):
                raise ConfigError("lanes[%d] (%s): \"downloads\" must be an object with at least "
                    "\"loras_dir\" (an absolute path) if present." % (i, lane["id"]))
            if not os.path.isabs(dl["loras_dir"]):
                raise ConfigError("lanes[%d] (%s): \"downloads\".\"loras_dir\" must be an absolute path."
                    % (i, lane["id"]))
            mb = dl.get("max_bytes")
            if mb is not None and (not isinstance(mb, int) or isinstance(mb, bool) or mb <= 0):
                raise ConfigError("lanes[%d] (%s): \"downloads\".\"max_bytes\" must be a positive whole "
                    "number of bytes if present." % (i, lane["id"]))
        # UX-2 #5: "Remove also deletes the file" is OPT-IN PER LANE, same
        # shape as "downloads" above -- absent by default, so a fresh
        # config deletes nothing (AGENTS.md's "this app never deletes your
        # files" stays the default; this is the one deliberate exception,
        # and only once the owner points it at the lane's own output dir).
        outs = lane.get("outputs")
        if outs is not None:
            if not isinstance(outs, dict) or not outs.get("dir"):
                raise ConfigError("lanes[%d] (%s): \"outputs\" must be an object with at least "
                    "\"dir\" (an absolute path) if present." % (i, lane["id"]))
            if not os.path.isabs(outs["dir"]):
                raise ConfigError("lanes[%d] (%s): \"outputs\".\"dir\" must be an absolute path."
                    % (i, lane["id"]))

    # "models" is OPTIONAL. Model filenames are discovered from each lane at
    # runtime; anything named here simply overrides what was found. Only the
    # keys in MODEL_KEYS mean anything, so typos are worth catching early.
    models = cfg.get("models")
    if models is not None:
        if not isinstance(models, dict):
            raise ConfigError("\"models\" in %s must be an object (or left out: filenames are\n"
                "discovered from each lane automatically)." % CONFIG_FILE)
        unknown = [k for k in models if k not in MODEL_KEYS]
        if unknown:
            raise ConfigError("\"models\" in %s has key(s) this app does not use: %s\n"
                "Valid keys: %s" % (CONFIG_FILE, ", ".join(unknown), ", ".join(MODEL_KEYS)))

    # "helper" (L5) is OPTIONAL: an OpenAI-compatible chat endpoint for "Help
    # me write this" / "Describe this picture". Absent -> the feature does
    # not exist (no route, no button). The core never names the model --
    # that lives here, in the operator's own config.
    helper = cfg.get("helper")
    if helper is not None and (not isinstance(helper, dict) or not helper.get("url")):
        raise ConfigError("\"helper\" in %s must be an object with at least a \"url\" "
            "(ending in \"/v1\")." % CONFIG_FILE)
    ctx = helper.get("context") if helper is not None else None
    if ctx is not None and (not isinstance(ctx, int) or isinstance(ctx, bool) or ctx <= 0):
        raise ConfigError("\"helper\".\"context\" in %s must be a whole number of tokens (the context "
            "the helper model actually serves), e.g. 16384." % CONFIG_FILE)
    vision = helper.get("vision") if helper is not None else None
    if vision is not None and not isinstance(vision, bool):
        raise ConfigError("\"helper\".\"vision\" in %s must be true or false (whether the helper model "
            "can see pictures)." % CONFIG_FILE)
    max_images = helper.get("max_images") if helper is not None else None
    if max_images is not None and (not isinstance(max_images, int) or isinstance(max_images, bool)
                                   or max_images < 1):
        raise ConfigError("\"helper\".\"max_images\" in %s must be a whole number, 1 or more (how many "
            "pictures one guide message may carry)." % CONFIG_FILE)
    max_tokens = helper.get("max_tokens") if helper is not None else None
    if max_tokens is not None and (not isinstance(max_tokens, int) or isinstance(max_tokens, bool)
                                   or max_tokens < 1):
        raise ConfigError("\"helper\".\"max_tokens\" in %s must be a whole number, 1 or more (the fewest tokens "
            "every guide reply may use; a model that thinks first needs room for it), e.g. 8192." % CONFIG_FILE)
    return cfg


# W1 Setup mode: no config file yet -> instead of exiting, serve only the
# Setup page (setup.html) and /api/setup/*, which ask a few questions and
# write config.json. A synthetic config with no lanes and no helper; bound to
# this machine only, whatever the defaults say, so a first run never exposes
# the box. A config that exists but is broken still exits (load_config).
SETUP_MODE = not os.path.exists(CONFIG_FILE)
CONFIG = {"lanes": [], "bind": "127.0.0.1"} if SETUP_MODE else load_config()

PORT = 3998 if SETUP_MODE else int(CONFIG.get("port", 3998))
BIND = "127.0.0.1" if SETUP_MODE else CONFIG.get("bind", "0.0.0.0")
# B1: extra hostnames the Host check accepts ("allowed_hosts": ["forge.lan",
# "studio.local:3998"]). Optional; the guard works without it.
ALLOWED_HOSTS = [str(h).strip().lower() for h in (CONFIG.get("allowed_hosts") or [])
                 if str(h).strip()]
TITLE = CONFIG.get("title", "Black Wire Forge")

os.makedirs(CHAIN_DIR, exist_ok=True)
os.makedirs(LOCAL_OUTPUTS_DIR, exist_ok=True)
os.makedirs(SEQ_DIR, exist_ok=True)
os.makedirs(GUIDE_HIST_DIR, exist_ok=True)

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
# installed on the machine that runs the app, plus the macOS and Windows
# equivalents, which are simply skipped when absent. config.cut.fontfile,
# when set, always wins.
_DEFAULT_CUT_FONTS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
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

# Optional speech source (off by default). Omit "speech" from config.json and
# /api/speech 404s, /api/speech/status reports enabled: false, and nothing
# else changes. With a "speech" object containing a valid https url, the
# endpoint calls that OpenAI-style /audio/speech and returns an upload shaped
# exactly like /api/upload.
_SPEECH_RAW = CONFIG.get("speech")
SPEECH_ENABLED = False
SPEECH_URL = None
SPEECH_MODEL = "tts-1"
SPEECH_VOICE = "alloy"
SPEECH_API_KEY = None
SPEECH_TIMEOUT = 120.0
if isinstance(_SPEECH_RAW, dict):
    raw_url = _SPEECH_RAW.get("url")
    if isinstance(raw_url, str):
        parsed = urllib.parse.urlsplit(raw_url)
        if parsed.scheme in ("http", "https"):
            SPEECH_ENABLED = True
            SPEECH_URL = raw_url.rstrip("/")
            SPEECH_MODEL = str(_SPEECH_RAW.get("model", "tts-1")) or "tts-1"
            SPEECH_VOICE = str(_SPEECH_RAW.get("voice", "alloy")) or "alloy"
            api_key_env = _SPEECH_RAW.get("api_key_env")
            if isinstance(api_key_env, str) and api_key_env:
                SPEECH_API_KEY = os.environ.get(api_key_env)
            try:
                t = float(_SPEECH_RAW.get("timeout", 120))
            except Exception:
                t = 120.0
            SPEECH_TIMEOUT = max(5.0, min(600.0, t))

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
# B2: guide conversation history (data/guide_history/<key>.json) -- plain-room
# conversations have no sequence, so this is its own lock, not SEQ_LOCK.
GUIDE_HIST_LOCK = threading.Lock()

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
# LORA-1 Build B/C: a browsable Hugging Face Hub catalog of style LoRAs for
# the lane's picture model, and a guarded download of one file into it.
#
# Verified fact (orchestrator, this session): GET https://huggingface.co/api/
# models?filter=base_model:adapter:<base>&sort=downloads&limit=50 lists the
# adapters for <base>, no key needed. That listing carries no file sizes and
# no cardData.license (checked live 2026-09-28); GET /api/models/<id>?
# blobs=true does, for one repo at a time -- so a catalog build is one list
# call plus one detail call per repo it returns, cached below.
# ---------------------------------------------------------------------------

# Test-only override (never read outside this): points the CATALOG's list/
# detail calls at a local fixture server instead of the real Hub, so a UI
# suite can drive a real "Browse styles" render with no live network. The
# DOWNLOAD url below is never affected -- it always resolves against the
# real huggingface.co host, per Build C's own rule.
HF_API = os.environ.get("BWF_TEST_HF_API") or "https://huggingface.co/api/models"
# LORA-2B: the README-fetching web host (raw model-card text, never the
# download itself -- that always resolves against the real huggingface.co,
# per Build C's own rule above _allowed_download_host).
HF_WEB = os.environ.get("BWF_TEST_HF_WEB") or "https://huggingface.co"
CATALOG_CACHE_SECONDS = 600
CATALOG_LOCK = threading.Lock()
CATALOG_CACHE = {}   # base_id -> (fetched_at, [entry, ...])
README_CACHE_SECONDS = 600
README_MAX_BYTES = 256 * 1024
README_LOCK = threading.Lock()
README_CACHE = {}   # repo_id -> (fetched_at, {description, trigger_words, strength, preview})
_NSFW_RE = re.compile(r"nsfw|nude", re.IGNORECASE)
DOWNLOAD_DEFAULT_MAX_BYTES = 4 * 1024 ** 3
DOWNLOAD_LOCK = threading.Lock()
DOWNLOADS = {}        # lane_id -> {"repo","file","bytes","total","done","ok","error","cancel"}
REPO_ID_RE = re.compile(r"^[\w.-]+/[\w.-]+$")


def _style_family_adapter(sc):
    """Adapt one pack's "style_catalog" declaration to the v2 per-family
    shape LORA-2B needs (id/label/cap/modes/folder), so this module keeps
    working both before and after slice A lands the v2 shape (spec: "a
    tiny adapter ... accepts BOTH the old and the v2 shape"). The base
    commit's shape (still what the one picture pack declares) carries only
    role/match/hf_base -- give it a stable single-family id and an empty
    folder, which keeps today's flat <loras_dir>/<file> layout unchanged."""
    if "id" in sc:
        return sc
    out = dict(sc)
    out.setdefault("id", "default")
    out.setdefault("label", sc.get("hf_base", "Styles"))
    out.setdefault("cap", "image")
    out.setdefault("modes", [])
    out.setdefault("folder", "")
    return out


# Test-only override (never read outside this, same pattern as BWF_TEST_HF_API
# above): a JSON list of already-v2-shaped families, for a browser-driven UI
# suite to exercise multiple families/tabs against a REAL server subprocess
# (which can't be monkeypatched in-process) without waiting on slice A's real
# engine packs.
_TEST_STYLE_FAMILIES = os.environ.get("BWF_TEST_STYLE_FAMILIES")


def style_families():
    """engines.style_catalogs(), normalised to the v2 per-family shape --
    call this everywhere in server.py instead of engines.style_catalogs()
    directly, so the old/new-shape adapter above is applied exactly once."""
    if _TEST_STYLE_FAMILIES:
        return json.loads(_TEST_STYLE_FAMILIES)
    return [_style_family_adapter(sc) for sc in engines.style_catalogs()]


def _resolved_family(lane, family_id):
    """The style family for `family_id` on this lane -- refusing one that
    is declared but not PRESENT here (LORA-2E #2: its model must actually
    be discovered on `lane`, not merely declared somewhere in the catalog;
    LORA-2B's older "declared at all" check let a family from a DIFFERENT
    lane's models through). `family_id` empty defaults to the lane's only
    present family, same as before. Returns the family dict, or None when
    nothing can be resolved."""
    present = _families_for_lane(models_for(lane))
    if not family_id:
        return present[0] if len(present) == 1 else None
    return next((f for f in present if f["id"] == family_id), None)


def _families_for_lane(m):
    """Every style family whose role model this lane has discovered and
    whose match rule's POSITIVE terms (any/all) that model's filename
    satisfies -- the families "present" on this lane, in style_families()
    order. Deliberately ignores the match rule's own "none" list here: that
    exclusion exists to keep speed/distillation LoRA files out of the
    "Browse styles" catalog, not to judge the base model itself -- a
    legitimately-named distilled/speed-tuned BASE CHECKPOINT is not a LoRA
    and must still count as present (live-rig defect 2026-09-28: two real
    families vanished from a real lane's catalog because their own base
    model filenames tripped their own "none" list). This core names no
    model itself -- it only asks each pack's OWN declaration, minus the
    one field it must never apply here."""
    out = []
    for sc in style_families():
        name = (m.get(sc["role"]) or "").lower()
        if not name:
            continue
        rule = sc.get("match") or {}
        positive_only = {k: v for k, v in rule.items() if k in ("any", "all")}
        if _rule_matches([name], positive_only):
            out.append(sc)
    return out


def _readable_pack_name(repo_id):
    """A repo id's name-half turned into a readable name: '-'/'_' to
    spaces, Title Case for a plain-lowercase word (a word that already
    carries a capital -- an acronym, a CamelCase term -- is left exactly
    as the card wrote it, so 'XL' never becomes 'Xl')."""
    slug = repo_id.split("/", 1)[1] if "/" in repo_id else repo_id
    words = [w for w in re.split(r"[-_]+", slug) if w]
    return " ".join(w if any(c.isupper() for c in w) else w.capitalize() for w in words) or slug


def _pack_author(repo_id):
    return repo_id.split("/", 1)[0] if "/" in repo_id else ""


# ---------------------------------------------------------------------------
# LORA-2B: a tolerant, regex-only reading of a model card's README.md --
# never a real markdown/YAML parser (neither ships with the stdlib, and this
# app runs with no required third-party packages -- see requirements.txt).
# Every extractor below fails soft: a card that doesn't match its pattern
# yields None, never an exception that could take the whole catalog list
# down with it (LORA-2B spec: "failures -> fallback text, never an error for
# the whole list").
# ---------------------------------------------------------------------------

_FRONT_MATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_MD_HEADING_RE = re.compile(r"^#{1,6}\s")
_MD_IMAGE_RE = re.compile(r"^!\[")
_MD_HTML_RE = re.compile(r"^<")
_MD_LINK_ONLY_RE = re.compile(r"^\[[^\]]*\]\([^)]*\)\.?$")
_MD_BADGE_RE = re.compile(r"shields\.io|badge\.fury|img\.shields", re.IGNORECASE)
_MD_RULE_RE = re.compile(r"^[-*_]{3,}$")
_MD_LINK_INLINE_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_EMPHASIS_RE = re.compile(r"(\*\*\*|\*\*|\*|___|__|_|`)")


def _strip_front_matter(text):
    """(body, front_matter_yaml_text) -- front_matter_yaml_text is "" when
    the README has none."""
    m = _FRONT_MATTER_RE.match(text or "")
    return (text[m.end():], m.group(1)) if m else (text or "", "")


def _strip_markdown_inline(line):
    line = _MD_LINK_INLINE_RE.sub(r"\1", line)
    line = _MD_EMPHASIS_RE.sub("", line)
    return line.strip()


def _is_noise_line(line):
    """A line that is never itself prose -- a heading, an image, raw HTML,
    a link-only line, a badge, or a markdown rule. NOT a blockquote/table
    line: real cards put trigger words and specs inside those."""
    return bool(_MD_HEADING_RE.match(line) or _MD_IMAGE_RE.match(line) or _MD_HTML_RE.match(line)
                or _MD_LINK_ONLY_RE.match(line) or _MD_BADGE_RE.search(line) or _MD_RULE_RE.match(line))


def _sentence_from(text):
    """The first sentence in `text` (a joined paragraph), capped at 160
    chars and cut at a word boundary with an ellipsis."""
    m = re.search(r".+?[.!?](?=\s|$)", text)
    sentence = m.group(0) if m else text
    if len(sentence) > 160:
        cut = sentence[:160].rsplit(" ", 1)[0].rstrip(".,;:- ")
        sentence = (cut or sentence[:160]) + "…"
    return sentence


def _first_prose_sentence(readme_text):
    """LORA-2B: the model card's first real prose sentence. Front-matter,
    headings, badges, images and HTML are skipped; consecutive prose lines
    are joined into one paragraph first (a card that hard-wraps its first
    paragraph across several lines must not truncate mid-sentence). A
    trigger/activation/instance-prompt line is skipped too (live-rig
    defect 2026-09-28: H3 Realism's description came out as "Trigger word:
    r34l1sm" -- that line feeds trigger_words, via _extract_trigger_words'
    own _TRIGGER_LINE_RE below, reused here so the two stay in step; the
    NEXT real prose sentence after it is what description wants). None
    when the card has no prose line at all."""
    body, _fm = _strip_front_matter(readme_text)
    paragraph = []
    for raw_line in body.splitlines() + [""]:
        line = raw_line.strip()
        if not line or (line[:1] in (">", "|")) or _is_noise_line(line) or _TRIGGER_LINE_RE.search(line):
            if paragraph:
                text = _strip_markdown_inline(" ".join(paragraph))
                paragraph = []
                if text:
                    return _sentence_from(text)
            continue
        paragraph.append(line)
    return None


# LORA-2E #3: widened past "trigger word"/"activation" alone -- also
# "trigger prompt", a bare "trigger:" label, "activation word/text/prompt/
# TOKEN(s)", and "instance prompt" (the DreamBooth/PEFT term for the same
# thing). "trigger\s*:" needs trigger immediately followed by the colon (no
# word in between) so it only catches the bare label, not "trigger word:"
# (already matched by the first alternative).
_TRIGGER_LINE_RE = re.compile(
    r"trigger\s*word|trigger\s*prompt|trigger\s*:|activation|instance\s*prompt", re.IGNORECASE)
_TRIGGER_LABEL_RE = re.compile(
    r"trigger\s*words?\s*:?|trigger\s*prompts?\s*:?|trigger\s*:?|"
    r"activation\s*(word|text|prompt|token)s?\s*:?|instance\s*prompts?\s*:?",
    re.IGNORECASE)
# The README front-matter's own `instance_prompt:` key (DreamBooth/PEFT
# convention) -- checked ahead of the body scan since it's the most
# structured, unambiguous source when the card sets it.
_INSTANCE_PROMPT_FM_RE = re.compile(r"^instance_prompt\s*:\s*(.*)$", re.IGNORECASE | re.MULTILINE)


def _extract_trigger_words(readme_text):
    """LORA-2B (+ LORA-2E #3 widening): a short trigger-word/activation
    line, kept short. A backtick-quoted term (how most cards actually write
    it) wins over the whole sentence around it. The front-matter
    `instance_prompt:` key, when set to a real value (not null/empty), wins
    over the body scan entirely."""
    body, fm = _strip_front_matter(readme_text)
    fm_match = _INSTANCE_PROMPT_FM_RE.search(fm or "")
    if fm_match:
        value = fm_match.group(1).strip().strip("'\"")
        if value and value.lower() not in ("null", "none", "~"):
            return value[:160]
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or _MD_HEADING_RE.match(line) or not _TRIGGER_LINE_RE.search(line):
            continue
        backticked = re.findall(r"`([^`]{1,60})`", line)
        if backticked:
            return ", ".join(dict.fromkeys(backticked))[:160]
        text = line.lstrip(">*- ")
        text = text.split(":", 1)[1] if ":" in text else text
        text = _strip_markdown_inline(text)
        text = _TRIGGER_LABEL_RE.sub("", text, count=1).strip(" -:>*")
        if text:
            return text[:160]
    return None


_STRENGTH_RE = re.compile(r"strength[^0-9]{0,12}(\d(?:\.\d+)?)", re.IGNORECASE)


def _extract_strength(readme_text):
    body, _fm = _strip_front_matter(readme_text)
    m = _STRENGTH_RE.search(body)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


_WIDGET_SECTION_RE = re.compile(r"^widget\s*:\s*$")
_URL_LINE_RE = re.compile(r"^-?\s*url\s*:\s*(\S+)\s*$")


def _extract_preview_image(readme_text, repo_id):
    """A preview image URL, ONLY when the front matter names one under a
    "widget: / output: / url:" block, hosted on huggingface.co (LORA-2B
    spec: "skip otherwise" -- never a guess, never a body-text image)."""
    _body, fm = _strip_front_matter(readme_text)
    if not fm:
        return None
    in_widget = False
    for raw_line in fm.splitlines():
        stripped = raw_line.strip()
        if _WIDGET_SECTION_RE.match(stripped):
            in_widget = True
            continue
        if not in_widget:
            continue
        if not raw_line[:1].isspace() and raw_line[:1] != "-":
            in_widget = False   # back to column 0: the widget block ended
            continue
        m = _URL_LINE_RE.match(stripped)
        if m:
            return _resolve_preview_url(m.group(1).strip("'\""), repo_id)
    return None


def _resolve_preview_url(url, repo_id):
    if url.startswith("http://") or url.startswith("https://"):
        full = url
    else:
        full = "https://huggingface.co/%s/resolve/main/%s" % (repo_id, url.lstrip("/"))
    u = urllib.parse.urlparse(full)
    if u.scheme != "https" or not _allowed_download_host(u.hostname):
        return None
    return full


def _fetch_readme_text(repo_id):
    req = urllib.request.Request("%s/%s/raw/main/README.md" % (HF_WEB, repo_id))
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return r.read(README_MAX_BYTES).decode("utf-8", "replace")


def _readme_extras(repo_id):
    """Cached (10 min, same window as the catalog) description/trigger/
    strength/preview for one repo's README. A fetch failure never fails
    the whole catalog list -- every field just falls back to None/the
    caller's own fallback text."""
    now = time.time()
    with README_LOCK:
        cached = README_CACHE.get(repo_id)
    if cached and now - cached[0] < README_CACHE_SECONDS:
        return cached[1]
    extras = {"description": None, "trigger_words": None, "strength": None, "preview": None}
    try:
        text = _fetch_readme_text(repo_id)
        extras["description"] = _first_prose_sentence(text)
        extras["trigger_words"] = _extract_trigger_words(text)
        extras["strength"] = _extract_strength(text)
        extras["preview"] = _extract_preview_image(text, repo_id)
    except Exception:
        pass
    with README_LOCK:
        README_CACHE[repo_id] = (now, extras)
    return extras


def _build_catalog(base_id):
    listing = http_get_json(
        "%s?filter=base_model:adapter:%s&sort=downloads&limit=50"
        % (HF_API, urllib.parse.quote(base_id, safe="")), timeout=HTTP_TIMEOUT) or []
    out = []
    for item in listing:
        repo_id = item.get("id") or item.get("modelId")
        if not repo_id or not REPO_ID_RE.match(repo_id):
            continue
        tags = item.get("tags") or []
        nsfw = ("not-for-all-audiences" in tags) or bool(_NSFW_RE.search(repo_id))
        try:
            detail = http_get_json(
                "%s/%s?blobs=true" % (HF_API, urllib.parse.quote(repo_id, safe="/")),
                timeout=HTTP_TIMEOUT) or {}
        except Exception:
            detail = {}
        licence = (detail.get("cardData") or {}).get("license")
        files = [{"filename": s["rfilename"], "size": s.get("size")}
                 for s in (detail.get("siblings") or [])
                 if isinstance(s.get("rfilename"), str) and s["rfilename"].endswith(".safetensors")]
        card_summary = (detail.get("cardData") or {}).get("summary") or item.get("description")
        extras = _readme_extras(repo_id)
        out.append({"id": repo_id, "name": _readable_pack_name(repo_id), "author": _pack_author(repo_id),
                    "downloads": item.get("downloads", 0), "likes": item.get("likes", 0), "licence": licence,
                    "nsfw": nsfw, "files": files,
                    "description": extras["description"] or card_summary or "No description on the model card.",
                    "trigger_words": extras["trigger_words"], "strength": extras["strength"],
                    "preview": extras["preview"]})
    return out


def catalog_for(base_id):
    """The cached catalog for one HF base model id, rebuilt at most every
    CATALOG_CACHE_SECONDS."""
    now = time.time()
    with CATALOG_LOCK:
        cached = CATALOG_CACHE.get(base_id)
    if cached and now - cached[0] < CATALOG_CACHE_SECONDS:
        return cached[1]
    data = _build_catalog(base_id)
    with CATALOG_LOCK:
        CATALOG_CACHE[base_id] = (now, data)
    return data


class DownloadsOff(ValueError):
    """Security review Finding 4: the ONE refusal in Build C that must
    surface as 403, not 400 (spec section C's last bullet). A ValueError
    subclass so existing `except ValueError` callers still catch it if they
    don't care about the distinction; callers that DO care catch this
    first."""
    pass


# Adversarial review 2026-09-28: the generic reader now validates data_offsets
# itself (non-negative, start<=end<=data section length, no overlap between
# tensors) and cross-checks byte length against shape*dtype-size -- so it
# needs a dtype-size table of its own (a safetensors FORMAT fact, the same
# one engines/audio.py's own table names; not engine-specific, so this stays
# in the engine-agnostic core rather than importing a pack's table).
_SAFETENSORS_DTYPE_SIZE = {
    "F64": 8, "F32": 4, "F16": 2, "BF16": 2,
    "I64": 8, "I32": 4, "I16": 2, "I8": 1, "U8": 1, "BOOL": 1,
}
_SAFETENSORS_MAX_HEADER_BYTES = 64 * 1024 * 1024   # sanity cap, independent of the download size cap


def _read_safetensors_file(path):
    """Read a safetensors file into an ordered dict of
    {key: {"dtype": str, "shape": list[int], "data": bytes}} -- stdlib
    only, no safetensors/torch dependency (LORA-2E #1: "no new heavy
    dependency"). Raises ValueError on anything that doesn't parse as a
    valid safetensors header/body, including a malformed/malicious
    data_offsets pair (negative, reversed, past the file, or overlapping
    another tensor's bytes) or a byte length that doesn't match its own
    declared shape/dtype -- never a partial or wrong-region read."""
    with open(path, "rb") as f:
        header_len_bytes = f.read(8)
        if len(header_len_bytes) != 8:
            raise ValueError("Not a valid safetensors file (short header).")
        header_len = struct.unpack("<Q", header_len_bytes)[0]
        if header_len > _SAFETENSORS_MAX_HEADER_BYTES:
            raise ValueError("Not a valid safetensors file (header too large: %d bytes)." % header_len)
        header_json = f.read(header_len)
        if len(header_json) != header_len:
            raise ValueError("Not a valid safetensors file (truncated header).")
        try:
            header = json.loads(header_json)
        except (ValueError, UnicodeDecodeError):
            raise ValueError("Not a valid safetensors file (bad header JSON).")
        header.pop("__metadata__", None)
        data_start = 8 + header_len
        data_len = os.fstat(f.fileno()).st_size - data_start
        entries = _validate_safetensors_entries(header, data_len)
        tensors = {}
        for start, end, key, shape, dtype in entries:
            f.seek(data_start + start)
            data = f.read(end - start)
            if len(data) != end - start:
                raise ValueError("Not a valid safetensors file (truncated tensor %r)." % key)
            tensors[key] = {"dtype": dtype, "shape": shape, "data": data}
    return tensors


def _is_plain_int(x):
    return isinstance(x, int) and not isinstance(x, bool)


def _validate_safetensors_entries(header, data_len):
    """Every tensor entry in a parsed safetensors `header`, validated and
    returned as [(start, end, key, shape, dtype), ...] -- refuses (plain
    ValueError) a non-integer/negative/reversed/out-of-file data_offsets
    pair, an unrecognised dtype, a byte length that doesn't match
    shape*dtype-size, or two tensors whose byte ranges overlap."""
    entries = []
    for key, info in header.items():
        shape = info.get("shape") if isinstance(info, dict) else None
        offsets = info.get("data_offsets") if isinstance(info, dict) else None
        dtype = info.get("dtype") if isinstance(info, dict) else None
        if not (isinstance(shape, list) and all(_is_plain_int(d) and d >= 0 for d in shape)
                and isinstance(offsets, list) and len(offsets) == 2
                and all(_is_plain_int(o) for o in offsets)):
            raise ValueError("Not a valid safetensors file (bad tensor entry %r)." % key)
        start, end = offsets
        if start < 0 or end < 0 or start > end:
            raise ValueError("Not a valid safetensors file (invalid data_offsets for %r)." % key)
        if end > data_len:
            raise ValueError("Not a valid safetensors file (data_offsets for %r reach past the file)." % key)
        elemsize = _SAFETENSORS_DTYPE_SIZE.get(dtype)
        if elemsize is None:
            raise ValueError("Not a valid safetensors file (unsupported dtype %r for %r)." % (dtype, key))
        expect_len = elemsize
        for d in shape:
            expect_len *= d
        if end - start != expect_len:
            raise ValueError("Not a valid safetensors file (%r's byte length doesn't match its shape/dtype)." % key)
        entries.append((start, end, key, shape, dtype))
    max_end_so_far = -1
    for start, end, key, _shape, _dtype in sorted(entries, key=lambda e: (e[0], e[1])):
        if start < max_end_so_far:
            raise ValueError("Not a valid safetensors file (%r overlaps another tensor's bytes)." % key)
        max_end_so_far = max(max_end_so_far, end)
    return entries


def _write_safetensors_file(path, tensors):
    """Write `tensors` ({key: {"dtype","shape","data"}}) as a safetensors
    file, the same on-disk shape _read_safetensors_file() reads back --
    stdlib only. Builds the whole file in memory first, so a caller writing
    to a temp path never leaves a half-written file behind on failure."""
    header = {}
    offset = 0
    buffers = []
    for key, t in tensors.items():
        n = len(t["data"])
        header[key] = {"dtype": t["dtype"], "shape": list(t["shape"]), "data_offsets": [offset, offset + n]}
        offset += n
        buffers.append(t["data"])
    header_bytes = json.dumps(header).encode("utf-8")
    # Real safetensors files pad the header with trailing spaces to an
    # 8-byte boundary so the data section starts aligned; not every reader
    # requires it, but this does it too so a converted file looks ordinary.
    header_bytes += b" " * ((-len(header_bytes)) % 8)
    with open(path, "wb") as f:
        f.write(struct.pack("<Q", len(header_bytes)))
        f.write(header_bytes)
        for b in buffers:
            f.write(b)


def _convert_lora_file(convert_name, src_path, dest_path):
    """LORA-2E #1: run the pack-declared conversion `convert_name` against
    a just-downloaded LoRA file, writing the result to `dest_path` (never
    `src_path` itself -- the caller swaps it in only once this returns
    without raising). The core here knows nothing about what `convert_name`
    does or which model it is for -- it only asks engines.lora_converter()
    for the function a pack registered under that name, and raises
    ValueError (the original file stays untouched) if none is registered
    or the conversion itself refuses."""
    convert = engines.lora_converter(convert_name)
    if convert is None:
        raise ValueError("No LoRA converter named %r is registered." % convert_name)
    tensors = _read_safetensors_file(src_path)
    converted = convert(tensors)
    _write_safetensors_file(dest_path, converted)


def _lora_download_target(lane, family_id, repo, filename):
    """Build C (+ LORA-2B's per-family folder): every download-safety rule
    in one place. Returns (url, dest, max_bytes) or raises ValueError
    (DownloadsOff for the opt-in check) with the plain sentence to refuse
    with. `family_id` may be None/absent when exactly one style family is
    present on this lane (keeps the pre-LORA-2B single-family call shape
    working with no family named)."""
    dl = lane.get("downloads")
    if not isinstance(dl, dict) or not dl.get("loras_dir"):
        raise DownloadsOff("Downloads are off for %s. Set \"downloads\" on this lane in "
                           "config.json to turn them on." % lane["name"])
    if not isinstance(repo, str) or not REPO_ID_RE.match(repo):
        raise ValueError("That is not a valid Hugging Face repo id.")
    family = _resolved_family(lane, family_id)
    if family is None:
        raise ValueError("Name a style family to download from (Browse styles again and "
                          "pick \"Get it\" from the list).")
    try:
        catalog = catalog_for(family["hf_base"])
    except Exception:
        catalog = []
    entry = next((c for c in catalog if c["id"] == repo), None)
    if entry is None:
        raise ValueError("That pack is not in this lane's catalog. Browse styles again and "
                          "pick \"Get it\" from the list.")
    if not isinstance(filename, str) or os.path.basename(filename) != filename \
            or filename.startswith(".") or not filename.endswith(".safetensors"):
        raise ValueError("That is not a valid .safetensors filename.")
    if not any(f["filename"] == filename for f in entry["files"]):
        raise ValueError("That file is not listed for %s in the catalog." % repo)
    loras_dir = os.path.abspath(dl["loras_dir"])
    folder = family.get("folder") or ""
    target_dir = os.path.abspath(os.path.join(loras_dir, folder)) if folder else loras_dir
    # commonpath refuses "../" (or any other) escape from loras_dir via the
    # family's own folder, the same way the dest-vs-target_dir check below
    # refuses one via the filename.
    if os.path.commonpath([target_dir, loras_dir]) != loras_dir:
        raise ValueError("That style family's folder would land outside the lane's LoRA folder.")
    dest = os.path.abspath(os.path.join(target_dir, filename))
    if os.path.dirname(dest) != target_dir:
        raise ValueError("That filename would land outside the lane's LoRA folder.")
    if os.path.exists(dest):
        raise ValueError("%s already has a file named %s." % (lane["name"], filename))
    max_bytes = dl.get("max_bytes") or DOWNLOAD_DEFAULT_MAX_BYTES
    url = "https://huggingface.co/%s/resolve/main/%s" % (
        urllib.parse.quote(repo, safe="/"), urllib.parse.quote(filename))
    return url, dest, max_bytes


def _allowed_download_host(host):
    """Security review Finding 2: a redirect target is allowed only on
    Hugging Face's own hosts. Confirmed live 2026-09-28 (a HEAD against a
    real resolve URL, in development, never in a test): huggingface.co's
    resolve endpoint 302s to *.hf.co (its CDN, e.g. us.aws.cdn.hf.co)."""
    host = (host or "").lower()
    return host in ("huggingface.co", "hf.co") or host.endswith(".huggingface.co") or host.endswith(".hf.co")


class _PinnedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Default urllib follows a redirect to ANY host. This refuses one that
    isn't https and on an allowed Hugging Face host, so a compromised/MITM'd
    resolve response can't make the server fetch-and-write bytes from an
    attacker-controlled host."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _download_url_ok(newurl):
            raise ValueError("The download redirected to an untrusted address; refused.")
        new = urllib.request.HTTPRedirectHandler.redirect_request(
            self, req, fp, code, msg, headers, newurl)
        # W3: a Hugging Face token rides only as an UNREDIRECTED header, so
        # urllib never copies it to the CDN; it is put back only for a hop that
        # lands on huggingface.co itself (a renamed repo, say).
        token = req.unredirected_hdrs.get("Authorization")
        if new is not None and token and _hf_token_host(newurl):
            new.add_unredirected_header("Authorization", token)
        return new


def _test_origin(name):
    """Test-only (same pattern as BWF_TEST_HF_API): a plain-http stand-in for
    a Hugging Face host. Pinned: honoured only as exactly
    http://127.0.0.1:<port>, anything else in the variable is ignored."""
    m = re.match(r"^http://127\.0\.0\.1:(\d{1,5})$", os.environ.get(name) or "")
    return ("127.0.0.1", int(m.group(1))) if m else None


_TEST_HF_DOWNLOAD = _test_origin("BWF_TEST_HF_DOWNLOAD")   # stands in for huggingface.co
_TEST_HF_CDN = _test_origin("BWF_TEST_HF_CDN")             # stands in for its file CDN
MODEL_DL_BASE = ("http://127.0.0.1:%d" % _TEST_HF_DOWNLOAD[1]) if _TEST_HF_DOWNLOAD else "https://huggingface.co"


def _url_origin(url):
    u = urllib.parse.urlparse(url)
    try:
        return u.scheme, (u.hostname or "").lower(), u.port
    except ValueError:
        return u.scheme, "", None


def _download_url_ok(url):
    """Where a download may be fetched from, redirects included: https on a
    Hugging Face host, or the test-only pinned stand-ins above."""
    scheme, host, port = _url_origin(url)
    if scheme == "https":
        return _allowed_download_host(host)
    return scheme == "http" and (host, port) in [o for o in (_TEST_HF_DOWNLOAD, _TEST_HF_CDN) if o]


def _hf_token_host(url):
    """W3: the only host HF_TOKEN is ever sent to -- huggingface.co itself
    (its CDN serves signed URLs and never needs it), or its test stand-in."""
    scheme, host, port = _url_origin(url)
    if scheme == "https":
        return host == "huggingface.co"
    return scheme == "http" and _TEST_HF_DOWNLOAD is not None and (host, port) == _TEST_HF_DOWNLOAD


_DOWNLOAD_OPENER = urllib.request.build_opener(_PinnedRedirectHandler)
# Windows has neither flag: 0 there, and the lstat/fstat checks around every
# open stay the guard (security review 2026-09-30, finding 4).
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


class _Cancelled(ValueError):
    pass


class _SizeRefused(ValueError):
    """The server's size disagrees with the size the download may have."""
    pass


class _Short(ValueError):
    """The stream ended before the expected size (the .part is resumable)."""
    pass


def _download_body(r, open_part, limit, exact=False, have=0, cancelled=None, progress=None):
    """The download core Build C's LoRA download and W3's model downloads
    share: copy the open response `r` into the file `open_part()` returns
    (called only after the size checks pass, so a refused answer never
    creates a .part), starting at byte `have`.

    exact=False (LoRA): `limit` is a cap -- a Content-Length over it is
    refused, and so is the stream once it passes it.
    exact=True (W3): `limit` IS the file's size -- Content-Length must say
    exactly the bytes still missing, writing stops at `limit`, and one more
    byte from the server refuses the file (_SizeRefused); a stream that ends
    early raises _Short. -> the bytes now in the file."""
    total = r.headers.get("Content-Length")
    total = int(total) if total and total.isdigit() else None
    if progress:
        progress(have, total)
    if exact:
        if total != limit - have:
            raise _SizeRefused("the server said %s bytes, not the %d expected"
                               % ("no size" if total is None else "%d" % (have + total), limit))
    elif total and total > limit:
        raise ValueError("That file is %d bytes, over the %d byte limit." % (total, limit))
    got = have
    with open_part() as f:
        while not exact or got < limit:
            if cancelled and cancelled():
                raise _Cancelled("Cancelled.")
            # read1: whatever has arrived, up to 64 KiB -- a slow drip must not
            # hold Cancel back for a whole MiB (security review finding 3).
            chunk = getattr(r, "read1", r.read)(min(1 << 16, limit - got) if exact else 1 << 16)
            if not chunk:
                if exact:
                    raise _Short("stopped at %d of %d bytes" % (got, limit))
                break
            got += len(chunk)
            if not exact and got > limit:
                raise ValueError("That file is over the %d byte limit." % limit)
            f.write(chunk)
            if progress:
                progress(got, total)
        if exact and r.read(1):
            raise _SizeRefused("the server sent more than the %d bytes expected" % limit)
        f.flush()
        os.fsync(f.fileno())
    return got


def _run_lora_download(lane, url, dest, max_bytes, repo, filename, convert_name=None):
    state = {"repo": repo, "file": filename, "bytes": 0, "total": None,
             "done": False, "ok": False, "error": "", "cancel": False}
    with DOWNLOAD_LOCK:
        DOWNLOADS[lane["id"]] = state
    part = dest + ".part"
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    try:
        def open_part():
            # Security review Finding 4: O_EXCL|O_NOFOLLOW refuses a
            # pre-existing ".part" path outright -- a real file OR a
            # symlink -- instead of writing through it.
            fd = os.open(part, os.O_CREAT | os.O_EXCL | os.O_WRONLY | _O_NOFOLLOW, 0o644)
            return os.fdopen(fd, "wb")

        def cancelled():
            with DOWNLOAD_LOCK:
                return bool(DOWNLOADS.get(lane["id"], {}).get("cancel"))

        def progress(got, total):
            state["bytes"], state["total"] = got, total

        with _DOWNLOAD_OPENER.open(urllib.request.Request(url), timeout=60.0) as r:
            _download_body(r, open_part, max_bytes, cancelled=cancelled, progress=progress)
        os.replace(part, dest)
        if convert_name:
            # LORA-2E #1: the pack declared a conversion for this family --
            # run it now, on the just-verified download, before it's ever
            # listed. A conversion failure fails the WHOLE download (clear
            # sentence, no partials left behind) rather than leaving an
            # unconverted file a picker would list as if it just worked.
            tmp = dest + ".converting"
            try:
                _convert_lora_file(convert_name, dest, tmp)
                os.replace(tmp, dest)
            except Exception as e:
                for p in (tmp, dest):
                    try:
                        if os.path.exists(p):
                            os.remove(p)
                    except OSError:
                        pass
                raise ValueError("Downloaded, but could not convert %s for this engine: %s"
                                 % (filename, e))
        state["ok"] = True
        discover_lane(lane)   # Build B: so the picker lists it right away
    except OSError as e:
        # Security review Finding 3: never let a filesystem error's own
        # message (it embeds the absolute path) reach a client -- name only
        # the file, and the error class.
        state["error"] = "Could not write %s (%s)." % (filename, e.__class__.__name__)
        try:
            if os.path.exists(part):
                os.remove(part)
        except OSError:
            pass
    except Exception as e:
        try:
            if os.path.exists(part):
                os.remove(part)
        except OSError:
            pass
        state["error"] = str(e)
    finally:
        state["done"] = True


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
OPTIONAL_NODES = engines.optional_nodes()   # CHARS-1: short id -> node class name, declared by packs

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


def _rule_matches(pool, rule):
    """Every name in `pool` that satisfies `rule` (all/none/any substrings,
    case-insensitive). Shared by pick_model (picks the single best match)
    and a "pool_select" field's options (LORA-1: every match is offered,
    not just the best)."""
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
    return hits


def pick_model(pool, rule, small_card):
    """Best filename in `pool` for one role, or None."""
    hits = _rule_matches(pool, rule)
    if not hits:
        return None
    return sorted(hits, key=lambda x: (-_rank(x, rule, small_card), x))[0]


def pool_select_options(lane, f):
    """LORA-1: every filename in lane's discovered `f["pool"]` that matches
    `f["match"]`, for a "pool_select" field -- the live options a picker
    offers for this lane. Never a hard-coded list."""
    with DISCOVERY_LOCK:
        pool = list((DISCOVERY.get(lane["id"]) or {}).get("pools", {}).get(f.get("pool"), []))
    return _rule_matches(pool, f.get("match") or {})


def fields_with_pool_options(cap, mode, lane):
    """LORA-1: a "pool_select" field's live options, from THIS lane's
    discovered pool -- the static declaration never carries a filename.
    Copies only the fields that need it; every other field is the pack's
    own object, unchanged. A plain function (not a Handler method) so it
    works the same whether called from a request or from a test with no
    Handler instance at all (see tests/test_examples.py)."""
    fields = engines.fields(cap, mode)
    if not lane:
        return fields
    out = []
    for f in fields:
        if f.get("type") == "pool_select":
            f = dict(f)
            f["options"] = [""] + pool_select_options(lane, f)
        out.append(f)
    return out


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
    return pool_names(pool, lambda node: http_get_json(lane_url(lane, "/object_info/%s" % node), timeout=8.0),
                      lane["name"])


def pool_names(pool, info_for, where):
    """fetch_pool()'s reading of one pool from /object_info answers: info_for(node)
    -> that node's /object_info dict (per-node or the whole thing), or raises.
    W2's Setup reuses it on one whole-/object_info fetch."""
    names = []
    for node, field in POOL_NODES[pool]:
        try:
            info = info_for(node)
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
                % (where, node, field), "warn")
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
            DISCOVERY[lane["id"]] = {"models": {}, "pools": pools, "nodes": {}, "checked": time.time(),
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

    # CHARS-1: optional speed nodes, no model file of their own -- presence
    # is a plain /object_info hit, True/False per short id, read back through
    # nodes_for() below; a generate call hands them to the graph builder.
    node_flags = {}
    for short_id, node_cls in OPTIONAL_NODES.items():
        try:
            info = http_get_json(lane_url(lane, "/object_info/%s" % node_cls), timeout=8.0)
        except Exception:
            info = None
        node_flags[short_id] = bool(info and node_cls in info)

    with DISCOVERY_LOCK:
        entry = DISCOVERY.get(lane["id"]) or {}
        prev, first = entry.get("models") or {}, not entry.get("checked")
        DISCOVERY[lane["id"]] = {"models": found, "pools": pools, "nodes": node_flags,
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


def nodes_for(lane):
    """CHARS-1: which of the packs' optional speed nodes this lane has,
    {short id: bool}. Kept apart from models_for() so the lane listing's
    "files" stays a list of files; a generate call merges the two into the
    dict a graph builder reads."""
    with DISCOVERY_LOCK:
        return dict((DISCOVERY.get(lane["id"]) or {}).get("nodes") or {})


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
    if ext in (".json", ".txt", ".csv"):
        return "file"      # the page has no renderer for it: a named tile with a Download link
    return "image"


def poll_process_lane(lane):
    """A process lane is never networked: discovery is which() per declared
    bin, so installing a program shows up on the next poll, no restart.

    The lane is UP when at least one of its process packs has every program
    it declares; it is down only when no pack can run. Whether a MODE can run
    is decided per mode from the found programs (`able` / missing_words), so a
    pack with a program missing stays visible with its own "missing" list and
    never takes a sibling pack's tools down with it. A missing program is still
    named in the lane's `err` so the user knows what to install."""
    found, notes = {}, []
    ran, any_pack = False, False
    for pack in engines.packs():
        if pack["cap"] not in lane["caps"]:
            continue
        if (pack.get("lane_kind") or "comfy") != "process":
            continue
        any_pack = True
        pack_ok = True
        for role, prog in (pack.get("bins") or {}).items():
            path = shutil.which(str(prog))
            found.setdefault(role, path)
            if not path:
                pack_ok = False
                note = ("this lane needs %s installed to work"
                        % (pack.get("words") or {}).get(role, role))
                if note not in notes:
                    notes.append(note)
        ran = ran or pack_ok
    up = ran or not any_pack
    with DISCOVERY_LOCK:
        DISCOVERY[lane["id"]] = {"models": found, "pools": {}, "checked": time.time(), "err": ""}
    st = {"up": up, "checked": time.time(), "live_ids": [], "err": "; ".join(notes),
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
    # A plan with "summary": true prints its one-line result last (grid check): keep just that
    # line as the notes. Any other program's last line is its own output, not a result.
    last = ([t for t in tail if t.strip()][-1:]
            if isinstance(plan, dict) and plan.get("summary") else None)
    _finish_process_job(jid, "done", None, last, outs)


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


def _pct_clamp(pct):
    return max(0.0, min(100.0, pct))


def _stage_percent(n_stages, finished_stages, step, total):
    """UX-2 #9's percent math, a pure function so it is unit-testable without
    a live ws: (finished_stages + current step/total) / n_stages, clamped
    0-100. `n_stages` is the graph's own sampling-stage node count (>=1);
    `finished_stages` is how many of those nodes have already reported and
    been superseded by a later one (never counting the CURRENT node as
    finished)."""
    n_stages = max(1, n_stages)
    frac = _pct_clamp((step / total) * 100.0) / 100.0 if total else 0.0
    return _pct_clamp((finished_stages + frac) / n_stages * 100.0)


def job_progress_view(job):
    """UX-2 #9: the derived {state, stage, stages, percent, elapsed} for a
    queued/running job -- None once it's done/error/interrupted (History and
    Monitor fall back to their existing finished-state rendering then). The
    raw step/total counters stay on the job dict untouched, for the tooltip."""
    status = job.get("status")
    if status not in ("queued", "running"):
        return None
    stage_nodes = job.get("stage_nodes")
    n_stages = max(1, len(stage_nodes)) if stage_nodes is not None else 1
    seen = job.get("stages_seen") or []
    if status == "queued":
        state, stage_index = "loading", 0
    else:
        state = job.get("progress_state") or "loading"
        stage_index = min(len(seen), n_stages) if stage_nodes is not None else (0 if state == "loading" else 1)
    percent = job.get("progress_pct")
    started = job.get("started")
    elapsed = round(time.time() - started, 1) if started else None
    return {"state": state, "stage": stage_index, "stages": n_stages,
            "percent": round(percent, 1) if percent is not None else 0.0, "elapsed": elapsed}


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
            step = int(data.get("value") or 0)
            total = int(data.get("max") or 0) or job.get("total") or 0
            job["step"] = step
            job["total"] = total
            node = data.get("node")
            stage_nodes = job.get("stage_nodes")
            if stage_nodes is None:
                # UX-2 #9: a job whose record predates the stage_nodes field
                # (loaded from jobs.json, or a process/legacy job) -- fall
                # back to the original single-stage step/total percent.
                pct = _pct_clamp((step / total) * 100.0) if total else None
                if pct is not None:
                    job["progress_pct"] = max(pct, job.get("progress_pct") or 0.0)
                job["progress_state"] = "sampling"
            elif node in stage_nodes:
                seen = job.setdefault("stages_seen", [])
                if node not in seen:
                    seen.append(node)
                pct = _stage_percent(len(stage_nodes), len(seen) - 1, step, total)
                job["progress_pct"] = max(pct, job.get("progress_pct") or 0.0)
                job["progress_state"] = "sampling"
            job["updated"] = time.time()
        elif mtype == "executing":
            job["status"] = "running"
            node = data.get("node")
            job["node"] = node
            stage_nodes = job.get("stage_nodes")
            seen = job.get("stages_seen") or []
            if stage_nodes:
                if not seen:
                    job["progress_state"] = "loading"
                elif node not in stage_nodes:
                    # A non-stage node is running (decode/save/upscale/a
                    # second encode...). "Finishing..." is only correct once
                    # the LAST stage has started -- a node BETWEEN stage 1
                    # and stage 2 of a multi-stage graph (e.g. a latent
                    # upscale) is not finishing, it's still sampling, and
                    # must not flip to Finishing then back to sampling when
                    # stage 2 starts. Either way the floor is every fully-
                    # seen stage counting as done, via the SAME pure
                    # _stage_percent math progress uses (never a second,
                    # slightly different formula) so the bar never steps
                    # backward.
                    floor_pct = _stage_percent(len(stage_nodes), len(seen), 0, 0)
                    job["progress_pct"] = max(job.get("progress_pct") or 0.0, floor_pct)
                    job["progress_state"] = "finishing" if len(seen) >= len(stage_nodes) else "sampling"
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


# -- E1 "Sing along" ---------------------------------------------------------
# A video shot whose mode's pack declares `sing_along` can sing the sequence's
# song: the first SOUND-lane slot with a pick (the cut's own bed rule). The
# shot's own record is slot["sing"] = {"on", "start"}; where it sings is always
# COMPUTED here, never typed, except a chain head's "Starts at" (R3).

def _sing_spec(cap, mode):
    """The owning pack's `sing_along` entry for this mode, or None."""
    for pack in engines.packs():
        if pack["cap"] == cap and mode in pack["graphs"]:
            return (pack.get("sing_along") or {}).get(mode)
    return None


def seq_song_slot(seq):
    return next((s for s in seq.get("slots") or [] if s.get("lane") == "sound" and s.get("pick")), None)


def _sing_length(slot, spec):
    """The shot's length in frames, snapped exactly as its graph will get it."""
    field = spec.get("length_field", "length")
    val = (slot.get("values") or {}).get(field)
    if val is None:
        val = next((f.get("default") for f in engines.fields(slot.get("cap"), slot.get("mode"))
                    if f.get("id") == field), None)
    try:
        return snap_frames(val)
    except (TypeError, ValueError):
        return None


def seq_sing_plan(seq):
    """R3: {slot id: {start, end, length, fps, head, lead_in, heard_start}} for every video
    shot singing along right now, in video-lane order. A shot continue-cabled
    from an earlier singing shot starts where that shot's last carried frames
    start: prev.start + (prev.length - carried) / fps. Any other singing shot
    is a chain head and starts at its own "Starts at". `lead_in` is how far
    into its slice a shot's first DELIVERED frame is (a cabled shot's carried
    frames are trimmed off its delivery); `heard_start` = start + lead_in is
    where its audible span begins (what the page prints)."""
    if seq_song_slot(seq) is None:
        return {}
    plan = {}
    cables = seq.get("cables") or []
    for slot in seq.get("slots") or []:
        if slot.get("lane") != "video" or not (slot.get("sing") or {}).get("on"):
            continue
        spec = _sing_spec(slot.get("cap"), slot.get("mode"))
        length = _sing_length(slot, spec) if spec else None
        if length is None:
            continue
        fps = float(spec["fps"])
        video_jacks = {j["field"] for j in slot_jacks(slot) if j.get("type") == "video"}
        cable = next((c for c in cables if c.get("to") == slot["id"] and c.get("field") in video_jacks), None)
        carried = spec.get("carried_frames", 0) if cable else 0
        prev = plan.get(cable.get("from")) if cable else None
        if prev is not None:
            start = prev["start"] + (prev["length"] - carried) / fps
        else:
            start = float(slot["sing"].get("start") or 0.0)
        plan[slot["id"]] = {"start": round(start, 6), "end": round(start + length / fps, 6), "length": length,
                            "fps": fps, "head": prev is None, "lead_in": round(carried / fps, 6),
                            "heard_start": round(start + carried / fps, 6)}
    return plan


def _resolved_sing_entry(seq, slot):
    return seq_sing_plan(seq).get(slot.get("id"))


def _resolved_sing_use(seq, slot):
    """What a generate right now would put in take["inputs"]["sing"], or None."""
    entry = seq_sing_plan(seq).get(slot.get("id"))
    if entry is None:
        return None
    return {"song": seq_song_slot(seq)["pick"], "start": entry["start"], "lead_in": entry["lead_in"]}


# -- E2 "join: dissolve" -------------------------------------------------------
# A mode whose pack declares `joins` can keep its carried frames (the shot
# before's last frames, re-rendered) instead of trimming them; the cut then
# cross-dissolves across them. The take records what its render did.

def _join_spec(cap, mode):
    """The owning pack's `joins` entry for this mode, or None."""
    for pack in engines.packs():
        if pack["cap"] == cap and mode in pack["graphs"]:
            return (pack.get("joins") or {}).get(mode)
    return None


def _join_seq_defaults(cap, mode):
    """{join field: the Cutting Room's default} for a mode whose pack declares
    a `sequence_default` for its join field, else {}. A sequence shot that has
    never set the field takes it; any other request takes the field's own."""
    spec = _join_spec(cap, mode)
    return {spec["field"]: bool(spec["sequence_default"])} if spec and "sequence_default" in spec else {}


def _join_kept(cap, mode, values):
    """How many carried frames a render from these request values keeps: the
    pack's overlap when its join field is on (the field's own default when
    unset) AND a video jack is filled, else 0."""
    spec = _join_spec(cap, mode)
    if not spec:
        return 0
    fields = engines.fields(cap, mode)
    on = values.get(spec["field"])
    if on is None:
        on = next((f.get("default") for f in fields if f.get("id") == spec["field"]), False)
    if not on or not any(values.get(f["id"]) for f in fields if f.get("type") == "video"):
        return 0
    return int(spec["overlap_frames"])


def slot_stale_inputs(seq, slot, take):
    """The inputs half of STALE: "room plate changed" arrives with refs
    (C3.2b); "<jack label, lower-cased> changed" arrives with cables (C3.4);
    E1's song/offset changes arrive with sing.
    It compares the pick's take["inputs"] against what would resolve now."""
    reasons = []
    inputs = take.get("inputs")
    if isinstance(inputs, dict) and inputs.get("adopted") is True:
        # F5: a take the person already had (adopt_take) was not made from this
        # slot's refs or cables, so it is never stale for them; only the song
        # check below still speaks.
        inputs = {k: v for k, v in inputs.items() if k not in ("refs", "cables")}
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
    if isinstance(inputs, dict):
        made, now = inputs.get("sing"), _resolved_sing_use(seq, slot)
        if made != now:
            if made and now and made.get("song") != now.get("song"):
                reasons.append("song changed")
            elif made and now:
                reasons.append("song timing changed")
            else:
                reasons.append("sing along changed")
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


def _sing_upstream(seq, slot):
    """The slot cabled into a video jack of `slot`, or None."""
    video_jacks = {j["field"] for j in slot_jacks(slot) if j.get("type") == "video"}
    cable = next((c for c in seq.get("cables") or []
                  if c.get("to") == slot.get("id") and c.get("field") in video_jacks), None)
    if cable is None:
        return None
    return next((s for s in seq.get("slots") or [] if s.get("id") == cable.get("from")), None)


def slot_stale(seq, slot, beats, _seen=None):
    take = next((t for t in slot.get("takes") or [] if t.get("job_id") == slot.get("pick")), None)
    if not take:
        return []
    reasons = []
    beat = beats.get(slot.get("beat_id"))
    if beat and take.get("beat_rev") is not None and take["beat_rev"] != beat.get("rev"):
        reasons.append("script changed")
    reasons += slot_stale_inputs(seq, slot, take)
    # E1: along a SINGING chain staleness is transitive (a chain is re-rolled
    # in order). Only for a shot that sings; everything else keeps the one hop.
    if slot.get("id") in seq_sing_plan(seq):
        seen = (_seen or set()) | {slot.get("id")}
        up = _sing_upstream(seq, slot)
        if up is not None and up.get("id") not in seen and slot_stale(seq, up, beats, seen):
            reasons.append("an earlier shot changed")
    return reasons


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
    song = seq_song_slot(out)
    sing_plan = seq_sing_plan(out)
    out["song"] = {"slot_id": song["id"], "job_id": song["pick"]} if song else None
    # RS3: known only once a probe has run (probe_song_async); absent, not guessed.
    song_len = song_seconds_known(song["pick"]) if song else None
    if song_len is not None:
        out["song_seconds"] = song_len
    for slot in out.get("slots") or []:
        # E1: "Sing along" is offered on a video shot whose mode can take the
        # song, only while the sequence has one; sing_span is where it sings.
        slot["sing_offer"] = bool(song and slot.get("lane") == "video"
                                  and _sing_spec(slot.get("cap"), slot.get("mode")))
        slot["sing_span"] = sing_plan.get(slot["id"])
        slot["state"], slot["progress"] = slot_state(slot, jobs, lane_up)
        slot["stale"] = slot_stale(out, slot, beats)
        slot["sees_refs"] = slot_sees_refs(slot)
        slot["warnings"] = slot_warnings(out, slot)
        slot["jacks"] = slot_jacks(slot)
        slot["trim_effective"] = slot_trim_effective(out["id"], slot)
        # E2: what an unset field means on this shot (the page shows it so).
        seq_defaults = _join_seq_defaults(slot.get("cap"), slot.get("mode"))
        if seq_defaults:
            slot["seq_defaults"] = seq_defaults
    out["can_cut"] = CAN_CUT
    out["cut_reason"] = CUT_REASON
    out["can_title"] = CAN_TITLE
    out["title_reason"] = TITLE_REASON
    if out.get("audio_led"):
        _audio_led_derive(out)
    return out


AUDIO_LED_FPS = 24


def _slot_planned_len(slot):
    """Seconds a video shot is planned to run in an audio-led sequence: its
    trim length when it has one, else the frames it will be made with (an
    8n+1 LTX shot keeps 8n, so 97 frames plan 4.0 s)."""
    trim = slot.get("trim")
    if isinstance(trim, dict):
        ln = trim.get("len")
        if isinstance(ln, (int, float)) and not isinstance(ln, bool) and ln > 0:
            return float(ln)
    try:
        frames = int((slot.get("values") or {}).get("length", 97))
    except (TypeError, ValueError):
        frames = 97
    return max(frames - 1, 1) / AUDIO_LED_FPS


def _audio_led_master_slot(seq):
    """The master sound: the first sound-lane slot, in slot order, with a pick
    (the same slot the classic cut uses as its bed)."""
    return next((s for s in seq.get("slots") or [] if s.get("lane") == "sound" and s.get("pick")), None)


def _audio_led_master_source(seq):
    """(absolute path, start offset in seconds) of the master sound; raises ValueError with a plain sentence.
    An imported sound file wins; otherwise the first picked sound shot."""
    sid = seq["id"]
    mf = seq.get("master")
    if isinstance(mf, dict) and mf.get("file"):
        base = os.path.realpath(os.path.join(SEQ_MEDIA_DIR, sid))
        path = os.path.realpath(os.path.join(base, mf["file"]))
        if not path.startswith(base + os.sep) or not os.path.isfile(path):
            raise ValueError("The master sound file is missing. Add it again.")
        return path, float(mf.get("start") or 0.0)
    master = _audio_led_master_slot(seq)
    if master is None:
        raise ValueError("This sequence is audio-led, so it needs a master sound: pick a take on a sound shot, "
                         "or add a sound file.")
    try:
        path = _cut_ensure_take_file(sid, master["id"], master["pick"])
    except ValueError:
        raise ValueError("The master sound (%s) is not copied yet — is its lane off?" % master["id"])
    return path, 0.0


def _audio_led_derive(out):
    """Audio-led only: master_slot_id (and master_file when a sound file was imported), and each video
    shot's window on the master (planned lengths laid end to end, no gaps)."""
    mf = out.get("master")
    has_file = isinstance(mf, dict) and bool(mf.get("file"))
    master = None if has_file else _audio_led_master_slot(out)
    out["master_slot_id"] = master["id"] if master else None
    if has_file:
        out["master_file"] = {"name": mf.get("name"), "seconds": mf.get("seconds"), "start": mf.get("start", 0.0)}
    start = 0.0
    for slot in out.get("slots") or []:
        if slot.get("lane") != "video":
            continue
        ln = _slot_planned_len(slot)
        slot["window"] = {"start": round(start, 6), "len": round(ln, 6)}
        start += ln


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


def _op_set_audio_led(seq, p):
    """Opt-in: the sequence's master sound (its first picked sound shot) leads
    the cut and drives the shots. Off deletes the key, so a sequence switched
    off is byte-identical to one that never had it."""
    on = p.get("on")
    if not isinstance(on, bool):
        raise ValueError("Audio-led is either on or off.")
    if on:
        seq["audio_led"] = True
    else:
        seq.pop("audio_led", None)


def _op_set_master_start(seq, p):
    """Where in the imported master sound the cut starts (seconds): lets a long song be used from the middle."""
    mf = seq.get("master")
    if not isinstance(mf, dict) or not mf.get("file"):
        raise ValueError("Add a sound file to this sequence first.")
    start = p.get("start")
    if isinstance(start, bool) or not isinstance(start, (int, float)) or start < 0:
        raise ValueError("The start must be a number of seconds, zero or more.")
    secs = mf.get("seconds")
    if isinstance(secs, (int, float)) and start >= secs:
        raise ValueError("The sound file is only %.1f seconds long; start earlier than that." % secs)
    mf["start"] = float(start)


def _op_clear_master(seq, p):
    seq.pop("master", None)


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
    # UX-1: freed ids are reused (max(used)+1 over whatever is LEFT after an
    # op queued just ahead of this one on the client's own op chain already
    # landed), so a client that finds "the new one" by diffing ids against
    # what it had before can land on the very id an in-flight remove just
    # freed and this add reused -- name it directly instead.
    return {"added_slot_id": slot["id"]}


def _switch_slot_mode(slot, cap, mode):
    """A shot turning into another mode of the same kind (adopt_take, and
    RS2's "Kind of shot" through update_slot): what the new mode does not
    declare is dropped, and the recipe and quality -- both belong to the old
    mode -- are reset. The caller runs _check_slot on the result."""
    slot["mode"] = mode
    if mode in engines.modes_for(cap):
        known = {f["id"] for f in engines.fields(cap, mode)}
        slot["values"] = {k: v for k, v in (slot.get("values") or {}).items() if k in known}
    slot["recipe"] = slot["quality"] = None


def _op_update_slot(seq, p):
    """Changes any of cap/mode/recipe/quality/refs, and MERGES `values`
    (a key sent as null is removed). The result must still validate whole.
    A changed `mode` is a mode switch (_switch_slot_mode): values the new
    mode does not declare are pruned and recipe/quality reset, then anything
    else sent in the same call is applied on top."""
    slot = _slot(seq, p)
    new = copy.deepcopy(slot)
    if "mode" in p and p["mode"] != slot.get("mode"):
        _switch_slot_mode(new, p.get("cap", slot.get("cap")), p["mode"])
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


def _op_adopt_take(seq, p):
    """F5. {slot_id, job_id}: make a finished job the person already has (from
    History) one of this shot's takes and its pick. A take of another mode
    of the same kind turns the shot into that mode."""
    slot = _slot(seq, p)
    jid = p.get("job_id")
    if not isinstance(jid, str) or not jid:
        raise ValueError("Choose one of the finished takes.")
    with JOBS_LOCK:
        job = copy.deepcopy(JOBS.get(jid))
    if job is None:
        raise ValueError("That take is not in History any more.")
    if job.get("status") != "done":
        raise ValueError("That take has not finished, so it cannot be used.")
    cap = slot.get("cap")
    if job.get("kind") != cap:
        raise ValueError("That take is a %s, but this shot makes a %s." % (
            engines.cap_word(job.get("kind")), engines.cap_word(cap)))
    if job.get("mode") not in engines.modes_for(cap):
        raise ValueError("No installed engine makes that kind of take.")
    if jid in [t.get("job_id") for t in slot["takes"]]:
        raise ValueError("This shot already has that take.")
    if job["mode"] != slot.get("mode"):
        _switch_slot_mode(slot, cap, job["mode"])   # values pruned, recipe/quality reset
        _check_slot(seq, slot)
    inputs = {"refs": [], "cables": {}, "adopted": True}
    if isinstance(job.get("join"), dict):
        inputs["join"] = job["join"]   # E2: its first frames are the overlap; the cut drops them
    slot["takes"].append({"job_id": jid, "made": time.time(), "beat_rev": None,
                          "inputs": inputs, "file": None})
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


def _op_set_sing(seq, p):
    """E1 (R2/R3). {slot_id, on?, start?}: a video shot's "Sing along" toggle
    and its "Starts at" (seconds into the song -- only a chain head uses it;
    everything else about where a shot sings is computed, seq_sing_plan)."""
    slot = _slot(seq, p)
    if slot.get("lane") != "video" or not _sing_spec(slot.get("cap"), slot.get("mode")):
        raise ValueError("This shot's recipe cannot sing along.")
    sing = dict(slot.get("sing") or {"on": False, "start": 0.0})
    if "on" in p:
        if not isinstance(p["on"], bool):
            raise ValueError("Sing along is either on or off.")
        if p["on"] and seq_song_slot(seq) is None:
            raise ValueError("Pick a song in the sound lane first.")
        sing["on"] = p["on"]
    if "start" in p:
        start = _num(p["start"], "Starts at")
        try:
            start = float(start)
            in_range = 0 <= start <= 86400
        except OverflowError:   # a huge int has no float
            in_range = False
        if not in_range:   # NaN and inf fail the comparison too
            raise ValueError("Starts at must be a number of seconds from 0 up to 24 hours.")
        sing["start"] = start
    slot["sing"] = sing


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
    "set_audio_led": _op_set_audio_led, "set_master_start": _op_set_master_start, "clear_master": _op_clear_master,
    "add_slot": _op_add_slot, "update_slot": _op_update_slot, "move_slot": _op_move_slot,
    "remove_slot": _op_remove_slot, "pick_take": _op_pick_take, "adopt_take": _op_adopt_take, "set_trim": _op_set_trim,
    "set_title_card": _op_set_title_card, "set_sing": _op_set_sing,
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
SEQ_OPS_PIN_JOBS = frozenset({"add_ref", "pick_take", "adopt_take"})


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
    song = seq_song_slot(seq)
    if song:
        probe_song_async(sid, song["id"], song["pick"])   # RS3: e.g. after an app restart; background only
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
    if op in ("pick_take", "adopt_take"):
        # RS3: the song take just became (or stayed) the sequence's song --
        # learn its length in the background, never in this request.
        song = seq_song_slot(new)
        if song and song["id"] == p.get("slot_id"):
            probe_song_async(sid, song["id"], song["pick"])
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


_WIN_RESERVED_NAMES = {"con", "prn", "aux", "nul"}
_WIN_RESERVED_NAMES |= {"com%d" % i for i in range(1, 10)}
_WIN_RESERVED_NAMES |= {"lpt%d" % i for i in range(1, 10)}


def _unsafe_output_name(filename):
    """True for a filename Windows cannot store as a real file, checked in
    ONE place for both output resolvers below.

    Windows silently trims trailing spaces and dots, so `NUL.tmp` and
    `render.png.` name something other than what was asked for, and CON /
    PRN / AUX / NUL / COM1-9 / LPT1-9 are DEVICES with any extension --
    writing one discards the bytes and the later os.replace() raises
    FileNotFoundError (E-1/E-2 in the Windows audit). Only the part before
    the first dot counts, so an ordinary name that merely starts with one
    (`con_art.png`, `nulled.png`) is left alone.
    """
    if not isinstance(filename, str) or not filename:
        return False
    if filename[-1] in " .":
        return True
    return filename.split(".")[0].lower() in _WIN_RESERVED_NAMES


def lane_output_path(lane, output):
    """UX-2 #5: resolve one job["outputs"] entry (subfolder+filename) to a
    real path under this LANE's own configured `outputs.dir`, or raise
    ValueError with a plain sentence -- same shape as local_output_path()'s
    containment rule (basename the filename, realpath both sides, refuse
    anything that lands outside, which also catches a symlink escape).
    None (not ValueError) when the lane has no outputs.dir configured at
    all -- that is the normal, off-by-default case, not a refusal.
    """
    outs = lane.get("outputs")
    if not isinstance(outs, dict) or not outs.get("dir"):
        return None
    filename = os.path.basename(output.get("filename") or "")
    if not filename or filename in (".", "..") or _unsafe_output_name(filename):
        raise ValueError("that output filename is not allowed")
    subfolder = output.get("subfolder") or ""
    # A subfolder is ComfyUI's own (e.g. a job-id-shaped folder name), never
    # a path a caller can steer: forbid a separator or ".." outright rather
    # than trust realpath alone to catch every shape of escape.
    if os.sep in subfolder or (os.altsep and os.altsep in subfolder) or ".." in subfolder.split(os.sep):
        raise ValueError("that output path is not allowed")
    base = os.path.realpath(outs["dir"])
    path = os.path.realpath(os.path.join(base, subfolder, filename))
    if path != base and not path.startswith(base + os.sep):
        raise ValueError("that output path is not allowed")
    return path


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
    if not filename or filename in (".", "..") or _unsafe_output_name(filename):
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
            # The write above only records the take's local cache path -- not a
            # user-visible change (the take, its pick and its stale reasons were all
            # set earlier, by ops that did bump rev), so NO rev bump: this runs from
            # job_poller, and a bump 409s the op a person sends on the rev they
            # already read. Every op re-reads from disk under SEQ_LOCK.
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


ESTIMATE_WINDOW_S = 72 * 3600
ESTIMATE_SAMPLE_MAX = 5


def estimate_seconds(lane_id, cap, mode, quality_id):
    """R5/A3: MEDIAN elapsed seconds of this lane's finished jobs with this
    exact (mode, quality tier), windowed to the most recent ESTIMATE_SAMPLE_MAX
    (5) of them that finished within the last ESTIMATE_WINDOW_S (72h) -- never
    extrapolated from a different tier or a different lane. None with fewer
    than 3 in that window.

    An earlier, all-time version took the median of EVERY finished job ever
    recorded, with no time window: a single slow cold-start job (a model's
    first load into VRAM) stayed baked into the number indefinitely, and a
    runtime change that made a mode faster could not lower it until enough
    new jobs had piled up to outvote the old ones. Windowing to the last 72h
    (and only the 5 most recent within it) lets the number actually expire.
    """
    cutoff = time.time() - ESTIMATE_WINDOW_S
    with JOBS_LOCK:
        recent = sorted((j for j in JOBS.values()
                          if j.get("lane") == lane_id and j.get("kind") == cap and j.get("mode") == mode
                          and j.get("quality") == quality_id and j.get("status") == "done" and "elapsed" in j
                          and (j.get("finished") or 0) >= cutoff),
                         key=lambda j: j.get("finished") or 0, reverse=True)[:ESTIMATE_SAMPLE_MAX]
        vals = sorted(j["elapsed"] for j in recent)
    if len(vals) < 3:
        return None
    n, mid = len(vals), len(vals) // 2
    return vals[mid] if n % 2 else round((vals[mid - 1] + vals[mid]) / 2, 1)


def estimate_range(lane_id, cap, mode, quality_id):
    """UX-2 #3: sibling of estimate_seconds, same (lane, mode, quality) filter
    and the same 72h / ESTIMATE_SAMPLE_MAX(5) window -- but returns
    (min, max, n) instead of a median, and with no 3-job floor: a range
    reads honestly at n==1 too ("about N"), unlike the median which needs 3
    to mean anything. estimate_seconds itself is UNCHANGED for its existing
    callers (owner ruling B)."""
    cutoff = time.time() - ESTIMATE_WINDOW_S
    with JOBS_LOCK:
        recent = sorted((j for j in JOBS.values()
                          if j.get("lane") == lane_id and j.get("kind") == cap and j.get("mode") == mode
                          and j.get("quality") == quality_id and j.get("status") == "done" and "elapsed" in j
                          and (j.get("finished") or 0) >= cutoff),
                         key=lambda j: j.get("finished") or 0, reverse=True)[:ESTIMATE_SAMPLE_MAX]
        vals = sorted(j["elapsed"] for j in recent)
    if not vals:
        return (None, None, 0)
    return (vals[0], vals[-1], len(vals))


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
    # UX-2 #9: the graph's own sampling-stage nodes (KSampler and friends,
    # plus whatever each pack's ENGINE["stage_classes"] adds), fixed at
    # dispatch time from the exact graph just queued -- never guessed from
    # the mode name, so a mode with two samplers (an upscale pass) counts
    # two stages without server.py knowing why.
    stage_classes = engines.stage_class_types()
    stage_nodes = [nid for nid, node in graph.items()
                   if isinstance(node, dict) and node.get("class_type") in stage_classes]
    job = {
        "id": jid, "lane": lane["id"], "lane_name": lane["name"], "prompt_id": res["prompt_id"],
        "kind": kind, "mode": mode, "status": "queued", "step": 0, "total": meta.get("steps", 0),
        "created": time.time(), "started": time.time(), "updated": time.time(),
        "outputs": [], "notes": notes,
        "stage_nodes": stage_nodes, "stages_seen": [], "progress_state": "loading", "progress_pct": 0.0,
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
            "%s -m pip install Pillow" % runner.venv_python_hint()
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
    _helper_connect_check_to(HELPER)


def _helper_connect_check_to(helper):
    """P3: a plain HTTP request's timeout covers connect AND read together,
    so an unreachable (not merely refusing) helper host used to hang for the
    full read timeout_s (default 60s) before the page saw anything besides
    "Thinking...". A short TCP connect probe first turns that into an
    immediate, specific sentence; a helper that DOES answer, just slowly,
    still gets the full timeout_s as its read timeout below."""
    parsed = urllib.parse.urlsplit(helper["url"])
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
    return _helper_chat_to(HELPER, messages, max_tokens, timeout)


def _read_by(r, deadline, limit):
    """Read an HTTP response's body by a time.monotonic() deadline, at most
    `limit` bytes -> bytes, or raises. Each socket wait is cut to the time
    left, so neither a stalled nor a trickling answer outlasts the deadline.
    ponytail: covers the BODY only -- a server trickling its status line and
    headers is still bounded only by the per-read socket timeout passed to
    urlopen (a watchdog thread would close that gap)."""
    sock = getattr(getattr(getattr(r, "fp", None), "raw", None), "_sock", None)
    buf = bytearray()
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("answer took too long")
        if sock is not None and not r.isclosed():   # a finished response closes its socket
            sock.settimeout(left)
        chunk = r.read1(65536)
        if not chunk:
            return bytes(buf)
        buf += chunk
        if len(buf) > limit:
            raise ValueError("answer too large")


def _helper_chat_to(helper, messages, max_tokens=512, timeout=None, opener=None, total=None):
    """_helper_chat against a given helper dict ({"url", "model", ...}) --
    Setup tests a guide before any config names it. Setup also passes its
    no-redirect `opener` and `total`: a wall-clock limit in seconds for the
    whole exchange, which caps the answer at SETUP_READ_LIMIT bytes too.
    Neither given = exactly the normal-mode behaviour."""
    deadline = time.monotonic() + total if total else None
    _helper_connect_check_to(helper)
    url = helper["url"].rstrip("/") + "/chat/completions"
    payload = {"model": helper.get("model") or "", "messages": messages, "max_tokens": max_tokens}
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    wait = timeout or helper.get("timeout_s", 60)
    if deadline:
        wait = max(0.1, min(wait, deadline - time.monotonic()))
    try:
        with (opener.open if opener else urllib.request.urlopen)(req, timeout=wait) as r:
            data = _read_by(r, deadline, SETUP_READ_LIMIT) if deadline else r.read()
            raw = json.loads(data.decode("utf-8"))
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


def _has_dotdot(path):
    """True when any component of `path` is "..". Both separators count:
    os.sep is "/" here but "\\" on Windows, and a client (or a stored
    relative path) can carry either -- splitting only on "/" would let
    "a\\..\\b" through the check on both platforms. realpath stays the
    backstop; this keeps the shape check portable."""
    return ".." in str(path).replace("\\", "/").split("/")


def _resolve_upload_bytes(lane_id, name):
    """-> (bytes, filename) for an uploaded input, reached the same contained
    way an upload already is: a process lane's own uploads dir (basename
    only, no traversal); a comfy lane's /view?type=input (subfolder split
    off the stored name, same as _carry_source_bytes)."""
    if not name or "\x00" in name or _has_dotdot(name):
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


# B2: guide history on the server (data/guide_history/<key>.json), the same
# atomic tmp+fsync+os.replace shape as _seq_read/_seq_write. <key> is exactly
# what the client's own guideHistKey() computes today ("bwf.guide.hist." +
# room id, plus ".<sequence id>" in the Cutting Room) -- never guessed at:
# every part is checked against a REAL room or sequence, which also closes
# off path traversal (nothing past the prefix can be anything else).
GUIDE_HIST_KEY_PREFIX = "bwf.guide.hist."
GUIDE_HIST_MAX_TURNS = 200
GUIDE_HIST_MAX_BYTES = 256 * 1024


def _guide_hist_key_parts(key):
    """A guide history key -> (room_id, seq_id or None), or None to refuse."""
    if not isinstance(key, str) or not key.startswith(GUIDE_HIST_KEY_PREFIX):
        return None
    rest = key[len(GUIDE_HIST_KEY_PREFIX):]
    room_id, dot, seq_id = rest.partition(".")
    room = next((r for r in engines.rooms() if r.get("id") == room_id), None)
    if room is None:
        return None
    if dot:
        if room.get("kind") != "cutting" or not seq_valid_id(seq_id):
            return None
        return room_id, seq_id
    return room_id, None


def _guide_hist_path(key):
    parts = _guide_hist_key_parts(key)
    if parts is None:
        raise ValueError("That is not a guide history key.")
    room_id, seq_id = parts
    return os.path.join(GUIDE_HIST_DIR, room_id + (("." + seq_id) if seq_id else "") + ".json")


def _guide_hist_read(key):
    """Caller holds GUIDE_HIST_LOCK. -> (generation, history). (0, []) when
    nothing is stored yet, or the file is damaged -- never guessed at; an
    empty history is the same safe fallback a blocked-localStorage browser
    already sees today. UX-2 #8: the stored shape grew a "generation"
    counter alongside the turns (bumped only by a CLEAR, never by an
    ordinary save) -- a file saved before this field carries no "generation"
    key at all, so that case reads as generation 0, same as brand new."""
    path = _guide_hist_path(key)
    if not os.path.exists(path):
        return 0, []
    try:
        with open(path) as f:
            stored = json.load(f)
    except ValueError:
        return 0, []
    if isinstance(stored, list):          # pre-UX-2 #8 file: bare turns list
        return 0, stored
    if isinstance(stored, dict) and isinstance(stored.get("turns"), list):
        gen = stored.get("generation")
        return (gen if isinstance(gen, int) and not isinstance(gen, bool) else 0), stored["turns"]
    return 0, []


def _guide_hist_write(key, generation, hist):
    """Caller holds GUIDE_HIST_LOCK. Per-writer tmp name, fsync, then os.replace."""
    path = _guide_hist_path(key)
    tmp = "%s.%d.tmp" % (path, threading.get_ident())
    with open(tmp, "w") as f:
        json.dump({"generation": generation, "turns": hist}, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _guide_hist_turn_ok(turn):
    """The same shape the client's own guideHistLoad() keeps: {"role": "user"
    or "assistant", "content": <str>, ...}. Extra keys (w, done, neighbours,
    mode, topic, ...) ride along untouched -- the action "done" marker lives
    in one of them, which is what makes re-applying an action idempotent
    across a reload or a device switch."""
    return (isinstance(turn, dict) and turn.get("role") in ("user", "assistant")
            and isinstance(turn.get("content"), str))


def _guide_hist_trim(hist):
    """Cap at GUIDE_HIST_MAX_TURNS turns, then GUIDE_HIST_MAX_BYTES of JSON,
    oldest dropped first -- the same "drop the oldest" rule as the client's
    own GUIDE_HIST_MAX trim, just with the server's own numbers. Never drops
    the single newest turn just to fit the byte cap."""
    hist = hist[-GUIDE_HIST_MAX_TURNS:]
    while len(hist) > 1 and len(json.dumps(hist).encode("utf-8")) > GUIDE_HIST_MAX_BYTES:
        hist = hist[1:]
    return hist


def guide_history_get(key):
    """GET /api/guide/history?key=<key> -> ({ok, history, generation}, code)."""
    if _guide_hist_key_parts(key) is None:
        return {"ok": False, "error": "That is not a guide history key."}, 400
    with GUIDE_HIST_LOCK:
        gen, hist = _guide_hist_read(key)
    return {"ok": True, "history": hist, "generation": gen}, 200


def guide_history_set(p):
    """POST /api/guide/history {key, history, generation} -> ({ok}, code).
    Replaces the stored array wholesale -- the client already computes the
    full trimmed array before every guideHistSave() call, so a whole-array
    replace matches its own logic exactly.

    UX-2 #8 (a clear wins everywhere): "generation" is optional, for
    back-compat with anything still sending the pre-#8 shape -- omitted, this
    behaves exactly as before. A client that DOES send it is declaring which
    generation its local copy was built from; if that is older than what the
    server now has (another device cleared this conversation since), the
    save is refused (409) and the CURRENT server state comes back in the
    body so the caller can adopt the clear instead of re-uploading its
    stale, longer copy over it."""
    if not isinstance(p, dict):
        return {"ok": False, "error": "Send a JSON object."}, 400
    key = p.get("key")
    if _guide_hist_key_parts(key) is None:
        return {"ok": False, "error": "That is not a guide history key."}, 400
    hist = p.get("history")
    if not isinstance(hist, list) or not all(_guide_hist_turn_ok(t) for t in hist):
        return {"ok": False, "error": "\"history\" must be a list of {\"role\", \"content\", ...} turns."}, 400
    client_gen = p.get("generation")
    hist = _guide_hist_trim(hist)
    with GUIDE_HIST_LOCK:
        server_gen, server_hist = _guide_hist_read(key)
        if isinstance(client_gen, int) and not isinstance(client_gen, bool) and client_gen < server_gen:
            return {"ok": False, "error": "This conversation was cleared elsewhere. Adopting that clear.",
                    "history": server_hist, "generation": server_gen}, 409
        _guide_hist_write(key, server_gen, hist)
    return {"ok": True, "generation": server_gen}, 200


def guide_history_clear(p):
    """POST /api/guide/history/clear {key} -> ({ok, generation}, code). UX-2
    #8: THE clear action -- empties the stored turns and bumps the
    generation counter, so any device whose local copy predates this call
    (even a longer one) loses the CAS race in guide_history_set above and
    must adopt this clear on its next save."""
    if not isinstance(p, dict):
        return {"ok": False, "error": "Send a JSON object."}, 400
    key = p.get("key")
    if _guide_hist_key_parts(key) is None:
        return {"ok": False, "error": "That is not a guide history key."}, 400
    with GUIDE_HIST_LOCK:
        gen, _hist = _guide_hist_read(key)
        gen += 1
        _guide_hist_write(key, gen, [])
    return {"ok": True, "generation": gen}, 200


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
# FB-2: the room's one box sends everything typed in a writer mode here, so
# every writer may also just talk: one shared rule, appended to each writer's
# own system prompt (engines.parse_writer_reply reads the SAY line).
GUIDE_SKILL_SAY_RULE = ("If the user asked a question or said something that is not a request to make "
                        "something, write nothing: reply with one line, SAY: <your answer>, and nothing else.")
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
    with no prompt box, the picture field it writes about, and -- B3 -- the
    field ids it actually fills (from its own "keys" line-name -> field-id
    map, deduped, in the order those lines are declared), so the page can
    say "Writes: <labels>" under Help me write this."""
    w = engines.writer(cap, mode)
    if not w:
        return None
    out = {"label": w["label"]}
    if w.get("target"):
        tcap, tmode = _writer_target(w, cap, mode)
        out["target"] = _mode_target(tcap, tmode)
    for k in ("topic_label", "pictures", "topic_default"):
        if w.get(k):
            out[k] = w[k]
    if w.get("keys"):
        out["fills"] = list(dict.fromkeys(w["keys"].values()))
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
# of its own in context.neighbours, one line each, plus the previous shot's
# actual prompt and a "keep" line (the first beat's subjects and props, the
# light so far), so the shot keeps the story's continuity; the writer is told
# to write only its own beat (walkthrough 2: shot 1 got beat 2's words, the
# light changed shot to shot and the paper plane became a toy one).
GUIDE_NEIGHBOUR_KEYS = (("before", "Before"), ("after", "After"),
                        ("previous", "The previous shot's actual prompt"), ("keep", "Keep"))


def _skill_neighbours(context, cap, mode):
    """Validate context.neighbours -> (block of text or "", error or None).
    A beat in another engine's format loses that engine's task prefix."""
    n = (context or {}).get("neighbours") if isinstance(context, dict) else None
    if n is None:
        return "", None
    if not isinstance(n, dict) or set(n) - {k for k, _ in GUIDE_NEIGHBOUR_KEYS}:
        return "", ("The context's \"neighbours\" must be an object with \"before\", \"after\", "
                    "\"previous\" and/or \"keep\".")
    lines = []
    for k, word in GUIDE_NEIGHBOUR_KEYS:
        v = n.get(k)
        if v is None:
            continue
        if not isinstance(v, str) or len(v) > GUIDE_CONTEXT_VALUE_CHARS:
            return "", ("The neighbour \"%s\" must be text of at most %d characters."
                        % (k, GUIDE_CONTEXT_VALUE_CHARS))
        v = engines.foreign_prefix_stripped(cap, mode, " ".join(v.split()))
        if v:
            lines.append("%s: %s" % (word, v))
    if not lines:
        return "", None
    return ("The storyboard around this shot, for continuity only. Write THIS shot, and only its own "
            "beat (the Request below), not the others:\n" + "\n".join(lines) + "\nWrite THIS beat only; "
            "keep the listed subjects, props and light unless this beat changes them."), None


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
    # A writer with a `topic_default` (CHARS-1) needs no words of its own: its
    # pictures and the form are the request (no picture is answered by its
    # `missing` check). A topic box holding a whole earlier draft, too long to
    # be a request, counts as empty.
    default_topic = (engines.writer(cap, mode) or {}).get("topic_default")
    if default_topic and (topic is None or (isinstance(topic, str) and len(topic) > HELPER_TEXT_LIMIT)):
        topic = ""
    if not isinstance(topic, str) or not (topic.strip() or refs or default_topic):
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
        user += ("Request (THIS shot's beat, the only one to write): " if neighbours else "Request: ") \
            + engines.foreign_prefix_stripped(cap, mode, topic.strip() or w.get("topic_default") or "")
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
        sent = {"system": w["prompt"] + "\n\n" + GUIDE_SKILL_SAY_RULE, "user": text}
        if refs or attached:
            sent["pictures"] = len(urls)
        reply, finish = _guide_helper_chat([{"role": "system", "content": sent["system"]},
                                            {"role": "user", "content": _with_pictures(text, urls)}],
                                           max_tokens=w.get("max_tokens", 1024), retry_cut=True)
        cut.append(finish == "length")
        try:
            parsed = engines.parse_writer_reply(w, reply, prose_say=finish != "length")
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
        if "say" in parsed:
            body = {"ok": True, "say": parsed["say"], "sent": sent, "retried": False}
            if refs or attached:
                body["vision"] = vision
            return body, 200
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
    m = dict(models_for(lane), **nodes_for(lane))
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
        # the picture generator's ability is now MODE-specific
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
                if ftype in ("select", "pool_select") and val == "":
                    val = None
                if ftype in ("audio", "image", "image_list", "video_list", "model") and (
                        (isinstance(val, str) and not val.strip()) or val == []):
                    val = None
                if val is None:
                    continue
                # LORA-1: a "pool_select" value must be one the lane's own
                # discovered pool actually offers right now, same discipline
                # as "select"'s options check -- never a filename passed
                # straight into the graph unchecked.
                if ftype == "pool_select" and val not in pool_select_options(lane, f):
                    raise ValueError("%s is not available on %s." % (f.get("label", fid), lane["name"]))
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
    # E1: the Cutting Room's song slice (seq_generate/resolve_slot_sing), never
    # a room-form field. Only a graph that declares sing_along reads it.
    if p.get("sing_audio"):
        start = p.get("sing_start", 0.0)
        if (isinstance(start, bool) or not isinstance(start, (int, float))
                or not math.isfinite(start) or start < 0):
            return {"ok": False, "error": "The song's start must be a number of seconds, zero or more."}, 400
        args["sing_audio"], args["sing_start"] = str(p["sing_audio"]), float(start)
    # E2: the pack's join field (on by default), only ever on with a clip to join.
    join, kept = _join_spec("video", mode), _join_kept("video", mode, p)
    if join:
        args[join["field"]] = bool(kept)
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
    if kept:
        meta["join"] = {"dissolve": True, "overlap": kept}
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
            if ftype in ("select", "pool_select") and val == "":
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
            # LORA-1: same live-pool check as the legacy image path above.
            if ftype == "pool_select" and val not in pool_select_options(lane, f):
                raise ValueError("%s is not available on %s." % (f.get("label", fid), lane["name"]))
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
        # An upload name that is not in this lane's uploads is refused now,
        # not as a failed job later (run_process_job() checks again: a file
        # can still go missing before the job runs).
        for f in engines.fields(kind, mode) or []:
            val = args.get(f["id"]) if isinstance(f, dict) else None
            if f.get("type") not in ("audio", "image", "image_list", "video_list", "model") or not val:
                continue
            for name in (val if isinstance(val, list) else [val]):
                if not os.path.isfile(os.path.join(UPLOADS_DIR, lane["id"], os.path.basename(str(name)))):
                    return {"ok": False, "error": "There is no uploaded file called %s for %s. Upload it "
                            "(POST /api/upload) and use the name it answers with."
                            % (os.path.basename(str(name)), f.get("label") or f["id"])}, 400
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
            "model": engines.describe_mode(m, kind, mode), "model_file": m.get(role, ""),
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


# ponytail: process-lifetime cache -- a lane that wipes its input folder
# mid-session keeps the stale name until this app restarts; key it on the
# lane's boot id if that ever bites.
SING_UPLOADS = {}   # (lane id, song job id) -> the name that lane answered with
SING_DURATIONS = {}   # same key -> the song file's probed length in seconds
# RS3: the same probe, run once in the background when a take becomes the
# sequence's song, so the page can warn before Make. Keyed by the song JOB
# alone (SING_DURATIONS is per lane, and a GET has no lane to ask about).
SONG_SECONDS = {}      # song job id -> seconds
SONG_PROBE_TRIED = {}  # song job id -> when a background probe last started
SONG_PROBE_RETRY_S = 60.0


def song_seconds_known(job_id):
    """The song take's length if some probe already learned it, else None.
    Reads caches only -- never touches a file or the network (it runs in GET)."""
    if job_id in SONG_SECONDS:
        return SONG_SECONDS[job_id]
    for (_lane, jid), dur in list(SING_DURATIONS.items()):
        if jid == job_id:
            return dur
    return None


def _song_probe_worker(sid, slot_id, job_id):
    try:
        path = _cut_ensure_take_file(sid, slot_id, job_id)
        dur = _probe_duration(path)
        if dur is not None:
            SONG_SECONDS[job_id] = dur
    except Exception:
        pass   # unknown stays unknown: no warning, and Make (F4) still refuses


def probe_song_async(sid, slot_id, job_id):
    """RS3: probe a song take's length ONCE in the background. A no-op when it
    is already known, or a probe for it started less than a minute ago."""
    if not job_id or song_seconds_known(job_id) is not None:
        return
    now = time.time()
    if now - SONG_PROBE_TRIED.get(job_id, 0.0) < SONG_PROBE_RETRY_S:
        return
    SONG_PROBE_TRIED[job_id] = now
    threading.Thread(target=_song_probe_worker, args=(sid, slot_id, job_id), daemon=True).start()


def _song_clock(sec):
    tenths = int(round(sec * 10))
    return "%d:%04.1f" % (tenths // 600, (tenths % 600) / 10.0)


def resolve_slot_sing(seq, slot, target_lane):
    """E1 (R2): (lane file name, the take's inputs.sing record) for a shot
    singing along, else (None, None). The song reaches the lane the way a
    continue cable's clip does: the cut's own harvest retry gets the take onto
    local disk, carry() uploads it unchanged -- once per lane per song take."""
    use = _resolved_sing_use(seq, slot)
    if use is None:
        return None, None
    key = (target_lane["id"], use["song"])
    name = SING_UPLOADS.get(key)
    duration = SING_DURATIONS.get(key)
    if name is None or duration is None:
        song = seq_song_slot(seq)
        path = _cut_ensure_take_file(seq["id"], song["id"], use["song"])
        if duration is None:
            duration = _probe_duration(path)
            if duration is None:
                raise ValueError("Couldn't read the song's length.")
            SING_DURATIONS[key] = duration
        if name is None:
            with JOBS_LOCK:
                job = copy.deepcopy(JOBS.get(use["song"]))
            if job is None:
                raise ValueError("The song (%s) is not made yet." % song["id"])
            name, _note = carry(job, 0, target_lane, fit=None, cache_path=path)
            SING_UPLOADS[key] = name
    entry = _resolved_sing_entry(seq, slot)
    fps = entry["fps"]
    if entry["end"] > duration + 1.0 / fps:
        raise ValueError("This shot would sing to %s but the song is %s long. Start it earlier or make it shorter."
                         % (_song_clock(entry["end"]), _song_clock(duration)))
    return name, use


def _audio_led_slice(seq, slot, lane):
    """Audio-led generate: cut this shot's window out of the master sound, upload
    it to the lane the shot will run on, and return (uploaded name, window).
    Raises ValueError with a plain sentence for every refusal."""
    if not CAN_CUT:
        raise ValueError(CUT_REASON)
    sid = seq["id"]
    mpath, mstart = _audio_led_master_source(seq)
    derived = copy.deepcopy(seq)
    _audio_led_derive(derived)
    win = next((s.get("window") for s in derived.get("slots") or [] if s.get("id") == slot.get("id")), None)
    if not win:
        raise ValueError("Shot %s has no place on the master sound." % slot.get("id"))
    outdir = os.path.join(SEQ_MEDIA_DIR, sid, "audio")
    os.makedirs(outdir, exist_ok=True)
    wav = os.path.join(outdir, "%s.wav" % slot["id"])
    r = subprocess.run([FFMPEG_BIN, "-y", "-hide_banner", "-ss", "%.6f" % (mstart + win["start"]), "-i", mpath,
                        "-af", "apad", "-t", "%.6f" % (win["len"] + 1.0 / AUDIO_LED_FPS),
                        "-ar", "44100", "-ac", "2", wav], capture_output=True, text=True, timeout=120)
    if r.returncode != 0 or not os.path.isfile(wav):
        raise ValueError("The master sound could not be cut for shot %s." % slot["id"])
    with open(wav, "rb") as f:
        payload = f.read()
    res = http_post_multipart(
        lane_url(lane, "/upload/image"),
        {"type": "input", "overwrite": "false"},
        [("image", "%s_%s.wav" % (sid, slot["id"]), "audio/wav", payload)])
    if "_http_error" in res or not res.get("name"):
        raise ValueError("%s would not take the sound clip. Try the other lane." % lane["name"])
    name = res["name"]
    if res.get("subfolder"):
        name = res["subfolder"] + "/" + name
    return name, {"start": win["start"], "len": win["len"]}


MASTER_EXTS = (".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus")
MASTER_MAX_BYTES = 200 * 1024 * 1024


def seq_master_import(sid, rev, filename, data):
    """POST /api/sequence/master (multipart id, rev, file): keep the user's own sound file as this
    sequence's master. Opt-in: nothing happens to a sequence unless it is called, and the master only
    leads an audio-led cut. Returns (body, http code)."""
    if not seq_valid_id(sid):
        return {"ok": False, "error": "That is not a sequence id."}, 400
    if not CAN_CUT:
        return {"ok": False, "error": CUT_REASON}, 400
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", os.path.basename(filename or ""))[:120]
    ext = os.path.splitext(name)[1].lower()
    if ext not in MASTER_EXTS:
        return {"ok": False, "error": "That is not a sound file I can use (wav, mp3, m4a, aac, flac, ogg or opus)."}, 400
    if not data or len(data) > MASTER_MAX_BYTES:
        return {"ok": False, "error": "The sound file is empty or larger than %d MB." % (MASTER_MAX_BYTES // (1024 * 1024))}, 400
    with SEQ_LOCK:
        seq = _seq_read(sid)
    if seq is None:
        return {"ok": False, "error": "There is no such sequence."}, 404
    if rev is not None and seq.get("rev") != rev:
        return {"ok": False, "error": "This sequence changed somewhere else. Here is how it is now.",
                "sequence": seq_derive(seq)}, 409
    adir = os.path.join(SEQ_MEDIA_DIR, sid, "audio")
    os.makedirs(adir, exist_ok=True)
    tmp = os.path.join(adir, ".master" + ext + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
    info = _probe_json(tmp)
    try:
        dur = float(info["format"]["duration"]) if info else None
    except (KeyError, TypeError, ValueError):
        dur = None
    if info is None or _probe_audio_stream(info) is None or not dur or dur <= 0:
        os.remove(tmp)
        return {"ok": False, "error": "That file has no sound I can read."}, 400
    for old in os.listdir(adir):
        if old.startswith("master."):
            os.remove(os.path.join(adir, old))
    dest = os.path.join(adir, "master" + ext)
    os.replace(tmp, dest)
    with SEQ_LOCK:
        seq = _seq_read(sid)
        if seq is None:
            return {"ok": False, "error": "There is no such sequence."}, 404
        seq["master"] = {"file": "audio/master" + ext, "name": name, "seconds": round(dur, 3), "start": 0.0}
        seq["rev"] += 1
        seq["updated"] = time.time()
        _seq_write(seq)
    return seq_derive(seq), 200


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
    # E2: a shot that never set the pack's join field takes the Cutting Room's default.
    for k, v in _join_seq_defaults(slot.get("cap"), slot.get("mode")).items():
        if slot_values.get(k) is None:
            slot_values[k] = v
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
        sing_audio, sing_use = resolve_slot_sing(seq, slot, lane)
    except ValueError as e:
        return {"ok": False, "error": str(e)}, 400
    field_values.update(cable_values)
    p.update(field_values)
    p["values"].update(field_values)
    if sing_use:
        p["sing_audio"], p["sing_start"] = sing_audio, sing_use["start"]
    audio_inputs = {}
    # Opt-in audio-led mode: an LTX video shot with no sound file of its own is
    # driven by its window of the sequence's master sound.
    if (seq.get("audio_led") and slot.get("lane") == "video" and slot.get("mode") == "ltx"
            and not str(slot_values.get("audio_slice") or "").strip() and not sing_use):
        try:
            led_name, led_window = _audio_led_slice(seq, slot, lane)
        except ValueError as e:
            return {"ok": False, "error": str(e)}, 400
        p["audio_slice"] = led_name
        p["values"]["audio_slice"] = led_name
        audio_inputs = {"audio_window": led_window}
    body, code = generate(p)
    if not body.get("ok"):
        return body, code
    job = body["job"]
    beat = next((b for b in seq.get("beats") or [] if b.get("id") == slot.get("beat_id")), None)
    inputs = {"refs": ref_uses, "cables": cable_uses}
    inputs.update(audio_inputs)
    if sing_use:
        inputs["sing"] = sing_use
    # E2 (R2): only when the render kept the overlap (join field on + cabled).
    overlap = _join_kept(slot.get("cap"), slot.get("mode"), p)
    if overlap:
        inputs["join"] = {"dissolve": True, "overlap": overlap}
    seq_add_take(sid, slot_id, job["id"], beat_rev=beat.get("rev") if beat else None, inputs=inputs)
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
    # A temp file of its own: the background Sing along probe and a Make can
    # copy the same take at once, and a shared "<dest>.tmp" made the second
    # rename fail. Same bytes either way; the last rename wins.
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dest), prefix=os.path.basename(dest) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
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
                # Recording a take's local cache path is not a user-visible change,
                # so NO rev bump: this runs in the background (RS3's song probe),
                # and a bump here 409s whatever op the person sends next on the rev
                # they already read. Every op re-reads the sequence from disk under
                # SEQ_LOCK, so the field cannot be lost.
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
    audio_led = bool(seq.get("audio_led"))
    shots, probed, sung, joined = [], [], [], []
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
        if audio_led and slot.get("trim") is None:
            # Audio-led: an untrimmed shot keeps exactly its planned window (an 8n+1 take keeps 8n),
            # so the windows tile and the picture never drifts against the master.
            planned = _slot_planned_len(slot)
            if 0 < planned < length:
                length = planned
        with JOBS_LOCK:
            job = JOBS.get(job_id) or {}
        shots.append({"slot_id": slot["id"], "job_id": job_id, "licence": job.get("licence")})
        probed.append({"slot_id": slot["id"], "path": path, "in": in_point, "len": length,
                        "w": vw, "h": vh, "has_audio": _probe_audio_stream(info) is not None,
                        "title": slot.get("title")})
        # E1 (R4): what this take was made singing (its own record, not the
        # slot's current state), and its pictures' own exact length.
        made = next((t.get("inputs") for t in slot.get("takes") or [] if t.get("job_id") == job_id), None)
        try:
            vdur = float(vstream.get("duration"))
        except (TypeError, ValueError):
            vdur = None
        sung.append({"sing": made.get("sing") if isinstance(made, dict) else None,
                     "whole": slot.get("trim") is None, "vdur": vdur})
        # E2: what this take's render did at its join (its own record).
        try:
            num, den = (vstream.get("r_frame_rate") or "24/1").split("/")
            fps = float(num) / float(den)
        except (ValueError, ZeroDivisionError):
            fps = 24.0
        joined.append({"slot": slot, "made": made if isinstance(made, dict) else {}, "dur": duration,
                       "vdur": vdur, "fps": fps if fps > 0 else 24.0})

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
    # Audio-led (opt-in): the master sound leads the cut, so the shots are joined hard on its windows
    # (no dissolve overlaps) and a song-sung soundtrack is not built; the master IS the soundtrack.
    if audio_led:
        dissolves, join_notes = 0, []
    else:
        dissolves, join_notes = _cut_join_plan(seq, clip_plans, joined, sung)
    bed_path = None
    master_start = 0.0
    if audio_led:
        mf_in = seq.get("master")
        if isinstance(mf_in, dict) and mf_in.get("file"):
            # An imported sound file is the master (it wins over a picked sound shot).
            try:
                bed_path, master_start = _audio_led_master_source(seq)
            except ValueError as e:
                return {"ok": False, "error": str(e)}, 400
            if _probe_json(bed_path) is None:
                return {"ok": False, "error": "The master sound file could not be read — it may be corrupt."}, 400
            shots.append({"slot_id": None, "job_id": None, "licence": None, "role": "master", "name": mf_in.get("name")})
    # The bed is the first SOUND-lane slot (timeline order) with a pick --
    # whether or not it is local YET. It gets the same cut-time harvest
    # retry as a video shot (R2), never a silent "no bed" (owner review,
    # 2026-09-23): a picked bed is a decision the user made, so a copy
    # failure is refused, naming the slot, exactly like a missing video take.
    bed_slot = next((s for s in seq.get("slots") or [] if s.get("lane") == "sound" and s.get("pick")), None)
    if bed_slot is not None and bed_path is None:
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
                      "role": "master" if audio_led else "bed"})
    if audio_led and bed_path is None:
        return {"ok": False, "error": "This sequence is audio-led, so it needs a master sound to lead the cut: "
                "pick a take on a sound shot, or add a sound file."}, 400

    # E1 (R4): with a song and at least one shot made singing it, the song
    # itself is the soundtrack -- unless the timeline cannot line every such
    # shot up on one continuous track, when each shot keeps its own sound.
    song_plan, song_note = None, None
    if not audio_led and bed_slot is not None and any(x["sing"] and x["sing"].get("song") == bed_slot["pick"] for x in sung):
        song_plan = _cut_song_plan(clip_plans, sung, bed_slot["pick"])
        if song_plan is None:
            song_note = ("The song could not run as one track under the singing shots (the timeline's order "
                         "or gaps break its timing), so each shot keeps its own sound.")
            bed_path = None
            shots = [s for s in shots if s.get("role") != "bed"]
        else:
            for clip, length in zip(clip_plans, song_plan["lens"]):
                clip["len"] = length

    cut_id = "k" + uuid.uuid4().hex[:8]
    out_path = os.path.join(SEQ_MEDIA_DIR, sid, "cuts", cut_id + ".mp4")
    entry = {"id": cut_id, "status": "queued", "made": time.time(), "file": None, "log": "",
             "shots": shots, "left_out": left_out, "loudness": None}
    if song_plan:
        entry["soundtrack"] = "song"
    elif song_note:
        entry["soundtrack"] = "clips"
        entry["soundtrack_note"] = song_note
    if dissolves or join_notes:
        entry["dissolves"] = dissolves
        entry["join_note"] = " ".join(
            (["%d shot join%s blended across the shared frames." % (dissolves, "" if dissolves == 1 else "s")]
             if dissolves else []) + join_notes)
    if audio_led:
        entry["audio_led"] = True
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
                     kwargs={"song_plan": song_plan, "audio_led": audio_led, "master_start": master_start}, daemon=True).start()
    return {"ok": True, "cut_id": cut_id}, 200


CUT_SING_TOLERANCE = 1.0 / 48   # half a frame at 24 fps


def _cut_join_plan(seq, clip_plans, joined, sung):
    """E2 (R3): per seam, from the PICKED take's own `inputs.join` (never the
    slot's current field). A take that kept its overlap and follows, in the
    cut, the very take its video jack was cabled from cross-dissolves over the
    overlap: clip["xfade"] = the overlap in seconds (the timeline stays the
    hard join of the trimmed take). Any other such take -- first in the cut,
    not following its source take, or trims that leave less than the whole
    overlap -- joins with a straight cut and its overlap frames are dropped,
    with one sentence per seam. -> (dissolves, notes)."""
    dissolves, notes = 0, []
    for i, (clip, j) in enumerate(zip(clip_plans, joined)):
        rec = j["made"].get("join")
        if not (isinstance(rec, dict) and rec.get("dissolve") and rec.get("overlap")):
            continue
        fps, frames = j["fps"], int(rec["overlap"])
        over, half = frames / fps, 0.5 / fps
        sung[i]["join_over"] = over
        slot, reason = j["slot"], None
        if i == 0:
            reason = ("Shot %s starts the cut, so its first %d frames (the end of the shot it continues) "
                      "are left out." % (slot["id"], frames))
        else:
            prev, pj = clip_plans[i - 1], joined[i - 1]
            jacks = {x["field"] for x in slot_jacks(slot) if x.get("type") == "video"}
            cabled = any(c.get("to") == slot["id"] and c.get("from") == pj["slot"]["id"] and c.get("field") in jacks
                         for c in seq.get("cables") or [])
            used = j["made"].get("cables") or {}
            if not (cabled and any(used.get(f) == pj["slot"].get("pick") for f in jacks)):
                reason = ("Shot %s was not made from the take of shot %s before it in the cut, so they join "
                          "with a straight cut and its first %d frames are left out."
                          % (slot["id"], pj["slot"]["id"], frames))
            elif (pj["dur"] - (prev["in"] + prev["len"]) > half or clip["in"] > half
                  or prev["len"] - prev.get("xfade", 0.0) < over - half or clip["len"] < over + 1.0 / fps - half):
                reason = ("Shot %s joins shot %s with a straight cut: the trims leave less than the %d frames "
                          "they share to blend across, so its first %d frames are left out."
                          % (slot["id"], pj["slot"]["id"], frames, frames))
        if reason is None:
            # The seam is 22 FRAMES of pictures: the shot before ends at its
            # pictures' own end, not an audio tail past it, so the overlap's
            # sound lines up with the sound it repeats.
            if pj["vdur"] and prev["in"] + prev["len"] > pj["vdur"]:
                prev["len"] = pj["vdur"] - prev["in"]
            clip["xfade"] = over
            dissolves += 1
            continue
        drop = max(0.0, over - clip["in"])
        if drop >= clip["len"] - half:
            notes.append("Shot %s's trim sits inside the %d frames it shares with the shot before, so it "
                         "plays as trimmed." % (slot["id"], frames))
            continue
        clip["in"] += drop
        clip["len"] -= drop
        if sung[i]["vdur"]:
            sung[i]["vdur"] -= drop
        notes.append(reason)
    return dissolves, notes


def _cut_song_plan(clip_plans, sung, song_job):
    """R4: where the song sits under the cut. A shot made singing `song_job`
    shows song time (its start + lead_in + its own trim in-point) at its first
    cut frame; every such shot must agree on ONE offset (song time minus cut
    time, within half a frame) for one continuous track. A whole-take shot's
    length becomes its pictures' own exact length, so the song and the
    pictures never drift apart. -> {"offset", "lens", "sing"}, or None."""
    t, offset, lens, sing = 0.0, None, [], []
    for clip, made in zip(clip_plans, sung):
        rec = made["sing"]
        on = bool(rec and rec.get("song") == song_job)
        length = made["vdur"] if on and made["whole"] and made["vdur"] else clip["len"]
        # E2 (R4): a dissolve seam overlaps the shot before by its overlap, and
        # a take that kept its overlap starts that much earlier in its slice.
        t -= clip.get("xfade", 0.0)
        if on:
            d = rec["start"] + rec.get("lead_in", 0.0) - made.get("join_over", 0.0) + clip["in"] - t
            if offset is None:
                offset = d
            elif abs(d - offset) > CUT_SING_TOLERANCE:
                return None
        lens.append(length)
        sing.append(on)
        t += length
    return {"offset": offset, "lens": lens, "sing": sing}


def _song_track_chain(clip_plans, song_plan):
    """R4: the song as the soundtrack -- shifted so cut time t plays song time
    t + offset; full level under the singing shots (whose own audio is
    silenced, never doubled), the bed's CUT_BED_GAIN_DB everywhere else."""
    parts = ["aformat=sample_rates=%d:channel_layouts=%s" % (CUT_SAMPLE_RATE, CUT_CHANNEL_LAYOUT)]
    offset = song_plan["offset"]
    if offset >= 0:
        parts += ["atrim=start=%.6f" % offset, "asetpts=PTS-STARTPTS"]
    else:
        ms = int(round(-offset * 1000))
        parts.append("adelay=delays=%d|%d" % (ms, ms))
    spans, t = [], 0.0
    for clip, on in zip(clip_plans, song_plan["sing"]):
        t -= clip.get("xfade", 0.0)
        if on:
            spans.append("between(t,%.6f,%.6f)" % (t, t + clip["len"]))
        t += clip["len"]
    # 1 ms frames, so the level switches within a millisecond of a shot change.
    parts += ["asetnsamples=n=%d:p=0" % (CUT_SAMPLE_RATE // 1000),
              "volume=volume='if(%s,1,%.6f)':eval=frame" % ("+".join(spans), 10 ** (CUT_BED_GAIN_DB / 20.0))]
    return ",".join(parts)


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
    holding a space or colon. ffmpeg's filter syntax reads `:` as its
    option separator, so a Windows drive path gets forward slashes and an
    escaped drive colon; any other path comes back exactly as before."""
    text = str(value)
    if re.match(r"^[A-Za-z]:[\\/]", text):      # a Windows drive path, and only that
        text = text.replace("\\", "/")
        text = text[0] + "\\:" + text[2:]
    return "'" + text.replace("'", "'\\''") + "'"


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
    if clip.get("dissolve"):
        # E2: xfade needs one frame rate and time base on both sides, and each
        # side exactly its cut length (a last frame held, as concat's cfr does),
        # so the seams sit where the timeline says and never drift.
        parts += ["fps=24", "tpad=stop_mode=clone:stop=-1", "trim=duration=%.6f" % clip["len"]]
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
        if clip.get("dissolve"):
            parts.append("apad=whole_dur=%.6f" % clip["len"])   # E2: exactly its cut length, as above
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


def _run_cut(sid, cut_id, clip_plans, bed_path, out_path, out_w, out_h, song_plan=None, audio_led=False, master_start=0.0):
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
            if clip.get("xfade") or (i + 1 < n and clip_plans[i + 1].get("xfade")):
                clip = dict(clip, dissolve=True)
            vchain, ttmp = _clip_video_chain(i, clip, out_w, out_h)
            if ttmp:
                title_tmps.append(ttmp)
            v_chains.append(vchain)
            # E1 (R4): a singing shot's own audio IS the song -- silenced here,
            # the song track below plays under it instead.
            sings = bool(song_plan and song_plan["sing"][i])
            if not audio_led:
                a_chains.append(_clip_audio_chain(i, dict(clip, has_audio=False) if sings else clip))
        if audio_led:
            # Audio-led (opt-in): the clips' own audio is dropped; the master
            # sound plays from 0 at full level for exactly the picture's length.
            total = sum(c["len"] for c in clip_plans)
            master = ("[%d:a]aformat=sample_rates=%d:channel_layouts=%s,atrim=start=%.6f:duration=%.6f,"
                      "asetpts=PTS-STARTPTS,apad=whole_dur=%.6f[acat]"
                      % (bed_index, CUT_SAMPLE_RATE, CUT_CHANNEL_LAYOUT, master_start, total, total))
            graph = v_chains + ["%sconcat=n=%d:v=1:a=0[vcat]" % ("".join("[v%d]" % i for i in range(n)), n), master]
            audio_graph = [master]
        else:
            # E2 (R3): a dissolve seam joins its clip onto the run before it (xfade +
            # an equal-gain acrossfade over the overlap); runs are then concatenated.
            runs, xv, xa, run_len = [], [], [], 0.0
            for i, clip in enumerate(clip_plans):
                if clip.get("xfade") and runs:
                    d, off = clip["xfade"], run_len - clip["xfade"]
                    xv.append("[%s][v%d]xfade=transition=fade:duration=%.6f:offset=%.6f[vx%d]" % (runs[-1][0], i, d, off, i))
                    # equal GAIN (tri): the overlap's sound is the shot before's own
                    # tail re-rendered, i.e. correlated; equal power would swell ~3 dB.
                    xa.append("[%s][a%d]acrossfade=d=%.6f:c1=tri:c2=tri[ax%d]" % (runs[-1][1], i, d, i))
                    runs[-1], run_len = ("vx%d" % i, "ax%d" % i), off + clip["len"]
                else:
                    runs.append(("v%d" % i, "a%d" % i))
                    run_len = clip["len"]
            concat_inputs = "".join("[%s][%s]" % r for r in runs)
            graph = v_chains + a_chains + xv + xa + ["%sconcat=n=%d:v=1:a=1[vcat][acat]" % (concat_inputs, len(runs))]
        # A second, AUDIO-ONLY concat (never producing [vcat]) for the
        # measurement pass below: on ffmpeg 4.4.2, a filter_complex output
        # pad that is built but never `-map`ped ("Filter concat:out:v0 has
        # an unconnected output") is a hard error, not just a warning, so
        # the measurement pass -- which only ever maps the measured audio,
        # never [vcat] -- needs a graph that never creates an unmapped
        # video pad in the first place. Bonus: ffmpeg then never decodes
        # the video streams for this pass at all.
        if not audio_led:
            audio_graph = a_chains + xa + ["%sconcat=n=%d:v=0:a=1[acat]" % ("".join("[%s]" % r[1] for r in runs), len(runs))]

        def _mix_bed(stmts, acat_label):
            stmts = list(stmts)
            if song_plan:
                stmts.append("[%d:a]%s[bed]" % (bed_index, _song_track_chain(clip_plans, song_plan)))
            else:
                stmts.append("[%d:a]aformat=sample_rates=%d:channel_layouts=%s,volume=%ddB[bed]"
                             % (bed_index, CUT_SAMPLE_RATE, CUT_CHANNEL_LAYOUT, CUT_BED_GAIN_DB))
            stmts.append("[%s]aformat=sample_rates=%d:channel_layouts=%s[acatf]"
                         % (acat_label, CUT_SAMPLE_RATE, CUT_CHANNEL_LAYOUT))
            stmts.append("[acatf][bed]amix=inputs=2:duration=first:normalize=0[amixed]")
            return stmts, "amixed"

        final_audio = "acat"
        if bed_path and not audio_led:
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
    if not rel or not isinstance(rel, str) or _has_dotdot(rel):
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

def own_host_names(local_ip=None):
    """The names this computer answers to as far as the server knows them: the loopback
    names, the local address a connection arrived on (when given) and the machine's own
    host name. The request guard below and Setup's "is ComfyUI on this computer?" hint
    both read this one set."""
    names = {"localhost", "127.0.0.1", "::1"}
    if local_ip:
        names.add(local_ip.lower())
    gname = socket.gethostname().lower()
    return names | {gname, gname + ".local"}


def host_is_this_computer(host, local_ip=None):
    """True when `host` (a typed address or computer name) is this computer by those names,
    any loopback address (127.x, ::1), or any other address this computer holds (its LAN IP
    typed in): only an address of this computer's own can be bound to."""
    h = str(host or "").strip().lower().strip("[]")
    if h in own_host_names(local_ip):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    if ip.is_loopback:
        return True
    try:
        with socket.socket(socket.AF_INET6 if ip.version == 6 else socket.AF_INET) as s:
            s.bind((h, 0))
        return True
    except OSError:
        return False


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
    try:                                   # the local address this connection
        local_ip = handler.connection.getsockname()[0]   # arrived on,
    except Exception:                      # so a LAN IP needs no config
        local_ip = None
    allowed = own_host_names(local_ip)
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
    if handler.path.split("?")[0] in ("/api/upload", "/api/sequence/master"):
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


# ---------------------------------------------------------------------------
# W1 Setup: the first-run questions (setup.html) behind /api/setup/*. Live
# ONLY in SETUP_MODE (no config file): every route here 404s once a config
# exists. The probes take a host + port or an http(s) URL typed on the Setup
# page, time out in 4 s or less, and hand back only the fields the page shows
# -- never a raw response body. The page sends answers, never JSON to write:
# the config is built here and checked by validate_config() before writing.
# ---------------------------------------------------------------------------

SETUP_PROBE_TIMEOUT = 4.0
SETUP_HELPER_PROBE_TIMEOUT = 3.0
SETUP_READ_LIMIT = 1024 * 1024
SETUP_COMFY_PORTS = (8188, 8000)   # ComfyUI Desktop's port first, then portable/manual's default
SETUP_HELPER_CANDIDATES = (("Ollama", "http://127.0.0.1:11434/v1"),
                           ("LM Studio", "http://127.0.0.1:1234/v1"),
                           ("llama.cpp", "http://127.0.0.1:8080/v1"))
SETUP_TEST_MESSAGE = "Reply with the word ready."
SETUP_TEST_SECONDS = 60.0   # "Test it", whole exchange: a model still loading needs this long
SETUP_EXISTS = "A settings file already exists."
_SETUP_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.\-]{0,251}[A-Za-z0-9])?$")
SETUP_LOCK = threading.Lock()
SETUP_HELPER_TRIED = {}   # (url, model) -> True when "Test it" got a reply
SETUP_COMFY_SEEN = set()  # (host, port) a ComfyUI probe found: the only addresses W2's rooms step reads
SETUP_POOLS = {}          # (host, port) -> its model pools, read once per Setup session
SETUP_POOLS_TIMEOUT = 20.0              # the whole /object_info: big installs take seconds to list
SETUP_POOLS_READ_LIMIT = 64 * 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None   # a probe answers for the address typed, never somewhere else


_SETUP_OPENER = urllib.request.build_opener(_NoRedirect)


def _setup_get_json(url, timeout, limit=SETUP_READ_LIMIT):
    """GET url -> parsed JSON (dict or list), or raises. Reads at most `limit`
    bytes (1 MiB unless said), and the whole exchange ends within `timeout` seconds."""
    deadline = time.monotonic() + timeout
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with _SETUP_OPENER.open(req, timeout=timeout) as r:
        raw = _read_by(r, deadline, limit)
    return json.loads(raw.decode("utf-8"))


def _setup_short(v, n):
    return v.strip()[:n] if isinstance(v, str) else ""


def _setup_address(host, port):
    """A typed host + port (or an http(s)://host:port the user pasted into the
    host box) -> (host, port) or raises ValueError with a plain sentence."""
    host = str(host or "").strip()
    if "://" in host:
        parts = urllib.parse.urlsplit(host)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("Type an address like 127.0.0.1 and a port like 8188.")
        try:
            port = port or parts.port
        except ValueError:
            raise ValueError("That port is not a number from 1 to 65535.")
        host = parts.hostname
    if not _SETUP_HOST_RE.match(host):
        raise ValueError("Type an address like 127.0.0.1 or a computer name, with no spaces.")
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise ValueError("That port is not a number from 1 to 65535.")
    if not 1 <= port <= 65535:
        raise ValueError("That port is not a number from 1 to 65535.")
    return host, port


def _setup_comfy_stats(host, port):
    """-> the fields the page shows, or None when no ComfyUI answered there."""
    try:
        stats = _setup_get_json("http://%s:%d/system_stats" % (host, port), SETUP_PROBE_TIMEOUT)
    except Exception:
        return None
    if not isinstance(stats, dict) or not isinstance(stats.get("system"), dict):
        return None
    devs = stats.get("devices") if isinstance(stats.get("devices"), list) else []
    dev = devs[0] if devs and isinstance(devs[0], dict) else {}
    gpu = _setup_short(dev.get("name"), 120)
    vram = dev.get("vram_total")
    vram_gb = (round(vram / 2 ** 30, 1)
               if isinstance(vram, (int, float)) and not isinstance(vram, bool) and vram > 0 else None)
    # ComfyUI names a device like "cuda:0 <card name> : <allocator>": the card
    # name alone is the suggested lane name, which the user can change.
    name = re.sub(r"^\S+:\d+\s+", "", gpu).split(" : ")[0].strip()
    SETUP_COMFY_SEEN.add((host, port))
    return {"host": host, "port": port, "gpu": gpu or None, "vram_gb": vram_gb,
            "version": _setup_short(stats["system"].get("comfyui_version"), 40) or None,
            "name": name or "ComfyUI"}


def setup_probe_comfy(body, local_ip=None):
    """{host?, port?} -> the ComfyUI found. No host: 127.0.0.1 on 8188, then 8000.
    `this_computer` says whether that address is this computer (see host_is_this_computer)."""
    res, code = _setup_probe_comfy(body)
    if res.get("ok"):
        res["found"]["this_computer"] = host_is_this_computer(res["found"]["host"], local_ip)
    return res, code


def _setup_probe_comfy(body):
    if body.get("host"):
        try:
            host, port = _setup_address(body.get("host"), body.get("port"))
        except ValueError as e:
            return {"ok": False, "error": str(e)}, 400
        found = _setup_comfy_stats(host, port)
        if not found:
            return {"ok": False, "error": "No ComfyUI answered at %s:%d." % (host, port)}, 200
        return {"ok": True, "found": found}, 200
    for port in SETUP_COMFY_PORTS:
        found = _setup_comfy_stats("127.0.0.1", port)
        if found:
            return {"ok": True, "found": found}, 200
    return {"ok": False, "error": "No ComfyUI answered on this computer (ports %s)."
            % " or ".join(str(p) for p in SETUP_COMFY_PORTS)}, 200


def _setup_helper_url(url):
    """A typed guide address -> its OpenAI-compatible base URL (".../v1"), or
    raises ValueError. A bare http://host:port gets "/v1" added."""
    url = str(url or "").strip().rstrip("/")
    parts = urllib.parse.urlsplit(url)
    try:
        parts.port
    except ValueError:
        raise ValueError("That address has a port that is not a number.")
    if (parts.scheme not in ("http", "https") or not parts.hostname or "@" in parts.netloc
            or parts.query or parts.fragment or len(url) > 300):
        raise ValueError("Type the guide's address like http://127.0.0.1:11434/v1.")
    return url if parts.path else url + "/v1"


def _setup_helper_models(url):
    """GET <url>/models -> a list of model names, or None when nothing
    OpenAI-compatible answered."""
    try:
        raw = _setup_get_json(url + "/models", SETUP_HELPER_PROBE_TIMEOUT)
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    names = []
    for key, fields in (("data", ("id",)), ("models", ("model", "name"))):
        for m in raw.get(key) if isinstance(raw.get(key), list) else []:
            for fld in fields:
                n = _setup_short(m.get(fld) if isinstance(m, dict) else None, 200)
                if n:
                    if n not in names:
                        names.append(n)
                    break
    return names[:50]


def setup_probe_helper(body):
    """{url?} -> the guides found: that address, or the usual ones on this machine."""
    if body.get("url"):
        try:
            cands = [("", _setup_helper_url(body.get("url")))]
        except ValueError as e:
            return {"ok": False, "error": str(e)}, 400
    else:
        cands = list(SETUP_HELPER_CANDIDATES)
    results = [None] * len(cands)

    def one(i, url):
        results[i] = _setup_helper_models(url)
    threads = [threading.Thread(target=one, args=(i, u), daemon=True) for i, (_, u) in enumerate(cands)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(SETUP_HELPER_PROBE_TIMEOUT + 1)
    found = [{"label": label, "url": url, "models": models}
             for (label, url), models in zip(cands, results) if models is not None]
    if not found:
        where = (cands[0][1] if body.get("url")
                 else "this computer (%s)" % ", ".join(label for label, _ in cands))
        return {"ok": False, "found": [], "error": "No guide answered at %s." % where}, 200
    return {"ok": True, "found": found}, 200


def setup_test_helper(body):
    """{url, model} -> one short chat through _helper_chat_to: the reply, or
    one plain sentence. Remembers the outcome for setup_build_config()."""
    try:
        url = _setup_helper_url(body.get("url"))
    except ValueError as e:
        return {"ok": False, "error": str(e)}, 400
    model = _setup_short(body.get("model"), 200)
    helper = {"url": url, "model": model}
    parts = urllib.parse.urlsplit(url)
    where = "%s:%d" % (parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    try:
        _helper_connect_check_to(helper)
    except ValueError:
        SETUP_HELPER_TRIED[(url, model)] = False
        return {"ok": False, "error": "Nothing is answering at %s." % where}, 200
    try:
        text, _ = _helper_chat_to(helper, [{"role": "user", "content": SETUP_TEST_MESSAGE}], max_tokens=1024,
                                  opener=_SETUP_OPENER, total=SETUP_TEST_SECONDS)
    except ValueError:
        SETUP_HELPER_TRIED[(url, model)] = False
        return {"ok": False, "error": "The guide at %s did not answer the test: it may be busy, still "
                "loading, or the model name may be wrong." % where}, 200
    SETUP_HELPER_TRIED[(url, model)] = True
    reply = _clean_helper_text(text)[:200]
    return {"ok": True, "reply": reply or "(it answered with no words: a model that thinks first may "
            "need more room, but it is working)"}, 200


def setup_build_config(body):
    """The page's answers -> the config dict to write, or raises ValueError
    with a plain sentence. Nothing here is taken as raw JSON."""
    lanes = []
    comfy = body.get("comfy")
    if comfy:
        if not isinstance(comfy, dict):
            raise ValueError("The ComfyUI answer is not in the expected shape.")
        host, port = _setup_address(comfy.get("host"), comfy.get("port"))
        name = _setup_short(comfy.get("name"), 80)
        if not name or any(ord(c) < 32 for c in name):
            raise ValueError("Give your ComfyUI a name, like \"My computer\".")
        lanes.append({"id": "comfy", "name": name, "host": host, "port": port})
    if body.get("process") is True:
        lanes.append({"id": "cpu", "name": "This machine", "kind": "process", "caps": ["3d"]})
    if not lanes:
        raise ValueError("Setup needs somewhere to make things: a ComfyUI, or this machine for "
                         "the 3D turntable.")
    bind = {"local": "127.0.0.1", "network": "0.0.0.0"}.get(body.get("who"))
    if not bind:
        raise ValueError("Choose who can open Black Wire Forge.")
    cfg = {"bind": bind, "lanes": lanes}
    guide = body.get("helper")
    if guide:
        if not isinstance(guide, dict):
            raise ValueError("The guide answer is not in the expected shape.")
        url = _setup_helper_url(guide.get("url"))
        model = _setup_short(guide.get("model"), 200)
        tried = SETUP_HELPER_TRIED.get((url, model))
        if not (tried or (tried is False and guide.get("insist") is True)):
            raise ValueError("Test the guide first, or skip it for now.")
        cfg["helper"] = {"url": url, "model": model}
    try:
        validate_config(copy.deepcopy(cfg))
    except ConfigError as e:
        raise ValueError(str(e).replace(CONFIG_FILE, os.path.basename(CONFIG_FILE)))
    return cfg


def setup_preview(body):
    try:
        cfg = setup_build_config(body)
    except ValueError as e:
        return {"ok": False, "error": str(e)}, 400
    return {"ok": True, "name": os.path.basename(CONFIG_FILE), "text": json.dumps(cfg, indent=2) + "\n"}, 200


def _setup_write_new(text):
    """Create CONFIG_FILE holding text, never replacing one: a temp file in the
    same folder (O_EXCL), then a hard link to the real name, which fails if
    the name exists. Returns False when a file is already there."""
    folder = os.path.dirname(os.path.abspath(CONFIG_FILE))
    fd, tmp = tempfile.mkstemp(prefix=".config-setup-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp, CONFIG_FILE)
            return True
        except FileExistsError:
            return False
        except OSError:
            pass   # a filesystem without hard links: exclusive create instead
        try:
            out = os.open(CONFIG_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            return False
        try:
            with os.fdopen(out, "w") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
        except BaseException:
            os.unlink(CONFIG_FILE)   # ours, created just above: never leave half a file
            raise
        return True
    finally:
        os.unlink(tmp)


def _setup_restart():
    """Re-exec this server in-process so the new config loads (same pid,
    same arguments, same environment)."""
    time.sleep(0.5)   # let the response reach the page first
    log("Settings saved to %s -- restarting." % os.path.basename(CONFIG_FILE), "ok")
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable, [sys.executable] + sys.argv)


def setup_write(body):
    if os.path.lexists(CONFIG_FILE):
        return {"ok": False, "error": SETUP_EXISTS}, 409
    try:
        cfg = setup_build_config(body)
    except ValueError as e:
        return {"ok": False, "error": str(e)}, 400
    with SETUP_LOCK:
        if os.path.lexists(CONFIG_FILE):
            return {"ok": False, "error": SETUP_EXISTS}, 409
        try:
            written = _setup_write_new(json.dumps(cfg, indent=2) + "\n")
        except OSError:
            return {"ok": False, "error": "Could not save %s: check that its folder exists and can be "
                    "written to." % os.path.basename(CONFIG_FILE)}, 500
        if not written:
            return {"ok": False, "error": SETUP_EXISTS}, 409
    threading.Thread(target=_setup_restart, daemon=True).start()
    return {"ok": True, "name": os.path.basename(CONFIG_FILE)}, 200


def setup_state():
    return {"ok": True, "setup": True, "name": os.path.basename(CONFIG_FILE),
            "exists": os.path.lexists(CONFIG_FILE),
            "tools": {"blender": bool(shutil.which("blender")), "ffmpeg": bool(shutil.which("ffmpeg"))}}, 200


def _setup_pools(host, port):
    """The model pools of a ComfyUI a probe found, read the way discovery reads
    them (pool_names) from ONE bounded GET /object_info, kept for this Setup
    session. None when it cannot be read (then nothing is marked installed)."""
    key = (host, port)
    if key in SETUP_POOLS:
        return SETUP_POOLS[key]
    try:
        info = _setup_get_json("http://%s:%d/object_info" % key, SETUP_POOLS_TIMEOUT, SETUP_POOLS_READ_LIMIT)
    except Exception:
        return None
    if not isinstance(info, dict):
        return None
    pools = {p: pool_names(p, lambda node: info, "ComfyUI at %s:%d" % key) for p in POOL_NODES}
    SETUP_POOLS[key] = pools
    return pools


def setup_rooms(query):
    """W2 "What do you want to make first?": every room (rooms.json order) with
    the files its modes need, from the packs' own sources. ?host=&port= names
    the ComfyUI step 1 found; only an address a probe found is ever read, and
    `installed` stays null without one."""
    pools = None
    host, port = (query.get("host") or [""])[0], (query.get("port") or [""])[0]
    if host:
        try:
            host, port = _setup_address(host, port)
        except ValueError:
            host = ""
        if host and (host, port) in SETUP_COMFY_SEEN:
            pools = _setup_pools(host, port)
    with MODEL_DL_LOCK:
        models_root = MODEL_DL["models"]
    out = []
    for room in engines.rooms():
        entries, nodes, programs, licences, modes = [], [], [], [], []
        sources, words = {}, {}
        for m in room.get("modes") or []:
            n = engines.needs(m["cap"], m["mode"])
            modes.append({"mode": m["mode"], "label": engines.mode_words(m["cap"]).get(m["mode"], m["mode"])})
            sources.update(n["sources"])
            words.update(n["words"])
            for e in n["roles"]:
                if e not in entries:
                    entries.append(e)
            for x, into in ((n["nodes"], nodes), (n["programs"], programs), ([n["licence"]] if n["licence"] else [],
                                                                             licences)):
                for item in x:
                    if item not in into:
                        into.append(item)
        roles, counted, total = [], set(), 0
        need_files, need_bytes = set(), 0
        for e in entries:
            group = e if isinstance(e, list) else [e]
            srcs = [s for r in group for s in sources.get(r, [])]
            srcs.sort(key=lambda s: not s["run_by_us"])          # the recommended file first
            picks = ([] if pools is None else
                     [pick_model(pools.get(ROLE_POOL[r], []), ROLE_RULES[r], False) for r in group])
            found = next((x for x in picks if x), None)       # the file that actually covers the role
            installed = None if pools is None else bool(found)
            if models_root and not installed:   # W3: a file in the declared models folder, at its exact size
                on_disk = next((s for s in srcs if _model_on_disk(models_root, s)), None)
                installed = bool(on_disk)
                found = on_disk["file"] if on_disk else None
            role = {"role": group[0], "label": words.get(group[0], group[0]), "sources": srcs, "installed": installed,
                    "found": found.rsplit("/", 1)[-1] if found else None}
            if len(group) > 1:
                role["any_of"] = group
            roles.append(role)
            main = next((s for s in srcs if s["run_by_us"]), None)
            if main:
                key = (main["repo"], main["file"])
                if key not in counted:
                    counted.add(key)
                    total += main["size"]
                if not installed and key not in need_files:
                    need_files.add(key)
                    need_bytes += main["size"]
        out.append({"id": room["id"], "name": room.get("name", room["id"]), "group": room.get("group") or "",
                    "blurb": room.get("blurb", ""), "kind": room.get("kind") or "modes", "modes": modes,
                    "roles": roles, "nodes": nodes, "programs": programs, "licences": licences,
                    "total_bytes": total, "needs": {"files": len(need_files), "bytes": need_bytes},
                    "installed": None if (pools is None and not models_root) or not roles
                    else all(r["installed"] for r in roles)})
        if models_root:   # W3: what "Download" would fetch, shown on its button before anything starts
            plan = _room_plan(out[-1])
            to = []
            for x in plan:
                try:            # REV-A: the REAL destination, through any subfolder link, shown before Download
                    to.append({"file": _model_dest(models_root, x)[1].rsplit(os.sep, 1)[-1],
                               "where": "/".join(p for p in (x["folder"], x.get("subdir")) if p),
                               "dest": os.path.realpath(_model_dest(models_root, x)[1])})
                except ValueError:
                    pass
            out[-1]["download"] = {"files": len(plan), "bytes": sum(x["size"] for x in plan), "to": to}
    return {"ok": True, "comfy": pools is not None, "models": models_root, "rooms": out}, 200


# ---------------------------------------------------------------------------
# W3: one-click model downloads. Setup's step 1 lets the user DECLARE that
# ComfyUI runs on this computer and name its models folder (never guessed);
# a room card then fetches the room's files into it, one file at a time,
# through the LoRA download's core (_download_body: .part O_EXCL|O_NOFOLLOW,
# cancel) and its pinned-host opener. Each file's size is the one the packs'
# sources state (docs/MODELS.md): the server may send exactly that and no
# more. The queue lives in <data>/downloads.json and resumes with a Range
# request after the Setup re-exec or any restart. Only ever on a server bound
# to this computer alone. Nothing here runs a model or talks to ComfyUI.
# ---------------------------------------------------------------------------

MODELS_SUBFOLDERS = ("checkpoints", "diffusion_models", "loras", "vae", "text_encoders", "unet", "clip",
                     "upscale_models")
MODEL_DL_FILE = os.path.join(DATA_DIR, "downloads.json")
MODEL_DL_MARGIN = 2 * 1024 ** 3        # R3: room to spare on the models folder's drive
MODEL_DL_STALL = 30.0                  # no bytes for this long -> that file fails (security review finding 3)
MODEL_DL_STATES = ("queued", "running", "done", "failed", "skipped", "cancelled")
MODEL_DL_LOCK = threading.Lock()
MODEL_DL = {"models": None, "queue": [], "parts": []}   # what downloads.json holds
MODEL_DL_RUN = {"thread": None, "cancel": False, "bytes": 0}
_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.+-]*$")
DOWNLOADS_LOCAL_ONLY = ("Model downloads work only when Black Wire Forge is open to this computer alone "
                        "(\"bind\": \"127.0.0.1\" in %s)." % os.path.basename(CONFIG_FILE))


def _downloads_here():
    """R5: downloads are a same-machine, local-user feature."""
    return SETUP_MODE or BIND in ("127.0.0.1", "::1", "localhost")


def _gb(n):
    return "%.1f GB" % (n / 1e9)


def _models_folder_check(path):
    """R1: the models folder the user typed -> (its real path, None), or
    (None, the sentence saying which check failed)."""
    if not isinstance(path, str) or not path.strip():
        return None, "Type the path of ComfyUI's models folder."
    path = path.strip()
    if len(path) > 4096 or "\x00" in path:
        return None, "That is not a folder path."
    if not os.path.isabs(path):
        return None, ("Type the whole path, from the top of the drive (like /path/to/ComfyUI/models "
                      "or C:\\ComfyUI\\models).")
    if not os.path.exists(path):
        return None, "Nothing is at that path. Check the spelling."
    real = os.path.realpath(path)
    if not os.path.isdir(real):
        return None, "That is a file, not a folder. Give the models folder itself."
    found = [n for n in MODELS_SUBFOLDERS if os.path.isdir(os.path.join(real, n))]
    if len(found) < 3:
        return None, ("That does not look like ComfyUI's models folder: it holds %d of ComfyUI's usual folders "
                      "(%s), and needs at least 3. It is the folder called models inside your ComfyUI folder."
                      % (len(found), ", ".join(MODELS_SUBFOLDERS)))
    probe = os.path.join(real, ".bwf-write-check-%s" % uuid.uuid4().hex[:12])
    try:
        os.close(os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY | _O_NOFOLLOW, 0o600))
        os.unlink(probe)
    except OSError as e:
        return None, ("Black Wire Forge cannot write to that folder (%s). Run it as a user who can, or use the "
                      "command under each file instead." % e.__class__.__name__)
    return real, None


def _model_dest(root, src):
    """R2: -> (folder, file) where one source lands:
    <models>/<folder>[/<subdir>]/<basename(file)>, contained by the same
    commonpath check as the LoRA download. Raises ValueError otherwise."""
    f, folder, subdir = src.get("file"), src.get("folder"), src.get("subdir") or ""
    if not all(isinstance(x, str) for x in (f, folder, subdir)):
        raise ValueError("That file's entry is not in the expected shape.")
    name = f.replace("\\", "/").rsplit("/", 1)[-1]
    if not _MODEL_NAME_RE.match(name) or name.endswith(".part"):
        raise ValueError("%r is not a file name Black Wire Forge will write." % name[:80])
    target = os.path.abspath(os.path.join(root, folder, subdir))
    # commonpath() raises ValueError ("Paths don't have the same drive") when
    # folder/subdir point at another Windows drive; that is simply another
    # way of landing outside `root`, so it gets the same plain sentence
    # rather than CPython's.
    try:
        inside = os.path.commonpath([target, root]) == root
    except ValueError:
        inside = False
    if not folder or target == root or not inside:
        raise ValueError("The folder for %s would land outside the models folder; refused." % name)
    return target, os.path.join(target, name)


def _model_url(src):
    """R2: https://huggingface.co/<repo>/resolve/main/<file>, from the manifest only."""
    repo, f = src.get("repo"), src.get("file")
    if not isinstance(repo, str) or not REPO_ID_RE.match(repo) or not isinstance(f, str) \
            or not all(_MODEL_NAME_RE.match(seg) for seg in f.split("/")):
        raise ValueError("That file's source is not a Hugging Face repo and file.")
    return "%s/%s/resolve/main/%s" % (MODEL_DL_BASE, urllib.parse.quote(repo, safe="/"),
                                      urllib.parse.quote(f, safe="/"))


def _model_on_disk(root, src):
    """Already present with the exact expected size -> Installed."""
    try:
        dest = _model_dest(root, src)[1]
        st = os.lstat(dest)
    except (ValueError, OSError):
        return False
    return stat.S_ISREG(st.st_mode) and st.st_size == src.get("size")


def _manifest_sources():
    """(repo, file) -> the recommended ("run by us") source; "not run by us"
    alternatives are never downloaded, so they are not in here."""
    out = {}
    for p in engines.packs():
        for lst in (p.get("sources") or {}).values():
            for s in lst:
                if s.get("run_by_us"):
                    out.setdefault((s["repo"], s["file"]), s)
    return out


def _dl_save():
    """Persist the queue (caller holds MODEL_DL_LOCK): temp file + rename."""
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = MODEL_DL_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(MODEL_DL, f, indent=1)
    os.replace(tmp, MODEL_DL_FILE)


def _dl_load():
    """R4: read downloads.json at start. Every entry is re-derived from the
    packs' own sources (repo + file must be a "run by us" source, its size
    the manifest's), so an edited file can never widen what is fetched."""
    try:
        with open(MODEL_DL_FILE) as f:
            d = json.load(f)
    except FileNotFoundError:
        return
    except (OSError, ValueError):
        log("downloads.json could not be read, so no model download resumes.", "warn")
        return
    if not isinstance(d, dict) or not d.get("models"):
        return
    root, why = _models_folder_check(d.get("models"))
    if why:
        log("Model downloads are not resumed: the models folder they were going to cannot be used now. " + why,
            "warn")
        return
    known, queue = _manifest_sources(), []
    for e in d.get("queue") if isinstance(d.get("queue"), list) else []:
        s = known.get((e.get("repo"), e.get("file"))) if isinstance(e, dict) else None
        if not s or e.get("expected") != s["size"]:
            continue
        try:
            dest = _model_dest(root, s)[1]
        except ValueError:
            continue
        state = e.get("state") if e.get("state") in MODEL_DL_STATES else "failed"
        queue.append({"room": str(e.get("room") or "")[:80], "repo": s["repo"], "file": s["file"],
                      "folder": s["folder"], "subdir": s.get("subdir") or "", "dest": os.path.relpath(dest, root),
                      "expected": s["size"], "state": state, "error": str(e.get("error") or "")[:400]})
    # A .part record is kept only while the file is still exactly the one this
    # app wrote (device, inode, size, mtime), at a manifest file's .part name,
    # reached from the declared folder through validated folders only. After
    # a kill mid-write the record lags the last second of writes: for a file
    # that was running then, same device+inode and size/mtime no smaller is
    # trusted to RESUME -- never to delete (security review finding 2).
    running = {x["dest"] + ".part" for x in queue if x["state"] == "running"}
    parts = []
    for p in d.get("parts") if isinstance(d.get("parts"), list) else []:
        if not isinstance(p, dict):
            continue
        rec = dict({k: p.get(k) for k in _PART_KEYS}, rel=p.get("rel"))
        if _part_checked(root, rec):
            parts.append(rec)
        elif rec["rel"] in running and _part_checked(root, rec, grown=True):
            parts.append(dict(rec, grown_ok=True))
    with MODEL_DL_LOCK:
        MODEL_DL.update(models=root, queue=queue, parts=parts)


_PART_KEYS = ("dev", "ino", "size", "mtime_ns")


class _NotOurs(ValueError):
    """The .part at the path is no longer the file this app wrote: never unlinked, never written."""
    pass


def _part_id(st):
    return {"dev": st.st_dev, "ino": st.st_ino, "size": st.st_size, "mtime_ns": st.st_mtime_ns}


def _same_file(a, b):
    """Two stat results of the same, unchanged regular file."""
    return a is not None and b is not None and stat.S_ISREG(a.st_mode) and stat.S_ISREG(b.st_mode) and \
        (a.st_dev, a.st_ino, a.st_size, a.st_mtime_ns) == (b.st_dev, b.st_ino, b.st_size, b.st_mtime_ns)


def _part_matches(rec, st, grown=False):
    """Is `st` (an lstat) still the .part this app recorded in `rec`? Exact:
    device, inode, size and mtime. grown=True (resume after a kill only):
    device and inode, and size and mtime no smaller than recorded."""
    if not rec or st is None or not stat.S_ISREG(st.st_mode) or \
            not all(isinstance(rec.get(k), int) for k in _PART_KEYS):
        return False
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        return False
    if (st.st_dev, st.st_ino) != (rec["dev"], rec["ino"]):
        return False
    if grown:
        return st.st_size >= rec["size"] and st.st_mtime_ns >= rec["mtime_ns"]
    return (st.st_size, st.st_mtime_ns) == (rec["size"], rec["mtime_ns"])


def _part_names(root):
    """The only .part paths this app ever writes: one per manifest file."""
    out = set()
    for src in _manifest_sources().values():
        try:
            out.add(os.path.relpath(_model_dest(root, src)[1], root) + ".part")
        except ValueError:
            pass
    return out


def _part_checked(root, rec, grown=False):
    """A recorded .part -> its path if it is at a manifest file's .part name,
    reached from the declared folder through validated folders only, and
    still the file this app wrote; else None."""
    if not root or not isinstance(rec, dict) or rec.get("rel") not in _part_names(root):
        return None
    path = os.path.join(root, rec["rel"])
    if _dl_folder(root, os.path.dirname(path), rec["rel"], create=False):
        return None
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return path if _part_matches(rec, st, grown) else None


def _part_rec(rel):
    """Caller holds MODEL_DL_LOCK."""
    return next((p for p in MODEL_DL["parts"] if isinstance(p, dict) and p.get("rel") == rel), None)


def _part_set(rel, st):
    """Record (or refresh) the identity of a .part this app is writing."""
    with MODEL_DL_LOCK:
        rec = _part_rec(rel)
        if rec is None:
            MODEL_DL["parts"].append(dict(_part_id(st), rel=rel))
        else:
            rec.clear()
            rec.update(_part_id(st), rel=rel)
        _dl_save()


def _dl_busy():
    """Caller holds MODEL_DL_LOCK."""
    return MODEL_DL_RUN["thread"] is not None


def _dl_kick():
    """Start the one download worker unless it runs (caller holds MODEL_DL_LOCK)."""
    if MODEL_DL_RUN["thread"] is not None:
        return
    if not any(e["state"] in ("queued", "running") for e in MODEL_DL["queue"]):
        return
    MODEL_DL_RUN["cancel"] = False
    MODEL_DL_RUN["thread"] = t = threading.Thread(target=_dl_worker, daemon=True)
    t.start()


def _dl_worker():
    """R4: one file at a time, in queue order, until none is left or Cancel."""
    while True:
        with MODEL_DL_LOCK:
            e = None if MODEL_DL_RUN["cancel"] else next(
                (x for x in MODEL_DL["queue"] if x["state"] in ("running", "queued")), None)
            if e is None:
                MODEL_DL_RUN["thread"] = None
                return
            e["state"], e["error"] = "running", ""
            MODEL_DL_RUN["bytes"] = 0
            root = MODEL_DL["models"]
            _dl_save()
        try:
            state, error = _dl_one(root, e)
        except Exception as x:   # never let one file stop the queue silently
            traceback.print_exc()
            state, error = "failed", "Could not download %s (%s)." % (os.path.basename(e["dest"]),
                                                                     x.__class__.__name__)
        with MODEL_DL_LOCK:
            e["state"], e["error"] = state, error
            _dl_save()
        if state == "done" and not SETUP_MODE:
            # Installed badges: the lane pollers re-read ComfyUI's model list
            # on their next pass (they do this every few minutes anyway).
            with DISCOVERY_LOCK:
                for disc in DISCOVERY.values():
                    disc["checked"] = 0


def _dl_folder(root, target, where, create=True):
    """REV-A: make <target> by walking down from the declared models folder,
    one name at a time (never through anything but the declared folder).
    A name that already exists as a link is the user's own layout (a big
    models subfolder moved to another drive) and is used only if its real
    path is an existing folder this user owns and can write to (an O_EXCL
    probe); this app only ever creates plain folders, never links.
    -> None, or the sentence saying why not."""
    cur = root
    for part in os.path.relpath(target, root).split(os.sep):
        cur = os.path.join(cur, part)
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            if not create:
                return "The folder %s is not there." % where
            os.mkdir(cur, 0o755)
            continue
        if stat.S_ISLNK(st.st_mode):
            real = os.path.realpath(cur)
            try:
                rst = os.stat(real)
            except OSError:
                rst = None
            if rst is None or not stat.S_ISDIR(rst.st_mode):
                return "The folder %s is a link to something that is not a folder." % where
            if hasattr(os, "getuid") and rst.st_uid != os.getuid():
                return "The folder %s is a link to a folder that belongs to another user." % where
            probe = os.path.join(real, ".bwf-write-check-%s" % uuid.uuid4().hex[:12])
            try:
                os.close(os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY | _O_NOFOLLOW, 0o600))
                os.unlink(probe)
            except OSError as e:
                return "Black Wire Forge cannot write to the folder %s links to (%s)." % (where, e.__class__.__name__)
        elif not stat.S_ISDIR(st.st_mode):
            return "%s in the models folder is not a folder." % where
    return None


def _dl_forget_part(rel):
    with MODEL_DL_LOCK:
        rec = _part_rec(rel)
        if rec is not None:
            MODEL_DL["parts"].remove(rec)
            _dl_save()


def _dl_drop_part(part, rel, ident):
    """Remove our .part only while the path still holds exactly `ident`."""
    try:
        if _same_file(os.lstat(part), ident):
            os.unlink(part)
            _dl_forget_part(rel)
    except OSError:
        pass


def _dl_one(root, e):
    """Fetch one queued file. -> (state, sentence)."""
    try:
        target, dest = _model_dest(root, e)
        url = _model_url(e)
    except ValueError as x:
        return "failed", str(x)
    name, expected = os.path.basename(dest), e["expected"]
    where = "/".join(x for x in (e["folder"], e["subdir"]) if x)
    if os.path.lexists(dest):
        if _model_on_disk(root, {"file": e["file"], "folder": e["folder"], "subdir": e["subdir"], "size": expected}):
            return "done", ""
        return "failed", ("A different file named %s is already in %s. Black Wire Forge leaves it alone: move it "
                          "away to download this one." % (name, where))
    why = _dl_folder(root, target, where)
    if why:
        return "failed", why + " So %s is not downloaded; use the command under the file instead." % name
    part = dest + ".part"
    rel_part = os.path.relpath(part, root)
    try:
        st = os.lstat(part)
    except FileNotFoundError:
        st = None
    if st is not None:
        if not stat.S_ISREG(st.st_mode):
            return "failed", ("Something that is not a plain file (a link, say) is at %s.part in %s, so %s is not "
                              "downloaded. Remove it first." % (name, where, name))
        with MODEL_DL_LOCK:
            rec = dict(_part_rec(rel_part) or {})
        if not (_part_matches(rec, st) or (rec.get("grown_ok") and _part_matches(rec, st, grown=True))):
            return "failed", ("%s.part in %s was not left by Black Wire Forge, so it is not touched. Move it away "
                              "to download %s." % (name, where, name))
    have = [st.st_size if st is not None and st.st_size <= expected else 0]
    mine = [st]      # the identity of what this app wrote: from its own descriptor once it writes
    keep = [None]    # a second descriptor on our .part, so that identity holds whatever the path holds later
    saved_at = [0.0]
    not_ours = ("%s.part in %s was replaced by something else during the download, so it is left alone. Move it "
                "away to download %s." % (name, where, name))

    def open_part():
        if st is not None and not have[0]:
            # Starting again from byte 0: a fresh .part. The old one goes only
            # while the path still holds exactly the file checked above.
            try:
                now = os.lstat(part)
            except FileNotFoundError:
                now = None
            if now is not None:
                if not _same_file(now, st):
                    raise _NotOurs(not_ours)
                os.unlink(part)
        if st is None or not have[0]:
            fd = os.open(part, os.O_CREAT | os.O_EXCL | os.O_WRONLY | _O_NOFOLLOW, 0o644)
        else:
            # Resume: never creates, never follows a link, and O_NONBLOCK so a
            # FIFO swapped in fails at once (ENXIO) instead of hanging here.
            try:
                fd = os.open(part, os.O_WRONLY | _O_NOFOLLOW | _O_NONBLOCK)
            except OSError:
                raise _NotOurs(not_ours)
            if not _same_file(os.fstat(fd), st):
                os.close(fd)
                raise _NotOurs(not_ours)
            if _O_NONBLOCK:
                os.set_blocking(fd, True)
        keep[0] = os.dup(fd)
        mine[0] = os.fstat(fd)
        _part_set(rel_part, mine[0])
        f = os.fdopen(fd, "wb")
        f.truncate(have[0])
        f.seek(have[0])
        return f

    def progress(got, total):
        MODEL_DL_RUN["bytes"] = got
        if keep[0] is not None and time.monotonic() - saved_at[0] > 1.0:   # a kill leaves the record ~1 s behind
            saved_at[0] = time.monotonic()
            _part_set(rel_part, os.fstat(keep[0]))

    try:
        try:
            if have[0] < expected:
                token = (os.environ.get("HF_TOKEN") or "").strip()
                for attempt in (1, 2):
                    req = urllib.request.Request(url)
                    if have[0]:
                        req.add_header("Range", "bytes=%d-" % have[0])
                    if token and _hf_token_host(url):
                        req.add_unredirected_header("Authorization", "Bearer " + token)
                    try:
                        r = _DOWNLOAD_OPENER.open(req, timeout=MODEL_DL_STALL)
                    except urllib.error.HTTPError as x:
                        if x.code == 416 and have[0] and attempt == 1:
                            have[0] = 0      # REV-B: the server will not resume this: once more, from byte 0
                            continue
                        raise
                    break
                with r:
                    if have[0] and r.status != 206:
                        have[0] = 0          # the server sent the whole file: start again from byte 0
                    elif have[0] and (r.headers.get("Content-Range") or "").strip() != "bytes %d-%d/%d" % (
                            have[0], expected - 1, expected):
                        raise _SizeRefused("the server resumed at a different place")
                    _download_body(r, open_part, expected, exact=True, have=have[0],
                                   cancelled=lambda: MODEL_DL_RUN["cancel"], progress=progress)
            elif st is None:
                return "failed", "%s has no size to download." % name
        finally:
            if keep[0] is not None:
                mine[0] = os.fstat(keep[0])
                os.close(keep[0])
                keep[0] = None
                _part_set(rel_part, mine[0])
        try:
            now = os.lstat(part)
        except FileNotFoundError:
            now = None
        if not _same_file(now, mine[0]):
            raise _NotOurs(not_ours)
        if mine[0].st_size != expected:
            raise _SizeRefused("the finished file is %d bytes, not %d" % (mine[0].st_size, expected))
        try:
            os.link(part, dest)          # never replaces a file that appeared meanwhile
        except FileExistsError:
            return "failed", "A file named %s appeared in %s meanwhile; it is left alone." % (name, where)
        except OSError:
            if os.path.lexists(dest):
                return "failed", "A file named %s appeared in %s meanwhile; it is left alone." % (name, where)
            os.replace(part, dest)       # a drive without hard links
            _dl_forget_part(rel_part)
        else:
            _dl_drop_part(part, rel_part, mine[0])
        return "done", ""
    except urllib.error.HTTPError as x:
        page = "https://huggingface.co/" + e["repo"]
        if x.code in (401, 403):
            return "skipped", ("Skipped %s: %s needs a Hugging Face login. Accept its terms on that page, then start "
                               "Black Wire Forge with HF_TOKEN set to a token from your Hugging Face account."
                               % (name, page))
        if x.code == 404:
            return "failed", "%s is not at %s any more (404)." % (name, page)
        return "failed", "Hugging Face answered %d for %s. Try again later." % (x.code, name)
    except _Cancelled:
        return "cancelled", "Cancelled. What already arrived is kept for next time."
    except _Short as x:
        return "failed", ("The download of %s %s; press Download again to carry on from there." % (name, x))
    except _NotOurs as x:
        return "failed", str(x)                  # never unlinks: it is not ours any more
    except _SizeRefused as x:
        _dl_drop_part(part, rel_part, mine[0])
        return "failed", "%s was refused: %s." % (name, x)
    except TimeoutError:
        return "failed", ("The download of %s stalled (nothing arrived for %d seconds); press Download again to "
                          "carry on from there." % (name, MODEL_DL_STALL))
    except OSError as x:
        # Security review Finding 3: the error's class, never its message
        # (which embeds the absolute path).
        return "failed", ("Could not finish %s (%s); press Download again to carry on from there."
                          % (name, x.__class__.__name__))
    except ValueError as x:
        return "failed", "%s: %s" % (name, x)


def _room_plan(room):
    """R3: the files a room (one entry of setup_rooms) still needs: each
    role's recommended file unless the role is installed, each file once,
    none already queued. -> [source]."""
    with MODEL_DL_LOCK:
        queued = {(x["repo"], x["file"]) for x in MODEL_DL["queue"] if x["state"] in ("queued", "running")}
    out, seen = [], set()
    for role in room["roles"]:
        main = next((s for s in role["sources"] if s["run_by_us"]), None)
        if role["installed"] or not main or (main["repo"], main["file"]) in seen | queued:
            continue
        seen.add((main["repo"], main["file"]))
        out.append(main)
    return out


def setup_models_folder(body):
    """R1: POST /api/setup/models-folder {path} (null forgets it)."""
    with MODEL_DL_LOCK:
        if _dl_busy():
            return {"ok": False, "error": "Cancel the downloads first, then change the folder."}, 409
    path = body.get("path")
    real = None
    if path is not None:
        real, why = _models_folder_check(path)
        if why:
            return {"ok": False, "error": why}, 400
    with MODEL_DL_LOCK:
        if real != MODEL_DL["models"]:
            MODEL_DL.update(models=real, queue=[], parts=[])
        _dl_save()
    return {"ok": True, "path": real}, 200


def downloads_start(body):
    """R3: POST /api/downloads/start {room, bytes, host?, port?}. `bytes` is
    what the page showed on the button: more than that is never queued."""
    with MODEL_DL_LOCK:
        root = MODEL_DL["models"]
        if MODEL_DL_RUN["thread"] is not None and MODEL_DL_RUN["cancel"]:
            return {"ok": False, "error": "Still stopping the last download. Try again in a moment."}, 409
    if not root:
        return {"ok": False, "error": "Tell step 1 where ComfyUI's models folder is first."}, 400
    _, why = _models_folder_check(root)
    if why:
        return {"ok": False, "error": why}, 400
    rooms = setup_rooms({"host": [str(body.get("host") or "")], "port": [str(body.get("port") or "")]})[0]["rooms"]
    room = next((r for r in rooms if r["id"] == body.get("room")), None)
    if room is None:
        return {"ok": False, "error": "There is no room by that name."}, 400
    plan = _room_plan(room)
    need = sum(s["size"] for s in plan)
    try:
        dests = [os.path.relpath(_model_dest(root, s)[1], root) for s in plan]
    except ValueError as e:
        return {"ok": False, "error": str(e)}, 400
    if not plan:
        return {"ok": True, "queued": 0, "bytes": 0}, 200
    said = body.get("bytes")
    if not isinstance(said, int) or isinstance(said, bool) or need > said:
        return {"ok": False, "error": "What this room needs changed since the page showed it. Look at the room "
                                      "again."}, 409
    with MODEL_DL_LOCK:
        pending = sum(x["expected"] for x in MODEL_DL["queue"] if x["state"] in ("queued", "running"))
        free = shutil.disk_usage(root).free
        if free < pending + need + MODEL_DL_MARGIN:
            return {"ok": False, "error": "These downloads need %s, plus %s to spare, but the drive with the models "
                                          "folder has only %s free." % (_gb(pending + need), _gb(MODEL_DL_MARGIN),
                                                                        _gb(free))}, 400
        if MODEL_DL_RUN["thread"] is None:          # a new batch: forget the finished one
            MODEL_DL["queue"] = [x for x in MODEL_DL["queue"] if x["state"] in ("queued", "running")]
        for s, dest in zip(plan, dests):
            MODEL_DL["queue"].append({"room": room["id"], "repo": s["repo"], "file": s["file"],
                                      "folder": s["folder"], "subdir": s.get("subdir") or "",
                                      "dest": dest, "expected": s["size"], "state": "queued", "error": ""})
        _dl_save()
        _dl_kick()
    return {"ok": True, "queued": len(plan), "bytes": need}, 200


def downloads_status():
    """R4: GET /api/downloads -- every file of the current batch, its state
    and bytes, and the batch's totals."""
    with MODEL_DL_LOCK:
        q = [dict(x) for x in MODEL_DL["queue"]]
        running, now = MODEL_DL_RUN["thread"] is not None, MODEL_DL_RUN["bytes"]
        root, parts = MODEL_DL["models"], len(MODEL_DL["parts"])
    files, total, got, finished = [], 0, 0, 0
    for x in q:
        b = x["expected"] if x["state"] == "done" else now if x["state"] == "running" and running else 0
        files.append({"room": x["room"], "repo": x["repo"], "file": os.path.basename(x["dest"]),
                      "where": "/".join(p for p in (x["folder"], x["subdir"]) if p), "expected": x["expected"],
                      "bytes": b, "state": x["state"], "error": x["error"]})
        if x["state"] in ("skipped", "failed", "cancelled"):
            continue
        total += x["expected"]
        got += b
        finished += x["state"] == "done"
    counted = sum(1 for f in files if f["state"] not in ("skipped", "failed", "cancelled"))
    return {"ok": True, "models": root, "running": running, "files": files, "done": finished, "total": counted,
            "position": min(finished + 1, counted), "bytes": got, "total_bytes": total,
            "percent": int(100 * got / total) if total else None, "parts": parts}, 200


def downloads_cancel(body):
    """R4: stop the current file (its .part stays) and clear the queue."""
    with MODEL_DL_LOCK:
        MODEL_DL["queue"] = [x for x in MODEL_DL["queue"] if x["state"] != "queued"]
        if MODEL_DL_RUN["thread"] is not None:
            MODEL_DL_RUN["cancel"] = True
        else:
            for x in MODEL_DL["queue"]:
                if x["state"] == "running":
                    x["state"], x["error"] = "cancelled", "Cancelled. What already arrived is kept for next time."
        _dl_save()
    return {"ok": True}, 200


def downloads_discard(body):
    """R4: remove only this app's own .part files: at a manifest file's .part
    name, reached from the declared folder through validated folders, and
    still exactly the file it wrote (device, inode, size, mtime)."""
    removed = left = 0
    with MODEL_DL_LOCK:
        if MODEL_DL_RUN["thread"] is not None:
            return {"ok": False, "error": "Cancel the downloads first."}, 409
        root = MODEL_DL["models"]
        for rec in MODEL_DL["parts"]:
            path = _part_checked(root, rec)
            try:
                if path:
                    os.unlink(path)
                    removed += 1
                elif root and isinstance(rec, dict) and isinstance(rec.get("rel"), str) and \
                        os.path.lexists(os.path.join(root, rec["rel"])):
                    left += 1
            except OSError:
                pass
        MODEL_DL["parts"] = []
        _dl_save()
    return {"ok": True, "removed": removed, "left": left}, 200


DOWNLOAD_POSTS = {"/api/downloads/start": downloads_start, "/api/downloads/cancel": downloads_cancel,
                  "/api/downloads/discard": downloads_discard}


SETUP_POSTS = {"/api/setup/probe-comfy": setup_probe_comfy, "/api/setup/probe-helper": setup_probe_helper,
               "/api/setup/test-helper": setup_test_helper, "/api/setup/preview": setup_preview,
               "/api/setup/write": setup_write, "/api/setup/models-folder": setup_models_folder}
SETUP_UNFINISHED = {"ok": False, "setup": True, "error": "Finish Setup first."}


# ---------------------------------------------------------------------------
# WB: the Workflows tab. Every ComfyUI lane serves its own template library
# (GET /templates/index.json, /templates/<name>.json, /templates/<name>-1.webp)
# and the workflows saved in its own Workflows panel (/api/userdata?dir=workflows).
# This lists both, says what each still needs on that lane, and saves a template
# into that panel. Nothing is bundled, installed or fetched from the internet:
# every address is a config.json lane, every name one the lane itself listed,
# and every read is bounded through Setup's no-redirect opener (_setup_get_json).
# ---------------------------------------------------------------------------

WF_CACHE_SECONDS = 60.0
WF_NODES_SECONDS = DISCOVER_SECONDS     # the whole /object_info: big, so read rarely
WF_TIMEOUT = 10.0
WF_LIST_LIMIT = 16 * 1024 * 1024        # the lane's index is ~0.7 MB today
WF_WORKFLOW_LIMIT = 16 * 1024 * 1024
WF_THUMB_LIMIT = 2 * 1024 * 1024
WF_READY_MAX = 24
WF_MEDIA = ("image", "video", "audio", "3d")
# Nodes that live only in the ComfyUI page, never in /object_info: core's own, and
# (REV-2) well-known packs' frontend-only nodes, confirmed from each pack's source as
# registered in its web JS with no Python NODE_CLASS_MAPPINGS entry: KJNodes
# (GetNode, SetNode), Easy-Use (easy bookmark), rgthree (every "(rgthree)" name below;
# its "Display Any" is a Python node and is NOT here). Their JSON carries no marker.
WF_UI_ONLY_NODES = frozenset({
    "Note", "MarkdownNote", "Reroute", "PrimitiveNode",
    "GetNode", "SetNode", "easy bookmark",
    "Fast Groups Bypasser (rgthree)", "Fast Groups Muter (rgthree)", "Fast Muter (rgthree)",
    "Fast Bypasser (rgthree)", "Label (rgthree)", "Bookmark (rgthree)", "Fast Actions Button (rgthree)",
    "Node Collector (rgthree)", "Mute / Bypass Repeater (rgthree)", "Mute / Bypass Relay (rgthree)",
    "Reroute (rgthree)", "Random Unmuter (rgthree)", "Power Conductor (rgthree)"})
WF_MODEL_EXTS = (".safetensors", ".gguf", ".ckpt", ".pt", ".pth", ".bin", ".sft", ".onnx")
# REV-2: a widget value that names a model file looks like a path: no pattern characters,
# no whitespace, and a name before the extension.
_WF_FILE_VALUE = re.compile(r"^[^*?\[\]\s]*[^*?\[\]\s/\\.][^*?\[\]\s]*$")


def _wf_file_value(v):
    return (isinstance(v, str) and len(v) <= 300 and v.lower().endswith(WF_MODEL_EXTS)
            and bool(_WF_FILE_VALUE.match(v)) and not v.replace("\\", "/").rsplit("/", 1)[-1].startswith("."))


def _wf_base(v):
    return v.replace("\\", "/").rsplit("/", 1)[-1]
_WF_FILE_BAD = re.compile(r"[^A-Za-z0-9 ._()\-]")
_WF_LOCK = threading.Lock()
_WF_CACHE = {}         # (lane id, key) -> (fetched at, value)
_WF_FETCHING = {}      # (lane id, key) -> Lock: one outbound read per key at a time


class WorkflowError(Exception):
    def __init__(self, code, sentence):
        super().__init__(sentence)
        self.code, self.sentence = code, sentence


def _wf_answer(fn, *args):
    try:
        return fn(*args)
    except WorkflowError as e:
        return {"ok": False, "error": e.sentence}, e.code


def _wf_cached(lane, key, ttl, fetch):
    k = (lane["id"], key)
    with _WF_LOCK:
        hit = _WF_CACHE.get(k)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        gate = _WF_FETCHING.setdefault(k, threading.Lock())
    with gate:
        with _WF_LOCK:
            hit = _WF_CACHE.get(k)
            if hit and time.time() - hit[0] < ttl:
                return hit[1]
        value = fetch()
        with _WF_LOCK:
            _WF_CACHE[k] = (time.time(), value)
        return value


def _wf_forget(lane, key):
    with _WF_LOCK:
        _WF_CACHE.pop((lane["id"], key), None)


def _wf_get_bytes(lane, path, limit):
    """GET a lane path -> (bytes, content type): at most `limit` bytes, within
    WF_TIMEOUT, never following a redirect (the reader _setup_get_json uses)."""
    deadline = time.monotonic() + WF_TIMEOUT
    with _SETUP_OPENER.open(urllib.request.Request(lane_url(lane, path)), timeout=WF_TIMEOUT) as r:
        return _read_by(r, deadline, limit), (r.headers.get("Content-Type") or "")


def _wf_json(lane, path, limit=WF_LIST_LIMIT, timeout=WF_TIMEOUT):
    return _setup_get_json(lane_url(lane, path), timeout, limit)


def _wf_userdata_path(file):
    return "/api/userdata/" + urllib.parse.quote("workflows/" + file, safe="")


def _wf_str(v, n):
    return v[:n] if isinstance(v, str) else ""


def _wf_strs(v, count, n):
    return [x[:n] for x in v if isinstance(x, str) and x][:count] if isinstance(v, list) else []


def _wf_num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else None


def _wf_link(v):
    """An http(s) address from a lane's file, for the page to show as a link -- or None."""
    if not isinstance(v, str) or len(v) > 2000:
        return None
    return v if urllib.parse.urlsplit(v).scheme in ("http", "https") else None


def _wf_version(v):
    m = re.match(r"^\D*(\d+(?:\.\d+)*)", v or "")
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def _wf_lane(lane_id):
    """-> (lane, its latest poll state), or raises: an unknown or non-ComfyUI lane
    is a 400; a lane whose latest poll found it down says so in a sentence."""
    lane = LANE_BY_ID.get(lane_id if isinstance(lane_id, str) else "")
    if not lane or lane_kind(lane) != "comfy":
        raise WorkflowError(400, "Pick one of this app's ComfyUI machines.")
    with STATE_LOCK:
        st = dict(LANE_STATE.get(lane["id"]) or {})
    if st.get("checked") and not st.get("up"):
        raise _wf_down(lane)
    return lane, st


def _wf_down(lane):
    return WorkflowError(502, "%s is not answering right now. Start its ComfyUI, then look again." % lane["name"])


def _wf_templates(lane):
    """The lane's own template index, flattened: name -> the fields the page shows."""
    def fetch():
        raw = _wf_json(lane, "/templates/index.json")
        out = {}
        for cat in raw if isinstance(raw, list) else []:
            if not isinstance(cat, dict):
                continue
            ctype = cat.get("type") if cat.get("type") in WF_MEDIA else "other"
            for t in cat.get("templates") if isinstance(cat.get("templates"), list) else []:
                name = t.get("name") if isinstance(t, dict) else None
                if not isinstance(name, str) or not name or len(name) > 200 or name in out:
                    continue
                sub = t.get("mediaSubtype")
                out[name] = {
                    "name": name, "title": _wf_str(t.get("title"), 200) or name,
                    "description": _wf_str(t.get("description"), 1000),
                    "tags": _wf_strs(t.get("tags"), 12, 60), "models": _wf_strs(t.get("models"), 12, 100),
                    "size": _wf_num(t.get("size")), "usage": _wf_num(t.get("usage")) or 0,
                    "date": _wf_str(t.get("date"), 20),
                    "local": t.get("openSource") is not False,   # false = a cloud (paid API) template
                    "packs": _wf_strs(t.get("requiresCustomNodes"), 20, 100),
                    "min_version": _wf_str(t.get("minComfyUIVersion"), 20) or None,
                    "tutorial": _wf_link(t.get("tutorialUrl")),
                    "thumb_ext": sub if t.get("mediaType") == "image" and sub in ("webp", "png") else None,
                    "category": _wf_str(cat.get("title"), 80), "type": ctype,
                }
        return out
    return _wf_cached(lane, "index", WF_CACHE_SECONDS, fetch)


def _wf_mine(lane):
    """The workflows saved in the lane's own Workflows panel: file -> {file, size, modified}."""
    def fetch():
        raw = _wf_json(lane, "/api/userdata?dir=workflows&full_info=true")
        out = {}
        for e in raw if isinstance(raw, list) else []:
            path = e.get("path") if isinstance(e, dict) else None
            if isinstance(path, str) and path.lower().endswith(".json") and len(path) <= 400:
                out[path] = {"file": path, "size": _wf_num(e.get("size")), "modified": _wf_num(e.get("modified"))}
        return out
    return _wf_cached(lane, "mine", WF_CACHE_SECONDS, fetch)


def _wf_listing(lane, kind):
    try:
        return _wf_templates(lane) if kind == "template" else _wf_mine(lane)
    except Exception:
        raise _wf_down(lane)


def _wf_combo_files(spec):
    """One /object_info node entry -> the model-file options of its required/optional
    COMBO inputs (both dropdown shapes pool_names reads), or None when it has no COMBO."""
    inputs = spec.get("input") if isinstance(spec, dict) else None
    found, files = False, set()
    for section in ("required", "optional"):
        fields = inputs.get(section) if isinstance(inputs, dict) else None
        for opts in fields.values() if isinstance(fields, dict) else []:
            if not (isinstance(opts, list) and opts):
                continue
            if isinstance(opts[0], list):
                candidates = opts[0]
            elif opts[0] == "COMBO" and len(opts) > 1 and isinstance(opts[1], dict):
                candidates = opts[1].get("options") or []
            else:
                continue
            found = True
            files.update(o for o in candidates if isinstance(o, str) and o.lower().endswith(WF_MODEL_EXTS))
    return frozenset(files) if found else None


def _wf_nodes(lane):
    """ONE bounded /object_info per lane (the read W2's Setup does) -> {node class: the
    model files its dropdowns list, or None when it has no dropdown}."""
    def fetch():
        info = _wf_json(lane, "/object_info", SETUP_POOLS_READ_LIMIT, SETUP_POOLS_TIMEOUT)
        if not isinstance(info, dict):
            raise ValueError("/object_info is not a node list")
        return {cls: _wf_combo_files(spec) for cls, spec in info.items()}
    return _wf_cached(lane, "nodes", WF_NODES_SECONDS, fetch)


def _wf_folders(lane):
    raw = _wf_cached(lane, "folders", WF_CACHE_SECONDS, lambda: _wf_json(lane, "/models", SETUP_READ_LIMIT))
    return {x for x in raw if isinstance(x, str)} if isinstance(raw, list) else set()


def _wf_folder_files(lane, folder):
    """A model folder the lane listed -> its file names, with and without subfolders."""
    raw = _wf_cached(lane, "models:" + folder, WF_CACHE_SECONDS,
                     lambda: _wf_json(lane, "/models/" + urllib.parse.quote(folder, safe="")))
    names = set()
    for x in raw if isinstance(raw, list) else []:
        if isinstance(x, str):
            names.add(x)
            names.add(x.replace("\\", "/").rsplit("/", 1)[-1])
    return names


def _wf_needs(wf):
    """A workflow's JSON -> (node class names, [{directory, name, url}], [(node class,
    file)]), over its nodes and every subgraph's. A subgraph's own id appears as a node
    type and is not a class; neither are the page's own UI-only nodes. The last list is
    every widget value that ends in a model extension (REV-1: hand-saved workflows name
    their files only there)."""
    if not isinstance(wf, dict):
        raise ValueError("not a workflow")
    defs = wf.get("definitions") if isinstance(wf.get("definitions"), dict) else {}
    subs = [s for s in (defs.get("subgraphs") or []) if isinstance(s, dict)] \
        if isinstance(defs.get("subgraphs"), list) else []
    sub_ids = {s.get("id") for s in subs}
    types, models, seen, widget_files = set(), [], set(), []
    for g in [wf] + subs:
        for n in g.get("nodes") if isinstance(g.get("nodes"), list) else []:
            if not isinstance(n, dict):
                continue
            t = n.get("type")
            wv = n.get("widgets_values")
            loads = [v for v in (wv if isinstance(wv, list) else (wv.values() if isinstance(wv, dict) else []))
                     if _wf_file_value(v)]
            if isinstance(t, str) and t and t not in sub_ids and t not in WF_UI_ONLY_NODES:
                types.add(t)
                widget_files += [(t, v) for v in loads if (t, v) not in widget_files]
            # REV-2: what a node LOADS is its widget value; properties.models is the
            # author's hint, so it counts only for a node with no file widget, or
            # when it names the same file the widget does.
            loaded = {_wf_base(v) for v in loads}
            props = n.get("properties") if isinstance(n.get("properties"), dict) else {}
            for m in props.get("models") if isinstance(props.get("models"), list) else []:
                if not (isinstance(m, dict) and isinstance(m.get("name"), str) and m["name"]
                        and isinstance(m.get("directory"), str) and m["directory"]):
                    continue
                if loads and _wf_base(m["name"]) not in loaded:
                    continue
                key = (m["directory"], m["name"])
                if key not in seen:
                    seen.add(key)
                    models.append({"directory": m["directory"][:200], "name": m["name"][:300],
                                   "url": _wf_link(m.get("url"))})
    return sorted(types), models, widget_files


def _wf_readiness(lane, st, kind, name, entry):
    packs = entry.get("packs") or [] if kind == "template" else []
    min_version = entry.get("min_version") if kind == "template" else None
    out = {"state": "unknown", "missing_models": [], "missing_nodes": [], "packs": packs, "min_version": min_version}
    path = ("/templates/%s.json" % urllib.parse.quote(name, safe="") if kind == "template"
            else _wf_userdata_path(name))
    try:
        types, models, widget_files = _wf_cached(lane, "needs:%s:%s" % (kind, name), WF_CACHE_SECONDS,
                                                 lambda: _wf_needs(_wf_json(lane, path, WF_WORKFLOW_LIMIT)))
        known, folders = _wf_nodes(lane), _wf_folders(lane)
        missing_models = [m for m in models
                          if m["directory"] not in folders or m["name"] not in _wf_folder_files(lane, m["directory"])]
        # REV-1: a file named only in a widget must be one of the lane's own dropdown
        # options for that class (exact: ComfyUI stores the path as it lists it). A
        # file properties.models already names is judged there; a class this lane
        # lacks is a missing node; a class with no dropdown has no list to be in.
        listed = {_wf_base(m["name"]) for m in models}
        missing_models += [{"directory": None, "name": v, "node": cls, "url": None}
                           for cls, v in widget_files
                           if _wf_base(v) not in listed and known.get(cls) is not None and v not in known[cls]]
    except Exception:
        return out     # the lane did not answer one of these reads: say "can't tell", never "ready"
    out["missing_nodes"] = [t for t in types if t not in known]
    out["missing_models"] = missing_models
    have, need = _wf_version(st.get("comfy")), _wf_version(min_version)
    if have and need and have < need:
        out["state"] = "needs_version"
    elif out["missing_nodes"]:
        out["state"] = "needs_nodes"
    elif missing_models:
        out["state"] = "needs_models"
    else:
        out["state"] = "ready"
    return out


def workflows_list(lane_id):
    """GET /api/workflows?lane= -> the lane's templates and saved workflows."""
    lane, st = _wf_lane(lane_id)
    out = {"ok": True, "lane": lane["id"], "templates": [], "mine": [], "version": st.get("comfy") or None}
    fails = 0
    try:
        out["templates"] = [dict({k: v for k, v in t.items() if k != "thumb_ext"}, thumb=bool(t["thumb_ext"]))
                            for t in _wf_templates(lane).values()]
    except Exception:
        fails += 1
        out["templates_error"] = "%s did not list its templates." % lane["name"]
    try:
        out["mine"] = list(_wf_mine(lane).values())
    except Exception:
        fails += 1
        out["mine_error"] = "%s did not list its saved workflows." % lane["name"]
    if fails == 2:
        raise _wf_down(lane)
    with _WF_LOCK:
        hit = _WF_CACHE.get((lane["id"], "nodes"))
    out["nodes_known"] = bool(hit and time.time() - hit[0] < WF_NODES_SECONDS)
    return out, 200


def workflows_ready(q, raw_query):
    """GET /api/workflows/ready?lane=&kind=template|mine&names=a,b,c -> per name
    what it still needs on that lane. Names are sent comma-joined, each
    percent-encoded, so a saved file with a comma in its name survives."""
    lane, st = _wf_lane((q.get("lane") or [""])[0])
    kind = (q.get("kind") or [""])[0]
    if kind not in ("template", "mine"):
        raise WorkflowError(400, "Say which list: template or mine.")
    m = re.search(r"(?:^|&)names=([^&]*)", raw_query or "")
    names = [urllib.parse.unquote_plus(x) for x in m.group(1).split(",")] if m and m.group(1) else []
    if not names:
        raise WorkflowError(400, "Name at least one workflow.")
    if len(names) > WF_READY_MAX:
        raise WorkflowError(400, "Ask about at most %d workflows at a time." % WF_READY_MAX)
    listing = _wf_listing(lane, kind)
    if any(n not in listing for n in names):
        raise WorkflowError(400, "That workflow is not on %s. Refresh the list." % lane["name"])
    return {"ok": True, "lane": lane["id"], "kind": kind,
            "ready": {n: _wf_readiness(lane, st, kind, n, listing[n]) for n in names}}, 200


def workflows_thumb(q):
    """GET /api/workflows/thumb?lane=&name= -> (bytes, content type) of a template's
    own thumbnail on that lane, or None (webp/png only, at most 2 MiB)."""
    try:
        lane, _ = _wf_lane((q.get("lane") or [""])[0])
        entry = _wf_templates(lane).get((q.get("name") or [""])[0])
        if not entry or not entry["thumb_ext"]:
            return None
        data, _ = _wf_get_bytes(lane, "/templates/%s-1.%s" % (urllib.parse.quote(entry["name"], safe=""),
                                                                   entry["thumb_ext"]), WF_THUMB_LIMIT)
    except Exception:
        return None
    # The type comes from the bytes, never from the lane's header: ComfyUI's own
    # /templates route answers application/octet-stream for a real webp/png.
    # Served only when the bytes are the image the index said (HTML, or any
    # other file, stays a 404).
    for ctype, ext, magic in (("image/webp", "webp", lambda b: b[:4] == b"RIFF" and b[8:12] == b"WEBP"),
                              ("image/png", "png", lambda b: b[:8] == b"\x89PNG\r\n\x1a\n")):
        if entry["thumb_ext"] == ext and data and magic(data):
            return data, ctype
    return None


def _wf_file_name(title, n):
    base = re.sub(r"\s+", " ", _WF_FILE_BAD.sub("", title or "")).strip(" .")[:120].strip(" .") or "Workflow"
    return "%s%s.json" % (base, "" if n == 1 else " (%d)" % n)


def _wf_post(lane, path, data):
    """POST bytes to a lane path -> the status code (a redirect is refused, not followed)."""
    req = urllib.request.Request(lane_url(lane, path), data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    deadline = time.monotonic() + WF_TIMEOUT
    try:
        with _SETUP_OPENER.open(req, timeout=WF_TIMEOUT) as r:
            _read_by(r, deadline, SETUP_READ_LIMIT)
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def workflows_open(body):
    """POST /api/workflows/open {lane, kind, name}: a template is saved (never
    overwriting) into the lane's own Workflows panel as "<its title>.json", then
    " (2)", " (3)"...; a saved workflow is already there. -> the lane's address."""
    if not isinstance(body, dict):
        raise WorkflowError(400, "Send a JSON object.")
    lane, _ = _wf_lane(body.get("lane"))
    kind, name = body.get("kind"), body.get("name")
    if kind not in ("template", "mine") or not isinstance(name, str) or not name:
        raise WorkflowError(400, "Say which workflow to open.")
    entry = _wf_listing(lane, kind).get(name)
    if entry is None:
        raise WorkflowError(400, "That workflow is not on %s. Refresh the list." % lane["name"])
    url = "http://%s:%d/" % (lane["host"], lane["port"])
    if kind == "mine":
        return {"ok": True, "url": url}, 200
    try:
        data, _ = _wf_get_bytes(lane, "/templates/%s.json" % urllib.parse.quote(name, safe=""), WF_WORKFLOW_LIMIT)
        if not isinstance(json.loads(data.decode("utf-8")), dict):
            raise ValueError("not a workflow")
        _wf_forget(lane, "mine")
        taken = {f.lower() for f in _wf_mine(lane)}
    except Exception:
        raise _wf_down(lane)
    for n in range(1, 100):
        fname = _wf_file_name(entry["title"], n)
        if fname.lower() in taken:
            continue
        try:
            code = _wf_post(lane, _wf_userdata_path(fname) + "?overwrite=false", data)
        except Exception:
            raise _wf_down(lane)
        if code == 409:
            continue
        _wf_forget(lane, "mine")
        if 200 <= code < 300:
            return {"ok": True, "url": url, "saved": fname}, 200
        raise WorkflowError(502, "%s did not save the workflow (it answered %d)." % (lane["name"], code))
    raise WorkflowError(409, "There are already 99 copies of this template on %s." % lane["name"])


AUDIO_UPLOAD_EXTS = (".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus")


def _audio_upload_seconds(filename, data=None, path=None):
    """Length in seconds of an uploaded SOUND file, or None (not a sound file by name, no
    ffprobe, unreadable). Additive: callers only add the key when this is a number."""
    if not FFPROBE_BIN or os.path.splitext(filename or "")[1].lower() not in AUDIO_UPLOAD_EXTS:
        return None
    try:
        if path is not None:
            d = _probe_duration(path)
        else:
            with tempfile.NamedTemporaryFile(suffix=os.path.splitext(filename)[1].lower(), delete=False) as tf:
                tf.write(data)
                tmp = tf.name
            try:
                d = _probe_duration(tmp)
            finally:
                os.remove(tmp)
    except Exception:
        return None
    return round(d, 3) if isinstance(d, float) and d > 0 else None


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

    def setup_route(self, u):
        """W1: True when this request was answered here. In Setup mode only the
        Setup page, /help, the favicon, /api/health, /api/setup/* and (W3)
        /api/downloads* answer; every other API is 503. Once a config exists,
        /api/setup/* is 404."""
        is_setup = u.path.startswith("/api/setup/")
        if not SETUP_MODE and not is_setup:
            return False
        if self.command == "POST":
            body_ok = True
            try:
                body = self.read_json()
                body_ok = isinstance(body, dict)
            except ValueError:
                body_ok = False
        try:
            if not SETUP_MODE:
                self.send_json({"error": "not found"}, 404)
            elif self.command == "GET" and u.path in ("/", "/index.html"):
                with open(os.path.join(APP_DIR, "setup.html"), "rb") as f:
                    self.send_blob(f.read(), "text/html; charset=utf-8")
            elif self.command == "GET" and u.path == "/api/health":
                self.send_json({"ok": True, "port": PORT, "lanes": 0, "setup": True})
            elif self.command == "GET" and u.path == "/api/setup/state":
                self.send_json(*setup_state())
            elif self.command == "GET" and u.path == "/api/setup/rooms":
                self.send_json(*setup_rooms(urllib.parse.parse_qs(u.query)))
            elif self.command == "GET" and u.path == "/api/downloads":
                self.send_json(*downloads_status())
            elif self.command == "POST" and (u.path in SETUP_POSTS or u.path in DOWNLOAD_POSTS):
                if not body_ok:
                    self.send_json({"ok": False, "error": "Send a JSON object."}, 400)
                else:
                    fn = SETUP_POSTS.get(u.path) or DOWNLOAD_POSTS[u.path]
                    if fn is setup_probe_comfy:      # the address this page was reached on counts as "here"
                        self.send_json(*fn(body, self.connection.getsockname()[0]))
                    else:
                        self.send_json(*fn(body))
            elif u.path.startswith("/api/"):
                self.send_json(SETUP_UNFINISHED, 503)
            elif self.command == "POST":
                self.send_json({"error": "not found"}, 404)
            else:
                return False   # /help, /favicon.ico, anything else: the usual answer
        except BrokenPipeError:
            pass
        except Exception:
            traceback.print_exc()
            self.send_json({"ok": False, "error": "Something went wrong in Setup."}, 500)
        return True

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
        if self.setup_route(u):
            return
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
                return self.send_json({"ok": True, "port": PORT, "lanes": len(LANES), "setup": SETUP_MODE})
            if u.path == "/api/downloads":   # W3 R5: status, on a server bound to this computer only
                if not _downloads_here():
                    return self.send_json({"ok": False, "error": DOWNLOADS_LOCAL_ONLY}, 403)
                return self.send_json(*downloads_status())
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
            if u.path == "/api/guide/history":
                return self.send_json(*guide_history_get((q.get("key") or [""])[0]))
            if u.path == "/api/catalog/loras":
                return self.send_json(*self.api_catalog_loras(q))
            if u.path == "/api/lora/download":
                return self.send_json(*self.api_lora_download_status(q))
            if u.path == "/api/workflows":
                return self.send_json(*_wf_answer(workflows_list, (q.get("lane") or [""])[0]))
            if u.path == "/api/workflows/ready":
                return self.send_json(*_wf_answer(workflows_ready, q, u.query))
            if u.path == "/api/workflows/thumb":
                thumb = workflows_thumb(q)
                return self.send_blob(*thumb) if thumb else self.send_json({"error": "not found"}, 404)
            if u.path == "/api/forge/run":
                return self.send_json(*forge_run_get((q.get("id") or [""])[0]))
            if u.path == "/api/speech/status":
                return self.send_json({"enabled": SPEECH_ENABLED})
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
                # LORA-1: whether this lane opted in to catalog downloads
                # (config.json "downloads"), and where -- never a value the
                # page can use to build a path itself, only to know whether
                # to offer the "Get it" button or the copy-paste command.
                "downloads": bool(isinstance(l.get("downloads"), dict) and l["downloads"].get("loras_dir")),
                # UX-2 #5: whether this lane opted in to Remove-also-deletes
                # (config.json "outputs"), same never-a-path-to-the-page
                # shape as "downloads" above -- just enough for the confirm
                # text to say "...and delete the file".
                "deletes_files": bool(isinstance(l.get("outputs"), dict) and l["outputs"].get("dir")),
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
                # FB-1: which model this mode runs -- the pack's own words for
                # its primary role, and the file discovery resolved for that role
                # on the QUERIED lane (the same m.get(role) dispatch reads), as a
                # basename only. null when there is nothing honest to say.
                model_file = None
                if lane is not None and not kind_mismatch and lane_kind(lane) != "process":
                    _role = engines.primary_role(cap, mode)
                    _f = m.get(_role) if _role else None
                    if isinstance(_f, str) and _f:
                        model_file = _f.replace("\\", "/").rsplit("/", 1)[-1] or None
                modes.append({
                    "id": mode,
                    "label": words.get(mode, mode),
                    "model": engines.mode_model_words(cap, mode),
                    "model_file": model_file,
                    "available": bool(able.get(ability)) and deps_reason is None and not kind_mismatch,
                    "missing": model_missing if kind_mismatch else
                               model_missing + ([deps_reason] if deps_reason else []),
                    "fields": fields_with_pool_options(cap, mode, lane),
                    "presets": engines.presets(cap, mode),
                    # H2: the "Try this" row -- a one-click example per mode.
                    "examples": engines.examples(cap, mode),
                    # R1/R4: the quality ladder, per-lane availability and
                    # R5's honest estimate layered on top of the pack's own
                    # declaration -- the page never sees a step count.
                    "quality": [
                        dict(tier,
                             available=bool(able.get(tier["requires"])) if tier.get("requires") else True,
                             estimate_s=estimate_seconds(lane["id"], cap, mode, tier["id"]) if lane else None,
                             # UX-2 #3: (min, max, n) alongside the unchanged median.
                             estimate_range=list(estimate_range(lane["id"], cap, mode, tier["id"])) if lane else None)
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


    def api_catalog_loras(self, q):
        """LORA-2B: multi-family catalog. `family` is optional -- omitted,
        this defaults to the families present on the lane (optionally
        narrowed by `cap`, the room's cap), picking the first one so a
        single-family lane (today's only shape) still gets its catalog in
        one call; `families` always lists what's present, so the page can
        build tabs when there's more than one.

        Owner ruling 2026-09-28: no hiding, no "Advanced" filter -- every
        pack is listed, each carrying its own "nsfw" field so the page can
        badge it. `advanced` is accepted and ignored (harmless for an old
        caller); it no longer changes the response."""
        lane = LANE_BY_ID.get((q.get("lane") or [""])[0])
        if not lane:
            return {"ok": False, "error": "Pick a lane first."}, 400
        cap = (q.get("cap") or [""])[0] or None
        requested = (q.get("family") or [""])[0] or None
        present = _families_for_lane(models_for(lane))
        if cap:
            present = [f for f in present if f.get("cap") == cap]
        # "convert" is a bool, not the pack's converter name -- the page
        # only needs to know a converted file needs one, never what it is
        # or which model it's for (LORA-2E #1: stays engine-agnostic).
        families_meta = [{"id": f["id"], "label": f.get("label", f["id"]), "cap": f.get("cap"),
                          "folder": f.get("folder") or "", "convert": bool(f.get("convert"))}
                         for f in present]
        if requested:
            family = next((f for f in present if f["id"] == requested), None)
        else:
            family = present[0] if present else None
        if family is None:
            return {"ok": True, "loras": [], "families": families_meta, "family": None, "folder": "",
                    "note": "This lane's models aren't ones this app has a style catalog for yet."}, 200
        try:
            catalog = catalog_for(family["hf_base"])
        except Exception as e:
            return {"ok": False, "error": "Could not reach Hugging Face: %s" % e}, 502
        # UX-2 #10/#11: per-entry flags, computed fresh against THIS lane and
        # family (never cached alongside the shared HF listing above, which
        # is keyed only by hf_base and has no lane or family of its own).
        none_words = [w.lower() for w in ((family.get("match") or {}).get("none") or [])]
        with DISCOVERY_LOCK:
            # ComfyUI's lora pool lists names WITH their subfolder
            # ('a_family/x.safetensors', 'library/a_family/x.safetensors'),
            # and on Windows hosts possibly with backslashes -- catalog
            # filenames stay bare, so match on the pool entry's BASENAME
            # (split on both '/' and '\\'), not the whole pool string.
            installed_names = {re.split(r"[/\\]", n)[-1].lower()
                                for n in (DISCOVERY.get(lane["id"], {}).get("pools", {}).get("lora") or [])}
        badged = []
        for entry in catalog:
            # #10: this repo/file matches the family's OWN "none" exclusion
            # words (the same ones the style picker already uses to keep
            # speed/IC-LoRA/upscaler files out of the local dropdown) -- the
            # live catalog still LISTS it (owner: everything lives together,
            # marked), just badged as not a style.
            haystack = (entry.get("id") or "").lower() + " " + " ".join(
                (f.get("filename") or "").lower() for f in entry.get("files") or [])
            needs_workflow = bool(none_words) and any(w in haystack for w in none_words)
            # #11: "Installed" -- matched by FILENAME against this lane's own
            # discovered "lora" pool (ComfyUI's /object_info dropdown, the
            # only channel this app has into what's on the lane's disk).
            # Size is NOT compared: ComfyUI's dropdown gives filenames only,
            # no byte sizes -- there is no second channel to read one from
            # without inventing filesystem access this app deliberately
            # doesn't have (AGENTS.md; also true of a .metadata.json/.txt
            # sidecar -- skipped for the same reason, not invented here).
            files = [dict(f, installed=re.split(r"[/\\]", (f.get("filename") or "").lower())[-1] in installed_names)
                     for f in entry.get("files") or []]
            badged.append(dict(entry, files=files, workflow_only=needs_workflow))
        return {"ok": True, "loras": badged, "families": families_meta,
                "family": family["id"], "folder": family.get("folder") or ""}, 200

    def api_lora_download_status(self, q):
        lane = LANE_BY_ID.get((q.get("lane") or [""])[0])
        if not lane:
            return {"ok": False, "error": "Pick a lane first."}, 400
        with DOWNLOAD_LOCK:
            state = dict(DOWNLOADS.get(lane["id"]) or {})
        return {"ok": True, "download": state or None}, 200

    def api_lora_download_start(self, p):
        lane = LANE_BY_ID.get(p.get("lane")) if isinstance(p.get("lane"), str) else None
        if not lane:
            return {"ok": False, "error": "Pick a lane first."}, 400
        # Security review Finding 1: the check and the claim must be ONE
        # atomic step under DOWNLOAD_LOCK -- the placeholder below is
        # registered here, before _lora_download_target's (possibly slow,
        # possibly network-hitting) validation runs unlocked, so a second
        # concurrent start for the same lane sees it and is refused.
        with DOWNLOAD_LOCK:
            running = DOWNLOADS.get(lane["id"])
            if running and not running.get("done"):
                return {"ok": False, "error": "A download is already running for %s." % lane["name"]}, 409
            DOWNLOADS[lane["id"]] = {"repo": p.get("repo"), "file": p.get("file"), "bytes": 0,
                                     "total": None, "done": False, "ok": False, "error": "",
                                     "cancel": False}
        try:
            url, dest, max_bytes = _lora_download_target(lane, p.get("family"), p.get("repo"), p.get("file"))
        except DownloadsOff as e:
            with DOWNLOAD_LOCK:
                DOWNLOADS.pop(lane["id"], None)
            return {"ok": False, "error": str(e)}, 403
        except ValueError as e:
            with DOWNLOAD_LOCK:
                DOWNLOADS.pop(lane["id"], None)
            return {"ok": False, "error": str(e)}, 400
        # LORA-2E #1: the SAME family _lora_download_target just resolved,
        # named only by its declared "convert" string -- never re-derives
        # anything download-safety already checked.
        convert_name = (_resolved_family(lane, p.get("family")) or {}).get("convert")
        threading.Thread(target=_run_lora_download,
                         args=(lane, url, dest, max_bytes, p.get("repo"), p.get("file")),
                         kwargs={"convert_name": convert_name},
                         daemon=True).start()
        return {"ok": True}, 200

    def api_lora_download_cancel(self, p):
        lane = LANE_BY_ID.get(p.get("lane")) if isinstance(p.get("lane"), str) else None
        if not lane:
            return {"ok": False, "error": "Pick a lane first."}, 400
        with DOWNLOAD_LOCK:
            state = DOWNLOADS.get(lane["id"])
            if state and not state.get("done"):
                state["cancel"] = True
        return {"ok": True}, 200

    def jobs_payload(self, q):
        limit = int((q.get("limit") or ["60"])[0])
        with JOBS_LOCK:
            ids = list(JOB_ORDER)[-limit:][::-1]
            jobs = [dict(JOBS[i]) for i in ids if i in JOBS]
        # UX-2 #9: derived progress for every queued/running job, computed
        # fresh per poll (not stored) so it always reflects "now" for elapsed.
        for j in jobs:
            pv = job_progress_view(j)
            if pv is not None:
                j["progress"] = pv
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
        if self.setup_route(u):
            return
        try:
            if u.path in DOWNLOAD_POSTS:     # W3 R5: only Cancel outlives Setup, and only bound locally
                if u.path != "/api/downloads/cancel":
                    return self.send_json({"error": "not found"}, 404)
                if not _downloads_here():
                    return self.send_json({"ok": False, "error": DOWNLOADS_LOCAL_ONLY}, 403)
                return self.send_json(*downloads_cancel(self.read_json()))
            if u.path == "/api/upload":
                return self.api_upload()
            if u.path == "/api/sequence/master":
                return self.api_sequence_master()
            if u.path == "/api/generate":
                return self.api_generate()
            if u.path == "/api/compare":
                return self.api_compare()
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
            if u.path == "/api/guide/history":
                return self.send_json(*guide_history_set(self.read_json()))
            if u.path == "/api/guide/history/clear":
                return self.send_json(*guide_history_clear(self.read_json()))
            if u.path == "/api/lora/download":
                return self.send_json(*self.api_lora_download_start(self.read_json()))
            if u.path == "/api/lora/download/cancel":
                return self.send_json(*self.api_lora_download_cancel(self.read_json()))
            if u.path == "/api/workflows/open":
                return self.send_json(*_wf_answer(workflows_open, self.read_json()))
            if u.path == "/api/speech":
                return self.api_speech()
            if u.path == "/api/forge/music-video":
                return self.send_json(*forge_music_video(self.read_json()))
            if u.path == "/api/forge/stop":
                return self.send_json(*forge_stop(self.read_json()))
            self.send_json({"error": "not found"}, 404)
        except BrokenPipeError:
            pass
        except Exception as e:
            # E1: same as do_GET's catch-all -- full text to the log, a
            # plain sentence plus `detail` to the page.
            traceback.print_exc()
            self.send_json({"ok": False, "error": "Something went wrong handling that request.",
                            "detail": str(e)}, 500)

    def speech_store(self, lane, data):
        """Store generated speech audio the same way api_upload stores a file for
        that lane: process lanes keep it under UPLOADS_DIR/<lane id> with a
        unique sanitized name; ComfyUI lanes POST multipart to /upload/image.
        Returns (name, original, bytes, seconds) or raises ValueError(sentence)."""
        name = "speech.wav"
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)
        if lane_kind(lane) == "process":
            d = os.path.join(UPLOADS_DIR, lane["id"])
            os.makedirs(d, exist_ok=True)
            stored = uuid.uuid4().hex[:8] + "_" + safe
            path = os.path.join(d, stored)
            with open(path, "xb") as f:
                f.write(data)
            secs = _audio_upload_seconds(name, path=path)
            entry = {"name": stored, "original": name, "bytes": len(data)}
            if secs is not None:
                entry["seconds"] = secs
            log("Kept speech for %s" % lane["name"])
            return entry
        # ComfyUI lane: POST multipart to /upload/image
        res = http_post_multipart(
            lane_url(lane, "/upload/image"),
            {"type": "input", "overwrite": "false"},
            [("image", name, "audio/wav", data)])
        if "_http_error" in res or not res.get("name"):
            raise ValueError("%s would not take the speech file. Try a smaller file or the other lane."
                             % lane["name"])
        stored_name = res["name"]
        if res.get("subfolder"):
            stored_name = res["subfolder"] + "/" + stored_name
        secs = _audio_upload_seconds(name, data=data)
        entry = {"name": stored_name, "original": name, "bytes": len(data)}
        if secs is not None:
            entry["seconds"] = secs
        log("Sent speech over to %s" % lane["name"])
        return entry

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
                entry = {"name": stored, "original": p["filename"], "bytes": len(p["data"])}
                secs = _audio_upload_seconds(name, path=os.path.join(d, stored))
                if secs is not None:
                    entry["seconds"] = secs
                uploaded.append(entry)
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
            entry = {"name": name, "original": p["filename"], "bytes": len(p["data"])}
            secs = _audio_upload_seconds(p["filename"], data=p["data"])
            if secs is not None:
                entry["seconds"] = secs
            uploaded.append(entry)
        log("Sent %d picture/clip(s) over to %s" % (len(uploaded), lane["name"]))
        return self.send_json({"ok": True, "files": uploaded})

    def api_speech(self):
        """POST /api/speech JSON -> calls configured OpenAI-style TTS, stores the
        wav like api_upload does, returns the same {"ok": true, "files": [...]} shape."""
        if not SPEECH_ENABLED:
            return self.send_json({"ok": False, "error": "No speech source is set up. Add a \"speech\" section to config.json, or use your own recording."}, 404)
        p = self.read_json()
        lane_id = (p.get("lane") or "").strip()
        text = (p.get("text") or "").strip()
        voice = (p.get("voice") or "").strip() or None
        instructions = (p.get("instructions") or "").strip() or None
        if not lane_id:
            return self.send_json({"ok": False, "error": "unknown lane %r" % lane_id}, 400)
        lane = LANE_BY_ID.get(lane_id)
        if not lane:
            return self.send_json({"ok": False, "error": "unknown lane %r" % lane_id}, 400)
        if not text:
            return self.send_json({"ok": False, "error": "The text to speak cannot be empty."}, 400)
        if len(text) > 2000:
            return self.send_json({"ok": False, "error": "The text is too long (max 2000 characters)."}, 400)
        # Build the request payload for the TTS endpoint
        payload = {"model": SPEECH_MODEL, "input": text, "voice": voice or SPEECH_VOICE, "response_format": "wav"}
        if instructions:
            payload["instructions"] = instructions
        headers = {"Content-Type": "application/json"}
        if SPEECH_API_KEY:
            headers["Authorization"] = "Bearer " + SPEECH_API_KEY
        url = SPEECH_URL + "/audio/speech"
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=SPEECH_TIMEOUT) as r:
                # Cap at 50 MB
                max_bytes = 50 * 1024 * 1024
                data = b""
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    data += chunk
                    if len(data) > max_bytes:
                        return self.send_json({"ok": False, "error": "The speech source returned more than 50 MB."}, 502)
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            detail = raw[:300]
            log("Speech source HTTP error %d: %s" % (e.code, detail))
            return self.send_json({"ok": False, "error": "The speech source returned an error.", "detail": detail}, 502)
        except Exception as e:
            log("Speech source request failed: %s" % str(e)[:300])
            return self.send_json({"ok": False, "error": "Could not reach the speech source.", "detail": str(e)[:300]}, 502)
        # Validate it's a readable sound file
        secs = _audio_upload_seconds("speech.wav", data=data)
        if secs is None:
            return self.send_json({"ok": False, "error": "The speech source did not return a sound file."}, 502)
        # Store it like api_upload does
        try:
            entry = self.speech_store(lane, data)
        except ValueError as e:
            return self.send_json({"ok": False, "error": str(e)}, 502)
        log("Speech generated for %s (%.2fs)" % (lane["name"], secs))
        return self.send_json({"ok": True, "files": [entry]})

    def api_sequence_master(self):
        """Multipart: id, optional rev, file. The size is checked BEFORE the body is read."""
        ctype = self.headers.get("Content-Type", "")
        m = re.search(r"boundary=([^;]+)", ctype)
        if not m:
            return self.send_json({"ok": False, "error": "expected multipart"}, 400)
        if int(self.headers.get("Content-Length") or 0) > MASTER_MAX_BYTES + 1024 * 1024:
            return self.send_json({"ok": False, "error": "The sound file is larger than %d MB." % (MASTER_MAX_BYTES // (1024 * 1024))}, 413)
        parts = parse_multipart(self.read_body(), m.group(1).strip().strip('"').encode())
        sid, rev, fpart = "", None, None
        for p in parts:
            if p["name"] == "id" and not p["filename"]:
                sid = p["data"].decode("utf-8", "replace").strip()
            elif p["name"] == "rev" and not p["filename"]:
                try:
                    rev = int(p["data"].decode("utf-8", "replace").strip())
                except ValueError:
                    rev = None
            elif p["filename"]:
                fpart = p
        if fpart is None:
            return self.send_json({"ok": False, "error": "No file came with that."}, 400)
        return self.send_json(*seq_master_import(sid, rev, fpart["filename"], fpart["data"]))

    def api_generate(self):
        return self.send_json(*generate(self.read_json()))

    def api_compare(self):
        """POST /api/compare: run the same request across a single axis value."""
        p = self.read_json()
        compare = p.get("compare")
        if not isinstance(compare, dict):
            return self.send_json({"ok": False, "error": "Missing compare axis and values."}, 400)
        axis = compare.get("axis")
        values = compare.get("values")
        if not isinstance(axis, str):
            return self.send_json({"ok": False, "error": "Missing axis."}, 400)
        if not isinstance(values, list):
            return self.send_json({"ok": False, "error": "Values must be a list."}, 400)
        if len(values) < 2 or len(values) > 6:
            return self.send_json({"ok": False, "error": "Need 2 to 6 values, got %d." % len(values)}, 400)

        kind = p.get("kind")
        if kind not in ("image", "video"):
            return self.send_json({"ok": False, "error": "Compare works on pictures and clips."}, 400)

        mode = p.get("mode", "")
        # Derive qmode the same way generate() does.
        if kind == "image":
            qmode = "edit" if mode == "edit" else "t2i"
        else:  # video
            qmode = mode if mode in ("ref2v", "continue") else "fl2va"

        # Build the set of valid axis ids.
        valid_axes = ["seed", "quality"]
        for f in engines.fields(kind, qmode):
            if f["type"] in ("int", "number", "select"):
                valid_axes.append(f["id"])

        if axis not in valid_axes:
            return self.send_json(
                {"ok": False,
                 "error": "Unknown axis %r. Allowed: %s." % (axis, ", ".join(valid_axes))},
                400)

        # Validate values for this axis.
        if axis == "quality":
            allowed = [t["id"] for t in engines.quality(kind, qmode)]
            for v in values:
                if v not in allowed:
                    return self.send_json(
                        {"ok": False, "error": "Unknown quality setting %r." % v}, 400)
        else:
            # For int/number/select axes, validate against field definition.
            field_def = None
            if axis != "seed":
                field_def = next((f for f in engines.fields(kind, qmode) if f["id"] == axis), None)
            for v in values:
                if axis == "seed":
                    try:
                        int(v)
                    except (ValueError, TypeError):
                        return self.send_json(
                            {"ok": False, "error": "%s needs a whole number." % (axis)}, 400)
                elif field_def:
                    try:
                        coerced = _coerce_field_value(field_def, v)
                    except ValueError as e:
                        return self.send_json({"ok": False, "error": str(e)}, 400)
                    # Check the declared range if present.
                    rng = field_def.get("range")
                    if rng and len(rng) == 2:
                        lo, hi = rng[0], rng[1]
                        if lo is not None and coerced < lo:
                            return self.send_json(
                                {"ok": False, "error": "%s must be >= %s." % (field_def.get("label", axis), lo)}, 400)
                        if hi is not None and coerced > hi:
                            return self.send_json(
                                {"ok": False, "error": "%s must be <= %s." % (field_def.get("label", axis), hi)}, 400)

        # Pin a single seed for all runs (when axis is not "seed").
        pinned_seed = None
        if axis != "seed":
            if p.get("seed") is not None:
                try:
                    pinned_seed = int(p["seed"])
                except (ValueError, TypeError):
                    pinned_seed = None
            if pinned_seed is None:
                pinned_seed = random.randint(1, 2 ** 48)

        # Clone the body once, then modify per-value.
        body = dict(p)

        queued = []
        group = uuid.uuid4().hex[:12]

        for val in values:
            copy = dict(body)
            # Set the axis value in both top-level and values dict.
            if axis == "quality":
                copy["quality"] = val
            else:
                copy[axis] = val
                if isinstance(copy.get("values"), dict):
                    copy["values"] = dict(copy["values"])
                    copy["values"][axis] = val
            if axis != "seed":
                copy["seed"] = pinned_seed

            result, code = generate(copy)
            if not result.get("ok") or code != 200:
                return self.send_json(
                    {"ok": False, "error": result.get("error", "generate failed"),
                     "queued": queued},
                    code)

            job = result["job"]
            with JOBS_LOCK:
                if job["id"] in JOBS:
                    JOBS[job["id"]]["compare"] = {"group": group, "axis": axis, "value": val}
            save_jobs()
            queued.append({"id": job["id"], "value": val})

        log("Compare %s axis=%s count=%d" % (LANE_BY_ID.get(p.get("lane", ""), {}).get("name", p.get("lane", "")), axis, len(values)), "compare")
        return self.send_json({"ok": True, "group": group, "axis": axis,
                               "jobs": queued})

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
        """Remove a finished result from the gallery. This drops OUR record of the job; the
        picture or clip stays on the lane's own disk UNLESS that lane opted in to deleting it
        too (config.json "outputs", UX-2 #5 -- off by default, so a fresh config still deletes
        nothing, matching AGENTS.md's "this app never deletes your files" for every lane that
        hasn't explicitly turned it on)."""
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
            # UX-2 #5: opt-in per lane (lane_output_path returns None when
            # the lane has no "outputs" configured -- the default, and the
            # existing "this app never deletes your files" behaviour). The
            # sequence-in-use refusal above already ran, so nothing left
            # here is still needed by a Cutting Room sequence. A file that
            # is already gone, or that fails the containment check, is
            # skipped rather than failing the whole Remove -- the History
            # entry still comes out either way.
            lane = LANE_BY_ID.get(j.get("lane"))
            deleted_files = False
            if lane is not None:
                for o in j.get("outputs") or []:
                    if o.get("type", "output") != "output":
                        continue
                    try:
                        path = lane_output_path(lane, o)
                    except ValueError:
                        continue
                    if not path:
                        continue
                    try:
                        os.remove(path)
                        deleted_files = True
                    except OSError:
                        pass
            with JOBS_LOCK:
                JOBS.pop(jid, None)
        save_jobs()
        return self.send_json({"ok": True, "deleted_files": deleted_files})


# ---------------------------------------------------------------------------
# Forge Master v0: a finished song + one photo -> a music video, in one go.
# forge_master.py plans the shots and writes the scenes, forge_run.py orchestrates; this block is the thin
# adapter over this server's own sequence / generate / carry functions, and the three routes.
# ---------------------------------------------------------------------------

class _ForgeBackend:
    """The backend forge_run.MusicVideoRun is written against (see its docstring). Every method calls an existing
    server function; anything a person should read is raised as forge_run.RunError."""

    def __init__(self, run_id, lane, w, h):
        self.run_id, self.lane, self.w, self.h = run_id, lane, w, h

    # -- sequence -----------------------------------------------------------
    def _rev(self, sid):
        with SEQ_LOCK:
            seq = _seq_read(sid)
        if seq is None:
            raise forge_run.RunError("The sequence is gone.")
        return seq["rev"]

    def _op(self, sid, op, **kw):
        for _ in range(3):          # a take landing between the read and the write makes the rev stale: read again
            body, code = seq_op(dict(kw, id=sid, rev=self._rev(sid), op=op))
            if code == 200:
                return body
            if code != 409:
                raise forge_run.RunError(body.get("error") or "That step did not go through.")
        raise forge_run.RunError("The sequence kept changing; try again.")

    def create_sequence(self, title):
        body, code = seq_create({"title": title, "mode": "sequence"})
        if code >= 300 or not body.get("id"):
            raise forge_run.RunError(body.get("error") or "Could not start a sequence.")
        return body["id"]

    def set_canvas(self, sid, w, h):
        self._op(sid, "set_canvas", width=w, height=h)

    def set_audio_led(self, sid):
        self._op(sid, "set_audio_led", on=True)

    def import_master(self, sid, song_job_id):
        try:
            data, fname = _resolve_job_output_bytes(song_job_id, 0)
        except ValueError as e:
            raise forge_run.RunError(str(e))
        body, code = seq_master_import(sid, self._rev(sid), fname, data)
        if code != 200:
            raise forge_run.RunError(body.get("error") or "The song could not be used.")
        return float((body.get("master") or {}).get("seconds") or 0)

    def add_shot(self, sid, window, prompt, start_image_name, w, h):
        body = self._op(sid, "add_slot", lane="video", cap="video", mode="ltx",
                        values={"prompt": prompt, "start_image": start_image_name, "length": window["frames"],
                                "width": w, "height": h, "two_stage": True, "image_strength": 0.9})
        return body["slots"][-1]["id"]

    def pick(self, sid, slot_id, job_id):
        self._op(sid, "pick_take", slot_id=slot_id, job_id=job_id)

    def generate_shot(self, sid, slot_id):
        body, code = seq_generate({"id": sid, "slot_id": slot_id})
        if code != 200 or not body.get("ok"):
            raise forge_run.RunError(body.get("error") or "A shot could not be started.")
        return body["job"]["id"]

    def cut(self, sid):
        body, code = seq_cut_start({"id": sid})
        if code != 200 or not body.get("cut_id"):
            raise forge_run.RunError(body.get("error") or "The cut could not be started.")
        return body["cut_id"]

    # -- waiting (every loop honours stop) ------------------------------------
    def _poll(self, timeout, step, probe):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.check_stop()
            r = probe()
            if r is not None:
                return r
            time.sleep(step)
        return None

    def wait_take(self, sid, slot_id, job_id, timeout):
        def probe():
            with SEQ_LOCK:
                seq = _seq_read(sid)
            slot = next((s for s in (seq or {}).get("slots") or [] if s.get("id") == slot_id), None)
            if slot and any(t.get("job_id") == job_id and t.get("file") for t in slot.get("takes") or []):
                return True
            with JOBS_LOCK:
                job = JOBS.get(job_id) or {}
            return False if job.get("status") in ("error", "failed") else None
        return bool(self._poll(timeout, 1.0, probe))

    def wait_cut(self, sid, cut_id, timeout):
        def probe():
            with SEQ_LOCK:
                seq = _seq_read(sid)
            cut = next((c for c in (seq or {}).get("cuts") or [] if c.get("id") == cut_id), None)
            if cut and cut.get("status") in ("done", "error", "interrupted"):
                return {"status": cut["status"], "file": cut.get("file")}
            return None
        return self._poll(timeout, 2.0, probe) or {"status": "timeout"}

    # -- stills ---------------------------------------------------------------
    def make_still(self, prompt, photo_name, w, h):
        body, code = generate({"lane": self.lane["id"], "kind": "image", "mode": "edit", "confirm": True,
                               "prompt": prompt, "ref_images": [photo_name]})
        if code != 200 or not body.get("ok"):
            raise forge_run.RunError(body.get("error") or "A picture could not be started.")
        return body["job"]["id"]

    def wait_job(self, job_id, timeout):
        def probe():
            with JOBS_LOCK:
                job = copy.deepcopy(JOBS.get(job_id) or {})
            return job if job.get("status") in ("done", "error", "failed") else None
        job = self._poll(timeout, 1.0, probe)
        if job is None:
            raise forge_run.RunError("A picture took too long to make.")
        return job

    def carry_still(self, job_id):
        with JOBS_LOCK:
            job = copy.deepcopy(JOBS.get(job_id) or {})
        try:
            name, _note = carry(job, 0, self.lane, fit=(self.w, self.h))
        except ValueError as e:
            raise forge_run.RunError(str(e))
        return name

    # -- helpers --------------------------------------------------------------
    def helper_chat(self, system, user):
        reply, _finish = _helper_chat([{"role": "system", "content": system}, {"role": "user", "content": user}],
                                      max_tokens=4096, timeout=90)
        return reply

    def check_stop(self):
        if forge_run.FORGE_RUN_STOP.get(self.run_id):
            raise forge_run.RunStopped()

    def sleep(self, s):
        time.sleep(s)

    def plan(self, plan_line, n_shots):
        forge_run.update_run(self.run_id, plan_line=plan_line, total=2 * n_shots)

    def say(self, stage, message, done, total):
        fields = {"stage": stage, "message": message}
        if total:
            fields.update(done=done, total=total)
        forge_run.update_run(self.run_id, **fields)
        log("Music video: %s (%d/%d)" % (stage, done, total))      # counts only: never the song, photo, lyrics or prompts


def _forge_run_validation(p):
    """Validate the forge/music-video request body.

    Returns (lane, errors) where errors is a list of (status_code, sentence).
    """
    if not isinstance(p, dict):
        return None, [(400, "Send a JSON object.")]

    lane_id = (p.get("lane") or "").strip()
    lane = LANE_BY_ID.get(lane_id)
    if not lane:
        return None, [(400, "unknown lane %r" % lane_id)]
    with STATE_LOCK:
        lane_up = LANE_STATE.get(lane["id"], {}).get("up")
    if not lane_up:
        return None, [(400, "%s is offline right now. Pick one of the lanes glowing green." % lane["name"])]

    song_job = (p.get("song_job") or "").strip()
    if not song_job:
        return None, [(400, "Pick a finished song first.")]
    with JOBS_LOCK:
        song_job_rec = JOBS.get(song_job)
    if not song_job_rec or song_job_rec.get("status") != "done":
        return None, [(400, "Pick a finished song first.")]
    # Verify it's an audio job
    outs = song_job_rec.get("outputs") or []
    if not outs:
        return None, [(400, "Pick a finished song first.")]
    first_out = outs[0]
    media = first_out.get("media", "")
    if media != "audio":
        # Check by extension as fallback
        fn = first_out.get("filename", "").lower()
        if not any(fn.endswith(e) for e in (".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus")):
            return None, [(400, "Pick a finished song first.")]

    photo = (p.get("photo") or "").strip()
    if not photo:
        return None, [(400, "Add a photo of who is in the video.")]

    style = (p.get("style") or "").strip()
    if len(style) > 200:
        return None, [(400, "The style note is too long (max 200 characters).")]

    lyrics = (p.get("lyrics") or "").strip()
    if len(lyrics) > 6000:
        return None, [(400, "The lyrics are too long (max 6000 characters).")]
    # If no lyrics provided, try to get from song job's meta
    if not lyrics:
        lyrics = (song_job_rec.get("args") or {}).get("lyrics", "")
        if isinstance(lyrics, str):
            lyrics = lyrics.strip()
        if not lyrics:
            lyrics = None

    width = p.get("width")
    height = p.get("height")
    if width is None:
        width = 576
    if height is None:
        height = 768
    try:
        width = int(width)
        height = int(height)
    except (TypeError, ValueError):
        return None, [(400, "Width and height must be numbers.")]
    # Validate multiples of 16, 256..1152
    if width < 256 or width > 1152 or width % 16 != 0:
        return None, [(400, "Width must be a multiple of 16, between 256 and 1152.")]
    if height < 256 or height > 1152 or height % 16 != 0:
        return None, [(400, "Height must be a multiple of 16, between 256 and 1152.")]

    return {
        "lane": lane, "song_job": song_job, "photo": photo,
        "style": style, "lyrics": lyrics, "width": width, "height": height,
    }, []


def _forge_thread(run_id, v):
    be = _ForgeBackend(run_id, v["lane"], v["width"], v["height"])
    try:
        out = forge_run.MusicVideoRun(be).run(v["song_job"], v["photo"], v["style"], v["lyrics"] or "", v["width"], v["height"])
        forge_run.update_run(run_id, status="done", stage="done", message="Your video is ready.", file=out["file"],
                             cut_id=out["cut_id"], sequence=out["sequence"], done=2 * out["shots"], total=2 * out["shots"])
    except forge_run.RunStopped:
        forge_run.update_run(run_id, status="stopped", message="Stopped. The shots made so far are in the Cutting Room.")
    except forge_run.RunError as e:
        forge_run.update_run(run_id, status="error", error=str(e), message=str(e))
    except Exception:                                            # never leave a run "running" forever
        traceback.print_exc()
        forge_run.update_run(run_id, status="error", error="Something went wrong making the video.",
                             message="Something went wrong making the video.")


def forge_music_video(p):
    """POST /api/forge/music-video -> (body, code). The whole run happens on a background thread."""
    v, errors = _forge_run_validation(p)
    if errors:
        code, sentence = errors[0]
        return {"ok": False, "error": sentence}, code
    run_id = forge_run.new_run()
    if run_id is None:
        return {"ok": False, "error": "A music video is already being made. Wait for it, or stop it first."}, 409
    threading.Thread(target=_forge_thread, args=(run_id, v), daemon=True).start()
    log("Music video started on %s" % v["lane"]["name"])
    return {"ok": True, "run_id": run_id}, 200


def forge_run_get(run_id):
    rec = forge_run.get_run(run_id)
    if rec is None:
        return ({"error": "There is no such run."}, 404) if run_id else ({"run": None}, 200)
    return rec, 200


def forge_stop(p):
    rid = (p.get("id") or "") if isinstance(p, dict) else ""
    if not forge_run.request_stop(rid):
        return {"ok": False, "error": "There is no such run."}, 404
    return {"ok": True}, 200


def _open_browser_if_asked():
    """`--open`: open the default browser on this computer once the server is listening.
    BWF_OPENED stops the Setup re-exec (same argv, same environment) from opening a second tab."""
    if "--open" not in sys.argv or os.environ.get("BWF_OPENED") == "1":
        return
    os.environ["BWF_OPENED"] = "1"

    def _go():
        time.sleep(1)
        try:
            log("Opening your browser...", "ok")
            webbrowser.open("http://127.0.0.1:%d/" % PORT)
        except Exception:
            pass
    threading.Thread(target=_go, daemon=True).start()


class _Server(ThreadingHTTPServer):
    """The listening socket, with one Windows-only fix.

    On Windows SO_REUSEADDR does not mean "the old socket is gone": it lets a
    SECOND process bind the same address and port, so two copies of this app
    (or any other local program) silently share the port and requests go to
    whichever one answers first. SO_EXCLUSIVEADDRUSE is the Windows spelling of
    "no, this port is mine" and is set before the bind. POSIX needs none of
    this -- a listening socket already refuses a second binder -- so its
    behaviour here is exactly what ThreadingHTTPServer did.
    """
    allow_reuse_address = (os.name != "nt")

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def main():
    # Bind first, before starting any threads: if the port is taken there is no
    # point polling seven lanes, and a stack trace is a poor way to say
    # "something else is already using this port".
    try:
        srv = _Server((BIND, PORT), Handler)
    except OSError as e:
        if getattr(e, "errno", None) in (48, 98, 10048):  # EADDRINUSE on BSD / Linux / Windows
            find = ("netstat -ano | findstr :%d" if os.name == "nt"
                    else "lsof -nP -iTCP:%d -sTCP:LISTEN") % PORT
            die("Port %d is already in use.\n\n"
                "Either this app is already running, or something else has the port.\n"
                "Find it with:  %s\n"
                "Or pick another port by changing \"port\" in %s."
                % (PORT, find, os.path.basename(CONFIG_FILE)))
        if getattr(e, "errno", None) in (49, 10049):  # EADDRNOTAVAIL
            die("Cannot bind to %r. Check \"bind\" in %s: use \"0.0.0.0\" for every\n"
                "interface, or \"127.0.0.1\" for this machine only."
                % (BIND, os.path.basename(CONFIG_FILE)))
        raise
    srv.daemon_threads = True

    _dl_load()   # W3 R4: a model download queue survives the Setup re-exec and any restart
    with MODEL_DL_LOCK:
        pending = any(e["state"] in ("queued", "running") for e in MODEL_DL["queue"])
        if pending and _downloads_here():
            _dl_kick()
    if pending and not _downloads_here():
        log("Model downloads are waiting, not running. " + DOWNLOADS_LOCAL_ONLY, "warn")
    if SETUP_MODE:
        # Basename only, like below: the full path is how a home directory
        # ends up in a screenshot. No lanes, no pollers, no jobs to load.
        log("No %s yet: open http://127.0.0.1:%d/ in a browser on this computer to set up %s."
            % (os.path.basename(CONFIG_FILE), PORT, TITLE), "ok")
        _open_browser_if_asked()
        srv.serve_forever()
        return
    load_jobs()
    forge_run.configure(DATA_DIR)
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
    _open_browser_if_asked()
    srv.serve_forever()


if __name__ == "__main__":
    main()
