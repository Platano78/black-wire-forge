"""C1 -- help.html's live availability count and index.html's process-lane
READY tile, checked as plain text/structure against the HTML/JS source.

Neither page's JS runs anywhere without a browser (the Playwright suites --
test_*_ui.py -- cover that, and need a running server + browser this host
does not run). This is the cheaper, server-free half: it proves the SPECIFIC
defective patterns reported by review2.md are gone and the specific fix is
present, by reading the files as text. It cannot prove the JS is bug-free in
general -- only that these two named defects are fixed.

Run:  python3 tests/test_ui_lane_gating.py
Exit 0 = all good. No third-party imports, no server, no browser.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

FAILED = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond:
        print("        -> %s" % (detail,))
        FAILED.append(name)


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# help.html: counting a lane "can make this" for a mode
# ---------------------------------------------------------------------------
help_src = read("help.html")

# The buggy pattern this test guards against: `m.available` alone, with
# nothing else gating it, straight into `can.push`.
buggy = re.search(r"if\(m\s*&&\s*m\.available\)\s*can\.push\(lane\.name\);", help_src)
check("help.html no longer counts a lane available from m.available alone",
      buggy is None, "found: %r" % (buggy and buggy.group(0),))

# The fix: an `eligible` (or equivalent) gate that checks lane.up,
# lane.declared_caps, lane.kind vs m.lane_kind, AND m.available, before the
# lane is counted as able to make this mode right now.
push_block = re.search(r"lanes\.forEach\(lane\s*=>\s*\{.*?\}\);", help_src, re.S)
check("help.html has a lanes.forEach block for the per-mode 'can make this' count",
      bool(push_block), "block not found")
block = push_block.group(0) if push_block else ""
check("...gates on lane.up", "lane.up" in block, block)
check("...gates on the lane's declared_caps including this cap",
      "declared_caps" in block, block)
check("...gates on lane.kind matching the mode's lane_kind",
      re.search(r"lane\.kind\s*===?\s*m\.lane_kind", block) is not None, block)
check("...still requires m.available too",
      "m.available" in block, block)

# ---------------------------------------------------------------------------
# index.html: the process-lane branch of machineState() must consult lane.up
# ---------------------------------------------------------------------------
index_src = read("index.html")
m = re.search(
    r"if\(laneKindOf\(lane\)\s*===\s*'process'\)\{(.*?)\n  \}\n  if\(!lane\.up\)",
    index_src, re.S)
check("index.html's machineState() has a process-lane branch", bool(m), "branch not found")
proc_branch = m.group(1) if m else ""
check("...checks lane.up before ever returning READY",
      "lane.up" in proc_branch, proc_branch)
# The exact bug reported: idle (running==0, pending==0) unconditionally READY.
old_bug = re.search(
    r"if\(laneKindOf\(lane\) === 'process'\)\{\s*"
    r"//[^\n]*\n\s*//[^\n]*\n\s*if\(lane\.running > 0 \|\| lane\.pending > 0\) "
    r"return \{state:'working', label:'WORKING'\};\s*\n\s*return \{state:'ready', label:'READY'\};\s*\n\s*\}",
    index_src)
check("index.html's old unconditional-READY process-lane branch is gone",
      old_bug is None, "old pattern still present verbatim")

print()
if FAILED:
    print("FAILED: %d check(s): %s" % (len(FAILED), ", ".join(FAILED)))
    sys.exit(1)
print("All lane-gating UI checks passed.")
