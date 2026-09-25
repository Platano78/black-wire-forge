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
BPM: <NONE, unless the request states a tempo: integer 40-220>
KEY: <NONE, unless the request names a key: a real key, e.g. "E minor">
DURATION: <seconds, 5-300; the form's own duration when set, else 150>
TIMESIG: <NONE, unless stated: one of 2, 3, 4, 6>
LANGUAGE: <NONE, unless stated: a language code>
LYRICS:
<[Section]-tagged lyric text, or exactly NONE for instrumental>
```

**Worked example** — topic: "a song about missing the last train home, punk" (150 s, so at least two
verses and two choruses with words, and a voice in the tags; BPM/key/time signature/language were not
stated, so they stay NONE and the form keeps its own):
```
TAGS: 1977 UK punk rock, fast downstroke guitars, raw shouted female vocals, snotty and urgent
BPM: NONE
KEY: NONE
DURATION: 150
TIMESIG: NONE
LANGUAGE: NONE
LYRICS:
[Intro]
One two three four, go

[Verse]
Platform empty and the lights went dead
Last train gone and I am ten steps behind

[Chorus]
Missed it again, missed it again
The last train home is leaving without me

[Verse]
Counting coins beneath a broken sign
Walking home along the railway line

[Chorus]
Missed it again, missed it again
The last train home is leaving without me

[Outro]
Missed it again, I missed it again
```
When sung or instrumental is unclear, the whole reply is one line: `QUESTION: Sung, or instrumental?`
(The app's own writer for this mode lives in `engines/audio.py`, `SONG_WRITER_PROMPT`.)

## 2. Background music writer — `music` mode (MiniMax-Music3), Music room

**Fields filled:** `caption`, `lyrics`, `seconds`. (The app's own writer for this mode is
`engines/audio_writers/music.txt`; its check is `music_check` in `engines/audio.py`.)

**Rules:**
- `caption` is ALWAYS the three-section Structured Caption, written as **plain text**: the labels
  Global Metadata, Vocal Details and Arrangement each on a line of their own, full sentences under
  each, about 250-450 words. No Markdown: no asterisks, no `#` headings, no bullets. The model reads
  every character as description.
- **Only what can be heard**: genre, era, tempo feel, drums, bass, instruments, the voice, the mix,
  the arrangement. Never places, rooms, smells, weather, objects or the story: the subject goes in
  the lyrics. (The vendor's own template has an "Application Scenarios & Imagery" line; it is left
  out for that reason.)
- Vocal Details names the voice that performs the words (sung or rapped); an instrumental opens
  Vocal Details with "Instrumental, no vocals." and names the lead instrument.
- `lyrics` is **required** when it is sung or rapped: real words under every `[Section]` tag, sized
  to `seconds` with the song writer's table. With no lyrics this model makes no real words. Only
  words to be performed: no stage directions in brackets or parentheses.
- `seconds`: 150 unless the user gives a length (the only length measured to end cleanly).

**Questions, one per turn, only when still open:** sung, rapped or instrumental? (OPTIONS: Sung |
Rapped | Instrumental); who performs it (A male voice | A female voice | A duet); how long (2.5 / 3.5
/ 5 minutes). In the live trial the brain asked the first when it was genuinely open and the second
sometimes; it never asked the length and chose 150, naming its choices in NOTE.

**Output shape:**
```
SECONDS: <5-300, 150 unless given>
NOTE: <the choices it made itself>
CAPTION:
Global Metadata
<plain sentences>
Vocal Details
<plain sentences>
Arrangement
<plain sentences, section by section>
LYRICS:
<[Section]-tagged words, or exactly NONE for an instrumental>
```
CAPTION and LYRICS both run over several lines; each ends at the next key line.

**The check (also at Make):** a Markdown caption; a missing section; a caption under 200 words; a
word for something that cannot be heard (a smell, imagery); Vocal Details describing a singer or
rapper with no lyrics ("this will likely come out with no real words"); lyrics with an instrumental
Vocal Details; lyrics too short for `seconds`.

## 3. Planned song writer — `yue2` mode (YuE2-3B), Music room

**Fields filled:** `style`, `lyrics`, and `max_duration` / `mode` only when the user states them
(`plan` is left to the form). Writer: `engines/audio_writers/yue2.txt`, check `yue2_check`.

**Rules:**
- `style` is ONE comma-separated line: language (English unless asked), genre and era, mood,
  instruments, and the **voice** whenever there are lyrics.
- `max_duration` is a ceiling, not a target; the lyrics are sized to it (300 when unset) with the
  song writer's table, since the song lasts about as long as its words.
- `mode: melody` only when the user says it will become a cover source.

**The one question:** "Sung, or instrumental?" when the request does not say.

**Output shape:**
```
STYLE: <one line, voice included when sung>
MAX_DURATION: <NONE unless stated>
MODE: <NONE unless stated>
NOTE: <the choices it made itself>
LYRICS:
<[Section]-tagged words, or exactly NONE for an instrumental>
```

**The check (also at Make):** lyrics with no voice in `style`; too few sections for `max_duration`.

## 4. Cover arranger — `cover` mode (YuE2-3B), Cover room

**Fields filled:** `style`, `lyrics`, and `mode` only when the user states it. Writer:
`engines/audio_writers/cover.txt`, check `cover_check`.

**First, a track.** With no uploaded Source track, the guide does not ask anything: it answers
with one plain sentence to upload the song first (the writer's `needs`; the brain is not called).

**The track gives the tune, never its words.** The engine reads the melody (and, in `full` mode,
the harmony) from the upload; it sings only what is in `lyrics`. Empty lyrics are an instrumental
cover, not "the original words".

**Rules:**
- `style` describes ONLY the new arrangement plus the voice that sings: never the melody.
- Keep the original words: when Lyrics already holds them, the writer answers `KEEP` and the field
  is left as it is; when it does not, it asks the user to paste them into the Lyrics box and press
  Done.
- New words: about what the user says (it asks what they are about when nothing says).

**Questions:** "Keep the original words, or write new ones?" (OPTIONS: Keep the original words |
Write new words | No words), then the paste or topic question when needed.

**Output shape:**
```
STYLE: <the new arrangement, voice included when there are words>
MODE: <NONE unless stated>
NOTE: <the choices it made itself>
LYRICS:
<new [Section]-tagged words, or exactly KEEP, or exactly NONE for no words>
```

**The check (also at Make):** lyrics with no voice in `style`; a voice in `style` with no lyrics
("this will likely come out with no real words").

## 5. Sound effect writer — `sfx` mode (Stable Audio Open), Sound FX room

**Fields filled:** `prompt`, `seconds`, and `negative` only when the user says what to keep out.
Writer: `engines/audio_writers/sfx.txt`, check `sfx_check`.

**Rules:**
- `prompt`: ONE sound, its source first in plain concrete words, then material and size, the space,
  and how it plays out; optional "high-quality, stereo" at the end. English.
- No music, melody, singing or spoken words. Human sounds with no words are fine (footsteps, a crowd
  murmuring, applause).
- `seconds`: 2 for a one-shot, 3 when it plays out, longer only when the sound itself lasts (rain,
  an engine idling) or the user gives a length, at most 10 unless asked. In the live trial the brain
  kept the form's 2 s for rain; set it by hand for a lasting sound.

**Not this room:** a request for words spoken gets one line pointing to the Talking Head room; a
request for music gets one line pointing to the Music room. Otherwise it asks only when the sound
itself is unnamed ("a sound for my game"), offering 3-4 sounds.

**Output shape:**
```
SECONDS: <0.5-30>
NEGATIVE: <NONE unless stated>
NOTE: <the choices it made itself>
PROMPT: <the sound, on one line>
```

**The check (also at Make):** words asking for speech or singing ("saying", "narration", "singing",
...), with the room to use instead.
