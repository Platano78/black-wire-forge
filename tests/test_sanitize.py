"""Acceptance gate for sanitize.py, the lossless audio metadata stripper.

Section A: safety on adversarial/malformed input (no fixtures needed).
Section B: byte-level correctness on small synthetic containers we build
           in-process (no fixtures needed) -- proves the parser/rebuilder
           logic independent of any real render.
Section C: the decisive proof against REAL renders captured by
           capture_audio_fixtures.py into tests/fixtures/audio/. Skips
           cleanly (prints SKIP, does not fail) when those files or ffmpeg
           are absent -- this is the network/GPU-dependent half.
Section D: video (ffmpeg stream-copy remux). Skips cleanly (prints SKIP,
           does not fail) when ffmpeg/ffprobe are absent, same discipline
           as Section C -- a clean clone with no ffmpeg installed must see
           SKIP, never a FAIL (finding #21, "the exact failure a stranger
           reads as 'the project is broken'").

Run: python3 tests/test_sanitize.py
"""
import json
import os
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True  # a same-length source edit leaves file SIZE
# unchanged, so .pyc invalidation (mtime+size) can serve stale bytecode for code
# you just changed -- observed 2026-09-22 reporting a defect already reverted.

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
FIXTURES = os.path.join(HERE, "fixtures", "audio")

FAILED = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + detail) if not cond and detail else ""))
    if not cond:
        FAILED.append(name)


import sanitize  # noqa: E402

print("Section A -- safety on adversarial input")
check("empty bytes -> unchanged, not stripped",
      sanitize.strip_audio(b"", "x.mp3") == (b"", False))
check("truncated mp3 -> unchanged, not stripped",
      sanitize.strip_audio(b"ID3\x04\x00\x00\x00\x00\x00", "x.mp3") == (b"ID3\x04\x00\x00\x00\x00\x00", False))
_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
check("a PNG passed in by mistake -> unchanged, not stripped",
      sanitize.strip_audio(_png, "x.png") == (_png, False))
_random = bytes((i * 37 + 11) % 256 for i in range(500))
check("random bytes -> unchanged, not stripped",
      sanitize.strip_audio(_random, "x.mp3") == (_random, False))
check("random bytes, no filename -> unchanged, not stripped",
      sanitize.strip_audio(_random, "") == (_random, False))


print()
print("Section B -- byte-level correctness on synthetic containers")


def _flac_block(btype, payload, last):
    return bytes([(0x80 if last else 0) | btype]) + len(payload).to_bytes(3, "big") + payload


def _build_flac(comment_payload, extra_block=None):
    streaminfo = _flac_block(0, b"\x00" * 34, False)
    comment = _flac_block(4, comment_payload, False)   # VORBIS_COMMENT -- must be dropped
    blocks = streaminfo + comment
    if extra_block is not None:
        blocks += extra_block
    seek = _flac_block(3, b"seektable-bytes-here", True)  # SEEKTABLE -- must be kept, last
    audio = b"AUDIO-FRAMES-PAYLOAD-UNCHANGED-0123456789"
    return b"fLaC" + blocks + seek + audio, audio, streaminfo


needle = b"...class_type...safetensors..."
flac_data, flac_audio, flac_streaminfo = _build_flac(needle)
out, ok = sanitize.strip_audio(flac_data, "x.flac")
check("flac: stripped", ok)
check("flac: recipe (VORBIS_COMMENT) gone", needle not in out)
check("flac: STREAMINFO preserved verbatim, still first",
      out[4:4 + len(flac_streaminfo)] == flac_streaminfo)
check("flac: audio frames byte-identical tail", out.endswith(flac_audio))

flac_unknown, _, _ = _build_flac(needle, extra_block=_flac_block(120, b"reserved-type", False))
out2, ok2 = sanitize.strip_audio(flac_unknown, "x.flac")
check("flac: unknown/reserved block type -> bail, not stripped",
      (out2, ok2) == (flac_unknown, False))

flac_truncated = flac_data[:20]
check("flac: truncated -> bail, not stripped",
      sanitize.strip_audio(flac_truncated, "x.flac") == (flac_truncated, False))


def _synchsafe(n):
    return bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])


def _build_mp3(tag_body, id3v1=True):
    header = b"ID3" + bytes([4, 0, 0]) + _synchsafe(len(tag_body))
    audio = b"\xff\xfb" + b"AUDIO-FRAME-DATA-BYTES-STAY-PUT"
    out = header + tag_body + audio
    if id3v1:
        out += b"TAG" + b"\x00" * 125
    return out, audio


mp3_needle = b"...ckpt_name...class_type...safetensors..."
mp3_data, mp3_audio = _build_mp3(mp3_needle)
out3, ok3 = sanitize.strip_audio(mp3_data, "x.mp3")
check("mp3: stripped", ok3)
check("mp3: recipe (ID3v2 TXXX-equivalent) gone", mp3_needle not in out3)
check("mp3: frames byte-identical, tag boundaries exact", out3 == mp3_audio)

mp3_no_tag = b"\xff\xfb" + b"BARE-FRAMES-NO-METADATA-AT-ALL-HERE"
out4, ok4 = sanitize.strip_audio(mp3_no_tag, "x.mp3")
check("mp3: no tag present -> trivially clean, stripped True, bytes unchanged",
      (out4, ok4) == (mp3_no_tag, True))

mp3_garbage = b"ID3" + bytes([4, 0, 0]) + _synchsafe(9999) + b"short"
check("mp3: ID3v2 size overruns file -> bail, not stripped",
      sanitize.strip_audio(mp3_garbage, "x.mp3") == (mp3_garbage, False))


print()
print("Section C -- decisive proof against REAL renders (tests/fixtures/audio/)")

FIXTURE_FILES = {"flac": "sfx_flac.flac", "mp3": "sfx_mp3.mp3", "opus": "sfx_opus.opus"}
NEEDLES = [
    b"black wire forge metadata test tone, distinctive marker XKQ77-STRIP",
    b"stable-audio-open-1.0/stable-audio-open-1.0.safetensors",
    b"stable-audio-open-1.0/t5-base.safetensors",
    b"class_type", b"ckpt_name", b"safetensors",
]


def _has_tool(name):
    from shutil import which
    return which(name) is not None


def _ffmpeg_md5(path):
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "md5", "-"],
                        capture_output=True, text=True, timeout=30)
    return r.stdout.strip()


def _ffprobe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                         "format=duration,format_name:stream=codec_name,sample_rate,channels",
                         "-of", "json", path], capture_output=True, text=True, timeout=30)
    return json.loads(r.stdout)


def _scan(data):
    return [n for n in NEEDLES if n in data]


def _flac_audio_start(data):
    """Independent (does not call sanitize) walk to the first audio byte,
    used only to prove the tail sanitize kept is byte-identical."""
    pos = 4
    while True:
        b0 = data[pos]
        length = int.from_bytes(data[pos + 1:pos + 4], "big")
        pos += 4 + length
        if b0 & 0x80:
            return pos


def _mp3_audio_start(data):
    """Independent (does not call sanitize) ID3v2-header-only walk."""
    if data[:3] != b"ID3":
        return 0
    size = 0
    for b in data[6:10]:
        size = (size << 7) | (b & 0x7F)
    tag_len = 10 + size
    if data[5] & 0x10:
        tag_len += 10
    return tag_len


if not _has_tool("ffmpeg") or not _has_tool("ffprobe"):
    print("  SKIP  ffmpeg/ffprobe not on PATH -- Section C skipped")
elif not all(os.path.isfile(os.path.join(FIXTURES, f)) for f in FIXTURE_FILES.values()):
    print("  SKIP  tests/fixtures/audio/ not present -- run "
          "tests/capture_audio_fixtures.py against an idle rig first")
else:
    for fmt, fname in FIXTURE_FILES.items():
        orig_path = os.path.join(FIXTURES, fname)
        orig = open(orig_path, "rb").read()

        red_hits = _scan(orig)
        check("%s: falsifiability RED -- original DOES carry the recipe" % fmt,
              len(red_hits) == len(NEEDLES), str(red_hits))

        out, ok = sanitize.strip_audio(orig, fname)
        check("%s: strip_audio reports stripped=True" % fmt, ok)
        if not ok:
            continue

        strip_path = "/tmp/_sanitize_test_%s_%s" % (fmt, fname)
        open(strip_path, "wb").write(out)

        green_hits = _scan(out)
        check("%s: falsifiability GREEN -- stripped carries ZERO of the recipe" % fmt,
              green_hits == [], str(green_hits))

        orig_md5 = _ffmpeg_md5(orig_path)
        strip_md5 = _ffmpeg_md5(strip_path)
        print("  %s ffmpeg md5   original=%s   stripped=%s" % (fmt, orig_md5, strip_md5))
        check("%s: LOSSLESS -- decoded-audio MD5 identical" % fmt,
              orig_md5 and orig_md5 == strip_md5)

        if fmt == "flac":
            a_start = _flac_audio_start(orig)
            check("flac: audio frames byte-identical (independent boundary walk)",
                  out.endswith(orig[a_start:]) and len(out) - len(orig[a_start:]) < len(orig))
        elif fmt == "mp3":
            a_start = _mp3_audio_start(orig)
            check("mp3: audio frames byte-identical (independent ID3v2 boundary walk)",
                  out == orig[a_start:a_start + len(out)])

        o_probe, s_probe = _ffprobe(orig_path), _ffprobe(strip_path)
        o_fmt, s_fmt = o_probe["format"], s_probe["format"]
        o_stream, s_stream = o_probe["streams"][0], s_probe["streams"][0]
        check("%s: still plays -- same codec/sample_rate/channels" % fmt,
              (o_stream["codec_name"], o_stream["sample_rate"], o_stream["channels"]) ==
              (s_stream["codec_name"], s_stream["sample_rate"], s_stream["channels"]))
        check("%s: still plays -- duration within 0.05s" % fmt,
              abs(float(o_fmt["duration"]) - float(s_fmt["duration"])) < 0.05)

print()
if shutil.which("ffmpeg") and shutil.which("ffprobe"):
    print("Section D -- video (ffmpeg stream-copy remux, ffmpeg/ffprobe present here)")

    def _ffmpeg_framemd5(path):
        r = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-map", "0", "-f", "framemd5", "-"],
                            capture_output=True, text=True, timeout=30)
        return "\n".join(ln for ln in r.stdout.splitlines() if not ln.startswith("#"))


    def _ffprobe_format_tags(path):
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format_tags",
                             "-of", "json", path], capture_output=True, text=True, timeout=30)
        return r.stdout


    CLIP_PATH = "/tmp/_sanitize_test_clip.mp4"
    STRIP_PATH = "/tmp/_sanitize_test_clip_stripped.mp4"
    clip_ok = False
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-v", "error",
             "-f", "lavfi", "-i", "testsrc=size=160x120:rate=24",
             "-f", "lavfi", "-i", "sine=frequency=440",
             "-t", "2", "-c:v", "libx264", "-c:a", "aac",
             "-metadata", "comment=SECRET_RECIPE", "-metadata", "title=SECRET_TITLE",
             CLIP_PATH],
            capture_output=True, timeout=30)
        clip_ok = r.returncode == 0 and os.path.isfile(CLIP_PATH)
    except Exception:
        clip_ok = False
    check("video: test clip built (ffmpeg lavfi testsrc+sine, 2s, tagged SECRET_RECIPE/SECRET_TITLE)",
          clip_ok)

    clip_data = open(CLIP_PATH, "rb").read() if clip_ok else b""
    tags_before = _ffprobe_format_tags(CLIP_PATH) if clip_ok else ""
    check("video: falsifiability RED -- original clip DOES carry the recipe (format_tags)",
          clip_ok and "SECRET_RECIPE" in tags_before and "SECRET_TITLE" in tags_before, tags_before)
    check("video: falsifiability RED -- SECRET_RECIPE in the original's raw bytes",
          clip_ok and b"SECRET_RECIPE" in clip_data)

    out, ok = sanitize.strip_video(clip_data, "clip.mp4") if clip_ok else (b"", False)
    check("video: strip_video reports stripped=True", ok)
    if ok:
        open(STRIP_PATH, "wb").write(out)
        tags_after = _ffprobe_format_tags(STRIP_PATH)
        check("video: falsifiability GREEN -- stripped format_tags carries no SECRET anywhere",
              "SECRET" not in tags_after, tags_after)
        check("video: falsifiability GREEN -- SECRET_RECIPE not in the stripped file's raw bytes",
              b"SECRET_RECIPE" not in out)

        md5_before = _ffmpeg_framemd5(CLIP_PATH)
        md5_after = _ffmpeg_framemd5(STRIP_PATH)
        print("  video framemd5   original=%s...   stripped=%s..." %
              (md5_before[:60], md5_after[:60]))
        check("video: decoded content identical (framemd5, header lines excluded)",
              bool(md5_before) and md5_before == md5_after)
    else:
        check("video: falsifiability GREEN -- stripped format_tags carries no SECRET anywhere",
              False, "strip_video did not report success")
        check("video: falsifiability GREEN -- SECRET_RECIPE not in the stripped file's raw bytes",
              False, "strip_video did not report success")
        check("video: decoded content identical (framemd5, header lines excluded)",
              False, "strip_video did not report success")

    check("video: non-video bytes named .mp4 -> unchanged, not stripped",
          sanitize.strip_video(_random, "x.mp4") == (_random, False))
    if clip_ok:
        corrupt = clip_data[:300]   # truncated -- not a parseable mp4
        check("video: corrupt/truncated .mp4 -> unchanged, not stripped",
              sanitize.strip_video(corrupt, "x.mp4") == (corrupt, False))
else:
    print("Section D -- SKIP: ffmpeg/ffprobe not on PATH")


print()
print("/api/credits' clean_download reflects what is ACTUALLY installed here, "
      "not a hardcoded assumption (finding #21: a clean clone may have neither "
      "ffmpeg nor Pillow)")
import importlib.util  # noqa: E402
import _scratch_config  # noqa: E402 -- must run before server.py's own exec_module below

try:
    import PIL  # noqa: F401
    _pil_here = True
except ImportError:
    _pil_here = False
_ffmpeg_here = shutil.which("ffmpeg") is not None

_srv_spec = importlib.util.spec_from_file_location("srv_credits_check", os.path.join(ROOT, "server.py"))
srv_credits = importlib.util.module_from_spec(_srv_spec)
_srv_spec.loader.exec_module(srv_credits)

_h = srv_credits.Handler.__new__(srv_credits.Handler)
_h.path = "/api/credits"
_h.command = "GET"
_h.headers = {"Host": "127.0.0.1"}   # B1 guard reads Host; stub has no socket
_results = []
_h.send_json = lambda payload, code=200: _results.append((payload, code))
srv_credits.Handler.do_GET(_h)
_payload, _code = _results[-1] if _results else ({}, None)
check("video: /api/credits returns 200 with a 'clean_download' key",
      _code in (200, None) and isinstance(_payload.get("clean_download"), list), repr(_payload)[:200])
check("/api/credits clean_download contains 'video' iff ffmpeg is on PATH here (%s)" % _ffmpeg_here,
      ("video" in (_payload.get("clean_download") or [])) == _ffmpeg_here,
      repr(_payload.get("clean_download")))
check("/api/credits clean_download contains 'image' iff Pillow is importable here (%s)" % _pil_here,
      ("image" in (_payload.get("clean_download") or [])) == _pil_here,
      repr(_payload.get("clean_download")))
check("/api/credits clean_download always contains 'audio' (some audio formats are always cleanable)",
      "audio" in (_payload.get("clean_download") or []))
check("video: /api/credits clean_download never contains '3d'",
      "3d" not in (_payload.get("clean_download") or []))
check("video: /api/credits clean_audio_exts is exactly sanitize's real audio coverage",
      set(_payload.get("clean_audio_exts") or []) == {"flac", "mp3", "opus", "ogg"},
      repr(_payload.get("clean_audio_exts")))


print()
print(("FAILED: %d" % len(FAILED)) if FAILED else "ALL PASS")
sys.exit(1 if FAILED else 0)
