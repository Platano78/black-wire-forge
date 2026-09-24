# Film craft knowledge — for the Film Room Guide

Rule-per-line, grouped by the moment in Black Wire Forge (BWF) where it applies: idea → script →
shots → takes → cut. Each line is labelled **observed** (read at the cited source) or **inferred**
(my read connecting two observed facts, or general film-craft knowledge held with high confidence
but not pulled from a single citable passage today). Terms in `code font` are the app's own words —
the guide must speak in these, not generic film-school jargon.

## IDEA — before there's a `sequence`

- A short film is one change, not a plot: setup (what's normal) → turn (what breaks it) → button
  (the last image or line that lands the change). A 30-second film is its last five seconds — decide
  the ending early. — inferred, the project's own short-film method notes and
  the project's multi-shot continuity notes state this as house method for the project's own
  film pipeline (observed there, generalized here as craft advice, not H3-specific).
- Two characters is a practical ceiling for a solo maker: each one needs a look that survives every
  shot it appears in. One location is the practical ceiling too — a second location costs a whole new
  `set` reference plate and risks continuity. — observed, the project's short-film method notes §1, §3.
- Nine beats of 3–4.5 s makes a 30 s film. A beat with no change in it is not a shot — it's a held
  frame and reads as a mistake. — observed, the project's short-film method notes §2.
- "Planning costs cents, rendering costs real money" — decide the shot list before generating
  anything, because in BWF a shot is a `slot` that must be regenerated (a `take`) to see, not
  free. — documented (Kling Director Mode blog, as paraphrased/cited), a survey of paid AI-film tools §5 borrow #2, source https://kling.ai/blog/kling-video-3-0-ai-director-features-guide.

## SCRIPT — the storyboard's script lane, one `beat` per paragraph

- A `beat` is one paragraph of the script lane; a slugline (`INT.`/`EXT.`/`INT./EXT.`) makes a
  `scene` beat with no slot; a `film`/`picture`/`sound` beat gets exactly one `slot`. — observed,
  the Cutting Room's design spec §3.
- A slot's prompt is pre-filled **once** from its beat's text, then belongs to the user; editing the
  beat afterward never rewrites the prompt — it flags the slot **STALE ("script changed")** and offers
  **Copy beat into prompt**. — observed, spec §3, §1 slot-state table.
- No other AI-film tool documents what happens to an already-rendered shot when its script text
  changes afterward — every one describes only the forward path. BWF's STALE-on-edit + explicit
  copy-in (never silent rewrite) is not something a Guide can borrow phrasing for from a competitor;
  it should just explain the mechanic plainly. — observed (as an absence), a survey of paid AI-film tools
  §1.
- Write description and dialogue as you'd say them out loud, one idea per beat — the storyboard
  splits an imported script on blank lines (`import_script`), so a beat that runs two ideas together
  becomes one overloaded shot. — observed, spec §3 "Pasting a whole script... splits on blank lines".

## SHOTS — writing a shot, and what a video model actually needs

- **Coverage** = shooting/generating enough distinct shots of a scene (wide, medium, close, reaction)
  that the cut has choices; a scene told in one unbroken shot has no coverage and no cutting room. —
  inferred, standard editing-craft vocabulary (Murch-style continuity practice), not pulled from a
  single citation today.
- **Shot sizes** (wide/establishing, medium, close-up) each do a job: wide sets geography, medium
  carries the interaction, close carries the reaction or the detail that matters. Cutting in tighter
  on a beat is how the cut says "look here now." — inferred, standard craft.
- **180° rule**: pick an imaginary line between two subjects and keep every shot on the same side of
  it, or the audience's sense of who's where flips. — observed, https://learnaboutfilm.com/film-language/sequence/180-degree-rule/
  and https://www.cadrage.app/the-180-degree-rule-in-filmmaking/ (continuity-editing convention).
- **Eyeline match**: a shot of someone looking, cut to what they're looking at, sells "same room,
  same moment" even when the two shots were made completely separately — which is exactly BWF's
  situation, since every shot is a separate render. Matching shot size/distance/horizon between the
  two shots gives the match its best chance. — observed, https://www.studiobinder.com/blog/what-is-an-eyeline-match/ .
- **Cutting on action**: cut in the middle of a movement (a hand reaching, a door starting to open)
  rather than before or after it — the eye follows the motion across the cut and the join disappears.
  — observed, https://www.studiobinder.com/blog/what-is-continuity-editing-in-film/ ("cutting on
  action").
- **Jump cut**: two shots of the same subject less than ~30° apart in angle (or too similar in
  framing) read as a jarring skip rather than a cut — deliberate in some styles, a mistake in most. —
  observed, same StudioBinder continuity source (30°-apart guideline).
- BWF-specific: **name the camera against the plate, in every shot.** The `set` reference doesn't
  just supply the room — a video model reads it as the camera position too, and a shot written as "a
  close-up" without saying so can come back as the plate's own wide head-on view. State the angle,
  how much of the room is visible, and that it's *not* the plate's view. — observed,
  the project's multi-shot continuity notes "LAW — the set plate anchors COMPOSITION too".
- BWF-specific: an asymmetric `set` reference is safer than a symmetric one — a perfectly mirrored
  room (two chairs, two cups) gave a one-character shot two identical characters, and saying "there is
  exactly one" in the prompt did not fix it; only rewriting the framing did. — observed, same source.
- BWF-specific: the `set` reference (`refs: auto`, pinned first) should be a plate with **no
  people** in it — a plate with a figure fights the character references. — observed,
  the project's multi-shot continuity notes "LAW — the SET is a reference subject", and enforced/warned in spec §5.
- BWF-specific: a `character` reference should show the face plainly — facing camera or slightly
  turned, every feature visible, never a profile, even light, neutral background — and **not** lit
  like the finished film; mood belongs in the shot prompt, not the anchor. — observed,
  the project's short-film method notes §3 and the project's multi-shot continuity notes "Character anchors".
- A `character` reference shot from more than one angle (front/side/back/detail) gives the model a
  more accurate hold on the face than one frontal image alone. — documented (Kling Subject Binding
  blog), a survey of paid AI-film tools §2 borrow #4, source
  https://kling.ai/blog/kling-3-subject-binding-character-consistency.
- A `cable` (BWF's patch-cord object linking one slot's picture output to another slot's image field,
  e.g. first frame) is how a video shot inherits a starting frame from another slot's pick — an empty
  source refuses generation, and re-picking the source later marks the target **STALE ("first frame
  changed")**. — observed, spec §4.
- What's DIFFERENT because the shots come from a video model, not a camera: renders are short and a
  fixed length (BWF's H3 pack snaps to a 17-frame grid, minimum 124 frames ≈ 5.17 s); a model has no
  memory between separate renders, so nothing but the reference room ties one shot's room to the
  next; and a chained "continue from the last shot" made only from the last **frame** freezes or jumps
  the motion at the join and makes the sound restart 12–18 dB louder than it should. — observed,
  the project's frame-chaining continuity measurements (verdict + the "+15.1 dB" mean loudness-step
  measurement for frame-only chaining) and spec §1 R3 note on `snap_frames`.

## TAKES — trying a shot again, picking the one that works

- Every render of a slot becomes a `take`; nothing is overwritten, and the user `pick`s which take is
  the shot. BWF keeps every take forever by design — "remove take" only drops the record, never
  the file. — observed, spec §1 (`takes[]`/`pick`) and design ruling #6.
- Judge a take at full resolution, never from a small preview strip — a shot that looks fine small can
  show, at full size, that a face has drifted from the reference or a hand has the wrong count of
  fingers. Before calling a detail wrong, make sure the part of the frame actually being judged
  contains that detail — a wrong-color-eyes claim on the project's own film once turned out to be a
  crop that landed on the eyebrow. — observed, the project's multi-shot continuity notes "Traps, each one paid for".
- Nothing else in the market shows "what shots use this reference" as a queryable view — every paid
  tool examined shows only the input side (attach a reference at generate time). If BWF ever wants
  that view, there's no existing UI pattern to borrow. — observed (as an absence),
  a survey of paid AI-film tools §2 last bullet.

## CUT — the `sequence`'s CUT control, trims, titles, music

- The default trim is **tail-keep**: `in = clip_length − beat_length`, keeping the end of the take and
  cutting from the head. This is deliberate, not a default that happens to be there — H3 (and video
  models generally, per BWF's own measurement) put spoken dialogue **late** in a rendered clip, so
  trimming from the head instead would decapitate the line. Measured mean: the last line lands
  ~87% into a 5.17 s clip. — observed, the project's multi-shot continuity notes "LAW — H3 puts the line LATE" and
  spec §5's tail-keep law + the cut's design notes R3.
- A `title` (`{"text","at","dur"}`) only exists at cut time, burned in with `drawtext` — there is no
  title field on a render, so a typo or restyle never costs a re-render. — observed, spec §5, §6; the cut's
  design notes R6.
- Sound: one music bed (the first SOUND-lane slot with a picked, local take, in timeline order) is
  mixed **under** the clip audio at **−18 dB**, before a single loudness pass over the whole
  assembly; it starts at 0, is cut at the film's end, and never loops. — observed, spec design ruling
  #3 and the cut's design notes R7.
- Loudness is normalized **once, over the entire joined film**, never per shot — normalizing each
  shot on its own would flatten the intended dynamic between a near-silent bed and a loud line of
  dialogue. — observed, the project's multi-shot continuity notes "Master the assembly once, not each shot" and
  spec §5.
- Broadcast/EBU loudness convention: EBU R128 targets **−23 LUFS** integrated, ±0.5 LU tolerance (±1 LU
  for less predictable material), true peak never above **−1 dBTP**. This is the broadcast number, not
  the number a web-delivered short film should chase. — observed, https://tech.ebu.ch/docs/r/r128.pdf
  and https://en.wikipedia.org/wiki/EBU_R_128.
- Web/streaming convention (music and most short-form video delivery today): **−14 LUFS** integrated
  with a **−1 dBTP** true-peak ceiling covers Spotify, YouTube and Amazon; Apple Music normalizes to
  −16 LUFS. Mastering louder than the platform's target buys nothing once the platform turns it back
  down — you only keep any distortion from pushing too hot. — observed,
  https://www.forasoft.com/learn/audio-for-video/articles-audio/lufs-targets-per-platform-2026 and
  AES TD1008 (cited in that piece as the source the major platforms converged on).
- BWF's own cut pipeline targets **true peak ≤ −1.0 dBTP** as its frozen gate — consistent with both
  conventions above, and is the number to quote to the user rather than a raw LUFS integrated target,
  since the app's `loudnorm` pass is calibrated on ffmpeg 4.4.2, not tuned per-platform. —
  observed, the cut's design notes R4.
- The cut re-encodes at a **constant frame rate** (never `-c copy`/stream-copy) — a same-fps concat
  done with `-c copy` produced a file that *reported* 120/1 with an average of 23.58, invisible on
  playback but breaking downstream editing. — observed, the project's multi-shot continuity notes "Assembly gotchas"
  (the VFR bug) and spec finding 9 / the `-vsync cfr -r 24` fix.
- Only VIDEO-lane slots with a pick go into the cut, in timeline order; an unpicked slot is skipped
  and named in the cut's own record. A picked take whose file hasn't been copied down from its render
  lane yet gets one retry at cut time before the cut refuses, naming the shot. — observed,
  the cut's design notes R2.
- Every cut is hard cuts only — no dissolves, no sound bridges — in this version of BWF. — observed,
  spec §6 "Hard cuts only" and the project's short-film method notes "Every transition is a hard cut."
- No metadata from any input (prompts, comments) survives into the delivered file — `-map_metadata
  -1` strips it, on both the container and every stream. — observed, the cut's design notes R4.

## What the machine cannot judge — say this honestly, don't guess

- Whether the `set` plate is actually empty of people, or actually asymmetric. — observed, spec §5
  "Cannot check at all."
- Whether a `character` anchor is lit neutrally or shows every feature (front-on vs profile). —
  observed, spec §5.
- Whether a shot's prompt actually states the camera against the plate. — observed, spec §5.
- **Likeness at full resolution, and whether the set has drifted between shots — "nothing errors."**
  Each shot is plausible on its own; only a human looking at the sequence catches the room changing
  underneath it. — observed, spec §5 and the project's multi-shot continuity notes "The set drifts silently."
- Whether a take's audio actually contains the dialogue that was asked for (only that an audio stream
  exists). — observed, spec §5 and the project's multi-shot continuity notes "having an audio stream is not the same
  as containing sound."
- The app's own fixed line for this, which the Guide should echo rather than invent a softer version
  of: *"The room can't see faces or framing. Check each shot at full size."* — observed, spec §5.
