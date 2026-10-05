"""Fit and Mix reach the page: pick the mode, upload the tracks, press Make, and the
result shows the one-line summary plus the downloads, done by clicking (Producer ->
Fit a part / Mix tracks -> upload -> Make), not through the API. Stand-in programs do
the work: a stand-in "producer Python" for Fit (it also records the argv it was given)
and a stand-in python3 on PATH for Mix (records argv the same way); a stand-in ffmpeg
is only found on PATH. Real server.py, real browser; SKIPs without Playwright/Chromium.

Run: python3 tests/test_producer_room_ui.py
"""
import base64
import io
import json
import os
import struct
import subprocess
import sys
import tempfile
import time
import wave

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import _ui_fixture as ui  # noqa: E402  (SKIPs cleanly without Playwright/Chromium)
from _ui_fixture import check, free_port, wait_up  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

REPO = os.path.dirname(HERE)
FIT_SUMMARY = "fitted 64 beats onto the target: 100.0 -> 95.0 BPM (stretch 0.95x), ffmpeg atempo"
MIX_SUMMARY = "mixed 2 tracks, -14.0 LUFS, true peak -1.0 dBTP, 0:05"
S = tempfile.mkdtemp(prefix="bwf_prod_ui_")
ARGV_FILE = os.path.join(S, "argv.json")

# The audio frames the stand-ins write, and the exact WAV bytes they emit
# (both write a real WAV through the `wave` module, 8000 Hz 16-bit mono),
# so the download check can compare the fetched file against the program's
# output.
FIT_FRAMES = bytes(800)
MIX_FRAMES = bytes(1600)
FIT_WAV = (b"RIFF" + struct.pack("<I", 36 + len(FIT_FRAMES)) + b"WAVEfmt "
           + struct.pack("<IHHIIHH", 16, 1, 1, 8000, 16000, 2, 16)
           + b"data" + struct.pack("<I", len(FIT_FRAMES)) + FIT_FRAMES)
MIX_WAV = (b"RIFF" + struct.pack("<I", 36 + len(MIX_FRAMES)) + b"WAVEfmt "
           + struct.pack("<IHHIIHH", 16, 1, 1, 8000, 16000, 2, 16)
           + b"data" + struct.pack("<I", len(MIX_FRAMES)) + MIX_FRAMES)

# Runs in the page context, so the fetch goes out with the same origin and
# Host header the page itself uses. Reports the link's href, the HTTP status,
# the byte length, the first 12 bytes (hex), the base64 body on success, and
# the response body on refusal.
DOWNLOAD_FETCH_JS = """async (name) => {
  const a = [...document.querySelectorAll('#jobExtras a')].find(x => x.textContent.includes(name));
  if(!a) return {found: false, name: name};
  const r = await fetch(a.href);
  const body = r.ok ? '' : (await r.clone().text()).slice(0, 300);
  const b = new Uint8Array(await r.arrayBuffer());
  let b64 = '';
  if (r.ok) { let s = ''; for (let i = 0; i < b.length; i += 0x8000) s += String.fromCharCode.apply(null, b.subarray(i, i + 0x8000)); b64 = btoa(s); }
  return {found: true, name: name, href: a.href, status: r.status, len: b.length,
          head: Array.from(b.slice(0, 12)).map(x => x.toString(16).padStart(2, '0')).join(''),
          b64: b64, body: body};
}"""


def download_check(page, label, name, want, frames):
    """Fetch the named link's href from the page context and check it answers
    200 with the exact length and the first 12 bytes the stand-in wrote, and
    that the downloaded file's audio frames equal the stand-in's (read with
    the `wave` module)."""
    res = page.evaluate(DOWNLOAD_FETCH_JS, name)
    ok = bool(res and res.get("found")) and res.get("status") == 200 \
        and res.get("len") == len(want) and res.get("head") == want[:12].hex()
    detail = res
    if ok:
        try:
            with wave.open(io.BytesIO(base64.b64decode(res["b64"]))) as w:
                ok = w.readframes(w.getnframes()) == frames
        except Exception as e:
            ok = False
            detail = (res, repr(e))
    check(label, ok, detail)

FIT_STUB = ("#!%s\nimport json, os, sys, wave\na = sys.argv[2:]\nif '--check' in a: sys.exit(0)\n"
            "out = a[a.index('--out') + 1]; os.makedirs(out, exist_ok=True)\n"
            "json.dump(sys.argv, open(os.environ['BWF_UI_ARGV'], 'w'))\n"
            "w = wave.open(os.path.join(out, 'fitted.wav'), 'wb')\n"
            "w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000); w.writeframes(bytes(800)); w.close()\n"
            "open(os.path.join(out, 'preview.mp3'), 'wb').write(b'ID3' + bytes(64))\n"
            "json.dump({'source_bpm': 100.0, 'target_bpm': 95.0, 'stretch': 0.95}, "
            "open(os.path.join(out, 'fit.json'), 'w'))\n"
            "print('PROGRESS 5/5', flush=True); print(%r, flush=True)\n") % (sys.executable, FIT_SUMMARY)

MIX_STUB = ("#!%s\nimport json, os, sys, wave\na = sys.argv[1:]\n"
            "json.dump(sys.argv, open(os.environ['BWF_UI_ARGV'], 'w'))\n"
            "if '--check' in a: sys.exit(0)\n"
            "out = a[a.index('--out') + 1]; os.makedirs(out, exist_ok=True)\n"
            "open(os.path.join(out, 'mix.mp3'), 'wb').write(b'ID3' + bytes(64))\n"
            "w = wave.open(os.path.join(out, 'mix.wav'), 'wb')\n"
            "w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000); w.writeframes(bytes(1600)); w.close()\n"
            "json.dump({'measured_lufs': -14.0, 'true_peak_dbtp': -1.0}, "
            "open(os.path.join(out, 'mix.json'), 'w'))\n"
            "print('PROGRESS 3/3', flush=True); print(%r, flush=True)\n") % (sys.executable, MIX_SUMMARY)


def executable(path, text):
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, 0o755)
    return path


os.makedirs(os.path.join(S, "bin"))
os.makedirs(os.path.join(S, "data"))
fit_stub = executable(os.path.join(S, "producer-python"), FIT_STUB)
executable(os.path.join(S, "bin", "python3"), MIX_STUB)
executable(os.path.join(S, "bin", "ffmpeg"), "#!/bin/sh\nexit 0\n")
songs = {}
for name, pcm_len in (("song_a.wav", 16000), ("song_b.wav", 8000), ("song_c.wav", 24000)):
    path = os.path.join(S, name)
    pcm = bytes(pcm_len)
    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, 8000, 16000, 2, 16)
                + b"data" + struct.pack("<I", len(pcm)) + pcm)
    songs[name] = path
port = free_port()
cfg = os.path.join(S, "config.json")
with open(cfg, "w") as f:
    json.dump({"title": "producer ui", "port": port, "bind": "127.0.0.1",
               "lanes": [{"id": "cpu", "name": "This machine", "kind": "process", "caps": ["producer"]}],
               "timing": {"poll_seconds": 0.5, "job_poll_seconds": 1.0}}, f)
env = dict(os.environ, GENCENTER_CONFIG=cfg, GENCENTER_DATA=os.path.join(S, "data"),
           BWF_PRODUCER_PYTHON=fit_stub, BWF_UI_ARGV=ARGV_FILE,
           PATH=os.path.join(S, "bin") + os.pathsep + os.environ.get("PATH", ""))
ui.PROCS.append(subprocess.Popen([sys.executable, os.path.join(REPO, "server.py")], cwd=REPO, env=env,
                                 stdout=open(os.path.join(S, "server.log"), "w"), stderr=subprocess.STDOUT))
url = "http://127.0.0.1:%d/" % port


def argv_of():
    with open(ARGV_FILE) as f:
        return json.load(f)


SHOTS_DIR = os.environ.get("TMPDIR", tempfile.gettempdir())
try:
    wait_up(url + "api/health")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        def pick_mode(label):
            chip = page.locator("#engineChipBtn")
            chip.click()
            page.locator("#enginePicker").get_by_text(label, exact=False).first.click()

        def wait_summary(text, timeout_ms=45000):
            summary = page.locator("#jobSummary")
            deadline = time.time() + timeout_ms / 1000
            while time.time() < deadline:
                if summary.count() and summary.text_content() == text:
                    return summary
                page.wait_for_timeout(200)
            return summary

        page.goto(url + "?t=%f" % time.time())
        page.wait_for_selector('#roomStrip [data-room-id="producer"]', timeout=15000)
        page.click('#roomStrip [data-room-id="producer"]')

        # ---- Flow 1: Fit a part onto another beat
        pick_mode("Fit a part onto another beat")
        page.locator("#upload_source_audio_name").set_input_files(songs["song_a.wav"])
        page.locator("#upload_part_audio_name").set_input_files(songs["song_b.wav"])
        page.locator("#upload_target_audio_name").set_input_files(songs["song_c.wav"])
        page.wait_for_timeout(1500)
        page.get_by_role("button", name="Make", exact=True).first.click()
        summary = wait_summary(FIT_SUMMARY)
        check("the finished Fit shows its summary on the page",
              summary.count() == 1 and summary.text_content() == FIT_SUMMARY,
              summary.text_content() if summary.count() else page.locator("#monitor").inner_text()[:300])
        check("... and a Download for fit.json",
              page.locator("#jobExtras a", has_text="fit.json").count() == 1)
        check("... and a Download for fitted.wav (the file the next step, Mix, feeds on)",
              page.locator("#jobExtras a", has_text="fitted.wav").count() == 1)
        download_check(page, "fetched, the fitted.wav Download answers 200 with the Fit's frames intact (wave module)",
                       "fitted.wav", FIT_WAV, FIT_FRAMES)
        check("the preview still plays on the stage", page.locator("#monitor audio").count() == 1)
        try:
            argv = argv_of()
        except Exception as e:
            argv = []
            check("the Fit stand-in recorded its argv", False, repr(e))
        vals = {}
        for flag in ("--source", "--part", "--target"):
            if flag in argv:
                vals[flag] = argv[argv.index(flag) + 1]
        check("Fit was given --source, --part and --target",
              set(vals) == {"--source", "--part", "--target"}, vals)
        check("those are three DIFFERENT staged files",
              len(set(vals.values())) == 3 and all(os.path.isfile(v) for v in vals.values()), vals)
        check("and their basenames are the three uploads, in the right fields",
              vals.get("--source", "").endswith("song_a.wav")
              and vals.get("--part", "").endswith("song_b.wav")
              and vals.get("--target", "").endswith("song_c.wav"), vals)
        fit_shot = os.path.join(SHOTS_DIR, "bwf-producer-fit-result.png")
        page.screenshot(path=fit_shot)
        print("screenshot: %s" % fit_shot)

        # ---- Flow 2: Mix tracks
        pick_mode("Mix tracks")
        page.locator("#upload_track_1").set_input_files(songs["song_a.wav"])
        page.locator("#upload_track_2").set_input_files(songs["song_b.wav"])
        page.wait_for_timeout(1500)
        page.locator("#advancedDisclosure summary").click()
        page.locator("#f_gain_2").fill("-3")
        page.locator("#f_offset_2").fill("1.5")
        page.wait_for_timeout(300)
        page.get_by_role("button", name="Make", exact=True).first.click()
        summary = wait_summary(MIX_SUMMARY)
        check("the finished Mix shows its summary on the page",
              summary.count() == 1 and summary.text_content() == MIX_SUMMARY,
              summary.text_content() if summary.count() else page.locator("#monitor").inner_text()[:300])
        check("... and a Download for mix.json",
              page.locator("#jobExtras a", has_text="mix.json").count() == 1)
        check("... and a Download for mix.wav (the finished mix as a wave file)",
              page.locator("#jobExtras a", has_text="mix.wav").count() == 1)
        download_check(page, "fetched, the mix.wav Download answers 200 with the Mix's frames intact (wave module)",
                       "mix.wav", MIX_WAV, MIX_FRAMES)
        check("the mix still plays on the stage", page.locator("#monitor audio").count() == 1)
        try:
            argv = argv_of()
        except Exception as e:
            argv = []
            check("the Mix stand-in recorded its argv", False, repr(e))
        seq = []
        for i, a in enumerate(argv):  # the recorded argv holds mix.py's full path, not its name
            if a.endswith("mix.py"):
                seq = argv[i + 1:]
                break

        got, want = None, ["--track", "song_a.wav", "--gain", "0", "--offset", "0",
                           "--track", "song_b.wav", "--gain", "-3", "--offset", "1.5"]
        if seq.count("--track") >= 2 and seq.count("--gain") >= 2 and seq.count("--offset") >= 2:
            i = seq.index("--track")
            slice_ = seq[i:i + 12]
            ok = (len(slice_) == 12
                  and slice_[0::2] == want[0::2]
                  and os.path.basename(slice_[1]).endswith("song_a.wav") and slice_[3] in ("0", "0.0")
                  and slice_[5] in ("0", "0.0")
                  and os.path.basename(slice_[7]).endswith("song_b.wav") and slice_[9] in ("-3", "-3.0")
                  and slice_[11] in ("1.5", "1.50", "1.500"))
            got = slice_
        check("the command line lays the tracks out in order with the gains and starts",
              ok, got)
        check("and it levels to -14 LUFS",
              "--lufs" in seq and float(seq[seq.index("--lufs") + 1]) == -14.0,
              seq)
        mix_shot = os.path.join(SHOTS_DIR, "bwf-producer-mix-result.png")
        page.screenshot(path=mix_shot)
        print("screenshot: %s" % mix_shot)

        check("no page errors", not errors, errors)
        browser.close()
finally:
    ui.finish()
