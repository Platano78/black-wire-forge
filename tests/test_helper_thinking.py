"""Gate for a thinking-model helper: a model that spends its whole reply budget
thinking comes back with finish_reason "length", empty content and the thought
in reasoning_content. Every guide call (skill, revise, chat) retries that ONCE
with four times the budget (capped at 16384), and when it is still empty says
so in one plain sentence -- never a shape error, never an empty bubble.
config's helper.max_tokens raises every guide call's budget. Runs against a
fake OpenAI-compatible helper that records every request. No browser.

Run: python3 tests/test_helper_thinking.py
"""
import importlib.util
import json
import os
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


# ---------------------------------------------------------------------------
# Fake helper: each POST pops the next scripted reply (content, finish,
# reasoning_content); the last one repeats once the script runs out.
# ---------------------------------------------------------------------------
THOUGHT = {"content": "", "finish": "length", "reasoning": "Let me think about the song. " * 40}
HELPER_STATE = {"script": [], "requests": []}


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
        script = HELPER_STATE["script"]
        r = script.pop(0) if len(script) > 1 else script[0]
        msg = {"role": "assistant", "content": r["content"]}
        if r.get("reasoning"):
            msg["reasoning_content"] = r["reasoning"]
        self._send(200, {"choices": [{"message": msg, "finish_reason": r["finish"]}]})

    def log_message(self, *a):
        pass


fake_helper = ThreadingHTTPServer(("127.0.0.1", 0), FakeHelperHandler)
HELPER_PORT = fake_helper.server_address[1]
threading.Thread(target=fake_helper.serve_forever, daemon=True).start()
BRAIN = {"url": "http://127.0.0.1:%d/v1" % HELPER_PORT, "model": "test-model", "timeout_s": 5}


def write_config(helper, scratch):
    cfg = {"port": free_port(), "bind": "127.0.0.1", "title": "thinking",
           "timing": {"poll_seconds": 30, "job_poll_seconds": 30},
           "lanes": [{"id": "t", "name": "Test lane", "host": "127.0.0.1", "port": 1,
                      "caps": ["image", "audio"]}],
           "helper": helper}
    path = os.path.join(scratch, "config.json")
    with open(path, "w") as f:
        json.dump(cfg, f)
    return path, cfg["port"]


scratch = tempfile.mkdtemp(prefix="bwf_thinking_")
cfg_path, PORT = write_config(dict(BRAIN), scratch)
os.environ["GENCENTER_CONFIG"] = cfg_path
os.environ["GENCENTER_DATA"] = os.path.join(scratch, "data")
spec = importlib.util.spec_from_file_location("srv_thinking", os.path.join(ROOT, "server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)
httpd = ThreadingHTTPServer(("127.0.0.1", PORT), srv.Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()


def http(path, body):
    r = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path), data=json.dumps(body).encode(),
                               method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def run(path, body, script):
    del HELPER_STATE["requests"][:]
    HELPER_STATE["script"] = list(script)
    code, b = http(path, body)
    return code, b, [r["max_tokens"] for r in HELPER_STATE["requests"]]


SENTENCE = ("Your helper spent its whole answer thinking and wrote nothing. Give it more room with "
            "\"helper\": {\"max_tokens\": 8192} in config.json, or use a model that doesn't think first.")
SONG = {"room": "music", "mode": "song", "topic": "a song about the sea",
        "context": {"mode": "song", "fields": {"duration": 150}}}
GOOD_SONG = ("TAGS: warm pop, clear female vocals\nBPM: NONE\nKEY: NONE\nDURATION: 150\nTIMESIG: NONE\n"
             "LANGUAGE: NONE\nLYRICS:\n[Intro]\nhello\n\n[Verse]\na\n\n[Chorus]\nb\n\n[Verse]\nc\n\n"
             "[Chorus]\nd\n\n[Outro]\ne")

# ---------------------------------------------------------------------------
# 1. guide skill
# ---------------------------------------------------------------------------
print("skill: thought the whole budget away, then wrote on the retry")
code, b, budgets = run("/api/guide/skill", SONG, [THOUGHT, {"content": GOOD_SONG, "finish": "stop"}])
check("skill: 200 with the fields from the retry", code == 200 and b.get("ok") is True
      and b.get("fields", {}).get("tags") == "warm pop, clear female vocals", (code, b))
check("skill: the retry asked for four times the budget", budgets == [1024, 4096], budgets)

print("skill: thought the whole budget away twice")
code, b, budgets = run("/api/guide/skill", SONG, [THOUGHT])
check("skill: 502 with the plain sentence", code == 502 and b.get("ok") is False and b.get("error") == SENTENCE,
      (code, b))
check("skill: exactly one retry", budgets == [1024, 4096], budgets)

print("skill: an unclosed <think> cut off by the length limit counts as no answer")
code, b, budgets = run("/api/guide/skill", SONG, [{"content": "<think>The user wants a song", "finish": "length"},
                                                  {"content": GOOD_SONG, "finish": "stop"}])
check("skill: retried, and the retry's fields come back", code == 200 and budgets == [1024, 4096]
      and b.get("fields", {}).get("tags") == "warm pop, clear female vocals", (code, b, budgets))

print("skill: an empty answer that was NOT cut short is not retried")
code, b, budgets = run("/api/guide/skill", SONG, [{"content": "", "finish": "stop"}])
check("skill: one call, the shape error as before", code == 502 and budgets == [1024]
      and b.get("error") == srv.GUIDE_SKILL_SHAPE_ERROR, (code, b, budgets))

print("skill: helper.max_tokens raises the budget, and the retry is capped at 16384")
srv.HELPER["max_tokens"] = 8192
code, b, budgets = run("/api/guide/skill", SONG, [THOUGHT, {"content": GOOD_SONG, "finish": "stop"}])
check("skill: first call at helper.max_tokens, retry capped", code == 200 and budgets == [8192, 16384],
      (code, budgets))
srv.HELPER["max_tokens"] = 512
code, b, budgets = run("/api/guide/skill", SONG, [{"content": GOOD_SONG, "finish": "stop"}])
check("skill: a helper.max_tokens below the writer's own budget never lowers it", budgets == [1024], budgets)
srv.HELPER["max_tokens"] = 16384
code, b, budgets = run("/api/guide/skill", SONG, [THOUGHT])
check("skill: at the cap already, no pointless retry -- the sentence", code == 502 and budgets == [16384]
      and b.get("error") == SENTENCE, (code, b, budgets))
del srv.HELPER["max_tokens"]

# ---------------------------------------------------------------------------
# 2. guide chat
# ---------------------------------------------------------------------------
CHAT = {"room": "music", "verbosity": "compact", "messages": [{"role": "user", "content": "how long should a song be?"}]}
chat_budget = srv._room_guide("music")[1]["projections"]["compact"]["max_tokens"]

print("chat: thought the whole budget away, then answered on the retry")
code, b, budgets = run("/api/guide/chat", CHAT, [THOUGHT, {"content": "About three minutes.", "finish": "stop"}])
check("chat: 200 with the retry's words", code == 200 and b.get("text") == "About three minutes."
      and b.get("truncated") is False, (code, b))
check("chat: the retry asked for four times the budget", budgets == [chat_budget, chat_budget * 4], budgets)

print("chat: thought the whole budget away twice -- never an empty bubble")
code, b, budgets = run("/api/guide/chat", CHAT, [THOUGHT])
check("chat: 502 with the plain sentence, no text", code == 502 and b.get("error") == SENTENCE
      and not b.get("text"), (code, b))
check("chat: exactly one retry", budgets == [chat_budget, chat_budget * 4], budgets)

srv.HELPER["max_tokens"] = 8192
code, b, budgets = run("/api/guide/chat", CHAT, [{"content": "About three minutes.", "finish": "stop"}])
check("chat: helper.max_tokens raises the budget", code == 200 and budgets == [8192], budgets)
del srv.HELPER["max_tokens"]

# ---------------------------------------------------------------------------
# 3. guide revise
# ---------------------------------------------------------------------------
PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                    "1f15c4890000000d49444154789c6300010000050001" "0d0a2db40000000049454e44ae426082")
jd = os.path.join(srv.LOCAL_OUTPUTS_DIR, "pic1")
os.makedirs(jd, exist_ok=True)
with open(os.path.join(jd, "render.png"), "wb") as f:
    f.write(PNG)
with srv.JOBS_LOCK:
    srv.JOBS["pic1"] = {"id": "pic1", "lane": "t", "lane_name": "Test lane", "kind": "image", "mode": "t2i",
                        "status": "done", "prompt": "two monsters", "negative": "", "cfg": 3.0, "width": 1328,
                        "height": 1328, "steps": 20, "seed": 7,
                        "outputs": [{"filename": "render.png", "subfolder": "pic1", "type": "local",
                                     "media": "image"}]}
REVISE = {"room": "picture", "job_id": "pic1", "complaint": "I got three monsters"}
GOOD_REVISE = ("QUESTION:\nDIAGNOSIS: The count is not pinned.\nFIX: reroll\n"
               "PROMPT: Exactly two giant monsters facing each other.\nNOTE: Pinned the count.\nTWEAK:")

print("revise: thought the whole budget away, then answered on the retry")
code, b, budgets = run("/api/guide/revise", REVISE, [THOUGHT, {"content": GOOD_REVISE, "finish": "stop"}])
check("revise: 200 with the retry's fix", code == 200 and b.get("fix") == "reroll", (code, b))
check("revise: the retry asked for four times the budget", budgets == [1024, 4096], budgets)

print("revise: thought the whole budget away twice")
code, b, budgets = run("/api/guide/revise", REVISE, [THOUGHT])
check("revise: 502 with the plain sentence", code == 502 and b.get("error") == SENTENCE, (code, b))

# ---------------------------------------------------------------------------
# 3b. a write or a fix CUT OFF by the length limit (words, but not all of them)
# ---------------------------------------------------------------------------
CUT = ("The helper's answer was cut off before it finished. Give it more room with "
       "\"helper\": {\"max_tokens\": 8192} in config.json.")
print("skill: cut off mid-write, then finished on the retry")
code, b, budgets = run("/api/guide/skill", SONG, [{"content": GOOD_SONG[:120], "finish": "length"},
                                                  {"content": GOOD_SONG, "finish": "stop"}])
check("skill: the retry's whole write, no cut-off problem", code == 200 and budgets == [1024, 4096]
      and CUT not in b.get("problems", []) and b.get("fields", {}).get("tags") == "warm pop, clear female vocals",
      (code, budgets, b.get("problems")))

print("skill: still cut off after the retry -> the fields WITH a problem, never clean")
code, b, budgets = run("/api/guide/skill", SONG, [{"content": GOOD_SONG, "finish": "length"}])
check("skill: fields and the cut-off problem", code == 200 and budgets == [1024, 4096] and b.get("fields")
      and CUT in b.get("problems", []), (code, budgets, b.get("problems")))
code, b, budgets = run("/api/guide/skill", SONG, [{"content": "Sure! Here is a lovely song ab", "finish": "length"}])
check("skill: cut off before any field -> 502 with the cut-off sentence", code == 502 and b.get("error") == CUT,
      (code, b.get("error")))

print("revise: cut off, then finished on the retry; still cut -> a problem")
code, b, budgets = run("/api/guide/revise", REVISE, [{"content": GOOD_REVISE[:70], "finish": "length"},
                                                     {"content": GOOD_REVISE, "finish": "stop"}])
check("revise: the retry's fix, no cut-off problem", code == 200 and budgets == [1024, 4096]
      and b.get("fix") == "reroll" and CUT not in (b.get("problems") or []), (code, budgets, b))
code, b, budgets = run("/api/guide/revise", REVISE, [{"content": GOOD_REVISE, "finish": "length"}])
check("revise: still cut -> the fix WITH the cut-off problem", code == 200 and budgets == [1024, 4096]
      and b.get("fix") == "reroll" and CUT in (b.get("problems") or []), (code, budgets, b.get("problems")))

print("chat: a reply cut off with words is flagged truncated, not retried (unchanged)")
code, b, budgets = run("/api/guide/chat", CHAT, [{"content": "About three", "finish": "length"}])
check("chat: one call, truncated", code == 200 and budgets == [chat_budget] and b.get("truncated") is True, (code, budgets, b))

# ---------------------------------------------------------------------------
# 4. startup: helper.max_tokens must be a positive whole number
# ---------------------------------------------------------------------------
print("startup: a helper.max_tokens that is not a positive whole number is refused")
for bad in ("big", 0, -5, True, 8192.5):
    d = tempfile.mkdtemp(prefix="bwf_thinking_bad_")
    path, _ = write_config(dict(BRAIN, max_tokens=bad), d)
    try:
        proc = subprocess.run([sys.executable, os.path.join(ROOT, "server.py")], capture_output=True, text=True,
                              timeout=20, env=dict(os.environ, GENCENTER_CONFIG=path,
                                                   GENCENTER_DATA=os.path.join(d, "data")))
        refused = (proc.returncode, proc.stdout + proc.stderr)
    except subprocess.TimeoutExpired:   # it started and kept serving: not refused
        refused = (None, "still running after 20s")
    check("startup: max_tokens=%r refused with a sentence naming it" % (bad,),
          refused[0] not in (0, None) and "\"max_tokens\"" in refused[1], repr((refused[0], refused[1][-300:])))

if FAILED:
    print("FAILED: %d checks: %s" % (len(FAILED), ", ".join(FAILED)))
    sys.exit(1)
print("OK: all checks passed")