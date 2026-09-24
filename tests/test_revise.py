"""Gate for "Not right? Tell the guide" (P2b): the pack "revisers" contract and
its line parser, the helper's vision capability (config, then the /models
probe), pictures in guide calls (limits, results, uploads, a clip's still,
the grounding line, image_url on the last user message only, nothing sent to
a helper that cannot see), and POST /api/guide/revise against a fake
OpenAI-compatible helper that records every request. No browser.

Run: python3 tests/test_revise.py
"""
import base64
import importlib.util
import json
import os
import shutil
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


# A 1x1 PNG, and the fake helper that records every request.
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
MULTIMODAL = {"models": [{"name": "test-model", "capabilities": ["completion", "multimodal"]}],
              "data": [{"id": "test-model", "meta": {"n_ctx": 8192}}]}
HELPER_STATE = {"reply": "ok", "status": 200, "models": MULTIMODAL, "requests": [], "model_gets": 0}


class FakeHelperHandler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        resp = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            HELPER_STATE["model_gets"] += 1
            if HELPER_STATE["models"] is None:
                return self._send(404, {"error": "no"})
            return self._send(200, HELPER_STATE["models"])
        self._send(404, {"error": "no"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        HELPER_STATE["requests"].append(json.loads(self.rfile.read(n) or b"{}"))
        if HELPER_STATE["status"] != 200:
            return self._send(HELPER_STATE["status"], {"error": "boom"})
        self._send(200, {"choices": [{"message": {"content": HELPER_STATE["reply"]}, "finish_reason": "stop"}]})

    def log_message(self, *a):
        pass


fake_helper = ThreadingHTTPServer(("127.0.0.1", 0), FakeHelperHandler)
HELPER_PORT = fake_helper.server_address[1]
threading.Thread(target=fake_helper.serve_forever, daemon=True).start()
LANES = [{"id": "t", "name": "Test lane", "host": "127.0.0.1", "port": 1, "caps": ["image", "audio"]},
         {"id": "cpu", "name": "This machine", "kind": "process", "caps": ["3d"]}]


def write_config(helper):
    scratch = tempfile.mkdtemp(prefix="bwf_revise_")
    cfg = {"port": free_port(), "bind": "127.0.0.1", "title": "revise",
           "timing": {"poll_seconds": 30, "job_poll_seconds": 30}, "lanes": LANES}
    if helper is not None:
        cfg["helper"] = helper
    path = os.path.join(scratch, "config.json")
    with open(path, "w") as f:
        json.dump(cfg, f)
    return scratch, path, cfg["port"]


def start_server(name, helper):
    scratch, path, port = write_config(helper)
    os.environ["GENCENTER_CONFIG"] = path
    os.environ["GENCENTER_DATA"] = os.path.join(scratch, "data")
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), mod.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return mod, port


BRAIN = {"url": "http://127.0.0.1:%d/v1" % HELPER_PORT, "model": "test-model", "timeout_s": 5}
srv, PORT = start_server("srv_revise", dict(BRAIN))
srv2, PORT2 = start_server("srv_revise_nobrain", None)


def http(path, body=None, port=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request("http://127.0.0.1:%d%s" % (port or PORT, path), data=data,
                               method="POST" if data is not None else "GET",
                               headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


import engines  # noqa: E402

R = engines.reviser("image", "t2i")

# ---------------------------------------------------------------------------
# 1. the pack contract and the parser
# ---------------------------------------------------------------------------
print("contract: the pack's revisers key")
check("contract: t2i has a reviser with label, prompt, keys, fills, edit_mode",
      R is not None and R["label"] == "Picture fixer" and R["prompt"].strip() != ""
      and R["keys"] == ["QUESTION", "DIAGNOSIS", "FIX", "PROMPT", "NOTE", "TWEAK"]
      and R["fills"] == "prompt" and R["edit_mode"] == "edit", R and {k: R[k] for k in R if k != "prompt"})
check("contract: fills is a field of t2i", any(f["id"] == R["fills"] for f in engines.fields("image", "t2i")))
check("contract: edit_mode is a mode of the same cap with that field",
      any(f["id"] == R["fills"] for f in engines.fields("image", R["edit_mode"])))
check("contract: modes without a reviser return None",
      engines.reviser("image", "edit") is None and engines.reviser("audio", "song") is None)
check("contract: the prompt carries the Godzilla worked example",
      "exactly two giant monsters" in R["prompt"] and "golden three-headed mechanical dragon" in R["prompt"]
      and "single-headed winged dragon" in R["prompt"])

code, body = http("/api/engines?lane=t")
modes = [(cap, m) for cap, v in body.items() if cap not in ("rooms", "helper") for m in v["modes"]]
t2i = next(m for cap, m in modes if cap == "image" and m["id"] == "t2i")
check("contract: /api/engines reports the t2i reviser's label", t2i.get("reviser") == {"label": "Picture fixer"}, t2i.get("reviser"))
check("contract: every other mode reports reviser null",
      [m["id"] for cap, m in modes if m is not t2i and m.get("reviser") is not None] == [])
check("contract: every mode carries a reviser key", all("reviser" in m for _, m in modes))

print("parser: line keys")
FULL = ("QUESTION:\nDIAGNOSIS: the name is unknown.\nFIX: reroll\nPROMPT: exactly two monsters.\n"
        "NOTE: pinned the count.\nTWEAK:")
check("parser: a full reply", engines.parse_reviser_reply(R, FULL) == {
    "diagnosis": "the name is unknown.", "fix": "reroll", "prompt": "exactly two monsters.",
    "note": "pinned the count.", "tweak": ""})
check("parser: a QUESTION wins", engines.parse_reviser_reply(R, "QUESTION: What looks wrong?\nFIX: reroll\nPROMPT: x")
      == {"question": "What looks wrong?"})
check("parser: QUESTION NONE is not a question", "fix" in engines.parse_reviser_reply(R, "QUESTION: NONE\n" + FULL))
check("parser: FIX is read without case or trailing words",
      engines.parse_reviser_reply(R, FULL.replace("FIX: reroll", "**FIX:** Edit (a local change)"))["fix"] == "edit")
check("parser: other lines are ignored, the first of a key wins",
      engines.parse_reviser_reply(R, "Sure!\n" + FULL + "\nPROMPT: second")["prompt"] == "exactly two monsters.")
for label, text in (("FIX not edit/reroll", FULL.replace("reroll", "maybe")), ("no FIX", FULL.replace("FIX: reroll\n", "")),
                    ("blank PROMPT", FULL.replace("exactly two monsters.", "")), ("junk", "Sure! Here you go.")):
    try:
        engines.parse_reviser_reply(R, text)
        check("parser: %s raises ValueError" % label, False)
    except ValueError:
        check("parser: %s raises ValueError" % label, True)

# ---------------------------------------------------------------------------
# 2. vision: config first, then the /models probe
# ---------------------------------------------------------------------------
def vision_with(models=None, config=None):
    srv._GUIDE_VISION.update(at=None, value=False)
    HELPER_STATE["models"] = models
    srv.HELPER.pop("vision", None)
    if config is not None:
        srv.HELPER["vision"] = config
    gets = HELPER_STATE["model_gets"]
    v = srv._helper_vision()
    srv.HELPER.pop("vision", None)
    return v, HELPER_STATE["model_gets"] - gets


print("vision: config, then the probe")
check("vision: config false wins over a multimodal probe, no probe made", vision_with(MULTIMODAL, False) == (False, 0))
check("vision: config true wins over an unreachable probe, no probe made", vision_with(None, True) == (True, 0))
check("vision: a multimodal capability means yes", vision_with(MULTIMODAL) == (True, 1))
check("vision: a vision capability means yes",
      vision_with({"data": [{"id": "test-model", "capabilities": ["Vision"]}]})[0] is True)
check("vision: text-only capabilities mean no",
      vision_with({"models": [{"name": "test-model", "capabilities": ["completion"]}]})[0] is False)
check("vision: no capabilities listed means no", vision_with({"data": [{"id": "test-model"}]})[0] is False)
check("vision: an unreachable probe means no", vision_with(None)[0] is False)
check("vision: the configured model's entry wins over another model's",
      vision_with({"data": [{"id": "other", "capabilities": ["multimodal"]},
                            {"id": "test-model", "capabilities": ["completion"]}]})[0] is False)
check("vision: an alias names the configured model",
      vision_with({"data": [{"id": "other", "capabilities": ["completion"]},
                            {"id": "x", "aliases": ["test-model"], "capabilities": ["multimodal"]}]})[0] is True)
vision_with(MULTIMODAL)
gets = HELPER_STATE["model_gets"]
check("vision: the answer is cached", srv._helper_vision() is True and HELPER_STATE["model_gets"] == gets)
check("vision: GET /api/guide reports it", http("/api/guide?room=picture")[1].get("helper_vision") is True)

print("vision: config validation at startup")
for label, helper, words in (("vision not a bool", dict(BRAIN, vision="yes"), '"vision"'),
                             ("max_images 0", dict(BRAIN, max_images=0), '"max_images"'),
                             ("max_images not a number", dict(BRAIN, max_images="2"), '"max_images"')):
    scratch, path, _ = write_config(helper)
    out = subprocess.run([sys.executable, os.path.join(ROOT, "server.py")], capture_output=True, text=True, timeout=30,
                         env=dict(os.environ, GENCENTER_CONFIG=path, GENCENTER_DATA=os.path.join(scratch, "data")))
    check("config: %s refuses to start and names the key" % label,
          out.returncode != 0 and words in (out.stdout + out.stderr), (out.returncode, (out.stdout + out.stderr)[-300:]))

# ---------------------------------------------------------------------------
# 3. grounding, and pictures in POST /api/guide/chat
# ---------------------------------------------------------------------------
print("grounding: the line says what the helper actually got")
check("grounding: none", srv.guide_grounding(0) == "\n\n[No picture is attached to this message.]")
check("grounding: one", srv.guide_grounding(1) == "\n\n[1 picture attached.]")
check("grounding: two", srv.guide_grounding(2) == "\n\n[2 pictures attached.]")
check("grounding: one the helper cannot see",
      srv.guide_grounding(1, False) == "\n\n[The user attached a picture, but this helper cannot see pictures.]")
check("grounding: two the helper cannot see",
      srv.guide_grounding(2, False) == "\n\n[The user attached 2 pictures, but this helper cannot see pictures.]")


def seed_job(jid, outputs, kind="image", mode="t2i", status="done", **extra):
    d = os.path.join(srv.LOCAL_OUTPUTS_DIR, jid)
    os.makedirs(d, exist_ok=True)
    outs = []
    for name, media, data in outputs:
        with open(os.path.join(d, name), "wb") as f:
            f.write(data)
        outs.append({"filename": name, "subfolder": jid, "type": "local", "media": media})
    job = {"id": jid, "lane": "t", "lane_name": "Test lane", "kind": kind, "mode": mode, "status": status,
           "prompt": "a battle between Godzilla and MechaKing Ghidorah", "negative": "", "cfg": 3.0,
           "width": 1328, "height": 1328, "steps": 20, "seed": 7, "outputs": outs}
    job.update(extra)
    with srv.JOBS_LOCK:
        srv.JOBS[jid] = job
    return job


seed_job("pic1", [("render.png", "image", PNG)])
seed_job("snd1", [("song.flac", "audio", b"fLaC")], kind="audio", mode="song")
os.makedirs(os.path.join(srv.UPLOADS_DIR, "cpu"), exist_ok=True)
with open(os.path.join(srv.UPLOADS_DIR, "cpu", "up.png"), "wb") as f:
    f.write(PNG)
with open(os.path.join(srv.UPLOADS_DIR, "cpu", "notes.txt"), "w") as f:
    f.write("hello")
MSGS = [{"role": "user", "content": "first"}, {"role": "assistant", "content": "an answer"},
        {"role": "user", "content": "look at this"}]


def chat(pictures, port=None):
    del HELPER_STATE["requests"][:]
    HELPER_STATE["reply"] = "I see it."
    return http("/api/guide/chat", {"room": "picture", "verbosity": "compact", "messages": MSGS,
                                    "pictures": pictures}, port=port)


print("chat: a picture rides on the last user message only")
vision_with(MULTIMODAL)
code, b = chat([{"job_id": "pic1", "output": 0}])
req = HELPER_STATE["requests"][-1] if HELPER_STATE["requests"] else {"messages": []}
last = req["messages"][-1]["content"] if req["messages"] else None
check("chat: 200 and vision true", code == 200 and b.get("vision") is True, b)
check("chat: the last user message is text + one image_url part", isinstance(last, list) and len(last) == 2
      and last[0]["type"] == "text" and last[1]["type"] == "image_url", last if not isinstance(last, list) else [p["type"] for p in last])
check("chat: the image is the result's bytes as a data URL", isinstance(last, list)
      and last[1]["image_url"]["url"] == "data:image/png;base64," + base64.b64encode(PNG).decode())
check("chat: the text part is grounded as one attached picture",
      isinstance(last, list) and last[0]["text"] == "look at this\n\n[1 picture attached.]", last[0] if isinstance(last, list) else last)
check("chat: earlier user turns stay plain text, no picture",
      len(req["messages"]) == 4 and req["messages"][1]["content"] == "first\n\n[No picture is attached to this message.]")
check("chat: no image_url anywhere else", sum(json.dumps(m).count("image_url") for m in req["messages"][:-1]) == 0)

code, b = chat([{"lane": "cpu", "upload": "up.png"}])
check("chat: an upload is attached too", code == 200 and "image_url" in json.dumps(HELPER_STATE["requests"][-1]["messages"][-1]))
code, b = chat(None)
check("chat: no pictures -> no vision key and plain text",
      code == 200 and "vision" not in b and isinstance(HELPER_STATE["requests"][-1]["messages"][-1]["content"], str))

print("chat: limits and refusals")
code, b = chat([{"job_id": "pic1", "output": 0}, {"job_id": "pic1", "output": 0}])
check("chat: two pictures over the default max_images 1 -> 400", code == 400 and "at most 1 picture" in b.get("error", ""), b)
check("chat: nothing reached the helper", HELPER_STATE["requests"] == [])
srv.HELPER["max_images"] = 2
code, b = chat([{"job_id": "pic1", "output": 0}, {"lane": "cpu", "upload": "up.png"}])
check("chat: max_images 2 takes two", code == 200 and
      HELPER_STATE["requests"][-1]["messages"][-1]["content"][0]["text"].endswith("[2 pictures attached.]"))
srv.HELPER.pop("max_images")
for label, pics, words in (
        ("not a list", {"job_id": "pic1"}, "must be a list"),
        ("a bad shape", [{"job_id": "pic1"}], "Each picture"),
        ("a bool index", [{"job_id": "pic1", "output": True}], "Each picture"),
        ("an unknown job", [{"job_id": "nope", "output": 0}], "cannot find that result"),
        ("an index out of range", [{"job_id": "pic1", "output": 3}], "no picture"),
        ("an audio result", [{"job_id": "snd1", "output": 0}], "Only a picture or a clip"),
        ("a non-image upload", [{"lane": "cpu", "upload": "notes.txt"}], "Only a picture"),
        ("an upload path escape", [{"lane": "cpu", "upload": "../x.png"}], "not allowed")):
    code, b = chat(pics)
    check("chat: %s -> 400" % label, code == 400 and words in b.get("error", ""), (code, b))
real_limit = srv.HELPER_IMAGE_LIMIT
srv.HELPER_IMAGE_LIMIT = 10
code, b = chat([{"job_id": "pic1", "output": 0}])
check("chat: a picture over the limit -> 400", code == 400 and "too large" in b.get("error", ""), b)
srv.HELPER_IMAGE_LIMIT = real_limit

print("chat: a helper that cannot see gets no picture and the truth")
vision_with(None)
code, b = chat([{"job_id": "pic1", "output": 0}])
last = HELPER_STATE["requests"][-1]["messages"][-1]["content"] if HELPER_STATE["requests"] else None
check("chat: vision false in the response", code == 200 and b.get("vision") is False, b)
check("chat: no image part sent", isinstance(last, str) and "image_url" not in json.dumps(HELPER_STATE["requests"][-1]))
check("chat: grounded truthfully", last == "look at this\n\n[The user attached a picture, but this helper cannot see pictures.]", last)
code, b = chat([{"job_id": "pic1", "output": 0}], port=PORT2)
check("chat: pictures with no brain -> 409", code == 409 and b.get("no_brain") is True)

print("chat: a clip becomes its middle still")
FFMPEG = shutil.which("ffmpeg")
if not FFMPEG:
    print("  SKIP  the video-still checks: ffmpeg is not on PATH")
else:
    clip = os.path.join(tempfile.mkdtemp(prefix="bwf_revise_clip_"), "c.mp4")
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=64x48:rate=10",
                    "-t", "2", "-pix_fmt", "yuv420p", clip], check=True, timeout=60)
    with open(clip, "rb") as f:
        seed_job("vid1", [("clip.mp4", "video", f.read())], kind="image", mode="t2i")
    vision_with(MULTIMODAL)
    code, b = chat([{"job_id": "vid1", "output": 0}])
    url = HELPER_STATE["requests"][-1]["messages"][-1]["content"][1]["image_url"]["url"] if code == 200 else ""
    check("chat: the clip is sent as one PNG still", code == 200 and url.startswith("data:image/png;base64,")
          and base64.b64decode(url.split(",", 1)[1])[:8] == b"\x89PNG\r\n\x1a\n", (code, b, url[:40]))
real_ffmpeg = srv.FFMPEG_BIN
srv.FFMPEG_BIN = None
seed_job("vid2", [("clip.mp4", "video", b"not really a clip")])
vision_with(MULTIMODAL)
code, b = chat([{"job_id": "vid2", "output": 0}])
check("chat: a clip with no ffmpeg -> 400 naming ffmpeg", code == 400 and "ffmpeg" in b.get("error", ""), b)
srv.FFMPEG_BIN = real_ffmpeg

# ---------------------------------------------------------------------------
# 4. POST /api/guide/revise
# ---------------------------------------------------------------------------
def revise(body, reply=None, port=None):
    del HELPER_STATE["requests"][:]
    if reply is not None:
        HELPER_STATE["reply"] = reply
    return http("/api/guide/revise", body, port=port)


GOOD = ("QUESTION:\nDIAGNOSIS: The model does not know the second name.\nFIX: reroll\n"
        "PROMPT: A battle between exactly two giant monsters: a grey kaiju and a golden three-headed mechanical dragon.\n"
        "NOTE: Pinned the count to two.\nTWEAK: A night sky would add contrast.")
BASE = {"room": "picture", "job_id": "pic1", "complaint": "I got two Godzillas and some other monster"}

print("revise: the reroll path")
vision_with(MULTIMODAL)
code, b = revise(BASE, GOOD)
req = HELPER_STATE["requests"][-1] if HELPER_STATE["requests"] else {"messages": [{}, {}]}
user = req["messages"][1].get("content")
text = user[0]["text"] if isinstance(user, list) else ""
check("revise: 200 with the parsed reply", code == 200 and b.get("ok") and b.get("fix") == "reroll"
      and b.get("prompt", "").startswith("A battle between exactly two") and b.get("diagnosis") and b.get("note")
      and b.get("tweak") == "A night sky would add contrast.", b)
check("revise: fills and edit_mode come from the pack", b.get("fills") == "prompt" and b.get("edit_mode") == "edit")
check("revise: vision true", b.get("vision") is True)
check("revise: the system prompt is the pack's reviser prompt", req["messages"][0] == {"role": "system", "content": R["prompt"]})
check("revise: one user message with text + the picture", isinstance(user, list) and [p["type"] for p in user] == ["text", "image_url"], user)
check("revise: the job's prompt, verbatim", text.startswith("The prompt that made it: a battle between Godzilla and MechaKing Ghidorah\n"), text)
check("revise: the job's own settings, by label", "The settings it was made with: " in text and 'Things to avoid: ""' in text
      and "Guidance strength: 3" in text and "Width: 1328" in text and "Prompt:" not in text, text)
check("revise: the complaint", "\nThe user says: I got two Godzillas and some other monster" in text)
check("revise: grounded as one attached picture", text.endswith("\n\n[1 picture attached.]"), text[-60:])
check("revise: sent is the system prompt, the text and a picture count",
      b.get("sent") == {"system": R["prompt"], "user_text": text, "pictures": 1}, (b.get("sent") or {}).get("pictures"))
check("revise: sent carries no image bytes", "base64" not in json.dumps(b) and "image_url" not in json.dumps(b))
check("revise: max_tokens 1024", req.get("max_tokens") == 1024)

code, b = revise(dict(BASE, answer="the dragon"), GOOD)
text = HELPER_STATE["requests"][-1]["messages"][1]["content"][0]["text"]
check("revise: an answer is forwarded after the complaint",
      code == 200 and "\nThe user says: I got two Godzillas and some other monster\nThe user answered: the dragon" in text)
code, b = revise(dict(BASE, context={"mode": "t2i", "fields": {"prompt": "now"}}), GOOD)
text = HELPER_STATE["requests"][-1]["messages"][1]["content"][0]["text"]
check("revise: a context line leads, without the mode's label",
      code == 200 and text.startswith('[Current room: Picture · Prompt: "now"]\n\nThe prompt that made it: '), text[:80])

print("revise: the question path")
code, b = revise(BASE, "QUESTION: What looks wrong: the monsters, the city or the light?\nFIX: reroll\nPROMPT: x")
check("revise: a question comes back alone", code == 200 and b.get("question") == "What looks wrong: the monsters, the city or the light?"
      and "fix" not in b and "prompt" not in b and b.get("vision") is True and b.get("sent", {}).get("pictures") == 1, b)

print("revise: the edit path")
code, b = revise(BASE, "DIAGNOSIS: Only the dragon's colour is off.\nFIX: edit\nPROMPT: Make the dragon gold.\nNOTE:\nTWEAK:")
check("revise: FIX edit with the instruction", code == 200 and b.get("fix") == "edit" and b.get("prompt") == "Make the dragon gold."
      and b.get("edit_mode") == "edit" and b.get("note") == "", b)

print("revise: a reply in the wrong shape")
for label, reply in (("junk", "Sure! " + "x" * 3000), ("FIX maybe", GOOD.replace("reroll", "maybe")),
                     ("blank PROMPT", GOOD.replace("PROMPT: A battle", "PROMPT:\nA battle"))):
    code, b = revise(BASE, reply)
    check("revise: %s -> 502 parse failure" % label, code == 502 and b.get("ok") is False
          and b.get("error") == "The writer's answer didn't come back in the expected shape."
          and len(b.get("raw", "")) <= 2000 and b.get("sent", {}).get("pictures") == 1, b.get("error"))

print("revise: a helper that cannot see")
vision_with(None)
code, b = revise(BASE, GOOD)
user = HELPER_STATE["requests"][-1]["messages"][1]["content"] if HELPER_STATE["requests"] else None
check("revise: no picture sent, vision false", code == 200 and b.get("vision") is False and isinstance(user, str)
      and "image_url" not in json.dumps(HELPER_STATE["requests"][-1]), b)
check("revise: grounded truthfully",
      isinstance(user, str) and user.endswith("\n\n[The user attached a picture, but this helper cannot see pictures.]"))
check("revise: sent counts no pictures", b.get("sent", {}).get("pictures") == 0 and b["sent"]["user_text"] == user)

print("revise: refusals")
vision_with(MULTIMODAL)
seed_job("edit1", [("e.png", "image", PNG)], mode="edit")
seed_job("run1", [], status="running")
seed_job("noimg", [("m.glb", "model", b"glTF")])
refusals = [
    ("not an object", ["x"], 400, "JSON object"),
    ("unknown room", dict(BASE, room="nowhere"), 404, "no room called"),
    ("unknown job", dict(BASE, job_id="nope"), 404, "cannot find that result"),
    ("job id not text", dict(BASE, job_id=5), 404, "cannot find that result"),
    ("a result from another room", dict(BASE, job_id="snd1"), 400, "another room"),
    ("a mode with no reviser", dict(BASE, job_id="edit1"), 404, "This kind of result has no fixer yet."),
    ("not finished", dict(BASE, job_id="run1"), 400, "no finished picture"),
    ("no picture or clip output", dict(BASE, job_id="noimg"), 400, "no finished picture"),
    ("output out of range", dict(BASE, output=4), 400, "no output number 4"),
    ("output a bool", dict(BASE, output=True), 400, "no output number"),
    ("no complaint", dict(BASE, complaint="  "), 400, "Say what is wrong"),
    ("complaint too long", dict(BASE, complaint="x" * 4001), 400, "too long"),
    ("answer too long", dict(BASE, answer="x" * 501), 400, "at most 500"),
    ("answer not text", dict(BASE, answer=5), 400, "at most 500"),
    ("bad context", dict(BASE, context={"mode": "t2i", "fields": {"nope": 1}}), 400, "not a field"),
]
for label, body, expected, words in refusals:
    code, b = revise(body, GOOD)
    check("revise: %s -> %d" % (label, expected), code == expected and b.get("ok") is False and words in b.get("error", ""), (code, b))
    check("revise: %s never reached the helper" % label, HELPER_STATE["requests"] == [])
with srv2.JOBS_LOCK:
    srv2.JOBS["pic1"] = dict(srv.JOBS["pic1"])
code, b = revise(BASE, port=PORT2)
check("revise: no brain -> 409 no_brain", code == 409 and b.get("no_brain") is True and "config.json" in b.get("error", ""), b)
HELPER_STATE["status"] = 500
code, b = revise(BASE, GOOD)
check("revise: a helper that fails -> 503", code == 503 and b.get("error") == "The helper is busy or switched off right now.", b)
HELPER_STATE["status"] = 200

print("ratchet: server.py and index.html name no model")
out = subprocess.run(["bash", "scripts/check-engine-independence.sh"], cwd=ROOT, capture_output=True, text=True)
check("ratchet: check-engine-independence passes", out.returncode == 0, out.stdout + out.stderr)
if FAILED:
    print("FAILED: %d checks: %s" % (len(FAILED), ", ".join(FAILED)))
    sys.exit(1)
print("OK: all checks passed")
