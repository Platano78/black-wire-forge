"""Acceptance gate for the local output store (a `post` step's result,
type "local"): path traversal on /api/view, a raising `post` failing its
job cleanly, and sanitize.strip_metadata applying to a local output exactly
as it does to a proxied one.

Run: python3 tests/test_local_outputs.py
"""
import importlib.util, os, shutil, sys, types
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + detail) if not cond and detail else ""))
    if not cond: FAILED.append(name)

import engines
import _scratch_config  # noqa: E402 -- must run before server.py's own exec_module below

spec = importlib.util.spec_from_file_location("srv_local", os.path.join(ROOT, "server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)

# A scratch LOCAL_OUTPUTS_DIR of our own, so this test never touches the real
# data/outputs/ tree and cleans up after itself regardless of outcome.
SCRATCH = os.path.join(HERE, "_scratch_outputs")
shutil.rmtree(SCRATCH, ignore_errors=True)
os.makedirs(os.path.join(SCRATCH, "job1"), exist_ok=True)
with open(os.path.join(SCRATCH, "job1", "sprite.png"), "wb") as f:
    f.write(b"\x89PNG\r\n\x1a\nnot a real png but bytes are bytes")
srv.LOCAL_OUTPUTS_DIR = SCRATCH
# run_post_step() ends in save_jobs(): without these it rewrote the REAL
# data/jobs.json with this test's in-memory jobs (observed 2026-09-22).
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


print("path traversal is refused on /api/view local outputs (RED then GREEN)")


def _unguarded_path(job_id, filename):
    # RED: the naive join this code must NOT use -- reproduced inline, never
    # re-imported, purely to prove the attack actually escapes the directory.
    return os.path.join(SCRATCH, job_id, filename)


escape_job, escape_file = "job1", "../../../../etc/passwd"
naive = os.path.realpath(_unguarded_path(escape_job, escape_file))
check("RED: the naive join actually escapes SCRATCH (the attack this guards against)",
      not naive.startswith(os.path.realpath(SCRATCH) + os.sep), naive)

kind, payload, code = call_view({"job": [escape_job], "filename": [escape_file]})
# local_output_path() basename()s the filename BEFORE resolving it, so the
# traversal in "../../../../etc/passwd" never reaches realpath at all --
# it looks for SCRATCH/job1/passwd (absent) and refuses with 404, not the
# job-id case's 400. Either way nothing outside SCRATCH is ever opened.
check("GREEN: the real handler refuses the escaping filename (never serves it)",
      kind == "json" and code in (400, 404), str((kind, payload, code)))

kind2, payload2, code2 = call_view({"job": ["../job1"], "filename": ["sprite.png"]})
check("GREEN: a traversal in the job id alone is also refused",
      kind2 == "json" and code2 == 400, str((kind2, payload2, code2)))

kind3, data3, ctype3 = call_view({"job": ["job1"], "filename": ["sprite.png"]})
check("a legitimate job/filename is served",
      kind3 == "blob" and data3 == open(os.path.join(SCRATCH, "job1", "sprite.png"), "rb").read(),
      str((kind3, ctype3)))

if os.name == "posix":
    os.makedirs(os.path.join(SCRATCH, "job2"), exist_ok=True)
    link = os.path.join(SCRATCH, "job2", "evil.png")
    try:
        os.symlink("/etc/hostname", link)
        kind4, payload4, code4 = call_view({"job": ["job2"], "filename": ["evil.png"]})
        check("GREEN: a symlink escaping SCRATCH is also refused",
              kind4 == "json" and code4 == 400, str((kind4, payload4, code4)))
    finally:
        if os.path.islink(link):
            os.unlink(link)


print()
print("the page's own /api/view shape (job id as `subfolder`, no `job`) works")

# index.html's viewURL() builds /api/view?lane=..&filename=..&subfolder=..&type=local
# and NEVER sends `job`; the job id travels in `subfolder`, which is where
# run_post_step() (and run_process_job()) store it. proxy_view_local() must
# accept the id from `job`, else `subfolder`.
page_shape = {"lane": ["pixelart"], "filename": ["sprite.png"],
              "subfolder": ["job1"], "type": ["local"]}
kind5, data5, ctype5 = call_view(page_shape)
check("the page shape (subfolder=job id, no job) serves the file",
      kind5 == "blob" and data5 == open(os.path.join(SCRATCH, "job1", "sprite.png"), "rb").read(),
      str((kind5, ctype5)))

for bad_sub in ("../job1", "/etc/passwd"):
    q = {"lane": ["pixelart"], "filename": ["sprite.png"],
         "subfolder": [bad_sub], "type": ["local"]}
    kind6, payload6, code6 = call_view(q)
    check("a %r subfolder is refused exactly like a bad job id" % bad_sub,
          kind6 == "json" and code6 == 400, str((kind6, payload6, code6)))

print()
print("write-side traversal -- a pack's `post` return value -- is refused (RED then GREEN)")


def _unguarded_write(job_id, out_name):
    # RED: the shape run_post_step had BEFORE this fix (out_name used as-is,
    # never basename()'d) -- reproduced inline, never re-imported, purely to
    # prove the escape a buggy or third-party pack's `post` could cause.
    job_dir = os.path.join(SCRATCH, job_id)
    return os.path.join(job_dir, out_name)


rel_escape_target = os.path.realpath(os.path.join(SCRATCH, "escape_rel.png"))
esc_rel = os.path.realpath(_unguarded_write("job_red_rel", "../escape_rel.png"))
check("RED: a relative '../' out_name escapes its own job directory",
      esc_rel == rel_escape_target, esc_rel)

abs_escape_target = "/tmp/bwf_escape_abs_%d.png" % os.getpid()
esc_abs = _unguarded_write("job_red_abs", abs_escape_target)
check("RED: an absolute out_name discards the job directory entirely (os.path.join's own semantics)",
      esc_abs == abs_escape_target, esc_abs)


def _fake_post_factory(name):
    def _fn(data, filename, args):
        return b"escaped bytes", name
    return _fn


write_pack = types.ModuleType("engines._write_escape_fake")
write_pack.ENGINE = {
    "id": "zzz-write-escape", "cap": "vector",
    "roles": {"esc_unet": ("unet", {"all": ["esc"]})},
    "provides": {"escrel": ["esc_unet"], "escabs": ["esc_unet"]},
    "words": {"esc_unet": "the escape model"},
    "graphs": {"escrel": lambda p, m: {"1": {"class_type": "X", "inputs": {}}},
               "escabs": lambda p, m: {"1": {"class_type": "X", "inputs": {}}}},
    "post": {"escrel": _fake_post_factory("../escape_rel.png"),
             "escabs": _fake_post_factory(abs_escape_target)},
    "describe": lambda m: "",
    "licence": {"name": "TEST", "shippable": True, "attribution": "Test"},
}
sys.modules["engines._write_escape_fake"] = write_pack
engines._PACKS = None
engines.packs.__globals__["_PACKS"] = engines.packs() + [write_pack.ENGINE]
srv.engines._PACKS = engines.packs.__globals__["_PACKS"]

write_lane = {"id": "writelane", "name": "Write Lane", "box": "b", "note": "", "host": "127.0.0.1", "port": 1}
srv.http_get_bytes = lambda url, timeout=60.0: (b"fake render bytes", "image/png")

for mode, escape_target in (("escrel", rel_escape_target), ("escabs", abs_escape_target)):
    if os.path.exists(escape_target):
        os.remove(escape_target)
    job_id = "job_green_%s" % mode
    job = {"id": job_id, "kind": "vector", "mode": mode,
           "outputs": [{"filename": "render.png", "subfolder": "", "type": "output"}],
           "status": "done", "args": {}}
    with srv.JOBS_LOCK:
        srv.JOBS[job_id] = job
    raised = False
    try:
        srv.run_post_step(write_lane, job)
    except Exception:
        raised = True
    check("GREEN %s: run_post_step never lets the escaping filename raise out" % mode, not raised)
    with srv.JOBS_LOCK:
        after = dict(srv.JOBS.get(job_id) or {})
    check("GREEN %s: nothing was written outside SCRATCH" % mode,
          not os.path.exists(escape_target), escape_target)
    # basename() reduces "../escape_rel.png"/an absolute path down to a plain
    # filename ("escape_rel.png"), which is neither empty nor "."/".." --
    # that is not itself refused; it is simply written INSIDE the job's own
    # directory under that name, same as any other filename would be. The
    # security property this gate proves is "never outside SCRATCH", checked
    # above -- containment, not a mandatory refusal for every odd character.
    local_outs = [o for o in after.get("outputs", []) if o.get("type") == "local"]
    check("GREEN %s: the local output was written safely inside its own job directory" % mode,
          len(local_outs) == 1 and os.path.isfile(
              os.path.join(SCRATCH, job_id, local_outs[0]["filename"])), str(after))
    if os.path.exists(escape_target):
        os.remove(escape_target)   # never leave a real escape behind, pass or fail


print()
print("an out_name that basename() reduces to '', '.' or '..' is refused outright, job failed")
for bad_name in ("..", ".", ""):
    job_id = "job_dotdot_%d" % (hash(bad_name) & 0xffff)
    job = {"id": job_id, "kind": "vector", "mode": "escrel",
           "outputs": [{"filename": "render.png", "subfolder": "", "type": "output"}],
           "status": "done", "args": {}}
    write_pack.ENGINE["post"]["escrel"] = _fake_post_factory(bad_name)
    with srv.JOBS_LOCK:
        srv.JOBS[job_id] = job
    srv.run_post_step(write_lane, job)
    with srv.JOBS_LOCK:
        after = dict(srv.JOBS.get(job_id) or {})
    check("out_name %r: the job fails cleanly with a plain message" % bad_name,
          after.get("status") == "error" and "not allowed" in after.get("error", ""), str(after))


print()
print("a raising `post` fails the job cleanly, never crashes the caller")
boom_pack = types.ModuleType("engines._boom_post_fake")


def _boom_post(data, filename, args):
    raise ValueError("this post step always fails, on purpose")


boom_pack.ENGINE = {
    "id": "zzz-boom-post", "cap": "vector",
    "roles": {"boom_unet": ("unet", {"all": ["boom"]})},
    "provides": {"boompost": ["boom_unet"]},
    "words": {"boom_unet": "the boom model"},
    "graphs": {"boompost": lambda p, m: {"1": {"class_type": "X", "inputs": {}}}},
    "post": {"boompost": _boom_post},
    "describe": lambda m: "",
    "licence": {"name": "TEST", "shippable": True, "attribution": "Boom Co"},
}
sys.modules["engines._boom_post_fake"] = boom_pack
engines._PACKS = None
engines.packs.__globals__["_PACKS"] = engines.packs() + [boom_pack.ENGINE]
srv.engines._PACKS = engines.packs.__globals__["_PACKS"]

lane = {"id": "boomlane", "name": "Boom Lane", "box": "b", "note": "", "host": "127.0.0.1", "port": 1}
srv.http_get_bytes = lambda url, timeout=60.0: (b"fake render bytes", "image/png")
job = {"id": "boomjob", "kind": "vector", "mode": "boompost",
       "outputs": [{"filename": "render.png", "subfolder": "", "type": "output"}],
       "status": "done", "args": {}}
with srv.JOBS_LOCK:
    srv.JOBS[job["id"]] = job

raised = False
try:
    srv.run_post_step(lane, job)
except Exception:
    raised = True
check("run_post_step never lets the pack's exception escape", not raised)
with srv.JOBS_LOCK:
    after = dict(srv.JOBS.get("boomjob") or {})
check("the job is marked failed", after.get("status") == "error", str(after))
check("the failure message is the exception's own plain sentence",
      after.get("error") == "this post step always fails, on purpose", str(after))


print()
print("a local output is sanitized on dl=1 exactly as a proxied one is")
try:
    from PIL import Image
    from PIL.PngImagePlugin import PngInfo
except ImportError:
    # Finding #21: Pillow is an optional dep of the app itself -- its
    # absence on a clean clone SKIPs just this PNG-specific section, not
    # the whole suite (everything above this point needs no PIL at all).
    print("  SKIP  Pillow is not installed -- this section needs it")
else:
    import io
    im = Image.new("RGB", (4, 4), (10, 20, 30))
    meta_png = io.BytesIO()
    # Embed a text chunk the same way ComfyUI's own PNGs carry their workflow.
    pnginfo = PngInfo()
    pnginfo.add_text("prompt", '{"1": {"class_type": "Secret"}}')
    im.save(meta_png, format="PNG", pnginfo=pnginfo)
    job3dir = os.path.join(SCRATCH, "job3")
    os.makedirs(job3dir, exist_ok=True)
    with open(os.path.join(job3dir, "sprite.png"), "wb") as f:
        f.write(meta_png.getvalue())

    _, view_data, _ = call_view({"job": ["job3"], "filename": ["sprite.png"], "dl": ["0"]})
    check("without dl=1, the metadata is untouched",
          b"Secret" in view_data)
    _, dl_data, _ = call_view({"job": ["job3"], "filename": ["sprite.png"], "dl": ["1"]})
    check("with dl=1, the local output is sanitized (the workflow chunk is gone)",
          b"Secret" not in dl_data)
    # .tobytes() over .getdata() (removed in Pillow 14, 2027-10-15, per
    # Pillow's own DeprecationWarning) -- exact byte-for-byte pixel compare.
    check("with dl=1, the pixels survive the strip",
          Image.open(io.BytesIO(dl_data)).convert("RGB").tobytes() == im.tobytes())

shutil.rmtree(SCRATCH, ignore_errors=True)

print()
print(("FAILED: %d" % len(FAILED)) if FAILED else "ALL PASS")
sys.exit(1 if FAILED else 0)
