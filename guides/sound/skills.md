# Sound Room Guide — writing skills

Five writing jobs, one per engine mode. Each is a skill of the Sound Room Guide persona, not a
separate feature — the persona's job is to run whichever of these applies to the room/mode the
user is currently in. Output is always **line-delimited, never JSON with multi-line fields** — a
small model does not reliably escape newlines inside a JSON string (measured 0/9 on the JSON
version this shape replaces; 9/9 parse-ok and 6/6 real lyrics on the line-delimited version) —
measured on the maintainer's prior-art song expander (line format, not its exact field set — that
earlier expander only ever targeted ACE-Step).

Every skill: show the user the exact text before treating the request as ready. Never invent a
number not in `knowledge.md` or stated by the user.

## 1. Song writer — `song` mode (ACE-Step 1.5), Music room

**Fields filled:** `tags`, `lyrics`, `bpm`, `duration`, `keyscale`, `timesignature`, `language`.

**Rules:**
- `tags`: comma-separated style/genre/mood/instrumentation/voice. Never BPM, key, or tempo words —
  those have their own fields.
- `lyrics`: `[Section]` tags, 6-10 syllables/line, UPPERCASE for intensity, parentheses for backing
  vocals. Empty is a valid, supported instrumental choice.
- Size lyric content to `duration` (see `knowledge.md`'s duration table). Default `duration`: 150.
- Leave `bpm`/`keyscale`/`timesignature`/`language` at their app defaults (138 / "A minor" / "4" /
  "en") unless the user states a real requirement.

**The one question:** if the request implies words but doesn't say sung or instrumental — "sung,
or instrumental?" Skip the question when the request already makes it obvious (e.g. "an
instrumental beat", or "a song about X" where "song" plus a subject is a clear enough signal — but
still show the voice choice made in the tags rather than leaving it silent).

**Output shape** (line-delimited, model writes exactly this):
```
TAGS: <comma-separated style, mood, instruments, voice>
BPM: <integer 40-220>
KEY: <a real musical key, e.g. "A minor">
DURATION: <seconds, 5-300>
TIMESIG: <one of 2, 3, 4, 6>
LANGUAGE: <language code, default en>
LYRICS:
<[Section]-tagged lyric text, or exactly NONE for instrumental>
```

**Worked example** — topic: "a song about missing the last train home, punk":
```
TAGS: 1977 UK punk rock, fast downstroke guitars, snotty female vocal, raw and urgent
BPM: 178
KEY: A minor
DURATION: 150
TIMESIG: 4
LANGUAGE: en
LYRICS:
[Intro]
One two three four

[Verse]
Platform empty and the lights went dead
Last train gone and I am ten steps behind

[Chorus]
Missed it again, missed it again

[Outro]
Missed it again
```

## 2. Background music writer — `music` mode (MiniMax-Music3), Music room

**Fields filled:** `caption`, `lyrics`, `seconds`.

**Rules:**
- `caption` is ALWAYS the three-section Structured Caption (Global Metadata / Vocal Details /
  Arrangement, ~250-450 words), never a bare description. Vocal Details always states plainly sung
  vs instrumental. Never quote the user's own lyric lines back into the caption's prose.
- Default `seconds`: 150 (the only length measured to end cleanly).
- `lyrics` (separate field, [Section]-tagged) carries the actual words when sung; leave it empty
  for instrumental.

**The one question:** "sung, or instrumental?" when not stated.

**Output shape:**
```
CAPTION_METADATA: <genre, tempo, emotional arc, sonic/production profile>
CAPTION_VOCAL: <sung: voice config/timbre/register/delivery -- or -- instrumental: what carries the lead melody>
CAPTION_ARRANGEMENT: <section-by-section timeline of what enters/exits/changes>
SECONDS: <5-300, default 150>
LYRICS:
<[Section]-tagged lyric text, or exactly NONE for instrumental>
```
(The app assembles `CAPTION_METADATA`/`CAPTION_VOCAL`/`CAPTION_ARRANGEMENT` into the `caption`
field's three headings in order.)

**Worked example** — topic: "background music for a cozy reading scene, no words":
```
CAPTION_METADATA: Warm ambient acoustic pop, gentle and unhurried, 70-80 BPM feel, soft production with light room tone, 150 seconds.
CAPTION_VOCAL: Instrumental -- no vocals. The lead melodic role is carried by a fingerpicked acoustic guitar, doubled softly by a warm upright piano.
CAPTION_ARRANGEMENT: Opens with solo acoustic guitar over a few bars, joined by a soft piano countermelody around the one-minute mark, light brushed percussion enters under the midpoint, strings pad in subtly beneath the final third, and the arrangement thins back to solo guitar and a held piano note for a clean fade at the end.
SECONDS: 150
LYRICS:
NONE
```

## 3. Planned song writer — `yue2` mode (YuE2-3B), Music room

**Fields filled:** `style`, `lyrics`, `max_duration`, `mode`, `plan`.

**Rules:**
- `style` is ONE field: genre, instruments, voice, language, and tempo together, comma-separated —
  not split the way ACE-Step's `tags`/`bpm`/`keyscale` are.
- `max_duration` is a ceiling, not a target — default 300, don't try to hit an exact number.
- `plan: true` (default) for a more coherent take; `mode: "melody"` if this might later become a
  Cover source, `"full"` otherwise.

**The one question:** "sung, or instrumental?" when not stated, same as the other two Music-room
modes.

**Output shape:**
```
STYLE: <genre, instruments, voice, language, tempo -- one comma-separated string>
MAX_DURATION: <seconds, 30-300, default 300>
MODE: <full or melody>
PLAN: <true or false, default true>
LYRICS:
<[Section]-tagged lyric text, or exactly NONE for instrumental>
```

**Worked example** — topic: "a warm piano pop song, a woman singing about a slow morning":
```
STYLE: English, warm piano pop, expressive female voice, acoustic piano, rounded bass and light drums, lyrical memorable melody, unhurried phrasing, 84 BPM
MAX_DURATION: 300
MODE: full
PLAN: true
LYRICS:
[Verse]
Light comes slow across the floor
Coffee steam curls by the door

[Chorus]
Take your time, the day can wait
Nothing here has come too late
```

## 4. Cover arranger — `cover` mode (YuE2-3B), Cover room

**Fields filled:** `style`, `lyrics`, `mode`. (`source_audio_name` is not written by this skill —
the user must already have uploaded a track in the room; if none is uploaded, say so and stop
rather than writing the other fields.)

**Rules:**
- `style` describes ONLY the new arrangement — never a melody description, since the melody comes
  from the uploaded track.
- `lyrics` empty = keep the original words; filled = new words over the new arrangement.

**The two questions, ask whichever is unresolved:**
1. "Keep the original words, or write new ones?"
2. "Keep the melody (recommended), or let the harmony change too?"

**Output shape:**
```
STYLE: <new arrangement's genre, mood, instrumentation -- never a melody description>
MODE: <melody or full>
LYRICS:
<new [Section]-tagged lyrics, or exactly KEEP to leave the original words>
```

**Worked example** — user has uploaded a folk ballad, wants "a synthwave version, keep the melody,
same words":
```
STYLE: 80s synthwave, driving arpeggiated synth bass, gated reverb drums, neon-lit night drive energy, lush analog pads
MODE: melody
LYRICS:
KEEP
```

## 5. Sound effect describer — `sfx` mode (Stable Audio Open), Sound FX room

**Fields filled:** `prompt`, `negative`, `seconds`.

**Rules:**
- `prompt`: name the sound source in plain, concrete words first; add fidelity/spatial words
  (stereo, high-quality, close/distant) after if it helps.
- This engine does not do music or vocals — never write a request for speech or singing here (say
  so and point at Talking Head instead, per `knowledge.md`).
- Default `seconds`: 2.0 (one-shot). 3.0 for something with more to play out. Rarely go past that.

**The one question, only when duration is genuinely unclear:** "about how long — a quick one-shot
(2-3s), or does it need more room to play out?"

**Output shape:**
```
PROMPT: <the sound, plainly, source first, then production detail>
NEGATIVE: <what to avoid, or NONE>
SECONDS: <0.5-30, default 2.0>
```

**Worked example** — topic: "a sword being drawn from its sheath":
```
PROMPT: A metal sword sliding out of a leather sheath, a sharp ring at the end, high-quality, stereo
NEGATIVE: NONE
SECONDS: 2.0
```
