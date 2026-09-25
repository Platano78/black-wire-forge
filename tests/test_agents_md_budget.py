"""Gate: AGENTS.md keeps its essentials inside the part a harness keeps.

Some agent harnesses load AGENTS.md capped at 20,000 characters and keep only
its first 14,000 (plus the last 4,000), cutting the middle. So the sections an
agent needs to set the app up safely -- what it is, the rules, the path, the
install, the checks and getting a file -- must each start AND end before
character 13,500, in that order; the reference sections follow. No browser.

Run: python3 tests/test_agents_md_budget.py
"""
import os
import re
import sys

sys.dont_write_bytecode = True
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILED = []
BUDGET = 13500
ESSENTIAL = ["What this is / is not", "Rules for the agent", "Decide the path with the user first",
             "Install + start", "Verify", "Make something and get the file"]
REFERENCE = ["Fixing what's missing", "Troubleshooting", "Hardware", "Tests", "Getting the models",
             "Where to read further"]


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond:
        print("        -> %s" % (detail,))
        FAILED.append(name)


with open(os.path.join(ROOT, "AGENTS.md"), encoding="utf-8") as f:
    text = f.read()
heads = [(m.start(), m.group(1)) for m in re.finditer(r"^## (.+)$", text, re.M)]
spans = {h: (s, heads[i + 1][0] if i + 1 < len(heads) else len(text)) for i, (s, h) in enumerate(heads)}
print("AGENTS.md: %d characters; sections: %s" % (len(text), [(h, s, e) for h, (s, e) in spans.items()]))

check("every required section is there", all(h in spans for h in ESSENTIAL + REFERENCE),
      [h for h in ESSENTIAL + REFERENCE if h not in spans])
check("the essentials come first, in order, then the reference sections",
      [h for _, h in heads] == ESSENTIAL + REFERENCE, [h for _, h in heads])
for h in ESSENTIAL:
    s, e = spans.get(h, (None, None))
    check("'%s' starts and ends before character %d" % (h, BUDGET), e is not None and e <= BUDGET, (s, e))
check("'Where the file actually lives' is inside the kept part",
      0 <= text.find("Where the file actually lives") < BUDGET, text.find("Where the file actually lives"))
check("the top says the essentials are at the top, for harnesses that truncate",
      "truncat" in text[:spans.get(ESSENTIAL[0], (0,))[0] + 1].lower())

if FAILED:
    print("FAILED: %d checks: %s" % (len(FAILED), ", ".join(FAILED)))
    sys.exit(1)
print("OK: all checks passed")
