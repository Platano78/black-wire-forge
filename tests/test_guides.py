"""Gate for room guides P1 -- guides.py, GET /api/guide, POST /api/guide/chat.

Everything runs against:
  - a fake OpenAI-compatible helper (stdlib http.server) that records every
    chat request and answers GET /models with whatever the test sets;
  - a scratch GENCENTER_CONFIG / GENCENTER_DATA (mkdtemp), set BEFORE
    server.py is imported, the same pattern as test_helper.py;
  - the real HTTP API, served in-process on a free 127.0.0.1 port.

A second server, imported from a SEPARATE scratch config with no "helper"
key, covers the no-brain half of the contract.

Run: python3 tests/test_guides.py
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
                "models": None, "requests": [], "model_gets": 0}


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
        body = json.loads(self.rfile.read(n) or b"{}")
        HELPER_STATE["requests"].append(body)
        if HELPER_STATE["status"] != 200:
            return self._send(HELPER_STATE["status"], {"error": "boom"})
        self._send(200, {"choices": [{"message": {"content": HELPER_STATE["reply"]},
                                      "finish_reason": HELPER_STATE["finish"]}]})

    def log_message(self, *a):
        pass


fake_helper = ThreadingHTTPServer(("127.0.0.1", 0), FakeHelperHandler)
HELPER_PORT = fake_helper.server_address[1]
threading.Thread(target=fake_helper.serve_forever, daemon=True).start()


def start_server(name, with_helper):
    scratch = tempfile.mkdtemp(prefix="bwf_guides_")
    cfg_path = os.path.join(scratch, "config.json")
    port = free_port()
    cfg = {"port": port, "bind": "127.0.0.1", "title": "guides",
           "timing": {"poll_seconds": 30, "job_poll_seconds": 30},
           "lanes": [{"id": "c1", "name": "Comfy lane", "host": "127.0.0.1", "port": 1, "caps": ["image"]}]}
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


srv, PORT = start_server("srv_guides", True)
srv2, PORT2 = start_server("srv_guides_nobrain", False)


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


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def reset_context_cache():
    srv._GUIDE_CONTEXT.update(at=None, value=None)


def chat(messages, verbosity="compact", room="cutting", port=None):
    return http("/api/guide/chat", {"room": room, "verbosity": verbosity, "messages": messages}, port=port)


COMPACT = read("guides/film/system-prompt.txt")
VERBOSE = read("guides/film/system-prompt-verbose.txt")
META = json.loads(read("guides/film/guide.json"))

# ---------------------------------------------------------------------------
# 1. every guide rooms.json names loads; the shipped files hold their shape
# ---------------------------------------------------------------------------
print("loader: every guide rooms.json names loads")
import guides  # noqa: E402  (ROOT is on sys.path above)
named = {r["guide"] for r in json.loads(read("rooms.json")) if r.get("guide")}
loaded = guides.load_all()
check("loader: the Cutting Room names the film guide",
      any(r.get("id") == "cutting" and r.get("guide") == "film" for r in json.loads(read("rooms.json"))))
check("loader: every named guide loaded", named and set(loaded) == named, repr((named, set(loaded))))
check("loader: the server holds the same guides", set(srv.GUIDES) == named, repr(set(srv.GUIDES)))
check("loader: token estimate is chars/4",
      loaded["film"]["projections"]["compact"]["tokens"] == (len(COMPACT) + 3) // 4
      and loaded["film"]["projections"]["verbose"]["tokens"] == (len(VERBOSE) + 3) // 4)
check("guide files: compact is a verbatim prefix of verbose", VERBOSE.startswith(COMPACT))
check("guide files: no live-trial or README ships",
      not any(os.path.exists(os.path.join(ROOT, "guides/film", f)) for f in ("live-trial.md", "README.md")))

print("loader: a broken guide is refused with a sentence naming the file")
tmp = tempfile.mkdtemp(prefix="bwf_guides_broken_")
rooms_path = os.path.join(tmp, "rooms.json")
with open(rooms_path, "w") as f:
    json.dump([{"id": "cutting", "kind": "cutting", "guide": "film"}], f)
try:
    guides.load_all(rooms_path, os.path.join(tmp, "guides"))
    check("loader: missing guide.json refused", False, "no error")
except guides.GuideError as e:
    check("loader: missing guide.json refused, naming the file", "guide.json" in str(e), str(e))
os.makedirs(os.path.join(tmp, "guides", "film"))
for fname in ("film.persona.json", "system-prompt.txt", "system-prompt-verbose.txt", "knowledge.md"):
    with open(os.path.join(tmp, "guides", "film", fname), "w") as f:
        f.write(read("guides/film/" + fname))
bad = dict(META, caps={"compact": {"max_tokens": 0, "answer_chars": 10},
                       "verbose": {"max_tokens": 1, "answer_chars": 1}})
with open(os.path.join(tmp, "guides", "film", "guide.json"), "w") as f:
    json.dump(bad, f)
try:
    guides.load_all(rooms_path, os.path.join(tmp, "guides"))
    check("loader: a zero cap is refused", False, "no error")
except guides.GuideError as e:
    check("loader: a zero cap is refused", "caps.compact" in str(e), str(e))
with open(os.path.join(tmp, "guides", "film", "guide.json"), "w") as f:
    json.dump(META, f)
open(os.path.join(tmp, "guides", "film", "knowledge.md"), "w").close()
try:
    guides.load_all(rooms_path, os.path.join(tmp, "guides"))
    check("loader: an empty knowledge file is refused", False, "no error")
except guides.GuideError as e:
    check("loader: an empty knowledge file is refused, naming it", "knowledge.md" in str(e), str(e))

# ---------------------------------------------------------------------------
# 2. GET /api/guide
# ---------------------------------------------------------------------------
print("GET /api/guide: the Cutting Room's guide, with token sizes")
HELPER_STATE["models"] = {"data": [{"id": "other", "meta": {"n_ctx": 111}},
                                   {"id": "test-model", "meta": {"n_ctx": 8192, "n_ctx_train": 131072}}]}
reset_context_cache()
code, body = http("/api/guide?room=cutting")
g = body.get("guide") or {}
check("guide: 200", code == 200, repr((code, body)))
check("guide: room echoed", body.get("room") == "cutting", repr(body))
check("guide: id/name/definition/greeting",
      g.get("id") == "film" and g.get("name") == META["name"] and g.get("greeting") == META["greeting"]
      and g.get("definition") == json.loads(read("guides/film/film.persona.json"))["definition"], repr(g))
check("guide: no_brain lines", g.get("no_brain") == META["no_brain"], repr(g.get("no_brain")))
check("guide: verbosity default is compact", g.get("verbosity_default") == "compact", repr(g))
check("guide: token sizes for both projections",
      (g.get("projections") or {}).get("compact", {}).get("tokens") == (len(COMPACT) + 3) // 4
      and (g.get("projections") or {}).get("verbose", {}).get("tokens") == (len(VERBOSE) + 3) // 4,
      repr(g.get("projections")))
check("guide: no prompt text in the payload", "text" not in json.dumps(g.get("projections")), repr(g.get("projections")))
check("guide: helper true", body.get("helper") is True, repr(body))
check("guide: helper_context from the matching model's meta.n_ctx", body.get("helper_context") == 8192, repr(body))
check("guide: add_brain sentence", "helper" in (body.get("add_brain") or "") and "config.json" in body["add_brain"],
      repr(body.get("add_brain")))

print("GET /api/guide: helper_context is cached, then read from other keys, then null")
gets = HELPER_STATE["model_gets"]
http("/api/guide?room=cutting")
http("/api/guide?room=cutting")
check("context: probed at most once per cache window", HELPER_STATE["model_gets"] == gets,
      repr((gets, HELPER_STATE["model_gets"])))
HELPER_STATE["models"] = {"data": [{"id": "test-model", "context_length": 32768}]}
reset_context_cache()
check("context: context_length is read", http("/api/guide?room=cutting")[1].get("helper_context") == 32768)
HELPER_STATE["models"] = {"data": [{"id": "not-the-model", "max_context_length": 4096}]}
reset_context_cache()
check("context: no matching id falls back to the first entry",
      http("/api/guide?room=cutting")[1].get("helper_context") == 4096)
HELPER_STATE["models"] = {"data": [{"id": "test-model"}]}
reset_context_cache()
check("context: null when the endpoint says nothing", http("/api/guide?room=cutting")[1].get("helper_context") is None)
HELPER_STATE["models"] = None
reset_context_cache()
gets = HELPER_STATE["model_gets"]
check("context: null when /models is missing", http("/api/guide?room=cutting")[1].get("helper_context") is None)
http("/api/guide?room=cutting")
check("context: a failure is cached too", HELPER_STATE["model_gets"] == gets + 1,
      repr((gets, HELPER_STATE["model_gets"])))

print("GET /api/guide: a room without a guide, an unknown room")
code, body = http("/api/guide?room=music")
check("no-guide room: 200 with guide null", code == 200 and body.get("guide") is None, repr((code, body)))
code, body = http("/api/guide?room=no-such-room")
check("unknown room: 404 with a sentence", code == 404 and isinstance(body.get("error"), str) and body["error"],
      repr((code, body)))

# ---------------------------------------------------------------------------
# 3. POST /api/guide/chat -- what reaches the helper
# ---------------------------------------------------------------------------
print("chat compact: system is the compact file exactly, max_tokens is the compact cap")
HELPER_STATE.update(reply="Start with the ending.", finish="stop", status=200, requests=[])
code, body = chat([{"role": "user", "content": "a lighthouse keeper's last night"}])
req = HELPER_STATE["requests"][-1] if HELPER_STATE["requests"] else {}
check("compact: 200 ok", code == 200 and body.get("ok") is True, repr((code, body)))
check("compact: reply fields", body.get("text") == "Start with the ending." and body.get("truncated") is False
      and body.get("dropped") == 0 and body.get("verbosity") == "compact", repr(body))
check("compact: system message is the compact file, byte for byte",
      (req.get("messages") or [{}])[0] == {"role": "system", "content": COMPACT})
check("compact: max_tokens is the compact cap", req.get("max_tokens") == META["caps"]["compact"]["max_tokens"],
      repr(req.get("max_tokens")))

print("chat verbose: system is the verbose file exactly, max_tokens is the verbose cap")
code, body = chat([{"role": "user", "content": "go deep"}], verbosity="verbose")
req = HELPER_STATE["requests"][-1]
check("verbose: 200", code == 200 and body.get("verbosity") == "verbose", repr((code, body)))
check("verbose: system message is the verbose file, byte for byte",
      req["messages"][0] == {"role": "system", "content": VERBOSE})
check("verbose: max_tokens is the verbose cap", req.get("max_tokens") == META["caps"]["verbose"]["max_tokens"])

print("chat: history reaches the helper in order, after the system message")
hist = [{"role": "user", "content": "idea one"}, {"role": "assistant", "content": "answer one"},
        {"role": "user", "content": "idea two"}, {"role": "assistant", "content": "answer two"},
        {"role": "user", "content": "and now?"}]
code, body = chat(hist)
req = HELPER_STATE["requests"][-1]
check("history: sent in order", req["messages"][1:] == hist, repr(req["messages"][1:]))

print("chat: guide chat uses timeout_s (default 120); /api/helper keeps its own default")
seen = []
_orig_chat = srv._helper_chat


def _recording_chat(messages, **kw):
    seen.append(kw)
    return _orig_chat(messages, **kw)


srv._helper_chat = _recording_chat
chat([{"role": "user", "content": "hi"}])
saved_timeout = srv.HELPER.pop("timeout_s")
chat([{"role": "user", "content": "hi"}])
srv.HELPER["timeout_s"] = saved_timeout
http("/api/helper", {"action": "write", "cap": "image", "mode": "t2i", "text": "a kite"})
srv._helper_chat = _orig_chat
check("timeout: guide chat passes timeout_s", seen and seen[0].get("timeout") == 2, repr(seen))
check("timeout: guide chat defaults to 120", len(seen) > 1 and seen[1].get("timeout") == 120, repr(seen))
check("timeout: /api/helper passes no timeout of its own (keeps its 60 default)",
      len(seen) > 2 and "timeout" not in seen[2] and "max_tokens" not in seen[2], repr(seen))

print("chat: trimming keeps the newest turns, first kept is user, last user always kept")
many = [{"role": "user" if i % 2 == 0 else "assistant", "content": "m%02d " % i + "x" * 95} for i in range(51)]
code, body = chat(many)
req = HELPER_STATE["requests"][-1]
kept = req["messages"][1:]
check("trim turns: dropped counted", body.get("dropped") == 12, repr(body.get("dropped")))
check("trim turns: 39 kept", len(kept) == 39, repr(len(kept)))
check("trim turns: first kept is a user message", kept and kept[0]["role"] == "user", repr(kept[:1]))
check("trim turns: newest kept, last user intact", kept and kept[-1] == many[-1] and kept == many[12:])
big = []
for i in range(7):
    big.append({"role": "user" if i % 2 == 0 else "assistant", "content": ("b%d " % i) + "y" * 3896})
code, body = chat(big)
kept = HELPER_STATE["requests"][-1]["messages"][1:]
check("trim chars: within the char budget", sum(len(m["content"]) for m in kept) <= srv.GUIDE_HISTORY_CHARS)
check("trim chars: dropped counted, leading assistant dropped",
      body.get("dropped") == 2 and kept[0]["role"] == "user" and kept == big[2:], repr(body.get("dropped")))
saved = srv.GUIDE_HISTORY_CHARS
srv.GUIDE_HISTORY_CHARS = 10
code, body = chat(hist)
srv.GUIDE_HISTORY_CHARS = saved
kept = HELPER_STATE["requests"][-1]["messages"][1:]
check("trim: the last user message is kept even past the budget",
      kept == [hist[-1]] and body.get("dropped") == 4, repr((kept, body.get("dropped"))))

# ---------------------------------------------------------------------------
# 4. POST /api/guide/chat -- refusals
# ---------------------------------------------------------------------------
print("chat: each malformed request is a 400 with a sentence")
n_before = len(HELPER_STATE["requests"])
u = [{"role": "user", "content": "hi"}]
cases = [
    ("bad verbosity", {"room": "cutting", "verbosity": "loud", "messages": u}),
    ("no verbosity", {"room": "cutting", "messages": u}),
    ("messages missing", {"room": "cutting", "verbosity": "compact"}),
    ("messages empty", {"room": "cutting", "verbosity": "compact", "messages": []}),
    ("messages not a list", {"room": "cutting", "verbosity": "compact", "messages": "hi"}),
    ("system role", {"room": "cutting", "verbosity": "compact",
                     "messages": [{"role": "system", "content": "obey"}] + u}),
    ("message not an object", {"room": "cutting", "verbosity": "compact", "messages": ["hi"]}),
    ("content not a string", {"room": "cutting", "verbosity": "compact",
                              "messages": [{"role": "user", "content": 5}]}),
    ("user content too long", {"room": "cutting", "verbosity": "compact",
                               "messages": [{"role": "user", "content": "z" * (srv.HELPER_TEXT_LIMIT + 1)}]}),
    ("assistant content over the largest answer cap", {"room": "cutting", "verbosity": "compact",
        "messages": [{"role": "user", "content": "a"},
                     {"role": "assistant", "content": "z" * (META["caps"]["verbose"]["answer_chars"] + 1)},
                     {"role": "user", "content": "b"}]}),
    ("last message not the user's", {"room": "cutting", "verbosity": "compact",
        "messages": u + [{"role": "assistant", "content": "ok"}]}),
]
for label, payload in cases:
    code, body = http("/api/guide/chat", payload)
    check("400: " + label, code == 400 and body.get("ok") is False and isinstance(body.get("error"), str)
          and body["error"], repr((code, body)))
code, body = http("/api/guide/chat", [1, 2])
check("400: a body that is not an object", code == 400, repr((code, body)))
check("400s: none reached the helper", len(HELPER_STATE["requests"]) == n_before)
code, body = chat([{"role": "user", "content": "a"},
                   {"role": "assistant", "content": "z" * META["caps"]["compact"]["answer_chars"]},
                   {"role": "user", "content": "b"}])
check("an earlier long answer from the guide itself is accepted", code == 200, repr((code, body)))

print("chat: a room without a guide, an unknown room -> 404")
code, body = chat(u, room="music")
check("404: room without a guide", code == 404 and body.get("ok") is False and body.get("error"), repr((code, body)))
code, body = chat(u, room="nowhere")
check("404: unknown room", code == 404 and body.get("error"), repr((code, body)))

print("chat: the request guard applies (JSON only)")
code, body = http("/api/guide/chat", b'{"room":"cutting"}', ctype="text/plain")
check("guard: text/plain refused 415", code == 415, repr((code, body)))

# ---------------------------------------------------------------------------
# 5. POST /api/guide/chat -- what comes back
# ---------------------------------------------------------------------------
print("chat: finish_reason length is flagged truncated, text kept")
HELPER_STATE.update(reply="Beat one: the lamp. Beat two: the", finish="length")
code, body = chat(u)
check("length: truncated true", code == 200 and body.get("truncated") is True, repr(body))
check("length: text kept as sent", body.get("text") == "Beat one: the lamp. Beat two: the", repr(body))

print("chat: an over-cap answer is truncated at a sentence end")
cap = META["caps"]["compact"]["answer_chars"]
HELPER_STATE.update(reply="The lamp turns once more. " * (cap // 20), finish="stop")
code, body = chat(u)
t = body.get("text") or ""
check("over cap: truncated true", body.get("truncated") is True, repr(body.get("truncated")))
check("over cap: within the cap", len(t) <= cap, repr(len(t)))
check("over cap: ends at a sentence end", t.endswith("more.") and len(t) >= int(cap * 0.7), repr(t[-40:]))
HELPER_STATE.update(reply="w" * (cap + 500))
code, body = chat(u)
check("over cap, no sentence end: hard cut at the cap",
      body.get("truncated") is True and body.get("text") == "w" * cap, repr(len(body.get("text") or "")))
HELPER_STATE.update(reply="z" * cap)
check("exactly at the cap: not truncated", chat(u)[1].get("truncated") is False)

print("chat: <think> blocks are stripped")
HELPER_STATE.update(reply="<think>the user wants a plan</think>\n  Name the camera against the plate.  ")
code, body = chat(u)
check("think: stripped and trimmed", body.get("text") == "Name the camera against the plate.", repr(body.get("text")))

print("chat: helper failure -> 503 with the existing sentence")
HELPER_STATE.update(status=500)
code, body = chat(u)
check("503: helper errors", code == 503 and body.get("error") == srv.HELPER_BUSY_SENTENCE, repr((code, body)))
HELPER_STATE.update(status=200)
saved_url = srv.HELPER["url"]
srv.HELPER["url"] = "http://127.0.0.1:%d/v1" % free_port()
code, body = chat(u)
srv.HELPER["url"] = saved_url
check("503: helper unreachable, the connect sentence", code == 503 and "isn't answering" in (body.get("error") or ""),
      repr((code, body)))

# ---------------------------------------------------------------------------
# 6. /api/helper is unchanged
# ---------------------------------------------------------------------------
print("/api/helper write: still 512 tokens, same response shape")
HELPER_STATE.update(reply="a red kite over the sea", finish="length", requests=[])
code, body = http("/api/helper", {"action": "write", "cap": "image", "mode": "t2i", "text": "a kite"})
req = HELPER_STATE["requests"][-1] if HELPER_STATE["requests"] else {}
check("helper: max_tokens 512", req.get("max_tokens") == 512, repr(req.get("max_tokens")))
check("helper: response is exactly ok + text", code == 200 and body == {"ok": True, "text": "a red kite over the sea"},
      repr(body))
HELPER_STATE.update(reply="q" * 3000)
code, body = http("/api/helper", {"action": "write", "text": "x"})
check("helper: still capped at 2000 chars", len(body.get("text") or "") == srv.HELPER_ANSWER_LIMIT,
      repr(len(body.get("text") or "")))

# ---------------------------------------------------------------------------
# 7. no brain configured
# ---------------------------------------------------------------------------
print("no helper: /api/guide still carries the guidance; chat is a 409 no_brain")
code, body = http("/api/guide?room=cutting", port=PORT2)
check("no brain: guide still there", code == 200 and (body.get("guide") or {}).get("no_brain") == META["no_brain"],
      repr((code, body)))
check("no brain: helper false, context null", body.get("helper") is False and body.get("helper_context") is None,
      repr(body))
code, body = chat(u, port=PORT2)
check("no brain: 409 no_brain with the add_brain sentence",
      code == 409 and body.get("no_brain") is True and body.get("error") == srv2.GUIDE_ADD_BRAIN, repr((code, body)))

# ---------------------------------------------------------------------------
# 8. nothing private ships in guides/
# ---------------------------------------------------------------------------
print("guides/: no forbidden string")
forbidden = os.environ.get("BWF_FORBIDDEN_LIST") or os.path.join(ROOT, "tools", "publish", "forbidden.txt")
if not os.path.isfile(forbidden):
    print("  SKIP  forbidden-string check: no list at %s (the publisher's list; set BWF_FORBIDDEN_LIST "
          "to run it)" % os.path.relpath(forbidden, ROOT))
else:
    with open(forbidden, encoding="utf-8") as f:
        words = [w.strip().lower() for w in f if w.strip() and not w.startswith("#")]
    hits = []
    for dirpath, _, files in os.walk(os.path.join(ROOT, "guides")):
        for fname in files:
            path = os.path.join(dirpath, fname)
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read().lower()
            hits += ["%s: %s" % (os.path.relpath(path, ROOT), w) for w in words if w in text]
    check("forbidden: guides/ is clean (%d strings checked)" % len(words), not hits, repr(hits[:10]))

if FAILED:
    print("FAILED: %d checks: %s" % (len(FAILED), ", ".join(FAILED)))
    sys.exit(1)
print("OK: all checks passed")
