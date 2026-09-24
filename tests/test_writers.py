"""Gate for guide skills (P2): the pack "writers" contract, the line-delimited
writer parser, the song check, POST /api/guide/skill (against a fake
OpenAI-compatible helper that records every request), the Make-time guard in
generate() (and its pass-through from a sequence slot), and the fixed "Try
this" song example. No browser.

Run: python3 tests/test_writers.py
"""
import importlib.util
import json
import os
import socket
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


# ---------------------------------------------------------------------------
# Fake OpenAI-compatible helper: POST /v1/chat/completions, GET /v1/models
# ---------------------------------------------------------------------------
HELPER_STATE = {"reply": "Start with the ending.", "finish": "stop", "status": 200,
                "models": None, "props": None, "requests": [], "model_gets": 0, "props_gets": 0}


class FakeHelperHandler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        resp = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def do_GET(self):
        if self.path.rstrip("/") == "/props":
            HELPER_STATE["props_gets"] += 1
            if HELPER_STATE["props"] is None:
                return self._send(404, {"error": "no"})
            return self._send(200, HELPER_STATE["props"])
        if self.path.rstrip("/").endswith("/models"):
            HELPER_STATE["model_gets"] += 1
            if HELPER_STATE["models"] is None:
                return self._send(404, {"error": "no"})
            return self._send(200, HELPER_STATE["models"])
        self._send(404, {"error": "no"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        HELPER_STATE["requests"].append(body)
        if HELPER_STATE["status"] != 200:
            return self._send(HELPER_STATE["status"], {"error": "boom"})
        reply = HELPER_STATE["replies"].pop(0) if HELPER_STATE.get("replies") else HELPER_STATE["reply"]
        self._send(200, {"choices": [{"message": {"content": reply},
                                      "finish_reason": HELPER_STATE["finish"]}]})

    def log_message(self, *a):
        pass


fake_helper = ThreadingHTTPServer(("127.0.0.1", 0), FakeHelperHandler)
HELPER_PORT = fake_helper.server_address[1]
threading.Thread(target=fake_helper.serve_forever, daemon=True).start()


def start_server(name, with_helper):
    scratch = tempfile.mkdtemp(prefix="bwf_writers_")
    cfg_path = os.path.join(scratch, "config.json")
    port = free_port()
    cfg = {"port": port, "bind": "127.0.0.1", "title": "guides",
           "timing": {"poll_seconds": 30, "job_poll_seconds": 30},
           "lanes": [{"id": "t", "name": "Test lane", "host": "127.0.0.1", "port": 1, "caps": ["image", "audio"]}]}
    if with_helper:
        cfg["helper"] = {"url": "http://127.0.0.1:%d/v1" % HELPER_PORT, "model": "test-model", "timeout_s": 2}
    with open(cfg_path, "w") as f:
        json.dump(cfg, f)
    os.environ["GENCENTER_CONFIG"] = cfg_path
    os.environ["GENCENTER_DATA"] = os.path.join(scratch, "data")
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), mod.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return mod, port


srv, PORT = start_server("srv_writers", True)
srv2, PORT2 = start_server("srv_writers_nobrain", False)

def http(path, body=None, port=None, ctype="application/json"):
    """(status, parsed-json) for a request to the in-process API."""
    data = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None)
    r = urllib.request.Request("http://127.0.0.1:%d%s" % (port or PORT, path), data=data,
                               method="POST" if data is not None else "GET",
                               headers={"Content-Type": ctype} if data else {})
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw


import engines  # noqa: E402  (ROOT is on sys.path above)
from engines import audio  # noqa: E402
W = engines.writer("audio", "song")

# ---------------------------------------------------------------------------
# 1. the pack contract, the parser, the song check, the Try this example
# ---------------------------------------------------------------------------
print("contract: the pack's writers key")
check("contract: song writer has label, prompt, multiline, none_token, check", W is not None and W["label"] == "Song writer" and W["multiline"] == "LYRICS" and W["none_token"] == "NONE" and callable(W["check"]) and W["prompt"].strip() != "")
check("contract: keys map LYRICS and TAGS to their fields", W["keys"]["LYRICS"] == "lyrics" and W["keys"]["TAGS"] == "tags")
check("contract: modes without a writer return None", engines.writer("audio", "sfx") is None and engines.writer("image", "t2i") is None)

code, body = http("/api/engines?lane=t")
check("contract: /api/engines answers 200", code == 200)
song = next(m for m in body["audio"]["modes"] if m["id"] == "song")
check("contract: /api/engines reports the song writer's label", song["writer"] == {"label": "Song writer"})
others = [(cap, m["id"]) for cap, v in body.items() if cap not in ("rooms", "helper") for m in v["modes"] if not (cap == "audio" and m["id"] == "song") and m.get("writer") is not None]
check("contract: every other mode reports writer null", others == [], others)
check("contract: every mode carries a writer key", all("writer" in m for cap, v in body.items() if cap not in ("rooms", "helper") for m in v["modes"]))

print("parser: line keys, LYRICS to the end, NONE")
REPLY = "TAGS: warm pop, clear female vocals\nBPM: 96\nKEY: NONE\nDURATION: 150\nTIMESIG: 4\nLANGUAGE: en\nLYRICS:\n[Verse]\nline one\n\n[Chorus]\nline two\nTAGS: not a key here"
r = engines.parse_writer_reply(W, REPLY)
check("parser: TAGS", r["values"]["tags"] == "warm pop, clear female vocals")
check("parser: BPM, DURATION, TIMESIG, LANGUAGE", r["values"]["bpm"] == "96" and r["values"]["duration"] == "150" and r["values"]["timesignature"] == "4" and r["values"]["language"] == "en")
check("parser: NONE on a plain key leaves the field out", "keyscale" not in r["values"])
check("parser: LYRICS runs to the end, key-like lines included", r["values"]["lyrics"] == "[Verse]\nline one\n\n[Chorus]\nline two\nTAGS: not a key here")

r = engines.parse_writer_reply(W, "TAGS: x\nLYRICS:\nNONE")
check("parser: LYRICS NONE means empty", r["values"]["lyrics"] == "")

r = engines.parse_writer_reply(W, "TAGS: x\nQUESTION: Sung, or instrumental?\nLYRICS:\n[Verse]\nhi")
check("parser: a QUESTION line wins", r == {"question": "Sung, or instrumental?", "options": []})

r = engines.parse_writer_reply(W, "QUESTION: NONE\nTAGS: x\nLYRICS: NONE")
check("parser: QUESTION NONE is not a question", "values" in r)

try:
    engines.parse_writer_reply(W, "Sure! Here is a lovely song for you.")
    check("parser: junk raises ValueError", False)
except ValueError:
    check("parser: junk raises ValueError", True)

print("check: the song check")
VOICE_LESS = {"tags": "warm acoustic pop, gentle drums", "lyrics": "[Verse]\na\n[Chorus]\nb\n[Verse]\nc\n[Chorus]\nd", "duration": 150.0}
p = audio.song_check(VOICE_LESS, {})
check("check: lyrics with no voice in the tags is a problem", len(p) == 1 and "voice" in p[0])

p = audio.song_check({"tags": "warm pop", "lyrics": "", "duration": 150.0}, {})
check("check: an instrumental needs no voice", p == [])

p = audio.song_check({"tags": "pop, clear female vocals", "lyrics": "[Verse]\na\n[Chorus]\nb", "duration": 150.0}, {})
check("check: too few sections for 150 s is a problem naming both numbers", len(p) == 1 and "150 seconds" in p[0] and "at least 4" in p[0] and "these have 2" in p[0])

p = audio.song_check({"tags": "pop, clear female vocals", "lyrics": "[Intro]\na\n[Verse]\nb\n[Chorus]\nc\n[Verse]\nd\n[Chorus]\ne\n[Outro]\nf", "duration": 150.0}, {})
check("check: enough sections with a voice is fine", p == [])

check("check: a tag with no words under it does not count", audio.song_sections_with_words("[Verse]\n\n[Chorus]\nwords") == 1)

p = audio.song_check({"tags": "rap, male rapper", "lyrics": "[Verse]\na\n[Verse]\nb\n[Chorus]\nc\n[Bridge]\nd", "duration": 200.0}, {})
check("check: 200 s needs 5 sections; rapper counts as a voice", len(p) == 1 and "at least 5" in p[0])

print("example: the fixed Try this")
ex = engines.examples("audio", "song")[0]
vals = dict(ex["values"])
vals.setdefault("duration", 150.0)
check("example: the Try this song passes the check", audio.song_check(vals, {}) == [], audio.song_check(vals, {}))
check("example: the Try this tags name a voice", audio.song_voice_named(ex["values"]["tags"]))

# ---------------------------------------------------------------------------
# 2. POST /api/guide/skill
# ---------------------------------------------------------------------------
def skill(body, port=None):
    return http("/api/guide/skill", body, port=port)


CTX = {"mode": "song", "fields": {"duration": 150}}
GOOD = ("TAGS: warm pop, clear female vocals\nBPM: NONE\nKEY: NONE\nDURATION: 150\nTIMESIG: NONE\nLANGUAGE: NONE\n"
        "LYRICS:\n[Intro]\nhello\n\n[Verse]\na\n\n[Chorus]\nb\n\n[Verse]\nc\n\n[Chorus]\nd\n\n[Outro]\ne")
BAD = "TAGS: warm pop\nDURATION: 150\nLYRICS:\n[Verse]\na\n\n[Chorus]\nb"

print("skill: the question path")
del HELPER_STATE["requests"][:]
HELPER_STATE["replies"] = ["QUESTION: Sung, or instrumental?"]
code, b = skill({"room": "music", "mode": "song", "topic": "music for my video about the sea", "context": CTX})
check("skill: the question path", code == 200 and b.get("ok") is True and b.get("question") == "Sung, or instrumental?", b)
check("skill: the question path #2", "fields" not in b)
check("skill: the question path #3", len(HELPER_STATE["requests"]) == 1)

req = HELPER_STATE["requests"][-1]
check("skill: the request the fake got", req["messages"][0] == {"role": "system", "content": W["prompt"]})
check("skill: the request the fake got #2", req["max_tokens"] == 1024)
user = req["messages"][1]["content"]
check("skill: the request the fake got #3", user.startswith("[Current room: Music · Duration (seconds): 150]"), user)
check("skill: the request the fake got #4", "mode:" not in user.split("\n")[0])
check("skill: the request the fake got #5", "Request: music for my video about the sea" in user)
check("skill: the request the fake got #6", user.endswith("[No picture is attached to this message.]"))

print("skill: an answer is forwarded")
HELPER_STATE["replies"] = [GOOD]
code, b = skill({"room": "music", "mode": "song", "topic": "music for my video about the sea", "answer": "Sung", "context": CTX})
check("skill: an answer is forwarded", code == 200 and b["fields"]["tags"] == "warm pop, clear female vocals")
check("skill: an answer is forwarded #2", "The user answered: Sung" in HELPER_STATE["requests"][-1]["messages"][1]["content"])

print("skill: the fields path")
del HELPER_STATE["requests"][:]
HELPER_STATE["replies"] = [GOOD]
code, b = skill({"room": "music", "mode": "song", "topic": "a song about the sea", "context": CTX})
check("skill: the fields path", code == 200 and b["ok"] and b["problems"] == [] and b["retried"] is False, b)
check("skill: the fields path #2", b["fields"]["duration"] == 150.0 and "bpm" not in b["fields"] and "keyscale" not in b["fields"])
check("skill: the fields path #3", b["fields"]["lyrics"].startswith("[Intro]\nhello"))
check("skill: the fields path #4", len(HELPER_STATE["requests"]) == 1)
check("skill: the fields path #5", b["sent"] == {"system": HELPER_STATE["requests"][-1]["messages"][0]["content"], "user": HELPER_STATE["requests"][-1]["messages"][1]["content"]})

print("skill: one retry on problems")
del HELPER_STATE["requests"][:]
HELPER_STATE["replies"] = [BAD, GOOD]
body_5 = {"room": "music", "mode": "song", "topic": "a song about the sea", "context": CTX}
code, b = skill(body_5)
check("skill: one retry on problems", code == 200 and b["retried"] is True and b["problems"] == [] and b["fields"]["tags"] == "warm pop, clear female vocals")
check("skill: one retry on problems #2", len(HELPER_STATE["requests"]) == 2)
retry_user = HELPER_STATE["requests"][1]["messages"][1]["content"]
check("skill: one retry on problems #3", retry_user.startswith(HELPER_STATE["requests"][0]["messages"][1]["content"]) and "Your draft had these problems:" in retry_user and "voice" in retry_user and retry_user.endswith("Rewrite it."))
check("skill: one retry on problems #4", b["sent"]["user"] == retry_user)

print("skill: problems after a failed retry are returned, not hidden")
del HELPER_STATE["requests"][:]
HELPER_STATE["replies"] = [BAD, BAD]
code, b = skill(body_5)
check("skill: problems after a failed retry are returned, not hidden", code == 200 and b["retried"] is True and len(b["problems"]) == 2 and b["fields"]["lyrics"] == "[Verse]\na\n\n[Chorus]\nb")
check("skill: problems after a failed retry are returned, not hidden #2", len(HELPER_STATE["requests"]) == 2)

print("skill: an out-of-range value is named, never clamped")
HELPER_STATE["replies"] = [GOOD.replace("BPM: NONE", "BPM: 300"), GOOD.replace("BPM: NONE", "BPM: 300")]
code, b = skill(body_5)
check("skill: an out-of-range value is named, never clamped", "bpm" not in b["fields"] and any("BPM" in p and "300" in p for p in b["problems"]), b)

HELPER_STATE["replies"] = [GOOD.replace("KEY: NONE", "KEY: e MINOR")]
code, b = skill(body_5)
check("skill: a key is matched to the engine's own spelling", b["fields"].get("keyscale") == "E minor", b)

print("skill: a reply in the wrong shape")
HELPER_STATE["replies"] = ["Sure! " + "x" * 3000]
code, b = skill(body_5)
check("skill: a reply in the wrong shape", code == 502 and b["ok"] is False and b["error"] == "The writer's answer didn't come back in the expected shape." and len(b["raw"]) == 2000)

print("skill: refusals")
refusals = [
    ("no topic", {"room": "music", "mode": "song", "topic": "  "}, 400),
    ("topic too long", {"room": "music", "mode": "song", "topic": "x" * 4001}, 400),
    ("answer too long", {"room": "music", "mode": "song", "topic": "t", "answer": "x" * 501}, 400),
    ("answer not text", {"room": "music", "mode": "song", "topic": "t", "answer": 5}, 400),
    ("mode not in room", {"room": "music", "mode": "sfx", "topic": "t"}, 400),
    ("context mode differs", {"room": "music", "mode": "song", "topic": "t", "context": {"mode": "yue2", "fields": {}}}, 400),
    ("bad context field", {"room": "music", "mode": "song", "topic": "t", "context": {"mode": "song", "fields": {"nope": 1}}}, 400),
    ("bad recipe", {"room": "music", "mode": "song", "topic": "t", "context": {"mode": "song", "fields": {}, "recipe": "nope"}}, 400),
    ("unknown room", {"room": "nowhere", "mode": "song", "topic": "t"}, 404),
    ("no writer for this mode", {"room": "cleanup", "mode": "cutout", "topic": "t"}, 404),
    ("not an object", ["x"], 400)
]
for label, body, expected_code in refusals:
    code, b = skill(body)
    check(f"skill: refusals ({label})", code == expected_code and b.get("ok") is False and b.get("error"))
    if label == "no writer for this mode":
        check(f"skill: refusals ({label}) #2", b.get("error") == "This mode has no writer yet.")

print("skill: no brain")
code, b = skill({"room": "music", "mode": "song", "topic": "a song"}, port=PORT2)
check("skill: no brain", code == 409 and b.get("no_brain") is True)

HELPER_STATE["replies"] = []

# ---------------------------------------------------------------------------
# 3. the Make-time guard, from /api/generate and from a sequence slot
# ---------------------------------------------------------------------------
import subprocess  # noqa: E402
import time  # noqa: E402

ACE = {"ace_unet": "ace.safetensors", "ace_clip1": "a.safetensors", "ace_clip2": "b.safetensors", "ace_vae": "v.safetensors", "sao_ckpt": "sao.safetensors", "sao_clip": "t5.safetensors"}
srv.LANE_BY_ID["t"]["models"] = dict(ACE)
with srv.STATE_LOCK:
    srv.LANE_STATE["t"] = {"up": True, "checked": time.time(), "err": ""}
DISPATCHED = []

def fake_dispatch(lane, graph, kind, mode, meta):
    DISPATCHED.append((kind, mode, graph))
    return {"ok": True, "job": {"id": "job%d" % len(DISPATCHED), "status": "queued"}}

srv.dispatch = fake_dispatch
NO_VOICE = {"lane": "t", "kind": "audio", "mode": "song", "tags": "warm acoustic pop, gentle drums", "lyrics": "[Verse]\nSunlight on the water\n[Chorus]\nHold on", "duration": 150}

print("guard: Make-time check in generate()")
body, code = srv.generate(dict(NO_VOICE))
check("guard: lyrics with no voice -> 409 needs_confirm", code == 409 and body.get("needs_confirm") is True and body.get("ok") is False, body)
check("guard: both problems listed, the voice one included", len(body.get("problems") or []) == 2 and any("voice" in p for p in body["problems"]), "")
check("guard: an error sentence for older clients", body.get("error", "").startswith("Check this before it renders:"), "")
check("guard: nothing was sent to the lane", DISPATCHED == [], "")

body, code = srv.generate(dict(NO_VOICE, confirm=True))
check("guard: confirm true renders it", code == 200 and body.get("ok") is True and len(DISPATCHED) == 1, body)

body, code = srv.generate(dict(NO_VOICE, confirm="yes"))
check("guard: only JSON true confirms", code == 409, "")

body, code = srv.generate({"lane": "t", "kind": "audio", "mode": "song", "tags": "warm acoustic pop", "lyrics": "", "duration": 150})
check("guard: an instrumental needs no confirm", code == 200 and body.get("ok"), body)

ex = srv.engines.examples("audio", "song")[0]
body, code = srv.generate(dict(ex["values"], lane="t", kind="audio", mode="song", quality="standard"))
check("guard: the fixed Try this needs no confirm", code == 200 and body.get("ok"), body)

body, code = srv.generate({"lane": "t", "kind": "audio", "mode": "song", "tags": "pop, clear female vocals", "lyrics": "[Verse]\na\n[Chorus]\nb", "quality": "long"})
check("guard: the quality tier's duration is what the check sees", code == 409 and any("240 seconds" in p for p in body.get("problems", [])), body)

n = len(DISPATCHED)
body, code = srv.generate({"lane": "t", "kind": "audio", "mode": "sfx", "prompt": "a door creaking", "seconds": 2})
check("guard: other modes (sfx) are untouched", code == 200 and body.get("ok") and len(DISPATCHED) == n + 1, body)
check("guard: a mode with no writer has no Make-time problems", srv._make_time_problems({"prompt": "x"}, srv.LANE_BY_ID["t"], srv.abilities(srv.LANE_BY_ID["t"]), "image", "t2i") == [], "")

body, code = srv.generate(dict(NO_VOICE, timesignature="zzz"))
check("guard: a value that would be refused anyway is refused, not confirmed", code == 400
      and body.get("needs_confirm") is None, body)

srv.LANE_BY_ID["t"]["models"] = {}
body, code = srv.generate(dict(NO_VOICE))
check("guard: a mode the lane cannot run keeps its own refusal", body.get("needs_confirm") is None, body)
srv.LANE_BY_ID["t"]["models"] = dict(ACE)

print("guard: a sequence slot passes confirm through")
SEEN = []
real_generate = srv.generate
def spy(p):
    SEEN.append(dict(p))
    return real_generate(p)
srv.generate = spy

seq, code = srv.seq_create({"title": "Song slot", "mode": "sequence"})
sid = seq["id"]
b, c = srv.seq_op({"id": sid, "rev": seq["rev"], "op": "add_slot", "lane": "sound", "cap": "audio", "mode": "song", "values": {"tags": NO_VOICE["tags"], "lyrics": NO_VOICE["lyrics"], "duration": 150}})
check("guard: a song slot is added", c == 200, b)
slot_id = b["slots"][0]["id"]
r, c = srv.seq_generate({"id": sid, "slot_id": slot_id})
check("guard: a slot with problems -> 409 needs_confirm", c == 409 and r.get("needs_confirm") is True, r)
check("guard: no confirm is added on the slot's behalf", "confirm" not in SEEN[-1], SEEN[-1:])
r, c = srv.seq_generate({"id": sid, "slot_id": slot_id, "confirm": True})
check("guard: confirm on the slot renders it", c == 200 and r.get("ok"), r)
check("guard: the slot's confirm reaches generate()", SEEN[-1].get("confirm") is True, "")
srv.generate = real_generate

print("ratchet: server.py and index.html name no model")
out = subprocess.run(["bash", "scripts/check-engine-independence.sh"], cwd=ROOT, capture_output=True, text=True)
check("ratchet: check-engine-independence passes", out.returncode == 0, out.stdout + out.stderr)
if FAILED:
    print("FAILED: %d checks: %s" % (len(FAILED), ", ".join(FAILED)))
    sys.exit(1)
print("OK: all checks passed")
