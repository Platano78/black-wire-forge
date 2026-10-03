"""Tests for forge_master planning module.

Run: python3 tests/test_forge_master_plan.py
"""
import json
import math
import sys
import os

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import forge_master  # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (
        ("  " + str(detail)) if not cond and detail else ""))
    if not cond:
        FAILED.append(name)


# ---------------------------------------------------------------------------
# plan_windows
# ---------------------------------------------------------------------------

def _test_plan_windows():
    # 169 s: 507 thirds, n = round(507/12) = 42
    ws = forge_master.plan_windows(169.0)
    # formula: total_thirds=507, n=round(507/12)=42
    check("plan_windows(169.0) count", len(ws) == 42, str(len(ws)))
    total = sum(w["thirds"] for w in ws)
    check("plan_windows(169.0) total thirds", total == 507, str(total))
    # tiling: start of i+1 == start_i + len_i within 1e-9
    tiling_ok = True
    for i in range(len(ws) - 1):
        gap = abs(ws[i + 1]["start"] - ws[i]["start"] - ws[i]["len"])
        if gap >= 1e-9:
            check("plan_windows(169.0) tile gap", False,
                  f"i={i} gap={gap}")
            tiling_ok = False
            break
    if tiling_ok:
        check("plan_windows(169.0) tiling", True)
    # every shot within bounds
    for w in ws:
        ok = forge_master.MIN_THIRDS <= w["thirds"] <= forge_master.MAX_THIRDS
        if not ok:
            check("plan_windows(169.0) bounds", False, str(w["thirds"]))
            return
    check("plan_windows(169.0) bounds all", True)
    # frames == 8*thirds+1
    for w in ws:
        ok = w["frames"] == 8 * w["thirds"] + 1
        if not ok:
            check("plan_windows(169.0) frames", False, str(w))
            return
    check("plan_windows(169.0) frames formula", True)

    # Valid inputs
    try:
        forge_master.plan_windows(60.0)
        check("plan_windows(60.0) valid", True)
    except Exception as e:
        check("plan_windows(60.0) valid", False, str(e))

    try:
        forge_master.plan_windows(2.0)
        check("plan_windows(2.0) valid", True)
    except Exception as e:
        check("plan_windows(2.0) valid", False, str(e))

    try:
        forge_master.plan_windows(3.4)
        check("plan_windows(3.4) valid", True)
    except Exception as e:
        check("plan_windows(3.4) valid", False, str(e))

    # Too short
    try:
        forge_master.plan_windows(1.5)
        check("plan_windows(1.5) raises", False, "no exception")
    except ValueError as e:
        check("plan_windows(1.5) raises exact",
              str(e) == "That song is too short for a video (under 2 seconds).",
              str(e))

    # Too long
    try:
        forge_master.plan_windows(601)
        check("plan_windows(601) raises", False, "no exception")
    except ValueError as e:
        check("plan_windows(601) raises exact",
              str(e) == "That song is too long to plan in one go.",
              str(e))

    # 4 s song: 12 thirds, n=1, one shot of 12
    ws4 = forge_master.plan_windows(4.0)
    check("plan_windows(4.0) count", len(ws4) == 1, str(len(ws4)))
    check("plan_windows(4.0) thirds", ws4[0]["thirds"] == 12, str(ws4[0]["thirds"]))
    check("plan_windows(4.0) len", ws4[0]["len"] == 4.0, str(ws4[0]["len"]))


# ---------------------------------------------------------------------------
# parse_lyrics
# ---------------------------------------------------------------------------

def _test_parse_lyrics():
    # Tags with colon and without
    text = "[Verse 1]\nHello world\n[Chorus: Hype Man]\nSing along\n\n"
    parsed = forge_master.parse_lyrics(text)
    check("parse_lyrics section names",
          len(parsed) == 2 and parsed[0]["section"] == "Verse 1"
          and parsed[1]["section"] == "Chorus",
          str([s["section"] for s in parsed]))
    check("parse_lyrics lines",
          parsed[0]["lines"] == ["Hello world"]
          and parsed[1]["lines"] == ["Sing along"],
          str(parsed))

    # Line before any tag goes into ""
    text2 = "Intro line\n[Verse 1]\nAfter verse\n"
    parsed2 = forge_master.parse_lyrics(text2)
    check("parse_lyrics pre-tag section", parsed2[0]["section"] == "",
          str(parsed2[0]["section"]))
    check("parse_lyrics pre-tag line", parsed2[0]["lines"] == ["Intro line"])

    # Blank lines are skipped
    text3 = "[Verse 1]\n\nLine 1\n\nLine 2\n"
    parsed3 = forge_master.parse_lyrics(text3)
    check("parse_lyrics blank lines skipped",
          parsed3[0]["lines"] == ["Line 1", "Line 2"],
          str(parsed3[0]["lines"]))

    # Empty string
    check("parse_lyrics empty string", forge_master.parse_lyrics("") == [])

    # None
    check("parse_lyrics None", forge_master.parse_lyrics(None) == [])


# ---------------------------------------------------------------------------
# assign_lyrics
# ---------------------------------------------------------------------------

def _test_assign_lyrics():
    windows = forge_master.plan_windows(169.0)
    parsed = [
        {"section": "Verse", "lines": ["a", "b", "c", "d"]},
        {"section": "Chorus", "lines": ["e", "f"]},
    ]
    assigned = forge_master.assign_lyrics(windows, parsed)

    # Every line appears exactly once
    all_lines = []
    for w in assigned:
        all_lines.extend(w["lines"])
    check("assign_lyrics every line once", all_lines == ["a", "b", "c", "d", "e", "f"],
          str(all_lines))

    # Windows with no lines have section "" and lines []
    empty_windows = [w for w in assigned if not w["lines"]]
    for ew in empty_windows:
        check("assign_lyrics empty section", ew["section"] == "", ew["section"])
        check("assign_lyrics empty lines", ew["lines"] == [])

    # Section of window is its first line's section
    for w in assigned:
        if w["lines"]:
            first_line = w["lines"][0]
            expected_section = None
            for sec in parsed:
                if first_line in sec["lines"]:
                    expected_section = sec["section"]
                    break
            check(f"assign_lyrics section for line '{first_line}'",
                  w["section"] == expected_section,
                  f"got {w['section']}, expected {expected_section}")


# ---------------------------------------------------------------------------
# fallback_scenes
# ---------------------------------------------------------------------------

def _test_fallback_scenes():
    scenes = forge_master.fallback_scenes(5)
    check("fallback_scenes count", len(scenes) == 5, str(len(scenes)))
    # Different motions
    motions = [s["motion"] for s in scenes]
    check("fallback_scenes different motions", len(set(motions)) == 5, str(motions))

    # Style note appended
    scenes2 = forge_master.fallback_scenes(2, style_note="retro vibes")
    check("fallback_scenes style note",
          "retro vibes" in scenes2[0]["scene"],
          scenes2[0]["scene"])


# ---------------------------------------------------------------------------
# build_scene_request
# ---------------------------------------------------------------------------

def _test_build_scene_request():
    windows = [
        {"index": 0, "lines": ["a", "b"], "section": "Verse"},
        {"index": 1, "lines": [], "section": "", "thirds": 12, "frames": 97,
         "start": 4.0, "len": 4.0},
    ]
    system, user = forge_master.build_scene_request(windows, "retro", has_person=True)

    # System mentions exactly N=2
    check("build_scene_request N in system", 'EXACTLY 2' in system)

    # User has one numbered line per window (+ optional style note line)
    lines_in_user = [l for l in user.split("\n") if l.strip()]
    # style note is prepended as its own line, so expect 3 lines (style + 2 numbered)
    check("build_scene_request one line per window", len(lines_in_user) == 3,
          str(len(lines_in_user)) + " lines; user=" + repr(user))

    # First numbered line (index 1 after style note) has section and lines joined
    check("build_scene_request section label",
          "[Verse]" in lines_in_user[1],
          lines_in_user[1])
    check("build_scene_request joined lines",
          "a / b" in lines_in_user[1],
          lines_in_user[1])

    # Second numbered line says instrumental for empty
    check("build_scene_request instrumental",
          "instrumental" in lines_in_user[2].lower(),
          lines_in_user[2])

    # Style note in user
    check("build_scene_request style note",
          "retro" in user,
          user[:100])


# ---------------------------------------------------------------------------
# parse_scene_reply
# ---------------------------------------------------------------------------

def _test_parse_scene_reply():
    n = 2
    base_entry = {"scene": "on a stage", "motion": "dancing"}
    valid = {"scenes": [base_entry, {"scene": "in a field", "motion": "singing"}]}

    # Plain JSON
    r = forge_master.parse_scene_reply(json.dumps(valid), n)
    check("parse_scene_reply plain JSON", r is not None and len(r) == 2)

    # ```json fenced
    r2 = forge_master.parse_scene_reply("```json\n" + json.dumps(valid) + "\n```", n)
    check("parse_scene_reply fenced", r2 is not None and len(r2) == 2)

    # Prose around JSON
    r3 = forge_master.parse_scene_reply("Here is the answer:\n" + json.dumps(valid) + "\nDone.", n)
    check("parse_scene_reply prose around", r3 is not None and len(r3) == 2)

    # Wrong count
    wrong = {"scenes": [base_entry]}
    r4 = forge_master.parse_scene_reply(json.dumps(wrong), n)
    check("parse_scene_reply wrong count -> None", r4 is None)

    # Empty strings -> None
    empty = {"scenes": [{"scene": "", "motion": "x"}, {"scene": "y", "motion": "z"}]}
    r5 = forge_master.parse_scene_reply(json.dumps(empty), n)
    check("parse_scene_reply empty string -> None", r5 is None)

    # Garbage -> None
    r6 = forge_master.parse_scene_reply("not json at all", n)
    check("parse_scene_reply garbage -> None", r6 is None)

    # Long strings cut to 300
    long_entry = {"scene": "x" * 500, "motion": "y" * 500}
    long_valid = {"scenes": [long_entry, long_entry]}
    r7 = forge_master.parse_scene_reply(json.dumps(long_valid), n)
    check("parse_scene_reply cut to 300",
          all(len(s["scene"]) <= 300 and len(s["motion"]) <= 300 for s in r7) if r7 else False,
          str(len(r7[0]["scene"])) if r7 else "None")


# ---------------------------------------------------------------------------
# write_scenes
# ---------------------------------------------------------------------------

def _test_write_scenes():
    windows = forge_master.plan_windows(169.0)

    # chat=None -> fallback
    scenes, source = forge_master.write_scenes(windows)
    check("write_scenes None chat -> fallback", source == "fallback", source)
    check("write_scenes fallback len", len(scenes) == len(windows), str(len(scenes)))

    # Good fake chat -> helper
    call_count = [0]

    def good_chat(system, user):
        call_count[0] += 1
        n = len(windows)
        return json.dumps({"scenes": [
            {"scene": f"scene {i}", "motion": f"move {i}"} for i in range(n)
        ]})

    scenes, source = forge_master.write_scenes(windows, chat=good_chat)
    check("write_scenes good chat -> helper", source == "helper", source)
    check("write_scenes helper len", len(scenes) == len(windows), str(len(scenes)))
    check("write_scenes good chat called once", call_count[0] == 1, str(call_count[0]))

    # Chat that raises twice -> fallback
    def bad_chat(system, user):
        raise RuntimeError("boom")

    scenes, source = forge_master.write_scenes(windows, chat=bad_chat)
    check("write_scenes bad chat -> fallback", source == "fallback", source)
    check("write_scenes bad chat len", len(scenes) == len(windows))

    # Chat fails once then succeeds -> helper (one retry)
    def flaky_chat(system, user):
        if flaky_chat._called == 0:
            flaky_chat._called += 1
            raise RuntimeError("first fail")
        flaky_chat._called += 1
        return json.dumps({"scenes": [
            {"scene": f"scene {i}", "motion": f"move {i}"} for i in range(len(windows))
        ]})
    flaky_chat._called = 0

    scenes, source = forge_master.write_scenes(windows, chat=flaky_chat)
    check("write_scenes flaky chat -> helper", source == "helper", source)
    check("write_scenes flaky called twice", flaky_chat._called == 2, str(flaky_chat._called))

    # Fake chat called at most twice
    call_count2 = [0]
    def bounded_chat(system, user):
        call_count2[0] += 1
        return json.dumps({"scenes": [
            {"scene": "s", "motion": "m"} for _ in windows
        ]})
    forge_master.write_scenes(windows, chat=bounded_chat)
    check("write_scenes max two calls", call_count2[0] <= 2, str(call_count2[0]))


# ---------------------------------------------------------------------------
# plan_line
# ---------------------------------------------------------------------------

def _test_plan_line():
    ws42 = forge_master.plan_windows(169.0)
    sent42 = forge_master.plan_line(ws42)
    check("plan_line 42 shots starts with 42", sent42.startswith("42 shots"),
          sent42[:60])
    check("plan_line 42 mentions seconds", "seconds" in sent42, sent42[:60])

    ws1 = forge_master.plan_windows(4.0)
    sent1 = forge_master.plan_line(ws1)
    check("plan_line 1 shot singular", sent1.startswith("1 shot"), sent1[:60])
    # When n==1, "shot" is singular; seconds stays plural if mean > 1
    check("plan_line 1 shot second plural", "seconds" in sent1, sent1[:60])
    check("plan_line 1 shot has no 'each' or 'in all'", " each" not in sent1 and " in all" not in sent1, sent1)


# ---------------------------------------------------------------------------
# Run all
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    _test_plan_windows()
    _test_parse_lyrics()
    _test_assign_lyrics()
    _test_fallback_scenes()
    _test_build_scene_request()
    _test_parse_scene_reply()
    _test_write_scenes()
    _test_plan_line()

    print()
    if FAILED:
        print(f"FAILED ({len(FAILED)}): {', '.join(FAILED)}")
    else:
        print("All tests passed.")
    sys.exit(1 if FAILED else 0)
