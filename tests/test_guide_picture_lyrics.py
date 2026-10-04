"""Gate for "Write from a picture" in the Sound rooms, in process: with a
picture attached, POST /api/guide/skill keeps the mode's OWN writer when that
writer fills a `lyrics` field (the song writer writes the song FROM the
picture), while every other mode still gets the generic writer ("Describe
this picture"). Same setup as tests/test_picture_skill.py (tests/_picture_server
building blocks) but with the fake helper configured `"vision": true`, so the
picture really goes as an image part.

Run: python3 tests/test_guide_picture_lyrics.py
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import _scratch_config  # noqa: E402,F401
import _picture_server as ps  # noqa: E402
from engines import audio  # noqa: E402
from _picture_server import FAILED, GOOD, check  # noqa: E402

# A valid song-writer reply: a voice named, lyrics short enough for 30 s.
SONG_REPLY = ("TAGS: warm female vocals, folk, singing\nBPM: NONE\nKEY: NONE\nDURATION: 30\n"
              "TIMESIG: NONE\nLANGUAGE: NONE\nLYRICS:\n[Verse]\nline one\nline two\n")


def start(name):
    """Like tests/_picture_server.start, but the helper sees pictures:
    "helper": {"vision": true}."""
    helper = ThreadingHTTPServer(("127.0.0.1", 0), ps.FakeHelper)
    threading.Thread(target=helper.serve_forever, daemon=True).start()
    scratch = tempfile.mkdtemp(prefix="bwf_%s_" % name)
    store = os.path.join(scratch, "fake_lane")
    os.makedirs(os.path.join(store, "inputs"), exist_ok=True)
    # The upload _guide_picture_url reads: a comfy lane's /view?type=input
    # serves <store>/inputs/<name>, which is where POST /api/upload puts files.
    with open(os.path.join(store, "inputs", "mood.png"), "wb") as f:
        f.write(ps.PNG)
    lane_port = ps.free_port()
    lane = subprocess.Popen([sys.executable, os.path.join(HERE, "fixtures", "fake_comfy.py"), "--port",
                             str(lane_port), "--store", store], cwd=os.path.dirname(HERE),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/system_stats" % lane_port, timeout=1)
            break
        except Exception:
            time.sleep(0.1)
    cfg_path = os.path.join(scratch, "config.json")
    with open(cfg_path, "w") as f:
        json.dump({"port": ps.free_port(), "bind": "127.0.0.1", "title": name,
                   "timing": {"poll_seconds": 30, "job_poll_seconds": 30},
                   "lanes": [{"id": "t", "name": "Test lane", "host": "127.0.0.1", "port": lane_port,
                              "caps": ["image", "video", "audio"]}],
                   "helper": {"url": "http://127.0.0.1:%d/v1" % helper.server_address[1],
                              "model": "test-model", "timeout_s": 5, "vision": True}}, f)
    os.environ["GENCENTER_CONFIG"] = cfg_path
    os.environ["GENCENTER_DATA"] = os.path.join(scratch, "data")
    spec = importlib.util.spec_from_file_location("srv_" + name,
                                                  os.path.join(os.path.dirname(HERE), "server.py"))
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    return srv, lane


REQ = ps.HELPER_STATE["requests"]


def skill(body, *replies):
    REQ.clear()
    ps.HELPER_STATE["replies"] = list(replies)
    return srv.guide_skill(body)


def system(i=0):
    return REQ[i]["messages"][0]["content"] if len(REQ) > i else ""


def user(i=0):
    return REQ[i]["messages"][1]["content"] if len(REQ) > i else ""


srv, LANE = start("guide_picture_lyrics")
PIC = {"lane": "t", "upload": "mood.png"}
try:
    print("the song writer keeps a picture: it writes the song FROM it")
    b, code = skill({"room": "music", "mode": "song", "pictures": [PIC]}, SONG_REPLY)
    check("song + picture: 200 with the lyrics field filled",
          code == 200 and b.get("ok") and (b.get("fields") or {}).get("lyrics", "").startswith("[Verse]"),
          (code, b))
    check("song + picture: the song writer's own prompt, not the generic writer's",
          system().startswith(audio.SONG_WRITER_PROMPT), system()[:90])
    parts = user()
    check("song + picture: the user message carries the picture as an image part",
          isinstance(parts, list) and any(p.get("type") == "image_url" for p in parts), parts if not isinstance(parts, list) else [p.get("type") for p in parts])
    text = parts[0]["text"] if isinstance(parts, list) else parts
    check("song + picture: asked for the song's mood, not a caption",
          "do not describe the picture line by line" in text, text[:200])
    check("song + picture: grounded on the one picture it was sent",
          text.endswith("[1 picture attached.]"), text[-60:])
    b2, _ = skill({"room": "music", "mode": "song", "topic": "a lighthouse in a storm", "pictures": [PIC]},
                  SONG_REPLY)
    t2 = user()[0]["text"] if isinstance(user(), list) else user()
    check("song + picture: the user's own words ride along",
          "The user's own words so far: a lighthouse in a storm" in t2, t2[:260])

    print("every other mode keeps today's behaviour: the generic writer describes the picture")
    b, code = skill({"room": "picture", "mode": "t2i", "pictures": [PIC]},
                    "NEGATIVE: NONE\nNOTE: a film still.\nPROMPT: " + GOOD)
    check("t2i + picture: 200, the generic writer's prompt",
          code == 200 and not system().startswith(audio.SONG_WRITER_PROMPT)
          and (b.get("fields") or {}).get("prompt") == GOOD, (code, system()[:90]))
    t3 = user()[0]["text"] if isinstance(user(), list) else user()
    check("t2i + picture: still asked to describe it", "Describe the attached picture" in t3, t3[:200])
finally:
    LANE.terminate()

print("\nFAILED: %d" % len(FAILED) + (" checks: " + ", ".join(FAILED) if FAILED else ""))
sys.exit(1 if FAILED else 0)
