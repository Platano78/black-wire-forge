"""Forge Master planning module.

Cuts a song into shot windows, assigns lyric lines, and builds scene requests.
Pure Python, stdlib only. No network. No hardware-specific names.
"""

import json
import math
import re

# --- Constants ---

SHOT_THIRDS = 12            # target shot: 4 s (97 frames)
MIN_THIRDS, MAX_THIRDS = 6, 16   # allowed shot: 2 s .. 5.33 s

SECTION_FALLBACK_MOTIONS = (
    "heads to the beat",
    "points at the camera",
    "leans closer to the lens",
    "raises a fist in the air",
    "breaks into a big smile",
    "shakes head no",
    "spreads both arms wide",
    "bobs along to the rhythm",
    "nods slowly",
    "gestures with one hand",
    "tilts head to the side",
    "claps along to the beat",
)


# --- plan_windows ---

def plan_windows(song_seconds, shot_thirds=SHOT_THIRDS):
    """Return a list of shot-window dicts that tile the song exactly.

    Raises ValueError with a plain sentence if song_seconds < 2.0 or > 600,
    or if any resulting shot falls outside MIN_THIRDS..MAX_THIRDS.
    """
    if song_seconds < 2.0:
        raise ValueError(
            "That song is too short for a video (under 2 seconds)."
        )
    if song_seconds > 600:
        raise ValueError("That song is too long to plan in one go.")

    total_thirds = math.ceil(song_seconds * 3 - 1e-9)
    n = max(1, round(total_thirds / shot_thirds))
    base, extra = divmod(total_thirds, n)

    windows = []
    start_thirds = 0
    for i in range(n):
        t = base + 1 if i < extra else base
        windows.append({
            "index": i,
            "thirds": t,
            "frames": 8 * t + 1,
            "start": start_thirds / 3,
            "len": t / 3,
        })
        start_thirds += t

    # Validate every shot is within bounds
    for w in windows:
        if w["thirds"] < MIN_THIRDS or w["thirds"] > MAX_THIRDS:
            raise ValueError(
                "That song is too long to plan in one go."
            )

    return windows


# --- parse_lyrics ---

def parse_lyrics(text):
    """Return list of {"section": str, "lines": [str]} in order.

    Bracketed tags like [Verse 1] or [Chorus: Hype Man] start a new section.
    Lines before the first tag go into section "".
    Empty/None text returns []. Never raises.
    """
    if not text:
        return []

    sections = []
    current_section = ""
    current_lines = []
    tag_re = re.compile(r"^\[(.+?)\]\s*$")

    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue

        m = tag_re.match(stripped)
        if m:
            # Flush previous section
            if current_lines:
                sections.append(
                    {"section": current_section, "lines": list(current_lines)}
                )
            # Start new section (text before optional colon, stripped)
            name = m.group(1).split(":")[0].strip()
            current_section = name
            current_lines = []
        else:
            current_lines.append(stripped)

    # Flush last section
    if current_lines:
        sections.append(
            {"section": current_section, "lines": list(current_lines)}
        )
    elif current_section and not sections:
        # There was a section but no lines — still emit it
        sections.append({"section": current_section, "lines": []})

    return sections


# --- assign_lyrics ---

def assign_lyrics(windows, parsed):
    """Distribute all lyric lines over windows proportionally.

    Returns a NEW list of window dicts, each extended with "lines" and "section".
    """
    # Flatten all lines in order, keeping section info
    flat = []
    for sec in parsed:
        for line in sec["lines"]:
            flat.append((sec["section"], line))

    L = len(flat)
    n = len(windows)
    new_windows = []
    for i in range(n):
        lo = math.floor(i * L / n)
        hi = math.floor((i + 1) * L / n)
        window_lines = [line for _, line in flat[lo:hi]]
        if window_lines:
            section = flat[lo][0]
        else:
            section = ""
        new_windows.append({
            "index": windows[i]["index"],
            "thirds": windows[i]["thirds"],
            "frames": windows[i]["frames"],
            "start": windows[i]["start"],
            "len": windows[i]["len"],
            "lines": window_lines,
            "section": section,
        })
    return new_windows


# --- fallback_scenes ---

def fallback_scenes(n, style_note=""):
    """Return n fallback scene dicts."""
    base_scene = "in a bright, colourful music-video set"
    if style_note:
        base_scene += ", " + style_note.strip()
    return [
        {
            "scene": base_scene,
            "motion": SECTION_FALLBACK_MOTIONS[i % len(SECTION_FALLBACK_MOTIONS)],
        }
        for i in range(n)
    ]


# --- build_scene_request ---

def build_scene_request(windows, style_note="", has_person=True):
    """Return (system, user) strings for a chat model request.

    N = number of windows.
    """
    n = len(windows)
    system = (
        "You write one scene per shot of a music video for a song; "
        "output ONLY a JSON object "
        '{"scenes": [{"scene": ..., "motion": ...}, ...]}'
        f" with EXACTLY {n} entries, in order; "
        '"scene" is where the singer is and what surrounds them, '
        "one concrete visual sentence, no camera jargon; "
        '"motion" is what they do while singing, one sentence; '
        "scenes should be vivid and varied and may be wild or funny, "
        "and should react to the lyric lines of their shot; "
        "the same person appears in every shot "
        "(do not describe their face or clothes); "
        "no text, logos or captions in the picture."
    )

    user_parts = []
    if style_note:
        user_parts.append(style_note)
    for w in windows:
        sec = w.get("section", "") or "instrumental"
        lines_joined = " / ".join(w.get("lines", [])) or "instrumental"
        user_parts.append(f"{w['index'] + 1}. [{sec}] {lines_joined}")
    user = "\n".join(user_parts)

    return system, user


# --- parse_scene_reply ---

def parse_scene_reply(reply, n):
    """Parse chat model reply into list of n {"scene","motion"} dicts.

    Accepts JSON wrapped in ```json fences or surrounded by prose.
    Returns None when JSON is invalid or count is not exactly n.
    Strips and cuts scene/motion to 300 characters. Never raises.
    """
    try:
        # Find outermost {...}
        first = reply.find("{")
        last = reply.rfind("}")
        if first == -1 or last == -1 or last < first:
            return None
        json_text = reply[first : last + 1]
        data = json.loads(json_text)
    except (json.JSONDecodeError, ValueError):
        return None

    scenes = data.get("scenes", [])
    if not isinstance(scenes, list) or len(scenes) != n:
        return None

    result = []
    for entry in scenes:
        scene = entry.get("scene", "")
        motion = entry.get("motion", "")
        if not isinstance(scene, str) or not isinstance(motion, str):
            return None
        scene = scene.strip()
        motion = motion.strip()
        if not scene or not motion:
            return None
        result.append({
            "scene": scene[:300],
            "motion": motion[:300],
        })

    return result


# --- write_scenes ---

def write_scenes(windows, chat=None, style_note="", has_person=True):
    """Return (scenes, source).

    source is "helper" or "fallback".
    Retries exactly once on failure. Never raises.
    """
    n = len(windows)
    system, user = build_scene_request(windows, style_note, has_person)

    # Try chat
    if chat is not None:
        try:
            reply = chat(system, user)
            result = parse_scene_reply(reply, n)
            if result is not None:
                return result, "helper"
        except Exception:
            pass

        # One retry
        try:
            reply = chat(system, user)
            result = parse_scene_reply(reply, n)
            if result is not None:
                return result, "helper"
        except Exception:
            pass

    # Fallback
    return fallback_scenes(n, style_note), "fallback"


# --- plan_line ---

def plan_line(windows, seconds_per_shot=65):
    """Return a plain sentence describing the plan for the person."""
    n = len(windows)
    mean_shot_seconds = round(
        sum(w["len"] for w in windows) / n
    )
    minutes = max(1, round(n * seconds_per_shot / 60))

    shot_word = "shot" if n == 1 else "shots"
    second_word = "second" if mean_shot_seconds == 1 else "seconds"
    minute_word = "minute" if minutes == 1 else "minutes"

    each = "" if n == 1 else " each"
    in_all = "" if n == 1 else " in all"
    return (
        f"{n} {shot_word} of about {mean_shot_seconds} {second_word}{each}, "
        f"roughly {minutes} {minute_word}{in_all} "
        f"(about {seconds_per_shot} seconds a shot on a 16 GB card, measured once); "
        f"say stop to change it."
    )
