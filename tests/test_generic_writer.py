"""Gate for the generic writer (P2c "One voice"): a mode with no pack writer
still gets "Help me write this" through POST /api/guide/skill. Its system
prompt is the room guide's compact voice + the mode's own prompt_guides line
+ a fixed PROMPT/QUESTION contract; PROMPT fills the mode's first text field.
"Describe this picture" is the same call with "pictures" (vision-gated).
Against a fake OpenAI-compatible helper that records every request. No browser.

Run: python3 tests/test_generic_writer.py
"""
import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FAILED = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond:
        print("        -> %s" % (detail,))
        FAILED.append(name)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


HELPER_STATE = {"reply": "PROMPT: x", "requests": [], "replies": []}


class FakeHelperHandler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        resp = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def do_GET(self):
        self._send(404, {"error": "no"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        HELPER_STATE["requests"].append(json.loads(self.rfile.read(n) or b"{}"))
        reply = HELPER_STATE["replies"].pop(0) if HELPER_STATE["replies"] else HELPER_STATE["reply"]
        self._send(200, {"choices": [{"message": {"content": reply}, "finish_reason": "stop"}]})

    def log_message(self, *a):
        pass


fake_helper = ThreadingHTTPServer(("127.0.0.1", 0), FakeHelperHandler)
threading.Thread(target=fake_helper.serve_forever, daemon=True).start()

# A tiny real PNG, uploaded to a process lane: the one upload the server reads
# from its own disk, so "Describe this picture" needs no ComfyUI here.
PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
                    "0000000c4944415408d763f8cfc000000301010018dd8db00000000049454e44ae426082")


def start_server(name, helper):
    scratch = tempfile.mkdtemp(prefix="bwf_generic_writer_")
    cfg_path = os.path.join(scratch, "config.json")
    port = free_port()
    cfg = {"port": port, "bind": "127.0.0.1", "title": "generic writer",
           "timing": {"poll_seconds": 30, "job_poll_seconds": 30},
           "lanes": [{"id": "t", "name": "Test lane", "host": "127.0.0.1", "port": 1,
                      "caps": ["image", "video", "audio"]},
                     {"id": "cpu", "name": "This machine", "kind": "process", "caps": ["3d"]}]}
    if helper is not None:
        cfg["helper"] = dict({"url": "http://127.0.0.1:%d/v1" % fake_helper.server_address[1],
                              "model": "test-model", "timeout_s": 2}, **helper)
    with open(cfg_path, "w") as f:
        json.dump(cfg, f)
    os.environ["GENCENTER_CONFIG"] = cfg_path
    os.environ["GENCENTER_DATA"] = os.path.join(scratch, "data")
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    os.makedirs(os.path.join(mod.UPLOADS_DIR, "cpu"), exist_ok=True)
    with open(os.path.join(mod.UPLOADS_DIR, "cpu", "pic.png"), "wb") as f:
        f.write(PNG)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), mod.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return mod, port


srv, PORT = start_server("srv_generic", {"vision": True})
_, PORT_BLIND = start_server("srv_generic_blind", {"vision": False})
_, PORT_NONE = start_server("srv_generic_nobrain", None)


def skill(body, port=None):
    data = json.dumps(body).encode()
    r = urllib.request.Request("http://127.0.0.1:%d/api/guide/skill" % (port or PORT), data=data, method="POST",
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


import engines  # noqa: E402
import guides  # noqa: E402

G = guides.load_all()
# P3d gave t2i a pack writer of its own (tests/test_picture_writers.py). This
# suite gates the GENERIC writer, with t2i as its example mode, so it takes
# that pack writer out for the run (the server shares this engines module).
engines._owner("image", "t2i")["writers"].pop("t2i")
NO_PICTURE = "\n\n[No picture is attached to this message.]"
PIC = {"lane": "cpu", "upload": "pic.png"}

# ---------------------------------------------------------------------------
# 1. the generic writer itself, per mode
# ---------------------------------------------------------------------------
print("writer: built from the room guide's compact voice and the mode's prompt_guide")
for room, gid, cap, mode, fills in (("picture", "picture", "image", "t2i", "prompt"),
                                    ("video", "motion", "video", "ltx", "prompt"),
                                    ("sfx", "sound", "audio", "sfx", "prompt"),
                                    ("talking", "motion", "video", "talking", "line"),
                                    ("music", "sound", "audio", "music", "caption")):
    w = srv._generic_writer(cap, mode, G[gid])
    guide_line = engines.prompt_guide(cap, mode)
    check("writer %s: the mode has a prompt_guide to use" % mode, bool(guide_line))
    check("writer %s: the system prompt holds the mode's prompt_guide" % mode, w and guide_line in w["prompt"])
    check("writer %s: and starts with the room guide's compact text" % mode,
          w and w["prompt"].startswith(G[gid]["projections"]["compact"]["text"].rstrip()))
    check("writer %s: the contract names both reply shapes" % mode,
          w and "QUESTION: <one short question>" in w["prompt"] and "PROMPT: <the finished words" in w["prompt"])
    check("writer %s: PROMPT fills %s" % (mode, fills), w and w["keys"]["PROMPT"] == fills, w and w["keys"])
    check("writer %s: every other text/number/select field is a setting line" % mode, w and sorted(v for k, v in w["keys"].items() if k != "PROMPT")
          == sorted(f["id"] for f in engines.fields(cap, mode) if f["id"] != fills
                    and f["type"] in ("text", "textarea", "number", "int", "select")), w and w["keys"])
    check("writer %s: asks only what changes the result" % mode, w and "Ask only what changes the result for this mode" in w["prompt"]
          and "If the request or an earlier answer already answers it, don't ask." in w["prompt"])
check("writer: a mode with no text field has none", srv._generic_writer("image", "cutout", G["picture"]) is None)

# ---------------------------------------------------------------------------
# 2. POST /api/guide/skill for a mode with no pack writer
# ---------------------------------------------------------------------------
print("skill: Picture t2i, PROMPT fills the prompt")
CTX = {"mode": "t2i", "fields": {"prompt": "a lighthouse"}}
del HELPER_STATE["requests"][:]
HELPER_STATE["reply"] = "PROMPT: A lighthouse at dusk on a rocky point,\nwarm light in the lamp room."
code, b = skill({"room": "picture", "mode": "t2i", "topic": "a lighthouse", "context": CTX})
check("t2i: 200 with the prompt as the field", code == 200 and b.get("ok") and b.get("fields") ==
      {"prompt": "A lighthouse at dusk on a rocky point,\nwarm light in the lamp room."}, b)
check("t2i: no problems, no retry", b.get("problems") == [] and b.get("retried") is False and len(HELPER_STATE["requests"]) == 1)
req = HELPER_STATE["requests"][-1]
system = req["messages"][0]["content"]
check("t2i: the system prompt holds the room guide's compact text",
      G["picture"]["projections"]["compact"]["text"].strip() in system)
check("t2i: and the mode's prompt_guide", engines.prompt_guide("image", "t2i") in system)
user = req["messages"][1]["content"]
check("t2i: the user message is the context line, then the request, grounded",
      user.startswith("[Current room: Picture") and "Request: a lighthouse" in user and user.endswith(NO_PICTURE), user)
check("t2i: sent is what the brain was asked", b.get("sent") == {"system": system, "user": user}, b.get("sent"))

print("skill: Video ltx and Sound FX")
HELPER_STATE["reply"] = "PROMPT: A slow push in on a rain-soaked street at night."
code, b = skill({"room": "video", "mode": "ltx", "topic": "a rainy street"})
check("ltx: PROMPT fills the prompt", code == 200 and b.get("fields") == {"prompt": "A slow push in on a rain-soaked street at night."}, b)
check("ltx: the system prompt holds ltx's own prompt_guide",
      engines.prompt_guide("video", "ltx") in HELPER_STATE["requests"][-1]["messages"][0]["content"])
HELPER_STATE["reply"] = "**PROMPT:** a heavy wooden door creaking open"
code, b = skill({"room": "sfx", "mode": "sfx", "topic": "a creaky door"})
check("sfx: PROMPT fills the prompt (bold key tolerated)", code == 200 and b.get("fields") == {"prompt": "a heavy wooden door creaking open"}, b)

print("skill: QUESTION")
HELPER_STATE["reply"] = "QUESTION: Day or night?"
code, b = skill({"room": "picture", "mode": "t2i", "topic": "a street"})
check("question: returned as the question", code == 200 and b.get("question") == "Day or night?" and "fields" not in b, b)

print("skill: a malformed reply")
for label, reply in (("prose", "Sure! Here is a lovely prompt for you."), ("PROMPT NONE", "PROMPT: NONE"),
                     ("PROMPT empty", "PROMPT:")):
    HELPER_STATE["reply"] = reply
    code, b = skill({"room": "picture", "mode": "t2i", "topic": "a street"})
    check("malformed (%s): the parse-failure shape" % label, code == 502 and b.get("ok") is False
          and b.get("error") == "The writer's answer didn't come back in the expected shape."
          and b.get("raw") == reply and "sent" in b, b)

print("skill: a mode with a pack writer still uses it")
HELPER_STATE["reply"] = "QUESTION: Sung, or instrumental?"
code, b = skill({"room": "music", "mode": "song", "topic": "a song"})
check("song: the pack writer's own prompt", code == 200 and HELPER_STATE["requests"][-1]["messages"][0]["content"]
      == engines.writer("audio", "song")["prompt"])

# ---------------------------------------------------------------------------
# 3. "Describe this picture": the same call with pictures
# ---------------------------------------------------------------------------
print("describe: a helper that can see gets the picture")
del HELPER_STATE["requests"][:]
HELPER_STATE["reply"] = "PROMPT: A single red pixel on white."
code, b = skill({"room": "picture", "mode": "t2i", "topic": "", "pictures": [PIC]})
check("describe: 200, PROMPT fills the prompt", code == 200 and b.get("fields") == {"prompt": "A single red pixel on white."}, b)
check("describe: vision reported", b.get("vision") is True and b.get("sent", {}).get("pictures") == 1, b)
content = HELPER_STATE["requests"][-1]["messages"][1]["content"] if HELPER_STATE["requests"] else None
check("describe: the user message carries text + the picture", isinstance(content, list)
      and [x["type"] for x in content] == ["text", "image_url"]
      and content[1]["image_url"]["url"].startswith("data:image/png;base64,"), type(content))
text = content[0]["text"] if isinstance(content, list) else ""
check("describe: it asks for a description, grounded with the picture",
      "Describe the attached picture" in text and text.endswith("[1 picture attached.]"), text)
check("describe: the generic writer even on a pack-writer mode", code == 200 and skill(
    {"room": "music", "mode": "song", "pictures": [PIC]})[0] == 200
      and "PROMPT: <the finished words" in HELPER_STATE["requests"][-1]["messages"][0]["content"])
code, b = skill({"room": "picture", "mode": "t2i", "topic": "keep it moody", "pictures": [PIC]})
check("describe: the user's own words ride along", "The user's own words so far: keep it moody"
      in HELPER_STATE["requests"][-1]["messages"][1]["content"][0]["text"])

print("describe: a helper that cannot see is told so and gets no picture")
del HELPER_STATE["requests"][:]
HELPER_STATE["reply"] = "QUESTION: I can't see the picture, so tell me what it shows."
code, b = skill({"room": "picture", "mode": "t2i", "pictures": [PIC]}, port=PORT_BLIND)
content = HELPER_STATE["requests"][-1]["messages"][1]["content"] if HELPER_STATE["requests"] else None
check("blind: the call went, text only, with the truth", isinstance(content, str)
      and content.endswith("[The user attached a picture, but this helper cannot see pictures.]"), content)
check("blind: the question comes back with vision false", code == 200 and b.get("question") and b.get("vision") is False
      and b.get("sent", {}).get("pictures") == 0, b)

print("refusals")
for label, body, port, want, sentence in (
        ("no topic, no picture", {"room": "picture", "mode": "t2i", "topic": " "}, None, 400, "Say what it should be about first."),
        ("pictures not a list", {"room": "picture", "mode": "t2i", "pictures": PIC}, None, 400, None),
        ("too many pictures", {"room": "picture", "mode": "t2i", "pictures": [PIC, PIC]}, None, 400, None),
        ("a bad picture", {"room": "picture", "mode": "t2i", "pictures": [{"x": 1}]}, None, 400, None),
        ("a missing upload", {"room": "picture", "mode": "t2i", "pictures": [{"lane": "cpu", "upload": "gone.png"}]},
         None, 400, "I cannot find that upload any more."),
        ("no text field", {"room": "cleanup", "mode": "cutout", "topic": "t"}, None, 404, "This mode has no writer yet."),
        ("no brain", {"room": "picture", "mode": "t2i", "topic": "t"}, PORT_NONE, 409, None)):
    code, b = skill(body, port=port)
    check("refusal (%s): %d" % (label, want), code == want and b.get("ok") is False and b.get("error")
          and (sentence is None or b["error"] == sentence), (code, b))

# ---------------------------------------------------------------------------
# 3b. the write conversation: OPTIONS, answers in order, the cap, settings
# ---------------------------------------------------------------------------
print("settings: the mode's own fields are listed, and filled from <FIELD_ID> lines")
w = srv._generic_writer("image", "t2i", G["picture"])
check("settings: t2i lists its numbers and selects with their ranges and choices",
      "\nWIDTH: " in w["prompt"] and "a whole number 256-2048" in w["prompt"]
      and "\nSAMPLER: " in w["prompt"] and "one of: seeds_2, euler" in w["prompt"], w["prompt"][-1500:])
del HELPER_STATE["requests"][:]
HELPER_STATE["reply"] = "WIDTH: 1024\nSAMPLER: euler\nSTEPS: 999\nGUIDANCE_STYLE: Loud\nPROMPT: a red kite"
code, b = skill({"room": "picture", "mode": "t2i", "topic": "a kite, square"})
check("settings: good values fill their fields (numbers as numbers, selects as their option)",
      code == 200 and b["fields"].get("width") == 1024 and b["fields"].get("sampler") == "euler"
      and b["fields"].get("prompt") == "a red kite", b)
check("settings: a bad number and a bad choice are left out, each with a sentence, never clamped",
      "steps" not in b["fields"] and "guidance_style" not in b["fields"]
      and any("999" in x and "1-80" in x for x in b["problems"]) and any("Loud" in x for x in b["problems"]), b.get("problems"))
check("settings: problems get the one automatic retry", b.get("retried") is True and len(HELPER_STATE["requests"]) == 2)

print("options: a question may offer 2-5 choices")
HELPER_STATE["reply"] = "QUESTION: How many kites?\nOPTIONS: One | Two | A whole sky of them"
code, b = skill({"room": "picture", "mode": "t2i", "topic": "kites"})
check("options: returned with the question", code == 200 and b.get("question") == "How many kites?"
      and b.get("options") == ["One", "Two", "A whole sky of them"], b)
HELPER_STATE["reply"] = "QUESTION: How many kites?\nOPTIONS: just one"
code, b = skill({"room": "picture", "mode": "t2i", "topic": "kites"})
check("options: fewer than 2 choices is no options", b.get("options") == [], b)
HELPER_STATE["reply"] = "QUESTION: Sung, or instrumental?\nOPTIONS: Sung | Instrumental"
code, b = skill({"room": "music", "mode": "song", "topic": "music for my video about the sea"})
check("options: the song writer's question carries them too", b.get("options") == ["Sung", "Instrumental"], b)
check("options: the song writer's own prompt offers them", "OPTIONS: Sung | Instrumental" in engines.writer("audio", "song")["prompt"])

print("answers: every answer goes back, in order")
del HELPER_STATE["requests"][:]
HELPER_STATE["reply"] = "PROMPT: two red kites over a grey sea, square"
ANS = [{"q": "How many kites?", "a": "Two"}, {"q": "Where are they?", "a": "over a grey sea"}]
code, b = skill({"room": "picture", "mode": "t2i", "topic": "kites", "answers": ANS})
user = HELPER_STATE["requests"][-1]["messages"][1]["content"]
check("answers: Q1/A1 then Q2/A2 in the user message", code == 200 and
      "Q1: How many kites?\nA1: Two\nQ2: Where are they?\nA2: over a grey sea" in user, user)
check("answers: under the cap, no write-now line", "No more questions" not in user)

print("cap: after 4 answers the writer must write, naming its defaults")
FOUR = [{"q": "Q%d?" % i, "a": "a%d" % i} for i in range(1, 5)]
del HELPER_STATE["requests"][:]
HELPER_STATE["reply"] = "NOTE: I chose a square picture and daylight.\nPROMPT: a kite on a beach"
code, b = skill({"room": "picture", "mode": "t2i", "topic": "kites", "answers": FOUR})
user = HELPER_STATE["requests"][-1]["messages"][1]["content"]
check("cap: the 5th call tells the writer to write now", srv.GUIDE_SKILL_WRITE_NOW in user, user)
check("cap: the NOTE comes back with the fields", code == 200 and b.get("note") == "I chose a square picture and daylight."
      and b["fields"] == {"prompt": "a kite on a beach"}, b)
del HELPER_STATE["requests"][:]
HELPER_STATE["reply"] = "QUESTION: One more thing?"
code, b = skill({"room": "picture", "mode": "t2i", "topic": "kites", "answers": FOUR})
check("cap: a writer that still asks is asked once more, then refused plainly", code == 502
      and b.get("error") == srv.GUIDE_SKILL_KEPT_ASKING and len(HELPER_STATE["requests"]) == 2
      and "You already had your answers." in HELPER_STATE["requests"][1]["messages"][1]["content"], (code, b))
HELPER_STATE["replies"] = ["QUESTION: One more thing?", "PROMPT: a kite"]
code, b = skill({"room": "picture", "mode": "t2i", "topic": "kites", "answers": FOUR})
check("cap: ... and one that writes on the second ask is fine", code == 200 and b["fields"] == {"prompt": "a kite"}, b)
for label, answers in (("five answers", FOUR + [{"q": "x", "a": "y"}]), ("not a list", {"q": "x", "a": "y"}),
                       ("an empty answer", [{"q": "x", "a": " "}]), ("no q", [{"a": "y"}])):
    code, b = skill({"room": "picture", "mode": "t2i", "topic": "kites", "answers": answers})
    check("answers refused (%s): 400 with a sentence" % label, code == 400 and b.get("ok") is False and b.get("error"), (code, b))

# ---------------------------------------------------------------------------
# 4. the ratchet: the contract names no engine, and coupling has not grown
# ---------------------------------------------------------------------------
print("ratchet")
PAT = r"qwen|minimax|h3_|ace.step|\byue\b|trellis|ltx|birefnet|esrgan|sdxl|rife"
check("ratchet: the generic contract names no engine", not re.search(PAT, srv.GENERIC_WRITER_TASK, re.I))
r = subprocess.run(["bash", os.path.join(ROOT, "scripts", "check-engine-independence.sh")], cwd=ROOT,
                   capture_output=True, text=True)
check("ratchet: check-engine-independence.sh passes", r.returncode == 0, r.stdout + r.stderr)

if FAILED:
    print("FAILED: %d checks: %s" % (len(FAILED), ", ".join(FAILED)))
    sys.exit(1)
print("OK: all checks passed")
