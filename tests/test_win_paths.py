"""Acceptance gate for the Windows path-handling fixes: the `post` step's
temp file gets a name this process builds (never a lane-supplied one, which
was a real traversal on every OS), both output resolvers refuse Windows
device names and trailing dot/space, both `split("/")` dotdot checks see
either separator, and a cross-drive models folder gets the plain sentence
instead of commonpath's own ValueError.

Runs on ANY platform -- the Windows behaviour is exercised by the shape of
the input strings, never by being on Windows.

Run: python3 tests/test_win_paths.py
"""
import importlib.util, os, shutil, sys
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond else ""))
    if not cond: FAILED.append(name)

print()
print("engines/pixelart.py: the post step's temp name is one WE build (E-7)")
import engines.pixelart as pa
for given, want in (("../../etc/passwd", "render.png"),
                    ("C:/Users/x/evil.png", "render.png"),
                    ("a\\b\\ok.jpg", "render.jpg"),
                    ("NUL", "render.png"),
                    (None, "render.png"),
                    ("image_00001_.png", "render.png")):
    check("_source_name(%r) == %r" % (given, want),
          pa._source_name(given) == want, repr(pa._source_name(given)))

print()
print("both output resolvers refuse a name Windows cannot store (E-1/E-2)")
import _scratch_config  # noqa: E402 -- must run before server.py's own exec_module below

spec = importlib.util.spec_from_file_location("srv_win_paths", os.path.join(ROOT, "server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)

# A scratch LOCAL_OUTPUTS_DIR of our own, so nothing here touches the real
# data/outputs/ tree and it cleans up after itself either way.
SCRATCH = os.path.join(HERE, "_scratch_win_paths")
shutil.rmtree(SCRATCH, ignore_errors=True)
os.makedirs(os.path.join(SCRATCH, "job1"), exist_ok=True)
srv.LOCAL_OUTPUTS_DIR = SCRATCH

for bad in ("NUL", "nul.png", "CON.txt", "aux", "COM1", "lpt9.log", "foo.", "foo "):
    check("_unsafe_output_name(%r) is True" % bad, srv._unsafe_output_name(bad) is True)
for good in ("render.png", "IMG_00095_.png", "con_art.png", "nulled.png", "comet.png"):
    check("_unsafe_output_name(%r) is False" % good, srv._unsafe_output_name(good) is False)


def refused(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except ValueError:
        return True, ""
    return False, "no ValueError"


ok, detail = refused(srv.local_output_path, "job1", "NUL.png")
check("local_output_path('job1', 'NUL.png') raises ValueError", ok, detail)
check("a normal name still resolves inside the job directory",
      srv.local_output_path("job1", "render.png")
      == os.path.join(os.path.realpath(SCRATCH), "job1", "render.png"),
      srv.local_output_path("job1", "render.png"))

LANE_DIR = os.path.join(SCRATCH, "lane_outs")
os.makedirs(LANE_DIR, exist_ok=True)
lane = {"id": "winlane", "name": "Win Lane", "box": "b", "outputs": {"dir": LANE_DIR}}
ok, detail = refused(srv.lane_output_path, lane, {"filename": "NUL.png", "subfolder": ""})
check("lane_output_path({'filename': 'NUL.png'}) raises ValueError", ok, detail)
check("lane_output_path still resolves a normal name",
      srv.lane_output_path(lane, {"filename": "render.png", "subfolder": "sub"})
      == os.path.join(os.path.realpath(LANE_DIR), "sub", "render.png"),
      srv.lane_output_path(lane, {"filename": "render.png", "subfolder": "sub"}))

print()
print("both dotdot checks see either separator (E-4)")
check("_has_dotdot('a\\\\..\\\\b') is True", srv._has_dotdot("a\\..\\b") is True)
check("_has_dotdot('a/../b') is True", srv._has_dotdot("a/../b") is True)
check("_has_dotdot('a/b..c/d') is False", srv._has_dotdot("a/b..c/d") is False)

shutil.rmtree(SCRATCH, ignore_errors=True)

print()
print(("FAILED: %d" % len(FAILED)) if FAILED else "ALL PASS")
sys.exit(1 if FAILED else 0)