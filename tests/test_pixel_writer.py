"""Gate for P3a, Pixel Art that looks like pixel art: the pack's sprite writer
(parse, fields, check, prompt rules), the guide text that replaced "keep this
about the subject, not the pixel style", the "Try this" example, and the
post step's output being the job's RESULT (marked "post": true, backfilled
on jobs saved before the mark, harvested for a sequence, and named on
/api/engines). No browser: tests/test_pixel_stage_ui.py covers the page.

Run: python3 tests/test_pixel_writer.py
"""
import importlib.util
import io
import json
import os
import shutil
import sys

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FAILED = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail else ""))
    if not cond:
        FAILED.append(name)


import engines  # noqa: E402
import _scratch_config  # noqa: E402,F401 -- must run before server.py's own exec_module below
from engines import pixelart  # noqa: E402

# The prompt of owner job f5931104bae0 (2026-09-24): Godzilla, but not pixel art.
OLD_PROMPT = ("A Monsterverse Godzilla standing in a heroic pose, firing a bright blue energy blast from its "
              "mouth, thick dorsal fins glowing with intense light, cinematic lighting, scales rendered in deep "
              "charcoal and slate grey")
# A live reply from the 12B helper, trial 2026-09-24 ("a 32px green frog jumping, 4 colours").
LIVE_REPLY = ("PIXEL_SIZE: 32\nPIXEL_COLORS: 4\nNOTE: I chose a side view.\n"
              "PROMPT: Pixel art sprite, retro video game style, a green frog, a small amphibian with long hind "
              "legs, jumping forward through the air, full body from head to feet in side view with empty space "
              "all around it, vibrant green skin, pale yellow belly, flat colours in a few large shapes, a thick "
              "dark outline, plain flat white background.")

print("the pack declares a sprite writer on P2's contract")
w = engines.writer("image", "pixelart")
check("writer exists for image/pixelart", bool(w))
check("line keys fill prompt, pixel_size and pixel_colors",
      w and w["keys"] == {"PIXEL_SIZE": "pixel_size", "PIXEL_COLORS": "pixel_colors", "PROMPT": "prompt"}, w and w["keys"])
check("PROMPT is the multiline key, NONE the empty word",
      w and w["multiline"] == "PROMPT" and w["none_token"] == "NONE")
check("the writer carries a check", w and callable(w.get("check")))

print("the writer's prompt holds each sprite rule")
P = pixelart.SPRITE_WRITER_PROMPT
for label, needle in (("pixel-art wording", "Pixel art sprite, retro video game style"),
                      ("whole body in frame", "WHOLE body in frame"),
                      ("side or three-quarter view", "three-quarter view"),
                      ("bold silhouette", "silhouette"),
                      ("flat colours + thick dark outline", "thick dark outline"),
                      ("plain flat background", "plain flat white background"),
                      ("no cinematic / photo words", "NEVER write: cinematic"),
                      ("named subject by its appearance", "keep the name AND describe how it looks"),
                      ("asks the pose", "POSE"), ("asks the view", "VIEW"), ("asks the colours", "COLOURS"),
                      ("pixel_size line", "PIXEL_SIZE:"), ("pixel_colors line", "PIXEL_COLORS:")):
    check("prompt: " + label, needle in P)
check("prompt: its worked examples avoid the trial's topics (godzilla, knight, dragon, cat, spaceship)",
      not any(t in P.lower() for t in ("godzilla", "knight", "dragon", "cat,", "spaceship")))

print("parse: a live reply and a question")
parsed = engines.parse_writer_reply(w, LIVE_REPLY)
check("live reply parses to prompt + sizes",
      parsed.get("values", {}).get("pixel_size") == "32" and parsed["values"].get("pixel_colors") == "4"
      and parsed["values"]["prompt"].startswith("Pixel art sprite"), parsed)
check("live reply keeps its NOTE", parsed.get("note") == "I chose a side view.", parsed)
q = engines.parse_writer_reply(w, "QUESTION: What should the cat be doing?\nOPTIONS: Sitting | Walking | Pouncing")
check("a question parses with its options", q == {"question": "What should the cat be doing?",
                                                "options": ["Sitting", "Walking", "Pouncing"]}, q)
none = engines.parse_writer_reply(w, "PIXEL_SIZE: NONE\nPIXEL_COLORS: NONE\nPROMPT: Pixel art sprite, a frog.")
check("NONE leaves the size fields out", set(none["values"]) == {"prompt"}, none)

print("check: photo and lighting words are flagged, sprite prompts pass")
old_problems = pixelart.sprite_check({"prompt": OLD_PROMPT, "pixel_size": 64}, {})
check("the old godzilla prompt is flagged once, naming cinematic",
      len(old_problems) == 1 and "cinematic" in old_problems[0] and "64 px" in old_problems[0], old_problems)
check("the sprite size in the sentence follows the field",
      "32 px" in pixelart.sprite_check({"prompt": "a close-up photo of a frog", "pixel_size": 32}, {})[0])
check("a live writer prompt passes", pixelart.sprite_check(parsed["values"] | {"pixel_size": 32}, {}) == [])
check("whole words only: 'photographer' / 'cinema' do not trip it",
      pixelart.sprite_check({"prompt": "Pixel art sprite of a photographer at the cinema"}, {}) == [])

print("guide text: the old advice is gone")
guide = engines.prompt_guide("image", "pixelart")
check("prompt_guide no longer says 'not the pixel style'", "not the pixel style" not in guide, guide)
check("prompt_guide names the sprite rules", all(x in guide for x in ("whole body", "outline", "plain flat background")))
gp = os.path.join(ROOT, "guides", "picture")
blob = "".join(open(os.path.join(gp, f), encoding="utf-8").read() for f in
               ("system-prompt.txt", "system-prompt-verbose.txt", "knowledge.md", "skills.md",
                "guide.json", "picture.persona.json"))
for stale in ("Prompt the SUBJECT only", "subject only, never pixel-style words", "describe the subject only",
              "keep the prompt about the subject only", "prompt the subject only, never the pixel style"):
    check("guides/picture no longer says %r" % stale, stale not in blob)
check("skills.md carries the sprite writer skill", "## Skill 3: Pixel Art sprite writer" in blob)

print("'Try this' follows the writer's rules")
ex = engines.examples("image", "pixelart")[0]["values"]["prompt"]
check("example starts with the pixel-art wording", ex.startswith("Pixel art sprite, retro video game style"), ex)
check("example: whole body, view, outline, plain background",
      all(x in ex for x in ("full body", "three-quarter view", "thick dark outline", "plain flat white background")), ex)
check("example passes the check", pixelart.sprite_check({"prompt": ex}, {}) == [])

print("post step words")
check("pixelart's post step is called the pixel step", engines.post_words("image", "pixelart") == "the pixel step")
check("a mode with no post step has no words", engines.post_words("image", "t2i") is None
      and engines.post_words("image", "upscale") is None)

# ---------------------------------------------------------------------------
spec = importlib.util.spec_from_file_location("srv_pixel_writer", os.path.join(ROOT, "server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)
SCRATCH = os.path.join(HERE, "_scratch_pixel_writer")
shutil.rmtree(SCRATCH, ignore_errors=True)
os.makedirs(SCRATCH)
srv.LOCAL_OUTPUTS_DIR = os.path.join(SCRATCH, "outputs")
srv.JOBS_FILE = os.path.join(SCRATCH, "jobs.json")
srv.SEQ_DIR = os.path.join(SCRATCH, "sequences")

print("the post step's output is marked as the result")
if not (importlib.util.find_spec("PIL") and importlib.util.find_spec("numpy")):
    print("  SKIP  the pixel post step needs Pillow and numpy (requirements.txt), not installed here")
else:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGBA", (256, 256), (200, 40, 40, 255)).save(buf, "PNG")
    srv.http_get_bytes = lambda url, timeout=60.0: (buf.getvalue(), "image/png")
    lane = {"id": "l", "name": "L", "host": "127.0.0.1", "port": 1}
    job = {"id": "pxjob1", "kind": "image", "mode": "pixelart", "status": "done", "args": {"pixel_size": 32},
           "outputs": [{"filename": "PIXELART_00001_.png", "subfolder": "blackwire", "type": "output", "media": "image"}]}
    with srv.JOBS_LOCK:
        srv.JOBS[job["id"]] = job
    srv.run_post_step(lane, job)
    outs = srv.JOBS["pxjob1"]["outputs"]
    check("run_post_step appends ONE local output marked post: true",
          len(outs) == 2 and outs[1].get("post") is True and outs[1]["type"] == "local"
          and not outs[0].get("post"), outs)
    check("result_output() is the sprite", srv.result_output(srv.JOBS["pxjob1"]) is outs[1])
check("result_output() of a job with no post step is its first output",
      srv.result_output({"outputs": [{"filename": "a"}, {"filename": "b"}]}) == {"filename": "a"})
check("result_output() of a job with no outputs is None", srv.result_output({"outputs": []}) is None)

print("a job saved before the mark is backfilled on load")
legacy = {"id": "f5931104bae0", "kind": "image", "mode": "pixelart", "status": "done", "lane": "l",
          "outputs": [{"filename": "PIXELART_00004_.png", "subfolder": "blackwire", "type": "output", "media": "image"},
                      {"filename": "sprite_64x64.png", "subfolder": "f5931104bae0", "type": "local", "media": "image"}]}
plain = {"id": "t2ijob", "kind": "image", "mode": "t2i", "status": "done", "lane": "l",
         "outputs": [{"filename": "a.png", "type": "output"}, {"filename": "b.png", "type": "local"}]}
with open(srv.JOBS_FILE, "w") as f:
    json.dump([legacy, plain], f)
srv.load_jobs()
check("legacy pixel job: its sprite is marked post", srv.JOBS["f5931104bae0"]["outputs"][1].get("post") is True,
      srv.JOBS["f5931104bae0"]["outputs"])
check("legacy pixel job: its render is not", not srv.JOBS["f5931104bae0"]["outputs"][0].get("post"))
check("a mode with no post step is never marked", not any(o.get("post") for o in srv.JOBS["t2ijob"]["outputs"]))

print("/api/engines names each mode's post step")
payload = srv.Handler.engines_payload(srv.Handler.__new__(srv.Handler), {})
modes = {m["id"]: m for m in payload["image"]["modes"]}
check("pixelart: post = {words: 'the pixel step'}", modes["pixelart"].get("post") == {"words": "the pixel step"},
      modes["pixelart"].get("post"))
check("t2i, edit, cutout, upscale: post is None",
      all(modes[m].get("post") is None for m in ("t2i", "edit", "cutout", "upscale") if m in modes))
check("pixelart: the writer is the sprite writer", modes["pixelart"].get("writer") == {"label": "Sprite writer"})

print("a sequence harvests the result, not the render before it")
if "pxjob1" not in srv.JOBS:
    print("  SKIP  needs the pixel post step's job above (Pillow and numpy), not run here")
else:
    srv.SEQ_MEDIA_DIR = os.path.join(SCRATCH, "seq")
    got = []
    srv._carry_source_bytes = lambda j, out, cache_path=None: (got.append(out), b"bytes")[1]
    srv._seq_read = lambda sid: None
    srv.seq_harvest(lane, dict(srv.JOBS["pxjob1"], sequence_id="s1", slot_id="v1"))
    check("seq_harvest copies the post output", got and got[0].get("post") is True, got)

shutil.rmtree(SCRATCH, ignore_errors=True)
print()
print("FAILED: %d" % len(FAILED))
for n in FAILED:
    print("  - " + n)
sys.exit(1 if FAILED else 0)