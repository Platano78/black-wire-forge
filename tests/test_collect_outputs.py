"""Acceptance gate for collect_outputs() (server.py) -- the C3.4b regression
found 2026-09-23 (docs/... diagnosis by c34b-diagnose): ComfyUI's LoadVideo
node echoes the video it just loaded back into a job's own /history
`outputs` dict, tagged `"type": "input"` -- indistinguishable in SHAPE from
a real SaveVideo result (same "images"/animated:true layout), just tagged
differently. collect_outputs() used to scan every node's file-dict lists
with no filter on that `type` field, so a "continue"-mode job (prev_video
set, h3_continue_graph's LoadVideo node "220" runs before SaveVideo node
"92") ended up with job["outputs"] == [<LoadVideo's echoed prev_video>,
<SaveVideo's real result>] -- and every `job["outputs"][0]` call site
(the take harvest, the cable resolver's carry(job, 0, ...), two more reads)
silently served the WRONG file: the shot's own input, not its render.

RED on the pre-fix tree: (a) and (b) below both fail -- the input echo
survives collect_outputs() and outputs[0] is the echo, not the real result.

Run: python3 tests/test_collect_outputs.py
"""
import importlib.util, os, sys
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + detail) if not cond and detail else ""))
    if not cond: FAILED.append(name)

import _scratch_config  # noqa: E402 -- must run before server.py's own exec_module below

spec = importlib.util.spec_from_file_location("srv_collect_outputs", os.path.join(ROOT, "server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)


# ---------------------------------------------------------------------------
# (a) LoadVideo's real shape, taken verbatim from the rig's own /history for
# job 00d23a60-259a-4196-9c9e-408e89e200dc (the failing live job): node "220"
# (LoadVideo, echoing prev_video) recorded BEFORE node "92" (SaveVideo, the
# real result) -- dict insertion order follows execution order, and
# LoadVideo runs near the start of h3_continue_graph.
# ---------------------------------------------------------------------------
print("(a) a LoadVideo input echo ordered before the real SaveVideo output")
hist_a = {
    "outputs": {
        "220": {"images": [{"filename": "CONT_00002_.mp4", "subfolder": "", "type": "input"}],
                "animated": [True]},
        "92": {"images": [{"filename": "CONT_00003_.mp4", "subfolder": "blackwire", "type": "output"}],
               "animated": [True]},
    }
}
outs_a = srv.collect_outputs(hist_a)
check("only the real output survives", len(outs_a) == 1, str(outs_a))
check("outputs[0] is the SaveVideo file, not the LoadVideo echo",
      bool(outs_a) and outs_a[0]["filename"] == "CONT_00003_.mp4", str(outs_a))


# ---------------------------------------------------------------------------
# (b) LoadAudio's shape. NOTE: verified against the rig's own ComfyUI source
# (comfy_extras/nodes_audio.py, class LoadAudio.execute) -- unlike LoadVideo,
# LoadAudio.execute() returns `IO.NodeOutput(audio)` with NO `ui=` kwarg, so
# it does NOT echo an input preview into a real job's history the way
# LoadVideo does. A synthetic input-typed audio entry is used here anyway --
# the fix is a generic `type` filter, not a video-specific one, and this
# guards that seam in case a future ComfyUI audio loader (or a Cover-mode
# node) starts previewing the same way LoadVideo does.
# ---------------------------------------------------------------------------
print("(b) an audio input echo ordered before the real SaveAudio output "
      "(synthetic -- LoadAudio itself does not echo today, see note above)")
hist_b = {
    "outputs": {
        "150": {"audio": [{"filename": "cover_src.flac", "subfolder": "", "type": "input"}]},
        "9": {"audio": [{"filename": "MUSIC_00003.mp3", "subfolder": "blackwire", "type": "output"}]},
    }
}
outs_b = srv.collect_outputs(hist_b)
check("only the real audio output survives", len(outs_b) == 1, str(outs_b))
check("outputs[0] is the SaveAudio file, not the input echo",
      bool(outs_b) and outs_b[0]["filename"] == "MUSIC_00003.mp3", str(outs_b))


# ---------------------------------------------------------------------------
# (c) A PreviewImage node, type "temp" (comfy's own nodes.py: PreviewImage
# sets self.type = "temp"), ordered before the real SaveImage output.
# ---------------------------------------------------------------------------
print("(c) a temp preview image ordered before the real SaveImage output")
hist_c = {
    "outputs": {
        "55": {"images": [{"filename": "ComfyUI_temp_abcde_00001_.png", "subfolder": "", "type": "temp"}]},
        "60": {"images": [{"filename": "IMG_00013_.png", "subfolder": "blackwire", "type": "output"}]},
    }
}
outs_c = srv.collect_outputs(hist_c)
check("only the real image output survives", len(outs_c) == 1, str(outs_c))
check("outputs[0] is the SaveImage file, not the temp preview",
      bool(outs_c) and outs_c[0]["filename"] == "IMG_00013_.png", str(outs_c))


# ---------------------------------------------------------------------------
# (d) A history with ONLY an input echo (no real output node at all) --
# collect_outputs() must return [], not the echo. A job stuck at [] then has
# no output rather than a wrong one -- see the job_poller check below for
# what that does downstream.
# ---------------------------------------------------------------------------
print("(d) a history with only an input echo, no real output node")
hist_d = {
    "outputs": {
        "220": {"images": [{"filename": "CONT_00002_.mp4", "subfolder": "", "type": "input"}],
                "animated": [True]},
    }
}
outs_d = srv.collect_outputs(hist_d)
check("no output entries survive", outs_d == [], str(outs_d))


# ---------------------------------------------------------------------------
# A missing `type` key still counts as "output" (unchanged from before this
# fix) -- most real SaveX entries in the wild never set `type` explicitly
# and rely on it defaulting to "output" server-side (comfy's SaveImage
# hard-codes self.type = "output" but some third-party Save nodes omit the
# field on the dict entirely).
# ---------------------------------------------------------------------------
print("a file entry with no type key at all still counts as output")
hist_e = {"outputs": {"92": {"images": [{"filename": "no_type_field.mp4", "subfolder": "blackwire"}]}}}
outs_e = srv.collect_outputs(hist_e)
check("a missing type key is treated as output",
      len(outs_e) == 1 and outs_e[0]["filename"] == "no_type_field.mp4", str(outs_e))

print()
print(("FAILED: %d" % len(FAILED)) if FAILED else "ALL PASS")
sys.exit(1 if FAILED else 0)
