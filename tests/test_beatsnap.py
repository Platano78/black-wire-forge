"""beatsnap.py: beat-snapped shot windows. Pure arithmetic, no server, no media.

The oracle is the Return's plan (34 segments, 4262 frames) copied as numbers into tests/fixtures/beatsnap_song_a.json:
every window must land on the same f0/f1 as the reference planner, with 0 frames of difference, and the P shots must
render the same 17n+5 length. The known-bad control runs the same fixture with each shot rounded on its own and must
FAIL the identity check, which proves the check can tell the difference. Run: python3 tests/test_beatsnap.py
"""
import json
import os
import sys

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
import beatsnap  # noqa: E402

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail != "" else ""))
    if not cond:
        FAILED.append(name)

def raises(fn, *needles):
    try:
        fn()
    except ValueError as e:
        msg = str(e)
        return all(n in msg for n in needles), msg
    return False, "no ValueError"

H3 = {"step": 17, "offset": 5, "min": 124, "max": 362, "carried": 0}
H3C = dict(H3, carried=22)
LTX = {"step": 8, "offset": 1, "min": 9, "max": 993, "carried": 0}
EXACT = {"step": 1, "offset": 0, "min": 1, "max": 10000, "carried": 0}
# The reference planner has no 124-frame floor (it takes the smallest 17n+5); BWF's H3 pack does. Identity of f0/f1
# is checked on the reference's own grid, and the pack floor is pinned separately below.
ORACLE_GRIDS = dict({"fl2va": dict(H3, min=22), "exact": EXACT})
GRIDS = {"fl2va": H3, "continue": H3C, "ltx": LTX, "exact": EXACT}

FX = json.load(open(os.path.join(HERE, "fixtures", "beatsnap_song_a.json")))
SEGS = FX["segments"]
def shots_of(segs):
    return [{k: v for k, v in s.items() if k in ("bars", "end_at", "to_end", "grid")} | {"id": "seg%02d" % i}
            for i, s in enumerate(segs)]

def diffs(wins):
    return [i for i, (w, s) in enumerate(zip(wins, SEGS)) if (w["f0"], w["f1"]) != (s["f0"], s["f1"])]

# --- the oracle: Claude's reference plan ------------------------------------------------------------------------------
print("oracle identity")
W = beatsnap.windows(FX["downbeats"], FX["end"], shots_of(SEGS), ORACLE_GRIDS)
check("34 windows", len(W) == len(SEGS) == 34, len(W))
check("f0/f1 equal the reference plan on every segment (0 frames of difference)", diffs(W) == [], diffs(W))
check("sum of window frames is 4262", sum(w["W"] for w in W) == FX["total_frames"] == 4262, sum(w["W"] for w in W))
check("windows tile with no gap", all(a["f1"] == b["f0"] for a, b in zip(W, W[1:])) and W[0]["f0"] == 0)
check("last window ends at round(song * 24)", W[-1]["f1"] == round(FX["end"] * 24))
bad = [i for i, (w, s) in enumerate(zip(W, SEGS)) if "render_frames" in s and w["render_frames"] != s["render_frames"]]
check("P shots render the reference's frame counts", bad == [] and sum("render_frames" in s for s in SEGS) == 21, bad)
floor = beatsnap.windows(FX["downbeats"], FX["end"], shots_of(SEGS), GRIDS)
fl = [i for i, (w, s) in enumerate(zip(floor, SEGS)) if "render_frames" in s and w["render_frames"] != s["render_frames"]]
check("with BWF's H3 floor (124) the only render differences are the two short shots the reference rendered at 107",
      fl == [28, 32] and all(floor[i]["render_frames"] == 124 and SEGS[i]["render_frames"] == 107 for i in fl), fl)
check("the floor leaves every f0/f1 unchanged", diffs(floor) == [])
check("exact shots render exactly W", all(w["render_frames"] == w["W"] for w, s in zip(W, SEGS) if s["grid"] == "exact"))
check("intended downbeat recorded for bar shots", W[1]["k"] == 2 and W[1]["bars"] == 2 and W[0]["k"] == 0, (W[0]["k"], W[1]["k"]))
check("end_at and to_end shots record no downbeat", W[25]["k"] is None and W[25]["edge"] == "end_at" and W[33]["edge"] == "to_end")

print("known-bad control: per-shot rounding must fail the identity check")
orig = beatsnap._next_frame
beatsnap._next_frame = lambda f0, t0, t1, fps: f0 + round((t1 - t0) * fps)
try:
    WB = beatsnap.windows(FX["downbeats"], FX["end"], shots_of(SEGS), GRIDS)
finally:
    beatsnap._next_frame = orig
d_bad = diffs(WB)
check("per-shot rounding differs from the reference somewhere", len(d_bad) > 0, d_bad)
check("per-shot rounding misses the 4262 total or a frame edge", sum(w["W"] for w in WB) != 4262 or len(d_bad) > 0)
W2 = beatsnap.windows(FX["downbeats"], FX["end"], shots_of(SEGS), ORACLE_GRIDS)
check("after the control the real rounding is restored", diffs(W2) == [])
beatsnap._next_frame = lambda f0, t0, t1, fps: int(t1 * fps)
try:
    WT = beatsnap.windows(FX["downbeats"], FX["end"], shots_of(SEGS), GRIDS)
finally:
    beatsnap._next_frame = orig
check("truncating also fails the identity check", len(diffs(WT)) > 0)

# --- unit cases ---------------------------------------------------------------------------------------------------------
print("pickup before the first downbeat")
D = [1.0, 3.0, 5.0, 7.0, 9.0, 11.0]
w = beatsnap.windows(D, 12.0, [{"bars": 2, "grid": "exact"}, {"to_end": True, "grid": "exact"}], GRIDS)
check("first shot starts at 0 and ends two bars after the nearest downbeat (D[0] = 1.0 -> D[2] = 5.0)",
      (w[0]["f0"], w[0]["f1"], w[0]["k"]) == (0, 120, 0), w[0])
check("the next shot starts on the downbeat it ended on and runs to the end", (w[1]["f0"], w[1]["f1"]) == (120, 288), w[1])

print("nearest-downbeat tie goes to the earlier downbeat")
D = [2.0, 4.0, 6.0, 8.0]
w = beatsnap.windows(D, 9.0, [{"end_at": 3.0, "grid": "exact"}, {"bars": 1, "grid": "exact"}], GRIDS)
check("start 3.0 sits exactly between D[0]=2.0 and D[1]=4.0 -> D[0]; 1 bar ends at 4.0 (frame 96)",
      (w[1]["k"], w[1]["f0"], w[1]["f1"]) == (0, 72, 96), w[1])
w = beatsnap.windows(D, 9.0, [{"end_at": 3.01, "grid": "exact"}, {"bars": 1, "grid": "exact"}], GRIDS)
check("just past the midpoint it goes to the later one", w[1]["k"] == 1, w[1]["k"])

print("half-frame rounding is Python round() (half to even), separate from the tie rule")
# 1.5/24, 4.5/24, 7.5/24, 10.5/24 and 13.5/24 are exact in binary, so t*24 is exactly k + 0.5.
D = [0.0, 5.0]
halves = [{"end_at": x / 24, "grid": "exact"} for x in (1.5, 4.5, 7.5, 10.5, 13.5)]
w = beatsnap.windows(D, 6.0, halves, GRIDS)
check("exact halves 1.5, 4.5, 7.5, 10.5, 13.5 land on frames 2, 4, 8, 10, 14 (even), not 2, 5, 8, 11, 14",
      [x["f1"] for x in w] == [2, 4, 8, 10, 14], [x["f1"] for x in w])
check("round() itself is half to even here", [round(x) for x in (1.5, 4.5, 7.5, 10.5, 13.5)] == [2, 4, 8, 10, 14])
ok, msg = raises(lambda: beatsnap.windows(D, 6.0, [{"id": "v1", "end_at": 1.0, "grid": "exact"},
                                                   {"id": "v2", "end_at": 1.0 + 0.01 / 24, "grid": "exact"}], GRIDS), "v2", "0 frames")
check("a shot whose two edges round to the same frame is refused by name", ok, msg)

print("continue: carried frames, tail anchor, slice start")
D = [0.0, 2.5, 5.0, 7.5, 10.0, 12.5, 15.0]
shots = [{"id": "up", "bars": 3, "grid": "fl2va", "tail": True}, {"id": "next", "bars": 2, "grid": "continue"}]
w = beatsnap.windows(D, 16.0, shots, GRIDS)
up, nx = w
check("upstream W = 180, renders 192 (17*11+5 = 192)", (up["W"], up["render_frames"]) == (180, 192), up)
check("upstream E = 12 and is tail-anchored: keep tail, trim_in = E/24",
      (up["E"], up["keep"], up["trim_in"]) == (12, "tail", 0.5), up)
check("upstream slice starts E/24 earlier than its window (start 0 -> -0.5, leading silence)",
      up["slice_start"] == -0.5, up["slice_start"])
check("continue: W = 120, need = 142 -> min 124 does not bite, 17n+5 >= 142 is 142 (n=8.06 -> 9 -> 158)",
      (nx["W"], nx["render_frames"]) == (120, 158), nx)
check("continue delivers render - 22 = 136, E = 16, head-anchored", (nx["E"], nx["keep"], nx["trim_in"]) == (16, "head", 0.0), nx)
check("continue slice starts carried/24 before its window", nx["slice_start"] == nx["start"] - 22 / 24, nx)
check("a head shot's slice_start is its start", beatsnap.windows(D, 16.0, [{"bars": 1, "grid": "fl2va"}], GRIDS)[0]["slice_start"] == 0.0)

print("negative slice start (window arithmetic only; the leading silence is the audio side's job)")
check("tail-anchored first shot has a negative slice_start, documented as leading silence", up["slice_start"] < 0)
w = beatsnap.windows(D, 16.0, [{"bars": 1, "grid": "continue"}], GRIDS)
check("a continue shot at the very start also reaches before 0 by carried/24", w[0]["slice_start"] == -22 / 24, w[0]["slice_start"])

print("LTX needs W+1")
w = beatsnap.windows(D, 16.0, [{"bars": 2, "grid": "ltx"}], GRIDS)[0]
check("W = 120 -> need 121 -> 8n+1 = 121; delivers 121, E = 1", (w["W"], w["render_frames"], w["E"]) == (120, 121, 1), w)
w = beatsnap.windows(D, 16.0, [{"end_at": 5.0 + 1 / 24, "grid": "ltx"}], GRIDS)[0]
check("W = 121 -> need 122 -> 129 (a take of 8n+1 = 121 would lose a frame)", (w["W"], w["render_frames"]) == (121, 129), w)
check("render_frames(W, LTX) direct", beatsnap.render_frames(112, LTX) == 113 and beatsnap.render_frames(113, LTX) == 121)

print("H3 minimum and maximum")
check("a short H3 window is raised to 124", beatsnap.render_frames(60, H3) == 124 and beatsnap.render_frames(118, H3) == 124)
check("124 stays 124 (17*7+5) and 125 goes to 141", beatsnap.render_frames(124, H3) == 124 and beatsnap.render_frames(125, H3) == 141)
check("362 fits", beatsnap.render_frames(362, H3) == 362)
D = [i * 5.0 for i in range(10)]
ok, msg = raises(lambda: beatsnap.windows(D, 46.0, [{"id": "v3", "bars": 4, "grid": "fl2va"}], GRIDS), "v3", "4 bars", "at most 362", "at most 3 bars fit")
check("a 4-bar (480-frame) H3 shot is refused, naming the shot and the most bars that fit", ok, msg)
print("        " + msg)
w = beatsnap.windows(D, 46.0, [{"bars": 1, "grid": "fl2va"}, {"grid": "fl2va"}, {"to_end": True, "grid": "exact"}], GRIDS)
check("auto with no planned length is 1 bar", w[1]["bars"] == 1)
w = beatsnap.windows(D, 46.0, [{"planned_len": 99.0, "grid": "fl2va"}, {"to_end": True, "grid": "exact"}], GRIDS)
check("auto is clamped to the most bars that fit (3)", w[0]["bars"] == 3, w[0])
w = beatsnap.windows(D, 46.0, [{"planned_len": 5.2, "grid": "fl2va"}, {"to_end": True, "grid": "exact"}], GRIDS)
check("auto rounds planned_len / median bar (5.2 / 5.0 -> 1)", w[0]["bars"] == 1, w[0])
check("fit_max_bars from frame 0 on D[0]", beatsnap.fit_max_bars(D, 0, 0, H3) == 3 and beatsnap.fit_max_bars(D, 0, 0, EXACT) == 9,
      (beatsnap.fit_max_bars(D, 0, 0, H3), beatsnap.fit_max_bars(D, 0, 0, EXACT)))

print("bars past the end")
D = [0.0, 2.5, 5.0, 7.5, 10.0]
ok, msg = raises(lambda: beatsnap.windows(D, 11.0, [{"id": "v6", "bars": 3, "grid": "exact"}, {"id": "v7", "bars": 3, "grid": "exact"}], GRIDS),
                 "v7", "3 bars", "1 bar left", "after shot v6")
check("the sentence names the shot, the bars asked, the bars left and the shot before", ok, msg)
print("        " + msg)
check("bars on the very first shot say so", raises(lambda: beatsnap.windows(D, 11.0, [{"id": "v1", "bars": 9, "grid": "exact"}], GRIDS), "v1", "from the start")[0])
check("a shot after to_end is refused", raises(lambda: beatsnap.windows(D, 11.0, [{"to_end": True, "grid": "exact"}, {"bars": 1, "grid": "exact", "id": "late"}], GRIDS), "late", "end of the song")[0])
check("end_at past the song is refused", raises(lambda: beatsnap.windows(D, 11.0, [{"end_at": 12.0, "grid": "exact", "id": "v1"}], GRIDS), "v1", "past the end")[0])
check("a missing grid is refused by shot name", raises(lambda: beatsnap.windows(D, 11.0, [{"bars": 1, "id": "v1"}], GRIDS), "v1", "frame grid")[0])
check("two edge settings are refused", raises(lambda: beatsnap.windows(D, 11.0, [{"bars": 1, "to_end": True, "grid": "exact", "id": "v1"}], GRIDS), "v1", "more than one")[0])
check("bars of 0 are refused", raises(lambda: beatsnap.windows(D, 11.0, [{"bars": 0, "grid": "exact", "id": "v1"}], GRIDS), "v1", "bars")[0])

print("master_start > 0")
D = [0.5, 3.0, 5.5, 8.0, 10.5, 13.0]
w = beatsnap.windows(D, 14.0, [{"bars": 2, "grid": "exact"}, {"to_end": True, "grid": "exact"}], GRIDS, master_start=3.0)
check("downbeats before the master start are dropped and the clock starts at 0 (5.5-3.0 = 2.5 s -> 60 frames after D[0]=0.0 + 2 bars = 5.0 s)",
      (w[0]["f0"], w[0]["f1"], w[0]["k"]) == (0, 120, 0), w[0])
check("the end is master end - master_start (14 - 3 = 11 s = 264 frames)", w[1]["f1"] == 264, w[1])
check("the same song with master_start 0 differs", beatsnap.windows(D, 14.0, [{"bars": 2, "grid": "exact"}], GRIDS)[0]["f1"] != 120)

print("parse_timed_lines")
t = beatsnap.parse_timed_lines("[0:05.8] first line\n\n[1:02.5]   second  \n[10:00] third")
check("parses minutes, seconds and tenths", [(x["start"], x["text"]) for x in t] == [(5.8, "first line"), (62.5, "second"), (600.0, "third")], t)
for bad in ("no stamp here", "[x:12] words", "[0:75] words", "[5] words", "[0:05"):
    check("refuses %r with the line number" % bad, raises(lambda b=bad: beatsnap.parse_timed_lines("[0:01] ok\n" + b), "Line 2")[0])
check("refuses a line over 300 characters", raises(lambda: beatsnap.parse_timed_lines("[0:01] " + "x" * 301), "300")[0])
check("refuses over 400 lines", raises(lambda: beatsnap.parse_timed_lines("\n".join("[0:01] a" for _ in range(401))), "400")[0])

print()
print("ALL PASS" if not FAILED else "FAILED: %d -- %s" % (len(FAILED), FAILED))
sys.exit(1 if FAILED else 0)
