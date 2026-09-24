#!/usr/bin/env bash
# Engine coupling RATCHET — not a pass/fail gate. See the internal design-decisions doc Principle 0.
#
# WHY THIS IS A RATCHET, NOT A GATE (2026-09-22):
# We chose 2Wild over MCWW for its UI, and that choice COST us engine-independence.
# MCWW's core named no model; 2Wild's core is Qwen-Image + MiniMax-H3 specific by
# design -- measured 88 vendor-naming lines in server.py at fork time. So Principle 0
# is no longer a property we inherited; it is a direction we build toward.
#
# A pass/fail gate here could only ever fail, which makes it noise that gets
# ignored. Instead this pins a BASELINE and fails when coupling GROWS -- so the
# number can only go down as engines move into engines/ packs.
#
# When you legitimately reduce coupling, lower the baselines here in the same commit.
set -u
BASE_SERVER=0
BASE_INDEX=0
BASE_ROOMS=0
BASE_RUNNER=0
BASE_HELP=0
PAT='qwen|minimax|h3_|ace.step|\byue\b|trellis'
# rooms.json is core config in TASK words only -- it also must not name the
# tools packs load (a room is "Clean-up", never the model that cleans up).
ROOMS_PAT="$PAT|ltx|birefnet|esrgan|sdxl|rife"
# runner.py is the engine-agnostic job runner -- it gets the rooms.json
# treatment too, plus the render tools that packs load: it names none.
RUNNER_PAT="$ROOMS_PAT|blender|ffmpeg"
# help.html reads its live sections from /api/engines and /api/credits, so
# it must be exactly as engine-agnostic as rooms.json -- same pattern.
HELP_PAT="$ROOMS_PAT"

[ -f server.py ] || { echo "FAIL: server.py missing; ratchet would pass vacuously"; exit 2; }
[ -f rooms.json ] || { echo "FAIL: rooms.json missing; ratchet would pass vacuously"; exit 2; }
[ -f runner.py ] || { echo "FAIL: runner.py missing; ratchet would pass vacuously"; exit 2; }
[ -f help.html ] || { echo "FAIL: help.html missing; ratchet would pass vacuously"; exit 2; }

s=$(grep -ciE "$PAT" server.py)
i=$(grep -ciE 'qwen|minimax|h3|ace.step' index.html)
r=$(grep -ciE "$ROOMS_PAT" rooms.json)
u=$(grep -ciE "$RUNNER_PAT" runner.py)
h=$(grep -ciE "$HELP_PAT" help.html)
echo "engine coupling: server.py=$s (baseline $BASE_SERVER)  index.html=$i (baseline $BASE_INDEX)  rooms.json=$r (baseline $BASE_ROOMS)  runner.py=$u (baseline $BASE_RUNNER)  help.html=$h (baseline $BASE_HELP)"

rc=0
[ "$s" -gt "$BASE_SERVER" ] && { echo "FAIL: server.py coupling GREW ($BASE_SERVER -> $s)"; rc=1; }
[ "$i" -gt "$BASE_INDEX" ]  && { echo "FAIL: index.html coupling GREW ($BASE_INDEX -> $i)"; rc=1; }
[ "$r" -gt "$BASE_ROOMS" ]  && { echo "FAIL: rooms.json coupling GREW ($BASE_ROOMS -> $r)"; rc=1; }
[ "$u" -gt "$BASE_RUNNER" ] && { echo "FAIL: runner.py coupling GREW ($BASE_RUNNER -> $u)"; rc=1; }
[ "$h" -gt "$BASE_HELP" ]   && { echo "FAIL: help.html coupling GREW ($BASE_HELP -> $h)"; rc=1; }
[ "$s" -lt "$BASE_SERVER" ] && echo "note: server.py coupling FELL to $s — lower BASE_SERVER in this commit"
[ "$i" -lt "$BASE_INDEX" ]  && echo "note: index.html coupling FELL to $i — lower BASE_INDEX in this commit"
[ "$u" -lt "$BASE_RUNNER" ] && echo "note: runner.py coupling FELL to $u — lower BASE_RUNNER in this commit"
[ "$h" -lt "$BASE_HELP" ]   && echo "note: help.html coupling FELL to $h — lower BASE_HELP in this commit"
[ "$rc" = 0 ] && echo "PASS — coupling has not grown"
exit $rc
