"""Acceptance gate for F1: a download labelled "recipe removed" must never
serve a file that was not actually cleaned.

Covers the fix to strip_metadata()/proxy_view_local() -- a WAV clean
download (dl=1, no keep_recipe) must ACTUALLY BE CLEANED (200 blob with
the text chunks gone), a WAV sanitize cannot parse (truncated) must be
REFUSED (4xx), never silently served as the original under the "cleaned"
label; a FLAC clean download must actually be cleaned; a plain
(keep_recipe=1) download of a WAV must still work unchanged; and
/api/credits must advertise WAV as cleanable but not M4A.

RED/GREEN: run against the pre-fix strip_metadata() (which discarded
sanitize's `_stripped` result and always returned the original bytes with
no signal), "WAV clean download refused" FAILED -- the handler served a
200 blob of the untouched original instead of a 4xx. After propagating
`stripped` through strip_metadata() and refusing on False in
proxy_view()/proxy_view_local(), it passes. (WAV cleaning itself landed
later: sanitize.strip_audio gained _strip_wav, so the first section now
expects a 200 with the text chunks gone rather than a 4xx.)

Run: python3 tests/test_clean_download.py
"""
import importlib.util, os, re, shutil, sys
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FAILED = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + detail) if not cond and detail else ""))
    if not cond:
        FAILED.append(name)


import _scratch_config  # noqa: E402 -- must run before server.py's own exec_module below
import sanitize  # noqa: E402

spec = importlib.util.spec_from_file_location("srv_clean_dl", os.path.join(ROOT, "server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)

SCRATCH = os.path.join(HERE, "_scratch_clean_download")
shutil.rmtree(SCRATCH, ignore_errors=True)
os.makedirs(os.path.join(SCRATCH, "job1"), exist_ok=True)
srv.LOCAL_OUTPUTS_DIR = SCRATCH
srv.JOBS_FILE = os.path.join(SCRATCH, "jobs.json")
srv.SEQ_DIR = os.path.join(SCRATCH, "sequences")


def call_view(q):
    obj = srv.Handler.__new__(srv.Handler)
    results = []
    obj.send_json = lambda payload, code=200: results.append(("json", payload, code))
    obj.send_blob = lambda data, ctype, filename=None, download=False, ranges=False: \
        results.append(("blob", data, ctype))
    srv.Handler.proxy_view_local(obj, q)
    return results[-1] if results else None


def _riff(cid, payload):
    chunk = cid + len(payload).to_bytes(4, "little") + payload
    return chunk + (b"\x00" if len(payload) % 2 else b"")


fmt = (b"fmt " + (16).to_bytes(4, "little") + b"\x01\x00\x01\x00"
       + (8000).to_bytes(4, "little") + (8000).to_bytes(4, "little")
       + b"\x01\x00\x08\x00")
needle = b"prompt: a secret prompt"
info = b"INFO" + b"ICMT" + len(needle).to_bytes(4, "little") + needle
data = b"data" + (64).to_bytes(4, "little") + bytes(64)
wav_bytes = b"RIFF" + (len(fmt) + len(_riff(b"LIST", info)) + len(data) + 4).to_bytes(4, "little") \
    + b"WAVE" + fmt + _riff(b"LIST", info) + data
wav_clean = b"RIFF" + (len(fmt) + len(data) + 4).to_bytes(4, "little") + b"WAVE" + fmt + data
wav_trunc = wav_bytes[:len(wav_bytes) - 10]    # data chunk declared larger than present
for name, blob in (("clip.wav", wav_bytes), ("clip_trunc.wav", wav_trunc)):
    with open(os.path.join(SCRATCH, "job1", name), "wb") as f:
        f.write(blob)

print("F1: WAV clean download is actually cleaned, never the untouched original")
kind, payload, code = call_view({"job": ["job1"], "filename": ["clip.wav"], "dl": ["1"]})
check("dl=1 (clean download) on a WAV is served as a blob, not a refusal",
      kind == "blob", str((kind, code, payload))[:200])
check("the text chunk (the prompt) is actually gone from the served bytes",
      kind == "blob" and needle not in payload and payload == wav_clean, repr(payload)[:200])
check("sanity: sanitize.strip_audio itself reports stripped=True for this WAV",
      sanitize.strip_audio(wav_bytes, "clip.wav")[1] is True)

print()
print("F1: a WAV that cannot be parsed is refused, never silently served as the original")
kind, payload, code = call_view({"job": ["job1"], "filename": ["clip_trunc.wav"], "dl": ["1"]})
check("dl=1 on a truncated WAV refuses with a 4xx, not a 200 blob",
      kind == "json" and 400 <= code < 500, str((kind, code, payload))[:200])
check("the refusal is the plain sentence, not a stack trace or Python exception text",
      kind == "json" and "recipe" in (payload.get("error") or "").lower(), str(payload)[:200])

print()
print("F1: plain (keep_recipe) download of the same WAV still works exactly as before")
kind2, data2, _ = call_view({"job": ["job1"], "filename": ["clip.wav"], "dl": ["1"], "keep_recipe": ["1"]})
check("keep_recipe=1 still serves the original bytes unchanged",
      kind2 == "blob" and data2 == wav_bytes, str(kind2))

print()
print("F1: viewing (dl=0) a WAV is untouched, same as any other output")
kind3, data3, _ = call_view({"job": ["job1"], "filename": ["clip.wav"], "dl": ["0"]})
check("dl=0 serves the original bytes untouched",
      kind3 == "blob" and data3 == wav_bytes, str(kind3))

def _flac_block(btype, payload, last):
    return bytes([(0x80 if last else 0) | btype]) + len(payload).to_bytes(3, "big") + payload


needle = b"...class_type...safetensors..."
streaminfo = _flac_block(0, b"\x00" * 34, False)
comment = _flac_block(4, needle, False)
seek = _flac_block(3, b"seektable-bytes-here", True)
audio = b"AUDIO-FRAMES-PAYLOAD-UNCHANGED-0123456789"
flac_bytes = b"fLaC" + streaminfo + comment + seek + audio

with open(os.path.join(SCRATCH, "job1", "clip.flac"), "wb") as f:
    f.write(flac_bytes)

print()
print("F1: FLAC clean download is actually cleaned")
kind4, data4, _ = call_view({"job": ["job1"], "filename": ["clip.flac"], "dl": ["1"]})
check("a FLAC clean download succeeds as a blob (sanitize.strip_audio supports it)",
      kind4 == "blob", str(kind4))
check("the recipe is actually gone from the served bytes",
      kind4 == "blob" and needle not in data4, "")
check("sanity: sanitize.strip_audio itself reports stripped=True for this FLAC",
      sanitize.strip_audio(flac_bytes, "clip.flac")[1] is True)

print()
print("F1: /api/credits advertises WAV as cleanable audio, still not M4A")
_h = srv.Handler.__new__(srv.Handler)
_h.path = "/api/credits"
_h.command = "GET"
_h.headers = {"Host": "127.0.0.1"}
_results = []
_h.send_json = lambda payload, code=200: _results.append((payload, code))
srv.Handler.do_GET(_h)
_payload, _code = _results[-1] if _results else ({}, None)
exts = set(_payload.get("clean_audio_exts") or [])
check("clean_audio_exts contains 'wav'", "wav" in exts, repr(exts))
check("clean_audio_exts does not contain 'm4a'", "m4a" not in exts, repr(exts))
check("clean_audio_exts is sanitize's real coverage: flac/mp3/opus/ogg/wav",
      exts == {"flac", "mp3", "opus", "ogg", "wav"}, repr(exts))

shutil.rmtree(SCRATCH, ignore_errors=True)

print()
print("F1: the share dialog's 'downloads exactly as it was made' button asks for the original")
# STATIC read of index.html, not a browser run: the server paths above are what
# enforce the refusal; this pins the one UI wiring that turned it against the user
# (an uncleanable WAV linked dl=1 without keep_recipe, so the browser saved the
# 422 JSON as the file).
_page = open(os.path.join(os.path.dirname(HERE), "index.html"), encoding="utf-8").read()
_m = re.search(r'id="shareDownloadLink" href="\' \+ viewURL\(([^)]*)\)', _page)
check("shareDownloadLink exists in openShare's uncleanable branch", _m is not None)
check("shareDownloadLink requests keep_recipe (viewURL(..., true, true))",
      bool(_m) and _m.group(1).replace(" ", "").endswith("true,true"),
      _m.group(1) if _m else "not found")

print()
print(("FAILED: %d" % len(FAILED)) if FAILED else "ALL PASS")
sys.exit(1 if FAILED else 0)
