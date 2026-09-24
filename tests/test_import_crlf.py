"""Acceptance gate for F3 (small-fixes recipe): import_script must normalise
Windows (\\r\\n) and old-Mac (\\r) line endings to \\n before splitting on
blank lines, so a script pasted/uploaded from Windows imports the same beats
and kinds as its LF equivalent -- not one giant paragraph (the blank-line
split regex is \\n[ \\t]*\\n, which \\r\\n\\r\\n never matches).

RED on the pre-fix tree: the CRLF text collapses to a single beat instead of
five.

Run: python3 tests/test_import_crlf.py
"""
import importlib.util, json, os, socket, sys, tempfile
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail != "" else ""))
    if not cond:
        FAILED.append(name)

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    return port

SCRATCH = tempfile.mkdtemp(prefix="bwf_crlf_")
CONFIG = os.path.join(SCRATCH, "config.json")
json.dump({"port": free_port(), "bind": "127.0.0.1", "title": "f3 test",
           "timing": {"poll_seconds": 30, "job_poll_seconds": 30, "http_timeout": 2.0},
           "lanes": [{"id": "t", "name": "Test lane", "host": "127.0.0.1", "port": free_port(),
                      "caps": ["image", "video", "audio"]}]}, open(CONFIG, "w"))
os.environ["GENCENTER_CONFIG"] = CONFIG
os.environ["GENCENTER_DATA"] = os.path.join(SCRATCH, "data")

spec = importlib.util.spec_from_file_location("srv_crlf", os.path.join(ROOT, "server.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod.JOBS_FILE = os.path.join(SCRATCH, "data", "jobs.json")
mod.SEQ_DIR = os.path.join(SCRATCH, "data", "sequences")
mod.SEQ_MEDIA_DIR = os.path.join(SCRATCH, "data", "seq")
mod.CHAIN_DIR = os.path.join(SCRATCH, "data", "chain")
mod.LOCAL_OUTPUTS_DIR = os.path.join(SCRATCH, "data", "outputs")
for d in (mod.SEQ_DIR, mod.SEQ_MEDIA_DIR, mod.CHAIN_DIR, mod.LOCAL_OUTPUTS_DIR):
    os.makedirs(d, exist_ok=True)

# 2 sluglines, 3 paragraphs (5 beats total).
BEATS = [
    ("scene", "EXT. COASTAL ROAD - DAY."),
    ("film", "A red vintage sports car keeps driving along a coastal road at a steady speed."),
    ("scene", "INT. LUNAR APARTMENT - NIGHT."),
    ("film", "The target video pushes slowly into the subject as tea is offered to someone off screen."),
    ("film", "The target video holds a close-up as the subject refuses to look at someone off screen."),
]
LF_TEXT = "\n\n".join(text for _, text in BEATS)
CRLF_TEXT = LF_TEXT.replace("\n", "\r\n")
EXPECTED_KINDS = [k for k, _ in BEATS]
EXPECTED_TEXTS = [t for _, t in BEATS]


def import_into_fresh_storyboard(text, title):
    seq, _ = mod.seq_create({"title": title, "mode": "storyboard"})
    sid, rev = seq["id"], seq["rev"]
    b, c = mod.seq_op({"id": sid, "rev": rev, "op": "import_script", "text": text})
    return b, c


print("import_script normalises CRLF to LF before splitting on blank lines")
b_lf, c_lf = import_into_fresh_storyboard(LF_TEXT, "crlf-lf")
check("LF import -> 200", c_lf == 200, b_lf)
lf_kinds = [x.get("kind") for x in (b_lf.get("beats") or [])]
lf_texts = [x.get("text") for x in (b_lf.get("beats") or [])]
check("LF import: %d beats" % len(BEATS), len(lf_texts) == len(BEATS), lf_texts)
check("LF import: kinds match", lf_kinds == EXPECTED_KINDS, lf_kinds)
check("LF import: texts match", lf_texts == EXPECTED_TEXTS, lf_texts)

b_crlf, c_crlf = import_into_fresh_storyboard(CRLF_TEXT, "crlf-crlf")
check("CRLF import -> 200", c_crlf == 200, b_crlf)
crlf_kinds = [x.get("kind") for x in (b_crlf.get("beats") or [])]
crlf_texts = [x.get("text") for x in (b_crlf.get("beats") or [])]
check("CRLF import: %d beats (same as LF)" % len(BEATS), len(crlf_texts) == len(BEATS), crlf_texts)
check("CRLF import: kinds match the LF version", crlf_kinds == lf_kinds, crlf_kinds)
check("CRLF import: texts match the LF version (no stray \\r left in the text)",
      crlf_texts == lf_texts, crlf_texts)

print()
if FAILED:
    print("FAILED: %d checks: %s" % (len(FAILED), ", ".join(FAILED)))
    sys.exit(1)
print("All F3 CRLF-import checks passed.")
