# Sound craft knowledge — for the Sound Room Guide

Rule-per-line, grouped by the **moment** the user is in — idea → words → settings → the render →
judging the result — the same shape as the Film Room Guide's knowledge.md (idea → script → shots →
takes → cut), not grouped by engine first. Each moment spans all three rooms (Music, Cover, Sound
FX) and names which room/engine a rule belongs to inline. Each line is labelled **observed** (read
at the cited source, or measured by the maintainer) or **inferred** (craft generalisation, or a claim
carried over from a different model/version/product). Terms in `code font` are the app's own field
ids and room names. In-house sources (the project's own vendored docs, pack code, and measured
notes) were read before the web; the web fills only what no in-house source covers — see "Research
log" at the end for exactly which claims came from where.

## IDEA — before there's a request at all

- The `music` room (`blurb`: "songs, scores and anything sung") runs one of three engines by mode:
  `song` (ACE-Step 1.5), `music` (MiniMax-Music3), `yue2` (YuE2-3B). `cover` (YuE2-3B) has its own
  room; `sfx` (Stable Audio Open) has its own room too. — **observed**, `rooms.json`,
  `engines/audio.py` `mode_rooms`.
- Each engine needs its own prompt shape — there is no one style/lyrics format that works across
  all three Music-room engines, let alone Cover and Sound FX too. Reusing one engine's expander on
  another measured a mean score of 0.632 on its own engine and 0.011 fed to a different one. —
  **observed**, the maintainer's music-lane audit (2026-09-21).
- **No spoken-word / voice-acting / narration engine lives in this group.** All five modes here are
  music or sound-effect models; none do text-to-speech or scripted dialogue. A request for a voice
  actor reading a line, a narration, or a talking-head script belongs in the app's own **Talking
  Head** room (a different room group, a different engine) — never faked with a lyrics or `sfx`
  field. — **observed**, `rooms.json` (no voice-acting room in the SOUND group), and Stable Audio
  Open's own model card stating it cannot generate realistic vocals at all (see "WORDS" below).
- One mastering chain (EQ + compressor, target −14 LUFS / −1 dBTP) is shared across all five modes
  behind a single `master` checkbox, off by default. See "SETTINGS" below for why it defaults off.
  — **observed**, `engines/audio.py` `attach_mastering`.

## WORDS — turning a topic into the text each engine actually reads

**The one rule that matters most: a voice is a choice, not a default.** The maintainer's own ear,
2026-09-24, same shipped "Try this" lyrics (5 lines, 150 s), seeds 1111/2222/3333: ACE-Step tags
naming no voice ("warm acoustic pop, gentle drums, sunny afternoon") came back **instrumental
3/3**; the same tags plus "clear female vocals, singing" came back **sung 3/3**. This is the
single highest-value fix in this guide. The failure it replaces, seen twice before the ear-gate:
2026-09-19 "Expander: skipped" when `[Section]` lyrics were given with no voice named; 2026-09-18
"90 s of instrumental before any vocal" from short lyrics over a long duration — the same root
cause seen from two angles (no voice named, or not enough lyric content for the length asked). —
**observed**, the maintainer's listening A/B (2026-09-24).
The fix generalises to every engine with a lyrics field in this group (`song`, `music`, `yue2`, and
`cover` when new words are wanted): name a voice in the prompt whenever the user wants words sung,
unless they explicitly asked for instrumental. **The one question to ask when this is ambiguous:
"sung, or instrumental?"** — never guess either way, but also never ask again once the request has
already answered it (e.g. "I want a woman singing"). — **inferred** for `music`/`yue2`/`cover`
specifically (no dedicated A/B run on those three yet, only on `song`); **observed** for the
question-asking rule itself, the same A/B notes.

Per-engine text shape, in the moment order a user actually fills them:

- **`song` (ACE-Step 1.5, Music room).** `tags`: comma-separated style, era, mood, instrumentation,
  vocal character — carry every concrete detail, don't generalise "90s techno" down to "techno".
  **Never put BPM, key, or tempo words in `tags`** — those are dedicated fields, and mixing them in
  is documented to hurt results. — **observed**, ace-step/ace-step-skills (GitHub), the ACE-Step
  songwriting skill guide, "Don't put BPM/key/tempo in Caption". Combine style + emotion +
  instruments + timbre; avoid stacking contradictory genres in one caption — if a genre shift is
  wanted over the song's length, write it as a progression ("starts soft strings, turns to rock by
  the chorus"), not both at once. — **observed**, same source, "Caption Writing Principles".
  `lyrics` uses `[Verse]`/`[Chorus]`/`[Bridge]`/`[Intro]`/`[Outro]`/`[Instrumental]`/`[Guitar Solo]`
  structure tags, `[raspy vocal]`/`[whispered]`/`[falsetto]`/`[harmonies]` vocal-control tags, and
  `[building energy]`/`[explosive]` energy tags, combined sparingly with `-`
  (`[Chorus - anthemic]`, not a five-part stack). — **observed**, same source, "Structure Tags".
  Lyric-line craft: 6–10 syllables a line (same-role lines within ±1–2 of each other); UPPERCASE
  reads as shouted/intense; parentheses mark backing vocals; a blank line separates sections.
  Avoid adjective-stacking with no image, inconsistent rhyme, lines too long to sing in one breath,
  and mixing metaphors mid-song — one core metaphor, explored. — **observed**, same source, "Lyric
  Writing Tips" / "Avoiding 'AI-flavored' Lyrics".
- **`music` (MiniMax-Music3, Music room).** `caption` must ALWAYS be a three-section Structured
  Caption, in order — **Global Metadata** (genre/subgenre, tempo, emotional arc, production
  profile; exact BPM/key only when explicit or clearly useful), **Vocal Details** (sung: the lead
  voice's configuration/timbre/register/delivery/backing vocals; instrumental: say so plainly and
  name what carries the lead melody instead), **Arrangement** (a section-by-section timeline of
  what enters/exits/changes) — ~250–450 words total, never a bare description (a bare one truncated
  mid-phrase; the Structured Caption version rendered clean with the clearest lyrics of the three
  engines tried, measured). Never quote/paraphrase the user's own lyric lines into the caption's
  prose. Write it as **plain text** (the labels on their own lines, no Markdown asterisks or
  headings) and describe **only what can be heard**; sung or rapped words always go in `lyrics`.
  A Markdown caption padded with a setting and a smell, with empty lyrics, rendered a rap request
  as something the maintainer called "hot garbage" (2026-09-24). — **observed**, the vendor's
  example template (plain labels), the maintainer's listening report. — **observed**, MiniMax-AI/MiniMax-Music3 (GitHub), the caption-rewriter skill's
  "Output Contract", `engines/audio.py` `music_graph` docstring. Vendor's own framing: this is a
  **five-minute full-song vocal model with expressive vocals**, not an "ambience generator" — the
  app's own old code comment calling it that described the old, wrong wiring, not the model. —
  **observed**, the maintainer's music-lane audit (2026-09-21).
- **`yue2` (YuE2-3B, Music room).** `style` is ONE field carrying genre, instruments, voice,
  language, AND tempo together — this engine's own documented convention, not a limitation to work
  around. Vendor's own shipped example: `"English, warm piano pop, expressive female voice,
  acoustic piano, rounded bass and light drums, lyrical memorable melody, unhurried phrasing, 88
  BPM"`. `lyrics` uses the same `[Section]` tags as ACE-Step. — **observed**,
  multimodal-art-projection/YuE (GitHub), the YuE2 generation guide's "Style, lyrics, and
  planning" section and its shipped example song request.
- **`cover` (YuE2-3B, Cover room).** `style` describes ONLY the new arrangement — the tune comes
  from the uploaded source track via the engine's own melody transcription, not from this text.
  — **observed**, `engines/audio.py` `cover_graph` docstring / `prompt_guides["cover"]`. The track
  gives the tune, **not its words**: the graph turns the upload into ABC notation (melody, and
  harmony in `full` mode) and sings only the `lyrics` text it is given — **observed**, `cover_graph`
  (SheetSage2AudioToABC → YuE2GenerateMusic's separate `lyrics` input). So empty `lyrics` is an
  instrumental cover, and keeping the original words means putting them in `lyrics` — **inferred**
  from that wiring, not heard on a render. **The one question here is "keep the original words, or
  write new ones?"** — **inferred**, applying the Music-room voice rule to a cover's equivalent
  choice; not itself A/B tested on this engine.
- **`sfx` (Stable Audio Open, Sound FX room).** Name the sound source in plain, concrete words
  first ("a wooden door creaking open", "a hammer hitting a wooden surface" — the model card's own
  two examples), then add production/spatial detail (stereo, high-quality, close/distant) if it
  helps. This model is trained for **sound effects and field recordings, not music**, and is
  explicitly **"not able to generate realistic vocals"** — never send it a request for spoken words,
  singing, or dialogue. English only — the card says it "will not perform as well in other
  languages." — **observed**, Stability AI, `stabilityai/stable-audio-open-1.0` model card,
  https://huggingface.co/stabilityai/stable-audio-open-1.0. Adding fidelity/production words
  ("high-quality", "44.1kHz", "stereo") after the source description is a technique documented for
  Stable Audio 2.0, a later, closed, non-Open model in the same product family — carried over here
  as **inferred**, not observed for Open 1.0 specifically, since the underlying "describe the sound,
  then its recording quality" idea is generic prompting craft rather than something tied to the
  newer model's own features. — **inferred**, https://www.jordipons.me/on-prompting-stable-audio/.

## SETTINGS — the knobs, and which numbers are actually safe

- **Size the lyrics to the duration.** Intro/outro ~5–10 s each; instrumental sections ~5–15 s
  each; two verses + two choruses needs 120–150 s minimum; add a bridge and it's 180–240 s minimum;
  a full song with intro/outro runs 210–270 s. Slower BPM (60–80) needs MORE duration for the same
  lyric content than fast BPM (150–180). When unsure, write more sections rather than fewer — too
  little lyric content for the length asked has rendered as a long stretch of instrumental before
  the first line, measured on this app (see "IDEA"/"WORDS" above). — **observed**, vendored
  ACE-Step songwriting guide, "Duration Calculation"; **inferred** for `music`/`yue2` — no
  equivalent vendor sizing table exists for those two, the ACE-Step table is the best available
  craft heuristic for them.
- **`song` `duration`:** the maintainer's measured sweet spot is **150 s** — the `full-verse`
  preset, clean ending at 52 s render time (ACE-Step resolves its ending 18/18 times measured, the
  only one of the three Music-room engines that reliably does). Below ~60 s or above ~240 s the
  render gets slower per second of audio. — **observed**, `engines/audio.py` `song_graph` preset
  comment, measured directly against the live ACE-Step engine by the maintainer, 2026-09-21.
- **`music` `seconds`:** set to **150** by default — both measured shorter lengths (30 s, 90 s) cut
  off mid-phrase (2/2 abrupt endings); this engine does not compose to fit the requested length, it
  plays until the clock runs out. **Never silently pass through a shorter duration the user asked
  for** — name the truncation risk and use 150, or ask, rather than guessing it'll be fine. —
  **observed**, measured directly against the live MiniMax-Music3 engine by the maintainer,
  2026-09-21 ("ACE composes to length, Music3 and a cramped YuE2 just stop"), `engines/audio.py`
  `music_graph` `presets["music"]`.
- **`yue2` `max_duration`:** a ceiling the model can finish short of, not a target — measured: a
  360 s ask returned 354.9 s with a clean ending (the model reached its own natural end before the
  cap), while a too-tight cap (previously 120 s) visibly clipped songs, which is why the app's own
  default was raised 120→180→300 over three fixes. Give it room rather than dialing in an exact
  number. — **observed**, `engines/audio.py` `yue2_graph` docstring, measured directly against the
  live YuE2 engine by the maintainer, 2026-09-21. **This field belongs to `yue2` only** — Cover's own
  exposed fields are `source_audio_name`/`style`/`lyrics`/`mode`, with no duration control at all;
  Cover's own internal `max_duration` (a fixed 180.0 the graph uses under the hood) is not a field
  the Guide can name or change. — **observed**, `engines/audio.py` `cover_graph` field list.
- **`sfx` `seconds`:** keep it short — the app's own measured presets are 2 s (one-shot foley
  default) and 3 s (a door-slam take, rendered in 16 s), both far under the field's 30 s ceiling.
  There's no measured benefit to a long SFX render; the model card itself frames this as a
  short-clip tool. Ask "about how long — a quick one-shot (2–3 s), or does it need more room to
  play out?" only when the described sound genuinely has duration built into it (an engine starting
  up, something falling for a while) — most single-object sounds don't need to ask. — **observed**,
  `engines/audio.py` `sfx_graph` `presets["sfx"]`; **inferred**, the question threshold.
- **Which engine, when unstated:** default to `song` (ACE-Step) — fastest of the three, and the
  only one measured to end cleanly at any length. Only pick `music` (MiniMax-Music3) for something
  it's actually for (a five-minute full song, a structured-caption request) or `yue2` for a
  planned-melody request — never switch engines on a guess. — **inferred**, a rule added after this
  guide's own live trial surfaced a silent wrong-engine pick.
- **Cover `mode`:** "melody" (the app's own field default AND its recommended choice) keeps the
  source's tune while the arrangement changes; "full" also imposes the source's original harmony.
  Ask rather than silently picking either one just because the request didn't say — it's a real
  choice with two real outcomes, not a value to fill in without checking. — **observed**,
  `engines/audio.py` `cover_graph` field hint + `presets["cover"]["keep-melody"]`.
- **Mastering, all five modes:** off by default **by design, not oversight** — both ACE-Step
  and YuE2 were ear-gated on *unmastered* output, and mastering audibly changes the sound; an
  ear-gate decision can only be moved by another ear-gate. Name that the toggle exists; never turn
  it on for the user or claim it'll sound better — that's a listening call. — **observed**,
  `engines/audio.py` `attach_mastering` comment.

## THE RENDER — what happens once "Make" is pressed

- Every request becomes a job with a status: queued → running → done / error / interrupted. The
  Guide only ever knows this status and the plain-English error sentence on failure — never a
  description of the audio itself. — **inferred**, generalising the app's own job model (same shape
  as every other room in this app) to this group; no group-specific job-status doc exists.
- **Relative speed, if the user is choosing between Music-room engines:** YuE2 is fastest
  (~0.5–0.8× real time), ACE-Step next (~0.3× real time), MiniMax-Music3 is far slower
  (~6.7–6.9× real time — nearly 7 minutes of render for one minute of audio). Mention this if speed
  matters to the user's choice; never refuse or discourage a choice on this basis alone, it's
  theirs to make. — **observed**, measured directly against all three live engines by the
  maintainer, 2026-09-21 ("Speed").
- **How each engine ends, when it ends wrong:** ACE-Step resolves its ending cleanly every time
  measured (18/18). MiniMax-Music3 does not compose to fit the requested length — it stops when the
  clock runs out, which is why its `seconds` default matters so much (see "SETTINGS"). YuE2 can cut
  off short at a tight `max_duration` but ends cleanly once given room. — **observed**, same source,
  "ACE composes to length. Music3 and a cramped YuE2 just stop."

## JUDGE THE RESULT — what I can't judge, say so, don't guess

- **I cannot hear a render, and neither can this app's own automated check.** The maintainer built a
  lyric-fidelity check using two whisper.cpp models to transcribe renders and score recall against
  the lyrics sheet — the two models disagreed by up to **0.904** (on a 0–1 scale) on the same audio,
  in both directions, and the maintainer directly heard clear lyrics in a recording both models scored
  **0.000**. It cannot tell "no vocals were sung" from "vocals were sung that a speech-transcription
  model couldn't parse over a dense backing track." — **observed**, measured directly on the
  maintainer's own render output, 2026-09-21 ("Lyric fidelity — the instrument is NOT reliable").
- So: the Guide never claims a render sounds sung, instrumental, clean, or wrong. It only knows
  what request was sent and what the job status reports. If the user says a render came back wrong,
  the fix is a better prompt (a named voice, clearer section tags, more lyric content, a longer
  duration, a rewritten caption) — never a re-check the Guide can't actually perform. — **inferred**
  (combining the whisper finding above with the same tool-honesty boundary the Film Room Guide
  holds for pictures it wasn't sent — see `guides/film/knowledge.md`, "What the machine cannot
  judge" — two observed facts connected into one rule, per this file's own observed/inferred
  definition at the top).
- I also can't judge whether a caption/tags/style string will render well before it's tried — every
  duration/quality number in this file is a measured *tendency*, not a guarantee for a specific
  request. Say what's measured, not what's certain. — **inferred**, standard epistemic hygiene for
  a craft guide grounded in a small number of trials.

## HOW PAID TOOLS DO THIS — claimed/documented, not independently verified

A short survey of how three commercial music/SFX tools turn a topic into the fields their models
need, for borrow-worthy interaction patterns (never for copying wording — see the app's own
non-commercial/licence rules elsewhere in this repo).

- **Suno** splits its Custom Mode into two panels — a Style box and a Lyrics box — and documents a
  5-part style formula: genre+subgenre, mood/energy, vocal style/character, key instruments +
  production quality, tempo/BPM, as 8–15 short tags (15–30 words). It documents that style and
  lyrics are read together and a mismatch between them ("a melancholy lyric with an upbeat style
  prompt") produces incoherent output — match the two. It also documents that each "extend" is a
  fresh instruction with no memory of the original style prompt, so a user must re-paste their tags
  each time or the track drifts. — **claimed/documented**, https://hookgenius.app/learn/
  suno-prompt-guide-2026/, https://jackrighteous.com/en-us/blogs/guides-using-suno-ai-music-
  creation/where-to-put-your-suno-prompt-guide.
  **Borrow:** the "keep style and lyric mood consistent with each other" check is a real, cheap
  validation this guide's skills could add before showing fields to the user (not built into
  `skills.md` yet — an open idea, not a promise). **Don't copy:** Suno's own tag wording/format —
  this app's engines each have their own documented shape (see "WORDS" above), and Suno's isn't one
  of them.
- **Udio** documents the same three-part shape (short comma-separated genre/mood/voice/tempo
  description) plus a separate custom-lyrics box with `[Section]` tags and vocal-effect tags
  (`[Scream]`, `[Chorus]`, `[Guitar Solo]`) and parentheses for backing vocals — close to ACE-Step's
  own conventions, though not the same model. It documents a "guidance tags" picker (typed with `/`)
  as a discoverability aid for its own tag vocabulary, and a repeat-key-words-to-emphasize technique.
  — **claimed/documented**, https://help.udio.com/en/articles/10716541-prompt-like-a-master,
  https://help.udio.com/en/articles/11166249-level-up-your-creations-with-guidance-tags.
  **Borrow:** the general shape (short style description + separate structured lyrics box) is
  already this app's own shape for `song`/`yue2` — confirms the app's field layout matches an
  established pattern, nothing new to add. **Don't copy:** the specific tag vocabulary — it's
  Udio's own model's vocabulary, not ACE-Step's or YuE2's.
- **ElevenLabs Sound Effects** documents "simple" one-line prompts ("glass breaking") versus
  "detailed" prompts that add material/size/environment/distance, temporal shape ("starts quietly
  and builds to a crash"), acoustic space ("in a large cathedral"), and recommends generating
  individual sounds separately rather than one prompt for a whole sequence of sounds. It exposes a
  duration field (auto or manual, up to 30 s) and a "prompt influence" slider. — **claimed/
  documented**, https://elevenlabs.io/docs/eleven-creative/playground/sound-effects,
  https://help.elevenlabs.io/hc/en-us/articles/25735604945041-How-do-I-prompt-for-sound-effects.
  **Borrow:** "generate one sound per request, not a sequence" is already this app's own `sfx`
  design (one prompt, one short clip) — confirms rather than changes anything. The temporal-shape
  and acoustic-space vocabulary ("starts quietly and builds to...", "in a large cathedral") is a
  useful addition to how the Sound FX skill should coach a user past a bare one-line request; not
  yet folded into `skills.md`'s worked example, worth adding next pass. **Don't copy:** the
  "prompt influence" slider — Stable Audio Open's ComfyUI graph in this app has no such control
  exposed, and inventing one would violate the "never suggest a control that doesn't exist" rule.

## Research log

**In-house sources read first, before any web lookup:** the ACE-Step 1.5 songwriting guide and API
doc (vendored copies of each engine's own repo), the MiniMax-Music3 caption-rewriter contract and
README (same vendor dir), YuE2's generation guide and its one shipped example request (same vendor
dir), the maintainer's own 2026-09-21 capability sweep (full test, audit and measurements),
the maintainer's 2026-09-24 listening A/B and the persona-first design decision, the maintainer's music-lane audit (2026-09-21), and `engines/audio.py` itself
(every field, default, preset and docstring cited above was read from this file, not guessed).

**Web used only where no in-house source existed:** Stability AI's `stable-audio-open-1.0` model
card (no vendor copy of Stable Audio Open's own prompting guidance is mirrored in this project)
plus one adjacent (Stable Audio 2.0, not Open) prompting writeup by a Stability researcher, flagged
inferred/not-fully-applicable in the "WORDS" section above since it documents a different, later,
closed model in the same product family. The "HOW PAID TOOLS DO THIS" survey (Suno, Udio,
ElevenLabs Sound Effects) is web-only by nature — no in-house source describes competitor tools —
and every claim there is labelled claimed/documented with its URL, not folded into the rest of this
file as fact about this app's own engines.

**Could not confirm:** any A/B evidence for the "name a voice" rule on `music`/`yue2`/`cover`
specifically — only `song` (ACE-Step) has a maintainer-run trial; the extension to the other three
engines is a reasoned generalisation, marked inferred throughout. Also could not confirm current
Stable Audio Open-specific prompting guidance beyond its own model card.
